"""A cancelled required check never routes a Task to a correct round (:mod:`asf.harvest.lane`).

A run the CI queue's relief cancelled judged no code. A job cut mid-step reads ``failure`` with
the host's runner-loss annotation ("The operation was canceled."); read as red, the lane sent the
Task to a correct session, which found nothing to fix, ended empty and parked the Task
(2026-10-05). It is re-run or waited on instead.
"""
import json
import unittest
from unittest import mock

from asf.harvest import harvest, lane

from tests.test_harvest import ProductHarvestTests

LINK = 'https://github.com/o/p/actions/runs/9/job/77'


class CancelledCheck(ProductHarvestTests):
    def test_a_check_cut_short_by_a_cancel_waits_and_is_re_run_never_corrected(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'fail', 'link': LINK}])
        real = harvest._gh.side_effect

        def gh(args):
            if args[:2] == ['api', 'repos/o/p/check-runs/77/annotations']:
                calls.append(list(args))
                return 0, json.dumps([{'annotation_level': 'failure',
                                       'message': 'The operation was canceled.'}]), ''
            if args[:2] == ['run', 'rerun']:
                calls.append(list(args))
                return 0, '', ''
            return real(args)
        harvest._gh.side_effect = gh
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results.get('fix/B-0001'), 'waiting', lines)
        self.assertFalse(self.record('fix/B-0001').get('correction'), lines)
        self.assertEqual(self.merges(calls), [])
        self.assertTrue(any('cut short' in l and 'never a correct round' in l for l in lines),
                        lines)
        self.assertIn(['run', 'rerun', '--job', '77', '-R', 'o/p'], calls)

    def test_a_real_failure_still_goes_back(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'fail', 'link': LINK}])
        real = harvest._gh.side_effect

        def gh(args):
            if args[:2] == ['api', 'repos/o/p/check-runs/77/annotations']:
                calls.append(list(args))
                return 0, json.dumps([{'annotation_level': 'failure',
                                       'message': 'Process completed with exit code 1.'}]), ''
            return real(args)
        harvest._gh.side_effect = gh
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results.get('fix/B-0001'), 'held', lines)
        self.assertEqual(self.record('fix/B-0001')['correction']['kind'], 'gate')


class HeldRerunIsPending(unittest.TestCase):
    def test_a_failed_check_of_a_run_the_ci_queue_holds_to_re_run_is_pending(self):
        rollup = [{'name': 'gate', 'bucket': 'fail', 'link': LINK}]
        with mock.patch.object(lane.pr_graph, 'checks_for', return_value=rollup):
            state, detail, _c = lane.pr_checks('o/p', 41, ('gate',), rerun=[9])
            self.assertEqual(state, 'pending', detail)
            self.assertIn('re-run queued', detail)
            state, _d, _c = lane.pr_checks('o/p', 41, ('gate',), rerun=[])
            self.assertEqual(state, 'red')


def load_tests(loader, tests, pattern):
    """This module's own tests only — never the imported base class's again."""
    names = [f'{__name__}.CancelledCheck.{n}' for n in vars(CancelledCheck) if n.startswith('test_')]
    names += [f'{__name__}.HeldRerunIsPending.{n}' for n in vars(HeldRerunIsPending)
              if n.startswith('test_')]
    return loader.loadTestsFromNames(names)


if __name__ == '__main__':
    unittest.main()
