"""The wave's relaunch cap (asf.workers.relaunch) and the per-launch ledger ingest.

Reproduces a product's two loops of 2026-09-29/30: ``reshape-t-0332`` launched 76 times and
``delivery-code-t-0042`` 69 times, every run on the same head and card, every report saying the
work was already on the trunk or needed a person — and the record holding 2 rows for them."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf.metrics import metrics
from asf.tick import step_wave
from asf.workers import lifecycle, pool as pool_mod, relaunch

HEAD = 'e0920dc75051f5f2f109068f70bd85d872fb5f9d'
CARD = 'eeda7f0410a1c9a4'


def report(status, left='none', extra=''):
    return (f'{extra}\n```\nREPORT\nitem: T-0332\nkind: reshape\nstatus: {status}\n'
            f'branch: cloud/plan-T-0332\npushed: yes {HEAD}\ncommits: none\ntests: none\n'
            f'left out: {left}\nneeds writes: none\nproves: none\n```')


class _Ledger(unittest.TestCase):
    JOB, ITEM = 'reshape-t-0332', 'T-0332'

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 'sessions.jsonl')
        self.log = os.path.join(self.d, f'{self.JOB}.jsonl')
        self.n = 0

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def run_once(self, text, head=HEAD, card=CARD, cause='', job=None, usd=0.4):
        """One launch line, its log run and its end line."""
        self.n += 1
        job = job or self.JOB
        rec = {'job': job, 'item': self.ITEM, 'kind': job.rsplit('-', 2)[0],
               'branch': 'cloud/plan-T-0332', 'pid': 100 + self.n, 'log': self.log,
               'started': f'2026-09-30T05:{self.n:02d}:00Z', 'card_digest': card,
               'launch_head': head}
        if cause:
            rec['cause'] = cause
        self.write(rec)
        with open(self.log, 'a') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'result': text,
                                'total_cost_usd': usd, 'duration_ms': 390000}) + '\n')
        self.write({'job': job, 'ended': f'2026-09-30T05:{self.n:02d}:30Z',
                    'end_reason': 'finished'})

    def verdict(self, head=None, card=CARD, cause='', repo=None):
        return relaunch.verdict(self.path, self.JOB, self.ITEM, head=head, card=card,
                                cause=cause, repo=repo)


class RelaunchCapTests(_Ledger):

    def test_the_reshape_loop_parks_at_the_second_launch_on_one_state(self):
        # T-0332: the branch is deleted after each "landed already on main" — head unknown
        self.run_once(report('done', 'the split itself — already on origin/main'))
        self.assertIsNone(self.verdict(head=None))
        self.run_once(report('done', 'the split itself — already on origin/main'))
        why = self.verdict(head=None)
        self.assertIn('launched 2 time(s)', why)
        self.assertIn('asf unpark T-0332', why)

    def test_a_terminal_report_on_an_unmoved_head_parks_at_one(self):
        # T-0042: "delivery was already complete and pushed", the branch still on its head
        self.run_once(report('done', 'delivery already complete'))
        why = self.verdict(head=HEAD)
        self.assertIn('status done', why)

    def test_needs_input_parks_at_one(self):
        self.run_once(report('partial', extra='NEEDS OPERATOR: confirm the stale writes: line'))
        self.assertIn('needs input — confirm the stale writes', self.verdict(head=HEAD))

    def test_a_partial_report_gets_one_more_run(self):
        self.run_once(report('partial', 'half the Tasks'))
        self.assertIsNone(self.verdict(head=HEAD))
        self.run_once(report('partial', 'half the Tasks'))
        self.assertIsNotNone(self.verdict(head=HEAD))

    def test_a_new_commit_is_a_state_change(self):
        self.run_once(report('done'))
        self.run_once(report('done'))
        self.assertIsNone(self.verdict(head='f' * 40))

    def test_a_card_edit_is_a_state_change(self):
        self.run_once(report('done'))
        self.run_once(report('done'))
        self.assertIsNone(self.verdict(head=HEAD, card='0123456789abcdef'))

    def test_a_new_cause_is_a_state_change(self):
        a, b = relaunch.cause_key('FIX → CORRECT', 'gate red'), relaunch.cause_key(
            'FIX → CORRECT', 'review: changes requested')
        self.run_once(report('partial'), cause=a)
        self.run_once(report('partial'), cause=a)
        self.assertIsNotNone(self.verdict(head=HEAD, cause=a))
        self.assertIsNone(self.verdict(head=HEAD, cause=b))

    def test_runs_on_different_heads_are_no_streak_even_with_the_head_unknown(self):
        self.run_once(report('partial'), head='a' * 40)
        self.run_once(report('partial'), head='b' * 40)
        self.assertIsNone(self.verdict(head=None))

    def test_an_unpark_starts_the_count_again(self):
        self.run_once(report('partial'))
        self.run_once(report('partial'))
        self.write({'job': self.JOB, 'unparked': '2026-09-30T05:59:00Z'})
        self.assertIsNone(self.verdict(head=HEAD))

    def test_a_spent_window_is_not_a_launch(self):
        self.run_once(report('partial'))
        self.write({'job': self.JOB, 'end_reason': lifecycle.QUOTA_EXHAUSTED_REASON})
        self.assertIsNone(self.verdict(head=HEAD))

    def test_the_park_is_a_pending_parked_correction_the_feeder_reads(self):
        self.run_once(report('done'))
        self.run_once(report('done'))
        why = self.verdict(head=None)
        self.write(dict(relaunch.park_fields(why, CARD, '2026-09-30T06:00:00Z'), job=self.JOB))
        corr = lifecycle.corrections(self.path)[self.ITEM]
        self.assertTrue(corr['parked'])
        self.assertEqual(corr['kind'], lifecycle.RELAUNCH_CAP)
        from asf.feeder import rows
        product = mock.Mock(conventions=mock.Mock(branch_kind=lambda b: 'task'))
        items = {self.ITEM: {'id': self.ITEM, 'type': 'task', 'state': 'Active',
                             'title': 't', 'writes': ['a.ts']}}
        with mock.patch.object(rows, 'console_amend_row', lambda *a, **k: None):
            out, ids = rows.correction_rows(items, product, set(), {self.ITEM: corr})
        self.assertEqual(ids, {self.ITEM})
        self.assertTrue(out[0].action.startswith(rows.PARKED))
        self.assertFalse(out[0].launches)

    def test_a_park_outlives_its_closed_pr(self):
        # T-0042's PR #902 closed unmerged: a park on that branch must still hold the row
        self.run_once(report('done'))
        self.write({'job': self.JOB, 'lane': {'state': 'PR_OPEN', 'pr': 902, 'head': HEAD}})
        self.write(dict(relaunch.park_fields('looped', CARD, '2026-09-30T06:00:00Z'),
                        job=self.JOB))
        occ = lifecycle.occupancy(self.path, ended={902: {'state': 'CLOSED', 'head': HEAD}})
        self.assertTrue(occ['corrections'][self.ITEM]['parked'])


class TrunkEvidenceTests(_Ledger):

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.d, 'repo')
        os.makedirs(self.repo)
        git = lambda *a: subprocess.run(['git', *a], cwd=self.repo, check=True,  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
            '-m', 'ci: the work')
        self.sha = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def test_an_already_on_main_claim_is_verified_against_git(self):
        self.run_once(report('done', f'the split — on origin/main under {self.sha[:9]}'))
        why = self.verdict(head=HEAD, repo=self.repo)
        self.assertIn(f'on origin/main at {self.sha[:9]} (verified)', why)
        self.assertIn('close T-0332', why)

    def test_a_sha_not_on_main_is_no_evidence(self):
        self.run_once(report('done', 'the split — on origin/main under 1234567abc'))
        self.assertNotIn('verified', self.verdict(head=HEAD, repo=self.repo))


class WaveCapTests(_Ledger):
    """The wave parks the row instead of launching it, and says so."""

    def test_the_wave_parks_a_looping_row(self):
        self.run_once(report('done'))
        self.run_once(report('done'))
        product = mock.Mock(repo_dir=None, main='main')
        product.name = 'p'
        row = mock.Mock(kind='RESHAPE → PLAN', correction='')
        wrow = pool_mod.Row(self.JOB, self.ITEM, branch='cloud/plan-T-0332', card_digest=CARD)
        lines = []
        writes = []
        with mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path), \
                mock.patch.object(pool_mod, 'update_session',
                                  lambda _p, job, **f: writes.append((job, f))):
            self.assertTrue(step_wave.relaunch_capped(product, row, wrow, lines.append))
        self.assertTrue(lines[0].startswith('parked   reshape-t-0332'))
        self.assertTrue(writes[0][1]['correction']['parked'])
        self.assertTrue(wrow.cause)

    def test_the_wave_launches_a_first_row(self):
        product = mock.Mock(repo_dir=None, main='main')
        row = mock.Mock(kind='RESHAPE → PLAN', correction='')
        wrow = pool_mod.Row(self.JOB, self.ITEM, branch='cloud/plan-T-0332', card_digest=CARD)
        with mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path):
            self.assertFalse(step_wave.relaunch_capped(product, row, wrow, lambda _l: None))


class PerLaunchIngestTests(_Ledger):
    """Every launch is one `sessions` event: 3 launches of one job are 3 rows, each its own cost."""

    def test_each_launch_of_a_job_is_its_own_event(self):
        for usd in (0.1, 0.2, 0.3):
            self.run_once(report('done'), usd=usd)
        evs = metrics.sessions_from_registry(None, state_path=self.path, logs_dir=self.d)
        evs = [e for e in evs if e['task'] == self.JOB]
        self.assertEqual(len(evs), 3)
        self.assertEqual([e['usd'] for e in evs], [0.1, 0.2, 0.3])
        self.assertEqual(len({metrics.natural_key('sessions', e) for e in evs}), 3)

    def test_a_live_run_is_not_an_event_yet(self):
        self.run_once(report('done'))
        self.write({'job': self.JOB, 'item': self.ITEM, 'pid': 9, 'log': self.log,
                    'started': '2026-09-30T06:00:00Z'})
        evs = metrics.sessions_from_registry(None, state_path=self.path, logs_dir=self.d)
        evs = [e for e in evs if e['task'] == self.JOB]
        self.assertEqual([e['usd'] for e in evs], [0.4])


if __name__ == '__main__':
    unittest.main()
