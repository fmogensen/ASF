"""asf.workers.report — a session's typed REPORT read back, and the one failure it declares at
the source: ``pushed: no`` (F-0087, class "worker behaviour": B-0024, B-0052)."""
import unittest

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
