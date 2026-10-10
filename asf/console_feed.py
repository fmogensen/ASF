"""asf.console_feed — B-0121: the FACTORY STATUS table ticks in every console on its own clock,
instead of the operator typing ``/loop 5m /asf:status`` by hand every session.

``/asf:console-feed`` (:func:`run_once`, dispatched as the ``console-feed`` command) is what the
loop calls each tick: the FACTORY STATUS table (:mod:`asf.views.status`) plus the tick digest
lines (:mod:`asf.tick.watch`) landed since the last call — nothing, silently, when
``status_every`` says it is not due yet. A per-product marker
(``state/<product>/console-feed.json``) is this command's own; nothing else reads or writes it.

``console-feed-hint`` runs once from the plugin's generated ``SessionStart`` hook
(:mod:`asf.plugin_build`) and prints the one line that starts the loop — so "no session has to
remember to start a loop" (the card's own words) is true in code, not just in a console's memory.
:func:`resolve_every` decides the interval: the product's own ``conventions.flags
.console_status_every`` first (a flag, never a new top-level product key — see
``tests.test_env.PinnedReader``), then the operator's ``config.yaml`` ``console.status_every``,
else :data:`DEFAULT_EVERY`; ``off`` (or ``0``) prints nothing at all.
"""
import datetime
import json
import os

#: ``console.status_every``'s default — five minutes, same as the operator's own ``/loop 5m
#: /asf:status`` habit the card names.
DEFAULT_EVERY = '5m'

_UNITS = {'s': 1, 'm': 60, 'h': 3600}


def parse_every(value):
    """Seconds, or ``None`` when the feed is off. ``0``/``off``/``none`` disable it; a bare
    number is minutes; a unit-suffixed one (``30s``, ``5m``, ``1h``) is itself. Anything else —
    including an unset value — falls back to :data:`DEFAULT_EVERY`: a typo must never silently
    mean "never show the table again"."""
    if isinstance(value, bool):
        return parse_every(DEFAULT_EVERY) if value else None
    if isinstance(value, (int, float)):
        return int(value * 60) if value > 0 else None
    text = str(value if value is not None else DEFAULT_EVERY).strip().lower()
    if text in ('off', 'none', '0', ''):
        return None
    if text[-1] in _UNITS and text[:-1].replace('.', '', 1).isdigit():
        return int(float(text[:-1]) * _UNITS[text[-1]])
    if text.isdigit():
        return int(text) * 60
    return parse_every(DEFAULT_EVERY)


#: the product-file override, under ``conventions.flags`` (never a new top-level key: a product
#: file is read by whichever ``asf`` is pinned to it, and only ``flags`` is read verbatim at
#: every pinned version — see ``tests.test_env.PinnedReader``).
_PRODUCT_FLAG = 'console_status_every'


def resolve_every(product, config):
    """``console.status_every``: the product's own ``conventions.flags.console_status_every``
    wins, else the operator's ``config.yaml`` ``console.status_every``, else
    :data:`DEFAULT_EVERY`."""
    if product is not None:
        value = product.flag(_PRODUCT_FLAG)
        if value is not None:
            return parse_every(value)
    cfg_console = (config or {}).get('console')
    if isinstance(cfg_console, dict) and 'status_every' in cfg_console:
        return parse_every(cfg_console['status_every'])
    return parse_every(DEFAULT_EVERY)


def _label(seconds):
    return f'{seconds // 60}m' if seconds % 60 == 0 else f'{seconds}s'


def hint_line(seconds):
    """What the ``SessionStart`` hook prints when the feed is on — the one instruction that
    starts the loop, worded for the model reading it as context at the top of a session."""
    label = _label(seconds)
    return (f'ASF console feed: start it now — invoke `/loop {label} /asf:console-feed` so the '
            f'FACTORY STATUS table ticks every {label} with no further reminder.')


def _marker_path(product):
    from asf import env
    return os.path.join(env.state_dir(product), 'console-feed.json')


def _read_marker(product):
    try:
        with open(_marker_path(product), encoding='utf-8') as f:
            ts = json.load(f).get('ts')
    except (OSError, ValueError):
        return None
    try:
        return datetime.datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _write_marker(product, now):
    path = _marker_path(product)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'ts': now.isoformat()}, f)


def due(last, now, interval_seconds):
    """Whether it has been long enough since ``last`` (``None`` — no marker yet — is always
    due, so the very first call of a session shows the table at once, not five minutes in)."""
    if interval_seconds is None:
        return False
    if last is None:
        return True
    return (now - last).total_seconds() >= interval_seconds


def _ticks_since(product, since):
    """The tick digest lines (:func:`asf.tick.watch.digest_lines`) landed after ``since`` —
    ``[]`` with no marker to delta from. Read from the live tick's own clone
    (:func:`asf.tick.shadow.record_dir`, the same source ``asf watch`` tails), never the
    operator's checkout, which only catches up once a tick pushes. Reads only the clock's own
    UTC day: a feed gap that spans midnight shows fewer deltas, which is the smallest cost of
    the smallest fix."""
    if since is None:
        return []
    from asf.tick import shadow, watch
    root = shadow.record_dir(product)
    path = os.path.join(watch._ticks_dir(root), since.strftime('%Y-%m-%d') + '.jsonl')
    if not os.path.isfile(path):
        return []
    cutoff = since.strftime('%Y-%m-%dT%H:%M:%SZ')
    out = []
    with open(path, encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get('ts', '') <= cutoff:
                continue
            out.extend(watch.digest_lines(rec))
    return out


def run_once(product, root, now, render_status=None):
    """What ``/asf:console-feed`` prints: the FACTORY STATUS table plus the tick deltas since
    the last call, when ``console.status_every`` says enough time has passed — ``''`` otherwise,
    so a loop tick between the clock's own ticks prints no empty line. Advances this product's
    marker to ``now`` on every print. ``render_status`` is injected for a fixed-clock test; the
    live default is :func:`asf.views.status.render`, as heavy and as live as ``asf status``
    itself."""
    from asf import env
    config = env.load_config()
    interval = resolve_every(product, config)
    last = _read_marker(product)
    if not due(last, now, interval):
        return ''
    if render_status is None:
        from asf.views import status as status_view
        render_status = lambda: status_view.render(root, product)
    lines = [render_status().rstrip('\n')]
    ticks = _ticks_since(product, last)
    if ticks:
        lines.append('')
        lines.extend(ticks)
    _write_marker(product, now)
    return '\n'.join(lines) + '\n'


def cmd_console_feed(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(run_once(product, root, datetime.datetime.now(datetime.timezone.utc)), end='')
    return 0


def cmd_console_feed_hint(args):
    """The ``SessionStart`` hook's own command (B-0121): one line, only when a product resolves
    (``--product``/``$ASF_PRODUCT``/the cwd/``default_product``) and its feed is not ``off`` —
    silent otherwise, so a console with no ASF product configured gets nothing extra."""
    from asf import env
    try:
        product = env.load_product(getattr(args, 'product', None))
        config = env.load_config()
    except env.ConfigError:
        return 0
    seconds = resolve_every(product, config)
    if seconds is not None:
        print(hint_line(seconds))
    return 0


def register_hint(sub):
    p = sub.add_parser('console-feed-hint', help='the SessionStart hook: one line that starts the status feed loop')
    p.add_argument('--product', default=None)
    p.set_defaults(run=cmd_console_feed_hint)
    return p
