"""The window's dollars, read per provider kind (inbox: "two accounts on different plan sizes do
not have the same window, and one scalar prices a heavy spec the same on both").

* ``worker_pool.accounts[].provider`` — an opaque operator label naming what sort of account this
  is; unset is the kind ``DEFAULT_PROVIDER``. ``Pool.providers()`` maps every account to its kind;
  ``env.validate_worker_pool`` checks it is a non-blank string.
* ``CostTable.share(kind, model, provider, window)`` — the launch's own dollars over that
  window's dollars on that provider's own scale, first hit wins: the operator's
  ``quota_guards.window_usd[provider][window]``; that kind's own ``five_h`` (configured or
  learned) × :data:`WINDOW_RATIO`; the pool-wide ``five_h_usd`` × the same ratio; else the fixed
  table (``five_h``) or that same share spread over a longer window; else ``None``.
* ``record_samples`` writes every window and the provider kind it was read under, so a later
  reading can be attributed; a sample written before this card, or by an account naming none,
  folds into ``'default'``.
"""
import datetime
import json
import os
import tempfile
import unittest

from asf import env
from asf.workers import headroom
from asf.workers import pool as pool_mod
from tests.test_quota_headroom import at

UTC = datetime.timezone.utc


class ProviderConfigTests(unittest.TestCase):
    def test_account_from_dict_reads_the_provider_kind(self):
        acct = pool_mod.Account.from_dict({'name': 'a', 'provider': 'plan-small'})
        self.assertEqual(acct.provider, 'plan-small')

    def test_an_account_naming_none_is_the_default_kind(self):
        self.assertEqual(pool_mod.Account.from_dict({'name': 'a'}).provider,
                         pool_mod.DEFAULT_PROVIDER)
        self.assertEqual(pool_mod.Account('a').provider, pool_mod.DEFAULT_PROVIDER)

    def test_pool_providers_maps_every_account_to_its_kind(self):
        a = pool_mod.Account('a', provider='plan-small')
        b = pool_mod.Account('b')
        p = pool_mod.Pool([a, b])
        self.assertEqual(p.providers(), {'a': 'plan-small', 'b': 'default'})

    def test_a_string_provider_is_well_formed(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'a', 'provider': 'plan-small'}]}}
        self.assertEqual(env.validate_worker_pool(cfg), [])

    def test_a_malformed_provider_is_named(self):
        for provider in (7, '  '):
            problems = env.validate_worker_pool(
                {'worker_pool': {'accounts': [{'name': 'a', 'provider': provider}]}})
            self.assertEqual(len(problems), 1, problems)
            self.assertEqual(problems[0][0], 'worker_pool.accounts[a].provider')


class WindowDollarsTests(unittest.TestCase):
    def table(self, window_usd, five_h_usd=24.0):
        runs = [{'kind': 'spec', 'model': 'claude-opus-5', 'usd': u,
                'started': f'2026-09-25T0{i}:00:00Z'} for i, u in enumerate((4.0, 4.8, 6.0))]
        return headroom.estimate(runs, window_usd=window_usd, five_h_usd=five_h_usd)

    def test_the_dollar_median_prices_each_kind_s_own_window(self):
        t = self.table({'plan-small': {'five_h': 12}, 'plan-large': {'five_h': 48}})
        self.assertEqual(t.usd[('spec', 'opus')], 4.8)
        self.assertEqual(t.share('spec', 'Opus', 'plan-small', 'five_h'), 40)
        self.assertEqual(t.share('spec', 'Opus', 'plan-large', 'five_h'), 10)

    def test_usd_medians_are_learned_with_no_pool_wide_five_h_usd(self):
        t = self.table({'plan-small': {'five_h': 12}, 'plan-large': {'five_h': 48}},
                       five_h_usd=None)
        self.assertEqual(t.usd[('spec', 'opus')], 4.8)
        self.assertEqual(t.shares, {})
        self.assertEqual(t.share('spec', 'Opus', 'plan-small', 'five_h'), 40)
        self.assertEqual(t.share('spec', 'Opus', 'plan-large', 'five_h'), 10)

    def test_a_kind_named_nowhere_falls_back_to_the_pool_wide_five_h_usd(self):
        t = self.table({'plan-small': {'five_h': 12}})
        self.assertEqual(t.share('spec', 'Opus', 'plan-medium', 'five_h'), 20)  # 4.8*100/24

    def test_an_unconfigured_long_window_is_derived_from_the_kind_s_own_five_h(self):
        t = self.table({'plan-small': {'five_h': 12}})
        # 40 (plan-small's own five_h share) / 33.6 — not 1 (PD6's floor is a 5h-only floor),
        # and not the unrounded 1.19047...
        self.assertEqual(t.share('spec', 'Opus', 'plan-small', 'seven_d'), 1.2)

    def test_an_explicit_long_window_overrides_the_derived_one(self):
        t = self.table({'plan-large': {'five_h': 48, 'seven_d': 1200}})
        self.assertEqual(t.share('spec', 'Opus', 'plan-large', 'seven_d'), 0.4)  # 4.8*100/1200

    def test_a_window_with_neither_dollars_nor_a_default_share_is_none(self):
        t = self.table({'plan-small': {'five_h': 12}})
        self.assertIsNone(t.share('spec', 'Opus', 'plan-small', 'a-fourth-window'))

    def test_thin_history_falls_back_to_the_fixed_table(self):
        t = headroom.estimate([], window_usd={'plan-small': {'five_h': 12}}, five_h_usd=24.0)
        self.assertEqual(t.share('spec', 'Opus', 'plan-small', 'five_h'), 10)       # DEFAULT_COST
        self.assertEqual(t.share('spec', 'Opus', 'plan-small', 'seven_d'), 0.3)     # 10 / 33.6

    def test_five_h_usd_by_provider_gives_each_kind_its_own_median(self):
        samples, runs = [], []
        for i in range(3):
            a = datetime.datetime(2026, 9, 25, 8 + i, 0, tzinfo=UTC)
            b = a + datetime.timedelta(minutes=30)
            samples += [{'ts': a.isoformat(), 'account': 'x', 'provider': 'plan-small',
                        'five_h_pct': 10 * i},
                       {'ts': b.isoformat(), 'account': 'x', 'provider': 'plan-small',
                        'five_h_pct': 10 * i + 4}]
            runs.append({'account': 'x', 'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                        'ended': (a + datetime.timedelta(minutes=25)).isoformat(), 'usd': 2.0})
            samples += [{'ts': a.isoformat(), 'account': 'y', 'provider': 'plan-large',
                        'five_h_pct': 10 * i},
                       {'ts': b.isoformat(), 'account': 'y', 'provider': 'plan-large',
                        'five_h_pct': 10 * i + 4}]
            runs.append({'account': 'y', 'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                        'ended': (a + datetime.timedelta(minutes=25)).isoformat(), 'usd': 4.0})
        self.assertEqual(headroom.five_h_usd_by_provider(samples, runs),
                         {'plan-small': 50.0, 'plan-large': 100.0})

    def test_a_kind_with_too_few_intervals_has_no_entry(self):
        a = datetime.datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
        b = a + datetime.timedelta(minutes=30)
        samples = [{'ts': a.isoformat(), 'account': 'x', 'provider': 'plan-small', 'five_h_pct': 0},
                  {'ts': b.isoformat(), 'account': 'x', 'provider': 'plan-small', 'five_h_pct': 4}]
        runs = [{'account': 'x', 'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                'ended': (a + datetime.timedelta(minutes=25)).isoformat(), 'usd': 2.0}]
        self.assertEqual(headroom.five_h_usd_by_provider(samples, runs), {})


class SampleRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._home = env.ASF_HOME
        env.ASF_HOME = self._tmpdir.name

    def tearDown(self):
        env.ASF_HOME = self._home
        self._tmpdir.cleanup()

    def test_a_line_carries_the_provider_kind_and_every_window(self):
        headroom.record_samples(
            {'a': {'five_h_pct': 51, 'seven_d_pct': 22, 'seven_d_model_pct': None}},
            providers={'a': 'plan-small'}, now=at('2026-09-25T12:00:00Z'))
        self.assertEqual(headroom.read_samples(),
                         [{'ts': '2026-09-25T12:00:00Z', 'account': 'a', 'provider': 'plan-small',
                           'five_h_pct': 51, 'seven_d_pct': 22, 'seven_d_model_pct': None}])

    def test_an_account_providers_names_none_for_folds_into_default(self):
        headroom.record_samples(
            {'a': {'five_h_pct': 10, 'seven_d_pct': 0, 'seven_d_model_pct': 0}},
            now=at('2026-09-25T12:00:00Z'))
        self.assertEqual(headroom.read_samples()[0]['provider'], 'default')

    def test_a_legacy_line_with_no_provider_key_folds_into_default(self):
        path = headroom.samples_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        runs = []
        with open(path, 'w', encoding='utf-8') as f:
            for i in range(3):
                a = datetime.datetime(2026, 9, 25, 8 + i, 0, tzinfo=UTC)
                b = a + datetime.timedelta(minutes=30)
                f.write(json.dumps({'ts': a.isoformat(), 'account': 'x', 'five_h_pct': 10 * i}) + '\n')
                f.write(json.dumps({'ts': b.isoformat(), 'account': 'x',
                                    'five_h_pct': 10 * i + 4}) + '\n')
                runs.append({'account': 'x',
                            'started': (a + datetime.timedelta(minutes=5)).isoformat(),
                            'ended': (a + datetime.timedelta(minutes=25)).isoformat(), 'usd': 2.0})
        samples = headroom.read_samples()
        self.assertNotIn('provider', samples[0])
        self.assertEqual(headroom.five_h_usd_by_provider(samples, runs), {'default': 50.0})

    def test_an_account_reading_none_writes_no_line(self):
        headroom.record_samples({'a': None, 'b': {'five_h_pct': None, 'seven_d_pct': 10}},
                                now=at('2026-09-25T12:00:00Z'))
        self.assertEqual(headroom.read_samples(), [])


if __name__ == '__main__':
    unittest.main()
