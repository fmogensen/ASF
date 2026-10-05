"""Correction quality: a correct round is never briefed on a head its review no longer judges,
and ``asf answer`` answers a session's question without spending a correction round."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.workers import answer, judged, lifecycle


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(cwd, name, text):
    with open(os.path.join(cwd, name), 'w') as f:
        f.write(text)
    _git(cwd, 'add', name)
    _git(cwd, 'commit', '-q', '-m', f'task(T-0017): {name}')
    return _git(cwd, 'rev-parse', 'HEAD')


class JudgedHeadTests(unittest.TestCase):
    """Defect: a correction round launched against a stale head — the branch moved past the
    head the review that asked for it judged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='judged_test_')
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        _git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        _git(self.tmp, 'clone', '-q', self.origin, self.repo)
        for k, v in (('user.email', 't@e'), ('user.name', 't'), ('commit.gpgsign', 'false')):
            _git(self.repo, 'config', k, v)
        _commit(self.repo, 'base.txt', 'base\n')
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        _git(self.repo, 'checkout', '-q', '-b', 'task/T-0017')
        self.judged = _commit(self.repo, 'a.txt', 'a\n')
        _git(self.repo, 'push', '-q', 'origin', 'task/T-0017')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_hold_records_the_head_its_review_judged(self):
        path = os.path.join(self.tmp, 'sessions.jsonl')
        open(path, 'w').close()
        run = {'job': 'task-t-0017', 'item': 'T-0017', 'kind': 'task', 'branch': 'task/T-0017',
               'started': '2026-10-05T09:00:00Z'}
        fields, _line = lifecycle.hold(path, run, 'review', 'reviews/1.md reads changes',
                                       '2026-10-05T10:00:00Z', head=self.judged)
        self.assertEqual(fields['correction'].get('judged_head'), self.judged)

    def test_an_unmoved_branch_needs_no_note(self):
        self.assertIsNone(judged.moved(self.repo, 'task/T-0017', self.judged, 'main'))
        self.assertEqual(judged.section(self.repo, 'task/T-0017', self.judged, 'main'), '')

    def test_a_moved_branch_briefs_the_real_head_and_the_commits_since(self):
        later = _commit(self.repo, 'b.txt', 'b\n')
        _git(self.repo, 'push', '-q', 'origin', 'task/T-0017')
        _git(self.repo, 'fetch', '-q', 'origin')
        got = judged.moved(self.repo, 'task/T-0017', self.judged, 'main')
        self.assertIsNotNone(got)
        head, commits, _stat = got
        self.assertEqual(head, later)
        self.assertEqual(len(commits), 1)
        text = judged.section(self.repo, 'task/T-0017', self.judged, 'main')
        self.assertIn('BRANCH MOVED', text)
        self.assertIn(later[:9], text)
        self.assertIn(self.judged[:9], text)
        self.assertIn('b.txt', text)

    def test_a_rebase_onto_a_newer_trunk_alone_is_no_move(self):
        _git(self.repo, 'checkout', '-q', 'main')
        _commit(self.repo, 'trunk.txt', 't\n')
        _git(self.repo, 'push', '-q', 'origin', 'main')
        _git(self.repo, 'checkout', '-q', 'task/T-0017')
        _git(self.repo, 'rebase', '-q', 'main')
        _git(self.repo, 'push', '-q', '-f', 'origin', 'task/T-0017')
        _git(self.repo, 'fetch', '-q', 'origin')
        self.assertIsNone(judged.moved(self.repo, 'task/T-0017', self.judged, 'main'))

    def test_the_launch_reads_the_judged_head_off_the_pending_correction(self):
        later = _commit(self.repo, 'b.txt', 'b\n')
        _git(self.repo, 'push', '-q', 'origin', 'task/T-0017')
        _git(self.repo, 'fetch', '-q', 'origin')
        path = os.path.join(self.tmp, 'sessions.jsonl')
        with open(path, 'w') as f:
            f.write(json.dumps({'job': 'task-t-0017', 'item': 'T-0017', 'kind': 'task',
                                'branch': 'task/T-0017', 'started': '2026-10-05T09:00:00Z',
                                'ended': '2026-10-05T09:30:00Z', 'end_reason': 'finished',
                                'correction': {'kind': 'review', 'text': 'answer C1',
                                               'at': '2026-10-05T10:00:00Z',
                                               'judged_head': self.judged}}) + '\n')
        text = judged.launch_section(path, self.repo, 'main', 'correct', 'T-0017', 'task/T-0017')
        self.assertIn(later[:9], text)
        # only a correction round is briefed this way
        self.assertEqual(judged.launch_section(path, self.repo, 'main', 'task', 'T-0017',
                                               'task/T-0017'), '')


class AnswerTests(unittest.TestCase):
    """Defect: no command answers a session that stopped with ``needs input``."""
    RUN = {'job': 'task-t-0017', 'item': 'T-0017', 'kind': 'task', 'pid': 1,
           'branch': 'task/T-0017', 'started': '2026-10-05T09:00:00Z',
           'ended': '2026-10-05T09:30:00Z', 'end_reason': 'failed: empty branch'}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='answer_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\n')
        self.path = os.path.join(env.state_dir('sample'), 'sessions.jsonl')
        self.product = env.load_product('sample')

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger(self, *records):
        with open(self.path, 'w') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def run_cmd(self, target='task-t-0017', text='use the v2 endpoint', file=None):
        args = argparse.Namespace(target=target, text=text, file=file, product='sample')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = answer.cmd_answer(args)
        return rc, out.getvalue()

    def test_an_answer_reaches_the_next_brief_of_the_item(self):
        self.ledger(self.RUN)
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        text = answer.brief_section(self.product, 'T-0017')
        self.assertIn('OPERATOR ANSWER', text)
        self.assertIn('use the v2 endpoint', text)
        self.assertEqual(answer.brief_section(self.product, 'T-0099'), '')

    def test_the_brief_builder_quotes_the_answer(self):
        import importlib
        build = importlib.import_module('asf.briefs.build')
        self.ledger(self.RUN)
        self.run_cmd()
        self.assertIn('use the v2 endpoint', build.answer_section(self.product, 'T-0017'))
        self.assertEqual(build.answer_section(self.product, 'none'), '')

    def test_an_answer_is_no_correction_round(self):
        self.ledger(self.RUN)
        before = lifecycle.rounds_of(self.path, 'T-0017')
        self.run_cmd(target='T-0017')
        self.assertEqual(lifecycle.rounds_of(self.path, 'T-0017'), before)
        self.assertIsNone(lifecycle.correction_of(self.path, 'T-0017'))

    def test_an_answer_releases_the_question_park(self):
        corr = {'kind': lifecycle.BLOCKED, 'text': 'which endpoint?', 'at': '2026-10-05T09:31:00Z',
                'parked': True, 'reason': 'blocked: which endpoint?'}
        self.ledger(dict(self.RUN, correction=corr))
        self.assertTrue(lifecycle.correction_of(self.path, 'T-0017').get('parked'))
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        self.assertIsNone(lifecycle.correction_of(self.path, 'T-0017'))
        self.assertEqual(lifecycle.rounds_of(self.path, 'T-0017'), 0)

    def test_an_answer_is_taken_while_the_session_runs(self):
        live = dict(self.RUN)
        live.pop('ended')
        live.pop('end_reason')
        self.ledger(live)
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        self.assertIn('use the v2 endpoint', answer.brief_section(self.product, 'T-0017'))

    def test_the_answer_comes_from_a_file(self):
        self.ledger(self.RUN)
        src = os.path.join(self.tmp, 'a.txt')
        with open(src, 'w') as f:
            f.write('from the file\n')
        rc, out = self.run_cmd(text=None, file=src)
        self.assertEqual(rc, 0, out)
        self.assertIn('from the file', answer.brief_section(self.product, 'T-0017'))

    def test_no_text_and_an_unknown_target_are_refused(self):
        self.ledger(self.RUN)
        self.assertEqual(self.run_cmd(text='')[0], 2)
        self.assertEqual(self.run_cmd(target='nope-job')[0], 1)

    def test_the_cli_knows_the_verb(self):
        from asf import cli
        parser = cli.build_parser()
        args = parser.parse_args(['answer', 'T-0017', '--text', 'x'])
        self.assertIs(args.func, answer.cmd_answer)


if __name__ == '__main__':
    unittest.main()
