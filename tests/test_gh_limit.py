"""asf.gh_limit — 2026-09-30 07:41Z: the shared GitHub token hit its 5,000/hr core limit and every
``gh`` call failed at once. A rate-limited answer is UNKNOWN, never state: the wrapper raises
:class:`asf.gh_limit.RateLimited`, the process stops calling GitHub, and no pass acts on it — no
cancel, re-run, close, park, merge or relaunch. The budget read skips non-essential polling
under the reserve, and a pass reads one listing once."""
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from asf import ci_pool, ci_queue, env, gh_limit
from asf.harvest import harvest, lane
from tests import test_ci_queue as tq
from tests import test_lane as tl

LIMIT = ('gh: API rate limit exceeded for user ID 1. If you reach out to GitHub Support for '
         'help, please include the request ID E00B:1C07C1')
SECONDARY = 'HTTP 403: You have exceeded a secondary rate limit. Please wait a few minutes.'
MUTATING = (('run', 'cancel'), ('run', 'rerun'), ('workflow', 'run'), ('pr', 'merge'),
            ('pr', 'close'), ('pr', 'edit'), ('pr', 'create'))


def limited(argv):
    return subprocess.CompletedProcess(argv, 1, '', LIMIT)


def writes(calls):
    return [c for c in calls if tuple(c[1:3]) in MUTATING
            or (len(c) > 3 and c[1] == 'api' and '-X' in c)]


class Base(unittest.TestCase):
    def setUp(self):
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)
        self.err = io.StringIO()
        ctx = redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


class Detection(Base):
    def test_the_primary_and_secondary_limits_and_a_zero_remaining_header_are_rate_limits(self):
        for text in (LIMIT, SECONDARY, 'HTTP 429: Too Many Requests', 'x-ratelimit-remaining: 0',
                     'HTTP 403: API rate limit exceeded for installation ID 9'):
            self.assertTrue(gh_limit.is_rate_limited(text), text)

    def test_an_ordinary_failure_is_not(self):
        for text in ('HTTP 404: Not Found', 'no checks reported on the branch',
                     'HTTP 403: Resource not accessible by integration', 'x-ratelimit-remaining: 07'):
            self.assertFalse(gh_limit.is_rate_limited(text), text)

    def test_a_success_is_never_one_whatever_it_prints(self):
        gh_limit.inspect(['pr', 'list'], 0, 'API rate limit exceeded', '')
        self.assertIsNone(gh_limit.latched())

    def test_a_rate_limit_latches_the_process_and_says_so_once(self):
        with self.assertRaises(gh_limit.RateLimited):
            gh_limit.inspect(['run', 'list'], 1, '', LIMIT)
        self.assertIn('GitHub rate limit', gh_limit.latched())
        with self.assertRaises(gh_limit.RateLimited):
            gh_limit.guard(['pr', 'view'])
        self.assertEqual(self.err.getvalue().count('GitHub rate limit'), 1)

    def test_rate_limited_is_never_caught_by_an_exception_fallback(self):
        def fallback():
            try:
                gh_limit.trip('limit')
            except Exception:  # noqa: BLE001 — what every "unreadable → act anyway" branch does
                return 'acted'
        with self.assertRaises(gh_limit.RateLimited):
            fallback()


class Harvest(Base):
    """harvest._gh — the lane's and the gate's one wrapper."""

    def test_a_rate_limited_read_raises_and_the_next_call_never_spawns(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return limited(argv)
        with mock.patch.object(harvest.subprocess, 'run', side_effect=run):
            with self.assertRaises(gh_limit.RateLimited):
                harvest.gh_json(['pr', 'list', '-R', 'o/r', '--json', 'number'], [])
            with self.assertRaises(gh_limit.RateLimited):
                harvest.merged_sha('o/r', 7)
        self.assertEqual(len(calls), 1)            # latched: the second call spent nothing

    def test_an_ordinary_failure_is_still_the_callers_to_read(self):
        with mock.patch.object(harvest.subprocess, 'run', side_effect=lambda argv, **kw:
                               subprocess.CompletedProcess(argv, 1, '', 'HTTP 404: Not Found')):
            self.assertEqual(harvest.gh_json(['pr', 'view', '7', '-R', 'o/r'], 'dflt'), 'dflt')
        self.assertIsNone(gh_limit.latched())


class Lane(Base):
    """A rate-limited check read or merge moves no PR: no WAITING, no send-back, no merge."""

    def host(self):
        return tl.SkippedRequiredCheck._host(self)

    def test_a_rate_limited_check_read_changes_no_state_and_merges_nothing(self):
        host = self.host()
        f = {'branch': 'worker/T-0001', 'prev': tl.rec(lane.GATE, pr=7), 'class': lane.CODE,
             'head': tl.HEAD}
        calls, moved = [], {}

        def run(argv, **kw):
            calls.append(argv)
            return limited(argv)
        with mock.patch.object(harvest.subprocess, 'run', side_effect=run), \
                mock.patch.object(lane.Lane, 'set', lambda self, f, s, r, result=None, **kw:
                                  moved.update({f['branch']: (s, r)})), \
                mock.patch.object(lane, 'send_back', lambda ln, f, *a, **k:
                                  moved.update({f['branch']: ('back', a)})):
            with self.assertRaises(gh_limit.RateLimited):
                host.check_gate(f, 7, ['src/a.py'])
        self.assertEqual(moved, {})
        self.assertEqual(self.runner.results, {})
        self.assertEqual(writes(calls), [])

    def test_a_merge_refused_for_rate_is_never_read_as_refused(self):
        # the incident's own line: "MERGING → WAITING (merge refused: failed to delete remote
        # branch …: HTTP 403: API rate limit exceeded …)"
        host = self.host()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 1, '', 'failed to delete remote branch plan/F-1: HTTP 403: API rate limit '
                'exceeded for user ID 1.')
        with mock.patch.object(harvest.subprocess, 'run', side_effect=run), \
                mock.patch.object(lane.GitHubHost, 'has_queue', return_value=False):
            with self.assertRaises(gh_limit.RateLimited):
                host.merge('plan/F-1', 7)
        self.assertEqual(len(calls), 1)             # no second method tried on it


class CiQueue(tq.ReliefBase):
    """The queue's pass under a rate limit: it raises out, cancels and re-runs nothing."""

    def setUp(self):
        super().setUp()
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)
        err = redirect_stderr(io.StringIO())
        err.__enter__()
        self.addCleanup(err.__exit__, None, None, None)

    def pass_with(self, limit_when):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, base = self.gh(self.runs())

        def run(argv, **kw):
            if limit_when(argv):
                gh.calls.append(argv)
                return limited(argv)
            return base(argv, **kw)
        with self.assertRaises(gh_limit.RateLimited):
            ci_queue.queue_pass(p, source=ci_queue.GitHubSource(p, run=run),
                                out=self.lines.append, now=self.t0)
        return gh

    def test_the_fixture_cancels_when_github_answers(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.assertEqual(ci_queue.queue_pass(p, source=ci_queue.GitHubSource(p, run=run),
                                             out=self.lines.append, now=self.t0), (3, 0))

    def test_a_rate_limited_job_listing_cancels_nothing(self):
        gh = self.pass_with(lambda argv: '/jobs' in ' '.join(argv))
        self.assertEqual(self.cancels(gh), [])
        self.assertEqual(self.cancels(gh, 'rerun'), [])
        self.assertEqual(writes(gh.calls), [])

    def test_a_rate_limited_run_listing_cancels_nothing(self):
        gh = self.pass_with(lambda argv: argv[1:3] == ['run', 'list'])
        self.assertEqual(writes(gh.calls), [])

    def test_a_rate_limited_cancel_is_the_last_call_of_the_pass(self):
        # a queued run's cancel is the force-cancel (asf.run_cancel), a running one's the plain
        gh = self.pass_with(lambda argv: argv[1:3] == ['run', 'cancel']
                            or str(argv[-1]).endswith('/force-cancel'))
        self.assertEqual(len(writes(gh.calls)), 1)   # the refused one, and nothing after it

    def test_a_rate_limited_runner_read_is_no_backend_error_to_fall_back_on(self):
        backend = ci_pool.GitHubBackend(self.product(), run=lambda argv, **kw: limited(argv))
        with self.assertRaises(gh_limit.RateLimited):
            backend.runners()


class Budget(Base):
    def product(self, reserve=None):
        conv = {} if reserve is None else {'gh_rate_reserve': reserve}
        return env.Product('p', {'repo_slug': 'o/r', 'conventions': conv})

    def rate(self, remaining):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {'limit': 5000, 'remaining': remaining, 'used': 5000 - remaining}), '')
        return calls, run

    def test_under_the_reserve_is_low_and_the_read_is_made_once(self):
        calls, run = self.rate(120)
        self.assertTrue(gh_limit.low(self.product(), run=run, env={'GH_TOKEN': 't'}))
        self.assertTrue(gh_limit.low(self.product(), run=run, env={'GH_TOKEN': 't'}))
        self.assertEqual(calls, [['gh', 'api', 'rate_limit', '--jq', '.resources.core']])
        self.assertEqual(self.err.getvalue().count('non-essential polling skipped'), 1)

    def test_the_reserve_is_the_products_to_set(self):
        _calls, run = self.rate(120)
        self.assertFalse(gh_limit.low(self.product(reserve=100), run=run, env={'GH_TOKEN': 't'}))
        self.assertEqual(gh_limit.reserve_of(self.product()), gh_limit.DEFAULT_RESERVE)
        self.assertEqual(gh_limit.reserve_of(self.product(reserve='x')), gh_limit.DEFAULT_RESERVE)

    def test_an_unreadable_budget_is_not_low(self):
        self.assertFalse(gh_limit.low(self.product(), env={'GH_TOKEN': 't'}, run=lambda argv, **kw:
                                      subprocess.CompletedProcess(argv, 1, '', 'boom')))

    def test_under_the_reserve_a_live_runs_jobs_are_unknown_and_cost_no_call(self):
        p = tq.product()
        calls = []
        src = ci_queue.GitHubSource(p, run=subprocess.run)
        with mock.patch.object(gh_limit, 'remaining', return_value=10), \
                mock.patch.object(ci_queue.subprocess, 'run',
                                  side_effect=lambda *a, **k: calls.append(a)):
            src._run = ci_queue.subprocess.run
            self.assertIsNone(src.live_jobs(42))
        self.assertEqual(calls, [])


class Memo(Base):
    def test_a_pass_reads_one_listing_once_and_a_write_forgets_it(self):
        p = tq.product()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '[]', '')
        with mock.patch.object(ci_queue.subprocess, 'run', side_effect=run):
            src = ci_queue.GitHubSource(p)
            src.gh_try(['run', 'list', '-R', 'o/r'])
            src.gh_try(['run', 'list', '-R', 'o/r'])
            self.assertEqual(len(calls), 1)
            src.gh_try(['run', 'cancel', '5', '-R', 'o/r'])
            src.gh_try(['run', 'list', '-R', 'o/r'])
        self.assertEqual(len(calls), 3)

    def test_an_injected_runner_is_never_memoised(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '[]', '')
        src = ci_queue.GitHubSource(tq.product(), run=run)
        src.gh_try(['run', 'list'])
        src.gh_try(['run', 'list'])
        self.assertEqual(len(calls), 2)


class Tick(Base):
    def test_a_rate_limited_step_is_skipped_not_failed_and_the_tick_goes_on(self):
        from asf.tick import tick

        def step(ctx):
            gh_limit.trip('limit')
        ctx = mock.Mock(product=env.Product('p', {}), stale_reason=None)
        out = io.StringIO()
        with mock.patch.object(tick, '_asf_step', return_value=step), redirect_stdout(out):
            self.assertEqual(tick.run_asf_step('wave', ctx), 0)
            self.assertEqual(tick.run_asf_step('record', ctx), 1)
        self.assertIn('[step:wave] skipped — GitHub rate limit', out.getvalue())
        self.assertEqual(ctx.stale_reason, 'GitHub rate limit')

    def test_the_cli_exits_tempfail_on_one(self):
        from asf import cli
        with mock.patch.object(cli, '_main', side_effect=gh_limit.RateLimited('x')):
            self.assertEqual(cli.main(['status']), cli.EX_TEMPFAIL)


if __name__ == '__main__':
    unittest.main()
