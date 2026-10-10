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
    """F-0253: the factory-only merge rule runs before the install, in a job the required checks
    (``tools/merge-pr.sh``'s own ``MERGE_PR_CHECKS`` default) are green only after. F-0301: the
    required checks are the gathering job ``tests``, which needs ``checks`` and every ``part``."""

    def setUp(self):
        with open(WORKFLOW) as f:
            self.data = env.loads(f.read())
        with open(MERGE_PR_SCRIPT) as f:
            self.merge_pr_text = f.read()

    def test_the_required_checks_name_the_gathering_job(self):
        jobs = self.data['jobs']
        self.assertEqual(set(jobs), {'checks', 'part', 'tests'})
        m = re.search(r'MERGE_PR_CHECKS:-([^}]+)\}', self.merge_pr_text)
        checks = m.group(1).split(';')
        pythons = jobs['tests']['strategy']['matrix']['python']
        self.assertEqual(jobs['tests']['name'], 'tests (${{ matrix.python }})')
        self.assertEqual(checks, [f'tests ({py})' for py in pythons])
        self.assertEqual(sorted(jobs['tests']['needs']), ['checks', 'part'])
        self.assertEqual(jobs['tests']['if'], 'always()')  # a skipped job reads green

    def test_every_python_is_checked_and_split_into_every_part(self):
        jobs = self.data['jobs']
        pythons = jobs['tests']['strategy']['matrix']['python']
        self.assertEqual(jobs['checks']['strategy']['matrix']['python'], pythons)
        self.assertEqual(jobs['part']['strategy']['matrix']['python'], pythons)
        parts = jobs['part']['strategy']['matrix']['part']
        self.assertEqual(parts, list(range(1, int(self.data['env']['PARTS']) + 1)))

    def test_the_step_runs_before_the_install(self):
        steps = self.data['jobs']['checks']['steps']
        runs = [s.get('run') for s in steps]
        self.assertIn('bash tools/check_factory_only.sh', runs)
        factory_idx = runs.index('bash tools/check_factory_only.sh')
        install_idx = next(i for i, s in enumerate(steps) if s.get('name') == 'install with pipx')
        self.assertLess(factory_idx, install_idx)


if __name__ == '__main__':
    unittest.main()
