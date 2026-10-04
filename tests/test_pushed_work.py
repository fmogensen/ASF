"""Pushed work is never silent: an open Task/Bug with an open PR always has a NEXT row.

asf 2026-10-04: 22 ``worker/T-*`` PRs sat open for up to 8 days with no row in ``asf next``.
Their PUSHED → REVIEW rows were candidates, but the cut to capacity dropped every launching row
past the free seats without a trace, the seats went to new coders of nearer Features first, and
a tier-2 WAITS row vanished whenever no seat was free. Three rules close it: a row on pushed
work ranks before new work (finish before you start), it is always emitted — past the seats as
``WAITS ON a free slot`` — and a pushed item no other row speaks for gets a PUSHED → LAND WAITS
row. :func:`asf.feeder.rows.orphaned_pushed` counts the rest, and ``asf doctor`` says it."""
import unittest

from asf.env import Product
from asf.feeder import rows, tiers


def product():
    return Product('sample', {'conventions': {}})


def index():
    """F-0001 (rank 1) building, its T-0001 ready to code; F-0002 (rank 2) building, its T-0002
    pushed with PR #7 open."""
    items = {'E-0001': {'id': 'E-0001', 'type': 'epic', 'rank': 1, 'state': 'Active'}}
    for n in (1, 2):
        items[f'F-000{n}'] = {'id': f'F-000{n}', 'type': 'feature', 'parent': 'E-0001',
                              'rank': n, 'stage': 'building 0/1', 'state': 'Active',
                              'decided': True, 'children': [f'T-000{n}']}
    items['T-0001'] = {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                       'state': 'New', 'decided': True, 'writes': ['a.py']}
    items['T-0002'] = {'id': 'T-0002', 'type': 'task', 'parent': 'F-0002', 'rank': 1,
                       'state': 'Active', 'decided': True, 'writes': ['b.py'],
                       'evidence': ['PR #7 OPEN', 'branch worker/T-0002']}
    return {'items': items}


def in_review():
    return {'review': {'T-0002': {'branch': 'worker/T-0002', 'round': 1, 'pr': 7}},
            'waiting_landing': {'T-0002': 'lane REVIEW PR #7'}}


def by_item(out):
    return {r.item_id: r for r in out}


class PushedWorkRanksFirst(unittest.TestCase):

    def test_a_review_takes_the_seat_before_a_new_coder_of_a_nearer_feature(self):
        out = rows.plan_rows(index(), product(), [], 1, occupancy=in_review())
        launched = [(r.kind, r.item_id) for r in out if r.launches]
        self.assertEqual(launched, [(rows.PUSHED_REVIEW, 'T-0002')])


class PushedWorkIsAlwaysShown(unittest.TestCase):

    def test_a_review_past_the_seats_waits_on_a_free_slot(self):
        busy = [{'item': 'X-1', 'kind': 'task'}]
        out = by_item(rows.plan_rows(index(), product(), busy, 1, occupancy=in_review()))
        self.assertIn('T-0002', out)
        self.assertFalse(out['T-0002'].launches)
        self.assertEqual(out['T-0002'].action, tiers.NO_SLOT)

    def test_a_waiting_correction_shows_with_no_seat_free(self):
        idx = index()
        idx['items']['T-0002']['after'] = ['T-0001']
        occ = {'corrections': {'T-0002': {'kind': 'gate', 'text': 'red', 'rounds': 1,
                                          'branch': 'worker/T-0002'}}}
        busy = [{'item': 'X-1', 'kind': 'task'}]
        out = by_item(rows.plan_rows(idx, product(), busy, 1, occupancy=occ))
        self.assertIn('T-0002', out)
        self.assertFalse(out['T-0002'].launches)

    def test_finished_and_awaiting_harvest_is_a_landing_row(self):
        occ = {'waiting_landing': {'T-0002': 'finished, awaiting harvest'}}
        out = by_item(rows.plan_rows(index(), product(), [], 4, occupancy=occ))
        self.assertEqual(out['T-0002'].kind, rows.PUSHED_LAND)
        self.assertFalse(out['T-0002'].launches)
        self.assertIn('finished, awaiting harvest', out['T-0002'].action)

    def test_an_open_pr_no_run_holds_is_a_landing_row(self):
        out = by_item(rows.plan_rows(index(), product(), [], 4, occupancy={}))
        self.assertEqual(out['T-0002'].kind, rows.PUSHED_LAND)
        self.assertIn('PR #7', out['T-0002'].action)

    def test_a_blocked_item_in_review_waits_on_its_blocker(self):
        idx = index()
        idx['items']['T-0002']['blockedBy'] = ['T-0001']
        out = by_item(rows.plan_rows(idx, product(), [], 4, occupancy=in_review()))
        self.assertEqual(out['T-0002'].action, 'WAITS ON T-0001')


class OrphanedPushedCount(unittest.TestCase):

    def test_none_once_every_pushed_item_has_its_row(self):
        for occ in (in_review(), {}, {'waiting_landing': {'T-0002': 'finished'}}):
            out = rows.plan_rows(index(), product(), [{'item': 'X', 'kind': 't'}], 1,
                                 occupancy=occ)
            self.assertEqual(rows.orphaned_pushed(index(), out, occ, []), [])

    def test_a_pushed_item_with_no_row_is_counted(self):
        self.assertEqual(rows.orphaned_pushed(index(), [], in_review(), []), ['T-0002'])

    def test_a_live_session_on_it_is_not_an_orphan(self):
        live = [{'item': 'T-0002', 'kind': 'review'}]
        self.assertEqual(rows.orphaned_pushed(index(), [], in_review(), live), [])

    def test_a_done_card_is_not_pushed_work(self):
        idx = index()
        idx['items']['T-0002']['state'] = 'Resolved'
        self.assertEqual(rows.orphaned_pushed(idx, [], in_review(), []), [])


class DoctorSaysIt(unittest.TestCase):

    def test_the_doctor_line_is_red_with_the_ids(self):
        from asf import doctor
        ok, detail = doctor.pushed_work_line(['T-0002', 'T-0009'])
        self.assertFalse(ok)
        self.assertIn('2', detail)
        self.assertIn('T-0002', detail)

    def test_the_doctor_line_is_green_at_zero(self):
        from asf import doctor
        ok, detail = doctor.pushed_work_line([])
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
