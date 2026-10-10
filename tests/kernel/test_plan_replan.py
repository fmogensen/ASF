"""A merged plan the record refuses to mint is never a silent wait (F-0343).

The record's minter refuses a landed plan whole for an id no claim covers, a decision not on the
register, a Story never declared, a plan with no Task heading. The kernel then:

- reads why into ``Facts.plan_refusals`` (the port keeps each refusal of its last mint);
- claims, in code, the ids the plan only cites when that is its only fault and no record item nor
  claim holds them (blocks of one on the record's origin), and mints on the same tick;
- else notes the refusal, classes the Feature's wait ``replan``, and launches a plan session on
  a fresh plan branch carrying the refusal as its finding — one fix round each, then Stuck on the
  operator; once that re-plan merges the tick mints again.
"""
import os
import shutil
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel import waits as W
from asf.kernel.decide import REPLAN, decide, replan_finding
from asf.record import idclaim
from tests.kernel import builders as B
from tests.kernel import fakes as F
from tests.kernel.test_plan_mint import SPEC, card, git

State = B.State

PLAN = """# Plan {fid}

The old wedged item was T-0999 (an example, not a card).

### Task 1: the reader
writes: src/{name}.py

**Steps**: write it
"""


class RecordWithClaims(unittest.TestCase):
    """A record repo with an origin whose id claims are on, a product repo whose origin holds
    the plans."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='plan_replan_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        git(self.tmp, 'clone', '-q', origin, self.repo)
        rorigin = os.path.join(self.tmp, 'record.git')
        self.root = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', rorigin)
        git(self.tmp, 'clone', '-q', rorigin, self.root)
        card(self.root, 'epics', 'E-0001', ['type: epic', 'title: factory'])
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'init')
        git(self.root, 'push', '-q', 'origin', 'HEAD:main')
        self.product = env.Product('sample', {
            'backlog_dir': self.root, 'repo_dir': self.repo, 'main': 'main',
            'conventions': {'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans'}})
        self.lines = []

    def feature(self, fid, plan, block=None):
        """Feature ``fid`` with ``plan`` landed on the product's origin, and its plan session's
        claimed T block (``block``: ``(lo, hi)``)."""
        card(self.root, 'features', fid, ['type: feature', 'title: %s' % fid,
                                          'parent: E-0001', 'decided: true'])
        for rel, text in (('docs/plans/%s.md' % fid.lower(), plan),
                          ('docs/specs/%s.md' % fid.lower(), SPEC)):
            os.makedirs(os.path.join(self.repo, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), 'w') as f:
                f.write(text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'plan(%s)' % fid)
        git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        if block:
            idclaim.claim(self.root, {'T': block[1] - block[0] + 1}, 'plan-%s' % fid,
                          start=block[0])

    def mint(self, claim_cited):
        rec = P.RealRecord(self.product, state_dir=os.path.join(self.tmp, 'state'))
        rec.refresh_trunk()
        made = rec.mint_plan_tasks(out=self.lines.append, claim_cited=claim_cited)
        return rec, made

    def tasks_of(self, fid):
        rec = P.RealRecord(self.product, state_dir=os.path.join(self.tmp, 'state'))
        return sorted(i for i, it in rec.items().items() if it.parent == fid
                      and it.type == 'task')

    def test_a_refused_plan_is_kept_for_the_facts(self):
        self.feature('F-0001', PLAN.format(fid='F-0001', name='a'), block=(5000, 5049))
        rec, made = self.mint(claim_cited=False)
        self.assertEqual(made, [])
        why = rec.plan_refusals()
        self.assertEqual(list(why), ['F-0001'], self.lines)
        self.assertIn('T-0999', why['F-0001'])
        self.assertIn('no claim covers', why['F-0001'])

    def test_cited_ids_nothing_holds_are_claimed_and_the_plan_mints(self):
        self.feature('F-0001', PLAN.format(fid='F-0001', name='a'), block=(5000, 5049))
        self.feature('F-0002', PLAN.format(fid='F-0002', name='b'), block=(5050, 5099))
        rec, made = self.mint(claim_cited=True)
        self.assertEqual(rec.plan_refusals(), {}, self.lines)
        self.assertEqual(len(self.tasks_of('F-0001')), 1, self.lines)
        self.assertEqual(len(self.tasks_of('F-0002')), 1, self.lines)
        self.assertEqual(sorted(made), sorted(self.tasks_of('F-0001') + self.tasks_of('F-0002')))
        idclaim.fetch(self.root)
        held = idclaim.covers(idclaim.claims(self.root), 'T-0999')
        self.assertIsNotNone(held, 'the cited id is claimed on the record origin')
        self.assertEqual((held.lo, held.hi), (999, 999), 'a block of one, at its own number')
        self.assertIn('F-0001', held.claimant)
        self.assertIn('F-0002', held.claimant, 'one claim names every Feature that cites it')
        self.assertTrue(any('claimed cited id(s) T-0999' in x for x in self.lines), self.lines)

    def test_an_id_another_claim_covers_is_not_claimed_the_plan_is_replanned(self):
        idclaim.claim(self.root, {'T': 10}, 'plan-F-0009', start=995)  # T-0995..T-1004
        self.feature('F-0001', PLAN.format(fid='F-0001', name='a'), block=(5000, 5049))
        rec, made = self.mint(claim_cited=True)
        self.assertEqual(made, [])
        self.assertIn('F-0001', rec.plan_refusals(), 'another session may mint it: no claim')
        self.assertEqual(self.tasks_of('F-0001'), [])

    def test_an_id_the_record_holds_for_another_card_is_never_claimed(self):
        plan = PLAN.format(fid='F-0001', name='a').replace(
            'The old wedged item was T-0999 (an example, not a card).',
            '### T-0042: something else\n')
        card(self.root, 'tasks', 'T-0042', ['type: task', 'title: the real one',
                                            'parent: E-0001'])
        self.feature('F-0001', plan, block=(5000, 5049))
        rec, made = self.mint(claim_cited=True)
        self.assertIn('F-0001', rec.plan_refusals(), self.lines)
        self.assertIn('already exists', rec.plan_refusals()['F-0001'])


def plan_pr(number=9, branch='plan/F-0001'):
    return B.pr(number, 'F-0001', branch=branch, merged=True, files=['docs/plans/f-0001.md'])


def spec_pr(number=7):
    return B.pr(number, 'F-0001', branch='spec/F-0001', merged=True,
                files=['docs/specs/f-0001.md'])


WHY = 'docs/plans/f-0001.md mints id(s) no claim covers: T-0999 is outside … — nothing minted'


class RefusedPlanIsReplanned(unittest.TestCase):
    """``decide`` on a Feature whose merged plan the record refused."""

    def plan(self, items=None, prs=None, sessions=(), refused=True, **kw):
        items = items or [B.item('F-0001', rank=1)]
        facts = B.facts(items, prs=prs if prs is not None else [spec_pr(), plan_pr()],
                        sessions=list(sessions), specs_landed={'F-0001': SPEC},
                        plan_refusals={'F-0001': WHY} if refused else {})
        return decide(facts, B.config(**kw))

    def test_it_launches_a_plan_session_on_a_fresh_branch_carrying_the_refusal(self):
        plan = self.plan()
        [launch] = B.of(plan, A.Launch)
        self.assertEqual((launch.kind, launch.item_id, launch.branch),
                         ('plan', 'F-0001', 'plan/F-0001-replan-1'))
        self.assertEqual(launch.findings, [replan_finding(WHY)])
        self.assertTrue(launch.findings[0].startswith(REPLAN))
        self.assertIn('T-0999', launch.findings[0])

    def test_it_is_never_silent_a_note_says_why(self):
        notes = [a.text for a in B.of(self.plan(), A.NoteItem) if a.item_id == 'F-0001']
        self.assertEqual(notes, ['plan refused by the record: %s' % WHY])

    def test_without_a_refusal_it_waits_new_for_the_mint(self):
        plan = self.plan(refused=False)
        self.assertEqual(B.launched(plan), [])
        self.assertIs(B.state(plan, 'F-0001'), State.NEW)

    def test_a_parked_feature_is_not_replanned(self):
        plan = self.plan(items=[B.item('F-0001', rank=1, priority='later')])
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.of(plan, A.NoteItem), [])

    def test_the_knob_off_holds_it_new(self):
        self.assertEqual(B.launched(self.plan(replan_refused=False)), [])

    def test_each_replan_is_a_fix_round_the_next_one_takes_the_next_branch(self):
        plan = self.plan(prs=[spec_pr(), plan_pr(), plan_pr(11, 'plan/F-0001-replan-1')])
        [launch] = B.of(plan, A.Launch)
        self.assertEqual(launch.branch, 'plan/F-0001-replan-2')

    def test_past_the_fix_round_cap_it_is_stuck_on_the_operator(self):
        prs = [spec_pr(), plan_pr(), plan_pr(11, 'plan/F-0001-replan-1'),
               plan_pr(12, 'plan/F-0001-replan-2')]
        plan = self.plan(prs=prs, max_fix_rounds=2)
        self.assertEqual(B.launched(plan), [])
        self.assertIs(B.state(plan, 'F-0001'), State.STUCK)
        stuck = B.stuck(plan, 'F-0001')
        self.assertEqual(stuck.owner, 'operator')
        self.assertTrue(stuck.reason.endswith('after 2 fix rounds'), stuck.reason)

    def test_an_answer_at_the_cap_grants_one_more(self):
        prs = [spec_pr(), plan_pr(), plan_pr(11, 'plan/F-0001-replan-1'),
               plan_pr(12, 'plan/F-0001-replan-2')]
        it = B.item('F-0001', rank=1, extra_rounds=1)
        plan = self.plan(items=[it], prs=prs, max_fix_rounds=2)
        [launch] = B.of(plan, A.Launch)
        self.assertEqual(launch.branch, 'plan/F-0001-replan-3')

    def test_a_live_replan_session_holds_it_building(self):
        s = B.session('plan-1', 'F-0001', kind='plan', branch='plan/F-0001-replan-1')
        plan = self.plan(sessions=[s])
        self.assertEqual(B.launched(plan), [])
        self.assertIs(B.state(plan, 'F-0001'), State.BUILDING)

    def test_a_replan_that_pushed_gets_its_pr(self):
        s = B.session('plan-1', 'F-0001', kind='plan', branch='plan/F-0001-replan-1',
                      alive=False, ended=True, result='pushed', status='done')
        plan = self.plan(sessions=[s])
        [opened] = B.of(plan, A.OpenPR)
        self.assertEqual(opened.branch, 'plan/F-0001-replan-1')
        self.assertIs(B.state(plan, 'F-0001'), State.REVIEW)

    def test_an_open_replan_pr_is_reviewed_like_any_document_pr(self):
        prs = [spec_pr(), plan_pr(),
               B.pr(11, 'F-0001', branch='plan/F-0001-replan-1', files=['docs/plans/f-0001.md'])]
        plan = self.plan(prs=prs)
        self.assertEqual(B.launched(plan, 'review'), ['F-0001'])

    def test_once_its_tasks_are_minted_it_follows_them(self):
        items = [B.item('F-0001', rank=1),
                 B.task('T-0001', state=State.NEW, parent='F-0001', writes=['src/a.py'])]
        plan = self.plan(items=items, refused=False,
                         prs=[spec_pr(), plan_pr(), plan_pr(11, 'plan/F-0001-replan-1')])
        self.assertEqual(B.launched(plan, 'plan'), [])
        self.assertIs(B.state(plan, 'F-0001'), State.READY)


class ReplanWaitClass(unittest.TestCase):
    """The wait ledger follows a refused Feature: its wait class is ``replan``."""

    def test_the_refused_feature_is_followed_as_replan(self):
        facts = B.facts([B.item('F-0001', rank=1)], prs=[spec_pr(), plan_pr()],
                        specs_landed={'F-0001': SPEC}, plan_refusals={'F-0001': WHY})
        config = B.config()
        now = W.current(decide(facts, config), facts, config)
        self.assertEqual(now['F-0001'][1], 'replan')
        self.assertEqual(now['F-0001'][2], WHY)

    def test_a_feature_with_nothing_refused_is_not_followed(self):
        facts = B.facts([B.item('F-0001', rank=1)], prs=[spec_pr(), plan_pr()],
                        specs_landed={'F-0001': SPEC})
        config = B.config()
        self.assertNotIn('F-0001', W.current(decide(facts, config), facts, config))


class LoopAsksForTheClaim(unittest.TestCase):
    """The tick mints with ``claim_cited`` when ``resolve_plan_ids`` is on, and reads the
    refusals into the facts it decides."""

    def run_tick(self, **cfg):
        seen = {}

        class Rec(F.FakeRecord):
            def refresh_trunk(self):
                return None

            def mint_plan_tasks(self, out=print, claim_cited=False):
                seen['claim_cited'] = claim_cited
                return []

            def plan_refusals(self):
                return {'F-0001': WHY}

        rec = Rec([B.item('F-0001', rank=1)])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ports = F.ports(record=rec)
        ports.github.prs = lambda: [spec_pr(), plan_pr()]
        loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}), ports=ports,
                  config=B.config(**cfg), state_dir=tmp, out=lambda _l: None)
        return seen, ports

    def test_claim_cited_follows_the_knob(self):
        self.assertTrue(self.run_tick()[0]['claim_cited'])
        self.assertFalse(self.run_tick(resolve_plan_ids=False)[0]['claim_cited'])

    def test_the_refusal_reaches_decide_and_launches_the_replan(self):
        _seen, ports = self.run_tick()
        self.assertIn(('plan', 'F-0001', 'plan/F-0001-replan-1'),
                      [(k, i, b) for k, i, b, _ in ports.sessions.launched])


if __name__ == '__main__':
    unittest.main()
