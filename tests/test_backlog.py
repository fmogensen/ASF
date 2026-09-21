import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf.record import frontmatter
from asf.record import check as check_mod

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
    env = dict(os.environ)
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

    def test_mints_sequential_ids(self):
        r1 = run(['new', 'epic', '--title', 'Factory'], self.root)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r1.stdout.strip(), 'E-0001')
        r2 = run(['new', 'epic', '--title', 'Something else entirely'], self.root)
        self.assertEqual(r2.stdout.strip(), 'E-0002')

    def test_overlap_refused_and_force_overrides(self):
        run(['new', 'epic', '--title', 'Factory Operations'], self.root)
        r = run(['new', 'epic', '--title', 'The Factory Operations'], self.root)
        self.assertEqual(r.returncode, 3)
        self.assertIn('E-0001', r.stderr)
        r2 = run(['new', 'epic', '--title', 'The Factory Operations', '--force'], self.root)
        self.assertEqual(r2.returncode, 0)

    def test_missing_parent_refused(self):
        r = run(['new', 'feature', '--title', 'Free plan'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_epic_rejects_parent(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        r = run(['new', 'epic', '--title', 'Other epic', '--parent', 'E-0001'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_wrong_parent_type_refused(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        r = run(['new', 'story', '--title', 'A story', '--parent', 'E-0001'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_nonexistent_parent_refused(self):
        r = run(['new', 'feature', '--title', 'Free plan', '--parent', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 2)

    def test_new_writes_skeleton_and_empty_machine_block(self):
        r = run(['new', 'epic', '--title', 'Factory'], self.root)
        iid = r.stdout.strip()
        text = open(os.path.join(self.root, 'epics', f'{iid}.md'), encoding='utf-8').read()
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
        r = run(['new', 'bug', '--title', 'Broken thing', '--parent', 'E-0001',
                 '--severity', 'S2', '--set', 'rank=5', '--set', 'source=review',
                 '--set', 'blockedBy=[E-0001]', '--set', 'links.spec=docs/specs/x.md'],
                self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        iid = r.stdout.strip()
        text = open(os.path.join(self.root, 'bugs', f'{iid}.md'), encoding='utf-8').read()
        meta, _body = frontmatter.parse(text)
        self.assertEqual(meta['rank'], 5)
        self.assertEqual(meta['source'], 'review')
        self.assertEqual(meta['blockedBy'], ['E-0001'])
        self.assertEqual(meta['links'], {'spec': 'docs/specs/x.md'})

    def test_new_set_refuses_a_field_the_type_does_not_have(self):
        r = run(['new', 'epic', '--title', 'Factory', '--set', 'severity=S1'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('severity', r.stderr)
        r = run(['new', 'epic', '--title', 'Factory', '--set', 'noequals'], self.root)
        self.assertEqual(r.returncode, 2)


class CheckCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_clean_repo_passes(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

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
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                   body="## Description\nSee D171 for context.\n\n## Children\n\n## Backlinks\n")
        r = run(['check'], self.root)
        self.assertIn('bare decision reference', r.stdout)

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
    """B-0015: `asf new bug` takes --severity/--signature/--found-in; `asf check` flags none."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')

    def new_bug(self, *extra):
        import argparse
        import contextlib
        import io
        from asf.record import new as new_mod
        p = argparse.ArgumentParser(prog='asf')
        p_new = p.add_subparsers(dest='command').add_parser('new')
        p_new.add_argument('type')
        p_new.add_argument('--title', required=True)
        p_new.add_argument('--parent')
        p_new.add_argument('--priority')
        p_new.add_argument('--area')
        p_new.add_argument('--legacy-id')
        p_new.add_argument('--body-file')
        p_new.add_argument('--force', action='store_true')
        new_mod.add_arguments(p_new)
        args = p.parse_args(['new', 'bug', '--title', 'Crash on save', '--parent', 'E-0001',
                             *extra])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = new_mod.cmd_new(args, self.root)
        return rc, out.getvalue().strip(), err.getvalue()

    def test_bug_without_severity_is_refused_with_usage(self):
        rc, _out, err = self.new_bug()
        self.assertEqual(rc, 2)
        self.assertIn('usage:', err)
        self.assertIn('--severity', err)
        self.assertEqual(os.listdir(os.path.join(self.root, 'bugs')), [])

    def test_severity_signature_found_in_written_in_order_before_machine_block(self):
        rc, out, _err = self.new_bug('--severity', 'S2', '--signature', 'crash-save',
                                     '--found-in', 'prod')
        self.assertEqual(rc, 0)
        text = open(os.path.join(self.root, 'bugs', f'{out}.md'), encoding='utf-8').read()
        keys = ['severity: S2', 'found_in: prod', 'signature: crash-save', '# ---- machine ----']
        positions = [text.index(k) for k in keys]
        self.assertEqual(positions, sorted(positions))

    def test_found_in_defaults_to_dev(self):
        rc, out, _err = self.new_bug('--severity', 'S3')
        self.assertEqual(rc, 0)
        text = open(os.path.join(self.root, 'bugs', f'{out}.md'), encoding='utf-8').read()
        self.assertIn('found_in: dev', text)
        self.assertNotIn('signature:', text)

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


if __name__ == '__main__':
    unittest.main()
