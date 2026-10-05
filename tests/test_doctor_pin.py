"""The doctor's pin rows (W0-PR3): ``clock install``'s effective import, ``product loads under
venv``, ``cli dispatcher`` and the ``product`` warnings row.

Every venv is a temp dir whose ``bin/python`` is a shell script, every plist a temp file, the asf
home a temp dir, and pipx's venvs dir ``$PIPX_HOME/venvs`` under the same temp dir — never the
operator's ``~/.ASF``, LaunchAgents, pipx or ``~/.local/bin/asf``."""
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from asf import clockinstall, conventions, dispatch, doctor, env, installs, scheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA = 'abcdef1' + '0' * 33
OLD = '1234567' + '0' * 33


def _script(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n' + body)
    os.chmod(path, 0o755)
    return path


def fake_venv(root, name, sha=SHA, python_body=None):
    """A pipx-shaped venv: ``direct_url.json`` for ``sha``, a site-packages ``asf`` dir, and a
    ``bin/python`` that answers the import probe with ``$FAKE_ASF`` when the plist sets it, else
    this venv's own ``asf/__init__.py``."""
    venv = os.path.join(root, name)
    site = os.path.join(venv, 'lib', 'python3.12', 'site-packages')
    os.makedirs(os.path.join(site, 'asf'))
    open(os.path.join(site, 'asf', '__init__.py'), 'w').close()
    info = os.path.join(site, 'asf_factory-0.1.0.dist-info')
    os.makedirs(info)
    with open(os.path.join(info, 'direct_url.json'), 'w', encoding='utf-8') as f:
        json.dump({'url': 'git+https://example.invalid/r.git', 'vcs_info': {'commit_id': sha}}, f)
    own = os.path.join(site, 'asf', '__init__.py')
    _script(os.path.join(venv, 'bin', 'python'),
            python_body or f'echo "${{FAKE_ASF:-{own}}}"\n')
    _script(os.path.join(venv, 'bin', 'asf'), 'exit 0\n')
    return venv


def shim_venv(root, name, pythonpath, sha=SHA):
    """A venv whose ``bin/python`` is this test's own interpreter over ``pythonpath``'s ``asf``."""
    venv = fake_venv(root, name, sha)
    _script(os.path.join(venv, 'bin', 'python'),
            f'PYTHONPATH={pythonpath} exec {sys.executable} "$@"\n')
    return venv


OLD_READER_ENV = '''
import os
class ConfigError(Exception):
    pass
def validate_product_text(text):
    return [(n, line.split(':')[0], 'is not a field of the product file')
            for n, line in enumerate(text.splitlines(), 1) if line.startswith('surprise:')]
def load_product(name):
    path = os.path.join(os.environ['ASF_HOME'], 'products', name + '.yaml')
    found = validate_product_text(open(path).read())
    if found:
        raise ConfigError(f'{path}: line {found[0][0]}: {found[0][1]} {found[0][2]}')
    return object()
'''


def old_reader(root):
    """A package dir holding an ``asf`` whose loader predates the warnings split: every unknown
    key refuses the file."""
    pkg = os.path.join(root, 'old-reader')
    os.makedirs(os.path.join(pkg, 'asf'))
    open(os.path.join(pkg, 'asf', '__init__.py'), 'w').close()
    with open(os.path.join(pkg, 'asf', 'env.py'), 'w', encoding='utf-8') as f:
        f.write(OLD_READER_ENV)
    return pkg


class PinFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-doctor-pin-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'asf-home')
        os.makedirs(os.path.join(self.home, 'products'))
        os.makedirs(os.path.join(self.home, 'state', 'sample'))
        self.venvs = os.path.join(self.tmp, 'pipx', 'venvs')
        os.makedirs(self.venvs)
        self.agents = os.path.join(self.tmp, 'LaunchAgents')
        os.makedirs(self.agents)
        for patch in (mock.patch.object(env, 'ASF_HOME', self.home),
                      mock.patch.dict(os.environ, {'PIPX_HOME': os.path.dirname(self.venvs)}),
                      mock.patch.object(scheduler, 'launch_agents_dir', return_value=self.agents),
                      mock.patch.object(clockinstall, 'venvs_dir', return_value=self.venvs),
                      mock.patch.object(clockinstall, 'trunk', return_value=('', ''))):
            patch.start()
            self.addCleanup(patch.stop)
        self.product = env.Product('sample', {'repo_dir': self.tmp, 'backlog_dir': self.tmp,
                                              'ci': {'provider': 'none'}})
        self.pin = fake_venv(self.venvs, 'asf-factory-sample-abcdef1')
        self.shared = fake_venv(self.venvs, 'asf-factory', OLD)

    def write_plist(self, clock, venv, env_vars=None):
        path = os.path.join(self.agents, f'asf.sample.{clock}.plist')
        with open(path, 'wb') as f:
            plistlib.dump({'Label': f'asf.sample.{clock}',
                           'ProgramArguments': [os.path.join(venv, 'bin', 'python'), '-m',
                                                'asf.cli', 'tick', '--product', 'sample'],
                           'EnvironmentVariables': env_vars or {'ASF_HOME': self.home},
                           'WorkingDirectory': self.tmp}, f)
        return path

    def pin_it(self, venv=None, sha=SHA):
        installs.write('sample', sha, venv or self.pin,
                       previous={'sha': OLD, 'venv': self.shared}, by='test')

    def rows(self):
        return doctor.check_clock_installs({}, self.product)


class EffectiveImportTests(PinFixture):
    def test_a_pinned_clock_importing_its_pin_is_ok_and_names_pin_venv_and_previous(self):
        self.pin_it()
        self.write_plist('tick', self.pin)
        [(required, ok, detail)] = self.rows()
        self.assertTrue(required)
        self.assertTrue(ok, detail)
        self.assertIn('pinned asf-factory-sample-abcdef1 (effective import ok)', detail)
        self.assertIn('pin=abcdef100 venv=asf-factory-sample-abcdef1 previous=123456700', detail)

    def test_a_plist_env_that_imports_another_venv_is_red(self):
        self.pin_it()
        other = os.path.join(self.shared, 'lib', 'python3.12', 'site-packages', 'asf',
                             '__init__.py')
        self.write_plist('tick', self.pin, {'ASF_HOME': self.home, 'FAKE_ASF': other})
        [(required, ok, detail)] = self.rows()
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn(f'clock asf.sample.tick imports asf from {other}, pin is {self.pin}',
                      detail)
        self.assertTrue(doctor.is_red([('clock install', required, ok, detail)]))

    def test_a_plist_still_on_the_shared_venv_after_the_pin_is_red(self):
        self.pin_it()
        self.write_plist('tick', self.shared)
        [(_r, ok, detail)] = self.rows()
        self.assertFalse(ok)
        self.assertIn('pin is ' + self.pin, detail)

    def test_an_unreadable_pin_while_a_per_product_venv_exists_is_red(self):
        with open(installs.record_path('sample'), 'w', encoding='utf-8') as f:
            f.write('{"sha": "abc", "ven')       # cut off mid-write
        self.write_plist('tick', self.shared)
        rows = self.rows()
        reds = [d for r, ok, d in rows if r and ok is False]
        self.assertTrue(reds, rows)
        self.assertIn('pin unreadable', reds[0])
        self.assertIn('asf-factory-sample-abcdef1', reds[0])

    def test_no_record_but_a_clock_on_a_per_product_venv_is_red(self):
        self.write_plist('tick', self.pin)
        [(_r, ok, detail)] = self.rows()
        self.assertFalse(ok)
        self.assertIn('no pin names it', detail)

    def test_an_unpinned_clock_on_the_shared_venv_is_ok_and_says_where_it_imports(self):
        self.write_plist('tick', self.shared)
        [(_r, ok, detail)] = self.rows()
        self.assertTrue(ok, detail)
        self.assertIn('unpinned · imports asf from ' + self.shared, detail)

    def test_a_pinned_clock_whose_import_fails_is_red(self):
        self.pin_it()
        _script(os.path.join(self.pin, 'bin', 'python'), 'echo "ModuleNotFoundError: asf" >&2; exit 1\n')
        self.write_plist('tick', self.pin)
        [(_r, ok, detail)] = self.rows()
        self.assertFalse(ok)
        self.assertIn('import asf failed', detail)

    def test_the_probe_runs_with_the_plists_env_and_working_directory(self):
        self.pin_it()
        self.write_plist('tick', self.pin, {'ASF_HOME': self.home, 'MARK': 'x'})
        seen = []

        def probe(interpreter, env_vars, cwd):
            seen.append((interpreter, dict(env_vars), cwd))
            return os.path.join(self.pin, 'lib', 'x', 'asf', '__init__.py'), ''
        doctor.check_clock_installs({}, self.product, probe=probe)
        self.assertEqual(seen, [(os.path.join(self.pin, 'bin', 'python'),
                                 {'ASF_HOME': self.home, 'MARK': 'x'}, self.tmp)])


class ProductLoadsUnderVenvTests(PinFixture):
    def write_product(self, extra=''):
        with open(os.path.join(self.home, 'products', 'sample.yaml'), 'w') as f:
            f.write(f'product: sample\nrepo_dir: {self.tmp}\nbacklog_dir: {self.tmp}\n'
                    f'ci: {{provider: none}}\n{extra}')

    def test_loads_under_the_pins_own_interpreter(self):
        shutil.rmtree(self.pin)
        venv = shim_venv(self.venvs, 'asf-factory-sample-abcdef1', ROOT)
        self.pin_it(venv)
        self.write_product('surprise: 1\n')
        [(required, ok, detail)] = doctor.check_product_loads_under_venv({}, self.product)
        self.assertTrue(required)
        self.assertTrue(ok, detail)
        self.assertIn('pin asf-factory-sample-abcdef1 @ abcdef1: loads', detail)
        self.assertIn('1 warning(s): surprise', detail)

    def test_an_older_reader_that_refuses_the_file_is_red(self):
        shutil.rmtree(self.pin)
        venv = shim_venv(self.venvs, 'asf-factory-sample-abcdef1', old_reader(self.tmp))
        self.pin_it(venv)
        self.write_product('surprise: 1\n')
        [(required, ok, detail)] = doctor.check_product_loads_under_venv({}, self.product)
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('refuses products/sample.yaml', detail)
        self.assertIn('surprise', detail)

    def test_unpinned_loads_under_the_clocks_interpreter(self):
        venv = shim_venv(self.venvs, 'asf-factory-shared2', ROOT)
        self.write_plist('tick', venv)
        self.write_product()
        [(_r, ok, detail)] = doctor.check_product_loads_under_venv({}, self.product)
        self.assertTrue(ok, detail)
        self.assertIn('clock asf.sample.tick (unpinned)', detail)

    def test_nothing_to_load_under_is_a_skip(self):
        [(required, ok, _d)] = doctor.check_product_loads_under_venv({}, self.product)
        self.assertFalse(required)
        self.assertIsNone(ok)


class CliDispatcherTests(PinFixture):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp, 'bin', 'asf')
        os.makedirs(os.path.dirname(self.path))

    def write_dispatcher(self):
        rc, detail = dispatch.install(path=self.path, asf_home=self.home, venvs=self.venvs,
                                      default_product='', cli=os.path.join(self.shared, 'bin', 'asf'))
        self.assertEqual(rc, 0, detail)

    def test_resolves_into_the_pin(self):
        self.pin_it()
        self.write_dispatcher()
        [(required, ok, detail)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertTrue(required)
        self.assertTrue(ok, detail)
        self.assertTrue(detail.startswith('ok · sample → asf-factory-sample-abcdef1'), detail)

    def test_a_pin_whose_cli_is_gone_falls_back_and_is_red(self):
        self.pin_it()
        os.remove(os.path.join(self.pin, 'bin', 'asf'))
        self.write_dispatcher()
        [(_r, ok, detail)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertFalse(ok)
        self.assertIn('pin is ' + self.pin, detail)

    def test_the_shared_link_on_a_pinned_product_is_a_warning(self):
        self.pin_it()
        os.symlink(os.path.join(self.shared, 'bin', 'asf'), self.path)
        [(required, ok, detail)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertEqual((required, ok), (False, 'warn'))
        self.assertIn('not the dispatcher', detail)
        self.assertIn('warn', doctor.format_table('sample', [('cli dispatcher', required, ok, detail)]))

    def test_the_shared_link_on_an_unpinned_product_is_ok(self):
        os.symlink(os.path.join(self.shared, 'bin', 'asf'), self.path)
        [(_r, ok, detail)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertTrue(ok)
        self.assertIn('unpinned', detail)

    def test_no_cli_at_all_for_a_pinned_product_is_red(self):
        self.pin_it()
        [(_r, ok, _d)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertFalse(ok)

    def test_a_damaged_pin_is_red_here_too(self):
        with open(installs.record_path('sample'), 'w') as f:
            f.write('not json')
        self.write_dispatcher()
        [(_r, ok, detail)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertFalse(ok)
        self.assertIn('pin unreadable', detail)


class ProductWarningsRowTests(unittest.TestCase):
    def test_unknown_keys_and_unknown_flags_are_warn_rows_never_red(self):
        product = env.Product('sample', {'conventions': {'flags': {'mechanical': 'on',
                                                                   'mechanicl': 'on'}}},
                              warnings=[(7, 'surprise', env.UNKNOWN_KEY)])
        rows = doctor.check_product_warnings(product)
        self.assertEqual([ok for ok, _d in rows], ['warn', 'warn'])
        self.assertIn('line 7: surprise is not a field', rows[0][1])
        self.assertIn('conventions.flags: mechanicl', rows[1][1])
        self.assertIn(conventions.KNOWN_FLAGS[0], rows[1][1])
        table = [('product', False, ok, d) for ok, d in rows]
        self.assertFalse(doctor.is_red(table))
        self.assertIn('product  warn', doctor.format_table('sample', table))

    def test_a_clean_file_is_one_ok_row(self):
        rows = doctor.check_product_warnings(env.Product('sample', {}))
        self.assertEqual(rows, [(True, 'no unknown keys, no unknown flags')])


class PlanHeadingsRowTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='plan_rows_')
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        os.makedirs(os.path.join(self.repo, 'docs', 'plans'))
        self.product = env.Product('sample', {'repo_dir': self.repo,
                                              'conventions': {'plans_dir': 'docs/plans'}})

    def plan(self, name, text):
        with open(os.path.join(self.repo, 'docs', 'plans', name), 'w') as f:
            f.write(text)

    def test_a_plan_with_task_like_headings_and_no_parsed_task_is_a_warn_row(self):
        self.plan('good.md', '### Task T1: a\n')
        self.plan('drift.md', '# P\n### Task #1 - a\n### Task #2 - b\n')
        self.plan('prose.md', '# P\nno tasks\n')
        rows = doctor.check_plan_headings(self.product)
        self.assertEqual([ok for ok, _d in rows], ['warn'])
        self.assertIn('docs/plans/drift.md', rows[0][1])
        self.assertIn('2 Task-like heading', rows[0][1])
        self.assertNotIn('good.md', rows[0][1])

    def test_clean_plans_are_one_ok_row(self):
        self.plan('good.md', '### Task T1: a\n')
        self.assertEqual(doctor.check_plan_headings(self.product),
                         [(True, '1 plan(s): every Task-like heading parses')])


if __name__ == '__main__':
    unittest.main()
