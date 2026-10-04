"""asf.tick.watchdog — a tick past its wall-clock budget names its step, exits, and frees the lock.

2026-10-04: a product's tick spun at 100% CPU in its wave step for 23+ minutes holding
``tick.lock``, and no later tick of the product started."""
import fcntl
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from asf.tick import watchdog

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BudgetTests(unittest.TestCase):

    def test_three_intervals_floored_at_an_hour(self):
        self.assertEqual(watchdog.seconds_for(None, ['wave'], interval_s=300), watchdog.FLOOR_S)
        self.assertEqual(watchdog.seconds_for(None, ['wave'], interval_s=1800), 5400)

    def test_no_interval_is_the_default(self):
        self.assertEqual(watchdog.seconds_for(None, ['daily']), watchdog.DEFAULT_S)

    def test_the_config_wins_and_zero_is_off(self):
        self.assertEqual(watchdog.seconds_for(None, ['wave'], budget_s=90, interval_s=300), 90)
        self.assertIsNone(watchdog.arm(0))

    def test_configured_reads_tick_budget_s(self):
        with mock.patch.object(watchdog.env, 'load_config', return_value={'tick': {'budget_s': 42}}):
            self.assertEqual(watchdog.configured(), 42)
        with mock.patch.object(watchdog.env, 'load_config', return_value={}):
            self.assertIsNone(watchdog.configured())
        with mock.patch.object(watchdog.env, 'load_config', return_value={'tick': {'budget_s': 'x'}}):
            self.assertIsNone(watchdog.configured())

    def test_the_clock_interval_is_the_one_running_these_steps(self):
        from asf import scheduler
        clocks = [scheduler.Clock('tick', ['record', 'wave'], False, 300, None),
                  scheduler.Clock('batch', ['batch'], False, 600, None)]
        with mock.patch.object(scheduler, 'clocks', return_value=clocks):
            self.assertEqual(watchdog.clock_interval(None, ['record', 'wave']), 300)
            self.assertEqual(watchdog.clock_interval(None, ['batch']), 600)
            self.assertIsNone(watchdog.clock_interval(None, ['daily']))

    def test_the_line_names_the_step(self):
        watchdog.enter('wave')
        self.assertIn('in step wave', watchdog.line(900))

    def test_expiry_calls_exit_with_124(self):
        codes = []
        with mock.patch('sys.stdout'), mock.patch('sys.stderr'):
            watchdog._expire(5, codes.append)
        self.assertEqual(codes, [watchdog.EXIT_CODE])


class SpinningTickTests(unittest.TestCase):
    """A process holding the tick lock and spinning forever is ended by its budget, and the lock
    is free for the next tick — the real ``os._exit``, in a child process."""

    def test_a_spinning_tick_exits_and_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, 'tick.lock')
            child = textwrap.dedent(f"""
                import fcntl
                from asf.tick import watchdog
                f = open({lock!r}, 'a')
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                watchdog.arm(1)
                watchdog.enter('wave')
                n = 0
                while True:   # a step that never ends
                    n += 1
            """)
            env = dict(os.environ, PYTHONPATH=REPO)
            p = subprocess.run([sys.executable, '-c', child], env=env, capture_output=True,
                               text=True, timeout=60)
            self.assertEqual(p.returncode, watchdog.EXIT_CODE, p.stderr)
            self.assertIn('over its wall-clock budget (1s) in step wave', p.stdout)
            with open(lock, 'a') as f:   # the next tick takes the lock at once
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
