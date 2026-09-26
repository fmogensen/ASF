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

from asf import gitpush
from asf.conventions import DEFAULT_PUSH_TIMEOUT_S, Conventions
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
                         refs_only=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--no-verify', r.args)
        self.assertIn('archive/x', self.heads())
        r = gitpush.push(['-q', 'origin', ':refs/heads/archive/x'], self.repo, refs_only=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('archive/x', self.heads())
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('--no-verify', r.args)
        self.assertIn('lint: red', r.stderr)

    def test_a_hanging_push_times_out_names_the_ref_and_leaves_it(self):
        hook(self.repo, 'sleep 30\nexit 0\n')
        lines = []
        started = time.monotonic()
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, timeout=1,
                         log=lines.append)
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
