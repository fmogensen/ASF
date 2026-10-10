"""The kernel's GitHub budget (2026-10-10: the REST core limit ran out at 60 open PRs — every tick
re-read each head's tree and compare, each red run's attempt and log, and the branch refs).

A fact that never changes is read once and kept across ticks in ``kernel-gh-cache.json``: a
commit's tree, a head's change against trunk, a finished red job's attempt and failed log. The
pushed branches are read with ``git ls-remote`` (no API cost). Once per tick the core budget is
read off a real call's ``X-RateLimit-*`` headers (``gh api rate_limit`` was seen answering 5000
left while every call was refused); under ``kernel.github.slow_below`` of the limit the tick
slows its reads and says so. ``asf kernel status --live`` reuses a plan under one tick old."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest

from asf import env, github, mutation_guard
from asf.kernel import ports as P
from asf.kernel import status

HEADERS = ('HTTP/2.0 200 OK\nX-Ratelimit-Limit: 5000\nX-Ratelimit-Remaining: %d\n'
           'X-Ratelimit-Reset: 1791617678\nX-Ratelimit-Resource: core\n\n{"id": 1}')

LISTING = [{'number': 7, 'headRefName': 'worker/t-0001-slug', 'headRefOid': 'h1',
            'baseRefName': 'main', 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN',
            'files': [{'path': 'src/a.py'}],
            'statusCheckRollup': [
                {'__typename': 'CheckRun', 'name': 'tests', 'status': 'COMPLETED',
                 'conclusion': 'FAILURE', 'completedAt': '2026-10-10T07:00:00Z',
                 'detailsUrl': 'https://x/actions/runs/55/job/1'}]}]


def product(**kernel):
    return env.Product('sample', {'repo_slug': 'o/r', 'kernel': kernel} if kernel
                       else {'repo_slug': 'o/r'})


class Fake:
    """A ``gh`` that answers the tick's reads and counts every call by its first two words."""

    def __init__(self, remaining=4000):
        self.calls, self.remaining = [], remaining

    def __call__(self, argv, **kw):
        args = argv[1:]
        self.calls.append(args)
        if args[:2] == ['api', '-i']:
            return subprocess.CompletedProcess(argv, 0, HEADERS % self.remaining, '')
        if args[:4] == ['pr', 'list', '-R', 'o/r'] and 'open' in args:
            return subprocess.CompletedProcess(argv, 0, json.dumps(LISTING), '')
        if args[:2] == ['pr', 'list']:
            return subprocess.CompletedProcess(argv, 0, '[]', '')
        if args[:2] == ['api', 'repos/o/r/git/commits/h1']:
            return subprocess.CompletedProcess(argv, 0, json.dumps({'tree': {'sha': 't1'}}), '')
        if args[:2] == ['api', 'repos/o/r/compare/main...h1']:
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {'files': [{'filename': 'src/a.py', 'status': 'modified', 'patch': '+x'}]}), '')
        if args[:2] == ['api', 'repos/o/r/actions/runs/55']:
            return subprocess.CompletedProcess(argv, 0, json.dumps({'run_attempt': 2}), '')
        if args[:3] == ['run', 'view', '55']:
            return subprocess.CompletedProcess(argv, 0, 'FAIL: test_x (tests.test_a.Case)\n'
                                                        ' src/a.py:3\n', '')
        return subprocess.CompletedProcess(argv, 1, '', 'unexpected %s' % args)

    def count(self, prefix):
        return sum(1 for a in self.calls if a[:len(prefix)] == prefix)


class Cache(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'kernel-gh-cache.json')
        P.RealGitHub._changes.clear()

    def port(self, fake, **kw):
        return P.RealGitHub(product(**kw), run=fake, cache_path=self.path)

    def test_a_second_tick_reads_no_tree_compare_attempt_or_log_again(self):
        fake = Fake()
        first = self.port(fake).prs()[0]
        P.RealGitHub._changes.clear()  # a new tick is a new process
        second = self.port(fake).prs()[0]
        for prefix in (['api', 'repos/o/r/git/commits/h1'], ['api', 'repos/o/r/compare/main...h1'],
                       ['api', 'repos/o/r/actions/runs/55'], ['run', 'view', '55']):
            self.assertEqual(fake.count(prefix), 1, prefix)
        self.assertEqual((second.tree_sha, second.change_id), (first.tree_sha, first.change_id))
        c1, c2 = first.checks[0], second.checks[0]
        self.assertEqual((c2.attempt, c2.failing_files, c2.failed_step, c2.log_tail),
                         (c1.attempt, c1.failing_files, c1.failed_step, c1.log_tail))
        self.assertEqual((c2.attempt, c2.failing_files), (2, ['src/a.py', 'tests/test_a.py']))

    def test_a_rerun_job_is_a_new_fact(self):
        fake = Fake()
        self.port(fake).prs()
        LISTING[0]['statusCheckRollup'][0]['detailsUrl'] = 'https://x/actions/runs/55/job/2'
        try:
            self.port(fake).prs()
        finally:
            LISTING[0]['statusCheckRollup'][0]['detailsUrl'] = 'https://x/actions/runs/55/job/1'
        self.assertEqual(fake.count(['api', 'repos/o/r/actions/runs/55']), 2)
        self.assertEqual(fake.count(['api', 'repos/o/r/git/commits/h1']), 1)

    def test_a_dry_run_reads_the_cache_and_writes_none(self):
        fake = Fake()
        with mutation_guard.active(), contextlib.redirect_stdout(io.StringIO()):
            self.port(fake).prs()
        self.assertFalse(os.path.exists(self.path))

    def test_an_unreadable_cache_is_an_empty_one(self):
        with open(self.path, 'w') as f:
            f.write('{not json')
        fake = Fake()
        self.assertEqual(self.port(fake).prs()[0].tree_sha, 't1')
        with open(self.path) as f:
            self.assertEqual(json.load(f)['trees'], {'h1': 't1'})


class Budget(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        P.RealGitHub._changes.clear()

    def port(self, fake):
        gh = P.RealGitHub(product(), run=fake,
                          cache_path=os.path.join(self.tmp, 'kernel-gh-cache.json'))
        self.lines = []
        gh.log = self.lines.append
        return gh

    def test_the_budget_is_read_once_per_tick_off_a_real_calls_headers(self):
        fake = Fake(remaining=4000)
        gh = self.port(fake)
        gh.prs()
        gh.prs()
        self.assertEqual(fake.count(['api', '-i']), 1)
        self.assertEqual(gh.budget, (4000, 5000, 1791617678))
        self.assertFalse(gh.slow)
        self.assertEqual(self.lines, [])

    def test_under_fifteen_percent_the_tick_slows_its_reads_and_says_so(self):
        fake = Fake(remaining=600)
        gh = self.port(fake)
        pr = gh.prs()[0]
        self.assertTrue(gh.slow)
        self.assertEqual(pr.tree_sha, 't1')  # a verdict's key: still read
        self.assertEqual(fake.count(['api', 'repos/o/r/compare/main...h1']), 0)
        self.assertEqual(fake.count(['run', 'view']), 0)
        self.assertEqual(pr.change_id, '')
        self.assertEqual(len(self.lines), 1)
        self.assertIn('600/5000', self.lines[0])
        self.assertIn('slowed', self.lines[0])

    def test_the_threshold_is_a_kernel_setting(self):
        fake = Fake(remaining=600)
        gh = P.RealGitHub(product(github={'slow_below': 0.1}), run=fake,
                          cache_path=os.path.join(self.tmp, 'c.json'))
        gh.prs()
        self.assertFalse(gh.slow)

    def test_headers_parse(self):
        self.assertEqual(P.rate_headers(HEADERS % 12), (12, 5000, 1791617678))
        self.assertIsNone(P.rate_headers('{"id": 1}'))


class Branches(unittest.TestCase):

    def test_pushed_branches_come_from_ls_remote_not_the_api(self):
        repo = tempfile.mkdtemp()
        prod = env.Product('sample', {'repo_slug': 'o/r', 'repo_dir': repo})
        seen = []

        def git(args, cwd, **kw):
            seen.append(args)
            return github.Result(ok=True, rc=0, data='abc\trefs/heads/worker/T-0001\n'
                                                     'def\trefs/heads/worker/nope\n')
        gh = P.RealGitHub(prod, run=lambda *a, **k: self.fail('gh ran'), git=git)
        got = gh.branches()
        self.assertEqual([(b.name, b.item_id, b.head_sha) for b in got],
                         [('worker/T-0001', 'T-0001', 'abc')] * len(gh._prefixes()))
        self.assertEqual(seen[0][:2], ['ls-remote', 'origin'])

    def test_an_unreadable_ls_remote_falls_back_to_the_api(self):
        prod = env.Product('sample', {'repo_slug': 'o/r', 'repo_dir': tempfile.mkdtemp()})
        calls = []

        def run(argv, **kw):
            calls.append(argv[1:])
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                [{'ref': 'refs/heads/worker/T-0001', 'object': {'sha': 'abc'}}]), '')
        gh = P.RealGitHub(prod, run=run, git=lambda *a, **k: github.Result(ok=False, rc=128))
        self.assertTrue(gh.branches())
        self.assertTrue(all('matching-refs' in c[1] for c in calls))


class LiveStatus(unittest.TestCase):

    def plan(self, tmp, age_s):
        import datetime
        at = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=age_s)).strftime('%Y-%m-%dT%H:%M:%SZ')
        with open(os.path.join(tmp, 'kernel-plan.json'), 'w') as f:
            json.dump({'at': at, 'states': {}, 'sessions': []}, f)
        return at

    def test_live_reuses_a_plan_under_one_tick_old(self):
        from tests.kernel import fakes as F
        tmp = tempfile.mkdtemp()
        at = self.plan(tmp, 30)

        class NoGitHub:
            def prs(self):
                raise AssertionError('GitHub read')
        ports = F.ports()
        ports.github = NoGitHub()
        text = status.status(env.Product('sample', {}), ports=ports, out=lambda *_: None,
                             live=True, state_dir=tmp)
        self.assertIn(at, text)

    def test_live_reads_afresh_when_the_plan_is_older_than_a_tick(self):
        from tests.kernel import fakes as F
        from tests.kernel import builders as B
        tmp = tempfile.mkdtemp()
        at = self.plan(tmp, 600)
        text = status.status(env.Product('sample', {}), ports=F.ports(), config=B.config(),
                             out=lambda *_: None, live=True, state_dir=tmp)
        self.assertNotIn(at, text)


if __name__ == '__main__':
    unittest.main()
