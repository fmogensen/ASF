"""asf.workers.heartbeat — every run beats; a stalled one is ended and continued in one tick.

The config (``workers.heartbeat_*``, the ``cloud:`` lane over it), the rule (interval x missed
beats, the grace for the first beat), the movement sources (branch or beat ref), the beat loop
itself against a real origin (and its fence), the hook shim passing a beat untouched, the brief,
and the whole stall path on the claude-remote lane (one ``list_runs``, the routine disabled, the
snapshot on the branch, the continuation's brief, the zombie push) and on the local lane of a
minimal product. Nothing reaches the network: origin is a bare repo, the helper is a fake."""
import json
import os
import subprocess
import time
import unittest

from asf import env
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import githooks
from asf.workers import heartbeat as hb
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_heartbeat` does not
    import test_one_push_heavy_ci as onepush
    import test_remote
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests import test_one_push_heavy_ci as onepush
    from tests import test_remote
    from tests.test_workers import Home, feature_row, git

remote_job = test_remote.job

M = 60.0


class Config(unittest.TestCase):
    def test_defaults(self):
        s = hb.settings({})
        self.assertEqual((s.interval_min, s.missed, s.grace_min, s.resumes), (5, 2, 10, 2))
        self.assertEqual(s.limit_min, 10)

    def test_explicit_values_and_the_cloud_lane_over_workers(self):
        cfg = {'workers': {'heartbeat_min': 3, 'heartbeat_missed': 4},
               'cloud': {'heartbeat_min': 7}}
        local = hb.settings(cfg)
        self.assertEqual((local.interval_min, local.missed), (3, 4))
        lane = hb.settings(cfg, lane='cloud')
        self.assertEqual((lane.interval_min, lane.missed), (7, 4))

    def test_a_product_file_overrides_the_operator(self):
        product = env.Product('p', {'repo_dir': '/r', 'main': 'main',
                                    'workers': {'heartbeat_missed': 3}})
        self.assertEqual(hb.settings({'workers': {'heartbeat_missed': 5}}, product).missed, 3)

    def test_bad_values_are_config_problems(self):
        for bad in (0, -1, 'x', True):
            for key in ('heartbeat_min', 'heartbeat_missed', 'heartbeat_grace_min'):
                probs = hb.config_problems({key: bad})
                self.assertEqual([k for k, _ in probs], [f'workers.{key}'], (key, bad))
                self.assertEqual([k for k, _ in cloud.config_problems({key: bad})],
                                 [f'cloud.{key}'])
        self.assertEqual(hb.config_problems({'heartbeat_missed': 0.5})[0][0],
                         'workers.heartbeat_missed')
        self.assertEqual(hb.config_problems({'heartbeat_resumes': -1})[0][0],
                         'workers.heartbeat_resumes')
        self.assertEqual(hb.config_problems({'heartbeat_resumes': 0}), [])
        self.assertEqual(hb.config_problems({'heartbeat_min': 3, 'heartbeat_missed': 1}), [])
        # a bad key reads as its default and never discards a good one
        s = hb.settings({'workers': {'heartbeat_min': 0, 'heartbeat_missed': 3}})
        self.assertEqual((s.interval_min, s.missed), (5, 3))

    def test_the_keys_are_registered_and_documented(self):
        from asf import config_keys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'docs', 'config.example.yaml'), encoding='utf-8') as f:
            doc = f.read()
        for key in hb.KEYS:
            self.assertIn(f'workers.{key}', config_keys.KNOWN_CONFIG_KEYS)
            self.assertIn(f'  {key}:', doc)

    def test_only_a_run_launched_with_the_heartbeat_is_judged(self):
        self.assertIsNone(hb.for_run({'job': 'j'}, {}))
        s = hb.for_run({'job': 'j', 'heartbeat_min': 3, 'runtime_lane': 'cloud'},
                       {'cloud': {'heartbeat_missed': 4}})
        self.assertEqual((s.interval_min, s.missed), (3, 4))


class Rule(unittest.TestCase):
    """judge(started, moved, now, s): the interval times the missed beats, after the grace."""
    T = 1_000_000.0

    def stalled(self, quiet_min, **kw):
        s = hb.Settings(**kw)
        started = self.T - 3600  # long past the grace
        return hb.judge(started, self.T - quiet_min * M, self.T, s)[0]

    def test_interval(self):
        self.assertFalse(self.stalled(9))
        self.assertTrue(self.stalled(11))
        self.assertTrue(self.stalled(7, interval_min=3, missed=2))
        self.assertFalse(self.stalled(19, interval_min=5, missed=4))
        self.assertTrue(self.stalled(21, interval_min=5, missed=4))

    def test_missed_beat_count(self):
        self.assertTrue(self.stalled(6, interval_min=5, missed=1))
        self.assertFalse(self.stalled(14, interval_min=5, missed=3))

    def test_the_grace_for_the_first_beat(self):
        s = hb.Settings()
        self.assertFalse(hb.judge(self.T - 9 * M, None, self.T, s)[0])   # inside the grace
        self.assertFalse(hb.judge(self.T - 19 * M, None, self.T, s)[0])  # grace + 9m
        self.assertTrue(hb.judge(self.T - 21 * M, None, self.T, s)[0])   # grace + 11m


class Movement(Home):
    """observe(): a change of either ref, seen by the pass, resets the clock."""

    def run_rec(self, started):
        return {'job': 'j', 'session': 'sample/j@1', 'branch': 'task/j',
                'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started)),
                'heartbeat_min': 5}

    def look(self, run, refs, now):
        beats = hb.Beats(self.product, runs=[run], ls_remote=lambda want: dict(refs))
        return hb.observe(self.product, run, beats, now, hb.Settings(), out=lambda _l: None)

    def test_either_ref_alone_resets_the_clock(self):
        t0 = 2_000_000.0
        run = self.run_rec(t0)
        refs = {'refs/heads/task/j': 'a' * 40}
        self.assertFalse(self.look(run, refs, t0 + 60)[0])
        self.assertTrue(self.look(run, refs, t0 + 21 * M)[0])        # no beat: stalled
        refs['refs/heads/task/j'] = 'b' * 40                         # the branch alone moved
        self.assertFalse(self.look(run, refs, t0 + 22 * M)[0])
        self.assertFalse(self.look(run, refs, t0 + 31 * M)[0])       # 9m since
        self.assertTrue(self.look(run, refs, t0 + 33 * M)[0])        # 11m since
        refs['refs/asf/hb/j'] = 'c' * 40                             # the beat ref alone moved
        self.assertFalse(self.look(run, refs, t0 + 34 * M)[0])
        self.assertTrue(self.look(run, refs, t0 + 45 * M)[0])

    def test_an_unreadable_origin_judges_nothing(self):
        run = self.run_rec(2_000_000.0)
        beats = hb.Beats(self.product, runs=[run], ls_remote=lambda want: None)
        stalled, quiet, why = hb.observe(self.product, run, beats, 2_000_000.0 + 3600,
                                         hb.Settings(), out=lambda _l: None)
        self.assertEqual((stalled, quiet), (False, None))
        self.assertIn('unreadable', why)

    def test_one_ls_remote_for_the_pass(self):
        runs = [dict(self.run_rec(2_000_000.0), job=f'j{i}', branch=f'task/j{i}') for i in range(3)]
        asked = []
        beats = hb.Beats(self.product, runs=runs, ls_remote=lambda want: asked.append(want) or {})
        for r in runs:
            hb.observe(self.product, r, beats, 2_000_000.0 + 60, hb.Settings(), out=lambda _l: None)
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(asked[0]), 6)


class Brief(unittest.TestCase):
    def test_the_cloud_brief_carries_the_heartbeat_rule(self):
        j = remote_job(name='task-t-0001')
        j.heartbeat = hb.Settings(interval_min=3, missed=2)
        text = cloud.cloud_brief('the brief', j)
        self.assertIn('HEARTBEAT', text)
        self.assertIn('- Every 3 minutes this session proves it is moving: a background loop '
                      'pushes your work in progress and `NOTES.asf.md` to '
                      '`refs/asf/hb/task-t-0001` on origin. A run with no beat for 6 minutes is '
                      'ended and continued by a new session from your branch and your notes.',
                      text)
        self.assertIn("nohup sh \"$(git rev-parse --git-dir)/asf-heartbeat.sh\" 180 "
                      "refs/asf/hb/task-t-0001 'sid-1' >/dev/null 2>&1 &", text)
        self.assertLess(text.index('HEARTBEAT'), len(text))
        self.assertNotIn('HEARTBEAT', cloud.cloud_brief('the brief', remote_job()))


class BeatLoop(unittest.TestCase):
    """The loop the brief starts, run for real in a session's worktree under the hook shim."""
    setUp = onepush.PrePushLog.setUp
    _product_hook = onepush.PrePushLog._product_hook
    _commit_and_push = onepush.PrePushLog._commit_and_push
    _logged = onepush.PrePushLog._logged

    def start(self):
        script = os.path.join(self.tmp, 'hb.sh')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(hb.SCRIPT)
        with open(os.path.join(self.wt, 'NOTES.asf.md'), 'w', encoding='utf-8') as f:
            f.write('done: the parser\nnext: the tests\n')
        with open(os.path.join(self.wt, 'wip.txt'), 'w', encoding='utf-8') as f:
            f.write('half a change\n')
        e = dict(self.env)
        e.pop('ASF_ONE_PUSH')
        p = subprocess.Popen(['sh', script, '1', 'refs/asf/hb/j', 'sample/j@1'], cwd=self.wt,
                             env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p

    def wait_ref(self, want=True, limit=15):
        end = time.time() + limit
        while time.time() < end:
            out = subprocess.run(['git', 'for-each-ref', '--format=%(objectname)', 'refs/asf/hb/j'],
                                 cwd=self.remote, capture_output=True, text=True).stdout.strip()
            if bool(out) == want:
                return out
            time.sleep(0.2)
        self.fail('the beat ref never appeared' if want else 'the beat ref never went')

    def test_a_beat_carries_the_work_and_the_notes_and_is_no_push_of_work(self):
        self._product_hook('exit 1\n')  # the product's hook refuses everything: a beat skips it
        p = self.start()
        sha = self.wait_ref()
        msg = subprocess.run(['git', 'log', '-n1', '--format=%B', sha], cwd=self.remote,
                             capture_output=True, text=True).stdout
        self.assertTrue(msg.startswith('wip: heartbeat 20'))
        self.assertIn('next: the tests', msg)
        self.assertIn('ASF-Session: sample/j@1', msg)
        files = subprocess.run(['git', 'ls-tree', '--name-only', sha], cwd=self.remote,
                               capture_output=True, text=True).stdout.split()
        self.assertIn('wip.txt', files)
        self.assertEqual(self._logged(), [])  # never counted as a push
        self.assertEqual(git('status', '--porcelain', cwd=self.wt).count('wip.txt'), 1)
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=self.wt), 'fix/B-0001')
        self.assertIsNone(p.poll())

    def test_the_loop_exits_once_the_factory_takes_its_ref(self):
        p = self.start()
        self.wait_ref()
        subprocess.run(['git', 'update-ref', '-d', 'refs/asf/hb/j'], cwd=self.remote, check=True)
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail('a fenced beat loop kept running')
        self.wait_ref(want=False, limit=3)

    def test_the_shim_lets_a_beat_through_and_a_branch_push_still_runs_the_hooks(self):
        self._product_hook('exit 1\n')
        p = self._commit_and_push(1)
        self.assertNotEqual(p.returncode, 0)  # a branch push still meets the product's hook
        self.assertIn('refs/asf/hb/', githooks.ASF_HOOK)


class CloudStall(Home):
    """The claude-remote lane: a run that stops beating is ended and continued in one sync."""
    client = test_remote.Sync.client
    launch = test_remote.Sync.launch
    sync = test_remote.Sync.sync
    push_report = test_remote.Sync.push_report

    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.cfg = dict(self.cfg, cloud=dict(test_remote.ON))
        self.cfg['worker_pool'] = dict(self.cfg['worker_pool'], accounts=[
            {'name': 'acct-a', 'role': 'local', 'cap': 1},
            {'name': 'acct-c', 'role': 'worker', 'cap': 1}])
        self.fake = test_remote.FakeHelper()
        self.cfg['cloud'] = dict(self.cfg['cloud'], poll_min=600)
        self.fake.answers.update({
            'list_runs': (200, {'data': [{'id': 'cse_9', 'status': 'running',
                                          'worker_status': 'requires_action',
                                          'last_event_at': '2026-10-06T10:00:00Z'}]}, ''),
            'get_run_log': (200, {'data': [
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pytest -x'}}]}},
                {'type': 'error', 'error': 'waiting on a permission prompt'}]}, '')})
        self.origin = os.path.join(self.tmp, 'origin.git')

    def checkout(self, rec):
        other = os.path.join(self.tmp, 'cloud-checkout')
        if not os.path.isdir(other):
            git('clone', '-q', '-b', rec['branch'], self.origin, other, cwd=self.tmp)
            for k, v in (('user.email', 'c@example.com'), ('user.name', 'c')):
                git('config', k, v, cwd=other)
        return other

    def beat(self, rec, notes='done: the parser\nnext: the tests'):
        other = self.checkout(rec)
        with open(os.path.join(other, 'wip.txt'), 'a', encoding='utf-8') as f:
            f.write('more\n')
        git('add', '-A', cwd=other)
        tree = git('write-tree', cwd=other)
        c = git('commit-tree', tree, '-p', 'HEAD', '-m', 'wip: heartbeat now', '-m', notes,
                '-m', f"ASF-Session: {rec['session']}", cwd=other)
        git('reset', '-q', cwd=other)
        git('push', '-q', '--force', 'origin', f'{c}:refs/asf/hb/{rec["job"]}', cwd=other)
        return c

    def t0(self, rec):
        return hb.parse_ts(rec['started'])

    def test_a_stalled_run_is_dead_and_continued_in_the_same_sync(self):
        self.stall_and_continue()

    def stall_and_continue(self):
        rec = self.launch()
        self.assertEqual(rec['heartbeat_min'], 5)
        brief = git('show', 'refs/asf/briefs/spec-1:brief.md', cwd=self.origin)
        self.assertIn(f"refs/asf/hb/spec-1 '{rec['session']}'", brief)
        t0 = self.t0(rec)
        self.assertEqual(self.sync(t0 + 60)[0][1], cloud.WORKING)
        self.assertEqual(self.fake.actions(), ['create', 'run', 'get'])
        snap = self.beat(rec)
        self.assertEqual(self.sync(t0 + 20 * M)[0][1], cloud.WORKING)
        self.assertEqual(self.sync(t0 + 29 * M)[0][1], cloud.WORKING)  # last beat 9m old
        # the detection itself made no helper call (poll_min is off the stall path)
        self.assertEqual(self.fake.actions(), ['create', 'run', 'get'])
        self.fake.answers['create'] = (200, {'trigger': {'id': 'trig_2'}},
                                       'https://claude.ai/code/routines/trig_2')
        self.fake.answers['run'] = (200, {'session_id': 'cse_10'}, '')
        lines = []
        found = cloud.sync(self.product, self.cfg, out=lines.append, now=t0 + 31 * M,
                           remote_client=self.client())
        (job, status, why), = found
        self.assertEqual((job, status), ('spec-1', cloud.DEAD))
        self.assertTrue(why.startswith('stalled: no beat 11m'), why)
        self.assertIn('worker_status=requires_action', why)
        # one list_runs on the transition, the routine disabled, the run log read, relaunched
        self.assertEqual(self.fake.actions()[3:],
                         ['list_runs', 'update', 'get_run_log', 'create', 'run'])
        self.assertEqual(cloudpid.load()['remote:trig_1']['worker_status'], 'requires_action')
        self.assertEqual(cloudpid.load()['remote:trig_1']['status'], cloud.DEAD)
        # the snapshot is on the branch the continuation starts from; the beat ref is gone
        self.assertEqual(git('rev-parse', f"refs/heads/{rec['branch']}", cwd=self.origin), snap)
        self.assertEqual(git('for-each-ref', 'refs/asf/hb/', cwd=self.origin), '')
        runs = lifecycle.runs(pool_mod.sessions_path(self.product))['spec-1']
        old, new = runs[-2], runs[-1]
        self.assertTrue(old['end_reason'].startswith('failed: stalled: no beat 11m'), old)
        self.assertEqual((new['pid'], new['branch'], new['resumed_from'], new['resumes']),
                         ('remote:trig_2', rec['branch'], rec['session'], 1))
        self.assertNotEqual(new['session'], rec['session'])
        self.assertTrue(lifecycle.is_live(new))
        self.assertTrue(lifecycle.pid_alive(new['pid']))
        brief = git('show', 'refs/asf/briefs/spec-1:brief.md', cwd=self.origin)
        self.assertIn('CONTINUE', brief)
        self.assertIn(snap[:12], brief)
        self.assertIn('next: the tests', brief)
        self.assertIn('tool Bash: {"command": "pytest -x"}', brief)
        self.assertIn('waiting on a permission prompt', brief)
        self.assertIn(f"refs/asf/hb/spec-1 '{new['session']}'", brief)  # the new id beats
        self.assertEqual(brief.count('CONTINUE\n'), 1)
        self.assertTrue(any('continued as' in ln for ln in lines), lines)
        return rec, new

    def test_a_zombie_push_never_finishes_either_run(self):
        rec, new = self.stall_and_continue()
        other = self.checkout(rec)  # the old session, still running, pushes its report
        git('fetch', '-q', 'origin', cwd=other)
        git('reset', '-q', '--hard', f"origin/{rec['branch']}", cwd=other)
        git('commit', '-q', '--allow-empty', '-m', 'asf: report spec-1\n\nREPORT\nitem: F-0001',
            '--trailer', f"ASF-Session: {rec['session']}", '--trailer', 'ASF-Report: spec-1',
            cwd=other)
        git('push', '-q', 'origin', rec['branch'], cwd=other)
        lines = []
        (job, status, why), = cloud.sync(self.product, self.cfg, out=lines.append,
                                         now=self.t0(new) + 60, remote_client=self.client())
        self.assertEqual(status, cloud.WORKING, why)
        self.assertTrue(any('zombie push' in ln for ln in lines), lines)
        self.assertIsNone(runtime_mod.read_result(new['log']))
        self.assertTrue(lifecycle.is_live(pool_mod.load_sessions(self.product)['spec-1']))
        old = lifecycle.runs(pool_mod.sessions_path(self.product))['spec-1'][-2]
        self.assertTrue(old['end_reason'].startswith('failed: stalled'))
        # the zombie's push is no movement of the new run
        self.assertIsNone(hb.load_state(self.product)['spec-1']['moved'])

    def test_a_run_that_keeps_beating_still_times_out(self):
        rec = self.launch()
        t0 = self.t0(rec)
        self.sync(t0 + 60)
        for minute in range(20, 241, 9):
            self.beat(rec)
            self.assertEqual(self.sync(t0 + minute * M)[0][1], cloud.WORKING, minute)
        self.beat(rec)
        (_job, status, why), = self.sync(t0 + 241 * M)
        self.assertEqual((status, why), (cloud.DEAD, 'timed out after 240m'))
        self.assertNotIn('create', self.fake.actions()[2:])  # a timeout is not continued

    def test_resumes_cap_the_chain(self):
        self.cfg['workers'] = {'heartbeat_resumes': 0}
        rec = self.launch()
        t0 = self.t0(rec)
        self.sync(t0 + 60)
        (_job, status, why), = self.sync(t0 + 25 * M)
        self.assertEqual(status, cloud.DEAD)
        self.assertTrue(why.startswith('stalled: no beat'))
        self.assertNotIn('create', self.fake.actions()[2:])
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertEqual(run['session'], rec['session'])  # left to health's dead path


class LocalStall(Home):
    """A minimal product — one account, no cloud lane — on the local lane."""

    def test_a_stalled_local_run_is_stopped_and_continued(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        rec = spawn_mod.spawn(self.product, feature_row('spec-1'), self.acct(), 'spec it\n',
                              runtime=rt, cfg=self.cfg)
        _job, brief = rt.calls[0]
        self.assertIn('HEARTBEAT', brief)
        self.assertIn(f"refs/asf/hb/spec-1 '{rec['session']}'", brief)
        self.assertEqual(rec['heartbeat_min'], 5)
        with open(os.path.join(rec['worktree'], 'NOTES.asf.md'), 'w', encoding='utf-8') as f:
            f.write('next: wire the parser')
        with open(rec['log'], 'a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'a.py'}}]}}) + '\n')
        t0 = hb.parse_ts(rec['started'])
        stopped, lines = [], []
        rt2 = runtime_mod.FakeRuntime([{'running': True, 'pid': 4343}])
        kw = dict(cfg=self.cfg, runtime_fn=lambda: rt2, alive=lambda pid: True,
                  stop=lambda run, alive: stopped.append(run['pid']), out=lines.append)
        self.assertEqual(hb.sweep(self.product, now=t0 + 60, **kw), [])
        (job, why, new), = hb.sweep(self.product, now=t0 + 21 * M, **kw)
        self.assertEqual((job, why), ('spec-1', 'stalled: no beat 21m'))
        self.assertEqual(stopped, [4242])
        self.assertEqual((new['pid'], new['worktree'], new['resumed_from']),
                         (4343, rec['worktree'], rec['session']))
        _job, text = rt2.calls[0]
        self.assertIn('CONTINUE', text)
        self.assertIn('next: wire the parser', text)
        self.assertIn('tool Read', text)
        self.assertIn("Your worktree is the stalled run's own", text)
        runs = lifecycle.runs(pool_mod.sessions_path(self.product))['spec-1']
        self.assertTrue(runs[-2]['end_reason'].startswith('failed: stalled'))
        self.assertTrue(lifecycle.is_live(runs[-1]))

    def test_an_ended_runs_beat_ref_is_deleted_on_origin(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        rec = spawn_mod.spawn(self.product, feature_row('spec-1'), self.acct(), 'spec it\n',
                              runtime=rt, cfg=self.cfg)
        wt = rec['worktree']
        git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q',
            '--allow-empty', '-m', 'wip: heartbeat', cwd=wt)
        git('push', '-q', '--no-verify', 'origin', 'HEAD:refs/asf/hb/spec-1', cwd=wt)
        t0 = hb.parse_ts(rec['started'])
        kw = dict(cfg=self.cfg, alive=lambda pid: True, stop=lambda *a: self.fail('stopped'),
                  out=lambda _l: None)
        self.assertEqual(hb.sweep(self.product, now=t0 + 60, **kw), [])
        origin = os.path.join(self.tmp, 'origin.git')
        self.assertNotEqual(git('for-each-ref', 'refs/asf/hb/', cwd=origin), '')
        pool_mod.update_session(self.product, 'spec-1', ended=pool_mod.now_iso(),
                                end_reason=lifecycle.FINISHED)
        hb.sweep(self.product, now=t0 + 120, **kw)
        self.assertEqual(git('for-each-ref', 'refs/asf/hb/', cwd=origin), '')
        self.assertEqual(hb.load_state(self.product), {})

    def test_a_run_without_the_heartbeat_is_never_judged(self):
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        rec = spawn_mod.spawn(self.product, feature_row('spec-1'), self.acct(), 'spec it\n',
                              runtime=rt, cfg=self.cfg)
        pool_mod.update_session(self.product, 'spec-1', heartbeat_min=None)
        found = hb.sweep(self.product, cfg=self.cfg, now=hb.parse_ts(rec['started']) + 3600,
                         alive=lambda pid: True, stop=lambda *a: self.fail('stopped'),
                         out=lambda _l: None)
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
