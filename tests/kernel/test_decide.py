"""Edge cases of :func:`asf.kernel.decide.decide` beyond the 18 scenarios: rank order, the launch
limit, one-at-a-time on overlapping writes, the fix-round cap, the conflict path, a paused tick
that still lands, and the Stories parser's corners."""
import unittest

from asf.kernel import actions as A
from asf.kernel.decide import decide
from asf.kernel.stories import declared_stories

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.kernel…` does not
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

State = B.State


class Launches(unittest.TestCase):

    def test_rank_order_lower_first_unranked_last(self):
        items = [B.task('T-0003', rank=3), B.task('T-0001', rank=1), B.task('T-0002', rank=2),
                 B.task('T-0004', rank=None)]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.launched(plan, 'build'), ['T-0001', 'T-0002', 'T-0003', 'T-0004'])
        self.assertEqual(B.state(plan, 'T-0004'), State.READY)

    def test_task_inherits_its_features_rank(self):
        items = [B.item('F-0001', rank=4), B.item('S-0001', parent='F-0001'),
                 B.task('T-0001', rank=None, parent='S-0001', state=State.NEW)]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan, 'build'), ['T-0001'])

    def test_unranked_lineage_still_launches_after_ranked_work(self):
        items = [B.item('F-0001'), B.task('T-0001', rank=None, parent='F-0001', state=State.NEW),
                 B.item('F-0002', rank=9), B.task('T-0002', rank=None, parent='F-0002'),
                 B.task('B-0001', rank=None, state=State.NEW)]
        plan = decide(B.facts(items), B.config(max_sessions=5))
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan, 'build'), ['T-0002', 'B-0001', 'T-0001'])

    def test_launch_order_follows_feature_rank(self):
        items = [B.item('F-0001', rank=5), B.item('F-0002', rank=2),
                 B.task('T-0001', rank=None, parent='F-0001'),
                 B.task('T-0003', rank=None, parent='F-0002'),
                 B.task('T-0002', rank=None, parent='F-0002'),
                 B.item('E-0001', rank=3), B.item('F-0003', parent='E-0001'),
                 B.task('T-0004', rank=None, parent='F-0003')]
        plan = decide(B.facts(items), B.config(max_sessions=9))
        self.assertEqual(B.launched(plan, 'build'), ['T-0002', 'T-0003', 'T-0004', 'T-0001'])

    def test_later_ancestor_still_parks_an_inheriting_task(self):
        items = [B.item('F-0001', rank=1, priority='later'),
                 B.task('T-0001', rank=None, parent='F-0001', state=State.NEW),
                 B.task('T-0002', rank=None, state=State.NEW, priority='later')]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.PARKED)
        self.assertEqual(B.state(plan, 'T-0002'), State.PARKED)
        self.assertEqual(B.launched(plan), [])

    def test_parent_cycle_ends_the_rank_walk(self):
        items = [B.task('T-0001', rank=None, parent='T-0002'),
                 B.task('T-0002', rank=None, parent='T-0001')]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.launched(plan, 'build'), ['T-0001', 'T-0002'])

    def test_launch_limit_counts_live_sessions(self):
        items = [B.task('T-0001', state=State.BUILDING)] + [
            B.task('T-000%d' % n, rank=n) for n in (2, 3, 4)]
        f = B.facts(items, sessions=[B.session('j1', 'T-0001')])
        self.assertEqual(B.launched(decide(f, B.config(max_sessions=3))), ['T-0002', 'T-0003'])

    def test_reviews_launch_before_builds(self):
        items = [B.task('T-0001', rank=1), B.task('T-0002', state=State.REVIEW, rank=9)]
        f = B.facts(items, prs=[B.pr(1, 'T-0002')])
        plan = decide(f, B.config(max_sessions=1))
        self.assertEqual([(a.kind, a.item_id) for a in B.of(plan, A.Launch)],
                         [('review', 'T-0002')])

    def test_overlap_one_at_a_time_within_a_tick(self):
        items = [B.task('T-0001', rank=1, writes=['src/**']),
                 B.task('T-0002', rank=2, writes=['src/a.py']),
                 B.task('T-0003', rank=3, writes=['docs/x.md'])]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.launched(plan), ['T-0001', 'T-0003'])
        self.assertEqual(B.state(plan, 'T-0002'), State.READY)

    def test_build_branch_and_doc_lane_branch_come_from_config(self):
        items = [B.task('T-0001', rank=1), B.item('F-0001', rank=2)]
        plan = decide(B.facts(items), B.config(work_branch='w/'))
        self.assertEqual([(a.kind, a.branch) for a in B.of(plan, A.Launch)],
                         [('build', 'w/T-0001'), ('spec', 'spec/F-0001')])

    def test_after_waits_only_while_the_target_is_not_done(self):
        items = [B.task('T-0001', state=State.BUILDING), B.task('T-0002', rank=1,
                                                                after=['T-0001'])]
        plan = decide(B.facts(items, sessions=[B.session('j1', 'T-0001')]), B.config())
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.state(plan, 'T-0002'), State.NEW)


class Rounds(unittest.TestCase):

    def test_changes_verdict_sends_a_fix_round_on_the_pr_branch(self):
        f = B.facts([B.task('T-0001', state=State.REVIEW)], prs=[B.pr(4, 'T-0001')],
                    reviews=[B.review('T-0001', verdict='changes', findings=['name it'])])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual([(a.kind, a.branch) for a in B.of(plan, A.Launch)],
                         [('build', 'worker/T-0001')])

    def test_fix_round_cap_sticks_on_operator(self):
        f = B.facts([B.task('T-0001', state=State.REVIEW, fix_rounds=2)],
                    prs=[B.pr(4, 'T-0001')], reviews=[B.review('T-0001', verdict='changes')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')
        self.assertEqual(B.launched(plan), [])

    def test_red_on_the_prs_files_is_a_fix_round_not_a_rerun(self):
        red = B.check('test', conclusion='timed_out', run_id=8, failing_files=['src/a.py'])
        f = B.facts([B.task('T-0001', state=State.LANDING, fix_rounds=1)],
                    prs=[B.pr(4, 'T-0001', checks=[red])], reviews=[B.review('T-0001')])
        plan = decide(f, B.config())
        self.assertEqual(B.of(plan, A.Rerun), [])
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.of(plan, A.EnableAutoMerge), [])

    def test_conflict_updates_then_gets_a_rebase_session_then_sticks_on_operator(self):
        from asf.kernel.decide import rebase_finding

        def world(attempts, findings=(), head='head-1', fix_rounds=0, **kw):
            return B.facts([B.task('T-0001', state=State.LANDING, attempts=attempts,
                                   findings=list(findings), fix_rounds=fix_rounds, **kw)],
                           prs=[B.pr(4, 'T-0001', conflicting=True, behind=True, head=head)],
                           reviews=[B.review('T-0001')])
        first = decide(world([]), B.config())
        self.assertEqual([u.pr for u in B.of(first, A.UpdateBranch)], [4])
        self.assertEqual(B.state(first, 'T-0001'), State.LANDING)
        # the update failed on the conflict: one fix round, a correct session that rebases
        second = decide(world(['conflict: PR #4: update failed']), B.config())
        self.assertEqual(B.of(second, A.UpdateBranch), [])
        self.assertEqual(B.state(second, 'T-0001'), State.READY)
        launch, = B.of(second, A.Launch)
        self.assertEqual((launch.kind, launch.branch), ('build', 'worker/T-0001'))
        finding, = launch.findings
        self.assertTrue(rebase_finding(finding, 4))
        self.assertIn('rebase the branch onto the base', finding)
        # that session ended and the PR still conflicts, on a new head or the same one
        for head in ('head-2', 'head-1'):
            third = decide(world(['conflict: PR #4: update failed'], [finding], head=head,
                                 fix_rounds=1), B.config())
            self.assertEqual(B.of(third, A.Launch), [])
            self.assertEqual(B.of(third, A.UpdateBranch), [])
            info = B.stuck(third, 'T-0001')
            self.assertEqual(info.owner, 'operator')
            self.assertTrue(info.reason.startswith('conflict the rebase session could not resolve'))
            self.assertIn('PR #4', info.next_action)

    def test_a_rebase_round_counts_against_the_fix_round_cap(self):
        f = B.facts([B.task('T-0001', state=State.LANDING, fix_rounds=2,
                            attempts=['conflict: PR #4: x'])],
                    prs=[B.pr(4, 'T-0001', conflicting=True)], reviews=[B.review('T-0001')])
        plan = decide(f, B.config(max_fix_rounds=2))
        self.assertEqual(B.of(plan, A.Launch), [])
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')

    def test_the_old_loop_stuck_on_a_conflict_gets_its_rebase_session(self):
        old = B.M.Stuck('conflict: PR #4 still conflicts after an update', 'loop')
        f = B.facts([B.task('T-0001', state=State.STUCK, stuck=old,
                            attempts=['conflict: PR #4: x'])],
                    prs=[B.pr(4, 'T-0001', conflicting=True)], reviews=[B.review('T-0001')])
        plan = decide(f, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan, 'build'), ['T-0001'])

    def test_paused_still_lands_ends_and_answers(self):
        items = [B.task('T-0001', state=State.LANDING), B.task('T-0002', rank=1),
                 B.task('T-0003', state=State.STUCK, stuck=B.M.Stuck('which?', 'operator'))]
        f = B.facts(items, prs=[B.pr(4, 'T-0001', behind=True)], reviews=[B.review('T-0001')],
                    sessions=[B.session('j9', 'T-0002', alive=False)],
                    answers=[B.answer('T-0003', 'this one')], paused=True)
        plan = decide(f, B.config())
        self.assertEqual(B.of(plan, A.Launch), [])
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [4])
        self.assertEqual([a.pr for a in B.of(plan, A.UpdateBranch)], [4])
        self.assertEqual([e.job for e in B.of(plan, A.EndSession)], ['j9'])
        self.assertEqual(len(B.of(plan, A.ApplyAnswer)), 1)
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual(B.state(plan, 'T-0002'), State.READY)

    def test_session_question_sticks_on_operator(self):
        s = B.session('j1', 'T-0001', alive=False, ended=True, result='question',
                      question='json or yaml?')
        plan = decide(B.facts([B.task('T-0001', state=State.BUILDING)], sessions=[s]),
                      B.config())
        self.assertEqual((B.stuck(plan, 'T-0001').owner, B.stuck(plan, 'T-0001').reason),
                         ('operator', 'json or yaml?'))

    def test_states_cover_every_item_and_derive_containers(self):
        items = [B.item('E-0001'), B.item('F-0001', parent='E-0001'),
                 B.task('T-0001', state=State.DONE, parent='F-0001'),
                 B.task('T-0002', state=State.STUCK, parent='F-0001',
                        stuck=B.M.Stuck('boom', 'session'))]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(set(plan.states), {'E-0001', 'F-0001', 'T-0001', 'T-0002'})
        self.assertEqual(B.state(plan, 'F-0001'), State.STUCK)
        self.assertEqual(B.state(plan, 'E-0001'), State.STUCK)


class Stories(unittest.TestCase):

    def test_no_stories_section_is_empty(self):
        self.assertEqual(declared_stories('# F-0001\n\n- S-0001: not in a section\n'), {})

    def test_section_ends_at_a_heading_of_the_same_level(self):
        spec = '## Stories\n- S-0001: one\n  - a\n## Later\n- S-0002: two\n'
        self.assertEqual(declared_stories(spec), {'S-0001': {'title': 'one', 'acceptance': ['a']}})

    def test_sibling_bullet_ends_a_bullet_story(self):
        spec = '## Stories\n- S-0001: one\n  - a\n- a stray note\n  - not acceptance\n'
        self.assertEqual(declared_stories(spec)['S-0001']['acceptance'], ['a'])


if __name__ == '__main__':
    unittest.main()
