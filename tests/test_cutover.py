"""tools/cutover.sh and tools/rollback.sh on a fixture operator dir (``ASF_HOME=/tmp/asf/home``).

The gate (item (a)) also runs `asf doctor`, whose result depends on which CLIs are logged in on
the machine running the tests; tests that are not about the gate pass `--force`, and the gate's
own tests drive `asf shadow-diff --ref` against fixture trees.
"""
import json
import os
import shutil
import subprocess
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUTOVER = os.path.join(PROJECT_ROOT, 'tools', 'cutover.sh')
ROLLBACK = os.path.join(PROJECT_ROOT, 'tools', 'rollback.sh')

FIXTURE_HOME = '/tmp/asf/home'
FIXTURE_SRC = '/tmp/asf/cutover-fixture-src'


def run(script, args, home, timeout=30):
    env = dict(os.environ)
    env['ASF_HOME'] = home
    return subprocess.run(['bash', script] + args, env=env, capture_output=True, text=True,
                          timeout=timeout)


class CutoverFixtureTest(unittest.TestCase):
    def setUp(self):
        for d in (FIXTURE_HOME, FIXTURE_SRC):
            if os.path.exists(d):
                shutil.rmtree(d)
            os.makedirs(d)
        os.makedirs(os.path.join(FIXTURE_HOME, 'products'))
        os.makedirs(os.path.join(FIXTURE_HOME, 'state'))

        self.repo_dir = os.path.join(FIXTURE_SRC, 'repo')
        self.backlog_dir = os.path.join(FIXTURE_SRC, 'backlog')
        os.makedirs(self.repo_dir)
        os.makedirs(self.backlog_dir)
        subprocess.run(['git', 'init', '-q'], cwd=self.repo_dir, check=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.backlog_dir, check=True)

        self.tick_file = os.path.join(FIXTURE_SRC, 'tick.md')
        with open(self.tick_file, 'w') as f:
            f.write('# Tick procedure\n\nStep 0: run the old tools by hand.\n')

        self.plugin_dir = os.path.join(FIXTURE_SRC, 'plugin')
        os.makedirs(self.plugin_dir)
        with open(os.path.join(self.plugin_dir, 'board.md'), 'w') as f:
            f.write('Run the old board tool.\n')

        self.legacy_a = os.path.join(FIXTURE_SRC, 'legacy-a')
        self.legacy_b = os.path.join(FIXTURE_SRC, 'legacy-b')
        os.makedirs(self.legacy_a)
        os.makedirs(self.legacy_b)
        open(os.path.join(self.legacy_a, 'old-tool.py'), 'w').close()
        open(os.path.join(self.legacy_b, 'old-helper.sh'), 'w').close()

        with open(os.path.join(FIXTURE_HOME, 'config.yaml'), 'w') as f:
            f.write(
                "schema_version: 1\n"
                "default_product: sample\n"
                "scheduler:\n"
                "  kind: gh-actions\n"  # not launchd: step (d) is exercised as a no-op skip
                "operator:\n"
                f"  tick_file: {self.tick_file}\n"
                f"  plugin_dir: {self.plugin_dir}\n"
                "legacy_paths:\n"
                f"  - {self.legacy_a}\n"
                f"  - {self.legacy_b}\n"
            )
        with open(os.path.join(FIXTURE_HOME, 'products', 'sample.yaml'), 'w') as f:
            f.write(
                "product: sample\n"
                "repo_slug: acme/sample\n"
                f"repo_dir: {self.repo_dir}\n"
                "main: main\n"
                f"backlog_dir: {self.backlog_dir}\n"
            )

    def tearDown(self):
        for d in (FIXTURE_HOME, FIXTURE_SRC):
            if os.path.exists(d):
                shutil.rmtree(d)

    def marker_path(self):
        return os.path.join(FIXTURE_HOME, 'state', 'sample', 'cutover-done')

    # ---- dry-run ---------------------------------------------------------------------------

    def test_dry_run_changes_nothing(self):
        result = run(CUTOVER, ['sample', '--force'], FIXTURE_HOME)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('== CUTOVER sample (dry-run)', result.stdout)
        self.assertIn('would update', result.stdout + result.stderr)
        # nothing touched: no marker, no retired dir, tick/plugin files untouched
        self.assertFalse(os.path.exists(self.marker_path()))
        with open(self.tick_file) as f:
            self.assertNotIn('ASF:CUTOVER', f.read())
        self.assertTrue(os.path.isdir(self.legacy_a))
        self.assertTrue(os.path.isdir(self.legacy_b))

    def test_dry_run_refuses_without_force_when_shadow_diff_unavailable(self):
        result = run(CUTOVER, ['sample'], FIXTURE_HOME)
        self.assertEqual(result.returncode, 1)
        self.assertIn('refusing', result.stdout + result.stderr)

    def _write_tree(self, root, table_text):
        os.makedirs(os.path.join(root, 'tables'), exist_ok=True)
        with open(os.path.join(root, 'index.json'), 'w') as f:
            json.dump({'items': []}, f)
        for name in ('roadmap', 'backlog', 'parity', 'prod', 'sessions', 'status'):
            with open(os.path.join(root, 'tables', f'{name}.md'), 'w') as f:
                f.write(f'stamp line\n{table_text}\n')

    def test_ref_is_forwarded_to_shadow_diff(self):
        shadow = os.path.join(FIXTURE_HOME, 'state', 'sample', 'shadow')
        ref = os.path.join(FIXTURE_SRC, 'ref')
        self._write_tree(shadow, '| a | b |')
        self._write_tree(ref, '| a | b |')
        result = run(CUTOVER, ['sample', '--ref', ref, '--force'], FIXTURE_HOME)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('shadow-diff: clean', result.stdout)

    def test_ref_that_differs_from_the_shadow_fails_the_gate(self):
        shadow = os.path.join(FIXTURE_HOME, 'state', 'sample', 'shadow')
        ref = os.path.join(FIXTURE_SRC, 'ref')
        self._write_tree(shadow, '| a | b |')
        self._write_tree(ref, '| a | DIFFERENT |')
        result = run(CUTOVER, ['sample', '--ref', ref], FIXTURE_HOME)
        self.assertEqual(result.returncode, 1)
        self.assertIn('DIFFERENT', result.stdout)
        self.assertIn('refusing', result.stdout + result.stderr)

    def test_missing_ref_says_so(self):
        result = run(CUTOVER, ['sample'], FIXTURE_HOME)
        self.assertIn('--ref', result.stdout + result.stderr)

    # ---- apply -------------------------------------------------------------------------------

    def test_apply_rewrites_tick_file_and_plugin_skills(self):
        result = run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        with open(self.tick_file) as f:
            tick_text = f.read()
        self.assertIn('asf tick --product sample', tick_text)
        self.assertIn('ASF:CUTOVER:BEGIN', tick_text)

        with open(os.path.join(self.plugin_dir, 'board.md')) as f:
            plugin_text = f.read()
        self.assertIn('asf board --product sample', plugin_text)

        self.assertFalse(os.path.isdir(self.legacy_a))
        self.assertFalse(os.path.isdir(self.legacy_b))
        self.assertTrue(os.path.exists(self.marker_path()))

    def test_apply_moves_legacy_dirs_under_retired(self):
        run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        with open(self.marker_path()) as f:
            date = f.read().strip()
        retired = os.path.join(FIXTURE_HOME, 'state', 'sample', 'retired', date)
        name_a = self.legacy_a.lstrip('/').replace('/', '-')
        name_b = self.legacy_b.lstrip('/').replace('/', '-')
        self.assertTrue(os.path.isdir(os.path.join(retired, name_a)))
        self.assertTrue(os.path.isfile(os.path.join(retired, name_a, 'old-tool.py')))
        self.assertTrue(os.path.isdir(os.path.join(retired, name_b)))

    def test_apply_records_a_cutover_event(self):
        run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        events_dir = os.path.join(self.backlog_dir, 'metrics', 'events')
        self.assertTrue(os.path.isdir(events_dir))
        files = os.listdir(events_dir)
        self.assertEqual(len(files), 1)
        with open(os.path.join(events_dir, files[0])) as f:
            events = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'cutover')
        self.assertEqual(events[0]['product'], 'sample')

    def test_apply_is_idempotent(self):
        first = run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        with open(self.tick_file) as f:
            tick_after_first = f.read()

        second = run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already cut over', second.stdout)
        with open(self.tick_file) as f:
            self.assertEqual(f.read(), tick_after_first)

    # ---- rollback ------------------------------------------------------------------------------

    def test_rollback_without_prior_cutover_refuses(self):
        result = run(ROLLBACK, ['sample', '--apply'], FIXTURE_HOME)
        self.assertEqual(result.returncode, 1)
        self.assertIn('nothing to roll back', result.stdout + result.stderr)

    def test_rollback_restores_everything(self):
        with open(self.tick_file) as f:
            original_tick = f.read()
        with open(os.path.join(self.plugin_dir, 'board.md')) as f:
            original_plugin = f.read()

        apply_result = run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        self.assertEqual(apply_result.returncode, 0, apply_result.stdout + apply_result.stderr)

        rollback_result = run(ROLLBACK, ['sample', '--apply'], FIXTURE_HOME)
        self.assertEqual(rollback_result.returncode, 0,
                         rollback_result.stdout + rollback_result.stderr)

        with open(self.tick_file) as f:
            self.assertEqual(f.read(), original_tick)
        with open(os.path.join(self.plugin_dir, 'board.md')) as f:
            self.assertEqual(f.read(), original_plugin)
        self.assertTrue(os.path.isdir(self.legacy_a))
        self.assertTrue(os.path.isfile(os.path.join(self.legacy_a, 'old-tool.py')))
        self.assertTrue(os.path.isdir(self.legacy_b))
        self.assertFalse(os.path.exists(self.marker_path()))

    def test_rollback_dry_run_changes_nothing(self):
        run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        with open(self.tick_file) as f:
            tick_after_cutover = f.read()

        result = run(ROLLBACK, ['sample'], FIXTURE_HOME)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('would restore', result.stdout)
        with open(self.tick_file) as f:
            self.assertEqual(f.read(), tick_after_cutover)
        self.assertTrue(os.path.exists(self.marker_path()))

    def test_cutover_then_rollback_then_cutover_again(self):
        r1 = run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        r2 = run(ROLLBACK, ['sample', '--apply'], FIXTURE_HOME)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        r3 = run(CUTOVER, ['sample', '--force', '--apply'], FIXTURE_HOME)
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        with open(self.tick_file) as f:
            self.assertIn('asf tick --product sample', f.read())


if __name__ == '__main__':
    unittest.main()
