"""One infra-red classifier (:func:`asf.flake.classify_red`) and its one answer.

Seen 2026-10-07 on a product: a run that concluded ``failure`` with jobs still queued and no job
failed (a *phantom*), and a gate job that failed with no runner, no failed step and no logs (a
*lost runner*), were both read as reds and held land-requested PRs until someone re-ran them by
hand. Both are infra: re-run once on the head (``gh run rerun --failed``), claimed ``infra`` in
``ci-cancels.json``, never a red check; a second on the same head is one watchdog breach.
"""
import json
import os
import shutil
import tempfile
import unittest

from asf import ci_queue, flake


def job(name, conclusion='success', status='completed', runner='runner-1', failed_step=False,
        jid=1):
    steps = [{'name': 'test', 'conclusion': 'failure' if failed_step else 'success'}]
    return {'id': jid, 'name': name, 'status': status, 'conclusion': conclusion,
            'runner_name': runner, 'steps': steps}


FAILED = {'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}


class TheClassifier(unittest.TestCase):
    def test_a_failed_run_with_queued_jobs_and_no_failed_job_is_a_phantom(self):
        jobs = [job('gate'), job('gate-tests', None, 'queued', runner='')]
        self.assertEqual(flake.classify_red(FAILED, jobs), flake.PHANTOM)

    def test_a_failed_run_missing_a_required_job_is_a_phantom(self):
        self.assertEqual(flake.classify_red(FAILED, [job('gate')], required=['gate', 'suite']),
                         flake.PHANTOM)
        self.assertEqual(flake.classify_red(FAILED, []), flake.PHANTOM)

    def test_a_failed_job_with_no_runner_and_no_failed_step_is_a_lost_runner(self):
        jobs = [job('gate'), job('gate-tests', 'failure', runner='', jid=2)]
        self.assertEqual(flake.classify_red(FAILED, jobs), flake.LOST_RUNNER)

    def test_a_job_a_runner_judged_is_a_test_red(self):
        self.assertEqual(flake.classify_red(FAILED, [job('t', 'failure', failed_step=True)]),
                         flake.TEST)
        # one lost, one real: the real one decides
        jobs = [job('a', 'failure', runner=''), job('b', 'failure', failed_step=True)]
        self.assertEqual(flake.classify_red(FAILED, jobs), flake.TEST)

    def test_a_queued_run_is_no_phantom(self):
        run = {'status': 'queued', 'conclusion': None}
        self.assertEqual(flake.classify_red(run, [job('gate', None, 'queued')]), flake.TEST)


class GH:
    """``gh`` as the classifier and the re-run call it: a run, its jobs, every call recorded."""

    def __init__(self, run, jobs, rerun_rc=0):
        self.run, self.jobs, self.rerun_rc, self.calls = run, jobs, rerun_rc, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ['run', 'rerun']:
            return self.rerun_rc, '', 'HTTP 503' if self.rerun_rc else ''
        if args[0] == 'api' and args[1].endswith('/jobs?per_page=100'):
            return 0, json.dumps({'jobs': self.jobs}), ''
        if args[0] == 'api' and '/actions/runs/' in args[1]:
            return 0, json.dumps(self.run), ''
        return 1, '', 'unexpected'

    def reruns(self):
        return [c for c in self.calls if c[:2] == ['run', 'rerun']]


LINK = 'https://github.com/o/p/actions/runs/77/job/{}'


class TheOneAnswer(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='redclass_')
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.lines = []

    def lost(self, attempt=1):
        return GH(dict(FAILED, run_attempt=attempt),
                  [job('gate'), job('gate-tests', 'failure', runner='', jid=5)])

    def test_infra_class_reads_one_run_behind_the_checks(self):
        gh = self.lost()
        got = flake.infra_class('o/p', [{'name': 'gate-tests', 'link': LINK.format(5)}], gh=gh)
        self.assertEqual(got, (flake.LOST_RUNNER, '77', 1))
        two = [{'link': LINK.format(5)}, {'link': 'https://x/actions/runs/78/job/6'}]
        self.assertIsNone(flake.infra_class('o/p', two, gh=gh))
        real = GH(FAILED, [job('gate', 'failure', failed_step=True)])
        self.assertIsNone(flake.infra_class('o/p', [{'link': LINK.format(1)}], gh=real))

    def test_a_pending_probe_reads_a_run_once_an_interval(self):
        gh = self.lost()
        checks = [{'link': LINK.format(5)}]
        self.assertIsNotNone(flake.infra_class('o/p', checks, gh=gh, state_dir=self.state))
        self.assertIsNone(flake.infra_class('o/p', checks, gh=gh, state_dir=self.state))
        self.assertEqual(len(gh.calls), 2)

    def test_the_first_infra_red_is_re_run_once_and_claimed(self):
        gh = self.lost()
        got = flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.LOST_RUNNER,
                                where='PR #9', out=self.lines.append, gh=gh, attempt=1)
        self.assertEqual(got, 'rerun')
        self.assertEqual(gh.reruns(), [['run', 'rerun', '77', '--failed', '-R', 'o/p']])
        claim = ci_queue.load_claims(self.state)['77']
        self.assertEqual((claim['cause'], claim['cls']), ('infra', flake.LOST_RUNNER))
        self.assertIn('re-run queued', self.lines[0])
        self.assertIn('not a red check', self.lines[0])

    def test_the_same_attempt_read_again_is_held_quietly(self):
        gh = self.lost()
        flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.LOST_RUNNER, out=self.lines.append,
                          gh=gh, attempt=1)
        got = flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.LOST_RUNNER,
                                out=self.lines.append, gh=gh, attempt=1)
        self.assertEqual(got, 'held')
        self.assertEqual(len(gh.reruns()), 1)
        self.assertEqual(len(self.lines), 1)

    def test_a_second_infra_red_on_the_same_head_is_one_breach(self):
        gh = self.lost()
        flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.PHANTOM, out=self.lines.append,
                          gh=gh, attempt=1)
        got = flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.PHANTOM,
                                out=self.lines.append, gh=gh, attempt=2)
        self.assertEqual(got, 'breach')
        self.assertIn('watchdog: BREACH infra red (phantom)', self.lines[-1])
        again = flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.PHANTOM,
                                  out=self.lines.append, gh=gh, attempt=3)
        self.assertEqual(again, 'held')
        self.assertEqual(len(gh.reruns()), 1)
        self.assertEqual(sum('BREACH' in l for l in self.lines), 1)
        # a new head is a new budget
        self.assertEqual(flake.infra_rerun(self.state, 'o/p', 'b' * 40, '77', flake.PHANTOM,
                                           out=self.lines.append, gh=gh, attempt=3), 'rerun')

    def test_a_refused_re_run_is_judged_as_before(self):
        gh = GH(FAILED, [], rerun_rc=1)
        got = flake.infra_rerun(self.state, 'o/p', 'a' * 40, '77', flake.PHANTOM,
                                out=self.lines.append, gh=gh, attempt=1)
        self.assertEqual(got, 'refused')
        self.assertNotIn('77', ci_queue.load_claims(self.state))


class TheCancelsLedgerKnowsInfra(unittest.TestCase):
    def test_infra_is_an_honoured_cause_in_one_group(self):
        from asf import ci_cancels
        self.assertIn('infra', ci_cancels.CAUSES)
        self.assertEqual(ci_cancels.HONOURED['infra'], 'infra')
        self.assertIn('infra', ci_cancels.GROUPS['sound'])


if __name__ == '__main__':
    unittest.main()
