"""The ``scheduler`` connector kind: launchd moved behind it unchanged, systemd user timers added
(argv injected — nothing touches the machine's scheduler), the registry's choice, and the
:mod:`asf.scheduler` operations dispatched through it."""
import os
import subprocess
import unittest
from unittest import mock

from asf import connectors, scheduler
from asf.connectors import fakes, launchd, protocols, systemd

try:
    from test_scheduler import SchedulerTestCase
except ImportError:  # pragma: no cover — run as tests.test_connectors_scheduler
    from tests.test_scheduler import SchedulerTestCase


class Runner:
    """A ``subprocess.run`` stand-in: records argv, answers ``answers[tuple(argv)]``."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], dict(answers or {})

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        rc, out = self.answers.get(tuple(argv), (0, ''))
        return subprocess.CompletedProcess(argv, rc, out, '')


def every_clock(seconds=300):
    return scheduler.Clock('tick', ['record'], False, seconds, None)


def at_clock(hour=3, minute=5):
    return scheduler.Clock('daily', ['daily'], False, None, {'Hour': hour, 'Minute': minute})


class RegistryTest(unittest.TestCase):
    def setUp(self):
        connectors.reset()
        self.addCleanup(connectors.reset)

    def test_launchd_is_the_default(self):
        self.assertEqual(connectors.configured('scheduler', {})[0], 'launchd')
        self.assertIsInstance(connectors.get('scheduler', {}), launchd.LaunchdScheduler)

    def test_the_older_scheduler_kind_still_chooses(self):
        cfg = {'scheduler': {'kind': 'systemd'}}
        self.assertEqual(connectors.configured('scheduler', cfg)[:2], ('systemd', 'scheduler.kind'))
        self.assertIsInstance(connectors.get('scheduler', cfg), systemd.SystemdScheduler)
        self.assertEqual(scheduler.kind(cfg), 'systemd')
        self.assertEqual(scheduler.kind({'scheduler': {'provider': 'cron'}}), 'cron')

    def test_connectors_scheduler_wins(self):
        cfg = {'connectors': {'scheduler': 'systemd'}, 'scheduler': {'kind': 'launchd'}}
        self.assertEqual(scheduler.kind(cfg), 'systemd')

    def test_each_in_tree_implementation_is_a_scheduler(self):
        for name in ('launchd', 'cron', 'none', 'systemd', 'fake'):
            got = connectors.get('scheduler', {'connectors': {'scheduler': name}})
            self.assertIsInstance(got, protocols.Scheduler, name)

    def test_an_unknown_kind_keeps_the_launchd_path(self):
        self.assertIsNone(scheduler.backend_for('gh-actions', {}))
        for name in ('launchd', 'cron', 'none'):
            self.assertIsNone(scheduler.backend_for(name, {}))


class LaunchdTest(unittest.TestCase):
    def test_launchctl_is_the_one_process_call(self):
        run = Runner({('launchctl', 'list'): (0, 'PID\tStatus\tLabel\n-\t0\tasf.a.tick\n')})
        with mock.patch('asf.connectors.launchd.subprocess.run', run):
            self.assertEqual(scheduler._launchctl(['list'])[0], 0)
            self.assertEqual(launchd.LaunchdScheduler().loaded_labels(), ['asf.a.tick'])
        self.assertEqual(run.calls, [['launchctl', 'list']] * 2)

    def test_a_missing_launchctl_is_127(self):
        with mock.patch('asf.connectors.launchd.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(launchd.launchctl(['list'])[0], 127)

    def test_stop_and_load_are_bootout_and_bootstrap(self):
        calls = []
        with mock.patch.object(scheduler, '_launchctl',
                               side_effect=lambda a, timeout=30: calls.append(a) or (0, '', '')):
            launchd.LaunchdScheduler().stop('asf.a.tick')
            launchd.LaunchdScheduler().load('/x/asf.a.tick.plist')
        uid = os.getuid()
        self.assertEqual(calls, [['bootout', f'gui/{uid}/asf.a.tick'],
                                 ['bootstrap', f'gui/{uid}', '/x/asf.a.tick.plist']])

    def test_argv_for_injected_runners(self):
        self.assertEqual(launchd.list_argv(), ['launchctl', 'list'])
        self.assertEqual(launchd.list_argv('old.job'), ['launchctl', 'list', 'old.job'])
        self.assertEqual(launchd.bootout_hint('old.job'), 'launchctl bootout gui/$(id -u)/old.job')

    def test_render_writes_the_same_plist_as_before(self):
        base = {'label': 'asf.a.tick', 'log': '/l', 'argv': ['py', 'x']}
        job = launchd.LaunchdScheduler().render(dict(base, every_s=300), '/w', {'HOME': '/h'})
        self.assertEqual(job['plist'], {
            'Label': 'asf.a.tick', 'ProgramArguments': ['py', 'x'], 'WorkingDirectory': '/w',
            'EnvironmentVariables': {'HOME': '/h'}, 'StandardOutPath': '/l',
            'StandardErrorPath': '/l', 'RunAtLoad': True, 'StartInterval': 300})
        job = launchd.LaunchdScheduler().render(dict(base, at={'Hour': 3, 'Minute': 5}), '/w', {})
        self.assertEqual(job['plist']['StartCalendarInterval'], {'Hour': 3, 'Minute': 5})
        self.assertNotIn('RunAtLoad', job['plist'])


class SystemdUnitsTest(unittest.TestCase):
    def test_service_and_every_timer(self):
        job = {'label': 'asf.a.tick', 'log': '/logs/t.log', 'argv': ['/py', '-m', 'asf.cli',
                                                                   'tick', '--steps', 'a b'],
               'every_s': 300}
        s = systemd.SystemdScheduler({'scheduler': {'systemd_unit_dir': '/u'}})
        job = s.render(job, '/work', {'PATH': '/bin:/usr/bin', 'HOME': '/h'})
        svc, timer = job['units']['service'], job['units']['timer']
        self.assertEqual(job['path'], '/u/asf.a.tick.timer')
        self.assertIn('Type=oneshot\nWorkingDirectory=/work\n', svc)
        self.assertIn('Environment="PATH=/bin:/usr/bin"\nEnvironment="HOME=/h"\n', svc)
        self.assertIn('ExecStart=/py -m asf.cli tick --steps "a b"\n', svc)
        self.assertIn('StandardOutput=append:/logs/t.log\nStandardError=append:/logs/t.log', svc)
        self.assertIn('OnActiveSec=1s\nOnUnitActiveSec=300s\nPersistent=true\n'
                      'Unit=asf.a.tick.service\n', timer)
        self.assertIn('WantedBy=timers.target', timer)
        self.assertEqual(systemd.parse_exec_start(svc),
                         (['/py', '-m', 'asf.cli', 'tick', '--steps', 'a b'], '/work'))

    def test_at_timer_is_on_calendar(self):
        job = {'label': 'asf.a.daily', 'log': '/l', 'argv': ['x'], 'at': {'Hour': 3, 'Minute': 5}}
        timer = systemd.timer_text(job)
        self.assertIn('OnCalendar=*-*-* 03:05:00\n', timer)
        self.assertNotIn('OnUnitActiveSec', timer)

    def test_percent_and_quotes_are_escaped(self):
        self.assertEqual(systemd._quote('50%'), '50%%')
        self.assertEqual(systemd._quote('a"b c'), '"a\\"b c"')

    def test_unit_dir_default(self):
        self.assertEqual(systemd.unit_dir({}), os.path.expanduser('~/.config/systemd/user'))


class SystemdOpsTest(SchedulerTestCase):
    """systemd through :mod:`asf.scheduler`'s own operations, ``systemctl`` injected."""

    def write_config(self, extra=''):
        self.units = os.path.join(self.tmp, 'units')
        with open(os.path.join(self.asf_home, 'config.yaml'), 'w') as f:
            f.write('scheduler:\n  kind: systemd\n'
                    f'  systemd_unit_dir: {self.units}\n' + extra)

    def setUp(self):
        super().setUp()
        connectors.reset()
        self.addCleanup(connectors.reset)
        self.run = Runner()
        connectors.register('scheduler', 'systemd',
                            lambda cfg: systemd.SystemdScheduler(cfg, run=self.run))

    def _job(self):
        return scheduler.render('sample', every_clock(), venv=None)

    def test_render_install_status_pause_resume_uninstall(self):
        job = self._job()
        self.assertEqual(job['kind'], 'systemd')
        self.assertNotIn('needs_operator', job)
        label = job['label']
        lines = scheduler.install(job)
        self.assertTrue(os.path.exists(os.path.join(self.units, f'{label}.timer')))
        self.assertTrue(os.path.exists(os.path.join(self.units, f'{label}.service')))
        self.assertIn(f'scheduler: enabled {label}.timer', lines)
        self.assertEqual(self.run.calls, [['systemctl', '--user', 'daemon-reload'],
                                          ['systemctl', '--user', 'enable', '--now',
                                           f'{label}.timer']])
        self.assertEqual(scheduler.definition_path(label),
                         os.path.join(self.units, f'{label}.timer'))
        self.assertEqual(scheduler.product_labels('sample'), [label])

        self.run.calls.clear()
        scheduler.pause('sample', ['tick'], 'test', 'op')
        self.assertEqual(self.run.calls, [['systemctl', '--user', 'disable', '--now',
                                           f'{label}.timer'],
                                          ['systemctl', '--user', 'stop', f'{label}.service']])
        self.run.calls.clear()
        self.run.answers[('systemctl', '--user', 'show', f'{label}.timer', '--property',
                          'ActiveState,LoadState,UnitFileState')] = (0, 'ActiveState=inactive\n'
                                                                         'LoadState=loaded\n')
        lines = scheduler.resume('sample', ['tick'])
        self.assertEqual(lines, [f'scheduler: resumed {label}'])
        self.assertEqual(self.run.calls[-1], ['systemctl', '--user', 'enable', '--now',
                                              f'{label}.timer'])
        self.assertEqual(scheduler.read_pauses('sample'), {})

        self.run.calls.clear()
        lines = scheduler.uninstall(label)
        self.assertEqual(self.run.calls, [['systemctl', '--user', 'disable', '--now',
                                           f'{label}.timer'],
                                          ['systemctl', '--user', 'daemon-reload']])
        self.assertFalse(os.path.exists(os.path.join(self.units, f'{label}.timer')))

    def test_a_paused_clock_is_written_but_not_enabled(self):
        job = self._job()
        scheduler.pause('sample', ['tick'], 'test', 'op', bootout=False)
        lines = scheduler.install(job)
        self.assertNotIn(['systemctl', '--user', 'enable', '--now', f"{job['label']}.timer"],
                         self.run.calls)
        self.assertTrue(any('not enabled' in ln for ln in lines))

    def test_status_and_loaded_jobs_read_systemctl(self):
        job = self._job()
        scheduler.install(job)
        label = job['label']
        self.run.answers[('systemctl', '--user', 'show', f'{label}.timer', '--property',
                          'ActiveState,LoadState,UnitFileState')] = (0, 'ActiveState=active\n'
                                                                         'LoadState=loaded\n')
        self.run.answers[('systemctl', '--user', 'show', f'{label}.service', '--property',
                          'ActiveState,ExecMainStatus,NRestarts,FragmentPath,'
                          'ExecMainExitTimestampMonotonic')] = (
            0, 'ActiveState=inactive\nExecMainStatus=0\nExecMainExitTimestampMonotonic=12345\n')
        info = scheduler.status(label)
        self.assertEqual((info['loaded'], info['last_exit'], info['never_exited']),
                         (True, 0, False))
        self.run.answers[('systemctl', '--user', 'list-timers', '--all', '--no-legend',
                          '--plain')] = (0, f'Mon 2026-10-06 10:00:00 UTC 4min left - - '
                                            f'{label}.timer {label}.service\n')
        jobs = scheduler.loaded_jobs()
        self.assertEqual([j['label'] for j in jobs], [label])
        self.assertEqual(jobs[0]['argv'], job['argv'])

    def test_status_of_an_unknown_timer_is_not_loaded(self):
        self.run.answers[('systemctl', '--user', 'show', 'asf.sample.x.timer', '--property',
                          'ActiveState,LoadState,UnitFileState')] = (0, 'LoadState=not-found\n')
        self.assertFalse(scheduler.status('asf.sample.x')['loaded'])


class FakeAndCommandTest(SchedulerTestCase):
    def write_config(self, extra=''):
        with open(os.path.join(self.asf_home, 'config.yaml'), 'w') as f:
            f.write('connectors:\n  scheduler: fake\n')

    def setUp(self):
        super().setUp()
        connectors.reset()
        self.addCleanup(connectors.reset)
        self.fake = fakes.FakeScheduler()
        connectors.register('scheduler', 'fake', lambda cfg: self.fake)

    def test_the_fake_receives_every_operation(self):
        job = scheduler.render('sample', at_clock(), venv=None)
        self.assertEqual(job['kind'], 'fake')
        scheduler.install(job)
        self.assertTrue(scheduler.status(job['label'])['loaded'])
        self.assertEqual(scheduler.product_labels('sample'), [job['label']])
        scheduler.uninstall(job['label'])
        self.assertFalse(scheduler.status(job['label'])['loaded'])
        self.assertEqual([c[0] for c in self.fake.calls],
                         ['render', 'install', 'uninstall'])


class LaunchdUnchangedTest(SchedulerTestCase):
    """The default config still renders and installs launchd plists through launchctl."""

    def test_render_is_launchd(self):
        job = scheduler.render('sample', every_clock(), venv=None)
        self.assertEqual(job['kind'], 'launchd')
        self.assertTrue(job['path'].endswith('.plist'))
        self.assertIsNone(scheduler.active_backend())


if __name__ == '__main__':
    unittest.main()
