import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf.record.core import canonicalize, load_items
from asf.tick import migrate

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']

GOALS_FIXTURE = (
    'ROADMAP 2026-09-21 (max value fast). Epic = GOAL; Feature = spec+plan id.\n'
    '\n'
    'GOAL 1 — TEST EPIC. ON PROD: nothing yet. NEXT: FREE-1 (spec cloud/spec-free-plan) '
    '→ plan. BLOCKED: Ops — needs a key.\n'
    '\n'
    'GOAL 2 — SECOND EPIC. ON PROD: nothing. BLOCKED: —.\n'
)

PLAN_FIXTURE = (
    '# FREE-1 — Free plan\n'
    '\n'
    '## Task 1: Wire the model\n'
    'Files: packages/billing/src/plans.ts, packages/billing/src/plans.test.ts\n'
    'Touches F-ID-1.\n'
    '\n'
    '## Task 2: Ship the UI\n'
    'No files line here.\n'
    '\n'
    '## Task 3: Wire it up DONE\n'
    'Dispatch: T3 -> cloud/free-plan-t3\n'
)

SPEC_FIXTURE = (
    '# FREE-1 — Free plan\n'
    '\n'
    'A free plan lets a member sign up with no card. It cites F-ID-1 directly.\n'
    '\n'
    'More prose that is not part of the first paragraph.\n'
)

D_ROW_FIXTURE = (
    '| # | Question | Default applied |\n'
    '| --- | --- | --- |\n'
    '| D42 | Is the sky blue? | **Decided (2026-09-02): Yes, the sky is blue.** '
    'Extra context about the sky. ADR 0042 |\n'
)

NON_RULING_TABLE = (
    '| Id | Note |\n'
    '| --- | --- |\n'
    '| D295 · something | not a ruling |\n'
)

EMPTY_SRC = {
    'design_spec_text': None, 'sdd_decisions_text': None, 'main_spec_texts': {},
    'hotfix_texts': {}, 'open_branches': [],
}


def make_repo():
    root = tempfile.mkdtemp(prefix='migrate_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


class ParseGoalsTests(unittest.TestCase):
    def test_two_blocks_in_file_order(self):
        goals = migrate.parse_goals(GOALS_FIXTURE)
        self.assertEqual([g['num'] for g in goals], ['1', '2'])
        self.assertEqual([g['legacy_id'] for g in goals], ['GOAL 1', 'GOAL 2'])

    def test_title_case_and_description(self):
        goals = migrate.parse_goals(GOALS_FIXTURE)
        g1 = goals[0]
        self.assertEqual(g1['title'], 'Test Epic')
        self.assertEqual(g1['description'], 'ON PROD: nothing yet.')

    def test_blocked_by_name_dash_becomes_name_colon(self):
        goals = migrate.parse_goals(GOALS_FIXTURE)
        self.assertEqual(goals[0]['blockedBy'], ['Ops: needs a key'])

    def test_em_dash_only_blocked_is_empty(self):
        goals = migrate.parse_goals(GOALS_FIXTURE)
        self.assertEqual(goals[1]['blockedBy'], [])


class PlanTaskRecordsTests(unittest.TestCase):
    def test_three_tasks_in_order_with_titles(self):
        records = migrate.plan_task_records(PLAN_FIXTURE)
        self.assertEqual([r['tid'] for r in records], ['T1', 'T2', 'T3'])
        self.assertEqual(records[0]['title'], 'Wire the model')
        self.assertEqual(records[1]['title'], 'Ship the UI')

    def test_done_marker_stripped_from_title(self):
        records = migrate.plan_task_records(PLAN_FIXTURE)
        t3 = next(r for r in records if r['tid'] == 'T3')
        self.assertEqual(t3['title'], 'Wire it up')

    def test_writes_lines_parses_files_line(self):
        records = migrate.plan_task_records(PLAN_FIXTURE)
        t1 = next(r for r in records if r['tid'] == 'T1')
        writes = migrate.writes_lines(t1['body'])
        self.assertEqual(writes, ['packages/billing/src/plans.ts',
                                  'packages/billing/src/plans.test.ts'])

    def test_no_files_line_returns_none(self):
        records = migrate.plan_task_records(PLAN_FIXTURE)
        t2 = next(r for r in records if r['tid'] == 'T2')
        self.assertIsNone(migrate.writes_lines(t2['body']))

    def test_story_token_found_in_task_body(self):
        records = migrate.plan_task_records(PLAN_FIXTURE)
        t1 = next(r for r in records if r['tid'] == 'T1')
        self.assertEqual(migrate.STORY_TOKEN_RE.findall(t1['body']), ['F-ID-1'])


class DecisionRowTests(unittest.TestCase):
    def test_parses_legacy_id_title_date_statement(self):
        line = D_ROW_FIXTURE.splitlines()[2]
        row = migrate.parse_decision_row(line)
        self.assertEqual(row['legacy_id'], 'D42')
        self.assertEqual(row['id_num'], 42)
        self.assertEqual(row['title'], 'Is the sky blue?')
        self.assertEqual(row['date'], '2026-09-02')
        self.assertEqual(row['statement'], 'Yes, the sky is blue.')
        self.assertIn('Extra context about the sky.', row['context'])

    def test_find_decision_rows_needs_verdict_marker(self):
        # a traceability table whose row happens to start with `Dnnn` (feature-matrix-style)
        # is not a ruling and must not be mistaken for one.
        rows = migrate.find_decision_rows(NON_RULING_TABLE)
        self.assertEqual(rows, [])

    def test_find_decision_rows_returns_genuine_rows(self):
        rows = migrate.find_decision_rows(D_ROW_FIXTURE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['legacy_id'], 'D42')

    def test_supersedes_extraction_excludes_self_reference(self):
        line = ('| D99 | Question | **Decided (2026-09-02): New rule,** superseding '
               'D42 entirely; also see D99 elsewhere. |')
        row = migrate.parse_decision_row(line)
        self.assertEqual(row['supersedes'], ['D-0042'])

    def test_linkify_decisions_matches_checks_own_bare_regex(self):
        text = 'See D171 and the range D607-D616 for context.'
        linked = migrate.linkify_decisions(text)
        from asf.record.core import BARE_DECISION_RE
        self.assertEqual(BARE_DECISION_RE.findall(linked), [])
        self.assertIn('[[D-0171]]', linked)
        self.assertIn('[[D-0607]]', linked)
        self.assertIn('[[D-0616]]', linked)


class MigrateEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.goals_path = os.path.join(self.root, 'goals.txt')
        with open(self.goals_path, 'w', encoding='utf-8') as f:
            f.write(GOALS_FIXTURE)

        self.ev = {
            'features': {
                'free-plan': {
                    'alias': 'FREE-1', 'spec': 'origin/main:docs/x.md', 'spec_branch': None,
                    'spec_on_main': True, 'spec_review': None,
                    'plan': 'origin/main:docs/plan.md', 'plan_branch': None, 'plan_on_main': True,
                    'plan_review': (1, 'APPROVED', 'r1.md'),
                    'tasks': {'T1': {'branch': 'cloud/free-plan-t1', 'pr': 601,
                                     'pr_state': 'MERGED', 'merged_sha': 'd' * 40},
                             'T2': {'branch': None, 'pr': None, 'pr_state': None,
                                    'merged_sha': None},
                             'T3': {'branch': 'cloud/free-plan-t3', 'pr': None,
                                    'pr_state': None, 'merged_sha': None}},
                    'prs': [601],
                },
            },
            'stories': {
                'F-ID-1': {'status': 'doing', 'impl': ['a.ts'], 'test': ['a.test.ts'],
                          'area': 'billing', 'milestone': 'M1', 'cap': 'Sign in via the seam.'},
            },
            'prod_sha': None, 'dev_sha': None, 'checked': set(), 'main_sha': None,
            'merged': {601: 'd' * 40}, 'branches': ['cloud/free-plan-t1', 'cloud/free-plan-t3'],
        }
        self.src = dict(EMPTY_SRC, design_spec_text=D_ROW_FIXTURE,
                        hotfix_texts={'origin/cloud/fix-x:.sdd-input/hotfix-x-report.md':
                                      'CI failure: workflow red.\nError: something broke.\n'},
                        open_branches=['cloud/fix-x'])
        self.doc_texts = {'origin/main:docs/x.md': SPEC_FIXTURE,
                          'origin/main:docs/plan.md': PLAN_FIXTURE}

    def run_migrate(self, dry_run=False):
        args = types.SimpleNamespace(dry_run=dry_run, fresh=False, product=None)
        with mock.patch.object(migrate, 'GOALS_PATH', self.goals_path), \
             mock.patch.object(migrate.evidence, 'load', return_value=self.ev), \
             mock.patch.object(migrate.evidence, 'migrate_sources', return_value=self.src), \
             mock.patch.object(migrate.evidence, 'read_refs', return_value=self.doc_texts):
            return migrate.cmd_migrate(args, self.root)

    def _read_all(self):
        snapshot = {}
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            for name in sorted(os.listdir(d)):
                with open(os.path.join(d, name), encoding='utf-8') as fh:
                    snapshot[os.path.join(f, name)] = fh.read()
        return snapshot

    def test_creates_epics_feature_story_task_bug_decision(self):
        self.assertEqual(self.run_migrate(), 0)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'epics'))), 2)
        # Just the one real Feature (free-plan): unlike the original product's migrate, a
        # generic ASF has no fixed "not built yet" roadmap cards baked into the tool — those
        # were product backlog content, not migration logic.
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'features'))), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'stories'))), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'tasks'))), 3)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'bugs'))), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'decisions'))), 1)

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_migrate(dry_run=True), 0)
        self.assertEqual(os.listdir(os.path.join(self.root, 'epics')), [])
        self.assertEqual(os.listdir(os.path.join(self.root, 'decisions')), [])

    def test_feature_matched_to_epic_by_alias_mention(self):
        self.run_migrate()
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        fid = next(iid for iid, r in canonical.items()
                  if r['meta'].get('legacy_id') == 'FREE-1')
        epic_id = next(iid for iid, r in canonical.items()
                       if r['meta'].get('legacy_id') == 'GOAL 1')
        self.assertEqual(canonical[fid]['meta'].get('parent'), epic_id)

    def test_decision_body_has_no_bare_reference(self):
        self.run_migrate()
        text = open(os.path.join(self.root, 'decisions', 'D-0042.md'), encoding='utf-8').read()
        from asf.record.core import BARE_DECISION_RE
        self.assertNotRegex(text.split('---', 2)[2], BARE_DECISION_RE)

    def test_second_run_is_a_byte_no_op(self):
        self.run_migrate()
        before = self._read_all()
        rc = self.run_migrate()
        self.assertEqual(rc, 0)
        after = self._read_all()
        self.assertEqual(before.keys(), after.keys())
        for k in before:
            self.assertEqual(before[k], after[k], k)


class MigrateConventionsTests(unittest.TestCase):
    """The goals file and the parity Epic come from the product's conventions, never a name."""

    def product(self, conventions, repo='/repo'):
        from asf import env
        return env.Product('p', {'repo_dir': repo, 'conventions': conventions})

    def resolve(self, conventions, repo='/repo'):
        with mock.patch.object(migrate.env, 'load_product', return_value=self.product(conventions, repo)):
            args = types.SimpleNamespace(product='p')
            return migrate.goals_file_path(args, migrate._conventions(args))

    def test_goals_file_under_the_repo_absolute_kept_unset_none(self):
        self.assertEqual(self.resolve({'goals_file': 'plans/goals.md'}), '/repo/plans/goals.md')
        self.assertEqual(self.resolve({'goals_file': '/elsewhere/g.txt'}), '/elsewhere/g.txt')
        self.assertIsNone(self.resolve({}))

    def test_no_goals_file_is_reported_not_raised(self):
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ev = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None, 'checked': set(),
              'main_sha': None, 'merged': {}, 'branches': []}
        out = []
        with mock.patch.object(migrate.env, 'load_product', return_value=self.product({})), \
                mock.patch.object(migrate.evidence, 'load', return_value=ev), \
                mock.patch.object(migrate.evidence, 'migrate_sources', return_value=dict(EMPTY_SRC)), \
                mock.patch('builtins.print', lambda *a, **k: out.append(' '.join(map(str, a)))):
            rc = migrate.cmd_migrate(types.SimpleNamespace(dry_run=True, fresh=False, product='p'), root)
        self.assertEqual(rc, 0)
        self.assertIn('epic (conventions.goals_file unset): no goals file — no Epics migrated', '\n'.join(out))


if __name__ == '__main__':
    unittest.main()
