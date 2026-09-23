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


if __name__ == '__main__':
    unittest.main()


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

    def test_a_field_outside_the_last_report_block_is_ignored(self):
        earlier = 'REPORT\nitem: T-0009\nblocked_on: T-0001\n\nlater text\nREPORT\nitem: T-0009\n'
        self.assertIsNone(report.ruling_fields(earlier)['blocked_on'])
        before_any = 'blocked_on: T-0001\n' + self.report('blocked_on: none')
        self.assertIsNone(report.ruling_fields(before_any)['blocked_on'])

    def test_the_ruling_paragraph_is_untouched_by_the_fields(self):
        text = self.report('ruling: it waits for T-0025', 'blocked_on: T-0025')
        self.assertEqual(report.ruling(text), 'it waits for T-0025')


if __name__ == '__main__':
    unittest.main()
