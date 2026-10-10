"""The floor's three starvation defects (2026-10-10: Ready 0 against 18 seats while 172 items sat
in New): a Story covered by a Task's declared ``stories:`` closes with that Task; a Feature whose
spec landed and whose Stories were minted still launches its plan; a merged spec PR never closes
its Feature."""
import os
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import ports as P
from asf.kernel.decide import decide

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.kernel…` does not
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

State = B.State
SPEC = '## Stories\n- S-0001: one\n  - it works\n'


def launches(plan):
    return [(a.kind, a.item_id) for a in B.of(plan, A.Launch)]


class StoriesLine(unittest.TestCase):

    def test_a_story_its_tasks_name_on_stories_closes_with_them(self):
        items = [B.item('F-0001', rank=1), B.item('S-0001', parent='F-0001'),
                 B.item('S-0002', parent='F-0001'),
                 B.task('T-0001', parent='F-0001', state=State.DONE, stories=['S-0001']),
                 B.task('T-0002', parent='F-0001', state=State.DONE, stories=['S-0002'])]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.state(plan, 'S-0001'), State.DONE)
        self.assertEqual(B.state(plan, 'F-0001'), State.DONE)

    def test_a_story_follows_its_open_task(self):
        items = [B.item('F-0001', rank=1), B.item('S-0001', parent='F-0001'),
                 B.task('T-0001', parent='F-0001', stories=['S-0001'])]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.state(plan, 'S-0001'), State.READY)
        self.assertEqual(launches(plan), [('build', 'T-0001')])

    def test_the_stories_line_is_read_off_the_card(self):
        root, repo = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(root, 'tasks'))
        with open(os.path.join(root, 'tasks', 'T-0001.md'), 'w') as f:
            f.write('---\nid: T-0001\ntype: task\ntitle: one\nparent: F-0001\n---\n'
                    '## Description\nstories: S-0001, S-0002\nwrites: a.py\n')
        with open(os.path.join(root, 'tasks', 'T-0002.md'), 'w') as f:
            f.write('---\nid: T-0002\ntype: task\ntitle: two\nparent: F-0001\n'
                    'stories: [S-0003]\n---\n## Description\nAlso covers S-0004.\n')
        product = env.Product('sample', {'backlog_dir': root, 'repo_dir': repo})
        items = P.RealRecord(product, state_dir=tempfile.mkdtemp()).items()
        self.assertEqual(items['T-0001'].stories, ['S-0001', 'S-0002'])
        self.assertEqual(items['T-0002'].stories, ['S-0003'])


class PlanAfterMintedStories(unittest.TestCase):

    def test_a_feature_with_only_stories_launches_its_plan(self):
        items = [B.item('F-0001', rank=1), B.item('S-0001', parent='F-0001')]
        plan = decide(B.facts(items, specs_landed={'F-0001': SPEC}), B.config())
        self.assertEqual(launches(plan), [('plan', 'F-0001')])
        self.assertEqual(B.state(plan, 'F-0001'), State.READY)
        self.assertEqual(B.state(plan, 'S-0001'), State.NEW)

    def test_a_feature_with_a_task_launches_no_document(self):
        items = [B.item('F-0001', rank=1), B.item('S-0001', parent='F-0001'),
                 B.task('T-0001', parent='F-0001', state=State.DONE)]
        plan = decide(B.facts(items, specs_landed={'F-0001': SPEC}), B.config())
        self.assertEqual(launches(plan), [])

    def test_a_running_plan_is_building_and_not_launched_again(self):
        items = [B.item('F-0001', rank=1, state=State.BUILDING), B.item('S-0001', parent='F-0001')]
        plan = decide(B.facts(items, specs_landed={'F-0001': SPEC},
                              sessions=[B.session('j1', 'F-0001', kind='plan')]), B.config())
        self.assertEqual(launches(plan), [])
        self.assertEqual(B.state(plan, 'F-0001'), State.BUILDING)


class MergedSpecIsNotDone(unittest.TestCase):

    def spec_pr(self, branch='spec/F-0001'):
        return B.pr(5, 'F-0001', branch=branch, merged=True, files=['docs/specs/f-0001.md'])

    def test_a_merged_spec_launches_the_plan(self):
        for card in (State.NEW, State.DONE):  # Done: what the old rule wrote to the card
            items = [B.item('F-0001', rank=1, state=card), B.item('S-0001', parent='F-0001')]
            plan = decide(B.facts(items, prs=[self.spec_pr()]), B.config())
            self.assertEqual(launches(plan), [('plan', 'F-0001')], card)

    def test_a_merged_plan_still_closes_the_feature(self):
        items = [B.item('F-0001', rank=1)]
        plan = decide(B.facts(items, prs=[self.spec_pr(), B.pr(6, 'F-0001', branch='plan/F-0001',
                                                                merged=True)],
                              specs_landed={'F-0001': SPEC}), B.config())
        self.assertEqual(launches(plan), [])
        self.assertEqual(B.state(plan, 'F-0001'), State.DONE)


if __name__ == '__main__':
    unittest.main()
