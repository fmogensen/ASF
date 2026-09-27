"""A credential probe is read-only and never prints a token — the surface Tasks 2, 4, 5 and 6 (the
command, the daily part, the doctor's row, the installer) are all built against
(:mod:`asf.credentials`)."""
import datetime
import os
import subprocess
import time
import unittest
from unittest import mock

from asf import credentials, env
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def fake_run(stdout='', stderr='', returncode=0):
    def run(cmd, env=None, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


class StubProduct:
    """What :func:`asf.credentials.for_product` and :func:`asf.credentials.probe_env` need — a
    stand-in for :class:`asf.env.Product` with no product file on disk."""

    def __init__(self, name='sample', repo_dir='/nowhere', credentials=()):
        self.name = name
        self.repo_dir = repo_dir
        self.credentials = list(credentials)


class ProbeTest(unittest.TestCase):
    def provider(self, probe='check-provider-a'):
        return credentials.Provider('provider-a', probe, 'renew-provider-a')

    def test_exit_0_with_an_expiry_line_is_valid_with_that_expiry(self):
        result = credentials.run_probe(self.provider(), {}, NOW,
                                       run=fake_run('expires 2026-12-01T00:00:00Z\n'))
        self.assertEqual(result.state, 'valid')
        self.assertEqual(result.expires, '2026-12-01T00:00:00Z')

    def test_exit_0_with_expires_never_has_no_days_left(self):
        result = credentials.run_probe(self.provider(), {}, NOW, run=fake_run('expires never\n'))
        self.assertEqual(result.state, 'valid')
        self.assertEqual(result.expires, 'never')
        self.assertIsNone(credentials.days_left(result, NOW))

    def test_exit_0_with_an_unreadable_line_is_valid_with_an_unknown_expiry(self):
        result = credentials.run_probe(self.provider(), {}, NOW, run=fake_run('ready\n'))
        self.assertEqual(result.state, 'valid')
        self.assertEqual(result.expires, '')

    def test_exit_1_is_invalid(self):
        result = credentials.run_probe(self.provider(), {}, NOW,
                                       run=fake_run('not signed in\n', returncode=1))
        self.assertEqual(result.state, 'invalid')

    def test_exit_2_is_broken(self):
        result = credentials.run_probe(self.provider(), {}, NOW,
                                       run=fake_run(stderr='boom\n', returncode=2))
        self.assertEqual(result.state, 'broken')

    def test_a_probe_that_sleeps_past_the_timeout_is_broken_and_returns_promptly(self):
        provider = self.provider(probe='sleep 5')
        env_map = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        start = time.monotonic()
        with mock.patch.object(credentials, 'PROBE_TIMEOUT_S', 0.2):
            result = credentials.run_probe(provider, env_map, NOW)
        elapsed = time.monotonic() - start
        self.assertEqual(result.state, 'broken')
        self.assertLess(elapsed, 3.0)   # well inside the real 5s sleep: timeout=, not ignored


class VerdictTest(unittest.TestCase):
    def result(self, expires='', state='valid'):
        return credentials.Result(provider='provider-a', state=state, expires=expires)

    def _in(self, days):
        return (NOW + datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def test_expiry_in_two_days_is_expiring_at_the_default_window(self):
        self.assertEqual(credentials.verdict(self.result(self._in(2)), NOW, 3), 'expiring')

    def test_expiry_in_four_days_is_ok_at_the_default_window(self):
        self.assertEqual(credentials.verdict(self.result(self._in(4)), NOW, 3), 'ok')

    def test_expiry_in_two_days_is_ok_at_a_one_day_window(self):
        self.assertEqual(credentials.verdict(self.result(self._in(2)), NOW, 1), 'ok')

    def test_an_unknown_expiry_is_unknown_never_expiring(self):
        self.assertEqual(credentials.verdict(self.result(''), NOW, 3), 'unknown')
        self.assertIsNone(credentials.days_left(self.result(''), NOW))

    def test_never_expires_is_unknown_too(self):
        self.assertEqual(credentials.verdict(self.result('never'), NOW, 3), 'unknown')
        self.assertIsNone(credentials.days_left(self.result('never'), NOW))

    def test_invalid_and_broken_pass_through_regardless_of_expiry(self):
        self.assertEqual(credentials.verdict(self.result(self._in(2), state='invalid'), NOW, 3),
                         'invalid')
        self.assertEqual(credentials.verdict(self.result('', state='broken'), NOW, 3), 'broken')

    def test_bad_collects_invalid_and_expiring_in_provider_order(self):
        expiring = self.result(self._in(2))
        ok = credentials.Result(provider='provider-b', state='valid', expires=self._in(30))
        invalid = credentials.Result(provider='provider-c', state='invalid')
        by_name = {
            'provider-a': credentials.Provider('provider-a', 'p', 'r', window_days=3),
            'provider-b': credentials.Provider('provider-b', 'p', 'r', window_days=3),
            'provider-c': credentials.Provider('provider-c', 'p', 'r', window_days=3),
        }
        self.assertEqual(
            credentials.bad([expiring, ok, invalid], providers_by_name=by_name, now=NOW),
            [expiring, invalid])


class ConfigTest(unittest.TestCase):
    def cfg(self, **credentials_section):
        return {'credentials': credentials_section} if credentials_section else {}

    def test_providers_reads_the_map_and_its_defaults(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a'},
            'provider-b': {'probe': 'check-b', 'renew': 'login-b', 'window_days': 7,
                          'account': 'lane-1'},
        })
        found = credentials.providers(cfg)
        self.assertEqual(set(found), {'provider-a', 'provider-b'})
        self.assertEqual(found['provider-a'].window_days, credentials.DEFAULT_WINDOW_DAYS)
        self.assertEqual(found['provider-a'].account, '')
        self.assertEqual(found['provider-b'].window_days, 7)
        self.assertEqual(found['provider-b'].account, 'lane-1')
        self.assertEqual(credentials.probe_every({}), credentials._seconds('1h'))
        self.assertEqual(credentials.probe_every(self.cfg(probe_every='30m')), 30 * 60)

    def test_a_missing_or_misshapen_section_is_no_providers_never_a_raise(self):
        self.assertEqual(credentials.providers(None), {})
        self.assertEqual(credentials.providers({'credentials': 'TODO'}), {})
        self.assertEqual(credentials.providers(self.cfg(providers='TODO')), {})

    def test_config_problems_names_a_provider_with_no_probe(self):
        cfg = self.cfg(providers={'provider-a': {'renew': 'login-a'}})
        self.assertIn(('credentials.providers.provider-a.probe', 'is required'),
                      credentials.config_problems(cfg))

    def test_config_problems_names_a_provider_with_no_renew(self):
        cfg = self.cfg(providers={'provider-a': {'probe': 'check-a'}})
        self.assertIn(('credentials.providers.provider-a.renew', 'is required'),
                      credentials.config_problems(cfg))

    def test_config_problems_names_a_bad_window_days(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a', 'window_days': 0}})
        self.assertIn(
            ('credentials.providers.provider-a.window_days', "must be a positive integer, not 0"),
            credentials.config_problems(cfg))

    def test_config_problems_names_a_bad_probe_every(self):
        cfg = self.cfg(probe_every='soon')
        self.assertIn(("credentials.probe_every", "must be '<n>s|m|h|d', not 'soon'"),
                      credentials.config_problems(cfg))

    def test_config_problems_names_an_account_no_pool_entry_names(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a', 'account': 'lane-9'}})
        self.assertIn(
            ('credentials.providers.provider-a.account', "'lane-9' is not a worker_pool account"),
            credentials.config_problems(cfg))

    def test_config_problems_names_a_secret_shaped_renew(self):
        secret_renew = 'echo ghp_' + 'A' * 40
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': secret_renew}})
        self.assertIn(
            ('credentials.providers.provider-a.renew',
             'carries a secret-shaped value — put the secret in a file'),
            credentials.config_problems(cfg))

    def test_for_product_returns_the_products_own_order(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a'},
            'provider-b': {'probe': 'check-b', 'renew': 'login-b'},
        })
        product = StubProduct(credentials=['provider-b', 'provider-a'])
        found = credentials.for_product(product, cfg)
        self.assertEqual([p.name for p in found], ['provider-b', 'provider-a'])

    def test_for_product_raises_on_a_name_config_does_not_define(self):
        cfg = self.cfg(providers={'provider-a': {'probe': 'check-a', 'renew': 'login-a'}})
        product = StubProduct(credentials=['provider-z'])
        with self.assertRaises(env.ConfigError) as cm:
            credentials.for_product(product, cfg)
        self.assertEqual(str(cm.exception),
                         'product.yaml credentials: provider-z is not a provider in '
                         'config.yaml credentials.providers')

    def test_probe_env_without_an_account_uses_the_worker_base(self):
        provider = credentials.Provider('provider-a', 'check-a', 'login-a')
        env_map = credentials.probe_env(provider, {}, StubProduct())
        self.assertEqual(env_map['GIT_TERMINAL_PROMPT'], '0')

    def test_probe_env_with_an_account_builds_the_accounts_own_environment(self):
        provider = credentials.Provider('provider-a', 'check-a', 'login-a', account='lane-1')
        cfg = {'worker_pool': {'accounts': [{'name': 'lane-1'}]}}
        env_map = credentials.probe_env(provider, cfg, StubProduct())
        self.assertEqual(env_map['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env_map['ASF_PRODUCT'], 'sample')

    def test_probe_env_raises_on_an_account_no_pool_entry_names(self):
        provider = credentials.Provider('provider-a', 'check-a', 'login-a', account='lane-9')
        with self.assertRaises(runtime_mod.AuthEnvError):
            credentials.probe_env(provider, {}, StubProduct())


if __name__ == '__main__':
    unittest.main()
