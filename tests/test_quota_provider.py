"""tests/test_quota_provider.py — F-0138 Task 1: the account declares its provider *kind*, and a
window's dollars are read and learned per kind instead of once for the whole pool.

Three classes, each a direct ``unittest.TestCase`` (PD12 — none subclasses another module's
harness): ``ProviderConfigTests`` (the kind on the account and its config check),
``WindowDollarsTests`` (``CostTable.share`` and ``five_h_usd_by_provider``) and
``SampleRecordTests`` (the samples file carries the kind, and a line written before this card
folds into ``default``).
"""
import datetime
import json
import os
import shutil
import tempfile
import unittest

from asf import env
from asf.workers import headroom
from asf.workers import pool as pool_mod

UTC = datetime.timezone.utc


class ProviderConfigTests(unittest.TestCase):
    def test_account_from_dict_reads_its_provider_kind(self):
        a = pool_mod.Account.from_dict({'name': 'a', 'provider': 'plan-small'})
        self.assertEqual(a.provider, 'plan-small')

    def test_an_account_naming_none_is_the_default_kind(self):
        a = pool_mod.Account.from_dict({'name': 'a'})
        self.assertEqual(a.provider, pool_mod.DEFAULT_PROVIDER)

    def test_pool_providers_maps_every_account_to_its_kind(self):
        a = pool_mod.Account('a', provider='plan-small')
        b = pool_mod.Account('b')
        p = pool_mod.Pool([a, b])
        self.assertEqual(p.providers(), {'a': 'plan-small', 'b': pool_mod.DEFAULT_PROVIDER})

    def test_a_string_provider_is_fine(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'a', 'provider': 'plan-small'}]}}
        self.assertEqual(env.validate_worker_pool(cfg), [])

    def test_a_non_string_provider_is_one_problem(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'a', 'provider': 7}]}}
        problems = env.validate_worker_pool(cfg)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][0], 'worker_pool.accounts[a].provider')

    def test_a_blank_provider_is_one_problem(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'a', 'provider': '  '}]}}
        problems = env.validate_worker_pool(cfg)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][0], 'worker_pool.accounts[a].provider')


class WindowDollarsTests(unittest.TestCase):
    def table(self, window_usd=None, usd=None, five_h_usd=None):
        return headroom.CostTable(window_usd=window_usd, usd=usd, five_h_usd=five_h_usd)

    def test_each_kind_prices_the_same_launch_at_its_own_scale(self):
        t = self.table(window_usd={'plan-small': {'five_h': 12}, 'plan-large': {'five_h': 48}},
                       usd={('spec', 'opus'): 4.80})
        self.assertEqual(t.share('spec', 'Opus', 'plan-small', 'five_h'), 40)
        self.assertEqual(t.share('spec', 'Opus', 'plan-large', 'five_h'), 10)

    def test_a_kind_named_nowhere_falls_back_to_the_pool_wide_five_h_usd(self):
        t = self.table(usd={('spec', 'opus'): 4.80}, five_h_usd=48.0)
        self.assertEqual(t.share('spec', 'Opus', 'unknown-kind', 'five_h'), 10)

    def test_an_unconfigured_long_window_derives_from_that_kinds_five_h(self):
        t = self.table(window_usd={'plan-small': {'five_h': 12}}, usd={('spec', 'opus'): 4.80})
        self.assertEqual(round(t.share('spec', 'Opus', 'plan-small', 'seven_d'), 1), 1.2)

    def test_an_explicit_long_window_dollar_value_overrides_the_derivation(self):
        t = self.table(window_usd={'plan-large': {'five_h': 48, 'seven_d': 1200}},
                       usd={('spec', 'opus'): 4.80})
        self.assertEqual(t.share('spec', 'Opus', 'plan-large', 'seven_d'), 0.4)

    def test_a_window_with_neither_dollars_nor_a_default_share_is_none(self):
        t = self.table(usd={('spec', 'opus'): 4.80})
        self.assertIsNone(t.share('spec', 'Opus', 'plan-small', 'made_up_window'))

    def test_five_h_usd_by_provider_gives_each_kind_its_own_median(self):
        def interval_samples(acct, provider, usd_per_run):
            samples, runs = [], []
            for i in range(3):
                a = datetime.datetime(2026, 9, 25, 8 + i, 0, tzinfo=UTC)
                b = a + datetime.timedelta(minutes=30)
                samples += [{'ts': a.isoformat(), 'account': acct, 'provider': provider,
                            'five_h_pct': 10 * i},
                           {'ts': b.isoformat(), 'account': acct, 'provider': provider,
                            'five_h_pct': 10 * i + 4}]
                runs.append({'account': acct,
                            'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                            'ended': (a + datetime.timedelta(minutes=25)).isoformat(),
                            'usd': usd_per_run})
            return samples, runs

        s1, r1 = interval_samples('a', 'plan-small', 2.0)
        s2, r2 = interval_samples('b', 'plan-large', 4.0)
        got = headroom.five_h_usd_by_provider(s1 + s2, r1 + r2)
        self.assertEqual(got['plan-small'], 50.0)
        self.assertEqual(got['plan-large'], 100.0)


class SampleRecordTests(unittest.TestCase):
    def setUp(self):
        self._home = env.ASF_HOME
        self._tmp = tempfile.mkdtemp(prefix='quota_provider_')
        env.ASF_HOME = self._tmp

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_line_carries_the_provider_and_every_window(self):
        headroom.record_samples(
            {'a': {'five_h_pct': 51, 'seven_d_pct': 22, 'seven_d_model_pct': None}},
            providers={'a': 'plan-small'}, now=headroom.now_utc())
        [rec] = headroom.read_samples()
        self.assertEqual(rec['provider'], 'plan-small')
        self.assertEqual(rec['five_h_pct'], 51)
        self.assertEqual(rec['seven_d_pct'], 22)
        self.assertIsNone(rec['seven_d_model_pct'])

    def test_a_line_written_without_a_provider_folds_into_default(self):
        path = headroom.samples_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines, runs = [], []
        for i in range(3):
            a = datetime.datetime(2026, 9, 25, 8 + i, 0, tzinfo=UTC)
            b = a + datetime.timedelta(minutes=30)
            lines.append(json.dumps({'ts': a.isoformat(), 'account': 'a', 'five_h_pct': 10 * i}))
            lines.append(json.dumps({'ts': b.isoformat(), 'account': 'a',
                                     'five_h_pct': 10 * i + 4}))
            runs.append({'account': 'a', 'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                        'ended': (a + datetime.timedelta(minutes=25)).isoformat(), 'usd': 2.0})
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        samples = headroom.read_samples()
        self.assertTrue(all('provider' not in s for s in samples))
        self.assertEqual(headroom.five_h_usd_by_provider(samples, runs), {'default': 50.0})

    def test_an_unreadable_account_writes_no_line(self):
        headroom.record_samples({'a': None, 'b': {'five_h_pct': None}}, providers={'a': 'x'})
        self.assertEqual(headroom.read_samples(), [])


if __name__ == '__main__':
    unittest.main()
