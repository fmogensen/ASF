"""asf.invariants — the executable invariants (the registry; W4 fills it).

Each :class:`Invariant` has an ``id`` (``I1``…), a ``scope`` and a ``check(ctx) -> [Finding]``,
pure over the facts ``ctx`` carries. They run at three points of the tick, and every one fails
**soft** — an invariant never aborts a tick:

- ``record``: over the staged record tree before ``commit_and_push`` (I2 after ingest's
  restamp). A finding reverts only the offending paths (``git checkout -- <paths>``), the rest
  of the tick commits, and one Bug is filed per ``(invariant, path)``.
- ``feeder``: over ``plan_rows`` before the wave. A violating row is dropped and logged as
  ``INVARIANT <id>: <row>``.
- ``lane``: after harvest. Reported only.

``asf check --invariants`` runs them all read-only against the record and state directory, and
the tests assert the same checks. I6, I12 and I13 are tests, not tick checks; I9 is an event,
not a violation.
"""
from dataclasses import dataclass, field
from typing import Callable

#: The points of the tick an invariant runs at.
SCOPES = ('record', 'feeder', 'lane')


@dataclass(frozen=True)
class Finding:
    """One violation: which invariant, where, and what to revert (``record`` scope) or drop
    (``feeder`` scope, ``subject`` the row)."""
    invariant: str
    scope: str
    subject: str
    message: str
    paths: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Invariant:
    """``check(ctx) -> [Finding]``, pure over ``ctx``."""
    id: str
    scope: str
    check: Callable


#: The registry: I1–I13 as W4 lands them.
INVARIANTS = []


def run(ctx, scope=None):
    """Every registered invariant (only those of ``scope`` when given) over ``ctx``: the
    findings, in registry order; ``[]`` when all hold."""
    if scope is not None and scope not in SCOPES:
        raise ValueError(f'not an invariant scope: {scope!r} (one of {", ".join(SCOPES)})')
    findings = []
    for inv in INVARIANTS:
        if scope is None or inv.scope == scope:
            findings.extend(inv.check(ctx) or ())
    return findings
