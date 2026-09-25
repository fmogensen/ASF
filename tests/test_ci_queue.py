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

    def __init__(self, busy=(), offline=(), history=None, inflight=0):
        self.busy, self.offline = set(busy), set(offline)
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
            out = json.dumps([{'databaseId': i} for i in range(len(self.history))])
        elif argv[:3] == ['gh', 'run', 'list']:
            out = str(self.inflight)
        elif argv[:2] == ['gh', 'api'] and any('/jobs' in a for a in argv):
            run_id = int(next(a for a in argv if '/jobs' in a).split('/runs/')[1].split('/')[0])
            out = '\n'.join(json.dumps(j) for j in self.history[run_id])
        return subprocess.CompletedProcess(argv, 0, out, '')


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
        # heavy per run 3, 3, 2 (the third by its label) → 3; light 1; the hosted job needs none
        self.assertEqual(needs, {'heavy': 3, 'light': 1})
        big = [[{'runner_name': 'h1'}] * 9]
        self.assertEqual(ci_queue.needs_from_history(big, ci_pool.load_pool(pool)), {'heavy': 3})

    def test_free_runners_are_online_and_idle_at_their_slots(self):
        gh = FakeGh(busy={'h1'}, offline={'h2'})
        q = self.queue(product(), gh)
        self.assertEqual(q.free(), {'heavy': 1, 'light': 2})


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
        self.admit(self.queue(p, busy, minutes=2), 'trunk:t', 'T-0500', kind='trunk')  # trunk
        self.admit(self.queue(p, busy, minutes=3), 'trunk:fix', 'B-0007', kind='trunk')  # S1
        self.admit(self.queue(p, busy, minutes=4), 'pr:new', 'T-0500')  # other, newest
        entries = ci_queue.load('p')['entries']
        self.assertEqual(ci_queue.line_order(entries),
                         ['trunk:fix', 'trunk:t', 'pr:feat', 'pr:old', 'pr:new'])
        self.assertEqual(self.lines[-1], 'ci queue: T-0500 waits — heavy 0 free, needs 3, '
                                         'light 0 free, needs 1 (Task, 5th in line)')
        # runners come free: the S2 asks first, but the S1 ahead of it is owed them
        q = self.queue(p, FakeGh(), minutes=5)
        self.assertFalse(self.admit(q, 'pr:old', 'B-0008').admitted)
        self.assertIn('(S2, 4th in line)', self.lines[-1])
        self.assertTrue(self.admit(q, 'trunk:fix', 'B-0007', kind='trunk').admitted)

    def test_a_trunk_run_goes_before_pr_runs_queued_earlier(self):
        p = product()
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        self.admit(self.queue(p, busy), 'pr:feat', 'T-0341')
        self.admit(self.queue(p, busy, minutes=1), 'trunk:t', 'T-0500', kind='trunk')
        self.assertIn('(trunk, 1st in line)', self.lines[-1])
        q = self.queue(p, FakeGh(), minutes=2)
        self.assertFalse(self.admit(q, 'pr:feat', 'T-0341').admitted)
        self.assertTrue(self.admit(q, 'trunk:t', 'T-0500', kind='trunk').admitted)


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
            'ci.yml': {'at': stamp, 'needs': {'heavy': 3}},
            'pr.yml': {'at': stamp, 'needs': {'heavy': 1}},
            'batch.yml': {'at': stamp, 'needs': {'heavy': 2}}}})

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
        self.assertEqual(self.lines, ['ci queue: T-0500 waits — at the ci ceiling (2/2 runs in '
                                      'flight) (Task, 1st in line)'])
        q = self.queue(p, FakeGh(inflight=1), minutes=1)
        self.assertTrue(self.admit(q, 'pr:a', 'T-0500').admitted)
        self.assertFalse(self.admit(q, 'batch', 'batch', kind='batch').admitted)  # 1 + 1 admitted


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

    def test_status_names_the_depth_and_the_head(self):
        p = product()
        self.assertEqual(ci_queue.status_clause(p, now=self.t0), 'ci queue empty')
        busy = FakeGh(busy={'h1', 'h2', 'h3'})
        self.admit(self.queue(p, busy), 'pr:a', 'T-0500')
        self.admit(self.queue(p, busy), 'pr:b', 'B-0007')
        self.assertEqual(ci_queue.status_clause(p, now=self.t0),
                         'ci queue 2, head B-0007 waits — heavy 0 free, needs 3 (S1, 1st in line)')

    def test_config_problems(self):
        self.assertEqual(ci_queue.config_problems({'queue': {'mode': 'on', 'history': 5,
                                                             'workflows': {'batch': 'b.yml'}}}), [])
        bad = dict(ci_queue.config_problems({'queue': {'mode': 'loud', 'history': 0,
                                                       'workflows': {'nightly': 'x'}, 'x': 1}}))
        self.assertEqual(sorted(bad), ['ci.queue.history', 'ci.queue.mode',
                                       'ci.queue.workflows.nightly', 'ci.queue.x'])


if __name__ == '__main__':
    unittest.main()
