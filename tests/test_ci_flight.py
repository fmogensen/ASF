"""asf.ci_flight — the reader of a branch's live run, the urgency read, and the verdict table.
Every test runs with no ``gh`` and no network: the ``run=`` seam or a fake ``Flight`` stands in."""
import subprocess
import unittest
from unittest import mock

from asf import ci_flight, ci_queue, env


def product(workflow='ci.yml', repo_slug='o/r', name='p'):
    data = {}
    if workflow is not None or repo_slug is not None:
        data['ci'] = {'workflow': workflow} if workflow is not None else {}
    if repo_slug is not None:
        data['repo_slug'] = repo_slug
    return env.Product(name, data)


class FakeGh:
    """``subprocess.run`` for ``gh run list``: canned rows, argv logged."""

    def __init__(self, rows=(), rc=0, out=None):
        self.rows = rows
        self.rc = rc
        self.out = out
        self.calls = []

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        import json
        out = self.out if self.out is not None else json.dumps(list(self.rows))
        return subprocess.CompletedProcess(argv, self.rc, out, '')


class RunInFlight(unittest.TestCase):
    def test_the_newest_not_completed_run_is_returned(self):
        gh = FakeGh(rows=[
            {'databaseId': 1, 'status': 'queued', 'createdAt': '2026-09-26T18:00:00Z'},
            {'databaseId': 2, 'status': 'in_progress', 'createdAt': '2026-09-26T18:05:00Z'}])
        got = ci_flight.run_in_flight(product(), 'worker/B-1382', run=gh)
        self.assertEqual(got, {'id': 2, 'status': 'in_progress'})

    def test_a_completed_run_among_others_is_never_chosen(self):
        gh = FakeGh(rows=[
            {'databaseId': 1, 'status': 'queued', 'createdAt': '2026-09-26T18:00:00Z'},
            {'databaseId': 2, 'status': 'completed', 'createdAt': '2026-09-26T18:05:00Z'}])
        got = ci_flight.run_in_flight(product(), 'worker/B-1382', run=gh)
        self.assertEqual(got, {'id': 1, 'status': 'queued'})

    def test_the_newest_by_createdat_wins_even_printed_second(self):
        gh = FakeGh(rows=[
            {'databaseId': 2, 'status': 'queued', 'createdAt': '2026-09-26T18:05:00Z'},
            {'databaseId': 1, 'status': 'queued', 'createdAt': '2026-09-26T18:00:00Z'}])
        got = ci_flight.run_in_flight(product(), 'worker/B-1382', run=gh)
        self.assertEqual(got, {'id': 2, 'status': 'queued'})

    def test_no_rows_is_none(self):
        gh = FakeGh(rows=[])
        self.assertIsNone(ci_flight.run_in_flight(product(), 'worker/B-1382', run=gh))

    def test_no_ci_workflow_is_none_and_gh_is_never_called(self):
        gh = FakeGh(rows=[{'databaseId': 1, 'status': 'queued', 'createdAt': ''}])
        self.assertIsNone(ci_flight.run_in_flight(product(workflow=None), 'b', run=gh))
        self.assertEqual(gh.calls, [])

    def test_no_repo_slug_is_none_and_gh_is_never_called(self):
        gh = FakeGh(rows=[{'databaseId': 1, 'status': 'queued', 'createdAt': ''}])
        self.assertIsNone(ci_flight.run_in_flight(product(repo_slug=None), 'b', run=gh))
        self.assertEqual(gh.calls, [])

    def assertUnknown(self, got, reason=None):
        self.assertTrue(ci_flight.is_unknown(got), got)
        if reason is not None:
            self.assertIn(reason, got.reason)

    def test_gh_exiting_nonzero_is_unknown_never_none(self):
        gh = FakeGh(rc=1, out='error')
        self.assertUnknown(ci_flight.run_in_flight(product(), 'b', run=gh), 'rc 1')

    def test_gh_timing_out_is_unknown(self):
        def run(*_a, **_kw):
            raise subprocess.TimeoutExpired('gh', 30)
        self.assertUnknown(ci_flight.run_in_flight(product(), 'b', run=run), 'timeout')

    def test_gh_raising_oserror_is_unknown(self):
        def run(*_a, **_kw):
            raise OSError('no gh binary')
        self.assertUnknown(ci_flight.run_in_flight(product(), 'b', run=run), 'not runnable')

    def test_garbage_output_is_unknown(self):
        gh = FakeGh(out='not json')
        self.assertUnknown(ci_flight.run_in_flight(product(), 'b', run=gh), 'bad json')

    def test_a_non_list_answer_is_unknown(self):
        gh = FakeGh(out='{"x": 1}')
        self.assertUnknown(ci_flight.run_in_flight(product(), 'b', run=gh), 'bad json')

    def test_a_rate_limit_is_unknown_and_never_raises(self):
        from asf import gh_limit
        def run(argv, **_kw):
            return subprocess.CompletedProcess(argv, 1, '', 'API rate limit exceeded')
        try:
            self.assertUnknown(ci_flight.run_in_flight(product(), 'b', run=run), 'rate limited')
        finally:
            gh_limit.reset()

    def test_the_argv_names_the_flags_and_the_branch_asked_for(self):
        gh = FakeGh(rows=[])
        ci_flight.run_in_flight(product(workflow='ci.yml', repo_slug='o/r'), 'worker/B-1382',
                                 run=gh)
        [argv] = gh.calls
        self.assertIn('-R', argv)
        self.assertIn('o/r', argv)
        self.assertIn('--workflow', argv)
        self.assertIn('ci.yml', argv)
        self.assertIn('--branch', argv)
        self.assertIn('worker/B-1382', argv)
        i = argv.index('--json')
        self.assertEqual(argv[i + 1], 'databaseId,status,createdAt')


class Urgent(unittest.TestCase):
    def test_a_hotfix_branch_name_is_urgent_on_its_own(self):
        self.assertTrue(ci_flight.urgent('hotfix/x'))

    def test_severity_s1_is_urgent(self):
        self.assertTrue(ci_flight.urgent('worker/B-1', severity='S1'))

    def test_severity_s2_is_not_urgent(self):
        self.assertFalse(ci_flight.urgent('worker/B-1', severity='S2'))

    def test_no_severity_and_no_hotfix_name_is_not_urgent(self):
        self.assertFalse(ci_flight.urgent('worker/B-1', severity=None))

    def test_with_items_the_answer_is_ci_queue_prioritys(self):
        items = {
            'B-0007': {'id': 'B-0007', 'type': 'bug', 'severity': 'S1'},
            'B-0008': {'id': 'B-0008', 'type': 'bug', 'severity': 'S2'},
            'H-0001': {'id': 'H-0001', 'type': 'hotfix'},
        }
        self.assertTrue(ci_flight.urgent('worker/B-0007', item='B-0007', items=items))
        self.assertTrue(ci_flight.urgent('worker/H-0001', item='H-0001', items=items))
        self.assertFalse(ci_flight.urgent('worker/B-0008', item='B-0008', items=items))
        self.assertEqual(ci_queue.priority('B-0007', items, branch='worker/B-0007')[0],
                          ci_queue.S1)


class _Flight:
    def __init__(self, ans):
        self.ans = ans

    def read(self, product, branch):
        return self.ans


class RaisingFlight:
    def read(self, product, branch):
        raise AssertionError('run_in_flight must not be consulted with nothing in flight')


class Verdict(unittest.TestCase):
    RUN = {'id': 36262385912, 'status': 'in_progress'}

    def test_nothing_in_flight_is_always_a_push_whatever_needed_says(self):
        flight = _Flight(None)
        for needed in (None, ci_flight.CONFLICT, ci_flight.RED, ci_flight.STRICT):
            got = ci_flight.verdict(None, 'worker/B-1382', 'rebase', needed=needed,
                                     severity='S1', flight=flight)
            self.assertEqual(got, '')

    def test_nothing_in_flight_never_consults_urgent(self):
        flight = _Flight(None)
        with mock.patch.object(ci_flight, 'urgent', side_effect=AssertionError('consulted')):
            got = ci_flight.verdict(None, 'worker/B-1382', 'rebase', needed=None, flight=flight)
        self.assertEqual(got, '')

    def test_in_flight_not_urgent_no_exception_defers(self):
        flight = _Flight(self.RUN)
        got = ci_flight.verdict(None, 'worker/B-1382', 'rebase', needed=None, flight=flight)
        self.assertEqual(got, 'rebase deferred: worker/B-1382 CI in flight (run 36262385912)')

    def test_in_flight_not_urgent_any_named_exception_pushes(self):
        flight = _Flight(self.RUN)
        for needed in (ci_flight.CONFLICT, ci_flight.RED, ci_flight.STRICT):
            got = ci_flight.verdict(None, 'worker/B-1382', 'rebase', needed=needed, flight=flight)
            self.assertEqual(got, '')

    def test_in_flight_urgent_conflict_pushes(self):
        flight = _Flight(self.RUN)
        got = ci_flight.verdict(None, 'worker/B-1382', 'rebase', needed=ci_flight.CONFLICT,
                                 severity='S1', flight=flight)
        self.assertEqual(got, '')

    def test_in_flight_urgent_red_or_strict_defers(self):
        flight = _Flight(self.RUN)
        for needed in (ci_flight.RED, ci_flight.STRICT, None):
            got = ci_flight.verdict(None, 'worker/B-1382', 'sign-off', needed=needed,
                                     severity='S1', flight=flight)
            self.assertEqual(got, 'sign-off deferred: worker/B-1382 CI in flight (run '
                                   '36262385912)')

    def test_an_unknown_read_defers_whatever_exception_or_urgency(self):
        from asf import github
        flight = _Flight(github.unknown('timeout'))
        for needed in (None, ci_flight.CONFLICT, ci_flight.RED, ci_flight.STRICT):
            for severity in (None, 'S1'):
                got = ci_flight.verdict(None, 'worker/B-1382', 'rebase', needed=needed,
                                         severity=severity, flight=flight)
                self.assertEqual(got, 'rebase deferred: worker/B-1382 CI in flight unknown '
                                      '(timeout)')

    def test_in_flight_urgent_by_hotfix_name_red_defers(self):
        flight = _Flight(self.RUN)
        got = ci_flight.verdict(None, 'hotfix/x', 'sign-off', needed=ci_flight.RED, flight=flight)
        self.assertEqual(got, 'sign-off deferred: hotfix/x CI in flight (run 36262385912)')


class FlightMemo(unittest.TestCase):
    def test_one_read_per_branch_for_the_pass_including_a_remembered_none(self):
        calls = []

        def fake(product, branch, run=None, timeout=None):
            calls.append(branch)
            return None if branch == 'b-none' else {'id': 1, 'status': 'queued'}

        with mock.patch.object(ci_flight, 'run_in_flight', side_effect=fake):
            flight = ci_flight.Flight()
            self.assertEqual(flight.read(None, 'b'), {'id': 1, 'status': 'queued'})
            self.assertEqual(flight.read(None, 'b'), {'id': 1, 'status': 'queued'})
            self.assertIsNone(flight.read(None, 'b-none'))
            self.assertIsNone(flight.read(None, 'b-none'))
        self.assertEqual(calls, ['b', 'b-none'])


if __name__ == '__main__':
    unittest.main()
