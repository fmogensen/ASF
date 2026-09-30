"""``asf unpark <item|branch|job>`` — the operator's way out of a park (F-0095 §2.5); ``asf park``
its way in.

A park the factory writes (the relaunch cap, an empty or blocked end, a security hold) is a
pending correction carrying ``parked`` on a run; it keeps the scope it was written with. A park
written by hand, ``asf park <target> --why TEXT``, is its own registry line
(:func:`asf.workers.lifecycle.note_park`) — never a run, so no later run answers it, no closed-PR
reset retires it and no correction overwrites it — at the scope its target names:

* an item id holds the item: every row of it shows ``PARKED``;
* a branch holds that branch's rows alone — the item's other branches (a CORRECT round on its
  code branch while its plan branch is parked) go on;
* a job (``<kind>-<item>``) holds that job's rows alone.

``asf unpark`` takes the same targets: an item id releases every park on the item (by hand or by
the factory, at any scope), a branch or a job the parks on it. Each release is one appended line;
the park and its undo both stay in the file."""
import datetime
import re

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod


def parked_job(path, item):
    """``(job, correction)`` of the newest pending park-carrying correction on ``item``, or
    ``(None, None)``."""
    held = factory_parks(path, lambda r: r.get('item') == item)
    if not held:
        return None, None
    return max(held, key=lambda h: h[1].get('at') or '')


def factory_parks(path, match):
    """``[(job, correction)]``: every job whose latest run ``match`` accepts and holds a pending
    correction carrying ``parked``."""
    out = []
    for job, r in lifecycle.latest(path).items():
        if not match(r):
            continue
        corr = lifecycle.pending_correction(r, path)
        if corr and corr.get('parked'):
            out.append((job, corr))
    return out


#: An item id, as ``asf park`` reads its argument.
ITEM_RE = re.compile(r'[A-Z]+-\d+')
#: The correction kind of a park written by hand.
OPERATOR_PARK = lifecycle.OPERATOR_PARK


def _target(path, arg):
    """``(scope, item, branch, job)`` ``arg`` names: an item id (item scope), a job of the
    ledger (job scope, on its latest run's item and branch), or a branch a run is on (branch
    scope, on the item of the newest run there). ``(None, None, None, None)`` when it names
    nothing."""
    arg = (arg or '').strip()
    if ITEM_RE.fullmatch(arg.upper()):
        return lifecycle.SCOPE_ITEM, arg.upper(), '', ''
    latest = lifecycle.latest(path)
    run = latest.get(arg) or latest.get(arg.lower())
    if run and run.get('item'):
        return lifecycle.SCOPE_JOB, run['item'], run.get('branch') or '', run['job']
    on_branch = [(r.get('started') or '', r) for r in latest.values()
                 if r.get('branch') == arg and r.get('item')]
    if on_branch:
        run = max(on_branch, key=lambda x: x[0])[1]
        return lifecycle.SCOPE_BRANCH, run['item'], arg, ''
    return None, None, None, None


def _scope_text(scope, item, branch, job):
    if scope == lifecycle.SCOPE_BRANCH:
        return f'branch {branch}'
    if scope == lifecycle.SCOPE_JOB:
        return f'job {job}'
    return f'item {item}'


def cmd_unpark(args):
    product = env.load_product(getattr(args, 'product', None))
    arg = (args.item or '').strip()
    path = pool_mod.sessions_path(product)
    scope, item, branch, job = _target(path, arg)
    if scope is None:
        print(f'asf unpark: {arg} names no item id, job or branch in the ledger')
        return 1
    known = any(r.get('item') == item for rs in lifecycle.runs(path).values() for r in rs) \
        or any(p['item'] == item for p in lifecycle.parks(path))
    if not known:
        print(f'asf unpark: {item} is not in the ledger — nothing to unpark')
        return 1

    def holds(p):
        if p['item'] != item:
            return False
        if scope == lifecycle.SCOPE_BRANCH:
            return p.get('scope') == lifecycle.SCOPE_BRANCH and p.get('branch') == branch
        if scope == lifecycle.SCOPE_JOB:
            return p.get('scope') == lifecycle.SCOPE_JOB and p.get('on_job') == job
        return True
    by_hand = [p for p in lifecycle.parks(path) if holds(p)]
    match = {lifecycle.SCOPE_BRANCH: lambda r: r.get('branch') == branch,
             lifecycle.SCOPE_JOB: lambda r: r.get('job') == job}.get(
                 scope, lambda r: r.get('item') == item)
    by_factory = factory_parks(path, match)
    if not by_hand and not by_factory:
        print(f'asf unpark: {_scope_text(scope, item, branch, job)} is not parked — nothing to undo')
        return 1
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    why = getattr(args, 'why', None) or ''
    for p in by_hand:
        lifecycle.note_unpark(path, p, why=why, now=now)
        where = _scope_text(p.get('scope'), p['item'], p.get('branch'), p.get('on_job'))
        print(f'unparked {p["item"]} ({where}, park job {p["job"]}): {p.get("reason") or ""}'
              .rstrip(': '))
    for fjob, corr in by_factory:
        pool_mod.update_session(product, fjob, correction=None, unparked=now, unpark_why=why)
        print(f'unparked {item} (job {fjob}): {corr.get("reason") or ""}'.rstrip(': '))
    if why:
        print(f'why: {why}')
    return 0


def cmd_park(args):
    product = env.load_product(getattr(args, 'product', None))
    why = ' '.join(str(getattr(args, 'why', '') or '').split())
    if not why:
        print('asf park: --why is required — the reason is what the PARKED row shows')
        return 2
    path = pool_mod.sessions_path(product)
    scope, item, branch, job = _target(path, args.target or '')
    if scope is None:
        print(f'asf park: {args.target} names no item id, job or branch in the ledger')
        return 1
    where = _scope_text(scope, item, branch, job)
    pjob = lifecycle.park_job(scope, {lifecycle.SCOPE_BRANCH: branch,
                                      lifecycle.SCOPE_JOB: job}.get(scope) or item)
    standing = {p['job']: p for p in lifecycle.parks(path)}
    if pjob in standing:
        print(f'asf park: {where} is already parked: {standing[pjob].get("reason")}')
        return 1
    reason = f'{OPERATOR_PARK} on {where}: {why}'
    lifecycle.note_park(path, item, scope, reason, why, branch=branch, on_job=job)
    rest = '' if scope == lifecycle.SCOPE_ITEM else f"; {item}'s other rows go on"
    print(f'parked {item} at {where} (park job {pjob}{rest}): {why} — '
          f'`asf unpark {args.target.strip()}` releases it')
    return 0


def register(sub):
    """``asf unpark <item|branch|job> [--why TEXT] [--product P]`` and ``asf park
    <item|branch|job> --why TEXT [--product P]``."""
    p = sub.add_parser('unpark', help='release a park: an item id releases every park on it, a '
                                      'branch or job the parks on that branch or job')
    p.add_argument('item', help='an item id, or a branch or job a park holds')
    p.add_argument('--why', help='why the park is released (recorded beside it)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_unpark)
    p = sub.add_parser('park', help='hold an item, or one branch or job of it, until asf unpark')
    p.add_argument('target', help='an item id (holds the item), a branch a run is on (holds that '
                                  'branch only) or a job of the ledger (holds that job only)')
    p.add_argument('--why', required=True, help='why it is held (shown on its PARKED row)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_park)
