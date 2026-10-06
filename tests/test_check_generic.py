import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_SCRIPT = os.path.join(REPO_ROOT, 'tools', 'check_generic.sh')
PATTERNS_FILE = os.path.join(REPO_ROOT, 'tools', 'forbidden-names.txt')


def run_in(repo):
    return subprocess.run(['bash', CHECK_SCRIPT], cwd=repo, capture_output=True, text=True)


def make_git_repo():
    root = tempfile.mkdtemp(prefix='check_generic_test_')
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'test'], cwd=root, check=True)
    return root


class CheckGenericTests(unittest.TestCase):
    def test_the_real_repo_is_clean(self):
        r = subprocess.run(['bash', CHECK_SCRIPT], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('clean', r.stdout)

    def test_forbidden_word_in_a_tracked_file_fails(self):
        root = make_git_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        # Built from parts so this file's own tracked source never contains the literal forbidden
        # word — otherwise this test would trip check_generic.sh against itself.
        forbidden_word = 'bots' + 'eon'
        with open(os.path.join(root, 'notes.md'), 'w', encoding='utf-8') as f:
            f.write(f'the product used to be called {forbidden_word} internally\n')
        subprocess.run(['git', 'add', 'notes.md'], cwd=root, check=True)
        shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(root, 'tools'))
        r = run_in(root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('notes.md', r.stdout)

    def test_license_file_is_exempt(self):
        root = make_git_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        forbidden_word = 'nor' + 'dio'
        with open(os.path.join(root, 'LICENSE'), 'w', encoding='utf-8') as f:
            f.write(f'a license mentioning {forbidden_word} would still be exempt\n')
        subprocess.run(['git', 'add', 'LICENSE'], cwd=root, check=True)
        shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(root, 'tools'))
        r = run_in(root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_clean_repo_passes(self):
        root = make_git_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, 'notes.md'), 'w', encoding='utf-8') as f:
            f.write('a perfectly generic note\n')
        subprocess.run(['git', 'add', 'notes.md'], cwd=root, check=True)
        shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(root, 'tools'))
        r = run_in(root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_finding_prints_file_and_line_not_the_word(self):
        root = make_git_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        # Built from parts so this file's own tracked source never contains the literal word.
        forbidden_word = 'bots' + 'eon'
        with open(os.path.join(root, 'notes.md'), 'w', encoding='utf-8') as f:
            f.write(f'first line\nthe product used to be called {forbidden_word} internally\n')
        subprocess.run(['git', 'add', 'notes.md'], cwd=root, check=True)
        shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(root, 'tools'))
        r = run_in(root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('notes.md:2: name (tools/forbidden-names.txt)', r.stdout)
        self.assertNotIn(forbidden_word, r.stdout)

    def test_extra_private_list_is_honored(self):
        root = make_git_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, 'notes.md'), 'w', encoding='utf-8') as f:
            f.write('mentions a private codename here\n')
        subprocess.run(['git', 'add', 'notes.md'], cwd=root, check=True)
        shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(root, 'tools'))
        extra = os.path.join(root, 'extra.txt')
        with open(extra, 'w', encoding='utf-8') as f:
            f.write('\\bcodename\\b\n')
        r = subprocess.run(['bash', CHECK_SCRIPT, '--extra', extra], cwd=root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('notes.md', r.stdout)


def _public_patterns():
    import re
    from asf import redact
    with open(PATTERNS_FILE, encoding='utf-8') as f:
        return [re.compile(t, re.IGNORECASE) for t in redact._names_from_lines(f.read())]


class ReleaseAuditPatternTests(unittest.TestCase):
    """The genericity audit's section B: the private details check_generic used to miss. Every
    sample is built from parts, so this file never carries the literal it tests."""

    def hits(self, line):
        return [p.pattern for p in _public_patterns() if p.search(line)]

    def test_each_private_detail_is_caught(self):
        samples = [
            'refresh via ' + 'c' + 'ux usage refresh',                 # an account tool's name
            'the lock at ~/.' + 'c' + 'ux/.lock',                     # its home directory
            'see https://claude.ai/code/' + 'artifact/2564648b-4a57',  # a private artifact link
            'see https://claude.ai/' + 'artifact/abc',
            'see https://claude.ai/' + 'chat/abc',
            'mail op' + '@' + 'gmail.com for access',                  # a consumer mailbox
            'mail op' + '@' + 'proton' + 'mail.com',
            'cd /' + 'Users/' + 'alice/Code/repo',                     # an operator home
            'cd /' + 'home/' + 'alice/src',
            "PATH='/opt/" + "homebrew/bin:'",                         # a package manager prefix
        ]
        for line in samples:
            with self.subTest(line=line):
                self.assertTrue(self.hits(line), line)

    def test_placeholders_and_generic_text_pass(self):
        clean = [
            'path = /Users/operator/Library/LaunchAgents/asf.sample.record.plist',
            'file:///Users/someone/x', '/usr/bin/python3 /Users/x/.local/bin/asf tick',
            '/home/someone/.local/bin/claude', "<tmp>/home/state/sample",
            'https://claude.ai/code/session_01Xy',     # the cloud runtime's own link template
            'account_lock.path', 'quota_guards.reclaim_cux_lock', 'an.operator@example.invalid',
            '/usr/local/bin/asf hook approvals',
        ]
        for line in clean:
            with self.subTest(line=line):
                self.assertEqual(self.hits(line), [], line)


if __name__ == '__main__':
    unittest.main()
