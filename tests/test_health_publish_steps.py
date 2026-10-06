"""The health pass makes the publishes of independent runs at once (asf.workers.health.run_steps).

A factory publish is a push the product's pre-push hook gates; one after another they were most
of a tick's health step. Runs on different branches, worktrees and items publish side by side;
runs sharing any of them go strictly in the ledger's order, each seeing the one before it.

F-0223, S-36551: a run the runtime cut off mid-flight (`lifecycle.FAILED_BARE`, the bare
`failed` reason) has its branch published on the same terms as an unpushed one — the gate
widens by exactly that one case, and nothing downstream of it moves."""
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from asf.workers import health
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

sys.path.insert(0, os.path.dirname(__file__))
from test_workers import Home, feature_row, git  # noqa: E402

from asf.workers import health as health_mod  # noqa: E402
from asf.workers import spawn as spawn_mod  # noqa: E402


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


class BareFailedPublishTests(Home):
    """S-36551: `publish_gap`'s gate widened by the single case `reason ==
    lifecycle.FAILED_BARE` — a run the runtime cut off mid-flight has its commits published, on
    the same terms as an unpushed one, and nothing else gains anything."""

    ITEM = 'F-0001'

    def spawn(self, job, step, item=ITEM):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, feature_row(job, item=item), self.acct(), 'b',
                               runtime=rt, cfg=self.cfg)

    def commit(self, wt, name='x'):
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, name), 'w') as f:
            f.write(name)
        git('add', name, cwd=wt)
        git('commit', '-q', '-m', name, cwd=wt)

    def _remote_sha(self, wt, branch):
        p = subprocess.run(['git', 'ls-remote', '--heads', 'origin', branch],
                           cwd=wt, capture_output=True, text=True)
        return p.stdout.split()[0] if p.returncode == 0 and p.stdout.strip() else ''

    def test_a_bare_failed_run_with_commits_is_published(self):
        rec = self.spawn('broken', {'ok': False, 'pid': 51})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt)
        head = git('rev-parse', 'HEAD', cwd=wt)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        published = [d for j, w, d in found if j == 'broken' and w == 'published']
        self.assertTrue(published, found)
        self.assertEqual(self._remote_sha(wt, branch), head)
        s = pool_mod.load_sessions(self.product)['broken']
        self.assertEqual(s['end_reason'], lifecycle.FAILED_BARE)

    def test_a_failed_with_a_second_clause_is_not_published(self):
        # the gate tests equality, never a prefix: `failed: <anything>` gains nothing (P4, P6)
        rec = self.spawn('authfail', {'ok': False, 'pid': 52,
                                      'result': 'Invalid API key · Please run /login'})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse([f for f in found if f[0] == 'authfail' and f[1] == 'published'], found)
        self.assertEqual(self._remote_sha(wt, branch), '')
        s = pool_mod.load_sessions(self.product)['authfail']
        self.assertEqual(s['end_reason'], 'failed: auth')

    def test_an_unpushed_run_still_behaves_exactly_as_before(self):
        # regression, not a new case: `failed: not pushed: …` keeps publishing as it always did
        rec = self.spawn('stillworks', {'ok': True, 'pid': 53})
        wt = rec['worktree']
        self.commit(wt)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        published = [d for j, w, d in found if j == 'stillworks' and w == 'published']
        self.assertTrue(published, found)

    def test_a_hook_refusal_behaves_exactly_as_for_an_unpushed_run(self):
        rec = self.spawn('refused', {'ok': False, 'pid': 54})
        wt = rec['worktree']
        self.commit(wt)
        with mock.patch.object(health_mod, 'push_retry',
                               return_value=(lifecycle.HOOK_REFUSED, 'redact: secrets.py:1 a key')):
            found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        held = [d for j, w, d in found if j == 'refused' and w == 'held']
        self.assertTrue(held, found)
        s = pool_mod.load_sessions(self.product)['refused']
        self.assertTrue(str(s['end_reason']).startswith(f'failed: {lifecycle.HOOK_REFUSED}'))

    def test_a_network_blip_is_retried_with_no_round_spent(self):
        rec = self.spawn('blippy', {'ok': False, 'pid': 55})
        wt = rec['worktree']
        self.commit(wt)
        with mock.patch.object(health_mod, 'push_retry',
                               return_value=(lifecycle.NETWORK_ERROR, 'connection reset by peer')):
            found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        retried = [d for j, w, d in found if j == 'blippy' and w == 'retry']
        self.assertTrue(retried, found)
        s = pool_mod.load_sessions(self.product)['blippy']
        self.assertNotIn('ended', s)  # no round spent: the next pass publishes again

    def test_publish_refused_is_recorded_through_republish_steps_and_not_twice(self):
        # PD2: `publish_refused` is written in exactly one place, behind `republish_steps`' own
        # gate — an ended bare-failed run whose publish is refused on a later pass, never on the
        # live pass that first ends it (that one holds through `hook_refusal_hold` instead)
        rec = self.spawn('redacted', {'ok': False, 'pid': 56})
        wt = rec['worktree']
        self.commit(wt)
        refused_line = 'publish fix/t-9001 refused: redact: secrets.py:1 a key'
        calls = {'n': 0}

        def fake_publish_gap(product, run, ev, reason, alive=None):
            calls['n'] += 1
            return (reason, ev, None) if calls['n'] == 1 else (reason, ev, refused_line)

        with mock.patch.object(health_mod, 'publish_gap', side_effect=fake_publish_gap):
            found1 = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
            self.assertIn(('redacted', 'ended', lifecycle.FAILED_BARE), found1)
            found2 = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
            self.assertIn(('redacted', 'published', refused_line), found2)
            s = pool_mod.load_sessions(self.product)['redacted']
            self.assertEqual(s.get('publish_refused'), refused_line)
            found3 = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        # the same line: quiet (no second 'published' event for a refusal already recorded)
        self.assertFalse([f for f in found3 if f[0] == 'redacted' and f[1] == 'published'], found3)
        s = pool_mod.load_sessions(self.product)['redacted']
        self.assertEqual(s['publish_refused'], refused_line)

    def test_a_ready_adjudicate_ruling_publishes_even_when_bare_failed(self):
        branch = 'fix/B-9001'
        row = pool_mod.Row('adjudicate-ready', 'B-9001', kind='adjudicate', model='Opus',
                           branch=branch)
        rt = runtime_mod.FakeRuntime([{'ok': False, 'pid': 57, 'result':
            'REPORT\nitem: B-9001\nkind: adjudicate\nstatus: done\n'
            'ruling: the branch is ready\nblocked_on: none\n'
            'pushed: rebased deadbee — the factory publishes\n'}])
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)
        self.commit(rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        published = [d for j, w, d in found if j == 'adjudicate-ready' and w == 'published']
        self.assertTrue(published, found)
        self.assertFalse([f for f in found if f[0] == 'adjudicate-ready' and f[1] in ('held', 'parked')],
                         found)


if __name__ == '__main__':
    unittest.main()
