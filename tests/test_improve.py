"""asf.improve — the arithmetic (F-0091, Task 1): every run, its spend, the table, the four classes.

The hand-written registry in ``tests/fixtures/improve/registry.jsonl`` and its two job logs are
small enough to compute by hand; every expected number below is worked out in a comment beside it.
"""
import json
import os
import tempfile
import unittest

from asf import env
from asf.improve import classes, measure

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'improve')
REGISTRY = os.path.join(FIXTURES, 'registry.jsonl')
LOGS = os.path.join(FIXTURES, 'logs')
FROZEN = os.path.join(FIXTURES, 'asf-2026-09-23.jsonl')


def fixture_runs(**kw):
    return measure.ended_runs(None, ledger=REGISTRY, logs_dir=LOGS, **kw)


def write_lines(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


class RunsTests(unittest.TestCase):
    def test_a_three_run_job_is_three_sessions(self):
        runs = fixture_runs()
        self.assertEqual([r.job for r in runs].count('coder-t-0001'), 3)
        self.assertEqual(len(runs), 6)  # 3 + adjudicate + spec + fix-bug; plan-t-0004 has not ended

    def test_a_run_that_has_not_ended_does_not_count(self):
        self.assertNotIn('plan-t-0004', [r.job for r in fixture_runs()])

    def test_minutes_come_from_started_and_ended(self):
        by_start = {r.started: r for r in fixture_runs()}
        self.assertEqual(by_start['2026-09-01T10:00:00Z'].minutes, 30.0)
        self.assertEqual(by_start['2026-09-01T11:00:00Z'].minutes, 45.0)
        self.assertEqual(by_start['2026-09-02T09:00:00Z'].minutes, 60.0)

    def test_an_unparseable_pair_is_zero_minutes(self):
        run = [r for r in fixture_runs() if r.job == 'fix-bug-b-0005'][0]
        self.assertEqual(run.minutes, 0.0)

    def test_minutes_never_go_negative_and_round_to_one_place(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, 'sessions.jsonl')
            write_lines(ledger, [
                {'job': 'a', 'started': '2026-01-01T00:10:00Z', 'pid': 1, 'ended': '2026-01-01T00:00:00Z'},
                {'job': 'b', 'started': '2026-01-01T00:00:00Z', 'pid': 2, 'ended': '2026-01-01T00:01:20Z'}])
            runs = {r.job: r for r in measure.ended_runs(None, ledger=ledger, logs_dir=d)}
        self.assertEqual(runs['a'].minutes, 0.0)
        self.assertEqual(runs['b'].minutes, 1.3)  # 80 s = 1.333… min

    def test_landed_is_harvested_not_finished(self):
        by_start = {r.started: r for r in fixture_runs()}
        self.assertFalse(by_start['2026-09-01T11:00:00Z'].landed)  # ended 'finished', never harvested
        self.assertTrue(by_start['2026-09-01T12:00:00Z'].landed)
        self.assertEqual(by_start['2026-09-01T11:00:00Z'].end_reason, 'finished')

    def test_the_run_carries_its_registry_fields(self):
        run = [r for r in fixture_runs() if r.job == 'adjudicate-t-0002'][0]
        self.assertEqual((run.kind, run.model, run.item, run.ended),
                         ('adjudicate', 'claude-opus-5', 'T-0002', '2026-09-02T10:00:00Z'))

    def test_a_run_is_frozen(self):
        with self.assertRaises(Exception):
            fixture_runs()[0].minutes = 1.0

    def test_since_keeps_runs_ended_on_or_after_the_day(self):
        self.assertEqual({r.job for r in fixture_runs(since='2026-09-03')}, {'spec-t-0003', 'fix-bug-b-0005'})

    def test_as_of_keeps_runs_ended_at_or_before_the_stamp(self):
        jobs = [r.job for r in fixture_runs(as_of='2026-09-02T10:00:00Z')]
        self.assertEqual(jobs, ['coder-t-0001'] * 3 + ['adjudicate-t-0002'])

    def test_the_default_ledger_is_the_products_registry(self):
        old = env.ASF_HOME
        with tempfile.TemporaryDirectory() as home:
            env.ASF_HOME = home
            try:
                state = env.state_dir('p')
                write_lines(os.path.join(state, 'sessions.jsonl'), [
                    {'job': 'j', 'started': '2026-01-01T00:00:00Z', 'pid': 1,
                     'ended': '2026-01-01T01:00:00Z'}])
                runs = measure.ended_runs('p')
            finally:
                env.ASF_HOME = old
        self.assertEqual([(r.job, r.minutes, r.usd) for r in runs], [('j', 60.0, None)])

    def test_no_ledger_is_no_runs(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(measure.ended_runs(None, ledger=os.path.join(d, 'none.jsonl'), logs_dir=d), [])


class SpendTests(unittest.TestCase):
    def test_the_max_per_session_id_summed_not_the_last_line_and_not_every_line(self):
        # s1 rises 0.5 -> 1.0, s2 rises 0.4 -> 0.8: 1.0 + 0.8. The last line would say 0.8, every line 2.7.
        self.assertAlmostEqual(measure.job_spend('coder-t-0001', LOGS), 1.8)

    def test_lines_with_no_session_id_form_one_group(self):
        self.assertAlmostEqual(measure.job_spend('adjudicate-t-0002', LOGS), 3.0)

    def test_no_log_is_none(self):
        self.assertIsNone(measure.job_spend('spec-t-0003', LOGS))
        self.assertIsNone(measure.job_spend('spec-t-0003', None))

    def test_a_log_with_no_cost_field_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(os.path.join(d, 'j.jsonl'), [
                {'type': 'assistant', 'total_cost_usd': 9.0}, {'type': 'result', 'session_id': 'a'}])
            self.assertIsNone(measure.job_spend('j', d))

    def test_a_torn_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'j.jsonl'), 'w') as f:
                f.write('{"type": "result", "session_id": "a", "total_cost_usd": 1.5}\n{"type": "res')
            self.assertAlmostEqual(measure.job_spend('j', d), 1.5)

    def test_a_jobs_spend_is_split_over_its_ended_runs_by_minutes(self):
        by_start = {r.started: r for r in fixture_runs()}
        # 1.8 over 30 / 45 / 15 of 90 minutes
        self.assertAlmostEqual(by_start['2026-09-01T10:00:00Z'].usd, 0.6)
        self.assertAlmostEqual(by_start['2026-09-01T11:00:00Z'].usd, 0.9)
        self.assertAlmostEqual(by_start['2026-09-01T12:00:00Z'].usd, 0.3)
        self.assertAlmostEqual(by_start['2026-09-02T09:00:00Z'].usd, 3.0)

    def test_a_job_with_no_log_has_no_usd(self):
        run = [r for r in fixture_runs() if r.job == 'spec-t-0003'][0]
        self.assertIsNone(run.usd)

    def test_a_job_of_zero_minutes_splits_evenly(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, 'sessions.jsonl')
            write_lines(ledger, [
                {'job': 'j', 'started': 'x', 'pid': 1, 'ended': 'y'},
                {'job': 'j', 'started': 'x', 'pid': 2, 'ended': 'y'}])
            write_lines(os.path.join(d, 'j.jsonl'), [{'type': 'result', 'total_cost_usd': 1.0}])
            runs = measure.ended_runs(None, ledger=ledger, logs_dir=d)
        self.assertEqual([r.usd for r in runs], [0.5, 0.5])

    def test_a_window_carries_its_own_share_of_the_jobs_spend(self):
        runs = fixture_runs(since='2026-09-01', as_of='2026-09-01T11:45:00Z')
        self.assertAlmostEqual(sum(r.usd for r in runs), 1.5)  # the first two runs: 0.6 + 0.9


class TableTests(unittest.TestCase):
    def setUp(self):
        self.t = measure.table(fixture_runs())

    def test_the_scalars(self):
        t = self.t
        self.assertEqual(t['sessions'], 6)
        self.assertEqual(t['hours'], 3.0)            # 30+45+15+60+30+0 = 180 min
        self.assertEqual(t['usd'], 4.8)              # 1.8 + 3.0; the spec and fix-bug runs have none
        self.assertEqual(t['landed_sessions'], 2)    # the third coder run, the adjudicate run
        self.assertEqual(t['landed_items'], 2)       # T-0001, T-0002
        self.assertEqual(t['non_landing_share'], 0.583)  # (180 - 75) / 180
        self.assertEqual(t['minutes_per_landed_item'], 90.0)  # 180 / 2
        self.assertEqual(t['usd_per_landed_item'], 2.4)       # 4.8 / 2

    def test_by_kind(self):
        bk = self.t['by_kind']
        self.assertEqual(bk['coder'], (3, 1.5, 1.8))
        self.assertEqual(bk['adjudicate'], (1, 1.0, 3.0))
        self.assertEqual(bk['spec'], (1, 0.5, 0.0))
        self.assertEqual(bk['fix-bug'], (1, 0.0, 0.0))
        self.assertEqual(bk['adjudicate'].usd_per_hour, 3.0)
        self.assertEqual(bk['fix-bug'].usd_per_hour, 0.0)

    def test_by_model(self):
        bm = self.t['by_model']
        self.assertEqual(bm['claude-sonnet-5'], (4, 1.5, 1.8))
        self.assertEqual(bm['claude-opus-5'], (2, 1.5, 3.0))

    def test_by_kind_model(self):
        cells = self.t['by_kind_model']
        self.assertEqual(sorted(cells), [
            ('adjudicate', 'claude-opus-5'), ('coder', 'claude-sonnet-5'),
            ('fix-bug', 'claude-sonnet-5'), ('spec', 'claude-opus-5')])
        self.assertEqual(cells[('adjudicate', 'claude-opus-5')].hours, 1.0)
        self.assertEqual(cells[('coder', 'claude-sonnet-5')].sessions, 3)

    def test_repeat_items_are_those_with_three_ended_runs(self):
        r = self.t['repeat_items']
        self.assertEqual((r['count'], r['items']), (1, ['T-0001']))
        self.assertEqual(r['usd_share'], 0.375)   # 1.8 / 4.8
        self.assertEqual(r['hours_share'], 0.5)   # 90 / 180 min

    def test_record_usd_is_carried_or_none(self):
        self.assertIsNone(self.t['record_usd'])
        self.assertEqual(measure.table(fixture_runs(), record_usd=1.62)['record_usd'], 1.62)

    def test_an_empty_window_is_zeros_not_a_crash(self):
        t = measure.table([])
        self.assertEqual((t['sessions'], t['hours'], t['usd'], t['non_landing_share']), (0, 0.0, 0.0, 0.0))
        self.assertIsNone(t['minutes_per_landed_item'])
        self.assertIsNone(t['usd_per_landed_item'])
        self.assertEqual(t['by_kind'], {})

    def test_table_reads_no_file(self):
        runs = fixture_runs()
        old = env.ASF_HOME
        env.ASF_HOME = '/nonexistent'
        try:
            self.assertEqual(measure.table(runs), self.t)
        finally:
            env.ASF_HOME = old

    def test_an_item_less_run_is_never_a_repeat_or_a_landed_item(self):
        runs = [measure.Run('j', 'k', 'm', None, 's', 'e', 60.0, True, 'finished', 1.0)] * 3
        t = measure.table(runs)
        self.assertEqual(t['repeat_items']['count'], 0)
        self.assertEqual((t['landed_sessions'], t['landed_items']), (3, 0))
        self.assertIsNone(t['minutes_per_landed_item'])


class FrozenRegistryTests(unittest.TestCase):
    """The frozen 216-run cut reproduces the numbers the card asked for — no ~/.ASF, no logs."""

    def test_the_cut_is_216_runs_and_carries_no_paths_or_accounts(self):
        runs = measure.ended_runs(None, ledger=FROZEN, logs_dir=None)
        self.assertEqual(len(runs), 216)
        with open(FROZEN, encoding='utf-8') as f:
            keys = {k for line in f for k in json.loads(line)}
        self.assertLessEqual(keys, {'job', 'kind', 'model', 'item', 'started', 'ended',
                                    'end_reason', 'harvested', 'pid'})

    def test_the_numbers_at_the_cut(self):
        t = measure.table(measure.ended_runs(None, ledger=FROZEN, logs_dir=None))
        self.assertEqual((t['sessions'], t['hours']), (216, 195.2))
        self.assertEqual((t['landed_sessions'], t['landed_items']), (110, 86))
        self.assertEqual(t['non_landing_share'], 0.354)
        self.assertEqual(t['minutes_per_landed_item'], 136.2)
        self.assertEqual(t['by_kind_model'][('adjudicate', 'claude-opus-5')].hours, 23.7)
        self.assertEqual(t['by_kind']['fix-bug'].hours, 54.0)
        self.assertEqual(t['repeat_items']['count'], 21)


def synthetic(**over):
    """A table with 100 h and $200 (so $2/h), 30 landed items, and the four class inputs."""
    t = {
        'sessions': 100, 'hours': 100.0, 'usd': 200.0, 'landed_items': 30,
        'non_landing_share': 0.0, 'minutes_per_landed_item': 0.0,
        'repeat_items': {'count': 3, 'items': [], 'usd_share': 0.0, 'hours_share': 0.0},
        'by_kind_model': {('adjudicate', 'claude-opus-5'): measure.Cell(1, 0.0, 0.0)}}
    t.update(over)
    return t


def cell(hours):
    return {('adjudicate', 'claude-opus-5'): measure.Cell(1, hours, hours * 2.0)}


# class -> (table override for a measured value, the threshold, the excess at threshold + delta)
CASES = {
    'repeat_sessions': (lambda v: {'repeat_items': {'count': 3, 'items': [], 'usd_share': v, 'hours_share': v}},
                        0.33, 0.01, 2.0),                     # 0.01 x $200
    'time_per_landed_item': (lambda v: {'minutes_per_landed_item': v},
                             120, 0.1, 0.1),                  # 0.1 min x 30 items / 60 x $2/h
    'premium_model_in_kind': (lambda v: {'by_kind_model': cell(v)},
                              8.0, 0.1, 0.2),                 # 0.1 h x $2/h
    'non_landing_sessions': (lambda v: {'non_landing_share': v},
                             0.25, 0.01, 2.0),                # 0.01 x $200
}


def record(records, name):
    return [r for r in records if r['name'] == name][0]


class ClassTests(unittest.TestCase):
    def test_the_four_classes_and_their_default_thresholds(self):
        self.assertEqual({c.name: c.threshold for c in classes.CLASSES}, {
            'repeat_sessions': 0.33, 'time_per_landed_item': 120,
            'premium_model_in_kind': 8.0, 'non_landing_sessions': 0.25})
        self.assertEqual(classes.DEFAULT_PREMIUM_MODELS, ('claude-opus-5',))

    def test_each_class_at_just_under_and_just_over_its_threshold(self):
        for name, (make, threshold, delta, excess) in CASES.items():
            with self.subTest(name=name):
                for value, over, expect in ((threshold, False, 0.0), (threshold - delta, False, 0.0),
                                            (threshold + delta, True, excess)):
                    rec = record(classes.evaluate(synthetic(**make(value))), name)
                    self.assertEqual(rec['over'], over, value)
                    self.assertEqual(rec['threshold'], threshold)
                    self.assertAlmostEqual(rec['measured'], value)
                    self.assertAlmostEqual(rec['excess_usd'], expect, places=2)
                    self.assertAlmostEqual(classes.excess_usd(name, synthetic(**make(value))), expect, places=2)

    def test_every_class_is_returned_over_or_not(self):
        records = classes.evaluate(synthetic())
        self.assertEqual({r['name'] for r in records}, set(CASES))
        self.assertFalse(any(r['over'] for r in records))
        self.assertEqual([r['excess_usd'] for r in records], [0.0] * 4)

    def test_the_sentence_carries_the_number(self):
        table = synthetic(non_landing_share=0.354, minutes_per_landed_item=136.2,
                          repeat_items={'count': 21, 'items': [], 'usd_share': 0.53, 'hours_share': 0.6},
                          by_kind_model=cell(23.7))
        records = classes.evaluate(table)
        self.assertIn('35 % of session time landed nothing', record(records, 'non_landing_sessions')['sentence'])
        self.assertIn('136.2', record(records, 'time_per_landed_item')['sentence'])
        self.assertIn('53 % of spend', record(records, 'repeat_sessions')['sentence'])
        self.assertIn('adjudicate × claude-opus-5: 23.7 h', record(records, 'premium_model_in_kind')['sentence'])

    def test_ordered_by_excess_usd_descending_and_ranked_in_tens(self):
        table = synthetic(
            repeat_items={'count': 21, 'items': [], 'usd_share': 0.53, 'hours_share': 0.6},   # 0.20 x 200 = 40.0
            minutes_per_landed_item=136.2,                                                    # 16.2 x 30 / 60 x 2 = 16.2
            by_kind_model=cell(23.7),                                                         # 15.7 x 2 = 31.4
            non_landing_share=0.354)                                                          # 0.104 x 200 = 20.8
        records = classes.evaluate(table)
        self.assertEqual([r['name'] for r in records], [
            'repeat_sessions', 'premium_model_in_kind', 'non_landing_sessions', 'time_per_landed_item'])
        for r, expect in zip(records, (40.0, 31.4, 20.8, 16.2)):
            self.assertAlmostEqual(r['excess_usd'], expect, places=2)
            self.assertTrue(r['over'])
        self.assertEqual([r['rank'] for r in records], [10, 20, 30, 40])

    def test_a_class_not_over_ranks_after_the_ones_that_are(self):
        records = classes.evaluate(synthetic(non_landing_share=0.4))
        self.assertEqual(records[0]['name'], 'non_landing_sessions')
        self.assertEqual([r['over'] for r in records], [True, False, False, False])

    def test_the_premium_class_is_the_largest_premium_cell_and_ignores_other_models(self):
        cells = {('spec', 'claude-opus-5'): measure.Cell(1, 12.0, 24.0),
                 ('adjudicate', 'claude-opus-5'): measure.Cell(1, 9.0, 18.0),
                 ('coder', 'claude-sonnet-5'): measure.Cell(1, 50.0, 50.0)}
        rec = record(classes.evaluate(synthetic(by_kind_model=cells)), 'premium_model_in_kind')
        self.assertEqual(rec['measured'], 12.0)
        self.assertIn('spec × claude-opus-5', rec['sentence'])
        self.assertEqual(classes.premium_cell(synthetic(by_kind_model={}), ('claude-opus-5',)), None)

    def test_no_landed_items_is_not_over_the_time_threshold(self):
        table = synthetic(minutes_per_landed_item=None, landed_items=0)
        self.assertFalse(record(classes.evaluate(table), 'time_per_landed_item')['over'])

    def test_no_spend_means_no_excess_usd_even_over_threshold(self):
        rec = record(classes.evaluate(synthetic(non_landing_share=0.9, usd=0.0)), 'non_landing_sessions')
        self.assertTrue(rec['over'])
        self.assertEqual(rec['excess_usd'], 0.0)


class OverrideTests(unittest.TestCase):
    def test_no_block_is_the_defaults(self):
        for given in (None, {}, env.Product('p', {})):
            s = classes.settings(given)
            self.assertEqual(s.thresholds['non_landing_sessions'], 0.25)
            self.assertEqual((s.epic, s.window_days, s.premium_models), (None, None, ('claude-opus-5',)))

    def test_a_product_overrides_any_key_and_keeps_the_rest(self):
        product = env.Product('p', env.loads(
            'improve:\n  thresholds:\n    non_landing_sessions: 0.30\n  epic: E-0001\n'
            '  window_days: 28\n  premium_models: [big-model]\n'))
        s = classes.settings(product)
        self.assertEqual(s.thresholds['non_landing_sessions'], 0.30)
        self.assertEqual(s.thresholds['repeat_sessions'], 0.33)
        self.assertEqual((s.epic, s.window_days, s.premium_models), ('E-0001', 28, ('big-model',)))

    def test_an_unknown_or_non_numeric_threshold_is_ignored(self):
        s = classes.settings({'thresholds': {'made_up': 1, 'repeat_sessions': 'high', 'time_per_landed_item': True}})
        self.assertEqual(s.thresholds, {c.name: c.threshold for c in classes.CLASSES})

    def test_a_raised_threshold_moves_a_class_from_over_to_not(self):
        table = synthetic(non_landing_share=0.26)
        self.assertTrue(record(classes.evaluate(table), 'non_landing_sessions')['over'])
        conv = {'thresholds': {'non_landing_sessions': 0.30}}
        rec = record(classes.evaluate(table, conv), 'non_landing_sessions')
        self.assertFalse(rec['over'])
        self.assertEqual(rec['threshold'], 0.30)

    def test_premium_models_choose_which_cell_is_measured(self):
        cells = {('spec', 'big-model'): measure.Cell(1, 12.0, 24.0)}
        table = synthetic(by_kind_model=cells)
        self.assertFalse(record(classes.evaluate(table), 'premium_model_in_kind')['over'])
        rec = record(classes.evaluate(table, {'premium_models': ['big-model']}), 'premium_model_in_kind')
        self.assertTrue(rec['over'])
        self.assertAlmostEqual(rec['excess_usd'], 8.0)  # 4.0 h x $2/h

    def test_the_closing_line_names_the_number(self):
        rec = record(classes.evaluate(synthetic()), 'non_landing_sessions')
        self.assertEqual(rec['closes'], 'non_landing_share below 0.25, measured over a full week')


if __name__ == '__main__':
    unittest.main()
