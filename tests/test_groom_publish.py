"""F-0133 §1: `asf groom` commits and pushes what it wrote (:mod:`asf.record.publish`), on
the console as well as on the tick. On `tests/test_new_publish.py`'s bare-origin shape (:21-30),
with the fuller record seed a real groom pass needs and the product-less call `tests/test_groom.py`
uses (PD16)."""
import argparse
import contextlib
import datetime
import filecmp
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.groom import groom
from asf.record.core import ITEM_FOLDERS
from asf.record.ids import write_new_item
from tests.test_groom import make_repo


def git(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')


class GroomPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='groompub_')
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.root = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@x')):
            git(self.root, 'config', k, v)
        for folder in ITEM_FOLDERS:
            os.makedirs(os.path.join(self.root, folder))
        os.makedirs(os.path.join(self.root, 'inbox'))
        with open(os.path.join(self.root, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump({'generated': '', 'items': {}}, f)
        write_new_item(self.root, {}, 'epic', 'E-0001', {'title': 'Factory', 'decided': True}, '',
                       '2026-01-01', 'seed')
        with open(os.path.join(self.root, 'NOTES.md'), 'w', encoding='utf-8') as f:
            f.write('operator notes\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'seed')
        git(self.root, 'push', '-q', 'origin', 'HEAD:main')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, **kw):
        base = dict(date=None, apply=False, product=None, default_bug_epic=None,
                    answers_file=None, event=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _write_inbox(self, name, text):
        with open(os.path.join(self.root, 'inbox', name), 'w', encoding='utf-8') as f:
            f.write(text)

    def groom(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(env, 'load_product', side_effect=env.ConfigError('none')):
            return groom.cmd_groom(self._args(**kw), self.root)

    def test_the_groom_commits_and_pushes_what_it_wrote(self):
        self._write_inbox('a.md', '# Checkout is broken\nsignature: pay\n\nCustomers cannot pay.\n')
        self.assertEqual(self.groom(default_bug_epic='E-0001'), 0)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(head, git(self.origin, 'rev-parse', 'main'))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        committed = git(self.root, 'show', '--name-only', '--format=', 'HEAD').splitlines()
        self.assertIn(f'groom/{today()}.md', committed)
        self.assertIn('bugs/B-0001.md', committed)

    def test_the_commit_names_the_groom_and_its_date_and_is_signed_off(self):
        date = today()

        def subject():
            return git(self.root, 'log', '-1', '--format=%s')

        def body():
            return git(self.root, 'log', '-1', '--format=%B')

        self._write_inbox('a.md', '# Thing one\nsignature: one\n\nOne.\n')
        self.assertEqual(self.groom(), 0)
        self.assertEqual(subject(), f'groom: {date}')
        self.assertIn('Signed-off-by', body())

        self._write_inbox('b.md', '# Thing two\nsignature: two\n\nTwo.\n')
        self.assertEqual(self.groom(apply=True), 0)
        self.assertEqual(subject(), f'groom: {date} --apply')
        self.assertIn('Signed-off-by', body())

        self._write_inbox('c.md', '# Thing three\nsignature: three\n\nThree.\n')
        self.assertEqual(self.groom(incremental=True), 0)
        self.assertEqual(subject(), f'groom: {date} (tick)')
        self.assertIn('Signed-off-by', body())

        answers_path = os.path.join(self.root, 'groom', f'{date}.answers')
        os.makedirs(os.path.dirname(answers_path), exist_ok=True)
        with open(answers_path, 'w', encoding='utf-8') as f:
            f.write('- [ ] E-0001 Factory — something → answer: ____\n')
        self.assertEqual(self.groom(answers_file=answers_path), 0)
        self.assertEqual(subject(), f'groom: {date} (answers)')
        self.assertIn('Signed-off-by', body())

    def test_only_the_groom_s_own_writes_are_committed(self):
        notes_path = os.path.join(self.root, 'NOTES.md')
        with open(notes_path, 'a', encoding='utf-8') as f:
            f.write('a note the operator is mid-edit on\n')
        self._write_inbox('a.md', '# Checkout is broken\nsignature: pay\n\nCustomers cannot pay.\n')
        self.assertEqual(self.groom(default_bug_epic='E-0001'), 0)
        status = git(self.root, 'status', '--porcelain', 'NOTES.md')
        self.assertIn('NOTES.md', status)
        committed = git(self.root, 'show', '--name-only', '--format=', 'HEAD').splitlines()
        self.assertNotIn('NOTES.md', committed)

    def test_a_record_that_is_not_a_checkout_is_left_alone(self):
        wrapped_root = make_repo()
        direct_root = make_repo()
        try:
            for root in (wrapped_root, direct_root):
                with open(os.path.join(root, 'inbox', 'a.md'), 'w', encoding='utf-8') as f:
                    f.write('# Checkout is broken\nsignature: pay\n\nCustomers cannot pay.\n')
            args = self._args()
            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(env, 'load_product', side_effect=env.ConfigError('none')):
                wrapped_rc = groom.cmd_groom(args, wrapped_root)
                direct_rc = groom._groom(self._args(), direct_root)
            self.assertEqual(wrapped_rc, direct_rc)
            cmp = filecmp.dircmp(wrapped_root, direct_root)
            self.assertEqual(cmp.diff_files, [])
            self.assertEqual(cmp.left_only, [])
            self.assertEqual(cmp.right_only, [])
            self.assertFalse(os.path.isdir(os.path.join(wrapped_root, '.git')))
        finally:
            shutil.rmtree(wrapped_root, ignore_errors=True)
            shutil.rmtree(direct_root, ignore_errors=True)

    def test_a_groom_that_changed_nothing_makes_no_commit(self):
        self.assertEqual(self.groom(), 0)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(self.groom(), 0)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_parse_errors_still_return_1_and_commit_nothing(self):
        with open(os.path.join(self.root, 'tasks', 'T-9999.md'), 'w', encoding='utf-8') as f:
            f.write('not frontmatter at all\n')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(self.groom(), 1)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), git(self.origin, 'rev-parse', 'main'))


if __name__ == '__main__':
    unittest.main()
