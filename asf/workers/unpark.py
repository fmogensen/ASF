"""``asf unpark <item>`` — the operator's way out of a park (F-0095 §2.5).

A park is a pending correction carrying ``parked``; it is cleared only by a later run on the item
starting, and a park launches none, so it is permanent until this command records the exit. The
exit is one appended ledger line for the job that holds the park — the park and its undo both stay
in the file."""
import datetime

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


def register(sub):
    """``asf unpark <item> [--why TEXT] [--product P]``."""
    p = sub.add_parser('unpark', help='release an item the empty-end park is holding')
    p.add_argument('item')
    p.add_argument('--why', help='why the park is released (recorded beside it)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_unpark)
