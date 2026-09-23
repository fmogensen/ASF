import os
import shutil
import unittest

from asf.groom import groom, shape
from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, load_items
from tests.test_groom import make_repo, run, write_item

TODAY = '2026-09-21'
YESTERDAY = '2026-09-20'
FEATURE_MACHINE = ['state: New', 'stage: plan-approved', 'stage_since: 2026-09-01T00:00:00Z',
                   'updated: 2026-09-01T00:00:00Z']


def read(root, folder, iid):
    with open(os.path.join(root, folder, f'{iid}.md'), encoding='utf-8') as f:
        return f.read()


def meta_of(root, iid):
    meta, _body = frontmatter.parse(read(root, 'tasks', iid), path=f'tasks/{iid}.md')
    return meta


def section(root, title, date=TODAY):
    """The lines under `## <title>` of a rendered groom file."""
    with open(os.path.join(root, 'groom', f'{date}.md'), encoding='utf-8') as f:
        text = f.read()
    block = text.split(f'## {title}\n', 1)[1].split('\n## ', 1)[0]
    return [ln for ln in block.split('\n') if ln.strip()]


class ShapeRepo(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['decided: true'])
        self.feature('F-0001')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def feature(self, fid, stage='plan-approved'):
        write_item(self.root, fid, 'feature', f'Feature {fid}', parent='E-0001',
                   typed_lines=['decided: true'],
                   machine_lines=[ln.replace('plan-approved', stage) for ln in FEATURE_MACHINE])

    def task(self, tid, writes, parent='F-0001', state='New', extra=(), stories=()):
        typed = [f"writes: [{', '.join(writes)}]", *extra]
        if stories:
            typed.append(f"stories: [{', '.join(stories)}]")
        write_item(self.root, tid, 'task', f'Task {tid}', parent=parent, typed_lines=typed,
                   machine_lines=[f'state: {state}', 'stage_since: 2026-09-01T00:00:00Z',
                                  'updated: 2026-09-01T00:00:00Z'])

    def groom(self, *flags):
        r = run(['groom', '--date', TODAY, *flags], self.root)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r

    def answer(self, *lines, title='Merges proposed'):
        with open(os.path.join(self.root, 'groom', f'{YESTERDAY}.md'), 'w', encoding='utf-8') as f:
            f.write(f"# Groom {YESTERDAY}\n\n## {title}\n\n" + '\n'.join(lines) + '\n')


class AreaTest(unittest.TestCase):
    def test_area_of(self):
        self.assertEqual(shape.area_of('asf/feeder/**', 2), 'asf/feeder')
        self.assertEqual(shape.area_of('asf/feeder/rows.py', 2), 'asf/feeder')
        self.assertEqual(shape.area_of('tests/test_groom.py', 2), 'tests')
        self.assertEqual(shape.area_of('README.md', 2), '.')
        self.assertEqual(shape.area_of('asf/*.py', 2), 'asf')
        self.assertEqual(shape.area_of('asf/feeder/rows.py', 1), 'asf')

    def test_areas_keep_first_seen_order(self):
        self.assertEqual(shape.areas(['b/x/y.py', 'a/z.py', 'b/x/**'], 2),
                         {'b/x': ['b/x/y.py', 'b/x/**'], 'a': ['a/z.py']})


class MergeProposalTest(ShapeRepo):
    def test_overlapping_tasks_of_one_feature_are_proposed(self):
        self.task('T-0001', ['asf/groom/**'])
        self.task('T-0002', ['asf/groom/groom.py'])
        self.groom()
        lines = section(self.root, 'Merges proposed')
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('- [ ] T-0001 merge T-0001+T-0002 —'), lines[0])
        self.assertTrue(lines[0].endswith('→ answer: ____'))

    def test_chain_is_one_proposal(self):
        self.task('T-0001', ['a/one/**', 'b/x.py'])
        self.task('T-0002', ['a/one/f.py', 'c/y.py'])
        self.task('T-0003', ['c/y.py'])
        self.groom()
        lines = section(self.root, 'Merges proposed')
        self.assertEqual(len(lines), 1)
        self.assertIn('merge T-0001+T-0002+T-0003 —', lines[0])

    def test_disjoint_or_cross_feature_is_not(self):
        self.feature('F-0002')
        self.task('T-0001', ['a/**'])
        self.task('T-0002', ['b/**'])
        self.task('T-0005', ['d/**'])
        self.task('T-0006', ['d/y.py'], parent='F-0002')
        self.groom()
        self.assertEqual(section(self.root, 'Merges proposed'), ['(none)'])

    def test_active_task_is_not_proposed(self):
        self.task('T-0001', ['asf/groom/**'])
        self.task('T-0002', ['asf/groom/groom.py'], state='Active')
        self.groom()
        self.assertEqual(section(self.root, 'Merges proposed'), ['(none)'])

    def test_feature_before_plan_approved_is_not_proposed(self):
        self.feature('F-0003', stage='plan-review r1')
        self.task('T-0001', ['x/**'], parent='F-0003')
        self.task('T-0002', ['x/f.py'], parent='F-0003')
        self.groom()
        self.assertEqual(section(self.root, 'Merges proposed'), ['(none)'])


class BatchProposalTest(ShapeRepo):
    """Capacity is the default lane size, 4, the same as a config.yaml `feeder: {capacity: 4}`."""

    def small(self, n):
        for i in range(1, n + 1):
            self.task(f'T-000{i}', [f'd{i}/f.py'])

    def test_over_capacity_small_tasks_batched(self):
        self.task('T-0001', ['d1/f.py'])
        self.task('T-0002', ['d2/f.py'])
        self.task('T-0003', ['d3/f.py'], extra=['rank: 1'])
        for i in (4, 5, 6):
            self.task(f'T-000{i}', [f'e{i}/a.py', f'e{i}/b.py', f'e{i}/c.py'])
        self.groom()
        lines = section(self.root, 'Batches proposed')
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('- [ ] T-0003 batch T-0003+T-0001+T-0002 —'), lines[0])
        self.assertIn('F-0001 has 6 ready Tasks for 4 slots', lines[0])

    def test_at_or_under_capacity_no_batch(self):
        self.small(4)
        self.groom()
        self.assertEqual(section(self.root, 'Batches proposed'), ['(none)'])

    def test_no_small_tasks_no_batch(self):
        for i in range(1, 7):
            self.task(f'T-000{i}', [f'e{i}/a.py', f'e{i}/b.py', f'e{i}/c.py'])
        self.groom()
        self.assertEqual(section(self.root, 'Batches proposed'), ['(none)'])

    def test_capacity_argument_is_honoured(self):
        self.small(6)
        canonical, _d = canonicalize(load_items(self.root)[0])
        derived = compute_derived(canonical)
        self.assertEqual(groom.build_groom_sections(canonical, derived, TODAY, capacity=6)['batch'], [])
        self.assertEqual(len(groom.build_groom_sections(canonical, derived, TODAY, capacity=4)['batch']), 1)

    def test_batch_max_globs_argument_is_honoured(self):
        for i in range(1, 7):
            self.task(f'T-000{i}', [f'e{i}/a.py', f'e{i}/b.py'])
        canonical, _d = canonicalize(load_items(self.root)[0])
        derived = compute_derived(canonical)
        self.assertEqual(groom.build_groom_sections(canonical, derived, TODAY,
                                                    batch_max_globs=1)['batch'], [])
        self.assertEqual(len(groom.build_groom_sections(canonical, derived, TODAY)['batch']), 1)


class SplitProposalTest(ShapeRepo):
    def setUp(self):
        super().setUp()
        self.task('T-0050', ['asf/feeder/**', 'asf/harvest/**'])

    def test_one_area_held_one_free(self):
        self.task('T-0020', ['asf/feeder/rows.py'], state='Active')
        self.groom()
        lines = section(self.root, 'Splits proposed')
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('- [ ] T-0050 split asf/feeder | asf/harvest —'), lines[0])
        self.assertIn('held by T-0020', lines[0])

    def test_all_areas_held_no_split(self):
        self.task('T-0020', ['asf/feeder/rows.py', 'asf/harvest/x.py'], state='Active')
        self.groom()
        self.assertEqual(section(self.root, 'Splits proposed'), ['(none)'])

    def test_nothing_held_no_split(self):
        self.groom()
        self.assertEqual(section(self.root, 'Splits proposed'), ['(none)'])

    def test_a_ready_task_of_another_feature_holds(self):
        self.feature('F-0002')
        self.task('T-0021', ['asf/harvest/x.py'], parent='F-0002')
        self.groom()
        lines = section(self.root, 'Splits proposed')
        self.assertEqual(len(lines), 1)
        self.assertIn('asf/harvest is held by T-0021', lines[0])


class ApplyTest(ShapeRepo):
    MERGE = '- [ ] T-0001 merge T-0001+T-0002 — F-0001: writes overlap → answer: '
    SPLIT = '- [ ] T-0050 split asf/feeder | asf/harvest — held → answer: '

    def merge_fixture(self):
        self.task('T-0001', ['asf/groom/**'], stories=['S-0001'])
        self.task('T-0002', ['asf/groom/groom.py', 'tests/x.py'], stories=['S-0001', 'S-0002'])
        run(['index'], self.root)

    def test_merge_yes_applies(self):
        self.merge_fixture()
        self.answer(self.MERGE + 'yes')
        r = self.groom('--apply')
        self.assertIn('applied 2', r.stdout)
        a, b = meta_of(self.root, 'T-0001'), meta_of(self.root, 'T-0002')
        self.assertEqual(a['writes'], ['asf/groom/**', 'asf/groom/groom.py', 'tests/x.py'])
        self.assertEqual(a['stories'], ['S-0001', 'S-0002'])
        self.assertEqual(a['merged'], ['T-0002'])
        self.assertEqual(b['removed'], f'merged into T-0001 (groom {TODAY})')
        self.assertIn(f'{TODAY} groom: merged T-0002 into T-0001 (operator)',
                      read(self.root, 'tasks', 'T-0001'))
        self.assertIn(f'{TODAY} groom: removed → merged into T-0001 (operator)',
                      read(self.root, 'tasks', 'T-0002'))

    def test_merge_apply_is_idempotent(self):
        self.merge_fixture()
        self.answer(self.MERGE + 'yes')
        self.groom('--apply')
        first = [read(self.root, 'tasks', t) for t in ('T-0001', 'T-0002')]
        r = self.groom('--apply')
        self.assertIn('applied 0', r.stdout)
        self.assertNotIn('skipped', r.stdout)
        self.assertEqual([read(self.root, 'tasks', t) for t in ('T-0001', 'T-0002')], first)

    def test_merge_skipped_when_a_task_went_active(self):
        self.merge_fixture()
        self.task('T-0002', ['asf/groom/groom.py'], state='Active')
        run(['index'], self.root)
        before = [read(self.root, 'tasks', t) for t in ('T-0001', 'T-0002')]
        self.answer(self.MERGE + 'yes')
        r = self.groom('--apply')
        self.assertIn('merge T-0001+T-0002 skipped — T-0002 is Active', r.stdout)
        self.assertEqual([read(self.root, 'tasks', t) for t in ('T-0001', 'T-0002')], before)

    def test_no_declines_and_suppresses(self):
        self.merge_fixture()
        self.answer(self.MERGE + 'no')
        self.groom('--apply')
        for tid in ('T-0001', 'T-0002'):
            meta = meta_of(self.root, tid)
            self.assertFalse(meta.get('removed'))
            self.assertEqual(meta['reshape_declined'], ['merge T-0001+T-0002'])
            self.assertIn(f'{TODAY} groom: merge declined (operator)', read(self.root, 'tasks', tid))
        self.assertEqual(section(self.root, 'Merges proposed'), ['(none)'])

    def test_blank_answer_changes_nothing(self):
        self.merge_fixture()
        before = [read(self.root, 'tasks', t) for t in ('T-0001', 'T-0002')]
        self.answer(self.MERGE + '____')
        self.groom('--apply')
        self.assertEqual([read(self.root, 'tasks', t) for t in ('T-0001', 'T-0002')], before)

    def test_split_yes_marks_reshape(self):
        self.task('T-0050', ['asf/feeder/**', 'asf/harvest/**'])
        self.task('T-0020', ['asf/feeder/rows.py'], state='Active')
        run(['index'], self.root)
        self.answer(self.SPLIT + 'yes', title='Splits proposed')
        self.groom('--apply')
        meta = meta_of(self.root, 'T-0050')
        self.assertEqual(meta['reshape'], f'split asf/feeder | asf/harvest (groom {TODAY})')
        self.assertNotIn('decided', meta)
        self.assertIn(f'{TODAY} groom: reshape → split asf/feeder | asf/harvest (operator)',
                      read(self.root, 'tasks', 'T-0050'))
        self.assertEqual(section(self.root, 'Splits proposed'), ['(none)'])

    def test_split_parts_listed_to_confirm(self):
        self.task('T-0060', ['asf/harvest/**'], extra=['split_from: T-0050'])
        run(['index'], self.root)
        self.groom()
        lines = section(self.root, 'Split parts to confirm')
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('- [ ] T-0060 Task T-0060 — split from T-0050'))
        self.answer(lines[0].replace('____', 'yes'), title='Split parts to confirm')
        self.groom('--apply')
        self.assertIs(meta_of(self.root, 'T-0060')['decided'], True)
        self.assertEqual(section(self.root, 'Split parts to confirm'), ['(none)'])

    def test_no_on_a_proposal_never_removes(self):
        self.merge_fixture()
        self.answer(self.MERGE + 'close')
        self.groom('--apply')
        self.assertFalse(meta_of(self.root, 'T-0001').get('removed'))
        self.assertFalse(meta_of(self.root, 'T-0002').get('removed'))


if __name__ == '__main__':
    unittest.main()
