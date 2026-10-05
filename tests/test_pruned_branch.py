"""tests.test_pruned_branch — a revived card whose branch the lane archived and pruned from
origin: the launch cuts the branch fresh from the trunk. The local branch left behind (its tip
kept on origin under ``archive/<branch>``) and the stale ``origin/<branch>`` tracking ref no
longer make every launch fail ("couldn't find remote ref", or "exists locally with N commits")."""
import json
import os

from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from tests.test_workers import Home, git

BRANCH = 'cloud/T-0621'


class PrunedBranchTests(Home):
    def _old_work(self):
        """The branch as the card's first life left it: one commit pushed, then archived and
        deleted on origin by the lane; the clone still has the local branch and its tracking
        ref."""
        git('branch', BRANCH, 'origin/main', cwd=self.repo)
        wt = os.path.join(self.tmp, 'old')
        git('worktree', 'add', '-q', wt, BRANCH, cwd=self.repo)
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'old.txt'), 'w') as f:
            f.write('old\n')
        git('add', 'old.txt', cwd=wt)
        git('commit', '-q', '-m', 'old work', cwd=wt)
        git('push', '-q', 'origin', BRANCH, cwd=wt)
        tip = git('rev-parse', 'HEAD', cwd=wt)
        git('worktree', 'remove', '--force', wt, cwd=self.repo)
        git('push', '-q', 'origin', f'{tip}:refs/heads/archive/{BRANCH}', cwd=self.repo)
        git('push', '-q', 'origin', '--delete', BRANCH, cwd=self.repo)
        git('update-ref', f'refs/remotes/origin/{BRANCH}', tip, cwd=self.repo)  # stale, unpruned
        return tip

    def row(self):
        return pool_mod.parse_row(json.dumps({'job': 'coder-t-0621', 'item': 'T-0621',
                                              'state': 'PLAN', 'action': 'CODE', 'model': 'Opus',
                                              'kind': 'coder', 'branch': BRANCH}))

    def test_an_archived_and_pruned_branch_is_cut_fresh_from_the_trunk(self):
        old = self._old_work()
        rec = spawn_mod.spawn(self.product, self.row(), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 7}]),
                              cfg=self.cfg)
        wt = rec['worktree']
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), BRANCH)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt),
                         git('rev-parse', 'origin/main', cwd=self.repo))
        self.assertFalse(os.path.exists(os.path.join(wt, 'old.txt')))
        # the old tip is still on origin, under its archive
        self.assertIn(old, git('ls-remote', '--heads', 'origin', cwd=self.repo))
        # and the stale tracking ref no longer poses as origin's head
        p = git('for-each-ref', f'refs/remotes/origin/{BRANCH}', cwd=self.repo)
        self.assertEqual(p, '')


if __name__ == '__main__':
    import unittest
    unittest.main()
