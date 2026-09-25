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

from asf import ci_queue, env
from asf.harvest import deploy


def pool_data():
    return [
        {'runner': 'h1', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'h2', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'h3', 'provider': 'beta', 'role': 'heavy'},
        {'runner': 'l1', 'provider': 'alpha', 'role': 'light', 'slots': 2},
    ]


def product(pool=True, cap=None, queue=None, name='p'):
    ci = {'provider': 'github-actions', 'workflow': 'ci.yml'}
    if pool:
        ci['pool'] = pool_data()
    if queue is not None:
        ci['queue'] = queue
    data = {'repo_slug': 'o/r', 'ci': ci}
    if cap is not None:
        data['capacity'] = cap
    return env.Product(name, data)


class FakeGh:
    """``subprocess.run`` for ``gh``: runners, a workflow's run history, its jobs, runs in flight.
    Every argv is logged."""

    def __init__(self, busy=(), offline=(), history=None, inflight=0, listed=None):
        self.busy, self.offline = set(busy), set(offline)
        #: the completed runs ``gh run list`` names (default: every history run, a success)
        self.listed = listed
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
            out = '\n'.join(json.dumps({
                'name': r['runner'], 'id': i, 'busy': r['runner'] in self.busy,
                'status': 'offline' if r['runner'] in self.offline else 'online',
                'labels': [{'name': 'self-hosted', 'type': 'read-only'},
                           {'name': r['role'], 'type': 'custom'}]})
                for i, r in enumerate(pool_data()))
        elif argv[:3] == ['gh', 'run', 'list'] and '--status' in argv:
            out = json.dumps(self.listed if self.listed is not None else [
                {'databaseId': i, 'conclusion': 'success', 'attempt': 1}
                for i in range(1, len(self.history) + 1)])
        elif argv[:3] == ['gh', 'run', 'list']:
            out = str(self.inflight)
        elif argv[:2] == ['gh', 'api'] and any('/jobs' in a for a in argv):
            run_id = int(next(a for a in argv if '/jobs' in a).split('/runs/')[1].split('/')[0])
            out = '\n'.join(json.dumps(j) for j in self.history[run_id - 1])
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

    def test_the_median_over_the_runs_capped_at_the_class(self):
        pool = self.pool()
        # three of ten runs peak at 3 heavy, seven at 2: p90 said 3, the median is 2
        runs = [staged_run(stage2=2)] * 7 + [staged_run(stage2=3)] * 3
        self.assertEqual(ci_queue.needs_from_history(runs, pool)['heavy'], 2)
        # one outlier at 3 never lifts it
        runs = [staged_run(stage2=2)] * 9 + [staged_run(stage2=3)]
        self.assertEqual(ci_queue.needs_from_history(runs, pool)['heavy'], 2)
        # an even split rounds up: 2 and 3 → 3
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
        for field in ('runner_name', 'conclusion', 'started_at', 'completed_at', 'run_attempt'):
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

class TestAdmission(Base):
    def test_holds_until_the_class_has_the_runners_the_run_needs(self):
        p = product()
        d = self.admit(self.queue(p, FakeGh(busy={'h1', 'h2'})), 'pr:task/T-0500', 'T-0500')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: T-0500 waits — heavy 1 free, needs 3 '
                                      '(Task, 1st in line)'])
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
    def test_s1_then_trunk_then_customer_features_then_the_rest(self):
        self.assertEqual(ci_queue.priority('B-0007', ITEMS), (0, 'S1'))
        self.assertEqual(ci_queue.priority('B-0007', ITEMS, kind='trunk'), (0, 'S1'))
        self.assertEqual(ci_queue.priority('T-0500', ITEMS, branch='hotfix/x'), (0, 'hotfix'))
        self.assertEqual(ci_queue.priority('T-0500', ITEMS, kind='trunk'), (1, 'trunk'))
        self.assertEqual(ci_queue.priority('T-0341', ITEMS), (2, 'Feature'))
        self.assertEqual(ci_queue.priority('B-0008', ITEMS), (3, 'S2'))
        cust = env.Product('p', {'customer_paths': ['web/']})
        items = dict(ITEMS, **{'F-0001': {'id': 'F-0001', 'type': 'feature'}})
        self.assertEqual(ci_queue.priority('T-0341', items, files=['web/a.ts'], product=cust),
                         (2, 'Feature'))
        self.assertEqual(ci_queue.priority('T-0341', items, files=['lib/a.py'], product=cust)[0], 3)

    def test_the_line_serves_priority_first_and_an_entry_ahead_keeps_its_runners(self):
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        self.admit(self.queue(p, busy), 'pr:old', 'B-0008')             # S2, oldest
        self.admit(self.queue(p, busy, minutes=1), 'pr:feat', 'T-0341')  # Feature
        self.admit(self.queue(p, busy, minutes=4), 'pr:new', 'T-0500')  # other, newest
        entries = ci_queue.load('p')['entries']
        self.assertEqual(ci_queue.line_order(entries), ['pr:feat', 'pr:old', 'pr:new'])
        self.assertEqual(self.lines[-1], 'ci queue: T-0500 waits — heavy 0 free, needs 3, '
                                         'light 0 free, needs 1 (Task, 3rd in line)')
        # runners come free: the S2 asks first, but the Feature ahead of it is owed them
        q = self.queue(p, FakeGh(), minutes=5)
        self.assertFalse(self.admit(q, 'pr:old', 'B-0008').admitted)
        self.assertIn('(S2, 2nd in line)', self.lines[-1])
        self.assertTrue(self.admit(q, 'pr:feat', 'T-0341').admitted)

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


class TestSuperseded(Base):
    RUNS = [
        {'databaseId': 1, 'status': 'in_progress', 'createdAt': '2026-09-25T19:00:00Z',
         'headSha': 'a' * 40},
        {'databaseId': 2, 'status': 'queued', 'createdAt': '2026-09-25T19:04:00Z',
         'headSha': 'b' * 40},
        {'databaseId': 3, 'status': 'queued', 'createdAt': '2026-09-25T19:10:00Z',
         'headSha': 'c' * 40},
        {'databaseId': 4, 'status': 'queued', 'createdAt': '2026-09-25T19:20:00Z',
         'headSha': 'd' * 40},
        {'databaseId': 0, 'status': 'completed', 'createdAt': '2026-09-25T18:00:00Z',
         'headSha': 'e' * 40},
    ]

    def gh(self, runs):
        gh = FakeGh()
        base = gh.__call__

        def run(argv, **kw):
            if argv[:3] == ['gh', 'run', 'list'] and '--event' in argv:
                gh.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(runs), '')
            return base(argv, **kw)
        return gh, run

    def test_older_queued_trunk_runs_are_cancelled_keeping_the_newest(self):
        p = product()
        gh, run = self.gh(self.RUNS)
        n = ci_queue.cancel_superseded(p, source=ci_queue.GitHubSource(p, run=run),
                                       out=self.lines.append)
        self.assertEqual(n, 2)
        cancels = [c[3] for c in gh.calls if c[:3] == ['gh', 'run', 'cancel']]
        self.assertEqual(cancels, ['2', '3'])  # the running 1 finishes, the newest 4 stays
        self.assertEqual(len(self.lines), 2)
        self.assertEqual(self.lines[0], 'ci queue: cancelled superseded main run 2 (ci.yml at '
                                        'bbbbbbbbb) — run 4 at ddddddddd judges it')
        listed = next(c for c in gh.calls if '--event' in c)
        self.assertEqual(listed[listed.index('--branch') + 1], 'main')
        self.assertEqual(listed[listed.index('--event') + 1], 'push')

    def test_dry_run_names_them_and_cancels_nothing(self):
        p = product(queue={'mode': 'dry-run'})
        gh, run = self.gh(self.RUNS)
        self.assertEqual(ci_queue.cancel_superseded(
            p, source=ci_queue.GitHubSource(p, run=run), out=self.lines.append), 0)
        self.assertFalse([c for c in gh.calls if c[:3] == ['gh', 'run', 'cancel']])
        self.assertEqual(len(self.lines), 2)
        self.assertTrue(all('would cancel' in l for l in self.lines))

    def test_one_live_run_or_no_pool_cancels_nothing(self):
        gh, run = self.gh(self.RUNS[:1] + self.RUNS[4:])
        p = product()
        self.assertEqual(ci_queue.cancel_superseded(p, source=ci_queue.GitHubSource(p, run=run)),
                         0)
        self.assertEqual(ci_queue.cancel_superseded(product(pool=False), source=NoGh()), 0)


class TestTrunkRelief(Base):
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
            'pr.yml': [pr(101, 'queued', 'bug/B-0008', -60),       # S2: cancelled first
                       pr(102, 'queued', 'task/T-0341', -50),      # Feature: second
                       pr(103, 'queued', 'bug/B-0007', -40),       # S1: never
                       pr(104, 'queued', 'hotfix/db', -35),        # hotfix: never
                       pr(105, 'in_progress', 'task/T-0500', -90),  # started: never
                       pr(106, 'queued', 'task/T-0500', -5)],      # behind the trunk run
            'batch.yml': [{'databaseId': 201, 'status': 'queued', 'event': 'workflow_dispatch',
                           'headBranch': 'main', 'headSha': 'f' * 40, 'createdAt': t(-45)}],
        }

    def gh(self, runs, busy=('h1', 'h2', 'h3')):
        gh = FakeGh(busy=busy)
        base = gh.__call__

        def run(argv, **kw):
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
        self.assertEqual(self.lines[0], 'ci queue: cancelled queued pr run 101 (B-0008, S2) — '
                                        'main run 900 at fffffffff has waited 25m for runners')
        self.assertEqual(len(self.lines), 3)
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [101, 102, 201])
        # the next tick with the trunk still queued cancels nothing more: the runs are gone
        gh, run = self.gh({k: [r for r in v if r['databaseId'] not in (101, 102, 201)]
                           for k, v in self.runs().items()})
        self.assertEqual(self.relieve(p, run, minutes=1), (0, 0))

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
        self.assertIn('ci queue: re-ran pr run 102 (T-0341, Feature) — main run 900 at '
                      'fffffffff started after waiting 24m', self.lines)
        self.assertIn('ci queue: batch waits — heavy 1 free, needs 2 (batch, 1st in line)', self.lines)
        self.assertEqual([r['id'] for r in ci_queue.load('p')['relief']], [201])
        gh, run = self.gh(self.runs(trunk_status='completed'), busy=())
        self.assertEqual(self.relieve(p, run, minutes=10), (0, 1))
        self.assertEqual(self.cancels(gh, 'rerun'), ['201'])
        self.assertEqual(ci_queue.load('p')['relief'], [])

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


class TestCeiling(Base):
    def test_capacity_ci_is_a_hard_ceiling_above_the_queue(self):
        p = product(cap={'ci': 2})
        d = self.admit(self.queue(p, FakeGh(inflight=2)), 'pr:a', 'T-0500')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: T-0500 waits — at the ci ceiling (2 runs in '
                                      'flight; batch and PR starts below 2) (Task, 1st in line)'])
        q = self.queue(p, FakeGh(inflight=1), minutes=1)
        self.assertTrue(self.admit(q, 'pr:a', 'T-0500').admitted)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)  # 1 + 1 admitted


    def test_s1_hotfix_trunk_and_deploy_starts_are_exempt_from_ceiling_and_fit(self):
        """PR runs fill the ceiling (4/4): a trunk run ranked right after the S1 still starts;
        a batch and an ordinary or Feature PR wait at the ceiling."""
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
        q = self.queue(p, FakeGh(inflight=4), minutes=40)
        self.assertFalse(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)
        self.assertTrue(all('at the ci ceiling' in l for l in self.lines))
        # exempt from the runner fit too: the host queues its jobs
        self.lines.clear()
        q = self.queue(p, FakeGh(inflight=4, busy={'h1', 'h2'}), minutes=50)
        self.assertTrue(self.admit(q, 'trunk:t2', 'T-0500', kind='trunk').admitted)
        self.assertEqual(self.lines, [])

    def test_ceiling_applies_to_batch_and_ordinary_pr_starts_only(self):
        ok = ci_queue.ceiling_applies
        self.assertTrue(ok({'kind': 'pr', 'prio': ci_queue.OTHER}))
        self.assertTrue(ok({'kind': 'pr', 'prio': ci_queue.FEATURE}))
        self.assertTrue(ok({'kind': 'batch', 'prio': ci_queue.OTHER}))
        self.assertFalse(ok({'kind': 'pr', 'prio': ci_queue.S1}))
        self.assertFalse(ok({'kind': 'trunk', 'prio': ci_queue.TRUNK}))
        self.assertFalse(ok({'kind': 'deploy', 'prio': ci_queue.OTHER}))


class TestOneCount(Base):
    """The status row and the queue read one in-flight count, from one function."""

    def test_the_queue_and_the_row_read_the_same_function_and_argv(self):
        from asf import capacity
        from unittest import mock
        p = product(cap={'ci': 4})
        gh = FakeGh(inflight=5)
        self.assertEqual(ci_queue.GitHubSource(p, run=gh).inflight(), 5)
        with mock.patch('subprocess.run', side_effect=FakeGh(inflight=5)) as run:
            self.assertEqual(capacity.CiRuns().read(p), 5)
        self.assertEqual(run.call_args[0][0], gh.calls[-1])
        self.assertEqual(capacity.ci_inflight_text(5), '5 runs in flight')
        self.assertIn('not completed', capacity.CI_INFLIGHT_WHAT)

    def test_a_ceiling_hold_in_the_row_names_the_rows_own_count(self):
        """The queue held a PR at 4/4; the row's count is now 5: the row names 5, never 4."""
        from unittest import mock
        from asf.views import status
        p = product(cap={'ci': 4})
        self.t0 = ci_queue._now()   # the row reads the file as of now
        self.assertFalse(self.admit(self.queue(p, FakeGh(inflight=4)), 'pr:a', 'T-0500').admitted)
        self.assertIn('(4 runs in flight;', self.lines[-1])
        with mock.patch('subprocess.run', side_effect=FakeGh(inflight=5)):
            cell = status.capacity_cell({}, p)
        self.assertIn('ci 5 runs in flight (batch and PR starts below 4 — they wait)', cell)
        self.assertIn('head T-0500 waits — at the ci ceiling (5 runs in flight; batch and PR '
                      'starts below 4) (Task, 1st in line)', cell)
        self.assertNotIn('4 runs in flight', cell)
        self.assertNotIn('4/4', cell)
        # the count dropped below the ceiling: the head is not said to be at it
        with mock.patch('subprocess.run', side_effect=FakeGh(inflight=2)):
            cell = status.capacity_cell({}, p)
        self.assertIn('ci 2 runs in flight', cell)
        self.assertNotIn('at the ci ceiling', cell)


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
                                      'needs 3 (Task, 1st in line)'])
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
                         'ci queue 2, head T-0341 waits — heavy 0 free, needs 3 '
                         '(Feature, 1st in line)')

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
            self.assertIn(f'1. T-0341 [pr, Feature, since', view[2])
            self.assertTrue(view[2].endswith(said), view[2])
            row = ci_queue.status_clause(p, source=self.src(p, gh))
            self.assertEqual(row, f"ci queue 1, head T-0341 {said.replace('waits:', 'waits —')} "
                                  f"(Feature, 1st in line)")
            self.assertNotIn('as of tick', row)

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
        self.assertTrue(row.startswith('ci queue 1, head T-0500 waits — heavy 0 free'), row)
        self.assertNotIn('dry-run', row)

    def test_an_unreadable_host_shows_the_snapshot_dated(self):
        p = product()
        self.t0 = ci_queue._now()
        self.admit(self.queue(p, FakeGh(busy={'h1', 'h2', 'h3'})), 'pr:a', 'T-0341')
        stamp = datetime.datetime.fromtimestamp(
            os.path.getmtime(ci_queue._path('p'))).strftime('%H:%M')
        self.assertEqual(ci_queue.status_clause(p, source=self.src(p, DeadGh())),
                         f'ci queue 1 (as of tick {stamp}), head T-0341 waits — heavy 0 free, '
                         f'needs 3 (Feature, 1st in line)')

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
        self.assertIn('head T-0341 waits — heavy 0 free, needs 2 (Feature, 1st in line)', row)
        # the snapshot, when the host is unreadable, re-states the hold from the same estimate
        row = ci_queue.status_clause(p, now=self.t0, source=self.src(p, DeadGh()))
        self.assertIn('head T-0341 waits — heavy 0 free, needs 2 (Feature, 1st in line)', row)
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


if __name__ == '__main__':
    unittest.main()
