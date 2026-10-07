"""B-0380 — the worktree a launch expects is there, and a leftover that loses nothing is taken.

* a clean orphan worktree (no run recorded it) whose HEAD is exactly ``origin/<branch>`` is
  adopted by the next spawn at once, never refused every tick for the orphan grace period;
* a worktree missing at a correction or a relaunch is rebuilt from origin's head (the trunk for a
  fresh branch) by :func:`asf.workers.worktrees.ensure` before the session starts — the run
  never dies on ``No such file or directory`` and never counts toward a cap.

Hermetic: a bare origin and its clone in a temp dir; the runtime is a fake."""
import os
import subprocess
import unittest

from asf.workers import correct as correct_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stall as stall_mod
from asf.workers import worktrees as wt_mod

try:  # `unittest discover -s tests` puts tests/ on the path
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row, git


class _Base(Home):
    def launch(self, job='spec-1', pid=40):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': pid}])
        rec = spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b\n', runtime=rt,
                              cfg=self.cfg)
        wt = rec['worktree']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'work.txt'), 'w') as f:
            f.write('work\n')
        git('add', 'work.txt', cwd=wt)
        git('commit', '-q', '-m', 'spec(F-0001): work', cwd=wt)
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        return rec, rt

    def forget(self):
        """Every ledger line gone: the worktree is an orphan (no run recorded it)."""
        os.remove(pool_mod.sessions_path(self.product))


class AnOrphanAtOriginIsAdopted(_Base):
    def test_a_young_clean_orphan_at_origin_is_adopted(self):
        rec, _ = self.launch()
        self.forget()
        path = os.path.join(spawn_mod.worktrees_dir(self.product), 'spec-1')
        verdict, _why = lifecycle.launch_verdict(pool_mod.sessions_path(self.product), 'spec-1',
                                                 path)
        self.assertEqual(verdict, lifecycle.ORPHAN)
        self.assertFalse(spawn_mod._stale_orphan(path))  # inside the grace: refused before
        self.assertEqual(wt_mod.adoptable(path, rec['branch']),
                         (True, f"clean at origin/{rec['branch']}"))
        again = spawn_mod.make_worktree(self.product, 'spec-1', rec['branch'])
        self.assertEqual(os.path.realpath(again), os.path.realpath(path))
        self.assertTrue(os.path.exists(os.path.join(again, 'work.txt')))

    def test_a_dirty_or_unpushed_orphan_is_still_refused_inside_the_grace(self):
        rec, _ = self.launch()
        self.forget()
        path = rec['worktree']
        with open(os.path.join(path, 'wip.txt'), 'w') as f:
            f.write('wip')
        self.assertEqual(wt_mod.adoptable(path, rec['branch'])[0], False)
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.make_worktree(self.product, 'spec-1', rec['branch'])
        os.remove(os.path.join(path, 'wip.txt'))
        git('commit', '-q', '--allow-empty', '-m', 'local only', cwd=path)
        self.assertEqual(wt_mod.adoptable(path, rec['branch']),
                         (False, f"HEAD is not origin/{rec['branch']}"))


class AMissingWorktreeIsRecreated(_Base):
    def test_ensure_rebuilds_from_origin_and_from_the_trunk_for_a_fresh_branch(self):
        rec, _ = self.launch()
        path = rec['worktree']
        git('worktree', 'remove', '--force', path, cwd=self.repo)
        self.assertFalse(os.path.exists(path))
        got = wt_mod.ensure(self.product, 'spec-1', rec['branch'], path=path)
        self.assertEqual(os.path.realpath(got), os.path.realpath(path))
        self.assertEqual(git('rev-parse', 'HEAD', cwd=got),
                         git('rev-parse', f"origin/{rec['branch']}", cwd=self.repo))
        self.assertEqual(wt_mod.ensure(self.product, 'spec-1', rec['branch'], path=path), got)
        fresh = wt_mod.ensure(self.product, 'spec-9', 'spec/F-0009-fresh')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=fresh),
                         git('rev-parse', 'origin/main', cwd=self.repo))
        with self.assertRaises(spawn_mod.SpawnError):
            wt_mod.ensure(self.product, 'nobranch', None)

    def test_a_cold_retry_on_a_reaped_worktree_starts_instead_of_dying(self):
        rec, _ = self.launch()
        git('worktree', 'remove', '--force', rec['worktree'], cwd=self.repo)
        pool_mod.update_session(self.product, 'spec-1', ended='2026-10-07T10:00:00Z',
                                end_reason=lifecycle.DEAD_PID)
        session = pool_mod.load_sessions(self.product)['spec-1']
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 41}])
        self.assertTrue(stall_mod.correct_once(self.product, session, 'it died', rt))
        job, _brief = rt.calls[0]
        self.assertTrue(os.path.isdir(job.cwd))
        self.assertEqual(git('rev-parse', 'HEAD', cwd=job.cwd),
                         git('rev-parse', f"origin/{rec['branch']}", cwd=self.repo))
        retry = pool_mod.load_sessions(self.product)['spec-1-correction']
        self.assertEqual(retry['worktree'], job.cwd)

    def test_asf_correct_rebuilds_the_rounds_worktree(self):
        import argparse
        rec, _ = self.launch()
        pool_mod.update_session(self.product, 'spec-1', ended='2026-10-07T10:00:00Z',
                                end_reason='finished')
        git('worktree', 'remove', '--force', rec['worktree'], cwd=self.repo)
        from unittest import mock
        with mock.patch('asf.env.load_product', return_value=self.product):
            rc = correct_mod.cmd_correct(argparse.Namespace(
                product='sample', item='F-0001', why='fix the thing', drop=False, from_pr=None),
                alive=lambda pid: False)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(rec['worktree']))


if __name__ == '__main__':
    unittest.main()
