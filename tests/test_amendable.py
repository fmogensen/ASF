"""The amendable set (F-0024): named once, every glob derived from the module that owns it."""
import importlib
import os
import unittest

from asf import amendable, hooks
from asf.env import Product
from asf.record import core
build = importlib.import_module('asf.briefs.build')  # `asf.briefs.build` the attribute is a function

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def product(**conventions):
    return Product('demo', {'main': 'main', 'conventions': conventions})


class SetTests(unittest.TestCase):
    def kinds(self, **conventions):
        return {k.name: k for k in amendable.kinds(product(**conventions))}

    def test_the_six_kinds_in_order(self):
        self.assertEqual([k.name for k in amendable.kinds(product())],
                         ['rule_cards', 'checks', 'hooks', 'role_agents', 'briefs', 'evals'])
        for k in amendable.kinds(product()):
            self.assertTrue(k.why)

    def test_every_glob_is_the_constant_it_came_from(self):
        rules = core.TYPES['rule'][0]
        k = self.kinds(briefs_dir='docs/briefs', evals_dir='qa')
        self.assertEqual(k['rule_cards'].globs, (f'{rules}/*.md', f'{rules}/index.json'))
        self.assertEqual(k['checks'].globs, (f'{rules}/*.sh', hooks.CHECKS_DIR + '/*'))
        self.assertEqual(k['hooks'].globs,
                         tuple(hooks.RUNTIME_SETTINGS_GLOBS) + tuple(hooks.GIT_HOOK_GLOBS))
        templates = os.path.relpath(build.TEMPLATES_DIR, REPO_ROOT)
        self.assertEqual(k['role_agents'].globs,
                         (templates + '/*.md',) + tuple(hooks.RUNTIME_AGENT_GLOBS))
        self.assertEqual(k['briefs'].globs, ('docs/briefs/*',))
        self.assertEqual(k['evals'].globs, ('qa/*',))

    def test_evals_default_and_no_briefs_glob_when_unset(self):
        k = self.kinds()
        self.assertEqual(k['evals'].globs, ('evals/*',))
        self.assertEqual(k['briefs'].globs, ())

    def test_paths_unset_is_the_defaults(self):
        p = product()
        self.assertEqual(amendable.source(p), 'default')
        self.assertEqual(amendable.paths(p),
                         tuple(g for k in amendable.kinds(p) for g in k.globs))

    def test_paths_a_list_is_that_list(self):
        p = product(amendable_paths=['docs/CONSTITUTION.md'])
        self.assertEqual(amendable.source(p), 'yaml')
        self.assertEqual(amendable.paths(p), ('docs/CONSTITUTION.md',))

    def test_paths_empty_list_is_empty(self):
        p = product(amendable_paths=[])
        self.assertEqual(amendable.source(p), 'yaml')
        self.assertEqual(amendable.paths(p), ())

    def test_kind_of_one_path_per_kind(self):
        p = product(briefs_dir='docs/briefs')
        for path, kind in (
            ('rules/R-0042.md', 'rule_cards'), ('rules/index.json', 'rule_cards'),
            ('rules/R-0042.sh', 'checks'), ('tools/checks/x.sh', 'checks'),
            ('.githooks/pre-commit', 'hooks'), ('.claude/settings.json', 'hooks'),
            ('asf/briefs/templates/coder.md', 'role_agents'),
            ('.claude/agents/coder.md', 'role_agents'),
            ('docs/briefs/coder.md', 'briefs'), ('evals/e1.yaml', 'evals'),
        ):
            self.assertEqual(amendable.kind_of(p, path).name, kind, path)

    def test_kind_of_none_outside_the_set(self):
        p = product(briefs_dir='docs/briefs')
        for path in ('docs/specs/f-0024.md', 'plugin/skills/status/SKILL.md', 'asf/approvals.py'):
            self.assertIsNone(amendable.kind_of(p, path), path)

    def test_reaches(self):
        p = product()
        self.assertEqual(amendable.reaches(p, ['rules/*']), 'rules/*')
        self.assertEqual(amendable.reaches(p, ['rules/R-0042.md']), 'rules/R-0042.md')
        self.assertIsNone(amendable.reaches(p, ['docs/**']))
        self.assertEqual(amendable.reaches(p, ['docs/**', 'rules/*']), 'rules/*')

    def test_write_target(self):
        p = product()
        hit = amendable.write_target(p, 'Write', {'file_path': 'rules/R-0099.md'}, REPO_ROOT)
        self.assertEqual((hit[0], hit[1].name), ('rules/R-0099.md', 'rule_cards'))
        hit = amendable.write_target(
            p, 'Bash', {'command': 'echo x > .githooks/pre-commit'}, REPO_ROOT)
        self.assertEqual(hit[1].name, 'hooks')
        self.assertIsNone(
            amendable.write_target(p, 'Bash', {'command': 'cat rules/R-0042.md'}, REPO_ROOT))
        self.assertIsNone(amendable.write_target(
            p, 'Write', {'file_path': 'docs/specs/f-0024.md'}, REPO_ROOT))

    def test_format_set_has_a_row_per_kind(self):
        rows = amendable.format_set(product())
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0].startswith('rule_cards'))


if __name__ == '__main__':
    unittest.main()
