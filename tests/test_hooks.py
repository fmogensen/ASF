"""``asf hooks install`` with the CLI dispatcher (asf.dispatch): it writes the dispatcher first and
every hook it writes then names the dispatcher, never a venv behind it. All under a temp dir —
never the operator's ``~/.local/bin/asf`` or a worker account's real settings."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import dispatch, env, hooks
from asf.env import Product


def _venv_cli(base, name):
    cli = os.path.join(base, name, 'bin', 'asf')
    os.makedirs(os.path.dirname(cli))
    with open(cli, 'w') as f:
        f.write('#!/bin/sh\nexit 0\n')
    os.chmod(cli, 0o755)
    return cli


class HooksInstallWritesTheDispatcher(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-hooks-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.product = Product('demo', env.loads(f'product: demo\nrepo_dir: {self.repo}\n'))
        self.account = os.path.join(self.tmp, 'accounts', 'a')
        self.cfg = {'worker_pool': {'accounts': [{'name': 'a', 'config_dir': self.account}]}}
        self.dispatcher = dispatch.default_path(os.path.join(self.tmp, 'home'))
        self.pinned = _venv_cli(self.tmp, 'pinned')
        state = os.path.join(env.ASF_HOME, 'state', 'demo')
        os.makedirs(state, exist_ok=True)
        self.addCleanup(shutil.rmtree, state, True)
        with open(os.path.join(state, 'install.json'), 'w') as f:
            json.dump({'sha': 'a' * 40, 'venv': os.path.dirname(os.path.dirname(self.pinned))}, f)
        self.shared = _venv_cli(self.tmp, 'shared')

    def install(self):
        orig = dispatch.install

        def pinned_default(path, **kw):  # the default product is the one pinned above
            kw.setdefault('default_product', 'demo')
            return orig(path, **kw)
        with mock.patch.object(dispatch, 'install', pinned_default):
            return hooks.install(self.product, rules_dir=os.path.join(self.tmp, 'none'),
                                 which=lambda n: self.shared, cfg=self.cfg,
                                 dispatcher=self.dispatcher)

    def commands(self):
        with open(os.path.join(self.account, 'settings.json')) as f:
            data = json.load(f)
        return [h['command'] for groups in data['hooks'].values() for g in groups
                for h in g['hooks']]

    def test_every_hook_names_the_dispatcher(self):
        rc, msg = self.install()
        self.assertEqual(rc, 0, msg)
        self.assertTrue(dispatch.is_ours(self.dispatcher))
        self.assertEqual(dispatch.baked(self.dispatcher, 'DEFAULT_CLI'), self.pinned)
        self.assertIn('dispatcher:', msg)
        self.assertEqual(sorted(self.commands()), sorted(
            [f'{self.dispatcher} hook approvals', f'{self.dispatcher} hook unpushed']))
        pre_push = os.path.join(hooks.git_hooks_dir(self.repo), 'pre-push')
        with open(pre_push) as f:
            self.assertIn(f'exec "{self.dispatcher}" redact --pre-push --product demo', f.read())

    def test_a_foreign_file_is_refused_and_every_other_hook_still_written(self):
        os.makedirs(os.path.dirname(self.dispatcher))
        with open(self.dispatcher, 'w') as f:
            f.write('#!/bin/sh\necho mine\n')
        rc, msg = self.install()
        self.assertEqual(rc, 2, msg)
        self.assertIn('NEEDS OPERATOR', msg)
        self.assertIn('is not asf\'s dispatcher', msg)
        with open(self.dispatcher) as f:
            self.assertEqual(f.read(), '#!/bin/sh\necho mine\n')
        self.assertIn(f'{self.shared} hook approvals', self.commands())

    def test_a_live_shared_link_is_replaced_and_kept_beside_it(self):
        """F-0283: while the shared install's link held the path, every hook ran that install
        and never the pin. The link is moved aside, the dispatcher written, every hook names it."""
        os.makedirs(os.path.dirname(self.dispatcher))
        os.symlink(self.shared, self.dispatcher)
        rc, msg = self.install()
        self.assertEqual(rc, 0, msg)
        self.assertTrue(dispatch.is_ours(self.dispatcher))
        self.assertEqual(os.readlink(self.dispatcher + dispatch.LINK_BACKUP), self.shared)
        self.assertIn(f'{self.dispatcher} hook approvals', self.commands())
        self.assertEqual(hooks.verify(self.product), [])

    def test_a_pinned_product_whose_hooks_would_not_name_the_dispatcher_is_refused(self):
        rc, msg = hooks.install(self.product, rules_dir=os.path.join(self.tmp, 'none'),
                                which=lambda n: self.shared, cfg=self.cfg)
        self.assertEqual(rc, 2, msg)
        self.assertIn('NEEDS OPERATOR: demo is pinned', msg)
        self.assertFalse(os.path.lexists(self.dispatcher))
        self.assertNotIn('dispatcher:', msg)
        self.assertTrue(any('not the dispatcher' in line for line in hooks.verify(self.product)))

    def test_an_older_hook_of_asfs_own_is_moved_onto_the_dispatcher(self):
        """The body before F-0283 fell back to asf on PATH and named the shared install: a hook
        asf wrote itself is rewritten; one an operator wrote is never touched."""
        hooks_dir = hooks.git_hooks_dir(self.repo)
        os.makedirs(hooks_dir, exist_ok=True)
        old = hooks._git_hook_body('pre-push', self.shared, 'demo').replace(
            '# no asf here', 'if command -v asf >/dev/null 2>&1; then exec asf redact '
            '--pre-push --product demo; fi\n# no asf here')
        with open(os.path.join(hooks_dir, 'pre-push'), 'w') as f:
            f.write(old)
        self.assertTrue(any('not the dispatcher' in line for line in hooks.verify(self.product)))
        rc, msg = self.install()
        self.assertEqual(rc, 0, msg)
        with open(os.path.join(hooks_dir, 'pre-push')) as f:
            text = f.read()
        self.assertEqual(text, hooks._git_hook_body('pre-push', self.dispatcher, 'demo'))
        self.assertEqual(hooks.verify(self.product), [])

    def test_the_hook_body_never_falls_back_to_asf_on_path(self):
        for name in hooks.GIT_HOOK_NAMES:
            body = hooks._git_hook_body(name, self.dispatcher, 'demo')
            self.assertNotIn('command -v asf', body)
            self.assertTrue(hooks.is_git_hook_ours(body, name))


class PrePushHookTests(unittest.TestCase):
    """``hooks.pre_push_hook`` (F-0235): whether a repo's ``pre-push`` exists, whether asf wrote
    it, and its path — the fact ``asf.briefs.facts.repo_facts`` carries into every brief."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-pre-push-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)

    def _write_hook(self, text):
        hooks_dir = hooks.git_hooks_dir(self.repo)
        os.makedirs(hooks_dir, exist_ok=True)
        path = os.path.join(hooks_dir, 'pre-push')
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def test_asfs_own_hook_is_present_and_ours(self):
        # written through _git_hook_body, never a hand-copied string: a body that drifts from
        # what asf actually writes is a test that passes while the product breaks
        path = self._write_hook(hooks._git_hook_body('pre-push', '/opt/p/bin/asf', 'demo'))
        self.assertEqual(hooks.pre_push_hook(self.repo), (True, True, path))

    def test_a_foreign_hook_is_present_and_not_ours(self):
        path = self._write_hook('#!/bin/sh\necho mine\n')
        self.assertEqual(hooks.pre_push_hook(self.repo), (True, False, path))

    def test_no_hook_at_all(self):
        self.assertEqual(hooks.pre_push_hook(self.repo), (False, False, ''))

    def test_not_a_git_repo_returns_the_same_triple_and_does_not_raise(self):
        not_repo = os.path.join(self.tmp, 'not-a-repo')
        os.makedirs(not_repo)
        self.assertEqual(hooks.pre_push_hook(not_repo), (False, False, ''))


if __name__ == '__main__':
    unittest.main()
