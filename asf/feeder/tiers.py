"""asf.feeder.tiers — the S1 lane: a fixed order over rows, and the cut to capacity.

Tier 0 is an open S1 ``BUG → FIX``, tier 1 an S2 one, tier 2 everything else (by Feature rank,
then id — :func:`asf.feeder.rows.candidates` already hands tier 2 over in that order).

The cut: ``capacity`` less the sessions already in flight is the number of launches this tick.
An S1 row is always emitted first — with no free slot it still shows, waiting on one. While any
S1 row is emitted (an S1 Bug no session holds), no tier-2 row is: Features wait until the
incident has a session. A ``WAITS ON`` row launches nothing and costs no slot; it is shown for
the Tasks the cut reached.
"""
from asf.feeder import rows as R

TIER_S1, TIER_S2, TIER_REST = 0, 1, 2
NO_SLOT = 'WAITS ON a free slot'


def tier_of(row):
    return row.tier


def free_slots(inflight, capacity):
    return max(int(capacity) - len(inflight or []), 0)


def select(candidates, inflight, capacity):
    """The emitted rows, in order. ``candidates`` is :func:`asf.feeder.rows.candidates`' list."""
    ordered = sorted(candidates, key=tier_of)  # stable: keeps the Feature order within a tier
    free = free_slots(inflight, capacity)
    s1_waiting = any(r.tier == TIER_S1 and r.launches for r in ordered)
    out = []
    for r in ordered:
        if r.tier == TIER_REST and s1_waiting:
            break
        if not r.launches:
            if free > 0:
                out.append(r)
            continue
        if free > 0:
            free -= 1
            out.append(r)
        elif r.tier == TIER_S1:
            out.append(R.Row(**{**r.__dict__, 'action': NO_SLOT}))
    return out
