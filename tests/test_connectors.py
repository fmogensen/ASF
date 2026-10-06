"""``asf.connectors``: the registry (``connectors.<kind>``, legacy keys, entry points, the command
form), the Protocols, and each kind's default moved behind it with no behaviour change."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import connectors, env, github
from asf.connectors import command as command_mod
from asf.connectors import fakes, protocols
from asf.connectors import quota as quota_conn
from asf.connectors import secrets as secrets_conn
from asf.connectors.ci_github import GitHubActionsCI
from asf.connectors.github_forge import GitHubForge
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod


class RegistryCase(unittest.TestCase):
    def setUp(self):
        connectors.reset()
        self.addCleanup(connectors.reset)


class ConfiguredTest(RegistryCase):
    def test_unset_kinds_are_their_defaults(self):
        for kind in connectors.KINDS:
            self.assertEqual(connectors.configured(kind, {}),
                             (connectors.DEFAULTS[kind], 'default', None))

    def test_a_name_is_read_from_connectors_kind(self):
        self.assertEqual(connectors.configured('forge', {'connectors': {'forge': 'fake'}})[:2],
                         ('fake', 'connectors.forge'))

    def test_a_mapping_with_command_is_the_command_form(self):
        cfg = {'connectors': {'forge': {'command': 'bridge'}}}
        self.assertEqual(connectors.configured('forge', cfg)[0], connectors.COMMAND)

    def test_a_mapping_without_command_or_name_is_an_error(self):
        with self.assertRaises(connectors.ConnectorError):
            connectors.configured('forge', {'connectors': {'forge': {'timeout_s': 3}}})

    def test_an_unknown_kind_is_an_error(self):
        with self.assertRaises(connectors.ConnectorError):
            connectors.configured('telepathy', {})

    def test_the_older_quota_command_still_chooses_the_command_form(self):
        cfg = {'worker_pool': {'quota_command': 'q {account}'}}
        self.assertEqual(connectors.configured('quota', cfg)[:2],
                         ('command', 'worker_pool.quota_command'))

    def test_connectors_kind_wins_over_the_legacy_key(self):
        cfg = {'connectors': {'quota': 'none'}, 'worker_pool': {'quota_command': 'q'}}
        self.assertEqual(connectors.configured('quota', cfg)[0], 'none')


class ResolveTest(RegistryCase):
    def test_the_default_forge_is_github(self):
        f = connectors.forge({})
        self.assertIsInstance(f, GitHubForge)
        self.assertIsInstance(f, protocols.Forge)

    def test_an_unknown_name_is_an_error_never_the_default(self):
        with self.assertRaises(connectors.ConnectorError) as cm:
            connectors.get('forge', {'connectors': {'forge': 'nowhere'}})
        self.assertIn('asf.connectors.forge:nowhere', str(cm.exception))

    def test_a_registered_implementation_comes_first(self):
        sentinel = object()
        connectors.register('forge', 'github', lambda cfg: sentinel)
        self.assertIs(connectors.forge({}), sentinel)

    def test_an_entry_point_resolves_a_third_party_name(self):
        class EP:
            name = 'thirdparty'

            def load(self):
                return lambda cfg: ('built', cfg)
        with mock.patch('importlib.metadata.entry_points', return_value=[EP()]) as eps:
            got = connectors.get('forge', {'connectors': {'forge': 'thirdparty'}})
        self.assertEqual(got[0], 'built')
        eps.assert_called_with(group='asf.connectors.forge')

    def test_get_reads_the_operator_config_and_keeps_the_instance(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('connectors:\n  forge: fake\n')
            a = connectors.forge()
            self.assertIsInstance(a, fakes.FakeForge)
            self.assertIs(connectors.forge(), a)

    def test_no_operator_config_is_the_defaults(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            self.assertIsInstance(connectors.forge(), GitHubForge)
            self.assertEqual([n for _k, n, _s in connectors.active()],
                             [connectors.DEFAULTS[k] for k in connectors.KINDS])

    def test_active_lists_every_kind_and_problems_names_the_unresolvable(self):
        cfg = {'connectors': {'forge': 'nowhere'}}
        kinds = [k for k, _n, _s in connectors.active(cfg)]
        self.assertEqual(kinds, list(connectors.KINDS))
        self.assertEqual([k for k, _m in connectors.problems(cfg)], ['forge'])
        self.assertEqual(connectors.problems({}), [])


class GitHubForgeTest(RegistryCase):
    """The default forge is :mod:`asf.github`'s readers, argument for argument."""

    def _run(self, calls, stdout='[]'):
        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout, '')
        return run

    def test_each_reader_sends_the_same_gh_argv(self):
        f = GitHubForge()
        for name, args, kw in (
                ('open_prs', ('o/r',), {'fields': ('number',)}),
                ('prs', ('o/r',), {'state': 'all', 'search': 'X', 'limit': 5, 'fields': ('number',)}),
                ('pr', ('o/r', 7, ('headRefOid',)), {}),
                ('checks', ('o/r', 'abc'), {}),
                ('api', ('repos/o/r/commits/abc/status',), {})):
            mine, theirs = [], []
            getattr(f, name)(*args, run=self._run(mine), **kw)
            getattr(github, name)(*args, run=self._run(theirs), **kw)
            self.assertEqual(mine, theirs, name)
            self.assertEqual(mine[0][0], 'gh')

    def test_close_and_reopen_are_the_gh_pr_commands(self):
        calls = []
        f = GitHubForge()
        f.close_pr('o/r', 4, comment='why', run=self._run(calls, ''))
        f.reopen_pr('o/r', 4, run=self._run(calls, ''))
        f.auth_status(run=self._run(calls, ''))
        self.assertEqual(calls, [['gh', 'pr', 'close', '4', '-R', 'o/r', '--comment', 'why'],
                                 ['gh', 'pr', 'reopen', '4', '-R', 'o/r'],
                                 ['gh', 'auth', 'status']])


class FakeForgeTest(RegistryCase):
    def test_records_calls_and_answers_or_unknown(self):
        f = fakes.FakeForge(answers={'open_prs': [{'number': 1}]})
        self.assertEqual(f.open_prs('o/r').data, [{'number': 1}])
        self.assertFalse(f.checks('o/r', 'abc').ok)
        self.assertEqual([c[0] for c in f.calls], ['open_prs', 'checks'])
        self.assertIsInstance(f, protocols.Forge)

    def test_callers_reach_the_configured_fake(self):
        from asf.facts import cache
        fake = fakes.FakeForge(answers={'open_prs': [{'number': 3, 'headRefName': 'b'}]})
        connectors.register('forge', 'github', lambda cfg: fake)

        class P:
            name = 'p'
            repo_slug = 'o/r'
        cache.clear()
        self.addCleanup(cache.clear)
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            got = cache.prime(P())
        self.assertEqual([p['number'] for p in got.prs], [3])
        self.assertEqual(fake.calls[0][0], 'open_prs')


class GitHubActionsCITest(RegistryCase):
    """The default CI is GitHub Actions through :mod:`asf.github`, argv for argv."""

    def _run(self, calls, stdout='[]'):
        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout, '')
        return run

    def test_default_and_protocol(self):
        c = connectors.ci({})
        self.assertIsInstance(c, GitHubActionsCI)
        self.assertIsInstance(c, protocols.CI)
        self.assertIsInstance(fakes.FakeCI(), protocols.CI)

    def test_each_operation_is_the_gh_argv_it_replaced(self):
        calls = []
        c = GitHubActionsCI()
        c.rerun('o/r', '12', run=self._run(calls))
        c.run('o/r', 12, run=self._run(calls, '{}'))
        c.runs_for_sha('o/r', 'abc', run=self._run(calls, '{}'))
        c.cancel('o/r', 12, run=self._run(calls, ''))
        c.call(['run', 'list', '-R', 'o/r'], run=self._run(calls))
        self.assertEqual(calls, [
            ['gh', 'run', 'rerun', '12', '--failed', '-R', 'o/r'],
            ['gh', 'api', 'repos/o/r/actions/runs/12'],
            ['gh', 'api', 'repos/o/r/actions/runs?head_sha=abc&per_page=100'],
            ['gh', 'run', 'cancel', '12', '-R', 'o/r'],
            ['gh', 'run', 'list', '-R', 'o/r']])

    def test_source_and_backend_are_the_existing_github_adapters(self):
        from asf import ci_pool, ci_queue

        class P:
            name = 'p'
            repo_slug = 'o/r'
            ci = {}
            conventions = None
        c = GitHubActionsCI()
        with mock.patch.object(ci_queue, 'GitHubSource') as src, \
                mock.patch.object(ci_pool, 'GitHubBackend') as be:
            c.source(P())
            c.backend(P(), run=print)
        src.assert_called_once()
        be.assert_called_once()
        self.assertIs(be.call_args.kwargs['run'], print)

    def test_callers_reach_the_configured_fake(self):
        from asf import flake
        fake = fakes.FakeCI(answers={'call': 'out'})
        connectors.register('ci', 'github-actions', lambda cfg: fake)
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            r = flake._call(None, ['run', 'view', '1'])
        self.assertEqual(r.data, 'out')
        self.assertEqual(fake.calls, [('call', (['run', 'view', '1'],), {})])


class RuntimeTest(RegistryCase):
    def test_default_is_the_coding_agent_cli_with_the_configured_binary(self):
        from asf.connectors import claude_code
        c = connectors.runtime({})
        self.assertIsInstance(c, protocols.Runtime)
        self.assertIsInstance(c.local(), runtime_mod.ClaudeCodeRuntime)
        self.assertEqual(c.local().binary, claude_code.DEFAULT_BINARY)
        local = connectors.runtime({'worker_pool': {'binary': '/opt/agent'}}).local()
        self.assertEqual(local.binary, '/opt/agent')

    def test_from_config_keeps_the_backend_key(self):
        self.assertIsInstance(runtime_mod.from_config({}), runtime_mod.ClaudeCodeRuntime)
        self.assertIsInstance(runtime_mod.from_config(None), runtime_mod.ClaudeCodeRuntime)
        self.assertIsInstance(runtime_mod.from_config({'worker_pool': {'backend': 'fake'}}),
                              runtime_mod.FakeRuntime)
        # any other backend was, and is, the CLI
        self.assertIsInstance(runtime_mod.from_config({'worker_pool': {'backend': 'claude_code'}}),
                              runtime_mod.ClaudeCodeRuntime)
        self.assertIsInstance(runtime_mod.from_config({'connectors': {'runtime': 'fake'}}),
                              runtime_mod.FakeRuntime)

    def test_the_cloud_lane_runtime_follows_cloud_runtime(self):
        from asf.workers import actions, cloud, remote

        class S:
            runtime = cloud.RUNTIME_REMOTE
        with mock.patch.object(remote, 'RemoteRuntime', return_value='remote') as rr, \
                mock.patch.object(actions, 'ActionsRuntime', return_value='actions'):
            with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
                self.assertEqual(cloud.lane_runtime(S(), 'P'), 'remote')
                S.runtime = cloud.RUNTIME_ACTIONS
                self.assertEqual(cloud.lane_runtime(S(), 'P'), 'actions')
        rr.assert_called_once()

    def test_the_command_form_runs_a_job(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        script = os.path.join(d, 'rt.py')
        with open(script, 'w') as f:
            f.write('import json,sys\nreq=json.load(sys.stdin)\n'
                    'print(json.dumps({"ok": True, "pid": 7, "result": req["args"][0]["name"]}))\n')
        c = connectors.runtime({'connectors': {'runtime': {'command': f'{sys.executable} {script}'}}})

        class Job:
            def __init__(self):
                self.name, self.log_path, self.brief_path = 'j1', None, '/b'
        r = c.local().run(Job())
        self.assertEqual((r.ok, r.pid, r.text), (True, 7, 'j1'))


class CommandFormTest(RegistryCase):
    def _script(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        path = os.path.join(d, 'bridge.py')
        with open(path, 'w') as f:
            f.write(body)
        return f'{sys.executable} {path}'

    def test_an_operation_is_one_run_with_json_in_and_out(self):
        cmd = self._script('import json,sys\nreq=json.load(sys.stdin)\n'
                           'print("noise")\nprint(json.dumps({"op": sys.argv[1], "req": req}))\n')
        f = connectors.forge({'connectors': {'forge': {'command': cmd}}})
        self.assertIsInstance(f, command_mod.CommandConnector)
        r = f.open_prs('o/r', limit=3, run=lambda *a, **k: None)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.data['op'], 'open_prs')
        self.assertEqual(r.data['req'], {'kind': 'forge', 'op': 'open_prs', 'args': ['o/r'],
                                         'kwargs': {'limit': 3}})

    def test_a_failure_is_unknown_never_empty(self):
        for body, reason in (('import sys\nsys.stderr.write("boom\\n")\nsys.exit(3)\n', 'rc 3: boom'),
                             ('print("not json")\n', 'bad json')):
            f = command_mod.CommandConnector('forge', self._script(body))
            r = f.checks('o/r', 'abc')
            self.assertFalse(r.ok)
            self.assertIsNone(r.data)
            self.assertEqual(r.reason, reason)

    def test_no_command_and_no_binary_are_unknown(self):
        self.assertFalse(command_mod.CommandConnector('forge', '').pr('o/r', 1, ()).ok)
        r = command_mod.CommandConnector('forge', '/nonexistent/bridge').pr('o/r', 1, ())
        self.assertIn('not runnable', r.reason)

    def test_objects_are_sent_as_their_public_attributes(self):
        class Job:
            def __init__(self):
                self.name, self._secret, self.cb = 'j', 's', print
        self.assertEqual(command_mod.jsonable(Job()), {'name': 'j', 'cb': None})


class QuotaTest(RegistryCase):
    def test_no_command_is_no_quota_source(self):
        self.assertIsInstance(quota_mod.source_from_config({}), quota_mod.NoQuotaSource)
        self.assertIsInstance(quota_mod.source_from_config(None), quota_mod.NoQuotaSource)

    def test_the_older_quota_command_is_the_same_command_source(self):
        src = quota_mod.source_from_config({'worker_pool': {'quota_command': 'q {account}'}})
        self.assertIsInstance(src, quota_mod.CommandQuotaSource)
        self.assertEqual((src.command, src.timeout), ('q {account}', 60))

    def test_connectors_quota_command_form(self):
        src = quota_mod.source_from_config(
            {'connectors': {'quota': {'command': 'u {account}', 'timeout_s': 5}}})
        self.assertEqual((src.command, src.timeout), ('u {account}', 5.0))

    def test_quota_command_form_reads_the_usage_line(self):
        src = quota_conn.command({'worker_pool': {'quota_command':
                                                  f'{sys.executable} -c "print(1);print(\'{{\\"five_h_pct\\": 7}}\')" {{account}}'}})
        self.assertEqual(src.read('a1'), {'five_h_pct': 7})


class SecretsTest(RegistryCase):
    def test_file_is_the_default_and_reads_stripped(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('  tok\n')
        self.addCleanup(os.unlink, f.name)
        s = connectors.get('secrets', {})
        self.assertIsInstance(s, secrets_conn.FileSecrets)
        self.assertEqual(s.read(f.name), 'tok')
        with self.assertRaises(FileNotFoundError):
            s.read(f.name + '.missing')

    def test_command_form_substitutes_ref_and_failure_is_oserror(self):
        cfg = {'connectors': {'secrets': {'command': f'{sys.executable} -c "import sys;print(sys.argv[1][::-1])" {{ref}}'}}}
        s = connectors.get('secrets', cfg)
        self.assertEqual(s.read('abc'), 'cba')
        bad = secrets_conn.CommandSecrets(f'{sys.executable} -c "import sys;sys.exit(2)"')
        with self.assertRaises(OSError):
            bad.read('abc')

    def test_auth_env_values_resolves_through_the_secrets_connector(self):
        class Acct:
            name = 'a1'
            auth_env = {'TOKEN_VAR': '/vault/x'}
        connectors.register('secrets', 'file', lambda cfg: secrets_conn.CommandSecrets(
            f'{sys.executable} -c "import sys;print(\'v-\'+sys.argv[1])" {{ref}}'))
        with tempfile.TemporaryDirectory() as home, mock.patch.object(env, 'ASF_HOME', home):
            self.assertEqual(runtime_mod.auth_env_values(Acct()), {'TOKEN_VAR': 'v-/vault/x'})

    def test_auth_env_values_on_a_missing_file_is_unchanged(self):
        class Acct:
            name = 'a1'
            auth_env = {'TOKEN_VAR': '/nonexistent/tok'}
        with self.assertRaises(runtime_mod.AuthEnvError) as cm:
            runtime_mod.auth_env_values(Acct())
        self.assertIn('does not exist', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
