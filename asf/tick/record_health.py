"""asf.tick.record_health — the record step's own health (B-0124): whether the last tick's
record step refreshed the index, and — when it didn't — since when and how many ticks running.

Never git-tracked and never read through the record clone: a failing record step is exactly the
case where the clone's own push can't reach origin, so nothing written to it survives to the next
tick. This is one local file per product, under its state dir, written on every tick's record
step — landed or not — so ``asf status`` and the per-tick digest (B-0087) can both say the table
is stale instead of printing yesterday's numbers as if they were fresh.
"""
import datetime
import json
import os

from asf import env


def path(product):
    return os.path.join(env.state_dir(product), 'record-health.json')


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read(product):
    try:
        with open(path(product), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def record(product, ok, reason=None, checkout=None, now=None):
    """Write the record step's outcome. A success clears any streak; a failure extends the
    previous stamp's streak (``since``, ``ticks``) when it was already failing, else starts a new
    one at this tick.

    ``checkout`` is the operator checkout's sync refusal (:data:`asf.tick.shadow.Sync`, as stored
    by :func:`update`) or ``None``: a record step that landed but could not carry its push into
    the checkout the read views read (F-0260). It is kept whatever ``ok`` says, because the two
    are independent — defaulting to whatever is already on file so the record step's own call,
    which names neither, neither sets nor clears it (PD7)."""
    now = now or _stamp()
    prev = read(product) or {}
    if checkout is None:
        checkout = prev.get('checkout')
    if ok:
        data = {'ok': True, 'ts': now}
    else:
        streak = not prev.get('ok', True)
        data = {
            'ok': False,
            'ts': now,
            'since': (prev.get('since') if streak else None) or now,
            'ticks': ((prev.get('ticks') or 0) + 1) if streak else 1,
            'reason': reason or 'see tick log',
        }
    if checkout is not None:
        data['checkout'] = checkout
    p = path(product)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return data


def update(product, checkout=None):
    """The one setter and clearer of the ``checkout`` field (F-0260 PD7): read, merge, write.
    ``checkout`` a :data:`asf.tick.shadow.Sync` whose refusal is kept, with its own
    ``since``/``ticks`` streak — independent of ``ok``'s, which :func:`record` alone keeps — or
    ``None``, which clears the field. Pure over the checkout's current state, with no ledger of
    what it once was: said until it is gone (D5). A missing file is created with the field alone,
    and no ``ok`` key."""
    data = read(product) or {}
    if checkout is None:
        data.pop('checkout', None)
    else:
        prev = data.get('checkout') or {}
        streak = prev.get('why') is not None
        data['checkout'] = {
            'why': checkout.why,
            'behind': checkout.behind,
            'paths': list(checkout.paths),
            'detail': checkout.detail,
            'since': (prev.get('since') if streak else None) or _stamp(),
            'ticks': ((prev.get('ticks') or 0) + 1) if streak else 1,
        }
    p = path(product)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return data


def line(product):
    """``STALE since <local HH:MM> — record failed: <reason> (<n> ticks)`` when the last tick's
    own record step failed — checked first, the more severe of the two (F-0260 D4). Otherwise
    ``CHECKOUT <n> behind since <HH:MM> — <why>: <detail> (<n> ticks)`` when the operator's
    record checkout cannot be fast-forwarded — kept outside the ``ok`` gate, because a landed
    record step says nothing about whether its push ever reached the checkout the read views
    read (PD7). ``None`` when neither: the record step landed and the checkout is caught up, or
    no tick has run yet."""
    data = read(product)
    if not data:
        return None
    from asf.views import index_reader as ix
    if not data.get('ok', True):
        since = ix.local_stamp(data.get('since') or data.get('ts'), '%H:%M')
        ticks = data.get('ticks') or 1
        return (f"STALE since {since} — record failed: {data.get('reason') or 'see tick log'} "
                f"({ticks} tick{'s' if ticks != 1 else ''})")
    checkout = data.get('checkout')
    if not checkout:
        return None
    since = ix.local_stamp(checkout.get('since') or data.get('ts'), '%H:%M')
    ticks = checkout.get('ticks') or 1
    why = checkout.get('why')
    if why == 'local-changes':
        detail = f"local changes: {', '.join(checkout.get('paths') or [])}"
    elif why == 'off-trunk':
        detail = f"off the trunk: {checkout.get('detail') or ''}"
    elif why == 'merge-refused':
        detail = f"merge refused: {checkout.get('detail') or ''}"
    else:
        detail = why or ''
    return (f"CHECKOUT {checkout.get('behind')} behind since {since} — {detail} "
            f"({ticks} tick{'s' if ticks != 1 else ''})")
