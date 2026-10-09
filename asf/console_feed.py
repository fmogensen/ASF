"""asf.console_feed — B-0121: the FACTORY STATUS table refreshes itself in the console, so no
session has to remember `/loop 5m /asf:status` by hand (the console half of B-0087, which built
the per-tick digest and `asf watch` but never wired either into a console).

`asf console-feed --product P` is one tick of the feed: the FACTORY STATUS table
(:func:`asf.views.status.render`, the same table `/asf:status` prints) and the tick digest lines
(:func:`asf.tick.watch.digest_lines`) written to the ticks stream since this feed's own last
print for `product` — a small marker file (:func:`_state_path`), never a replay, and never a
clone, fetch or reset of the tick's own record clone (:mod:`asf.tick.shadow`): like `asf watch`,
this only reads what a tick already wrote there. It ends by naming the interval to call it again
at — repeating the call on a clock is the console's own job (its `SessionStart` hook,
:mod:`asf.plugin_build`, says so at every session start), not a loop this command runs itself.

The interval is `console.status_every` in `~/.ASF/config.yaml`; a product's own
`conventions.flags.status_every` overrides it. Either is a duration
(:func:`asf.tick.stale.limit_seconds`'s syntax, e.g. `5m`, `90s`), or `off`/`0` to turn the feed
off for that product; unset, :data:`DEFAULT_EVERY_S`.
"""
import json
import os

from asf import env
from asf.tick import shadow, stale, watch

#: the feed's own default period, same order of magnitude as the manual `/loop 5m /asf:status`
#: it replaces.
DEFAULT_EVERY_S = 300


def _raw_every(product, cfg=None):
    flag = product.flag('status_every') if product is not None else None
    if flag is not None:
        return flag
    cfg = env.load_config() if cfg is None else cfg
    return (cfg.get('console') or {}).get('status_every')


def every_s(product, cfg=None):
    """Seconds between feed ticks for `product`, or `None` while the feed is off for it
    (`console.status_every`/`conventions.flags.status_every` is `off` or `0`)."""
    raw = _raw_every(product, cfg)
    if raw is None:
        return DEFAULT_EVERY_S
    if str(raw).strip().lower() in ('off', '0'):
        return None
    try:
        seconds = stale.limit_seconds(str(raw).strip())
    except ValueError:
        return DEFAULT_EVERY_S
    return seconds or None


def _state_path(product):
    return os.path.join(env.state_dir(product), 'console-feed.json')


def _load_state(product):
    try:
        with open(_state_path(product), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(product, state):
    path = _state_path(product)
    tmp = f'{path}.tmp-{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def tick_lines(product, root, now=None):
    """The FACTORY STATUS table, then (only when there is one) the digest of every tick line
    `product`'s ticks stream gained since this feed's own last call for it — never a line this
    feed already showed, the same one-shot-per-line guarantee as `asf watch`."""
    from asf.views import status
    now = now or watch._utcnow
    lines = [status.render(root, product).rstrip('\n')]
    path = watch._today_path(shadow.record_dir(product), now)
    state = _load_state(product)
    # no prior state for this path: start at its current end, the same as `asf watch` starting
    # up — a feed's first-ever call never replays a day's ticks from before it existed.
    start = os.path.getsize(path) if os.path.isfile(path) else 0
    pos = state.get('pos', start) if state.get('path') == path else start
    new_lines, pos = watch._read_new_lines(path, pos)
    _save_state(product, {'path': path, 'pos': pos})
    digest = []
    for raw in new_lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        digest.extend(watch.digest_lines(rec))
    if digest:
        lines.append('')
        lines.append('since the last print:')
        lines.extend(digest)
    return lines


def run(product, root, out=print, now=None):
    interval = every_s(product)
    if interval is None:
        out(f'console-feed: console.status_every is off for {product.name} — nothing to do')
        return 0
    for line in tick_lines(product, root, now):
        out(line)
    out('')
    out(f'console-feed: call `asf console-feed --product {product.name}` again in '
        f'{stale.format_age(interval)} to keep this table current — no operator loop needed.')
    return 0


def cmd_console_feed(args, root):
    product = env.load_product(getattr(args, 'product', None))
    return run(product, root)
