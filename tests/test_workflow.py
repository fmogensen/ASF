"""The CI workflow itself: the push trigger names the trunk, not every branch (B-0053) — a push
to a worker branch must not fire the suite; the pull_request trigger stays for outside
contributors and ``landing: pull-request`` products."""
import os
import re
import unittest

from asf import env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, '.github', 'workflows', 'tests.yml')
MERGE_PR_SCRIPT = os.path.join(REPO, 'tools', 'merge-pr.sh')


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


class FactoryOnlyStep(unittest.TestCase):
    """F-0253: the factory-only merge rule runs in the one job whose checks are required
    (``tools/merge-pr.sh``'s own ``MERGE_PR_CHECKS`` default), before the install."""

    def setUp(self):
        with open(WORKFLOW) as f:
            self.data = env.loads(f.read())
        with open(MERGE_PR_SCRIPT) as f:
            self.merge_pr_text = f.read()

    def test_the_required_checks_name_the_one_job(self):
        jobs = self.data['jobs']
        self.assertEqual(set(jobs), {'tests'})
        job, = jobs
        m = re.search(r'MERGE_PR_CHECKS:-([^}]+)\}', self.merge_pr_text)
        checks = m.group(1).split(';')
        pythons = jobs[job]['strategy']['matrix']['python']
        self.assertEqual(checks, [f'{job} ({py})' for py in pythons])

    def test_the_step_runs_before_the_install(self):
        steps = self.data['jobs']['tests']['steps']
        runs = [s.get('run') for s in steps]
        self.assertIn('bash tools/check_factory_only.sh', runs)
        factory_idx = runs.index('bash tools/check_factory_only.sh')
        install_idx = next(i for i, s in enumerate(steps) if s.get('name') == 'install with pipx')
        self.assertLess(factory_idx, install_idx)


if __name__ == '__main__':
    unittest.main()
