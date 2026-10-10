"""A transient GitHub failure never ends the tick: the port retries it with a short backoff
(``kernel.github.retry_delays_s``), and a read that still fails leaves the tick blind (no action
that needs PR facts, crashed sessions still ended, exit 0)."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, gh_limit
from asf.kernel import loop, settings
from asf.kernel import ports as P
from asf.kernel.decide import CRASH

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

GATEWAY = 'HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)'


def proc(argv, rc, out='', err=''):
    return subprocess.CompletedProcess(argv, rc, out, err)


class Retry(unittest.TestCase):

    def setUp(self):
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)
        self.slept = []

    def gh(self, run, kernel=None):
        product = env.Product('sample', {'repo_slug': 'o/r', **({'kernel': kernel} if kernel else {})})
        return P.RealGitHub(product, run=run, sleep=self.slept.append)

    def test_a_504_is_retried_with_the_backoff_and_then_read(self):
        replies = [proc([], 1, '', GATEWAY), proc([], 1, '', 'HTTP 502: Bad Gateway'),
                   proc([], 0, '[]', '')]

        def run(argv, **kw):
            if argv[1:3] == ['pr', 'list'] and '--state' in argv and \
                    argv[argv.index('--state') + 1] == 'open':
                return replies.pop(0)
            return proc(argv, 0, '[]', '')
        self.assertEqual(self.gh(run).prs(), [])
        self.assertEqual(self.slept, [2, 5])

    def test_retries_spent_raise_once_and_later_reads_do_not_wait_again(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return proc(argv, 1, '', GATEWAY)
        gh = self.gh(run, kernel={'github': {'retry_delays_s': [1, 3]}})
        with self.assertRaises(P.PortError) as e:
            gh.prs()
        self.assertIn('504', str(e.exception))
        # the tick's one budget probe (never retried), then the PR list's three tries
        self.assertEqual((self.slept, len(calls)), ([1, 3], 4))
        self.assertEqual(gh._tree('h1'), '')
        self.assertEqual(self.slept, [1, 3], 'GitHub is down this tick: no second backoff')

    def test_a_timeout_and_a_secondary_rate_limit_are_transient(self):
        replies = [subprocess.TimeoutExpired('gh', 30),
                   proc([], 1, '', 'You have exceeded a secondary rate limit'),
                   proc([], 0, json.dumps({'tree': {'sha': 't9'}}), '')]

        def run(argv, **kw):
            r = replies.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        self.assertEqual(self.gh(run)._tree('h1'), 't9')
        self.assertEqual(self.slept, [2, 5])
        self.assertIsNone(gh_limit.latched())

    def test_a_real_error_is_not_retried(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return proc(argv, 1, '', 'HTTP 404: Not Found')
        self.assertEqual(self.gh(run)._tree('h1'), '')
        self.assertEqual((self.slept, len(calls)), ([], 1))

    def test_a_write_is_never_retried(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return proc(argv, 1, '', GATEWAY)
        with self.assertRaises(P.PortError):
            self.gh(run).update_branch(7)
        self.assertEqual((self.slept, len(calls)), ([], 1))

    def test_an_unreadable_merged_list_is_no_partial_fact(self):
        def run(argv, **kw):
            if '--state' in argv and argv[argv.index('--state') + 1] == 'merged':
                return proc(argv, 1, '', 'HTTP 500: Internal Server Error')
            return proc(argv, 0, '[]', '')
        with self.assertRaises(P.PortError):
            self.gh(run).prs()

    def test_the_knob_defaults_and_validates(self):
        self.assertEqual(settings.read(None)['github']['retry_delays_s'], [2, 5, 10])
        errors, _ = settings.problems({'github': {'retry_delays_s': ['soon']}})
        self.assertEqual([k for k, _ in errors], ['kernel.github.retry_delays_s'])


class BlindGitHub(F.FakeGitHub):

    def prs(self):
        raise P.PortError('open PRs unreadable: rc 1: ' + GATEWAY)


class Blind(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})
        self.lines = []

    def tick(self, ports, **kw):
        return loop.tick(self.product, ports=ports, config=B.config(**kw), state_dir=self.tmp,
                         out=self.lines.append)

    def test_unreadable_prs_skip_every_pr_action_but_end_crashed_sessions(self):
        rec = F.FakeRecord([B.task('T-0001'), B.task('T-0002', state=State.BUILDING),
                            B.task('T-0003', state=State.BUILDING)])
        sess = F.FakeSessions([B.session('j2', 'T-0002', alive=False, ended=False),
                               B.session('j3', 'T-0003', alive=False, ended=True,
                                         status='done')])
        gh = BlindGitHub()
        summary = self.tick(F.ports(record=rec, github=gh, sessions=sess))
        self.assertEqual(sess.launched, [], 'nothing launched on blind facts')
        self.assertEqual(gh.calls, [])
        self.assertEqual(sess.ended, [('j2', True)], 'a crash needs no PR fact')
        self.assertEqual(rec.fields['T-0002'], {P.ATTEMPTS: [CRASH]}, 'no state written')
        self.assertEqual((rec.fields['T-0001'], rec.fields['T-0003']), ({}, {}),
                         'an ended session waits for readable facts')
        self.assertIn('504', summary['blind'])
        self.assertEqual(summary['failed'], [])
        blind = [x for x in self.lines if 'GitHub unreadable' in x]
        self.assertEqual(len(blind), 1, self.lines)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, loop.PLAN_FILE)),
                         'the last good plan stands')

    def test_a_rate_limit_on_the_read_is_blind_too(self):
        class Limited(F.FakeGitHub):
            def prs(self):
                raise gh_limit.RateLimited('gh: GitHub rate limit')
        summary = self.tick(F.ports(record=F.FakeRecord([B.task('T-0001')]), github=Limited()))
        self.assertIn('rate limit', summary['blind'])

    def test_the_cli_exits_0_on_a_blind_tick(self):
        from asf import cli
        with mock.patch('asf.kernel.loop.tick', return_value={'blind': 'x', 'failed': []}):
            self.assertEqual(cli.main(['kernel', 'tick', '--product', 'sample']), 0)


if __name__ == '__main__':
    unittest.main()
