"""asf.feeder.idle — why a free slot is free, as an accounting no card falls out of.

The wave's old answer was ``wave: nothing to launch``, and for 73 undecided cards that sentence
was true and useless (F-0096). This module takes the roots the feeder starts work from — every
open Feature, every open S1/S2 Bug (D4) — and puts each in exactly one bucket. The last bucket
is ``other``: a root that matched no rule is *named*, not dropped, so a gate added later is
visible the tick it starts hiding cards (D3).

It imports the feeder's rows and tiers and nothing of the tick (D2), so ``asf next``, ``asf
status`` and the wave say the same thing.
"""
import dataclasses

from asf.feeder import rows as R
from asf.feeder import tiers
from asf.views import index_reader as ix

#: In classification order — the first that matches wins, and the order is the operator's:
#: what is happening, what a slot would start, what a person must do, what waits on itself.
BUCKETS = ('launching', 'in flight', 'no slot', 'held', 'blocked', 'undecided', 'waiting',
           'landed', 'other')
LANDED_STAGES = ('landed', 'on-prod')
MORE = '(asf next --all)'
UNKNOWN = '(no row and no reason: a gate this accounting does not know)'


@dataclasses.dataclass
class Idle:
    slots: int                    #: free session slots once the launches are counted
    launched: int
    buckets: dict                 #: {bucket: [(item id, why)]}, BUCKETS order, empty ones kept
    undecided: list               #: [(id, rank, age)] every undecided root, ranked — not cut
    shown: int                    #: how many of them got a row (conventions.decision_rows)

    @property
    def dry(self):
        """A tick with a free slot and nothing launched — the card's own failure."""
        return self.slots > 0 and self.launched == 0


def roots(items):
    """D4: every open Feature and every open S1/S2 Bug, in rank then id order."""
    out = [v for v in items.values()
           if R.is_open(v) and (v['type'] == 'feature'
                                or (v['type'] == 'bug' and v.get('severity') in ('S1', 'S2')))]
    return sorted(out, key=lambda v: (ix.rank(v), v['id']))


def _in_flight(items, root, inflight, busy):
    for v in ix.subtree(items, root):
        s = R.session_of(inflight, v['id'])
        if s:
            return f"{s.get('job') or v['id']} {s.get('age') or ''}".strip()
        if v['id'] in busy:
            return 'awaiting harvest'
    return None


def _waits(row):
    return row.action if row.action.startswith('WAITS ON') else f'WAITS ON {row.waits_on or row.action}'


def _classify(items, root, mine, selected, inflight, busy, capacity, held):
    """(bucket, why) — the first rule of the table that holds."""
    for r in mine:
        if r in selected and r.launches:
            return 'launching', f'{r.item_id} {r.kind}'
    why = _in_flight(items, root, inflight, busy)
    if why:
        return 'in flight', why
    if any(r.launches for r in mine):
        return 'no slot', f'would launch: capacity {capacity}'
    if root['id'] in held:
        cls, level = held[root['id']]
        return 'held', f'held {cls} ({level})'
    if root.get('blocked'):
        return 'blocked', 'blocked by ' + (', '.join(root.get('blocked_by_open') or []) or 'an open item')
    if root.get('decided') is not True:
        rank = ix.rank(root)
        return 'undecided', (f"undecided {ix.age(root.get('stage_since'))}"
                             + (f', rank {rank}' if rank != ix.BIG else ''))
    if mine:
        return 'waiting', _waits(mine[0])
    if root.get('stage') in LANDED_STAGES or not R.is_open(root):
        return 'landed', 'landed'
    return 'other', 'no row, no reason'


def account(items, product, candidates, selected, inflight, busy, capacity, held=None):
    """The ``Idle`` for this tick. ``candidates`` is every row the index supports,
    ``selected`` the rows after the capacity cut, so a root whose row launches but was cut is
    ``no slot`` rather than a mystery."""
    held = held or {}
    busy_ids = R.inflight_ids(inflight) | set(busy or ())
    buckets = {b: [] for b in BUCKETS}
    for root in roots(items):
        mine = [r for r in candidates if r.item_id == root['id'] or r.feature_id == root['id']]
        bucket, why = _classify(items, root, mine, selected, inflight, busy_ids, capacity, held)
        buckets[bucket].append((root['id'], why))
    launched = sum(1 for r in selected if r.launches)
    slots = max(tiers.free_slots(inflight, capacity) - launched, 0)
    ids = {i for i, _why in buckets['undecided']}
    undecided = [(r.item_id, ix.rank(items[r.item_id]), ix.age(items[r.item_id].get('stage_since')))
                 for r in R.undecided_rows(items, product, busy_ids, limit=0) if r.item_id in ids]
    cap = R.decision_rows(product)
    return Idle(slots=slots, launched=launched, buckets=buckets, undecided=undecided,
                shown=min(len(undecided), cap) if cap else len(undecided))


def _named(entry):
    i, rank, age = entry
    return f"{i} ({f'rank {rank}, ' if rank != ix.BIG else ''}undecided {age})"


def lines(idle, product=None, top=None):
    """``wave:`` lines — the counts, the ranked decisions, the residue."""
    counts = [f'{len(v)} {b}' for b, v in idle.buckets.items() if v]
    out = [f'wave: {idle.launched} launched, {idle.slots} slots free'
           + (' — ' + ', '.join(counts) if counts else '')]
    if idle.undecided:
        n = top or R.decision_rows(product)
        named = idle.undecided[:n] if n else idle.undecided
        line = 'wave: decide next — ' + ' · '.join(_named(e) for e in named)
        if len(idle.undecided) > len(named):
            line += f' — +{len(idle.undecided) - len(named)} more {MORE}'
        out.append(line)
    if idle.buckets['other']:
        out.append('wave: unaccounted — ' + ' '.join(i for i, _why in idle.buckets['other']) + f' {UNKNOWN}')
    return out


def needs_operator(idle, product, groom_file):
    """The one sentence a person must act on, or None: what is waiting, and the two ways to
    end it — the groom line the next tick applies, or ``asf set`` on the top card."""
    if not idle.undecided:
        return None
    name = getattr(product, 'name', product)
    return (f'{len(idle.undecided)} cards await a decision and {idle.slots} slots are idle — '
            f'answer their lines in {groom_file} (the next tick applies them), or decide the top one: '
            f'`asf set {idle.undecided[0][0]} decided=true --product {name}`')
