"""B-0060: a plan the lane landed on the trunk becomes Task cards in the tick's record step —
before this, a Feature sat at plan-approved for ever with nothing for the feeder to launch."""
import os
import shutil
import tempfile
import unittest

from asf.record import frontmatter
from asf.record import plan_tasks

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks'}
PLAN = """# Plan F-0001

## Decisions
none

### Task 1: the reader
stories: S-0001, S-0009
writes: asf/record/reader.py, tests/test_reader.py

**Files**: asf/record/reader.py
**Steps**: write it
**Gate**: python3 -m unittest tests.test_reader

### Task 2: the table
stories: S-0001
writes: asf/views/table.py

**Steps**: draw it

coverage: 1/1 stories; uncovered: none
"""


def write_item(root, id_, type_, title, parent=None):
    lines = [f'id: {id_}', f'type: {type_}', f'title: {title}']
    if parent:
        lines.append(f'parent: {parent}')
    lines += ['# ---- machine ----', 'state: New', 'stage_since: 2026-09-01T00:00:00Z',
              'updated: 2026-09-01T00:00:00Z']
    path = os.path.join(root, FOLDER_OF[type_], f'{id_}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n## Description\n\n## History\n- 2026-09-01: created\n')


def read(root, type_, id_):
    with open(os.path.join(root, FOLDER_OF[type_], f'{id_}.md'), encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{id_}.md')


class PlanTasksTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='plan_tasks_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Read it', parent='F-0001')
        self.lines = []

    def ev(self, on_main=True):
        return {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                        'plan_on_main': on_main, 'spec_on_main': True}}}

    def mint(self, ev=None, text=PLAN):
        return plan_tasks.mint_plan_tasks(self.root, None, ev or self.ev(), out=self.lines.append,
                                          read_ref=lambda ref: text)

    def test_a_landed_plan_becomes_task_cards_once(self):
        made = self.mint()
        self.assertEqual(made, ['T-0001', 'T-0002'])
        meta, body = read(self.root, 'task', 'T-0001')
        self.assertEqual(meta['title'], 'the reader')
        self.assertEqual(meta['parent'], 'F-0001')
        self.assertIs(meta['decided'], True)
        self.assertEqual(meta['writes'], ['asf/record/reader.py', 'tests/test_reader.py'])
        self.assertEqual(meta['stories'], ['S-0001'])  # S-0009 is not in the record
        self.assertEqual(meta['links'], {'plan': 'docs/plans/f-0001.md'})
        self.assertEqual(meta['state'], 'New')
        self.assertIn('**Gate**: python3 -m unittest tests.test_reader', body)
        self.assertIn('created (plan F-0001)', body)
        self.assertEqual(read(self.root, 'task', 'T-0002')[0]['writes'], ['asf/views/table.py'])
        self.assertEqual(self.lines, ['plan-tasks: F-0001: 2 Task(s) from docs/plans/f-0001.md: T-0001, T-0002'])
        # read once: the second run mints nothing
        self.assertEqual(self.mint(), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, 'tasks'))), ['T-0001.md', 'T-0002.md'])

    def test_a_plan_still_on_its_branch_mints_nothing(self):
        self.assertEqual(self.mint(self.ev(on_main=False)), [])
        self.assertEqual(os.listdir(os.path.join(self.root, 'tasks')), [])

    def test_a_plan_without_task_headings_is_named_not_minted(self):
        self.assertEqual(self.mint(text='# Plan\n\nprose only\n'), [])
        self.assertEqual(self.lines, ['plan-tasks: F-0001: docs/plans/f-0001.md has no `### Task N:` heading — nothing to mint'])


if __name__ == '__main__':
    unittest.main()
