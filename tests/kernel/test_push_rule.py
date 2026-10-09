"""A kernel session pushes its own branch (B-82658, B-83312): every kernel brief says so — a
rebase with ``git push --force-with-lease origin <branch>`` — and none says "the factory
publishes", while the floor's own brief keeps its wording. The host's safety net: a session that
still ended ``pushed: rebased <sha>`` has that sha pushed once from its worktree."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import actions as A
from asf.kernel import briefs as KB
from asf.kernel import loop
from asf.kernel import ports as P
from asf.workers import pushlog

try:
    from kernel import builders as B
    from kernel import fakes as F
    from kernel.test_go_live import _briefer, _product
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F
    from tests.kernel.test_go_live import _briefer, _product

State = B.State
LEASE = '`git push --force-with-lease origin worker/T-0001`'


class KernelBriefsPush(unittest.TestCase):

    def brief(self, kind, findings=(), pr=None):
        item = B.task('T-0001', state=State.REVIEW if pr else State.READY)
        return _briefer(_product())(item, A.Launch(kind, 'T-0001', 'worker/T-0001'),
                                    list(findings), pr).text

    def test_every_kernel_brief_says_the_session_pushes_its_own_rebase(self):
        texts = {'coder': self.brief('build'),
                 'correct': self.brief('build', ['src/a.py:3 — guard'], B.pr(7, 'T-0001')),
                 'review': self.brief('review', pr=B.pr(7, 'T-0001'))}
        for kind, text in texts.items():
            with self.subTest(kind=kind):
                self.assertNotIn('factory publishes', text)
                self.assertNotIn('the factory publishes', text.lower())
                self.assertIn(LEASE, text)
                self.assertIn('You push your own branch', text)
        self.assertIn('kind: correct', texts['correct'])
        # the review's verdict lines stay last
        self.assertTrue(texts['review'].rstrip().endswith(KB.VERDICT_RULE.splitlines()[-1]))

    def test_the_correct_template_and_the_report_line_are_rewritten(self):
        text = self.brief('build', ['src/a.py:3 — guard'], B.pr(7, 'T-0001'))
        self.assertIn('push it yourself with ' + LEASE, text)
        self.assertIn('rebased <sha> — pushed with --force-with-lease', text)

    def test_the_floors_brief_keeps_its_wording(self):
        from asf.briefs.build import TAIL
        self.assertIn('the factory publishes', TAIL)
        with open(os.path.join(os.path.dirname(KB.__file__), '..', 'briefs', 'templates',
                               'correct.md'), encoding='utf-8') as f:
            self.assertIn('the factory publishes', f.read())

    def test_the_rewrite_is_idempotent_on_text_without_the_floor_wording(self):
        out = KB.kernel_push_text('plain brief\n', 'b1')
        self.assertTrue(out.startswith('plain brief\n\n## Pushing'))
        self.assertIn('`git push --force-with-lease origin b1`', out)


def _rebased(job, kind='build', sha='abc1234', worktree='/work/j'):
    return B.session(job, 'T-0001', kind=kind, alive=False, ended=True, result='report',
                     status='done', branch='worker/T-0001', worktree=worktree,
                     fields={'pushed': 'rebased %s — the factory publishes' % sha})


class SafetyNet(unittest.TestCase):

    def tick(self, sess, prs=()):
        rec = F.FakeRecord([B.task('T-0001', state=State.BUILDING)])
        ports = F.ports(record=rec, github=F.FakeGitHub(prs=list(prs)), sessions=sess)
        lines = []
        loop.tick(env.Product('sample', {}), ports=ports, config=B.config(),
                  state_dir=tempfile.mkdtemp(), out=lines.append)
        return lines

    def test_an_ended_rebase_is_pushed_once_before_the_session_ends(self):
        sess = F.FakeSessions([_rebased('j1')])
        self.tick(sess, prs=[B.pr(7, 'T-0001')])
        self.assertEqual(sess.pushed, [('j1', 'worker/T-0001', 'abc1234')])
        self.assertEqual(sess.ended, [('j1', True)])

    def test_a_refused_push_keeps_the_worktree(self):
        sess = F.FakeSessions([_rebased('j1')], fail={('push_rebase', 'j1')})
        self.tick(sess, prs=[B.pr(7, 'T-0001')])
        self.assertEqual(sess.pushed, [])
        self.assertEqual(sess.ended, [('j1', False)])

    def test_a_plain_push_or_a_review_is_never_pushed_by_the_host(self):
        yes = B.session('j2', 'T-0001', alive=False, ended=True, result='pushed', status='done',
                        fields={'pushed': 'yes abc1234'})
        sess = F.FakeSessions([yes])
        self.tick(sess, prs=[B.pr(7, 'T-0001')])
        self.assertEqual(sess.pushed, [])
        from asf.kernel.apply import rebased_sha
        self.assertEqual(rebased_sha(_rebased('rv', kind='review')), '')
        self.assertEqual(rebased_sha(_rebased('j3')), 'abc1234')


def _git(args, cwd):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class RealPushRebase(unittest.TestCase):
    """The real port over scratch repos: the worktree's HEAD is pushed over origin's branch with
    a lease, recorded on the push log, and a second call pushes nothing."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.state = os.path.join(root, 'state')
        self.origin = os.path.join(root, 'origin.git')
        self.wt = os.path.join(root, 'wt')
        _git(['init', '-q', '--bare', '-b', 'main', self.origin], root)
        _git(['clone', '-q', self.origin, self.wt], root)
        _git(['commit', '-q', '--allow-empty', '-m', 'base'], self.wt)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.wt)
        _git(['checkout', '-q', '-b', 'worker/T-0001'], self.wt)
        _git(['commit', '-q', '--allow-empty', '-m', 'work'], self.wt)
        _git(['push', '-q', 'origin', 'worker/T-0001'], self.wt)
        _git(['checkout', '-q', 'main'], self.wt)
        _git(['commit', '-q', '--allow-empty', '-m', 'trunk moved'], self.wt)
        _git(['push', '-q', 'origin', 'main'], self.wt)
        _git(['checkout', '-q', 'worker/T-0001'], self.wt)
        _git(['rebase', '-q', 'main'], self.wt)
        self.head = _git(['rev-parse', 'HEAD'], self.wt)
        self.product = env.Product('sample', {'repo_dir': self.wt, 'main': 'main'})

    def push(self, sha):
        s = _rebased('j1', sha=sha, worktree=self.wt)
        with mock.patch('asf.env.state_dir', return_value=self.state):
            note = P.RealSessions(self.product).push_rebase(s, sha)
            return note, pushlog.shas(self.product, 'j1')

    def test_the_rebased_head_is_pushed_with_a_lease_and_recorded(self):
        note, log = self.push(self.head[:7])
        self.assertIn('pushed rebased', note)
        self.assertEqual(_git(['rev-parse', 'worker/T-0001'], self.origin), self.head)
        self.assertEqual(log, [self.head])
        note, _ = self.push(self.head[:7])
        self.assertIn('already pushed', note)

    def test_a_worktree_that_moved_on_is_not_pushed(self):
        with self.assertRaises(P.PortError):
            self.push('0000000')
        self.assertNotEqual(_git(['rev-parse', 'worker/T-0001'], self.origin), self.head)


if __name__ == '__main__':
    unittest.main()
