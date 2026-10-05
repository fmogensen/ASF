"""A landed or dropped batch's leftover CI runs are reaped (:func:`asf.merge_queue.reap_leftover_runs`).

A job the product does not require (a ``site`` build) held a heavy runner 4 h after its batch
ref was gone. The queue cancels a run still queued or in progress on a batch ref no batch in
flight holds — but never a run whose head sha is the trunk's or a live batch's while a required
check on that sha is not yet success.
"""
import json
import os
import unittest
from unittest import mock

from asf import ci_queue, github, merge_queue

from tests import contracts
from tests.test_lane import sh
from tests.test_merge_queue import FakeGH, QueueRepo, check_run


class ReapGH(FakeGH):
    def __init__(self):
        super().__init__()
        self.live = []        # workflow runs listed queued / in progress
        self.cancelled = []

    def __call__(self, args):
        if args[0] == 'api' and '/actions/runs?status=' in args[1]:
            self.calls.append(list(args))
            want = args[1].split('status=')[1].split('&')[0]
            return 0, json.dumps({'workflow_runs': [r for r in self.live
                                                    if r['status'] == want]}), ''
        if args[:2] == ['run', 'cancel']:
            self.calls.append(list(args))
            self.cancelled.append(int(args[2]))
            return 0, '', ''
        if args[:3] == ['api', '-X', 'POST'] and args[3].endswith('/force-cancel'):
            self.calls.append(list(args))
            self.cancelled.append(int(args[3].split('/')[-2]))
            return 0, '', ''
        if args[0] == 'api' and '/actions/runs/' in args[1] and '--jq' in args:
            self.calls.append(list(args))
            return 0, 'completed', ''
        return super().__call__(args)


class Reap(QueueRepo):
    def setUp(self):
        super().setUp()
        self.gh = ReapGH()
        patch = mock.patch.object(github, 'call', side_effect=contracts.as_call(self.gh))
        patch.start()
        self.addCleanup(patch.stop)
        self.main = sh(['git', 'rev-parse', 'main'], cwd=self.origin).stdout.strip()

    def run_on(self, rid, ref, sha, status='in_progress'):
        self.gh.live.append({'id': rid, 'head_branch': ref, 'head_sha': sha, 'status': status})

    def reap(self, batches=(), now=1_000_000):
        ln = self.lane()
        st = merge_queue.settings(ln.conv)
        return merge_queue.reap_leftover_runs(ln, st, list(batches), now=now)

    def test_a_dropped_batch_s_leftover_run_is_cancelled_and_claimed(self):
        self.run_on(901, 'batch/20261005-1200-aaaaaaa', 'a' * 40)
        self.run_on(902, 'batch/20261005-1300-bbbbbbb', 'b' * 40, status='queued')
        self.assertEqual(self.reap(), 2)
        self.assertEqual(sorted(self.gh.cancelled), [901, 902])
        claims = ci_queue.load_claims(self.state_dir)
        self.assertEqual({str(k) for k in claims}, {'901', '902'})
        self.assertTrue(any('reaped leftover run 901' in l for l in self.lines), self.lines)

    def test_a_landed_batch_s_leftover_on_main_s_sha_is_cancelled_once_required_is_green(self):
        self.run_on(903, 'batch/20261005-1200-ccccccc', self.main)
        self.gh.checks[self.main] = [check_run('gate'), check_run('gate-tests'),
                                     check_run('site', None, 'in_progress')]
        self.assertEqual(self.reap(), 1)
        self.assertEqual(self.gh.cancelled, [903])

    def test_a_run_on_main_s_sha_with_a_required_check_not_success_is_kept(self):
        self.run_on(904, 'batch/20261005-1200-ddddddd', self.main)
        self.gh.checks[self.main] = [check_run('gate'), check_run('gate-tests', None, 'in_progress')]
        self.assertEqual(self.reap(), 0)
        self.assertEqual(self.gh.cancelled, [])
        self.assertTrue(any('904' in l and 'kept' in l for l in self.lines), self.lines)

    def test_a_live_batch_s_run_and_other_branches_are_never_touched(self):
        self.run_on(905, 'batch/live', 'e' * 40)
        self.run_on(906, 'worker/T-0001', 'f' * 40)
        self.run_on(907, 'main', self.main)
        self.assertEqual(self.reap([{'ref': 'batch/live', 'sha': 'e' * 40}]), 0)
        self.assertEqual(self.gh.cancelled, [])

    def test_a_dead_ref_s_run_on_a_live_batch_s_sha_waits_for_its_required_checks(self):
        self.run_on(908, 'batch/old', '9' * 40)
        self.assertEqual(self.reap([{'ref': 'batch/live', 'sha': '9' * 40}]), 0)
        self.assertEqual(self.gh.cancelled, [])

    def test_the_reap_lists_at_most_once_per_interval(self):
        self.run_on(909, 'batch/gone', 'a' * 40)
        self.assertEqual(self.reap(now=2_000_000), 1)
        self.gh.live = []
        self.run_on(910, 'batch/gone2', 'b' * 40)
        self.assertEqual(self.reap(now=2_000_000 + 10), 0)
        self.assertEqual(self.reap(now=2_000_000 + merge_queue.REAP_EVERY_S + 1), 1)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, merge_queue.REAP_FILE)))


if __name__ == '__main__':
    unittest.main()
