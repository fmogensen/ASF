"""asf.precheck — the level, the dimensions, and the table's grammar (F-0060 T1, §3.1, §3.2);
and the pass's own command, ``asf precheck`` and ``asf precheck report`` (T-0275, §2.7/§3.7)."""
import ast
import contextlib
import inspect
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import cli, env, precheck, size

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

    def test_grammar_half_imports_nothing_outside_collections_re_and_size(self):
        """§2.1's own promise holds for the grammar above the ``# ---- the command ----``
        divider; the command half below it is the one part of this module that opens a file,
        reads the clock and knows a path (T-0275)."""
        grammar, _, _ = inspect.getsource(precheck).partition('# ---- the command ----')
        tree = ast.parse(grammar)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        self.assertEqual(names, {'collections', 're', 'asf'})


def _complete_table(level=precheck.LOW, extra=()):
    rows = [f'| {d} | pass | n/a | ok |' for d in precheck.DIMENSIONS[level]]
    rows.extend(extra)
    return f'head: 4f2a9c1b8e03d7a6\nlevel: {level}\n\n' + table(*rows)


class CommandTests(unittest.TestCase):
    """§3.7: ``asf precheck PATH --level …``, through ``cli.main``, over files in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='precheck_cmd_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_complete_table_exits_0_blocking_0(self):
        path = self._write('p.md', _complete_table())
        rc, out, err = self._run(['precheck', path, '--level', 'low'])
        self.assertEqual(rc, 0, out + err)
        self.assertIn('blocking: 0', out)

    def test_one_high_fail_row_exits_0_blocking_1(self):
        extra = ('| extra check | fail | high | some/file.py:1 - x |',)
        path = self._write('p.md', _complete_table(extra=extra))
        rc, out, err = self._run(['precheck', path, '--level', 'low'])
        self.assertEqual(rc, 0, out + err)
        self.assertIn('blocking: 1', out)
        self.assertIn('this branch will be sent back', out)

    def test_missing_dimension_exits_1_naming_it(self):
        dims = precheck.DIMENSIONS[precheck.LOW]
        rows = [f'| {d} | pass | n/a | ok |' for d in dims[:-1]]
        path = self._write('p.md', 'head: 4f2a9c1b8e03d7a6\n\n' + table(*rows))
        rc, out, err = self._run(['precheck', path, '--level', 'low'])
        self.assertEqual(rc, 1, out + err)
        self.assertIn('dimension not covered:', out)
        self.assertIn(dims[-1], out)

    def test_unfilled_confidence_exits_1_naming_its_line(self):
        dims = precheck.DIMENSIONS[precheck.LOW]
        rows = [f'| {d} | pass | n/a | ok |' for d in dims[:-1]]
        rows.append(f'| {dims[-1]} | pass | <n/a\\|high\\|medium\\|low> | ok |')
        text = 'head: 4f2a9c1b8e03d7a6\n\n' + table(*rows)
        path = self._write('p.md', text)
        rc, out, err = self._run(['precheck', path, '--level', 'low'])
        self.assertEqual(rc, 1, out + err)
        lineno = text.splitlines().index(rows[-1]) + 1
        self.assertIn(f'{path}:{lineno}:', out)
        self.assertIn('confidence is unfilled', out)

    def test_no_head_line_exits_1(self):
        rows = [f'| {d} | pass | n/a | ok |' for d in precheck.DIMENSIONS[precheck.LOW]]
        path = self._write('p.md', table(*rows))
        rc, out, err = self._run(['precheck', path, '--level', 'low'])
        self.assertEqual(rc, 1, out + err)
        self.assertIn('no head: line', out)

    def test_missing_path_exits_2_one_line_no_traceback(self):
        path = os.path.join(self.tmp, 'does-not-exist.md')
        rc, out, err = self._run(['precheck', path, '--level', 'low'])
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(out, '')

    def test_level_omitted_exits_2(self):
        path = self._write('p.md', _complete_table())
        rc, out, err = self._run(['precheck', path])
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertTrue(err.strip().startswith('usage:'), err)

    def test_json_parses_sorted_keys_rows_length(self):
        path = self._write('p.md', _complete_table())
        rc, out, err = self._run(['precheck', path, '--level', 'low', '--json'])
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(list(data), sorted(data))
        self.assertEqual(len(data['rows']), len(precheck.DIMENSIONS[precheck.LOW]))


class ReportTests(unittest.TestCase):
    """§3.7: ``asf precheck report`` over a fixture record and reviews directory — no git, no
    network: the product's checkout and its reviews directory are the whole input."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='precheck_report_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        self.repo = os.path.join(self.tmp, 'repo')
        self.reviews_dir = os.path.join(self.repo, 'docs', 'reviews')
        os.makedirs(self.reviews_dir)
        with open(env.product_path('sample'), 'w', encoding='utf-8') as f:
            f.write(f'product: sample\nrepo_slug: x/y\nrepo_dir: {self.repo}\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.reviews_dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _seed(self):
        precheck_text = (
            '# Precheck — T-0244, level high\n\n'
            'head: 4f2a9c1b8e03d7a6\n'
            'level: high\n\n'
            + table(
                '| dim1 | pass | n/a | ok |',
                '| dim2 | fail | high | asf/precheck.py:118 - bad |',
                '| dim3 | fail | medium | asf/other.py:10 - meh |',
            )
        )
        self._write('precheck-t-0244.md', precheck_text)
        self._write('1-t-0244.md', (
            '# Review T-0244 — round 1\n\n## C\n\n'
            '1. `asf/precheck.py:118` - fix this\n'
            '2. `asf/new_file.py:5` - new problem\n'
        ))
        self._write('2-t-0244.md', (
            '# Review T-0244 — round 2\n\n## C\n\n'
            '1. `asf/other.py:10` - still here\n'
        ))

    def test_one_row_per_item_with_level_findings_and_rounds(self):
        self._seed()
        rc, out, err = self._run(['precheck', 'report', '--product', 'sample', '--json'])
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(len(data['items']), 1)
        row = data['items'][0]
        self.assertEqual(row['item'], 'T-0244')
        self.assertEqual(row['level'], 'high')
        self.assertEqual(row['findings'], {'high': 1, 'medium': 1, 'low': 0})
        self.assertEqual(len(row['rounds']), 2)

    def test_c_item_the_precheck_already_named_is_repeated_not_new(self):
        self._seed()
        rc, out, _ = self._run(['precheck', 'report', '--product', 'sample', '--json'])
        row = json.loads(out)['items'][0]
        round1 = next(r for r in row['rounds'] if r['round'] == 1)
        self.assertEqual(round1['c'], 2)
        self.assertEqual(round1['repeated'], 1)
        self.assertEqual(round1['new'], 1)

    def test_round_raising_only_already_named_findings_is_all_repeated(self):
        self._seed()
        rc, out, _ = self._run(['precheck', 'report', '--product', 'sample', '--json'])
        row = json.loads(out)['items'][0]
        round2 = next(r for r in row['rounds'] if r['round'] == 2)
        self.assertEqual(round2['c'], 1)
        self.assertEqual(round2['repeated'], 1)
        self.assertEqual(round2['new'], 0)

    def test_json_is_one_typed_object_with_the_same_numbers(self):
        self._seed()
        rc, out, err = self._run(['precheck', 'report', '--product', 'sample', '--json'])
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(data['days'], 30)
        row = data['items'][0]
        self.assertEqual(row['repeated'], 2)
        self.assertEqual(row['new'], 1)

    def test_human_output_names_the_item_and_level(self):
        self._seed()
        rc, out, err = self._run(['precheck', 'report', '--product', 'sample'])
        self.assertEqual(rc, 0, out + err)
        self.assertIn('T-0244', out)
        self.assertIn('high', out)

    def test_days_window_excludes_an_older_precheck(self):
        self._seed()
        old = 1000 * 86400
        now = os.path.getmtime(os.path.join(self.reviews_dir, 'precheck-t-0244.md'))
        os.utime(os.path.join(self.reviews_dir, 'precheck-t-0244.md'), (now - old, now - old))
        rc, out, _ = self._run(['precheck', 'report', '--product', 'sample', '--json'])
        self.assertEqual(json.loads(out)['items'], [])

    def test_no_review_for_the_precheck_raises_no_row(self):
        self._write('precheck-t-0500.md', 'head: 4f2a9c1b8e03d7a6\nlevel: low\n\n' + table())
        rc, out, _ = self._run(['precheck', 'report', '--product', 'sample', '--json'])
        self.assertEqual(json.loads(out)['items'], [])


if __name__ == '__main__':
    unittest.main()
