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


if __name__ == '__main__':
    unittest.main()
