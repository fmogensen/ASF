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
from asf.record.new import cmd_new


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


if __name__ == '__main__':
    unittest.main()
