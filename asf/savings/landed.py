"""asf.savings.landed — the join: minutes and dollars per landed change, per kind (F-0100 §2.3).

Pure functions over three event lists (``landings``, ``sessions``, ``gates``). No I/O, no product,
no record. A cell with nothing in it is ``None`` — its callers print ``—``, never ``0``.
"""
import dataclasses
import statistics


@dataclasses.dataclass(frozen=True)
class Landed:
    job: str
    branch: str
    kind: str
    item: str | None
    sha: str
    ts: str
    sessions: int
    rounds: int
    minutes: float
    usd: float | None
    in_tokens: int | None
    gate_minutes: float
    gates: int


def _sum(values):
    """The sum of the non-``None`` values, or ``None`` when there are none."""
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _lane_sessions(landing, sessions):
    ts = landing.get('ts') or ''
    branch = landing.get('branch')
    if branch:
        return [s for s in sessions if s.get('branch') == branch and (s.get('ts') or '') <= ts]
    return [s for s in sessions if s.get('item') == landing.get('item')
            and s.get('kind') == landing.get('kind') and (s.get('ts') or '') <= ts]


def _lane_gates(landing, gates):
    branch = landing.get('branch')
    if not branch:
        return []
    ts = landing.get('ts') or ''
    return [g for g in gates if branch in (g.get('branches') or []) and (g.get('ts') or '') <= ts]


def landed_changes(landings, sessions, gates):
    """One ``Landed`` per landing event, joined to its lane's sessions and gate runs."""
    out = []
    for lg in landings:
        own = _lane_sessions(lg, sessions)
        lane_gates = _lane_gates(lg, gates)
        out.append(Landed(
            job=lg.get('job'), branch=lg.get('branch'), kind=lg.get('kind'), item=lg.get('item'),
            sha=lg.get('sha'), ts=lg.get('ts'),
            sessions=len(own), rounds=max([s.get('round') or 1 for s in own], default=1),
            minutes=_sum([s.get('minutes') for s in own]) or 0.0,
            usd=_sum([s.get('usd') for s in own]),
            in_tokens=_sum([s.get('in_tokens') for s in own]),
            gate_minutes=sum((g.get('seconds') or 0) / 60 / len(g['branches']) for g in lane_gates),
            gates=len(lane_gates)))
    return out


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def _cell(rows):
    return {'landings': len(rows),
            'minutes': _median([r.minutes for r in rows]),
            'usd': _median([r.usd for r in rows]),
            'gate_minutes': _median([r.gate_minutes for r in rows]),
            'rounds': _mean([r.rounds for r in rows]),
            'in_tokens': _median([r.in_tokens for r in rows])}


def per_kind(rows):
    """{kind: cell} for every kind present (sorted), plus ``all``. Medians, and ``rounds`` a mean."""
    out = {kind: _cell([r for r in rows if r.kind == kind])
           for kind in sorted({r.kind for r in rows}, key=str)}
    out['all'] = _cell(list(rows))
    return out
