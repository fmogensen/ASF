"""asf.feeder.tiers — the S1 lane: a fixed order over rows, and the cut to capacity.

Tier 0 is an open S1 ``BUG → FIX``, tier 1 an S2 one, tier 2 everything else (by Feature rank,
then id — :func:`asf.feeder.rows.candidates` already hands tier 2 over in that order).

The cut: ``capacity`` less the sessions already in flight is the number of launches this tick.
An S1 row is always emitted first — with no free slot it still shows, waiting on one. An S1 row
takes the first seat, ahead of every other row; tier-2 rows are cut only while an S1 row that
needs a session cannot get a seat — otherwise the seats left go to tier 2 in feeder order.
(2026-09-26: the S1 lane reserves the first seat, not the whole floor — two product waves
launched 1 row with 7 seats free while the lane held every tier-2 row behind a seated S1.) A ``WAITS ON`` row launches nothing and costs no slot; it is shown for
the Tasks the cut reached, and always in tiers 0 and 1 (an S1/S2 Bug is never silent).

A launching row on an item an approval hold parks (``held``, :func:`asf.approvals.parked` — a
harvest merge hold, or a refused write to the amendable set; any other refusal from the hook
never parks) is
emitted — the wave says it waits for a person — but costs no slot and holds no tier back: it
cannot start whatever the capacity, so a slot given to it is a slot no session gets.
"""
import collections

from asf.feeder import rows as R

TIER_S1, TIER_S2, TIER_REST = 0, 1, 2
NO_SLOT = 'WAITS ON a free slot'

#: The remedy for an S1 nothing is working: the downgrade, with the reason `asf set` requires for a
#: move off S1 (F-0163) left as a placeholder — the gate's own `reason` describes the queue, not the
#: Bug, and a machine-written defence is not one (D8).
DOWNGRADE = 'asf set {item_id} severity=S2 --why "<why this is not S1>" --product {name}'

#: a tier-0 WAITS row whose ``waits_on`` is one of these is being worked — a live session, a
#: branch, a landing, a merge. Every other reason is a person's (F-0113, D5).
WORKED = ('session', 'landing', 'branch', 'merge')

Gate = collections.namedtuple('Gate', 'holders held behind by_kind unworked')
#: holders  [item id]   the S1 rows that took the floor: tier 0, launching, unparked, no seat
#: held     int         tier-2 launching rows the cut dropped
#: behind   int         tier-2 launching rows behind the S1 lane at all (held + the seated ones)
#: by_kind  [(kind, n)] the held rows by row kind, in feeder order
#: unworked [(id, waits_on, reason)]  open S1s no row launches and nothing is working (D5)


def tier_of(row):
    return row.tier


def free_slots(inflight, capacity):
    return max(int(capacity) - len(inflight or []), 0)


def select(candidates, inflight, capacity, held=(), s1_first=True, keep=()):
    """The emitted rows, in order. ``candidates`` is :func:`asf.feeder.rows.candidates`' list;
    ``held`` the item ids a hold parks (their launching rows take no slot). ``s1_first=False``
    drops the S1 lane's cut of the tier-2 rows: the product's *demand*
    (:func:`asf.tick.step_wave.demand`), not this tick's launch order. ``keep``: the item ids
    whose rows are always emitted (:func:`asf.feeder.rows.pushed_ids` — pushed work is never
    silent): a launching one past the seats as ``WAITS ON a free slot``, a WAITS one as it is."""
    held, keep = set(held or ()), set(keep or ())
    ordered = sorted(candidates, key=tier_of)  # stable: keeps the Feature order within a tier
    free = free_slots(inflight, capacity)
    s1_rows = sum(1 for r in ordered if r.tier == TIER_S1 and r.launches and r.item_id not in held)
    s1_waiting = s1_first and s1_rows > free   # an S1 row that needs a session gets no seat
    out = []
    for r in ordered:
        if r.tier == TIER_REST and s1_waiting:
            if r.item_id in keep:
                out.append(r if not r.launches or r.item_id in held else _no_slot(r))
            continue
        if r.launches and r.item_id in held:
            out.append(r)
            continue
        if not r.launches:
            # an S1/S2 row is named even with no slot free: a WAITS row for a held or busy Bug
            # is the one place NEXT says why it gets no session
            if free > 0 or r.tier < TIER_REST or r.item_id in keep:
                out.append(r)
            continue
        if free > 0:
            free -= 1
            out.append(r)
        elif r.tier == TIER_S1 or r.item_id in keep:
            out.append(_no_slot(r))
    return out


def _no_slot(row):
    return R.Row(**{**row.__dict__, 'action': NO_SLOT})


def gate(cut, uncut, held=()):
    """What the S1 lane did to this plan, or ``None`` when it did nothing worth a line.

    ``cut`` is the plan as :func:`select` cut it, ``uncut`` the same plan made with
    ``s1_first=False`` (:func:`asf.feeder.rows.plan_rows`) — their difference in tier-2
    launching rows is what the lane dropped. ``held``: the item ids an approval hold parks.
    ``None`` when no S1 took the floor and no open S1 is going unworked."""
    held = set(held or ())
    holders = list(dict.fromkeys(r.item_id for r in cut
                                  if r.tier == TIER_S1 and r.action == NO_SLOT
                                  and r.item_id not in held))
    behind_rows = [r for r in uncut if r.tier == TIER_REST and r.launches and r.item_id not in held]
    cut_keys = {(r.item_id, r.kind) for r in cut if r.action != NO_SLOT}
    dropped = [r for r in behind_rows if (r.item_id, r.kind) not in cut_keys]
    order, counts = [], {}
    for r in dropped:
        if r.kind not in counts:
            order.append(r.kind)
        counts[r.kind] = counts.get(r.kind, 0) + 1
    by_kind = [(k, counts[k]) for k in order]
    unworked = []
    for r in uncut:
        if r.tier != TIER_S1:
            continue
        if r.launches:
            if r.item_id in held:
                unworked.append((r.item_id, 'held', r.reason))
        elif r.waits_on not in WORKED:
            unworked.append((r.item_id, r.waits_on, r.reason))
    if not holders and not unworked:
        return None
    return Gate(holders=holders, held=len(dropped), behind=len(behind_rows),
                by_kind=by_kind, unworked=unworked)


def gate_line(g):
    """The one line every view prints, or ``None``::

        S1 gate: B-0057 holds 94 rows (PLAN → CODE 47, CARD → SPEC 27, STARVED → PLAN 12, STARVED → SPEC 8)

    Two holders read ``B-0057, B-0061 hold 94 rows``. ``None`` when ``g`` is ``None`` or
    ``g.held`` is 0 — a gate with nothing behind it is not news."""
    if g is None or not g.held:
        return None
    verb = 'holds' if len(g.holders) == 1 else 'hold'
    kinds = ', '.join(f'{k} {n}' for k, n in g.by_kind)
    return f"S1 gate: {', '.join(g.holders)} {verb} {g.held} rows ({kinds})"


def _remedy(item_id, waits_on, reason, product):
    """(reason as printed, command) for one :data:`Gate.unworked` entry — the spec's remedy
    table, switched on ``waits_on``. The row's own ``reason`` already carries the age or the
    attempt count; never re-derived off the item, because this function is handed none."""
    name = product.name if hasattr(product, 'name') else product
    if waits_on == 'decision':
        return reason.split(' — ', 1)[0], f'asf set {item_id} decided=true --product {name}'
    if waits_on == 'operator':
        text = reason.split(': ', 1)[-1].split(' (limit', 1)[0]
        return text, DOWNGRADE.format(item_id=item_id, name=name)
    if waits_on == 'held':
        from asf import approvals
        cls = approvals.parked(product).get(item_id, ('hold', None))[0]
        return f'held {cls}', f'asf approvals resolve {item_id}/{cls} granted|done|dropped'
    tail = reason.split(': ', 1)[-1]
    if tail.startswith('blocked by '):
        return tail, f'asf set {item_id} blockedBy= --product {name}'
    return reason, DOWNGRADE.format(item_id=item_id, name=name)


def needs_operator(g, product, skip=()):
    """One line per S1 nothing is working, or ``[]``. ``skip``: ids another line already named
    this tick (D8)::

        NEEDS OPERATOR: B-0057 is S1 and nothing is working it — blocked by B-0041 — the S1
        lane holds 0 of 94 tier-2 rows behind it — asf set B-0057 blockedBy= --product asf
    """
    if g is None:
        return []
    skip = set(skip or ())
    out = []
    for item_id, waits_on, reason in g.unworked:
        if item_id in skip:
            continue
        text, cmd = _remedy(item_id, waits_on, reason, product)
        out.append(f'NEEDS OPERATOR: {item_id} is S1 and nothing is working it — {text} — '
                   f'the S1 lane holds {g.held} of {g.behind} tier-2 rows behind it — {cmd}')
    return out
