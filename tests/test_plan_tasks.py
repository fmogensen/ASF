"""B-0060: a plan the lane landed on the trunk becomes Task cards in the tick's record step —
before this, a Feature sat at plan-approved for ever with nothing for the feeder to launch."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.evidence import evidence
from asf.record import frontmatter
from asf.record import idcheck
from asf.record import plan_tasks

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks'}
PLAN = """# Plan F-0001

## Decisions
none

### Task 1: the reader
stories: S-0001, S-0009
writes: asf/record/reader.py, tests/test_reader.py

**Files**: asf/record/reader.py
**Steps**: write it
**Gate**: python3 -m unittest tests.test_reader

### Task 2: the table
stories: S-0001
writes: asf/views/table.py

**Steps**: draw it

coverage: 1/1 stories; uncovered: none
"""


def write_item(root, id_, type_, title, parent=None):
    lines = [f'id: {id_}', f'type: {type_}', f'title: {title}']
    if parent:
        lines.append(f'parent: {parent}')
    lines += ['# ---- machine ----', 'state: New', 'stage_since: 2026-09-01T00:00:00Z',
              'updated: 2026-09-01T00:00:00Z']
    path = os.path.join(root, FOLDER_OF[type_], f'{id_}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n## Description\n\n## History\n- 2026-09-01: created\n')


def read(root, type_, id_):
    with open(os.path.join(root, FOLDER_OF[type_], f'{id_}.md'), encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{id_}.md')


class PlanTasksTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='plan_tasks_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Read it', parent='F-0001')
        self.lines = []

    def ev(self, on_main=True):
        return {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                        'plan_on_main': on_main, 'spec_on_main': True}}}

    def mint(self, ev=None, text=PLAN):
        return plan_tasks.mint_plan_tasks(self.root, None, ev or self.ev(), out=self.lines.append,
                                          read_ref=lambda ref: text)

    def test_a_landed_plan_becomes_task_cards_once(self):
        made = self.mint()
        self.assertEqual(made, ['T-0001', 'T-0002'])
        meta, body = read(self.root, 'task', 'T-0001')
        self.assertEqual(meta['title'], 'the reader')
        self.assertEqual(meta['parent'], 'F-0001')
        self.assertIs(meta['decided'], True)
        self.assertEqual(meta['writes'], ['asf/record/reader.py', 'tests/test_reader.py'])
        self.assertEqual(meta['stories'], ['S-0001'])  # S-0009 is not in the record
        self.assertEqual(meta['links'], {'plan': 'docs/plans/f-0001.md'})
        self.assertEqual(meta['state'], 'New')
        self.assertIn('**Gate**: python3 -m unittest tests.test_reader', body)
        self.assertIn('created (plan F-0001)', body)
        self.assertEqual(read(self.root, 'task', 'T-0002')[0]['writes'], ['asf/views/table.py'])
        self.assertEqual(self.lines, ['plan-tasks: F-0001: 2 Task(s) from docs/plans/f-0001.md: T-0001, T-0002'])
        # read once: the second run mints nothing
        self.assertEqual(self.mint(), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, 'tasks'))), ['T-0001.md', 'T-0002.md'])

    def test_a_plan_still_on_its_branch_mints_nothing(self):
        self.assertEqual(self.mint(self.ev(on_main=False)), [])
        self.assertEqual(os.listdir(os.path.join(self.root, 'tasks')), [])

    def test_a_plan_without_task_headings_is_named_not_minted(self):
        self.assertEqual(self.mint(text='# Plan\n\nprose only\n'), [])
        self.assertEqual(self.lines, ['plan-tasks: F-0001: docs/plans/f-0001.md has no `### Task N:` heading — nothing to mint'])

    def test_a_task_t1_plan_mints(self):
        text = PLAN.replace('### Task 1:', '### Task T1:').replace('### Task 2:', '### Task T2 \u2014')
        self.assertEqual(self.mint(text=text), ['T-0001', 'T-0002'])

    def test_task_like_headings_that_parse_to_nothing_are_flagged(self):
        self.assertEqual(self.mint(text='# Plan\n\n### Task #1 - x\nbody\n'), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn('Task-like heading', self.lines[0])
        self.assertIn('parses to 0 Tasks', self.lines[0])


class DeclaredAndPhantomStories(PlanTasksTests):
    """F-0285 (a product's F-1144, 2026-10-07): its spec said Stories S-29500..S-29505 were
    "minted from the spec session's own range" — a cloud session, which cannot reach the record —
    and none existed. A Story the plan declares as a ``### S-…: <title>`` heading is minted here,
    before the Tasks that cite it; one its ``## Stories`` section cites and nothing declares
    refuses the plan whole."""

    DECL = PLAN.replace('coverage: 1/1 stories', '''## Stories

### S-29501: the reader reads
- [ ] it reads the file
- [ ] it refuses a broken one

coverage: 1/1 stories''').replace('stories: S-0001, S-0009', 'stories: S-0001, S-29501')

    def test_a_declared_story_is_minted_before_the_tasks_that_cite_it(self):
        made = self.mint(text=self.DECL)
        self.assertEqual(made, ['S-29501', 'T-0001', 'T-0002'], self.lines)
        meta, body = read(self.root, 'story', 'S-29501')
        self.assertEqual((meta['title'], meta['parent']), ('the reader reads', 'F-0001'))
        self.assertIn('- [ ] it reads the file\n- [ ] it refuses a broken one', body)
        self.assertEqual(read(self.root, 'task', 'T-0001')[0]['stories'], ['S-0001', 'S-29501'])

    def test_a_story_its_stories_section_cites_and_nothing_declares_refuses_the_plan(self):
        text = self.DECL.replace('### S-29501: the reader reads', '- S-29501 the reader reads')
        self.assertEqual(self.mint(text=text), [])
        self.assertIn('cites Story id(s) never minted: S-29501', self.lines[0])
        self.assertEqual(os.listdir(os.path.join(self.root, 'tasks')), [])

    def test_a_story_the_spec_declares_is_minted_with_the_plan(self):
        spec = '# Spec F-0001\n\n## Stories\n\n### S-29501: the reader reads\n- [ ] it reads\n'
        plan = self.DECL.replace('### S-29501: the reader reads', '- S-29501 the reader reads')
        ev = {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                      'plan_on_main': True, 'spec_on_main': True,
                                      'spec': 'origin/main:docs/specs/f-0001.md'}}}
        made = plan_tasks.mint_plan_tasks(
            self.root, None, ev, out=self.lines.append,
            read_ref=lambda ref: spec if 'specs/' in ref else plan)
        self.assertEqual(made, ['S-29501', 'T-0001', 'T-0002'], self.lines)

    BULLET = DECL.replace('### S-29501: the reader reads\n- [ ] it reads the file\n'
                          '- [ ] it refuses a broken one',
                          '- S-29501: the reader reads\n  - [ ] it reads the file\n'
                          '  - [ ] it refuses a broken one')

    def test_a_bullet_form_declaration_is_minted_with_its_acceptance(self):
        # F-0318: a spec's `- S-n: title` bullet, its `- [ ]` lines indented beneath, declares
        # the Story as the `### S-n: title` heading does
        self.assertIn('- S-29501: the reader reads\n  - [ ] it reads', self.BULLET)
        made = self.mint(text=self.BULLET)
        self.assertEqual(made, ['S-29501', 'T-0001', 'T-0002'], self.lines)
        meta, body = read(self.root, 'story', 'S-29501')
        self.assertEqual((meta['title'], meta['parent']), ('the reader reads', 'F-0001'))
        self.assertIn('- [ ] it reads the file\n- [ ] it refuses a broken one', body)

    def test_declared_stories_reads_bullets_and_stops_at_a_sibling(self):
        text = ('## Stories\n\n- S-29501: the reader reads\n  - [ ] it reads\n'
                '- S-29502: the writer writes\n  - [ ] it writes\n  - [x] it flushes\n'
                '- [ ] a loose checkbox\n\n## Notes\n- S-29503: not a Stories section\n')
        self.assertEqual(idcheck.declared_stories(text), {
            'S-29501': {'title': 'the reader reads', 'acceptance': ['it reads']},
            'S-29502': {'title': 'the writer writes', 'acceptance': ['it writes', 'it flushes']}})

    def test_a_fenced_bullet_declaration_is_still_ignored(self):
        text = '## Stories\n\n```\n- S-29501: an example\n  - [ ] shown, not declared\n```\n'
        self.assertEqual(idcheck.declared_stories(text), {})
        fenced = self.BULLET.replace('- S-29501: the reader reads\n  - [ ] it reads the file\n'
                                     '  - [ ] it refuses a broken one',
                                     '```\n- S-29501: the reader reads\n```\n')
        self.assertNotIn('S-29501', self.mint(text=fenced))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'stories', 'S-29501.md')))


class HeaderRoute(unittest.TestCase):
    """S-38950: the gate's third route — a plan whose only id carrier is its own H1."""

    DATED = 'docs/plans/2026-09-25-thing-plan.md'

    def ev(self, alias='F-0001'):
        fev = {'plan': f'origin/main:{self.DATED}', 'plan_on_main': True, 'spec_on_main': True}
        if alias is not None:
            fev['alias'] = alias
        return {'features': {'thing-plan': fev}}

    def test_plan_alias_returns_the_alias_of_the_entry_whose_plan_path_matches(self):
        self.assertEqual(plan_tasks.plan_alias(self.ev(), self.DATED), 'F-0001')

    def test_plan_alias_is_none_for_a_path_no_entry_carries(self):
        self.assertIsNone(plan_tasks.plan_alias(self.ev(), 'docs/plans/no-such-plan.md'))

    def test_own_plan_is_true_for_the_header_route(self):
        ref = f'origin/main:{self.DATED}'
        self.assertTrue(plan_tasks.own_plan('F-0001', ref, self.ev()))

    def test_own_plan_is_false_for_an_alias_that_is_another_id(self):
        # D2's floodgate: the fixture's legacy milestone plan opens `# M3 — infra`, and no card
        # is called M3 — tests.test_doc_lane_landing asserts this end to end for F-0026
        ref = f'origin/main:{self.DATED}'
        self.assertFalse(plan_tasks.own_plan('F-0026', ref, self.ev(alias='M3')))

    def test_own_plan_is_true_for_an_id_named_file_with_no_alias_at_all(self):
        # route one does not depend on route three
        ref = 'origin/main:docs/plans/f-0001.md'
        self.assertTrue(plan_tasks.own_plan('F-0001', ref, {'features': {}}))

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='plan_tasks_header_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Read it', parent='F-0001')
        self.lines = []

    def test_mint_plan_tasks_mints_for_an_undecided_feature_by_header_alone(self):
        made = plan_tasks.mint_plan_tasks(self.root, None, self.ev(), out=self.lines.append,
                                          read_ref=lambda ref: PLAN)
        self.assertEqual(made, ['T-0001', 'T-0002'])
        meta, _body = read(self.root, 'task', 'T-0001')
        self.assertEqual(meta['links'], {'plan': self.DATED})
        self.assertEqual(self.lines,
                         [f'plan-tasks: F-0001: 2 Task(s) from {self.DATED}: T-0001, T-0002'])


class SkipsSayWhy(unittest.TestCase):
    """S-38951: each of _mint's four outcomes for a Feature whose plan is on the trunk names
    itself in one line; the skips above it — no plan on the trunk, a Task child, a done or
    retired card — stay silent."""

    PLAN_PATH = 'docs/plans/2026-09-25-other-plan.md'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='plan_tasks_skips_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Read it', parent='F-0001')
        self.lines = []

    def ev(self, on_main=True, alias=None):
        fev = {'plan': f'origin/main:{self.PLAN_PATH}', 'plan_on_main': on_main,
               'spec_on_main': True}
        if alias is not None:
            fev['alias'] = alias
        return {'features': {'f-0001': fev}}

    def mint(self, ev=None, text=PLAN):
        return plan_tasks.mint_plan_tasks(self.root, None, ev if ev is not None else self.ev(),
                                          out=self.lines.append, read_ref=lambda ref: text)

    def test_the_gate_refused_an_undecided_card_names_the_plan_and_the_reason(self):
        self.mint(ev=self.ev())  # no alias: the dated plan names F-0001 nowhere
        self.assertEqual(self.lines, [
            f'plan-tasks: F-0001: {self.PLAN_PATH} is on the trunk but names F-0001 nowhere — '
            f'not decided, so nothing minted'])

    def test_read_ref_returning_nothing_names_the_plan(self):
        self.mint(ev=self.ev(alias='F-0001'), text='')
        self.assertEqual(self.lines,
                         [f'plan-tasks: F-0001: {self.PLAN_PATH} could not be read — nothing minted'])

    def test_no_task_heading_is_the_existing_line_unchanged(self):
        self.mint(ev=self.ev(alias='F-0001'), text='# Plan\n\nprose only\n')
        self.assertEqual(self.lines,
                         [f'plan-tasks: F-0001: {self.PLAN_PATH} has no `### Task N:` heading — '
                          f'nothing to mint'])

    def test_minted_is_the_existing_line_unchanged(self):
        self.mint(ev=self.ev(alias='F-0001'))
        self.assertEqual(self.lines,
                         [f'plan-tasks: F-0001: 2 Task(s) from {self.PLAN_PATH}: T-0001, T-0002'])

    def test_a_plan_not_on_the_trunk_is_silent(self):
        self.mint(ev=self.ev(on_main=False))
        self.assertEqual(self.lines, [])

    def test_a_feature_with_a_task_child_is_silent(self):
        write_item(self.root, 'T-0099', 'task', 'already here', parent='F-0001')
        self.mint(ev=self.ev(alias='F-0001'))
        self.assertEqual(self.lines, [])

    def test_a_resolved_feature_is_silent(self):
        path = os.path.join(self.root, 'features', 'F-0001.md')
        with open(path, encoding='utf-8') as fh:
            content = fh.read()
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content.replace('state: New', 'state: Resolved'))
        self.mint(ev=self.ev(alias='F-0001'))
        self.assertEqual(self.lines, [])


PLAN2 = """# Plan F-0002

### Task 1: the second reader
stories: S-0001
writes: asf/record/reader2.py

### Task 2: the second table
stories: S-0001
writes: asf/views/table2.py
"""


class BatchedMint(unittest.TestCase):
    """D14: ``_mint`` collects the refs of every Feature about to mint, then reads them in one
    ``evidence.read_refs`` — the same cards mint, in the same order, as reading each ref where it
    was needed. The injected ``read_ref`` (the tests' seam) stays, and mints identically."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='batched_mint_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        origin = os.path.join(self.tmp, 'origin.git')
        work = os.path.join(self.tmp, 'work')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', origin], check=True,
                       capture_output=True, env=GIT_ENV)
        subprocess.run(['git', 'clone', '-q', origin, work], check=True, capture_output=True,
                       env=GIT_ENV)
        for path, text in (('docs/plans/f-0001.md', PLAN), ('docs/plans/f-0002.md', PLAN2)):
            full = os.path.join(work, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w', encoding='utf-8') as f:
                f.write(text)
        subprocess.run(['git', '-C', work, 'add', '.'], check=True, capture_output=True,
                       env=GIT_ENV)
        subprocess.run(['git', '-C', work, 'commit', '-q', '-m', 'plans'], check=True,
                       capture_output=True, env=GIT_ENV)
        subprocess.run(['git', '-C', work, 'push', '-q', 'origin', 'main'], check=True,
                       capture_output=True, env=GIT_ENV)
        repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'clone', '-q', origin, repo], check=True, capture_output=True,
                       env=GIT_ENV)
        self.product = env.Product('sample', {'repo_dir': repo, 'main': 'main'})

    def fixture(self):
        root = tempfile.mkdtemp(prefix='batched_mint_root_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(root, f))
        write_item(root, 'E-0001', 'epic', 'Factory')
        write_item(root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(root, 'F-0002', 'feature', 'The second reader', parent='E-0001')
        write_item(root, 'S-0001', 'story', 'Read it', parent='F-0001')
        return root

    def ev(self):
        return {'features': {
            'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md', 'plan_on_main': True,
                       'spec_on_main': True},
            'f-0002': {'plan': 'origin/main:docs/plans/f-0002.md', 'plan_on_main': True,
                       'spec_on_main': True}}}

    def test_one_read_refs_mints_the_same_cards_in_the_same_order(self):
        root = self.fixture()
        real = subprocess.run
        # satisfied_on_trunk (F-0106) and the id-claim check are their own, unrelated readers —
        # not what this case measures, so both are held fixed while `_batch` is counted
        with mock.patch.object(plan_tasks.trunk_check, 'satisfied_on_trunk', return_value=None), \
                mock.patch.object(plan_tasks, '_claim_view', return_value=[]), \
                mock.patch.object(evidence.subprocess, 'run', side_effect=real) as run:
            made = plan_tasks.mint_plan_tasks(root, self.product, self.ev(), out=lambda *_: None)
        self.assertEqual(made, ['T-0001', 'T-0002', 'T-0003', 'T-0004'])
        self.assertEqual(read(root, 'task', 'T-0001')[0]['writes'], ['asf/record/reader.py',
                                                                      'tests/test_reader.py'])
        self.assertEqual(read(root, 'task', 'T-0003')[0]['writes'], ['asf/record/reader2.py'])
        self.assertEqual(run.call_count, 2)  # one read_refs round-trip for both Features' plans

    def test_the_injected_reader_mints_identically(self):
        root = self.fixture()
        texts = {'origin/main:docs/plans/f-0001.md': PLAN,
                 'origin/main:docs/plans/f-0002.md': PLAN2}
        made = plan_tasks.mint_plan_tasks(root, self.product, self.ev(), out=lambda *_: None,
                                          read_ref=lambda ref: texts[ref])
        self.assertEqual(made, ['T-0001', 'T-0002', 'T-0003', 'T-0004'])
        self.assertEqual(read(root, 'task', 'T-0001')[0]['writes'], ['asf/record/reader.py',
                                                                      'tests/test_reader.py'])
        self.assertEqual(read(root, 'task', 'T-0003')[0]['writes'], ['asf/record/reader2.py'])


if __name__ == '__main__':
    unittest.main()
