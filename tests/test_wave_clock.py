"""asf.tick.wave_clock — the wave's own clock (T-defect #54): a dedicated, short job that runs
the launch path alone, under its own lock, never the product's tick lock.

Shares its fixture (``StepsTestCase``: a real record origin seeded with one S1 bug card, and a
real product repo) with ``test_tick_steps.py``.
"""
import os
import time
import unittest
from unittest import mock

from asf import pause
from asf.feeder import rows as feeder_rows
from asf.tick import step_wave, tick, wave_clock
from asf.workers import pool as pool_mod

from tests.test_tick_steps import StepsTestCase, _brief


class ConfigTests(StepsTestCase):
    """``clocks.wave: off`` / ``clocks.wave.every`` — read fresh, never cached."""

    def test_off_by_default(self):
        self.assertFalse(wave_clock.off())

    def test_off_turns_it_off(self):
        self.write_config('clocks:\n  wave: off\n')
        self.assertTrue(wave_clock.off())

    def test_off_is_case_and_space_insensitive(self):
        self.write_config('clocks:\n  wave: " OFF "\n')
        self.assertTrue(wave_clock.off())

    def test_default_every_is_60s(self):
        self.assertEqual(wave_clock.every_s(), 60)

    def test_every_is_configurable(self):
        self.write_config('clocks:\n  wave:\n    every: 5m\n')
        self.assertEqual(wave_clock.every_s(), 300)

    def test_every_under_the_floor_is_clamped(self):
        self.write_config('clocks:\n  wave:\n    every: 10s\n')
        self.assertEqual(wave_clock.every_s(), wave_clock.MIN_EVERY_S)

    def test_a_bad_every_falls_back_to_the_default(self):
        self.write_config('clocks:\n  wave:\n    every: nonsense\n')
        self.assertEqual(wave_clock.every_s(), wave_clock.DEFAULT_EVERY_S)


class MarkerTests(StepsTestCase):
    """The marker the wave job leaves — read back by the product's own tick (:func:`ran_recently`,
    :func:`asf.tick.tick.wave_job_recent`) so it skips a launch the dedicated clock just made."""

    def test_no_marker_is_not_recent(self):
        self.assertIsNone(wave_clock.last_ran_at(self.product))
        self.assertFalse(wave_clock.ran_recently(self.product))

    def test_a_fresh_marker_is_recent(self):
        wave_clock.note_ran(self.product)
        self.assertTrue(wave_clock.ran_recently(self.product, within_s=60))

    def test_an_old_marker_is_not_recent(self):
        wave_clock.note_ran(self.product, now=time.time() - 3600)
        self.assertFalse(wave_clock.ran_recently(self.product, within_s=60))

    def test_tick_skips_its_own_wave_step_when_the_wave_job_just_ran(self):
        wave_clock.note_ran(self.product)
        self.assertTrue(tick.wave_job_recent(self.product))

    def test_tick_does_not_skip_when_clocks_wave_is_off_and_never_ran(self):
        self.write_config('clocks:\n  wave: off\n')
        self.assertFalse(tick.wave_job_recent(self.product))


class LockTests(StepsTestCase):
    def test_a_second_run_finds_the_lock_held(self):
        held = wave_clock.acquire_lock(self.product)
        self.addCleanup(held.close)
        self.assertIsNone(wave_clock.acquire_lock(self.product))

    def test_the_lock_is_its_own_never_the_tick_lock(self):
        self.assertNotEqual(wave_clock.lock_path(self.product), tick.lock_path(self.product))


class RunNoOpTests(StepsTestCase):
    def test_off_is_a_no_op_that_never_calls_launch_now(self):
        self.write_config('clocks:\n  wave: off\n')
        with mock.patch.object(step_wave, 'launch_now') as launch:
            rc = wave_clock.run(self.product, out=lambda _l: None)
        self.assertEqual(rc, 0)
        launch.assert_not_called()

    def test_a_held_lock_is_a_no_op_that_never_calls_launch_now(self):
        held = wave_clock.acquire_lock(self.product)
        self.addCleanup(held.close)
        with mock.patch.object(step_wave, 'launch_now') as launch:
            rc = wave_clock.run(self.product, out=lambda _l: None)
        self.assertEqual(rc, 0)
        launch.assert_not_called()

    def test_a_failure_is_caught_but_leaves_no_marker(self):
        """B-84831: a run that raises never notes itself as having run — a persistently
        failing dedicated clock would otherwise mark itself "recent" forever (the exception is
        caught before :func:`note_ran`), and the product's own tick (:func:`asf.tick.tick.
        wave_job_recent`) would skip its own wave step forever too, leaving a launchable row
        idle with a free seat past every watchdog limit. A transient failure instead falls back
        to the tick's own wave step as soon as this marker goes stale."""
        with mock.patch.object(step_wave, 'launch_now', side_effect=RuntimeError('boom')):
            lines = []
            rc = wave_clock.run(self.product, out=lines.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any('FAILED' in l for l in lines))
        self.assertIsNone(wave_clock.last_ran_at(self.product))

    def test_a_failure_lets_the_ticks_own_wave_step_take_over(self):
        """B-84831: :func:`asf.tick.tick.wave_job_recent` stops treating the dedicated clock as
        alive once it has stopped succeeding — so the product's own tick resumes launching."""
        with mock.patch.object(step_wave, 'launch_now', side_effect=RuntimeError('boom')):
            wave_clock.run(self.product, out=lambda _l: None)
        self.assertFalse(tick.wave_job_recent(self.product))


class OwnCloneTests(StepsTestCase):
    """The job reads its own clone (:func:`record_dir`), never the tick's own
    (``shadow.record_dir``) — a fetch, never shared, never pushed."""

    def test_record_root_is_its_own_directory_and_is_never_pushed(self):
        from asf.tick import shadow
        ctx = wave_clock._Context(self.product)
        root = ctx.record_root()
        self.assertEqual(root, wave_clock.record_dir(self.product))
        self.assertNotEqual(root, shadow.record_dir(self.product))
        self.assertTrue(os.path.isfile(os.path.join(root, 'index.json')))
        # a real clone of the same origin: the card the record step seeded is there
        self.assertTrue(os.path.isfile(os.path.join(root, 'bugs', 'B-0001.md')))

    def test_event_goes_nowhere_it_never_raises(self):
        ctx = wave_clock._Context(self.product)
        self.assertIsNone(ctx.event('launch', item='B-0001'))


class WaveJobLaunchTests(StepsTestCase):
    """:func:`asf.tick.step_wave.launch_now` — the launch path alone: plan, screen, spawn."""

    def setUp(self):
        super().setUp()
        self.push_branch('fix/B-0001')
        self.row = feeder_rows.Row(0, 'BUG → FIX', 'B-0001', '', 'would launch fix-b-0001 (Opus)',
                                   'fix-bug', 'fix/B-0001', 'S1 open')
        self.launches = []

        def build(product, row, index, inflight, repo_facts=None):
            return _brief('fix-bug', row.item_id)

        def plan(index, product, inflight, capacity, **_facts):
            # the real feeder's own contract: a row already live is not planned again — the
            # mechanism under test, not a shortcut this fake invents
            running_items = {s.get('item') for s in inflight}
            return [self.row] if self.row.item_id not in running_items else []

        def wave(product, rows, n, brief_fn=None, out=print, **_kw):
            launched = []
            for row in rows:
                rec = {'account': 'acct-a', 'model': 'opus', 'pid': 1}
                pool_mod.append_session(product, dict(rec, job=row.job, item=row.item,
                                                      kind=row.kind))
                self.launches.append(row.job)
                out(f'launched {row.job:<24} {row.item:<10} → acct-a (opus) pid 1')
                launched.append((row, rec))
            return launched, []

        for name, fn in (('_build', build), ('_wave', wave)):
            p = mock.patch.object(step_wave, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(feeder_rows, 'plan_rows', plan)
        p.start()
        self.addCleanup(p.stop)

    def _run_once(self):
        ctx = wave_clock._Context(self.product)
        lines = []
        rc = step_wave.launch_now(ctx, out=lines.append)
        return rc, lines

    def test_launches_the_ready_row(self):
        rc, lines = self._run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self.launches, ['fix-bug-b-0001'])
        self.assertTrue(any(l.startswith('launched fix-bug-b-0001') for l in lines), lines)

    def test_runs_while_a_fake_long_tick_holds_the_product_tick_lock(self):
        """A long tick's health/harvest/CI-log work never blocks the wave job: it never takes the
        product's own tick lock."""
        lock = tick.acquire_lock(self.product)
        self.assertIsNotNone(lock)
        self.addCleanup(lock.close)
        rc, lines = self._run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self.launches, ['fix-bug-b-0001'])

    def test_no_row_is_launched_twice_when_both_run(self):
        """Two runs (the dedicated clock's and, were it to overlap, the product's own tick) over
        the same ready row: the second sees the first's launch on the session ledger
        (:func:`asf.tick.step_wave.inflight`) and plans nothing — the existing occupancy/claim
        mechanism, not a new one."""
        rc1, lines1 = self._run_once()
        self.assertEqual(rc1, 0)
        self.assertEqual(self.launches, ['fix-bug-b-0001'])
        rc2, lines2 = self._run_once()
        self.assertEqual(rc2, 0)
        self.assertEqual(self.launches, ['fix-bug-b-0001'])  # not launched again
        self.assertIn('wave: nothing to launch', lines2)

    def test_a_paused_product_launches_nothing_on_the_wave_clock_s_own_path(self):
        """B-84833: the wave clock's :func:`step_wave.launch_now` reads the operator's own
        pause (F-0137) the same as :func:`step_wave.launch` — a seat free past the clock's
        tick is never a reason to launch a paused product's row."""
        pause.pause(self.product.name, 'release freeze', 'op1')
        rc, lines = self._run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self.launches, [])
        self.assertTrue(any(ln.startswith('wave:') and 'release freeze' in ln
                            for ln in lines), lines)

    def test_via_wave_clock_run_end_to_end(self):
        """The CLI's own path (``asf wave``): lock, launch, note the run."""
        lines = []
        rc = wave_clock.run(self.product, out=lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(self.launches, ['fix-bug-b-0001'])
        self.assertIsNotNone(wave_clock.last_ran_at(self.product))


class BreakerCooldownRereadTests(StepsTestCase):
    """Breaker/quota config is never cached: a cool-down edit takes effect on the very next wave
    job run, not the product's next long tick."""

    def test_cloud_settings_reread_every_call(self):
        self.write_config('cloud:\n  enabled: true\n  fallback_cooldown_min: 5\n')
        before = step_wave.cloud_settings(self.product)
        self.assertEqual(before.fallback_cooldown_min, 5)
        self.write_config('cloud:\n  enabled: true\n  fallback_cooldown_min: 45\n')
        after = step_wave.cloud_settings(self.product)
        self.assertEqual(after.fallback_cooldown_min, 45)

    def test_the_breaker_itself_is_built_fresh_off_the_reread_settings(self):
        from asf.workers import cloud as cloud_mod
        self.write_config('cloud:\n  enabled: true\n  default: true\n  fallback_cooldown_min: 1\n')
        cloud1 = step_wave.cloud_settings(self.product)
        breaker1 = cloud_mod.Breaker(self.product, cloud1)
        self.assertEqual(breaker1.s.fallback_cooldown_min, 1)
        self.write_config('cloud:\n  enabled: true\n  default: true\n  fallback_cooldown_min: 99\n')
        cloud2 = step_wave.cloud_settings(self.product)
        breaker2 = cloud_mod.Breaker(self.product, cloud2)
        self.assertEqual(breaker2.s.fallback_cooldown_min, 99)


if __name__ == '__main__':
    unittest.main()
