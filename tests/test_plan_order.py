"""The order a plan states reaches the Task cards as ``after:`` — at mint, by backfill for cards
minted without it, and as the wave's guard when a card still lacks it. Before this every Task of a
plan launched at once, and each successor's coder ended `empty branch: nothing to land`."""
import os
import shutil
import tempfile
import unittest

from asf.feeder import rows as feeder_rows
from asf.record import plan_order, plan_tasks
from tests.test_plan_tasks import FOLDERS, read, write_item

WAVES_PLAN = """# Plan F-0001

Wave order: **(1, 2) → 3 → 4**.

### Task 1: the reader
stories: S-0001
writes: a.py

### Task 2: the writer
stories: S-0001
writes: b.py

### Task 3: the table
stories: S-0001
writes: c.py

### Task 4: the view
stories: S-0001
writes: d.py
after: Task 1

coverage: 1/1 stories; uncovered: none
"""

TABLE_PLAN = """# Plan F-0002

| wave | Task | stories | runs with | runs after |
|---|---|---|---|---|
| 1 | Task 1 — the gatherer | S-1 | Task 3 | — |
| 1 | Task 3 — the stream | S-2 | Task 1 | — |
| 2 | Task 2 — the launch path | S-3 | Task 4 | Task 1 |
| 2 | Task 4 — the report | S-4 | Task 2 | Task 3 |

### Task 1: the gatherer
writes: a.py

### Task 2: the launch path
writes: b.py

### Task 3: the stream
writes: c.py

### Task 4: the report
writes: d.py
"""

PROSE_PLAN = """# Plan

### Task 1: one
writes: a.py

### Task 2: two
writes: b.py

Runs after Task 1: it imports what Task 1 writes.

### Task 3: three
writes: c.py

Independent of Tasks 1 and 2; starts in wave 1.

### Task 4: four
writes: d.py
after: none
"""


class TaskOrderTests(unittest.TestCase):
    def test_wave_order_line_and_an_explicit_after_line_wins(self):
        self.assertEqual(plan_order.task_order(WAVES_PLAN), {1: [], 2: [], 3: [1, 2], 4: [1]})

    def test_the_wave_table_runs_after_column(self):
        self.assertEqual(plan_order.task_order(TABLE_PLAN), {1: [], 2: [1], 3: [], 4: [3]})

    def test_prose_and_none(self):
        self.assertEqual(plan_order.task_order(PROSE_PLAN), {1: [], 2: [1], 3: [], 4: []})

    def test_a_plan_that_states_no_order_stays_parallel(self):
        text = '### Task 1: a\nwrites: a.py\n\n### Task 2: b\nwrites: b.py\n'
        self.assertEqual(plan_order.task_order(text), {1: [], 2: []})

    def test_a_cycle_is_dropped_not_waited_on_for_ever(self):
        text = ('### Task 1: a\nafter: Task 2\n\n### Task 2: b\nafter: Task 1\n')
        order = plan_order.task_order(text)
        self.assertFalse(order[1] and order[2], order)

    def test_card_ids_in_an_after_line_are_not_read_as_task_numbers(self):
        self.assertEqual(plan_order.task_order('### Task 1: a\n\n### Task 2: b\nafter: [T-0001]\n'),
                         {1: [], 2: []})


class RecordCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='plan_order_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Read it', parent='F-0001')
        self.lines = []


class MintOrderTests(RecordCase):
    def test_minted_cards_carry_the_plans_order(self):
        ev = {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md', 'plan_on_main': True}}}
        made = plan_tasks.mint_plan_tasks(self.root, None, ev, out=self.lines.append,
                                          read_ref=lambda ref: WAVES_PLAN)
        self.assertEqual(made, ['T-0001', 'T-0002', 'T-0003', 'T-0004'])
        after = {i: read(self.root, 'task', i)[0].get('after') for i in made}
        self.assertEqual(after, {'T-0001': None, 'T-0002': None, 'T-0003': ['T-0001', 'T-0002'],
                                 'T-0004': ['T-0001']})


def write_task(root, id_, title, state='New', after=None, plan='docs/plans/f-0001.md'):
    lines = [f'id: {id_}', 'type: task', f'title: {title}', 'parent: F-0001', 'decided: true',
             f'links: {{plan: {plan}}}', 'writes: [x.py]']
    if after is not None:
        lines.append(f"after: [{', '.join(after)}]")
    lines += ['# ---- machine ----', f'state: {state}', 'stage_since: 2026-09-01T00:00:00Z',
              'updated: 2026-09-01T00:00:00Z']
    with open(os.path.join(root, 'tasks', f'{id_}.md'), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n## Description\n\n## History\n- 2026-09-01: created\n')


class BackfillTests(RecordCase):
    def setUp(self):
        super().setUp()
        write_task(self.root, 'T-0010', 'the reader', state='Closed')
        write_task(self.root, 'T-0011', 'the writer', state='Active')
        write_task(self.root, 'T-0012', 'the table', state='Active')
        write_task(self.root, 'T-0013', 'the view', after=[])  # a person already said: none

    def backfill(self):
        return plan_order.backfill(self.root, lambda path: WAVES_PLAN, out=self.lines.append)

    def test_open_cards_without_after_get_the_plans_order_once(self):
        self.assertEqual(self.backfill(), {'T-0012': ['T-0010', 'T-0011']})
        meta, body = read(self.root, 'task', 'T-0012')  # parses: written through the parser
        self.assertEqual(meta['after'], ['T-0010', 'T-0011'])
        self.assertEqual(meta['state'], 'Active')
        self.assertIn('## History', body)
        self.assertEqual(read(self.root, 'task', 'T-0013')[0]['after'], [])
        self.assertNotIn('after', read(self.root, 'task', 'T-0010')[0])
        self.assertEqual(self.lines, ['plan-order: T-0012: after: [T-0010, T-0011] (from docs/plans/f-0001.md)'])
        self.assertEqual(self.backfill(), {})  # idempotent

    def test_an_unreadable_plan_writes_nothing(self):
        self.assertEqual(plan_order.backfill(self.root, lambda path: None, out=self.lines.append), {})


class GuardTests(unittest.TestCase):
    """The wave refuses a Task whose plan predecessors have not landed, even with no `after:`."""
    ITEMS = {
        'F-0001': {'id': 'F-0001', 'type': 'feature', 'title': 'f', 'state': 'Active',
                   'stage': 'building', 'decided': True,
                   'children': ['T-0001', 'T-0002', 'T-0003', 'T-0004']},
        'T-0001': {'id': 'T-0001', 'type': 'task', 'title': 'the gatherer', 'parent': 'F-0001',
                   'state': 'Active', 'links': {'plan': 'docs/plans/f-0002.md'}, 'writes': ['a.py']},
        'T-0002': {'id': 'T-0002', 'type': 'task', 'title': 'the launch path', 'parent': 'F-0001',
                   'state': 'New', 'links': {'plan': 'docs/plans/f-0002.md'}, 'writes': ['b.py'],
                   'decided': True},
        'T-0003': {'id': 'T-0003', 'type': 'task', 'title': 'the stream', 'parent': 'F-0001',
                   'state': 'Closed', 'links': {'plan': 'docs/plans/f-0002.md'}, 'writes': ['c.py']},
        'T-0004': {'id': 'T-0004', 'type': 'task', 'title': 'the report', 'parent': 'F-0001',
                   'state': 'New', 'links': {'plan': 'docs/plans/f-0002.md'}, 'writes': ['d.py'],
                   'decided': True},
    }

    def test_overlay_adds_the_derived_order_and_never_touches_the_input(self):
        items = plan_order.overlay(self.ITEMS, lambda path: TABLE_PLAN)
        self.assertEqual(items['T-0002']['after'], ['T-0001'])
        self.assertEqual(items['T-0004']['after'], ['T-0003'])
        self.assertNotIn('after', self.ITEMS['T-0002'])
        self.assertIs(plan_order.overlay(self.ITEMS, lambda path: None)['T-0002'], self.ITEMS['T-0002'])

    def test_a_card_with_its_own_after_keeps_it(self):
        items = dict(self.ITEMS, **{'T-0002': dict(self.ITEMS['T-0002'], after=[])})
        self.assertEqual(plan_order.overlay(items, lambda path: TABLE_PLAN)['T-0002']['after'], [])

    def test_task_rows_wait_on_the_unlanded_predecessor(self):
        items = plan_order.overlay(self.ITEMS, lambda path: TABLE_PLAN)
        rows = feeder_rows.task_rows(items, None, items['F-0001'], set(), [])
        by_id = {r.item_id: r for r in rows}
        self.assertEqual(by_id['T-0002'].action, 'WAITS ON T-0001')
        self.assertFalse(by_id['T-0002'].launches)
        self.assertTrue(by_id['T-0004'].launches)  # T-0003 is Closed


if __name__ == '__main__':
    unittest.main()
