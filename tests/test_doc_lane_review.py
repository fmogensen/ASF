"""F-0280's three Stories, one class each: S-73204's `TheFeatureLaneRows` (`FEATURE_LANES`, the
branch-keyed gate in `lane_rows`, `lane_speaks`, and a Feature admitted to `pushed_ids`/
`pushed_rows` on the branch the lane recorded, never a type-derived guess); S-73206's
`TheReviewBriefOfADocumentBranch` (`reviews.EVIDENCE`, `render_table(rows, evidence=None)`, and
the review brief's `{checklist}`/`{review_reads}` — the table and the reading order chosen per
branch, not the code table alone)."""
import importlib
import unittest

from asf import reviews
from asf.briefs import preamble as preamble_mod
from asf.env import Product
from asf.feeder import rows, tiers
from asf.harvest import lane

# ``asf.briefs.build`` is both the package's entry function and a submodule; the function wins
# the attribute lookup, so the module is asked for by name (as ``tests/test_briefs.py`` does).
build_mod = importlib.import_module('asf.briefs.build')


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


def review_row(item_id, branch, feature_id=None, review_kinds=None):
    """A `PUSHED → REVIEW` row (:class:`rows.Row`) for ``item_id`` on ``branch``.
    ``review_kinds`` is `Row.review_kinds` (F-0280 S-73205, not yet a dataclass field — set here
    as a plain attribute, which `getattr(row, 'review_kinds', ())` reads the same way)."""
    r = rows.Row(tier=1, kind=rows.PUSHED_REVIEW, item_id=item_id,
                feature_id=item_id if feature_id is None else feature_id,
                action=f'{rows.LAUNCH} review', brief_kind='review', branch=branch, reason='',
                review_round=1)
    if review_kinds is not None:
        r.review_kinds = review_kinds
    return r


def review_ctx(row, idx):
    """`asf.briefs.build.context` for ``row`` off fixture ``idx`` — no product-level files, no
    inflight, no repo facts; the review template reads none of those."""
    facts = preamble_mod.collect(product(), row, idx, [], None)
    return build_mod.context(product(), row, 'review', facts)


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


class TheReviewBriefOfADocumentBranch(unittest.TestCase):
    """S-73206 — the review brief asks for the checklist of the branch in front of it: a Task's
    brief still asks the six code checks it asks today, a Feature's `spec/` branch of class
    `code` asks eleven, and the reading order names the document the branch itself carries."""

    def test_task_review_renders_six_rows_with_todays_hints(self):
        idx = index(feature(fid='F-0280'), task('T-9001', 'F-0280'))
        ctx = review_ctx(review_row('T-9001', 'task/T-9001', feature_id='F-0280'), idx)
        self.assertEqual(ctx['checklist'],
                         reviews.render_table(reviews.required('code'), reviews.EVIDENCE))
        for hint in ('the file, or the one outside it', 'the step → the code', 'file:line',
                     "the run's last line", "each command's last line", 'what you looked at'):
            self.assertIn(hint, ctx['checklist'])
        # six checks, header and separator included — no spec or plan check leaked in
        self.assertEqual(len(ctx['checklist'].splitlines()), 8)
        self.assertNotIn('one Task per Story', ctx['checklist'])

    def test_feature_code_class_spec_branch_renders_eleven_rows(self):
        idx = index(feature(fid='F-0280', stage='spec-review'))
        row = review_row('F-0280', 'spec/F-0280', review_kinds=('spec', 'code'))
        ctx = review_ctx(row, idx)
        lines = ctx['checklist'].splitlines()
        self.assertEqual(len(lines), 13)   # header, separator, 5 spec rows, 6 code rows
        data_rows = lines[2:]
        self.assertEqual(len(data_rows), 11)
        for line in data_rows:
            self.assertIn('<pass\\|fail>', line)
        expected_names = list(reviews.required('spec', 'code'))
        for name, line in zip(expected_names, data_rows):
            self.assertTrue(line.startswith(f'| {name} |'), line)
            self.assertIn(reviews.EVIDENCE[name], line)
        # the five spec checks first, then the six code checks
        self.assertTrue(all(n in reviews.required('spec') for n in expected_names[:5]))
        self.assertTrue(all(n in reviews.required('code') for n in expected_names[5:]))

    def test_review_reads_names_the_branchs_own_document(self):
        with self.subTest('spec'):
            idx = index(feature(fid='F-0280', stage='spec-review'))
            ctx = review_ctx(review_row('F-0280', 'spec/F-0280'), idx)
            self.assertIn('the spec `docs/specs/f-0280.md`', ctx['read_order'])
            self.assertNotIn('for the Task it claims to deliver', ctx['read_order'])
        with self.subTest('plan'):
            idx = index(feature(fid='F-0280', stage='plan-review'))
            ctx = review_ctx(review_row('F-0280', 'plan/F-0280'), idx)
            self.assertIn('the plan `docs/plans/f-0280.md`', ctx['read_order'])
            self.assertNotIn('docs/specs/f-0280.md', ctx['read_order'])
            self.assertNotIn('for the Task it claims to deliver', ctx['read_order'])
        with self.subTest('direct'):
            idx = index(feature(fid='F-0280', stage='card', lane='direct'))
            ctx = review_ctx(review_row('F-0280', 'cloud/direct-F-0280'), idx)
            self.assertIn('docs/specs/f-0280.md', ctx['read_order'])
            self.assertIn('docs/plans/f-0280.md', ctx['read_order'])
        with self.subTest('unknown prefix'):
            idx = index(feature(fid='F-0280', stage='card'))
            ctx = review_ctx(review_row('F-0280', 'mystery/F-0280'), idx)
            self.assertIn('for the Task it claims to deliver', ctx['read_order'])
        with self.subTest('task'):
            idx = index(feature(fid='F-0280'), task('T-9001', 'F-0280'))
            ctx = review_ctx(review_row('T-9001', 'task/T-9001', feature_id='F-0280'), idx)
            self.assertIn('for the Task it claims to deliver', ctx['read_order'])

    def test_render_table_with_no_evidence_is_unchanged(self):
        names = reviews.required('spec')
        expected = '\n'.join(['| check | result | evidence |', '| --- | --- | --- |']
                             + [f'| {n} | <pass\\|fail> | |' for n in names])
        self.assertEqual(reviews.render_table(names), expected)
        self.assertEqual(reviews.render_table(names), reviews.render_table(names, evidence=None))

    def test_evidence_covers_every_checklist_name(self):
        names = [n for kind in reviews.KINDS for tup in reviews.CHECKLIST[kind] for n in tup]
        self.assertEqual(len(names), 21)
        for name in names:
            self.assertIn(reviews.normalize(name), reviews.EVIDENCE)
        # a name with no entry renders an empty cell
        self.assertEqual(reviews.render_table(['a name nothing covers'], reviews.EVIDENCE),
                         '| check | result | evidence |\n| --- | --- | --- |\n'
                         '| a name nothing covers | <pass\\|fail> | |')

    def test_delivery_branch_brief_unchanged(self):
        idx = index(feature(fid='F-0280'),
                   task('T-9001', 'F-0280', delivers=['T-9002', 'T-9003']))
        row = review_row('T-9001', 'task/T-9001', feature_id='F-0280')
        ctx = review_ctx(row, idx)
        self.assertTrue(ctx['delivery_checks'])
        self.assertTrue(ctx['delivery_checks'].startswith('\n\n'))
        from asf import briefs
        brief = briefs.build(product(), row, idx, [], None)
        self.assertIn(ctx['checklist'] + ctx['delivery_checks'], brief.text)


if __name__ == '__main__':
    unittest.main()
