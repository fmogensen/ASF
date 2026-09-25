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

    def test_the_owner_tick_whose_upgrade_is_pending_runs_no_step_either(self):
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
        self.assertEqual(rc, 0)
        step.assert_not_called()
        self.assertIn(f'tick: waiting — upgrade to {self.head[:7]} pending', out.getvalue())

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
