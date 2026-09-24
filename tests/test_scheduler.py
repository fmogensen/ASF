"""asf.scheduler against a fake ``launchctl``.

The fake is a shell stub written into the test's own temp dir and put first on ``PATH``: it
appends every invocation's argv to a log (so a test can prove ``install`` boots out before it
bootstraps) and answers ``list``/``print`` from files the test drops next to it. Nothing here
talks to the real launchd, and no test loads a job on the machine running it.

:func:`fake_launchctl` is shared with ``test_doctor`` and ``test_cutover``.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import env, scheduler, snapshot
from asf.scheduler import Clock

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')

_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_DIR/argv.log"
case "$1" in
  list)
    if [ -f "$FAKE_LAUNCHCTL_DIR/list.txt" ]; then cat "$FAKE_LAUNCHCTL_DIR/list.txt"; fi
    exit 0
    ;;
  print)
    label="${2##*/}"
    if [ -f "$FAKE_LAUNCHCTL_DIR/print-$label.txt" ]; then
      cat "$FAKE_LAUNCHCTL_DIR/print-$label.txt"
      exit 0
    fi
    echo "Could not find service \"$label\" in domain for gui" >&2
    exit 113
    ;;
  bootout)
    label="${2##*/}"
    printf '%s\n' "$label" >> "$FAKE_LAUNCHCTL_DIR/booted-out.txt"
    if [ -f "$FAKE_LAUNCHCTL_DIR/print-$label.txt" ]; then exit 0; fi
    exit 3
    ;;
  bootstrap)
    printf '%s\n' "$3" >> "$FAKE_LAUNCHCTL_DIR/bootstrapped.txt"
    label="$(basename "$3" .plist)"
    # Stand in for launchd actually running the job: a template makes `print` answer for the
    # new label, and a hook script lets a test do what the job itself would have done.
    if [ -f "$FAKE_LAUNCHCTL_DIR/print-template.txt" ]; then
      sed "s/@LABEL@/$label/g" "$FAKE_LAUNCHCTL_DIR/print-template.txt" \
        > "$FAKE_LAUNCHCTL_DIR/print-$label.txt"
    fi
    if [ -x "$FAKE_LAUNCHCTL_DIR/on-bootstrap.sh" ]; then
      "$FAKE_LAUNCHCTL_DIR/on-bootstrap.sh" "$label" || true
    fi
    exit 0
    ;;
esac
exit 0
"""


def fake_launchctl(directory):
    """Install the stub into ``<directory>/bin`` and return its state dir (put ``bin`` on PATH)."""
    bindir = os.path.join(directory, 'bin')
    statedir = os.path.join(directory, 'launchctl-state')
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(statedir, exist_ok=True)
    stub = os.path.join(bindir, 'launchctl')
    with open(stub, 'w', encoding='utf-8') as f:
        f.write(_STUB)
    os.chmod(stub, 0o755)
    return bindir, statedir


#: The CLIs `asf doctor` probes for a login (``asf.doctor._CLI_TOOLS``, git excepted — git is
#: real everywhere in the suite). On the operator's machine each probe is a network round trip
#: (`gh auth status`, `vercel whoami`: 1.6–8 s a call, B-0071), and its answer is that
#: machine's, not the fixture's.
DOCTOR_CLIS = ('gh', 'gcloud', 'az', 'aws', 'flyctl', 'vercel')


def fake_clis(bindir, names=DOCTOR_CLIS, rc=0):
    """Put an instant stub for each CLI in ``names`` into ``bindir`` (first on PATH): one line
    of output, exit ``rc``. A test that wants one of them to answer differently writes its own
    stub over it (``test_sample_product`` keeps ``gh`` offline)."""
    os.makedirs(bindir, exist_ok=True)
    for name in names:
        stub = os.path.join(bindir, name)
        with open(stub, 'w', encoding='utf-8') as f:
            f.write(f'#!/bin/sh\necho "{name}: stubbed in tests"\nexit {rc}\n')
        os.chmod(stub, 0o755)
    return bindir


def fake_loaded(statedir, labels):
    """Tell the stub which labels ``launchctl list`` reports."""
    with open(os.path.join(statedir, 'list.txt'), 'w', encoding='utf-8') as f:
        f.write('PID\tStatus\tLabel\n')
        for pid, label in enumerate(labels, start=100):
            f.write(f'{pid}\t0\t{label}\n')


def fake_print(statedir, label, text):
    with open(os.path.join(statedir, f'print-{label}.txt'), 'w', encoding='utf-8') as f:
        f.write(text)


def stub_argv(statedir):
    path = os.path.join(statedir, 'argv.log')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
        return f.read()


class SchedulerTestCase(unittest.TestCase):
    """A temp ASF_HOME, a temp HOME (so ``~/Library/LaunchAgents`` is the test's), a fake
    launchctl first on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-scheduler-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.asf_home = os.path.join(self.tmp, 'ASF')
        os.makedirs(os.path.join(self.asf_home, 'products'))
        os.makedirs(self.home)
        self.bindir, self.statedir = fake_launchctl(self.tmp)

        self._old_env = dict(os.environ)
        self._old_asf_home = env.ASF_HOME
        os.environ['HOME'] = self.home
        os.environ['PATH'] = self.bindir + os.pathsep + os.environ.get('PATH', '')
        os.environ['FAKE_LAUNCHCTL_DIR'] = self.statedir
        env.ASF_HOME = self.asf_home
        self.addCleanup(self._restore)

        self.repo_dir = os.path.join(self.tmp, 'repo')
        self.backlog_dir = os.path.join(self.tmp, 'backlog')
        os.makedirs(self.repo_dir)
        os.makedirs(self.backlog_dir)
        self.write_config()
        with open(os.path.join(self.asf_home, 'products', 'sample.yaml'), 'w') as f:
            f.write('product: sample\nrepo_slug: acme/sample\n'
                    f'repo_dir: {self.repo_dir}\nmain: main\nbacklog_dir: {self.backlog_dir}\n')

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        env.ASF_HOME = self._old_asf_home

    def write_config(self, extra=''):
        with open(os.path.join(self.asf_home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write('schema_version: 1\ndefault_product: sample\n'
                    'scheduler:\n  kind: launchd\n  label_prefix: asf\n' + extra)

    def write_product(self, clocks_yaml, steps_yaml=''):
        """Overwrite ``products/sample.yaml`` with a ``clocks:`` block (and any ``steps:``)."""
        with open(os.path.join(self.asf_home, 'products', 'sample.yaml'), 'w') as f:
            f.write('product: sample\nrepo_slug: acme/sample\n'
                    f'repo_dir: {self.repo_dir}\nmain: main\nbacklog_dir: {self.backlog_dir}\n'
                    + steps_yaml + 'clocks:\n' + clocks_yaml)


class RenderTest(SchedulerTestCase):
    RECORD = Clock('record', ['record'], False, 600, None)
    DISPATCH = Clock('dispatch', ['health', 'wave', 'prs', 'batch'], False, 300, None)
    DAILY = Clock('daily', ['daily'], False, None, {'Hour': 6, 'Minute': 50})

    def test_launchd_golden_from_a_checkout_runs_the_snapshot_launcher(self):
        """The clock never imports from a moving checkout: it runs the launcher copied into the
        state dir, which ticks from a snapshot of HEAD (the card: the factory's own clock
        imports from the live dev checkout)."""
        root = self._checkout()
        with mock.patch.object(scheduler, 'repo_root', return_value=root):
            job = scheduler.render('sample', self.RECORD)
        code = os.path.join(self.asf_home, 'state', 'sample', 'code')
        launcher = os.path.join(code, 'launch.py')
        self.assertEqual(job['launcher'], launcher)
        self.assertEqual(job['plist']['ProgramArguments'], [
            sys.executable, launcher, '--repo', root, '--code-dir', code, '--',
            '-m', 'asf.cli', 'tick', '--product', 'sample', '--steps', 'record'])
        self.assertEqual(job['plist']['WorkingDirectory'], code)
        self.assertNotIn('PYTHONPATH', job['plist']['EnvironmentVariables'])
        self.assertEqual(job['plist']['EnvironmentVariables']['ASF_HOME'], self.asf_home)
        # read back as the same clock
        self.assertEqual(scheduler.clock_of_plist('asf.sample.record', job['plist'], 'sample'),
                         self.RECORD)
        # install copies the launcher (the module, byte for byte) and makes the cwd
        scheduler.install(job)
        with open(launcher, encoding='utf-8') as f, open(snapshot.__file__, encoding='utf-8') as g:
            self.assertEqual(f.read(), g.read())

    def test_cron_line_from_a_checkout_runs_the_snapshot_launcher(self):
        root = self._checkout()
        with mock.patch.object(scheduler, 'repo_root', return_value=root), \
                mock.patch.object(scheduler, 'kind', return_value='cron'):
            job = scheduler.render('sample', self.RECORD)
        code = os.path.join(self.asf_home, 'state', 'sample', 'code')
        self.assertIn(f'cd {code} && ', job['line'])
        self.assertIn(f"launch.py --repo {root} --code-dir {code} -- -m asf.cli tick", job['line'])
        self.assertNotIn('PYTHONPATH', job['line'])

    def _checkout(self):
        root = os.path.join(self.tmp, 'checkout')
        os.makedirs(os.path.join(root, '.git'))
        return root

    def test_launchd_golden(self):
        """An installed package (no ``.git``) does not move under the clock: it ticks in place."""
        root = os.path.join(self.tmp, 'site-packages')
        os.makedirs(os.path.join(root, 'asf'))
        with mock.patch.object(scheduler, 'repo_root', return_value=root):
            job = scheduler.render('sample', self.RECORD)
        self.assertEqual(job['kind'], 'launchd')
        self.assertEqual(job['label'], 'asf.sample.record')
        self.assertEqual(job['path'],
                         os.path.join(self.home, 'Library', 'LaunchAgents',
                                      'asf.sample.record.plist'))
        self.assertEqual(job['plist'], {
            'Label': 'asf.sample.record',
            'ProgramArguments': [sys.executable, '-m', 'asf.cli', 'tick',
                                 '--product', 'sample', '--steps', 'record'],
            'WorkingDirectory': root,
            'EnvironmentVariables': {
                'PATH': os.pathsep.join(
                    d for d in dict.fromkeys(os.environ['PATH'].split(os.pathsep))
                    if d and os.path.isabs(d)),
                'HOME': self.home,
                'PYTHONPATH': root,
                'ASF_HOME': self.asf_home,
            },
            'StandardOutPath': os.path.join(self.asf_home, 'logs', 'tick-sample-record.log'),
            'StandardErrorPath': os.path.join(self.asf_home, 'logs', 'tick-sample-record.log'),
            'RunAtLoad': True,
            'StartInterval': 600,
        })

    def test_the_interpreter_is_absolute_and_the_package_importable(self):
        """B-0014 (b): a bare `python3` with no PYTHONPATH is a job that cannot run."""
        with mock.patch.object(scheduler, 'snapshot_repo', return_value=None):
            job = scheduler.render('sample', self.RECORD)
        argv = job['plist']['ProgramArguments']
        self.assertTrue(os.path.isabs(argv[0]), argv[0])
        self.assertNotEqual(argv[0], 'python3')
        self.assertTrue(os.path.isdir(job['plist']['WorkingDirectory']))
        self.assertTrue(os.path.isfile(
            os.path.join(job['plist']['EnvironmentVariables']['PYTHONPATH'], 'asf', 'cli.py')))
        # from a checkout: the launcher's directory exists once installed, and it is runnable
        with mock.patch.object(scheduler, 'snapshot_repo', return_value=scheduler.repo_root()):
            job = scheduler.render('sample', self.RECORD)
        scheduler.install(job)
        self.assertTrue(os.path.isdir(job['plist']['WorkingDirectory']))
        self.assertTrue(os.path.isfile(job['plist']['ProgramArguments'][1]))

    def test_path_keeps_only_absolute_entries(self):
        os.environ['PATH'] = os.pathsep.join([self.bindir, '.', '', 'relative/bin', '/usr/bin'])
        job = scheduler.render('sample', self.RECORD)
        self.assertEqual(job['plist']['EnvironmentVariables']['PATH'],
                         os.pathsep.join([self.bindir, '/usr/bin']))

    def test_multi_step_label_and_log(self):
        job = scheduler.render('sample', self.DISPATCH)
        self.assertEqual(job['label'], 'asf.sample.dispatch')
        self.assertEqual(job['plist']['ProgramArguments'][-2:],
                         ['--steps', 'health,wave,prs,batch'])
        self.assertTrue(job['log'].endswith('tick-sample-dispatch.log'))
        self.assertEqual(job['plist']['StartInterval'], 300)

    def test_daily_is_a_calendar_job(self):
        job = scheduler.render('sample', self.DAILY)
        self.assertEqual(job['plist']['StartCalendarInterval'], {'Hour': 6, 'Minute': 50})
        self.assertNotIn('StartInterval', job['plist'])
        self.assertEqual(job['plist']['ProgramArguments'][-2:], ['--steps', 'daily'])

    def test_daily_job_runs_the_daily_step_only(self):
        # a bare `--daily` means "every step, and daily even if it ran today": the daily job then
        # ran record/wave/harvest beside the interval job, on the same record clone
        argv = scheduler.render('sample', self.DAILY)['plist']['ProgramArguments']
        self.assertNotIn('--daily', argv)
        self.assertEqual(argv[argv.index('--steps') + 1], 'daily')

    def test_daily_job_reads_back_as_its_clock(self):
        job = scheduler.render('sample', self.DAILY)
        back = scheduler.clock_of_plist(job['label'], job['plist'], 'sample')
        self.assertEqual(back.steps, ['daily'])

    def test_label_prefix_comes_from_config(self):
        cfg = {'scheduler': {'kind': 'launchd', 'label_prefix': 'factory'}}
        job = scheduler.render('sample', self.RECORD, cfg=cfg)
        self.assertEqual(job['label'], 'factory.sample.record')

    def test_plist_xml_round_trips(self):
        job = scheduler.render('sample', self.RECORD)
        parsed = plistlib.loads(scheduler.render_plist(job).encode('utf-8'))
        self.assertEqual(parsed, job['plist'])

    def test_cron_renders_a_line(self):
        cfg = {'scheduler': {'kind': 'cron'}}
        job = scheduler.render('sample', self.RECORD, cfg=cfg)
        self.assertEqual(job['kind'], 'cron')
        self.assertTrue(job['line'].startswith('*/10 * * * * cd '))
        self.assertIn(sys.executable, job['line'])
        self.assertIn('tick-sample-record.log', job['line'])

    def test_unknown_kind_needs_an_operator(self):
        cfg = {'scheduler': {'kind': 'systemd'}}
        job = scheduler.render('sample', self.RECORD, cfg=cfg)
        self.assertTrue(job['needs_operator'].startswith('NEEDS OPERATOR:'))
        self.assertIn('systemd', job['needs_operator'])
        self.assertEqual(scheduler.install(job), [job['needs_operator']])


class InstallTest(SchedulerTestCase):
    RECORD = Clock('record', ['record'], False, 600, None)

    def test_install_writes_the_plist_and_boots_out_before_bootstrap(self):
        job = scheduler.render('sample', self.RECORD)
        lines = scheduler.install(job)
        self.assertTrue(os.path.isfile(job['path']))
        with open(job['path'], 'rb') as f:
            self.assertEqual(plistlib.load(f), job['plist'])
        argv = stub_argv(self.statedir)
        self.assertEqual(len(argv), 2, argv)
        self.assertTrue(argv[0].startswith('bootout gui/'))
        self.assertTrue(argv[0].endswith('/asf.sample.record'))
        self.assertTrue(argv[1].startswith('bootstrap gui/'))
        self.assertIn(job['path'], argv[1])
        self.assertIn('bootstrapped asf.sample.record', ' '.join(lines))

    def test_install_survives_a_bootout_that_fails(self):
        """Nothing holds the label on a first install — bootout failing there is not an error."""
        job = scheduler.render('sample', self.RECORD)
        lines = scheduler.install(job)
        self.assertNotIn('failed', ' '.join(lines))

    def test_install_creates_the_log_directory(self):
        job = scheduler.render('sample', self.RECORD)
        scheduler.install(job)
        self.assertTrue(os.path.isdir(os.path.join(self.asf_home, 'logs')))

    def test_uninstall_boots_out_and_removes_the_plist(self):
        job = scheduler.render('sample', self.RECORD)
        scheduler.install(job)
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))
        lines = scheduler.uninstall('asf.sample.record')
        self.assertFalse(os.path.exists(job['path']))
        self.assertIn('booted out asf.sample.record', ' '.join(lines))

    def test_cron_install_only_prints(self):
        cfg = {'scheduler': {'kind': 'cron'}}
        job = scheduler.render('sample', self.RECORD, cfg=cfg)
        lines = scheduler.install(job)
        self.assertTrue(any(l.startswith('NEEDS OPERATOR:') for l in lines))
        self.assertEqual(stub_argv(self.statedir), [])


class StatusTest(SchedulerTestCase):
    def test_parses_a_captured_sample(self):
        parsed = scheduler.parse_print(read_fixture('launchctl-print.txt'))
        self.assertEqual(parsed['state'], 'running')
        self.assertEqual(parsed['runs'], 7)
        self.assertEqual(parsed['last_exit'], 0)
        self.assertFalse(parsed['never_exited'])
        self.assertEqual(parsed['program'], '/usr/local/bin/python3.12')
        self.assertEqual(parsed['path'],
                         '/Users/operator/Library/LaunchAgents/asf.sample.record.plist')

    def test_never_exited_is_not_exit_zero(self):
        parsed = scheduler.parse_print(read_fixture('launchctl-print-never-exited.txt'))
        self.assertEqual(parsed['runs'], 0)
        self.assertIsNone(parsed['last_exit'])
        self.assertTrue(parsed['never_exited'])
        self.assertEqual(parsed['state'], 'not running')

    def test_status_reads_through_launchctl(self):
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))
        info = scheduler.status('asf.sample.record')
        self.assertTrue(info['loaded'])
        self.assertEqual(info['runs'], 7)
        self.assertEqual(info['last_exit'], 0)

    def test_status_of_an_unknown_label_is_not_loaded(self):
        info = scheduler.status('asf.sample.nope')
        self.assertFalse(info['loaded'])


class LoadedJobsTest(SchedulerTestCase):
    def _write_plist(self, label, argv, working_dir=None):
        path = os.path.join(self.home, 'Library', 'LaunchAgents', f'{label}.plist')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {'Label': label, 'ProgramArguments': argv}
        if working_dir:
            data['WorkingDirectory'] = working_dir
        with open(path, 'wb') as f:
            plistlib.dump(data, f)
        return path

    def test_lists_our_prefix_and_the_configured_legacy_labels(self):
        self.write_config('  legacy_labels:\n    - old.factory.*\n')
        legacy_dir = os.path.join(self.tmp, 'legacy')
        os.makedirs(legacy_dir)
        self._write_plist('asf.sample.record',
                          [sys.executable, '-m', 'asf.cli', 'tick', '--product', 'sample'])
        self._write_plist('old.factory.dispatch',
                          ['/bin/bash', os.path.join(legacy_dir, 'dispatch.sh')])
        fake_loaded(self.statedir,
                    ['com.apple.something', 'asf.sample.record', 'old.factory.dispatch'])

        jobs = scheduler.loaded_jobs()
        self.assertEqual(sorted(j['label'] for j in jobs),
                         ['asf.sample.record', 'old.factory.dispatch'])
        legacy = [j for j in jobs if j['label'] == 'old.factory.dispatch'][0]
        self.assertIn(os.path.join(legacy_dir, 'dispatch.sh'), legacy['paths'])

    def test_legacy_labels_default_to_empty(self):
        """Undeclared legacy labels are invisible — the operator names them, we never guess."""
        self._write_plist('old.factory.dispatch', ['/bin/bash', '/tmp/x.sh'])
        fake_loaded(self.statedir, ['asf.sample.record', 'old.factory.dispatch'])
        self.assertEqual([j['label'] for j in scheduler.loaded_jobs()], ['asf.sample.record'])

    def test_working_directory_counts_as_a_referenced_path(self):
        legacy_dir = os.path.join(self.tmp, 'legacy')
        os.makedirs(legacy_dir)
        self.write_config('  legacy_labels: [old.*]\n')
        self._write_plist('old.dispatch', ['/bin/bash', 'run.sh'], working_dir=legacy_dir)
        fake_loaded(self.statedir, ['old.dispatch'])
        jobs = scheduler.loaded_jobs()
        self.assertEqual(scheduler.jobs_using(legacy_dir, jobs),
                         [('old.dispatch', legacy_dir)])

    def test_jobs_using_matches_only_inside_the_directory(self):
        jobs = [{'label': 'a', 'paths': ['/opt/legacy-tools/run.sh']},
                {'label': 'b', 'paths': ['/opt/legacy-tools-2/run.sh']}]
        self.assertEqual(scheduler.jobs_using('/opt/legacy-tools', jobs),
                         [('a', '/opt/legacy-tools/run.sh')])

    def test_a_job_with_no_readable_plist_still_lists(self):
        fake_loaded(self.statedir, ['asf.sample.record'])
        jobs = scheduler.loaded_jobs()
        self.assertEqual(jobs[0]['label'], 'asf.sample.record')
        self.assertEqual(jobs[0]['paths'], [])


class CliTest(SchedulerTestCase):
    """The subcommand surface. Driven through ``python3 -m asf.scheduler``: ``asf scheduler``
    reaches the same :func:`asf.scheduler.cmd_scheduler` once ``asf/cli.py`` wires
    :func:`asf.scheduler.register`, which is the controller's one-line change, not this job's."""

    def _run(self, args):
        cmd = [sys.executable, '-m', 'asf.scheduler'] + args
        proc_env = dict(os.environ)
        proc_env['ASF_HOME'] = self.asf_home
        proc_env['PYTHONPATH'] = scheduler.repo_root()
        return subprocess.run(cmd, capture_output=True, text=True, env=proc_env,
                              cwd=scheduler.repo_root(), timeout=60)

    def test_render_prints_a_plist(self):
        self.write_product('  record:\n    steps: [record]\n    every: 5m\n')
        result = self._run(['render', '--product', 'sample', '--clock', 'record'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('# asf.sample.record\n'))
        parsed = plistlib.loads(result.stdout.split('\n', 1)[1].encode('utf-8'))
        self.assertEqual(parsed['Label'], 'asf.sample.record')

    def test_list_json_is_machine_readable(self):
        import json
        self.write_config('  legacy_labels: [old.*]\n')
        fake_loaded(self.statedir, ['old.dispatch'])
        result = self._run(['list', '--json'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([j['label'] for j in json.loads(result.stdout)], ['old.dispatch'])

    def test_status_of_a_loaded_job(self):
        self.write_product('  record:\n    steps: [record]\n    every: 5m\n')
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))
        result = self._run(['status', '--product', 'sample', '--clock', 'record'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('runs=7', result.stdout)
        self.assertIn('last-exit=0', result.stdout)


class InstalledButUndeclaredTest(SchedulerTestCase):
    """A job the adapter installed before its clock lived in the product file (T-0043) left
    ``status`` saying "declares no clocks" over a job firing every ten minutes. The job and the
    file must never disagree: status reports the installed job and the exact clock that declares
    it, and ``install`` adopts it into the file (appended — nothing above it is touched)."""

    LABEL = 'asf.sample.record-health-wave-prs-harvest'
    STEPS = ['record', 'health', 'wave', 'prs', 'harvest']
    BLOCK = ('clocks:\n  record-health-wave-prs-harvest:\n'
             '    steps: [record, health, wave, prs, harvest]\n    every: 10m\n')

    def install_old_job(self, label=None, steps=None, interval=600):
        label = label or self.LABEL
        path = scheduler.plist_path(label)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        argv = [sys.executable, '-m', 'asf.cli', 'tick', '--product', 'sample',
                '--steps', ','.join(steps or self.STEPS)]
        with open(path, 'wb') as f:
            plistlib.dump({'Label': label, 'ProgramArguments': argv,
                           'StartInterval': interval}, f)
        fake_print(self.statedir, label, read_fixture('launchctl-print.txt'))
        return path

    def product_text(self):
        with open(os.path.join(self.asf_home, 'products', 'sample.yaml'), encoding='utf-8') as f:
            return f.read()

    def _run(self, args):
        return CliTest._run(self, args)

    def test_the_clock_is_read_back_off_the_installed_plist(self):
        self.install_old_job()
        fake_loaded(self.statedir, [self.LABEL, 'asf.other.record'])
        found = scheduler.installed_clocks('sample')
        self.assertEqual([(label, c) for label, c in found],
                         [(self.LABEL, Clock('record-health-wave-prs-harvest', self.STEPS,
                                             False, 600, None))])
        self.assertEqual(scheduler.clocks_yaml([c for _l, c in found]), self.BLOCK)

    def test_status_reports_the_installed_job_and_the_clock_that_declares_it(self):
        self.install_old_job()
        fake_loaded(self.statedir, [self.LABEL])
        result = self._run(['status', '--product', 'sample'])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('declares no clocks', result.stdout)
        self.assertIn(f'{self.LABEL}  state=', result.stdout)
        self.assertIn(f'{self.LABEL} is installed but not a clock in products/sample.yaml',
                      result.stdout)
        self.assertIn(self.BLOCK, result.stdout)

    def test_status_names_an_installed_job_beside_the_declared_clocks(self):
        self.write_product('  record:\n    steps: [record]\n    every: 5m\n')
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))
        self.install_old_job()
        fake_loaded(self.statedir, ['asf.sample.record', self.LABEL])
        result = self._run(['status', '--product', 'sample'])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('asf.sample.record  state=', result.stdout)
        self.assertIn(f'{self.LABEL} is installed but not a clock in products/sample.yaml',
                      result.stdout)

    def test_install_adopts_the_installed_job_into_the_product_file(self):
        before = self.product_text()
        self.install_old_job()
        fake_loaded(self.statedir, [self.LABEL])
        result = self._run(['install', '--product', 'sample'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.product_text(), before + self.BLOCK)
        self.assertIn(f'scheduler: declared clock record-health-wave-prs-harvest in '
                      f'products/sample.yaml from the installed {self.LABEL}', result.stdout)
        self.assertIn(f'scheduler: bootstrapped {self.LABEL}', result.stdout)
        self.assertNotIn('retired', result.stdout)
        env_product = env.load_product('sample')
        self.assertEqual([scheduler.label_for('sample', c.name) for c in
                          scheduler.clocks(env_product)], [self.LABEL])


if __name__ == '__main__':
    unittest.main()
