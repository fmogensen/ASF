"""Edge cases of :func:`asf.kernel.decide.decide` beyond the 18 scenarios: rank order, the launch
limit, one-at-a-time on overlapping writes, the fix-round cap, the conflict path, a paused tick
that still lands, and the Stories parser's corners."""
import unittest

from asf.kernel import actions as A
from asf.kernel.decide import NEXT_ACTION, decide
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

    REQUIRED = ('tests (3.12)', 'tests (3.13)')

    def _landing(self, checks, **kw):
        return B.facts([B.task('T-0001', state=kw.pop('state', State.LANDING), **kw)],
                       prs=[B.pr(4, 'T-0001', files=['src/a.py'], checks=checks)],
                       reviews=[B.review('T-0001')])

    def test_a_red_on_a_check_that_is_not_required_does_not_block_landing(self):
        checks = [B.check('tests (3.12)'), B.check('tests (3.13)'),
                  B.check('install-clean-linux', conclusion='failure', run_id=9, attempt=2,
                          failing_files=['tests/test_other.py'])]
        plan = decide(self._landing(checks), B.config(required_checks=self.REQUIRED))
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [4])
        self.assertEqual((B.of(plan, A.Rerun), B.of(plan, A.MarkStuck), B.launched(plan)),
                         ([], [], []))
        note, = B.of(plan, A.NoteItem)
        self.assertIn('install-clean-linux', note.text)
        self.assertIn('not a required check', note.text)

    def test_with_no_required_checks_named_every_red_counts(self):
        checks = [B.check('install-clean-linux', conclusion='failure', run_id=9,
                          failing_files=['tests/test_other.py'])]
        plan = decide(self._landing(checks), B.config())
        self.assertEqual([r.run_id for r in B.of(plan, A.Rerun)], [9])

    def test_a_required_red_off_the_pr_still_reruns_then_sticks_on_ci(self):
        def world(attempt):
            return self._landing([B.check('tests (3.12)', conclusion='failure', run_id=9,
                                          attempt=attempt, failing_files=['tests/test_b.py'])])
        cfg = B.config(required_checks=self.REQUIRED)
        self.assertEqual([r.run_id for r in B.of(decide(world(1), cfg), A.Rerun)], [9])
        plan = decide(world(2), cfg)
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'ci')

    def test_a_required_red_on_the_prs_files_is_a_fix_round(self):
        red = B.check('tests (3.13)', conclusion='failure', run_id=9, failing_files=['src/a.py'])
        plan = decide(self._landing([red]), B.config(required_checks=self.REQUIRED))
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        launch, = B.of(plan, A.Launch)
        self.assertTrue(launch.findings[0].startswith('red: tests (3.13)'))

    def test_a_required_red_with_no_known_files_is_a_fix_round_with_step_and_log_tail(self):
        red = B.check('tests (3.12)', conclusion='failure', run_id=9, attempt=1,
                      failed_step='no new raw call site (the client ratchet)',
                      log_tail='asf/x.py:3: raw call')
        plan = decide(self._landing([red]), B.config(required_checks=self.REQUIRED))
        self.assertEqual(B.of(plan, A.Rerun), [])
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        launch, = B.of(plan, A.Launch)
        self.assertEqual(launch.branch, 'worker/T-0001')
        finding, = launch.findings
        self.assertIn('the client ratchet', finding)
        self.assertIn('asf/x.py:3: raw call', finding)

    def test_stuck_ci_on_a_red_that_is_not_required_is_rejudged_to_landing(self):
        stuck = B.M.Stuck('red off the PR after 1 rerun(s): install-clean-linux', 'ci')
        checks = [B.check('tests (3.12)'),
                  B.check('install-clean-linux', conclusion='failure', run_id=9, attempt=2,
                          failing_files=['tests/test_other.py'])]
        plan = decide(self._landing(checks, state=State.STUCK, stuck=stuck),
                      B.config(required_checks=self.REQUIRED))
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [4])

    def test_stuck_ci_on_a_required_red_with_no_known_files_is_rejudged_to_a_fix_round(self):
        stuck = B.M.Stuck('red off the PR after 2 rerun(s): tests (3.12)', 'ci')
        red = B.check('tests (3.12)', conclusion='failure', run_id=9, attempt=3,
                      failed_step='ratchet', log_tail='boom')
        plan = decide(self._landing([red], state=State.STUCK, stuck=stuck),
                      B.config(required_checks=self.REQUIRED))
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan), ['T-0001'])

    def test_stuck_ci_on_a_required_red_off_the_pr_stays_stuck(self):
        reason = 'red off the PR after 1 rerun(s): tests (3.12)'
        red = B.check('tests (3.12)', conclusion='failure', run_id=9, attempt=2,
                      failing_files=['tests/test_b.py'])
        plan = decide(self._landing([red], state=State.STUCK, stuck=B.M.Stuck(reason, 'ci', NEXT_ACTION['ci'])),
                      B.config(required_checks=self.REQUIRED))
        self.assertEqual((B.state(plan, 'T-0001'), B.stuck(plan, 'T-0001').reason),
                         (State.STUCK, reason))
        self.assertEqual(B.of(plan, A.MarkStuck), [])

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


class MergeTrain(unittest.TestCase):
    """At most ``update_parallel`` behind Landing PRs are updated at once, by rank then oldest."""

    def world(self, n=13, extra=(), extra_prs=()):
        # T-0101..T-0113, ranks descending so rank order differs from PR/id order
        items = [B.task('T-%04d' % (100 + i), state=State.LANDING, rank=n + 1 - i)
                 for i in range(1, n + 1)] + list(extra)
        prs = [B.pr(i, 'T-%04d' % (100 + i), behind=True, auto_merge=True)
               for i in range(1, n + 1)] + list(extra_prs)
        return B.facts(items, prs=prs, reviews=[B.review(i.id) for i in items])

    def test_thirteen_behind_with_two_places_update_the_two_best_ranked(self):
        from asf.kernel.decide import TRAIN_NOTE
        plan = decide(self.world(), B.config(update_parallel=2))
        self.assertEqual([u.pr for u in B.of(plan, A.UpdateBranch)], [13, 12])
        self.assertTrue(all(B.state(plan, 'T-%04d' % (100 + i)) is State.LANDING
                            for i in range(1, 14)))
        self.assertEqual(len(plan.notes), 11)
        self.assertEqual(plan.notes['T-0111'], [TRAIN_NOTE % (3, 13)])
        self.assertNotIn('T-0113', plan.notes)
        self.assertEqual(B.of(plan, A.NoteItem), [])

    def test_ties_on_rank_go_to_the_oldest_pr(self):
        items = [B.task('T-0001', state=State.LANDING, rank=1),
                 B.task('T-0002', state=State.LANDING, rank=1),
                 B.task('T-0003', state=State.LANDING, rank=1)]
        prs = [B.pr(9, 'T-0001', behind=True), B.pr(5, 'T-0002', behind=True),
               B.pr(7, 'T-0003', behind=True)]
        plan = decide(B.facts(items, prs=prs, reviews=[B.review(i.id) for i in items]),
                      B.config(update_parallel=2))
        self.assertEqual([u.pr for u in B.of(plan, A.UpdateBranch)], [5, 7])

    def test_one_update_already_running_its_checks_leaves_one_place(self):
        running = B.task('T-0200', state=State.LANDING, rank=50)
        pr = B.pr(20, 'T-0200', auto_merge=True,
                  checks=[B.check('tests', status='in_progress')])
        plan = decide(self.world(extra=[running], extra_prs=[pr]), B.config(update_parallel=2))
        self.assertEqual([u.pr for u in B.of(plan, A.UpdateBranch)], [13])

    def test_a_pending_check_that_is_not_required_holds_no_place(self):
        running = B.task('T-0200', state=State.LANDING, rank=50)
        pr = B.pr(20, 'T-0200', checks=[B.check('lint', status='queued'), B.check('tests')])
        plan = decide(self.world(extra=[running], extra_prs=[pr]),
                      B.config(update_parallel=2, required_checks=('tests',)))
        self.assertEqual([u.pr for u in B.of(plan, A.UpdateBranch)], [13, 12])

    def test_a_full_train_updates_nothing_and_conflicts_keep_their_path(self):
        items = [B.task('T-0001', state=State.LANDING), B.task('T-0002', state=State.LANDING),
                 B.task('T-0003', state=State.LANDING), B.task('T-0004', state=State.LANDING)]
        prs = [B.pr(1, 'T-0001', checks=[B.check(status='queued')]),
               B.pr(2, 'T-0002', checks=[B.check(status='in_progress')]),
               B.pr(3, 'T-0003', behind=True),
               B.pr(4, 'T-0004', behind=True, conflicting=True)]
        plan = decide(B.facts(items, prs=prs, reviews=[B.review(i.id) for i in items]),
                      B.config(update_parallel=2))
        self.assertEqual([u.pr for u in B.of(plan, A.UpdateBranch)], [4])
        self.assertEqual(list(plan.notes), ['T-0003'])

    def test_a_pr_not_behind_needs_no_update(self):
        items = [B.task('T-0001', state=State.LANDING)]
        plan = decide(B.facts(items, prs=[B.pr(1, 'T-0001')], reviews=[B.review('T-0001')]),
                      B.config(update_parallel=2))
        self.assertEqual(B.of(plan, A.UpdateBranch), [])
        self.assertEqual(plan.notes, {})


class ChangeKeyedVerdicts(unittest.TestCase):
    """A verdict holds on the head tree or on the PR's own change: GitHub's "update branch"
    (trunk merged in) moves the tree and keeps the change, so an approval survives it."""

    def plan(self, tree, change, review):
        f = B.facts([B.task('T-0001', state=State.LANDING)],
                    prs=[B.pr(4, 'T-0001', tree=tree, change_id=change, auto_merge=True)],
                    reviews=[review])
        return decide(f, B.config())

    def test_approved_then_updated_with_trunk_stays_landing(self):
        plan = self.plan('tree-2', 'change-1', B.review('T-0001', tree='tree-1', change='change-1'))
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        self.assertEqual(B.launched(plan, 'review'), [])

    def test_approved_then_a_new_commit_goes_back_to_review(self):
        plan = self.plan('tree-2', 'change-2', B.review('T-0001', tree='tree-1', change='change-1'))
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
        self.assertEqual(B.launched(plan, 'review'), ['T-0001'])

    def test_an_old_ledger_row_still_matches_on_its_tree(self):
        plan = self.plan('tree-1', 'change-1', B.review('T-0001', tree='tree-1'))
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        # and two unread changes are no match: an old row on another tree needs a review
        plan = self.plan('tree-2', '', B.review('T-0001', tree='tree-1'))
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)


class Stories(unittest.TestCase):

    def test_no_stories_section_is_empty(self):
        self.assertEqual(declared_stories('# F-0001\n\n- S-0001: not in a section\n'), {})

    def test_section_ends_at_a_heading_of_the_same_level(self):
        spec = '## Stories\n- S-0001: one\n  - a\n## Later\n- S-0002: two\n'
        self.assertEqual(declared_stories(spec), {'S-0001': {'title': 'one', 'acceptance': ['a']}})

    def test_sibling_bullet_ends_a_bullet_story(self):
        spec = '## Stories\n- S-0001: one\n  - a\n- a stray note\n  - not acceptance\n'
        self.assertEqual(declared_stories(spec)['S-0001']['acceptance'], ['a'])


class OnlyFeaturesGetDocuments(unittest.TestCase):

    def test_a_story_with_no_tasks_gets_no_launch(self):
        items = [B.item('F-0001', rank=1), B.task('T-0001', parent='F-0001', state=State.DONE),
                 B.item('S-0005', parent='F-0001'), B.item('S-0006', rank=2)]
        plan = decide(B.facts(items, specs_landed={'F-0001': ''}), B.config())
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.state(plan, 'S-0005'), State.NEW)
        self.assertEqual(B.state(plan, 'S-0006'), State.NEW)

    def test_a_childless_epic_gets_no_launch_and_a_feature_still_does(self):
        items = [B.item('E-0001', rank=1), B.item('F-0002', rank=2)]
        plan = decide(B.facts(items), B.config())
        self.assertEqual([(a.kind, a.item_id) for a in B.of(plan, A.Launch)], [('spec', 'F-0002')])


class RankMode(unittest.TestCase):

    def test_own_reads_only_the_items_own_rank(self):
        items = [B.item('F-0001', rank=1), B.task('T-0001', rank=None, parent='F-0001'),
                 B.task('T-0002', rank=5)]
        self.assertEqual(B.launched(decide(B.facts(items), B.config()), 'build'),
                         ['T-0001', 'T-0002'])
        self.assertEqual(B.launched(decide(B.facts(items), B.config(rank='own')), 'build'),
                         ['T-0002', 'T-0001'])

    def test_own_keeps_an_unranked_feature_new(self):
        items = [B.item('E-0001', rank=1), B.item('F-0001', parent='E-0001')]
        self.assertEqual(B.launched(decide(B.facts(items), B.config())), ['F-0001'])
        plan = decide(B.facts(items), B.config(rank='own'))
        self.assertEqual((B.launched(plan), B.state(plan, 'F-0001')), ([], State.NEW))


class IdleAlarm(unittest.TestCase):

    def test_free_seats_waiting_work_and_no_launch_raise_it_with_reasons(self):
        items = [B.task('T-0001', state=State.BUILDING, writes=['src/a.py']),
                 B.task('T-0002', writes=['src/a.py']), B.task('T-0003', writes=['src/*']),
                 B.task('T-0004', after=['T-0001'])]
        f = B.facts(items, sessions=[B.session('j1', 'T-0001')])
        plan = decide(f, B.config(max_sessions=4))
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(plan.idle, {'free': 3, 'waiting': 3,
                                     'reasons': [('file overlap', 2), ('waits on after:', 1)]})

    def test_a_launch_or_full_seats_or_the_switch_keep_it_down(self):
        self.assertIsNone(decide(B.facts([B.task('T-0001')]), B.config()).idle)
        items = [B.task('T-0001', state=State.BUILDING), B.task('T-0002', after=['T-0001'])]
        f = B.facts(items, sessions=[B.session('j1', 'T-0001')])
        self.assertIsNone(decide(f, B.config(max_sessions=1)).idle)
        self.assertIsNone(decide(f, B.config(idle_alarm=False)).idle)
        self.assertIsNone(decide(f, B.config(max_sessions=3, idle_min_free=3)).idle)
        self.assertEqual(decide(f, B.config(max_sessions=3, idle_min_free=2)).idle['free'], 2)

    def test_paused_says_so(self):
        f = B.facts([B.task('T-0001')], paused=True)
        self.assertEqual(decide(f, B.config()).idle['reasons'], [('launches paused', 1)])


if __name__ == '__main__':
    unittest.main()
