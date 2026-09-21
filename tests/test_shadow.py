"""asf.tick.shadow — the shadow clone never fast-forwards; it resets to origin every run."""
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.tick import shadow


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _rev_parse(cwd, rev='HEAD'):
    out = subprocess.run(['git', 'rev-parse', rev], cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _init_repo(path):
    os.makedirs(path)
    _git(['init', '-q'], path)
    _git(['config', 'user.email', 'a@example.com'], path)
    _git(['config', 'user.name', 'a'], path)


class EnsureShadowCloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='shadow_test_')
        self.origin = os.path.join(self.tmp, 'origin')
        _init_repo(self.origin)
        with open(os.path.join(self.origin, 'README.md'), 'w') as f:
            f.write('one\n')
        _git(['add', '-A'], self.origin)
        _git(['commit', '-q', '-m', 'one'], self.origin)

        self._orig_asf_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.product = env.Product('sample', {'backlog_dir': self.origin})

    def tearDown(self):
        env.ASF_HOME = self._orig_asf_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clones_on_first_run(self):
        path = shadow.ensure_shadow_clone(self.product)
        self.assertTrue(os.path.isdir(os.path.join(path, '.git')))
        self.assertTrue(os.path.exists(os.path.join(path, 'README.md')))

    def test_stray_local_commit_is_reset_and_tick_proceeds(self):
        path = shadow.ensure_shadow_clone(self.product)
        _git(['config', 'user.email', 'a@example.com'], path)
        _git(['config', 'user.name', 'a'], path)

        # a stray local commit, as if a prior run committed but its push then failed
        with open(os.path.join(path, 'stray.txt'), 'w') as f:
            f.write('stray\n')
        _git(['add', '-A'], path)
        _git(['commit', '-q', '-m', 'stray'], path)
        stray_sha = _rev_parse(path)

        # origin moves on, as it would between two shadow ticks
        with open(os.path.join(self.origin, 'README.md'), 'a') as f:
            f.write('two\n')
        _git(['add', '-A'], self.origin)
        _git(['commit', '-q', '-m', 'two'], self.origin)
        origin_sha = _rev_parse(self.origin)

        path2 = shadow.ensure_shadow_clone(self.product)

        self.assertEqual(path, path2)
        self.assertEqual(_rev_parse(path), origin_sha)
        self.assertNotEqual(_rev_parse(path), stray_sha)
        self.assertFalse(os.path.exists(os.path.join(path, 'stray.txt')))
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=path,
                                 capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
