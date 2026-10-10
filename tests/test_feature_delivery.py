"""The Feature is the delivery unit (``conventions.delivery: feature``, :mod:`asf.record.slice`).

A planned Feature's Tasks become one delivery — one branch, one PR, one review, one commit per
Task — led by the first Task; a plan past ``slice_max_tasks`` Tasks or ``size.medium_max_files``
footprint entries is cut along its ``after:`` boundaries into ordered slices. The feeder emits
one DELIVERY → CODE row for the lead instead of N PLAN → CODE rows, an ``after:`` inside the
delivery is commit order, a branch the lane held ``incomplete`` (a crash, a run cap, ``status:
partial``) opens no PR and comes back to the same lead, a half-built Feature's free Tasks
migrate into a delivery behind its started ones, the ingest still closes each Task by its own
commit, and a product that did not opt in is unchanged.
"""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import budget, conventions
import importlib
from asf.env import Product

build_mod = importlib.import_module('asf.briefs.build')
from asf.feeder import rows
from asf.harvest import lane
from asf.record import frontmatter, ingest, slice as slice_mod
from asf.scorecard import score
from asf.tick import step_prs
from asf.workers import continuation, lifecycle
from tests.test_ingest import EMPTY_EV, landed_ids, make_repo, write as write_card

FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks'}


def product(**conv):
    # the fixtures carry no Story: the stories-first gate (tests/test_stories_first.py) is off
    return Product('sample', {'conventions': dict({'delivery': 'feature',
                                                   'feeder': {'stories_before_plan': False}},
                                                  **conv)})


def read_meta(root, type_, iid):
    with open(os.path.join(root, FOLDER_OF[type_], f'{iid}.md'), encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{iid}.md')[0]


# ---- the cut (pure) ---------------------------------------------------------------------

class CutTests(unittest.TestCase):
    """:func:`slice.cut`: whole by default; past the limits, ordered slices along ``after:``."""

    def ids(self, n):
        return [f'T-{k:04d}' for k in range(1, n + 1)]

    def chain(self, ids):
        return {t: [ids[k - 1]] for k, t in enumerate(ids) if k}

    def writes(self, ids, n=1):
        return {t: [f'{t.lower()}-{k}.py' for k in range(n)] for t in ids}

    def test_a_plan_within_the_limits_is_one_whole_delivery(self):
        ids = self.ids(5)
        self.assertEqual(slice_mod.cut(ids, self.chain(ids), self.writes(ids, 2), 6, 15), [ids])

    def test_more_than_six_tasks_cut_at_an_after_boundary(self):
        ids = self.ids(8)
        out = slice_mod.cut(ids, self.chain(ids), self.writes(ids), 6, 15)
        self.assertEqual(out, [ids[:6], ids[6:]])

    def test_a_cut_backs_up_to_the_last_wave_boundary(self):
        # T-0001, T-0002 independent; T-0003..T-0007 each after T-0001: seven Tasks in two waves
        ids = self.ids(7)
        after = {t: ['T-0001'] for t in ids[2:]}
        out = slice_mod.cut(ids, after, self.writes(ids), 6, 15)
        self.assertEqual(out, [ids[:2], ids[2:]])

    def test_seven_independent_tasks_leave_a_slice_of_one(self):
        ids = self.ids(7)
        self.assertEqual(slice_mod.cut(ids, {}, self.writes(ids), 6, 15), [ids[:6], ids[6:]])

    def test_a_union_footprint_over_the_file_limit_cuts(self):
        ids = self.ids(4)
        out = slice_mod.cut(ids, {}, self.writes(ids, 5), 6, 15)   # 4 × 5 = 20 > 15
        self.assertEqual(out, [ids[:3], ids[3:]])

    def test_a_small_feature_is_always_whole(self):
        ids = self.ids(8)
        self.assertEqual(slice_mod.cut(ids, self.chain(ids), self.writes(ids, 5), 6, 15, whole=True),
                         [ids])

    def test_levels_ignore_outside_ids_and_break_cycles(self):
        lv = slice_mod.levels(['a', 'b', 'c'], {'a': ['x'], 'b': ['a'], 'c': ['b', 'c']})
        self.assertEqual(lv, {'a': 0, 'b': 1, 'c': 2})
        self.assertEqual(slice_mod.levels(['a', 'b'], {'a': ['b'], 'b': ['a']})['a'], 1)


# ---- the record pass -----------------------------------------------------------------

class RecordFixture(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lines = []

    def feature(self, fid='F-0001', stage='plan-approved', size=None, state='Active'):
        typed = ['decided: true'] + ([f'size: {size}'] if size else [])
        write_card(self.root, fid, 'feature', 'The reader', 'features', typed_lines=typed,
                   machine_lines=[f'state: {state}', f'stage: {stage}',
                                  'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'])

    def task(self, tid, writes, after=(), state='New', stories=(), fid='F-0001', decided=True,
             evidence=()):
        typed = [f"writes: [{', '.join(writes)}]"]
        if after:
            typed.append(f"after: [{', '.join(after)}]")
        if stories:
            typed.append(f"stories: [{', '.join(stories)}]")
        if decided:
            typed.append('decided: true')
        machine = [f'state: {state}', 'stage_since: 2026-01-01T00:00:00Z']
        if evidence:
            machine += ['evidence:'] + [f'  - "{e}"' for e in evidence]
        write_card(self.root, tid, 'task', f'Task {tid}', 'tasks', parent=fid, typed_lines=typed,
                   machine_lines=machine + ['updated: 2026-01-01T00:00:00Z'])

    def run_pass(self, busy=(), **conv):
        return slice_mod.deliveries(self.root, product(**conv), busy, out=self.lines.append)


class WholeFeatureTests(RecordFixture):
    """A freshly planned Feature: every Task is free, the plan fits, one delivery led by Task 1."""

    def setUp(self):
        super().setUp()
        self.feature()
        self.task('T-0001', ['a.py'], stories=['S-0001'])
        self.task('T-0002', ['b.py'], after=['T-0001'], stories=['S-0001'])
        self.task('T-0003', ['c.py'], after=['T-0002'], stories=['S-0002'])

    def test_the_first_task_leads_and_the_rest_point_back(self):
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002', 'T-0003']})
        lead = read_meta(self.root, 'task', 'T-0001')
        self.assertEqual(lead['delivers'], ['T-0001', 'T-0002', 'T-0003'])
        self.assertNotIn('delivered_by', lead)
        self.assertNotIn('after', lead)   # nothing started: no hold added
        for tid in ('T-0002', 'T-0003'):
            self.assertEqual(read_meta(self.root, 'task', tid)['delivered_by'], 'T-0001')
        self.assertEqual(read_meta(self.root, 'task', 'T-0002')['after'], ['T-0001'])  # kept
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0001 delivers T-0001, T-0002, T-0003 '
                                      '(proves S-0001, S-0002)'])

    def test_the_pass_is_idempotent(self):
        self.run_pass()
        before = {t: read_meta(self.root, 'task', t) for t in ('T-0001', 'T-0002', 'T-0003')}
        self.assertEqual(self.run_pass(), {})
        self.assertEqual({t: read_meta(self.root, 'task', t) for t in before}, before)

    def test_a_feature_not_yet_planned_is_left_alone(self):
        self.feature(stage='plan-review r1')
        self.assertEqual(self.run_pass(), {})
        self.assertNotIn('delivers', read_meta(self.root, 'task', 'T-0001'))

    def test_the_preview_reads_the_same_rule(self):
        from asf.record.core import canonicalize, load_items
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        metas = {i: r['meta'] for i, r in canonical.items()}
        self.assertEqual(slice_mod.plan_slices(metas, 'F-0001', product().conventions),
                         [('T-0001', ['T-0001', 'T-0002', 'T-0003'])])


class SliceCutRecordTests(RecordFixture):
    """A plan past the limits lands as ordered slices, each led by its first Task; a slice of
    one Task stays on the Task lane."""

    def test_eight_chained_tasks_become_two_slices(self):
        self.feature()
        prev = None
        for k in range(1, 9):
            tid = f'T-{k:04d}'
            self.task(tid, [f'{k}.py'], after=[prev] if prev else ())
            prev = tid
        out = self.run_pass()
        self.assertEqual(out, {'T-0001': [f'T-{k:04d}' for k in range(1, 7)],
                               'T-0007': ['T-0007', 'T-0008']})
        self.assertEqual(read_meta(self.root, 'task', 'T-0008')['delivered_by'], 'T-0007')
        self.assertEqual(read_meta(self.root, 'task', 'T-0007')['after'], ['T-0006'])
        self.assertEqual(self.lines[0][:22], 'slice: F-0001 1/2: T-0')

    def test_a_footprint_over_the_file_limit_cuts_and_a_lone_task_rides_its_own_lane(self):
        self.feature()
        for k in range(1, 5):
            self.task(f'T-{k:04d}', [f'{k}-{j}.py' for j in range(5)])
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002', 'T-0003']})
        self.assertNotIn('delivers', read_meta(self.root, 'task', 'T-0004'))
        self.assertNotIn('delivered_by', read_meta(self.root, 'task', 'T-0004'))

    def test_a_small_feature_is_one_delivery_whatever_its_size(self):
        self.feature(size='s')
        for k in range(1, 9):
            self.task(f'T-{k:04d}', [f'{k}-{j}.py' for j in range(5)])
        self.assertEqual(list(self.run_pass()), ['T-0001'])
        self.assertEqual(len(read_meta(self.root, 'task', 'T-0001')['delivers']), 8)


class MigrationTests(RecordFixture):
    """A half-built Feature: its free Tasks become one delivery behind its started ones;
    mid-lane Tasks land as they are."""

    def setUp(self):
        super().setUp()
        self.feature(stage='building 1/4')
        self.task('T-0001', ['a.py'], state='Closed')
        self.task('T-0002', ['b.py'], state='Active', evidence=['branch worker/T-0002, PR #7 OPEN'])
        self.task('T-0003', ['c.py'])
        self.task('T-0004', ['b.py'])

    def test_free_tasks_become_a_delivery_after_the_started_ones(self):
        # T-0006: started too, but its footprint shares no path with either member — disjoint,
        # beside T-0002's overlap with T-0004, in the class that owns the migration (D7)
        self.task('T-0006', ['far/away.py'], state='Active',
                  evidence=['branch worker/T-0006, PR #9 OPEN'])
        self.assertEqual(self.run_pass(), {'T-0003': ['T-0003', 'T-0004']})
        lead = read_meta(self.root, 'task', 'T-0003')
        self.assertEqual(lead['delivers'], ['T-0003', 'T-0004'])
        self.assertEqual(lead['after'], ['T-0002'])   # Active: a hold; Closed: not; T-0006: disjoint
        self.assertEqual(read_meta(self.root, 'task', 'T-0004')['delivered_by'], 'T-0003')
        for tid in ('T-0001', 'T-0002'):
            m = read_meta(self.root, 'task', tid)
            self.assertNotIn('delivers', m)
            self.assertNotIn('delivered_by', m)
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0003 delivers T-0003, T-0004 '
                                      'after T-0002 (T-0004 shares b.py) '
                                      '— 1 started, no shared path: T-0006'])

    def test_a_task_a_session_holds_is_started_not_free(self):
        self.assertEqual(self.run_pass(busy={'T-0003'}), {})   # one free Task left: no delivery
        self.assertNotIn('delivers', read_meta(self.root, 'task', 'T-0004'))

    def test_an_active_task_on_an_idle_branch_folds_into_the_delivery(self):
        # T-0005: Active because its branch exists, no PR, its cloud run dead on quota — idle;
        # T-0002 (Active, a PR open) is started, and the delivery waits on it
        write_card(self.root, 'T-0005', 'task', 'Idle', 'tasks', parent='F-0001',
                   typed_lines=['writes: [e.py]', 'decided: true', 'links: {branches: [worker/T-0005]}'],
                   machine_lines=['state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                                  'evidence:', '  - branch worker/T-0005 exists',
                                  'updated: 2026-01-01T00:00:00Z'])
        write_card(self.root, 'T-0002', 'task', 'Started', 'tasks', parent='F-0001',
                   typed_lines=['writes: [b.py]', 'decided: true'],
                   machine_lines=['state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                                  'evidence:', '  - "branch worker/T-0002, PR #7 OPEN"',
                                  'updated: 2026-01-01T00:00:00Z'])
        self.assertEqual(self.run_pass(), {'T-0003': ['T-0003', 'T-0004', 'T-0005']})
        self.assertEqual(read_meta(self.root, 'task', 'T-0005')['delivered_by'], 'T-0003')
        self.assertEqual(read_meta(self.root, 'task', 'T-0003')['after'], ['T-0002'])
        # a session on the idle branch makes it started again, not free
        self.assertEqual(self.run_pass(busy={'T-0005'}), {})

    def test_a_task_started_since_waits_the_delivery(self):
        self.task('T-0005', ['c.py'])
        out = self.run_pass(busy={'T-0003'})
        self.assertEqual(out, {'T-0004': ['T-0004', 'T-0005']})
        self.assertEqual(sorted(read_meta(self.root, 'task', 'T-0004')['after']), ['T-0002', 'T-0003'])


class SharedPathBindingTests(RecordFixture):
    """S-82205: a delivery lead waits on a started Task only when they share a path — the
    ``binds``/``loose`` split in :func:`slice.deliveries` that replaces the old unconditional
    ``extra``."""

    def base(self):
        self.feature(stage='building 1/3')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['b.py'], after=['T-0001'])

    def started(self, tid, writes):
        self.task(tid, writes, state='Active', evidence=[f'branch worker/{tid}, PR #9 OPEN'])

    def test_a_disjoint_started_task_leaves_the_leads_after_as_it_was(self):
        self.base()
        self.started('T-0003', ['z.py'])
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002']})
        self.assertNotIn('after', read_meta(self.root, 'task', 'T-0001'))

    def test_a_lead_with_its_own_after_keeps_it_and_nothing_more(self):
        self.feature(stage='building 1/3')
        self.task('T-0009', ['y.py'], state='Active', evidence=['branch worker/T-0009, PR #1 OPEN'])
        self.task('T-0001', ['a.py'], after=['T-0009'])
        self.task('T-0002', ['b.py'], after=['T-0001'])
        self.started('T-0003', ['z.py'])
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002']})
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['after'], ['T-0009'])

    def test_a_glob_matching_a_members_path_binds_the_lead(self):
        self.base()
        self.started('T-0003', ['b.py'])
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002']})
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['after'], ['T-0003'])

    def test_a_path_shared_with_a_non_lead_member_binds_the_lead_not_that_member(self):
        self.base()
        self.started('T-0003', ['b.py'])
        self.run_pass()
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['after'], ['T-0003'])
        self.assertEqual(read_meta(self.root, 'task', 'T-0002')['after'], ['T-0001'])

    def test_a_started_task_a_member_already_waits_on_is_not_added_twice(self):
        self.feature(stage='building 1/3')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['b.py'], after=['T-0001', 'T-0003'])
        self.started('T-0003', ['b.py'])
        self.run_pass()
        self.assertNotIn('after', read_meta(self.root, 'task', 'T-0001'))

    def test_a_shared_path_match_does_not_bind(self):
        self.feature(stage='building 1/3')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['uv.lock'], after=['T-0001'])
        self.started('T-0003', ['uv.lock'])
        self.assertEqual(self.run_pass(shared_paths=['uv.lock']), {'T-0001': ['T-0001', 'T-0002']})
        self.assertNotIn('after', read_meta(self.root, 'task', 'T-0001'))

    def test_a_task_with_no_footprint_binds_nothing_and_is_bound_by_nothing(self):
        self.feature(stage='building 1/3')
        self.task('T-0001', [])
        self.task('T-0002', ['b.py'], after=['T-0001'])
        self.started('T-0003', [])
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002']})
        self.assertNotIn('after', read_meta(self.root, 'task', 'T-0001'))


class EdgeLineTests(RecordFixture):
    """S-82206: the pass names each ``after:`` edge it adds, the member and the path that
    caused it, and the started Tasks it declined to wait on."""

    def base(self):
        self.feature(stage='building 1/3')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['b.py'], after=['T-0001'])

    def started(self, tid, writes):
        self.task(tid, writes, state='Active', evidence=[f'branch worker/{tid}, PR #9 OPEN'])

    def test_one_line_per_delivery_written(self):
        self.feature(fid='F-0001')
        self.task('T-0001', ['a.py'], fid='F-0001')
        self.task('T-0002', ['b.py'], after=['T-0001'], fid='F-0001')
        self.feature(fid='F-0002')
        self.task('T-0003', ['c.py'], fid='F-0002')
        self.task('T-0004', ['d.py'], after=['T-0003'], fid='F-0002')
        self.run_pass()
        self.assertEqual(len(self.lines), 2)

    def test_each_edge_names_the_task_the_member_and_the_glob(self):
        self.base()
        self.started('T-0003', ['b.py'])
        self.run_pass()
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0001 delivers T-0001, T-0002 '
                                      'after T-0003 (T-0002 shares b.py)'])

    def test_two_differing_globs_are_both_printed_member_first(self):
        self.feature(stage='building 1/3')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['apps/web/**'], after=['T-0001'])
        self.started('T-0003', ['apps/web/x.ts'])
        self.run_pass()
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0001 delivers T-0001, T-0002 '
                                      'after T-0003 (T-0002 shares apps/web/** ~ apps/web/x.ts)'])

    def test_two_equal_globs_are_printed_once(self):
        self.base()
        self.started('T-0003', ['b.py'])
        self.run_pass()
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0001 delivers T-0001, T-0002 '
                                      'after T-0003 (T-0002 shares b.py)'])

    def test_started_tasks_that_bound_nothing_are_named_with_their_count(self):
        self.base()
        self.started('T-0003', ['z.py'])
        self.started('T-0004', ['y.py'])
        self.run_pass()
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0001 delivers T-0001, T-0002 '
                                      '— 2 started, no shared path: T-0003, T-0004'])

    def test_both_clauses_absent_when_there_is_nothing_to_say(self):
        self.base()
        self.run_pass()
        self.assertEqual(self.lines, ['slice: F-0001 1/1: T-0001 delivers T-0001, T-0002'])


class RemovedEdgeTests(RecordFixture):
    """S-82207: a lead already in a delivery is never re-cut (:func:`slice._in_delivery`), so
    an ``after:`` edge the pass wrote and an operator then removed by hand is not put back — the
    mechanism S-82205/S-82206 build on, pinned directly."""

    def setUp(self):
        super().setUp()
        self.feature(stage='building 1/3')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['b.py'], after=['T-0001'])
        self.task('T-0003', ['b.py'], state='Active', evidence=['branch worker/T-0003, PR #9 OPEN'])

    def clear_lead_after(self):
        from asf.record.core import canonicalize, load_items
        from asf.record.setfield import set_typed
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        err = set_typed(canonical['T-0001'], {'after': None})
        self.assertIsNone(err)

    def test_a_removed_after_edge_is_not_re_added(self):
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002']})
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['after'], ['T-0003'])
        self.clear_lead_after()
        self.assertNotIn('after', read_meta(self.root, 'task', 'T-0001'))
        self.lines.clear()
        self.assertEqual(self.run_pass(), {})
        self.assertNotIn('after', read_meta(self.root, 'task', 'T-0001'))
        self.assertEqual(self.lines, [])

    def test_the_delivers_and_delivered_by_lists_are_untouched_by_the_second_pass(self):
        self.run_pass()
        self.clear_lead_after()
        self.run_pass()
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['delivers'], ['T-0001', 'T-0002'])
        self.assertEqual(read_meta(self.root, 'task', 'T-0002')['delivered_by'], 'T-0001')

    def test_a_delivering_lead_is_not_among_the_free_tasks(self):
        self.run_pass()
        from asf.record.core import canonicalize, load_items
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        metas = {i: r['meta'] for i, r in canonical.items()}
        self.assertNotIn('T-0001', slice_mod.free_tasks(metas, 'F-0001'))

    def test_a_delivered_by_member_is_not_among_the_free_tasks(self):
        self.run_pass()
        from asf.record.core import canonicalize, load_items
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        metas = {i: r['meta'] for i, r in canonical.items()}
        self.assertNotIn('T-0002', slice_mod.free_tasks(metas, 'F-0001'))


class UndeliveredTests(RecordFixture):
    """S-82208: a Task an ``asf undeliver`` ruled out of a delivery is never bundled back into
    one — the ``skip`` thread (:func:`slice.undelivered_tasks`) that reads the member-shape
    ``## History`` line directly, because ``undeliver`` deletes ``delivered_by:``."""

    def setUp(self):
        super().setUp()
        self.feature(stage='building 1/5')
        self.task('T-0001', ['a.py'])
        self.task('T-0002', ['b.py'], after=['T-0001'])
        self.task('T-0003', ['c.py'], after=['T-0002'])
        self.task('T-0004', ['d.py'], after=['T-0003'])

    def undeliver(self, lead, member, why='ruled out'):
        from asf.record.core import canonicalize, load_items, today
        from asf.record.setfield import set_typed
        from asf.record.undeliver import HISTORY
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        lead_rec, member_rec = canonical[lead], canonical[member]
        kept = [m for m in lead_rec['meta'].get('delivers') or () if m != member]
        err = set_typed(lead_rec, {'delivers': kept if [m for m in kept if m != lead] else None},
                        history=[HISTORY.format(date=today(), who=f'{member} ', lead=lead, why=why)])
        self.assertIsNone(err)
        err = set_typed(member_rec, {'delivered_by': None},
                        history=[HISTORY.format(date=today(), who='', lead=lead, why=why)])
        self.assertIsNone(err)

    def test_an_undelivered_member_is_in_no_delivery_after_two_further_passes(self):
        self.assertEqual(self.run_pass(), {'T-0001': ['T-0001', 'T-0002', 'T-0003', 'T-0004']})
        self.undeliver('T-0001', 'T-0002')
        self.assertEqual(self.run_pass(), {})
        self.assertEqual(self.run_pass(), {})

    def test_the_undelivered_member_gains_no_after_edge_from_the_pass(self):
        self.run_pass()
        self.undeliver('T-0001', 'T-0002')
        self.run_pass()
        self.run_pass()
        # T-0002 keeps the after: it declared in the plan — the pass only ever writes after:
        # onto a slice's lead, and T-0002 is no lead of any delivery the pass formed
        self.assertEqual(read_meta(self.root, 'task', 'T-0002')['after'], ['T-0001'])

    def test_two_members_undelivered_from_the_same_lead_are_not_bundled_with_each_other(self):
        self.run_pass()
        self.undeliver('T-0001', 'T-0002')
        self.undeliver('T-0001', 'T-0003')
        self.assertEqual(self.run_pass(), {})
        self.assertEqual(self.run_pass(), {})
        for tid in ('T-0002', 'T-0003'):
            m = read_meta(self.root, 'task', tid)
            self.assertNotIn('delivers', m)
            self.assertNotIn('delivered_by', m)

    def test_the_members_that_stayed_keep_their_delivery_unchanged(self):
        self.run_pass()
        self.undeliver('T-0001', 'T-0002')
        self.undeliver('T-0001', 'T-0003')
        self.run_pass()
        self.run_pass()
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['delivers'], ['T-0001', 'T-0004'])
        self.assertEqual(read_meta(self.root, 'task', 'T-0004')['delivered_by'], 'T-0001')

    def test_a_later_free_task_is_bundled_with_neither_undelivered_task(self):
        self.run_pass()
        self.undeliver('T-0001', 'T-0002')
        self.undeliver('T-0001', 'T-0003')
        self.task('T-0005', ['e.py'])
        self.task('T-0006', ['f.py'])
        out = self.run_pass()
        for members in out.values():
            self.assertNotIn('T-0002', members)
            self.assertNotIn('T-0003', members)

    def test_an_undelivered_task_that_is_started_still_binds_a_lead_sharing_its_path(self):
        self.run_pass()
        self.undeliver('T-0001', 'T-0002')
        from asf.record import frontmatter
        from asf.record.core import canonicalize, load_items
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        # T-0002 is now started (Active, a PR open) — undelivered still means "excluded from
        # being bundled", never "excluded from being waited on" (PD6)
        frontmatter.write_machine(canonical['T-0002']['path'],
                                  {'state': 'Active', 'stage_since': '2026-01-01T00:00:00Z',
                                   'evidence': ['branch worker/T-0002, PR #9 OPEN'],
                                   'updated': '2026-01-01T00:00:00Z'})
        self.task('T-0005', ['x.py'])
        self.task('T-0006', ['b.py'])
        out = self.run_pass()
        self.assertEqual(out, {'T-0005': ['T-0005', 'T-0006']})
        self.assertEqual(read_meta(self.root, 'task', 'T-0005')['after'], ['T-0002'])


class NotOptedInTests(RecordFixture):
    """A product that says nothing keeps every lane as it is: the convention is off, the pass
    does not run, and the feeder emits its PLAN → CODE rows unchanged."""

    def test_the_default_is_the_task_lane(self):
        conv = conventions.Conventions.from_mapping({})
        self.assertFalse(conv.delivery_feature())
        self.assertEqual(conv.delivery, 'task')
        self.assertTrue(conventions.Conventions.from_mapping({'delivery': 'feature'}).delivery_feature())
        bad = conventions.Conventions.from_mapping({'delivery': 'story'})
        self.assertFalse(bad.delivery_feature())
        self.assertEqual([k for k, _ in bad.shape_findings()], ['delivery'])

    def test_the_record_step_runs_the_pass_only_when_opted_in(self):
        from asf.tick import tick as tick_mod
        source = open(tick_mod.__file__, encoding='utf-8').read()
        self.assertIn("if product.conventions.delivery_feature():", source)
        self.assertIn("slice_mod.deliveries", source)

    def test_plain_tasks_still_get_their_own_rows(self):
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True, 'state': 'Active',
                            'stage': 'plan-approved', 'children': ['T-0001', 'T-0002']},
                 'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                            'writes': ['a.py']},
                 'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                            'writes': ['b.py']}}
        out = rows.candidates({'items': items}, Product('sample', {'conventions': {
            'feeder': {'stories_before_plan': False}}}), [])
        self.assertEqual([(r.kind, r.item_id, r.launches) for r in out],
                         [(rows.PLAN_CODE, 'T-0001', True), (rows.PLAN_CODE, 'T-0002', True)])
        self.assertEqual(slice_mod.plan_slices({i: dict(v) for i, v in items.items()}, 'F-0001',
                                               product().conventions), [])  # undecided Tasks


# ---- the feeder ------------------------------------------------------------------------

class FeatureDeliveryRowsTest(unittest.TestCase):
    """One DELIVERY → CODE row for the slice's lead, WAITS ON delivery for the rest, no PLAN →
    CODE row; ``after:`` inside the delivery is commit order; a member whose lead landed
    without it is residual; an ``incomplete`` hold comes back as the same row."""

    def index(self, **over):
        items = {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True, 'state': 'Active',
                       'stage': 'plan-approved', 'children': ['T-0001', 'T-0002', 'T-0003']},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                       'writes': ['a.py'], 'delivers': ['T-0001', 'T-0002', 'T-0003']},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                       'writes': ['b.py'], 'after': ['T-0001'], 'delivered_by': 'T-0001'},
            'T-0003': {'id': 'T-0003', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                       'writes': ['c.py'], 'after': ['T-0002'], 'delivered_by': 'T-0001'},
        }
        for iid, fields in over.items():
            items.setdefault(iid, {'id': iid}).update(fields)
        return {'items': items}

    def test_one_row_for_the_lead_and_none_for_the_tasks(self):
        out = rows.candidates(self.index(), product(), [])
        self.assertEqual([r.kind for r in out if r.kind == rows.PLAN_CODE], [])
        lead = [r for r in out if r.item_id == 'T-0001']
        self.assertEqual(len(lead), 1)
        r = lead[0]
        self.assertEqual((r.kind, r.brief_kind, r.branch, r.feature_id, r.action),
                         (rows.DELIVERY_CODE, 'delivery-code', 'worker/T-0001', 'F-0001', rows.LAUNCH))
        self.assertIn('3 Tasks of F-0001: one branch, one PR, one review', r.reason)
        waits = {r.item_id: r for r in out if r.item_id in ('T-0002', 'T-0003')}
        for w in waits.values():
            self.assertEqual((w.kind, w.action, w.feature_id, w.launches),
                             (rows.DELIVERY_CODE, 'WAITS ON delivery T-0001', 'F-0001', False))
        launching = [r for r in rows.plan_rows(self.index(), product(), [], 3) if r.launches]
        self.assertEqual([(r.kind, r.item_id) for r in launching], [(rows.DELIVERY_CODE, 'T-0001')])

    def test_after_inside_the_delivery_is_commit_order_not_a_hold(self):
        out = rows.candidates(self.index(), product(), [])
        self.assertEqual([r for r in out if r.item_id == 'T-0001'][0].action, rows.LAUNCH)
        # an after: on a Task outside the delivery still holds the whole delivery
        idx = self.index(**{'T-0009': {'type': 'task', 'parent': 'F-0001', 'state': 'New',
                                       'writes': ['z.py']},
                            'T-0002': {'after': ['T-0001', 'T-0009']}})
        idx['items']['F-0001']['children'].append('T-0009')
        r = [r for r in rows.candidates(idx, product(), []) if r.item_id == 'T-0001'][0]
        self.assertEqual((r.action, r.waits_on), ('WAITS ON T-0009', 'T-0009'))

    def test_the_lead_holds_the_union_footprint_while_it_runs(self):
        idx = self.index(**{'F-0002': {'type': 'feature', 'decided': True, 'state': 'Active',
                                       'stage': 'plan-approved', 'children': ['T-0010']},
                            'T-0010': {'type': 'task', 'parent': 'F-0002', 'state': 'New',
                                       'writes': ['c.py']}})
        r = [r for r in rows.candidates(idx, product(), [{'item': 'T-0001'}])
             if r.item_id == 'T-0010'][0]
        self.assertEqual((r.action, r.waits_on), ('WAITS ON T-0001', 'T-0001'))

    def test_a_member_whose_lead_landed_without_it_is_residual(self):
        idx = self.index(**{'T-0001': {'state': 'Closed'}, 'T-0002': {'state': 'Closed'}})
        out = rows.candidates(idx, product(), [])
        self.assertEqual([(r.kind, r.item_id, r.launches) for r in out],
                         [(rows.PLAN_CODE, 'T-0003', True)])

    def test_an_incomplete_hold_comes_back_as_the_same_delivery_row(self):
        occ = {'corrections': {'T-0001': {'kind': lifecycle.INCOMPLETE, 'rounds': 1, 'same': 1,
                                          'branch': 'worker/T-0001', 'at': '2026-01-01T00:00:00Z',
                                          'text': 'delivery incomplete: no commit names T-0002, T-0003'}}}
        out = rows.candidates(self.index(), product(), [], occupancy=occ)
        mine = [r for r in out if r.item_id == 'T-0001']
        self.assertEqual(len(mine), 1)
        r = mine[0]
        self.assertEqual((r.kind, r.brief_kind, r.branch, r.feature_id, r.launches),
                         (rows.DELIVERY_CODE, 'delivery-code', 'worker/T-0001', 'F-0001', True))
        self.assertEqual(r.correction, 'delivery incomplete: no commit names T-0002, T-0003')
        self.assertIn('continue the delivery from the head of worker/T-0001', r.reason)
        self.assertEqual(rows.INCOMPLETE, lifecycle.INCOMPLETE)

    def test_a_lane_back_incomplete_branch_launches_the_resume_not_waits_on_landing(self):
        """2026-09-27, a product: delivery-code-t-0362 pushed T-0362, reported partial, the
        lane held cloud/T-0362 BACK (kind=incomplete) — and ``asf next`` said ``WAITS ON
        landing: cloud/T-0362 BACK`` every tick after: I4 allowed BACK the correction kinds
        but not the delivery's own resume row, so the feeder's gate turned it into a wait."""
        from asf import invariants
        occ = {'busy': {}, 'waiting_landing': {}, 'landing': {}, 'review': {},
               'corrections': {'T-0001': {'kind': lifecycle.INCOMPLETE, 'rounds': 1, 'same': 1,
                                          'branch': 'worker/T-0001', 'at': '2026-01-01T00:00:00Z',
                                          'text': 'delivery incomplete: no commit on '
                                                  'origin/worker/T-0001 names T-0002, T-0003 '
                                                  '(done: T-0001; the report says partial).'}}}
        lanes = {'worker/T-0001': {'state': lane.BACK, 'reason': f'kind={lifecycle.INCOMPLETE}'}}

        def gate(rs, _items):
            ctx = invariants.FeederContext(rows=list(rs), lanes=lanes, occupancy=occ)
            with mock.patch.object(invariants, 'feeder_context', return_value=ctx):
                return invariants.feeder_waits(product(), rs, _items, out=lambda _l: None)

        out = rows.plan_rows(self.index(), product(), [], 3, occupancy=occ, gate=gate)
        r = [r for r in out if r.item_id == 'T-0001'][0]
        self.assertEqual((r.kind, r.action, r.branch, r.brief_kind),
                         (rows.DELIVERY_CODE, rows.LAUNCH, 'worker/T-0001', 'delivery-code'))
        self.assertIn('done: T-0001', r.correction)
        self.assertIn('T-0002, T-0003', r.correction)

    def test_a_parked_incomplete_hold_launches_nothing(self):
        occ = {'corrections': {'T-0001': {'kind': lifecycle.INCOMPLETE, 'rounds': 2, 'same': 2,
                                          'parked': True, 'reason': 'no Task progress twice',
                                          'branch': 'worker/T-0001', 'at': '2026-01-01T00:00:00Z',
                                          'text': 'delivery incomplete: no commit names T-0002'}}}
        out = rows.candidates(self.index(), product(), [], occupancy=occ)
        mine = [r for r in out if r.item_id == 'T-0001']
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0].launches)
        self.assertTrue(mine[0].action.startswith(rows.PARKED), mine[0].action)

    def test_the_lead_counts_against_the_features_in_build_cap(self):
        idx = self.index(**{'F-0002': {'type': 'feature', 'decided': True, 'state': 'Active',
                                       'stage': 'building 0/1', 'children': ['T-0010'], 'rank': 0},
                            'T-0010': {'type': 'task', 'parent': 'F-0002', 'state': 'Active',
                                       'writes': ['z.py']}})
        p = product(feeder={'max_features_in_build': 1})
        # a correction row on F-0002's Task keeps it buildable; F-0001's delivery waits its turn
        occ = {'corrections': {'T-0010': {'kind': 'gate', 'rounds': 1, 'same': 1, 'text': 'red',
                                          'branch': 'worker/T-0010', 'at': '2026-01-01T00:00:00Z'}}}
        out = rows.plan_rows(idx, p, [], 4, occupancy=occ)
        r = [r for r in out if r.item_id == 'T-0001'][0]
        self.assertEqual(r.waits_on, 'finish')


class IdleBranchTests(unittest.TestCase):
    """An Active Task on an idle branch — no PR, no session, no lane state, no correction,
    nothing on the trunk — got no row at all and stalled for ever (a product's T-0362/T-0371,
    cloud runs dead on quota). Its coder is launched again on the same branch, worktree kept;
    under ``delivery: feature`` the delivery builds it instead."""

    def index(self, **over):
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True, 'state': 'Active',
                            'stage': 'building 0/1', 'children': ['T-0362']},
                 'T-0362': {'id': 'T-0362', 'type': 'task', 'parent': 'F-0001', 'state': 'Active',
                            'writes': ['a.py'], 'links': {'branches': ['cloud/T-0362']},
                            'evidence': ['branch cloud/T-0362 exists']}}
        for iid, fields in over.items():
            items.setdefault(iid, {'id': iid}).update(fields)
        return {'items': items}

    def rows_for(self, idx=None, inflight=(), **kw):
        return {r.item_id: r for r in rows.candidates(idx or self.index(), Product('sample', {}),
                                                      list(inflight), **kw)}

    def test_the_coder_is_launched_again_on_its_own_branch(self):
        r = self.rows_for()['T-0362']
        self.assertEqual((r.kind, r.brief_kind, r.branch, r.feature_id, r.action),
                         (rows.PLAN_CODE, 'task', 'cloud/T-0362', 'F-0001', rows.LAUNCH))
        self.assertIn('idle branch', r.reason)
        self.assertIn('worktree kept', r.reason)

    def test_not_idle_when_something_holds_it(self):
        self.assertNotIn('T-0362', self.rows_for(inflight=[{'item': 'T-0362'}]))   # a live session
        # pushed, or a PR open: no coder again — the one row is a WAITS ON landing (pushed
        # work is never silent, tests/test_pushed_work.py)
        occ = {'waiting_landing': {'T-0362': 'pushed, waiting to land'}}
        r = self.rows_for(occupancy=occ)['T-0362']                                  # pushed
        self.assertEqual((r.kind, r.launches), (rows.PUSHED_LAND, False))
        pr = self.index(**{'T-0362': {'evidence': ['branch cloud/T-0362, PR #7 OPEN']}})
        r = self.rows_for(pr)['T-0362']                                             # a PR
        self.assertEqual((r.kind, r.launches), (rows.PUSHED_LAND, False))
        corr = {'corrections': {'T-0362': {'kind': 'gate', 'rounds': 1, 'same': 1, 'text': 'red',
                                           'branch': 'cloud/T-0362', 'at': '2026-01-01T00:00:00Z'}}}
        by = self.rows_for(occupancy=corr)
        self.assertEqual(by['T-0362'].kind, rows.FIX_CORRECT)                        # a correction
        on_trunk = self.rows_for(landed_shas={'T-0362': ('c' * 40, 'feat(T-0362): done')})
        self.assertNotIn('T-0362', on_trunk)                                        # landed
        blocked = self.index(**{'T-0362': {'blocked': True, 'blocked_by_open': ['B-0001']}})
        self.assertNotIn('T-0362', self.rows_for(blocked))

    def test_under_a_feature_delivery_the_delivery_builds_it(self):
        idx = self.index(**{'T-0362': {'delivered_by': 'T-0360'},
                            'T-0360': {'type': 'task', 'parent': 'F-0001', 'state': 'New',
                                       'writes': ['b.py'], 'delivers': ['T-0360', 'T-0362']}})
        idx['items']['F-0001']['children'].append('T-0360')
        by = self.rows_for(idx)
        self.assertEqual((by['T-0360'].kind, by['T-0360'].launches), (rows.DELIVERY_CODE, True))
        self.assertEqual(by['T-0362'].action, 'WAITS ON delivery T-0360')


# ---- the lane ----------------------------------------------------------------------------

class IncompleteLaneTests(unittest.TestCase):
    """No PR while the delivery is not whole: the lane holds the branch ``incomplete`` and the
    same lead comes back; a ``done`` report lands the branch without the members it left out."""

    def facts(self, **kw):
        f = {'branch': 'worker/T-0001', 'item': 'T-0001', 'head': 'a' * 40, 'ended': True,
             'landed': False, 'live': False, 'ahead': 1, 'mode': 'pr', 'host': True,
             'now': 1_800_000_000.0, 'stale_after': 2 * 86400, 'review_required': True,
             'refusal': (lifecycle.INCOMPLETE, 'delivery incomplete: no commit names T-0002')}
        f.update(kw)
        return f

    def test_no_pr_while_partial(self):
        state, reason = lane.next_state(None, self.facts())
        self.assertEqual(state, lane.PUSHED)
        rec = {'state': state, 'head': 'a' * 40, 'pr': None, 'at': '2027-01-15T08:00:00Z', 'reason': reason}
        self.assertEqual(lane.next_state(rec, self.facts()), (lane.BACK, f'kind={lifecycle.INCOMPLETE}'))
        # the same facts without the refusal open the PR
        self.assertEqual(lane.next_state(rec, self.facts(refusal=None)), (lane.PR_OPEN, 'open a PR'))

    def refusal(self, named, missing, status, run=True):
        def members_named(_repo, _trunk, _branch, ids):
            return [i for i in ids if i in named], [i for i in ids if i in missing]
        result = {'result': f'REPORT\nitem: T-0001\nstatus: {status}\n'} if status is not None else None
        with mock.patch.object(lane, 'members_named', members_named), \
             mock.patch.object(lane.lifecycle, 'result_of', return_value=result):
            return lane.incomplete_refusal('repo', 'main', 'worker/T-0001',
                                           ('T-0001', 'T-0002', 'T-0003'),
                                           {'job': 'delivery-code-t-0001', 'log': 'x'} if run else None)

    def test_partial_crash_and_run_cap_hold_and_say_what_is_done(self):
        for status in ('partial', 'blocked', None):
            kind, text = self.refusal(['T-0001'], ['T-0002', 'T-0003'], status)
            self.assertEqual(kind, lifecycle.INCOMPLETE)
            self.assertIn('no commit on origin/worker/T-0001 names T-0002, T-0003', text)
            self.assertIn('done: T-0001', text)
            self.assertIn('build T-0002 next', text)
            self.assertIn('No PR opens', text)
        self.assertIn('the run ended without a report', self.refusal(['T-0001'], ['T-0002'], None)[1])

    def sessions(self, lines):
        import json
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path

    def run_line(self, n, started):
        return {'job': 'delivery-code-t-0001', 'pid': n, 'started': started,
                'branch': 'worker/T-0001', 'item': 'T-0001', 'kind': 'delivery-code',
                'launch_head': f'{n}' * 40}

    def incomplete(self, missing, at):
        return {'job': 'delivery-code-t-0001', 'correction': {
            'kind': lifecycle.INCOMPLETE, 'at': at, 'finding': list(missing),
            'same': 1, 'text': f"delivery incomplete: no commit names {', '.join(missing)}"}}

    def test_a_resume_that_moves_no_task_forward_twice_parks_the_lead(self):
        """Pause after the second failure: the first incomplete hold resumes; a resumed run that
        leaves the same Tasks unbuilt is the second — the lead is parked, not relaunched."""
        first = [self.run_line(1, 't1'),
                 {'job': 'delivery-code-t-0001', 'ended': 'e1', 'end_reason': 'finished'},
                 self.incomplete(['T-0002', 'T-0003'], 't2')]
        resumed = first + [self.run_line(2, 't3'),
                           {'job': 'delivery-code-t-0001', 'ended': 'e3', 'end_reason': 'finished'}]
        path = self.sessions(resumed)
        run = lifecycle.by_branch(path)['worker/T-0001']
        fields, line = lifecycle.hold(path, run, lifecycle.INCOMPLETE,
                                      'delivery incomplete: no commit names T-0002, T-0003', 't4',
                                      finding=['T-0002', 'T-0003'])
        corr = fields['correction']
        self.assertIs(corr.get('parked'), True, line)
        self.assertIn('no new Task', corr['reason'])
        self.assertIn('T-0002, T-0003', corr['reason'])
        self.assertTrue(line.startswith('parked worker/T-0001'), line)
        # progress — one more Task on the branch — is a new finding: it resumes again
        fields, line = lifecycle.hold(path, run, lifecycle.INCOMPLETE,
                                      'delivery incomplete: no commit names T-0003', 't4',
                                      finding=['T-0003'])
        self.assertNotIn('parked', fields['correction'], line)
        self.assertEqual(fields['correction']['same'], 1)

    def test_the_first_incomplete_hold_resumes(self):
        path = self.sessions([self.run_line(1, 't1'),
                              {'job': 'delivery-code-t-0001', 'ended': 'e1', 'end_reason': 'finished'}])
        run = lifecycle.by_branch(path)['worker/T-0001']
        fields, _line = lifecycle.hold(path, run, lifecycle.INCOMPLETE,
                                       'delivery incomplete: no commit names T-0002', 't2',
                                       finding=['T-0002'])
        self.assertNotIn('parked', fields['correction'])

    def test_done_lands_without_the_left_out_and_whole_is_no_refusal(self):
        self.assertIsNone(self.refusal(['T-0001'], ['T-0002'], 'done'))
        self.assertIsNone(self.refusal(['T-0001', 'T-0002', 'T-0003'], [], 'partial'))
        self.assertIsNone(self.refusal(['T-0001'], ['T-0002'], 'partial', run=False))

    def test_only_a_feature_delivery_is_held_incomplete(self):
        items = {'T-0001': {'type': 'task', 'delivers': ['T-0001', 'T-0002']},
                 'F-0097': {'type': 'feature', 'delivers': ['F-0097', 'B-0034']}}
        self.assertTrue(lane.feature_delivery(items, 'T-0001'))
        self.assertFalse(lane.feature_delivery(items, 'F-0097'))   # F-0102 D11 stands
        self.assertFalse(lane.feature_delivery(items, 'T-0002'))

    def test_the_delivery_row_may_continue_the_writers_session(self):
        self.assertIn('delivery-code', continuation.ANSWERING)


# ---- the ingest ------------------------------------------------------------------------

class IngestClosesEachTaskTest(unittest.TestCase):
    """One commit per Task on the one branch: the ingest closes each Task by its own commit,
    and a Task no commit names stays open — the record needs no new closing code."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_card(self.root, 'F-0001', 'feature', 'The reader', 'features', typed_lines=['decided: true'])
        write_card(self.root, 'T-0001', 'task', 'One', 'tasks', parent='F-0001',
                   typed_lines=['delivers: [T-0001, T-0002, T-0003]', 'writes: [a.py]'])
        write_card(self.root, 'T-0002', 'task', 'Two', 'tasks', parent='F-0001',
                   typed_lines=['delivered_by: T-0001', 'writes: [b.py]'])
        write_card(self.root, 'T-0003', 'task', 'Three', 'tasks', parent='F-0001',
                   typed_lines=['delivered_by: T-0001', 'writes: [c.py]'])

    def run_ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root)

    def test_each_task_closes_on_its_own_commit(self):
        ev = dict(EMPTY_EV, ci=True,
                  ids={**landed_ids('T-0001', 'a' * 40), **landed_ids('T-0002', 'b' * 40)})
        self.assertEqual(self.run_ingest(ev), 0)
        self.assertEqual(read_meta(self.root, 'task', 'T-0001')['state'], 'Closed')
        self.assertEqual(read_meta(self.root, 'task', 'T-0002')['state'], 'Closed')
        self.assertEqual(read_meta(self.root, 'task', 'T-0003')['state'], 'New')
        # a Task lead rides its Feature's plan: it gets no ladder of its own
        self.assertNotIn('stage', read_meta(self.root, 'task', 'T-0001'))


# ---- the brief, the PR, the budget, the scorecard -----------------------------------------

class SurfacesTests(unittest.TestCase):
    def member(self, mid, proves=''):
        return (mid, 'task', f'Task {mid}', ['a.py'], ['- [ ] it works'], [], proves)

    def test_the_review_gets_a_block_per_task_and_a_row_per_story_line(self):
        facts = {'members': [self.member('T-0001', 'S-0001 the reader: 1 it reads;\n        2 it writes'),
                             self.member('T-0002')]}
        text = build_mod.delivery_checks(facts)
        self.assertTrue(text.startswith('\n\nTHE BRANCH DELIVERS SEVERAL ITEMS'))
        self.assertIn('### T-0001 — Task T-0001 (task); writes: a.py', text)
        self.assertIn('### T-0002 — Task T-0002 (task)', text)
        self.assertIn('S-0001 the reader: 1 it reads;', text)
        self.assertIn('`proves: <S-id> line <n>` pass | fail', text)
        self.assertEqual(build_mod.delivery_checks({'members': [self.member('T-0001')]}), '')
        self.assertEqual(build_mod.delivery_checks({}), '')

    def test_the_delivery_code_brief_resumes_from_the_branch(self):
        text = build_mod.load_template('delivery-code')
        for line in ('RESUME, NEVER RESTART', '`git log origin/{main}..{branch}`',
                     'Push after every item\'s commit', 'Proves: <S-id> line <n>',
                     'no PR opens for\na partial delivery'):
            self.assertIn(line, text)
        row = types.SimpleNamespace(correction='delivery incomplete: no commit names T-0002')
        self.assertIn('no commit names T-0002', build_mod.correction_text(row, 'delivery-code'))
        self.assertEqual(build_mod.correction_text(row, 'task'), '')

    def test_the_pr_names_every_task_it_delivers(self):
        items = {'T-0001': {'id': 'T-0001', 'title': 'One', 'folder': 'tasks',
                            'delivers': ['T-0001', 'T-0002']},
                 'T-0002': {'id': 'T-0002', 'title': 'Two', 'folder': 'tasks'}}
        title, body = step_prs.title_and_body('T-0001', items['T-0001'], None, 'worker/T-0001', items)
        self.assertEqual(title, 'T-0001 — One (delivers T-0002)')
        self.assertIn('## Delivers\n- [T-0002](tasks/T-0002.md) — Two', body)
        plain, _ = step_prs.title_and_body('T-0002', items['T-0002'], None, 'worker/T-0002', items)
        self.assertEqual(plain, 'T-0002 — Two')

    def test_a_lead_has_the_budget_of_its_members(self):
        conv = conventions.Conventions.from_mapping({})
        b = budget.of(conv, {'type': 'task', 'delivers': ['T-0001', 'T-0002', 'T-0003']})
        self.assertEqual((b.sessions, b.usd), (9, 30))
        self.assertEqual(budget.of(conv, {'type': 'task'}).sessions, 3)
        own = budget.of(conv, {'type': 'task', 'delivers': ['T-0001', 'T-0002'], 'budget_sessions': 4})
        self.assertEqual((own.sessions, own.source), (4, 'item'))

    def test_the_scorecard_reads_the_unit_off_the_tasks(self):
        def items(grouped):
            return {'F-0001': {'id': 'F-0001', 'type': 'feature', 'parent': None, 'landed': '2026-09-03T10:00:00Z'},
                    'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001',
                               'delivers': ['T-0001', 'T-0002'] if grouped else []},
                    'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001',
                               'delivered_by': 'T-0001' if grouped else None}}
        self.assertEqual(score.unit_of(items(True), items(True)['F-0001']), score.UNIT_FEATURE)
        self.assertEqual(score.unit_of(items(False), items(False)['F-0001']), score.UNIT_TASK)
        mixed = items(True)
        mixed['T-0003'] = {'id': 'T-0003', 'type': 'task', 'parent': 'F-0001'}
        self.assertEqual(score.unit_of(mixed, mixed['F-0001']), score.UNIT_MIXED)
        self.assertEqual(score.unit_of({}, {'id': 'F-0009', 'lane': 'direct'}), score.UNIT_DIRECT)


if __name__ == '__main__':
    unittest.main()
