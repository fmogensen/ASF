"""asf.workers.cloud and asf.workers.actions — the cloud lane: claude-cloud refused at config
check, the actions runtime (brief ref, dispatch, run lookup), the workflow template and its
install, the status mapping, the pid token every liveness check reads, placement under host
pressure, the sync of a finished, a failed and a timed-out run, and the doctor rows. Nothing is
dispatched: gh is a fake, git is a bare repo in a temp dir."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.workers import actions
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import wave as wave_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_cloud` does not
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row, git

ON = {'enabled': True, 'runtime': 'actions', 'max_inflight': 2, 'rows': 'any',
      'accounts': ['acct-c'], 'launch_wait_s': 0}


class FakeGh:
    """``gh`` as a run callable: records each argv, answers from ``self.*``."""

    def __init__(self, runs=(), view=None, secrets=('CLAUDE_CODE_OAUTH_TOKEN',), workflow=True,
                 dispatch_ok=True, runners=()):
        self.calls = []
        self.runs = list(runs)
        self.view_ = view or {'status': 'in_progress', 'conclusion': ''}
        self.secrets = secrets
        self.workflow = workflow
        self.dispatch_ok = dispatch_ok
        self.runners = list(runners)

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        a = argv[1:]

        def done(out='', rc=0, err=''):
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)
        if a[:2] == ['workflow', 'run']:
            return done() if self.dispatch_ok else done(rc=1, err='HTTP 404: workflow not found')
        if a[:2] == ['run', 'list']:
            return done(json.dumps(self.runs))
        if a[:2] == ['run', 'view']:
            return done(json.dumps(self.view_))
        if a[:2] == ['run', 'cancel']:
            return done()
        if a[:2] == ['secret', 'list']:
            if self.secrets is None:
                return done(rc=1, err='HTTP 403')
            return done(json.dumps([{'name': n} for n in self.secrets]))
        if a[0] == 'api' and '/contents/' in a[1]:
            return done('.github/workflows/asf-worker.yml') if self.workflow \
                else done(rc=1, err='HTTP 404: Not Found')
        if a[0] == 'api' and 'actions/runners' in ' '.join(a):
            return done(''.join(json.dumps(r) + '\n' for r in self.runners))
        return done(rc=1, err=f'unexpected {argv}')

    def named(self, *prefix):
        return [c for c in self.calls if c[1:1 + len(prefix)] == list(prefix)]


def job(**kw):
    j = runtime_mod.Job('sample', 'task-t-0001', '/wt', '/b.md', 'opus',
                        env={'BACKLOG_ID_RANGE': 'S:5000-5049', 'ASF_SESSION': 'sid-1',
                             'VITEST_MAX_WORKERS': '2'},
                        branch='task/t-0001', base='main', setup='pnpm install')
    for k, v in kw.items():
        setattr(j, k, v)
    return j


class Refused(unittest.TestCase):
    """claude-cloud cannot launch (`-p --cloud` is interactive only): the config check refuses it."""

    def test_the_block_is_refused_with_the_reason(self):
        (key, why), = cloud.config_problems({'enabled': True, 'runtime': 'claude-cloud'})
        self.assertEqual(key, 'cloud.runtime')
        self.assertIn('--cloud cannot be combined with --print. Starting a new cloud session '
                      'with --cloud is interactive only', why)
        self.assertIn('--bg and --cloud are different backends', why)
        self.assertIn('use runtime: claude-remote', why)
        self.assertEqual(cloud.config_problems({'runtime': 'claude-cloud'})[0][0], 'cloud.runtime')
        self.assertEqual(cloud.config_problems({'enabled': True}), [])  # the default: actions
        self.assertEqual(cloud.config_problems(None), [])
        self.assertEqual(cloud.config_problems({'token_secret': 'a b'})[0][0],
                         'cloud.token_secret')
        self.assertFalse(cloud.settings({'cloud': {'enabled': True, 'runtime': 'claude-cloud',
                                                    'max_inflight': 2}}).on)

    def test_load_config_refuses_it(self):
        home = tempfile.mkdtemp()
        with open(os.path.join(home, 'config.yaml'), 'w') as f:
            f.write('cloud:\n  enabled: true\n  runtime: claude-cloud\n  max_inflight: 2\n')
        old, env.ASF_HOME = env.ASF_HOME, home
        try:
            with self.assertRaises(env.ConfigError) as cm:
                env.load_config()
        finally:
            env.ASF_HOME = old
        self.assertIn('claude-cloud is refused', str(cm.exception))

    def test_a_product_file_is_refused_too(self):
        text = 'product: sample\ncloud:\n  enabled: true\n  runtime: claude-cloud\n'
        (_ln, key, why), = env.validate_product_text(text)
        self.assertEqual(key, 'cloud.runtime')
        self.assertIn('interactive only', why)
        ok = 'product: sample\ncloud:\n  enabled: true\n  runs_on: [self-hosted, linux]\n'
        self.assertEqual(env.validate_product_text(ok), [])

    def test_doctor_names_the_reason(self):
        cfg = {'cloud': dict(ON, runtime='claude-cloud')}
        (required, ok, detail), = cloud.doctor_rows(cfg, env.Product('sample', {}))
        self.assertEqual((required, ok), (True, False))
        self.assertIn('cloud.runtime claude-cloud is refused', detail)


class Settings(unittest.TestCase):
    def test_defaults_and_the_product_override(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1}})
        self.assertEqual((s.runtime, s.runs_on, s.token_secret, s.workflow),
                         ('actions', ('ubuntu-latest',), 'CLAUDE_CODE_OAUTH_TOKEN',
                          'asf-worker.yml'))
        self.assertTrue(s.on)
        cfg = {'cloud': {'enabled': False}, 'worker_pool': {'caps': {'cloud_max_inflight': 3}}}
        product = env.Product('sample', {'cloud': {'enabled': True,
                                                   'runs_on': ['self-hosted', 'linux']}})
        s = cloud.settings(cfg, product)
        self.assertTrue(s.on)
        self.assertEqual((s.max_inflight, s.rows, s.runs_on),
                         (3, cloud.ROWS_CLOUD_OK, ('self-hosted', 'linux')))
        self.assertFalse(cloud.settings(cfg).on)

    def test_the_brief_carries_the_branch_and_the_end_marker(self):
        text = cloud.cloud_brief('Do the task.\n', job())
        self.assertTrue(text.startswith('Do the task.'))
        self.assertIn('checked out on branch `task/t-0001` (base `main`)', text)
        self.assertIn('ASF-Session: sid-1', text)
        self.assertIn('ASF-Report: task-t-0001', text)


class Workflow(unittest.TestCase):
    def test_the_template(self):
        s = cloud.settings({'cloud': dict(ON, runs_on=['self-hosted', 'linux'],
                                          token_secret='CLAUDE_CODE_OAUTH_TOKEN',
                                          timeout_min=120)})
        text = actions.render_workflow(s)
        self.assertIn('  workflow_dispatch:\n', text)
        for name in ('job', 'branch', 'base', 'model', 'brief_ref', 'run_name'):
            self.assertIn(f'      {name}: {{', text)
        self.assertIn('run-name: ${{ inputs.run_name || inputs.job }}', text)
        self.assertIn('runs-on: ["self-hosted", "linux"]', text)
        self.assertIn('timeout-minutes: 130', text)
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}', text)
        self.assertIn('ref: ${{ inputs.branch }}', text)
        self.assertIn('npm i -g @anthropic-ai/claude-code', text)
        self.assertIn('claude "${args[@]}" < "$RUNNER_TEMP/asf/brief.md"', text)
        self.assertIn('>> "$GITHUB_ENV"', text)
        for placeholder in ('@RUNS_ON@', '@TIMEOUT@', '@SECRET@'):
            self.assertNotIn(placeholder, text)
        self.assertIn('runs-on: ubuntu-latest', actions.render_workflow(cloud.settings({})))

    def test_install_writes_and_pushes_nothing(self):
        repo = tempfile.mkdtemp()
        subprocess.run(['git', 'init', '-q', repo], check=True)
        product = env.Product('sample', {'repo_dir': repo, 'main': 'main', 'repo_slug': 'o/r'})
        lines = []
        path = actions.install(product, cloud.settings({'cloud': ON}), out=lines.append)
        self.assertEqual(path, os.path.join(repo, '.github', 'workflows', 'asf-worker.yml'))
        self.assertTrue(os.path.exists(path))
        text = '\n'.join(lines)
        self.assertIn('git -C', text)
        self.assertIn('add .github/workflows/asf-worker.yml', text)
        self.assertIn('repo secret CLAUDE_CODE_OAUTH_TOKEN', text)
        status = subprocess.run(['git', '-C', repo, 'status', '--porcelain'], capture_output=True,
                                text=True).stdout
        self.assertIn('?? .github/', status)  # written, not committed
        lines.clear()
        actions.install(product, cloud.settings({'cloud': ON}), out=lines.append)
        self.assertIn('unchanged', lines[0])


class Classify(unittest.TestCase):
    def test_the_mapping(self):
        rep = {'sha': 'a' * 40, 'body': 'REPORT'}
        done = {'status': 'completed', 'conclusion': 'failure'}
        running = {'status': 'in_progress', 'conclusion': ''}
        cases = [
            (running, rep, 5, '1', cloud.FINISHED),
            ({'status': 'completed', 'conclusion': 'success'}, rep, 5, '1', cloud.FINISHED),
            (done, None, 5, '1', cloud.DEAD),
            (running, None, 300, '1', cloud.DEAD),          # timed out
            (running, None, 30, '1', cloud.WORKING),
            ({'status': 'queued'}, None, 1, '1', cloud.WORKING),
            (None, None, 30, '1', cloud.WORKING),             # gh could not say
            (None, None, 1, None, cloud.WORKING),             # not in the run list yet
            (None, None, 20, None, cloud.DEAD),               # never appeared
        ]
        for view, report, minutes, run_id, want in cases:
            status, why = cloud.classify(view, report, minutes, 240, run_id)
            self.assertEqual(status, want, (view, report, minutes, run_id, why))
        self.assertEqual(cloud.classify(done, None, 5, 240, '9')[1],
                         'run 9 ended failure without the report commit')
        self.assertEqual(cloud.classify(running, None, 300, 240, '9')[1], 'timed out after 240m')


class TokenLiveness(unittest.TestCase):
    def setUp(self):
        self._home = env.ASF_HOME
        env.ASF_HOME = tempfile.mkdtemp()

    def tearDown(self):
        env.ASF_HOME = self._home

    def test_every_liveness_check_reads_the_status_file(self):
        tok = cloudpid.token('123')
        self.assertEqual(tok, 'actions:123')
        self.assertTrue(cloudpid.is_token('cloud:sid-old'))
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


class Launch(Home):
    """The actions runtime against a bare origin and a fake gh."""

    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.brief = os.path.join(self.tmp, 'b.md')
        with open(self.brief, 'w') as f:
            f.write('the brief')

    def job(self):
        return job(brief_path=self.brief, cwd=self.repo,
                   log_path=os.path.join(self.tmp, 'j.jsonl'))

    def runtime(self, fake):
        return actions.ActionsRuntime(cloud.settings({'cloud': ON}), self.product,
                                      gh=actions.Gh(self.product, run=fake),
                                      sleep=lambda _s: None)

    def test_brief_ref_dispatch_and_run_id(self):
        fake = FakeGh(runs=[{'databaseId': 77, 'displayTitle': 'asf task-t-0001 sid-1',
                             'status': 'queued', 'url': 'https://example.test/runs/77'},
                            {'databaseId': 76, 'displayTitle': 'asf other', 'status': 'queued'}])
        res = self.runtime(fake).run(self.job())
        self.assertEqual(res.pid, 'actions:77')
        self.assertEqual((res.extra['actions_run_id'], res.extra['cloud_url'], res.extra['brief_ref']),
                         ('77', 'https://example.test/runs/77', 'refs/asf/briefs/task-t-0001'))
        (argv,) = fake.named('workflow', 'run')
        self.assertEqual(argv[:8], ['gh', 'workflow', 'run', 'asf-worker.yml', '-R', 'o/r',
                                    '--ref', 'main'])
        fields = dict(argv[i + 1].split('=', 1) for i, a in enumerate(argv) if a == '-f')
        self.assertEqual(fields, {'job': 'task-t-0001', 'branch': 'task/t-0001', 'base': 'main',
                                  'model': 'opus', 'brief_ref': 'refs/asf/briefs/task-t-0001',
                                  'run_name': 'asf task-t-0001 sid-1'})
        # the brief is on origin as a tree the workflow reads back the way it does
        other = os.path.join(self.tmp, 'ci-checkout')
        git('clone', '-q', os.path.join(self.tmp, 'origin.git'), other, cwd=self.tmp)
        git('fetch', '--no-tags', '-q', 'origin',
            '+refs/asf/briefs/task-t-0001:refs/asf/brief', cwd=other)
        brief = git('cat-file', '-p', 'refs/asf/brief:brief.md', cwd=other)
        self.assertTrue(brief.startswith('the brief'))
        self.assertIn('ASF-Report: task-t-0001', brief)
        self.assertEqual(git('cat-file', '-p', 'refs/asf/brief:setup', cwd=other), 'pnpm install')
        self.assertIn('VITEST_MAX_WORKERS=2', git('cat-file', '-p', 'refs/asf/brief:env', cwd=other))
        self.assertTrue(lifecycle.pid_alive(res.pid))
        self.assertIsNone(self.runtime(fake).continue_run(self.job()))

    def test_a_run_not_listed_yet_keeps_the_session_token(self):
        res = self.runtime(FakeGh(runs=[])).run(self.job())
        self.assertEqual(res.pid, 'actions:sid-1')
        self.assertIsNone(res.extra['actions_run_id'])
        self.assertEqual(res.extra['actions_run_name'], 'asf task-t-0001 sid-1')

    def test_a_refused_dispatch_refuses_the_launch_and_clears_the_ref(self):
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.runtime(FakeGh(dispatch_ok=False)).run(self.job())
        self.assertIn('HTTP 404', str(cm.exception))
        self.assertEqual(git('ls-remote', 'origin', 'refs/asf/briefs/*', cwd=self.repo), '')


class FakeCloudRuntime(runtime_mod.Runtime):
    """The cloud lane with no dispatch: a token pid and a run id, as the real one records."""
    name = cloud.RUNTIME
    lane = 'cloud'

    def __init__(self):
        self.jobs = []

    def run(self, job, wait=False):
        self.jobs.append(job)
        log_path = job.log_path or runtime_mod.job_log_path(job.product, job.name)
        open(log_path, 'a').close()
        r = runtime_mod.Result(pid=cloudpid.token('500'), log_path=log_path)
        r.extra = {'runtime_lane': 'cloud', 'actions_run_id': '500',
                   'actions_run_name': f'asf {job.name} {job.session}',
                   'cloud_url': f'https://example.test/runs/{job.name}'}
        return r


class Lanes(Home):
    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.cfg = dict(self.cfg, cloud=dict(ON))
        self.cfg['worker_pool'] = dict(self.cfg['worker_pool'], accounts=[
            {'name': 'acct-a', 'role': 'local', 'cap': 1},
            {'name': 'acct-c', 'role': 'cloud', 'cap': 1}])

    def run_wave(self, rows, live=(), local_hold='', cfg=None, ready=(True, '')):
        accounts = pool_mod.accounts_from_config(cfg or self.cfg)
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource({}), live=live)
        self.crt = FakeCloudRuntime()
        lines = []
        launched, waits = wave_mod.wave(
            self.product, rows, 5, pool=pool,
            runtime=runtime_mod.FakeRuntime([{'running': True}] * 5), cfg=cfg or self.cfg,
            out=lines.append, local_hold=local_hold, cloud_runtime=self.crt, cloud_ready=ready)
        return launched, waits, lines


class Placement(Lanes):
    def test_host_pressure_sends_the_row_to_the_cloud(self):
        launched, waits, lines = self.run_wave([feature_row('spec-1')],
                                               local_hold='host pressure load 50/cores 10')
        self.assertEqual(waits, [])
        (row, rec), = launched
        self.assertEqual((rec['account'], rec['runtime_lane'], rec['pid']),
                         ('acct-c', 'cloud', 'actions:500'))
        self.assertIn('→ acct-c (opus) cloud https://example.test/runs/spec-1', lines[0])
        # the fresh branch is on origin before the job checks it out, and nothing was set up
        # on this host
        self.assertTrue(git('ls-remote', '--heads', 'origin', rec['branch'], cwd=self.repo))
        self.assertEqual(self.crt.jobs[0].branch, rec['branch'])
        self.assertNotIn('setup_s', rec)

    def test_the_default_lane_runtime_is_actions(self):
        rt = cloud.lane_runtime(cloud.settings(self.cfg, self.product), self.product)
        self.assertIsInstance(rt, actions.ActionsRuntime)

    def test_a_free_local_seat_keeps_the_row_local(self):
        launched, _waits, _lines = self.run_wave([feature_row('spec-1')])
        self.assertEqual(launched[0][1]['account'], 'acct-a')
        self.assertNotEqual(launched[0][1].get('runtime_lane'), 'cloud')

    def test_a_full_local_lane_overflows_to_the_cloud_and_the_cloud_cap_holds(self):
        live = [{'job': 'x', 'account': 'acct-a'},                     # the local seat taken
                {'job': 'c1', 'account': 'acct-c', 'runtime_lane': 'cloud'}]  # one of two cloud seats
        rows = [feature_row('spec-1'), feature_row('spec-2', item='F-0002')]
        launched, waits, _lines = self.run_wave(rows, live=live)
        self.assertEqual([(r.job, rec['runtime_lane']) for r, rec in launched], [('spec-1', 'cloud')])
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
        self.assertEqual(launched[0][1]['runtime_lane'], 'cloud')

    def test_the_lane_off_changes_nothing(self):
        cfg = dict(self.cfg, cloud={'enabled': False})
        live = [{'job': 'x', 'account': 'acct-a'}, {'job': 'y', 'account': 'acct-c'}]
        launched, waits, _ = self.run_wave([feature_row('spec-1')], live=live, cfg=cfg)
        self.assertEqual((launched, waits[0][1]), ([], 'pool full'))


class DefaultPlacement(Lanes):
    """``cloud.default: true``: an eligible row goes to the cloud first, local after."""

    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, cloud=dict(ON, default=True, rows='cloud-ok'))

    def lanes(self, launched):
        return [(r.job, rec.get('runtime_lane') or 'local') for r, rec in launched]

    def test_settings_read_the_keys(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1}})
        self.assertEqual((s.default, s.local_only), (False, ()))
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'default': True,
                                      'local_only': ['groom', 'review']}})
        self.assertEqual((s.default, s.local_only), (True, ('groom', 'review')))
        self.assertEqual(cloud.settings({'cloud': {'local_only': 'groom'}}).local_only,
                         ('groom',))

    def test_default_on_sends_an_eligible_row_to_the_cloud_first(self):
        # a free local seat is there, and the row still goes to the cloud
        launched, waits, lines = self.run_wave([feature_row('spec-1')])
        self.assertEqual((self.lanes(launched), waits), ([('spec-1', 'cloud')], []))
        self.assertIn('→ acct-c (opus) cloud', lines[0])

    def test_a_kind_off_the_list_stays_local(self):
        row = pool_mod.Row('close-f-0001', 'F-0001', model='Opus', kind='close')
        launched, _waits, _ = self.run_wave([row])
        self.assertEqual(self.lanes(launched), [('close-f-0001', 'local')])
        # rows: any takes every kind first
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], rows='any'))
        row = pool_mod.Row('close-f-0002', 'F-0002', model='Opus', kind='close')
        launched, _waits, _ = self.run_wave([row], cfg=cfg)
        self.assertEqual(self.lanes(launched), [('close-f-0002', 'cloud')])

    def test_local_only_kinds_and_items_stay_local(self):
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], local_only=['spec']))
        launched, _waits, _ = self.run_wave([feature_row('spec-1')], cfg=cfg)
        self.assertEqual(self.lanes(launched), [('spec-1', 'local')])
        row = feature_row('spec-2', item='F-0002')
        row.local_only = True
        launched, _waits, _ = self.run_wave([row])
        self.assertEqual(self.lanes(launched), [('spec-2', 'local')])

    def test_local_only_never_overflows_either(self):
        cfg = dict(self.cfg, cloud=dict(ON, rows='any'))   # overflow mode
        row = feature_row('spec-1')
        row.local_only = True
        live = [{'job': 'x', 'account': 'acct-a'}]
        launched, waits, _ = self.run_wave([row], live=live, cfg=cfg)
        self.assertEqual((launched, waits[0][1]), ([], 'pool full'))

    def test_a_full_cloud_lane_falls_back_to_local(self):
        live = [{'job': 'c1', 'account': 'acct-c', 'runtime_lane': 'cloud'},
                {'job': 'c2', 'account': 'acct-c', 'runtime_lane': 'cloud'}]
        launched, waits, _ = self.run_wave([feature_row('spec-1')], live=live)
        self.assertEqual((self.lanes(launched), waits), ([('spec-1', 'local')], []))

    def test_cloud_first_up_to_max_inflight_then_local(self):
        rows = [feature_row(f'spec-{n}', item=f'F-000{n}') for n in (1, 2, 3)]
        launched, _waits, _ = self.run_wave(rows)
        self.assertEqual(self.lanes(launched),
                         [('spec-1', 'cloud'), ('spec-2', 'cloud'), ('spec-3', 'local')])

    def test_an_unready_lane_falls_back_to_local_with_one_line_why(self):
        rows = [feature_row('spec-1'), feature_row('spec-2', item='F-0002')]
        launched, _waits, lines = self.run_wave(
            rows, ready=(False, 'repo secret CLAUDE_CODE_OAUTH_TOKEN is missing on o/r'))
        self.assertEqual(self.lanes(launched), [('spec-1', 'local')])
        why = [l for l in lines if l.startswith('cloud lane unready')]
        self.assertEqual(why, ['cloud lane unready: repo secret CLAUDE_CODE_OAUTH_TOKEN is '
                               'missing on o/r — local lane only'])

    def test_host_pressure_holds_local_rows_but_not_cloud_rows(self):
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], local_only=['close']))
        close = pool_mod.Row('close-f-0009', 'F-0009', model='Opus', kind='close')
        rows = [feature_row('spec-1'), close]
        launched, waits, _ = self.run_wave(rows, local_hold='host pressure load 50/cores 10',
                                           cfg=cfg)
        self.assertEqual(self.lanes(launched), [('spec-1', 'cloud')])
        self.assertEqual([(r.job, why) for r, why in waits],
                         [('close-f-0009', 'held: host pressure load 50/cores 10')])

    def test_readiness_is_read_once_per_wave_when_not_given(self):
        calls = []

        def ready(cfg, product, run_cmd=None):
            calls.append(product.name)
            return True, ''
        rows = [feature_row(f'spec-{n}', item=f'F-000{n}') for n in (1, 2, 3)]
        with mock.patch.object(cloud, 'readiness', ready):
            launched, _waits, _ = self.run_wave(rows, ready=None)
        self.assertEqual(calls, ['sample'])
        self.assertEqual(len(launched), 3)


class StepWaveLanes(unittest.TestCase):
    """The wave step's split of the host hold: a ready lane takes the hold off cloud rows only;
    an unready (or off) lane leaves the hold on every row and adds no seats."""

    def test_the_split(self):
        from asf.tick import step_wave
        s = cloud.settings({'cloud': dict(ON, default=True)})
        self.assertEqual(step_wave.split_hold(s, (True, ''), True, 'host pressure x'),
                         (False, 'host pressure x', 2))
        self.assertEqual(step_wave.split_hold(s, (False, 'no secret'), True, 'host pressure x'),
                         (True, '', 0))
        self.assertEqual(step_wave.split_hold(s, (True, ''), False, ''), (False, '', 2))
        off = cloud.settings({})
        self.assertEqual(step_wave.split_hold(off, (False, 'off'), True, 'h'), (True, '', 0))


class Readiness(unittest.TestCase):
    def product(self):
        return env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})

    def cfg(self, **over):
        return {'cloud': dict(ON, default=True, **over),
                'worker_pool': {'accounts': [{'name': 'acct-c', 'role': 'cloud'}]}}

    def test_a_sound_lane_is_ready(self):
        self.assertEqual(cloud.readiness(self.cfg(), self.product(), FakeGh()), (True, ''))

    def test_a_critical_gap_is_unready_and_says_why(self):
        ok, why = cloud.readiness(self.cfg(), self.product(), FakeGh(secrets=('OTHER',)))
        self.assertFalse(ok)
        self.assertIn('repo secret CLAUDE_CODE_OAUTH_TOKEN is missing on o/r', why)
        ok, why = cloud.readiness(self.cfg(), self.product(), FakeGh(workflow=False))
        self.assertFalse(ok)
        self.assertIn('asf-worker.yml is not on main of o/r', why)

    def test_an_unreadable_secret_list_is_not_critical(self):
        self.assertEqual(cloud.readiness(self.cfg(), self.product(), FakeGh(secrets=None)),
                         (True, ''))

    def test_off_is_unready(self):
        self.assertEqual(cloud.readiness({}, self.product(), FakeGh())[0], False)


class DoctorCommand(unittest.TestCase):
    def product(self):
        return env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})

    def cfg(self, **over):
        return {'cloud': dict(dict(ON, default=True), **over),
                'worker_pool': {'accounts': [{'name': 'acct-c', 'role': 'cloud'}]}}

    def run_doctor(self, cfg, fake):
        lines = []
        rc = cloud.doctor(cfg, self.product(), run_cmd=fake, out=lines.append)
        return rc, lines

    def test_a_ready_lane_is_all_ok(self):
        fake = FakeGh(runners=[{'name': 'r1', 'status': 'online',
                                'labels': [{'name': 'self-hosted'}, {'name': 'linux'}]}])
        rc, lines = self.run_doctor(self.cfg(runs_on=['self-hosted', 'linux']), fake)
        self.assertEqual(rc, 0, lines)
        body = [l for l in lines if l.startswith(('ok ', 'gap '))]
        self.assertTrue(body and all(l.startswith('ok ') for l in body), lines)
        text = '\n'.join(lines)
        for want in ('cloud.enabled', 'cloud.default', 'repo secret CLAUDE_CODE_OAUTH_TOKEN',
                     '.github/workflows/asf-worker.yml on main of o/r', 'online runner r1',
                     'acct-c', 'ready: yes'):
            self.assertIn(want, text)
        # names only: the secret list is asked for names, never values
        (secret_call,) = fake.named('secret', 'list')
        self.assertEqual(secret_call[-2:], ['--json', 'name'])

    def test_gaps_are_named_and_the_lane_is_unready(self):
        fake = FakeGh(secrets=('OTHER',), workflow=False, runners=[
            {'name': 'r1', 'status': 'offline', 'labels': [{'name': 'self-hosted'}]}])
        rc, lines = self.run_doctor(self.cfg(runs_on=['self-hosted'], default=False), fake)
        self.assertEqual(rc, 1)
        text = '\n'.join(l for l in lines if l.startswith('gap '))
        for want in ('cloud.default', 'repo secret CLAUDE_CODE_OAUTH_TOKEN is missing',
                     'asf-worker.yml is not on main of o/r', 'no online runner carries'):
            self.assertIn(want, text)
        self.assertTrue(lines[-1].startswith('ready: no'), lines)

    def test_off_says_so(self):
        rc, lines = self.run_doctor({}, FakeGh())
        self.assertEqual(rc, 1)
        self.assertTrue(any(l.startswith('gap ') and 'cloud.enabled' in l for l in lines))

    def test_the_cli_registers_the_subcommand(self):
        from asf import cli
        args = cli.build_parser().parse_args(['cloud', 'doctor', '--product', 'sample'])
        self.assertEqual((args.cloud_command, args.product), ('doctor', 'sample'))


class Sync(Placement):
    def launch(self):
        launched, _, _ = self.run_wave([feature_row('spec-1')], local_hold='host pressure')
        rec = launched[0][1]
        # the brief ref the real runtime leaves on origin
        tree = subprocess.run(['git', 'mktree'], cwd=self.repo, input='', capture_output=True,
                              text=True, check=True).stdout.strip()
        git('push', '-q', 'origin', f'+{tree}:refs/asf/briefs/spec-1', cwd=self.repo)
        pool_mod.update_session(self.product, 'spec-1', brief_ref='refs/asf/briefs/spec-1')
        return rec

    def sync(self, fake, **kw):
        lines = kw.pop('lines', [])
        return cloud.sync(self.product, self.cfg, gh=actions.Gh(self.product, run=fake),
                          out=lines.append, **kw)

    def push_as_the_job(self, rec, trailers=True):
        other = os.path.join(self.tmp, 'ci-checkout')
        git('clone', '-q', '-b', rec['branch'], os.path.join(self.tmp, 'origin.git'), other,
            cwd=self.tmp)
        for k, v in (('user.email', 'c@example.com'), ('user.name', 'c')):
            git('config', k, v, cwd=other)
        with open(os.path.join(other, 'work.txt'), 'w') as f:
            f.write('work\n')
        git('add', '.', cwd=other)
        msg = 'asf: report spec-1\n\nREPORT\nitem: F-0001\npushed: yes'
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
        fake = FakeGh()
        self.assertEqual(self.sync(fake, lines=lines)[0][1], cloud.WORKING)
        self.assertTrue(lifecycle.pid_alive(rec['pid']))
        self.assertEqual(fake.named('run', 'view')[0][3], '500')
        tip = self.push_as_the_job(rec)
        fake.view_ = {'status': 'completed', 'conclusion': 'success'}
        (job, status, why), = self.sync(fake, lines=lines)
        self.assertEqual((job, status), ('spec-1', cloud.FINISHED), why)
        self.assertFalse(lifecycle.pid_alive(rec['pid']))
        result = runtime_mod.read_result(rec['log'])
        self.assertTrue(runtime_mod.result_ok(result))
        self.assertIn('item: F-0001', result['result'])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=rec['worktree']), tip)  # caught up
        ev = lifecycle.gather(self.product, pool_mod.load_sessions(self.product)['spec-1'])
        self.assertEqual(lifecycle.judge(rec, ev), lifecycle.FINISHED)
        self.assertIn('cloud    spec-1', lines[-1])
        self.assertEqual(git('ls-remote', 'origin', 'refs/asf/briefs/*', cwd=self.repo), '')

    def test_a_run_that_ended_without_the_marker_is_dead(self):
        rec = self.launch()
        self.push_as_the_job(rec, trailers=False)
        fake = FakeGh()
        self.assertEqual(self.sync(fake)[0][1], cloud.WORKING)
        fake.view_ = {'status': 'completed', 'conclusion': 'failure'}
        (_job, status, why), = self.sync(fake)
        self.assertEqual((status, why),
                         (cloud.DEAD, 'run 500 ended failure without the report commit'))
        self.assertFalse(lifecycle.pid_alive(rec['pid']))

    def test_a_run_past_its_limit_is_cancelled_and_dead(self):
        rec = self.launch()
        fake = FakeGh()
        (_job, status, why), = self.sync(fake, now=time.time() + 5 * 3600)
        self.assertEqual((status, why), (cloud.DEAD, 'timed out after 240m'))
        (cancel,) = fake.named('run', 'cancel')
        self.assertEqual(cancel[3:6], ['500', '-R', 'o/r'])
        self.assertFalse(lifecycle.pid_alive(rec['pid']))
        run = pool_mod.load_sessions(self.product)['spec-1']
        self.assertEqual(lifecycle.judge(run, lifecycle.gather(self.product, run)),
                         lifecycle.DEAD_PID)

    def test_the_run_id_is_found_later(self):
        self.launch()
        pool_mod.update_session(self.product, 'spec-1', actions_run_id=None)
        fake = FakeGh(runs=[{'databaseId': 501, 'displayTitle': 'nope'}])
        self.assertEqual(self.sync(fake)[0][2], 'dispatched')
        run = pool_mod.load_sessions(self.product)['spec-1']
        fake.runs = [{'databaseId': 501, 'displayTitle': run['actions_run_name'],
                      'url': 'https://example.test/runs/501'}]
        self.assertEqual(self.sync(fake)[0][2], 'run 501 in_progress')
        self.assertEqual(pool_mod.load_sessions(self.product)['spec-1']['actions_run_id'], '501')


class Doctor(unittest.TestCase):
    def product(self, slug='o/r'):
        return env.Product('sample', {'repo_slug': slug, 'main': 'main'})

    def cfg(self, **over):
        return {'cloud': dict(ON, **over),
                'worker_pool': {'accounts': [{'name': 'acct-c', 'role': 'cloud'}]}}

    def test_no_rows_while_off(self):
        self.assertEqual(cloud.doctor_rows({}, self.product(), FakeGh()), [])

    def test_a_sound_lane_is_one_green_row(self):
        rows = cloud.doctor_rows(self.cfg(), self.product(), FakeGh())
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][:2], (False, True))
        self.assertIn('cloud lane on: 2 seat(s), runtime actions on [ubuntu-latest]', rows[0][2])

    def test_each_gap_is_a_red_row(self):
        cfg = self.cfg(accounts=['nobody'], max_inflight=0, runs_on=['self-hosted', 'gpu'])
        fake = FakeGh(secrets=('OTHER',), workflow=False, runners=[
            {'name': 'r1', 'status': 'offline', 'labels': [{'name': 'self-hosted'}, {'name': 'gpu'}]},
            {'name': 'r2', 'status': 'online', 'labels': [{'name': 'self-hosted'}]}])
        rows = cloud.doctor_rows(cfg, self.product(), fake)
        text = '\n'.join(d for _r, ok, d in rows if not ok)
        for want in ('cloud.max_inflight is 0', 'no cloud-lane account',
                     '.github/workflows/asf-worker.yml is not on main of o/r',
                     'asf cloud install --product sample',
                     'repo secret CLAUDE_CODE_OAUTH_TOKEN is missing',
                     'no online runner carries [self-hosted, gpu]'):
            self.assertIn(want, text)
        self.assertTrue(all(required for required, ok, _d in rows if not ok))
        self.assertFalse(any(ok for _r, ok, _d in rows))

    def test_an_online_runner_with_the_labels_and_unreadable_secrets(self):
        cfg = self.cfg(runs_on=['self-hosted', 'linux'])
        fake = FakeGh(secrets=None, runners=[
            {'name': 'r1', 'status': 'online',
             'labels': [{'name': 'self-hosted'}, {'name': 'Linux'}, {'name': 'X64'}]}])
        rows = cloud.doctor_rows(cfg, self.product(), fake)
        (required, ok, detail), = rows
        self.assertEqual((required, ok), (False, False))
        self.assertIn('cannot list the secrets', detail)

    def test_no_repo_slug(self):
        rows = cloud.doctor_rows(self.cfg(), self.product(slug=None), FakeGh())
        self.assertIn('no repo_slug', rows[-1][2])

    def test_the_doctor_table_carries_the_rows(self):
        from asf import doctor
        rows = doctor.check_cloud(self.cfg(runtime='nope'), self.product(slug=None))
        self.assertIn("cloud.runtime 'nope' is not one ASF runs", rows[0][2])


if __name__ == '__main__':
    unittest.main()
