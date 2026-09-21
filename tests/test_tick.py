"""asf.tick.tick — the live tick works in its own clone and pushes (B-0013); the step manifest."""
import argparse
import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.tick import steps, tick


def _git(args, cwd=None):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _args(**kw):
    base = dict(product='sample', shadow=False, fresh=False, steps=None, manifest=False, daily=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _tree_digest(path):
    """Every file under ``path`` (the .git dir included), name and bytes, hashed."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            h.update(os.path.relpath(full, path).encode())
            with open(full, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()


def _fake_step0(root, product, fresh=False):
    """Stands in for the real metrics/ingest/rollup pass (it needs CI and session evidence): the
    one derived-state file a rollup would write, with fixed content so a re-run changes nothing."""
    os.makedirs(os.path.join(root, 'state'), exist_ok=True)
    with open(os.path.join(root, 'state', 'rollup.md'), 'w') as f:
        f.write('derived\n')


class TickTestCase(unittest.TestCase):
    """A temp ASF home, a bare origin seeded with a minimal record, the operator's own checkout."""

    product_yaml = ''

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tick_test_')
        self.origin = os.path.join(self.tmp, 'origin.git')
        seed = os.path.join(self.tmp, 'seed')
        _git(['init', '-q', '--bare', '-b', 'main', self.origin])
        _git(['clone', '-q', self.origin, seed])
        _git(['config', 'user.email', 'seed@example.com'], seed)
        _git(['config', 'user.name', 'seed'], seed)
        os.makedirs(os.path.join(seed, 'features'))
        with open(os.path.join(seed, 'features', 'F-0001.md'), 'w') as f:
            f.write('---\nid: F-0001\ntitle: sample\n---\n')
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'seed'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)
        self.operator = os.path.join(self.tmp, 'operator')
        _git(['clone', '-q', self.origin, self.operator])

        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        self.write_product(self.product_yaml)
        self.write_config('')

        patcher = mock.patch.object(tick, 'run_step0', _fake_step0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_product(self, extra):
        with open(env.product_path('sample'), 'w') as f:
            f.write(f'repo_slug: x/y\nbacklog_dir: {self.operator}\n{extra}')

    def write_config(self, text):
        with open(env.config_path(), 'w') as f:
            f.write(text)

    def run_tick(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tick.cmd_tick(_args(**kw))
        return rc, out.getvalue()

    def origin_commits(self):
        return int(_git(['rev-list', '--count', 'main'], self.origin))

    def record_path(self):
        return os.path.join(env.ASF_HOME, 'state', 'sample', 'record')


class RecordStepTests(TickTestCase):
    """The record step's own commit and push; the tick's step-log commit is TickLineTests'."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(tick, 'write_tick_line', lambda ctx, ran: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_produces_one_pushed_commit(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(out, f'tick: state committed and pushed ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 2)
        self.assertEqual(_git(['show', 'main:state/rollup.md'], self.origin), 'derived')
        self.assertEqual(_git(['log', '-1', '--format=%an <%ae>', 'main'], self.origin), 'ASF <asf@localhost>')

    def test_configured_identity_is_the_commit_author(self):
        self.write_config('factory:\n  git_identity: Bot Name <bot@example.com>\n')
        self.run_tick(steps='record')
        self.assertEqual(_git(['log', '-1', '--format=%an <%ae>', 'main'], self.origin),
                         'Bot Name <bot@example.com>')

    def test_second_run_with_no_change_makes_no_commit(self):
        self.run_tick(steps='record')
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(out, f'tick: no change ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 2)

    def test_refused_push_exits_1_then_next_run_resets_the_stray_commit(self):
        hook = os.path.join(self.origin, 'hooks', 'pre-receive')
        with open(hook, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(hook, 0o755)

        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 1)
        self.assertEqual(out, f'tick: state committed, push refused — re-derived next run ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 1)
        self.assertEqual(_git(['rev-list', '--count', 'HEAD'], self.record_path()), '2')  # seed + the stray

        os.remove(hook)
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertIn('committed and pushed', out)
        self.assertEqual(self.origin_commits(), 2)
        self.assertEqual(_git(['rev-list', '--count', 'HEAD'], self.record_path()), '2')  # not stacked on it
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.record_path()), _git(['rev-parse', 'main'], self.origin))

    def test_operator_checkout_is_untouched(self):
        before = _tree_digest(self.operator)
        self.run_tick(steps='record')
        self.run_tick(steps='record')
        self.assertEqual(_tree_digest(self.operator), before)

    def test_no_backlog_dir_is_reported_not_raised(self):
        with open(env.product_path('sample'), 'w') as f:
            f.write('repo_slug: x/y\n')
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 1)
        self.assertIn('tick: record failed', out)


class ManifestTests(TickTestCase):
    product_yaml = ('steps:\n'
                    '  health: bash ~/x/health.sh --fix\n'
                    '  wave: off\n')

    def test_resolution_asf_command_off_undeclared(self):
        rows = steps.resolve(env.load_product('sample'))
        self.assertEqual(rows, [
            ('record', 'asf', None),
            ('health', 'command', 'bash ~/x/health.sh --fix'),
            ('wave', 'off', None),
            ('prs', 'asf', None),
            ('harvest', 'asf', None),
            ('batch', 'undeclared', None),
            ('daily', 'asf', None),
        ])

    def test_asf_declared_for_a_step_asf_lacks_is_undeclared(self):
        self.write_product('steps:\n  batch: asf\n  health: asf\n')
        rows = dict((s, o) for s, o, _ in steps.resolve(env.load_product('sample')))
        self.assertEqual(rows['batch'], 'undeclared')
        self.assertEqual(rows['health'], 'asf')

    def test_manifest_table_golden(self):
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, (
            'step     owner       command\n'
            'record   asf         asf.tick.tick:run_record_step\n'
            'health   command     bash ~/x/health.sh --fix\n'
            'wave     off         -\n'
            'prs      asf         asf.tick.step_prs:run\n'
            'harvest  asf         asf.tick.step_harvest:run\n'
            'batch    undeclared  -\n'
            'daily    asf         asf.tick.step_daily:run\n'))

    def test_manifest_golden_all_asf_and_a_batch_command(self):
        self.write_product('steps:\n  batch: bash ~/q/merge-queue.sh --once\n')
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, (
            'step     owner    command\n'
            'record   asf      asf.tick.tick:run_record_step\n'
            'health   asf      asf.tick.step_health:run\n'
            'wave     asf      asf.tick.step_wave:run\n'
            'prs      asf      asf.tick.step_prs:run\n'
            'harvest  asf      asf.tick.step_harvest:run\n'
            'batch    command  bash ~/q/merge-queue.sh --once\n'
            'daily    asf      asf.tick.step_daily:run\n'))

    def test_undeclared_batch_exits_2(self):
        self.write_product('')
        rc, out = self.run_tick()
        self.assertEqual(rc, 2)
        self.assertEqual(out, 'tick: step batch has no owner — declare it under steps in '
                              'products/sample.yaml (asf | <command> | off)\n')

    def test_undeclared_step_refuses_before_running_anything(self):
        marker = os.path.join(self.tmp, 'ran')
        self.write_product(f'steps:\n  health: touch {marker}\n  wave: off\n')
        rc, out = self.run_tick()
        self.assertEqual(rc, 2)
        self.assertEqual(out, 'tick: step batch has no owner — declare it under steps in '
                              'products/sample.yaml (asf | <command> | off)\n')
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(self.record_path()))
        self.assertEqual(self.origin_commits(), 1)

    def test_steps_subset_only_needs_its_own_steps_declared(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertNotIn('has no owner', out)

    def test_unknown_step_name_is_refused(self):
        rc, out = self.run_tick(steps='record,nope')
        self.assertEqual(rc, 2)
        self.assertIn('unknown step nope', out)


class LegacyStepTests(TickTestCase):
    product_yaml = ('steps:\n'
                    '  health: python3 -c \'print("hi")\'\n'
                    '  wave: off\n'
                    '  prs: off\n'
                    '  batch: off\n'
                    '  daily: python3 -c \'print("daily ran")\'\n')

    def test_command_output_is_prefixed_in_the_log(self):
        rc, out = self.run_tick(steps='health')
        self.assertEqual(rc, 0)
        self.assertEqual(out, '[command:health] hi\n')

    def test_steps_subset_runs_only_those_and_in_manifest_order(self):
        rc, out = self.run_tick(steps='health,record')
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], '[command:health] hi')
        # the one commit comes last: after every step, over the state and the tick line together
        self.assertEqual(out.splitlines()[-1], f'tick: state committed and pushed ({self.record_path()})')
        self.assertNotIn('daily', out)

    def test_off_step_is_reported_not_run(self):
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'tick: step batch off (another job runs it)\n')

    def test_failing_command_step_exits_1_and_later_steps_still_run(self):
        self.write_product('steps:\n  health: python3 -c \'import sys; print("bad"); sys.exit(3)\'\n'
                           '  wave: python3 -c \'print("after")\'\n')
        rc, out = self.run_tick(steps='health,wave')
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines(), ['[command:health] bad', 'tick: step health exited 3',
                                            '[command:wave] after'])

    def test_timeout_kills_a_sleep(self):
        self.write_config('tick:\n  step_timeout_s: 1\n')
        self.write_product('steps:\n  health: sleep 30\n')
        t0 = time.monotonic()
        rc, out = self.run_tick(steps='health')
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(rc, 1)
        self.assertIn('[command:health] timeout after 1s — killed', out)
        self.assertIn('tick: step health exited 124', out)

    def test_timeout_kills_the_whole_process_group(self):
        lines = []
        rc = steps.run_command('health', "sh -c 'sleep 30 & sleep 30'", 1, emit=lines.append)
        self.assertEqual(rc, 124)

    def test_shadow_never_runs_a_command_step(self):
        marker = os.path.join(self.tmp, 'ran')
        self.write_product(f'steps:\n  health: touch {marker}\n')
        with mock.patch.object(tick, 'render_tables', return_value={}):
            rc, out = self.run_tick(shadow=True)
        self.assertEqual(rc, 0)
        self.assertIn('tick --shadow:', out)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.origin_commits(), 1)  # and never pushes

    def test_daily_runs_once_a_day(self):
        rc, out = self.run_tick(steps='daily')
        self.assertEqual(out, '[command:daily] daily ran\n')
        with open(steps.stamp_path(env.load_product('sample'))) as f:
            self.assertEqual(f.read().strip(), steps._today())

        rc, out = self.run_tick(steps='daily')
        self.assertEqual(out, 'tick: step daily already ran today\n')

        rc, out = self.run_tick(steps='daily', daily=True)
        self.assertEqual(out, '[command:daily] daily ran\n')

    def test_daily_runs_when_the_stamp_is_from_another_day(self):
        product = env.load_product('sample')
        with open(steps.stamp_path(product), 'w') as f:
            f.write('2001-01-01\n')
        rc, out = self.run_tick(steps='daily')
        self.assertEqual(out, '[command:daily] daily ran\n')

    def test_failed_daily_is_not_stamped(self):
        self.write_product('steps:\n  daily: python3 -c \'raise SystemExit(1)\'\n')
        self.run_tick(steps='daily')
        self.assertFalse(os.path.exists(steps.stamp_path(env.load_product('sample'))))


class Step0Tests(unittest.TestCase):
    """What step 0 hands the backfill: CI runs only, of the product's workflow; no launcher dir."""

    def step0(self, product):
        from asf.metrics import metrics
        from asf.record import ingest
        from asf.tick import file_bugs
        calls = []
        with mock.patch.object(metrics, 'cmd_backfill', lambda a, r: calls.append(a)), \
                mock.patch.object(metrics, 'cmd_rollup', lambda a, r: 0), \
                mock.patch.object(ingest, 'cmd_ingest', lambda a, r: 0), \
                mock.patch.object(file_bugs, 'cmd_file_bugs', lambda a, r: 0), \
                mock.patch.object(tick, 'do_index', lambda r: 0), \
                mock.patch.dict(os.environ):
            tick.run_step0('/nowhere', product)
        return calls

    def test_backfill_reads_the_products_workflow_and_no_launcher_dir(self):
        (a,) = self.step0(env.Product('p', {'ci': {'provider': 'gh-actions', 'workflow': 'build'}}))
        self.assertEqual((a.workflow, a.launch_dir, a.sessions, a.log), ('build', None, None, None))
        (a,) = self.step0(env.Product('p', {}))
        self.assertEqual(a.workflow, 'ci')

    def test_no_ci_no_backfill(self):
        self.assertEqual(self.step0(env.Product('p', {'ci': {'provider': 'none'}})), [])
        self.assertEqual(self.step0(env.Product('p', {'ci': 'none'})), [])


if __name__ == '__main__':
    unittest.main()
