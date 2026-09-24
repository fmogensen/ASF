"""asf.capacity — the resolver: the session ceiling, the CI ceiling, the reserve, the overlay.
A temp ``ASF_HOME`` with product files and ``state/<p>/sessions.jsonl`` ledgers stands in for
what is in flight; the CI source is stubbed so no test ever shells out to ``gh``."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import capacity
from asf import env
from asf.workers import pool as pool_mod


def product(name='asf', data=None):
    return env.Product(name, data or {})


class CountingCiSource:
    """A fake CI source that counts its own calls, so a test can prove it was never asked."""

    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    def read(self, product):
        self.calls += 1
        return self.value


class Home(unittest.TestCase):
    """A temp ``ASF_HOME`` with ``~/.ASF/products/*.yaml`` and session ledgers writable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_product(self, name, text=''):
        with open(os.path.join(self.tmp, 'products', f'{name}.yaml'), 'w', encoding='utf-8') as f:
            f.write(text or f'product: {name}\n')

    def launch(self, name, job, **fields):
        rec = dict(fields, job=job, started=pool_mod.now_iso(), pid=1)
        pool_mod.append_session(name, rec)


class ResolveSessions(Home):
    def test_product_key_wins_over_operator_default(self):
        p = product('asf', {'capacity': {'sessions': 3}})
        cfg = {'capacity': {'per_product': {'sessions': 2}}}
        self.assertEqual(capacity.product_sessions(p, cfg), (3, 'product'))

    def test_operator_default_when_the_product_declares_none(self):
        p = product('asf', {})
        cfg = {'capacity': {'per_product': {'sessions': 2}}}
        self.assertEqual(capacity.product_sessions(p, cfg), (2, 'operator default'))

    def test_deprecated_feeder_capacity_is_still_honoured(self):
        p = product('asf', {})
        cfg = {'feeder': {'capacity': 5}}
        self.assertEqual(capacity.product_sessions(p, cfg), (5, 'feeder.capacity'))

    def test_default_four_when_nothing_is_configured(self):
        self.assertEqual(capacity.product_sessions(product(), {}), (4, 'default'))
        self.assertEqual(capacity.DEFAULT_SESSIONS, 4)

    def test_total_less_other_products_inflight_caps_the_ceiling(self):
        self.write_product('asf')
        self.write_product('web')
        self.launch('web', 'j1')
        self.launch('web', 'j2')
        p = product('asf', {'capacity': {'sessions': 4}})
        cfg = {'capacity': {'total': {'sessions': 3}}}
        r = capacity.resolve(p, cfg=cfg, ci_source=CountingCiSource())
        self.assertEqual(r.sessions, 1)
        self.assertEqual(r.sessions_bound, 'operator total')

    def test_a_full_total_elsewhere_resolves_to_zero_not_negative(self):
        self.write_product('asf')
        self.write_product('web')
        for i in range(5):
            self.launch('web', f'j{i}')
        p = product('asf', {'capacity': {'sessions': 4}})
        cfg = {'capacity': {'total': {'sessions': 3}}}
        r = capacity.resolve(p, cfg=cfg, ci_source=CountingCiSource())
        self.assertEqual(r.sessions, 0)

    def test_bound_by_names_the_term_that_won(self):
        self.write_product('asf')
        p = product('asf', {'capacity': {'sessions': 4}})
        cfg = {'capacity': {'total': {'sessions': 10}}}
        r = capacity.resolve(p, cfg=cfg, ci_source=CountingCiSource())
        self.assertEqual((r.sessions, r.sessions_bound), (4, 'product'))


class ResolveCi(Home):
    def test_no_ci_keys_means_no_ceiling_and_no_source_call(self):
        self.write_product('asf')
        p = product('asf', {})
        src = CountingCiSource(5)
        r = capacity.resolve(p, cfg={}, ci_source=src)
        self.assertEqual((r.ci, r.ci_bound, r.ci_inflight), (None, None, None))
        self.assertEqual(src.calls, 0)

    def test_product_ceiling_capped_by_the_operator_total(self):
        self.write_product('asf')
        p = product('asf', {'capacity': {'ci': 5}})
        cfg = {'capacity': {'total': {'ci': 2}}}
        r = capacity.resolve(p, cfg=cfg, ci_source=CountingCiSource(1))
        self.assertEqual((r.ci, r.ci_bound), (2, 'operator total'))

    def test_unreadable_source_is_none_not_an_exception(self):
        self.write_product('asf')
        p = product('asf', {'repo_slug': 'acme/x', 'ci': {'workflow': 'ci.yml'},
                            'capacity': {'ci': 5}})
        with mock.patch('subprocess.run', side_effect=OSError('boom')):
            self.assertIsNone(capacity.CiRuns().read(p))
        with mock.patch('subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 1)):
            self.assertIsNone(capacity.CiRuns().read(p))
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=1, stdout='')):
            self.assertIsNone(capacity.CiRuns().read(p))
        cfg = {'capacity': {'total': {'ci': 2}}}
        with mock.patch('subprocess.run', side_effect=OSError('boom')):
            r = capacity.resolve(p, cfg=cfg, ci_source=capacity.CiRuns())
        self.assertIsNone(r.ci_inflight)
        self.assertEqual(r.ci, 2)  # unknown never lowers ci (D8)

    def test_ci_none_product_never_calls_gh(self):
        self.write_product('asf')
        with mock.patch('subprocess.run', side_effect=AssertionError('gh must not be called')):
            p = product('asf', {'ci': 'none', 'capacity': {'ci': 5}})
            cfg = {'capacity': {'total': {'ci': 2}}}
            r = capacity.resolve(p, cfg=cfg)
            self.assertIsNone(r.ci_inflight)


class Reserve(Home):
    def test_capacity_reserve_wins_over_worker_pool_reserve(self):
        cfg = {'capacity': {'reserve_for_s1': {'local': 3}},
               'worker_pool': {'reserve_for_s1': {'local': 9, 'cloud': 9}}}
        self.assertEqual(capacity.reserve(cfg), {'local': 3, 'cloud': 1})

    def test_worker_pool_reserve_is_still_read(self):
        cfg = {'worker_pool': {'reserve_for_s1': {'local': 0, 'cloud': 2}}}
        self.assertEqual(capacity.reserve(cfg), {'local': 0, 'cloud': 2})

    def test_default_reserve_when_neither_is_set(self):
        self.assertEqual(capacity.reserve({}), {'local': 1, 'cloud': 1})
        self.assertEqual(capacity.reserve({}), capacity.DEFAULT_RESERVE)


class Overlay(Home):
    def test_only_resolved_keys_are_exported(self):
        r = capacity.Resolved(sessions=3, sessions_bound='product', ci=None, ci_bound=None,
                              ci_inflight=None, batch={}, reserve={'local': 1, 'cloud': 1})
        self.assertEqual(capacity.env_overlay(r, product('asf')),
                         {'ASF_PRODUCT': 'asf', 'ASF_CAPACITY_SESSIONS': '3'})

    def test_batch_shape_reaches_the_environment(self):
        r = capacity.Resolved(sessions=3, sessions_bound='product', ci=2, ci_bound='product',
                              ci_inflight=1, batch={'per_run': 8, 'parallel': 2, 'runners': 4},
                              reserve={'local': 1, 'cloud': 1})
        self.assertEqual(capacity.env_overlay(r, product('asf')), {
            'ASF_PRODUCT': 'asf', 'ASF_CAPACITY_SESSIONS': '3', 'ASF_CAPACITY_CI': '2',
            'ASF_CAPACITY_BATCH_PER_RUN': '8', 'ASF_CAPACITY_BATCH_PARALLEL': '2',
            'ASF_CAPACITY_RUNNERS': '4'})
        self.assertEqual(capacity.batch_shape(product('asf', {'capacity': {'batch': {
            'per_run': 8, 'parallel': 2, 'runners': 4, 'unknown': 1}}}), {}),
            {'per_run': 8, 'parallel': 2, 'runners': 4})


if __name__ == '__main__':
    unittest.main()


WAVE_PRODUCT = '''product: {name}
clocks:
  tick:
    steps: [record, wave]
    every: 10m
'''


def pool_cfg(sessions=8):
    """4 accounts × cap 4 and the operator's per-product default of ``sessions``."""
    return {'capacity': {'per_product': {'sessions': sessions}},
            'worker_pool': {'accounts': [{'name': n, 'cap': 4}
                                         for n in ('acct-a', 'acct-b', 'acct-c', 'acct-d')]}}


def bands():
    """acct-a in cooldown (7-day 91 ≥ 90), acct-d at stop (7-day 96 ≥ 95), the other two free."""
    from asf.workers import quota as quota_mod
    return quota_mod.FakeQuotaSource({'acct-a': {'five_h_pct': 10, 'seven_d_pct': 91},
                                      'acct-d': {'five_h_pct': 10, 'seven_d_pct': 96}})


class FairShare(Home):
    """Two products on one pool split what the pool can take now, not the configured ceiling."""

    def test_usable_counts_cap_when_free_one_in_cooldown_none_at_stop(self):
        self.assertEqual(capacity.usable_slots(pool_cfg(), bands()), 4 + 4 + 1 + 0)

    def test_two_active_products_each_get_the_ceil_of_half_the_usable_pool(self):
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web', WAVE_PRODUCT.format(name='web'))
        for name in ('asf', 'web'):
            r = capacity.resolve(env.load_product(name), pool_cfg(), quota_source=bands())
            self.assertEqual(r.sessions, 5)
            self.assertEqual(r.sessions_bound, 'fair share')
            self.assertEqual(r.ceiling, 8)
            self.assertEqual(r.fair_share_reason,
                             'fair share: 5 of 9 usable slots across 2 products')

    def test_a_single_active_product_keeps_its_configured_ceiling(self):
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web')  # no clock runs its wave: not competing for the pool
        r = capacity.resolve(env.load_product('asf'), pool_cfg(), quota_source=bands())
        self.assertEqual((r.sessions, r.sessions_bound), (8, 'operator default'))
        self.assertEqual(r.fair_share_reason, '')

    def test_a_command_owned_wave_is_not_active(self):
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('bots', WAVE_PRODUCT.format(name='bots')
                           + 'steps:\n  wave: bash wave.sh\n')
        self.assertEqual(capacity.active_products('asf'), ['asf'])

    def test_a_share_above_the_configured_ceiling_changes_nothing(self):
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web', WAVE_PRODUCT.format(name='web'))
        r = capacity.resolve(env.load_product('asf'), pool_cfg(sessions=3), quota_source=bands())
        self.assertEqual((r.sessions, r.sessions_bound), (3, 'operator default'))

    def test_over_its_share_a_product_launches_nothing_and_keeps_its_sessions(self):
        from asf.feeder import tiers
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web', WAVE_PRODUCT.format(name='web'))
        for i in range(8):
            self.launch('asf', f'task-t-{i}', account='acct-b' if i < 4 else 'acct-c')
        r = capacity.resolve(env.load_product('asf'), pool_cfg(), quota_source=bands())
        inflight = pool_mod.live_sessions('asf')
        self.assertEqual(tiers.free_slots(inflight, r.sessions), 0)
        self.assertEqual(capacity.inflight_sessions('asf'), 8)  # nothing ended, nothing killed

    def test_the_wave_names_the_share_on_each_row_it_cut(self):
        from types import SimpleNamespace as NS
        from asf.tick import step_wave
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web', WAVE_PRODUCT.format(name='web'))
        r = capacity.resolve(env.load_product('asf'), pool_cfg(), quota_source=bands())
        rows = [NS(item_id=f'T-{i}', kind='TASK → BUILD', brief_kind='task', launches=True)
                for i in range(8)]
        with mock.patch('asf.feeder.rows.plan_rows',
                        side_effect=lambda items, p, running, cap, **kw: rows[:cap]):
            held = step_wave.held_by_share({}, None, [], r, rows[:r.sessions], {})
        self.assertEqual([h.item_id for h in held], ['T-5', 'T-6', 'T-7'])

    def test_capacity_table_shows_the_effective_ceiling(self):
        from asf.views import capacity as view
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web', WAVE_PRODUCT.format(name='web'))
        with mock.patch('asf.workers.quota.source_from_config', return_value=bands()):
            text = view.render([env.load_product('asf')], pool_cfg())
        line = [ln for ln in text.splitlines() if ln.startswith('asf ')][0]
        self.assertIn(' 5 ', line)
        self.assertIn('fair share of 9 usable / 2 products (configured 8)', line)
