"""A session's Stuck reason comes from its REPORT, never a stray line: the REPORT read off an
ended session's result (the real shapes the live factory saw), the reason built from the report's
own words, the guard that rejects an empty, fenced or noise reason, and what ``decide`` does with
a ``partial`` report, no report, an API failure and a ``done`` push."""
import json
import os
import tempfile
import unittest
from unittest import mock

from asf.kernel import actions as A
from asf.kernel import model as M
from asf.kernel import ports as P
from asf.kernel import reports as R
from asf.kernel.decide import API_FAILED, NO_REPORT as NO_REPORT_ATTEMPT, decide

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.kernel…` does not
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

#: the shape of a live build session's result (T-81129): prose, then a fenced REPORT
PARTIAL = """Confirmed up to date at `ff1adf377` on origin, redaction clean.

```
REPORT
item: T-0001
kind: correct
status: partial
branch: worker/T-0001
pushed: yes ff1adf3772f59c83f283a91266c0e4ba77c124c6
commits: none
tests: python3 -m unittest tests.test_conventions_flags -v → reproduces the CI failure locally: \
FAILED (failures=1) — AssertionError: {'regression.exempt_found_in': 'asf/harvest/regression.py'} \
!= {}; python3 -m unittest tests.test_regression_gate -v → OK (11 tests, PredicateTest all pass)
left out: the actual CI fix — registering `regression.exempt_found_in` in `conventions.KNOWN_FLAGS` \
requires editing asf/conventions.py, outside this task's writes: boundary.
needs writes: asf/conventions.py docs/guide/product-config.md
proves: none
NEEDS OPERATOR: none — the fix is mechanical but sits outside this task's writes: boundary, so it \
is named on `needs writes:` for the harvest to route rather than guessed here.
```
"""

#: the shape of a live session the API refused (B-82809): no REPORT, ids on the last lines
API = """API Error: the model's safeguards flagged this message. Apply to the verification \
program to reduce these interruptions.

Details: `[cyber]`

Request ID: req_011CfrwoFQKGk1TZsxEZVxWR

Message ID: msg_011CfrwoFegk4YPbTCyeJUCe"""

#: a session that ran out of turns mid-thought: no REPORT, a code fence last
NO_REPORT = """Running the suite now.

error: cannot import name widget from src.a
```
"""

DONE = """```
REPORT
item: T-0001
kind: coder
status: done
branch: worker/T-0001
pushed: yes 9f8cded06f8aefecb4cb3db14f1e46b49e71f7c3
commits: 9f8cded task(T-0001): the thing
tests: python3 -m unittest tests.test_a -> OK (11 tests)
left out: none
needs writes: none
NEEDS OPERATOR: none
```
"""

BLOCKED_ASKS = """REPORT
item: T-0001
status: blocked
pushed: no — nothing to push
left out: the token scope
NEEDS OPERATOR: grant the deploy key write access — `gh repo deploy-key list`
"""


class Read(unittest.TestCase):

    def test_a_partial_report_fills_status_and_fields_and_asks_nothing(self):
        said = R.read({'result': PARTIAL, 'is_error': False})
        self.assertEqual(said['status'], 'partial')
        self.assertTrue(said['fields']['pushed'].startswith('yes ff1adf3'))
        self.assertEqual(said['fields']['commits'], 'none')
        self.assertIn('FAILED (failures=1)', said['fields']['tests'])
        self.assertEqual(said['question'], '', 'NEEDS OPERATOR: none asks nothing')
        self.assertEqual(said['api_error'], '')
        self.assertTrue(said['last_line'].startswith('NEEDS OPERATOR: none'))

    def test_an_api_failure_is_named_by_its_error_line_never_its_message_id(self):
        said = R.read({'result': API, 'is_error': True})
        self.assertEqual((said['status'], said['fields']), ('', {}))
        self.assertTrue(said['api_error'].startswith('API Error: the model'))
        self.assertNotIn('Message ID', said['last_line'])
        self.assertTrue(said['last_line'].startswith('API Error'))

    def test_api_failures_by_flag_rate_limit_or_overload(self):
        self.assertTrue(R.api_error('Message ID: msg_011'))
        self.assertTrue(R.api_error('rate limit reached, retry later'))
        self.assertTrue(R.api_error('{"type":"overloaded_error"}'))
        self.assertTrue(R.api_error('stopped', is_error=True))
        self.assertEqual(R.api_error('the suite passed; I pushed the fix'), '')

    def test_no_report_keeps_the_last_line_that_says_something(self):
        said = R.read({'result': NO_REPORT})
        self.assertEqual((said['status'], said['fields'], said['api_error']), ('', {}, ''))
        self.assertEqual(said['last_line'], 'error: cannot import name widget from src.a')

    def test_a_question_is_read_off_needs_operator(self):
        said = R.read({'result': BLOCKED_ASKS})
        self.assertEqual(said['status'], 'blocked')
        self.assertTrue(said['question'].startswith('grant the deploy key'))

    def test_session_result(self):
        said = R.read({'result': PARTIAL})
        self.assertEqual(P.session_result('build', True, said), 'pushed')
        self.assertEqual(P.session_result('review', False, said), 'report')
        self.assertEqual(P.session_result('build', False, said), 'report')
        self.assertEqual(P.session_result('build', False, R.read({'result': BLOCKED_ASKS})),
                         'question')
        self.assertEqual(P.session_result('build', False, R.read({'result': NO_REPORT})), 'none')


class Reason(unittest.TestCase):

    def test_partial_reason_is_the_failing_tests_in_the_reports_words(self):
        said = R.read({'result': PARTIAL})
        reason = R.stuck_reason(said['status'], said['fields'], said['question'])
        self.assertTrue(reason.startswith('partial: tests: python3 -m unittest '
                                          'tests.test_conventions_flags'), reason)
        self.assertIn('FAILED (failures=1)', reason)
        self.assertLessEqual(len(reason), R.MAX_REASON)

    def test_without_a_failing_test_left_out_then_needs_writes(self):
        self.assertEqual(R.stuck_reason('blocked', {'tests': 'OK', 'left out': 'the token'}),
                         'blocked: left out: the token')
        self.assertEqual(R.stuck_reason('partial', {'left out': 'none',
                                                    'needs writes': 'asf/x.py'}),
                         'partial: needs writes: asf/x.py')
        self.assertEqual(R.stuck_reason('partial', {}), 'partial: the REPORT names no reason')

    def test_the_guard_rejects_empty_fence_and_noise(self):
        for bad in ('', '   ', '```', '```python', '---', '**', 'Message ID: msg_011Cfrwo…',
                    'Request ID: req_011', None):
            self.assertFalse(R.meaningful(bad), repr(bad))
            self.assertEqual(R.clean(bad, 'fallback'), 'fallback')
        for good in ('partial: tests: FAILED', 'ended without a REPORT', 'json or yaml?'):
            self.assertTrue(R.meaningful(good), good)

    def test_no_report_reason_never_carries_a_fence(self):
        self.assertEqual(R.no_report_reason('```'), 'ended without a REPORT')
        self.assertEqual(R.no_report_reason('boom'), 'ended without a REPORT: boom')


def ended(text, kind='build', pushed=False, is_error=False):
    """The Session the port builds for a session that ended with ``text``."""
    said = R.read({'result': text, 'is_error': is_error})
    return B.session('j1', 'T-0001', kind=kind, alive=False, ended=True,
                     result=P.session_result(kind, pushed, said), question=said['question'] or None,
                     last_line=said['last_line'], report=text, status=said['status'],
                     fields=said['fields'], api_error=said['api_error'])


class Decide(unittest.TestCase):

    def plan(self, session, item=None, **kw):
        return decide(B.facts([item or B.task('T-0001', state=State.BUILDING)],
                              sessions=[session], **kw), B.config())

    def test_partial_is_stuck_on_the_session_with_the_reports_words(self):
        plan = self.plan(ended(PARTIAL))
        info = B.stuck(plan, 'T-0001')
        self.assertEqual(info.owner, 'session')
        self.assertTrue(info.reason.startswith('partial: tests: '), info.reason)
        self.assertIn('test_conventions_flags', info.reason)
        self.assertEqual([m.reason for m in B.of(plan, A.MarkStuck)], [info.reason])

    def test_partial_even_with_a_push_is_stuck(self):
        info = B.stuck(self.plan(ended(PARTIAL, pushed=True)), 'T-0001')
        self.assertEqual(info.owner, 'session')

    def test_blocked_with_a_question_is_the_operators(self):
        info = B.stuck(self.plan(ended(BLOCKED_ASKS)), 'T-0001')
        self.assertEqual(info.owner, 'operator')
        self.assertTrue(info.reason.startswith('blocked: NEEDS OPERATOR: grant the deploy key'))

    def test_no_report_names_it_and_its_last_line(self):
        again = B.task('T-0001', state=State.BUILDING, attempts=[NO_REPORT_ATTEMPT])
        info = B.stuck(self.plan(ended(NO_REPORT), item=again), 'T-0001')
        self.assertEqual((info.owner, info.reason),
                         ('session', 'ended without a REPORT: error: cannot import name widget '
                                     'from src.a'))

    def test_api_error_relaunches_once_then_sticks_on_the_loop(self):
        plan = self.plan(ended(API, is_error=True))
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan), [], 'not relaunched on the tick its session ended')
        # the applier recorded the attempt: the next tick relaunches
        retry = B.task('T-0001', state=State.READY, attempts=[API_FAILED])
        self.assertEqual(B.launched(decide(B.facts([retry]), B.config())), ['T-0001'])
        # the relaunch failed on the API again: Stuck on the loop, never a work verdict
        plan = self.plan(ended(API, is_error=True),
                         item=B.task('T-0001', state=State.BUILDING, attempts=[API_FAILED]))
        info = B.stuck(plan, 'T-0001')
        self.assertEqual(info.owner, 'loop')
        self.assertTrue(info.reason.startswith('the session API failed: API Error: '),
                        info.reason)
        self.assertNotIn('Message ID', info.reason)

    def test_done_with_a_push_goes_on_to_review(self):
        plan = self.plan(ended(DONE, pushed=True), prs=[B.pr(7, 'T-0001', checks=[])])
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertEqual(B.state(plan, 'T-0001'), State.REVIEW)

    def test_done_without_a_push_or_a_pr_names_its_pushed_line(self):
        info = B.stuck(self.plan(ended(DONE)), 'T-0001')
        self.assertEqual(info.owner, 'session')
        self.assertTrue(info.reason.startswith('done without a push: pushed: yes 9f8cded'))

    def test_a_recorded_noise_reason_is_judged_afresh(self):
        for noise in ('```', 'Message ID: msg_011Cfrwo…', ''):
            it = B.task('T-0001', state=State.STUCK, stuck=M.Stuck(noise, 'session'))
            plan = decide(B.facts([it]), B.config())
            self.assertEqual(B.state(plan, 'T-0001'), State.READY, repr(noise))

    def test_no_reason_decide_writes_is_noise(self):
        s = B.session('j1', 'T-0001', alive=False, ended=True, result='none', last_line='```')
        again = B.task('T-0001', state=State.BUILDING, attempts=[NO_REPORT_ATTEMPT])
        info = B.stuck(self.plan(s, item=again), 'T-0001')
        self.assertTrue(R.meaningful(info.reason))
        self.assertEqual(info.reason, 'ended without a REPORT')


class Apply(unittest.TestCase):

    def test_an_api_failure_is_an_attempt_on_the_card(self):
        from asf.kernel.apply import apply
        rec = F.FakeRecord([B.task('T-0001', state=State.BUILDING)])
        sess = F.FakeSessions([ended(API, is_error=True)])
        ports = F.ports(record=rec, sessions=sess)
        facts = B.facts([B.task('T-0001', state=State.BUILDING)], sessions=sess.sessions())
        apply(decide(facts, B.config()), facts, ports, now='t', log=lambda *_: None)
        self.assertEqual(sess.ended, [('j1', True)])
        self.assertEqual(rec.fields['T-0001'][P.ATTEMPTS], [API_FAILED])


class Port(unittest.TestCase):
    """The session port reads the REPORT off the job's stream-json log."""

    def test_sessions_fill_the_report_fields(self):
        tmp = tempfile.mkdtemp()
        log = os.path.join(tmp, 'build-t-0001-1.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': PARTIAL}) + '\n')
        run = {'job': 'build-t-0001-1', 'item': 'T-0001', 'kind': 'task', 'pid': 1, 'log': log}
        with mock.patch('asf.workers.pool.load_sessions', return_value={run['job']: run}), \
                mock.patch('asf.workers.lifecycle.is_live', return_value=True), \
                mock.patch('asf.workers.lifecycle.pid_alive', return_value=False), \
                mock.patch('asf.workers.pushlog.count', return_value=0):
            got = P.RealSessions(product=None).sessions()
        self.assertEqual(len(got), 1)
        s = got[0]
        self.assertEqual((s.ended, s.result, s.status, s.api_error), (True, 'report', 'partial', ''))
        self.assertTrue(s.fields['pushed'].startswith('yes ff1adf3'))
        self.assertNotEqual(s.last_line, '```')


if __name__ == '__main__':
    unittest.main()
