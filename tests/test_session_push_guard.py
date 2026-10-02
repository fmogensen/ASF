"""An ASF session never pushes to a non-factory branch, and an ``asf land`` request's PR is
never factory work.

* the spawn guard (:func:`asf.workers.spawn.spawn_refusal`) refuses a launch for a PR that is an
  ``asf land`` request, and any pushing session on a branch outside the factory prefixes — before
  a worktree is made;
* the session's ``pre-push`` hook (:data:`asf.workers.githooks.ASF_HOOK`) refuses a push to any
  ``refs/heads/`` ref outside ``ASF_PUSH_ALLOW`` (:func:`asf.workers.spawn.push_allow`).
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, merge_queue
from asf.workers import githooks, spawn
from asf.workers.pool import Row


def _git(args, cwd, e=None):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=e)


def product():
    return env.Product('p', {'repo_slug': 'o/p', 'main': 'main', 'conventions': {
        'merge': 'queue', 'branch_prefixes': {'code': 'cloud/', 'spec': 'cloud/spec-'}}})


class SpawnGuard(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='pushguard_')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.object(env, 'ASF_HOME', self.home)
        p.start()
        self.addCleanup(p.stop)
        merge_queue.add_request(env.state_dir('p'), 1022, 'ci/gate-manifest', priority=True)

    def test_a_land_request_pr_is_never_launched(self):
        for kind in ('review', 'coder', 'correct'):
            row = Row(f'{kind}-pr-1022', 'PR-1022', kind=kind, branch='ci/gate-manifest')
            why = spawn.spawn_refusal(product(), row, 'ci/gate-manifest')
            self.assertIn('PR #1022 is an asf land request', why)

    def test_a_pushing_session_on_a_non_factory_branch_is_refused(self):
        row = Row('correct-pr-0012', 'PR-0012', kind='correct', branch='ci/other')
        self.assertIn('not a factory branch', spawn.spawn_refusal(product(), row, 'ci/other'))
        review = Row('review-pr-0012', 'PR-0012', kind='review', branch='ci/other')
        self.assertEqual(spawn.spawn_refusal(product(), review, 'ci/other'), '')
        task = Row('coder-t-0001', 'T-0001', kind='coder', branch='cloud/T-0001')
        self.assertEqual(spawn.spawn_refusal(product(), task, 'cloud/T-0001'), '')
        minted = Row('groom-x', 'G-1', kind='groom')            # a branch the spawn mints
        self.assertEqual(spawn.spawn_refusal(product(), minted, 'groom/groom-x'), '')

    def test_spawn_refuses_before_any_worktree_is_made(self):
        row = Row('correct-pr-1022', 'PR-1022', kind='correct', branch='ci/gate-manifest')
        with mock.patch.object(spawn, 'make_worktree') as make, \
                mock.patch.object(spawn.runtime_mod, 'auth_env_values', return_value={}):
            with self.assertRaises(spawn.SpawnError):
                spawn.spawn(product(), row, None, 'brief', runtime=mock.Mock(), cfg={})
            make.assert_not_called()

    def test_push_allow_names_the_factory_prefixes_and_a_minted_branch(self):
        allow = spawn.push_allow(product(), Row('groom-x', 'G-1', kind='groom'), 'groom/groom-x')
        self.assertIn('cloud/', allow.split())
        self.assertIn('groom/groom-x', allow.split())
        allow = spawn.push_allow(product(), Row('r', 'PR-1', kind='review', branch='ci/x'), 'ci/x')
        self.assertNotIn('ci/x', allow.split())
        fix = Row('correct-b-1', 'B-0001', kind='correct', branch='fix-bug/fix-bug-b-0001')
        self.assertIn('fix-bug/fix-bug-b-0001', spawn.push_allow(product(), fix, fix.branch).split())
        self.assertEqual(spawn.spawn_refusal(product(), fix, fix.branch), '')


class PrePushAllow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pushallow_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hooks = os.path.join(self.tmp, 'asf-hooks')
        os.makedirs(self.hooks)
        githooks._write_if_changed(os.path.join(self.hooks, 'asf-hook'), githooks.ASF_HOOK)
        for name in githooks.HOOKS:
            githooks._write_if_changed(os.path.join(self.hooks, name),
                                       githooks._SHIM.format(name=name))
        self.remote = os.path.join(self.tmp, 'remote.git')
        _git(['init', '-q', '--bare', self.remote], self.tmp)
        self.wt = os.path.join(self.tmp, 'wt')
        _git(['init', '-q', '-b', 'cloud/T-0001', self.wt], self.tmp)
        _git(['remote', 'add', 'origin', self.remote], self.wt)
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp,
                    'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'user.name',
                    'GIT_CONFIG_VALUE_0': 't', 'GIT_CONFIG_KEY_1': 'user.email',
                    'GIT_CONFIG_VALUE_1': 't@example.com', 'GIT_CONFIG_KEY_2': 'core.hooksPath',
                    'GIT_CONFIG_VALUE_2': self.hooks, 'ASF_PUSH_ALLOW': 'cloud/ fix/'}
        with open(os.path.join(self.wt, 'a'), 'w', encoding='utf-8') as f:
            f.write('a')
        _git(['add', '-A'], self.wt, self.env)
        _git(['commit', '-q', '-m', 'task(T-0001): a'], self.wt, self.env)

    def heads(self):
        return _git(['for-each-ref', '--format=%(refname:short)', 'refs/heads'],
                    self.remote).stdout.split()

    def test_a_push_to_a_factory_branch_goes(self):
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.heads(), ['cloud/T-0001'])

    def test_a_push_to_a_non_factory_branch_is_refused(self):
        p = _git(['push', '-q', 'origin', 'HEAD:refs/heads/ci/gate-manifest'], self.wt, self.env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pushes only to factory branches', p.stderr)
        self.assertIn('ci/gate-manifest', p.stderr)
        self.assertEqual(self.heads(), [])

    def test_without_the_allow_list_nothing_is_refused(self):
        e = dict(self.env)
        e.pop('ASF_PUSH_ALLOW')
        p = _git(['push', '-q', 'origin', 'HEAD:refs/heads/ci/gate-manifest'], self.wt, e)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
