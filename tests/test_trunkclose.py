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

from asf import env
from asf.evidence import evidence as evidence_mod
from asf.tick import step_wave
from asf.workers import landing, lifecycle, pool as pool_mod, relaunch, trunkclose

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
        self.commit('task(T-0332): the work, landed under its own name')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.product = mock.Mock(repo_dir=self.repo, main='main')
        self.product.name = 'p'
        self.writes = []
        # gh is a fake everywhere here: no open PR names the item, unless a test says otherwise
        patches = [mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path),
                   mock.patch.object(pool_mod, 'update_session', self.update),
                   mock.patch.object(landing, 'open_prs', lambda _r, _i: [])]
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
        self.assertEqual(fields['trunk_arm'], 'names')  # the stamp's `by: trunkclose/names`
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
        # the first park of a new loop also raises one ALARM line (asf.workers.relaunch.alarmed)
        self.assertTrue(any(l.startswith('ALARM loop') for l in lines), lines)
        self.assertTrue(any(l.startswith('parked') for l in lines), lines)

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


class AttributionTests(_Repo):
    """A trunk commit is evidence for the item only when it is the item's: named by it, its PR's
    merge, or covering its ``writes:`` — never merely an ancestor of the trunk (a product's
    T-0042, 2026-09-30: the session named the trunk head its branch had merged in)."""
    JOB, ITEM, BRANCH = 'correct-t-0042', 'T-0042', 'cloud/T-0042'

    def setUp(self):
        super().setUp()
        self.base = self.sha
        self.git('checkout', '-q', '-b', 'work')
        self.commit('task(T-0042): the inspector action')
        self.commit('task(T-0402): the inspector sheet')
        self.git('checkout', '-q', '-')
        self.commit('merge-queue: #977 (cloud/plan-T-0353 @ c364f06b7fa5a54d702a4f82b90f9faba101ac29)')
        self.other = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('checkout', '-q', 'work')
        self.git('-c', 'user.email=a@b', '-c', 'user.name=a', 'merge', '-q', '--no-edit',
                 '--no-ff', self.other)   # main merged into the branch: its second parent
        self.head = self.git('rev-parse', 'HEAD')
        self.git('update-ref', f'refs/remotes/origin/{self.BRANCH}', 'HEAD')
        self.git('checkout', '-q', '-')
        self.assertEqual(self.git('rev-parse', f'{self.head}^2'), self.other)

    def test_t0042_the_trunk_head_merged_into_an_open_branch_is_no_evidence(self):
        text = report('done', f'the work it names is on origin/main at {self.other[:9]}')
        self.run_once(text, head=self.head)
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))
        self.assertFalse(trunkclose.closes_before_launch(self.product, 'coder', self.ITEM,
                                                         lambda _l: None))
        reason, landed = relaunch.assess(self.path, self.JOB, self.ITEM, head=self.head,
                                         card=CARD, repo=self.repo)
        self.assertTrue(reason)
        self.assertEqual(landed, '')
        self.assertNotIn('(verified)', reason)

    def test_t0042_a_needs_input_park_on_the_merged_trunk_head_never_closes(self):
        text = ('NEEDS OPERATOR: none — the work it names is on origin/main at '
                f'{self.other[:9]}' + report('blocked', 'none'))
        self.run_once(text, head=self.head)
        reason, landed = relaunch.assess(self.path, self.JOB, self.ITEM, head=self.head,
                                         card=CARD, repo=self.repo)
        self.assertEqual(landed, '')
        self.assertEqual(relaunch.landed_in(reason), '')
        # a park written before the fix, carrying the false "(verified)" text, stays a park
        old = (f'{self.JOB} launched 1 time(s) — the work it names is on origin/main at '
               f'{self.other[:9]} (verified): close T-0042 on that evidence. Not relaunched')
        self.write(dict(relaunch.park_fields(old, CARD, '2026-09-30T06:00:00Z'), job=self.JOB))
        self.assertEqual(trunkclose.close_parked(self.product, lambda _l: None), [])
        self.assertIn(self.ITEM, lifecycle.corrections(self.path))
        self.assertEqual(self.writes, [])

    def test_another_items_trunk_commit_is_no_evidence_even_with_no_branch_left(self):
        self.git('update-ref', '-d', f'refs/remotes/origin/{self.BRANCH}')
        self.run_once(report('done', f'already on origin/main at {self.other[:9]}'),
                      head=self.head)
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_an_attributable_commit_with_an_open_pr_holding_work_is_no_evidence(self):
        self.git('update-ref', '-d', f'refs/remotes/origin/{self.BRANCH}')
        self.commit('task(T-0042): landed part')
        mine = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.run_once(report('done', f'on origin/main at {mine[:9]}'), head=self.head)
        self.assertEqual(trunkclose.evidence(self.path, self.ITEM, self.repo, ask_gh=False)[0],
                         mine)
        with mock.patch.object(landing, 'open_prs', lambda _r, _i: ['cloud/T-0042-b']):
            self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))

    def test_a_commit_covering_the_writes_footprint_is_evidence(self):
        self.git('update-ref', '-d', f'refs/remotes/origin/{self.BRANCH}')
        os.makedirs(os.path.join(self.repo, 'apps'))
        with open(os.path.join(self.repo, 'apps', 'panel.tsx'), 'w') as f:
            f.write('x')
        self.git('add', '-A')
        self.commit('chore: the sweep')
        sweep = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.run_once(report('done', f'on origin/main at {sweep[:9]}'), head=self.head)
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo, ask_gh=False))
        sha, run, _claim = trunkclose.evidence(self.path, self.ITEM, self.repo,
                                               writes=['apps/panel.tsx'], ask_gh=False)
        self.assertEqual((sha, run['trunk_arm']), (sweep, 'covers'))
        self.assertEqual(landing.attribution(self.repo, 'main', sweep, self.ITEM,
                                             ['apps/panel.tsx']), 'covers')
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo, ask_gh=False,
                                              writes=['apps/panel.tsx', 'apps/other.tsx']))

    def test_a_covers_commit_never_closes_over_the_items_own_pr(self):
        from asf import github
        from asf.facts import cache
        self.git('update-ref', '-d', f'refs/remotes/origin/{self.BRANCH}')
        os.makedirs(os.path.join(self.repo, 'apps'))
        with open(os.path.join(self.repo, 'apps', 'panel.tsx'), 'w') as f:
            f.write('x')
        self.git('add', '-A')
        self.commit('chore: the sweep')
        sweep = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.run_once(report('done', f'on origin/main at {sweep[:9]}'), head=self.head)
        product = mock.Mock(repo_dir=self.repo, main='main', repo_slug='o/r')
        product.name = 'p-covers'

        def host(*prs, ok=True):
            data = [dict(title=f'{self.ITEM}: the panel', headRefName='hand/x', **p) for p in prs]
            return lambda *_a, **_k: github.Result(ok, data if ok else None, 0, '', '', '',
                                                   '' if ok else 'rc 1')

        def ask(answer):
            cache.clear()
            self.addCleanup(cache.clear)
            with mock.patch.object(github, 'prs', answer):
                return trunkclose.evidence(self.path, self.ITEM, self.repo,
                                           writes=['apps/panel.tsx'], product=product)

        closed = dict(number=7, state='CLOSED', mergedAt=None)
        self.assertIsNone(ask(host(closed)))                                # closed unmerged
        self.assertIsNone(ask(host(dict(number=8, state='OPEN', mergedAt=None))))   # still open
        got = ask(host(closed, ok=False))
        self.assertIsInstance(got, trunkclose.Unknown)                      # never on unknown
        self.assertEqual(ask(host())[0], sweep)                             # no PR of its own
        merged = dict(number=9, state='MERGED', mergedAt='2026-09-30T05:00:00Z')
        self.assertEqual(ask(host(closed, merged))[0], sweep)               # one of them merged

    def test_the_items_own_pr_merge_is_evidence(self):
        self.git('update-ref', '-d', f'refs/remotes/origin/{self.BRANCH}')
        self.commit('The prompt inspector (#902)')
        merged = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.write({'job': 'coder-t-0042', 'item': self.ITEM, 'branch': self.BRANCH,
                    'lane': {'pr': 902}, 'started': '2026-09-30T04:00:00Z', 'pid': 1},
                   {'job': 'coder-t-0042', 'ended': '2026-09-30T04:30:00Z',
                    'end_reason': 'finished'})
        self.run_once(report('done', f'on origin/main at {merged[:9]}'), head=self.head)
        sha, run, _claim = trunkclose.evidence(self.path, self.ITEM, self.repo, ask_gh=False)
        self.assertEqual((sha, run['trunk_arm']), (merged, 'pr'))
        self.assertFalse(landing.names(self.repo, 'main', merged, self.ITEM, prs=['903']))
        text = report('done', f'on origin/main at {merged[:9]}')
        self.assertEqual(trunkclose.trunk_sha(self.repo, 'main', {}, text, self.ITEM,
                                              prs=['902']), (merged[:9], 'pr'))
        self.assertEqual(trunkclose.trunk_sha(self.repo, 'main', {}, text, self.ITEM), ('', ''))

    def test_a_report_commit_on_the_trunk_does_not_name_its_item(self):
        self.commit('asf(T-0042): report correct-t-0042\n\nREPORT\nitem: T-0042\n\n'
                    'ASF-Report: correct-t-0042')
        rep = self.git('rev-parse', 'HEAD')
        self.assertFalse(landing.names(self.repo, 'main', rep, self.ITEM))
        self.commit('task(T-0042): the inspector, landed')
        self.assertTrue(landing.names(self.repo, 'main', self.git('rev-parse', 'HEAD'), self.ITEM))



class UnknownTests(_Repo):
    """Unknown never closes: when ``gh`` cannot list the open PRs, the evidence is
    :class:`trunkclose.Unknown` — no close, no launch, the row waits."""

    def setUp(self):
        super().setUp()
        from asf import gh_limit
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)
        self.bin = os.path.join(self.d, 'bin')
        os.makedirs(self.bin)
        p = mock.patch.dict(os.environ, {'PATH': self.bin + os.pathsep + os.environ['PATH']})
        p.start()
        self.addCleanup(p.stop)
        # the real open_prs, over the fake gh on PATH
        p = mock.patch.object(landing, 'open_prs', _REAL_OPEN_PRS)
        p.start()
        self.addCleanup(p.stop)

    def gh(self, rc=0, stdout='', stderr=''):
        path = os.path.join(self.bin, 'gh')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n'
                    f"cat <<'EOF'\n{stdout}\nEOF\n"
                    f"echo {json.dumps(stderr)} >&2\n"
                    f'exit {rc}\n')
        os.chmod(path, 0o755)

    def done(self):
        self.run_once(report('done', f'the whole scope is on origin/main under {self.sha[:9]}'))

    def test_open_prs_reads_heads_and_is_unknown_on_failure_or_a_full_page(self):
        self.gh(stdout=json.dumps([{'number': 1, 'title': 'task(T-0332): x',
                                    'headRefName': 'cloud/T-0332-b'}]))
        self.assertEqual(landing.open_prs(self.repo, self.ITEM), ['cloud/T-0332-b'])
        self.gh(rc=1, stderr='could not resolve host')
        self.assertIsNone(landing.open_prs(self.repo, self.ITEM))
        self.gh(stdout='not json')
        self.assertIsNone(landing.open_prs(self.repo, self.ITEM))
        full = [{'number': n, 'title': 'x', 'headRefName': f'b{n}'} for n in range(300)]
        self.gh(stdout=json.dumps(full))
        self.assertIsNone(landing.open_prs(self.repo, self.ITEM))  # len == limit: maybe cut

    def test_a_failed_gh_makes_a_perfect_report_unknown_not_evidence(self):
        self.done()
        self.gh(rc=1, stderr='could not resolve host')
        hit = trunkclose.evidence(self.path, self.ITEM, self.repo)
        self.assertIsInstance(hit, trunkclose.Unknown)
        self.assertFalse(hit)
        self.assertIsNone(landing.open_work(self.repo, 'main', self.path, self.ITEM))
        self.gh(stdout='[]')
        self.assertEqual(trunkclose.evidence(self.path, self.ITEM, self.repo)[0], self.sha)

    def test_the_row_waits_on_gh_unknown_neither_launched_nor_closed(self):
        self.done()
        self.gh(rc=1, stderr='could not resolve host')
        lines = []
        self.assertTrue(trunkclose.closes_before_launch(self.product, 'coder', self.ITEM,
                                                        lines.append))
        self.assertEqual(self.writes, [])
        self.assertIn(trunkclose.WAITS_UNKNOWN, lines[0])
        self.assertTrue(lines[0].startswith('waits'), lines)

    def test_close_parked_closes_nothing_on_unknown(self):
        self.done()
        reason = (f'reshape launched 1 time(s) — the work it names is on origin/main at '
                  f'{self.sha[:9]} (verified): close T-0332 on that evidence. Not relaunched')
        self.write(dict(relaunch.park_fields(reason, CARD, '2026-09-30T06:00:00Z'), job=self.JOB))
        self.gh(rc=1, stderr='could not resolve host')
        self.assertEqual(trunkclose.close_parked(self.product, lambda _l: None), [])
        self.assertEqual(self.writes, [])
        self.assertIn(self.ITEM, lifecycle.corrections(self.path))

    def test_a_rate_limit_answer_raises_through_close_parked(self):
        from asf import gh_limit
        self.done()
        reason = (f'reshape launched 1 time(s) — the work it names is on origin/main at '
                  f'{self.sha[:9]} (verified): close T-0332 on that evidence. Not relaunched')
        self.write(dict(relaunch.park_fields(reason, CARD, '2026-09-30T06:00:00Z'), job=self.JOB))
        self.gh(rc=1, stderr='API rate limit exceeded for user ID 1.')
        with mock.patch('sys.stderr'), self.assertRaises(gh_limit.RateLimited):
            trunkclose.close_parked(self.product, lambda _l: None)
        self.assertEqual(self.writes, [])


class GitUnknownTests(_Repo):
    """A git call that cannot answer (a timeout) is Unknown and closes nothing (W5-PR2): the
    trunk check, the attribution and the branch check all fail closed, and a landing whose
    attribution git could not answer is neither verified nor unverified this tick (S-m4)."""

    def hang(self, *subcommands):
        """``subprocess.run`` timing out for every ``git <subcommand>`` named (all git calls
        when none is)."""
        real = subprocess.run

        def run(argv, *a, **kw):
            if argv and argv[0] == 'git' and (not subcommands or any(
                    s in argv for s in subcommands)):
                raise subprocess.TimeoutExpired(argv, kw.get('timeout') or 1)
            return real(argv, *a, **kw)
        p = mock.patch('subprocess.run', run)
        p.start()
        self.addCleanup(p.stop)

    def done(self):
        self.run_once(report('done', f'the whole scope is on origin/main under {self.sha[:9]}'))

    def test_a_git_timeout_closes_nothing(self):
        self.done()
        self.hang()
        self.assertIsNone(trunkclose.evidence(self.path, self.ITEM, self.repo))
        self.assertFalse(trunkclose.closes_before_launch(self.product, 'coder', self.ITEM,
                                                         lambda _l: None))
        self.assertEqual(self.writes, [])

    def test_on_trunk_and_attributable_are_unknown_not_false(self):
        self.hang('merge-base')
        self.assertIsNone(landing.on_trunk(self.repo, 'main', self.sha))
        self.assertIsNone(landing.attributable(self.repo, 'main', self.sha, self.ITEM))

    def test_a_naming_read_that_times_out_is_unknown(self):
        self.hang('log')
        self.assertIsNone(landing.attributable(self.repo, 'main', self.sha, self.ITEM))

    def test_an_unreadable_branch_holds_work(self):
        self.hang('rev-parse')
        self.assertTrue(landing.unlanded(self.repo, 'main', self.BRANCH))

    def test_verify_landings_skips_an_item_git_could_not_attribute(self):
        occ = {'landed': {self.ITEM: self.sha}, 'landed_on': {self.ITEM: self.BRANCH}}
        items = {self.ITEM: {'type': 'task', 'state': 'Active'}}
        product = env.Product('p', {'repo_dir': self.repo, 'main': 'main'})
        self.hang('log')
        verified, unverified = landing.verify_landings(product, occ, items, path=self.path)
        self.assertEqual((verified, unverified), ({}, {}))  # no NEEDS DECISION on a timeout


_REAL_OPEN_PRS = landing.open_prs


if __name__ == '__main__':
    unittest.main()
