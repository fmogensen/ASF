"""B-82960: a Feature's plan starts once its spec PR is approved, not once it merged (2026-10-10).

Each document stage waited for the previous stage's PR to merge (review, CI, the merge train),
so a Feature took three serial lead times before any Task was built. With
``Config.plan_on_approve`` the plan session launches as soon as the spec PR carries an
``approve`` verdict on its current head: it reads the spec off the spec branch head, the spec PR
lands on its own (its judge never sees the plan session), the plan PR opens beside it and is
reviewed only once the spec has landed — so every plan is judged against the spec that merged,
whatever fix round moved the spec's head meanwhile. A running plan is never cancelled."""
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


def spec_pr(number=7, tree='tree-1', head='head-1', **kw):
    return B.pr(number, 'F-0001', branch='spec/F-0001', tree=tree, head=head,
                files=['docs/specs/f-0001.md'], **kw)


def plan_pr(number=9):
    return B.pr(number, 'F-0001', branch='plan/F-0001', tree='tree-9', head='head-9',
                files=['docs/plans/f-0001.md'])


def feature(**kw):
    kw.setdefault('rank', 1)
    kw.setdefault('state', State.LANDING)
    return B.item('F-0001', **kw)


def plan_of(prs, sessions=(), reviews=None, config=None, last_jobs=None, items=None):
    facts = B.facts(items or [feature()], prs=list(prs), sessions=list(sessions),
                    reviews=[B.review('F-0001')] if reviews is None else reviews,
                    last_jobs=dict(last_jobs or {}))
    return decide(facts, config or B.config(plan_on_approve=True))


def plans(plan):
    return [(a.kind, a.branch, a.spec_head) for a in B.of(plan, A.Launch) if a.kind == 'plan']


class PlanStartsOnAnApprovedSpec(unittest.TestCase):

    def test_an_approved_spec_pr_launches_the_plan_and_still_lands(self):
        plan = plan_of([spec_pr()])
        self.assertEqual(plans(plan), [('plan', 'plan/F-0001', 'head-1')])
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [7])
        self.assertEqual(B.state(plan, 'F-0001'), State.LANDING)

    def test_off_by_the_knob_the_plan_waits_for_the_merge(self):
        self.assertEqual(plans(plan_of([spec_pr()], config=B.config())), [])

    def test_no_approve_on_the_current_head_launches_nothing(self):
        plan = plan_of([spec_pr(tree='tree-2')], reviews=[B.review('F-0001', tree='tree-1')])
        self.assertEqual(plans(plan), [])
        plan = plan_of([spec_pr()], reviews=[B.review('F-0001', verdict='changes')])
        self.assertEqual(plans(plan), [])

    def test_a_parked_or_paused_feature_launches_no_plan(self):
        plan = decide(B.facts([feature()], prs=[spec_pr()], reviews=[B.review('F-0001')],
                              paused=True), B.config(plan_on_approve=True))
        self.assertEqual(plans(plan), [])

    def test_the_wip_cap_holds_the_early_plan_like_any_plan(self):
        items = [feature()] + [B.task('T-000%d' % n, state=State.REVIEW) for n in range(1, 4)]
        prs = [spec_pr()] + [B.pr(20 + n, 'T-000%d' % n) for n in range(1, 4)]
        plan = plan_of(prs, items=items, config=B.config(plan_on_approve=True, max_open_prs=2))
        self.assertEqual(plans(plan), [])

    def test_a_plan_already_tried_on_this_spec_is_not_relaunched(self):
        plan = plan_of([spec_pr()], last_jobs={'F-0001': 'plan-f-0001-1791648148'})
        self.assertEqual(plans(plan), [])

    def test_a_spec_with_no_open_pr_keeps_the_merged_path(self):
        merged = spec_pr(merged=True)
        plan = plan_of([merged], items=[feature(state=State.DONE)])
        self.assertEqual([(a.kind, a.branch, a.spec_head) for a in B.of(plan, A.Launch)],
                         [('plan', 'plan/F-0001', '')])


class TheSpecPrStaysTheFeaturesPr(unittest.TestCase):

    def test_a_live_plan_session_never_holds_the_spec_pr(self):
        live = B.session('plan-f-0001', 'F-0001', kind='plan', branch='plan/F-0001')
        plan = plan_of([spec_pr()], sessions=[live])
        self.assertEqual(plans(plan), [])
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [7])
        self.assertEqual(B.state(plan, 'F-0001'), State.LANDING)

    def test_a_running_plan_is_not_cancelled_when_the_spec_head_moves(self):
        live = B.session('plan-f-0001', 'F-0001', kind='plan', branch='plan/F-0001')
        plan = plan_of([spec_pr(tree='tree-2', head='head-2')], sessions=[live])
        self.assertEqual(B.of(plan, A.EndSession), [])
        self.assertEqual([(a.kind, a.branch) for a in B.of(plan, A.Launch)],
                         [('review', 'spec/F-0001')])

    def test_the_ended_plan_opens_its_pr_beside_the_spec_pr(self):
        ended = B.session('plan-f-0001', 'F-0001', kind='plan', branch='plan/F-0001',
                          alive=False, ended=True, status='done', result='pushed')
        plan = plan_of([spec_pr()], sessions=[ended])
        self.assertEqual([(a.item_id, a.branch) for a in B.of(plan, A.OpenPR)],
                         [('F-0001', 'plan/F-0001')])
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [7])
        self.assertEqual(plans(plan), [])

    def test_with_both_prs_open_the_plan_pr_waits_for_the_spec_to_land(self):
        plan = plan_of([spec_pr(), plan_pr()])
        self.assertEqual([(a.kind, a.branch) for a in B.of(plan, A.Launch)], [])
        self.assertEqual([a.pr for a in B.of(plan, A.EnableAutoMerge)], [7])
        self.assertEqual(B.state(plan, 'F-0001'), State.LANDING)

    def test_once_the_spec_landed_the_plan_pr_is_reviewed_against_it(self):
        plan = plan_of([spec_pr(merged=True), plan_pr()])
        self.assertEqual([(a.kind, a.branch) for a in B.of(plan, A.Launch)],
                         [('review', 'plan/F-0001')])
        self.assertEqual(B.state(plan, 'F-0001'), State.REVIEW)


class ASpecAndAPlanPrCoexist(unittest.TestCase):
    """F-0337 (2026-10-10): with the plan PR open beside the approved spec PR, the "newer open
    PR" rule put the spec PR in LIMBO. The rule compares a Feature's PRs within one lane only."""

    def test_no_limbo_for_the_spec_and_plan_pair(self):
        plan = plan_of([spec_pr(), plan_pr()])
        self.assertNotIn('PR #7', plan.limbo)
        self.assertNotIn('PR #9', plan.limbo)

    def test_the_spec_lands_first_and_the_plan_pr_is_not_merged(self):
        plan = plan_of([spec_pr(), plan_pr()])
        landing = [a.pr for a in B.of(plan, A.EnableAutoMerge)] + \
            [a.pr for a in B.of(plan, A.MergePR)]
        self.assertIn(7, landing)
        self.assertNotIn(9, landing)

    def test_two_open_prs_on_one_lane_still_show_the_older_in_limbo(self):
        plan = plan_of([spec_pr(), plan_pr(), plan_pr(number=11)])
        self.assertEqual(plan.limbo.get('PR #9'), 'F-0001 has a newer open PR #11')
        self.assertNotIn('PR #7', plan.limbo)

    def test_a_review_verdict_with_no_pr_is_keyed_to_its_own_branchs_pr(self):
        from asf.kernel import apply as AP
        facts = B.facts([feature()], prs=[plan_pr(), spec_pr()])
        s = B.session('review-f-0001', 'F-0001', kind='review', branch='spec/F-0001',
                      alive=False, ended=True)
        self.assertEqual(AP._session_pr(s, facts).number, 7)


class TheApplierAndTheBrief(unittest.TestCase):

    def tick(self, rec, gh, sess, briefer=F.brief, product=None):
        ports = F.ports(record=rec, github=gh, sessions=sess, briefer=briefer)
        return loop.tick(product or env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                         ports=ports, config=B.config(plan_on_approve=True),
                         state_dir=tempfile.mkdtemp(), out=lambda *_: None)

    def test_the_early_plan_keeps_the_spec_prs_round_count_and_state(self):
        rec = F.FakeRecord([feature(fix_rounds=1)])
        gh = F.FakeGitHub(prs=[spec_pr()], reviews=[B.review('F-0001')])
        sess = F.FakeSessions()
        self.tick(rec, gh, sess)
        self.assertEqual([(k, b) for k, _i, b, _t in sess.launched], [('plan', 'plan/F-0001')])
        fields = rec.fields.get('F-0001', {})
        self.assertNotIn(P.FIX_ROUNDS, fields)
        self.assertNotEqual(fields.get(P.STATE), State.BUILDING.value)

    def test_a_live_early_plan_does_not_hold_the_spec_prs_fix_round(self):
        rec = F.FakeRecord([feature(state=State.REVIEW)])
        live = B.session('plan-f-0001', 'F-0001', kind='plan', branch='plan/F-0001')
        gh = F.FakeGitHub(prs=[spec_pr(checks=[B.check(conclusion='failure')])],
                          reviews=[B.review('F-0001')])
        sess = F.FakeSessions(sessions=[live])
        self.tick(rec, gh, sess)
        self.assertEqual([(k, b) for k, _i, b, _t in sess.launched], [('spec', 'spec/F-0001')])

    def test_the_brief_reads_the_spec_off_the_spec_branch_head(self):
        try:
            from kernel import test_go_live as G
        except ImportError:  # pragma: no cover - import shape only
            from tests.kernel import test_go_live as G
        product = G._product()
        rec = F.FakeRecord([feature()])
        gh = F.FakeGitHub(prs=[spec_pr(head='abc123def')], reviews=[B.review('F-0001')])
        sess = F.FakeSessions()
        self.tick(rec, gh, sess, briefer=G._briefer(product), product=product)
        (kind, _iid, branch, text), = sess.launched
        self.assertEqual((kind, branch), ('plan', 'plan/F-0001'))
        self.assertIn('git show abc123def:docs/specs/f-0001.md', text)
        self.assertIn('origin/spec/F-0001', text)
        self.assertNotIn('This is a fix round', text)


if __name__ == '__main__':
    unittest.main()
