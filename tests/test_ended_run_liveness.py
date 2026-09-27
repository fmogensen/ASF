"""An ended run is finished, whatever its pid still answers (T-0196).

Seen live, twice:

* local — review-t-0196's ``claude -p`` wrote its result line and health ended the run, but the
  process stayed up 54 minutes. The pool counted it as an extra observed session carrying the
  run's own job, the wave said ``already running`` every tick, and health kept its worktree
  ``ended: pid still alive`` until the console killed the pid by hand;
* cloud — coder-t-0362 and coder-t-0371 (``pid: remote:trig_…``) were ended ``failed:
  quota-exhausted`` in the same health pass that first synced them. The cloud status file still
  said ``working`` for their tokens, and :func:`asf.workers.cloud.sync` only looks at runs with
  no ``ended`` line, so the token answered alive for a day: 114 ticks of ``keep … ended: pid
  still alive``.

The rule: an ended run's token is settled at once (its routine disabled through the one safe call,
:func:`asf.workers.remote.retire`), and an ended local run's process the factory itself launched —
its pid, its ``ASF_SESSION``, a ``claude -p`` command line — is stopped once the grace period has
passed: SIGTERM, then SIGKILL. A process that is not provably the run's own is never touched.
"""
import os
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_workers import Home  # noqa: E402

from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import pool as pool_mod

ENDED = '2026-09-26T12:55:43Z'
T_ENDED = cloud._parse_ts(ENDED)
SID = 'sample/review-t-0196@20260927T115627Z'


def sleeper():
    p = subprocess.Popen(['sleep', '600'])
    return p


class CloudEndedTokenTest(Home):
    def cloud_run(self, job='coder-t-0362', trig='trig_01AU6RysamQ4NmVJf9qTjECg', **extra):
        tok = f'remote:{trig}'
        pool_mod.append_session(self.product, dict({
            'job': job, 'item': 'T-0362', 'kind': 'coder', 'account': 'acct-a', 'pid': tok,
            'runtime': 'claude-remote', 'runtime_lane': 'cloud',
            'started': '2026-09-26T12:47:46Z'}, **extra))
        cloudpid.record(tok, cloudpid.WORKING, f'run {trig} in_progress', product='sample', job=job)
        return tok

    def test_an_ended_cloud_run_whose_token_says_working_is_not_alive(self):
        tok = self.cloud_run()
        pool_mod.update_session(self.product, 'coder-t-0362', ended=ENDED,
                                end_reason='failed: quota-exhausted')
        self.assertTrue(lifecycle.pid_alive(tok))  # the defect's precondition
        retired = []
        found = health_mod.settle_ended(self.product, pool_mod.load_sessions(self.product),
                                        now=T_ENDED + 60, retire=retired.append)
        self.assertFalse(lifecycle.pid_alive(tok))
        self.assertEqual(cloudpid.status(tok), cloudpid.DEAD)
        self.assertEqual([r['job'] for r in retired], ['coder-t-0362'])
        self.assertEqual([(j, w) for j, w, _ in found], [('coder-t-0362', 'settled')])
        # a second pass has nothing left to settle, and never disables the routine again
        again = health_mod.settle_ended(self.product, pool_mod.load_sessions(self.product),
                                        now=T_ENDED + 120, retire=retired.append)
        self.assertEqual(again, [])
        self.assertEqual(len(retired), 1)

    def test_a_finished_cloud_run_settles_finished(self):
        tok = self.cloud_run(job='coder-t-0371', trig='trig_016jj7xwCCK3cdvhndN4wu6S')
        pool_mod.update_session(self.product, 'coder-t-0371', ended=ENDED,
                                end_reason=lifecycle.FINISHED)
        health_mod.settle_ended(self.product, pool_mod.load_sessions(self.product),
                                now=T_ENDED + 1, retire=lambda run: True)
        self.assertEqual(cloudpid.status(tok), cloudpid.FINISHED)

    def test_a_live_cloud_run_is_left_working(self):
        tok = self.cloud_run()
        found = health_mod.settle_ended(self.product, pool_mod.load_sessions(self.product),
                                        now=T_ENDED, retire=lambda run: self.fail('retired'))
        self.assertEqual(found, [])
        self.assertTrue(lifecycle.pid_alive(tok))

    def test_health_no_longer_keeps_the_ended_cloud_runs_worktree(self):
        tok = self.cloud_run()
        wt = os.path.join(self.tmp, 'asf-home', 'state', 'sample', 'worktrees', 'coder-t-0362')
        os.makedirs(wt)
        pool_mod.update_session(self.product, 'coder-t-0362', worktree=wt, ended=ENDED,
                                end_reason='failed: quota-exhausted')
        lines = []
        orig = health_mod.remote_retire
        health_mod.remote_retire = lambda run: True
        try:
            found = health_mod.health(self.product, out=lines.append, items={})
        finally:
            health_mod.remote_retire = orig
        self.assertNotIn(('coder-t-0362', 'keep', 'ended: pid still alive'), found)
        self.assertFalse(lifecycle.pid_alive(tok))


class LocalLingeringProcessTest(Home):
    def setUp(self):
        super().setUp()
        self.proc = sleeper()
        self.addCleanup(self._reap)

    def _reap(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def local_run(self, ended=ENDED, session=SID, pid=None):
        pool_mod.append_session(self.product, {
            'job': 'review-t-0196', 'item': 'T-0196', 'kind': 'review', 'account': 'acct-a',
            'pid': pid or self.proc.pid, 'session': session, 'started': '2026-09-27T11:56:27Z'})
        if ended:
            pool_mod.update_session(self.product, 'review-t-0196', ended=ended,
                                    end_reason=lifecycle.FINISHED)

    def observed(self, sid=SID):
        env = {'ASF_SESSION': sid} if sid else {}
        return observe.FakeSource([{'pid': self.proc.pid, 'ppid': 1, 'env': env}])

    def settle(self, now, source=None, cmdline=None):
        return health_mod.settle_ended(
            self.product, pool_mod.load_sessions(self.product), now=now,
            session_source=source or self.observed(),
            cmdline=cmdline or (lambda pid: 'claude -p --permission-mode bypassPermissions'),
            wait_s=2)

    def assert_stopped(self):
        self.proc.wait(timeout=5)
        self.assertIsNotNone(self.proc.returncode)

    def assert_running(self):
        time.sleep(0.05)
        self.assertIsNone(self.proc.poll())

    def test_an_ended_runs_own_claude_p_is_stopped_after_the_grace(self):
        self.local_run()
        found = self.settle(T_ENDED + health_mod.LINGER_GRACE_S + 1)
        self.assert_stopped()
        self.assertEqual([(j, w) for j, w, _ in found], [('review-t-0196', 'stopped')])

    def test_within_the_grace_it_is_left_alone(self):
        self.local_run()
        self.assertEqual(self.settle(T_ENDED + 60), [])
        self.assert_running()

    def test_a_run_not_ended_is_never_touched(self):
        self.local_run(ended=None)
        self.assertEqual(self.settle(T_ENDED + 86400), [])
        self.assert_running()

    def test_a_process_carrying_another_session_is_not_the_runs(self):
        self.local_run()
        self.settle(T_ENDED + 86400, source=self.observed('sample/other@20260927T120000Z'))
        self.assert_running()

    def test_an_interactive_session_at_the_pid_is_never_touched(self):
        self.local_run()
        self.settle(T_ENDED + 86400, source=self.observed(None))
        self.assert_running()

    def test_a_run_with_no_session_id_is_never_touched(self):
        self.local_run(session=None)
        self.settle(T_ENDED + 86400, source=self.observed(None))
        self.assert_running()

    def test_a_command_line_that_is_not_claude_p_is_never_touched(self):
        self.local_run()
        self.settle(T_ENDED + 86400, cmdline=lambda pid: 'claude --resume abc')
        self.assert_running()
        self.settle(T_ENDED + 86400, cmdline=health_mod.command_line)  # the real one: `sleep`
        self.assert_running()

    def test_sigkill_follows_when_sigterm_is_ignored(self):
        self.proc.kill()
        self.proc.wait()
        self.proc = subprocess.Popen([sys.executable, '-c',
                                      'import signal,time\n'
                                      'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                                      'print("ready", flush=True)\n'
                                      'time.sleep(600)'], stdout=subprocess.PIPE, text=True)
        self.proc.stdout.readline()
        self.local_run()
        health_mod.settle_ended(
            self.product, pool_mod.load_sessions(self.product),
            now=T_ENDED + health_mod.LINGER_GRACE_S + 1, session_source=self.observed(),
            cmdline=lambda pid: 'claude -p', wait_s=0.5)
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.assertEqual(self.proc.returncode, -signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
