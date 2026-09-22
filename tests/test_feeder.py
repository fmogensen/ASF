"""asf.feeder: rows, the footprint gate, the stalemate gate, the S1 lane tiers, the incident clock
and ``asf next`` — against the fixture index under tests/fixtures/feeder/."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import os
import tempfile
import unittest

from asf import env
from asf.env import Product
from asf.feeder import footprint, register, render, rows, tiers

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

    def test_undecided_and_removed_cards_emit_nothing(self):
        ids = {r.item_id for r in self.cand()}
        self.assertNotIn('F-0006', ids)
        self.assertNotIn('F-0008', ids)

    def test_two_tasks_sharing_a_file_one_waits(self):
        by = {r.item_id: r for r in self.cand()}
        self.assertEqual(by['T-0001'].action, 'would launch')
        self.assertEqual(by['T-0002'].action, 'WAITS ON T-0001')
        self.assertEqual(by['T-0002'].waits_on, 'T-0001')

    def test_a_running_task_holds_its_footprint(self):
        by = {r.item_id: r for r in self.cand()}
        self.assertEqual(by['T-0003'].action, 'WAITS ON T-0007')

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
        self.assertEqual((by['T-0007'].kind, by['T-0007'].branch), ('CONFLICT → REBASE', 'task/T-0007'))
        self.assertEqual((by['T-0006'].kind, by['T-0006'].brief_kind), ('STALE → CLOSE', 'close'))

    def test_bug_rows(self):
        bugs = [r for r in self.cand() if r.kind == 'BUG → FIX']
        self.assertEqual([(r.item_id, r.tier, r.brief_kind, r.branch) for r in bugs],
                         [('B-0001', 0, 'fix-bug', 'fix/B-0001'), ('B-0002', 1, 'fix-bug', 'fix/B-0002')])

    def test_bug_with_a_session_is_not_a_row(self):
        self.assertNotIn('B-0001', {r.item_id for r in self.cand([S1_SESSION])})

    def test_fix_prefix_from_conventions(self):
        p = product(conventions={'branch_prefixes': {'fix': 'hotfix-'}})
        by = {r.item_id: r for r in rows.candidates(self.index, p, [])}
        self.assertEqual(by['B-0001'].branch, 'hotfix-B-0001')

    def test_blocked_feature_emits_nothing(self):
        idx = copy.deepcopy(self.index)
        idx['items']['F-0001']['blocked'] = True
        self.assertNotIn('F-0001', {r.item_id for r in rows.candidates(idx, self.p, [])})

    def test_takes_a_bare_item_map(self):
        self.assertEqual(kinds(rows.candidates(self.index['items'], self.p, [])), kinds(self.cand()))


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
        self.assertEqual(out, [])


class CorrectionRowTest(unittest.TestCase):
    """B-0032: a held branch goes back to its session as a FIX → CORRECT row."""

    def corr(self, rounds, text='FAIL: test_x'):
        return {'B-0001': {'kind': 'gate', 'text': text, 'rounds': rounds, 'at': '2026-09-21T00:00:00Z'}}

    def test_a_correction_yields_a_correct_row_in_the_items_tier(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             corrections=self.corr(1))
        self.assertEqual(kinds(out), [('FIX → CORRECT', 'B-0001')])
        self.assertEqual((out[0].brief_kind, out[0].tier, out[0].branch, out[0].correction),
                         ('correct', 0, 'fix/B-0001', 'FAIL: test_x'))
        self.assertTrue(out[0].launches)

    def test_three_rounds_is_the_adjudicate_row(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [], 1, attempts={'B-0001': 1},
                             corrections=self.corr(3))
        self.assertEqual([(r.kind, r.brief_kind) for r in out],
                         [('STALEMATE → ADJUDICATE', 'adjudicate')])

    def test_a_busy_item_gets_no_correct_row(self):
        out = rows.plan_rows(s1_bugs('B-0001'), product(), [{'item': 'B-0001'}], 1,
                             corrections=self.corr(1))
        self.assertEqual(out, [])


class TiersTest(unittest.TestCase):
    """F-0071 acceptance 1: the S1 lane."""

    def test_unheld_s1_blocks_every_feature(self):
        out = rows.plan_rows(ten_features_and_an_s1(), product(), [], 4)
        self.assertEqual(kinds(out), [('BUG → FIX', 'B-0001')])

    def test_held_s1_frees_the_rest_of_capacity(self):
        out = rows.plan_rows(ten_features_and_an_s1(), product(), [S1_SESSION], 4)
        # rank order: F-0010 has rank 1
        self.assertEqual(kinds(out), [('CARD → SPEC', 'F-0010'), ('CARD → SPEC', 'F-0009'),
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
        self.assertEqual(sum(1 for r in out if r.launches), 5)
        self.assertEqual(kinds(out)[-3:], [('PLAN → CODE', 'T-0002'), ('PLAN → CODE', 'T-0003'),
                                           ('STALEMATE → ADJUDICATE', 'F-0003')])


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


class RenderTest(unittest.TestCase):
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
                                        'correction'})


class CliTest(unittest.TestCase):
    def run_next(self, argv):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
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
        self.assertEqual([d['item_id'] for d in json.loads(out)], ['B-0001', 'B-0002'])


if __name__ == '__main__':
    unittest.main()
