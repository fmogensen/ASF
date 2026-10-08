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

    def test_the_shared_link_on_a_pinned_product_is_red(self):
        """F-0283: its hooks run the shared install, never the pin."""
        self.pin_it()
        os.symlink(os.path.join(self.shared, 'bin', 'asf'), self.path)
        [(required, ok, detail)] = doctor.check_cli_dispatcher(self.product, self.path)
        self.assertEqual((required, ok), (True, False))
        self.assertIn('not the dispatcher', detail)
        self.assertIn('asf hooks install', detail)

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


class RedactionHookExecPathTests(PinFixture):
    """F-0283: a pinned product's git hooks must exec the dispatcher — one naming any other asf
    runs whatever was installed last, never the pin. Read off the hook files, in a temp repo."""

    def setUp(self):
        super().setUp()
        import subprocess
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.product = env.Product('sample', {'repo_dir': self.repo, 'ci': {'provider': 'none'}})
        self.dispatcher = os.path.join(self.tmp, 'bin', 'asf')
        os.makedirs(os.path.dirname(self.dispatcher))
        rc, detail = dispatch.install(path=self.dispatcher, asf_home=self.home, venvs=self.venvs,
                                      default_product='', cli=os.path.join(self.shared, 'bin', 'asf'))
        self.assertEqual(rc, 0, detail)

    def write_hooks(self, asf_path):
        from asf import hooks
        d = hooks.git_hooks_dir(self.repo)
        os.makedirs(d, exist_ok=True)
        for name in hooks.GIT_HOOK_NAMES:
            with open(os.path.join(d, name), 'w') as f:
                f.write(hooks._git_hook_body(name, asf_path, 'sample'))

    def test_a_pinned_products_hook_naming_the_shared_install_is_red(self):
        self.pin_it()
        self.write_hooks(os.path.join(self.shared, 'bin', 'asf'))
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertFalse(ok)
        self.assertIn('not the dispatcher', detail)

    def test_a_pinned_products_hook_naming_the_dispatcher_is_ok(self):
        self.pin_it()
        self.write_hooks(self.dispatcher)
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertTrue(ok, detail)

    def _old_body(self, name, asf_path):
        """The body :func:`asf.hooks._git_hook_body` wrote before F-0283 Task 1 (measured at
        ``9a3a897d6``): it names ``asf_path`` directly, with none of the current body's
        ``asf_pinned()`` guard — asf's own hook (the marker is there, the line matches
        :func:`asf.hooks.is_git_hook_ours`), just an older shape than this build writes."""
        from asf import hooks
        p = 'sample'
        lines = [
            '#!/bin/sh',
            '# written by asf hooks install — the redaction gate (F-0075)',
            f'if [ -x "{asf_path}" ]; then exec "{asf_path}" redact --{name} --product {p}; fi',
            f'if [ -x "$HOME/.local/bin/asf" ]; then exec "$HOME/.local/bin/asf" redact --{name} '
            f'--product {p}; fi',
            '# no asf here (a cloud container): the repository\'s own check, else refused',
            'top=$(git rev-parse --show-toplevel 2>/dev/null)',
            f'if [ -n "$top" ] && [ -x "$top/{hooks.CHECKS_DIR}/redact.sh" ]; then '
            f'exec "$top/{hooks.CHECKS_DIR}/redact.sh" --{name}; fi',
        ]
        what = 'commit' if name == 'pre-commit' else 'push'
        lines += [
            f'echo "asf: REDACTION REFUSED ({name}) — no asf and no {hooks.CHECKS_DIR}/redact.sh '
            f'here: nothing can scan this {what} for names or secrets, so it is refused (a pushed '
            f'branch is public before any landing re-scan). Install asf, or add '
            f'{hooks.CHECKS_DIR}/redact.sh." >&2',
            'exit 1',
        ]
        return '\n'.join(lines) + '\n'

    def test_a_hook_with_an_older_dispatcher_body_is_named_stale_until_rewritten(self):
        """S-76255: measured at ``9a3a897d6`` (pre-Task-1), a pinned product's pre-commit naming
        the dispatcher with the older, guard-less body gave ``verify() == []`` and
        ``check_redaction_hooks() == (True, 'pre-commit, pre-push in 1 repos')`` — the one case
        that was green. A body this build no longer writes is now named instead, on both."""
        from asf import hooks
        self.pin_it()
        self.write_hooks(self.dispatcher)
        pre_commit = os.path.join(hooks.git_hooks_dir(self.repo), 'pre-commit')
        with open(pre_commit, 'w') as f:
            f.write(self._old_body('pre-commit', self.dispatcher))
        want = (f'{pre_commit} is an older body than this build writes — it does not check that '
                f'the asf it runs is the pin')
        self.assertEqual(hooks.verify(self.product), [want])
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertFalse(ok)
        self.assertIn('is an older body than this build writes', detail)
        self.assertIn(pre_commit, detail)
        with open(pre_commit, 'w') as f:
            f.write(hooks._git_hook_body('pre-commit', self.dispatcher, 'sample'))
        self.assertEqual(hooks.verify(self.product), [])
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertTrue(ok, detail)

    def test_every_other_verify_line_and_the_cli_dispatcher_row_are_unmoved(self):
        """Step 1's unmoved set: missing, foreign, execs-not-dispatcher and falls-back-to-PATH are
        each still exactly what `verify` said before this Task."""
        from asf import hooks
        self.pin_it()
        d = hooks.git_hooks_dir(self.repo)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'pre-commit'), 'w') as f:
            f.write('#!/bin/sh\necho mine\n')
        os.chmod(os.path.join(d, 'pre-commit'), 0o755)
        problems = hooks.verify(self.product)
        self.assertIn(f"{os.path.join(d, 'pre-commit')} is not asf's", problems)
        self.assertIn(f"{os.path.join(d, 'pre-push')} missing", problems)

    def test_an_unpinned_product_is_judged_on_presence_alone(self):
        self.write_hooks(os.path.join(self.shared, 'bin', 'asf'))
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertTrue(ok, detail)


class RedactionHooksApproveRemedyTests(PinFixture):
    """S-76255 Step 3: the remedy `check_redaction_hooks` names gains ``--approve`` exactly when
    the problem hook's own path is one the approval matrix withholds (P8) — a tracked
    ``.githooks`` as ``core.hooksPath`` (``human-now`` by default, no matrix config needed), never
    the default, untracked ``.git/hooks``."""

    def setUp(self):
        super().setUp()
        import subprocess
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.product = env.Product('sample', {'repo_dir': self.repo, 'ci': {'provider': 'none'}})
        self.dispatcher = os.path.join(self.tmp, 'bin', 'asf')
        os.makedirs(os.path.dirname(self.dispatcher))
        rc, detail = dispatch.install(path=self.dispatcher, asf_home=self.home, venvs=self.venvs,
                                      default_product='', cli=os.path.join(self.shared, 'bin', 'asf'))
        self.assertEqual(rc, 0, detail)

    def hooks_dir(self, tracked):
        import subprocess
        from asf import hooks
        if tracked:
            subprocess.run(['git', '-C', self.repo, 'config', 'core.hooksPath', '.githooks'],
                           check=True)
        d = hooks.git_hooks_dir(self.repo)
        os.makedirs(d, exist_ok=True)
        return d

    def test_a_tracked_withheld_hook_gets_approve_in_the_remedy(self):
        """No hook written yet: ``missing``, and ``plan``'s action for it is ``write`` — the
        matrix withholds a tracked ``.githooks`` write by default (``human-now``), so the remedy
        is the ``--approve`` form (P8)."""
        self.pin_it()
        d = self.hooks_dir(tracked=True)
        from asf import hooks
        self.assertTrue(hooks.is_tracked(os.path.join(d, 'pre-commit')))
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertFalse(ok)
        self.assertIn('asf hooks install --product sample --approve', detail)

    def test_an_untracked_hook_gets_no_approve_in_the_remedy(self):
        self.pin_it()
        d = self.hooks_dir(tracked=False)
        from asf import hooks
        self.assertFalse(hooks.is_tracked(os.path.join(d, 'pre-commit')))
        ok, detail = doctor.check_redaction_hooks(self.product)
        self.assertFalse(ok)
        self.assertIn('asf hooks install --product sample', detail)
        self.assertNotIn('--approve', detail)


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
