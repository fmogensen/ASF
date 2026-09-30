"""asf.ci_measure — job kind stripping, the per-runner-per-kind readings, the fleet-relative
scores, the published baseline and its command. Every event here is a hand-built dict: nothing
reads a stream file and nothing imports :mod:`asf.ci_pool`."""
import argparse
import datetime
import json
import os
import shutil
import tempfile
import unittest

from asf import ci_measure, env

NOW = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _ts(days_ago):
    return (NOW - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def job(name, conclusion, runner, minutes=1, seconds=None, queued_s=None):
    d = {'name': name, 'conclusion': conclusion, 'runner': runner, 'minutes': minutes}
    if seconds is not None:
        d['seconds'] = seconds
    if queued_s is not None:
        d['queued_s'] = queued_s
    return d


def ev(ts, jobs, wall_minutes=0):
    return {'ts': ts, 'jobs': jobs, 'wall_minutes': wall_minutes}


def burst(runner, name, n, seconds=100.0, conclusion='success', days_ago=1):
    return [ev(_ts(days_ago), [job(name, conclusion, runner, seconds=seconds)]) for _ in range(n)]


class JobKind(unittest.TestCase):
    def test_trailing_matrix_suffix_stripped(self):
        self.assertEqual(ci_measure.job_kind('test (3.12)'), 'test')

    def test_bare_name_is_itself(self):
        self.assertEqual(ci_measure.job_kind('gate'), 'gate')

    def test_interior_parenthesis_left_alone(self):
        self.assertEqual(ci_measure.job_kind('test (a) (b)'), 'test (a)')

    def test_trailing_parenthesis_with_no_opener_left_alone(self):
        self.assertEqual(ci_measure.job_kind('weird)'), 'weird)')


class Readings(unittest.TestCase):
    def test_seconds_used_when_present(self):
        events = [ev(_ts(1), [job('gate', 'success', 'ci-1', minutes=1, seconds=120.5)])]
        r = ci_measure.readings(events, now=NOW)
        self.assertEqual(r[('ci-1', 'gate')][0].seconds, 120.5)

    def test_minutes_times_60_when_seconds_absent(self):
        events = [ev(_ts(1), [job('gate', 'success', 'ci-1', minutes=3)])]
        r = ci_measure.readings(events, now=NOW)
        self.assertEqual(r[('ci-1', 'gate')][0].seconds, 180.0)

    def test_job_with_no_runner_dropped(self):
        events = [ev(_ts(1), [job('gate', 'success', None, minutes=3)])]
        self.assertEqual(ci_measure.readings(events, now=NOW), {})

    def test_job_outside_window_dropped(self):
        events = [ev(_ts(ci_measure.WINDOW_DAYS + 6), [job('gate', 'success', 'ci-1', minutes=3)])]
        self.assertEqual(ci_measure.readings(events, now=NOW), {})

    def test_skipped_job_counts_in_neither_median_nor_green_rate(self):
        events = [ev(_ts(1), [job('gate', 'success', 'ci-1', seconds=100.0),
                               job('gate', 'success', 'ci-1', seconds=100.0),
                               job('gate', 'skipped', 'ci-1', seconds=999.0)])]
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertEqual(sc['ci-1'].medians['gate'], 100.0)
        self.assertEqual(sc['ci-1'].n, 2)
        self.assertEqual(sc['ci-1'].green_rate, 1.0)


class Scores(unittest.TestCase):
    def test_two_runners_at_min_readings_ratio_1_and_the_slower_ones_own_over_best(self):
        events = burst('ci-a', 'gate', ci_measure.MIN_READINGS, seconds=100.0) + \
            burst('ci-b', 'gate', ci_measure.MIN_READINGS, seconds=150.0)
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertAlmostEqual(sc['ci-a'].ratios['gate'], 1.0)
        self.assertAlmostEqual(sc['ci-b'].ratios['gate'], 1.5)

    def test_kind_only_one_runner_has_is_in_medians_and_in_no_ratios(self):
        events = burst('ci-c', 'special', ci_measure.MIN_READINGS, seconds=50.0)
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertEqual(sc['ci-c'].medians['special'], 50.0)
        self.assertNotIn('special', sc['ci-c'].ratios)

    def test_runner_with_no_comparable_kind_has_ratio_none(self):
        events = burst('ci-c', 'special', ci_measure.MIN_READINGS, seconds=50.0)
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertIsNone(sc['ci-c'].ratio)

    def test_runners_own_score_is_the_median_of_its_ratios_not_worst_not_mean(self):
        events = (burst('ci-d', 'k1', ci_measure.MIN_READINGS, seconds=100.0) +
                  burst('ci-e', 'k1', ci_measure.MIN_READINGS, seconds=100.0) +
                  burst('ci-d', 'k2', ci_measure.MIN_READINGS, seconds=110.0) +
                  burst('ci-f', 'k2', ci_measure.MIN_READINGS, seconds=100.0) +
                  burst('ci-d', 'k3', ci_measure.MIN_READINGS, seconds=300.0) +
                  burst('ci-g', 'k3', ci_measure.MIN_READINGS, seconds=100.0))
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertAlmostEqual(sc['ci-d'].ratio, 1.1)

    def test_12_successes_6_failures_green_rate_and_flaky(self):
        events = burst('ci-h', 'k', 12, seconds=100.0, conclusion='success') + \
            burst('ci-h', 'k', 6, seconds=100.0, conclusion='failure')
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertEqual(sc['ci-h'].green_rate, 0.667)
        self.assertTrue(sc['ci-h'].flaky)

    def test_4_successes_4_failures_not_flaky_below_min_readings(self):
        events = burst('ci-i', 'k', 4, seconds=100.0, conclusion='success') + \
            burst('ci-i', 'k', 4, seconds=100.0, conclusion='failure')
        sc = ci_measure.scores(ci_measure.readings(events, now=NOW))
        self.assertFalse(sc['ci-i'].flaky)


class Baselines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_own_p50_tier_fallback_and_neither(self):
        events = (burst('ci-1', 'gate', 6, seconds=180.0) +
                  burst('ci-9', 'gate', 2, seconds=190.0) +
                  burst('ci-2', 'gate', 5, seconds=200.0))
        r = ci_measure.readings(events, now=NOW)
        tiers = {'ci-1': 'asf-fast', 'ci-9': 'asf-fast', 'ci-2': 'asf-fast'}
        per, tier = ci_measure.baselines(r, tiers)
        self.assertEqual(per['ci-1']['gate'], (180.0, 6))
        self.assertNotIn('ci-9', per)
        self.assertIn('gate', tier['asf-fast'])

        ci_measure.write_baselines('p', r, tiers, now=NOW)
        self.assertEqual(ci_measure.baseline_for('p', 'gate', 'ci-1', tiers=tiers),
                          (180.0, 6, 'runner'))
        got9 = ci_measure.baseline_for('p', 'gate', 'ci-9', tiers=tiers)
        self.assertEqual(got9[2], 'tier')
        self.assertIsNone(ci_measure.baseline_for('p', 'nope', 'ci-9', tiers=tiers))

    def test_regressions_flags_1_6x_not_1_4x(self):
        events6 = burst('ci-4', 'gate', 5, seconds=200.0) + \
            [ev(_ts(1), [job('gate', 'success', 'ci-4', seconds=320.0)])]
        r6 = ci_measure.readings(events6, now=NOW)
        per6, _ = ci_measure.baselines(r6, {})
        self.assertEqual(ci_measure.regressions(r6, per6), [('ci-4', 'gate', 320.0, 200.0)])

        events4 = burst('ci-5', 'gate', 5, seconds=200.0) + \
            [ev(_ts(1), [job('gate', 'success', 'ci-5', seconds=280.0)])]
        r4 = ci_measure.readings(events4, now=NOW)
        per4, _ = ci_measure.baselines(r4, {})
        self.assertEqual(ci_measure.regressions(r4, per4), [])

    def test_run_wall_p50_ignores_zero_and_returns_none(self):
        zeros = [ev(_ts(1), [], wall_minutes=0), ev(_ts(2), [], wall_minutes=0)]
        self.assertIsNone(ci_measure.run_wall_p50(zeros, now=NOW))
        withsome = zeros + [ev(_ts(1), [], wall_minutes=12.0), ev(_ts(2), [], wall_minutes=14.0)]
        self.assertEqual(ci_measure.run_wall_p50(withsome, now=NOW), 12.0)


class BaselineCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_baselines(self):
        doc = {
            'v': 1, 'taken': '2026-09-30T11:04:07Z', 'window_days': 14,
            'per': {'ci-1': {'gate': {'p50_s': 182.4, 'n': 19}}},
            'tier': {'asf-fast': {'gate': {'p50_s': 188.1, 'n': 34}}},
        }
        with open(os.path.join(env.state_dir('p'), ci_measure.BASELINES_FILE), 'w',
                  encoding='utf-8') as f:
            json.dump(doc, f)

    def _write_census(self):
        doc = {'runners': [{'runner': 'ci-9', 'tier': 'asf-fast'}]}
        with open(os.path.join(env.state_dir('p'), 'ci-census.json'), 'w', encoding='utf-8') as f:
            json.dump(doc, f)

    def test_prints_the_runners_own_p50(self):
        self._write_baselines()
        out = []
        args = argparse.Namespace(product='p', job='gate', runner='ci-1', json=False)
        rc = ci_measure.cmd_baseline(args, out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(out, [182.4])

    def test_falls_back_to_the_tier_with_json(self):
        self._write_baselines()
        self._write_census()
        out = []
        args = argparse.Namespace(product='p', job='gate', runner='ci-9', json=True)
        rc = ci_measure.cmd_baseline(args, out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out[0]), {'seconds': 188.1, 'n': 34, 'from': 'tier'})

    def test_exits_1_with_no_output_for_a_kind_in_neither(self):
        self._write_baselines()
        out = []
        args = argparse.Namespace(product='p', job='nope', runner='ci-9', json=False)
        rc = ci_measure.cmd_baseline(args, out=out.append)
        self.assertEqual(rc, 1)
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
