"""asf.gitops — the one client for read-only ``git``: every call is a Result, and a call git
could not answer (a timeout, no repository, an unknown object) is Unknown — ``None`` from the
helpers — never read as "no"."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import gitops


class GitopsTests(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.repo = os.path.join(self.d, 'repo')
        os.makedirs(self.repo)
        self.raw('init', '-q')
        self.commit('one')
        self.one = self.raw('rev-parse', 'HEAD')
        self.commit('two\n\nbody line')
        self.two = self.raw('rev-parse', 'HEAD')
        self.raw('update-ref', 'refs/remotes/origin/main', self.one)

    def raw(self, *a):
        return subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, msg):
        self.raw('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
                 '-m', msg)

    def test_git_ok_and_failed(self):
        r = gitops.git(['rev-parse', 'HEAD'], self.repo)
        self.assertTrue(r.ok)
        self.assertEqual(r.data, self.two)
        self.assertTrue(r.as_of)
        r = gitops.git(['rev-parse', '--verify', 'nope'], self.repo)
        self.assertFalse(r.ok)
        self.assertNotEqual(r.rc, 0)
        self.assertTrue(r.reason.startswith(f'rc {r.rc}'))

    def test_a_timeout_or_a_missing_cwd_is_unknown(self):
        boom = subprocess.TimeoutExpired(['git'], 1)
        with mock.patch.object(gitops.subprocess, 'run', side_effect=boom):
            r = gitops.git(['status'], self.repo, timeout=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, 'timeout')
        self.assertEqual(r.rc, -1)
        r = gitops.git(['status'], os.path.join(self.d, 'gone'))
        self.assertFalse(r.ok)
        self.assertEqual(r.rc, -1)

    def test_rev_parse(self):
        self.assertEqual(gitops.rev_parse(self.repo, 'HEAD'), self.two)
        self.assertEqual(gitops.rev_parse(self.repo, 'refs/remotes/origin/gone'), '')
        self.assertIsNone(gitops.rev_parse(self.d, 'HEAD'))  # not a repository: unknown

    def test_is_ancestor(self):
        self.assertIs(gitops.is_ancestor(self.repo, self.one, 'origin/main'), True)
        self.assertIs(gitops.is_ancestor(self.repo, self.two, 'origin/main'), False)
        self.assertIsNone(gitops.is_ancestor(self.repo, 'f' * 40, 'origin/main'))
        with mock.patch.object(gitops.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired(['git'], 1)):
            self.assertIsNone(gitops.is_ancestor(self.repo, self.one, 'origin/main'))

    def test_log1_and_rev_list_count(self):
        self.assertEqual(gitops.log1(self.repo, self.two, '%s'), 'two')
        self.assertIn('body line', gitops.log1(self.repo, self.two, '%B'))
        self.assertIsNone(gitops.log1(self.repo, 'f' * 40, '%s'))
        self.assertEqual(gitops.rev_list_count(self.repo, 'origin/main', 'HEAD'), 1)
        self.assertEqual(gitops.rev_list_count(self.repo, 'HEAD', 'origin/main'), 0)
        self.assertIsNone(gitops.rev_list_count(self.repo, 'origin/main', 'origin/gone'))

    def test_a_hooks_git_dir_never_leaks_in(self):
        with mock.patch.dict(os.environ, {'GIT_DIR': os.path.join(self.d, 'elsewhere')}):
            self.assertEqual(gitops.rev_parse(self.repo, 'HEAD'), self.two)


if __name__ == '__main__':
    unittest.main()
