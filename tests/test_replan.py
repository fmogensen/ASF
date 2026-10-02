"""A Feature-level ``reshape:`` is re-planned: the feeder gives the Feature one RESHAPE → REPLAN
row and holds its code rows, the record applies the landed replan, and a park on one of its Tasks
lifts once the replan is recorded.

The fixture is shaped like the live case that went unserved for two days: a Feature in build
carrying a groom ``reshape:`` answer, its delivery lead parked (``incomplete`` twice), and the
lead's members depending on Tasks of an archived Feature — a dependency that can never land."""
import os
import shutil
import tempfile
import unittest

import importlib

from asf.env import Product
from asf.feeder import rows
from asf.record import frontmatter
from asf.record import replan

build_mod = importlib.import_module('asf.briefs.build')

HOW = ('re-plan Task 7 and the Tasks the lead delivers onto lib/plan.py; the 3 missing '
       'starters become separate follow-up Tasks, one per starter, not blocking')
PARK = 'delivery incomplete 2 times in a row with no new Task on the branch: `asf unpark T-0027`'


def product(**conv):
    base = {'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'},
            'delivery': 'feature'}
    base.update(conv)
    return Product('sample', {'conventions': base})


def f0090_index(**feature):
    """F-0090 building 1/5: T-0026 landed, T-0027 leads a delivery of T-0030/T-0032/T-0037, whose
    T-0030 and T-0037 wait on T-0020 — a Task of F-0088, which groom archived."""
    items = {
        'E-0014': {'id': 'E-0014', 'type': 'epic', 'rank': 1, 'state': 'Active'},
        'F-0088': {'id': 'F-0088', 'type': 'feature', 'parent': 'E-0014', 'state': 'New',
                   'removed': 'archived (groom)', 'decided': True},
        'T-0020': {'id': 'T-0020', 'type': 'task', 'parent': 'F-0088', 'state': 'New',
                   'removed': 'archived with F-0088'},
        'F-0090': dict({'id': 'F-0090', 'type': 'feature', 'parent': 'E-0014', 'rank': 20,
                        'state': 'Active', 'decided': True, 'stage': 'building 1/5',
                        'reshape': HOW, 'evidence': ['spec on origin/main', 'plan on origin/main'],
                        'children': ['T-0026', 'T-0027', 'T-0030', 'T-0032', 'T-0037']},
                       **feature),
        'T-0026': {'id': 'T-0026', 'type': 'task', 'parent': 'F-0090', 'state': 'Closed',
                   'writes': ['lib/batch.py']},
        'T-0027': {'id': 'T-0027', 'type': 'task', 'parent': 'F-0090', 'state': 'Active',
                   'decided': True, 'writes': ['lib/card.py'],
                   'delivers': ['T-0027', 'T-0030', 'T-0032', 'T-0037']},
    }
    for tid, after in (('T-0030', ['T-0020']), ('T-0032', []), ('T-0037', ['T-0020'])):
        items[tid] = {'id': tid, 'type': 'task', 'parent': 'F-0090', 'state': 'New',
                      'decided': True, 'writes': [f'lib/{tid.lower()}.py'],
                      'delivered_by': 'T-0027', 'after': after}
    return {'items': items}


#: the spec's landing-gate hold, adjudicated two days before and answered by the spec landing
SPEC_HOLD = {'kind': 'landing-gate', 'text': 'the spec turns the gate red on main', 'rounds': 3,
             'same': 3, 'settled': True, 'prs': ['847', '846'], 'branch': 'spec/team-staffing'}


def parked():
    return {'corrections': {'T-0027': {'kind': 'incomplete', 'text': 'T-0030 unbuilt',
                                       'rounds': 2, 'parked': True, 'reason': PARK,
                                       'branch': 'cloud/T-0027'},
                            'F-0090': dict(SPEC_HOLD)}}


def resume():
    return {'corrections': {'T-0027': {'kind': 'incomplete', 'text': 'T-0030 unbuilt',
                                       'rounds': 1, 'same': 1, 'branch': 'cloud/T-0027'}}}


def by_item(out):
    got = {}
    for r in out:
        got.setdefault(r.item_id, []).append(r)
    return got


class APendingFeatureReshapeIsReplanned(unittest.TestCase):

    def test_the_feature_gets_one_replan_row_carrying_the_reshape_text(self):
        out = rows.candidates(f0090_index(), product(), [], occupancy=parked())
        replans = [r for r in out if r.kind == rows.REPLAN]
        self.assertEqual(len(replans), 1)
        r = replans[0]
        self.assertEqual((r.item_id, r.feature_id, r.brief_kind, r.branch, r.action),
                         ('F-0090', 'F-0090', 'replan', 'plan/F-0090-replan', rows.LAUNCH))
        self.assertIn(HOW, r.reason)

    def test_a_spec_hold_whose_spec_landed_no_longer_speaks_for_the_feature(self):
        out = by_item(rows.candidates(f0090_index(), product(), [], occupancy=parked()))
        self.assertEqual([r.kind for r in out['F-0090']], [rows.REPLAN])

    def test_a_spec_hold_whose_spec_has_not_landed_still_holds(self):
        idx = f0090_index(evidence=['spec on spec/team-staffing'])
        out = by_item(rows.candidates(idx, product(), [], occupancy=parked()))
        self.assertEqual([r.action for r in out['F-0090']], ['WAITS ON merge: #847, #846'])

    def test_the_parked_lead_stays_parked_and_no_member_launches(self):
        out = by_item(rows.candidates(f0090_index(), product(), [], occupancy=parked()))
        self.assertEqual([r.action.split(' ')[0] for r in out['T-0027']], ['PARKED'])
        for tid in ('T-0030', 'T-0032', 'T-0037'):
            self.assertFalse(any(r.launches for r in out.get(tid, ())), (tid, out.get(tid)))

    def test_a_resumed_delivery_waits_on_the_replan_not_the_old_plan(self):
        out = by_item(rows.candidates(f0090_index(), product(), [], occupancy=resume()))
        lead = out['T-0027']
        self.assertFalse(any(r.launches for r in lead), lead)
        self.assertTrue(any(r.waits_on in ('replan', 'T-0020') for r in lead), lead)

    def test_the_code_rows_of_a_replanned_feature_wait_on_the_replan(self):
        idx = f0090_index()
        for tid in ('T-0027', 'T-0030', 'T-0032', 'T-0037'):   # no delivery: the Task lane
            for k in ('delivers', 'delivered_by', 'after'):
                idx['items'][tid].pop(k, None)
            idx['items'][tid]['state'] = 'New'
        out = by_item(rows.candidates(idx, product(delivery='task'), []))
        for tid in ('T-0027', 'T-0030', 'T-0032', 'T-0037'):
            (r,) = out[tid]
            self.assertEqual((r.action, r.waits_on), ('WAITS ON replan F-0090', 'replan'))
        self.assertTrue(out['F-0090'][0].launches)

    def test_a_reshape_the_record_applied_yields_no_row(self):
        idx = f0090_index(reshape_applied=replan.digest(HOW))
        out = rows.candidates(idx, product(), [])
        self.assertFalse([r for r in out if r.kind == rows.REPLAN])

    def test_a_newer_reshape_than_the_applied_one_is_pending_again(self):
        idx = f0090_index(reshape_applied=replan.digest('an older decision'))
        out = rows.candidates(idx, product(), [])
        self.assertEqual([r.item_id for r in out if r.kind == rows.REPLAN], ['F-0090'])

    def test_a_pushed_replan_waits_to_land(self):
        occ = {'branches': {'plan/F-0090-replan': 'PR #9 open'}}
        out = rows.candidates(f0090_index(), product(), [], occupancy=occ)
        (r,) = [r for r in out if r.item_id == 'F-0090']
        self.assertEqual(r.kind, rows.PUSHED_LAND)
        self.assertFalse(r.launches)

    def test_a_running_replan_session_is_the_features_one_session(self):
        out = rows.candidates(f0090_index(), product(), [{'item': 'F-0090', 'kind': 'replan'}])
        self.assertFalse([r for r in out if r.item_id == 'F-0090'])

    def test_a_feature_not_in_build_gets_no_replan_row(self):
        out = rows.candidates(f0090_index(stage='spec-draft'), product(), [])
        self.assertFalse([r for r in out if r.kind == rows.REPLAN])


def landing(task, pr, state='PUSHED'):
    """Occupancy shaped like :func:`asf.workers.lifecycle.occupancy`'s ``landing`` (a lane state
    other than REVIEW) for one Task's PR — the live case: F-0094's T-0048, PR #954 PUSHED."""
    return {'landing': {task: {'branch': 'cloud/' + task, 'state': state, 'pr': pr, 'why': ''}}}


def in_review(task, pr, round_=1):
    return {'review': {task: {'branch': 'cloud/' + task, 'round': round_, 'pr': pr, 'why': ''}}}


class AReplanWaitsOnAnInFlightPR(unittest.TestCase):
    """A Feature's reshape must not rewrite the plan under a Task whose PR is about to land."""

    def test_a_pushed_pr_holds_the_replan(self):
        out = by_item(rows.candidates(f0090_index(), product(), [], occupancy=landing('T-0027', 954)))
        (r,) = out['F-0090']
        self.assertEqual(r.kind, rows.REPLAN)
        self.assertFalse(r.launches)
        self.assertEqual(r.action, 'WAITS ON T-0027 #954')
        self.assertEqual(r.waits_on, 'T-0027')

    def test_a_pr_in_review_holds_the_replan_too(self):
        out = by_item(rows.candidates(f0090_index(), product(), [], occupancy=in_review('T-0027', 954)))
        (r,) = out['F-0090']
        self.assertFalse(r.launches)
        self.assertEqual(r.action, 'WAITS ON T-0027 #954')

    def test_several_in_flight_prs_are_all_named_on_one_line(self):
        idx = f0090_index()
        idx['items']['T-0030']['state'] = 'Active'
        occ = {'landing': {'T-0027': {'branch': 'cloud/T-0027', 'state': 'PUSHED', 'pr': 954},
                           'T-0030': {'branch': 'cloud/T-0030', 'state': 'GATE', 'pr': 960}}}
        out = by_item(rows.candidates(idx, product(), [], occupancy=occ))
        (r,) = out['F-0090']
        self.assertFalse(r.launches)
        self.assertEqual(r.action, 'WAITS ON T-0027 #954, T-0030 #960')

    def test_a_merged_pr_no_longer_holds_the_replan(self):
        # merged/closed PRs drop out of occupancy's review/landing (asf.workers.lifecycle);
        # none named here reads as nothing in flight
        out = by_item(rows.candidates(f0090_index(), product(), [], occupancy={}))
        (r,) = out['F-0090']
        self.assertTrue(r.launches, r)

    def test_a_closed_task_with_a_stale_landing_entry_does_not_hold_it(self):
        idx = f0090_index()
        idx['items']['T-0027']['state'] = 'Closed'
        out = by_item(rows.candidates(idx, product(), [], occupancy=landing('T-0027', 954)))
        (r,) = out['F-0090']
        self.assertTrue(r.launches, r)


class TheReplanRowRespectsTheCaps(unittest.TestCase):

    def ready_elsewhere(self, idx):
        """F-0091: planned, one free Task — a Feature with Tasks ready to build."""
        idx['items']['F-0091'] = {'id': 'F-0091', 'type': 'feature', 'parent': 'E-0014',
                                  'rank': 1, 'state': 'Active', 'decided': True,
                                  'stage': 'building 0/1', 'children': ['T-0091', 'T-0092']}
        idx['items']['T-0091'] = {'id': 'T-0091', 'type': 'task', 'parent': 'F-0091',
                                  'state': 'Active', 'writes': ['lib/other.py']}
        idx['items']['T-0092'] = {'id': 'T-0092', 'type': 'task', 'parent': 'F-0091',
                                  'state': 'New', 'writes': ['lib/other2.py']}
        return idx

    def test_max_specs_in_flight_holds_the_replan(self):
        idx = self.ready_elsewhere(f0090_index())
        prod = product(feeder={'max_specs_in_flight': 1})
        out = rows.plan_rows(idx, prod, [{'item': 'F-0077', 'kind': 'plan'}], 10)
        (r,) = [r for r in out if r.item_id == 'F-0090']
        self.assertEqual(r.waits_on, 'finish')
        self.assertIn('cap 1 (feeder.max_specs_in_flight)', r.action)

    def test_a_running_replan_counts_against_max_specs_in_flight(self):
        idx = self.ready_elsewhere(f0090_index())
        idx['items']['F-0003'] = {'id': 'F-0003', 'type': 'feature', 'parent': 'E-0014',
                                  'rank': 3, 'state': 'New', 'decided': True, 'stage': 'card'}
        prod = product(feeder={'max_specs_in_flight': 1})
        out = rows.plan_rows(idx, prod, [{'item': 'F-0090', 'kind': 'replan'}], 10)
        (r,) = [r for r in out if r.item_id == 'F-0003']
        self.assertEqual(r.waits_on, 'finish')

    def test_the_in_build_cap_holds_a_replan_of_a_feature_not_yet_in_build(self):
        idx = self.ready_elsewhere(f0090_index())
        items = idx['items']
        items['T-0026']['state'] = 'New'      # nothing of F-0090 started: not in build
        items['T-0027'].update(state='New')
        items['F-0090']['stage'] = 'plan-approved'
        prod = product(feeder={'max_features_in_build': 1})
        out = rows.plan_rows(idx, prod, [], 10)
        (r,) = [r for r in out if r.item_id == 'F-0090']
        self.assertEqual(r.waits_on, 'finish')
        self.assertIn('in build, cap 1', r.action)

    def test_a_replan_of_a_feature_in_build_is_not_held_by_the_in_build_cap(self):
        idx = self.ready_elsewhere(f0090_index())
        prod = product(feeder={'max_features_in_build': 1})
        out = rows.plan_rows(idx, prod, [], 10)
        (r,) = [r for r in out if r.item_id == 'F-0090']
        self.assertTrue(r.launches, r)


class TheReplanBrief(unittest.TestCase):

    def test_the_brief_carries_the_reshape_the_digest_the_path_and_the_tasks(self):
        idx = f0090_index()
        row = [r for r in rows.candidates(idx, product(), []) if r.kind == rows.REPLAN][0]
        text = build_mod.build(product(), row, idx, [], {}).text
        d = replan.digest(HOW)
        self.assertIn(HOW, text)
        self.assertIn(f'replan: F-0090 {d}', text)
        self.assertIn(replan.doc_path(product().conventions.plans_dir, 'F-0090', d), text)
        self.assertIn('- T-0026 [landed — keep]', text)
        self.assertIn('- T-0030 [New]', text)
        self.assertIn('after: T-0020; delivered by T-0027', text)
        self.assertEqual(build_mod.build(product(), row, idx, [], {}).kind, 'replan')


# ---- the record pass --------------------------------------------------------

FOLDER = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks'}


def write_card(root, meta):
    typed = [f'{k}: {frontmatter_value(v)}' for k, v in meta.items() if k != 'state']
    lines = typed + ['# ---- machine ----', f"state: {meta.get('state', 'New')}",
                     'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z']
    with open(os.path.join(root, FOLDER[meta['type']], f"{meta['id']}.md"), 'w',
              encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n## Description\n\n## History\n'
                '- 2026-09-01: created\n')


def frontmatter_value(v):
    if isinstance(v, list):
        return '[' + ', '.join(v) + ']'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str) and (':' in v or v.startswith(('[', '{'))):
        return '"' + v.replace('"', '\\"') + '"'
    return str(v)


def read_card(root, type_, id_):
    with open(os.path.join(root, FOLDER[type_], f'{id_}.md'), encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{id_}.md')[0]


class TheRecordAppliesALandedReplan(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='replan_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in ('epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules'):
            os.makedirs(os.path.join(self.root, f))
        for meta in f0090_index()['items'].values():
            meta = {k: v for k, v in meta.items() if k not in ('rank', 'stage', 'children')}
            write_card(self.root, meta)
        self.d = replan.digest(HOW)
        self.path = replan.doc_path(product().conventions.plans_dir, 'F-0090', self.d)
        self.doc = f"""# F-0090 replan

replan: F-0090 {self.d}

### Task T-0030: propose_team over lib/plan.py
stories: S-0010
writes: lib/plan.py, tests/test_plan.py
after: none

**Steps**: build it on the new surface

### Task T-0032: the transaction bodies
writes: lib/t-0032.py
after: T-0030

### Task T-0026: a landed Task named here changes nothing
writes: lib/other.py

### Task new: starter one
writes: lib/starters/one.py
after: none

### Task new: starter two
writes: lib/starters/two.py
after: new 1

### Drop T-0037: the close-out is the follow-up Tasks now
"""
        self.lines = []

    def apply(self, text=None):
        docs = {self.path: self.doc if text is None else text}
        return replan.apply_replans(self.root, product(), docs.get, out=self.lines.append)

    def test_the_replan_rewrites_adds_drops_and_records_it_applied(self):
        done = self.apply()
        self.assertEqual(list(done), ['F-0090'], self.lines)
        t30 = read_card(self.root, 'task', 'T-0030')
        self.assertEqual(t30['title'], 'propose_team over lib/plan.py')
        self.assertEqual(t30['writes'], ['lib/plan.py', 'tests/test_plan.py'])
        self.assertEqual(t30.get('after'), [])       # the archived Feature's Task is gone
        self.assertEqual(t30['links']['plan'], self.path)
        self.assertEqual(t30['delivered_by'], 'T-0027')   # its delivery is kept
        self.assertEqual(read_card(self.root, 'task', 'T-0032')['after'], ['T-0030'])
        self.assertTrue(read_card(self.root, 'task', 'T-0037')['removed'].startswith(
            'replan F-0090: the close-out'))
        t26 = read_card(self.root, 'task', 'T-0026')
        self.assertEqual(t26['writes'], ['lib/batch.py'])   # landed work is kept
        new = sorted(n[:-3] for n in os.listdir(os.path.join(self.root, 'tasks'))
                     if n[:-3] not in {'T-0020', 'T-0026', 'T-0027', 'T-0030', 'T-0032', 'T-0037'})
        self.assertEqual(len(new), 2, new)
        one, two = (read_card(self.root, 'task', n) for n in new)
        self.assertEqual((one['parent'], one['writes'], one.get('after')),
                         ('F-0090', ['lib/starters/one.py'], None))
        self.assertEqual(two['after'], [new[0]])
        f = read_card(self.root, 'feature', 'F-0090')
        self.assertEqual(f['reshape_applied'], self.d)
        self.assertTrue(f['reshape_applied_at'])
        self.assertEqual(replan.pending(dict(f, type='feature')), '')

    def test_a_rewritten_task_keeps_the_paths_the_factory_widened_it_onto(self):
        # a product's T-0500 (2026-10-02): widened onto three files outside its plan, then its
        # Feature's replan landed and rewrote writes: without them — the next correct session
        # reported `needs writes` for the very same files and the Task went to a reshape
        path = os.path.join(self.root, 'tasks', 'T-0030.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        text = text.replace('writes: [lib/t-0030.py]',
                            'writes: [lib/t-0030.py, lib/widened.py, tests/test_plan.py]')
        text = text.replace('- 2026-09-01: created\n', (
            '- 2026-09-01: created\n'
            '- 2026-09-02 10:00 footprint widened: +lib/widened.py tests/test_plan.py '
            'lib/reverted.py (report: needs writes)\n'
            '- 2026-09-02 11:00 footprint widening reverted: overlaps T-0032\n'))
        self.assertIn('lib/widened.py', text)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        self.apply()
        t30 = read_card(self.root, 'task', 'T-0030')
        # the replan's own paths first, the kept widening after; a widened path the replan lists
        # is not doubled, a reverted one (no longer on writes:) is not brought back, and the plan
        # path the replan dropped (lib/t-0030.py) stays dropped
        self.assertEqual(t30['writes'], ['lib/plan.py', 'tests/test_plan.py', 'lib/widened.py'])
        self.assertIn('T-0030: keeps its widened lib/widened.py', ' '.join(self.lines))
        # a Task never widened is rewritten exactly as the replan says
        self.assertEqual(read_card(self.root, 'task', 'T-0032')['writes'], ['lib/t-0032.py'])

    def test_kept_widenings_reads_the_cards_history_and_its_current_writes(self):
        rec = {'meta': {'writes': ['a.py', 'b.py', 'c.py']},
               'text': '- x footprint widened: +b.py c.py d.py (report: needs writes)\n'}
        self.assertEqual(replan.kept_widenings(rec, ['a.py', 'c.py']), ['b.py'])
        self.assertEqual(replan.kept_widenings({'meta': {'writes': ['a.py']}, 'text': ''},
                                               ['z.py']), [])

    def test_a_second_pass_is_a_no_op(self):
        self.apply()
        self.assertEqual(self.apply(), {})

    def test_no_replan_on_the_trunk_changes_nothing(self):
        self.assertEqual(replan.apply_replans(self.root, product(), lambda p: None,
                                              out=self.lines.append), {})
        self.assertNotIn('reshape_applied', read_card(self.root, 'feature', 'F-0090'))

    def test_a_replan_of_another_decision_is_not_applied(self):
        self.assertEqual(self.apply(self.doc.replace(self.d, 'abcdef012345')), {})
        self.assertIn('names another decision', ' '.join(self.lines))

    def test_after_the_replan_the_feeder_launches_the_delivery_and_drops_the_hold(self):
        self.apply()
        idx = f0090_index(reshape_applied=self.d)
        idx['items']['T-0030']['after'] = []
        idx['items']['T-0037']['after'] = []
        out = by_item(rows.candidates(idx, product(), []))
        self.assertFalse([r for r in rows.candidates(idx, product(), []) if r.kind == rows.REPLAN])
        self.assertTrue(any(r.launches and r.kind == rows.DELIVERY_CODE for r in out['T-0027']),
                        out['T-0027'])


class ParseAndPending(unittest.TestCase):

    def test_pending_is_the_digest_of_the_reshape_until_applied(self):
        f = {'id': 'F-0001', 'type': 'feature', 'reshape': HOW}
        self.assertEqual(replan.pending(f), replan.digest(HOW))
        self.assertEqual(replan.pending(dict(f, reshape_applied=replan.digest(HOW))), '')
        self.assertEqual(replan.pending({'id': 'T-0001', 'type': 'task', 'reshape': HOW}), '')

    def test_rewrapping_the_same_words_is_the_same_decision(self):
        self.assertEqual(replan.digest('a  b\nc'), replan.digest('a b c'))

    def test_replanned_since_reads_the_feature_above(self):
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature',
                            'reshape_applied_at': '2026-09-29T15:00:00Z'},
                 'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001'}}
        self.assertTrue(replan.replanned_since(items, 'T-0001', '2026-09-29T14:00:00Z'))
        self.assertFalse(replan.replanned_since(items, 'T-0001', '2026-09-29T16:00:00Z'))
        self.assertFalse(replan.replanned_since({'T-0001': items['T-0001']}, 'T-0001', ''))


if __name__ == '__main__':
    unittest.main()
