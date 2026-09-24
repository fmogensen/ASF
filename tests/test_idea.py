import glob
import json
import os
import shutil
import unittest

from asf.idea.answered import Answered, answer_from_record
from asf.idea.tree import Node, TreeError, parse_tree
from asf.record.core import canonicalize, load_items
from tests.test_groom import make_repo, run, write_item

ASK = 'Bill customers for what they actually use.'

TREE = f"""# The ask
{ASK}

---

## Epic E1: Metered billing

Charging for usage, end to end: the meter, the invoice and the statement a customer can read.

### Assumptions
- one meter per account, not per API key — E-0001 already parents billing to the account

---

## Feature F1 under E1: The usage meter

Every request is counted against the account that made it, durably enough to bill from.

### Assumptions
- counts are kept per day

### Acceptance
- [ ] `python3 -m unittest tests.test_meter -v` is green
- [ ] a replayed request is counted once

---

## Feature F2 under E1: The invoice

One invoice per account per month.

### Acceptance
- [ ] an invoice sums the month's meter

---

## Story S1 under F1: A request increments its account's meter

### Acceptance
- [ ] `python3 -m unittest tests.test_meter.IncrementTest -v`
"""


def seed(root, feature_state=None):
    write_item(root, 'E-0001', 'epic', 'Factory billing', typed_lines=['decided: yes'])
    machine = None
    if feature_state:
        machine = [f'state: {feature_state}', 'stage_since: 2026-09-01T00:00:00Z',
                   'updated: 2026-09-01T00:00:00Z']
    write_item(root, 'F-0001', 'feature', 'Approval classes', parent='E-0001',
               machine_lines=machine)


def canonical_of(root):
    return canonicalize(load_items(root)[0])[0]


def node(type_, title):
    return Node(type_, 'X1', None, title, '', [], [])


class TreeParseTest(unittest.TestCase):
    def test_a_three_level_tree_parses(self):
        tree = parse_tree(TREE)
        by_key = {n.key: n for n in tree.nodes}
        self.assertEqual([n.key for n in tree.nodes], ['E1', 'F1', 'F2', 'S1'])
        self.assertEqual(by_key['S1'].parent_key, 'F1')
        self.assertEqual(by_key['F1'].acceptance,
                         ['`python3 -m unittest tests.test_meter -v` is green',
                          'a replayed request is counted once'])
        self.assertEqual(len(by_key['E1'].assumptions), 1)
        self.assertTrue(by_key['F1'].description.startswith('Every request is counted'))

    def test_the_ask_is_kept(self):
        self.assertEqual(parse_tree(TREE).ask, ASK)

    def assertRefused(self, text, phrase):
        with self.assertRaises(TreeError) as caught:
            parse_tree(text)
        self.assertIn(phrase, str(caught.exception))

    def test_a_block_that_is_no_node_is_refused(self):
        self.assertRefused(TREE + "\n---\n\n## Chore C1: x\n", 'not a node header')

    def test_duplicate_key(self):
        self.assertRefused(TREE.replace('Feature F2 under', 'Feature F1 under'),
                           'key F1 is already used at line')

    def test_unknown_under(self):
        self.assertRefused(TREE.replace('Story S1 under F1', 'Story S1 under F9'),
                           'under F9 — no such key')

    def test_story_under_an_epic(self):
        self.assertRefused(TREE.replace('Story S1 under F1', 'Story S1 under E1'),
                           'a Story hangs under a Feature')

    def test_a_feature_without_an_epic(self):
        self.assertRefused(TREE.replace('Feature F2 under E1', 'Feature F2'),
                           'a Feature hangs under an Epic')

    def test_an_epic_under_something(self):
        self.assertRefused(TREE.replace('Epic E1:', 'Epic E0 under E1:'), 'an Epic hangs under nothing')

    def test_epic_with_one_feature(self):
        text = TREE.replace('Feature F2 under E1', 'Feature F2 under E1')
        one = text[:text.index('---\n\n## Feature F2')] + text[text.index('---\n\n## Story S1'):]
        self.assertRefused(one, 'an Epic is a business outcome spanning several Features')

    def test_story_without_acceptance(self):
        text = TREE[:TREE.index('### Acceptance', TREE.index('Story S1'))]
        self.assertRefused(text, 'a Story is one PR with one acceptance list')

    def test_a_questions_section_is_refused(self):
        self.assertRefused(TREE + "\n### Questions\n- what about tax?\n",
                           'an interrogation proposes, it does not ask')

    def test_the_tree_must_open_with_the_ask(self):
        self.assertRefused(TREE[TREE.index('---'):], 'the tree opens with # The ask')


class AnsweredFromTheRecordTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        seed(self.root)

    def test_a_feature_the_record_already_carries_is_answered(self):
        answered = answer_from_record(node('Feature', 'Approval classes'), canonical_of(self.root))
        self.assertIsInstance(answered, Answered)
        self.assertEqual(answered.source, 'F-0001')
        self.assertEqual(answered.text, 'F-0001 — Approval classes (feature, New)')

    def test_a_closed_item_does_not_answer(self):
        seed(self.root, feature_state='Closed')
        self.assertIsNone(answer_from_record(node('Feature', 'Approval classes'),
                                             canonical_of(self.root)))

    def test_a_different_type_does_not_answer(self):
        self.assertIsNone(answer_from_record(node('Epic', 'Approval classes'),
                                             canonical_of(self.root)))

    def test_below_the_overlap_is_not_answered(self):
        self.assertIsNone(answer_from_record(node('Feature', 'Approval classes for billing plans'),
                                             canonical_of(self.root)))

    def test_a_lower_overlap_lets_it_answer(self):
        answered = answer_from_record(node('Feature', 'Approval classes for billing plans'),
                                      canonical_of(self.root), overlap=0.4)
        self.assertEqual(answered.source, 'F-0001')


class ApplyTreeTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        seed(self.root)
        self.tree = os.path.join(self.root, 'idea.tree.md')
        self.write_tree(TREE)

    def write_tree(self, text):
        with open(self.tree, 'w', encoding='utf-8') as f:
            f.write(text)

    def apply(self, *extra):
        return run(['idea', 'apply', '--tree', self.tree, *extra], self.root)

    def inbox(self):
        return sorted(n for n in os.listdir(os.path.join(self.root, 'inbox')) if n.endswith('.md'))

    def read(self, name):
        with open(os.path.join(self.root, 'inbox', name), encoding='utf-8') as f:
            return f.read()

    def named(self, marker):
        return next(n for n in self.inbox() if marker in n)

    def test_files_one_card_per_node_in_order(self):
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        names = self.inbox()
        self.assertEqual(len(names), 4)
        for name, marker in zip(names, ['-01-epic-', '-02-feature-', '-03-feature-', '-04-story-']):
            self.assertTrue(name.startswith('idea-') and marker in name, name)
        self.assertIn('next: asf groom', r.stdout)

    def test_the_epic_card_carries_its_features_and_no_parent(self):
        self.apply()
        text = self.read(self.named('-01-epic-'))
        self.assertNotIn('parent:', text)
        self.assertIn('## Features\n- The usage meter\n- The invoice\n', text)

    def test_a_child_points_at_its_parent_file(self):
        self.apply()
        epic = self.named('-01-epic-')
        self.assertTrue(epic.endswith('-01-epic-metered-billing.md'))
        self.assertIn(f'parent: inbox:{epic}\n', self.read(self.named('-02-feature-')))
        self.assertIn(f'parent: inbox:{self.named("-02-feature-")}\n',
                      self.read(self.named('-04-story-')))

    def test_assumptions_and_acceptance_are_sections(self):
        self.apply()
        text = self.read(self.named('-02-feature-'))
        self.assertIn('## Assumptions\n- counts are kept per day\n', text)
        self.assertIn('## Acceptance\n- [ ] `python3 -m unittest tests.test_meter -v` is green\n'
                      '- [ ] a replayed request is counted once\n', text)

    def test_an_answered_node_is_not_filed(self):
        self.write_tree(TREE.replace('The invoice', 'Approval classes'))
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('answered F-0001', r.stdout)
        self.assertEqual(len(self.inbox()), 3)
        self.assertFalse([n for n in self.inbox() if 'approval-classes' in n])

    def test_an_answered_nodes_stories_go_with_it(self):
        self.write_tree(TREE.replace('The usage meter', 'Approval classes'))
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([n.split('-')[2] for n in self.inbox()], ['01', '03'])
        with open(self.tree + '.applied', encoding='utf-8') as f:
            applied = json.load(f)
        self.assertEqual([a['node'] for a in applied['answered']], ['F1', 'S1'])
        self.assertEqual({a['id'] for a in applied['answered']}, {'F-0001'})

    def test_everything_answered_is_a_result_not_a_failure(self):
        write_item(self.root, 'E-0002', 'epic', 'Metered billing', typed_lines=['decided: yes'])
        self.write_tree(TREE.replace('The usage meter', 'Approval classes'))
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('answered E-0002', r.stdout)

    def test_nothing_is_written_when_one_node_fails(self):
        before = self.inbox()
        self.write_tree(TREE[:TREE.index('### Acceptance', TREE.index('Story S1'))])
        r = self.apply()
        self.assertEqual(r.returncode, 2)
        self.assertIn('a Story is one PR with one acceptance list', r.stderr)
        self.assertEqual(self.inbox(), before)
        self.assertFalse(os.path.exists(self.tree + '.applied'))

    def test_a_missing_tree_is_refused(self):
        os.remove(self.tree)
        r = self.apply()
        self.assertEqual(r.returncode, 2)
        self.assertIn('no tree at', r.stderr)

    def test_the_report_keeps_the_whole_tree(self):
        self.apply()
        reports = glob.glob(os.path.join(self.root, 'inbox', 'done', 'idea-*.md'))
        self.assertEqual(len(reports), 1)
        with open(reports[0], encoding='utf-8') as f:
            report = f.read()
        self.assertIn(ASK, report)
        for key in ('E1', 'F1', 'F2', 'S1'):
            self.assertIn(f'- {key} ', report)
        self.assertEqual(report.count('filed idea-'), 4)
        self.assertIn(TREE.rstrip('\n'), report)

    def test_the_applied_file_names_the_run(self):
        self.apply()
        with open(self.tree + '.applied', encoding='utf-8') as f:
            applied = json.load(f)
        self.assertEqual(sorted(applied), ['answered', 'filed', 'report', 'stamp'])
        self.assertEqual(applied['filed'], self.inbox())
        self.assertTrue(os.path.isfile(os.path.join(self.root, applied['report'])))

    def test_json_prints_the_applied_file(self):
        r = self.apply('--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['filed'], self.inbox())

    def test_a_second_apply_is_refused(self):
        self.apply()
        before = self.inbox()
        r = self.apply()
        self.assertEqual(r.returncode, 2)
        self.assertIn('already applied', r.stderr)
        self.assertEqual(self.inbox(), before)
        r = self.apply('--force')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.inbox()), 8)


if __name__ == '__main__':
    unittest.main()
