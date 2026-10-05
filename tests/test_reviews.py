import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import cli, env, reviews


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

    def test_invalid_result_word_is_a_fault_naming_its_line(self):
        text = (
            '| check | result | evidence |\n'
            '| --- | --- | --- |\n'
            '| the check | approved | ran it |\n'
        )
        checks, faults = reviews.parse(text)
        self.assertEqual(checks, [])
        self.assertEqual(len(faults), 1)
        self.assertIn('line 3', faults[0])

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

    def test_an_unescaped_pipe_in_the_evidence_stays_in_the_evidence(self):
        text = ('| check | result | evidence |\n| --- | --- | --- |\n'
                '| no secret value printed | pass | `grep -E "key|token"` → no match |\n')
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual(checks[0].result, reviews.PASS)
        self.assertIn('token', checks[0].evidence)

    def test_a_result_opening_a_qualifier_is_its_word(self):
        text = ('| check | result | evidence |\n| --- | --- | --- |\n'
                '| the Gate commands are green | pass, with a caveat | ran them |\n'
                '| the diff stays inside `writes:` | FAIL — one file outside | x.py |\n')
        checks, faults = reviews.parse(text)
        self.assertEqual(faults, [])
        self.assertEqual([c.result for c in checks], [reviews.PASS, reviews.FAIL])

    def test_a_row_too_wide_with_no_result_word_is_still_a_fault(self):
        text = ('| check | result | evidence |\n| --- | --- | --- |\n'
                '| a | b | c | d |\n| x | passable | y |\n')
        checks, faults = reviews.parse(text)
        self.assertEqual(checks, [])
        self.assertEqual(len(faults), 2)

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

    def test_a_row_worded_shorter_or_under_a_label_covers_its_check(self):
        # the brief's skeleton once worded two rows so: 119 of 121 approved code reviews read
        # BOUNCE and each sent an approved branch back for a correction round
        required = reviews.required('code')
        names = ['scope: the diff stays inside `writes:`', 'every Step of the Task is implemented',
                 "acceptance tests byte-identical to the plan's",
                 'those tests were run and are green', 'the Gate commands are green',
                 'no secret value printed, no background process, no skipped check']
        checks = [reviews.Check(name=n, result=reviews.PASS, evidence='e', block=1, line=i)
                  for i, n in enumerate(names)]
        self.assertEqual(reviews.missing(checks, required), [])

    def test_a_row_naming_another_check_does_not_cover(self):
        self.assertFalse(reviews.covers('the acceptance tests are green',
                                        "the acceptance tests are byte-identical to the plan's"))
        self.assertFalse(reviews.covers('the diff', 'the diff stays inside `writes:`'))

    def test_the_review_brief_skeleton_names_the_code_checklist(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'asf', 'briefs', 'templates', 'review.md')
        with open(path, encoding='utf-8') as fh:
            checks, _faults = reviews.parse(fh.read().replace("{delivery_checks}", ""))
        self.assertEqual([reviews.normalize(c.name) for c in checks][:6],
                         list(reviews.required('code')))


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


class PreReviewCommandTests(unittest.TestCase):
    """§3.3, through ``cli.main`` over files written into a temp dir with ``ASF_HOME`` pinned
    to it — no git, no network."""

    STORY_LINES = ['the widget renders its three views', 'the widget updates when clicked',
                   'the widget clears on reset']

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='review_checks_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.backlog = os.path.join(self.tmp, 'backlog')
        os.makedirs(os.path.join(self.backlog, 'stories'))
        with open(os.path.join(self.backlog, 'stories', 'S-19950.md'), 'w', encoding='utf-8') as f:
            f.write('## Acceptance\n' + ''.join(f'- [ ] {line}\n' for line in self.STORY_LINES))
        with open(os.path.join(self.backlog, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump({'generated': '', 'items': {
                'S-19950': {'id': 'S-19950', 'type': 'story', 'folder': 'stories'},
            }}, f)
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w', encoding='utf-8') as f:
            f.write(f'product: sample\nrepo_slug: x/y\nbacklog_dir: {self.backlog}\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    @staticmethod
    def _table(rows):
        lines = ['| check | result | evidence |', '| --- | --- | --- |']
        lines += [f'| {name} | {result} | {evidence} |' for name, result, evidence in rows]
        return '\n'.join(lines) + '\n'

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv + ['--product', 'sample'])
        return rc, out.getvalue(), err.getvalue()

    def test_complete_pass_table_exits_0(self):
        rows = [(name, 'pass', 'checked') for name in reviews.CHECKLIST['spec'][0]]
        path = self._write('r1.md', self._table(rows))
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec'])
        self.assertEqual(rc, 0, out + err)
        self.assertIn('verdict: APPROVED', out)

    def test_one_row_deleted_bounces_naming_the_missing_check(self):
        names = reviews.CHECKLIST['spec'][0]
        rows = [(name, 'pass', 'checked') for name in names[:-1]]
        path = self._write('r2.md', self._table(rows))
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec'])
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f'required check missing: "{reviews.normalize(names[-1])}"', out)
        self.assertIn('verdict: BOUNCE', out)

    def test_prose_with_a_typed_verdict_but_no_table_bounces(self):
        path = self._write('r3.md', 'Looks fine.\nverdict: APPROVED\n')
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec'])
        self.assertEqual(rc, 1, out + err)
        self.assertIn('no check table', out)
        self.assertIn('verdict: BOUNCE', out)

    def test_malformed_row_exits_1_naming_its_line(self):
        text = '| check | result | evidence |\n| --- | --- | --- |\n| pass | ran it |\n'
        path = self._write('r4.md', text)
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec'])
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f'{os.path.basename(path)}:3:', out)

    def test_complete_table_with_one_fail_exits_0(self):
        names = reviews.CHECKLIST['spec'][0]
        rows = [(n, 'pass', 'checked') for n in names[:-1]] + [(names[-1], 'fail', 'broke')]
        path = self._write('r5.md', self._table(rows))
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec'])
        self.assertEqual(rc, 0, out + err)
        self.assertIn('verdict: CHANGES REQUESTED', out)

    def test_card_code_review_requires_every_story_acceptance_line(self):
        names = list(reviews.CHECKLIST['code'][0])
        rows = [(n, 'pass', 'checked') for n in names + self.STORY_LINES[:-1]]
        path = self._write('r6.md', self._table(rows))
        rc, out, err = self._run(['review-checks', path, '--kind', 'code', '--card', 'S-19950'])
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f'required check missing: "{self.STORY_LINES[-1]}"', out)

    def test_card_code_review_with_full_coverage_is_approved(self):
        names = list(reviews.CHECKLIST['code'][0])
        rows = [(n, 'pass', 'checked') for n in names + self.STORY_LINES]
        path = self._write('r7.md', self._table(rows))
        rc, out, err = self._run(['review-checks', path, '--kind', 'code', '--card', 'S-19950'])
        self.assertEqual(rc, 0, out + err)
        self.assertIn('verdict: APPROVED', out)

    def test_json_output_parses_with_sorted_keys_and_matching_check_count(self):
        names = reviews.CHECKLIST['spec'][0]
        rows = [(n, 'pass', 'checked') for n in names]
        path = self._write('r8.md', self._table(rows))
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec', '--json'])
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual(list(data.keys()), sorted(data.keys()))
        self.assertEqual(len(data['checks']), len(names))

    def test_missing_path_exits_2_with_one_line_and_no_traceback(self):
        path = os.path.join(self.tmp, 'does-not-exist.md')
        rc, out, err = self._run(['review-checks', path, '--kind', 'spec'])
        self.assertEqual(rc, 2)
        self.assertNotIn('Traceback', err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)

    def test_kind_omitted_exits_2(self):
        path = self._write('r9.md', 'x\n')
        with self.assertRaises(SystemExit) as cm:
            self._run(['review-checks', path])
        self.assertEqual(cm.exception.code, 2)

    def test_unknown_card_exits_2(self):
        path = self._write('r10.md', 'x\n')
        rc, out, err = self._run(['review-checks', path, '--kind', 'code', '--card', 'S-99999'])
        self.assertEqual(rc, 2)
        self.assertIn('S-99999', err)


if __name__ == '__main__':
    unittest.main()
