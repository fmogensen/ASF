"""tests.test_inbox_shape — acceptance tests for F-0073: the shape rules decide an inbox card's
type from what it carries (header lines, sections, its parent's type), not from words or a
hand-set `type:` line."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import hermetic
from asf.groom import shape
from asf.init import ITEM_FOLDERS, STREAM_FOLDERS
from asf.record import frontmatter
from asf.record.core import today

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules'}
DEFAULT_BODY = (
    "## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"
)

MACHINE_LINES = ('state: New', 'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z')


def card(title, headers=None, description='', features=(), acceptance=()):
    return shape.Card(title, dict(headers or {}), description, list(features), list(acceptance))


def _record(header_lines):
    text = ("---\n" + "\n".join(header_lines) + "\n# ---- machine ----\n"
            + "\n".join(MACHINE_LINES) + "\n---\n## Description\n")
    meta, body = frontmatter.parse(text)
    return {'meta': meta, 'body': body}


def seeded_canonical():
    """The record every class in this module starts from: E-0001 (epic, decided, title
    `Factory billing`), F-0001 (feature under E-0001, title `Billing plans`) and S-0001 (story
    under F-0001)."""
    return {
        'E-0001': _record(['id: E-0001', 'type: epic', 'title: Factory billing', 'decided: true']),
        'F-0001': _record(['id: F-0001', 'type: feature', 'title: Billing plans', 'parent: E-0001']),
        'S-0001': _record(['id: S-0001', 'type: story', 'title: Billing plan tiers', 'parent: F-0001']),
    }


def run(args, cwd):
    """The suite's usual subprocess runner (`tests.test_groom.run`), with `ASF_HOME` pinned to a
    directory that does not exist — a shaped test asserts the same thing on every machine, not
    whatever an operator's own ~/.ASF happens to configure (PD11, `tests/test_file_bugs.py:94`)."""
    env = hermetic.build()
    env.pop('BACKLOG_ID_RANGE', None)  # a job's range must not leak into the fixture's own mints (B-0012)
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['ASF_HOME'] = os.path.join(cwd, 'no-such-asf-home')
    return subprocess.run([sys.executable, '-m', 'asf.cli'] + args, cwd=cwd, env=env,
                           capture_output=True, text=True)


def make_repo():
    root = tempfile.mkdtemp(prefix='inbox_shape_test_')
    for f in ITEM_FOLDERS:
        os.makedirs(os.path.join(root, f))
    for f in STREAM_FOLDERS:
        os.makedirs(os.path.join(root, f), exist_ok=True)
    return root


def make_record():
    """A record on disk, ready for `asf index`/`asf check` — unlike `seeded_canonical()`'s
    in-memory world, `resolve_record` needs the item folders and an `index.json` to see it."""
    root = make_repo()
    run(['index'], root)
    return root


def write_item(root, id_, type_, title, parent=None, typed_lines=(), machine_lines=None, body=None):
    if machine_lines is None:
        machine_lines = ['schema_version: 1', 'state: New',
                         'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z']
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    path = os.path.join(root, FOLDER_OF[type_], f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}")
    return path


def shaped_body(rule, type_, why='inbox', acceptance=(), features=()):
    """A body whose `## History` carries the `— shape:` line `asf check`'s size pass keys on."""
    acc = ''.join(f"- [ ] {a}\n" for a in acceptance) if acceptance else "- [ ] \n"
    features_section = ('## Features\n' + ''.join(f"- {f}\n" for f in features) + '\n') if features else ''
    return (
        "## Description\n\n"
        f"{features_section}"
        f"## Acceptance\n{acc}\n"
        "## Non-goals\n\n"
        "## History\n"
        f"- 2026-09-01: created ({why}) — shape: {rule} → {type_}\n\n"
        "## Children\n\n"
        "## Backlinks\n"
    )


def seed(root):
    """The same E-0001/F-0001/S-0001 world as `seeded_canonical()` (an epic, decided, title
    `Factory billing`; a feature under it; a story under that), written to disk. S-0001 is
    written plain — it carries no `— shape:` line, same as the in-memory version."""
    write_item(root, 'E-0001', 'epic', 'Factory billing', typed_lines=['decided: true'])
    write_item(root, 'F-0001', 'feature', 'Billing plans', parent='E-0001')
    write_item(root, 'S-0001', 'story', 'Billing plan tiers', parent='F-0001')


class ShapeRulesTest(unittest.TestCase):
    def setUp(self):
        self.canonical = seeded_canonical()

    def test_signature_is_a_bug(self):
        c = card('Checkout fails', headers={'signature': 'test_checkout::test_pay', 'parent': 'E-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('bug', 'signature', 'E-0001'))

    def test_signature_uses_the_default_bug_parent(self):
        c = card('Checkout fails', headers={'signature': 'test_checkout::test_pay'})
        result = shape.derive(c, self.canonical, default_bug_parent='E-0001')
        self.assertEqual(result, shape.Shape('bug', 'signature', 'E-0001'))

    def test_writes_is_a_task(self):
        c = card('Widen the retry window', headers={'writes': 'asf/groom/**', 'parent': 'F-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('task', 'writes', 'F-0001'))

    def test_two_features_is_an_epic(self):
        c = card('A bigger billing outcome', features=['Plan tiers', 'Invoicing'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('epic', 'features-list', None))

    def test_one_feature_bullet_is_a_feature(self):
        c = card('Billing invoices', features=['Invoicing'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))

    def test_parent_feature_with_acceptance_is_a_story(self):
        c = card('Add plan tiers', headers={'parent': 'F-0001'}, acceptance=['python3 -m unittest tests.x'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('story', 'parent-feature', 'F-0001'))

    def test_plain_card_is_a_feature_under_the_named_epic(self):
        c = card('Something new', headers={'parent': 'E-0001'}, description='A description.')
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))

    def test_words_do_not_type(self):
        c = card('A new goal for the quarter', headers={'parent': 'E-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))


class ShapeQuestionTest(unittest.TestCase):
    def setUp(self):
        self.canonical = seeded_canonical()

    def test_signature_and_writes(self):
        c = card('One or the other', headers={'signature': 'x', 'writes': 'a/**', 'parent': 'E-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('A card is one thing', result.text)

    def test_story_without_acceptance(self):
        c = card('Add plan tiers', headers={'parent': 'F-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('one acceptance list', result.text)

    def test_under_a_story_needs_writes(self):
        c = card('Something under a story', headers={'parent': 'S-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('only a Task hangs', result.text)

    def test_epic_with_parent(self):
        c = card('A bigger outcome', headers={'parent': 'E-0001'}, features=['Plan tiers', 'Invoicing'])
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('An Epic has no parent', result.text)

    def test_defect_words_without_signature(self):
        c = card('Checkout is broken', description='Customers cannot pay.')
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('A Bug carries a signature', result.text)

    def test_defect_words_with_acceptance_is_work(self):
        c = card('Checkout is broken', headers={'parent': 'E-0001'}, description='Customers cannot pay.',
                  acceptance=['python3 -m unittest tests.x'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))

    def test_type_line_disagreeing(self):
        c = card('Checkout fails', headers={'type': 'feature', 'signature': 'x', 'parent': 'E-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn("is not read — the card's shape reads as bug (signature", result.text)

    def test_type_line_agreeing_is_filed(self):
        c = card('Checkout fails', headers={'type': 'bug', 'signature': 'x', 'parent': 'E-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('bug', 'signature', 'E-0001'))

    def test_missing_parent(self):
        c = card('Something', headers={'parent': 'E-0999'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('does not exist', result.text)


class CheckShapeTest(unittest.TestCase):
    """`asf check`'s size pass: an item whose History carries a `— shape:` line is held to that
    type's size (D6); an item with none is grandfathered and never checked."""

    def setUp(self):
        self.root = make_record()
        seed(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_shaped_bug_without_signature(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0001',
                   body=shaped_body('signature', 'bug'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('bug without a signature', r.stdout)

    def test_shaped_task_without_writes(self):
        write_item(self.root, 'T-0001', 'task', 'Widen the retry window', parent='F-0001',
                   body=shaped_body('writes', 'task'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('task without writes:', r.stdout)

    def test_shaped_story_without_acceptance(self):
        write_item(self.root, 'S-0002', 'story', 'Add plan tiers', parent='F-0001',
                   body=shaped_body('parent-feature', 'story'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('story without an acceptance list', r.stdout)

    def test_shaped_epic_with_one_feature(self):
        write_item(self.root, 'E-0002', 'epic', 'A bigger outcome',
                   body=shaped_body('features-list', 'epic', features=['Plan tiers']))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('epic spanning fewer than two Features', r.stdout)

    def test_shaped_feature_with_writes(self):
        write_item(self.root, 'F-0002', 'feature', 'Billing invoices', parent='E-0001',
                   typed_lines=['writes: [asf/groom/**]'],
                   body=shaped_body('default', 'feature'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("feature carries writes:/signature:", r.stdout)

    def test_shaped_items_of_the_right_size_pass(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0001',
                   typed_lines=['signature: test_checkout::test_pay', 'severity: S2'],
                   body=shaped_body('signature', 'bug'))
        write_item(self.root, 'T-0001', 'task', 'Widen the retry window', parent='F-0001',
                   typed_lines=['writes: [asf/groom/**]'],
                   body=shaped_body('writes', 'task'))
        write_item(self.root, 'S-0002', 'story', 'Add plan tiers', parent='F-0001',
                   body=shaped_body('parent-feature', 'story',
                                     acceptance=['python3 -m unittest tests.x']))
        write_item(self.root, 'E-0002', 'epic', 'A bigger outcome',
                   body=shaped_body('features-list', 'epic', features=['Plan tiers', 'Invoicing']))
        write_item(self.root, 'F-0002', 'feature', 'Billing invoices', parent='E-0001',
                   body=shaped_body('default', 'feature'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_unshaped_items_are_not_sized(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertNotIn('without a signature', r.stdout)

    def test_closed_item_not_sized(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0001',
                   machine_lines=['schema_version: 1', 'state: Closed',
                                  'stage_since: 2026-09-01T00:00:00Z',
                                  'updated: 2026-09-01T00:00:00Z'],
                   body=shaped_body('signature', 'bug'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertNotIn('without a signature', r.stdout)

    def test_file_bugs_writes_the_shape_line(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ci_path = os.path.join(self.root, 'metrics', 'ci', f"{today()}.jsonl")
        with open(ci_path, 'w', encoding='utf-8') as f:
            for i, ts in enumerate([now - datetime.timedelta(hours=1),
                                    now - datetime.timedelta(hours=2)]):
                f.write(json.dumps({
                    'run': 100 + i, 'sha': 'deadbee', 'branch': 'main',
                    'ts': ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'jobs': [{'name': 'gate', 'failed_step': 'flaky', 'conclusion': 'failure'}],
                }) + '\n')
        r = run(['file-bugs', '--default-bug-epic', 'E-0001'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 filed', r.stdout)
        bugs = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(len(bugs), 1)
        with open(os.path.join(self.root, 'bugs', bugs[0]), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('created (file-bugs) — shape: signature → bug', text)
        r2 = run(['check'], self.root)
        self.assertNotIn('without a signature', r2.stdout)


if __name__ == '__main__':
    unittest.main()
