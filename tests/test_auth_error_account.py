"""An auth error on a session launch takes its account out of the pool at once (defect #30).

One account answered every launch with an auth refusal (``oauth_org_not_allowed``) and burned
seven jobs over ~2.5 h before anyone noticed. Now:

* a run whose result — or, with no result line, whose raw log — matches an auth-error pattern
  (``worker_pool.auth_error_patterns``, generic defaults) ends ``failed: auth``;
* its account is unusable from that FIRST failure (``~/.ASF/state/account-auth.json``) until
  ``asf workers enable <account>``;
* ONE alarm is raised — the first failure's line, and one doctor row — never one per failure;
* the run is no attempt and no round: the item relaunches on another account, or, with one
  account, waits (not burned) and the wait says so.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import config_keys
from asf import env
from asf.workers import account_auth
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod

ORG_TEXT = 'API Error: 403 {"type":"error","error":{"type":"forbidden","message":"oauth_org_not_allowed"}}'
FREE = {'five_h_pct': 0, 'seven_d_pct': 0}


def result(text, ok=False):
    return {'type': 'result', 'subtype': 'success' if ok else 'error', 'is_error': not ok,
            'result': text}


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='authacct_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {'repo_dir': self.tmp, 'main': 'main'})
        os.makedirs(env.state_dir(self.product), exist_ok=True)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestClassification(HomeCase):
    def test_auth_refusals_are_classed_auth(self):
        for text in (ORG_TEXT,
                     'API Error: 401 unauthorized',
                     'HTTP 403 Forbidden: subscription disabled',
                     'Your subscription has been disabled',
                     'Invalid API key · Please run /login',
                     'authentication_error: invalid bearer token',
                     # B-0041: "disabled" before "subscription", not after — the two patterns
                     # above only match "subscription ... disabled", never this order
                     'Error: your organization has disabled this workspace\'s subscription access'):
            with self.subTest(text=text):
                self.assertEqual(runtime_mod.failure_reason(result(text)), account_auth.AUTH)

    def test_the_organization_disabled_subscription_text_matches_in_log_too(self):
        # B-0041: the dead-pid path reads a run's raw log through account_auth.in_log — the same
        # MATCHER failure_reason uses — so a session that died with no result line still blocks
        # its account on this phrasing, not just a live result.
        log = os.path.join(self.tmp, 'died.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write('This organization has disabled the subscription access for this seat\n')
        self.assertTrue(account_auth.in_log(log))

    def test_prose_that_only_mentions_the_words_is_not(self):
        for text in ('the forbidden list grew by one', 'unauthorized edits are reverted',
                     'line 401 of the parser'):
            with self.subTest(text=text):
                self.assertIsNone(runtime_mod.failure_reason(result(text)))

    def test_the_config_key_replaces_the_defaults(self):
        with open(env.config_path(), 'w') as f:
            f.write('worker_pool:\n  auth_error_patterns: ["seat revoked"]\n')
        self.assertEqual(runtime_mod.failure_reason(result('Seat REVOKED for this user')),
                         account_auth.AUTH)
        self.assertIsNone(runtime_mod.failure_reason(result(ORG_TEXT)))

    def test_the_key_is_registered(self):
        cfg = {'worker_pool': {'auth_error_patterns': ['x']}}
        self.assertEqual(config_keys.unknown_keys(cfg), [])

    def test_a_dead_run_with_the_refusal_only_in_its_raw_log_is_auth(self):
        log = os.path.join(self.tmp, 'run.log')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}) + '\n')
            f.write('Error: request failed with status 403 oauth_org_not_allowed\n')
        ev = mock.Mock(alive=False, result=None)
        self.assertEqual(lifecycle.judge({'job': 'j', 'log': log}, ev),
                         f'failed: {account_auth.AUTH}')

    def test_a_dead_run_without_it_is_still_a_dead_pid(self):
        log = os.path.join(self.tmp, 'run.log')
        with open(log, 'w') as f:
            f.write('Segmentation fault\n')
        ev = mock.Mock(alive=False, result=None)
        self.assertEqual(lifecycle.judge({'job': 'j', 'log': log}, ev), lifecycle.DEAD_PID)


class TestOneAlarm(HomeCase):
    def test_the_first_failure_blocks_the_account_and_alarms_once(self):
        run = {'job': 'spec-f-1', 'account': 'a', 'item': 'F-0001'}
        first = account_auth.note(self.product, run, ORG_TEXT, accounts=('a', 'b'))
        self.assertTrue(first.startswith('ALARM '), first)
        self.assertIn('a unusable', first)
        self.assertIn('asf workers enable a', first)
        self.assertIn('a', account_auth.blocked())
        second = account_auth.note(self.product, dict(run, job='spec-f-2'), ORG_TEXT,
                                   accounts=('a', 'b'))
        self.assertNotIn('ALARM', second)

    def test_with_one_account_the_alarm_says_the_job_waits(self):
        line = account_auth.note(self.product, {'job': 'j', 'account': 'a'}, ORG_TEXT,
                                 accounts=('a',))
        self.assertIn('no other account', line)
        self.assertIn('waits', line)

    def test_enable_lifts_it(self):
        account_auth.note(self.product, {'job': 'j', 'account': 'a'}, ORG_TEXT)
        self.assertTrue(account_auth.enable('a'))
        self.assertEqual(account_auth.blocked(), {})
        self.assertFalse(account_auth.enable('a'))

    def test_a_run_finished_after_the_block_re_enables_it(self):
        account_auth.block('a', job='j')
        at = account_auth.blocked()['a']['at']
        self.assertIsNone(account_auth.proved({'account': 'a', 'started': '2000-01-01T00:00:00Z'}))
        self.assertIn('a', account_auth.blocked())
        line = account_auth.proved({'account': 'a', 'started': at[:-1] + '.5+00:00'})
        self.assertIn('re-enabled', line)
        self.assertEqual(account_auth.blocked(), {})

    def test_one_doctor_row_while_blocked(self):
        from asf import doctor
        self.assertIsNone(doctor.check_account_auth())
        account_auth.note(self.product, {'job': 'j1', 'account': 'a'}, ORG_TEXT)
        account_auth.note(self.product, {'job': 'j2', 'account': 'a'}, ORG_TEXT)
        ok, detail = doctor.check_account_auth()
        self.assertFalse(ok)
        self.assertIn('ALARM', detail)
        self.assertIn('asf workers enable a', detail)


class TestPool(HomeCase):
    def pool(self, accounts, blocked):
        return pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource(
            {a.name: FREE for a in accounts}), blocked=blocked)

    def test_the_blocked_account_is_skipped_and_the_job_goes_to_another(self):
        a, b = pool_mod.Account('a', cap=4), pool_mod.Account('b', cap=4)
        p = self.pool([a, b], {'a': {'at': '2026-10-06T00:00:00Z'}})
        self.assertEqual(p.band(a)[0], quota_mod.STOP)
        self.assertEqual(p.pick_account('spec', 'Opus')[0].name, 'b')

    def test_one_account_blocked_waits_and_says_so(self):
        a = pool_mod.Account('a', cap=4)
        acct, why = self.pool([a], {'a': {'at': '2026-10-06T00:00:00Z'}}).pick_account(
            'spec', 'Opus')
        self.assertIsNone(acct)
        self.assertIn('auth', why)
        self.assertIn('asf workers enable a', why)

    def test_no_block_is_a_no_op(self):
        a = pool_mod.Account('a', cap=4)
        self.assertEqual(self.pool([a], {}).pick_account('spec', 'Opus')[0].name, 'a')
        self.assertEqual(pool_mod.Pool([a], quota_source=quota_mod.FakeQuotaSource(
            {'a': FREE})).pick_account('spec', 'Opus')[0].name, 'a')

    def test_the_capacity_share_drops_its_seats(self):
        from asf import capacity
        account_auth.note(self.product, {'job': 'j', 'account': 'b'}, ORG_TEXT)
        cfg = {'worker_pool': {'accounts': [{'name': n, 'cap': 4} for n in ('a', 'b')]}}
        source = quota_mod.FakeQuotaSource({'a': FREE, 'b': FREE})
        self.assertEqual(capacity.usable_slots(cfg, source), 4)

    def test_the_quota_table_shows_it(self):
        from asf.workers import cmd_quota
        account_auth.note(self.product, {'job': 'j', 'account': 'w1'}, ORG_TEXT)
        cfg = {'worker_pool': {'accounts': [{'name': 'w1', 'role': 'worker', 'cap': 4}],
                               'sessions': 'fake'}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch('asf.workers._product', return_value=self.product), \
             mock.patch('asf.workers.spawn.load_cfg', return_value=cfg):
            cmd_quota(argparse.Namespace(product='sample'))
        self.assertIn('auth error', buf.getvalue())


class TestNotBurned(HomeCase):
    def ledger(self, *lines):
        path = pool_mod.sessions_path(self.product)
        with open(path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path

    def test_an_auth_failed_run_is_no_attempt(self):
        path = self.ledger(
            {'job': 'fix-b-1', 'item': 'B-0001', 'started': '2026-10-06T10:00:00Z', 'pid': 1},
            {'job': 'fix-b-1', 'ended': '2026-10-06T10:01:00Z',
             'end_reason': f'failed: {account_auth.AUTH}'},
            {'job': 'fix-b-1', 'item': 'B-0001', 'started': '2026-10-06T11:00:00Z', 'pid': 2},
            {'job': 'fix-b-1', 'ended': '2026-10-06T11:10:00Z', 'end_reason': 'failed'})
        self.assertEqual(lifecycle.attempts(path), {'B-0001': 1})


class TestEnableCommand(HomeCase):
    def run_cmd(self, name):
        from asf.workers import cmd_enable
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_enable(argparse.Namespace(account=name))
        return rc, buf.getvalue()

    def test_enable_clears_a_blocked_account(self):
        account_auth.note(self.product, {'job': 'j', 'account': 'a'}, ORG_TEXT)
        rc, out = self.run_cmd('a')
        self.assertEqual(rc, 0)
        self.assertIn('a', out)
        self.assertEqual(account_auth.blocked(), {})

    def test_enable_of_a_usable_account_says_so(self):
        rc, out = self.run_cmd('a')
        self.assertEqual(rc, 1)
        self.assertIn('not blocked', out)

    def test_the_subcommand_is_registered(self):
        from asf.workers import register
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='cmd')
        register(sub)
        args = parser.parse_args(['workers', 'enable', 'a'])
        self.assertEqual(args.account, 'a')


try:
    from test_workers import Home as RepoHome, feature_row
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home as RepoHome, feature_row


class TestHealthEndsAnAuthRun(RepoHome):
    def test_no_hold_the_account_blocked_one_alarm(self):
        from asf.workers import health as health_mod
        from asf.workers import spawn as spawn_mod
        rt = runtime_mod.FakeRuntime([{'ok': False, 'pid': 31, 'result': ORG_TEXT}])
        spawn_mod.spawn(self.product, feature_row('spec-f-0001'),
                        pool_mod.Account('acct-a'), 'b', runtime=rt, cfg=self.cfg)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['spec-f-0001']
        self.assertEqual(run['end_reason'], f'failed: {account_auth.AUTH}')
        self.assertFalse(run.get('correction'))
        self.assertIn('acct-a', account_auth.blocked())
        alarms = [d for _j, w, d in found if 'ALARM' in d]
        self.assertEqual(len(alarms), 1, found)
        self.assertFalse([f for f in found if f[1] == 'held'], found)
        self.assertEqual(lifecycle.attempts(pool_mod.sessions_path(self.product)), {})


if __name__ == '__main__':
    unittest.main()
