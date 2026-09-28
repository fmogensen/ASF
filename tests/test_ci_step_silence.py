"""asf.ci_queue.step_silence — the pure judge over one job's steps: which step it is inside, and
whether that step has gone quiet past the product's own limit — and the seam it reads:
``Source.live_runs`` and the steps carried on ``Source.live_jobs``, both memoised with the
phantom watch's own listing so a pass running both pays for one answer, not two."""
import datetime
import os
import subprocess
import unittest

from asf import ci_queue, env
from asf.workers import stall
from tests.test_ci_phantom import Base, HEAVY, ITEMS, Host, T0, at, job, product


def _t(minutes_ago, now):
    return (now - datetime.timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _step(name, started=None, completed=None):
    step = {'name': name}
    if started is not None:
        step['started_at'] = started
    if completed is not None:
        step['completed_at'] = completed
    return step


def _job(*steps):
    return {'status': 'in_progress', 'steps': list(steps)}


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 9, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def test_a_step_started_past_the_limit_with_no_completion_since_is_step_silence(self):
        job = _job(_step('build', _t(30, self.now), _t(25, self.now)),
                    _step('test', _t(11, self.now)))
        cls, step, since = ci_queue.step_silence(job, self.now, 600)
        self.assertEqual(cls, ci_queue.STEP_SILENCE)
        self.assertEqual(step, 'test')
        self.assertEqual(since, ci_queue._parse(_t(11, self.now)))

    def test_the_named_step_is_the_newest_started_one(self):
        job = _job(_step('build', _t(30, self.now), _t(25, self.now)),
                    _step('test', _t(11, self.now)))
        _, step, _ = ci_queue.step_silence(job, self.now, 600)
        self.assertEqual(step, 'test')

    def test_a_completion_after_the_newest_start_is_moving(self):
        job = _job(_step('build', _t(30, self.now), _t(25, self.now)),
                    _step('test', _t(11, self.now), _t(2, self.now)))
        cls, step, _ = ci_queue.step_silence(job, self.now, 600)
        self.assertEqual(cls, ci_queue.MOVING)
        self.assertEqual(step, 'test')

    def test_a_step_under_the_limit_is_moving(self):
        job = _job(_step('test', _t(4, self.now)))
        cls, step, _ = ci_queue.step_silence(job, self.now, 600)
        self.assertEqual(cls, ci_queue.MOVING)
        self.assertEqual(step, 'test')

    def test_a_job_between_steps_is_moving_not_stalled(self):
        job = _job(_step('build', _t(20, self.now), _t(12, self.now)),
                    _step('test'))
        cls, step, _ = ci_queue.step_silence(job, self.now, 600)
        self.assertEqual(cls, ci_queue.MOVING)
        self.assertEqual(step, 'build')

    def test_no_steps_is_unknown_and_claims_nothing(self):
        for job in ({'status': 'in_progress', 'steps': []}, {'status': 'in_progress'},
                    {'status': 'in_progress', 'steps': None}):
            cls, step, since = ci_queue.step_silence(job, self.now, 600)
            self.assertEqual((cls, step, since), (ci_queue.UNKNOWN, None, None))

    def test_steps_with_no_start_are_unknown(self):
        job = _job(_step('build'), _step('test'))
        self.assertEqual(ci_queue.step_silence(job, self.now, 600),
                          (ci_queue.UNKNOWN, None, None))

    def test_an_unparsable_stamp_is_unknown_never_stalled(self):
        job = _job(_step('build', 'not-a-timestamp'))
        self.assertEqual(ci_queue.step_silence(job, self.now, 600),
                          (ci_queue.UNKNOWN, None, None))

    def test_a_tie_on_started_at_names_the_later_step_in_provider_order(self):
        same = _t(20, self.now)
        job = _job(_step('a', same), _step('b', same))
        cls, step, _ = ci_queue.step_silence(job, self.now, 600)
        self.assertEqual(cls, ci_queue.STEP_SILENCE)
        self.assertEqual(step, 'b')

    def test_the_limit_is_read_from_the_product_and_defaults_to_ten_minutes(self):
        default_product = env.Product('p', {'ci': {'queue': {}}})
        self.assertEqual(ci_queue.step_silence_seconds(default_product), 600)
        five_min = env.Product('p', {'ci': {'queue': {'step_silence_min': 5}}})
        self.assertEqual(ci_queue.step_silence_seconds(five_min), 300)

    def test_zero_and_off_turn_the_watch_off(self):
        for v in (0, '0', 'off', 'OFF', ' none ', 'false', False, -5):
            product = env.Product('p', {'ci': {'queue': {'step_silence_min': v}}})
            self.assertEqual(ci_queue.step_silence_seconds(product), 0, msg=repr(v))

    def test_the_duration_grammar_matches_stall_silent_minutes_over_one_table(self):
        table = (45, '45', '45m', '1h', '30s', '2d', 'nonsense', '', True)
        default = 999
        for v in table:
            got = ci_queue._minutes(v, default)
            product = env.Product('p', {'stage_limits': {'silent_min': v}})
            want = stall.silent_minutes(product)
            if got == default:
                self.assertEqual(want, stall.DEFAULT_SILENT_MIN, msg=repr(v))
            else:
                self.assertEqual(got, want, msg=repr(v))


class SeamTests(unittest.TestCase):
    def test_live_runs_returns_the_products_non_completed_runs_in_the_queues_vocabulary(self):
        p, host = product(), Host()
        runs = ci_queue.GitHubSource(p, run=host).live_runs()
        self.assertEqual({r['id'] for r in runs}, {900, 110, 111, 112, 113})
        for r in runs:
            want = host.runs[r['id']]
            self.assertEqual(r, {'id': want['id'], 'status': want['status'],
                                 'headBranch': want['head_branch'], 'headSha': want['head_sha'],
                                 'createdAt': want['created_at'],
                                 'workflow': os.path.basename(want['path'])})

    def test_live_runs_carries_the_runs_workflow_basename(self):
        p, host = product(), Host()
        host.runs[900]['path'] = '.github/workflows/nested/ci-special.yml'
        runs = ci_queue.GitHubSource(p, run=host).live_runs()
        by_id = {r['id']: r for r in runs}
        self.assertEqual(by_id[900]['workflow'], 'ci-special.yml')

    def test_the_two_listings_are_made_once_per_pass_for_both_watches(self):
        for order in ('busy_first', 'live_first'):
            with self.subTest(order=order):
                p, host = product(), Host()
                src = ci_queue.GitHubSource(p, run=host)
                if order == 'busy_first':
                    src.busy_runners()
                    src.live_runs()
                else:
                    src.live_runs()
                    src.busy_runners()
                calls = [c for c in host.calls if c[:2] == ['gh', 'api']
                        and any('/actions/runs?status=' in a for a in c)]
                statuses = [next(a for a in c if '/actions/runs?status=' in a)
                           .split('status=')[1].split('&')[0] for c in calls]
                self.assertEqual(len(statuses), len(set(statuses)), calls)
                self.assertEqual(set(statuses), ci_queue.QUEUED_STATUSES | {'in_progress'})

    def test_an_unreadable_listing_makes_live_runs_none_and_is_not_asked_twice(self):
        p, host = product(), Host()
        attempts = []

        def flaky(argv, **kw):
            if '/actions/runs?status=queued' in ' '.join(argv):
                attempts.append(argv)
                return subprocess.CompletedProcess(argv, 1, '', 'boom')
            return host(argv, **kw)

        src = ci_queue.GitHubSource(p, run=flaky)
        self.assertIsNone(src.live_runs())
        self.assertIsNone(src.live_runs())
        self.assertEqual(len(attempts), 1)

    def test_the_base_source_returns_none_for_both_readers(self):
        src = ci_queue.Source()
        self.assertIsNone(src.live_runs())
        self.assertIsNone(src.live_jobs(1))

    def test_live_jobs_carries_the_steps_the_provider_published(self):
        p, host = product(), Host()
        jobs = ci_queue.GitHubSource(p, run=host).live_jobs(110)
        self.assertEqual(jobs[0]['steps'], host.jobs[110][0]['steps'])

    def test_a_job_with_no_steps_array_carries_no_steps_key(self):
        p, host = product(), Host()
        jobs = ci_queue.GitHubSource(p, run=host).live_jobs(900)
        gate_tests = next(j for j in jobs if j['name'] == 'gate-tests')
        self.assertNotIn('steps', gate_tests)


class TwelveMinutesTests(Base):
    """A job hung inside a step is cancelled within twelve minutes, and a moving one never is —
    driven a pass a minute from the step's start (``QUEUE_EVERY_S``), over a fake host carrying
    only the run under test (so a fixture job's own, unrelated, long-stale ``steps`` never
    joins the watch)."""

    def _host(self, start_min, run_id=200, branch='task/T-0341', run_status='in_progress',
              path='.github/workflows/pr.yml', step_name='unit tests'):
        host = Host()
        host.runs = {run_id: {'id': run_id, 'status': run_status, 'event': 'pull_request',
                              'head_branch': branch, 'head_sha': 'd' * 40,
                              'created_at': at(start_min - 1), 'path': path}}
        host.jobs = {run_id: [job('gate', 'in_progress', HEAVY, 'h1', start_min, start_min,
                                  steps=[{'name': step_name, 'status': 'in_progress',
                                          'started_at': at(start_min), 'completed_at': None}])]}
        return host

    def stall(self, p, host, minutes, dry_run=False):
        return ci_queue.update_stalls(p, ci_queue.GitHubSource(p, run=host), items=ITEMS,
                                      out=self.lines.append, dry_run=dry_run,
                                      now=T0 + datetime.timedelta(minutes=minutes))

    def test_a_job_hung_inside_a_step_is_cancelled_by_the_twelfth_minute(self):
        p, host = product(), self._host(-11)
        self.stall(p, host, 0)
        self.assertEqual(host.cancels(), [])
        self.assertEqual(self.stall(p, host, 1), 1)
        self.assertEqual(host.cancels(), ['200'])
        self.assertTrue(any('cancelled in-progress pr run 200' in l and 'gate' in l
                            and '"unit tests"' in l for l in self.lines), self.lines)

    def test_nothing_is_cancelled_before_the_threshold(self):
        p, host = product(), self._host(-9)
        self.stall(p, host, 0)
        self.assertEqual(host.cancels(), [])

    def test_one_pass_only_records_and_does_not_cancel(self):
        p, host = product(), self._host(-11)
        self.stall(p, host, 0)
        rec = ci_queue.load(p.name)['stalled']['200:gate']
        self.assertEqual(rec['passes'], 1)
        self.assertEqual(host.cancels(), [])

    def test_a_job_that_starts_a_new_step_leaves_the_watch(self):
        p, host = product(), self._host(-11)
        self.stall(p, host, 0)
        self.assertIn('200:gate', ci_queue.load(p.name)['stalled'])
        host.jobs[200][0]['steps'] = [{'name': 'deploy', 'status': 'in_progress',
                                       'started_at': at(0), 'completed_at': None}]
        self.stall(p, host, 1)
        self.assertEqual(ci_queue.load(p.name)['stalled'], {})
        self.assertEqual(host.cancels(), [])

    def test_a_dry_run_mode_prints_would_cancel_and_writes_nothing(self):
        p, host = product(), self._host(-11)
        self.stall(p, host, 0)
        path = os.path.join(env.state_dir(p.name), ci_queue.QUEUE_FILE)
        with open(path, encoding='utf-8') as f:
            before = f.read()
        self.stall(p, host, 1, dry_run=True)
        self.assertTrue(any('would cancel in-progress pr run 200' in l for l in self.lines),
                        self.lines)
        self.assertEqual(host.cancels(), [])
        with open(path, encoding='utf-8') as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_mode_off_makes_no_host_call(self):
        p, host = product(), self._host(-11)
        p.ci['queue']['mode'] = 'off'
        self.assertEqual(self.stall(p, host, 1), 0)
        self.assertEqual(host.calls, [])
        p.ci['queue']['mode'] = 'on'
        p.ci['queue']['step_silence_min'] = 'off'
        self.assertEqual(self.stall(p, host, 1), 0)
        self.assertEqual(host.calls, [])

    def test_an_unreadable_listing_neither_cancels_nor_forgets(self):
        p, host = product(), self._host(-11)
        self.stall(p, host, 0)
        before = ci_queue.load(p.name)['stalled']['200:gate']

        class _Flaky:
            def live_runs(self):
                raise RuntimeError('boom')

        n = ci_queue.update_stalls(p, _Flaky(), items=ITEMS, out=self.lines.append,
                                   now=T0 + datetime.timedelta(minutes=1))
        self.assertEqual(n, 0)
        self.assertEqual(ci_queue.load(p.name)['stalled']['200:gate'], before)

    def test_a_refused_cancel_keeps_the_record_and_says_so(self):
        p, host = product(), self._host(-11)

        class _RefuseCancel(ci_queue.GitHubSource):
            def _gh(self, args):
                if args[:2] == ['run', 'cancel']:
                    return None
                return super()._gh(args)

        src = _RefuseCancel(p, run=host)
        ci_queue.update_stalls(p, src, items=ITEMS, out=self.lines.append, now=T0)
        n = ci_queue.update_stalls(p, src, items=ITEMS, out=self.lines.append,
                                   now=T0 + datetime.timedelta(minutes=1))
        self.assertEqual(n, 0)
        self.assertTrue(any('cancel of stalled pr run 200 refused' in l for l in self.lines),
                        self.lines)
        rec = ci_queue.load(p.name)['stalled']['200:gate']
        self.assertEqual(rec['passes'], ci_queue.STALL_PASSES)

    def test_the_trunk_run_is_cancelled_too_and_a_ci_config_pr_is_not_exempt(self):
        p, host = product(), self._host(-11, branch='main', path='.github/workflows/ci.yml')
        self.stall(p, host, 0)
        self.assertEqual(self.stall(p, host, 1), 1)
        self.assertEqual(host.cancels(), ['200'])
        self.assertTrue(any('cancelled in-progress trunk run 200' in l for l in self.lines),
                        self.lines)

    def test_the_watch_runs_before_the_relief_in_the_pass(self):
        p, host = product(), Host(trunk_queued_min=5)
        for jobs in host.jobs.values():
            for j in jobs:
                j.pop('steps', None)
        host.runs[200] = {'id': 200, 'status': 'in_progress', 'event': 'pull_request',
                          'head_branch': 'task/T-0341', 'head_sha': 'd' * 40,
                          'created_at': at(-12), 'path': '.github/workflows/pr.yml'}
        host.jobs[200] = [job('gate2', 'in_progress', HEAVY, 'x1', -11, -11, steps=[
            {'name': 'unit tests', 'status': 'in_progress', 'started_at': at(-11),
             'completed_at': None}])]

        def pass_(minutes):
            return ci_queue.queue_pass(p, items=ITEMS, source=ci_queue.GitHubSource(p, run=host),
                                       out=self.lines.append,
                                       now=T0 + datetime.timedelta(minutes=minutes))

        pass_(0)
        pass_(1)
        cancels = host.cancels()
        self.assertIn('200', cancels)
        self.assertIn('110', cancels)
        self.assertLess(cancels.index('200'), cancels.index('110'))


class ReRunTests(Base):
    """The cancel is remembered and re-run like every other cancel here (:func:`rerun_ids`,
    :func:`_rerun_cancelled`'s ``started`` clause), and the third stall on one ``(branch, sha)``
    is not — one loud line, no ``relief`` record, and the ``stalls`` history counts it whether
    or not the cap was hit."""

    def _host(self, start_min, run_id=200, branch='task/T-0341', sha='d' * 40,
              path='.github/workflows/pr.yml', step_name='unit tests'):
        host = Host()
        host.runs = {run_id: {'id': run_id, 'status': 'in_progress', 'event': 'pull_request',
                              'head_branch': branch, 'head_sha': sha,
                              'created_at': at(start_min - 1), 'path': path}}
        host.jobs = {run_id: [job('gate', 'in_progress', HEAVY, 'h1', start_min, start_min,
                                  steps=[{'name': step_name, 'status': 'in_progress',
                                          'started_at': at(start_min), 'completed_at': None}])]}
        return host

    def stall(self, p, host, minutes):
        return ci_queue.update_stalls(p, ci_queue.GitHubSource(p, run=host), items=ITEMS,
                                      out=self.lines.append,
                                      now=T0 + datetime.timedelta(minutes=minutes))

    def _cancel(self, p, host):
        self.stall(p, host, 0)
        return self.stall(p, host, 1)

    def test_the_cancel_writes_a_relief_record_carrying_the_class_and_the_step(self):
        p, host = product(), self._host(-11)
        self.assertEqual(self._cancel(p, host), 1)
        relief = ci_queue.load(p.name)['relief']
        self.assertEqual(len(relief), 1)
        rec = relief[0]
        self.assertEqual(rec['id'], 200)
        self.assertEqual(rec['stall'], ci_queue.STEP_SILENCE)
        self.assertEqual(rec['stall_step'], 'unit tests')
        self.assertEqual(rec['stalls'], 1)

    def test_rerun_ids_carries_the_cancelled_run_so_the_lane_waits(self):
        p, host = product(), self._host(-11)
        self._cancel(p, host)
        self.assertIn('200', ci_queue.rerun_ids(env.state_dir(p.name)))

    def test_the_record_is_re_run_on_the_next_pass_with_nothing_to_wait_for(self):
        p, host = product(), self._host(-11)
        self._cancel(p, host)
        self.lines.clear()
        self.relieve(p, host, minutes=2)
        reruns = [c[3] for c in host.calls if c[:3] == ['gh', 'run', 'rerun']]
        self.assertEqual(reruns, ['200'])
        self.assertEqual(ci_queue.load(p.name)['relief'], [])

    def test_the_re_run_line_names_the_step_it_was_cancelled_for(self):
        p, host = product(), self._host(-11)
        self._cancel(p, host)
        self.lines.clear()
        self.relieve(p, host, minutes=2)
        self.assertTrue(any('re-ran pr run 200' in l and 'stalled in step "unit tests"' in l
                            for l in self.lines), self.lines)

    def test_a_stale_workflow_still_refuses_the_re_run(self):
        p, host = product(), self._host(-11)
        self._cancel(p, host)
        self.lines.clear()

        def stale(argv, **kw):
            joined = ' '.join(argv)
            if argv[:2] == ['gh', 'api'] and 'actions/runs/200' in joined and 'jobs' not in joined:
                return subprocess.CompletedProcess(argv, 0, f"{'d' * 40}\t{'.github/workflows/pr.yml'}\n", '')
            if argv[:2] == ['gh', 'api'] and 'contents/.github/workflows/pr.yml' in joined:
                blob = '1111111aaaa' if f'ref={"d" * 40}' in joined else '2222222bbbb'
                return subprocess.CompletedProcess(argv, 0, blob + '\n', '')
            return host(argv, **kw)

        self.relieve(p, stale, minutes=2)
        reruns = [c for c in host.calls if c[:3] == ['gh', 'run', 'rerun']]
        self.assertEqual(reruns, [])
        self.assertIn('ci queue: skip rerun 200 on task/T-0341 — workflow changed since '
                      '(1111111→2222222); next push runs fresh', self.lines)
        self.assertEqual(ci_queue.load(p.name)['relief'], [])

    def test_the_third_stall_on_one_sha_is_cancelled_and_not_re_run(self):
        p, host = product(), self._host(-11)
        branch, sha = host.runs[200]['head_branch'], host.runs[200]['head_sha']
        data = ci_queue.load(p.name)
        data['stalls'] = [{'run': 1, 'branch': branch, 'sha': sha, 'step': 'unit tests',
                           'at': ci_queue._iso(T0), 'job': 'gate'},
                          {'run': 2, 'branch': branch, 'sha': sha, 'step': 'unit tests',
                           'at': ci_queue._iso(T0), 'job': 'gate'}]
        ci_queue.save(p.name, data)
        self.assertEqual(self._cancel(p, host), 1)
        self.assertEqual(host.cancels(), ['200'])
        self.assertTrue(any(
            'ci queue: NOT re-running pr run 200 (T-0341) — step "unit tests" has stalled 3 '
            f'times on {branch} at {sha[:9]}; the step is the defect, not the run' in l
            for l in self.lines), self.lines)
        self.assertEqual(ci_queue.load(p.name)['relief'], [])
        self.lines.clear()
        self.relieve(p, host, minutes=2)
        self.assertEqual([c for c in host.calls if c[:3] == ['gh', 'run', 'rerun']], [])

    def test_the_cap_is_counted_per_branch_and_sha_not_per_run(self):
        p, host = product(), self._host(-11)
        branch, sha = host.runs[200]['head_branch'], host.runs[200]['head_sha']
        data = ci_queue.load(p.name)
        data['stalls'] = [{'run': 1, 'branch': branch, 'sha': sha, 'step': 'unit tests',
                           'at': ci_queue._iso(T0), 'job': 'gate'},
                          {'run': 2, 'branch': branch, 'sha': sha, 'step': 'unit tests',
                           'at': ci_queue._iso(T0), 'job': 'gate'}]
        ci_queue.save(p.name, data)
        self._cancel(p, host)
        self.assertEqual(ci_queue.load(p.name)['relief'], [])   # capped on this sha

        host2 = self._host(-11, run_id=201, branch=branch, sha='e' * 40)
        self.assertEqual(self._cancel(p, host2), 1)
        relief = ci_queue.load(p.name)['relief']
        self.assertEqual([(r['id'], r['stalls']) for r in relief], [(201, 1)])

    def test_the_history_record_is_written_even_at_the_cap(self):
        p, host = product(), self._host(-11)
        branch, sha = host.runs[200]['head_branch'], host.runs[200]['head_sha']
        data = ci_queue.load(p.name)
        data['stalls'] = [{'run': 1, 'branch': branch, 'sha': sha, 'step': 'unit tests',
                           'at': ci_queue._iso(T0), 'job': 'gate'},
                          {'run': 2, 'branch': branch, 'sha': sha, 'step': 'unit tests',
                           'at': ci_queue._iso(T0), 'job': 'gate'}]
        ci_queue.save(p.name, data)
        self._cancel(p, host)
        stalls = ci_queue.load(p.name)['stalls']
        self.assertEqual(len(stalls), 3)
        self.assertEqual(stalls[-1]['run'], 200)

    def test_the_history_is_pruned_at_forty_eight_hours(self):
        data = {'entries': {}, 'started': [], 'stalls': [
            {'run': 1, 'branch': 'b', 'sha': 's', 'step': 'x', 'job': 'gate',
             'at': ci_queue._iso(T0 - datetime.timedelta(hours=49))},
            {'run': 2, 'branch': 'b', 'sha': 's', 'step': 'x', 'job': 'gate',
             'at': ci_queue._iso(T0 - datetime.timedelta(hours=47))}]}
        pruned = ci_queue.prune(data, T0)
        self.assertEqual([s['run'] for s in pruned['stalls']], [2])


if __name__ == '__main__':
    unittest.main()
