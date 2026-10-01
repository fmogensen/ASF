"""An adjudicator's ruling binds every later review of the same item (:mod:`asf.evidence.rulings`).

Reproduces a product's T-0042 (2026-09-30): review rounds 6–9 each re-raised the stray-files
point the adjudicator had ruled non-blocking twice, and each read as changes — the reviews ran
24–25k characters, so the reader's 20,000-character window cut off the ``verdict: approved``
line and the empty C list — and the lane sent the branch back to a correction with nothing to
answer: 11 sessions in 24h.
"""
import importlib
import os
import shutil
import tempfile
import unittest

from asf.evidence import review, rulings
from asf.harvest import lane
from tests.test_lane import LaneFixture, facts, rec

# ``asf.briefs.build`` is both the package's entry function and a submodule; the function wins
brief_build = importlib.import_module('asf.briefs.build')

#: A card's History as ``file_rulings`` writes it (T-0042's two rulings, abridged).
CARD = """---
id: T-0001
type: task
title: the prompt inspector
---
## Description

The pane.

## History

- 2026-09-28 05:34 adjudicate (adjudicate-t-0001): Disputed: whether the branch owes a fourth round. The twelve stray files outside writes: carry no behaviour beyond the Story: overruled, non-blocking.
- 2026-09-28 19:42 adjudicate (adjudicate-t-0001): Round 4's C list holds three findings. C2 (apps/web/prompt-actions.ts sits outside writes:) is overruled: non-blocking, the file is the reshaped Task's own. C3 upheld and fixed.
- 2026-09-28 20:00 coder (coder-t-0001): pushed 1 commit
"""

#: T-0042 round 7's own words for the stray-files row (the C item a later round re-raised).
STRAY = ('the diff stays inside `writes:` — unchanged in substance from rounds 1–6, already '
         'adjudicated non-blocking (`c4642bf7a`, `bd4781d69`): `git diff 72336dcc9..HEAD '
         '--stat` this round for the 12 stray files')


def row(check, result, evidence='x'):
    return f'| {check} | {result} | {evidence} |'


def long_review(verdict='approved', c_list='none.'):
    """A delivery review the shape of T-0042's rounds 6–9: one table per member, 24k+
    characters, the verdict and the C list at the end."""
    members = []
    for n in range(40):
        members += [f'### T-04{n:02d} — a member', '', '| check | result | evidence |',
                    '| --- | --- | --- |',
                    row('the diff stays inside `writes:`', 'fail', STRAY),
                    row('every Step of the Task is implemented', 'pass', 'y' * 200),
                    row('the acceptance tests are byte-identical to the plan\'s', 'pass'),
                    row('those tests were run and are green', 'pass'),
                    row('the Gate commands are green', 'pass'),
                    row('no secret value printed, no background process, no skipped check',
                        'pass'), '']
    return '\n'.join(['# Round 7 — T-0001', 'head: ' + 'a' * 40, ''] + members
                     + [f'verdict: {verdict}', '', '### C — must fix before approval', '',
                        c_list, '', '### I', '', '- nothing'])


class ReaderWindowTests(unittest.TestCase):

    def test_a_long_approved_review_with_no_c_reads_approved(self):
        text = long_review()
        self.assertGreater(len(text), 20000)
        self.assertGreater(text.index('verdict: approved'), 20000)
        self.assertEqual(review.verdict_of(text), review.APPROVED)

    def test_a_long_review_asking_for_changes_still_reads_changes(self):
        text = long_review('changes requested', '- C1. b.txt:3 crashes on an empty list')
        self.assertEqual(review.verdict_of(text), review.CHANGES)


class RulingsTests(unittest.TestCase):

    def setUp(self):
        self.rulings = rulings.parse(CARD)

    def test_the_card_history_yields_the_rulings_only(self):
        self.assertEqual([r['job'] for r in self.rulings], ['adjudicate-t-0001'] * 2)
        self.assertTrue(self.rulings[0]['text'].startswith('Disputed: whether'))

    def test_a_c_item_citing_a_ruling_is_settled(self):
        body = f'verdict: changes requested\n\n## C\n\n- C1. {STRAY}\n'
        self.assertEqual(rulings.reraised_only(body, self.rulings), ['adjudicate-t-0001'])

    def test_a_c_item_the_ruling_overruled_by_id_over_the_same_file_is_settled(self):
        body = ('verdict: changes requested\n\n## C\n\n'
                '- **C2** `apps/web/prompt-actions.ts` is outside `writes:`\n')
        self.assertEqual(rulings.reraised_only(body, self.rulings), ['adjudicate-t-0001'])

    def test_the_same_id_over_another_file_is_a_new_defect(self):
        body = ('verdict: changes requested\n\n## C\n\n'
                '- **C2** `apps/web/run-view.ts:40` drops the expired case\n')
        self.assertIsNone(rulings.reraised_only(body, self.rulings))

    def test_one_new_defect_beside_a_ruled_point_blocks(self):
        body = (f'verdict: changes requested\n\n## C\n\n- C1. {STRAY}\n'
                '- C2. `b.txt:3` crashes on an empty list\n')
        self.assertIsNone(rulings.reraised_only(body, self.rulings))

    def test_no_ruling_or_no_c_item_settles_nothing(self):
        body = f'verdict: changes requested\n\n## C\n\n- C1. {STRAY}\n'
        self.assertIsNone(rulings.reraised_only(body, []))
        self.assertIsNone(rulings.reraised_only('verdict: changes requested\n', self.rulings))

    def test_the_long_t0042_shape_re_raising_the_ruled_point_is_settled(self):
        text = long_review('changes requested', f'- C1. {STRAY}')
        self.assertEqual(review.verdict_of(text), review.CHANGES)
        self.assertEqual(rulings.reraised_only(text, self.rulings), ['adjudicate-t-0001'])


class GateTests(unittest.TestCase):

    def test_a_ruled_only_review_goes_to_the_gate_not_back(self):
        state, reason = lane.next_state(rec(lane.PR_OPEN), facts(
            review_required=True, review={'current': True, 'verdict': review.CHANGES,
                                          'path': 'r/t-0001-r7.md', 'round': 7},
            ruled=['adjudicate-t-0001']))
        self.assertEqual(state, lane.GATE)
        self.assertIn('only re-raises points ruled by adjudicate-t-0001', reason)


class LaneRulingsRepo(LaneFixture):
    """The lane over a real origin: the card's ruling settles a later round's C list."""

    def _setup(self, c_item):
        backlog = os.path.join(self.base, 'backlog')
        self.write(backlog, 'tasks/T-0001.md', CARD)
        review_text = ('head: \nverdict: changes requested\n\n### C — must fix\n\n'
                       f'- C1. {c_item}\n')
        self.push_lane('worker/T-0001', {'a.txt': 'a\n', 'reviews/1-t-0001.md': review_text},
                       'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        product = self.product(lane={'review': {'code': 'required'}})
        product._data['backlog_dir'] = backlog
        return product

    def test_a_round_that_only_re_raises_the_ruling_goes_to_the_gate(self):
        product = self._setup(STRAY)
        lines = []
        lane.lane_pass(product, self.state_dir, out=lines.append)
        got = self.lane_of('worker/T-0001')
        self.assertEqual(got['state'], lane.GATE, lines)
        self.assertIn('only re-raises points ruled by adjudicate-t-0001', got['reason'])

    def test_a_round_raising_a_new_defect_goes_back(self):
        product = self._setup('`b.txt:3` crashes on an empty list')
        lane.lane_pass(product, self.state_dir, out=lambda *_: None)
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.BACK)


class BriefTests(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        os.makedirs(os.path.join(self.d, 'tasks'))
        with open(os.path.join(self.d, 'tasks', 'T-0001.md'), 'w', encoding='utf-8') as f:
            f.write(CARD)

        class P:
            backlog_dir = self.d
        self.product = P()

    def test_review_correct_and_fixer_briefs_carry_the_rulings_verbatim(self):
        item = {'id': 'T-0001', 'folder': 'tasks'}
        for kind in ('review', 'correct', 'fixer'):
            with self.subTest(kind=kind):
                text = brief_build.rulings_section(self.product, kind, item)
                self.assertIn('STANDING RULINGS', text)
                for r in rulings.parse(CARD):
                    self.assertIn(r['text'], text)
        self.assertEqual(brief_build.rulings_section(self.product, 'coder', item), '')
        self.assertEqual(brief_build.rulings_section(self.product, 'review', {'id': 'T-0002'}), '')


if __name__ == '__main__':
    unittest.main()
