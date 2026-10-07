"""asf.workers — runtime command line, pick rule, S1 reserve, spawn, wave, health, stall,
cold-retry correction. Every run goes through the fake runtime; git is a bare repo in a temp
dir; no network, no account, no real product."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from asf import ci_flight
from asf import env
from asf import hooks as hooks_mod
from asf import progress
from asf.improve.measure import Run
from asf.scorecard import diagnose, score
from asf.scorecard.facts import Facts, to_dt
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stall as stall_mod
from asf.workers import wave as wave_mod
from asf.workers import cmd_quota, register
from asf.feeder import rows
from asf.tick.step_wave import corrections as step_wave_corrections

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_workers` does not
    from gitfixture import Template
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'workers')


def git(*args, cwd):
    p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f'git {args}: {p.stderr}')
    return p.stdout.strip()


def feature_row(job, item='F-0001'):
    return pool_mod.parse_row(f'STARVED → SPEC {item} "a feature"   → launch {job} (Opus)')


def s1_row(job='fix-b-0001', item='B-0001', sev='S1'):
    return pool_mod.parse_row(f'BUG → FIX {item} "a bug" ({sev})   → launch {job} (Opus)')


def _cfg_with_sampler(cfg):
    """``cfg`` with the sampler turned back on — ``Home.cfg`` disables it so the other spawn
    tests never fork a real detached process (F-0066 Task 5)."""
    return {**cfg, 'worker_pool': {k: v for k, v in cfg['worker_pool'].items()
                                   if k != 'progress_sampler'}}


def _build_home_repos(tmp):
    """A bare origin with one seed commit and its clone — built once, copied per test (B-0071)."""
    origin = os.path.join(tmp, 'origin.git')
    seed = os.path.join(tmp, 'seed')
    git('init', '-q', '--bare', '-b', 'main', origin, cwd=tmp)
    git('init', '-q', '-b', 'main', seed, cwd=tmp)
    for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
        git('config', k, v, cwd=seed)
    with open(os.path.join(seed, 'README'), 'w') as f:
        f.write('seed\n')
    git('add', '.', cwd=seed)
    git('commit', '-q', '-m', 'seed', cwd=seed)
    git('push', '-q', origin, 'main', cwd=seed)
    git('clone', '-q', origin, os.path.join(tmp, 'repo'), cwd=tmp)


HOME_REPOS = Template(_build_home_repos, prefix='workers_home_')


class Home(unittest.TestCase):
    """A temp ASF_HOME with a product whose repo is a clone of a bare origin."""

    def setUp(self):
        self.tmp = HOME_REPOS.fresh()
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'asf-home')
        self.repo = os.path.join(self.tmp, 'repo')
        self.grant = os.path.join(self.tmp, 'grant')
        os.makedirs(self.grant)
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                              'job_grants': [self.grant],
                                              'stage_limits': {'silent_min': 30}})
        self.cfg = {'worker_pool': {'accounts': [{'name': 'acct-a', 'role': 'local', 'cap': 2}],
                                    'models': {'Opus': 'opus'}, 'sessions': 'fake',
                                    # off by default: every other spawn test here would otherwise
                                    # fork a real detached sampler process (F-0066 Task 5); the
                                    # sampler-specific tests turn it back on with _cfg_with_sampler
                                    'progress_sampler': False}}
        # asf.hooks.ensure_git_hooks is Task 8353's (F-0075) and is not yet in this checkout;
        # a test that cares about the push-gate check overrides this with its own
        # mock.patch.object(hooks_mod, 'ensure_git_hooks', ..., create=True) around its call.
        patcher = mock.patch.object(hooks_mod, 'ensure_git_hooks', create=True,
                                    return_value=(True, 'ok'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def acct(self):
        return pool_mod.Account('acct-a', cap=2, config_dir='/cfg/acct-a')

    def _held_branch_behind_main(self, name, text_on_main='main\n', shape='plain'):
        """``fix/<name>`` on origin with one commit of its own, and ``main`` one commit past its
        base. ``shape``: ``plain``; ``merge`` — the branch merged the trunk in; ``copy`` — the
        branch carries a cherry-picked copy of a trunk commit; ``conflict`` — main's commit
        rewrites the branch's own file. Returns the clone the pushes were made from."""
        def commit(cwd, fname, text, msg):
            with open(os.path.join(cwd, fname), 'w') as f:
                f.write(text)
            git('add', '.', cwd=cwd)
            git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'commit', '-q', '-m', msg,
                cwd=cwd)
        other = os.path.join(self.tmp, f'other-{name}')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        branch = f'fix/{name}'
        git('checkout', '-q', '-b', branch, cwd=other)
        commit(other, 'a.txt', 'branch\n', 'held work')
        git('checkout', '-q', 'main', cwd=other)
        commit(other, 'a.txt' if shape == 'conflict' else f'b-{name}.txt', text_on_main,
               'main moves on')
        git('push', '-q', 'origin', 'main', cwd=other)
        git('checkout', '-q', branch, cwd=other)
        if shape == 'merge':
            git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'merge', '-q',
                '--no-edit', 'main', cwd=other)
        elif shape == 'copy':
            main_sha = git('rev-parse', 'main', cwd=other)
            git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'cherry-pick', main_sha,
                cwd=other)
            commit(other, 'c.txt', 'more\n', 'more held work')
        git('push', '-q', 'origin', branch, cwd=other)
        return other, branch


class TestRuntime(unittest.TestCase):
    def test_command_line_golden(self):
        job = runtime_mod.Job('sample', 'spec-f-0031', '/wt/spec-f-0031', '/b/spec-f-0031.md',
                              'opus', add_dirs=['/grant/a', '/grant/b'],
                              permission_mode='bypassPermissions')
        self.assertEqual(runtime_mod.build_command(job), [
            'claude', '-p', '--permission-mode', 'bypassPermissions',
            '--add-dir', '/grant/a', '--add-dir', '/grant/b', '--model', 'opus',
            '--output-format', 'stream-json', '--verbose'])

    def test_command_line_carries_the_settings_file(self):
        job = runtime_mod.Job('sample', 'j', '/wt', '/b.md', 'opus', settings_file='/cfg/settings.json')
        cmd = runtime_mod.build_command(job)
        self.assertEqual(cmd[cmd.index('--settings') + 1], '/cfg/settings.json')

    def test_settings_file_must_exist(self):
        from asf.workers import spawn as spawn_mod
        self.assertIsNone(spawn_mod.settings_file({}))
        with self.assertRaises(FileNotFoundError):
            spawn_mod.settings_file({'settings_file': '/nowhere/settings.json'})

    def test_env_isolates_the_account_and_carries_the_id_range(self):
        acct = pool_mod.Account('acct-a', home='/homes/a', config_dir='/cfg/a')
        job = runtime_mod.Job('sample', 'j1', '/wt', '/b.md', 'opus', account=acct,
                              env={'BACKLOG_ID_RANGE': 'S:5000-5049'})
        e = runtime_mod.build_env(job, base={'PATH': '/bin', 'HOME': '/me'})
        # the builder (asf.hermetic) pins git's default branch for every child of ASF
        self.assertEqual({k: v for k, v in e.items() if not k.startswith('GIT_CONFIG_')},
                         {'PATH': '/bin', 'HOME': '/homes/a', 'CLAUDE_CONFIG_DIR': '/cfg/a',
                          'ASF_PRODUCT': 'sample', 'ASF_JOB': 'j1', 'ASF_HOME': env.ASF_HOME,
                          'BACKLOG_ID_RANGE': 'S:5000-5049'})
        self.assertEqual((e['GIT_CONFIG_KEY_0'], e['GIT_CONFIG_VALUE_0']), ('init.defaultBranch', 'main'))

    def test_claude_code_backend_runs_the_command_and_reads_the_last_line(self):
        tmp = tempfile.mkdtemp()
        try:
            fake_bin = os.path.join(tmp, 'agent')
            with open(fake_bin, 'w') as f:
                f.write('#!/bin/sh\ncat >/dev/null\necho \'{"type":"system"}\'\n'
                        'echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",'
                        '\\"result\\":\\"$ASF_JOB\\"}"\n')
            os.chmod(fake_bin, 0o755)
            brief = os.path.join(tmp, 'b.md')
            with open(brief, 'w') as f:
                f.write('do it\n')
            log = os.path.join(tmp, 'j.jsonl')
            job = runtime_mod.Job('sample', 'j9', tmp, brief, 'opus', log_path=log)
            r = runtime_mod.ClaudeCodeRuntime(binary=fake_bin).run(job, wait=True)
            self.assertTrue(r.ok)
            self.assertEqual(r.text, 'j9')
        finally:
            shutil.rmtree(tmp)

    def test_a_session_started_without_wait_is_never_the_callers_child(self):
        # a tick that spawned sessions kept them as children: one that ended sat <defunct>
        tmp = tempfile.mkdtemp()
        try:
            fake_bin = os.path.join(tmp, 'agent')
            with open(fake_bin, 'w') as f:
                f.write('#!/bin/sh\ncat >/dev/null\nsleep 1\n'
                        'echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",'
                        '\\"result\\":\\"$ASF_JOB\\"}"\n')
            os.chmod(fake_bin, 0o755)
            brief = os.path.join(tmp, 'b.md')
            with open(brief, 'w') as f:
                f.write('do it\n')
            log = os.path.join(tmp, 'j.jsonl')
            job = runtime_mod.Job('sample', 'j8', tmp, brief, 'opus', log_path=log)
            r = runtime_mod.ClaudeCodeRuntime(binary=fake_bin).run(job)
            self.assertEqual(os.getpgid(r.pid), r.pid)  # its own group: what stop signals
            with self.assertRaises(ChildProcessError):
                os.waitpid(r.pid, os.WNOHANG)
            deadline = time.monotonic() + 15
            while runtime_mod.read_result(log) is None and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(runtime_mod.read_result(log)['result'], 'j8')
        finally:
            shutil.rmtree(tmp)

    def test_a_detached_command_that_cannot_start_is_an_oserror(self):
        from asf import detach
        with self.assertRaises(OSError):
            detach.spawn(['/nowhere/agent'], stderr=subprocess.DEVNULL)

    def test_b0028_result_followed_by_system_lines_is_still_the_result(self):
        # the runtime writes background-task system lines after the result line
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'j.jsonl')
            with open(log, 'w') as f:
                for rec in ({'type': 'system', 'subtype': 'init'},
                            {'type': 'assistant'},
                            {'type': 'result', 'subtype': 'success', 'is_error': False},
                            {'type': 'system', 'subtype': 'background_tasks_changed'},
                            {'type': 'system', 'subtype': 'task_updated'},
                            {'type': 'system', 'subtype': 'task_notification'}):
                    f.write(json.dumps(rec) + '\n')
            rec = runtime_mod.read_result(log)
            self.assertIsNotNone(rec)
            self.assertTrue(runtime_mod.result_ok(rec))
            # a second run (a correction) appended after it is a new run: no result until it ends
            with open(log, 'a') as f:
                f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
                f.write(json.dumps({'type': 'assistant'}) + '\n')
            self.assertIsNone(runtime_mod.read_result(log))
            with open(log, 'a') as f:
                f.write(json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True}) + '\n')
            self.assertFalse(runtime_mod.result_ok(runtime_mod.read_result(log)))

    def test_a_cloud_relaunch_never_reads_the_earlier_local_runs_result(self):
        # a cloud launch writes only asf header lines, never a system/init: its header is the
        # boundary, or the earlier local run's result is read as the live cloud run's own
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'j.jsonl')
            local = ({'type': 'asf', 'subtype': 'session', 'session': 'p/j@20261004T100000Z'},
                     {'type': 'system', 'subtype': 'init', 'session_id': 'local-1'},
                     {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'old'})
            cloud = ({'type': 'asf', 'subtype': 'session', 'session': 'p/j@20261004T200000Z'},
                     {'type': 'asf', 'subtype': 'cloud', 'runtime': 'claude-remote',
                      'trigger': 't1', 'run': 'r1'})
            with open(log, 'w') as f:
                for rec in local + cloud:
                    f.write(json.dumps(rec) + '\n')
            self.assertIsNone(runtime_mod.read_result(log))
            self.assertIsNone(runtime_mod.init_line(log))
            self.assertEqual(runtime_mod.runtime_session(log), '')
            # the cloud run's own result, appended when it finishes, is the one that counts
            with open(log, 'a') as f:
                f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                    'result': 'new'}) + '\n')
                # a later status line of the same run is no new launch
                f.write(json.dumps({'type': 'asf', 'subtype': 'cloud', 'status': 'dead',
                                    'why': 'x'}) + '\n')
            self.assertEqual(runtime_mod.read_result(log)['result'], 'new')

    def test_a_header_only_launch_without_a_session_id_is_still_a_boundary(self):
        # an actions-lane launch of a job with no session id writes only the cloud header
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'j.jsonl')
            with open(log, 'w') as f:
                for rec in ({'type': 'system', 'subtype': 'init'},
                            {'type': 'result', 'subtype': 'success', 'is_error': False},
                            {'type': 'asf', 'subtype': 'cloud', 'runtime': 'actions'}):
                    f.write(json.dumps(rec) + '\n')
            self.assertIsNone(runtime_mod.read_result(log))

    def test_fake_runtime_replays_a_fixture(self):
        rt = runtime_mod.FakeRuntime(path=os.path.join(FIXTURES, 'fake-results.json'))
        self.assertEqual([s['ok'] for s in rt.script], [False, True])


class TestQuota(unittest.TestCase):
    def test_default_guards_are_the_band(self):
        g = quota_mod.guards_from_config({})
        self.assertEqual(g['stop'], {'five_h': 95, 'seven_d': 95, 'seven_d_model': 95})
        self.assertEqual(g['cooldown'], {'five_h': 90, 'seven_d': 90, 'seven_d_model': 90})

    def test_the_old_forms_set_the_stop_and_derive_the_cooldown(self):
        g = quota_mod.guards_from_config({'quota_guards': {'five_h': 80}})
        self.assertEqual((g['stop']['five_h'], g['cooldown']['five_h']), (80, 75))
        self.assertEqual((g['stop']['seven_d'], g['cooldown']['seven_d']), (95, 90))
        old = quota_mod.guards_from_config({'worker_pool': {'quota_guard': {'max_7d': 0.5}}})
        self.assertEqual((old['stop']['seven_d'], old['cooldown']['seven_d']), (50, 45))

    def test_a_named_band_wins_and_is_clamped_to_its_own_stop(self):
        g = quota_mod.guards_from_config(
            {'quota_guards': {'stop': {'seven_d': 96}, 'cooldown': {'seven_d': 88}}})
        self.assertEqual((g['stop']['seven_d'], g['cooldown']['seven_d']), (96, 88))
        g = quota_mod.guards_from_config({'quota_guards': {'cooldown': {'five_h': 99}}})
        self.assertEqual(g['cooldown']['five_h'], 95)          # never above the stop

    def test_band(self):
        g = quota_mod.guards_from_config({})
        self.assertEqual(quota_mod.band({'five_h_pct': 89, 'seven_d_pct': 89}, g), ('free', ''))
        self.assertEqual(quota_mod.band({'five_h_pct': 0, 'seven_d_pct': 90}, g),
                         ('cooldown', 'seven_d_pct 90 ≥ 90'))
        self.assertEqual(quota_mod.band({'five_h_pct': 94.5, 'seven_d_pct': 0}, g),
                         ('cooldown', 'five_h_pct 94.5 ≥ 90'))
        self.assertEqual(quota_mod.band({'five_h_pct': 0, 'seven_d_pct': 95}, g),
                         ('stop', 'seven_d_pct 95 ≥ 95'))
        self.assertEqual(quota_mod.band({'seven_d_model_pct': 96}, g)[0], 'stop')
        self.assertEqual(quota_mod.band(None, g), ('stop', 'quota unreadable'))

    def test_a_stop_anywhere_beats_a_cooldown_in_an_earlier_window(self):
        g = quota_mod.guards_from_config({})
        self.assertEqual(quota_mod.band({'five_h_pct': 91, 'seven_d_pct': 97}, g),
                         ('stop', 'seven_d_pct 97 ≥ 95'))

    def test_under_guard_is_gone(self):
        self.assertFalse(hasattr(quota_mod, 'under_guard'))

    def test_command_source_parses_one_json_line(self):
        cmd = (f'{sys.executable} -c "import json,sys; print(\'noise\'); '
               f'print(json.dumps(dict(five_h_pct=len(sys.argv[1]), seven_d_pct=3)))" {{account}}')
        self.assertEqual(quota_mod.CommandQuotaSource(cmd).read(pool_mod.Account('abcd')),
                         {'five_h_pct': 4, 'seven_d_pct': 3})
        self.assertIsNone(quota_mod.CommandQuotaSource(f'{sys.executable} -c "print(1/0)"')
                          .read(pool_mod.Account('a')))


class ModelFallbackTests(unittest.TestCase):
    def cfg(self, **models):
        return {'worker_pool': {'models': models}}

    def test_cheap_uses_its_own_entry(self):
        self.assertEqual(spawn_mod.model_arg('cheap', self.cfg(light='l', cheap='c')), 'c')

    def test_cheap_falls_back_to_light(self):
        self.assertEqual(spawn_mod.model_arg('cheap', self.cfg(heavy='h', light='l')), 'l')

    def test_cheap_with_neither_is_refused(self):
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            spawn_mod.model_arg('cheap', self.cfg(heavy='h'))
        self.assertIn('no entry for cheap', str(cm.exception))

    def test_other_labels_do_not_fall_back(self):
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.model_arg('heavy', self.cfg(light='l'))


class TestRows(unittest.TestCase):
    def test_parse_feeder_rows(self):
        with open(os.path.join(FIXTURES, 'rows.txt'), encoding='utf-8') as f:
            rows = [pool_mod.parse_row(ln) for ln in f]
        self.assertEqual([(r.job, r.item, r.kind, r.severity, r.is_fix) for r in rows], [
            ('spec-f-0031', 'F-0031', 'spec', None, False),
            ('fix-b-0012', 'B-0012', 'fix-bug', 'S1', True),
            ('plan-f-0032', 'F-0032', 'plan', None, False)])
        self.assertTrue(pool_mod.s1_open(rows))
        self.assertIsNone(pool_mod.parse_row('not a row'))
        self.assertEqual(pool_mod.parse_row('{"job": "j", "item": "T-1"}').job, 'j')


class TestPick(unittest.TestCase):
    def pool(self, accounts, live=(), usage=None, reserve=None):
        return pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource(usage or {}),
                             reserve=reserve, live=live)

    def test_lowest_load_wins_ties_by_name(self):
        a, b = pool_mod.Account('a', cap=3), pool_mod.Account('b', cap=3)
        p = self.pool([b, a], live=[{'account': 'a'}])
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'b')
        p = self.pool([b, a])
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'a')

    def test_caps_and_model_caps(self):
        a = pool_mod.Account('a', cap=2, caps={'opus': 1})
        p = self.pool([a], live=[{'account': 'a', 'model': 'opus'}])
        self.assertEqual(p.pick_account('spec', 'Opus'),
                         (None, 'pool full — accounts at cap: a 1/1 opus'))
        self.assertEqual(p.pick_account('spec', 'sonnet')[0].name, 'a')

    def test_over_guard_is_needs_operator_reason_not_exception(self):
        a = pool_mod.Account('a', cap=2)
        p = self.pool([a], usage={'a': {'five_h_pct': 95, 'seven_d_pct': 10}})
        acct, reason = p.pick_account('spec', 'opus')
        self.assertIsNone(acct)
        self.assertTrue(reason.startswith('NEEDS OPERATOR: no account under quota — '))

    def test_guard_skips_one_account(self):
        a, b = pool_mod.Account('a', cap=2), pool_mod.Account('b', cap=2)
        p = self.pool([a, b], usage={'a': {'five_h_pct': 0, 'seven_d_pct': 99}})
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'b')

    def test_cooldown_lets_one_job_through_then_holds(self):
        a = pool_mod.Account('a', cap=3)
        p = self.pool([a], usage={'a': {'five_h_pct': 0, 'seven_d_pct': 91}})
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'a')
        p.take(a, 'opus', 'j1')
        self.assertEqual(p.pick_account('spec', 'opus'), (None, pool_mod.REASON_COOLDOWN))

    def test_a_cooling_account_that_is_already_busy_takes_nothing(self):
        a = pool_mod.Account('a', cap=3)
        p = self.pool([a], live=[{'account': 'a'}], usage={'a': {'seven_d_pct': 91}})
        self.assertEqual(p.pick_account('spec', 'opus'), (None, pool_mod.REASON_COOLDOWN))

    def test_at_the_stop_nothing_goes_through_and_it_pages(self):
        a = pool_mod.Account('a', cap=3)
        p = self.pool([a], usage={'a': {'five_h_pct': 0, 'seven_d_pct': 95}})
        acct, reason = p.pick_account('spec', 'opus')
        self.assertIsNone(acct)
        self.assertTrue(reason.startswith('NEEDS OPERATOR: no account under quota — '))

    def test_a_free_account_is_preferred_over_a_cooling_one(self):
        a, b = pool_mod.Account('a', cap=3), pool_mod.Account('b', cap=3)
        p = self.pool([a, b], live=[{'account': 'b'}, {'account': 'b'}],
                      usage={'a': {'seven_d_pct': 92}})
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'b')   # busier, but free

    def test_cooldown_beats_the_needs_operator_line(self):
        a, b = pool_mod.Account('a', cap=3), pool_mod.Account('b', cap=3)
        p = self.pool([a, b], live=[{'account': 'b'}],
                      usage={'a': {'seven_d_pct': 99}, 'b': {'seven_d_pct': 92}})
        self.assertEqual(p.pick_account('spec', 'opus'), (None, pool_mod.REASON_COOLDOWN))

    def test_seats_at_cap_beside_stopped_accounts_is_a_wait_not_a_page(self):
        # a product's coder row, 2026-09-25: two accounts at 4/4 under their 5h guard, two stopped on the
        # 7d window — the row waits for a seat, it does not page
        acc = [pool_mod.Account(n, cap=4) for n in ('a', 'b', 'c', 'd')]
        live = [{'account': n} for n in ('a', 'c') for _ in range(4)]
        p = self.pool(acc, live=live,
                      usage={'a': {'five_h_pct': 46, 'seven_d_pct': 38},
                             'c': {'five_h_pct': 18, 'seven_d_pct': 1},
                             'b': {'five_h_pct': 0, 'seven_d_pct': 100},
                             'd': {'five_h_pct': 0, 'seven_d_pct': 99}})
        acct, reason = p.pick_account('coder', 'sonnet')
        self.assertIsNone(acct)
        self.assertNotIn('NEEDS OPERATOR', reason)
        self.assertEqual(reason, 'pool full — accounts at cap: a 4/4, c 4/4; the rest '
                                 'stopped: b (seven_d_pct 100 ≥ 95), d (seven_d_pct 99 ≥ 95)')

    def test_an_account_at_cap_and_stopped_still_pages(self):
        a, b = pool_mod.Account('a', cap=1), pool_mod.Account('b', cap=1)
        p = self.pool([a, b], live=[{'account': 'a'}],
                      usage={'a': {'seven_d_pct': 99}, 'b': {'seven_d_pct': 99}})
        self.assertEqual(p.pick_account('spec', 'opus'), (None, pool_mod.REASON_NO_QUOTA))

    def test_an_unreadable_account_is_stopped_not_cooling(self):
        a = pool_mod.Account('a', cap=3)
        p = self.pool([a], usage={'a': None})
        self.assertEqual(p.pick_account('spec', 'opus'), (None, pool_mod.REASON_NO_QUOTA))

    def test_the_band_is_applied_after_the_s1_reserve(self):
        a = pool_mod.Account('a', role='local', cap=3)
        p = self.pool([a], live=[{'account': 'a'}, {'account': 'a'}],
                      usage={'a': {'seven_d_pct': 96}})
        # the reserved slot exists, but the account is past its stop — the S1 fix waits too
        self.assertEqual(p.pick_account('fix-bug', 'opus', is_fix=True, s1_is_open=True),
                         (None, pool_mod.REASON_NO_QUOTA))

    def test_reserve_rule(self):
        a = pool_mod.Account('a', role='local', cap=3)
        p = self.pool([a], live=[{'account': 'a'}, {'account': 'a'}])
        # one free slot, an S1 open → a Feature row waits, the S1 fix gets it
        self.assertEqual(p.pick_account('spec', 'opus', s1_is_open=True),
                         (None, 'reserved for S1'))
        self.assertEqual(p.pick_account('fix-bug', 'opus', is_fix=True, s1_is_open=True)[0].name, 'a')
        # no S1 open → the slot is anyone's
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'a')

    def test_the_local_lane_is_every_account_not_role_cloud(self):
        w = pool_mod.Account('w', role='worker', cap=2)
        c = pool_mod.Account('c', role='cloud', cap=2)
        p = self.pool([c, w])
        self.assertEqual(p.pick_account('spec', 'opus', lane='local')[0].name, 'w')
        self.assertEqual(p.pick_account('spec', 'opus', lane='cloud')[0].name, 'c')

    def test_no_account_in_the_lane_says_so(self):
        c = pool_mod.Account('c', role='cloud', cap=2)
        acct, why = self.pool([c]).pick_account('spec', 'opus', lane='local')
        self.assertIsNone(acct)
        self.assertEqual(why, 'pool full — no account serves the local lane '
                              '(worker_pool.accounts roles: c cloud)')

    def test_a_full_pool_always_names_the_accounts_at_cap(self):
        a = pool_mod.Account('a', cap=1)
        p = self.pool([a], live=[{'account': 'a', 'model': 'sonnet'}])
        self.assertEqual(p.pick_account('spec', 'opus'),
                         (None, 'pool full — accounts at cap: a 1/1'))

    def test_the_local_reserve_holds_on_worker_role_accounts(self):
        w = pool_mod.Account('w', role='worker', cap=2)
        p = self.pool([w], live=[{'account': 'w'}], reserve={'local': 1, 'cloud': 0})
        self.assertEqual(p.pick_account('spec', 'opus', s1_is_open=True), (None, 'reserved for S1'))
        self.assertEqual(p.pick_account('fix-bug', 'opus', is_fix=True, s1_is_open=True)[0].name,
                         'w')

    def test_reserve_is_per_lane(self):
        loc = pool_mod.Account('l', role='local', cap=1)
        cl = pool_mod.Account('c', role='cloud', cap=2)
        p = self.pool([loc, cl], reserve={'local': 0, 'cloud': 1})
        self.assertEqual(p.pick_account('spec', 'opus', s1_is_open=True)[0].name, 'c')
        p.take(cl, 'opus')
        self.assertEqual(p.pick_account('spec', 'opus', s1_is_open=True)[0].name, 'l')


class TestSpawn(Home):
    def test_spawn_worktree_ranges_brief_and_ledger(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        row = s1_row()
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'fix the bug\n', runtime=rt, cfg=self.cfg)
        wt = os.path.join(env.ASF_HOME, 'state', 'sample', 'worktrees', 'fix-b-0001')
        self.assertEqual(rec['worktree'], wt)
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), 'fix-bug/fix-b-0001')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), git('rev-parse', 'origin/main', cwd=self.repo))
        job, brief = rt.calls[0]
        self.assertEqual(job.add_dirs, [self.grant])
        self.assertEqual(job.cwd, wt)
        self.assertEqual(job.env['BACKLOG_ID_RANGE'], 'S:5000-5049,T:5000-5049,B:5000-5049')
        self.assertEqual(job.env['ASF_READ_ROOTS'], self.grant)
        self.assertTrue(brief.startswith('Kind: fix-bug — B-0001 (S1)\nHarvest requires the named test:'))
        self.assertTrue(brief.endswith('fix the bug\n'))
        self.assertTrue(os.path.exists(os.path.join(env.ASF_HOME, 'state', 'sample', 'briefs',
                                                    'fix-b-0001.md')))
        sessions = pool_mod.load_sessions(self.product)
        s = sessions['fix-b-0001']
        for k in pool_mod.SESSION_FIELDS:
            self.assertIn(k, s)
        self.assertEqual((s['pid'], s['account'], s['kind'], s['model'], s['item']),
                         (4242, 'acct-a', 'fix-bug', 'opus', 'B-0001'))

    def test_the_ledger_line_keeps_the_card_digest_the_brief_was_built_from(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        row = s1_row()
        row.card_digest = 'abcd1234abcd1234'
        spawn_mod.spawn(self.product, row, self.acct(), 'fix the bug\n', runtime=rt, cfg=self.cfg)
        self.assertEqual(pool_mod.load_sessions(self.product)['fix-b-0001']['card_digest'],
                         'abcd1234abcd1234')

    def test_the_ledger_line_keeps_the_head_a_held_branch_was_launched_on(self):
        """The loop guard (:func:`asf.workers.lifecycle.same_head_loop`) counts launches on one
        head: a branch already on origin records its sha as ``launch_head``; a fresh one none."""
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        spawn_mod.spawn(self.product, s1_row(), self.acct(), 'fix\n', runtime=rt, cfg=self.cfg)
        self.assertNotIn('launch_head', pool_mod.load_sessions(self.product)['fix-b-0001'])
        git('push', '-q', 'origin', 'origin/main:refs/heads/fix-bug/fix-b-0002', cwd=self.repo)
        row = s1_row(job='fix-b-0002', item='B-0002')
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4243}])
        spawn_mod.spawn(self.product, row, self.acct(), 'fix\n', runtime=rt, cfg=self.cfg)
        self.assertEqual(pool_mod.load_sessions(self.product)['fix-b-0002']['launch_head'],
                         git('rev-parse', 'origin/main', cwd=self.repo))

    def test_a_row_grants_its_own_directories_and_they_exist(self):
        # the groom brief grants the answers file's directory (PD7), but spawn passed only the
        # product's job_grants: the adjudicate session could not write its answers, and staged
        # them in its worktree instead (groom-2026-09-22)
        answers_dir = os.path.join(env.ASF_HOME, 'state', 'sample', 'groom')
        row = s1_row()
        row.add_dirs = [answers_dir, self.grant]
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4243}])
        spawn_mod.spawn(self.product, row, self.acct(), 'b\n', runtime=rt, cfg=self.cfg)
        job, _brief = rt.calls[0]
        self.assertEqual(job.add_dirs, [self.grant, answers_dir])
        self.assertTrue(os.path.isdir(answers_dir))

    def test_the_wave_row_carries_the_briefs_grants(self):
        from types import SimpleNamespace
        from asf.tick import step_wave
        frow = SimpleNamespace(kind='GROOM → ADJUDICATE', item_id='F-0001', feature_id='F-0001',
                               branch='groom/2026-01-01', groom_date='2026-01-01')
        brief = SimpleNamespace(kind='groom', model='opus', add_dirs=['/x/groom'])
        self.assertEqual(step_wave.worker_row(frow, brief, {}).add_dirs, ['/x/groom'])

    def test_b0046_correct_row_spawns_on_the_held_branch_not_rebased_when_it_merges_clean(self):
        # 2026-09-29: the launch rebase onto a moved trunk was published at once — a push that
        # restarted the PR's CI with the branch's own changes byte-identical (25 of 57 superseded
        # runs). A held branch that merges cleanly is taken as origin holds it: nothing pushed.
        def commit(cwd, name, text, msg):
            with open(os.path.join(cwd, name), 'w') as f:
                f.write(text)
            git('add', '.', cwd=cwd)
            git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'commit', '-q', '-m', msg,
                cwd=cwd)
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        git('checkout', '-q', '-b', 'fix/B-0046', cwd=other)
        commit(other, 'a.txt', 'branch\n', 'held work')
        git('push', '-q', 'origin', 'fix/B-0046', cwd=other)
        git('checkout', '-q', 'main', cwd=other)
        commit(other, 'b.txt', 'main\n', 'main moves on')
        git('push', '-q', 'origin', 'main', cwd=other)
        row = pool_mod.Row('correct-b-0046', 'B-0046', kind='correct', branch='fix/B-0046')
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 1}])
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'fix it\n', runtime=rt, cfg=self.cfg)
        wt = rec['worktree']
        self.assertTrue(wt.endswith(os.path.join('worktrees', 'correct-b-0046')))
        self.assertEqual(rec['branch'], 'fix/B-0046')
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), 'fix/B-0046')
        self.assertTrue(os.path.exists(os.path.join(wt, 'a.txt')))
        self.assertFalse(os.path.exists(os.path.join(wt, 'b.txt')))  # not rebased: not needed
        held = git('rev-parse', 'fix/B-0046', cwd=other)
        # nothing published: origin still holds the held head, and the session's own push is a
        # fast-forward of it
        self.assertEqual(git('ls-remote', '--heads', 'origin', 'fix/B-0046', cwd=wt).split()[0],
                         held)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), held)

    def test_a_held_branch_carrying_trunk_history_is_rebased_and_published(self):
        # what a rebase alone clears still gets it: a merge of the trunk, or a copy of a trunk
        # commit, on the branch (the lane would hold it, B-0056)
        for shape in ('merge', 'copy'):
            with self.subTest(shape=shape):
                name = f'B-{shape}'
                other, branch = self._held_branch_behind_main(name, shape=shape)
                row = pool_mod.Row(f'correct-{name.lower()}', name, kind='correct', branch=branch)
                rec = spawn_mod.spawn(self.product, row, self.acct(), 'fix it\n',
                                      runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 1}]),
                                      cfg=self.cfg)
                wt = rec['worktree']
                self.assertEqual(git('rev-list', '--merges', 'origin/main..HEAD', cwd=wt), '')
                git('merge-base', '--is-ancestor', 'origin/main', 'HEAD', cwd=wt)
                self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                                 git('rev-parse', 'HEAD', cwd=wt))

    def test_trunk_rebase_needed_names_only_what_a_rebase_clears(self):
        cases = {'plain': '', 'merge': 'merge commits above the trunk',
                 'copy': 'copies of trunk commits above the trunk',
                 'conflict': 'does not merge cleanly into the trunk'}
        for shape, want in cases.items():
            with self.subTest(shape=shape):
                other, branch = self._held_branch_behind_main(f'N-{shape}', 'trunk\n', shape)
                git('fetch', '-q', 'origin', cwd=other)
                git('checkout', '-q', branch, cwd=other)
                self.assertEqual(spawn_mod.trunk_rebase_needed(other, 'main'), want)

    def test_a_review_on_a_branch_behind_main_pushes_nothing(self):
        other, branch = self._held_branch_behind_main('B-review')
        held = git('rev-parse', branch, cwd=other)
        row = pool_mod.Row('review-b-review', 'B-review', kind='review', branch=branch)
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'review it\n',
                              runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 1}]),
                              cfg=self.cfg)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=rec['worktree']), held)
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=other).split()[0], held)

    def test_b0048_adjudicate_row_spawns_on_the_held_branch(self):
        """The reuse-a-held-branch rule (B-0046) was keyed on ``kind == 'correct'`` — a
        STALEMATE → ADJUDICATE row's kind is ``adjudicate``, so it took the fresh-branch path and
        silently lost the branch's own history. The check must be on the branch existing on
        origin, not on the row's kind (B-0048)."""
        def commit(cwd, name, text, msg):
            with open(os.path.join(cwd, name), 'w') as f:
                f.write(text)
            git('add', '.', cwd=cwd)
            git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'commit', '-q', '-m', msg,
                cwd=cwd)
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        git('checkout', '-q', '-b', 'fix/B-0048', cwd=other)
        commit(other, 'a.txt', 'branch\n', 'held work')
        git('push', '-q', 'origin', 'fix/B-0048', cwd=other)
        git('checkout', '-q', 'main', cwd=other)
        commit(other, 'b.txt', 'main\n', 'main moves on')
        git('push', '-q', 'origin', 'main', cwd=other)
        row = pool_mod.Row('adjudicate-b-0048', 'B-0048', kind='adjudicate', branch='fix/B-0048')
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 1}])
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'adjudicate it\n', runtime=rt, cfg=self.cfg)
        wt = rec['worktree']
        self.assertEqual(rec['branch'], 'fix/B-0048')
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), 'fix/B-0048')
        self.assertTrue(os.path.exists(os.path.join(wt, 'a.txt')))  # the held branch's own work
        self.assertFalse(os.path.exists(os.path.join(wt, 'b.txt')))  # merges clean: not rebased
        self.assertEqual(git('ls-remote', '--heads', 'origin', 'fix/B-0048', cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))

    def test_id_ranges_do_not_overlap_and_are_sticky(self):
        r1 = spawn_mod.reserve_id_range(self.product, 'j1', prefixes=['T'])
        r2 = spawn_mod.reserve_id_range(self.product, 'j2', prefixes=['T'])
        self.assertEqual((r1, r2), ('T:5000-5049', 'T:5050-5099'))
        self.assertEqual(spawn_mod.reserve_id_range(self.product, 'j1', prefixes=['T']), r1)

    def test_b0007_release_id_range_drops_only_that_jobs_row(self):
        spawn_mod.reserve_id_range(self.product, 'j1', prefixes=['T'])
        spawn_mod.reserve_id_range(self.product, 'j2', prefixes=['T'])
        self.assertTrue(spawn_mod.release_id_range(self.product, 'j1'))
        rows = spawn_mod._read_ranges(spawn_mod.id_ranges_path(self.product))
        self.assertEqual([j for j, _ in rows], ['j2'])
        # already gone: releasing again reports nothing to do
        self.assertFalse(spawn_mod.release_id_range(self.product, 'j1'))

    def test_existing_worktree_refuses(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': os.getpid()}])
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def test_b0051_ended_sessions_worktree_is_reused_not_refused(self):
        # a session that ended 'finished' without pushing (B-0051) must not permanently block
        # its item: the next spawn for the same job reuses the worktree as it stands — branch
        # and tree — instead of refusing it forever. The refusal stays only for a worktree whose
        # session is still live (B-0025). Since B-0056 the factory publishes the committed work
        # at health time, so the run is finished and pushed. A trunk that moved meanwhile is not
        # rebased onto: the branch merges cleanly, and a published rebase restarts the PR's CI.
        row = feature_row('again')
        rt = runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}])
        rec = spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)
        wt = rec['worktree']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'x'), 'w') as f:
            f.write('x')
        git('add', 'x', cwd=wt)
        git('commit', '-q', '-m', 'own work, never pushed', cwd=wt)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        s = pool_mod.load_sessions(self.product)['again']
        self.assertTrue(s.get('ended'))
        self.assertEqual(s['end_reason'], 'finished')  # B-0056: published by the factory
        self.assertEqual(git('ls-remote', '--heads', 'origin', rec['branch'], cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))
        # the trunk moves on while the worktree sits there
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        with open(os.path.join(other, 'trunk.txt'), 'w') as f:
            f.write('trunk moved\n')
        git('add', '.', cwd=other)
        git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'commit', '-q',
           '-m', 'main moves on', cwd=other)
        git('push', '-q', 'origin', 'main', cwd=other)
        rt2 = runtime_mod.FakeRuntime([{'running': True, 'pid': 41}])
        rec2 = spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt2, cfg=self.cfg)
        self.assertEqual(os.path.realpath(rec2['worktree']), os.path.realpath(wt))
        self.assertTrue(os.path.exists(os.path.join(wt, 'x')))
        self.assertFalse(os.path.exists(os.path.join(wt, 'trunk.txt')))  # not rebased
        self.assertEqual(git('ls-remote', '--heads', 'origin', rec['branch'], cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))  # origin's head, nothing pushed

    def _ended_run_then_a_newer_remote_head(self, job):
        """An ended run's worktree whose branch is on origin; a person then pushes a newer
        commit on top from elsewhere. Returns ``(worktree, branch, newer)``."""
        rec = spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}]), cfg=self.cfg)
        wt, branch = rec['worktree'], rec['branch']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'mine'), 'w') as f:
            f.write('mine')
        git('add', 'mine', cwd=wt)
        git('commit', '-q', '-m', 'round 4 of the review', cwd=wt)
        git('push', '-q', 'origin', f'HEAD:refs/heads/{branch}', cwd=wt)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        person = os.path.join(self.tmp, 'person')
        git('clone', '-q', '-b', branch, os.path.join(self.tmp, 'origin.git'), person, cwd=self.tmp)
        with open(os.path.join(person, 'theirs'), 'w') as f:
            f.write('theirs')
        git('add', 'theirs', cwd=person)
        git('-c', 'user.email=p@example.com', '-c', 'user.name=p', 'commit', '-q',
            '-m', 'the answer to round 4, pushed by a person', cwd=person)
        git('push', '-q', 'origin', branch, cwd=person)
        return wt, branch, git('rev-parse', 'HEAD', cwd=person)

    def test_a_stale_reused_worktree_is_fast_forwarded_never_published_over_origin(self):
        # 2026-09-25: the takeover published the worktree's older head over a person's newer
        # commit (the lease was the newer head, so it held)
        wt, branch, newer = self._ended_run_then_a_newer_remote_head('stale-ff')
        spawn_mod.make_worktree(self.product, 'stale-ff', branch)
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0], newer)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), newer)

    def test_a_review_starts_on_the_branchs_current_remote_head(self):
        wt, branch, newer = self._ended_run_then_a_newer_remote_head('stale-review')
        with open(os.path.join(wt, 'mine'), 'w') as f:
            f.write('a stale local review commit, never pushed')
        git('commit', '-q', '-am', 'stale round 5', cwd=wt)
        spawn_mod.make_worktree(self.product, 'stale-review', branch, kind='review')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), newer)
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0], newer)

    def test_b0025_live_sessions_worktree_still_refuses(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': os.getpid()}])
        row = feature_row('again')
        spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def test_f0087_a_correction_takes_over_the_ended_jobs_worktree_with_its_uncommitted_work(self):
        # a fix session ends "done" with files it never committed (B-0051/B-0052); health holds
        # it; the FIX → CORRECT row's job has another name, but the same branch — it must run
        # in that worktree, where the work is, and health must not reap it meanwhile
        rec = spawn_mod.spawn(self.product, s1_row('fix-bug-b-0001'), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 41}]), cfg=self.cfg)
        wt = rec['worktree']
        with open(os.path.join(wt, 'work.txt'), 'w') as f:
            f.write('half done\n')
        # B-0094: the factory commits a finished run's leftovers; only a commit the repo's hooks
        # refuse stays a hold
        hooks = os.path.join(self.tmp, 'refusing-hooks')
        os.makedirs(hooks)
        with open(os.path.join(hooks, 'pre-commit'), 'w') as f:
            f.write('#!/bin/sh\necho refused >&2\nexit 1\n')
        os.chmod(os.path.join(hooks, 'pre-commit'), 0o755)
        git('config', 'core.hooksPath', hooks, cwd=self.repo)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('fix-bug-b-0001', 'ended',
                       'failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)'), found)
        held = [d for j, w, d in found if w == 'held']
        self.assertEqual(held, [lifecycle.unpushed_text(
            'failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)')
            + ' — back to its session (round 1)'])
        self.assertTrue(os.path.isdir(wt))  # kept: there is work in it
        corr = step_wave_corrections(self.product)
        self.assertEqual(corr['B-0001']['kind'], 'unpushed')
        self.assertEqual(corr['B-0001']['rounds'], 1)
        row = pool_mod.parse_row(json.dumps({'job': 'correct-b-0001', 'item': 'B-0001', 'state': 'FIX',
                                             'action': 'CORRECT', 'model': 'Opus', 'kind': 'correct',
                                             'branch': rec['branch']}))
        rec2 = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                               runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': os.getpid()}]), cfg=self.cfg)
        self.assertEqual(os.path.realpath(rec2['worktree']), os.path.realpath(wt))
        self.assertTrue(os.path.exists(os.path.join(wt, 'work.txt')))
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), rec['branch'])
        self.assertEqual(step_wave_corrections(self.product), {})  # answered
        found = health_mod.health(self.product, fix=True, alive=lambda pid: pid == os.getpid(),
                                  out=lambda s: None)
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse([f for f in found if f[1] in ('reaped', 'reapable')], found)
        # a third session on the same branch while the correction is live: refused
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.spawn(self.product, s1_row('fix-bug-b-0001'), self.acct(), 'b',
                            runtime=runtime_mod.FakeRuntime([{'ok': True}]), cfg=self.cfg)

    def test_f0087_an_empty_reaped_branch_never_blocks_the_next_launch_on_it(self):
        rec = spawn_mod.spawn(self.product, feature_row('spec-f-0001'), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 51}]), cfg=self.cfg)
        health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse(os.path.exists(rec['worktree']))
        self.assertEqual(git('branch', '--list', rec['branch'], cwd=self.repo), '')  # gone with it
        # the same branch, another job (the correction row): a fresh worktree, no refusal
        row = pool_mod.parse_row(json.dumps({'job': 'correct-f-0001', 'item': 'F-0001', 'state': 'FIX',
                                             'action': 'CORRECT', 'model': 'Opus', 'kind': 'correct',
                                             'branch': rec['branch']}))
        rec2 = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                               runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 52}]), cfg=self.cfg)
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=rec2['worktree']), rec['branch'])

    def stray_branch(self):
        """A local ``spec/F-0001`` one commit past origin/main, with no worktree: its tip."""
        git('branch', 'spec/F-0001', 'origin/main', cwd=self.repo)
        tmp_wt = os.path.join(self.tmp, 'stray')
        git('worktree', 'add', '-q', tmp_wt, 'spec/F-0001', cwd=self.repo)
        with open(os.path.join(tmp_wt, 'x'), 'w') as f:
            f.write('x')
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=tmp_wt)
        git('add', 'x', cwd=tmp_wt)
        git('commit', '-q', '-m', 'x', cwd=tmp_wt)
        tip = git('rev-parse', 'HEAD', cwd=tmp_wt)
        git('worktree', 'remove', '--force', tmp_wt, cwd=self.repo)
        return tip

    def spec_row(self):
        return pool_mod.parse_row(json.dumps({'job': 'spec-f-0001', 'item': 'F-0001',
                                              'state': 'CARD', 'action': 'SPEC', 'model': 'Opus',
                                              'kind': 'spec', 'branch': 'spec/F-0001'}))

    def test_f0276_a_stray_local_branch_is_saved_to_a_recovery_ref_and_the_job_launches(self):
        tip = self.stray_branch()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rec = spawn_mod.spawn(self.product, self.spec_row(), self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 61}]),
                                  cfg=self.cfg)
        refs = git('ls-remote', 'origin', 'refs/asf/recovered/*', cwd=self.repo).splitlines()
        self.assertEqual(len(refs), 1, refs)
        sha, ref = refs[0].split('\t')
        self.assertEqual(sha, tip)                      # the unpushed commit is on origin
        self.assertRegex(ref, r'^refs/asf/recovered/spec/F-0001/\d{8}T\d{6}Z$')
        self.assertIn(f'kept as {ref}', err.getvalue())  # one line names the ref
        # the launch cut the branch fresh from the trunk: the stray tip is gone locally
        self.assertEqual(git('rev-parse', 'HEAD', cwd=rec['worktree']),
                         git('rev-parse', 'origin/main', cwd=self.repo))

    def test_f0276_a_stray_branch_that_cannot_be_saved_is_refused_as_branch_state(self):
        tip = self.stray_branch()
        from asf import gitpush
        refused = subprocess.CompletedProcess(['git', 'push'], 1, '', 'remote: denied')
        with mock.patch.object(gitpush, 'push', return_value=refused), \
                self.assertRaises(spawn_mod.BranchState) as cm:
            spawn_mod.spawn(self.product, self.spec_row(), self.acct(), 'b',
                            runtime=runtime_mod.FakeRuntime([{'ok': True}]), cfg=self.cfg)
        self.assertIn('spec/F-0001 exists locally with 1 commit(s)', str(cm.exception))
        self.assertIn('remote: denied', str(cm.exception))
        self.assertEqual(git('rev-parse', 'refs/heads/spec/F-0001', cwd=self.repo), tip)  # kept

    def test_b0142_a_branch_held_by_an_external_worktree_waits_and_is_never_removed(self):
        # a worktree ASF did not create (outside ~/.ASF/state/<p>/worktrees) holds the branch —
        # a console agent's own checkout, still at work on it. No run recorded it, but it is not
        # an operator matter: never NEEDS OPERATOR, never reclaimed or removed by the factory.
        git('branch', 'spec/F-0001', 'origin/main', cwd=self.repo)
        external = os.path.join(self.tmp, 'external-agent-worktree')
        git('worktree', 'add', '-q', external, 'spec/F-0001', cwd=self.repo)
        row = pool_mod.parse_row(json.dumps({'job': 'spec-f-0001', 'item': 'F-0001', 'state': 'CARD',
                                             'action': 'SPEC', 'model': 'Opus', 'kind': 'spec',
                                             'branch': 'spec/F-0001'}))
        with self.assertRaises(spawn_mod.WorktreeExternal) as cm:
            spawn_mod.spawn(self.product, row, self.acct(), 'b',
                            runtime=runtime_mod.FakeRuntime([{'ok': True}]), cfg=self.cfg)
        self.assertIn(f'external worktree {os.path.realpath(external)}', str(cm.exception))
        self.assertIn("waits until it's released", str(cm.exception))
        self.assertTrue(os.path.isdir(external))  # never touched, let alone removed
        self.assertIn(os.path.realpath(external), git('worktree', 'list', cwd=self.repo))

    def test_b0024_unmapped_model_label_spawns_nothing(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'acct-a', 'role': 'local', 'cap': 2}]}}
        rt = runtime_mod.FakeRuntime([{'running': True}])
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            spawn_mod.spawn(self.product, s1_row(), self.acct(), 'b', runtime=rt, cfg=cfg)
        self.assertEqual(str(cm.exception), 'NEEDS OPERATOR: worker_pool.models has no entry '
                                            'for Opus — add it to config.yaml')
        self.assertEqual(rt.calls, [])
        self.assertFalse(os.path.exists(os.path.join(env.ASF_HOME, 'state', 'sample',
                                                     'worktrees', 'fix-b-0001')))

    def test_b0024_result_with_model_error_is_not_ok(self):
        log = os.path.join(self.tmp, 'r.jsonl')
        rec = {'type': 'result', 'subtype': 'success', 'is_error': False,
               'result': 'There is an issue with the selected model (light)'}
        with open(log, 'w') as f:
            f.write(json.dumps(rec) + '\n')
        self.assertFalse(runtime_mod.result_ok(runtime_mod.read_result(log)))
        self.assertEqual(runtime_mod.failure_reason(rec), 'unknown model')

    def test_b0294_an_overloaded_api_error_is_named_not_left_bare(self):
        rec = {'type': 'result', 'subtype': 'success', 'is_error': True,
               'result': 'API Error: 529 {"type":"error","error":{"type":"overloaded_error",'
                         '"message":"Overloaded"}}'}
        self.assertFalse(runtime_mod.result_ok(rec))
        self.assertEqual(runtime_mod.failure_reason(rec), runtime_mod.OVERLOADED)

    def test_spawn_starts_the_sampler_once_for_a_local_launch(self):
        calls = []

        def fake_spawn(argv):
            calls.append(argv)
            return 5150
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        with mock.patch('asf.detach.spawn', side_effect=fake_spawn):
            spawn_mod.spawn(self.product, feature_row('samp'), self.acct(), 'b', runtime=rt,
                            cfg=_cfg_with_sampler(self.cfg))
        self.assertEqual(calls, [[sys.executable, '-m', 'asf.cli', 'workers', 'progress',
                                  '--product', 'sample', '--job', 'samp', '--watch']])
        self.assertEqual(pool_mod.load_sessions(self.product)['samp']['progress_pid'], 5150)

    def test_spawn_starts_no_sampler_for_a_cloud_lane(self):
        calls = []
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        rt.lane = 'cloud'
        with mock.patch('asf.detach.spawn', side_effect=lambda argv: calls.append(argv) or 1):
            spawn_mod.spawn(self.product, feature_row('cloudy'), self.acct(), 'b', runtime=rt,
                            cfg=_cfg_with_sampler(self.cfg))
        self.assertEqual(calls, [])
        self.assertNotIn('progress_pid', pool_mod.load_sessions(self.product)['cloudy'])

    def test_spawn_starts_no_sampler_under_progress_sampler_false(self):
        calls = []
        cfg = dict(self.cfg, worker_pool=dict(self.cfg['worker_pool'], progress_sampler=False))
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        with mock.patch('asf.detach.spawn', side_effect=lambda argv: calls.append(argv) or 1):
            spawn_mod.spawn(self.product, feature_row('quietsamp'), self.acct(), 'b', runtime=rt,
                            cfg=cfg)
        self.assertEqual(calls, [])
        self.assertNotIn('progress_pid', pool_mod.load_sessions(self.product)['quietsamp'])

    def test_spawn_record_still_returned_when_the_sampler_start_raises(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        with mock.patch('asf.detach.spawn', side_effect=OSError('no fork slots')):
            rec = spawn_mod.spawn(self.product, feature_row('failsamp'), self.acct(), 'b',
                                  runtime=rt, cfg=_cfg_with_sampler(self.cfg))
        self.assertEqual(rec['job'], 'failsamp')
        self.assertNotIn('progress_pid', pool_mod.load_sessions(self.product)['failsamp'])

    def test_a_broken_push_credential_refuses_the_launch_before_anything_is_spent(self):
        # B-0040: the account's git push credential is probed before a worktree, an id range or
        # a session is spent — a broken one refuses this launch outright, no worktree left behind.
        from asf.workers import account_auth
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        err = io.StringIO()
        with mock.patch('asf.doctor.probe_account_push_auth',
                        return_value=(False, 'could not read Username')) as probe, \
                contextlib.redirect_stderr(err):
            with self.assertRaises(spawn_mod.SpawnError):
                spawn_mod.spawn(self.product, s1_row(), self.acct(), 'fix\n', runtime=rt,
                                cfg=self.cfg)
        probe.assert_called_once()
        self.assertNotIn('fix-b-0001', pool_mod.load_sessions(self.product))
        self.assertFalse(os.path.exists(os.path.join(env.ASF_HOME, 'state', 'sample',
                                                      'worktrees', 'fix-b-0001')))
        self.assertIn('ALARM', err.getvalue())
        self.assertIn('acct-a', account_auth.blocked())

    def test_a_blocked_account_is_refused_with_no_second_probe(self):
        # once blocked, the account is unusable for every later launch until an operator clears
        # it — a second attempt costs no new network round trip and prints no second ALARM.
        from asf.workers import account_auth
        account_auth.block_now('acct-a', self.product, 'could not read Username')
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        with mock.patch('asf.doctor.probe_account_push_auth') as probe:
            with self.assertRaises(spawn_mod.SpawnError):
                spawn_mod.spawn(self.product, s1_row(), self.acct(), 'fix\n', runtime=rt,
                                cfg=self.cfg)
        probe.assert_not_called()

    def test_a_working_push_credential_launches_normally(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        with mock.patch('asf.doctor.probe_account_push_auth', return_value=(True, 'authenticates')) as probe:
            rec = spawn_mod.spawn(self.product, s1_row(), self.acct(), 'fix\n', runtime=rt,
                                  cfg=self.cfg)
        probe.assert_called_once()
        self.assertEqual(rec['job'], 'fix-b-0001')

    def test_a_cloud_lane_launch_probes_nothing_it_pushes_from_the_runner(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        rt.lane = 'cloud'
        with mock.patch('asf.detach.spawn', side_effect=lambda argv: 1), \
                mock.patch('asf.doctor.probe_account_push_auth') as probe:
            spawn_mod.spawn(self.product, feature_row('cloudy2'), self.acct(), 'b', runtime=rt,
                            cfg=self.cfg)
        probe.assert_not_called()

    def test_conventions_flags_push_auth_preflight_false_opts_out(self):
        product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                         'job_grants': [self.grant],
                                         'conventions': {'flags': {'push_auth_preflight': False}}})
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        with mock.patch('asf.doctor.probe_account_push_auth') as probe:
            spawn_mod.spawn(product, s1_row(), self.acct(), 'fix\n', runtime=rt, cfg=self.cfg)
        probe.assert_not_called()


class LaunchRebaseDefersMidRun(Home):
    """F-0203 — a launch never re-pushes a branch whose CI run is in flight: the launch rebase
    asks :func:`asf.ci_flight.verdict` before it rebases, and leaves the branch exactly as origin
    holds it (byte-identical, un-re-dated) when the verdict defers."""

    class _Flight:
        """A :class:`asf.ci_flight.Flight`-shaped fake answering ``answer`` for every branch."""

        def __init__(self, answer):
            self.answer = answer

        def read(self, product, branch):
            return self.answer

    def test_a_branch_carrying_trunk_history_with_a_run_in_flight_is_left_untouched(self):
        # the card's own incident: a rebase re-dates every commit it replays even when the
        # branch's own content does not change — the deferral must leave the head, and every
        # commit's date, exactly as origin holds them
        for shape in ('copy', 'merge'):
            with self.subTest(shape=shape):
                name = f'B-defer-{shape}'
                other, branch = self._held_branch_behind_main(name, shape=shape)
                held = git('rev-parse', branch, cwd=other)
                dates = git('log', '--format=%cI', branch, cwd=other)
                flight = self._Flight({'id': 36262385912, 'status': 'in_progress'})
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    wt = spawn_mod.make_worktree(self.product, f'correct-{name.lower()}', branch,
                                                 kind='correct', flight=flight)
                self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), held)
                self.assertEqual(git('log', '--format=%cI', 'HEAD', cwd=wt), dates)
                self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                                 held)
                self.assertIn(f'rebase deferred: {branch} CI in flight (run 36262385912)',
                             buf.getvalue())

    def test_with_nothing_in_flight_the_rebase_and_publish_are_unchanged(self):
        name = 'B-nodefer-copy'
        other, branch = self._held_branch_behind_main(name, shape='copy')
        flight = self._Flight(None)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            wt = spawn_mod.make_worktree(self.product, f'correct-{name.lower()}', branch,
                                         kind='correct', flight=flight)
        self.assertEqual(git('rev-list', '--merges', 'origin/main..HEAD', cwd=wt), '')
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))
        self.assertNotIn('deferred', buf.getvalue())

    def test_a_conflicting_branch_is_rebased_at_once_run_in_flight_or_not(self):
        for severity in (None, 'S1'):
            with self.subTest(severity=severity):
                name = f'B-conflict-{severity}'
                other, branch = self._held_branch_behind_main(name, f'main-{severity}\n',
                                                               'conflict')
                flight = self._Flight({'id': 1, 'status': 'queued'})
                buf = io.StringIO()
                with mock.patch.object(ci_flight, 'verdict', wraps=ci_flight.verdict) as v, \
                     contextlib.redirect_stderr(buf):
                    spawn_mod.make_worktree(self.product, f'correct-{name.lower()}', branch,
                                            kind='correct', severity=severity, flight=flight)
                self.assertNotIn('rebase deferred', buf.getvalue())
                v.assert_called_once()
                self.assertEqual(v.call_args.kwargs['needed'], ci_flight.CONFLICT)

    def test_an_s1_branch_carrying_trunk_history_with_a_run_in_flight_still_defers(self):
        # C4: for an S1/hotfix branch, only CONFLICT survives — a copy of a trunk commit claims
        # no exception, so an S1 held branch waits for the run exactly like any other
        name = 'B-s1-copy'
        other, branch = self._held_branch_behind_main(name, shape='copy')
        held = git('rev-parse', branch, cwd=other)
        flight = self._Flight({'id': 36262385912, 'status': 'in_progress'})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            wt = spawn_mod.make_worktree(self.product, f'correct-{name.lower()}', branch,
                                         kind='correct', severity='S1', flight=flight)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), held)
        self.assertIn(f'rebase deferred: {branch} CI in flight (run 36262385912)', buf.getvalue())

    def test_urgent_read_off_the_branch_name_alone_still_defers_a_copy(self):
        # C5: urgent() off the branch name needs no severity and no record
        other, branch = self._held_branch_behind_main('hotfix-B-name', shape='copy')
        held = git('rev-parse', branch, cwd=other)
        flight = self._Flight({'id': 36262385912, 'status': 'in_progress'})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            wt = spawn_mod.make_worktree(self.product, 'correct-hotfix', branch, kind='correct',
                                         severity=None, flight=flight)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), held)
        self.assertIn('deferred', buf.getvalue())

    def test_a_fresh_branch_not_on_origin_is_never_asked(self):
        row = s1_row(job='fresh-not-on-origin', item='B-9999')
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 1}])
        with mock.patch.object(ci_flight, 'run_in_flight',
                               side_effect=AssertionError('asked')):
            rec = spawn_mod.spawn(self.product, row, self.acct(), 'fix\n', runtime=rt,
                                  cfg=self.cfg)
        self.assertEqual(rec['job'], 'fresh-not-on-origin')

    def test_product_none_reaches_the_gate_without_raising(self):
        other, branch = self._held_branch_behind_main('B-none-product', shape='copy')
        git('fetch', '-q', 'origin', cwd=other)
        git('checkout', '-q', branch, cwd=other)
        spawn_mod._rebase_onto_trunk(other, branch, 'main', product=None)
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=other).split()[0],
                         git('rev-parse', 'HEAD', cwd=other))

    def test_severity_is_threaded_from_the_row_through_spawn(self):
        # the only row that proves severity=row.severity is actually passed (every other test
        # here calls make_worktree directly and would pass with the thread missing)
        other, branch = self._held_branch_behind_main('B-thread-s1', shape='copy')
        held = git('rev-parse', branch, cwd=other)
        row = pool_mod.Row('correct-thread', 'B-thread-s1', kind='correct', branch=branch,
                           severity='S1')
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 1}])
        buf = io.StringIO()
        with mock.patch.object(ci_flight, 'run_in_flight',
                               return_value={'id': 1, 'status': 'queued'}), \
             contextlib.redirect_stderr(buf):
            rec = spawn_mod.spawn(self.product, row, self.acct(), 'fix it\n', runtime=rt,
                                  cfg=self.cfg)
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=rec['worktree']).split()[0],
                         held)
        self.assertIn('rebase deferred', buf.getvalue())


class TestReclaimDeadWorktree(Home):
    """2026-09-26 (T-0349, F-0003): a dead run's worktree, detached at a factory WIP commit with
    a rebase of the branch in progress, held the branch — ``worktree add -B`` failed and the
    launch surfaced NEEDS OPERATOR every tick. A dead holder is reclaimed: its commits not on
    origin archived (``archive/<branch-dashed>-wip-<sha9>``), the rebase aborted, the tree
    trashed, the add retried. A live run's worktree is never touched."""

    def _stuck(self, job='coder-t-0349', push_branch=True):
        """A run's worktree on its branch, left mid-rebase (a conflict) with a WIP commit made
        detached on top — the B-0094 state. Returns ``(worktree, branch, wip_sha, branch_tip)``."""
        rec = spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}]),
                              cfg=self.cfg)
        wt, branch = rec['worktree'], rec['branch']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'README'), 'w') as f:
            f.write('branch side\n')
        git('commit', '-q', '-am', 'branch work', cwd=wt)
        if push_branch:
            git('push', '-q', 'origin', f'HEAD:refs/heads/{branch}', cwd=wt)
        tip = git('rev-parse', 'HEAD', cwd=wt)
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        with open(os.path.join(other, 'README'), 'w') as f:
            f.write('trunk side\n')
        git('-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'commit', '-q', '-am',
            'trunk moves', cwd=other)
        git('push', '-q', 'origin', 'main', cwd=other)
        git('fetch', '-q', 'origin', cwd=wt)
        r = subprocess.run(['git', 'rebase', 'origin/main'], cwd=wt, capture_output=True)
        self.assertNotEqual(r.returncode, 0)  # stopped on the conflict
        with open(os.path.join(wt, 'README'), 'w') as f:
            f.write('factory wip\n')
        git('add', '-A', cwd=wt)
        git('commit', '-q', '--no-verify', '-m', 'wip: committed by the factory', cwd=wt)
        wip = git('rev-parse', 'HEAD', cwd=wt)
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), 'HEAD')
        gitdir = git('rev-parse', '--absolute-git-dir', cwd=wt)
        with open(os.path.join(gitdir, 'rebase-merge', 'head-name')) as f:
            self.assertEqual(f.read().strip(), f'refs/heads/{branch}')
        return wt, branch, wip, tip

    def _adjudicate(self, branch, job='adjudicate-t-0349'):
        return pool_mod.parse_row(json.dumps({'job': job, 'item': 'F-0001', 'state': 'FIX',
                                              'action': 'ADJUDICATE', 'model': 'Opus',
                                              'kind': 'adjudicate', 'branch': branch}))

    def _remote_heads(self):
        out = git('ls-remote', '--heads', os.path.join(self.tmp, 'origin.git'), cwd=self.tmp)
        return {ln.split()[1][len('refs/heads/'):]: ln.split()[0] for ln in out.splitlines()}

    def test_a_dead_stuck_rebase_worktree_is_reclaimed_and_the_spawn_succeeds(self):
        wt, branch, wip, _tip = self._stuck()
        err = io.StringIO()
        with mock.patch.object(lifecycle, 'pid_alive', lambda pid: pid == os.getpid()), \
                contextlib.redirect_stderr(err):
            rec = spawn_mod.spawn(self.product, self._adjudicate(branch), self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'running': True,
                                                                    'pid': os.getpid()}]),
                                  cfg=self.cfg)
        archive = f'archive/{branch.replace("/", "-")}-wip-{wip[:9]}'
        self.assertEqual(self._remote_heads().get(archive), wip)
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn(os.path.realpath(wt), git('worktree', 'list', cwd=self.repo))
        # the new worktree holds the branch (its takeover rebase conflicts: aborted, F-0037)
        self.assertEqual(os.path.realpath(spawn_mod._holding_worktree(self.repo, branch)),
                         os.path.realpath(rec['worktree']))
        self.assertEqual(spawn_mod._in_progress(rec['worktree']), [])
        self.assertIn(f'reclaimed {os.path.realpath(wt)} from dead coder-t-0349: archived '
                      f'{archive}', err.getvalue())

    def test_a_live_sessions_stuck_worktree_is_never_touched(self):
        wt, branch, wip, _tip = self._stuck()
        with mock.patch.object(lifecycle, 'pid_alive', lambda pid: True):
            with self.assertRaises(spawn_mod.WorktreeBusy):
                spawn_mod.spawn(self.product, self._adjudicate(branch), self.acct(), 'b',
                                runtime=runtime_mod.FakeRuntime([{'running': True}]),
                                cfg=self.cfg)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), wip)
        gitdir = git('rev-parse', '--absolute-git-dir', cwd=wt)
        self.assertTrue(os.path.isdir(os.path.join(gitdir, 'rebase-merge')))
        self.assertFalse([h for h in self._remote_heads() if h.startswith('archive/')])

    def test_unpushed_commits_are_archived_before_the_worktree_goes(self):
        # the branch never reached origin: its own tip and the WIP on top are both kept
        wt, branch, wip, tip = self._stuck(push_branch=False)
        with mock.patch.object(lifecycle, 'pid_alive', lambda pid: pid == os.getpid()), \
                contextlib.redirect_stderr(io.StringIO()):
            rec = spawn_mod.spawn(self.product, self._adjudicate(branch), self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'running': True,
                                                                    'pid': os.getpid()}]),
                                  cfg=self.cfg)
        archived = {sha for h, sha in self._remote_heads().items() if h.startswith('archive/')}
        self.assertIn(wip, archived)
        git('fetch', '-q', 'origin', cwd=self.repo)
        for sha in (wip, tip):  # every unpushed commit reachable from an archive ref
            self.assertTrue(any(subprocess.run(['git', 'merge-base', '--is-ancestor', sha, a],
                                               cwd=self.repo).returncode == 0
                                for a in archived), sha)
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=rec['worktree']), branch)


class TestReclaimOrphanHolder(TestReclaimDeadWorktree):
    """A worktree in ASF's own dir that no run recorded holds a branch another role needs: a
    stale one (untouched for ``ORPHAN_GRACE_S``) is reclaimed — its uncommitted files committed
    and archived first — instead of a NEEDS OPERATOR every tick for ever; a fresh one (a launch
    whose ledger line is not written yet) is still refused."""

    def _orphan(self, age_s):
        wt, branch, _wip, _tip = self._stuck(job='coder-t-0349')
        with open(os.path.join(wt, 'NOTES'), 'w') as f:
            f.write('uncommitted work\n')
        # the ledger never recorded it: drop every line naming its worktree
        reg = pool_mod.sessions_path(self.product)
        with open(reg) as f:
            keep = [ln for ln in f if 'coder-t-0349' not in ln]
        with open(reg, 'w') as f:
            f.writelines(keep)
        return wt, branch, time.time() + age_s

    def _spawn(self, branch, now):
        err = io.StringIO()
        with mock.patch.object(spawn_mod.time, 'time', lambda: now), \
                mock.patch.object(lifecycle, 'pid_alive', lambda pid: pid == os.getpid()), \
                contextlib.redirect_stderr(err):
            rec = spawn_mod.spawn(self.product, self._adjudicate(branch), self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'running': True,
                                                                    'pid': os.getpid()}]),
                                  cfg=self.cfg)
        return rec, err.getvalue()

    def test_a_stale_orphan_holder_is_reclaimed_with_its_leftovers_archived(self):
        wt, branch, later = self._orphan(spawn_mod.ORPHAN_GRACE_S + 60)
        rec, err = self._spawn(branch, later)
        self.assertFalse(os.path.exists(wt))
        self.assertIn('from an orphan (no run recorded)', err)
        self.assertEqual(os.path.realpath(spawn_mod._holding_worktree(self.repo, branch)),
                         os.path.realpath(rec['worktree']))
        archives = [h for h in self._remote_heads() if h.startswith('archive/')]
        self.assertTrue(archives)
        git('fetch', '-q', 'origin', cwd=self.repo)
        files = {git('show', f'origin/{a}:NOTES', cwd=self.repo) for a in archives
                 if subprocess.run(['git', 'cat-file', '-e', f'origin/{a}:NOTES'],
                                   cwd=self.repo).returncode == 0}
        self.assertEqual(files, {'uncommitted work'})

    def test_a_fresh_orphan_holder_is_still_refused(self):
        wt, branch, _later = self._orphan(0)
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self._spawn(branch, time.time())
        self.assertIn('no run recorded it', str(cm.exception))
        self.assertTrue(os.path.exists(wt))


class TestSpawnFailureMemoryForgets(Home):
    def test_a_failure_not_seen_again_is_forgotten(self):
        f = wave_mod.Failures(self.product, now=1000)
        f.note('adjudicate-t-0349', 'spawn failed: held')
        f.note('adjudicate-t-0349', 'spawn failed: held')
        self.assertEqual(wave_mod.Failures(self.product, now=1000 + 60).seen['adjudicate-t-0349']['count'], 2)
        later = wave_mod.Failures(self.product, now=1000 + wave_mod.Failures.FORGET_S + 1)
        self.assertNotIn('adjudicate-t-0349', later.seen)
        # and the file itself forgot it
        self.assertNotIn('adjudicate-t-0349', wave_mod.Failures(self.product, now=1000).seen)


class TestFailingToSpawnIsRead(Home):
    """W2-PR8: a job failing to spawn twice in a row is readable without writing — the plan's
    rows say FAILING TO SPAWN from it; once and stale are not."""

    def test_read_names_repeats_only_and_forgets_the_stale(self):
        f = wave_mod.Failures(self.product, now=1000)
        f.note('task-t-0001', 'spawn failed: held')
        f.note('task-t-0001', 'spawn failed: held')
        f.note('task-t-0002', 'spawn failed: once')
        got = wave_mod.Failures.read(self.product, now=1000)
        self.assertEqual(list(got), ['task-t-0001'])
        self.assertEqual(got['task-t-0001']['count'], 2)
        self.assertEqual(wave_mod.Failures.read(self.product, now=1000 + wave_mod.Failures.FORGET_S + 1), {})
        # read only: the file still holds both
        self.assertEqual(set(wave_mod.Failures(self.product, now=1000).seen), {'task-t-0001', 'task-t-0002'})

    def test_summary_names_each_repeating_job(self):
        f = wave_mod.Failures(self.product, now=1000)
        for _ in range(3):
            f.note('task-t-0001', 'spawn failed: branch cloud/T-0001 exists locally')
        f.note('task-t-0002', 'spawn failed: once')
        self.assertEqual(f.summary(['task-t-0001', 'task-t-0002']),
                         'spawn: 1 job(s) failing every tick — task-t-0001 ×3 '
                         '(branch cloud/T-0001 exists locally)')
        self.assertIsNone(f.summary(['task-t-0002']))


class TestNoWorktreeHandedOverMidRebase(Home):
    """A product's F-0037, 2026-09-27: a correct session's worktree started with a rebase in
    progress whose ``onto`` was the branch's stale ``asf: report`` commit, replaying 257
    unrelated commits. The worktree's head had been rebased onto a newer trunk by the last
    session and origin's tip was the cloud session's report commit: :func:`spawn._catch_up`
    rebased the whole head onto origin's tip — every trunk commit the head had gained in the
    range — and left the conflict for the session. A head that is a rebase of origin's tip is
    caught up (the factory publishes it); no factory rebase goes onto a report commit or
    replays trunk commits; and no worktree is ever handed over mid-rebase."""

    def _rebased_over_a_report(self, job='correct-f-0037'):
        rec = spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}]),
                              cfg=self.cfg)
        wt, branch = rec['worktree'], rec['branch']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'README'), 'w') as f:
            f.write('branch side\n')
        git('commit', '-q', '-am', 'spec(F-0001): branch work', cwd=wt)
        git('push', '-q', 'origin', f'HEAD:refs/heads/{branch}', cwd=wt)
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', '-b', branch, os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=other)
        git('commit', '-q', '--allow-empty', '-m', f'asf: report {job}', cwd=other)
        git('push', '-q', 'origin', branch, cwd=other)
        report = git('rev-parse', 'HEAD', cwd=other)
        git('checkout', '-q', 'main', cwd=other)
        with open(os.path.join(other, 'README'), 'w') as f:
            f.write('trunk side\n')
        git('commit', '-q', '-am', 'trunk moves', cwd=other)
        git('push', '-q', 'origin', 'main', cwd=other)
        # the last session rebased onto the trunk, resolving README, and never pushed
        git('fetch', '-q', 'origin', cwd=wt)
        r = subprocess.run(['git', 'rebase', 'origin/main'], cwd=wt, capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        with open(os.path.join(wt, 'README'), 'w') as f:
            f.write('resolved\n')
        git('add', 'README', cwd=wt)
        subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=wt,
                       capture_output=True, check=True)
        return wt, branch, report, git('rev-parse', 'HEAD', cwd=wt)

    def _in_progress(self, wt):
        gitdir = git('rev-parse', '--absolute-git-dir', cwd=wt)
        return [s for s in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD')
                if os.path.exists(os.path.join(gitdir, s))]

    def test_a_head_rebased_over_a_report_commit_is_published_never_rebased_onto_it(self):
        wt, branch, report, head = self._rebased_over_a_report()
        with mock.patch.object(lifecycle, 'pid_alive', lambda pid: False):
            path = spawn_mod.make_worktree(self.product, 'correct-f-0037', branch, kind='correct')
        self.assertEqual(os.path.realpath(path), os.path.realpath(wt))
        self.assertEqual(self._in_progress(wt), [])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), head)
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0], head)
        archive = lifecycle.copies_archive(branch, report)
        self.assertEqual(git('ls-remote', '--heads', 'origin', archive, cwd=wt).split()[0], report)

    def test_a_conflicting_catch_up_is_aborted_never_handed_over(self):
        wt, branch, report, head = self._rebased_over_a_report()
        # a person's real commit on origin that the head lacks and that conflicts
        person = os.path.join(self.tmp, 'person')
        git('clone', '-q', '-b', branch, os.path.join(self.tmp, 'origin.git'), person,
            cwd=self.tmp)
        with open(os.path.join(person, 'README'), 'w') as f:
            f.write('a person\n')
        git('-c', 'user.email=p@example.com', '-c', 'user.name=p', 'commit', '-q', '-am',
            'a person edits README', cwd=person)
        git('push', '-q', 'origin', branch, cwd=person)
        with mock.patch.object(lifecycle, 'pid_alive', lambda pid: False):
            spawn_mod.make_worktree(self.product, 'correct-f-0037', branch, kind='correct')
        self.assertEqual(self._in_progress(wt), [])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), head)
        self.assertEqual(git('status', '--porcelain', cwd=wt), '')

    def test_a_worktree_left_mid_rebase_is_settled_before_launch(self):
        wt, branch, report, head = self._rebased_over_a_report()
        git('reset', '-q', '--hard', 'origin/' + branch, cwd=wt)  # back on the report tip
        r = subprocess.run(['git', 'rebase', 'origin/main'], cwd=wt, capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._in_progress(wt), ['rebase-merge'])
        with mock.patch.object(lifecycle, 'pid_alive', lambda pid: False):
            spawn_mod.make_worktree(self.product, 'correct-f-0037', branch, kind='correct')
        self.assertEqual(self._in_progress(wt), [])
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), branch)
        self.assertEqual(git('status', '--porcelain', cwd=wt), '')


class SpawnHookTests(Home):
    """T-0026 — no session is launched into a repo with no push gate: ``make_worktree`` calls
    ``asf.hooks.ensure_git_hooks(product)`` first, before any of the four worktree paths.
    ``ensure_git_hooks`` itself is Task 8353's (not yet in this checkout); these tests stand in
    for it with ``create=True`` mocks, against the plan's stated contract — an ``(ok, detail)``
    pair, ``detail`` the ``NEEDS OPERATOR`` line ``ensure_git_hooks`` produces on a foreign hook."""

    def test_spawn_installs_missing_hooks_before_the_worktree(self):
        wt = os.path.join(env.ASF_HOME, 'state', 'sample', 'worktrees', 'j')
        calls = []

        def fake(product):
            calls.append(product)
            self.assertFalse(os.path.exists(wt))  # called before the worktree is created
            return True, 'hooks: installed'

        with mock.patch.object(hooks_mod, 'ensure_git_hooks', side_effect=fake, create=True):
            rec = spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 1}]),
                                  cfg=self.cfg)
        self.assertEqual(calls, [self.product])
        self.assertTrue(os.path.exists(rec['worktree']))

    def test_spawn_refuses_to_launch_past_a_foreign_hook(self):
        needs_operator = ('NEEDS OPERATOR: .git/hooks/pre-push is not asf\'s — add the line: '
                          '"<asf>" redact --pre-push --product sample')
        with mock.patch.object(hooks_mod, 'ensure_git_hooks', create=True,
                               return_value=(False, needs_operator)):
            with self.assertRaises(spawn_mod.SpawnError) as cm:
                spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b',
                                runtime=runtime_mod.FakeRuntime([{'running': True}]), cfg=self.cfg)
        self.assertEqual(str(cm.exception), needs_operator)
        self.assertFalse(os.path.exists(os.path.join(env.ASF_HOME, 'state', 'sample',
                                                      'worktrees', 'j')))
        self.assertNotIn('j', pool_mod.load_sessions(self.product))

    def test_a_reused_ended_worktree_gets_the_hooks_too(self):
        row = feature_row('again')
        calls = []

        def fake(product):
            calls.append(product)
            return True, 'ok'

        with mock.patch.object(hooks_mod, 'ensure_git_hooks', side_effect=fake, create=True):
            rec = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}]),
                                  cfg=self.cfg)
        wt = rec['worktree']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'x'), 'w') as f:
            f.write('x')
        git('add', 'x', cwd=wt)
        git('commit', '-q', '-m', 'own work, never pushed', cwd=wt)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        with mock.patch.object(hooks_mod, 'ensure_git_hooks', side_effect=fake, create=True):
            rec2 = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                                   runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 41}]),
                                   cfg=self.cfg)
        self.assertEqual(os.path.realpath(rec2['worktree']), os.path.realpath(wt))
        self.assertEqual(calls, [self.product, self.product])

    def test_a_reused_local_branch_with_no_worktree_gets_the_hooks_too(self):
        # a local branch left behind with no worktree and 0 commits ahead of trunk — B-0025's
        # "carries nothing, reused" case — still gets the hook check before spawn takes it
        git('branch', 'spec/F-0001', 'origin/main', cwd=self.repo)
        calls = []

        def fake(product):
            calls.append(product)
            self.assertEqual(git('branch', '--list', 'spec/F-0001', cwd=self.repo), 'spec/F-0001')
            return True, 'ok'

        row = pool_mod.parse_row(json.dumps({'job': 'spec-f-0001', 'item': 'F-0001', 'state': 'CARD',
                                             'action': 'SPEC', 'model': 'Opus', 'kind': 'spec',
                                             'branch': 'spec/F-0001'}))
        with mock.patch.object(hooks_mod, 'ensure_git_hooks', side_effect=fake, create=True):
            rec = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                                  runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 1}]),
                                  cfg=self.cfg)
        self.assertEqual(calls, [self.product])
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=rec['worktree']),
                         'spec/F-0001')


class RelaunchHeadTests(Home):
    """T-0345 — a relaunched session starts on the old head: :func:`spawn.make_worktree` hands a
    second run of the same job the worktree the first run left, on top of its commits (or a
    rebase of them, when the trunk moved under it); the launch line's ``launch_head`` names the
    sha origin held when the launch fetched it; and a worktree reused while behind origin is
    caught up before anything is committed, never left to diverge (:func:`spawn._catch_up`)."""

    def _push_two_commits(self, job):
        """Spawn ``job``, push two commits on its branch, end the run. Returns
        ``(branch, old_head)``."""
        rec = spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b',
                              runtime=runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}]),
                              cfg=self.cfg)
        wt, branch = rec['worktree'], rec['branch']
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        for i in (1, 2):
            with open(os.path.join(wt, f'f{i}.txt'), 'w') as f:
                f.write(f'commit {i}\n')
            git('add', f'f{i}.txt', cwd=wt)
            git('commit', '-q', '-m', f'work {i}', cwd=wt)
        git('push', '-q', 'origin', f'HEAD:refs/heads/{branch}', cwd=wt)
        old_head = git('rev-parse', 'HEAD', cwd=wt)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        return branch, old_head

    def test_a_relaunch_starts_on_top_of_the_old_head(self):
        branch, old_head = self._push_two_commits('relaunch-a')
        wt = spawn_mod.make_worktree(self.product, 'relaunch-a', branch, kind='correct')
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'round2.txt'), 'w') as f:
            f.write('round 2\n')
        git('add', 'round2.txt', cwd=wt)
        git('commit', '-q', '-m', 'round 2 continues', cwd=wt)
        parent = git('rev-parse', 'HEAD~1', cwd=wt)
        on_old_head = parent == old_head
        rebased = lifecycle.rebase_of(wt, lifecycle.worktree_head(wt), old_head, self.product.main)
        self.assertTrue(on_old_head or rebased)

    def test_launch_head_names_the_sha_origin_held_at_launch(self):
        branch, old_head = self._push_two_commits('relaunch-b')
        spawn_mod.make_worktree(self.product, 'relaunch-b', branch, kind='correct')
        self.assertEqual(spawn_mod._launch_head(self.repo, branch),
                         git('rev-parse', f'origin/{branch}', cwd=self.repo))
        self.assertEqual(spawn_mod._launch_head(self.repo, branch), old_head)

    def test_a_worktree_behind_origin_is_caught_up_before_the_commit(self):
        branch, old_head = self._push_two_commits('relaunch-c')
        wt = spawn_mod.make_worktree(self.product, 'relaunch-c', branch, kind='correct')
        # a person pushes on top while the worktree sits at the old head
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', '-b', branch, os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        git('-c', 'user.email=p@example.com', '-c', 'user.name=p', 'commit', '-q',
            '--allow-empty', '-m', 'a person pushes on top', cwd=other)
        git('push', '-q', 'origin', branch, cwd=other)
        newer = git('rev-parse', 'HEAD', cwd=other)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        wt2 = spawn_mod.make_worktree(self.product, 'relaunch-c', branch, kind='correct')
        self.assertEqual(os.path.realpath(wt2), os.path.realpath(wt))
        git('merge-base', '--is-ancestor', newer, 'HEAD', cwd=wt2)


class TestWave(Home):
    def run_wave(self, rows, n, accounts, live=(), usage=None):
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource(usage or {}),
                             live=live)
        lines = []
        rt = runtime_mod.FakeRuntime([{'running': True}] * 10)
        launched, waits = wave_mod.wave(self.product, rows, n, pool=pool, runtime=rt,
                                        cfg=self.cfg, out=lines.append)
        return launched, waits, lines

    def test_a_repeated_spawn_failure_reports_once_as_needs_operator(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        orphan = os.path.join(spawn_mod.worktrees_dir(self.product), 'spec-9')
        os.makedirs(orphan)  # a worktree no run recorded: refused, tick after tick
        outs = [self.run_wave([feature_row('spec-9')], 5, [acct])[2] for _ in range(4)]
        self.assertIn('— spawn failed: worktree already exists', outs[0][0])
        self.assertIn('NEEDS OPERATOR: spec-9 fails to spawn each tick', outs[1][0])
        self.assertIn(f'worktree remove --force {orphan}', outs[1][0])
        # nothing changed: no per-launch line, but never silent — one summary line per wave
        self.assertEqual(len(outs[2]), 1)
        self.assertTrue(outs[2][0].startswith('spawn: 1 job(s) failing every tick — spec-9 ×3 '
                                              '(worktree already exists'), outs[2])
        self.assertTrue(outs[3][0].startswith('spawn: 1 job(s) failing every tick — spec-9 ×4 ('))
        self.assertEqual(len(outs[3]), 1)
        self.assertTrue(outs[1][-1].startswith('spawn: 1 job(s) failing every tick — spec-9 ×2'))
        # the obstacle goes: the row launches and the memory of it is cleared
        shutil.rmtree(orphan)
        launched, _, lines = self.run_wave([feature_row('spec-9')], 5, [acct])
        self.assertEqual([r.job for r, _ in launched], ['spec-9'])
        self.assertTrue(lines[0].startswith('launched spec-9'))

    def test_a_worktree_held_by_a_live_run_is_a_wait_not_a_failure(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        wt = os.path.join(spawn_mod.worktrees_dir(self.product), 'spec-9')
        os.makedirs(wt)
        pool_mod.append_session(self.product, {'job': 'correct-9', 'pid': os.getpid(),
                                               'started': 't', 'worktree': wt})
        for _ in range(3):
            _, waits, lines = self.run_wave([feature_row('spec-9')], 5, [acct])
            self.assertIn('already running: worktree already exists', waits[0][1])
            self.assertIn('held by live run correct-9', lines[0])
            self.assertNotIn('NEEDS OPERATOR', lines[0])

    def test_b0142_a_branch_held_by_an_external_worktree_waits_not_needs_operator(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        row = feature_row('spec-9')
        branch = spawn_mod.branch_for(self.product, row)
        git('branch', branch, 'origin/main', cwd=self.repo)
        external = os.path.join(self.tmp, 'external-agent-worktree')
        git('worktree', 'add', '-q', external, branch, cwd=self.repo)
        for _ in range(4):
            _, waits, lines = self.run_wave([row], 5, [acct])
            self.assertIn(f'external worktree {os.path.realpath(external)}', waits[0][1])
            self.assertIn("waits until it's released", waits[0][1])
            self.assertNotIn('NEEDS OPERATOR', lines[0])
        self.assertTrue(os.path.isdir(external))  # never removed by the factory

    def test_pool_full_of_features_an_s1_arrives_and_launches(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        live = [{'job': f'spec-{i}', 'account': 'acct-a'} for i in range(2)]
        launched, waits, lines = self.run_wave([feature_row('spec-9'), s1_row()], 5, [acct], live)
        self.assertEqual([r.job for r, _ in launched], ['fix-b-0001'])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('spec-9', 'pool full — accounts at cap: acct-a 3/3')])
        self.assertTrue(lines[0].startswith('launched fix-b-0001'))
        self.assertIn('— pool full', lines[1])

    def test_feature_waits_on_the_reserved_slot(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        live = [{'job': 'spec-0', 'account': 'acct-a'},
                {'job': 'fix-b-0001', 'account': 'acct-a'}]
        launched, waits, lines = self.run_wave([feature_row('spec-9'), s1_row()], 5, [acct], live)
        self.assertEqual(launched, [])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('fix-b-0001', 'already running'), ('spec-9', 'reserved for S1')])
        self.assertTrue(lines[1].startswith('waits    spec-9') and lines[1].endswith('— reserved for S1'))

    def test_n_caps_the_wave(self):
        acct = pool_mod.Account('acct-a', cap=5)
        launched, waits, _ = self.run_wave([feature_row('a'), feature_row('b')], 1, [acct])
        self.assertEqual([r.job for r, _ in launched], ['a'])
        self.assertEqual([why for _, why in waits], ['wave full'])

    def test_a_cooling_account_launches_one_row_and_the_rest_wait_without_paging(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        rows = [feature_row('spec-1'), feature_row('spec-2', item='F-0002')]
        launched, waits, lines = self.run_wave(
            rows, 5, [acct], usage={'acct-a': {'five_h_pct': 0, 'seven_d_pct': 93}})
        self.assertEqual([r.job for r, _ in launched], ['spec-1'])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('spec-2', pool_mod.REASON_COOLDOWN)])
        self.assertTrue(lines[1].endswith('— quota cooldown — one job at a time'), lines[1])
        self.assertNotIn('NEEDS OPERATOR', '\n'.join(lines))


class TestWaveLaunchesConcurrently(Home):
    """F-0161: a wave's per-launch setup (worktree, publish, install, cloud create) runs on a
    bounded pool; the seat decisions stay serial, in row order."""

    def wave(self, rows, n, spawn_fn, cap=8, cfg=None, out=None):
        acct = pool_mod.Account('acct-a', role='local', cap=cap)
        pool = pool_mod.Pool([acct], quota_source=quota_mod.FakeQuotaSource({}))
        lines = [] if out is None else out
        cfg = cfg or self.cfg
        launched, waits = wave_mod.wave(self.product, rows, n, pool=pool, cfg=cfg,
                                        out=lines.append, spawn_fn=spawn_fn)
        return launched, waits, lines, pool

    def test_launch_setups_overlap(self):
        # serial launches never meet at the barrier: it breaks after its timeout
        barrier = threading.Barrier(4, timeout=10)

        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            barrier.wait()
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(4)]
        launched, waits, lines, _ = self.wave(rows, 4, spawn_fn)
        self.assertEqual([r.job for r, _ in launched], [f'spec-{i}' for i in range(4)])
        self.assertEqual(waits, [])

    def test_lines_keep_row_order_and_each_launch_prints_its_setup_time(self):
        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            time.sleep(0.2 if row.job == 'spec-0' else 0)  # the first row finishes last
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(3)]
        launched, _, lines, _ = self.wave(rows, 3, spawn_fn)
        row_lines = [ln for ln in lines if ln.startswith(('launched', 'waits'))]
        self.assertEqual([ln.split()[1] for ln in row_lines], ['spec-0', 'spec-1', 'spec-2'])
        timing = [ln for ln in lines if ln.startswith('launch ')]
        self.assertEqual([ln.split()[1] for ln in timing], ['spec-0:', 'spec-1:', 'spec-2:'])
        self.assertRegex(timing[0], r'^launch spec-0: setup \d+\.\ds$')
        self.assertGreaterEqual(float(timing[0].split()[-1][:-1]), 0.2)

    def test_a_failed_launch_gives_its_seat_to_the_next_row(self):
        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            if row.job == 'spec-1':
                raise spawn_mod.SpawnError('boom')
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(4)]
        launched, waits, lines, pool = self.wave(rows, 2, spawn_fn)
        self.assertEqual([r.job for r, _ in launched], ['spec-0', 'spec-2'])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('spec-1', 'spawn failed: boom'), ('spec-3', 'wave full')])
        self.assertEqual(sorted(s['job'] for s in pool.live), ['spec-0', 'spec-2'])

    def test_a_failed_launch_frees_the_account_seat(self):
        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            if row.job == 'spec-0':
                raise spawn_mod.SpawnError('boom')
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(3)]
        launched, waits, _, _ = self.wave(rows, 5, spawn_fn, cap=1)
        self.assertEqual([r.job for r, _ in launched], ['spec-1'])
        self.assertEqual([r.job for r, _ in waits], ['spec-0', 'spec-2'])

    def test_two_rows_on_one_branch_never_launch_together(self):
        seen = []

        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            seen.append(row.job)
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        a = pool_mod.parse_row(json.dumps({'job': 'review-t-1', 'item': 'T-0001', 'state': 'X',
                                           'action': 'REVIEW', 'model': 'Opus', 'kind': 'review',
                                           'branch': 'cloud/T-0001'}))
        b = pool_mod.parse_row(json.dumps({'job': 'correct-t-1', 'item': 'T-0001', 'state': 'X',
                                           'action': 'CORRECT', 'model': 'Opus',
                                           'kind': 'correct', 'branch': 'cloud/T-0001'}))
        launched, waits, _, _ = self.wave([a, b], 5, spawn_fn)
        self.assertEqual(seen, ['review-t-1'])
        self.assertEqual([r.job for r, _ in launched], ['review-t-1'])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('correct-t-1', 'already running: branch cloud/T-0001 launches in '
                                          'this wave (review-t-1)')])

    def test_launch_concurrency_one_is_serial(self):
        live = []
        peak = []

        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            live.append(row.job)
            peak.append(len(live))
            time.sleep(0.05)
            live.remove(row.job)
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        cfg = json.loads(json.dumps(self.cfg))
        cfg['worker_pool']['launch_concurrency'] = 1
        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(3)]
        launched, _, _, _ = self.wave(rows, 3, spawn_fn, cfg=cfg)
        self.assertEqual(len(launched), 3)
        self.assertEqual(max(peak), 1)

    def test_an_s1_minted_mid_wave_launches_in_the_same_wave_ahead_of_the_rest(self):
        # the wave's rows were fixed at its start; an S1 groomed while it launches goes next
        calls, minted = [], []

        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            calls.append(row.job)
            minted.append(True)             # the S1 lands in the record during this launch
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        asked = []

        def refresh(known):
            asked.append(set(known))
            if minted and not any('fix-b-0009' in k for k in asked[:-1]):
                return [s1_row('fix-b-0009', item='B-0009'),
                        feature_row('spec-0')]  # a job the wave has: never twice
            return []

        cfg = json.loads(json.dumps(self.cfg))
        cfg['worker_pool']['launch_concurrency'] = 1
        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(3)]
        acct = pool_mod.Account('acct-a', role='local', cap=8)
        pool = pool_mod.Pool([acct], quota_source=quota_mod.FakeQuotaSource({}))
        with mock.patch.object(wave_mod, 'REFRESH_S', 0):
            launched, waits = wave_mod.wave(self.product, rows, 3, pool=pool, cfg=cfg,
                                            out=lambda s: None, spawn_fn=spawn_fn,
                                            refresh=refresh)
        self.assertEqual(calls, ['spec-0', 'fix-b-0009', 'spec-1', 'spec-2'])
        self.assertEqual(sorted(r.job for r, _ in launched),
                         ['fix-b-0009', 'spec-0', 'spec-1', 'spec-2'])
        self.assertEqual(waits, [])
        self.assertIn('spec-0', asked[0])

    def test_the_last_look_before_the_wave_ends_catches_an_s1(self):
        minted = []

        def spawn_fn(product, row, acct, brief, runtime=None, cfg=None):
            minted.append(row.job)
            return {'job': row.job, 'model': 'opus', 'pid': 1}

        def refresh(known):
            return [s1_row('fix-b-0009', item='B-0009')] if minted and 'fix-b-0009' not in known else []

        acct = pool_mod.Account('acct-a', role='local', cap=8)
        pool = pool_mod.Pool([acct], quota_source=quota_mod.FakeQuotaSource({}))
        launched, waits = wave_mod.wave(self.product, [feature_row('spec-0')], 1, pool=pool,
                                        cfg=self.cfg, out=lambda s: None, spawn_fn=spawn_fn,
                                        refresh=refresh)
        self.assertEqual([r.job for r, _ in launched], ['spec-0', 'fix-b-0009'])
        self.assertEqual(waits, [])

    def test_id_ranges_reserved_from_threads_never_overlap(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(lambda j: spawn_mod.reserve_id_range(self.product, f'j{j}'),
                              range(16)))
        self.assertEqual(len(set(got)), 16)

    def test_real_spawns_in_parallel_all_land_in_the_ledger(self):
        rows = [feature_row(f'spec-{i}', item=f'F-000{i}') for i in range(4)]
        acct = pool_mod.Account('acct-a', role='local', cap=8)
        pool = pool_mod.Pool([acct], quota_source=quota_mod.FakeQuotaSource({}))
        rt = runtime_mod.FakeRuntime([{'running': True}] * 10)
        launched, waits = wave_mod.wave(self.product, rows, 4, pool=pool, runtime=rt,
                                        cfg=self.cfg, out=lambda s: None)
        self.assertEqual(waits, [])
        self.assertEqual(sorted(pool_mod.load_sessions(self.product)),
                         [f'spec-{i}' for i in range(4)])
        ranges = {rec['id_range'] for _, rec in launched}
        self.assertEqual(len(ranges), 4)
        for _, rec in launched:
            self.assertTrue(os.path.isdir(rec['worktree']))


class TestHealth(Home):
    def spawn(self, job, step):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b', runtime=rt,
                               cfg=self.cfg)

    def commit(self, wt, name='x'):
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, name), 'w') as f:
            f.write(name)
        git('add', name, cwd=wt)
        git('commit', '-q', '-m', name, cwd=wt)

    def land(self, wt, branch):
        """Push the branch and fast-forward origin/main onto it, as a harvest does."""
        git('push', '-q', 'origin', branch, cwd=wt)
        git('push', '-q', 'origin', branch + ':main', cwd=wt)

    def harvest_land(self, wt, branch, job):
        """As ``asf.harvest.harvest.land_ff`` actually lands a branch: it rebases the tip in a
        throwaway worktree (a new commit, not the one sitting in ``wt``), pushes that to main,
        deletes the remote branch, and marks the session ``harvested`` — leaving ``wt``'s own
        branch behind with no remote counterpart at all (B-0049)."""
        git('push', '-q', 'origin', branch, cwd=wt)
        tree = git('rev-parse', 'HEAD^{tree}', cwd=wt)
        sha = git('commit-tree', tree, '-p', 'origin/main', '-m', 'landed', cwd=wt)
        git('push', '-q', 'origin', f'{sha}:refs/heads/main', cwd=wt)
        git('push', '-q', 'origin', '--delete', branch, cwd=wt)
        pool_mod.update_session(self.product, job, harvested=sha)
        return sha

    def test_b0041_relaunch_starts_a_clean_run(self):
        # first run fails; the relaunch must not inherit ended/end_reason from it
        rt = runtime_mod.FakeRuntime([{'ok': False, 'pid': 21}, {'running': True, 'pid': 22}])
        spawn_mod.spawn(self.product, feature_row('again'), self.acct(), 'b', runtime=rt, cfg=self.cfg)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(pool_mod.load_sessions(self.product)['again']['end_reason'], 'failed')
        wt = pool_mod.load_sessions(self.product)['again']['worktree']
        git('worktree', 'remove', '--force', wt, cwd=self.repo)
        git('branch', '-D', pool_mod.load_sessions(self.product)['again']['branch'], cwd=self.repo)
        spawn_mod.spawn(self.product, feature_row('again'), self.acct(), 'b', runtime=rt, cfg=self.cfg)
        s = pool_mod.load_sessions(self.product)['again']
        for k in ('ended', 'end_reason', 'rc', 'corrected', 'operator_flagged', 'harvested'):
            self.assertNotIn(k, s, k)
        self.assertEqual(s['pid'], 22)
        found = health_mod.health(self.product, alive=lambda pid: pid == 22, out=lambda s: None)
        self.assertFalse([f for f in found if f[0] == 'again' and f[1] == 'ended'])
        self.assertIn('again', [x['job'] for x in pool_mod.live_sessions(self.product)])

    def test_transitions(self):
        self.spawn('done', {'ok': True, 'pid': 11})
        self.spawn('gone', {'running': True, 'pid': 12})
        self.spawn('live', {'running': True, 'pid': 13})
        found = health_mod.health(self.product, alive=lambda pid: pid == 13, out=lambda s: None)
        ended = {j: d for j, w, d in found if w == 'ended'}
        # 'done' finished ok but never pushed a thing — a finished result is only 'finished'
        # once its branch is actually pushed, or the item it worked stays blocked forever:
        # health says finished, harvest never sees a branch to land (B-0051).
        self.assertEqual(ended, {'done': 'failed: not pushed: 0 uncommitted file(s), 0 unpushed commit(s)',
                                 'gone': 'dead pid'})
        s = pool_mod.load_sessions(self.product)
        self.assertEqual(s['done']['end_reason'], ended['done'])
        self.assertFalse(s['live'].get('ended'))
        # neither 'done' nor 'gone' carries anything ahead of main or uncommitted, so B-0049's
        # rule reaps both as empty rather than keeping them around forever unfinished.
        reapable = {j: d for j, w, d in found if w == 'reapable'}
        self.assertEqual(reapable, {'done': 'empty', 'gone': 'empty'})

    def _ls_remote_one(self, cwd, branch):
        p = subprocess.run(['git', 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}'],
                           cwd=cwd, capture_output=True, text=True)
        want = f'refs/heads/{branch}'
        return next((ln.split()[0] for ln in p.stdout.splitlines()
                     if ln.split()[1:] == [want]), '') if p.returncode == 0 else ''

    def test_remote_heads_answers_as_ls_remote_of_the_one_branch(self):
        # ls-remote's bare pattern is a tail match: `B-1` finds refs/heads/B-1 and
        # refs/heads/x/B-1 alike, and `x/B-1` sorts first — the snapshot answers the exact
        # refs/heads/<branch> only (2026-10-04: archive/<branch> read as the branch held a
        # session's Stop over a commit origin already had)
        rec = self.spawn('snap', {'running': True, 'pid': 5})
        wt = rec['worktree']
        self.commit(wt, 'a')
        for b in ('fix/B-1', 'cloud/fix/B-1', 'B-1', 'zz/B-1'):
            git('push', '-q', 'origin', f'HEAD:refs/heads/{b}', cwd=wt)
            self.commit(wt, b.replace('/', '-'))
        heads = lifecycle.RemoteHeads()
        for cwd in (wt, self.repo):
            for b in ('fix/B-1', 'B-1', 'cloud/fix/B-1', 'missing', 'fix', 'main', rec['branch']):
                with self.subTest(cwd=cwd, branch=b):
                    self.assertEqual(heads.sha(cwd, b), self._ls_remote_one(cwd, b))
        self.assertIsNone(heads.sha(wt, 'B-*'))   # a glob: git's own answer, never guessed
        self.assertIsNone(heads.sha(wt, ''))

    def test_the_worktree_pass_asks_origin_once_and_finds_the_same(self):
        for i, pushed in enumerate((True, False, True)):
            rec = self.spawn(f'wt{i}', {'running': True, 'pid': 30 + i})
            self.commit(rec['worktree'], f'f{i}')
            if pushed:
                git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)  # settle

        calls = []
        real = subprocess.run

        def counting(args, *a, **kw):
            if isinstance(args, (list, tuple)) and 'ls-remote' in args:
                calls.append(list(args))
            return real(args, *a, **kw)

        with mock.patch.object(lifecycle.RemoteHeads, 'sha', return_value=None), \
                mock.patch.object(lifecycle.subprocess, 'run', side_effect=counting):
            per_branch = health_mod.health(self.product, alive=lambda pid: False,
                                           out=lambda s: None)
        before, calls[:] = list(calls), []
        with mock.patch.object(lifecycle.subprocess, 'run', side_effect=counting):
            once = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(once, per_branch)
        # the dead runs' revisit asks per branch as before (a publish may push between them);
        # the worktree pass — one question per worktree before — asks origin once
        whole = [c for c in calls if c[-1] == 'origin']
        self.assertEqual(len(whole), 1, calls)
        self.assertEqual(len(calls) - 1, len(before) - 3, (before, calls))

    def test_b0028_dead_pid_is_rejudged_when_the_result_arrives(self):
        rec = self.spawn('late', {'running': True, 'pid': 12})
        self.commit(rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('late', 'ended', 'dead pid'), found)
        with open(rec['log'], 'a') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}) + '\n')
            f.write(json.dumps({'type': 'system', 'subtype': 'task_notification'}) + '\n')
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('late', 're-judged', 'finished'), found)
        s = pool_mod.load_sessions(self.product)['late']
        self.assertEqual(s['end_reason'], 'finished')
        self.assertEqual(s['rc'], 0)
        # settled: the next pass leaves it alone
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse([f for f in found if f[1] == 're-judged'])

    def test_b0051_rejudged_result_is_ok_but_never_pushed_stays_failed(self):
        # the same re-judge path (B-0028), but the branch was never pushed — an `ok` result
        # must not re-judge to 'finished' or the item it worked on is blocked forever: health
        # says finished, harvest never sees a branch to land, and spawn refuses the worktree.
        rec = self.spawn('late', {'running': True, 'pid': 12})
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        with open(rec['log'], 'a') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}) + '\n')
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        reason = dict((j, d) for j, w, d in found if w == 're-judged')['late']
        self.assertTrue(reason.startswith('failed: not pushed:'), reason)
        s = pool_mod.load_sessions(self.product)['late']
        self.assertEqual(s['end_reason'], reason)
        self.assertEqual(s['rc'], 1)

    def test_b0075_a_self_reported_unpushed_result_is_held_for_correction_too(self):
        # the brief's own words: "pushed: no is read as that failure at once" — a session that
        # honestly backgrounds the suite and says so must be held for correction exactly like a
        # git-detected unpushed run is, or an honest report is a dead end nobody comes back to
        text = ('I stopped to wait for the background test run.\n\n'
                'REPORT\nitem: F-0001\nkind: coder\nstatus: partial\nbranch: b\n'
                'pushed: no — the suite is still running in the background\n'
                'commits: none\ntests: python3 -m unittest (background)\nleft out: the push\n')
        self.spawn('waiting', {'ok': True, 'result': text, 'pid': 61})
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('waiting', 'ended', 'failed: unpushed work'), found)
        held = [d for j, w, d in found if w == 'held']
        self.assertTrue(held, found)
        s = pool_mod.load_sessions(self.product)['waiting']
        self.assertEqual(s['rounds'], 1)

    def test_b0076_a_finished_run_with_an_empty_branch_is_held_not_finished_forever(self):
        # a session that ends ok and pushes its branch, but never committed anything on it, has
        # nothing for harvest to land — read "finished" it sits `eligible` forever, and the item
        # it worked stays "busy" for ever, blocking every task waiting on its footprint
        rec = self.spawn('empty', {'ok': True, 'pid': 71})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('empty', 'ended', 'failed: empty branch: nothing to land'), found)
        held = [d for j, w, d in found if w == 'held']
        self.assertTrue(held, found)
        s = pool_mod.load_sessions(self.product)['empty']
        self.assertEqual(s['rounds'], 1)
        self.assertFalse(lifecycle.eligible(s))  # never sits `awaiting harvest` with nothing to land

    def _lane_landed_then_relaunched(self, second_step=None):
        """Run one on a lane branch, squash-merged by the native PR lane (marked harvested at
        merge), then a second session on the same branch that writes nothing and pushes."""
        rec = self.spawn('plan-f-0079', {'ok': True, 'pid': 91})
        self.commit(rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        pool_mod.update_session(self.product, 'plan-f-0079', harvested='6' * 40, correction=None)
        time.sleep(1.1)  # the relaunch's `started` must sort after the first run's
        git('push', '-q', 'origin', '--delete', rec['branch'], cwd=rec['worktree'])
        shutil.rmtree(rec['worktree'])
        git('worktree', 'prune', cwd=self.repo)
        git('branch', '-D', rec['branch'], cwd=self.repo)
        again = self.spawn('plan-f-0079', second_step or {'ok': True, 'pid': 92})
        git('push', '-q', 'origin', again['branch'], cwd=again['worktree'])
        return again

    def test_an_empty_run_on_a_lane_already_landed_is_landed_not_sent_back(self):
        # PR #740 squash-merged cloud/plan-F-0079; a later session on the branch found the plan
        # on the trunk and wrote nothing. It is landed — no correction, no FIX → CORRECT row
        self._lane_landed_then_relaunched()
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse([f for f in found if f[1] == 'held'], found)
        self.assertIn('landed', [w for j, w, d in found if j == 'plan-f-0079'])
        s = pool_mod.load_sessions(self.product)['plan-f-0079']
        self.assertEqual(s['harvested'], '6' * 40)
        self.assertEqual(lifecycle.corrections(pool_mod.sessions_path(self.product)), {})

    def test_a_correction_already_written_on_a_landed_lane_is_neutralised_next_tick(self):
        self._lane_landed_then_relaunched()
        path = pool_mod.sessions_path(self.product)
        # what the unfixed health wrote at 06:49:33Z
        pool_mod.update_session(self.product, 'plan-f-0079', ended='2099-01-01T00:00:00Z',
                                end_reason='failed: ' + lifecycle.EMPTY_BRANCH, rounds=1,
                                correction={'kind': 'unpushed', 'at': '2099-01-01T00:00:00Z',
                                            'text': lifecycle.empty_branch_text()})
        # derived at once: no pending correction, so no FIX → CORRECT row
        self.assertEqual(lifecycle.corrections(path), {})
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active', 'decided': True}}
        got, _ids = rows.correction_rows(items, self.product, set(), lifecycle.corrections(path))
        self.assertEqual(got, [])
        # and the next health pass records it landed, the correction dropped
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn('landed', [w for j, w, d in found if j == 'plan-f-0079'])
        s = pool_mod.load_sessions(self.product)['plan-f-0079']
        self.assertEqual((s['harvested'], s.get('correction')), ('6' * 40, None))

    def test_reap_only_when_pushed(self):
        rec = self.spawn('done', {'ok': True, 'pid': 11})
        wt = rec['worktree']
        self.commit(wt)
        self.land(wt, rec['branch'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'reapable', 'ended'), found)
        self.assertTrue(os.path.isdir(wt))
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'reaped', 'ended'), found)
        self.assertFalse(os.path.exists(wt))

    def test_b0007_reap_releases_the_id_range(self):
        rec = self.spawn('done', {'ok': True, 'pid': 11})
        wt = rec['worktree']
        self.commit(wt)
        self.land(wt, rec['branch'])
        rows = spawn_mod._read_ranges(spawn_mod.id_ranges_path(self.product))
        self.assertIn('done', [j for j, _ in rows])
        health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        rows = spawn_mod._read_ranges(spawn_mod.id_ranges_path(self.product))
        self.assertNotIn('done', [j for j, _ in rows])

    def test_local_commit_not_pushed_is_published_by_the_factory(self):
        # B-0051: the result says ok, but a commit made after the last push never went out —
        # that must not be read as 'finished' with nothing on origin for harvest to land.
        # B-0056: the factory publishes what a clean tree holds, then judges — finished, and
        # origin has the commit; the session never needed a push it may not make.
        rec = self.spawn('done', {'ok': True})
        wt = rec['worktree']
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        self.commit(wt, 'x')
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['done']
        self.assertEqual(run['end_reason'], 'finished')
        self.assertIn(('done', 'published', f"published {rec['branch']} at " + git('rev-parse', '--short', 'HEAD', cwd=wt)
                       + ' (rebased; lease held)'), found)
        self.assertEqual(git('ls-remote', '--heads', 'origin', rec['branch'], cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))

    def test_b0056_a_rebased_clean_run_is_published_and_finished(self):
        # spawn rebased the branch (or the session finished a conflicted rebase): HEAD is off
        # origin/<branch>, every patch is there; finished means pushed — the factory pushes
        rec = self.spawn('rebased', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'fix')
        git('push', '-q', 'origin', branch, cwd=wt)
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        self.commit(other, 'trunk-moves')
        git('push', '-q', 'origin', 'HEAD:main', cwd=other)
        git('fetch', '-q', 'origin', cwd=wt)
        git('rebase', '-q', 'origin/main', cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(pool_mod.load_sessions(self.product)['rebased']['end_reason'], 'finished')
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))
        self.assertTrue(any(w == 'published' for _j, w, _d in found), found)

    def test_an_ended_unpushed_run_is_published_again_once_the_factory_can(self):
        # a product's F-0094: the factory's publish refused the session's commits (its rebase
        # conflicted), the run ended `failed: not pushed` with a correction — and no later pass
        # ever tried again. The publish fix landed, the worktree sat publishable, and the item
        # waited for another whole session. An ended unpushed run's worktree is published again
        # on every pass; once it goes out the run is re-judged and its correction dropped.
        rec = self.spawn('stuck', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'fix')
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(self.product, 'stuck', ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1,
                                correction={'kind': lifecycle.UNPUSHED,
                                            'text': lifecycle.unpushed_text(reason),
                                            'at': '2026-09-27T07:00:45Z'})
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['stuck']
        self.assertTrue(any(j == 'stuck' and w == 'published' for j, w, _d in found), found)
        self.assertEqual(run['end_reason'], 'finished')
        self.assertIsNone(run.get('correction'))
        self.assertEqual(git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))

    def test_a_finished_republish_opens_the_items_pr_in_the_same_pass(self):
        # B-0039: the park's only cause was the failed push (kind: UNPUSHED) — once health
        # publishes it this pass, it must also open (or adopt) the item's PR right here, not
        # leave the branch pushed with no PR until some later pass happens to notice.
        rec = self.spawn('stuck', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'fix')
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(self.product, 'stuck', ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1,
                                correction={'kind': lifecycle.UNPUSHED,
                                            'text': lifecycle.unpushed_text(reason),
                                            'at': '2026-09-27T07:00:45Z'})
        with mock.patch('asf.ci_queue._open_pr', return_value=(42, '')) as opened:
            found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                      out=lambda s: None)
        self.assertEqual(opened.call_args[0][:3], (self.product, branch, 'F-0001'))
        run = pool_mod.load_sessions(self.product)['stuck']
        self.assertEqual(run['end_reason'], 'finished')
        self.assertIsNone(run.get('correction'))
        self.assertIn(('stuck', 'pr-opened', '#42'), found)

    def test_conventions_flags_health_opens_pr_false_opts_out(self):
        product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                         'job_grants': [self.grant],
                                         'conventions': {'flags': {'health_opens_pr': False}}})
        rt = runtime_mod.FakeRuntime([{'ok': True}])
        wt = spawn_mod.spawn(product, feature_row('stuck3'), self.acct(), 'b', runtime=rt,
                             cfg=self.cfg)['worktree']
        self.commit(wt, 'fix')
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(product, 'stuck3', ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1,
                                correction={'kind': lifecycle.UNPUSHED,
                                            'text': lifecycle.unpushed_text(reason),
                                            'at': '2026-09-27T07:00:45Z'})
        with mock.patch('asf.ci_queue._open_pr') as opened:
            found = health_mod.health(product, fix=True, alive=lambda pid: False,
                                      out=lambda s: None)
        opened.assert_not_called()
        run = pool_mod.load_sessions(product)['stuck3']
        self.assertEqual(run['end_reason'], 'finished')
        self.assertIsNone(run.get('correction'))
        self.assertFalse(any(j == 'stuck3' and w.startswith('pr-') for j, w, _d in found), found)

    def test_a_finished_republish_with_no_pr_host_still_clears_the_park(self):
        # the sample product here lands with no PR host (no repo_slug): opening the PR cannot
        # succeed, but the park whose only cause was the push must still clear — the item is
        # never left parked again just because there was nothing to open.
        rec = self.spawn('stuck2', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'fix')
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(self.product, 'stuck2', ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1,
                                correction={'kind': lifecycle.UNPUSHED,
                                            'text': lifecycle.unpushed_text(reason),
                                            'at': '2026-09-27T07:00:45Z'})
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['stuck2']
        self.assertEqual(run['end_reason'], 'finished')
        self.assertIsNone(run.get('correction'))
        self.assertTrue(any(j == 'stuck2' and w == 'pr-not-opened' for j, w, _d in found), found)

    def _t0338_shape(self, job):
        """A run whose worktree is the lane's rebase arriving (a product's T-0338): origin/<branch>
        holds its own commit and a copy of a trunk commit on an old base; the trunk landed that
        commit and changed the own commit's file; the head is the own commit, resolved by hand,
        on the new trunk. ``(rec, wt, branch, old tip)``."""
        rec = self.spawn(job, {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'copy')                       # a copy of what the trunk lands
        self.commit(wt, 'own')                        # the branch's own commit
        git('push', '-q', 'origin', branch, cwd=wt)
        old = git('rev-parse', 'HEAD', cwd=wt)
        other = tempfile.mkdtemp(prefix='trunk_')
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        git('clone', '-q', git('remote', 'get-url', 'origin', cwd=wt), other, cwd=wt)
        self.commit(other, 'copy')                    # the same patch lands on the trunk
        with open(os.path.join(other, 'own'), 'w') as f:
            f.write('trunk version')                  # and the trunk changes `own`
        git('add', 'own', cwd=other)
        git('commit', '-q', '-m', 'trunk own', cwd=other)
        git('push', '-q', 'origin', 'HEAD:main', cwd=other)
        git('fetch', '-q', 'origin', cwd=wt)
        r = subprocess.run(['git', 'rebase', '-q', 'origin/main'], cwd=wt, capture_output=True)
        self.assertNotEqual(r.returncode, 0)          # the conflict the lane named
        with open(os.path.join(wt, 'own'), 'w') as f:
            f.write('resolved')
        git('add', 'own', cwd=wt)
        subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=wt,
                       capture_output=True, check=True)
        self.assertEqual(git('merge-base', 'HEAD', 'origin/main', cwd=wt),
                         git('rev-parse', 'origin/main', cwd=wt))
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(self.product, job, ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1,
                                correction={'kind': lifecycle.UNPUSHED,
                                            'text': lifecycle.unpushed_text(reason),
                                            'at': '2026-09-27T07:00:45Z'})
        return rec, wt, branch, old

    def test_the_republish_pass_publishes_the_lanes_rebase_over_the_stale_remote(self):
        # the ping-pong's exit: an ended run whose head is the lane's rebase is published by the
        # republish pass over the stale remote (old tip archived) — never rebased back onto it,
        # never refused "rebase onto origin/<branch>" — and the run is re-judged finished
        _rec, wt, branch, old = self._t0338_shape('pingpong')
        head = git('rev-parse', 'HEAD', cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None)
        lines = [d for j, w, d in found if j == 'pingpong' and w == 'published']
        self.assertTrue(lines, found)
        self.assertIn('old tip kept as', lines[0])
        self.assertNotIn('refused', lines[0])
        run = pool_mod.load_sessions(self.product)['pingpong']
        self.assertEqual(run['end_reason'], 'finished')
        self.assertIsNone(run.get('correction'))
        self.assertEqual(self._ls_remote_one(wt, branch), head)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt), head)
        self.assertEqual(self._ls_remote_one(wt, lifecycle.copies_archive(branch, old)), old)

    def test_the_republish_pass_carries_a_commit_the_remote_gained_meanwhile(self):
        # someone pushed onto origin/<branch> while the run was stuck: the pass carries that
        # commit onto the rebase and publishes; nothing of theirs is dropped
        _rec, wt, branch, _old = self._t0338_shape('carried')
        other = tempfile.mkdtemp(prefix='person_')
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        git('clone', '-q', '-b', branch, git('remote', 'get-url', 'origin', cwd=wt), other, cwd=wt)
        self.commit(other, 'theirs')
        git('push', '-q', 'origin', branch, cwd=other)
        theirs = git('rev-parse', 'HEAD', cwd=other)
        head = git('rev-parse', 'HEAD', cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None)
        lines = [d for j, w, d in found if j == 'carried' and w == 'published']
        self.assertTrue(lines, found)
        self.assertIn(f'carried 1 commit(s) the rebase dropped ({theirs[:9]} theirs)', lines[0])
        run = pool_mod.load_sessions(self.product)['carried']
        self.assertEqual(run['end_reason'], 'finished')
        remote = self._ls_remote_one(wt, branch)
        self.assertEqual(remote, git('rev-parse', 'HEAD', cwd=wt))
        self.assertEqual(git('rev-parse', 'HEAD~1', cwd=wt), head)
        self.assertEqual(git('log', '-1', '--format=%s', remote, cwd=wt), 'theirs')

    def test_an_ended_unpushed_run_still_refused_gets_the_precise_refusal(self):
        # the retried publish is refused again: the pending generic "commit and push what you
        # have" is replaced by the factory's own refusal line, so the next session reads what
        # blocked the push — and a refusal already carried is not re-logged every pass
        rec = self.spawn('refused', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'fix')
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(self.product, 'refused', ended='2026-09-27T07:00:45Z',
                                end_reason=reason, rc=1,
                                correction={'kind': lifecycle.UNPUSHED,
                                            'text': lifecycle.unpushed_text(reason),
                                            'at': '2026-09-27T07:00:45Z'})
        line = f'publish {branch} refused: redact: a.txt:1 names a worker account'
        with mock.patch.object(lifecycle, 'publish', return_value=(False, line)) as pub:
            found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
            self.assertEqual(pub.call_count, 1)
            run = pool_mod.load_sessions(self.product)['refused']
            self.assertIn('redact: a.txt:1', run['correction']['text'])
            self.assertNotIn('commit and push what you have', run['correction']['text'])
            self.assertIn(('refused', 'published', line), found)
            # F-0228: the pair it was refused at, and no second push while it reads the same
            self.assertEqual(run['publish_refused_heads'],
                             git('rev-parse', 'HEAD', cwd=wt) + ' '
                             + (self._ls_remote_one(wt, branch) or '-'))
            found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
            self.assertEqual(pub.call_count, 1)          # was 2: the hook is not run again
            self.assertFalse(any(j == 'refused' and w == 'published' for j, w, _d in found), found)
            self.commit(wt, 'another')                   # the head moves: the refusal is forgotten
            found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
            self.assertEqual(pub.call_count, 2)

    def test_b0063_stale_local_lane_branches_are_pruned_strays_named(self):
        # thirty-seven local branches sat in the scheduler's checkout after their worktrees
        # were reaped by hand or by a landing that rebased the tip: nothing pruned them
        other = os.path.join(self.tmp, 'other')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        self.commit(other, 'landed-fix')
        git('push', '-q', 'origin', 'HEAD:main', cwd=other)
        git('fetch', '-q', 'origin', cwd=self.repo)
        git('branch', 'fix/B-0001', 'origin/main', cwd=self.repo)  # on the trunk already
        git('branch', 'fix/B-0002', 'origin/main~1', cwd=self.repo)
        git('branch', 'fix/B-0003', 'origin/main~1', cwd=self.repo)
        git('branch', 'other/B-0004', 'origin/main~1', cwd=self.repo)  # not a lane prefix
        wt = os.path.join(self.tmp, 'wt-b0003')
        git('worktree', 'add', '-q', wt, 'fix/B-0003', cwd=self.repo)
        self.commit(wt, 'unlanded-work')
        git('branch', 'fix/B-0005', 'fix/B-0003', cwd=self.repo)  # same unlanded work, no worktree
        pool_mod.update_session(self.product, 'fix-bug-b-0002', harvested='superseded', branch='fix/B-0002')
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                              'conventions': {'branch_prefixes': {'fix': 'fix/', 'code': 'worker/'}}})
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        by = {b: (w, d) for b, w, d in found if b.startswith(('fix/', 'other/'))}
        self.assertEqual(by['fix/B-0001'], ('pruned', 'every change on origin/main'))
        self.assertEqual(by['fix/B-0002'], ('pruned', 'landed by the lane (superseded)'))
        self.assertEqual(by['fix/B-0005'], ('stray', '1 patch(es) not on origin/main, never landed — no worktree'))
        self.assertNotIn('fix/B-0003', by)  # held by a worktree: the worktree rules own it
        self.assertNotIn('other/B-0004', by)
        branches = git('branch', '--list', '--format=%(refname:short)', cwd=self.repo).split()
        self.assertNotIn('fix/B-0001', branches)
        self.assertNotIn('fix/B-0002', branches)
        self.assertIn('fix/B-0005', branches)
        self.assertIn('fix/B-0003', branches)

    def test_b0094_uncommitted_work_of_an_ok_run_is_committed_and_published_by_the_factory(self):
        # 19% of sessions ended `failed: not pushed: N uncommitted file(s)`: the work was done and
        # lost to a missing commit. The factory commits it, signed off, and publishes it.
        rec = self.spawn('dirty', {'ok': True})
        wt = rec['worktree']
        self.commit(wt, 'fix')
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        with open(os.path.join(wt, 'loose'), 'w') as f:
            f.write('loose')
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(pool_mod.load_sessions(self.product)['dirty']['end_reason'], 'finished')
        self.assertTrue(any(w == 'published' for _j, w, _d in found), found)
        self.assertEqual(git('status', '--porcelain', cwd=wt), '')
        self.assertEqual(git('ls-remote', '--heads', 'origin', rec['branch'], cwd=wt).split()[0],
                         git('rev-parse', 'HEAD', cwd=wt))
        self.assertIn('Signed-off-by:', git('log', '-1', '--format=%B', cwd=wt))

    def test_b0094_a_failed_run_is_never_committed_for(self):
        rec = self.spawn('broken', {'ok': False})
        wt = rec['worktree']
        with open(os.path.join(wt, 'loose'), 'w') as f:
            f.write('loose')
        health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(git('status', '--porcelain', cwd=wt), '?? loose')

    def test_orphan_worktree(self):
        path = os.path.join(spawn_mod.worktrees_dir(self.product), 'stray')
        git('worktree', 'add', '-q', '-b', 'stray', path, 'origin/main', cwd=self.repo)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('stray', 'keep', 'orphan: branch not pushed'), found)
        git('push', '-q', 'origin', 'stray', cwd=path)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('stray', 'keep', 'orphan: no commits yet'), found)
        self.commit(path)
        self.land(path, 'stray')
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('stray', 'reaped', 'orphan'), found)

    def test_b0019_live_session_with_no_commits_survives_and_is_opening(self):
        rec = self.spawn('fresh', {'running': True, 'pid': 21})
        wt = rec['worktree']
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: True, out=lambda s: None)
        self.assertTrue([f for f in found if f[:2] == ('fresh', 'opening')], found)
        self.assertFalse([f for f in found if f[1] in ('reaped', 'reapable')], found)
        self.assertTrue(os.path.isdir(wt))

    def test_b0019_ended_session_with_no_commits_is_not_merged(self):
        # B-0076: never committed to, this is `failed: empty branch`, not `finished` — it never
        # reads as landed, and reap takes the worktree at once since there is nothing to lose
        rec = self.spawn('empty', {'ok': True, 'pid': 22})
        wt = rec['worktree']
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('empty', 'ended', 'failed: empty branch: nothing to land'), found)
        self.assertIn(('empty', 'reaped', 'empty'), found)
        self.assertFalse(os.path.isdir(wt))

    def test_b0019_pushed_but_unlanded_commit_is_kept_until_it_reaches_main(self):
        rec = self.spawn('done', {'ok': True, 'pid': 23})
        wt = rec['worktree']
        self.commit(wt)
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'keep', 'ended: not in origin/main'), found)
        self.assertTrue(os.path.isdir(wt))
        self.land(wt, rec['branch'])
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'reaped', 'ended'), found)
        self.assertFalse(os.path.exists(wt))

    def test_b0049_harvested_worktree_is_reaped_though_never_pushed_from_here(self):
        # harvest rebases the tip in its own throwaway worktree and deletes the remote branch,
        # so `pushed()` can never see this worktree's HEAD on origin — it would stay 'reapable'
        # forever without the `harvested` short-circuit.
        rec = self.spawn('done', {'ok': True, 'pid': 11})
        wt = rec['worktree']
        self.commit(wt)
        sha = self.harvest_land(wt, rec['branch'], 'done')
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'reapable', f'landed {sha}'), found)
        lines = []
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lines.append)
        self.assertIn(('done', 'reaped', f'landed {sha}'), found)
        self.assertIn(f'reaped done (landed {sha})', lines)
        self.assertFalse(os.path.exists(wt))

    def test_b0049_operator_stopped_session_with_empty_worktree_is_reaped(self):
        rec = self.spawn('idle', {'running': True, 'pid': 30})
        wt = rec['worktree']
        pool_mod.update_session(self.product, 'idle', ended=pool_mod.now_iso(),
                                end_reason='stopped by operator')
        lines = []
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lines.append)
        self.assertIn(('idle', 'reaped', 'empty'), found)
        self.assertIn('reaped idle (empty)', lines)
        self.assertFalse(os.path.exists(wt))

    def test_b0049_stopped_session_with_local_commits_is_kept(self):
        rec = self.spawn('wip', {'running': True, 'pid': 31})
        wt = rec['worktree']
        self.commit(wt)
        pool_mod.update_session(self.product, 'wip', ended=pool_mod.now_iso(),
                                end_reason='stopped by operator')
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('wip', 'keep', 'ended: session stopped by operator, not finished'), found)
        self.assertTrue(os.path.isdir(wt))


class NothingToLandHealthTests(Home):
    """F-0157 Task 3 §2: health's `elif` rewrites `failed: empty branch: …` to
    `lifecycle.NOTHING_TO_LAND` when the run's own report proves it — an adjudicate run's
    ruling, or a correct run answering a review hold the lane's restack already cleared — and
    never raises a hold, spends a round or writes a correction for such a run. F-0126's blocked
    park still wins over it, and every other kind is still held exactly as before."""

    def spawn(self, job, item, kind, step, branch=None):
        row = pool_mod.Row(job, item, kind=kind, model='Opus', branch=branch or f'{kind}/{job}')
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def _report(self, item, kind, branch, status='done', commits='none', extra=''):
        return (f'REPORT\nitem: {item}\nkind: {kind}\nstatus: {status}\nbranch: {branch}\n'
                f'pushed: yes\ncommits: {commits}\ntests: n/a\nleft out: none\n{extra}')

    def test_an_adjudicate_ruling_with_no_commit_is_rejudged_nothing_to_land(self):
        branch = 'adjudicate/F-0001a'
        text = self._report('F-0001', 'adjudicate', branch,
                            extra='ruling: overruled — the finding does not hold\n')
        rec = self.spawn('adjudicate-1', 'F-0001', 'adjudicate',
                         {'ok': True, 'pid': 81, 'result': text}, branch=branch)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        rejudged = [d for j, w, d in found if j == 'adjudicate-1' and w == 're-judged']
        self.assertEqual(len(rejudged), 1, found)
        self.assertIn('ruling filed', rejudged[0])
        self.assertFalse([f for f in found if f[0] == 'adjudicate-1' and f[1] in ('held', 'parked')],
                         found)
        s = pool_mod.load_sessions(self.product)['adjudicate-1']
        self.assertEqual(s['end_reason'], lifecycle.NOTHING_TO_LAND)
        self.assertNotIn('correction', s)
        self.assertNotIn('rounds', s)
        self.assertFalse(lifecycle.eligible(s))  # `eligible` reads `finished`, not `delivered`

    def test_the_same_ruling_with_needs_operator_is_parked_not_rejudged(self):
        item = 'F-0001'
        items = {item: {'id': item, 'title': 'a feature', 'writes': ['a.py']}}
        branch = 'adjudicate/F-0001b'
        text = self._report(item, 'adjudicate', branch,
                            extra='ruling: overruled — the finding does not hold\n'
                                  'NEEDS OPERATOR: which account owns this?\n')
        rec = self.spawn('adjudicate-2', item, 'adjudicate',
                         {'ok': True, 'pid': 82, 'result': text}, branch=branch)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertFalse([f for f in found if f[0] == 'adjudicate-2' and f[1] == 're-judged'], found)
        parked = [d for j, w, d in found if j == 'adjudicate-2' and w == 'parked']
        self.assertEqual(len(parked), 1, found)
        s = pool_mod.load_sessions(self.product)['adjudicate-2']
        self.assertEqual(s['correction']['kind'], lifecycle.BLOCKED)
        self.assertEqual(s['end_reason'], f'failed: {lifecycle.EMPTY_BRANCH}')

    def test_a_correct_run_answering_a_cleared_review_hold_is_rejudged(self):
        item = 'F-0001'
        review_path = 'docs/reviews/1-f-0001.md'
        pool_mod.append_session(self.product, {
            'job': 'held-review', 'item': item, 'kind': 'adjudicate', 'pid': 998,
            'started': '2026-01-01T00:00:00Z', 'branch': 'adjudicate/held-review',
            'ended': '2026-01-01T00:05:00Z', 'end_reason': lifecycle.FINISHED,
            'harvested': 'f' * 40,
            'correction': {'kind': lifecycle.REVIEW, 'at': '2026-01-01T00:10:00Z',
                           'text': f'{review_path} reads changes requested'}})
        branch = 'correct/F-0001'
        text = self._report(item, 'correct', branch)
        rec = self.spawn('correct-1', item, 'correct',
                         {'ok': True, 'pid': 83, 'result': text}, branch=branch)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        rejudged = [d for j, w, d in found if j == 'correct-1' and w == 're-judged']
        self.assertEqual(len(rejudged), 1, found)
        self.assertIn(review_path, rejudged[0])
        s = pool_mod.load_sessions(self.product)['correct-1']
        self.assertEqual(s['end_reason'], lifecycle.NOTHING_TO_LAND)

    def test_a_correct_run_with_nothing_unpushed_is_a_no_op_not_a_hold(self):
        # 2026-09-26..28: correct/review sessions ended `not pushed: 0 uncommitted file(s), 0
        # unpushed commit(s)` and each bought another session to push nothing
        branch = 'correct/F-0009'
        text = self._report('F-0009', 'correct', branch)
        self.spawn('correct-9', 'F-0009', 'correct', {'ok': True, 'pid': 91, 'result': text},
                   branch=branch)  # never pushed: its branch is not on origin
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('correct-9', 'ended', lifecycle.NOTHING_UNPUSHED), found)
        rejudged = [d for j, w, d in found if j == 'correct-9' and w == 're-judged']
        self.assertEqual(len(rejudged), 1, found)
        self.assertFalse([f for f in found if f[0] == 'correct-9' and f[1] in ('held', 'parked')],
                         found)
        s = pool_mod.load_sessions(self.product)['correct-9']
        self.assertEqual(s['end_reason'], lifecycle.NOTHING_TO_LAND)
        self.assertNotIn('correction', s)

    def test_a_coder_run_with_nothing_unpushed_is_still_held(self):
        branch = 'coder/F-0010'
        text = self._report('F-0010', 'coder', branch)
        self.spawn('coder-10', 'F-0010', 'coder', {'ok': True, 'pid': 92, 'result': text},
                   branch=branch)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertTrue([f for f in found if f[0] == 'coder-10' and f[1] == 'held'], found)

    def test_a_coder_run_with_an_empty_branch_and_a_clean_report_is_still_held(self):
        branch = 'coder/F-0001'
        text = self._report('F-0001', 'coder', branch)
        rec = self.spawn('coder-1', 'F-0001', 'coder',
                         {'ok': True, 'pid': 86, 'result': text}, branch=branch)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('coder-1', 'ended', f'failed: {lifecycle.EMPTY_BRANCH}'), found)
        self.assertTrue([f for f in found if f[0] == 'coder-1' and f[1] == 'held'], found)
        s = pool_mod.load_sessions(self.product)['coder-1']
        self.assertEqual(s['correction']['kind'], lifecycle.EMPTY)  # P6: EMPTY, not UNPUSHED

    def test_an_adjudicate_run_with_a_partial_status_is_still_held(self):
        branch = 'adjudicate/F-0001c'
        text = self._report('F-0001', 'adjudicate', branch, status='partial',
                            extra='ruling: overruled — the finding does not hold\n')
        rec = self.spawn('adjudicate-3', 'F-0001', 'adjudicate',
                         {'ok': True, 'pid': 84, 'result': text}, branch=branch)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('adjudicate-3', 'ended', f'failed: {lifecycle.EMPTY_BRANCH}'), found)
        self.assertTrue([f for f in found if f[0] == 'adjudicate-3' and f[1] == 'held'], found)

    def test_an_adjudicate_run_claiming_a_commit_is_still_held(self):
        branch = 'adjudicate/F-0001d'
        text = self._report('F-0001', 'adjudicate', branch, commits='deadbee fixed it',
                            extra='ruling: overruled — the finding does not hold\n')
        rec = self.spawn('adjudicate-4', 'F-0001', 'adjudicate',
                         {'ok': True, 'pid': 85, 'result': text}, branch=branch)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('adjudicate-4', 'ended', f'failed: {lifecycle.EMPTY_BRANCH}'), found)
        self.assertTrue([f for f in found if f[0] == 'adjudicate-4' and f[1] == 'held'], found)


class RulingReadyPublishTests(Home):
    """F-0176 Task 2: an adjudicate run whose REPORT rules a branch ready — a ``ruling:`` naming
    no ``blocked_on:`` — is published by the factory on the pass that judges it: the branch the
    session itself has no credential to put on origin (B-0123 went round six times because
    nothing ever did). Nothing is held: the run ends honestly ``failed``, and a ruling that names
    what it waits on opens no publish at all."""

    def spawn(self, job, item, branch, step):
        row = pool_mod.Row(job, item, kind='adjudicate', model='Opus', branch=branch)
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def commit(self, wt, name='x'):
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, name), 'w') as f:
            f.write(name)
        git('add', name, cwd=wt)
        git('commit', '-q', '-m', name, cwd=wt)

    def _ruling(self, blocked_on='none'):
        return ('REPORT\nitem: B-0123\nkind: adjudicate\nstatus: done\n'
                'ruling: the branch is ready — only the missing push credential stands between '
                'this HEAD and origin\n'
                f'blocked_on: {blocked_on}\n'
                'pushed: rebased deadbee — the factory publishes\n')

    def _remote_sha(self, wt, branch):
        p = subprocess.run(['git', 'ls-remote', '--heads', 'origin', branch], cwd=wt,
                           capture_output=True, text=True)
        return p.stdout.split()[0] if p.returncode == 0 and p.stdout.strip() else ''

    def test_a_ready_ruling_publishes_the_branch_and_holds_nothing(self):
        branch = 'fix/B-0123'
        rec = self.spawn('adjudicate-ready', 'B-0123', branch,
                         {'ok': False, 'pid': 91, 'result': self._ruling()})
        wt = rec['worktree']
        self.commit(wt, 'fix')  # a commit origin lacks: the session's own work
        head = git('rev-parse', 'HEAD', cwd=wt)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(self._remote_sha(wt, branch), head)
        published = [d for j, w, d in found if j == 'adjudicate-ready' and w == 'published']
        self.assertTrue(published, found)
        self.assertFalse([f for f in found if f[0] == 'adjudicate-ready' and f[1] in ('held', 'parked')],
                         found)
        s = pool_mod.load_sessions(self.product)['adjudicate-ready']
        self.assertNotIn('correction', s)
        self.assertEqual(s['end_reason'], 'failed')

    def test_a_ruling_that_names_what_it_waits_on_publishes_nothing(self):
        branch = 'fix/B-0124'
        rec = self.spawn('adjudicate-blocked', 'B-0124', branch,
                         {'ok': False, 'pid': 92, 'result': self._ruling(blocked_on='T-0519')})
        wt = rec['worktree']
        self.commit(wt, 'fix')
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertEqual(self._remote_sha(wt, branch), '')
        self.assertFalse([f for f in found if f[0] == 'adjudicate-blocked' and f[1] == 'published'],
                         found)

    def test_a_stray_ruling_field_off_an_adjudicate_kind_is_not_ready(self):
        branch = 'fix/B-0125'
        rec = self.spawn('coder-1', 'B-0125', branch,
                         {'ok': False, 'pid': 93, 'result': self._ruling()})
        s = dict(pool_mod.load_sessions(self.product)['coder-1'], kind='coder')
        self.assertFalse(health_mod.ruling_ready(s))

    def test_an_adjudicate_report_with_no_ruling_field_is_not_ready(self):
        branch = 'fix/B-0126'
        text = ('REPORT\nitem: B-0126\nkind: adjudicate\nstatus: done\n'
                'pushed: rebased deadbee — the factory publishes\n')
        self.spawn('adjudicate-plain', 'B-0126', branch, {'ok': False, 'pid': 94, 'result': text})
        s = pool_mod.load_sessions(self.product)['adjudicate-plain']
        self.assertFalse(health_mod.ruling_ready(s))

    def test_publishing_twice_publishes_once(self):
        branch = 'fix/B-0127'
        rec = self.spawn('adjudicate-twice', 'B-0127', branch,
                         {'ok': False, 'pid': 95, 'result': self._ruling()})
        wt = rec['worktree']
        self.commit(wt, 'fix')
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse([f for f in found if f[0] == 'adjudicate-twice' and f[1] == 'published'],
                         found)


class EmptyBranchParkTests(Home):
    """F-0157 Task 3 §3: `hold`'s `EMPTY_CAP` park, wired to its real switch — the item's
    second genuine empty end parks it, rather than spending a third, fourth and fifth session
    on its way to the round cap (P6, P7)."""

    def spawn(self, job, item, step, branch=None):
        row = pool_mod.Row(job, item, kind='coder', model='Opus', branch=branch or f'coder/{job}')
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def test_the_first_empty_end_holds_and_spends_a_round_without_parking(self):
        rec = self.spawn('empty-1', 'F-0001', {'ok': True, 'pid': 91})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertTrue([f for f in found if f[0] == 'empty-1' and f[1] == 'held'], found)
        self.assertFalse([f for f in found if f[0] == 'empty-1' and f[1] == 'parked'], found)
        s = pool_mod.load_sessions(self.product)['empty-1']
        self.assertEqual(s['correction']['kind'], lifecycle.EMPTY)
        self.assertNotIn('parked', s['correction'])
        self.assertEqual(s['rounds'], 1)
        self.assertNotIn('operator_flagged', s)

    def test_the_second_empty_end_on_one_item_parks_it(self):
        rec1 = self.spawn('empty-1', 'F-0001', {'ok': True, 'pid': 92})
        git('push', '-q', 'origin', rec1['branch'], cwd=rec1['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)

        rec2 = self.spawn('empty-2', 'F-0001', {'ok': True, 'pid': 93})
        git('push', '-q', 'origin', rec2['branch'], cwd=rec2['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        parked = [d for j, w, d in found if j == 'empty-2' and w == 'parked']
        self.assertEqual(len(parked), 1, found)
        self.assertFalse([f for f in found if f[0] == 'empty-2' and f[1] == 'held'], found)
        s = pool_mod.load_sessions(self.product)['empty-2']
        corr = s['correction']
        self.assertEqual(corr['kind'], lifecycle.EMPTY)
        self.assertIs(corr['parked'], True)
        self.assertEqual(corr['reason'], lifecycle.park_text(2))
        self.assertEqual(s['operator_flagged'], 1)

    def test_a_rejudged_nothing_to_land_end_does_not_count_toward_the_cap(self):
        rec1 = self.spawn('empty-1', 'F-0001', {'ok': True, 'pid': 94})
        git('push', '-q', 'origin', rec1['branch'], cwd=rec1['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)

        row = pool_mod.Row('adjudicate-mid', 'F-0001', kind='adjudicate', model='Opus',
                           branch='adjudicate/mid')
        text = ('REPORT\nitem: F-0001\nkind: adjudicate\nstatus: done\nbranch: adjudicate/mid\n'
                'pushed: yes\ncommits: none\ntests: n/a\nleft out: none\n'
                'ruling: overruled — the finding does not hold\n')
        rt = runtime_mod.FakeRuntime([{'ok': True, 'pid': 95, 'result': text}])
        rec_mid = spawn_mod.spawn(self.product, row, self.acct(), 'b', runtime=rt, cfg=self.cfg)
        git('push', '-q', 'origin', rec_mid['branch'], cwd=rec_mid['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('adjudicate-mid', 're-judged'), [(j, w) for j, w, d in found])
        self.assertEqual(lifecycle.empty_ends(pool_mod.sessions_path(self.product), 'F-0001'), 1)

        rec2 = self.spawn('empty-2', 'F-0001', {'ok': True, 'pid': 96})
        git('push', '-q', 'origin', rec2['branch'], cwd=rec2['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        parked = [d for j, w, d in found if j == 'empty-2' and w == 'parked']
        self.assertEqual(len(parked), 1, found)


class ABlockedRunIsParkedUntilTheCardChanges(Home):
    """F-0126 §5: a run that ends with nothing to land while its own report declares a question
    for a person is parked, not handed back to another session — relaunching it would only buy
    the same report again. The park lifts when the item's card changes, or `asf unpark` says so."""

    ITEM = 'T-0001'

    def spawn(self, job, step, item=ITEM):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, feature_row(job, item=item), self.acct(), 'b',
                               runtime=rt, cfg=self.cfg)

    def commit(self, wt, name='x'):
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, name), 'w') as f:
            f.write(name)
        git('add', name, cwd=wt)
        git('commit', '-q', '-m', name, cwd=wt)

    def card(self, **fields):
        return {self.ITEM: dict({'id': self.ITEM, 'title': 'a task', 'writes': ['a.py']}, **fields)}

    def test_an_empty_branch_with_a_needs_operator_line_is_parked(self):
        items = self.card()
        rec = self.spawn('parked', {'ok': True, 'pid': 71,
                                    'result': 'NEEDS OPERATOR: which account owns this?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertIn(('parked', 'ended', 'failed: empty branch: nothing to land'), found)
        self.assertFalse([f for f in found if f[1] == 'held'], found)
        parked = [d for j, w, d in found if w == 'parked']
        self.assertEqual(len(parked), 1, found)
        self.assertIn('which account owns this?', parked[0])
        s = pool_mod.load_sessions(self.product)['parked']
        corr = s['correction']
        self.assertEqual(corr['kind'], lifecycle.BLOCKED)
        self.assertIs(corr['parked'], True)
        self.assertEqual(s['operator_flagged'], 1)
        self.assertEqual(corr['card'], lifecycle.card_fingerprint(self.product, self.ITEM, items))
        self.assertNotIn('rounds', s)  # no round spent
        self.assertEqual(s['end_reason'], 'failed: empty branch: nothing to land')

    def test_a_status_blocked_report_with_no_needs_operator_line_parks_on_its_own_words(self):
        items = self.card()
        text = 'REPORT\nitem: T-0001\nkind: coder\nstatus: blocked\nleft out: needs a credential\n'
        rec = self.spawn('worded', {'ok': True, 'pid': 72, 'result': text})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        parked = [d for j, w, d in found if w == 'parked']
        self.assertEqual(len(parked), 1, found)
        self.assertIn('needs a credential', parked[0])

    def test_a_second_pass_with_the_card_unchanged_leaves_the_park_as_it_was(self):
        items = self.card()
        rec = self.spawn('parked', {'ok': True, 'pid': 73,
                                    'result': 'NEEDS OPERATOR: which account owns this?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None, items=items)
        before = pool_mod.load_sessions(self.product)['parked']['correction']
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        after = pool_mod.load_sessions(self.product)['parked']['correction']
        self.assertEqual(after, before)

    def test_a_needs_operator_line_with_a_readonly_command_is_run_and_attached(self):
        # B-0042: the NEEDS OPERATOR question names its exact command in backticks; health runs
        # it itself (it reads only) and attaches the output to the park.
        items = self.card()
        rec = self.spawn('parked2', {'ok': True, 'pid': 74, 'result':
                         'NEEDS OPERATOR: which commit is this — `git log -1 --format=%s`?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        parked = [d for j, w, d in found if w == 'parked']
        self.assertEqual(len(parked), 1, found)
        self.assertIn('git log -1 --format=%s', parked[0])
        corr = pool_mod.load_sessions(self.product)['parked2']['correction']
        self.assertEqual(corr['probe']['command'], 'git log -1 --format=%s')
        self.assertTrue(corr['probe']['output'])

    def test_a_needs_operator_line_with_a_mutating_command_is_parked_with_no_probe(self):
        items = self.card()
        rec = self.spawn('parked3', {'ok': True, 'pid': 75, 'result':
                         'NEEDS OPERATOR: should this merge — `git push origin main --force`?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None, items=items)
        corr = pool_mod.load_sessions(self.product)['parked3']['correction']
        self.assertNotIn('probe', corr)

    def test_the_card_gaining_writes_releases_the_park(self):
        items = self.card()
        rec = self.spawn('parked', {'ok': True, 'pid': 74,
                                    'result': 'NEEDS OPERATOR: which account owns this?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None, items=items)
        changed = self.card(writes=['a.py', 'b.py'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=changed)
        released = [d for j, w, d in found if w == 'released']
        self.assertEqual(released, [f'{self.ITEM}: the card changed — the park lifts'], found)
        s = pool_mod.load_sessions(self.product)['parked']
        self.assertNotIn('correction', s)

    def test_a_replan_of_its_feature_after_the_park_releases_it(self):
        feature = {'id': 'F-0001', 'type': 'feature', 'reshape': 'move T-0001 onto lib/x.py'}
        items = dict(self.card(parent='F-0001', type='task'), **{'F-0001': feature})
        rec = self.spawn('parked', {'ok': True, 'pid': 75,
                                    'result': 'NEEDS OPERATOR: which account owns this?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None, items=items)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertFalse([f for f in found if f[1] == 'released'], found)  # not re-planned yet
        replanned = dict(items, **{'F-0001': dict(feature, reshape_applied='abc',
                                                  reshape_applied_at='2999-01-01T00:00:00Z')})
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=replanned)
        released = [d for j, w, d in found if w == 'released']
        self.assertEqual(released, [f'{self.ITEM}: its Feature was re-planned — the park lifts'],
                         found)
        s = pool_mod.load_sessions(self.product)['parked']
        self.assertNotIn('correction', s)
        self.assertTrue(s['unparked'])

    def test_a_pushed_finished_run_with_a_needs_operator_line_is_not_parked(self):
        items = self.card()
        rec = self.spawn('done', {'ok': True, 'pid': 75,
                                  'result': 'REPORT\nitem: T-0001\nkind: coder\nstatus: done\n'
                                            'pushed: yes abc123\nNEEDS OPERATOR: a ruling later\n'})
        self.commit(rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertFalse([f for f in found if f[1] in ('parked', 'held')], found)
        self.assertIn(('done', 'ended', 'finished'), found)

    def test_an_empty_branch_landed_earlier_is_landed_not_parked(self):
        rec = self.spawn('plan-f-0079', {'ok': True, 'pid': 91})
        self.commit(rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        pool_mod.update_session(self.product, 'plan-f-0079', harvested='6' * 40, correction=None)
        time.sleep(1.1)
        git('push', '-q', 'origin', '--delete', rec['branch'], cwd=rec['worktree'])
        shutil.rmtree(rec['worktree'])
        git('worktree', 'prune', cwd=self.repo)
        git('branch', '-D', rec['branch'], cwd=self.repo)
        again = self.spawn('plan-f-0079', {'ok': True, 'pid': 92,
                                           'result': 'NEEDS OPERATOR: does this still apply?'},
                           item='F-0001')
        git('push', '-q', 'origin', again['branch'], cwd=again['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        self.assertIn('landed', [w for j, w, d in found if j == 'plan-f-0079'])

    def test_a_hook_refusal_takes_the_retry_branch_and_is_held_not_parked(self):
        items = self.card()
        rec = self.spawn('refused', {'ok': True, 'pid': 76,
                                     'result': 'NEEDS OPERATOR: is this account right?'})
        self.commit(rec['worktree'])  # uncommitted-nothing but unpushed: 'not pushed' reason
        with mock.patch.object(health_mod, 'push_retry',
                               return_value=(lifecycle.HOOK_REFUSED, 'redact: secrets.py:1 a key')):
            found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                      items=items)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        held = [d for j, w, d in found if w == 'held']
        self.assertTrue(held, found)

    def test_a_run_with_local_commits_and_a_question_is_held_never_parked_nothing_to_land(self):
        """A product's T-0349: a transplant committed locally, its push refused (not a
        fast-forward), the factory's publish refused too, and the report asked a person to
        publish it. Commits in the worktree are something to land: the run is held with its
        work as the correction's input, never parked "nothing to land"."""
        items = self.card()
        rec = self.spawn('transplant', {'ok': True, 'pid': 79,
                                        'result': 'NEEDS OPERATOR: run `asf land` — the sandbox '
                                                  'denies asf'})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'a')
        git('push', '-q', 'origin', branch, cwd=wt)
        other = tempfile.mkdtemp(prefix='person_')
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        git('clone', '-q', '-b', branch, git('remote', 'get-url', 'origin', cwd=wt), other, cwd=wt)
        with open(os.path.join(other, 'b'), 'w') as f:
            f.write('theirs')
        self.commit(other, 'b')   # origin gains a commit the worktree's head will not carry
        git('push', '-q', 'origin', branch, cwd=other)
        with open(os.path.join(wt, 'b'), 'w') as f:
            f.write('ours')
        git('add', 'b', cwd=wt)
        git('commit', '-qm', 'ours', cwd=wt)
        git('fetch', '-q', 'origin', cwd=wt)   # the session saw origin's tip, as T-0349's did
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        self.assertTrue([d for j, w, d in found if j == 'transplant' and w == 'held'], found)
        corr = pool_mod.load_sessions(self.product)['transplant']['correction']
        self.assertNotEqual(corr['kind'], lifecycle.BLOCKED)
        self.assertFalse(corr.get('parked'))
        self.assertNotIn('nothing to land', corr.get('reason') or '')

    def test_a_closed_items_park_is_released_not_kept(self):
        items = self.card()
        rec = self.spawn('parked', {'ok': True, 'pid': 77,
                                    'result': 'NEEDS OPERATOR: which account owns this?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None, items=items)
        closed = self.card(state='Resolved')
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=closed)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        self.assertIn(('parked', 'released', f'{self.ITEM} is Resolved: no correction'), found)

    def test_items_none_parks_nothing_and_raises_nothing(self):
        rec = self.spawn('noindex', {'ok': True, 'pid': 78,
                                     'result': 'NEEDS OPERATOR: which account owns this?'})
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=None)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        self.assertFalse([f for f in found if f[1] == 'released'], found)

    def test_a_correction_drops_once_a_different_branch_of_the_item_lands(self):
        """#46: an ``asf correct`` correction (or ruling) written against one branch of an item
        is automatically dropped once a *different* branch of the same item lands — the work it
        asked for got done another way, so health never leaves a session waiting to redo it."""
        items = self.card()
        rec = self.spawn('review', {'ok': True, 'pid': 80,
                                    'result': 'REPORT\nitem: T-0001\nkind: coder\nstatus: done\n'
                                              'pushed: yes abc123\n'})
        self.commit(rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        pool_mod.update_session(self.product, 'review', correction={
            'kind': 'operator', 'text': 'answer the C list', 'at': '2026-10-01T09:00:00Z'})
        pool_mod.update_session(self.product, 'coder-take2', item=self.ITEM,
                                branch='coder/take2', started='2026-10-02T09:00:00Z',
                                ended='2026-10-02T09:30:00Z', harvested='f00dface00')
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertIn(('review', 'released', f'{self.ITEM}: coder/take2 landed instead — the '
                                             'correction drops'), found)
        s = pool_mod.load_sessions(self.product)['review']
        self.assertNotIn('correction', s)

    def test_landing_on_its_own_branch_does_not_drop_its_own_correction(self):
        items = self.card()
        rec = self.spawn('review', {'ok': True, 'pid': 81,
                                    'result': 'REPORT\nitem: T-0001\nkind: coder\nstatus: done\n'
                                              'pushed: yes abc123\n'})
        self.commit(rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])
        pool_mod.update_session(self.product, 'review', correction={
            'kind': 'operator', 'text': 'answer the C list', 'at': '2026-10-01T09:00:00Z'},
            harvested='f00dface00', branch=rec['branch'])
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None,
                                  items=items)
        self.assertFalse([f for f in found if f[0] == 'review' and f[1] == 'released'], found)
        s = pool_mod.load_sessions(self.product)['review']
        self.assertIn('correction', s)


class TestStall(Home):
    def spawn(self, job, step):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b', runtime=rt,
                               cfg=self.cfg)

    def test_classification_and_ack(self):
        quiet = self.spawn('quiet', {'running': True, 'pid': 1})
        self.spawn('busy', {'running': True, 'pid': 2})
        self.spawn('dead', {'running': True, 'pid': 3})
        self.spawn('done', {'ok': True, 'pid': 4})
        now = time.time()
        os.utime(quiet['log'], (now - 31 * 60, now - 31 * 60))
        alive = lambda pid: pid in (1, 2, 4)
        found = stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None)
        self.assertEqual(sorted((j, st) for j, st, _ in found), [('dead', 'DEAD'), ('quiet', 'STALL')])
        with open(stall_mod.ack_path(self.product), 'w') as f:
            f.write('quiet STALL\ndead DEAD  # known\n')
        self.assertEqual(stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None), [])
        with open(stall_mod.ack_path(self.product), 'w') as f:
            f.write('quiet DEAD\n')
        found = stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None)
        self.assertIn('quiet', [j for j, _, _ in found])

    def test_silent_min_units(self):
        for v, want in ((30, 30), ('45m', 45), ('1h', 60), ('bogus', 30)):
            p = env.Product('x', {'stage_limits': {'silent_min': v}})
            self.assertEqual(stall_mod.silent_minutes(p), want)
        self.assertEqual(stall_mod.silent_minutes(env.Product('x', {})), 30)

    def _prime(self, job, now, same=True):
        base = {'commit': 'abc', 'digest': 'd1', 'classes': 1, 'novel': 1, 'files': 0,
                'seen': ['Bash']}
        older_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 20 * 60))
        newer_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
        progress.append(self.product, job, dict(base, at=older_at, last_call_at=older_at))
        newer = dict(base, at=newer_at, last_call_at=newer_at if same else older_at)
        if not same:
            newer['commit'] = 'zzz'
        progress.append(self.product, job, newer)

    def test_stuck_moving_and_cloud_rows(self):
        self.spawn('loop', {'running': True, 'pid': 101})
        self.spawn('busy2', {'running': True, 'pid': 102})
        self.spawn('cloud-run', {'running': True, 'pid': 'actions:1'})
        now = time.time()
        self._prime('loop', now, same=True)
        self._prime('busy2', now, same=False)
        self._prime('cloud-run', now, same=True)
        alive = lambda pid: pid in (101, 102, 'actions:1')
        found = stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None,
                                sample=False)
        self.assertEqual(sorted((j, st) for j, st, _ in found), [('loop', 'STUCK')])
        self.assertEqual([m for j, _, m in found if j == 'loop'][0], 20)
        sessions = pool_mod.load_sessions(self.product)
        self.assertEqual(sessions['loop']['progress'], progress.STUCK)
        self.assertEqual(sessions['busy2']['progress'], progress.MOVING)
        self.assertNotIn('progress', sessions['cloud-run'])

    def test_class_written_once_not_repeated_when_unchanged(self):
        self.spawn('loop', {'running': True, 'pid': 101})
        now = time.time()
        self._prime('loop', now, same=True)
        alive = lambda pid: pid == 101
        stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None, sample=False)
        with open(pool_mod.sessions_path(self.product)) as f:
            n1 = len(f.readlines())
        stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None, sample=False)
        with open(pool_mod.sessions_path(self.product)) as f:
            n2 = len(f.readlines())
        self.assertEqual(n1, n2)

    def test_input_row_for_a_needs_operator_line(self):
        self.spawn('needs-cred', {'ok': False, 'result': 'NEEDS OPERATOR: no credential'})
        found = stall_mod.stall(self.product, now=time.time(), alive=lambda pid: False,
                                out=lambda s: None, sample=False)
        self.assertIn(('needs-cred', 'INPUT', None), found)

    def test_input_row_for_a_blocked_report(self):
        text = 'REPORT\nitem: T-01\nkind: fix\nstatus: blocked\nleft out: needs a credential\n'
        self.spawn('blocked', {'ok': False, 'result': text})
        found = stall_mod.stall(self.product, now=time.time(), alive=lambda pid: False,
                                out=lambda s: None, sample=False)
        self.assertIn(('blocked', 'INPUT', None), found)

    def test_ack_silences_stuck_and_input_leaving_the_rest(self):
        self.spawn('loop', {'running': True, 'pid': 101})
        self.spawn('blocked', {'ok': False, 'result': 'NEEDS OPERATOR: need approval'})
        self.spawn('quiet2', {'running': True, 'pid': 103})
        now = time.time()
        self._prime('loop', now, same=True)
        log = pool_mod.load_sessions(self.product)['quiet2']['log']
        os.utime(log, (now - 31 * 60, now - 31 * 60))
        alive = lambda pid: pid in (101, 103)
        found = stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None,
                                sample=False)
        self.assertEqual(sorted((j, st) for j, st, _ in found),
                         [('blocked', 'INPUT'), ('loop', 'STUCK'), ('quiet2', 'STALL')])
        with open(stall_mod.ack_path(self.product), 'w') as f:
            f.write('loop STUCK\nblocked INPUT\n')
        found = stall_mod.stall(self.product, now=now, alive=alive, out=lambda s: None,
                                sample=False)
        self.assertEqual([(j, st) for j, st, _ in found], [('quiet2', 'STALL')])


class LivenessForTests(unittest.TestCase):
    """F-0234 §2: `observe.liveness_for`, and `identity_alive` re-expressed on top of it — both
    doors (P6) shut at one callable. The false death: a process is really there, but the
    observation could not read its session — no longer a corpse when it looks like one of the
    factory's own."""

    RUN = {'job': 'j', 'pid': 4242, 'session': 'p/j@t'}

    @staticmethod
    def _observed(pid, session=None):
        return observe.Observed(pid=pid, ppid=None, account=None, session=session,
                                 product=None, job=None, owner='foreign', cwd=None)

    def test_the_false_death_is_alive_and_unknown(self):
        obs = self._observed(4242, session=None)
        alive = observe.identity_alive([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: True)
        self.assertTrue(alive(4242))
        verdict = observe.liveness_for([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: True)
        self.assertEqual(verdict(4242), lifecycle.UNKNOWN)
        self.assertIsNone(stall_mod.classify({'pid': 4242}, time.time(), 30, alive))

    def test_the_reverse_row_something_not_ours_is_reused(self):
        # the same fixture, is_ours=False: REUSED, not ALIVE — a rule, not just an outcome
        obs = self._observed(4242, session=None)
        alive = observe.identity_alive([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: False)
        self.assertFalse(alive(4242))
        verdict = observe.liveness_for([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: False)
        self.assertEqual(verdict(4242), lifecycle.REUSED)

    def test_a_pid_no_run_recorded_is_alive_iff_observed(self):
        # unchanged rule (PD3): with no run at all, only the observation decides
        obs = self._observed(9999, session=None)
        self.assertTrue(observe.identity_alive([obs], [])(9999))
        self.assertFalse(observe.identity_alive([], [])(9999))

    def test_a_cloud_token_follows_cloudpid_alive(self):
        # unchanged rule (PD3): the cloud branch comes ahead of the recorded/unrecorded split
        tok = lifecycle.cloudpid.token('1')
        run = {'job': 'j', 'pid': tok}
        with mock.patch.object(lifecycle.cloudpid, 'alive', return_value=True):
            self.assertTrue(observe.identity_alive([], [run])(tok))
        with mock.patch.object(lifecycle.cloudpid, 'alive', return_value=False):
            self.assertFalse(observe.identity_alive([], [run])(tok))


class LivenessSilenceBoundTests(unittest.TestCase):
    """F-0234 §2 / S-35801: `UNKNOWN` defers only while the log moves — past
    `stall.silent_minutes` it is `GONE`, and the run is judged `dead pid` exactly as today, so
    the bound restores today's behaviour rather than inventing a third outcome."""

    RUN = {'job': 'j', 'pid': 4242, 'session': 'p/j@t'}

    @staticmethod
    def _observed(pid):
        return observe.Observed(pid=pid, ppid=None, account=None, session=None,
                                 product=None, job=None, owner='foreign', cwd=None)

    def test_within_the_bound_is_unknown_and_alive(self):
        obs = self._observed(4242)
        alive = observe.identity_alive([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: True, silent_min=30,
                                        silent_for=lambda p: 5.0)
        self.assertTrue(alive(4242))

    def test_past_the_bound_is_gone_and_judged_dead_pid(self):
        obs = self._observed(4242)
        alive = observe.identity_alive([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: True, silent_min=30,
                                        silent_for=lambda p: 31.0)
        self.assertFalse(alive(4242))
        ev = lifecycle.gather(None, self.RUN, alive=alive)
        self.assertEqual(lifecycle.judge(self.RUN, ev), lifecycle.DEAD_PID)

    def test_an_unmeasured_silence_stays_unknown(self):
        # PD4's guard: silent_for answering None (no log, or an unreadable mtime) is "not
        # measured", not "quiet forever"
        obs = self._observed(4242)
        alive = observe.identity_alive([obs], [self.RUN], exists=lambda p: True,
                                        is_ours=lambda p: True, silent_min=30,
                                        silent_for=lambda p: None)
        self.assertTrue(alive(4242))


class DeadCensusTests(Home):
    """F-0234 §3/§4: the ledger's own `dead pid` runs, classed and counted — `health.dead_census`
    and `doctor`'s `dead sessions` row, and the number this card exists to drive to zero."""

    def _dead(self, job, dead_class, ended='2026-01-05T00:00:00Z'):
        pool_mod.append_session(self.product, {'job': job, 'item': 'B-0001', 'branch': f'fix/{job}',
                                               'pid': 1, 'started': '2025-12-31T00:00:00Z'})
        fields = {'ended': ended, 'end_reason': lifecycle.DEAD_PID}
        if dead_class is not None:
            fields['dead_class'] = dead_class
        pool_mod.update_session(self.product, job, **fields)

    def test_the_census_classes_each_death_and_counts_what_has_no_class(self):
        self._dead('j1', lifecycle.GONE)
        self._dead('j2', lifecycle.REUSED)
        self._dead('j3', lifecycle.UNKNOWN)
        self._dead('j4', None)  # a row written before this card: a death with no dead_class
        now = to_dt('2026-01-10T00:00:00Z').timestamp()
        data = health_mod.dead_census(self.product, days=14, now=now)
        self.assertEqual(data['by_class'], {'gone': 1, 'reused': 1, 'unknown': 1})
        self.assertEqual(data['unclassified'], 1)
        self.assertEqual(data['runs'], 4)

    def test_the_line_reads_not_ok_while_unknown_stands_and_ok_once_it_does_not(self):
        ok, line = health_mod.dead_census_line({'days': 14, 'runs': 11, 'unclassified': 0,
                                                'by_class': {'gone': 6, 'reused': 1, 'unknown': 4}})
        self.assertFalse(ok)
        self.assertEqual(line, 'dead sessions: 11 in 14 days — gone 6, reused 1, unknown 4')
        ok, _ = health_mod.dead_census_line({'days': 14, 'runs': 7, 'unclassified': 0,
                                             'by_class': {'gone': 6, 'reused': 1}})
        self.assertTrue(ok)

    def test_every_row_still_counts_under_the_card_it_was_filed_under(self):
        # the card's own verification (C1, PD13): the class lives on a new field, `end_reason`
        # stays `dead pid`, and `score`/`diagnose` read it with the key the card was filed under
        # — a renamed class would score a false victory over a number nobody moved
        self._dead('j1', lifecycle.GONE)
        self._dead('j2', lifecycle.REUSED)
        self._dead('j3', lifecycle.UNKNOWN)
        self._dead('j4', None)
        sessions = pool_mod.load_sessions(self.product)
        runs = []
        for job in ('j1', 'j2', 'j3', 'j4'):
            s = sessions[job]
            self.assertEqual(s['end_reason'], lifecycle.DEAD_PID)
            self.assertEqual(score.failure_class(s['end_reason']), 'dead pid')
            runs.append(Run(job=job, kind='task', model='m', item='B-0001',
                            started='2025-12-31T00:00:00Z', ended=s['ended'], minutes=10.0,
                            landed=False, end_reason=s['end_reason'], usd=1.0))
        facts = Facts(items={}, sessions=[], ci=[], gates=[], runs=runs, clutter={},
                     as_of='2026-01-10T00:00:00Z')
        start, end = diagnose.window(facts.as_of, 14)
        self.assertEqual(diagnose.metric(facts, 'failure:dead pid', start, end), 2.0)


class HealthOneObserveCallTests(Home):
    """F-0234 PD5: `health.health`'s own `if alive is None:` branch reads the tick's one
    `ps axeww` once and derives both `alive` and `liveness` from it — a second, independent read
    here would double the single most expensive thing the health pass does."""

    class _CountingSource:
        def __init__(self):
            self.calls = 0

        def read(self):
            self.calls += 1
            return []

    def test_one_read_builds_both_callables(self):
        src = self._CountingSource()
        with mock.patch.object(health_mod, 'settle_ended', return_value=[]):
            health_mod.health(self.product, fix=False, session_source=src)
        self.assertEqual(src.calls, 1)


class TestCorrectOnce(Home):
    def test_retry_with_the_error_then_give_up(self):
        rec = spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'original brief\n',
                              runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        rt = runtime_mod.FakeRuntime(path=os.path.join(FIXTURES, 'fake-results.json'))
        session = pool_mod.load_sessions(self.product)['j']
        # True = a correction is now running (B-0085); its verdict belongs to the next tick
        self.assertTrue(stall_mod.correct_once(self.product, session, 'test_x failed', rt))
        _job, brief = rt.calls[0]
        self.assertEqual(brief, 'original brief\n\n\nCORRECTION: the step failed with:\ntest_x failed\n')
        self.assertEqual(pool_mod.load_sessions(self.product)['j']['corrected'], 1)
        # already corrected once → no second retry, the caller files the Bug
        self.assertFalse(stall_mod.correct_once(self.product, session, 'again', rt))
        self.assertEqual(len(rt.calls), 1)
        self.assertEqual(rec['job'], 'j')

    def test_retry_that_passes(self):
        rec = spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b\n',
                              runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        with open(os.path.join(rec['worktree'], 'x'), 'w') as f:
            f.write('x')
        git('add', 'x', cwd=rec['worktree'])
        git('commit', '-q', '-m', 'the retry fixed it', cwd=rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])  # the retry pushed
        session = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(stall_mod.correct_once(self.product, session, 'boom',
                                               runtime_mod.FakeRuntime([{'ok': True}])))

    def test_b0085_the_correction_is_launched_and_left_for_the_next_tick_to_judge(self):
        # was: `f0087_a_retry_that_says_ok_without_pushing_is_not_finished`. The retry is still
        # judged by its result AND its push (B-0051) — by health, on the next tick, exactly as
        # every other run is. Judging it here meant waiting for it here, and that stopped the
        # whole tick for as long as a model session takes (B-0085).
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b\n',
                        runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        session = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(stall_mod.correct_once(self.product, session, 'boom',
                                               runtime_mod.FakeRuntime([{'ok': True}])))
        retry = pool_mod.load_sessions(self.product)['j-correction']
        self.assertIsNone(retry.get('ended'), 'the correction is running, not ended')
        self.assertIsNone(retry.get('end_reason'))
        self.assertTrue(retry.get('pid'), 'it is in the registry with its pid, so it can be seen')
        self.assertTrue(retry.get('started'))

    def test_b0039_the_retry_relaunches_cold_its_own_job_and_log(self):
        """D-0048 part b: a correction is a fresh session, not the dead one resumed — its own
        job and log, its own ledger line; the dead run's record is never rewritten to look like
        the one that passed."""
        rec = spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b\n',
                              runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        with open(os.path.join(rec['worktree'], 'x'), 'w') as f:
            f.write('x')
        git('add', 'x', cwd=rec['worktree'])
        git('commit', '-q', '-m', 'the retry fixed it', cwd=rec['worktree'])
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])  # the retry pushed
        session = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(stall_mod.correct_once(self.product, session, 'boom',
                                               runtime_mod.FakeRuntime([{'ok': True}])))
        sessions = pool_mod.load_sessions(self.product)
        new_jobs = [j for j in sessions if j != 'j']
        self.assertEqual(len(new_jobs), 1, sessions)
        retry = sessions[new_jobs[0]]
        self.assertNotEqual(retry['log'], session['log'])
        self.assertEqual(retry['job'], 'j-correction')
        self.assertIsNone(retry.get('end_reason'), 'the next tick judges it (B-0085)')
        self.assertEqual(retry['branch'], rec['branch'])
        self.assertEqual(retry['worktree'], rec['worktree'])
        self.assertNotIn('end_reason', sessions['j'])


class TestCli(unittest.TestCase):
    def test_register_adds_the_seven_verbs(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        register(sub)
        for verb in ('spawn --row x --brief y', 'wave -n 2', 'health --fix', 'stall', 'quota',
                     'reserve-id --job j', 'progress --job j'):
            args = p.parse_args(['workers', *verb.split(), '--product', 'sample'])
            self.assertEqual(args.product, 'sample')
            self.assertTrue(callable(args.func))


class TestQuotaCli(Home):
    def run_quota(self, usage):
        # 'sessions': 'fake' for the same reason Home.cfg carries it (D6): this cfg replaces
        # Home's through load_cfg, and acct-a names no config_dir, so the real process table
        # would put the operator's own sessions in the Load column.
        cfg = {'worker_pool': {'accounts': [{'name': 'acct-a', 'role': 'local', 'cap': 3}],
                               'quota_command': 'true {account}', 'sessions': 'fake'}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch('asf.workers._product', return_value=self.product), \
             mock.patch('asf.workers.spawn.load_cfg', return_value=cfg), \
             mock.patch.object(quota_mod.CommandQuotaSource, 'read', lambda self, a: usage):
            rc = cmd_quota(argparse.Namespace(product='sample'))
        return rc, buf.getvalue()

    def test_the_table_names_the_band_and_the_window_that_decided_it(self):
        rc, text = self.run_quota({'five_h_pct': 10, 'seven_d_pct': 91})
        self.assertEqual(rc, 0)
        self.assertIn('| Band |', text.splitlines()[0])
        self.assertIn('| acct-a | local | 0 / 3 | 10 | 91 | cooldown — seven_d_pct 91 ≥ 90 |', text)
        self.assertIn('| stop — seven_d_pct 96 ≥ 95 |',
                      self.run_quota({'five_h_pct': 10, 'seven_d_pct': 96})[1])
        self.assertIn('| free |', self.run_quota({'five_h_pct': 10, 'seven_d_pct': 10})[1])
        self.assertIn('| ? | ? | stop — quota unreadable |', self.run_quota(None)[1])


class TestReserveIdCli(Home):
    def test_b0007_reserve_id_prints_the_range_a_launcher_can_export(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        register(sub)
        args = p.parse_args(['workers', 'reserve-id', '--product', 'sample', '--job', 'fix-b-0007'])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch('asf.workers._product', return_value=self.product), \
             mock.patch('asf.workers.spawn.load_cfg', return_value=self.cfg):
            rc = args.func(args)
        self.assertEqual(rc, 0)
        printed = buf.getvalue().strip()
        self.assertEqual(printed, 'S:5000-5049,T:5000-5049,B:5000-5049')
        # sticky: a second reservation for the same job returns the same range, unchanged
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2), \
             mock.patch('asf.workers._product', return_value=self.product), \
             mock.patch('asf.workers.spawn.load_cfg', return_value=self.cfg):
            args.func(args)
        self.assertEqual(buf2.getvalue().strip(), printed)


class TestProgressCli(Home):
    def spawn(self, job, step):
        rt = runtime_mod.FakeRuntime([step])
        return spawn_mod.spawn(self.product, feature_row(job), self.acct(), 'b', runtime=rt,
                               cfg=self.cfg)

    def _prime(self, job, now, same=True):
        base = {'commit': 'abc', 'digest': 'd1', 'classes': 1, 'novel': 1, 'files': 0,
               'seen': ['Bash']}
        older_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 20 * 60))
        newer_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
        progress.append(self.product, job, dict(base, at=older_at, last_call_at=older_at))
        newer = dict(base, at=newer_at, last_call_at=newer_at if same else older_at)
        if not same:
            newer['commit'] = 'zzz'
        progress.append(self.product, job, newer)

    def run_progress(self, argv):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        register(sub)
        args = p.parse_args(['workers', 'progress', *argv, '--product', 'sample'])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch('asf.workers._product', return_value=self.product):
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_product_alone_renders_the_table_and_exits_1_when_stuck(self):
        self.spawn('loop', {'running': True, 'pid': 101})
        self.spawn('busy', {'running': True, 'pid': 102})
        now = time.time()
        self._prime('loop', now, same=True)
        self._prime('busy', now, same=False)
        with mock.patch('time.time', return_value=now):
            rc, out = self.run_progress([])
        self.assertIn('| Job | Class | For | Commit | Files | Classes (10m) | Last call |', out)
        self.assertIn('| loop | stuck |', out)
        self.assertIn('| busy | moving |', out)
        self.assertEqual(rc, 1)

    def test_product_alone_exits_0_without_a_stuck_run(self):
        self.spawn('busy', {'running': True, 'pid': 102})
        now = time.time()
        self._prime('busy', now, same=False)
        with mock.patch('time.time', return_value=now):
            rc, out = self.run_progress([])
        self.assertEqual(rc, 0)

    def test_job_prints_the_newest_sample_and_its_verdict(self):
        self.spawn('loop', {'running': True, 'pid': 101})
        now = time.time()
        self._prime('loop', now, same=True)
        with mock.patch('time.time', return_value=now):
            rc, out = self.run_progress(['--job', 'loop'])
        self.assertEqual(rc, 0)
        self.assertIn('loop', out)
        self.assertIn('stuck', out)

    def test_job_watch_calls_watch(self):
        self.spawn('loop', {'running': True, 'pid': 101})
        with mock.patch('asf.progress.watch') as watch:
            rc, out = self.run_progress(['--job', 'loop', '--watch'])
        self.assertEqual(rc, 0)
        watch.assert_called_once_with(self.product, 'loop')


if __name__ == '__main__':
    unittest.main()


class FactoryCliInSessionHome(unittest.TestCase):
    """A session under an isolated HOME finds the factory's CLI at $HOME/.local/bin/asf, where a
    product's own git hooks call it."""

    def test_isolated_home_links_the_operators_asf(self):
        import tempfile
        from asf.workers import runtime as rt
        with tempfile.TemporaryDirectory() as d:
            op = os.path.join(d, 'op')
            cli = os.path.join(op, '.local', 'bin', 'asf')
            os.makedirs(os.path.dirname(cli))
            with open(cli, 'w') as f:
                f.write('#!/bin/sh\n')
            home = os.path.join(d, 'homes', 'a')
            os.makedirs(home)
            link = rt.link_factory_cli(home, op)
            self.assertEqual(os.path.realpath(link), os.path.realpath(cli))
            self.assertEqual(link, os.path.join(home, '.local', 'bin', 'asf'))
            # idempotent, and a stale link is replaced
            os.remove(link)
            os.symlink('/nowhere', link)
            rt.link_factory_cli(home, op)
            self.assertEqual(os.path.realpath(link), os.path.realpath(cli))
