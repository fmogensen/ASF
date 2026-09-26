"""asf.workers.remote — the cloud lane's claude-remote runtime: the routine body, the helper's
environment (the account's login, never a runtime token), the helper's answer read off its
stream-json log (a saved result included), a launch (create, verify, fire), per-account
environments, placement, and the sync of a finished, a failed and a timed-out routine run.
Nothing reaches the network: the helper `claude -p` is a fake runner."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import remote
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import wave as wave_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_remote` does not
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row, git

ENV_ID = 'env_0123'
ON = {'enabled': True, 'runtime': 'claude-remote', 'environment_id': ENV_ID, 'max_inflight': 2,
      'rows': 'any', 'accounts': ['acct-c']}


def stream(tool_input, result, name='RemoteTrigger'):
    """A helper's stream-json log: one tool call and its result."""
    return '\n'.join(json.dumps(r) for r in (
        {'type': 'system', 'subtype': 'init', 'tools': ['RemoteTrigger']},
        {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tu_1', 'name': name, 'input': tool_input}]}},
        {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'tu_1', 'content': result}]}},
        {'type': 'result', 'subtype': 'success', 'result': 'HTTP 200'})) + '\n'


class FakeHelper:
    """The helper ``claude -p`` as a run callable: relays the prompt's arguments (or an altered
    copy) and answers each action from ``self.answers``."""

    def __init__(self, alter=False, answers=None, no_call=False):
        self.calls = []
        self.alter = alter
        self.no_call = no_call
        self.answers = {
            'create': (200, {'outcome': 'CREATE_TRIGGER_OUTCOME_CREATED',
                             'trigger': {'id': 'trig_1'}},
                       'Runs at 2036-01-01. https://claude.ai/code/routines/trig_1'),
            'run': (200, {'session_id': 'cse_9', 'trigger': {'id': 'trig_1'}}, ''),
            'get': (200, {'trigger': {'id': 'trig_1', 'last_run': {
                'status': 'ROUTINE_RUN_STATUS_RUNNING', 'finished_at': None}}}, ''),
            'update': (200, {'trigger': {'id': 'trig_1', 'enabled': False}}, ''),
        }
        self.answers.update(answers or {})

    def __call__(self, argv, input='', env=None, **_kw):
        args = json.loads(input.split('<<<ARGS\n', 1)[1].split('\nARGS>>>', 1)[0])
        self.calls.append({'argv': argv, 'args': args, 'env': env})
        if self.no_call:
            return subprocess.CompletedProcess(argv, 1, stdout='', stderr='Not logged in')
        sent = dict(args)
        if self.alter and args['action'] == 'create':
            sent = json.loads(json.dumps(args))
            sent['body']['job_config']['ccr']['events'][0]['data']['message']['content'] = 'x'
        code, obj, tail = self.answers[args['action']]
        text = f'HTTP {code}\n{json.dumps(obj)}\n{tail}'
        return subprocess.CompletedProcess(argv, 0, stdout=stream(sent, text), stderr='')

    def actions(self):
        return [c['args']['action'] for c in self.calls]


def acct(name='acct-c'):
    return pool_mod.Account(name, cap=1, config_dir=f'/cfg/{name}',
                            auth_env={'CLAUDE_CODE_OAUTH_TOKEN': '/secret/tok'})


def job(**kw):
    j = runtime_mod.Job('sample', 'task-t-0001', '/wt', '/b.md', 'claude-opus-5',
                        account=acct(),
                        env={'BACKLOG_ID_RANGE': 'S:5000-5049', 'ASF_SESSION': 'sid-1'},
                        branch='task/t-0001', base='main', setup='pnpm install')
    for k, v in kw.items():
        setattr(j, k, v)
    return j


class Body(unittest.TestCase):
    def test_the_routine_body(self):
        self.assertEqual(remote.trigger_body('n', 'the brief', ENV_ID, 'claude-opus-5', ['Bash'],
                                             'https://github.com/o/r'), {
            'name': 'n', 'enabled': True, 'persist_session': False,
            'run_once_at': remote.FAR_FUTURE,
            'job_config': {'ccr': {
                'environment_id': ENV_ID,
                'events': [{'type': 'user', 'data': {'message': {'role': 'user',
                                                                 'content': 'the brief'}}}],
                'session_context': {'allowed_tools': ['Bash'], 'model': 'claude-opus-5',
                                    'sources': [{'git_repository': {
                                        'url': 'https://github.com/o/r'}}]}}}})
        ctx = remote.trigger_body('n', 'p', ENV_ID, '', (), 'u')['job_config']['ccr'][
            'session_context']
        self.assertEqual(ctx['allowed_tools'], list(remote.DEFAULT_ALLOWED_TOOLS))
        self.assertNotIn('model', ctx)

    def test_the_brief_checks_the_branch_out_and_exports_the_environment(self):
        j = job()
        text = cloud.cloud_brief('the brief', j, setting=remote.setting_lines(j))
        self.assertTrue(text.startswith('the brief'))
        self.assertIn('claude.ai cloud session', text)
        self.assertIn('git fetch origin task/t-0001 && git checkout -B task/t-0001 '
                      'origin/task/t-0001', text)
        self.assertIn('export ASF_SESSION=sid-1 BACKLOG_ID_RANGE=S:5000-5049', text)
        self.assertIn('`pnpm install`', text)
        self.assertIn('ASF-Report: task-t-0001', text)
        self.assertNotIn('CI job', text)
        self.assertIn('CI job', cloud.cloud_brief('b', j))  # the actions brief is unchanged


class Helper(unittest.TestCase):
    def test_the_env_is_the_accounts_login_never_a_runtime_token(self):
        base = {'PATH': '/bin', 'HOME': '/Users/op', 'CLAUDE_CODE_OAUTH_TOKEN': 'sk-x',
                'ANTHROPIC_API_KEY': 'sk-y'}
        e = remote.helper_env(acct(), base=base)
        self.assertEqual(e['CLAUDE_CONFIG_DIR'], '/cfg/acct-c')
        self.assertEqual(e['HOME'], '/Users/op')  # the keychain that holds the login
        for var in remote.TOKEN_VARS:
            self.assertNotIn(var, e)

    def test_one_call_is_one_relayed_tool_call(self):
        fake = FakeHelper()
        c = remote.TriggerClient(acct(), binary='claude', run=fake, cwd='/tmp')
        code, obj, tail, sent = c.call('get', trigger_id='trig_1')
        self.assertEqual((code, obj['trigger']['id'], sent),
                         (200, 'trig_1', {'action': 'get', 'trigger_id': 'trig_1'}))
        argv = fake.calls[0]['argv']
        self.assertEqual(argv, ['claude', '-p', '--model', 'haiku', '--allowedTools',
                                'RemoteTrigger', '--output-format', 'stream-json', '--verbose'])
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', fake.calls[0]['env'])
        with self.assertRaises(remote.HelperError):
            c.call('get', trigger_id='trig_1; rm -rf /')

    def test_a_saved_result_is_read_from_its_file(self):
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write('HTTP 200\n{"data": [{"id": "trig_a"}, {"id": "trig_b"}]}\n')
        self.addCleanup(os.unlink, f.name)
        text = (f'<persisted-output>\nOutput too large (56.2KB). Full output saved to: {f.name}'
                '\n\nPreview (first 2KB):\nHTTP 200\n{"data":[{"id"')
        code, obj, _tail = remote.http_json(text)
        self.assertEqual((code, len(obj['data'])), (200, 2))

    def test_no_tool_call_is_an_error(self):
        c = remote.TriggerClient(acct(), binary='claude', run=FakeHelper(no_call=True), cwd='/t')
        with self.assertRaises(remote.HelperError) as cm:
            c.call('list')
        self.assertIn('Not logged in', str(cm.exception))

    def test_create_reads_the_nested_id_and_the_link(self):
        c = remote.TriggerClient(acct(), binary='claude', run=FakeHelper(), cwd='/t')
        body = remote.trigger_body('n', 'p', ENV_ID, 'm', (), 'u')
        self.assertEqual(c.create(body), ('trig_1', 'https://claude.ai/code/routines/trig_1'))

    def test_an_altered_body_is_disabled_and_refused(self):
        fake = FakeHelper(alter=True)
        c = remote.TriggerClient(acct(), binary='claude', run=fake, cwd='/t')
        with self.assertRaises(remote.HelperError) as cm:
            c.create(remote.trigger_body('n', 'p', ENV_ID, 'm', (), 'u'))
        self.assertIn('altered', str(cm.exception))
        self.assertEqual(fake.actions(), ['create', 'update'])
        self.assertEqual(fake.calls[1]['args']['body'], {'enabled': False})

    def test_a_refused_create_names_the_reason(self):
        fake = FakeHelper(answers={'create': (400, {'error': {
            'message': 'Environment env_x does not exist', 'reason': 'environment_not_found'}},
            '')})
        c = remote.TriggerClient(acct(), binary='claude', run=fake, cwd='/t')
        with self.assertRaises(remote.HelperError) as cm:
            c.create(remote.trigger_body('n', 'p', 'env_x', 'm', (), 'u'))
        self.assertIn('HTTP 400 Environment env_x does not exist', str(cm.exception))

    def test_the_session_link(self):
        self.assertEqual(remote.session_url('cse_01Xy'), 'https://claude.ai/code/session_01Xy')
        self.assertEqual(remote.session_of({'conversation_id': '', 'session_id': 'cse_9'}),
                         'cse_9')

    def test_last_run_maps_onto_the_workflow_view(self):
        self.assertIsNone(remote.view_of(None))
        self.assertEqual(remote.view_of({'status': 'ROUTINE_RUN_STATUS_RUNNING'})['status'],
                         'in_progress')
        self.assertEqual(remote.view_of({'status': 'ROUTINE_RUN_STATUS_SUCCEEDED',
                                         'finished_at': '2026-09-26T08:56:16Z'}),
                         {'status': 'completed', 'conclusion': 'succeeded'})
        self.assertEqual(remote.view_of({'status': 'ROUTINE_RUN_STATUS_FAILED'})['conclusion'],
                         'failed')


class Config(unittest.TestCase):
    def test_claude_remote_is_a_lane_runtime_and_claude_cloud_stays_refused(self):
        self.assertEqual(cloud.config_problems(dict(ON)), [])
        self.assertTrue(cloud.settings({'cloud': ON}).on)
        (key, why), = cloud.config_problems({'enabled': True, 'runtime': 'claude-cloud'})
        self.assertEqual(key, 'cloud.runtime')
        self.assertIn('claude-remote', why)

    def test_the_environment_is_required_and_may_be_per_account(self):
        (key, _why), = cloud.config_problems({'enabled': True, 'runtime': 'claude-remote'})
        self.assertEqual(key, 'cloud.environment_id')
        per = dict(ON, environment_id={'acct-c': 'env_c', 'acct-d': 'env_d'},
                   accounts=['acct-c', 'acct-d', 'acct-e'], allowed_tools=['Bash'],
                   model='claude-sonnet-5', poll_min=5)
        self.assertEqual(cloud.config_problems(per), [])
        s = cloud.settings({'cloud': per})
        self.assertEqual((s.environment_id, remote.environment_for(s, 'acct-d'),
                          remote.environment_for(s, 'acct-e')), ('', 'env_d', None))
        self.assertEqual((s.allowed_tools, s.model, s.poll_min),
                         (('Bash',), 'claude-sonnet-5', 5.0))
        accounts = [pool_mod.Account(n, cap=1) for n in ('acct-c', 'acct-d', 'acct-e')]
        self.assertEqual([a.name for a in cloud.lane_accounts(accounts, s)], ['acct-c', 'acct-d'])
        self.assertEqual(cloud.config_problems(dict(ON, allowed_tools='Bash'))[0][0],
                         'cloud.allowed_tools')

    def test_doctor_rows(self):
        product = env.Product('sample', {'repo_dir': '/r', 'main': 'main'})
        s = cloud.settings({'cloud': dict(ON, environment_id='')})
        self.assertEqual(len(remote.doctor_rows(s, product)), 2)
        product = env.Product('sample', {'repo_dir': '/r', 'main': 'main', 'repo_slug': 'o/r'})
        self.assertEqual(remote.doctor_rows(cloud.settings({'cloud': ON}), product), [])

    def test_the_token(self):
        self.assertTrue(cloudpid.is_token('remote:trig_1'))
        self.assertTrue(cloud.is_cloud({'pid': 'remote:trig_1'}))
        self.assertTrue(remote.is_remote({'pid': 'remote:trig_1'}))
        self.assertFalse(remote.is_remote({'pid': 'actions:5'}))


class Launch(Home):
    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.brief = os.path.join(self.tmp, 'b.md')
        with open(self.brief, 'w') as f:
            f.write('the brief')

    def job(self, **kw):
        return job(brief_path=self.brief, cwd=self.repo,
                   log_path=os.path.join(self.tmp, 'j.jsonl'), **kw)

    def runtime(self, fake, block=ON):
        return remote.RemoteRuntime(
            cloud.settings({'cloud': block}), self.product,
            client=lambda a: remote.TriggerClient(a, binary='claude', run=fake, cwd=self.tmp))

    def test_create_verify_fire_and_the_token(self):
        fake = FakeHelper()
        res = self.runtime(fake).run(self.job())
        self.assertEqual(res.pid, 'remote:trig_1')
        self.assertEqual(fake.actions(), ['create', 'run'])
        body = fake.calls[0]['args']['body']
        ccr = body['job_config']['ccr']
        self.assertEqual(body['name'], 'asf sample task-t-0001 sid-1')
        self.assertEqual((ccr['environment_id'], ccr['session_context']['model'],
                          ccr['session_context']['sources']),
                         (ENV_ID, 'claude-opus-5',
                          [{'git_repository': {'url': 'https://github.com/o/r'}}]))
        prompt = ccr['events'][0]['data']['message']['content']
        self.assertTrue(prompt.startswith('the brief'))
        self.assertIn('ASF-Session: sid-1', prompt)
        self.assertEqual(fake.calls[1]['args'], {'action': 'run', 'trigger_id': 'trig_1'})
        self.assertEqual(res.extra, {
            'lane': 'cloud', 'cloud_runtime': 'claude-remote', 'remote_trigger_id': 'trig_1',
            'remote_session_id': 'cse_9', 'cloud_url': 'https://claude.ai/code/session_9',
            'remote_routine_url': 'https://claude.ai/code/routines/trig_1'})
        self.assertTrue(lifecycle.pid_alive(res.pid))
        with open(res.log_path) as f:
            logged = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual((logged[-1]['trigger'], logged[-1]['run']), ('trig_1', 'cse_9'))
        self.assertIsNone(self.runtime(fake).continue_run(self.job()))

    def test_the_accounts_own_environment_and_model_override(self):
        fake = FakeHelper()
        block = dict(ON, environment_id={'acct-c': 'env_c'}, model='claude-sonnet-5')
        self.runtime(fake, block).run(self.job())
        ccr = fake.calls[0]['args']['body']['job_config']['ccr']
        self.assertEqual((ccr['environment_id'], ccr['session_context']['model']),
                         ('env_c', 'claude-sonnet-5'))
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.runtime(fake, block).run(self.job(account=acct('acct-z')))
        self.assertIn('no environment for account acct-z', str(cm.exception))

    def test_a_refused_fire_disables_the_routine(self):
        fake = FakeHelper(answers={'run': (429, {'error': {'message': 'fire cap'}}, '')})
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.runtime(fake).run(self.job())
        self.assertIn('fire cap', str(cm.exception))
        self.assertEqual(fake.actions(), ['create', 'run', 'update'])

    def test_no_repo_slug_refuses(self):
        self.product = env.Product('sample', {k: v for k, v in self.product._data.items()
                                              if k != 'repo_slug'})
        with self.assertRaises(spawn_mod.SpawnError):
            self.runtime(FakeHelper()).run(self.job())


class Sync(Home):
    """Placement through the wave, then cloud.sync reading the routine and the branch."""

    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.cfg = dict(self.cfg, cloud=dict(ON))
        self.cfg['worker_pool'] = dict(self.cfg['worker_pool'], accounts=[
            {'name': 'acct-a', 'role': 'local', 'cap': 1},
            {'name': 'acct-c', 'role': 'worker', 'cap': 1}])
        self.fake = FakeHelper()

    def client(self, account=None):
        return remote.TriggerClient(account or acct(), binary='claude', run=self.fake,
                                    cwd=self.tmp)

    def launch(self):
        accounts = pool_mod.accounts_from_config(self.cfg)
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource({}))
        rt = remote.RemoteRuntime(cloud.settings(self.cfg, self.product), self.product,
                                  client=self.client)
        lines = []
        launched, waits = wave_mod.wave(
            self.product, [feature_row('spec-1')], 5, pool=pool,
            runtime=runtime_mod.FakeRuntime([{'running': True}] * 5), cfg=self.cfg,
            out=lines.append, local_hold='host pressure', cloud_runtime=rt)
        self.assertEqual(waits, [])
        (_row, rec), = launched
        self.assertEqual((rec['account'], rec['lane'], rec['pid']),
                         ('acct-c', 'cloud', 'remote:trig_1'))
        self.assertIn('cloud https://claude.ai/code/session_9', lines[0])
        return rec

    def sync(self, now=None):
        return cloud.sync(self.product, self.cfg, out=lambda _l: None, now=now,
                          remote_client=self.client())

    def push_report(self, rec, trailers=True):
        other = os.path.join(self.tmp, 'cloud-checkout')
        git('clone', '-q', '-b', rec['branch'], os.path.join(self.tmp, 'origin.git'), other,
            cwd=self.tmp)
        for k, v in (('user.email', 'c@example.com'), ('user.name', 'c')):
            git('config', k, v, cwd=other)
        args = ['commit', '-q', '--allow-empty', '-m',
                'asf: report spec-1\n\nREPORT\nitem: F-0001\npushed: yes']
        if trailers:
            args += ['--trailer', f"ASF-Session: {rec['session']}", '--trailer',
                     'ASF-Report: spec-1']
        git(*args, cwd=other)
        git('push', '-q', 'origin', rec['branch'], cwd=other)

    def test_the_report_commit_finishes_the_run_and_the_routine_is_disabled(self):
        rec = self.launch()
        self.assertEqual(self.sync()[0][1], cloud.WORKING)
        self.assertEqual(self.fake.actions(), ['create', 'run', 'get'])
        self.sync()  # inside cloud.poll_min: the cached last_run, no helper call
        self.assertEqual(self.fake.actions(), ['create', 'run', 'get'])
        self.push_report(rec)
        (_job, status, why), = self.sync()
        self.assertEqual(status, cloud.FINISHED, why)
        self.assertEqual(self.fake.actions()[-1], 'update')
        self.assertFalse(lifecycle.pid_alive(rec['pid']))
        self.assertIn('item: F-0001', runtime_mod.read_result(rec['log'])['result'])
        self.sync()
        self.assertEqual(self.fake.actions().count('update'), 1)  # retired once

    def test_a_run_that_ended_without_the_report_is_dead(self):
        rec = self.launch()
        self.fake.answers['get'] = (200, {'trigger': {'last_run': {
            'status': 'ROUTINE_RUN_STATUS_SUCCEEDED', 'finished_at': '2026-09-26T08:56:16Z',
            'session_id': 'cse_9'}}}, '')
        (_job, status, why), = self.sync()
        self.assertEqual((status, why),
                         (cloud.DEAD, 'run trig_1 ended succeeded without the report commit'))
        self.assertFalse(lifecycle.pid_alive(rec['pid']))
        self.assertEqual(self.fake.actions()[-1], 'update')

    def test_a_run_past_its_limit_is_dead_and_its_routine_disabled(self):
        rec = self.launch()
        (_job, status, why), = self.sync(now=time.time() + 5 * 3600)
        self.assertEqual((status, why), (cloud.DEAD, 'timed out after 240m'))
        self.assertEqual(self.fake.actions().count('update'), 1)
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertEqual(lifecycle.judge(run, lifecycle.gather(self.product, run)),
                         lifecycle.DEAD_PID)
        self.assertEqual(rec['remote_trigger_id'], 'trig_1')

    def test_stop_disables_the_routine(self):
        rec = self.launch()
        client = self.client()
        with mock.patch.object(remote, 'TriggerClient', lambda *_a, **_k: client), \
                mock.patch.object(remote, '_account', lambda _run: acct()):
            ok, detail = cloud.stop(rec)
        self.assertEqual((ok, detail), (True, 'routine trig_1 disabled'))
        self.assertFalse(lifecycle.pid_alive(rec['pid']))

    def test_the_lane_runtime_is_the_routine_runtime(self):
        rt = cloud.lane_runtime(cloud.settings(self.cfg, self.product), self.product)
        self.assertIsInstance(rt, remote.RemoteRuntime)


if __name__ == '__main__':
    unittest.main()
