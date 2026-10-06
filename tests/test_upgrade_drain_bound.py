"""A move's drain (``asf upgrade --product <p> --to <sha> --wait-s N``) ends within ``--wait-s``
plus :data:`asf.upgrade.DRAIN_MARGIN_S` on every path, and its marker never outlives it.

Defect: a move with ``--wait-s 300`` held ``state/<p>/upgrade-draining.json`` for 20+ minutes
until it was interrupted — the bound counted only the seconds asked of ``sleep``, so the time a
poll's ``pgrep``/``ps`` took, a sleep that overslept, and the pause/resume of every re-cut
cycle were free; the marker was held through the switch as well, and the marker blocked the
product's launches, not only its new merge-queue cuts.

Hermetic: a fake clock replaces ``time`` inside :mod:`asf.upgrade`; the run, the clock and hook
operations are the fakes of :mod:`tests.test_upgrade`."""
import json
import os
import shutil
import signal
import tempfile
import time
import types
import unittest
from unittest import mock

from asf import env, merge_queue, upgrade
from tests import test_merge_queue, test_tick, test_upgrade

SHA = test_upgrade.SHA
FakeRun = test_upgrade.FakeRun
FakeOps = test_upgrade.FakeOps
#: the margin the bound allows — a literal, so a build without the constant fails on the bound
MARGIN = 60


class Clock:
    """``time.time`` and ``time.monotonic`` alike, moved only by ``sleep`` and ``advance``."""

    def __init__(self, t=1_800_000_000.0):
        self.t = self.start = t

    def time(self):
        return self.t

    monotonic = time

    def sleep(self, s):
        self.t += s

    def advance(self, s):
        self.t += s

    def elapsed(self):
        return self.t - self.start


class FakeClockCase(test_upgrade.MoveCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        fake = types.SimpleNamespace(time=self.clock.time, monotonic=self.clock.monotonic,
                                     sleep=self.clock.sleep, strftime=time.strftime,
                                     localtime=time.localtime)
        p = mock.patch.object(upgrade, 'time', fake)
        p.start()
        self.addCleanup(p.stop)

    def write_batch(self, ref='batch/1'):
        os.makedirs(self.state(), exist_ok=True)
        with open(self.state('merge-queue.json'), 'w', encoding='utf-8') as f:
            json.dump({'batches': [{'ref': ref, 'sha': 'c' * 40, 'members': []}] if ref else []}, f)

    def busy_run(self, cost=0):
        """A run whose floor never goes quiet (a tick of alpha runs); each pgrep/ps costs
        ``cost`` seconds of the fake clock."""
        run = FakeRun(self.venvs, procs=['73\n'])
        run.listing = lambda: [(73, 'tick --product alpha --steps record,wave')]
        clock, inner = self.clock, run.__call__
        self.timeouts = []

        def call(cmd, **kw):
            if cmd[:1] in (['pgrep'], ['ps']):
                self.timeouts.append(kw.get('timeout'))
                clock.advance(cost)
            return inner(cmd, **kw)
        return call

    def assert_bounded_and_cleared(self, rc, wait_s, out=()):
        self.assertEqual(rc, upgrade.MOVE_DEFERRED, out)
        self.assertLessEqual(self.clock.elapsed(), wait_s + MARGIN, out)
        self.assertFalse(os.path.exists(upgrade.draining_path('alpha')))
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))


class WaitPathsTest(FakeClockCase):
    """One test per wait of the drain: each ends within the budget and clears the marker."""

    def test_the_floor_wait_counts_the_time_its_polls_take(self):
        """pgrep and ps each cost 10s: 60 polls of 5s sleep used to be 45 minutes."""
        ops = FakeOps()
        rc = upgrade.move('alpha', to=SHA, run=self.busy_run(cost=10), out=(out := []).append,
                          sleep=self.clock.sleep, ops=ops, wait_s=300)
        self.assert_bounded_and_cleared(rc, 300, out)
        self.assertEqual([e[0] for e in ops.log], ['pause', 'resume'])
        # each poll's subprocess gets at most what is left of the budget (never its own 10s)
        self.assertTrue(all(t is not None and t <= 10 for t in self.timeouts), self.timeouts)
        self.assertLessEqual(self.timeouts[-1], 1)

    def test_the_in_flight_wait_counts_a_sleep_that_oversleeps(self):
        """The batch in flight never lands, and every sleep takes four times what it asked."""
        self.write_batch()
        rc = upgrade.move('alpha', to=SHA, run=FakeRun(self.venvs), out=(out := []).append,
                          sleep=lambda s: self.clock.advance(4 * s), ops=FakeOps(), wait_s=300)
        self.assert_bounded_and_cleared(rc, 300, out)

    def test_the_recut_cycle_counts_its_pause_and_resume(self):
        """A batch is cut each time the floor quiets and lands each time the clocks resume: every
        cycle's pause and resume (launchctl) cost 20s, which the bound used to leave out."""
        clock = self.clock

        class SlowOps(FakeOps):
            def pause(inner, product, clocks, by):
                clock.advance(20)
                return super().pause(product, clocks, by)

            def resume(inner, product, clocks):
                clock.advance(20)
                return super().resume(product, clocks)
        naps = []

        def sleep(s):
            naps.append(s)
            clock.advance(s)
            self.write_batch('batch/1' if len(naps) % 2 else None)
        rc = upgrade.move('alpha', to=SHA, run=self.busy_run(), out=(out := []).append,
                          sleep=sleep, ops=SlowOps(), wait_s=60)
        self.assert_bounded_and_cleared(rc, 60, out)

    def test_a_held_harvest_lock_wait_ends_within_the_budget(self):
        import fcntl
        os.makedirs(self.state(), exist_ok=True)
        lock = open(self.state('harvest.lock'), 'a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = self.busy_run(cost=10)
        rc = upgrade.move('alpha', to=SHA, run=run, out=(out := []).append,
                          sleep=lambda s: self.clock.advance(2 * s), ops=FakeOps(), wait_s=120)
        self.assert_bounded_and_cleared(rc, 120, out)
        self.assertIn('harvest.lock is held', '\n'.join(out))

    def test_the_marker_records_its_deadline(self):
        self.write_batch()
        seen = []

        def sleep(s):
            seen.append(upgrade.draining('alpha'))
            self.clock.advance(s)
            self.write_batch(None)
        start = self.clock.time()
        rc = upgrade.move('alpha', to=SHA, run=FakeRun(self.venvs), out=(out := []).append,
                          sleep=sleep, ops=FakeOps(), wait_s=300)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen[0]['deadline'], start + 300)

    def test_the_marker_is_gone_once_the_drain_ends_and_the_switch_starts(self):
        """The switch (clocks, hooks, smoke) may take minutes: the drain marker is not held
        through it — the move's own hold and the paused clocks keep the floor quiet."""
        seen = []

        class Ops(FakeOps):
            def install_clocks(inner, product):
                seen.append(('clocks', upgrade.draining(product)))
                return super().install_clocks(product)

            def smoke(inner, product):
                seen.append(('smoke', upgrade.draining(product)))
                return super().smoke(product)
        rc = upgrade.move('alpha', to=SHA, run=FakeRun(self.venvs), out=(out := []).append,
                          sleep=self.clock.sleep, ops=Ops(), wait_s=300)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [('clocks', None), ('smoke', None)])


class OperatorWaitTest(FakeClockCase):
    """``asf upgrade --wait N`` (:func:`asf.upgrade.drain`): the other processes never end and
    each sleep oversleeps fourfold — the wait still ends within ``N`` plus the margin."""

    def test_the_operator_wait_counts_a_sleep_that_oversleeps(self):
        with mock.patch.object(upgrade, 'other_ticks', return_value=[73]), \
                mock.patch.object(upgrade, 'describe', return_value=['73']):
            left = upgrade.drain([73], 100, run=FakeRun(self.venvs), out=lambda _l: None,
                                 sleep=lambda s: self.clock.advance(4 * s))
        self.assertEqual(left, [73])
        self.assertLessEqual(self.clock.elapsed(), 100 + MARGIN)


class InterruptedDrainTest(FakeClockCase):
    """However the drain ends — a signal, Ctrl-C, an exception — the marker goes with it."""

    def interrupted(self, raise_it, expected):
        self.write_batch()
        with self.assertRaises(expected):
            upgrade.move('alpha', to=SHA, run=FakeRun(self.venvs), out=lambda _l: None,
                         sleep=raise_it, ops=FakeOps(), wait_s=300)
        self.assertFalse(os.path.exists(upgrade.draining_path('alpha')))
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))

    def test_sigterm(self):
        def sleep(_s):
            os.kill(os.getpid(), signal.SIGTERM)   # the move's handler raises SystemExit
            time.sleep(0.5)                        # the handler runs before this returns
        self.interrupted(sleep, SystemExit)

    def test_sigint(self):
        def sleep(_s):
            raise KeyboardInterrupt
        self.interrupted(sleep, KeyboardInterrupt)

    def test_an_exception(self):
        def sleep(_s):
            raise RuntimeError('boom')
        self.interrupted(sleep, RuntimeError)


class StaleMarkerTest(FakeClockCase):
    """A marker past its deadline (plus the margin) is ignored and removed by every reader,
    though its process lives on — a hung move never holds the product."""

    def mark(self, **data):
        upgrade._write_json(upgrade.draining_path('alpha'),
                            dict({'sha': SHA, 'pid': os.getpid()}, **data))

    def test_a_marker_past_its_deadline_is_dropped(self):
        now = self.clock.time()
        self.mark(at=now - 400, deadline=now - MARGIN - 1)
        self.assertIsNone(upgrade.draining('alpha'))
        self.assertFalse(os.path.exists(upgrade.draining_path('alpha')))

    def test_a_marker_within_its_margin_holds(self):
        now = self.clock.time()
        self.mark(at=now - 300, deadline=now - 10)
        self.assertIsNotNone(upgrade.draining('alpha'))

    def test_an_older_builds_marker_expires_after_the_default_bound(self):
        now = self.clock.time()
        self.mark(at=now - 60)
        self.assertIsNotNone(upgrade.draining('alpha'))
        self.mark(at=now - upgrade.DEFAULT_DRAIN_MARKER_MAX_S - 1)
        self.assertIsNone(upgrade.draining('alpha'))
        self.assertFalse(os.path.exists(upgrade.draining_path('alpha')))

    def test_the_default_bound_is_a_config_key(self):
        with open(env.config_path(), 'w', encoding='utf-8') as f:
            f.write('upgrade:\n  drain_marker_max_s: 30\n')
        now = self.clock.time()
        self.mark(at=now - 31)
        self.assertIsNone(upgrade.draining('alpha'))
        from asf import config_keys
        self.assertTrue(config_keys._known('upgrade.drain_marker_max_s', config_keys.KNOWN_CONFIG_KEYS))


class TickDrainTest(test_tick.TickTestCase):
    """A live drain never blocks the tick's launches; the tick reaps a stale marker."""
    product_yaml = ('steps:\n'
                    '  health: python3 -c \'print("health ran")\'\n'
                    '  wave: python3 -c \'print("wave launched")\'\n'
                    '  prs: off\n'
                    '  batch: off\n')

    def mark(self, **data):
        upgrade._write_json(upgrade.draining_path('sample'),
                            dict({'sha': 'f' * 40, 'pid': os.getpid()}, **data))
        self.addCleanup(upgrade.clear_draining, 'sample')

    def test_a_live_drain_launches_the_wave(self):
        self.mark(at=time.time(), deadline=time.time() + 300)
        rc, out = self.run_tick(steps='health,wave')
        self.assertEqual(rc, 0, out)
        self.assertIn('health ran', out)
        self.assertIn('wave launched', out)
        self.assertTrue(os.path.exists(upgrade.draining_path('sample')))

    def test_the_tick_removes_a_stale_marker(self):
        self.mark(at=time.time() - 4000, deadline=time.time() - 3000)
        rc, out = self.run_tick(steps='health')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(upgrade.draining_path('sample')))


class MergeQueueDrainTest(test_merge_queue.QueueRepo):
    """A live drain holds new batch cuts; a stale one holds nothing and is removed."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        home = tempfile.mkdtemp(prefix='drain_bound_home_')
        self.addCleanup(shutil.rmtree, home, True)
        p = mock.patch.object(env, 'ASF_HOME', home)
        p.start()
        self.addCleanup(p.stop)

    def mark(self, **data):
        upgrade._write_json(upgrade.draining_path('sample'),
                            dict({'sha': 'f' * 40, 'pid': os.getpid()}, **data))

    def ready(self):
        f = self.entry('worker/T-0001', 1)
        f['green'] = {'head': f['head'], 'trunk': 'x' * 40}
        return [f]

    def test_a_live_drain_cuts_no_batch(self):
        self.mark(at=time.time(), deadline=time.time() + 300)
        self.queue_pass(self.lane(), self.ready())
        self.assertEqual(merge_queue.load(self.state_dir)['batches'], [])

    def test_a_stale_drain_is_removed_and_the_batch_is_cut(self):
        self.mark(at=time.time() - 4000, deadline=time.time() - 3000)
        self.queue_pass(self.lane(), self.ready())
        self.assertEqual(len(merge_queue.load(self.state_dir)['batches']), 1)
        self.assertFalse(os.path.exists(upgrade.draining_path('sample')))


if __name__ == '__main__':
    unittest.main()
