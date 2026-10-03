"""The CLI dispatcher (asf.dispatch): the script is written into a temp HOME and executed, with
two products pinned to fake venvs whose ``bin/asf`` prints its own venv name — never the
operator's ``~/.local/bin/asf``, ``~/.ASF`` or a real venv."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import dispatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMOKE = os.path.join(ROOT, 'tools', 'hook-smoke.sh')
ZERO = '0' * 40


def fake_venv(base, name):
    """A venv dir whose ``bin/asf`` prints ``<name> <args>`` and drains stdin."""
    venv = os.path.join(base, name)
    os.makedirs(os.path.join(venv, 'bin'))
    cli = os.path.join(venv, 'bin', 'asf')
    with open(cli, 'w') as f:
        f.write(f'#!/bin/sh\ncat >/dev/null\necho "{name} $*"\n')
    os.chmod(cli, 0o755)
    return venv


class Fixture(unittest.TestCase):
    """A temp HOME with an asf home holding products ``alpha`` (the default) and ``beta``, each
    with a repo dir and a pin, and a dispatcher written at ``<HOME>/.local/bin/asf``."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-dispatch-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.asf_home = os.path.join(self.home, '.ASF')
        self.venvs = os.path.join(self.tmp, 'venvs')
        os.makedirs(os.path.join(self.asf_home, 'products'))
        self.repos = {}
        for name in ('alpha', 'beta'):
            repo = os.path.join(self.tmp, 'repos', name)
            os.makedirs(repo)
            self.repos[name] = repo
            with open(os.path.join(self.asf_home, 'products', f'{name}.yaml'), 'w') as f:
                f.write(f'product: {name}\nrepo_dir: {repo}\n')
        self.shared = fake_venv(self.venvs, 'shared')
        self.pin('alpha', fake_venv(self.venvs, 'alpha-1111111'))
        self.pin('beta', fake_venv(self.venvs, 'beta-2222222'), previous=self.shared)
        self.path = dispatch.default_path(self.home)
        rc, detail = self.write()
        self.assertEqual(rc, 0, detail)

    def write(self, **kw):
        kw.setdefault('asf_home', self.asf_home)
        kw.setdefault('venvs', self.venvs)
        kw.setdefault('default_product', 'alpha')
        return dispatch.install(self.path, **kw)

    def pin(self, product, venv, previous=None, indent=2):
        state = os.path.join(self.asf_home, 'state', product)
        os.makedirs(state, exist_ok=True)
        rec = {'sha': 'f' * 40, 'venv': venv,
               'previous': {'sha': 'e' * 40, 'venv': previous} if previous else None,
               'at': '2026-10-03T00:00:00Z', 'by': 'test', 'policy': 'pinned'}
        if previous:  # the record's key order is not the dispatcher's business
            rec = {'previous': rec.pop('previous'), **rec}
        with open(os.path.join(state, 'install.json'), 'w') as f:
            json.dump(rec, f, indent=indent)

    def run_asf(self, *args, cwd=None, env_extra=None, path=None):
        env = {'HOME': self.home, 'PATH': '/usr/bin:/bin', 'ASF_DISPATCH_TRACE': '1',
               'PYTHONPATH': ROOT}
        env.update(env_extra or {})
        return subprocess.run([path or self.path, *args], cwd=cwd or self.tmp, env=env,
                              capture_output=True, text=True, input='{}')


class Resolution(Fixture):

    def test_product_flag_picks_that_products_pin(self):
        p = self.run_asf('hook', 'approvals', '--product', 'beta')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), 'beta-2222222 hook approvals --product beta')
        self.assertIn('from=--product via=pin', p.stderr)

    def test_product_equals_form_and_the_last_flag_wins(self):
        p = self.run_asf('land', '--product=alpha', '--product', 'beta')
        self.assertTrue(p.stdout.startswith('beta-2222222 '), p.stdout + p.stderr)
        p = self.run_asf('land', '--product=beta')
        self.assertTrue(p.stdout.startswith('beta-2222222 '), p.stdout + p.stderr)

    def test_asf_product_env_picks_that_products_pin(self):
        p = self.run_asf('hook', 'unpushed', env_extra={'ASF_PRODUCT': 'beta'})
        self.assertEqual(p.stdout.strip(), 'beta-2222222 hook unpushed')
        self.assertIn('from=$ASF_PRODUCT', p.stderr)

    def test_the_flag_beats_the_env(self):
        p = self.run_asf('set', '--product', 'alpha', env_extra={'ASF_PRODUCT': 'beta'})
        self.assertTrue(p.stdout.startswith('alpha-1111111 '), p.stdout + p.stderr)

    def test_cwd_in_a_products_repo_picks_its_pin(self):
        sub = os.path.join(self.repos['beta'], 'src')
        os.makedirs(sub)
        p = self.run_asf('hook', 'approvals', cwd=sub)
        self.assertEqual(p.stdout.strip(), 'beta-2222222 hook approvals', p.stderr)
        self.assertIn('product=beta from=cwd via=pin', p.stderr)

    def test_cwd_in_a_products_state_worktree_picks_its_pin(self):
        wt = os.path.join(self.asf_home, 'state', 'beta', 'worktrees', 'T-1')
        os.makedirs(wt)
        p = self.run_asf('hook', 'approvals', cwd=wt)
        self.assertEqual(p.stdout.strip(), 'beta-2222222 hook approvals', p.stderr)

    def test_no_product_falls_back_to_the_default_products_pin(self):
        p = self.run_asf('status')
        self.assertEqual(p.stdout.strip(), 'alpha-1111111 status', p.stderr)
        self.assertIn('product=none from=none via=default-pin', p.stderr)

    def test_a_product_with_no_install_record_falls_back_to_the_default(self):
        p = self.run_asf('status', '--product', 'gamma')
        self.assertEqual(p.stdout.strip(), 'alpha-1111111 status --product gamma', p.stderr)

    def test_a_pin_whose_venv_is_gone_falls_back_to_the_default(self):
        shutil.rmtree(os.path.join(self.venvs, 'beta-2222222'))
        p = self.run_asf('status', '--product', 'beta')
        self.assertTrue(p.stdout.startswith('alpha-1111111 '), p.stdout + p.stderr)

    def test_with_no_pin_at_all_the_baked_default_answers(self):
        shutil.rmtree(os.path.join(self.asf_home, 'state'))
        p = self.run_asf('status', '--product', 'beta')
        self.assertTrue(p.stdout.startswith('alpha-1111111 '), p.stdout + p.stderr)
        self.assertIn('via=default-cli', p.stderr)

    def test_nothing_to_run_exits_127_naming_the_product(self):
        shutil.rmtree(os.path.join(self.asf_home, 'state'))
        shutil.rmtree(os.path.join(self.venvs, 'alpha-1111111'))
        p = self.run_asf('status', '--product', 'beta')
        self.assertEqual(p.returncode, 127)
        self.assertIn("product 'beta'", p.stderr)

    def test_previous_is_never_taken_in_either_layout(self):
        for indent in (2, None):
            self.pin('beta', os.path.join(self.venvs, 'beta-2222222'), previous=self.shared,
                     indent=indent)
            p = self.run_asf('x', '--product', 'beta')
            self.assertTrue(p.stdout.startswith('beta-2222222 '), (indent, p.stdout, p.stderr))

    def test_a_bare_venv_name_resolves_under_the_venvs_dir(self):
        self.pin('beta', 'beta-2222222')
        p = self.run_asf('x', '--product', 'beta')
        self.assertTrue(p.stdout.startswith('beta-2222222 '), p.stdout + p.stderr)
        self.assertEqual(dispatch.pinned_cli('beta', self.asf_home, self.venvs),
                         os.path.join(self.venvs, 'beta-2222222', 'bin', 'asf'))

    def test_a_product_name_never_escapes_the_state_dir(self):
        p = self.run_asf('x', '--product', '../beta')
        self.assertTrue(p.stdout.startswith('alpha-1111111 '), p.stdout + p.stderr)

    def test_an_asf_home_in_the_env_beats_the_baked_one(self):
        other = os.path.join(self.tmp, 'other-home')
        state = os.path.join(other, 'state', 'beta')
        os.makedirs(state)
        with open(os.path.join(state, 'install.json'), 'w') as f:
            json.dump({'venv': self.shared}, f)
        p = self.run_asf('x', '--product', 'beta', env_extra={'ASF_HOME': other})
        self.assertTrue(p.stdout.startswith('shared '), p.stdout + p.stderr)

    def test_a_session_home_link_reaches_the_same_pin(self):
        from asf.workers import runtime
        session = os.path.join(self.tmp, 'session-home')
        os.makedirs(session)
        link = runtime.link_factory_cli(session, self.home)
        p = self.run_asf('hook', 'approvals', path=link, env_extra={'HOME': session,
                                                                    'ASF_PRODUCT': 'beta'})
        self.assertEqual(p.stdout.strip(), 'beta-2222222 hook approvals', p.stderr)


class Install(Fixture):

    def test_the_script_is_posix_sh_with_the_marker(self):
        with open(self.path) as f:
            text = f.read()
        self.assertTrue(text.startswith('#!/bin/sh\n' + dispatch.MARKER))
        self.assertTrue(os.access(self.path, os.X_OK))
        if shutil.which('dash'):
            p = subprocess.run(['dash', '-n', self.path], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run(['sh', '-n', self.path], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_the_baked_default_is_the_default_products_pin(self):
        self.assertEqual(dispatch.baked(self.path, 'DEFAULT_CLI'),
                         os.path.join(self.venvs, 'alpha-1111111', 'bin', 'asf'))
        self.assertEqual(dispatch.baked(self.path, 'HOME'), self.asf_home)

    def test_a_rewrite_of_its_own_file_is_idempotent(self):
        rc, detail = self.write()
        self.assertEqual(rc, 0)
        self.assertIn('current', detail)

    def test_a_foreign_file_is_refused_and_left_untouched(self):
        with open(self.path, 'w') as f:
            f.write('#!/bin/sh\necho mine\n')
        rc, detail = self.write()
        self.assertEqual(rc, 2)
        self.assertIn('NEEDS OPERATOR', detail)
        with open(self.path) as f:
            self.assertEqual(f.read(), '#!/bin/sh\necho mine\n')

    def test_a_live_pipx_link_is_left_alone(self):
        os.remove(self.path)
        target = os.path.join(self.shared, 'bin', 'asf')
        os.symlink(target, self.path)
        rc, detail = self.write()
        self.assertEqual(rc, 0, detail)
        self.assertIn('not written', detail)
        self.assertEqual(os.readlink(self.path), target)

    def test_a_dangling_link_is_replaced(self):
        os.remove(self.path)
        os.symlink(os.path.join(self.tmp, 'gone', 'bin', 'asf'), self.path)
        rc, detail = self.write()
        self.assertEqual(rc, 0, detail)
        self.assertTrue(dispatch.is_ours(self.path))

    def test_no_fallback_cli_refuses(self):
        os.remove(self.path)
        shutil.rmtree(os.path.join(self.asf_home, 'state'))
        with mock.patch.object(sys, 'prefix', sys.base_prefix):  # no venv runs this process
            rc, detail = self.write(default_product='nobody')
        self.assertEqual(rc, 2)
        self.assertIn('NEEDS OPERATOR', detail)


class SessionLink(unittest.TestCase):

    def test_session_cli_link_names_the_dispatcher(self):
        from asf.workers import runtime
        with tempfile.TemporaryDirectory() as d:
            op = os.path.join(d, 'op')
            venv = fake_venv(d, 'v')
            path = dispatch.default_path(op)
            rc, detail = dispatch.install(path, asf_home=os.path.join(op, '.ASF'),
                                          venvs=d, default_product='',
                                          cli=os.path.join(venv, 'bin', 'asf'))
            self.assertEqual(rc, 0, detail)
            home = os.path.join(d, 'session')
            os.makedirs(home)
            link = runtime.link_factory_cli(home, op)
            self.assertEqual(os.readlink(link), path)
            self.assertTrue(dispatch.is_ours(os.path.realpath(link)))


class HookSmoke(Fixture):

    def smoke(self, product='beta', default_dir=False):
        env = {'HOME': self.home, 'PATH': '/usr/bin:/bin', 'ASF_HOME': self.asf_home,
               'PYTHONPATH': ROOT, 'ASF_DISPATCHER': self.path,
               'ASF_JOB': 'must-be-dropped', 'ASF_PRODUCT': 'must-be-dropped'}
        args = [] if default_dir else [self.repos[product]]
        return subprocess.run(['bash', SMOKE, product, *args], env=env,
                              capture_output=True, text=True)

    def test_with_no_dir_an_empty_leftover_worktree_is_passed_over_for_the_repo_dir(self):
        os.makedirs(os.path.join(self.asf_home, 'state', 'beta', 'worktrees', 'leftover'))
        p = self.smoke(default_dir=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"hook-smoke: beta from {self.repos['beta']}\n", p.stdout)
        self.assertEqual(p.stdout.count('beta-2222222'), 3, p.stdout)

    def test_the_smoke_script_answers_from_the_pin(self):
        p = self.smoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.count('beta-2222222'), 3, p.stdout)

    def agent_home(self, name):
        home = os.path.join(self.asf_home, 'state', 'homes', name)
        os.makedirs(home)
        return home

    def test_the_smoke_script_runs_the_pre_push_under_every_agent_home(self):
        from asf.workers import runtime
        for name in ('agent-a', 'agent-b'):
            self.assertIsNotNone(runtime.link_factory_cli(self.agent_home(name), self.home))
        p = self.smoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.count('beta-2222222'), 5, p.stdout)
        self.assertIn('pre-push@home:agent-a', p.stdout)
        self.assertIn('pre-push@home:agent-b', p.stdout)

    def test_the_smoke_script_fails_on_an_agent_home_whose_asf_dangles(self):
        home = self.agent_home('agent-a')
        os.makedirs(os.path.join(home, '.local', 'bin'))
        os.symlink(os.path.join(self.tmp, 'gone-venv', 'bin', 'asf'),
                   os.path.join(home, '.local', 'bin', 'asf'))
        p = self.smoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('FAIL  pre-push@home:agent-a', p.stdout)

    def test_the_smoke_script_fails_when_the_default_answers(self):
        shutil.rmtree(os.path.join(self.venvs, 'beta-2222222'))
        p = self.smoke()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('FAIL', p.stdout)


if __name__ == '__main__':
    unittest.main()
