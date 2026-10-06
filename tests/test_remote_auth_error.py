"""A cloud helper refused on an auth error (#30): the CLI writes the reason into the stream-json
``result`` record and nothing on stderr. The error names that reason, and the account is marked
unusable on the first failure — the one ALARM — as a local session's auth error does."""
import json
import os
import subprocess
from unittest import mock

from asf.workers import account_auth, remote, spawn as spawn_mod

try:
    from test_remote import FakeHelper, acct, job
    from test_workers import Home
except ImportError:  # pragma: no cover - import shape only
    from tests.test_remote import FakeHelper, acct, job
    from tests.test_workers import Home
from asf import env

REASON = 'Your organization has disabled Claude subscription access for Claude Code'


def refused(argv, input='', env=None, **_kw):
    out = '\n'.join(json.dumps(r) for r in (
        {'type': 'system', 'subtype': 'init', 'tools': ['RemoteTrigger']},
        {'type': 'assistant', 'message': {'model': '<synthetic>', 'content': [
            {'type': 'text', 'text': f'API Error: 403 {REASON}'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': True,
         'terminal_reason': 'api_error', 'result': f'API Error: 403 {REASON}'})) + '\n'
    return subprocess.CompletedProcess(argv, 1, stdout=out, stderr='')


class RemoteAuthError(Home):
    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.brief = os.path.join(self.tmp, 'b.md')
        with open(self.brief, 'w') as f:
            f.write('the brief')

    def job(self, **kw):
        return job(brief_path=self.brief, cwd=self.repo,
                   log_path=os.path.join(self.tmp, 'j.jsonl'), **kw)

    def test_the_error_names_the_result_text_when_stderr_is_empty(self):
        c = remote.TriggerClient(acct(), binary='claude', run=refused, cwd=self.tmp)
        with self.assertRaises(remote.HelperError) as cm:
            c.call('create', body={})
        self.assertIn(REASON, str(cm.exception))

    def test_stderr_still_wins_when_there_is_one(self):
        c = remote.TriggerClient(acct(), binary='claude', run=FakeHelper(no_call=True), cwd='/t')
        with self.assertRaises(remote.HelperError) as cm:
            c.call('list')
        self.assertIn('Not logged in', str(cm.exception))

    def test_a_cloud_launch_refused_on_auth_blocks_the_account_at_once(self):
        rt = remote.RemoteRuntime(
            remote.cloud.settings({'cloud': {'enabled': True, 'runtime': 'claude-remote',
                                             'environment_id': 'env_0123',
                                             'accounts': ['acct-c']}}),
            self.product,
            client=lambda a: remote.TriggerClient(a, binary='claude', run=refused, cwd=self.tmp))
        with mock.patch.object(account_auth, '_configured_accounts', return_value=('acct-c',)):
            with self.assertRaises(spawn_mod.SpawnError) as cm:
                rt.run(self.job())
            self.assertIn(REASON, str(cm.exception))
            self.assertIn('acct-c', account_auth.blocked())
            # a second refusal is no second ALARM
            with self.assertRaises(spawn_mod.SpawnError):
                rt.run(self.job())
        self.assertTrue(account_auth.blocked()['acct-c'].get('alarmed'))

    def test_a_non_auth_helper_failure_blocks_nothing(self):
        rt = remote.RemoteRuntime(
            remote.cloud.settings({'cloud': {'enabled': True, 'runtime': 'claude-remote',
                                             'environment_id': 'env_0123',
                                             'accounts': ['acct-c']}}),
            self.product,
            client=lambda a: remote.TriggerClient(a, binary='claude',
                                                  run=FakeHelper(no_call=True), cwd=self.tmp))
        with self.assertRaises(spawn_mod.SpawnError):
            rt.run(self.job())
        self.assertNotIn('acct-c', account_auth.blocked())
