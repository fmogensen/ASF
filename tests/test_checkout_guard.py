import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import upgrade


def git(root, *args):
    subprocess.run(['git', '-C', root, '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                   check=True, capture_output=True)


class TestCheckoutOffMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        git(self.root, 'init', '-q', '-b', 'main')
        with open(os.path.join(self.root, 'f'), 'w') as f:
            f.write('1')
        git(self.root, 'add', 'f')
        git(self.root, 'commit', '-q', '-m', 'one')
        git(self.root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_main_at_origin_is_fine(self):
        self.assertIsNone(upgrade.checkout_off_main(self.root))

    def test_not_a_checkout_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(upgrade.checkout_off_main(os.path.realpath(d)))

    def test_other_branch_names_branch_and_sha(self):
        git(self.root, 'checkout', '-q', '-b', 'feat/x')
        msg = upgrade.checkout_off_main(self.root)
        self.assertIn('on feat/x', msg)
        self.assertIn('branch feat/x', msg)
        sha = subprocess.run(['git', '-C', self.root, 'rev-parse', 'HEAD'], capture_output=True,
                             text=True).stdout.strip()
        self.assertIn(sha[:7], msg)

    def test_detached_head(self):
        git(self.root, 'checkout', '-q', '--detach')
        self.assertIn('a detached head', upgrade.checkout_off_main(self.root))

    def test_dirty_main(self):
        with open(os.path.join(self.root, 'f'), 'w') as f:
            f.write('2')
        self.assertIn('uncommitted changes', upgrade.checkout_off_main(self.root))

    def test_main_ahead_of_origin(self):
        with open(os.path.join(self.root, 'f'), 'w') as f:
            f.write('2')
        git(self.root, 'commit', '-q', '-am', 'two')
        self.assertIn('is not origin/main', upgrade.checkout_off_main(self.root))


class TestUpgradeRefusal(unittest.TestCase):
    def args(self, ref=None):
        return mock.Mock(skip_pipx=False, ref=ref, owner=None, wait=None, sleep=None)

    def test_refuses_from_a_non_main_branch_without_ref(self):
        with mock.patch.object(upgrade, 'checkout_off_main', return_value='off'), \
                mock.patch.object(upgrade, '_git', return_value='feat/x'), \
                mock.patch.object(upgrade, 'install') as inst:
            self.assertEqual(upgrade.cmd_upgrade(self.args()), 2)
            inst.assert_not_called()

    def test_explicit_ref_installs(self):
        with mock.patch.object(upgrade, 'checkout_off_main', return_value='off'), \
                mock.patch.object(upgrade, '_git', return_value='feat/x'), \
                mock.patch.object(upgrade, 'install', return_value=1) as inst:
            self.assertEqual(upgrade.cmd_upgrade(self.args('abc')), 1)
            inst.assert_called_once()


if __name__ == '__main__':
    unittest.main()


class TestDoctorRow(unittest.TestCase):
    def product(self, home):
        return types.SimpleNamespace(name='p')

    def test_no_row_without_an_installed_clock(self):
        from asf import doctor, env
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            self.assertEqual(doctor.check_install_checkout(self.product(home)), [])

    def test_red_row_names_the_checkout_when_a_clock_is_installed(self):
        from asf import doctor, env, scheduler
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            path = scheduler.launcher_path('p')
            os.makedirs(os.path.dirname(path))
            open(path, 'w').close()
            with mock.patch.object(upgrade, 'checkout_off_main', return_value='on feat/x'):
                self.assertEqual(doctor.check_install_checkout(self.product(home)),
                                 [(False, 'on feat/x')])
            with mock.patch.object(upgrade, 'checkout_off_main', return_value=None):
                self.assertTrue(doctor.check_install_checkout(self.product(home))[0][0])
