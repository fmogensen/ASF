"""``asf correct``: one correction round asked for by hand — written like the harvest's own,
turned into a CORRECT row, bound by the round cap, the park and the one-push rule."""
import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import importlib
import unittest

from asf import env
from asf.feeder import rows
from asf.workers import correct, lifecycle, pushlog


class CorrectTests(unittest.TestCase):
    RUN = {'job': 'review-t-0017', 'item': 'T-0017', 'kind': 'review', 'pid': 1,
           'branch': 'cloud/T-0017', 'started': '2026-09-01T09:00:00Z',
           'ended': '2026-09-01T09:30:00Z', 'end_reason': 'finished'}
    ITEMS = {'T-0017': {'id': 'T-0017', 'type': 'task', 'state': 'New'}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='correct_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\n')
        self.path = os.path.join(env.state_dir('sample'), 'sessions.jsonl')

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger(self, *records):
        with open(self.path, 'w') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def read(self):
        with open(self.path) as f:
            return f.read()

    def run_cmd(self, why='widen the budgets', pr=None, item='T-0017', fetch=None):
        args = argparse.Namespace(item=item, why=why, from_pr=pr, product='sample')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = correct.cmd_correct(args, fetch=fetch)
        return rc, out.getvalue()

    def rows(self):
        return rows.correction_rows(self.ITEMS, env.load_product('sample'), set(),
                                    lifecycle.corrections(self.path))[0]

    def test_writes_a_correct_row_quoting_the_text(self):
        self.ledger(self.RUN)
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        [row] = self.rows()
        self.assertEqual((row.kind, row.brief_kind, row.branch, row.action),
                         (rows.FIX_CORRECT, 'correct', 'cloud/T-0017', rows.LAUNCH))
        self.assertIn('widen the budgets', row.correction)
        c = lifecycle.corrections(self.path)['T-0017']
        self.assertEqual((c['kind'], c['rounds']), (correct.OPERATOR, 1))

    def test_the_brief_carries_the_one_push_rule(self):
        self.ledger(self.RUN)
        self.run_cmd()
        [row] = self.rows()
        build_mod = importlib.import_module('asf.briefs.build')
        text = build_mod.correction_text(row, 'correct')
        self.assertIn('widen the budgets', text)
        self.assertIn('ONE PUSH', text)
        self.assertIn('correct', pushlog.ONE_PUSH_KINDS)

    def test_from_pr_names_the_commits_to_cherry_pick(self):
        self.ledger(self.RUN)
        seen = []

        def fetch(args):
            seen.append(args)
            return {'baseRefName': 'cloud/T-0017',
                    'commits': [{'oid': 'a' * 40}, {'oid': 'b' * 40}]}
        rc, out = self.run_cmd(pr=1027, fetch=fetch)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen[0][:3], ['pr', 'view', '1027'])
        [row] = self.rows()
        self.assertIn('PR #1027', row.correction)
        self.assertIn('a' * 40 + ' ' + 'b' * 40, row.correction)
        self.assertIn('cherry-pick', row.correction)
        self.assertIn('never landed', row.correction)
        self.assertIn('push once', row.correction)

    def test_a_pr_gh_cannot_read_is_refused_and_writes_nothing(self):
        self.ledger(self.RUN)
        before = self.read()
        rc, out = self.run_cmd(pr=9, fetch=lambda a: None)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.read(), before)

    def test_the_round_cap_refuses(self):
        self.ledger(dict(self.RUN, rounds=lifecycle.ROUND_CAP))
        before = self.read()
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1, out)
        self.assertIn('adjudication', out)
        self.assertEqual(self.read(), before)

    def test_a_round_is_spent(self):
        self.ledger(dict(self.RUN, rounds=1))
        self.run_cmd()
        self.assertEqual(lifecycle.rounds_of(self.path, 'T-0017'), 2)

    def test_a_hand_park_and_a_live_session_refuse(self):
        self.ledger(self.RUN)
        lifecycle.note_park(self.path, 'T-0017', lifecycle.SCOPE_ITEM, 'r', 'w')
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1, out)
        self.assertIn('asf unpark', out)
        live = dict(self.RUN, job='correct-t-0017', started='2026-09-02T09:00:00Z')
        live.pop('ended'), live.pop('end_reason')
        self.ledger(self.RUN, live)
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1, out)
        self.assertIn('running', out)

    def test_it_supersedes_a_factory_park_on_the_item(self):
        parked = {'job': 'review-t-0017', 'correction': {
            'kind': 'relaunch cap', 'text': 'x', 'at': '2026-09-01T10:00:00Z',
            'parked': True, 'reason': 'launched twice'}}
        self.ledger(self.RUN, parked)
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        [row] = self.rows()
        self.assertEqual(row.action, rows.LAUNCH)

    def test_unknown_item_and_missing_why(self):
        self.ledger(self.RUN)
        self.assertEqual(self.run_cmd(item='T-9999')[0], 1)
        self.assertEqual(self.run_cmd(why='  ')[0], 2)
        self.assertEqual(self.run_cmd(item='nope')[0], 2)


if __name__ == '__main__':
    unittest.main()
