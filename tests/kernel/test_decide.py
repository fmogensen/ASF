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

    def test_rank_order_lower_first_unranked_never(self):
        items = [B.task('T-0003', rank=3), B.task('T-0001', rank=1), B.task('T-0002', rank=2),
                 B.task('T-0004', rank=None)]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.launched(plan, 'build'), ['T-0001', 'T-0002', 'T-0003'])
        self.assertEqual(B.state(plan, 'T-0004'), State.NEW)

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

    def test_conflict_updates_once_then_sticks_on_loop(self):
        def world(attempts):
            return B.facts([B.task('T-0001', state=State.LANDING, attempts=attempts)],
                           prs=[B.pr(4, 'T-0001', conflicting=True, behind=True)],
                           reviews=[B.review('T-0001')])
        first = decide(world([]), B.config())
        self.assertEqual([u.pr for u in B.of(first, A.UpdateBranch)], [4])
        self.assertEqual(B.state(first, 'T-0001'), State.LANDING)
        second = decide(world(['conflict: update failed']), B.config())
        self.assertEqual(B.of(second, A.UpdateBranch), [])
        self.assertEqual(B.state(second, 'T-0001'), State.STUCK)
        self.assertEqual(B.stuck(second, 'T-0001').owner, 'loop')

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
