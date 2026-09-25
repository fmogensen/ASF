"""The lane experiment: a ``lane: direct`` Feature is one session end to end (DIRECT → BUILD), a
``size: s`` Feature writes its spec and plan in one session (CARD → SPEC+PLAN) and its small Tasks
skip the review, and the scorecard compares the lanes — pooled and per ``ab_pair``."""
import datetime
import importlib
import unittest

from asf import briefs
from asf.conventions import Conventions
from asf.env import Product
from asf.feeder import rows
from asf.harvest import lane
from asf.record.setfield import parse_assignments
from asf.scorecard import pairs, score
from asf.scorecard.facts import Facts
try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from occfixture import occ
except ImportError:  # pragma: no cover - import shape only
    from tests.occfixture import occ

build_mod = importlib.import_module('asf.briefs.build')
UTC = datetime.timezone.utc


def product(**conv):
    return Product('sample', {'conventions': dict(conv), 'main': 'main'})


def kinds(rs):
    return [(r.kind, r.item_id) for r in rs]


def feature(fid='F-0001', stage='card', **extra):
    return dict({'id': fid, 'type': 'feature', 'stage': stage, 'state': 'New', 'decided': True,
                 'title': 'a feature'}, **extra)


def index(*cards):
    return {'items': {c['id']: c for c in cards}}


class SettableFields(unittest.TestCase):
    def test_lane_size_and_pair_are_settable_on_a_feature(self):
        got = parse_assignments('feature', ['lane=direct', 'size=s', 'ab_pair=p1'])
        self.assertEqual([(k, v) for k, _s, _op, v in got],
                         [('lane', 'direct'), ('size', 's'), ('ab_pair', 'p1')])

    def test_a_lane_or_size_outside_its_words_is_refused(self):
        for bad in ('lane=fast', 'size=huge'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_assignments('feature', [bad])

    def test_a_task_has_no_lane(self):
        with self.assertRaises(ValueError):
            parse_assignments('task', ['lane=direct'])


class DirectRows(unittest.TestCase):
    def test_a_direct_card_gets_one_direct_build_row_and_no_spec(self):
        out = rows.candidates(index(feature(lane='direct')), product(), [])
        self.assertEqual(kinds(out), [('DIRECT → BUILD', 'F-0001')])
        r = out[0]
        self.assertTrue(r.launches)
        self.assertEqual((r.brief_kind, r.branch, r.tier), ('direct', 'cloud/direct-F-0001', 2))

    def test_no_spec_or_plan_row_at_any_document_stage(self):
        for stage in ('spec-draft', 'spec-review r1', 'spec-approved', 'plan-draft'):
            with self.subTest(stage=stage):
                out = rows.candidates(index(feature(stage=stage, lane='direct')), product(), [])
                self.assertEqual(kinds(out), [('DIRECT → BUILD', 'F-0001')])

    def test_pushed_and_waiting_it_launches_nothing(self):
        o = occ(busy=['F-0001'], open_branches=['cloud/direct-F-0001'])
        out = rows.candidates(index(feature(lane='direct')), product(), [], occupancy=o)
        self.assertEqual(kinds(out), [('PUSHED → LAND', 'F-0001')])
        self.assertFalse(out[0].launches)

    def test_a_live_session_holds_it(self):
        inflight = [{'item': 'F-0001', 'kind': 'direct'}]
        self.assertEqual(rows.candidates(index(feature(lane='direct')), product(), inflight), [])

    def test_its_lane_review_is_a_review_row_on_the_feature(self):
        o = occ(review={'F-0001': {'branch': 'cloud/direct-F-0001', 'round': 1}})
        out = rows.candidates(index(feature(lane='direct')), product(), [], occupancy=o)
        self.assertEqual(kinds(out), [('PUSHED → REVIEW', 'F-0001')])
        self.assertEqual(out[0].branch, 'cloud/direct-F-0001')

    def test_a_product_naming_its_own_prefixes_still_has_the_direct_lane(self):
        conv = Conventions.from_mapping({'branch_prefixes': {'code': 'feature/'}})
        self.assertEqual(conv.branch_kind('cloud/direct-F-0001'), 'direct')
        self.assertIn('cloud/direct-', conv.all_prefixes())
        o = occ(review={'F-0001': {'branch': 'cloud/direct-F-0001', 'round': 1}})
        out = rows.candidates(index(feature(lane='direct')),
                              product(branch_prefixes={'code': 'feature/'}), [], occupancy=o)
        self.assertEqual(kinds(out), [('PUSHED → REVIEW', 'F-0001')])

    def test_a_correction_is_its_one_session(self):
        o = occ(corrections={'F-0001': {'kind': 'gate', 'text': 'FAIL x', 'rounds': 1,
                                        'branch': 'cloud/direct-F-0001'}})
        out = rows.candidates(index(feature(lane='direct')), product(), [], occupancy=o)
        self.assertEqual(kinds(out), [('FIX → CORRECT', 'F-0001')])

    def test_finish_first_caps_direct_rows_while_planned_tasks_wait(self):
        cards = [feature(f'F-000{n}', lane='direct', rank=n) for n in (1, 2, 3)]
        cards += [feature('F-0099', stage='plan-approved', rank=99, children=['T-0099']),
                  {'id': 'T-0099', 'type': 'task', 'parent': 'F-0099', 'state': 'New',
                   'writes': ['a.py']}]
        idx = index(*cards)
        out = rows.finish_first(rows.candidates(idx, product(), []), idx['items'], product(), [])
        direct = [r for r in out if r.kind == 'DIRECT → BUILD']
        self.assertEqual([r.launches for r in direct], [True, True, False])
        self.assertTrue(direct[2].action.startswith(rows.FINISH))

    def test_a_planned_direct_feature_keeps_building_its_tasks(self):
        cards = [feature(stage='plan-approved', lane='direct', children=['T-0001']),
                 {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'New',
                  'writes': ['a.py']}]
        out = rows.candidates(index(*cards), product(), [])
        self.assertEqual(kinds(out), [('PLAN → CODE', 'T-0001')])


class SmallFeatureRows(unittest.TestCase):
    def test_a_small_card_gets_one_spec_plan_row(self):
        out = rows.candidates(index(feature(size='s')), product(), [])
        self.assertEqual(kinds(out), [('CARD → SPEC+PLAN', 'F-0001')])
        self.assertEqual((out[0].brief_kind, out[0].branch), ('spec-plan', 'plan/F-0001'))

    def test_a_normal_card_still_gets_its_spec(self):
        out = rows.candidates(index(feature(size='m')), product(), [])
        self.assertEqual(kinds(out), [('CARD → SPEC', 'F-0001')])

    def test_its_document_pushed_and_waiting_launches_nothing(self):
        o = occ(unlanded={'F-0001': {'spec-plan': 'pushed, waiting to land'}})
        out = rows.candidates(index(feature(size='s')), product(), [], occupancy=o)
        self.assertEqual(kinds(out), [('PUSHED → LAND', 'F-0001')])
        self.assertFalse(out[0].launches)


class Briefs(unittest.TestCase):
    def row(self, kind, brief_kind, branch):
        return rows.Row(tier=2, kind=kind, item_id='F-0001', feature_id='F-0001',
                        action=rows.LAUNCH, brief_kind=brief_kind, branch=branch, reason='r')

    def test_the_direct_brief_names_the_feature_in_every_subject_and_one_branch(self):
        idx = index(feature(lane='direct'))
        b = briefs.build(product(test_command='make test'),
                         self.row('DIRECT → BUILD', 'direct', 'cloud/direct-F-0001'), idx, [], {})
        self.assertEqual(b.kind, 'direct')
        self.assertIn('`feat(F-0001): <what>`', b.text)
        self.assertIn('ONE BRANCH: `cloud/direct-F-0001`', b.text)
        self.assertIn('What and how', b.text)
        self.assertIn('`make test`', b.text)
        self.assertEqual(b.model, build_mod.HEAVY)
        self.assertFalse(b.id_ranges_needed)

    def test_the_spec_plan_brief_writes_one_document_with_task_lines(self):
        idx = index(feature(size='s'))
        b = briefs.build(product(), self.row('CARD → SPEC+PLAN', 'spec-plan', 'plan/F-0001'),
                         idx, [], {})
        self.assertEqual(b.kind, 'spec-plan')
        self.assertIn('### Task N:', b.text)
        self.assertIn('`plan(F-0001): <what>`', b.text)
        self.assertTrue(b.id_ranges_needed)


class ReviewWaiver(unittest.TestCase):
    items = {'F-0001': feature(size='s'),
             'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'state': 'Active'},
             'F-0002': feature('F-0002'),
             'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0002', 'state': 'Active'}}

    def test_a_direct_branch_needs_no_review(self):
        self.assertTrue(lane.review_waived(Conventions(), lane.DIRECT, {}, 'F-0009', None))

    def test_a_small_features_small_task_skips_the_review(self):
        conv = Conventions()
        self.assertEqual(conv.review_skip_under_lines(), 80)
        self.assertIn('79 changed lines', lane.review_waived(conv, 'code', self.items, 'T-0001', 79))
        self.assertEqual(lane.review_waived(conv, 'code', self.items, 'T-0001', 80), '')
        self.assertEqual(lane.review_waived(conv, 'code', self.items, 'T-0002', 5), '')

    def test_the_limit_is_a_convention(self):
        conv = Conventions.from_mapping({'review': {'skip_under_lines': 200}})
        self.assertEqual(conv.review_skip_under_lines(), 200)
        self.assertTrue(lane.review_waived(conv, 'code', self.items, 'T-0001', 150))
        off = Conventions.from_mapping({'review': {'skip_under_lines': 0}})
        self.assertEqual(lane.review_waived(off, 'code', self.items, 'T-0001', 1), '')


def lane_facts():
    """F-0010 direct (pair p1): carded 09-01, landed 09-02 — 1 session $5, 10 CI min.
    F-0011 full (pair p1): carded 09-01, landed 09-04, Task T-0011 — spec $4, coder $6, review $1
    (a repair session), 20 CI min on its Task. F-0012 full / F-0013 direct (pair p2): open."""
    def f(iid, lane_, pair, landed=None, parent=None, typ='feature'):
        return {'id': iid, 'type': typ, 'parent': parent, 'title': iid, 'text': '',
                'created': '2026-09-01T00:00:00Z', 'landed': landed, 'prod': None,
                'lane': lane_, 'ab_pair': pair, 'send_backs': 0, 'reopens': 0}
    items = {'F-0010': f('F-0010', 'direct', 'p1', '2026-09-02T00:00:00Z'),
             'F-0011': f('F-0011', None, 'p1', '2026-09-04T00:00:00Z'),
             'T-0011': f('T-0011', None, None, '2026-09-04T00:00:00Z', 'F-0011', 'task'),
             'F-0012': f('F-0012', None, 'p2'),
             'F-0013': f('F-0013', 'direct', 'p2')}
    sessions = [{'ts': '2026-09-01T10:00:00Z', 'task': 'direct-f-0010', 'item': 'F-0010', 'usd': 5.0},
                {'ts': '2026-09-01T10:00:00Z', 'task': 'spec-f-0011', 'item': 'F-0011', 'usd': 4.0},
                {'ts': '2026-09-02T10:00:00Z', 'task': 'coder-t-0011', 'item': 'T-0011', 'usd': 6.0},
                {'ts': '2026-09-03T10:00:00Z', 'task': 'review-t-0011', 'item': 'T-0011', 'usd': 1.0}]
    ci = [{'ts': '2026-09-01T12:00:00Z', 'items': ['F-0010'], 'minutes': 10},
          {'ts': '2026-09-03T12:00:00Z', 'items': ['T-0011'], 'minutes': 20}]
    return Facts(items=items, sessions=sessions, ci=ci, gates=[], runs=[], clutter={},
                 as_of='2026-09-06T23:00:00Z')


class ScorecardByLane(unittest.TestCase):
    def window(self):
        end = datetime.datetime(2026, 9, 6, 23, 0, 1, tzinfo=UTC)
        return end - datetime.timedelta(days=7), end

    def test_the_pooled_numbers_split_by_lane(self):
        got = score.by_lane(lane_facts(), *self.window())
        self.assertEqual(got['direct'], {'lane': 'direct', 'landed': 1, 'ids': ['F-0010'],
                                         'median_lead_days': 1.0, 'usd_per_feature': 5.0,
                                         'sessions_per_feature': 1.0, 'repair_per_feature': 0.0,
                                         'ci_min_per_feature': 10.0})
        # F-0011: spec $4 + coder $6 + review $1 = $11, 3 sessions, 1 repair, 20 CI min
        self.assertEqual(got['full'], {'lane': 'full', 'landed': 1, 'ids': ['F-0011'],
                                       'median_lead_days': 3.0, 'usd_per_feature': 11.0,
                                       'sessions_per_feature': 3.0, 'repair_per_feature': 1.0,
                                       'ci_min_per_feature': 20.0})
        line = score.lanes_line(got, 7)
        self.assertTrue(line.startswith('lanes 7 d: direct 1 landed, lead 1 d, $5.00/f'), line)
        self.assertIn('full 1 landed, lead 3 d, $11.00/f, 3 sessions/f, 1 repair/f, 20 CI min/f',
                      line)

    def test_the_pair_table_puts_each_direct_feature_beside_its_partner(self):
        table = {p['pair']: p for p in score.pair_table(lane_facts())}
        p1 = table['p1']
        self.assertEqual((p1['direct']['id'], p1['full']['id']), ('F-0010', 'F-0011'))
        self.assertEqual(p1['delta'], {'lead_days': -2.0, 'cost': -6.0, 'sessions': -2,
                                       'repair_sessions': -1, 'ci_min': -10.0})
        self.assertEqual(p1['note'], '')
        p2 = table['p2']
        self.assertEqual((p2['direct']['id'], p2['full']['id']), ('F-0013', 'F-0012'))
        self.assertNotIn('lead_days', p2['delta'])  # neither landed: no lead time to compare

    def test_a_pair_with_two_features_on_one_lane_says_so(self):
        facts = lane_facts()
        facts.items['F-0012']['lane'] = 'direct'
        p2 = {p['pair']: p for p in score.pair_table(facts)}['p2']
        self.assertIsNone(p2['full'])
        self.assertIn('one direct and one full', p2['note'])

    def test_the_view_renders_lanes_pairs_and_overlaps(self):
        from asf.views import scorecard as view
        facts = lane_facts()
        prod = Product('sample', {'conventions': {}})
        d = view.compute_lanes(None, prod, days=7, facts=facts,
                               overlaps=[('p1', ('F-0010', 'F-0011'), ['app/x.py'])])
        text = view.render_lanes(d)
        self.assertIn('| direct | 1 | 1 d | $5.00 |', text)
        self.assertIn('| p1 | F-0010 | F-0011 | 1 d / 3 d / -2 d | $5.00 / $11.00 / -$6.00 |', text)
        self.assertIn('| p2 | F-0013 (open) | F-0012 (open) |', text)
        self.assertIn('pair p1 (F-0010 / F-0011) overlaps in 1 file(s): app/x.py', text)


class PairOverlap(unittest.TestCase):
    items = {'F-0010': {'id': 'F-0010', 'type': 'feature', 'ab_pair': 'p1'},
             'F-0011': {'id': 'F-0011', 'type': 'feature', 'ab_pair': 'p1'},
             'T-0011': {'id': 'T-0011', 'type': 'task', 'parent': 'F-0011'},
             'F-0020': {'id': 'F-0020', 'type': 'feature', 'ab_pair': 'p2'},
             'F-0021': {'id': 'F-0021', 'type': 'feature', 'ab_pair': 'p2'}}

    def test_pairs_by_name(self):
        self.assertEqual(pairs.pairs(self.items), {'p1': ['F-0010', 'F-0011'],
                                                   'p2': ['F-0020', 'F-0021']})

    def test_a_pair_whose_features_touch_one_file_is_flagged(self):
        files = {'F-0010': {'app/a.py', 'app/b.py'}, 'F-0011': {'app/b.py'},
                 'F-0020': {'app/c.py'}, 'F-0021': {'app/d.py'}}
        found = pairs.overlaps(None, self.items, touched_fn=files.get)
        self.assertEqual(found, [('p1', ('F-0010', 'F-0011'), ['app/b.py'])])
        self.assertIn('overlaps in 1 file(s): app/b.py', pairs.overlap_lines(found)[0])

    def test_a_features_branches_are_its_direct_lane_and_its_tasks_code_lane(self):
        ids = pairs.subtree_ids(self.items, 'F-0011')
        self.assertEqual(ids, ['F-0011', 'T-0011'])
        self.assertEqual(pairs.branches_of(self.items, ids, Conventions()),
                         ['cloud/direct-F-0011', 'worker/T-0011'])


if __name__ == '__main__':
    unittest.main()
