"""The 5h window as a budget (inbox: "the wave overloads one account's quota window").

* a per-launch cost estimate per (kind, model): the fixed default table while history is thin,
  else the median dollars of recent runs over ``quota_guards.five_h_usd`` — itself estimated from
  the account's own ``five_h_pct`` samples against the dollars its runs spent in between;
* the wave places a launch on an account only while ``now + committed + running allowance +
  this launch`` stays under the 5h guard, else it waits with the ``quota: …`` line;
* a session ending on a session/usage limit is ``quota-exhausted``: no hold, no round, no
  attempt counted; its account stops until the reset the message names.
"""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.workers import headroom
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod

UTC = datetime.timezone.utc
LIMIT_TEXT = "You've hit your session limit · resets 3:20pm (Europe/Copenhagen)"


def at(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))


def spec_row(job, item):
    return pool_mod.parse_row(f'STARVED → SPEC {item} "a feature"   → launch {job} (Opus)')


class TestResetParse(unittest.TestCase):
    def test_the_session_limit_message_names_its_reset_in_its_own_zone(self):
        # 11:37Z is 13:37 in Copenhagen (CEST, +2): the reset is 15:20 local = 13:20Z
        self.assertEqual(headroom.reset_at(LIMIT_TEXT, at('2026-09-25T11:37:00Z')),
                         at('2026-09-25T13:20:00Z'))

    def test_a_reset_just_passed_is_today_not_tomorrow(self):
        self.assertEqual(headroom.reset_at(LIMIT_TEXT, at('2026-09-25T13:40:00Z')),
                         at('2026-09-25T13:20:00Z'))

    def test_a_reset_long_past_is_the_next_day(self):
        self.assertEqual(headroom.reset_at(LIMIT_TEXT, at('2026-09-25T20:00:00Z')),
                         at('2026-09-26T13:20:00Z'))

    def test_hour_only_and_no_zone(self):
        got = headroom.reset_at('usage limit reached · resets 11am (UTC)', at('2026-09-25T09:00:00Z'))
        self.assertEqual(got, at('2026-09-25T11:00:00Z'))

    def test_no_reset_named_is_none(self):
        self.assertIsNone(headroom.reset_at('You have hit your limit', at('2026-09-25T09:00:00Z')))

    def test_the_result_is_classed_quota_exhausted(self):
        rec = {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': LIMIT_TEXT}
        self.assertEqual(runtime_mod.failure_reason(rec), headroom.QUOTA_EXHAUSTED)
        self.assertFalse(runtime_mod.result_ok(rec))
        self.assertEqual(lifecycle.outcome_class(f'failed: {headroom.QUOTA_EXHAUSTED}'),
                         headroom.QUOTA_EXHAUSTED)


class TestCostTable(unittest.TestCase):
    def test_the_default_table(self):
        t = headroom.CostTable()
        self.assertEqual(t.cost('spec', 'claude-opus-5'), 10)
        self.assertEqual(t.cost('spec', 'Opus'), 10)
        self.assertEqual(t.cost('review', 'claude-opus-5'), 6)
        self.assertEqual(t.cost('correct', 'claude-sonnet-5'), 4)
        self.assertEqual(t.cost('task', 'Sonnet'), 4)
        self.assertGreater(t.cost('spec', None), 0)  # an observed session of unknown model

    def test_history_with_a_dollar_window(self):
        runs = [{'kind': 'spec', 'model': 'claude-opus-5', 'usd': u} for u in (4.0, 6.0, 5.0)]
        t = headroom.estimate(runs, five_h_usd=50.0)
        self.assertEqual(t.cost('spec', 'claude-opus-5'), 10)       # median 5 / 50
        self.assertEqual(t.cost('review', 'claude-opus-5'), 6)      # no history: the default
        self.assertEqual(t.source, 'history')

    def test_thin_history_keeps_the_default(self):
        runs = [{'kind': 'spec', 'model': 'claude-opus-5', 'usd': 20.0}]
        self.assertEqual(headroom.estimate(runs, five_h_usd=50.0).cost('spec', 'opus'), 10)

    def test_no_dollar_window_keeps_the_default(self):
        runs = [{'kind': 'spec', 'model': 'claude-opus-5', 'usd': 20.0}] * 5
        self.assertEqual(headroom.estimate(runs, five_h_usd=None).cost('spec', 'opus'), 10)

    def test_the_dollar_window_from_five_h_samples(self):
        # three quiet intervals on one account: one run each, 2$ moved the window 4 points
        samples, runs = [], []
        for i in range(3):
            a = datetime.datetime(2026, 9, 25, 8 + i, 0, tzinfo=UTC)
            b = a + datetime.timedelta(minutes=30)
            samples += [{'ts': a.isoformat(), 'account': 'x', 'five_h_pct': 10 * i},
                        {'ts': b.isoformat(), 'account': 'x', 'five_h_pct': 10 * i + 4}]
            runs.append({'account': 'x', 'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                         'ended': (a + datetime.timedelta(minutes=25)).isoformat(), 'usd': 2.0})
        self.assertEqual(headroom.five_h_usd_from_samples(samples, runs), 50.0)

    def test_an_interval_with_a_run_of_unknown_cost_is_skipped(self):
        a = datetime.datetime(2026, 9, 25, 8, tzinfo=UTC)
        b = a + datetime.timedelta(minutes=30)
        samples = [{'ts': a.isoformat(), 'account': 'x', 'five_h_pct': 0},
                   {'ts': b.isoformat(), 'account': 'x', 'five_h_pct': 30}]
        runs = [{'account': 'x', 'started': a.isoformat(), 'ended': None, 'usd': None}]
        self.assertIsNone(headroom.five_h_usd_from_samples(samples, runs))


class TestPlacement(unittest.TestCase):
    def pool(self, accounts, usage, live=(), guard=65, limits=None):
        cfg = {'quota_guards': {'five_h': guard}}
        return pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource(usage),
                             guards=quota_mod.guards_from_config(cfg), live=live,
                             limits=limits)

    def test_the_13_26_wave_is_spread_by_headroom(self):
        a = pool_mod.Account('a', cap=8)
        b = pool_mod.Account('b', cap=8)
        p = self.pool([a, b], {'a': {'five_h_pct': 51, 'seven_d_pct': 10},
                               'b': {'five_h_pct': 30, 'seven_d_pct': 10}})
        got = []
        for i in range(5):
            acct, why = p.pick_account('spec', 'Opus')
            got.append(acct.name if acct else why)
            if acct:
                p.take(acct, 'claude-opus-5', f'spec-{i}', kind='spec')
        # a (51%) takes one spec (61%), b (30%) takes three (60%); the fifth fits nowhere
        self.assertEqual(sorted(got[:4]), ['a', 'b', 'b', 'b'])
        # the wait names the account closest to fitting: b at 30 + 30 + 10 = 70
        self.assertEqual(got[4], 'headroom: b would exceed 65% (now 30%, +30% committed, '
                                 '+10% this launch)')

    def test_running_sessions_hold_an_allowance(self):
        a = pool_mod.Account('a', cap=8)
        live = [{'account': 'a', 'model': 'claude-opus-5', 'kind': 'spec', 'job': f'r{i}'}
                for i in range(4)]
        p = self.pool([a], {'a': {'five_h_pct': 40, 'seven_d_pct': 0}}, live=live)
        # 40 + 4 × 10 × 0.5 = 60; a spec more is 70 — over
        acct, why = p.pick_account('spec', 'Opus')
        self.assertIsNone(acct)
        self.assertEqual(why, 'headroom: a would exceed 65% (now 40%, +20% committed, '
                              '+10% this launch)')
        # a sonnet correction still fits: 64
        self.assertEqual(p.pick_account('correct', 'Sonnet')[0].name, 'a')

    def test_an_account_stopped_by_a_session_limit_is_skipped_until_its_reset(self):
        a, b = pool_mod.Account('a', cap=8), pool_mod.Account('b', cap=8)
        until = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        p = self.pool([a, b], {'a': {'five_h_pct': 0, 'seven_d_pct': 0},
                               'b': {'five_h_pct': 60, 'seven_d_pct': 0}},
                      limits={'a': {'until': until}})
        state, why = p.band(a)
        self.assertEqual(state, quota_mod.STOP)
        self.assertIn('session limit until', why)
        acct, why = p.pick_account('spec', 'Opus')
        self.assertIsNone(acct)
        # a quota wait, never a page: the reset is known
        self.assertNotIn('NEEDS OPERATOR', why)
        self.assertTrue(why.startswith(('quota: ', 'headroom: ')), why)

    def test_a_past_limit_no_longer_stops(self):
        a = pool_mod.Account('a', cap=8)
        p = self.pool([a], {'a': {'five_h_pct': 0, 'seven_d_pct': 0}},
                      limits={'a': {'until': '2020-01-01T00:00:00Z'}})
        self.assertEqual(p.pick_account('spec', 'Opus')[0].name, 'a')


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='headroom_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {'repo_dir': self.tmp, 'main': 'main'})
        os.makedirs(env.state_dir(self.product), exist_ok=True)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestExhaustedRun(HomeCase):
    def test_limits_are_recorded_and_read_back(self):
        headroom.record_limit('a', at('2026-09-25T13:20:00Z'), job='spec-f-1', product='sample')
        now = at('2026-09-25T13:00:00Z')
        self.assertEqual(headroom.active_limits(now)['a']['until'], '2026-09-25T13:20:00Z')
        self.assertEqual(headroom.active_limits(at('2026-09-25T13:21:00Z')), {})

    def test_the_account_of_an_exhausted_run_is_stopped_until_the_reset(self):
        run = {'job': 'spec-f-1', 'account': 'a', 'item': 'F-0001'}
        rec = {'type': 'result', 'is_error': True, 'result': LIMIT_TEXT}
        line = headroom.note_exhausted(self.product, run, rec, now=at('2026-09-25T11:37:00Z'))
        self.assertIn('a stopped until', line)
        self.assertEqual(headroom.active_limits(at('2026-09-25T12:00:00Z'))['a']['until'],
                         '2026-09-25T13:20:00Z')

    def test_no_reset_named_stops_the_account_for_the_default_hold(self):
        run = {'job': 'spec-f-1', 'account': 'a'}
        rec = {'type': 'result', 'is_error': True, 'result': 'usage limit reached'}
        headroom.note_exhausted(self.product, run, rec, now=at('2026-09-25T11:00:00Z'))
        self.assertEqual(headroom.active_limits(at('2026-09-25T11:30:00Z'))['a']['until'],
                         '2026-09-25T12:00:00Z')

    def ledger(self, *lines):
        path = pool_mod.sessions_path(self.product)
        with open(path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path

    def test_an_exhausted_run_is_no_attempt(self):
        path = self.ledger(
            {'job': 'fix-b-1', 'item': 'B-0001', 'started': '2026-09-25T10:00:00Z', 'pid': 1},
            {'job': 'fix-b-1', 'ended': '2026-09-25T10:10:00Z',
             'end_reason': f'failed: {headroom.QUOTA_EXHAUSTED}'},
            {'job': 'fix-b-1', 'item': 'B-0001', 'started': '2026-09-25T11:00:00Z', 'pid': 2},
            {'job': 'fix-b-1', 'ended': '2026-09-25T11:10:00Z', 'end_reason': 'failed'})
        self.assertEqual(lifecycle.attempts(path), {'B-0001': 1})

    def test_an_exhausted_run_does_not_answer_a_correction(self):
        corr = {'kind': 'landing-gate', 'text': 'fix the gate', 'at': '2026-09-25T10:30:00Z'}
        path = self.ledger(
            {'job': 'spec-f-1', 'item': 'F-0001', 'started': '2026-09-25T10:00:00Z', 'pid': 1},
            {'job': 'spec-f-1', 'ended': '2026-09-25T10:20:00Z', 'end_reason': 'finished'},
            {'job': 'spec-f-1', 'correction': corr, 'rounds': 1},
            {'job': 'correct-f-1', 'item': 'F-0001', 'started': '2026-09-25T10:40:00Z', 'pid': 2},
            {'job': 'correct-f-1', 'ended': '2026-09-25T10:50:00Z',
             'end_reason': f'failed: {headroom.QUOTA_EXHAUSTED}'})
        run = lifecycle.latest(path)['spec-f-1']
        self.assertEqual(lifecycle.pending_correction(run, path)['text'], 'fix the gate')

    def test_an_exhausted_correction_holds_nothing(self):
        from asf.tick import step_health
        path = self.ledger(
            {'job': 'spec-f-1', 'item': 'F-0001', 'started': '2026-09-25T10:00:00Z', 'pid': 1},
            {'job': 'spec-f-1', 'ended': '2026-09-25T10:20:00Z', 'end_reason': 'dead pid',
             'corrected': True},
            {'job': 'spec-f-1-correction', 'item': 'F-0001', 'started': '2026-09-25T10:40:00Z',
             'pid': 2},
            {'job': 'spec-f-1-correction', 'ended': '2026-09-25T10:50:00Z',
             'end_reason': f'failed: {headroom.QUOTA_EXHAUSTED}'})
        ctx = mock.Mock(product=self.product)
        step_health.hold_failed_corrections(ctx, lifecycle.latest(path), out=lambda s: None)
        self.assertIsNone(lifecycle.latest(path)['spec-f-1'].get('correction'))


try:
    from test_workers import Home as RepoHome, feature_row
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home as RepoHome, feature_row


class TestHealthEndsAnExhaustedRun(RepoHome):
    def test_no_hold_the_account_stopped_the_worktree_kept(self):
        from asf.workers import health as health_mod
        from asf.workers import spawn as spawn_mod
        rt = runtime_mod.FakeRuntime([{'ok': False, 'pid': 31, 'result': LIMIT_TEXT}])
        rec = spawn_mod.spawn(self.product, feature_row('spec-f-0001'),
                              pool_mod.Account('acct-a'), 'b', runtime=rt, cfg=self.cfg)
        with open(os.path.join(rec['worktree'], 'partial.md'), 'w') as f:
            f.write('half a spec\n')
        lines = []
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lines.append)
        run = pool_mod.load_sessions(self.product)['spec-f-0001']
        self.assertEqual(run['end_reason'], f'failed: {headroom.QUOTA_EXHAUSTED}')
        self.assertFalse(run.get('correction'))
        self.assertIn('acct-a', headroom.active_limits(at('2020-01-01T00:00:00Z')))
        self.assertTrue(any(w == 'quota' and 'acct-a stopped until' in d for _j, w, d in found))
        self.assertFalse([f for f in found if f[1] in ('held', 'reaped')], found)
        self.assertTrue(os.path.isfile(os.path.join(rec['worktree'], 'partial.md')))


class TestStatusCell(HomeCase):
    def test_a_stopped_account_shows_its_reset(self):
        from asf.views import status
        until = datetime.datetime.now(UTC) + datetime.timedelta(minutes=90)
        headroom.record_limit('w1', until, job='j', product='sample')
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 100, 'seven_d_pct': 40}):
            cell = status.quota_cell(cfg)
        local = until.astimezone().strftime('%H:%M')
        self.assertEqual(cell, f'w1 100%/40% stop — resets {local}')

    def test_a_stop_by_the_reading_shows_the_sources_reset_when_it_names_one(self):
        from asf.views import status
        cfg = {'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        reset = '2026-09-25T13:20:00Z'
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 97, 'seven_d_pct': 40,
                                         'five_h_resets_at': reset}):
            cell = status.quota_cell(cfg)
        local = at(reset).astimezone().strftime('%H:%M')
        self.assertEqual(cell, f'w1 97%/40% stop — resets {local}')


class TestTheRealLimiterIsNamed(unittest.TestCase):
    """a product's 15:35 wave (2026-09-26): 3 launches on acct-a and 1 on acct-d left both at 4/4; every
    later row waited with ``quota: acct-c stopped until 17:10`` — the session-limit branch ran
    before the seat check, so the wait named an idle, stopped account instead of the full seats."""

    def pool(self, accounts, usage, live=(), limits=None):
        return pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource(usage),
                             guards=quota_mod.guards_from_config({}), live=live, limits=limits)

    def until(self):
        return (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')

    def test_the_15_35_wave_waits_on_the_full_seats_not_on_anette(self):
        names = ('acct-a', 'acct-b', 'acct-c', 'acct-d', 'acct-e')
        acc = [pool_mod.Account(n, cap=4) for n in names]
        live = [{'account': n, 'model': 'claude-sonnet-5'} for n in ('acct-a', 'acct-d') for _ in range(4)]
        until = self.until()
        p = self.pool(acc, {'acct-a': {'five_h_pct': 3, 'seven_d_pct': 6},
                            'acct-b': {'five_h_pct': 0, 'seven_d_pct': 100},
                            'acct-c': {'five_h_pct': 42, 'seven_d_pct': 72},
                            'acct-d': {'five_h_pct': 17, 'seven_d_pct': 22},
                            'acct-e': {'five_h_pct': 100, 'seven_d_pct': 88}},
                      live=live, limits={'acct-c': {'until': until},
                                         'acct-e': {'until': until}})
        acct, why = p.pick_account('review', 'claude-sonnet-5', lane='local')
        self.assertIsNone(acct)
        label = headroom.reset_label(until)
        self.assertEqual(why, 'pool full — accounts at cap: acct-a 4/4, acct-d 4/4; the rest stopped: '
                              'acct-b (seven_d_pct 100 ≥ 95), '
                              f'acct-c (session limit until {label}), '
                              f'acct-e (session limit until {label})')

    def test_seats_and_headroom_together_name_both(self):
        a, d = pool_mod.Account('acct-a', cap=4), pool_mod.Account('acct-d', cap=4)
        live = [{'account': 'acct-a', 'model': 'claude-opus-5', 'kind': 'spec'}] * 4
        p = self.pool([a, d], {'acct-a': {'five_h_pct': 3, 'seven_d_pct': 6},
                                    'acct-d': {'five_h_pct': 88, 'seven_d_pct': 22}}, live=live)
        acct, why = p.pick_account('spec', 'claude-opus-5')
        self.assertIsNone(acct)
        _fits, headroom_why, _total = p.headroom(d, 'spec', 'claude-opus-5')
        self.assertTrue(headroom_why.startswith('headroom: acct-d would exceed 95% (now 88%'))
        self.assertEqual(why, f'pool full — accounts at cap: acct-a 4/4; {headroom_why}')

    def test_only_a_session_limit_left_still_names_the_reset(self):
        a = pool_mod.Account('a', cap=4)
        until = self.until()
        p = self.pool([a], {'a': {'five_h_pct': 0, 'seven_d_pct': 0}},
                      limits={'a': {'until': until}})
        self.assertEqual(p.pick_account('review', 'sonnet'),
                         (None, f'quota: a stopped until {headroom.reset_label(until)} '
                                '(session limit)'))


class TestOneStopSource(HomeCase):
    """``asf workers quota`` and the capacity share read the session-limit stop the pool and the
    status row read (``quota-limits.json``) — at 15:42 quota said acct-c ``free`` while status
    said ``acct-c stop — resets 17:10``, and the fair share counted acct-c's 4 seats."""

    def test_the_quota_table_shows_a_session_limit_stop(self):
        import argparse
        import contextlib
        import io
        from asf.workers import cmd_quota
        until = datetime.datetime.now(UTC) + datetime.timedelta(minutes=90)
        headroom.record_limit('w1', until, job='j', product='sample')
        cfg = {'worker_pool': {'accounts': [{'name': 'w1', 'role': 'worker', 'cap': 4}],
                               'quota_command': 'true {account}', 'sessions': 'fake'}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch('asf.workers._product', return_value=self.product), \
             mock.patch('asf.workers.spawn.load_cfg', return_value=cfg), \
             mock.patch.object(quota_mod.CommandQuotaSource, 'read',
                               lambda self, a: {'five_h_pct': 42, 'seven_d_pct': 72}):
            cmd_quota(argparse.Namespace(product='sample'))
        label = until.astimezone().strftime('%H:%M')
        self.assertIn(f'| w1 | worker | 0 / 4 | 42 | 72 | stop — session limit until {label} |',
                      buf.getvalue())

    def test_a_session_limited_account_has_no_usable_slot(self):
        from asf import capacity
        until = datetime.datetime.now(UTC) + datetime.timedelta(minutes=90)
        headroom.record_limit('b', until, job='j', product='sample')
        cfg = {'worker_pool': {'accounts': [{'name': n, 'cap': 4} for n in ('a', 'b')]}}
        source = quota_mod.FakeQuotaSource({'a': {'five_h_pct': 0, 'seven_d_pct': 0},
                                            'b': {'five_h_pct': 0, 'seven_d_pct': 0}})
        self.assertEqual(capacity.usable_slots(cfg, source), 4)


if __name__ == '__main__':
    unittest.main()
