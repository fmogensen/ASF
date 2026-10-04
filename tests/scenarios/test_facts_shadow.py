"""The landing fact in shadow under the workers' deciders (W6-PR2; the harness is
``tests/scenarios/__init__.py``).

Two tables on the one world:

* :data:`FACT_EDGES` — the fact itself (:func:`asf.facts.landing.landed`) on every named edge of
  a landing (S-M14) and on each host behaviour: ``Landed`` / ``NotLanded`` / ``Unknown``, and the
  rule (``by``) that attributed a landing. The host is read only through the pass's cache.
* :data:`SHADOW_ROWS` — a close path run the way the tick runs it under ``flags.facts: shadow``
  (the pass primed: its one open-PR read): the path's verdict is unchanged (``S.holds`` against
  the close-path table's expectation), and the deciders that disagree with the fact are exactly
  the ones named — none on the known-good rows, the old decider named on the deliberately-wrong
  ones (a ``covers``-only close: the trunk commit only touches the Task's ``writes:``).
"""
import contextlib
import io
import re
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.scenarios…` does not
    import scenarios as S
    from e2e.factory import _git
except ImportError:  # pragma: no cover - import shape only
    from tests import scenarios as S
    from tests.e2e.factory import _git

CB = 'trunkclose.closes_before_launch'
CP = 'trunkclose.close_parked'
RC = 'relaunch.assess → step_wave park/close'

#: ``(behaviour, edit, fact, by)`` — ``by`` is the attributing rule of a ``Landed``.
FACT_EDGES = (
    ('open', None, 'NotLanded', ''),            # only the decoy covers the writes: a hint
    ('close-unmerged', None, 'NotLanded', ''),
    ('merged', None, 'Landed', 'names'),        # the squash names the Task
    ('rate-limit', None, 'NotLanded', ''),      # nothing landed: the host is never asked
    ('open', 'squash', 'Landed', 'names'),
    ('open', 'merge-commit', 'Landed', 'names'),
    ('open', 'rebase-merge', 'Landed', 'names'),
    ('open', 'reworded-patch', 'NotLanded', ''),  # git alone cannot tell: no lane record
    ('open', 'revert', 'NotLanded', ''),
    ('open', 'doc-lane', 'NotLanded', ''),
    ('open', 'archive-branch', 'NotLanded', ''),
    ('open', 'cloud-session', 'Landed', 'pr-merge'),
    ('open', 'cloud-report-only', 'NotLanded', ''),
    ('open', 'batch-ref', 'NotLanded', ''),
    ('open', 'attested-sha', 'Landed', 'pr-merge'),
    ('merged', 'voided-landing', 'NotLanded', ''),
)
#: What the fact says of ``why`` on the rows that name it.
WHY = {('open', 'revert'): 'reverted by', ('merged', 'voided-landing'): 'voided'}

#: ``(path, behaviour, expected, disagreeing deciders)`` — ``expected`` as the close-path table.
SHADOW_ROWS = (
    (CB, 'open', 'none', ()),
    (CB, 'merged', 'decoy', ()),
    (CB, 'close-unmerged', 'decoy', ('trunkclose',)),   # a covers-only close
    (CB, 'rate-limit', 'none', ()),
    (CP, 'open', 'none', ()),
    (CP, 'merged', 'decoy', ()),
    (CP, 'close-unmerged', 'decoy', ('trunkclose',)),
    # the cap's landed half asks no host: it names the decoy while PR #1 is open
    (RC, 'open', 'none', ('relaunch',)),
    (RC, 'merged', 'decoy', ()),
    (RC, 'close-unmerged', 'decoy', ('relaunch', 'trunkclose')),
)


@contextlib.contextmanager
def shadowed(behaviour, edit=None):
    """A fork under ``flags.facts: shadow``, the host behaving, ``edit`` applied, the product
    checkout fetched and the pass primed — what the tick does before its deciders run."""
    from asf.facts import cache, landing as facts_landing
    f = S.WORLD.fork()
    S.set_flag(f, 'facts', 'shadow')
    S.BEHAVIOURS[behaviour](f)
    with S.deciding(f) as product, contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        if edit:
            S.EDITS[edit](S.WORLD, f)
        _git(['fetch', '-q', 'origin'], cwd=product.repo_dir)
        cache.clear()
        facts_landing.prime(product)
        try:
            yield f, product
        finally:
            cache.clear()


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')


class FactEdges(unittest.TestCase):
    """One generated test per row of :data:`FACT_EDGES`."""

    def check(self, behaviour, edit, fact, by):
        from asf.facts import landing as facts_landing
        with shadowed(behaviour, edit) as (f, product):
            got = facts_landing.landed(product, S.ITEM)
        self.assertEqual(type(got).__name__, fact, got)
        if by:
            self.assertEqual(got.by, by, got)
        why = WHY.get((behaviour, edit))
        if why:
            self.assertIn(why, got.why, got)
        if behaviour == 'open' and edit is None:
            self.assertEqual(got.hint_sha, S.WORLD.decoy)  # covers is a hint, never a landing


def _fact_test(row):
    def test(self):
        self.check(*row)
    behaviour, edit, fact, _by = row
    test.__doc__ = f'{behaviour}{" + " + edit if edit else ""} → {fact}'
    return test


for _row in FACT_EDGES:
    setattr(FactEdges, f'test_{_slug(_row[0])}__{_slug(_row[1] or "none")}', _fact_test(_row))


class ShadowRows(unittest.TestCase):
    """One generated test per row of :data:`SHADOW_ROWS`."""

    def check(self, path, behaviour, expected, deciders):
        from asf.facts import disagree, landing as facts_landing
        lines = []
        with shadowed(behaviour) as (f, product):
            self.assertEqual(product.flag('facts'), 'shadow')
            verdict, waited = S.PATHS[path](S.WORLD, f, product, lines.append)
            log = [r for r in disagree.records(product) if r.get('fact') == facts_landing.FACT]
        o = S.Outcome(waited=waited, lines=lines, stamp=S.stamp(f))
        if verdict:
            o.closed, o.how = True, f'{path} ({verdict})'
        if not o.closed and o.stamp:
            o.closed, o.how = True, f'a landing stamp ({o.stamp[:9]})'
        said = f'{path} × {behaviour}: {o}\n' + '\n'.join(lines) + f'\nlog: {log}'
        self.assertTrue(S.holds(expected, o), f'the shadow changed the verdict\n{said}')
        got = sorted({r.get("decider") for r in log})
        self.assertEqual(got, sorted(deciders), said)
        self.assertFalse([r for r in log if str(r.get('new')).startswith('error:')], said)


def _shadow_test(row):
    def test(self):
        self.check(*row)
    path, behaviour, expected, deciders = row
    test.__doc__ = f'{path} × {behaviour} under shadow: {expected}, disagree {deciders or "none"}'
    return test


for _row in SHADOW_ROWS:
    setattr(ShadowRows, f'test_{_slug(_row[0])}__{_slug(_row[1])}', _shadow_test(_row))


if __name__ == '__main__':
    unittest.main()
