import os
import subprocess
import sys
import tempfile
import unittest

from asf import doctor, env

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

    def test_launchd_without_label_is_red(self):
        ok, detail = doctor.check_scheduler({'scheduler': {'kind': 'launchd'}})
        self.assertFalse(ok)


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


if __name__ == '__main__':
    unittest.main()
