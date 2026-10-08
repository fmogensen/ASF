"""asf.pause — one product's launches held, while its tick goes on recording and harvesting.

``asf pause`` writes ``state/<product>/paused-launches.json`` (reason, who, when); every door the
tick opens onto a new session asks :func:`held` before it opens. Nothing else changes: ``record``,
``health``, ``groom``, ``prs`` and ``harvest`` run as they always did, the lane pass still moves
finished branches to the gate, and the sessions already running are never touched — they finish,
they push, and the harvest lands them under the pause.

This is not :func:`asf.scheduler.pause`, which boots the product's launchd clock out so that
*nothing* ticks. Both are durable, both carry a reason; this one stops starting, that one stops
everything.
"""
import datetime
import json
import os

from asf import env

PAUSE_FILE = 'paused-launches.json'


def pause_path(product_name):
    return os.path.join(env.ASF_HOME, 'state', product_name, PAUSE_FILE)


def read(product_name):
    """``{'reason', 'by', 'at'}`` while ``product_name``'s launches are paused, else ``None``.
    An unreadable or malformed file is no pause: a pause nobody can read must never be able to
    stop the floor silently."""
    try:
        with open(pause_path(product_name), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def held(product):
    """:func:`read` for a :class:`asf.env.Product` or a name — the one predicate every door asks."""
    name = getattr(product, 'name', None) or (product if isinstance(product, str) else None)
    return None if name is None else read(name)


def text(record):
    """``paused since <at> (<reason>; by <who>)`` — the same sentence shape as
    :func:`asf.scheduler.pause_text`, so the two pauses read alike on one status table."""
    return (f"paused since {record.get('at') or '?'} ({record.get('reason') or 'no reason'}; "
            f"by {record.get('by') or '?'})")


def hold_reason(record):
    """The wave's ``waits`` reason: ``launches paused: <reason> (by <who>, since <at>)``."""
    return (f"launches paused: {record.get('reason') or 'no reason'} "
            f"(by {record.get('by') or '?'}, since {record.get('at') or '?'})")


def pause(product_name, reason, by, now=None):
    """Write the record; returns the lines to print. Overwrites an existing pause (a new reason
    replaces the old one, and the ``at`` moves) rather than refusing."""
    at = (now or datetime.datetime.now()).astimezone().isoformat(timespec='seconds')
    rec = {'reason': reason, 'by': by, 'at': at}
    path = pause_path(product_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rec, f, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)
    return [f'pause: {product_name} launches paused — {reason} (by {by})',
            f'       `asf resume --product {product_name}` lifts it.']


def resume(product_name):
    """Remove the record; returns the lines to print. Resuming what is not paused is not an
    error — it is the operator making sure."""
    path = pause_path(product_name)
    if os.path.exists(path):
        os.remove(path)
    return [f'pause: {product_name} launches resumed — the next wave starts what the feeder plans']


def cmd_pause(args):
    product = env.load_product(getattr(args, 'product', None))
    reason = (getattr(args, 'reason', None) or '').strip()
    if not reason:
        print('asf pause needs --reason "<why>" — it is recorded with the pause')
        return 2
    by = getattr(args, 'by', None) or os.environ.get('USER') or '?'
    first, last = pause(product.name, reason, by)
    try:
        from asf import capacity
        in_flight = capacity.inflight_sessions(product.name)
    except Exception:  # noqa: BLE001 — a count that cannot be read is 0, never a refusal
        in_flight = 0
    for line in (first,
                 f'       record, health, groom, prs and harvest go on; the {in_flight} sessions '
                 f'in flight finish and land.',
                 last):
        print(line)
    return 0


def cmd_resume(args):
    product = env.load_product(getattr(args, 'product', None))
    for line in resume(product.name):
        print(line)
    return 0


def register(subparsers):
    p_pause = subparsers.add_parser(
        'pause', help='hold one product\'s launches — the clocks keep ticking: record and '
                      'harvest go on, nothing launches (see `asf scheduler pause` to stop the '
                      'clock itself)')
    p_pause.add_argument('--product')
    p_pause.add_argument('--reason', help='required: recorded with the pause')
    p_pause.add_argument('--by', help='defaults to $USER')
    p_pause.set_defaults(run=cmd_pause)

    p_resume = subparsers.add_parser(
        'resume', help='lift a launch pause (`asf pause`) — see `asf scheduler resume` for the '
                       'clock itself')
    p_resume.add_argument('--product')
    p_resume.set_defaults(run=cmd_resume)
    return p_pause, p_resume
