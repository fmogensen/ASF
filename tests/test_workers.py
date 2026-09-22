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
import time
import unittest
from unittest import mock

from asf import env
from asf.workers import health as health_mod
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stall as stall_mod
from asf.workers import wave as wave_mod
from asf.workers import register
from asf.tick.step_wave import corrections as step_wave_corrections

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


class Home(unittest.TestCase):
    """A temp ASF_HOME with a product whose repo is a clone of a bare origin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'asf-home')
        origin = os.path.join(self.tmp, 'origin.git')
        seed = os.path.join(self.tmp, 'seed')
        git('init', '-q', '--bare', '-b', 'main', origin, cwd=self.tmp)
        git('init', '-q', '-b', 'main', seed, cwd=self.tmp)
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=seed)
        with open(os.path.join(seed, 'README'), 'w') as f:
            f.write('seed\n')
        git('add', '.', cwd=seed)
        git('commit', '-q', '-m', 'seed', cwd=seed)
        git('push', '-q', origin, 'main', cwd=seed)
        self.repo = os.path.join(self.tmp, 'repo')
        git('clone', '-q', origin, self.repo, cwd=self.tmp)
        self.grant = os.path.join(self.tmp, 'grant')
        os.makedirs(self.grant)
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                              'job_grants': [self.grant],
                                              'stage_limits': {'silent_min': 30}})
        self.cfg = {'worker_pool': {'accounts': [{'name': 'acct-a', 'role': 'local', 'cap': 2}],
                                    'models': {'Opus': 'opus'}}}

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def acct(self):
        return pool_mod.Account('acct-a', cap=2, config_dir='/cfg/acct-a')


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
        self.assertEqual(e, {'PATH': '/bin', 'HOME': '/homes/a', 'CLAUDE_CONFIG_DIR': '/cfg/a',
                             'ASF_PRODUCT': 'sample', 'ASF_JOB': 'j1',
                             'BACKLOG_ID_RANGE': 'S:5000-5049'})

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

    def test_fake_runtime_replays_a_fixture(self):
        rt = runtime_mod.FakeRuntime(path=os.path.join(FIXTURES, 'fake-results.json'))
        self.assertEqual([s['ok'] for s in rt.script], [False, True])


class TestQuota(unittest.TestCase):
    def test_default_guards_and_config(self):
        self.assertEqual(quota_mod.guards_from_config({}),
                         {'five_h': 92, 'seven_d': 85, 'seven_d_model': 90})
        g = quota_mod.guards_from_config({'quota_guards': {'five_h': 80}})
        self.assertEqual(g['five_h'], 80)
        old = quota_mod.guards_from_config({'worker_pool': {'quota_guard': {'max_7d': 0.5}}})
        self.assertEqual(old['seven_d'], 50)

    def test_under_guard(self):
        g = quota_mod.guards_from_config({})
        self.assertEqual(quota_mod.under_guard({'five_h_pct': 91, 'seven_d_pct': 84}, g), (True, ''))
        self.assertFalse(quota_mod.under_guard({'five_h_pct': 92, 'seven_d_pct': 0}, g)[0])
        self.assertFalse(quota_mod.under_guard({'five_h_pct': 0, 'seven_d_pct': 85}, g)[0])
        self.assertFalse(quota_mod.under_guard({'seven_d_model_pct': 90}, g)[0])
        self.assertEqual(quota_mod.under_guard(None, g), (False, 'quota unreadable'))

    def test_command_source_parses_one_json_line(self):
        cmd = (f'{sys.executable} -c "import json,sys; print(\'noise\'); '
               f'print(json.dumps(dict(five_h_pct=len(sys.argv[1]), seven_d_pct=3)))" {{account}}')
        self.assertEqual(quota_mod.CommandQuotaSource(cmd).read(pool_mod.Account('abcd')),
                         {'five_h_pct': 4, 'seven_d_pct': 3})
        self.assertIsNone(quota_mod.CommandQuotaSource(f'{sys.executable} -c "print(1/0)"')
                          .read(pool_mod.Account('a')))


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
        self.assertEqual(p.pick_account('spec', 'Opus'), (None, pool_mod.REASON_FULL))
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

    def test_reserve_rule(self):
        a = pool_mod.Account('a', role='local', cap=3)
        p = self.pool([a], live=[{'account': 'a'}, {'account': 'a'}])
        # one free slot, an S1 open → a Feature row waits, the S1 fix gets it
        self.assertEqual(p.pick_account('spec', 'opus', s1_is_open=True),
                         (None, 'reserved for S1'))
        self.assertEqual(p.pick_account('fix-bug', 'opus', is_fix=True, s1_is_open=True)[0].name, 'a')
        # no S1 open → the slot is anyone's
        self.assertEqual(p.pick_account('spec', 'opus')[0].name, 'a')

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

    def test_b0046_correct_row_spawns_on_the_held_branch_rebased_onto_main(self):
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
        self.assertTrue(os.path.exists(os.path.join(wt, 'b.txt')))
        git('merge-base', '--is-ancestor', 'origin/main', 'HEAD', cwd=wt)

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
        self.assertTrue(os.path.exists(os.path.join(wt, 'b.txt')))  # rebased onto main
        git('merge-base', '--is-ancestor', 'origin/main', 'HEAD', cwd=wt)

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
        rt = runtime_mod.FakeRuntime([{'running': True}])
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)

    def test_b0051_ended_sessions_worktree_is_reused_not_refused(self):
        # a session that ended 'finished' without pushing (B-0051) must not permanently block
        # its item: the next spawn for the same job reuses the worktree as it stands — branch
        # and tree — rebased onto the trunk, instead of refusing it forever. The refusal stays
        # only for a worktree whose session is still live (B-0025).
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
        self.assertNotEqual(s['end_reason'], 'finished')
        # the trunk moves on while the worktree sits there, unpushed
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
        self.assertTrue(os.path.exists(os.path.join(wt, 'trunk.txt')))
        git('merge-base', '--is-ancestor', 'origin/main', 'HEAD', cwd=wt)

    def test_b0025_live_sessions_worktree_still_refuses(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 50}])
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
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('fix-bug-b-0001', 'ended',
                       'failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)'), found)
        held = [d for j, w, d in found if w == 'held']
        self.assertEqual(held, ['unpushed work: not pushed: 1 uncommitted file(s), 0 unpushed '
                                'commit(s) — commit and push what you have, or say why not in the '
                                'report — back to its session (round 1)'])
        self.assertTrue(os.path.isdir(wt))  # kept: there is work in it
        corr = step_wave_corrections(self.product)
        self.assertEqual(corr['B-0001']['kind'], 'unpushed')
        self.assertEqual(corr['B-0001']['rounds'], 1)
        row = pool_mod.parse_row(json.dumps({'job': 'correct-b-0001', 'item': 'B-0001', 'state': 'FIX',
                                             'action': 'CORRECT', 'model': 'Opus', 'kind': 'correct',
                                             'branch': rec['branch']}))
        rec2 = spawn_mod.spawn(self.product, row, self.acct(), 'b',
                               runtime=runtime_mod.FakeRuntime([{'running': True, 'pid': 42}]), cfg=self.cfg)
        self.assertEqual(os.path.realpath(rec2['worktree']), os.path.realpath(wt))
        self.assertTrue(os.path.exists(os.path.join(wt, 'work.txt')))
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt), rec['branch'])
        self.assertEqual(step_wave_corrections(self.product), {})  # answered
        found = health_mod.health(self.product, fix=True, alive=lambda pid: pid == 42, out=lambda s: None)
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse([f for f in found if f[1] in ('reaped', 'reapable')], found)
        # a third session on the same branch while the correction is live: refused
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.spawn(self.product, s1_row('fix-bug-b-0001'), self.acct(), 'b',
                            runtime=runtime_mod.FakeRuntime([{'ok': True}]), cfg=self.cfg)

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


class TestWave(Home):
    def run_wave(self, rows, n, accounts, live=()):
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource(), live=live)
        lines = []
        rt = runtime_mod.FakeRuntime([{'running': True}] * 10)
        launched, waits = wave_mod.wave(self.product, rows, n, pool=pool, runtime=rt,
                                        cfg=self.cfg, out=lines.append)
        return launched, waits, lines

    def test_pool_full_of_features_an_s1_arrives_and_launches(self):
        acct = pool_mod.Account('acct-a', role='local', cap=3)
        live = [{'job': f'spec-{i}', 'account': 'acct-a'} for i in range(2)]
        launched, waits, lines = self.run_wave([feature_row('spec-9'), s1_row()], 5, [acct], live)
        self.assertEqual([r.job for r, _ in launched], ['fix-b-0001'])
        self.assertEqual([(r.job, why) for r, why in waits], [('spec-9', 'pool full')])
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

    def test_b0028_dead_pid_is_rejudged_when_the_result_arrives(self):
        rec = self.spawn('late', {'running': True, 'pid': 12})
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

    def test_local_commit_not_pushed_is_kept(self):
        # B-0051: the result says ok, but a commit made after the last push never went out —
        # that must not be read as 'finished' (or the item is blocked forever: health says
        # finished, harvest sees nothing to land). It ends 'failed: not pushed: ...' instead,
        # and the worktree with the unpushed work is kept, not reaped.
        rec = self.spawn('done', {'ok': True})
        wt = rec['worktree']
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=wt)
        with open(os.path.join(wt, 'x'), 'w') as f:
            f.write('x')
        git('add', 'x', cwd=wt)
        git('commit', '-q', '-m', 'x', cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        end_reason = pool_mod.load_sessions(self.product)['done']['end_reason']
        self.assertEqual(end_reason, 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)')
        self.assertIn(('done', 'keep', f'ended: session {end_reason}, not finished'), found)
        self.assertTrue(os.path.isdir(wt))

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
        rec = self.spawn('empty', {'ok': True, 'pid': 22})
        wt = rec['worktree']
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('empty', 'keep', 'ended: no commits yet'), found)
        self.assertTrue(os.path.isdir(wt))

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


class TestCorrectOnce(Home):
    def test_retry_with_the_error_then_give_up(self):
        rec = spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'original brief\n',
                              runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        rt = runtime_mod.FakeRuntime(path=os.path.join(FIXTURES, 'fake-results.json'))
        session = pool_mod.load_sessions(self.product)['j']
        self.assertFalse(stall_mod.correct_once(self.product, session, 'test_x failed', rt))
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
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])  # the retry pushed
        session = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(stall_mod.correct_once(self.product, session, 'boom',
                                               runtime_mod.FakeRuntime([{'ok': True}])))

    def test_f0087_a_retry_that_says_ok_without_pushing_is_not_finished(self):
        # the cold retry is judged like every run: by its result AND its push (B-0051)
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b\n',
                        runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        session = pool_mod.load_sessions(self.product)['j']
        self.assertFalse(stall_mod.correct_once(self.product, session, 'boom',
                                                runtime_mod.FakeRuntime([{'ok': True}])))
        retry = pool_mod.load_sessions(self.product)['j-correction']
        self.assertEqual(retry['end_reason'],
                         'failed: not pushed: 0 uncommitted file(s), 0 unpushed commit(s)')

    def test_b0039_the_retry_relaunches_cold_its_own_job_and_log(self):
        """D-0048 part b: a correction is a fresh session, not the dead one resumed — its own
        job and log, its own ledger line; the dead run's record is never rewritten to look like
        the one that passed."""
        rec = spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b\n',
                              runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        git('push', '-q', 'origin', rec['branch'], cwd=rec['worktree'])  # the retry pushed
        session = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(stall_mod.correct_once(self.product, session, 'boom',
                                               runtime_mod.FakeRuntime([{'ok': True}])))
        sessions = pool_mod.load_sessions(self.product)
        new_jobs = [j for j in sessions if j != 'j']
        self.assertEqual(len(new_jobs), 1, sessions)
        retry = sessions[new_jobs[0]]
        self.assertNotEqual(retry['log'], session['log'])
        self.assertEqual(retry['end_reason'], 'finished')
        self.assertEqual(retry['rc'], 0)
        self.assertEqual(retry['branch'], rec['branch'])
        self.assertEqual(retry['worktree'], rec['worktree'])
        self.assertNotIn('end_reason', sessions['j'])


class TestCli(unittest.TestCase):
    def test_register_adds_the_six_verbs(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        register(sub)
        for verb in ('spawn --row x --brief y', 'wave -n 2', 'health --fix', 'stall', 'quota',
                     'reserve-id --job j'):
            args = p.parse_args(['workers', *verb.split(), '--product', 'sample'])
            self.assertEqual(args.product, 'sample')
            self.assertTrue(callable(args.func))


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


if __name__ == '__main__':
    unittest.main()
