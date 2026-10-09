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

from asf import dispatch, env, merge_queue
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
        # a session's worktree is a linked worktree of the product clone
        self.clone = os.path.join(self.tmp, 'clone')
        _git(['init', '-q', '-b', 'main', self.clone], self.tmp)
        _git(['remote', 'add', 'origin', self.remote], self.clone)
        self.wt = os.path.join(self.tmp, 'wt')
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp,
                    'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'user.name',
                    'GIT_CONFIG_VALUE_0': 't', 'GIT_CONFIG_KEY_1': 'user.email',
                    'GIT_CONFIG_VALUE_1': 't@example.com', 'GIT_CONFIG_KEY_2': 'core.hooksPath',
                    'GIT_CONFIG_VALUE_2': self.hooks, 'ASF_PUSH_ALLOW': 'cloud/ fix/'}
        _git(['commit', '-q', '--allow-empty', '-m', 'root'], self.clone, self.env)
        _git(['worktree', 'add', '-q', '-b', 'cloud/T-0001', self.wt], self.clone, self.env)
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

    def test_a_product_test_fixture_push_to_a_scratch_repo_is_not_the_sessions(self):
        """2026-10-02, a product: its gate's self-test pushes a branch named ``main`` into a
        throwaway bare repo; the session's core.hooksPath, inherited through GIT_CONFIG_*,
        refused it, the gate went red on every branch, five sessions ended "hook refused"."""
        bare = os.path.join(self.tmp, 'fixture.git')
        _git(['init', '-q', '--bare', bare], self.tmp)
        fx = os.path.join(self.tmp, 'fixture')
        _git(['init', '-q', '-b', 'main', fx], self.tmp)
        _git(['commit', '-q', '--allow-empty', '-m', 'x'], fx, self.env)
        for url in (bare, 'file://' + bare):
            p = _git(['push', '-q', url, 'main'], fx, self.env)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(_git(['for-each-ref', '--format=%(refname:short)', 'refs/heads'],
                              bare).stdout.split(), ['main'])

    def test_the_session_worktree_pushing_to_a_local_path_is_still_guarded(self):
        p = _git(['push', '-q', self.remote, 'HEAD:refs/heads/main'], self.wt, self.env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pushes only to factory branches', p.stderr)
        self.assertEqual(self.heads(), [])


class ARefusedPushIsWrittenDown(PrePushAllow):
    """F-0266 S-64355: each of the shim's three refusal points appends one line to
    ``ASF_REFUSAL_LOG``; a push that passes, a heartbeat and a scratch push write nothing; and the
    hook's exit status never depends on the write."""

    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, 'gates', 'job.refusals')
        os.makedirs(os.path.dirname(self.log))
        self.env['ASF_REFUSAL_LOG'] = self.log

    def lines(self):
        import json
        try:
            with open(self.log, encoding='utf-8') as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def own_hook(self, body):
        h = os.path.join(self.clone, '.git', 'hooks', 'pre-push')
        with open(h, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(h, 0o755)

    def test_a_push_allow_refusal_is_one_line(self):
        p = _git(['push', '-q', 'origin', 'HEAD:refs/heads/ci/gate-manifest'], self.wt, self.env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pushes only to factory branches', p.stderr)
        [rec] = self.lines()
        self.assertEqual((rec['kind'], rec['where']), ('push-allow', 'shim'))
        self.assertIn('ci/gate-manifest', rec['line'])
        self.assertRegex(rec['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_the_products_own_hook_refusal_carries_its_output(self):
        self.own_hook('printf "%s\\n" \'pre-push: lint "failed" in a\\b.py\' >&2\nexit 1\n')
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('lint "failed"', p.stderr)   # still printed
        [rec] = self.lines()
        self.assertEqual(rec['kind'], 'hook refused')
        self.assertIn('lint "failed" in a\\b.py', rec['line'])

    def test_a_trunk_check_refusal_is_written_too(self):
        cli = os.path.join(self.tmp, '.local', 'bin', 'asf')
        fallback = os.path.join(self.tmp, 'fallback-cli')
        with open(fallback, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\ncat >/dev/null\necho "trunk-check: pre-push failed on the merge" >&2\n'
                    'exit 1\n')
        os.chmod(fallback, 0o755)
        rc, detail = dispatch.install(path=cli, asf_home=os.path.join(self.tmp, 'dispatch-home'),
                                       venvs=os.path.join(self.tmp, 'dispatch-venvs'),
                                       default_product='', cli=fallback)
        self.assertEqual(rc, 0, detail)
        e = dict(self.env, ASF_PRODUCT='p')
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, e)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('trunk-check: pre-push failed', p.stderr)
        [rec] = self.lines()
        self.assertEqual(rec['kind'], 'hook refused')
        self.assertIn('pre-push failed on the merge', rec['line'])

    def test_a_passing_push_a_heartbeat_and_a_scratch_push_write_nothing(self):
        self.assertEqual(_git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt,
                              self.env).returncode, 0)
        self.assertEqual(_git(['push', '-q', 'origin', 'HEAD:refs/asf/hb/job'], self.wt,
                              self.env).returncode, 0)
        bare = os.path.join(self.tmp, 'fixture.git')
        _git(['init', '-q', '--bare', bare], self.tmp)
        fx = os.path.join(self.tmp, 'fixture')
        _git(['init', '-q', '-b', 'main', fx], self.tmp)
        _git(['commit', '-q', '--allow-empty', '-m', 'x'], fx, self.env)
        self.assertEqual(_git(['push', '-q', bare, 'main'], fx, self.env).returncode, 0)
        self.assertFalse(os.path.exists(self.log))

    def test_the_exit_status_never_depends_on_the_write(self):
        for log in (None, os.path.join(self.tmp, 'no', 'such', 'dir', 'x.refusals')):
            e = dict(self.env)
            if log is None:
                e.pop('ASF_REFUSAL_LOG')
            else:
                e['ASF_REFUSAL_LOG'] = log
            p = _git(['push', '-q', 'origin', 'HEAD:refs/heads/ci/x'], self.wt, e)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('pushes only to factory branches', p.stderr)
            self.assertEqual(_git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt,
                                  e).returncode, 0)


class ASFHookNamesNoUnpinnedAsf(unittest.TestCase):
    """S-76258, C12: the shim's trunk check names only $HOME/.local/bin/asf — never the global,
    unpinned build a bare ``command -v asf`` would find."""

    def test_command_v_asf_appears_nowhere_in_the_shim(self):
        self.assertNotIn('command -v asf', githooks.ASF_HOOK)

    def test_the_marker_the_shim_greps_is_the_dispatchers_own(self):   # PD8
        self.assertTrue(dispatch.MARKER.startswith('# asf dispatcher'))


class TrunkCheckRunsThePinOrRefuses(PrePushAllow):
    """S-76258 §5: the pre-push trunk check runs only a file at ``$HOME/.local/bin/asf`` that is
    asf's own dispatcher (:data:`asf.dispatch.MARKER`) — anything else there refuses the push
    outright, naming the path and the remedy; no file there skips the check, as today."""

    def setUp(self):
        super().setUp()
        self.env['ASF_PRODUCT'] = 'p'
        self.cli = os.path.join(self.tmp, '.local', 'bin', 'asf')
        self.log = os.path.join(self.tmp, 'gates', 'job.refusals')
        os.makedirs(os.path.dirname(self.log))
        self.env['ASF_REFUSAL_LOG'] = self.log

    def lines(self):
        import json
        try:
            with open(self.log, encoding='utf-8') as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def install_dispatcher(self, fallback_cli):
        rc, detail = dispatch.install(path=self.cli, asf_home=os.path.join(self.tmp, 'dispatch-home'),
                                       venvs=os.path.join(self.tmp, 'dispatch-venvs'),
                                       default_product='', cli=fallback_cli)
        self.assertEqual(rc, 0, detail)

    def test_a_dispatcher_at_the_path_runs_the_trunk_check_and_the_push_goes(self):
        fallback = os.path.join(self.tmp, 'fallback-cli')
        with open(fallback, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\ncat >/dev/null\nexit 0\n')
        os.chmod(fallback, 0o755)
        self.install_dispatcher(fallback)
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.heads(), ['cloud/T-0001'])
        self.assertEqual(self.lines(), [])

    def test_a_non_dispatcher_file_at_the_path_refuses_the_push(self):
        os.makedirs(os.path.dirname(self.cli), exist_ok=True)
        with open(self.cli, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\necho hi\n')
        os.chmod(self.cli, 0o755)
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(self.cli, p.stderr)
        self.assertIn('asf hooks install --product p', p.stderr)
        self.assertEqual(self.heads(), [])
        [rec] = self.lines()
        self.assertEqual(rec['kind'], 'hook refused')
        self.assertIn(self.cli, rec['line'])
        self.assertIn('asf hooks install --product p', rec['line'])

    def test_no_file_at_the_path_skips_the_trunk_check_as_today(self):
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.heads(), ['cloud/T-0001'])
        self.assertEqual(self.lines(), [])


if __name__ == '__main__':
    unittest.main()
