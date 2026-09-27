"""A quota reading the source polled long ago is stale, not current (inbox: the account manager
stopped polling at 01:26 and the wave kept judging launches on the frozen percentages).

* the source may print ``polled_at`` (ISO); a reading older than ``quota_guards.stale_after_min``
  (default 30) is stale;
* a stale reading's percentages never band or budget an account — the session-limit stop
  (``quota-limits.json``, written when a run ends on a usage limit or a 429) governs it instead;
* ``asf status`` names it: ``w1 38%/89% stale since 01:26``.
"""
import datetime
import unittest
from unittest import mock

from asf.workers import headroom
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from tests.test_quota_headroom import HomeCase

UTC = datetime.timezone.utc


def ago(minutes):
    return (datetime.datetime.now(UTC) - datetime.timedelta(minutes=minutes)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def local_hm(iso):
    return headroom.parse_ts(iso).astimezone().strftime('%H:%M')


class TestStaleReading(unittest.TestCase):
    def test_threshold_defaults_to_30_and_is_configurable(self):
        self.assertEqual(quota_mod.guards_from_config({})['stale_after_min'], 30)
        g = quota_mod.guards_from_config({'quota_guards': {'stale_after_min': 10}})
        self.assertEqual(g['stale_after_min'], 10)

    def test_a_reading_older_than_the_threshold_is_stale(self):
        g = quota_mod.guards_from_config({})
        old = {'five_h_pct': 38, 'seven_d_pct': 89, 'polled_at': ago(45)}
        self.assertIsNotNone(quota_mod.stale_since(old, g))
        self.assertIsNone(quota_mod.stale_since({'five_h_pct': 1, 'polled_at': ago(5)}, g))
        # a source that names no poll time is taken as current
        self.assertIsNone(quota_mod.stale_since({'five_h_pct': 1}, g))
        self.assertIsNone(quota_mod.stale_since(None, g))

    def test_stale_percentages_do_not_band(self):
        g = quota_mod.guards_from_config({})
        state, why = quota_mod.band({'five_h_pct': 99, 'seven_d_pct': 99, 'polled_at': ago(300)}, g)
        self.assertEqual(state, quota_mod.FREE)
        self.assertIn('stale since', why)
        state, _ = quota_mod.band({'five_h_pct': 99, 'seven_d_pct': 1, 'polled_at': ago(1)}, g)
        self.assertEqual(state, quota_mod.STOP)


class TestPoolFallsBackToSessionLimits(HomeCase):
    def pool(self, reading, limits=None):
        src = quota_mod.FakeQuotaSource({'w1': reading})
        return pool_mod.Pool([pool_mod.Account('w1', cap=2)], quota_source=src,
                             guards=quota_mod.guards_from_config({}), limits=limits or {})

    def test_stale_reading_neither_stops_nor_budgets(self):
        p = self.pool({'five_h_pct': 99, 'seven_d_pct': 99, 'polled_at': ago(120)})
        self.assertEqual(p.band(p.accounts[0])[0], quota_mod.FREE)
        fits, _why, _total = p.headroom(p.accounts[0], 'spec', 'claude-opus')
        self.assertTrue(fits)

    def test_a_session_limit_still_stops_a_stale_account(self):
        until = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')
        p = self.pool({'five_h_pct': 1, 'seven_d_pct': 1, 'polled_at': ago(120)},
                      limits={'w1': {'until': until}})
        self.assertEqual(p.band(p.accounts[0])[0], quota_mod.STOP)

    def test_an_api_429_is_a_spent_window(self):
        for text in ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
                     'Claude AI usage limit reached|1759000000'):
            rec = {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': text}
            self.assertEqual(runtime_mod.failure_reason(rec), headroom.QUOTA_EXHAUSTED, text)


class TestStatusShowsStale(HomeCase):
    def test_quota_row_says_stale_since(self):
        from asf.views import status
        polled = ago(270)
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 38, 'seven_d_pct': 89, 'polled_at': polled}):
            cell = status.quota_cell(cfg)
        self.assertEqual(cell, f'w1 38%/89% stale since {local_hm(polled)}')

    def test_fresh_reading_unchanged(self):
        from asf.views import status
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 12, 'seven_d_pct': 40, 'polled_at': ago(2)}):
            self.assertEqual(status.quota_cell(cfg), 'w1 12%/40%')


if __name__ == '__main__':
    unittest.main()
