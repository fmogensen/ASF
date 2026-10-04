"""asf.groom.policy — ``flags.groom_rules`` (W8-PR4a): one open Bug per finding's cause, one open
scorecard card per cause class, and a covered Feature reported with a verify Task, never closed.
"""
import datetime
import os
import shutil
import subprocess
import unittest

from asf import hermetic
from asf.env import Product
from asf.groom import policy
from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, load_items
from tests.test_groom import make_repo, write_item

ALL = list(policy.GROOM_RULES)


def product(rules=None, repo_dir=None):
    data = {'conventions': {'flags': {} if rules is None else {'groom_rules': rules}}}
    if repo_dir:
        data['repo_dir'] = repo_dir
    return Product('sample', data)


def body(evidence=(), extra='', created='2026-09-01'):
    ev = ''.join(f'- {e}\n' for e in evidence)
    return (f"## Description\nwhat\n\nEvidence:\n{ev}\n{extra}\n\n## Acceptance\n- [ ] \n\n"
            f"## Non-goals\n\n## History\n- {created}: created\n\n## Children\n\n## Backlinks\n")


def finding_bug(root, iid, sig, evidence, typed=(), machine=None):
    write_item(root, iid, 'bug', f'Invariant {iid}', parent='E-0001',
               typed_lines=[f'signature: "{sig}"', 'decided: false', *typed],
               machine_lines=machine, body=body(evidence))


def scorecard_card(root, iid, key, machine=None):
    write_item(root, iid, 'feature', f'Sessions die with {key}', parent='E-0001',
               typed_lines=['decided: true'], machine_lines=machine,
               body=body(extra=f'scorecard-cause: {key} #1'))


def meta(root, folder, iid):
    with open(os.path.join(root, folder, f'{iid}.md'), encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{folder}/{iid}.md')


class RulesFlagTests(unittest.TestCase):

    def test_unset_is_no_rule(self):
        self.assertEqual(policy.groom_rules(product()), frozenset())
        self.assertEqual(policy.groom_rules(None), frozenset())

    def test_a_list_or_a_string_names_the_rules_and_unknown_names_are_dropped(self):
        self.assertEqual(policy.groom_rules(product(['dedupe-findings', 'nope'])),
                         frozenset({'dedupe-findings'}))
        self.assertEqual(policy.groom_rules(product('dedupe-scorecard, report-covered')),
                         frozenset({'dedupe-scorecard', 'report-covered'}))

    def test_groom_rules_is_a_known_flag(self):
        from asf import conventions
        self.assertIn('groom_rules', conventions.KNOWN_FLAGS)


class DedupeTests(unittest.TestCase):

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['decided: true'])

    def canonical(self):
        canonical, _d = canonicalize(load_items(self.root)[0])
        return canonical, compute_derived(canonical)

    def test_an_old_path_keyed_finding_and_a_cause_keyed_one_share_a_key(self):
        finding_bug(self.root, 'B-0001', 'invariant I10: features/F-0093.md',
                    ['F-0093: Resolved with open Task(s) T-0183, T-0187'])
        finding_bug(self.root, 'B-0002', 'invariant I10: features/F-0173.md',
                    ['F-0173: Resolved with open Task(s) T-0547'])
        finding_bug(self.root, 'B-0003', 'invariant I10: Resolved with open Task(s) …',
                    ['features/F-0200.md — F-0200: Resolved with open Task(s) T-0900'])
        finding_bug(self.root, 'B-0004', 'invariant I10: Closed with an open child …',
                    ['features/F-0201.md — F-0201: Closed with an open child T-0901'])
        canonical, _d = self.canonical()
        keys = {i: policy.dedupe_key(canonical[i], {'dedupe-findings'})[0]
                for i in ('B-0001', 'B-0002', 'B-0003', 'B-0004')}
        self.assertEqual(keys['B-0001'], keys['B-0002'])
        self.assertEqual(keys['B-0001'], keys['B-0003'])
        self.assertNotEqual(keys['B-0001'], keys['B-0004'])

    def test_younger_findings_of_one_cause_close_as_duplicates_of_the_oldest(self):
        finding_bug(self.root, 'B-0001', 'invariant I10: features/F-0093.md',
                    ['F-0093: Resolved with open Task(s) T-0183'], typed=['decided: true'])
        finding_bug(self.root, 'B-0002', 'invariant I10: features/F-0173.md',
                    ['F-0173: Resolved with open Task(s) T-0547'])
        finding_bug(self.root, 'B-0003', 'invariant I10: features/F-0174.md',
                    ['F-0174: Resolved with open Task(s) T-0548'],
                    typed=['links: {branches: [fix/b-0003]}'])
        write_item(self.root, 'B-0004', 'bug', 'Something else', parent='E-0001',
                   typed_lines=['signature: "spawn: x"'])
        said = []
        done = policy.apply_groom_rules(product(['dedupe-findings']), self.root, out=said.append)
        self.assertEqual(done, [('B-0002', 'close')])
        m, b = meta(self.root, 'bugs', 'B-0002')
        self.assertTrue(m['removed'].startswith('duplicate of B-0001 (invariant I10: Resolved'))
        self.assertEqual(b.count('groom: removed → duplicate of B-0001'), 1)
        self.assertNotIn('removed', meta(self.root, 'bugs', 'B-0001')[0])
        self.assertNotIn('removed', meta(self.root, 'bugs', 'B-0003')[0])   # has a branch
        self.assertNotIn('removed', meta(self.root, 'bugs', 'B-0004')[0])
        # a second pass changes nothing
        self.assertEqual(policy.apply_groom_rules(product(['dedupe-findings']), self.root,
                                                  out=said.append), [])

    def test_scorecard_cards_of_one_class_fold_into_the_oldest_and_a_started_one_stays(self):
        scorecard_card(self.root, 'F-0001', 'failure:failed: not pushed',
                       machine=['state: Active', 'stage: building 1/2',
                                'stage_since: 2026-09-01T00:00:00Z'])
        scorecard_card(self.root, 'F-0002', 'failure:failed: unpushed work')
        scorecard_card(self.root, 'F-0003', 'failure:dead pid',
                       machine=['state: Active', 'stage: plan-approved',
                                'stage_since: 2026-09-01T00:00:00Z'])
        scorecard_card(self.root, 'F-0004', 'gate:tests red')
        said = []
        done = policy.apply_groom_rules(product(['dedupe-scorecard']), self.root,
                                        out=said.append, dry_run=True)
        self.assertEqual(done, [('F-0002', 'close')])
        self.assertIn('groom rules: would close F-0002 — duplicate of F-0001 (scorecard class '
                      'failure)', said)
        self.assertTrue(any(l.startswith('groom rules: keeps F-0003 — same key as F-0001, but '
                                         'started (Active)') for l in said), said)
        self.assertNotIn('removed', meta(self.root, 'features', 'F-0002')[0])   # dry run
        policy.apply_groom_rules(product(['dedupe-scorecard']), self.root, out=said.append)
        self.assertIn('removed', meta(self.root, 'features', 'F-0002')[0])
        self.assertNotIn('removed', meta(self.root, 'features', 'F-0004')[0])

    def test_rules_off_write_and_print_nothing(self):
        finding_bug(self.root, 'B-0001', 'invariant I10: features/F-0093.md', ['F-0093: x'])
        finding_bug(self.root, 'B-0002', 'invariant I10: features/F-0094.md', ['F-0094: x'])
        said = []
        for p in (product(), product([]), product(['unknown'])):
            self.assertEqual(policy.apply_groom_rules(p, self.root, out=said.append), [])
        self.assertEqual(said, [])
        self.assertNotIn('removed', meta(self.root, 'bugs', 'B-0002')[0])

    def test_close_exact_duplicate_answers_on_the_key_whatever_the_titles(self):
        finding_bug(self.root, 'B-0001', 'invariant I10: features/F-0093.md', ['F-0093: x'])
        finding_bug(self.root, 'B-0002', 'invariant I10: features/F-0094.md', ['F-0094: x'])
        canonical, derived = self.canonical()
        off = policy.Ctx(date='2026-10-04', now=None, duplicate_overlap=0.99)
        on = policy.Ctx(date='2026-10-04', now=None, duplicate_overlap=0.99,
                        groom_rules=frozenset({'dedupe-findings'}))
        self.assertIsNone(policy.close_exact_duplicate('B-0002', canonical['B-0002'], canonical,
                                                       derived, off))
        ans = policy.close_exact_duplicate('B-0002', canonical['B-0002'], canonical, derived, on)
        self.assertEqual((ans.word, ans.field), ('no', 'removed'))
        self.assertTrue(ans.why.startswith('duplicate of B-0001'), ans.why)


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env=hermetic.git_env())


class CoveredReportTests(unittest.TestCase):

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Covered', parent='E-0001',
                   typed_lines=['decided: true'],
                   machine_lines=['state: Active', 'stage: building 0/2',
                                  'stage_since: 2026-09-01T00:00:00Z'])
        for tid, w in (('T-0001', 'a.py'), ('T-0002', 'b.py')):
            write_item(self.root, tid, 'task', f'Task {tid}', parent='F-0001',
                       typed_lines=[f'writes: [{w}]'])

    def commits(self, *entries):
        return lambda day: [(sha, '2026-09-10', subj, paths) for sha, subj, paths in entries]

    def test_a_covered_feature_gets_one_verify_task_and_nothing_closes(self):
        cf = self.commits(('a' * 40, 'fix(x): a', ['a.py']), ('b' * 40, 'fix(y): b', ['b.py']))
        said = []
        done = policy.apply_groom_rules(product(['report-covered']), self.root, out=said.append,
                                        commits_for=cf)
        self.assertEqual(done, [('F-0001', 'report')])
        verify = [i for i in os.listdir(os.path.join(self.root, 'tasks'))
                  if i not in ('T-0001.md', 'T-0002.md')]
        self.assertEqual(verify, ['T-0003.md'])
        m, b = meta(self.root, 'tasks', 'T-0003')
        self.assertEqual((m['parent'], m['state'], list(m['writes'])), ('F-0001', 'New',
                                                                         ['a.py', 'b.py']))
        self.assertIn(f'{policy.COVERED_MARK} F-0001', b)
        self.assertIn('aaaaaaaaa', b)
        for tid in ('T-0001', 'T-0002'):
            tm = meta(self.root, 'tasks', tid)[0]
            self.assertEqual(tm['state'], 'New')
            self.assertNotIn('removed', tm)
            self.assertNotIn('landed', tm)
        self.assertEqual(policy.apply_groom_rules(product(['report-covered']), self.root,
                                                  out=said.append, commits_for=cf), [])

    def test_one_uncovered_task_or_a_commit_naming_an_item_is_no_report(self):
        for cf in (self.commits(('a' * 40, 'fix: a', ['a.py'])),
                   self.commits(('a' * 40, 'fix: a', ['a.py']),
                                ('b' * 40, 'task(T-0099): b', ['b.py'])),
                   lambda day: None):
            said = []
            self.assertEqual(policy.apply_groom_rules(product(['report-covered']), self.root,
                                                      out=said.append, commits_for=cf), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, 'tasks'))),
                         ['T-0001.md', 'T-0002.md'])

    def test_trunk_commits_reads_origin_main_with_each_commits_paths(self):
        repo = make_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        _git(repo, 'init', '-q', '-b', 'main')
        for name in ('a.py', 'b.py'):
            with open(os.path.join(repo, name), 'w') as f:
                f.write('x\n')
            _git(repo, 'add', name)
            _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m',
                 f'fix: {name}')
        _git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        today = datetime.date.today().isoformat()
        got = policy.trunk_commits(repo, 'main', '2000-01-01')
        self.assertEqual([(s, p) for _sha, _d, s, p in got],
                         [('fix: b.py', ['b.py']), ('fix: a.py', ['a.py'])])
        self.assertEqual(got[0][1], today)
        self.assertIsNone(policy.trunk_commits(repo, 'nope', '2000-01-01'))


if __name__ == '__main__':
    unittest.main()
