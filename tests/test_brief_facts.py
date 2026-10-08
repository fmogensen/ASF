"""asf.briefs.facts — the runner's facts reach the brief: the key contract, the head, the sizes,
the tests already in the footprint, the previous session's report, and the rule that the
preamble itself is code (no model, no network, no git in the renderer).

Git is real: a bare origin and a clone, built once and copied per test (``tests/gitfixture.py``).
"""
import ast
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

from asf import env
from asf import reservations as reservations_mod
from asf.briefs import facts
from asf.briefs import preamble as preamble_mod
from asf.env import Product
from asf.feeder.rows import Row
from asf.workers import report as report_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from gitfixture import Template
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SPEC, PLAN = 'docs/specs/f-0001.md', 'docs/plans/f-0001.md'


def _git(args, cwd):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _build_repos(tmp):
    origin, repo = os.path.join(tmp, 'origin.git'), os.path.join(tmp, 'repo')
    _git(['init', '-q', '--bare', '-b', 'main', origin], tmp)
    _git(['clone', '-q', origin, repo], tmp)
    _git(['config', 'user.email', 'r@example.com'], repo)
    _git(['config', 'user.name', 'r'], repo)
    for path, text in ((SPEC, ''.join('line %d\n' % i for i in range(42))), ('README', 'r\n'),
                       ('tests/test_a.py', 'a\n'), ('tests/test_b.py', 'b\n'),
                       ('asf/feeder/rows.py', 'x\n')):
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)
    _git(['add', '-A'], repo)
    _git(['commit', '-q', '-m', 'init'], repo)
    _git(['push', '-q', 'origin', 'HEAD:main'], repo)
    _git(['checkout', '-q', '-b', 'fix/B-0001'], repo)
    with open(os.path.join(repo, 'fix.txt'), 'w') as f:
        f.write('fix\n')
    _git(['add', '-A'], repo)
    _git(['commit', '-q', '-m', 'the fix'], repo)
    _git(['push', '-q', 'origin', 'fix/B-0001'], repo)
    _git(['checkout', '-q', 'main'], repo)


TEMPLATE = Template(_build_repos, prefix='brief_facts_')


def make_index(writes=None, tests=None):
    task = {'id': 'T-0001', 'type': 'task', 'title': 'the task', 'feature': 'F-0001',
            'parent': 'F-0001', 'folder': 'tasks', 'state': 'New', 'writes': list(writes or []),
            'tests': list(tests or [])}
    feature = {'id': 'F-0001', 'type': 'feature', 'title': 'the feature', 'folder': 'features',
               'links': {'spec': SPEC, 'plan': PLAN}}
    return {'generated': '', 'items': {'T-0001': task, 'F-0001': feature}}


def make_row(branch='fix/B-0001', kind='task'):
    return Row(tier=2, kind='PLAN → CODE', item_id='T-0001', feature_id='F-0001',
               action='would launch', brief_kind=kind, branch=branch, reason='r')


class FactsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TEMPLATE.fresh()
        self.repo = os.path.join(self.tmp, 'repo')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.addCleanup(setattr, env, 'ASF_HOME', self._home)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def product(self, repo_dir=None, **conventions):
        return Product('sample', {'repo_dir': repo_dir or self.repo, 'main': 'main',
                                  'conventions': conventions})

    def facts_for(self, writes=None, tests=None, branch='fix/B-0001', **conventions):
        return facts.repo_facts(self.product(**conventions), make_row(branch),
                                make_index(writes, tests))


class ContractTests(FactsCase):
    def test_keys_match_what_the_preamble_reads(self):
        self.assertEqual(set(self.facts_for()), set(preamble_mod.REPO_FACT_KEYS))

    def test_no_product_repo_returns_the_keys_empty(self):
        out = facts.repo_facts(self.product(repo_dir=os.path.join(self.tmp, 'nope')),
                               make_row(), make_index(['tests/**']))
        self.assertEqual(set(out), set(preamble_mod.REPO_FACT_KEYS))
        self.assertIs(out['branch_exists'], False)
        self.assertEqual((out['files'], out['tests']), ({}, []))

    def test_reservations_is_empty_with_no_snapshot_on_disk(self):
        self.assertEqual(self.facts_for()['reservations'], {})

    def test_reservations_is_read_from_the_snapshot_on_disk(self):
        product = self.product()
        snap = {'at': 'now', 'trunk': 'main', 'refs': [], 'prs': {},
                'sequences': {'bands': {'width': 4, 'held': {'289': ['worker/T-0361']}}},
                'errors': []}
        reservations_mod.save(env.state_dir(product), snap)
        out = facts.repo_facts(product, make_row(), make_index())
        self.assertEqual(out['reservations'], snap)

    def test_no_fetch_and_no_gh_call_reads_it(self):
        calls = []
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return real_run(args, **kwargs)

        with unittest.mock.patch('asf.briefs.facts.subprocess.run', fake_run):
            self.facts_for()
        self.assertFalse([c for c in calls if c[:1] == ['gh']])
        self.assertFalse([c for c in calls if 'fetch' in c])


class RelaunchFactsTests(FactsCase):
    """§2.5/T5: ``repo_facts`` wires ``predecessor``, ``last_progress`` and ``commits_on`` into
    the three new keys, and pays for the log read only when there is a predecessor to read."""

    def ledger(self, *records):
        path = os.path.join(env.state_dir(self.product()), 'sessions.jsonl')
        with open(path, 'w') as f:
            f.writelines(json.dumps(r) + '\n' for r in records)

    def log(self, *texts):
        path = os.path.join(self.tmp, 'run.log')
        with open(path, 'w') as f:
            for text in texts:
                f.write(json.dumps(
                    {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}})
                    + '\n')
        return path

    def test_a_predecessor_on_the_branch_fills_commits_progress_and_relaunch(self):
        self.ledger(
            {'job': 'job-a', 'item': 'T-0001', 'branch': 'fix/B-0001', 'kind': 'PLAN → CODE',
             'started': '2026-01-01T00:00:00Z', 'log': self.log('writing the fix'), 'pid': 1},
            {'job': 'job-a', 'ended': '2026-01-01T01:00:00Z', 'end_reason': 'stopped'})
        out = self.facts_for()
        self.assertEqual(out['relaunch'], {'job': 'job-a', 'ended': '2026-01-01T01:00:00Z',
                                           'end_reason': 'stopped', 'attempt': 1})
        self.assertEqual(out['progress'], 'writing the fix')
        self.assertEqual(out['commits']['total'], 1)
        self.assertTrue(out['commits']['lines'][0].endswith('the fix'), out['commits'])

    def test_no_predecessor_pays_for_no_log_read_though_the_branch_still_lists_its_commits(self):
        out = self.facts_for()
        self.assertEqual(out['relaunch'], {})
        self.assertEqual(out['progress'], '')
        self.assertEqual(out['commits']['total'], 1)   # C2 is not gated on a relaunch (PD5)

    def test_a_live_run_is_not_a_relaunch_and_fills_no_progress(self):
        self.ledger({'job': 'job-a', 'item': 'T-0001', 'branch': 'fix/B-0001', 'kind': 'PLAN → CODE',
                    'started': '2026-01-01T00:00:00Z', 'log': self.log('still going'), 'pid': 1})
        out = self.facts_for()
        self.assertEqual(out['relaunch'], {})
        self.assertEqual(out['progress'], '')


class HeadTests(FactsCase):
    def test_pushed_branch_reads_its_own_head(self):
        sha = _git(['rev-parse', '--short', 'origin/fix/B-0001'], self.repo)
        out = self.facts_for()
        self.assertIs(out['branch_exists'], True)
        self.assertTrue(out['head'].startswith(sha), out['head'])
        self.assertTrue(out['head'].endswith('(origin/fix/B-0001)'), out['head'])

    def test_unpushed_branch_reads_trunk_and_says_so(self):
        out = self.facts_for(branch='fix/B-9999')
        self.assertIs(out['branch_exists'], False)
        self.assertTrue(out['head'].endswith('(origin/main — fix/B-9999 is not on origin yet)'),
                        out['head'])

    def test_pushed_branch_reads_the_sha_ls_remote_reports_not_the_local_ref(self):
        older = _git(['rev-parse', 'origin/fix/B-0001'], self.repo)
        _git(['checkout', '-q', 'fix/B-0001'], self.repo)
        with open(os.path.join(self.repo, 'fix2.txt'), 'w') as f:
            f.write('fix2\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'the second fix'], self.repo)
        _git(['push', '-q', 'origin', 'fix/B-0001'], self.repo)
        newer = _git(['rev-parse', 'origin/fix/B-0001'], self.repo)
        newer_short = _git(['rev-parse', '--short', 'origin/fix/B-0001'], self.repo)
        _git(['checkout', '-q', 'main'], self.repo)
        _git(['update-ref', 'refs/remotes/origin/fix/B-0001', older], self.repo)
        head, exists, rev = facts.head_of(self.repo, 'fix/B-0001', 'main')
        self.assertIs(exists, True)
        self.assertEqual(rev, newer)
        self.assertTrue(head.startswith(newer_short), head)
        self.assertTrue(head.endswith('(origin/fix/B-0001)'), head)

    def test_a_head_the_clone_lacks_says_not_in_this_clone_yet(self):
        origin = os.path.join(self.tmp, 'origin.git')
        other = os.path.join(self.tmp, 'other')
        _git(['clone', '-q', origin, other], self.tmp)
        _git(['config', 'user.email', 'r@example.com'], other)
        _git(['config', 'user.name', 'r'], other)
        _git(['checkout', '-q', 'fix/B-0001'], other)
        with open(os.path.join(other, 'more.txt'), 'w') as f:
            f.write('more\n')
        _git(['add', '-A'], other)
        _git(['commit', '-q', '-m', 'not fetched here'], other)
        _git(['push', '-q', 'origin', 'fix/B-0001'], other)
        head, exists, rev = facts.head_of(self.repo, 'fix/B-0001', 'main')
        self.assertIs(exists, True)
        self.assertEqual(rev, '')
        self.assertTrue(head.endswith('(origin/fix/B-0001 — not in this clone yet)'), head)
        self.assertEqual(facts.commits_on(self.repo, rev, 'main'), {'total': 0, 'lines': []})


class CommitListTests(FactsCase):
    def _commit(self, name, msg):
        with open(os.path.join(self.repo, name), 'w') as f:
            f.write('x\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', msg], self.repo)

    def test_three_commits_above_the_trunk(self):
        for i in range(3):
            self._commit(f'c{i}.txt', f'commit {i}')
        shas = _git(['log', '--format=%h', 'origin/main..HEAD'], self.repo).splitlines()
        out = facts.commits_on(self.repo, 'HEAD', 'main')
        self.assertEqual(out, {'total': 3, 'lines': [f'{shas[0]} commit 2', f'{shas[1]} commit 1',
                                                       f'{shas[2]} commit 0']})

    def test_level_with_the_trunk_is_empty(self):
        self.assertEqual(facts.commits_on(self.repo, 'origin/main', 'main'),
                         {'total': 0, 'lines': []})

    def test_twelve_commits_are_capped_at_the_limit_with_the_true_total(self):
        for i in range(12):
            self._commit(f'd{i}.txt', f'commit {i}')
        out = facts.commits_on(self.repo, 'HEAD', 'main')
        self.assertEqual(out['total'], 12)
        self.assertEqual(len(out['lines']), 10)

    def test_a_rev_that_does_not_resolve_is_empty(self):
        self.assertEqual(facts.commits_on(self.repo, 'deadbeef' * 5, 'main'),
                         {'total': 0, 'lines': []})


class SizeTests(FactsCase):
    def test_named_documents_are_measured(self):
        self.assertEqual(self.facts_for()['files'][SPEC], 42)

    def test_a_missing_document_is_absent_not_zero(self):
        self.assertNotIn(PLAN, self.facts_for()['files'])

    def test_wanted_paths_are_capped_and_ordered(self):
        writes = ['asf/feeder/f%d.py' % i for i in range(19)] + ['README']
        out = self.facts_for(writes=writes, preamble_max_files=3)
        self.assertLessEqual(len(out['files']), 3)
        wanted = preamble_mod.wanted_paths(
            preamble_mod.collect(self.product(), make_row(), make_index(writes)), limit=3)
        self.assertEqual(wanted[:2], [SPEC, PLAN])

    def test_globs_are_never_measured(self):
        out = self.facts_for(writes=['asf/feeder/**'])
        self.assertFalse([p for p in out['files'] if '*' in p])


class ExistingTestTests(FactsCase):
    TREE = ['tests/test_a.py', 'tests/test_b.py', 'asf/feeder/rows.py']

    def test_tests_inside_the_footprint_are_found(self):
        self.assertEqual(facts.tests_under(self.TREE, ['tests/**']),
                         ['tests/test_a.py', 'tests/test_b.py'])
        self.assertEqual(self.facts_for(writes=['tests/**'])['tests'],
                         ['tests/test_a.py', 'tests/test_b.py'])

    def test_tests_outside_it_are_not(self):
        self.assertEqual(facts.tests_under(self.TREE, ['asf/feeder/**']), [])

    def test_limit_holds(self):
        tree = ['tests/test_%s.py' % c for c in 'ihgfedcba']
        self.assertEqual(facts.tests_under(tree, ['tests/**']),
                         ['tests/test_%s.py' % c for c in 'abcdef'])


class LastReportTests(FactsCase):
    def setUp(self):
        super().setUp()
        self.prod = self.product()
        self.path = os.path.join(env.state_dir(self.prod), 'sessions.jsonl')

    def log(self, name, result_text):
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'result': result_text}) + '\n')
        return path

    def ledger(self, *records):
        with open(self.path, 'w') as f:
            f.writelines(json.dumps(r) + '\n' for r in records)

    def run_rec(self, job, ended, log, reason='finished', branch=None):
        rec = {'job': job, 'item': 'B-0001', 'started': '2026-01-01T00:00:00Z', 'pid': 1,
               'log': log}
        if branch:
            rec['branch'] = branch
        recs = [rec]
        if ended:
            recs.append({'job': job, 'ended': ended, 'end_reason': reason})
        return recs

    def test_the_newest_ended_session_report_is_carried(self):
        old = self.log('old.log', report_mod.render('coder', status='partial', pushed='no',
                                                      why='still running'))
        new = self.log('new.log', 'lots of transcript\n\n' +
                       report_mod.render('coder', status='done', pushed='yes', sha='abc'))
        self.ledger(*self.run_rec('job-old', '2026-01-01T01:00:00Z', old),
                    *self.run_rec('job-new', '2026-01-02T01:00:00Z', new))
        text = facts.last_report(self.prod, 'B-0001')
        self.assertIn('job-new ended 2026-01-02T01:00:00Z — finished', text)
        self.assertIn('status: done', text)
        self.assertIn('pushed: yes', text)
        self.assertNotIn('transcript', text)
        self.assertNotIn('job-old', text)

    def test_a_running_session_is_not_a_report(self):
        self.ledger(*self.run_rec('job-a', None, self.log('a.log', 'REPORT\nstatus: done')))
        self.assertEqual(facts.last_report(self.prod, 'B-0001'), '')

    def test_a_missing_log_falls_back_to_the_ledger_line(self):
        log = self.log('gone.log', 'REPORT\nstatus: done')
        self.ledger(*self.run_rec('job-a', '2026-01-02T01:00:00Z', log))
        os.remove(log)
        self.assertEqual(facts.last_report(self.prod, 'B-0001'),
                         'job-a ended 2026-01-02T01:00:00Z — finished')

    def test_a_field_is_capped(self):
        log = self.log('big.log', report_mod.render('coder', left_out=['x' * 5000]))
        self.ledger(*self.run_rec('job-a', '2026-01-02T01:00:00Z', log))
        text = facts.last_report(self.prod, 'B-0001')
        self.assertIn('left out: xxx', text)
        self.assertLessEqual(max(len(l) for l in text.splitlines()), 200)

    def test_a_branch_given_ignores_other_branches_and_takes_the_newest_here(self):
        other = self.log('other.log', report_mod.render(
            'coder', tests=[{'command': 'x', 'last_line': 'other'}]))
        here = self.log('here.log', report_mod.render(
            'coder', tests=[{'command': 'x', 'last_line': 'here'}]))
        self.ledger(*self.run_rec('job-other', '2026-01-03T00:00:00Z', other,
                                   branch='fix/B-9999'),
                    *self.run_rec('job-here', '2026-01-02T00:00:00Z', here,
                                   branch='fix/B-0001'))
        text = facts.last_report(self.prod, 'B-0001', branch='fix/B-0001')
        self.assertIn('job-here', text)
        self.assertIn('here', text)
        self.assertNotIn('job-other', text)
        self.assertNotIn('other', text)

    def test_a_run_with_no_report_falls_back_to_the_branch_review_verdict(self):
        log = self.log('crashed.log', 'a crash, no REPORT block here')
        self.ledger(*self.run_rec('job-a', '2026-01-02T00:00:00Z', log, branch='fix/B-0001'))
        review = (2, 'docs/reviews/fix-b-0001-r2.md', 'Verdict: approved')
        text = facts.last_report(self.prod, 'B-0001', branch='fix/B-0001', review=review)
        self.assertIn('job-a ended', text)
        self.assertIn('review round 2 (docs/reviews/fix-b-0001-r2.md): verdict approved', text)

    def test_a_report_already_present_ignores_the_review_fallback(self):
        log = self.log('ok.log', report_mod.render('coder', status='done', pushed='yes', sha='abc'))
        self.ledger(*self.run_rec('job-a', '2026-01-02T00:00:00Z', log, branch='fix/B-0001'))
        review = (1, 'docs/reviews/fix-b-0001-r1.md', 'Verdict: approved')
        text = facts.last_report(self.prod, 'B-0001', branch='fix/B-0001', review=review)
        self.assertNotIn('review round', text)

    def test_a_branch_with_neither_a_run_nor_a_review_adds_nothing(self):
        self.ledger()
        self.assertEqual(facts.last_report(self.prod, 'B-0001', branch='fix/B-0001'), '')


class PredecessorTests(FactsCase):
    def setUp(self):
        super().setUp()
        self.prod = self.product()
        self.path = os.path.join(env.state_dir(self.prod), 'sessions.jsonl')

    def ledger(self, *records):
        with open(self.path, 'w') as f:
            f.writelines(json.dumps(r) + '\n' for r in records)

    def launch(self, job, item, branch, kind, started):
        return {'job': job, 'item': item, 'branch': branch, 'kind': kind, 'started': started,
                'pid': 1}

    def end(self, job, ended, reason='finished'):
        return {'job': job, 'ended': ended, 'end_reason': reason}

    def test_the_newest_ended_unlanded_run_is_returned_with_its_attempt(self):
        self.ledger(self.launch('job-a', 'B-0001', 'fix/B-0001', 'coder', '2026-01-01T00:00:00Z'),
                    self.end('job-a', '2026-01-01T01:00:00Z'),
                    self.launch('job-a', 'B-0001', 'fix/B-0001', 'coder', '2026-01-02T00:00:00Z'),
                    self.end('job-a', '2026-01-02T01:00:00Z'))
        self.assertEqual(facts.predecessor(self.prod, 'B-0001', 'fix/B-0001', 'coder'),
                         {'job': 'job-a', 'ended': '2026-01-02T01:00:00Z',
                          'end_reason': 'finished', 'attempt': 2})

    def test_a_live_run_is_not_a_predecessor(self):
        self.ledger(self.launch('job-a', 'B-0001', 'fix/B-0001', 'coder', '2026-01-01T00:00:00Z'))
        self.assertEqual(facts.predecessor(self.prod, 'B-0001', 'fix/B-0001', 'coder'), {})

    def test_a_landed_run_is_not_a_predecessor(self):
        self.ledger(self.launch('job-a', 'B-0001', 'fix/B-0001', 'coder', '2026-01-01T00:00:00Z'),
                    self.end('job-a', '2026-01-01T01:00:00Z'),
                    {'job': 'job-a', 'harvested': 'deadbeef'})
        self.assertEqual(facts.predecessor(self.prod, 'B-0001', 'fix/B-0001', 'coder'), {})

    def test_a_run_of_another_kind_on_the_same_branch_is_not_a_predecessor(self):
        self.ledger(self.launch('job-r', 'B-0001', 'fix/B-0001', 'review', '2026-01-01T00:00:00Z'),
                    self.end('job-r', '2026-01-01T01:00:00Z'))
        self.assertEqual(facts.predecessor(self.prod, 'B-0001', 'fix/B-0001', 'coder'), {})

    def test_a_run_of_the_same_kind_on_another_branch_is_not_a_predecessor(self):
        self.ledger(self.launch('job-a', 'B-0001', 'fix/B-9999', 'coder', '2026-01-01T00:00:00Z'),
                    self.end('job-a', '2026-01-01T01:00:00Z'))
        self.assertEqual(facts.predecessor(self.prod, 'B-0001', 'fix/B-0001', 'coder'), {})

    def test_an_item_with_no_run_at_all_is_empty(self):
        self.ledger()
        self.assertEqual(facts.predecessor(self.prod, 'B-0001', 'fix/B-0001', 'coder'), {})


class ProgressLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='progress_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def log(self, name, records):
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            for rec in records:
                f.write(json.dumps(rec) + '\n')
        return path

    def assistant(self, *texts, extra=''):
        rec = {'type': 'assistant',
               'message': {'content': [{'type': 'text', 'text': t} for t in texts]}}
        if extra:
            rec['pad'] = extra
        return rec

    def test_the_last_blocks_first_non_empty_line_is_taken(self):
        path = self.log('a.log', [
            {'type': 'system', 'subtype': 'init'},
            self.assistant('first block\nmore', '\n  second block first line  \nsecond line'),
        ])
        self.assertEqual(facts.last_progress(path), 'second block first line')

    def test_a_result_after_the_last_turn_still_finds_it(self):
        path = self.log('b.log', [
            self.assistant('doing the thing'),
            {'type': 'result', 'result': 'REPORT\nstatus: done'},
        ])
        self.assertEqual(facts.last_progress(path), 'doing the thing')

    def test_a_long_line_is_capped(self):
        path = self.log('c.log', [self.assistant('x' * 5000)])
        out = facts.last_progress(path)
        self.assertLessEqual(len(out), facts.FIELD_CAP)
        self.assertTrue(out.endswith('…'))

    def test_a_large_log_finds_the_turn_inside_the_tail(self):
        path = self.log('d.log', [
            self.assistant('too old to matter here', extra='x' * 5000),
            self.assistant('inside the tail'),
        ])
        self.assertEqual(facts.last_progress(path, tail=200), 'inside the tail')

    def test_a_turn_before_the_tail_is_not_seen(self):
        path = self.log('e.log', [
            self.assistant('too old to see'),
            {'type': 'system', 'subtype': 'init', 'pad': 'x' * 4096},
        ])
        self.assertEqual(facts.last_progress(path, tail=64), '')

    def test_a_missing_log_is_empty(self):
        self.assertEqual(facts.last_progress(os.path.join(self.tmp, 'nope.log')), '')

    def test_an_empty_log_is_empty(self):
        self.assertEqual(facts.last_progress(self.log('empty.log', [])), '')

    def test_pure_garbage_is_empty(self):
        path = os.path.join(self.tmp, 'garbage.log')
        with open(path, 'w') as f:
            f.write('not json at all\n{"broken":\n')
        self.assertEqual(facts.last_progress(path), '')

    def test_no_assistant_turn_is_empty(self):
        path = self.log('no_turn.log', [{'type': 'system', 'subtype': 'init'},
                                         {'type': 'result', 'result': 'REPORT\nstatus: done'}])
        self.assertEqual(facts.last_progress(path), '')


class PreambleIsCodeTests(unittest.TestCase):
    FORBIDDEN = ('http', 'urllib', 'socket', 'requests')

    def imports(self, name):
        with open(os.path.join(REPO_ROOT, 'asf', 'briefs', name), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                out.append(node.module or '')
        return tree, out

    def test_no_model_client_is_imported(self):
        for name in ('preamble.py', 'facts.py'):
            _tree, mods = self.imports(name)
            bad = [m for m in mods if m.split('.')[0] in self.FORBIDDEN]
            self.assertEqual(bad, [], name)

    def test_preamble_never_runs_git(self):
        tree, mods = self.imports('preamble.py')
        self.assertNotIn('subprocess', [m.split('.')[0] for m in mods])
        popen = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == 'popen'
                 and isinstance(n.value, ast.Name) and n.value.id == 'os']
        self.assertEqual(popen, [])

    def test_the_text_is_deterministic(self):
        product, row, index = Product('sample', {'main': 'main'}), make_row(), make_index(['a.py'])
        given = {'head': 'abc1234 x', 'branch_exists': True, 'files': {SPEC: 42},
                 'tests': [], 'last_report': ''}
        first = preamble_mod.build(product, row, index, [], given)
        self.assertEqual(first, preamble_mod.build(product, row, index, [], given))
        preamble_mod.collect(product, row, index, [], given)
        self.assertEqual(first, preamble_mod.build(product, row, index, [], given))


if __name__ == '__main__':
    unittest.main()


class SubjectRuleTest(unittest.TestCase):
    def test_each_kind_names_its_item_in_the_subject(self):
        for kind, prefix in (('spec', 'spec'), ('plan', 'plan'), ('fix-bug', 'fix'), ('coder', 'task')):
            row = Row(tier=2, kind='X', item_id='F-0007', feature_id='F-0007', action='launch',
                      brief_kind=kind, branch='b', reason='r')
            line = preamble_mod.subject_rule(row, {'id': 'F-0007'})
            self.assertIn(f'`{prefix}(F-0007): <what>`', line)
            self.assertIn('branch name does not count', line)
