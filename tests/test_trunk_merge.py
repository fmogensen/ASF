"""A session's push is checked the way CI's merge ref is (:mod:`asf.workers.trunkmerge`).

Reproduces ``cloud/plan-T-0483`` (2026-10-02): the branch was cut before the trunk's check script
gained a forbidden-names step. Its own pre-push ran the branch's copy of the script and passed;
CI ran the trunk's on the merge ref and went red on that step. The pre-push now runs the check
on the branch merged with ``origin/<trunk>`` and refuses the push, naming the trunk's step."""
import argparse
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.workers import githooks, trunkmerge

OLD_CHECK = '#!/bin/sh\necho "step lint ok"\n'
NEW_CHECK = ('#!/bin/sh\necho "step lint ok"\n'
             'if grep -rq xAI src; then echo "step forbidden-names failed"; exit 1; fi\n'
             'echo "step forbidden-names ok"\n')
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@x'}


def _git(args, cwd, env_=None):
    e = dict(os.environ, **ENV)
    for k in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        e.pop(k, None)
    e.update(env_ or {})
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=e)
    return r


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    os.chmod(path, mode)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='trunkmerge_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = os.path.join(self.tmp, 'remote.git')
        _git(['init', '-q', '--bare', '-b', 'main', self.remote], self.tmp)
        self.repo = os.path.join(self.tmp, 'repo')
        _git(['clone', '-q', self.remote, self.repo], self.tmp)
        _git(['checkout', '-q', '-b', 'main'], self.repo)
        _write(os.path.join(self.repo, 'check.sh'), OLD_CHECK, 0o755)
        _write(os.path.join(self.repo, 'src', 'a.txt'), 'a\n')
        self.commit('base')
        _git(['push', '-q', 'origin', 'main'], self.repo)
        # the branch is cut here, before the trunk's check gains its step
        _git(['checkout', '-q', '-b', 'cloud/T-0483'], self.repo)
        _write(os.path.join(self.repo, 'src', 'catalogue.txt'), "label: 'xAI'\n")
        self.commit('task(T-0483): a provider')
        self.branch_sha = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()
        # the trunk moves: its check list gains the forbidden-names step
        _git(['checkout', '-q', 'main'], self.repo)
        _write(os.path.join(self.repo, 'check.sh'), NEW_CHECK, 0o755)
        self.commit('ci: forbidden names')
        _git(['push', '-q', 'origin', 'main'], self.repo)
        _git(['checkout', '-q', 'cloud/T-0483'], self.repo)
        self.holder = os.path.join(self.tmp, 'state', trunkmerge.HOLDER)

    def commit(self, msg):
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', msg], self.repo)


class CheckTests(Fixture):
    def test_the_branch_own_check_passes(self):
        r = subprocess.run('./check.sh', shell=True, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_the_trunk_check_on_the_merge_refuses_naming_the_step(self):
        ok, line = trunkmerge.check(self.repo, self.holder, 'cloud/T-0483', self.branch_sha,
                                    'main', './check.sh')
        self.assertIs(ok, False)
        self.assertIn('step forbidden-names failed', line)
        self.assertIn('merged with origin/main', line)

    def test_a_clean_branch_passes_and_the_checkout_is_reused(self):
        os.remove(os.path.join(self.repo, 'src', 'catalogue.txt'))
        _write(os.path.join(self.repo, 'src', 'b.txt'), 'b\n')
        self.commit('task(T-0483): fine')
        sha = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()
        ok, line = trunkmerge.check(self.repo, self.holder, 'cloud/T-0483', sha, 'main',
                                    './check.sh')
        self.assertIs(ok, True, line)
        cached = os.path.join(self.holder, 'cloud-T-0483')
        _write(os.path.join(cached, 'node_modules', 'dep'), 'kept\n')  # ignored deps stay
        _write(os.path.join(cached, '.gitignore'), 'node_modules\n')
        ok, line = trunkmerge.check(self.repo, self.holder, 'cloud/T-0483', self.branch_sha,
                                    'main', './check.sh')
        self.assertIs(ok, False)
        self.assertEqual(_git(['status', '--porcelain'], cached).stdout.strip(), '')

    def test_a_conflict_with_trunk_refuses_asking_for_a_rebase(self):
        _write(os.path.join(self.repo, 'check.sh'), '#!/bin/sh\necho mine\n', 0o755)
        self.commit('task(T-0483): edit the check')
        sha = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()
        ok, line = trunkmerge.check(self.repo, self.holder, 'cloud/T-0483', sha, 'main',
                                    './check.sh')
        self.assertIs(ok, False)
        self.assertIn('conflicts with trunk — rebase', line)
        self.assertIn('check.sh', line)

    def test_a_stale_checkout_is_pruned(self):
        trunkmerge.check(self.repo, self.holder, 'cloud/old', self.branch_sha, 'main', 'true')
        old = os.path.join(self.holder, 'cloud-old')
        self.assertTrue(os.path.isdir(old))
        gone = trunkmerge.prune(self.repo, self.holder,
                                now=os.path.getmtime(old) + trunkmerge.STALE_S + 1)
        self.assertEqual(gone, ['cloud-old'])
        self.assertFalse(os.path.exists(old))

    def test_pushed_heads_skips_deletes_tags_and_the_trunk(self):
        z = '0' * 40
        text = (f'refs/heads/a {"1" * 40} refs/heads/a {z}\n'
                f'(delete) {z} refs/heads/b {"2" * 40}\n'
                f'refs/tags/v {"3" * 40} refs/tags/v {z}\n'
                f'refs/heads/main {"4" * 40} refs/heads/main {z}\n')
        self.assertEqual(trunkmerge.pushed_heads(text, 'main'), [('a', '1' * 40)])


class CommandTests(Fixture):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, 'asf-home')
        os.makedirs(os.path.join(self.home, 'products'))
        p = mock.patch.object(env, 'ASF_HOME', self.home)
        p.start()
        self.addCleanup(p.stop)
        with open(env.product_path('p'), 'w') as f:
            f.write('product: p\nrepo_slug: o/p\nmain: main\n'
                    'conventions:\n  pre_push_check:\n    code: ./check.sh\n')

    def run_cmd(self, stdin_text):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            rc = trunkmerge.cmd_trunk_check(argparse.Namespace(pre_push=True, product='p'),
                                            stdin=io.StringIO(stdin_text), out=out)
        finally:
            os.chdir(cwd)
        return rc, out.getvalue()

    def test_the_push_is_refused_naming_the_trunk_step(self):
        rc, out = self.run_cmd(f'refs/heads/cloud/T-0483 {self.branch_sha} '
                               f'refs/heads/cloud/T-0483 {"0" * 40}\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('push refused', out)
        self.assertIn('step forbidden-names failed', out)

    def test_a_delete_only_push_is_not_checked(self):
        rc, out = self.run_cmd(f'(delete) {"0" * 40} refs/heads/cloud/T-0483 {self.branch_sha}\n')
        self.assertEqual((rc, out), (0, ''))


class HookTests(unittest.TestCase):
    """The session's pre-push hook runs ``asf trunk-check --pre-push`` after the product's own
    hook, and its refusal is the push's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='trunkhook_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hooks = os.path.join(self.tmp, 'asf-hooks')
        os.makedirs(self.hooks)
        githooks._write_if_changed(os.path.join(self.hooks, 'asf-hook'), githooks.ASF_HOOK)
        for name in githooks.HOOKS:
            githooks._write_if_changed(os.path.join(self.hooks, name),
                                       githooks._SHIM.format(name=name))
        self.calls = os.path.join(self.tmp, 'calls')
        _write(os.path.join(self.tmp, '.local', 'bin', 'asf'),
               f'#!/bin/sh\necho "$@" >> {self.calls}\ncat >> {self.calls}\n'
               f'[ -f {self.tmp}/refuse ] && {{ echo "asf: push refused — trunk" >&2; exit 1; }}\n'
               'exit 0\n', 0o755)
        self.remote = os.path.join(self.tmp, 'remote.git')
        _git(['init', '-q', '--bare', self.remote], self.tmp)
        # a session's worktree is a linked worktree of the product clone
        clone = os.path.join(self.tmp, 'clone')
        _git(['init', '-q', '-b', 'main', clone], self.tmp)
        _git(['remote', 'add', 'origin', self.remote], clone)
        _git(['-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q',
              '--allow-empty', '-m', 'root'], clone)
        self.wt = os.path.join(self.tmp, 'wt')
        _git(['worktree', 'add', '-q', '-b', 'cloud/T-0001', self.wt], clone)
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp,
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                    'GIT_CONFIG_VALUE_0': self.hooks, 'ASF_PRODUCT': 'p',
                    'ASF_PUSH_ALLOW': 'cloud/'}
        _write(os.path.join(self.wt, 'a'), 'a')
        _git(['add', '-A'], self.wt, self.env)
        _git(['commit', '-q', '-m', 'task(T-0001): a'], self.wt, self.env)

    def test_the_hook_runs_the_trunk_check_and_takes_its_refusal(self):
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(self.calls) as f:
            calls = f.read()
        self.assertIn('trunk-check --pre-push --product p', calls)
        self.assertIn('refs/heads/cloud/T-0001', calls)
        _write(os.path.join(self.tmp, 'refuse'), '')
        _write(os.path.join(self.wt, 'b'), 'b')
        _git(['add', '-A'], self.wt, self.env)
        _git(['commit', '-q', '-m', 'task(T-0001): b'], self.wt, self.env)
        p = _git(['push', '-q', 'origin', 'cloud/T-0001'], self.wt, self.env)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('push refused — trunk', p.stderr)


if __name__ == '__main__':
    unittest.main()
