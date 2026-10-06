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
from asf.workers import landing, lifecycle, pool as pool_mod, relaunch

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


class HeadCapTests(_Ledger):
    """A product's ``correct-t-0042`` (2026-09-30): four correction rounds on head ``3fe323b``
    in 2h40m. Each round's cause was its correction text, and the text named the review file
    of the moment (``7-t-0042.md``, then ``8-t-0042.md``, then the red checks): a review filed
    between rounds on the same head wrote a new cause, and the (job, item, cause) streak broke
    every time. A head that did not move takes at most :data:`relaunch.CAP` correction rounds."""
    JOB, ITEM = 'correct-t-0042', 'T-0042'
    PARTIAL = report('partial')

    def test_a_third_correction_on_one_head_parks_whatever_the_cause(self):
        self.run_once(self.PARTIAL, cause='7b35ed73')
        self.assertIsNone(self.verdict(head=HEAD, cause='a52059b4'))
        self.run_once(self.PARTIAL, cause='a52059b4')
        why = self.verdict(head=HEAD, cause='b0d61586')
        self.assertIn('launched 2 time(s) on e0920dc75 without a new commit', why)
        self.assertIn('a52059b4, 7b35ed73', why)

    def test_a_review_between_rounds_does_not_reset_it(self):
        self.run_once(self.PARTIAL, cause='7b35ed73')
        self.run_once(report('done'), job='review-t-0042', cause='bf60facf')
        self.run_once(self.PARTIAL, cause='a52059b4')
        self.assertIsNotNone(self.verdict(head=HEAD, cause='b0d61586'))

    def test_a_new_commit_a_card_edit_or_an_unpark_buys_a_round(self):
        self.run_once(self.PARTIAL, cause='7b35ed73')
        self.run_once(self.PARTIAL, cause='a52059b4')
        self.assertIsNone(self.verdict(head='f' * 40, cause='b0d61586'))
        self.assertIsNone(self.verdict(head=HEAD, card='79176bdca68333ee', cause='b0d61586'))
        self.write({'job': self.JOB, 'unparked': '2026-09-30T05:59:00Z'})
        self.assertIsNone(self.verdict(head=HEAD, cause='b0d61586'))

    def test_another_kind_keeps_the_cause_streak(self):
        for cause in ('c1', 'c2'):
            self.run_once(report('partial'), job='review-t-0042', cause=cause)
        self.assertIsNone(relaunch.verdict(self.path, 'review-t-0042', self.ITEM, head=HEAD,
                                           card=CARD, cause='c3'))


class StatusParkedTests(_Ledger):

    def test_status_names_a_parked_item_and_is_silent_without_one(self):
        from asf.views import status
        with mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path):
            self.run_once(report('done'))
            self.assertIsNone(status.parked_cell(None))
            self.write(dict(relaunch.park_fields('looped twice', CARD, '2026-09-30T06:00:00Z'),
                            job=self.JOB))
            cell = status.parked_cell(None)
        self.assertIn(f'T-0332 [relaunch cap on job {self.JOB}]: looped twice', cell)
        self.assertTrue(cell.startswith('1 — '))


class TrunkEvidenceTests(_Ledger):

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.d, 'repo')
        os.makedirs(self.repo)
        git = lambda *a: subprocess.run(['git', *a], cwd=self.repo, check=True,  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
            '-m', 'task(T-0332): the work')
        self.sha = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        # gh is a fake here: no open PR names the item, unless a test says otherwise
        p = mock.patch.object(landing, 'open_prs', lambda _r, _i: [])
        p.start()
        self.addCleanup(p.stop)

    def test_an_already_on_main_claim_is_verified_against_git(self):
        self.run_once(report('done', f'the split — on origin/main under {self.sha[:9]}'))
        why = self.verdict(head=HEAD, repo=self.repo)
        self.assertIn(f'on origin/main at {self.sha[:9]} (verified)', why)
        self.assertIn('close T-0332', why)

    def test_an_open_pr_of_the_item_is_no_landing_and_an_unread_host_no_decision(self):
        self.run_once(report('done', f'the split — on origin/main under {self.sha[:9]}'))
        with mock.patch.object(landing, 'open_prs', lambda _r, _i: ['cloud/T-0332-b']):
            reason, landed = relaunch.assess(self.path, self.JOB, 'T-0332', head=HEAD,
                                             card=CARD, repo=self.repo)
        self.assertEqual(landed, '')
        self.assertNotIn('(verified)', reason)
        with mock.patch.object(landing, 'open_prs', lambda _r, _i: None):
            reason, landed = relaunch.assess(self.path, self.JOB, 'T-0332', head=HEAD,
                                             card=CARD, repo=self.repo)
        self.assertIsInstance(landed, landing.Unknown)
        self.assertNotIn('(verified)', reason)

    def test_a_trunk_commit_of_another_item_is_no_evidence(self):
        git = lambda *a: subprocess.run(['git', *a], cwd=self.repo, check=True,  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
        git('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
            '-m', 'merge-queue: #977 (cloud/plan-T-0353 @ c364f06b7)')
        other = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.run_once(report('done', f'the split — on origin/main under {other[:9]}'))
        self.assertNotIn('verified', self.verdict(head=HEAD, repo=self.repo))

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
        self.assertTrue(any(l.startswith('parked   reshape-t-0332') for l in lines), lines)
        self.assertTrue(any(l.startswith('ALARM loop') for l in lines), lines)
        self.assertTrue(writes[-1][1]['correction']['parked'])
        self.assertTrue(wrow.cause)

    def test_the_wave_launches_a_first_row(self):
        product = mock.Mock(repo_dir=None, main='main')
        row = mock.Mock(kind='RESHAPE → PLAN', correction='')
        wrow = pool_mod.Row(self.JOB, self.ITEM, branch='cloud/plan-T-0332', card_digest=CARD)
        with mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path):
            self.assertFalse(step_wave.relaunch_capped(product, row, wrow, lambda _l: None))


class LoopGuardTests(_Ledger):
    """``relaunch.loop_guard``: the job's runs since the item's latest unpark, read off the
    ledger (:mod:`asf.workers.loops`'s two rules, over the real ledger this time). Each prior
    run's own ``report_key`` is stamped by hand here, exactly as :func:`asf.tick.step_wave.
    _stamp_report_key` would have left it on the ledger before the next launch — the shared
    per-job log in :class:`_Ledger` only ever holds the newest run's own result (loop_guard
    reads an older run off the ledger field, never its log, by the same rule the module's own
    docstring states)."""

    GUARD = {'same_report': 2, 'daily_cap': 6}

    def run_with_key(self, text, **kw):
        from asf.workers import loops
        self.run_once(text, **kw)
        self.write({'job': self.JOB, 'report_key': loops.report_key(text)})

    def test_two_runs_on_the_same_report_park_the_third(self):
        self.run_with_key(report('done', 'nothing more'))
        self.run_once(report('done', 'nothing more'))
        reason = relaunch.loop_guard(self.path, self.JOB, self.ITEM, card=CARD, guard=self.GUARD)
        self.assertIsNotNone(reason)
        self.assertIn('same report', reason)
        self.assertIn(self.ITEM, reason)

    def test_a_different_report_each_time_does_not_park(self):
        self.run_with_key(report('done', 'the first thing'))
        self.run_once(report('done', 'a second, different thing'))
        reason = relaunch.loop_guard(self.path, self.JOB, self.ITEM, card=CARD, guard=self.GUARD)
        self.assertIsNone(reason)

    def test_a_card_change_does_not_park(self):
        self.run_with_key(report('done', 'nothing more'), card='cardAAAAAAAAAAAA')
        self.run_once(report('done', 'nothing more'), card='cardBBBBBBBBBBBB')
        reason = relaunch.loop_guard(self.path, self.JOB, self.ITEM, card='cardBBBBBBBBBBBB',
                                     guard=self.GUARD)
        self.assertIsNone(reason)

    def test_no_item_is_never_parked(self):
        self.assertIsNone(relaunch.loop_guard(self.path, self.JOB, '', card=CARD,
                                              guard=self.GUARD))

    def test_an_unpark_resets_the_streak(self):
        self.run_with_key(report('done', 'nothing more'))
        self.run_once(report('done', 'nothing more'))
        self.write({'job': self.JOB, 'item': self.ITEM, 'unparked': '2026-09-30T05:10:00Z'})
        reason = relaunch.loop_guard(self.path, self.JOB, self.ITEM, card=CARD, guard=self.GUARD)
        self.assertIsNone(reason)

    def test_the_six_launches_in_24h_cap_parks_even_with_different_reports(self):
        from asf.workers import loops
        for i in range(5):
            self.run_with_key(report('done', f'thing number {i}'))
        self.run_once(report('done', 'thing number 5'))
        now = loops._ts('2026-09-30T05:10:00Z')
        reason = relaunch.loop_guard(self.path, self.JOB, self.ITEM, card=CARD, now=now,
                                     guard={'same_report': None, 'daily_cap': 6})
        self.assertIsNotNone(reason)
        self.assertIn('daily cap', reason)


class AlarmDedupeTests(_Ledger):
    """``relaunch.loop_key``/``relaunch.alarmed``: one alarm per loop, not one per re-park."""

    def test_counts_and_shas_do_not_change_the_key(self):
        a = relaunch.loop_key(f'reshape launched 2 time(s) on {HEAD[:9]} — close T-0332')
        b = relaunch.loop_key(f'reshape launched 7 time(s) on {"a" * 9} — close T-0332')
        self.assertEqual(a, b)

    def test_a_different_reason_is_a_different_key(self):
        a = relaunch.loop_key('reshape launched 2 time(s) — the same report twice')
        b = relaunch.loop_key('reshape launched 2 time(s) — the daily cap of 6')
        self.assertNotEqual(a, b)

    def test_not_alarmed_with_no_prior_park(self):
        self.assertFalse(relaunch.alarmed(self.path, self.JOB, self.ITEM, 'somekey'))

    def test_alarmed_once_a_park_already_carried_the_key(self):
        self.write({'job': self.JOB, 'item': self.ITEM,
                   'correction': {'loop_key': 'somekey', 'at': '2026-09-30T05:00:00Z'}})
        self.assertTrue(relaunch.alarmed(self.path, self.JOB, self.ITEM, 'somekey'))
        self.assertFalse(relaunch.alarmed(self.path, self.JOB, self.ITEM, 'otherkey'))

    def test_an_unpark_resets_the_alarm(self):
        self.write({'job': self.JOB, 'item': self.ITEM,
                   'correction': {'loop_key': 'somekey', 'at': '2026-09-30T05:00:00Z'}})
        self.write({'job': self.JOB, 'item': self.ITEM, 'unparked': '2026-09-30T05:30:00Z'})
        self.assertFalse(relaunch.alarmed(self.path, self.JOB, self.ITEM, 'somekey'))

    def test_park_fields_carry_the_loop_key(self):
        fields = relaunch.park_fields('reshape launched 2 time(s) on abc1234', CARD, '2026-09-30T05:00:00Z')
        self.assertEqual(fields['correction']['loop_key'],
                         relaunch.loop_key('reshape launched 2 time(s) on abc1234'))


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
