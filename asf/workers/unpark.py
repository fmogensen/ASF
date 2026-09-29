"""``asf unpark <item>`` — the operator's way out of a park (F-0095 §2.5).

A park is a pending correction carrying ``parked``; it is cleared only by a later run on the item
starting, and a park launches none, so it is permanent until this command records the exit. The
exit is one appended line per parked job: every park the item holds goes in one call, and the
parks and their undos all stay in the file."""
import datetime

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod


def parked_jobs(path, item):
    """Every park ``item`` is holding now: ``[(job, correction), ...]`` oldest first, one entry
    per job (P4: only one park per job is ever pending, and a job takes one undo line whatever
    the ledger holds). Read once, before any line is appended — the order and the count the
    operator is told are this list's."""
    held = {}
    for run in lifecycle.item_runs(path, item):
        corr = lifecycle.pending_correction(run, path)
        if not corr or not corr.get('parked'):
            continue
        job = run.get('job')
        prev = held.get(job)
        if prev is None or (corr.get('at') or '') >= (prev.get('at') or ''):
            held[job] = corr
    return sorted(held.items(), key=lambda jc: ((jc[1].get('at') or ''), jc[0]))


def still_holding(path, item):
    """``[(job, correction), ...]`` of every correction still pending on ``item`` — read after
    the undo lines are appended, so what it names is what the next tick will see (P6)."""
    out = []
    for run in lifecycle.item_runs(path, item):
        corr = lifecycle.pending_correction(run, path)
        if corr:
            out.append((run.get('job'), corr))
    return sorted(out, key=lambda jc: ((jc[1].get('at') or ''), jc[0]))


def cmd_unpark(args):
    product = env.load_product(getattr(args, 'product', None))
    item = (args.item or '').strip().upper()
    path = pool_mod.sessions_path(product)
    known = any(r.get('item') == item for rs in lifecycle.runs(path).values() for r in rs)
    if not known:
        print(f'asf unpark: {item} is not in the ledger — nothing to unpark')
        return 1
    parks = parked_jobs(path, item)
    if not parks:
        print(f'asf unpark: {item} is not parked — nothing to undo')
        return 1
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    why = getattr(args, 'why', None) or ''
    for job, corr in parks:
        pool_mod.update_session(product, job, correction=None, unparked=now, unpark_why=why)
        print(f'unparked {item} (job {job}): {corr.get("reason") or ""}'.rstrip(': '))
    left = still_holding(path, item)
    released = f'released {len(parks)} park{"s" if len(parks) != 1 else ""} on {item}'
    if left:
        released += (f' — {len(left)} correction still holds it' if len(left) == 1
                     else f' — {len(left)} corrections still hold it')
    print(released)
    for job, corr in left:
        what = 'still parked' if corr.get('parked') else 'still held'
        line = f'{what} ({job}): {corr.get("reason") or corr.get("text") or ""}'.rstrip(': ')
        print(line + (f' — run `asf unpark {item}` again' if corr.get('parked') else ''))
    if why:
        print(f'why: {why}')
    return 0


def register(sub):
    """``asf unpark <item> [--why TEXT] [--product P]``."""
    p = sub.add_parser('unpark', help='release every park an item is holding')
    p.add_argument('item')
    p.add_argument('--why', help='why the park is released (recorded beside it)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_unpark)
