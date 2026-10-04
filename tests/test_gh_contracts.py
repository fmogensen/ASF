"""The readers of the host, driven by recorded ``gh`` answers (``tests/fixtures/gh``, W3-PR3).

Each behaviour is one the host really showed (recorded read-only, redacted by
:class:`tests.contracts.Redactor`); each test drives an existing reader through its own ``gh``
seam (``harvest._gh`` or ``subprocess.run``) and asserts the verdict the incident fixed."""
import datetime
import json
import subprocess
import types
import unittest
from unittest import mock

from asf import (attestation, capacity, ci_flight, env, gh_limit, github, merge_queue,
                 stale_ref, trunk_red, upgrade)
from asf.facts import cache as facts_cache
from asf.harvest import deploy
from asf.harvest import lane
from asf.harvest import harvest as H
from tests import contracts
from tests.contracts import SLUG, FixtureGh

BEHAVIOURS = ('stale-merge-ref', 'skipped-required-job/attested',
              'skipped-required-job/not-attested', 'run-success-hiding-job', 'rate-limit',
              'pr-closed-unmerged', 'path-filtered-check', 'label-supersedes-first-run')
PRODUCT = types.SimpleNamespace(repo_slug=SLUG, conventions={})


def out(behaviour, call):
    return json.loads(contracts.load(behaviour, call)[1])


def sha_of(behaviour):
    """The sha a behaviour's ``commits/<sha>/…`` call reads."""
    return contracts.fixture(behaviour, 'check-runs')['argv'][1].split('/')[4]


def run_id(behaviour):
    return contracts.fixture(behaviour, 'run-jobs')['argv'][2]


class Base(unittest.TestCase):
    def setUp(self):
        gh_limit.reset()
        attestation._SEEN.clear()
        facts_cache.clear()
        self.addCleanup(gh_limit.reset)
        self.addCleanup(attestation._SEEN.clear)
        self.addCleanup(facts_cache.clear)

    def through_subprocess(self, *behaviours):
        fx = FixtureGh(*behaviours)
        patch = mock.patch.object(subprocess, 'run', side_effect=fx.run)
        patch.start()
        self.addCleanup(patch.stop)
        return fx


class TheFixturesLoad(unittest.TestCase):
    def test_every_named_behaviour_is_recorded_and_loads(self):
        self.assertEqual(set(contracts.behaviours()), set(BEHAVIOURS))
        for b in BEHAVIOURS:
            for c in contracts.calls(b):
                rc, stdout, stderr = contracts.load(b, c)
                self.assertIsInstance(rc, int)
                self.assertIsInstance(stdout, str)
                self.assertIsInstance(stderr, str)

    def test_a_call_no_fixture_names_is_a_failure_not_an_empty_success(self):
        rc, stdout, stderr = FixtureGh('pr-closed-unmerged')(['pr', 'view', '1'])
        self.assertEqual((rc, stdout), (1, ''))
        self.assertIn('no fixture', stderr)


class SkippedRequiredJob(Base):
    """A required job that concluded ``skipped``: never green to the merge queue; green to the
    deploy pick only on a sha the merge queue attested."""
    REQUIRED = ['gate', 'tests', 'e2e-a']

    def test_the_merge_queue_verdict_is_red_on_both_shas(self):
        for b in ('skipped-required-job/attested', 'skipped-required-job/not-attested'):
            runs = out(b, 'check-runs')['check_runs']
            state, why = merge_queue.verdict(runs, self.REQUIRED)
            self.assertEqual(state, 'red', b)
            self.assertIn('tests (skipped)', why)

    def test_the_attestation_reads_from_the_commit_status(self):
        for b, want in (('skipped-required-job/attested', True),
                        ('skipped-required-job/not-attested', False)):
            attestation._SEEN.clear()   # each behaviour's redactor starts the same sha set
            fx = FixtureGh(b)
            read = lambda p, fx=fx: json.loads(fx(['api', p])[1])  # noqa: E731
            self.assertIs(attestation.attested(SLUG, sha_of(b), read=read), want, b)

    def test_the_deploy_pick_counts_skipped_green_only_on_the_attested_sha(self):
        b = 'skipped-required-job/attested'
        self.through_subprocess(b)
        ok, rule = deploy._jobs_verdict(PRODUCT, {'databaseId': run_id(b), 'headSha': sha_of(b)},
                                        self.REQUIRED, deploy._sh)
        self.assertTrue(ok, rule)
        self.assertIn('skipped on the trunk, attested by asf/attested', rule)

        b = 'skipped-required-job/not-attested'
        attestation._SEEN.clear()
        self.through_subprocess(b)
        ok, rule = deploy._jobs_verdict(PRODUCT, {'databaseId': run_id(b), 'headSha': sha_of(b)},
                                        self.REQUIRED, deploy._sh)
        self.assertFalse(ok)
        self.assertEqual(rule, 'required job tests not green (skipped)')


    def test_an_unreadable_attestation_is_unknown_and_holds_the_deploy_pick(self):
        # S-M23: the status call does not answer — Unknown, never "not attested"
        b = 'skipped-required-job/attested'
        fx = FixtureGh(b)
        status = tuple(contracts.fixture(b, 'status')['argv'])
        del fx.answers[status]
        with mock.patch.object(subprocess, 'run', side_effect=fx.run):
            self.assertIsNone(attestation.attested(SLUG, sha_of(b)))
            ok, rule = deploy._jobs_verdict(
                PRODUCT, {'databaseId': run_id(b), 'headSha': sha_of(b)}, self.REQUIRED,
                deploy._sh)
        self.assertIsNone(ok)
        self.assertIn('unreadable', rule)
        self.assertIn(list(status), fx.calls)


class PathFilteredCheck(Base):
    """A required check the path filter skipped under its unexpanded matrix name never appears
    under its own name: not started, never green."""

    def test_the_required_check_is_absent_and_reads_not_started(self):
        runs = out('path-filtered-check', 'check-runs')['check_runs']
        self.assertNotIn('e2e-d', {deploy.job_key(r['name']) for r in runs})
        self.assertIn('e2e-d${{ matrix.suffix }}', {r['name'] for r in runs})
        state, why = merge_queue.verdict(runs, ['gate', 'e2e-d'])
        self.assertEqual((state, why), ('pending', 'e2e-d (not started)'))

    def sh(self, status):
        """The fixture's check runs as one trunk run's jobs (``gh run view --json jobs``), and
        a commit status carrying ``asf/attested`` in ``status`` (none when None)."""
        jobs = out('path-filtered-check', 'check-runs')['check_runs']
        statuses = [{'context': 'asf/attested', 'state': status}] if status else []

        def run(cmd, *a, **k):
            if cmd[:3] == ['gh', 'run', 'view']:
                return json.dumps({'jobs': jobs})
            if cmd[:2] == ['gh', 'api'] and cmd[2].endswith('/status?per_page=100'):
                return json.dumps({'state': 'success', 'statuses': statuses})
            return None
        return run

    def test_the_deploy_pick_counts_it_green_on_an_attested_trunk_sha(self):
        run = {'databaseId': 10002, 'headSha': sha_of('path-filtered-check')}
        ok, rule = deploy._jobs_verdict(PRODUCT, run, ['gate', 'e2e-d'], self.sh('success'))
        self.assertTrue(ok, rule)
        self.assertIn('e2e-d skipped on the trunk, attested by asf/attested', rule)

    def test_the_deploy_pick_reads_it_missing_on_an_unattested_sha(self):
        run = {'databaseId': 10002, 'headSha': sha_of('path-filtered-check')}
        ok, rule = deploy._jobs_verdict(PRODUCT, run, ['gate', 'e2e-d'], self.sh(None))
        self.assertFalse(ok)
        self.assertEqual(rule, 'required job e2e-d not green (missing)')

    def test_an_absent_job_with_no_unexpanded_trace_stays_missing_when_attested(self):
        run = {'databaseId': 10002, 'headSha': sha_of('path-filtered-check')}
        ok, rule = deploy._jobs_verdict(PRODUCT, run, ['gate', 'e2e-z'], self.sh('success'))
        self.assertFalse(ok)
        self.assertEqual(rule, 'required job e2e-z not green (missing)')


class RunSuccessHidingJob(Base):
    """A run that concluded ``success`` over a failed job: the run's conclusion never stands in
    for its jobs."""
    B = 'run-success-hiding-job'

    def test_the_run_concluded_success_with_a_failed_job(self):
        view = out(self.B, 'run-view')
        self.assertEqual((view['status'], view['conclusion']), ('completed', 'success'))
        self.assertIn('failure', {j['conclusion'] for j in view['jobs']})

    def test_the_deploy_pick_reads_the_failed_job(self):
        self.through_subprocess(self.B)
        ok, rule = deploy._jobs_verdict(PRODUCT, {'databaseId': run_id(self.B), 'headSha': ''},
                                        ['e2e-nightly'], deploy._sh)
        self.assertFalse(ok)
        self.assertEqual(rule, 'required job e2e-nightly not green (failure)')

    def test_the_trunk_red_reader_names_the_failed_job(self):
        jobs = out(self.B, 'run-view')['jobs']
        red = trunk_red.red_jobs(jobs, ['e2e-nightly'])
        self.assertEqual([r['name'] for r in red], ['e2e-nightly'])
        self.assertIn('/actions/runs/', red[0]['link'])


class StaleMergeRef(Base):
    """A PR red on a ``pull_request`` run created before the trunk's tip arrived is stale."""
    B = 'stale-merge-ref'

    def test_the_red_check_is_found_stale(self):
        pr = out(self.B, 'pr-view')
        tip = out(self.B, 'trunk-tip')
        self.assertEqual(pr['baseRefOid'], tip['sha'])
        arrived = int(datetime.datetime.fromisoformat(
            tip['commit']['committer']['date'].replace('Z', '+00:00')).timestamp())
        red = [c for c in out(self.B, 'check-runs')['check_runs']
               if c['conclusion'] == 'failure']
        self.assertTrue(red)
        fx = FixtureGh(self.B)
        with mock.patch.object(github, 'call', side_effect=contracts.as_call(fx)), \
                mock.patch.object(stale_ref, 'arrival', return_value=arrived):
            got = stale_ref.stale(PRODUCT, SLUG, red, tip['sha'], None)
        self.assertEqual(got, [c['name'] for c in red])
        self.assertIn('gate', got)
        self.assertEqual(stale_ref.run_of(SLUG, red[0]['html_url'])[0], 'pull_request')

    def test_a_run_created_after_the_tip_arrived_is_not_stale(self):
        red = [c for c in out(self.B, 'check-runs')['check_runs']
               if c['conclusion'] == 'failure']
        with mock.patch.object(github, 'call', side_effect=contracts.as_call(FixtureGh(self.B))), \
                mock.patch.object(stale_ref, 'arrival', return_value=1):
            self.assertEqual(stale_ref.stale(PRODUCT, SLUG, red, 'x', None), [])


class RateLimit(Base):
    """A rate-limit answer is never a result: the wrapper latches and raises."""

    def test_the_gh_wrapper_raises_rate_limited(self):
        fx = self.through_subprocess('rate-limit')
        argv = contracts.fixture('rate-limit', 'run-list')['argv']
        with self.assertRaises(gh_limit.RateLimited):
            H._gh(argv)
        self.assertEqual(fx.calls, [argv])
        self.assertTrue(gh_limit.latched())


class UpgradeReadsThroughTheClient(Base):
    """``asf upgrade``'s CI readers (W5-PR4) over the recorded host: a recorded verdict is read
    as recorded; anything the host did not answer is Unknown — never green."""
    B = 'skipped-required-job/attested'
    URL = f'https://github.com/{SLUG}.git'

    def verdict(self, *names):
        self.through_subprocess(self.B)
        return upgrade.ci_verdict(self.URL, sha_of(self.B), run=None, checks=list(names))

    def test_recorded_success_and_failure_read_as_recorded(self):
        self.assertEqual(self.verdict('job-12', 'job-13')[0], 'green')
        self.assertEqual(self.verdict('job-5')[0], 'red')

    def test_a_check_only_an_unanswered_status_read_could_name_is_unknown(self):
        self.assertEqual(self.verdict('asf/attested'), ('unknown', 'asf/attested has no run'))

    def test_an_unrecorded_run_list_defers_the_plain_install(self):
        self.through_subprocess(self.B)
        self.assertEqual(upgrade.ci_state(self.URL, sha_of(self.B), run=None)[0], 'unknown')
        self.assertIsNone(upgrade.ci_red(self.URL, sha_of(self.B), run=None))

    def test_a_rate_limit_is_unknown_and_latches(self):
        self.through_subprocess('rate-limit')
        r = upgrade._gh(None, contracts.fixture('rate-limit', 'run-list')['argv'])
        self.assertEqual((r.ok, r.reason), (False, 'rate limited'))
        self.assertTrue(gh_limit.latched())


class InFlightReadersOverARateLimit(Base):
    """The capacity count and the rewrite guard over a rate-limited host: Unknown, latched, and
    no rewrite pushed on it."""

    def product(self):
        from asf import env
        return env.Product('p', {'repo_slug': SLUG, 'ci': {'workflow': 'ci.yml'}})

    def limited(self):
        """Every ``gh`` call answers the recorded rate limit, whatever its argv."""
        rc, stdout, stderr = contracts.load('rate-limit', 'run-list')
        patch = mock.patch.object(subprocess, 'run', side_effect=lambda cmd, *_a, **_kw:
                                  subprocess.CompletedProcess(list(cmd), rc, stdout, stderr))
        patch.start()
        self.addCleanup(patch.stop)

    def test_capacity_reads_none(self):
        self.limited()
        with mock.patch('asf.ci_pool._gh_env', return_value={}):
            self.assertIsNone(capacity.ci_runs_in_flight(self.product()))
        self.assertTrue(gh_limit.latched())

    def test_the_rewrite_guard_defers(self):
        self.limited()
        with mock.patch('asf.ci_pool._gh_env', return_value={}):
            got = ci_flight.verdict(self.product(), 'worker/B-1', 'rebase',
                                    needed=ci_flight.CONFLICT)
        self.assertEqual(got, 'rebase deferred: worker/B-1 CI in flight unknown (rate limited)')


class PrClosedUnmerged(Base):
    B = 'pr-closed-unmerged'

    def test_a_closed_unmerged_pr_has_no_merge_commit(self):
        pr = out(self.B, 'pr-view')
        self.assertEqual((pr['state'], pr['mergedAt'], pr['mergeCommit']), ('CLOSED', None, None))
        number = contracts.fixture(self.B, 'merged-sha')['argv'][2]
        with mock.patch.object(H, '_gh', side_effect=FixtureGh(self.B)):
            self.assertEqual(H.merged_sha(SLUG, number), '')


class LabelSupersedesFirstRun(Base):
    """A product's #1057: the heavy-CI label went on at 11:21:14 while the PR's first ``ci`` run
    (created 11:11:14) was in progress; the labelled run (11:21:17) shared its concurrency group
    and the host cancelled the first one at 11:21:37. The lane reads the head's runs before the
    label (:meth:`asf.harvest.lane.GitHubHost.run_in_flight`) and holds it while one runs."""
    B = 'label-supersedes-first-run'

    def host(self):
        product = env.Product('p', {'repo_slug': SLUG, 'conventions': {},
                                    'ci': {'workflow': 'ci.yml'}})
        return lane.GitHubHost(product, None)

    def head(self):
        return contracts.fixture(self.B, 'head-runs')['argv'][1].split('head_sha=')[1] \
            .split('&')[0]

    def test_the_first_run_was_cancelled_by_the_labelled_twin(self):
        runs = out(self.B, 'head-runs')['workflow_runs']
        ci = sorted((r for r in runs if r['name'] == 'ci'), key=lambda r: r['created_at'])
        self.assertEqual([r['conclusion'] for r in ci], ['cancelled', 'success'])
        self.assertEqual({r['head_sha'] for r in ci}, {self.head()})

    def test_at_the_labels_moment_the_first_run_is_in_flight(self):
        fx = FixtureGh(self.B)
        argv = tuple(contracts.fixture(self.B, 'head-runs')['argv'])
        body = out(self.B, 'head-runs')
        first = min((r for r in body['workflow_runs'] if r['name'] == 'ci'),
                    key=lambda r: r['created_at'])
        # rewound to 11:21:14: the labelled run not created yet, the first one in progress
        body['workflow_runs'] = [dict(r, status='in_progress', conclusion=None)
                                 if r is first else r for r in body['workflow_runs']
                                 if r['created_at'] <= '2026-10-03T11:21:14Z']
        fx.answers[argv] = (0, json.dumps(body), '')
        with mock.patch.object(H, '_gh', side_effect=fx):
            self.assertEqual(self.host().run_in_flight(self.head()), (first['id'], 'in_progress'))
        # as recorded (every run completed): nothing in flight, the label may go on
        with mock.patch.object(H, '_gh', side_effect=FixtureGh(self.B)):
            self.assertIsNone(self.host().run_in_flight(self.head()))


if __name__ == '__main__':
    unittest.main()
