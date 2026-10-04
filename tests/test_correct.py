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

    def run_cmd(self, why='widen the budgets', pr=None, item='T-0017', fetch=None, alive=None):
        args = argparse.Namespace(item=item, why=why, from_pr=pr, product='sample')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = correct.cmd_correct(args, fetch=fetch, alive=alive or (lambda pid: True))
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

    def card(self):
        backlog = os.path.join(self.tmp, 'backlog')
        os.makedirs(os.path.join(backlog, 'tasks'), exist_ok=True)
        with open(env.product_path('sample'), 'w') as f:
            f.write(f'product: sample\nrepo_slug: x/y\nbacklog_dir: {backlog}\n')
        path = os.path.join(backlog, 'tasks', 'T-0017.md')
        with open(path, 'w') as f:
            f.write('---\nid: T-0017\ntype: task\ntitle: t\n---\n## Description\nd\n\n'
                    '## History\n- 2026-09-01 09:00 created\n')
        return path

    def test_at_the_round_cap_the_instruction_goes_to_adjudication_as_a_ruling(self):
        import importlib
        build = importlib.import_module("asf.briefs.build")
        from asf.evidence import rulings
        card = self.card()
        self.ledger(dict(self.RUN, rounds=lifecycle.ROUND_CAP))
        rc, out = self.run_cmd(why='D12 stands: remove the provider entry; push once.')
        self.assertEqual(rc, 0, out)
        self.assertIn('operator ruling', out)
        self.assertEqual(lifecycle.rounds_of(self.path, 'T-0017'), lifecycle.ROUND_CAP)
        [row] = self.rows()
        self.assertEqual((row.kind, row.brief_kind), (rows.STALEMATE, 'adjudicate'))
        self.assertIn('OPERATOR RULING', row.correction)
        self.assertIn('D12 stands: remove the provider entry', row.correction)
        brief = build.correction_text(row, 'adjudicate')
        self.assertIn('OPERATOR RULING', brief)
        self.assertIn('D12 stands', brief)
        with open(card) as f:
            self.assertIn('adjudicate (operator): D12 stands', f.read())
        [ruling] = rulings.standing(env.load_product('sample'), 'T-0017')
        self.assertEqual(ruling['job'], 'operator')
        self.assertIn('D12 stands', rulings.brief_section([ruling]))
        self.assertIn('adjudicate', build.RULINGS_KINDS)

    def test_at_the_cap_a_from_pr_is_refused(self):
        self.ledger(dict(self.RUN, rounds=lifecycle.ROUND_CAP))
        before = self.read()
        rc, out = self.run_cmd(pr=12, fetch=lambda a: {})
        self.assertEqual(rc, 1, out)
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

    def _open_run(self, pid):
        live = dict(self.RUN, job='correct-t-0017', pid=pid, started='2026-09-02T09:00:00Z')
        live.pop('ended'), live.pop('end_reason')
        self.ledger(self.RUN, live)

    def test_a_dead_pid_entry_is_stale_and_does_not_block(self):
        self._open_run(999999)
        rc, out = self.run_cmd(alive=lambda pid: False)
        self.assertEqual(rc, 0, out)
        self.assertIn('stale', out)

    def test_a_live_run_refuses(self):
        self._open_run(4242)
        rc, out = self.run_cmd(alive=lambda pid: pid == 4242)
        self.assertEqual(rc, 1, out)
        self.assertIn('running', out)

    def test_a_cloud_run_still_working_refuses_and_an_ended_one_proceeds(self):
        from unittest import mock
        from asf.workers import cloudpid
        tok = cloudpid.PREFIXES[0] + 'x1' if isinstance(cloudpid.PREFIXES, tuple) else 'x'
        self._open_run(tok)
        with mock.patch.object(cloudpid, 'status', return_value=cloudpid.WORKING):
            rc, out = self.run_cmd(alive=lifecycle.pid_alive)
        self.assertEqual(rc, 1, out)
        self.assertIn('running', out)
        with mock.patch.object(cloudpid, 'status', return_value='ended'):
            rc, out = self.run_cmd(alive=lifecycle.pid_alive)
        self.assertEqual(rc, 0, out)
        self.assertIn('stale', out)

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



class ResetTests(unittest.TestCase):
    """``asf reset <item> --why``: an operator voids a wrong landing claim ``(pr, head)``; the
    item starts over, a different PR lands normally, the same claim re-reported closes nothing;
    ``--undo`` takes it back."""
    SHA = '53c00ae' + '0' * 33
    LANDED = {'job': 'coder-t-0091', 'item': 'T-0091', 'kind': 'coder', 'pid': 1,
              'branch': 'cloud/T-0091', 'started': '2026-09-01T09:00:00Z',
              'ended': '2026-09-01T09:30:00Z', 'end_reason': 'finished', 'harvested': SHA,
              'lane': {'state': 'MERGED', 'pr': 966, 'sha': SHA, 'at': '2026-09-01T10:00:00Z'}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='reset_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\n')
        self.path = os.path.join(env.state_dir('sample'), 'sessions.jsonl')
        self.history = []
        from unittest import mock
        p = mock.patch.object(correct, 'file_history',
                              side_effect=lambda product, item, line, msg, step='': self.history.append(line) or '')
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger(self, *records):
        with open(self.path, 'a') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def reset(self, undo=False, why='#966 merged but did not land T-0091', alive=None):
        args = argparse.Namespace(item='t-0091', why=why, undo=undo, product='sample')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = correct.cmd_reset(args, alive=alive or (lambda pid: False))
        return rc, out.getvalue()

    def lines(self, key):
        with open(self.path) as f:
            return [json.loads(x) for x in f if key in json.loads(x)]

    def test_reset_voids_the_claim_clears_harvested_writes_history_and_is_idempotent(self):
        self.ledger(self.LANDED)
        rc, out = self.reset()
        self.assertEqual(rc, 0, out)
        [line] = self.lines('reset')
        self.assertEqual((line['reset']['pr'], line['reset']['head'], line['reset']['void']),
                         (966, self.SHA, True))
        self.assertIsNone(lifecycle.latest(self.path)['coder-t-0091'].get('harvested'))
        self.assertEqual(len(self.history), 1)
        self.assertIn('reset (operator): landing 53c00ae (PR #966) voided', self.history[0])
        rc, out = self.reset()
        self.assertEqual(rc, 0, out)
        self.assertIn('already void', out)
        self.assertEqual(len(self.lines('reset')), 1)

    def test_a_live_run_refuses_and_writes_nothing(self):
        live = dict(self.LANDED, job='correct-t-0091', pid=4242, started='2026-09-02T09:00:00Z')
        for k in ('ended', 'end_reason', 'harvested', 'lane'):
            live.pop(k)
        self.ledger(self.LANDED, live)
        rc, out = self.reset(alive=lambda pid: pid == 4242)
        self.assertEqual(rc, 1, out)
        self.assertIn('running', out)
        self.assertEqual(self.lines('reset'), [])

    def test_no_landing_claim_and_missing_why(self):
        self.ledger(dict(self.LANDED, harvested='superseded', lane={}))
        self.assertEqual(self.reset()[0], 1)
        self.assertEqual(self.reset(why=' ')[0], 2)

    def test_the_item_is_no_longer_landed_and_undo_restores_it(self):
        self.ledger(self.LANDED)
        self.assertEqual(lifecycle.occupancy(self.path, alive=lambda pid: False)['landed'],
                         {'T-0091': self.SHA})
        self.reset()
        self.assertEqual(lifecycle.occupancy(self.path, alive=lambda pid: False)['landed'], {})
        self.assertEqual(len(lifecycle.voids(self.path, 'T-0091')), 1)
        rc, out = self.reset(undo=True, why='it did land')
        self.assertEqual(rc, 0, out)
        self.assertEqual(lifecycle.voids(self.path, 'T-0091'), [])
        self.assertEqual(lifecycle.latest(self.path)['coder-t-0091']['harvested'], self.SHA)
        self.assertEqual(lifecycle.occupancy(self.path, alive=lambda pid: False)['landed'],
                         {'T-0091': self.SHA})
        self.assertIn('restored', self.history[-1])
        self.assertEqual(self.reset(undo=True, why='again')[0], 1)  # nothing left to undo

    def test_merge_facts_ignores_the_voided_claim_and_lands_a_new_pr(self):
        from asf.evidence import evidence
        product = env.load_product('sample')
        self.ledger(self.LANDED)
        self.assertEqual(evidence.merge_facts(product, self.path)['code']['T-0091']['pr'], 966)
        self.reset()
        # the host still says #966 merged (its lane record stands): no merge fact for T-0091
        self.assertNotIn('T-0091', evidence.merge_facts(product, self.path)['code'])
        sha2 = 'a1b2c3d' + '0' * 33
        self.ledger({'job': 'coder-t-0091', 'item': 'T-0091', 'kind': 'coder', 'pid': 2,
                     'branch': 'cloud/T-0091', 'started': '2099-01-01T09:00:00Z'},
                    {'job': 'coder-t-0091', 'ended': '2099-01-01T09:30:00Z',
                     'end_reason': 'finished', 'harvested': sha2,
                     'lane': {'state': 'MERGED', 'pr': 1001, 'sha': sha2}})
        self.assertEqual(evidence.merge_facts(product, self.path)['code']['T-0091'],
                         {'sha': sha2, 'branch': 'cloud/T-0091', 'pr': 1001})

    def test_the_same_claim_re_reported_closes_nothing_and_parks_with_the_reason(self):
        from unittest import mock
        from asf.workers import relaunch, trunkclose
        self.ledger(self.LANDED)
        self.reset()
        again = {'job': 'coder-t-0091', 'item': 'T-0091', 'kind': 'coder', 'pid': 3,
                 'branch': 'cloud/T-0091', 'started': '2099-01-01T09:00:00Z',
                 'launch_head': 'f' * 40, 'ended': '2099-01-01T09:10:00Z',
                 'end_reason': 'finished'}
        self.ledger(again)
        text = f'REPORT\nstatus: done\nleft out: already on origin/main at {self.SHA[:7]}\n'
        with mock.patch.object(relaunch, '_result_text', return_value=text), \
                mock.patch.object(trunkclose, 'trunk_sha', return_value=(self.SHA, 'names')), \
                mock.patch.object(trunkclose, 'unlanded', return_value=False), \
                mock.patch.object(trunkclose.landing, 'open_work', return_value=False):
            self.assertIsNone(trunkclose.evidence(self.path, 'T-0091', self.tmp))
        with mock.patch.object(relaunch, '_result_text', return_value=text), \
                mock.patch.object(relaunch, 'on_trunk', return_value=self.SHA), \
                mock.patch.object(relaunch.landing, 'open_work', return_value=False):
            reason, landed = relaunch.assess(self.path, 'coder-t-0091', 'T-0091', head='f' * 40,
                                             repo=self.tmp)
        self.assertEqual(landed, '')
        self.assertIn('claims voided landing 53c00ae', reason)


if __name__ == '__main__':
    unittest.main()
