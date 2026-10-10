"""F-0295 — the landing gate asks :func:`asf.flake.infra_class` on its own three stand-downs
(``asf.harvest.lane.GitHubHost.infra_gate``), exactly as the review path (``head_red``) already
does: a lost-runner red is re-run once instead of sent back as a correct round, and a phantom
under a pending or a missing required check is re-run instead of waited out or gated locally.

The sign-off-repaired-first ordering is not re-tested here: it is already proven by
``tests.test_lane.SignoffRepair.test_a_red_dco_check_triggers_the_repair_and_no_hold`` (unmoved,
this Task's own Gate runs it unchanged).
"""
import datetime
import json
import re
import unittest
from unittest import mock

from asf import flake
from asf.harvest import harvest, lane

from tests import test_lane
from tests.test_lane import HEAD, rec

_RUN_RE = re.compile(r'/actions/runs/(\d+)')

LINK = 'https://github.com/o/p/actions/runs/777/job/888'
OTHER_LINK = 'https://github.com/o/p/actions/runs/999/job/111'


def job(conclusion='failure', runner_name='', steps=(), status='completed'):
    return {'conclusion': conclusion, 'runner_name': runner_name, 'status': status,
            'steps': list(steps)}


def run(status='completed', conclusion='failure', attempt=1):
    return {'status': status, 'conclusion': conclusion, 'run_attempt': attempt}


class InfraGate(unittest.TestCase):
    """``infra_gate`` at the gate's red, pending and missing branches — reuses
    ``SkippedRequiredCheck``'s ``_host`` (:class:`tests.test_lane.SkippedRequiredCheck`), with a
    ``gh`` that answers ``pr checks``, ``/annotations``, ``/actions/runs/<id>`` and
    ``…/<id>/jobs`` by argument rather than one fixed reply."""

    def _host(self, conv=None):
        return test_lane.SkippedRequiredCheck._host(self, conv=conv)

    def _gh(self, checks, runs=None, jobs=None, rerun=(0, '', ''), annotations=()):
        runs, jobs = runs or {}, jobs or {}

        def gh(args):
            self.calls.append(args)
            if args[:2] == ['pr', 'checks']:
                return 0, json.dumps(checks), ''
            if args[0] == 'api' and '/annotations' in args[1]:
                return 0, json.dumps(list(annotations)), ''
            if args[0] == 'api' and args[1].split('?')[0].endswith('/jobs'):
                m = _RUN_RE.search(args[1])
                return 0, json.dumps({'jobs': jobs.get(m.group(1) if m else None, [])}), ''
            if args[0] == 'api' and '/actions/runs/' in args[1]:
                m = _RUN_RE.search(args[1])
                r = runs.get(m.group(1) if m else None)
                return (0, json.dumps(r), '') if r is not None else (1, '', 'not found')
            if args[:2] == ['run', 'rerun']:
                return rerun
            return 1, '', 'not found'
        return gh

    def _gate(self, host, checks, runs=None, jobs=None, rerun=(0, '', ''), annotations=(),
               prev=None, number=7, cls=lane.CODE):
        f = {'branch': 'worker/T-0001', 'prev': prev or rec(lane.GATE, pr=number), 'class': cls,
             'head': HEAD}
        self.calls, self.send_back_calls = [], []
        gh = self._gh(checks, runs, jobs, rerun, annotations)
        with mock.patch.object(harvest, '_gh', gh), \
                mock.patch.object(lane.Lane, 'set', lambda self, f, s, r, result=None, **kw:
                                  self.results.update({f['branch']: (s, r)})), \
                mock.patch.object(lane, 'send_back', lambda ln, f, *a, **k:
                                  self.send_back_calls.append(a)):
            how = host.check_gate(f, number, ['src/a.py'])
            return how, self.runner.results.get(f['branch'])

    def _reruns(self):
        return [c for c in self.calls if c[:2] == ['run', 'rerun']]

    # ---- S-80804: the red branch — a lost-runner red is re-run once, never sent back --------

    def test_a_lost_runner_red_is_re_run_once_not_sent_back(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        runs, jobs = {'777': run(attempt=1)}, {'777': [job()]}
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertEqual(len(self._reruns()), 1)
        self.assertEqual(self._reruns()[0], ['run', 'rerun', '777', '--failed', '-R', 'o/p'])
        self.assertEqual(self.send_back_calls, [])
        self.assertTrue(any('infra red (lost-runner)' in l and 're-run queued' in l
                            and 'never a correct round' in l for l in self.lines))

        # read again on the same head, after the re-run concluded (a new attempt, same cause):
        # no second ``run rerun``, one watchdog BREACH line, still no send_back
        before = len(self.lines)
        runs['777'] = run(attempt=2)
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertEqual(len(self._reruns()), 0)
        self.assertEqual(self.send_back_calls, [])
        self.assertTrue(any('BREACH' in l for l in self.lines[before:]))

        # a third read on that same head is quiet: no run rerun, no second BREACH
        before = len(self.lines)
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertEqual(len(self._reruns()), 0)
        self.assertEqual(self.send_back_calls, [])
        self.assertFalse(any('BREACH' in l for l in self.lines[before:]))

    def test_a_job_the_runner_judged_is_sent_back_as_today(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        runs = {'777': run()}
        jobs = {'777': [job(runner_name='runner-1', steps=[{'conclusion': 'failure'}])]}
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(len(self.send_back_calls), 1)
        self.assertEqual(self.send_back_calls[0][0], 'gate')
        self.assertEqual(self._reruns(), [])

    def test_a_lost_runner_the_review_path_already_re_ran_gets_no_second_rerun(self):
        host = self._host(conv={'landing_checks': ['gate']})
        data = flake.load(host.lane.state_dir)
        data['infra'][HEAD] = {'count': 1, 'class': flake.LOST_RUNNER, 'run': '777',
                               'at': flake._iso(flake._now())}
        flake.save(host.lane.state_dir, data)
        checks = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        runs, jobs = {'777': run(attempt=1)}, {'777': [job()]}
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(self._reruns(), [])
        self.assertEqual(self.send_back_calls, [])
        self.assertEqual(got[0], lane.WAITING_CI)

    def test_a_refused_rerun_is_judged_as_before_and_names_the_run(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        runs, jobs = {'777': run()}, {'777': [job()]}
        how, got = self._gate(host, checks, runs=runs, jobs=jobs,
                              rerun=(1, '', 'denied: insufficient permission'))
        self.assertIsNone(how)
        self.assertEqual(len(self.send_back_calls), 1)
        self.assertEqual(self.send_back_calls[0][0], 'gate')
        self.assertTrue(any('run 777' in l and 'refused' in l for l in self.lines))

    def test_an_unreadable_run_is_sent_back_as_before_no_exception(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        how, got = self._gate(host, checks, runs={}, jobs={})
        self.assertIsNone(how)
        self.assertEqual(len(self.send_back_calls), 1)
        self.assertEqual(self.send_back_calls[0][0], 'gate')
        self.assertEqual(self._reruns(), [])

    def test_a_stale_merge_ref_red_is_a_fresh_run_never_an_infra_read(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        runs, jobs = {'777': run()}, {'777': [job()]}
        with mock.patch.object(lane.GitHubHost, 'stale_red', return_value=True):
            how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertIn('stale merge ref', got[1])
        self.assertEqual(self._reruns(), [])
        self.assertFalse(any(c[0] == 'api' and 'actions/runs' in c[1] for c in self.calls))
        self.assertEqual(self.send_back_calls, [])

    # ---- S-80805: the pending and missing branches — a phantom is re-run, once per interval --

    def test_a_pending_phantom_is_re_run_not_waited_out(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'pending', 'link': LINK}]
        runs = {'777': run()}
        jobs = {'777': [job(conclusion='queued', runner_name=None, status='queued')]}
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertEqual(len(self._reruns()), 1)
        self.assertTrue(any('infra red (phantom)' in l and 're-run queued' in l for l in
                            self.lines))
        self.assertFalse(any('checks pending —' in l for l in self.lines))

    def test_a_pending_phantom_is_probed_once_per_interval(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'pending', 'link': LINK}]
        runs = {'777': run()}
        jobs = {'777': [job(conclusion='queued', runner_name=None, status='queued')]}
        self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertEqual(len(self._reruns()), 1)

        # inside PHANTOM_PROBE_S: the run is not re-read, no second run rerun — the ordinary
        # pending line reappears since nothing was re-answered this pass
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertFalse(any('runs/777' in c[1] for c in self.calls if len(c) > 1))
        self.assertEqual(self._reruns(), [])

        # past PHANTOM_PROBE_S: the run is re-read and the answer is held, not a second re-run
        later = flake._now() + datetime.timedelta(seconds=flake.PHANTOM_PROBE_S + 1)
        with mock.patch.object(flake, '_now', return_value=later):
            how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertTrue(any('runs/777' in c[1] for c in self.calls if len(c) > 1))
        self.assertEqual(self._reruns(), [])

    def test_a_pending_check_whose_run_is_still_queued_waits_as_before(self):
        host = self._host(conv={'landing_checks': ['gate']})
        checks = [{'name': 'gate', 'bucket': 'pending', 'link': LINK}]
        runs, jobs = {'777': run(status='queued', conclusion=None)}, {'777': []}
        how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(self._reruns(), [])
        self.assertTrue(any('checks pending —' in l for l in self.lines))

    def test_the_pending_probe_never_reads_a_run_behind_a_green_check(self):
        host = self._host(conv={'landing_checks': ['gate', 'other']})
        checks = [{'name': 'gate', 'bucket': 'pending', 'link': LINK},
                  {'name': 'other', 'bucket': 'pass', 'link': OTHER_LINK}]
        runs = {'777': run(), '999': run()}
        jobs = {'777': [job(conclusion='queued', runner_name=None, status='queued')],
               '999': [job(conclusion='success', runner_name='r', status='completed')]}
        self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertFalse(any('runs/999' in c[1] for c in self.calls if len(c) > 1))

    def test_the_missing_branch_is_probed_before_stalled_and_the_local_gate(self):
        host = self._host(conv={'landing_checks': ['gate', 'other']})
        checks = [{'name': 'gate', 'bucket': 'pass', 'link': LINK}]
        runs = {'777': run()}
        jobs = {'777': [job(conclusion='success', runner_name='r', status='completed')]}
        with mock.patch.object(lane.GitHubHost, 'stalled', side_effect=AssertionError(
                'stalled consulted on a phantom')):
            how, got = self._gate(host, checks, runs=runs, jobs=jobs)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertEqual(len(self._reruns()), 1)
        self.assertTrue(any('infra red (phantom)' in l for l in self.lines))
