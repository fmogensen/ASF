"""asf.drift — B-0086: the factory names the version of itself it is running, and the trunk's."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import __version__, doctor, drift, env
from asf.tick import tick


def git(cwd, *argv):
    return subprocess.run(['git', '-C', cwd, *argv], check=True, capture_output=True,
                          text=True).stdout.strip()


class DriftTestCase(unittest.TestCase):
    """A product repo that is the factory's own source: three commits, the first the installed one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='drift_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(self.repo, 'asf'))
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@example.com')
        git(self.repo, 'config', 'user.name', 't')
        self.installed = self.commit('pyproject.toml', '[project]\nname = "asf-factory"\n', 'first')
        self.commit('asf/__init__.py', f'__version__ = "{__version__}"\n', 'the package')
        self.head = self.commit('asf/cli.py', 'x = 2\n', 'a fix')
        self.product = env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                         'ci': {'provider': 'none'}})
        from asf import upgrade
        self.clear_last = lambda: os.path.exists(upgrade.last_path()) and os.remove(upgrade.last_path())
        self.clear_last()
        self.addCleanup(self.clear_last)

    def commit(self, path, text, message):
        with open(os.path.join(self.repo, path), 'w') as f:
            f.write(text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', message)
        return git(self.repo, 'rev-parse', 'HEAD')

    def behind(self):
        return mock.patch.object(drift, 'installed_commit', return_value=self.installed)


class TickPrintsTheDriftLine(DriftTestCase):
    def run_tick(self, level='human-now', upgrade=None):
        out = io.StringIO()
        self.product = env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                         'ci': {'provider': 'none'}, 'approvals': {'upgrade': level}})
        ctx = tick.Context(self.product)
        with self.behind(), contextlib.redirect_stdout(out), \
                mock.patch('asf.tick.summary.run'), \
                mock.patch('asf.upgrade.cmd_upgrade', upgrade or mock.Mock(return_value=0)):
            tick._run_steps(mock.Mock(), self.product, ctx, [], None)
        return out.getvalue().splitlines()

    def test_a_tick_whose_installed_package_is_behind_prints_the_drift_line(self):
        lines = self.run_tick()
        self.assertEqual(lines[0],
                         f'factory: asf {__version__} @ {self.installed[:7]} · '
                         f'trunk {self.head[:7]} · BEHIND by 2 commits')

    def test_a_change_to_the_package_proposes_its_own_upgrade(self):
        lines = self.run_tick()
        self.assertIn(f'UPGRADE AVAILABLE {__version__}@{self.installed[:7]} → '
                      f'{__version__}@{self.head[:7]}', lines)

    def test_it_stays_a_proposal_when_the_level_holds_it(self):
        upgrade = mock.Mock(return_value=0)
        self.run_tick('human-now', upgrade)
        upgrade.assert_not_called()

    def test_under_auto_it_runs_asf_upgrade_and_says_so(self):
        upgrade = mock.Mock(return_value=0)
        lines = self.run_tick('auto', upgrade)
        upgrade.assert_called_once()
        self.assertTrue(any(ln.startswith('tick: ran asf upgrade') for ln in lines), lines)

    def test_it_installs_the_head_it_read_and_the_steps_wait_for_the_next_tick(self):
        upgrade = mock.Mock(return_value=0)
        lines = self.run_tick('auto', upgrade)
        self.assertEqual(upgrade.call_args[0][0].ref, self.head)
        self.assertIn('tick: the install was upgraded under this tick; its steps run on the next tick',
                      lines)

    def test_a_deferred_upgrade_does_not_claim_it_ran(self):
        lines = self.run_tick('auto', mock.Mock(return_value=drift.DEFERRED))
        self.assertFalse(any(ln.startswith('tick: ran asf upgrade') for ln in lines), lines)
        self.assertFalse(any('upgraded under this tick' in ln for ln in lines), lines)

    def test_the_owner_tick_whose_upgrade_is_pending_still_runs_its_steps(self):
        from asf import upgrade

        def deferred(args):
            self.assertEqual(args.owner, 'p')
            upgrade.write_pending(args.ref, args.owner)
            return drift.DEFERRED
        self.addCleanup(upgrade.clear_pending)
        with mock.patch.object(tick, 'run_asf_step') as step:
            out = io.StringIO()
            self.product = env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                             'ci': {'provider': 'none'}, 'approvals': {'upgrade': 'auto'}})
            with self.behind(), contextlib.redirect_stdout(out), mock.patch('asf.tick.summary.run'), \
                    mock.patch('asf.upgrade.cmd_upgrade', deferred):
                rc = tick._run_steps(mock.Mock(), self.product, tick.Context(self.product),
                                     [('harvest', 'asf', None)], None)
        step.assert_called()   # the owner keeps its product moving; others wait for the gap
        self.assertIn(f'tick: upgrade to {self.head[:7]} pending — this tick runs', out.getvalue())

    def test_the_owner_tick_drains_a_live_background_harvest_then_installs(self):
        """A background harvest from the owner's last tick is still running at its start: the
        tick marks the upgrade pending, polls within upgrade.drain_wait_s, and installs once it
        ends — never deferred forever behind its own product's harvest."""
        from asf import upgrade
        self.addCleanup(upgrade.clear_pending)
        polls = iter([[54171], [54171], []])
        installed = []
        self.product = env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                         'ci': {'provider': 'none'}, 'approvals': {'upgrade': 'auto'}})
        out = io.StringIO()
        with self.behind(), contextlib.redirect_stdout(out), mock.patch('asf.tick.summary.run'), \
                mock.patch.object(upgrade, 'other_ticks', side_effect=lambda *a, **k: next(polls)), \
                mock.patch.object(upgrade, 'describe', side_effect=lambda pids, run=None: [str(p) for p in pids]), \
                mock.patch.object(upgrade, 'drain_wait_s', return_value=180), \
                mock.patch.object(upgrade, '_install',
                                  side_effect=lambda ref, run, out: installed.append(ref) or 0), \
                mock.patch.object(upgrade, '_drain_sleep') as sleep, \
                mock.patch.object(tick, 'run_asf_step') as step:
            tick._run_steps(mock.Mock(), self.product, tick.Context(self.product),
                            [('harvest', 'asf', None)], None)
        self.assertEqual(installed, [self.head])
        self.assertEqual(sleep.call_count, 2)
        step.assert_not_called()  # the steps run on the next tick, under the new install
        self.assertIsNone(upgrade.read_pending())
        self.assertIn('upgrade: the floor drained', out.getvalue())

    def test_no_line_of_drift_when_the_install_is_the_trunk(self):
        out = io.StringIO()
        with mock.patch.object(drift, 'installed_commit', return_value=self.head), \
                contextlib.redirect_stdout(out), mock.patch('asf.tick.summary.run'):
            tick._run_steps(mock.Mock(), self.product, tick.Context(self.product), [], None)
        self.assertNotIn('BEHIND', out.getvalue())
        self.assertNotIn('UPGRADE AVAILABLE', out.getvalue())

    def test_a_product_that_is_not_the_factory_prints_nothing(self):
        other = env.Product('q', {'repo_dir': self.tmp, 'main': 'main', 'ci': {'provider': 'none'}})
        self.assertIsNone(drift.check(other, installed=self.installed))


class UpgradesAreBatched(DriftTestCase):
    """After an upgrade, the next waits ``upgrade.min_interval_min`` unless the head is urgent."""

    run_tick = TickPrintsTheDriftLine.run_tick

    def setUp(self):
        super().setUp()
        cfg = mock.patch('asf.env.load_config', return_value={'upgrade': {'min_interval_min': 30}})
        cfg.start()
        self.addCleanup(cfg.stop)

    def test_the_first_upgrade_with_no_history_proceeds(self):
        upgrade = mock.Mock(return_value=0)
        lines = self.run_tick('auto', upgrade)
        upgrade.assert_called_once()
        self.assertFalse(any('(batching)' in ln for ln in lines), lines)

    def test_the_interval_holds_a_non_urgent_upgrade(self):
        import time
        from asf import upgrade as upgrading
        at = time.time() - 10 * 60
        upgrading.record_last(self.installed, now=at)
        upgrade = mock.Mock(return_value=0)
        lines = self.run_tick('auto', upgrade)
        upgrade.assert_not_called()
        due = time.strftime('%H:%M', time.localtime(at + 30 * 60))
        self.assertIn(f'upgrade due at {due} (batching)', lines)
        self.assertFalse(any('upgraded under this tick' in ln for ln in lines), lines)

    def test_the_interval_once_passed_lets_the_upgrade_run(self):
        import time
        from asf import upgrade as upgrading
        upgrading.record_last(self.installed, now=time.time() - 31 * 60)
        upgrade = mock.Mock(return_value=0)
        self.run_tick('auto', upgrade)
        upgrade.assert_called_once()

    def test_an_urgent_trailer_bypasses_the_interval(self):
        import time
        from asf import upgrade as upgrading
        upgrading.record_last(self.installed, now=time.time() - 60)
        self.head = self.commit('asf/cli.py', 'x = 3\n', 'fix: the factory is down\n\nUrgent: yes')
        upgrade = mock.Mock(return_value=0)
        lines = self.run_tick('auto', upgrade)
        upgrade.assert_called_once()
        self.assertEqual(upgrade.call_args[0][0].ref, self.head)
        self.assertFalse(any('(batching)' in ln for ln in lines), lines)

    def test_a_successful_install_records_the_time(self):
        from asf import upgrade as upgrading
        with mock.patch.object(upgrading, 'other_ticks', return_value=[]), \
                mock.patch.object(upgrading, '_install', return_value=0):
            upgrading.install(self.head, out=lambda *_: None)
        self.assertEqual(upgrading.read_last()['sha'], self.head)


class DoctorRow(DriftTestCase):
    def test_the_row_goes_red_on_drift(self):
        ok, detail = doctor.check_drift(self.product, installed=self.installed)
        self.assertFalse(ok)
        self.assertIn('BEHIND by 2 commits', detail)

    def test_the_row_is_ok_when_current(self):
        ok, detail = doctor.check_drift(self.product, installed=self.head)
        self.assertTrue(ok, detail)


if __name__ == '__main__':
    unittest.main()
