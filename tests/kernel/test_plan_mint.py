"""A merged plan becomes Task cards on the kernel's tick, read off ``origin/<main>`` (never a
checkout's working tree that fell behind), once; and a Feature whose plan landed is not Done
until the Tasks under it are."""
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel.decide import decide
from asf.record import frontmatter
from tests.kernel import builders as B
from tests.kernel import fakes as F

State = B.State

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

PLAN = """# Plan F-0001

### Task 1: the reader
writes: src/reader.py, tests/test_reader.py

**Steps**: write it

### Task 2: the table
writes: src/table.py

**Steps**: draw it
"""

SPEC = '# F-0001 spec\n\nthe reader reads.\n'


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def card(root, folder, iid, lines):
    os.makedirs(os.path.join(root, folder), exist_ok=True)
    with open(os.path.join(root, folder, iid + '.md'), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(['id: %s' % iid] + lines + [
            '# ---- machine ----', 'state: New', 'stage_since: 2026-10-01T00:00:00Z',
            'updated: 2026-10-01T00:00:00Z']) + '\n---\n## Description\n\n## History\n')


class LandedPlanMints(unittest.TestCase):
    """The record port over a scratch record and a product repo whose checkout is behind its
    origin: the plan (and spec) that landed on origin after the checkout was cloned."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='plan_mint_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        self.other = os.path.join(self.tmp, 'other')
        self.root = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.other)
        with open(os.path.join(self.other, 'README'), 'w') as f:
            f.write('x\n')
        git(self.other, 'add', '-A')
        git(self.other, 'commit', '-q', '-m', 'init')
        git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        git(self.tmp, 'clone', '-q', self.origin, self.repo)  # the checkout, from here on behind
        for rel, text in (('docs/plans/f-0001.md', PLAN), ('docs/specs/f-0001.md', SPEC)):
            os.makedirs(os.path.join(self.other, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.other, rel), 'w') as f:
                f.write(text)
        git(self.other, 'add', '-A')
        git(self.other, 'commit', '-q', '-m', 'plan(F-0001)')
        git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        card(self.root, 'epics', 'E-0001', ['type: epic', 'title: factory'])
        card(self.root, 'features', 'F-0001', ['type: feature', 'title: the reader',
                                               'parent: E-0001', 'decided: true'])
        self.product = env.Product('sample', {
            'backlog_dir': self.root, 'repo_dir': self.repo, 'main': 'main',
            'conventions': {'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans'}})
        self.state = os.path.join(self.tmp, 'state')
        self.lines = []

    def record(self):
        return P.RealRecord(self.product, state_dir=self.state)

    def tasks(self):
        d = os.path.join(self.root, 'tasks')
        out = {}
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            with open(os.path.join(d, name), encoding='utf-8') as f:
                meta, body = frontmatter.parse(f.read(), path=name)
            out[meta['id']] = (meta, body)
        return out

    def test_the_trunk_is_fetched_and_read_off_origin_not_the_checkout(self):
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'docs')),
                         'the checkout is behind: the plan is only on origin')
        rec = self.record()
        rec.refresh_trunk()
        self.assertEqual(rec.specs_landed(), {'F-0001': SPEC})
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'docs')),
                         'the checkout working tree is never touched')

    def test_a_merged_plan_mints_its_tasks_once(self):
        rec = self.record()
        rec.refresh_trunk()
        made = rec.mint_plan_tasks(out=self.lines.append)
        tasks = self.tasks()
        self.assertEqual(sorted(made), sorted(tasks))
        self.assertEqual(len(tasks), 2, self.lines)
        writes = sorted(tuple(m['writes']) for m, _b in tasks.values())
        self.assertEqual(writes, [('src/reader.py', 'tests/test_reader.py'), ('src/table.py',)])
        for meta, body in tasks.values():
            self.assertEqual((meta['type'], meta['parent'], meta['decided']),
                             ('task', 'F-0001', True))
            self.assertEqual(meta['links']['plan'], 'docs/plans/f-0001.md')
            self.assertIn('**Steps**', body)
        self.assertEqual(self.record().mint_plan_tasks(out=self.lines.append), [],
                         'a re-run is a no-op')
        self.assertEqual(len(self.tasks()), 2)
        items = self.record().items()
        self.assertEqual(sorted(i for i, it in items.items() if it.parent == 'F-0001'),
                         sorted(tasks))

    def test_without_a_fetch_the_stale_origin_ref_mints_nothing(self):
        self.assertEqual(self.record().mint_plan_tasks(out=self.lines.append), [])
        self.record().refresh_trunk()
        self.assertEqual(len(self.record().mint_plan_tasks(out=self.lines.append)), 2)

    def test_a_dry_run_fetches_nothing(self):
        from asf import mutation_guard
        with mutation_guard.active():
            self.record().refresh_trunk()
        self.assertEqual(self.record().specs_landed(), {})


class TickMintsBeforeItReads(unittest.TestCase):
    """The loop refreshes the trunk and mints a landed plan's Tasks before it reads the facts:
    the Tasks are decided on the same tick, and the tick publishes them."""

    def test_the_minted_tasks_are_in_the_same_ticks_facts(self):
        calls = []

        class Rec(F.FakeRecord):
            def refresh_trunk(self):
                calls.append('refresh')

            def mint_plan_tasks(self, out=print):
                calls.append('mint')
                t = B.task('T-0009', state=State.READY, parent='F-0001', writes=['src/a.py'])
                self._items[t.id] = t
                self.fields[t.id] = {}
                return [t.id]

            def items(self):
                calls.append('items')
                return super().items()

        rec = Rec([B.item('F-0001', rank=1)])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lines = []
        ports = F.ports(record=rec)
        summary = loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                            ports=ports, config=B.config(), state_dir=tmp,
                            out=lines.append)
        self.assertEqual(calls[:3], ['refresh', 'mint', 'items'])
        self.assertIn(('build', 'T-0009'),
                      [(k, i) for k, i, _b, _ in ports.sessions.launched],
                      'the minted Task is decided (and launched) on the same tick')
        self.assertNotIn('locked', summary)
        self.assertTrue(any('plan-tasks: minted T-0009' in x for x in lines), lines)


def plan_pr(number=9):
    return B.pr(number, 'F-0001', branch='plan/F-0001', merged=True,
                files=['docs/plans/f-0001.md'])


def spec_pr(number=7):
    return B.pr(number, 'F-0001', branch='spec/F-0001', merged=True,
                files=['docs/specs/f-0001.md'])


class FeatureNotDoneOnItsPlan(unittest.TestCase):
    """A Feature's merged plan PR is the start of its build, never its end."""

    def state(self, items):
        facts = B.facts(items, prs=[spec_pr(), plan_pr()])
        return B.state(decide(facts, B.config()), 'F-0001')

    def test_a_landed_plan_with_no_task_under_it_is_not_done(self):
        self.assertIsNot(self.state([B.item('F-0001', rank=1, state=State.DONE)]), State.DONE)

    def test_it_is_not_relaunched_either(self):
        facts = B.facts([B.item('F-0001', rank=1, state=State.DONE)], prs=[spec_pr(), plan_pr()])
        self.assertEqual(B.launched(decide(facts, B.config())), [])

    def test_with_its_tasks_open_it_follows_them(self):
        items = [B.item('F-0001', rank=1, state=State.DONE),
                 B.task('T-0001', state=State.NEW, parent='F-0001', writes=['src/a.py'])]
        self.assertIs(self.state(items), State.READY, 'its Task starts: the Feature is not Done')

    def test_once_its_tasks_are_done_it_is_done(self):
        items = [B.item('F-0001', rank=1, state=State.DONE),
                 B.task('T-0001', state=State.DONE, parent='F-0001', writes=['src/a.py'])]
        self.assertIs(self.state(items), State.DONE)


if __name__ == '__main__':
    unittest.main()
