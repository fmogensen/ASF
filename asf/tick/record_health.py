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


def record(product, ok, reason=None, now=None):
    """Write the record step's outcome. A success clears any streak; a failure extends the
    previous stamp's streak (``since``, ``ticks``) when it was already failing, else starts a new
    one at this tick."""
    now = now or _stamp()
    if ok:
        data = {'ok': True, 'ts': now}
    else:
        prev = read(product) or {}
        streak = not prev.get('ok', True)
        data = {
            'ok': False,
            'ts': now,
            'since': (prev.get('since') if streak else None) or now,
            'ticks': ((prev.get('ticks') or 0) + 1) if streak else 1,
            'reason': reason or 'see tick log',
        }
    p = path(product)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return data


def line(product):
    """``STALE since <local HH:MM> — record failed: <reason> (<n> ticks)``, or ``None`` when the
    last tick's record step landed (or none has run yet)."""
    data = read(product)
    if not data or data.get('ok', True):
        return None
    from asf.views import index_reader as ix
    since = ix.local_stamp(data.get('since') or data.get('ts'), '%H:%M')
    ticks = data.get('ticks') or 1
    return (f"STALE since {since} — record failed: {data.get('reason') or 'see tick log'} "
            f"({ticks} tick{'s' if ticks != 1 else ''})")
