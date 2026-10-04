"""tests.test_seat_claims — F-0189: the claim ledger, the compare-and-set, the row that
re-decides, and the two-process proof. Four classes, one per Task of the plan:
``SeatLedger`` (T-0573), ``PoolClaims`` (T-0574), ``WaveRefusedSeat``, ``TwoWavesOneAccount``.

This module carries ``SeatLedger`` and ``PoolClaims`` so far — the other two land with the Tasks
that make them meaningful."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_workers import Home, feature_row  # noqa: E402,F401

from asf import env  # noqa: E402
from asf.workers import observe as observe_mod  # noqa: E402
from asf.workers import pool as pool_mod  # noqa: E402
from asf.workers import seats as seats_mod  # noqa: E402


def dead_pid():
    p = subprocess.Popen(['true'])
    p.wait()
    return p.pid


def claim(product='sample', job='fix-bug-b-0001', pid=None, at=None, account='acct-a',
          model='opus', kind='fix-bug', lane='local'):
    return {'account': account, 'product': product, 'job': job, 'model': model, 'kind': kind,
            'lane': lane, 'pid': os.getpid() if pid is None else pid,
            'at': seats_mod._stamp() if at is None else at}


def registered_run(product, job, pid, started='2026-09-29T00:00:00Z'):
    path = os.path.join(seats_mod.root(), product, 'sessions.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'job': job, 'pid': pid, 'started': started, 'product': product}) + '\n')


def registered_seat(product, job, pid, account='acct-a', started='2026-09-29T00:00:00Z'):
    """A registered run that also holds an account's seat — :func:`registered_run` plus the
    ``account`` key :meth:`asf.workers.pool.Pool.load` reads."""
    pool_mod.append_session(product, {'job': job, 'pid': pid, 'account': account,
                                      'started': started})


def pool_cfg(cap=4, caps=None, name='acct-a'):
    """One-account config for a :meth:`asf.workers.pool.Pool.from_config` pool — PD12: the two
    products a fixture spans here are ``a`` and ``b``, never a real product name."""
    acct = {'name': name, 'role': 'local', 'cap': cap}
    if caps:
        acct['caps'] = caps
    return {'worker_pool': {'accounts': [acct], 'sessions': 'fake'}}


class SeatLedger(Home):
    def test_a_claim_round_trips_through_add_then_read(self):
        with seats_mod.held() as ledger:
            ledger.add(claim())
        live, why = seats_mod.read()
        self.assertEqual(why, '')
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0], claim())

    def test_add_stores_exactly_fields_and_nothing_else(self):
        with seats_mod.held() as ledger:
            ledger.add(dict(claim(), extra='not a field'))
        live, _why = seats_mod.read()
        self.assertEqual(set(live[0]), set(seats_mod.FIELDS))

    def test_a_second_lock_while_one_is_open_raises_busy(self):
        with seats_mod.held():
            with self.assertRaises(seats_mod.Busy):
                with seats_mod.held(wait_s=0.05):
                    pass  # pragma: no cover - never entered

    def test_a_claim_whose_pid_is_dead_is_swept(self):
        with seats_mod.held() as ledger:
            ledger.add(claim(pid=dead_pid()))
        live, _why = seats_mod.read()
        self.assertEqual(live, [])

    def test_a_claim_older_than_the_ttl_is_swept(self):
        stale = seats_mod._stamp(time.time() - seats_mod.CLAIM_TTL_S - 60)
        with seats_mod.held() as ledger:
            ledger.add(claim(at=stale))
        live, _why = seats_mod.read()
        self.assertEqual(live, [])

    def test_a_claim_of_a_live_registered_run_is_neither_counted_nor_kept(self):
        registered_run('sample', 'fix-bug-b-0001', os.getpid())
        with seats_mod.held() as ledger:
            ledger.add(claim(product='sample', job='fix-bug-b-0001'))
        live, _why = seats_mod.read()
        self.assertEqual(live, [])
        with seats_mod.held() as ledger:
            self.assertEqual(ledger.claims, [])  # held() swept it out on the way in too

    def test_a_garbage_ledger_reads_as_no_claims_with_a_reason_and_is_rewritten(self):
        os.makedirs(seats_mod.root(), exist_ok=True)
        with open(seats_mod.ledger_path(), 'w', encoding='utf-8') as f:
            f.write('not json')
        live, why = seats_mod.read()
        self.assertEqual(live, [])
        self.assertTrue(why)
        with seats_mod.held():
            pass  # the next writer rewrites the garbage file on the way in
        with open(seats_mod.ledger_path(), encoding='utf-8') as f:
            self.assertEqual(json.load(f), [])

    def test_an_unwritable_state_root_raises_unusable(self):
        os.makedirs(os.path.dirname(seats_mod.root()), exist_ok=True)
        with open(seats_mod.root(), 'w'):
            pass  # a plain file sits where the state dir must go
        with self.assertRaises(seats_mod.Unusable):
            with seats_mod.held():
                pass  # pragma: no cover - never entered

    def test_ledger_path_follows_asf_home(self):
        first = seats_mod.ledger_path()
        other = tempfile.mkdtemp()
        old = env.ASF_HOME
        env.ASF_HOME = other
        try:
            second = seats_mod.ledger_path()
            self.assertNotEqual(first, second)
            self.assertTrue(second.startswith(other))
        finally:
            env.ASF_HOME = old
            shutil.rmtree(other, ignore_errors=True)


class PoolClaims(Home):
    """:meth:`asf.workers.pool.Pool.take` as a compare-and-set across products — the opening
    reproduction (docs/specs/f-0189.md) inverted: after it, 4 on an account at ``cap: 4`` answers
    4, not 8."""

    def test_a_pools_takes_are_seen_by_a_freshly_built_pool_of_another_product(self):
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a')
        for i in range(4):
            self.assertTrue(a.take(a.accounts[0], 'opus', job=f'job-{i}', product='a',
                                   kind='task'))
        b = pool_mod.Pool.from_config(cfg, 'b')
        self.assertEqual(b.load(b.accounts[0]), 4)
        self.assertFalse(b.under_caps(b.accounts[0], 'opus'))
        self.assertEqual(b.pick_account('code', 'opus'),
                         (None, 'pool full — accounts at cap: acct-a 4/4'))

    def test_a_5th_take_by_either_product_is_refused_and_writes_no_claim(self):
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a')
        for i in range(4):
            self.assertTrue(a.take(a.accounts[0], 'opus', job=f'job-{i}', product='a',
                                   kind='task'))
        claims, _why = seats_mod.read()
        self.assertEqual(len(claims), 4)
        b = pool_mod.Pool.from_config(cfg, 'b')
        self.assertFalse(b.take(b.accounts[0], 'opus', job='job-b', product='b', kind='task'))
        claims, _why = seats_mod.read()
        self.assertEqual(len(claims), 4)

    def test_a_per_model_caps_ceiling_refuses_the_same_way(self):
        cfg = pool_cfg(cap=4, caps={'opus': 1})
        a = pool_mod.Pool.from_config(cfg, 'a')
        self.assertTrue(a.take(a.accounts[0], 'opus', job='job-0', product='a', kind='task'))
        b = pool_mod.Pool.from_config(cfg, 'b')
        self.assertFalse(b.under_caps(b.accounts[0], 'opus'))
        self.assertFalse(b.take(b.accounts[0], 'opus', job='job-1', product='b', kind='task'))

    def test_untake_returns_the_seat_and_the_claim_in_one_step(self):
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a')
        acct = a.accounts[0]
        self.assertTrue(a.take(acct, 'opus', job='job-0', product='a', kind='task'))
        self.assertTrue(a.untake(acct, 'opus', job='job-0', product='a', kind='task'))
        self.assertEqual(seats_mod.read()[0], [])
        b = pool_mod.Pool.from_config(cfg, 'b')
        self.assertEqual(b.load(b.accounts[0]), 0)

    def test_untake_still_finds_its_seat_after_a_refresh(self):
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a')
        acct = a.accounts[0]
        self.assertTrue(a.take(acct, 'opus', job='job-0', product='a', kind='task'))
        self.assertTrue(a.take(acct, 'opus', job='job-1', product='a', kind='task'))  # a _refresh
        self.assertTrue(a.untake(acct, 'opus', job='job-0', product='a', kind='task'))
        self.assertEqual(a.load(acct), 1)

    def test_retake_swaps_the_model_on_the_claim_and_the_live_row_never_refused(self):
        cfg = pool_cfg(cap=1)
        a = pool_mod.Pool.from_config(cfg, 'a')
        acct = a.accounts[0]
        seat = {'model': 'opus', 'job': 'job-0', 'product': 'a', 'kind': 'task', 'lane': None}
        self.assertTrue(a.take(acct, **seat))
        new_seat = a.retake(acct, seat, 'sonnet')
        self.assertEqual(new_seat['model'], 'sonnet')
        self.assertEqual(a.load(acct, 'sonnet'), 1)
        self.assertEqual(a.load(acct, 'opus'), 0)
        claims, _why = seats_mod.read()
        self.assertEqual(claims[0]['model'], 'sonnet')

    def test_a_busy_lock_makes_take_return_false(self):
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a')
        with mock.patch.object(pool_mod.seats_mod, 'held', side_effect=seats_mod.Busy('busy')):
            self.assertFalse(a.take(a.accounts[0], 'opus', job='job-0', product='a', kind='task'))
        self.assertEqual(a.load(a.accounts[0]), 0)

    def test_an_unusable_ledger_makes_take_return_true_and_sets_seats_degraded(self):
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a')
        self.assertEqual(a.seats_degraded, '')
        with mock.patch.object(pool_mod.seats_mod, 'held',
                              side_effect=seats_mod.Unusable('state dir is a file')):
            self.assertTrue(a.take(a.accounts[0], 'opus', job='job-0', product='a', kind='task'))
        self.assertEqual(a.seats_degraded, 'state dir is a file')
        self.assertEqual(a.load(a.accounts[0]), 1)

    def test_a_hand_built_pools_live_survives_a_take(self):
        acct = pool_mod.Account('acct-a', cap=4)
        p = pool_mod.Pool([acct], live=[{'job': 'x', 'account': 'acct-a'}])
        self.assertTrue(p.take(acct, 'opus', job='job-0', product='a', kind='task'))
        self.assertEqual(p.load(acct), 2)

    def test_a_dead_pid_but_ps_visible_run_still_holds_its_seat_after_a_take(self):
        dead = dead_pid()
        registered_seat('a', 'legacy-job', dead)
        src = observe_mod.FakeSource([{'pid': dead, 'ppid': 1, 'env': {}}])
        cfg = pool_cfg()
        a = pool_mod.Pool.from_config(cfg, 'a', session_source=src)
        self.assertEqual(a.load(a.accounts[0]), 1)
        self.assertTrue(a.take(a.accounts[0], 'opus', job='job-0', product='a', kind='task'))
        self.assertEqual(a.load(a.accounts[0]), 2)
