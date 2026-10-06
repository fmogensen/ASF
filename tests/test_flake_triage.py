"""Flake-vs-defect triage before any correct round (:mod:`asf.flake`).

* a required job red on a PR's exact head is re-run once before any correct round: green on
  its re-run is a flake — a quarantine entry (job, step, test, sha, owner card, 7-day expiry,
  shown by ``asf status``) and no correct round; red again is a real defect — the correct round
  goes as before;
* a job in quarantine that goes red is re-run, not corrected, until its entry expires;
* a merge-queue batch red on one job is re-run before it is split or a member sent back;
* a job lost with its runner (an infra red: ``The runner has received a shutdown signal``, ``lost
  communication with the server``, ``The operation was canceled`` alone) is re-run, never sent
  to a correct round and never counted toward the flake budget.
"""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env, flake, github
from asf.harvest import harvest, lane
from asf.views import status

from tests import contracts
from tests.test_merge_queue import QueueRepo, check_run

HEAD = 'a' * 40
LINK = 'https://github.com/o/p/actions/runs/{run}/job/{job}'
NOW = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, bucket, job, run=100, at='2026-10-01T10:00:00Z'):
    return {'name': name, 'bucket': bucket, 'link': LINK.format(run=run, job=job),
            'workflow': 'ci', 'startedAt': at}


class FakeGH:
    """``gh`` as head_red calls it: the PR's checks (one list per pass), a re-run that is taken
    or refused, the failed job's steps and log."""

    def __init__(self, rollup, rerun_rc=0, rerun_err='', annotations=None, run_jobs=None):
        self.rollup, self.rerun_rc, self.rerun_err, self.calls = rollup, rerun_rc, rerun_err, []
        #: ``{job id: [annotation message]}``: the failure annotations a job carries
        self.annotations = annotations or {}
        #: ``{run id: [{name, status}]}``: a workflow run's own jobs (:func:`flake.run_live`,
        #: ``required`` narrowed)
        self.run_jobs = run_jobs or {}

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ['pr', 'checks']:
            return 0, json.dumps(self.rollup), ''
        if args[0] == 'api' and args[1].endswith('/annotations'):
            job = args[1].split('/check-runs/')[1].split('/')[0]
            return 0, json.dumps([{'annotation_level': 'failure', 'message': m}
                                  for m in self.annotations.get(job, ())]), ''
        if args[0] == 'api' and '/actions/runs/' in args[1] and '/jobs' in args[1]:
            run = args[1].split('/actions/runs/')[1].split('/')[0]
            return 0, json.dumps({'jobs': self.run_jobs.get(run, [])}), ''
        if args[0] == 'api' and '/actions/runs/' in args[1]:
            # the aggregate status a caller with no ``required`` of its own still reads: live
            # whenever this run has jobs recorded (:attr:`run_jobs`), for a test to narrow
            run = args[1].split('/actions/runs/')[1].split('/')[0].split('?')[0]
            return 0, json.dumps({'status': 'in_progress' if run in self.run_jobs
                                  else 'completed'}), ''
        if args[0] == 'api' and '/check-runs' in args[1]:
            return 0, json.dumps({'check_runs': []}), ''
        if args[:2] == ['run', 'rerun']:
            return self.rerun_rc, '', self.rerun_err
        if args[0] == 'api' and args[1].endswith('/logs'):
            return 0, '2026-10-01T10:00:00.1Z FAIL  tests/chat.spec.ts > replies once\n', ''
        if args[0] == 'api' and '/actions/jobs/' in args[1]:
            return 0, json.dumps({'steps': [{'name': 'Set up', 'conclusion': 'success'},
                                            {'name': 'Run e2e', 'conclusion': 'failure'}]}), ''
        return 1, '', 'unexpected'

    def reruns(self):
        return [c for c in self.calls if c[:2] == ['run', 'rerun']]


class Triage(unittest.TestCase):
    F = {'branch': 'cloud/T-0042', 'head': HEAD, 'pr': {'number': 902, 'head': HEAD},
         'class': lane.CODE, 'files': ['src/a.ts']}

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.lines = []
        self.product = env.Product('p', {'repo_slug': 'o/p', 'ci': {'reference_class': 'reference-heavy'},
                                         'conventions': {'landing': 'pull-request',
                                                         'landing_checks': ['gate', 'gate-tests']}})

    def head_red(self, gh):
        host = lane.GitHubHost(self.product)
        host.lane = mock.Mock(state_dir=self.state_dir, repo=None, out=self.lines.append)
        with mock.patch.object(github, 'call', side_effect=contracts.as_call(gh)), \
                mock.patch.object(host, 'merge_required', return_value=(('gate', 'gate-tests'), None)), \
                mock.patch('asf.harvest.pr_graph.checks_for', return_value=None):
            return host.head_red(self.F, 902)

    def queued_cards(self):
        path = os.path.join(self.state_dir, 'scorecard-queue.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def test_flake_is_quarantined_and_no_correct_round_is_sent(self):
        gh = FakeGH([check('gate', 'pass', 11), check('gate-tests', 'fail', 12)])
        self.assertIsNone(self.head_red(gh))                 # red once: re-run, no correct round
        self.assertEqual(gh.reruns(), [['run', 'rerun', '--job', '12', '-R', 'o/p']])
        gh.rollup = [check('gate', 'pass', 11),
                     check('gate-tests', 'pass', 22, at='2026-10-01T10:30:00Z')]
        self.assertIsNone(self.head_red(gh))                 # green on its re-run: a flake
        (q,) = flake.load(self.state_dir)['quarantine']
        self.assertEqual((q['job'], q['sha'], q['step'], q['test']),
                         ('gate-tests', HEAD, 'Run e2e', 'tests/chat.spec.ts > replies once'))
        self.assertEqual(q['reference_class'], 'reference-heavy')
        at, until = (datetime.datetime.strptime(q[k], '%Y-%m-%dT%H:%M:%SZ') for k in ('at', 'expires'))
        self.assertEqual((until - at).days, 7)
        (card,) = self.queued_cards()                         # the owner card, through the queue
        self.assertEqual(card['kind'], 'inbox')
        self.assertIn('flake-quarantine: gate-tests', card['text'])
        self.assertEqual(card['marker'], q['owner'])
        self.assertEqual(len(gh.reruns()), 1)
        cell = flake.status_cell(self.product, state_dir=self.state_dir)
        self.assertIn('gate-tests until', cell)
        self.assertIn('Run e2e', cell)

    def test_red_twice_is_a_defect_and_goes_to_a_correct_round(self):
        gh = FakeGH([check('gate', 'pass', 11), check('gate-tests', 'fail', 12)])
        self.assertIsNone(self.head_red(gh))
        gh.rollup = [check('gate', 'pass', 11),
                     check('gate-tests', 'fail', 22, at='2026-10-01T10:30:00Z')]
        red = self.head_red(gh)
        self.assertEqual(red['names'], ['gate-tests'])
        self.assertEqual(len(gh.reruns()), 1)                 # never a second re-run
        self.assertEqual(flake.load(self.state_dir)['quarantine'], [])

    def test_the_rerun_job_still_shown_red_waits_for_its_result(self):
        gh = FakeGH([check('gate-tests', 'fail', 12)])
        self.assertIsNone(self.head_red(gh))
        self.assertIsNone(self.head_red(gh))                  # the host has not shown the re-run yet
        self.assertEqual(len(gh.reruns()), 1)

    def test_a_refused_rerun_is_the_correct_round_as_before(self):
        gh = FakeGH([check('gate-tests', 'fail', 12)], rerun_rc=1, rerun_err='HTTP 403')
        self.assertEqual(self.head_red(gh)['names'], ['gate-tests'])

    def test_a_run_still_in_progress_waits_for_the_next_pass(self):
        gh = FakeGH([check('gate-tests', 'fail', 12)], rerun_rc=1,
                    rerun_err='run 100 cannot be rerun; This workflow is already running')
        self.assertIsNone(self.head_red(gh))

    def test_a_job_lost_with_its_runner_is_rerun_and_never_quarantined(self):
        shutdown = ('The runner has received a shutdown signal. This can happen when the runner '
                    'service is stopped, or a manually started runner is canceled.')
        gh = FakeGH([check('gate', 'pass', 11), check('gate-tests', 'fail', 12)],
                    annotations={'12': [shutdown, 'The operation was canceled.']})
        self.assertIsNone(self.head_red(gh))                 # infra: re-run, no correct round
        self.assertEqual(gh.reruns(), [['run', 'rerun', '--job', '12', '-R', 'o/p']])
        self.assertTrue(any('lost its runner' in l for l in self.lines), self.lines)
        gh.rollup = [check('gate', 'pass', 11),
                     check('gate-tests', 'pass', 22, at='2026-10-01T10:30:00Z')]
        self.assertIsNone(self.head_red(gh))                 # green on its re-run
        self.assertEqual(flake.load(self.state_dir)['quarantine'], [])   # no flake counted
        self.assertEqual(self.queued_cards(), [])
        self.assertTrue(any('runner loss (infra)' in l for l in self.lines), self.lines)

    def test_runner_loss_twice_is_still_rerun_and_a_real_red_after_it_gets_its_one_rerun(self):
        lost = {'12': ['The self-hosted runner: runner-3 lost communication with the server.'],
                '22': ['The operation was canceled.']}
        gh = FakeGH([check('gate-tests', 'fail', 12)], annotations=lost)
        self.assertIsNone(self.head_red(gh))
        gh.rollup = [check('gate-tests', 'fail', 22, at='2026-10-01T10:30:00Z')]
        self.assertIsNone(self.head_red(gh))                 # lost again: re-run, no defect
        gh.rollup = [check('gate-tests', 'fail', 32, at='2026-10-01T11:00:00Z')]
        self.assertIsNone(self.head_red(gh))                 # a real red: its one flake re-run
        self.assertEqual(len(gh.reruns()), 3)
        gh.rollup = [check('gate-tests', 'fail', 42, at='2026-10-01T11:30:00Z')]
        self.assertEqual(self.head_red(gh)['names'], ['gate-tests'])   # red twice: a defect

    def test_runner_loss_past_the_cap_is_held_never_corrected(self):
        gh = FakeGH([], annotations={str(j): ['The operation was canceled.']
                                     for j in range(10, 100)})
        for i in range(flake.INFRA_RERUNS + 2):
            gh.rollup = [check('gate-tests', 'fail', 10 + i, at=f'2026-10-01T1{i}:00:00Z')]
            self.assertIsNone(self.head_red(gh))
        self.assertEqual(len(gh.reruns()), flake.INFRA_RERUNS)
        self.assertEqual(sum('held for the runner pool' in l for l in self.lines), 1)

    def test_a_refused_rerun_of_a_runner_loss_is_held_not_corrected(self):
        gh = FakeGH([check('gate-tests', 'fail', 12)], rerun_rc=1, rerun_err='HTTP 403',
                    annotations={'12': ['The operation was canceled.']})
        self.assertIsNone(self.head_red(gh))

    def test_a_test_failure_beside_a_cancel_is_no_infra_red(self):
        self.assertFalse(flake.is_infra(['Process completed with exit code 1.',
                                         'The operation was canceled.']))
        self.assertFalse(flake.is_infra([]))
        self.assertFalse(flake.is_infra(['The job running on runner r1 has exceeded the maximum '
                                         'execution time of 30 minutes.']))
        self.assertTrue(flake.is_infra(['The operation was canceled.']))

    def test_triage_off_corrects_at_once(self):
        self.product = env.Product('p', {'repo_slug': 'o/p', 'ci': {'flake_triage': 'off'},
                                         'conventions': {'landing': 'pull-request',
                                                         'landing_checks': ['gate-tests']}})
        gh = FakeGH([check('gate-tests', 'fail', 12)])
        self.assertEqual(self.head_red(gh)['names'], ['gate-tests'])
        self.assertEqual(gh.reruns(), [])


class RequiredJobsOnly(unittest.TestCase):
    """A caller that names its batch's own ``required`` jobs (the merge queue) is never held
    waiting on a job it does not require (B-0274): a ``site`` job, left running alone in the
    same workflow run, kept a red ``gate-tests`` — and the queue — pending for hours."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.product = env.Product('p', {'repo_slug': 'o/p'})

    def triage(self, run_jobs, required):
        gh = FakeGH([check('gate-tests', 'fail', 12)], rerun_rc=1, rerun_err='HTTP 422: Conflict',
                    run_jobs=run_jobs)
        return flake.triage(self.product, self.state_dir, 'o/p', HEAD,
                            [check('gate-tests', 'fail', 12)], out=lambda *_: None, gh=gh,
                            required=required)

    def test_a_non_required_job_still_live_is_never_waited_on(self):
        defects, held = self.triage(
            {'100': [{'name': 'gate-tests', 'status': 'completed'},
                     {'name': 'site', 'status': 'in_progress'}]}, required=('gate', 'gate-tests'))
        self.assertEqual((defects, held), (['gate-tests'], []))

    def test_a_required_job_still_live_is_waited_on_as_before(self):
        defects, held = self.triage(
            {'100': [{'name': 'gate-tests', 'status': 'completed'},
                     {'name': 'gate', 'status': 'in_progress'}]}, required=('gate', 'gate-tests'))
        self.assertEqual((defects, held), ([], ['gate-tests']))

    def test_with_no_required_named_the_whole_run_still_decides(self):
        # the default (PR head, trunk): with no required set to narrow it, the run's own
        # aggregate status decides, unfiltered — live on ``site`` alone still holds, as before
        defects, held = self.triage(
            {'100': [{'name': 'gate-tests', 'status': 'completed'},
                     {'name': 'site', 'status': 'in_progress'}]}, required=None)
        self.assertEqual((defects, held), ([], ['gate-tests']))


def reruns(gh):
    """The ``gh run rerun`` calls a mocked ``gh`` saw (the annotation reads aside)."""
    return [c for c in gh.call_args_list if list(c.args[0][:2]) == ['run', 'rerun']]


class Door(unittest.TestCase):
    """flake reads and re-runs through :mod:`asf.github`; a test's own ``gh`` may still answer
    ``(rc, stdout, stderr)`` or a :class:`asf.github.Result`."""

    def test_the_default_door_is_the_github_client(self):
        ann = [{'annotation_level': 'failure', 'message': 'The runner has received a shutdown '
                                                           'signal.'}]
        with mock.patch.object(github, 'gh', return_value=github.Result(True, json.dumps(ann))) \
                as gh:
            self.assertIsNotNone(flake.infra_red('o/p', '12', None))
        self.assertEqual(gh.call_args.args[0], ['api', 'repos/o/p/check-runs/12/annotations'])

    def test_an_unknown_read_is_no_infra_red(self):
        with mock.patch.object(github, 'gh', return_value=github.unknown('timeout')):
            self.assertIsNone(flake.infra_red('o/p', '12', None))

    def test_an_injected_door_may_answer_a_result_or_a_triple(self):
        ann = json.dumps([{'annotation_level': 'failure',
                           'message': 'The runner has received a shutdown signal.'}])
        for answer in ((0, ann, ''), github.Result(True, ann, 0, ann)):
            self.assertIsNotNone(flake.infra_red('o/p', '12', lambda _a, answer=answer: answer))
        self.assertIsNone(flake.infra_red('o/p', '12', lambda _a: (1, '', 'HTTP 502')))


class Quarantine(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.product = env.Product('p', {'repo_slug': 'o/p'})

    def quarantine(self, expires):
        flake.save(self.state_dir, {'reruns': {}, 'quarantine': [
            {'job': 'gate-tests', 'sha': 'c' * 40, 'at': '2026-09-28T12:00:00Z',
             'expires': expires}]})

    def triage(self, job, now=NOW):
        gh = mock.Mock(return_value=(0, '', ''))
        defects, held = flake.triage(self.product, self.state_dir, 'o/p', HEAD,
                                     [check('gate-tests', 'fail', job)], out=lambda *_: None,
                                     now=now, gh=gh)
        return defects, held, gh

    def test_a_quarantined_job_red_again_is_rerun_not_corrected(self):
        self.quarantine('2026-10-05T12:00:00Z')
        for job in (12, 22, 32):
            defects, held, gh = self.triage(job)
            self.assertEqual((defects, held), ([], ['gate-tests']))
            self.assertEqual(len(reruns(gh)), 1)
        defects, _held, gh = self.triage(42)                 # past QUARANTINE_RERUNS on one sha
        self.assertEqual(defects, ['gate-tests'])
        self.assertEqual(reruns(gh), [])

    def test_quarantine_expiry(self):
        self.quarantine('2026-09-30T12:00:00Z')               # expired yesterday
        self.assertEqual(flake.load(self.state_dir, now=NOW)['quarantine'], [])
        self.assertIsNone(flake.status_cell(self.product, state_dir=self.state_dir, now=NOW))
        defects, held, _gh = self.triage(12)                  # an ordinary job again: one re-run
        self.assertEqual(held, ['gate-tests'])
        defects, held, gh = self.triage(22)                   # red on its re-run: a defect
        self.assertEqual((defects, held), (['gate-tests'], []))
        self.assertEqual(reruns(gh), [])

    def test_status_row_shows_a_live_entry(self):
        self.quarantine('2099-10-05T12:00:00Z')
        with mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir):
            self.assertIn('gate-tests until 2099-10-05', status.quarantine_cell(self.product))


class BatchTriage(QueueRepo):
    """A batch red on one required job is re-run before it is split or a member sent back."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')

    def test_a_batch_red_once_is_rerun_then_lands_and_the_job_is_quarantined(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1, 'T-0001')])
        (batch,) = self.batches()
        red = check_run('gate', 'failure')
        red['html_url'] = LINK.format(run=7, job=71)
        self.gh.checks[batch['sha']] = [red, check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])                      # no correct round, no split
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertIn(['run', 'rerun', '--job', '71', '-R', 'o/p'], self.gh.calls)
        green = check_run('gate')
        green['html_url'] = LINK.format(run=7, job=72)
        self.gh.checks[batch['sha']] = [green, check_run('gate-tests')]
        self.gh.pr_state = {1: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        (q,) = flake.load(self.state_dir)['quarantine']
        self.assertEqual((q['job'], q['sha']), ('gate', batch['sha']))

    def test_a_batch_job_lost_with_its_runner_is_rerun_then_lands_with_no_quarantine(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1, 'T-0001')])
        (batch,) = self.batches()
        red = check_run('gate-tests', 'failure')
        red['html_url'] = LINK.format(run=7, job=71)
        self.gh.annotations['71'] = [{'annotation_level': 'failure',
                                      'message': 'The runner has received a shutdown signal.'}]
        self.gh.checks[batch['sha']] = [check_run('gate'), red]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])                      # no correct round, no split
        self.assertIn(['run', 'rerun', '--job', '71', '-R', 'o/p'], self.gh.calls)
        green = check_run('gate-tests')
        green['html_url'] = LINK.format(run=7, job=72)
        self.gh.checks[batch['sha']] = [check_run('gate'), green]
        self.gh.pr_state = {1: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertEqual(flake.load(self.state_dir)['quarantine'], [])

    def test_a_batch_red_on_its_rerun_goes_back(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1, 'T-0001')])
        (batch,) = self.batches()
        red = check_run('gate', 'failure')
        red['html_url'] = LINK.format(run=7, job=71)
        self.gh.checks[batch['sha']] = [red, check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        red2 = dict(red, html_url=LINK.format(run=7, job=72))
        self.gh.checks[batch['sha']] = [red2, check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual([b for b, _k, _t, _f in self.backs], ['worker/T-0001'])
        self.assertEqual(flake.load(self.state_dir)['quarantine'], [])


if __name__ == '__main__':
    unittest.main()
