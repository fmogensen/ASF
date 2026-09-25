"""asf.workers.cloud — the cloud lane: the launch command line, what the launcher prints, the
status mapping, the pid token every liveness check reads, placement under host pressure, the sync
of a finished and a timed-out run, and the doctor rows. Nothing is dispatched: the launcher is a
fake spawn, git is a bare repo in a temp dir."""
import json
import os
import subprocess
import tempfile
import time
import unittest

from asf import env
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import wave as wave_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_cloud` does not
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row, git

ON = {'enabled': True, 'runtime': 'claude-cloud', 'max_inflight': 2, 'rows': 'any',
      'accounts': ['acct-c']}


def job(**kw):
    j = runtime_mod.Job('sample', 'task-t-0001', '/wt', '/b.md', 'opus',
                        env={'BACKLOG_ID_RANGE': 'S:5000-5049', 'ASF_SESSION': 'sid-1',
                             'VITEST_MAX_WORKERS': '2'},
                        branch='task/t-0001', base='main', setup='pnpm install')
    for k, v in kw.items():
        setattr(j, k, v)
    return j


class Command(unittest.TestCase):
    def test_default_cloud_environment(self):
        s = cloud.settings({'cloud': ON})
        self.assertEqual(cloud.build_command(job(), s), [
            'claude', '-p', '--cloud', '--on-branch', 'task/t-0001', '--model', 'opus',
            '-n', 'task-t-0001', '--forward-home-settings', 'false', '--output-format', 'json'])

    def test_a_self_hosted_environment(self):
        s = cloud.settings({'cloud': dict(ON, environment='ccpool_abc'),
                            'worker_pool': {'binary': '/opt/claude'}})
        cmd = cloud.build_command(job(), s)
        self.assertEqual(cmd[:4], ['/opt/claude', '-p', '--environment', 'ccpool_abc'])
        self.assertNotIn('--cloud', cmd)

    def test_settings_fall_back_to_the_inherited_cap_and_the_product_overrides(self):
        cfg = {'cloud': {'enabled': False}, 'worker_pool': {'caps': {'cloud_max_inflight': 3}}}
        product = env.Product('sample', {'cloud': {'enabled': True}})
        s = cloud.settings(cfg, product)
        self.assertTrue(s.on)
        self.assertEqual((s.max_inflight, s.rows), (3, cloud.ROWS_CLOUD_OK))
        self.assertFalse(cloud.settings(cfg).on)

    def test_the_brief_carries_the_branch_the_env_and_the_end_marker(self):
        text = cloud.cloud_brief('Do the task.\n', job())
        self.assertTrue(text.startswith('Do the task.'))
        self.assertIn('checked out on branch `task/t-0001` (base `main`)', text)
        self.assertIn("export ASF_SESSION='sid-1' BACKLOG_ID_RANGE='S:5000-5049'", text)
        self.assertIn('`pnpm install`', text)
        self.assertIn('ASF-Session: sid-1', text)
        self.assertIn('ASF-Report: task-t-0001', text)


class ParseLaunch(unittest.TestCase):
    def log(self, *lines):
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        self.addCleanup(os.remove, path)
        return path

    def test_json_created(self):
        la = cloud.parse_launch(self.log(
            json.dumps({'type': 'asf', 'subtype': 'session', 'session': 's'}),
            json.dumps({'ok': True, 'session_id': 'cse_1', 'title': 't',
                        'url': 'https://claude.ai/code/cse_1'})))
        self.assertEqual((la.session_id, la.url, la.error, la.result),
                         ('cse_1', 'https://claude.ai/code/cse_1', '', None))

    def test_json_refused(self):
        la = cloud.parse_launch(self.log(json.dumps({'ok': False, 'error': 'no environment'})))
        self.assertEqual((la.session_id, la.error), ('', 'no environment'))

    def test_text_form(self):
        la = cloud.parse_launch(self.log('Created cloud session: t', 'Session ID: cse_2',
                                         'View: https://claude.ai/code/cse_2',
                                         'Resume with: claude --teleport cse_2'))
        self.assertEqual((la.session_id, la.url), ('cse_2', 'https://claude.ai/code/cse_2'))
        la = cloud.parse_launch(self.log('Error: Unable to create cloud session'))
        self.assertEqual(la.error, 'Unable to create cloud session')

    def test_an_attached_launcher_result(self):
        la = cloud.parse_launch(self.log(json.dumps(
            {'type': 'result', 'subtype': 'success', 'result': 'REPORT', 'session_id': 'cse_3',
             'session_url': 'https://claude.ai/code/cse_3'})))
        self.assertEqual(la.session_id, 'cse_3')
        self.assertEqual(la.result['result'], 'REPORT')


class Classify(unittest.TestCase):
    L = cloud.Launch

    def test_the_mapping(self):
        rep = {'sha': 'a' * 40, 'body': 'REPORT'}
        cases = [
            (self.L(result={'type': 'result'}), None, False, 5, cloud.FINISHED),
            (self.L('cse'), rep, False, 5, cloud.FINISHED),
            (self.L(error='refused'), None, False, 1, cloud.DEAD),
            (self.L(error='refused'), None, True, 1, cloud.WORKING),   # still printing
            (self.L('cse'), None, False, 300, cloud.DEAD),              # timed out
            (self.L(), None, True, 1, cloud.WORKING),                   # launching
            (self.L('cse'), None, False, 30, cloud.WORKING),            # running in the cloud
            (self.L(), None, False, 1, cloud.DEAD),                     # no session at all
        ]
        for launch, report, up, minutes, want in cases:
            status, why = cloud.classify(launch, report, up, minutes, 240)
            self.assertEqual(status, want, (launch, report, up, minutes, why))
        self.assertEqual(cloud.classify(self.L('cse'), None, False, 300, 240)[1],
                         'timed out after 240m')


class TokenLiveness(unittest.TestCase):
    def setUp(self):
        self._home = env.ASF_HOME
        env.ASF_HOME = tempfile.mkdtemp()

    def tearDown(self):
        env.ASF_HOME = self._home

    def test_every_liveness_check_reads_the_status_file(self):
        tok = cloudpid.token('sid-9')
        run = {'job': 'j', 'pid': tok, 'started': 't'}
        self.assertTrue(lifecycle.pid_alive(tok))          # not seen yet: a launch
        self.assertTrue(lifecycle.occupies(run))
        alive = observe.identity_alive([], [run])
        self.assertTrue(alive(tok))
        cloudpid.record(tok, cloudpid.FINISHED, 'report commit')
        self.assertFalse(lifecycle.pid_alive(tok))
        self.assertFalse(lifecycle.occupies(run))
        self.assertFalse(alive(tok))
        from asf.views import sessions
        self.assertFalse(sessions.pid_alive(tok))


class LauncherRun(unittest.TestCase):
    def setUp(self):
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        env.ASF_HOME = self._home

    def test_run_records_the_token_and_the_session(self):
        brief = os.path.join(self.tmp, 'b.md')
        with open(brief, 'w') as f:
            f.write('the brief')
        calls = []

        def spawn(argv, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
            calls.append((argv, stdin.read().decode(), env))
            stdout.write(json.dumps({'ok': True, 'session_id': 'cse_7',
                                     'url': 'https://claude.ai/code/cse_7'}).encode() + b'\n')
            stdout.flush()
            return 4242

        j = job(brief_path=brief, cwd=self.tmp, log_path=os.path.join(self.tmp, 'j.jsonl'))
        rt = cloud.CloudRuntime(cloud.settings({'cloud': ON}), spawn=spawn,
                                sleep=lambda _s: None, alive=lambda _p: False)
        res = rt.run(j)
        self.assertEqual(res.pid, 'cloud:sid-1')
        self.assertEqual(res.extra['cloud_session'], 'cse_7')
        self.assertEqual(res.extra['launcher_pid'], 4242)
        argv, stdin, child_env = calls[0]
        self.assertEqual(argv[:3], ['claude', '-p', '--cloud'])
        self.assertIn('CLOUD SESSION', stdin)
        self.assertEqual(child_env['ASF_SESSION'], 'sid-1')
        self.assertIsNone(rt.continue_run(j))


class FakeCloudRuntime(runtime_mod.Runtime):
    """The cloud lane with no launch: a token pid and a session id, as the real one records."""
    name = cloud.RUNTIME
    lane = 'cloud'

    def __init__(self):
        self.jobs = []

    def run(self, job, wait=False):
        self.jobs.append(job)
        log_path = job.log_path or runtime_mod.job_log_path(job.product, job.name)
        open(log_path, 'a').close()
        r = runtime_mod.Result(pid=cloudpid.token(job.session), log_path=log_path)
        r.extra = {'lane': 'cloud', 'launcher_pid': None, 'cloud_session': f'cse_{job.name}',
                   'cloud_url': f'https://claude.ai/code/cse_{job.name}'}
        return r


class Placement(Home):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, cloud=dict(ON))
        self.cfg['worker_pool'] = dict(self.cfg['worker_pool'], accounts=[
            {'name': 'acct-a', 'role': 'local', 'cap': 1},
            {'name': 'acct-c', 'role': 'cloud', 'cap': 1}])

    def run_wave(self, rows, live=(), local_hold='', cfg=None):
        accounts = pool_mod.accounts_from_config(cfg or self.cfg)
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource({}), live=live)
        self.crt = FakeCloudRuntime()
        lines = []
        launched, waits = wave_mod.wave(
            self.product, rows, 5, pool=pool,
            runtime=runtime_mod.FakeRuntime([{'running': True}] * 5), cfg=cfg or self.cfg,
            out=lines.append, local_hold=local_hold, cloud_runtime=self.crt)
        return launched, waits, lines

    def test_host_pressure_sends_the_row_to_the_cloud(self):
        launched, waits, lines = self.run_wave([feature_row('spec-1')],
                                               local_hold='host pressure load 50/cores 10')
        self.assertEqual(waits, [])
        (row, rec), = launched
        self.assertEqual((rec['account'], rec['lane'], rec['pid']),
                         ('acct-c', 'cloud', f"cloud:{rec['session']}"))
        self.assertEqual(rec['cloud_session'], 'cse_spec-1')
        self.assertIn('→ acct-c (opus) cloud https://claude.ai/code/cse_spec-1', lines[0])
        # the fresh branch is on origin before the session checks it out, and nothing was set
        # up on this host
        self.assertTrue(git('ls-remote', '--heads', 'origin', rec['branch'], cwd=self.repo))
        self.assertEqual(self.crt.jobs[0].branch, rec['branch'])
        self.assertNotIn('setup_s', rec)

    def test_a_free_local_seat_keeps_the_row_local(self):
        launched, _waits, _lines = self.run_wave([feature_row('spec-1')])
        self.assertEqual(launched[0][1]['account'], 'acct-a')
        self.assertNotEqual(launched[0][1].get('lane'), 'cloud')

    def test_a_full_local_lane_overflows_to_the_cloud_and_the_cloud_cap_holds(self):
        live = [{'job': 'x', 'account': 'acct-a'},                     # the local seat taken
                {'job': 'c1', 'account': 'acct-c', 'lane': 'cloud'}]   # one of two cloud seats
        rows = [feature_row('spec-1'), feature_row('spec-2', item='F-0002')]
        launched, waits, _lines = self.run_wave(rows, live=live)
        self.assertEqual([(r.job, rec['lane']) for r, rec in launched], [('spec-1', 'cloud')])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('spec-2', 'pool full; cloud full — 2/2 in flight')])

    def test_a_row_not_cloud_ok_waits_on_the_host(self):
        cfg = dict(self.cfg, cloud=dict(ON, rows='cloud-ok'))
        launched, waits, _ = self.run_wave([feature_row('spec-1')], local_hold='host pressure x',
                                           cfg=cfg)
        self.assertEqual(launched, [])
        self.assertEqual(waits[0][1], 'held: host pressure x')
        row = pool_mod.parse_row('STARVED → SPEC F-0003 "f" (cloud-ok)   → launch spec-3 (Opus)')
        self.assertTrue(row.cloud_ok)
        launched, _, _ = self.run_wave([row], local_hold='host pressure x', cfg=cfg)
        self.assertEqual(launched[0][1]['lane'], 'cloud')

    def test_the_lane_off_changes_nothing(self):
        cfg = dict(self.cfg, cloud={'enabled': False})
        live = [{'job': 'x', 'account': 'acct-a'}, {'job': 'y', 'account': 'acct-c'}]
        launched, waits, _ = self.run_wave([feature_row('spec-1')], live=live, cfg=cfg)
        self.assertEqual((launched, waits[0][1]), ([], 'pool full'))


class Sync(Placement):
    def launch(self):
        launched, _, _ = self.run_wave([feature_row('spec-1')], local_hold='host pressure')
        return launched[0][1]

    def push_as_the_cloud_session(self, rec, trailers=True):
        other = os.path.join(self.tmp, 'cloud-checkout')
        git('clone', '-q', '-b', rec['branch'], os.path.join(self.tmp, 'origin.git'), other,
            cwd=self.tmp)
        for k, v in (('user.email', 'c@example.com'), ('user.name', 'c')):
            git('config', k, v, cwd=other)
        with open(os.path.join(other, 'work.txt'), 'w') as f:
            f.write('work\n')
        git('add', '.', cwd=other)
        msg = f'asf: report spec-1\n\nREPORT\nitem: F-0001\npushed: yes'
        args = ['commit', '-q', '-m', msg]
        if trailers:
            args += ['--trailer', f"ASF-Session: {rec['session']}", '--trailer',
                     'ASF-Report: spec-1']
        git(*args, cwd=other)
        git('push', '-q', 'origin', rec['branch'], cwd=other)
        return git('rev-parse', 'HEAD', cwd=other)

    def test_the_report_commit_finishes_the_run(self):
        rec = self.launch()
        lines = []
        self.assertEqual(cloud.sync(self.product, self.cfg, launcher_alive=lambda _p: False,
                                    out=lines.append)[0][1], cloud.WORKING)
        self.assertTrue(lifecycle.pid_alive(rec['pid']))
        tip = self.push_as_the_cloud_session(rec)
        (job, status, why), = cloud.sync(self.product, self.cfg,
                                         launcher_alive=lambda _p: False, out=lines.append)
        self.assertEqual((job, status), ('spec-1', cloud.FINISHED), why)
        self.assertFalse(lifecycle.pid_alive(rec['pid']))
        result = runtime_mod.read_result(rec['log'])
        self.assertTrue(runtime_mod.result_ok(result))
        self.assertIn('item: F-0001', result['result'])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=rec['worktree']), tip)  # caught up
        ev = lifecycle.gather(self.product, pool_mod.load_sessions(self.product)['spec-1'])
        self.assertEqual(lifecycle.judge(rec, ev), lifecycle.FINISHED)
        self.assertIn('cloud    spec-1', lines[-1])

    def test_a_commit_without_the_marker_is_still_working(self):
        rec = self.launch()
        self.push_as_the_cloud_session(rec, trailers=False)
        (_job, status, _why), = cloud.sync(self.product, self.cfg,
                                           launcher_alive=lambda _p: False, out=lambda _l: None)
        self.assertEqual(status, cloud.WORKING)

    def test_a_run_past_its_limit_is_stopped_and_dead(self):
        rec = self.launch()
        stopped = []
        later = time.time() + 5 * 3600
        (_job, status, why), = cloud.sync(self.product, self.cfg, now=later,
                                          launcher_alive=lambda _p: False,
                                          stop_fn=stopped.append, out=lambda _l: None)
        self.assertEqual((status, why), (cloud.DEAD, 'timed out after 240m'))
        self.assertEqual([r['job'] for r in stopped], ['spec-1'])
        self.assertFalse(lifecycle.pid_alive(rec['pid']))
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertEqual(lifecycle.judge(run, lifecycle.gather(self.product, run)),
                         lifecycle.DEAD_PID)


class Doctor(unittest.TestCase):
    HELP = 'Usage: claude\n  --cloud [description]\n  --environment <id>\n'

    def run_cmd(self, text):
        return lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=text, stderr='')

    def product(self, url):
        tmp = tempfile.mkdtemp()
        subprocess.run(['git', 'init', '-q', tmp], check=True)
        subprocess.run(['git', '-C', tmp, 'remote', 'add', 'origin', url], check=True)
        return env.Product('sample', {'repo_dir': tmp, 'main': 'main'})

    def cfg(self, **over):
        return {'cloud': dict(ON, **over),
                'worker_pool': {'accounts': [{'name': 'acct-c', 'role': 'cloud'}]}}

    def test_no_rows_while_off(self):
        self.assertEqual(cloud.doctor_rows({}, self.product('x'), self.run_cmd('')), [])

    def test_a_sound_lane_is_one_green_row(self):
        rows = cloud.doctor_rows(self.cfg(), self.product('https://github.com/o/r.git'),
                                 self.run_cmd(self.HELP))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], (False, True))
        self.assertIn('cloud lane on: 2 seat(s)', rows[0][2])

    def test_each_gap_is_a_red_row(self):
        cfg = self.cfg(environment='pool-1', accounts=['nobody'], max_inflight=0)
        rows = cloud.doctor_rows(cfg, self.product('/srv/git/r.git'),
                                 self.run_cmd('Usage: claude\n  --cloud\n'))
        text = '\n'.join(d for _r, ok, d in rows if not ok)
        for want in ('cloud.max_inflight is 0', "cloud.environment 'pool-1' is not",
                     'no cloud-lane account', 'has no --environment', 'no GitHub origin'):
            self.assertIn(want, text)
        self.assertTrue(all(required for required, ok, _d in rows if not ok))

    def test_the_doctor_table_carries_the_rows(self):
        from asf import doctor
        rows = doctor.check_cloud(self.cfg(runtime='actions'), self.product('x'))
        self.assertIn("cloud.runtime 'actions' is not one ASF runs", rows[0][2])


if __name__ == '__main__':
    unittest.main()
