"""asf.feeder: rows, the footprint gate, the stalemate gate, the S1 lane tiers, the incident clock
and ``asf next`` — against the fixture index under tests/fixtures/feeder/."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import env
from asf.env import Product
from asf.feeder import footprint, register, render, rows, tiers
try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from occfixture import occ
except ImportError:  # pragma: no cover - import shape only
    from tests.occfixture import occ

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, 'fixtures', 'feeder')
NOW = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone.utc)
S1_SESSION = {'item': 'B-0001', 'kind': 'fix-bug', 'account': 'w1', 'age': '5m'}


def product(**extra):
    conv = {'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}
    conv.update(extra.pop('conventions', {}))
    return Product('sample', dict({'conventions': conv}, **extra))


def fixture_index():
    with open(os.path.join(FIXTURES, 'index.json'), encoding='utf-8') as f:
        return json.load(f)


def golden(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
        return f.read()


def kinds(rs):
    return [(r.kind, r.item_id) for r in rs]


class DeclaredOrderIsADependency(unittest.TestCase):
    """B-0076: a sequential plan says so with `after:`; without it tasks stay footprint-parallel."""

    def items(self, first='New'):
        return {'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'building 0/2',
                           'state': 'Active', 'children': ['T-0001', 'T-0002']},
                'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                           'state': first, 'writes': ['a.py']},
                'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'rank': 2,
                           'state': 'New', 'writes': ['b.py'], 'after': ['T-0001']}}

    def rows_for(self, items):
        return {r.item_id: r for r in rows.task_rows(items, product(), items['F-0001'], set(), [])}

    def test_a_task_waits_for_the_one_it_declares_after(self):
        by = self.rows_for(self.items())
        self.assertEqual(by['T-0001'].action, 'would launch')
        self.assertEqual(by['T-0002'].action, 'WAITS ON T-0001')

    def test_it_runs_once_that_one_landed(self):
        by = self.rows_for(self.items(first='Closed'))
        self.assertEqual(by['T-0002'].action, 'would launch')


class AnAfterOnAMergedTaskWaitsOnItsAbsorber(unittest.TestCase):
    """A groom merge removes T-0002 into T-0001 (``merged: [T-0002]``); the index reader drops
    the removed card, so an ``after: [T-0002]`` on T-0003 named an id that never lands again and
    T-0003 waited on it for ever (asf 2026-09-25: T-0058, T-0096, T-0134, T-0163, T-0192)."""

    def index(self, absorber='New'):
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'building 0/2', 'decided': True,
                       'state': 'Active', 'children': ['T-0001', 'T-0003', 'T-0004']},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                       'decided': True, 'state': absorber, 'writes': ['a.py'],
                       'merged': ['T-0002']},
            'T-0003': {'id': 'T-0003', 'type': 'task', 'parent': 'F-0001', 'rank': 3,
                       'decided': True, 'state': 'New', 'writes': ['c.py'], 'after': ['T-0002']},
            'T-0004': {'id': 'T-0004', 'type': 'task', 'parent': 'F-0001', 'rank': 4,
                       'decided': True, 'state': 'New', 'writes': ['d.py'],
                       'after': ['T-0001', 'T-0002']}}}

    def by(self, idx):
        return {r.item_id: r for r in rows.candidates(idx, product(), [])}

    def test_it_waits_on_the_task_that_absorbed_it(self):
        by = self.by(self.index())
        self.assertEqual(by['T-0003'].action, 'WAITS ON T-0001')
        self.assertEqual(by['T-0004'].action, 'WAITS ON T-0001')

    def test_it_runs_once_the_absorber_landed(self):
        by = self.by(self.index(absorber='Closed'))
        self.assertEqual(by['T-0003'].action, 'would launch')
        self.assertEqual(by['T-0004'].action, 'would launch')

    def test_an_absorber_does_not_wait_on_what_it_absorbed(self):
        idx = self.index()
        idx['items']['T-0001']['after'] = ['T-0002']
        self.assertEqual(self.by(idx)['T-0001'].action, 'would launch')


class NoRowLaunchesBehindAnUnlandedPredecessor(unittest.TestCase):
    """B-0080: `after:` held the PLAN → CODE row only; a held branch's correction and adjudicate
    rows launched anyway (on Opus) for an item that was not in dispute, only waiting."""

    def index(self, first='New'):
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'building 0/2', 'decided': True,
                       'state': 'Active', 'children': ['T-0001', 'T-0002', 'B-0009']},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                       'state': first, 'writes': ['a.py']},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'rank': 2,
                       'state': 'New', 'after': ['T-0001'], 'writes': ['b.py']},
            'B-0009': {'id': 'B-0009', 'type': 'bug', 'parent': 'F-0001', 'severity': 'S1',
                       'decided': True, 'state': 'New', 'after': ['T-0001']}}}

    def rows_of(self, item_id, first='New', rounds=None, attempts=None):
        corr = ({i: {'kind': 'gate', 'text': 'FAIL: x', 'rounds': rounds}
                 for i in ('T-0002', 'B-0009')} if rounds is not None else None)
        return [r for r in rows.candidates(self.index(first), product(), [], attempts=attempts,
                                           occupancy=occ(corrections=corr)) if r.item_id == item_id]

    CASES = (dict(), dict(rounds=1), dict(rounds=3), dict(attempts={'B-0009': 3}))

    def test_every_row_kind_is_held_and_says_so_once(self):
        for kw in self.CASES:
            for iid in ('T-0002', 'B-0009'):
                with self.subTest(item=iid, **kw):
                    rs = self.rows_of(iid, **kw)
                    self.assertEqual([(r.action, r.waits_on) for r in rs],
                                     [('WAITS ON T-0001', 'T-0001')])

    def test_property_no_row_launches_for_an_unlanded_predecessor(self):
        for first in ('New', 'Active'):
            for kw in self.CASES:
                for iid in ('T-0002', 'B-0009'):
                    with self.subTest(first=first, item=iid, **kw):
                        self.assertFalse(any(r.launches for r in self.rows_of(iid, first, **kw)))

    def test_a_held_s1_row_does_not_stop_the_features(self):
        out = rows.plan_rows(self.index(), product(), [], 3)
        self.assertIn(('PLAN → CODE', 'T-0001', True), [(r.kind, r.item_id, r.launches) for r in out])

    def test_once_the_predecessor_landed_the_rows_launch(self):
        for kw in (dict(rounds=1), dict(rounds=3)):
            with self.subTest(**kw):
                rs = self.rows_of('T-0002', first='Closed', **kw)
                self.assertTrue(rs and all(r.launches for r in rs))


class FootprintHoldersAreLiveRuns(unittest.TestCase):
    """B-0076: only a live run (or one awaiting harvest) holds its files."""

    def items(self):
        return {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active', 'writes': ['a.py']},
                'T-0002': {'id': 'T-0002', 'type': 'task', 'state': 'New', 'writes': ['a.py']}}

    def test_an_active_card_alone_holds_nothing(self):
        self.assertEqual(rows.running_footprints(self.items(), set()), [])

    def test_a_live_run_holds_its_files(self):
        self.assertEqual(rows.running_footprints(self.items(), {'T-0001'}), [('T-0001', ['a.py'])])


class ADoneCardHoldsNoFootprint(unittest.TestCase):
    """A Resolved/Closed card's branch is on the trunk: even when the ledger still lists it as
    awaiting harvest, it blocks no sibling ("WAITS ON" a landed Task for ever)."""

    def index(self, state):
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'building 1/2', 'decided': True,
                       'state': 'Active', 'children': ['T-0001', 'T-0002']},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                       'state': state, 'writes': ['a.py']},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'rank': 2,
                       'state': 'New', 'writes': ['a.py']}}}

    def action(self, state):
        by = {r.item_id: r for r in rows.candidates(self.index(state), product(), [],
                                                    occupancy=occ(busy={'T-0001'}))}
        return by['T-0002'].action

    def test_an_open_card_awaiting_harvest_holds_its_files(self):
        self.assertEqual(self.action('Active'), 'WAITS ON T-0001')

    def test_a_done_card_awaiting_harvest_holds_nothing(self):
        self.assertEqual(self.action('Resolved'), 'would launch')
        self.assertEqual(self.action('Closed'), 'would launch')


class FootprintTest(unittest.TestCase):
    def test_same_file(self):
        self.assertTrue(footprint.overlaps(['a/b.ts'], ['a/b.ts']))

    def test_glob_matches_file_either_way(self):
        self.assertTrue(footprint.overlaps(['apps/web/*.ts'], ['apps/web/a.ts']))
        self.assertTrue(footprint.overlaps(['apps/web/a.ts'], ['apps/**']))

    def test_nested_globs(self):
        self.assertTrue(footprint.globs_overlap('apps/web/**', 'apps/web/lib/*.ts'))

    def test_directory(self):
        self.assertTrue(footprint.globs_overlap('apps/web/', 'apps/web/x/y.ts'))

    def test_disjoint(self):
        self.assertIsNone(footprint.overlaps(['apps/web/**'], ['apps/api/**', 'docs/x.md']))
        self.assertIsNone(footprint.overlaps([], ['a']))

    def test_first_conflict_names_the_running_task(self):
        running = [('T-1', ['docs/a.md']), ('T-2', ['src/**'])]
        self.assertEqual(footprint.first_conflict(['src/x.py'], running), 'T-2')
        self.assertIsNone(footprint.first_conflict(['lib/x.py'], running))


class RowsTest(unittest.TestCase):
    def setUp(self):
        self.index = fixture_index()
        self.p = product()

    def cand(self, inflight=()):
        return rows.candidates(self.index, self.p, list(inflight))

    def test_decided_card_without_spec(self):
        r = [r for r in self.cand() if r.item_id == 'F-0001'][0]
        self.assertEqual((r.kind, r.brief_kind, r.branch), ('CARD → SPEC', 'spec', 'spec/F-0001'))

    def test_removed_cards_emit_nothing(self):
        ids = {r.item_id for r in self.cand()}
        self.assertNotIn('F-0008', ids)

    def test_two_tasks_sharing_a_file_one_waits(self):
        by = {r.item_id: r for r in self.cand()}
        self.assertEqual(by['T-0001'].action, 'would launch')
        self.assertEqual(by['T-0002'].action, 'WAITS ON T-0001')
        self.assertEqual(by['T-0002'].waits_on, 'T-0001')

    def test_an_active_card_with_nobody_writing_it_holds_nothing(self):
        # B-0076: `Active` is a card state, not a worker. A held branch must not block a sibling
        # that shares a file — that deadlocked a whole Feature; the two meet at the rebase.
        by = {r.item_id: r for r in self.cand()}
        self.assertEqual(by['T-0003'].action, 'would launch')

    def test_an_inflight_task_holds_its_footprint_too(self):
        idx = copy.deepcopy(self.index)
        idx['items']['T-0007']['state'] = 'New'
        by = {r.item_id: r for r in rows.candidates(idx, self.p, [{'item': 'T-0007', 'kind': 'task'}])}
        self.assertEqual(by['T-0003'].action, 'WAITS ON T-0007')
        self.assertNotIn('T-0007', by)

    def test_stalemate_is_the_only_row_for_its_feature(self):
        f3 = [r for r in self.cand() if r.feature_id == 'F-0003']
        self.assertEqual(kinds(f3), [('STALEMATE → ADJUDICATE', 'F-0003')])
        self.assertEqual(f3[0].brief_kind, 'adjudicate')

    def test_stalemate_suppresses_branch_rows_too(self):
        idx = copy.deepcopy(self.index)
        idx['items']['F-0007']['stage'] = 'spec-review r5'
        self.assertEqual(kinds(r for r in rows.candidates(idx, self.p, []) if r.feature_id == 'F-0007'),
                         [('STALEMATE → ADJUDICATE', 'F-0007')])

    def test_review_round_below_the_limit_is_starved(self):
        idx = copy.deepcopy(self.index)
        idx['items']['F-0003']['stage'] = 'plan-review r3'
        f3 = [r for r in rows.candidates(idx, self.p, []) if r.feature_id == 'F-0003']
        self.assertEqual(kinds(f3), [('STARVED → PLAN', 'F-0003')])

    def test_stalemate_round_is_configurable(self):
        p = product(conventions={'stalemate_round': 5})
        f3 = [r for r in rows.candidates(self.index, p, []) if r.feature_id == 'F-0003']
        self.assertEqual(kinds(f3), [('STARVED → PLAN', 'F-0003')])

    def test_adjudicator_in_flight_leaves_the_feature_silent(self):
        self.assertEqual([r for r in self.cand([{'item': 'F-0003'}]) if r.feature_id == 'F-0003'], [])

    def test_starved_spec_and_plan(self):
        by = {r.item_id: r for r in self.cand()}
        self.assertEqual(by['F-0004'].kind, 'STARVED → SPEC')
        self.assertEqual(by['F-0005'].kind, 'STARVED → PLAN')
        self.assertEqual(by['F-0005'].branch, 'plan/F-0005')

    def test_a_feature_with_a_session_is_not_starved(self):
        self.assertNotIn('F-0004', {r.item_id for r in self.cand([{'item': 'F-0004'}])})

    def test_conflict_and_stale(self):
        by = {r.item_id: r for r in self.cand()}
        self.assertEqual((by['T-0007'].kind, by['T-0007'].branch), ('CONFLICT → REBASE', 'task/T-0007'))  # its recorded branch
        self.assertEqual((by['T-0006'].kind, by['T-0006'].brief_kind), ('STALE → CLOSE', 'close'))

    def test_bug_rows(self):
        bugs = [r for r in self.cand() if r.kind == 'BUG → FIX']
        self.assertEqual([(r.item_id, r.tier, r.brief_kind, r.branch) for r in bugs],
                         [('B-0001', 0, 'fix-bug', 'fix/B-0001'), ('B-0002', 1, 'fix-bug', 'fix/B-0002')])

    def test_bug_with_a_session_is_not_a_launching_row(self):
        rs = [r for r in self.cand([S1_SESSION]) if r.item_id == 'B-0001']
        self.assertEqual([(r.launches, r.waits_on) for r in rs], [(False, 'session')])

    def test_fix_prefix_from_conventions(self):
        p = product(conventions={'branch_prefixes': {'fix': 'hotfix-'}})
        by = {r.item_id: r for r in rows.candidates(self.index, p, [])}
        self.assertEqual(by['B-0001'].branch, 'hotfix-B-0001')

    def test_branch_for_reads_the_products_prefix(self):
        import inspect
        self.assertNotIn('default', inspect.signature(rows.branch_for).parameters)
        p = product(conventions={'branch_prefixes': {'code': 'feature/'}})
        self.assertEqual(rows.branch_for(p, 'code', 'T-0001'), 'feature/T-0001')
        self.assertEqual(rows.branch_for(None, 'code', 'T-0001'), 'worker/T-0001')

    def test_blocked_feature_emits_nothing(self):
        idx = copy.deepcopy(self.index)
        idx['items']['F-0001']['blocked'] = True
        self.assertNotIn('F-0001', {r.item_id for r in rows.candidates(idx, self.p, [])})

    def test_takes_a_bare_item_map(self):
        self.assertEqual(kinds(rows.candidates(self.index['items'], self.p, [])), kinds(self.cand()))


class FeederHoldTest(unittest.TestCase):
    """``feeder.hold``: a held class's new-work rows wait on the hold; everything else runs."""

    def idx(self):
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True, 'state': 'Active',
                       'rank': 1, 'stage': 'building 0/2', 'children': ['T-0001', 'T-0002']},
            'F-0002': {'id': 'F-0002', 'type': 'feature', 'decided': True, 'state': 'New',
                       'rank': 2, 'stage': 'card'},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                       'writes': ['src/one.py']},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'state': 'Active',
                       'writes': ['src/two.py']},
            'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'New', 'severity': 'S2',
                       'decided': True}}}

    CORRECTIONS = {'T-0002': {'kind': 'unpushed', 'text': 'push it', 'rounds': 1}}

    def launching(self, p):
        rs = rows.candidates(self.idx(), p, [], occupancy=occ(corrections=self.CORRECTIONS))
        return {(r.kind, r.item_id): r.action for r in rs}

    def test_the_default_holds_nothing(self):
        self.assertEqual(product().feeder_hold, frozenset())
        acts = self.launching(product())
        self.assertEqual(acts[(rows.PLAN_CODE, 'T-0001')], rows.LAUNCH)
        self.assertEqual(acts[(rows.CARD_SPEC, 'F-0002')], rows.LAUNCH)

    def test_features_held_waits_their_new_work_and_nothing_else(self):
        acts = self.launching(product(feeder={'hold': ['features']}))
        self.assertEqual(acts[(rows.PLAN_CODE, 'T-0001')], 'WAITS ON hold: features')
        self.assertEqual(acts[(rows.CARD_SPEC, 'F-0002')], 'WAITS ON hold: features')
        self.assertEqual(acts[(rows.BUG_FIX, 'B-0001')], rows.LAUNCH)
        self.assertEqual(acts[(rows.FIX_CORRECT, 'T-0002')], rows.LAUNCH)

    def test_bugs_held_waits_the_fix(self):
        acts = self.launching(product(feeder={'hold': ['bugs']}))
        self.assertEqual(acts[(rows.BUG_FIX, 'B-0001')], 'WAITS ON hold: bugs')
        self.assertEqual(acts[(rows.PLAN_CODE, 'T-0001')], rows.LAUNCH)

    def test_the_product_file_declares_it(self):
        self.assertEqual(env.validate_product_text('feeder:\n  hold: [features]\n'), [])
        self.assertTrue(env.validate_product_text('feeder:\n  hold: features\n'))
        self.assertTrue(env.validate_product_text('feeder:\n  other: 1\n'))


class ReshapeRowsTest(unittest.TestCase):
    """T-0053 / S-7904: the feeder holds a reshape-marked Task and launches one reshape session;
    an unconfirmed split part waits too. Both holds are on the Task, never the Feature (D10)."""

    def idx(self, tasks):
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True, 'state': 'Active',
                            'rank': 1, 'stage': 'building 1/2', 'children': list(tasks)}}
        items.update(tasks)
        return {'items': items}

    def task(self, tid, **over):
        base = {'id': tid, 'type': 'task', 'parent': 'F-0001', 'state': 'New',
               'writes': [f'asf/{tid}.py']}
        base.update(over)
        return base

    def test_reshape_holds_the_task_and_launches_a_reshape(self):
        idx = self.idx({'T-0050': self.task(
            'T-0050', reshape='split asf/feeder | asf/harvest (groom 2026-09-22)')})
        by_id = [r for r in rows.candidates(idx, product(), []) if r.item_id == 'T-0050']
        waits = [r for r in by_id if r.kind == rows.PLAN_CODE]
        launched = [r for r in by_id if r.kind == rows.RESHAPE]
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0].action, waits[0].waits_on, waits[0].launches),
                         ('WAITS ON reshape', 'reshape', False))
        self.assertEqual(len(launched), 1)
        r = launched[0]
        self.assertEqual((r.brief_kind, r.branch, r.feature_id, r.action),
                         ('reshape', 'plan/T-0050', 'F-0001', rows.LAUNCH))

    def test_other_tasks_of_the_feature_still_launch(self):
        idx = self.idx({
            'T-0050': self.task('T-0050',
                                reshape='split asf/feeder | asf/harvest (groom 2026-09-22)'),
            'T-0051': self.task('T-0051', writes=['docs/other.md']),
        })
        by = {r.item_id: r for r in rows.candidates(idx, product(), []) if r.kind == rows.PLAN_CODE}
        self.assertEqual(by['T-0051'].action, rows.LAUNCH)

    def test_busy_reshape_emits_nothing(self):
        idx = self.idx({'T-0050': self.task(
            'T-0050', reshape='split asf/feeder | asf/harvest (groom 2026-09-22)')})
        out = rows.candidates(idx, product(), [{'item': 'T-0050'}])
        self.assertNotIn('T-0050', {r.item_id for r in out})

    def test_unconfirmed_part_waits(self):
        idx = self.idx({'T-0060': self.task('T-0060', split_from='T-0050')})
        r = [r for r in rows.candidates(idx, product(), []) if r.item_id == 'T-0060'][0]
        self.assertEqual((r.action, r.waits_on, r.launches), ('WAITS ON confirm', 'confirm', False))

        idx2 = self.idx({'T-0060': self.task('T-0060', split_from='T-0050', decided=True)})
        r2 = [r for r in rows.candidates(idx2, product(), []) if r.item_id == 'T-0060'][0]
        self.assertEqual(r2.action, rows.LAUNCH)

    def test_reshape_row_takes_a_slot_and_waits_take_none(self):
        idx = self.idx({
            'T-0050': self.task('T-0050',
                                reshape='split asf/feeder | asf/harvest (groom 2026-09-22)'),
            'T-0060': self.task('T-0060', split_from='T-0050'),
        })
        cand = rows.candidates(idx, product(), [])
        self.assertEqual(kinds(cand), [(rows.RESHAPE, 'T-0050'), (rows.PLAN_CODE, 'T-0050'),
                                       (rows.PLAN_CODE, 'T-0060')])
        out = rows.plan_rows(idx, product(), [], 1)
        self.assertEqual([(r.kind, r.item_id, r.launches) for r in out],
                         [(rows.RESHAPE, 'T-0050', True)])


class GroomRowTests(unittest.TestCase):
    """T8: one GROOM → ADJUDICATE row per groom day, for every question the policy pass did not
    answer, capped at ``groom.adjudicate_attempts`` sessions (§2.5)."""

    def setUp(self):
        self.index = fixture_index()
        self.auto = product(approvals={'groom': 'auto'})

    def state(self, **over):
        base = {'date': '2026-09-22', 'open': ['F-0001', 'F-0003'], 'oldest': 'F-0001',
               'attempts': 0, 'file': 'groom/2026-09-22.md',
               'answers': 'state/groom/2026-09-22.answers',
               'lines': ['- [ ] F-0001 … → answer: ____', '- [ ] F-0003 … → answer: ____']}
        base.update(over)
        return base

    def groom_rows(self, p=None, inflight=(), **state_over):
        out = rows.candidates(self.index, p or self.auto, list(inflight),
                              groom_state=self.state(**state_over))
        return [r for r in out if r.kind == rows.GROOM_ADJUDICATE]

    def test_row_emitted_with_two_open_questions(self):
        gr = self.groom_rows()
        self.assertEqual(len(gr), 1)
        r = gr[0]
        self.assertEqual((r.item_id, r.brief_kind, r.branch, r.tier),
                         ('F-0001', 'groom', 'groom/2026-09-22', 2))
        self.assertEqual(r.groom_date, '2026-09-22')
        self.assertEqual(r.groom_file, 'groom/2026-09-22.md')
        self.assertEqual(r.answers_file, 'state/groom/2026-09-22.answers')
        self.assertEqual(r.open_questions,
                         ('- [ ] F-0001 … → answer: ____', '- [ ] F-0003 … → answer: ____'))

    def test_not_emitted_when_the_product_is_not_auto(self):
        self.assertEqual(self.groom_rows(p=product()), [])

    def test_not_emitted_when_open_is_empty(self):
        self.assertEqual(self.groom_rows(open=[], oldest=None), [])

    def test_not_emitted_when_a_live_session_holds_the_groom_day(self):
        self.assertEqual(self.groom_rows(inflight=[{'job': 'groom-2026-09-22'}]), [])

    def test_not_emitted_past_the_attempt_cap(self):
        self.assertEqual(self.groom_rows(attempts=2), [])

    def test_still_emitted_below_the_attempt_cap(self):
        self.assertEqual(len(self.groom_rows(attempts=1)), 1)

    def test_no_groom_state_is_todays_behaviour(self):
        self.assertEqual(kinds(rows.candidates(self.index, self.auto, [])),
                         kinds(rows.candidates(self.index, self.auto, [], groom_state=None)))

    def test_adjudicate_attempts_is_configurable(self):
        p = product(approvals={'groom': 'auto'}, groom={'adjudicate_attempts': 3})
        self.assertEqual(len(self.groom_rows(p=p, attempts=2)), 1)
        self.assertEqual(len(self.groom_rows(p=p, attempts=3)), 0)


def ten_features_and_an_s1():
    items = {'B-0001': {'id': 'B-0001', 'type': 'bug', 'title': 'Down', 'severity': 'S1',
                        'decided': True, 'state': 'New'}}
    for n in range(1, 11):
        fid = f"F-{n:04d}"
        items[fid] = {'id': fid, 'type': 'feature', 'title': f"Feature {n}", 'decided': True,
                      'rank': 11 - n, 'stage': 'card', 'state': 'New'}
    return {'items': items}


def s1_bugs(*ids):
    """Open, decided S1 Bugs; the first id listed is the oldest card."""
    return {'items': {i: {'id': i, 'type': 'bug', 'title': i, 'severity': 'S1', 'decided': True,
                          'state': 'New', 'stage_since': f"2026-01-01T0{n}:00:00Z"}
                      for n, i in enumerate(ids)}}


class AttemptOrderTest(unittest.TestCase):
    """B-0026: within a tier, fewer attempts first, so a spent Bug cannot starve a fresh one."""

    def test_the_fresh_bug_launches_before_four_attempted_ones(self):
        idx = s1_bugs('B-0003', 'B-0008', 'B-0014', 'B-0020', 'B-0023')
        attempts = {'B-0003': 1, 'B-0008': 1, 'B-0014': 2, 'B-0020': 1}
        out = rows.plan_rows(idx, product(), [], 1, attempts=attempts)
        self.assertEqual([r.item_id for r in out if r.launches], ['B-0023'])
        self.assertEqual([r.item_id for r in out],  # the rest wait on a slot, fewest attempts first
                         ['B-0023', 'B-0003', 'B-0008', 'B-0020', 'B-0014'])

    def test_equal_attempts_the_older_card_first_then_id(self):
        idx = s1_bugs('B-0002', 'B-0001', 'B-0003')  # B-0002 is the oldest card
        out = rows.plan_rows(idx, product(), [], 3, attempts={})
        self.assertEqual([r.item_id for r in out], ['B-0002', 'B-0001', 'B-0003'])

    def test_no_attempts_given_keeps_the_order_by_age(self):
        out = rows.plan_rows(s1_bugs('B-0001', 'B-0002'), product(), [], 2)
        self.assertEqual([r.item_id for r in out], ['B-0001', 'B-0002'])

    def test_three_attempts_is_an_adjudicate_row_not_a_fourth_session(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 3})
        self.assertEqual([(r.kind, r.item_id, r.brief_kind) for r in out],
                         [('STALEMATE → ADJUDICATE', 'B-0001', 'adjudicate')])

    def test_two_attempts_still_launch_a_fix(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 2})
        self.assertEqual(kinds(out), [('BUG → FIX', 'B-0001')])

    def test_once_adjudicated_the_bug_is_not_relaunched(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 4})
        self.assertEqual([(r.item_id, r.launches, r.waits_on) for r in out],
                         [('B-0001', False, 'operator')])


class NoS1S2BugIsInvisible(unittest.TestCase):
    """Inbox "NEXT drops S1/S2 bugs silently": every skip branch of :func:`rows.bug_rows` for a
    decided, open S1/S2 Bug is a non-launching WAITS row that says why — busy, blocked, Active
    with nothing else speaking for it, over the attempt limit — so NEXT always names it."""

    def cand(self, items=None, inflight=(), occupancy=None, attempts=None):
        idx = items or s1_bugs('B-0001')
        return [r for r in rows.candidates(idx, product(), list(inflight), attempts=attempts,
                                           occupancy=occupancy) if r.item_id == 'B-0001']

    def only_wait(self, rs):
        self.assertEqual(len(rs), 1, rs)
        self.assertFalse(rs[0].launches)
        self.assertTrue(rs[0].action.startswith('WAITS ON'), rs[0].action)
        self.assertEqual(rs[0].tier, 0)
        return rs[0]

    def test_a_live_session_is_a_waits_row(self):
        r = self.only_wait(self.cand(inflight=[S1_SESSION]))
        self.assertEqual(r.waits_on, 'session')

    def test_work_waiting_to_land_is_a_waits_row(self):
        r = self.only_wait(self.cand(occupancy=occ(busy=['B-0001'])))
        self.assertEqual(r.waits_on, 'landing')
        self.assertIn('waiting to land', r.reason)

    def test_a_blocked_bug_names_its_blockers(self):
        idx = s1_bugs('B-0001')
        idx['items']['B-0001'].update(blocked=True, blocked_by_open=['T-0009'])
        r = self.only_wait(self.cand(idx))
        self.assertEqual((r.action, r.waits_on), ('WAITS ON T-0009', 'T-0009'))

    def test_an_active_bug_with_no_branch_row_is_a_waits_row(self):
        idx = s1_bugs('B-0001')
        idx['items']['B-0001']['state'] = 'Active'
        r = self.only_wait(self.cand(idx))
        self.assertEqual(r.waits_on, 'branch')

    def test_over_the_attempt_limit_waits_on_the_operator(self):
        r = self.only_wait(self.cand(attempts={'B-0001': 4}))
        self.assertEqual(r.waits_on, 'operator')
        self.assertIn('4 sessions', r.reason)

    def test_a_correction_row_speaks_for_it_once(self):
        corr = {'B-0001': {'kind': 'gate', 'text': 'FAIL: x', 'rounds': 1}}
        rs = self.cand(occupancy=occ(corrections=corr))
        self.assertEqual([(r.kind, r.launches) for r in rs], [(rows.FIX_CORRECT, True)])

    def test_s3_and_undecided_bugs_stay_out(self):
        idx = s1_bugs('B-0001')
        for extra in ({'severity': 'S3'}, {'decided': False}):
            with self.subTest(**extra):
                i = copy.deepcopy(idx)
                i['items']['B-0001'].update(extra)
                self.assertFalse([r for r in self.cand(i, inflight=[S1_SESSION])
                                  if r.kind == rows.BUG_FIX])

    def test_shown_even_with_no_free_slot(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [S1_SESSION], 1)
        self.assertEqual([(r.item_id, r.action) for r in out], [('B-0001', 'WAITS ON session')])

    def test_a_waits_row_takes_no_slot_and_holds_no_tier(self):
        idx = s1_bugs('B-0001')
        idx['items']['F-0001'] = {'id': 'F-0001', 'type': 'feature', 'decided': True,
                                  'state': 'New', 'stage': 'card', 'rank': 1}
        out = rows.plan_rows(idx, product(), [S1_SESSION], 3)
        self.assertIn((rows.CARD_SPEC, 'F-0001', True), [(r.kind, r.item_id, r.launches) for r in out])


class CapOverEveryLaunchingKindTest(unittest.TestCase):
    """F-0080 §2.6 / §3.6: one adjudicate row at the limit, silence above it, for every kind."""

    def feature(self, **over):
        f = {'id': 'F-0001', 'type': 'feature', 'title': 'F', 'decided': True, 'rank': 1,
             'stage': 'card', 'state': 'New'}
        f.update(over)
        return {'items': {'F-0001': f}}

    def task_index(self, **over):
        t = {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'New', 'writes': ['a.py']}
        t.update(over)
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True, 'rank': 1,
                       'state': 'Active', 'stage': 'plan-approved', 'children': ['T-0001']},
            'T-0001': t}}

    def cases(self):
        return [
            (rows.CARD_SPEC, self.feature(), 'F-0001'),
            (rows.STARVED_SPEC, self.feature(stage='spec-draft'), 'F-0001'),
            (rows.STARVED_PLAN, self.feature(stage='spec-approved'), 'F-0001'),
            (rows.PLAN_CODE, self.task_index(), 'T-0001'),
            (rows.CONFLICT, self.task_index(state='Active', mergeable='CONFLICTING'), 'T-0001'),
            (rows.STALE, self.task_index(state='Active', evidence=['PR #5 CLOSED']), 'T-0001'),
        ]

    def of(self, idx, kind, iid, n, prod=None):
        out = rows.candidates(idx, prod or product(), [], attempts={iid: n})
        return [r for r in out if r.item_id == iid and r.kind in (kind, rows.STALEMATE)]

    def test_each_kind_launches_below_the_limit(self):
        for kind, idx, iid in self.cases():
            with self.subTest(kind=kind):
                got = self.of(idx, kind, iid, 2)
                self.assertEqual([(r.kind, r.launches) for r in got], [(kind, True)])

    def test_each_kind_is_one_adjudicate_row_at_the_limit(self):
        for kind, idx, iid in self.cases():
            with self.subTest(kind=kind):
                got = self.of(idx, kind, iid, 3)
                self.assertEqual([(r.kind, r.brief_kind, r.launches) for r in got],
                                 [(rows.STALEMATE, 'adjudicate', True)])
                self.assertIn(kind, got[0].reason)

    def test_each_kind_is_silent_above_the_limit(self):
        for kind, idx, iid in self.cases():
            with self.subTest(kind=kind):
                self.assertEqual(self.of(idx, kind, iid, 4), [])

    def test_the_limit_is_conventions_attempt_limit(self):
        p = product(conventions={'attempt_limit': 1})
        got = self.of(self.feature(), rows.CARD_SPEC, 'F-0001', 1, p)
        self.assertEqual([r.kind for r in got], [rows.STALEMATE])

    def test_the_adjudicate_row_keeps_its_branch_and_place(self):
        plain = self.of(self.feature(), rows.CARD_SPEC, 'F-0001', 0)[0]
        capped = self.of(self.feature(), rows.CARD_SPEC, 'F-0001', 3)[0]
        self.assertEqual((capped.branch, capped.tier, capped.feature_id),
                         (plain.branch, plain.tier, plain.feature_id))

    def test_a_waits_on_row_is_never_capped(self):
        idx = self.task_index(after=['T-0000'])
        idx['items']['T-0000'] = {'id': 'T-0000', 'type': 'task', 'parent': 'F-0001',
                                  'state': 'New', 'writes': ['z.py']}
        for n in (3, 4):
            with self.subTest(attempts=n):
                out = [r for r in rows.candidates(idx, product(), [], attempts={'T-0001': n})
                       if r.item_id == 'T-0001']
                self.assertEqual([(r.kind, r.action) for r in out],
                                 [(rows.PLAN_CODE, 'WAITS ON T-0000')])

    def test_a_closed_item_whose_evidence_went_quiet_stays_closed_and_unemitted(self):
        """`sticky`, end to end: the rule falls back to New, Closed is held, no row of any kind."""
        from asf.evidence import closing
        held = closing.sticky('Closed', closing.Closing('New', 'planned'))
        self.assertEqual(held.state, 'Closed')
        idx = self.task_index(state=held.state, evidence=[])
        idx['items']['F-0001']['state'] = 'Closed'
        for attempts in ({}, {'T-0001': 3}, {'T-0001': 4}):
            with self.subTest(attempts=attempts):
                out = rows.candidates(idx, product(), [], attempts=attempts)
                self.assertEqual([r for r in out if r.item_id in ('T-0001', 'F-0001')], [])


class AlreadyOnTrunkTests(unittest.TestCase):
    """F-0095 §3.2: a New Task the trunk already names is not launched into an empty branch."""

    SHA = 'abc123def4567'
    LANDED = {'T-0017': (SHA, 'feat(T-0017): the meter')}

    def index(self, state='New'):
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'plan-approved', 'decided': True,
                       'state': 'Active', 'children': ['T-0017']},
            'T-0017': {'id': 'T-0017', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                       'state': state, 'writes': ['a.py']}}}

    def rows_of(self, landed=None, state='New'):
        return rows.candidates(self.index(state), product(), [], landed_shas=landed)

    def test_the_row_is_on_trunk_and_launches_nothing(self):
        (row,) = self.rows_of(self.LANDED)
        self.assertEqual(row.action, 'ON TRUNK abc123def456')
        self.assertFalse(row.launches)
        self.assertEqual(row.waits_on, 'trunk')
        for part in ('abc123def456', 'feat(T-0017): the meter', 'New'):
            self.assertIn(part, row.reason)

    def test_with_no_fact_the_same_index_launches_it(self):
        (row,) = self.rows_of(None)
        self.assertEqual(row.action, rows.LAUNCH)

    def test_an_active_or_closed_task_named_by_the_trunk_mints_no_extra_row(self):
        for state in ('Active', 'Closed'):
            with self.subTest(state=state):
                self.assertEqual(self.rows_of(self.LANDED, state), [])

    def test_select_keeps_the_row_and_spends_no_slot(self):
        before = tiers.free_slots([], 2)
        out = rows.plan_rows(self.index(), product(), [], 2, landed_shas=self.LANDED)
        self.assertEqual([r.action for r in out], ['ON TRUNK abc123def456'])
        self.assertEqual(tiers.free_slots([], 2), before)


class AfterCountsTheTrunkTests(unittest.TestCase):
    """F-0095 §3.3: `after:` counts a predecessor the trunk names, in both gates."""

    def items(self, other_feature=False):
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'building 0/1',
                            'decided': True, 'state': 'Active', 'children': ['T-0002']},
                 'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'rank': 2,
                            'state': 'New', 'after': ['T-0001'], 'writes': ['b.py']},
                 'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                            'state': 'New'}}
        if other_feature:
            items['F-0001']['children'] = ['T-0001', 'T-0002']
            items['F-0002'] = {'id': 'F-0002', 'type': 'feature', 'stage': 'plan-approved',
                               'decided': True, 'state': 'Active', 'children': []}
            items['T-0001'].update(parent='F-0002', state='Closed')
            items['F-0001']['children'] = ['T-0002']
        return {'items': items}

    TRUNK = {'T-0001': ('a' * 12, 'feat(T-0001): x')}

    def action_of(self, item_id, landed, corrections=None, index=None):
        out = [r for r in rows.candidates(index or self.items(), product(), [], occupancy=occ(corrections=corrections),
                                          landed_shas=landed) if r.item_id == item_id]
        return [r.action for r in out]

    def test_a_predecessor_the_trunk_names_lets_the_successor_launch(self):
        self.assertEqual(self.action_of('T-0002', self.TRUNK), ['would launch'])

    def test_with_no_fact_it_still_waits(self):
        self.assertEqual(self.action_of('T-0002', None), ['WAITS ON T-0001'])

    def test_hold_unlanded_lets_correction_and_adjudicate_rows_through(self):
        for rounds in (1, 3):
            with self.subTest(rounds=rounds):
                corr = {'T-0002': {'kind': 'gate', 'text': 'FAIL: x', 'rounds': rounds}}
                self.assertEqual(self.action_of('T-0002', None, corr), ['WAITS ON T-0001'])
                got = self.action_of('T-0002', self.TRUNK, corr)
                self.assertIn('would launch', got)
                self.assertFalse([a for a in got if a.startswith('WAITS ON')])

    def test_an_after_naming_a_task_of_another_feature_no_longer_waits_for_ever(self):
        self.assertEqual(self.action_of('T-0002', None, index=self.items(other_feature=True)),
                         ['would launch'])

    def test_landed_ids_is_the_record_union_the_trunk(self):
        items = {'T-1': {'state': 'Closed'}, 'T-2': {'state': 'New'}, 'T-3': {'state': 'New'}}
        self.assertEqual(rows.landed_ids(items), {'T-1'})
        self.assertEqual(rows.landed_ids(items, {'T-2': ('s', 'x')}), {'T-1', 'T-2'})
        self.assertEqual(rows.landed_ids(items, {}), {'T-1'})


class ParkedCorrectionTests(unittest.TestCase):
    """F-0095 §3.5 (feeder third): a parked correction launches nothing, at any round count."""

    def corr(self, rounds):
        return {'B-0001': {'kind': 'empty', 'text': 'nothing to land', 'rounds': rounds, 'parked': True,
                           'reason': 'ended empty 2 times: asf unpark B-0001'}}

    def test_the_row_is_parked_and_neither_correct_nor_adjudicate(self):
        for rounds in (0, 1, 3):
            with self.subTest(rounds=rounds):
                out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                                     occupancy=occ(corrections=self.corr(rounds)))
                self.assertEqual(len(out), 1)
                row = out[0]
                self.assertTrue(row.action.startswith('PARKED '))
                self.assertFalse(row.launches)
                self.assertEqual(row.waits_on, 'operator')
                self.assertNotEqual(row.kind, rows.STALEMATE)
                self.assertIn('asf unpark', row.reason)

    def test_a_parked_task_behind_an_unlanded_after_keeps_its_parked_row(self):
        index = {'items': {
            'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'New'},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'state': 'New', 'after': ['T-0001']}}}
        corr = {'T-0002': dict(self.corr(0)['B-0001'])}
        out = rows.candidates(index, product(), [], occupancy=occ(corrections=corr))
        self.assertEqual([r.action.split(' ')[0] for r in out], ['PARKED'])


class CorrectionRowTest(unittest.TestCase):
    """B-0032: a held branch goes back to its session as a FIX → CORRECT row."""

    def corr(self, rounds, text='FAIL: test_x'):
        return {'B-0001': {'kind': 'gate', 'text': text, 'rounds': rounds, 'at': '2026-09-21T00:00:00Z'}}

    def test_a_correction_yields_a_correct_row_in_the_items_tier(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             occupancy=occ(corrections=self.corr(1)))
        self.assertEqual(kinds(out), [('FIX → CORRECT', 'B-0001')])
        self.assertEqual((out[0].brief_kind, out[0].tier, out[0].branch, out[0].correction),
                         ('correct', 0, 'fix/B-0001', 'FAIL: test_x'))
        self.assertTrue(out[0].launches)

    def test_three_rounds_is_the_adjudicate_row(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             occupancy=occ(corrections=self.corr(3)))
        self.assertEqual([(r.kind, r.brief_kind) for r in out],
                         [('STALEMATE → ADJUDICATE', 'adjudicate')])

    def test_the_adjudicate_row_carries_the_holds_own_text_into_its_brief(self):
        """The adjudicate brief says "read, in full: the hold's own text above" — a product,
        2026-09-26: three adjudicate sessions on a Task held on a red CI check, none of whose
        briefs held the hold's text at all (the row dropped it), so none saw what was red."""
        import importlib
        brief_build = importlib.import_module('asf.briefs.build')  # the package shadows the name
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             occupancy=occ(corrections=self.corr(3, 'PR #7 checks red: gate\n'
                                                                    'lint: 13 finding(s)')))
        self.assertEqual(out[0].kind, rows.STALEMATE)
        self.assertIn('lint: 13 finding(s)', out[0].correction)
        self.assertIn('lint: 13 finding(s)', brief_build.correction_text(out[0], 'adjudicate'))

    def test_b0128_a_settled_correction_waits_on_merge_not_another_adjudicate(self):
        corr = self.corr(3)
        corr['B-0001']['settled'] = True
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             occupancy=occ(corrections=corr))
        self.assertEqual([r.kind for r in out], [rows.FIX_CORRECT])
        self.assertFalse(out[0].launches)
        self.assertTrue(out[0].action.startswith(rows.WAITS_MERGE), out[0].action)

    def test_b0128_a_settled_correction_names_the_prs_the_ruling_waits_on(self):
        # the card's Want: `asf next` shows `WAITS ON merge: #773, #775`
        corr = self.corr(3)
        corr['B-0001'].update(settled=True, prs=['773', '775'])
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             occupancy=occ(corrections=corr))
        self.assertEqual(out[0].action, f'{rows.WAITS_MERGE}: #773, #775')

    def test_b0128_a_settled_correction_with_no_pr_named_is_the_bare_action(self):
        corr = self.corr(3)
        corr['B-0001'].update(settled=True, prs=[])
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             occupancy=occ(corrections=corr))
        self.assertEqual(out[0].action, rows.WAITS_MERGE)

    def test_b0058_a_blocked_item_gets_no_correct_or_adjudicate_row(self):
        # the controller blocked eleven held items on the Bug about their loop; the next tick
        # still launched an adjudicate session on one — correction rows skipped `blocked`
        items = s1_bugs('B-0001')
        items['items']['B-0001']['blocked'] = True
        for rounds in (1, 3):
            with self.subTest(rounds=rounds):
                out = rows.plan_rows(items, product(), [], 1, attempts={'B-0001': 1},
                                     occupancy=occ(corrections=self.corr(rounds)))
                # no session of any kind: only the Bug's own WAITS row, saying it is blocked
                self.assertEqual([(r.kind, r.launches, r.waits_on) for r in out],
                                 [(rows.BUG_FIX, False, 'blocked')])

    def test_a_busy_item_gets_no_correct_row(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [{'item': 'B-0001'}], 1,
                             occupancy=occ(corrections=self.corr(1)))
        self.assertEqual([(r.kind, r.launches, r.waits_on) for r in out],
                         [(rows.BUG_FIX, False, 'session')])


def undecided_features(*specs, decided=()):
    """Open Features, ``(id, rank)`` each; ``decided`` is True only for the ids named."""
    items = {}
    for fid, rank in specs:
        v = {'id': fid, 'type': 'feature', 'title': fid, 'state': 'New', 'stage': 'card',
             'decided': fid in decided, 'stage_since': '2026-01-01T09:00:00Z'}
        if rank is not None:
            v['rank'] = rank
        items[fid] = v
    return {'items': items}


class UndecidedRowsTests(unittest.TestCase):
    """F-0096 §3.1: the undecided card's ranked, non-launching row, and the one cap."""

    def setUp(self):
        self.index = undecided_features(('F-0001', 3), ('F-0002', 1), ('F-0003', None),
                                        ('F-0004', 2), ('F-0005', 5), ('F-0006', 4),
                                        decided=('F-0005',))

    def cand(self, index=None, p=None, **kw):
        return rows.candidates(index or self.index, p or product(), [], **kw)

    def decision(self, rs):
        return [r for r in rs if r.kind == rows.UNDECIDED]

    def test_one_card_spec_and_five_decision_rows(self):
        rs = self.cand()
        self.assertEqual(kinds([r for r in rs if r.kind == rows.CARD_SPEC]),
                         [('CARD → SPEC', 'F-0005')])
        dec = self.decision(rs)
        self.assertEqual(len(dec), 5)
        for r in dec:
            self.assertEqual(r.action, 'NEEDS DECISION')
            self.assertFalse(r.launches)
            self.assertEqual((r.waits_on, r.brief_kind), ('decision', 'spec'))
            self.assertRegex(r.reason, r'^undecided \d+[mhd] — CARD → SPEC waits for decided: true$')
        self.assertEqual(dec[0].branch, 'spec/F-0002')

    def test_the_shared_fixture_undecided_card_gets_only_its_decision_row(self):
        rs = [r for r in self.cand(fixture_index()) if r.item_id == 'F-0006']
        self.assertEqual(kinds(rs), [(rows.UNDECIDED, 'F-0006')])

    def test_ranked_with_the_unranked_one_last(self):
        self.assertEqual([r.item_id for r in self.decision(self.cand())],
                         ['F-0002', 'F-0004', 'F-0001', 'F-0006', 'F-0003'])

    def test_an_undecided_s1_is_tier_0_and_an_s2_tier_1(self):
        idx = undecided_features(('F-0001', 1))
        for bid, sev in (('B-0001', 'S1'), ('B-0002', 'S2'), ('B-0003', 'S3')):
            idx['items'][bid] = {'id': bid, 'type': 'bug', 'title': bid, 'severity': sev,
                                 'decided': False, 'state': 'New', 'parent': 'F-0001'}
        dec = self.decision(self.cand(idx))
        self.assertEqual([(r.item_id, r.tier) for r in dec],
                         [('B-0001', 0), ('B-0002', 1), ('F-0001', 2)])
        self.assertEqual((dec[0].brief_kind, dec[0].branch, dec[0].feature_id),
                         ('fix-bug', 'fix/B-0001', 'F-0001'))
        self.assertIn('BUG → FIX waits for decided: true', dec[0].reason)

    def test_a_blocked_undecided_feature_gets_no_row(self):
        self.index['items']['F-0002']['blocked'] = 'waiting on the vendor'
        self.assertNotIn('F-0002', [r.item_id for r in self.decision(self.cand())])

    def test_decided_closed_and_task_cards_are_absent(self):
        idx = self.index
        idx['items']['F-0001']['state'] = 'Closed'
        idx['items']['T-0001'] = {'id': 'T-0001', 'type': 'task', 'title': 't', 'state': 'New',
                                  'decided': False, 'parent': 'F-0002'}
        ids = [r.item_id for r in self.decision(self.cand(idx))]
        self.assertEqual(ids, ['F-0002', 'F-0004', 'F-0006', 'F-0003'])

    def test_a_card_a_session_holds_gets_no_row(self):
        rs = rows.candidates(self.index, product(), [{'item': 'F-0002'}])
        self.assertNotIn('F-0002', [r.item_id for r in self.decision(rs)])

    def test_the_one_cap_and_the_way_past_it(self):
        p = product(conventions={'decision_rows': 2})
        self.assertEqual(rows.decision_rows(p), 2)
        self.assertEqual([r.item_id for r in self.decision(self.cand(p=p))], ['F-0002', 'F-0004'])
        self.assertEqual(len(self.decision(self.cand(p=p, decision_limit=0))), 5)
        self.assertEqual(len(self.decision(rows.plan_rows(self.index, p, [], 4, decision_limit=0))), 5)
        self.assertEqual(rows.decision_rows(product()), rows.DECISION_ROWS)
        self.assertEqual(rows.decision_rows(None), 5)

    def test_a_bad_cap_falls_back_to_the_default(self):
        for bad in (-1, 'many', None, 2.5):
            self.assertEqual(rows.decision_rows(product(conventions={'decision_rows': bad})), 5)
        self.assertEqual(rows.decision_rows(product(conventions={'decision_rows': 0})), 0)

    def test_the_rows_spend_no_slot(self):
        inflight = [{'item': 'X-1'}]
        before = tiers.free_slots(inflight, 3)
        out = rows.plan_rows(self.index, product(), inflight, 3)
        self.assertEqual(len(self.decision(out)), 5)
        self.assertEqual(sum(1 for r in out if r.launches), 1)
        self.assertEqual(tiers.free_slots(inflight, 3), before)

    def test_an_unlanded_after_leaves_the_decision_row_alone(self):
        idx = copy.deepcopy(self.index)
        idx['items']['F-0002']['after'] = ['T-9999']
        r = [r for r in self.cand(idx) if r.item_id == 'F-0002'][0]
        self.assertEqual((r.kind, r.action, r.waits_on), (rows.UNDECIDED, 'NEEDS DECISION', 'decision'))


class TiersTest(unittest.TestCase):
    """F-0071 acceptance 1: the S1 lane."""

    # 2026-09-26: the S1 lane reserves the first seat, not the whole floor — two product waves
    # launched 1 row with 7 seats free (it replaced test_unheld_s1_blocks_every_feature)
    def test_a_seated_s1_takes_the_first_seat_and_the_features_fill_the_rest(self):
        out = rows.plan_rows(ten_features_and_an_s1(), product(), [], 7)
        launching = [r for r in out if r.launches]
        self.assertEqual(kinds(launching[:1]), [('BUG → FIX', 'B-0001')])
        self.assertEqual([r.item_id for r in launching[1:]],
                         ['F-0010', 'F-0009', 'F-0008', 'F-0007', 'F-0006', 'F-0005'])

    def test_an_s1_that_cannot_be_seated_holds_every_feature(self):
        busy = [{'item': 'X-1'}, {'item': 'X-2'}]
        out = rows.plan_rows(ten_features_and_an_s1(), product(), busy, 2)
        self.assertEqual(kinds(out), [('BUG → FIX', 'B-0001')])
        self.assertEqual(out[0].action, tiers.NO_SLOT)

    def test_two_s1_rows_and_one_seat_hold_the_features(self):
        idx = s1_bugs('B-0001', 'B-0002')
        idx['items'].update(ten_features_and_an_s1()['items'])
        idx['items']['B-0001'] = s1_bugs('B-0001')['items']['B-0001']
        out = rows.plan_rows(idx, product(), [], 1)
        self.assertEqual([r.item_id for r in out if r.launches], ['B-0001'])
        self.assertEqual([r.item_id for r in out], ['B-0001', 'B-0002'])

    def test_held_s1_frees_the_rest_of_capacity(self):
        out = rows.plan_rows(ten_features_and_an_s1(), product(), [S1_SESSION], 4)
        # rank order: F-0010 has rank 1; the held S1 is still named, as a WAITS row, no slot
        self.assertEqual([(r.item_id, r.waits_on) for r in out if not r.launches],
                         [('B-0001', 'session')])
        self.assertEqual(kinds(r for r in out if r.launches), [('CARD → SPEC', 'F-0010'), ('CARD → SPEC', 'F-0009'),
                                      ('CARD → SPEC', 'F-0008')])

    def test_s1_shows_even_without_a_free_slot(self):
        busy = [{'item': 'X-1'}, {'item': 'X-2'}]
        out = rows.plan_rows(ten_features_and_an_s1(), product(), busy, 2)
        self.assertEqual(kinds(out), [('BUG → FIX', 'B-0001')])
        self.assertEqual(out[0].action, tiers.NO_SLOT)
        self.assertFalse(out[0].launches)

    def test_s2_before_features_but_does_not_hold_them(self):
        idx = ten_features_and_an_s1()
        idx['items']['B-0001']['severity'] = 'S2'
        out = rows.plan_rows(idx, product(), [], 3)
        self.assertEqual([r.tier for r in out], [1, 2, 2])

    def test_waits_on_rows_cost_no_slot(self):
        out = rows.plan_rows(fixture_index(), product(), [S1_SESSION], 6)
        # B-0076: T-0003 no longer waits on an Active card nobody is writing; T-0002 still waits
        # on T-0001, which this very cut launches.
        self.assertEqual(sum(1 for r in out if r.launches), 5)
        # B-0067: a coder's branch is the code lane's prefix, the one harvest scans for code
        self.assertTrue(all(r.branch.startswith('worker/') for r in out if r.kind == 'PLAN → CODE'),
                        [r.branch for r in out])
        # finish before you start: the planned Features' Task rows take the slots before any
        # Feature's spec or plan does
        self.assertEqual(kinds(out)[:3], [('UNDECIDED → DECIDE', 'B-0004'), ('BUG → FIX', 'B-0001'),
                                          ('BUG → FIX', 'B-0002')])
        self.assertEqual([r.waits_on for r in out if r.item_id == 'B-0001'], ['session'])
        self.assertEqual(kinds(out)[3:], [('CONFLICT → REBASE', 'T-0007'),
                                          ('PLAN → CODE', 'T-0001'), ('PLAN → CODE', 'T-0002'),
                                          ('PLAN → CODE', 'T-0003'), ('STALE → CLOSE', 'T-0006')])
        self.assertEqual([r.action for r in out if r.item_id == 'T-0002'], ['WAITS ON T-0001'])


def finish_index(cards=4, build_stage='plan-approved', task_state='New'):
    """A ranked Epic of ``cards`` decided Feature cards (F-0001 first) and, ranked last, F-0099:
    planned, one Task (T-0099) ready to build — and an open S2 Bug."""
    items = {'E-0001': {'id': 'E-0001', 'type': 'epic', 'rank': 1, 'state': 'New'}}
    for n in range(1, cards + 1):
        items[f'F-{n:04d}'] = {'id': f'F-{n:04d}', 'type': 'feature', 'parent': 'E-0001',
                               'rank': n, 'stage': 'card', 'state': 'New', 'decided': True}
    items['F-0099'] = {'id': 'F-0099', 'type': 'feature', 'parent': 'E-0001', 'rank': 99,
                       'stage': build_stage, 'state': 'Active', 'decided': True,
                       'children': ['T-0099']}
    items['T-0099'] = {'id': 'T-0099', 'type': 'task', 'parent': 'F-0099', 'rank': 1,
                       'state': task_state, 'writes': ['a.py']}
    items['B-0001'] = {'id': 'B-0001', 'type': 'bug', 'parent': 'F-0001', 'severity': 'S2',
                       'state': 'New', 'decided': True}
    return {'items': items}


class FinishBeforeYouStart(unittest.TestCase):
    """asf-36, a product over 7 days: 36 Features plan-approved and idle, 13 plan-draft, 3
    building, 2 landed. A planned Feature's Task rows go before any unbuilt Feature's spec or
    plan, and new spec/plan sessions are capped while Tasks are ready to build."""

    def test_a_planned_features_task_ranks_above_a_higher_ranked_cards_spec(self):
        out = rows.candidates(finish_index(cards=1), product(), [])
        self.assertEqual(kinds(out), [('BUG → FIX', 'B-0001'), ('PLAN → CODE', 'T-0099'),
                                      ('CARD → SPEC', 'F-0001')])

    def test_the_task_takes_the_slot_the_spec_had(self):
        out = rows.plan_rows(finish_index(cards=1), product(), [], 2)
        self.assertEqual([k for k in kinds(out)], [('BUG → FIX', 'B-0001'),
                                                   ('PLAN → CODE', 'T-0099')])

    def test_a_building_features_correction_and_review_rank_above_a_plan(self):
        idx = finish_index(cards=1, build_stage='building 0/1', task_state='Active')
        idx['items']['F-0001']['stage'] = 'plan-draft'
        occupancy = {'corrections': {'T-0099': {'kind': 'gate', 'text': 'red', 'rounds': 1}}}
        out = rows.candidates(idx, product(), [], occupancy=occupancy)
        self.assertEqual(kinds(out)[1:], [('FIX → CORRECT', 'T-0099'),
                                          ('STARVED → PLAN', 'F-0001')])
        occupancy = {'review': {'T-0099': {'branch': 'worker/T-0099', 'round': 1}}}
        out = rows.candidates(idx, product(), [], occupancy=occupancy)
        self.assertEqual(kinds(out)[1:], [('PUSHED → REVIEW', 'T-0099'),
                                          ('STARVED → PLAN', 'F-0001')])

    def test_bug_fixes_keep_their_precedence(self):
        idx = finish_index(cards=2)
        idx['items']['B-0001']['severity'] = 'S1'
        out = rows.plan_rows(idx, product(), [], 10)
        # the S1 takes the first seat; with seats to spare tier 2 fills the rest (2026-09-26)
        self.assertEqual(kinds(out)[0], ('BUG → FIX', 'B-0001'))
        self.assertIn(('PLAN → CODE', 'T-0099'), kinds(out))
        idx['items']['B-0001']['severity'] = 'S2'
        out = rows.plan_rows(idx, product(), [], 10)
        self.assertEqual(kinds(out)[0], ('BUG → FIX', 'B-0001'))

    def test_new_specs_are_capped_while_tasks_wait_to_build(self):
        out = rows.plan_rows(finish_index(cards=4), product(), [], 10)
        by = {r.item_id: r for r in out}
        self.assertEqual([r.item_id for r in out if r.launches],
                         ['B-0001', 'T-0099', 'F-0001', 'F-0002'])
        for fid in ('F-0003', 'F-0004'):
            self.assertFalse(by[fid].launches)
            self.assertEqual(by[fid].waits_on, 'finish')
            self.assertEqual(by[fid].action,
                             'WAITS ON finish: 0 spec/plan in flight + 2 this wave, cap 2 '
                             '(feeder.max_specs_in_flight) while 1 planned Feature has Tasks '
                             'to build: F-0099')

    def test_a_feature_in_a_lane_experiment_is_never_held_by_the_cap(self):
        idx = finish_index(cards=4)
        idx['items']['F-0004']['ab_pair'] = 'p1'
        out = rows.plan_rows(idx, product(), [], 10)
        by = {r.item_id: r for r in out}
        self.assertTrue(by['F-0004'].launches)
        self.assertEqual(by['F-0003'].waits_on, 'finish')

    def test_a_feature_in_a_lane_experiment_is_not_starved_by_the_cut(self):
        # both arms of a pair sat behind every Task row and the capacity cut never reached
        # them — exempt from the cap, yet never started
        idx = finish_index(cards=4)
        idx['items']['F-0004']['ab_pair'] = 'p1'
        out = rows.plan_rows(idx, product(), [], 2)
        self.assertIn('F-0004', [r.item_id for r in out if r.launches], kinds(out))
        self.assertNotIn('F-0003', [r.item_id for r in out])

    def test_running_spec_and_plan_sessions_count_against_the_cap(self):
        running = [{'item': 'F-0050', 'kind': 'spec'}, {'item': 'T-0050', 'kind': 'coder'}]
        out = rows.plan_rows(finish_index(cards=3), product(), running, 10)
        self.assertEqual([r.item_id for r in out if r.kind == 'CARD → SPEC' and r.launches],
                         ['F-0001'])
        waits = [r for r in out if r.waits_on == 'finish']
        self.assertEqual([r.item_id for r in waits], ['F-0002', 'F-0003'])
        self.assertIn('1 spec/plan in flight + 1 this wave, cap 2', waits[0].action)
        running = [{'item': 'F-0050', 'kind': 'spec'}, {'item': 'F-0051', 'kind': 'plan'}]
        out = rows.plan_rows(finish_index(cards=3), product(), running, 10)
        self.assertEqual([r.item_id for r in out if r.kind == 'CARD → SPEC' and r.launches], [])

    def test_no_cap_without_a_planned_feature_ready_to_build(self):
        for idx in (finish_index(cards=4, task_state='Resolved'),
                    finish_index(cards=4, build_stage='plan-draft')):
            out = rows.plan_rows(idx, product(), [], 10)
            self.assertFalse([r for r in out if r.waits_on == 'finish'], kinds(out))
        # a Task that cannot start (it waits on its predecessor) is not ready to build either
        idx = finish_index(cards=4)
        idx['items']['T-0099']['after'] = ['T-0098']
        idx['items']['T-0098'] = {'id': 'T-0098', 'type': 'task', 'parent': 'F-0099',
                                  'state': 'Active', 'writes': ['b.py']}
        out = rows.plan_rows(idx, product(), [], 10)
        self.assertFalse([r for r in out if r.waits_on == 'finish'], kinds(out))

    def test_a_parked_task_is_not_ready_to_build(self):
        out = rows.plan_rows(finish_index(cards=4), product(), [], 10, held={'T-0099'})
        self.assertFalse([r for r in out if r.waits_on == 'finish'])

    def test_the_cap_is_a_convention(self):
        for cap, launched in ((0, []), (3, ['F-0001', 'F-0002', 'F-0003'])):
            p = product(conventions={'feeder': {'max_specs_in_flight': cap}})
            self.assertEqual(rows.max_specs_in_flight(p), cap)
            out = rows.plan_rows(finish_index(cards=4), p, [], 10)
            self.assertEqual([r.item_id for r in out if r.kind == 'CARD → SPEC' and r.launches],
                             launched)
        for bad in (-1, 'two', True, None):
            p = product(conventions={'feeder': {'max_specs_in_flight': bad}})
            self.assertEqual(rows.max_specs_in_flight(p), 2)
        self.assertEqual(rows.max_specs_in_flight(product()), 2)

    def test_a_refused_documents_correction_is_never_held_but_counts(self):
        idx = finish_index(cards=3)
        idx['items']['F-0003']['stage'] = 'spec-draft'
        occupancy = {'corrections': {'F-0003': {'kind': rows.LANDING_GATE, 'text': 'gate red',
                                                'rounds': 1, 'branch': 'spec/F-0003'}}}
        out = rows.plan_rows(idx, product(), [], 10, occupancy=occupancy)
        by = {r.item_id: r for r in out}
        self.assertTrue(by['F-0003'].launches)
        self.assertEqual(by['F-0003'].kind, 'STARVED → SPEC')
        self.assertTrue(by['F-0001'].launches)
        self.assertEqual(by['F-0002'].waits_on, 'finish')
        self.assertIn('0 spec/plan in flight + 2 this wave', by['F-0002'].action)

    def test_the_conventions_check_names_a_bad_cap(self):
        from asf import conventions
        self.assertEqual(conventions.validate_mapping({'feeder': {'max_specs_in_flight': 2}}), [])
        self.assertEqual([k for k, _ in conventions.validate_mapping(
            {'feeder': {'max_specs_in_flight': -1}})], ['feeder.max_specs_in_flight'])
        self.assertEqual([k for k, _ in conventions.validate_mapping({'feeder': 3})], ['feeder'])


class IncidentsTest(unittest.TestCase):
    """F-0071 acceptance 5, 6: the S1 clock."""

    def test_incidents(self):
        got = render.incidents(fixture_index(), [], NOW, product())
        self.assertEqual([tuple(i) for i in got], [
            ('B-0001', 'S1', '3h', 'no session', True),
            ('B-0004', 'S1', '1h', 'no session', False),
            ('B-0002', 'S2', '30m', 'no session', False),
        ])

    def test_held_s1_is_not_starved(self):
        got = render.incidents(fixture_index(), [S1_SESSION], NOW, product())
        self.assertEqual(tuple(got[0]), ('B-0001', 'S1', '3h', 'w1', False))

    def test_s1_hours_from_stage_limits(self):
        got = render.incidents(fixture_index(), [], NOW, product(stage_limits={'s1_hours': 4}))
        self.assertFalse(got[0].starved)


class TableTests(unittest.TestCase):
    def test_golden_s1_unheld(self):
        out = render.table(rows.plan_rows(fixture_index(), product(), [], 10))
        self.assertEqual(out, golden('next-s1-unheld.md'))

    def test_golden_s1_held(self):
        out = render.table(rows.plan_rows(fixture_index(), product(), [S1_SESSION], 10))
        self.assertEqual(out, golden('next-s1-held.md'))

    def test_empty(self):
        self.assertIn('nothing to start', render.table([]))

    def test_json(self):
        data = json.loads(render.rows_json(rows.plan_rows(fixture_index(), product(), [], 10)))
        self.assertEqual(data[0]['item_id'], 'B-0001')
        self.assertEqual(set(data[0]), {'tier', 'kind', 'item_id', 'feature_id', 'action',
                                        'brief_kind', 'branch', 'reason', 'waits_on',
                                        'correction', 'review_round', 'groom_date', 'groom_file',
                                        'answers_file', 'open_questions'})


class CliTest(unittest.TestCase):
    def run_next(self, argv, ledger=()):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            if ledger:
                os.makedirs(os.path.join(home, 'state', 'sample'))
                with open(os.path.join(home, 'state', 'sample', 'sessions.jsonl'), 'w') as f:
                    f.writelines(json.dumps(r) + '\n' for r in ledger)
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(f"product: sample\nbacklog_dir: {FIXTURES}\nconventions:\n"
                        "  branch_prefixes:\n    spec: spec\n    plan: plan\n    task: task\n")
            inflight = os.path.join(home, 'inflight.json')
            with open(inflight, 'w') as f:
                json.dump({'inflight': [S1_SESSION]}, f)
            p = argparse.ArgumentParser()
            register(p.add_subparsers(dest='command'))
            args = p.parse_args([a.replace('@INFLIGHT', inflight) for a in argv])
            old, env.ASF_HOME = env.ASF_HOME, home
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = args.func(args)
            finally:
                env.ASF_HOME = old
        return rc, buf.getvalue()

    def test_next_table(self):
        rc, out = self.run_next(['next', '--product', 'sample', '--capacity', '10', '--inflight', '@INFLIGHT'])
        self.assertEqual(rc, 0)
        self.assertEqual(out, golden('next-s1-held.md'))

    def test_next_json(self):
        rc, out = self.run_next(['next', '--product', 'sample', '--capacity', '10', '--json'])
        self.assertEqual(rc, 0)
        # the S1s first, then S2, then the tier-2 rows the seats left (2026-09-26)
        self.assertEqual([d['item_id'] for d in json.loads(out)][:3], ['B-0001', 'B-0004', 'B-0002'])
        self.assertIn('F-0001', [d['item_id'] for d in json.loads(out)])

    def test_next_holds_a_branch_awaiting_harvest_busy_as_the_tick_does(self):
        ledger = [{'job': 'fix-bug-b-0001', 'item': 'B-0001', 'branch': 'fix/B-0001', 'pid': 1,
                   'started': 't1'},
                  {'job': 'fix-bug-b-0001', 'ended': 't2', 'end_reason': 'finished'}]
        rc, out = self.run_next(['next', '--product', 'sample', '--capacity', '10', '--json'],
                                ledger=ledger)
        self.assertEqual(rc, 0)
        got = json.loads(out)
        ids = [d['item_id'] for d in got]
        # held busy: named as a WAITS row (never silent), never a launch
        self.assertEqual([d['action'] for d in got if d['item_id'] == 'B-0001'], ['WAITS ON landing'])
        self.assertEqual(ids[:3], ['B-0004', 'B-0001', 'B-0002'])


    def test_next_reads_the_live_sessions_off_the_ledger_as_the_tick_does(self):
        # without --inflight, `asf next` listed items whose coder was already live as "would launch"
        ledger = [{'job': 'fix-bug-b-0001', 'item': 'B-0001', 'kind': 'fix', 'branch': 'fix/B-0001',
                   'pid': 1, 'started': 't1'}]
        rc, out = self.run_next(['next', '--product', 'sample', '--capacity', '10', '--json'],
                                ledger=ledger)
        self.assertEqual(rc, 0)
        self.assertEqual([d['action'] for d in json.loads(out) if d['item_id'] == 'B-0001'],
                         ['WAITS ON session'])


class NextAllTests(unittest.TestCase):
    """F-0096 §2.4: the footer for what the cap hid, and ``asf next --all``."""
    FOOTER = '— 3 more cards await a decision (asf next --all)'

    def run_next(self, *flags):
        with tempfile.TemporaryDirectory() as home:
            backlog = os.path.join(home, 'backlog')
            os.makedirs(backlog)
            os.makedirs(os.path.join(home, 'products'))
            shutil.copy(os.path.join(FIXTURES, 'index-undecided.json'),
                        os.path.join(backlog, 'index.json'))
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(f"product: sample\nbacklog_dir: {backlog}\nconventions:\n"
                        "  branch_prefixes:\n    spec: spec\n    plan: plan\n    task: task\n")
            p = argparse.ArgumentParser()
            register(p.add_subparsers(dest='command'))
            args = p.parse_args(['next', '--product', 'sample', '--capacity', '10', *flags])
            old, env.ASF_HOME = env.ASF_HOME, home
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = args.func(args)
            finally:
                env.ASF_HOME = old
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_the_cap_shows_five_decision_rows_and_the_footer(self):
        out = self.run_next()
        self.assertEqual(out.count('NEEDS DECISION'), 5)
        self.assertIn('| 2 | UNDECIDED → DECIDE | F-0005 |', out)
        self.assertNotIn('F-0006', out)
        self.assertIn('**NEXT** — 5 rows · 0 would launch', out)
        self.assertEqual(out.splitlines()[-1], self.FOOTER)

    def test_all_prints_every_undecided_card_and_no_footer(self):
        out = self.run_next('--all')
        self.assertEqual(out.count('NEEDS DECISION'), 8)
        self.assertIn('| F-0008 |', out)
        self.assertNotIn('await a decision', out)
        self.assertIn('**NEXT** — 8 rows · 0 would launch', out)

    def test_json_carries_the_rows_the_table_showed_footer_or_not(self):
        for flags, ids in (((), ['F-000%d' % i for i in range(1, 6)]),
                           (('--all',), ['F-000%d' % i for i in range(1, 9)])):
            with self.subTest(flags=flags):
                data = json.loads(self.run_next('--json', *flags))
                self.assertEqual([d['item_id'] for d in data], ids)
                table = self.run_next(*flags)
                self.assertEqual(table.count('NEEDS DECISION'), len(data))

    def test_the_footer_is_absent_when_the_cap_hid_nothing(self):
        out = render.table(rows.plan_rows(fixture_index(), product(), [], 10), hidden=0)
        self.assertNotIn('await a decision', out)
        self.assertEqual(render.table([], hidden=2).splitlines()[-1],
                         '— 2 more cards await a decision (asf next --all)')


class EpicRankOrdersFeatures(unittest.TestCase):
    """Rank orders only within a parent, so the feeder orders Features by (Epic rank, Feature
    rank, id) — ranking an Epic first puts its whole subtree first; no Epic or no rank goes last.
    Tasks follow their Feature (Epic, Feature, Task rank); S1 tiers stay first."""

    def index(self):
        items = {
            'B-0001': {'id': 'B-0001', 'type': 'bug', 'title': 'Down', 'severity': 'S1',
                       'decided': True, 'state': 'New'},
            'E-0001': {'id': 'E-0001', 'type': 'epic', 'rank': 2, 'state': 'Active'},
            'E-0002': {'id': 'E-0002', 'type': 'epic', 'rank': 1, 'state': 'Active'},
            'E-0003': {'id': 'E-0003', 'type': 'epic', 'state': 'Active'},
        }
        feats = [('F-0001', 'E-0001', 1), ('F-0002', 'E-0002', 2), ('F-0003', 'E-0002', 1),
                 ('F-0004', None, 1), ('F-0005', 'E-0003', 1), ('F-0006', 'E-0001', None)]
        for fid, epic, rank in feats:
            f = {'id': fid, 'type': 'feature', 'decided': True, 'stage': 'card', 'state': 'New'}
            if epic:
                f['parent'] = epic
            if rank is not None:
                f['rank'] = rank
            items[fid] = f
        # F-0003 is building: its Tasks come in Task-rank order, inside its Feature's place
        items['F-0003'].update(stage='plan-approved', children=['T-0001', 'T-0002'])
        items['T-0001'] = {'id': 'T-0001', 'type': 'task', 'parent': 'F-0003', 'rank': 2,
                           'state': 'New', 'writes': ['a.py']}
        items['T-0002'] = {'id': 'T-0002', 'type': 'task', 'parent': 'F-0003', 'rank': 1,
                           'state': 'New', 'writes': ['b.py']}
        return {'items': items}

    ORDER = ['T-0002', 'T-0001', 'F-0002', 'F-0001', 'F-0006', 'F-0004', 'F-0005']

    def test_feature_rows_follow_the_epic_rank(self):
        out = rows.feature_rows(self.index()['items'], product(), set(), [])
        self.assertEqual([r.item_id for r in out], self.ORDER)

    def test_candidates_keep_s1_first_then_epic_order(self):
        out = [r.item_id for r in rows.candidates(self.index(), product(), [])]
        self.assertEqual(out[0], 'B-0001')
        self.assertEqual([i for i in out if i[0] in 'FT'], self.ORDER)


if __name__ == '__main__':
    unittest.main()


class ATaskWithNoWritesIsNotLaunched(unittest.TestCase):
    def test_no_writes_waits_instead_of_launching(self):
        from asf.feeder import rows as rows_mod
        items = {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active', 'stage': 'plan-approved',
                       'children': ['T-0001', 'T-0002']},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1, 'state': 'New'},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'rank': 2, 'state': 'New',
                       'writes': ['x.py']},
        }
        out = rows_mod.task_rows(items, None, items['F-0001'], set(), [])
        by_id = {r.item_id: r for r in out}
        self.assertEqual(by_id['T-0001'].action, 'WAITS ON writes')
        self.assertIn('no writes: declared', by_id['T-0001'].reason)
        self.assertEqual(by_id['T-0002'].action, rows_mod.LAUNCH)
