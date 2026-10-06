"""tests.test_center — F-0117's command center: the cross-product read model, the card's
write-scope guarantee, the merged tail and the wiring, every one of them fenced against a fixture
``ASF_HOME`` and never a real product tree. ``ReadersDoNotWrite`` is Task T-0728's piece: before
``asf/center/`` exists, it proves the three readers of a product's own state —
``capacity.read_demand_record``, ``capacity.inflight_sessions``/``live_sessions``/``claim``/
``fair_share`` and ``approvals.open_holds`` — leave an absent state directory absent, and never
reach ``env.state_dir`` to do it; and that the writers behind them (``capacity.write_demand``,
``pool.append_session``, ``approvals.append``) still make the directory a reader will later find.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import approvals, capacity, env
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod


class ReadersDoNotWrite(unittest.TestCase):
    """A fresh ``ASF_HOME`` with ``products/p-one.yaml`` and no ``state/`` directory at all."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(os.path.join(self.tmp, 'products', 'p-one.yaml'), 'w', encoding='utf-8') as f:
            f.write('product: p-one\n')
        self.product = env.Product('p-one', {})
        self.cfg = {}

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state_dir_exists(self):
        return os.path.exists(os.path.join(self.tmp, 'state', 'p-one'))

    def _assert_empty_answers(self):
        self.assertIsNone(capacity.read_demand_record('p-one'))
        self.assertEqual(capacity.inflight_sessions('p-one'), 0)
        self.assertEqual(capacity.live_sessions('p-one'), [])
        self.assertEqual(capacity.claim('p-one', 3), 3)
        self.assertEqual(approvals.open_holds(self.product), [])
        self.assertIsNone(capacity.fair_share(self.product, self.cfg,
                                               quota_source=quota_mod.FakeQuotaSource()))

    def test_six_readers_answer_empty_and_leave_no_state_directory(self):
        self.assertFalse(self._state_dir_exists())
        self._assert_empty_answers()
        self.assertFalse(self._state_dir_exists())

    def test_six_readers_never_reach_state_dir(self):
        with mock.patch.object(env, 'state_dir',
                                side_effect=AssertionError('state_dir called')):
            self._assert_empty_answers()
        self.assertFalse(self._state_dir_exists())

    def test_write_demand_makes_its_own_directory(self):
        self.assertFalse(self._state_dir_exists())
        capacity.write_demand('p-one', 1, 2)
        self.assertTrue(self._state_dir_exists())
        self.assertEqual(capacity.read_demand_record('p-one'), (1, 2))

    def test_append_session_makes_its_own_directory(self):
        self.assertFalse(self._state_dir_exists())
        pool_mod.append_session(self.product, {'job': 'j1', 'started': pool_mod.now_iso()})
        self.assertTrue(self._state_dir_exists())
        self.assertIn('j1', pool_mod.load_sessions(self.product))

    def test_approvals_append_makes_its_own_directory(self):
        self.assertFalse(self._state_dir_exists())
        approvals.append(self.product, {'event': 'refused', 'hold': 'X/touch_production'})
        self.assertTrue(self._state_dir_exists())
        self.assertEqual([r['hold'] for r in approvals.read(self.product)], ['X/touch_production'])
