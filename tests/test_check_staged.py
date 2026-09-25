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
        self.assertIn('1 pre-existing errors already on HEAD — not blocking', r.stdout)
        c = subprocess.run(['git', 'commit', '-qm', 'x'], cwd=self.root, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(c.returncode, 0, c.stdout + c.stderr)

    def test_a_staged_change_that_leaves_the_index_stale_stages_the_derived_index(self):
        # a retitled decision: no parent's Children, no Backlinks — only index.json derives it
        text = self.read('decisions/D-0001.md')
        self.write('decisions/D-0001.md', text.replace('title: A decision', 'title: Paid plan'))
        self.git(self.root, 'add', 'decisions/D-0001.md')
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('index.json is stale', r.stdout)
        self.assertIn('index.json: regenerated', r.stdout)
        self.assertIn('Paid plan', self.git(self.root, 'show', ':index.json'))
        self.assertIn('Paid plan', self.read('index.json'))   # the working tree follows

    def test_a_hand_edit_commits_with_its_index_regenerated(self):
        text = self.read('decisions/D-0001.md')
        self.write('decisions/D-0001.md', text.replace('title: A decision', 'title: Paid plan'))
        self.git(self.root, 'add', 'decisions/D-0001.md')
        c = subprocess.run(['git', 'commit', '-qm', 'hand edit'], cwd=self.root, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertIn('Paid plan', self.git(self.root, 'show', 'HEAD:index.json'))
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')

    def test_a_real_error_is_still_refused_after_the_index_is_regenerated(self):
        text = self.read('features/F-0001.md')
        self.write('features/F-0001.md', text.replace('title: Free plan', 'title: Paid plan')
                   .replace('## Description\n', '## Description\nsee D1 here\n'))
        self.git(self.root, 'add', 'features/F-0001.md')
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('features/F-0001.md', r.stdout)
        self.assertNotIn('index.json is stale', r.stdout)
        c = subprocess.run(['git', 'commit', '-qm', 'x'], cwd=self.root, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(c.returncode, 0, c.stdout + c.stderr)

    def test_the_standing_error_prints_as_a_warning_with_one_summary_line(self):
        write_item(self.root, 'F-0002', 'feature', 'Second', parent='E-0001')
        self.asf('index')
        self.git(self.root, 'add', 'features/F-0002.md', 'index.json', 'epics/E-0001.md')
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('epics/E-0002.md:', r.stdout)
        self.assertIn(': warning: bare decision reference', r.stdout)
        self.assertIn('1 pre-existing errors already on HEAD — not blocking', r.stdout)

    # -- what a publishing command commits ------------------------------------------------------

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), 'w') as f:
            f.write(text)

    def committed(self, rel):
        return self.git(self.root, 'show', f'HEAD:{rel}')

    def commit_past_the_hook(self, message='seed more'):
        self.git(self.root, 'add', '-A')
        self.git(self.root, 'commit', '-q', '--no-verify', '-m', message)

    def test_new_story_commits_its_parent_children_and_the_index(self):
        before = self.head()
        r = self.asf('new', 'story', '--title', 'A story', '--parent', 'F-0001', '--acceptance', 'x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.head(), before, r.stderr)
        new_id = r.stdout.strip().splitlines()[-1]
        changed = self.git(self.root, 'show', '--name-only', '--format=', 'HEAD').split()
        self.assertIn(f'stories/{new_id}.md', changed)
        self.assertIn('features/F-0001.md', changed)   # its ## Children gained the story
        self.assertIn('index.json', changed)
        self.assertIn(new_id, self.committed('features/F-0001.md'))
        self.assertIn(new_id, self.committed('index.json'))
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.asf('check', '--staged').returncode, 0)

    def test_set_blocked_by_commits_the_targets_backlinks(self):
        r = self.asf('set', 'F-0001', 'blockedBy=D-0001')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        changed = self.git(self.root, 'show', '--name-only', '--format=', 'HEAD').split()
        self.assertIn('decisions/D-0001.md', changed)
        backlinks = self.committed('decisions/D-0001.md').split('## Backlinks', 1)[1]
        self.assertIn('F-0001', backlinks)
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')
        whole = self.asf('check')
        self.assertNotIn('decisions/D-0001.md', whole.stdout)
        self.assertNotIn('index.json', whole.stdout)

    def test_set_refreshes_the_index_past_an_unreadable_card(self):
        self.write('features/F-0009.md', '---\nid: F-0009\nno closing marker\n')
        self.commit_past_the_hook('a broken card')
        before = self.head()
        r = self.asf('set', 'F-0001', 'rank=5')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.head(), before, r.stderr)
        self.assertIn('"rank": 5', self.committed('index.json'))

    def test_an_uncommitted_edit_to_another_card_stays_out_of_the_commit(self):
        epic = self.read('epics/E-0001.md')
        self.write('epics/E-0001.md', epic.replace('title: Factory', 'title: Hand edited'))
        r = self.asf('set', 'F-0001', 'rank=5')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('Hand edited', self.committed('index.json'))
        self.assertNotIn('Hand edited', self.committed('epics/E-0001.md'))
        self.assertIn('"rank": 5', self.committed('index.json'))
        # the hand edit is still there, uncommitted
        self.assertIn('Hand edited', self.read('epics/E-0001.md'))
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), 'M epics/E-0001.md')

    # -- what the staged change breaks elsewhere refuses it --------------------------------------

    def assert_refused(self, *expected):
        r = self.asf('check', '--staged')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        blocking = r.stdout.split(': warning:')[0] if ': warning:' in r.stdout else r.stdout
        for text in expected:
            self.assertIn(text, blocking)
        return r

    def test_deleting_a_parent_is_refused_by_its_child(self):
        self.git(self.root, 'rm', '-q', 'epics/E-0001.md')
        self.assert_refused('features/F-0001.md', 'parent E-0001 does not exist')

    def test_deleting_a_blocked_by_target_is_refused(self):
        feature = self.read('features/F-0001.md')
        self.write('features/F-0001.md', feature.replace(
            '# ---- machine ----', 'blockedBy:\n  - D-0001\n# ---- machine ----'))
        self.asf('index')
        self.commit_past_the_hook('blocked by a decision')
        self.git(self.root, 'rm', '-q', 'decisions/D-0001.md')
        self.assert_refused('blockedBy references missing item D-0001')

    def test_a_bare_string_blocked_by_names_a_missing_target(self):
        # `asf set X blockedBy=D-0001` stores a single scalar, not a one-item list (B-0084); the
        # missing-target check must not walk that string character by character.
        feature = self.read('features/F-0001.md')
        self.write('features/F-0001.md', feature.replace(
            '# ---- machine ----', 'blockedBy: D-9999\n# ---- machine ----'))
        self.git(self.root, 'add', 'features/F-0001.md')
        self.assert_refused('blockedBy references missing item D-9999')

    def test_a_backlink_left_stale_on_an_unstaged_card_is_refused(self):
        feature = self.read('features/F-0001.md')
        self.write('features/F-0001.md', feature.replace(
            '# ---- machine ----', 'blockedBy:\n  - D-0001\n# ---- machine ----'))
        self.git(self.root, 'add', 'features/F-0001.md')
        self.assert_refused('decisions/D-0001.md', '## Backlinks section is stale')

    def test_a_new_duplicate_id_is_refused(self):
        write_item(self.root, 'F-0001', 'story', 'Same id', parent='F-0001')
        self.git(self.root, 'add', 'stories/F-0001.md')
        self.assert_refused('duplicate id F-0001')

    def test_the_staged_blob_is_judged_not_the_working_tree(self):
        good = self.read('features/F-0001.md')
        self.write('features/F-0001.md', good.replace('## Description\n', '## Description\nsee D1\n'))
        self.git(self.root, 'add', 'features/F-0001.md')
        self.write('features/F-0001.md', good)       # fixed on disk, not staged
        self.assert_refused('bare decision reference')
        self.git(self.root, 'add', 'features/F-0001.md')
        self.write('features/F-0001.md', good.replace('## Description\n', '## Description\nsee D1\n'))
        r = self.asf('check', '--staged')             # broken on disk, the staged blob sound
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_the_committing_checkout_is_judged_not_the_configured_record(self):
        other = os.path.join(self.tmp, 'other')
        self.git(self.tmp, 'clone', '-q', self.origin, other)
        with open(os.path.join(other, 'features', 'F-0001.md')) as f:
            text = f.read()
        with open(os.path.join(other, 'features', 'F-0001.md'), 'w') as f:
            f.write(text.replace('## Description\n', '## Description\nsee D1\n'))
        self.git(other, 'add', 'features/F-0001.md')
        code = ('import sys; from asf.record.check import cmd_check_staged; '
                f'sys.exit(cmd_check_staged({self.root!r}))')
        r = subprocess.run([sys.executable, '-c', code], cwd=other, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('bare decision reference', r.stdout)
        # the configured record itself stages nothing
        self.assertEqual(self.asf('check', '--staged').returncode, 0)


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

    def test_only_a_real_whole_record_check_command_is_matched(self):
        for line in ('asf check', 'exec asf check || exit 1', '"$HOME/bin/asf" check --product p',
                     "/x/asf check --product=p; echo done", '  asf check && echo ok'):
            self.assertIsNotNone(hooks.WHOLE_RECORD_CHECK_RE.search(line), line)
        for line in ('asf check-foo', 'echo "asf check"', "echo 'run asf check'", '# asf check',
                     'asf check --deep', 'asf check --invariants', 'asf check --staged',
                     'asf check features/F-0001.md', 'asf check --product p features/F-0001.md',
                     'xargs -0 asf check', 'myasf check'):
            self.assertIsNone(hooks.WHOLE_RECORD_CHECK_RE.search(line), line)

    def test_an_older_init_hook_keeps_its_operator_lines_and_flags(self):
        old = PRE_COMMIT.replace(
            'asf check --staged || exit 1\n',
            'staged="$(git diff --cached --name-only --diff-filter=ACMR)"\n'
            '[ -n "$staged" ] || exit 0\n'
            'echo "$staged" | tr \'\\n\' \'\\0\' | xargs -0 asf check || exit 1\n'
            'echo "asf check is next"  # an operator line\n')
        new = hooks.init_hook_upgrade(old, 'pre-commit')
        self.assertIn('\nasf check --staged || exit 1\n', new)
        self.assertNotIn('xargs -0 asf check', new)
        self.assertIn('echo "asf check is next"  # an operator line\n', new)
        self.assertIsNone(hooks.init_hook_upgrade(new, 'pre-commit'))
        flagged = PRE_COMMIT.replace('asf check --staged', 'asf check --product p') + 'echo mine\n'
        new = hooks.init_hook_upgrade(flagged, 'pre-commit')
        self.assertEqual(new, flagged.replace('asf check --product p', 'asf check --staged --product p'))

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
