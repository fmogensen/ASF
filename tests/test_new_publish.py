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


if __name__ == '__main__':
    unittest.main()
