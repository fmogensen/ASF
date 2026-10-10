"""The ``kernel:`` block, ``asf kernel install`` and ``asf kernel watch`` over a fake launchctl,
in a temp ASF home and a temp LaunchAgents directory."""
import datetime
import fcntl
import os
import plistlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

from asf import env, scheduler
from asf.kernel import host, loop, settings
from asf.kernel import ports as P


class FakeLaunchctl:
    """``launchctl`` as a dict of loaded labels: bootstrap loads, bootout unloads, print asks."""

    def __init__(self, loaded=()):
        self.loaded = set(loaded)
        self.calls = []

    def __call__(self, args, timeout=30):
        self.calls.append(list(args))
        verb, target = args[0], args[-1]
        label = os.path.basename(target)[:-len('.plist')] if target.endswith('.plist') \
            else target.rpartition('/')[2]
        if verb == 'bootstrap':
            self.loaded.add(label)
        elif verb == 'bootout':
            if label not in self.loaded:
                return 3, '', 'not loaded'
            self.loaded.discard(label)
        elif verb in ('print', 'kickstart') and label not in self.loaded:
            return 113, '', 'Could not find service'
        return 0, '', ''


class Home(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.asf_home = os.path.join(self.tmp, 'asf-home')
        os.makedirs(os.path.join(self.asf_home, 'logs'))
        for p in (mock.patch.object(env, 'ASF_HOME', self.asf_home),
                  mock.patch.dict(os.environ, {'HOME': self.tmp})):
            p.start()
            self.addCleanup(p.stop)
        self.lc = FakeLaunchctl()
        p = mock.patch.object(scheduler, '_launchctl', self.lc)
        p.start()
        self.addCleanup(p.stop)
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'kernel': {
            'tick': {'interval_s': 90}, 'watch': {'stale_after_s': 300}}})
        self.state = os.path.join(self.asf_home, 'state', 'sample')
        os.makedirs(self.state)
        self.lines = []

    def plist(self, clock):
        with open(scheduler.plist_path('asf.sample.%s' % clock), 'rb') as f:
            return plistlib.load(f)


class Settings(unittest.TestCase):

    def test_defaults_fill_every_key(self):
        k = settings.read(None)
        self.assertEqual((k['tick']['interval_s'], k['launch']['max_sessions'], k['launch']['rank'],
                          k['watch']['interval_s'], k['watch']['stale_after_s']),
                         (120, 6, 'inherit', 600, 900))
        self.assertEqual(k['idle_alarm'], {'enabled': True, 'min_free_seats': 1})
        self.assertEqual(k['landing'], {'update_parallel': 2, 'max_wait_h': 2.0})
        self.assertEqual(P.config_for(env.Product('sample', {'repo_slug': 'o/r'}))
                         .landing_max_wait_h, 2.0)
        self.assertEqual(P.config_for(env.Product('sample', {'repo_slug': 'o/r'})).update_parallel,
                         2)
        self.assertEqual(k['gate'], {'window_h': 24, 'first_push_green_min': 0.7, 'landed_min': 5,
                                     'silent_stuck_max': 0, 'since': None})

    def test_a_malformed_value_refuses_the_load_and_an_unknown_key_warns(self):
        text = ('product: sample\nrepo_slug: o/r\nkernel:\n  launch:\n    rank: random\n'
                '  tick:\n    interval_s: 5\n  gate:\n    since: soon\n    extra: 1\n'
                '  landing:\n    update_parallel: two\n')
        errors, warnings = env.product_problems(text)
        self.assertEqual(sorted(k for _l, k, _w in errors),
                         ['kernel.gate.since', 'kernel.landing.update_parallel',
                          'kernel.launch.rank', 'kernel.tick.interval_s'])
        zero = 'product: sample\nrepo_slug: o/r\nkernel:\n  landing:\n    update_parallel: 0\n'
        self.assertEqual([k for _l, k, _w in env.product_problems(zero)[0]],
                         ['kernel.landing.update_parallel'])
        self.assertEqual([k for _l, k, _w in warnings], ['kernel.gate.extra'])

    def test_a_well_formed_block_loads_clean_and_reaches_the_config(self):
        text = ('product: sample\nrepo_slug: o/r\nkernel:\n  launch:\n    max_sessions: 3\n'
                '    rank: own\n  idle_alarm:\n    min_free_seats: 2\n'
                '  landing:\n    update_parallel: 4\n'
                '  gate:\n    since: 2026-10-09T14:00:00Z\n')
        self.assertEqual(env.product_problems(text), ([], []))
        product = env.Product('sample', env.loads(text))
        cfg = P.config_for(product)
        self.assertEqual((cfg.max_sessions, cfg.rank, cfg.idle_alarm, cfg.idle_min_free,
                          cfg.update_parallel), (3, 'own', True, 2, 4))
        self.assertEqual(product.kernel['gate']['since'],
                         datetime.datetime(2026, 10, 9, 14, tzinfo=datetime.timezone.utc))


class Install(Home):

    def test_writes_and_loads_both_jobs_from_this_venv_with_the_floor_env(self):
        rc = host.install(self.product, cfg={}, python='/venv/bin/python', out=self.lines.append)
        self.assertEqual(rc, 0)
        tick, watch = self.plist('kernel'), self.plist('kernel-watch')
        self.assertEqual(tick['ProgramArguments'],
                         ['/venv/bin/python', '-m', 'asf.cli', 'kernel', 'tick', '--product', 'sample'])
        self.assertEqual(watch['ProgramArguments'][3:5], ['kernel', 'watch'])
        self.assertEqual((tick['StartInterval'], watch['StartInterval']), (90, 600))
        self.assertEqual(set(tick['EnvironmentVariables']), {'ASF_HOME', 'HOME', 'PATH'})
        self.assertEqual(tick['EnvironmentVariables']['ASF_HOME'], self.asf_home)
        self.assertEqual(self.lc.loaded, {'asf.sample.kernel', 'asf.sample.kernel-watch'})

    def test_is_idempotent_and_replaces_a_job_on_another_venv(self):
        host.install(self.product, cfg={}, python='/old/bin/python', out=self.lines.append)
        self.lc.calls.clear()
        host.install(self.product, cfg={}, python='/old/bin/python', out=self.lines.append)
        self.assertFalse([c for c in self.lc.calls if c[0] in ('bootstrap', 'bootout')])
        self.assertIn('unchanged', self.lines[-1])
        host.install(self.product, cfg={}, python='/new/bin/python', out=self.lines.append)
        self.assertEqual(self.plist('kernel')['ProgramArguments'][0], '/new/bin/python')
        self.assertEqual([c[0] for c in self.lc.calls],
                         ['print', 'print'] + ['bootout', 'bootstrap'] * 2)

    def test_dry_run_prints_the_plists_and_touches_nothing(self):
        host.install(self.product, dry_run=True, cfg={}, python='/v/bin/python',
                     out=self.lines.append)
        text = '\n'.join(self.lines)
        self.assertIn('<string>asf.sample.kernel</string>', text)
        self.assertIn('<string>asf.sample.kernel-watch</string>', text)
        self.assertFalse(os.path.exists(scheduler.plist_path('asf.sample.kernel')))
        self.assertEqual(self.lc.calls, [])


class Watch(Home):

    def plan(self, age_s):
        path = os.path.join(self.state, loop.PLAN_FILE)
        with open(path, 'w') as f:
            f.write('{}')
        t = time.time() - age_s
        os.utime(path, (t, t))

    def watch(self):
        rc, line = host.watch(self.product, cfg={}, python='/v/bin/python')
        return rc, line

    def test_a_fresh_plan_on_a_loaded_job_is_ok_and_logs_one_line(self):
        host.install(self.product, cfg={}, python='/v/bin/python', out=self.lines.append)
        self.plan(10)
        with open(host.tick_log('sample'), 'w') as f:
            f.write('kernel tick: ready 2\nTraceback (most recent call last):\n')
        rc, line = self.watch()
        self.assertEqual(rc, 0)
        self.assertIn(' ok plan_age=', line)
        self.assertIn('tracebacks_last200=1 | kernel tick: ready 2', line)
        with open(host.watch_log('sample')) as f:
            self.assertEqual(f.read().splitlines(), [line])

    def test_an_unloaded_job_is_loaded_again(self):
        host.install(self.product, cfg={}, python='/v/bin/python', out=self.lines.append)
        self.lc.loaded.discard('asf.sample.kernel')
        self.plan(10)
        rc, line = self.watch()
        self.assertEqual(rc, 0)
        self.assertIn('RELOADED', line)
        self.assertIn('asf.sample.kernel', self.lc.loaded)

    def test_a_missing_plist_is_installed(self):
        rc, line = self.watch()
        self.assertIn('INSTALLED', line)
        self.assertEqual(self.plist('kernel')['ProgramArguments'][0], '/v/bin/python')

    def test_a_stale_plan_with_no_tick_running_is_kicked(self):
        host.install(self.product, cfg={}, python='/v/bin/python', out=self.lines.append)
        self.plan(1000)
        rc, line = self.watch()
        self.assertIn('KICKED (no tick for 1000s)', line.replace('1001s', '1000s'))
        self.assertEqual(self.lc.calls[-1][0], 'kickstart')

    def test_a_stale_plan_while_a_tick_holds_the_lock_is_left_alone(self):
        host.install(self.product, cfg={}, python='/v/bin/python', out=self.lines.append)
        self.plan(1000)
        fd = os.open(os.path.join(self.state, loop.LOCK_FILE), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(host.tick_running(self.state))
        rc, line = self.watch()
        self.assertIn(' ok ', line)
        self.assertNotIn('kickstart', [c[0] for c in self.lc.calls])


if __name__ == '__main__':
    unittest.main()
