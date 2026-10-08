"""Work the product put aside, and the build cap's count (2026-10-03).

* ``priority: later`` on a Feature (or an operator park on an item): its new work waits, every
  row waiting on it says why (``WAITS ON T-x (parked: F-x later)``), those rows rank behind the
  live work, and ``asf parity`` shows its Stories as later — not parity work in progress;
* the build cap counts a Feature as moving only for a live session or a launching row — a Task
  whose branch only waits to land (CI, review, the merge queue) holds no start;
* the status line says "no new Feature starts" only while the cap holds a row.
"""
import json
import os
import tempfile
import unittest

from asf.env import Product
from asf.feeder import rows
from asf.views import parity

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from occfixture import occ
except ImportError:  # pragma: no cover - import shape only
    from tests.occfixture import occ


def product(**feeder):
    conv = {'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}
    if feeder:
        conv['feeder'] = feeder
    return Product('sample', {'conventions': conv})


def index(later=False):
    """E-0001 > F-0101 (building, T-01010 landed, T-01011 New) and F-0102 (building, T-01020
    landed, T-01021 New after T-01011) — F-0101 ``priority: later`` when ``later``."""
    items = {'E-0001': {'id': 'E-0001', 'type': 'epic', 'rank': 1, 'state': 'New'}}
    for n, fid in enumerate(('F-0101', 'F-0102')):
        kids = [f'T-{fid[2:]}0', f'T-{fid[2:]}1']
        items[fid] = {'id': fid, 'type': 'feature', 'parent': 'E-0001', 'rank': 10 + n,
                      'stage': 'building', 'state': 'Active', 'decided': True, 'children': kids}
        for k, tid in enumerate(kids):
            items[tid] = {'id': tid, 'type': 'task', 'parent': fid, 'rank': k + 1,
                          'state': 'Resolved' if k == 0 else 'New', 'writes': [f'{tid}.py']}
    items['T-01021']['after'] = ['T-01011']
    if later:
        items['F-0101']['priority'] = 'later'
    return {'items': items}


class Shelved(unittest.TestCase):

    def test_later_names_the_nearest_later_card_and_a_park_names_itself(self):
        items = rows.items_of(index(later=True))
        why = rows.shelved(items, [{'item': 'T-01021', 'scope': 'item'},
                                   {'item': 'T-01020', 'scope': 'branch'}])
        self.assertEqual(why['T-01011'], 'F-0101 later')
        self.assertEqual(why['F-0101'], 'F-0101 later')
        self.assertEqual(why['T-01021'], 'operator park')
        self.assertNotIn('T-01010', why)            # Resolved: not open work
        self.assertNotIn('F-0102', why)

    def test_a_later_features_new_work_waits_and_its_dependants_say_why(self):
        out = rows.plan_rows(index(later=True), product(), [], 20)
        by = {r.item_id: r for r in out}
        self.assertFalse(by['T-01011'].launches)
        self.assertEqual(by['T-01011'].action, 'WAITS ON later: F-0101 later')
        self.assertEqual(by['T-01011'].waits_on, 'later')
        # the edge stays: T-01021 still waits on T-01011, and says it will not move
        self.assertEqual(by['T-01021'].action, 'WAITS ON T-01011 (parked: F-0101 later)')
        self.assertEqual(by['T-01021'].waits_on, 'T-01011')

    def test_without_later_the_same_work_launches(self):
        by = {r.item_id: r for r in rows.plan_rows(index(), product(), [], 20)}
        self.assertTrue(by['T-01011'].launches)
        self.assertEqual(by['T-01021'].action, 'WAITS ON T-01011')

    def test_put_aside_rows_rank_behind_the_live_rows(self):
        idx = index(later=True)
        idx['items']['E-0001']['rank'] = 1
        idx['items']['F-0101']['rank'] = 1          # the later Feature outranks F-0102 by rank
        idx['items']['T-01021'].pop('after')
        out = rows.candidates(idx, product(), [])
        ids = [r.item_id for r in out if r.tier == 2]
        self.assertLess(ids.index('T-01021'), ids.index('T-01011'), ids)

    def test_a_delivery_member_waiting_on_a_parked_lead_says_parked(self):
        r = rows.Row(tier=2, kind=rows.DELIVERY_CODE, item_id='T-0002', feature_id='F-0001',
                     action='WAITS ON delivery T-0001', brief_kind='task', branch='',
                     reason='delivered by T-0001', waits_on='delivery')
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active'},
                 'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'Active'},
                 'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'state': 'New'}}
        out, aside = rows.hold_shelved([r], items, [{'item': 'T-0001', 'scope': 'item'}])
        self.assertEqual(out[0].action, 'WAITS ON delivery T-0001 (parked: operator park)')
        self.assertEqual(aside, {id(out[0])})
        # nothing put aside: the rows pass untouched
        self.assertEqual(rows.hold_shelved([r], items, []), ([r], set()))

    def test_a_review_of_pushed_work_under_a_later_feature_still_finishes(self):
        r = rows.Row(tier=2, kind=rows.PUSHED_REVIEW, item_id='T-01011', feature_id='F-0101',
                     action=rows.LAUNCH, brief_kind='review', branch='task/T-01011',
                     reason='pushed')
        out, _aside = rows.hold_shelved([r], rows.items_of(index(later=True)))
        self.assertTrue(out[0].launches)


class ParkedHolderHoldsNothing(unittest.TestCase):
    """B-82960: an Active Task under a ``priority: later`` Feature holds no footprint and orders
    no live Task — an ``after:`` to it from a non-later Feature's Task is read as satisfied."""

    def parked(self, later=True):
        idx = index(later=later)
        its = idx['items']
        its['T-01011'].update(state='Active', writes=['lib/x.py'])
        its['T-01021'].update(writes=['lib/x.py'])
        return idx

    def test_a_need_task_after_a_parked_active_holder_launches(self):
        by = {r.item_id: r for r in rows.plan_rows(self.parked(), product(), [], 20)}
        self.assertTrue(by['T-01021'].launches, by['T-01021'])
        self.assertIn('T-01011 parked: F-0101 later', by['T-01021'].reason)

    def test_the_parked_holder_holds_no_footprint_even_while_busy(self):
        items = rows.items_of(self.parked())
        self.assertEqual(rows.running_footprints(items, {'T-01011'}), [])
        live = rows.items_of(self.parked(later=False))
        self.assertEqual(rows.running_footprints(live, {'T-01011'}), [('T-01011', ['lib/x.py'])])

    def test_raised_again_the_after_order_is_back(self):
        # the edge is never rewritten: once the Feature is raised, the order holds again
        by = {r.item_id: r for r in rows.plan_rows(self.parked(later=False), product(), [], 20)}
        self.assertEqual(by['T-01021'].action, 'WAITS ON T-01011')

    def test_a_later_task_keeps_its_order_on_another_later_task(self):
        idx = self.parked()
        idx['items']['F-0102']['priority'] = 'later'
        item = rows.items_of(idx)['T-01021']
        self.assertEqual(rows.after_of(rows.items_of(idx), item), ['T-01011'])


class ParityLater(unittest.TestCase):

    def render(self, later):
        idx = index(later=later)
        for n, fid in enumerate(('F-0101', 'F-0102')):
            idx['items'][f'S-{n}'] = {'id': f'S-{n}', 'type': 'story', 'parent': fid,
                                      'state': 'Active', 'area': '3.1 Area',
                                      'title': f'Story {n}'}
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'index.json'), 'w', encoding='utf-8') as f:
                json.dump(dict(idx, generated='2026-10-03T08:00:00Z'), f)
            return parity.render(d, full=True)

    def test_a_later_features_stories_are_later_not_doing(self):
        out = self.render(later=True)
        self.assertIn('1 doing, 0 todo, 1 later (priority: later', out)
        self.assertIn('across 1 Features still landing', out)
        self.assertIn('| 3.1 Area | 0 | 1 | 0 | 1 | 2 |', out)
        lines = [ln for ln in out.splitlines() if ln.startswith('| ') and ' S-' in ln]
        self.assertIn('S-1', lines[0])                         # the live row first
        self.assertIn('— F-0101 later', lines[1])
        self.assertIn('| later |', lines[1])

    def test_without_later_both_are_doing(self):
        out = self.render(later=False)
        self.assertIn('2 doing, 0 todo, across 2 Features', out)


def build_index(started=3, unstarted=1):
    items = {'E-0001': {'id': 'E-0001', 'type': 'epic', 'rank': 1, 'state': 'New'},
             'F-0001': {'id': 'F-0001', 'type': 'feature', 'parent': 'E-0001', 'rank': 1,
                        'stage': 'card', 'state': 'New', 'decided': True}}

    def feature(fid, rank, stage, done):
        kids = [f'T-{fid[2:]}{k}' for k in range(2)]
        items[fid] = {'id': fid, 'type': 'feature', 'parent': 'E-0001', 'rank': rank,
                      'stage': stage, 'state': 'Active', 'decided': True, 'children': kids}
        for k, tid in enumerate(kids):
            items[tid] = {'id': tid, 'type': 'task', 'parent': fid, 'rank': k + 1,
                          'state': 'Resolved' if k < done else 'New', 'writes': [f'{tid}.py']}
    for n in range(started):
        feature(f'F-{101 + n:04d}', 10 + n, 'building', 1)
    for n in range(unstarted):
        feature(f'F-{201 + n:04d}', 50 + n, 'plan-approved', 0)
    return {'items': items}


class BuildCapCount(unittest.TestCase):
    """2026-10-03: "Features in build 7 / 4 — no new Feature starts" with 0 sessions running —
    4 of the 7 only had a branch waiting to land, and the view's count read other inputs than
    the cap's."""

    def test_a_task_only_waiting_to_land_does_not_hold_a_start(self):
        o = occ(busy=['T-01011'])                       # F-0101's open Task: a PR waiting
        p = product(max_features_in_build=3)
        x, n, _why, binds = rows.build_state(build_index(), p, 20, [], o)
        self.assertEqual((x, n, binds), (2, 3, False))
        by = {r.item_id: r for r in rows.plan_rows(build_index(), p, [], 20, occupancy=o)}
        self.assertTrue(by['F-0001'].launches, by['F-0001'].action)

    def test_a_live_session_still_counts(self):
        o = occ(live=['T-01011'])
        p = product(max_features_in_build=3)
        x, n, _why, binds = rows.build_state(build_index(), p, 20, [], o)
        self.assertEqual((x, n, binds), (3, 3, True))
        by = {r.item_id: r for r in rows.plan_rows(build_index(), p, [], 20, occupancy=o)}
        self.assertEqual(by['F-0001'].waits_on, 'finish')

    def test_the_line_says_no_new_feature_starts_only_while_a_row_is_held(self):
        p = product(max_features_in_build=2)
        x, n, why, binds = rows.build_state(build_index(), p, 20)
        self.assertTrue(binds)
        self.assertEqual(rows.build_load_line(x, n, why, binds),
                         'Features in build 3 / 2 (feeder.max_features_in_build)'
                         ' — no new Feature starts')
        # over the cap, but no Feature in build has a launching Task row: the cap holds nothing
        idx = build_index()
        for fid in ('F-0101', 'F-0102', 'F-0103'):
            idx['items'][f'T-{fid[2:]}1']['blockedBy'] = ['F-0001']
        x, n, why, binds = rows.build_state(idx, p, 20, [{'item': 'T-01011', 'kind': 'coder'},
                                                         {'item': 'T-01021', 'kind': 'coder'}])
        self.assertEqual((x, binds), (2, False))
        self.assertEqual(rows.build_binds_note(x, n, binds), ' — the cap holds no row now')
        self.assertEqual(rows.build_binds_note(1, 2, True), '')
        self.assertEqual(rows.build_load(build_index(), p, 20)[:2], (3, 2))


if __name__ == '__main__':
    unittest.main()
