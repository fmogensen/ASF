"""F-0280 S-73204 — the feeder speaks for a Feature the lane holds: `FEATURE_LANES`, the
branch-keyed gate in `lane_rows`, `lane_speaks`, and a Feature admitted to `pushed_ids`/
`pushed_rows` on the branch the lane recorded, never a type-derived guess."""
import unittest

from asf.env import Product
from asf.feeder import rows, tiers
from asf.harvest import lane


def product(**conv):
    # the fixtures carry no Story: the stories-first gate (tests/test_stories_first.py) is off
    return Product('sample', {'conventions': dict({'feeder': {'stories_before_plan': False}},
                                                  **conv), 'main': 'main'})


def feature(fid='F-0280', stage='card', **extra):
    return dict({'id': fid, 'type': 'feature', 'stage': stage, 'state': 'Active',
                 'decided': True, 'title': 'a feature'}, **extra)


def task(tid, fid, **extra):
    return dict({'id': tid, 'type': 'task', 'parent': fid, 'rank': 1, 'state': 'New',
                 'decided': True, 'writes': [f'{tid.lower()}.py']}, **extra)


def index(*cards):
    return {'items': {c['id']: c for c in cards}}


def kinds(rs):
    return [(r.kind, r.item_id) for r in rs]


def by_item(rs):
    return {r.item_id: r for r in rs}


def doc_occ(fid, branch, kind, state=lane.REVIEW, round_=1, pr=941, reason=''):
    """An occupancy for one Feature's document branch, in the shape
    ``asf.workers.lifecycle.occupancy`` builds for it (docs/plans/f-0280.md PD4): a REVIEW or
    other open-state record populates ``review``/``landing``, ``lanes``, ``waiting_landing``,
    ``branches`` and ``docs`` together in one pass — a fixture that sets only ``review``/
    ``landing`` cannot show the duplicate row :func:`rows.lane_speaks` removes."""
    pr_part = f" PR #{pr}" if pr else ''
    why = f"lane {state}{pr_part}: {reason}".rstrip(': ')
    out = {'busy': {}, 'waiting_landing': {fid: why}, 'corrections': {},
           'lanes': {branch: {'item': fid, 'kind': kind, 'state': state, 'pr': pr,
                               'reason': reason}},
           'review': {}, 'landing': {}, 'branches': {branch: why}, 'docs': {fid: {kind: why}},
           'landed': {}, 'landed_on': {}, 'parks': {}, 'back': {}}
    if state == lane.REVIEW:
        out['review'][fid] = {'branch': branch, 'round': round_, 'pr': pr, 'why': reason}
    else:
        out['landing'][fid] = {'branch': branch, 'state': state, 'pr': pr, 'why': why,
                                'heavy': None, 'head': None}
    return out


class TheFeatureLaneRows(unittest.TestCase):

    def test_a_feature_in_review_draws_one_pushed_review_row_and_no_land_row(self):
        occ = doc_occ('F-0280', 'spec/F-0280', 'spec', state=lane.REVIEW, pr=941)
        out = rows.candidates(index(feature(stage='spec-draft')), product(), [], occupancy=occ)
        self.assertEqual(kinds(out), [(rows.PUSHED_REVIEW, 'F-0280')])
        r = out[0]
        self.assertTrue(r.launches)
        self.assertEqual(r.brief_kind, 'review')
        self.assertEqual(r.branch, 'spec/F-0280')
        self.assertEqual(r.review_round, 1)

    def test_the_same_holds_on_the_plan_branch_and_the_direct_branch_is_unchanged(self):
        with self.subTest('plan'):
            occ = doc_occ('F-0280', 'plan/F-0280', 'plan', state=lane.REVIEW, pr=897)
            out = rows.candidates(index(feature(stage='plan-draft')), product(), [],
                                  occupancy=occ)
            self.assertEqual(kinds(out), [(rows.PUSHED_REVIEW, 'F-0280')])
            self.assertEqual(out[0].branch, 'plan/F-0280')
        with self.subTest('direct'):
            occ = doc_occ('F-0280', 'cloud/direct-F-0280', 'direct', state=lane.REVIEW, pr=1)
            out = rows.candidates(index(feature(stage='card', lane='direct')), product(), [],
                                  occupancy=occ)
            self.assertEqual(kinds(out), [(rows.PUSHED_REVIEW, 'F-0280')])
            self.assertEqual(out[0].branch, 'cloud/direct-F-0280')

    def test_a_feature_in_any_other_open_lane_state_draws_one_pushed_land_row(self):
        for state in (lane.GATE, lane.WAITING, lane.WAITING_CI, lane.MERGING):
            with self.subTest(state=state):
                occ = doc_occ('F-0280', 'spec/F-0280', 'spec', state=state)
                out = rows.candidates(index(feature(stage='spec-draft')), product(), [],
                                      occupancy=occ)
                self.assertEqual(kinds(out), [(rows.PUSHED_LAND, 'F-0280')])
                self.assertFalse(out[0].launches)
                self.assertIn(state, out[0].action)

    def test_a_building_feature_keeps_its_task_rows_and_loses_only_its_landing_row(self):
        occ = doc_occ('F-0280', 'plan/F-0280-replan', 'plan', state=lane.GATE)
        idx = index(feature(stage='building 1/2', reshape='cut the scope',
                            children=['T-9001']),
                    task('T-9001', 'F-0280'))
        out = rows.candidates(idx, product(), [], occupancy=occ)
        self.assertEqual(kinds([r for r in out if r.item_id == 'F-0280']),
                         [(rows.PUSHED_LAND, 'F-0280')])
        self.assertIn('T-9001', by_item(out))

    def test_a_feature_on_an_unknown_branch_prefix_draws_no_row(self):
        occ = doc_occ('F-0280', 'mystery/F-0280', 'spec', state=lane.REVIEW)
        out = rows.lane_rows(index(feature(stage='spec-draft'))['items'], product(), [], occ)
        self.assertEqual(out, [])

    def test_pushed_ids_and_orphaned_pushed_see_the_feature(self):
        occ = doc_occ('F-0280', 'spec/F-0280', 'spec', state=lane.REVIEW)
        idx = index(feature(stage='spec-draft'))
        self.assertIn('F-0280', rows.pushed_ids(idx['items'], occ))
        self.assertEqual(rows.orphaned_pushed(idx, [], occ, []), ['F-0280'])
        out = rows.candidates(idx, product(), [], occupancy=occ)
        self.assertEqual(rows.orphaned_pushed(idx, out, occ, []), [])

    def test_plan_rows_at_zero_free_seats_still_emits_the_row(self):
        occ = doc_occ('F-0280', 'spec/F-0280', 'spec', state=lane.REVIEW)
        idx = index(feature(stage='spec-draft'))
        busy = [{'item': 'X-1', 'kind': 'task'}]
        out = by_item(rows.plan_rows(idx, product(), busy, 1, occupancy=occ))
        self.assertIn('F-0280', out)
        self.assertFalse(out['F-0280'].launches)
        self.assertEqual(out['F-0280'].action, tiers.NO_SLOT)

    def test_pushed_rows_names_the_occupancy_branch_not_the_fix_lane(self):
        occ = doc_occ('F-0280', 'spec/F-0280', 'spec', state=lane.REVIEW)
        items = index(feature(stage='spec-draft'))['items']
        pushed = rows.pushed_ids(items, occ)
        out = rows.pushed_rows(items, product(), pushed, occ, set())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].branch, 'spec/F-0280')
        self.assertNotEqual(out[0].branch, 'fix/F-0280')

    def test_a_feature_no_occupancy_entry_names_gets_no_pushed_rows_row(self):
        occ = {'corrections': {'F-0280': {'kind': 'gate'}}}
        items = index(feature(stage='spec-draft'))['items']
        # a correction names no branch, so the pushed set does not owe the Feature a row
        # `pushed_rows` would have to refuse: its FIX → CORRECT row is what speaks for it
        pushed = rows.pushed_ids(items, occ)
        self.assertNotIn('F-0280', pushed)
        self.assertEqual(rows.pushed_rows(items, product(), pushed, occ, set()), [])
        # and the refusal holds on its own, for a caller that names the id anyway
        self.assertEqual(rows.pushed_rows(items, product(), {'F-0280'}, occ, set()), [])

    def test_direct_lane_rows_are_unmoved(self):
        # the direct branch was already in the gate before this card (FEATURE_LANES keeps
        # `DIRECT` beside `spec`/`plan`): its REVIEW row is covered above, and here its row at
        # any other open lane state is the same PUSHED → LAND `lane_rows` draws for a document
        # branch — unmoved by generalising the gate from "is it direct" to "is it a feature lane".
        for state in (lane.GATE, lane.WAITING, lane.WAITING_CI, lane.MERGING):
            with self.subTest(state=state):
                occ = doc_occ('F-0280', 'cloud/direct-F-0280', 'direct', state=state)
                out = rows.candidates(index(feature(stage='card', lane='direct')), product(), [],
                                      occupancy=occ)
                self.assertEqual(kinds(out), [(rows.PUSHED_LAND, 'F-0280')])
                self.assertFalse(out[0].launches)
                self.assertIn(state, out[0].action)
        # and `tests.test_direct_lane` — the module that pins the direct lane's rows end to
        # end — stays green; it is not re-run here, only named as what "unmoved" means (its own
        # suite is part of this Task's Gate).


if __name__ == '__main__':
    unittest.main()
