"""asf.feeder.tiers — the S1 lane: a fixed order over rows, and the cut to capacity.

Tier 0 is an open S1 ``BUG → FIX``, tier 1 an S2 one, tier 2 everything else (by Feature rank,
then id — :func:`asf.feeder.rows.candidates` already hands tier 2 over in that order).

The cut: ``capacity`` less the sessions already in flight is the number of launches this tick.
An S1 row is always emitted first — with no free slot it still shows, waiting on one. While any
S1 row is emitted (an S1 Bug no session holds), no tier-2 row is: Features wait until the
incident has a session. A ``WAITS ON`` row launches nothing and costs no slot; it is shown for
the Tasks the cut reached, and always in tiers 0 and 1 (an S1/S2 Bug is never silent).

A launching row on an item an approval hold parks (``held``, :func:`asf.approvals.parked` — a
harvest merge hold, or a refused write to the amendable set; any other refusal from the hook
never parks) is
emitted — the wave says it waits for a person — but costs no slot and holds no tier back: it
cannot start whatever the capacity, so a slot given to it is a slot no session gets.
"""
from asf.feeder import rows as R

TIER_S1, TIER_S2, TIER_REST = 0, 1, 2
NO_SLOT = 'WAITS ON a free slot'


def tier_of(row):
    return row.tier


def free_slots(inflight, capacity):
    return max(int(capacity) - len(inflight or []), 0)


def select(candidates, inflight, capacity, held=(), s1_first=True):
    """The emitted rows, in order. ``candidates`` is :func:`asf.feeder.rows.candidates`' list;
    ``held`` the item ids a hold parks (their launching rows take no slot). ``s1_first=False``
    drops the S1 lane's cut of the tier-2 rows: the product's *demand*
    (:func:`asf.tick.step_wave.demand`), not this tick's launch order."""
    held = set(held or ())
    ordered = sorted(candidates, key=tier_of)  # stable: keeps the Feature order within a tier
    free = free_slots(inflight, capacity)
    s1_waiting = s1_first and any(r.tier == TIER_S1 and r.launches and r.item_id not in held for r in ordered)
    out = []
    for r in ordered:
        if r.tier == TIER_REST and s1_waiting:
            break
        if r.launches and r.item_id in held:
            out.append(r)
            continue
        if not r.launches:
            # an S1/S2 row is named even with no slot free: a WAITS row for a held or busy Bug
            # is the one place NEXT says why it gets no session
            if free > 0 or r.tier < TIER_REST:
                out.append(r)
            continue
        if free > 0:
            free -= 1
            out.append(r)
        elif r.tier == TIER_S1:
            out.append(R.Row(**{**r.__dict__, 'action': NO_SLOT}))
    return out
