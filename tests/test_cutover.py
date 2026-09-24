"""tools/cutover.sh and tools/rollback.sh on a fixture operator dir.

Everything is per-test and disposable: its own ``ASF_HOME``, its own ``HOME`` (so
``~/Library/LaunchAgents`` is the test's), a fake ``launchctl`` first on ``PATH`` (from
``test_scheduler``) and a stub ``asf`` that answers ``tick --manifest`` and forwards every other
subcommand to the real CLI. Nothing here loads a job on the machine running the tests, and no
two tests can collide over a shared path.

Each script run costs seconds (a score of `python3` starts, `asf doctor` twice), so a test that
only reads what one run left behind does not run it again: the dry-run and the applied states
are each reached once per class (:class:`SharedFixture`) and every test starts from a copy of
that snapshot. No test waits on a clock — gate 3's deadline is 0 s, checked after its one look.

The gate (item (a)) also runs `asf doctor`, whose result depends on which CLIs are logged in on
the machine running the tests; tests that are not about the gate pass `--force`, and the gate's
own tests drive `asf shadow-diff --ref` against fixture trees.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_cutover` does not
    from gitfixture import Template
    from test_scheduler import fake_clis, fake_launchctl, fake_loaded
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template
    from tests.test_scheduler import fake_clis, fake_launchctl, fake_loaded

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUTOVER = os.path.join(PROJECT_ROOT, 'tools', 'cutover.sh')
ROLLBACK = os.path.join(PROJECT_ROOT, 'tools', 'rollback.sh')

#: A guard against a hung script, not a speed bound: a run costs ~1 s alone, but in harvest's
#: combined gate on a loaded machine (8 shards, load ~30) two runs passed 120 s and the module
#: went red — a timeout, not a defect — which bisected every set it was in to "green alone".
SCRIPT_TIMEOUT_S = 1800

# The stub `asf`: `tick --manifest` comes from files the test controls, everything else is the
# real CLI. `asf tick --manifest` is another job's command; this is how it is stubbed.
ASF_STUB = r"""#!/bin/sh
if [ "$1" = "tick" ]; then
  for a in "$@"; do
    if [ "$a" = "--manifest" ]; then
      [ -f "$STUB_DIR/manifest.txt" ] && cat "$STUB_DIR/manifest.txt"
      exit "$(cat "$STUB_DIR/manifest-rc" 2>/dev/null || echo 0)"
    fi
  done
fi
exec python3 -m asf.cli "$@"
"""

FULL_MANIFEST = (
    'step · owner · command\n'
    'record · asf · asf tick --product sample --steps record\n'
    'health · asf · asf tick --product sample --steps health\n'
    'wave · asf · asf tick --product sample --steps wave\n'
    'prs · asf · asf tick --product sample --steps prs\n'
    'batch · asf · asf tick --product sample --steps batch\n'
    'daily · asf · asf tick --product sample --steps daily\n'
)

RECORD_ONLY_MANIFEST = (
    'step · owner · command\n'
    'record · asf · asf tick --product sample --steps record\n'
)

PRINT_TEMPLATE = """@LABEL@ = {
\tactive count = 0
\tpath = /dev/null
\tstate = not running

\tprogram = /usr/bin/python3

\truns = 1
\tlast exit code = 0
}
"""


def _git(args, cwd):
    env = dict(os.environ)
    env.update({'GIT_AUTHOR_NAME': 'test', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                'GIT_COMMITTER_NAME': 'test', 'GIT_COMMITTER_EMAIL': 't@example.invalid'})
    subprocess.run(['git'] + args, cwd=cwd, check=True, env=env, capture_output=True, text=True)


def _build_repos(root):
    """The product repo (empty), the record's bare origin and its clone with one root commit —
    built once, copied per test (B-0071)."""
    repo_dir = os.path.join(root, 'repo')
    origin_dir = os.path.join(root, 'origin.git')
    backlog_dir = os.path.join(root, 'backlog')
    os.makedirs(repo_dir)
    _git(['init', '-q'], cwd=repo_dir)
    subprocess.run(['git', 'init', '-q', '--bare', origin_dir], check=True)
    subprocess.run(['git', 'clone', '-q', origin_dir, backlog_dir], check=True,
                   capture_output=True)
    _git(['commit', '-q', '--allow-empty', '-m', 'root'], cwd=backlog_dir)
    _git(['push', '-q', 'origin', 'HEAD'], cwd=backlog_dir)


REPOS = Template(_build_repos, prefix='asf-cutover-')


class CutoverFixture:
    """The fixture operator dir and the knobs over it; no tests of its own."""

    def build(self, tmp):
        self.tmp = tmp
        self.home = os.path.join(self.tmp, 'home')
        self.asf_home = os.path.join(self.tmp, 'ASF')
        self.stub_dir = os.path.join(self.tmp, 'stub')
        for d in (self.home, os.path.join(self.asf_home, 'products'),
                  os.path.join(self.asf_home, 'state'), self.stub_dir):
            os.makedirs(d)

        self.bindir, self.statedir = fake_launchctl(self.tmp)
        fake_clis(self.bindir)  # `asf doctor` runs twice per cutover; its probes answer at once
        self.asf_stub = os.path.join(self.stub_dir, 'asf-stub.sh')
        with open(self.asf_stub, 'w', encoding='utf-8') as f:
            f.write(ASF_STUB)
        os.chmod(self.asf_stub, 0o755)
        self.set_manifest(FULL_MANIFEST, 0)
        with open(os.path.join(self.statedir, 'print-template.txt'), 'w') as f:
            f.write(PRINT_TEMPLATE)

        self.repo_dir = os.path.join(self.tmp, 'repo')
        self.origin_dir = os.path.join(self.tmp, 'origin.git')
        self.backlog_dir = os.path.join(self.tmp, 'backlog')
        self.set_job_runs(True)

        self.tick_file = os.path.join(self.tmp, 'tick.md')
        with open(self.tick_file, 'w') as f:
            f.write('# Tick procedure\n\nStep 0: run the old tools by hand.\n')

        self.plugin_dir = os.path.join(self.tmp, 'plugin')
        os.makedirs(self.plugin_dir)
        with open(os.path.join(self.plugin_dir, 'board.md'), 'w') as f:
            f.write('Run the old board tool.\n')

        self.legacy_a = os.path.join(self.tmp, 'legacy-a')
        self.legacy_b = os.path.join(self.tmp, 'legacy-b')
        os.makedirs(self.legacy_a)
        os.makedirs(self.legacy_b)
        open(os.path.join(self.legacy_a, 'old-tool.py'), 'w').close()
        open(os.path.join(self.legacy_b, 'old-helper.sh'), 'w').close()

        self.write_config()
        self.write_product_clocks(
            '  record:\n    steps: [record]\n    every: 5m\n'
            '  dispatch:\n    steps: [health, wave, prs, batch]\n    every: 10m\n'
            '  daily:\n    steps: [daily]\n    at: "06:50"\n')

    # ---- fixture knobs ------------------------------------------------------------------------

    def write_config(self, scheduler_extra='', legacy_steps=None):
        with open(os.path.join(self.asf_home, 'config.yaml'), 'w') as f:
            f.write('schema_version: 1\n'
                    'default_product: sample\n'
                    'scheduler:\n'
                    '  kind: launchd\n'
                    '  label_prefix: asf\n'
                    '  interval_s: 600\n'
                    + scheduler_extra +
                    'cutover:\n'
                    # gate 3 checks once before it looks at the clock: the fake launchd runs
                    # the job inside `bootstrap`, so 0 s is never short, and a job that never
                    # runs is refused at once instead of after a `sleep 3` under a 1 s deadline
                    '  job_timeout_s: 0\n'
                    'operator:\n'
                    f'  tick_file: {self.tick_file}\n'
                    f'  plugin_dir: {self.plugin_dir}\n'
                    'legacy_paths:\n'
                    f'  - {self.legacy_a}\n'
                    f'  - {self.legacy_b}\n'
                    + ('legacy_steps:\n' + ''.join(f'  - {s}\n' for s in legacy_steps)
                       if legacy_steps else ''))

    def write_product_clocks(self, clocks_yaml):
        """Overwrite ``products/sample.yaml`` with a ``clocks:`` block (``batch`` is a legacy
        step no ``asf`` implementation owns, so it must be declared ``off`` for
        ``scheduler.clocks()`` to accept a clock that ticks it)."""
        with open(os.path.join(self.asf_home, 'products', 'sample.yaml'), 'w') as f:
            f.write('product: sample\nrepo_slug: acme/sample\n'
                    f'repo_dir: {self.repo_dir}\nmain: main\n'
                    f'backlog_dir: {self.backlog_dir}\n'
                    'steps:\n  batch: off\n'
                    'clocks:\n' + clocks_yaml)

    def set_manifest(self, text, rc):
        with open(os.path.join(self.stub_dir, 'manifest.txt'), 'w') as f:
            f.write(text)
        with open(os.path.join(self.stub_dir, 'manifest-rc'), 'w') as f:
            f.write(str(rc))

    def set_job_runs(self, runs, commit_subject='tick: state 2026-09-21T17:04:00Z'):
        """Whether the fake launchd actually runs the job it is handed.

        ``True`` writes a hook the stub calls on bootstrap, which does what the record tick
        would have done: push a ``tick: state`` commit to the record's origin.
        """
        hook = os.path.join(self.statedir, 'on-bootstrap.sh')
        template = os.path.join(self.statedir, 'print-template.txt')
        if not runs:
            for path in (hook, template):
                if os.path.exists(path):
                    os.remove(path)
            return
        with open(hook, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n'
                    f'case "$1" in *.record) ;; *) exit 0 ;; esac\n'
                    f'export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=t@example.invalid\n'
                    f'export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=t@example.invalid\n'
                    f'git -C {self.backlog_dir} commit -q --allow-empty '
                    f'-m "{commit_subject}"\n'
                    f'git -C {self.backlog_dir} push -q origin HEAD\n')
        os.chmod(hook, 0o755)

    def load_legacy_job(self, label, script_path):
        """Put a loaded legacy job on the fake launchd that runs ``script_path``."""
        import plistlib
        agents = os.path.join(self.home, 'Library', 'LaunchAgents')
        os.makedirs(agents, exist_ok=True)
        path = os.path.join(agents, f'{label}.plist')
        with open(path, 'wb') as f:
            plistlib.dump({'Label': label, 'ProgramArguments': ['/bin/bash', script_path]}, f)
        fake_loaded(self.statedir, [label])
        return path

    def run_script(self, script, args, timeout=SCRIPT_TIMEOUT_S):
        env = dict(os.environ)
        env['ASF_HOME'] = self.asf_home
        env['HOME'] = self.home
        env['STUB_DIR'] = self.stub_dir
        env['ASF_CMD'] = self.asf_stub
        env['FAKE_LAUNCHCTL_DIR'] = self.statedir
        env['PATH'] = self.bindir + os.pathsep + env.get('PATH', '')
        return subprocess.run(['bash', script] + args, env=env, capture_output=True, text=True,
                              timeout=timeout)

    def cutover(self, args, **kw):
        return self.run_script(CUTOVER, args, **kw)

    def rollback(self, args, **kw):
        return self.run_script(ROLLBACK, args, **kw)

    def marker_path(self):
        return os.path.join(self.asf_home, 'state', 'sample', 'cutover-done')

    def agents_dir(self):
        return os.path.join(self.home, 'Library', 'LaunchAgents')

    def _manifest_rows(self):
        with open(self.marker_path()) as f:
            date = f.read().strip()
        path = os.path.join(self.asf_home, 'state', 'sample', 'retired', date, 'manifest.tsv')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [line.rstrip('\n').split('\t') for line in f if line.strip()]

    def _write_tree(self, root, table_text):
        os.makedirs(os.path.join(root, 'tables'), exist_ok=True)
        with open(os.path.join(root, 'index.json'), 'w') as f:
            json.dump({'items': []}, f)
        for name in ('roadmap', 'backlog', 'parity', 'prod', 'sessions', 'status'):
            with open(os.path.join(root, 'tables', f'{name}.md'), 'w') as f:
                f.write(f'stamp line\n{table_text}\n')


class CutoverFixtureTest(CutoverFixture, unittest.TestCase):
    """A fresh fixture per test: the tests that change it before the script runs."""

    def setUp(self):
        self.build(REPOS.fresh())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_gate_1_refuses_a_dir_a_loaded_job_still_references(self):
        script = os.path.join(self.legacy_a, 'old-tool.py')
        self.write_config('  legacy_labels: [old.factory.*]\n')
        self.load_legacy_job('old.factory.dispatch', script)

        result = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        expected = (f'cutover: {self.legacy_a} is still used by loaded job '
                    f'old.factory.dispatch ({script}) — retire the job first or declare the '
                    f'step under legacy_steps')
        self.assertIn(expected, result.stdout + result.stderr)
        # refused before anything changed
        self.assertFalse(os.path.exists(self.marker_path()))
        self.assertTrue(os.path.isdir(self.legacy_a))
        with open(self.tick_file) as f:
            self.assertNotIn('ASF:CUTOVER', f.read())

    def test_force_does_not_bypass_gate_1(self):
        self.write_config('  legacy_labels: [old.factory.*]\n')
        self.load_legacy_job('old.factory.dispatch', os.path.join(self.legacy_a, 'old-tool.py'))
        result = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(result.returncode, 3)

    def test_gate_1_excuses_a_job_declared_under_legacy_steps(self):
        self.write_config('  legacy_labels: [old.factory.*]\n',
                          legacy_steps=['old.factory.dispatch'])
        self.load_legacy_job('old.factory.dispatch', os.path.join(self.legacy_a, 'old-tool.py'))
        result = self.cutover(['sample', '--force'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('1 referenced-dirs · pass ·', result.stdout)

    def test_gate_2_refuses_an_incomplete_manifest(self):
        self.set_manifest('step · owner · command\n'
                          'record · asf · asf tick\n'
                          'wave ·  · no owner for step wave\n', 2)
        result = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('no owner for step wave', result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.marker_path()))
        self.assertTrue(os.path.isdir(self.legacy_a))

    def test_force_does_not_bypass_gate_2(self):
        self.set_manifest('wave ·  · no owner for step wave\n', 2)
        self.assertEqual(self.cutover(['sample', '--force', '--apply']).returncode, 3)

    def test_gate_3_rolls_back_when_the_installed_job_never_completes(self):
        """B-0014 (b) end to end: the job loads, never runs, and the kit must undo itself."""
        self.set_job_runs(False)
        result = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn('cutover: installed job did not complete — rolled back',
                      result.stdout + result.stderr)
        # rolled back: nothing this run changed survives
        self.assertFalse(os.path.exists(self.marker_path()))
        self.assertTrue(os.path.isdir(self.legacy_a))
        self.assertTrue(os.path.isfile(os.path.join(self.legacy_a, 'old-tool.py')))
        with open(self.tick_file) as f:
            self.assertNotIn('ASF:CUTOVER', f.read())
        self.assertFalse(os.path.exists(
            os.path.join(self.agents_dir(), 'asf.sample.record.plist')))

    def test_cutover_skips_an_unowned_clock(self):
        self.set_manifest(RECORD_ONLY_MANIFEST, 0)
        result = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('leaving the legacy job loaded', result.stdout)
        self.assertFalse(os.path.isfile(
            os.path.join(self.agents_dir(), 'asf.sample.dispatch.plist')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.agents_dir(), 'asf.sample.record.plist')))

    def test_cutover_without_clocks_stops_before_changing_anything(self):
        with open(os.path.join(self.asf_home, 'products', 'sample.yaml'), 'w') as f:
            f.write('product: sample\nrepo_slug: acme/sample\n'
                    f'repo_dir: {self.repo_dir}\nmain: main\n'
                    f'backlog_dir: {self.backlog_dir}\n')
        result = self.cutover(['sample', '--force', '--apply'])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('NEEDS OPERATOR', result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.marker_path()))
        self.assertFalse(os.path.isdir(self.agents_dir()))
        self.assertTrue(os.path.isdir(self.legacy_a))

    def test_ref_is_forwarded_to_shadow_diff(self):
        shadow = os.path.join(self.asf_home, 'state', 'sample', 'shadow')
        ref = os.path.join(self.tmp, 'ref')
        self._write_tree(shadow, '| a | b |')
        self._write_tree(ref, '| a | b |')
        result = self.cutover(['sample', '--ref', ref, '--force'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('shadow-diff: clean', result.stdout)

    def test_ref_that_differs_from_the_shadow_fails_the_gate(self):
        shadow = os.path.join(self.asf_home, 'state', 'sample', 'shadow')
        ref = os.path.join(self.tmp, 'ref')
        self._write_tree(shadow, '| a | b |')
        self._write_tree(ref, '| a | DIFFERENT |')
        result = self.cutover(['sample', '--ref', ref])
        self.assertEqual(result.returncode, 1)
        self.assertIn('DIFFERENT', result.stdout)
        self.assertIn('refusing', result.stdout + result.stderr)

    def test_rollback_without_prior_cutover_refuses(self):
        result = self.rollback(['sample', '--apply'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('nothing to roll back', result.stdout + result.stderr)


class SharedFixture(CutoverFixture):
    """One fixture per class, taken to its starting state once (:meth:`prepare`, which runs the
    scripts) and snapshotted; every test then starts from a copy of that snapshot put back at the
    same absolute path — the paths the scripts wrote into plists, the config and the manifest
    stay true — instead of running the scripts again to get there. A test may change the fixture
    as it likes: the next one gets the snapshot back."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fx = cls.__new__(cls)
        fx.build(REPOS.fresh())
        cls.addClassCleanup(shutil.rmtree, fx.tmp, True)
        fx.prepare()
        snapshot = tempfile.mkdtemp(prefix='asf-cutover-snapshot-')
        cls.addClassCleanup(shutil.rmtree, snapshot, True)
        os.rmdir(snapshot)
        shutil.copytree(fx.tmp, snapshot, symlinks=True)
        cls.snapshot = snapshot
        cls.fixture = dict(vars(fx))

    def setUp(self):
        self.__dict__.update(self.fixture)
        shutil.rmtree(self.tmp)
        shutil.copytree(self.snapshot, self.tmp, symlinks=True)

    def prepare(self):
        raise NotImplementedError


class DryRunTest(SharedFixture, unittest.TestCase):
    """The untouched fixture, dry-run once with ``--force`` and once without."""

    def prepare(self):
        self.forced = self.cutover(['sample', '--force'])
        self.bare = self.cutover(['sample'])

    def test_gate_table_is_printed_in_dry_run(self):
        result = self.forced
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('gate · result · line', result.stdout)
        self.assertIn('1 referenced-dirs · pass ·', result.stdout)
        self.assertIn('2 manifest · pass ·', result.stdout)
        self.assertIn('3 installed-job-runs · skip ·', result.stdout)

    def test_dry_run_changes_nothing(self):
        result = self.forced
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('== CUTOVER sample (dry-run)', result.stdout)
        self.assertIn('would update', result.stdout + result.stderr)
        self.assertIn('would install asf.sample.record', result.stdout)
        # nothing touched: no marker, no retired dir, tick/plugin files untouched
        self.assertFalse(os.path.exists(self.marker_path()))
        with open(self.tick_file) as f:
            self.assertNotIn('ASF:CUTOVER', f.read())
        self.assertTrue(os.path.isdir(self.legacy_a))
        self.assertTrue(os.path.isdir(self.legacy_b))
        self.assertFalse(os.path.isdir(self.agents_dir()))

    def test_dry_run_refuses_without_force_when_shadow_diff_unavailable(self):
        result = self.bare
        self.assertEqual(result.returncode, 1)
        self.assertIn('refusing', result.stdout + result.stderr)

    def test_missing_ref_says_so(self):
        result = self.bare
        self.assertIn('--ref', result.stdout + result.stderr)


class AppliedTest(SharedFixture, unittest.TestCase):
    """The fixture cut over once (``--force --apply``), with an old job under
    ``scheduler.launchd_label`` for the cutover to boot out and a rollback to put back."""

    def prepare(self):
        import plistlib
        agents = self.agents_dir()
        os.makedirs(agents, exist_ok=True)
        self.old_plist = os.path.join(agents, 'old.factory.tick.plist')
        with open(self.old_plist, 'wb') as f:
            plistlib.dump({'Label': 'old.factory.tick',
                           'ProgramArguments': ['/bin/bash', '/somewhere/else/run.sh']}, f)
        self.write_config('  launchd_label: old.factory.tick\n')
        with open(self.tick_file) as f:
            self.original_tick = f.read()
        with open(os.path.join(self.plugin_dir, 'board.md')) as f:
            self.original_plugin = f.read()
        self.applied = self.cutover(['sample', '--force', '--apply'])

    def test_apply_produces_a_factory_that_commits_tick_state(self):
        result = self.applied
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('completed a run, the record\'s origin has it', result.stdout)

        subject = subprocess.run(['git', '-C', self.origin_dir, 'log', '-1', '--format=%s'],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(subject.startswith('tick: state'), subject)
        self.assertTrue(os.path.exists(self.marker_path()))

    def test_cutover_installs_the_declared_clocks(self):
        result = self.applied
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for label in ('asf.sample.record', 'asf.sample.dispatch', 'asf.sample.daily'):
            self.assertTrue(os.path.isfile(os.path.join(self.agents_dir(), f'{label}.plist')),
                            f'{label} not installed:\n{result.stdout}')
        installs = [row for row in self._manifest_rows() if row[0] == 'scheduler_install']
        self.assertEqual(sorted(row[3] for row in installs),
                         sorted(['asf.sample.record', 'asf.sample.dispatch', 'asf.sample.daily']))

    def test_the_installed_job_is_runnable(self):
        """B-0014 (b): absolute interpreter, a working directory, PYTHONPATH and log paths."""
        import plistlib
        import sys
        with open(os.path.join(self.agents_dir(), 'asf.sample.record.plist'), 'rb') as f:
            data = plistlib.load(f)
        self.assertEqual(data['ProgramArguments'][0], sys.executable)
        self.assertTrue(os.path.isdir(data['WorkingDirectory']))
        self.assertIn('PYTHONPATH', data['EnvironmentVariables'])
        self.assertIn('PATH', data['EnvironmentVariables'])
        self.assertIn('HOME', data['EnvironmentVariables'])
        self.assertTrue(data['StandardOutPath'].endswith('tick-sample-record.log'))
        self.assertTrue(data['StandardErrorPath'].endswith('tick-sample-record.log'))
        self.assertEqual(data['StartInterval'], 300)

    def test_apply_rewrites_tick_file_and_plugin_skills(self):
        result = self.applied
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
        result = self.applied
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(self.marker_path()) as f:
            date = f.read().strip()
        retired = os.path.join(self.asf_home, 'state', 'sample', 'retired', date)
        name_a = self.legacy_a.lstrip('/').replace('/', '-')
        name_b = self.legacy_b.lstrip('/').replace('/', '-')
        self.assertTrue(os.path.isdir(os.path.join(retired, name_a)))
        self.assertTrue(os.path.isfile(os.path.join(retired, name_a, 'old-tool.py')))
        self.assertTrue(os.path.isdir(os.path.join(retired, name_b)))

    def test_apply_records_a_cutover_event(self):
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
        first = self.applied
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        with open(self.tick_file) as f:
            tick_after_first = f.read()

        second = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already cut over', second.stdout)
        with open(self.tick_file) as f:
            self.assertEqual(f.read(), tick_after_first)

    # ---- rollback ------------------------------------------------------------------------------

    def test_rollback_restores_everything(self):
        apply_result = self.applied
        self.assertEqual(apply_result.returncode, 0, apply_result.stdout + apply_result.stderr)

        rollback_result = self.rollback(['sample', '--apply'])
        self.assertEqual(rollback_result.returncode, 0,
                         rollback_result.stdout + rollback_result.stderr)

        with open(self.tick_file) as f:
            self.assertEqual(f.read(), self.original_tick)
        with open(os.path.join(self.plugin_dir, 'board.md')) as f:
            self.assertEqual(f.read(), self.original_plugin)
        self.assertTrue(os.path.isdir(self.legacy_a))
        self.assertTrue(os.path.isfile(os.path.join(self.legacy_a, 'old-tool.py')))
        self.assertTrue(os.path.isdir(self.legacy_b))
        self.assertFalse(os.path.exists(self.marker_path()))

    def test_rollback_removes_the_jobs_cutover_installed(self):
        for label in ('asf.sample.record', 'asf.sample.dispatch',
                      'asf.sample.daily'):
            self.assertTrue(os.path.isfile(os.path.join(self.agents_dir(), f'{label}.plist')))

        result = self.rollback(['sample', '--apply'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for label in ('asf.sample.record', 'asf.sample.dispatch',
                      'asf.sample.daily'):
            self.assertFalse(os.path.isfile(os.path.join(self.agents_dir(), f'{label}.plist')),
                             label)
        with open(os.path.join(self.statedir, 'booted-out.txt')) as f:
            self.assertIn('asf.sample.record', f.read())

    def test_rollback_reloads_the_job_cutover_booted_out(self):
        result = self.applied
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.old_plist), 'the old job should have been retired')

        rb = self.rollback(['sample', '--apply'])
        self.assertEqual(rb.returncode, 0, rb.stdout + rb.stderr)
        self.assertTrue(os.path.exists(self.old_plist), 'rollback must put the old job back')
        with open(os.path.join(self.statedir, 'bootstrapped.txt')) as f:
            self.assertIn('old.factory.tick.plist', f.read())

    def test_rollback_dry_run_changes_nothing(self):
        with open(self.tick_file) as f:
            tick_after_cutover = f.read()

        result = self.rollback(['sample'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('would restore', result.stdout)
        with open(self.tick_file) as f:
            self.assertEqual(f.read(), tick_after_cutover)
        self.assertTrue(os.path.exists(self.marker_path()))

    def test_cutover_then_rollback_then_cutover_again(self):
        r1 = self.applied
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        r2 = self.rollback(['sample', '--apply'])
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        r3 = self.cutover(['sample', '--force', '--apply'])
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        with open(self.tick_file) as f:
            self.assertIn('asf tick --product sample', f.read())


if __name__ == '__main__':
    unittest.main()
