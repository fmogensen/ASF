"""The real ports' pure parts and the record port over a scratch record: a card's kernel fields
round-trip through the machine block, a Story is minted once, and GitHub's answers map to the
model (no network: ``gh`` is a fake ``run``)."""
import json
import os
import subprocess
import tempfile
import unittest

from asf import env
from asf.kernel import model as M
from asf.kernel import ports as P
from tests.kernel import builders as B

CARD = """---
id: T-0001
type: task
title: one
parent: F-0001
writes: [src/a.py]
after: [T-0002]
rank: 3
# ---- machine ----
schema_version: 1
state: New
evidence:
  - "rule: planned"
---
## Description
body
"""

FEATURE = """---
id: F-0001
type: feature
title: feat
# ---- machine ----
state: Closed
---
## Description
"""


class Record(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.repo = tempfile.mkdtemp()
        for folder, name, text in (('tasks', 'T-0001', CARD), ('features', 'F-0001', FEATURE)):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
            with open(os.path.join(self.root, folder, name + '.md'), 'w') as f:
                f.write(text)
        os.makedirs(os.path.join(self.repo, 'specs'))
        with open(os.path.join(self.repo, 'specs', 'f-0001.md'), 'w') as f:
            f.write('## Stories\n- S-0001: one\n')
        self.product = env.Product('sample', {'backlog_dir': self.root, 'repo_dir': self.repo,
                                              'conventions': {'specs_dir': 'specs'}})
        self.state = tempfile.mkdtemp()

    def record(self):
        return P.RealRecord(self.product, state_dir=self.state)

    def test_a_card_reads_as_an_item(self):
        items = self.record().items()
        t = items['T-0001']
        self.assertEqual((t.type, t.parent, t.rank, t.after, t.writes, t.state),
                         ('task', 'F-0001', 3, ['T-0002'], ['src/a.py'], M.State.NEW))
        self.assertIs(items['F-0001'].state, M.State.DONE, 'an old Closed card reads as Done')
        self.assertEqual(self.record().specs_landed(), {'F-0001': '## Stories\n- S-0001: one\n'})

    def test_kernel_fields_round_trip_and_keep_the_rest(self):
        odd = 'conflict: PR #7: rc 1: "quoted", [bracket], a: colon'
        self.record().write_fields('T-0001', {
            P.STATE: 'stuck', P.STUCK_REASON: odd, P.STUCK_OWNER: 'loop',
            P.ATTEMPTS: [odd, 'launch: x'], P.FIX_ROUNDS: 2})
        t = self.record().items()['T-0001']
        self.assertEqual((t.state, t.stuck.reason, t.stuck.owner, t.attempts, t.fix_rounds),
                         (M.State.STUCK, odd, 'loop', [odd, 'launch: x'], 2))
        with open(os.path.join(self.root, 'tasks', 'T-0001.md')) as f:
            text = f.read()
        self.assertIn('  - "rule: planned"', text)
        self.assertIn('writes: [src/a.py]', text)
        self.record().write_fields('T-0001', {P.STUCK_REASON: None, P.STATE: 'ready'})
        t = self.record().items()['T-0001']
        self.assertEqual((t.state, t.stuck), (M.State.READY, None))

    def test_a_story_is_minted_once(self):
        r = self.record()
        r.mint_story('F-0001', 'S-0001', 'one', ['it works'])
        r.mint_story('F-0001', 'S-0001', 'one', ['it works'])
        s = self.record().items()['S-0001']
        self.assertEqual((s.type, s.parent, s.title), ('story', 'F-0001', 'one'))
        with self.assertRaises(P.PortError):
            r.mint_story('F-0404', 'S-0002', 'two', [])

    def test_answers_and_reviews_ledgers(self):
        with open(os.path.join(self.state, P.ANSWERS_FILE), 'w') as f:
            f.write(json.dumps({'item': 'T-0001', 'text': 'yes'}) + '\nnot json\n')
        with open(os.path.join(self.state, P.REVIEWS_FILE), 'w') as f:
            f.write(json.dumps({'item': 'T-0001', 'tree': 't1', 'verdict': 'approve'}) + '\n')
        r = self.record()
        self.assertEqual(r.answers(), [M.Answer('T-0001', 'yes')])
        self.assertEqual(r.reviews(), [M.Review('T-0001', 't1', 'approve', [])])


class GitHub(unittest.TestCase):

    def test_open_prs_map_to_the_model(self):
        listing = [{'number': 7, 'headRefName': 'worker/t-0001-slug', 'headRefOid': 'h1',
                    'mergeable': 'CONFLICTING', 'mergeStateStatus': 'BEHIND',
                    'autoMergeRequest': None, 'files': [{'path': 'src/a.py'}],
                    'statusCheckRollup': [
                        {'__typename': 'CheckRun', 'name': 'tests', 'status': 'COMPLETED',
                         'conclusion': 'FAILURE',
                         'detailsUrl': 'https://x/actions/runs/55/job/1'}],
                    'latestReviews': [{'state': 'APPROVED', 'commit': {'oid': 'h1'}, 'body': ''}]}]
        answers = {('pr', 'list', '-R', 'o/r', '--state', 'open'): json.dumps(listing),
                   ('pr', 'list', '-R', 'o/r', '--state', 'merged'): json.dumps(
                       [{'number': 3, 'headRefName': 'worker/T-0002', 'headRefOid': 'h0'}]),
                   ('api', 'repos/o/r/git/commits/h1'): json.dumps({'tree': {'sha': 'tree1'}}),
                   ('api', 'repos/o/r/actions/runs/55'): json.dumps({'run_attempt': 2}),
                   ('run', 'view', '55'): 'FAIL: test_x (tests.test_a.Case)\n src/a.py:3\n'}
        calls = []

        def run(argv, **kw):
            args = argv[1:]
            calls.append(args)
            for key, out in answers.items():
                if tuple(args[:len(key)]) == key:
                    return subprocess.CompletedProcess(argv, 0, out, '')
            return subprocess.CompletedProcess(argv, 1, '', 'unexpected')

        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run)
        prs = gh.prs()
        open_pr, merged = prs
        self.assertEqual((open_pr.item_id, open_pr.tree_sha, open_pr.conflicting, open_pr.behind,
                          open_pr.files), ('T-0001', 'tree1', True, True, ['src/a.py']))
        c, = open_pr.checks
        self.assertEqual((c.conclusion, c.run_id, c.attempt, c.failing_files),
                         ('failure', 55, 2, ['src/a.py', 'tests/test_a.py']))
        self.assertEqual((merged.item_id, merged.merged), ('T-0002', True))
        self.assertEqual(gh.reviews(prs[:1]), [M.Review('T-0001', 'tree1', 'approve', [])])

    def test_writes_refuse_under_the_dry_run_guard(self):
        from asf import mutation_guard
        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}),
                          run=lambda *a, **k: self.fail('gh ran'))
        import contextlib
        import io
        with mutation_guard.active(), contextlib.redirect_stdout(io.StringIO()):
            for write in (lambda: gh.enable_auto_merge(7), lambda: gh.update_branch(7),
                          lambda: gh.rerun(55)):
                with self.assertRaises(P.PortError):
                    write()


class Helpers(unittest.TestCase):

    def test_item_of_branch(self):
        self.assertEqual(P.item_of_branch('fix/F-0318-stories-gate'), 'F-0318')
        self.assertEqual(P.item_of_branch('spec/f-0313'), 'F-0313')
        self.assertIsNone(P.item_of_branch('main'))

    def test_config_for_reads_the_conventions(self):
        cfg = P.config_for(env.Product('sample', {'conventions': {
            'branch_prefixes': {'code': 'w/', 'spec': 's/', 'plan': 'p/'}, 'specs_dir': 'sp'}}))
        self.assertEqual((cfg.work_branch, cfg.doc_branches), ('w/', ('s/', 'p/')))
        self.assertIn('sp/**', cfg.doc_paths)


if __name__ == '__main__':
    unittest.main()
