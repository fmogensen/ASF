"""The snapshot tick (R22; the card: the factory's own clock imports from the live dev checkout —
a tick mid-update gets a torn tree). The clock runs :mod:`asf.snapshot`'s launcher, which ticks
from an immutable worktree of the checkout's HEAD sha — never from the files being rewritten.

Each test builds a small checkout holding a two-module package, the shape of the torn tree seen
live: a new module lands before the name it imports from its dependency."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from asf import hermetic, scheduler, snapshot

A_V1 = 'from pkg.b import X\nVALUE = X\n'
B_V1 = "X = 'v1'\n"
A_V2 = 'from pkg.b import Y\nVALUE = Y\n'
B_V2 = "X = 'v1'\nY = 'v2'\n"
PROBE = ['-c', 'import os, pkg.a; print(pkg.a.VALUE, os.getcwd())']


def _git(args, cwd):
    p = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
                       cwd=cwd, capture_output=True, text=True, env=hermetic.build())
    if p.returncode != 0:
        raise AssertionError(f'git {args}: {p.stderr}')
    return p.stdout.strip()


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-snapshot-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'checkout')
        os.makedirs(os.path.join(self.repo, 'pkg'))
        _git(['init', '-q', '-b', 'main', self.repo], self.tmp)
        self.write('pkg/__init__.py', '')
        self.write('pkg/a.py', A_V1)
        self.write('pkg/b.py', B_V1)
        self.v1 = self.commit('v1')
        self.code = os.path.join(self.tmp, 'state', 'sample', 'code')
        # the clock runs the installed copy of the launcher, never the module in the checkout
        os.makedirs(self.code)
        self.launcher = os.path.join(self.code, snapshot.LAUNCHER)
        shutil.copyfile(snapshot.__file__, self.launcher)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), 'w', encoding='utf-8') as f:
            f.write(text)

    def commit(self, msg):
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', msg], self.repo)
        return _git(['rev-parse', 'HEAD'], self.repo)

    def launch(self, probe=PROBE, launcher=None):
        env = hermetic.build()
        env['PYTHONPATH'] = self.repo    # what the clock had before: the live checkout first
        return subprocess.run([sys.executable, launcher or self.launcher, '--repo', self.repo,
                               '--code-dir', self.code, '--', *probe],
                              capture_output=True, text=True, env=env, cwd=self.code, timeout=60)


class SnapshotTickTest(SnapshotCase):
    def test_r22_a_tick_started_while_the_checkout_is_rewritten_imports_one_consistent_tree(self):
        # mid-update: the new a.py is on disk, the b.py it needs is not yet
        self.write('pkg/a.py', A_V2)
        live = subprocess.run([sys.executable, *PROBE], cwd=self.repo, capture_output=True,
                              text=True, env=dict(hermetic.build(), PYTHONPATH=self.repo))
        self.assertIn('ImportError', live.stderr)          # the control: the live tree is torn
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        value, cwd = r.stdout.strip().splitlines()[-1].split()
        self.assertEqual(value, 'v1')                      # HEAD's tree, whole
        self.assertEqual(os.path.realpath(cwd),
                         os.path.realpath(os.path.join(self.code, self.v1)))
        self.assertIn(f'snapshot: {self.v1[:12]}', r.stdout)

    def test_r22_ticks_during_a_rewrite_loop_all_import_head(self):
        """A writer rewrites the checkout's files the whole time several ticks start."""
        stop = threading.Event()

        def churn():
            i = 0
            while not stop.is_set():
                self.write('pkg/a.py', (A_V2, A_V1)[i % 2])
                self.write('pkg/b.py', ('', B_V1)[i % 2])
                i += 1
        writer = threading.Thread(target=churn, daemon=True)
        writer.start()
        try:
            results = [self.launch() for _ in range(4)]
        finally:
            stop.set()
            writer.join()
        for r in results:
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip().splitlines()[-1].split()[0], 'v1')

    def test_a_new_head_gets_a_new_snapshot_and_the_same_head_reuses_it(self):
        path = snapshot.ensure(self.repo, self.code)
        sentinel = os.path.join(path, '.sentinel')
        with open(sentinel, 'w') as f:
            f.write('x')
        self.assertEqual(snapshot.ensure(self.repo, self.code), path)
        self.assertTrue(os.path.exists(sentinel))          # reused, not made again
        self.write('pkg/a.py', A_V2)
        self.write('pkg/b.py', B_V2)
        v2 = self.commit('v2')
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1].split()[0], 'v2')
        self.assertEqual(snapshot.current(self.code)[0], v2)
        self.assertEqual(snapshot.snapshots(self.code), sorted([self.v1, v2]))  # v1 kept a while

    def test_an_unused_snapshot_is_pruned_and_the_current_one_never(self):
        snapshot.ensure(self.repo, self.code)
        self.write('pkg/b.py', B_V2)
        v2 = self.commit('v2')
        snapshot.ensure(self.repo, self.code, now=time.time() + snapshot.KEEP_UNUSED_S + 60)
        self.assertEqual(snapshot.snapshots(self.code), [v2])
        self.assertFalse(os.path.exists(os.path.join(self.code, self.v1)))
        self.assertNotIn(os.path.join(self.code, self.v1), _git(['worktree', 'list'], self.repo))

    def test_an_interrupted_snapshot_is_made_again(self):
        half = os.path.join(self.code, self.v1)
        os.makedirs(os.path.join(half, 'pkg'))            # no completion marker
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(half, 'pkg', 'b.py')))

    def test_no_snapshot_no_tick(self):
        shutil.rmtree(os.path.join(self.repo, '.git'))
        r = self.launch()
        self.assertEqual(r.returncode, 2)
        self.assertIn('snapshot: REFUSED', r.stderr)

    def test_a_clock_installed_from_a_snapshot_still_follows_the_checkout(self):
        path = snapshot.ensure(self.repo, self.code)
        self.assertEqual(os.path.realpath(snapshot.source_checkout(path)),
                         os.path.realpath(self.repo))
        self.assertIsNone(snapshot.source_checkout(self.repo))
        self.assertEqual(os.path.realpath(scheduler.snapshot_repo(path)),
                         os.path.realpath(self.repo))

    def test_the_launcher_imports_nothing_from_asf(self):
        with open(snapshot.__file__, encoding='utf-8') as f:
            text = f.read()
        self.assertNotRegex(text, r'(?m)^\s*(from asf|import asf)')


class LauncherRefreshTests(SnapshotCase):
    """Two clocks refreshing the same launcher at once is safe by ``_write``'s per-pid temp and
    atomic ``os.replace`` — a property that needs no test of its own here, not a race."""

    def setUp(self):
        super().setUp()
        # the launcher this Task adds has something to refresh from: a source checkout
        with open(snapshot.__file__, encoding='utf-8') as f:
            self.source_text = f.read()
        import asf
        with open(asf.__file__, encoding='utf-8') as f:
            init_text = f.read()
        os.makedirs(os.path.join(self.repo, 'asf'))
        self.write('asf/__init__.py', init_text)
        self.write('asf/snapshot.py', self.source_text)
        self.head = self.commit('add asf package')

    def stale_launcher_text(self):
        marker = "print('LAUNCHER-STALE-MARKER', flush=True)\n"
        lines = self.source_text.splitlines(keepends=True)
        i = next(n for n, line in enumerate(lines) if line.startswith('import time'))
        return ''.join(lines[:i + 1]) + marker + ''.join(lines[i + 1:])

    def write_stale_launcher(self):
        with open(self.launcher, 'w', encoding='utf-8') as f:
            f.write(self.stale_launcher_text())

    def test_a_stale_launcher_is_refreshed_for_the_next_launch_not_this_one(self):
        self.write_stale_launcher()
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'snapshot: launcher refreshed from {self.head[:12]}', r.stdout)
        self.assertIn('LAUNCHER-STALE-MARKER', r.stdout)    # the old code still ran this launch
        with open(self.launcher, encoding='utf-8') as f:
            self.assertEqual(f.read(), self.source_text)

    def test_a_second_launch_at_the_same_head_refreshes_nothing(self):
        self.write_stale_launcher()
        first = self.launch()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.launch()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn('launcher refreshed', second.stdout)
        self.assertNotIn('LAUNCHER-STALE-MARKER', second.stdout)

    def test_a_launcher_already_equal_to_the_snapshot_is_not_rewritten_at_all(self):
        with open(self.launcher, 'w', encoding='utf-8') as f:
            f.write(self.source_text)
        before = os.stat(self.launcher)
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('launcher refreshed', r.stdout)
        after = os.stat(self.launcher)
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_it_never_refuses_the_tick(self):
        # (1) a snapshot of a sha that has no asf/snapshot.py — a sha older than the file (PD4)
        _git(['checkout', '-q', self.v1], self.repo)
        self.write_stale_launcher()
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('launcher refreshed', r.stdout)
        with open(self.launcher, encoding='utf-8') as f:
            self.assertIn('LAUNCHER-STALE-MARKER', f.read())
        _git(['checkout', '-q', 'main'], self.repo)

        # (2) the snapshot's source is unreadable (PD7a)
        snap = snapshot.ensure(self.repo, self.code)
        source = os.path.join(snap, 'asf', 'snapshot.py')
        os.chmod(source, 0o000)
        self.write_stale_launcher()
        try:
            r = self.launch()
        finally:
            os.chmod(source, 0o644)      # restored before (3), which needs the source readable
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('launcher refreshed', r.stdout)

        # (3) the launcher's own directory is read-only (PD7b)
        ro = os.path.join(self.tmp, 'ro')
        os.makedirs(ro)
        ro_launcher = os.path.join(ro, snapshot.LAUNCHER)
        with open(ro_launcher, 'w', encoding='utf-8') as f:
            f.write(self.stale_launcher_text())
        os.chmod(ro, 0o500)
        self.addCleanup(os.chmod, ro, 0o700)
        r = self.launch(launcher=ro_launcher)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('launcher refreshed', r.stdout)
        with open(ro_launcher, encoding='utf-8') as f:
            self.assertIn('LAUNCHER-STALE-MARKER', f.read())

    def test_the_guard_never_overwrites_the_package_module_running_as_itself(self):
        # make the fixture checkout's committed asf/snapshot.py differ, so a broken guard has
        # something visible to write (PD6)
        self.write('asf/snapshot.py', self.stale_launcher_text())
        self.commit('stale asf/snapshot.py')
        site_root = os.path.join(self.tmp, 'site')
        site = os.path.join(site_root, 'asf')
        os.makedirs(site)
        import asf
        with open(asf.__file__, encoding='utf-8') as f:
            init_text = f.read()
        with open(os.path.join(site, '__init__.py'), 'w', encoding='utf-8') as f:
            f.write(init_text)
        site_snapshot = os.path.join(site, 'snapshot.py')
        with open(snapshot.__file__, encoding='utf-8') as f:
            real_text = f.read()
        with open(site_snapshot, 'w', encoding='utf-8') as f:
            f.write(real_text)
        env = hermetic.build()
        env['PYTHONPATH'] = site_root
        r = subprocess.run([sys.executable, '-m', 'asf.snapshot', '--repo', self.repo,
                            '--code-dir', self.code, '--', *PROBE],
                           capture_output=True, text=True, env=env, cwd=self.code, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(site_snapshot, encoding='utf-8') as f:
            self.assertEqual(f.read(), real_text)
        with open(snapshot.__file__, encoding='utf-8') as f:
            self.assertEqual(f.read(), real_text)


class DoctorShowsTheClocksSha(SnapshotCase):
    def test_clock_code_names_the_sha_and_the_head_to_come(self):
        from unittest import mock
        from asf import doctor, env
        snapshot.ensure(self.repo, self.code)
        with mock.patch.object(scheduler, 'repo_root', return_value=self.repo), \
                mock.patch.object(scheduler, 'code_dir', return_value=self.code):
            ok, detail = doctor.check_clock_code(env.Product('sample', {}))
            self.assertTrue(ok)
            self.assertIn(f'snapshot {self.v1[:12]}', detail)
            self.write('pkg/b.py', B_V2)
            v2 = self.commit('v2')
            ok, detail = doctor.check_clock_code(env.Product('sample', {}))
            self.assertIn(f'checkout HEAD {v2[:12]} (the next tick takes it)', detail)


if __name__ == '__main__':
    unittest.main()
