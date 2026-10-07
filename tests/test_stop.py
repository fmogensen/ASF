"""B-0069: killing a session's process must stop the run, not just the client."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest

from asf.workers import lifecycle as lc


class TestStop(unittest.TestCase):
    def test_b0069_stop_kills_the_whole_group_and_marks_the_run_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            work = os.path.join(d, 'pushes')
            # a client with a child that keeps "pushing" after the client itself is gone
            script = 'sleep 300 & while true; do echo x >> "$0"; sleep 0.05; done'
            proc = subprocess.Popen(['sh', '-c', script, work], start_new_session=True)
            try:
                time.sleep(0.3)
                reg = os.path.join(d, 'sessions.jsonl')
                run = {'job': 'fix-bug-b-0069', 'pid': proc.pid, 'started': 't', 'log': work,
                       'pgid': proc.pid}
                with open(reg, 'w') as f:
                    f.write(json.dumps(run) + '\n')
                ok, detail = lc.stop(reg, run, grace=0.5, poll=0.05,
                                     alive=lambda pid: proc.poll() is None)
                self.assertTrue(ok, detail)
                size = os.path.getsize(work)
                time.sleep(0.4)
                self.assertEqual(os.path.getsize(work), size, 'the work kept writing after stop')
                last = lc.latest(reg)['fix-bug-b-0069']
                self.assertEqual(last['end_reason'], 'stopped')
                self.assertTrue(last['ended'])
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()


try:  # `unittest discover -s tests` puts tests/ on the path
    from test_workers import Home, feature_row
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row


class AsfStop(Home):
    """``asf stop <job|item>`` (asf.workers.stop, F-0275): the run's process group TERM then
    KILL (lifecycle.stop), ended ``stopped`` with ``stopped_by: operator``, its seat released —
    and nothing the factory reads as a fault: no correction, no round, no relaunch-cap count."""

    def launch(self, job='spec-1', item='F-0001', pid=4242):
        from asf.workers import runtime as runtime_mod, spawn as spawn_mod
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': pid}])
        return spawn_mod.spawn(self.product, feature_row(job, item), self.acct(), 'b\n',
                               runtime=rt, cfg=self.cfg)

    def stop(self, target, stop_fn=None):
        import argparse
        from unittest import mock
        from asf.workers import stop as stop_mod
        lines = []
        with mock.patch('asf.env.load_product', return_value=self.product):
            rc = stop_mod.cmd_stop(argparse.Namespace(target=target, product='sample'),
                                   stop_fn=stop_fn or self.quick, out=lines.append)
        return rc, lines

    @staticmethod
    def quick(path, run):
        return lc.stop(path, run, grace=0.2, poll=0.05)

    def test_a_job_is_stopped_ended_and_its_seat_released(self):
        proc = subprocess.Popen(['sleep', '300'], start_new_session=True)
        time.sleep(0.3)  # the child's setsid has run: its group exists
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.launch(pid=proc.pid)
        # our own child: reaped by poll(), or its zombie would read as a live group
        rc, lines = self.stop('spec-1', stop_fn=lambda path, run: lc.stop(
            path, run, grace=0.2, poll=0.05, alive=lambda pid: proc.poll() is None))
        self.assertEqual(rc, 0, lines)
        self.assertIsNotNone(proc.wait(timeout=10))  # the group got the signal
        from asf.workers import pool as pool_mod
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertEqual((run['end_reason'], run['stopped_by']), (lc.STOPPED, 'operator'))
        self.assertTrue(run.get('ended'))
        self.assertFalse(lc.is_live(run))
        self.assertFalse(lc.occupies(run, alive=lambda pid: True))  # no seat held
        self.assertIn('stopped spec-1', lines[0])

    def test_no_correction_no_round_and_no_cap_count(self):
        from asf.workers import health as health_mod, pool as pool_mod, relaunch
        self.launch(pid=999999)
        self.assertEqual(self.stop('spec-1')[0], 0)
        health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None,
                          items={})
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertIsNone(run.get('correction'))
        self.assertFalse(run.get('rounds'))
        self.assertEqual(run['end_reason'], lc.STOPPED)  # health did not re-judge it
        self.assertFalse(relaunch.counted(run))
        self.assertEqual(relaunch.streak(pool_mod.sessions_path(self.product), 'spec-1',
                                         'F-0001'), [])
        self.assertIn('stopped_by', lc.RUN_FIELDS)

    def test_an_item_stops_its_live_runs_and_an_unknown_target_nothing(self):
        from asf.workers import pool as pool_mod
        self.launch('spec-1', 'F-0001', pid=999998)
        self.launch('spec-2', 'F-0002', pid=999997)
        self.assertEqual(self.stop('F-0001')[0], 0)
        sessions = pool_mod.load_sessions(self.product)
        self.assertEqual(sessions['spec-1']['end_reason'], lc.STOPPED)
        self.assertTrue(lc.is_live(sessions['spec-2']))
        rc, lines = self.stop('nothing-here')
        self.assertEqual(rc, 1)
        self.assertIn('no running session for nothing-here', lines[0])

    def test_an_unverified_stop_ends_nothing(self):
        from asf.workers import pool as pool_mod
        self.launch(pid=999996)
        rc, lines = self.stop('spec-1', stop_fn=lambda path, run: (False, 'group still alive'))
        self.assertEqual(rc, 1)
        self.assertIn('not stopped — group still alive', lines[0])
        self.assertTrue(lc.is_live(pool_mod.load_sessions(self.product)['spec-1']))

    def test_a_cloud_run_is_cancelled_through_the_cloud_lane(self):
        from unittest import mock
        from asf.workers import cloud, pool as pool_mod
        self.launch()
        pool_mod.update_session(self.product, 'spec-1', pid='actions:500')
        with mock.patch.object(cloud, 'stop', return_value=(True, 'run 500 cancelled')) as cs:
            rc, lines = self.stop('spec-1', stop_fn=lc.stop)
        self.assertEqual(rc, 0)
        self.assertEqual(cs.call_args[0][0]['pid'], 'actions:500')
        self.assertIn('run 500 cancelled', lines[0])
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertEqual((run['end_reason'], run['stopped_by']), (lc.STOPPED, 'operator'))

    def test_the_command_is_registered(self):
        from asf import cli
        from asf.workers import stop as stop_mod
        args = cli.build_parser().parse_args(['stop', 'spec-1'])
        self.assertIs(args.func, stop_mod.cmd_stop)
        self.assertEqual(args.target, 'spec-1')


if __name__ == '__main__':
    unittest.main()
