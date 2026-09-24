"""An isolated worker session gets its credentials explicitly: ``worker_pool.accounts[].auth_env``
maps a variable to a file, and at spawn each file's content becomes that variable in that
account's sessions — never HOME, never the keychain. A missing file refuses the launch
(NEEDS OPERATOR), a ``GH_TOKEN`` gives git an HTTPS credential for the code host with nothing
written to disk, no value reaches a log ASF writes, and the redaction gate knows every value."""
import json
import os
import subprocess
import unittest

from asf import doctor, env, redact
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod

from tests.test_worker_isolation import IsolatedSession

OAUTH = 'sk-ant-oat01-' + 'Q' * 40
GH = 'github_pat_' + 'Z' * 60


class AuthEnvSession(IsolatedSession):
    def setUp(self):
        super().setUp()
        self.secrets = os.path.join(self.tmp, 'secrets')
        os.makedirs(self.secrets)
        self.token_file = self.secret('acct-a.token', OAUTH + '\n')
        self.gh_file = self.secret('acct-a.gh', '  ' + GH + '\n')

    def secret(self, name, text):
        path = os.path.join(self.secrets, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def account(self, **auth):
        return pool_mod.Account('acct-a', config_dir='/cfg/acct-a', auth_env=auth or {
            'CLAUDE_CODE_OAUTH_TOKEN': self.token_file, 'GH_TOKEN': self.gh_file})

    def product_with_auth_env(self, **auth):
        return env.Product('sample', dict(self.product._data, conventions={'auth_env': auth}))

    def test_auth_env_values_reach_the_child_stripped_and_only_its_accounts(self):
        _rec, seen = self.spawn(self.account())
        self.assertEqual(seen['CLAUDE_CODE_OAUTH_TOKEN'], OAUTH)
        self.assertEqual(seen['GH_TOKEN'], GH)
        os.remove(self.dump)
        _rec, other = self.spawn(pool_mod.Account('acct-b', config_dir='/cfg/acct-b'), job='j2')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', other)
        self.assertNotIn('GH_TOKEN', other)

    def test_no_value_reaches_the_ledger_the_brief_or_the_setup_log(self):
        product = env.Product('sample', dict(self.product._data, conventions={
            'worktree_setup': 'echo "token=$CLAUDE_CODE_OAUTH_TOKEN"; echo "gh=$GH_TOKEN" >&2'}))
        rec, seen = self.spawn(self.account(), product=product)
        self.assertEqual(seen['GH_TOKEN'], GH)   # it reached the child…
        with open(spawn_mod.setup_log_path(product, 'j1'), encoding='utf-8') as f:
            log = f.read()
        self.assertIn('token=[redacted:CLAUDE_CODE_OAUTH_TOKEN]', log)   # …and not the log
        self.assertIn('gh=[redacted:GH_TOKEN]', log)
        written = json.dumps(rec) + json.dumps(pool_mod.load_sessions(product))
        with open(rec['brief'], encoding='utf-8') as f:
            written += f.read()
        with open(rec['log'], encoding='utf-8') as f:
            written += f.read()
        for value in (OAUTH, GH):
            self.assertNotIn(value, log)
            self.assertNotIn(value, written)

    def test_the_redaction_gate_knows_every_auth_env_value(self):
        cfg = {'worker_pool': {'accounts': [
            {'name': 'acct-a', 'auth_env': {'CLAUDE_CODE_OAUTH_TOKEN': self.token_file,
                                            'MY_THING': self.secret('x', 'plainvalue-1234')}},
            {'name': 'acct-b', 'auth_env': {'GH_TOKEN': os.path.join(self.secrets, 'absent')}}]}}
        pats = redact.patterns(cfg=cfg, environ={})
        findings = redact.scan_text('notes.txt', f'a {OAUTH} b\nc plainvalue-1234 d\n', pats)
        sources = {f.source for f in findings}
        self.assertIn('auth_env:CLAUDE_CODE_OAUTH_TOKEN', sources)
        self.assertIn('auth_env:MY_THING', sources)   # whatever the variable is called
        for f in findings:
            self.assertNotIn(OAUTH, repr(f))
            self.assertNotIn('plainvalue-1234', repr(f))

    def test_a_missing_file_refuses_the_launch_naming_the_file_and_the_command(self):
        missing = os.path.join(self.secrets, 'acct-a.nope')
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.spawn(self.account(CLAUDE_CODE_OAUTH_TOKEN=missing))
        msg = str(cm.exception)
        self.assertTrue(msg.startswith('NEEDS OPERATOR'), msg)
        self.assertIn(missing, msg)
        self.assertIn('claude setup-token', msg)
        self.assertIn('claude setup-token', cm.exception.clear)
        self.assertFalse(os.path.exists(os.path.join(spawn_mod.worktrees_dir(self.product), 'j1')))
        self.assertEqual(pool_mod.load_sessions(self.product), {})
        self.assertFalse(os.path.exists(self.dump))    # no session started

    def test_an_empty_file_refuses_too_and_never_prints_a_value(self):
        empty = self.secret('acct-a.gh-empty', '\n')
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.spawn(self.account(CLAUDE_CODE_OAUTH_TOKEN=self.token_file, GH_TOKEN=empty))
        self.assertIn('GH_TOKEN', str(cm.exception))
        self.assertIn('is empty', str(cm.exception))
        self.assertNotIn(OAUTH, str(cm.exception))

    def test_gh_token_gives_git_an_https_credential_without_the_keychain(self):
        _rec, seen = self.spawn(self.account())
        n = int(seen['GIT_CONFIG_COUNT'])
        pairs = [(seen[f'GIT_CONFIG_KEY_{i}'], seen[f'GIT_CONFIG_VALUE_{i}']) for i in range(n)]
        key = 'credential.https://github.com.helper'
        self.assertIn((key, ''), pairs)                 # every other helper (keychain) reset
        self.assertIn((key, runtime_mod.GIT_CREDENTIAL_HELPER), pairs)
        self.assertLess(pairs.index((key, '')), pairs.index((key, runtime_mod.GIT_CREDENTIAL_HELPER)))
        self.assertNotIn(GH, json.dumps(pairs))        # the token is read from the env, not config
        p = subprocess.run(['git', 'credential', 'fill'], env=dict(seen, GIT_TERMINAL_PROMPT='0'),
                           input='protocol=https\nhost=github.com\npath=o/r.git\n\n',
                           capture_output=True, text=True, cwd=seen['HOME'], timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('username=x-access-token\n', p.stdout)
        self.assertIn(f'password={GH}\n', p.stdout)
        # nothing written to the home: its only file is ASF's identity-only .gitconfig
        # beside it, at most the link to the factory's installed CLI (hotfix cd1adb0)
        self.assertEqual(sorted(set(os.listdir(seen['HOME'])) - {'.local'}), ['.gitconfig'])
        if os.path.lexists(os.path.join(seen['HOME'], '.local')):
            self.assertTrue(os.path.islink(os.path.join(seen['HOME'], '.local', 'bin', 'asf')))
            self.assertEqual(os.listdir(os.path.join(seen['HOME'], '.local', 'bin')), ['asf'])

    def test_no_gh_token_no_credential_config(self):
        _rec, seen = self.spawn(self.account(CLAUDE_CODE_OAUTH_TOKEN=self.token_file))
        self.assertNotIn('credential.', ' '.join(v for k, v in seen.items()
                                                 if k.startswith('GIT_CONFIG_KEY_')))
        self.assertEqual(runtime_mod.git_credential_config({}), [])

    def test_a_seeded_gitconfig_is_left_alone(self):
        acct = pool_mod.Account('acct-a', home_seed=[os.path.join(self.operator_home, '.gitconfig')],
                                auth_env={'CLAUDE_CODE_OAUTH_TOKEN': self.token_file})
        _rec, seen = self.spawn(acct)
        with open(os.path.join(seen['HOME'], '.gitconfig'), encoding='utf-8') as f:
            self.assertEqual(f.read(), '[user]\n\tname = op\n')

    # ---- products/<p>.yaml conventions.auth_env: GitHub access is per product -----------------

    def test_the_products_auth_env_wins_over_the_accounts_for_the_same_variable(self):
        product_gh = self.secret('product.gh', 'other-owner-token-XYZ\n')
        product = self.product_with_auth_env(GH_TOKEN=product_gh)
        _rec, seen = self.spawn(self.account(), product=product)
        self.assertEqual(seen['GH_TOKEN'], 'other-owner-token-XYZ')       # the product's own…
        self.assertEqual(seen['CLAUDE_CODE_OAUTH_TOKEN'], OAUTH)          # …the account's stays

    def test_a_product_only_variable_reaches_the_child_too(self):
        npm_token = self.secret('product.npm', 'npm-secret-1234\n')
        product = self.product_with_auth_env(NPM_TOKEN=npm_token)
        _rec, seen = self.spawn(self.account(), product=product)
        self.assertEqual(seen['NPM_TOKEN'], 'npm-secret-1234')
        self.assertEqual(seen['GH_TOKEN'], GH)                            # the account's, untouched

    def test_a_missing_product_auth_env_file_refuses_the_launch(self):
        missing = os.path.join(self.secrets, 'product.nope')
        product = self.product_with_auth_env(GH_TOKEN=missing)
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.spawn(self.account(), product=product)
        msg = str(cm.exception)
        self.assertTrue(msg.startswith('NEEDS OPERATOR'), msg)
        self.assertIn(missing, msg)
        self.assertIn('GH_TOKEN', msg)
        self.assertFalse(os.path.exists(os.path.join(spawn_mod.worktrees_dir(product), 'j1')))
        self.assertEqual(pool_mod.load_sessions(product), {})
        self.assertFalse(os.path.exists(self.dump))    # no session started

    def test_no_value_from_the_product_reaches_the_ledger_the_brief_or_the_setup_log(self):
        product_gh = self.secret('product.gh', 'other-owner-token-XYZ\n')
        product = env.Product('sample', dict(self.product._data, conventions={
            'auth_env': {'GH_TOKEN': product_gh},
            'worktree_setup': 'echo "gh=$GH_TOKEN"'}))
        rec, seen = self.spawn(self.account(), product=product)
        self.assertEqual(seen['GH_TOKEN'], 'other-owner-token-XYZ')
        with open(spawn_mod.setup_log_path(product, 'j1'), encoding='utf-8') as f:
            log = f.read()
        self.assertIn('gh=[redacted:GH_TOKEN]', log)
        written = json.dumps(rec) + json.dumps(pool_mod.load_sessions(product))
        with open(rec['brief'], encoding='utf-8') as f:
            written += f.read()
        with open(rec['log'], encoding='utf-8') as f:
            written += f.read()
        self.assertNotIn('other-owner-token-XYZ', log)
        self.assertNotIn('other-owner-token-XYZ', written)


class AuthEnvConfig(unittest.TestCase):
    def test_parsed_and_expanded(self):
        acct = {'name': 'a', 'auth_env': {'GH_TOKEN': '~/.ASF/secrets/a.gh'}}
        self.assertEqual(env.account_auth_env(acct),
                         {'GH_TOKEN': os.path.expanduser('~/.ASF/secrets/a.gh')})
        self.assertEqual(env.account_auth_env({'name': 'b'}), {})
        self.assertEqual(pool_mod.Account.from_dict(acct).auth_env,
                         {'GH_TOKEN': os.path.expanduser('~/.ASF/secrets/a.gh')})

    def test_malformed_auth_env_is_named(self):
        for auth, why in (('~/x', 'map variable names to files'),
                          ({'NOT-A-VAR': '~/x'}, "'NOT-A-VAR' is not one"),
                          ({'GH_TOKEN': ''}, 'GH_TOKEN must name a file')):
            problems = env.validate_worker_pool(
                {'worker_pool': {'accounts': [{'name': 'x', 'auth_env': auth}]}})
            self.assertEqual(len(problems), 1, problems)
            self.assertEqual(problems[0][0], 'worker_pool.accounts[x].auth_env')
            self.assertIn(why, problems[0][1])


class AuthEnvDoctor(unittest.TestCase):
    def cfg(self, backend=None, **acct):
        wp = {'accounts': [dict({'name': 'acct-a'}, **acct)]}
        if backend:
            wp['backend'] = backend
        return {'worker_pool': wp}

    def test_an_isolated_account_without_the_runtime_login_is_red(self):
        ok, detail = doctor.check_worker_env(self.cfg())
        self.assertFalse(ok)
        self.assertIn('acct-a', detail)
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN', detail)
        self.assertIn('claude setup-token', detail)
        ok, detail = doctor.check_worker_env(self.cfg(auth_env={'GH_TOKEN': '/s/a.gh'}))
        self.assertFalse(ok, detail)                    # a push token is not a login

    def test_green_with_a_login_or_a_fake_backend(self):
        ok, detail = doctor.check_worker_env(
            self.cfg(auth_env={'CLAUDE_CODE_OAUTH_TOKEN': '/s/a.token'}))
        self.assertTrue(ok, detail)
        ok, detail = doctor.check_worker_env(self.cfg(backend='fake'))
        self.assertTrue(ok, detail)

    def test_the_secrets_row_lists_presence_by_name_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            token = os.path.join(d, 'a.token')
            with open(token, 'w', encoding='utf-8') as f:
                f.write(OAUTH)
            gh = os.path.join(d, 'a.gh')
            ok, detail = doctor.check_worker_secrets(self.cfg(auth_env={
                'CLAUDE_CODE_OAUTH_TOKEN': token, 'GH_TOKEN': gh}))
            self.assertFalse(ok)
            self.assertIn('missing: acct-a:GH_TOKEN', detail)
            self.assertIn(gh, detail)
            self.assertIn('present: acct-a:CLAUDE_CODE_OAUTH_TOKEN', detail)
            self.assertNotIn(OAUTH, detail)
            with open(gh, 'w', encoding='utf-8') as f:
                f.write(GH)
            ok, detail = doctor.check_worker_secrets(self.cfg(auth_env={
                'CLAUDE_CODE_OAUTH_TOKEN': token, 'GH_TOKEN': gh}))
            self.assertTrue(ok, detail)
            self.assertNotIn(GH, detail)
        ok, detail = doctor.check_worker_secrets(self.cfg())
        self.assertTrue(ok)
        self.assertEqual(detail, 'no auth_env files configured')

    def test_the_secrets_row_lists_the_products_own_auth_env_by_name_too(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            gh = os.path.join(d, 'product.gh')
            product = env.Product('sample', {'conventions': {'auth_env': {'GH_TOKEN': gh}}})
            ok, detail = doctor.check_worker_secrets(self.cfg(), product=product)
            self.assertFalse(ok, detail)
            self.assertIn('missing: product:sample:GH_TOKEN', detail)
            self.assertIn(gh, detail)
            with open(gh, 'w', encoding='utf-8') as f:
                f.write(GH)
            ok, detail = doctor.check_worker_secrets(self.cfg(), product=product)
            self.assertTrue(ok, detail)
            self.assertIn('present: product:sample:GH_TOKEN', detail)
            self.assertNotIn(GH, detail)


class ProductAuthEnvConfig(unittest.TestCase):
    """``products/<p>.yaml conventions.auth_env``: parsed under ``conventions`` (so an older
    ``asf`` — which keeps unknown ``conventions`` keys — still loads the file, R23), merged over
    an account's own at spawn (:func:`asf.workers.runtime.auth_env_values`), the product's value
    winning for a variable both name."""

    def product(self, auth):
        return env.Product('sample', {'conventions': {'auth_env': auth}})

    def test_parsed_and_expanded(self):
        product = self.product({'GH_TOKEN': '~/.ASF/secrets/sample.gh'})
        self.assertEqual(env.product_auth_env(product),
                         {'GH_TOKEN': os.path.expanduser('~/.ASF/secrets/sample.gh')})
        self.assertEqual(env.product_auth_env(env.Product('sample', {})), {})
        self.assertEqual(env.product_auth_env(None), {})

    def test_malformed_auth_env_is_named_in_the_conventions_shape_check(self):
        from asf import conventions as conventions_mod
        for auth, why in (('~/x', 'map variable names to files'),
                          ({'NOT-A-VAR': '~/x'}, "'NOT-A-VAR' is not one"),
                          ({'GH_TOKEN': ''}, 'GH_TOKEN must name a file')):
            problems = conventions_mod.validate_mapping({'auth_env': auth})
            self.assertEqual(len(problems), 1, problems)
            self.assertEqual(problems[0][0], 'auth_env')
            self.assertIn(why, problems[0][1])

    def test_merge_precedence_the_products_own_wins(self):
        from asf.workers import runtime as runtime_mod
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            acct_gh = os.path.join(d, 'acct.gh')
            product_gh = os.path.join(d, 'product.gh')
            oauth = os.path.join(d, 'acct.token')
            for path, text in ((acct_gh, 'acct-owner-token'), (product_gh, 'product-owner-token'),
                              (oauth, 'oauth-token')):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
            acct = pool_mod.Account('acct-a', auth_env={
                'GH_TOKEN': acct_gh, 'CLAUDE_CODE_OAUTH_TOKEN': oauth})
            values = runtime_mod.auth_env_values(acct, {'GH_TOKEN': product_gh})
            self.assertEqual(values['GH_TOKEN'], 'product-owner-token')          # the product wins
            self.assertEqual(values['CLAUDE_CODE_OAUTH_TOKEN'], 'oauth-token')   # the account's stays
            # no product override at all: the account's own is unaffected
            self.assertEqual(runtime_mod.auth_env_values(acct, {})['GH_TOKEN'], 'acct-owner-token')
            self.assertEqual(runtime_mod.auth_env_values(acct, None)['GH_TOKEN'], 'acct-owner-token')


class ProductAuthEnvRedaction(unittest.TestCase):
    """The redaction gate reads every configured product's own ``conventions.auth_env`` file
    too, on disk under ``ASF_HOME/products/`` — GitHub access is per product, so a product's own
    token must never leak either."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='asf-product-auth-env-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(setattr, env, 'ASF_HOME', self._home)
        os.makedirs(os.path.join(self.tmp, 'products'))
        self.gh_file = os.path.join(self.tmp, 'product.gh')
        with open(self.gh_file, 'w', encoding='utf-8') as f:
            f.write(GH + '\n')
        with open(os.path.join(self.tmp, 'products', 'sample.yaml'), 'w', encoding='utf-8') as f:
            f.write('product: sample\n'
                    'repo_dir: /tmp/nowhere\n'
                    'conventions:\n'
                    f'  auth_env:\n    GH_TOKEN: {self.gh_file}\n')

    def test_the_redaction_gate_knows_the_products_auth_env_value_too(self):
        pairs = redact.auth_env_secrets({})
        self.assertIn(('GH_TOKEN', GH), pairs)
        pats = redact.patterns(cfg={}, environ={})
        findings = redact.scan_text('notes.txt', f'a {GH} b\n', pats)
        self.assertTrue(any(f.source == 'auth_env:GH_TOKEN' for f in findings))
        for f in findings:
            self.assertNotIn(GH, repr(f))


if __name__ == '__main__':
    unittest.main()
