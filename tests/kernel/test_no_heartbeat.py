"""B-0098: a kernel session is launched with no heartbeat — its brief names no HEARTBEAT command,
no ``refs/asf/hb/`` ref and no notes file, and the port spawns it without the beat — and a session
whose REPORT is ``done`` with a pushed head moves on even when it also asks a ``NEEDS OPERATOR``
question: the question becomes an item note, shown by status, holding nothing."""
import inspect
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel import status as KS
from asf.kernel.decide import decide

try:
    from kernel import builders as B
    from kernel import fakes as F
    from kernel.test_go_live import _briefer, _product
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F
    from tests.kernel.test_go_live import _briefer, _product

State = B.State
FORBIDDEN = ('HEARTBEAT', 'refs/asf/hb', 'asf-heartbeat', 'NOTES.asf')


class KernelBriefsCarryNoHeartbeat(unittest.TestCase):

    def brief(self, kind, item=None, findings=(), pr=None):
        item = item or B.task('T-0001', state=State.REVIEW if pr else State.READY)
        return _briefer(_product())(item, A.Launch(kind, item.id, 'worker/%s' % item.id),
                                    list(findings), pr).text

    def test_coder_fix_and_review_briefs_name_no_heartbeat(self):
        texts = {
            'coder': self.brief('build'),
            'fix-bug': self.brief('build', item=B.task('B-0098', type='bug')),
            'correct': self.brief('build', findings=['src/a.py:3 — guard'], pr=B.pr(7, 'T-0001')),
            'review': self.brief('review', pr=B.pr(7, 'T-0001')),
        }
        for kind, text in texts.items():
            for word in FORBIDDEN:
                with self.subTest(kind=kind, word=word):
                    self.assertNotIn(word, text)
            with self.subTest(kind=kind):
                self.assertNotIn('heartbeat', text.lower())
                self.assertIn('REPORT', text)

    def test_the_floors_tail_keeps_its_heartbeat_wording(self):
        from asf.briefs.build import TAIL
        self.assertIn('HEARTBEAT', TAIL)

    def test_the_port_spawns_with_no_heartbeat(self):
        from asf.workers import pool, spawn
        self.assertTrue(inspect.signature(spawn.spawn).parameters['heartbeat'].default)
        sessions = P.RealSessions(env.Product('sample', {}), cfg={})
        acct = mock.Mock(role='local')
        acct.name = 'a1'
        with mock.patch.object(P.RealSessions, '_account', return_value=acct), \
                mock.patch.object(spawn, 'spawn') as sp, \
                mock.patch.object(pool, 'update_session'):
            brief = mock.Mock(text='brief', model='', add_dirs=(), card_digest='')
            sessions.launch('build', 'T-0001', 'worker/T-0001', brief)
        self.assertIs(sp.call_args.kwargs['heartbeat'], False)

    def test_runtime_adds_no_block_without_a_beat(self):
        from asf.workers import runtime
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
            f.write('the brief\n')
        job = mock.Mock(brief_path=f.name, heartbeat=None)
        self.assertEqual(runtime.brief_text(job), 'the brief\n')
        self.assertEqual(runtime.session_brief(job), f.name)


def _done(question=None, result='pushed', pushed='yes abc1234'):
    return B.session('j1', 'T-0001', alive=False, ended=True, result=result, status='done',
                     question=question, fields={'pushed': pushed, 'tests': 'ok'})


Q = 'the HEARTBEAT setup command was blocked by the sandbox'


class DoneAndPushedMovesOn(unittest.TestCase):

    def plan(self, session, prs=()):
        return decide(B.facts([B.task('T-0001', state=State.BUILDING)], sessions=[session],
                              prs=list(prs)), B.config())

    def test_done_pushed_with_a_question_goes_to_review_and_notes_it(self):
        plan = self.plan(_done(Q), prs=[B.pr(7, 'T-0001')])
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        notes = B.of(plan, A.NoteItem)
        self.assertEqual([(n.item_id, n.text) for n in notes],
                         [('T-0001', 'j1: NEEDS OPERATOR: %s' % Q)])

    def test_done_pushed_with_a_question_and_no_pr_yet_is_not_stuck(self):
        plan = self.plan(_done(Q))
        self.assertNotEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual(len(B.of(plan, A.NoteItem)), 1)

    def test_done_pushed_without_a_question_leaves_no_note(self):
        plan = self.plan(_done(), prs=[B.pr(7, 'T-0001')])
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
        self.assertEqual(B.of(plan, A.NoteItem), [])

    def test_done_with_no_push_and_a_question_is_stuck_on_the_operator(self):
        plan = self.plan(_done(Q, result='question', pushed='no — sandbox'))
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')
        self.assertEqual(B.of(plan, A.NoteItem), [])

    def test_done_with_no_push_and_no_pr_is_stuck(self):
        plan = self.plan(_done(result='report', pushed='no — why'))
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)

    def test_partial_or_blocked_that_pushed_is_still_stuck(self):
        for status in ('partial', 'blocked'):
            with self.subTest(status=status):
                s = _done(Q)
                s.status = status
                plan = self.plan(s, prs=[B.pr(7, 'T-0001')])
                self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
                self.assertEqual(B.of(plan, A.NoteItem), [])

    def test_a_note_already_on_the_card_is_not_added_again(self):
        it = B.task('T-0001', state=State.BUILDING, notes=['j1: NEEDS OPERATOR: %s' % Q])
        plan = decide(B.facts([it], sessions=[_done(Q)], prs=[B.pr(7, 'T-0001')]), B.config())
        self.assertEqual(B.of(plan, A.NoteItem), [])

    def test_the_tick_writes_the_note_and_status_shows_it(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.BUILDING)])
        sess = F.FakeSessions([_done(Q)])
        ports = F.ports(record=rec, github=F.FakeGitHub(prs=[B.pr(7, 'T-0001')]), sessions=sess)
        state_dir = tempfile.mkdtemp()
        loop.tick(env.Product('sample', {}), ports=ports, config=B.config(),
                  state_dir=state_dir, out=lambda *_: None)
        written = [f for iid, f in rec.writes if iid == 'T-0001' and P.NOTES in f]
        self.assertEqual(written[-1][P.NOTES], ['j1: NEEDS OPERATOR: %s' % Q])
        self.assertNotIn(State.STUCK.value, [f.get(P.STATE) for _, f in rec.writes])
        lines = []
        KS.status(env.Product('sample', {}), ports=ports, out=lines.append, state_dir=state_dir)
        self.assertIn('## Notes', lines[-1])
        self.assertIn('| T-0001 | j1: NEEDS OPERATOR: the HEARTBEAT setup', lines[-1])


if __name__ == '__main__':
    unittest.main()
