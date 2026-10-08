import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asf import console_perms, doctor, env, hooks
from tests.gitfixture import executable_asf

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
        self.assertFalse(ok)


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

    def _factory_source(self, d, name):
        legacy = os.path.join(d, 'legacy')
        os.makedirs(legacy)
        open(os.path.join(legacy, 'frontmatter.py'), 'w').close()
        repo = os.path.join(d, 'repo')
        os.makedirs(os.path.join(repo, 'asf'))
        open(os.path.join(repo, 'asf', 'frontmatter.py'), 'w').close()
        with open(os.path.join(repo, 'pyproject.toml'), 'w') as f:
            f.write(f'[project]\nname = "{name}"\n')
        return {'legacy_paths': [legacy]}, env.Product('asf', {'repo_dir': repo})

    def test_installed_factorys_source_repo_is_skipped(self):
        # asf runs from its install, not the checkout: the product whose repo is the source of
        # the installed distribution is the factory itself, not a second copy of it
        with tempfile.TemporaryDirectory() as d:
            cfg, product = self._factory_source(d, 'asf-factory')
            with mock.patch.object(doctor, 'package_root', return_value=os.path.join(d, 'site')), \
                    mock.patch.object(doctor, 'installed_dist_names', return_value={'asf-factory'}):
                ok, detail = doctor.check_one_factory(cfg, product)
            self.assertTrue(ok, detail)
            self.assertIn('it is the factory itself', detail)

    def test_other_projects_copy_is_still_red(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, product = self._factory_source(d, 'someone-elses-app')
            with mock.patch.object(doctor, 'package_root', return_value=os.path.join(d, 'site')), \
                    mock.patch.object(doctor, 'installed_dist_names', return_value={'asf-factory'}):
                ok, detail = doctor.check_one_factory(cfg, product)
            self.assertFalse(ok)
            self.assertIn('frontmatter.py', detail)

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


class TestCheckDeadSessions(unittest.TestCase):
    """F-0234 §4: `doctor.check_dead_sessions` wraps `health.dead_census_line`, exactly as
    `check_branches` wraps `retention.doctor_line` — a count of things that already happened is
    not a broken installation (C8), so the row is non-required (``False``)."""

    def test_reads_the_health_census_line(self):
        product = env.Product('x', {})
        data = {'days': 14, 'runs': 0, 'by_class': {}, 'unclassified': 0}
        with mock.patch('asf.workers.health.dead_census', return_value=data):
            ok, detail = doctor.check_dead_sessions(product)
        self.assertTrue(ok)
        self.assertEqual(detail, 'dead sessions: 0 in 14 days')

    def test_an_unknown_death_standing_is_not_ok(self):
        product = env.Product('x', {})
        data = {'days': 14, 'runs': 1, 'by_class': {'unknown': 1}, 'unclassified': 0}
        with mock.patch('asf.workers.health.dead_census', return_value=data):
            ok, detail = doctor.check_dead_sessions(product)
        self.assertFalse(ok)
        self.assertIn('unknown 1', detail)

    def test_an_unreadable_ledger_is_none_not_a_broken_installation(self):
        product = env.Product('x', {})
        with mock.patch('asf.workers.health.dead_census', side_effect=OSError('boom')):
            self.assertIsNone(doctor.check_dead_sessions(product))


class DeadCensusNamesTheRefusals(unittest.TestCase):
    """F-0266 S-64359 (C15): the dead-sessions row gains ``; refusals: …`` when any death's
    last ASF refusal is known, nothing when none is, and its ``ok`` rule is unchanged."""

    def test_the_clause(self):
        product = env.Product('x', {})
        data = {'days': 14, 'runs': 11, 'by_class': {'gone': 6, 'unknown': 4}, 'unclassified': 1,
                'by_refusal': {'naming': 3, 'hook refused': 1}}
        with mock.patch('asf.workers.health.dead_census', return_value=data):
            ok, detail = doctor.check_dead_sessions(product)
        self.assertFalse(ok)  # unknown still stands: the refusals do not excuse it
        self.assertEqual(detail, 'dead sessions: 11 in 14 days — gone 6, unknown 4, '
                                 'unclassified 1; refusals: naming 3, hook refused 1')
        data = dict(data, by_class={'gone': 6}, by_refusal={})
        with mock.patch('asf.workers.health.dead_census', return_value=data):
            ok, detail = doctor.check_dead_sessions(product)
        self.assertTrue(ok)
        self.assertNotIn('refusals', detail)


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

        asf = executable_asf(os.path.join(self.tmp, 'bin'))
        ok, detail = hooks.ensure_git_hooks(self.product, which=lambda name: asf)
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


class ForeignGitHookStillGuardsTests(unittest.TestCase):
    """A product repo with its own git hook: ``asf hooks install`` writes every hook it can —
    the worker approvals hook above all — and only then refuses with NEEDS OPERATOR, non-zero.
    The doctor's ``approvals-hook`` row is red while a worker account has no approvals hook."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='doctor_approvals_hook_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.product = env.Product('sample', {'repo_dir': self.repo})
        self.acct = os.path.join(self.tmp, 'acct')
        self.cfg = {'worker_pool': {'accounts': [{'name': 'w1', 'config_dir': self.acct}]}}
        self.asf = executable_asf(os.path.join(self.tmp, 'bin'))
        self.which = lambda name: self.asf

    def _foreign_pre_push(self):
        path = os.path.join(self.repo, '.git', 'hooks', 'pre-push')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\necho the product own hook\n')
        return path

    def test_foreign_pre_push_still_writes_the_approvals_hook_then_needs_operator(self):
        foreign = self._foreign_pre_push()
        rc, msg = hooks.install(self.product, rules_dir=os.path.join(self.tmp, 'none'),
                                which=self.which, cfg=self.cfg)
        self.assertEqual(rc, 2, msg)
        self.assertIn(f'NEEDS OPERATOR: {foreign} is not asf', msg)
        self.assertIn('approvals in 1 worker accounts', msg)
        import json
        with open(os.path.join(self.acct, 'settings.json')) as f:
            pre = json.load(f)['hooks']['PreToolUse']
        self.assertEqual(pre[0]['hooks'][0]['command'], f'{self.asf} hook approvals')
        # the missing git hook beside the foreign one is still written; the foreign one untouched
        self.assertTrue(os.path.isfile(os.path.join(self.repo, '.git', 'hooks', 'pre-commit')))
        with open(foreign) as f:
            self.assertIn('the product own hook', f.read())
        ok, detail = doctor.check_approvals_hook(self.cfg, self.product)
        self.assertTrue(ok, detail)

    def test_doctor_is_red_when_the_approvals_hook_is_missing(self):
        ok, detail = doctor.check_approvals_hook(self.cfg, self.product)
        self.assertFalse(ok)
        self.assertIn('w1', detail)
        self.assertIn('asf hooks install --product sample', detail)
        with mock.patch.object(doctor, 'check_config',
                               return_value=(True, '', self.cfg, self.product)), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_drift', return_value=(True, '')):
            rows = doctor.run('sample')
        row = [r for r in rows if r[0] == 'approvals-hook'][0]
        self.assertTrue(row[1])
        self.assertFalse(row[2])
        self.assertTrue(doctor.is_red(rows))

    def test_the_hook_in_the_product_repo_settings_counts(self):
        import json
        path = os.path.join(self.repo, '.claude', 'settings.json')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            json.dump(hooks.merge({}, [('PreToolUse', 'approvals')], '/opt/bin/asf', None), f)
        ok, detail = doctor.check_approvals_hook(self.cfg, self.product)
        self.assertTrue(ok, detail)

    def test_no_accounts_or_fake_backend_is_not_red(self):
        self.assertTrue(doctor.check_approvals_hook({}, self.product)[0])
        cfg = {'worker_pool': dict(self.cfg['worker_pool'], backend='fake')}
        self.assertTrue(doctor.check_approvals_hook(cfg, self.product)[0])


class ConsolePermissionsDoctorTests(unittest.TestCase):
    """B-0131: the operator console cannot run `asf` or the installer without a prompt while
    neither its user-level settings nor the product repo's carries the console's own allow list.
    The ``console permissions`` doctor row (:func:`asf.console_perms.check_doctor`) is red and
    names, by name, each rule missing from both."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='doctor_console_perms_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main'})

    def write_settings(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            import json
            json.dump(data, f)

    def test_no_settings_file_is_not_configured_and_names_the_command(self):
        """F-0109: a clean install has no console allow list yet — the offer is shown, the
        operator writes it. That is ``not configured`` (None), never red; a partial list still is."""
        ok, detail = console_perms.check_doctor(self.product, home=self.home)
        self.assertIsNone(ok)
        self.assertTrue(detail.startswith('not configured'), detail)
        self.assertIn('asf console-permissions install --product sample', detail)

    def test_a_partial_list_names_only_what_is_missing(self):
        path = os.path.join(self.home, '.claude', 'settings.json')
        self.write_settings(path, {'permissions': {'allow': list(console_perms.FIXED_ALLOW)}})
        ok, detail = console_perms.check_doctor(self.product, home=self.home)
        self.assertFalse(ok)
        for rule in console_perms.FIXED_ALLOW:
            self.assertNotIn(rule, detail)
        self.assertIn('Bash(git push --force* origin main)', detail)

    def test_the_rules_split_across_user_and_repo_settings_both_count(self):
        user_path = os.path.join(self.home, '.claude', 'settings.json')
        repo_path = os.path.join(self.repo, '.claude', 'settings.json')
        self.write_settings(user_path, {'permissions': {'allow': list(console_perms.FIXED_ALLOW)}})
        self.write_settings(repo_path, console_perms.merge({}, self.product))
        # the repo file alone already carries every rule the user file is missing
        ok, detail = console_perms.check_doctor(self.product, home=self.home)
        self.assertTrue(ok, detail)

    def test_every_rule_present_is_green(self):
        path = os.path.join(self.home, '.claude', 'settings.json')
        self.write_settings(path, console_perms.merge({}, self.product))
        ok, detail = console_perms.check_doctor(self.product, home=self.home)
        self.assertTrue(ok, detail)
        self.assertIn('settings file(s) checked, every rule present', detail)

    def test_doctor_run_carries_the_row_and_turns_red(self):
        cfg = {}
        with mock.patch.object(doctor, 'check_config',
                               return_value=(True, '', cfg, self.product)), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_drift', return_value=(True, '')), \
                mock.patch.object(console_perms, 'settings_paths',
                                  return_value=[os.path.join(self.home, '.claude', 'settings.json')]):
            rows = doctor.run('sample')
        row = [r for r in rows if r[0] == 'console permissions'][0]
        self.assertTrue(row[1])
        self.assertFalse(row[2])
        self.assertTrue(doctor.is_red(rows))


class LegacySchedulerJobTests(unittest.TestCase):
    """The scheduler row is red while a pre-ASF job the config names is still live: a fake
    ``launchctl`` / ``crontab`` on PATH stands in for the machine's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='doctor_legacy_sched_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindir = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bindir)
        patcher = mock.patch.dict(os.environ, {'PATH': self.bindir + os.pathsep + os.environ['PATH']})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stub(self, name, body):
        path = os.path.join(self.bindir, name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(path, 0o755)

    def test_a_loaded_legacy_launchd_label_is_red_with_bootout(self):
        self._stub('launchctl', '[ "$1" = list ] && [ "$2" = old.job ] && exit 0\nexit 113\n')
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'launchd', 'launchd_label': 'old.job'}})
        self.assertFalse(ok)
        self.assertIn('launchctl bootout gui/$(id -u)/old.job', detail)
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'launchd', 'launchd_label': 'gone.job'}})
        self.assertTrue(ok, detail)

    def test_a_present_legacy_cron_entry_is_red_with_crontab_removal(self):
        self._stub('crontab', 'printf "# old-tick commented\\n*/5 * * * * /opt/old/old-tick run\\n"\n')
        cfg = {'scheduler': {'kind': 'cron', 'legacy_cron': '/opt/old/old-tick'}}
        ok, detail = doctor.check_scheduler(cfg)
        self.assertFalse(ok)
        self.assertIn("crontab -l | grep -vF '/opt/old/old-tick' | crontab -", detail)
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'cron', 'legacy_cron': ['old-tick']}})
        self.assertFalse(ok)  # the commented line alone would not count; the live one does
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'cron', 'legacy_cron': ['nothere']}})
        self.assertTrue(ok, detail)
        self.assertIn('retired', detail)

    def test_a_red_scheduler_row_makes_doctor_red(self):
        self._stub('launchctl', 'exit 0\n')
        cfg = {'scheduler': {'kind': 'launchd', 'launchd_label': 'old.job'}}
        product = env.Product('sample', {})
        with mock.patch.object(doctor, 'check_config', return_value=(True, '', cfg, product)), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_drift', return_value=(True, '')):
            rows = doctor.run('sample')
        row = [r for r in rows if r[0] == 'scheduler'][0]
        self.assertFalse(row[2])
        self.assertTrue(doctor.is_red(rows))


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


class GhAuth(unittest.TestCase):
    """`doctor._gh_auth` — the `cli:gh` row through asf.github: an Unknown is never ok."""

    def test_an_answer_is_ok_with_its_first_line(self):
        done = subprocess.CompletedProcess(['gh'], 0, '', 'github.com\n  Logged in\n')
        with mock.patch('subprocess.run', return_value=done):
            self.assertEqual(doctor._gh_auth(), (True, 'github.com'))

    def test_unknown_reads_are_never_ok(self):
        for effect in (OSError('no gh'), subprocess.TimeoutExpired('gh', 10)):
            with mock.patch('subprocess.run', side_effect=effect):
                ok, detail = doctor._gh_auth()
            self.assertFalse(ok)
            self.assertTrue(detail)
        failed = subprocess.CompletedProcess(['gh'], 1, '', 'You are not logged in\n')
        with mock.patch('subprocess.run', return_value=failed):
            self.assertEqual(doctor._gh_auth(), (False, 'You are not logged in'))


class CredentialsRowTests(unittest.TestCase):
    """`doctor.check_credentials` — spec F-0042 §2.5, the doctor's `credentials` row: a view
    over the cache, never a probe."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='doctor_credentials_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        env.ASF_HOME = self._orig_home

    def cfg(self):
        return {'credentials': {'providers': {
            'provider-a': {'probe': 'check-a', 'renew': 'renew-a'}}}}

    def product(self, names=('provider-a',)):
        return env.Product('sample', {'credentials': list(names)})

    def test_red_when_the_cache_holds_an_invalid_provider(self):
        from asf import credentials
        product = self.product()
        credentials.write_cache(product, [credentials.Result(provider='provider-a',
                                                               state='invalid')])
        with mock.patch('asf.credentials.subprocess.run') as run:
            ok, detail = doctor.check_credentials(self.cfg(), product)
        run.assert_not_called()
        self.assertFalse(ok)
        self.assertIn('renew-a', detail)

    def test_red_when_the_cache_holds_an_expiring_provider(self):
        from asf import credentials
        product = self.product()
        now = datetime.datetime.now(datetime.timezone.utc)
        expiry = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        credentials.write_cache(product, [credentials.Result(provider='provider-a',
                                                               state='valid', expires=expiry)])
        with mock.patch('asf.credentials.subprocess.run') as run:
            ok, detail = doctor.check_credentials(self.cfg(), product)
        run.assert_not_called()
        self.assertFalse(ok)
        self.assertIn('renew-a', detail)

    def test_red_on_a_config_problem_names_the_dotted_key(self):
        cfg = {'credentials': {'providers': {'provider-a': {'probe': 'check-a'}}}}  # no renew
        ok, detail = doctor.check_credentials(cfg, self.product())
        self.assertFalse(ok)
        self.assertIn('credentials.providers.provider-a.renew', detail)

    def test_no_providers_configured_is_ok(self):
        self.assertEqual(doctor.check_credentials(self.cfg(), self.product(names=())),
                         (True, 'no providers configured'))

    def test_the_row_runs_no_probe(self):
        with mock.patch('asf.credentials.subprocess.run') as run:
            ok, detail = doctor.check_credentials(self.cfg(), self.product())
        run.assert_not_called()
        self.assertTrue(ok, detail)
        self.assertIn('not probed yet', detail)


class NotConfiguredRowsTests(unittest.TestCase):
    """F-0109 / the clean install: a row that waits on a login no install can make reads
    ``not configured`` (skip), never RED — and a login that is there but broken stays red."""

    def test_gh_not_logged_in_is_not_configured(self):
        with mock.patch.object(doctor, '_gh_auth',
                               return_value=(False, 'You are not logged into any GitHub hosts. '
                                                    'To log in, run: gh auth login')):
            rows = {r[0]: r for r in doctor.check_cli_sessions(None)}
        _name, _required, ok, detail = rows['gh']
        self.assertIsNone(ok)
        self.assertTrue(detail.startswith(doctor.NOT_CONFIGURED), detail)
        self.assertFalse(doctor.is_red([('cli:gh', True, ok, detail)]))

    def test_gh_broken_otherwise_stays_red(self):
        with mock.patch.object(doctor, '_gh_auth', return_value=(False, 'rate limited')):
            rows = {r[0]: r for r in doctor.check_cli_sessions(None)}
        self.assertIs(rows['gh'][2], False)

    def test_a_pool_with_no_worker_login_is_not_configured(self):
        cfg = {'worker_pool': {'backend': 'claude-code', 'accounts': [{'name': 'acct-a'}]}}
        ok, detail = doctor.check_worker_env(cfg)
        self.assertIsNone(ok)
        self.assertTrue(detail.startswith(doctor.NOT_CONFIGURED), detail)
        self.assertIn('acct-a', detail)

    def test_one_account_with_a_login_and_one_without_stays_red(self):
        cfg = {'worker_pool': {'backend': 'claude-code', 'accounts': [
            {'name': 'acct-a'},
            {'name': 'acct-b', 'auth_env': {'CLAUDE_CODE_OAUTH_TOKEN': '/x/b.token'}}]}}
        ok, detail = doctor.check_worker_env(cfg)
        self.assertIs(ok, False)
        self.assertIn('acct-a', detail)
        self.assertNotIn('acct-b is isolated', detail)


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


class TestCheckSavings(unittest.TestCase):
    """`doctor.check_savings` — spec f-0100 §2.9, the doctor's `savings` row."""

    def test_a_clean_product_is_one_green_row(self):
        product = env.Product('a', {'conventions': {'savings': {'gate_minutes_max': 30.0}}})
        findings = doctor.check_savings(product)
        self.assertEqual(findings, [(True, 'savings: 8 thresholds, 1 overridden')])

    def test_no_savings_block_is_green_with_none_overridden(self):
        product = env.Product('a', {})
        findings = doctor.check_savings(product)
        self.assertEqual(findings, [(True, 'savings: 8 thresholds, 0 overridden')])

    def test_an_unknown_key_is_named(self):
        product = env.Product('a', {'conventions': {'savings': {'windw_days': 7}}})
        findings = doctor.check_savings(product)
        bad = [d for ok, d in findings if not ok]
        self.assertTrue(any('windw_days' in d for d in bad), bad)

    def test_a_zero_or_negative_value_is_named(self):
        product = env.Product('a', {'conventions': {'savings': {'min_landings': 0,
                                                                  'spend_ratio': -1.5}}})
        findings = doctor.check_savings(product)
        bad = [d for ok, d in findings if not ok]
        self.assertTrue(any('min_landings' in d for d in bad), bad)
        self.assertTrue(any('spend_ratio' in d for d in bad), bad)

    def test_a_non_dict_savings_block_is_named_not_crashed_on(self):
        product = env.Product('a', {'conventions': {'savings': 'yes'}})
        findings = doctor.check_savings(product)
        self.assertEqual(findings, [(False, "conventions.savings must be a map, not 'yes'")])

    def test_the_row_appears_in_run_after_capacity(self):
        product = env.Product('a', {})
        with mock.patch.object(doctor, 'check_config', return_value=(True, '', {}, product)), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_drift', return_value=(True, '')):
            rows = doctor.run('a')
        names = [r[0] for r in rows]
        self.assertIn('savings', names)
        self.assertEqual(names.index('savings'), names.index('capacity') + 1)
        savings_rows = [r for r in rows if r[0] == 'savings']
        self.assertFalse(any(r[1] for r in savings_rows))  # never required


class TestCheckQueuePause(unittest.TestCase):
    """`doctor.check_queue_pause` — F-0036: the `queue pause` row while
    `merge_queue.inflight: 0` pauses new cuts, never red (a deliberate pause is not a defect)."""

    def test_no_row_by_default(self):
        product = env.Product('a', {'conventions': {'merge': 'queue'}})
        self.assertEqual(doctor.check_queue_pause(product), [])

    def test_no_row_with_a_plain_positive_inflight(self):
        product = env.Product('a', {'conventions': {
            'merge': 'queue', 'merge_queue': {'inflight': 2}}})
        self.assertEqual(doctor.check_queue_pause(product), [])

    def test_a_warn_row_while_inflight_is_0(self):
        product = env.Product('a', {'conventions': {
            'merge': 'queue', 'merge_queue': {'inflight': 0}}})
        rows = doctor.check_queue_pause(product)
        self.assertEqual(len(rows), 1, rows)
        required, ok, detail = rows[0]
        self.assertFalse(required)
        self.assertEqual(ok, 'warn')
        self.assertIn('merge_queue.inflight: 0', detail)
        self.assertFalse(doctor.is_red([('queue pause', required, ok, detail)]))

    def test_the_row_appears_in_run(self):
        product = env.Product('a', {'conventions': {
            'merge': 'queue', 'merge_queue': {'inflight': 0}}})
        with mock.patch.object(doctor, 'check_config', return_value=(True, '', {}, product)), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_drift', return_value=(True, '')):
            rows = doctor.run('a')
        (row,) = [r for r in rows if r[0] == 'queue pause']
        self.assertEqual(row[1:3], (False, 'warn'))


class CheapTierAdviceTests(unittest.TestCase):
    """`doctor.check_models` — the `models` row naming an unmapped `cheap` (plan F-0093 Task 2)."""

    @staticmethod
    def _rows(cfg):
        findings = doctor.check_models(cfg)
        return findings, [('models', False, ok, d) for ok, d in findings]

    def test_an_unmapped_cheap_is_named_and_never_red(self):
        findings, rows = self._rows({'worker_pool': {'models': {'heavy': 'h', 'light': 'l'}}})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], 'warn')  # yellow: cheap falls back to light
        self.assertIn('models  warn', doctor.format_table('p', rows))
        for word in ('cheap', 'rebase', 'close', 'light'):
            self.assertIn(word, findings[0][1])
        self.assertFalse(doctor.is_red(rows))

    def test_a_mapped_cheap_has_no_row(self):
        findings, _ = self._rows({'worker_pool': {'models': {'heavy': 'h', 'cheap': 'c'}}})
        self.assertEqual(findings, [])

    def test_no_worker_pool_block_still_prints_the_advice(self):
        findings, rows = self._rows({})
        self.assertEqual(len(findings), 1)
        self.assertIn('cheap', findings[0][1])
        self.assertFalse(doctor.is_red(rows))


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


class SchedulerSectionFixture(unittest.TestCase):
    """The fixture `TestSchedulerSection` and `SchedulerStepFieldTests` both build on: a fake
    launchd, a fake ASF_HOME, and the one product both read jobs and logs for."""

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


class TestSchedulerSection(SchedulerSectionFixture):
    """`asf doctor`'s scheduler section, against the fake launchctl from test_scheduler."""

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

    def test_another_products_clock_is_not_this_products_row(self):
        self.install_plist('asf.other.tick', ['python3', '-m', 'asf.cli'],
                           interval=600, age_s=3600)
        fake_loaded(self.statedir, ['asf.other.tick'])
        fake_print(self.statedir, 'asf.other.tick',
                   read_fixture('launchctl-print-never-exited.txt'))
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertNotIn('asf.other.tick', [r[1] for r in rows])

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

    def test_a_declared_clock_with_no_loaded_job_is_red_naming_the_label(self):
        """B-0136: an install once left ``record-health-wave-prs-harvest`` with a plist on disk
        but never bootstrapped — loaded_jobs() never mentions it, so the old code had no row for
        it at all. A clock the product declares that is not among the loaded jobs must name
        itself RED, not vanish."""
        product = env.Product('sample', {'repo_dir': self.tmp, 'repo_slug': 'acme/sample',
                                         'backlog_dir': self.tmp,
                                         'clocks': {'daily': {'shadow': True, 'at': '06:50'},
                                                    'record-health-wave-prs-harvest':
                                                        {'shadow': True, 'every': '10m'}}})
        self.install_plist('asf.sample.daily', ['python3'], calendar={'Hour': 6, 'Minute': 50})
        fake_loaded(self.statedir, ['asf.sample.daily'])
        fake_print(self.statedir, 'asf.sample.daily', read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(self.cfg(), product)
        self.assertIn((doctor.RED, 'asf.sample.record-health-wave-prs-harvest',
                       'declared in products/sample.yaml but not loaded'), rows)
        self.assertTrue(doctor.scheduler_is_red(rows))

    def test_a_paused_clock_is_a_paused_row_not_red(self):
        from asf import scheduler
        product = env.Product('sample', {'repo_dir': self.tmp, 'repo_slug': 'acme/sample',
                                         'backlog_dir': self.tmp,
                                         'clocks': {'tick': {'shadow': True, 'every': '10m'}}})
        self.install_plist('asf.sample.tick', ['python3'])
        scheduler.pause('sample', ['tick'], 'operator reset', 'op')
        fake_loaded(self.statedir, [])
        rows = doctor.scheduler_rows(self.cfg(), product)
        self.assertEqual([(r[0], r[1]) for r in rows], [(doctor.YELLOW, 'PAUSED')])
        self.assertIn('asf.sample.tick paused since', rows[0][2])
        self.assertIn('operator reset', rows[0][2])
        self.assertFalse(doctor.scheduler_is_red(rows))

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

    def test_a_pending_upgrade_names_what_the_ticks_wait_on(self):
        """B-0141: while a pending marker parks the ticks, the section said 'ok' about clocks
        that were firing into a tick that skipped every time. It names the wait instead."""
        from asf import upgrade
        log = self.write_log('tick-sample-record.log', 'tick: waiting — upgrade to f5aa236\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3', '-m', 'asf.cli'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))
        at = time.time() - 300
        upgrade.write_pending('f5aa236' + 'a' * 33, 'other', self.product.name, now=at)

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        held = [r for r in rows if r[1] == 'upgrade']
        self.assertEqual(len(held), 1, rows)
        self.assertNotEqual(held[0][0], doctor.OK)
        self.assertEqual(held[0][2], f'waiting on upgrade to f5aa236 since '
                                     f'{time.strftime("%H:%M", time.localtime(at))} '
                                     f'(owner other)')
        self.assertFalse(doctor.scheduler_is_red(rows), 'a wait is a warning, not a failure')

    def test_the_markers_own_owner_is_never_told_it_is_waiting(self):
        """B-0141 review round 1 C1: the owner is the one product whose ticks still run — it
        drains and installs the upgrade — so its own section must not claim a wait."""
        from asf import upgrade
        fake_loaded(self.statedir, [])
        upgrade.write_pending('f5aa236' + 'a' * 33, self.product.name, self.product.name,
                              now=time.time() - 300)
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual([r for r in rows if r[1] == 'upgrade'], [], rows)

    def test_a_marker_no_tick_honours_is_no_wait(self):
        """B-0141 review round 1 C2: an operator wait whose process is gone, and a marker dated
        in the future, are both ignored by every tick — the section must not report them."""
        from asf import upgrade
        fake_loaded(self.statedir, [])
        upgrade.write_pending('f5aa236' + 'a' * 33, None, self.product.name, now=time.time() - 300)
        data = upgrade.read_pending(self.product.name)
        data['pid'] = 999999  # an `asf upgrade --wait` killed before it cleared its own mark
        upgrade._write_json(upgrade.pending_path(self.product.name), data)
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual([r for r in rows if r[1] == 'upgrade'], [], rows)

        upgrade.clear_pending(self.product.name)
        upgrade.write_pending('f5aa236' + 'a' * 33, 'other', self.product.name, now=time.time() + 3600)
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual([r for r in rows if r[1] == 'upgrade'], [], rows)

    def test_no_pending_upgrade_adds_no_row(self):
        fake_loaded(self.statedir, [])
        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual([r for r in rows if r[1] == 'upgrade'], [])

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


class CurrentStepTests(unittest.TestCase):
    """`doctor.current_step` (F-0142): the step a tick's log says it is inside right now, read off
    its own start line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-doctor-step-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text):
        path = os.path.join(self.tmp, 'tick.log')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_a_dangling_start_line_gives_the_step_owner_pid_and_age(self):
        path = self.write('[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n')
        now = datetime.datetime(2026, 1, 1, 0, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        running = doctor.current_step(path, alive=lambda pid: True, now=now)
        self.assertEqual(running, {'step': 'harvest', 'owner': 'asf', 'pid': 48213,
                                    'alive': True, 'seconds': 600.0})

    def test_a_start_line_closed_by_its_own_end_line_gives_none(self):
        path = self.write('[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n'
                           '[step:harvest] 22.7s ok=yes owner=asf pid=48213 '
                           'at=2026-01-01T00:00:22Z\n')
        self.assertIsNone(doctor.current_step(path))

    def test_a_start_line_closed_by_a_different_steps_end_line_gives_none(self):
        """The loop is strictly sequential (D14): whichever step line is last answers, even if
        it does not pair with the start line above it."""
        path = self.write('[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n'
                           '[step:wave] 1.0s ok=yes owner=asf pid=48213 at=2026-01-01T00:00:01Z\n')
        self.assertIsNone(doctor.current_step(path))

    def test_a_log_with_no_step_line_gives_none(self):
        path = self.write('starting\ntick: state committed\n')
        self.assertIsNone(doctor.current_step(path))

    def test_an_empty_log_gives_none(self):
        self.assertIsNone(doctor.current_step(self.write('')))

    def test_a_missing_path_gives_none(self):
        self.assertIsNone(doctor.current_step(os.path.join(self.tmp, 'no-such-file.log')))

    def test_a_none_path_gives_none(self):
        self.assertIsNone(doctor.current_step(None))

    def test_a_pre_f_0142_log_ending_in_the_bare_end_line_gives_none(self):
        path = self.write('[step:x] 3.1s\n')
        self.assertIsNone(doctor.current_step(path))

    def test_a_failed_line_matches_neither_pattern_and_the_start_line_still_answers(self):
        path = self.write('[step:wave] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n'
                           '[step:wave] FAILED the wave broke\n'
                           'Traceback (most recent call last):\n'
                           '    ...\n')
        running = doctor.current_step(path)
        self.assertEqual(running['step'], 'wave')

    def test_a_dead_pid_comes_back_not_alive_via_the_alive_argument(self):
        path = self.write('[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n')
        running = doctor.current_step(path, alive=lambda pid: False)
        self.assertFalse(running['alive'])

    def test_a_dead_pid_comes_back_not_alive_via_the_default_lifecycle_check(self):
        path = self.write('[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n')
        with mock.patch.object(doctor.lifecycle, 'pid_alive', return_value=False):
            running = doctor.current_step(path)
        self.assertFalse(running['alive'])

    def test_an_unparseable_at_gives_seconds_none_and_the_rest_intact(self):
        path = self.write('[step:harvest] start owner=asf pid=48213 at=not-a-timestamp\n')
        running = doctor.current_step(path, alive=lambda pid: True)
        self.assertEqual(running, {'step': 'harvest', 'owner': 'asf', 'pid': 48213,
                                    'alive': True, 'seconds': None})

    def test_a_line_written_by_tick_step_start_line_itself_parses_back(self):
        """The one assertion that stops `_STEP_START_RE` and the formatter drifting apart
        (F-0142 plan, Task 2 step 11)."""
        from asf.tick import tick
        path = self.write(tick.step_start_line('harvest', 'asf') + '\n')
        running = doctor.current_step(path)
        self.assertEqual(running['step'], 'harvest')
        self.assertEqual(running['owner'], 'asf')
        self.assertEqual(running['pid'], os.getpid())


class LogTailWindowTests(unittest.TestCase):
    """`_log_lines` and `_log_tail` over it (F-0142, D7/D15/PD5): a bounded read of the log's own
    end, never the whole file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-doctor-tail-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text):
        path = os.path.join(self.tmp, 'tick.log')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_log_lines_on_a_small_log_is_its_non_empty_rstripped_lines_oldest_first(self):
        path = self.write('one\n\ntwo\nthree\n')
        self.assertEqual(doctor._log_lines(path), ['one', 'two', 'three'])

    def test_log_tail_on_a_small_log_is_unchanged_from_today(self):
        path = self.write('one\ntwo\nthree\n')
        self.assertEqual(doctor._log_tail(path), 'three')

    def test_log_tail_on_a_log_larger_than_the_window_still_yields_the_true_last_line(self):
        path = self.write(('x' * 2000 + '\n') * 10 + 'last\n')
        self.assertEqual(doctor._log_lines(path, limit=1000)[-1], 'last')
        self.assertEqual(doctor._log_tail(path), 'last')

    def test_log_tail_of_none_is_none(self):
        self.assertIsNone(doctor._log_tail(None))

    def test_log_tail_of_a_missing_path_is_none(self):
        self.assertIsNone(doctor._log_tail(os.path.join(self.tmp, 'gone.log')))

    def test_log_tail_of_an_empty_file_is_empty_string(self):
        self.assertEqual(doctor._log_tail(self.write('')), '')

    def test_a_start_line_pushed_out_of_the_window_gives_no_current_step(self):
        """D7's accepted cost, pinned: a step that prints more than the window's worth of its own
        output after its start line shows no `step:` field, rather than a wrong one."""
        path = self.write('[step:batch] start owner=command pid=48213 at=2026-01-01T00:00:00Z\n')
        with open(path, 'a', encoding='utf-8') as f:
            while os.path.getsize(path) <= doctor.LOG_TAIL_BYTES:
                f.write('[command:batch] filler line of output\n')
        self.assertIsNone(doctor.current_step(path))

    def test_a_window_boundary_falling_mid_line_drops_the_fragment(self):
        lines = [f'line-{i:03d}' for i in range(50)]
        path = self.write('\n'.join(lines) + '\n')
        size = os.path.getsize(path)
        # a limit that lands inside a line, not on a boundary
        limit = size - len(lines[0]) - 3
        result = doctor._log_lines(path, limit=limit)
        self.assertEqual(result[-1], lines[-1])
        self.assertNotIn(lines[0], result)

    def test_a_file_larger_than_the_window_with_no_newline_in_it_yields_the_partial_line(self):
        """PD5/D15's one named behaviour change: `_log_tail` reading the whole file would have
        returned this file's one true last line too, but a bounded read whose window holds no
        newline at all yields that partial fragment instead — the cost D7 accepts."""
        path = self.write('x' * 5000)
        result = doctor._log_lines(path, limit=10)
        self.assertEqual(result, ['x' * 10])


class SchedulerStepFieldTests(SchedulerSectionFixture):
    """The SCHEDULER row's `step:` field and its one new `YELLOW` (F-0142)."""

    def test_a_healthy_job_mid_step_is_ok_and_shows_the_step_between_last_run_and_log(self):
        log = self.write_log(
            'tick-sample-record.log',
            f'[step:harvest] start owner=asf pid={os.getpid()} at=2026-01-01T00:00:00Z\n'
            '[command:batch] merging #412\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3', '-m', 'asf.cli'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.OK)
        detail = rows[0][2]
        self.assertIn('step: harvest running', detail)
        self.assertIn('(asf, pid', detail)
        self.assertLess(detail.index('last-run='), detail.index('  step: '))
        self.assertLess(detail.index('  step: '), detail.index('  log: '))

    def test_a_dead_pid_mid_step_is_yellow_and_names_the_unfinished_step(self):
        log = self.write_log(
            'tick-sample-record.log',
            '[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3', '-m', 'asf.cli'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))

        with mock.patch.object(doctor.lifecycle, 'pid_alive', return_value=False):
            rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.YELLOW)
        self.assertIn('unfinished — its tick (pid 48213) is gone', rows[0][2])
        self.assertFalse(doctor.scheduler_is_red(rows))

    def test_a_nonzero_last_exit_with_a_dangling_start_line_stays_red_not_downgraded(self):
        log = self.write_log(
            'tick-sample-record.log',
            '[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record',
                   read_fixture('launchctl-print.txt').replace('last exit code = 0',
                                                               'last exit code = 1'))
        with mock.patch.object(doctor.lifecycle, 'pid_alive', return_value=False):
            rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.RED)

    def test_a_never_exited_job_past_two_intervals_with_a_dangling_start_line_stays_red(self):
        log = self.write_log(
            'tick-sample-dispatch.log',
            '[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n')
        self.install_plist('asf.sample.dispatch', ['python3', '-m', 'asf.cli'],
                           log=log, interval=600, age_s=3600)
        fake_loaded(self.statedir, ['asf.sample.dispatch'])
        fake_print(self.statedir, 'asf.sample.dispatch',
                   read_fixture('launchctl-print-never-exited.txt'))
        with mock.patch.object(doctor.lifecycle, 'pid_alive', return_value=False):
            rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertEqual(rows[0][0], doctor.RED)

    def test_a_job_with_no_step_line_grows_no_step_field_and_is_byte_for_byte_todays_row(self):
        log = self.write_log('tick-sample-record.log', 'starting\ntick: state committed\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3', '-m', 'asf.cli'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertNotIn('  step: ', rows[0][2])
        self.assertEqual(rows[0][2],
                         'state=running  runs=7  last-exit=0  '
                         'last-run=' + doctor.format_age(doctor._age_s(log)) +
                         '  log: tick: state committed')

    def test_a_job_whose_log_ends_in_an_end_line_grows_no_step_field(self):
        log = self.write_log(
            'tick-sample-record.log',
            '[step:harvest] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n'
            '[step:harvest] 22.7s ok=yes owner=asf pid=48213 at=2026-01-01T00:00:22Z\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3', '-m', 'asf.cli'], log=log)
        fake_loaded(self.statedir, ['asf.sample.record'])
        fake_print(self.statedir, 'asf.sample.record', read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        self.assertNotIn('  step: ', rows[0][2])

    def test_each_clocks_row_reads_its_own_log(self):
        """P19: a job row's step field names its own clock's tick, not another's."""
        record_log = self.write_log(
            'tick-sample-record.log',
            f'[step:record] start owner=asf pid={os.getpid()} at=2026-01-01T00:00:00Z\n')
        health_log = self.write_log(
            'tick-sample-health.log',
            f'[step:health] start owner=asf pid={os.getpid()} at=2026-01-01T00:00:00Z\n')
        wave_log = self.write_log(
            'tick-sample-wave.log',
            '[step:wave] start owner=asf pid=48213 at=2026-01-01T00:00:00Z\n'
            '[step:wave] 1.0s ok=yes owner=asf pid=48213 at=2026-01-01T00:00:01Z\n')
        self.install_plist('asf.sample.record', ['/usr/bin/python3'], log=record_log)
        self.install_plist('asf.sample.health', ['/usr/bin/python3'], log=health_log)
        self.install_plist('asf.sample.wave', ['/usr/bin/python3'], log=wave_log)
        fake_loaded(self.statedir, ['asf.sample.record', 'asf.sample.health', 'asf.sample.wave'])
        for label in ('asf.sample.record', 'asf.sample.health', 'asf.sample.wave'):
            fake_print(self.statedir, label, read_fixture('launchctl-print.txt'))

        rows = doctor.scheduler_rows(self.cfg(), self.product)
        by_label = {r[1]: r[2] for r in rows}
        self.assertIn('step: record running', by_label['asf.sample.record'])
        self.assertIn('step: health running', by_label['asf.sample.health'])
        self.assertNotIn('  step: ', by_label['asf.sample.wave'])


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


class TestBriefSmoke(unittest.TestCase):
    """§12: the doctor renders one brief of every kind for the product — a config smoke test.
    A kind whose brief raises turns the row red, naming the kind and the error."""

    def test_every_kind_renders_for_a_product(self):
        from asf import briefs
        ok, detail = doctor.check_briefs(env.Product('sample', {}))
        self.assertTrue(ok, detail)
        self.assertEqual(detail, f'{len(briefs.KINDS)} brief kinds render')

    def test_a_kind_that_raises_is_red_with_its_kind_and_error(self):
        import importlib
        build_mod = importlib.import_module('asf.briefs.build')
        real = build_mod.load_template

        def broken(kind):
            if kind == 'review':
                raise KeyError('reviews_dir')
            return real(kind)
        with mock.patch.object(build_mod, 'load_template', broken):
            ok, detail = doctor.check_briefs(env.Product('sample', {}))
        self.assertFalse(ok)
        self.assertEqual(detail, "review: KeyError: 'reviews_dir'")
        self.assertTrue(doctor.is_red([('briefs', True, ok, detail)]))


if __name__ == '__main__':
    unittest.main()


class TokenCaps(unittest.TestCase):
    """`doctor.check_token_caps` — the doctor's `token-caps` row."""

    @staticmethod
    def _rows(block):
        data = {} if block is None else {'token_caps': block}
        findings = doctor.check_token_caps({}, env.Product('a', data))
        return findings, [('token-caps', False, ok, d) for ok, d in findings]

    def test_doctor_prints_the_resolved_caps(self):
        findings, _ = self._rows(None)
        self.assertEqual(len(findings), 1)
        ok, detail = findings[0]
        self.assertTrue(ok)
        for text in ('in 8.0 M', 'out 1.0 M', 'cache rd 400.0 M', 'cache wr 40.0 M'):
            self.assertIn(text, detail)

    def test_doctor_names_a_bad_block(self):
        findings, rows = self._rows({'default': {'bogus': 1}})
        self.assertTrue(findings)
        self.assertFalse(findings[0][0])
        self.assertIn('bogus', findings[0][1])
        self.assertFalse(doctor.is_red(rows))

    def test_doctor_names_an_off_dimension(self):
        findings, rows = self._rows({'spec': {'cache_read': 'off'}})
        self.assertTrue(any(not ok and 'spec.cache_read' in d for ok, d in findings), findings)
        self.assertFalse(doctor.is_red(rows))


class CiMeasureRows(unittest.TestCase):
    """`doctor.check_ci_measure` — the slow box, the flaky box and the baseline that grew, off
    the `ci` stream and two local state files, no CI host call at all (PD8). Every row advisory
    (D19); every missing or broken state file silent — a diagnostic command that dies on a state
    file is worse than one that says nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.root = tempfile.mkdtemp()
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.product = env.Product('p', {})
        from asf import ci_pool
        self._backend_patch = mock.patch.object(
            ci_pool, 'backend_for', side_effect=AssertionError('ci_pool.backend_for called'))
        self._backend_patch.start()
        self._subprocess_patch = mock.patch(
            'subprocess.run', side_effect=AssertionError('subprocess.run called'))
        self._subprocess_patch.start()

    def tearDown(self):
        self._subprocess_patch.stop()
        self._backend_patch.stop()
        env.ASF_HOME = self.home
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def _ts(self, days_ago=1):
        return (self.now - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _job(self, name, conclusion, runner_name, seconds):
        return {'name': name, 'conclusion': conclusion, 'runner': runner_name, 'seconds': seconds}

    def _ev(self, jobs, days_ago=1):
        return {'ts': self._ts(days_ago), 'jobs': jobs}

    def _write_stream(self, events):
        import json
        from asf.metrics import metrics
        by_day = {}
        for e in events:
            by_day.setdefault(e['ts'][:10], []).append(e)
        for day, evs in by_day.items():
            path = metrics.stream_path(self.root, 'ci', day)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                for e in evs:
                    f.write(json.dumps(e) + '\n')

    def _write_census(self, runners):
        import json
        from asf import ci_census
        path = os.path.join(env.state_dir(self.product), ci_census.CENSUS_FILE)
        doc = {'v': 1, 'taken': self._ts(days_ago=0),
               'runners': [{'runner': r, 'tier': t} for r, t in runners]}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)

    def _write_baselines(self, per):
        import json
        from asf import ci_measure
        path = os.path.join(env.state_dir(self.product), ci_measure.BASELINES_FILE)
        doc = {'v': 1, 'taken': self._ts(days_ago=0), 'window_days': 14, 'per': per, 'tier': {}}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)

    def test_no_census_gives_no_rows(self):
        self._write_stream([self._ev([self._job('gate', 'success', 'ci-1', 100.0)])])
        self._write_baselines({'ci-1': {'gate': {'p50_s': 100.0, 'n': 10}}})
        self.assertEqual(doctor.check_ci_measure(self.product, now=self.now, root=self.root), [])

    def test_no_state_at_all_gives_no_rows_and_raises_nothing(self):
        missing_root = os.path.join(self.tmp, 'no-such-record-dir')
        self.assertEqual(
            doctor.check_ci_measure(self.product, now=self.now, root=missing_root), [])

    def test_truncated_census_gives_no_rows(self):
        from asf import ci_census
        path = os.path.join(env.state_dir(self.product), ci_census.CENSUS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self._write_stream([self._ev([self._job('gate', 'success', 'ci-1', 100.0)])])
        self.assertEqual(doctor.check_ci_measure(self.product, now=self.now, root=self.root), [])

    def test_missing_record_root_gives_no_rows(self):
        self._write_census([('ci-1', 'asf-fast')])
        missing_root = os.path.join(self.tmp, 'no-such-record-dir')
        self.assertEqual(
            doctor.check_ci_measure(self.product, now=self.now, root=missing_root), [])

    def test_slow_runner_row_names_the_worst_kind_and_tier_none_for_the_fleet_best(self):
        self._write_census([('ci-1', 'asf-fast'), ('ci-3', 'asf-bulk')])
        events = [self._ev([self._job('gate', 'success', 'ci-1', 100.0)], days_ago=d)
                  for d in range(1, 12)]
        events += [self._ev([self._job('gate', 'success', 'ci-3', 210.0)], days_ago=d)
                   for d in range(1, 12)]
        self._write_stream(events)
        rows = doctor.check_ci_measure(self.product, now=self.now, root=self.root)
        self.assertEqual(len(rows), 1)
        required, ok, detail = rows[0]
        self.assertFalse(required)
        self.assertFalse(ok)
        self.assertEqual(detail, 'ci measure: ci-3 runs gate at 210s, 2.1× the fleet best '
                                  '(11 green readings, 14 d) — asf-bulk')

    def test_flaky_runner_row_and_no_row_under_min_readings(self):
        self._write_census([('ci-1', 'asf-fast')])
        # 18 green + 7 red (within a 7-day span, well inside the 14 d window) => green rate
        # 18 / 25 == 0.72, n == 18 (D14, the green count, not the rated total).
        events = [self._ev([self._job('flaky_job', 'success', 'ci-6', 50.0)], days_ago=1 + i % 7)
                  for i in range(18)]
        events += [self._ev([self._job('flaky_job', 'failure', 'ci-6', 50.0)], days_ago=1 + i % 7)
                   for i in range(7)]
        events += [self._ev([self._job('flaky_job', 'success', 'ci-9', 50.0)], days_ago=1 + i)
                   for i in range(4)]
        events += [self._ev([self._job('flaky_job', 'failure', 'ci-9', 50.0)], days_ago=1)]
        self._write_stream(events)
        rows = doctor.check_ci_measure(self.product, now=self.now, root=self.root)
        self.assertEqual(len(rows), 1)
        required, ok, detail = rows[0]
        self.assertFalse(required)
        self.assertFalse(ok)
        self.assertEqual(detail,
                          'ci measure: ci-6 green 0.72 over 18 readings — flaky, never asf-fast')

    def test_baseline_regression_row_and_no_row_under_threshold(self):
        self._write_census([('ci-1', 'asf-fast')])
        self._write_baselines({
            'ci-4': {'gate2': {'p50_s': 318.0, 'n': 8}},
            'ci-5': {'gate3': {'p50_s': 300.0, 'n': 8}},
        })
        events = [self._ev([self._job('gate2', 'success', 'ci-4', 512.0)], days_ago=1)]
        events += [self._ev([self._job('gate3', 'success', 'ci-5', 420.0)], days_ago=1)]
        self._write_stream(events)
        rows = doctor.check_ci_measure(self.product, now=self.now, root=self.root)
        self.assertEqual(len(rows), 1)
        required, ok, detail = rows[0]
        self.assertFalse(required)
        self.assertFalse(ok)
        self.assertEqual(detail, 'ci baseline: gate2 on ci-4 last ran 512s against its 318s '
                                  'baseline (1.6×) — 8 green readings')

    def test_every_row_is_advisory_never_required(self):
        self._write_census([('ci-1', 'asf-fast'), ('ci-3', 'asf-bulk')])
        events = [self._ev([self._job('gate', 'success', 'ci-1', 100.0)], days_ago=d)
                  for d in range(1, 12)]
        events += [self._ev([self._job('gate', 'success', 'ci-3', 210.0)], days_ago=d)
                   for d in range(1, 12)]
        self._write_stream(events)
        rows = doctor.check_ci_measure(self.product, now=self.now, root=self.root)
        self.assertTrue(rows)
        self.assertTrue(all(required is False for required, _ok, _detail in rows))


class ClockCodeRowTests(unittest.TestCase):
    """`doctor.check_clock_code` — spec t-0755 §3.4, pure over `scheduler.clock_code`'s dict."""

    def _check(self, info):
        with mock.patch.object(doctor.scheduler, 'clock_code', return_value=info):
            return doctor.check_clock_code(env.Product('sample', {}))

    def test_installed_package_detail_is_unchanged_and_carries_no_suffix(self):
        ok, detail = self._check({'snapshot': False, 'root': '/opt/asf'})
        self.assertTrue(ok)
        self.assertEqual(detail, 'installed package /opt/asf')

    def test_no_tick_yet_gets_the_snapshot_count_suffix(self):
        ok, detail = self._check({'snapshot': True, 'root': '/repo', 'sha': None, 'at': None,
                                   'head': 'a' * 40, 'snapshots': 5,
                                   'launcher_stale': (False, '')})
        self.assertTrue(ok)
        self.assertIn('no tick has run from one yet', detail)
        self.assertTrue(detail.endswith('· 5 snapshots'))

    def test_sha_detail_ends_with_the_snapshot_count(self):
        ok, detail = self._check({'snapshot': True, 'root': '/repo', 'sha': 'b' * 40,
                                   'at': time.time(), 'head': 'b' * 40, 'snapshots': 5,
                                   'launcher_stale': (False, '')})
        self.assertTrue(ok)
        self.assertTrue(detail.endswith('· 5 snapshots'))

    def test_a_stale_launcher_names_the_next_tick_and_the_install_command(self):
        ok, detail = self._check({'snapshot': True, 'root': '/repo', 'sha': 'c' * 40,
                                   'at': time.time(), 'head': 'c' * 40, 'snapshots': 5,
                                   'launcher_stale': (True, 'the installed launch.py differs '
                                                            "from this package's asf/snapshot.py")})
        self.assertTrue(ok)
        self.assertIn('launcher is stale', detail)
        self.assertIn('the next tick refreshes it', detail)
        self.assertIn('`asf scheduler install --product sample`', detail)
        self.assertNotIn('clock install', detail)

    def test_a_stale_launcher_on_the_no_tick_yet_branch_still_gets_both_suffixes(self):
        ok, detail = self._check({'snapshot': True, 'root': '/repo', 'sha': None, 'at': None,
                                   'head': 'd' * 40, 'snapshots': 1,
                                   'launcher_stale': (True, 'the installed launch.py differs '
                                                            "from this package's asf/snapshot.py")})
        self.assertTrue(ok)
        self.assertIn('no tick has run from one yet', detail)
        self.assertIn('· 1 snapshots', detail)
        self.assertIn('launcher is stale', detail)
        self.assertIn('`asf scheduler install --product sample`', detail)

    def test_ok_is_true_in_every_branch(self):
        for info in [
            {'snapshot': False, 'root': '/opt/asf'},
            {'snapshot': True, 'root': '/repo', 'sha': None, 'at': None, 'head': None,
             'snapshots': 0, 'launcher_stale': (False, '')},
            {'snapshot': True, 'root': '/repo', 'sha': 'e' * 40, 'at': time.time(),
             'head': 'e' * 40, 'snapshots': 3, 'launcher_stale': (True, 'stale')},
        ]:
            ok, _detail = self._check(info)
            self.assertTrue(ok)


class SecurityRowTests(unittest.TestCase):
    """`asf.security.doctor_findings` — the doctor's `security` row (spec F-0069 §2.10, replan
    T-0367): the record's adoption of R-0009, and the age of the last read of the host's own
    alert feeds. A product that configures nothing still gets the row (P21) rather than being
    skipped for a block it never asked for."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='doctor_security_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.backlog = os.path.join(self.tmp, 'backlog')
        os.makedirs(self.backlog)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        env.ASF_HOME = self._orig_home

    def product(self):
        return env.Product('sample', {'repo_slug': 'acme/x', 'backlog_dir': self.backlog})

    def _write_card(self):
        with open(os.path.join(self.backlog, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump({'items': {'R-0009': {'id': 'R-0009', 'type': 'rule',
                                             'check': 'tools/checks/r0009.sh'}}}, f)

    def _write_cache(self, age_h):
        from asf.security import alerts
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = (now - datetime.timedelta(hours=age_h)).strftime(alerts._TS_FORMAT)
        alerts._write_cache(alerts.cache_path(self.product()),
                             {'ts': ts, 'secrets': [], 'dependencies': []})

    def findings(self):
        from asf import security
        return security.doctor_findings(self.product())

    def test_unconfigured_product_is_red_and_names_the_missing_card(self):
        findings = self.findings()
        self.assertEqual(len(findings), 1)
        ok, detail = findings[0]
        self.assertFalse(ok)
        self.assertIn('not adopted', detail)
        self.assertIn('r0009.sh', detail)

    def test_adopted_with_a_fresh_cache_is_green_and_prints_the_counts(self):
        self._write_card()
        self._write_cache(2)
        findings = self.findings()
        self.assertEqual(len(findings), 1)
        ok, detail = findings[0]
        self.assertTrue(ok, detail)
        self.assertIn('2h', detail)
        self.assertIn('R-0009 adopted', detail)

    def test_adopted_with_a_stale_cache_is_red_and_names_the_age(self):
        self._write_card()
        self._write_cache(30)
        findings = self.findings()
        self.assertEqual(len(findings), 1)
        ok, detail = findings[0]
        self.assertFalse(ok)
        self.assertIn('30h', detail)

    def test_adopted_with_no_cache_at_all_is_red_and_says_never_read(self):
        self._write_card()
        findings = self.findings()
        self.assertEqual(len(findings), 1)
        ok, detail = findings[0]
        self.assertFalse(ok)
        self.assertIn('never been read', detail)

    def test_no_finding_ever_carries_an_alert_value_or_a_machine_address(self):
        for setup in (lambda: None,
                      lambda: (self._write_card(), self._write_cache(2)),
                      lambda: (self._write_card(), self._write_cache(30)),
                      lambda: self._write_card()):
            setup()
            for _ok, detail in self.findings():
                self.assertNotIn('acme/x', detail)
                self.assertNotIn(self.backlog, detail)
