"""The one review reader (asf.evidence.review): one contract — ``conventions.review_pattern``,
the item's id as the slug — for a spec, a plan and code, with the legacy
``<slug>-review-r<n>.md`` form as the fallback. Spec and plan approval in the evidence pass read
through it (spec-plan-approval-ignores-review-pattern)."""
import os
import subprocess
import unittest

from asf import reviews
from asf.conventions import Conventions
from asf.evidence import evidence, review
from tests.test_doc_lane_landing import GIT_ENV, PLAN, Product, git


class VerdictOf(unittest.TestCase):
    def test_the_verdict_line(self):
        cases = {
            'verdict: approved\n': review.APPROVED,
            '**Verdict:** APPROVED\n': review.APPROVED,
            '## Verdict: changes requested\n': review.CHANGES,
            'verdict: `changes`\n': review.CHANGES,
            'prose that says approved\n': None,
            '| verdict | approved |\n': None,     # a check row, not the verdict line
            '': None,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(review.verdict_of(text), want)

    def test_the_first_verdict_line_wins(self):
        self.assertEqual(review.verdict_of('verdict: changes requested\n\nverdict: approved\n'),
                         review.CHANGES)

    def test_a_legacy_file_falls_back_to_its_first_verdict_word(self):
        self.assertEqual(review.legacy_verdict_of('LGTM, APPROVED with nits'), review.APPROVED)
        self.assertEqual(review.legacy_verdict_of(b'BOUNCE: see C1'), review.CHANGES)
        self.assertEqual(evidence.verdict_of(b'changes requested, see below'), 'CHANGES REQUESTED')
        # the contract form never reads a stray word
        self.assertEqual(evidence.verdict_of(b'APPROVED somewhere', legacy=False), '')

    def test_head(self):
        self.assertEqual(review.head_of('verdict: approved\nhead: ABCDEF1234\n'), 'abcdef1234')
        self.assertIsNone(review.head_of('verdict: approved\n'))


class TableVerdictOf(unittest.TestCase):
    """The table decides first (D4): :func:`asf.reviews.verdict` over the checklist, `BOUNCE`
    mapped to :data:`review.CHANGES` so the lane's two-value vocabulary is never stranded."""

    MECH = reviews.CHECKLIST['spec'][0]
    REQUIRED = reviews.required('spec')

    @staticmethod
    def table(fail=None, drop=None):
        names = [n for n in TableVerdictOf.MECH if n != drop]
        lines = ['| check | result | evidence |', '| --- | --- | --- |']
        for n in names:
            lines.append(f"| {n} | {'fail' if n == fail else 'pass'} | ok |")
        return '\n'.join(lines) + '\n'

    def test_full_coverage_all_pass_is_approved(self):
        self.assertEqual(review.verdict_of(self.table(), self.REQUIRED), review.APPROVED)

    def test_a_fail_row_under_a_typed_approved_line_with_a_c_item_is_changes(self):
        for c in ('## C\n\n- `a.py:3` — fix the guard\n', '## C list\n\nThe guard at a.py:3.\n',
                  'C1. `a.py:3` — fix the guard\n', 'C: `a.py:3` — fix the guard\n'):
            with self.subTest(c=c):
                text = self.table(fail=self.MECH[0]) + '\nverdict: approved\n\n' + c
                self.assertEqual(review.verdict_of(text, self.REQUIRED), review.CHANGES)

    def test_a_fail_row_the_reviewer_approved_over_asking_nothing_is_approved(self):
        """A product's T-0362 (2026-09-27): rows for Tasks already on the trunk marked ``fail``,
        ``verdict: approved``, ``## C list`` "None." — read as changes, it went correct → review
        → correct → adjudicate with no C item to answer. 69 of 1,526 reviews over 8 days."""
        for c in ('## C list\n\nNone.\n', '## C\n\n(none)\n', '## C\n\n- none.\n',
                  "## C — Critical (0)\n\nNone found.\n",
                  "## C\n\nNone. Round 1's C1 is fixed:\n- **C1** `a.py:3` — closed\n\n## I\n\n- x\n",
                  'C: none.\n', 'No C list — nothing found blocks this diff.\n', ''):
            with self.subTest(c=c):
                text = self.table(fail=self.MECH[0]) + '\nverdict: approved\n\n' + c
                self.assertEqual(review.verdict_of(text, self.REQUIRED), review.APPROVED)

    def test_a_fail_row_under_a_changes_line_or_a_missing_row_stays_changes(self):
        empty = '\n## C\n\nNone.\n'
        self.assertEqual(review.verdict_of(self.table(fail=self.MECH[0]) +
                                           '\nverdict: changes requested\n' + empty,
                                           self.REQUIRED), review.CHANGES)
        self.assertEqual(review.verdict_of(self.table(fail=self.MECH[0]) + empty, self.REQUIRED),
                         review.CHANGES)
        self.assertEqual(review.verdict_of(self.table(drop=self.MECH[0]) +
                                           '\nverdict: approved\n' + empty, self.REQUIRED),
                         review.CHANGES)

    def test_a_required_row_removed_is_changes_the_bounce_mapped(self):
        text = self.table(drop=self.MECH[0])
        self.assertEqual(review.verdict_of(text, self.REQUIRED), review.CHANGES)

    def test_a_file_with_no_table_keeps_the_answer_its_verdict_line_gives(self):
        self.assertEqual(review.verdict_of('verdict: approved\n', self.REQUIRED), review.APPROVED)
        self.assertEqual(review.verdict_of('verdict: changes requested\n', self.REQUIRED),
                         review.CHANGES)


class Pick(unittest.TestCase):
    conv = Conventions()

    def test_the_pattern_form_and_the_legacy_fallback(self):
        paths = ['docs/reviews/1-f-0001.md', 'docs/reviews/3-f-0001.md', 'docs/reviews/2-f-0002.md',
                 'docs/reviews/spec-f-0001-review-r2.md', 'docs/reviews/f-0001-review-r4.md']
        self.assertEqual(review.pick(self.conv, paths, 'F-0001', ('f-0001', 'spec-f-0001')),
                         (4, 'docs/reviews/f-0001-review-r4.md', True))
        self.assertEqual(review.pick(self.conv, paths[:4], 'f-0001', ('spec-f-0001',)),
                         (3, 'docs/reviews/3-f-0001.md', False))
        self.assertIsNone(review.pick(self.conv, paths, 'f-0009'))

    def test_the_pattern_wins_a_tie(self):
        paths = ['docs/reviews/f-0001-review-r2.md', 'docs/reviews/2-f-0001.md']
        self.assertEqual(review.pick(self.conv, paths, 'f-0001')[1], 'docs/reviews/2-f-0001.md')

    def test_a_products_own_pattern(self):
        conv = Conventions(reviews_dir='rev', review_pattern='{reviews_dir}/{slug}/round-{n}.md')
        self.assertEqual(review.pick(conv, ['rev/t-0003/round-2.md', 'rev/t-0003/round-10.md'],
                                     'T-0003'), (10, 'rev/t-0003/round-10.md', False))


class ReviewPatternApprovesTheSpec(unittest.TestCase):
    """A spec reviewed under ``review_pattern`` (``docs/reviews/<n>-<id>.md``) is approved by the
    evidence pass — before, only the legacy name was read, so the approval was never seen; and a
    review the trunk already holds, carried on the plan branch, is the spec's, not the plan's."""

    def setUp(self):
        self.p = Product()
        self.addCleanup(self.p.close)
        p = self.p
        p.commit("seed", {"src/a.ts": "x"})
        p.publish()
        git(p.work, "checkout", "-q", "-b", "spec")
        p.commit("spec", {"docs/specs/f-0001.md": "# F-0001 — the reader\n",
                          "docs/reviews/1-f-0001.md": "| check | result |\n\nverdict: approved\n"})
        git(p.work, "push", "-q", "origin", "spec:refs/heads/cloud/spec-F-0001")
        # the spec landed on main with its review; the plan branch is cut from there
        git(p.work, "checkout", "-q", "main")
        p.commit("docs(spec): F-0002", {"docs/specs/f-0002.md": "# F-0002 — b\n",
                                        "docs/reviews/1-f-0002.md": "verdict: approved\n"})
        git(p.work, "checkout", "-q", "-b", "plan")
        p.commit("plan", {"docs/plans/f-0002.md": "# F-0002 — plan\n\n" + PLAN})
        git(p.work, "push", "-q", "origin", "plan:refs/heads/cloud/plan-F-0002")
        git(p.work, "checkout", "-q", "main")
        p.publish()
        git(p.repo, "fetch", "-q", "origin")

    def test_spec_approval_reads_the_review_pattern(self):
        ev = self.p.discover([])
        self.assertEqual(ev["features"]["F-0001"]["spec_review"], (1, "APPROVED", "1-f-0001.md"))
        self.assertEqual(evidence.feature_stage(
            {"exists": True, "approved": True, "review": (1, "APPROVED")},
            {"exists": False, "approved": False, "review": None}, [], False), "spec-approved")

    def test_a_review_the_trunk_holds_is_not_the_plans(self):
        ev = self.p.discover([])
        self.assertIsNone(ev["features"]["F-0002"]["plan_review"])

    def test_newest_on_a_branch(self):
        self.assertEqual(review.newest(self.p.product(), "cloud/spec-F-0001", "F-0001"),
                         (1, review.APPROVED, None))
        self.assertIsNone(review.newest(self.p.product(), "cloud/spec-F-0001", "F-0009"))
        self.assertIsNone(review.newest(self.p.product(), "no-such-branch", "F-0001"))



class CItems(unittest.TestCase):
    """The files a review's C list opens its items with — the finding a correction answers
    (operator policy 2026-09-27: a C-item carried over is the same finding)."""

    BODY = """verdict: changes requested

## C

1. **`docs/process/README.md:69` — rule 2f is untouched.**
   It still reads `pnpm gate` in `scripts/x.sh:3`.

2. **`docs/process/README.md:263-264` (rule 2ab) still specify the run.**
3. `.githooks/pre-push:5-6` repeats rule 2f.
- **C4** `apps/web/lib/a.ts:10` — wrong.

## I

1. `apps/other.ts:1` — nice to have.
"""

    def test_the_files_each_c_item_opens_with(self):
        self.assertEqual(review.c_items(self.BODY),
                         ['.githooks/pre-push', 'apps/web/lib/a.ts', 'docs/process/README.md'])

    def test_no_c_list_is_no_finding(self):
        self.assertEqual(review.c_items('verdict: approved\n'), [])
        self.assertEqual(review.c_items(None), [])


class IsCurrentNamingAHead(unittest.TestCase):
    """B-0147: a review naming the head it read (``Head: <sha>``, as the brief's preamble hands
    it) is committed on that branch by its own session, and the lane rewrites the branch's
    commits (reword, sign-off, trunk copies dropped, a restack onto a newer trunk). Neither
    changes what was reviewed, so the verdict stands; else every round asks for the next one."""

    def setUp(self):
        import tempfile
        self.repo = tempfile.mkdtemp()
        git(self.repo, 'init', '-q', '-b', 'main')
        self.write('app.py', 'x = 1\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'base')
        git(self.repo, 'checkout', '-qb', 'worker/T-1')
        self.write('app.py', 'x = 2\n')
        git(self.repo, 'commit', '-qam', 'task(T-1): the change')
        self.code = git(self.repo, 'rev-parse', 'HEAD')
        self.conv = Conventions()

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as fh:
            fh.write(text)

    def commit_review(self, n, head):
        path = f'{review._dir_of(self.conv)}/{n}-t-1.md'
        self.write(path, f'# Review\n\nHead: `{head[:9]}`\n\nverdict: approved\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', f'review(T-1): round {n}')

    def current(self, trunk='main'):
        ref = 'worker/T-1'
        rv = review.review_at(self.repo, self.conv, ref, 'T-1')
        self.assertEqual(rv['verdict'], review.APPROVED)
        tip = git(self.repo, 'rev-parse', ref)
        return review.is_current(self.repo, self.conv, ref, rv, tip, trunk=trunk)

    def test_the_review_commit_on_top_of_the_head_it_names_is_current(self):
        self.commit_review(1, self.code)
        self.assertTrue(self.current())

    def test_a_restack_onto_a_newer_trunk_keeps_it_current(self):
        self.commit_review(1, self.code)
        git(self.repo, 'checkout', '-q', 'main')
        self.write('other.py', 'y = 1\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'trunk moves')
        git(self.repo, 'checkout', '-q', 'worker/T-1')
        git(self.repo, 'rebase', '-q', '--committer-date-is-author-date', 'main')
        self.assertNotEqual(git(self.repo, 'rev-parse', 'worker/T-1~1'), self.code)
        self.assertTrue(self.current())

    def test_code_after_the_named_head_is_not_current(self):
        self.write('app.py', 'x = 3\n')
        git(self.repo, 'commit', '-qam', 'more code')
        self.commit_review(1, self.code)
        self.assertFalse(self.current())

    def test_an_unknown_named_head_is_not_current(self):
        self.commit_review(1, 'deadbeef1')
        self.assertFalse(self.current())


class ReadNewestReviewAtForwardRequired(unittest.TestCase):
    """``read``, ``newest`` and ``review_at`` each take a ``required=()`` that forwards to
    :func:`review.verdict_of`, so a caller that knows the checklist a review must cover (C1 of
    round 1) can make a table missing that coverage bounce instead of silently approving."""

    MECH = reviews.CHECKLIST['code'][0]
    REQUIRED = reviews.required('code')

    @staticmethod
    def table():
        return f'| check | result | evidence |\n| --- | --- | --- |\n| {ReadNewestReviewAtForwardRequired.MECH[0]} | pass | ok |\n'

    def test_read_forwards_required(self):
        text = self.table()
        self.assertEqual(review.read(text)[0], review.APPROVED)
        self.assertEqual(review.read(text, required=self.REQUIRED)[0], review.CHANGES)

    def setUp(self):
        import tempfile
        self.repo = tempfile.mkdtemp()
        git(self.repo, 'init', '-q', '-b', 'main')
        os.makedirs(os.path.join(self.repo, 'docs/reviews'), exist_ok=True)
        with open(os.path.join(self.repo, 'src.py'), 'w') as fh:
            fh.write('x = 1\n')
        with open(os.path.join(self.repo, 'docs/reviews/1-t-1.md'), 'w') as fh:
            fh.write(self.table())
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'base')
        self.conv = Conventions()

    def test_newest_forwards_required(self):
        p = self.p = Product()
        self.addCleanup(p.close)
        os.makedirs(os.path.join(p.work, 'docs/reviews'), exist_ok=True)
        p.commit('review', {'docs/reviews/1-t-1.md': self.table()})
        p.publish()
        product = p.product()
        self.assertEqual(review.newest(product, 'main', 'T-1'), (1, review.APPROVED, None))
        self.assertEqual(review.newest(product, 'main', 'T-1', required=self.REQUIRED),
                         (1, review.CHANGES, None))

    def test_review_at_forwards_required(self):
        self.assertEqual(review.review_at(self.repo, self.conv, 'main', 'T-1')['verdict'],
                         review.APPROVED)
        self.assertEqual(review.review_at(self.repo, self.conv, 'main', 'T-1',
                                          required=self.REQUIRED)['verdict'], review.CHANGES)


if __name__ == '__main__':
    unittest.main()
