"""tools/run_tests.py (B-0068): the suite one module per process, N at a time, with the
serial suite's summary shape and exit status."""
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO_ROOT, 'tools', 'run_tests.py')


def load_runner():
    spec = importlib.util.spec_from_file_location('run_tests', RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GREEN = ("import unittest\n\nclass T(unittest.TestCase):\n"
         "    def test_one(self):\n        self.assertTrue(True)\n"
         "    def test_two(self):\n        self.assertTrue(True)\n")
RED = ("import unittest\n\nclass T(unittest.TestCase):\n"
       "    def test_ok(self):\n        pass\n"
       "    def test_breaks(self):\n        self.fail('the red one')\n")
SKIPPED = ("import unittest\n\nclass T(unittest.TestCase):\n"
           "    @unittest.skip('later')\n    def test_later(self):\n        pass\n")
HOME_PROBE = ("import os, unittest\n\nclass T(unittest.TestCase):\n"
              "    def test_own_home(self):\n"
              "        home = os.environ['ASF_TESTS_HOME']\n"
              "        open(os.path.join(home, 'mark'), 'x').close()  # a second writer would fail\n")


class FixtureSuite(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='run_tests_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tests = os.path.join(self.root, 'tests')
        os.makedirs(self.tests)
        open(os.path.join(self.tests, '__init__.py'), 'w').close()
        self.runner = load_runner()

    def module(self, name, text):
        with open(os.path.join(self.tests, name + '.py'), 'w', encoding='utf-8') as f:
            f.write(text)

    def run_suite(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.runner.run(self.tests, out=lambda l: print(l), **kw)
        return rc, buf.getvalue()


class PlanTests(FixtureSuite):
    def test_every_module_is_in_exactly_one_place_heaviest_first(self):
        self.module('test_00_home', GREEN)
        self.module('test_small', SKIPPED)
        self.module('test_big', GREEN + "    def test_three(self):\n        pass\n")
        self.module('test_mid', GREEN)
        self.module('test_alone', GREEN)
        self.module('helper', GREEN)  # not a test module
        pool, serial = self.runner.plan(self.tests, serial=('test_alone',))
        self.assertEqual(pool, ['test_big', 'test_mid', 'test_small'])
        self.assertEqual(serial, ['test_alone'])
        self.assertNotIn('test_00_home', pool + serial)  # every process runs it first

    def test_the_real_suite_is_planned_whole(self):
        tests_dir = os.path.join(REPO_ROOT, 'tests')
        pool, serial = self.runner.plan(tests_dir)
        modules = sorted(n[:-3] for n in os.listdir(tests_dir)
                         if n.startswith('test_') and n.endswith('.py'))
        modules += sorted(f'{d}.{n[:-3]}' for d in os.listdir(tests_dir)
                          if os.path.isfile(os.path.join(tests_dir, d, '__init__.py'))
                          for n in os.listdir(os.path.join(tests_dir, d))
                          if n.startswith('test_') and n.endswith('.py'))
        self.assertIn('scenarios.test_close_paths', modules)
        self.assertEqual(sorted(pool + serial + [self.runner.HOME_MODULE]), sorted(modules))
        self.assertEqual(len(set(pool + serial)), len(pool + serial))


class PartTests(FixtureSuite):
    """F-0301: ``--part K/N`` — CI's N parallel jobs together run every module exactly once."""

    def test_parse_part(self):
        self.assertEqual(self.runner.parse_part('2/4'), (2, 4))
        for bad in ('0/4', '5/4', '4', 'a/b', ''):
            with self.assertRaises(ValueError, msg=bad):
                self.runner.parse_part(bad)

    def test_the_parts_partition_the_real_suite(self):
        tests_dir = os.path.join(REPO_ROOT, 'tests')
        pool, serial = self.runner.plan(tests_dir)
        for n in range(1, 9):
            parts = [self.runner.plan_part(tests_dir, (k, n)) for k in range(1, n + 1)]
            got = [m for p, t in parts for m in p + t]
            self.assertEqual(sorted(got), sorted(pool + serial), n)
            self.assertEqual(len(got), len(set(got)), n)

    def test_the_parts_are_balanced_and_keep_the_heaviest_first_order(self):
        weights = {'a': 9, 'b': 5, 'c': 4, 'd': 3, 'e': 1}
        parts = self.runner.partition(['a', 'b', 'c', 'd', 'e'], weights, 2)
        self.assertEqual(parts, [['a', 'd'], ['b', 'c', 'e']])  # loads 12 and 10
        self.assertEqual(self.runner.partition(['a', 'b'], weights, 4), [['a'], ['b'], [], []])

    def test_serial_modules_go_to_part_one_and_a_part_runs_only_its_own(self):
        self.module('test_00_home', GREEN)
        self.module('test_a', GREEN)
        self.module('test_b', GREEN + "    def test_three(self):\n        pass\n")
        self.module('test_alone', GREEN)
        self.assertEqual(self.runner.plan_part(self.tests, (1, 2), serial=('test_alone',)),
                         (['test_b'], ['test_alone']))
        self.assertEqual(self.runner.plan_part(self.tests, (2, 2), serial=('test_alone',)),
                         (['test_a'], []))
        rc, text = self.run_suite(shards=2, part=(2, 2), serial=('test_alone',))
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r'(?m)^Ran \d+ tests in [\d.]+s \(1 module\(s\), 2 at a time\)$')

    def records(self, n, python='3.12', skip=(), checks=True):
        d = os.path.join(self.root, 'records')
        os.makedirs(d, exist_ok=True)
        green = os.path.join(self.root, 'green.out')
        with open(green, 'w') as f:
            f.write('Ran 7 tests in 1.0s (3 module(s), 4 at a time)\n\nOK\n')
        for k in range(1, n + 1):
            if k not in skip:
                with redirect_stdout(io.StringIO()):
                    self.runner.record(os.path.join(d, f'part-{python}-{k}.txt'), self.tests,
                                       (k, n), [green, green])
        if checks:
            open(os.path.join(d, f'checks-{python}.txt'), 'w').close()
        return d

    def gather(self, d, n, python='3.12'):
        lines = []
        return self.runner.gather(d, self.tests, python, n, out=lines.append), '\n'.join(lines)

    def test_gather_is_green_when_every_part_and_the_checks_are(self):
        for m in ('test_00_home', 'test_a', 'test_b', 'test_c'):
            self.module(m, GREEN)
        rc, text = self.gather(self.records(2), 2)
        self.assertEqual(rc, 0, text)
        self.assertIn('3 of 3 module(s) in 2 part(s); tests ran per pass: 14 / 14', text)

    def test_gather_is_red_on_a_missing_part_checks_or_module(self):
        for m in ('test_00_home', 'test_a', 'test_b', 'test_c'):
            self.module(m, GREEN)
        rc, text = self.gather(self.records(2, skip=(2,)), 2)
        self.assertEqual(rc, 1)
        self.assertIn('part (3.12, 2) did not finish green', text)
        rc, text = self.gather(self.records(2, python='3.13', checks=False), 2, python='3.13')
        self.assertIn('checks (3.13) did not finish green', text)
        d = self.records(2)
        self.module('test_d', GREEN)  # a module no part's record names
        rc, text = self.gather(d, 2)
        self.assertEqual(rc, 1)
        self.assertIn('run in no part: test_d', text)

    def test_record_is_red_when_a_run_was(self):
        self.module('test_a', GREEN)
        red = os.path.join(self.root, 'red.out')
        with open(red, 'w') as f:
            f.write('Ran 2 tests in 1.0s\n\nFAILED (failures=1)\n')
        with redirect_stdout(io.StringIO()):
            rc = self.runner.record(os.path.join(self.root, 'r.txt'), self.tests, (1, 1), [red])
        self.assertEqual(rc, 1)


class RunTests(FixtureSuite):
    def test_green_suite_prints_the_suites_summary_and_exits_zero(self):
        self.module('test_a', GREEN)
        self.module('test_b', GREEN)
        self.module('test_c', SKIPPED)
        rc, text = self.run_suite(shards=2)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r'(?m)^Ran 5 tests in [\d.]+s \(3 module\(s\), 2 at a time\)$')
        self.assertRegex(text, r'\nOK \(skipped=1\)\n?$')
        self.assertNotIn('test_one', text)  # a green module's output is not printed

    def test_a_red_module_fails_the_run_and_its_failure_is_streamed(self):
        self.module('test_a', GREEN)
        self.module('test_red', RED)
        self.module('test_b', GREEN)
        rc, text = self.run_suite(shards=3)
        self.assertEqual(rc, 1)
        self.assertIn('--- test_red: FAILED (rc 1)', text)
        self.assertIn('the red one', text)
        self.assertIn('FAIL: test_breaks', text)
        self.assertRegex(text, r'(?m)^Ran 6 tests in [\d.]+s')
        self.assertIn('\nFAILED (failures=1)\n', text)
        self.assertIn('red: test_red', text)

    def test_a_module_that_never_reaches_its_summary_is_an_error(self):
        self.module('test_a', GREEN)
        self.module('test_boom', 'raise RuntimeError("import time")\n')
        rc, text = self.run_suite(shards=2)
        self.assertEqual(rc, 1)
        self.assertIn('--- test_boom:', text)
        self.assertIn('RuntimeError', text)
        self.assertIn('FAILED (errors=1)', text)

    def test_one_shard_is_the_serial_suite(self):
        self.module('test_a', GREEN)
        self.module('test_b', GREEN)
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)
        self.assertIn('(2 module(s), 1 at a time)', text)

    def test_the_serial_set_runs_last_in_one_process_and_each_process_has_its_own_home(self):
        self.module('test_00_home', GREEN)
        self.module('test_a', HOME_PROBE)
        self.module('test_b', HOME_PROBE)
        self.module('test_z1', GREEN)
        self.module('test_z2', GREEN)
        rc, text = self.run_suite(shards=2, verbose=True, serial=('test_z1', 'test_z2'))
        self.assertEqual(rc, 0, text)
        order = [l for l in text.splitlines() if l.startswith('--- ')]
        self.assertEqual(order[-1].split(':')[0], '--- test_z1+test_z2')
        # the home module ran in every process: 3 processes x 2, two probes, two green pairs
        self.assertRegex(text, r'(?m)^Ran 12 tests')

    def test_children_run_in_the_hermetic_environment(self):
        self.module('test_env', (
            "import os, unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_it(self):\n"
            "        self.assertNotIn('GIT_DIR', os.environ)\n"
            "        self.assertNotIn('ASF_PRODUCT', os.environ)\n"
            f"        self.assertTrue(os.environ['PYTHONPATH'].startswith({self.root!r}))\n"))
        os.environ['GIT_DIR'] = '/nowhere'
        os.environ['ASF_PRODUCT'] = 'leak'
        try:
            rc, text = self.run_suite(shards=1)
        finally:
            os.environ.pop('GIT_DIR', None)
            os.environ.pop('ASF_PRODUCT', None)
        self.assertEqual(rc, 0, text)


def flaky_once(marker, homes_log=None):
    """A test module whose one test fails on its first attempt (``marker`` does not exist yet)
    and passes on its second (the first attempt created it) — a real ``FAIL:`` traceback raised
    from the module's own file, so :func:`run_tests.flaky_rows` reads a genuine frame. With
    ``homes_log``, each attempt appends its own ``ASF_TESTS_HOME`` to it, one per line."""
    log = (f"        with open({homes_log!r}, 'a') as f:\n"
          f"            f.write(os.environ['ASF_TESTS_HOME'] + chr(10))\n") if homes_log else ''
    return (
        "import os, unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_flakes(self):\n"
        f"{log}"
        f"        if not os.path.exists({marker!r}):\n"
        f"            open({marker!r}, 'x').close()\n"
        "            self.fail('rolled badly once')\n")


class RetryPassTests(FixtureSuite):
    """S-67254: a red module gets exactly one more attempt, over itself alone, and a module red
    then green leaves the run green with its second result in place of its first."""

    def test_the_retry_line_names_only_the_red_module(self):
        self.module('test_a', GREEN)
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker')))
        self.module('test_b', GREEN)
        rc, text = self.run_suite(shards=3)
        self.assertEqual(rc, 0, text)
        lines = [l for l in text.splitlines() if l.startswith('retry: ')]
        self.assertEqual(lines, ['retry: test_flakes'])

    def test_a_module_red_then_green_leaves_the_run_green(self):
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker')))
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)

    def test_the_replaced_result_prints_ok_with_no_red_line(self):
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker')))
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r'\nOK\b')
        self.assertNotIn('red:', text)

    def test_the_module_count_and_ran_count_are_the_final_attempts(self):
        self.module('test_a', GREEN)
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker')))
        rc, text = self.run_suite(shards=2)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r'(?m)^Ran 3 tests in [\d.]+s \(2 module\(s\), 2 at a time\)$')

    def test_the_retry_line_prints_before_the_summary(self):
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker')))
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)
        summary_at = re.search(r'(?m)^Ran \d+ tests? in [\d.]+s \(\d+ module\(s\)', text).start()
        self.assertLess(text.index('retry: test_flakes'), summary_at)

    def test_the_second_attempts_home_differs_from_the_first(self):
        homes_log = os.path.join(self.root, 'homes.log')
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker'), homes_log))
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)
        with open(homes_log, encoding='utf-8') as f:
            homes = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(homes), 2)
        self.assertNotEqual(homes[0], homes[1])


class FlakyBlockTests(FixtureSuite):
    """S-67255: a flake is read off the first attempt's output, in the shape the factory
    already reads, after the summary's verdict."""

    def test_flaky_rows_is_pure(self):
        before_cwd, before_env = os.getcwd(), dict(os.environ)
        output = ('FAIL: test_x (tests.test_mod.T.test_x)\n'
                  'Traceback (most recent call last):\n'
                  '  File "tests/test_mod.py", line 9, in test_x\n'
                  "    self.fail('boom')\n")
        rows = self.runner.flaky_rows('test_mod', 1, output)
        self.assertEqual(rows, [('tests/test_mod.py', 9, 'T.test_x')])
        self.assertEqual(os.getcwd(), before_cwd)
        self.assertEqual(dict(os.environ), before_env)

    def test_a_real_frame_under_the_modules_own_file_gives_the_file_and_line(self):
        output = ('FAIL: test_x (tests.test_mod.T.test_x)\n'
                  'Traceback (most recent call last):\n'
                  '  File "/checkout/tests/test_mod.py", line 42, in test_x\n'
                  "    self.fail('boom')\n")
        self.assertEqual(self.runner.flaky_rows('test_mod', 1, output),
                         [('tests/test_mod.py', 42, 'T.test_x')])

    def test_the_title_is_class_dot_test(self):
        output = 'FAIL: test_x (tests.pkg.test_mod.SomeClass.test_x)\n'
        rows = self.runner.flaky_rows('pkg.test_mod', 1, output)
        self.assertEqual(rows[0][2], 'SomeClass.test_x')

    def test_a_dotted_module_resolves_its_own_file_with_slashes(self):
        output = ('FAIL: test_x (tests.pkg.test_sub.T.test_x)\n'
                  'Traceback (most recent call last):\n'
                  '  File "tests/pkg/test_sub.py", line 3, in test_x\n')
        self.assertEqual(self.runner.flaky_rows('pkg.test_sub', 1, output),
                         [('tests/pkg/test_sub.py', 3, 'T.test_x')])

    def test_a_frame_elsewhere_falls_back_to_line_one(self):
        output = ('FAIL: test_x (tests.test_mod.T.test_x)\n'
                  'Traceback (most recent call last):\n'
                  '  File "/no/tests/frame/here.py", line 99, in test_x\n')
        self.assertEqual(self.runner.flaky_rows('test_mod', 1, output),
                         [('tests/test_mod.py', 1, 'T.test_x')])

    def test_an_attempt_with_no_block_names_itself_no_summary(self):
        self.assertEqual(self.runner.flaky_rows('test_boom', 2, 'Traceback\nRuntimeError: boom\n'),
                         [('tests/test_boom.py', 1, 'test_boom: no summary (rc 2)')])

    def test_two_failures_yield_two_rows_in_order(self):
        output = ('FAIL: test_x (tests.test_mod.T.test_x)\n'
                  '  File "tests/test_mod.py", line 5, in test_x\n'
                  'ERROR: test_y (tests.test_mod.T.test_y)\n'
                  '  File "tests/test_mod.py", line 9, in test_y\n')
        self.assertEqual(self.runner.flaky_rows('test_mod', 1, output),
                         [('tests/test_mod.py', 5, 'T.test_x'), ('tests/test_mod.py', 9, 'T.test_y')])

    def test_the_full_run_prints_the_block_after_the_verdict_with_the_real_line(self):
        self.module('test_flakes', flaky_once(os.path.join(self.root, 'marker')))
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)
        self.assertLess(text.index('\nOK'), text.index('1 flaky'))
        self.assertRegex(text, r'(?m)^  tests/test_flakes\.py:\d+:1 › T\.test_flakes$')

    def test_a_green_run_prints_no_flaky_block(self):
        self.module('test_a', GREEN)
        rc, text = self.run_suite(shards=1)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('flaky', text)


class RetryBoundsTests(FixtureSuite):
    """S-67256: the second pass is bounded and switchable."""

    def test_the_flags_default_to_the_module_constants(self):
        args = self.runner.build_parser().parse_args([])
        self.assertEqual(args.retry, self.runner.RETRY)
        self.assertEqual(args.retry_max, self.runner.RETRY_MAX)
        self.assertEqual(self.runner.RETRY, 1)
        self.assertEqual(self.runner.RETRY_MAX, 4)

    def test_retry_zero_runs_no_second_attempt(self):
        self.module('test_red', RED)
        rc, text = self.run_suite(shards=1, retry=0)
        self.assertEqual(rc, 1)
        self.assertNotIn('retry:', text)
        self.assertNotIn('flaky', text)

    def test_main_passes_the_cli_flags_through_to_run(self):
        self.module('test_red', RED)
        r = subprocess.run([sys.executable, RUNNER, '-s', self.tests, '--shards', '1',
                           '--retry', '0'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('retry:', r.stdout)

    def test_more_red_modules_than_the_bound_skips_the_retry(self):
        for i in range(5):
            self.module(f'test_red{i}', RED)
        rc, text = self.run_suite(shards=4, retry_max=4)
        self.assertEqual(rc, 1)
        self.assertIn('retry: skipped — 5 red module(s) over the bound of 4', text)
        for i in range(5):
            self.assertEqual(text.count(f'--- test_red{i}:'), 1)

    def test_exactly_the_bound_still_gets_the_second_pass(self):
        for i in range(4):
            self.module(f'test_red{i}', RED)
        rc, text = self.run_suite(shards=4, retry_max=4)
        self.assertEqual(rc, 1)
        self.assertNotIn('skipped', text)
        for i in range(4):
            self.assertEqual(text.count(f'--- test_red{i}:'), 2)


class OnlyTests(FixtureSuite):
    def test_the_named_modules_alone_run_and_the_red_line_names_the_red_ones(self):
        self.module('test_a', GREEN)
        self.module('test_red', RED)
        self.module('test_leak', (  # the variable is the runner's, never its children's
            "import os, unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_it(self):\n"
            "        self.assertNotIn('ASF_GATE_MODULES', os.environ)\n"))
        env = dict(os.environ, ASF_GATE_MODULES='test_red, test_leak test_gone')
        r = subprocess.run([sys.executable, RUNNER, '-s', self.tests, '--shards', '2'],
                           cwd=self.root, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('(2 module(s), 2 at a time)', r.stdout)
        self.assertEqual(r.stdout.rstrip().splitlines()[-1], 'red: test_red')

    def test_parse_only(self):
        self.assertEqual(self.runner.parse_only(' a, b  c '), ['a', 'b', 'c'])
        self.assertIsNone(self.runner.parse_only(''))


class ParseTests(unittest.TestCase):
    def test_the_processs_own_summary_is_the_last_one_in_its_output(self):
        runner = load_runner()
        inner = 'F\nFAIL: test_x\nRan 7 tests in 0.100s\n\nFAILED (failures=1)\n'
        own = '..\n----\nRan 2 tests in 0.400s\n\nOK (skipped=1)\n'
        self.assertEqual(runner.parse(inner + own), (2, 0.4, 'OK', {'skipped': 1}))
        self.assertEqual(runner.parse(own + inner), (7, 0.1, 'FAILED', {'failures': 1}))
        self.assertEqual(runner.parse('Traceback\nRuntimeError: boom\n'), (0, 0.0, None, {}))

    def test_expected_failures_are_green_and_unexpected_successes_are_red(self):
        runner = load_runner()
        green = 'Ran 3 tests in 1.000s\n\nOK (expected failures=2, skipped=1)\n'
        self.assertEqual(runner.parse(green),
                         (3, 1.0, 'OK', {'expected failures': 2, 'skipped': 1}))
        self.assertTrue(runner.summary([('test_x', 0, green)], 1.0, 1).splitlines()[-1]
                        .startswith('OK (expected failures=2'))
        red = 'Ran 1 test in 1.000s\n\nFAILED (unexpected successes=1)\n'
        self.assertIn('FAILED', runner.summary([('test_y', 1, red)], 1.0, 1))
        self.assertEqual(runner.parse(red)[3], {'unexpected successes': 1})


class HookLeakTests(unittest.TestCase):
    """The backstop (F-0143 S-34602): a hook file the confine never saw turns the run red."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='hook_leak_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(['git', 'init', '-q', self.root], check=True, capture_output=True)
        self.runner = load_runner()

    def _hooks_dir(self):
        out = subprocess.run(['git', '-C', self.root, 'rev-parse', '--git-path', 'hooks'],
                             capture_output=True, text=True, check=True).stdout.strip()
        return out if os.path.isabs(out) else os.path.join(self.root, out)

    def test_hook_dirs_is_the_repos_own_and_core_hookspath_when_set(self):
        own = self._hooks_dir()
        self.assertEqual(self.runner.hook_dirs(self.root), [own])
        shared = os.path.join(self.root, 'shared')
        os.makedirs(shared)
        subprocess.run(['git', '-C', self.root, 'config', 'core.hooksPath', shared],
                       check=True, capture_output=True)
        # git's own --git-path answer already follows core.hooksPath once set, so the
        # de-duplicated list collapses to the one dir both resolutions now name (D11).
        self.assertEqual(self._hooks_dir(), shared)
        self.assertEqual(self.runner.hook_dirs(self.root), [shared])

    def test_hook_dirs_of_a_tree_that_is_not_a_git_repo_is_empty(self):
        not_repo = tempfile.mkdtemp(prefix='not_a_repo_')
        self.addCleanup(shutil.rmtree, not_repo, ignore_errors=True)
        self.assertEqual(self.runner.hook_dirs(not_repo), [])

    def test_hook_digest_of_a_missing_dir_and_of_devnull_is_empty_and_does_not_raise(self):
        missing = os.path.join(self.root, 'nope')
        self.assertEqual(self.runner.hook_digest([missing, os.devnull]), {})

    def test_hook_leaks_is_empty_for_an_unchanged_pair(self):
        d = self._hooks_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'pre-push'), 'w') as f:
            f.write('unchanged\n')
        before = self.runner.hook_digest([d])
        self.assertEqual(self.runner.hook_leaks(before, before), [])

    def test_hook_leaks_names_an_added_an_overwritten_and_a_removed_file(self):
        d = self._hooks_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'pre-push'), 'w') as f:
            f.write('AAAAAAAAAA')
        with open(os.path.join(d, 'pre-commit'), 'w') as f:
            f.write('BBBBBBBBBB')
        before = self.runner.hook_digest([d])
        os.remove(os.path.join(d, 'pre-push'))
        with open(os.path.join(d, 'pre-commit'), 'w') as f:
            f.write('CBBBBBBBBB')  # same length as before — a size check would miss this (D10)
        with open(os.path.join(d, 'post-checkout'), 'w') as f:
            f.write('DDDDDDDDDD')
        after = self.runner.hook_digest([d])
        self.assertEqual(self.runner.hook_leaks(before, after), sorted([
            f'{d}/pre-push removed',
            f'{d}/pre-commit changed',
            f'{d}/post-checkout added',
        ]))

    def _plant(self, tests_dir, name, text):
        with open(os.path.join(tests_dir, name + '.py'), 'w', encoding='utf-8') as f:
            f.write(text)

    def test_the_runner_reddens_when_a_planted_module_writes_a_hook_the_confine_never_saw(self):
        tests_dir = os.path.join(self.root, 'tests')
        os.makedirs(tests_dir)
        open(os.path.join(tests_dir, '__init__.py'), 'w').close()
        hooks_dir = self._hooks_dir()
        self._plant(tests_dir, 'test_plant', (
            "import os, unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_it(self):\n"
            f"        os.makedirs({hooks_dir!r}, exist_ok=True)\n"
            f"        with open(os.path.join({hooks_dir!r}, 'pre-commit'), 'w') as f:\n"
            "            f.write('planted by a suite write that never asked git')\n"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.runner.run(tests_dir, out=lambda l: print(l))
        text = buf.getvalue()
        self.assertNotEqual(rc, 0, text)
        self.assertIn(f'hook leak: {hooks_dir}/pre-commit added', text)
        self.assertIn('FAILED (hook files changed outside a temp repo — F-0143)', text)

    def test_the_runner_over_a_clean_module_exits_zero(self):
        tests_dir = os.path.join(self.root, 'tests')
        os.makedirs(tests_dir)
        open(os.path.join(tests_dir, '__init__.py'), 'w').close()
        self._plant(tests_dir, 'test_clean', GREEN)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.runner.run(tests_dir, out=lambda l: print(l))
        self.assertEqual(rc, 0, buf.getvalue())


class CommandLineTests(unittest.TestCase):
    def test_list_prints_the_plan_and_shards_is_capped(self):
        r = subprocess.run([sys.executable, RUNNER, '--list', '--shards', '3'], cwd=REPO_ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], 'shards: 3')
        self.assertIn('pool    test_harvest', r.stdout)
        runner = load_runner()
        self.assertLessEqual(runner.default_shards(), runner.MAX_SHARDS)
        self.assertGreaterEqual(runner.default_shards(), 1)


if __name__ == '__main__':
    unittest.main()


class TouchedModules(unittest.TestCase):
    """``--touched BASE``: the pre-push gate runs every test module the change touches, whole —
    the changed test modules and the ones importing a changed source module — never the suite."""

    def setUp(self):
        self.runner = load_runner()
        self.tmp = tempfile.mkdtemp(prefix='touched_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tests = os.path.join(self.tmp, 'tests')
        os.makedirs(self.tests)
        files = {'__init__.py': '', 'test_00_home.py': GREEN, 'test_alpha.py': 'from pkg import alpha\n' + GREEN,
                 'test_uses_beta.py': 'from pkg.sub import beta, gamma\n' + GREEN,
                 'test_dotted.py': 'import pkg.sub.delta\n' + GREEN,
                 'test_other.py': GREEN}
        for name, text in files.items():
            with open(os.path.join(self.tests, name), 'w', encoding='utf-8') as fh:
                fh.write(text)

    def test_a_source_change_names_the_modules_importing_it(self):
        got = self.runner.touched_modules(self.tests, ['pkg/alpha.py', 'pkg/sub/beta.py',
                                                       'pkg/sub/delta.py', 'README.md'])
        self.assertEqual(got, ['test_alpha', 'test_dotted', 'test_uses_beta'])

    def test_a_changed_test_module_is_itself_touched(self):
        self.assertEqual(self.runner.touched_modules(self.tests, ['tests/test_other.py']),
                         ['test_other'])
        self.assertEqual(self.runner.touched_modules(self.tests, ['docs/x.md']), [])

    def test_the_cli_runs_only_the_touched_modules(self):
        def git(*a):
            subprocess.run(['git', '-C', self.tmp, *a], check=True, capture_output=True,
                           env=dict(os.environ, GIT_AUTHOR_NAME='a', GIT_AUTHOR_EMAIL='a@x',
                                    GIT_COMMITTER_NAME='a', GIT_COMMITTER_EMAIL='a@x'))
        git('init', '-q', '-b', 'main')
        os.makedirs(os.path.join(self.tmp, 'pkg'))
        with open(os.path.join(self.tmp, 'pkg', '__init__.py'), 'w') as fh:
            fh.write('')
        git('add', '-A')
        git('commit', '-qm', 'base')
        with open(os.path.join(self.tmp, 'pkg', 'alpha.py'), 'w') as fh:
            fh.write('X = 1\n')
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.runner.main(['--touched', 'main', '-s', self.tests, '--shards', '1'])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn('1 touched module(s): test_alpha', buf.getvalue())
        self.assertIn('(1 module(s)', buf.getvalue())
