"""A cached view is invalidated by the kernel's own writes in the same tick: once an apply has
merged, closed, archived, reverted or updated a PR, no later action of that apply acts on the view
the plan was decided on (a review launch, an update, a rerun, a second merge), and the GitHub port
drops its per-tick view of a PR it wrote. The cross-tick cache holds content-addressed facts only
(commit -> tree, range -> change, job URL -> log), which no write can make stale."""
import unittest

from asf.kernel import actions as A
from asf.kernel import ports as P
from asf.kernel.apply import apply

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F


def _apply(actions, prs):
    items = [B.task('T-0001', state=B.State.LANDING), B.task('T-0002', state=B.State.REVIEW)]
    gh = F.FakeGitHub(prs=prs)
    sess = F.FakeSessions()
    facts = B.facts(items, prs=prs)
    plan = A.Plan(states={}, actions=actions)
    log = []
    res = apply(plan, facts, F.ports(record=F.FakeRecord(items), github=gh, sessions=sess),
                now='t', log=log.append, judged=False)
    return gh, sess, res, log


class SameTick(unittest.TestCase):

    def setUp(self):
        self.prs = [B.pr(7, 'T-0001', head='h7', checks=[B.check(run_id=70)]),
                    B.pr(8, 'T-0002', head='h8', checks=[B.check(run_id=80)])]

    def test_nothing_acts_on_a_pr_merged_earlier_in_the_same_apply(self):
        gh, sess, res, log = _apply([
            A.MergePR(7, 'h7'), A.Launch('review', 'T-0001', 'worker/T-0001'),
            A.UpdateBranch(7), A.Rerun(70), A.CancelRun(70), A.EnableAutoMerge(7),
            A.MergePR(7, 'h7'), A.ClosePR(7, 'worker/T-0001', 'T-0001', 'x'),
            # another PR is not touched by it
            A.Rerun(80), A.UpdateBranch(8)], self.prs)
        self.assertEqual(gh.calls, [('merge', 7), ('rerun', 80), ('update_branch', 8)])
        self.assertEqual(sess.launched, [])
        self.assertEqual(res.failed, [])
        self.assertTrue(any('#7 was merged earlier this tick' in line for line in log))

    def test_an_updated_or_closed_pr_is_not_merged_on_its_old_head(self):
        for first, what in ((A.UpdateBranch(7), 'updated'),
                            (A.ClosePR(7, 'worker/T-0001', 'T-0001', 'x'), 'closed')):
            gh, _, _, log = _apply([first, A.MergePR(7, 'h7')], self.prs)
            self.assertEqual(len(gh.calls), 1, what)
            self.assertTrue(any('was %s earlier this tick' % what in line for line in log), what)

    def test_a_failed_write_changes_nothing_so_the_next_action_still_runs(self):
        items = [B.task('T-0001', state=B.State.LANDING)]
        gh = F.FakeGitHub(prs=self.prs, fail=[('merge', 7)])
        facts = B.facts(items, prs=self.prs)
        plan = A.Plan(states={}, actions=[A.MergePR(7, 'h7'), A.UpdateBranch(7)])
        apply(plan, facts, F.ports(record=F.FakeRecord(items), github=gh), now='t',
              log=lambda *_: None, judged=False)
        self.assertEqual(gh.calls, [('update_branch', 7)])


class PortView(unittest.TestCase):

    def test_a_write_drops_the_ports_view_of_that_pr(self):
        for write in (lambda p: p.merge(7, 'h7'), lambda p: p.update_branch(7),
                      lambda p: p.close_pr(7, 'c')):
            port = P.RealGitHub.__new__(P.RealGitHub)
            port.slug, port.open_heads = 'o/r', {7: 'h7', 8: 'h8'}
            port._write = lambda *a, **k: None
            port.product = type('Pr', (), {'conventions': {}})()
            write(port)
            self.assertEqual(port.open_heads, {8: 'h8'})

    def test_the_cross_tick_cache_is_keyed_by_content_only(self):
        self.assertEqual(P.GhCache.SECTIONS, ('trees', 'changes', 'jobs'))
