"""A trunk sha the merge queue attested reads as green on its required checks.

The merge queue sets ``asf/attested`` = ``success`` on the exact sha a green batch run gated
(:func:`asf.merge_queue.attest`); the product's push run on the trunk then skips its heavy jobs.
Every reader of the trunk's required checks — the deploy's pick, ``trunk_red``, ``pr_checks`` and
the CI queue's trunk relief — counts a required job ``skipped`` there as green
(:mod:`asf.attestation`). Never a job that ran and failed, never a sha without a ``success``
attestation (``pending``, ``failure`` or none).
"""
import json
import subprocess
import types
import unittest
from unittest import mock

from asf import attestation, ci_queue, merge_queue
from asf.conventions import Conventions
from asf.harvest import deploy
from asf.harvest import harvest as H
from asf.harvest import lane

SHA, OLDER = 'a' * 40, 'b' * 40
REQUIRED = ['gate', 'rules', 'm6-e2e']


def combined(state=None, context='asf/attested'):
    """A ``commits/<sha>/status`` body carrying ``context`` in ``state`` (none when None)."""
    statuses = [{'context': 'asf/queue', 'state': 'success'}]
    if state:
        statuses.append({'context': context, 'state': state})
    return {'state': 'success', 'statuses': statuses}


def job(name, conclusion, status='completed'):
    return {'name': name, 'status': status, 'conclusion': conclusion}


class Base(unittest.TestCase):
    def setUp(self):
        attestation._SEEN.clear()
        self.addCleanup(attestation._SEEN.clear)


class Deploy(Base):
    """The deploy's pick (prod auto-deploy, ``deploy_sha``) on a trunk run's required jobs."""

    def product(self, conv=None):
        return types.SimpleNamespace(
            repo_slug='o/r', repo_dir='/repo', main='main',
            conventions=conv or {'ci_workflow': 'ci.yml', 'deploy_workflow': 'deploy-prod.yml'},
            deploy_sha={'prod': {'mode': 'auto', 'workflow': 'deploy-prod.yml',
                                 'required_jobs': REQUIRED}})

    def sh(self, jobs, status=None, context='asf/attested'):
        calls = []

        def run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ['gh', 'run', 'view']:
                return json.dumps({'jobs': jobs})
            if cmd[:2] == ['gh', 'api'] and cmd[2].endswith(f'/commits/{SHA}/status?per_page=100'):
                return json.dumps(combined(status, context))
            return None
        run.calls = calls
        return run

    def pick(self, jobs, status=None, product=None, context='asf/attested'):
        sh = self.sh(jobs, status, context)
        run = {'databaseId': 7, 'headSha': SHA, 'status': 'completed', 'conclusion': 'success'}
        return deploy._pick(product or self.product(), 'prod', [run], sh), sh

    HEAVY_SKIPPED = [job('attested', 'success'), job('rules', 'success'),
                     job('gate', 'skipped'), job('m6-e2e', 'skipped')]

    def test_attested_and_heavy_skipped_is_green_for_deploy(self):
        (sha, rule), _sh = self.pick(self.HEAVY_SKIPPED, status='success')
        self.assertEqual(sha, SHA)
        self.assertIn('gate, m6-e2e skipped on the trunk, attested by asf/attested', rule)

    def test_unattested_and_heavy_skipped_is_not_green_for_deploy(self):
        (sha, rule), _sh = self.pick(self.HEAVY_SKIPPED, status=None)
        self.assertIsNone(sha)

    def test_attested_pending_or_failure_is_not_counted_for_deploy(self):
        for state in ('pending', 'failure', 'error'):
            attestation._SEEN.clear()
            (sha, _rule), _sh = self.pick(self.HEAVY_SKIPPED, status=state)
            self.assertIsNone(sha, state)

    def test_attested_but_gate_red_is_red_for_deploy(self):
        jobs = [job('rules', 'failure'), job('gate', 'skipped'), job('m6-e2e', 'skipped')]
        (sha, _rule), _sh = self.pick(jobs, status='success')
        self.assertIsNone(sha)
        green, why = deploy._jobs_verdict(self.product(), {'databaseId': 7, 'headSha': SHA},
                                          REQUIRED, self.sh(jobs, 'success'))
        self.assertFalse(green)
        self.assertEqual(why, 'required job rules not green (failure)')

    def test_attested_but_a_required_job_still_running_is_not_green(self):
        jobs = [job('rules', None, status='in_progress'), job('gate', 'skipped'),
                job('m6-e2e', 'skipped')]
        (sha, _rule), _sh = self.pick(jobs, status='success')
        self.assertIsNone(sha)

    def test_attested_does_not_stand_in_for_a_missing_job(self):
        (sha, _rule), _sh = self.pick([job('rules', 'success'), job('gate', 'skipped')],
                                      status='success')
        self.assertIsNone(sha)

    def test_all_green_reads_no_status(self):
        jobs = [job(n, 'success') for n in REQUIRED]
        (sha, _rule), sh = self.pick(jobs, status=None)
        self.assertEqual(sha, SHA)
        self.assertFalse([c for c in sh.calls if c[:2] == ['gh', 'api']])

    def test_the_context_is_configurable(self):
        conv = Conventions.from_mapping({'ci_workflow': 'ci.yml',
                                         'ci': {'attest_status': 'acme/attested'}})
        product = self.product(conv)
        (sha, _rule), _sh = self.pick(self.HEAVY_SKIPPED, status='success', product=product,
                                      context='acme/attested')
        self.assertEqual(sha, SHA)
        attestation._SEEN.clear()
        (sha, _rule), _sh = self.pick(self.HEAVY_SKIPPED, status='success', product=product)
        self.assertIsNone(sha)  # the default context is not this product's attestation


class Context(Base):
    def test_default_is_the_merge_queue_context(self):
        self.assertEqual(Conventions.from_mapping({}).attest_status(), 'asf/attested')
        self.assertEqual(merge_queue.ATTEST_CONTEXT, attestation.CONTEXT)
        conv = Conventions.from_mapping({'ci': {'attest_status': 'x/att'}})
        self.assertEqual(attestation.context(types.SimpleNamespace(conventions=conv)), 'x/att')
        self.assertEqual(attestation.context(None), 'asf/attested')

    def test_unreadable_is_not_attested(self):
        self.assertFalse(attestation.attested('o/r', SHA, read=lambda p: None))
        self.assertFalse(attestation.attested('o/r', SHA, read=lambda p: 1 / 0))
        self.assertTrue(attestation.attested('o/r', SHA, read=lambda p: combined('success')))


class TrunkRed(Base):
    """``trunk_red`` walks the trunk newest first: a skipped run judges by the attestation."""

    def host(self, runs, status):
        h = lane.GitHubHost.__new__(lane.GitHubHost)
        h.lane = types.SimpleNamespace(repo='/repo')
        h.slug, h.trunk = 'o/r', 'main'
        h.product = types.SimpleNamespace(conventions=Conventions.from_mapping({}))
        h._trunk_runs, h._attested = {}, {}

        def gh(args):
            path = args[1]
            sha = path.split('/commits/')[1].split('/')[0]
            if path.endswith('/check-runs?per_page=100'):
                return 0, json.dumps({'check_runs': runs.get(sha, [])}), ''
            return 0, json.dumps(combined(status.get(sha))), ''
        log = subprocess.CompletedProcess([], 0, f'{SHA}\n{OLDER}\n', '')
        for p in (mock.patch.object(H, '_gh', side_effect=gh),
                  mock.patch.object(H, 'sh', return_value=log)):
            p.start()
            self.addCleanup(p.stop)
        return h

    RUNS = {SHA: [job('rules', 'success'), job('gate', 'skipped')],
            OLDER: [job('rules', 'success'), job('gate', 'failure')]}

    def test_attested_and_heavy_skipped_is_green_for_trunk_red(self):
        self.assertEqual(self.host(self.RUNS, {SHA: 'success'}).trunk_red(['gate', 'rules']), {})

    def test_unattested_and_heavy_skipped_is_not_green_for_trunk_red(self):
        self.assertEqual(self.host(self.RUNS, {}).trunk_red(['gate', 'rules']), {'gate': OLDER})

    def test_attested_pending_or_failure_is_not_counted_for_trunk_red(self):
        for state in ('pending', 'failure'):
            attestation._SEEN.clear()
            self.assertEqual(self.host(self.RUNS, {SHA: state}).trunk_red(['gate']),
                             {'gate': OLDER}, state)

    def test_attested_but_gate_red_is_red_for_trunk_red(self):
        runs = {SHA: [job('rules', 'failure'), job('gate', 'failure')]}
        self.assertEqual(self.host(runs, {SHA: 'success'}).trunk_red(['gate', 'rules']),
                         {'gate': SHA, 'rules': SHA})


class PrChecks(Base):
    """``pr_checks`` / ``not_green`` on a head that is a trunk sha the queue attested."""

    def checks(self, rollup, status):
        reads = []

        def gh(args):
            reads.append(args)
            return 0, json.dumps(combined(status)), ''
        with mock.patch.object(lane.pr_graph, 'checks_for', return_value=rollup), \
                mock.patch.object(H, '_gh', side_effect=gh):
            got = lane.pr_checks('o/r', 5, ('gate', 'rules'), head=SHA)
        return got, reads

    ROLLUP = [{'name': 'rules', 'bucket': 'pass', 'workflow': 'ci', 'startedAt': '1'},
              {'name': 'gate', 'bucket': 'skipping', 'workflow': 'ci', 'startedAt': '1'}]

    def test_attested_and_heavy_skipped_is_green_for_pr_checks(self):
        (state, _detail, checks), _reads = self.checks([dict(c) for c in self.ROLLUP], 'success')
        self.assertEqual(state, 'green')
        self.assertEqual(set(lane.passed_names(checks, ('gate', 'rules'))), {'gate', 'rules'})
        gate = next(c for c in checks if c['name'] == 'gate')
        self.assertEqual(gate['attested'], 'asf/attested')

    def test_unattested_and_heavy_skipped_is_not_green_for_pr_checks(self):
        (state, _detail, checks), _reads = self.checks([dict(c) for c in self.ROLLUP], None)
        self.assertNotIn('gate', lane.passed_names(checks, ('gate', 'rules')))
        self.assertEqual(next(c for c in checks if c['name'] == 'gate')['bucket'], 'skipping')

    def test_attested_pending_or_failure_is_not_counted_for_pr_checks(self):
        for state in ('pending', 'failure'):
            (_s, _d, checks), _reads = self.checks([dict(c) for c in self.ROLLUP], state)
            self.assertNotIn('gate', lane.passed_names(checks, ('gate', 'rules')), state)

    def test_attested_but_gate_red_is_red_for_pr_checks(self):
        rollup = [{'name': 'rules', 'bucket': 'fail', 'workflow': 'ci', 'startedAt': '1'},
                  {'name': 'gate', 'bucket': 'skipping', 'workflow': 'ci', 'startedAt': '1'}]
        (state, detail, _checks), _reads = self.checks(rollup, 'success')
        self.assertEqual((state, detail), ('red', 'rules'))

    def test_nothing_skipped_reads_no_status(self):
        rollup = [{'name': n, 'bucket': 'pass', 'workflow': 'ci', 'startedAt': '1'}
                  for n in ('gate', 'rules')]
        (state, _d, _c), reads = self.checks(rollup, 'success')
        self.assertEqual((state, reads), ('green', []))


class CiQueue(Base):
    """The CI queue's trunk relief: an attested trunk run's heavy jobs skip, so the run-level
    sizing (measured on full runs) is never a reason to cancel PR runs for it."""

    def test_the_source_reads_the_attestation(self):
        product = types.SimpleNamespace(repo_slug='o/r', name='p',
                                        conventions=Conventions.from_mapping({}))
        src = ci_queue.GitHubSource.__new__(ci_queue.GitHubSource)
        src.product, src.slug = product, 'o/r'
        with mock.patch.object(src, '_gh', return_value=json.dumps(combined('success'))):
            self.assertTrue(src.attested(SHA))
        attestation._SEEN.clear()
        with mock.patch.object(src, '_gh', return_value=json.dumps(combined('pending'))):
            self.assertFalse(src.attested(SHA))
        self.assertFalse(ci_queue.Source().attested(SHA))

    def test_an_attested_trunk_run_gets_no_run_level_relief(self):
        import datetime
        now = datetime.datetime(2026, 10, 2, 12, tzinfo=datetime.timezone.utc)
        target = {'databaseId': 9, 'headSha': SHA,
                  'createdAt': (now - datetime.timedelta(hours=1)).isoformat()}
        src = mock.Mock()
        src.live_jobs.return_value = []
        src.attested.return_value = True
        q = mock.Mock()
        lines = []
        product = types.SimpleNamespace(repo_slug='o/r', name='p', main='main',
                                        conventions=Conventions.from_mapping({}))
        with mock.patch.object(ci_queue, 'workflow_for', return_value='ci.yml'), \
                mock.patch.object(ci_queue, '_broken_reserve', return_value={}):
            n = ci_queue._relieve_for(product, q, src, {}, lambda wf: [], now, target,
                                      lines.append, False, owner='main run 9', possessive='main',
                                      wait_s=60, escalate_s=120, run_level=True)
        self.assertEqual(n, 0)
        q.needs.assert_not_called()
        self.assertIn('is attested (asf/attested)', lines[-1])


if __name__ == '__main__':
    unittest.main()
