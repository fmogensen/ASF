"""asf.metrics.throughput — the eight throughput metrics, each computed from fixture streams; the
scorecard's table with its 7-day trend; one breach line per alarm; ``n/a`` (never an alarm) for a
minimal product: one local seat, no forge, no cloud, no merge queue, no runner, no quota reader."""
import datetime
import os
import tempfile
import types
import unittest

from asf import env
from asf.improve.measure import Run
from asf.metrics import metrics, throughput as tp

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
START = NOW - datetime.timedelta(days=7)
END = NOW + datetime.timedelta(seconds=1)
SEATS = dict(tp.SEAT_DEFAULTS)
CFG = dict(tp.DEFAULTS)


def at(minutes=0, hours=0, days=0):
    return (NOW - datetime.timedelta(minutes=minutes, hours=hours, days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def tick(ts, busy, avail, launchable, cloud=(0, 0), cause=''):
    return {'ts': ts, 'tick': 1, 'seats': tp.seats_record(busy, avail, cloud[0], cloud[1], launchable, cause)}


def run(ended, reason='finished', landed=False, cloud=False, usd=None, minutes=10.0):
    return Run(job='j', kind='code', model='m', item=None, started=ended, ended=ended, minutes=minutes,
               landed=landed, end_reason=reason, usd=usd, cloud=cloud)


def facts(**over):
    f = {'ticks': [], 'events': [], 'landings': [], 'ci': [], 'gates': [], 'items': {}, 'runs': [],
         'forge': True, 'pool': [], 'quota': [], 'stop': 95.0}
    f.update(over)
    return f


class SeatsTest(unittest.TestCase):
    def test_per_tick_utilisation_counts_local_and_cloud(self):
        pts = tp.seat_points([tick(at(10), 3, 8, 1, cloud=(2, 4))])
        self.assertEqual([(p[1], p[2], p[3]) for p in pts], [(5, 12, 1)])

    def test_a_31_minute_stretch_below_60_with_launchable_rows_alarms(self):
        ticks = [tick(at(m), 2, 8, 3, cause='host load') for m in (31, 25, 20, 15, 10, 5, 0)]
        row = tp.seats_metric(ticks, START, END, SEATS)
        self.assertEqual(row['status'], tp.ALARM)
        self.assertEqual(row['value'], 25.0)
        self.assertEqual(len(row['stretches']), 1)
        self.assertEqual(row['stretches'][0]['minutes'], 31.0)
        self.assertIn('host load', row['detail'])
        self.assertTrue(tp.breach_line(row).startswith('metrics: BREACH seats — '))

    def test_quiet_or_short_is_ok(self):
        quiet = [tick(at(m), 2, 8, 0) for m in (31, 25, 20, 15, 10, 5, 0)]
        short = [tick(at(m), 2, 8, 3) for m in (29, 25, 20, 15, 10, 5, 0)]
        self.assertEqual(tp.seats_metric(quiet, START, END, SEATS)['status'], tp.OK)
        self.assertEqual(tp.seats_metric(short, START, END, SEATS)['status'], tp.OK)

    def test_a_gap_in_the_readings_ends_a_stretch(self):
        ticks = [tick(at(m), 2, 8, 3) for m in (40, 35, 10, 5, 0)]   # 25 min with no reading
        self.assertEqual(tp.idle_stretches(tp.seat_points(ticks), SEATS), [])

    def test_hourly(self):
        ticks = [tick('2026-10-06T10:10:00Z', 2, 8, 1), tick('2026-10-06T10:50:00Z', 6, 8, 1),
                 tick('2026-10-06T11:10:00Z', 8, 8, 1)]
        self.assertEqual(tp.hourly(tp.seat_points(ticks), START, END),
                         {'2026-10-06T10:00:00Z': 50.0, '2026-10-06T11:00:00Z': 100.0})

    def test_no_reading_is_na(self):
        self.assertEqual(tp.seats_metric([{'ts': at(1), 'tick': 1}], START, END, SEATS)['status'], tp.NA)


class FirstPassTest(unittest.TestCase):
    CI = [
        {'ts': at(hours=5), 'pr': 1, 'sha': 'a', 'attempt': 1, 'conclusion': 'success', 'batch': None},
        {'ts': at(hours=5), 'pr': 1, 'sha': 'a', 'attempt': 1, 'conclusion': 'success', 'batch': None},
        {'ts': at(hours=4), 'pr': 2, 'sha': 'b', 'attempt': 1, 'conclusion': 'failure', 'batch': None},
        {'ts': at(hours=3), 'pr': 2, 'sha': 'b', 'attempt': 2, 'conclusion': 'success', 'batch': None},
        {'ts': at(hours=3), 'pr': 3, 'sha': 'c', 'attempt': 1, 'conclusion': 'success', 'batch': None},
        {'ts': at(hours=2), 'pr': 3, 'sha': 'd', 'attempt': 1, 'conclusion': 'failure', 'batch': None},
        {'ts': at(hours=2), 'pr': None, 'sha': 'e', 'attempt': 1, 'conclusion': 'failure', 'batch': 'b/1'},
    ]

    def test_green_on_the_first_head_first_attempt_over_prs_seen(self):
        self.assertEqual(tp.first_pass(self.CI, START, END), (2, 3))
        row = tp.first_pass_metric(self.CI, START, END, dict(CFG, first_pass_min=0.8))
        # the day-window row reads; the alarm is the run-window target's (asf.metrics.reds)
        self.assertEqual((row['value'], row['status']), (0.667, tp.OK))

    def test_no_forge_is_na(self):
        self.assertEqual(tp.first_pass_metric(self.CI, START, END, CFG, forge=False)['status'], tp.NA)


class FalseCloseTest(unittest.TestCase):
    ITEMS = {'S-0001': {'closes': [at(days=3)], 'reopened': [at(days=2)]},
             'S-0002': {'closes': [at(days=3)], 'reopened': []},
             'S-0003': {'closes': [at(days=30)], 'reopened': []}}

    def test_reopens_over_closes_in_the_window_alarm_over_zero(self):
        self.assertEqual(tp.false_close(self.ITEMS, START, END), (1, 2, ['S-0001']))
        row = tp.false_close_metric(self.ITEMS, START, END, CFG)
        self.assertEqual((row['value'], row['status']), (0.5, tp.ALARM))
        self.assertIn('S-0001', row['detail'])

    def test_none_reopened_is_ok(self):
        items = {k: dict(v, reopened=[]) for k, v in self.ITEMS.items()}
        self.assertEqual(tp.false_close_metric(items, START, END, CFG)['status'], tp.OK)


class DetectTest(unittest.TestCase):
    def test_first_breach_age_per_watched_fact(self):
        ev = [{'kind': 'watchdog', 'ts': at(60), 'state': 'launchable_idle', 'key': 'T-1', 'age_min': 12.0},
              {'kind': 'watchdog', 'ts': at(50), 'state': 'launchable_idle', 'key': 'T-1', 'age_min': 22.0},
              {'kind': 'watchdog', 'ts': at(40), 'state': 'chain_no_cut', 'key': 'x', 'age_min': 40.0},
              {'kind': 'launch', 'ts': at(40)}]
        self.assertEqual(sorted(tp.detect_minutes(ev, START, END)), [12.0, 40.0])
        row = tp.detect_metric(ev, START, END, dict(CFG, detect_p90_max_min=30))
        self.assertEqual((row['p50'], row['p90'], row['status']), (12.0, 40.0, tp.ALARM))


class CloudTest(unittest.TestCase):
    def test_outcomes_cloud_against_local(self):
        runs = [run(at(60), landed=True, cloud=True, usd=2.0), run(at(50), 'dead pid', cloud=True, usd=1.0),
                run(at(40), 'timeout', cloud=True), run(at(30), landed=True), run(at(20), 'stalled')]
        row = tp.cloud_metric(runs, START, END, dict(CFG, cloud_dead_max=0.5))
        self.assertEqual(row['cloud']['runs'], 3)
        self.assertEqual((row['cloud'][tp.DEAD], row['cloud'][tp.TIMEOUT]), (0.333, 0.333))
        self.assertEqual(row['cloud']['usd_per_run'], 1.5)
        self.assertEqual(row['local'][tp.DEAD], 0.5)
        self.assertEqual(row['status'], tp.ALARM)

    def test_unpriced_runs_say_na_for_dollars(self):
        row = tp.cloud_metric([run(at(5), landed=True, cloud=True)], START, END, CFG)
        self.assertIn('n/a/run', row['detail'])

    def test_no_cloud_run_is_na(self):
        self.assertEqual(tp.cloud_metric([run(at(5))], START, END, CFG)['status'], tp.NA)


class MergeTest(unittest.TestCase):
    def test_green_to_landed_and_batches(self):
        ci = [{'ts': at(100), 'branch': 'task/T-1', 'conclusion': 'success'},
              {'ts': at(90), 'branch': 'task/T-1', 'conclusion': 'success'},
              {'ts': at(80), 'branch': 'task/T-2', 'conclusion': 'success'}]
        lands = [{'ts': at(60), 'branch': 'task/T-1'}, {'ts': at(20), 'branch': 'task/T-2'},
                 {'ts': at(10), 'branch': 'task/T-3'}]
        gates = [{'ts': at(70), 'conclusion': 'success'}, {'ts': at(30), 'conclusion': 'failure'}]
        self.assertEqual(sorted(tp.merge_waits(ci, lands, START, END)), [30.0, 60.0])
        row = tp.merge_metric(ci, lands, gates, START, END, CFG)
        self.assertEqual((row['p50'], row['p90'], row['red_batch_rate']), (30.0, 60.0, 0.5))

    def test_no_merge_queue_says_so_and_still_measures_the_wait(self):
        row = tp.merge_metric([{'ts': at(30), 'branch': 'b', 'conclusion': 'success'}],
                              [{'ts': at(10), 'branch': 'b'}], [], START, END, CFG)
        self.assertEqual(row['status'], tp.OK)
        self.assertIn('n/a (no merge queue)', row['detail'])


class RunnersTest(unittest.TestCase):
    def test_busy_over_available_per_class_and_queue_wait(self):
        pool = [types.SimpleNamespace(runner='r1', cls='big', provider='p', role='ci', slots=1),
                types.SimpleNamespace(runner='r2', cls='big', provider='p', role='ci', slots=1)]
        ci = [{'ts': at(30), 'jobs': [{'runner': 'r1', 'minutes': 2016.0, 'queued_s': 60},
                                      {'runner': 'gh-hosted-9', 'minutes': 5, 'queued_s': 600}]}]
        row = tp.runners_metric(ci, START, END, dict(CFG, runner_queue_p90_max_min=5), pool)
        self.assertEqual(row['busy'], {'big': 2016.0})
        self.assertIn('big 10 % busy', row['detail'])
        self.assertIn('hosted queue p50 10 min', row['detail'])
        self.assertEqual(row['status'], tp.ALARM)

    def test_no_self_hosted_runner_is_na_for_use_but_queue_wait_is_measured(self):
        ci = [{'ts': at(30), 'jobs': [{'runner': 'x', 'minutes': 5, 'queued_s': 120}]}]
        row = tp.runners_metric(ci, START, END, CFG, [])
        self.assertIn('runner use n/a', row['detail'])
        self.assertIn('hosted queue p50 2 min', row['detail'])
        self.assertEqual(tp.runners_metric([], START, END, CFG, [])['status'], tp.NA)


class QuotaTest(unittest.TestCase):
    def test_burn_and_time_to_stop_from_successive_readings(self):
        s = [{'ts': at(60), 'account': 'a', 'five_h_pct': 80}, {'ts': at(30), 'account': 'a', 'five_h_pct': 5},
             {'ts': at(0), 'account': 'a', 'five_h_pct': 50}, {'ts': at(30), 'account': 'b', 'five_h_pct': 10},
             {'ts': at(0), 'account': 'b', 'five_h_pct': 10}]
        b = tp.burn(s, NOW, 60)
        self.assertEqual(b['a'], {'pct': 50.0, 'per_hour': 90.0, 'hours_to_stop': 0.5})
        self.assertEqual(b['b']['hours_to_stop'], None)
        row = tp.quota_metric(s, NOW, CFG)
        self.assertEqual((row['status'], row['value']), (tp.ALARM, 0.5))

    def test_no_reader_is_na(self):
        self.assertEqual(tp.quota_metric([], NOW, CFG)['status'], tp.NA)


class ScorecardTest(unittest.TestCase):
    def test_every_metric_has_a_row_a_7_day_trend_and_alarms_are_breach_lines(self):
        ticks = [tick(at(m), 2, 8, 3) for m in (35, 30, 25, 20, 15, 10, 5, 0)]
        ticks += [tick(at(m, days=2), 8, 8, 3) for m in (10, 5, 0)]
        d = tp.compute(facts(ticks=ticks), at(0), SEATS, CFG)
        self.assertEqual([r['key'] for r in d['rows']], list(tp.METRICS))
        seats = d['rows'][0]
        self.assertEqual(len(seats['trend']), 7)
        self.assertEqual(seats['trend'][-1], 25.0)
        self.assertEqual(seats['trend'][-3], 100.0)
        self.assertEqual(d['alarms'], [tp.breach_line(seats)])
        text = tp.render(d)
        self.assertIn('| Seat utilisation | ALARM |', text)
        self.assertIn('100 ', text)
        self.assertIn('metrics: BREACH seats', text)

    def test_a_minimal_product_is_all_na_and_no_alarm(self):
        d = tp.compute(facts(forge=False), at(0), SEATS, CFG)
        self.assertEqual({r['status'] for r in d['rows']}, {tp.NA})
        self.assertEqual(d['alarms'], [])

    def test_settings(self):
        p = env.Product('p', {'release': {'seats': {'min_pct': 50}},
                              'improve': {'scorecard': {'throughput': {'false_close_max': 'off',
                                                                       'quota_cap_hours_min': 2}}}})
        seats, cfg = tp.settings(p)
        self.assertEqual(seats['min_pct'], 50)
        self.assertEqual(seats['idle_min'], 30)
        self.assertIsNone(cfg['false_close_max'])
        self.assertEqual(cfg['quota_cap_hours_min'], 2)

    def test_the_scorecard_view_shows_the_table(self):
        from unittest import mock
        from asf.scorecard.facts import Facts
        from asf.views import scorecard
        f = Facts(items={}, sessions=[], ci=[], gates=[], runs=[], clutter={}, as_of=at(0))
        p = env.Product('p', {})
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'metrics', 'ticks'))
            path = metrics.stream_path(d, 'ticks', NOW.date().isoformat())
            with open(path, 'w') as fh:
                for t in [tick(at(m), 2, 8, 3) for m in (35, 30, 25, 20, 15, 10, 5, 0)]:
                    fh.write(metrics.dumps(t) + '\n')
            with mock.patch('asf.workers.headroom.read_samples', return_value=[]):
                out = scorecard.compute(d, p, weeks=1, facts=f)
        self.assertIn('throughput', out)
        self.assertIn('**Throughput**', scorecard.render(out))
        self.assertIn('metrics: BREACH seats', scorecard.render(out))


class WriterTest(unittest.TestCase):
    """The streams carry what the metrics read: the wave's seat reading on the tick line, a CI
    job's queue wait, a run's lane."""

    def test_the_tick_line_carries_the_wave_seat_reading(self):
        from asf.tick import tick as tick_mod
        from asf.tick import step_wave
        ctx = types.SimpleNamespace(counts={'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0,
                                            'relaunches': 0}, product=types.SimpleNamespace(name='p'),
                                    seats=None)
        lane = types.SimpleNamespace(on=False, max_inflight=4)
        step_wave.note_seats(ctx, 1, lane, [{'pid': 123}], 2, 'WAITS ON host load')
        step_wave.note_launched(ctx, [(None, {'pid': 456})])
        line = tick_mod.tick_line(ctx, [], now=NOW)
        self.assertEqual(line['seats'], {'local_busy': 2, 'local_seats': 1, 'cloud_busy': 0,
                                         'cloud_seats': 0, 'launchable': 2, 'cause': 'WAITS ON host load'})
        ev = metrics.validate('ticks', dict(line), {})
        self.assertEqual(ev['seats']['launchable'], 2)

    def test_a_tick_with_no_wave_carries_no_seats(self):
        from asf.tick import tick as tick_mod
        ctx = types.SimpleNamespace(counts={}, product=types.SimpleNamespace(name='p'), seats=None)
        self.assertNotIn('seats', tick_mod.tick_line(ctx, [], now=NOW))

    def test_the_top_cause_folds_digits(self):
        from asf.tick import step_wave
        self.assertEqual(step_wave.top_cause(['fair share 3/5', 'fair share 4/5', 'blocked']), 'fair share N/N')
        self.assertEqual(step_wave.top_cause([]), '')

    def test_the_tick_prints_one_breach_line_while_a_stretch_runs(self):
        from asf.tick import tick as tick_mod
        ctx = types.SimpleNamespace(product=env.Product('p', {}))
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, '2026-10-06.jsonl'), 'w') as fh:
                for m in (35, 30, 25, 20, 15, 10, 5, 0):
                    fh.write(metrics.dumps(tick(at(m), 2, 8, 3, cause='host load')) + '\n')
            lines = []
            tick_mod.seat_alarm(ctx, d, at(0), out=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('metrics: BREACH seats — 35 min at 2/8'), lines[0])

    def test_no_line_once_the_stretch_ended(self):
        ticks = [tick(at(m), 2, 8, 3) for m in (40, 35, 30, 25, 20, 15, 10, 5)] + [tick(at(0), 8, 8, 3)]
        self.assertIsNone(tp.live_breach(ticks, SEATS))

    def test_a_ci_job_carries_its_queue_wait(self):
        self.assertEqual(metrics.queued_s('2026-10-06T10:00:00Z', '2026-10-06T10:02:30Z'), 150.0)
        self.assertIsNone(metrics.queued_s(None, '2026-10-06T10:02:30Z'))


if __name__ == '__main__':
    unittest.main()
