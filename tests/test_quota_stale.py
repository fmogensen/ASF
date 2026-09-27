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

    def test_stale_percentages_under_the_guard_do_not_band(self):
        g = quota_mod.guards_from_config({})
        state, why = quota_mod.band({'five_h_pct': 92, 'seven_d_pct': 94, 'polled_at': ago(300)}, g)
        self.assertEqual(state, quota_mod.FREE)
        self.assertIn('stale since', why)
        state, _ = quota_mod.band({'five_h_pct': 99, 'seven_d_pct': 1, 'polled_at': ago(1)}, g)
        self.assertEqual(state, quota_mod.STOP)


def ahead(minutes):
    return (datetime.datetime.now(UTC) + datetime.timedelta(minutes=minutes)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


class TestStaleStopHolds(unittest.TestCase):
    """A stale reading that was at or over a stop guard when it was read keeps the account
    stopped until the reset that window carried (the regression: a 7-day window read at 100%
    went stale and the wave seated sessions that died on ``You've hit your weekly limit``)."""

    def setUp(self):
        self.g = quota_mod.guards_from_config({})

    def test_a_stale_seven_day_stop_holds_until_its_reset(self):
        u = {'five_h_pct': 0, 'seven_d_pct': 100, 'polled_at': ago(300),
             'seven_d_resets_at': ahead(3 * 24 * 60)}
        state, why = quota_mod.band(u, self.g)
        self.assertEqual(state, quota_mod.STOP)
        self.assertIn('seven_d_pct 100', why)
        self.assertIn('stale since', why)
        self.assertEqual(quota_mod.stale_stop_until(u, self.g), u['seven_d_resets_at'])

    def test_the_stop_ends_at_the_reset_it_carried(self):
        u = {'five_h_pct': 0, 'seven_d_pct': 100, 'polled_at': ago(300),
             'seven_d_resets_at': ago(5)}
        self.assertEqual(quota_mod.band(u, self.g)[0], quota_mod.FREE)
        self.assertIsNone(quota_mod.stale_stop_until(u, self.g))

    def test_a_stale_stop_with_no_reset_holds_for_the_window_length(self):
        # no reset named: the window cannot have reset before polled_at + its length
        u = {'five_h_pct': 0, 'seven_d_pct': 95, 'polled_at': ago(300)}
        self.assertEqual(quota_mod.band(u, self.g)[0], quota_mod.STOP)
        five = {'five_h_pct': 99, 'seven_d_pct': 1, 'polled_at': ago(301)}
        self.assertEqual(quota_mod.band(five, self.g)[0], quota_mod.FREE)
        five['polled_at'] = ago(120)
        self.assertEqual(quota_mod.band(five, self.g)[0], quota_mod.STOP)

    def test_the_guard_is_the_configured_stop(self):
        g = quota_mod.guards_from_config({'quota_guards': {'seven_d': 99}})
        u = {'five_h_pct': 0, 'seven_d_pct': 97, 'polled_at': ago(300)}
        self.assertEqual(quota_mod.band(u, g)[0], quota_mod.FREE)


class TestPoolFallsBackToSessionLimits(HomeCase):
    def pool(self, reading, limits=None):
        src = quota_mod.FakeQuotaSource({'w1': reading})
        return pool_mod.Pool([pool_mod.Account('w1', cap=2)], quota_source=src,
                             guards=quota_mod.guards_from_config({}), limits=limits or {})

    def test_stale_reading_under_the_guard_neither_stops_nor_budgets(self):
        p = self.pool({'five_h_pct': 94, 'seven_d_pct': 94, 'polled_at': ago(120)})
        self.assertEqual(p.band(p.accounts[0])[0], quota_mod.FREE)
        fits, _why, _total = p.headroom(p.accounts[0], 'spec', 'claude-opus')
        self.assertTrue(fits)

    def test_a_session_limit_still_stops_a_stale_account(self):
        until = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')
        p = self.pool({'five_h_pct': 1, 'seven_d_pct': 1, 'polled_at': ago(120)},
                      limits={'w1': {'until': until}})
        self.assertEqual(p.band(p.accounts[0])[0], quota_mod.STOP)

    def test_a_stale_stop_keeps_the_account_out_of_the_wave(self):
        p = self.pool({'five_h_pct': 0, 'seven_d_pct': 100, 'polled_at': ago(300),
                       'seven_d_resets_at': ahead(60 * 24)})
        self.assertEqual(p.band(p.accounts[0])[0], quota_mod.STOP)
        self.assertIsNone(p.pick_account('fix-bug', 'claude-sonnet-5')[0])

    def test_an_api_429_is_a_spent_window(self):
        for text in ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
                     'Claude AI usage limit reached|1759000000'):
            rec = {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': text}
            self.assertEqual(runtime_mod.failure_reason(rec), headroom.QUOTA_EXHAUSTED, text)


class TestLimitDeathStopsAtOnce(HomeCase):
    """A run that died on a usage limit stops its account before the next launch is placed —
    not when its own product's health step next judges it. The regression: one product's run
    died on ``You've hit your weekly limit`` and another product's wave, two minutes later,
    seated a session on the same account."""

    def write_run(self, product, job, result_text, pid=999999):
        import json, os
        from asf import env
        d = os.path.join(env.ASF_HOME, 'state', product)
        os.makedirs(d, exist_ok=True)
        log = os.path.join(self.tmp, f'{job}.jsonl')
        with open(log, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True,
                                'result': result_text}) + '\n')
        with open(os.path.join(d, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'account': 'w1', 'pid': pid, 'log': log,
                                'item': 'T-1', 'kind': 'coder', 'started': ago(1),
                                'session': f'{product}/{job}@20260927T000000Z'}) + '\n')

    def test_a_dead_run_on_a_usage_limit_stops_its_account(self):
        from asf.workers import lifecycle
        self.write_run('other', 'coder-t-1', "You've hit your weekly limit · resets 8am (UTC)")
        lines = lifecycle.note_spent_windows(alive=lambda _pid: False)
        self.assertEqual(len(lines), 1)
        self.assertIn('w1', headroom.active_limits())

    def test_a_dead_run_on_other_grounds_stops_nothing(self):
        from asf.workers import lifecycle
        self.write_run('other', 'coder-t-2', 'Invalid API key · Please run /login')
        self.assertEqual(lifecycle.note_spent_windows(alive=lambda _pid: False), [])
        self.assertEqual(headroom.active_limits(), {})

    def test_the_pool_reads_the_stop_the_death_left(self):
        self.write_run('other', 'coder-t-3', "You've hit your weekly limit · resets 8am (UTC)")
        cfg = {'worker_pool': {'accounts': [{'name': 'w1', 'cap': 2}]}}
        with mock.patch('asf.workers.lifecycle.pid_alive', lambda _pid: False), \
                mock.patch('asf.workers.observe.read', lambda *a, **k: ([], '')):
            p = pool_mod.Pool.from_config(cfg, self.product)
        self.assertEqual(p.band(p.accounts[0])[0], quota_mod.STOP)


class TestStatusShowsStale(HomeCase):
    def test_quota_row_says_stale_since(self):
        from asf.views import status
        polled = ago(270)
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 38, 'seven_d_pct': 89, 'polled_at': polled}):
            cell = status.quota_cell(cfg)
        self.assertEqual(cell, f'w1 38%/89% stale since {local_hm(polled)}')

    def test_quota_row_names_a_stale_stop_and_its_reset(self):
        from asf.views import status
        polled, reset = ago(300), ahead(60 * 24 * 3)
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 0, 'seven_d_pct': 100, 'polled_at': polled,
                                         'seven_d_resets_at': reset}):
            cell = status.quota_cell(cfg)
        self.assertTrue(cell.startswith(f'w1 0%/100% stop stale since {local_hm(polled)} — resets '),
                        cell)

    def test_fresh_reading_unchanged(self):
        from asf.views import status
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 12, 'seven_d_pct': 40, 'polled_at': ago(2)}):
            self.assertEqual(status.quota_cell(cfg), 'w1 12%/40%')


if __name__ == '__main__':
    unittest.main()
