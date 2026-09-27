"""asf.budget — what an item may spend, and what one run may take.

Two verdicts and the text each prints: an item's budget (ended sessions, dollars) against the
consumption its card already carries, and one run's caps (wall clock, turns) against what the
ledger and the log say. Pure: no clock it is not handed, no io, no git. A leaf — the stdlib and
:mod:`asf.conventions` only — so the feeder, the metrics writer, the health step and the board
view may all import it.
"""
from dataclasses import dataclass

from asf import conventions

SESSIONS, USD, MINUTES, TURNS = 'sessions', 'usd', 'run_minutes', 'run_turns'
OFF = 'off'                     #: a measure a product turned off, as `asf.tokens` spells it
RUN_CAP = 'run cap'             #: the `end_reason` a capped run is recorded with
OVER = 'OVER BUDGET'            #: the first word of the line the wave prints


@dataclass(frozen=True)
class Budget:
    """What one item may spend. ``None`` is no limit. ``source`` is ``default``, ``product`` or
    ``item`` — whichever set the measure that is lowest, for the groom line to name."""
    sessions: int = None
    usd: float = None
    source: str = 'default'


@dataclass(frozen=True)
class Spent:
    """What the card says it has taken, and the verdict. ``measure`` is the first measure over
    its budget (``sessions`` before ``usd``), or ``''`` when none is."""
    sessions: int = 0
    usd: float = None
    budget: Budget = None
    measure: str = ''

    @property
    def over(self):
        return bool(self.measure)


def _off(value):
    return None if value == OFF else value


def _money(value):
    value = float(value)
    return str(int(value)) if value == int(value) else f'{value:.2f}'


def of(conv, item):
    """The :class:`Budget` for one item (an ``index.json`` entry or a card's meta): the product's
    ``conventions.budget`` under the card's own ``budget_sessions:`` / ``budget_usd:``.

    An Epic has no budget (``Budget()``): it launches no row, and its ``budget_usd`` is the
    subtree figure the rollup reports (F-0101), not a stop.
    """
    item = item or {}
    if item.get('type') == 'epic':
        return Budget()
    sessions = _off(conventions.budget_for(conv, SESSIONS))
    usd = _off(conventions.budget_for(conv, USD))
    source = ('default' if (sessions, usd) == (_off(conventions.DEFAULT_BUDGET[SESSIONS]),
                                                _off(conventions.DEFAULT_BUDGET[USD]))
              else 'product')
    item_sessions, item_usd = item.get('budget_sessions'), item.get('budget_usd')
    if item_sessions is not None:
        sessions = item_sessions
    if item_usd is not None:
        usd = item_usd
    if item_sessions is not None or item_usd is not None:
        source = 'item'
    # a delivery lead (``delivers:``) spends for every member it builds: its budget is the
    # per-item one times the members, unless the card sets its own
    members = len(item.get('delivers') or ()) if isinstance(item.get('delivers'), list) else 0
    if members > 1 and source != 'item':
        sessions = None if sessions is None else sessions * members
        usd = None if usd is None else usd * members
    return Budget(sessions=sessions, usd=usd, source=source)


def spent(conv, item):
    """:class:`Spent` for one item: ``cost.sessions`` and ``cost.usd`` against :func:`of`.

    A card with no ``cost:`` block has spent nothing. A measure whose budget is ``None``, or
    whose consumption the record has not measured (``cost.usd`` is ``None`` when no log carried a
    cost), is never over — an unmeasured dollar is not a spent one.
    """
    item = item or {}
    budget = of(conv, item)
    cost = item.get('cost') or {}
    sessions = cost.get('sessions') or 0
    usd = cost.get('usd')
    measure = ''
    if budget.sessions is not None and sessions >= budget.sessions:
        measure = SESSIONS
    elif budget.usd is not None and usd is not None and usd >= budget.usd:
        measure = USD
    return Spent(sessions=sessions, usd=usd, budget=budget, measure=measure)


def line(item_id, s):
    """The wave's line for an over-budget item, both measures always::

        OVER BUDGET T-0021 — 9/3 sessions, $13.53/$10

    A measure that is off prints ``—`` in place of its cap."""
    sessions_cap = '—' if s.budget.sessions is None else str(s.budget.sessions)
    usd_cap = '—' if s.budget.usd is None else _money(s.budget.usd)
    usd_spent = '—' if s.usd is None else _money(s.usd)
    return (f'{OVER} {item_id} — {s.sessions}/{sessions_cap} sessions, '
            f'${usd_spent}/${usd_cap}')


def run_caps(conv):
    """``(minutes, turns)`` — each an int, or None when the product turned it off."""
    return _off(conventions.budget_for(conv, MINUTES)), _off(conventions.budget_for(conv, TURNS))


def run_over(elapsed_min, turns, caps):
    """``(measure, value, limit)`` for the first run cap exceeded — the wall clock before the
    turns — else None. A None on either side is never over, so a run whose ``started`` the
    ledger lost is judged on its turns alone."""
    minutes_cap, turns_cap = caps
    if (isinstance(elapsed_min, (int, float)) and isinstance(minutes_cap, (int, float))
            and elapsed_min > minutes_cap):
        return MINUTES, elapsed_min, minutes_cap
    if (isinstance(turns, (int, float)) and isinstance(turns_cap, (int, float))
            and turns > turns_cap):
        return TURNS, turns, turns_cap
    return None


def run_cap_text(kind, measure, value, limit):
    return f'run cap: {measure} {value} over the {limit} cap for a {kind} job'


def run_cap_result(kind, measure, value, limit, at):
    """The result record the factory appends to a run it ended on a cap — the twin of
    :func:`asf.tokens.cap_result`, and the same judgement: the structured ``asf.run_cap`` object
    is what a reader believes, the text says the same for a human. No ``duration_ms`` and no
    ``total_cost_usd``: nothing was measured."""
    return {'type': 'result', 'subtype': 'error', 'is_error': True,
            'result': run_cap_text(kind, measure, value, limit) + ' — stopped by the factory',
            'asf': {'run_cap': {'kind': kind, 'measure': measure, 'value': value,
                                'limit': limit, 'at': at}}}
