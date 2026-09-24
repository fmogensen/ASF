"""A run whose pid is dead, or that has ended, holds no seat; a run of a removed or done item is
never held or sent back to its session.

Seen live: a fix-bug session died, its Bug was then removed, and the product's tick (which does
not run health) never wrote the run's ``ended`` line. The pool counted it as the account's one
cooldown job, so every other row waited on ``quota cooldown — one job at a time`` until an
operator ran health by hand; health then held the removed Bug's empty branch "back to its
session"."""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_workers import Home, feature_row  # noqa: E402

from asf import capacity as capacity_mod
from asf.feeder import rows as feeder_rows
from asf.tick import step_health
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod


def dead_pid():
    p = subprocess.Popen(['true'])
    p.wait()
    return p.pid


def launch(job, pid, account='acct-a', item='B-0001', **extra):
    return dict({'job': job, 'item': item, 'kind': 'fix-bug', 'account': account,
                 'model': 'sonnet', 'pid': pid, 'started': '2026-09-24T04:10:55Z'}, **extra)


class DeadRunHoldsNoSeat(Home):
    def registry(self, *lines):
        path = pool_mod.sessions_path('sample')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            for line in lines:
                f.write(json.dumps(line) + '\n')
        return path

    def cfg_cooling(self):
        return {'worker_pool': {'accounts': [{'name': 'acct-a', 'role': 'local', 'cap': 4}],
                                'sessions': 'fake'}}

    def test_a_dead_pid_run_is_not_load_inflight_or_a_session(self):
        path = self.registry(launch('fix-bug-b-0001', dead_pid()))
        self.assertTrue(lifecycle.is_live(lifecycle.latest(path)['fix-bug-b-0001']))
        self.assertEqual(lifecycle.inflight(path), [])
        self.assertEqual(capacity_mod.inflight_sessions('sample'), 0)
        p = pool_mod.Pool.from_config(self.cfg_cooling(), 'sample')
        self.assertEqual(p.load(p.accounts[0]), 0)

    def test_an_ended_run_is_not_load_and_a_live_pid_is(self):
        self.registry(launch('a', os.getpid()), launch('b', os.getpid(), item='B-0002'),
                      {'job': 'b', 'ended': '2026-09-24T04:27:24Z', 'end_reason': 'dead pid'})
        p = pool_mod.Pool.from_config(self.cfg_cooling(), 'sample')
        self.assertEqual(p.load(p.accounts[0]), 1)
        self.assertEqual([r['job'] for r in lifecycle.inflight(pool_mod.sessions_path('sample'))],
                         ['a'])

    def test_a_cooldown_account_with_only_a_dead_run_takes_one_new_job(self):
        self.registry(launch('fix-bug-b-0001', dead_pid()))
        p = pool_mod.Pool.from_config(self.cfg_cooling(), 'sample',
                                      quota_source=quota_mod.FakeQuotaSource(
                                          {'acct-a': {'five_h_pct': 0, 'seven_d_pct': 91}}))
        acct, why = p.pick_account('plan', 'sonnet')
        self.assertEqual((acct.name, why), ('acct-a', ''))
        p.take(acct, 'sonnet', 'plan-f-0019')
        self.assertEqual(p.pick_account('plan', 'sonnet'), (None, pool_mod.REASON_COOLDOWN))

    def test_a_dead_pid_runs_worktree_is_reused(self):
        wt = os.path.join(self.tmp, 'wt')
        os.makedirs(wt)
        path = self.registry(launch('j', dead_pid(), worktree=wt))
        self.assertEqual(lifecycle.may_launch(path, 'j', wt), (True, ''))
        self.registry(launch('k', os.getpid(), worktree=wt))
        self.assertFalse(lifecycle.may_launch(path, 'k', wt)[0])


class ClosedItemIsNeverHeld(Home):
    def spawn(self, job, step, item):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, feature_row(job, item=item), self.acct(), 'b',
                               runtime=rt, cfg=self.cfg)

    def test_closed_state(self):
        items = {'B-1': {'removed': 'merged'}, 'B-2': {'state': 'Resolved'},
                 'T-3': {'state': 'Closed'}, 'B-4': {'state': 'New'}}
        self.assertEqual([lifecycle.closed_state(items, i) for i in ('B-1', 'B-2', 'T-3', 'B-4', 'X')],
                         ['removed', 'Resolved', 'Closed', None, None])
        self.assertIsNone(lifecycle.closed_state(None, 'B-1'))

    def test_health_ends_a_removed_items_run_without_a_hold(self):
        self.spawn('gone', {'ok': True, 'pid': 12}, item='F-0001')    # ok, nothing pushed
        self.spawn('kept', {'ok': True, 'pid': 13}, item='F-0002')
        items = {'F-0001': {'removed': 'not a violation'}, 'F-0002': {'state': 'New'}}
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        ended = {j for j, w, _d in found if w == 'ended'}
        held = {j for j, w, _d in found if w == 'held'}
        self.assertEqual((ended, held), ({'gone', 'kept'}, {'kept'}))
        s = pool_mod.load_sessions(self.product)
        self.assertNotIn('correction', s['gone'])
        self.assertIn('correction', s['kept'])
        self.assertEqual(set(lifecycle.corrections(pool_mod.sessions_path(self.product))),
                         {'F-0002'})

    def test_a_pending_correction_on_a_removed_item_is_released(self):
        self.spawn('gone', {'ok': True, 'pid': 12}, item='F-0001')
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None, items={})
        path = pool_mod.sessions_path(self.product)
        self.assertEqual(set(lifecycle.corrections(path)), {'F-0001'})
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items={'F-0001': {'state': 'Closed'}})
        self.assertIn(('gone', 'released', 'F-0001 is Closed: no correction'), found)
        self.assertEqual(lifecycle.corrections(path), {})

    def test_a_dead_run_of_a_removed_item_gets_no_cold_retry(self):
        class Ctx:
            product = self.product

            def event(self, *a, **k):
                raise AssertionError('no event for a closed item')
        s = {'job': 'fix-bug-b-0001', 'item': 'B-0001', 'pid': 1}
        got = step_health.handle_dead(Ctx(), s, runtime_fn=lambda: self.fail('no retry'),
                                      out=lambda s: None, items={'B-0001': {'removed': 'x'}})
        self.assertEqual(got, 'closed')


class FeederSendsNothingBack(unittest.TestCase):
    def test_a_removed_or_done_item_gets_no_correction_row(self):
        corr = {'kind': 'unpushed', 'text': 'empty branch', 'rounds': 1, 'at': 't'}
        items = {'B-0002': {'id': 'B-0002', 'type': 'bug', 'state': 'Resolved', 'severity': 'S2'}}
        # a removed card never reaches the feeder's items (feeder.rows drops ``removed:``)
        rows, ids = feeder_rows.correction_rows(items, None, set(),
                                                {'B-0001': corr, 'B-0002': corr})
        self.assertEqual((rows, ids), ([], set()))


if __name__ == '__main__':
    unittest.main()
