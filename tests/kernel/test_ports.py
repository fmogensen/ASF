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
        r.record_review('T-0001', 7, 't2', 'approve', [], 'c2')
        self.assertEqual(r.reviews()[-1], M.Review('T-0001', 't2', 'approve', [], 'c2'))


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

    def test_a_dirty_merge_state_is_a_conflict_and_unknown_stays_unknown(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, '', 'no')
        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run)

        def conflicting(mergeable, state):
            return gh._open_pr({'number': 7, 'headRefName': 'worker/t-0001-slug',
                                'headRefOid': 'h1', 'mergeable': mergeable,
                                'mergeStateStatus': state}).conflicting
        self.assertTrue(conflicting('UNKNOWN', 'DIRTY'))
        armed = gh._open_pr({'number': 7, 'headRefName': 'worker/t-0001-slug',
                             'autoMergeRequest': {'enabledAt': '2026-10-10T01:14:38Z'}})
        self.assertEqual((armed.auto_merge, armed.auto_merge_at),
                         (True, '2026-10-10T01:14:38Z'))
        self.assertTrue(conflicting('CONFLICTING', 'BLOCKED'))
        self.assertFalse(conflicting('UNKNOWN', 'BEHIND'))
        self.assertFalse(conflicting('MERGEABLE', 'CLEAN'))

    def test_change_id_is_blind_to_line_numbers_and_sees_a_new_commit(self):
        files = [{'filename': 'src/a.py', 'status': 'modified', 'sha': 'b1',
                  'patch': '@@ -1,2 +1,3 @@ def f():\n x = 1\n+y = 2\n z = 3'},
                 {'filename': 'img.png', 'status': 'added', 'sha': 'b2'}]
        moved = [dict(files[0], patch=files[0]['patch'].replace('-1,2 +1,3', '-40,2 +41,3'),
                      sha='b9'), files[1]]
        self.assertEqual(P.change_id(files), P.change_id(list(reversed(moved))))
        more = [dict(files[0], patch=files[0]['patch'] + '\n+w = 4'), files[1]]
        self.assertNotEqual(P.change_id(files), P.change_id(more))
        self.assertNotEqual(P.change_id(files), P.change_id([files[0], dict(files[1], sha='b3')]))
        self.assertEqual(P.change_id([]), '')

    def test_the_change_is_read_off_the_compare_api_once_per_head(self):
        calls = []
        compare = {'files': [{'filename': 'src/a.py', 'status': 'modified', 'patch': '+x'}]}

        def run(argv, **kw):
            calls.append(argv[1:])
            out = json.dumps(compare) if argv[1:3] == ['api', 'repos/o/r/compare/main...h9'] \
                else ''
            return subprocess.CompletedProcess(argv, 0 if out else 1, out, '')

        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run)
        P.RealGitHub._changes.pop(('o/r', 'main', 'h9'), None)
        self.assertEqual(gh._change('main', 'h9'), P.change_id(compare['files']))
        self.assertEqual(gh._change('main', 'h9'), P.change_id(compare['files']))
        self.assertEqual(len(calls), 1)
        self.assertEqual(gh._change('main', 'h8'), '')  # unreadable: keyed by tree alone

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


class EndReview(unittest.TestCase):
    """An ended review session's worktree is freed when its only dirty files are under the
    product's reviews_dir (copied to state/<p>/kernel-reviews/<job>.md first); any other dirty
    file keeps it."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.state = tempfile.mkdtemp()
        # the suite's global git config carries the identity (tests.test_hermetic's ratchet)
        for args in (['init', '-q', '-b', 'main'], ['commit', '-q', '--allow-empty', '-m', 'x']):
            subprocess.run(['git'] + args, cwd=self.repo, check=True, capture_output=True)
        self.wt = os.path.join(self.state, 'worktrees', 'review-t-0001')
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'r1', self.wt], cwd=self.repo,
                       check=True, capture_output=True)
        self.product = env.Product('sample', {'repo_dir': self.repo,
                                              'conventions': {'reviews_dir': 'docs/reviews'}})

    def write(self, rel, text):
        path = os.path.join(self.wt, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)

    def end(self):
        from unittest import mock
        s = M.Session(job='review-t-0001-1', item_id='T-0001', kind='review', alive=False,
                      ended=True, result='report', worktree=self.wt)
        with mock.patch('asf.env.state_dir', return_value=self.state), \
                mock.patch('asf.workers.pool.update_session'), \
                mock.patch('asf.workers.trash.kick'):
            P.RealSessions(self.product).end(s, True)

    def test_only_a_review_file_frees_the_worktree_and_keeps_the_file(self):
        self.write('docs/reviews/1-t-0001.md', 'VERDICT: approve\n')
        self.end()
        self.assertFalse(os.path.exists(self.wt))
        with open(os.path.join(self.state, P.REVIEW_COPIES_DIR, 'review-t-0001-1.md')) as f:
            self.assertEqual(f.read(), 'VERDICT: approve\n')

    def test_any_other_dirty_file_keeps_the_worktree(self):
        self.write('docs/reviews/1-t-0001.md', 'VERDICT: approve\n')
        self.write('src/a.py', 'x = 1\n')
        with self.assertRaises(P.PortError) as e:
            self.end()
        self.assertIn('uncommitted', str(e.exception))
        self.assertTrue(os.path.isdir(self.wt))
        self.assertFalse(os.path.exists(os.path.join(self.state, P.REVIEW_COPIES_DIR)))

    def test_dirty_paths_reads_renames_and_quotes(self):
        self.assertEqual(P.dirty_paths('?? docs/reviews/a.md\nR  a -> "b c"\n'),
                         ['docs/reviews/a.md', 'a', 'b c'])


class Helpers(unittest.TestCase):

    def test_item_of_branch(self):
        self.assertEqual(P.item_of_branch('fix/F-0318-stories-gate'), 'F-0318')
        self.assertEqual(P.item_of_branch('spec/f-0313'), 'F-0313')
        self.assertIsNone(P.item_of_branch('main'))

    def test_required_checks_come_from_landing_checks_first(self):
        class GH:
            def required_checks(self):
                return ('from rules',)
        named = env.Product('sample', {'conventions': {
            'landing_checks': ['tests (3.12)', 'tests (3.13)']}})
        self.assertEqual(P.config_for(named, github=GH()).required_checks,
                         ('tests (3.12)', 'tests (3.13)'))
        bare = env.Product('sample', {})
        self.assertEqual(P.config_for(bare, github=GH()).required_checks, ('from rules',))
        self.assertEqual(P.config_for(bare).required_checks, ())

        class Broken:
            def required_checks(self):
                raise P.PortError('down')
        self.assertEqual(P.config_for(bare, github=Broken()).required_checks, ())

    def test_required_checks_read_off_the_branch_rules(self):
        rules = [{'type': 'deletion'},
                 {'type': 'required_status_checks', 'parameters': {'required_status_checks': [
                     {'context': 'tests (3.12)'}, {'context': 'tests (3.13)'}]}}]

        def run(argv, **kw):
            if argv[1:3] == ['api', 'repos/o/r/rules/branches/main']:
                return subprocess.CompletedProcess(argv, 0, json.dumps(rules), '')
            return subprocess.CompletedProcess(argv, 1, '', 'unexpected')
        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run)
        self.assertEqual(gh.required_checks(), ('tests (3.12)', 'tests (3.13)'))

    def test_failed_step_and_log_tail_from_a_failed_log(self):
        log = ('tests (3.12)\tSet up\t2026-10-09T10:00:00.1Z ok\n'
               'tests (3.12)\tno new raw call site\t2026-10-09T10:00:01.2Z '
               '\x1b[31masf/x.py:3: raw call\x1b[0m\n'
               'tests (3.12)\tno new raw call site\t2026-10-09T10:00:01.3Z exit 1\n')
        self.assertEqual(P.failed_step(log),
                         ('no new raw call site', 'asf/x.py:3: raw call\nexit 1'))
        self.assertEqual(P.failed_step(''), ('', ''))

    def test_config_for_reads_the_conventions(self):
        cfg = P.config_for(env.Product('sample', {'conventions': {
            'branch_prefixes': {'code': 'w/', 'spec': 's/', 'plan': 'p/'}, 'specs_dir': 'sp'}}))
        self.assertEqual((cfg.work_branch, cfg.doc_branches), ('w/', ('s/', 'p/')))
        self.assertIn('sp/**', cfg.doc_paths)


class Scrub(unittest.TestCase):
    """Card text the kernel writes carries no worker account name: the record's pre-commit
    would refuse the whole tick's commit over one (2026-10-10: a launch failure naming an
    external worktree under a home directory named for the account landed in a card)."""

    def setUp(self):
        from unittest import mock
        from asf import redact
        Record.setUp(self)
        pats = redact.patterns(cfg={'worker_pool': {'accounts': [{'name': 'zelda'}]}},
                               environ={})
        patcher = mock.patch('asf.redact.default_patterns', return_value=pats)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_note_naming_an_account_is_scrubbed_before_the_write(self):
        leak = ('launch: branch checked out in an external worktree '
                '/home/someone/Code/x/.claude/worktrees/agent-1; cloud run on Zelda failed')
        P.RealRecord(self.product, state_dir=self.state).write_fields('T-0001', {
            P.ATTEMPTS: [leak], P.STUCK_REASON: leak, P.NOTES: ['asked zelda: why?']})
        with open(os.path.join(self.root, 'tasks', 'T-0001.md')) as f:
            text = f.read()
        self.assertNotIn('zelda', text.lower())
        self.assertIn('~/Code/x/.claude/worktrees/agent-1', text)
        self.assertIn('cloud run on a worker account failed', text)

    def test_a_card_changed_on_disk_is_scrubbed_before_the_commit(self):
        from unittest import mock
        rec = P.RealRecord(self.product, state_dir=self.state)
        rec._before = {}
        path = os.path.join(self.root, 'tasks', 'T-0001.md')
        with open(path, 'a') as f:
            f.write('seen by zelda\n')
        with open(path, 'rb') as f:
            dirty = {'tasks/T-0001.md': f.read()}
        with mock.patch('asf.record.publish._dirty', return_value=dirty):
            self.assertEqual(rec.scrub_changed(), ['tasks/T-0001.md'])
        with open(path) as f:
            self.assertNotIn('zelda', f.read())

    def test_a_refused_commit_is_one_line_naming_files_and_classes(self):
        err = subprocess.CalledProcessError(
            1, ['commit'], output='',
            stderr='features/F-0301.md:25: name (worker_pool.accounts)\n'
                   'index.json:32850: name (worker_pool.accounts)\n'
                   'redact: refused — 2 finding(s)\n')
        self.assertEqual(P.refusal_line(err),
                         'record commit refused: features/F-0301.md, index.json — '
                         'name (worker_pool.accounts)')


if __name__ == '__main__':
    unittest.main()
