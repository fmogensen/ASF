import unittest

from asf import reviews


class TableTests(unittest.TestCase):
    def test_one_table(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| the first check | pass | ran it |\n'
            '| a name with an escaped \\| pipe | pass | ran it |\n'
            '| the third check | fail | broke |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(c.block == 1 for c in checks))
        self.assertEqual(checks[1].name, 'a name with an escaped | pipe')

    def test_two_tables_under_two_headings(self):
        text = (
            '### Pass 1 — mechanical\n\n'
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| m1 | pass | a |\n'
            '| m2 | pass | a |\n'
            '| m3 | pass | a |\n\n'
            '### Pass 2 — standard\n\n'
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| s1 | pass | a |\n'
            '| s2 | pass | a |\n'
            '| s3 | pass | a |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(len(checks), 6)
        self.assertEqual([c.block for c in checks], [1, 1, 1, 2, 2, 2])
        self.assertEqual([c.name for c in checks], ['m1', 'm2', 'm3', 's1', 's2', 's3'])

    def test_ok_header_parses_the_same(self):
        text = (
            '| check | ok | evidence |\n'
            '| --- | --- | --- |\n'
            '| the only check | pass | ran it |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].result, reviews.PASS)

    def test_malformed_row_is_a_fault_naming_its_line(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| pass | ran it |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(checks, [])
        self.assertEqual(len(faults), 1)
        self.assertIn('line 3', faults[0])

    def test_invalid_result_word_is_unfilled_not_a_pass(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| the check | approved | ran it |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(len(checks), 1)
        self.assertNotEqual(checks[0].result, reviews.PASS)

    def test_skeleton_placeholder_is_unfilled_not_a_pass(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| the check | <pass\\|fail> | |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(len(checks), 1)
        self.assertNotEqual(checks[0].result, reviews.PASS)

    def test_header_with_no_separator_is_no_table_at_all(self):
        text = (
            '| check | result | evidence |\n'
            'this is not a separator\n'
            '| the check | pass | ran it |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(checks, [])
        self.assertEqual(faults, [])

    def test_empty_evidence_cell_parses_as_fail(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| the check | pass | |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(checks[0].result, reviews.FAIL)

    def test_render_table_reparses_with_no_fault(self):
        names = reviews.required('spec')
        text = reviews.render_table(names)
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(len(checks), 5)


class NormalizeTests(unittest.TestCase):
    def test_backticks_dash_and_case_fold_the_same(self):
        a = reviews.normalize('`The Gate commands are green.`')
        b = reviews.normalize('- the  gate commands are GREEN')
        self.assertEqual(a, b)
        self.assertEqual(a, 'the gate commands are green')

    def test_required_code_is_the_six_mechanical_names(self):
        names = reviews.required('code')
        self.assertEqual(len(names), 6)
        standard = {reviews.normalize(n) for n in reviews.CHECKLIST['code'][1]}
        self.assertFalse(standard & set(names))

    def test_missing_returns_names_in_checklist_order(self):
        required = reviews.required('code')
        checks = [reviews.Check(name=required[-1], result=reviews.PASS, evidence='a',
                                block=1, line=1)]
        self.assertEqual(reviews.missing(checks, required), list(required[:-1]))


class VerdictTests(unittest.TestCase):
    def _table(self, rows):
        lines = ['| check | result | evidence |', '| --- | --- | --- |']
        lines += [f'| {name} | {result} | {evidence} |' for name, result, evidence in rows]
        return '\n'.join(lines)

    def test_all_pass_is_approved(self):
        text = self._table([('c1', 'pass', 'a'), ('c2', 'pass', 'a')])
        self.assertEqual(reviews.verdict(text, ('c1', 'c2')), reviews.APPROVED)

    def test_one_fail_is_changes(self):
        text = self._table([('c1', 'pass', 'a'), ('c2', 'fail', 'a')])
        self.assertEqual(reviews.verdict(text, ('c1', 'c2')), reviews.CHANGES)

    def test_missing_required_check_is_bounce(self):
        text = self._table([('c1', 'pass', 'a')])
        self.assertEqual(reviews.verdict(text, ('c1', 'c2')), reviews.BOUNCE)

    def test_unfilled_result_cell_is_bounce(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| c1 | <pass\\|fail> | |\n'
        )
        self.assertEqual(reviews.verdict(text, ('c1',)), reviews.BOUNCE)

    def test_typed_word_never_outranks_a_failing_table(self):
        text = 'verdict: APPROVED\n\n' + self._table([('c1', 'fail', 'a'), ('c2', 'fail', 'a')])
        self.assertEqual(reviews.verdict(text, ('c1', 'c2')), reviews.CHANGES)

    def test_prose_with_no_table_falls_back_to_the_typed_word(self):
        text = 'Some notes.\nVerdict: APPROVED\n'
        self.assertEqual(reviews.verdict(text, ('c1',)), '')
        self.assertEqual(reviews.typed_verdict(text), reviews.APPROVED)

    def test_empty_string_is_empty(self):
        self.assertEqual(reviews.verdict(''), '')

    def test_pass_and_na_with_full_coverage_is_approved(self):
        text = self._table([('c1', 'pass', 'a'), ('c2', 'n/a', 'a')])
        self.assertEqual(reviews.verdict(text, ('c1', 'c2')), reviews.APPROVED)

    def test_pass_row_with_empty_evidence_is_changes(self):
        text = self._table([('c1', 'pass', '')])
        self.assertEqual(reviews.verdict(text, ('c1',)), reviews.CHANGES)


if __name__ == '__main__':
    unittest.main()
