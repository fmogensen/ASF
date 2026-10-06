"""Stories first (``feeder.stories_before_plan``, default on): a Feature with no Story is not
planned — no plan, replan or delivery-plan row — and its next step is a ``NO STORIES →
SPEC-AMEND`` row whose brief derives the Stories from the spec, the plan and the landed Tasks.
A Feature already in build keeps its build rows; with the key off, today's rows hold."""

import importlib
import unittest

from asf.env import Product
from asf.feeder import rows

try:
    from occfixture import occ
except ImportError:  # pragma: no cover - import shape only
    from tests.occfixture import occ

build_mod = importlib.import_module('asf.briefs.build')

STORY = {'id': 'S-0001', 'type': 'story', 'parent': 'F-0001', 'state': 'New',
         'title': 'the export writes one row per order'}


def product(**feeder):
    conv = {'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}
    if feeder:
        conv['feeder'] = feeder
    return Product('fixture', {'conventions': conv})


def index(stage, stories=False, tasks=(), **feature):
    f = dict({'id': 'F-0001', 'type': 'feature', 'title': 'Order export', 'decided': True,
              'rank': 1, 'stage': stage, 'state': 'Active',
              'evidence': ['spec on origin/main'], 'children': []}, **feature)
    items = {'F-0001': f}
    if stories:
        items['S-0001'] = dict(STORY)
        f['children'].append('S-0001')
    for tid, state in tasks:
        items[tid] = {'id': tid, 'type': 'task', 'parent': 'F-0001', 'state': state,
                      'decided': True, 'title': f'{tid} work', 'writes': [f'lib/{tid}.py']}
        f['children'].append(tid)
    return {'items': items}


def f_rows(idx, prod=None, inflight=(), occupancy=None):
    return [r for r in rows.candidates(idx, prod or product(), list(inflight),
                                       occupancy=occupancy)
            if r.feature_id == 'F-0001']


def kinds(rs):
    return [r.kind for r in rs]


class AStorylessFeatureIsNotPlanned(unittest.TestCase):
    def test_spec_approved_gets_spec_amend_and_no_plan_row(self):
        rs = f_rows(index('spec-approved'))
        self.assertEqual(kinds(rs), [rows.NO_STORIES])
        r = rs[0]
        self.assertTrue(r.launches)
        self.assertEqual((r.brief_kind, r.branch, r.item_id), ('spec-amend', 'spec/F-0001',
                                                               'F-0001'))
        self.assertIn('feeder.stories_before_plan', r.reason)

    def test_plan_draft_and_review_get_no_plan_row(self):
        for stage in ('plan-draft', 'plan-review r1'):
            with self.subTest(stage=stage):
                self.assertEqual(kinds(f_rows(index(stage))), [rows.NO_STORIES])

    def test_a_pending_replan_waits_for_the_stories(self):
        rs = f_rows(index('building 0/1', tasks=[('T-0001', 'New')],
                          reshape='re-cut the export', evidence=['spec on origin/main',
                                                                  'plan on origin/main']))
        self.assertNotIn(rows.REPLAN, kinds(rs))
        self.assertIn(rows.NO_STORIES, kinds(rs))

    def test_a_delivery_plan_waits_for_the_stories(self):
        idx = index('card', delivers=['F-0001', 'B-0001'])
        idx['items']['B-0001'] = {'id': 'B-0001', 'type': 'bug', 'state': 'New',
                                  'parent': 'F-0001', 'decided': True}
        rs = [r for r in rows.candidates(idx, product(), []) if r.item_id == 'F-0001']
        self.assertNotIn(rows.DELIVERY_PLAN, [r.kind for r in rs if r.launches])
        self.assertIn(rows.NO_STORIES, kinds(rs))

    def test_the_row_is_capped_like_the_other_doc_rows(self):
        for group in (rows.CAPPED_KINDS, rows.NEW_DOC_KINDS, rows.SPEC_PLAN_KINDS):
            self.assertIn(rows.NO_STORIES, group)

    def test_a_card_still_gets_its_spec_the_spec_session_mints_the_stories(self):
        self.assertEqual(kinds(f_rows(index('card'))), [rows.CARD_SPEC])


class OnceStoriesExistThePlanAppears(unittest.TestCase):
    def test_spec_approved_with_a_story_gets_the_plan_row(self):
        self.assertEqual(kinds(f_rows(index('spec-approved', stories=True))),
                         [rows.STARVED_PLAN])

    def test_a_removed_story_is_no_story(self):
        idx = index('spec-approved', stories=True)
        idx['items']['S-0001']['removed'] = 'archived'
        self.assertEqual(kinds(f_rows(idx)), [rows.NO_STORIES])


class AMidBuildFeatureIsNotBlocked(unittest.TestCase):
    def idx(self):
        return index('building 1/3', tasks=[('T-0001', 'Closed'), ('T-0002', 'New'),
                                            ('T-0003', 'New')],
                     evidence=['spec on origin/main', 'plan on origin/main'])

    def test_the_spec_amend_row_sits_next_to_the_build_rows(self):
        rs = f_rows(self.idx())
        self.assertIn(rows.NO_STORIES, kinds(rs))
        coders = [r.item_id for r in rs if r.kind == rows.PLAN_CODE and r.launches]
        self.assertEqual(coders, ['T-0002', 'T-0003'])

    def test_its_running_spec_amend_does_not_hold_the_tasks(self):
        running = [{'item': 'F-0001', 'kind': 'spec-amend', 'account': 'w1', 'age': '5m'}]
        rs = f_rows(self.idx(), inflight=running)
        self.assertNotIn(rows.NO_STORIES, kinds(rs))
        self.assertEqual([r.item_id for r in rs if r.kind == rows.PLAN_CODE and r.launches],
                         ['T-0002', 'T-0003'])

    def test_a_pushed_spec_amend_waits_to_land_and_the_tasks_build(self):
        o = occ(open_branches=['spec/F-0001'])
        rs = f_rows(self.idx(), occupancy=o)
        self.assertIn(rows.PUSHED_LAND, kinds(rs))
        self.assertNotIn(rows.NO_STORIES, [r.kind for r in rs if r.launches])
        self.assertTrue(any(r.kind == rows.PLAN_CODE and r.launches for r in rs))

    def test_a_feature_with_stories_in_build_gets_no_spec_amend(self):
        idx = self.idx()
        idx['items']['S-0001'] = dict(STORY)
        idx['items']['F-0001']['children'].append('S-0001')
        self.assertNotIn(rows.NO_STORIES, kinds(f_rows(idx)))


class TheConfigOffKeepsTodaysBehaviour(unittest.TestCase):
    OFF = {'stories_before_plan': False}

    def test_the_default_is_on(self):
        self.assertTrue(rows.stories_before_plan(product()))
        self.assertFalse(rows.stories_before_plan(product(**self.OFF)))

    def test_spec_approved_gets_the_plan_row(self):
        self.assertEqual(kinds(f_rows(index('spec-approved'), product(**self.OFF))),
                         [rows.STARVED_PLAN])

    def test_a_pending_replan_launches(self):
        rs = f_rows(index('building 0/1', tasks=[('T-0001', 'New')],
                          reshape='re-cut the export', evidence=['spec on origin/main',
                                                                  'plan on origin/main']),
                    product(**self.OFF))
        self.assertIn(rows.REPLAN, kinds(rs))
        self.assertNotIn(rows.NO_STORIES, kinds(rs))

    def test_mid_build_gets_no_spec_amend(self):
        rs = f_rows(index('building 0/1', tasks=[('T-0001', 'New')]), product(**self.OFF))
        self.assertEqual(kinds(rs), [rows.PLAN_CODE])

    def test_conventions_refuse_a_non_boolean(self):
        from asf import conventions
        bad = conventions.validate_mapping({'feeder': {'stories_before_plan': 'yes'}})
        self.assertIn('feeder.stories_before_plan', [k for k, _w in bad])
        self.assertEqual(conventions.validate_mapping({'feeder': {'stories_before_plan': False}}),
                         [])


class TheSpecAmendBrief(unittest.TestCase):
    def brief(self):
        idx = index('building 1/2', tasks=[('T-0001', 'Closed'), ('T-0002', 'New')],
                    evidence=['spec on origin/main', 'plan on origin/main'])
        row = next(r for r in rows.candidates(idx, product(), [])
                   if r.kind == rows.NO_STORIES)
        return build_mod.build(product(), row, idx, []).text

    def test_it_derives_from_the_spec_plan_and_landed_tasks(self):
        text = self.brief()
        self.assertIn('DERIVE THE STORIES FROM THE SPEC, THE PLAN AND THE LANDED TASKS', text)
        self.assertIn('docs/specs/f-0001.md', text)
        self.assertIn('docs/plans/f-0001.md', text)
        self.assertIn('are proof candidates', text)
        self.assertIn('T-0001 [landed — keep]', text)

    def test_it_mints_through_the_record_and_invents_no_ids(self):
        text = self.brief()
        self.assertNotIn('Your job: amend the spec', text)
        self.assertIn('asf new story --parent F-0001', text)
        self.assertIn('NO INVENTED IDS', text)

    def test_each_line_is_testable_and_cites_its_proof(self):
        text = self.brief()
        self.assertIn('EACH ACCEPTANCE LINE IS TESTABLE', text)
        self.assertIn('proven by <path>', text)

    def test_an_ordinary_spec_amend_brief_has_no_stories_section(self):
        idx = index('spec-draft')
        row = next(r for r in rows.candidates(idx, product(), [])
                   if r.item_id == 'F-0001' and r.launches)
        self.assertEqual(row.kind, rows.STARVED_SPEC)
        text = build_mod.build(product(), row, idx, []).text
        self.assertNotIn('DERIVE THE STORIES', text)
        self.assertIn('Your job: amend the spec for F-0001', text)


if __name__ == '__main__':
    unittest.main()
