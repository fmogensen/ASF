"""asf.workers — runtime command line, pick rule, S1 reserve, spawn, wave, health, stall,
same-session correction. Every run goes through the fake runtime; git is a bare repo in a temp
dir; no network, no account, no real product."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from asf import env
from asf.workers import health as health_mod
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stall as stall_mod
from asf.workers import wave as wave_mod
from asf.workers import register

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
        self.cfg = {'worker_pool': {'accounts': [{'name': 'acct-a', 'role': 'local', 'cap': 2}]}}

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

    def test_id_ranges_do_not_overlap_and_are_sticky(self):
        r1 = spawn_mod.reserve_id_range(self.product, 'j1', prefixes=['T'])
        r2 = spawn_mod.reserve_id_range(self.product, 'j2', prefixes=['T'])
        self.assertEqual((r1, r2), ('T:5000-5049', 'T:5050-5099'))
        self.assertEqual(spawn_mod.reserve_id_range(self.product, 'j1', prefixes=['T']), r1)

    def test_existing_worktree_refuses(self):
        rt = runtime_mod.FakeRuntime([{'running': True}])
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)
        with self.assertRaises(spawn_mod.SpawnError):
            spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)


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

    def test_transitions(self):
        self.spawn('done', {'ok': True, 'pid': 11})
        self.spawn('gone', {'running': True, 'pid': 12})
        self.spawn('live', {'running': True, 'pid': 13})
        found = health_mod.health(self.product, alive=lambda pid: pid == 13, out=lambda s: None)
        ended = {j: d for j, w, d in found if w == 'ended'}
        self.assertEqual(ended, {'done': 'finished', 'gone': 'dead pid'})
        s = pool_mod.load_sessions(self.product)
        self.assertEqual(s['done']['end_reason'], 'finished')
        self.assertFalse(s['live'].get('ended'))
        keep = {j: d for j, w, d in found if w == 'keep'}
        self.assertEqual(keep['done'], 'ended: branch not pushed')
        self.assertEqual(keep['gone'], 'ended: session dead pid, not finished')

    def test_reap_only_when_pushed(self):
        rec = self.spawn('done', {'ok': True, 'pid': 11})
        wt = rec['worktree']
        git('push', '-q', 'origin', rec['branch'], cwd=wt)
        found = health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'reapable', 'ended'), found)
        self.assertTrue(os.path.isdir(wt))
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('done', 'reaped', 'ended'), found)
        self.assertFalse(os.path.exists(wt))

    def test_local_commit_not_pushed_is_kept(self):
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
        self.assertIn(('done', 'keep', 'ended: local commits not pushed'), found)
        self.assertTrue(os.path.isdir(wt))

    def test_orphan_worktree(self):
        path = os.path.join(spawn_mod.worktrees_dir(self.product), 'stray')
        git('worktree', 'add', '-q', '-b', 'stray', path, 'origin/main', cwd=self.repo)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('stray', 'keep', 'orphan: branch not pushed'), found)
        git('push', '-q', 'origin', 'stray', cwd=path)
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        self.assertIn(('stray', 'reaped', 'orphan'), found)


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
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b\n',
                        runtime=runtime_mod.FakeRuntime([{'ok': False}]), cfg=self.cfg)
        session = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(stall_mod.correct_once(self.product, session, 'boom',
                                               runtime_mod.FakeRuntime([{'ok': True}])))


class TestCli(unittest.TestCase):
    def test_register_adds_the_five_verbs(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        register(sub)
        for verb in ('spawn --row x --brief y', 'wave -n 2', 'health --fix', 'stall', 'quota'):
            args = p.parse_args(['workers', *verb.split(), '--product', 'sample'])
            self.assertEqual(args.product, 'sample')
            self.assertTrue(callable(args.func))


if __name__ == '__main__':
    unittest.main()
