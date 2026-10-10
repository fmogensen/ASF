"""The host publishes a safe rebase at any session end (B-84832): a rebase session's sandbox
refuses the force-push, so it ended ``partial`` asking the operator for it and the PR went
Stuck "conflict the rebase session could not resolve".

- a session on an open PR's branch whose worktree holds a safe rebased HEAD is pushed by the host
  whatever it reported (``partial``, ``blocked``, no REPORT): no Stuck for that session, the PR
  is judged on next tick's facts;
- an unsafe history (origin holds a commit the branch never had) is Stuck(operator) on that
  reason, its worktree kept;
- a recorded Stuck that refused force-push left, whose kept worktree still holds a safe rebased
  HEAD, is re-judged: pushed, then Review;
- the rebase brief says not to force-push and to report ``pushed: rebased <sha>``.
"""
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel.decide import (CONFLICT, NOT_PUSHED, REBASE_ASK, decide, rebase_finding_for,
                               stranded)

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
HEAD = 'a0971693bdddda6afe0aca75917dea37d1670516'
ASK = ('the rebase is done and committed locally, but the sandbox refused the push — '
       'grant permission for, or run directly, the force-with-lease push')
COULD_NOT = ('%s the rebase session could not resolve: PR #7 still conflicts on head head-1'
             % CONFLICT)


def _rebase_round(**kw):
    """A task whose PR #7 conflicts, in its rebase round."""
    kw.setdefault('state', State.BUILDING)
    return B.task('T-0001', fix_rounds=1, findings=[rebase_finding_for(7)], **kw)


def _ended(status='partial', job='j1', **kw):
    fields = {'status': status, 'pushed': 'no — the sandbox refused the force-with-lease push'} \
        if status else {}
    kw.setdefault('result', 'question' if status else 'none')
    kw.setdefault('question', ASK if status else None)
    return B.session(job, 'T-0001', alive=False, ended=True, status=status, fields=fields,
                     branch='worker/T-0001', pr=7, **kw)


def _conflicting():
    return B.pr(7, 'T-0001', conflicting=True)


def _tick(sess, items, prs=()):
    rec = F.FakeRecord(items)
    gh = F.FakeGitHub(prs=list(prs))
    lines = []
    loop.tick(env.Product('sample', {}), ports=F.ports(record=rec, github=gh, sessions=sess),
              config=B.config(), state_dir=tempfile.mkdtemp(), out=lines.append)
    return rec, gh, lines


class AnySessionEnd(unittest.TestCase):

    def test_a_partial_session_with_a_safe_rebased_head_is_pushed_and_not_stuck(self):
        for s in (_ended('partial', unpushed=HEAD), _ended('blocked', unpushed=HEAD),
                  _ended('', unpushed=HEAD)):
            with self.subTest(status=s.status or 'no REPORT'):
                plan = decide(B.facts([_rebase_round()], sessions=[s], prs=[_conflicting()]),
                              B.config())
                self.assertEqual(B.of(plan, A.MarkStuck), [])
                self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
                self.assertEqual(B.launched(plan), [], 'judged on next tick\'s facts')
                sess = F.FakeSessions([s])
                rec, _, _ = _tick(sess, [_rebase_round()], prs=[_conflicting()])
                self.assertEqual(sess.pushed, [('j1', 'worker/T-0001', HEAD)])
                f = rec.fields['T-0001']
                self.assertEqual(f[P.STATE], State.REVIEW.value)
                self.assertNotIn('ended without a REPORT', f.get(P.ATTEMPTS, []))

    def test_next_tick_the_cleared_conflict_goes_on_to_review(self):
        plan = decide(B.facts([_rebase_round(state=State.REVIEW)],
                              prs=[B.pr(7, 'T-0001', head='head-2')]), B.config())
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)

    def test_an_unsafe_history_is_stuck_with_the_reason_and_keeps_its_worktree(self):
        why = 'origin/worker/T-0001 holds 1 commit(s) this branch never had (f8d8f2405)'
        s = _ended('partial', unpushed=HEAD, push_refused=why)
        plan = decide(B.facts([_rebase_round()], sessions=[s], prs=[_conflicting()]),
                      B.config())
        info = B.stuck(plan, 'T-0001')
        self.assertEqual((info.owner, info.reason), ('operator', NOT_PUSHED + why))
        sess = F.FakeSessions([s])
        rec, _, _ = _tick(sess, [_rebase_round()], prs=[_conflicting()])
        self.assertEqual(sess.pushed, [])
        self.assertEqual(sess.ended, [('j1', False)])
        self.assertEqual(rec.fields['T-0001'][P.STUCK_REASON], NOT_PUSHED + why)

    def test_a_refused_host_push_is_stuck_on_the_refusal(self):
        sess = F.FakeSessions([_ended('partial', unpushed=HEAD)], fail={('push_rebase', 'j1')})
        rec, _, _ = _tick(sess, [_rebase_round()], prs=[_conflicting()])
        f = rec.fields['T-0001']
        self.assertEqual((f[P.STATE], f[P.STUCK_OWNER]), (State.STUCK.value, 'operator'))
        self.assertTrue(f[P.STUCK_REASON].startswith(NOT_PUSHED), f[P.STUCK_REASON])
        self.assertEqual(sess.ended, [('j1', False)])

    def test_a_partial_with_nothing_unpushed_is_still_stuck_on_its_report(self):
        plan = decide(B.facts([_rebase_round()], sessions=[_ended('partial')],
                              prs=[_conflicting()]), B.config())
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')


class Rejudge(unittest.TestCase):

    def stuck_item(self, reason):
        return _rebase_round(state=State.STUCK, stuck=B.M.Stuck(reason, 'operator', ''))

    def left(self, **kw):
        return B.session('j0', 'T-0001', alive=False, ended=True, branch='worker/T-0001', pr=7,
                         unpushed=HEAD, **kw)

    def test_the_two_stuck_reasons_are_stranded_ones(self):
        self.assertTrue(stranded(B.M.Stuck(COULD_NOT, 'operator')))
        self.assertTrue(stranded(B.M.Stuck('partial: NEEDS OPERATOR: ' + ASK, 'operator')))
        self.assertFalse(stranded(B.M.Stuck('partial: tests red', 'session')))

    def test_a_stranded_safe_rebase_is_pushed_then_re_evaluated(self):
        for reason in (COULD_NOT, 'partial: NEEDS OPERATOR: ' + ASK):
            with self.subTest(reason=reason[:30]):
                plan = decide(B.facts([self.stuck_item(reason)], prs=[_conflicting()],
                                      stranded=[self.left()]), B.config())
                self.assertEqual(B.of(plan, A.PushStranded), [A.PushStranded('j0', 'T-0001')])
                self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
                sess = F.FakeSessions(stranded=[self.left()])
                rec, _, _ = _tick(sess, [self.stuck_item(reason)], prs=[_conflicting()])
                self.assertEqual(sess.pushed, [('j0', 'worker/T-0001', HEAD)])
                f = rec.fields['T-0001']
                self.assertEqual(f[P.STATE], State.REVIEW.value)
                self.assertIn((P.STUCK_REASON, None), rec.writes[-1][1].items(), 'cleared')

    def test_no_stranded_worktree_or_an_unsafe_one_stays_stuck(self):
        for left in ([], [self.left(push_refused='origin holds a foreign commit')]):
            plan = decide(B.facts([self.stuck_item(COULD_NOT)], prs=[_conflicting()],
                                  stranded=left), B.config())
            self.assertEqual(B.of(plan, A.PushStranded), [])
            self.assertEqual(B.stuck(plan, 'T-0001').reason, COULD_NOT)

    def test_a_refused_stranded_push_is_stuck_on_the_refusal(self):
        sess = F.FakeSessions(stranded=[self.left()], fail={('push_rebase', 'j0')})
        rec, _, _ = _tick(sess, [self.stuck_item(COULD_NOT)], prs=[_conflicting()])
        f = rec.fields['T-0001']
        self.assertEqual(f[P.STATE], State.STUCK.value)
        self.assertTrue(f[P.STUCK_REASON].startswith(NOT_PUSHED), f[P.STUCK_REASON])


class Brief(unittest.TestCase):

    def test_the_rebase_ask_and_the_push_rule_leave_the_push_to_the_host(self):
        from asf.kernel import briefs as KB
        self.assertIn('do not force-push yourself', REBASE_ASK)
        self.assertIn('pushed: rebased <sha>', REBASE_ASK)
        self.assertIn('Do not force-push yourself; commit the rebased branch locally and end with '
                      'REPORT `status: done`, `pushed: rebased <sha>`; the host publishes it.',
                      KB.PUSH_RULE)


if __name__ == '__main__':
    unittest.main()
