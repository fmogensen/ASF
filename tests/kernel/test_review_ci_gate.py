"""A review is a read, not a test run: CI is the test gate (live 2026-10-10: T-79565's review ran
85 minutes, ~65 of them in `run_tests.py --touched` on a busy host; p50 19m, p90 42m over 160).
The kernel launches a review only once the PR's required checks are green on the head, and the
brief (full and light) states that CI result as a fact and forbids the full/touched suite."""
import unittest

from asf.kernel import actions as A
from asf.kernel.decide import decide

try:
    from kernel import builders as B
    from kernel.test_go_live import _briefer, _product
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel.test_go_live import _briefer, _product

State = B.State
REQUIRED = ('tests (3.12)', 'tests (3.13)')
HEAD = 'abcdef0123456789abcdef0123456789abcdef01'


class ReviewWaitsForCI(unittest.TestCase):

    def plan(self, checks):
        f = B.facts([B.task('T-0001', state=State.REVIEW)],
                    prs=[B.pr(4, 'T-0001', checks=checks)])
        return decide(f, B.config(required_checks=REQUIRED))

    def test_green_required_checks_launch_the_review(self):
        plan = self.plan([B.check(n, run_id=i) for i, n in enumerate(REQUIRED, 1)])
        self.assertEqual(B.launched(plan, 'review'), ['T-0001'])

    def test_a_required_check_still_running_launches_no_review(self):
        plan = self.plan([B.check(REQUIRED[0]), B.check(REQUIRED[1], status='in_progress')])
        self.assertEqual(B.launched(plan, 'review'), [])
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)

    def test_a_required_check_not_yet_reported_launches_no_review(self):
        plan = self.plan([B.check(REQUIRED[0])])
        self.assertEqual(B.launched(plan, 'review'), [])

    def test_a_red_required_check_is_a_fix_round_not_a_review(self):
        plan = self.plan([B.check(REQUIRED[0]),
                          B.check(REQUIRED[1], conclusion='failure', run_id=9)])
        self.assertEqual(B.launched(plan, 'review'), [])


class ReviewBriefStatesCI(unittest.TestCase):

    def brief(self, files=None):
        item = B.task('T-0001', state=State.REVIEW)
        pr = B.pr(7, 'T-0001', head=HEAD, files=files or ['src/a.py'],
                  checks=[B.check('tests (3.12)', run_id=555), B.check('tests (3.13)', run_id=556)])
        return _briefer(_product())(item, A.Launch('review', 'T-0001', 'worker/T-0001'), [],
                                    pr).text

    def check_ci_text(self, text):
        self.assertIn('CI is green on head `%s`' % HEAD, text)
        self.assertIn('https://github.com/acme/sample/actions/runs/555', text)
        self.assertIn('https://github.com/acme/sample/actions/runs/556', text)
        self.assertIn('Do NOT run the full or the touched test suite', text)
        self.assertIn('run_tests.py', text)
        self.assertIn('at most one or two', text)
        self.assertIn('timeout', text)

    def test_full_review_brief_carries_the_ci_fact_and_the_no_suite_rule(self):
        text = self.brief()
        self.check_ci_text(text)
        # the floor's "were run and are green" rows no longer ask the reviewer to run anything
        self.assertNotIn('those tests were run and are green', text)
        self.assertNotIn('each command\'s last line', text)

    def test_light_review_brief_carries_the_ci_fact_and_the_no_suite_rule(self):
        text = self.brief(files=['docs/a.md'])
        self.assertIn('Light review', text)
        self.check_ci_text(text)


if __name__ == '__main__':
    unittest.main()
