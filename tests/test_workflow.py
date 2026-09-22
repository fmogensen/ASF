"""The CI workflow itself: the push trigger names the trunk, not every branch (B-0053) — a push
to a worker branch must not fire the suite; the pull_request trigger stays for outside
contributors and ``landing: pull-request`` products."""
import os
import unittest

from asf import env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, '.github', 'workflows', 'tests.yml')


class WorkflowTriggerTest(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW) as f:
            self.text = f.read()
        self.data = env.loads(self.text)

    def test_push_trigger_is_limited_to_the_trunk(self):
        push = self.data['on']['push']
        self.assertIsInstance(push, dict, f"push: {push!r} — fires on every branch, not just main")
        self.assertEqual(push.get('branches'), ['main'])

    def test_pull_request_trigger_is_kept(self):
        self.assertIn('pull_request', self.data['on'])


if __name__ == '__main__':
    unittest.main()
