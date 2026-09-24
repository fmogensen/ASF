import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asf import doctor, env, hooks

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_doctor` does not
    from test_scheduler import fake_clis, fake_launchctl, fake_loaded, fake_print, read_fixture
except ImportError:  # pragma: no cover - import shape only
    from tests.test_scheduler import fake_clis, fake_launchctl, fake_loaded, fake_print, read_fixture

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestCheckConfig(unittest.TestCase):
    def test_missing_config_file(self):
        with tempfile.TemporaryDirectory() as home:
            old = env.ASF_HOME
            env.ASF_HOME = home
            try:
                ok, detail = doctor.check_config('sample')[:2]
                self.assertFalse(ok)
            finally:
                env.ASF_HOME = old

    def test_product_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('schema_version: 1\n')
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write('product: sample\n')  # no repo_dir/repo_slug/backlog_dir
            old = env.ASF_HOME
            env.ASF_HOME = home
            try:
                ok, detail = doctor.check_config('sample')[:2]
                self.assertFalse(ok)
                self.assertIn('repo_dir', detail)
            finally:
                env.ASF_HOME = old

    def test_valid_config(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('schema_version: 1\n')
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write('product: sample\nrepo_dir: /tmp/x\nrepo_slug: acme/x\nbacklog_dir: /tmp/y\n')
            old = env.ASF_HOME
            env.ASF_HOME = home
            try:
                result = doctor.check_config('sample')
                self.assertTrue(result[0])
                self.assertEqual(len(result), 4)
            finally:
                env.ASF_HOME = old


class TestCheckRepoAndBacklog(unittest.TestCase):
    def test_repo_reachable(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['git', 'init', '-q'], cwd=d, check=True)
            product = env.Product('x', {'repo_dir': d})
            ok, detail = doctor.check_repo(product)
            self.assertTrue(ok, detail)

    def test_repo_missing(self):
        product = env.Product('x', {'repo_dir': '/no/such/dir/at/all'})
        ok, detail = doctor.check_repo(product)
        self.assertFalse(ok)

    def test_backlog_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            product = env.Product('x', {'backlog_dir': d})
            ok, detail = doctor.check_backlog(product)
            self.assertFalse(ok)


class TestCheckScheduler(unittest.TestCase):
    def test_non_launchd_kind_skips(self):
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'gh-actions'}})
        self.assertTrue(ok)
        self.assertIn('gh-actions', detail)

    def test_launchd_without_label_is_ok(self):
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'launchd'}})
        self.assertTrue(ok)
        self.assertIn('SCHEDULER', detail)

    def test_a_retired_pre_asf_job_is_not_red(self):
        cfg = {'scheduler': {'kind': 'launchd', 'launchd_label': 'com.example.old-cron'}}
        with mock.patch.object(doctor, '_run', return_value=(False, 'Could not find service')):
            ok, detail = doctor.check_scheduler(cfg)
        self.assertTrue(ok)
        self.assertIn('retired', detail)

    def test_a_still_loaded_pre_asf_job_says_retire_it(self):
        cfg = {'scheduler': {'kind': 'launchd', 'launchd_label': 'com.example.old-cron'}}
        with mock.patch.object(doctor, '_run', return_value=(True, '')):
            ok, detail = doctor.check_scheduler(cfg)
        self.assertIn('retire it', detail)


class TestCheckClockSteps(unittest.TestCase):
    def _product(self, clocks, steps=None):
        data = {'clocks': clocks}
        if steps:
            data['steps'] = steps
        return env.Product('x', data)

    def _all_but(self, step):
        from asf.tick import steps as tick_steps
        return [s for s in tick_steps.ASF_STEPS if s != step]

    def test_a_step_on_no_clock_is_named(self):
        from asf.tick import steps as tick_steps
        step = tick_steps.ASF_STEPS[-1]
        product = self._product({'dispatch': {'every': '5m', 'steps': self._all_but(step)}})
        lines = doctor.check_clock_steps(product)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('NEEDS OPERATOR:'))
        self.assertIn(f'step {step} ', lines[0])

    def test_every_step_on_a_clock_reports_nothing(self):
        from asf.tick import steps as tick_steps
        product = self._product({'a': {'every': '5m', 'steps': list(tick_steps.ASF_STEPS)}})
        self.assertEqual(doctor.check_clock_steps(product), [])

    def test_a_step_declared_off_is_not_reported(self):
        from asf.tick import steps as tick_steps
        step = tick_steps.ASF_STEPS[-1]
        product = self._product({'a': {'every': '5m', 'steps': self._all_but(step)}},
                                steps={step: 'off'})
        self.assertEqual(doctor.check_clock_steps(product), [])

    def test_asf_doctor_prints_the_line(self):
        import argparse
        import contextlib
        import io
        from asf.tick import steps as tick_steps
        step = tick_steps.ASF_STEPS[-1]
        product = self._product({'a': {'every': '5m', 'steps': self._all_but(step)}})
        out = io.StringIO()
        with mock.patch.object(doctor, 'run', return_value=[('config', True, True, 'ok')]), \
                mock.patch.object(doctor, 'scheduler_rows', return_value=[]), \
                mock.patch.object(env, 'load_config', return_value={}), \
                mock.patch.object(env, 'load_product', return_value=product), \
                mock.patch('asf.cli.stamp', return_value='stamp'), \
                contextlib.redirect_stdout(out):
            doctor.cmd_doctor(argparse.Namespace(product='x'), '.')
        self.assertIn(f'NEEDS OPERATOR: step {step} ', out.getvalue())


class TestOneFactoryCheck(unittest.TestCase):
    def test_no_legacy_paths_is_ok(self):
        ok, detail = doctor.check_one_factory({}, env.Product('x', {}))
        self.assertTrue(ok)

    def test_clean_when_names_unique(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, 'legacy')
            os.makedirs(legacy)
            open(os.path.join(legacy, 'old-tool.py'), 'w').close()
            repo = os.path.join(d, 'repo')
            os.makedirs(repo)
            cfg = {'legacy_paths': [legacy]}
            product = env.Product('x', {'repo_dir': repo})
            ok, detail = doctor.check_one_factory(cfg, product)
            self.assertTrue(ok, detail)

    def test_red_when_duplicated_in_repo(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, 'legacy')
            os.makedirs(legacy)
            open(os.path.join(legacy, 'old-tool.py'), 'w').close()
            repo = os.path.join(d, 'repo')
            scripts = os.path.join(repo, 'scripts')
            os.makedirs(scripts)
            open(os.path.join(scripts, 'old-tool.py'), 'w').close()
            cfg = {'legacy_paths': [legacy]}
            product = env.Product('x', {'repo_dir': repo})
            ok, detail = doctor.check_one_factory(cfg, product)
            self.assertFalse(ok)
            self.assertIn('old-tool.py', detail)

    def test_red_when_duplicated_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, 'legacy')
            os.makedirs(legacy)
            open(os.path.join(legacy, 'old-tool.sh'), 'w').close()
            other = os.path.join(d, 'other')
            os.makedirs(other)
            dupe = os.path.join(other, 'old-tool.sh')
            with open(dupe, 'w') as f:
                f.write('#!/bin/sh\n')
            os.chmod(dupe, 0o755)
            repo = os.path.join(d, 'repo')
            os.makedirs(repo)
            cfg = {'legacy_paths': [legacy]}
            product = env.Product('x', {'repo_dir': repo})
            old_path = os.environ.get('PATH', '')
            os.environ['PATH'] = other + os.pathsep + old_path
            try:
                ok, detail = doctor.check_one_factory(cfg, product)
            finally:
                os.environ['PATH'] = old_path
            self.assertFalse(ok)
            self.assertIn('old-tool.sh', detail)

    def test_the_factory_repo_itself_is_not_a_second_copy(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, 'legacy')
            os.makedirs(legacy)
            # a name this checkout itself carries under tools/
            open(os.path.join(legacy, 'check_generic.sh'), 'w').close()
            link = os.path.join(d, 'factory')  # a symlink: real paths are what is compared
            os.symlink(PROJECT_ROOT, link)
            cfg = {'legacy_paths': [legacy]}
            ok, detail = doctor.check_one_factory(cfg, env.Product('asf', {'repo_dir': link}))
            self.assertTrue(ok, detail)
            self.assertIn('repo skipped: it is the factory itself', detail)
            # a copy of the checkout elsewhere is still a second copy
            copy = os.path.join(d, 'copy')
            shutil.copytree(os.path.join(PROJECT_ROOT, 'tools'), os.path.join(copy, 'tools'))
            ok, detail = doctor.check_one_factory(cfg, env.Product('x', {'repo_dir': copy}))
            self.assertFalse(ok)
            self.assertIn('check_generic.sh in product repo', detail)


class RedactionHooksTests(unittest.TestCase):
    """T-0025 (F-0075 §2.4): ``check_redaction_hooks`` reads back the git hooks
    ``asf.hooks.ensure_git_hooks`` writes — read-only, so this row never writes one itself."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='doctor_redaction_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.product = env.Product('sample', {'repo_dir': self.repo})

    def test_row_is_ok_when_installed_and_not_ok_naming_the_command_when_not(self):
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertFalse(ok)
        self.assertIn('pre-commit', detail)
        self.assertIn('missing', detail)
        self.assertIn('asf hooks install --product sample', detail)

        ok, detail = hooks.ensure_git_hooks(self.product, which=lambda name: '/opt/bin/asf')
        self.assertTrue(ok, detail)

        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, 'pre-commit, pre-push in 1 repos')

        # a hook file that is not asf's is named too, and never overwritten by the check
        with open(os.path.join(self.repo, '.git', 'hooks', 'pre-push'), 'w') as f:
            f.write('#!/bin/sh\necho not asf\n')
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertFalse(ok)
        self.assertIn('pre-push', detail)
        self.assertIn('foreign', detail)


class TestNoPrHost(unittest.TestCase):
    """``ci: {provider: none}``: no repo_slug to demand, and gh is optional."""

    def test_repo_slug_not_required_without_a_pr_host(self):
        with tempfile.TemporaryDirectory() as home:
            old = env.ASF_HOME
            env.ASF_HOME = home
            try:
                os.makedirs(os.path.join(home, 'products'))
                with open(os.path.join(home, 'products', 'p.yaml'), 'w') as f:
                    f.write('product: p\nrepo_dir: /r\nbacklog_dir: /b\nci:\n  provider: none\n')
                self.assertTrue(doctor.check_config('p')[0])
                with open(os.path.join(home, 'products', 'p.yaml'), 'w') as f:
                    f.write('product: p\nrepo_dir: /r\nbacklog_dir: /b\n')
                ok, detail = doctor.check_config('p')[:2]
                self.assertFalse(ok)
                self.assertIn('repo_slug', detail)
            finally:
                env.ASF_HOME = old

    def test_gh_is_optional_without_a_pr_host(self):
        hostless = env.Product('p', {'ci': {'provider': 'none'}})
        hosted = env.Product('p', {'ci': {'provider': 'gh-actions'}})
        gh = lambda product: [r for r in doctor.check_cli_sessions(product) if r[0] == 'gh'][0]
        with tempfile.TemporaryDirectory() as bindir:  # the probes answer at once (B-0071)
            old_path = os.environ.get('PATH', '')
            os.environ['PATH'] = fake_clis(bindir) + os.pathsep + old_path
            try:
                self.assertFalse(gh(hostless)[1])
                self.assertTrue(gh(hosted)[1])
            finally:
                os.environ['PATH'] = old_path
        self.assertFalse(doctor.has_pr_host(env.Product('p', {'ci': 'none'})))


class Capacity(unittest.TestCase):
    """`doctor.check_capacity` — spec f-0079 §2.5, the doctor's `capacity` row."""

    def _rows(self, cfg, home):
        old = env.ASF_HOME
        env.ASF_HOME = home
        try:
            return doctor.check_capacity(cfg, env.Product('a', {}))
        finally:
            env.ASF_HOME = old

    @staticmethod
    def _as_doctor_rows(findings):
        return [('capacity', False, ok, detail) for ok, detail in findings]

    def test_oversubscribed_products_are_named(self):
        cfg = {'capacity': {'total': {'sessions': 3}}}
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'products', 'a.yaml'), 'w') as f:
                f.write('product: a\ncapacity:\n  sessions: 2\n')
            with open(os.path.join(home, 'products', 'b.yaml'), 'w') as f:
                f.write('product: b\ncapacity:\n  sessions: 2\n')
            findings = self._rows(cfg, home)
        bad = [d for ok, d in findings if not ok]
        self.assertTrue(any('4' in d and 'a' in d and 'b' in d for d in bad), bad)
        self.assertFalse(doctor.is_red(self._as_doctor_rows(findings)))

    def test_a_total_above_the_account_caps_is_named(self):
        cfg = {'capacity': {'total': {'sessions': 10}},
               'worker_pool': {'accounts': [{'name': 'x', 'cap': 3}]}}
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            findings = self._rows(cfg, home)
        bad = [d for ok, d in findings if not ok]
        self.assertTrue(any('10' in d and '3' in d for d in bad), bad)
        self.assertFalse(doctor.is_red(self._as_doctor_rows(findings)))

    def test_a_deprecated_key_is_named_with_its_new_home(self):
        cfg = {'feeder': {'capacity': 1}, 'worker_pool': {'reserve_for_s1': {'local': 1}}}
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            findings = self._rows(cfg, home)
        bad = [d for ok, d in findings if not ok]
        self.assertTrue(any('feeder.capacity' in d and 'capacity.per_product.sessions' in d
                            for d in bad), bad)
        self.assertTrue(any('worker_pool.reserve_for_s1' in d and 'capacity.reserve_for_s1' in d
                            for d in bad), bad)
        self.assertFalse(doctor.is_red(self._as_doctor_rows(findings)))

    def test_nothing_configured_is_one_ok_row(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            findings = self._rows({}, home)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0][0])


class TestFormatAndExit(unittest.TestCase):
    def test_is_red_true_only_for_required_failures(self):
        rows = [('config', True, True, 'ok'), ('cli:aws', False, False, 'no creds'),
                ('repo', True, False, 'missing')]
        self.assertTrue(doctor.is_red(rows))

    def test_is_red_false_when_only_optional_fails(self):
        rows = [('config', True, True, 'ok'), ('cli:aws', False, False, 'no creds')]
        self.assertFalse(doctor.is_red(rows))

    def test_format_table_marks_skip_for_optional_not_installed(self):
        rows = [('cli:vercel', False, None, 'not installed')]
        out = doctor.format_table('sample', rows)
        self.assertIn('skip', out)
        self.assertNotIn('RED', out)


class TestCmdDoctorSubprocess(unittest.TestCase):
    """End-to-end: `python3 -m asf.cli doctor --product sample` on a fixture ASF_HOME."""

    def _run(self, home, product='sample'):
        env_vars = dict(os.environ)
        env_vars['ASF_HOME'] = home
        env_vars['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env_vars.get('PYTHONPATH', '')
        # the login probes answer at once (B-0071); the rows these tests pin are not theirs
        env_vars['PATH'] = fake_clis(os.path.join(home, 'bin')) + os.pathsep + env_vars.get('PATH', '')
        return subprocess.run([sys.executable, '-m', 'asf.cli', 'doctor', '--product', product],
                              env=env_vars, capture_output=True, text=True, timeout=30)

    def test_exits_1_with_red_row_when_repo_missing(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('schema_version: 1\nscheduler:\n  kind: gh-actions\n')
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write('product: sample\nrepo_dir: /no/such/repo\nrepo_slug: acme/sample\n'
                        'backlog_dir: /no/such/backlog\n')
            result = self._run(home)
            self.assertEqual(result.returncode, 1)
            self.assertIn('== DOCTOR sample', result.stdout)
            self.assertIn('RED', result.stdout)

    def test_config_repo_backlog_rows_ok_when_sound(self):
        # Doesn't assert the overall exit code: `cli:gh`/`cli:git` reflect *this* machine's
        # own session state (required rows per the brief), which a portable test can't pin —
        # only that the rows this fixture controls (config, repo, backlog) come back ok.
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            repo = os.path.join(home, 'repo')
            backlog = os.path.join(home, 'backlog')
            os.makedirs(repo)
            os.makedirs(backlog)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            subprocess.run(['git', 'init', '-q'], cwd=backlog, check=True)
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('schema_version: 1\nscheduler:\n  kind: gh-actions\n')
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(f'product: sample\nrepo_dir: {repo}\nrepo_slug: acme/sample\n'
                        f'backlog_dir: {backlog}\n')
            result = self._run(home)
            lines = {l.split()[0]: l for l in result.stdout.splitlines() if l.split()[:1]}
            for row in ('config', 'repo', 'backlog', 'scheduler'):
                self.assertIn(' ok ', lines[row], lines[row])


class TestSchedulerSection(unittest.TestCase):
    """`asf doctor`'s scheduler section, against the fake launchctl from test_scheduler."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-doctor-sched-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.agents = os.path.join(self.home, 'Library', 'LaunchAgents')
        self.asf_home = os.path.join(self.tmp, 'ASF')
        self.logs = os.path.join(self.asf_home, 'logs')
        for d in (self.agents, self.logs, os.path.join(self.asf_home, 'products')):
            os.makedirs(d)
        self.bindir, self.statedir = fake_launchctl(self.tmp)

        self._old_env = dict(os.environ)
        self._old_asf_home = env.ASF_HOME
        os.environ['HOME'] = self.home
        os.environ['PATH'] = self.bindir + os.pathsep + os.environ.get('PATH', '')
        os.environ['FAKE_LAUNCHCTL_DIR'] = self.statedir
        env.ASF_HOME = self.asf_home
        self.addCleanup(self._restore)

        self.legacy_dir = os.path.join(self.tmp, 'legacy')
        os.makedirs(self.legacy_dir)
        self.product = env.Product('sample', {'repo_dir': self.tmp, 'repo_slug': 'acme/sample',
                                              'backlog_dir': self.tmp})

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        env.ASF_HOME = self._old_asf_home

    def cfg(self, legacy_labels=(), legacy_paths=(), interval_s=None):
        sched = {'kind': 'launchd', 'label_prefix': 'asf', 'legacy_labels': list(legacy_labels)}
        if interval_s is not None:
            sched['interval_s'] = interval_s
        return {'scheduler': sched, 'legacy_paths': list(legacy_paths)}

    def install_plist(self, label, argv, log=None, interval=600, calendar=None, age_s=0):
        import plistlib
        path = os.path.join(self.agents, f'{label}.plist')
        data = {'Label': label, 'ProgramArguments': argv}
        if calendar is not None:
            data['StartCalendarInterval'] = calendar
        else:
            data['StartInterval'] = interval
        if log:
            data['StandardOutPath'] = log
        with open(path, 'wb') as f:
            plistlib.dump(data, f)
        if age_s:
            stamp = time.time() - age_s
            os.utime(path, (stamp, stamp))
        return path

    def write_log(self, name, text, age_s=0):
        path = os.path.join(self.logs, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        if age_s:
            stamp = time.time() - age_s
            os.utime(path, (stamp, stamp))
        return path

    # ---- the rows ---------------------------------------------------------------------------

    def test_a_healthy_job_is_ok_and_shows_its_log_tail(self):
        log = self.write_log('tick-sample-record.log', 'starting\ntick: state committed\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3', '-m', 'asf.cli'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual([r[0] for r in rows], [doctor.OK])
        self.assertIn('runs=7', rows[0][2])
        self.assertIn('last-exit=0', rows[0][2])
        self.assertIn('log: tick: state committed', rows[0][2])
        self.assertFalse(doctor.scheduler_is_red(rows))

    def test_a_job_that_never_exited_past_two_intervals_is_red(self):
        """B-0014 (b): the job was installed, launchd holds it, and it has never once run."""
        self.install_plist('asf.sample.dispatch', ['python3', '-m', 'asf.cli'],
                           interval=600, age_s=3600)
        fake_loaded(self.statedir, ['asf.sample.dispatch'])
        fake_print(self.statedir, 'asf.sample.dispatch',
                   read_fixture('launchctl-print-never-exited.txt'))

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.RED)
        self.assertIn('never exited', rows[0][2])
        self.assertIn('over 2 intervals', rows[0][2])
        self.assertTrue(doctor.scheduler_is_red(rows))

    def test_a_job_that_never_exited_inside_two_intervals_is_not_yet_red(self):
        self.install_plist('asf.sample.dispatch', ['python3'], interval=600, age_s=60)
        fake_loaded(self.statedir, ['asf.sample.dispatch'])
        fake_print(self.statedir, 'asf.sample.dispatch',
                   read_fixture('launchctl-print-never-exited.txt'))
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.OK)

    def test_a_nonzero_last_exit_is_red(self):
        self.install_plist('asf.sample.record', ['/usr/bin/python3'])
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record',
                   read_fixture('launchctl-print.txt').replace('last exit code = 0',
                                                               'last exit code = 1'))
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.RED)
        self.assertIn('last-exit=1', rows[0][2])

    def test_no_loaded_job_at_all_is_red(self):
        fake_loaded(self.statedir, [])
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.RED)
        self.assertIn('nothing ticks', rows[0][2])

    def test_daily_job_never_ran_uses_a_day_not_the_global_interval(self):
        self.install_plist('asf.sample.daily', ['python3'],
                           calendar={'Hour': 6, 'Minute': 50}, age_s=1800)
        fake_loaded(self.statedir, ['asf.sample.daily'])
        fake_print(self.statedir, 'asf.sample.daily',
                   read_fixture('launchctl-print-never-exited.txt'))
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.OK)

    def test_interval_job_never_ran_uses_its_own_interval(self):
        self.install_plist('asf.sample.record', ['python3'], interval=300, age_s=660)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record',
                   read_fixture('launchctl-print-never-exited.txt'))
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.RED)
        self.assertIn('never ran', rows[0][2])

    def test_interval_s_in_config_is_yellow(self):
        fake_loaded(self.statedir, [])
        rows = doctor.scheduler_rows(self.cfg(interval_s=600), self.product)
        yellow = [r for r in rows if r[0] == doctor.YELLOW and r[1] == 'config']
        self.assertEqual(len(yellow), 1, rows)
        self.assertIn('scheduler.interval_s', yellow[0][2])

    def test_a_legacy_dir_a_loaded_job_still_points_at_is_yellow(self):
        script = os.path.join(self.legacy_dir, 'dispatch.sh')
        open(script, 'w').close()
        self.install_plist('old.factory.dispatch', ['/bin/bash', script])
        self.install_plist('asf.sample.record', ['/usr/bin/python3'])
        fake_loaded(self.statedir, ['asf.sample.record', 'old.factory.dispatch'])
        for label in ('asf.sample.record', 'old.factory.dispatch'):
            fake_print(self.statedir, label, read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(
            self.cfg(legacy_labels=['old.factory.*'], legacy_paths=[self.legacy_dir]),
            self.product)
        yellow = [r for r in rows if r[0] == doctor.YELLOW]
        self.assertEqual(len(yellow), 1, rows)
        self.assertEqual(yellow[0][1], self.legacy_dir)
        self.assertEqual(yellow[0][2], f'still in use by old.factory.dispatch ({script})')
        self.assertFalse(doctor.scheduler_is_red(rows), 'in-use is a warning, not a failure')

    def test_a_non_launchd_kind_says_so_in_one_line(self):
        rows = doctor.scheduler_rows({'scheduler': {'kind': 'gh-actions'}}, self.product)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], doctor.OK)
        self.assertIn('gh-actions', rows[0][2])

    def test_format_puts_every_job_on_its_own_line(self):
        rows = [(doctor.OK, 'asf.sample.record', 'state=running'),
                (doctor.RED, 'asf.sample.dispatch', 'last-exit=1'),
                (doctor.YELLOW, '/opt/legacy', 'still in use by old.dispatch')]
        out = doctor.format_scheduler('sample', rows)
        self.assertIn('== SCHEDULER sample', out)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn('RED', out)
        self.assertIn('YELLOW', out)

    def test_format_age(self):
        self.assertEqual(doctor.format_age(None), '-')
        self.assertEqual(doctor.format_age(30), '30s')
        self.assertEqual(doctor.format_age(600), '10m')
        self.assertEqual(doctor.format_age(7200), '2h')
        self.assertEqual(doctor.format_age(200000), '2d')


class TestDoctorStamp(unittest.TestCase):
    """The doctor's footer names asf's own version, never the product repo's HEAD."""

    def test_the_footer_is_asfs_version_not_the_products_head(self):
        import argparse
        import contextlib
        import io
        product = env.Product('x', {'repo_dir': '/nonexistent-product-repo'})
        out = io.StringIO()
        with mock.patch.object(doctor, 'run', return_value=[('config', True, True, 'ok')]), \
                mock.patch.object(doctor, 'scheduler_rows', return_value=[]), \
                mock.patch.object(doctor, 'check_clock_steps', return_value=[]), \
                mock.patch.object(env, 'load_config', return_value={}), \
                mock.patch.object(env, 'load_product', return_value=product), \
                mock.patch('asf.cli.version_string', return_value='9.9.9 (feedbee)'), \
                mock.patch('asf.cli._git_sha', return_value='productsha'), \
                contextlib.redirect_stdout(out):
            doctor.cmd_doctor(argparse.Namespace(product='x'), '.')
        footer = out.getvalue().strip().splitlines()[-1]
        self.assertIn('generated by asf doctor@9.9.9 (feedbee) ', footer)
        self.assertNotIn('productsha', footer)


if __name__ == '__main__':
    unittest.main()
