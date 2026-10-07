"""The operator's record checkout (``backlog_dir``) follows origin after every tick push.

The tick works in its own clone and pushes; the read views (``asf status``'s Decisions row,
``asf backlog``, ``asf next``) read ``backlog_dir``. Only a console command run there used to
pull it, so a groom whose answers the tick applied and pushed still showed every card
undecided in ``asf status`` — the checkout sat where the last console command left it."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from asf import env
from asf.tick import shadow, tick


def _git(args, cwd):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _args(**kw):
    import argparse
    base = dict(product='sample', shadow=False, fresh=False, steps=None, manifest=False, daily=False)
    base.update(kw)
    return argparse.Namespace(**base)


class RefusedSyncIsReadableTests(unittest.TestCase):
    """A2 (F-0260): the operator's record checkout the tick cannot fast-forward says so every
    tick, with the drift and the record-relative paths that block it — never the checkout's own
    filesystem path (D10) — and the record step itself still reads ``ok`` (D4)."""

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

    def dirty_the_checkout(self):
        with open(os.path.join(self.checkout, 'bugs', 'B-0001.md'), 'a') as f:
            f.write('hand edit\n')

    def origin_moves(self, n):
        """``n`` more commits land on origin from a clone of its own, elsewhere — never through
        ``self.checkout``, which stays exactly where it was."""
        other = tempfile.mkdtemp(prefix='elsewhere_', dir=self.tmp)
        _git(['clone', '-q', self.origin, other], self.tmp)
        _git(['config', 'user.email', 'a@example.com'], other)
        _git(['config', 'user.name', 'a'], other)
        for i in range(n):
            with open(os.path.join(other, 'bugs', f'B-{i + 2:04d}.md'), 'w') as f:
                f.write(f'---\nid: B-{i + 2:04d}\n---\n')
            _git(['add', '-A'], other)
            _git(['commit', '-q', '-m', f'elsewhere {i}'], other)
        _git(['push', '-q', 'origin', 'HEAD:main'], other)

    def events_of_kind(self, kind):
        """``metrics/events/<day>.jsonl`` out of the record clone (:func:`ctx.event`'s own
        write) — read directly by the fixed path :func:`asf.tick.shadow.record_dir` derives from
        this product and ``env.ASF_HOME``, since every ``_tick_decides`` call resets that same
        clone rather than making a new one."""
        root = shadow.record_dir(self.product)
        found = []
        events_dir = os.path.join(root, 'metrics', 'events')
        if not os.path.isdir(events_dir):
            return found
        for name in sorted(os.listdir(events_dir)):
            with open(os.path.join(events_dir, name), encoding='utf-8') as f:
                for raw in f:
                    raw = raw.strip()
                    if raw and json.loads(raw).get('kind') == kind:
                        found.append(json.loads(raw))
        return found

    def test_a_pushed_tick_fast_forwards_the_operator_checkout(self):
        rc, _out = self._tick_decides()
        self.assertEqual(rc, 0)
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout),
                         _git(['rev-parse', 'main'], self.origin))
        self.assertIn('decided: true', self._checkout_card())

    def test_a_checkout_with_local_edits_is_left_alone_and_named(self):
        """Unchanged (P18), with the drift and the record-relative path added to the line."""
        self.dirty_the_checkout()
        before = _git(['rev-parse', 'HEAD'], self.checkout)
        rc, out = self._tick_decides()
        self.assertEqual(rc, 0)
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout), before)
        self.assertIn('hand edit', self._checkout_card())
        self.assertIn('not fast-forwarded', out)
        self.assertIn('local changes: bugs/B-0001.md', out)
        self.assertNotIn(self.checkout, out)           # D10: never the machine path

    def test_a_refused_sync_raises_a_needs_operator_line_with_the_drift(self):
        self.dirty_the_checkout()
        self.origin_moves(3)
        _rc, out = self._tick_decides()
        line = next(l for l in out.splitlines() if l.startswith('NEEDS OPERATOR:'))
        self.assertIn('the record checkout', line)
        self.assertIn('behind', line)
        self.assertIn('bugs/B-0001.md', line)
        self.assertNotIn(self.checkout, line)

    def test_the_needs_operator_line_is_said_again_on_the_next_tick(self):
        """D5: pure over the checkout's state — no ledger, no marker, said until it is gone."""
        self.dirty_the_checkout()
        self.assertIn('NEEDS OPERATOR:', self._tick_decides()[1])
        self.assertIn('NEEDS OPERATOR:', self._tick_decides()[1])

    def test_a_refused_sync_writes_one_event_naming_no_machine_path(self):
        self.dirty_the_checkout()
        self._tick_decides()
        ev = self.events_of_kind('record_checkout_stale')
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['why'], 'local-changes')
        self.assertEqual(ev[0]['paths'], ['bugs/B-0001.md'])
        self.assertNotIn(self.checkout, json.dumps(ev[0]))

    def test_the_record_step_still_reads_ok_and_the_later_steps_still_run(self):
        """D4: a dirty operator checkout must not trip B-0083 and stop the factory."""
        from unittest import mock
        self.dirty_the_checkout()
        os.makedirs(os.path.join(env.ASF_HOME, 'products'), exist_ok=True)
        with open(env.product_path('sample'), 'w') as f:
            f.write(f'repo_slug: x/y\nbacklog_dir: {self.checkout}\n')
        with mock.patch.object(tick, 'run_step0', lambda root, product, fresh=False: None), \
                mock.patch.object(tick, 'run_record_tail_step', lambda ctx: None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = tick.cmd_tick(_args(steps='record'))
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn('[step:record]', out)
        self.assertIn('ok=yes', out)
        self.assertNotIn('nothing else ran', out)

    def test_a_clean_checkout_says_nothing_and_still_fast_forwards(self):
        _rc, out = self._tick_decides()
        self.assertNotIn('NEEDS OPERATOR:', out)
        self.assertNotIn('not fast-forwarded', out)
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout),
                         _git(['rev-parse', 'main'], self.origin))

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
