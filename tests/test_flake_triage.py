"""Flake-vs-defect triage before any correct round (:mod:`asf.flake`).

* a required job red on a PR's exact head is re-run once before any correct round: green on
  its re-run is a flake — a quarantine entry (job, step, test, sha, owner card, 7-day expiry,
  shown by ``asf status``) and no correct round; red again is a real defect — the correct round
  goes as before;
* a job in quarantine that goes red is re-run, not corrected, until its entry expires;
* a merge-queue batch red on one job is re-run before it is split or a member sent back.
"""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env, flake
from asf.harvest import harvest, lane
from asf.views import status

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

    def __init__(self, rollup, rerun_rc=0, rerun_err=''):
        self.rollup, self.rerun_rc, self.rerun_err, self.calls = rollup, rerun_rc, rerun_err, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ['pr', 'checks']:
            return 0, json.dumps(self.rollup), ''
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
        with mock.patch.object(harvest, '_gh', side_effect=gh), \
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

    def test_triage_off_corrects_at_once(self):
        self.product = env.Product('p', {'repo_slug': 'o/p', 'ci': {'flake_triage': 'off'},
                                         'conventions': {'landing': 'pull-request',
                                                         'landing_checks': ['gate-tests']}})
        gh = FakeGH([check('gate-tests', 'fail', 12)])
        self.assertEqual(self.head_red(gh)['names'], ['gate-tests'])
        self.assertEqual(gh.reruns(), [])


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
            gh.assert_called_once()
        defects, _held, gh = self.triage(42)                 # past QUARANTINE_RERUNS on one sha
        self.assertEqual(defects, ['gate-tests'])
        gh.assert_not_called()

    def test_quarantine_expiry(self):
        self.quarantine('2026-09-30T12:00:00Z')               # expired yesterday
        self.assertEqual(flake.load(self.state_dir, now=NOW)['quarantine'], [])
        self.assertIsNone(flake.status_cell(self.product, state_dir=self.state_dir, now=NOW))
        defects, held, _gh = self.triage(12)                  # an ordinary job again: one re-run
        self.assertEqual(held, ['gate-tests'])
        defects, held, gh = self.triage(22)                   # red on its re-run: a defect
        self.assertEqual((defects, held), (['gate-tests'], []))
        gh.assert_not_called()

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
