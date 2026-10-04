"""asf.gitpush — a factory push of a ref that carries no new code skips the product's pre-push
hook (``--no-verify``), and every factory push is killed after ``git.push_timeout_s``: the ref is
logged and left as it was, and the caller goes on (2026-09-26: a product's archive push sat 8+
minutes in its hook inside the tick's health step)."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import gitpush, hermetic, refguard
from asf.conventions import DEFAULT_PUSH_TIMEOUT_S, Conventions
from asf.env import Product
from asf.workers import lifecycle as lc
from tests import test_lifecycle as tl


def git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def hook(repo, body):
    """``repo``'s pre-push hook (under ``core.hooksPath``, as a product sets it)."""
    hooks = os.path.join(repo, '.hooks')
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, 'pre-push')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n' + body)
    os.chmod(path, 0o755)
    git(['config', 'core.hooksPath', hooks], repo)


#: the guard an ordinary branch push passes: the defaults, refusing
G = refguard.Guard()


class PushTest(unittest.TestCase):

    def setUp(self):
        base = tempfile.mkdtemp(prefix='gitpush_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        git(['init', '-q', '--bare', '-b', 'main', self.origin], base)
        git(['clone', '-q', self.origin, self.repo], base)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            git(['config', k, v], self.repo)
        git(['commit', '-q', '--allow-empty', '-m', 'seed'], self.repo)
        git(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sha = git(['rev-parse', 'HEAD'], self.repo)

    def heads(self):
        return git(['for-each-ref', '--format=%(refname:short)', 'refs/heads'], self.origin).split()

    def test_a_ref_only_push_skips_the_products_hook_and_a_content_push_runs_it(self):
        hook(self.repo, 'echo "lint: red" >&2\nexit 1\n')
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/archive/x'], self.repo,
                         refs_only=True, guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--no-verify', r.args)
        self.assertIn('archive/x', self.heads())
        r = gitpush.push(['-q', 'origin', ':refs/heads/archive/x'], self.repo, refs_only=True,
                         guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('archive/x', self.heads())
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, guard=G)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('--no-verify', r.args)
        self.assertIn('lint: red', r.stderr)

    def test_a_hanging_push_times_out_names_the_ref_and_leaves_it(self):
        hook(self.repo, 'sleep 30\nexit 0\n')
        lines = []
        started = time.monotonic()
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, timeout=1,
                         log=lines.append, guard=G)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('push timed out after 1s:'), lines)
        self.assertIn('refs/heads/work', lines[0])
        self.assertIn(lines[0], r.stderr)
        self.assertNotIn('work', self.heads())

    def test_the_timeout_is_the_git_block_of_the_conventions(self):
        self.assertEqual(gitpush.push_timeout(), DEFAULT_PUSH_TIMEOUT_S)
        self.assertEqual(DEFAULT_PUSH_TIMEOUT_S, 120)
        conv = Conventions.from_mapping({'git': {'push_timeout_s': 30, 'later': 'x'}})
        self.assertEqual(gitpush.push_timeout(conv), 30)
        self.assertEqual(conv.extra, {'git': {'later': 'x'}})
        self.assertEqual(Conventions.from_mapping({'git': {}}), Conventions())
        self.assertEqual(gitpush.push_timeout(Conventions.from_mapping(
            {'git': {'push_timeout_s': 'soon'}})), DEFAULT_PUSH_TIMEOUT_S)

    def test_the_child_env_is_hermetic_git_env(self):
        # F-0013 D4: gitpush no longer builds its own hook-variable list — one remover, shared
        # with the harvest gate and everything else that spawns a git child.
        base = dict(os.environ, GIT_DIR='/elsewhere/.git', GIT_QUARANTINE_PATH='/elsewhere/q',
                    FAKE_VAR='keep')
        captured = {}

        class FakePopen:
            def __init__(self, *args, **kwargs):
                captured['env'] = kwargs['env']
                self.pid = 1
                self.returncode = 0

            def communicate(self, timeout=None):
                return '', ''

        with mock.patch.object(gitpush.subprocess, 'Popen', FakePopen):
            gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/x'], self.repo, env=base,
                         guard=G)
        self.assertEqual(captured['env'], hermetic.git_env(base))


class RefGuardDoorTest(unittest.TestCase):
    """W4-PR1: every push passes a guard keyed on the repository it pushes. A target that is
    that repository's trunk or a protected ref is refused (``refuse``, before any git runs) or
    warned about (``warn``, the push proceeds); a landing path's door and the record pass."""

    setUp, heads = PushTest.setUp, PushTest.heads

    def push(self, target, guard, *pre):
        lines = []
        r = gitpush.push(['-q', *pre, 'origin', target], self.repo, refs_only=True,
                         guard=guard, log=lines.append)
        return r, lines

    def test_a_push_to_the_trunk_is_refused_without_a_subprocess(self):
        with mock.patch.object(gitpush.subprocess, 'Popen') as popen:
            r, lines = self.push(f'{self.sha}:refs/heads/main', refguard.Guard('main'))
        popen.assert_not_called()
        self.assertEqual(r.returncode, 1)
        self.assertNotEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('REF GUARD: refused push → main'), lines)
        self.assertEqual(r.stderr, lines[0])

    def test_warn_says_so_and_the_push_proceeds(self):
        git(['commit', '-q', '--allow-empty', '-m', 'two'], self.repo)
        two = git(['rev-parse', 'HEAD'], self.repo)
        r, lines = self.push(f'{two}:refs/heads/main', refguard.Guard('main', mode='warn'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('REF GUARD (warn): push → main'), lines)
        self.assertEqual(git(['rev-parse', 'main'], self.origin), two)

    def test_off_is_silent(self):
        self.assertEqual(refguard.Guard('main', mode='off').refusal('x:refs/heads/main'), '')

    def test_every_spelling_of_a_protected_target(self):
        g = refguard.Guard('main')
        for target in (':main', ':refs/heads/main', f'+{self.sha}:release/1', 'HEAD:main',
                       'refs/heads/master'):
            self.assertTrue(g.refusal(target), target)
        for target in (f'{self.sha}:refs/heads/worker/x', 'refs/tags/v1.0.0', ':archive/x',
                       f'+{self.sha}:refs/asf/briefs/j'):
            self.assertEqual(g.refusal(target), '', target)
        # --delete <name>: the name is the target
        self.assertEqual(gitpush.targets(['-q', '--force-with-lease=refs/heads/main:x',
                                          'origin', '--delete', 'main']), ['main'])
        with mock.patch.object(gitpush.subprocess, 'Popen') as popen:
            r, _ = self.push('main', g, '--delete')
        popen.assert_not_called()
        self.assertEqual(r.returncode, 1)

    def test_a_worker_branch_is_allowed(self):
        r, lines = self.push(f'{self.sha}:refs/heads/worker/x', refguard.Guard('main'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [])
        self.assertIn('worker/x', self.heads())

    def test_a_door_advances_the_trunk(self):
        git(['commit', '-q', '--allow-empty', '-m', 'two'], self.repo)
        two = git(['rev-parse', 'HEAD'], self.repo)
        r, lines = self.push(f'{two}:refs/heads/main', refguard.Guard('main', door=True))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [])
        self.assertEqual(git(['rev-parse', 'main'], self.origin), two)

    def test_protected_refs_and_a_develop_trunk(self):
        conv = Conventions.from_mapping({'protected_refs': ['stable/*'],
                                         'flags': {'refguard': 'refuse'}})
        g = refguard.guard_from('develop', conv)
        self.assertEqual(g.mode, 'refuse')
        self.assertTrue(g.refusal('x:refs/heads/develop'))
        self.assertTrue(g.refusal('x:refs/heads/stable/2'))
        self.assertEqual(g.refusal('x:refs/heads/main'), '')     # listed refs replace defaults
        self.assertEqual(g.refusal('x:refs/heads/release/1'), '')
        with mock.patch.object(gitpush.subprocess, 'Popen') as popen:
            r, _ = self.push(f'{self.sha}:refs/heads/develop', g)
        popen.assert_not_called()
        self.assertEqual(r.returncode, 1)

    def test_the_mode_is_the_flag_and_defaults_off(self):
        product = Product('p', {'repo_dir': self.repo, 'main': 'main', 'conventions': {}})
        self.assertEqual(refguard.guard_for(product).mode, 'off')
        self.assertEqual(refguard.DEFAULT_MODE, 'off')
        for value, mode in (('warn', 'warn'), ('refuse', 'refuse'), ('REFUSE', 'refuse'),
                            ('nonsense', 'off'), (True, 'off')):
            product = Product('p', {'repo_dir': self.repo, 'main': 'main',
                                    'conventions': {'flags': {'refguard': value}}})
            self.assertEqual(refguard.guard_for(product, self.repo).mode, mode, value)
        self.assertTrue(refguard.guard_for(product, self.repo, door=True).door)

    def test_the_record_guard_protects_nothing(self):
        git(['commit', '-q', '--allow-empty', '-m', 'record'], self.repo)
        two = git(['rev-parse', 'HEAD'], self.repo)
        r, lines = self.push('HEAD:main', refguard.RECORD)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [])
        self.assertEqual(git(['rev-parse', 'main'], self.origin), two)

    def test_the_door_is_declared_for_the_clients_lint(self):
        self.assertIs(gitpush.__gitpush_door__, True)
        import inspect
        param = inspect.signature(gitpush.push).parameters['guard']
        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(param.default, inspect.Parameter.empty)


class PublishArchiveSkipsTheHookTest(unittest.TestCase):
    """The health step's publish of a session's rebased branch archives the old tip first
    (``archive/<b>-copies-<sha9>``): that push skips the product's hook; the branch's own push
    runs it, and neither may hold the step past its timeout."""

    sh, commit = tl.PublishACopiesRebaseTest.sh, tl.PublishACopiesRebaseTest.commit

    def setUp(self):
        tl.PublishACopiesRebaseTest.setUp(self)  # a rebased head whose old tip needs archiving
        self.ran = os.path.join(os.path.dirname(self.repo), 'hook-ran')
        # the product's hook: it refuses any archive ref and records every push it judged
        hook(self.repo, f'while read -r _l _s ref _o; do echo "$ref" >> "{self.ran}"; '
                        'case "$ref" in refs/heads/archive/*) echo "lint: 8 minutes" >&2; '
                        'exit 1;; esac; done\nexit 0\n')

    def test_the_archive_push_skips_the_hook_and_the_branch_push_runs_it(self):
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        archive = f'archive/fix/B-7777-copies-{self.remote_sha[:9]}'
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', archive],
                                 self.repo).split()[0], self.remote_sha)
        with open(self.ran, encoding='utf-8') as f:
            self.assertEqual(f.read().split(), ['refs/heads/fix/B-7777'])

    def test_a_hanging_hook_times_out_and_publish_returns(self):
        hook(self.repo, 'sleep 30\nexit 0\n')
        started = time.monotonic()
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main',
                              push_timeout_s=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertFalse(ok, line)
        self.assertIn('push timed out after 1s', line)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', 'fix/B-7777'],
                                 self.repo).split()[0], self.remote_sha)


if __name__ == '__main__':
    unittest.main()
