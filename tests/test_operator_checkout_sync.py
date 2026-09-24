"""The operator's record checkout (``backlog_dir``) follows origin after every tick push.

The tick works in its own clone and pushes; the read views (``asf status``'s Decisions row,
``asf backlog``, ``asf next``) read ``backlog_dir``. Only a console command run there used to
pull it, so a groom whose answers the tick applied and pushed still showed every card
undecided in ``asf status`` — the checkout sat where the last console command left it."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from asf import env
from asf.tick import tick


def _git(args, cwd):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class OperatorCheckoutSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='checkout_sync_')
        seed = os.path.join(self.tmp, 'seed')
        os.makedirs(seed)
        _git(['init', '-q', '-b', 'main'], seed)
        _git(['config', 'user.email', 'a@example.com'], seed)
        _git(['config', 'user.name', 'a'], seed)
        os.makedirs(os.path.join(seed, 'bugs'))
        with open(os.path.join(seed, 'bugs', 'B-0001.md'), 'w') as f:
            f.write('---\nid: B-0001\ndecided: false\n---\n')
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'seed'], seed)
        self.origin = os.path.join(self.tmp, 'origin.git')
        _git(['clone', '-q', '--bare', seed, self.origin], self.tmp)
        self.checkout = os.path.join(self.tmp, 'checkout')
        _git(['clone', '-q', self.origin, self.checkout], self.tmp)
        _git(['config', 'user.email', 'a@example.com'], self.checkout)
        _git(['config', 'user.name', 'a'], self.checkout)
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.product = env.Product('sample', {'backlog_dir': self.checkout})

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tick_decides(self):
        ctx = tick.Context(self.product)
        root = ctx.record_root()
        with open(os.path.join(root, 'bugs', 'B-0001.md'), 'w') as f:
            f.write('---\nid: B-0001\ndecided: true\n---\n')
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tick.commit_and_push(ctx)
        return rc, buf.getvalue()

    def _checkout_card(self):
        with open(os.path.join(self.checkout, 'bugs', 'B-0001.md')) as f:
            return f.read()

    def test_a_pushed_tick_fast_forwards_the_operator_checkout(self):
        rc, _out = self._tick_decides()
        self.assertEqual(rc, 0)
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout),
                         _git(['rev-parse', 'main'], self.origin))
        self.assertIn('decided: true', self._checkout_card())

    def test_a_checkout_with_local_edits_is_left_alone_and_named(self):
        with open(os.path.join(self.checkout, 'bugs', 'B-0001.md'), 'a') as f:
            f.write('hand edit\n')
        before = _git(['rev-parse', 'HEAD'], self.checkout)
        rc, out = self._tick_decides()
        self.assertEqual(rc, 0)
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout), before)
        self.assertIn('hand edit', self._checkout_card())
        self.assertIn('not fast-forwarded', out)

    def test_no_change_tick_still_catches_the_checkout_up(self):
        # origin moved by someone else's push (a session, another console): the next tick,
        # even one with nothing of its own to commit, brings the checkout up to it
        other = os.path.join(self.tmp, 'other')
        _git(['clone', '-q', self.origin, other], self.tmp)
        _git(['config', 'user.email', 'a@example.com'], other)
        _git(['config', 'user.name', 'a'], other)
        with open(os.path.join(other, 'bugs', 'B-0001.md'), 'w') as f:
            f.write('---\nid: B-0001\ndecided: true\n---\n')
        _git(['commit', '-q', '-am', 'elsewhere'], other)
        _git(['push', '-q', 'origin', 'HEAD:main'], other)
        ctx = tick.Context(self.product)
        ctx.record_root()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tick.commit_and_push(ctx), 0)
        self.assertIn('decided: true', self._checkout_card())


if __name__ == '__main__':
    unittest.main()
