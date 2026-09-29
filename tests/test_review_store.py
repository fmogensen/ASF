"""The review store (asf.evidence.review_store): a review never touches the branch it reviews.

A review session once committed its review onto the PR branch and pushed it; every such push
moved the PR head, cancelled and restarted the PR's whole CI, and moved the head a merge watcher
was pinned to. Now the session writes the file and commits nothing, the health pass files it off
the branch bound to the head it reviewed (the writer), the lane and the evidence read it there
first (the readers), and a later push makes it history exactly as before (the gate)."""
import datetime
import importlib
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf.conventions import Conventions
from asf.evidence import review, review_store
from asf.workers import health, lifecycle
from tests.test_doc_lane_landing import GIT_ENV, git

#: the module, not the package's ``build`` function of the same name
build = importlib.import_module('asf.briefs.build')

BRANCH = 'worker/T-1'
APPROVED = '# Review\n\nverdict: approved\n'
CHANGES = '# Review\n\nverdict: changes requested\n\n## C\n\n1. `app.py:1` — fix it\n'


def _at(minute):
    return datetime.datetime(2026, 9, 29, 12, minute, tzinfo=datetime.timezone.utc)


class Repo(unittest.TestCase):
    """A bare origin, and a clone of it on ``worker/T-1`` pushed — the review session's worktree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='review_store_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.wt = os.path.join(self.tmp, 'wt')
        self.store = os.path.join(self.tmp, 'state', review_store.DIRNAME)
        env = dict(os.environ, **GIT_ENV)
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', self.origin], check=True, env=env)
        subprocess.run(['git', 'clone', '-q', self.origin, self.wt], check=True, env=env,
                       capture_output=True)
        self.write('app.py', 'x = 1\n')
        git(self.wt, 'add', '-A')
        git(self.wt, 'commit', '-qm', 'base')
        git(self.wt, 'push', '-q', 'origin', 'main')
        git(self.wt, 'checkout', '-qb', BRANCH)
        self.write('app.py', 'x = 2\n')
        git(self.wt, 'commit', '-qam', 'task(T-1): the change')
        git(self.wt, 'push', '-q', 'origin', BRANCH)
        self.code = git(self.wt, 'rev-parse', 'HEAD')
        self.conv = Conventions()

    def write(self, path, text):
        full = os.path.join(self.wt, path)
        os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
        with open(full, 'w') as fh:
            fh.write(text)

    def review_file(self, n, text=APPROVED):
        path = self.conv.review_path('t-1', n)
        self.write(path, text)
        return path

    def remote_head(self):
        return git(self.wt, 'ls-remote', '--heads', 'origin', BRANCH).split()[0]


class Writer(Repo):
    """:func:`review_store.take` — the session's file, filed and gone; the branch untouched."""

    def test_an_uncommitted_review_is_filed_under_the_head_and_leaves_the_worktree(self):
        path = self.review_file(1)
        filed = review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1', self.code)
        self.assertEqual(len(filed), 1)
        n, entry = filed[0]
        self.assertEqual(n, 1)
        self.assertIn(self.code, os.path.basename(entry))
        self.assertFalse(os.path.exists(os.path.join(self.wt, path)))
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        self.assertEqual(self.remote_head(), self.code)       # the PR head never moved
        got = review_store.newest(self.store, 't-1', BRANCH)
        self.assertEqual((got['round'], got['head'], got['text']), (1, self.code, APPROVED))

    def test_a_staged_review_is_unstaged_and_filed(self):
        path = self.review_file(2)
        git(self.wt, 'add', path)
        filed = review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1', self.code)
        self.assertEqual([n for n, _ in filed], [2])
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')

    def test_a_review_commit_made_out_of_habit_is_filed_and_dropped_unpushed(self):
        self.review_file(1)
        git(self.wt, 'add', '-A')
        git(self.wt, 'commit', '-qm', 'review(T-1): round 1')
        filed = review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1', self.code)
        self.assertEqual([n for n, _ in filed], [1])
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.code)
        self.assertEqual(review_store.newest(self.store, 't-1', BRANCH)['head'], self.code)

    def test_a_commit_touching_code_is_work_and_is_left_alone(self):
        self.review_file(1)
        self.write('app.py', 'x = 3\n')
        git(self.wt, 'add', '-A')
        git(self.wt, 'commit', '-qm', 'code and review')
        tip = git(self.wt, 'rev-parse', 'HEAD')
        self.assertEqual(review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1',
                                           self.code), [])
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), tip)

    def test_another_items_file_and_other_files_are_not_taken(self):
        self.write(self.conv.review_path('t-9', 1), APPROVED)
        self.write('notes.txt', 'x\n')
        self.assertEqual(review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1',
                                           self.code), [])
        self.assertTrue(os.path.exists(os.path.join(self.wt, self.conv.review_path('t-9', 1))))

    def test_put_refuses_an_entry_bound_to_no_head(self):
        with self.assertRaises(ValueError):
            review_store.put(self.store, 't-1', BRANCH, 1, '', APPROVED)


class Reader(Repo):
    """:func:`review.review_at` reads the store first and still reads the branch's own files."""

    def at(self, **kw):
        return review.review_at(self.wt, self.conv, f'origin/{BRANCH}', 'T-1',
                                store=self.store, **kw)

    def commit_branch_review(self, n, text):
        self.review_file(n, text)
        git(self.wt, 'add', '-A')
        git(self.wt, 'commit', '-qm', f'review(T-1): round {n}')
        git(self.wt, 'push', '-q', 'origin', BRANCH)

    def test_a_stored_review_is_read_with_its_head_and_the_conventions_path(self):
        review_store.put(self.store, 't-1', BRANCH, 1, self.code, CHANGES)
        rv = self.at()
        self.assertEqual(rv['verdict'], review.CHANGES)
        self.assertEqual(rv['round'], 1)
        self.assertEqual(rv['head'], self.code)
        self.assertEqual(rv['path'], self.conv.review_path('t-1', 1))
        self.assertTrue(rv['stored'].startswith(self.store))
        self.assertIn('app.py', review.c_items(rv['body']))

    def test_a_historical_review_on_the_branch_is_still_read(self):
        self.commit_branch_review(1, APPROVED)
        rv = self.at()
        self.assertEqual((rv['round'], rv['verdict']), (1, review.APPROVED))
        self.assertNotIn('stored', rv)

    def test_the_store_wins_a_tie_and_a_higher_round_the_branch_wins_only_when_strictly_higher(self):
        self.commit_branch_review(2, APPROVED)
        review_store.put(self.store, 't-1', BRANCH, 1, self.code, CHANGES)
        self.assertEqual(self.at()['verdict'], review.APPROVED)        # branch round 2 > 1
        review_store.put(self.store, 't-1', BRANCH, 2, self.code, CHANGES)
        self.assertEqual(self.at()['verdict'], review.CHANGES)         # the tie: the store

    def test_the_newest_filed_wins_on_a_recut_branch_whose_rounds_reset(self):
        review_store.put(self.store, 't-1', BRANCH, 3, 'a' * 40, APPROVED, now=_at(1))
        review_store.put(self.store, 't-1', BRANCH, 1, self.code, CHANGES, now=_at(2))
        rv = self.at()
        self.assertEqual((rv['round'], rv['head']), (1, self.code))

    def test_another_branch_s_review_is_not_this_one_s(self):
        review_store.put(self.store, 't-1', 'worker/T-1-old', 1, self.code, APPROVED)
        self.assertIsNone(self.at())

    def test_the_evidence_pass_reads_a_stored_review_under_its_conventions_name(self):
        from asf.evidence import evidence
        self.assertEqual(evidence.stored_name(self.conv, self.conv.reviews_dir, 'T-1', 2),
                         os.path.basename(self.conv.review_path('t-1', 2)))

    def test_the_brief_facts_prefer_the_stored_review(self):
        from asf.briefs import facts
        review_store.put(self.store, 't-1', BRANCH, 2, self.code, CHANGES)
        product = mock.Mock(conventions=self.conv)
        with mock.patch.object(review_store, 'root', return_value=self.store):
            n, path, text = facts.stored_review(product, 'T-1', BRANCH, (1, 'old', APPROVED))
        self.assertEqual((n, path, text), (2, self.conv.review_path('t-1', 2), CHANGES))


class Gate(Repo):
    """The verdict binds to the head it reviewed: a later push invalidates it, as before."""

    def current(self):
        rv = review.review_at(self.wt, self.conv, f'origin/{BRANCH}', 'T-1', store=self.store)
        git(self.wt, 'fetch', '-q', 'origin')
        tip = git(self.wt, 'rev-parse', f'origin/{BRANCH}')
        return rv, review.is_current(self.wt, self.conv, f'origin/{BRANCH}', rv, tip,
                                     trunk='origin/main')

    def test_an_approval_of_the_head_is_current(self):
        self.review_file(1)
        review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1', self.code)
        rv, current = self.current()
        self.assertEqual(rv['verdict'], review.APPROVED)
        self.assertTrue(current)

    def test_a_later_push_of_code_makes_it_history(self):
        self.review_file(1)
        review_store.take(self.store, self.conv, self.wt, BRANCH, 'T-1', self.code)
        self.write('app.py', 'x = 4\n')
        git(self.wt, 'commit', '-qam', 'more code')
        git(self.wt, 'push', '-q', 'origin', BRANCH)
        _rv, current = self.current()
        self.assertFalse(current)

    def test_a_review_run_that_filed_its_review_is_finished_without_a_commit(self):
        run = {'job': 'review-t-1', 'kind': 'review', 'branch': BRANCH, 'pid': 1, 'started': 't'}
        ev = lifecycle.Evidence(result={'subtype': 'success', 'is_error': False, 'result': 'ok'},
                                remote_sha=self.code, head_on_remote=True, has_commits=False,
                                worktree=True)
        self.assertEqual(lifecycle.judge(run, ev), f'failed: {lifecycle.EMPTY_BRANCH}')
        self.assertEqual(lifecycle.judge(dict(run, review_filed='/x'), ev), lifecycle.FINISHED)

    def test_health_files_a_finished_review_run_before_it_is_judged(self):
        self.review_file(1)
        run = {'job': 'review-t-1', 'kind': 'review', 'branch': BRANCH, 'item': 'T-1',
               'worktree': self.wt}
        product = mock.Mock(conventions=self.conv)
        ev = lifecycle.Evidence(result={'result': 'ok'}, remote_sha=self.code, uncommitted=1)
        found = []
        with mock.patch.object(review_store, 'root', return_value=self.store), \
                mock.patch.object(health.pool_mod, 'update_session') as update, \
                mock.patch.object(health.lifecycle, 'gather', return_value='regathered'):
            got = health.file_review(product, 'review-t-1', run, ev, None, found)
        self.assertEqual(got, 'regathered')
        update.assert_called_once()
        self.assertTrue(run['review_filed'].startswith(self.store))
        self.assertEqual(found[0][1], 'filed')
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        self.assertEqual(self.remote_head(), self.code)

    def test_health_leaves_a_coder_run_and_a_live_run_alone(self):
        self.review_file(1)
        ev = lifecycle.Evidence(result={'result': 'ok'}, remote_sha=self.code, uncommitted=1)
        for run, e in (({'kind': 'coder', 'branch': BRANCH, 'item': 'T-1', 'worktree': self.wt}, ev),
                       ({'kind': 'review', 'branch': BRANCH, 'item': 'T-1', 'worktree': self.wt},
                        lifecycle.Evidence(alive=True))):
            with self.subTest(kind=run['kind']), \
                    mock.patch.object(review_store, 'root', return_value=self.store):
                self.assertIs(health.file_review(mock.Mock(conventions=self.conv), 'j', run, e,
                                                 None, []), e)
        self.assertEqual(review_store.entries(self.store, 't-1', BRANCH), [])


class Brief(unittest.TestCase):
    """The session writes the review and commits nothing; a reader of a stored review gets its
    text in the brief, since it is not in the worktree."""

    def test_the_review_template_forbids_committing_and_pushing(self):
        text = build.load_template('review')
        self.assertIn('never commit it, never push', text)
        self.assertIn('pushed: n/a', text)
        self.assertNotIn('commit `{review_path}`', build.FOREIGN_REVIEW)

    def test_a_stored_review_is_quoted_for_the_kinds_that_read_it(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        review_store.put(tmp, 't-1', BRANCH, 2, 'b' * 40, CHANGES + '~~~~\n')
        product = mock.Mock(conventions=Conventions())
        with mock.patch.object(review_store, 'root', return_value=tmp):
            for kind in build.STORED_REVIEW_KINDS:
                with self.subTest(kind=kind):
                    text = build.stored_review_section(product, kind, BRANCH, 'T-1')
                    self.assertIn('round 2', text)
                    self.assertIn('fix it', text)
                    self.assertIn('~~~~~\n', text)            # a fence longer than the text's own
            self.assertEqual(build.stored_review_section(product, 'coder', BRANCH, 'T-1'), '')
            self.assertEqual(build.stored_review_section(product, 'fixer', 'other', 'T-1'), '')


if __name__ == '__main__':
    unittest.main()
