"""asf reopen — correcting a falsely derived Resolved/Closed (T-0159-adjacent fix, 4bffe7c).

`asf set` refuses `state`/`stage`, and `closing.sticky` holds `Closed` forever once ingest
writes it — right when the closing was real, a trap when a detector bug (a PR body's stray
branch-path mention) produced it. `asf reopen` is the supported way out: re-derive from current
evidence with the terminal hold lifted for one id, refuse if the fresh evidence still lands it,
else write the corrected state/stage, a `reopened:` marker and a History line."""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf.cli import build_parser
from asf.record import frontmatter, ingest
from asf.record.reopen import cmd_reopen

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs', 'decision': 'decisions'}
BODY = ("## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n"
        "- 2026-01-01: created\n\n## Children\n\n## Backlinks\n")
EMPTY_EV = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
            'checked': set(), 'main_sha': None, 'merged': {}, 'branches': [], 'ids': {}}


def make_record():
    root = tempfile.mkdtemp(prefix='reopen_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def write(root, id_, type_, title='t', parent=None, typed=(), machine=None, body=BODY):
    machine = list(machine if machine is not None else
                   ('schema_version: 1', 'state: New', 'stage_since: 2026-01-01T00:00:00Z',
                    'updated: 2026-01-01T00:00:00Z'))
    lines = [f'id: {id_}', f'type: {type_}', f'title: {title}']
    if parent:
        lines.append(f'parent: {parent}')
    lines += list(typed) + ['# ---- machine ----'] + machine
    rel = f'{FOLDER_OF[type_]}/{id_}.md'
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n' + body)
    return rel


def read(root, rel):
    with open(os.path.join(root, rel), encoding='utf-8') as f:
        return f.read()


def meta(root, rel):
    return frontmatter.parse(read(root, rel), path=rel)[0]


class ReopenTestCase(unittest.TestCase):
    def setUp(self):
        self.root = make_record()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def reopen(self, id_, reason='a fixed detector no longer names it', ev=None):
        ev = ev if ev is not None else EMPTY_EV
        args = types.SimpleNamespace(id=id_, reason=reason, product=None)
        # asf.record.ingest and asf.record.reopen both `from asf.evidence import evidence` — one
        # patch of the shared module object covers whichever of them a test exercises
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            return cmd_reopen(args, self.root)


class TaskFalselyClosedTests(ReopenTestCase):
    """T-0359's shape: a Task Closed by a commit id-matched only through a PR body's stray
    branch-path mention (4bffe7c fixed the matcher); the branch it was really still working on
    (`cloud/T-0359`, in this test `cloud/T-0001`) is the only evidence a corrected pass finds."""

    def falsely_closed(self):
        rel = write(self.root, 'T-0001', 'task', parent='F-0001', typed=(
            "links: {plan: docs/plans/f-0001.md}",),
            machine=('schema_version: 1', 'state: Closed',
                     'stage_since: 2026-01-01T00:00:00Z',
                     'evidence:', '  - "commit deadbeef names T-0001 (PR #847)"',
                     '  - "rule: landed-green"',
                     'updated: 2026-01-01T00:00:00Z'))
        return rel

    def corrected_ev(self):
        return dict(EMPTY_EV, ci=True, ids={
            'T-0001': {'branches': ['cloud/T-0001'], 'commit': '', 'green': False,
                      'open_prs': [], 'pr': None}})

    def test_reopens_to_what_the_fixed_evidence_now_says(self):
        rel = self.falsely_closed()
        rc = self.reopen('T-0001', reason='false landing, PR #847 body mention (4bffe7c)',
                         ev=self.corrected_ev())
        self.assertEqual(rc, 0)
        m = meta(self.root, rel)
        self.assertEqual(m['state'], 'Active')
        self.assertNotIn('stage', m)  # a Task carries no stage

    def test_records_a_marker_and_a_history_line(self):
        rel = self.falsely_closed()
        self.reopen('T-0001', reason='false landing, PR #847 body mention (4bffe7c)',
                    ev=self.corrected_ev())
        m = meta(self.root, rel)
        self.assertEqual(len(m['reopened']), 1)
        self.assertIn('false landing, PR #847 body mention (4bffe7c)', m['reopened'][0])
        self.assertIn('Closed → Active', m['reopened'][0])
        body = read(self.root, rel)
        self.assertIn('reopen: false landing, PR #847 body mention (4bffe7c) '
                      '— state Closed → Active', body)

    def test_a_later_plain_ingest_does_not_re_close_it_from_the_same_evidence(self):
        """The point of lifting the terminal hold once: a normal `asf ingest` afterward — no
        bypass, the same still-uncorroborated evidence — must not put Closed back (closing.sticky
        would have, had the card still read Closed)."""
        rel = self.falsely_closed()
        ev = self.corrected_ev()
        self.assertEqual(self.reopen('T-0001', ev=ev), 0)
        self.assertEqual(meta(self.root, rel)['state'], 'Active')
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            rc = ingest.cmd_ingest(types.SimpleNamespace(fresh=False, product=None), self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(meta(self.root, rel)['state'], 'Active')

    def test_refuses_when_current_evidence_still_lands_it(self):
        rel = self.falsely_closed()
        before = read(self.root, rel)
        still_landed = dict(EMPTY_EV, ci=True, ids={
            'T-0001': {'branches': [], 'commit': 'a' * 40, 'green': True, 'open_prs': [],
                      'pr': None}})
        rc = self.reopen('T-0001', ev=still_landed)
        self.assertEqual(rc, 2)
        self.assertEqual(read(self.root, rel), before)  # untouched


class FeatureStageTests(ReopenTestCase):
    """F-0112's shape: a Feature Resolved (`stage: landed`) by the same false PR-body mention,
    with a real spec on trunk once that evidence is discarded — reopens to Active/spec-approved,
    both `state` and `stage` lines in the one History entry."""

    def falsely_resolved(self):
        return write(self.root, 'F-0001', 'feature', typed=(
            "links: {spec: docs/specs/f-0001.md}",),
            machine=('schema_version: 1', 'state: Resolved', 'stage: landed',
                     'stage_since: 2026-01-01T00:00:00Z',
                     'evidence:', '  - "commit deadbeef names F-0001 (PR #847)"',
                     '  - "rule: landed"', 'updated: 2026-01-01T00:00:00Z'))

    def test_reopens_state_and_stage_together(self):
        rel = self.falsely_resolved()
        ev = dict(EMPTY_EV, ci=True, features={
            'x': {'spec': 'origin/main:docs/specs/f-0001.md', 'spec_on_main': True, 'tasks': {}}})
        rc = self.reopen('F-0001', reason='false landing, PR #847 body mention (4bffe7c)', ev=ev)
        self.assertEqual(rc, 0)
        m = meta(self.root, rel)
        self.assertEqual(m['state'], 'Active')
        self.assertEqual(m['stage'], 'spec-approved')
        body = read(self.root, rel)
        self.assertIn('state Resolved → Active, stage landed → spec-approved', body)


class RefusalTests(ReopenTestCase):
    def test_unknown_id(self):
        self.assertEqual(self.reopen('T-9999'), 2)

    def test_not_resolved_or_closed_is_refused(self):
        rel = write(self.root, 'T-0002', 'task', parent='F-0001',
                   machine=('schema_version: 1', 'state: Active',
                            'stage_since: 2026-01-01T00:00:00Z',
                            'updated: 2026-01-01T00:00:00Z'))
        before = read(self.root, rel)
        self.assertEqual(self.reopen('T-0002'), 2)
        self.assertEqual(read(self.root, rel), before)

    def test_a_non_evidence_type_is_refused(self):
        write(self.root, 'D-0001', 'decision', typed=('decided_by: op', 'date: 2026-01-01'),
             machine=('schema_version: 1',))
        self.assertEqual(self.reopen('D-0001'), 2)


class ParserTests(unittest.TestCase):
    def test_reopen_parses_id_and_reason(self):
        args = build_parser().parse_args(['reopen', 'T-0001', '--reason', 'x'])
        self.assertEqual(args.command, 'reopen')
        self.assertEqual(args.id, 'T-0001')
        self.assertEqual(args.reason, 'x')

    def test_reason_is_required(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(['reopen', 'T-0001'])


if __name__ == '__main__':
    unittest.main()
