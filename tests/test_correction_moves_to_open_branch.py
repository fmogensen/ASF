"""A correction whose PR has ended moves to the item's open code branch (round E #19).

A product's T-0488 (2026-10-05): an operator ruling (``asf correct``) was written on the run of
the item's plan branch, whose PR then merged with its batch. The occupancy dropped every
correction on a dead branch, so the ruling vanished; the Task's own code branch, sent BACK, had no
correction pending and got a non-launching PUSHED → LAND row saying no run held it."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf.feeder import rows
from asf.workers import lifecycle

HEAD_PLAN = 'a' * 40
HEAD_CODE = 'b' * 40
ITEM = 'T-0001'


class _Ledger(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 'sessions.jsonl')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def add_run(self, job, kind, branch, started, lane=None, correction=None):
        self.write({'job': job, 'item': ITEM, 'kind': kind, 'branch': branch, 'pid': 1,
                    'started': started},
                   {'job': job, 'ended': started.replace(':00Z', ':30Z'),
                    'end_reason': 'finished'})
        if lane:
            self.write({'job': job, 'lane': lane})
        if correction:
            self.write({'job': job, 'correction': correction})

    def ledger(self, code_lane):
        self.add_run('coder-t-0001', 'coder', 'cloud/T-0001', '2026-10-05T10:00:00Z',
                 lane=dict(code_lane, head=HEAD_CODE, item=ITEM))
        self.add_run('reshape-t-0001', 'reshape', 'cloud/plan-T-0001', '2026-10-05T11:00:00Z',
                 lane={'state': 'PR_OPEN', 'pr': 50, 'head': HEAD_PLAN, 'item': ITEM},
                 correction={'kind': 'operator', 'operator_ruling': True, 'same': 3,
                             'text': 'OPERATOR RULING: keep the schema as it is',
                             'at': '2026-10-05T12:00:00Z'})

    def occupancy(self):
        return lifecycle.occupancy(self.path, alive=lambda *_a, **_k: False,
                                   ended={50: {'state': 'MERGED', 'head': HEAD_PLAN}})


class CorrectionMovesTests(_Ledger):

    def test_a_ruling_on_a_merged_plan_branch_moves_to_the_tasks_open_code_branch(self):
        self.ledger({'state': 'BACK', 'pr': 51, 'reason': 'kind=review'})
        occ = self.occupancy()
        corr = occ['corrections'].get(ITEM)
        self.assertIsNotNone(corr, 'the ruling was dropped with its dead branch')
        self.assertEqual(corr['branch'], 'cloud/T-0001')
        self.assertEqual(corr['moved_from'], 'cloud/plan-T-0001')

    def test_the_moved_ruling_is_a_launching_correction_on_the_code_branch(self):
        self.ledger({'state': 'BACK', 'pr': 51, 'reason': 'kind=review'})
        occ = self.occupancy()
        items = {ITEM: {'id': ITEM, 'type': 'task', 'state': 'Active', 'title': 't',
                        'writes': ['a.ts']}}
        product = mock.Mock(conventions=mock.Mock(branch_kind=lambda b: 'code'))
        with mock.patch.object(rows, 'console_amend_row', lambda *a, **k: None):
            out, ids = rows.correction_rows(items, product, set(), occ['corrections'])
        self.assertEqual(ids, {ITEM})
        self.assertTrue(out[0].launches)
        self.assertEqual(out[0].branch, 'cloud/T-0001')
        self.assertIn('OPERATOR RULING', out[0].correction)

    def test_with_no_open_code_branch_it_is_still_dropped(self):
        self.ledger({'state': 'STALE', 'pr': 51, 'reason': 'PR #51 closed unmerged'})
        self.assertNotIn(ITEM, self.occupancy()['corrections'])

    def test_a_document_branch_is_never_the_code_branch(self):
        self.add_run('plan-t-0001', 'plan', 'cloud/plan2-T-0001', '2026-10-05T09:00:00Z',
                 lane={'state': 'PUSHED', 'head': 'c' * 40, 'item': ITEM})
        self.add_run('reshape-t-0001', 'reshape', 'cloud/plan-T-0001', '2026-10-05T11:00:00Z',
                 lane={'state': 'PR_OPEN', 'pr': 50, 'head': HEAD_PLAN, 'item': ITEM},
                 correction={'kind': 'operator', 'operator_ruling': True, 'same': 3,
                             'text': 'RULING', 'at': '2026-10-05T12:00:00Z'})
        self.assertNotIn(ITEM, self.occupancy()['corrections'])


class SentBackLabelTests(_Ledger):

    def test_a_sent_back_branch_with_no_correction_says_so(self):
        self.add_run('coder-t-0001', 'coder', 'cloud/T-0001', '2026-10-05T10:00:00Z',
                 lane={'state': 'BACK', 'pr': 51, 'head': HEAD_CODE, 'item': ITEM,
                       'reason': 'kind=review'})
        occ = self.occupancy()
        self.assertEqual(occ['back'][ITEM]['branch'], 'cloud/T-0001')
        items = {ITEM: {'id': ITEM, 'type': 'task', 'state': 'Active', 'title': 't'}}
        product = mock.Mock(conventions=mock.Mock(branch_kind=lambda b: 'code'))
        pushed = rows.pushed_ids(items, occ)
        self.assertEqual(pushed, {ITEM})
        with mock.patch.object(rows, '_branch_of', lambda *a, **k: 'cloud/T-0001'):
            out = rows.pushed_rows(items, product, pushed, occ, set())
        self.assertEqual(len(out), 1)
        self.assertNotIn('no run holds it', out[0].action)
        self.assertIn('sent back (kind=review)', out[0].action)
        self.assertIn('no correction pending', out[0].action)


if __name__ == '__main__':
    unittest.main()
