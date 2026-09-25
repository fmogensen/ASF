"""`asf check --staged` — the record pre-commit's mode: an error in a card nobody touched is the
record's standing debt (a warning), never a reason to refuse `asf set` / `asf inbox` on another."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import hermetic, hooks, init
from asf.init import PRE_COMMIT, STREAM_FOLDERS

from tests.test_backlog import FOLDERS, REPO_ROOT, write_item

BARE_REF_BODY = (
    "## Description\nas decided in D1, the thing\n\n"
    "## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n- 2026-01-01: created\n\n"
    "## Children\n\n## Backlinks\n"
)


class StagedCheckTests(unittest.TestCase):
    HOOK = PRE_COMMIT

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='staged_check_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = os.path.join(self.tmp, 'bin')
        os.makedirs(bindir)
        with open(os.path.join(bindir, 'asf'), 'w') as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} -m asf.cli "$@"\n')
        os.chmod(os.path.join(bindir, 'asf'), 0o755)
        self.env = hermetic.build()
        self.env['PATH'] = bindir + os.pathsep + self.env['PATH']
        self.env['PYTHONPATH'] = REPO_ROOT + os.pathsep + self.env.get('PYTHONPATH', '')
        self.env['ASF_HOME'] = os.path.join(self.tmp, 'asf-home')
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.root = os.path.join(self.tmp, 'record')
        self.git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        self.git(self.tmp, 'clone', '-q', self.origin, self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@x')):
            self.git(self.root, 'config', k, v)
        for folder in FOLDERS + list(STREAM_FOLDERS):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
            open(os.path.join(self.root, folder, '.keep'), 'w').close()
        hooks_dir = os.path.join(self.root, '.githooks')
        os.makedirs(hooks_dir)
        with open(os.path.join(hooks_dir, 'pre-commit'), 'w') as f:
            f.write(self.HOOK)
        os.chmod(os.path.join(hooks_dir, 'pre-commit'), 0o755)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0001')
        write_item(self.root, 'D-0001', 'decision', 'A decision')
        # the standing debt: a bare decision reference in a card nobody will touch
        write_item(self.root, 'E-0002', 'epic', 'Old epic', body=BARE_REF_BODY)
        self.asf('index')
        self.git(self.root, 'add', '-A')
        self.git(self.root, 'commit', '-qm', 'seed')     # before the hook is switched on
        self.git(self.root, 'push', '-q', 'origin', 'HEAD:main')
        self.git(self.root, 'config', 'core.hooksPath', '.githooks')
        whole = self.asf('check')
        self.assertEqual(whole.returncode, 1, whole.stdout)   # the record does carry an error
        self.assertIn('epics/E-0002.md', whole.stdout)

    def git(self, cwd, *a):
        return subprocess.run(['git', *a], cwd=cwd, env=self.env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def asf(self, *a):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *a], cwd=self.root, env=self.env,
                              capture_output=True, text=True)

    def head(self):
        return self.git(self.root, 'rev-parse', 'HEAD')

    def test_set_on_another_card_commits_past_a_standing_error(self):
        before = self.head()
        r = self.asf('set', 'F-0001', 'rank=5')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.head(), before, r.stderr)
        changed = self.git(self.root, 'show', '--name-only', '--format=', 'HEAD').split()
        self.assertIn('features/F-0001.md', changed)
        self.assertIn('index.json', changed)       # set refreshed the index itself
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.asf('check', 'features/F-0001.md').returncode, 0)

    def test_inbox_commits_past_a_standing_error(self):
        body = os.path.join(self.tmp, 'body.md')
        with open(body, 'w') as f:
            f.write('it flakes\n')
        before = self.head()
        r = self.asf('inbox', '--title', 'A flaky spec', '--body-file', body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.head(), before)
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')

    def test_a_staged_file_with_an_error_is_refused(self):
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            text = f.read()
        with open(os.path.join(self.root, 'features', 'F-0001.md'), 'w') as f:
            f.write(text.replace('## Description\n', '## Description\nsee D1 here\n'))
        self.git(self.root, 'add', 'features/F-0001.md')
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('features/F-0001.md', r.stdout)
        self.assertIn('1 pre-existing errors in files not staged — not blocking', r.stdout)
        c = subprocess.run(['git', 'commit', '-qm', 'x'], cwd=self.root, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(c.returncode, 0, c.stdout + c.stderr)

    def test_a_staged_change_that_leaves_the_index_stale_is_refused(self):
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            text = f.read()
        with open(os.path.join(self.root, 'features', 'F-0001.md'), 'w') as f:
            f.write(text.replace('title: Free plan', 'title: Paid plan'))
        self.git(self.root, 'add', 'features/F-0001.md')
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('index.json:1: index.json is stale', r.stdout)

    def test_the_standing_error_prints_as_a_warning_with_one_summary_line(self):
        write_item(self.root, 'F-0002', 'feature', 'Second', parent='E-0001')
        self.asf('index')
        self.git(self.root, 'add', 'features/F-0002.md', 'index.json', 'epics/E-0001.md')
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('epics/E-0002.md:', r.stdout)
        self.assertIn(': warning: bare decision reference', r.stdout)
        self.assertIn('1 pre-existing errors in files not staged — not blocking', r.stdout)


class HandWrittenHookStagedCheckTests(StagedCheckTests):
    """A record whose own pre-commit ran `asf check` over the whole record, as rewritten by
    `asf hooks install`."""
    HOOK = hooks.staged_check_upgrade(
        '#!/bin/sh\nset -e\nasf redact --pre-commit\nasf check\n', 'pre-commit')


class StagedHookInstallTests(unittest.TestCase):
    def test_the_generated_hook_carries_the_staged_flag(self):
        self.assertIn('asf check --staged', PRE_COMMIT)
        self.assertIsNone(hooks.WHOLE_RECORD_CHECK_RE.search(PRE_COMMIT))

    def test_an_older_init_hook_is_rewritten_to_the_staged_form(self):
        old = PRE_COMMIT.replace('asf check --staged',
                                 'echo "$staged" | tr \'\\n\' \'\\0\' | xargs -0 asf check')
        self.assertEqual(hooks.init_hook_upgrade(old, 'pre-commit'), init.PRE_COMMIT)
        self.assertIsNone(hooks.init_hook_upgrade(init.PRE_COMMIT, 'pre-commit'))

    def test_a_hand_written_record_hook_gains_the_flag_once(self):
        text = ('#!/bin/sh\nset -e\n'
                '"$HOME/.local/bin/asf" redact --pre-commit --product p\n'
                '"$HOME/.local/bin/asf" check --product p\n')
        new = hooks.staged_check_upgrade(text, 'pre-commit')
        self.assertIn('"$HOME/.local/bin/asf" check --staged --product p\n', new)
        self.assertIn('redact --pre-commit --product p\n', new)
        self.assertIsNone(hooks.staged_check_upgrade(new, 'pre-commit'))
        self.assertIsNone(hooks.staged_check_upgrade(text, 'pre-push'))

    def test_hooks_install_rewrites_the_record_pre_commit_idempotently(self):
        tmp = tempfile.mkdtemp(prefix='staged_hook_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(['git', 'init', '-q', tmp], check=True, env=hermetic.build())
        hooks_dir = hooks.git_hooks_dir(tmp)
        os.makedirs(hooks_dir, exist_ok=True)
        path = os.path.join(hooks_dir, 'pre-commit')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n"/x/asf" redact --pre-commit --product p\n"/x/asf" check --product p\n')

        class P:
            name = 'p'
            repo_dir = None
            backlog_dir = tmp
        ok, detail = hooks.ensure_git_hooks(P, which=lambda _n: '/x/asf')
        self.assertTrue(ok, detail)
        with open(path) as f:
            once = f.read()
        self.assertIn('"/x/asf" check --staged --product p', once)
        hooks.ensure_git_hooks(P, which=lambda _n: '/x/asf')
        with open(path) as f:
            self.assertEqual(f.read(), once)


if __name__ == '__main__':
    unittest.main()
