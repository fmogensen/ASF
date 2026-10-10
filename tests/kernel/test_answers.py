"""An operator answer clears any Stuck it is newer than, whatever the owner, and the item is
relaunched first carrying it; an older answer clears nothing; legacy session Stuck reasons the
newer rules handle are re-judged once. The old floor's ``asf answer`` writes the ledger the
kernel reads."""
import os
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import facts as K
from asf.kernel import loop
from asf.kernel import ports as P

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
SINCE = '2026-10-09T20:42:10Z'
NO_REPORT_REASON = 'ended without a REPORT: work is committed on worker/T-0432 but not yet pushed'
HOOK_REASON = ('done without a push: pushed: no — hook refused twice: `bash tools/check_generic.sh'
               ' && python3 -m unittest -q te…')


def stuck(iid, reason='partial: the session stopped', owner='session', **kw):
    return B.task(iid, state=State.STUCK, stuck=B.M.Stuck(reason, owner), stuck_since=SINCE, **kw)


def answer(iid, text='Retry: continue on the same branch', at='2026-10-09T21:18:33Z'):
    return B.M.Answer(iid, text, at)


class Facts(unittest.TestCase):

    def test_a_newer_answer_counts_for_any_owner_and_an_older_never(self):
        for owner in ('session', 'loop', 'ci', 'operator'):
            it = stuck('T-0001', owner=owner)
            self.assertTrue(K.answer_counts(answer('T-0001'), it), owner)
            self.assertFalse(K.answer_counts(answer('T-0001', at='2026-10-09T19:00:00Z'), it),
                             owner)

    def test_with_a_time_unknown_only_an_operator_stuck_or_a_question_counts(self):
        self.assertFalse(K.answer_counts(answer('T-0001', at=''), stuck('T-0001')))
        self.assertTrue(K.answer_counts(answer('T-0001', at=''), stuck('T-0001', owner='operator')))
        self.assertTrue(K.answer_counts(answer('T-0001', at=''),
                                        B.task('T-0001', question='which?')))
        self.assertFalse(K.answer_counts(answer('T-0001'), B.task('T-0001')), 'not waiting')

    def test_the_old_cli_writes_the_ledger_the_kernel_reads(self):
        from asf.workers import answer as old
        product = env.Product('sample', {})
        self.assertEqual(old.ANSWERS_FILE, P.ANSWERS_FILE)
        self.assertEqual(os.path.realpath(old.answers_path(product)),
                         os.path.realpath(os.path.join(P.RealRecord(product).state_dir,
                                                       P.ANSWERS_FILE)))

    def test_the_ledger_keeps_the_answer_time(self):
        state = tempfile.mkdtemp()
        with open(os.path.join(state, P.ANSWERS_FILE), 'w') as f:
            f.write('{"item": "T-0001", "text": "yes", "at": "2026-10-09T21:18:33Z"}\n')
        rec = P.RealRecord(env.Product('sample', {'backlog_dir': state}), state_dir=state)
        self.assertEqual(rec.answers(), [B.M.Answer('T-0001', 'yes', '2026-10-09T21:18:33Z')])


class Decide(unittest.TestCase):

    def test_a_session_owned_stuck_clears_on_an_answer(self):
        plan = D.decide(B.facts([stuck('T-0001')], answers=[answer('T-0001')]), B.config())
        self.assertEqual([(a.item_id, a.text) for a in B.of(plan, A.ApplyAnswer)],
                         [('T-0001', 'Retry: continue on the same branch')])
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan), [], 'launched once the record is read again')

    def test_an_answer_goes_to_review_on_an_open_pr_and_building_under_a_live_session(self):
        f = B.facts([stuck('T-0001')], answers=[answer('T-0001')], prs=[B.pr(7, 'T-0001')])
        self.assertEqual(B.state(D.decide(f, B.config()), 'T-0001'), State.REVIEW)
        f = B.facts([stuck('T-0001')], answers=[answer('T-0001')],
                    sessions=[B.session('j1', 'T-0001')])
        self.assertEqual(B.state(D.decide(f, B.config()), 'T-0001'), State.BUILDING)

    def test_no_answer_keeps_it_stuck(self):
        plan = D.decide(B.facts([stuck('T-0001')]), B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual(B.of(plan, A.ClearStuck), [])

    def test_ended_without_a_report_is_relaunched_once(self):
        plan = D.decide(B.facts([stuck('T-0001', NO_REPORT_REASON)]), B.config())
        self.assertEqual([(a.item_id, a.attempt) for a in B.of(plan, A.ClearStuck)],
                         [('T-0001', D.NO_REPORT)])
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        spent = stuck('T-0001', NO_REPORT_REASON, attempts=[D.NO_REPORT, D.NO_REPORT])
        plan = D.decide(B.facts([spent]), B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK, 'its relaunch was spent')
        self.assertEqual(B.of(plan, A.ClearStuck), [])

    def test_a_refused_hook_is_relaunched_once_with_the_finding(self):
        plan = D.decide(B.facts([stuck('B-0001', HOOK_REASON)]), B.config())
        self.assertEqual([a.attempt for a in B.of(plan, A.ClearStuck)],
                         [D.RELAUNCH + D.HOOK_FINDING])
        self.assertEqual(B.state(plan, 'B-0001'), State.READY)
        spent = stuck('B-0001', HOOK_REASON, attempts=[D.RELAUNCH + D.HOOK_FINDING])
        self.assertEqual(B.state(D.decide(B.facts([spent]), B.config()), 'B-0001'), State.STUCK)
        other = stuck('B-0001', 'done without a push: pushed: no — nothing to push')
        self.assertEqual(B.state(D.decide(B.facts([other]), B.config()), 'B-0001'), State.STUCK)


class Loop(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})

    def tick(self, ports, **kw):
        return loop.tick(self.product, ports=ports, config=B.config(**kw), state_dir=self.tmp,
                         out=lambda *_: None)

    def test_answer_clears_then_launches_first_with_the_answer_as_finding(self):
        blocker = stuck('T-0009', rank=9, attempts=['launch: x', 'launch: x'])
        rec = F.FakeRecord([blocker, B.task('T-0001', rank=1), B.task('T-0002', after=['T-0009'])],
                           answers=[answer('T-0009')])
        sess = F.FakeSessions()
        ports = F.ports(record=rec, sessions=sess)
        self.tick(ports, max_sessions=1)
        self.assertEqual(rec.fields['T-0009'][P.STATE], 'ready')
        self.assertEqual(rec.fields['T-0009'][P.ATTEMPTS],
                         [D.answer_attempt('Retry: continue on the same branch')])
        self.assertNotIn(P.STUCK_REASON, rec.fields['T-0009'])
        sess.launched.clear()
        sess._sessions.clear()
        self.tick(ports, max_sessions=1)
        self.assertEqual([i for _k, i, _b, _t in sess.launched], ['T-0009'],
                         'the answered blocker relaunches before a better-ranked item')
        self.assertIn('operator answer: Retry: continue on the same branch', sess.launched[0][3])
        self.assertEqual(rec.fields['T-0009'][P.STATE], 'building')

    def test_an_answer_older_than_the_stuck_clears_nothing(self):
        rec = F.FakeRecord([stuck('T-0001')], answers=[answer('T-0001', at='2026-10-09T19:00:00Z')])
        ports = F.ports(record=rec)
        facts = K.read_facts(ports)
        self.assertEqual(facts.answers, [])
        self.tick(ports)
        self.assertNotIn(P.STATE, rec.fields['T-0001'])
        self.assertEqual(ports.sessions.launched, [])

    def test_a_refused_hook_relaunches_with_its_finding(self):
        rec = F.FakeRecord([stuck('B-0001', HOOK_REASON)])
        sess = F.FakeSessions()
        ports = F.ports(record=rec, sessions=sess)
        self.tick(ports)
        self.assertEqual(sess.launched, [])
        self.tick(ports)
        self.assertEqual([i for _k, i, _b, _t in sess.launched], ['B-0001'])
        self.assertIn(D.HOOK_FINDING, sess.launched[0][3])


if __name__ == '__main__':
    unittest.main()
