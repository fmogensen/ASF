"""Nothing slow in the lane runs inside the tick: the pre-push check on a head the lane rebuilt
runs as a background job whose result a later pass reads (``lane.rebuild_check``), one lane pass
spends at most ``lane.pass_budget_s`` advancing branches, and the merge queue judges its batches
in the queue job every minute, not only in a harvest a main tick starts. 2026-10-06: one product
tick ran 57 min (13+ inside one synchronous check), another 60+ (checks one after another), and
a green batch waited for the next tick to land."""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from asf import ci_queue, conventions, env, merge_queue, scheduler
from asf.harvest import harvest as H
from asf.harvest import lane as lane_mod
from asf.harvest import rebuild_check


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(env, 'ASF_HOME', self.tmp)
        p.start()
        self.addCleanup(p.stop)


class RepoCase(HomeCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        _git(['init', '-q', '-b', 'main'], self.repo)
        _git(['config', 'user.email', 't@example.com'], self.repo)
        _git(['config', 'user.name', 't'], self.repo)
        with open(os.path.join(self.repo, 'a.txt'), 'w') as f:
            f.write('a\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'one'], self.repo)
        self.sha = _git(['rev-parse', 'HEAD'], self.repo)
        self.product = env.Product('p', {'repo_dir': self.repo,
                                         'conventions': {'pre_push_check': 'true'}})
        self.state = env.state_dir(self.product)
        os.makedirs(self.state, exist_ok=True)


class BackgroundCheckTests(RepoCase):
    def test_the_pass_returns_while_the_check_runs_and_a_later_pass_reads_its_result(self):
        started = threading.Event()
        release = threading.Event()
        threads = []

        def slow_check(repo, state_dir, sha, command, setup=None):
            started.set()
            release.wait(10)  # the product's gate, still running
            return True, ''

        def spawn(product, sha, k, state_dir=None):
            t = threading.Thread(target=rebuild_check.run_job, args=(product, sha, k),
                                 kwargs={'out': lambda _l: None, 'state_dir': state_dir})
            threads.append(t)
            t.start()
            return 4242
        with mock.patch.object(lane_mod, 'pre_push_check_at', slow_check):
            t0 = time.monotonic()
            ok, line = rebuild_check.check(self.product, self.repo, self.state, self.sha, 'true',
                                           spawn_fn=spawn)
            self.assertIsNone(ok)
            self.assertIn('started in the background', line)
            self.assertLess(time.monotonic() - t0, 2)   # the pass never waits on the check
            self.assertTrue(started.wait(5))
            # a pass while the job still runs: told so, nothing started twice
            ok, line = rebuild_check.check(self.product, self.repo, self.state, self.sha, 'true',
                                           spawn_fn=spawn)
            self.assertIsNone(ok)
            self.assertIn('runs in the background on this head', line)
            self.assertEqual(len(threads), 1)
            release.set()
            threads[0].join(10)
        ok, line = rebuild_check.check(self.product, self.repo, self.state, self.sha, 'true',
                                       spawn_fn=spawn)
        self.assertEqual((ok, line), (True, ''))
        self.assertEqual(len(threads), 1)

    def test_the_result_follows_the_content_not_the_commit(self):
        k = rebuild_check.key(self.repo, self.sha, 'true')
        rebuild_check.write_result(self.state, k, False, 'red: test_x', self.sha)
        # the lane rebuilds the branch every pass: a new commit, the same tree
        _git(['commit', '-q', '--amend', '-m', 'one, rebuilt'], self.repo)
        rebuilt = _git(['rev-parse', 'HEAD'], self.repo)
        self.assertNotEqual(rebuilt, self.sha)
        got = rebuild_check.check(self.product, self.repo, self.state, rebuilt, 'true',
                                  spawn_fn=lambda *a: self.fail('started again'))
        self.assertEqual(got, (False, 'red: test_x'))
        # another command is another check
        self.assertNotEqual(rebuild_check.key(self.repo, self.sha, 'make check'), k)

    def test_a_dry_run_starts_nothing(self):
        ok, line = rebuild_check.check(self.product, self.repo, self.state, self.sha, 'true',
                                       spawn_fn=lambda *a: self.fail('spawned'), dry_run=True)
        self.assertIsNone(ok)
        self.assertIn('DRY', line)

    def test_the_job_writes_a_failed_checks_output(self):
        k = rebuild_check.key(self.repo, self.sha, 'true')
        product = env.Product('p', {'repo_dir': self.repo,
                                    'conventions': {'pre_push_check': 'echo broken; exit 3'}})
        rebuild_check.run_job(product, self.sha, k, out=lambda _l: None)
        got = rebuild_check.read_result(self.state, k)
        self.assertFalse(got['ok'])
        self.assertIn('exit 3', got['line'])
        self.assertIn('broken', got['line'])


class LaneModeTests(RepoCase):
    def lane(self, mode):
        product = env.Product('p', {'repo_dir': self.repo, 'conventions': {
            'pre_push_check': 'true', 'lane': {'rebuild_check': mode}}})
        return types.SimpleNamespace(product=product, conv=product.conventions, repo=self.repo,
                                     state_dir=self.state, dry_run=False)

    def test_background_never_runs_the_check_in_the_pass(self):
        with mock.patch.object(lane_mod, 'pre_push_check_at',
                               side_effect=AssertionError('ran in the pass')), \
                mock.patch.object(rebuild_check, 'spawn', return_value=7):
            ok, line = lane_mod.Lane.pre_push_ok(self.lane('background'), self.sha)
        self.assertIsNone(ok)
        self.assertIn('background', line)

    def test_session_hands_it_back_and_off_pushes_unchecked(self):
        with mock.patch.object(lane_mod, 'pre_push_check_at',
                               side_effect=AssertionError('ran in the pass')):
            ok, line = lane_mod.Lane.pre_push_ok(self.lane('session'), self.sha)
            self.assertFalse(ok)
            self.assertIn('the session runs it', line)
            self.assertEqual(lane_mod.Lane.pre_push_ok(self.lane('off'), self.sha), (True, ''))

    def test_the_mode_is_validated_and_defaults_to_background(self):
        self.assertEqual(env.Product('p', {}).conventions.rebuild_check(), 'background')
        problems = conventions.validate_mapping({'lane': {'rebuild_check': 'inline',
                                                  'pass_budget_s': -1}})
        keys = [k for k, _w in problems]
        self.assertIn('lane.rebuild_check', keys)
        self.assertIn('lane.pass_budget_s', keys)

    def test_a_deferred_rebuild_waits_and_no_session_gets_a_round(self):
        f = {'branch': 'feature/T-0001', 'run': {'job': 'coder-t-0001'}, 'class': 'code'}
        lane = types.SimpleNamespace(repo=self.repo, dry_run=False, product=self.product,
                                     rebase_onto_trunk=lambda f: {'deferred': 'runs'})
        with mock.patch.object(lane_mod, 'wait') as wait, \
                mock.patch.object(lane_mod, 'hold_with_correction',
                                  side_effect=AssertionError('a session round')):
            got = lane_mod.send_back(lane, f, 'conflict', 'conflicts with main', ['a.txt'])
        self.assertEqual(got, 'waiting')
        wait.assert_called_once_with(lane, f, lane_mod.REBUILD_CHECK_WAIT)


class PassBudgetTests(HomeCase):
    def test_a_pass_stops_at_its_budget_and_names_what_waits(self):
        product = env.Product('p', {'repo_dir': self.tmp,
                                    'conventions': {'lane': {'pass_budget_s': 60}}})
        clock = [0.0]
        advanced, lines = [], []

        class FakeLane:
            repo = self.tmp
            items = None
            dry_run = False
            results = {}
            conv = product.conventions
            out = staticmethod(lines.append)

            def gather(self, prs=True):
                return {b: {'branch': b} for b in ('a', 'b', 'c', 'd')}

            def advance(self, f):
                advanced.append(f['branch'])
                clock[0] += 40   # a branch's work: two fit in 60 s

            def finish_ref_pushes(self):
                pass
        with mock.patch.object(lane_mod.time, 'monotonic', lambda: clock[0]), \
                mock.patch.object(H, 'sh'), \
                mock.patch.object(ci_queue, 'queue_pass', return_value={}):
            lane_mod.lane_pass(product, lane=FakeLane())
        self.assertEqual(advanced, ['a', 'b'])
        self.assertIn('lane: pass budget 60s spent — 2 branch(es) wait for the next pass (c, d)',
                      lines)

    def test_the_default_budget_and_none(self):
        self.assertEqual(env.Product('p', {}).conventions.pass_budget_s(), 300)
        self.assertEqual(env.Product('p', {'conventions': {
            'lane': {'pass_budget_s': 0}}}).conventions.pass_budget_s(), 0)


class MergeQueueOwnCadenceTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.product = env.Product('p', {'repo_dir': self.tmp, 'repo_slug': 'o/r',
                                         'conventions': {'merge': 'queue'}})
        os.makedirs(env.state_dir(self.product), exist_ok=True)

    def fake_lane(self):
        class FakeLane:
            def __init__(s, *a, **k):
                pass

            def finish_ref_pushes(s):
                pass
        return FakeLane

    def test_a_green_batch_lands_from_the_queue_job_while_the_main_tick_is_busy(self):
        from asf.tick import tick
        busy = tick.acquire_lock(self.product)   # a main tick holds the product for an hour
        self.addCleanup(busy.close)
        ran = []
        with mock.patch.object(merge_queue, 'run', lambda lane, ready: ran.append(ready)), \
                mock.patch.object(lane_mod, 'merge_queued', return_value=True), \
                mock.patch.object(lane_mod, 'Lane', self.fake_lane()), \
                mock.patch.object(ci_queue, 'mode', return_value='off'):
            self.assertEqual(ci_queue.apply(self.product, out=lambda _l: None), 0)
        self.assertEqual(ran, [[]])   # the batches in flight judged: the green one lands

    def test_a_running_harvest_keeps_the_pass_and_the_queue_job_steps_aside(self):
        lock = H.try_lock(os.path.abspath(env.state_dir(self.product)))
        self.addCleanup(lock.close)
        lines = []
        with mock.patch.object(merge_queue, 'run', side_effect=AssertionError('raced')):
            self.assertIsNone(merge_queue.own_pass(self.product, out=lines.append))
        self.assertIn('merge queue: a harvest holds the lock — its gate pass judges the batches',
                      lines)

    def test_no_merge_queue_no_pass_and_the_scheduler_installs_the_job(self):
        plain = env.Product('p', {'repo_dir': self.tmp})
        self.assertIsNone(merge_queue.own_pass(plain, out=lambda _l: None))
        product = env.Product('p', {'repo_dir': self.tmp, 'conventions': {'merge': 'queue'},
                                    'clocks': {'main': {'steps': ['record'], 'every': '5m'}}})
        with mock.patch.object(ci_queue, 'mode', return_value='off'):
            names = [c.name for c in scheduler.clocks(product)]
        self.assertIn(scheduler.QUEUE_CLOCK, names)


if __name__ == '__main__':
    unittest.main()
