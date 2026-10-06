"""A worktree directory that is no git checkout (round E #20).

A product's T-0091 (2026-10-05): a review session ran in a leftover worktree directory with no
git checkout in it. Filing its review returned nothing, silently — ``git status`` failed and
:func:`asf.evidence.review_store.take` read that as "no review" — and the next launch reused the
same directory again. Filing now refuses out loud, and a launch never reuses such a directory."""
import os
import shutil
import tempfile
import unittest

from asf.conventions import Conventions
from asf.evidence import review_store
from asf.workers import health as health_mod, pool as pool_mod, runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from tests.test_workers import Home, feature_row, git


class FilingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='noncheckout_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wt = os.path.join(self.tmp, 'wt')
        os.makedirs(self.wt)
        self.conv = Conventions()
        path = os.path.join(self.wt, self.conv.review_path('t-1', 1))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write('verdict: approved\n')

    def test_a_plain_directory_is_refused_out_loud(self):
        store = os.path.join(self.tmp, 'state', review_store.DIRNAME)
        with self.assertRaises(review_store.NotACheckout) as cm:
            review_store.take(store, self.conv, self.wt, 'worker/T-1', 'T-1', '')
        self.assertIn('not a git checkout', str(cm.exception))
        self.assertIsInstance(cm.exception, ValueError)  # health logs "review not filed: …"

    def test_a_directory_inside_another_checkout_is_not_its_checkout(self):
        git('init', '-q', self.tmp, cwd=self.tmp)
        self.assertFalse(review_store.is_checkout(self.wt))
        self.assertTrue(review_store.is_checkout(self.tmp))


class LaunchTests(Home):

    def test_a_leftover_directory_with_no_checkout_is_never_reused(self):
        row = feature_row('again')
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}]),
                              cfg=self.cfg)
        wt = rec['worktree']
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertTrue(pool_mod.load_sessions(self.product)['again'].get('ended'))
        # the checkout goes; the directory stays behind with a file in it
        shutil.rmtree(os.path.join(wt, '.git'), ignore_errors=True)
        if os.path.isfile(os.path.join(wt, '.git')):
            os.remove(os.path.join(wt, '.git'))
        with open(os.path.join(wt, 'left-behind'), 'w') as fh:
            fh.write('x')
        git('worktree', 'prune', cwd=self.product.repo_dir)
        path = spawn_mod.make_worktree(self.product, 'again', rec['branch'])
        self.assertTrue(os.path.exists(os.path.join(path, '.git')),
                        'the launch reused a directory with no git checkout')
        self.assertFalse(os.path.exists(os.path.join(path, 'left-behind')))
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=path), rec['branch'])


if __name__ == '__main__':
    unittest.main()
