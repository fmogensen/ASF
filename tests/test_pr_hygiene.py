"""PR hygiene is a read-only view over the lane (:mod:`asf.harvest.pr_hygiene`).

What it once classified by itself — a stale unreviewed PR, a conflicting approved one — is the
lane's transitions now (T12 STALE, T9 BACK ``kind=conflict``); the view lists them."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from asf import env
from asf.harvest import pr_hygiene


class HygieneView(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hygiene_')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.product = env.Product('sample', {'conventions': {}})

    def run_(self, job, item, branch, state, reason='', **extra):
        lane = {'state': state, 'head': 'a' * 40, 'pr': extra.pop('pr', None),
                'at': '2026-09-21T00:00:00Z', 'reason': reason, 'item': item, **extra}
        with open(os.path.join(self.dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'pid': None,
                                'started': '2026-09-21T00:00:00Z', 'lane': lane}) + '\n')

    def test_stale_and_conflicting_lane_branches_are_the_rows(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'PR #4 closed unmerged', pr=4)
        self.run_('b', 'T-0002', 'worker/T-0002', 'BACK', 'kind=conflict', pr=5)
        self.run_('c', 'T-0003', 'worker/T-0003', 'BACK', 'kind=gate')
        self.run_('d', 'T-0004', 'worker/T-0004', 'MERGED', 'method=squash', sha='b' * 40)
        self.run_('e', 'T-0005', 'worker/T-0005', 'REVIEW', 'round 1 wanted')
        found = pr_hygiene.rows(self.product, self.dir)
        self.assertEqual([(r['kind'], r['branch']) for r in found],
                         [(pr_hygiene.STALE_CLOSE, 'worker/T-0001'),
                          (pr_hygiene.CONFLICT_REBASE, 'worker/T-0002')])
        self.assertEqual(pr_hygiene.render(found[0]),
                         'STALE → CLOSE  worker/T-0001 (T-0001) PR #4 — PR #4 closed unmerged')

    def test_main_prints_the_rows_and_the_lanes(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'branch gone', pr=4)
        with mock.patch.object(env, 'load_product', lambda name=None: self.product), \
                mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.dir):
            for argv, want in (([], 'STALE → CLOSE  worker/T-0001 (T-0001) PR #4 — branch gone'),
                               (['--lanes'], '4')):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(pr_hygiene.main(argv), 0)
                self.assertEqual(buf.getvalue().strip(), want)


class LaneListing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hygiene_')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.product = env.Product('sample', {'conventions': {}})

    def run_(self, job, item, branch, state, reason='', **extra):
        lane = {'state': state, 'head': 'a' * 40, 'pr': extra.pop('pr', None),
                'at': '2026-09-21T00:00:00Z', 'reason': reason, 'item': item, **extra}
        with open(os.path.join(self.dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'pid': None,
                                'started': '2026-09-21T00:00:00Z', 'lane': lane}) + '\n')

    def test_two_prs_in_two_lanes_give_two_entries_with_the_five_keys(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'PR #4 closed unmerged', pr=4)
        self.run_('b', 'T-0002', 'worker/T-0002', 'BACK', 'kind=conflict', pr=5,
                   at=None, head_at='2026-09-20T00:00:00Z')
        found = pr_hygiene.lanes(self.product, self.dir)
        self.assertEqual([sorted(e.keys()) for e in found],
                         [sorted(['pr', 'branch', 'lane', 'since', 'action'])] * 2)
        self.assertEqual([(e['pr'], e['branch'], e['lane'], e['action']) for e in found],
                         [(4, 'worker/T-0001', 'stale', 'close'),
                          (5, 'worker/T-0002', 'conflict', 'rebase')])

    def test_lane_and_action_come_from_the_table(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'PR #4 closed unmerged', pr=4)
        found = pr_hygiene.lanes(self.product, self.dir)
        kind = pr_hygiene.STALE_CLOSE
        self.assertEqual((found[0]['lane'], found[0]['action']), pr_hygiene.LANES[kind])

    def test_since_is_the_records_at(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'PR #4 closed unmerged', pr=4)
        found = pr_hygiene.lanes(self.product, self.dir)
        self.assertEqual(found[0]['since'], '2026-09-21T00:00:00Z')

    def test_since_falls_back_to_head_at(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'BACK', 'kind=conflict', pr=4,
                   at=None, head_at='2026-09-20T00:00:00Z')
        found = pr_hygiene.lanes(self.product, self.dir)
        self.assertEqual(found[0]['since'], '2026-09-20T00:00:00Z')

    def test_since_is_none_when_neither_is_present(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'PR #4 closed unmerged', pr=4, at=None)
        found = pr_hygiene.lanes(self.product, self.dir)
        self.assertIn('since', found[0])
        self.assertIsNone(found[0]['since'])

    def test_a_conflict_row_with_no_pr_is_absent(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'BACK', 'kind=conflict')
        self.assertEqual(pr_hygiene.lanes(self.product, self.dir), [])

    def test_a_gate_a_merged_and_a_review_row_contribute_nothing(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'BACK', 'kind=gate', pr=4)
        self.run_('b', 'T-0002', 'worker/T-0002', 'MERGED', 'method=squash', pr=5,
                  sha='b' * 40)
        self.run_('c', 'T-0003', 'worker/T-0003', 'REVIEW', 'round 1 wanted', pr=6)
        self.assertEqual(pr_hygiene.lanes(self.product, self.dir), [])

    def test_the_vocabulary_is_exactly_two_lanes(self):
        self.assertEqual(set(pr_hygiene.LANES.values()), {('stale', 'close'), ('conflict', 'rebase')})


if __name__ == '__main__':
    unittest.main()
