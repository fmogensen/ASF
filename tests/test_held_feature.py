"""A Feature held by its own session always has a row that says so, however many corrections
are pending on it (F-0274, T-129157). ``held_feature_row`` no longer guesses from three
occupancy keys (``review``, ``landing``, ``corrections``) that can never be a row on this path —
it reads ``spoken``, the ``item_id`` of every row :func:`asf.feeder.rows.candidates` already
drew, threaded through ``feature_rows`` and ``_one_feature_rows``. A correction on the Feature
itself can never speak for it here: ``correction_rows`` drops every correction whose item is
busy, and the held path is only ever reached because the Feature is busy — so the row it used to
defer to had already stepped aside, and the Feature vanished from ``asf next`` with every one of
its open Tasks."""
import unittest

from asf.feeder import rows
from tests import test_replan

#: F-0090's four open Tasks once the delivery is cut out (PD4), in the order they held the row
HELD_TASKS = ('T-0027', 'T-0030', 'T-0032', 'T-0037')

#: the live session shape tests/test_replan.py:158 uses — a replan session running on F-0090
LIVE_SESSION = {'busy': {'F-0090': 'session replan-f-0090 running'}}


def held_index(**feature):
    """tests.test_replan.f0090_index with the delivery cut out: ``T-0027`` no longer leads a
    delivery of ``T-0030``/``T-0032``/``T-0037``, so each of the four open Tasks owns its own
    row and the Feature's silence (or held row) is total rather than partial (PD4)."""
    idx = test_replan.f0090_index(**feature)
    items = idx['items']
    items['T-0027'] = dict(items['T-0027'])
    items['T-0027'].pop('delivers', None)
    items['T-0027']['state'] = 'New'
    for tid in ('T-0030', 'T-0032', 'T-0037'):
        items[tid] = dict(items[tid])
        items[tid].pop('delivered_by', None)
        items[tid].pop('after', None)
    return idx


def correction(text='held', kind='incomplete', branch='cloud/F-0090'):
    return {'kind': kind, 'text': text, 'rounds': 1, 'branch': branch}


def by_item(out):
    got = {}
    for r in out:
        got.setdefault(r.item_id, []).append(r)
    return got


class AFeatureHeldByItsOwnSessionAlwaysHasARowThatSaysSo(unittest.TestCase):

    def test_a_live_session_and_a_pending_correction_draw_one_held_row_naming_every_open_task(self):
        occ = dict(LIVE_SESSION, corrections={'F-0090': correction()})
        out = by_item(rows.candidates(held_index(), test_replan.product(), [], occupancy=occ))
        for tid in HELD_TASKS:
            self.assertNotIn(tid, out)
        got = out.get('F-0090', [])
        self.assertEqual([r.kind for r in got], [rows.FEATURE_HELD])
        self.assertFalse(got[0].launches)
        self.assertEqual(got[0].action, 'WAITS ON replan-f-0090 (replan)')
        for tid in HELD_TASKS:
            self.assertIn(tid, got[0].reason)

    def test_a_landed_doc_correction_the_lane_has_already_answered_still_draws_the_held_row(self):
        occ = dict(LIVE_SESSION, corrections={'F-0090': dict(test_replan.SPEC_HOLD)})
        out = by_item(rows.candidates(held_index(), test_replan.product(), [], occupancy=occ))
        self.assertEqual([r.kind for r in out.get('F-0090', [])], [rows.FEATURE_HELD])

    def test_a_correction_on_a_task_leaves_the_held_row_standing_beside_its_fix_correct_row(self):
        occ = dict(LIVE_SESSION, corrections={'T-0027': correction(branch='cloud/T-0027')})
        out = by_item(rows.candidates(held_index(), test_replan.product(), [], occupancy=occ))
        self.assertEqual([r.kind for r in out['T-0027']], [rows.FIX_CORRECT])
        held = out.get('F-0090', [])
        self.assertEqual([r.kind for r in held], [rows.FEATURE_HELD])
        self.assertIn('T-0027', held[0].reason)

    def test_the_deleted_occupancy_guard_cannot_come_back_unnoticed(self):
        idx = held_index()
        f = idx['items']['F-0090']
        occ = {'corrections': {'F-0090': correction()}}
        self.assertIsNone(rows.held_feature_row(idx['items'], f, occ, spoken={'F-0090'}))
        row = rows.held_feature_row(idx['items'], f, occ, spoken=())
        self.assertEqual(row.kind, rows.FEATURE_HELD)

    def test_the_two_landed_cases_hold_steady_one_row_naming_open_tasks_and_no_row_when_none_are_open(self):
        out = by_item(rows.candidates(held_index(), test_replan.product(), [],
                                      occupancy=LIVE_SESSION))
        got = out.get('F-0090', [])
        self.assertEqual([r.kind for r in got], [rows.FEATURE_HELD])
        for tid in HELD_TASKS:
            self.assertIn(tid, got[0].reason)
        idx = held_index()
        for tid in HELD_TASKS:
            idx['items'][tid]['state'] = 'Closed'
        out = by_item(rows.candidates(idx, test_replan.product(), [], occupancy=LIVE_SESSION))
        self.assertNotIn('F-0090', out)

    def test_a_review_or_landing_occupancy_draws_pushed_land_not_a_held_row(self):
        occ = dict(LIVE_SESSION, waiting_landing={'F-0090': True},
                   branches={'plan/F-0090-replan': 'PR #9 open'})
        out = by_item(rows.candidates(held_index(), test_replan.product(), [], occupancy=occ))
        got = out.get('F-0090', [])
        self.assertEqual([r.kind for r in got], [rows.PUSHED_LAND])
        self.assertFalse(any(r.kind == rows.FEATURE_HELD for r in got))

    def test_the_cards_transition_as_three_draws_never_leaves_a_waiting_task_invisible(self):
        product = test_replan.product()
        idx = held_index()
        out = by_item(rows.candidates(idx, product, []))
        self.assertEqual([r.kind for r in out['F-0090']], [rows.REPLAN])
        for tid in HELD_TASKS:
            self.assertTrue(out[tid], tid)

        out = by_item(rows.candidates(idx, product, [], occupancy=LIVE_SESSION))
        for tid in HELD_TASKS:
            self.assertNotIn(tid, out)
        self.assertEqual([r.kind for r in out['F-0090']], [rows.FEATURE_HELD])

        occ = dict(LIVE_SESSION, corrections={'F-0090': correction()})
        out = by_item(rows.candidates(idx, product, [], occupancy=occ))
        self.assertEqual([r.kind for r in out['F-0090']], [rows.FEATURE_HELD])
        for tid in HELD_TASKS:
            self.assertNotIn(tid, out)

    def test_the_needs_stories_exemption_keeps_drawing_task_rows_beside_no_held_row(self):
        idx = held_index(reshape='')
        product = test_replan.product(feeder={'stories_before_plan': True})
        occ = dict(LIVE_SESSION, corrections={'F-0090': correction()})
        out = by_item(rows.candidates(idx, product, [], occupancy=occ))
        self.assertNotIn('F-0090', out)
        for tid in HELD_TASKS:
            (r,) = out[tid]
            self.assertEqual(r.kind, rows.PLAN_CODE)
            self.assertTrue(r.launches)

    def test_all_active_tasks_keep_their_own_rows_beside_the_held_row(self):
        idx = held_index()
        for tid in HELD_TASKS:
            idx['items'][tid]['state'] = 'Active'
        occ = dict(LIVE_SESSION, corrections={'F-0090': correction()})
        out = by_item(rows.candidates(idx, test_replan.product(), [], occupancy=occ))
        for tid in HELD_TASKS:
            self.assertEqual([r.kind for r in out[tid]], [rows.PLAN_CODE])
        self.assertEqual([r.kind for r in out['F-0090']], [rows.FEATURE_HELD])
