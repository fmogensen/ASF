"""tests.test_product_checks — G2 ask 3: the harvest runs the product's own ``check_commands``
itself (:mod:`asf.harvest.product_checks`, :func:`asf.harvest.lane.run_check_commands`) and a
review/correct brief attaches the result (:func:`asf.briefs.build.checks_section`) — hermetic, a
real small git repo as the one checkout every check runs against.
"""
import importlib
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.env import Product
from asf.harvest import lane, product_checks

# ``asf.briefs.build`` is both the package's entry function and a submodule; the function wins
# the attribute lookup, so the module is asked for by name (as tests.test_briefs does).
build_mod = importlib.import_module('asf.briefs.build')


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='product_checks_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        sh(['git', 'init', '-q', '-b', 'main'], self.repo)
        sh(['git', 'config', 'user.email', 'x@example.com'], self.repo)
        sh(['git', 'config', 'user.name', 'x'], self.repo)
        with open(os.path.join(self.repo, 'f.txt'), 'w', encoding='utf-8') as f:
            f.write('one\n')
        sh(['git', 'add', '-A'], self.repo)
        sh(['git', 'commit', '-q', '-m', 'init'], self.repo)
        self.sha = sh(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip()
        self.state_dir = os.path.join(self.tmp, 'state')
        os.makedirs(self.state_dir)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')

    def tearDown(self):
        env.ASF_HOME = self._orig_home

    def product(self, **conv):
        base = {'check_commands': ['echo ok', 'false']}
        base.update(conv)
        return Product('sample', {'repo_dir': self.repo, 'main': 'main', 'conventions': base})


class RunCheckCommandsTests(RepoCase):
    """:func:`asf.harvest.lane.run_check_commands` — every command runs, pass or fail, in one
    throwaway detached checkout."""

    def test_every_command_runs_and_the_tail_is_captured(self):
        results, line = lane.run_check_commands(
            self.repo, self.state_dir, self.sha, ['echo one', 'false', 'echo two'])
        self.assertEqual(line, '')
        self.assertEqual([r['command'] for r in results], ['echo one', 'false', 'echo two'])
        self.assertEqual((results[0]['rc'], results[0]['tail']), (0, ['one']))
        self.assertEqual(results[1]['rc'], 1)
        self.assertEqual(results[2]['rc'], 0)

    def test_a_failing_setup_means_no_checkout_was_judged(self):
        results, line = lane.run_check_commands(
            self.repo, self.state_dir, self.sha, ['echo should-not-run'], setup='exit 3')
        self.assertIsNone(results)
        self.assertIn('exit 3', line)

    def test_no_commands_is_a_no_op(self):
        self.assertEqual(lane.run_check_commands(self.repo, self.state_dir, self.sha, []), ([], ''))

    def test_the_throwaway_worktree_is_removed_after(self):
        lane.run_check_commands(self.repo, self.state_dir, self.sha, ['echo hi'])
        holder = os.path.join(self.state_dir, lane.CHECK_COMMANDS_DIR)
        self.assertEqual(os.listdir(holder), [])
        r = sh(['git', 'worktree', 'list'], self.repo)
        self.assertEqual(r.stdout.count('\n'), 1)  # only the repo's own checkout is listed


def _run_in_place(product, sha, k, state_dir=None):
    """A ``spawn_fn`` that runs the job synchronously — the tests judge its result, not a real
    background process (mirrors ``tests.test_lane``'s own ``_check_in_place``)."""
    product_checks.run_job(product, sha, k, out=lambda _l: None, state_dir=state_dir)
    return 0


class ProductChecksTests(RepoCase):
    def test_ensure_runs_in_the_background_and_reads_the_cached_result(self):
        p = self.product()
        commands = p.conventions.get('check_commands')
        results = product_checks.ensure(p, self.repo, self.state_dir, self.sha, commands,
                                        spawn_fn=_run_in_place)
        self.assertEqual([r['command'] for r in results], commands)
        self.assertEqual((results[0]['rc'], results[1]['rc']), (0, 1))

    def test_a_cache_hit_never_spawns_again(self):
        p = self.product()
        commands = p.conventions.get('check_commands')
        first = product_checks.ensure(p, self.repo, self.state_dir, self.sha, commands,
                                      spawn_fn=_run_in_place)
        spawned = []
        second = product_checks.ensure(
            p, self.repo, self.state_dir, self.sha, commands,
            spawn_fn=lambda *a, **k: spawned.append(1))
        self.assertEqual(second, first)
        self.assertEqual(spawned, [])

    def test_a_new_tree_is_a_new_key(self):
        commands = ['echo ok']
        k1 = product_checks.key(self.repo, self.sha, commands)
        with open(os.path.join(self.repo, 'f.txt'), 'w', encoding='utf-8') as f:
            f.write('two\n')
        sh(['git', 'commit', '-qam', 'change'], self.repo)
        sha2 = sh(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip()
        k2 = product_checks.key(self.repo, sha2, commands)
        self.assertIsNotNone(k1)
        self.assertNotEqual(k1, k2)

    def test_no_check_commands_starts_nothing(self):
        spawned = []
        self.assertIsNone(product_checks.ensure(
            self.product(), self.repo, self.state_dir, self.sha, [],
            spawn_fn=lambda *a, **k: spawned.append(1)))
        self.assertEqual(spawned, [])

    def test_dry_run_never_spawns(self):
        p = self.product()
        spawned = []
        self.assertIsNone(product_checks.ensure(
            p, self.repo, self.state_dir, self.sha, p.conventions.get('check_commands'),
            spawn_fn=lambda *a, **k: spawned.append(1), dry_run=True))
        self.assertEqual(spawned, [])

    def test_run_job_writes_empty_results_when_the_product_names_none(self):
        p = self.product(check_commands=None)
        k = 'deadbeef-aaaaaaaaaaaa'
        product_checks.run_job(p, self.sha, k, out=lambda _l: None, state_dir=self.state_dir)
        self.assertEqual(product_checks.read_result(self.state_dir, k)['results'], [])

    def test_a_held_lock_defers_rather_than_running_twice(self):
        p = self.product()
        lock = product_checks.try_lock(self.state_dir)
        self.addCleanup(lock.close)
        self.assertIsNone(product_checks.ensure(
            p, self.repo, self.state_dir, self.sha, p.conventions.get('check_commands'),
            spawn_fn=lambda *a, **k: self.fail('spawned while another check holds the lock')))


class ChecksSectionTests(RepoCase):
    """:func:`asf.briefs.build.checks_section` — a review/correct brief reads the harvest's own
    cached result; it never runs a check itself."""

    def test_empty_before_the_harvest_has_a_result(self):
        p = self.product()
        self.assertEqual(build_mod.checks_section(p, 'review', self.sha), '')

    def test_renders_once_the_result_is_cached(self):
        p = self.product()
        commands = p.conventions.get('check_commands')
        k = product_checks.key(self.repo, self.sha, commands)
        product_checks.run_job(p, self.sha, k, out=lambda _l: None,
                               state_dir=env.state_dir(p))
        for kind in ('review', 'correct'):
            with self.subTest(kind=kind):
                text = build_mod.checks_section(p, kind, self.sha)
                self.assertIn(f'Checks on {self.sha[:9]} (run by the harvest — do not re-run):', text)
                self.assertIn('`echo ok` — passed', text)
                self.assertIn('`false` — failed (exit 1)', text)

    def test_empty_for_a_kind_that_is_not_review_or_correct(self):
        p = self.product()
        commands = p.conventions.get('check_commands')
        k = product_checks.key(self.repo, self.sha, commands)
        product_checks.run_job(p, self.sha, k, out=lambda _l: None, state_dir=env.state_dir(p))
        self.assertEqual(build_mod.checks_section(p, 'coder', self.sha), '')

    def test_empty_for_a_product_with_no_check_commands(self):
        p = self.product(check_commands=[])
        self.assertEqual(build_mod.checks_section(p, 'review', self.sha), '')

    def test_empty_with_no_head(self):
        p = self.product()
        self.assertEqual(build_mod.checks_section(p, 'review', ''), '')


if __name__ == '__main__':
    unittest.main()
