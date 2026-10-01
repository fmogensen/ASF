"""Main as attestation (:func:`asf.merge_queue.attest`).

When the merge queue lands a batch whose exact sha had a green batch run, that sha gets the
commit status ``asf/attested`` = ``success`` before the trunk points at it — ``target_url`` the
batch run, description ``ASF-Batch-Run: <run id>`` — so the product's push run on the trunk can
skip its heavy matrix and deploy from the attestation. Never for a red, pending, moved or stale
batch; a refused status never blocks the landing.
"""
import json
import os
import unittest
from unittest import mock

from asf import merge_queue
from asf.harvest import harvest

from tests.test_merge_queue import QueueRepo, check_run

RUN_URL = 'https://github.com/o/p/actions/runs/{run}/job/{job}'


def green_run(name, run=4242, job=1):
    r = check_run(name)
    r['html_url'] = RUN_URL.format(run=run, job=job)
    return r


class Attestation(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')

    def reroute(self, fn):
        patch = mock.patch.object(harvest, '_gh', side_effect=fn)
        patch.start()
        self.addCleanup(patch.stop)

    def statuses(self):
        return [c for c in self.gh.calls if c[:3] == ['api', '-X', 'POST']
                and '/statuses/' in c[3]]

    def cut(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        return batch

    def test_a_green_exact_sha_batch_is_attested_before_the_trunk_moves(self):
        batch = self.cut()
        self.gh.checks[batch['sha']] = [green_run('gate', job=1), green_run('gate-tests', job=2)]
        self.gh.pr_state = {1: 'MERGED'}
        main_at_status = []

        def spy(args):
            if args[:3] == ['api', '-X', 'POST']:
                main_at_status.append(self.heads()['main'])
            return self.gh(args)
        self.reroute(spy)
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertEqual(main_at_status, [batch['base']])    # set before the trunk moved
        (call,) = self.statuses()
        self.assertEqual(call[3], f"repos/o/p/statuses/{batch['sha']}")
        fields = dict(a.split('=', 1) for a in call[4:] if '=' in a)
        self.assertEqual(fields['state'], 'success')
        self.assertEqual(fields['context'], 'asf/attested')
        self.assertEqual(fields['target_url'], 'https://github.com/o/p/actions/runs/4242')
        self.assertTrue(fields['description'].startswith('ASF-Batch-Run: 4242'))
        with open(os.path.join(self.state_dir, 'gates.jsonl'), encoding='utf-8') as fh:
            ledger = [json.loads(l) for l in fh]
        self.assertIn('ASF-Batch-Run: 4242', ledger[-1]['line'] if 'line' in ledger[-1]
                      else json.dumps(ledger[-1]))
        self.assertIn('merge queue: attested', '\n'.join(self.lines))

    def test_a_red_batch_is_never_attested(self):
        batch = self.cut()
        self.gh.checks[batch['sha']] = [green_run('gate'), check_run('gate-tests', 'failure')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.statuses(), [])
        self.assertEqual(self.heads()['main'], batch['base'])

    def test_a_pending_batch_is_never_attested(self):
        batch = self.cut()
        self.gh.checks[batch['sha']] = [green_run('gate'),
                                        check_run('gate-tests', None, 'in_progress')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.statuses(), [])

    def test_a_batch_whose_trunk_moved_is_never_attested(self):
        batch = self.cut()
        self.push_main({'hot.txt': 'fix\n'}, 'hotfix')
        self.gh.checks[batch['sha']] = [green_run('gate'), green_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertNotIn(batch['sha'], [c[3].rsplit('/', 1)[-1] for c in self.statuses()])

    def test_a_refused_status_never_blocks_the_landing(self):
        batch = self.cut()
        self.gh.checks[batch['sha']] = [green_run('gate'), green_run('gate-tests')]
        self.gh.pr_state = {1: 'MERGED'}

        def refuse(args):
            if args[:3] == ['api', '-X', 'POST']:
                self.gh.calls.append(list(args))
                return 1, '', 'HTTP 403: Resource not accessible'
            return self.gh(args)
        self.reroute(refuse)
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertIn('not attested', '\n'.join(self.lines))


class AttestedRuns(unittest.TestCase):
    def test_the_run_carrying_most_required_checks_is_named_first(self):
        runs = [green_run('gate', run=7), green_run('gate-tests (1)', run=8),
                green_run('gate-tests (2)', run=8)]
        self.assertEqual(merge_queue.attested_runs(runs, ('gate', 'gate-tests')), ['8', '7'])

    def test_no_attestation_unless_every_required_check_is_success(self):
        runs = [green_run('gate'), check_run('gate-tests', 'skipped')]
        self.assertEqual(merge_queue.attested_runs(runs, ('gate', 'gate-tests')), [])
        self.assertEqual(merge_queue.attested_runs([green_run('gate')], ('gate', 'gate-tests')), [])


if __name__ == '__main__':
    unittest.main()
