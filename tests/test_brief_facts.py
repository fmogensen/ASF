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
import unittest

from asf import env
from asf.briefs import facts
from asf.briefs import preamble as preamble_mod
from asf.env import Product
from asf.feeder.rows import Row

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

    def run_rec(self, job, ended, log, reason='finished'):
        recs = [{'job': job, 'item': 'B-0001', 'started': '2026-01-01T00:00:00Z', 'pid': 1,
                 'log': log}]
        if ended:
            recs.append({'job': job, 'ended': ended, 'end_reason': reason})
        return recs

    def test_the_newest_ended_session_report_is_carried(self):
        old = self.log('old.log', 'REPORT\nstatus: partial\npushed: no')
        new = self.log('new.log', 'lots of transcript\nREPORT\nstatus: done\npushed: yes abc')
        self.ledger(*self.run_rec('job-old', '2026-01-01T01:00:00Z', old),
                    *self.run_rec('job-new', '2026-01-02T01:00:00Z', new))
        text = facts.last_report(self.prod, 'B-0001')
        self.assertIn('job-new ended 2026-01-02T01:00:00Z — finished', text)
        self.assertIn('status: done', text)
        self.assertIn('pushed: yes abc', text)
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
        log = self.log('big.log', 'REPORT\nstatus: done\nleft out: ' + 'x' * 5000)
        self.ledger(*self.run_rec('job-a', '2026-01-02T01:00:00Z', log))
        text = facts.last_report(self.prod, 'B-0001')
        self.assertIn('left out: xxx', text)
        self.assertLessEqual(max(len(l) for l in text.splitlines()), 200)


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
