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
        self.assertEqual(sorted(pool + serial + [self.runner.HOME_MODULE]), modules)
        self.assertEqual(len(set(pool + serial)), len(pool + serial))


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
