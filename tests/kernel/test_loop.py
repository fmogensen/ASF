"""The kernel's loop end to end over fake ports: facts -> decide -> apply, and what reaches the
card, GitHub and the session host."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from asf import env
from asf.kernel import ports as P
from asf.kernel import loop, status
from asf.kernel.decide import CRASH

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State


class Tick(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})
        self.lines = []

    def tick(self, ports, dry_run=False, **kw):
        return loop.tick(self.product, dry_run=dry_run, ports=ports, config=B.config(**kw),
                         state_dir=self.tmp, out=self.lines.append)

    def test_a_ready_item_is_launched_and_recorded_building(self):
        rec = F.FakeRecord([B.task('T-0001')])
        ports = F.ports(record=rec)
        summary = self.tick(ports)
        self.assertEqual([(k, i, b) for k, i, b, _ in ports.sessions.launched],
                         [('build', 'T-0001', 'worker/T-0001')])
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'building')
        self.assertEqual(summary['launches'], [('build', 'T-0001', 'worker/T-0001')])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, loop.LOCK_FILE)))

    def test_an_approved_pr_gets_auto_merge_and_lands(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.REVIEW)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001')], reviews=[B.review('T-0001')])
        ports = F.ports(record=rec, github=gh)
        self.tick(ports)
        self.assertEqual(gh.calls, [('auto_merge', 7)])
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'landing')

    def test_a_dead_session_is_ended_and_its_crash_counted(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.BUILDING)])
        sess = F.FakeSessions([B.session('j1', 'T-0001', alive=False, ended=False)])
        ports = F.ports(record=rec, sessions=sess)
        self.tick(ports)
        self.assertEqual(sess.ended, [('j1', True)])
        self.assertEqual(rec.fields['T-0001'][P.ATTEMPTS], [CRASH])
        self.assertEqual(sess.launched, [], 'not relaunched on the tick its session ended')
        self.tick(ports)
        self.assertEqual([i for _, i, _, _ in sess.launched], ['T-0001'])

    def test_paused_launches_nothing_but_still_lands(self):
        rec = F.FakeRecord([B.task('T-0001'), B.task('T-0002', state=State.REVIEW)], paused=True)
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0002')], reviews=[B.review('T-0002')])
        ports = F.ports(record=rec, github=gh)
        summary = self.tick(ports)
        self.assertEqual(ports.sessions.launched, [])
        self.assertEqual(gh.calls, [('auto_merge', 7)])
        self.assertTrue(summary['paused'])

    def test_one_failing_action_never_stops_the_rest(self):
        rec = F.FakeRecord([B.task('T-0001', rank=1), B.task('T-0002', rank=2),
                            B.task('T-0003', state=State.REVIEW)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0003')], reviews=[B.review('T-0003')],
                          fail={('auto_merge', 7)})
        sess = F.FakeSessions(fail={('launch', 'T-0001')})
        summary = self.tick(F.ports(record=rec, github=gh, sessions=sess))
        self.assertEqual([i for _, i, _, _ in sess.launched], ['T-0002'])
        self.assertEqual(rec.fields['T-0001'][P.ATTEMPTS],
                         ['launch: no account with a free seat'])
        self.assertEqual(rec.fields['T-0002'][P.STATE], 'building')
        self.assertEqual(len(summary['failed']), 2)

    def test_one_failing_card_write_never_stops_the_others(self):
        rec = F.FakeRecord([B.task('T-0001', rank=1), B.task('T-0002', rank=2)],
                           fail={('write', 'T-0001')})
        sess = F.FakeSessions()
        summary = self.tick(F.ports(record=rec, sessions=sess))
        self.assertEqual(rec.fields['T-0002'][P.STATE], 'building')
        self.assertEqual(summary['written'], ['T-0002'])

    def test_dry_run_writes_nothing(self):
        rec = F.FakeRecord([B.task('T-0001'), B.task('T-0002', state=State.REVIEW)],
                           specs={'F-0001': '## Stories\n- S-0001: one\n  - it works\n'})
        rec._items['F-0001'] = B.item('F-0001')
        rec.fields['F-0001'] = {}
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0002')], reviews=[B.review('T-0002')])
        sess = F.FakeSessions([B.session('j9', 'T-0009', alive=False)])
        summary = self.tick(F.ports(record=rec, github=gh, sessions=sess), dry_run=True)
        self.assertEqual((rec.writes, rec.minted, gh.calls, sess.launched, sess.ended),
                         ([], [], [], [], []))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, loop.LOCK_FILE)))
        self.assertEqual(summary['actions'], {'EndSession': 1, 'EnableAutoMerge': 1,
                                              'Launch': 1, 'MintStory': 1})
        self.assertIn('would launch build T-0001 on worker/T-0001', self.lines)

    def test_a_fix_round_counts_and_carries_the_findings(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.REVIEW)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001')],
                          reviews=[B.review('T-0001', verdict='changes', findings=['C1: no test'])])
        sess = F.FakeSessions()
        self.tick(F.ports(record=rec, github=gh, sessions=sess))
        (kind, iid, branch, brief), = sess.launched
        self.assertEqual((kind, branch), ('build', 'worker/T-0001'))
        self.assertIn('C1: no test', brief)
        self.assertEqual(rec.fields['T-0001'][P.FIX_ROUNDS], 1)
        self.assertEqual(rec.fields['T-0001'][P.FINDINGS], ['C1: no test'])

    def test_a_failed_update_on_a_conflict_is_an_attempt_then_stuck(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.REVIEW)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', conflicting=True)], reviews=[B.review('T-0001')],
                          fail={('update_branch', 7)})
        ports = F.ports(record=rec, github=gh)
        self.tick(ports)
        self.assertTrue(rec.fields['T-0001'][P.ATTEMPTS][0].startswith('conflict: PR #7'))
        self.tick(ports)
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'stuck')
        self.assertEqual(rec.fields['T-0001'][P.STUCK_OWNER], 'loop')
        self.assertTrue(rec.fields['T-0001'][P.STUCK_SINCE])

    def test_a_second_tick_on_the_same_world_writes_nothing_new(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.REVIEW), B.task('T-0002', rank=None)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', auto_merge=True)], reviews=[B.review('T-0001')])
        ports = F.ports(record=rec, github=gh)
        self.tick(ports)
        writes = len(rec.writes)
        self.tick(ports)
        self.assertEqual(len(rec.writes), writes)
        self.assertEqual(gh.calls, [])

    def test_an_answer_unsticks_the_item(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.STUCK,
                                   stuck=B.M.Stuck('which db?', 'operator'))],
                           answers=[B.answer('T-0001', 'postgres')])
        rec.fields['T-0001'] = {P.STATE: 'stuck', P.STUCK_REASON: 'which db?',
                                P.STUCK_OWNER: 'operator'}
        sess = F.FakeSessions()
        self.tick(F.ports(record=rec, sessions=sess))
        f = rec.fields['T-0001']
        self.assertEqual((f[P.ANSWERS], f[P.STATE], P.STUCK_REASON in f),
                         (['postgres'], 'ready', False))
        self.assertEqual(sess.launched, [])

    def test_a_done_features_spec_mints_nothing(self):
        rec = F.FakeRecord([B.item('F-0001', state=State.DONE), B.item('F-0002')],
                           specs={'F-0001': '## Stories\n- S-0001: old\n',
                                  'F-0002': '## Stories\n- S-0002: new\n'})
        self.tick(F.ports(record=rec))
        self.assertEqual([m[1] for m in rec.minted], ['S-0002'])

    def test_a_held_lock_returns_at_once(self):
        with loop.lock(self.tmp):
            summary = self.tick(F.ports(record=F.FakeRecord([B.task('T-0001')])))
        self.assertIn('locked', summary)


class Status(unittest.TestCase):

    def test_stuck_first_then_states_then_sessions(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.STUCK, stuck=B.M.Stuck('red', 'ci')),
                            B.task('T-0002', after=['T-0001']), B.task('T-0003',
                                                                       state=State.BUILDING)])
        rec.fields['T-0001'] = {P.STATE: 'stuck', P.STUCK_REASON: 'red', P.STUCK_OWNER: 'ci',
                                P.STUCK_SINCE: '2026-01-01T00:00:00Z'}
        sess = F.FakeSessions([B.session('j3', 'T-0003')])
        out = []
        text = status.status(env.Product('sample', {}), ports=F.ports(record=rec, sessions=sess),
                             config=B.config(), out=out.append)
        self.assertLess(text.index('## Stuck'), text.index('## States'))
        self.assertLess(text.index('## States'), text.index('## Sessions'))
        self.assertIn('| T-0001 | ci | 1 |', text)
        self.assertIn('| building | 1 |', text)
        self.assertIn('| j3 | T-0003 | build | alive |', text)


class Cli(unittest.TestCase):

    def test_kernel_commands_parse(self):
        from asf.cli import build_parser
        p = build_parser()
        a = p.parse_args(['kernel', 'tick', '--dry-run', '--product', 'x'])
        self.assertEqual((a.command, a.kernel_command, a.dry_run, a.product),
                         ('kernel', 'tick', True, 'x'))
        for cmd in ('status', 'pause', 'resume'):
            self.assertEqual(p.parse_args(['kernel', cmd, '--product', 'x']).kernel_command, cmd)

    def test_pause_and_resume_write_the_one_pause_flag(self):
        from asf import cli, pause
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, 'products'))
        with open(os.path.join(home, 'products', 'x.yaml'), 'w') as f:
            f.write('product: x\nrepo_slug: o/r\nmain: main\n')
        old = env.ASF_HOME
        env.ASF_HOME = home
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli._main(['kernel', 'pause', '--product', 'x']), 0)
                self.assertEqual(pause.read('x')['reason'], 'kernel pause')
                self.assertEqual(cli._main(['kernel', 'resume', '--product', 'x']), 0)
            self.assertIsNone(pause.read('x'))
        finally:
            env.ASF_HOME = old


if __name__ == '__main__':
    unittest.main()
