"""B-0091: `asf new` / `asf inbox` commit the card they file and push it, so it reaches the tick."""
import argparse
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest

from asf.groom.inbox import cmd_inbox
from asf.record import frontmatter
from asf.record.ids import write_new_item
from asf.record.new import _parse_sets, cmd_new


def git(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class NewPublishesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='newpub_')
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.root = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@x')):
            git(self.root, 'config', k, v)
        write_new_item(self.root, {}, 'feature', 'F-0001', {'title': 'Parent feature'}, '',
                       '2026-01-01', 'seed')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'seed')
        git(self.root, 'push', '-q', 'origin', 'HEAD:main')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, **kw):
        base = dict(type='story', title='A brand new story', parent='F-0001', priority=None, area=None,
                    legacy_id=None, body_file=None, force=False, severity=None, signature=None,
                    set=None, found_in=None, acceptance=['it works'], writes=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_new_and_inbox_commit_and_push_the_card(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_new(self._args(), self.root), 0)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), git(self.origin, 'rev-parse', 'main'))
        self.assertIn('Signed-off-by', git(self.root, 'log', '-1', '--format=%B'))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_inbox(argparse.Namespace(title='Loose idea', body_file=None, parent=None,
                                              product=None), self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), git(self.origin, 'rev-parse', 'main'))

    def test_a_refused_commit_leaves_nothing_of_itself_in_the_checkout(self):
        """F-0282: the pre-commit refuses — the card the command created is removed and the card
        it edited is back to HEAD, so no later record write sweeps either up."""
        from asf.record import publish
        hooks = os.path.join(self.tmp, 'hooks')
        os.makedirs(hooks)
        with open(os.path.join(hooks, 'pre-commit'), 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\necho refused >&2\nexit 1\n')
        os.chmod(os.path.join(hooks, 'pre-commit'), 0o755)
        git(self.root, 'config', 'core.hooksPath', hooks)
        write_new_item(self.root, {}, 'story', 'S-0002', {'title': 'New', 'parent': 'F-0001'},
                       '', '2026-01-01', 'test')
        parent = os.path.join(self.root, 'features', 'F-0001.md')
        with open(parent, 'a', encoding='utf-8') as f:
            f.write('an edit the command made\n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(subprocess.CalledProcessError):
                publish.commit_paths(self.root, ['stories/S-0002.md', 'features/F-0001.md'],
                                     'record: new story S-0002')
        self.assertFalse(os.path.exists(os.path.join(self.root, 'stories', 'S-0002.md')))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertIn('put back', err.getvalue())

    def test_in_progress_flag_sets_in_progress_by(self):
        """B-0070: `asf new --in-progress operator` stamps the card so the lane does not
        duplicate a fix the operator is already carrying by hand."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_new(self._args(in_progress='operator'), self.root), 0)
        new_id = out.getvalue().strip()
        with open(os.path.join(self.root, 'stories', f'{new_id}.md'), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read())
        self.assertEqual(meta['in_progress_by'], 'operator')

    def test_no_in_progress_flag_leaves_the_field_out(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_new(self._args(), self.root), 0)
        new_id = out.getvalue().strip()
        with open(os.path.join(self.root, 'stories', f'{new_id}.md'), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read())
        self.assertNotIn('in_progress_by', meta)

    def test_write_new_item_state_keyword(self):
        write_new_item(self.root, {}, 'decision', 'D-0001', {'title': 'A decision'}, '',
                       '2026-01-01', 'seed', state='Closed')
        with open(os.path.join(self.root, 'decisions', 'D-0001.md'), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read())
        self.assertEqual(meta['state'], 'Closed')
        with open(os.path.join(self.root, 'features', 'F-0001.md'), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read())
        self.assertEqual(meta['state'], 'New')


class StabilityIsSettableOnFeatureAndEpic(unittest.TestCase):
    """S-80305's last bullet, Task 1's own: `stability` is settable on a Feature and an Epic,
    parses `true`/`false` as a boolean, and a bad value is refused before any card is written.

    `cmd_new` refuses type feature/epic/bug outright — "it enters through the inbox" — before
    `_parse_sets` ever runs (that guard predates this Task and is unrelated to stability), so
    `asf new --set stability=yes` can never reach a Feature or an Epic card to prove the
    refusal there. The refusal is proven instead at `_parse_sets`, the function `cmd_new` calls
    before writing any card for the types it does reach, and the one `asf set` already drives
    for an existing Feature/Epic card (tests/test_backlog.py)."""

    def test_stability_true_and_false_parse_as_a_boolean_on_both_types(self):
        for type_ in ('feature', 'epic'):
            self.assertEqual(_parse_sets(type_, ['stability=true']), [('stability', None, True)])
            self.assertEqual(_parse_sets(type_, ['stability=false']), [('stability', None, False)])

    def test_stability_yes_is_refused_before_any_card_is_touched(self):
        for type_ in ('feature', 'epic'):
            with self.assertRaises(ValueError) as cm:
                _parse_sets(type_, ['stability=yes'])
            self.assertIn('one of true, false', str(cm.exception))

    def test_asf_new_cannot_reach_a_feature_or_epic_card_at_all(self):
        # the pre-existing inbox guard, not this Task's: cmd_new returns before _parse_sets
        # runs or any folder is touched, for stability's good value as much as its bad one
        root = tempfile.mkdtemp(prefix='newpub_stability_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for type_ in ('feature', 'epic'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = cmd_new(argparse.Namespace(
                    type=type_, title='X', parent=None, priority=None, area=None,
                    legacy_id=None, body_file=None, force=False, set=['stability=true'],
                    in_progress=None, acceptance=None, writes=None), root)
            self.assertEqual(rc, 2)
            self.assertIn('enters through the inbox', out.getvalue())
            self.assertEqual(os.listdir(root), [])


if __name__ == '__main__':
    unittest.main()
