"""asf.ci_queue — the seam the step-silence watch reads: ``Source.live_runs`` and the steps
carried on ``Source.live_jobs``, both memoised with the phantom watch's own listing so a pass
running both pays for one answer, not two."""
import os
import subprocess
import unittest

from asf import ci_queue
from tests.test_ci_phantom import HEAVY, Host, job, product


class SeamTests(unittest.TestCase):
    def test_live_runs_returns_the_products_non_completed_runs_in_the_queues_vocabulary(self):
        p, host = product(), Host()
        runs = ci_queue.GitHubSource(p, run=host).live_runs()
        self.assertEqual({r['id'] for r in runs}, {900, 110, 111, 112})
        for r in runs:
            want = host.runs[r['id']]
            self.assertEqual(r, {'id': want['id'], 'status': want['status'],
                                 'headBranch': want['head_branch'], 'headSha': want['head_sha'],
                                 'createdAt': want['created_at'],
                                 'workflow': os.path.basename(want['path'])})

    def test_live_runs_carries_the_runs_workflow_basename(self):
        p, host = product(), Host()
        host.runs[900]['path'] = '.github/workflows/nested/ci-special.yml'
        runs = ci_queue.GitHubSource(p, run=host).live_runs()
        by_id = {r['id']: r for r in runs}
        self.assertEqual(by_id[900]['workflow'], 'ci-special.yml')

    def test_the_two_listings_are_made_once_per_pass_for_both_watches(self):
        for order in ('busy_first', 'live_first'):
            with self.subTest(order=order):
                p, host = product(), Host()
                src = ci_queue.GitHubSource(p, run=host)
                if order == 'busy_first':
                    src.busy_runners()
                    src.live_runs()
                else:
                    src.live_runs()
                    src.busy_runners()
                calls = [c for c in host.calls if c[:2] == ['gh', 'api']
                        and any('/actions/runs?status=' in a for a in c)]
                self.assertEqual(len(calls), 2, calls)

    def test_an_unreadable_listing_makes_live_runs_none_and_is_not_asked_twice(self):
        p, host = product(), Host()
        attempts = []

        def flaky(argv, **kw):
            if '/actions/runs?status=queued' in ' '.join(argv):
                attempts.append(argv)
                return subprocess.CompletedProcess(argv, 1, '', 'boom')
            return host(argv, **kw)

        src = ci_queue.GitHubSource(p, run=flaky)
        self.assertIsNone(src.live_runs())
        self.assertIsNone(src.live_runs())
        self.assertEqual(len(attempts), 1)

    def test_the_base_source_returns_none_for_both_readers(self):
        src = ci_queue.Source()
        self.assertIsNone(src.live_runs())
        self.assertIsNone(src.live_jobs(1))

    def test_live_jobs_carries_the_steps_the_provider_published(self):
        p, host = product(), Host()
        jobs = ci_queue.GitHubSource(p, run=host).live_jobs(110)
        self.assertEqual(jobs[0]['steps'], host.jobs[110][0]['steps'])

    def test_a_job_with_no_steps_array_carries_no_steps_key(self):
        p, host = product(), Host()
        jobs = ci_queue.GitHubSource(p, run=host).live_jobs(900)
        gate_tests = next(j for j in jobs if j['name'] == 'gate-tests')
        self.assertNotIn('steps', gate_tests)


if __name__ == '__main__':
    unittest.main()
