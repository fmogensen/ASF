"""The operator's record checkout (``backlog_dir``) follows origin after every tick push.

The tick works in its own clone and pushes; the read views (``asf status``'s Decisions row,
``asf board``, ``asf next``) read ``backlog_dir``. Only a console command run there used to
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

    def test_a_dirty_tree_is_named_once_and_synced_once_clean(self):
        self._card(self.checkout, 'F-0002')
        self._card(self.other, 'F-0010')
        _git(['push', '-q', 'origin', 'HEAD:main'], self.other)
        with open(os.path.join(self.checkout, 'README.md'), 'a') as f:
            f.write('hand edit\n')
        before = _git(['rev-parse', 'HEAD'], self.checkout)
        _moved, first = self._sync()
        _moved, second = self._sync()
        self.assertEqual(len(first), 1)
        self.assertIn('working tree has local changes', first[0])
        self.assertEqual(second, [])
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
