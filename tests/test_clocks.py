"""asf.scheduler.clocks() — the clocks: block, its validation, and the jobs it renders.

Shares its fixture (``SchedulerTestCase``, the fake ``launchctl``) with ``test_scheduler``: a
temp ``ASF_HOME``, a temp ``HOME``, nothing that touches the machine's real launchd.
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from asf import env, scheduler
from asf.scheduler import Clock

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_clocks` does not
    from test_scheduler import (SchedulerTestCase, fake_loaded, fake_print, read_fixture,
                                stub_argv)
except ImportError:  # pragma: no cover - import shape only
    from tests.test_scheduler import (SchedulerTestCase, fake_loaded, fake_print, read_fixture,
                                      stub_argv)

CARD_EXAMPLE = (
    '  record:\n    steps: [record]\n    every: 5m\n'
    '  dispatch:\n    steps: [health, wave, prs, harvest, batch]\n    every: 10m\n'
    '  daily:\n    steps: [daily]\n    at: "06:50"\n'
    '  shadow:\n    shadow: true\n    every: 30m\n'
)
BATCH_OWNED = 'steps:\n  batch: off\n'


class ClocksParseTest(SchedulerTestCase):
    def test_the_card_example_parses(self):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        product = env.load_product('sample')
        self.assertEqual(scheduler.clocks(product), [
            Clock('record', ['record'], False, 300, None),
            Clock('dispatch', ['health', 'wave', 'prs', 'harvest', 'batch'], False, 600, None),
            Clock('daily', ['daily'], False, None, {'Hour': 6, 'Minute': 50}),
            Clock('shadow', [], True, 1800, None),
        ])

    def test_no_clocks_is_needs_operator(self):
        product = env.load_product('sample')  # setUp's product has no clocks: block
        with self.assertRaises(scheduler.SchedulerError) as ctx:
            scheduler.clocks(product)
        self.assertTrue(str(ctx.exception).startswith(
            'NEEDS OPERATOR: products/sample.yaml declares no clocks'))

    def test_each_refusal_names_its_clock(self):
        self.write_product(
            '  bad-step:\n    steps: [nope]\n    every: 5m\n'
            '  both:\n    steps: [record]\n    every: 5m\n    at: "06:50"\n'
            '  neither:\n    steps: [record]\n'
            '  short:\n    steps: [record]\n    every: 30s\n'
            '  late:\n    steps: [daily]\n    at: "25:00"\n'
            '  mixed:\n    shadow: true\n    steps: [record]\n    every: 5m\n'
            '  typo:\n    steps: [record]\n    evrey: 5m\n'
        )
        product = env.load_product('sample')
        with self.assertRaises(scheduler.SchedulerError) as ctx:
            scheduler.clocks(product)
        msg = str(ctx.exception)
        for name in ('bad-step', 'both', 'neither', 'short', 'late', 'mixed', 'typo'):
            self.assertIn(f'clock {name}:', msg, msg)

    def test_ownerless_step_refused(self):
        self.write_product('  bad:\n    steps: [batch]\n    every: 5m\n')
        product = env.load_product('sample')
        with self.assertRaises(scheduler.SchedulerError) as ctx:
            scheduler.clocks(product)
        self.assertIn('batch', str(ctx.exception))

    def test_clocks_is_a_product_field(self):
        text = ('product: sample\nrepo_slug: acme/sample\n'
                f'repo_dir: {self.repo_dir}\nmain: main\nbacklog_dir: {self.backlog_dir}\n'
                'clocks:\n  record:\n    steps: [record]\n    every: 5m\n')
        self.assertEqual(env.validate_product_text(text), [])


class ClocksRenderTest(SchedulerTestCase):
    def _clock(self, name):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        product = env.load_product('sample')
        return product, [c for c in scheduler.clocks(product) if c.name == name][0]

    def test_interval_clock_launchd(self):
        product, clock = self._clock('record')
        job = scheduler.render(product, clock)
        self.assertEqual(job['plist']['Label'], 'asf.sample.record')
        self.assertEqual(job['plist']['StartInterval'], 300)
        self.assertEqual(job['plist']['ProgramArguments'][-2:], ['--steps', 'record'])
        self.assertTrue(job['log'].endswith('tick-sample-record.log'))

    def test_label_is_the_clock_name_not_the_steps(self):
        product, clock = self._clock('dispatch')
        job = scheduler.render(product, clock)
        self.assertEqual(job['plist']['Label'], 'asf.sample.dispatch')
        self.assertEqual(job['plist']['ProgramArguments'][-2:],
                         ['--steps', 'health,wave,prs,harvest,batch'])

    def test_at_clock_launchd(self):
        product, clock = self._clock('daily')
        job = scheduler.render(product, clock)
        self.assertEqual(job['plist']['StartCalendarInterval'], {'Hour': 6, 'Minute': 50})
        self.assertNotIn('StartInterval', job['plist'])
        self.assertEqual(job['plist']['ProgramArguments'][-1], '--daily')

    def test_shadow_clock(self):
        product, clock = self._clock('shadow')
        job = scheduler.render(product, clock)
        self.assertEqual(job['plist']['ProgramArguments'][-4:],
                         ['tick', '--product', 'sample', '--shadow'])
        self.assertEqual(job['plist']['StartInterval'], 1800)

    def test_two_products_two_intervals(self):
        for name, every in (('a', '5m'), ('b', '15m')):
            with open(os.path.join(self.asf_home, 'products', f'{name}.yaml'), 'w') as f:
                f.write(f'product: {name}\nrepo_slug: acme/{name}\n'
                        f'repo_dir: {self.repo_dir}\nmain: main\nbacklog_dir: {self.backlog_dir}\n'
                        f'clocks:\n  record:\n    steps: [record]\n    every: {every}\n')
        product_a, product_b = env.load_product('a'), env.load_product('b')
        clock_a = scheduler.clocks(product_a)[0]
        clock_b = scheduler.clocks(product_b)[0]
        job_a = scheduler.render(product_a, clock_a)
        job_b = scheduler.render(product_b, clock_b)
        self.assertEqual(job_a['label'], 'asf.a.record')
        self.assertEqual(job_b['label'], 'asf.b.record')
        self.assertEqual(job_a['plist']['StartInterval'], 300)
        self.assertEqual(job_b['plist']['StartInterval'], 900)

    def test_config_interval_s_is_not_read(self):
        self.write_config('  interval_s: 60\n')
        product, clock = self._clock('record')
        job = scheduler.render(product, clock, cfg=env.load_config())
        self.assertEqual(job['plist']['StartInterval'], 300)


class ClocksCronTest(SchedulerTestCase):
    CRON_CFG = {'scheduler': {'kind': 'cron'}}

    def test_minutes(self):
        clock = Clock('record', ['record'], False, 300, None)
        job = scheduler.render('sample', clock, cfg=self.CRON_CFG)
        self.assertTrue(job['line'].startswith('*/5 * * * *'))

    def test_hours(self):
        clock = Clock('record', ['record'], False, 7200, None)
        job = scheduler.render('sample', clock, cfg=self.CRON_CFG)
        self.assertTrue(job['line'].startswith('0 */2 * * *'))

    def test_at(self):
        clock = Clock('daily', ['daily'], False, None, {'Hour': 6, 'Minute': 50})
        job = scheduler.render('sample', clock, cfg=self.CRON_CFG)
        self.assertTrue(job['line'].startswith('50 6 * * *'))

    def test_inexpressible(self):
        clock = Clock('record', ['record'], False, 5400, None)
        job = scheduler.render('sample', clock, cfg=self.CRON_CFG)
        self.assertNotIn('line', job)
        self.assertIn('90m', job['needs_operator'])


class ClocksInstallTest(SchedulerTestCase):
    def _install(self, clock=None, product='sample'):
        args = argparse.Namespace(scheduler_command='install', product=product, clock=clock,
                                  label=None, json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = scheduler.cmd_scheduler(args)
        return rc, buf.getvalue()

    def _agents_dir(self):
        return os.path.join(self.home, 'Library', 'LaunchAgents')

    def _booted_out(self):
        path = os.path.join(self.statedir, 'booted-out.txt')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().split()

    def test_install_all_writes_one_plist_per_clock(self):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        rc, _out = self._install()
        self.assertEqual(rc, 0)
        for label in ('asf.sample.record', 'asf.sample.dispatch', 'asf.sample.daily',
                      'asf.sample.shadow'):
            self.assertTrue(
                os.path.isfile(os.path.join(self._agents_dir(), f'{label}.plist')), label)
        bootstraps = [l for l in stub_argv(self.statedir) if l.startswith('bootstrap')]
        self.assertEqual(len(bootstraps), 4, bootstraps)

    def test_install_retires_an_undeclared_job(self):
        self.write_config('  legacy_labels: [old.*]\n')
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        undeclared_label = 'asf.sample.health-wave-prs-batch'
        import plistlib
        os.makedirs(self._agents_dir(), exist_ok=True)
        with open(os.path.join(self._agents_dir(), f'{undeclared_label}.plist'), 'wb') as f:
            plistlib.dump({'Label': undeclared_label, 'ProgramArguments': ['x']}, f)
        fake_loaded(self.statedir, [undeclared_label, 'old.dispatch'])

        rc, out = self._install()
        self.assertEqual(rc, 0, out)
        self.assertIn(undeclared_label, self._booted_out())
        self.assertNotIn('old.dispatch', self._booted_out())
        self.assertFalse(os.path.exists(os.path.join(self._agents_dir(), f'{undeclared_label}.plist')))
        self.assertIn(f'scheduler: retired {undeclared_label}', out)

    def test_install_never_retires_another_products_jobs(self):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        fake_loaded(self.statedir, ['asf.other.record'])
        rc, out = self._install()
        self.assertEqual(rc, 0, out)
        self.assertNotIn('asf.other.record', self._booted_out())
        self.assertNotIn('retired asf.other.record', out)

    def test_install_one_clock_retires_nothing(self):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        fake_loaded(self.statedir, ['asf.sample.health-wave-prs-batch'])
        rc, out = self._install(clock='record')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('retired', out)
        bootstraps = [l for l in stub_argv(self.statedir) if l.startswith('bootstrap')]
        self.assertEqual(len(bootstraps), 1, bootstraps)

    def test_install_is_atomic(self):
        self.write_product('  record:\n    steps: [record]\n    every: 5m\n'
                           '  bad:\n    steps: [record]\n')
        rc, _out = self._install()
        self.assertEqual(rc, 2)
        self.assertEqual(stub_argv(self.statedir), [])
        self.assertFalse(os.path.isdir(self._agents_dir()))


class ClocksCliTest(SchedulerTestCase):
    """Driven through ``python3 -m asf.scheduler``, like ``test_scheduler.CliTest``."""

    def _run(self, args):
        cmd = [sys.executable, '-m', 'asf.scheduler'] + args
        proc_env = dict(os.environ)
        proc_env['ASF_HOME'] = self.asf_home
        proc_env['PYTHONPATH'] = scheduler.repo_root()
        return subprocess.run(cmd, capture_output=True, text=True, env=proc_env,
                              cwd=scheduler.repo_root(), timeout=60)

    def test_steps_and_interval_flags_are_gone(self):
        result = self._run(['install', '--steps', 'record', '--interval', '600'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized', result.stderr)

    def test_unknown_clock(self):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        result = self._run(['render', '--product', 'sample', '--clock', 'nope'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('no clock nope in products/sample.yaml (clocks: record, dispatch, daily, '
                      'shadow)', result.stdout + result.stderr)

    def test_render_json_lists_every_clock(self):
        self.write_product(CARD_EXAMPLE, steps_yaml=BATCH_OWNED)
        result = self._run(['render', '--product', 'sample', '--json'])
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs = json.loads(result.stdout)
        self.assertEqual([j['clock'] for j in jobs], ['record', 'dispatch', 'daily', 'shadow'])

    def test_no_clocks_exits_2_with_needs_operator(self):
        result = self._run(['render', '--product', 'sample'])
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stdout.splitlines()[0].startswith('NEEDS OPERATOR:'))

    def test_status_every_clock(self):
        self.write_product(
            '  record:\n    steps: [record]\n    every: 5m\n'
            '  dispatch:\n    steps: [health, wave, prs, harvest, batch]\n    every: 10m\n',
            steps_yaml=BATCH_OWNED)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))
        result = self._run(['status', '--product', 'sample'])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(result.stdout.strip().splitlines()), 2, result.stdout)


if __name__ == '__main__':
    unittest.main()
