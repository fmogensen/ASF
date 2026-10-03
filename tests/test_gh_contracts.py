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

from asf import attestation, gh_limit, merge_queue, stale_ref, trunk_red
from asf.harvest import deploy
from asf.harvest import harvest as H
from tests import contracts
from tests.contracts import SLUG, FixtureGh

BEHAVIOURS = ('stale-merge-ref', 'skipped-required-job/attested',
              'skipped-required-job/not-attested', 'run-success-hiding-job', 'rate-limit',
              'pr-closed-unmerged', 'path-filtered-check')
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
        stale_ref._RUNS.clear()
        self.addCleanup(gh_limit.reset)
        self.addCleanup(attestation._SEEN.clear)
        self.addCleanup(stale_ref._RUNS.clear)

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


class PathFilteredCheck(Base):
    """A required check the path filter skipped under its unexpanded matrix name never appears
    under its own name: not started, never green."""

    def test_the_required_check_is_absent_and_reads_not_started(self):
        runs = out('path-filtered-check', 'check-runs')['check_runs']
        self.assertNotIn('e2e-d', {deploy.job_key(r['name']) for r in runs})
        self.assertIn('e2e-d${{ matrix.suffix }}', {r['name'] for r in runs})
        state, why = merge_queue.verdict(runs, ['gate', 'e2e-d'])
        self.assertEqual((state, why), ('pending', 'e2e-d (not started)'))


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
        with mock.patch.object(H, '_gh', side_effect=fx), \
                mock.patch.object(stale_ref, 'arrival', return_value=arrived):
            got = stale_ref.stale(PRODUCT, SLUG, red, tip['sha'], None)
        self.assertEqual(got, [c['name'] for c in red])
        self.assertIn('gate', got)
        self.assertEqual(stale_ref.run_of(SLUG, red[0]['html_url'])[0], 'pull_request')

    def test_a_run_created_after_the_tip_arrived_is_not_stale(self):
        red = [c for c in out(self.B, 'check-runs')['check_runs']
               if c['conclusion'] == 'failure']
        with mock.patch.object(H, '_gh', side_effect=FixtureGh(self.B)), \
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


class PrClosedUnmerged(Base):
    B = 'pr-closed-unmerged'

    def test_a_closed_unmerged_pr_has_no_merge_commit(self):
        pr = out(self.B, 'pr-view')
        self.assertEqual((pr['state'], pr['mergedAt'], pr['mergeCommit']), ('CLOSED', None, None))
        number = contracts.fixture(self.B, 'merged-sha')['argv'][2]
        with mock.patch.object(H, '_gh', side_effect=FixtureGh(self.B)):
            self.assertEqual(H.merged_sha(SLUG, number), '')


if __name__ == '__main__':
    unittest.main()
