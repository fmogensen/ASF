import unittest

from asf.conventions import Conventions
from asf.harvest import regression

#: a default-conventions instance: `fix/` is a `fix` branch, `worker/` a `code` one, `plan/` and
#: `spec/` their own kinds, `cloud/direct-` a `direct` one — none of it set by this test.
DEFAULT_CONV = Conventions()

MIXED_FILES = ['tests/test_a.py', 'src/b_test.py', 'ui/c.test.tsx', 'data/fixtures/sample.json',
               'src/d.py']


class PredicateTest(unittest.TestCase):
    """Design §1 — `gated`, `test_files`, `failing_cases` and `verdict`: no git, no test run, no
    product. S-80454's eleven acceptance lines, one assertion each."""

    def test_a_fix_branch_found_in_prod_with_a_test_and_a_non_test_change_is_gated(self):
        items = {'B-0001': {'found_in': 'prod'}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'src/a.py'])
        self.assertEqual((ok, why), (True, ''))

    def test_the_same_branch_found_in_ci_is_not_gated_and_why_names_found_in_ci(self):
        items = {'B-0001': {'found_in': 'ci'}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'src/a.py'])
        self.assertFalse(ok)
        self.assertEqual(why, 'found_in: ci')

    def test_a_code_plan_spec_or_direct_branch_is_not_gated_whatever_its_card_says(self):
        items = {'B-0001': {'found_in': 'prod'}}
        for branch in ('worker/B-0001', 'plan/F-0001', 'spec/F-0001', 'cloud/direct-B-0001'):
            ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', branch,
                                        ['tests/test_a.py', 'src/a.py'])
            self.assertFalse(ok, branch)
            self.assertTrue(why, branch)

    def test_a_card_with_no_found_in_field_at_all_is_gated(self):
        items = {'B-0001': {}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'src/a.py'])
        self.assertEqual((ok, why), (True, ''))

    def test_a_fix_branch_whose_whole_diff_is_test_files_is_not_gated(self):
        items = {'B-0001': {'found_in': 'prod'}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'tests/test_b.py'])
        self.assertFalse(ok)
        self.assertIn('no change to be red against', why)

    def test_test_files_keeps_order_and_leaves_a_fixture_data_file_alone(self):
        self.assertEqual(regression.test_files(MIXED_FILES),
                          ['tests/test_a.py', 'src/b_test.py', 'ui/c.test.tsx'])

    def test_failing_cases_reads_unittest_fail_and_error_and_the_node_vitest_and_tap_shapes(self):
        log = ('FAIL: test_x (a.b.C.test_x)\n'
               'ERROR: test_y (a.b.C.test_y)\n'
               '✖ a node case\n'
               '✗ a vitest case\n'
               '× another vitest case (12ms)\n'
               'not ok 3 - a tap case\n')
        self.assertEqual(regression.failing_cases(log),
                          ['test_x', 'test_y', 'a node case', 'a vitest case',
                           'another vitest case', 'a tap case'])

    def test_verdict_returns_proved_when_a_case_is_red_before_and_absent_after(self):
        before = (1, 'FAIL: test_x (a.b.C.test_x)\nRan 3 tests\nFAILED (failures=1)')
        after = (0, 'Ran 3 tests\nOK')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual(state, 'proved')
        self.assertEqual(cases, ['test_x'])
        self.assertTrue(line)

    def test_verdict_returns_not_red_when_the_pre_fix_run_exits_zero_naming_no_case(self):
        before = (0, 'Ran 3 tests\nOK')
        after = (0, 'Ran 3 tests\nOK')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual((state, cases), ('not-red', []))
        self.assertTrue(line)

    def test_verdict_returns_inconclusive_when_the_pre_fix_run_exits_non_zero_naming_no_case(self):
        before = (1, "ImportError: cannot import name 'thing'\nFAILED (errors=1)")
        after = (0, 'Ran 3 tests\nOK')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual((state, cases), ('inconclusive', []))
        self.assertTrue(line)

    def test_verdict_returns_red_both_when_every_case_red_before_is_red_after_too(self):
        before = (1, 'FAIL: test_x (a.b.C.test_x)\nFAILED (failures=1)')
        after = (1, 'FAIL: test_x (a.b.C.test_x)\nFAILED (failures=1)')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual((state, cases), ('red-both', ['test_x']))
        self.assertTrue(line)


if __name__ == '__main__':
    unittest.main()
