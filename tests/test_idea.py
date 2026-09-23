"""tests.test_idea — `asf idea` (F-0023). `IdeaToGroomTest` is T-0058's: a filed tree, groomed,
is an Epic with Features, Stories and acceptance."""
import os
import shutil
import unittest

from asf.init import DEFAULT_INTAKE_DIR
from tests.test_inbox_shape import make_repo, run


class IdeaToGroomTest(unittest.TestCase):
    """The intake cards a tree files — one `.md` per node, parents by `inbox:<file>` (D3) —
    become one Epic, its Features and their Stories, each Story with acceptance."""

    def setUp(self):
        self.root = make_repo()
        self.intake = os.path.join(self.root, DEFAULT_INTAKE_DIR)
        cards = {
            '01-epic.md': "# Self-serve billing\n\nCustomers pay themselves.\n\n"
                          "## Features\n- Plans\n- Invoices\n\n## Assumptions\n- one currency\n",
            '02-plans.md': "# Plans\nparent: inbox:01-epic.md\n\nThe plans page.\n",
            '03-invoices.md': "# Invoices\nparent: inbox:01-epic.md\n\nThe invoice list.\n",
            '04-tiers.md': "# Plan tiers\nparent: inbox:02-plans.md\n\nTiers.\n\n"
                           "## Acceptance\n- [ ] python3 -m unittest tests.tiers\n",
            '05-list.md': "# Invoice list\nparent: inbox:03-invoices.md\n\nThe list.\n\n"
                          "## Acceptance\n- [ ] python3 -m unittest tests.invoices\n",
        }
        for name, text in cards.items():
            with open(os.path.join(self.intake, name), 'w', encoding='utf-8') as f:
                f.write(text)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _read(self, folder, iid):
        with open(os.path.join(self.root, folder, f"{iid}.md"), encoding='utf-8') as f:
            return f.read()

    def test_a_filed_tree_becomes_an_epic_with_features_stories_and_acceptance(self):
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(self.intake)), ['done'])
        for folder, ids in (('epics', ['E-0001.md']), ('features', ['F-0001.md', 'F-0002.md']),
                            ('stories', ['S-0001.md', 'S-0002.md'])):
            self.assertEqual(sorted(os.listdir(os.path.join(self.root, folder))), ids)
        self.assertIn('## Assumptions\n- one currency\n', self._read('epics', 'E-0001'))
        self.assertIn('parent: E-0001', self._read('features', 'F-0001'))
        self.assertIn('parent: E-0001', self._read('features', 'F-0002'))
        s1, s2 = self._read('stories', 'S-0001'), self._read('stories', 'S-0002')
        self.assertIn('parent: F-0001', s1)
        self.assertIn('parent: F-0002', s2)
        self.assertIn('- [ ] python3 -m unittest tests.tiers\n', s1)
        self.assertIn('- [ ] python3 -m unittest tests.invoices\n', s2)

    def test_the_groomed_tree_indexes_and_checks(self):
        run(['groom'], self.root)
        r = run(['index'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
