"""tests.test_seat_claims — F-0189: the claim ledger, the compare-and-set, the row that
re-decides, and the two-process proof. Four classes, one per Task of the plan:
``SeatLedger`` (T-0573), ``PoolClaims``, ``WaveRefusedSeat``, ``TwoWavesOneAccount``.

This module only carries ``SeatLedger`` so far — the other three land with the Tasks that make
them meaningful."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from test_workers import Home, feature_row  # noqa: E402,F401

from asf import env  # noqa: E402
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
