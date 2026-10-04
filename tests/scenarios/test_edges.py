"""The named edges of a landing, one row each (S-M14; the harness is ``tests/scenarios/__init__.py``).

The cutover of the record's readers onto one landing fact is judged on the rare shapes a landing
takes — the ones the incidents came from — so each has a row here before the cutover: squash,
merge commit, rebase-merge, a reworded patch, a revert, the document lane, an archive branch, a
cloud session (merged, and its empty report commit alone), a batch ref and an attested sha.

:data:`EDGES` is the table: ``(path, edge, expected, gap)``. Each edge is an edit
(:data:`scenarios.EDITS`) on the world under the ``open`` host — what a person, the lane or the
merge queue did — and ``expected``/``gap`` read as in ``test_close_paths.py``. The ingest is
asked of every edge (the record's verdict); the lane's merge facts of the edges that write a
run line; the release note of the reverted landing.
"""
import re
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.scenarios…` does not
    import scenarios as S
except ImportError:  # pragma: no cover - import shape only
    from tests import scenarios as S

CB = 'trunkclose.closes_before_launch'
IN = 'ingest.derive via merge_facts'
RL = 'release notes (I12)'
MF = 'evidence.merge_facts'

#: ``(path, edge, expected, gap)``
EDGES = (
    # the three merge methods: each lands the Task through its PR
    (IN, 'squash', 'closes', None),
    (IN, 'merge-commit', 'closes', None),
    (IN, 'rebase-merge', 'closes', None),
    # a squash whose subject a person reworded: the PR merged all the same
    (IN, 'reworded-patch', 'closes', None),
    # a landing the trunk reverted holds the Task's work no more (S-M15)
    (IN, 'revert', 'none', 'W6-PR6c'),
    (RL, 'revert', 'none', 'W6-PR6c'),
    # the document lane merged the Task's spec: a document landed, not the Task
    (IN, 'doc-lane', 'none', None),
    (MF, 'doc-lane', 'none', None),
    # a closed PR's head kept as an archive: provenance, never a landing
    (IN, 'archive-branch', 'none', None),
    # a cloud session's run landed through its PR; its empty report commit alone lands nothing
    (IN, 'cloud-session', 'closes', None),
    (MF, 'cloud-session', 'closes', None),
    (IN, 'cloud-report-only', 'none', None),
    # a batch ref cut but not landed is not the trunk; the attested batch sha the trunk moved to
    # is the landing, though the host never marked the PR merged
    (IN, 'batch-ref', 'none', None),
    (MF, 'batch-ref', 'none', None),
    (CB, 'batch-ref', 'none', None),
    (IN, 'attested-sha', 'closes', None),
    (MF, 'attested-sha', 'closes', None),
)


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')


class Edges(unittest.TestCase):
    """One generated test per row of :data:`EDGES` (``test_<edge>__<path>``)."""

    def check(self, path, edge, expected, gap):
        _scenario, o = S.run(path, 'open', edge)
        said = f'{path} × {edge}: {o}\n' + '\n'.join(o.lines)
        holds = S.holds(expected, o)
        if gap:
            self.assertFalse(holds, f'{gap} has landed — this row now holds: drop its gap marker.'
                                    f'\n{said}')
            return
        self.assertTrue(holds, f'expected {expected}\n{said}')


def _make(row):
    def test(self):
        self.check(*row)
    path, edge, expected, gap = row
    test.__doc__ = f'{edge} → {path}: {expected}' + (f' (still fails: {gap})' if gap else '')
    return test


for _row in EDGES:
    _name = f'test_{_slug(_row[1])}__{_slug(_row[0])}'
    assert not hasattr(Edges, _name), _name
    setattr(Edges, _name, _make(_row))


class Table(unittest.TestCase):

    def test_every_named_edge_has_a_row_and_an_edit(self):
        named = ('squash', 'merge-commit', 'rebase-merge', 'reworded-patch', 'revert',
                 'doc-lane', 'archive-branch', 'cloud-session', 'batch-ref', 'attested-sha')
        self.assertEqual([e for e in named if e not in {r[1] for r in EDGES}], [])
        self.assertEqual([e for e in S.EDGES if e not in {r[1] for r in EDGES}], [])
        self.assertEqual([r for r in EDGES if r[1] not in S.EDITS], [])

    def test_the_gaps_are_named_plan_items(self):
        for row in EDGES:
            self.assertIn(row[3], (None, *S.GAPS), row)


if __name__ == '__main__':
    unittest.main()
