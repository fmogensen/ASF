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

    def test_weights_split_the_pool_by_the_operators_priority(self):
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('web', WAVE_PRODUCT.format(name='web') + 'capacity:\n  weight: 3\n')
        web = capacity.resolve(env.load_product('web'), pool_cfg(), quota_source=bands())
        asf = capacity.resolve(env.load_product('asf'), pool_cfg(), quota_source=bands())
        self.assertEqual((web.sessions, asf.sessions), (7, 3))   # ceil(9·3/4), ceil(9·1/4)

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


def two_free_pool_cfg():
    """The 20:16 pool: five accounts of cap 4, three stopped by their quota guards, and the
    operator's per-product default of 8."""
    return {'capacity': {'per_product': {'sessions': 8}},
            'worker_pool': {'accounts': [{'name': n, 'cap': 4}
                                         for n in ('acct-a', 'acct-b', 'acct-c', 'acct-d',
                                                   'acct-e')]}}


def three_stopped():
    from asf.workers import quota as quota_mod
    return quota_mod.FakeQuotaSource({n: {'five_h_pct': 10, 'seven_d_pct': 96}
                                      for n in ('acct-c', 'acct-d', 'acct-e')})


class IdlePoolLaunches(Home):
    """The 20:16 wave: two products, two usable accounts of cap 4, nothing live, two sessions
    finished and awaiting harvest, two rows parked by an approval hold — and nothing launched,
    every free row told ``fair share: 4 of 8``. An idle pool launches up to the share, and a
    product whose partner is idle borrows the partner's idle share."""

    def setUp(self):
        super().setUp()
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('bots', WAVE_PRODUCT.format(name='bots'))

    def rows(self):
        from asf.feeder import rows as R
        def row(tier, iid, kind):
            return R.Row(tier=tier, kind=kind, item_id=iid, feature_id='', action=R.LAUNCH,
                         brief_kind='task', branch='', reason='')
        return [row(1, 'B-0087', 'BUG → CORRECT'), row(1, 'B-0101', 'BUG → CORRECT'),
                row(2, 'B-0102', 'GROOM → ADJUDICATE'), row(2, 'T-0118', 'TASK → CORRECT'),
                row(2, 'T-0185', 'TASK → BUILD'), row(2, 'T-0075', 'TASK → BUILD'),
                row(2, 'T-0107', 'TASK → BUILD')]

    def finished_awaiting_harvest(self):
        """Two ledger rows with no ``ended`` whose pids are gone: finished, not in flight."""
        from asf.workers import lifecycle
        for job in ('correct-b-0099', 'coder-t-0087'):
            pool_mod.append_session('asf', {'job': job, 'item': job[-6:].upper(),
                                            'started': pool_mod.now_iso(), 'pid': 999999})
        return lifecycle.inflight(pool_mod.sessions_path('asf'), alive=lambda pid: False)

    def test_parked_rows_take_no_slot_so_an_idle_pool_launches_its_share(self):
        from asf.feeder import tiers
        running = self.finished_awaiting_harvest()
        self.assertEqual(running, [])
        r = capacity.resolve(env.load_product('asf'), two_free_pool_cfg(),
                             quota_source=three_stopped())
        self.assertEqual((r.sessions, r.usable, r.active), (4, 8, 2))
        out = tiers.select(self.rows(), running, r.sessions, held={'B-0087', 'B-0101'})
        launching = [x.item_id for x in out if x.launches and x.item_id not in ('B-0087', 'B-0101')]
        self.assertEqual(launching, ['B-0102', 'T-0118', 'T-0185', 'T-0075'])
        self.assertEqual([x.item_id for x in out[:2]], ['B-0087', 'B-0101'])  # still said

    def test_a_parked_s1_does_not_hold_the_features_back(self):
        from asf.feeder import rows as R
        from asf.feeder import tiers
        s1 = R.Row(tier=0, kind='BUG → FIX', item_id='B-0001', feature_id='', action=R.LAUNCH,
                   brief_kind='fix-bug', branch='', reason='')
        out = tiers.select([s1] + self.rows()[2:], [], 2, held={'B-0001'})
        self.assertEqual([x.item_id for x in out], ['B-0001', 'B-0102', 'T-0118'])

    def test_an_idle_partner_lends_its_whole_share(self):
        capacity.write_demand('bots', 0, 0)
        r = capacity.resolve(env.load_product('asf'), two_free_pool_cfg(),
                             quota_source=three_stopped())
        self.assertEqual((r.sessions, r.sessions_bound), (8, 'operator default'))

    def test_a_partner_lends_only_what_it_does_not_want(self):
        capacity.write_demand('bots', 0, 3)
        r = capacity.resolve(env.load_product('asf'), two_free_pool_cfg(),
                             quota_source=three_stopped())
        self.assertEqual((r.sessions, r.borrowed), (5, 1))
        self.assertEqual(r.fair_share_reason, 'fair share: 5 of 8 usable slots across 2 '
                                              'products, 1 borrowed from idle products')

    def test_a_busy_partner_lends_nothing(self):
        for i in range(4):
            self.launch('bots', f'task-t-{i}', account='acct-a')
        capacity.write_demand('bots', 4, 0)
        r = capacity.resolve(env.load_product('asf'), two_free_pool_cfg(),
                             quota_source=three_stopped())
        self.assertEqual((r.sessions, r.borrowed), (4, 0))

    def test_a_stale_demand_record_lends_nothing(self):
        capacity.write_demand('bots', 0, 0)
        later = capacity._now() + __import__('datetime').timedelta(
            seconds=capacity.DEMAND_FRESH_S + 60)
        with mock.patch.object(capacity, '_now', return_value=later):
            r = capacity.resolve(env.load_product('asf'), two_free_pool_cfg(),
                                 quota_source=three_stopped())
        self.assertEqual((r.sessions, r.borrowed), (4, 0))



class ShareNeverOvershoots(Home):
    """2026-09-26 08:17 and 08:42 (a product tick): after waves held by host pressure, the next
    wave launched 11 and then 11 again under shares of 12 and 13 ("9 borrowed from idle
    products"), and the status Capacity row read ``sessions 10/5`` minutes later. The partner was
    not idle: its wave, held by host pressure, recorded ``in flight 4, wanted 2`` — and its claim
    was read as its in flight *now* plus that ``wanted``, so each of its sessions that ended lent
    one more slot it would have refilled but for the pressure; and once it launched its wanted
    rows they counted twice (in flight now and in ``wanted``), which is the ``/5``. A partner's
    claim is what its own wave recorded it would hold — in flight then plus wanted — never less
    than it holds now."""

    def setUp(self):
        super().setUp()
        self.write_product('asf', WAVE_PRODUCT.format(name='asf'))
        self.write_product('bots', WAVE_PRODUCT.format(name='bots'))

    def launch(self, name, job, **fields):
        pool_mod.append_session(name, dict(fields, job=job, started=pool_mod.now_iso(), pid=1))

    def end(self, name, job):
        pool_mod.append_session(name, {'job': job, 'ended': pool_mod.now_iso(),
                                       'end_reason': 'finished'})

    def resolve(self, name='asf'):
        return capacity.resolve(env.load_product(name), two_free_pool_cfg(),
                                quota_source=three_stopped())

    def queue(self, n):
        from asf.feeder import rows as R
        return [R.Row(tier=2, kind='TASK → BUILD', item_id=f'T-{i:04d}', feature_id='',
                      action=R.LAUNCH, brief_kind='task', branch='', reason='')
                for i in range(n)]

    def held_partner_whose_sessions_end(self):
        """bots: 3 in flight, its wave held by host pressure with 1 row wanted — then all 3 end."""
        for i in range(3):
            self.launch('bots', f'task-t-{i}')
        capacity.write_demand('bots', 3, 1)
        for i in range(3):
            self.end('bots', f'task-t-{i}')
        self.assertEqual(capacity.inflight_sessions('bots'), 0)

    def test_a_partner_held_by_pressure_lends_nothing_as_its_sessions_end(self):
        self.held_partner_whose_sessions_end()
        r = self.resolve()
        self.assertEqual((r.sessions, r.borrowed), (4, 0))

    def test_a_held_then_released_wave_with_a_long_queue_launches_only_share_less_live(self):
        from asf.feeder import tiers
        self.held_partner_whose_sessions_end()
        for i in range(2):
            self.launch('asf', f'coder-t-9{i}')
        r = self.resolve()
        running = capacity.live_sessions('asf')
        out = tiers.select(self.queue(12), running, r.sessions)
        self.assertEqual(sum(1 for x in out if x.launches), r.sessions - len(running))
        self.assertEqual(sum(1 for x in out if x.launches), 2)

    def test_a_partner_that_launched_its_wanted_rows_is_not_counted_twice(self):
        capacity.write_demand('bots', 0, 3)
        for i in range(3):
            self.launch('bots', f'task-t-{i}')
        r = self.resolve()
        self.assertEqual((r.sessions, r.borrowed), (5, 1))

    def test_a_partner_over_its_record_claims_what_it_holds(self):
        capacity.write_demand('bots', 1, 0)
        for i in range(3):
            self.launch('bots', f'task-t-{i}')
        self.assertEqual((self.resolve().sessions, self.resolve().borrowed), (5, 1))

    def test_the_capacity_row_and_the_wave_count_the_same_live_sessions(self):
        from asf.tick import step_wave
        from asf.views import status
        capacity.write_demand('bots', 3, 1)
        for i in range(3):
            self.launch('asf', f'coder-t-9{i}')
        self.end('asf', 'coder-t-90')
        p = env.load_product('asf')
        self.assertEqual(step_wave.inflight(p), capacity.live_sessions('asf'))
        self.assertEqual(len(step_wave.inflight(p)), capacity.inflight_sessions('asf'))
        r = self.resolve()
        with mock.patch('asf.workers.quota.source_from_config', return_value=three_stopped()):
            cell = status.capacity_cell(two_free_pool_cfg(), p)
        self.assertTrue(cell.startswith(f'sessions 2/{r.sessions}'), cell)

if __name__ == '__main__':
    unittest.main()
