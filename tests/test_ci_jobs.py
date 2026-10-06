import unittest

from asf import ci_jobs


def job(conclusion, minutes=0, runner='box1', limit=None):
    return {'name': 'tests', 'conclusion': conclusion, 'minutes': minutes, 'runner': runner, 'limit': limit}


class CancelCauseTests(unittest.TestCase):
    def test_at_its_limit_is_timeout_even_superseded_beside_a_cancelled_sibling(self):
        j = job('cancelled', minutes=30, limit=30)
        sibling = job('cancelled', minutes=1, limit=30)
        self.assertEqual(ci_jobs.cancel_cause(j, [j, sibling], 'cancelled', superseded=True),
                          ci_jobs.TIMEOUT)

    def test_one_minute_under_the_limit_is_timeout_with_the_slack(self):
        j = job('cancelled', minutes=29, limit=30)
        self.assertEqual(ci_jobs.cancel_cause(j, [j], 'cancelled'), ci_jobs.TIMEOUT)

    def test_a_red_sibling_is_failure(self):
        j = job('cancelled', minutes=2)
        sibling = job('failure', minutes=10)
        self.assertEqual(ci_jobs.cancel_cause(j, [j, sibling], 'cancelled'), ci_jobs.FAILURE)

    def test_a_run_whose_own_conclusion_is_failure_is_failure(self):
        j = job('cancelled', minutes=2)
        self.assertEqual(ci_jobs.cancel_cause(j, [j], 'failure'), ci_jobs.FAILURE)

    def test_a_superseded_run_whose_fast_sibling_went_green_is_failure(self):
        j = job('cancelled', minutes=1)
        sibling = job('success', minutes=1)
        self.assertEqual(ci_jobs.cancel_cause(j, [j, sibling], 'cancelled', superseded=True),
                          ci_jobs.FAILURE)

    def test_a_job_with_no_runner_and_zero_minutes_is_failure(self):
        j = job('cancelled', minutes=0, runner=None)
        self.assertEqual(ci_jobs.cancel_cause(j, [j], 'cancelled'), ci_jobs.FAILURE)

    def test_a_run_whose_every_started_job_is_cancelled_is_failure(self):
        j = job('cancelled', minutes=1)
        sibling = job('cancelled', minutes=2)
        self.assertEqual(ci_jobs.cancel_cause(j, [j, sibling], 'cancelled'), ci_jobs.FAILURE)

    def test_cancelled_well_short_of_a_known_limit_beside_a_green_sibling_is_runner_loss(self):
        j = job('cancelled', minutes=2, limit=30)
        sibling = job('success', minutes=25)
        self.assertEqual(ci_jobs.cancel_cause(j, [j, sibling], 'cancelled'), ci_jobs.RUNNER_LOSS)

    def test_the_same_job_with_no_known_limit_is_failure(self):
        j = job('cancelled', minutes=2, limit=None)
        sibling = job('success', minutes=25)
        self.assertEqual(ci_jobs.cancel_cause(j, [j, sibling], 'cancelled'), ci_jobs.FAILURE)

    def test_a_job_that_did_not_end_cancelled_gets_none(self):
        for conclusion in ('success', 'failure', 'timed_out', 'skipped'):
            j = job(conclusion, minutes=5)
            self.assertIsNone(ci_jobs.cancel_cause(j, [j], conclusion))

    def test_the_live_timeout_run(self):
        # run 36322982383: two 360-minute legs against the host's 360-minute default, also
        # superseded by a later push (F-0131's opening incident) — arm 1 beats arm 3.
        a = {'name': 'tests (3.13)', 'conclusion': 'cancelled', 'minutes': 360, 'runner': 'box1', 'limit': 360}
        b = {'name': 'tests (3.12)', 'conclusion': 'cancelled', 'minutes': 360, 'runner': 'box2', 'limit': 360}
        jobs = [a, b]
        self.assertEqual(ci_jobs.cancel_cause(a, jobs, 'cancelled', superseded=True), ci_jobs.TIMEOUT)
        self.assertEqual(ci_jobs.cancel_cause(b, jobs, 'cancelled', superseded=True), ci_jobs.TIMEOUT)

    def test_the_live_run_wide_cancel(self):
        # run 36260144093: both jobs cancelled 19 and 91 seconds in, no limit known, no red
        # anywhere — arm 5, every started job cancelled.
        a = {'name': 'tests (3.13)', 'conclusion': 'cancelled', 'minutes': 0, 'runner': 'box1', 'limit': None}
        b = {'name': 'tests (3.12)', 'conclusion': 'cancelled', 'minutes': 1, 'runner': 'box2', 'limit': None}
        jobs = [a, b]
        self.assertEqual(ci_jobs.cancel_cause(a, jobs, 'cancelled'), ci_jobs.FAILURE)
        self.assertEqual(ci_jobs.cancel_cause(b, jobs, 'cancelled'), ci_jobs.FAILURE)


if __name__ == '__main__':
    unittest.main()
