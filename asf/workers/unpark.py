"""``asf unpark <item>`` — the operator's way out of a park (F-0095 §2.5); ``asf park`` its way in.

A park is a pending correction carrying ``parked``; it is cleared only by a later run on the item
starting, and a park launches none, so it is permanent until this command records the exit. The
exit is one appended ledger line for the job that holds the park — the park and its undo both stay
in the file.

``asf park <item|branch> --why TEXT`` writes the same park by hand: one appended ledger line on
the job whose run holds the item (or the branch), a pending correction of kind ``operator park``
carrying ``parked`` and the reason — so the feeder shows the item ``PARKED <why>`` and launches
nothing on it, exactly as for a park the factory wrote, until ``asf unpark``. An item no run has
touched yet gets a run of its own to hold the park (job ``park-<item>``, kind ``park``: no pid,
ended the moment it is written — it holds no seat and is not counted as an attempt)."""
import datetime
import re

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod


def parked_job(path, item):
    """``(job, correction)`` of the newest pending correction on ``item``, or ``(None, None)``."""
    held = []
    for rs in lifecycle.runs(path).values():
        for r in rs:
            if r.get('item') != item:
                continue
            corr = lifecycle.pending_correction(r, path)
            if corr:
                held.append((corr.get('at') or '', r.get('job'), corr))
    if not held:
        return None, None
    _at, job, corr = max(held, key=lambda h: h[0])
    return job, corr


def cmd_unpark(args):
    product = env.load_product(getattr(args, 'product', None))
    item = (args.item or '').strip().upper()
    path = pool_mod.sessions_path(product)
    known = any(r.get('item') == item for rs in lifecycle.runs(path).values() for r in rs)
    if not known:
        print(f'asf unpark: {item} is not in the ledger — nothing to unpark')
        return 1
    job, corr = parked_job(path, item)
    if not corr or not corr.get('parked'):
        print(f'asf unpark: {item} is not parked — nothing to undo')
        return 1
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    why = getattr(args, 'why', None) or ''
    pool_mod.update_session(product, job, correction=None, unparked=now, unpark_why=why)
    print(f'unparked {item} (job {job}): {corr.get("reason") or ""}'.rstrip(': '))
    if why:
        print(f'why: {why}')
    return 0


#: An item id, as ``asf park`` reads its argument.
ITEM_RE = re.compile(r'[A-Z]+-\d+')
#: The correction kind of a park written by hand.
OPERATOR_PARK = 'operator park'
#: The kind of the run that holds a hand park on an item no run has touched.
PARK_KIND = 'park'


def _target(path, arg):
    """``(item, job)`` a park on ``arg`` is written to: ``arg`` read as an item id (the job of its
    newest run, or None when it has none), else as a branch (the job of the newest run on it).
    ``(None, None)`` when neither names anything."""
    latest = lifecycle.latest(path)
    item = arg.strip().upper()
    runs = [(r.get('started') or '', job, r) for job, r in latest.items() if r.get('item') == item]
    if runs:
        return item, max(runs, key=lambda x: x[0])[1]
    on_branch = [(r.get('started') or '', job, r) for job, r in latest.items()
                 if r.get('branch') == arg.strip()]
    if on_branch:
        _s, job, run = max(on_branch, key=lambda x: x[0])
        return run.get('item'), job
    if ITEM_RE.fullmatch(item):
        return item, None
    return None, None


def cmd_park(args):
    product = env.load_product(getattr(args, 'product', None))
    why = ' '.join(str(getattr(args, 'why', '') or '').split())
    if not why:
        print('asf park: --why is required — the reason is what the PARKED row shows')
        return 2
    path = pool_mod.sessions_path(product)
    item, job = _target(path, args.target or '')
    if not item:
        print(f'asf park: {args.target} names no item id and no branch in the ledger')
        return 1
    _job, corr = parked_job(path, item)
    if corr and corr.get('parked'):
        print(f'asf park: {item} is already parked: {corr.get("reason") or corr.get("text")}')
        return 1
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    reason = f'{OPERATOR_PARK}: {why}'
    correction = {'kind': OPERATOR_PARK, 'text': reason, 'at': now, 'parked': True,
                  'reason': reason}
    if job is None:
        job = f'{PARK_KIND}-{item}'.lower()
        pool_mod.append_session(product, {'job': job, 'item': item, 'kind': PARK_KIND,
                                          'started': now, 'ended': now, 'end_reason': PARK_KIND,
                                          'correction': correction, 'operator_flagged': 1})
    else:
        pool_mod.update_session(product, job, correction=correction, operator_flagged=1)
    print(f'parked {item} (job {job}): {why} — `asf unpark {item}` releases it')
    return 0


def register(sub):
    """``asf unpark <item> [--why TEXT] [--product P]`` and ``asf park <item|branch> --why TEXT
    [--product P]``."""
    p = sub.add_parser('unpark', help='release an item the empty-end park is holding')
    p.add_argument('item')
    p.add_argument('--why', help='why the park is released (recorded beside it)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_unpark)
    p = sub.add_parser('park', help='hold an item (or a branch) deliberately until asf unpark')
    p.add_argument('target', help='an item id, or a branch a run of the ledger is on')
    p.add_argument('--why', required=True, help='why it is held (shown on its PARKED row)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_park)
