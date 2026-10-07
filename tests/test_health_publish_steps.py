"""The health pass makes the publishes of independent runs at once (asf.workers.health.run_steps).

A factory publish is a push the product's pre-push hook gates; one after another they were most
of a tick's health step. Runs on different branches, worktrees and items publish side by side;
runs sharing any of them go strictly in the ledger's order, each seeing the one before it."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_workers import Home, feature_row, git  # noqa: E402

from asf.workers import health
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod


def _pass(job, log):
    """A run's pass: note its evidence read, yield one publish, note the resume."""
    log.append(('gather', job))
    result = yield (job,)
    log.append(('resumed', job, result))


class RunStepsTest(unittest.TestCase):
    def test_independent_runs_publish_at_once(self):
        barrier = threading.Barrier(2, timeout=5)
        log = []

        def publish(job):
            barrier.wait()  # breaks (BrokenBarrierError) unless both publishes run together
            return f'pushed {job}'

        entries = [('a', {'branch': 'cloud/a', 'item': 'T-1', 'worktree': '/w/a'}, None),
                   ('b', {'branch': 'cloud/b', 'item': 'T-2', 'worktree': '/w/b'}, None)]
        entries = [(j, r, _pass(j, log)) for j, r, _ in entries]
        health.run_steps(entries, publish, workers=2)
        self.assertIn(('resumed', 'a', 'pushed a'), log)
        self.assertIn(('resumed', 'b', 'pushed b'), log)

    def test_runs_on_one_branch_go_one_after_another(self):
        log = []

        def publish(job):
            log.append(('publish', job))
            return job

        entries = [(j, {'branch': 'cloud/x', 'worktree': f'/w/{j}', 'item': f'T-{j}'},
                    _pass(j, log)) for j in ('a', 'b')]
        health.run_steps(entries, publish, workers=3)
        # the second run's evidence is read only after the first one's publish and resume
        self.assertEqual(log, [('gather', 'a'), ('publish', 'a'), ('resumed', 'a', 'a'),
                               ('gather', 'b'), ('publish', 'b'), ('resumed', 'b', 'b')])

    def test_runs_of_one_item_or_one_worktree_go_one_after_another(self):
        for shared in ({'item': 'T-9'}, {'worktree': '/w/same'}):
            log = []
            runs = [dict({'branch': f'cloud/{j}', 'item': f'T-{j}', 'worktree': f'/w/{j}'},
                         **shared) for j in ('a', 'b')]
            entries = [(j, r, _pass(j, log)) for j, r in zip(('a', 'b'), runs)]
            health.run_steps(entries, lambda job: job, workers=3)
            self.assertEqual([e[:2] for e in log],
                             [('gather', 'a'), ('resumed', 'a'), ('gather', 'b'), ('resumed', 'b')],
                             shared)

    def test_a_run_with_no_publish_and_a_run_with_two(self):
        log = []

        def none(job):
            log.append(('done', job))
            return
            yield  # noqa: unreachable — a generator with nothing to publish

        def two(job):
            first = yield (job + '1',)
            second = yield (job + '2',)
            log.append(('two', first, second))

        entries = [('n', {'branch': 'n'}, none('n')), ('t', {'branch': 't'}, two('t'))]
        health.run_steps(entries, lambda x: x.upper(), workers=2)
        self.assertEqual(log, [('done', 'n'), ('two', 'T1', 'T2')])

    def test_a_publish_that_raises_is_raised(self):
        def boom(job):
            raise OSError('push hung')

        entries = [(j, {'branch': j}, _pass(j, [])) for j in ('a', 'b')]
        with self.assertRaises(OSError):
            health.run_steps(entries, boom, workers=2)


class StrayBranchTests(Home):
    """B-0056's lane-prefix guard, PD2/PD13: an ended unpushed run whose worktree sits on a
    branch under no launch prefix — a hand-made stray, never the factory's — is refused free,
    before any push, and its pending correction is left exactly as it was."""

    def spawn(self, job, row, step):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def commit(self, wt, name='x'):
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, name), 'w') as f:
            f.write(name)
        git('add', name, cwd=wt)
        git('commit', '-q', '-m', name, cwd=wt)

    def _park(self, job):
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        correction = {'kind': lifecycle.UNPUSHED, 'text': lifecycle.unpushed_text(reason),
                     'at': '2026-09-27T07:00:45Z'}
        pool_mod.update_session(self.product, job, ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1, correction=dict(correction))
        return reason, correction

    def test_a_stray_branch_is_refused_free_a_lane_branch_publishes_in_the_same_pass(self):
        # the label is a worker-account name and is never written down (PD13): `x` stands in
        # for it, `worktree-<label>-...` is the shape in prose — this is the one literal form
        row = pool_mod.Row('stray-run', 'F-0001', kind='spec', branch='worktree-x-hotfix-v3')
        rec = self.spawn('stray-run', row, {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.assertEqual(branch, 'worktree-x-hotfix-v3')
        self.commit(wt, 'fix')
        reason, correction = self._park('stray-run')

        normal = self.spawn('normal', feature_row('normal'), {'ok': True})
        self.commit(normal['worktree'], 'fix')
        self._park('normal')

        found = health.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)

        self.assertTrue(any(j == 'stray-run' and w == 'published' and 'is not a lane branch' in d
                            for j, w, d in found), found)
        self.assertFalse(any(j == 'stray-run' and w == 're-judged' for j, w, _d in found), found)
        stray = pool_mod.load_sessions(self.product)['stray-run']
        self.assertEqual(stray['end_reason'], reason)             # not re-judged
        self.assertEqual(stray['correction'], correction)          # byte for byte, untouched
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=self.repo), '')

        normal_run = pool_mod.load_sessions(self.product)['normal']
        self.assertEqual(normal_run['end_reason'], 'finished')
        self.assertIsNone(normal_run.get('correction'))
        self.assertEqual(git('ls-remote', '--heads', 'origin', normal['branch'],
                             cwd=self.repo).split()[0],
                         git('rev-parse', 'HEAD', cwd=normal['worktree']))

    def test_the_refusal_prints_once(self):
        row = pool_mod.Row('stray-run', 'F-0001', kind='spec', branch='worktree-x-hotfix-v3')
        rec = self.spawn('stray-run', row, {'ok': True})
        self.commit(rec['worktree'], 'fix')
        self._park('stray-run')
        first = health.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertTrue(any(j == 'stray-run' and w == 'published' for j, w, _d in first), first)
        second = health.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse(any(j == 'stray-run' and w == 'published' for j, w, _d in second), second)


if __name__ == '__main__':
    unittest.main()
