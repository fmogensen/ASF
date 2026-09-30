"""A Task whose work is already on the trunk is closed on verified evidence, never relaunched
(asf.workers.trunkclose): before a coder/delivery-code/reshape row launches, when the relaunch
cap would park it, and for a park that already carries the evidence."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf.evidence import evidence as evidence_mod
from asf.tick import step_wave
from asf.workers import lifecycle, pool as pool_mod, relaunch, trunkclose

CARD = 'eeda7f0410a1c9a4'
HEAD = 'e0920dc75051f5f2f109068f70bd85d872fb5f9d'


def report(status, left):
    return (f'\n```\nREPORT\nitem: T-0332\nkind: reshape\nstatus: {status}\n'
            f'branch: cloud/T-0332\npushed: none\ncommits: none\ntests: none\n'
            f'left out: {left}\nneeds writes: none\nproves: none\n```')


class _Repo(unittest.TestCase):
    JOB, ITEM, BRANCH = 'coder-t-0332', 'T-0332', 'cloud/T-0332'

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 'sessions.jsonl')
        self.log = os.path.join(self.d, 'log.jsonl')
        self.repo = os.path.join(self.d, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q')
        self.commit('task(T-0331): the work T-0332 also needed')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.product = mock.Mock(repo_dir=self.repo, main='main')
        self.product.name = 'p'
        self.writes = []
        patches = [mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path),
                   mock.patch.object(pool_mod, 'update_session', self.update)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.n = 0

    def git(self, *a):
        return subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, msg):
        self.git('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
                 '-m', msg)

    def update(self, _product, job, **fields):
        self.writes.append((job, fields))
        self.write(dict(fields, job=job))

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def run_once(self, text, job=None, head=HEAD):
        self.n += 1
        job = job or self.JOB
        self.write({'job': job, 'item': self.ITEM, 'kind': job.rsplit('-', 2)[0],
                    'branch': self.BRANCH, 'pid': 100 + self.n, 'log': self.log,
                    'started': f'2026-09-30T05:{self.n:02d}:00Z', 'card_digest': CARD,
                    'launch_head': head})
        with open(self.log, 'a') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'result': text}) + '\n')
        self.write({'job': job, 'ended': f'2026-09-30T05:{self.n:02d}:30Z',
                    'end_reason': 'finished'})


class EvidenceTests(_Repo):

    def test_a_done_report_naming_a_trunk_commit_is_evidence(self):
        self.run_once(report('done', f'the whole scope is on origin/main under {self.sha[:9]}'))
        sha, run, claim = trunkclose.evidence(self.path, self.ITEM, self.repo)
        self.assertEqual(sha, self.sha)  # spelled in full: the ingest's merge fact wants it
        self.assertEqual(run['job'], self.JOB)
        self.assertTrue(claim.startswith('status done'))

    def test_a_partial_or_blocked_report_is_no_evidence(self):
        self.run_once(report('partial', f'on origin/main under {self.sha[:9]}'))
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))
        self.run_once(report('blocked', f'on origin/main under {self.sha[:9]}'))
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_a_sha_not_on_the_trunk_is_no_evidence(self):
        self.run_once(report('done', 'on origin/main under 1234567abc'))
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_a_branch_with_work_past_the_trunk_is_no_evidence(self):
        self.git('checkout', '-q', '-b', 'work')
        self.commit('task(T-0332): not landed')
        self.git('update-ref', f'refs/remotes/origin/{self.BRANCH}', 'HEAD')
        self.run_once(report('done', f'on origin/main under {self.sha[:9]}'))
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_the_runs_own_output_proves_nothing(self):
        # a reshape whose split landed: the sha is its own commit, named in commits:
        text = report('done', f'the split landed as {self.sha[:9]}').replace(
            'commits: none', f'commits: {self.sha[:9]} task(T-0332): split into parts')
        self.run_once(text)
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_a_commit_the_run_pushed_proves_nothing(self):
        launch = self.sha
        self.commit('task(T-0332): the split, part one')
        mine = self.git('rev-parse', 'HEAD')
        self.commit('task(T-0332): the split, part two')
        head = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        text = report('done', f'part one landed as {mine[:9]}').replace(
            'pushed: none', f'pushed: yes {head}')
        self.run_once(text, head=launch)
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_the_trunks_merge_of_the_runs_own_branch_proves_nothing(self):
        self.commit(f'merge-queue: #956 ({self.BRANCH} @ 99b891a1a)')
        merge = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.run_once(report('done', f'the split landed (merge-queue {merge[:9]})'))
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_a_commit_subject_naming_the_evidence_does_not_hide_it(self):
        text = report('done', f'already on origin/main in {self.sha[:9]}').replace(
            'commits: none', f'commits: e0920dc75 task(T-0332): already landed in {self.sha[:9]}')
        self.run_once(text)
        self.assertEqual(trunkclose.evidence(self.path, self.ITEM, self.repo)[0], self.sha)

    def test_the_launch_head_proves_nothing(self):
        self.run_once(report('done', f'rebased on {self.sha[:9]}'), head=self.sha)
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))


class BeforeLaunchTests(_Repo):

    def test_a_coder_row_is_closed_not_launched(self):
        self.run_once(report('done', f'already on origin/main under {self.sha[:9]}'))
        lines = []
        self.assertTrue(trunkclose.closes_before_launch(self.product, 'coder', self.ITEM,
                                                        lines.append))
        job, fields = self.writes[-1]
        self.assertEqual(job, self.JOB)
        self.assertEqual(fields['harvested'], self.sha)
        self.assertIn('(verified)', fields['trunk_closed'])
        self.assertIsNone(fields['correction'])
        self.assertTrue(lines[0].startswith('closed   coder-t-0332'), lines)
        # closed once: the landed run is no new evidence
        self.assertFalse(trunkclose.closes_before_launch(self.product, 'coder', self.ITEM,
                                                         lines.append))

    def test_dry_run_writes_nothing_and_other_kinds_are_not_asked(self):
        self.run_once(report('done', f'already on origin/main under {self.sha[:9]}'))
        lines = []
        self.assertTrue(trunkclose.closes_before_launch(self.product, 'reshape', self.ITEM,
                                                        lines.append, dry_run=True))
        self.assertTrue(lines[0].startswith('would close'), lines)
        self.assertFalse(trunkclose.closes_before_launch(self.product, 'review', self.ITEM,
                                                         lines.append))
        self.assertEqual(self.writes, [])

    def test_no_evidence_launches(self):
        self.run_once(report('partial', 'more to do'))
        self.assertFalse(trunkclose.closes_before_launch(self.product, 'coder', self.ITEM,
                                                         lambda _l: None))

    def test_the_closed_run_is_the_items_merge_fact_whatever_its_lane(self):
        self.write({'job': 'reshape-t-0332', 'item': self.ITEM, 'kind': 'reshape',
                    'branch': 'cloud/plan-T-0332', 'pid': 1, 'started': '2026-09-30T05:00:00Z'},
                   {'job': 'reshape-t-0332', 'ended': '2026-09-30T05:01:00Z',
                    'end_reason': 'finished'})
        trunkclose.close(self.product, 'reshape-t-0332', self.sha, 'verified')
        product = mock.Mock(conventions=mock.Mock())
        with mock.patch.object(evidence_mod, 'branch_prefixes',
                               lambda _p: {'code': 'cloud/', 'fix': 'cloud/fix-',
                                           'spec': 'cloud/spec-', 'plan': 'cloud/plan-',
                                           'legacy': []}):
            facts = evidence_mod.merge_facts(product, path=self.path)
        self.assertEqual(facts['code'][self.ITEM]['sha'], self.sha)
        self.assertNotIn(self.ITEM, facts['docs'])


class ParkClosesTests(_Repo):
    JOB = 'reshape-t-0332'

    def test_the_cap_closes_instead_of_parking_on_verified_evidence(self):
        self.run_once(report('done', f'on origin/main under {self.sha[:9]}'))
        row = mock.Mock(kind='RESHAPE → PLAN', correction='')
        wrow = pool_mod.Row(self.JOB, self.ITEM, branch=self.BRANCH, card_digest=CARD)
        lines = []
        # the terminal shortcut needs the branch head known; the wave reads it off origin
        reason, landed = relaunch.assess(self.path, self.JOB, self.ITEM, head=HEAD, card=CARD,
                                         repo=self.repo)
        self.assertTrue(reason)
        self.assertEqual(landed, self.sha[:9])
        self.writes.clear()
        with mock.patch.object(relaunch, 'assess', lambda *a, **k: (reason, landed)):
            self.assertTrue(step_wave.relaunch_capped(self.product, row, wrow, lines.append))
        job, fields = self.writes[-1]
        self.assertEqual((job, fields['harvested']), (self.JOB, self.sha))
        self.assertIsNone(fields['correction'])
        self.assertTrue(lines[0].startswith('closed   reshape-t-0332'), lines)
        self.assertIn('landed: ', lines[0])

    def test_a_park_without_trunk_evidence_still_parks(self):
        self.run_once(report('done', 'nothing more'))
        self.run_once(report('done', 'nothing more'))
        row = mock.Mock(kind='RESHAPE → PLAN', correction='')
        wrow = pool_mod.Row(self.JOB, self.ITEM, branch=self.BRANCH, card_digest=CARD)
        lines = []
        self.assertTrue(step_wave.relaunch_capped(self.product, row, wrow, lines.append))
        self.assertTrue(self.writes[-1][1]['correction']['parked'])
        self.assertTrue(lines[0].startswith('parked'))

    def test_a_standing_park_with_verified_evidence_closes_its_card(self):
        self.run_once(report('done', f'on origin/main under {self.sha[:9]}'))
        reason = (f'reshape launched 1 time(s) — the work it names is on origin/main at '
                  f'{self.sha[:9]} (verified): close T-0332 on that evidence. Not relaunched')
        self.write(dict(relaunch.park_fields(reason, CARD, '2026-09-30T06:00:00Z'), job=self.JOB))
        self.assertIn(self.ITEM, lifecycle.corrections(self.path))
        lines = []
        self.assertEqual(trunkclose.close_parked(self.product, lines.append, dry_run=True),
                         [self.ITEM])
        self.assertEqual(self.writes, [])
        self.assertEqual(trunkclose.close_parked(self.product, lines.append), [self.ITEM])
        self.assertEqual(self.writes[-1][1]['harvested'], self.sha)
        self.assertEqual(lifecycle.corrections(self.path), {})
        self.assertIn(f'landed: {self.sha[:9]}', lines[-1])

    def test_a_park_whose_sha_is_not_on_the_trunk_stays(self):
        self.run_once(report('done', 'x'))
        reason = 'the work it names is on origin/main at 1234567ab (verified): close'
        self.write(dict(relaunch.park_fields(reason, CARD, '2026-09-30T06:00:00Z'), job=self.JOB))
        self.assertEqual(trunkclose.close_parked(self.product, lambda _l: None), [])
        self.assertIn(self.ITEM, lifecycle.corrections(self.path))


if __name__ == '__main__':
    unittest.main()
