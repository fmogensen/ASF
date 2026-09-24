import contextlib
import glob
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import cli, env
from asf.record.core import parse_sections, today
from asf.workers import runtime as runtime_mod

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


class SessionWritesTheTree(runtime_mod.FakeRuntime):
    """The fake session: it reads its brief for the tree path it is told to write and writes
    ``text`` there (or nothing, when ``text`` is None)."""

    def __init__(self, text):
        super().__init__([{'ok': True, 'result': 'wrote the tree'}])
        self.text = text

    def run(self, job, wait=False):
        result = super().run(job, wait=wait)
        if self.text is not None:
            with open(self.calls[-1][1].split('THE ONE FILE YOU WRITE IS `')[1].split('`')[0],
                      'w', encoding='utf-8') as f:
                f.write(self.text)
        return result


class FrontDoorTest(unittest.TestCase):
    """``asf idea "<text>"`` in-process, with ``worker_pool: {backend: fake}`` in the config."""

    def setUp(self):
        self.root = make_repo()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='idea_home_'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed(self.root)
        home, self._home, self._cwd = os.path.join(self.tmp, 'home'), env.ASF_HOME, os.getcwd()
        os.makedirs(os.path.join(home, 'products'))
        env.ASF_HOME = home
        self.addCleanup(setattr, env, 'ASF_HOME', self._home)
        self.addCleanup(os.chdir, self._cwd)
        with open(os.path.join(home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write('default_product: sample\nscheduler:\n  kind: none\nworker_pool:\n  backend: fake\n')
        with open(os.path.join(home, 'products', 'sample.yaml'), 'w', encoding='utf-8') as f:
            f.write(f'product: sample\nrepo_dir: {self.tmp}\nbacklog_dir: {self.root}\nmain: main\n'
                    'ci:\n  provider: none\nsteps:\n  batch: off\n  daily: off\n')
        os.chdir(self.root)
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('ASF_PRODUCT', None)

    def door(self, text, *extra, tree_text=TREE):
        fake = SessionWritesTheTree(tree_text)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(runtime_mod, 'from_config', return_value=fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(['idea', text, *extra])
        return rc, out.getvalue(), err.getvalue(), fake

    def inbox(self):
        return sorted(n for n in os.listdir(os.path.join(self.root, 'inbox')) if n.endswith('.md'))

    def test_one_session_is_run_with_the_idea_brief(self):
        rc, _out, _err, fake = self.door('Bill customers for usage')
        self.assertEqual(len(fake.calls), 1)
        job, brief = fake.calls[0]
        self.assertEqual(job.name[:5], 'idea-')
        self.assertEqual(job.model, 'heavy')
        self.assertIn('Bill customers for usage', '\n'.join(brief.split('\n')[:3]))
        self.assertIn(job.name[len('idea-'):] + '.tree.md', brief)
        self.assertIn('never ask — propose', brief)
        self.assertIn(os.path.realpath(self.root), job.add_dirs)
        self.assertEqual(rc, 0)

    def test_the_tree_the_session_wrote_is_applied(self):
        rc, out, err, _fake = self.door('Bill customers for usage')
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.inbox()), 4)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith('next: asf groom'))

    def test_a_tree_that_does_not_parse_writes_nothing(self):
        rc, _out, err, _fake = self.door('Bill customers for usage', tree_text='not a tree')
        self.assertEqual(rc, 2)
        self.assertIn('the tree opens with # The ask', err)
        self.assertEqual(self.inbox(), [])

    def test_a_session_that_wrote_no_tree_is_exit_2(self):
        rc, _out, err, _fake = self.door('Bill customers for usage', tree_text=None)
        self.assertEqual(rc, 2)
        self.assertIn('no tree at', err)
        self.assertEqual(self.inbox(), [])

    def test_no_interrogate_runs_no_session(self):
        tree = os.path.join(self.tmp, 'given.tree.md')
        with open(tree, 'w', encoding='utf-8') as f:
            f.write(TREE)
        rc, _out, err, fake = self.door('Bill customers for usage', '--no-interrogate',
                                        '--tree', tree)
        self.assertEqual((rc, fake.calls), (0, []), err)
        self.assertEqual(len(self.inbox()), 4)


ENRICH_TREE = """# The ask
Make the approval classes specable.

---

## Feature F1: Approval classes for the meter

Approvals are grouped in classes; a class carries the rule that says who may approve what.

### Assumptions
- a class is named by its rule, not by a number

### Acceptance
- [ ] `python3 -m unittest tests.test_approvals -v` is green
- [ ] an unknown class is refused
"""


class EnrichTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        seed(self.root)
        self.card = os.path.join(self.root, 'features', 'F-0002.md')
        write_item(self.root, 'F-0002', 'feature', 'Metering', parent='E-0001',
                   body=THIN_BODY)
        self.tree = os.path.join(self.root, 'enrich.tree.md')
        self.write_tree(ENRICH_TREE)

    def write_tree(self, text):
        with open(self.tree, 'w', encoding='utf-8') as f:
            f.write(text)

    def enrich(self, item='F-0002'):
        return run(['idea', '--enrich', item, '--tree', self.tree, '--no-interrogate'], self.root)

    def card_text(self):
        with open(self.card, encoding='utf-8') as f:
            return f.read()

    def test_a_thin_card_gains_acceptance_and_assumptions(self):
        r = self.enrich()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.card_text()
        _pre, sections = parse_sections(text.split('\n---\n', 1)[1])
        by_heading = {h: c for h, c in sections}
        self.assertEqual(by_heading['## Acceptance'].strip('\n').split('\n'),
                         ['- [ ] `python3 -m unittest tests.test_approvals -v` is green',
                          '- [ ] an unknown class is refused'])
        self.assertLess([h for h, _ in sections].index('## Assumptions'),
                        [h for h, _ in sections].index('## Acceptance'))
        self.assertIn('- a class is named by its rule, not by a number', by_heading['## Assumptions'])
        self.assertTrue(by_heading['## Description'].rstrip().endswith(
            'a class carries the rule that says who may approve what.'))
        self.assertTrue(by_heading['## Description'].startswith('\nMetering, counted.'))
        self.assertIn(f'enriched: {today()}', text.split('\n---\n')[0])
        self.assertTrue(by_heading['## History'].rstrip().endswith(
            f'- {today()}: enriched (idea) — +2 acceptance, +1 assumption(s)'))
        self.assertIn('enriched F-0002', r.stdout)

    def test_the_card_still_parses(self):
        run(['index'], self.root)

        def about_the_card():   # the fixture record fails `asf check` on its own; the card's lines
            return [l for l in run(['check'], self.root).stdout.splitlines() if 'F-0002' in l]
        checked = about_the_card()
        self.assertEqual(self.enrich().returncode, 0)
        self.assertEqual(about_the_card(), checked)
        self.assertFalse([l for l in checked if 'parse' in l or 'typed' in l], checked)
        before = self.card_text()
        self.assertEqual(run(['index'], self.root).returncode, 0)

        def stable(text):
            _pre, sections = parse_sections(text)
            return [(h, c) for h, c in sections if h not in ('## Children', '## Backlinks')]
        self.assertEqual(stable(self.card_text()), stable(before))

    def test_a_multi_node_tree_is_refused(self):
        before = self.card_text()
        self.write_tree(TREE)
        r = self.enrich()
        self.assertEqual(r.returncode, 2)
        self.assertIn('--enrich takes one node', r.stderr)
        self.assertEqual(self.card_text(), before)

    def test_an_unknown_id_is_refused(self):
        r = self.enrich('F-0099')
        self.assertEqual(r.returncode, 2)
        self.assertIn('no item', r.stderr)

    def test_only_a_feature_is_enriched(self):
        r = self.enrich('E-0001')
        self.assertEqual(r.returncode, 2)
        self.assertIn('only a Feature', r.stderr)

    def test_apply_takes_enrich_too(self):
        r = run(['idea', 'apply', '--tree', self.tree, '--enrich', 'F-0002'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('enriched: ', self.card_text())


THIN_BODY = ("## Description\nMetering, counted.\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
             "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n")


if __name__ == '__main__':
    unittest.main()
