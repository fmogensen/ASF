import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf.init import STREAM_FOLDERS
from asf.record import frontmatter
from asf.record import check as check_mod
from asf import hermetic

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']

DEFAULT_BODY = (
    "## Description\n\n"
    "## Acceptance\n"
    "- [ ] \n\n"
    "## Non-goals\n\n"
    "## History\n"
    "- 2026-01-01: created\n\n"
    "## Children\n\n"
    "## Backlinks\n"
)


def make_repo():
    root = tempfile.mkdtemp(prefix='backlog_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def item_text(id_, type_, title, parent=None, typed_lines=(),
              machine_lines=('schema_version: 1',
                             'state: New',
                              'stage_since: 2026-01-01T00:00:00Z',
                              'updated: 2026-01-01T00:00:00Z'),
              body=None):
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    return f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}"


def folder_of(type_):
    return {
        'epic': 'epics', 'feature': 'features', 'story': 'stories',
        'task': 'tasks', 'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules',
    }[type_]


def write_item(root, id_, type_, title, **kw):
    path = os.path.join(root, folder_of(type_), f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(item_text(id_, type_, title, **kw))
    return path


def run(args, cwd):
    env = hermetic.build()
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, '-m', 'asf.cli'] + args, cwd=cwd, env=env,
                           capture_output=True, text=True)


class FrontmatterParseTests(unittest.TestCase):
    def test_split_machine(self):
        text = item_text('F-0001', 'feature', 'Thing', typed_lines=['area: billing'])
        meta, _body = frontmatter.parse(text, path='F-0001.md')
        typed, machine = frontmatter.split_machine(meta)
        self.assertEqual(typed, {'id': 'F-0001', 'type': 'feature', 'title': 'Thing', 'area': 'billing'})
        self.assertEqual(machine['state'], 'New')

    def test_missing_leading_marker(self):
        with self.assertRaises(frontmatter.FrontmatterError) as cm:
            frontmatter.parse("id: F-0001\n---\nbody\n", path='x.md')
        self.assertEqual(cm.exception.file, 'x.md')

    def test_missing_closing_marker(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("---\nid: F-0001\nbody with no close\n", path='x.md')

    def test_unparsable_line(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("---\nthis is not key value\n---\nbody\n", path='x.md')

    def test_unbalanced_inline_list(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("---\nblockedBy: [F-0001, F-0002\n---\nbody\n", path='x.md')

    def test_bad_nested_line(self):
        text = "---\nlinks:\n  spec: ok\n  : bad\n---\nbody\n"
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse(text, path='x.md')

    def test_write_machine_leaves_typed_lines_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'F-0001.md')
            text = (
                "---\n"
                "id: F-0001\n"
                "type: feature\n"
                "title: Free plan                # a comment to preserve\n"
                "parent: E-0010\n"
                "# ---- machine ----\n"
                "state: New\n"
                "stage_since: 2026-01-01T00:00:00Z\n"
                "updated: 2026-01-01T00:00:00Z\n"
                "---\n"
                "## Description\nbody text\n"
            )
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            frontmatter.write_machine(path, {
                'state': 'Active',
                'stage_since': '2026-02-02T00:00:00Z',
                'updated': '2026-02-02T00:00:00Z',
            })
            new_text = open(path, encoding='utf-8').read()
            typed_head = text.split('# ---- machine ----')[0]
            self.assertTrue(new_text.startswith(typed_head + '# ---- machine ----\n'))
            self.assertIn('state: Active', new_text)
            self.assertTrue(new_text.endswith('## Description\nbody text\n'))


class NewCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')

    def test_mints_sequential_ids(self):
        r1 = run(['new', 'story', '--title', 'Factory', '--parent', 'F-0001', '--acceptance', 'x'], self.root)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r1.stdout.strip(), 'S-0001')
        r2 = run(['new', 'story', '--title', 'Something else entirely', '--parent', 'F-0001',
                  '--acceptance', 'x'], self.root)
        self.assertEqual(r2.stdout.strip(), 'S-0002')

    def test_overlap_refused_and_force_overrides(self):
        run(['new', 'story', '--title', 'Factory Operations', '--parent', 'F-0001', '--acceptance', 'x'], self.root)
        r = run(['new', 'story', '--title', 'The Factory Operations', '--parent', 'F-0001',
                 '--acceptance', 'x'], self.root)
        self.assertEqual(r.returncode, 3)
        self.assertIn('S-0001', r.stderr)
        r2 = run(['new', 'story', '--title', 'The Factory Operations', '--parent', 'F-0001',
                  '--acceptance', 'x', '--force'], self.root)
        self.assertEqual(r2.returncode, 0)

    def test_missing_parent_refused(self):
        r = run(['new', 'story', '--title', 'T', '--acceptance', 'x'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_epic_rejects_parent(self):
        r = run(['new', 'decision', '--title', 'T', '--parent', 'E-0001'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_wrong_parent_type_refused(self):
        r = run(['new', 'story', '--title', 'A story', '--parent', 'E-0001', '--acceptance', 'x'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_nonexistent_parent_refused(self):
        r = run(['new', 'story', '--title', 'Free plan', '--parent', 'F-0009', '--acceptance', 'x'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_new_writes_skeleton_and_empty_machine_block(self):
        r = run(['new', 'story', '--title', 'Factory', '--parent', 'F-0001', '--acceptance', 'x'], self.root)
        iid = r.stdout.strip()
        text = open(os.path.join(self.root, 'stories', f'{iid}.md'), encoding='utf-8').read()
        for heading in ('## Description', '## Acceptance', '## Non-goals',
                        '## History', '## Children', '## Backlinks'):
            self.assertIn(heading, text)
        meta, _body = frontmatter.parse(text)
        self.assertEqual(meta['state'], 'New')
        self.assertIn('stage_since', meta)
        self.assertIn('updated', meta)


class NewSetFieldsTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_new_set_types_the_schema_fields(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        r = run(['new', 'story', '--title', 'Broken thing', '--parent', 'F-0001',
                 '--acceptance', 'x', '--set', 'rank=5',
                 '--set', 'blockedBy=[E-0001]', '--set', 'links.spec=docs/specs/x.md'],
                self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        iid = r.stdout.strip()
        text = open(os.path.join(self.root, 'stories', f'{iid}.md'), encoding='utf-8').read()
        meta, _body = frontmatter.parse(text)
        self.assertEqual(meta['rank'], 5)
        self.assertEqual(meta['blockedBy'], ['E-0001'])
        self.assertEqual(meta['links'], {'spec': 'docs/specs/x.md'})

    def test_new_set_refuses_a_field_the_type_does_not_have(self):
        r = run(['new', 'decision', '--title', 'Factory', '--set', 'severity=S1'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('severity', r.stderr)
        r = run(['new', 'decision', '--title', 'Factory', '--set', 'noequals'], self.root)
        self.assertEqual(r.returncode, 2)


class SetCommandTests(unittest.TestCase):
    """B-0084: a typed field is written through the parser, so an unwritable value is refused."""
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        self.bug = write_item(self.root, 'B-0001', 'bug', 'Broken', parent='E-0001',
                              typed_lines=('severity: S2',))

    def read(self):
        with open(self.bug, encoding='utf-8') as f:
            return f.read()

    def test_set_writes_a_typed_field_through_the_parser(self):
        r = run(['set', 'B-0001', 'rank=5', 'links.spec=docs/specs/x.md'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        meta, _ = frontmatter.parse(self.read())
        self.assertEqual(meta['rank'], 5)
        self.assertEqual(meta['links'], {'spec': 'docs/specs/x.md'})
        self.assertEqual(meta['severity'], 'S2')

    def test_set_refuses_a_value_that_does_not_round_trip(self):
        before = self.read()
        r = run(['set', 'B-0001', 'decided=first line\nsecond line'], self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('decided', r.stderr)
        self.assertEqual(self.read(), before)

    def test_set_refuses_a_field_the_type_does_not_have(self):
        before = self.read()
        r = run(['set', 'B-0001', 'scope=x'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.read(), before)


class RecordPreCommitHookTests(unittest.TestCase):
    """B-0084: a commit into the record runs ``asf check`` on what it touches, even when a
    marker leaked in from another repo's hook run."""
    def test_a_leaked_hook_marker_does_not_bypass_the_hook(self):
        from asf.init import PRE_COMMIT
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        bindir = tempfile.mkdtemp(prefix='asf_bin_')
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        shim = os.path.join(bindir, 'asf')
        with open(shim, 'w') as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} -m asf.cli "$@"\n')
        os.chmod(shim, 0o755)
        hook = os.path.join(root, 'hook.sh')
        with open(hook, 'w') as f:
            f.write(PRE_COMMIT)
        env = hermetic.build()
        env['PATH'] = bindir + os.pathsep + env['PATH']
        env['ASF_HOOK_RUNNING'] = '1'      # left over from some other repo's hook run
        subprocess.run(['git', 'init', '-q'], cwd=root, env=env, check=True)
        write_item(root, 'B-0001', 'bug', 'Broken',
                   typed_lines=('severity: S2', 'decided: [unclosed'))
        subprocess.run(['git', 'add', '-A'], cwd=root, env=env, check=True)
        r = subprocess.run(['sh', hook], cwd=root, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)


class CheckCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        for f in STREAM_FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_missing_layout_folder_finding(self):
        # B-0005: `check` validated the record's items but never the layout the README names —
        # a backlog missing a stream folder (groom/, releases/, a metrics/ stream) passed.
        # T-0040/PD5: the intake dir left STREAM_FOLDERS when it became a product convention, so
        # `check` no longer requires one; the finding still carries its runnable command.
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write_item(root, 'E-0001', 'epic', 'Factory')
        run(['index'], root)
        r = run(['check'], root)
        self.assertEqual(r.returncode, 1)
        for f in STREAM_FOLDERS:
            self.assertIn(f"{f}/ is missing", r.stdout)
        self.assertIn('mkdir -p groom', r.stdout)
        self.assertNotIn('inbox/ is missing', r.stdout)

    def test_clean_repo_passes(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def _residue_story(self, sid='S-0104', evidence=('no evidence found (2026-09-23)', 'rule: no-rule')):
        machine = ['schema_version: 1', 'state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                   'evidence:'] + ['  - ' + line for line in evidence] + ['updated: 2026-01-01T00:00:00Z']
        write_item(self.root, sid, 'story', 'Shapeless', parent='F-0001', machine_lines=tuple(machine))

    def test_a_record_with_no_residue_produces_no_residue_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        self._residue_story(evidence=('commit 9f2ac41 names S-0104', 'rule: landed'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('no closing rule', r.stdout)

    def test_one_finding_per_residue_item_names_the_type_the_gap_and_the_spec(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        self._residue_story('S-0104')
        self._residue_story('S-0105')
        self._residue_story('S-0106', evidence=('no evidence found (2026-09-23)', 'rule: planned'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)   # a residue warns; it never refuses a commit
        found = [ln for ln in r.stdout.splitlines() if 'no closing rule sees this item' in ln]
        self.assertTrue(all(': warning: ' in ln for ln in found), found)
        self.assertEqual(len(found), 2, r.stdout)
        self.assertTrue(found[0].startswith('stories/S-0104.md:'), found[0])
        self.assertIn('(type story, no Task, no matrix row, parent F-0001 is New)', found[0])
        self.assertIn('it can never close; see docs/specs/f-0080.md §2.1', found[0])
        self.assertTrue(found[1].startswith('stories/S-0105.md:'), found[1])

    def test_a_story_a_task_lists_is_not_told_it_has_no_task(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        self._residue_story('S-0104')
        write_item(self.root, 'T-0001', 'task', 'Do it', parent='F-0001',
                   typed_lines=('stories: [S-0104]',))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertIn('(type story, no matrix row, parent F-0001 is New)', r.stdout)

    def test_landed_must_be_a_hex_sha(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001',
                   typed_lines=('landed: 9f2ac41',))
        write_item(self.root, 'F-0002', 'feature', 'Other', parent='E-0001',
                   typed_lines=('landed: not-a-sha',))
        write_item(self.root, 'F-0003', 'feature', 'Short', parent='E-0001',
                   typed_lines=('landed: 9f2a',))
        run(['index'], self.root)
        r = run(['check'], self.root)
        lines = [ln for ln in r.stdout.splitlines() if 'landed:' in ln]
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertTrue(any(ln.startswith('features/F-0002.md:') and "'not-a-sha'" in ln for ln in lines))
        self.assertTrue(any(ln.startswith('features/F-0003.md:') for ln in lines))
        self.assertIn('what `ingest` decides', lines[0])
        self.assertFalse(any('F-0001.md' in ln for ln in lines))

    def test_parse_error_finding(self):
        path = os.path.join(self.root, 'epics', 'E-0001.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('not frontmatter at all\n')
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('E-0001.md', r.stdout)

    def test_id_mismatch_finding(self):
        path = os.path.join(self.root, 'epics', 'E-0001.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(item_text('E-0002', 'epic', 'Factory'))
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('does not match filename', r.stdout)

    def test_type_mismatch_finding(self):
        path = os.path.join(self.root, 'epics', 'E-0001.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(item_text('E-0001', 'feature', 'Factory'))
        r = run(['check'], self.root)
        self.assertIn('does not match folder', r.stdout)

    def test_duplicate_id_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        with open(os.path.join(self.root, 'features', 'E-0001.md'), 'w', encoding='utf-8') as f:
            f.write(item_text('E-0001', 'feature', 'Duplicate'))
        r = run(['check'], self.root)
        self.assertIn('duplicate id', r.stdout)

    def test_missing_parent_finding(self):
        write_item(self.root, 'F-0001', 'feature', 'Free plan')
        write_item(self.root, 'S-0001', 'story', 'A story')
        r = run(['check'], self.root)
        self.assertIn('missing a parent', r.stdout)

    def test_feature_missing_parent_with_no_exception_configured(self):
        # Unlike the original product's dated migration exception, a generic ASF has no such
        # grace window by default: a parentless Feature is always flagged.
        write_item(self.root, 'F-0001', 'feature', 'Free plan')
        r = run(['check'], self.root)
        self.assertIn('missing a parent', r.stdout)

    def test_feature_missing_parent_exempt_when_configured(self):
        # A product may configure a dated grace window for its own migration by setting
        # check.FEATURE_NO_PARENT_UNTIL — exercised in-process since it's a module-level knob,
        # not (yet) a --product config key.
        with mock.patch.object(check_mod, 'FEATURE_NO_PARENT_UNTIL', '2999-01-01'):
            import argparse
            write_item(self.root, 'F-0001', 'feature', 'Free plan')
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = check_mod.cmd_check(argparse.Namespace(paths=[]), self.root)
            self.assertNotIn('missing a parent', buf.getvalue())

    def test_wrong_parent_type_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'S-0001', 'story', 'A story', parent='E-0001')
        r = run(['check'], self.root)
        self.assertIn('wrong type', r.stdout)

    def test_blockedby_missing_ref_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   typed_lines=['blockedBy: [F-0099]'])
        r = run(['check'], self.root)
        self.assertIn('blockedBy references missing item', r.stdout)

    def test_stories_missing_ref_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'T-0001', 'task', 'Do it', parent='F-0001',
                   typed_lines=['stories: [S-0099]'])
        r = run(['check'], self.root)
        self.assertIn('stories references missing item', r.stdout)

    def test_bare_decision_reference_finding(self):
        write_item(self.root, 'D-0171', 'decision', 'A ruling')
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   body="## Description\nSee D171 for context.\n\n## Children\n\n## Backlinks\n")
        r = run(['check'], self.root)
        self.assertIn('bare decision reference', r.stdout)

    def test_a_prefixed_id_like_pf_d18_is_not_a_decision_reference(self):
        # review-finding ids (PF-D18) end in a D<n> the record may have a card for: the hyphen
        # before the D makes it another id, not a bare decision reference
        write_item(self.root, 'D-0018', 'decision', 'A ruling')
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   body="## Description\nFixes PF-D18 and PF-D18b from round 2.\n\n## Children\n\n## Backlinks\n")
        r = run(['check'], self.root)
        self.assertNotIn('bare decision reference', r.stdout)

    def test_decision_reference_inside_fenced_code_is_not_a_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   body="## Description\n```ts\ndescribe('setup mode (D287 b)', () => {})\n```\n"
                        "\n## Children\n\n## Backlinks\n")
        r = run(['check'], self.root)
        self.assertNotIn('bare decision reference', r.stdout)

    def test_a_d_number_the_record_has_no_card_for_is_the_products_own_register(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   body="## Description\nPer D905 in the product's decisions.\n\n## Children\n\n## Backlinks\n")
        r = run(['check'], self.root)
        self.assertNotIn('bare decision reference', r.stdout)

    def test_bracketed_decision_reference_is_clean(self):
        write_item(self.root, 'D-0171', 'decision', 'A ruling')
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   body="## Description\nSee [[D-0171]] for context.\n\n## Children\n\n## Backlinks\n")
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_stale_children_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        r = run(['check'], self.root)
        self.assertIn('## Children section is stale', r.stdout)

    def test_feature_building_without_task_coverage_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001',
                   machine_lines=['state: Active', 'stage: building 0/1',
                                  'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])
        write_item(self.root, 'S-0001', 'story', 'Sign up', parent='F-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertIn('has no Task listing it', r.stdout)

    def test_active_task_writes_overlap_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'T-0001', 'task', 'First', parent='F-0001',
                   typed_lines=["writes: [apps/web/app/billing/**]"],
                   machine_lines=['state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])
        write_item(self.root, 'T-0002', 'task', 'Second', parent='F-0001',
                   typed_lines=["writes: [apps/web/app/billing/page.tsx]"],
                   machine_lines=['state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertIn('intersects Active task', r.stdout)

    def test_active_task_writes_no_overlap_is_clean(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'T-0001', 'task', 'First', parent='F-0001',
                   typed_lines=["writes: [apps/web/app/billing/**]"],
                   machine_lines=['schema_version: 1', 'state: Active',
                                  'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])
        write_item(self.root, 'T-0002', 'task', 'Second', parent='F-0001',
                   typed_lines=["writes: [apps/web/app/marketing/page.tsx]"],
                   machine_lines=['schema_version: 1', 'state: Active',
                                  'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_index_json_missing_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        r = run(['check'], self.root)
        self.assertIn('index.json is missing', r.stdout)

    def test_index_json_stale_finding(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        run(['index'], self.root)
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        run(['index'], self.root)  # keep bodies fresh...
        # ...but hand-corrupt index.json to simulate staleness
        idx_path = os.path.join(self.root, 'index.json')
        data = json.load(open(idx_path, encoding='utf-8'))
        data['items']['F-0001']['title'] = 'stale title'
        with open(idx_path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        r = run(['check'], self.root)
        self.assertIn('index.json is stale', r.stdout)


class IndexCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_children_and_backlinks_content(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'E-0009', 'epic', 'Factory ops')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'B-0001', 'bug', 'Trial banner shows on Free', parent='E-0009',
                   body="## Description\nSee [[F-0001]].\n\n## Children\n\n## Backlinks\n")
        run(['index'], self.root)
        epic_text = open(os.path.join(self.root, 'epics', 'E-0001.md'), encoding='utf-8').read()
        self.assertIn('- [F-0001](../features/F-0001.md) Free plan — New', epic_text)
        feature_text = open(os.path.join(self.root, 'features', 'F-0001.md'), encoding='utf-8').read()
        self.assertIn('## Backlinks\n- [B-0001](../bugs/B-0001.md) Trial banner shows on Free',
                      feature_text)

    def test_index_is_idempotent(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        run(['index'], self.root)
        snapshot = {}
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            for name in os.listdir(d):
                snapshot[(f, name)] = open(os.path.join(d, name), encoding='utf-8').read()
        idx1 = open(os.path.join(self.root, 'index.json'), encoding='utf-8').read()
        run(['index'], self.root)
        idx2 = open(os.path.join(self.root, 'index.json'), encoding='utf-8').read()
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            for name in os.listdir(d):
                self.assertEqual(snapshot[(f, name)],
                                  open(os.path.join(d, name), encoding='utf-8').read())
        self.assertEqual(idx1.split('"generated"')[1], idx2.split('"generated"')[1])


class BugSeverityTests(unittest.TestCase):
    """`asf new bug` is refused (a Bug enters through the inbox); `asf check` flags no severity."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')

    def test_new_bug_refused(self):
        r = run(['new', 'bug', '--title', 'Crash on save', '--parent', 'E-0001'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('enters through the inbox', r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.root, 'bugs')), [])

    def test_check_flags_bug_without_severity(self):
        write_item(self.root, 'B-0001', 'bug', 'No severity', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('B-0001: bug without severity', r.stdout)

    def test_check_accepts_bug_with_severity(self):
        write_item(self.root, 'B-0001', 'bug', 'Has severity', parent='E-0001',
                   typed_lines=('severity: S1',))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertNotIn('without severity', r.stdout)


class DeliveryFieldTests(unittest.TestCase):
    """`asf check` validates `delivers:` (on the lead) and `delivered_by:` (on each member)."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')

    def feature(self, id_, *lines):
        write_item(self.root, id_, 'feature', id_, parent='E-0001', typed_lines=lines)

    def check(self):
        run(['index'], self.root)
        r = run(['check'], self.root)
        return [l for l in r.stdout.splitlines() if 'deliver' in l]

    def test_a_well_formed_delivery_is_clean(self):
        self.feature('F-0001', 'delivers: [F-0001, F-0002]')
        self.feature('F-0002', 'delivered_by: F-0001')
        self.assertEqual(self.check(), [])

    def test_member_must_point_back(self):
        self.feature('F-0001', 'delivers: [F-0001, F-0002]')
        self.feature('F-0002')
        lines = self.check()
        self.assertEqual(len(lines), 1)
        self.assertIn('F-0002', lines[0])
        self.assertIn('delivered_by', lines[0])

    def test_no_item_in_two_deliveries(self):
        self.feature('F-0001', 'delivers: [F-0001, F-0003]')
        self.feature('F-0002', 'delivers: [F-0002, F-0003]')
        self.feature('F-0003', 'delivered_by: F-0001')
        lines = self.check()
        self.assertEqual(len(lines), 1)
        self.assertIn('two delivers: lists', lines[0])

    def test_both_fields_on_one_card(self):
        self.feature('F-0001', 'delivers: [F-0001]', 'delivered_by: F-0002')
        self.feature('F-0002')
        lines = self.check()
        self.assertEqual(len(lines), 1)
        self.assertIn('both delivers: and delivered_by:', lines[0])

    def test_lead_must_be_first(self):
        self.feature('F-0001', 'delivers: [F-0002, F-0001]')
        self.feature('F-0002', 'delivered_by: F-0001')
        self.assertTrue(any('not the lead itself' in l for l in self.check()))

    def test_member_must_exist_and_not_be_removed(self):
        self.feature('F-0001', 'delivers: [F-0001, F-0002, F-0009]')
        self.feature('F-0002', 'delivered_by: F-0001', 'removed: 2026-01-02')
        lines = self.check()
        self.assertTrue(any('missing item F-0009' in l for l in lines))
        self.assertTrue(any('removed item F-0002' in l for l in lines))


if __name__ == '__main__':
    unittest.main()
