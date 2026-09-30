"""asf.ci_census — ``ci.pool: discover``: the empty-census degradation, the census that tiers a
fake fleet off a hand-written ``ci`` stream, the band that stops a flap, the floor that keeps the
fast tier full, and the labels ``apply_tiers`` writes (and only those). No test shells out: the
host is :class:`tests.test_ci_pool.FakeBackend`."""
import datetime
import json
import os
import tempfile
import unittest

from asf import capacity, ci_census, ci_measure, ci_pool, env
from asf.metrics import metrics
from tests.test_ci_pool import BASE, FakeBackend, Home, pool_data, runner

NOW = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)


def product(pool='discover', reserve=None, name='p'):
    ci = {'provider': 'github-actions', 'pool': pool}
    if reserve is not None:
        ci['reserve'] = reserve
    return env.Product(name, {'repo_slug': 'o/r', 'ci': ci})


def _ts(days_ago=1):
    return (NOW - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def job(name, conclusion, runner_name, seconds=100.0):
    return {'name': name, 'conclusion': conclusion, 'runner': runner_name, 'seconds': seconds}


def ev(jobs, days_ago=1, wall_minutes=0):
    return {'ts': _ts(days_ago), 'jobs': jobs, 'wall_minutes': wall_minutes}


def burst(runner_name, kind, n, seconds=100.0, conclusion='success', days_ago=1):
    return [ev([job(kind, conclusion, runner_name, seconds=seconds)], days_ago=days_ago)
            for _ in range(n)]


def write_stream(root, events):
    """Every event into its day's ``metrics/ci/<day>.jsonl`` — a raw write, not
    :func:`asf.metrics.metrics.append_event`: :mod:`asf.metrics.metrics`'s ``ci`` schema does not
    carry ``seconds``/``queued_s``/``wall_minutes`` yet (Task 2, not this Task), and
    :func:`asf.metrics.metrics.read_stream` never validates what it reads."""
    by_day = {}
    for e in events:
        by_day.setdefault(e['ts'][:10], []).append(e)
    for day, evs in by_day.items():
        path = metrics.stream_path(root, 'ci', day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for e in evs:
                f.write(json.dumps(e) + '\n')


def score(ratio, n=10, green_rate=1.0, flaky=False, medians=None, ratios=None):
    return ci_measure.Score(runner='x', medians=medians or {}, ratios=ratios or {}, ratio=ratio,
                            n=n, green_rate=green_rate, flaky=flaky)


class PoolMode(unittest.TestCase):
    def test_pool_problems_accepts_discover_and_refuses_other_strings(self):
        self.assertEqual(ci_pool.pool_problems({'pool': 'discover'}), [])
        keys = [k for k, _why in ci_pool.pool_problems({'pool': 'auto'})]
        self.assertEqual(keys, ['ci.pool'])
        [(_k, why)] = ci_pool.pool_problems({'pool': 'auto'})
        self.assertIn('discover', why)

    def test_pool_mode_of_the_three_forms(self):
        self.assertEqual(ci_pool.pool_mode(product(pool='discover')), 'discover')
        self.assertEqual(ci_pool.pool_mode(product(pool=pool_data())), 'declared')
        self.assertEqual(ci_pool.pool_mode(product(pool=None)), 'none')


class NoCensus(Home):
    def test_cached_with_no_file_returns_empty(self):
        self.assertEqual(ci_census.cached(product()), [])

    def test_downstream_readers_degrade_exactly_like_no_pool(self):
        p = product()
        self.assertEqual(ci_pool.pool_ci(p), (None, None))
        self.assertEqual(ci_pool.doctor_rows(p), [])
        self.assertIsNone(capacity.runner_ci_free(p))
        self.assertEqual(ci_census.census_rows(p),
                         [(True, False, 'ci census: no census yet — the next tick takes one')])

    def test_census_rows_is_empty_for_a_declared_or_no_pool_product(self):
        self.assertEqual(ci_census.census_rows(product(pool=pool_data())), [])
        self.assertEqual(ci_census.census_rows(product(pool=None)), [])

    def test_a_wrong_version_is_empty(self):
        path = os.path.join(env.state_dir('p'), ci_census.CENSUS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'v': 2, 'runners': [{'runner': 'ci-1', 'tier': 'asf-fast'}]}, f)
        self.assertEqual(ci_census.cached(product()), [])

    def test_truncated_json_is_empty(self):
        path = os.path.join(env.state_dir('p'), ci_census.CENSUS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assertEqual(ci_census.cached(product()), [])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores file permissions')
    def test_an_unreadable_file_is_empty(self):
        path = os.path.join(env.state_dir('p'), ci_census.CENSUS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'v': 1, 'runners': [{'runner': 'ci-1', 'tier': 'asf-fast'}]}, f)
        os.chmod(path, 0)
        try:
            self.assertEqual(ci_census.cached(product()), [])
        finally:
            os.chmod(path, 0o644)


class Refresh(Home):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(dir=self.tmp)
        self.p = product()
        self.runners = [runner(f'ci-{i}') for i in range(1, 6)]
        self.backend = FakeBackend(self.runners, [])
        events = burst('ci-1', 'gate', ci_measure.MIN_READINGS, seconds=100.0)
        for name in ('ci-2', 'ci-3', 'ci-4', 'ci-5'):
            events += burst(name, 'gate', ci_measure.MIN_READINGS, seconds=250.0)
        write_stream(self.root, events)

    def test_one_entry_per_runner_sorted_by_name_slots_one_no_inventory(self):
        ci_census.refresh(self.p, self.backend, root=self.root, now=NOW, out=lambda *_: None)
        pool = ci_pool.load_pool(self.p)
        self.assertEqual([e.runner for e in pool], ['ci-1', 'ci-2', 'ci-3', 'ci-4', 'ci-5'])
        self.assertTrue(all(e.slots == 1 for e in pool))
        self.assertTrue(all((e.provider, e.box, e.size, e.cls) == ('', '', '', '') for e in pool))

    def test_product_ci_needs_no_change_to_capacity_py(self):
        # PD1: slots_by_role sorts alphabetically, so 'asf-bulk' precedes 'asf-fast' — the order
        # is slots_by_role's, not a tier ranking (O9 forbids reordering that function).
        ci_census.refresh(self.p, self.backend, root=self.root, now=NOW, out=lambda *_: None)
        self.assertEqual(capacity.product_ci(self.p, {}), (5, 'ci.pool: asf-bulk 4, asf-fast 1'))

    def test_a_sixth_runner_raises_the_ceiling_with_no_product_file_edit(self):
        ci_census.refresh(self.p, self.backend, root=self.root, now=NOW, out=lambda *_: None)
        self.backend._runners['ci-6'] = runner('ci-6')
        ci_census.refresh(self.p, self.backend, root=self.root, now=NOW, out=lambda *_: None)
        pool = ci_pool.load_pool(self.p)
        self.assertEqual(len(pool), 6)
        by_name = {e.runner: e.role for e in pool}
        self.assertEqual(by_name['ci-6'], ci_census.BULK)
        self.assertEqual(capacity.product_ci(self.p, {})[0], 6)

    def test_no_workflow_file_is_ever_read(self):
        class CountingBackend(FakeBackend):
            runs_on_calls = 0

            def runs_on(self):
                self.runs_on_calls += 1
                return []

        backend = CountingBackend(self.runners, [])
        ci_census.refresh(self.p, backend, root=self.root, now=NOW, out=lambda *_: None)
        self.assertEqual(backend.runs_on_calls, 0)

    def test_an_unreadable_host_takes_no_census_and_raises_nothing(self):
        class Failing(FakeBackend):
            def runners(self):
                raise ci_pool.BackendError('boom')

        lines = []
        moves = ci_census.refresh(self.p, Failing([], []), root=self.root, now=NOW, out=lines.append)
        self.assertEqual(moves, [])
        self.assertTrue(any('boom' in l for l in lines))
        self.assertIsNone(ci_census.taken_at(self.p))


class ApplyTiers(Home):
    def setUp(self):
        super().setUp()
        self.p = product()

    def test_a_demotion_adds_then_removes_and_leaves_other_labels(self):
        r = runner('ci-1', 'asf-fast', 'class-x')
        backend = FakeBackend([r], [])
        moves = [ci_census.Move('ci-1', ci_census.FAST, ci_census.BULK, None, False, 'demoted')]
        ci_census.apply_tiers(self.p, backend, moves, out=lambda *_: None)
        self.assertEqual(backend.writes, [('add', 'ci-1', ('asf-bulk',)), ('remove', 'ci-1', 'asf-fast')])
        self.assertEqual(backend._runners['ci-1'].norm_labels(),
                         {l.lower() for l in BASE} | {'asf-bulk', 'class-x'})

    def test_a_first_time_placement_only_adds(self):
        r = runner('ci-7')
        backend = FakeBackend([r], [])
        moves = [ci_census.Move('ci-7', None, ci_census.BULK, None, False, 'provisional')]
        ci_census.apply_tiers(self.p, backend, moves, out=lambda *_: None)
        self.assertEqual(backend.writes, [('add', 'ci-7', ('asf-bulk',))])

    def test_promotion_to_fast_starts_a_trial_whose_labels_before_is_the_whole_set(self):
        r = runner('ci-2', 'asf-bulk', 'class-y', 'asf-pr-fast')
        backend = FakeBackend([r], [])
        moves = [ci_census.Move('ci-2', ci_census.BULK, ci_census.FAST, None, False, 'promoted')]
        ci_census.apply_tiers(self.p, backend, moves, out=lambda *_: None)
        trials = ci_pool.load_trials(self.p.name)
        self.assertEqual(set(trials.keys()), {'ci-2'})
        self.assertEqual(set(trials['ci-2']['labels_before']),
                         {l.lower() for l in BASE} | {'asf-bulk', 'class-y', 'asf-pr-fast'})

    def test_a_failed_trial_rolls_back_to_every_original_label_not_just_the_tier(self):
        r = runner('ci-2', 'asf-bulk', 'class-y', 'asf-pr-fast')
        backend = FakeBackend([r], [])
        moves = [ci_census.Move('ci-2', ci_census.BULK, ci_census.FAST, None, False, 'promoted')]
        ci_census.apply_tiers(self.p, backend, moves, out=lambda *_: None)
        since = ci_pool.load_trials(self.p.name)['ci-2']['since']
        backend.jobs['ci-2'] = [{'name': 'gate', 'status': 'completed', 'conclusion': 'failure',
                                 'url': 'https://ci/x', 'started_at': since}]
        ci_pool.check_trials(self.p.name, backend, apply_changes=True, out=lambda *_: None)
        self.assertEqual(backend._runners['ci-2'].norm_labels(),
                         {l.lower() for l in BASE} | {'asf-bulk', 'class-y', 'asf-pr-fast'})
        self.assertEqual(ci_pool.load_trials(self.p.name)['ci-2']['state'], 'rolled_back')


class Tier(unittest.TestCase):
    def test_no_score_is_bulk(self):
        self.assertEqual(ci_census.tier_of(None), ci_census.BULK)

    def test_no_ratio_is_bulk(self):
        self.assertEqual(ci_census.tier_of(score(ratio=None)), ci_census.BULK)

    def test_fewer_than_min_readings_is_bulk_whatever_the_ratio(self):
        self.assertEqual(ci_census.tier_of(score(ratio=1.0, n=3)), ci_census.BULK)

    def test_flaky_is_bulk_at_any_ratio(self):
        self.assertEqual(ci_census.tier_of(score(ratio=1.0, n=10, flaky=True)), ci_census.BULK)

    def test_at_or_below_promote_at_is_fast(self):
        self.assertEqual(ci_census.tier_of(score(ratio=1.20, n=10)), ci_census.FAST)
        self.assertEqual(ci_census.tier_of(score(ratio=ci_census.PROMOTE_AT, n=10)), ci_census.FAST)

    def test_at_or_above_demote_at_is_bulk(self):
        self.assertEqual(ci_census.tier_of(score(ratio=1.70, n=10)), ci_census.BULK)
        self.assertEqual(ci_census.tier_of(score(ratio=ci_census.DEMOTE_AT, n=10)), ci_census.BULK)

    def test_the_band_holds_the_carried_tier_else_bulk(self):
        self.assertEqual(ci_census.tier_of(score(ratio=1.40, n=10), carried=ci_census.FAST),
                         ci_census.FAST)
        self.assertEqual(ci_census.tier_of(score(ratio=1.40, n=10), carried=ci_census.BULK),
                         ci_census.BULK)
        self.assertEqual(ci_census.tier_of(score(ratio=1.40, n=10), carried=None), ci_census.BULK)


class Band(Home):
    def test_two_refresh_calls_at_1_40_hold_the_carried_fast_tier(self):
        p = product()
        backend = FakeBackend([runner('ci-1'), runner('ci-2')], [])
        root = tempfile.mkdtemp(dir=self.tmp)
        write_stream(root, burst('ci-1', 'gate', ci_measure.MIN_READINGS, seconds=100.0) +
                    burst('ci-2', 'gate', ci_measure.MIN_READINGS, seconds=100.0))
        moves = ci_census.refresh(p, backend, root=root, now=NOW, out=lambda *_: None)
        # the tick always pairs refresh with apply_tiers (PD5/D8); without it the host never
        # carries the label the census just computed, and the next census would rightly see
        # that as drift, not a held band.
        ci_census.apply_tiers(p, backend, moves, out=lambda *_: None)
        by_name = {e.runner: e.role for e in ci_pool.load_pool(p)}
        self.assertEqual(by_name['ci-2'], ci_census.FAST)

        write_stream(root, burst('ci-1', 'gate', ci_measure.MIN_READINGS, seconds=100.0) +
                    burst('ci-2', 'gate', ci_measure.MIN_READINGS, seconds=140.0))
        moves = ci_census.refresh(p, backend, root=root, now=NOW, out=lambda *_: None)
        self.assertEqual(moves, [])
        by_name = {e.runner: e.role for e in ci_pool.load_pool(p)}
        self.assertEqual(by_name['ci-2'], ci_census.FAST)


class Floor(unittest.TestCase):
    def test_every_runner_above_demote_at_still_ends_with_exactly_one_fast(self):
        runners = [runner('ci-1'), runner('ci-2'), runner('ci-3')]
        sc = {'ci-1': score(1.90), 'ci-2': score(1.62), 'ci-3': score(2.40)}
        tiers, floored = ci_census._place(runners, {}, sc)
        self.assertEqual([n for n, t in tiers.items() if t == ci_census.FAST], ['ci-2'])
        self.assertEqual(floored, {'ci-2'})

    def test_a_fleet_with_one_qualifying_runner_is_not_floored(self):
        runners = [runner('ci-1'), runner('ci-2')]
        sc = {'ci-1': score(1.10), 'ci-2': score(1.90)}
        tiers, floored = ci_census._place(runners, {}, sc)
        self.assertEqual(floored, set())
        self.assertEqual(tiers['ci-1'], ci_census.FAST)
        self.assertEqual(tiers['ci-2'], ci_census.BULK)

    def test_an_offline_runner_is_never_floored_in(self):
        runners = [runner('ci-1', online=False), runner('ci-2')]
        sc = {'ci-1': score(1.0), 'ci-2': score(1.90)}
        tiers, floored = ci_census._place(runners, {}, sc)
        self.assertEqual(floored, {'ci-2'})
        self.assertNotEqual(tiers['ci-1'], ci_census.FAST)


class Places(unittest.TestCase):
    def test_a_demote_line_names_the_worst_kind_and_the_threshold(self):
        s = ci_measure.Score('ci-3', {'gate': 384.0}, {'gate': 2.1}, 2.1, 11, 1.0, False)
        mv = ci_census.Move('ci-3', ci_census.FAST, ci_census.BULK, s, False,
                            ci_census._reason_for(ci_census.BULK, s, False))
        line = mv.line()
        self.assertIn('ci-3 asf-fast → asf-bulk', line)
        self.assertIn('gate', line)
        self.assertIn('384', line)
        self.assertIn('2.1', line)
        self.assertIn('11 green readings in 14 d', line)

    def test_a_promote_line_names_the_overall_ratio_and_the_kind_count(self):
        s = ci_measure.Score('ci-5', {'a': 100, 'b': 100, 'c': 100},
                             {'a': 1.0, 'b': 1.04, 'c': 1.1}, 1.04, 18, 1.0, False)
        mv = ci_census.Move('ci-5', ci_census.BULK, ci_census.FAST, s, False,
                            ci_census._reason_for(ci_census.FAST, s, False))
        line = mv.line()
        self.assertIn('ci-5 asf-bulk → asf-fast', line)
        self.assertIn('1.04', line)
        self.assertIn('3 job kinds', line)
        self.assertIn('18 green readings in 14 d', line)

    def test_a_provisional_line_for_a_first_time_runner_has_no_from_tier(self):
        s = ci_measure.Score('ci-7', {}, {}, None, 2, 0.0, False)
        mv = ci_census.Move('ci-7', None, ci_census.BULK, s, False,
                            ci_census._reason_for(ci_census.BULK, s, False))
        line = mv.line()
        self.assertTrue(line.startswith('ci place: ci-7 → asf-bulk'))
        self.assertIn('2 green readings, fewer than 5: provisional', line)

    def test_a_floor_line_says_the_fast_tier_would_be_empty(self):
        s = ci_measure.Score('ci-2', {}, {}, 2.4, 6, 1.0, False)
        mv = ci_census.Move('ci-2', None, ci_census.FAST, s, True,
                            ci_census._reason_for(ci_census.FAST, s, True))
        line = mv.line()
        self.assertTrue(line.startswith('ci place: ci-2 → asf-fast'))
        self.assertIn('the fast tier would be empty', line)
        self.assertIn('2.4', line)


class PlacesFile(Home):
    def test_refresh_appends_one_places_line_per_move_and_prints_it(self):
        p = product()
        backend = FakeBackend([runner('ci-1')], [])
        root = tempfile.mkdtemp(dir=self.tmp)
        write_stream(root, burst('ci-1', 'gate', ci_measure.MIN_READINGS, seconds=100.0))
        lines = []
        ci_census.refresh(p, backend, root=root, now=NOW, out=lines.append)
        path = os.path.join(env.state_dir(p), ci_census.PLACES_FILE)
        with open(path, encoding='utf-8') as f:
            recorded = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]['runner'], 'ci-1')
        self.assertEqual(recorded[0]['to'], ci_census.FAST)
        self.assertTrue(any(l.startswith('ci place: ci-1') for l in lines))

    def test_no_moves_prints_the_summary_line_only(self):
        # PD5/D8: ci-1 is floored (its lone-runner ratio is unmeasured) — a steady state needs
        # the label actually on the host (refresh then apply_tiers, as the tick always pairs
        # them), or the floor would keep re-emitting the move every census (the C fix).
        p = product()
        backend = FakeBackend([runner('ci-1')], [])
        root = tempfile.mkdtemp(dir=self.tmp)
        write_stream(root, burst('ci-1', 'gate', ci_measure.MIN_READINGS, seconds=100.0))
        moves = ci_census.refresh(p, backend, root=root, now=NOW, out=lambda *_: None)
        ci_census.apply_tiers(p, backend, moves, out=lambda *_: None)
        places_path = os.path.join(env.state_dir(p), ci_census.PLACES_FILE)
        before = os.path.getsize(places_path)
        lines = []
        ci_census.refresh(p, backend, root=root, now=NOW, out=lines.append)
        self.assertEqual(lines, ['ci census: 1 runners — asf-fast 1'])
        self.assertEqual(os.path.getsize(places_path), before)


class Reconcile(Home):
    """C (review round 1): a floored promotion (D8) that fails its trial (D10) must not stay
    silently unlabelled forever — the floor re-selects the same runner every census, and without
    checking the host's actual labels that looked like ``to == frm``, no move."""

    def test_a_rolled_back_floored_trial_is_re_placed_by_the_next_census(self):
        p = product()
        backend = FakeBackend([runner('ci-1')], [])
        root = tempfile.mkdtemp(dir=self.tmp)  # no readings at all: ci-1's ratio is unmeasured

        moves = ci_census.refresh(p, backend, root=root, now=NOW, out=lambda *_: None)
        self.assertEqual([mv.to for mv in moves], [ci_census.FAST])
        ci_census.apply_tiers(p, backend, moves, out=lambda *_: None)
        self.assertIn('asf-fast', backend._runners['ci-1'].norm_labels())

        since = ci_pool.load_trials(p.name)['ci-1']['since']
        backend.jobs['ci-1'] = [{'name': 'gate', 'status': 'completed', 'conclusion': 'failure',
                                 'url': 'https://ci/x', 'started_at': since}]
        ci_pool.check_trials(p.name, backend, apply_changes=True, out=lambda *_: None)
        self.assertNotIn('asf-fast', backend._runners['ci-1'].norm_labels())
        self.assertEqual(ci_pool.load_trials(p.name)['ci-1']['state'], 'rolled_back')

        # the census file still says FAST (unmoved by the rollback) — the next census must
        # notice the host lost the label and re-place it, not treat frm == to as steady state.
        lines = []
        moves2 = ci_census.refresh(p, backend, root=root, now=NOW, out=lines.append)
        self.assertEqual([(mv.runner, mv.frm, mv.to) for mv in moves2],
                         [('ci-1', ci_census.FAST, ci_census.FAST)])
        self.assertTrue(any(l.startswith('ci place: ci-1') for l in lines))

        ci_census.apply_tiers(p, backend, moves2, out=lambda *_: None)
        self.assertIn('asf-fast', backend._runners['ci-1'].norm_labels())
        self.assertEqual(ci_pool.load_trials(p.name)['ci-1']['state'], 'pending')

    def test_a_rolled_back_merit_promoted_trial_is_re_placed_by_the_next_census(self):
        # C (review round 2): the same drift, reached without the floor (D8) — ci-1 is promoted
        # on its own ratio, ci-2 stays BULK and is never floored in, so `floored` is empty and
        # round 2's `name in floored` gate missed exactly this path.
        p = product()
        backend = FakeBackend([runner('ci-1'), runner('ci-2')], [])
        root = tempfile.mkdtemp(dir=self.tmp)
        write_stream(root, burst('ci-1', 'gate', ci_measure.MIN_READINGS, seconds=100.0) +
                    burst('ci-2', 'gate', ci_measure.MIN_READINGS, seconds=250.0))

        moves = ci_census.refresh(p, backend, root=root, now=NOW, out=lambda *_: None)
        by_runner = {mv.runner: mv for mv in moves}
        self.assertEqual(by_runner['ci-1'].to, ci_census.FAST)
        self.assertFalse(by_runner['ci-1'].floored)
        ci_census.apply_tiers(p, backend, moves, out=lambda *_: None)
        self.assertIn('asf-fast', backend._runners['ci-1'].norm_labels())

        since = ci_pool.load_trials(p.name)['ci-1']['since']
        backend.jobs['ci-1'] = [{'name': 'gate', 'status': 'completed', 'conclusion': 'failure',
                                 'url': 'https://ci/x', 'started_at': since}]
        ci_pool.check_trials(p.name, backend, apply_changes=True, out=lambda *_: None)
        self.assertNotIn('asf-fast', backend._runners['ci-1'].norm_labels())
        self.assertEqual(ci_pool.load_trials(p.name)['ci-1']['state'], 'rolled_back')

        # ci-1's ratio still qualifies it for FAST on its own merits (ci-2 never reaches FAST,
        # so the floor never fires) — the census file still says FAST, and the next census must
        # still notice the host lost the label and re-place it.
        lines = []
        moves2 = ci_census.refresh(p, backend, root=root, now=NOW, out=lines.append)
        self.assertEqual([(mv.runner, mv.frm, mv.to) for mv in moves2 if mv.runner == 'ci-1'],
                         [('ci-1', ci_census.FAST, ci_census.FAST)])
        self.assertTrue(any(l.startswith('ci place: ci-1') for l in lines))

        ci_census.apply_tiers(p, backend, moves2, out=lambda *_: None)
        self.assertIn('asf-fast', backend._runners['ci-1'].norm_labels())
        self.assertEqual(ci_pool.load_trials(p.name)['ci-1']['state'], 'pending')


class Command(Home):
    def _write_product(self):
        with open(os.path.join(self.tmp, 'products', 'p.yaml'), 'w', encoding='utf-8') as f:
            f.write('product: p\nrepo_slug: o/r\nci:\n  provider: github-actions\n')

    def setUp(self):
        super().setUp()
        self._write_product()
        self.backend = FakeBackend([runner('ci-1')], [])
        self.root = tempfile.mkdtemp(dir=self.tmp)

    def _run(self, apply, out):
        import argparse
        from unittest import mock
        args = argparse.Namespace(product='p', apply=apply)
        with mock.patch.object(ci_pool, 'backend_for', return_value=self.backend), \
                mock.patch('asf.tick.shadow.record_dir', return_value=self.root):
            return ci_census.cmd_census(args, out=out.append)

    def test_a_dry_run_prints_the_table_and_writes_nothing(self):
        out = []
        rc = self._run(apply=False, out=out)
        self.assertEqual(rc, 0)
        self.assertTrue(any('dry run' in l for l in out))
        self.assertFalse(os.path.exists(os.path.join(env.state_dir('p'), ci_census.CENSUS_FILE)))

    def test_apply_takes_the_census_and_writes_the_labels(self):
        out = []
        rc = self._run(apply=True, out=out)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(env.state_dir('p'), ci_census.CENSUS_FILE)))
        self.assertIn('asf-fast', self.backend._runners['ci-1'].norm_labels())


if __name__ == '__main__':
    unittest.main()
