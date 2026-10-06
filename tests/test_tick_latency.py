"""A tick launches first: the wave right after the record's fast parts, the bookkeeping after it
on its cadence (``every_n``, at least hourly), the span tick start → wave start measured
(``wave_latency``: the tick line, ``asf status``, the dwell watchdog) and the backfill reading
only the job logs that changed. 2026-10-06: one tick spent 22 minutes before its wave."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import dwell, env, scheduler, tokens
from asf.metrics import metrics
from asf.tick import cadence, steps, tick, wave_latency
from tests.test_dwell import NOW, FakeFacts
from tests.test_tick import TickTestCase


def rows(*names):
    return [(n, 'asf', None) for n in names]


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(env, 'ASF_HOME', self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.write_config('')

    def write_config(self, text):
        os.makedirs(os.path.dirname(env.config_path()), exist_ok=True)
        with open(env.config_path(), 'w') as f:
            f.write(text)


class WaveFirstOrderTests(HomeCase):
    def test_the_wave_moves_up_to_right_after_the_record(self):
        got = tick.wave_first(rows('record', 'health', 'groom', 'wave', 'prs', 'harvest'))
        self.assertEqual([r[0] for r in got],
                         ['record', 'wave', 'health', 'groom', 'prs', 'harvest'])

    def test_a_clock_without_the_record_runs_its_wave_first(self):
        got = tick.wave_first(rows('health', 'groom', 'wave', 'prs'))
        self.assertEqual([r[0] for r in got], ['wave', 'health', 'groom', 'prs'])

    def test_no_wave_no_change_and_the_flag_turns_it_off(self):
        self.assertEqual(tick.wave_first(rows('record', 'health')), rows('record', 'health'))
        self.write_config('tick:\n  wave_first: false\n')
        same = rows('record', 'health', 'groom', 'wave')
        self.assertEqual(tick.wave_first(same), same)


class CadenceTests(HomeCase):
    def setUp(self):
        super().setUp()
        # the clock lists its steps in its own order; --steps arrives in the manifest's
        self.product = env.Product('p', {'clocks': {'main': {
            'steps': ['wave', 'record', 'health'], 'every': '5m', 'every_n': {'health': 2}}}})

    def due_run(self, name, ticks, step_names=('record', 'health', 'wave'), start=NOW, gap=300):
        out = []
        for i in range(ticks):
            c = cadence.Cadence(self.product, list(step_names), now=start + i * gap,
                                out=lambda _l: None)
            out.append(c.due(name))
        return out

    def test_the_record_tail_defaults_to_every_third_tick(self):
        self.assertEqual(self.due_run('backfill', 7), [True, False, False, True, False, False, True])

    def test_an_unnamed_step_runs_every_tick(self):
        self.assertEqual(self.due_run('groom', 3), [True, True, True])

    def test_the_clock_sets_its_own_cadence_over_the_config(self):
        self.write_config('tick:\n  every_n: {health: 5, rollup: 1}\n')
        self.assertEqual(self.due_run('health', 4), [True, False, True, False])
        self.assertEqual(self.due_run('rollup', 3), [True, True, True])

    def test_a_deferred_part_still_runs_at_least_hourly(self):
        self.write_config('tick:\n  every_n: {backfill: 100}\n')
        # ticks 20 min apart: every_n alone would wait 100 ticks; the hour floor runs it
        self.assertEqual(self.due_run('backfill', 5, gap=20 * 60), [True, False, False, True, False])
        self.write_config('tick:\n  every_n: {backfill: 100}\n  deferred_max_age_s: 600\n')
        self.assertEqual(self.due_run('backfill', 3, start=NOW + 10 ** 6, gap=20 * 60),
                         [True, True, True])

    def test_the_record_and_the_wave_are_never_gated(self):
        self.write_config('tick:\n  every_n: {wave: 5, record: 5}\n')
        self.assertNotIn('wave', cadence.every_n(self.product, []))
        self.assertNotIn('record', cadence.every_n(self.product, []))

    def test_a_skipped_part_says_so_once(self):
        lines = []
        c = cadence.Cadence(self.product, [], now=NOW, out=lines.append)
        c.due('rollup')
        c = cadence.Cadence(self.product, [], now=NOW + 300, out=lines.append)
        self.assertFalse(c.due('rollup'))
        self.assertEqual(lines, ['tick: rollup deferred — every 3 ticks (1/2 skipped; runs at '
                                 'least every 60 min)'])

    def test_the_clock_block_refuses_a_bad_every_n(self):
        self.assertIsNone(cadence.refusal({'backfill': 2, 'groom': 1}))
        self.assertIn('not gateable', cadence.refusal({'wave': 2}))
        self.assertIn('whole number', cadence.refusal({'groom': 0}))
        self.assertIn('whole number', cadence.refusal({'groom': True}))
        self.assertIn('must be a map', cadence.refusal([1]))
        product = env.Product('p', {'clocks': {'main': {
            'steps': ['record'], 'every': '5m', 'every_n': {'wave': 2}}}})
        with self.assertRaises(scheduler.SchedulerError) as e:
            scheduler.clocks(product)
        self.assertIn('every_n: wave is not gateable', str(e.exception))
        ok = env.Product('p', {'clocks': {'main': {
            'steps': ['record'], 'every': '5m', 'every_n': {'backfill': 2}}}})
        self.assertEqual([c.name for c in scheduler.clocks(ok)], ['main'])


class TickOrderAndLatencyTests(TickTestCase):
    """A whole tick: the wave before health, the groom and the record's tail; its latency on the
    tick line, in the state dir and on its own log line."""

    def run_with_stubs(self, health=None, **kw):
        from asf.tick import step_groom, step_harvest, step_health, step_prs, step_wave
        order = []

        def stub(name):
            def run(ctx, out=print):
                order.append(name)
                return 0
            return run

        def tail(root, product, due=None):
            order.append('tail')
            return [p for p in cadence.RECORD_TAIL if due(p)]
        # the real tail step (TickTestCase stubs it out), over a stand-in tail
        with mock.patch.object(tick, 'run_record_tail_step', _real_tail_step), \
                mock.patch.object(tick, 'run_record_tail', tail), \
                mock.patch.object(step_health, 'run', health or stub('health')), \
                mock.patch.object(step_groom, 'run', stub('groom')), \
                mock.patch.object(step_wave, 'run', stub('wave')), \
                mock.patch.object(step_prs, 'run', stub('prs')), \
                mock.patch.object(step_harvest, 'run', stub('harvest')):
            rc, out = self.run_tick(**kw)
        return rc, out, order

    def test_the_wave_starts_before_health_groom_and_the_record_tail(self):
        rc, out, order = self.run_with_stubs(steps='record,health,groom,wave,prs,harvest')
        self.assertEqual(rc, 0)
        # the tail lands before the background harvest starts reading the record
        self.assertEqual(order, ['wave', 'health', 'groom', 'prs', 'tail', 'harvest'])
        self.assertLess(out.index('[step:wave] start '), out.index('[step:health] start '))
        self.assertLess(out.index('[step:record-tail] start '), out.index('[step:harvest] start '))

    def test_a_run_health_ends_has_the_wave_run_again_after_the_groom(self):
        """Health after the wave: a run it ends frees a seat this tick, as when it ran first."""
        def freeing(ctx, out=print):
            ctx.health_freed = True
            return 0
        _rc, out, order = self.run_with_stubs(health=freeing,
                                              steps='record,health,groom,wave,prs,harvest')
        self.assertEqual(order, ['wave', 'groom', 'wave', 'prs', 'tail', 'harvest'])
        self.assertEqual(out.count('tick: wave latency'), 1)  # the first wave's
        _rc, _out, order = self.run_with_stubs(steps='record,health,groom,wave,prs,harvest')
        self.assertEqual(order.count('wave'), 1)  # nothing freed: one wave

    def test_the_latency_is_logged_kept_and_on_the_tick_line(self):
        rc, out, _order = self.run_with_stubs(steps='record,health,groom,wave')
        self.assertEqual(rc, 0)
        self.assertRegex(out, r'tick: wave latency \d+\.\ds \(tick start → wave start\)')
        product = env.load_product('sample')
        kept = wave_latency.read(product)
        self.assertIsNotNone(kept)
        self.assertLess(kept['seconds'], 120)  # the fixture's record, the wave right after it
        from tests.test_tick import _git
        from asf.metrics import metrics as m
        log = _git(['show', f'main:metrics/ticks/{m.today()}.jsonl'], self.origin)
        line = json.loads(log.splitlines()[-1])
        self.assertEqual(line['wave_latency_s'], kept['seconds'])
        self.assertIn(('record-tail', True), [(s['step'], s['ok']) for s in line['steps']])
        m.validate('ticks', line, {})  # the stream's schema carries the field

    def test_a_slow_health_and_groom_no_longer_delay_the_wave(self):
        """The 10-06 tick: 494 s of health and 65 s of groom stood before the wave. Each now
        costs the wave nothing — its latency is the record's, not theirs."""
        clock = [1000.0]

        def slow(name, secs):
            def run(ctx, out=print):
                clock[0] += secs
                return 0
            return run
        from asf.tick import step_groom, step_health, step_wave
        seen = {}

        def wave(ctx, out=print):
            seen['latency'] = ctx.wave_latency_s
            return 0
        with mock.patch.object(tick.time, 'monotonic', lambda: clock[0]), \
                mock.patch.object(tick, 'run_record_tail_step', lambda ctx: None), \
                mock.patch.object(step_health, 'run', slow('health', 494)), \
                mock.patch.object(step_groom, 'run', slow('groom', 65)), \
                mock.patch.object(step_wave, 'run', wave):
            rc, _out = self.run_tick(steps='record,health,groom,wave')
        self.assertEqual(rc, 0)
        self.assertLessEqual(seen['latency'], 120)

    def test_a_deferred_record_part_is_named_and_skipped(self):
        self.write_config('tick:\n  every_n: {rollup: 2}\n')
        self.run_with_stubs(steps='record,wave')
        _rc, out, _order = self.run_with_stubs(steps='record,wave')
        self.assertIn('tick: rollup deferred — every 2 ticks', out)

    def test_a_deferred_step_does_not_run(self):
        self.write_config('tick:\n  every_n: {groom: 2}\n')
        _rc, _out, first = self.run_with_stubs(steps='record,groom,wave')
        _rc, out, second = self.run_with_stubs(steps='record,groom,wave')
        self.assertIn('groom', first)
        self.assertNotIn('groom', second)
        self.assertIn('tick: groom deferred — every 2 ticks', out)

    def test_a_new_inbox_card_has_the_groom_run_before_the_wave(self):
        def card(name, text):
            d = os.path.join(self.record_path(), 'inbox')
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), 'w') as f:
                f.write(text)
        _rc, _out, order = self.run_with_stubs(steps='record,groom,wave')
        self.assertEqual(order[:2], ['wave', 'groom'])
        with mock.patch.object(tick, 'fresh_inbox', lambda ctx: True):
            _rc, _out, order = self.run_with_stubs(steps='record,groom,wave')
        self.assertEqual(order[:2], ['groom', 'wave'])
        # read off the record's intake: an asked card waits on its answer, a new one is minted
        ctx = tick.Context(env.load_product('sample'))
        ctx._record = self.record_path()
        card('asked.md', '# Asked\n\n## Question\n\nwhich?\n')
        self.assertFalse(tick.fresh_inbox(ctx))
        card('new.md', '# New\n\nsomething\n')
        self.assertTrue(tick.fresh_inbox(ctx))

    def test_a_failed_record_runs_no_tail(self):
        boom = RuntimeError('ingest broke')
        with mock.patch.object(tick, 'run_step0', side_effect=boom):
            _rc, out, order = self.run_with_stubs(steps='record,wave')
        self.assertEqual(order, [])
        self.assertNotIn('[step:record-tail]', out)


_real_tail_step = tick.run_record_tail_step


class HealthSparesThisWavesRunsTests(HomeCase):
    """Health runs after the wave now: a run the wave launched seconds ago is the next tick's to
    judge, as it was when health ran first."""

    def test_only_the_runs_started_since_the_wave_are_spared(self):
        from asf.tick import step_health
        from asf.workers import pool as pool_mod
        product = env.Product('p', {})
        pool_mod.append_session(product, {'job': 'old', 'pid': 11, 'started': '2026-10-06T01:00:00Z'})
        pool_mod.append_session(product, {'job': 'new', 'pid': 22, 'started': '2026-10-06T01:20:05Z'})
        ctx = mock.Mock(product=product, wave_started_at=None, runs_before_wave=None)
        self.assertEqual(step_health.spare_this_waves_runs(ctx), ((), None))
        # the ledger as the wave began: 'old' was there; 'new' is this wave's — even when a
        # fast host launched both within one second
        ctx.wave_started_at = '2026-10-06T01:20:05Z'
        from asf.tick.tick import run_identity
        ctx.runs_before_wave = {'old': run_identity(pool_mod.load_sessions(product)['old'])}
        with mock.patch('asf.workers.health.alive_for', lambda product, runs: lambda pid: False):
            spare, alive = step_health.spare_this_waves_runs(ctx)
        self.assertEqual(spare, {'new'})
        self.assertTrue(alive(22))
        self.assertFalse(alive(11))


class WaveLatencyAlarmTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.product = env.Product('p', {})

    def found(self, product=None):
        product = product or self.product
        return [f for f in dwell.check(product, facts=FakeFacts(product), out=lambda _l: None)
                if f.state == 'wave_latency']

    def test_no_tick_reached_a_wave_no_finding(self):
        self.assertEqual(self.found(), [])

    def test_past_two_minutes_is_a_breach(self):
        wave_latency.write(self.product, 150.0, at='2026-10-06T01:20:00Z')
        (f,) = self.found()
        self.assertTrue(f.breach)
        self.assertEqual(f.limit_min, 2)
        self.assertIn('BREACH wave_latency tick — 2 min, limit 2 min (owner tick)', f.line())
        self.assertIn('reached its wave 150s after its start', f.line())

    def test_under_the_limit_is_no_breach_and_the_limit_is_the_products(self):
        wave_latency.write(self.product, 45.0)
        (f,) = self.found()
        self.assertFalse(f.breach)
        wave_latency.write(self.product, 400.0)
        product = env.Product('p', {'conventions': {'watchdog': {'wave_latency': 10}}})
        (f,) = self.found(product)
        self.assertFalse(f.breach)
        off = env.Product('p', {'conventions': {'watchdog': {'wave_latency': 'off'}}})
        self.assertEqual(self.found(off), [])

    def test_status_shows_it_and_says_when_it_is_over(self):
        from asf.views import status
        self.assertIsNone(status.wave_latency_cell(self.product))
        wave_latency.write(self.product, 45.0, at='2026-10-06T01:20:00Z')
        self.assertRegex(status.wave_latency_cell(self.product), r'^45s \(tick at \d\d:\d\d\)$')
        wave_latency.write(self.product, 756.0, at='2026-10-06T01:20:00Z')
        self.assertTrue(status.wave_latency_cell(self.product).endswith(
            '— over the 2 min limit'))
        text = status.render(None, self.product, cfg={})
        self.assertIn('| Wave latency | 756s', text)


class MeterCacheTests(HomeCase):
    """The backfill reads a job log again only when it changed (size or mtime)."""

    def setUp(self):
        super().setUp()
        self.logs = os.path.join(self.tmp, 'logs')
        os.makedirs(self.logs)
        self.state = os.path.join(self.tmp, 'sessions.jsonl')
        with open(self.state, 'w') as f:
            for job in ('a', 'b'):
                f.write(json.dumps({'job': job, 'started': '2026-10-05T10:00:00Z'}) + '\n')
                f.write(json.dumps({'job': job, 'ended': '2026-10-05T11:00:00Z',
                                    'end_reason': 'finished'}) + '\n')
        for job in ('a', 'b'):
            self.write_log(job, 'done   \nsecond line')
        self.cache_path = os.path.join(self.tmp, 'meter-cache.json')

    def write_log(self, job, report, extra=''):
        with open(os.path.join(self.logs, f'{job}.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 5}}}) + '\n')
            f.write(json.dumps({'type': 'result', 'result': report, 'total_cost_usd': 0.5,
                                'duration_ms': 60000, 'num_turns': 3,
                                'usage': {'input_tokens': 7, 'output_tokens': 2}}) + '\n')
            f.write(extra)

    def events(self, cache=None):
        return metrics.sessions_from_registry(None, state_path=self.state, logs_dir=self.logs,
                                              meter_cache=cache)

    def test_the_cached_events_are_the_uncached_ones(self):
        plain = self.events()
        cache = metrics.MeterCache(self.cache_path)
        self.assertEqual(self.events(cache), plain)
        cache.save()
        again = metrics.MeterCache(self.cache_path)
        self.assertEqual(self.events(again), plain)
        self.assertEqual(plain[0]['reason'], 'done   ')

    def test_an_unchanged_log_is_not_read_again_a_changed_one_is(self):
        cache = metrics.MeterCache(self.cache_path)
        self.events(cache)
        cache.save()
        real = tokens.meter_runs
        reads = []

        def counted(path):
            reads.append(os.path.basename(path))
            return real(path)
        with mock.patch.object(tokens, 'meter_runs', counted):
            self.events(metrics.MeterCache(self.cache_path))
            self.assertEqual(reads, [])
            self.write_log('b', 'redone', extra='\n')
            cache = metrics.MeterCache(self.cache_path)
            evs = self.events(cache)
        self.assertEqual(reads, ['b.jsonl'])
        self.assertEqual([e['reason'] for e in evs], ['done   ', 'redone'])

    def test_a_log_no_longer_asked_about_leaves_the_cache(self):
        cache = metrics.MeterCache(self.cache_path)
        self.events(cache)
        cache.save()
        os.remove(os.path.join(self.logs, 'a.jsonl'))
        with open(self.state, 'w') as f:
            f.write(json.dumps({'job': 'b', 'started': '2026-10-05T10:00:00Z'}) + '\n')
            f.write(json.dumps({'job': 'b', 'ended': '2026-10-05T11:00:00Z'}) + '\n')
        cache = metrics.MeterCache(self.cache_path)
        self.events(cache)
        cache.save()
        with open(self.cache_path) as f:
            kept = json.load(f)['logs']
        self.assertEqual([os.path.basename(p) for p in kept], ['b.jsonl'])

    def test_an_unreadable_cache_is_an_empty_one(self):
        with open(self.cache_path, 'w') as f:
            f.write('{not json')
        self.assertEqual(self.events(metrics.MeterCache(self.cache_path)), self.events())


class ManifestTests(unittest.TestCase):
    def test_every_gateable_name_is_a_step_or_a_tail_part(self):
        for name in cadence.gateable():
            self.assertTrue(name in steps.STEPS or name in cadence.RECORD_TAIL, name)
        for name in cadence.UNGATED:
            self.assertNotIn(name, cadence.gateable())


if __name__ == '__main__':
    unittest.main()
