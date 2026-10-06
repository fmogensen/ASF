"""asf.workers.runtime.error_subtype — F-0223, S-36550: the factory's largest failure class,
`failure:failed`, is a catch-all because the runtime's own structured error field is read
nowhere. This module pins the one new reader (`error_subtype`) and the two things that must
stay true of it: the bare `failed` reason it is read from is itself unchanged (D1), and the
field it writes never leaks onto a job's next run (PD8).

Plain dicts, unittest, no shell-out, no network.
"""
import unittest

from asf.improve.measure import Run
from asf.scorecard import score
from asf.workers import lifecycle as lc
from asf.workers import runtime as runtime_mod

# Every record here carries `type: 'result'` and has `failure_reason(rec) is None` (no `asf.cap`,
# no `asf.run_cap`, a `result` text that matches none of the CLI's `FAILURE_SIGNATURES` and parses
# no typed REPORT) — the precondition §1's biconditional and `judge`'s fallback both depend on
# (PD13). The first is the only one `result_ok` reads as True; the rest are the runtime's own
# declared errors, read five different ways.
TABLE = [
    {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'},
    {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'result': ''},
    {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'result': ''},
    {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': ''},
    {'type': 'result', 'is_error': True, 'result': ''},
    {'type': 'result', 'subtype': 'error_Some_New_Thing', 'is_error': True, 'result': ''},
]


class ErrorSubtype(unittest.TestCase):
    """`error_subtype(rec)`: structured fields only (D2), `subtype` read before `is_error`."""

    def test_a_mapped_subtype_reads_its_factory_word(self):
        self.assertEqual(runtime_mod.error_subtype({'subtype': 'error_max_turns'}), 'max turns')
        self.assertEqual(runtime_mod.error_subtype({'subtype': 'error_during_execution'}),
                          'during execution')

    def test_is_error_alone_or_behind_a_success_subtype_reads_runtime_error(self):
        self.assertEqual(runtime_mod.error_subtype({'is_error': True}), runtime_mod.RUNTIME_ERROR)
        self.assertEqual(runtime_mod.error_subtype({'subtype': 'success', 'is_error': True}),
                          runtime_mod.RUNTIME_ERROR)

    def test_an_unmapped_subtype_reads_its_own_text_sanitised(self):
        # leading `error_` dropped, non-alphanumerics collapsed, lowercased — no entry needed
        self.assertEqual(runtime_mod.error_subtype({'subtype': 'error_Some_New_Thing'}),
                          'some new thing')

    def test_a_success_subtype_with_no_is_error_reads_none(self):
        self.assertIsNone(runtime_mod.error_subtype({'subtype': 'success'}))

    def test_none_and_empty_read_none(self):
        # neither is reachable through `judge` (`ev.result is None` returns before `error_subtype`
        # is ever called, lifecycle.py's `if ev.result is None: return None if ev.alive else
        # DEAD_PID`) — asserted here only because `error_subtype` is pure and total (PD13)
        self.assertIsNone(runtime_mod.error_subtype(None))
        self.assertIsNone(runtime_mod.error_subtype({}))

    def test_the_biconditional_over_records_failure_reason_does_not_already_explain(self):
        # §1: for every record `failure_reason` reads as None, `error_subtype` is not None
        # exactly when `result_ok` is False — the whole of what makes bare `failed` nameable
        for rec in TABLE:
            with self.subTest(rec=rec):
                self.assertIsNone(runtime_mod.failure_reason(rec))
                self.assertEqual(runtime_mod.error_subtype(rec) is not None,
                                  runtime_mod.result_ok(rec) is False)


class TheReasonIsUnchanged(unittest.TestCase):
    """D1, asserted rather than trusted: naming the runtime's word moves nothing `judge` or
    `failure_class` already returns — the cause key `failure:failed` is the whole point this
    Story is not allowed to touch."""

    RUN = {'job': 'j', 'pid': 1, 'started': 't'}  # no branch: judged on the result alone

    def test_judge_still_returns_bare_failed_for_every_dead_record(self):
        for rec in TABLE:
            if runtime_mod.result_ok(rec):
                continue  # the one record in TABLE that is not a death
            with self.subTest(rec=rec):
                reason = lc.judge(self.RUN, lc.Evidence(result=rec))
                self.assertEqual(reason, lc.FAILED_BARE)
                self.assertEqual(reason, 'failed')
                self.assertEqual(score.failure_class(reason), 'failed')

    def test_a_dead_runs_run_is_dead_only_until_it_lands(self):
        reason = lc.judge(self.RUN, lc.Evidence(
            result={'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'result': ''}))
        self.assertEqual(reason, lc.FAILED_BARE)
        dead = Run(job='j', kind='task', model='m', item=None, started='t1', ended='t2',
                   minutes=1.0, landed=False, end_reason=reason, usd=0.0)
        self.assertTrue(score.is_dead(dead))
        landed = Run(job='j', kind='task', model='m', item=None, started='t1', ended='t2',
                    minutes=1.0, landed=True, end_reason=reason, usd=0.0)
        self.assertFalse(score.is_dead(landed))


class OnTheRow(unittest.TestCase):
    """What health writes to the registry row — pure fold, no file, no git: `runtime_error`
    belongs to its own run (PD8) exactly as every other member of `RUN_FIELDS` does."""

    def _fold(self, lines):
        return lc.fold(lines)

    def test_a_bare_failed_run_carries_its_word_on_the_row(self):
        runs = self._fold([
            {'job': 'j', 'pid': 1, 'started': 't1'},
            {'job': 'j', 'ended': 't2', 'end_reason': lc.FAILED_BARE, 'runtime_error': 'max turns'},
        ])
        row = runs['j'][-1]
        self.assertEqual(row['end_reason'], 'failed')
        self.assertEqual(row['runtime_error'], 'max turns')

    def test_a_finished_run_carries_an_empty_runtime_error(self):
        runs = self._fold([
            {'job': 'j', 'pid': 1, 'started': 't1'},
            {'job': 'j', 'ended': 't2', 'end_reason': lc.FINISHED, 'runtime_error': ''},
        ])
        self.assertEqual(runs['j'][-1]['runtime_error'], '')

    def test_runtime_error_is_a_run_field_in_run_fields(self):
        self.assertIn('runtime_error', lc.RUN_FIELDS)

    def test_a_second_launch_does_not_inherit_the_first_runs_word(self):
        # PD8: without the entry in RUN_FIELDS this run would read `max turns` for a run that
        # died on something else entirely — a launch line opens a run of its own, clean
        runs = self._fold([
            {'job': 'j', 'pid': 1, 'started': 't1'},
            {'job': 'j', 'ended': 't2', 'end_reason': lc.FAILED_BARE, 'runtime_error': 'max turns'},
            {'job': 'j', 'pid': 2, 'started': 't3'},
        ])
        rs = runs['j']
        self.assertEqual(len(rs), 2)
        self.assertEqual(rs[0]['runtime_error'], 'max turns')
        self.assertNotIn('runtime_error', rs[1])


if __name__ == '__main__':
    unittest.main()
