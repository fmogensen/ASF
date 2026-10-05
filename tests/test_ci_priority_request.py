"""An ``asf land --priority`` request's own PR run is S1 in the CI start queue
(:func:`asf.ci_queue.priority`, :func:`asf.ci_queue.land_priority`): ahead in line and never a
relief candidate (2026-10-05: S1 relief cancelled a priority request's PR run three times)."""
import os
import unittest

from asf import ci_queue, env, merge_queue

from tests.test_ci_queue import TestS1PrRelief


class PriorityRequestRun(TestS1PrRelief):
    def ask(self, *branches, priority=True):
        reqs = {str(100 + i): {'pr': 100 + i, 'branch': b, 'at': '2026-10-05T00:00:00Z',
                               **({'priority': True} if priority else {})}
                for i, b in enumerate(branches)}
        merge_queue.save_requests(env.state_dir('p'), reqs)

    def test_a_priority_request_s_branch_is_s1_a_plain_request_is_not(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.assertNotEqual(ci_queue.priority('T-0341', {}, 'task/T-0341', product=p)[0],
                            ci_queue.S1)
        self.ask('task/T-0341', priority=False)
        self.assertNotEqual(ci_queue.priority('T-0341', {}, 'task/T-0341', product=p)[0],
                            ci_queue.S1)
        self.ask('task/T-0341')
        self.assertEqual(ci_queue.priority('T-0341', {}, 'task/T-0341', product=p),
                         (ci_queue.S1, ci_queue.LAND_PRIORITY))
        self.assertEqual(ci_queue.priority('T-0500', {}, 'task/T-0500', product=p)[0],
                         ci_queue.OTHER)

    def test_s1_relief_never_cancels_a_priority_request_s_run(self):
        p = self.product()
        os.makedirs(env.state_dir('p'), exist_ok=True)
        self.ask('task/T-0341', 'task/T-0500')
        self.seed(self.t0)
        gh, run = self.gh(self.runs(), jobs=self.jobs())
        cancelled, _rerun = self.relieve(p, run)
        self.assertEqual(self.cancels(gh), [])
        self.assertEqual(cancelled, 0)
        self.assertIn(f'relief: exempt task/T-0341 — {ci_queue.LAND_PRIORITY} request, '
                      'never cancelled', self.lines)


def load_tests(loader, tests, pattern):
    """This module's own tests only — never the imported base class's again."""
    return loader.loadTestsFromNames(
        [f'{__name__}.PriorityRequestRun.{n}' for n in dir(PriorityRequestRun)
         if n.startswith('test_') and n in vars(PriorityRequestRun)])


if __name__ == '__main__':
    unittest.main()
