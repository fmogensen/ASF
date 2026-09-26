"""asf.workers.worktrees — the worktree reaper and its cap.

A worker worktree of an ended session is removed once nothing in it exists only there: a clean
tree whose HEAD is on origin (pushed) or on the trunk. Unpushed commits, uncommitted files and a
live session keep it, each with its reason. Worktrees a queued correction will reuse are kept
within ``worker_pool.worktree_buffer``, least-recently-used first out. Removal is ``git worktree
remove`` only: the branch stays, locally and on origin."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from asf import env
from asf.workers import pool as pool_mod
from asf.workers import spawn as spawn_mod
from asf.workers import worktrees as wt_mod

try:
    from gitfixture import Template
except ImportError:  # pragma: no cover
    from tests.gitfixture import Template


def git(*args, cwd):
    p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f'git {args}: {p.stderr}')
    return p.stdout.strip()


def _build(tmp):
    origin = os.path.join(tmp, 'origin.git')
    seed = os.path.join(tmp, 'seed')
    git('init', '-q', '--bare', '-b', 'main', origin, cwd=tmp)
    git('init', '-q', '-b', 'main', seed, cwd=tmp)
    for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
        git('config', k, v, cwd=seed)
    with open(os.path.join(seed, '.gitignore'), 'w') as f:
        f.write('node_modules/\n')
    git('add', '.', cwd=seed)
    git('commit', '-q', '-m', 'seed', cwd=seed)
    git('push', '-q', origin, 'main', cwd=seed)
    git('clone', '-q', origin, os.path.join(tmp, 'repo'), cwd=tmp)
    for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
        git('config', k, v, cwd=os.path.join(tmp, 'repo'))


REPOS = Template(_build, prefix='worktree_reaper_')

DEAD = staticmethod(lambda pid: False)


class Reaper(unittest.TestCase):
    def setUp(self):
        self.tmp = REPOS.fresh()
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'asf-home')
        self.repo = os.path.join(self.tmp, 'repo')
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main'})
        self.wdir = spawn_mod.worktrees_dir(self.product)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture helpers -------------------------------------------------------------

    def worktree(self, job, commit=True, push=True, ended=True, pid=None, **fields):
        path = os.path.join(self.wdir, job)
        branch = f'worker/{job}'
        git('worktree', 'add', '-q', '-b', branch, path, 'origin/main', cwd=self.repo)
        os.makedirs(os.path.join(path, 'node_modules', 'pkg'))
        with open(os.path.join(path, 'node_modules', 'pkg', 'index.js'), 'w') as f:
            f.write('x' * 4096)
        if commit:
            with open(os.path.join(path, f'{job}.txt'), 'w') as f:
                f.write(job)
            git('add', f'{job}.txt', cwd=path)
            git('commit', '-q', '-m', job, cwd=path)
        if push:
            git('push', '-q', 'origin', branch, cwd=path)
        rec = {'job': job, 'item': fields.pop('item', 'F-0001'), 'worktree': path,
               'branch': branch, 'pid': pid or 4242, 'started': '2026-09-01T00:00:00Z'}
        if ended:
            rec.update(ended='2026-09-01T01:00:00Z', end_reason='stopped', rc=1)
        rec.update(fields)
        pool_mod.append_session(self.product, rec)
        return path, branch

    def plan(self, buffer=2, alive=None):
        return {v.name: v for v in wt_mod.plan(self.product, buffer=buffer,
                                               alive=alive or (lambda pid: False))}

    def reap(self, buffer=2, alive=None):
        lines = []
        result = wt_mod.reap(self.product, buffer=buffer, alive=alive or (lambda pid: False),
                             out=lines.append)
        return result, lines

    # ---- the safety rule ---------------------------------------------------------------

    def test_clean_and_pushed_is_removed_and_the_branch_stays(self):
        path, branch = self.worktree('done')
        v = self.plan()['done']
        self.assertEqual(v.action, wt_mod.REMOVE)
        result, lines = self.reap()
        self.assertFalse(os.path.exists(path))
        self.assertEqual([v.name for v in result['removed']], ['done'])
        self.assertTrue(git('branch', '--list', branch, cwd=self.repo))  # never a branch delete
        self.assertTrue(git('ls-remote', '--heads', 'origin', branch, cwd=self.repo))
        self.assertNotIn(os.path.realpath(path), git('worktree', 'list', cwd=self.repo))
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^worktrees: reaped 1 \(\d+\.\d GB\), kept 0$')

    def test_landed_on_the_trunk_without_a_remote_branch_is_removed(self):
        path, branch = self.worktree('landed', push=False)
        git('push', '-q', 'origin', f'{branch}:main', cwd=path)
        git('fetch', '-q', 'origin', cwd=self.repo)
        self.assertEqual(self.plan()['landed'].action, wt_mod.REMOVE)

    def test_unpushed_commits_are_kept_with_the_reason(self):
        path, _ = self.worktree('ahead', push=True)
        with open(os.path.join(path, 'more.txt'), 'w') as f:
            f.write('more')
        git('add', 'more.txt', cwd=path)
        git('commit', '-q', '-m', 'more', cwd=path)
        v = self.plan()['ahead']
        self.assertEqual(v.action, wt_mod.KEEP)
        self.assertIn('1 unpushed commit', v.reason)
        result, lines = self.reap()
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(result['removed'], [])
        self.assertEqual(lines, [])  # nothing reaped: no line

    def test_never_pushed_branch_is_kept(self):
        path, _ = self.worktree('local', push=False)
        v = self.plan()['local']
        self.assertEqual(v.action, wt_mod.KEEP)
        self.assertIn('unpushed commit', v.reason)

    def test_dirty_tree_is_kept(self):
        path, _ = self.worktree('dirty')
        with open(os.path.join(path, 'scratch.txt'), 'w') as f:
            f.write('untracked')
        v = self.plan()['dirty']
        self.assertEqual(v.action, wt_mod.KEEP)
        self.assertIn('uncommitted', v.reason)
        self.reap()
        self.assertTrue(os.path.isdir(path))

    def test_live_session_is_kept(self):
        path, _ = self.worktree('running', ended=False, pid=777)
        v = self.plan(alive=lambda pid: pid == 777)['running']
        self.assertEqual(v.action, wt_mod.LIVE)
        self.reap(alive=lambda pid: pid == 777)
        self.assertTrue(os.path.isdir(path))

    def test_unended_run_is_kept_even_when_its_pid_is_gone(self):
        # health writes `ended`; until it has, the run is not the reaper's to judge
        path, _ = self.worktree('unjudged', ended=False)
        self.assertEqual(self.plan()['unjudged'].action, wt_mod.LIVE)

    def test_young_orphan_is_kept(self):
        path = os.path.join(self.wdir, 'fresh')
        git('worktree', 'add', '-q', '-b', 'worker/fresh', path, 'origin/main', cwd=self.repo)
        v = self.plan()['fresh']
        self.assertEqual(v.action, wt_mod.KEEP)
        self.assertIn('no session', v.reason)

    # ---- the buffer and the cap ---------------------------------------------------------

    def _queued(self, job, used):
        ended = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(used))
        path, _ = self.worktree(job, ended=True, correction={
            'kind': 'unpushed', 'text': 'fix it', 'at': '2099-01-01T00:00:00Z'})
        pool_mod.update_session(self.product, job, ended=ended)
        return path

    def test_queued_correction_worktrees_stay_within_the_buffer_lru_out(self):
        now = time.time()
        old = self._queued('old', now - 3000)
        mid = self._queued('mid', now - 2000)
        new = self._queued('new', now - 1000)
        got = self.plan(buffer=2)
        self.assertEqual(got['new'].action, wt_mod.SPARE)
        self.assertEqual(got['mid'].action, wt_mod.SPARE)
        self.assertEqual(got['old'].action, wt_mod.REMOVE)
        self.assertIn('buffer', got['old'].reason)
        self.reap(buffer=2)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(mid) and os.path.isdir(new))

    def test_finished_run_awaiting_its_landing_is_spare(self):
        path, _ = self.worktree('waiting', end_reason='finished', rc=0)
        self.assertEqual(self.plan(buffer=1)['waiting'].action, wt_mod.SPARE)
        self.assertEqual(self.plan(buffer=0)['waiting'].action, wt_mod.REMOVE)

    def test_unsafe_worktrees_use_up_the_buffer(self):
        now = time.time()
        queued = self._queued('queued', now - 100)
        dirty, _ = self.worktree('dirty')
        with open(os.path.join(dirty, 'wip.txt'), 'w') as f:
            f.write('wip')
        got = self.plan(buffer=1)
        self.assertEqual(got['dirty'].action, wt_mod.KEEP)
        self.assertEqual(got['queued'].action, wt_mod.REMOVE)

    def test_live_sessions_widen_the_cap(self):
        now = time.time()
        self.worktree('running', ended=False, pid=777)
        self._queued('a', now - 200)
        self._queued('b', now - 100)
        got = self.plan(buffer=1, alive=lambda pid: pid == 777)
        self.assertEqual(got['running'].action, wt_mod.LIVE)
        self.assertEqual(got['b'].action, wt_mod.SPARE)
        self.assertEqual(got['a'].action, wt_mod.REMOVE)

    def test_buffer_reads_the_config_with_a_default(self):
        self.assertEqual(wt_mod.buffer_of({}), 2)
        self.assertEqual(wt_mod.buffer_of({'worker_pool': {'worktree_buffer': 5}}), 5)
        self.assertEqual(wt_mod.buffer_of({'worker_pool': {'worktree_buffer': 'x'}}), 2)

    # ---- what the doctor reads ----------------------------------------------------------

    def test_the_pass_records_what_the_doctor_prints(self):
        self.worktree('done')
        kept, _ = self.worktree('ahead', push=False)
        self.reap()
        ok, line = wt_mod.doctor_line(self.product)
        self.assertIn('worktrees: 1', line)
        self.assertIn('0 removable', line)
        self.assertIn('last pass reaped 1', line)

    def test_doctor_line_notes_a_pnpm_store(self):
        with open(os.path.join(self.repo, 'pnpm-lock.yaml'), 'w') as f:
            f.write('lockfileVersion: 9\n')
        self.assertIn('pnpm store: default', wt_mod.pnpm_note(self.product, environ={}))
        self.assertIn('pnpm store: /s',
                      wt_mod.pnpm_note(self.product, environ={'npm_config_store_dir': '/s'}))
        os.remove(os.path.join(self.repo, 'pnpm-lock.yaml'))
        self.assertEqual(wt_mod.pnpm_note(self.product, environ={}), '')


if __name__ == '__main__':
    unittest.main()
