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

    def __init__(self, busy=(), offline=(), history=None, inflight=0, listed=None, files=None):
        self.busy, self.offline = set(busy), set(offline)
        #: ``{run id: [changed file]}``: the PR (numbered as its run) each run was for
        self.files = files or {}
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
            'pr.yml': [pr(101, 'queued', 'bug/B-0008', -60),       # S2: cancelled first
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
        self.assertEqual(self.lines[0], 'ci queue: cancelled queued pr run 101 (B-0008, S2) — '
                                        'main run 900 at fffffffff has waited 25m for runners')
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
        self.assertEqual(self.lines[0], 'relief: exempt bug/B-0008 — changes CI config')
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
        self.assertEqual(self.lines[0], 'relief: exempt bug/B-0008 — changes CI config')

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
        self.assertEqual(self.lines[0], 'relief: exempt bug/B-0008 — changes CI config')
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
        out['pr.yml'] = [pr(101, 'bug/B-0008', -60), pr(106, 'task/T-0500', -5),
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
                         'ci queue: cancelled queued pr run 106 (T-0500, Task) — created after '
                         'main run 900 at fffffffff but holds the heavy queue ahead of its queued '
                         'm3b-e2e (queued 25m)')
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
            "with main's m6-e2e queued 45m; sunk 3 min"])
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
                         'ci queue: cancelled queued pr run 120 (T-0341, Feature) — created after '
                         'S1 PR run 850 (B-0007) at sha850 but holds the heavy queue ahead of its '
                         'queued m6-e2e (queued 8m)')
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


class TestCeiling(Base):
    def test_capacity_ci_is_the_batch_steps_ceiling(self):
        p = product(cap={'ci': 2})
        d = self.admit(self.queue(p, FakeGh(inflight=2)), 'batch', 'batch', kind='batch')
        self.assertFalse(d.admitted)
        self.assertEqual(self.lines, ['ci queue: batch waits — at the ci ceiling (2 runs in '
                                      'flight; batch starts below 2) (other, 1st in line)'])
        p2 = product(cap={'ci': 2}, name='p2')
        q = self.queue(p2, FakeGh(inflight=1), minutes=1)
        self.assertTrue(self.admit(q, 'pr:a', 'T-0500').admitted)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)  # 1 + 1 admitted
        self.assertIn('1 runs in flight + 1 started this pass', self.lines[-1])

    def test_a_feature_pr_at_the_ceiling_starts_when_the_runners_fit(self):
        """The ceiling's worth of runs is always in flight: a Feature PR held by it would never
        open. At the ceiling it is governed by the runner fit alone."""
        p = product(cap={'ci': 4})
        q = self.queue(p, FakeGh(inflight=4))
        self.assertTrue(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertEqual(self.lines, [])
        q = self.queue(p, FakeGh(inflight=9, busy={'h1', 'h2'}), minutes=5)
        self.assertFalse(self.admit(q, 'pr:feat2', 'T-0341').admitted)   # the fit still holds
        self.assertEqual(self.lines, ['ci queue: T-0341 waits — heavy 1 free, needs 3 '
                                      '(Feature, 1st in line)'])
        self.lines.clear()
        q = self.queue(p, FakeGh(inflight=4), minutes=10)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)
        self.assertIn('at the ci ceiling', self.lines[-1])

    def test_a_batch_held_at_the_ceiling_sets_no_runners_aside(self):
        p = product(cap={'ci': 4}, name='b')
        self.assertFalse(self.admit(self.queue(p, FakeGh(inflight=4)), 'batch', 'batch',
                                    kind='batch').admitted)
        q = self.queue(p, FakeGh(inflight=4), minutes=1)
        self.assertTrue(self.admit(q, 'pr:task', 'T-0500').admitted)   # behind it in line


    def test_s1_hotfix_trunk_and_deploy_starts_are_exempt_from_ceiling_and_fit(self):
        """PR runs fill the ceiling (4/4): a trunk run ranked right after the S1 still starts;
        a batch waits at the ceiling, an ordinary or Feature PR starts on the runner fit."""
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
        self.assertTrue(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)
        self.assertEqual(len(self.lines), 1)
        self.assertIn('at the ci ceiling', self.lines[0])
        # exempt from the runner fit too: the host queues its jobs
        self.lines.clear()
        q = self.queue(p, FakeGh(inflight=4, busy={'h1', 'h2'}), minutes=50)
        self.assertTrue(self.admit(q, 'trunk:t2', 'T-0500', kind='trunk').admitted)
        self.assertEqual(self.lines, [])

    def test_ceiling_applies_to_batch_starts_only_fit_to_batch_and_ordinary_prs(self):
        ceil, fit = ci_queue.ceiling_applies, ci_queue.fit_applies
        self.assertFalse(ceil({'kind': 'pr', 'prio': ci_queue.OTHER}))
        self.assertFalse(ceil({'kind': 'pr', 'prio': ci_queue.FEATURE}))
        self.assertTrue(ceil({'kind': 'batch', 'prio': ci_queue.OTHER}))
        self.assertTrue(fit({'kind': 'pr', 'prio': ci_queue.OTHER}))
        self.assertTrue(fit({'kind': 'pr', 'prio': ci_queue.FEATURE}))
        self.assertTrue(fit({'kind': 'batch', 'prio': ci_queue.OTHER}))
        for ok in (ceil, fit):
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
        """The queue held a batch at 4/4; the row's count is now 5: the row names 5, never 4."""
        from unittest import mock
        from asf.views import status
        p = product(cap={'ci': 4})
        self.t0 = ci_queue._now()   # the row reads the file as of now
        self.assertFalse(self.admit(self.queue(p, FakeGh(inflight=4)), 'batch', 'batch',
                                    kind='batch').admitted)
        self.assertIn('(4 runs in flight;', self.lines[-1])
        with mock.patch('subprocess.run', side_effect=FakeGh(inflight=5)):
            cell = status.capacity_cell({}, p)
        self.assertIn('ci 5 runs in flight (batch starts below 4 — batch waits)', cell)
        self.assertIn('head batch waits — at the ci ceiling (5 runs in flight; batch '
                      'starts below 4) (other, 1st in line)', cell)
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
            self.assertIn(f'1. T-0341 [pr, Feature, full run, since', view[2])
            self.assertTrue(view[2].endswith(said), view[2])
            row = ci_queue.status_clause(p, source=self.src(p, gh))
            self.assertEqual(row, f"ci queue 1, head T-0341 {said.replace('waits:', 'waits —')} "
                                  f"(Feature, 1st in line)")
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
        self.assertEqual(ci_queue.needs_from_history([jobs] * 5, pool, runners), {'heavy': 3})

    def test_the_estimate_is_about_3_when_each_run_peaks_at_about_3_heavy(self):
        """Ten runs, half asking for ``heavy``, half for the sub-label, each 3 heavy at once (one
        run 4, one 2) over staggered stages on a 12-runner class: the estimate is 3, not 6+."""
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
                         {'heavy': 3, 'light': 1})


class TestStarvationGuard(Base):
    def test_a_pr_waiting_past_pr_wait_min_starts_on_half_its_expected_jobs(self):
        p = product()                              # expects heavy 3, light 1
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
