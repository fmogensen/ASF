"""I14 in ``refuse`` mode across the close paths (what W4-PR4's flip relies on; the harness is
``tests/scenarios/__init__.py``).

Every close a path decides reaches the Task's card one way: the record's ingest write, through
:func:`asf.record.stage.guarded` — the trunk check, the parked sweep and the relaunch cap write a
landing stamp on the run line, the ingest closes the card on it (or on the host's merged PR),
and the record invariants put back what they refuse. The approvals sweep, the groom's ``close_*``
answers, the release note and the merge facts read that card; they write none of their own here.
So a row runs its path under ``flags.i14: refuse`` (:func:`scenarios.run_written`), then that
write, and reads the card **on disk**.

I14 (W4-PR3b) accepts a close whose landing is sound and puts back the rest: a ``covers``
attribution on a commit older than the card (S-M16), a landing an ``asf reset`` voided (W4-PR5),
a landing the trunk reverted (S-M15). :data:`REFUSE_ROWS` is ``(path, behaviour, expected, gap,
edit)``, read as in ``test_close_paths.py``; W4-PR3b closed every gap this table named.
"""
import re
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.scenarios…` does not
    import scenarios as S
except ImportError:  # pragma: no cover - import shape only
    from tests import scenarios as S

CB = 'trunkclose.closes_before_launch'
CP = 'trunkclose.close_parked'
RC = 'relaunch.assess → step_wave park/close'
IN = 'ingest.derive via merge_facts'

#: ``(path, behaviour, expected, gap, edit)``
REFUSE_ROWS = (
    # a covers close stands: REPORT done, no PR of the Task's on the host (one open or closed
    # unmerged is its own work: no covers close at all), the covering commit newer than the card
    # ...
    (CB, 'open', 'decoy', None, 'no-pr'),
    (CP, 'open', 'decoy', None, 'no-pr'),
    (RC, 'open', 'decoy', None, 'no-pr'),
    # ... and is put back when the card is newer than the commit said to cover it
    (CB, 'open', 'none', None, 'no-pr-card-after-cover'),
    (CP, 'open', 'none', None, 'no-pr-card-after-cover'),
    (RC, 'open', 'none', None, 'no-pr-card-after-cover'),
    # the record's own close on the host's merged PR stands; on a voided or a reverted landing
    # it is put back
    (IN, 'merged', 'closes', None, None),
    (IN, 'open', 'closes', None, 'attested-sha'),
    (IN, 'merged', 'none', None, 'voided-landing'),  # W4-PR5: the ingest never closes it
    (IN, 'open', 'none', None, 'revert'),
    # nothing to refuse: open work, and a host that answers nothing
    (IN, 'open', 'none', None, None),
    (IN, 'rate-limit', 'none', None, None),
)


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')


class RefuseRows(unittest.TestCase):
    """One generated test per row of :data:`REFUSE_ROWS`."""

    def check(self, path, behaviour, expected, gap, edit):
        _scenario, o = S.run_written(path, behaviour, edit, i14='refuse')
        said = (f'{path} × {behaviour}{" + " + edit if edit else ""} (i14: refuse): {o}\n'
                + '\n'.join(o.lines))
        holds = S.holds(expected, o)
        if gap:
            self.assertFalse(holds, f'{gap} has landed — this row now holds: drop its gap marker.'
                                    f'\n{said}')
            return
        self.assertTrue(holds, f'expected {expected}\n{said}')


def _make(row):
    def test(self):
        self.check(*row)
    path, behaviour, expected, gap, edit = row
    test.__doc__ = (f'i14: refuse — {path} × {behaviour}{" + " + edit if edit else ""} → '
                    f'{expected}' + (f' (still fails: {gap})' if gap else ''))
    return test


for _row in REFUSE_ROWS:
    _name = f'test_{_slug(_row[0])}__{_slug(_row[1])}' + (f'__{_slug(_row[4])}' if _row[4] else '')
    assert not hasattr(RefuseRows, _name), _name
    setattr(RefuseRows, _name, _make(_row))


class Table(unittest.TestCase):

    def test_every_stamping_path_has_a_standing_and_a_refused_row(self):
        for path in (CB, CP, RC, IN):
            got = {r[2] != 'none' for r in REFUSE_ROWS if r[0] == path}
            self.assertEqual(got, {True, False}, path)

    def test_the_rate_limit_row_closes_nothing(self):
        self.assertEqual([r for r in REFUSE_ROWS if r[1] == 'rate-limit' and r[2] != 'none'], [])

    def test_the_gaps_are_named_plan_items(self):
        for row in REFUSE_ROWS:
            self.assertIn(row[3], (None, *S.GAPS), row)


if __name__ == '__main__':
    unittest.main()
