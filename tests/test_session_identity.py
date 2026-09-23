"""asf — session identity (F-0076): the id, the registry line, the environment, the log and the
commit trailer. This module grows with each Task of the plan; T-9450 added ``SessionIdTest``,
``LaunchIdentityTest`` and ``CommitTrailerTest``, T-9451 added ``ObserveTest``, and T-0048 adds
``PoolAcrossProductsTest``."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf import hermetic
from asf.workers import githooks
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stall as stall_mod
from asf.workers import wave as wave_mod

from tests.test_workers import Home, feature_row, s1_row


def _git(args, cwd, env=None):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=env)


def fake_ps(directory, table=None):
    """Install a ``ps`` stub into ``<directory>/bin`` (put it first on ``PATH``): with ``table``
    given (the fixed text ``ps axeww -o pid=,ppid=,command=`` would print), it prints exactly
    that and exits 0; with none, it exits 1 (F-0076 D10, an unreadable process table). The
    ``fake_launchctl`` pattern (``tests/test_scheduler.py:63``)."""
    bindir = os.path.join(directory, 'bin')
    os.makedirs(bindir, exist_ok=True)
    stub = os.path.join(bindir, 'ps')
    if table is None:
        body = '#!/bin/sh\nexit 1\n'
    else:
        data = os.path.join(bindir, 'ps-table.txt')
        with open(data, 'w', encoding='utf-8') as f:
            f.write(table)
        body = f'#!/bin/sh\ncat "{data}"\n'
    with open(stub, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(stub, 0o755)
    return bindir


class SessionIdTest(unittest.TestCase):
    def test_format_and_parse(self):
        sid = lifecycle.session_id('p', 'spec-f-0001', '2026-09-22T10:15:00Z')
        self.assertEqual(sid, 'p/spec-f-0001@20260922T101500Z')
        self.assertEqual(lifecycle.parse_session(sid), ('p', 'spec-f-0001', '20260922T101500Z'))
        self.assertIsNone(lifecycle.parse_session('no-slash-or-at'))
        self.assertIsNone(lifecycle.parse_session('p/j-no-at'))
        self.assertIsNone(lifecycle.parse_session('/j@stamp'))

    def _write(self, path, *lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for rec in lines:
                f.write(json.dumps(rec) + '\n')

    def test_old_line_gets_a_derived_id(self):
        tmp = tempfile.mkdtemp(prefix='asf-lifecycle-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_home, env.ASF_HOME = env.ASF_HOME, os.path.join(tmp, 'asf-home')
        try:
            path = os.path.join(env.ASF_HOME, 'state', 'p', 'sessions.jsonl')
            self._write(path, {'job': 'j', 'pid': 111, 'started': '2026-09-22T10:15:00Z'})
            latest = lifecycle.latest(path)
            self.assertEqual(latest['j']['session'], 'p/j@20260922T101500Z')
            self.assertEqual(latest['j']['product'], 'p')
        finally:
            env.ASF_HOME = old_home

    def test_two_runs_two_ids(self):
        tmp = tempfile.mkdtemp(prefix='asf-lifecycle-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_home, env.ASF_HOME = env.ASF_HOME, os.path.join(tmp, 'asf-home')
        try:
            path = os.path.join(env.ASF_HOME, 'state', 'p', 'sessions.jsonl')
            self._write(path,
                        {'job': 'j', 'pid': 111, 'started': '2026-09-22T10:15:00Z'},
                        {'job': 'j', 'ended': '2026-09-22T10:16:00Z', 'end_reason': 'finished'},
                        {'job': 'j', 'pid': 222, 'started': '2026-09-22T10:15:01Z'})
            runs = lifecycle.runs(path)['j']
            self.assertEqual(len(runs), 2)
            self.assertNotEqual(runs[0]['session'], runs[1]['session'])
        finally:
            env.ASF_HOME = old_home


class LaunchIdentityTest(Home):
    def _spawn(self, job='j1', item='B-0001', script=None):
        rt = runtime_mod.FakeRuntime(script or [{'ok': True, 'result': 'done', 'pid': 4242}])
        row = s1_row(job=job, item=item)
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'do it\n', runtime=rt, cfg=self.cfg)
        job_obj, _ = rt.calls[0]
        return rec, job_obj, rt

    def test_spawn_writes_session_and_product(self):
        rec, job_obj, rt = self._spawn()
        stamp = rec['started'].replace('-', '').replace(':', '')
        self.assertEqual(rec['session'], f'sample/j1@{stamp}')
        self.assertEqual(rec['product'], 'sample')
        s = pool_mod.load_sessions(self.product)['j1']
        self.assertEqual((s['session'], s['product']), (rec['session'], 'sample'))

    def test_env_carries_asf_session(self):
        rec, job_obj, rt = self._spawn(job='j2')
        e = runtime_mod.build_env(job_obj, base={'PATH': os.environ.get('PATH', ''), 'HOME': '/me'})
        self.assertEqual(e['ASF_SESSION'], job_obj.session)
        n = int(e['GIT_CONFIG_COUNT'])
        pairs = [(e[f'GIT_CONFIG_KEY_{i}'], e[f'GIT_CONFIG_VALUE_{i}']) for i in range(n)]
        self.assertIn(('core.hooksPath', job_obj.hooks_dir), pairs)
        self.assertEqual(job_obj.hooks_dir, os.path.join(env.state_dir(self.product), 'githooks'))

    def test_asf_session_never_leaks_to_a_child(self):
        e = hermetic.build({'ASF_SESSION': 'x', 'PATH': '/bin'})
        self.assertNotIn('ASF_SESSION', e)

    def test_log_opens_with_the_identity_line(self):
        rec, job_obj, rt = self._spawn(job='j3')
        with open(rec['log'], encoding='utf-8') as f:
            first = json.loads(f.readline())
        self.assertEqual(first, {'type': 'asf', 'subtype': 'session', 'session': rec['session'],
                                 'product': 'sample', 'job': 'j3', 'started': rec['started']})
        self.assertIsNotNone(runtime_mod.read_result(rec['log']))

    def test_correction_retry_has_its_own_id(self):
        rec, job_obj, rt = self._spawn(job='j4', script=[{'ok': False, 'result': 'boom'}])
        session = pool_mod.load_sessions(self.product)['j4']
        stall_mod.correct_once(self.product, session, 'it broke', rt)
        retry = pool_mod.load_sessions(self.product)['j4-correction']
        self.assertNotEqual(retry['session'], session['session'])
        self.assertTrue(retry['session'].startswith('sample/j4-correction@'))


class CommitTrailerTest(Home):
    def setUp(self):
        super().setUp()
        self.git_home = tempfile.mkdtemp(prefix='asf-git-home-')
        self.addCleanup(shutil.rmtree, self.git_home, ignore_errors=True)

    def _spawn_job(self, job='j1'):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        row = s1_row(job=job, item='B-0001')
        spawn_mod.spawn(self.product, row, self.acct(), 'do it\n', runtime=rt, cfg=self.cfg)
        job_obj, _ = rt.calls[0]
        return job_obj

    def _env(self, job_obj, user='tester', email='tester@example.com'):
        base = {'PATH': os.environ.get('PATH', ''), 'HOME': self.git_home,
               'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'user.name',
               'GIT_CONFIG_VALUE_0': user, 'GIT_CONFIG_KEY_1': 'user.email',
               'GIT_CONFIG_VALUE_1': email}
        return runtime_mod.build_env(job_obj, base=base)

    def _commit(self, wt, e, msg='x', extra_args=()):
        with open(os.path.join(wt, 'f.txt'), 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
        _git(['add', '.'], wt, env=e)
        return _git(['commit', '-s', '-m', msg, *extra_args], wt, env=e)

    def test_commit_in_a_session_env_gets_the_trailer(self):
        job_obj = self._spawn_job()
        wt = job_obj.cwd
        e = self._env(job_obj)
        p = self._commit(wt, e)
        self.assertEqual(p.returncode, 0, p.stderr)
        trailer = _git(['log', '-1', '--format=%(trailers:key=ASF-Session,valueonly)'], wt,
                       env=e).stdout.strip()
        self.assertEqual(trailer, job_obj.session)
        msg = _git(['log', '-1', '--format=%B'], wt, env=e).stdout
        self.assertIn('Signed-off-by:', msg)

    def test_amend_keeps_one_trailer(self):
        job_obj = self._spawn_job(job='j2')
        wt = job_obj.cwd
        e = self._env(job_obj)
        self._commit(wt, e)
        p = _git(['commit', '--amend', '--no-edit', '-s'], wt, env=e)
        self.assertEqual(p.returncode, 0, p.stderr)
        msg = _git(['log', '-1', '--format=%B'], wt, env=e).stdout
        self.assertEqual(msg.count('ASF-Session:'), 1)

    def test_product_hook_still_runs(self):
        job_obj = self._spawn_job(job='j3')
        wt = job_obj.cwd
        marker = os.path.join(wt, 'marker')
        _git(['config', 'core.hooksPath', '.githooks'], wt)
        hooks_dir = os.path.join(wt, '.githooks')
        os.makedirs(hooks_dir, exist_ok=True)
        pre_commit = os.path.join(hooks_dir, 'pre-commit')
        with open(pre_commit, 'w', encoding='utf-8') as f:
            f.write(f'#!/bin/sh\ntouch "{marker}"\n')
        os.chmod(pre_commit, 0o755)
        e = self._env(job_obj)
        p = self._commit(wt, e)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(marker))
        os.remove(marker)
        with open(pre_commit, 'w', encoding='utf-8') as f:
            f.write(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        p = self._commit(wt, e, msg='y')
        self.assertNotEqual(p.returncode, 0)

    def test_default_hooks_dir_chains(self):
        job_obj = self._spawn_job(job='j4')
        wt = job_obj.cwd
        # a fresh clone in this sandbox carries its own core.hooksPath (disabling hooks by
        # default) — cleared here so the scenario under test is the spec's: no override at all,
        # only the default <git-common-dir>/hooks
        _git(['config', '--unset-all', 'core.hooksPath'], wt)
        common = _git(['rev-parse', '--git-common-dir'], wt).stdout.strip()
        if not os.path.isabs(common):
            common = os.path.join(wt, common)
        hooks_dir = os.path.join(common, 'hooks')
        os.makedirs(hooks_dir, exist_ok=True)
        commit_msg = os.path.join(hooks_dir, 'commit-msg')
        with open(commit_msg, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\necho "chained" >> "$1"\n')
        os.chmod(commit_msg, 0o755)
        e = self._env(job_obj)
        p = self._commit(wt, e)
        self.assertEqual(p.returncode, 0, p.stderr)
        msg = _git(['log', '-1', '--format=%B'], wt, env=e).stdout
        self.assertIn('chained', msg)

    def test_commit_outside_a_session_gets_nothing(self):
        job_obj = self._spawn_job(job='j5')
        wt = job_obj.cwd
        _git(['config', 'user.name', 'tester'], wt)
        _git(['config', 'user.email', 'tester@example.com'], wt)
        base = {'PATH': os.environ.get('PATH', ''), 'HOME': self.git_home}
        p = self._commit(wt, base)
        self.assertEqual(p.returncode, 0, p.stderr)
        trailer = _git(['log', '-1', '--format=%(trailers:key=ASF-Session,valueonly)'],
                       wt).stdout.strip()
        self.assertEqual(trailer, '')


class ObserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-observe-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _path(self, bindir):
        return bindir + os.pathsep + os.environ.get('PATH', '')

    def test_sessions_and_their_children(self):
        table = ('  100     1 /usr/bin/claude -p --permission-mode acceptEdits\n'
                 '  101   100 -bash\n'
                 '  102   101 /usr/bin/claude -p --permission-mode acceptEdits\n')
        bindir = fake_ps(self.tmp, table)
        with mock.patch.dict(os.environ, {'PATH': self._path(bindir)}):
            observed, why = observe.read({}, [])
        self.assertEqual(why, '')
        self.assertEqual([o.pid for o in observed], [100])

    def test_owner_and_account(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'acct-a', 'config_dir': '/cfg/acct-a'}]}}
        accounts = pool_mod.accounts_from_config(cfg)
        table = ('  100     1 /usr/bin/claude ASF_SESSION=a/j@20260922T101500Z '
                 'CLAUDE_CONFIG_DIR=/cfg/acct-a\n'
                 '  200     1 /usr/bin/claude CLAUDE_CONFIG_DIR=/cfg/acct-a\n'
                 '  300     1 /usr/bin/claude CLAUDE_CONFIG_DIR=/cfg/unknown\n')
        bindir = fake_ps(self.tmp, table)
        with mock.patch.dict(os.environ, {'PATH': self._path(bindir)}):
            observed, why = observe.read(cfg, accounts)
        self.assertEqual(why, '')
        by_pid = {o.pid: o for o in observed}
        self.assertEqual((by_pid[100].owner, by_pid[100].account, by_pid[100].product),
                         ('asf', 'acct-a', 'a'))
        self.assertEqual((by_pid[200].owner, by_pid[200].account), ('foreign', 'acct-a'))
        self.assertIsNone(by_pid[300].account)

    def test_command_source(self):
        script = os.path.join(self.tmp, 'sessions-cmd')
        with open(script, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n'
                    'echo \'{"pid": 100, "ppid": 1, "env": '
                    '{"ASF_SESSION": "a/j@20260922T101500Z"}}\'\n'
                    'echo \'{"pid": 200, "ppid": 1, "env": {}}\'\n')
        os.chmod(script, 0o755)
        cfg = {'worker_pool': {'sessions': 'command', 'sessions_command': script}}
        observed, why = observe.read(cfg, [])
        self.assertEqual(why, '')
        by_pid = {o.pid: o for o in observed}
        self.assertEqual(sorted(by_pid), [100, 200])
        self.assertEqual(by_pid[100].owner, 'asf')
        self.assertEqual(by_pid[200].owner, 'foreign')

    def test_unreadable(self):
        bindir = fake_ps(self.tmp, None)
        with mock.patch.dict(os.environ, {'PATH': self._path(bindir)}):
            observed, why = observe.read({}, [])
        self.assertEqual(observed, [])
        self.assertNotEqual(why, '')


class PoolAcrossProductsTest(Home):
    """S-8154: an account's cap is the machine's, so load is summed over every product's
    registry and over the observed sessions no registry knows, foreign ones included, counting
    each seat once; the wave's ``already running`` check is per product.
    """

    def other(self):
        """Product ``b``, sharing this test's ``ASF_HOME``."""
        return env.Product('b', {'repo_dir': self.repo, 'main': 'main',
                                 'job_grants': [self.grant]})

    def register(self, product, job, account='acct-a', **fields):
        pool_mod.append_session(product, dict({'job': job, 'account': account,
                                               'product': product.name, 'started': '2026-09-23',
                                               'pid': 4242}, **fields))

    def test_other_products_sessions_count(self):
        self.register(self.other(), 'spec-f-0002')
        p = pool_mod.Pool.from_config(self.cfg, self.product)
        self.assertEqual(p.load(self.acct()), 1)

    def test_just_launched_counts_before_ps_sees_it(self):
        # No process ever existed at this pid: the registry line alone is the seat it holds.
        self.register(self.product, 'spec-f-0001', pid=999999)
        p = pool_mod.Pool.from_config(self.cfg, self.product)
        self.assertEqual(p.load(self.acct()), 1)

    def test_an_ended_run_frees_its_seat(self):
        other = self.other()
        self.register(other, 'spec-f-0002')
        pool_mod.update_session(other, 'spec-f-0002', ended='2026-09-23', end_reason='finished')
        p = pool_mod.Pool.from_config(self.cfg, self.product)
        self.assertEqual(p.load(self.acct()), 0)

    def test_same_job_name_in_two_products(self):
        """``spec-f-0001`` live under ``b`` never blocks ``a``'s own (D5/D12)."""
        self.register(self.other(), 'spec-f-0001')
        pool = pool_mod.Pool.from_config(self.cfg, self.product,
                                         quota_source=quota_mod.FakeQuotaSource({}))
        lines = []
        rt = runtime_mod.FakeRuntime([{'running': True}] * 10)
        launched, waits = wave_mod.wave(self.product, [feature_row('spec-f-0001')], 5, pool=pool,
                                        runtime=rt, cfg=self.cfg, out=lines.append)
        self.assertEqual([r.job for r, _ in launched], ['spec-f-0001'])
        self.assertEqual(waits, [])

    def test_the_same_job_in_this_product_still_waits(self):
        self.register(self.product, 'spec-f-0001')
        pool = pool_mod.Pool.from_config(self.cfg, self.product,
                                         quota_source=quota_mod.FakeQuotaSource({}))
        launched, waits = wave_mod.wave(self.product, [feature_row('spec-f-0001')], 5, pool=pool,
                                        runtime=runtime_mod.FakeRuntime([{'running': True}]),
                                        cfg=self.cfg, out=lambda s: None)
        self.assertEqual(launched, [])
        self.assertEqual([(r.job, why) for r, why in waits], [('spec-f-0001', 'already running')])

    def test_take_records_the_product(self):
        pool = pool_mod.Pool([self.acct()], quota_source=quota_mod.FakeQuotaSource({}))
        pool.take(self.acct(), 'opus', 'spec-f-0001', product='b')
        self.assertEqual(pool.live[-1]['product'], 'b')

    def test_a_hand_built_pool_is_readable(self):
        self.assertEqual(pool_mod.Pool([self.acct()]).unreadable, '')


if __name__ == '__main__':
    unittest.main()
