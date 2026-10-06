"""asf.workers.report — a session's typed REPORT read back, and the one failure it declares at
the source: ``pushed: no`` (F-0087, class "worker behaviour": B-0024, B-0052)."""
import unittest

RULED = """REPORT
item: B-0001
kind: adjudicate
status: done
branch: fix/B-0001
pushed: yes abc1234
commits: none
tests: none
left out: none
ruling: the hold was an add/add conflict, not a finding; the fix stands
  and the reviewer's point about the retry is overruled
```
"""

from asf.workers import report
from asf.workers import runtime as runtime_mod

DONE = """I fixed it.

REPORT
item: B-0001
kind: fix-bug
status: done
branch: fix/B-0001
pushed: yes 1a2b3c4
commits: 1a2b3c4 fix(B-0001): return 0 on empty
tests: python3 -m unittest — OK
left out: none
"""

WAITING = """I'll stop here and wait for the background test-suite task to finish; I'll continue
with the commit and push once it reports back.

REPORT
item: B-0001
kind: fix-bug
status: partial
branch: fix/B-0001
pushed: no — the suite is still running in the background
commits: none
tests: python3 -m unittest (background)
left out: the push
"""


class ParseTests(unittest.TestCase):
    def test_reads_every_field_of_the_last_block(self):
        rep = report.parse('REPORT\nitem: X\n\n' + DONE)
        self.assertEqual(rep['item'], 'B-0001')
        self.assertEqual(rep['status'], 'done')
        self.assertEqual(rep['pushed'], 'yes 1a2b3c4')
        self.assertEqual(rep['left out'], 'none')
        self.assertEqual(report.parse('no report here'), {})

    def test_a_multi_line_field_and_a_fence_end(self):
        text = 'REPORT\nitem: T-0001\ncommits: aaa one\nbbb two\ntests: ok\n```\nnot: a field\n'
        rep = report.parse(text)
        self.assertEqual(rep['commits'], 'aaa one\nbbb two')
        self.assertNotIn('not', rep)

    def test_proves_trailers_are_one_field_not_folded_into_left_out(self):
        """F-0040 P6: each `Proves:` trailer line collides, case-insensitively, with the
        `proves:` field name itself — a naive parse would restart the field on every line."""
        text = ('REPORT\nitem: T-0195\nstatus: done\nleft out: none\n'
                'proves: Proves: S-18750 line 1 — tests/test_proves.py::ParseTests::test_trailer\n'
                'Proves: S-18750 line 2 — tests/test_proves.py::ParseTests::test_duplicates\n'
                'ruling: not this kind\n```\n')
        rep = report.parse(text)
        self.assertEqual(rep['left out'], 'none')
        self.assertEqual(rep['proves'],
                         'Proves: S-18750 line 1 — tests/test_proves.py::ParseTests::test_trailer\n'
                         'Proves: S-18750 line 2 — tests/test_proves.py::ParseTests::test_duplicates')
        self.assertEqual(rep['ruling'], 'not this kind')


class FailureAtTheSourceTests(unittest.TestCase):
    def test_pushed_no_is_unpushed_work(self):
        self.assertEqual(report.failure(WAITING), report.UNPUSHED)
        self.assertIsNone(report.failure(DONE))
        self.assertIsNone(report.failure('done'))   # the fake runtime's results carry no REPORT
        self.assertIsNone(report.failure('REPORT\nitem: B-0001\nstatus: done\n'))  # declares nothing

    def test_the_runtime_marks_such_a_result_failed(self):
        rec = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': WAITING}
        self.assertEqual(runtime_mod.failure_reason(rec), report.UNPUSHED)
        self.assertFalse(runtime_mod.result_ok(rec))
        ok = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': DONE}
        self.assertIsNone(runtime_mod.failure_reason(ok))
        self.assertTrue(runtime_mod.result_ok(ok))


class NeedsInputTests(unittest.TestCase):
    def test_the_first_needs_operator_line_of_two(self):
        text = ('NEEDS OPERATOR: rotate the deploy key — run tools/rotate.sh\n'
                'NEEDS OPERATOR: approve the migration — run asf migrate --apply\n'
                'REPORT\nitem: B-0001\nstatus: done\n')
        self.assertEqual(report.needs_input(text),
                          'rotate the deploy key — run tools/rotate.sh')

    def test_a_needs_operator_line_outside_any_report_block_is_still_found(self):
        text = ('I did the work.\nNEEDS OPERATOR: confirm the rollback — run tools/rollback.sh\n'
                '\nREPORT\nitem: B-0001\nstatus: done\nbranch: fix/B-0001\n')
        self.assertEqual(report.needs_input(text), 'confirm the rollback — run tools/rollback.sh')

    def test_a_blocked_report_with_no_operator_line_gives_its_left_out(self):
        text = ('REPORT\nitem: B-0001\nstatus: blocked\nbranch: fix/B-0001\n'
                'left out: the migration — the prod DB is unreachable from this host\n')
        self.assertEqual(report.needs_input(text),
                          'the migration — the prod DB is unreachable from this host')

    def test_a_blocked_report_with_no_left_out_gives_the_first_line_of_the_text(self):
        text = ('The prod DB is unreachable from this host.\n\n'
                'REPORT\nitem: B-0001\nstatus: blocked\nbranch: fix/B-0001\nleft out: none\n')
        self.assertEqual(report.needs_input(text), 'The prod DB is unreachable from this host.')

    def test_none_for_a_done_report(self):
        self.assertIsNone(report.needs_input(DONE))

    def test_none_for_a_report_that_merely_mentions_waiting_for_ci(self):
        text = ('REPORT\nitem: B-0001\nstatus: partial\nbranch: fix/B-0001\n'
                'left out: the push — waiting for CI to go green\n')
        self.assertIsNone(report.needs_input(text))

    def test_none_for_text_with_no_report_at_all(self):
        self.assertIsNone(report.needs_input('I did the work and pushed it.'))

    def test_none_for_empty_and_none_text(self):
        self.assertIsNone(report.needs_input(''))
        self.assertIsNone(report.needs_input(None))

    def test_unfinished_still_pairs_partial_and_blocked(self):
        self.assertEqual(report.UNFINISHED, ('partial', 'blocked'))


if __name__ == '__main__':
    unittest.main()


class OperatorCommandTests(unittest.TestCase):
    """B-0042: the exact command a NEEDS OPERATOR question names, backtick-fenced."""

    def test_the_backtick_command_is_extracted(self):
        self.assertEqual(report.operator_command('rotate the key — `tools/rotate.sh`'),
                         'tools/rotate.sh')

    def test_the_last_of_several_backtick_spans_wins(self):
        q = 'check `git log -1` against `git status --short`'
        self.assertEqual(report.operator_command(q), 'git status --short')

    def test_empty_for_a_question_with_no_command(self):
        self.assertEqual(report.operator_command('approve the migration before it runs'), '')

    def test_empty_for_none_and_empty(self):
        self.assertEqual(report.operator_command(''), '')
        self.assertEqual(report.operator_command(None), '')


class RulingTests(unittest.TestCase):
    def test_b0064_the_ruling_paragraph_is_read_off_the_report(self):
        from asf.workers import report
        self.assertEqual(report.ruling(RULED),
                         "the hold was an add/add conflict, not a finding; the fix stands\n"
                         "and the reviewer's point about the retry is overruled")
        self.assertEqual(report.ruling('REPORT\nitem: B-0001\npushed: yes\n'), '')
        self.assertEqual(report.ruling('no report at all'), '')


class RulingFieldsTests(unittest.TestCase):
    """F-0090 D10: the ruling's mechanism is three typed fields, read off the last REPORT."""

    def report(self, *lines):
        return 'REPORT\nitem: T-0009\nkind: adjudicate\nstatus: done\n' + '\n'.join(lines) + '\n```\n'

    def test_the_three_fields_are_read(self):
        text = self.report('ruling: it waits', 'blocked_on: T-0025',
                           'writes: asf/a.py tests/test_a.py  docs/**', 'superseded_by: T-0030')
        self.assertEqual(report.ruling_fields(text),
                         {'blocked_on': 'T-0025', 'writes': ['asf/a.py', 'tests/test_a.py', 'docs/**'],
                          'superseded_by': 'T-0030'})

    def test_none_and_dashes_and_absent_are_all_no_claim(self):
        none = {'blocked_on': None, 'writes': None, 'superseded_by': None}
        for value in ('none', 'None', 'n/a', '-', '—', ''):
            with self.subTest(value=value):
                text = self.report(f'blocked_on: {value}', f'writes: {value}', f'superseded_by: {value}')
                self.assertEqual(report.ruling_fields(text), none)
        self.assertEqual(report.ruling_fields(self.report('ruling: nothing')), none)
        self.assertEqual(report.ruling_fields('no report at all'), none)

    def test_a_note_after_the_last_field_is_no_claim_of_an_id_field(self):
        """a product's B-1377 (2026-09-26): the ruling ended ``superseded_by: none`` and then a
        ``NEEDS OPERATOR: …`` paragraph. The run-on value read ``none\\nNEEDS OPERATOR: …`` — a
        superseded claim — so the lane refused the ruling and the loop guard parked the item.
        ``blocked_on`` and ``superseded_by`` name one item: their first line is the claim."""
        text = self.report('ruling: overruled', 'blocked_on: none', 'writes: a.md',
                           'superseded_by: none',
                           "NEEDS OPERATOR: PR #829's `gate` checks are red from a cancelled run")
        self.assertEqual(report.ruling_fields(text),
                         {'blocked_on': None, 'writes': ['a.md'], 'superseded_by': None})
        text = self.report('ruling: it waits', 'superseded_by: T-0030', 'the newer Task covers it')
        self.assertEqual(report.ruling_fields(text)['superseded_by'], 'T-0030')

    def test_a_field_outside_the_last_report_block_is_ignored(self):
        earlier = 'REPORT\nitem: T-0009\nblocked_on: T-0001\n\nlater text\nREPORT\nitem: T-0009\n'
        self.assertIsNone(report.ruling_fields(earlier)['blocked_on'])
        before_any = 'blocked_on: T-0001\n' + self.report('blocked_on: none')
        self.assertIsNone(report.ruling_fields(before_any)['blocked_on'])

    def test_the_ruling_paragraph_is_untouched_by_the_fields(self):
        text = self.report('ruling: it waits for T-0025', 'blocked_on: T-0025')
        self.assertEqual(report.ruling(text), 'it waits for T-0025')


    def test_a_report_quoting_a_cli_error_is_not_that_failure(self):
        text = ('REPORT\nitem: F-0001\nstatus: done\npushed: yes\n'
                'left out: tools/x.sh - permission denied in this session\n')
        rec = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}
        self.assertIsNone(runtime_mod.failure_reason(rec))
        self.assertTrue(runtime_mod.result_ok(rec))
        bare = dict(rec, result='Error: permission denied')
        self.assertEqual(runtime_mod.failure_reason(bare), 'permission')

if __name__ == '__main__':
    unittest.main()
