"""asf.ci_queue — runners busy without a job (2026-09-27: two runners ``ci.reserve`` kept for the
trunk showed busy while main's required jobs queued): detection across passes, the queue's and
the reservation's accounting, the trunk relief escalating at once when the reservation is broken,
and the doctor / status lines that name them."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import ci_pool, ci_queue, doctor, env

ITEMS = {
    'T-0341': {'id': 'T-0341', 'type': 'task'},
    'B-0008': {'id': 'B-0008', 'type': 'bug', 'severity': 'S2'},
    'B-0007': {'id': 'B-0007', 'type': 'bug', 'severity': 'S1'},
}
HEAVY = ['self-hosted', 'heavy']
PR_OK = HEAVY + ['pr-ok']
T0 = datetime.datetime(2026, 9, 27, 0, 57, tzinfo=datetime.timezone.utc)


def at(minutes):
    return (T0 + datetime.timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


def product(org=None):
    pool = [{'runner': n, 'provider': 'alpha', 'role': 'heavy'} for n in ('h1', 'h2', 'h3', 'r1', 'r2')]
    ci = {'provider': 'github-actions', 'workflow': 'ci.yml', 'pool': pool,
          'reserve': {'label': 'pr-ok', 'of': 'heavy', 'keep_free': 2, 'spread_by': 'none'},
          'queue': {'workflows': {'pr': 'pr.yml', 'trunk': 'ci.yml', 'batch': 'batch.yml'}}}
    if org:
        ci['runner_org'] = org
    return env.Product('p', {'repo_slug': 'o/r', 'ci': ci,
                             'deploy_sha': {'prod': {'required_jobs': ['gate-tests']}}})


def job(name, status, labels, runner=None, created=-5, start=None):
    return {'name': name, 'status': status, 'labels': labels, 'runner_name': runner,
            'created_at': at(created), 'started_at': at(start) if start is not None else None,
            'completed_at': None}


class Host:
    """A fake ``gh``: the runners (h1-h3 carry ``pr-ok``, r1/r2 are kept for the trunk), the
    runs by status (the listing that finds a run however old), each run's live jobs, and the
    per-workflow ``gh run list``. Every argv is logged."""

    def __init__(self, busy=('h1', 'h2', 'h3', 'r1', 'r2'), trunk_queued_min=5, extra_repos=None):
        self.busy = set(busy)
        self.calls = []
        self.extra_repos = extra_repos or {}
        t = at
        self.runs = {
            900: {'id': 900, 'status': 'queued', 'event': 'push', 'head_branch': 'main',
                  'head_sha': 'f' * 40, 'created_at': t(-trunk_queued_min - 1),
                  'path': '.github/workflows/ci.yml'},
            110: {'id': 110, 'status': 'in_progress', 'event': 'pull_request',
                  'head_branch': 'worker/scratch', 'head_sha': 'a' * 40, 'created_at': t(-40),
                  'path': '.github/workflows/pr.yml'},
            111: {'id': 111, 'status': 'in_progress', 'event': 'pull_request',
                  'head_branch': 'task/T-0341', 'head_sha': 'b' * 40, 'created_at': t(-30),
                  'path': '.github/workflows/pr.yml'},
            112: {'id': 112, 'status': 'in_progress', 'event': 'pull_request',
                  'head_branch': 'bug/B-0008', 'head_sha': 'c' * 40, 'created_at': t(-20),
                  'path': '.github/workflows/pr.yml'},
        }
        self.jobs = {
            900: [job('gate', 'completed', HEAVY, 'h1', -6, -6),
                  job('gate-tests', 'queued', HEAVY, created=-trunk_queued_min)],
            110: [job('gate', 'in_progress', PR_OK, 'h1', -30, -30)],
            111: [job('gate', 'in_progress', PR_OK, 'h2', -20, -20)],
            112: [job('gate', 'in_progress', PR_OK, 'h3', -10, -10)],
        }

    def runner_rows(self):
        out = []
        for i, n in enumerate(('h1', 'h2', 'h3', 'r1', 'r2')):
            labels = PR_OK if n.startswith('h') else HEAVY
            out.append({'name': n, 'id': i, 'busy': n in self.busy, 'status': 'online',
                        'labels': [{'name': l, 'type': 'read-only' if l == 'self-hosted'
                                    else 'custom'} for l in labels]})
        return out

    def listed(self, wf):
        return [{'databaseId': r['id'], 'status': r['status'], 'conclusion': None,
                 'event': r['event'], 'headBranch': r['head_branch'], 'headSha': r['head_sha'],
                 'createdAt': r['created_at']}
                for r in self.runs.values() if r['path'].endswith('/' + wf)]

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        ok = lambda out: subprocess.CompletedProcess(argv, 0, out, '')  # noqa: E731
        path = next((a for a in argv[2:] if '/' in a and not a.startswith('.')), '')
        if argv[:2] == ['gh', 'api'] and path.startswith('orgs/') and path.endswith('/repos?per_page=100'):
            return ok('\n'.join(['o/r', *self.extra_repos]))
        if argv[:2] == ['gh', 'api'] and '/actions/runners' in path:
            return ok('\n'.join(json.dumps(r) for r in self.runner_rows()))
        if argv[:2] == ['gh', 'api'] and '/actions/runs?status=' in path:
            repo = path.split('/actions/')[0].split('repos/')[1]
            status = path.split('status=')[1].split('&')[0]
            if repo != 'o/r':
                runs = [r for r in self.extra_repos.get(repo, []) if r['status'] == status]
            else:
                runs = [r for r in self.runs.values() if r['status'] == status]
            return ok('\n'.join([str(len(runs)), *(json.dumps(r) for r in runs)]))
        if argv[:2] == ['gh', 'api'] and path.endswith('&filter=latest'):
            rid = int(path.split('/runs/')[1].split('/')[0])
            return ok('\n'.join(json.dumps(j) for j in self.jobs.get(rid, [])))
        if argv[:2] == ['gh', 'api'] and '/actions/runs/' in path:
            return ok('')                           # no PR files: nothing exempt
        if argv[:3] == ['gh', 'run', 'list']:
            return ok(json.dumps(self.listed(argv[argv.index('--workflow') + 1])))
        if argv[:3] == ['gh', 'run', 'cancel']:
            return ok('')
        return ok('')

    def cancels(self):
        return [c[3] for c in self.calls if c[:3] == ['gh', 'run', 'cancel']]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.lines = []

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def relieve(self, p, host, minutes=0):
        return ci_queue.relieve_trunk(p, items=ITEMS, source=ci_queue.GitHubSource(p, run=host),
                                      out=self.lines.append,
                                      now=T0 + datetime.timedelta(minutes=minutes))


class TestDetection(Base):
    def test_busy_with_no_job_is_phantom_only_on_the_second_pass(self):
        p, host = product(), Host()
        self.relieve(p, host)
        self.assertEqual(ci_queue.phantom_names(p.name, T0), set())
        self.relieve(p, host, minutes=1)
        self.assertEqual(ci_queue.phantom_names(p.name, T0 + datetime.timedelta(minutes=1)),
                         {'r1', 'r2'})
        rec = ci_queue.load(p.name)['phantom']['r1']
        self.assertEqual((rec['since'], rec['passes']), (at(0), 2))

    def test_a_job_seen_in_an_old_run_listed_by_status_clears_it(self):
        # the live shape: a PR re-run from hours ago holds r1 — only the status listing finds it
        p, host = product(), Host()
        host.runs[50] = {'id': 50, 'status': 'queued', 'event': 'pull_request',
                         'head_branch': 'cloud/fix', 'head_sha': 'd' * 40,
                         'created_at': at(-600), 'path': '.github/workflows/pr.yml'}
        host.jobs[50] = [job('site', 'in_progress', HEAVY, 'r1', -40, -40)]
        self.relieve(p, host)
        self.relieve(p, host, minutes=1)
        self.assertEqual(ci_queue.phantom_names(p.name, T0 + datetime.timedelta(minutes=1)),
                         {'r2'})

    def test_a_runner_that_frees_or_gets_a_job_leaves_and_an_unreadable_listing_keeps(self):
        p, host = product(), Host()
        self.relieve(p, host)
        self.relieve(p, host, minutes=1)
        host.busy.discard('r2')
        dead = lambda argv, **kw: (subprocess.CompletedProcess(argv, 1, '', 'boom')  # noqa: E731
                                   if '/actions/runs?status=' in ' '.join(argv) else host(argv))
        self.relieve(p, dead, minutes=2)            # unreadable: nothing learnt, nothing lost
        self.assertEqual(ci_queue.load(p.name)['phantom']['r2']['passes'], 2)
        self.relieve(p, host, minutes=3)
        self.assertEqual(ci_queue.phantom_names(p.name, T0 + datetime.timedelta(minutes=3)),
                         {'r1'})

    def test_other_repos_of_the_runner_org_are_read(self):
        p = product(org='o')
        host = Host(extra_repos={'o/other': [
            {'id': 70, 'status': 'in_progress', 'event': 'push', 'head_branch': 'main',
             'head_sha': 'e' * 40, 'created_at': at(-3), 'path': '.github/workflows/x.yml'}]})
        host.jobs[70] = [job('build', 'in_progress', HEAVY, 'r2', -3, -3)]
        self.relieve(p, host)
        self.relieve(p, host, minutes=1)
        self.assertEqual(ci_queue.phantom_names(p.name, T0 + datetime.timedelta(minutes=1)),
                         {'r1'})

    def test_phantoms_are_neither_free_nor_reserved(self):
        p, host = product(), Host(busy=('h1', 'h2', 'h3', 'r1'))
        self.relieve(p, host)
        self.relieve(p, host, minutes=1)
        now = T0 + datetime.timedelta(minutes=1)
        q = ci_queue.Queue(p, source=ci_queue.GitHubSource(p, run=host), now=now,
                           out=self.lines.append)
        self.assertEqual(q.free(), {'heavy': 1})     # r2 only; r1 is no runner to count on
        runners = ci_pool.GitHubBackend(p, run=host).runners()
        plan = ci_pool.reserve_plans(p, runners, unavailable=ci_queue.phantom_names(p.name, now))[0]
        # r1 cannot keep a slot free: keep_free 2 takes one more runner off the PR runners
        self.assertNotIn('r1', plan.candidates)
        self.assertEqual(len(plan.reserved), 2)
        self.assertIn('r2', plan.reserved)
        self.assertEqual(len(plan.remove), 1)


class TestPhantomRelief(Base):
    def test_a_phantom_reservation_escalates_at_once_lowest_priority_first(self):
        p, host = product(), Host(trunk_queued_min=5)
        self.relieve(p, host)                       # pass 1: not yet phantom — nothing cancelled
        self.assertEqual(host.cancels(), [])
        self.assertEqual(self.relieve(p, host, minutes=1), (1, 0))
        # gate-tests needs one heavy runner: the scratch branch (no record item) goes first
        self.assertEqual(host.cancels(), ['110'])
        self.assertTrue(any('cancelled in-progress pr run 110' in l and 'r1, r2' in l
                            for l in self.lines), self.lines)
        rec = ci_queue.load(p.name)['relief']
        self.assertEqual([(r['id'], r.get('for')) for r in rec], [(110, None)])

    def test_a_pr_job_on_a_reserved_runner_breaks_the_reservation_too(self):
        # the 2026-09-27 shape: r1/r2 run PR jobs whose runs-on predates the reservation label
        p, host = product(), Host(trunk_queued_min=4)
        for rid, name in ((120, 'r1'), (121, 'r2')):
            host.runs[rid] = {'id': rid, 'status': 'queued', 'event': 'pull_request',
                              'head_branch': 'task/T-0341', 'head_sha': str(rid) * 13,
                              'created_at': at(-600), 'path': '.github/workflows/pr.yml'}
            host.jobs[rid] = [job('site', 'in_progress', HEAVY, name, -30, -30)]
        # a job on a runner is no guess: no second pass needed
        self.assertEqual(self.relieve(p, host), (1, 0))
        # a run holding a reserved runner goes first (newest of the two), before the
        # lower-priority scratch run on h1
        self.assertEqual(host.cancels(), ['121'])
        self.assertEqual(self.lines, [
            "ci queue: cancelled in-progress pr run 121 (T-0341) — main's reserved runners "
            "r1, r2 held by a pr job; it holds a runner main's gate-tests (queued 4m) can take; "
            "sunk 30 min"])

    def test_a_healthy_reservation_waits_for_the_usual_bar(self):
        p, host = product(), Host(busy=('h1', 'h2', 'h3'), trunk_queued_min=5)
        self.relieve(p, host)
        self.relieve(p, host, minutes=1)
        self.assertEqual(host.cancels(), [])        # r1/r2 idle: gate-tests gets one

    def test_s1_runs_are_never_cancelled(self):
        p, host = product(), Host(trunk_queued_min=5)
        for rid in (110, 111, 112):
            host.runs[rid]['head_branch'] = 'bug/B-0007'
        self.relieve(p, host)
        self.relieve(p, host, minutes=1)
        self.assertEqual(host.cancels(), [])


class TestPhantomLines(Base):
    def seed(self, since_min):
        data = ci_queue.load('p')
        data['phantom'] = {'r1': {'since': at(-since_min), 'last': at(0), 'passes': 3}}
        ci_queue.save('p', data)

    def test_runner_rows_name_the_runner_and_go_red_after_ten_minutes(self):
        p = product()
        self.seed(12)
        self.assertEqual(ci_queue.runner_rows(p, now=T0), [
            (True, False, 'r1 busy with no job for 12 min — restart its runner service')])
        self.seed(4)
        self.assertEqual(ci_queue.runner_rows(p, now=T0), [
            (True, True, 'r1 busy with no job for 4 min — restart its runner service')])

    def test_no_phantom_is_one_ok_row(self):
        self.assertEqual(ci_queue.runner_rows(product(), now=T0),
                         [(True, True, 'no runner busy without a job')])

    def test_the_doctor_carries_the_row(self):
        p = product()
        self.seed(12)
        rows = doctor.check_ci_runners(p, now=T0)
        self.assertEqual(rows, [(True, False,
                                 'r1 busy with no job for 12 min — restart its runner service')])

    def test_the_status_clause_names_it(self):
        p = product()
        self.seed(12)
        clause = ci_queue.status_clause(p, now=T0, source=ci_queue.GitHubSource(p, run=Host()))
        self.assertIn('r1 busy with no job for 12 min — restart its runner service', clause)


if __name__ == '__main__':
    unittest.main()
