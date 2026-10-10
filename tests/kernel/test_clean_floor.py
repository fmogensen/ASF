"""ASF keeps its own footprint clean: an ended session's worktree is freed, the watch reaps the
worktrees no session holds any more, and ``asf kernel install`` prunes its old venvs (the running
one and two previous kept, any pinned one kept) with their dangling ``~/.local/bin`` links."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.kernel import host, loop
from asf.kernel import model as M
from asf.kernel import ports as P

try:
    from kernel import builders as B
    from kernel import fakes as F
    from kernel.test_host import Home
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F
    from tests.kernel.test_host import Home

State = B.State


class EndedSessionsFreeTheirWorktree(unittest.TestCase):

    def test_every_ended_session_is_ended_with_its_worktree_freed(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.BUILDING),
                            B.task('T-0002', state=State.REVIEW)])
        sess = F.FakeSessions([
            B.session('j1', 'T-0001', alive=False, ended=False),
            B.session('r2', 'T-0002', kind='review', alive=False, ended=True, status='done',
                      report='VERDICT: approve')])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0002')])
        loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                  ports=F.ports(record=rec, github=gh, sessions=sess), config=B.config(),
                  state_dir=tempfile.mkdtemp(), out=lambda _l: None)
        self.assertEqual(sorted(sess.ended), [('j1', True), ('r2', True)])

    def test_the_real_port_removes_a_clean_ended_build_worktree(self):
        repo, state = tempfile.mkdtemp(), tempfile.mkdtemp()
        for args in (['init', '-q', '-b', 'main'], ['commit', '-q', '--allow-empty', '-m', 'x']):
            subprocess.run(['git'] + args, cwd=repo, check=True, capture_output=True)
        wt = os.path.join(state, 'worktrees', 'coder-t-0001')
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'w1', wt], cwd=repo, check=True,
                       capture_output=True)
        s = M.Session(job='coder-t-0001-1', item_id='T-0001', kind='build', alive=False,
                      ended=True, result='pushed', worktree=wt)
        with mock.patch('asf.env.state_dir', return_value=state), \
                mock.patch('asf.workers.pool.update_session'), \
                mock.patch('asf.workers.trash.kick'):
            P.RealSessions(env.Product('sample', {'repo_dir': repo})).end(s, True)
        self.assertFalse(os.path.exists(wt))


class WatchReaps(Home):

    def watch(self):
        return host.watch(self.product, cfg={}, python='/v/bin/python')

    def test_the_watch_reaps_worktrees_no_session_holds(self):
        host.install(self.product, cfg={}, python='/v/bin/python', out=self.lines.append)
        with mock.patch.object(host, 'reap_worktrees', return_value='worktrees_reaped=3'):
            rc, line = self.watch()
        self.assertEqual(rc, 0)
        self.assertIn('worktrees_reaped=3', line)

    def test_reap_worktrees_runs_the_reaper_on_the_product(self):
        repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        product = env.Product('sample', {'repo_dir': repo})
        with mock.patch('asf.env.state_dir', return_value=self.state), \
                mock.patch('asf.workers.worktrees.reap',
                           return_value={'removed': ['a', 'b'], 'failed': []}) as reap:
            self.assertEqual(host.reap_worktrees(product), 'worktrees_reaped=2')
        self.assertIs(reap.call_args[0][0], product)
        with mock.patch('asf.env.state_dir', return_value=self.state), \
                mock.patch('asf.workers.worktrees.reap', side_effect=RuntimeError('x')):
            self.assertEqual(host.reap_worktrees(product), 'worktrees_reap_failed')


class PruneVenvs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.venvs = os.path.join(self.tmp, 'venvs')
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        self.lines = []

    def venv(self, name, age_h):
        path = os.path.join(self.venvs, name)
        os.makedirs(os.path.join(path, 'bin'))
        open(os.path.join(path, 'bin', 'asf'), 'w').close()
        os.symlink(os.path.join(path, 'bin', 'asf'), os.path.join(self.bin, 'asf-' + name))
        t = time.time() - age_h * 3600
        os.utime(path, (t, t))
        return path

    def test_keeps_the_running_two_previous_and_pinned_and_drops_their_links(self):
        cur = self.venv('asf-factory-asf-kernel-aaaaaaa', 0)
        for i, sha in enumerate(('bbbbbbb', 'ccccccc', 'ddddddd', 'eeeeeee', 'fffffff')):
            self.venv('asf-factory-asf-kernel-' + sha, i + 1)
        self.venv('asf-factory-sample-1234567', 50)
        self.venv('asf-factory-asf-1111111', 50)
        pin = os.path.join(self.tmp, 'pin.plist')
        with open(pin, 'w') as f:
            f.write('<string>%s/asf-factory-asf-kernel-fffffff/bin/python</string>'
                    % self.venvs)
        removed = host.prune_venvs(os.path.join(cur, 'bin', 'python'), bin_dir=self.bin,
                                   pins=[pin], out=self.lines.append)
        self.assertEqual(sorted(removed), ['asf-factory-asf-kernel-ddddddd',
                                           'asf-factory-asf-kernel-eeeeeee'])
        self.assertEqual(sorted(os.listdir(self.venvs)), [
            'asf-factory-asf-1111111', 'asf-factory-asf-kernel-aaaaaaa',
            'asf-factory-asf-kernel-bbbbbbb', 'asf-factory-asf-kernel-ccccccc',
            'asf-factory-asf-kernel-fffffff', 'asf-factory-sample-1234567'])
        self.assertEqual(sorted(os.listdir(self.bin)), sorted(
            'asf-' + n for n in os.listdir(self.venvs)))
        self.assertIn('pruned 2 old venv(s)', self.lines[-1])

    def test_a_python_outside_a_family_venv_prunes_nothing(self):
        self.venv('asf-factory-asf-kernel-bbbbbbb', 5)
        self.assertEqual(host.prune_venvs('/usr/bin/python3', bin_dir=self.bin, pins=[]), [])
        self.assertEqual(host.prune_venvs(os.path.join(self.venvs, 'plain', 'bin', 'python'),
                                          bin_dir=self.bin, pins=[]), [])

    def test_install_prunes_but_a_dry_run_does_not(self):
        with mock.patch.object(host, 'jobs', return_value=[]), \
                mock.patch.object(host, 'prune_venvs') as prune:
            host.install(env.Product('sample', {}), dry_run=True, cfg={}, python='/p')
            prune.assert_not_called()
            host.install(env.Product('sample', {}), cfg={}, python='/p', out=self.lines.append)
            self.assertEqual(prune.call_args[0][0], '/p')


if __name__ == '__main__':
    unittest.main()
