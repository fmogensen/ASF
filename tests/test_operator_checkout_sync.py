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


class StrandedRecordCommitsTests(unittest.TestCase):
    """F-0260: a record commit made in the operator's checkout whose push was refused (``asf new``,
    ``asf inbox``) is rebased onto origin and pushed by the next sync — never left diverged —
    with ``index.json`` re-derived when it conflicts; anything else is left exactly as it was."""

    def setUp(self):
        from asf.record.ids import write_new_item
        from asf.record.index import do_index
        self.write_new_item, self.do_index = write_new_item, do_index
        self.tmp = tempfile.mkdtemp(prefix='stranded_')
        self.origin = os.path.join(self.tmp, 'origin.git')
        _git(['init', '-q', '--bare', '-b', 'main', self.origin], self.tmp)
        self.checkout = self._clone('checkout')
        write_new_item(self.checkout, {}, 'feature', 'F-0001', {'title': 'Parent feature'}, '',
                       '2026-01-01', 'seed')
        with open(os.path.join(self.checkout, 'README.md'), 'w') as f:
            f.write('record\n')
        self._commit(self.checkout, 'seed')
        _git(['push', '-q', 'origin', 'HEAD:main'], self.checkout)
        self.other = self._clone('other')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.product = env.Product('sample', {'backlog_dir': self.checkout})

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clone(self, name):
        path = os.path.join(self.tmp, name)
        _git(['clone', '-q', self.origin, path], self.tmp)
        _git(['config', 'user.email', 'a@example.com'], path)
        _git(['config', 'user.name', 'a'], path)
        return path

    def _commit(self, repo, message):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.do_index(repo), 0)
        _git(['add', '-A'], repo)
        _git(['commit', '-q', '-m', message], repo)

    def _card(self, repo, iid):
        self.write_new_item(repo, {}, 'feature', iid, {'title': f'Feature {iid}'}, '',
                            '2026-01-02', 'test')
        self._commit(repo, f'record: new feature {iid}')

    def _sync(self):
        from asf.tick import shadow
        lines = []
        moved = shadow.sync_operator_checkout(self.product, out=lines.append)
        return moved, lines

    def _drift(self):
        from asf.tick import shadow
        return shadow.checkout_drift(self.product)

    def _marker(self):
        from asf.tick import shadow
        return os.path.join(self.checkout, '.git', shadow.UNPUSHED_MARKER)

    def test_three_stranded_commits_and_a_moved_origin_end_level_with_index_rederived(self):
        for iid in ('F-0002', 'F-0003', 'F-0004'):
            self._card(self.checkout, iid)
        for iid in ('F-0010', 'F-0011'):
            self._card(self.other, iid)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.other)
        with open(self._marker(), 'w') as f:
            f.write('stranded\n')
        moved, lines = self._sync()
        self.assertTrue(moved)
        self.assertEqual(lines, ['record: pushed 3 stranded commit(s)'])
        self.assertEqual(self._drift(), (0, 0))
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout),
                         _git(['rev-parse', 'main'], self.origin))
        self.assertFalse(os.path.exists(self._marker()))
        self.assertEqual(_git(['status', '--porcelain'], self.checkout), '')
        # index.json is what `asf index` derives from the rebased cards: every card, none lost
        with open(os.path.join(self.checkout, 'index.json'), encoding='utf-8') as f:
            index = f.read()
        for iid in ('F-0001', 'F-0002', 'F-0003', 'F-0004', 'F-0010', 'F-0011'):
            self.assertIn(iid, index)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.do_index(self.checkout), 0)
        self.assertEqual(_git(['status', '--porcelain'], self.checkout), '')

    def test_a_conflict_off_the_index_is_aborted_and_the_checkout_left_as_it_was(self):
        with open(os.path.join(self.checkout, 'README.md'), 'w') as f:
            f.write('mine\n')
        _git(['commit', '-q', '-am', 'local readme'], self.checkout)
        with open(os.path.join(self.other, 'README.md'), 'w') as f:
            f.write('theirs\n')
        _git(['commit', '-q', '-am', 'their readme'], self.other)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.other)
        before = _git(['rev-parse', 'HEAD'], self.checkout)
        moved, lines = self._sync()
        self.assertFalse(moved)
        self.assertEqual(len(lines), 1)
        self.assertIn('conflict with origin/main on README.md', lines[0])
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout), before)
        self.assertEqual(_git(['status', '--porcelain'], self.checkout), '')
        self.assertFalse(os.path.isdir(os.path.join(self.checkout, '.git', 'rebase-merge')))
        self.assertEqual(self._drift(), (1, 1))

    def test_a_dirty_tree_is_named_every_sync_while_it_holds(self):
        """RD4: the trunk's own DIRTY_MARKER used to suppress this on the second call —
        removed, so the refusal is said every tick the tree stays dirty, not only the first."""
        self._card(self.checkout, 'F-0002')
        self._card(self.other, 'F-0010')
        _git(['push', '-q', 'origin', 'HEAD:main'], self.other)
        with open(os.path.join(self.checkout, 'README.md'), 'a') as f:
            f.write('hand edit\n')
        before = _git(['rev-parse', 'HEAD'], self.checkout)
        _moved, first = self._sync()
        _moved, second = self._sync()
        self.assertEqual(len(first), 1)
        self.assertIn('local changes: README.md', first[0])
        self.assertEqual(len(second), 1)
        self.assertIn('local changes: README.md', second[0])
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.checkout), before)
        _git(['checkout', '--', 'README.md'], self.checkout)
        moved, lines = self._sync()
        self.assertTrue(moved)
        self.assertEqual(lines, ['record: pushed 1 stranded commit(s)'])
        self.assertEqual(self._drift(), (0, 0))

    def test_a_refused_publish_leaves_a_marker_the_next_sync_clears(self):
        import argparse
        import contextlib
        from unittest import mock
        from asf.groom.inbox import cmd_inbox
        from asf.record.new import cmd_new
        from asf.tick import shadow
        args = argparse.Namespace(
            type='story', title='A brand new story', parent='F-0001', priority=None, area=None,
            legacy_id=None, body_file=None, force=False, severity=None, signature=None,
            set=None, found_in=None, acceptance=['it works'], writes=None)
        err = io.StringIO()
        with mock.patch.object(shadow, 'push', return_value=False), \
                redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cmd_new(args, self.checkout), 1)
            self.assertEqual(cmd_inbox(argparse.Namespace(title='Loose idea', body_file=None,
                                                          parent=None, product=None),
                                       self.checkout), 1)
        self.assertTrue(os.path.exists(self._marker()))
        with open(self._marker(), encoding='utf-8') as f:
            stranded = f.read().splitlines()
        self.assertEqual(len(stranded), 2)
        self.assertIn(stranded[0], err.getvalue())   # the unpushed commits are named
        self._card(self.other, 'F-0010')
        _git(['push', '-q', 'origin', 'HEAD:main'], self.other)
        moved, lines = self._sync()
        self.assertTrue(moved, lines)
        self.assertEqual(lines, ['record: pushed 2 stranded commit(s)'])
        self.assertFalse(os.path.exists(self._marker()))
        self.assertEqual(self._drift(), (0, 0))


class RecordWordTests(unittest.TestCase):
    """F-0260: after the sync, a checkout still out of step reads ``record behind N`` /
    ``record ahead N`` on the TICK line, never ``record ok``."""

    def test_the_tick_line_names_the_drift(self):
        from asf.tick import summary
        self.assertEqual(summary.digest([{'step': 'record', 'ok': True}], {})[0], 'TICK — record ok')
        self.assertEqual(
            summary.digest([{'step': 'record', 'ok': True,
                             'drift': {'ahead': 0, 'behind': 28}}], {})[0], 'TICK — record behind 28')
        self.assertEqual(
            summary.digest([{'step': 'record', 'ok': True,
                             'drift': {'ahead': 3, 'behind': 9}}], {})[0],
            'TICK — record behind 9 ahead 3')

    def test_finish_marks_the_record_entry_from_the_sync(self):
        from unittest import mock
        ctx = mock.Mock(has_record=True, record_drift=(2, 0))
        ran = [{'step': 'record', 'ok': True}]
        with mock.patch.object(tick, 'file_invariant_bugs'), \
                mock.patch.object(tick, 'write_tick_line'), \
                mock.patch.object(tick, 'commit_and_push', return_value=0):
            self.assertEqual(tick.finish(ctx, ran), 0)
        self.assertEqual(ran[0]['drift'], {'ahead': 2, 'behind': 0})


if __name__ == '__main__':
    unittest.main()
