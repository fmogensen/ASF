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


class ClonePathsCreateNothing(unittest.TestCase):
    """``record_dir``/``shadow_dir`` are questions, not clones: asking where one is must not
    create ``~/.ASF/state/<product>/`` as a side effect (the state dir move, T-0464)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='shadow_paths_test_')
        self._orig_asf_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.product = env.Product('sample', {'backlog_dir': '/does/not/matter'})

    def tearDown(self):
        env.ASF_HOME = self._orig_asf_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_dir_creates_nothing(self):
        path = shadow.record_dir(self.product)
        self.assertEqual(path, os.path.join(env.ASF_HOME, 'state', 'sample', 'record'))
        self.assertFalse(os.path.exists(os.path.join(env.ASF_HOME, 'state', 'sample')))

    def test_shadow_dir_creates_nothing(self):
        path = shadow.shadow_dir(self.product)
        self.assertEqual(path, os.path.join(env.ASF_HOME, 'state', 'sample', 'shadow'))
        self.assertFalse(os.path.exists(os.path.join(env.ASF_HOME, 'state', 'sample')))


class OperatorCheckoutSync(unittest.TestCase):
    """``sync_operator_checkout`` fast-forwards the operator's own checkout — never the tick's
    clone — to ``origin/<trunk>``, and says so only when it moves it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sync_operator_test_')
        self.origin = os.path.join(self.tmp, 'origin')
        _init_repo(self.origin)
        with open(os.path.join(self.origin, 'README.md'), 'w') as f:
            f.write('one\n')
        _git(['add', '-A'], self.origin)
        _git(['commit', '-q', '-m', 'one'], self.origin)

        self.checkout = os.path.join(self.tmp, 'checkout')
        _git(['clone', '-q', self.origin, self.checkout], self.tmp)
        _git(['config', 'user.email', 'a@example.com'], self.checkout)
        _git(['config', 'user.name', 'a'], self.checkout)

        self._orig_asf_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.product = env.Product('sample', {'backlog_dir': self.checkout})
        self.out = []

    def tearDown(self):
        env.ASF_HOME = self._orig_asf_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _advance_origin(self):
        with open(os.path.join(self.origin, 'README.md'), 'a') as f:
            f.write('two\n')
        _git(['add', '-A'], self.origin)
        _git(['commit', '-q', '-m', 'two'], self.origin)

    def test_moves_a_clean_checkout_on_the_trunk_and_says_so(self):
        self._advance_origin()
        origin_sha = _rev_parse(self.origin)
        moved = shadow.sync_operator_checkout(self.product, out=self.out.append)
        self.assertTrue(moved)
        self.assertEqual(_rev_parse(self.checkout), origin_sha)
        self.assertEqual(self.out, [f'record: {self.checkout} fast-forwarded to origin/main'])

    def test_silent_and_false_when_already_current(self):
        moved = shadow.sync_operator_checkout(self.product, out=self.out.append)
        self.assertFalse(moved)
        self.assertEqual(self.out, [])

    def test_refuses_and_names_a_dirty_tree(self):
        self._advance_origin()
        with open(os.path.join(self.checkout, 'scratch.txt'), 'w') as f:
            f.write('local edit\n')
        _git(['add', 'scratch.txt'], self.checkout)
        moved = shadow.sync_operator_checkout(self.product, out=self.out.append)
        self.assertFalse(moved)
        self.assertEqual(len(self.out), 1)
        self.assertIn('not fast-forwarded — working tree has local changes', self.out[0])

    def test_untracked_files_do_not_block_the_fast_forward(self):
        self._advance_origin()
        origin_sha = _rev_parse(self.origin)
        with open(os.path.join(self.checkout, 'scratch.txt'), 'w') as f:
            f.write('untracked scratch\n')
        moved = shadow.sync_operator_checkout(self.product, out=self.out.append)
        self.assertTrue(moved)
        self.assertEqual(_rev_parse(self.checkout), origin_sha)

    def test_refuses_and_names_a_non_trunk_head(self):
        self._advance_origin()
        _git(['checkout', '-q', '-b', 'other'], self.checkout)
        moved = shadow.sync_operator_checkout(self.product, out=self.out.append)
        self.assertFalse(moved)
        self.assertEqual(len(self.out), 1)
        self.assertIn('not fast-forwarded — on other, not main', self.out[0])

    def test_refuses_and_names_a_diverged_checkout(self):
        self._advance_origin()
        with open(os.path.join(self.checkout, 'local.txt'), 'w') as f:
            f.write('local commit\n')
        _git(['add', '-A'], self.checkout)
        _git(['commit', '-q', '-m', 'local'], self.checkout)
        moved = shadow.sync_operator_checkout(self.product, out=self.out.append)
        self.assertFalse(moved)
        self.assertEqual(len(self.out), 1)
        self.assertIn('not fast-forwarded —', self.out[0])

    def test_false_without_printing_when_backlog_dir_is_the_clone_itself(self):
        record = shadow.record_dir(self.product)
        shadow.ensure_clone(self.product, record)
        product = env.Product('sample', {'backlog_dir': record})
        self._advance_origin()
        moved = shadow.sync_operator_checkout(product, out=self.out.append)
        self.assertFalse(moved)
        self.assertEqual(self.out, [])


if __name__ == '__main__':
    unittest.main()
