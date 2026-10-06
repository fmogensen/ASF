"""tests.test_brief_commands — G2 ask 2: every command a brief tells a session to run — the
code pre-push check, the doc-kind pre-push steps, and the product's declared ``check_commands``
— passes the approvals hook outright, with no approval round. ``PRE_PUSH_RULE`` and
``PRE_PUSH_DOC_RULE`` both claim the hook allows exactly these commands
(:mod:`asf.briefs.build`); this is what makes that claim true rather than aspirational.
"""
import importlib
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import approvals, briefs, env
from tests.test_briefs import REPO_FACTS, ROWS, index

# ``asf.briefs.build`` is both the package's entry function and a submodule; the function wins
# the attribute lookup, so the module is asked for by name (as tests.test_briefs does).
build_mod = importlib.import_module('asf.briefs.build')

JOB = 'code-brief-commands'

#: The product's own conventions: a code pre-push check, doc-kind pre-push steps (``spec``,
#: ``plan``) and the check/test commands beyond those — every one a plain, read-only shell
#: command the grant (``asf/readonly.py``) must recognise.
PRODUCT_YAML = """product: sample
main: main
repo_slug: acme/sample
conventions:
  specs_dir: docs/specs
  plans_dir: docs/plans
  reviews_dir: docs/reviews
  review_pattern: docs/reviews/{n}-{slug}.md
  branch_prefixes:
    spec: spec
    plan: plan
    task: task
    fix: fix
  pre_push_check:
    code: bash tools/check_conventions.sh
    spec:
      - make spec-lint
    plan:
      - make plan-lint
      - make plan-extra
  check_commands:
    - bash tools/check_conventions.sh
    - make lint
"""


class BriefCommandsPassTheHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='brief_commands_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'state', 'sample'))
        with open(env.product_path('sample'), 'w', encoding='utf-8') as f:
            f.write(PRODUCT_YAML)
        self.product = env.load_product('sample')
        self.cwd = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._orig_home

    def call_hook(self, command):
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command},
                              'cwd': self.cwd})
        out, stdout = io.StringIO(), io.StringIO()
        rc = approvals.run_hook(payload, {'ASF_JOB': JOB, 'ASF_PRODUCT': 'sample'},
                                out=out, stdout=stdout)
        return rc, out.getvalue(), stdout.getvalue()

    def assertGranted(self, command):
        rc, out, stdout = self.call_hook(command)
        self.assertEqual(rc, 0, f'{command!r} was refused: {out}')
        self.assertIn('"permissionDecision": "allow"', stdout, command)

    def test_the_code_pre_push_check_passes_the_hook(self):
        command = approvals.pre_push_check(self.product)
        self.assertEqual(command, 'bash tools/check_conventions.sh')
        self.assertGranted(command)

    def test_every_doc_kinds_pre_push_steps_pass_the_hook(self):
        seen = 0
        for kind in approvals.PRE_PUSH_DOC_KEYS:
            for run, _note in approvals.pre_push_steps(self.product, kind):
                seen += 1
                self.assertGranted(run)
        self.assertGreater(seen, 0)  # the fixture's own steps were actually read

    def test_every_declared_check_command_passes_the_hook(self):
        for command in self.product.conventions.get('check_commands'):
            self.assertGranted(command)

    def test_the_code_rule_is_rendered_and_its_claim_holds(self):
        for kind in ('coder', 'fix-bug', 'correct'):
            if kind not in ROWS:
                continue
            with self.subTest(kind=kind):
                text = briefs.build(self.product, ROWS[kind], index(), [], REPO_FACTS).text
                self.assertIn('`bash tools/check_conventions.sh`', text)
                self.assertIn('the approvals hook allows it', text)
                self.assertGranted('bash tools/check_conventions.sh')

    def test_the_doc_rule_is_rendered_and_its_claim_holds(self):
        for kind in ('spec', 'plan'):
            with self.subTest(kind=kind):
                text = briefs.build(self.product, ROWS[kind], index(), [], REPO_FACTS).text
                self.assertIn('the approvals hook allows exactly these commands', text)
                for run, _note in approvals.pre_push_steps(self.product, kind):
                    self.assertIn(f'`{run}`', text)
                    self.assertGranted(run)

    def test_a_mutating_command_is_never_swept_in(self):
        # the grant is additive only: a real push stays governed exactly as before this change
        rc, out, stdout = self.call_hook('git push origin HEAD:main')
        self.assertEqual(stdout, '')


if __name__ == '__main__':
    unittest.main()
