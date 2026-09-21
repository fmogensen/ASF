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

    def test_ensure_clone_sets_the_factory_identity_and_can_exclude_tables(self):
        path = os.path.join(self.tmp, 'elsewhere', 'clone')
        for _ in range(2):  # the clone, then the reset path
            shadow.ensure_clone(self.product, path, exclude_tables=True)
        ident = subprocess.run(['git', 'config', 'user.name'], cwd=path, capture_output=True, text=True).stdout
        self.assertEqual(ident.strip(), 'ASF')
        with open(os.path.join(path, '.git', 'info', 'exclude')) as f:
            self.assertEqual(f.read().count('/tables/'), 1)

    def test_ensure_clone_drops_untracked_leftovers(self):
        path = shadow.ensure_clone(self.product, os.path.join(self.tmp, 'c'))
        with open(os.path.join(path, 'half-written.txt'), 'w') as f:
            f.write('x')
        shadow.ensure_clone(self.product, path)
        self.assertFalse(os.path.exists(os.path.join(path, 'half-written.txt')))

    def test_push_reports_a_refusal_as_false(self):
        path = shadow.ensure_clone(self.product, os.path.join(self.tmp, 'c'))
        with open(os.path.join(path, 'new.txt'), 'w') as f:
            f.write('x')
        self.assertTrue(shadow.commit_local(path, 'add'))
        self.assertFalse(shadow.commit_local(path, 'again'))
        _git(['remote', 'set-url', 'origin', os.path.join(self.tmp, 'missing')], path)
        self.assertFalse(shadow.push(path))


if __name__ == '__main__':
    unittest.main()
