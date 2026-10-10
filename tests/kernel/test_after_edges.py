"""An ``after:`` edge holds its item until the dependency is Done (its change merged) or retired —
never while the dependency is Building, in Review, Landing, Stuck, or in LIMBO (an unreadable
card) — and a session that stops "blocked on X" (X an item or a PR not yet merged) puts its item
back to New waiting on X, with no operator involved."""
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel.decide import decide

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.kernel…` does not
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
NOW = '2026-10-10T12:00:00Z'


def cfg(**kw):
    kw.setdefault('escalate_after_h', 0)
    kw.setdefault('rebuild_after_h', 0)
    return B.config(**kw)


def facts(items, **kw):
    kw.setdefault('now', NOW)
    return B.facts(items, **kw)


def ended(job, item_id, status='blocked', question='', **kw):
    kw.setdefault('fields', {'status': status})
    return B.session(job, item_id, alive=False, ended=True, status=status, question=question,
                     result='question' if question else 'report', **kw)


class EdgeSatisfiedOnlyWhenMerged(unittest.TestCase):

    def waiter(self, **kw):
        return B.task('T-0002', rank=1, after=['T-0001'], **kw)

    def test_a_dependency_short_of_done_holds_the_waiter(self):
        for st in (State.BUILDING, State.REVIEW, State.LANDING, State.STUCK, State.NEW):
            dep = B.task('T-0001', state=st, rank=9, priority=None,
                         stuck=B.M.Stuck('x', 'operator') if st is State.STUCK else None)
            prs = [B.pr(7, 'T-0001')] if st in (State.REVIEW, State.LANDING) else []
            plan = decide(facts([dep, self.waiter()], prs=prs), cfg(max_sessions=0))
            self.assertEqual(B.state(plan, 'T-0002'), State.NEW, st)
            self.assertNotIn('T-0002', B.launched(decide(
                facts([dep, self.waiter()], prs=prs), cfg(max_sessions=5))), st)

    def test_a_dependency_in_limbo_holds_the_waiter(self):
        # the T-82991 defect: T-82986's card was unreadable, so it was not in ``items`` and the
        # edge to it read as satisfied — T-82991 launched before T-82986 merged
        f = facts([self.waiter()], unreadable={'T-0001': 'tasks/T-0001.md: bad'})
        plan = decide(f, cfg(max_sessions=5))
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.state(plan, 'T-0002'), State.NEW)

    def test_a_dependency_not_on_the_record_holds_the_waiter_and_says_so(self):
        plan = decide(facts([self.waiter()]), cfg(max_sessions=5))
        self.assertEqual(B.launched(plan), [])
        self.assertIn('T-0001', plan.limbo.get('T-0002', ''))

    def test_a_merged_or_retired_dependency_releases_it(self):
        merged = B.task('T-0001', state=State.REVIEW)
        plan = decide(facts([merged, self.waiter()], prs=[B.pr(7, 'T-0001', merged=True)]),
                      cfg(max_sessions=5))
        self.assertIn('T-0002', B.launched(plan))
        retired = B.task('T-0001', state=State.DONE, priority='later')
        self.assertIn('T-0002', B.launched(decide(facts([retired, self.waiter()]),
                                                  cfg(max_sessions=5))))

    def test_the_idle_reason_names_the_dependency(self):
        dep = B.task('T-0001', state=State.REVIEW)
        plan = decide(facts([dep, self.waiter()], prs=[B.pr(7, 'T-0001')],
                            reviews=[B.review('T-0001')]), cfg(max_sessions=5, idle_min_free=1))
        reasons = dict((plan.idle or {}).get('reasons') or [])
        self.assertEqual(reasons.get('waits on T-0001'), 1, plan.idle)


class BlockedOnUnmergedWork(unittest.TestCase):

    def test_a_blocked_report_naming_an_unmerged_item_waits_on_it(self):
        dep = B.task('T-0001', state=State.REVIEW)
        it = B.task('T-0002', state=State.BUILDING)
        s = ended('j2', 'T-0002', 'partial', question='the red check cannot clear until T-0001 '
                  '(PR #7) lands')
        plan = decide(facts([dep, it], prs=[B.pr(7, 'T-0001')], sessions=[s]), cfg())
        self.assertEqual(B.of(plan, A.WaitOn), [A.WaitOn('T-0002', ['T-0001'])])
        self.assertEqual(B.state(plan, 'T-0002'), State.NEW)
        for cls in (A.MarkStuck, A.FileInbox, A.ClearStuck, A.Launch):
            self.assertEqual([a for a in B.of(plan, cls) if a.item_id == 'T-0002'], [], cls)

    def test_a_pr_number_names_its_item(self):
        dep = B.task('T-0001', state=State.REVIEW)
        it = B.task('T-0002', state=State.BUILDING)
        s = ended('j2', 'T-0002', 'blocked', question='waits for PR #7 to merge')
        plan = decide(facts([dep, it], prs=[B.pr(7, 'T-0001')], sessions=[s]), cfg())
        self.assertEqual(B.of(plan, A.WaitOn), [A.WaitOn('T-0002', ['T-0001'])])

    def test_a_recorded_stuck_on_its_own_open_edge_clears(self):
        # the T-82991 shape: the report names "Task 1", not an id — its after: edge is the blocker
        dep = B.task('T-0001', state=State.REVIEW)
        it = B.task('T-0002', state=State.STUCK, after=['T-0001'],
                    stuck=B.M.Stuck('blocked: NEEDS OPERATOR: Task 1 of plan docs/plans/f-1.md '
                                    'has not landed on origin/main', 'operator'))
        plan = decide(facts([dep, it], prs=[B.pr(7, 'T-0001')]), cfg())
        self.assertEqual(B.of(plan, A.WaitOn), [A.WaitOn('T-0002', ['T-0001'])])
        self.assertEqual(B.state(plan, 'T-0002'), State.NEW)
        self.assertEqual([a for a in B.of(plan, A.FileInbox) if a.item_id == 'T-0002'], [])

    def test_a_recorded_stuck_naming_unmerged_work_on_its_open_pr_clears(self):
        # the T-81129 shape: it holds an open PR and names T-79565 (PR #1222) as the blocker
        dep = B.task('T-0001', state=State.REVIEW)
        it = B.task('T-0002', state=State.STUCK, after=['T-0001'], stuck=B.M.Stuck(
            'partial: NEEDS OPERATOR: the red check cannot clear until T-0001 (PR #7) lands',
            'operator'))
        plan = decide(facts([dep, it], prs=[B.pr(7, 'T-0001'), B.pr(8, 'T-0002')]), cfg())
        self.assertEqual(B.of(plan, A.WaitOn), [A.WaitOn('T-0002', ['T-0001'])])
        self.assertEqual(B.state(plan, 'T-0002'), State.NEW)

    def test_merged_parked_or_self_or_lineage_ids_are_no_blocker(self):
        items = [B.task('T-0001', state=State.DONE), B.item('F-0001', rank=1),
                 B.task('T-0003', state=State.NEW, priority='later'),
                 B.task('T-0002', state=State.STUCK, parent='F-0001', stuck=B.M.Stuck(
                     'blocked: NEEDS OPERATOR: T-0001, T-0002, T-0003 and F-0001 first',
                     'operator'))]
        plan = decide(facts(items), cfg())
        self.assertEqual(B.of(plan, A.WaitOn), [])
        self.assertEqual(B.state(plan, 'T-0002'), State.STUCK)

    def test_a_red_or_conflict_stuck_is_not_a_report(self):
        dep = B.task('T-0001', state=State.REVIEW)
        it = B.task('T-0002', state=State.STUCK, after=['T-0001'],
                    stuck=B.M.Stuck('conflict: T-0001 holds the file', 'loop'))
        plan = decide(facts([dep, it], prs=[B.pr(7, 'T-0001')]), cfg())
        self.assertEqual(B.of(plan, A.WaitOn), [])


class AppliedToTheCard(unittest.TestCase):

    def test_the_edge_is_added_and_the_stuck_cleared_on_the_first_tick(self):
        dep = B.task('T-0001', state=State.REVIEW)
        it = B.task('T-0002', state=State.STUCK, after=['T-0009'], stuck=B.M.Stuck(
            'partial: NEEDS OPERATOR: cannot clear until T-0001 lands', 'operator'))
        rec = F.FakeRecord([dep, it, B.task('T-0009', state=State.DONE)])
        rec.fields['T-0002'].update({P.STATE: 'stuck', P.STUCK_REASON: it.stuck.reason,
                                     P.STUCK_OWNER: 'operator'})
        ports = F.ports(record=rec, github=F.FakeGitHub(prs=[B.pr(7, 'T-0001')]))
        loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}), ports=ports,
                  config=cfg(), state_dir=tempfile.mkdtemp(), out=lambda *_: None)
        f = rec.fields['T-0002']
        self.assertEqual(f[P.AFTER], ['T-0009', 'T-0001'])
        self.assertEqual(f[P.STATE], 'new')
        self.assertNotIn(P.STUCK_REASON, f)
        self.assertEqual(rec.items()['T-0002'].after, ['T-0009', 'T-0001'])


if __name__ == '__main__':
    unittest.main()
