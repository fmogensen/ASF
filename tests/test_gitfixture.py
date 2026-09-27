"""tests/test_gitfixture.py — the shared fixture helper disables hooks on every repo it builds
(B-0073): a test that commits inside a fixture must never trigger a real pre-commit hook, or a
hook's own suite would recurse into the test that started it."""
import os
import shutil
import subprocess
import tempfile
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_gitfixture` does not
    from gitfixture import Template, publish
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template, publish


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _build_repo_with_a_real_hook(root):
    """A repo shaped like a record ``asf init`` lays down: ``.githooks/pre-commit`` installed
    and ``core.hooksPath`` pointing at it — exactly what a fixture must override."""
    repo = os.path.join(root, 'repo')
    os.makedirs(os.path.join(repo, '.githooks'))
    _git(['init', '-q', repo], cwd=root)
    _git(['config', 'user.name', 'Test'], cwd=repo)
    _git(['config', 'user.email', 'test@example.com'], cwd=repo)
    hook = os.path.join(repo, '.githooks', 'pre-commit')
    with open(hook, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\nexit 1\n')  # a real hook that always refuses — proof it never ran
    os.chmod(hook, 0o755)
    _git(['config', 'core.hooksPath', '.githooks'], cwd=repo)


class GitFixtureHooksTests(unittest.TestCase):
    def test_b0073_fresh_repos_do_not_carry_the_products_hooks_path(self):
        template = Template(_build_repo_with_a_real_hook, prefix='b0073_test_')
        repo = os.path.join(template.fresh(), 'repo')
        self.assertNotEqual(_git(['config', 'core.hooksPath'], cwd=repo), '.githooks')
        self.assertEqual(_git(['config', 'core.hooksPath'], cwd=repo), '/dev/null')

    def test_b0073_a_fixture_commit_never_runs_the_real_hook(self):
        template = Template(_build_repo_with_a_real_hook, prefix='b0073_test_')
        repo = os.path.join(template.fresh(), 'repo')
        with open(os.path.join(repo, 'file.txt'), 'w', encoding='utf-8') as f:
            f.write('x')
        _git(['add', '-A'], cwd=repo)
        _git(['commit', '-qm', 'a fixture commit'], cwd=repo)  # raises if the hook ran and exit 1'd


def _build_tree_and_origin(root):
    """A tree and the bare origin ``publish`` pushes it to — the shape ``HousekeepingOffTests``
    checks for the writer PD18 names (a fixture's own commit or push starting a detached
    ``gc``/``maintenance`` that races a later copy of the same tree)."""
    tree = os.path.join(root, 'repo')
    os.makedirs(tree)
    with open(os.path.join(tree, 'file.txt'), 'w', encoding='utf-8') as f:
        f.write('x')
    publish(tree, os.path.join(root, 'origin.git'))


class HousekeepingOffTests(unittest.TestCase):
    """PD18: nothing a fixture builds can start git's own background gc or maintenance — the
    writer that raced ``Factory.fork``'s copy on the round-1 pull request."""

    def _assert_off(self, repo):
        for key, value in (('gc.auto', '0'), ('gc.autoDetach', 'false'),
                           ('maintenance.auto', 'false')):
            self.assertEqual(_git(['config', key], cwd=repo), value, f'{repo} {key}')

    def test_publish_turns_housekeeping_off_on_the_tree_and_its_bare_origin(self):
        tmp = tempfile.mkdtemp(prefix='housekeeping_test_')
        self.addCleanup(shutil.rmtree, tmp, True)
        _build_tree_and_origin(tmp)
        self._assert_off(os.path.join(tmp, 'repo'))
        self._assert_off(os.path.join(tmp, 'origin.git'))

    def test_template_fresh_copy_turns_housekeeping_off_on_every_repo_under_it(self):
        template = Template(_build_tree_and_origin, prefix='housekeeping_test_')
        copy_root = template.fresh()
        self._assert_off(os.path.join(copy_root, 'repo'))
        self._assert_off(os.path.join(copy_root, 'origin.git'))


if __name__ == '__main__':
    unittest.main()
