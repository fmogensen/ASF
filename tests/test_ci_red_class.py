"""One infra-red classifier (:func:`asf.flake.classify_red`) and its one answer.

Seen 2026-10-07 on a product: a run that concluded ``failure`` with jobs still queued and no job
failed (a *phantom*), and a gate job that failed with no runner, no failed step and no logs (a
*lost runner*), were both read as reds and held land-requested PRs until someone re-ran them by
hand. Both are infra: re-run once on the head (``gh run rerun --failed``), claimed ``infra`` in
``ci-cancels.json``, never a red check; a second on the same head is one watchdog breach.
"""
import datetime
import json
import os
import re
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
    """``gh`` as the classifier and the re-run call it: a run, its jobs, every call recorded.

    One run (``run``, ``jobs``) answers every run id by default; ``runs``, a ``{run id: (run,
    jobs)}`` map, answers a different run per run id instead — run 78 a test red while run 77
    is a lost runner, say."""

    def __init__(self, run=None, jobs=None, rerun_rc=0, runs=None):
        self.run, self.jobs, self.rerun_rc, self.calls = run, jobs, rerun_rc, []
        self.runs = runs or {}

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ['run', 'rerun']:
            return self.rerun_rc, '', 'HTTP 503' if self.rerun_rc else ''
        if args[0] == 'api' and '/actions/runs/' in args[1]:
            m = re.search(r'/actions/runs/(\w+)', args[1])
            run, jobs = self.runs.get(m.group(1), (self.run, self.jobs)) if m else (None, None)
            if args[1].endswith('/jobs?per_page=100'):
                return 0, json.dumps({'jobs': jobs}), ''
            return 0, json.dumps(run), ''
        return 1, '', 'unexpected'

    def reruns(self):
        return [c for c in self.calls if c[:2] == ['run', 'rerun']]


LINK = 'https://github.com/o/p/actions/runs/77/job/{}'


def link(run_id, job_id=1):
    return f'https://github.com/o/p/actions/runs/{run_id}/job/{job_id}'


class TheOneAnswer(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='redclass_')
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.lines = []

    def lost(self, attempt=1):
        return GH(dict(FAILED, run_attempt=attempt),
                  [job('gate'), job('gate-tests', 'failure', runner='', jid=5)])

    def test_infra_class_reads_the_run_behind_the_checks(self):
        gh = self.lost()
        got = flake.infra_class('o/p', [{'name': 'gate-tests', 'link': LINK.format(5)}], gh=gh)
        self.assertEqual(got, (flake.LOST_RUNNER, '77', 1))
        real = GH(FAILED, [job('gate', 'failure', failed_step=True)])
        self.assertIsNone(flake.infra_class('o/p', [{'link': LINK.format(1)}], gh=real))

    def test_an_unreadable_run_answers_none(self):
        class Unreadable(GH):
            def __call__(self, args):
                self.calls.append(list(args))
                return 1, '', 'rc 1'
        gh = Unreadable(FAILED, [])
        self.assertIsNone(flake.infra_class('o/p', [{'link': LINK.format(5)}], gh=gh))

    def test_the_phantom_beside_a_green_run_answers_regardless_of_order(self):
        phantom_run = dict(FAILED, run_attempt=1)
        phantom_jobs = [job('gate'), job('gate-tests', None, 'queued', runner='')]
        green_run = {'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}
        gh = GH(runs={'77': (phantom_run, phantom_jobs), '78': (green_run, [job('site')])})
        first = [{'link': link(77)}, {'link': link(78)}]
        second = [{'link': link(78)}, {'link': link(77)}]
        self.assertEqual(flake.infra_class('o/p', first, gh=gh), (flake.PHANTOM, '77', 1))
        self.assertEqual(flake.infra_class('o/p', second, gh=gh), (flake.PHANTOM, '77', 1))

    def test_two_infra_runs_answer_the_first_in_check_order_and_the_second_is_never_read(self):
        lost_run = dict(FAILED, run_attempt=1)
        lost_jobs = [job('gate'), job('gate-tests', 'failure', runner='', jid=5)]
        phantom_run = dict(FAILED, run_attempt=2)
        phantom_jobs = [job('gate'), job('gate-tests', None, 'queued', runner='')]
        gh = GH(runs={'77': (lost_run, lost_jobs), '78': (phantom_run, phantom_jobs)})
        checks = [{'link': link(77)}, {'link': link(78)}]
        got = flake.infra_class('o/p', checks, gh=gh)
        self.assertEqual(got, (flake.LOST_RUNNER, '77', 1))
        self.assertFalse([c for c in gh.calls if len(c) > 1 and 'runs/78' in c[1]])

    def test_run_ids_first_seen_order_no_duplicate_skips_non_dict(self):
        checks = [{'link': link(77)}, 'not-a-dict', {'link': link(78)}, {'link': link(77, 2)}]
        self.assertEqual(flake._run_ids(checks), ['77', '78'])

    def test_a_check_with_no_readable_run_id_is_skipped_not_fatal(self):
        gh = self.lost()
        checks = [{'link': 'https://example.com/no-run-id'}, {'link': LINK.format(5)}]
        self.assertEqual(flake.infra_class('o/p', checks, gh=gh), (flake.LOST_RUNNER, '77', 1))

    def test_every_run_id_read_gets_its_own_probe_record(self):
        phantom_run = dict(FAILED, run_attempt=1)
        phantom_jobs = [job('gate'), job('gate-tests', None, 'queued', runner='')]
        green_run = {'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}
        gh = GH(runs={'77': (phantom_run, phantom_jobs), '78': (green_run, [job('site')])})
        checks = [{'link': link(78)}, {'link': link(77)}]
        flake.infra_class('o/p', checks, gh=gh, state_dir=self.state)
        data = flake.load(self.state)
        self.assertIn('probe|78', data['infra'])
        self.assertIn('probe|77', data['infra'])

    def test_a_run_inside_its_interval_beside_one_outside_reads_only_the_outside_one(self):
        green_run = {'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}
        phantom_run = dict(FAILED, run_attempt=1)
        phantom_jobs = [job('gate'), job('gate-tests', None, 'queued', runner='')]
        gh = GH(runs={'77': (green_run, [job('site')]), '78': (phantom_run, phantom_jobs)})
        self.assertIsNone(flake.infra_class('o/p', [{'link': link(77)}], gh=gh,
                                            state_dir=self.state))
        calls_before = len(gh.calls)
        checks = [{'link': link(77)}, {'link': link(78)}]
        got = flake.infra_class('o/p', checks, gh=gh, state_dir=self.state)
        self.assertEqual(got, (flake.PHANTOM, '78', 1))
        self.assertEqual(len(gh.calls) - calls_before, 2)   # run 77 skipped: no new read of it
        self.assertFalse([c for c in gh.calls[calls_before:] if len(c) > 1 and 'runs/77' in c[1]])

    def test_a_pending_probe_reads_a_run_once_an_interval_then_again_past_it(self):
        gh = self.lost()
        checks = [{'link': LINK.format(5)}]
        self.assertIsNotNone(flake.infra_class('o/p', checks, gh=gh, state_dir=self.state))
        self.assertIsNone(flake.infra_class('o/p', checks, gh=gh, state_dir=self.state))
        self.assertEqual(len(gh.calls), 2)
        later = flake._now() + datetime.timedelta(seconds=flake.PHANTOM_PROBE_S + 1)
        self.assertIsNotNone(flake.infra_class('o/p', checks, gh=gh, state_dir=self.state,
                                               now=later))
        self.assertEqual(len(gh.calls), 4)

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
