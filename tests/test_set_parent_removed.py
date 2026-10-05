"""tests.test_set_parent_removed — ``asf set`` writes ``parent`` and ``removed``, validated: a
parent must be an existing card of a type that may hold this one, ``removed`` is true/false or a
reason. Anything else is refused and the card is left as it was."""
import shutil
import unittest

from asf.record import frontmatter
from tests.test_backlog import make_repo, run, write_item


class SetParentRemovedTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Feat', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Story', parent='F-0001')
        self.task = write_item(self.root, 'T-0001', 'task', 'Task', parent='F-0001',
                               typed_lines=('writes: [a.py]',))

    def meta(self):
        with open(self.task, encoding='utf-8') as f:
            return frontmatter.parse(f.read())[0]

    def test_parent_moves_to_an_existing_story(self):
        r = run(['set', 'T-0001', 'parent=S-0001'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['parent'], 'S-0001')

    def test_parent_refuses_a_missing_or_wrong_typed_card(self):
        for bad in ('S-0999', 'E-0001', 'nonsense', ''):
            with open(self.task, encoding='utf-8') as f:
                before = f.read()
            r = run(['set', 'T-0001', f'parent={bad}'], self.root)
            self.assertEqual(r.returncode, 2, (bad, r.stdout))
            self.assertIn('parent', r.stderr)
            with open(self.task, encoding='utf-8') as f:
                self.assertEqual(f.read(), before)

    def test_removed_takes_a_bool_or_a_reason(self):
        r = run(['set', 'T-0001', 'removed=true'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIs(self.meta()['removed'], True)
        r = run(['set', 'T-0001', 'removed=superseded by T-0002'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['removed'], 'superseded by T-0002')
        r = run(['set', 'T-0001', 'removed=false'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIs(self.meta()['removed'], False)

    def test_removed_refuses_a_number_or_empty(self):
        for bad in ('3', ''):
            r = run(['set', 'T-0001', f'removed={bad}'], self.root)
            self.assertEqual(r.returncode, 2, (bad, r.stdout))
            self.assertIn('removed', r.stderr)


if __name__ == '__main__':
    unittest.main()
