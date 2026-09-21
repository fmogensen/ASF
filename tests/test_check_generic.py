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
        with open(os.path.join(root, 'notes.md'), 'w', encoding='utf-8') as f:
            f.write('the product used to be called botseon internally\n')
        subprocess.run(['git', 'add', 'notes.md'], cwd=root, check=True)
        shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(root, 'tools'))
        r = run_in(root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('notes.md', r.stdout)

    def test_license_file_is_exempt(self):
        root = make_git_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, 'LICENSE'), 'w', encoding='utf-8') as f:
            f.write('a license mentioning nordio would still be exempt\n')
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


if __name__ == '__main__':
    unittest.main()
