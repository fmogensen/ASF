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

    def test_a_live_shared_link_keeps_todays_behaviour(self):
        os.makedirs(os.path.dirname(self.dispatcher))
        os.symlink(self.shared, self.dispatcher)
        rc, msg = self.install()
        self.assertEqual(rc, 0, msg)
        self.assertEqual(os.readlink(self.dispatcher), self.shared)
        self.assertIn(f'{self.shared} hook approvals', self.commands())

    def test_without_a_dispatcher_path_nothing_is_written_there(self):
        rc, msg = hooks.install(self.product, rules_dir=os.path.join(self.tmp, 'none'),
                                which=lambda n: self.shared, cfg=self.cfg)
        self.assertEqual(rc, 0, msg)
        self.assertFalse(os.path.lexists(self.dispatcher))
        self.assertNotIn('dispatcher', msg)


if __name__ == '__main__':
    unittest.main()
