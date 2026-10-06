import os
import subprocess
import tempfile
import unittest

from asf import docs
from asf.conventions import Conventions


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class LinkTests(unittest.TestCase):
    def test_a_page_relative_target_resolves_against_its_own_directory_not_the_root(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', '[b](b.md)\n')
            _write(root, 'docs/guide/b.md', '# B\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md', 'docs/guide/b.md']})
            self.assertEqual(docs.check(root, conv), [])

    def test_a_missing_relative_target_is_one_complaint_at_the_carrying_line(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', 'text\n\n[gone](missing.md)\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md']})
            self.assertEqual(docs.check(root, conv),
                             [('docs/guide/a.md', 3, 'missing.md: no such file')])

    def test_a_missing_page_itself_is_one_complaint(self):
        with tempfile.TemporaryDirectory() as root:
            conv = Conventions.from_mapping({'doc_pages': ['docs/nope.md']})
            self.assertEqual(docs.check(root, conv), [('docs/nope.md', 0, 'page does not exist')])

    def test_a_url_an_absolute_a_home_a_var_and_a_bare_anchor_are_all_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'README.md',
                  '[u](https://example.com/x)\n'
                  '[a](/etc/passwd)\n'
                  '[h](~/notes.md)\n'
                  '[v]($HOME/notes.md)\n'
                  '[frag](#somewhere)\n')
            conv = Conventions.from_mapping({'doc_pages': ['README.md']})
            self.assertEqual(docs.check(root, conv), [])

    def test_an_anchor_is_matched_under_the_github_slug_rule_pinned_case(self):
        text = '### `/asf:status` — FACTORY STATUS\n'
        self.assertEqual(docs.anchors(text), ['asfstatus--factory-status'])
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', text + '\n[s](#asfstatus--factory-status)\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md']})
            # the fragment is bare (same-page `#anchor`) and is skipped outright, not checked
            self.assertEqual(docs.check(root, conv), [])

    def test_a_fragment_on_the_page_itself_named_explicitly_resolves_against_its_own_anchors(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', '# Hello\n\n[self](a.md#hello)\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md']})
            self.assertEqual(docs.check(root, conv), [])

    def test_a_cross_page_fragment_that_does_not_match_is_a_complaint(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', '[s](b.md#nope)\n')
            _write(root, 'docs/guide/b.md', '# Hello\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md', 'docs/guide/b.md']})
            self.assertEqual(docs.check(root, conv),
                             [('docs/guide/a.md', 1, 'b.md#nope: no such anchor')])

    def test_inline_code_holding_a_slash_is_never_a_complaint(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', 'see `[x](y/z.md)` in prose\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md']})
            self.assertEqual(docs.check(root, conv), [])

    def test_a_glob_page_pattern_expands_against_the_worktree(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, 'docs/guide/a.md', '# A\n')
            _write(root, 'docs/guide/b.md', '# B\n')
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/*.md']})
            self.assertEqual(sorted(docs.pages(conv, root)),
                             ['docs/guide/a.md', 'docs/guide/b.md'])
            self.assertEqual(docs.check(root, conv), [])

    def test_a_glob_pattern_matching_nothing_is_silent_not_a_complaint(self):
        with tempfile.TemporaryDirectory() as root:
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/*.md']})
            self.assertEqual(docs.pages(conv, root), [])
            self.assertEqual(docs.check(root, conv), [])


class TreeTests(unittest.TestCase):
    def _git(self, repo, *args):
        subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True)

    def _init_repo(self, root):
        self._git(root, 'init', '-q')
        self._git(root, 'config', 'user.email', 'a@b.c')
        self._git(root, 'config', 'user.name', 'a')

    def _commit(self, root):
        self._git(root, 'add', '-A')
        self._git(root, 'commit', '-q', '-m', 'x')
        return subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_the_tree_form_reports_the_same_complaints_as_the_worktree_form(self):
        with tempfile.TemporaryDirectory() as root:
            self._init_repo(root)
            _write(root, 'docs/guide/a.md', '[b](b.md)\n\n[gone](nope.md)\n')
            _write(root, 'docs/guide/b.md', '# B\n')
            sha = self._commit(root)
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md', 'docs/guide/b.md']})
            worktree = docs.check(root, conv)
            tree = docs.check_at(root, sha, conv)
            self.assertEqual(worktree, tree)
            self.assertEqual(tree, [('docs/guide/a.md', 3, 'nope.md: no such file')])

    def test_a_glob_page_pattern_expands_against_the_tree_not_the_worktree(self):
        with tempfile.TemporaryDirectory() as root:
            self._init_repo(root)
            _write(root, 'docs/guide/a.md', '# A\n')
            _write(root, 'docs/guide/b.md', '# B\n')
            sha = self._commit(root)
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/*.md']})
            self.assertEqual(docs.check_at(root, sha, conv), [])

    def test_the_tree_form_opens_no_working_tree(self):
        with tempfile.TemporaryDirectory() as root:
            self._init_repo(root)
            _write(root, 'docs/guide/a.md', '# A\n')
            sha = self._commit(root)
            os.remove(os.path.join(root, 'docs', 'guide', 'a.md'))
            conv = Conventions.from_mapping({'doc_pages': ['docs/guide/a.md']})
            self.assertEqual(docs.check_at(root, sha, conv), [])
            self.assertEqual(docs.check(root, conv),
                             [('docs/guide/a.md', 0, 'page does not exist')])

    def test_an_unreadable_tree_returns_none_not_a_clean_list(self):
        with tempfile.TemporaryDirectory() as root:
            self._init_repo(root)
            _write(root, 'README.md', '# Hi\n')
            self._commit(root)
            conv = Conventions()
            self.assertIsNone(docs.check_at(root, 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef', conv))
            self.assertIsNone(docs.check_at(os.path.join(root, 'no-such-repo'), 'HEAD', conv))


if __name__ == '__main__':
    unittest.main()
