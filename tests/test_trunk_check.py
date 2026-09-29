"""F-0106: is this Task's work already on the trunk? `asf.record.trunk_check`'s predicate (the
tests the Task's own section names, checked against `origin/main`), the mint's use of it
(`asf.record.plan_tasks`), and the typed `landed:` field that finally closes a Task through
`closing`'s `reconciled` rule (`asf.record.ingest`). `WavePreflight` (F-0106 Task 3, the wave's own
call to the same predicate) is appended to this file separately."""
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf.env import Product
from asf.record import frontmatter, ingest, plan_tasks, trunk_check
from asf.record.core import canonicalize, load_items

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from tests import gitfixture
except ImportError:  # pragma: no cover - import shape only
    import gitfixture

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks'}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}
EMPTY_EV = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
           'checked': set(), 'main_sha': None, 'merged': {}, 'branches': []}


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


def _git(cwd, *args, env_=None):
    e = dict(os.environ, **GIT_ENV)
    if env_:
        e.update(env_)
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=e).stdout.strip()


class TestIdsRead(unittest.TestCase):
    def test_a_dotted_unittest_id_and_a_pytest_id_are_both_read(self):
        text = ("`python3 -m unittest -v tests.test_empty_ends.EmptyEndsTests` and "
                "`tests/test_empty_ends.py::ParkTextTests` — and again, "
                "`tests.test_empty_ends.EmptyEndsTests` (a duplicate, dropped)")
        self.assertEqual(trunk_check.test_ids(text),
                         [('tests/test_empty_ends.py', 'EmptyEndsTests'),
                          ('tests/test_empty_ends.py', 'ParkTextTests')])
        # the pytest method form: the node is the last component
        self.assertEqual(trunk_check.test_ids('tests/test_empty_ends.py::EmptyEndsTests::test_cap'),
                         [('tests/test_empty_ends.py', 'test_cap')])
        # a bare module dotted path with no further `.Name` names nothing (C3)
        self.assertEqual(trunk_check.test_ids(
            'python3 -m unittest -v tests.test_feeder tests.test_replan'), [])
        # more ids than CAP: only the first CAP, in first-seen order
        many = ' '.join(f'tests.test_x.Klass{i}' for i in range(trunk_check.CAP + 3))
        self.assertEqual(len(trunk_check.test_ids(many)), trunk_check.CAP)

    def test_a_bare_file_path_is_not_a_test_id(self):
        self.assertEqual(trunk_check.test_ids('tests/test_feeder.py passes'), [])
        fake = types.SimpleNamespace(main='main')
        self.assertIsNone(trunk_check.satisfied_on_trunk(
            fake, 'tests/test_feeder.py passes', (), read_ref=lambda ref: '', sh=lambda cmd: ''))


class TrunkPredicate(unittest.TestCase):
    """A real trunk (`tests.gitfixture.publish`): `origin/main` carries `tests/test_empty_ends.py`
    with `EmptyEndsTests` from its first commit and `ParkTextTests` added by a second, later one
    — the T-0084 shape, with both ids on the trunk but at different commits."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='trunk_check_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        tree = os.path.join(self.tmp, 'tree')
        origin = os.path.join(self.tmp, 'origin.git')
        os.makedirs(os.path.join(tree, 'tests'))
        with open(os.path.join(tree, 'tests', 'test_empty_ends.py'), 'w', encoding='utf-8') as f:
            f.write('class EmptyEndsTests:\n    def test_cap(self):\n        pass\n')
        with mock.patch.dict(os.environ, dict(GIT_ENV, GIT_AUTHOR_DATE='1700000000 +0000',
                                              GIT_COMMITTER_DATE='1700000000 +0000')):
            gitfixture.publish(tree, origin, message='add EmptyEndsTests')
        self.first_sha = _git(tree, 'rev-parse', 'HEAD')
        with open(os.path.join(tree, 'tests', 'test_empty_ends.py'), 'a', encoding='utf-8') as f:
            f.write('\n\nclass ParkTextTests:\n    def test_park(self):\n        pass\n')
        _git(tree, 'add', '-A')
        _git(tree, 'commit', '-q', '-m', 'add ParkTextTests',
            env_={'GIT_AUTHOR_DATE': '1700000100 +0000', 'GIT_COMMITTER_DATE': '1700000100 +0000'})
        self.second_sha = _git(tree, 'rev-parse', 'HEAD')
        _git(tree, 'push', '-q', 'origin', 'main')
        self.product = Product('trunk-check-test', {'repo_dir': tree, 'main': 'main'})

    def test_a_task_whose_named_tests_are_on_the_trunk_is_satisfied(self):
        text = ('tests.test_empty_ends.EmptyEndsTests and tests.test_empty_ends.ParkTextTests')
        found = trunk_check.satisfied_on_trunk(self.product, text, ())
        self.assertIsNotNone(found)
        sha, subject, why = found
        self.assertEqual(sha, self.second_sha)  # the newer of the two completing commits
        self.assertEqual(subject, 'add ParkTextTests')
        self.assertIn('tests/test_empty_ends.py::EmptyEndsTests', why)

    def test_one_named_test_missing_from_the_trunk_is_not_satisfied(self):
        text = 'tests.test_empty_ends.EmptyEndsTests and tests.test_empty_ends.NeverThere'
        self.assertIsNone(trunk_check.satisfied_on_trunk(self.product, text, ()))

    def test_a_writes_path_the_trunk_does_not_carry_is_not_satisfied(self):
        text = 'tests.test_empty_ends.EmptyEndsTests'
        self.assertIsNone(trunk_check.satisfied_on_trunk(self.product, text, ['asf/new_module.py']))
        # a glob entry is not asked about
        self.assertIsNotNone(trunk_check.satisfied_on_trunk(self.product, text, ['asf/views/*.py']))

    def test_nothing_closes_when_git_cannot_answer(self):
        text = 'tests.test_empty_ends.EmptyEndsTests'
        blob = 'class EmptyEndsTests:\n    def test_cap(self): pass\n'
        # a defined node, but git cannot say which commit introduced it
        self.assertIsNone(trunk_check.satisfied_on_trunk(
            types.SimpleNamespace(main='main'), text, (),
            read_ref=lambda ref: blob, sh=lambda cmd: ''))
        # an unreadable ref
        self.assertIsNone(trunk_check.satisfied_on_trunk(
            types.SimpleNamespace(main='main'), text, (),
            read_ref=lambda ref: None, sh=lambda cmd: ''))


PLAN_ALREADY_ON_TRUNK = """# Plan F-0001

## Decisions
none

### Task 1: a new module
writes: asf/new_thing.py

**Steps**: write it
**Gate**: python3 -m unittest tests.test_newmod.NewThing

### Task 2: the empty ends, already there
writes: tests/test_empty_ends.py

**Steps**: nothing — it is already on the trunk
**Gate**: python3 -m unittest tests.test_empty_ends.EmptyEndsTests

coverage: 1/1 stories; uncovered: none
"""

PLAN_NOT_ON_TRUNK = """# Plan F-0001

## Decisions
none

### Task 1: the reader
writes: asf/record/reader.py, tests/test_reader.py

**Files**: asf/record/reader.py
**Steps**: write it
**Gate**: python3 -m unittest tests.test_reader

coverage: 1/1 stories; uncovered: none
"""


class MintClosesByTrunk(unittest.TestCase):
    """PD10: no fixture here may quote T-0083, T-0084 or a sha from the card's own worked
    example — a plan of this test's own naming its own Task on a trunk this test builds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mint_trunk_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, 'backlog')
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        tree = os.path.join(self.tmp, 'tree')
        origin = os.path.join(self.tmp, 'origin.git')
        os.makedirs(os.path.join(tree, 'tests'))
        with open(os.path.join(tree, 'tests', 'test_empty_ends.py'), 'w', encoding='utf-8') as f:
            f.write('class EmptyEndsTests:\n    def test_cap(self):\n        pass\n')
        with mock.patch.dict(os.environ, GIT_ENV):
            gitfixture.publish(tree, origin, message='add EmptyEndsTests')
        self.sha = _git(tree, 'rev-parse', 'HEAD')
        self.product = Product('mint-trunk-test', {'repo_dir': tree, 'main': 'main'})
        self.lines = []

    def ev(self):
        return {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                        'plan_on_main': True, 'spec_on_main': True}}}

    def mint(self, text):
        return plan_tasks.mint_plan_tasks(self.root, self.product, self.ev(),
                                          out=self.lines.append, read_ref=lambda ref: text)

    def test_a_plan_whose_task_is_already_on_main_is_minted_closed_with_the_sha(self):
        made = self.mint(PLAN_ALREADY_ON_TRUNK)
        self.assertEqual(made, ['T-0001', 'T-0002'])
        meta1, _body1 = read(self.root, 'task', 'T-0001')
        self.assertEqual(meta1['state'], 'New')
        self.assertNotIn('landed', meta1)
        meta2, _body2 = read(self.root, 'task', 'T-0002')
        self.assertEqual(meta2['state'], 'Closed')
        self.assertEqual(meta2['landed'], self.sha)
        self.assertNotIn('after', meta2)
        self.assertTrue(any(self.sha[:12] in l and 'minted Closed' in l for l in self.lines), self.lines)

    def test_a_plan_whose_task_is_not_on_main_is_minted_new_as_before(self):
        made = self.mint(PLAN_NOT_ON_TRUNK)
        self.assertEqual(made, ['T-0001'])
        meta, body = read(self.root, 'task', 'T-0001')
        self.assertEqual(meta['state'], 'New')
        self.assertNotIn('landed', meta)
        self.assertIn('created (plan F-0001)', body)


class TypedLandedCloses(unittest.TestCase):
    """C6: `ingest.derive` over a Task carrying a typed `landed:` sha and no commit."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='typed_landed_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))

    def write_task(self, sha):
        lines = ['id: T-0001', 'type: task', 'title: T-0001 item', f'landed: {sha}',
                 '# ---- machine ----', 'state: New', 'stage_since: 2026-01-01T00:00:00Z',
                 'updated: 2026-01-01T00:00:00Z']
        path = os.path.join(self.root, 'tasks', 'T-0001.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('---\n' + '\n'.join(lines) + '\n---\n## Description\n\n## History\n- 2026-01-01: created\n')

    def canonical(self):
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        return canonical

    def test_a_typed_landed_sha_closes_a_task_through_the_reconciled_rule(self):
        sha = 'a' * 40
        self.write_task(sha)
        new_state, closings = ingest.derive(self.canonical(), dict(EMPTY_EV))[:2]
        self.assertEqual(new_state['T-0001'], 'Closed')
        self.assertEqual(closings['T-0001'].rule, 'reconciled')
        self.assertIn(f'landed {sha[:9]} (typed)', closings['T-0001'].lines)

        # a red CI product: the same typed sha does not close it
        with mock.patch.object(ingest.evidence, 'ci_green_runs', return_value=[]):
            new_state2 = ingest.derive(self.canonical(), dict(EMPTY_EV, ci=True))[0]
        self.assertNotEqual(new_state2['T-0001'], 'Closed')


if __name__ == '__main__':
    unittest.main()
