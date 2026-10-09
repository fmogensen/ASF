"""The kernel's 18 scenarios (ASF 0.2), one test each, written before the kernel.

Each builds :class:`asf.kernel.model.Facts` by hand (``tests/kernel/builders.py``) and asserts on
the :class:`asf.kernel.actions.Plan` that :func:`asf.kernel.decide.decide` returns, or on
:func:`asf.kernel.stories.declared_stories`. They name the failures of the old floor: a ``later``
item holding a lane, waits nobody declared, a stateless PR, a skipped check read as red, an
approval lost to a rebase, a dead session holding its item, one bad item stalling the lane.
"""
import unittest

from asf.kernel import actions as A
from asf.kernel.decide import decide
from asf.kernel.stories import declared_stories

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.kernel…` does not
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

State = B.State

SPEC = """# F-0100 — the widget

Background. The old plan named S-0999 and S-0998; neither is ours.

## Stories

### S-0101: The widget renders
- it renders on load
- it renders after a resize

- S-0102: The widget saves
  - a save writes the file
  - a failed save says why

```markdown
### S-0103: an example heading, not a Story
- S-0104: an example bullet, not a Story
```

## Notes

See S-0997 for history.
"""


class Scenarios(unittest.TestCase):

    def test_01_landed_spec_mints_declared_stories(self):
        got = declared_stories(SPEC)
        self.assertEqual(list(got), ['S-0101', 'S-0102'])
        self.assertEqual(got['S-0101']['title'], 'The widget renders')
        self.assertEqual(got['S-0101']['acceptance'],
                         ['it renders on load', 'it renders after a resize'])
        self.assertEqual(got['S-0102']['acceptance'],
                         ['a save writes the file', 'a failed save says why'])
        f = B.facts([B.item('F-0100')], specs_landed={'F-0100': SPEC})
        minted = B.of(decide(f, B.config()), A.MintStory)
        self.assertEqual(sorted(m.story_id for m in minted), ['S-0101', 'S-0102'])
        self.assertTrue(all(m.feature_id == 'F-0100' for m in minted))

    def test_02_later_item_with_overlapping_files_blocks_nothing(self):
        later = B.task('T-0001', state=State.BUILDING, priority='later', writes=['src/**'])
        ranked = B.task('T-0002', rank=1, writes=['src/a.py'])
        f = B.facts([later, ranked], sessions=[B.session('j1', 'T-0001')])
        self.assertIn('T-0002', B.launched(decide(f, B.config()), 'build'))

    def test_03_no_wait_without_after_or_real_overlap(self):
        building = B.task('T-0001', state=State.BUILDING, writes=['src/a.py'], parent='F-0001')
        sibling = B.task('T-0002', rank=1, writes=['src/b.py'], parent='F-0001')
        done = B.task('T-0003', state=State.DONE)
        waits_on_done = B.task('T-0004', rank=2, after=['T-0003'], writes=['src/c.py'])
        overlap = B.task('T-0005', rank=3, writes=['src/a.py'])
        f = B.facts([B.item('F-0001'), building, sibling, done, waits_on_done, overlap],
                    sessions=[B.session('j1', 'T-0001')])
        plan = decide(f, B.config())
        got = B.launched(plan, 'build')
        self.assertIn('T-0002', got)
        self.assertIn('T-0004', got)
        self.assertNotIn('T-0005', got)  # real overlap with a Building item: one at a time

    def test_04_doc_branch_pr_with_code_gets_a_review(self):
        pr = B.pr(7, 'F-0001', branch='spec/F-0001', files=['docs/specs/f-0001.md', 'src/x.py'])
        f = B.facts([B.item('F-0001', state=State.REVIEW)], prs=[pr])
        self.assertEqual(B.launched(decide(f, B.config()), 'review'), ['F-0001'])

    def test_05_every_open_pr_is_in_review_or_landing(self):
        items = [B.task('T-0001', state=State.BUILDING), B.task('T-0002', state=State.NEW),
                 B.task('T-0003', state=State.READY)]
        prs = [B.pr(1, 'T-0001'), B.pr(2, 'T-0002', tree='t2'),
               B.pr(3, 'T-0003', tree='t3', behind=True)]
        f = B.facts(items, prs=prs, reviews=[B.review('T-0003', tree='t3')])
        plan = decide(f, B.config())
        for iid in ('T-0001', 'T-0002', 'T-0003'):
            self.assertIn(B.state(plan, iid), (State.REVIEW, State.LANDING), iid)
        self.assertEqual(B.state(plan, 'T-0003'), State.LANDING)

    def test_06_skipped_and_in_progress_are_not_red(self):
        checks = [B.check('lint', conclusion='skipped', run_id=1),
                  B.check('test', status='in_progress', run_id=2),
                  B.check('build', status='queued', run_id=3)]
        f = B.facts([B.task('T-0001', state=State.LANDING)], prs=[B.pr(1, 'T-0001', checks=checks)],
                    reviews=[B.review('T-0001')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.of(plan, A.Rerun), [])
        self.assertEqual(B.of(plan, A.MarkStuck), [])

    def test_07_cancelled_run_is_no_verdict(self):
        checks = [B.check('test', conclusion='cancelled', run_id=9, failing_files=['src/a.py'])]
        f = B.facts([B.task('T-0001', state=State.LANDING)], prs=[B.pr(1, 'T-0001', checks=checks)],
                    reviews=[B.review('T-0001')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual(B.of(plan, A.Rerun), [])
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertEqual(B.launched(plan), [])

    def test_08_approval_survives_a_rebase_with_the_same_tree(self):
        pr = B.pr(1, 'T-0001', head='head-2', tree='tree-1')
        f = B.facts([B.task('T-0001', state=State.LANDING)], prs=[pr],
                    reviews=[B.review('T-0001', tree='tree-1')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual(B.launched(plan, 'review'), [])
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [1])

    def test_09_dead_session_is_ended_and_frees_its_item(self):
        dead = B.task('T-0001', state=State.BUILDING, writes=['src/a.py'])
        waiting = B.task('T-0002', rank=1, writes=['src/a.py'])
        f = B.facts([dead, waiting], sessions=[B.session('j1', 'T-0001', alive=False)])
        plan = decide(f, B.config())
        ends = B.of(plan, A.EndSession)
        self.assertEqual([(e.job, e.free_worktree) for e in ends], [('j1', True)])
        self.assertNotEqual(B.state(plan, 'T-0001'), State.BUILDING)
        self.assertIn('T-0002', B.launched(plan, 'build'))

    def test_10_repeated_launch_errors_stick_the_item_not_the_lane(self):
        bad = B.task('T-0001', rank=1,
                     attempts=['launch: worktree exists', 'launch: worktree exists'])
        good = B.task('T-0002', rank=2)
        plan = decide(B.facts([bad, good]), B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'loop')
        self.assertNotIn('T-0001', B.launched(plan))
        self.assertIn('T-0002', B.launched(plan, 'build'))

    def test_11_paused_launches_nothing(self):
        items = [B.task('T-0001', rank=1), B.task('T-0002', state=State.REVIEW)]
        f = B.facts(items, prs=[B.pr(1, 'T-0002')], paused=True)
        self.assertEqual(B.of(decide(f, B.config()), A.Launch), [])

    def test_12_answer_applied_and_no_relaunch_on_the_same_finding(self):
        asked = B.task('T-0001', state=State.STUCK, question='which file format?',
                       stuck=B.M.Stuck('which file format?', 'operator'))
        plan = decide(B.facts([asked], answers=[B.answer('T-0001', 'json')]), B.config())
        self.assertEqual([(a.item_id, a.text) for a in B.of(plan, A.ApplyAnswer)],
                         [('T-0001', 'json')])
        self.assertNotEqual(B.state(plan, 'T-0001'), State.STUCK)
        # next tick: the answer is on the card and a session already holds the item
        answered = B.task('T-0001', state=State.BUILDING, answers=['json'])
        f = B.facts([answered], answers=[B.answer('T-0001', 'json')],
                    sessions=[B.session('j2', 'T-0001')])
        plan = decide(f, B.config())
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.launched(plan), [])

    def test_13_session_ended_without_push_is_stuck_on_session(self):
        s = B.session('j1', 'T-0001', alive=False, ended=True, result='none',
                      last_line='error: cannot import name widget')
        f = B.facts([B.task('T-0001', state=State.BUILDING)], sessions=[s])
        plan = decide(f, B.config())
        marks = [(m.item_id, m.owner, m.reason) for m in B.of(plan, A.MarkStuck)]
        self.assertEqual(marks, [('T-0001', 'session', 'error: cannot import name widget')])
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)

    def test_14_reopened_done_item_accepts_a_new_pr(self):
        reopened = B.task('T-0001', state=State.DONE, reopened=True)
        f = B.facts([reopened], prs=[B.pr(2, 'T-0001', tree='tree-new')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertEqual(B.launched(plan, 'review'), ['T-0001'])

    def test_15_red_off_the_pr_reruns_once_then_sticks_on_ci(self):
        def world(attempt):
            red = B.check('test', conclusion='failure', run_id=55, attempt=attempt,
                          failing_files=['tests/test_other.py'])
            return B.facts([B.task('T-0001', state=State.LANDING)],
                           prs=[B.pr(1, 'T-0001', files=['src/a.py'], checks=[red])],
                           reviews=[B.review('T-0001')])
        first = decide(world(1), B.config())
        self.assertEqual([r.run_id for r in B.of(first, A.Rerun)], [55])
        self.assertNotEqual(B.state(first, 'T-0001'), State.STUCK)
        second = decide(world(2), B.config())
        self.assertEqual(B.of(second, A.Rerun), [])
        self.assertEqual(B.state(second, 'T-0001'), State.STUCK)
        self.assertEqual(B.stuck(second, 'T-0001').owner, 'ci')

    def test_16_stuck_carries_reason_owner_and_blocked_count(self):
        root = B.task('T-0001', state=State.STUCK, stuck=B.M.Stuck('conflict', 'loop'))
        a = B.task('T-0002', rank=1, after=['T-0001'])
        b = B.task('T-0003', rank=2, after=['T-0002'])
        unrelated = B.task('T-0004', rank=3)
        hidden = B.task('T-0005', rank=4, after=['T-0001'], priority='later')
        plan = decide(B.facts([root, a, b, unrelated, hidden]), B.config())
        info = B.stuck(plan, 'T-0001')
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual((info.reason, info.owner, info.blocked_count), ('conflict', 'loop', 2))

    def test_17_story_membership_is_the_parent_link(self):
        items = [B.item('F-0001'), B.item('S-0001', parent='F-0001'),
                 B.item('S-0002', parent='F-0001'),
                 B.task('T-0001', state=State.DONE, parent='S-0002'),
                 B.task('T-0002', state=State.BUILDING, parent='S-0001',
                        body='Also covers S-0002.')]
        f = B.facts(items, sessions=[B.session('j1', 'T-0002')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'S-0002'), State.DONE)
        self.assertEqual(B.state(plan, 'S-0001'), State.BUILDING)

    def test_18_foreign_id_in_spec_prose_is_never_minted(self):
        self.assertFalse({'S-0997', 'S-0998', 'S-0999', 'S-0103', 'S-0104'}
                         & set(declared_stories(SPEC)))
        f = B.facts([B.item('F-0100'), B.item('S-0101', parent='F-0100')],
                    specs_landed={'F-0100': SPEC})
        minted = [m.story_id for m in B.of(decide(f, B.config()), A.MintStory)]
        self.assertEqual(minted, ['S-0102'])


if __name__ == '__main__':
    unittest.main()
