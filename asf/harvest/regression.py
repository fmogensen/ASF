"""asf.harvest.regression — the test that would have caught it, run on the tree that had the
defect.

The predicate: four pure functions, no git and no subprocess, no product import beyond
:mod:`asf.feeder.widen` and the ``conv`` passed in. ``gated`` decides which fix branches the
regression gate covers and says why one is skipped — a silent skip is how the gap went unnoticed.
``verdict`` is the one place the four states (``proved``, ``not-red``, ``inconclusive``,
``red-both``) are decided, so the hold's text and the tick line can never disagree about the
same branch.
"""
import re

from asf.feeder import widen

#: the `found_in` values the gate does not cover by default (`flags.regression.exempt_found_in`).
DEFAULT_EXEMPT_FOUND_IN = ('ci',)

#: a failing case's line in a test runner's log: `unittest`'s ``FAIL:``/``ERROR:``, node:test
#: ``✖``, vitest ``✗``/``×``, TAP ``not ok N -``. A separate parse from `lane._FAIL_RE` on
#: purpose (D8): that one feeds what the factory calls a red test on the trunk.
_CASE_RE = re.compile(
    r'^\s*(?:(?:FAIL|ERROR):\s*(?P<a>\S.*)'
    r'|[✖✗×]\s+(?P<b>.+)'
    r'|not ok\s+\d+\s*-?\s*(?P<c>.+))$'
)
_TRAILING_RE = re.compile(r'\s*\([^()]*\)\s*$')


def _case_name(raw):
    """``raw``, its trailing parenthesised dotted path and any duration stripped."""
    name = raw.strip()
    while True:
        stripped = _TRAILING_RE.sub('', name).strip()
        if stripped == name:
            return name
        name = stripped


def gated(conv, items, item, branch, files):
    """``(True, '')`` when this branch is one the regression gate covers, else ``(False, why)``:
    a `fix` lane branch (`conv.branch_kind(branch) == 'fix'`) whose card's `found_in` is not in
    `exempt_found_in`, carrying both a test file and a non-test change (D1, D11)."""
    kind = conv.branch_kind(branch)
    if kind != 'fix':
        return False, f'not a fix branch: {kind or "none"}'
    card = (items or {}).get(item or '') or {}
    found_in = card.get('found_in')
    exempt = conv.flag('regression.exempt_found_in', DEFAULT_EXEMPT_FOUND_IN)
    if not isinstance(exempt, (list, tuple)):
        exempt = DEFAULT_EXEMPT_FOUND_IN
    if found_in in exempt:
        return False, f'found_in: {found_in}'
    tests = test_files(files)
    if not tests:
        return False, 'no test file in the diff: no change to be red against'
    if tests == list(files or []):
        return False, 'the whole diff is test files: no change to be red against'
    return True, ''


def test_files(files):
    """The branch's test files, in order — `widen.is_test_path` of each (D3, P5)."""
    return [f for f in (files or []) if widen.is_test_path(f)]


def failing_cases(log, pattern=None):
    """The test case names a run's output reports as not passing, in order, each once:
    `unittest`'s ``FAIL: <case>`` and ``ERROR: <case>``, node:test ``✖``, vitest ``✗``/``×``,
    TAP ``not ok N -`` — or `pattern`'s first group when the product names one (D8). The name is
    the case's own, trailing parenthesised dotted path and duration stripped."""
    out = []
    rx = None
    if pattern:
        rx = pattern if hasattr(pattern, 'search') else re.compile(pattern)
    for raw in (log or '').splitlines():
        if rx is not None:
            m = rx.search(raw)
            name = _case_name(m.group(1)) if m and m.group(1) else None
        else:
            m = _CASE_RE.match(raw)
            name = _case_name(next(g for g in m.group('a', 'b', 'c') if g)) if m else None
        if name and name not in out:
            out.append(name)
    return out


def verdict(before, after, pattern=None):
    """``(state, cases, line)`` — `'proved'` with the case names red before and green after
    (D5); `'not-red'` when the pre-fix run names no failing case and exited 0; `'inconclusive'`
    when it exited non-zero naming none (D4); `'red-both'` when every case red before is red
    after. One place decides, so the hold's text and the tick line cannot disagree."""
    before_rc, before_out = before
    _after_rc, after_out = after
    before_cases = failing_cases(before_out, pattern)
    if not before_cases:
        if before_rc == 0:
            return 'not-red', [], (
                "the branch's tests pass on the pre-fix tree: nothing on it would have caught "
                "this defect")
        return 'inconclusive', [], (
            'the pre-fix run exited non-zero naming no failing case: the grafted test did not '
            'run as a test on the pre-fix tree')
    after_cases = failing_cases(after_out, pattern)
    proved = [c for c in before_cases if c not in after_cases]
    if proved:
        return 'proved', proved, (
            f'{", ".join(proved)} red before the fix and absent after: proved')
    return 'red-both', before_cases, (
        f'{", ".join(before_cases)} red on both sides: not a regression test for this fix')
