"""The one review reader (asf.evidence.review): one contract — ``conventions.review_pattern``,
the item's id as the slug — for a spec, a plan and code, with the legacy
``<slug>-review-r<n>.md`` form as the fallback. Spec and plan approval in the evidence pass read
through it (spec-plan-approval-ignores-review-pattern)."""
import os
import subprocess
import unittest

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

if __name__ == '__main__':
    unittest.main()
