"""The order a plan states reaches the Task cards as ``after:`` — at mint, by backfill for cards
minted without it, and as the wave's guard when a card still lacks it. Before this every Task of a
plan launched at once, and each successor's coder ended `empty branch: nothing to land`."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.evidence import evidence
from asf.feeder import rows as feeder_rows
from asf.record import plan_order, plan_tasks
from tests.test_plan_tasks import FOLDERS, read, write_item

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

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

    def test_card_ids_in_an_after_line_pass_through_not_as_task_numbers(self):
        self.assertEqual(plan_order.task_order('### Task 1: a\n\n### Task 2: b\nafter: [T-0001]\n'),
                         {1: [], 2: ['T-0001']})

    def test_a_feature_id_on_the_after_line_is_kept(self):
        self.assertEqual(plan_order.task_order('### Task 1: a\nafter: F-0091\n'), {1: ['F-0091']})

    def test_a_task_number_and_a_card_id_on_one_line_are_both_kept(self):
        self.assertEqual(plan_order.task_order('### Task 1: a\n\n### Task 2: b\nafter: Task 1, F-0091\n'),
                         {1: [], 2: [1, 'F-0091']})

    def test_a_card_id_in_dependency_prose_is_kept_beside_after_none(self):
        text = ('### Task 1: a\n\n### Task 2: b\nafter: none\n\nThe part that waits: '
                '**F-0091 must land first**. It also depends on B-0012.\n')
        self.assertEqual(plan_order.task_order(text), {1: [], 2: ['B-0012', 'F-0091']})

    def test_a_card_id_quoted_in_code_is_not_a_dependency(self):
        text = '### Task 1: a\nafter: none\n\nthe row reads `WAITS ON T-0029`, `depends on B-0031`.\n'
        self.assertEqual(plan_order.task_order(text), {1: []})

    def test_a_plan_task_id_naming_its_own_numbering_is_that_task(self):
        text = '### Task 14650: a\n\n### Task 14651: b\nafter: [T-14650]\n'
        self.assertEqual(plan_order.task_order(text), {14650: [], 14651: [14650]})


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

    def test_an_open_card_gains_the_feature_its_plan_waits_on(self):
        # minted when the parser dropped card ids: `after: none` on the card, F-0091 in the plan
        write_item(self.root, 'F-0091', 'feature', 'The prerequisite', parent='E-0001')
        text = WAVES_PLAN.replace('writes: c.py\n', 'writes: c.py\nafter: Task 1, F-0091\n')
        written = plan_order.backfill(self.root, lambda path: text, out=self.lines.append)
        self.assertEqual(written, {'T-0012': ['T-0010', 'F-0091']})
        text = WAVES_PLAN.replace('writes: d.py\nafter: Task 1', 'writes: d.py\nafter: none\n\n'
                                  '**F-0091 must land first**.')
        written = plan_order.backfill(self.root, lambda path: text, out=self.lines.append)
        self.assertEqual(written, {'T-0013': ['F-0091']})  # it had `after: []`: only the card id
        self.assertEqual(read(self.root, 'task', 'T-0013')[0]['after'], ['F-0091'])
        self.assertEqual(plan_order.backfill(self.root, lambda path: text, out=self.lines.append), {})

    def test_a_card_id_the_record_does_not_hold_is_not_written(self):
        text = WAVES_PLAN.replace('writes: d.py\nafter: Task 1', 'writes: d.py\nafter: F-0999')
        self.assertEqual(plan_order.backfill(self.root, lambda path: text, out=self.lines.append),
                         {'T-0012': ['T-0010', 'T-0011']})

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


class BatchedPlanReads(unittest.TestCase):
    """D13: ``TrunkReader`` is a drop-in for ``trunk_reader``'s old closure, with a batched
    warm-up — ``backfill`` and ``overlay`` call ``prefetch`` only when the reader has one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='batched_plan_reads_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        origin = os.path.join(self.tmp, 'origin.git')
        work = os.path.join(self.tmp, 'work')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', origin], check=True,
                       capture_output=True, env=GIT_ENV)
        subprocess.run(['git', 'clone', '-q', origin, work], check=True, capture_output=True,
                       env=GIT_ENV)
        self.plans = {}
        for n in range(5):
            path = f'docs/plans/plan{n}.md'
            self.plans[path] = f'# Plan {n}\n'
        self.plans['docs/plans/f-0001.md'] = WAVES_PLAN
        self.plans['docs/plans/f-0002.md'] = TABLE_PLAN
        for path, text in self.plans.items():
            full = os.path.join(work, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w', encoding='utf-8') as f:
                f.write(text)
        subprocess.run(['git', '-C', work, 'add', '.'], check=True, capture_output=True,
                       env=GIT_ENV)
        subprocess.run(['git', '-C', work, 'commit', '-q', '-m', 'plans'], check=True,
                       capture_output=True, env=GIT_ENV)
        subprocess.run(['git', '-C', work, 'push', '-q', 'origin', 'main'], check=True,
                       capture_output=True, env=GIT_ENV)
        repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'clone', '-q', origin, repo], check=True, capture_output=True,
                       env=GIT_ENV)
        self.product = env.Product('sample', {'repo_dir': repo, 'main': 'main'})
        self.five = [f'docs/plans/plan{n}.md' for n in range(5)]

    def backfill_fixture(self):
        root = tempfile.mkdtemp(prefix='batched_plan_reads_backfill_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(root, f))
        write_item(root, 'E-0001', 'epic', 'Factory')
        write_item(root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(root, 'S-0001', 'story', 'Read it', parent='F-0001')
        write_task(root, 'T-0010', 'the reader', state='Closed')
        write_task(root, 'T-0011', 'the writer', state='Active')
        write_task(root, 'T-0012', 'the table', state='Active')
        return root

    def test_trunk_reader_is_callable_with_one_path_as_the_closure_is_today(self):
        reader = plan_order.trunk_reader(self.product)
        self.assertEqual(reader('docs/plans/plan0.md'), '# Plan 0\n')
        self.assertIsNone(reader('docs/plans/no-such-plan.md'))

    def test_prefetch_over_five_plans_makes_two_processes_and_the_five_reads_make_none(self):
        reader = plan_order.trunk_reader(self.product)
        real = subprocess.run
        with mock.patch.object(evidence.subprocess, 'run', side_effect=real) as run:
            reader.prefetch(self.five)
            self.assertEqual(run.call_count, 2)
            for n, path in enumerate(self.five):
                self.assertEqual(reader(path), f'# Plan {n}\n')
            self.assertEqual(run.call_count, 2)  # the five reads above cost nothing more

    def test_a_path_outside_the_prefetch_is_still_read(self):
        reader = plan_order.trunk_reader(self.product)
        reader.prefetch(self.five[:2])
        self.assertEqual(reader(self.five[3]), '# Plan 3\n')

    def test_backfill_handed_a_bare_lambda_behaves_as_before(self):
        root = self.backfill_fixture()
        written = plan_order.backfill(root, lambda path: WAVES_PLAN, out=lambda *_: None)
        self.assertEqual(written, {'T-0012': ['T-0010', 'T-0011']})

    def test_backfill_handed_a_trunk_reader_writes_the_same_after_values(self):
        root = self.backfill_fixture()
        reader = plan_order.trunk_reader(self.product)
        written = plan_order.backfill(root, reader, out=lambda *_: None)
        self.assertEqual(written, {'T-0012': ['T-0010', 'T-0011']})

    def test_overlay_handed_a_bare_lambda_behaves_as_before(self):
        items = plan_order.overlay(GuardTests.ITEMS, lambda path: TABLE_PLAN)
        self.assertEqual(items['T-0002']['after'], ['T-0001'])
        self.assertEqual(items['T-0004']['after'], ['T-0003'])

    def test_overlay_handed_a_trunk_reader_writes_the_same_after_values(self):
        reader = plan_order.trunk_reader(self.product)
        items = plan_order.overlay(GuardTests.ITEMS, reader)
        self.assertEqual(items['T-0002']['after'], ['T-0001'])
        self.assertEqual(items['T-0004']['after'], ['T-0003'])


if __name__ == '__main__':
    unittest.main()

    def test_a_feature_in_after_holds_the_row_until_it_is_done(self):
        items = dict(self.ITEMS, **{'F-0091': {'id': 'F-0091', 'type': 'feature', 'title': 'p',
                                                'state': 'Active', 'stage': 'plan-approved'}})
        plan = TABLE_PLAN.replace('writes: b.py\n', 'writes: b.py\nafter: F-0091\n')
        items = dict(items, **{'T-0001': dict(items['T-0001'], state='Closed')})
        held = plan_order.overlay(items, lambda path: plan)
        self.assertEqual(held['T-0002']['after'], ['F-0091'])
        rows = feeder_rows.hold_unlanded(feeder_rows.task_rows(held, None, held['F-0001'], set(), []), held)
        row = {r.item_id: r for r in rows}['T-0002']
        self.assertEqual((row.action, row.reason), ('WAITS ON F-0091', 'after: F-0091 has not landed'))
        self.assertFalse(row.launches)
        done = dict(held, **{'F-0091': dict(held['F-0091'], state='Closed', stage='landed')})
        rows = feeder_rows.hold_unlanded(feeder_rows.task_rows(done, None, done['F-0001'], set(), []), done)
        self.assertTrue({r.item_id: r for r in rows}['T-0002'].launches)

    def test_overlay_adds_a_plan_card_id_to_a_card_that_has_after(self):
        items = dict(self.ITEMS, **{'F-0091': {'id': 'F-0091', 'type': 'feature', 'state': 'Active'},
                                    'T-0002': dict(self.ITEMS['T-0002'], after=[])})
        plan = TABLE_PLAN.replace('writes: b.py\n', 'writes: b.py\nafter: Task 1, F-0091\n')
        self.assertEqual(plan_order.overlay(items, lambda path: plan)['T-0002']['after'], ['F-0091'])
