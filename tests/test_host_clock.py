"""The host clock ``asf.host.net-probe``: install / pause / resume / uninstall --host and the
doctor row, against a fake launchctl and a temp HOME — nothing touches the real ~/Library."""
import argparse
import io
import os
import unittest
from contextlib import redirect_stdout

from asf import doctor, scheduler
from tests.test_scheduler import SchedulerTestCase, stub_argv, fake_print, read_fixture

LABEL = 'asf.host.net-probe'


class HostClockTest(SchedulerTestCase):
    def cfg(self, probe='on'):
        return {'scheduler': {'kind': 'launchd', 'label_prefix': 'asf'},
                'network': {'probe': probe}}

    def plist(self):
        return scheduler.plist_path(LABEL)

    def cli(self, command, **kw):
        args = argparse.Namespace(scheduler_command=command, product=None, clock=None,
                                  reason=kw.get('reason'), by=None, label=None, json=False,
                                  host=True)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = scheduler.cmd_scheduler(args)
        return rc, out.getvalue()

    def test_install_writes_plist_and_bootstraps(self):
        lines = scheduler.install_host(self.cfg())
        self.assertTrue(os.path.exists(self.plist()))
        self.assertTrue(any('bootstrapped' in l for l in lines))
        self.assertTrue(any(a.startswith('bootstrap') for a in stub_argv(self.statedir)))

    def test_install_is_idempotent(self):
        first = scheduler.install_host(self.cfg())
        with open(self.plist(), 'rb') as f:
            body = f.read()
        second = scheduler.install_host(self.cfg())
        with open(self.plist(), 'rb') as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(len(first), len(second))

    def test_probe_off_removes_the_clock(self):
        scheduler.install_host(self.cfg())
        lines = scheduler.install_host(self.cfg('off'))
        self.assertFalse(os.path.exists(self.plist()))
        self.assertTrue(any('removed' in l for l in lines))

    def test_probe_off_with_nothing_installed_is_quiet(self):
        lines = scheduler.install_host(self.cfg('off'))
        self.assertEqual(len(lines), 1)
        self.assertIn('network.probe is off', lines[0])

    def test_uninstall_removes_plist_and_pause(self):
        scheduler.install_host(self.cfg())
        scheduler.pause(scheduler.HOST_PRODUCT, [scheduler.HOST_CLOCK], 'test', 'me')
        scheduler.uninstall_host(self.cfg())
        self.assertFalse(os.path.exists(self.plist()))
        self.assertEqual(scheduler.read_pauses(scheduler.HOST_PRODUCT), {})

    def test_pause_then_install_stays_unloaded_then_resume_loads(self):
        scheduler.install_host(self.cfg())
        rc, out = self.cli('pause', reason='maintenance')
        self.assertEqual(rc, 0)
        self.assertIn('paused', out)
        self.assertIsNotNone(scheduler.pause_record(LABEL))
        lines = scheduler.install_host(self.cfg())
        self.assertTrue(any('not loaded' in l for l in lines))
        rc, out = self.cli('resume')
        self.assertEqual(rc, 0)
        self.assertIsNone(scheduler.pause_record(LABEL))

    def test_cli_install_and_uninstall_host(self):
        self.write_config('network:\n  probe: on\n')
        rc, _ = self.cli('install')
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.plist()))
        rc, _ = self.cli('uninstall')
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.plist()))

    def test_doctor_row(self):
        self.assertIsNone(doctor.check_host_clock(self.cfg('off')))
        ok, detail = doctor.check_host_clock(self.cfg())
        self.assertFalse(ok)
        self.assertIn('not installed', detail)
        scheduler.install_host(self.cfg())
        fake_print(self.statedir, LABEL, read_fixture('launchctl-print.txt'))
        ok, detail = doctor.check_host_clock(self.cfg())
        self.assertTrue(ok, detail)
        scheduler.pause(scheduler.HOST_PRODUCT, [scheduler.HOST_CLOCK], 'x', 'me')
        ok, detail = doctor.check_host_clock(self.cfg())
        self.assertFalse(ok)
        self.assertIn('paused', detail)


if __name__ == '__main__':
    unittest.main()
