"""The two Stuck causes the live kernel showed (2026-10-09), fixed:

- a ``done`` session whose worktree holds commits origin lacks — a rebase its sandbox would not
  force-push (T-0196), a push its hook timed out on (B-0070) — is pushed once by the host with
  ``--force-with-lease=<branch>:<origin's tip>``, but only when origin's tip is in the branch's
  own history (an ancestor of HEAD or of a reflog entry, or patch-equivalent); then the PR flow
  goes on. Origin holding a commit the local history never had leaves it Stuck on that reason.
- a session that ended without a REPORT (T-0432) is relaunched once on its kept worktree, the
  relaunch first when it holds others; only a second no-REPORT is Stuck. A REPORT followed by a
  stray last message is still the REPORT.
"""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel.decide import NO_REPORT, NOT_PUSHED, decide
from asf.workers import pushlog

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
HEAD = 'a0971693bdddda6afe0aca75917dea37d1670516'


def _done(job='j1', item='T-0001', **kw):
    kw.setdefault('fields', {'status': 'done', 'pushed': 'no — the push was denied'})
    kw.setdefault('status', 'done')
    return B.session(job, item, alive=False, ended=True,
                     branch='worker/%s' % item, **kw)


def _tick(sess, items=None, prs=(), branches=()):
    rec = F.FakeRecord(items or [B.task('T-0001', state=State.BUILDING)])
    gh = F.FakeGitHub(prs=list(prs), branches=list(branches))
    lines = []
    loop.tick(env.Product('sample', {}), ports=F.ports(record=rec, github=gh, sessions=sess),
              config=B.config(), state_dir=tempfile.mkdtemp(), out=lines.append)
    return rec, gh, lines


class HostPushDecide(unittest.TestCase):

    def test_a_done_session_with_unpushed_work_moves_on_with_its_question_as_a_note(self):
        s = _done(result='question', unpushed=HEAD,
                  question='the branch is rebased and ready but not on origin')
        plan = decide(B.facts([B.task('T-0001', state=State.BUILDING)], sessions=[s]),
                      B.config())
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['worker/T-0001'])
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)
        self.assertEqual(len(B.of(plan, A.NoteItem)), 1)

    def test_origin_holding_foreign_commits_is_stuck_on_that_reason(self):
        why = 'origin/worker/T-0001 holds 1 commit(s) this branch never had (f8d8f2405)'
        s = _done(result='question', unpushed=HEAD, push_refused=why, question='push it')
        plan = decide(B.facts([B.task('T-0001', state=State.BUILDING)], sessions=[s]),
                      B.config())
        info = B.stuck(plan, 'T-0001')
        self.assertEqual(info.owner, 'operator')
        self.assertEqual(info.reason, NOT_PUSHED + why)
        self.assertEqual(B.of(plan, A.OpenPR), [])

    def test_partial_work_or_a_review_is_never_host_pushed(self):
        for s in (_done(status='partial', unpushed=HEAD, fields={'status': 'partial'}),
                  _done(kind='review', unpushed=HEAD)):
            plan = decide(B.facts([B.task('T-0001', state=State.BUILDING)], sessions=[s]),
                          B.config())
            self.assertEqual(B.of(plan, A.OpenPR), [], s)


class HostPushApply(unittest.TestCase):

    def test_the_host_pushes_once_then_opens_the_pr(self):
        sess = F.FakeSessions([_done(result='question', unpushed=HEAD, question='push it')])
        rec, gh, _ = _tick(sess)
        self.assertEqual(sess.pushed, [('j1', 'worker/T-0001', HEAD)])
        self.assertEqual(sess.ended, [('j1', True)])
        self.assertEqual([o[0] for o in gh.opened], ['worker/T-0001'])
        self.assertEqual(rec.fields['T-0001'][P.STATE], State.REVIEW.value)

    def test_a_refused_host_push_skips_the_pr_and_is_stuck_on_the_operator(self):
        sess = F.FakeSessions([_done(unpushed=HEAD)], fail={('push_rebase', 'j1')})
        rec, gh, _ = _tick(sess)
        self.assertEqual(sess.ended, [('j1', False)], 'the worktree is kept')
        self.assertEqual(gh.opened, [])
        f = rec.fields['T-0001']
        self.assertEqual(f[P.STATE], State.STUCK.value)
        self.assertEqual(f[P.STUCK_OWNER], 'operator')
        self.assertTrue(f[P.STUCK_REASON].startswith(NOT_PUSHED), f[P.STUCK_REASON])


class NoReportRelaunch(unittest.TestCase):

    def no_report(self, job='j1', item='T-0001'):
        return B.session(job, item, alive=False, ended=True, result='none',
                         last_line='That monitor has served its purpose')

    def test_the_first_no_report_is_relaunched_on_its_kept_worktree_the_second_is_stuck(self):
        sess = F.FakeSessions([self.no_report()])
        rec, _, _ = _tick(sess)
        self.assertEqual(sess.ended, [('j1', False)], 'worktree and branch kept')
        self.assertEqual(rec.fields['T-0001'][P.ATTEMPTS], [NO_REPORT])
        self.assertNotEqual(rec.fields['T-0001'].get(P.STATE), State.STUCK.value)
        self.assertEqual(sess.launched, [], 'not on the tick it ended')
        # next tick: relaunched on its branch
        rec2 = F.FakeRecord([B.task('T-0001', state=State.READY, attempts=[NO_REPORT])])
        sess2 = F.FakeSessions()
        loop.tick(env.Product('sample', {}), ports=F.ports(record=rec2, sessions=sess2),
                  config=B.config(), state_dir=tempfile.mkdtemp(), out=lambda _l: None)
        self.assertEqual([(k, i, b) for k, i, b, _ in sess2.launched],
                         [('build', 'T-0001', 'worker/T-0001')])
        # the relaunch ended without a REPORT too: Stuck on the session, its last line named
        plan = decide(B.facts([B.task('T-0001', state=State.BUILDING, attempts=[NO_REPORT])],
                              sessions=[self.no_report('j2')]), B.config())
        info = B.stuck(plan, 'T-0001')
        self.assertEqual((info.owner, info.reason),
                         ('session', 'ended without a REPORT: That monitor has served its purpose'))

    def test_a_relaunch_that_holds_others_goes_first(self):
        items = [B.task('T-0001', rank=1),
                 B.task('T-0002', rank=5, attempts=[NO_REPORT]),
                 B.task('T-0003', rank=1, after=['T-0002']),
                 B.task('T-0004', rank=1, after=['T-0003'])]
        plan = decide(B.facts(items), B.config(max_sessions=1))
        self.assertEqual(B.launched(plan), ['T-0002'])


def _git(args, cwd):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class RealGit(unittest.TestCase):
    """The real port over scratch repos, shaped like T-0196: origin's branch has a commit the
    session then rebased with a conflict, so its patch differs and only the reflog proves it."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.state = os.path.join(root, 'state')
        self.origin = os.path.join(root, 'origin.git')
        self.wt = os.path.join(root, 'wt')
        _git(['init', '-q', '--bare', '-b', 'main', self.origin], root)
        _git(['clone', '-q', self.origin, self.wt], root)
        for k, v in (('user.name', 't'), ('user.email', 't@example.invalid'),
                     ('commit.gpgsign', 'false')):
            _git(['config', k, v], self.wt)
        self.write('a.txt', 'base\n')
        _git(['add', '.'], self.wt)
        _git(['commit', '-q', '-m', 'base'], self.wt)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.wt)
        _git(['checkout', '-q', '-b', 'worker/T-0001'], self.wt)
        self.write('a.txt', 'work\n')
        _git(['commit', '-q', '-am', 'work'], self.wt)
        _git(['push', '-q', 'origin', 'worker/T-0001'], self.wt)
        self.old = _git(['rev-parse', 'HEAD'], self.wt)
        _git(['checkout', '-q', 'main'], self.wt)
        self.write('a.txt', 'trunk\n')
        _git(['commit', '-q', '-am', 'trunk moved'], self.wt)
        _git(['push', '-q', 'origin', 'main'], self.wt)
        _git(['checkout', '-q', 'worker/T-0001'], self.wt)
        # the session's rebase: a conflict resolved by hand (a different patch than origin's)
        subprocess.run(['git', 'rebase', '-q', 'main'], cwd=self.wt, capture_output=True)
        self.write('a.txt', 'trunk + work\n')
        _git(['add', 'a.txt'], self.wt)
        subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=self.wt,
                       check=True, capture_output=True)
        self.head = _git(['rev-parse', 'HEAD'], self.wt)
        self.product = env.Product('sample', {'repo_dir': self.wt, 'main': 'main'})

    def write(self, name, text):
        with open(os.path.join(self.wt, name), 'w') as f:
            f.write(text)

    def test_a_rebased_branch_is_unpushed_safe_and_pushed_with_a_lease(self):
        self.assertTrue(_git(['cherry', self.head, self.old], self.wt).startswith('+'),
                        'the patch differs: only the reflog proves origin was ours')
        head, refused = P.unpushed_head(self.wt, 'worker/T-0001', 'main')
        self.assertEqual((head, refused), (self.head, ''))
        s = _done(unpushed=head, worktree=self.wt)
        with mock.patch('asf.env.state_dir', return_value=self.state):
            note = P.RealSessions(self.product).push_rebase(s, head)
            self.assertIn('--force-with-lease', note)
            self.assertEqual(pushlog.shas(self.product, 'j1'), [self.head])
        self.assertEqual(_git(['rev-parse', 'worker/T-0001'], self.origin), self.head)
        self.assertEqual(P.unpushed_head(self.wt, 'worker/T-0001', 'main'), ('', ''))

    def test_a_commit_origin_holds_that_history_never_had_is_refused(self):
        other = os.path.join(os.path.dirname(self.wt), 'other')
        _git(['clone', '-q', '-b', 'worker/T-0001', self.origin, other], self.wt)
        _git(['-c', 'user.name=o', '-c', 'user.email=o@example.invalid', 'commit', '-q',
              '--allow-empty', '-m', 'someone else'], other)
        _git(['push', '-q', 'origin', 'worker/T-0001'], other)
        foreign = _git(['rev-parse', 'HEAD'], other)
        head, refused = P.unpushed_head(self.wt, 'worker/T-0001', 'main')
        self.assertEqual(head, self.head)
        self.assertIn('never had (', refused)
        self.assertIn(foreign[:9], refused)
        with mock.patch('asf.env.state_dir', return_value=self.state):
            with self.assertRaises(P.PortError):
                P.RealSessions(self.product).push_rebase(_done(worktree=self.wt), head)
        self.assertEqual(_git(['rev-parse', 'worker/T-0001'], self.origin), foreign)

    def test_the_trunk_is_never_a_target(self):
        _git(['checkout', '-q', 'main'], self.wt)
        _git(['commit', '-q', '--allow-empty', '-m', 'local only'], self.wt)
        self.assertEqual(P.unpushed_head(self.wt, 'main', 'main'), ('', ''))


class ReportResult(unittest.TestCase):

    def test_a_report_followed_by_a_stray_message_is_still_the_report(self):
        report = 'done.\n\n```\nREPORT\nitem: T-0432\nstatus: done\npushed: no\n```'
        path = os.path.join(tempfile.mkdtemp(), 'job.jsonl')
        with open(path, 'w') as f:
            for rec in ({'type': 'system', 'subtype': 'init'},
                        {'type': 'result', 'result': report},
                        {'type': 'result', 'result': 'That monitor has served its purpose.'}):
                f.write(json.dumps(rec) + '\n')
        got = P.report_result(path)
        self.assertEqual(got['result'], report)
        with open(path, 'a') as f:  # a later run starts afresh
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'result': 'no report here'}) + '\n')
        self.assertEqual(P.report_result(path)['result'], 'no report here')


if __name__ == '__main__':
    unittest.main()
