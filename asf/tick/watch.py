"""asf.tick.watch — ``asf watch``: tail the ticks stream so the console operator sees what a
tick did without running one themselves.

Every ``asf tick`` (:mod:`asf.tick.tick`) appends one line to ``metrics/ticks/<day>.jsonl`` in
its record clone (:func:`asf.tick.tick.write_tick_line`) — the counts, the steps, the duration —
whether it was this console or the autopilot daemon that ran it. ``watch`` reads that file back,
oldest-unseen line first, and prints each one through the same digest a tick prints for itself
(:func:`asf.tick.summary.digest`): read-only, never a clone, fetch or reset of the record — the
clone is the tick's own, and racing its ``git reset --hard`` (:func:`asf.tick.shadow.ensure_clone`)
is exactly what this never does. Only the lines written after ``watch`` started are shown, the
same way ``tail -f`` picks up from where it is invoked.
"""
import datetime
import json
import os
import time

from asf.tick import shadow, summary

POLL_SECONDS = 5

COUNTER_KEYS = ('launches', 'merges', 'stalls', 'refusals', 'relaunches')


def _ticks_dir(root):
    return os.path.join(root, 'metrics', 'ticks')


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _today_path(root, now=_utcnow):
    day = now().strftime('%Y-%m-%d')
    return os.path.join(_ticks_dir(root), f'{day}.jsonl')


def _read_new_lines(path, pos):
    """Complete lines appended past ``pos``, and the position past them — a line still being
    written (no trailing ``\\n`` yet) is left for the next read."""
    try:
        with open(path, 'rb') as f:
            f.seek(pos)
            chunk = f.read()
    except OSError:
        return [], pos
    if not chunk:
        return [], pos
    end = chunk.rfind(b'\n')
    if end == -1:
        return [], pos
    complete, pos = chunk[:end], pos + end + 1
    return [ln.decode('utf-8', errors='replace') for ln in complete.split(b'\n') if ln.strip()], pos


def digest_lines(rec):
    """One tick line (:func:`asf.tick.tick.tick_line`), rendered the way the tick itself would
    print it — the same two lines :func:`asf.tick.summary.digest` builds from ``steps`` and the
    counters, ``ts`` first so a tick watched later still says when it ran."""
    ran = rec.get('steps') or []
    counts = {k: rec.get(k, 0) for k in COUNTER_KEYS}
    return [f"{rec.get('ts', '?')} {line}" for line in summary.digest(ran, counts)]


def run(product, out=print, sleep=time.sleep, poll=POLL_SECONDS, forever=True, now=_utcnow):
    """Tail ``product``'s ticks stream, printing each tick as it lands. Never returns unless
    ``forever`` is False (tests) or interrupted — the operator's ``ctrl-c`` is the only exit.
    ``now`` is the UTC clock that names the day's file — injected so a test is not dated."""
    root = shadow.record_dir(product)
    out(f"watch: tailing {product.name}'s ticks in {root} (ctrl-c to stop)")
    path = _today_path(root, now)
    pos = os.path.getsize(path) if os.path.isfile(path) else 0
    while True:
        current = _today_path(root, now)
        if current != path:
            path, pos = current, 0
        lines, pos = _read_new_lines(path, pos)
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            for line in digest_lines(rec):
                out(line)
        if not forever:
            return 0
        sleep(poll)


def register(subparsers):
    """Add the ``watch`` subcommand."""
    p = subparsers.add_parser(
        'watch', help="tail the product's ticks stream — what the last one did, and the next "
                      "one as it lands (read-only, never a clone or a tick of its own)")
    p.add_argument('--product')
    p.add_argument('--poll', type=float, default=POLL_SECONDS,
                   help=f'seconds between reads (default: {POLL_SECONDS})')
    return p


def cmd_watch(args):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    poll = getattr(args, 'poll', None) or POLL_SECONDS
    try:
        return run(product, poll=poll)
    except KeyboardInterrupt:
        return 0
