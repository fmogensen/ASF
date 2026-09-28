"""asf.precheck — the level, the dimensions, and the table's grammar (F-0060 T1, §3.1, §3.2)."""
import ast
import inspect
import unittest

from asf import precheck, size

HEADER_LINE = '| check | result | confidence | evidence |'
SEP_LINE = '| --- | --- | --- | --- |'


def table(*rows):
    return '\n'.join([HEADER_LINE, SEP_LINE, *rows])


class LevelTests(unittest.TestCase):
    """§3.1: the level from the class and the diff, and the dimensions each level owes."""

    def test_small_task_is_low(self):
        level, why = precheck.level_for(size.SMALL, 40, 1500)
        self.assertEqual(level, precheck.LOW)
        self.assertIn(size.SMALL, why)

    def test_medium_task_is_high(self):
        level, why = precheck.level_for(size.MEDIUM, 40, 1500)
        self.assertEqual(level, precheck.HIGH)
        self.assertIn(size.MEDIUM, why)

    def test_large_task_is_high(self):
        level, why = precheck.level_for(size.LARGE, 40, 1500)
        self.assertEqual(level, precheck.HIGH)
        self.assertIn(size.LARGE, why)

    def test_small_task_over_the_line_is_max(self):
        level, why = precheck.level_for(size.SMALL, 4000, 1500)
        self.assertEqual(level, precheck.MAX)
        self.assertIn('4000', why)
        self.assertIn('1500', why)

    def test_large_task_over_the_line_is_max_too(self):
        level, why = precheck.level_for(size.LARGE, 4000, 1500)
        self.assertEqual(level, precheck.MAX)
        self.assertIn('4000', why)
        self.assertIn('1500', why)

    def test_unreadable_diff_never_lifts_the_level(self):
        level, why = precheck.level_for(size.SMALL, None, 1500)
        self.assertEqual(level, precheck.LOW)
        self.assertIn(size.SMALL, why)

    def test_unknown_class_is_high(self):
        level, why = precheck.level_for(None, 40, 1500)
        self.assertEqual(level, precheck.HIGH)
        self.assertIn('unknown', why)

    def test_max_lines_zero_turns_the_rule_off(self):
        level, why = precheck.level_for(size.SMALL, 4000, 0)
        self.assertEqual(level, precheck.LOW)

    def test_small_task_floored_to_high_by_security(self):
        level, why = precheck.level_for(size.SMALL, 40, 1500, security=True)
        self.assertEqual(level, precheck.HIGH)
        self.assertEqual(why, 'size class small, floored by sensitive paths')

    def test_max_answer_unchanged_by_security(self):
        level, why = precheck.level_for(size.SMALL, 4000, 1500, security=True)
        self.assertEqual(level, precheck.MAX)
        self.assertIn('4000', why)
        self.assertIn('1500', why)

    def test_every_case_reasserted_with_security_false(self):
        self.assertEqual(precheck.level_for(size.SMALL, 40, 1500, security=False),
                          precheck.level_for(size.SMALL, 40, 1500))
        self.assertEqual(precheck.level_for(size.MEDIUM, 40, 1500, security=False),
                          precheck.level_for(size.MEDIUM, 40, 1500))
        self.assertEqual(precheck.level_for(size.LARGE, 40, 1500, security=False),
                          precheck.level_for(size.LARGE, 40, 1500))
        self.assertEqual(precheck.level_for(size.SMALL, 4000, 1500, security=False),
                          precheck.level_for(size.SMALL, 4000, 1500))
        self.assertEqual(precheck.level_for(size.LARGE, 4000, 1500, security=False),
                          precheck.level_for(size.LARGE, 4000, 1500))
        self.assertEqual(precheck.level_for(size.SMALL, None, 1500, security=False),
                          precheck.level_for(size.SMALL, None, 1500))
        self.assertEqual(precheck.level_for(None, 40, 1500, security=False),
                          precheck.level_for(None, 40, 1500))
        self.assertEqual(precheck.level_for(size.SMALL, 4000, 0, security=False),
                          precheck.level_for(size.SMALL, 4000, 0))

    def test_dimensions_low_is_its_own_four(self):
        low = precheck.dimensions(precheck.LOW)
        self.assertEqual(low, precheck.DIMENSIONS[precheck.LOW])
        self.assertEqual(len(low), 4)

    def test_dimensions_high_is_low_then_its_own_four_in_order(self):
        self.assertEqual(precheck.dimensions(precheck.HIGH),
                          precheck.DIMENSIONS[precheck.LOW] + precheck.DIMENSIONS[precheck.HIGH])

    def test_dimensions_max_is_high_then_its_own_three_in_order(self):
        self.assertEqual(precheck.dimensions(precheck.MAX),
                          precheck.DIMENSIONS[precheck.LOW] + precheck.DIMENSIONS[precheck.HIGH]
                          + precheck.DIMENSIONS[precheck.MAX])

    def test_no_dimension_name_appears_twice(self):
        names = (precheck.DIMENSIONS[precheck.LOW] + precheck.DIMENSIONS[precheck.HIGH]
                 + precheck.DIMENSIONS[precheck.MAX])
        self.assertEqual(len(names), len(set(names)))

    def test_unknown_level_reads_as_high(self):
        self.assertEqual(precheck.dimensions('nonsense'), precheck.dimensions(precheck.HIGH))


class SecurityDimensionTests(unittest.TestCase):
    """§3.2: the rows a sensitive diff owes, additive over the level's own."""

    def test_security_dimensions_empty_for_no_hit(self):
        self.assertEqual(precheck.security_dimensions({}), ())
        self.assertEqual(precheck.security_dimensions(None), ())

    def test_security_dimensions_two_classes_in_order_then_the_standing_three(self):
        hit = {'auth': ['a.py'], 'billing': ['b.py']}
        dims = precheck.security_dimensions(hit)
        self.assertEqual(dims, (precheck.class_dimension('auth'), precheck.class_dimension('billing'))
                          + precheck.SECURITY_DIMENSIONS)
        self.assertEqual(len(dims), len(set(dims)))

    def test_dimensions_default_security_matches_plain_dimensions(self):
        self.assertEqual(precheck.dimensions(precheck.LOW),
                          precheck.DIMENSIONS[precheck.LOW])
        self.assertEqual(precheck.dimensions(precheck.HIGH),
                          precheck.DIMENSIONS[precheck.LOW] + precheck.DIMENSIONS[precheck.HIGH])

    def test_covered_default_security_matches_plain_covered(self):
        rows, faults = precheck.parse(table())
        self.assertEqual(precheck.covered(rows, precheck.LOW), precheck.DIMENSIONS[precheck.LOW])

    def test_dimensions_high_with_security_is_high_then_security_rows_in_order(self):
        security = precheck.security_dimensions({'billing': ['b.py']})
        self.assertEqual(precheck.dimensions(precheck.HIGH, security=security),
                          precheck.DIMENSIONS[precheck.LOW] + precheck.DIMENSIONS[precheck.HIGH]
                          + security)

    def test_covered_names_an_uncovered_class_row(self):
        security = precheck.security_dimensions({'billing': ['b.py']})
        rows, faults = precheck.parse(table())
        self.assertEqual(precheck.covered(rows, precheck.LOW, security=security),
                          precheck.DIMENSIONS[precheck.LOW] + security)

    def test_faults_one_dimension_not_covered_per_class_row(self):
        security = precheck.security_dimensions({'billing': ['b.py']})
        answered = precheck.dimensions(precheck.HIGH) + precheck.SECURITY_DIMENSIONS
        rows_text = table(*(f'| {d} | pass | n/a | ok |' for d in answered))
        class_faults = [f for f in precheck.faults(rows_text, precheck.HIGH, security=security)
                         if 'dimension not covered' in f]
        self.assertEqual(len(class_faults), 1)
        self.assertIn('billing:', class_faults[0])

    def test_render_table_with_security_reparses_one_row_per_dimension(self):
        security = precheck.security_dimensions({'billing': ['b.py']})
        text = precheck.render_table(precheck.HIGH, security=security)
        rows, faults = precheck.parse(text)
        expected = len(precheck.dimensions(precheck.HIGH, security=security))
        self.assertEqual(len(rows), expected)
        self.assertEqual(len(faults), expected * 2)
        self.assertTrue(all('unfilled' in f for f in faults))


class TableTests(unittest.TestCase):
    """§3.2: the grammar, and what it refuses."""

    def test_good_table_one_row_per_line_in_file_order_no_fault(self):
        row1 = ('| the diff stays inside the declared footprint | pass | n/a | '
                'asf/precheck.py, asf/cli.py |')
        row2 = '| a check with an escaped pipe \\| inside it | pass | n/a | ok |'
        rows, faults = precheck.parse(table(row1, row2))
        self.assertEqual(faults, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].check, 'the diff stays inside the declared footprint')
        self.assertEqual(rows[0].line, 3)
        self.assertEqual(rows[1].check, 'a check with an escaped pipe | inside it')
        self.assertEqual(rows[1].line, 4)

    def test_three_cell_row_is_a_fault_naming_its_line(self):
        rows, faults = precheck.parse(table('| a | pass | n/a |'))
        self.assertEqual(rows, [])
        self.assertEqual(len(faults), 1)
        self.assertIn('3', faults[0])

    def test_bad_result_is_a_fault_naming_its_line_and_value(self):
        row = '| the diff stays inside the declared footprint | approved | n/a | ok |'
        rows, faults = precheck.parse(table(row))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(faults), 1)
        self.assertIn('3', faults[0])
        self.assertIn('approved', faults[0])

    def test_bad_confidence_is_a_fault_naming_its_line_and_value(self):
        row = ('| the diff stays inside the declared footprint | fail | certain | '
               'some/file.py:1 - x |')
        rows, faults = precheck.parse(table(row))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(faults), 1)
        self.assertIn('3', faults[0])
        self.assertIn('certain', faults[0])

    def test_unfilled_result_is_reported_unfilled_never_as_a_pass(self):
        row = '| the diff stays inside the declared footprint | <pass\\|fail> | n/a | ok |'
        rows, faults = precheck.parse(table(row))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(faults), 1)
        self.assertIn('unfilled', faults[0])
        self.assertIn('3', faults[0])
        self.assertNotEqual(rows[0].result, precheck.PASS)
        self.assertEqual(precheck.findings(rows), [])
        self.assertEqual(precheck.blocking(rows), [])

    def test_fail_row_with_empty_evidence_is_a_fault(self):
        row = '| the diff stays inside the declared footprint | fail | high | |'
        rows, faults = precheck.parse(table(row))
        self.assertEqual(len(rows), 1)
        self.assertEqual(faults, ['row 3: fail row has empty evidence'])

    def test_header_with_no_separator_finds_no_table_and_no_fault(self):
        text = HEADER_LINE + '\n' + '| some | thing | not | separator |'
        rows, faults = precheck.parse(text)
        self.assertEqual(rows, [])
        self.assertEqual(faults, [])

    def test_covered_is_empty_for_a_complete_table(self):
        rows_text = table(*(f'| {d} | pass | n/a | ok |' for d in precheck.DIMENSIONS[precheck.LOW]))
        rows, faults = precheck.parse(rows_text)
        self.assertEqual(faults, [])
        self.assertEqual(precheck.covered(rows, precheck.LOW), ())

    def test_covered_returns_missing_dimensions_in_level_order(self):
        dims = precheck.DIMENSIONS[precheck.LOW]
        row_d = f'| {dims[3]} | pass | n/a | ok |'
        row_b = f'| {dims[1]} | pass | n/a | ok |'
        rows, faults = precheck.parse(table(row_d, row_b))
        self.assertEqual(faults, [])
        self.assertEqual(precheck.covered(rows, precheck.LOW), (dims[0], dims[2]))

    def test_findings_and_blocking_and_keys(self):
        row_low = '| a check | fail | low | some/file.py:1 - x |'
        row_med = '| b check | fail | medium | some/file.py:2 - y |'
        row_high = '| c check | fail | high | some/file.py:3 - z |'
        rows, faults = precheck.parse(table(row_low, row_med, row_high))
        self.assertEqual(faults, [])
        self.assertEqual(len(precheck.findings(rows)), 3)
        blocking = precheck.blocking(rows)
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0].confidence, precheck.CONF_HIGH)
        self.assertEqual(precheck.keys(rows), ['some/file.py:3'])

    def test_render_table_high_reparses_to_eight_rows_sixteen_unfilled_faults(self):
        text = precheck.render_table(precheck.HIGH)
        rows, faults = precheck.parse(text)
        self.assertEqual(len(rows), 8)
        self.assertEqual(len(faults), 16)
        self.assertTrue(all('unfilled' in f for f in faults))

    def test_head_of_reads_the_head_line(self):
        self.assertEqual(precheck.head_of('head: 4f2a9c1b8e03d7a6\nother text'), '4f2a9c1b8e03d7a6')
        self.assertIsNone(precheck.head_of('no head line here'))

    def test_level_of_reads_the_level_line(self):
        self.assertEqual(precheck.level_of('level: high\nother text'), precheck.HIGH)
        self.assertIsNone(precheck.level_of('no level line here'))

    def test_module_imports_nothing_outside_collections_re_and_size(self):
        tree = ast.parse(inspect.getsource(precheck))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        self.assertEqual(names, {'collections', 're', 'asf'})


if __name__ == '__main__':
    unittest.main()
