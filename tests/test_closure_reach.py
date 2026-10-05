"""Closure reaches every Task under a Feature — the parent chain or ``stories:``, at any depth.

A Feature derived Resolved while acceptance lines were unproved: its unbuilt Tasks hung under a
Story (``parent: S-xxxx``), or under a Task under one, and the closing rules never counted them
(a product's F-1139 and F-0106, 2026-10-05). Every Task whose parent chain or ``stories:``
reaches a Story counts among that Story's Tasks, and every Task and Story whose chain reaches a
Feature counts among the Feature's children.
"""
import unittest

from tests.test_no_test_no_done import EMPTY_EV, RecordCase, landed, proved, story_body

DONE = ('Resolved', 'Closed')


def matched(fid, *tasks):
    """The evidence of a Feature whose spec and plan are on the trunk, ``tasks`` landed green."""
    return dict(EMPTY_EV, ci=True, ids=landed(*tasks), features={fid.lower(): {
        'alias': None, 'spec': f'origin/main:docs/specs/{fid.lower()}.md', 'spec_branch': None,
        'spec_on_main': True, 'spec_review': None,
        'plan': f'origin/main:docs/plans/{fid.lower()}.md', 'plan_branch': None,
        'plan_on_main': True, 'plan_review': None, 'tasks': {}, 'prs': []}})


class StoryCountsTasksFiledUnderIt(RecordCase):
    def test_an_unbuilt_task_under_the_story_keeps_it_open(self):
        self.write('E-0001', 'epic')
        f = self.write('F-1139', 'feature', parent='E-0001')
        self.write('T-0001', 'task', parent='F-1139', typed=('stories: [S-0204]',))
        self.write('T-0002', 'task', parent='S-0204', typed=('decided: true',))
        s = self.write('S-0204', 'story', parent='F-1139',
                       body=story_body([(True, 'it works')], (proved(1, 'T-0001'),)))
        self.ingest(matched('F-1139', 'T-0001'))
        self.assertEqual(self.state('tasks/T-0002.md'), 'New')
        self.assertNotIn(self.state(s), DONE)
        self.assertNotIn(self.state(f), DONE)


class FeatureCountsTasksAtAnyDepth(RecordCase):
    def tree(self):
        self.write('E-0001', 'epic')
        f = self.write('F-1139', 'feature', parent='E-0001')
        self.write('S-0204', 'story', parent='F-1139',
                   body=story_body([(True, 'it works')], (proved(1, 'T-0001'),)))
        self.write('T-0001', 'task', parent='S-0204', typed=('stories: [S-0204]',))
        # a Task filed under a Task under the Story: unbuilt, and the Feature's work
        self.write('T-0002', 'task', parent='T-0001', typed=('decided: true',))
        return f

    def test_a_task_under_a_task_under_a_story_keeps_the_feature_open(self):
        f = self.tree()
        self.ingest(matched('F-1139', 'T-0001'))
        self.assertEqual(self.state('tasks/T-0002.md'), 'New')
        self.assertNotIn(self.state('stories/S-0204.md'), DONE)
        self.assertNotIn(self.state(f), DONE)

    def test_the_unmatched_feature_counts_it_too(self):
        f = self.tree()
        self.ingest(dict(EMPTY_EV, ci=True, ids=landed('T-0001')))
        self.assertNotIn(self.state(f), DONE)

    def test_a_removed_task_does_not_count(self):
        f = self.tree()
        self.write('T-0002', 'task', parent='T-0001', typed=('removed: true',))
        self.ingest(matched('F-1139', 'T-0001'))
        self.assertIn(self.state(f), DONE)


class FeatureCountsNestedStories(RecordCase):
    def test_an_open_story_under_a_story_keeps_the_feature_open(self):
        self.write('E-0001', 'epic')
        f = self.write('F-0106', 'feature', parent='E-0001')
        self.write('T-0001', 'task', parent='F-0106', typed=('stories: [S-1158]',))
        self.write('S-1158', 'story', parent='F-0106',
                   body=story_body([(True, 'it works')], (proved(1, 'T-0001'),)))
        self.write('S-1159', 'story', parent='S-1158',
                   body=story_body([(False, 'the unproved line')]))
        self.ingest(matched('F-0106', 'T-0001'))
        self.assertNotIn(self.state('stories/S-1159.md'), DONE)
        self.assertNotIn(self.state(f), DONE)

    def test_a_parent_cycle_does_not_hang(self):
        self.write('E-0001', 'epic')
        self.write('F-0106', 'feature', parent='E-0001')
        self.write('T-0001', 'task', parent='T-0002')
        self.write('T-0002', 'task', parent='T-0001')
        self.ingest(matched('F-0106'))


if __name__ == '__main__':
    unittest.main()
