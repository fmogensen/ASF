"""B-82960: a document PR's review rounds are fix rounds (2026-10-10, F-0330 / PR #1345).

A plan (or spec) PR the review sent back for changes relaunched its document session on the PR's
branch, but the applier counted a fix round only for a ``build`` launch: the doc round wrote no
``kernel_fix_rounds`` and carried no review findings, so the Feature went review -> plan -> review
for eight rounds with no cap. A doc launch on its open PR is now a fix round like a build's: it
counts against ``max_fix_rounds``, records and carries the review's findings, and the cap stops
it. A fresh doc launch (no PR yet: the plan after the landed spec) starts its own count."""
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
SPEC = '## Stories\n- S-0001: one\n  - it works\n'


def doc_pr(branch, number=7):
    return B.pr(number, 'F-0001', branch=branch, files=['docs/plans/f-0001.md'])


class DocFixRound(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tick(self, rec, gh, sess):
        ports = F.ports(record=rec, github=gh, sessions=sess)
        return loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                         ports=ports, config=B.config(), state_dir=self.tmp,
                         out=lambda *_: None)

    def run_round(self, lane):
        rec = F.FakeRecord([B.item('F-0001', rank=1, state=State.REVIEW)])
        landed = ([B.pr(5, 'F-0001', branch='spec/F-0001', merged=True,
                        files=['docs/specs/f-0001.md'])] if lane == 'plan' else [])
        gh = F.FakeGitHub(prs=landed + [doc_pr('%s/F-0001' % lane)],
                          reviews=[B.review('F-0001', verdict='changes',
                                            findings=['PD3 cites :1864, the skip is :1878'])])
        sess = F.FakeSessions()
        self.tick(rec, gh, sess)
        return rec, sess

    def test_a_plan_prs_changes_round_counts_and_carries_the_findings(self):
        rec, sess = self.run_round('plan')
        (kind, iid, branch, brief), = sess.launched
        self.assertEqual((kind, iid, branch), ('plan', 'F-0001', 'plan/F-0001'))
        self.assertIn('PD3 cites :1864, the skip is :1878', brief)
        self.assertEqual(rec.fields['F-0001'][P.FIX_ROUNDS], 1)
        self.assertEqual(rec.fields['F-0001'][P.FINDINGS], ['PD3 cites :1864, the skip is :1878'])

    def test_a_spec_prs_changes_round_counts_the_same(self):
        rec, sess = self.run_round('spec')
        (kind, iid, branch, brief), = sess.launched
        self.assertEqual((kind, branch), ('spec', 'spec/F-0001'))
        self.assertIn('PD3 cites :1864, the skip is :1878', brief)
        self.assertEqual(rec.fields['F-0001'][P.FIX_ROUNDS], 1)


class DocFixRoundBrief(unittest.TestCase):

    def test_the_round_brief_keeps_its_kind_and_names_the_pr_and_findings(self):
        try:
            from kernel import test_go_live as G
        except ImportError:  # pragma: no cover - import shape only
            from tests.kernel import test_go_live as G
        product = G._product()
        rec = F.FakeRecord([B.item('F-0001', rank=1, state=State.REVIEW)])
        sess = F.FakeSessions()
        gh = F.FakeGitHub(prs=[doc_pr('spec/F-0001')],
                          reviews=[B.review('F-0001', verdict='changes',
                                            findings=['the Stories section lacks S-0002'])])
        loop.tick(product, ports=F.ports(record=rec, github=gh, sessions=sess,
                                         briefer=G._briefer(product)),
                  config=B.config(), state_dir=tempfile.mkdtemp(), out=lambda *_: None)
        (kind, _iid, branch, text), = sess.launched
        self.assertEqual((kind, branch), ('spec', 'spec/F-0001'))
        self.assertIn('kind: spec', text)
        self.assertIn('the Stories section lacks S-0002', text)
        self.assertIn('PR #7', text)
        self.assertIn('Change only what the findings ask', text)
        self.assertEqual(text.count('the Stories section lacks S-0002'), 1)


class DocFixRoundCap(unittest.TestCase):

    def test_a_plan_pr_past_the_cap_is_stuck_not_relaunched(self):
        items = [B.item('F-0001', rank=1, state=State.REVIEW, fix_rounds=2)]
        plan = decide(B.facts(items, prs=[doc_pr('plan/F-0001')],
                              reviews=[B.review('F-0001', verdict='changes', findings=['x'])],
                              specs_landed={'F-0001': SPEC}),
                      B.config(max_fix_rounds=2))
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.state(plan, 'F-0001'), State.STUCK)
        self.assertIn('after 2 fix rounds', B.stuck(plan, 'F-0001').reason)

    def test_a_plan_pr_with_no_verdict_on_its_head_is_reviewed_not_replanned(self):
        items = [B.item('F-0001', rank=1, state=State.BUILDING)]
        plan = decide(B.facts(items, prs=[doc_pr('plan/F-0001')],
                              reviews=[B.review('F-0001', tree='old', verdict='changes')],
                              specs_landed={'F-0001': SPEC}), B.config())
        self.assertEqual([(a.kind, a.branch) for a in B.of(plan, A.Launch)],
                         [('review', 'plan/F-0001')])
        self.assertEqual(B.state(plan, 'F-0001'), State.REVIEW)


class FreshDocLaunchStartsItsOwnCount(unittest.TestCase):

    def test_the_plan_after_a_landed_spec_does_not_inherit_the_spec_rounds(self):
        rec = F.FakeRecord([B.item('F-0001', rank=1, fix_rounds=2), B.item('S-0001',
                                                                         parent='F-0001')])
        gh = F.FakeGitHub(prs=[B.pr(5, 'F-0001', branch='spec/F-0001', merged=True,
                                    files=['docs/specs/f-0001.md'])])
        sess = F.FakeSessions()
        loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                  ports=F.ports(record=rec, github=gh, sessions=sess), config=B.config(),
                  state_dir=tempfile.mkdtemp(), out=lambda *_: None)
        self.assertEqual([(k, b) for k, _i, b, _t in sess.launched], [('plan', 'plan/F-0001')])
        self.assertEqual(rec.fields['F-0001'][P.FIX_ROUNDS], 0)


if __name__ == '__main__':
    unittest.main()
