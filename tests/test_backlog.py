import argparse
import contextlib
import io
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
from asf import env, hermetic, redact

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

    def test_set_local_only_round_trips_and_cloud_routing_reads_it(self):
        from asf.workers import cloud
        from asf.workers import pool as pool_mod
        cfg = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'default': True}})
        for word, want in (('true', True), ('false', False)):
            r = run(['set', 'B-0001', f'local_only={word}'], self.root)
            self.assertEqual(r.returncode, 0, r.stderr)
            meta, _ = frontmatter.parse(self.read())
            self.assertIs(meta['local_only'], want)
            row = pool_mod.Row('j', 'B-0001', kind='coder',
                               local_only=cloud.truthy(meta.get('local_only')))
            self.assertEqual(cloud.first(row, cfg), not want)

    def test_set_local_only_refuses_a_non_boolean_and_check_flags_one(self):
        before = self.read()
        r = run(['set', 'B-0001', 'local_only=maybe'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('true, false', r.stderr)
        self.assertEqual(self.read(), before)
        with open(self.bug, 'w', encoding='utf-8') as f:
            f.write(before.replace('severity: S2', 'severity: S2\nlocal_only: maybe'))
        r = run(['check'], self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('local_only must be true or false', r.stdout + r.stderr)

    def test_set_takes_many_ids_in_one_call(self):
        other = write_item(self.root, 'B-0002', 'bug', 'Also', parent='E-0001',
                           typed_lines=('severity: S2',))
        r = run(['set', 'B-0001', 'B-0002', 'local_only=true'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in (self.bug, other):
            with open(path, encoding='utf-8') as f:
                self.assertIs(frontmatter.parse(f.read())[0]['local_only'], True)
        before = self.read()
        r = run(['set', 'B-0001', 'B-9999', 'rank=7'], self.root)  # an unknown id writes nothing
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.read(), before)


class SetSeverityFieldTests(unittest.TestCase):
    """F-0163: `severity` joins `SETTABLE['bug']`, gated to S1|S2|S3 (:data:`asf.record.new.
    SEVERITIES`) before the card is touched — and on no other type's list."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        self.bug = write_item(self.root, 'B-0001', 'bug', 'Broken', parent='E-0001',
                              typed_lines=('severity: S1',))

    def read(self):
        with open(self.bug, encoding='utf-8') as f:
            return f.read()

    def test_severity_is_settable_and_writes_the_field_and_the_history(self):
        r = run(['set', 'B-0001', 'severity=S2', '--why', 'main green, prod deployed'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        meta, body = frontmatter.parse(self.read())
        self.assertEqual(meta['severity'], 'S2')
        self.assertIn('set: severity S1 → S2 — main green, prod deployed', body)

    def test_lowercase_value_is_accepted_and_written_canonically(self):
        r = run(['set', 'B-0001', 'severity=s2', '--why', 'x'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        meta, _body = frontmatter.parse(self.read())
        self.assertEqual(meta['severity'], 'S2')

    def test_bad_value_is_refused_before_the_card_is_touched(self):
        before = self.read()
        for bad in ('S4', 'x'):
            r = run(['set', 'B-0001', f'severity={bad}'], self.root)
            self.assertEqual(r.returncode, 2)
            self.assertIn('one of S1, S2, S3', r.stderr)
            self.assertEqual(self.read(), before)

    def test_no_other_type_gained_the_field(self):
        f1 = write_item(self.root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        with open(f1, encoding='utf-8') as f:
            before = f.read()
        r = run(['set', 'F-0001', 'severity=S2'], self.root)
        self.assertEqual(r.returncode, 2)
        with open(f1, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)
        before_bug = self.read()
        r = run(['set', 'B-0001', 'scope=x'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.read(), before_bug)


class SetSeverityHistoryTests(unittest.TestCase):
    """F-0163 D2/D5/D9: a severity change files its own dated `## History` line in the same
    staged write as the field, so the record never holds a severity it cannot explain."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        self.bug = write_item(self.root, 'B-0001', 'bug', 'Broken', parent='E-0001',
                              typed_lines=('severity: S2',))

    def read(self):
        with open(self.bug, encoding='utf-8') as f:
            return f.read()

    def test_the_line_is_dated_and_placed_inside_history_untouched_otherwise(self):
        from asf.record.core import today
        r = run(['set', 'B-0001', 'severity=S3'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('history: severity S2 → S3', r.stdout)
        _meta, body = frontmatter.parse(self.read())
        self.assertIn(f'- {today()} set: severity S2 → S3\n', body)
        self.assertIn('- 2026-01-01: created\n', body)
        self.assertIn('## Children', body)
        self.assertIn('## Backlinks', body)

    def test_a_second_set_appends_a_second_line(self):
        run(['set', 'B-0001', 'severity=S3'], self.root)
        r = run(['set', 'B-0001', 'severity=S2'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        _meta, body = frontmatter.parse(self.read())
        self.assertEqual(body.count('set: severity'), 2)

    def test_a_change_to_the_same_value_writes_no_line_and_is_not_refused(self):
        r = run(['set', 'B-0001', 'severity=S2'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        _meta, body = frontmatter.parse(self.read())
        self.assertNotIn('set: severity', body)
        self.assertNotIn('history:', r.stdout)

    def test_a_card_with_no_history_section_is_refused_with_the_field_unwritten(self):
        bare = ("## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
                "## Children\n\n## Backlinks\n")
        no_hist = write_item(self.root, 'B-0002', 'bug', 'No history', parent='E-0001',
                             typed_lines=('severity: S2',), body=bare)
        with open(no_hist, encoding='utf-8') as f:
            before = f.read()
        r = run(['set', 'B-0002', 'severity=S3'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('History', r.stderr)
        with open(no_hist, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)

    def test_the_field_and_the_line_land_in_one_write_a_refused_write_has_neither(self):
        from asf.record.core import canonicalize, load_items
        from asf.record.setfield import set_typed
        write_item(self.root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        active = ('schema_version: 1', 'state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                 'updated: 2026-01-01T00:00:00Z')
        write_item(self.root, 'T-0002', 'task', 'Other', parent='F-0001',
                  typed_lines=('writes: [lib/x.py]',), machine_lines=active)
        t1 = write_item(self.root, 'T-0001', 'task', 'Do', parent='F-0001',
                        typed_lines=('writes: [src/a.py]',), machine_lines=active)
        with open(t1, encoding='utf-8') as f:
            before = f.read()
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        err = set_typed(canonical['T-0001'], {'writes': ['src/a.py', 'lib/x.py']},
                        history=['- 2026-01-01 set: test line'])
        self.assertIn('I3', err)
        with open(t1, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)


class SetSeverityWhyTests(unittest.TestCase):
    """F-0163 D4/D5/D6/PD5: leaving S1 requires `--why`, and its words land verbatim in the
    History line and nowhere else."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        self.bug = write_item(self.root, 'B-0001', 'bug', 'Broken', parent='E-0001',
                              typed_lines=('severity: S1',))

    def read(self):
        with open(self.bug, encoding='utf-8') as f:
            return f.read()

    def test_leaving_s1_without_why_is_refused(self):
        before = self.read()
        for target in ('S2', 'S3'):
            r = run(['set', 'B-0001', f'severity={target}'], self.root)
            self.assertEqual(r.returncode, 2)
            self.assertIn('--why', r.stderr)
            self.assertEqual(self.read(), before)

    def test_leaving_s1_with_why_lands_verbatim(self):
        r = run(['set', 'B-0001', 'severity=S2', '--why', 'main green, prod deployed'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        _meta, body = frontmatter.parse(self.read())
        self.assertIn('— main green, prod deployed', body)

    def test_no_flag_needed_once_off_s1(self):
        other = write_item(self.root, 'B-0002', 'bug', 'Also', parent='E-0001',
                           typed_lines=('severity: S2',))
        for target in ('S3', 'S2', 'S1'):  # S2→S3, S3→S2, S2→S1 — none leaves S1
            r = run(['set', 'B-0002', f'severity={target}'], self.root)
            self.assertEqual(r.returncode, 0, r.stderr)
        with open(other, encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read())
        self.assertEqual(meta['severity'], 'S1')

    def test_why_with_no_severity_assignment_is_refused(self):
        before = self.read()
        r = run(['set', 'B-0001', 'rank=5', '--why', 'x'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('--why', r.stderr)
        self.assertEqual(self.read(), before)

    def test_why_beside_a_noop_severity_is_refused(self):
        before = self.read()
        r = run(['set', 'B-0001', 'severity=S1', '--why', 'x'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.read(), before)

    def test_a_multiline_why_is_refused_and_the_card_is_byte_identical(self):
        before = self.read()
        _meta, body_before = frontmatter.parse(before)
        n_before = len(body_before.splitlines())
        r = run(['set', 'B-0001', 'severity=S2', '--why', 'line one\nline two'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('one line', r.stderr)
        self.assertEqual(self.read(), before)
        _meta, body_after = frontmatter.parse(self.read())
        self.assertEqual(len(body_after.splitlines()), n_before)

    def test_why_never_reaches_another_fields_line(self):
        r = run(['set', 'B-0001', 'rank=5', 'severity=S2', '--why', 'x'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        _meta, body = frontmatter.parse(self.read())
        self.assertEqual(body.count(' — x'), 1)
        self.assertIn('set: severity S1 → S2 — x', body)


class SetListFieldTests(unittest.TestCase):
    """A Task's writes: and after: are list fields of `asf set`: =, += and -= forms, through the
    same parser and stage as every other field (T-0338: widening writes: was a hand edit)."""
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        self.task = write_item(self.root, 'T-0001', 'task', 'Do', parent='F-0001',
                               typed_lines=('writes: [src/a.py, src/b.py]',))
        write_item(self.root, 'T-0002', 'task', 'Other', parent='F-0001',
                   typed_lines=('writes: [lib/x.py]',))

    def meta(self):
        with open(self.task, encoding='utf-8') as f:
            return frontmatter.parse(f.read())[0]

    def test_writes_add_appends_only_what_is_missing(self):
        r = run(['set', 'T-0001', 'writes+=[docs/p.md, src/a.py]'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['writes'], ['src/a.py', 'src/b.py', 'docs/p.md'])
        self.assertIn('writes=src/a.py src/b.py docs/p.md', r.stdout)

    def test_writes_remove_and_replace(self):
        r = run(['set', 'T-0001', 'writes-=src/b.py'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['writes'], ['src/a.py'])
        r = run(['set', 'T-0001', 'writes=[x.py, y.py]'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['writes'], ['x.py', 'y.py'])

    def test_after_add_then_remove(self):
        r = run(['set', 'T-0001', 'after+=T-0002'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['after'], ['T-0002'])
        r = run(['set', 'T-0001', 'after-=T-0002', 'writes+=c.py'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta().get('after') or [], [])
        self.assertIn('c.py', self.meta()['writes'])

    def test_emptying_writes_is_refused(self):
        with open(self.task, encoding='utf-8') as f:
            before = f.read()
        r = run(['set', 'T-0001', 'writes-=[src/a.py, src/b.py]'], self.root)
        self.assertEqual(r.returncode, 2)
        with open(self.task, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)

    def test_add_form_on_a_scalar_field_is_refused(self):
        r = run(['set', 'T-0001', 'rank+=3'], self.root)
        self.assertEqual(r.returncode, 2)
        r = run(['set', 'B-0001', 'writes+=x.py'], self.root)
        self.assertEqual(r.returncode, 2)


class SetListFieldNormalisationTests(unittest.TestCase):
    """T-0500: both sides of a list-field op are flattened first, so a plan's packed entry
    (``writes: [a.py b.py]``) is the paths it names, not the string it packed them into."""
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        write_item(self.root, 'T-0002', 'task', 'Other', parent='F-0001')
        write_item(self.root, 'T-0003', 'task', 'Another', parent='F-0001')
        self.task = write_item(self.root, 'T-0001', 'task', 'Do', parent='F-0001',
                               typed_lines=('writes: [asf/a.py asf/b.py, docs/g.md]',
                                            'after: [T-0002 T-0003]'))

    def read(self):
        with open(self.task, encoding='utf-8') as f:
            return f.read()

    def meta(self):
        return frontmatter.parse(self.read())[0]

    def test_removing_a_path_from_a_packed_entry_splits_it(self):
        r = run(['set', 'T-0001', 'writes-=asf/b.py'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['writes'], ['asf/a.py', 'docs/g.md'])

    def test_add_already_covered_by_a_packed_entry_says_so_and_flattens_the_card(self):
        r = run(['set', 'T-0001', 'writes+=asf/a.py'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('already covers', r.stdout)
        self.assertEqual(self.meta()['writes'], ['asf/a.py', 'asf/b.py', 'docs/g.md'])

    def test_after_field_normalises_the_same_way(self):
        r = run(['set', 'T-0001', 'after-=T-0003'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['after'], ['T-0002'])

    def test_a_covered_add_on_an_already_flat_card_is_byte_identical(self):
        flat = write_item(self.root, 'T-0004', 'task', 'Flat', parent='F-0001',
                          typed_lines=('writes: [asf/a.py, asf/b.py]',))
        with open(flat, encoding='utf-8') as f:
            before = f.read()
        r = run(['set', 'T-0004', 'writes+=asf/a.py'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(flat, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)


class SetFootprintCoverageTests(unittest.TestCase):
    """T-0500 (D3, D8): on ``writes:`` an add the footprint already covers by glob is a no-op
    that says so, and a remove that would remove nothing is refused rather than reported done."""
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        self.task = write_item(self.root, 'T-0001', 'task', 'Do', parent='F-0001',
                               typed_lines=('writes: [asf/feeder/**]',))

    def read(self):
        with open(self.task, encoding='utf-8') as f:
            return f.read()

    def meta(self):
        return frontmatter.parse(self.read())[0]

    def test_covered_add_is_a_no_op_that_says_so(self):
        r = run(['set', 'T-0001', 'writes+=asf/feeder/rows.py'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('already covers asf/feeder/rows.py', r.stdout)
        self.assertEqual(self.meta()['writes'], ['asf/feeder/**'])

    def test_remove_of_a_covered_path_is_refused(self):
        before = self.read()
        r = run(['set', 'T-0001', 'writes-=asf/feeder/rows.py'], self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('asf/feeder/** covers it', r.stderr)
        self.assertEqual(self.read(), before)

    def test_remove_of_a_path_the_field_does_not_name_is_refused(self):
        before = self.read()
        r = run(['set', 'T-0001', 'writes-=nope.py'], self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('does not name it', r.stderr)
        self.assertEqual(self.read(), before)

    def test_coverage_rule_is_widen_covered_directly(self):
        from asf.feeder import widen
        self.assertTrue(widen.covered('asf/feeder/rows.py', ['asf/feeder/**']))

    def test_a_covered_pair_in_one_command_refuses_the_second_op(self):
        before = self.read()
        r = run(['set', 'T-0001', 'writes+=asf/feeder/rows.py', 'writes-=asf/feeder/rows.py'],
                self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(self.read(), before)

    def test_an_uncovered_pair_in_one_command_exits_0_with_the_footprint_unchanged(self):
        r = run(['set', 'T-0001', 'writes+=docs/z.md', 'writes-=docs/z.md'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta()['writes'], ['asf/feeder/**'])


class SetFootprintStageTests(unittest.TestCase):
    """T-0500 (P2, P3): `writes+=` goes through the same stage and publish as every other typed
    write — an intersecting footprint is refused before it is written, and a successful write is
    committed and pushed, with `index.json` re-derived in the same commit."""
    ACTIVE = ('schema_version: 1', 'state: Active', 'stage_since: 2026-01-01T00:00:00Z',
             'updated: 2026-01-01T00:00:00Z')

    def test_an_intersecting_write_is_refused_before_it_is_written(self):
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write_item(root, 'E-0001', 'epic', 'Factory')
        write_item(root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        write_item(root, 'T-0002', 'task', 'Other', parent='F-0001',
                  typed_lines=('writes: [lib/x.py]',), machine_lines=self.ACTIVE)
        t1 = write_item(root, 'T-0001', 'task', 'Do', parent='F-0001',
                        typed_lines=('writes: [src/a.py]',), machine_lines=self.ACTIVE)
        with open(t1, encoding='utf-8') as f:
            before = f.read()
        r = run(['set', 'T-0001', 'writes+=lib/x.py'], root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('I3', r.stderr)
        self.assertIn('writes: intersects', r.stderr)
        with open(t1, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)

    def test_a_successful_write_is_committed_and_pushed(self):
        tmp = tempfile.mkdtemp(prefix='setpub_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        origin = os.path.join(tmp, 'origin.git')
        root = os.path.join(tmp, 'record')

        def git(*a, cwd=None):
            return subprocess.run(['git', *a], cwd=cwd or root, capture_output=True, text=True,
                                  check=True).stdout.strip()

        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', origin], check=True)
        subprocess.run(['git', 'clone', '-q', origin, root], check=True)
        git('config', 'user.name', 'T')
        git('config', 'user.email', 't@x')
        for f in FOLDERS:
            os.makedirs(os.path.join(root, f), exist_ok=True)
        write_item(root, 'E-0001', 'epic', 'Factory')
        write_item(root, 'F-0001', 'feature', 'Thing', parent='E-0001')
        write_item(root, 'T-0001', 'task', 'Do', parent='F-0001',
                  typed_lines=('writes: [src/a.py]',), machine_lines=self.ACTIVE)
        run(['index'], root)  # so index.json already exists and is re-derived by the set below
        git('add', '-A')
        git('commit', '-qm', 'seed')
        git('push', '-q', 'origin', 'HEAD:main')

        r = run(['set', 'T-0001', 'writes+=src/b.py'], root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(git('status', '--porcelain'), '')
        self.assertEqual(git('rev-parse', 'HEAD'), git('rev-parse', 'main', cwd=origin))
        self.assertEqual(git('log', '-1', '--format=%s'), 'record: set T-0001')
        changed = git('show', '--stat', '--format=', 'HEAD')
        self.assertIn('tasks/T-0001.md', changed)
        self.assertIn('index.json', changed)


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

    def test_a_review_proven_acceptance_line_still_reads_as_one(self):
        # §2.5/D8: `proves.tick`'s ` — <path>` suffix rides on the bullet's own text, so
        # `check.ACCEPTANCE_ITEM_RE` still finds the ticked line.
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'S-0104', 'story', 'The tick', parent='F-0001', body=(
            "## Description\n\n## Acceptance\n"
            "- [x] the table is parsed and the round's verdict comes from it"
            " — docs/reviews/3-t-0176.md\n"
            "- [ ] a fail row does not tick\n\n"
            "## Non-goals\n\n## History\n- 2026-01-01: created\n\n"
            "## Children\n\n## Backlinks\n"))
        write_item(self.root, 'T-0001', 'task', 'Do it', parent='F-0001',
                   typed_lines=('stories: [S-0104]',))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

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

    def test_task_writing_the_amendable_set_is_a_finding(self):
        # F-0024 §2.6: a Task whose writes: reaches the amendable set can only ever be
        # refused — asf check says so at plan time, before the hook ever has to
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        os.makedirs(os.path.join(home, 'products'))
        old_home = env.ASF_HOME
        env.ASF_HOME = home
        self.addCleanup(lambda: setattr(env, 'ASF_HOME', old_home))
        with open(os.path.join(home, 'products', 'demo.yaml'), 'w') as f:
            f.write('product: demo\nmain: main\n')
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'T-0001', 'task', 'Touch the rules', parent='F-0001',
                   typed_lines=["writes: [rules/*]"])
        run(['index'], self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = check_mod.cmd_check(argparse.Namespace(product='demo', paths=[]), self.root)
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("writes: 'rules/*' reaches the amendable set", out.getvalue())
        self.assertIn('file a proposal instead (F-0024)', out.getvalue())

    def test_task_writing_the_amendable_set_with_no_product_is_clean(self):
        # PD12: a product that will not load is "no amendable check", not a crash
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'T-0001', 'task', 'Touch the rules', parent='F-0001',
                   typed_lines=["writes: [rules/*]"])
        run(['index'], self.root)
        r = run(['check'], self.root)  # no --product: the subprocess resolves none
        self.assertNotIn('reaches the amendable set', r.stdout)

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


class ProtectedNameInTypedFieldTests(unittest.TestCase):
    """F-0132 §1/§3.1: `asf check` flags a protected name in any typed field, at the
    card that holds it, on that field's line — naming the field and the pattern source, never
    the matched text. A secret is left to the harvest redaction scan, not duplicated here."""

    def setUp(self):
        self.root = make_repo()
        for f in STREAM_FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, 'tools'), exist_ok=True)
        with open(os.path.join(self.root, 'tools', 'forbidden-names.txt'), 'w') as f:
            f.write('Zorblax\n')

    def test_the_card_that_holds_the_name_gets_one_finding_naming_the_field_and_source(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1, r.stdout)
        lines = [ln for ln in r.stdout.splitlines() if 'carries a name' in ln]
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertTrue(lines[0].startswith('features/F-0001.md:'), lines[0])
        self.assertIn('title:', lines[0])
        self.assertIn('tools/forbidden-names.txt', lines[0])
        self.assertNotIn('Zorblax', lines[0])

    def test_a_name_in_another_typed_field_is_found_too_and_title_is_reported_first(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001',
                   typed_lines=['areas: [Zorblax]'])
        run(['index'], self.root)
        r = run(['check'], self.root)
        lines = [ln for ln in r.stdout.splitlines() if 'carries a name' in ln]
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertIn('title:', lines[0])
        self.assertIn('areas:', lines[1])

    def test_the_machine_block_is_never_scanned(self):
        # D6: the machine block is ASF's own vocabulary, not where an operator writes a name
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001',
                   machine_lines=['schema_version: 1', 'state: Zorblax',
                                  'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertNotIn('carries a name', r.stdout)

    def test_patching_default_patterns_to_empty_is_the_only_way_to_reach_no_findings(self):
        # PD7: default_patterns is effectively never empty (it always carries the built-in secret
        # rules), so removing a name list proves nothing — only a patched, degraded
        # default_patterns reaches protected_fields' `if not pats: return []` guard.
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        run(['index'], self.root)
        with mock.patch.object(redact, 'default_patterns', return_value=[]):
            findings, _warnings, _index_wrong = check_mod.record_findings(self.root)
        self.assertFalse(any('carries a' in msg for _p, _l, msg in findings), findings)

    def test_a_secret_in_a_typed_field_gives_no_finding_here(self):
        # a secret is the harvest redaction scan's own job (every line of every file, this one
        # included) — a second, earlier finding for it over `asf check` would only race that scan
        # and, for a record branch, report under the wrong name before it ever runs. Built from
        # parts so this file's own text never carries the shape check_generic.sh forbids.
        secret = 'AK' + 'IA' + 'ABCDEFGHIJKLMNOP'
        write_item(self.root, 'E-0001', 'epic', f'Rotate {secret} now')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertNotIn('carries a', r.stdout)
        self.assertNotIn(secret, r.stdout)

    def test_exits_1_with_the_line_printed_as_relpath_colon_line_colon_message(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        found = [ln for ln in r.stdout.splitlines() if 'carries a name' in ln]
        self.assertEqual(len(found), 1, r.stdout)
        path, line, _msg = found[0].split(':', 2)
        self.assertEqual(path, 'features/F-0001.md')
        self.assertTrue(line.isdigit(), found[0])


class IndexTitleScrubTests(unittest.TestCase):
    """F-0132 §4/§3.3: no `index.json` entry copies a protected name — the title is scrubbed
    where the entry is built, which closes every reader of it with one seam."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, 'tools'), exist_ok=True)
        with open(os.path.join(self.root, 'tools', 'forbidden-names.txt'), 'w') as f:
            f.write('Zorblax\n')

    def _index(self):
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_the_entry_title_is_scrubbed_the_cards_own_title_is_not(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        run(['index'], self.root)
        data = self._index()
        self.assertEqual(data['items']['F-0001']['title'], 'Pay for [redacted] account')
        self.assertNotIn('Zorblax', json.dumps(data))
        card = open(os.path.join(self.root, 'features', 'F-0001.md'), encoding='utf-8').read()
        self.assertIn('title: Pay for Zorblax account', card)

    def test_a_second_index_changes_no_byte(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        run(['index'], self.root)
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            first = f.read()
        run(['index'], self.root)
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            second = f.read()
        self.assertEqual(first.split('"generated"')[1], second.split('"generated"')[1])

    def test_check_reports_the_index_clean_not_stale_on_an_unchanged_record(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertNotIn('index.json is stale', r.stdout)

    def test_with_no_patterns_matching_the_entry_title_is_byte_identical_to_todays(self):
        # PD7: patching default_patterns to `[]` is the only way to reach the no-scrub branch —
        # removing the name list leaves the built-in secret rules, which never match a plain name
        from asf.record.core import (
            build_index_data, canonicalize, compute_derived, load_items, title_scrub,
        )
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        with mock.patch.object(redact, 'default_patterns', return_value=[]):
            by_id, _errors = load_items(self.root)
            canonical, _dupes = canonicalize(by_id)
            derived = compute_derived(canonical)
            data = build_index_data(canonical, derived, title_scrub(self.root))
        self.assertEqual(data['items']['F-0001']['title'], 'Pay for Zorblax account')

    def test_a_writer_without_the_name_lists_keeps_the_scrubbed_titles(self):
        # F-0273: a session whose environment lacks the name lists re-rendered every scrubbed
        # title raw, and the record push was refused for names index.json never held
        from asf.record.core import canonicalize, compute_derived, load_items
        from asf.record.index import write_index_json
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Pay for Zorblax account', parent='E-0001')
        write_item(self.root, 'F-0002', 'feature', 'Zorblax plan', parent='E-0001')
        run(['index'], self.root)
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            before = f.read()
        write_item(self.root, 'F-0002', 'feature', 'Zorblax plan, renamed', parent='E-0001')
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        derived = compute_derived(canonical)
        write_index_json(self.root, canonical, derived, scrub=None)
        items = self._index()['items']
        self.assertIn('"Pay for [redacted] account"', before)
        self.assertEqual(items['F-0001']['title'], 'Pay for [redacted] account')
        self.assertEqual(items['F-0002']['title'], 'Zorblax plan, renamed')  # a changed title is new
        self.assertFalse(write_index_json(self.root, canonical, derived, scrub=None))

    def test_shuffled_load_orders_render_byte_identical_index_json(self):
        # F-0273: the pre-commit, `asf index` and the tick each load the cards their own way
        import random
        from asf.record.core import (
            build_index_data, canonicalize, compute_derived, load_items, render_index_json,
        )
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'E-0002', 'epic', 'Ops, see E-0001')
        for n in range(1, 9):
            write_item(self.root, f'F-000{n}', 'feature', f'Feature {n} for Zorblax',
                       parent='E-0001' if n % 2 else 'E-0002',
                       body=f"## Description\nSee F-000{9 - n} and E-0001.\n\n"
                            "## Children\n\n## Backlinks\n")
        write_item(self.root, 'B-0001', 'bug', 'A bug on F-0003', parent='E-0002')
        by_id, _errors = load_items(self.root)
        renders = set()
        for seed in (1, 2, 3):
            ids = list(by_id)
            random.Random(seed).shuffle(ids)
            canonical, _dupes = canonicalize({iid: by_id[iid] for iid in ids})
            data = build_index_data(canonical, compute_derived(canonical), str.upper)
            data['generated'] = 'fixed'
            renders.add(render_index_json(data))
        self.assertEqual(len(renders), 1)


class CheckOverlapTests(unittest.TestCase):
    """The spec's acceptance 2: `asf check`'s Active×Active loop reads the same
    `invariants.unordered_overlaps` as I3 (C2) — one line per unordered pair regardless of how
    many globs it intersects on (PD5), none for an ordered pair, none for a removed: card."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')

    def task(self, id_, writes, after=None, removed=None):
        typed = [f"writes: [{writes}]"]
        if after:
            typed.append(f"after: [{after}]")
        if removed:
            typed.append(f"removed: {removed}")
        write_item(self.root, id_, 'task', id_, parent='F-0001', typed_lines=typed,
                   machine_lines=['state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'])

    def lines(self, out):
        return [l for l in out.splitlines() if 'intersects Active task' in l]

    def test_an_unordered_pair_is_one_line(self):
        self.task('T-0001', 'lib/x.py')
        self.task('T-0002', 'lib/x.py')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(len(self.lines(r.stdout)), 1, r.stdout)

    def test_an_unordered_pair_on_two_globs_is_still_one_line(self):
        self.task('T-0001', 'lib/a.py, lib/b.py')
        self.task('T-0002', 'lib/a.py, lib/b.py')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(len(self.lines(r.stdout)), 1, r.stdout)

    def test_an_ordered_pair_is_no_line(self):
        self.task('T-0001', 'lib/x.py')
        self.task('T-0002', 'lib/x.py', after='T-0001')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(self.lines(r.stdout), [])

    def test_a_removed_card_is_no_line(self):
        self.task('T-0001', 'lib/x.py')
        self.task('T-0002', 'lib/x.py', removed='2026-01-02')
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(self.lines(r.stdout), [])


class SupersessionCheckTests(unittest.TestCase):
    """§3.2 / T-0256: `asf check` fails a supersession that dangles, cycles or is written on only
    one card — a record built with `write_item`, then `asf index`, then `asf check`."""

    def setUp(self):
        self.root = make_repo()
        for f in STREAM_FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def decision(self, id_, typed_lines=()):
        write_item(self.root, id_, 'decision', id_, typed_lines=typed_lines)

    def rule(self, id_, typed_lines=()):
        write_item(self.root, id_, 'rule', id_, typed_lines=typed_lines)

    def check(self):
        run(['index'], self.root)
        return run(['check'], self.root)

    def test_dangling_supersedes_finding(self):
        self.decision('D-0051', typed_lines=('supersedes: [D-0099]',))
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('decisions/D-0051.md:', r.stdout)
        self.assertIn('supersedes references missing item D-0099', r.stdout)

    def test_dangling_superseded_by_finding(self):
        self.decision('D-0042', typed_lines=('superseded_by: D-0099',))
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('decisions/D-0042.md:', r.stdout)
        self.assertIn('superseded_by references missing item D-0099', r.stdout)

    def test_half_written_pair_names_the_other_card_and_the_fix(self):
        self.decision('D-0051', typed_lines=('supersedes: [D-0042]',))
        self.decision('D-0042')
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('decisions/D-0042.md:', r.stdout)
        self.assertIn('D-0051 supersedes D-0042, which carries no superseded_by: D-0051', r.stdout)
        self.assertIn('asf set D-0042 superseded_by=D-0051', r.stdout)
        # the fix the message names is run, not just printed — the two SETTABLE lines proved
        r = run(['set', 'D-0042', 'superseded_by=D-0051'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_half_written_pair_mirror(self):
        self.decision('D-0042', typed_lines=('superseded_by: D-0051',))
        self.decision('D-0051')
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('decisions/D-0051.md:', r.stdout)
        self.assertIn('D-0042 is superseded_by D-0051, which does not list D-0042 in supersedes:', r.stdout)
        self.assertIn('asf set D-0051 supersedes=[D-0042]', r.stdout)
        r = run(['set', 'D-0051', 'supersedes=[D-0042]'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_three_card_ring_is_one_finding(self):
        self.decision('D-0001', typed_lines=('superseded_by: D-0002',))
        self.decision('D-0002', typed_lines=('superseded_by: D-0003',))
        self.decision('D-0003', typed_lines=('superseded_by: D-0001',))
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        rings = [l for l in r.stdout.splitlines() if 'supersession cycle' in l]
        self.assertEqual(len(rings), 1, r.stdout)
        self.assertTrue(rings[0].startswith('decisions/D-0001.md:'), rings[0])
        self.assertIn('supersession cycle D-0001 → D-0002 → D-0003 → D-0001', rings[0])

    def test_self_ring(self):
        self.decision('D-0001', typed_lines=('supersedes: [D-0001]', 'superseded_by: D-0001'))
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('supersession cycle D-0001 → D-0001', r.stdout)

    def test_a_correct_pair_and_a_five_card_chain_pass_clean(self):
        self.decision('D-0010', typed_lines=('supersedes: [D-0011]',))
        self.decision('D-0011', typed_lines=('superseded_by: D-0010',))
        chain = [f'D-00{i}' for i in range(20, 25)]
        for idx, id_ in enumerate(chain):
            typed = []
            if idx > 0:
                typed.append(f'superseded_by: {chain[idx - 1]}')
            if idx < len(chain) - 1:
                typed.append(f'supersedes: [{chain[idx + 1]}]')
            self.decision(id_, typed_lines=tuple(typed))
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('supersedes', r.stdout)
        self.assertNotIn('supersession', r.stdout)

    def test_cross_type_pair_is_not_a_finding(self):
        self.rule('R-0007', typed_lines=('superseded_by: D-0051',))
        self.decision('D-0051', typed_lines=('supersedes: [R-0007]',))
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_findings_sort_among_the_others_by_path_then_line(self):
        # a record with both a dangling parent and a dangling successor prints both, ordered
        self.decision('D-0001', typed_lines=('supersedes: [D-0098]', 'superseded_by: D-0099'))
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)
        lines = [l for l in r.stdout.splitlines() if 'decisions/D-0001.md:' in l
                 and ('references missing item' in l)]
        self.assertEqual(len(lines), 2, r.stdout)
        parsed = [(l.split(':')[0], int(l.split(':')[1])) for l in lines]
        self.assertEqual(parsed, sorted(parsed))


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


class S1RemedyRunsTests(unittest.TestCase):
    """F-0163 S-36203 (PD6): the S1 gate's `NEEDS OPERATOR` remedy is a command that actually
    runs — taken from the product, never hand-typed, so the gate's advice and the command's
    gate are proven to agree."""

    def test_the_printed_remedy_runs_against_a_real_record(self):
        import shlex

        from asf.env import Product
        from asf.feeder import tiers

        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write_item(root, 'E-0001', 'epic', 'Factory')
        bug = write_item(root, 'B-0057', 'bug', 'Nothing works it', parent='E-0001',
                         typed_lines=('severity: S1',))
        product = Product('sample', {})
        _text, cmd = tiers._remedy('B-0057', 'operator', 'adjudicated after 4 sessions', product)
        self.assertIn('--why', cmd)
        parts = shlex.split(cmd)[1:]  # drop the leading `asf`
        if '--product' in parts:  # the record is the subprocess's cwd; no product file here
            i = parts.index('--product')
            del parts[i:i + 2]
        r = run(parts, root)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(bug, encoding='utf-8') as f:
            meta, body = frontmatter.parse(f.read())
        self.assertEqual(meta['severity'], 'S2')
        self.assertIn('set: severity S1 → S2 — <why this is not S1>', body)


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
