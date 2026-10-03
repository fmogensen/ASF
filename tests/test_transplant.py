"""The lane's transplant (:mod:`asf.harvest.transplant`, :meth:`asf.harvest.lane.Lane.transplant`).

2026-09-25..29, a product's T-0338 and T-0349: ~35 adjudicate/correct sessions on two items
whose every review approved the CONTENT — the holds were git mechanics only (trunk history, a
rebase conflicting in a generated doc, the review files and ``.files-block.json`` riding along).
The lane now moves approved content onto a fresh trunk itself: no session, the approval carried
only when the diff is the approved one. Hermetic: a bare origin and checkouts in a temp dir.
"""
import json
import os
import unittest

from asf.harvest import lane, transplant
from asf.workers import lifecycle
from tests.test_lane import LaneFixture, sh

B = 'worker/T-0001'
ITEM = 'T-0001'
ITEMS = {ITEM: {'id': ITEM, 'type': 'task', 'state': 'Active'}}
AUTHOR = {'GIT_AUTHOR_NAME': 'Ada', 'GIT_AUTHOR_EMAIL': 'ada@x', 'GIT_COMMITTER_NAME': 'Ada',
          'GIT_COMMITTER_EMAIL': 'ada@x'}
REPORT = '# Report (generated)\n\nDo not edit by hand.\nrows: {}\n'
APPROVED = 'verdict: approved\nhead: {head}\n\n## C\nNone.\n'
OPEN_C = ('verdict: approved\nhead: {head}\n\n## C\n1. `a.txt:1` — the door opens inward; it '
          'must open outward\n')
CHANGES = 'verdict: changes requested\nhead: {head}\n\n## C\n1. `a.txt:1` — wrong door\n'
A0 = 'a0\n1\n2\n3\n4\n5\n'
A1 = 'a1\n1\n2\n3\n4\n5\n'
MECHANICAL = {'kind': 'rebase conflict', 'text': 'rebase conflicts in: gen/report.md'}


class Transplant(LaneFixture):

    def setUp(self):
        super().setUp()
        self.lines = []
        self.push_main({'a.txt': A0, 'gen/report.md': REPORT.format(1)}, 'chore: base')

    # ---- the case ---------------------------------------------------------------------------

    def commit(self, subject, files):
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=AUTHOR)
        return sh(['git', 'rev-parse', 'HEAD'], cwd=self.worker).stdout.strip()

    def approved_branch(self, review=APPROVED):
        """The Task's commit H (its own files, a generated report and a factory artifact), a
        review of H on top, pushed; then the trunk moves the generated report on."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', B, 'origin/main'], cwd=self.worker)
        h = self.commit(f'task({ITEM}): the door', {
            'a.txt': A1, 'b.txt': 'b\n', 'gen/report.md': REPORT.format(2),
            '.files-block.json': '{"modify": ["a.txt"]}\n'})
        self.commit(f'review({ITEM}): round 1', {'reviews/1-t-0001.md': review.format(head=h)})
        sh(['git', 'push', '-q', '-f', 'origin', B], cwd=self.worker)
        self.push_main({'gen/report.md': REPORT.format(5), 'z.txt': 'z\n'}, 'feat: z (#9)')
        return h

    def held(self, corr=MECHANICAL, state=lane.BACK, reason='kind=rebase conflict', live=False):
        """The item's correct run, ended and held with ``corr`` — the lane at ``state``."""
        tip = self.tip()
        run = {'job': 'correct-t-0001', 'item': ITEM, 'branch': B, 'kind': 'correct',
               'pid': 999999, 'started': '2026-09-21T00:00:00Z'}
        if not live:
            run.update(ended='2026-09-21T00:05:00Z', end_reason='finished', rc=0)
        run['lane'] = {'state': state, 'head': tip, 'pr': None, 'at': '2026-09-21T00:06:00Z',
                       'reason': reason, 'item': ITEM}
        if corr:
            run['correction'] = dict(corr, at='2026-09-21T00:06:00Z')
        with open(self.sessions(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(run) + '\n')
        return tip

    def sessions(self):
        return os.path.join(self.state_dir, 'sessions.jsonl')

    def tip(self):
        return sh(['git', 'rev-parse', B], cwd=self.origin).stdout.strip()

    def show(self, rev, path):
        r = sh(['git', 'show', f'{rev}:{path}'], cwd=self.origin)
        return r.stdout if r.returncode == 0 else None

    def reviewed(self, **conv):
        return self.product(lane={'review': {'code': 'required'}}, **conv)

    def run_pass(self, product=None):
        lane.lane_pass(product or self.reviewed(), self.state_dir, items=ITEMS,
                       out=self.lines.append)

    # ---- the tests --------------------------------------------------------------------------

    def test_approved_content_held_by_mechanics_is_transplanted_with_no_session(self):
        h = self.approved_branch()
        old = self.held()
        trunk = self.origin_main()
        self.run_pass()
        new = self.tip()
        self.assertNotEqual(new, old)
        # one commit on the fresh trunk, the approved files, no artifact, the trunk's report
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~1'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual((self.show(new, 'a.txt'), self.show(new, 'b.txt')), (A1, 'b\n'))
        self.assertEqual(self.show(new, 'gen/report.md'), REPORT.format(5))
        self.assertIsNone(self.show(new, '.files-block.json'))
        self.assertIsNone(self.show(new, 'reviews/1-t-0001.md'))
        msg = sh(['git', 'log', '-1', '--format=%B', new], cwd=self.origin).stdout
        self.assertTrue(msg.startswith(f'task({ITEM}): the door'), msg)
        self.assertIn(f'{transplant.TRAILER}: {h}', msg)
        self.assertEqual(sh(['git', 'log', '-1', '--format=%an', new], cwd=self.origin)
                         .stdout.strip(), 'Ada')
        # the old tip archived, no correction left for a session, the approval carried
        self.assertEqual(sh(['git', 'rev-parse', f'archive/{B}-transplant-{old[:9]}'],
                            cwd=self.origin).stdout.strip(), old)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        rec = self.lane_of(B)
        self.assertEqual((rec['state'], rec['head']), (lane.GATE, new), self.lines)
        self.assertEqual(rec['transplant']['from'], h)
        self.assertTrue(rec['transplant']['carried'])
        line = [l for l in self.lines if l.startswith(f'transplanted {B}')]
        self.assertEqual(len(line), 1, self.lines)
        self.assertIn('carried', line[0])
        self.assertIn('no session', line[0])
        self.assertEqual(transplant.count(self.state_dir, ITEM, h), 1)

    def test_a_trunk_change_beside_the_approved_one_is_merged_and_the_approval_carried(self):
        self.approved_branch()
        self.push_main({'a.txt': A0.replace('3\n', '3 on the trunk\n')}, 'fix: a (#10)')
        self.held()
        self.run_pass()
        new = self.tip()
        self.assertEqual(self.show(new, 'a.txt'), A1.replace('3\n', '3 on the trunk\n'))
        rec = self.lane_of(B)
        self.assertTrue(rec['transplant']['carried'])
        self.assertEqual(rec['state'], lane.GATE)

    def test_the_carried_approval_holds_on_a_later_pass_at_the_same_head(self):
        self.approved_branch()
        self.held()
        self.run_pass()
        new = self.tip()
        self.run_pass()
        rec = self.lane_of(B)
        self.assertEqual((rec['state'], rec['head']), (lane.GATE, new))

    def test_an_open_content_c_item_is_never_transplanted(self):
        for review in (OPEN_C, CHANGES):
            with self.subTest(review=review.split('\n', 1)[0]):
                self.approved_branch(review)
                old = self.held()
                self.run_pass()
                self.assertEqual(self.tip(), old)
                self.assertFalse(any(l.startswith('transplanted') for l in self.lines))

    def test_a_content_correction_is_never_transplanted(self):
        self.approved_branch()
        old = self.held({'kind': 'review', 'text': 'reviews/1-t-0001.md reads changes'},
                        reason='kind=review')
        self.run_pass()
        self.assertEqual(self.tip(), old)
        self.assertEqual(lifecycle.corrections(self.sessions())[ITEM]['kind'], 'review')

    def test_a_declared_generated_file_is_regenerated_by_its_command(self):
        self.approved_branch()
        self.held()
        product = self.reviewed(generated=[{
            'paths': ['gen/*.md'],
            'run': "printf '# Report (generated)\\nrows: regenerated\\n' > gen/report.md"}])
        self.run_pass(product)
        new = self.tip()
        self.assertEqual(self.show(new, 'gen/report.md'), '# Report (generated)\nrows: regenerated\n')
        self.assertEqual(self.show(new, 'a.txt'), A1)
        msg = sh(['git', 'log', '-1', '--format=%B', new], cwd=self.origin).stdout
        self.assertIn('Regenerated:', msg)
        self.assertTrue(self.lane_of(B)['transplant']['carried'])

    def test_a_re_review_only_when_the_transplanted_diff_differs(self):
        # the trunk moved a.txt beside the approved change: merged, never dropped, and the
        # change is the approved one — carried (the first test); here the trunk already holds
        # part of it (another PR landed a.txt's line), so what lands is not what was approved
        self.approved_branch()
        self.push_main({'a.txt': A1}, 'fix: a (#10)')
        self.held()
        self.run_pass()
        new = self.tip()
        self.assertEqual((self.show(new, 'a.txt'), self.show(new, 'b.txt')), (A1, 'b\n'))
        rec = self.lane_of(B)
        self.assertFalse(rec['transplant']['carried'])
        self.assertEqual((rec['state'], rec['head']), (lane.REVIEW, new), self.lines)
        self.assertIn('round 2 wanted', rec['reason'])
        self.assertTrue(any('re-review: the diff differs' in l for l in self.lines), self.lines)

    def test_a_trunk_change_to_the_same_lines_is_never_overwritten(self):
        h = self.approved_branch()
        self.push_main({'a.txt': A0.replace('a0', 'a0 on the trunk')}, 'fix: a (#10)')
        old = self.held()
        self.run_pass()
        self.assertEqual(self.tip(), old)
        self.assertTrue(any('conflicts with the trunk' in l and 'a.txt' in l
                            for l in self.lines), self.lines)
        self.assertEqual(transplant.count(self.state_dir, ITEM, h), 1)

    def test_never_while_a_session_runs_or_past_an_operator_park_ruling_or_the_cap(self):
        cases = {
            'live': dict(live=True),
            'operator correction': dict(corr={'kind': 'operator', 'text': 'copy from archive'}),
            'parked': dict(corr=dict(MECHANICAL, parked=True)),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.approved_branch()
                old = self.held(**kw)
                self.run_pass()
                self.assertEqual(self.tip(), old)
                os.remove(self.sessions())
        # an operator park on the item
        self.approved_branch()
        old = self.held()
        lifecycle.note_park(self.sessions(), ITEM, 'item', 'operator park', 'hold it')
        self.run_pass()
        self.assertEqual(self.tip(), old)
        os.remove(self.sessions())
        # the cap: an approved head transplanted CAP times already
        h = self.approved_branch()
        old = self.held()
        for _ in range(transplant.CAP):
            transplant.note(self.state_dir, item=ITEM, **{'from': h})
        self.run_pass()
        self.assertEqual(self.tip(), old)

    def test_a_red_pre_push_check_pushes_nothing_and_is_a_correction_round(self):
        self.approved_branch()
        old = self.held()
        self.run_pass(self.reviewed(pre_push_check='echo typecheck: 1 error in a.txt; exit 2'))
        self.assertEqual(self.tip(), old)
        corr = lifecycle.corrections(self.sessions())[ITEM]
        self.assertEqual(corr['kind'], 'gate')
        self.assertIn('pre-push check fails', corr['text'])
        self.assertIn('typecheck: 1 error', corr['text'])
        self.assertEqual(self.lane_of(B)['state'], lane.BACK)


class Pieces(unittest.TestCase):

    def test_mechanical_reads_the_kind_and_generated_only_reds(self):
        gen = lambda p: p.startswith('docs/research/')  # noqa: E731
        self.assertTrue(transplant.mechanical({'kind': 'naming'}))
        self.assertTrue(transplant.mechanical({'kind': 'rebase conflict'}))
        self.assertFalse(transplant.mechanical({'kind': 'review'}))
        self.assertFalse(transplant.mechanical({'kind': 'copies', 'parked': True}))
        self.assertTrue(transplant.mechanical(
            {'kind': 'hook refused', 'text': 'docs-check: docs/research/drift-report.md stale'},
            gen))
        self.assertFalse(transplant.mechanical(
            {'kind': 'gate', 'text': 'red: apps/web/lib/view-models.test.ts, '
                                     'docs/research/drift-report.md'}, gen))

    def test_artifacts_and_declared_generated_files(self):
        from asf.conventions import Conventions
        conv = Conventions.from_mapping({'reviews_dir': '.sdd-input/reviews', 'generated': [
            {'paths': ['docs/research/*.md'], 'run': 'node scripts/feature-matrix.mjs'}]})
        self.assertTrue(transplant.artifact(conv, '.sdd-input/reviews/5-t-0338.md'))
        self.assertTrue(transplant.artifact(conv, '.files-block.json'))
        self.assertFalse(transplant.artifact(conv, 'scripts/check-claims.sh'))
        self.assertEqual(transplant.rule_of(conv, 'docs/research/drift-report.md'),
                         (['docs/research/*.md'], 'node scripts/feature-matrix.mjs'))
        self.assertIsNone(transplant.rule_of(conv, 'docs/platform/privacy-residency.md'))


if __name__ == '__main__':
    unittest.main()
