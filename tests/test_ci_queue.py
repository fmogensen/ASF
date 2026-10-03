"""asf.ci_queue — the CI start queue against a fake ``gh``: admission by free runners per class
against measured jobs per class, priority order, the capacity.ci ceiling above the queue, the
dry-run mode, the status clause, and the fallback for a product with no ci.pool (no ``gh`` call)."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import ci_census, ci_queue, env
from asf.harvest import deploy


def pool_data():
    return [
        {'runner': 'h1', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'h2', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'h3', 'provider': 'beta', 'role': 'heavy'},
        {'runner': 'l1', 'provider': 'alpha', 'role': 'light', 'slots': 2},
    ]


def product(pool=True, cap=None, queue=None, reserve=None, name='p'):
    ci = {'provider': 'github-actions', 'workflow': 'ci.yml'}
    if pool:
        ci['pool'] = pool_data() if pool is True else pool
    if queue is not None:
        ci['queue'] = queue
    if reserve is not None:
        ci['reserve'] = reserve
    data = {'repo_slug': 'o/r', 'ci': ci}
    if cap is not None:
        data['capacity'] = cap
    return env.Product(name, data)


class FakeGh:
    """``subprocess.run`` for ``gh``: runners, a workflow's run history, its jobs, runs in flight.
    Every argv is logged."""

    def __init__(self, busy=(), offline=(), history=None, inflight=0, listed=None, files=None,
                 pool=None, labels=None, running=()):
        self.busy, self.offline = set(busy), set(offline)
        #: ``{run id: [changed file]}``: the PR (numbered as its run) each run was for
        self.files = files or {}
        #: the completed runs ``gh run list`` names (default: every history run, a success)
        self.listed = listed
        #: the runner rows the runners response iterates (default: the shared four-runner pool)
        self.pool = pool if pool is not None else pool_data()
        #: ``{runner: [label]}``, replacing the hardcoded ``self-hosted`` + role when given
        self.labels = labels
        #: ``[(run id, branch, sha, [runner name])]``: runs ``busy_runners()`` finds in progress
        self.running = list(running)
        # three runs of ci.yml: heavy 3, 3, 2 jobs and one light job each
        self.history = history if history is not None else [
            [{'runner_name': 'h1'}, {'runner_name': 'h2'}, {'runner_name': 'h3'},
             {'runner_name': 'l1'}],
            [{'runner_name': 'h1'}, {'runner_name': 'h3'}, {'runner_name': 'h2'},
             {'runner_name': 'l1'}],
            [{'runner_name': 'h1'}, {'runner_name': 'x-hosted', 'labels': ['ubuntu-latest']},
             {'runner_name': '', 'labels': ['self-hosted', 'heavy']}, {'runner_name': 'l1'}],
        ]
        self.inflight = inflight
        self.calls = []

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        out = ''
        if argv[:2] == ['gh', 'api'] and any('actions/runners' in a for a in argv):
            def _labels(r):
                if self.labels is not None:
                    return [{'name': l, 'type': 'custom'} for l in self.labels.get(r['runner'], [])]
                return [{'name': 'self-hosted', 'type': 'read-only'},
                        {'name': r['role'], 'type': 'custom'}]
            out = '\n'.join(json.dumps({
                'name': r['runner'], 'id': i, 'busy': r['runner'] in self.busy,
                'status': 'offline' if r['runner'] in self.offline else 'online',
                'labels': _labels(r)})
                for i, r in enumerate(self.pool))
        elif argv[:3] == ['gh', 'run', 'list'] and '--status' in argv:
            out = json.dumps(self.listed if self.listed is not None else [
                {'databaseId': i, 'conclusion': 'success', 'attempt': 1}
                for i in range(1, len(self.history) + 1)])
        elif argv[:3] == ['gh', 'run', 'list']:
            out = str(self.inflight)
        elif self.running and argv[:2] == ['gh', 'api'] and any(
                'actions/runs?status=' in a for a in argv):
            # unset ``running`` (the default): unreadable, today's behaviour against every other
            # fake caller of this listing (the phantom watch among them)
            status = next(a for a in argv if 'actions/runs?status=' in a
                         ).split('status=')[1].split('&')[0]
            rows = self.running if status == 'in_progress' else []
            lines = [str(len(rows))] + [
                json.dumps({'id': rid, 'status': 'in_progress', 'conclusion': None,
                           'event': 'pull_request', 'head_branch': branch, 'head_sha': sha,
                           'created_at': '2026-09-25T12:00:00Z',
                           'path': '.github/workflows/ci.yml'})
                for rid, branch, sha, _runners in rows]
            out = '\n'.join(lines)
        elif argv[:2] == ['gh', 'api'] and any('/jobs' in a for a in argv):
            run_id = int(next(a for a in argv if '/jobs' in a).split('/runs/')[1].split('/')[0])
            running = next((r for r in self.running if r[0] == run_id), None)
            if running is not None:
                names = running[3]
                out = '\n'.join(json.dumps({
                    'name': n, 'status': 'in_progress',
                    'labels': (self.labels or {}).get(n, []), 'runner_name': n,
                    'created_at': '2026-09-25T12:00:00Z', 'started_at': None,
                    'completed_at': None, 'steps': []}) for n in names)
            else:
                out = '\n'.join(json.dumps(j) for j in
                                (self.history[run_id - 1] if run_id <= len(self.history) else ()))
        elif argv[:2] == ['gh', 'api'] and any('/pulls/' in a for a in argv):
            n = int(next(a for a in argv if '/pulls/' in a).split('/pulls/')[1].split('/')[0])
            out = '\n'.join(self.files.get(n) or ())
        elif argv[:2] == ['gh', 'api'] and any('actions/runs/' in a for a in argv):
            run_id = int(next(a for a in argv if 'actions/runs/' in a).split('/runs/')[1]
                         .split('/')[0].split('?')[0])
            out = str(run_id) if run_id in self.files else ''
        return subprocess.CompletedProcess(argv, 0, out, '')


class DeadGh:
    """A ``gh`` that fails every call: the host is unreadable."""

    def __call__(self, argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, '', 'unreachable')


class NoGh:
    def __call__(self, argv, **_kw):
        raise AssertionError(f'gh called for an unqueued product: {argv}')


ITEMS = {
    'T-0341': {'id': 'T-0341', 'type': 'task', 'parent': 'S-0001'},
    'S-0001': {'id': 'S-0001', 'type': 'story', 'parent': 'F-0001'},
    'F-0001': {'id': 'F-0001', 'type': 'feature', 'customer_facing': True},
    'T-0500': {'id': 'T-0500', 'type': 'task'},
    'B-0007': {'id': 'B-0007', 'type': 'bug', 'severity': 'S1'},
    'B-0008': {'id': 'B-0008', 'type': 'bug', 'severity': 'S2'},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.lines = []
        self.t0 = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def queue(self, p, gh, minutes=0, **kw):
        return ci_queue.Queue(p, source=ci_queue.GitHubSource(p, run=gh), out=self.lines.append,
                              now=self.t0 + datetime.timedelta(minutes=minutes), **kw)

    def admit(self, q, key, item, kind='pr', branch=''):
        return ci_queue.admit(q.product, key, kind, item=item, items=ITEMS, branch=branch,
                              queue=q)


class TestMeasure(Base):
    def setUp(self):
        super().setUp()
        self.product = product()

    @staticmethod
    def _hold_gh():
        # one heavy run: h1 created 10:00, done 10:33; h2 created 10:02, done 11:06 — the class's
        # span is the earliest created to the latest completed, 66 min (3960 s)
        heavy = [
            {'runner_name': 'h1', 'conclusion': 'success', 'created_at': '2026-09-25T10:00:00Z',
             'started_at': '2026-09-25T10:00:30Z', 'completed_at': '2026-09-25T10:33:00Z'},
            {'runner_name': 'h2', 'conclusion': 'success', 'created_at': '2026-09-25T10:02:00Z',
             'started_at': '2026-09-25T10:02:30Z', 'completed_at': '2026-09-25T11:06:00Z'}]
        return FakeGh(history=[heavy])

    def queue(self, p=None, gh=None, minutes=0, **kw):
        if p is None and gh is None:
            q = super().queue(self.product, self._hold_gh(), minutes=minutes, **kw)
            q.needs('ci.yml')
            ci_queue.save(self.product.name, q.data)
            return q
        return super().queue(p, gh, minutes=minutes, **kw)

    def empty_queue(self):
        return super().queue(self.product, FakeGh(history=[]))

    def test_the_hold_is_the_p90_of_created_to_the_last_job_of_the_class(self):
        self.assertEqual(3960, self.queue().hold_s('ci.yml', 'heavy'))
        cached = ci_queue.load(self.product.name)['expect']['ci.yml']
        self.assertEqual(ci_queue.EXPECT_VERSION, cached['v'])

    def test_nothing_measured_is_zero_and_never_a_refusal(self):
        self.assertEqual(0, self.empty_queue().hold_s('ci.yml', 'heavy'))

    def test_needs_are_the_median_per_class_capped_at_the_pool(self):
        pool = env.Product('p', {'ci': {'pool': pool_data()}})
        from asf import ci_pool
        needs = ci_queue.needs_from_history(FakeGh().history, ci_pool.load_pool(pool))
        # heavy per run 3, 3, 1 (a job with no runner never ran) → p90 3; light 1; the hosted
        # job needs none. No timestamps: every job counts as running the whole run.
        self.assertEqual(needs, {'heavy': 3, 'light': 1})
        big = [[{'runner_name': 'h1'}] * 9]
        self.assertEqual(ci_queue.needs_from_history(big, ci_pool.load_pool(pool)), {'heavy': 3})

    def test_free_runners_are_online_and_idle_at_their_slots(self):
        gh = FakeGh(busy={'h1'}, offline={'h2'})
        q = self.queue(product(), gh)
        self.assertEqual(q.free(), {'heavy': 1, 'light': 2})

    def test_the_status_runners_row_and_the_queue_free_agree_from_one_read(self):
        from asf.views import status
        gh = FakeGh(busy={'h1', 'h3'}, offline={'h2'})
        p = product()
        q = self.queue(p, gh)
        row = status.runners_cell(p, source=ci_queue.GitHubSource(p, run=gh))
        self.assertEqual(row, '4 online · heavy 2/2 busy · light 0/2 busy, 1 offline')
        self.assertEqual(q.free(), {'heavy': 0, 'light': 2})
        # every class: the row's online minus busy is the queue's free, never another source
        for cls, n in q.free().items():
            busy, online = row.split(f'{cls} ')[1].split(' busy')[0].split('/')
            self.assertEqual(int(online) - int(busy), n)

    def test_the_runners_row_is_a_plain_total_with_no_classes(self):
        from asf import ci_pool
        runners = [ci_pool.Runner('a', True, [], busy=True), ci_pool.Runner('b', True, []),
                   ci_pool.Runner('c', False, [])]
        self.assertEqual(ci_queue.runners_text(runners, []),
                         '2 online, 1 busy, 1 idle, 1 offline')



def job(runner, start=None, end=None, conclusion='success', labels=()):
    """One job of a run's jobs API: ``start``/``end`` minutes past 10:00Z."""
    t = lambda m: None if m is None else f'2026-09-25T10:{m:02d}:00Z'
    return {'runner_name': runner, 'labels': list(labels), 'conclusion': conclusion,
            'started_at': t(start), 'completed_at': t(end)}


def staged_run(stage2=3):
    """A run as the host reports it: every heavy-labelled job of the workflow listed, most of
    them skipped or conditional (no runner), and two heavy stages run one after the other —
    2 jobs 0..5, then ``stage2`` jobs 5..10 — beside a light job overlapping each stage."""
    skipped = [job(None, conclusion='skipped', labels=['self-hosted', 'heavy'])
               for _ in range(6)]
    skipped.append(job('h1', 0, 0, conclusion='skipped'))       # skipped on a runner: still none
    heavy = [job('h1', 0, 5), job('h2', 0, 5)]
    heavy += [job(('h1', 'h2', 'h3')[i % 3], 5, 10) for i in range(stage2)]
    light = [job('l1', 1, 4), job('l1', 6, 9)]
    return skipped + heavy + light


class TestPeakConcurrency(Base):
    def pool(self):
        from asf import ci_pool
        return ci_pool.load_pool(env.Product('p', {'ci': {'pool': pool_data()}}))

    def test_skipped_jobs_never_count_and_sequential_stages_do_not_add_up(self):
        # 12 heavy jobs listed (6 skipped with no runner, 1 skipped on one, 5 run), light 2:
        # counting every job said heavy 12; at most 3 heavy and 1 light ever ran at once
        pool = self.pool()
        by_name = {e.runner: e for e in pool}
        from asf import ci_pool
        roles = set(ci_pool.roles(pool))
        self.assertEqual(ci_queue.peak_concurrent(staged_run(), by_name, roles),
                         {'heavy': 3, 'light': 1})
        self.assertEqual(ci_queue.needs_from_history([staged_run()] * 4, pool),
                         {'heavy': 3, 'light': 1})

    def test_the_p90_over_the_runs_capped_at_the_class(self):
        pool = self.pool()
        # three of ten runs peak at 3 heavy, seven at 2: the median said 2, p90 is 3
        runs = [staged_run(stage2=2)] * 7 + [staged_run(stage2=3)] * 3
        self.assertEqual(ci_queue.needs_from_history(runs, pool)['heavy'], 3)
        # one run in ten at 3 sits above p90: it never lifts it
        runs = [staged_run(stage2=2)] * 9 + [staged_run(stage2=3)]
        self.assertEqual(ci_queue.needs_from_history(runs, pool)['heavy'], 2)
        # two runs: 2 and 3 → 3
        runs = [staged_run(stage2=2), staged_run(stage2=3)]
        self.assertEqual(ci_queue.needs_from_history(runs, pool)['heavy'], 3)
        # a stage wider than the class (3 heavy runners) is capped at the class
        self.assertEqual(ci_queue.needs_from_history([staged_run(stage2=8)], pool)['heavy'], 3)

    def test_a_cached_figure_from_the_old_measure_is_read_again(self):
        p = product()
        data = ci_queue.load('p')
        data['expect']['ci.yml'] = {'at': ci_queue._iso(self.t0), 'needs': {'heavy': 12}}
        ci_queue.save('p', data)
        gh = FakeGh(history=[staged_run()])
        self.assertEqual(self.queue(p, gh).needs('ci.yml'), {'heavy': 3, 'light': 1})
        jq = next(a for c in gh.calls if any('/jobs' in x for x in c) for a in c
                  if a.startswith('.jobs'))
        for field in ('runner_name', 'conclusion', 'created_at', 'started_at', 'completed_at',
                      'run_attempt'):
            self.assertIn(field, jq)

    def test_the_estimate_ignores_cancelled_runs_and_superseded_attempts(self):
        """A rerun's first attempt ran 3 heavy at once, its second 1: only the second counts, and
        never the two overlapped. A cancelled run is not measured at all."""
        p = product()
        rerun = ([dict(job('h1', 0, 10), run_attempt=1), dict(job('h2', 0, 10), run_attempt=1),
                  dict(job('h3', 0, 10), run_attempt=1)]
                 + [dict(job('h1', 5, 8), run_attempt=2)])
        one = [dict(job('h1', 0, 5), run_attempt=1)]
        wide = [job(h, 0, 5) for h in ('h1', 'h2', 'h3')]
        gh = FakeGh(history=[rerun, one, wide], listed=[
            {'databaseId': 3, 'conclusion': 'cancelled', 'attempt': 1},   # 3 heavy: dropped
            {'databaseId': 1, 'conclusion': 'failure', 'attempt': 2},
            {'databaseId': 2, 'conclusion': 'success', 'attempt': 1}])
        q = self.queue(p, gh)
        self.assertEqual(q.needs('ci.yml'), {'heavy': 1})
        ci_queue.save('p', q.data)
        paths = [a for c in gh.calls for a in c if '/jobs' in a]
        self.assertEqual(len(paths), 2)
        self.assertIn('runs/1/attempts/2/jobs', paths[0])
        self.assertTrue(all('runs/3/' not in a for a in paths))
        # the same run ids after the TTL: the cached figure, no jobs read again
        gh2 = FakeGh(history=[rerun, one, wide], listed=gh.listed)
        q = self.queue(p, gh2, minutes=ci_queue.EXPECT_TTL_S // 60 + 1)
        self.assertEqual(q.needs('ci.yml'), {'heavy': 1})
        self.assertFalse([c for c in gh2.calls if any('/jobs' in a for a in c)])

    def test_repeated_calls_in_a_pass_agree(self):
        p = product()
        q = self.queue(p, FakeGh())
        first = q.needs('ci.yml')
        q.source._run = FakeGh(history=[[job('h1')]] * 3)    # the host changed mid-pass
        q.data['expect'].clear()
        self.assertEqual(q.needs('ci.yml'), first)


def fit_pool():
    return [{'runner': 'a1', 'provider': 'alpha', 'role': 'heavy'},
            {'runner': 'a2', 'provider': 'alpha', 'role': 'heavy'},
            {'runner': 'b1', 'provider': 'beta', 'role': 'heavy'},
            {'runner': 'b2', 'provider': 'beta', 'role': 'heavy'},
            {'runner': 'l1', 'provider': 'alpha', 'role': 'light', 'slots': 2}]


FIT_LABELS = {'a1': ['self-hosted', 'heavy', 'alpha-heavy'],
              'a2': ['self-hosted', 'heavy', 'alpha-heavy'],
              'b1': ['self-hosted', 'heavy'], 'b2': ['self-hosted', 'heavy'],
              'l1': ['self-hosted', 'light']}
ASKS = ['self-hosted', 'alpha-heavy']


def fit_history(runs=3):
    """Each run: two heavy jobs that ask for the provider-scoped label, one light job."""
    return [[job('a1', 0, 5, labels=ASKS), job('a2', 0, 5, labels=ASKS),
             job('l1', 0, 5, labels=['self-hosted', 'light'])] for _ in range(runs)]


class TestFitSupply(Base):
    def fit(self, **kw):
        p = product(pool=fit_pool())
        gh = FakeGh(pool=fit_pool(), labels=FIT_LABELS, history=fit_history(), **kw)
        return p, gh, self.queue(p, gh)

    def test_only_labels_that_split_a_class_make_a_key(self):
        from asf import ci_pool
        p, gh, q = self.fit()
        q._read_host()
        self.assertEqual(ci_queue.split_labels(q._runners, ci_pool.load_pool(p)),
                         {'heavy': frozenset({'alpha-heavy'}), 'light': frozenset()})
        self.assertEqual(ci_queue.fit_key('heavy', {'alpha-heavy'}), 'heavy[alpha-heavy]')
        self.assertEqual(ci_queue.fit_asks('heavy[alpha-heavy]'),
                         ('heavy', frozenset({'alpha-heavy'})))

    def test_a_free_runner_lacking_the_label_is_not_free_for_that_key(self):
        _p, _gh, q = self.fit(busy={'a1', 'a2'})
        free = q.free(keys={'heavy', 'heavy[alpha-heavy]', 'light'})
        self.assertEqual(free['heavy'], 2)              # b1, b2 are idle heavies
        self.assertEqual(free['heavy[alpha-heavy]'], 0)  # and neither carries the label
        _p, _gh, q = self.fit(busy={'b1', 'b2'})
        self.assertEqual(q.free(keys={'heavy[alpha-heavy]'})['heavy[alpha-heavy]'], 2)

    def test_a_class_that_splits_on_nothing_keeps_its_class_key(self):
        # the suite's own uniform fleet: every number as before (P5)
        q = self.queue(product(), FakeGh(busy={'h1'}))
        self.assertEqual(q.free(), {'heavy': 2, 'light': 2})
        self.assertEqual(q.free(keys={'heavy', 'light'}), {'heavy': 2, 'light': 2})

    def test_the_reservation_and_the_fit_key_compose(self):
        # a PR start sees only the reserved subset, counted per key within it
        p = product(pool=fit_pool(), reserve={'label': 'class-pr-heavy', 'of': 'heavy',
                                              'keep_free': 1})
        labels = {k: v + (['class-pr-heavy'] if k in ('a1', 'b1', 'b2') else [])
                  for k, v in FIT_LABELS.items()}
        q = self.queue(p, FakeGh(pool=fit_pool(), labels=labels, history=fit_history()))
        self.assertEqual(q.free('pr', keys={'heavy', 'heavy[alpha-heavy]'}),
                         {'heavy': 3, 'heavy[alpha-heavy]': 1})

    def test_load_by_fit_agrees_with_load_by_class_on_a_bare_key(self):
        from asf import ci_pool
        for p, gh in ((product(pool=fit_pool()),
                       FakeGh(pool=fit_pool(), labels=FIT_LABELS, history=fit_history())),
                      (product(), FakeGh())):
            q = self.queue(p, gh)
            q._read_host()
            pool = ci_pool.load_pool(p)
            by_class = ci_queue.load_by_class(q._runners, pool)
            self.assertEqual(ci_queue.load_by_fit(q._runners, pool, list(by_class)), by_class)


class TestFitDemand(Base):
    def fit(self, **kw):
        p = product(pool=fit_pool())
        gh = FakeGh(pool=fit_pool(), labels=FIT_LABELS, history=fit_history(), **kw)
        return p, gh, self.queue(p, gh)

    def test_the_measure_counts_each_key_over_the_jobs_that_need_it(self):
        _p, _gh, q = self.fit()
        # the bare class key is the class's peak as before; the strict key is the subset
        self.assertEqual(q.needs('ci.yml'),
                         {'heavy': 2, 'heavy[alpha-heavy]': 2, 'light': 1})
        self.assertEqual(q.data['expect']['ci.yml']['v'], ci_queue.EXPECT_VERSION)

    def test_a_uniform_fleet_measures_exactly_as_before(self):
        q = self.queue(product(), FakeGh())
        self.assertEqual(q.needs('ci.yml'), {'heavy': 3, 'light': 1})

    def test_the_run_is_held_while_only_the_wrong_heavies_are_free(self):
        _p, _gh, q = self.fit(busy={'a1', 'a2'})
        d = self.admit(q, 'pr:b', 'T-0500', branch='b')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: T-0500 waits — heavy[alpha-heavy] 0 free, '
                                      'needs 2 (Task unranked, 1st in line)'])
        self.assertEqual(q.data['entries']['pr:b']['free'], {'heavy[alpha-heavy]': 0})

    def test_the_same_run_starts_when_the_runners_it_needs_are_free(self):
        _p, _gh, q = self.fit(busy={'b1', 'b2'})
        self.assertTrue(self.admit(q, 'pr:b', 'T-0500', branch='b').admitted)
        self.assertEqual(self.lines, [])

    def test_an_override_on_the_class_replaces_the_class(self):
        p = product(pool=fit_pool(), queue={'estimate': {'heavy': 4}})
        q = self.queue(p, FakeGh(pool=fit_pool(), labels=FIT_LABELS, history=fit_history()))
        self.assertEqual(q.needs('ci.yml'), {'heavy': 4, 'light': 1})


class TestStartedOnce(Base):
    def fit(self, minutes=0, **kw):
        p = product(pool=fit_pool())
        gh = FakeGh(pool=fit_pool(), labels=FIT_LABELS, history=fit_history(), **kw)
        return p, gh, self.queue(p, gh, minutes=minutes)

    def start(self):
        _p, _gh, q = self.fit()
        self.assertTrue(self.admit(q, 'pr:b', 'T-0500', branch='b').admitted)
        rec, = q.data['started']
        self.assertEqual((rec['key'], rec['branch'], rec['workflow']), ('pr:b', 'b', 'ci.yml'))
        self.assertEqual(rec['needs'], {'heavy': 2, 'heavy[alpha-heavy]': 2, 'light': 1})

    def test_a_started_run_whose_job_is_on_a_busy_runner_is_counted_once(self):
        self.start()
        # 2 min later — inside PICKUP_S — its two heavy jobs hold a1 and a2
        _p, _gh, q = self.fit(minutes=2, busy={'a1', 'a2'},
                              running=[(9, 'b', 'sha1', ['a1', 'a2'])])
        free = q.free(keys={'heavy', 'heavy[alpha-heavy]', 'light'})
        # today: heavy 0 — the two busy runners were subtracted twice (P8)
        self.assertEqual(free, {'heavy': 2, 'heavy[alpha-heavy]': 0, 'light': 1})
        rec, = q.data['started']
        self.assertEqual(rec['placed'], {'heavy': 2, 'heavy[alpha-heavy]': 2})

    def test_a_record_with_nothing_left_unplaced_is_dropped(self):
        self.start()
        _p, _gh, q = self.fit(minutes=2, busy={'a1', 'a2', 'l1'},
                              running=[(9, 'b', 'sha1', ['a1', 'a2', 'l1'])])
        # every job of the run holds a runner: the claim is the busy count, and nothing else
        self.assertEqual(q.free(keys={'heavy', 'light'}), {'heavy': 2, 'light': 0})
        self.assertEqual(q.data['started'], [])
        # and the drop is written by the pass, not only held in memory
        self.admit(q, 'pr:c', 'T-0341', branch='c')     # held: no alpha heavy is free
        self.assertEqual(ci_queue.load('p')['started'], [])

    def test_another_branchs_run_nets_nothing(self):
        self.start()
        _p, _gh, q = self.fit(minutes=2, busy={'a1', 'a2'},
                              running=[(9, 'other', 'sha9', ['a1', 'a2'])])
        self.assertEqual(q.free(keys={'heavy'})['heavy'], 0)   # still claimed: not its run

    def test_an_unreadable_busy_map_counts_as_today(self):
        self.start()
        with mock.patch.object(ci_queue.GitHubSource, 'busy_runners', return_value=None):
            _p, _gh, q = self.fit(minutes=2, busy={'a1', 'a2'},
                                  running=[(9, 'b', 'sha1', ['a1', 'a2'])])
            self.assertEqual(q.free(keys={'heavy'})['heavy'], 0)   # over-holds, never over-starts
            self.assertEqual(q.data['started'][0]['needs']['heavy'], 2)

    def test_an_empty_ledger_asks_the_host_for_nothing(self):
        _p, gh, q = self.fit()
        self.assertEqual(q.data['started'], [])
        q.free(keys={'heavy'})
        self.assertEqual([c for c in gh.calls if 'status=in_progress' in ' '.join(c)], [])

    def test_a_record_is_still_forgotten_at_the_pickup_window(self):
        self.start()
        _p, _gh, q = self.fit(minutes=4)                        # PICKUP_S = 180 s
        self.assertEqual(q.data['started'], [])


class TestAdmission(Base):
    def test_holds_until_the_class_has_the_runners_the_run_needs(self):
        p = product()
        d = self.admit(self.queue(p, FakeGh(busy={'h1', 'h2'})), 'pr:task/T-0500', 'T-0500')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: T-0500 waits — heavy 1 free, needs 3 '
                                      '(Task unranked, 1st in line)'])
        self.assertIn('pr:task/T-0500', ci_queue.load('p')['entries'])
        d = self.admit(self.queue(p, FakeGh(), minutes=5), 'pr:task/T-0500', 'T-0500')
        self.assertTrue(d.admitted)
        data = ci_queue.load('p')
        self.assertEqual(data['entries'], {})
        self.assertEqual([s['key'] for s in data['started']], ['pr:task/T-0500'])

    def test_a_run_just_admitted_holds_its_runners_before_they_show_busy(self):
        p = product()
        q = self.queue(p, FakeGh())
        self.assertTrue(self.admit(q, 'pr:a', 'T-0500').admitted)
        self.assertFalse(self.admit(q, 'pr:b', 'B-0008').admitted)
        self.assertIn('heavy 0 free, needs 3', self.lines[-1])
        # a later pass still counts it for the pickup window, then no more
        self.assertFalse(self.admit(self.queue(p, FakeGh(), minutes=1), 'pr:b', 'B-0008').admitted)
        self.assertTrue(self.admit(self.queue(p, FakeGh(), minutes=10), 'pr:b', 'B-0008').admitted)

    def test_unreadable_history_never_blocks(self):
        p = product()
        d = self.admit(self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'}, history=[])), 'pr:a', 'T-0500')
        self.assertTrue(d.admitted)


class TestPriority(Base):
    def test_s1_then_trunk_then_s2_then_the_records_items_then_the_rest(self):
        self.assertEqual(ci_queue.priority('B-0007', ITEMS), (0, 'S1'))
        self.assertEqual(ci_queue.priority('B-0007', ITEMS, kind='trunk'), (0, 'S1'))
        self.assertEqual(ci_queue.priority('T-0500', ITEMS, branch='hotfix/x'), (0, 'hotfix'))
        self.assertEqual(ci_queue.priority('T-0500', ITEMS, kind='trunk'), (1, 'trunk'))
        self.assertEqual(ci_queue.priority('B-0008', ITEMS), (2, 'S2'))
        self.assertEqual(ci_queue.priority('T-0341', ITEMS), (3, 'Task F-0001 rank 1'))
        self.assertEqual(ci_queue.priority('T-0500', ITEMS), (3, 'Task unranked'))
        self.assertEqual(ci_queue.priority('worker/plan-measure-1', ITEMS), (4, 'other'))
        self.assertEqual(ci_queue.priority(None, ITEMS), (4, 'other'))
        gone = dict(ITEMS, **{'T-0341': dict(ITEMS['T-0341'], removed=True)})
        self.assertEqual(ci_queue.priority('T-0341', gone), (4, 'other'))

    def test_the_line_serves_priority_first_and_an_entry_ahead_keeps_its_runners(self):
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        self.admit(self.queue(p, busy), 'pr:new', 'T-0500')              # Task, oldest
        self.admit(self.queue(p, busy, minutes=1), 'pr:feat', 'T-0341')  # Task under F-0001
        self.admit(self.queue(p, busy, minutes=4), 'pr:old', 'B-0008')  # S2, newest
        entries = ci_queue.load('p')['entries']
        self.assertEqual(ci_queue.line_order(entries), ['pr:old', 'pr:feat', 'pr:new'])
        self.assertEqual(self.lines[-1], 'ci queue: B-0008 waits — heavy 0 free, needs 3 '
                                         '(S2, 1st in line)')
        # runners come free: the ranked Task asks first, but the S2 ahead of it is owed them
        q = self.queue(p, FakeGh(), minutes=5)
        self.assertFalse(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertIn('(Task F-0001 rank 1, 2nd in line)', self.lines[-1])
        self.assertTrue(self.admit(q, 'pr:old', 'B-0008').admitted)


class TestRecordRank(Base):
    """The line follows the record's own order (``asf next``): S2 before Feature work, the
    record's items by Epic rank, Feature rank, id — never age alone — and a branch with no record
    item last."""

    ITEMS = {
        'E-0001': {'id': 'E-0001', 'type': 'epic', 'rank': 2},
        'E-0002': {'id': 'E-0002', 'type': 'epic', 'rank': 1},
        # E-0002 is ranked first: its Feature leads though its own rank is higher
        'F-0010': {'id': 'F-0010', 'type': 'feature', 'parent': 'E-0001', 'rank': 1},
        'F-0020': {'id': 'F-0020', 'type': 'feature', 'parent': 'E-0002', 'rank': 5},
        'F-0030': {'id': 'F-0030', 'type': 'feature', 'parent': 'E-0001', 'rank': 2,
                   'state': 'Resolved'},
        'S-0010': {'id': 'S-0010', 'type': 'story', 'parent': 'F-0010'},
        'T-0011': {'id': 'T-0011', 'type': 'task', 'parent': 'S-0010'},
        'T-0021': {'id': 'T-0021', 'type': 'task', 'parent': 'F-0020'},
        'T-0022': {'id': 'T-0022', 'type': 'task', 'feature': 'F-0020'},
        'B-0031': {'id': 'B-0031', 'type': 'bug', 'severity': 'S2', 'parent': 'F-0010'},
    }

    def line(self, asks):
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        for m, (key, item) in enumerate(asks):
            ci_queue.admit(p, key, 'pr', item=item, items=self.ITEMS, branch=key[3:],
                           queue=self.queue(p, busy, minutes=m))
        return ci_queue.line_order(ci_queue.load('p')['entries'])

    def test_record_rank_follows_the_feeder_order(self):
        rank = lambda i: ci_queue.record_rank(i, self.ITEMS)  # noqa: E731
        self.assertEqual(rank('T-0021')[1], 'Task F-0020 rank 1')
        self.assertEqual(rank('T-0022')[1], 'Task F-0020 rank 1')    # via its feature: field
        self.assertEqual(rank('T-0011')[1], 'Task F-0010 rank 2')    # a resolved one never counts
        self.assertEqual(rank('F-0010')[1], 'Feature rank 2')
        self.assertLess(rank('T-0021')[0], rank('T-0022')[0])
        self.assertLess(rank('T-0022')[0], rank('T-0011')[0])
        # finish before you start: every Task's run before a Feature's own document PR
        self.assertLess(rank('T-0011')[0], rank('F-0020')[0])
        from asf.feeder import rows
        self.assertEqual(rank('T-0021')[0][1:4], list(rows.feature_order(
            self.ITEMS, self.ITEMS['F-0020'])))

    def test_a_record_ranked_task_goes_ahead_of_an_older_unmapped_branch(self):
        order = self.line([('pr:worker/plan-measure-1', 'worker/plan-measure-1'),
                           ('pr:cloud/tc-t0', 'cloud/tc-t0'),
                           ('pr:task/T-0011', 'T-0011'),
                           ('pr:task/T-0021', 'T-0021')])
        self.assertEqual(order, ['pr:task/T-0021', 'pr:task/T-0011',
                                 'pr:worker/plan-measure-1', 'pr:cloud/tc-t0'])
        # the later-asked, higher-ranked Task takes the head from the older unmapped branches
        self.assertEqual(self.lines[-1], 'ci queue: T-0021 waits — heavy 0 free, needs 3 '
                                         '(Task F-0020 rank 1, 1st in line)')

    def test_an_s2_goes_ahead_of_the_feature_tasks(self):
        order = self.line([('pr:task/T-0021', 'T-0021'), ('pr:task/T-0011', 'T-0011'),
                           ('pr:bug/B-0031', 'B-0031')])
        self.assertEqual(order, ['pr:bug/B-0031', 'pr:task/T-0021', 'pr:task/T-0011'])

    def test_unmapped_branches_go_last_oldest_first(self):
        order = self.line([('pr:worktree-m-a', 'worktree-m-a'), ('pr:feature/F-0010', 'F-0010'),
                           ('pr:cloud/x-t1', 'cloud/x-t1'), ('pr:task/T-0021', 'T-0021')])
        self.assertEqual(order, ['pr:task/T-0021', 'pr:feature/F-0010', 'pr:worktree-m-a',
                                 'pr:cloud/x-t1'])

    def test_an_entry_from_before_the_rank_sorts_after_the_ranked_ones(self):
        entries = {'pr:a': {'prio': ci_queue.RANKED, 'since': '2026-09-25T10:00:00Z'},
                   'pr:b': {'prio': ci_queue.RANKED, 'since': '2026-09-25T11:00:00Z',
                            'rank': ci_queue.record_rank('T-0011', self.ITEMS)[0]},
                   'pr:c': {'prio': ci_queue.OTHER, 'since': '2026-09-25T09:00:00Z'}}
        self.assertEqual(ci_queue.line_order(entries), ['pr:b', 'pr:a', 'pr:c'])


class TestPriorityExempt(Base):
    def test_trunk_and_s1_starts_go_with_no_runner_free(self):
        """The fit would fail (heavy 0 free, needs 3), yet a trunk run, an S1 and a hotfix PR go
        at once: the host queues their jobs, and they reserve nothing up front."""
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3', 'l1'})
        self.assertFalse(self.admit(self.queue(p, busy), 'pr:feat', 'T-0341').admitted)
        q = self.queue(p, busy, minutes=1)
        self.assertTrue(self.admit(q, 'trunk:t', 'T-0500', kind='trunk').admitted)
        self.assertTrue(self.admit(q, 'pr:fix', 'B-0007').admitted)
        self.assertTrue(self.admit(q, 'pr:hotfix/x', 'T-0500', branch='hotfix/x').admitted)
        self.assertTrue(self.admit(q, 'deploy:prod', 'deploy prod', kind='deploy').admitted)
        self.assertEqual(list(ci_queue.load('p')['entries']), ['pr:feat'])
        # the ordinary PR still waits on the fit
        self.assertFalse(self.admit(q, 'pr:feat', 'T-0341').admitted)


class TestTrunkRunsNeverCancelled(Base):
    """2026-09-27: the queue's superseded pass cancelled a product's main run 36295828657 (T-0382,
    its required jobs queued behind the pool) the moment 5c8251130 landed. With a merge every few
    minutes each landing killed the run before it, no main run ever finished, and the deploy never
    saw a green main. A trunk ``push`` run is never cancelled by the queue: the deploy needs a
    green trunk run, and only a finished one gives it."""
    RUNS = [
        {'databaseId': 1, 'status': 'in_progress', 'createdAt': '2026-09-25T19:00:00Z',
         'headSha': 'a' * 40},
        {'databaseId': 2, 'status': 'queued', 'createdAt': '2026-09-25T19:04:00Z',
         'headSha': 'b' * 40},
        {'databaseId': 3, 'status': 'queued', 'createdAt': '2026-09-25T19:10:00Z',
         'headSha': 'c' * 40},
        {'databaseId': 4, 'status': 'queued', 'createdAt': '2026-09-25T19:20:00Z',
         'headSha': 'd' * 40},
    ]

    def test_the_pass_leaves_every_older_trunk_run_alone(self):
        p = product()
        gh = FakeGh(history=[[{'name': 'gate', 'status': 'queued', 'runner_name': ''}]] * 4)
        base = gh.__call__
        runs = [dict(r, event='push', headBranch='main', conclusion=None) for r in self.RUNS]

        def run(argv, **kw):
            if argv[:3] == ['gh', 'run', 'list'] and '--json' in argv:
                gh.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(runs), '')
            return base(argv, **kw)
        ci_queue.queue_pass(p, source=ci_queue.GitHubSource(p, run=run), out=self.lines.append)
        cancels = [c[3] for c in gh.calls if c[:3] == ['gh', 'run', 'cancel']]
        self.assertEqual(cancels, [])
        self.assertFalse([l for l in self.lines if 'superseded' in l])
        self.assertFalse(hasattr(ci_queue, 'cancel_superseded'))

class TestDuplicatePush(Base):
    """2026-09-26 17:35Z, a product: branch worktree-m-p4-t1 had ci.yml runs 36259590283 and
    36259590441 (push) and 36259592614 (pull_request) on one sha cd5a5e5c3, all on heavy runners
    while an S1 PR waited. The push runs are duplicates the PR run covers."""
    SHA = 'cd5a5e5c3' + '0' * 31

    def runs(self):
        return [
            {'databaseId': 36259590283, 'status': 'in_progress', 'conclusion': '',
             'event': 'push', 'headBranch': 'worktree-m-p4-t1', 'headSha': self.SHA,
             'createdAt': '2026-09-26T17:30:01Z'},
            {'databaseId': 36259590441, 'status': 'queued', 'conclusion': '',
             'event': 'push', 'headBranch': 'worktree-m-p4-t1', 'headSha': self.SHA,
             'createdAt': '2026-09-26T17:30:02Z'},
            {'databaseId': 36259592614, 'status': 'queued', 'conclusion': '',
             'event': 'pull_request', 'headBranch': 'worktree-m-p4-t1', 'headSha': self.SHA,
             'createdAt': '2026-09-26T17:30:10Z'},
            # a plan branch with no PR (D44): its push CI stays
            {'databaseId': 500, 'status': 'queued', 'conclusion': '', 'event': 'push',
             'headBranch': 'plan-x', 'headSha': 'b' * 40, 'createdAt': '2026-09-26T17:31:00Z'},
            # the trunk push on a sha a PR run also ran on: never touched
            {'databaseId': 600, 'status': 'queued', 'conclusion': '', 'event': 'push',
             'headBranch': 'main', 'headSha': 'c' * 40, 'createdAt': '2026-09-26T17:32:00Z'},
            {'databaseId': 601, 'status': 'completed', 'conclusion': 'success',
             'event': 'pull_request', 'headBranch': 'task/T-0500', 'headSha': 'c' * 40,
             'createdAt': '2026-09-26T17:00:00Z'},
            # a release train: exempt, whatever runs on its sha
            {'databaseId': 700, 'status': 'queued', 'conclusion': '', 'event': 'push',
             'headBranch': 'train/2026-09-26', 'headSha': 'd' * 40,
             'createdAt': '2026-09-26T17:33:00Z'},
            {'databaseId': 701, 'status': 'queued', 'conclusion': '', 'event': 'pull_request',
             'headBranch': 'train/2026-09-26', 'headSha': 'd' * 40,
             'createdAt': '2026-09-26T17:33:05Z'},
            # a push whose PR run failed: the PR run covers nothing, the push stays
            {'databaseId': 800, 'status': 'queued', 'conclusion': '', 'event': 'push',
             'headBranch': 'task/T-0341', 'headSha': 'e' * 40,
             'createdAt': '2026-09-26T17:34:00Z'},
            {'databaseId': 801, 'status': 'completed', 'conclusion': 'failure',
             'event': 'pull_request', 'headBranch': 'task/T-0341', 'headSha': 'e' * 40,
             'createdAt': '2026-09-26T17:20:00Z'},
            # a completed-green PR run covers a still-queued push on the same sha
            {'databaseId': 900, 'status': 'queued', 'conclusion': '', 'event': 'push',
             'headBranch': 'task/T-0600', 'headSha': 'f' * 40,
             'createdAt': '2026-09-26T17:35:00Z'},
            {'databaseId': 901, 'status': 'completed', 'conclusion': 'success',
             'event': 'pull_request', 'headBranch': 'task/T-0600', 'headSha': 'f' * 40,
             'createdAt': '2026-09-26T17:10:00Z'},
        ]

    def gh(self, runs):
        gh = FakeGh()
        base = gh.__call__

        def run(argv, **kw):
            if argv[:3] == ['gh', 'run', 'list'] and 'event' in argv[-1]:
                gh.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(runs), '')
            return base(argv, **kw)
        return gh, run

    def dedupe(self, p, run, **kw):
        return ci_queue.cancel_duplicate_pushes(p, source=ci_queue.GitHubSource(p, run=run),
                                               out=self.lines.append, **kw)

    def cancels(self, gh):
        return [c[3] for c in gh.calls if c[:3] == ['gh', 'run', 'cancel']]

    def test_the_1735z_shape_cancels_both_push_runs_and_keeps_the_pr_run(self):
        p = product()
        gh, run = self.gh(self.runs())
        self.assertEqual(self.dedupe(p, run), 3)
        # newest duplicate first; the PR run, the plan branch, trunk, the train and the push
        # whose PR run failed are never touched
        self.assertEqual(self.cancels(gh), ['900', '36259590441', '36259590283'])
        self.assertIn('ci queue: cancel duplicate push 36259590441 on worktree-m-p4-t1 — '
                      'PR run 36259592614 covers cd5a5e5c3', self.lines)
        self.assertEqual(len(self.lines), 3)
        # one list for the pass, never a call per run, never a re-run
        self.assertEqual(len([c for c in gh.calls if c[:3] == ['gh', 'run', 'list']]), 1)
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'rerun']])
        self.assertEqual(ci_queue.load('p')['relief'], [])

    def test_push_without_pr_run_trunk_and_train_are_kept(self):
        p = product()
        runs = [r for r in self.runs() if r['databaseId'] in (500, 600, 601, 700, 701)]
        gh, run = self.gh(runs)
        self.assertEqual(self.dedupe(p, run), 0)
        self.assertEqual(self.cancels(gh), [])
        self.assertEqual(self.lines, [])

    def test_exempt_globs_are_configurable(self):
        p = product(queue={'dedupe_exempt_branches': ['worktree-*']})
        gh, run = self.gh(self.runs())
        self.dedupe(p, run)
        cancelled = self.cancels(gh)
        self.assertNotIn('36259590283', cancelled)
        self.assertIn('700', cancelled)   # train/* is no longer exempt once the list is set

    def test_off_dry_run_and_unqueued_cancel_nothing(self):
        gh, run = self.gh(self.runs())
        self.assertEqual(self.dedupe(product(queue={'dedupe_push': 'off'}), run), 0)
        self.assertFalse(gh.calls)
        self.assertEqual(self.dedupe(product(queue={'mode': 'dry-run'}), run), 0)
        self.assertEqual(self.cancels(gh), [])
        self.assertTrue(self.lines and all('would cancel' in l for l in self.lines))
        self.assertEqual(ci_queue.cancel_duplicate_pushes(product(pool=False), source=NoGh()), 0)

    def test_shared_listing_marks_the_cancelled_run_so_relief_never_reruns_it(self):
        p = product()
        gh, run = self.gh(self.runs())
        listing = {}
        self.dedupe(p, run, listing=listing)
        self.assertEqual(ci_queue._list_runs(ci_queue.GitHubSource(p, run=run), p, 'ci.yml',
                                             listing)[1]['status'], 'completed')
        self.assertEqual(len([c for c in gh.calls if c[:3] == ['gh', 'run', 'list']]), 1)

    def test_config_problems_name_bad_dedupe_fields(self):
        probs = dict(ci_queue.config_problems({'queue': {'dedupe_push': 'maybe',
                                                         'dedupe_exempt_branches': 'train/*'}}))
        self.assertIn('ci.queue.dedupe_push', probs)
        self.assertIn('ci.queue.dedupe_exempt_branches', probs)
        self.assertEqual(ci_queue.config_problems({'queue': {'dedupe_push': 'on'}}), [])


class TestExplainCancels(Base):
    """2026-09-29, a product: PR/batch/trunk runs 36618622420 and 36619311590 ended ``cancelled``
    with no line in the queue log. Nobody cancelled them: their m5-soak job hit its 30 minute
    ``timeout-minutes``, which the host reports as a cancel. Every cancelled run gets a cause."""
    WHEN = '2026-09-25T11:30:00Z'

    def run_of(self, rid, branch='task/T-0341', event='pull_request', when=None, sha=None):
        return {'databaseId': rid, 'status': 'completed', 'conclusion': 'cancelled',
                'event': event, 'headBranch': branch, 'headSha': sha or ('a' * 40),
                'createdAt': when or self.WHEN}

    def explain(self, runs, cause=(), prs=(), **kw):
        gh = FakeGh()
        base = gh.__call__

        def run(argv, **k):
            if argv[:3] == ['gh', 'run', 'list']:
                gh.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(runs), '')
            if argv[:3] == ['gh', 'pr', 'list']:
                return subprocess.CompletedProcess(argv, 0, json.dumps(list(prs)), '')
            if argv[:2] == ['gh', 'api'] and any('/jobs' in a for a in argv):
                gh.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '7\tm5-soak\n' if cause else '', '')
            if argv[:2] == ['gh', 'api'] and any('/annotations' in a for a in argv):
                return subprocess.CompletedProcess(argv, 0, cause, '')
            return base(argv, **k)
        p = kw.pop('p', None) or product()
        n = ci_queue.explain_cancels(p, source=ci_queue.GitHubSource(p, run=run),
                                     out=self.lines.append, now=self.t0, **kw)
        return n, gh

    TIMEOUT = 'The job has exceeded the maximum execution time of 30m0s\nThe operation was canceled.'

    def test_a_job_timeout_is_named_and_not_rerun(self):
        n, gh = self.explain([self.run_of(36618622420, 'worktree-m-batch', 'workflow_dispatch')],
                             cause=self.TIMEOUT)
        self.assertEqual(n, 1)
        self.assertEqual(self.lines, [
            'ci queue: run 36618622420 on worktree-m-batch at aaaaaaaaa cancelled by the host — '
            'job m5-soak passed its 30 min timeout; a timeout is a verdict, not re-queued (a '
            're-run would time out again), replaced by nothing'])
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'rerun']])
        self.assertEqual(ci_queue.load_claims(env.state_dir('p'))['36618622420']['cause'],
                         'job-timeout')
        self.lines.clear()
        self.assertEqual(self.explain([self.run_of(36618622420)], cause=self.TIMEOUT)[0], 0)
        self.assertEqual(self.lines, [])            # said once

    def test_a_supersede_names_the_run_that_replaced_it(self):
        later = dict(self.run_of(2), status='in_progress', conclusion='',
                     createdAt='2026-09-25T11:40:00Z', headSha='b' * 40)
        n, gh = self.explain([self.run_of(1), later])
        self.assertEqual(n, 1)
        self.assertIn('ci queue: run 1 on task/T-0341 at aaaaaaaaa cancelled by the host — a newer run '
                      'superseded it (concurrency group), replaced by run 2 at bbbbbbbbb',
                      self.lines)
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'rerun']])

    def test_an_orphaned_open_pr_head_is_rerun(self):
        pr = {'number': 9, 'headRefName': 'task/T-0341', 'state': 'OPEN', 'isDraft': False,
              'headRefOid': 'a' * 40}
        n, gh = self.explain([self.run_of(1)], prs=[pr])
        self.assertIn(['gh', 'run', 'rerun', '1', '-R', 'o/r'], gh.calls)
        self.assertIn('ci queue: run 1 on task/T-0341 at aaaaaaaaa cancelled by the host with no '
                      'later run — re-run of run 1 requested', self.lines)

    def test_an_orphan_that_is_no_open_head_or_is_old_is_told_and_left(self):
        n, gh = self.explain([self.run_of(1), self.run_of(3, 'task/T-0500', when='2026-09-25T08:00:00Z')])
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'rerun']])
        self.assertEqual(len([l for l in self.lines if 'replaced by nothing and left' in l]), 2)

    def test_factory_cancels_are_claimed_and_never_guessed_at(self):
        ci_queue.claim_cancel(env.state_dir('p'), 1, 'relief', self.t0)
        data = ci_queue.load('p')
        data['stalls'].append({'run': 2, 'branch': 'b', 'sha': 's', 'at': '2026-09-25T11:00:00Z'})
        ci_queue.save('p', data)
        n, gh = self.explain([self.run_of(1), self.run_of(2)])
        self.assertEqual((n, self.lines), (0, []))
        self.assertFalse([c for c in gh.calls if '/jobs' in ' '.join(c)])

    def test_dry_run_and_off_change_nothing(self):
        pr = {'number': 9, 'headRefName': 'task/T-0341', 'state': 'OPEN', 'isDraft': False,
              'headRefOid': 'a' * 40}
        n, gh = self.explain([self.run_of(1)], prs=[pr], dry_run=True)
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'rerun']])
        self.assertEqual(ci_queue.load_claims(env.state_dir('p')), {})
        self.assertEqual(ci_queue.explain_cancels(product(pool=False), source=NoGh()), 0)

    def test_the_duplicate_cancel_writes_its_claim(self):
        t = TestDuplicatePush()
        t.lines = self.lines
        p = product()
        gh, run = t.gh(t.runs())
        t.dedupe(p, run)
        claims = ci_queue.load_claims(env.state_dir('p'))
        self.assertEqual(claims['36259590283']['cause'], 'duplicate-push')
        self.assertEqual(claims['900']['by'], 901)


class ReliefBase(Base):
    """A trunk run queued past ``trunk_wait_min`` behind PR and batch runs at a FIFO host."""
    WF = {'pr': 'pr.yml', 'trunk': 'ci.yml', 'batch': 'batch.yml'}

    def product(self, **q):
        return product(queue=dict({'workflows': self.WF}, **q))

    @staticmethod
    def at(t):
        return t.strftime('%Y-%m-%dT%H:%M:%SZ')

    def runs(self, trunk_status='queued', trunk_created=None):
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        trunk = {'databaseId': 900, 'status': trunk_status, 'event': 'push', 'headBranch': 'main',
                 'headSha': 'f' * 40, 'createdAt': trunk_created or t(-25),
                 'startedAt': t(-1) if trunk_status != 'queued' else None}
        pr = lambda i, s, b, m: {'databaseId': i, 'status': s, 'event': 'pull_request',  # noqa
                                  'headBranch': b, 'headSha': 'a' * 40, 'createdAt': t(m)}
        return {
            'ci.yml': [trunk],
            'pr.yml': [pr(101, 'queued', 'worker/plan-measure', -60),  # no item: first
                       pr(102, 'queued', 'task/T-0341', -50),      # Feature: second
                       pr(103, 'queued', 'bug/B-0007', -40),       # S1: never
                       pr(104, 'queued', 'hotfix/db', -35),        # hotfix: never
                       pr(105, 'in_progress', 'task/T-0500', -90),  # started: never
                       pr(106, 'queued', 'task/T-0500', -5)],      # behind the trunk run
            'batch.yml': [{'databaseId': 201, 'status': 'queued', 'event': 'workflow_dispatch',
                           'headBranch': 'main', 'headSha': 'f' * 40, 'createdAt': t(-45)}],
        }

    def gh(self, runs, busy=('h1', 'h2', 'h3'), jobs=None, files=None):
        gh = FakeGh(busy=busy, files=files)
        base = gh.__call__

        def run(argv, **kw):
            live = next((a for a in argv if a.endswith('&filter=latest')), None)
            if argv[:2] == ['gh', 'api'] and live:
                gh.calls.append(argv)
                rid = int(live.split('/runs/')[1].split('/')[0])
                if rid not in (jobs or {}):
                    return subprocess.CompletedProcess(argv, 1, '', 'not found')
                return subprocess.CompletedProcess(
                    argv, 0, '\n'.join(json.dumps(j) for j in jobs[rid]), '')
            if argv[:3] == ['gh', 'run', 'list'] and 'event' in argv[-1]:
                gh.calls.append(argv)
                wf = argv[argv.index('--workflow') + 1]
                return subprocess.CompletedProcess(argv, 0, json.dumps(runs.get(wf, [])), '')
            return base(argv, **kw)
        return gh, run

    def seed(self, now):
        # measured jobs per class: the trunk run needs 3 heavy; a PR run 1, a batch run 2
        stamp = self.at(now)
        ci_queue.save('p', {'expect': {
            'ci.yml': {'at': stamp, 'needs': {'heavy': 3}, 'v': ci_queue.EXPECT_VERSION},
            'pr.yml': {'at': stamp, 'needs': {'heavy': 1}, 'v': ci_queue.EXPECT_VERSION},
            'batch.yml': {'at': stamp, 'needs': {'heavy': 2}, 'v': ci_queue.EXPECT_VERSION}}})

    def relieve(self, p, run, minutes=0, now=None, **kw):
        os.makedirs(env.state_dir('p'), exist_ok=True)
        return ci_queue.relieve_trunk(
            p, items=ITEMS, source=ci_queue.GitHubSource(p, run=run), out=self.lines.append,
            now=now if now is not None else self.t0 + datetime.timedelta(minutes=minutes), **kw)

    def cancels(self, gh, verb='cancel'):
        return [c[3] for c in gh.calls if c[:3] == ['gh', 'run', verb]]


class TestTrunkRelief(ReliefBase):
    def test_waits_up_to_the_threshold_then_cancels_lowest_priority_queued_runs_first(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(trunk_created=self.at(self.t0 - datetime.timedelta(minutes=19))))
        self.assertEqual(self.relieve(p, run), (0, 0))
        self.assertEqual(self.cancels(gh), [])
        gh, run = self.gh(self.runs())
        self.assertEqual(self.relieve(p, run), (3, 0))
        # PR S2, then PR Feature, then batch (1 + 1 + 2 >= 3); never S1, hotfix, started or behind
        self.assertEqual(self.cancels(gh), ['101', '102', '201'])
        self.assertEqual(self.lines[0], 'ci queue: cancelled queued pr run 101 (worker/plan-measure, other) — '
                                        'main run 900 at fffffffff has waited 25m for runners '
                                    '[branch worker/plan-measure, head aaaaaaaaa; replaced by '
                                    'its own re-run once main run 900 starts]')
        self.assertEqual(len(self.lines), 3)
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [101, 102, 201])
        # the next tick with the trunk still queued cancels nothing more: the runs are gone
        gh, run = self.gh({k: [r for r in v if r['databaseId'] not in (101, 102, 201)]
                           for k, v in self.runs().items()})
        self.assertEqual(self.relieve(p, run, minutes=1), (0, 0))

    def test_a_run_whose_pr_changes_ci_config_is_never_cancelled(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs()
        for r in runs['pr.yml']:
            r['headSha'] = f"sha{r['databaseId']}"
        gh, run = self.gh(runs, files={101: ['.github/workflows/checks.yml']})
        # 101 (lowest priority, would go first) is exempt: 102 then batch 201 go instead
        self.assertEqual(self.relieve(p, run), (2, 0))
        self.assertEqual(self.cancels(gh), ['102', '201'])
        self.assertEqual(self.lines[0], 'relief: exempt worker/plan-measure — changes CI config')
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [102, 201])

    def test_a_run_whose_pr_changes_actionlint_config_is_never_cancelled(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs()
        for r in runs['pr.yml']:
            r['headSha'] = f"sha{r['databaseId']}"
        gh, run = self.gh(runs, files={101: ['.github/actionlint.yaml']})
        self.assertEqual(self.relieve(p, run), (2, 0))
        self.assertEqual(self.cancels(gh), ['102', '201'])
        self.assertEqual(self.lines[0], 'relief: exempt worker/plan-measure — changes CI config')

    def test_relief_exempt_paths_config_is_honoured(self):
        p = self.product(relief_exempt_paths=['ops/runners/**'])
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs()
        for r in runs['pr.yml']:
            r['headSha'] = f"sha{r['databaseId']}"
        gh, run = self.gh(runs, files={101: ['ops/runners/pool.yaml']})
        self.assertEqual(self.relieve(p, run), (2, 0))
        self.assertEqual(self.cancels(gh), ['102', '201'])
        self.assertEqual(self.lines[0], 'relief: exempt worker/plan-measure — changes CI config')
        # a fresh pass (the earlier cancels forgotten): the same run's PR touching an ordinary
        # path is not exempt
        data = ci_queue.load('p')
        data['relief'] = []
        ci_queue.save('p', data)
        self.lines.clear()
        runs2 = self.runs()
        for r in runs2['pr.yml']:
            r['headSha'] = f"sha{r['databaseId']}"
        gh2, run2 = self.gh(runs2, files={101: ['asf/foo.py']})
        self.assertEqual(self.relieve(self.product(relief_exempt_paths=['ops/runners/**']), run2,
                                      minutes=1), (3, 0))
        self.assertEqual(self.cancels(gh2), ['101', '102', '201'])

    def test_a_run_touching_light_paths_only_is_not_exempt_and_the_files_are_read_once_per_head_sha(
            self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs()
        # 101 and 102 share a commit (same PR pushed twice, say): the lookup happens once
        for r in runs['pr.yml']:
            if r['databaseId'] in (101, 102):
                r['headSha'] = 'shared-sha'
        gh, run = self.gh(runs, files={101: ['docs/readme.md']})
        self.assertEqual(self.relieve(p, run), (3, 0))
        self.assertEqual(self.cancels(gh), ['101', '102', '201'])
        # one changed-files lookup for the shared sha (101 or 102), not two — batch run 201's
        # own lookup (its own sha) is separate
        lookups = [c for c in gh.calls
                  if any(f'actions/runs/{i}' in a and '/jobs' not in a for i in (101, 102)
                        for a in c)]
        self.assertEqual(len(lookups), 1)

    def test_stops_once_free_plus_freed_covers_the_trunk_run(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), busy=('h1', 'h2'))   # 1 heavy free: two PR runs suffice
        self.assertEqual(self.relieve(p, run), (2, 0))
        self.assertEqual(self.cancels(gh), ['101', '102'])
        gh, run = self.gh(self.runs(), busy=())              # runners free: nothing to do
        self.assertEqual(self.relieve(p, run, minutes=1), (0, 0))
        self.assertEqual(self.cancels(gh), [])

    def test_no_relief_cancel_under_50_percent_utilisation(self):
        """1 of 3 heavy busy: the trunk run's 3 do not fit in the 2 free, but the class is not
        saturated — a cancel frees nothing the host could not hand it. No cancel."""
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), busy=('h1',))
        self.assertEqual(self.relieve(p, run), (0, 0))
        self.assertEqual(self.cancels(gh), [])
        self.assertIn('relief: none for main run 900 — heavy 1/3 busy (33 %), below 50 %: not '
                      'saturated', self.lines)

    def test_relief_cancels_only_once_the_class_is_saturated(self):
        p = self.product(relief_saturation=1.0)
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), busy=('h1', 'h2'))      # 67 %: under a 100 % floor
        self.assertEqual(self.relieve(p, run), (0, 0))
        gh, run = self.gh(self.runs(), busy=('h1', 'h2', 'h3'))  # saturated: lowest first
        self.assertEqual(self.relieve(p, run, minutes=1), (3, 0))
        self.assertEqual(self.cancels(gh), ['101', '102', '201'])
        self.assertEqual(ci_queue.config_problems({'queue': {'relief_saturation': 1.5}}),
                         [('ci.queue.relief_saturation', 'must be a share in (0, 1], not 1.5')])

    def test_cancelled_runs_are_rerun_through_the_queue_once_the_trunk_starts(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        self.lines.clear()
        # the trunk run starts: runners come free, the queue admits by priority and runners
        gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=())
        self.assertEqual(self.relieve(p, run, minutes=2), (0, 2))
        self.assertEqual(self.cancels(gh, 'rerun'), ['102', '101'])  # Feature, then S2
        self.assertIn('ci queue: re-ran pr run 102 (T-0341, Task F-0001 rank 1) — main run 900 at '
                      'fffffffff started after waiting 24m', self.lines)
        self.assertIn('ci queue: batch waits — heavy 1 free, needs 2 (batch, 1st in line)', self.lines)
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [201])
        gh, run = self.gh(self.runs(trunk_status='completed'), busy=())
        self.assertEqual(self.relieve(p, run, minutes=10), (0, 1))
        self.assertEqual(self.cancels(gh, 'rerun'), ['201'])
        self.assertEqual(ci_queue.load('p')['relief'], [])

    def workflows(self, run, blobs):
        """``run`` answering a run's ``(head sha, workflow path)`` and a path's blob per ref
        (``blobs``: ``{(ref, path): blob}``); every other call as ``run`` answers it."""
        heads = {101: 'a' * 40, 102: 'a' * 40, 201: 'f' * 40}
        paths = {101: '.github/workflows/pr.yml', 102: '.github/workflows/pr.yml',
                 201: '.github/workflows/batch.yml'}

        def wrapped(argv, **kw):
            api = next((a for a in argv if a.startswith('repos/o/r/')), '') \
                if argv[:2] == ['gh', 'api'] else ''
            tail = api[len('repos/o/r/'):]
            if tail.startswith('actions/runs/') and tail.split('/')[2:3] and \
                    '/' not in tail[len('actions/runs/'):] and 'head_sha' in ' '.join(argv):
                run(['gh', 'noop', *argv[1:]])          # logged for the lookup count
                rid = int(tail.split('/')[2])
                return subprocess.CompletedProcess(argv, 0, f'{heads[rid]}\t{paths[rid]}\n', '')
            if tail.startswith('contents/'):
                run(['gh', 'noop', *argv[1:]])
                path, ref = tail[len('contents/'):].split('?ref=')
                return subprocess.CompletedProcess(argv, 0, blobs.get((ref, path), 'same') + '\n',
                                                   '')
            return run(argv, **kw)
        return wrapped

    def test_a_run_created_on_a_changed_workflow_is_never_rerun(self):
        """``gh run rerun`` replays the run's own workflow definition: a PR run created before
        the trunk changed its workflow (new runs-on labels) would land on runners reserved for
        the trunk. It asks for its place in line like any other re-run (never simply dropped —
        2026-09-28, a product: dropped here, its held entry swept out next as "no relief
        record", the PR stranded ~6h) and, once admitted, a fresh run replaces the stale one
        (here, the branch's still-queued run itself) instead of ``gh run rerun`` on it; the
        batch run on an unchanged workflow is re-run as before, held for the next tick behind
        the two PR starts it now competes with for runners."""
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        self.lines.clear()
        gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=())
        blobs = {('a' * 40, '.github/workflows/pr.yml'): '1111111aaaa',
                 ('main', '.github/workflows/pr.yml'): '2222222bbbb'}
        self.assertEqual(self.relieve(p, self.workflows(run, blobs), minutes=2), (0, 2))
        # neither PR run is replayed with `gh run rerun`; the batch run, unchanged, is held for
        # capacity instead (the two PR starts took the runners it needs)
        self.assertEqual(self.cancels(gh, 'rerun'), [])
        self.assertIn('ci queue: skip stale rerun pr run 102 (T-0341, Task F-0001 rank 1) on '
                      'task/T-0341 — workflow changed since (1111111→2222222); run 102 is '
                      'queued already instead', [l.split(' — main')[0] for l in self.lines])
        self.assertIn('ci queue: skip stale rerun pr run 101 (worker/plan-measure, other) on '
                      'worker/plan-measure — workflow changed since (1111111→2222222); run 101 '
                      'is queued already instead', [l.split(' — main')[0] for l in self.lines])
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [201])
        # one run lookup per run; one blob per (ref, path): pr.yml at a*40 and main, batch.yml
        # at f*40 and main — the workflow-changed PR runs resolve as already queued, without
        # ever asking `_start_run` to look at their run or its workflow a second time
        looks = [c for c in gh.calls if c[:2] == ['gh', 'noop']]
        runs_looked = [a for c in looks for a in c if '/actions/runs/' in a]
        self.assertEqual(sorted(runs_looked), sorted(f'repos/o/r/actions/runs/{i}'
                                                     for i in (101, 102, 201)))
        self.assertEqual(len([c for c in looks if any('/contents/' in a for a in c)]), 4)

    def test_a_run_on_the_same_workflow_is_rerun_as_before(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        self.lines.clear()
        gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=())
        self.assertEqual(self.relieve(p, self.workflows(run, {}), minutes=2), (0, 2))
        self.assertEqual(self.cancels(gh, 'rerun'), ['102', '101'])
        self.assertFalse([l for l in self.lines if 'skip rerun' in l])

    def test_a_held_rerun_asks_at_its_priority_in_the_record_now(self):
        """A relief record kept across a change of the order (its stored ``prio`` from the old
        numbering) never carries the stale number into the line: the re-run asks at what its
        branch's item is in the record now, a branch with no item last."""
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        data = ci_queue.load('p')
        for rec in data['relief']:
            rec['prio'] = 3 if rec['id'] == 101 else 2      # the old numbering's values
            rec.pop('rank', None)
        ci_queue.save('p', data)
        gh, run = self.gh(self.runs(trunk_status='in_progress'))    # started, runners all busy
        self.assertEqual(self.relieve(p, run, minutes=2), (0, 0))
        entries = ci_queue.load('p')['entries']
        self.assertEqual(entries['rerun:worker/plan-measure']['prio'], ci_queue.OTHER)
        self.assertEqual(entries['rerun:task/T-0341']['prio'], ci_queue.RANKED)
        self.assertEqual(entries['rerun:task/T-0341']['label'], 'Task F-0001 rank 1')
        order = ci_queue.line_order(entries)
        self.assertEqual(order[0], 'rerun:task/T-0341')
        self.assertIn('rerun:worker/plan-measure', order[1:])

    def test_a_workflow_change_starts_a_fresh_run_instead_of_stranding_the_pr(self):
        """2026-09-28, a product: a PR run cancelled for relief sat held (every runner busy) —
        its ``rerun:<branch>`` entry created and kept. By the time runners freed, the trunk's
        workflow had changed since the cancelled run was created: the record was dropped without
        ever being admitted, its held entry left behind — swept out next tick as "no relief
        record" — and the PR was left with no run and no place in the queue, stranded ~6h until
        someone pushed by hand. The record now asks for its place in line like any other re-run;
        once admitted, a fresh run starts for the branch instead of the stale one, and the entry
        never outlives it."""
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        # tick 1: the trunk relief cancels 101, 102 and 201
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        # tick 2: the trunk has started, but every runner is still busy — the Feature's re-run
        # is held, and an entry is created and kept for it (as in the test above)
        gh, run = self.gh(self.runs(trunk_status='in_progress'))
        self.assertEqual(self.relieve(p, run, minutes=2), (0, 0))
        self.assertIn('rerun:task/T-0341', ci_queue.load('p')['entries'])
        # isolate the Feature's record and entry: the other two are the same story already
        # covered elsewhere, and would only make this tick's outcome harder to read
        data = ci_queue.load('p')
        data['relief'] = [r for r in data['relief'] if r['item'] == 'T-0341']
        data['entries'] = {k: e for k, e in data['entries'].items() if k == 'rerun:task/T-0341'}
        ci_queue.save('p', data)
        # tick 3: runners are free, the trunk's workflow has changed since, and the host no
        # longer lists the cancelled run at all (it dropped off — nothing left to replay)
        runs3 = {'ci.yml': [{'databaseId': 900, 'status': 'in_progress', 'event': 'push',
                             'headBranch': 'main', 'headSha': 'f' * 40,
                             'createdAt': self.at(self.t0 - datetime.timedelta(minutes=25)),
                             'startedAt': self.at(self.t0 - datetime.timedelta(minutes=1))}],
                 'pr.yml': [], 'batch.yml': []}
        gh3, run3 = self.gh(runs3, busy=())
        blobs = {('a' * 40, '.github/workflows/pr.yml'): '1111111aaaa',
                 ('main', '.github/workflows/pr.yml'): '2222222bbbb'}
        self.assertEqual(self.relieve(p, self.workflows(run3, blobs), minutes=3), (0, 1))
        self.assertEqual([c for c in gh3.calls if c[:2] == ['gh', 'workflow']],
                         [['gh', 'workflow', 'run', 'pr.yml', '--ref', 'task/T-0341', '-R',
                           'o/r']])
        self.assertTrue(any('skip stale rerun' in l
                            and 'dispatched a fresh pr.yml run on task/T-0341 instead' in l
                            for l in self.lines), self.lines)
        data = ci_queue.load('p')
        self.assertEqual(data['relief'], [])
        self.assertNotIn('rerun:task/T-0341', data['entries'])

    def test_dry_run_says_what_it_would_cancel_and_writes_nothing(self):
        p = self.product(mode='dry-run')
        gh, run = self.gh(self.runs())
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        before = ci_queue.load('p')
        self.assertEqual(self.relieve(p, run), (0, 0))
        self.assertEqual(self.cancels(gh), [])
        self.assertEqual(len(self.lines), 3)
        self.assertTrue(all(l.startswith('ci queue: would cancel queued ') for l in self.lines))
        self.assertEqual(ci_queue.load('p'), before)
        # a dry-run pass of a live queue writes nothing either
        p = self.product()
        self.lines.clear()
        self.assertEqual(self.relieve(p, run, dry_run=True), (0, 0))
        self.assertEqual(self.cancels(gh), [])
        self.assertEqual(ci_queue.load('p'), before)

    def test_the_wait_is_utc_whatever_the_local_zone(self):
        old = os.environ.get('TZ')
        os.environ['TZ'] = 'Pacific/Auckland'
        time.tzset()
        try:
            p = self.product()
            real = datetime.datetime.now(datetime.timezone.utc)
            self.seed(real)
            ago = lambda m: self.at(real - datetime.timedelta(minutes=m))  # noqa: E731
            gh, run = self.gh(self.runs(trunk_created=ago(18)))
            # the host's createdAt is UTC ('Z'): an 18-minute wait is under the 20-minute bar
            self.assertEqual(ci_queue.relieve_trunk(
                p, items=ITEMS, source=ci_queue.GitHubSource(p, run=run),
                out=self.lines.append), (0, 0))
            self.assertEqual(self.cancels(gh), [])
            self.assertEqual(ci_queue._parse('2026-09-25T19:04:00Z'),
                             datetime.datetime(2026, 9, 25, 19, 4, tzinfo=datetime.timezone.utc))
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()

    def test_no_pool_makes_no_gh_call(self):
        self.assertEqual(ci_queue.relieve_trunk(product(pool=False), source=NoGh()), (0, 0))


class TestTrunkJobStarvation(ReliefBase):
    """The trunk run's early jobs ran; a *required* later job sits queued past the wait while PR
    runs created after it take the heavy runners for their own later jobs (one product, 2026-09-26:
    main's m3b-e2e queued 44 min, heavy 12/12 busy, 11 of 13 queued PR runs newer than main)."""

    def product(self, **q):
        data = {'repo_slug': 'o/r',
                'ci': {'provider': 'github-actions', 'workflow': 'ci.yml', 'pool': pool_data(),
                       'queue': dict({'workflows': self.WF}, **q)},
                'deploy_sha': {'prod': {'required_jobs': ['gate', 'm3b-e2e']}}}
        return env.Product('p', data)

    def jobs(self, m3b_queued_min=25, m3b_status='queued', held_101='h2'):
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        j = lambda name, status, labels, runner=None, m=-25: {  # noqa: E731
            'name': name, 'status': status, 'labels': ['self-hosted', *labels],
            'runner_name': runner, 'created_at': t(m), 'started_at': t(m)}
        return {
            900: [j('gate', 'completed', ['heavy'], 'h1'),
                  j('m3b-e2e', m3b_status, ['heavy'], m=-m3b_queued_min),
                  j('build-dev-images', 'queued', ['heavy'])],
            106: [j('gate', 'completed', ['heavy'], 'h3', -5), j('e2e', 'queued', ['heavy'], m=-5)],
            101: [j('e2e', 'in_progress', ['light' if held_101 == 'l1' else 'heavy'], held_101)],
            107: [j('e2e', 'in_progress', ['heavy'], 'h1', -3)],
        }

    def runs(self, trunk_status='queued', trunk_created=None):
        out = super().runs(trunk_status, trunk_created)
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        pr = lambda i, b, m: {'databaseId': i, 'status': 'queued', 'event': 'pull_request',  # noqa
                              'headBranch': b, 'headSha': 'a' * 40, 'createdAt': t(m)}
        # 101 (created before main) and the rest; 102 (Feature, before main) dropped for clarity
        out['pr.yml'] = [pr(101, 'worker/plan-measure', -60), pr(106, 'worker/late', -5),
                         pr(107, 'task/T-0341', -3), pr(103, 'bug/B-0007', -2),
                         pr(104, 'hotfix/db', -1)]
        return out

    def test_newer_runs_are_cancelled_newest_first_until_the_required_job_has_a_runner(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (2, 0))
        # 106 (created after main, only queued jobs) goes first; 101 frees heavy runner h2
        self.assertEqual(self.cancels(gh), ['106', '101'])
        self.assertEqual(self.lines[0],
                         'ci queue: cancelled queued pr run 106 (worker/late, other) — created after '
                         'main run 900 at fffffffff but holds the heavy queue ahead of its queued '
                         'm3b-e2e (queued 25m) [branch worker/late, head aaaaaaaaa; replaced by '
                         'its own re-run once main run 900 starts]')
        self.assertIn('created before main run 900', self.lines[1])
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [106, 101])

    def test_a_freed_runner_counts_only_when_it_carries_the_jobs_labels(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs(held_101='l1'))
        self.assertEqual(self.relieve(p, run), (2, 0))
        # 101 holds a light runner and queues nothing heavy: not in m3b-e2e's way, never
        # cancelled; the Feature run 107 (on h1) goes instead
        self.assertEqual(self.cancels(gh), ['106', '107'])

    def test_s1_and_hotfix_runs_are_never_cancelled(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs()
        runs['pr.yml'] = [r for r in runs['pr.yml'] if r['databaseId'] in (103, 104)]
        runs['batch.yml'] = []
        gh, run = self.gh(runs, jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (0, 0))
        self.assertEqual(self.cancels(gh), [])

    def test_cancelled_runs_are_rerun_once_the_trunk_run_has_its_runners(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.relieve(p, run)
        gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=(),
                          jobs=self.jobs(m3b_status='in_progress'))
        self.assertEqual(self.relieve(p, run, minutes=2), (0, 2))
        self.assertEqual(sorted(self.cancels(gh, 'rerun')), ['101', '106'])
        self.assertEqual(ci_queue.load('p')['relief'], [])

    def test_nothing_is_cancelled_when_the_trunk_is_not_starved(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        # under the wait
        gh, run = self.gh(self.runs(trunk_created=self.at(self.t0 - datetime.timedelta(minutes=19))),
                          jobs=self.jobs(m3b_queued_min=19))
        self.assertEqual(self.relieve(p, run), (0, 0))
        # past the wait, but an idle runner can take the required job
        gh2, run = self.gh(self.runs(), busy=('h1', 'h2'), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (0, 0))
        # the required job runs; only an optional one is queued: the run-level rule alone, which
        # never touches a run created after the trunk run
        gh3, run = self.gh(self.runs(), jobs=self.jobs(m3b_status='in_progress'))
        self.relieve(p, run)
        self.assertEqual(self.cancels(gh) + self.cancels(gh2), [])
        self.assertNotIn('106', self.cancels(gh3))
        self.assertNotIn('107', self.cancels(gh3))


class TestTrunkEscalation(ReliefBase):
    """A required trunk job queued past ``2 × trunk_wait_min``: the host does not hand a freed
    runner to the oldest queued job (one product, 2026-09-26: after relief freed a heavy runner at 12:06Z
    it went to a PR run's gate-tests queued 11:51Z, while main's gate-tests queued since 11:33Z
    waited on), so the runs with their own queued jobs for the same runners are cancelled —
    least sunk first, never S1 or hotfix, until main's queued required jobs fit by label."""

    def product(self, **q):
        data = {'repo_slug': 'o/r',
                'ci': {'provider': 'github-actions', 'workflow': 'ci.yml', 'pool': pool_data(),
                       'queue': dict({'workflows': self.WF}, **q)},
                'deploy_sha': {'prod': {'required_jobs': ['gate', 'm6-e2e']}}}
        return env.Product('p', data)

    def jobs(self, m6_queued_min=45):
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731

        def j(name, status, labels, runner=None, start=None, end=None, created=-30):
            return {'name': name, 'status': status, 'labels': ['self-hosted', *labels],
                    'runner_name': runner, 'created_at': t(created),
                    'started_at': t(start) if start is not None else None,
                    'completed_at': t(end) if end is not None else None}
        return {
            900: [j('gate', 'completed', ['heavy'], 'h1', -60, -48, -60),
                  j('m6-e2e', 'queued', ['heavy'], created=-m6_queued_min)],
            # newest; 20 heavy minutes done + 5 running on h1; its m3b-e2e queued for heavy
            110: [j('gate', 'completed', ['heavy'], 'h1', -26, -6, -26),
                  j('p1-e2e', 'in_progress', ['heavy'], 'h1', -5),
                  j('m3b-e2e', 'queued', ['heavy'], created=-6)],
            # 3 heavy minutes on h2 (plus a light job, not counted); its later jobs queued
            111: [j('connector-drill', 'completed', ['light'], 'l1', -14, -4, -15),
                  j('gate', 'in_progress', ['heavy'], 'h2', -3),
                  j('m8-e2e', 'queued', ['heavy'], created=-15)],
            # 10 heavy minutes on h3; its later jobs queued
            112: [j('gate', 'in_progress', ['heavy'], 'h3', -10),
                  j('m6-e2e', 'queued', ['heavy'], created=-10)],
            # everything it runs is on a runner: nothing queued competes, never a candidate
            113: [j('gate', 'in_progress', ['heavy'], 'h3', -1)],
            # only a light job queued: it does not compete for main's heavy runner
            114: [j('docs', 'queued', ['light'], created=-2)],
            # S1: never, however little is sunk
            103: [j('m6-e2e', 'queued', ['heavy'], created=-2)],
        }

    def runs(self, trunk_status='queued', trunk_created=None):
        out = super().runs(trunk_status, trunk_created or self.at(self.t0 - datetime.timedelta(
            minutes=60)))
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        pr = lambda i, s, b, m: {'databaseId': i, 'status': s, 'event': 'pull_request',  # noqa
                                 'headBranch': b, 'headSha': 'a' * 40, 'createdAt': t(m)}
        # the host keeps a run `queued` while any job of it waits, even with jobs on runners
        out['pr.yml'] = [pr(110, 'queued', 'task/T-0500', -5), pr(112, 'queued', 'bug/B-0008', -10),
                         pr(111, 'queued', 'task/T-0341', -15), pr(113, 'in_progress',
                                                                   'task/T-0500', -20),
                         pr(114, 'queued', 'task/T-0500', -2), pr(103, 'queued', 'bug/B-0007', -2)]
        out['batch.yml'] = []
        return out

    def test_escalation_cancels_the_least_sunk_competing_run_first(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (1, 0))
        # 111 has 3 heavy minutes sunk (112: 10, 110: 25) and frees h2: m6-e2e fits, stop
        self.assertEqual(self.cancels(gh), ['111'])
        self.assertEqual(self.lines, [
            "ci queue: cancelled in-progress pr run 111 (T-0341) — its queued heavy jobs compete "
            "with main's m6-e2e queued 45m; sunk 3 min [branch task/T-0341, head aaaaaaaaa; "
            "replaced by its own re-run once main run 900 starts]"])
        rec = ci_queue.load('p')['relief']
        self.assertEqual([r['id'] for r in rec], [111])
        self.assertEqual(rec[0]['kind'], 'pr')

    def test_escalation_never_cancels_a_run_whose_pr_changes_ci_config(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs()
        for r in runs['pr.yml']:
            r['headSha'] = f"sha{r['databaseId']}"
        gh, run = self.gh(runs, jobs=self.jobs(), files={111: ['.github/workflows/ci.yml']})
        # 111 (least sunk) is exempt: 112 (next least sunk) is cancelled instead, and its held
        # runner (h3) is enough for m6-e2e to fit
        self.assertEqual(self.relieve(p, run), (1, 0))
        self.assertEqual(self.cancels(gh), ['112'])
        self.assertIn('relief: exempt task/T-0341 — changes CI config', self.lines)
        rec = ci_queue.load('p')['relief']
        self.assertEqual([r['id'] for r in rec], [112])

    def test_below_the_escalation_bar_the_job_level_order_holds(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs(m6_queued_min=30))
        self.assertEqual(self.relieve(p, run), (1, 0))
        # 30m: past trunk_wait_min (20), under 2 × 20 — newest first: 114 queues only a light
        # job and holds no runner (not in the heavy runners' way: skipped), then 110 (frees h1)
        self.assertEqual(self.cancels(gh), ['110'])
        self.assertTrue(all('cancelled queued pr run' in l for l in self.lines))

    def test_the_escalation_bar_is_configurable(self):
        p = self.product(trunk_escalate_min=60)
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        self.assertEqual(ci_queue.trunk_escalate_min(p), 60)
        self.assertEqual(ci_queue.trunk_escalate_min(self.product(trunk_wait_min=15)), 30)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.relieve(p, run)
        self.assertEqual(self.cancels(gh), ['110'])     # 45m < 60m: not escalated
        self.assertEqual(ci_queue.config_problems({'queue': {'trunk_escalate_min': 0}}),
                         [('ci.queue.trunk_escalate_min', 'must be a number of minutes > 0, not 0')])
        self.assertEqual(ci_queue.config_problems({'queue': {'trunk_escalate_min': 50}}), [])

    def test_escalation_keeps_going_while_the_freed_runners_do_not_fit(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        jobs = self.jobs()
        # main needs two heavy runners now: 111 (h2) alone is not enough, 112 (h3) is next
        jobs[900].append(dict(jobs[900][1], name='gate'))
        gh, run = self.gh(self.runs(), jobs=jobs)
        self.assertEqual(self.relieve(p, run), (2, 0))
        self.assertEqual(self.cancels(gh), ['111', '112'])
        self.assertIn("sunk 10 min", self.lines[1])
        self.assertNotIn('103', self.cancels(gh))        # S1
        self.assertNotIn('113', self.cancels(gh))        # nothing queued
        self.assertNotIn('114', self.cancels(gh))        # its queued job needs no heavy runner

    def test_escalated_cancels_are_rerun_once_the_trunk_run_has_started(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.relieve(p, run)
        gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=(), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run, minutes=2), (0, 1))
        self.assertEqual(self.cancels(gh, 'rerun'), ['111'])
        self.assertEqual(ci_queue.load('p')['relief'], [])

    def test_dry_run_names_the_escalation_and_cancels_nothing(self):
        p = self.product(mode='dry-run')
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (0, 0))
        self.assertEqual(self.cancels(gh), [])
        self.assertEqual(self.lines, [
            "ci queue: would cancel in-progress pr run 111 (T-0341) — its queued heavy jobs "
            "compete with main's m6-e2e queued 45m; sunk 3 min"])


class TestS1PrRelief(ReliefBase):
    """An open S1 fix PR's run is served like the trunk run (2026-09-26: one product's S1 fix, the one
    prod was held on, had its heavy jobs queued behind ~20 feature-PR jobs while the relief acted
    only for main): its required jobs queued past ``ci.queue.s1_wait_min`` get the PR/batch runs
    in their way cancelled, lowest priority and newest first, never trunk, S1, hotfix, a run on
    runners or a CI-changing PR; each is re-run once the S1 run's required jobs have started."""

    def product(self, **q):
        data = {'repo_slug': 'o/r',
                'ci': {'provider': 'github-actions', 'workflow': 'ci.yml', 'pool': pool_data(),
                       'queue': dict({'workflows': self.WF}, **q)},
                'deploy_sha': {'prod': {'required_jobs': ['gate', 'm6-e2e']}}}
        return env.Product('p', data)

    def jobs(self, s1_queued_min=8, s1_status='queued'):
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731

        def j(name, status, labels, runner=None, created=-10):
            return {'name': name, 'status': status, 'labels': ['self-hosted', *labels],
                    'runner_name': runner, 'created_at': t(created),
                    'started_at': t(created) if runner else None}
        return {
            850: [j('gate', 'completed', ['heavy'], 'h1'),
                  j('m6-e2e', s1_status, ['heavy'], 'h1' if s1_status != 'queued' else None,
                    created=-s1_queued_min)],
            # a Feature PR created after the S1 run: its gate holds h2, its m6-e2e waits
            120: [j('gate', 'in_progress', ['heavy'], 'h2', -3),
                  j('m6-e2e', 'queued', ['heavy'], created=-2)],
            # an ordinary PR, newest, only queued jobs: it holds no runner
            121: [j('gate', 'queued', ['heavy'], created=-1)],
            # the CI-changing PR, the hotfix and the other S1 run: never, whatever they hold
            123: [j('gate', 'in_progress', ['heavy'], 'h3', -1)],
            104: [j('gate', 'in_progress', ['heavy'], 'h3', -1)],
            103: [j('gate', 'in_progress', ['heavy'], 'h3', -1)],
        }

    def runs(self, trunk_status='in_progress', s1_status='queued', only=None):
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        trunk = {'databaseId': 900, 'status': trunk_status, 'event': 'push', 'headBranch': 'main',
                 'headSha': 'f' * 40, 'createdAt': t(-30), 'startedAt': t(-29)}
        pr = lambda i, s, b, m: {'databaseId': i, 'status': s, 'event': 'pull_request',  # noqa
                                 'headBranch': b, 'headSha': f'sha{i}', 'createdAt': t(m)}
        prs = [pr(850, s1_status, 'fix-bug/fix-bug-b-0007', -10),   # the S1 fix, lower case
               pr(120, 'queued', 'task/T-0341', -3),                # Feature, newer
               pr(121, 'queued', 'task/T-0500', -1),                # ordinary, newest
               pr(122, 'in_progress', 'task/T-0500', -4),           # on runners: never
               pr(123, 'queued', 'task/T-0500', -0.5),              # changes CI config: never
               pr(104, 'queued', 'hotfix/db', -2),                  # hotfix: never
               pr(103, 'queued', 'bug/B-0007', -2)]                 # S1: never
        if only is not None:
            prs = [r for r in prs if r['databaseId'] in only]
        return {'ci.yml': [trunk], 'pr.yml': prs, 'batch.yml': []}

    def gh(self, runs, busy=('h1', 'h2', 'h3'), jobs=None, files=None):
        return super().gh(runs, busy=busy, jobs=jobs,
                          files={123: ['.github/workflows/ci.yml'], **(files or {})})

    def test_s1_pr_relief_cancels_a_newer_feature_pr_run(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (2, 0))
        # lowest priority first, newest first within it: 121 (holds nothing), then the Feature
        # run 120, whose gate frees h2 — the S1 run's m6-e2e fits, stop
        self.assertEqual(self.cancels(gh), ['121', '120'])
        self.assertEqual(self.lines[-1],
                         'ci queue: cancelled queued pr run 120 (T-0341, Task F-0001 rank 1) — created after '
                         'S1 PR run 850 (B-0007) at sha850 but holds the heavy queue ahead of its '
                         'queued m6-e2e (queued 8m) [branch task/T-0341, head sha120; replaced '
                         'by its own re-run once S1 PR run 850 (B-0007) starts]')
        self.assertIn('relief: exempt task/T-0500 — changes CI config', self.lines)
        rec = ci_queue.load('p')['relief']
        self.assertEqual([r['id'] for r in rec], [121, 120])
        self.assertEqual({r['for'] for r in rec}, {850})

    def test_never_cancels_trunk_s1_hotfix_in_progress_or_ci_changing_runs(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs(trunk_status='queued', only=(850, 122, 123, 104, 103))
        # main's own run is queued too, but under trunk_wait_min: nothing for it, and never a
        # candidate for the S1 run
        runs['ci.yml'][0]['createdAt'] = self.at(self.t0 - datetime.timedelta(minutes=2))
        gh, run = self.gh(runs, jobs=self.jobs())
        self.assertEqual(self.relieve(p, run), (0, 0))
        self.assertEqual(self.cancels(gh), [])

    def test_under_s1_wait_min_nothing_is_cancelled_and_the_wait_is_configurable(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        self.assertEqual(ci_queue.s1_wait_min(p), 5)
        gh, run = self.gh(self.runs(), jobs=self.jobs(s1_queued_min=4))
        self.assertEqual(self.relieve(p, run), (0, 0))
        gh2, run = self.gh(self.runs(), jobs=self.jobs())
        self.assertEqual(self.relieve(self.product(s1_wait_min=10), run), (0, 0))
        self.assertEqual(self.cancels(gh) + self.cancels(gh2), [])
        self.assertEqual(ci_queue.config_problems({'queue': {'s1_wait_min': 0}}),
                         [('ci.queue.s1_wait_min', 'must be a number of minutes > 0, not 0')])
        self.assertEqual(ci_queue.config_problems({'queue': {'s1_wait_min': 3}}), [])

    def test_cancelled_runs_are_rerun_once_the_s1_runs_required_jobs_have_started(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.relieve(p, run)
        # still queued a minute later: nothing re-run yet
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.assertEqual(self.relieve(p, run, minutes=1)[1], 0)
        self.assertEqual(self.cancels(gh, 'rerun'), [])
        # its m6-e2e has a runner (the host still calls the run queued while other jobs wait)
        gh, run = self.gh(self.runs(), busy=(), jobs=self.jobs(s1_status='in_progress'))
        self.assertEqual(self.relieve(p, run, minutes=2), (0, 2))
        self.assertEqual(sorted(self.cancels(gh, 'rerun')), ['120', '121'])
        self.assertIn('started after waiting', self.lines[-1])
        self.assertIn('S1 PR run 850', self.lines[-1])
        self.assertEqual(ci_queue.load('p')['relief'], [])

    def test_an_older_queued_run_whose_jobs_sit_ahead_of_the_s1_run_is_cancelled_too(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        runs = self.runs(only=(850, 104, 103))
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        # created 50 min before the S1 run; its heavy job queued ahead of the S1 run's, its
        # gate on h2
        runs['pr.yml'].append({'databaseId': 130, 'status': 'queued', 'event': 'pull_request',
                               'headBranch': 'task/T-0500', 'headSha': 'sha130',
                               'createdAt': t(-60)})
        jobs = self.jobs()
        jobs[130] = [{'name': 'gate', 'status': 'in_progress', 'labels': ['self-hosted', 'heavy'],
                      'runner_name': 'h2', 'created_at': t(-60), 'started_at': t(-12)},
                     {'name': 'm6-e2e', 'status': 'queued', 'labels': ['self-hosted', 'heavy'],
                      'runner_name': None, 'created_at': t(-40), 'started_at': None}]
        gh, run = self.gh(runs, jobs=jobs)
        self.assertEqual(self.relieve(p, run), (1, 0))
        self.assertEqual(self.cancels(gh), ['130'])
        self.assertIn('created before S1 PR run 850 (B-0007)', self.lines[-1])

    def test_a_superseded_s1_run_hands_its_records_to_the_branchs_newer_run(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        self.relieve(p, run)
        # a push supersedes 850 before its jobs start: it ends cancelled, 851 is queued
        runs = self.runs()
        s1 = runs['pr.yml'][0]
        s1.update(status='completed', conclusion='cancelled')
        runs['pr.yml'].append(dict(s1, databaseId=851, status='queued', conclusion='',
                                   headSha='sha851', createdAt=self.at(self.t0)))
        jobs = self.jobs()
        jobs[851] = [dict(j, created_at=self.at(self.t0)) for j in jobs.pop(850)]
        gh, run = self.gh(runs, jobs=jobs)
        self.assertEqual(self.relieve(p, run, minutes=1)[1], 0)
        self.assertEqual(self.cancels(gh, 'rerun'), [])
        self.assertEqual(sorted(r['id'] for r in ci_queue.load('p')['relief']), [120, 121])
        # 851's required jobs get runners: now they are re-run
        jobs[851] = [dict(j, status='in_progress', runner_name='h1') for j in jobs[851]]
        gh, run = self.gh(runs, busy=(), jobs=jobs)
        self.assertEqual(self.relieve(p, run, minutes=2)[1], 2)
        self.assertEqual(sorted(self.cancels(gh, 'rerun')), ['120', '121'])

    def test_relief_is_label_aware_a_run_on_runners_the_s1_job_cannot_use_is_left(self):
        # one product, 2026-09-26: the S1 run's jobs need alpha-heavy AND class-pr-heavy; the
        # busy heavy boxes of the other provider carry neither, so cancelling runs on them frees nothing
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        pair = ['self-hosted', 'alpha-heavy', 'class-pr-heavy']

        def job(name, status, labels, runner=None, created=-10):
            return {'name': name, 'status': status, 'labels': labels, 'runner_name': runner,
                    'created_at': t(created), 'started_at': t(created) if runner else None}
        jobs = {850: [job('gate', 'completed', ['self-hosted', 'heavy'], 'h1'),
                      job('m6-e2e', 'queued', pair, created=-8)],
                # newest, ordinary: its job runs on the other-provider box h1, nothing of it queued
                140: [job('gate', 'in_progress', ['self-hosted', 'heavy'], 'h1', -1)],
                # older Feature run: its p1-e2e holds h2, a alpha-heavy class-pr-heavy runner
                141: [job('p1-e2e', 'in_progress', pair, 'h2', -9)]}
        runs = self.runs(only=(850,))
        pr = lambda i, b, m: {'databaseId': i, 'status': 'queued', 'event': 'pull_request',  # noqa
                              'headBranch': b, 'headSha': f'sha{i}', 'createdAt': t(m)}
        runs['pr.yml'] += [pr(140, 'task/T-0500', -1), pr(141, 'task/T-0341', -30)]
        gh, run = self.gh(runs, jobs=jobs)
        labels = {'h1': ['heavy', 'beta-heavy'], 'h2': ['heavy', 'alpha-heavy', 'class-pr-heavy'],
                  'h3': ['heavy', 'alpha-heavy', 'class-pr-heavy'], 'l1': ['light']}

        def with_labels(argv, **kw):
            if argv[:2] == ['gh', 'api'] and any('actions/runners' in a for a in argv):
                gh.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '\n'.join(json.dumps({
                    'name': n, 'id': i, 'busy': n != 'l1', 'status': 'online',
                    'labels': [{'name': x} for x in ['self-hosted', *ls]]})
                    for i, (n, ls) in enumerate(labels.items())), '')
            return run(argv, **kw)
        self.assertEqual(self.relieve(p, with_labels), (1, 0))
        self.assertEqual(self.cancels(gh), ['141'])

    def test_trunk_relief_records_wait_for_the_trunk_not_the_s1_run(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        ci_queue.save('p', dict(ci_queue.load('p'), relief=[
            {'id': 777, 'kind': 'pr', 'item': 'T-0500', 'prio': 3, 'label': 'Task',
             'workflow': 'pr.yml', 'at': self.at(self.t0), 'trunk_id': 900}]))
        runs = self.runs(trunk_status='queued', only=(850,))
        runs['ci.yml'][0]['createdAt'] = self.at(self.t0 - datetime.timedelta(minutes=2))
        gh, run = self.gh(runs, busy=(), jobs=self.jobs(s1_status='in_progress'))
        self.relieve(p, run, minutes=1)
        self.assertEqual(self.cancels(gh, 'rerun'), [])       # main still queued: kept
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [777])


class NoRunnersGh(FakeGh):
    """``gh`` whose runner read fails (the in-flight count still reads): the ceiling's fallback."""

    def __call__(self, argv, **kw):
        if argv[:2] == ['gh', 'api'] and any('actions/runners' in a for a in argv):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', 'unreachable')
        return super().__call__(argv, **kw)


class TestCeiling(Base):
    """``capacity.ci`` is no longer the batch's gate: a batch start is admitted by the free
    capacity of the classes it needs against its per-run need. The fixed in-flight ceiling holds
    a batch only when the runners cannot be read."""

    def test_admission_under_budget_a_batch_starts_on_free_heavy_whatever_runs_are_in_flight(self):
        for n, inflight in enumerate((4, 9)):     # the old gate held both: 4/4 and 9/4
            p = product(cap={'ci': 4}, name=f'b{n}')
            d = self.admit(self.queue(p, FakeGh(inflight=inflight)), 'batch', 'batch',
                           kind='batch')
            self.assertTrue(d.admitted, self.lines)
        self.assertEqual(self.lines, [])

    def test_admission_saturated_a_batch_waits_for_heavy_capacity(self):
        p = product(cap={'ci': 4})
        d = self.admit(self.queue(p, FakeGh(inflight=1, busy={'h1', 'h2', 'h3'})), 'batch',
                       'batch', kind='batch')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: batch waits — heavy 0 free, needs 3 '
                                      '(other, 1st in line)'])

    def test_capacity_ci_is_the_fallback_when_the_runners_are_unreadable(self):
        p = product(cap={'ci': 2})
        d = self.admit(self.queue(p, NoRunnersGh(inflight=2)), 'batch', 'batch', kind='batch')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: batch waits — at the ci ceiling (2 runs in '
                                      'flight; batch starts below 2) (other, 1st in line)'])
        p2 = product(cap={'ci': 2}, name='p2')
        q = self.queue(p2, NoRunnersGh(inflight=1), minutes=1)
        self.assertTrue(self.admit(q, 'pr:a', 'T-0500').admitted)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)  # 1 + 1 admitted
        self.assertIn('1 runs in flight + 1 started this pass', self.lines[-1])

    def test_a_feature_pr_at_the_ceiling_starts_when_the_runners_fit(self):
        p = product(cap={'ci': 4})
        q = self.queue(p, FakeGh(inflight=4))
        self.assertTrue(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertEqual(self.lines, [])
        q = self.queue(p, FakeGh(inflight=9, busy={'h1', 'h2'}), minutes=5)
        self.assertFalse(self.admit(q, 'pr:feat2', 'T-0341').admitted)   # the fit still holds
        self.assertEqual(self.lines, ['ci queue: T-0341 waits — heavy 1 free, needs 3 '
                                      '(Task F-0001 rank 1, 1st in line)'])

    def test_s1_hotfix_trunk_and_deploy_starts_are_exempt_from_ceiling_and_fit(self):
        p = product(cap={'ci': 4})
        q = self.queue(p, FakeGh(inflight=4))
        self.assertTrue(self.admit(q, 'trunk:t', 'T-0500', kind='trunk').admitted)
        q = self.queue(p, FakeGh(inflight=4), minutes=10)
        self.assertTrue(self.admit(q, 'pr:hotfix/x', 'T-0500', branch='hotfix/x').admitted)
        q = self.queue(p, FakeGh(inflight=4), minutes=20)
        self.assertTrue(self.admit(q, 'pr:fix', 'B-0007').admitted)
        q = self.queue(p, FakeGh(inflight=4), minutes=30)
        self.assertTrue(self.admit(q, 'deploy:prod', 'deploy prod', kind='deploy').admitted)
        self.lines.clear()
        q = self.queue(p, FakeGh(inflight=4, busy={'h1', 'h2'}), minutes=50)
        self.assertTrue(self.admit(q, 'trunk:t2', 'T-0500', kind='trunk').admitted)
        self.assertEqual(self.lines, [])

    def test_ceiling_applies_to_batch_starts_only_fit_to_batch_and_ordinary_prs(self):
        ceil, fit = ci_queue.ceiling_applies, ci_queue.fit_applies
        self.assertFalse(ceil({'kind': 'pr', 'prio': ci_queue.OTHER}))
        self.assertFalse(ceil({'kind': 'pr', 'prio': ci_queue.RANKED}))
        self.assertTrue(ceil({'kind': 'batch', 'prio': ci_queue.OTHER}))
        self.assertTrue(fit({'kind': 'pr', 'prio': ci_queue.OTHER}))
        self.assertTrue(fit({'kind': 'pr', 'prio': ci_queue.RANKED}))
        self.assertTrue(fit({'kind': 'batch', 'prio': ci_queue.OTHER}))
        for ok in (ceil, fit):
            self.assertFalse(ok({'kind': 'pr', 'prio': ci_queue.S1}))
            self.assertFalse(ok({'kind': 'trunk', 'prio': ci_queue.TRUNK}))
            self.assertFalse(ok({'kind': 'deploy', 'prio': ci_queue.OTHER}))


class TestOneCount(Base):
    """The status row and the queue read one in-flight count, from one function."""

    def test_the_queue_and_the_row_read_the_same_function_and_argv(self):
        from asf import capacity, gh_limit
        from unittest import mock
        # the read memo keys on id(subprocess.run): an earlier test's mock freed at the same
        # address would answer for this one, and subprocess.run would never be called
        gh_limit.reset()
        p = product(cap={'ci': 4})
        gh = FakeGh(inflight=5)
        self.assertEqual(ci_queue.GitHubSource(p, run=gh).inflight(), 5)
        with mock.patch('subprocess.run', side_effect=FakeGh(inflight=5)) as run:
            self.assertEqual(capacity.CiRuns().read(p), 5)
        self.assertEqual(run.call_args[0][0], gh.calls[-1])
        self.assertEqual(capacity.ci_inflight_text(5), '5 runs in flight')
        self.assertIn('not completed', capacity.CI_INFLIGHT_WHAT)

    def test_a_ceiling_hold_in_the_row_names_the_rows_own_count(self):
        """The queue held a batch at 4/4; the row's count is now 5: the row names 5, never 4."""
        from unittest import mock
        from asf import capacity
        from asf.views import status
        p = product(cap={'ci': 4})
        # one fixed instant for the queue and the row: a wall clock that crosses a minute
        # between the admit and the row would otherwise read "waits 1 min"
        for clock in (mock.patch.object(ci_queue, '_now', return_value=self.t0),
                      mock.patch.object(capacity, '_now', return_value=self.t0)):
            clock.start()
            self.addCleanup(clock.stop)
        self.assertFalse(self.admit(self.queue(p, NoRunnersGh(inflight=4)), 'batch', 'batch',
                                    kind='batch').admitted)
        self.assertIn('(4 runs in flight;', self.lines[-1])
        with mock.patch('subprocess.run', side_effect=NoRunnersGh(inflight=5)):
            cell = status.capacity_cell({}, p)
        self.assertIn('ci 5 runs in flight (batch admitted by free runners; ceiling 4 only '
                      'when they are unreadable)', cell)
        self.assertIn('head batch waits 0 min — at the ci ceiling (5 runs in flight; batch '
                      'starts below 4) (other, 1st in line)', cell)
        self.assertNotIn('4 runs in flight', cell)
        self.assertNotIn('4/4', cell)
        # the count dropped below the ceiling: the head is not said to be at it
        with mock.patch('subprocess.run', side_effect=NoRunnersGh(inflight=2)):
            cell = status.capacity_cell({}, p)
        self.assertIn('ci 2 runs in flight', cell)
        self.assertNotIn('at the ci ceiling', cell)

    def test_a_saturated_batch_is_a_fit_hold_on_the_heads_clock(self):
        """With the runners read, a batch is held by the runner fit, not the run count: the
        row names the heavy shortfall and the head guard's clock."""
        p = product(cap={'ci': 4})
        self.t0 = ci_queue._now()
        gh = FakeGh(inflight=4, busy={'h1', 'h2', 'h3'})
        self.assertFalse(self.admit(self.queue(p, gh), 'batch', 'batch', kind='batch').admitted)
        row = ci_queue.status_clause(p, now=self.t0 + datetime.timedelta(minutes=1),
                                     inflight=4, ceiling=4,
                                     source=ci_queue.GitHubSource(p, run=gh))
        self.assertIn('head batch waits 1 min', row)
        self.assertIn('heavy 0 free, needs 3', row)
        self.assertNotIn('at the ci ceiling', row)

    def test_a_ceiling_hold_behind_the_head_does_not_silence_the_clause(self):
        p = product(cap={'ci': 4})
        self.t0 = ci_queue._now()
        gh = FakeGh(inflight=4, busy={'h1', 'h2', 'h3'})
        self.assertFalse(self.admit(self.queue(p, gh), 'batch', 'batch', kind='batch').admitted)
        self.assertFalse(self.admit(self.queue(p, gh), 'pr:a', 'T-0341').admitted)
        row = ci_queue.status_clause(p, now=self.t0, inflight=4, ceiling=4,
                                     source=ci_queue.GitHubSource(p, run=gh))
        self.assertEqual(row, 'ci queue 2, head T-0341 waits 0 min (0 min at the head; '
                              'admitted at 20) — heavy 0 free, needs 3 '
                              '(Task F-0001 rank 1, 1st in line)')


class TestModes(Base):
    def test_no_pool_falls_back_to_starting_at_once_with_no_gh_call(self):
        p = product(pool=False, cap={'ci': 1})
        d = self.admit(self.queue(p, NoGh()), 'pr:a', 'T-0500')
        self.assertTrue(d.admitted and d.bypass)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'state', 'p', ci_queue.QUEUE_FILE)))
        self.assertIsNone(ci_queue.status_clause(p))
        self.assertTrue(deploy.queue_admits(p, 'prod', 'a' * 40, source=NoGh()))

    def test_mode_off_falls_back_too(self):
        p = product(queue={'mode': False})  # YAML reads `off` as false
        self.assertEqual(ci_queue.mode(p), 'off')
        self.assertTrue(self.admit(self.queue(p, NoGh()), 'pr:a', 'T-0500').bypass)

    def test_dry_run_says_what_would_wait_starts_it_and_writes_nothing(self):
        p = product(queue={'mode': 'dry-run'})
        d = self.admit(self.queue(p, FakeGh(busy={'h1', 'h2'})), 'pr:a', 'T-0500')
        self.assertTrue(d.admitted)
        self.assertEqual(self.lines, ['ci queue (dry-run): T-0500 would wait — heavy 1 free, '
                                      'needs 3 (Task unranked, 1st in line)'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'state', 'p', ci_queue.QUEUE_FILE)))

    def test_the_view_says_view_only_and_dry_run_only_for_mode_dry_run(self):
        """``asf ci queue`` never writes; its header said DRY RUN under mode ``on`` too, and read
        as a queue in dry-run. DRY RUN is now the mode's word alone."""
        import types
        from unittest import mock
        heads = {}
        for m in ('on', 'dry-run'):
            p = product(queue={'mode': m})
            lines = []
            with mock.patch.object(env, 'load_product', return_value=p):
                ci_queue.cmd_queue(types.SimpleNamespace(product='p'),
                                   source=ci_queue.GitHubSource(p, run=FakeGh()), out=lines.append)
            heads[m] = lines[0]
        self.assertEqual(heads['on'], '== CI QUEUE p (mode on, 0 waiting; view only — nothing '
                                      'written)')
        self.assertNotIn('DRY RUN', heads['on'])
        self.assertIn('mode DRY RUN', heads['dry-run'])
        self.assertIn('view only', heads['dry-run'])

    def src(self, p, gh):
        return ci_queue.GitHubSource(p, run=gh)

    def test_status_names_the_depth_and_the_head(self):
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        self.assertEqual(ci_queue.status_clause(p, now=self.t0, source=self.src(p, busy)),
                         'ci queue empty')
        self.admit(self.queue(p, busy), 'pr:a', 'T-0500')
        self.admit(self.queue(p, busy), 'pr:b', 'T-0341')
        self.assertEqual(ci_queue.status_clause(p, now=self.t0, source=self.src(p, busy)),
                         'ci queue 2, head T-0341 waits 0 min (0 min at the head; '
                         'admitted at 20) — heavy 0 free, needs 3 '
                         '(Task F-0001 rank 1, 1st in line)')

    def view(self, p, gh):
        import types
        from unittest import mock
        lines = []
        with mock.patch.object(env, 'load_product', return_value=p):
            ci_queue.cmd_queue(types.SimpleNamespace(product='p'), source=self.src(p, gh),
                               out=lines.append)
        return lines

    def test_the_row_and_the_view_agree_live_not_the_ticks_snapshot(self):
        """The tick held the head at heavy 0 free; the runners have freed since. ``asf ci queue``
        says it would start, and the row says the same — never the tick's stored hold."""
        p = product()
        self.t0 = ci_queue._now()
        self.assertFalse(self.admit(self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'})), 'pr:a',
                                    'T-0341').admitted)
        for gh, said in ((FakeGh(), 'would start'),
                         (FakeGh(busy={'h1', 'h2'}), 'waits: heavy 1 free, needs 3')):
            view = self.view(p, gh)
            self.assertIn(f'1. T-0341 [pr, Task F-0001 rank 1, full run, since', view[2])
            self.assertTrue(view[2].endswith(said), view[2])
            row = ci_queue.status_clause(p, source=self.src(p, gh))
            self.assertEqual(row, f"ci queue 1, head T-0341 "
                                  f"{said.replace('waits:', 'waits 0 min (0 min at the head; admitted at 20) —')} "
                                  f"(Task F-0001 rank 1, 1st in line)")
            self.assertNotIn('as of tick', row)

    def write_mode(self, name, m):
        os.makedirs(os.path.join(self.tmp, 'products'), exist_ok=True)
        with open(env.product_path(name), 'w', encoding='utf-8') as fh:
            fh.write(f'repo_slug: o/r\nci:\n  workflow: ci.yml\n  queue:\n    mode: {m}\n')

    def test_dry_run_in_the_product_file_holds_nothing_on_a_product_loaded_before(self):
        """The tick loaded its product with the queue on; the operator set ``mode: dry-run`` in
        the product file mid-pass: the lane's PR start is not held, the line says would wait."""
        p = product(queue={'mode': 'on'})
        self.write_mode('p', 'dry-run')
        self.assertEqual(ci_queue.mode(p), 'dry-run')
        d = self.admit(self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'})), 'pr:a', 'T-0341')
        self.assertTrue(d.admitted)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith('ci queue (dry-run): T-0341 would wait'),
                        self.lines[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'state', 'p', ci_queue.QUEUE_FILE)))
        self.write_mode('p', 'off')
        self.assertEqual(ci_queue.mode(p), 'off')

    def test_the_lane_opens_a_pr_under_dry_run_from_the_product_file(self):
        import types
        from unittest import mock
        from asf.harvest import lane
        p = product(queue={'mode': 'on'})
        self.write_mode('p', 'dry-run')
        fake = types.SimpleNamespace(product=p, out=self.lines.append, ci_queue=None, items=ITEMS)
        with mock.patch('subprocess.run', side_effect=FakeGh(busy={'h1', 'h2', 'h3'})):
            self.assertTrue(lane.Lane.ci_admits(fake, {'branch': 'feat/x', 'item': 'T-0341'},
                                                'pr'))
        self.assertIn('would wait', self.lines[-1])

    def test_a_mode_change_shows_in_the_row_at_once(self):
        """The tick ran in dry-run; the mode is back to on: the row drops ``(dry-run)`` now."""
        self.t0 = ci_queue._now()
        dry = product(queue={'mode': 'dry-run'})
        self.admit(self.queue(product(), FakeGh(busy={'h1', 'h2', 'h3'})), 'pr:a', 'T-0500')
        gh = FakeGh(busy={'h1', 'h2', 'h3'})
        self.assertIn('ci queue 1 (dry-run), head T-0500 waits',
                      ci_queue.status_clause(dry, source=self.src(dry, gh)))
        on = product(queue={'mode': 'on'})
        row = ci_queue.status_clause(on, source=self.src(on, gh))
        self.assertTrue(row.startswith('ci queue 1, head T-0500 waits 0 min (0 min at the head; '
                                       'admitted at 20) — heavy 0 free'), row)
        self.assertNotIn('dry-run', row)

    def test_an_unreadable_host_shows_the_snapshot_dated(self):
        p = product()
        self.t0 = ci_queue._now()
        self.admit(self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'})), 'pr:a', 'T-0341')
        stamp = datetime.datetime.fromtimestamp(
            os.path.getmtime(ci_queue._path('p'))).strftime('%H:%M')
        self.assertEqual(ci_queue.status_clause(p, source=self.src(p, DeadGh())),
                         f'ci queue 1 (as of tick {stamp}), head T-0341 waits 0 min (0 min at the '
                         f'head; admitted at 20) — heavy 0 free, '
                         f'needs 3 (Task F-0001 rank 1, 1st in line)')

    def test_the_row_and_the_queue_line_read_one_estimate(self):
        """The head was held when the estimate said 3; another start of the same tick measured
        it again at 2: the row re-states the hold from the one cached estimate, never the
        number frozen in the old line."""
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        q = self.queue(p, busy)
        self.assertFalse(self.admit(q, 'pr:a', 'T-0341').admitted)
        self.assertIn('heavy 0 free, needs 3', self.lines[-1])
        self.assertIn('heavy 0 free, needs 3',
                      ci_queue.status_clause(p, now=self.t0, source=self.src(p, busy)))
        data = ci_queue.load('p')
        data['expect']['ci.yml']['needs'] = {'heavy': 2, 'light': 1}
        ci_queue.save('p', data)
        row = ci_queue.status_clause(p, now=self.t0, source=self.src(p, busy))
        self.assertIn('head T-0341 waits 0 min (0 min at the head; admitted at 20) — heavy 0 free, '
                      'needs 2 (Task F-0001 rank 1, 1st in line)', row)
        # the snapshot, when the host is unreadable, re-states the hold from the same estimate
        row = ci_queue.status_clause(p, now=self.t0, source=self.src(p, DeadGh()))
        self.assertIn('head T-0341 waits 0 min (0 min at the head; admitted at 20) — heavy 0 free, '
                      'needs 2 (Task F-0001 rank 1, 1st in line)', row)
        q = self.queue(p, busy, minutes=1)
        self.assertFalse(self.admit(q, 'pr:a', 'T-0341').admitted)
        self.assertEqual(self.lines[-1].split(' — ', 1)[1].split(' (')[0],
                         row.split(' — ', 1)[1].split(' (')[0])

    def test_config_problems(self):
        self.assertEqual(ci_queue.config_problems({'queue': {'mode': 'on', 'history': 5,
                                                             'workflows': {'batch': 'b.yml'}}}), [])
        bad = dict(ci_queue.config_problems({'queue': {'mode': 'loud', 'history': 0,
                                                       'workflows': {'nightly': 'x'}, 'x': 1}}))
        self.assertEqual(sorted(bad), ['ci.queue.history', 'ci.queue.mode',
                                       'ci.queue.workflows.nightly', 'ci.queue.x'])



def wide_pool(n_heavy=12, n_light=7):
    """A pool like a real product's: ``n_heavy`` heavy runners, ``n_light`` light ones."""
    from asf import ci_pool
    rows = [{'runner': f'h{i}', 'provider': 'alpha', 'role': 'heavy'} for i in range(1, n_heavy + 1)]
    rows += [{'runner': f'l{i}', 'provider': 'alpha', 'role': 'light'} for i in range(1, n_light + 1)]
    return ci_pool.load_pool(env.Product('p', {'ci': {'pool': rows}}))


def live_runners(pool, sub='fast-heavy', carriers=('h1', 'h2', 'h3', 'h4', 'h5', 'h6')):
    """The runners API: every heavy runner carries ``heavy``, some also the sub-label ``sub``."""
    from asf import ci_pool
    out = []
    for e in pool:
        labels = ['self-hosted', 'linux', e.role] + ([sub] if e.runner in carriers else [])
        out.append(ci_pool.Runner(e.runner, True, labels))
    return out


class TestSubLabels(Base):
    """A job's class is the class of the runner that ran it; a sub-label of a class is that class."""

    def test_a_sub_label_maps_to_its_parent_class_and_spanning_labels_to_none(self):
        pool = wide_pool()
        m = ci_queue.label_classes(pool, live_runners(pool))
        self.assertEqual(m['fast-heavy'], 'heavy')
        self.assertEqual(m['heavy'], 'heavy')
        self.assertEqual(m['light'], 'light')
        self.assertNotIn('self-hosted', m)
        self.assertNotIn('provider-alpha', m)      # carried by both classes

    def test_a_sub_label_job_counts_once_in_its_parent_class(self):
        pool = wide_pool()
        runners = live_runners(pool)
        # one job, both labels, run on a pool runner; one on a runner outside the pool asking
        # for the sub-label only; the same job id listed twice. Each is one heavy job.
        jobs = [dict(job('h1', 0, 10, labels=['self-hosted', 'heavy', 'fast-heavy']), id=1),
                dict(job('x-ephemeral', 0, 10, labels=['self-hosted', 'fast-heavy']), id=2),
                dict(job('h2', 0, 10, labels=['self-hosted', 'fast-heavy']), id=3),
                dict(job('h2', 0, 10, labels=['self-hosted', 'fast-heavy']), id=3)]
        by_name = {e.runner: e for e in pool}
        peak = ci_queue.peak_concurrent(jobs, by_name, ci_queue.label_classes(pool, runners))
        self.assertEqual(peak, {'heavy': 3})
        self.assertNotIn('fast-heavy', peak)
        # with the live runners taken (:func:`run_peaks`'s own split), the sub-label discriminates
        # the class after all — every one of these jobs asks for it, so its own key sits beside
        # the parent's at the same count, never summed with it
        self.assertEqual(ci_queue.needs_from_history([jobs] * 5, pool, runners),
                         {'heavy': 3, 'heavy[fast-heavy]': 3})

    def test_the_estimate_is_about_3_when_each_run_peaks_at_about_3_heavy(self):
        """Ten runs, half asking for ``heavy``, half for the sub-label, each 3 heavy at once (one
        run 4, one 2) over staggered stages on a 12-runner class: the estimate is 3, not 6+ — and,
        since the sub-label discriminates the live fleet, the same for its own key: the five runs
        that never ask for it count 0 there, and the p90 still lands on 3."""
        pool = wide_pool()
        runners = live_runners(pool)

        def run(k, width):
            lab = ['self-hosted', 'fast-heavy'] if k % 2 else ['self-hosted', 'heavy']
            jobs, n = [], 0
            for stage in range(3):               # three stages, one after the other
                for i in range(width):
                    n += 1
                    jobs.append(dict(job(f'h{(n % 12) + 1}', stage * 10, stage * 10 + 10,
                                         labels=lab), id=k * 100 + n))
            jobs.append(dict(job('l1', 0, 5, labels=['self-hosted', 'light']), id=k * 100 + 99))
            jobs.append(dict(job(None, conclusion='skipped', labels=lab), id=k * 100 + 98))
            return jobs

        runs = [run(k, 3) for k in range(8)] + [run(8, 4), run(9, 2)]
        self.assertEqual(ci_queue.needs_from_history(runs, pool, runners),
                         {'heavy': 3, 'heavy[fast-heavy]': 3, 'light': 1})


class TestStarvationGuard(Base):
    def test_a_pr_waiting_past_pr_wait_min_starts_on_half_its_expected_jobs(self):
        p = product(queue={'head_wait_max_min': 120})   # expects heavy 3, light 1; the head
        gh = FakeGh(busy={'h1'})                  # heavy 2 free: short of 3, but ceil(3/2) = 2
        d = self.admit(self.queue(p, gh), 'pr:task/T-0500', 'T-0500')
        self.assertFalse(d.admitted)
        self.assertIn('heavy 2 free, needs 3', self.lines[-1])
        for m in (20, 40, 44):                     # asked again each tick, keeping its place
            d = self.admit(self.queue(p, gh, minutes=m), 'pr:task/T-0500', 'T-0500')
            self.assertFalse(d.admitted)          # not yet past the default 45 minutes
        self.lines.clear()
        d = self.admit(self.queue(p, gh, minutes=46), 'pr:task/T-0500', 'T-0500')
        self.assertTrue(d.admitted)
        self.assertEqual(len(self.lines), 1)
        self.assertIn('T-0500 starts — starvation guard — waited 46m', self.lines[0])
        self.assertNotIn('pr:task/T-0500', ci_queue.load('p')['entries'])

    def test_the_guard_needs_half_free_and_holds_batch_not_at_the_ceiling(self):
        p = product(queue={'pr_wait_min': 10})
        busy2 = FakeGh(busy={'h1', 'h2'})         # heavy 1 free: below half of 3
        self.assertFalse(self.admit(self.queue(p, busy2), 'pr:a', 'T-0500').admitted)
        self.assertFalse(self.admit(self.queue(p, busy2, minutes=11), 'pr:a', 'T-0500').admitted)
        busy1 = FakeGh(busy={'h1'})
        self.assertFalse(self.admit(self.queue(p, busy1), 'batch', 'batch', kind='batch').admitted)
        self.assertFalse(self.admit(self.queue(p, busy1, minutes=11), 'batch', 'batch',
                                    kind='batch').admitted)     # a batch start is not guarded
        pc = product(cap={'ci': 2}, queue={'pr_wait_min': 10}, name='c')
        self.assertFalse(self.admit(self.queue(pc, FakeGh(inflight=2, busy={'h1', 'h2'})),
                                    'pr:c', 'T-0500').admitted)
        self.assertTrue(self.admit(self.queue(pc, FakeGh(inflight=2, busy={'h1'}), minutes=11),
                                   'pr:c', 'T-0500').admitted)  # the ceiling holds no PR
        self.assertEqual(ci_queue.pr_wait_min(p), 10)
        self.assertEqual(ci_queue.pr_wait_min(product()), 45)
        self.assertEqual(dict(ci_queue.config_problems({'queue': {'pr_wait_min': 0}})).keys(),
                         {'ci.queue.pr_wait_min'})


def waits(item, action, branch='', feature=''):
    """A feeder row (:class:`asf.feeder.rows.Row`'s fields the queue reads)."""
    import types
    return types.SimpleNamespace(item_id=item, action=action, branch=branch or f'cloud/{item}',
                                 feature_id=feature)


class TestUnblocks(Base):
    """Within a tier the entry more of ``asf next``'s rows wait on goes first; a rank, a priority
    and the starvation guard are untouched, and nothing waiting falls back to age (2026-09-27,
    the first customer: T-0374, T-0382, T-0360, T-0367 each held 3 waiting rows, F-0092's
    spec PR 2)."""

    ITEMS = TestRecordRank.ITEMS

    def setUp(self):
        super().setUp()
        ci_queue._UNBLOCKS_CACHE.clear()

    def tearDown(self):
        ci_queue._UNBLOCKS_CACHE.clear()
        super().tearDown()

    def line(self, asks, feeder):
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        for m, (key, item) in enumerate(asks):
            ci_queue.admit(p, key, 'pr', item=item, items=self.ITEMS, branch=key.split(':')[1],
                           queue=self.queue(p, busy, minutes=m, feeder=feeder))
        return ci_queue.line_order(ci_queue.load('p')['entries'])

    def test_dependents_count_the_rows_waiting_on_an_item_never_its_own_landing(self):
        rows = [waits('T-0383', 'WAITS ON T-0382'), waits('T-0384', 'WAITS ON T-0382'),
                waits('T-0038', 'WAITS ON F-0092 spec+plan on the trunk'),
                waits('T-0039', 'WAITS ON F-0092 spec+plan on the trunk'),
                waits('T-0382', 'WAITS ON landing: cloud/T-0382 PUSHED'),       # its own
                waits('T-0352', 'WAITS ON landing: PR #825 WAITING'),           # its own
                waits('T-0999', 'WAITS ON landing: PR #825 WAITING', 'cloud/T-0999'),
                waits('T-0998', 'WAITS ON landing: cloud/T-0352 PUSHED', 'cloud/T-0998'),
                waits('T-0024', 'WAITS ON writes'), waits('B-1384', 'WAITS ON session'),
                waits('T-0100', 'WAITS ON T-0100'),
                waits('T-0101', 'LAUNCH')]
        self.assertEqual(ci_queue.dependents(rows),
                         {'T-0382': 2, 'F-0092': 2, 'cloud/T-0352': 2})
        self.assertEqual(ci_queue.entry_unblocks('pr:cloud/T-0352', {'item': 'T-0352'},
                                                 ci_queue.dependents(rows)), 2)

    def test_same_rank_entries_go_by_dependents(self):
        asks = [('pr:task/T-0021', 'T-0021'), ('pr:task/T-0022', 'T-0022')]
        self.assertEqual(self.line(asks, []), ['pr:task/T-0021', 'pr:task/T-0022'])
        ci_queue.save('p', {})
        rows = [waits('T-0030', 'WAITS ON T-0022'), waits('T-0031', 'WAITS ON T-0022')]
        self.assertEqual(self.line(asks, rows), ['pr:task/T-0022', 'pr:task/T-0021'])
        entries = ci_queue.load('p')['entries']
        self.assertEqual(entries['pr:task/T-0022']['unblocks'], 2)
        self.assertNotIn('unblocks', entries['pr:task/T-0021'])

    def test_different_ranks_and_priorities_are_unaffected(self):
        rows = [waits(f'T-09{i}', 'WAITS ON T-0011') for i in range(10, 15)]
        rows += [waits(f'T-08{i}', 'WAITS ON landing: worker/x') for i in range(10, 13)]
        order = self.line([('pr:task/T-0021', 'T-0021'), ('pr:task/T-0011', 'T-0011'),
                           ('pr:worker/x', 'worker/x'), ('pr:bug/B-0031', 'B-0031')], rows)
        self.assertEqual(order, ['pr:bug/B-0031', 'pr:task/T-0021', 'pr:task/T-0011',
                                 'pr:worker/x'])

    def test_zero_dependents_falls_back_to_age(self):
        asks = [('pr:worker/old', 'worker/old'), ('pr:worker/mid', 'worker/mid'),
                ('pr:worker/new', 'worker/new')]
        self.assertEqual(self.line(asks, []), ['pr:worker/old', 'pr:worker/mid',
                                               'pr:worker/new'])
        ci_queue.save('p', {})
        rows = [waits('T-0900', 'WAITS ON landing: worker/new', 'cloud/T-0900')]
        self.assertEqual(self.line(asks, rows), ['pr:worker/new', 'pr:worker/old',
                                                 'pr:worker/mid'])

    def test_the_view_says_unblocks_and_the_pass_plans_once(self):
        root = os.path.join(self.tmp, 'record')
        os.makedirs(root)
        with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump({'items': {}}, f)
        p = env.Product('p', dict(product()._data, backlog_dir=root))
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        rows = [waits('T-0030', 'WAITS ON T-0341'), waits('T-0031', 'WAITS ON T-0341'),
                waits('T-0032', 'WAITS ON T-0341')]
        self.t0 = ci_queue._now()                 # the view reads the line as of now
        with mock.patch.object(ci_queue, 'feeder_rows', return_value=rows) as planned:
            self.admit(self.queue(p, busy), 'pr:a', 'T-0500')
            self.admit(self.queue(p, busy, minutes=1), 'pr:b', 'T-0341')
            lines = []
            with mock.patch.object(env, 'load_product', return_value=p):
                ci_queue.cmd_queue(mock.Mock(product='p', apply=False),
                                   source=ci_queue.GitHubSource(p, run=busy), out=lines.append)
        self.assertEqual(planned.call_count, 1)
        self.assertIn('1. T-0341 [pr, Task F-0001 rank 1, unblocks 3, full run', lines[-2])
        self.assertIn('2. T-0500 [pr, Task unranked, full run', lines[-1])

    def test_the_starvation_guard_is_unchanged(self):
        p = product(queue={'head_wait_max_min': 120})
        gh = FakeGh(busy={'h1'})
        rows = [waits('T-0030', 'WAITS ON T-0500')]
        self.assertFalse(self.admit(self.queue(p, gh, feeder=rows), 'pr:task/T-0500',
                                    'T-0500').admitted)
        for m in (20, 40, 44):                    # asked again each tick, keeping its place
            self.assertFalse(self.admit(self.queue(p, gh, minutes=m, feeder=rows),
                                        'pr:task/T-0500', 'T-0500').admitted)
        self.lines.clear()
        self.assertTrue(self.admit(self.queue(p, gh, minutes=46, feeder=rows), 'pr:task/T-0500',
                                   'T-0500').admitted)
        self.assertIn('T-0500 starts — starvation guard — waited 46m', self.lines[0])


if __name__ == '__main__':
    unittest.main()


def fanout_run(width=12, runners=4):
    """A run as the host reports it on a busy pool: ``gate`` on one heavy runner 0..5, then
    ``width`` heavy jobs all queued at once at 5 (``created_at``) that start only as a runner
    frees — ``runners`` at a time, each 10 minutes. The run *asks* for ``width`` heavy runners
    at once though at most ``runners`` ever ran together."""
    t = lambda m: f'2026-09-25T{10 + m // 60:02d}:{m % 60:02d}:00Z'
    jobs = [{'id': 1, 'runner_name': 'h1', 'labels': ['self-hosted', 'heavy'],
             'conclusion': 'success', 'created_at': t(0), 'started_at': t(0),
             'completed_at': t(5)}]
    for i in range(width):
        start = 5 + 10 * (i // runners)
        jobs.append({'id': 10 + i, 'runner_name': f'h{(i % 12) + 1}',
                     'labels': ['self-hosted', 'heavy'], 'conclusion': 'success',
                     'created_at': t(5), 'started_at': t(start), 'completed_at': t(start + 10)})
    jobs.append({'id': 99, 'runner_name': None, 'labels': ['self-hosted', 'heavy'],
                 'conclusion': 'skipped', 'created_at': t(5), 'started_at': t(5),
                 'completed_at': t(5)})
    return jobs


def fanout_product(queue=None, conv=None):
    ci = {'provider': 'github-actions', 'workflow': 'ci.yml',
          'pool': [{'runner': f'h{i}', 'provider': 'alpha', 'role': 'heavy'} for i in range(1, 13)]
          + [{'runner': 'l1', 'provider': 'alpha', 'role': 'light'}]}
    if queue is not None:
        ci['queue'] = queue
    data = {'repo_slug': 'o/r', 'ci': ci}
    if conv is not None:
        data['conventions'] = conv
    return env.Product('p', data)


class FanoutGh(FakeGh):
    """:class:`FakeGh` on the 12-heavy pool of :func:`fanout_product`."""

    def __call__(self, argv, **kw):
        if argv[:2] == ['gh', 'api'] and any('actions/runners' in a for a in argv):
            self.calls.append(argv)
            out = '\n'.join(json.dumps({
                'name': f'h{i}', 'id': i, 'busy': f'h{i}' in self.busy, 'status': 'online',
                'labels': [{'name': 'self-hosted'}, {'name': 'heavy'}]}) for i in range(1, 13))
            return subprocess.CompletedProcess(argv, 0, out, '')
        return super().__call__(argv, **kw)


class TestDemand(Base):
    """The estimate is the heavy runners a run *asks for* at once (its jobs queued or running
    together), not how many a contended pool happened to give it; per run type (full / light),
    at p90 over the runs."""

    def test_a_fan_out_asks_for_every_job_at_once_however_few_ran_together(self):
        pool = wide_pool()
        by_name = {e.runner: e for e in pool}
        # 12 heavy jobs queued at once after gate; the busy pool ran 4 at a time
        self.assertEqual(ci_queue.peak_concurrent(fanout_run(12, 4), by_name, {'heavy': 'heavy'}),
                         {'heavy': 12})
        self.assertEqual(ci_queue.needs_from_history([fanout_run(12, 3)] * 5, pool),
                         {'heavy': 12})
        # a job with no created_at is counted from its start, as before
        jobs = [dict(j, created_at=None) for j in fanout_run(12, 4)]
        self.assertEqual(ci_queue.peak_concurrent(jobs, by_name, {'heavy': 'heavy'}),
                         {'heavy': 4})

    def test_run_type_by_changed_paths(self):
        p = fanout_product()
        self.assertEqual(ci_queue.run_type(p, ['docs/guide/x.md', 'README.md']), 'light')
        self.assertEqual(ci_queue.run_type(p, ['docs/a/b/c.png']), 'light')
        self.assertEqual(ci_queue.run_type(p, ['docs/x.md', 'src/a.py']), 'full')
        self.assertEqual(ci_queue.run_type(p, []), 'full')          # nothing known: full
        self.assertEqual(ci_queue.run_type(p, None), 'full')
        p = fanout_product(queue={'light_paths': ['apps/site/**']})
        self.assertEqual(ci_queue.run_type(p, ['apps/site/page.tsx']), 'light')
        self.assertEqual(ci_queue.run_type(p, ['docs/x.md']), 'full')   # the list replaces
        # the product's own docs roots (the lane's docs class) are light too
        p = fanout_product(conv={'specs_dir': 'specs', 'doc_paths': ['plans/**']})
        self.assertEqual(ci_queue.run_type(p, ['specs/a.txt', 'plans/b/c.txt']), 'light')

    def test_full_and_light_runs_are_estimated_apart(self):
        pool = wide_pool()
        runs = [fanout_run(12, 3)] * 8 + [fanout_run(4, 4)] * 3
        types = ['full'] * 8 + ['light'] * 3
        self.assertEqual(ci_queue.needs_by_type(runs, types, pool),
                         {'full': {'heavy': 12}, 'light': {'heavy': 4}})
        # no light run measured: a light start is sized as a full one
        self.assertEqual(ci_queue.needs_by_type(runs[:8], types[:8], pool),
                         {'full': {'heavy': 12}, 'light': {'heavy': 12}})

    def test_the_queue_sizes_a_docs_pr_as_a_light_run_and_a_code_pr_as_a_full_one(self):
        p = fanout_product()
        # runs 1..8 full (PRs touching code), 9..10 light (docs PRs), 11 a run with no PR
        history = [fanout_run(12, 3)] * 8 + [fanout_run(4, 4)] * 2 + [fanout_run(12, 3)]
        files = {i: ['src/app.py'] for i in range(1, 9)}
        files.update({9: ['docs/a.md'], 10: ['README.md']})
        gh = FanoutGh(busy={f'h{i}' for i in range(1, 7)}, history=history, files=files,
                      listed=[{'databaseId': i, 'conclusion': 'success', 'attempt': 1}
                              for i in range(1, 12)])
        q = self.queue(p, gh, write=True)
        self.assertEqual(q.needs('ci.yml'), {'heavy': 12})
        self.assertEqual(q.needs('ci.yml', 'light'), {'heavy': 4})
        self.assertFalse(ci_queue.admit(p, 'pr:code', 'pr', item='T-0500', items=ITEMS,
                                        files=['src/app.py'], queue=q).admitted)
        self.assertIn('heavy 6 free, needs 12', self.lines[-1])
        q = self.queue(p, gh)
        d = ci_queue.admit(p, 'pr:docs', 'pr', item='T-0341', items=ITEMS,
                           files=['docs/guide.md'], queue=q)
        self.assertTrue(d.admitted, self.lines)
        # the next pass: the same run ids re-use the cached measure — no jobs or files read
        gh2 = FanoutGh(history=history, files=files, listed=gh.listed)
        q = self.queue(p, gh2, minutes=ci_queue.EXPECT_TTL_S // 60 + 1)
        self.assertEqual(q.needs('ci.yml', 'light'), {'heavy': 4})
        self.assertFalse([c for c in gh2.calls if any('/jobs' in a or '/pulls/' in a for a in c)])

    def test_the_trunk_run_is_sized_full(self):
        p = fanout_product()
        history = [fanout_run(12, 3)] * 3 + [fanout_run(4, 4)] * 3
        files = {i: ['src/a.py'] for i in (1, 2, 3)}
        files.update({i: ['docs/a.md'] for i in (4, 5, 6)})
        q = self.queue(p, FanoutGh(history=history, files=files))
        self.assertEqual(q.needs(ci_queue.workflow_for(p, 'trunk')), {'heavy': 12})

    def test_a_configured_estimate_overrides_the_measure(self):
        p = fanout_product(queue={'estimate': {'heavy': {'full': 10, 'light': 2}}})
        history = [fanout_run(12, 3)] * 3
        q = self.queue(p, FanoutGh(history=history))
        self.assertEqual(q.needs('ci.yml'), {'heavy': 10})
        self.assertEqual(q.needs('ci.yml', 'light'), {'heavy': 2})
        p = fanout_product(queue={'estimate': {'heavy': 7, 'light': {'light': 0}}})
        q = self.queue(p, FanoutGh(history=history))
        self.assertEqual(q.needs('ci.yml'), {'heavy': 7})
        self.assertEqual(q.needs('ci.yml', 'light'), {'heavy': 7})

    def test_config_problems_for_light_paths_and_estimate(self):
        ok = {'queue': {'light_paths': ['docs/**'], 'estimate': {'heavy': {'full': 12,
                                                                          'light': 4}}}}
        self.assertEqual(ci_queue.config_problems(ok), [])
        self.assertEqual(ci_queue.config_problems({'queue': {'estimate': {'heavy': 3}}}), [])
        bad = dict(ci_queue.config_problems({'queue': {
            'light_paths': 'docs/**', 'estimate': {'heavy': {'full': -1, 'huge': 3},
                                                   'light': 'x'}}}))
        self.assertEqual(sorted(bad), ['ci.queue.estimate.heavy.full',
                                       'ci.queue.estimate.heavy.huge',
                                       'ci.queue.estimate.light', 'ci.queue.light_paths'])

    def test_config_problems_for_relief_exempt_paths(self):
        self.assertEqual(ci_queue.config_problems(
            {'queue': {'relief_exempt_paths': ['ops/runners/**']}}), [])
        self.assertEqual(ci_queue.config_problems(
            {'queue': {'relief_exempt_paths': 'ops/runners/**'}}),
            [('ci.queue.relief_exempt_paths',
              "must be a list of path globs, not 'ops/runners/**'")])

    def test_relief_exempt_paths_is_the_defaults_plus_the_configured_extras(self):
        self.assertEqual(ci_queue.relief_exempt_paths(product()),
                         ['.github/workflows/**', '.github/actionlint.yaml'])
        self.assertEqual(
            ci_queue.relief_exempt_paths(product(queue={'relief_exempt_paths':
                                                         ['ops/runners/**']})),
            ['.github/workflows/**', '.github/actionlint.yaml', 'ops/runners/**'])


class TestDraft(Base):
    """A draft PR is parked by its owner: its branch never enters the line, never gets a start,
    and is never set aside as demand ahead of the starts behind it."""

    def test_a_draft_never_enters_the_line_nor_starts_and_asks_the_host_nothing(self):
        p = product()
        gh = FakeGh()
        q = self.queue(p, gh)
        d = ci_queue.admit(p, 'trunk:cloud/direct-F-0112', 'trunk', item='F-0112', items=ITEMS,
                           branch='cloud/direct-F-0112', draft=True, queue=q)
        self.assertFalse(d.admitted)
        self.assertIn('draft', d.line)
        self.assertEqual(ci_queue.load('p')['entries'], {})
        self.assertEqual(ci_queue.load('p')['started'], [])
        self.assertEqual(gh.calls, [])

    def test_a_pr_turned_draft_leaves_the_line_and_holds_nothing_behind_it(self):
        p = product()
        self.assertFalse(self.admit(self.queue(p, FakeGh(busy={'h1'})), 'pr:feat/a',
                                    'T-0341').admitted)          # Feature: 2 free, needs 3
        self.assertFalse(self.admit(self.queue(p, FakeGh(), minutes=1), 'pr:feat/b',
                                    'T-0500').admitted)          # 3 free, 3 set aside for a
        # a's owner opened it as a draft: the lane forgets its branch
        q = self.queue(p, FakeGh(), minutes=2)
        self.assertEqual(ci_queue.forget(p, 'feat/a', queue=q), 1)
        self.assertNotIn('pr:feat/a', ci_queue.load('p')['entries'])
        self.assertTrue(self.admit(self.queue(p, FakeGh(), minutes=3), 'pr:feat/b',
                                   'T-0500').admitted)
        # asking again as a draft never re-enters it
        q = self.queue(p, FakeGh(), minutes=4)
        self.assertFalse(ci_queue.admit(p, 'pr:feat/a', 'pr', item='T-0341', items=ITEMS,
                                        draft=True, queue=q).admitted)
        self.assertEqual(ci_queue.load('p')['entries'], {})

    def test_the_lane_never_asks_for_a_draft_and_forgets_it_each_pass(self):
        import types
        from unittest import mock
        from asf.harvest import lane
        self.t0 = ci_queue._now()           # the lane's own queue reads the real clock
        p = product()
        self.assertFalse(self.admit(self.queue(p, FakeGh(busy={'h1'})), 'pr:cloud/direct-F-0112',
                                    'T-0341').admitted)
        fake = types.SimpleNamespace(product=p, out=self.lines.append, ci_queue=None, items=ITEMS)
        f = {'branch': 'cloud/direct-F-0112', 'item': 'F-0112',
             'pr': {'number': 820, 'state': 'OPEN', 'draft': True}}
        with mock.patch('subprocess.run', side_effect=NoGh()):
            self.assertFalse(lane.Lane.ci_admits(fake, f, 'trunk'))
            self.assertEqual(ci_queue.load('p')['entries'], {})
        # the lane's pass forgets a draft branch's entries before it moves it
        forgot = []
        fake = types.SimpleNamespace(dry_run=False, repo=None, out=self.lines.append,
                                     ci_forget=forgot.append)
        prev = {'state': lane.PARKED, 'reason': 'PR #820 is a draft — parked by its owner',
                'head': 'a' * 40}
        lane.Lane.advance(fake, dict(f, prev=prev, head='a' * 40, mode='pr',
                                     pr=dict(f['pr'], head='a' * 40)))
        self.assertEqual([x['branch'] for x in forgot], ['cloud/direct-F-0112'])


class TestHeadStarvation(Base):
    """2026-09-26, a product: ``head T-0356 waits — heavy 0 free, needs 4`` from 18:57 to 20:42.
    Heavy runners free one at a time and the host hands each to a job of a run already
    dispatched (its own FIFO), so ``free >= need`` is never true for the head, and everything
    behind it that needs the class waits on the runners set aside for it."""

    def gh(self):
        return FanoutGh(busy={f'h{i}' for i in range(1, 12)})   # one heavy runner free

    def replay(self, p, until, step=5):
        """Every tick, the head (Feature) and an entry behind it ask again; one heavy runner is
        free at each, taken before the next tick by an in-flight run. ``{minute: admitted}``."""
        out = {}
        for m in range(0, until + 1, step):
            q = self.queue(p, self.gh(), minutes=m)
            head = self.admit(q, 'pr:task/T-0341', 'T-0341')
            behind = self.admit(q, 'pr:task/T-0500', 'T-0500')
            out[m] = (head.admitted, behind.admitted)
            if head.admitted:
                break
        return out

    def test_without_the_guard_the_head_never_starts(self):
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}, 'head_wait_max_min': 10_000})
        got = self.replay(p, 105)
        self.assertEqual(set(got.values()), {(False, False)})   # 105 minutes, nothing starts
        self.assertIn('ci queue: T-0341 waits — heavy 1 free, needs 4 (Task F-0001 rank 1, 1st in line)',
                      self.lines)

    def test_the_head_is_admitted_past_head_wait_max_min_and_not_before(self):
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}})       # default 20
        self.assertEqual(ci_queue.head_wait_max_min(p), 20)
        got = self.replay(p, 60)
        self.assertEqual([m for m, (h, _b) in got.items() if h], [25])
        self.assertTrue(all(not h for m, (h, _b) in got.items() if m <= 20))
        self.assertIn('ci queue: task/T-0341 admitted after 25 min at the head '
                      '(starvation guard; limit 20 min = the default)', self.lines)
        entries = ci_queue.load('p')['entries']
        self.assertNotIn('pr:task/T-0341', entries)
        # the next head's clock starts when it becomes the head, not when it entered the line
        self.assertEqual(entries['pr:task/T-0500'].get('head_since'),
                         ci_queue._iso(self.t0 + datetime.timedelta(minutes=25)))
        q = self.queue(p, self.gh(), minutes=40)
        self.assertFalse(self.admit(q, 'pr:task/T-0500', 'T-0500').admitted)
        q = self.queue(p, self.gh(), minutes=46)
        self.assertTrue(self.admit(q, 'pr:task/T-0500', 'T-0500').admitted)

    def test_only_the_head_by_priority_is_guarded(self):
        """An older unranked entry (a branch with no record item) behind a younger ranked Task is
        not the head: it never jumps the line, however long it has waited — and once it is the
        head, the guard still admits it after ``head_wait_max_min`` at the head."""
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}})
        self.assertFalse(self.admit(self.queue(p, self.gh()), 'pr:old', 'worker/plan-x').admitted)
        self.assertFalse(self.admit(self.queue(p, self.gh(), minutes=10), 'pr:feat',
                                    'T-0341').admitted)
        q = self.queue(p, self.gh(), minutes=25)          # the unranked one waited 25, the Task 15
        self.assertFalse(self.admit(q, 'pr:old', 'worker/plan-x').admitted)
        self.assertIn('(other, 2nd in line)', self.lines[-1])
        self.assertFalse(self.admit(q, 'pr:feat', 'T-0341').admitted)
        q = self.queue(p, self.gh(), minutes=31)
        self.assertFalse(self.admit(q, 'pr:old', 'worker/plan-x').admitted)
        self.assertTrue(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertIn('ci queue: feat admitted after 21 min at the head '
                      '(starvation guard; limit 20 min = the default)', self.lines)
        # the unranked one is the head now, its clock started at 31: no cascade
        self.assertFalse(self.admit(self.queue(p, self.gh(), minutes=35), 'pr:old',
                                    'worker/plan-x').admitted)
        # the guard stays the backstop for the lowest rank: 21 min at the head, admitted
        self.assertTrue(self.admit(self.queue(p, self.gh(), minutes=52), 'pr:old',
                                   'worker/plan-x').admitted)
        self.assertIn('ci queue: old admitted after 21 min at the head '
                      '(starvation guard; limit 20 min = the default)', self.lines)

    def test_a_queue_file_from_before_the_guard_counts_the_wait_in_line(self):
        """No head recorded yet (the file predates the guard): the head that has waited 105
        minutes is admitted on the first tick."""
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}})
        self.assertFalse(self.admit(self.queue(p, self.gh()), 'pr:task/T-0356',
                                    'T-0341').admitted)
        data = ci_queue.load('p')
        data.pop('head', None)
        for e in data['entries'].values():
            e.pop('head_since', None)
        ci_queue.save('p', data)
        for m in (25, 50, 75, 100):                     # the lane keeps asking every tick
            e = ci_queue.load('p')
            e['entries']['pr:task/T-0356']['seen'] = ci_queue._iso(
                self.t0 + datetime.timedelta(minutes=m))
            ci_queue.save('p', e)
        self.assertTrue(self.admit(self.queue(p, self.gh(), minutes=105), 'pr:task/T-0356',
                                   'T-0341').admitted)
        self.assertIn('ci queue: task/T-0356 admitted after 105 min at the head '
                      '(starvation guard; limit 20 min = the default)', self.lines)

    def test_the_status_names_the_heads_wait(self):
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}})
        self.admit(self.queue(p, self.gh()), 'pr:task/T-0341', 'T-0341')
        self.admit(self.queue(p, self.gh(), minutes=15), 'pr:task/T-0341', 'T-0341')
        src = ci_queue.GitHubSource(p, run=self.gh())
        now = self.t0 + datetime.timedelta(minutes=15)
        self.assertEqual(ci_queue.status_clause(p, now=now, source=src),
                         'ci queue 1, head T-0341 waits 15 min (15 min at the head; '
                         'admitted at 20) — heavy 1 free, needs 4 '
                         '(Task F-0001 rank 1, 1st in line)')

    def test_the_status_names_the_at_head_age_not_only_the_wait_in_line(self):
        """The defect: at minute 40 the entry behind the admitted head has been in line 40
        minutes and at the head for 15, and only the first was printed — so a head well under
        head_wait_max_min read as starved and un-admitted."""
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}})
        self.assertEqual([m for m, (h, _b) in self.replay(p, 60).items() if h], [25])
        q = self.queue(p, self.gh(), minutes=40)
        self.assertFalse(self.admit(q, 'pr:task/T-0500', 'T-0500').admitted)
        entries = ci_queue.load('p')['entries']
        self.assertEqual(entries['pr:task/T-0500'].get('head_since'),
                         ci_queue._iso(self.t0 + datetime.timedelta(minutes=25)))
        now = self.t0 + datetime.timedelta(minutes=40)
        row = ci_queue.status_clause(p, now=now, source=ci_queue.GitHubSource(p, run=self.gh()))
        self.assertEqual(row, 'ci queue 1, head T-0500 waits 40 min (15 min at the head; '
                              'admitted at 20) — heavy 1 free, needs 4 (Task unranked, '
                              '1st in line)')
        self.assertEqual(ci_queue.head_wait_max_min(p), 20)      # the threshold is the guard's
        # the number printed is the one the admission line will print
        q = self.queue(p, self.gh(), minutes=46)
        self.assertTrue(self.admit(q, 'pr:task/T-0500', 'T-0500').admitted)
        self.assertIn('admitted after 21 min at the head '
                      '(starvation guard; limit 20 min = the default)', self.lines[-1])

    def test_the_clause_names_the_products_own_threshold(self):
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}, 'head_wait_max_min': 7})
        self.admit(self.queue(p, self.gh()), 'pr:task/T-0341', 'T-0341')
        row = ci_queue.status_clause(p, now=self.t0,
                                     source=ci_queue.GitHubSource(p, run=self.gh()))
        self.assertIn('(0 min at the head; admitted at 7)', row)

    def test_a_file_from_before_the_guard_names_the_age_the_guard_will_use(self):
        p = fanout_product(queue={'estimate': {'heavy': 4, 'light': 0}})
        self.admit(self.queue(p, self.gh()), 'pr:task/T-0356', 'T-0341')
        data = ci_queue.load('p')
        data.pop('head', None)
        for e in data['entries'].values():
            e.pop('head_since', None)
        ci_queue.save('p', data)
        now = self.t0 + datetime.timedelta(minutes=15)
        row = ci_queue.status_clause(p, now=now, source=ci_queue.GitHubSource(p, run=self.gh()))
        self.assertEqual(row, 'ci queue 1, head T-0341 waits 15 min (15 min at the head; '
                              'admitted at 20) — heavy 1 free, needs 4 '
                              '(Task F-0001 rank 1, 1st in line)')

    def test_config(self):
        self.assertEqual(ci_queue.head_wait_max_min(fanout_product(
            queue={'head_wait_max_min': 7})), 7)
        self.assertEqual(ci_queue.head_wait_max_min(fanout_product()), 20)
        for bad in (0, -1, 'x', True):
            self.assertEqual(dict(ci_queue.config_problems(
                {'queue': {'head_wait_max_min': bad}})).keys(), {'ci.queue.head_wait_max_min'})
        self.assertEqual(ci_queue.config_problems({'queue': {'head_wait_max_min': 30}}), [])

    def test_a_queue_file_from_before_the_stall_watch_loads(self):
        os.makedirs(env.state_dir('p'), exist_ok=True)
        with open(ci_queue._path('p'), 'w', encoding='utf-8') as f:
            json.dump({'entries': {}, 'started': []}, f)
        data = ci_queue.load('p')
        self.assertEqual(data['stalled'], {})
        self.assertEqual(data['stalls'], [])
        self.assertEqual(ci_queue.prune(data, self.t0)['stalls'], [])


class TestStaleSweep(ReliefBase):
    """2026-09-26 22:07, a product: 45 entries in the line against 8 queued + 8 in-flight runs
    at the host — 38 of them ``rerun:<run id>`` of relief records whose PR had merged (T-0354
    sat at the head after its PR merged), whose branch head had moved on, or whose head sha
    already had a run; one branch held up to four entries. Each pass drops (a) a merged/closed
    PR's entries, (b) a draft's, (c) one whose head already has a run, one line each, and keys a
    branch's re-run by the branch: one entry per branch."""

    def prs(self):
        pr = lambda n, b, sha, state='OPEN', draft=False: {  # noqa: E731
            'number': n, 'headRefName': b, 'headRefOid': sha, 'state': state, 'isDraft': draft}
        return [pr(11, 'task/T-0341', 'a1'), pr(12, 'bug/B-0008', 'b1', 'MERGED'),
                pr(13, 'task/T-0500', 'c1', draft=True), pr(14, 'task/T-0600', 'd1'),
                pr(15, 'task/T-0700', 'e1'), pr(16, 'task/T-0800', 'f1'),
                pr(19, 'task/T-0900', 'h1'), pr(20, 'task/T-0902', 'g1', 'MERGED'),
                pr(21, 'task/T-0903', 'i1', draft=True), pr(22, 'task/T-0904', 'j1', 'CLOSED')]

    def listing(self):
        t = lambda m: self.at(self.t0 + datetime.timedelta(minutes=m))  # noqa: E731
        r = lambda i, b, sha, s='completed', c='cancelled': {  # noqa: E731
            'databaseId': i, 'status': s, 'conclusion': c, 'event': 'pull_request',
            'headBranch': b, 'headSha': sha, 'createdAt': t(-100 + i % 100)}
        return {
            'ci.yml': [{'databaseId': 900, 'status': 'completed', 'event': 'push',
                        'headBranch': 'main', 'headSha': 'f' * 40, 'createdAt': t(-30)}],
            'pr.yml': [r(300, 'task/T-0341', 'a0'), r(301, 'task/T-0341', 'a1'),
                       r(302, 'bug/B-0008', 'b1'), r(303, 'task/T-0500', 'c1'),
                       r(304, 'task/T-0600', 'd0'), r(314, 'task/T-0600', 'd1', 'queued', ''),
                       r(305, 'task/T-0700', 'e1'), r(306, 'task/T-0700', 'e1', 'in_progress', ''),
                       r(309, 'task/T-0900', 'h1', 'queued', ''),
                       r(310, 'task/T-0341', 'a1')],
            'batch.yml': [],
        }

    def sweep_gh(self, prs=None, views=None):
        gh, base = self.gh(self.listing())
        prs = self.prs() if prs is None else prs
        views = views if views is not None else {
            307: {'headBranch': 'task/T-0800', 'headSha': 'f1', 'status': 'completed',
                  'conclusion': 'cancelled'}}

        def run(argv, **kw):
            if argv[:3] == ['gh', 'pr', 'list']:
                gh.calls.append(argv)
                if prs is False:
                    return subprocess.CompletedProcess(argv, 1, '', 'unreachable')
                state = argv[argv.index('--state') + 1]
                out = [p for p in prs if state == 'all' or p['state'].lower() == state]
                return subprocess.CompletedProcess(argv, 0, json.dumps(out), '')
            if argv[:3] == ['gh', 'run', 'view']:
                gh.calls.append(argv)
                v = views.get(int(argv[3]))
                return subprocess.CompletedProcess(argv, 0 if v else 1, json.dumps(v or {}), '')
            return base(argv, **kw)
        return gh, run

    def seed_line(self):
        self.seed(self.t0)
        data = ci_queue.load('p')
        stamp = self.at(self.t0 - datetime.timedelta(minutes=90))
        rec = lambda i, item, **kw: dict({  # noqa: E731
            'id': i, 'kind': 'pr', 'item': item, 'prio': 3, 'label': 'Task', 'workflow': 'pr.yml',
            'at': stamp, 'trunk_id': 900, 'trunk_sha': 'f' * 40, 'trunk_created': stamp}, **kw)
        data['relief'] = [rec(300, 'T-0341'), rec(301, 'T-0341'), rec(302, 'B-0008'),
                          rec(303, 'T-0500'), rec(304, 'T-0600'), rec(305, 'T-0700'),
                          rec(307, 'T-0800'), rec(310, 'T-0341', branch='task/T-0341', sha='a1')]
        e = lambda item, **kw: dict({  # noqa: E731
            'kind': 'pr', 'item': item, 'prio': 3, 'label': 'Task', 'workflow': 'pr.yml',
            'run': 'full', 'since': stamp, 'seen': self.at(self.t0)}, **kw)
        data['entries'] = {f"rerun:{r['id']}": e(r['item']) for r in data['relief']}
        data['entries'].update({
            'pr:task/T-0900': e('T-0900'), 'pr:task/T-0901': e('T-0901'),
            'pr:task/T-0902': e('T-0902', sha='g1'), 'pr:task/T-0903': e('T-0903'),
            'pr:task/T-0904': e('T-0904', sha='zz'), 'rerun:999': e('gone')})
        ci_queue.save('p', data)

    def drops(self):
        return sorted(l for l in self.lines if ' drop ' in l)

    def test_each_class_is_dropped_with_one_line_and_a_branch_keeps_one_entry(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed_line()
        gh, run = self.sweep_gh()
        self.relieve(p, run)                       # no runner free: what stays is held
        data = ci_queue.load('p')
        self.assertEqual(self.drops(), sorted([
            # (a) merged or closed
            'ci queue: drop rerun:302 — bug/B-0008: PR #12 merged',
            'ci queue: drop pr:task/T-0902 — PR #20 merged',
            # (b) draft
            'ci queue: drop rerun:303 — task/T-0500: PR #13 is a draft, parked by its owner',
            'ci queue: drop pr:task/T-0903 — PR #21 is a draft, parked by its owner',
            # (c) the head moved on, or its head sha has a run already
            'ci queue: drop rerun:300 — task/T-0341: superseded, head is now a1',
            'ci queue: drop rerun:304 — task/T-0600: superseded, head is now d1',
            'ci queue: drop rerun:305 — task/T-0700: run 306 covers e1',
            'ci queue: drop pr:task/T-0900 — run 309 covers h1',
            # the same branch and sha twice: the newest record stays
            'ci queue: drop rerun:301 — task/T-0341: run 310 is its newer re-run record',
            # an entry whose relief record is gone
            'ci queue: drop rerun:999 — no relief record',
        ]))
        self.assertEqual(sorted(r['id'] for r in data['relief']), [307, 310])
        # (d): one entry per branch, keyed by the branch; a legacy key keeps its place
        self.assertEqual(sorted(data['entries']), [
            'pr:task/T-0901', 'pr:task/T-0904', 'rerun:task/T-0341', 'rerun:task/T-0800'])
        self.assertEqual(data['entries']['rerun:task/T-0800']['since'],
                         self.at(self.t0 - datetime.timedelta(minutes=90)))
        self.assertEqual(data['entries']['rerun:task/T-0800']['sha'], 'f1')
        rec = next(r for r in data['relief'] if r['id'] == 307)
        self.assertEqual((rec['branch'], rec['sha']), ('task/T-0800', 'f1'))
        # the legacy record is looked up once, then carried
        self.assertEqual(len([c for c in gh.calls if c[:3] == ['gh', 'run', 'view']]), 1)
        self.assertEqual(self.cancels(gh, 'rerun'), [])
        self.lines.clear()
        gh, run = self.sweep_gh()
        self.relieve(p, run, minutes=1)
        self.assertEqual(self.drops(), [])
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'view']])
        self.assertEqual(sorted(ci_queue.load('p')['entries']), sorted(data['entries']))

    def test_an_unreadable_pr_listing_drops_no_record(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed_line()
        gh, run = self.sweep_gh(prs=False)
        self.relieve(p, run)
        self.assertEqual(self.drops(), ['ci queue: drop rerun:999 — no relief record'])
        # none dropped: each record is kept, or re-run (the ranked T-0341 is the head now and
        # the head guard admits it after its 90 min)
        self.assertEqual(self.cancels(gh, 'rerun'), ['300'])
        self.assertEqual(len(ci_queue.load('p')['relief']), 7)

    def test_dry_run_names_the_drops_and_writes_nothing(self):
        p = self.product(mode='dry-run')
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed_line()
        before = ci_queue.load('p')
        gh, run = self.sweep_gh()
        self.relieve(p, run)
        self.assertIn('ci queue (dry-run): would drop rerun:302 — bug/B-0008: PR #12 merged',
                      self.lines)
        self.assertEqual(ci_queue.load('p'), before)

    def _one_record(self, runs, attempt=None):
        """One relief record for run 401 on open PR #31 (task/T-0490 at k1), the listing ``runs``."""
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        data = ci_queue.load('p')
        stamp = self.at(self.t0 - datetime.timedelta(minutes=2))
        rec = {'id': 401, 'kind': 'pr', 'item': 'T-0490', 'prio': 3, 'label': 'Task',
               'workflow': 'pr.yml', 'at': stamp, 'trunk_id': 900, 'trunk_sha': 'f' * 40,
               'trunk_created': stamp, 'branch': 'task/T-0490', 'sha': 'k1'}
        if attempt is not None:
            rec['attempt'] = attempt
        data['relief'] = [rec]
        ci_queue.save('p', data)
        listing = self.listing()
        listing['pr.yml'] = runs
        gh, base = self.gh(listing)
        prs = [{'number': 31, 'headRefName': 'task/T-0490', 'headRefOid': 'k1', 'state': 'OPEN',
                'isDraft': False}]

        def run(argv, **kw):
            if argv[:3] == ['gh', 'pr', 'list']:
                return subprocess.CompletedProcess(argv, 0, json.dumps(prs), '')
            return base(argv, **kw)
        self.relieve(p, run)
        return [r['id'] for r in ci_queue.load('p')['relief']]

    def run_(self, i, status='completed', conclusion='cancelled', attempt=1, minutes=-10):
        return {'databaseId': i, 'status': status, 'conclusion': conclusion,
                'event': 'pull_request', 'headBranch': 'task/T-0490', 'headSha': 'k1',
                'attempt': attempt,
                'createdAt': self.at(self.t0 + datetime.timedelta(minutes=minutes))}

    def test_the_run_just_cancelled_still_listed_queued_is_not_running_again(self):
        """2026-10-01 09:08, a product: relief cancelled PR #997's queued run and the next sweep
        read the same run, still ``queued`` while the host wound it down, as "runs again
        already" — the record went, no re-run was ever asked for, the PR sat 34 h."""
        self.assertEqual(self._one_record([self.run_(401, 'queued', '')], attempt=1), [401])
        self.assertFalse([l for l in self.lines if ' drop ' in l], self.lines)

    def test_a_later_attempt_of_the_cancelled_run_is_running_again(self):
        self.assertEqual(self._one_record([self.run_(401, 'in_progress', '', attempt=2)],
                                          attempt=1), [])
        self.assertIn('ci queue: drop rerun:401 — task/T-0490: run 401 runs again already',
                      self.lines)

    def test_an_older_green_run_on_the_same_sha_never_covers_the_cancelled_one(self):
        """2026-10-02 09:22, a product: PR #1029's heavy run (asked for by the heavy-CI label
        after review) was cancelled by relief; the sweep saw the light run of the same sha, green
        an hour earlier, as covering it and dropped the record — the gate never ran."""
        runs = [self.run_(401, attempt=1), self.run_(399, conclusion='success', minutes=-60)]
        self.assertEqual(self._one_record(runs, attempt=1), [401])
        self.assertFalse([l for l in self.lines if ' drop ' in l], self.lines)

    def test_a_newer_run_on_the_same_sha_covers_the_cancelled_one(self):
        runs = [self.run_(401), self.run_(402, 'queued', '', minutes=-1)]
        self.assertEqual(self._one_record(runs, attempt=1), [])
        self.assertIn('ci queue: drop rerun:401 — task/T-0490: run 402 covers k1', self.lines)

    def test_new_relief_records_carry_their_attempt(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        self.assertEqual(ci_queue.load('p')['relief'][0]['attempt'], 1)

    def test_new_relief_records_carry_branch_and_sha(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        rec = ci_queue.load('p')['relief'][0]
        self.assertEqual((rec['id'], rec['branch'], rec['sha']), (101, 'worker/plan-measure', 'a' * 40))

    def test_a_pr_start_records_its_head_sha_and_keeps_its_place_on_a_new_one(self):
        p = product()
        q = self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'}))
        ci_queue.admit(p, 'pr:feat/a', 'pr', item='T-0341', items=ITEMS, branch='feat/a',
                       sha='s1', queue=q)
        since = ci_queue.load('p')['entries']['pr:feat/a']['since']
        q = self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'}), minutes=5)
        ci_queue.admit(p, 'pr:feat/a', 'pr', item='T-0341', items=ITEMS, branch='feat/a',
                       sha='s2', queue=q)
        e = ci_queue.load('p')['entries']
        self.assertEqual(list(e), ['pr:feat/a'])
        self.assertEqual((e['pr:feat/a']['sha'], e['pr:feat/a']['since']), ('s2', since))


class TestHeldPrOpens(ReliefBase):
    """2026-09-27 06:18, a product: the head of the line, a ``pr:`` start the lane held (a Task,
    since 01:27), read "would start" minute after minute while nothing started it — the lane
    asks the queue once a tick, and the queue's own pass (every minute) only re-ran cancelled
    runs. Its 4 heavy counted as taken, every entry behind it waited on "heavy 0 free". The
    pass now opens each held PR the line admits (its run starts on it); a PR it cannot open is
    one loud line and leaves the line, so it never holds the runners of the starts behind."""

    def hold(self, p, branch, item, minutes=0):
        q = self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'}), minutes=minutes)
        d = ci_queue.admit(p, f'pr:{branch}', 'pr', item=item, items=ITEMS, branch=branch,
                           sha='a' * 40, queue=q)
        self.assertFalse(d.admitted)

    def apply(self, p, opened, minutes=5):
        gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=())

        def opener(product, branch, item, items):
            opened.append((branch, item))
            return opened_result.pop(0)
        opened_result = self.results
        with mock.patch.object(ci_queue, '_open_pr', opener):
            ci_queue.apply(p, source=ci_queue.GitHubSource(p, run=run), out=self.lines.append,
                           now=self.t0 + datetime.timedelta(minutes=minutes))
        return gh

    def test_the_pass_opens_the_held_pr_at_the_head_once_it_fits(self):
        p = self.product()
        self.seed(self.t0)
        self.hold(p, 'task/T-0341', 'T-0341')
        self.results, opened = [(42, '')], []
        self.apply(p, opened)
        self.assertEqual(opened, [('task/T-0341', 'T-0341')])
        data = ci_queue.load('p')
        self.assertNotIn('pr:task/T-0341', data['entries'])
        self.assertEqual([s['key'] for s in data['started']], ['pr:task/T-0341'])
        self.assertTrue(any(l.startswith('ci queue: opened PR #42 for task/T-0341 (T-0341')
                            for l in self.lines), self.lines)

    def test_a_pr_it_cannot_open_is_loud_leaves_the_line_and_the_next_one_starts(self):
        p = self.product()
        self.seed(self.t0)
        self.hold(p, 'task/T-0341', 'T-0341')
        self.hold(p, 'task/T-0500', 'T-0500', minutes=1)
        self.results, opened = [(None, 'no commits between main and task/T-0341'), (43, '')], []
        self.apply(p, opened)
        self.assertEqual(opened, [('task/T-0341', 'T-0341'), ('task/T-0500', 'T-0500')])
        data = ci_queue.load('p')
        self.assertEqual(data['entries'], {})
        # the failed start holds no runner: only the opened one counts as started
        self.assertEqual([s['key'] for s in data['started']], ['pr:task/T-0500'])
        self.assertIn('ci queue: START FAILED T-0341 (task/T-0341) — PR not opened: no commits '
                      'between main and task/T-0341; dropped from the line, the lane asks again '
                      'at its next pass', self.lines)

    def test_a_held_pr_that_does_not_fit_stays_in_line_and_nothing_opens(self):
        p = self.product()
        self.seed(self.t0)
        self.hold(p, 'task/T-0341', 'T-0341')
        gh, run = self.gh(self.runs(trunk_status='in_progress'))     # every heavy busy

        def opener(*_a):
            raise AssertionError('opened a PR that does not fit')
        with mock.patch.object(ci_queue, '_open_pr', opener):
            ci_queue.apply(p, source=ci_queue.GitHubSource(p, run=run), out=self.lines.append,
                           now=self.t0 + datetime.timedelta(minutes=5))
        self.assertIn('pr:task/T-0341', ci_queue.load('p')['entries'])


class TestAdmittedStarts(ReliefBase):
    """2026-09-27 08:5x CEST, a product: the head of the line, ``rerun:cloud/T-0141`` (a run the
    relief cancelled at 06:14Z for main run …326), read "would start (admitted after 28 min at
    the head (starvation guard))" minute after minute while nothing started it. Its relief
    record waited on the *newest* trunk run being queued — a newer push to main at 06:24Z,
    queued, not the run it was cancelled for (on its runners since 06:24) — so the pass never
    asked the line for it again, and nothing else starts a ``rerun:`` entry. A record now waits
    for its own trunk run; and every entry the line admits is started by the pass — a re-run
    (``gh run rerun``) or a fresh dispatch — or is one loud START FAILED line and leaves."""

    def cancelled_runs(self, trunk_status='queued', newer_trunk=None):
        """The runs after the relief cancelled 101, 102 and 201 for main run 900."""
        runs = self.runs(trunk_status=trunk_status)
        for r in runs['pr.yml'] + runs['batch.yml']:
            if r['databaseId'] in (101, 102, 201):
                r.update(status='completed', conclusion='cancelled')
        if newer_trunk:
            runs['ci.yml'].append(dict(runs['ci.yml'][0], databaseId=901, status='queued',
                                       headSha='e' * 40, createdAt=self.at(newer_trunk),
                                       startedAt=None))
        return runs

    def cancel_for_main(self, p):
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)
        self.assertEqual(self.cancels(gh), ['101', '102', '201'])
        self.lines.clear()

    def hold_at_head(self, key, rid=None, item=None, prio=None, label=None, waited_min=30,
                     sha='a' * 40):
        """``key`` in the line, at its head for ``waited_min`` (a record's re-run by default)."""
        data = ci_queue.load('p')
        rec = next((r for r in data['relief'] if r['id'] == rid), {})
        since = self.at(self.t0 - datetime.timedelta(minutes=waited_min))
        e = {'kind': 'pr', 'item': item or rec.get('item'), 'label': label or rec.get('label'),
             'prio': rec.get('prio', ci_queue.RANKED) if prio is None else prio,
             'workflow': 'pr.yml', 'run': 'full', 'sha': sha, 'since': since,
             'head_since': since, 'seen': self.at(self.t0)}
        if rec.get('rank'):
            e['rank'] = rec['rank']
        data['entries'][key] = e
        data['head'] = key
        ci_queue.save('p', data)

    def apply(self, p, run, minutes=1, opener=None):
        opened = []

        def default_opener(product, branch, item, items):
            opened.append((branch, item))
            return 44, ''
        with mock.patch.object(ci_queue, '_open_pr', opener or default_opener):
            ci_queue.apply(p, source=ci_queue.GitHubSource(p, run=run), out=self.lines.append,
                           now=self.t0 + datetime.timedelta(minutes=minutes))
        return opened

    @staticmethod
    def refusing(run, *verbs):
        """``run`` that refuses every ``gh <verb …>`` in ``verbs`` (argv prefixes)."""
        def wrapped(argv, **kw):
            if any(argv[1:1 + len(v)] == list(v) for v in verbs):
                return subprocess.CompletedProcess(argv, 1, '', 'HTTP 403: refused')
            return run(argv, **kw)
        return wrapped

    def test_a_record_waits_for_its_own_trunk_run_not_a_newer_queued_one(self):
        p = self.product()
        self.cancel_for_main(p)
        # main run 900 has its runners; a newer push to main (901) is queued a minute: the
        # records cancelled for 900 re-run — they never wait on 901
        gh, run = self.gh(self.cancelled_runs('in_progress', newer_trunk=self.t0 +
                                              datetime.timedelta(minutes=1)), busy=())
        self.assertEqual(self.relieve(p, run, minutes=2)[1], 2)
        self.assertEqual(self.cancels(gh, 'rerun'), ['102', '101'])
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [201])

    def test_a_record_whose_trunk_run_is_still_queued_waits(self):
        p = self.product()
        self.cancel_for_main(p)
        gh, run = self.gh(self.cancelled_runs('queued'), busy=())
        self.assertEqual(self.relieve(p, run, minutes=2)[1], 0)
        self.assertEqual(self.cancels(gh, 'rerun'), [])

    def test_an_admitted_rerun_head_starts_though_its_record_still_waits(self):
        p = self.product()
        self.cancel_for_main(p)
        self.hold_at_head('rerun:task/T-0341', rid=102)
        # main run 900 still queued, every heavy runner busy: only the head guard admits
        gh, run = self.gh(self.cancelled_runs('queued'))
        self.apply(p, run)
        self.assertEqual(self.cancels(gh, 'rerun'), ['102'])
        self.assertTrue(any(l.startswith('ci queue: START T-0341 (task/T-0341) — re-ran run 102')
                            for l in self.lines), self.lines)
        data = ci_queue.load('p')
        self.assertNotIn('rerun:task/T-0341', data['entries'])
        self.assertIn('rerun:task/T-0341', [s['key'] for s in data['started']])
        self.assertNotIn(102, [r['id'] for r in data['relief']])

    def test_a_failed_or_cancelled_latest_run_is_rerun_else_a_fresh_one_dispatched(self):
        p = self.product()
        self.cancel_for_main(p)
        self.hold_at_head('rerun:task/T-0341', rid=102)
        # 102 will not re-run; the head's newer run 107 failed: that one re-runs
        runs = self.cancelled_runs('queued')
        runs['pr.yml'].append({'databaseId': 107, 'status': 'completed', 'conclusion': 'failure',
                               'event': 'pull_request', 'headBranch': 'task/T-0341',
                               'headSha': 'a' * 40, 'createdAt': self.at(self.t0)})
        gh, run = self.gh(runs)
        refuse_102 = self.refusing(run, ('run', 'rerun', '102'))
        self.apply(p, refuse_102)
        self.assertTrue(any(l.startswith('ci queue: START T-0341 (task/T-0341) — re-ran run 107')
                            for l in self.lines), self.lines)
        # no run re-runs at all: a fresh one is dispatched on the branch
        self.cancel_for_main(p)
        self.hold_at_head('rerun:task/T-0341', rid=102)
        gh, run = self.gh(self.cancelled_runs('queued'))
        self.apply(p, self.refusing(run, ('run', 'rerun')), minutes=2)
        self.assertIn(['gh', 'workflow', 'run', 'pr.yml', '--ref', 'task/T-0341', '-R', 'o/r'],
                      gh.calls)
        self.assertTrue(any(l.startswith('ci queue: START T-0341 (task/T-0341) — dispatched')
                            for l in self.lines), self.lines)

    def test_a_start_that_fails_is_loud_leaves_the_line_and_the_pass_moves_on(self):
        p = self.product()
        self.cancel_for_main(p)
        self.hold_at_head('rerun:task/T-0341', rid=102)
        s1, label = ci_queue.priority('B-0007', ITEMS, 'bug/B-0007')
        data = ci_queue.load('p')
        data['entries']['pr:bug/B-0007'] = {
            'kind': 'pr', 'item': 'B-0007', 'label': label, 'prio': s1, 'workflow': 'pr.yml',
            'run': 'full', 'sha': 'b' * 40, 'since': self.at(self.t0),
            'seen': self.at(self.t0)}
        ci_queue.save('p', data)
        gh, run = self.gh(self.cancelled_runs('queued'))
        opened = self.apply(p, self.refusing(run, ('run', 'rerun'), ('workflow', 'run')))
        self.assertTrue(any(l.startswith('ci queue: START FAILED T-0341 (task/T-0341)')
                            for l in self.lines), self.lines)
        self.assertEqual(opened, [('bug/B-0007', 'B-0007')])
        data = ci_queue.load('p')
        self.assertNotIn('rerun:task/T-0341', data['entries'])
        self.assertNotIn('rerun:task/T-0341', [s['key'] for s in data['started']])
        self.assertNotIn(102, [r['id'] for r in data['relief']])

    def test_an_admitted_pr_whose_pr_is_open_already_starts_a_run_not_an_open(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        self.hold_at_head('pr:task/T-0341', item='T-0341', label='Task F-0001 rank 1',
                          sha='b' * 40)
        gh, run = self.gh(self.cancelled_runs('in_progress'), busy=())

        def with_pr(argv, **kw):
            if argv[:3] == ['gh', 'pr', 'list']:
                return subprocess.CompletedProcess(argv, 0, json.dumps([{
                    'number': 795, 'headRefName': 'task/T-0341', 'state': 'OPEN',
                    'isDraft': False, 'headRefOid': 'b' * 40}]), '')
            return run(argv, **kw)

        def opener(*_a):
            raise AssertionError('opened a PR that is open')
        self.apply(p, with_pr, opener=opener)
        # its head (b…) has no run: re-running 102 (on a…) would test the old head
        self.assertEqual(self.cancels(gh, 'rerun'), [])
        self.assertIn(['gh', 'workflow', 'run', 'pr.yml', '--ref', 'task/T-0341', '-R', 'o/r'],
                      gh.calls)

    def test_every_entry_the_view_says_would_start_is_started_or_fails_loudly(self):
        """The guard: what ``asf ci queue`` says would start, the pass starts — a start line
        (``START``, ``opened PR``, ``re-ran``) or a ``START FAILED`` line for each."""
        p = self.product()
        self.cancel_for_main(p)
        self.hold_at_head('rerun:task/T-0341', rid=102)
        data = ci_queue.load('p')
        for key, item in (('pr:bug/B-0007', 'B-0007'), ('pr:hotfix/db', 'hotfix/db')):
            prio, label = ci_queue.priority(item, ITEMS, key.split(':', 1)[1])
            data['entries'][key] = {'kind': 'pr', 'item': item, 'label': label, 'prio': prio,
                                    'workflow': 'pr.yml', 'run': 'full', 'sha': 'b' * 40,
                                    'since': self.at(self.t0), 'seen': self.at(self.t0)}
        data['entries']['pr:task/T-0500'] = {
            'kind': 'pr', 'item': 'T-0500', 'label': 'other', 'prio': ci_queue.OTHER,
            'workflow': 'pr.yml', 'run': 'full', 'sha': 'b' * 40, 'since': self.at(self.t0),
            'seen': self.at(self.t0)}
        ci_queue.save('p', data)
        gh, run = self.gh(self.cancelled_runs('queued'))
        now = self.t0 + datetime.timedelta(minutes=1)
        line = ci_queue.live_line(p, source=ci_queue.GitHubSource(p, run=run), now=now)
        would = [line.entries[k]['item'] for k, ok, _w in line.decisions if ok]
        self.assertEqual(sorted(would), ['B-0007', 'T-0341', 'hotfix/db'])

        def opener(product, branch, item, items):
            return (None, 'no commits') if branch == 'hotfix/db' else (45, '')
        self.apply(p, run, opener=opener)
        for item in would:
            said = [l for l in self.lines if f' {item} ' in l or f' {item})' in l
                    or f'({item},' in l]
            self.assertTrue(any('START' in l or 'opened PR' in l or 're-ran' in l
                                for l in said), (item, self.lines))


class TestOwnCadence(ReliefBase):
    """2026-09-26 22:42, a product: 7 heavy + 7 light runners idle with 45 in the line. The
    queue's pass (superseded, dedupe, relief and its re-runs) ran only inside a tick's lane pass
    — once a tick, ticks of 7–24 min, and none while an upgrade was pending. It is its own
    scheduler job now (``asf ci queue --apply``, every minute): its own lock, never the tick's,
    never skipped nor counted by the upgrade drain."""

    def test_the_pass_admits_while_a_tick_runs_and_an_upgrade_is_pending(self):
        from asf import upgrade
        from asf.tick import tick
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        self.relieve(p, run)                            # 101, 102, 201 cancelled for main
        held = tick.acquire_lock(p)                     # a tick of p is mid-health
        self.assertIsNotNone(held)
        upgrade.write_pending('a' * 40, 'other', 'p')
        try:
            self.assertTrue(upgrade.read_pending('p'))
            gh, run = self.gh(self.runs(trunk_status='in_progress'), busy=())
            self.lines.clear()
            self.assertEqual(ci_queue.apply(p, source=ci_queue.GitHubSource(p, run=run),
                                            out=self.lines.append,
                                            now=self.t0 + datetime.timedelta(minutes=2)), 0)
        finally:
            held.close()
        self.assertEqual(self.cancels(gh, 'rerun'), ['102', '101'])
        self.assertIn('ci queue: re-ran pr run 102 (T-0341, Task F-0001 rank 1) — main run 900 at '
                      'fffffffff started after waiting 24m', self.lines)

    def test_one_pass_at_a_time_the_lane_pass_leaves_it_to_a_running_one(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        lock = ci_queue.acquire_pass_lock(p)
        try:
            self.assertIsNone(ci_queue.queue_pass(p, source=ci_queue.GitHubSource(p, run=run),
                                                  out=self.lines.append, now=self.t0))
            self.assertEqual(ci_queue.apply(p, source=ci_queue.GitHubSource(p, run=run),
                                            out=self.lines.append, now=self.t0), 0)
        finally:
            lock.close()
        self.assertEqual(gh.calls, [])
        self.assertIn('ci queue: a pass of p runs already — skipped', self.lines)
        # free again: the pass runs — cancels for the starved trunk run, as the lane's did
        self.assertEqual(ci_queue.queue_pass(p, source=ci_queue.GitHubSource(p, run=run),
                                             out=self.lines.append, now=self.t0), (3, 0))

    def test_an_unqueued_product_makes_no_gh_call(self):
        self.assertEqual(ci_queue.apply(product(pool=False), source=NoGh()), 0)

    def test_a_queued_product_has_its_own_queue_clock_outside_the_drain(self):
        import re
        from asf import scheduler, upgrade
        clocks = {'main': {'steps': ['record'], 'every': '5m'}}
        queued = env.Product('p', {'repo_slug': 'o/r', 'clocks': clocks,
                                   'ci': {'provider': 'github-actions', 'workflow': 'ci.yml',
                                          'pool': pool_data()}})
        plain = env.Product('p', {'repo_slug': 'o/r', 'clocks': clocks})
        self.assertEqual([c.name for c in scheduler.clocks(plain)], ['main'])
        clock = scheduler.clocks(queued)[-1]
        self.assertEqual((clock.name, clock.interval_s), (scheduler.QUEUE_CLOCK, 60))
        with mock.patch.object(scheduler, 'snapshot_repo', return_value=None):
            job = scheduler.render(queued, clock, cfg={})
        argv = job['argv']
        self.assertEqual(argv[argv.index('ci'):], ['ci', 'queue', '--apply', '--product', 'p'])
        self.assertNotIn('tick', argv)
        self.assertIsNone(re.search(upgrade.TICK_PATTERN, ' '.join(argv)))
        # read back from the installed job as the same clock (no drift, never retired)
        label = scheduler.label_for('p', clock.name, {})
        self.assertEqual(scheduler.clock_of_plist(label, job['plist'], 'p', {}), clock)


class TestBackfill(Base):
    """2026-09-26 23:27–23:32, a product: 5 heavy and all 7 light runners idle, 26 in the line,
    the head needing 7 heavy. The fit set aside every entry ahead's whole claim, one light per
    full run: ``light 0 free, needs 1`` with 7 light idle. A later entry that fits in what is
    free once the head's claim is set aside (and those of the entries ahead that fit so too)
    starts now — ``ci queue: backfill <key> — fits in free <class> while head <key> waits``;
    the runners the head's claim needs stay its own, and the head guard stays the backstop."""

    def seed(self, full, light):
        ci_queue.save('p', {'expect': {'ci.yml': {
            'at': self.t0.strftime('%Y-%m-%dT%H:%M:%SZ'), 'needs': full, 'light': light,
            'v': ci_queue.EXPECT_VERSION}}})

    def ask(self, q, key, item, files=()):
        return ci_queue.admit(q.product, key, 'pr', item=item, items=ITEMS,
                              branch=key.split(':', 1)[1], files=files, queue=q)

    def test_idle_light_and_a_head_needing_heavy_a_light_only_entry_starts(self):
        p = product()
        self.seed({'heavy': 3, 'light': 1}, {'light': 1})
        q = self.queue(p, FakeGh(busy={'h1'}))           # heavy 2 free, light 2 free
        self.assertFalse(self.ask(q, 'pr:feat/a', 'T-0341').admitted)   # Feature: needs 3 heavy
        # a full run behind the head needs heavy the head's claim holds: it waits
        q = self.queue(p, FakeGh(busy={'h1'}), minutes=1)
        self.assertFalse(self.ask(q, 'pr:feat/c', 'T-0500').admitted)
        # the whole-line set-aside left no light (1 for the head, 1 for feat/c): a light run
        # waited on the light slot feat/c cannot use before the head starts
        q = self.queue(p, FakeGh(busy={'h1'}), minutes=2)
        d = self.ask(q, 'pr:docs/b', 'T-0500', files=['docs/guide.md'])
        self.assertTrue(d.admitted, self.lines)
        self.assertIn('ci queue: backfill pr:docs/b — fits in free light while head pr:feat/a '
                      'waits', self.lines)
        self.assertEqual(sorted(ci_queue.load('p')['entries']), ['pr:feat/a', 'pr:feat/c'])

    def test_backfill_never_takes_what_the_heads_claim_needs(self):
        p = product()
        self.seed({'heavy': 3, 'light': 2}, {'light': 1})  # the head claims both light slots
        q = self.queue(p, FakeGh(busy={'h1'}))
        self.assertFalse(self.ask(q, 'pr:feat/a', 'T-0341').admitted)
        self.assertFalse(self.ask(q, 'pr:docs/b', 'T-0500', files=['docs/guide.md']).admitted)
        self.assertFalse([l for l in self.lines if 'backfill' in l])
        # one light slot beyond the head's claim: the first light run takes it, the next finds
        # none left (what started this pass is off the free count)
        self.seed({'heavy': 3, 'light': 1}, {'light': 1})
        ci_queue.save('p', dict(ci_queue.load('p'), entries={}))
        q = self.queue(p, FakeGh(busy={'h1'}), minutes=1)
        self.assertFalse(self.ask(q, 'pr:feat/a', 'T-0341').admitted)
        self.assertTrue(self.ask(q, 'pr:docs/c', 'T-0500', files=['docs/x.md']).admitted)
        self.assertFalse(self.ask(q, 'pr:docs/d', 'T-0500', files=['docs/y.md']).admitted)
        # an entry ahead that backfills in line order keeps its place: docs/d waits behind the
        # head, a later light run is not sized ahead of it
        self.assertIn('pr:docs/d', ci_queue.load('p')['entries'])

    def test_the_head_guard_still_fires_at_20_minutes(self):
        p = product()
        self.seed({'heavy': 3, 'light': 1}, {'light': 1})
        for m in (0, 10, 19):
            q = self.queue(p, FakeGh(busy={'h1'}), minutes=m)
            self.assertFalse(self.ask(q, 'pr:feat/a', 'T-0341').admitted)
        q = self.queue(p, FakeGh(busy={'h1'}), minutes=21)
        self.assertTrue(self.ask(q, 'pr:feat/a', 'T-0341').admitted)
        self.assertIn('ci queue: feat/a admitted after 21 min at the head '
                      '(starvation guard; limit 20 min = the default)', self.lines)


class WaitLimits(Base):
    """``pr_wait_min`` and ``head_wait_max_min`` keep their declared value first; with none
    declared, the measured p50 run wall minutes — ``measured=`` (the test seam) or, in
    production, :func:`asf.ci_census.run_wall_p50_min` off the last census — clamped to each
    reader's own floor and ceiling; with no measure at all, the constant (D17). The floor is
    today's default, so no product's limit can come out shorter than it is tuned now."""

    def test_head_wait_max_min_keeps_its_declared_value(self):
        p = product(queue={'head_wait_max_min': 15})
        self.assertEqual(ci_queue.head_wait_max_min(p, measured=33), 15)

    def test_head_wait_max_min_follows_the_measured_p50(self):
        self.assertEqual(ci_queue.head_wait_max_min(product(), measured=33), 33)

    def test_head_wait_max_min_floor_never_shortens_the_tuned_default(self):
        self.assertEqual(ci_queue.head_wait_max_min(product(), measured=6),
                         ci_queue.DEFAULT_HEAD_WAIT_MAX_MIN)

    def test_head_wait_max_min_clamps_to_its_ceiling(self):
        self.assertEqual(ci_queue.head_wait_max_min(product(), measured=400),
                         ci_queue.HEAD_WAIT_CEILING)

    def test_head_wait_max_min_with_no_measure_at_all_is_the_constant(self):
        self.assertEqual(ci_queue.head_wait_max_min(product()),
                         ci_queue.DEFAULT_HEAD_WAIT_MAX_MIN)

    def test_pr_wait_min_keeps_its_declared_value(self):
        p = product(queue={'pr_wait_min': 30})
        self.assertEqual(ci_queue.pr_wait_min(p, measured=33), 30)

    def test_pr_wait_min_follows_twice_the_measured_p50(self):
        self.assertEqual(ci_queue.pr_wait_min(product(), measured=33), 66)

    def test_pr_wait_min_floor_never_shortens_the_tuned_default(self):
        self.assertEqual(ci_queue.pr_wait_min(product(), measured=6),
                         ci_queue.DEFAULT_PR_WAIT_MIN)

    def test_pr_wait_min_clamps_to_its_ceiling(self):
        self.assertEqual(ci_queue.pr_wait_min(product(), measured=400),
                         ci_queue.PR_WAIT_CEILING)

    def test_pr_wait_min_with_no_measure_at_all_is_the_constant(self):
        self.assertEqual(ci_queue.pr_wait_min(product()), ci_queue.DEFAULT_PR_WAIT_MIN)

    def test_with_no_census_file_at_all_neither_raises(self):
        p = product()
        self.assertFalse(os.path.exists(os.path.join(env.state_dir(p), ci_census.CENSUS_FILE)))
        self.assertEqual(ci_queue.pr_wait_min(p), ci_queue.DEFAULT_PR_WAIT_MIN)
        self.assertEqual(ci_queue.head_wait_max_min(p), ci_queue.DEFAULT_HEAD_WAIT_MAX_MIN)

    def test_the_production_fallback_reads_the_last_census(self):
        """PD7: with no ``measured=``, the reader falls to the p50 :func:`ci_census.refresh`
        persisted in the census file's ``measure`` block, not a stream read of its own."""
        p = product()
        path = os.path.join(env.state_dir(p), ci_census.CENSUS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'v': 1, 'measure': {'run_wall_p50_min': 33}}, f)
        self.assertEqual(ci_queue.head_wait_max_min(p), 33)
        self.assertEqual(ci_queue.pr_wait_min(p), 66)


class HeadGuardLine(Base):
    """The head guard's admission line names ``head_wait_max_min`` and where it came from, in
    each of the three source forms (PD11); and :func:`ci_queue.decide`'s verdicts over a fixed
    set of entries, needs and free counts are identical to today's for every limit value (O8) —
    the ``why`` unchanged when no source is passed proves the tuple did not move."""

    def fixture(self, waited_min):
        since = ci_queue._iso(self.t0)
        entries = {'pr:head': {'kind': 'pr', 'since': since, 'head_since': since}}
        order = ['pr:head']
        needs_of = lambda k: {'heavy': 3}
        free = {'heavy': 0}
        now = self.t0 + datetime.timedelta(minutes=waited_min)
        return order, entries, needs_of, free, now

    def test_the_why_is_todays_unchanged_when_no_source_is_passed(self):
        order, entries, needs_of, free, now = self.fixture(25)
        ok, why = ci_queue.decide('pr:head', order, entries, needs_of, free, now=now,
                                  head_wait_max_min=20)
        self.assertTrue(ok)
        self.assertEqual(why, 'admitted after 25 min at the head (starvation guard)')

    def test_the_line_names_a_declared_limit(self):
        order, entries, needs_of, free, now = self.fixture(25)
        ok, why = ci_queue.decide('pr:head', order, entries, needs_of, free, now=now,
                                  head_wait_max_min=20,
                                  head_wait_source='ci.queue.head_wait_max_min')
        self.assertTrue(ok)
        self.assertEqual(why, 'admitted after 25 min at the head '
                             '(starvation guard; limit 20 min = ci.queue.head_wait_max_min)')

    def test_the_line_names_a_measured_limit(self):
        order, entries, needs_of, free, now = self.fixture(34)
        ok, why = ci_queue.decide('pr:head', order, entries, needs_of, free, now=now,
                                  head_wait_max_min=33,
                                  head_wait_source='measured p50 run 33 min')
        self.assertTrue(ok)
        self.assertEqual(why, 'admitted after 34 min at the head '
                             '(starvation guard; limit 33 min = measured p50 run 33 min)')

    def test_the_line_names_the_default_limit(self):
        order, entries, needs_of, free, now = self.fixture(25)
        ok, why = ci_queue.decide('pr:head', order, entries, needs_of, free, now=now,
                                  head_wait_max_min=20, head_wait_source='the default')
        self.assertTrue(ok)
        self.assertEqual(why, 'admitted after 25 min at the head '
                             '(starvation guard; limit 20 min = the default)')

    def test_this_end_to_end_wiring_names_the_source_through_admit(self):
        p = product(queue={'head_wait_max_min': 15})
        gh = FakeGh(busy={'h1', 'h2', 'h3'})   # every heavy runner busy
        for m in (0, 14):
            self.assertFalse(self.admit(self.queue(p, gh, minutes=m), 'pr:head', 'T-0341',
                                        kind='pr', branch='head').admitted)
        self.assertTrue(self.admit(self.queue(p, gh, minutes=16), 'pr:head', 'T-0341', kind='pr',
                                   branch='head').admitted)
        self.assertIn('ci queue: head admitted after 16 min at the head '
                      '(starvation guard; limit 15 min = ci.queue.head_wait_max_min)',
                      self.lines)

    def test_decides_verdicts_are_identical_to_todays_for_every_limit_value(self):
        order, entries, needs_of, free, _now = self.fixture(0)
        for limit in (6, 20, 33, 66, 120, 400):
            for waited, admitted in ((limit - 1, False), (limit + 1, True)):
                now = self.t0 + datetime.timedelta(minutes=waited)
                ok, why = ci_queue.decide('pr:head', order, entries, needs_of, free, now=now,
                                          head_wait_max_min=limit)
                self.assertEqual(ok, admitted)
                self.assertEqual(why, f'admitted after {waited} min at the head '
                                      f'(starvation guard)' if admitted
                                 else 'heavy 0 free, needs 3')


class ItemOfRunTest(unittest.TestCase):
    """A PR run on a branch that names no item ranks as the record work its title carries
    (a product PR on `cloud/tm-t1`, titled "F-BILL-7, F-BILL-8, F-RUN-10 — …", was relieved as
    "other", prio 4, behind every Feature PR)."""

    ITEMS = {
        'F-0094': {'id': 'F-0094', 'type': 'feature', 'title': 'Transparent meter'},
        'T-0048': {'id': 'T-0048', 'type': 'task', 'parent': 'F-0094'},
        'S-0019': {'id': 'S-0019', 'type': 'story', 'parent': 'F-0094', 'legacy_id': 'F-BILL-7'},
        'S-0099': {'id': 'S-0099', 'type': 'story', 'legacy_id': 'F-OLD-1', 'removed': True},
    }

    def test_branch_id_still_wins(self):
        self.assertEqual(ci_queue._item_of_run('task/T-0048-x', 'F-BILL-7 — y', self.ITEMS),
                         'T-0048')

    def test_record_id_in_title(self):
        self.assertEqual(ci_queue._item_of_run('cloud/tm-t1', 'T-0048 — the catalogue', self.ITEMS),
                         'T-0048')

    def test_legacy_catalogue_row_in_title(self):
        self.assertEqual(ci_queue._item_of_run(
            'cloud/tm-t1', "F-BILL-7, F-BILL-8, F-RUN-10 — the turn's estimate", self.ITEMS),
            'S-0019')

    def test_unknown_or_removed_tokens_name_nothing(self):
        self.assertIsNone(ci_queue._item_of_run('cloud/tm-t1', 'F-OLD-1 and X-9 — misc', self.ITEMS))
        self.assertIsNone(ci_queue._item_of_run('cloud/tm-t1', None, self.ITEMS))
        self.assertIsNone(ci_queue._item_of_run('cloud/tm-t1', 'F-BILL-7', None))

    def test_ranks_as_record_work_not_other(self):
        item = ci_queue._item_of_run('cloud/tm-t1', 'F-BILL-7, F-BILL-8 — meter', self.ITEMS)
        prio, _label = ci_queue.priority(item, self.ITEMS, 'cloud/tm-t1')
        self.assertEqual(prio, ci_queue.RANKED)

    def test_held_rerun_ranks_by_its_recorded_title(self):
        rec = {'kind': 'pr', 'branch': 'cloud/tm-t1', 'item': 'cloud/tm-t1', 'label': 'other',
               'prio': ci_queue.OTHER, 'title': 'F-BILL-7, F-BILL-8, F-RUN-10 — meter'}
        prio, _label, rank = ci_queue._rerun_priority(rec, self.ITEMS)
        self.assertEqual(prio, ci_queue.RANKED)
        self.assertIsNotNone(rank)
        rec.pop('title')
        self.assertEqual(ci_queue._rerun_priority(rec, self.ITEMS)[0], ci_queue.OTHER)


class TestRefusedRerun(ReliefBase):
    """2026-09-29, a product: the queue cancelled a PR run for a main run, then logged "re-run of
    cancelled pr run … refused — tried again next tick" 944 passes in a row — ``_gh`` dropped
    ``gh``'s stderr, so no reason was ever read, and nothing counted or escalated. Each refusal
    now carries ``gh``'s reason; after :data:`RERUN_REFUSALS_MAX` a fresh run is dispatched for
    the branch head; both refused, the record is ``stuck``: one STUCK line, named on the status
    row and red on the doctor, asked again every :data:`STUCK_RETRY_S`."""

    cancelled_runs = TestAdmittedStarts.cancelled_runs
    cancel_for_main = TestAdmittedStarts.cancel_for_main
    WHY = 'run 102 cannot be rerun; This workflow is already running'

    def refusing(self, run, *verbs):
        def wrapped(argv, **kw):
            self.argvs.append(argv)
            if any(argv[1:1 + len(v)] == list(v) for v in verbs):
                return subprocess.CompletedProcess(argv, 1, '', f'X\n{self.WHY}\n')
            return run(argv, **kw)
        return wrapped

    def setUp(self):
        super().setUp()
        self.argvs = []                     # every argv, refused ones too

    def dispatches(self, _gh=None):
        return [c for c in self.argvs if c[:3] == ['gh', 'workflow', 'run']]

    def test_a_refusal_is_logged_with_its_reason_and_counted(self):
        p = self.product()
        self.cancel_for_main(p)
        gh, run = self.gh(self.cancelled_runs('in_progress'), busy=())
        self.relieve(p, self.refusing(run, ('run', 'rerun', '102')), minutes=2)
        self.assertIn(f'ci queue: re-run of cancelled pr run 102 (T-0341) refused '
                      f'(1/{ci_queue.RERUN_REFUSALS_MAX}) — {self.WHY}; tried again next tick',
                      self.lines)
        rec = next(r for r in ci_queue.load('p')['relief'] if r['id'] == 102)
        self.assertEqual((rec['refusals'], rec['refused_why']), (1, self.WHY))

    def test_a_rerun_that_goes_through_after_a_refusal_is_reported(self):
        p = self.product()
        self.cancel_for_main(p)
        gh, run = self.gh(self.cancelled_runs('in_progress'), busy=())
        self.relieve(p, self.refusing(run, ('run', 'rerun', '102')), minutes=2)
        self.lines.clear()
        self.relieve(p, run, minutes=3)
        self.assertTrue(any(l.startswith('ci queue: re-ran pr run 102 (T-0341') and
                            l.endswith('after 1 refused') for l in self.lines), self.lines)
        self.assertNotIn(102, [r['id'] for r in ci_queue.load('p')['relief']])

    def test_the_third_refusal_dispatches_a_fresh_run_of_the_branch_head(self):
        p = self.product()
        self.cancel_for_main(p)
        gh, run = self.gh(self.cancelled_runs('in_progress'), busy=())
        refuse = self.refusing(run, ('run', 'rerun', '102'))
        for i in range(ci_queue.RERUN_REFUSALS_MAX):   # passes past PICKUP_S apart
            self.relieve(p, refuse, minutes=2 + 5 * i)
        self.assertEqual(self.cancels(gh, 'rerun').count('102'), 0)  # every one refused
        self.assertEqual([c for c in self.argvs if c[:4] == ['gh', 'run', 'rerun', '102']],
                         [['gh', 'run', 'rerun', '102', '-R', 'o/r']] * ci_queue.RERUN_REFUSALS_MAX)
        self.assertEqual(self.dispatches(gh),
                         [['gh', 'workflow', 'run', 'pr.yml', '--ref', 'task/T-0341', '-R', 'o/r']])
        self.assertTrue(any(l.startswith(f'ci queue: re-run of cancelled pr run 102 (T-0341) '
                                         f'refused {ci_queue.RERUN_REFUSALS_MAX} times — last: '
                                         f'{self.WHY}; dispatched a fresh pr.yml run')
                            for l in self.lines), self.lines)
        self.assertNotIn(102, [r['id'] for r in ci_queue.load('p')['relief']])

    def test_both_refused_is_a_named_stuck_failure_not_a_silent_loop(self):
        p = self.product()
        self.cancel_for_main(p)
        gh, run = self.gh(self.cancelled_runs('in_progress'), busy=())
        refuse = self.refusing(run, ('run', 'rerun', '102'), ('workflow', 'run'))
        last = 2 + 5 * (ci_queue.RERUN_REFUSALS_MAX - 1)
        for m in range(2, last + 1, 5):                     # passes past PICKUP_S apart
            self.relieve(p, refuse, minutes=m)
        stuck = [l for l in self.lines if l.startswith('ci queue: STUCK pr run 102 (T-0341)')]
        self.assertEqual(len(stuck), 1, self.lines)
        self.assertIn(self.WHY, stuck[0])
        self.assertIn('fresh run refused too', stuck[0])
        now = self.t0 + datetime.timedelta(minutes=last + 5)
        named = ci_queue.stuck_lines(p, now=now)
        self.assertEqual(len(named), 1)
        self.assertTrue(named[0].startswith('CI START STUCK pr run 102 (T-0341) on task/T-0341'))
        self.assertIn((True, False, named[0]), ci_queue.runner_rows(p, now=now))
        # within STUCK_RETRY_S: no gh write at all, no new line
        n_calls, self.lines[:] = len(self.argvs), []
        self.relieve(p, refuse, now=now)
        writes = [c for c in self.argvs[n_calls:]
                  if c[:3] in (['gh', 'run', 'rerun'], ['gh', 'workflow', 'run'])]
        self.assertEqual(writes, [])
        self.assertFalse([l for l in self.lines if '102' in l], self.lines)
        # past it: asked again (the fresh dispatch), still no second STUCK line
        later = now + datetime.timedelta(seconds=ci_queue.STUCK_RETRY_S + 60)
        self.relieve(p, refuse, now=later)
        self.assertTrue(len(self.dispatches(gh)) >= 2, self.lines)
        self.assertFalse([l for l in self.lines if 'STUCK' in l], self.lines)
        # the dispatch goes through at last: the record leaves, the status row is clear
        self.relieve(p, self.refusing(run, ('run', 'rerun', '102')),
                     now=later + datetime.timedelta(seconds=ci_queue.STUCK_RETRY_S + 60))
        self.assertNotIn(102, [r['id'] for r in ci_queue.load('p')['relief']])
        self.assertEqual(ci_queue.stuck_lines(p), [])

    def test_gh_try_names_the_reason(self):
        p = self.product()

        def boom(argv, **_kw):
            raise FileNotFoundError('gh')

        def slow(argv, **_kw):
            raise subprocess.TimeoutExpired(argv, 30)
        self.assertEqual(ci_queue.GitHubSource(p, run=boom).gh_try(['x'])[0], None)
        self.assertIn('gh did not run', ci_queue.GitHubSource(p, run=boom).gh_try(['x'])[1])
        self.assertIn('timed out', ci_queue.GitHubSource(p, run=slow).gh_try(['x'])[1])
        self.assertEqual(ci_queue.GitHubSource(p, run=DeadGh()).gh_try(['x']),
                         (None, 'unreachable'))


class TestPriorityBatch(Base):
    """A batch holding an ``asf land --priority`` request — or any batch once the trunk has stood
    still past half ``ci.trunk_stall_hours`` — gets runner priority: it starts at trunk priority,
    PR starts behind it are held until its jobs have runners (no guard lets one past), relief
    never cancels its run, and the re-run of its cancelled run asks at trunk priority."""
    WHY = 'holds asf land --priority #1022'

    def start_batch(self, p, gh, sha='s' * 40):
        q = self.queue(p, gh)
        d = ci_queue.admit(p, 'batch:ci/gate-manifest', 'batch', item='PR-1022', items=ITEMS,
                           branch='ci/gate-manifest', queue=q, sha=sha,
                           run_branch='batch/20261002-1022', urgent=self.WHY)
        return q, d

    def test_a_priority_batch_starts_at_once_and_holds_pr_starts_until_its_jobs_have_runners(self):
        p = product()
        _q, d = self.start_batch(p, FakeGh(busy={'h1', 'h2', 'h3'}))
        self.assertTrue(d.admitted)                       # no runner free: it starts even so
        self.assertIn('batch/20261002-1022 starts first — holds asf land --priority #1022',
                      self.lines[-1])
        rec, = ci_queue.load('p')['started']
        self.assertEqual((rec['branch'], rec['urgent']), ('batch/20261002-1022', self.WHY))
        # 10 min on — past the 3-min pickup window — its runners are still set aside, and 35 min
        # on the head guard (20 min at the head) still does not let a PR past it
        for minutes in (10, 35):
            d = self.admit(self.queue(p, FakeGh(), minutes=minutes), 'pr:task/T-0500', 'T-0500')
            self.assertFalse(d.admitted)
            self.assertIn('runners set aside for priority batch batch/20261002-1022',
                          self.lines[-1])
        # its hold ends at URGENT_HOLD_S at the latest: nothing starves for good
        d = self.admit(self.queue(p, FakeGh(), minutes=50), 'pr:task/T-0500', 'T-0500')
        self.assertTrue(d.admitted)

    def test_the_hold_ends_once_its_jobs_are_on_runners(self):
        p = product()
        self.start_batch(p, FakeGh(), sha='s1')
        gh = FakeGh(busy={'h1', 'h2', 'h3', 'l1'},
                    running=[(9, 'batch/20261002-1022', 's1', ['h1', 'h2', 'h3', 'l1'])])
        q = self.queue(p, gh, minutes=10)
        q.free(keys={'heavy'})
        self.assertEqual(q.data['started'], [])           # placed: the claim is the busy count
        self.assertEqual(q._urgent_left, '')

    def test_an_ordinary_batch_start_still_waits_its_turn(self):
        p = product()
        q = self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'}))
        d = ci_queue.admit(p, 'batch:x', 'batch', item='x', items=ITEMS, queue=q)
        self.assertFalse(d.admitted)

    def test_an_urgent_record_is_pruned_at_the_hold_not_the_pickup_window(self):
        at = '2026-09-25T12:00:00Z'
        data = {'entries': {}, 'started': [{'key': 'a', 'at': at}, {'key': 'b', 'at': at,
                                                                    'urgent': self.WHY}]}
        kept = ci_queue.prune(data, self.t0 + datetime.timedelta(minutes=10))['started']
        self.assertEqual([s['key'] for s in kept], ['b'])
        kept = ci_queue.prune(data, self.t0 + datetime.timedelta(minutes=46))['started']
        self.assertEqual(kept, [])

    def test_a_cancelled_priority_batch_run_is_rerun_at_trunk_priority(self):
        rec = {'kind': 'batch', 'branch': 'batch/x', 'id': 5}
        p = product()
        with mock.patch.object(ci_queue, 'urgent_batch', return_value=self.WHY):
            self.assertEqual(ci_queue._rerun_priority(rec, ITEMS, p)[:2],
                             (ci_queue.TRUNK, 'batch, priority'))
        with mock.patch.object(ci_queue, 'urgent_batch', return_value=''):
            self.assertEqual(ci_queue._rerun_priority(rec, ITEMS, p)[0], ci_queue.OTHER)


class TestUrgentBatch(Base):
    def product(self):
        repo = os.path.join(self.tmp, 'repo')
        os.makedirs(repo, exist_ok=True)
        return env.Product('p', {'repo_dir': repo, 'repo_slug': 'o/r', 'conventions': {
            'merge': 'queue', 'merge_queue': {'ref_prefix': 'batch/'},
            'ci': {'trunk_stall_hours': 4}}})

    def write(self, name, data):
        with open(os.path.join(env.state_dir('p'), name), 'w', encoding='utf-8') as fh:
            json.dump(data, fh)

    def test_a_batch_holding_a_priority_request(self):
        p = self.product()
        self.write('merge-queue.json', {'batches': [
            {'ref': 'batch/a', 'sha': 'a', 'members': [{'pr': 7, 'branch': 'x'}]},
            {'ref': 'batch/b', 'sha': 'b', 'members': [{'pr': 1022, 'branch': 'ci/g',
                                                        'priority': True}]}]})
        self.assertEqual(ci_queue.urgent_batch(p, 'batch/b'), 'holds asf land --priority #1022')
        self.assertEqual(ci_queue.urgent_batch(p, 'batch/a'), '')
        self.assertEqual(ci_queue.urgent_batch(p, 'cloud/T-1'), '')     # not a batch ref
        self.assertEqual(ci_queue.urgent_batch(
            p, 'batch/new', members=[{'pr': 9, 'priority': True}]), 'holds asf land --priority #9')

    def test_any_batch_once_the_trunk_is_still_past_half_the_stall_limit(self):
        p = self.product()
        self.write('merge-queue.json', {'batches': [
            {'ref': 'batch/a', 'sha': 'a', 'members': [{'pr': 7, 'branch': 'x'}]}]})
        now = self.t0
        self.write('trunk-watch.json', {'moved_at': now.timestamp() - 1.5 * 3600})
        self.assertEqual(ci_queue.urgent_batch(p, 'batch/a', now=now), '')       # 1.5h < 2h
        self.write('trunk-watch.json', {'moved_at': now.timestamp() - 2.5 * 3600})
        self.assertIn('trunk still 2.5h (> half of 4h',
                      ci_queue.urgent_batch(p, 'batch/a', now=now))


class TestPriorityBatchRelief(ReliefBase):
    def test_relief_never_cancels_a_priority_batch_run(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.seed(self.t0)
        gh, run = self.gh(self.runs())
        with mock.patch.object(ci_queue, 'urgent_batch', return_value='holds asf land --priority #1'):
            self.relieve(p, run)
        self.assertNotIn('201', self.cancels(gh))
        self.assertTrue(any('relief: exempt main — priority batch' in l for l in self.lines),
                        self.lines)
