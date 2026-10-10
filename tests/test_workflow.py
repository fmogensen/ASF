"""The CI workflow itself: the push trigger names the trunk, not every branch (B-0053) — a push
to a worker branch must not fire the suite; the pull_request trigger stays for outside
contributors and ``landing: pull-request`` products."""
import glob
import os
import re
import sys
import unittest

from asf import env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, '.github', 'workflows', 'tests.yml')
MERGE_PR_SCRIPT = os.path.join(REPO, 'tools', 'merge-pr.sh')
sys.path.insert(0, os.path.join(REPO, 'tools'))
import ci_paths  # noqa: E402


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
        self.assertEqual(set(jobs), {'changes', 'checks', 'part', 'tests'})
        m = re.search(r'MERGE_PR_CHECKS:-([^}]+)\}', self.merge_pr_text)
        checks = m.group(1).split(';')
        pythons = jobs['tests']['strategy']['matrix']['python']
        self.assertEqual(jobs['tests']['name'], 'tests (${{ matrix.python }})')
        self.assertEqual(checks, [f'tests ({py})' for py in pythons])
        self.assertEqual(sorted(jobs['tests']['needs']), ['changes', 'checks', 'part'])
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



class DocsOnlyFastPath(unittest.TestCase):
    """A pull request touching only the Markdown records no test reads skips `checks` and every
    `part`; `tests (<python>)` still runs and reports. Anything else, or nothing known, is a full
    run, and a push to main is never docs-only."""

    def setUp(self):
        with open(WORKFLOW) as f:
            self.data = env.loads(f.read())

    def test_docs_only_is_markdown_under_the_records_only(self):
        self.assertTrue(ci_paths.docs_only(['docs/specs/f-0001.md', 'docs/plans/f-0001.md']))
        self.assertTrue(ci_paths.docs_only(['docs/decisions/0001-x.md', 'docs/reviews/1-t.md']))
        for files in ([], None, ['README.md'], ['docs/guide/operating.md'],
                      ['docs/KNOWN-ISSUES.md'], ['docs/specs/f-0069.stories.sh'],
                      ['docs/specs/f-0001.md', 'asf/tick.py'], ['.github/workflows/tests.yml'],
                      ['docs/specsx/a.md']):
            self.assertFalse(ci_paths.docs_only(files), files)

    def test_no_test_reads_a_docs_only_record_from_this_checkout(self):
        dirs = '|'.join(d.split('/')[1] for d in ci_paths.DOCS_ONLY_DIRS)
        rooted = re.compile(r"(REPO|ROOT)\w*\b.*(['\"]docs['\"]\s*,\s*['\"](%s)['\"]|docs/(%s)/)"
                            % (dirs, dirs))
        hits = []
        for path in sorted(glob.glob(os.path.join(REPO, 'tests', '*.py'))):
            with open(path, encoding='utf-8') as f:
                for n, line in enumerate(f, 1):
                    if rooted.search(line) and 'file_path' not in line:
                        hits.append(f'{os.path.basename(path)}:{n}')
        self.assertEqual(hits, [], 'a test reads a docs-only record: narrow DOCS_ONLY_DIRS')

    def test_the_heavy_jobs_skip_on_docs_only_and_run_when_unknown(self):
        jobs = self.data['jobs']
        for name in ('checks', 'part'):
            self.assertEqual(jobs[name]['needs'], 'changes')
            cond = jobs[name]['if']
            self.assertIn('!cancelled()', cond)
            self.assertIn("needs.changes.outputs.docs_only != 'true'", cond)

    def test_the_required_checks_report_on_both_paths(self):
        steps = self.data['jobs']['tests']['steps']
        gather = [s for s in steps if 'run' in s and '--gather' in s['run']]
        self.assertEqual(len(gather), 1)
        self.assertEqual(gather[0]['if'], "needs.changes.outputs.docs_only != 'true'")
        fast = [s for s in steps if s.get('if') == "needs.changes.outputs.docs_only == 'true'"]
        self.assertEqual(len(fast), 1)
        self.assertIn('test "$CHANGES" = success', fast[0]['run'])

    def test_the_fast_path_runs_the_tree_wide_checks(self):
        runs = [s.get('run') for s in self.data['jobs']['changes']['steps']]
        for check in ('bash tools/check_factory_only.sh', 'bash tools/check_generic.sh',
                      'bash tools/check_privacy.sh'):
            self.assertIn(check, runs)

    def test_concurrency_cancels_pr_runs_and_queues_one_main_run(self):
        c = self.data['concurrency']
        self.assertIn('github.head_ref', c['group'])
        self.assertEqual(c['cancel-in-progress'], "${{ github.event_name == 'pull_request' }}")


if __name__ == '__main__':
    unittest.main()
