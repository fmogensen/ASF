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
from tests.test_ci_phantom import HEAVY, Host, job, product


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
        self.assertEqual({r['id'] for r in runs}, {900, 110, 111, 112})
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
                self.assertEqual(len(calls), 2, calls)

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


if __name__ == '__main__':
    unittest.main()
