"""asf.workers.cloud and asf.workers.actions — the cloud lane: claude-cloud refused at config
check, the actions runtime (brief ref, dispatch, run lookup), the workflow template and its
install, the status mapping, the pid token every liveness check reads, placement under host
pressure, the sync of a finished, a failed and a timed-out run, and the doctor rows. Nothing is
dispatched: gh is a fake, git is a bare repo in a temp dir."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asf import env, gh_limit
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

import asf.briefs.build  # noqa: E402,F401 — the module; the package exports a build() function
build_mod = sys.modules['asf.briefs.build']

try:
    from tests import contracts
except ImportError:  # pragma: no cover - `discover -s tests`
    import contracts
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
        if a[0] == 'api' and a[-2:] == ['--jq', '.status']:
            return done(self.view_['status'] + '\n')
        if a[:3] == ['api', '-X', 'POST'] and a[3].endswith('/force-cancel'):
            self.view_ = {'status': 'completed', 'conclusion': 'cancelled'}
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


class GhUnknown(unittest.TestCase):
    """:class:`actions.Gh` through asf.github: an unread answer is ``ok`` false, never data."""

    def gh(self, effect):
        product = env.Product('p', {'repo_slug': 'o/r'})
        return actions.Gh(product, run=effect)

    def test_a_failure_or_a_timeout_reads_nothing(self):
        def failed(argv, **_kw):
            return subprocess.CompletedProcess(argv, 1, '[{"databaseId": 1}]', 'HTTP 502\n')

        def timed_out(argv, **_kw):
            raise subprocess.TimeoutExpired('gh', actions.GH_TIMEOUT_S)
        with mock.patch('asf.ci_pool._gh_env', return_value={}):
            self.assertEqual(self.gh(failed).call(['run', 'list']), (False, '', 'gh run: HTTP 502'))
            ok, out, err = self.gh(timed_out).call(['run', 'list'])
            self.assertEqual((ok, out), (False, ''))
            self.assertIn('timeout', err)
            self.assertIsNone(self.gh(failed).view(7))
            self.assertIsNone(self.gh(failed).secret_names())

    def test_a_dry_run_refuses_a_dispatch_without_spawning(self):
        from asf import mutation_guard
        spawned = []

        def run(argv, **_kw):
            spawned.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch('asf.ci_pool._gh_env', return_value={}), \
                mock.patch.object(mutation_guard, 'is_active', return_value=True), \
                mock.patch('builtins.print'):
            ok, _err = self.gh(run).dispatch('asf-worker.yml', 'main', {'job': 'j'})
        self.assertFalse(ok)
        self.assertEqual(spawned, [])


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

    def test_f0278_the_report_subject_names_the_item(self):
        # a cloud session runs no commit-msg hook: the unnamed `asf: report <job>` was refused
        # for naming and looped the lane's corrections (a product's T-0659, 2026-10-07)
        text = cloud.cloud_brief('Do the task.\n', job())
        self.assertIn('the subject `asf(T-0001): report task-t-0001`', text)
        self.assertNotIn('`asf: report', text)
        j = job(env={'ASF_SESSION': 'sid-1', 'ASF_ITEM': 'F-0042'})
        self.assertIn('`asf(F-0042): report task-t-0001`', cloud.cloud_brief('x', j))
        self.assertIn('`asf: report sweep`', cloud.cloud_brief('x', job(name='sweep', env={})))

    def test_f0278_both_report_subjects_peel_as_reports(self):
        # the older unnamed subject is still read as a report, beside the named one
        d = tempfile.mkdtemp(prefix='peel_')
        self.addCleanup(__import__('shutil').rmtree, d, ignore_errors=True)
        ident = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                     GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')

        def g(*args):
            return subprocess.run(['git', *args], cwd=d, capture_output=True, text=True,
                                  env=ident).stdout.strip()
        g('init', '-q', '-b', 'main')
        with open(os.path.join(d, 'a.txt'), 'w') as f:
            f.write('a\n')
        g('add', '-A')
        g('commit', '-qm', 'fix(T-0001): the hinge')
        work = g('rev-parse', 'HEAD')
        g('commit', '-q', '--allow-empty', '-m', 'asf: report coder-t-0001')
        g('commit', '-q', '--allow-empty', '-m', 'asf(T-0001): report coder-t-0001')
        self.assertEqual(spawn_mod._past_reports(d, g('rev-parse', 'HEAD')), work)
        for s in ('asf: report x', 'asf(T-1): report x', 'plan(F-1): asf(F-1): report x'):
            self.assertTrue(spawn_mod.REPORT_SUBJECT.match(s), s)
        self.assertFalse(spawn_mod.REPORT_SUBJECT.match('asf(F-1): reports page'))

    def test_the_brief_runs_the_products_pre_push_check_before_every_push(self):
        # a cloud job has no pre-push hook: the repo checks the host's hook runs must run here
        # (2026-10-05: a forbidden name in a cloud-committed review file, six PR reds a day)
        from asf import env
        p = env.Product('p', {'conventions': {'pre_push_check': 'make fast'}})
        text = cloud.cloud_brief('Do the task.\n', job(), product=p)
        self.assertIn("No pre-push hook runs here", text)
        self.assertIn("pre-push check `make fast`", text)
        self.assertNotIn('No pre-push hook', cloud.cloud_brief('x', job(), product=env.Product(
            'p', {})))


class LocalKinds(unittest.TestCase):
    """:data:`cloud.LOCAL_KINDS` — the closed exception list, and the placement rule it seeds
    (F-0216 §1). Pure: no ``gh``, no clock, no home."""

    def test_the_exception_list_is_three_kinds_and_every_one_is_a_kind(self):
        self.assertEqual(cloud.LOCAL_KINDS, ('groom', 'groom-clerk', 'close'))
        self.assertTrue(set(cloud.LOCAL_KINDS) <= set(build_mod.KINDS))

    def test_every_other_kind_goes_to_the_cloud(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'default': True}})
        for k in build_mod.KINDS:
            self.assertEqual(cloud.first(pool_mod.Row('j', 'F-0001', kind=k), s),
                             k not in cloud.LOCAL_KINDS, k)

    def test_an_alias_is_never_a_row_kind(self):
        for alias in build_mod.KIND_ALIASES:
            self.assertIn(build_mod.normalize_kind(alias), build_mod.KINDS)
            self.assertNotIn(alias, cloud.LOCAL_KINDS)

    def test_the_floor_holds_in_the_overflow_path_too(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'rows': 'any'}})
        self.assertFalse(cloud.eligible(pool_mod.Row('j', 'F-0001', kind='groom'), s))

    def test_cloud_local_only_adds_and_never_removes(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1,
                                      'local_only': ['review']}})
        self.assertTrue(cloud.local_only(pool_mod.Row('j', 'F-0001', kind='review'), s))
        self.assertTrue(cloud.local_only(pool_mod.Row('j', 'F-0001', kind='groom'), s))
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'local_only': []}})
        self.assertTrue(cloud.local_only(pool_mod.Row('j', 'F-0001', kind='groom'), s))

    def test_an_item_marked_local_only_never_leaves(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'default': True}})
        row = pool_mod.Row('j', 'F-0001', kind='coder', local_only=True)
        self.assertFalse(cloud.first(row, s))


class ACorrectionThatRebasesNeverLeavesTheHost(unittest.TestCase):
    """:data:`cloud.REWRITE_KINDS` — a correction whose answer rewrites the branch's history
    stays off both lane doors, under the cloud lane as the default executor and on the
    overflow path alike, and every other correction kind still goes to the cloud (F-0289
    S-76104). Pure: no ``gh``, no clock, no home."""

    def test_a_rewrite_kind_correction_stays_off_both_doors(self):
        primary = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'default': True}})
        overflow_any = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1,
                                                 'rows': 'any'}})
        for k in cloud.REWRITE_KINDS:
            row = pool_mod.Row('correct-t-0064', 'T-0064', kind='correct', correction_kind=k)
            for s in (primary, overflow_any):
                self.assertTrue(cloud.local_only(row, s), (k, s.mode))
            self.assertFalse(cloud.first(row, primary), k)
            self.assertFalse(cloud.eligible(row, overflow_any), k)

    def test_every_other_correction_kind_still_goes_to_the_cloud(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'rows': 'any'}})
        for k in ('merge', 'landing-gate', 'incomplete', 'footprint', ''):
            row = pool_mod.Row('correct-t-0064', 'T-0064', kind='correct', correction_kind=k)
            self.assertFalse(cloud.local_only(row, s), k)

    def test_the_list_is_three_kinds_and_a_subset_of_the_refusal_kinds(self):
        from asf.workers import refusals
        self.assertEqual(cloud.REWRITE_KINDS, ('conflict', 'copies', 'naming'))
        self.assertTrue(set(cloud.REWRITE_KINDS) <= set(refusals.CORRECTION_KINDS))
        self.assertIn('merge', refusals.CORRECTION_KINDS)
        self.assertNotIn('merge', cloud.REWRITE_KINDS)

    def test_local_only_and_correction_kind_are_two_reasons_not_one(self):
        s = cloud.settings({'cloud': {'enabled': True, 'max_inflight': 1, 'default': True}})
        # local_only: true with no rewrite-kind correction still stays home — its own field,
        # its own reason (F-0289 C3)
        marked = pool_mod.Row('j', 'F-0001', kind='coder', local_only=True)
        self.assertTrue(cloud.local_only(marked, s))
        self.assertFalse(marked.correction_kind)
        # a rewrite-kind correction with no local_only mark stays home too — the other reason,
        # not folded into the first
        rewrite = pool_mod.Row('j', 'F-0001', kind='coder', correction_kind='conflict')
        self.assertTrue(cloud.local_only(rewrite, s))
        self.assertFalse(rewrite.local_only)

    def test_the_fact_is_carried_end_to_end(self):
        import types
        from asf.feeder import rows as feeder_rows
        from asf.tick import step_wave
        frow = feeder_rows.Row(0, 'BUG → FIX', 'B-0001', '', 'LAUNCH', 'fix-bug', 'fix/B-0001', '',
                               correction_kind='conflict')
        brief = types.SimpleNamespace(kind='fix-bug', model='Opus', add_dirs=[], card_digest='')
        self.assertEqual(step_wave.worker_row(frow, brief, {}).correction_kind, 'conflict')
        self.assertEqual(
            pool_mod.Row.from_dict({'job': 'x', 'item': 'F-0001',
                                   'correction_kind': 'conflict'}).correction_kind, 'conflict')

    def test_correction_rows_sets_the_kind_from_the_hold(self):
        from asf.feeder import rows as feeder_rows
        product = env.Product('p', {'conventions': {}})
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active'}}

        plain = {'kind': 'conflict', 'text': 'x', 'rounds': 1, 'branch': 'fix/T-0001'}
        got, _ids = feeder_rows.correction_rows(items, product, set(), {'T-0001': plain})
        self.assertEqual([r.correction_kind for r in got], ['conflict'])

        parked = {'kind': 'conflict', 'text': 'x', 'parked': True, 'reason': 'needs operator'}
        got, _ids = feeder_rows.correction_rows(items, product, set(), {'T-0001': parked})
        self.assertEqual([r.correction_kind for r in got], ['conflict'])

        waits_on_merge = {'kind': 'conflict', 'text': 'x', 'rounds': 3, 'same': 3,
                          'settled': True, 'branch': 'fix/T-0001'}
        got, _ids = feeder_rows.correction_rows(items, product, set(), {'T-0001': waits_on_merge})
        self.assertEqual([r.correction_kind for r in got], ['conflict'])

    def test_the_two_operator_strings_name_the_new_floor(self):
        product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})
        line = cloud.lane_split({'cloud': dict(ON, default=True)}, product)
        self.assertIn(f'corrections that rebase ({", ".join(cloud.REWRITE_KINDS)})', line)
        rows = cloud.checks({'cloud': dict(ON, default=True),
                             'worker_pool': {'accounts': [{'name': 'acct-c', 'role': 'cloud'}]}},
                            product, FakeGh())
        _name, _req, _ok, detail = next(r for r in rows if r[0] == 'default')
        self.assertIn('corrections that rebase', detail)


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


class ClassifyNamesTheRefusal(unittest.TestCase):
    """F-0266 S-64357: ``classify(refusal=…)`` appends the clause to the "without the report
    commit" arm only, and stays pure."""

    def test_the_clause_is_on_the_one_arm(self):
        from asf.workers import refusals
        done = {'status': 'completed', 'conclusion': 'succeeded'}
        running = {'status': 'in_progress', 'conclusion': ''}
        at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 200))
        r = refusals.Refusal('naming', 'commits do not name T-44931: every commit subject',
                             at, 'correction')
        with mock.patch.object(env, 'ASF_HOME', '/nonexistent/asf-home'), \
                mock.patch('builtins.open', side_effect=AssertionError('classify read a file')):
            _s, bare = cloud.classify(done, None, 5, 240, '500')
            _s, why = cloud.classify(done, None, 5, 240, '500', refusal=r)
            timed = cloud.classify(running, None, 300, 240, '500', refusal=r)[1]
            never = cloud.classify(None, None, 20, 240, None, refusal=r)[1]
        self.assertEqual(bare, 'run 500 ended succeeded without the report commit')
        self.assertEqual(why, 'run 500 ended succeeded without the report commit — last ASF '
                              'refusal (naming, 3m before): commits do not name T-44931: every '
                              'commit subject')
        self.assertEqual(timed, 'timed out after 240m')
        self.assertEqual(never, 'the dispatched run never appeared')
        self.assertEqual(cloud.REPORT_MISSING, 'without the report commit')


class GhLogTail(unittest.TestCase):
    """F-0266 C11: ``Gh.log_tail`` reads ``--log-failed``, falls back to ``--log``, and answers
    '' for a failed call or a rate limit."""

    def gh(self, answers):
        calls = []

        def run(argv, **_kw):
            calls.append(argv)
            flag = argv[-1]
            rc, out = answers.get(flag, (1, ''))
            if rc == 'limit':
                return subprocess.CompletedProcess(argv, 1, stdout='',
                                                   stderr='API rate limit exceeded')
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr='' if rc == 0 else 'x')
        return actions.Gh(env.Product('p', {'repo_slug': 'o/r'}), run=run), calls

    def test_failed_first_then_the_whole_log(self):
        g, calls = self.gh({'--log-failed': (0, 'x' * 10 + 'TAIL')})
        self.assertEqual(g.log_tail('7', limit=4), 'TAIL')
        self.assertEqual(calls[0][1:], ['run', 'view', '7', '-R', 'o/r', '--log-failed'])
        g, calls = self.gh({'--log-failed': (0, ''), '--log': (0, 'whole log')})
        self.assertEqual(g.log_tail('7'), 'whole log')
        self.assertEqual([c[-1] for c in calls], ['--log-failed', '--log'])
        g, _ = self.gh({})
        self.assertEqual(g.log_tail('7'), '')
        g, _ = self.gh({'--log-failed': ('limit', ''), '--log': ('limit', '')})
        with mock.patch.object(actions.Gh, 'call', side_effect=gh_limit.RateLimited('x')):
            self.assertEqual(g.log_tail('7'), '')
        self.assertEqual(actions.LOG_TAIL_MAX, 64 * 1024)


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

    def run_wave(self, rows, live=(), local_hold='', cfg=None, ready=(True, ''), **kw):
        accounts = pool_mod.accounts_from_config(cfg or self.cfg)
        accounts_by_name = {a.name: a for a in accounts}
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource({}), live=live)
        self.crt = FakeCloudRuntime()
        lines = []
        launched, waits = wave_mod.wave(
            self.product, rows, 5, pool=pool,
            runtime=runtime_mod.FakeRuntime([{'running': True}] * 5), cfg=cfg or self.cfg,
            out=lines.append, local_hold=local_hold, cloud_runtime=self.crt, cloud_ready=ready,
            **kw)
        # each launched row's claim is released here, not left for the next ``run_wave`` call in
        # the same test to trip over (C1): a same-pid claim survives a pool rebuild on purpose
        # (PD2), so two placements in one test need their own free seats, not a stale one of ours.
        for row, rec in launched:
            lane = rec.get('runtime_lane') if rec.get('runtime_lane') == 'cloud' else None
            pool.untake(accounts_by_name[rec['account']], rec['model'], job=row.job,
                        product=self.product.name, kind=row.kind, lane=lane)
        return launched, waits, lines


class ACorrectionThatRebasesNeverLeavesTheHostInAWave(Lanes):
    """The predicate's effect on a real wave: a conflict correction stays host-side under
    ``cloud.mode: primary`` and waits for a local seat rather than overflowing (F-0289
    S-76104)."""

    def test_a_conflict_correction_stays_local_in_primary_mode(self):
        # the floor holds with a free cloud seat, and a correction-less row goes to the cloud
        # (F-0289 C1, C3 — the shape DefaultPlacement.test_a_groom_row_stays_on_the_host uses)
        cfg = dict(self.cfg, cloud=dict(ON, default=True, rows='cloud-ok'))
        conflict = pool_mod.Row('correct-t-0064', 'T-0064', model='Opus', kind='correct',
                                correction_kind='conflict')
        plain = pool_mod.Row('correct-t-0065', 'T-0065', model='Opus', kind='correct')
        launched, _waits, _ = self.run_wave([conflict, plain], cfg=cfg)
        self.assertEqual([(r.job, rec.get('runtime_lane') or 'local') for r, rec in launched],
                         [('correct-t-0064', 'local'), ('correct-t-0065', 'cloud')])

    def test_a_conflict_correction_waits_for_a_local_seat_rather_than_overflowing(self):
        cfg = dict(self.cfg, cloud=dict(ON, rows='any'))   # overflow mode
        conflict = pool_mod.Row('correct-t-0064', 'T-0064', model='Opus', kind='correct',
                                correction_kind='conflict')
        plain = pool_mod.Row('correct-t-0065', 'T-0065', model='Opus', kind='correct')
        live = [{'job': 'x', 'account': 'acct-a'}]          # no free local seat
        launched, waits, _ = self.run_wave([conflict, plain], live=live, cfg=cfg)
        waits_by_job = dict((r.job, why) for r, why in waits)
        self.assertEqual(waits_by_job.get('correct-t-0064'),
                         'pool full — accounts at cap: acct-a 1/1')
        self.assertNotIn('correct-t-0064',
                         [r.job for r, rec in launched if rec.get('runtime_lane') == 'cloud'])


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
                         [('spec-2', 'pool full — accounts at cap: acct-a 1/1; '
                                     'cloud full — 2/2 in flight')])

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
        self.assertEqual((launched, waits[0][1]),
                         ([], 'pool full — accounts at cap: acct-a 1/1, acct-c 1/1'))


    def test_worker_role_accounts_take_the_local_lane_when_the_cloud_is_on(self):
        """B: every account ``role: worker`` (not ``local``), the cloud lane on in overflow mode
        and holding its only account: a free worker account with room gets the local launch —
        the local lane is every account that is not ``role: cloud``."""
        cfg = dict(self.cfg, cloud=dict(ON, rows='any', accounts=['w2']))
        cfg['worker_pool'] = dict(cfg['worker_pool'], accounts=[
            {'name': 'w1', 'role': 'worker', 'cap': 4},
            {'name': 'w2', 'role': 'worker', 'cap': 4}])
        live = [{'job': 'x', 'account': 'w1'},
                {'job': 'c1', 'account': 'w2', 'runtime_lane': 'cloud'}]
        launched, waits, lines = self.run_wave([feature_row('spec-1')], live=live, cfg=cfg)
        self.assertEqual(waits, [])
        (row, rec), = launched
        self.assertEqual(rec['account'], 'w2')
        self.assertNotEqual(rec.get('runtime_lane'), 'cloud')
        self.assertIn('pid', lines[0])

    def test_the_local_share_bounds_the_local_lane_the_rest_overflows_to_the_cloud(self):
        """2026-09-26, a product: share 9, cloud max_inflight 4 — the step's 13 seats; 4 local +
        2 cloud in flight and the wave launched 6 more rows, all local (local accounts had room):
        10 local sessions on a share of 9 (``sessions 10/9``). The cloud seats are the cloud's:
        the local lane takes at most the share's free local seats, the rest overflow."""
        cfg = dict(self.cfg)
        cfg['worker_pool'] = dict(cfg['worker_pool'], accounts=[
            {'name': 'acct-a', 'role': 'local', 'cap': 5},
            {'name': 'acct-c', 'role': 'cloud', 'cap': 1}])
        rows = [feature_row('spec-1'), feature_row('spec-2', item='F-0002'),
                feature_row('spec-3', item='F-0003'), feature_row('spec-4', item='F-0004')]
        launched, waits, _ = self.run_wave(rows, cfg=cfg, local_seats=1)
        self.assertEqual([(r.job, rec.get('runtime_lane') or 'local') for r, rec in launched],
                         [('spec-1', 'local'), ('spec-2', 'cloud'), ('spec-3', 'cloud')])
        (row, why), = waits                 # the cloud lane's 2 seats taken too
        self.assertEqual(row.job, 'spec-4')
        self.assertTrue(why.startswith('no local seat — the share has 1 free this wave'), why)
        # no bound given: the local accounts take more than the share's one seat, as before
        launched, _waits, _ = self.run_wave(rows, cfg=cfg)
        self.assertGreater(len([1 for _r, rec in launched if not rec.get('runtime_lane')]), 1)

    def test_the_step_gives_the_wave_the_share_less_the_local_sessions_live(self):
        from asf.tick import step_wave
        running = [{'job': 'a', 'pid': 1}, {'job': 'b', 'pid': 2},
                   {'job': 'c', 'pid': cloudpid.token('9')}]
        self.assertEqual(step_wave.local_seats(9, running), 7)
        self.assertEqual(step_wave.local_seats(1, running), 0)

    def test_a_full_local_lane_names_its_accounts_and_caps(self):
        live = [{'job': 'x', 'account': 'acct-a'},
                {'job': 'c1', 'account': 'acct-c', 'runtime_lane': 'cloud'}]
        cfg = dict(self.cfg, cloud=dict(ON, max_inflight=1))
        _launched, waits, _ = self.run_wave([feature_row('spec-1')], live=live, cfg=cfg)
        self.assertEqual(waits[0][1],
                         'pool full — accounts at cap: acct-a 1/1; cloud full — 1/1 in flight')


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

    def test_a_groom_row_stays_on_the_host(self):
        # the floor holds with a free cloud seat, and a kind off the old allow-list —
        # a fixer — goes to the cloud (F-0216 C1, C3)
        groom = pool_mod.Row('groom-f-0001', 'F-0001', model='Opus', kind='groom')
        fixer = pool_mod.Row('fixer-f-0002', 'F-0002', model='Opus', kind='fixer')
        launched, _waits, _ = self.run_wave([groom, fixer])
        self.assertEqual(self.lanes(launched),
                         [('groom-f-0001', 'local'), ('fixer-f-0002', 'cloud')])

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
        self.assertEqual((launched, waits[0][1]), ([], 'pool full — accounts at cap: acct-a 1/1'))

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

    def test_the_extra_seats_are_the_lane_less_what_already_fills_it(self):
        """B-83574: a launchable row sat idle past the watchdog's limit while its wave kept
        reading a seat free — the lane's whole ``max_inflight`` was added beside the share
        however many of its seats another product's cloud runs already held. ``inflight``
        (:func:`asf.workers.cloud.inflight_all`, counted the same way the lane's own cap —
        :meth:`asf.workers.pool.Pool.cloud_load` — counts it) takes those seats off first."""
        from asf.tick import step_wave
        s = cloud.settings({'cloud': dict(ON, max_inflight=2)})
        self.assertEqual(step_wave.split_hold(s, (True, ''), False, '', inflight=1), (False, '', 1))
        self.assertEqual(step_wave.split_hold(s, (True, ''), False, '', inflight=2), (False, '', 0))
        # the lane overcommitted (more live runs than its own cap): never a negative seat count
        self.assertEqual(step_wave.split_hold(s, (True, ''), False, '', inflight=5), (False, '', 0))


class CloudInflightAll(Home):
    """``inflight_all`` (B-83574): the lane's live runs across every product on the host, the
    same scope the lane's own cap reads (:meth:`asf.workers.pool.Pool.cloud_load`) — a
    product's own ledger alone missed a sibling product's cloud runs."""

    def test_it_sums_live_cloud_runs_across_products_and_skips_the_rest(self):
        other = env.Product('two', {'repo_dir': self.repo, 'main': 'main'})
        for product, job, trig in ((self.product, 'coder-a', 'A'), (other, 'coder-b', 'B')):
            tok = f'actions:{trig}'
            pool_mod.append_session(product, {
                'job': job, 'item': 'T-1', 'kind': 'coder', 'account': 'acct-c', 'pid': tok,
                'runtime': 'actions', 'runtime_lane': 'cloud', 'started': '2026-10-08T20:00:00Z'})
            cloudpid.record(tok, cloudpid.WORKING, 'in_progress')
        # ended: holds no seat; local: not the cloud lane — neither counts
        pool_mod.append_session(self.product, {
            'job': 'coder-ended', 'item': 'T-2', 'kind': 'coder', 'account': 'acct-c',
            'pid': 'actions:C', 'runtime': 'actions', 'runtime_lane': 'cloud',
            'started': '2026-10-08T19:00:00Z', 'ended': '2026-10-08T19:30:00Z'})
        pool_mod.append_session(self.product, {
            'job': 'coder-local', 'item': 'T-3', 'kind': 'coder', 'account': 'acct-a', 'pid': 1,
            'started': '2026-10-08T20:00:00Z'})
        self.assertEqual(cloud.inflight_all(), 2)


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
        for want in ('cloud.enabled', 'cloud.mode: primary', 'repo secret CLAUDE_CODE_OAUTH_TOKEN',
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
        for want in ('cloud.mode: overflow', 'repo secret CLAUDE_CODE_OAUTH_TOKEN is missing',
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

    def test_a_fetch_failure_never_concludes_dead_off_a_stale_view(self):
        # B-0293: the report is genuinely on origin, but report_commit's own `git fetch` fails
        # transiently (a network blip) — the run must be left as it is, never judged dead off a
        # stale or absent local view of origin/<branch>
        rec = self.launch()
        fake = FakeGh()
        self.assertEqual(self.sync(fake)[0][1], cloud.WORKING)
        self.push_as_the_job(rec)
        fake.view_ = {'status': 'completed', 'conclusion': 'success'}
        real_git = cloud._git

        def failing_fetch(args, cwd):
            if args[:1] == ['fetch']:
                return subprocess.CompletedProcess(args, 1, stdout='',
                                                   stderr='fatal: unable to access origin')
            return real_git(args, cwd)

        lines = []
        with mock.patch.object(cloud, '_git', side_effect=failing_fetch):
            (job, status, why), = self.sync(fake, lines=lines)
        self.assertEqual((job, status), ('spec-1', cloud.WORKING), why)
        self.assertIn('report unreadable', why)
        self.assertFalse(pool_mod.load_sessions(self.product)['spec-1'].get('ended'))
        self.assertTrue(lifecycle.pid_alive(rec['pid']))
        self.assertTrue(any('report unreadable' in ln for ln in lines), lines)
        # the fetch recovers next pass: the report, already on origin, finishes the run
        (job, status, why), = self.sync(fake)
        self.assertEqual((job, status), ('spec-1', cloud.FINISHED), why)

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

    def test_a_queued_run_past_its_limit_is_force_cancelled(self):
        # a plain cancel on a queued run is accepted and does nothing (2026-10-05)
        self.launch()
        fake = FakeGh(view={'status': 'queued', 'conclusion': ''})
        (_job, status, _why), = self.sync(fake, now=time.time() + 5 * 3600)
        self.assertEqual(status, cloud.DEAD)
        self.assertEqual(fake.named('run', 'cancel'), [])
        self.assertEqual(len(fake.named('api', '-X', 'POST')), 1)
        self.assertTrue(fake.named('api', '-X', 'POST')[0][4].endswith('/runs/500/force-cancel'))

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

    def unread_lookup(self, rc, err):
        """A launched run whose id was never read, past LOST_AFTER_MIN, over a ``gh`` whose
        ``run list`` answers ``(rc, err)``: ``(found, lines, rec)``."""
        rec = self.launch()
        pool_mod.update_session(self.product, 'spec-1', actions_run_id=None)
        fake = FakeGh()
        plain = fake.__call__

        def answer(argv, **kw):
            if argv[1:3] == ['run', 'list']:
                fake.calls.append(argv)
                return subprocess.CompletedProcess(argv, rc, stdout='', stderr=err)
            return plain(argv, **kw)
        lines = []
        self.addCleanup(gh_limit.reset)
        found = self.sync(answer, lines=lines, now=time.time() + (cloud.LOST_AFTER_MIN + 5) * 60)
        return found, lines, rec

    def assert_left_as_it_is(self, found, lines, rec):
        (job, status, why), = found
        self.assertEqual((job, status), ('spec-1', cloud.WORKING), why)
        self.assertNotIn('never appeared', why)
        self.assertTrue(lifecycle.pid_alive(rec['pid']))
        self.assertFalse(pool_mod.load_sessions(self.product)['spec-1'].get('ended'))
        self.assertTrue(any('run lookup unreadable' in ln for ln in lines), lines)

    def test_an_unreadable_run_lookup_is_never_a_lost_run(self):
        # gh fails (a 502): Unknown, not "not found" — the run stays as it is, logged
        self.assert_left_as_it_is(*self.unread_lookup(1, 'HTTP 502: Bad Gateway'))

    def test_a_rate_limited_run_lookup_is_never_a_lost_run(self):
        rc, _out, err = contracts.load('rate-limit', 'run-list')  # the recorded host answer
        self.assert_left_as_it_is(*self.unread_lookup(rc, err))

    def test_a_definite_not_found_past_the_window_is_dead(self):
        self.launch()
        pool_mod.update_session(self.product, 'spec-1', actions_run_id=None)
        fake = FakeGh(runs=[{'databaseId': 501, 'displayTitle': 'another run'}])
        (_job, status, why), = self.sync(
            fake, now=time.time() + (cloud.LOST_AFTER_MIN + 5) * 60)
        self.assertEqual((status, why), (cloud.DEAD, 'the dispatched run never appeared'))


class FindRun(unittest.TestCase):
    """``Gh.find_run`` answers through :mod:`asf.github`: a hit, a definite miss (``ok`` with
    ``None``), or Unknown — a failed call, bad JSON and a rate limit are never a miss."""

    def gh(self, rc, out='', err=''):
        self.addCleanup(gh_limit.reset)
        return actions.Gh(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                          run=lambda argv, **_kw: subprocess.CompletedProcess(argv, rc, out, err))

    def test_hit_miss_and_unknown(self):
        with mock.patch('asf.ci_pool._gh_env', return_value={}):
            hit = self.gh(0, json.dumps([{'databaseId': 7, 'displayTitle': 'asf x'}]))
            self.assertEqual(hit.find_run('w.yml', 'asf x').data['id'], '7')
            miss = hit.find_run('w.yml', 'asf y')
            self.assertTrue(miss.ok)
            self.assertIsNone(miss.data)
            self.assertTrue(self.gh(1, err='HTTP 502').find_run('w.yml', 'asf x').unknown)
            self.assertTrue(self.gh(0, 'not json').find_run('w.yml', 'asf x').unknown)
            rc, _out, err = contracts.load('rate-limit', 'run-list')
            limited = self.gh(rc, err=err).find_run('w.yml', 'asf x')
            self.assertTrue(limited.unknown)
            self.assertIn('rate limit', limited.reason)


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

    def test_the_default_row_names_the_floor(self):
        # ON writes no `default` at this Task's sha, so pass it explicitly (F-0216 Task 1)
        rows = cloud.checks(self.cfg(default=True), self.product(), FakeGh())
        _name, _req, _ok, detail = next(r for r in rows if r[0] == 'default')
        self.assertEqual(detail, 'cloud.mode: primary — the cloud lane is the default executor '
                                 '(local: groom, groom-clerk, close, corrections that rebase, '
                                 'cloud.local_only, cards marked local_only, and the fallback)')


if __name__ == '__main__':
    unittest.main()


class ModeKeys(unittest.TestCase):
    """``cloud.mode``: overflow (default; ``local`` its alias), primary, off — ``default: true``
    the older spelling of primary, a written mode winning; the config check refuses the rest."""

    def mode(self, **block):
        return cloud.settings({'cloud': dict(ON, **block)}).mode

    def test_the_values(self):
        self.assertEqual(self.mode(), 'overflow')
        self.assertEqual(self.mode(mode='local'), 'overflow')
        self.assertEqual(self.mode(mode='primary'), 'primary')
        self.assertEqual(self.mode(mode='Primary '), 'primary')
        self.assertEqual(self.mode(mode='off'), 'off')
        self.assertEqual(self.mode(mode=False), 'off')         # a YAML off read as false
        self.assertEqual(self.mode(default=True), 'primary')
        self.assertEqual(self.mode(default=True, mode='overflow'), 'overflow')
        s = cloud.settings({'cloud': dict(ON, mode='primary')})
        self.assertTrue(s.default and s.on)
        self.assertFalse(cloud.settings({'cloud': dict(ON, mode='off')}).on)

    def test_the_check_refuses_a_mode_it_does_not_know_and_a_bad_fallback(self):
        self.assertEqual(cloud.config_problems({'mode': 'primary', 'fallback_failures': 2,
                                                'fallback_window_min': 10,
                                                'fallback_cooldown_min': 15}), [])
        got = dict(cloud.config_problems({'mode': 'cloud-first', 'fallback_failures': 0,
                                          'fallback_cooldown_min': 'soon'}))
        self.assertEqual(sorted(got), ['cloud.fallback_cooldown_min', 'cloud.fallback_failures',
                                       'cloud.mode'])
        self.assertIn('overflow, primary, off, local', got['cloud.mode'])

    def test_a_product_file_with_a_bad_mode_is_refused(self):
        errors, _warnings = env.product_problems('cloud:\n  mode: sometimes\n')
        self.assertEqual([k for _l, k, _w in errors], ['cloud.mode'])


class PrimaryMode(Lanes):
    """``cloud.mode: primary``: every eligible row to the cloud first — up to ``max_inflight``
    and the lane accounts' quota — local only for what must run here and as the fallback, each
    fallback said on its launch line and in the status row."""

    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, cloud=dict(ON, mode='primary', rows='cloud-ok'))

    def lanes(self, launched):
        return [(r.job, rec.get('runtime_lane') or 'local') for r, rec in launched]

    def run_wave(self, rows, quota=None, **kw):
        if quota is None:
            return super().run_wave(rows, **kw)
        fake = quota_mod.FakeQuotaSource
        with mock.patch.object(quota_mod, 'FakeQuotaSource', lambda _d: fake(quota)):
            return super().run_wave(rows, **kw)

    def test_an_eligible_row_goes_to_the_cloud_with_a_free_local_seat(self):
        launched, waits, lines = self.run_wave([feature_row('spec-1')])
        self.assertEqual((self.lanes(launched), waits), ([('spec-1', 'cloud')], []))
        self.assertNotIn('cloud fallback', lines[0])

    def test_what_must_run_here_stays_local(self):
        groom = pool_mod.Row('groom-f-0001', 'F-0001', model='Opus', kind='groom')
        card = feature_row('spec-2', item='F-0002')
        card.local_only = True
        launched, _waits, lines = self.run_wave([groom, card])
        self.assertEqual(self.lanes(launched), [('groom-f-0001', 'local')])
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], local_only=['spec']))
        launched, _waits, lines = self.run_wave([feature_row('spec-3', item='F-0003')], cfg=cfg)
        self.assertEqual(self.lanes(launched), [('spec-3', 'local')])
        self.assertFalse([l for l in lines if 'cloud fallback' in l])  # not a fallback

    def test_a_full_cloud_lane_falls_back_and_says_so(self):
        live = [{'job': 'c1', 'account': 'acct-c', 'runtime_lane': 'cloud'},
                {'job': 'c2', 'account': 'acct-c', 'runtime_lane': 'cloud'}]
        launched, _waits, lines = self.run_wave([feature_row('spec-1')], live=live)
        self.assertEqual(self.lanes(launched), [('spec-1', 'local')])
        self.assertTrue(lines[0].endswith('— cloud fallback: cloud full — 2/2 in flight'),
                        lines[0])
        self.assertEqual(cloud.last_fallback(self.product)['job'], 'spec-1')
        with mock.patch.object(cloud, 'inflight', return_value=2):
            clause = cloud.capacity_clause(self.cfg, self.product)
        self.assertRegex(clause, r'^cloud 2/2 primary \(last fallback 0m ago: spec-1 local — '
                                 r'cloud full — 2/2 in flight\)$')

    def test_no_cloud_quota_falls_back(self):
        quota = {'acct-c': {'five_h_pct': 99, 'seven_d_pct': 10}}
        launched, _waits, lines = self.run_wave([feature_row('spec-1')], quota=quota)
        self.assertEqual(self.lanes(launched), [('spec-1', 'local')])
        self.assertIn('— cloud fallback: cloud: accounts stopped: acct-c', lines[0])

    def test_an_unready_lane_falls_back_and_says_so(self):
        launched, _waits, lines = self.run_wave([feature_row('spec-1')],
                                                ready=(False, 'no runner'))
        self.assertEqual(self.lanes(launched), [('spec-1', 'local')])
        launch = [l for l in lines if l.startswith('launched')]
        self.assertTrue(launch[0].endswith('— cloud fallback: cloud lane unready: no runner'),
                        launch)

    def failing_spawn(self):
        def spawn(product, row, acct, brief, runtime=None, cfg=None):
            if runtime is self.crt:
                raise spawn_mod.SpawnError('remote create refused (HTTP 500)')
            return spawn_mod.spawn(product, row, acct, brief, runtime=runtime, cfg=cfg)
        return spawn

    def test_erroring_creates_trip_the_fallback_for_a_cool_down(self):
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], fallback_failures=2,
                                        fallback_cooldown_min=30))
        rows = [feature_row('spec-1'), feature_row('spec-2', item='F-0002')]
        launched, waits, lines = self.run_wave(rows, cfg=cfg, spawn_fn=self.failing_spawn())
        self.assertEqual(launched, [])
        trip = [l for l in lines if l.startswith('cloud lane: cloud launches erroring')]
        self.assertEqual(len(trip), 1, lines)
        self.assertIn('2 failed creates in 30 min (create; last: remote create refused '
                      '(HTTP 500))', trip[0])
        # the next tick: a free cloud seat, and the row still goes local, saying why
        launched, _waits, lines = self.run_wave([feature_row('spec-3', item='F-0003')], cfg=cfg)
        self.assertEqual(self.lanes(launched), [('spec-3', 'local')])
        self.assertIn('— cloud fallback: cloud launches erroring', lines[0])
        clause = cloud.capacity_clause(cfg, self.product)
        self.assertIn('primary (fallback: cloud launches erroring', clause)
        self.assertIn('cloud launches erroring', cloud.lane_split(cfg, self.product))

    def raising_spawn(self, make_err):
        def spawn(product, row, acct, brief, runtime=None, cfg=None):
            if runtime is self.crt:
                raise make_err(row)
            return spawn_mod.spawn(product, row, acct, brief, runtime=runtime, cfg=cfg)
        return spawn

    def two_rows(self):  # the lane's two seats (max_inflight 2)
        return [feature_row(f'spec-{i}', item=f'F-000{i}') for i in (1, 2)]

    def test_f0276_stray_branch_refusals_never_trip_the_breaker(self):
        # one job's leftover local branch is its own state, not the lane failing: three such
        # refusals in the window leave the breaker closed and every other row on the cloud
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], fallback_failures=3))
        stray = self.raising_spawn(lambda row: spawn_mod.BranchState(
            f'branch spec/{row.item} exists locally with 1 commit(s) not on origin/main and no '
            f'worktree — look before relaunching'))
        for _ in range(2):  # two ticks of two refusals: four in the window
            launched, _waits, lines = self.run_wave(self.two_rows(), cfg=cfg, spawn_fn=stray)
            self.assertEqual(launched, [])
            self.assertFalse([l for l in lines if 'erroring' in l], lines)
        s = cloud.settings(cfg, self.product)
        self.assertEqual(cloud.Breaker(self.product, s).tripped(), '')
        self.assertEqual(cloud.fallback_state(self.product, s), '')
        launched, _waits, _lines = self.run_wave([feature_row('spec-4', item='F-0004')], cfg=cfg)
        self.assertEqual(self.lanes(launched), [('spec-4', 'cloud')])

    def test_f0276_three_api_create_failures_trip_it_and_name_the_class(self):
        cfg = dict(self.cfg, cloud=dict(self.cfg['cloud'], fallback_failures=3))
        api = self.raising_spawn(lambda row: spawn_mod.SpawnError(
            'remote create refused: HTTP 401 Unauthorized'))
        lines = []
        for _ in range(2):  # two ticks of two failures: the third trips it
            launched, _waits, got = self.run_wave(self.two_rows(), cfg=cfg, spawn_fn=api)
            self.assertEqual(launched, [])
            lines += got
        trip = [l for l in lines if l.startswith('cloud lane: cloud launches erroring')]
        self.assertEqual(len(trip), 1, lines)
        self.assertIn('3 failed creates in 30 min (auth; last: remote create refused', trip[0])
        self.assertIn('(auth; last:', cloud.capacity_clause(cfg, self.product))

    def test_f0276_failure_classes(self):
        self.assertEqual(cloud.failure_class(spawn_mod.BranchState('x')), cloud.BRANCH_STATE)
        for text, kind in (('HTTP 403 forbidden', 'auth'), ('429 rate limit', 'quota'),
                           ('connection reset by peer', 'transport'),
                           ('remote create refused (HTTP 500)', 'create')):
            self.assertEqual(cloud.failure_class(spawn_mod.SpawnError(text)), kind, text)
        s = cloud.settings({'cloud': dict(ON, mode='primary', fallback_failures=1)})
        b = cloud.Breaker(self.product, s)
        self.assertIsNone(b.fail(spawn_mod.BranchState('branch x exists locally')))
        self.assertEqual(b.tripped(), '')
        self.assertIn('(transport; last: timed out)', b.fail('timed out'))

    def test_the_breaker_counts_consecutive_failures_in_its_window_and_cools_down(self):
        s = cloud.settings({'cloud': dict(ON, mode='primary', fallback_failures=2,
                                          fallback_window_min=10, fallback_cooldown_min=30)})
        now = [1_000_000.0]
        b = cloud.Breaker(self.product, s, clock=lambda: now[0])
        self.assertIsNone(b.fail('x'))
        b.ok()                                   # a success between: not consecutive
        self.assertIsNone(b.fail('x'))
        now[0] += 11 * 60                        # out of the window
        self.assertIsNone(b.fail('x'))
        now[0] += 60
        self.assertTrue(b.fail('y'))             # two within ten minutes: tripped
        self.assertTrue(cloud.Breaker(self.product, s, clock=lambda: now[0]).tripped())
        now[0] += 31 * 60                        # the cool-down is over
        self.assertEqual(cloud.Breaker(self.product, s, clock=lambda: now[0]).tripped(), '')

    def test_overflow_is_unchanged_and_keeps_no_breaker(self):
        cfg = dict(self.cfg, cloud=dict(ON, mode='overflow', fallback_failures=1))
        launched, _waits, lines = self.run_wave([feature_row('spec-1')], cfg=cfg)
        self.assertEqual(self.lanes(launched), [('spec-1', 'local')])
        live = [{'job': 'x', 'account': 'acct-a'}]
        launched, waits, lines = self.run_wave([feature_row('spec-2', item='F-0002')], cfg=cfg,
                                               live=live, spawn_fn=self.failing_spawn())
        self.assertEqual(launched, [])
        self.assertFalse([l for l in lines if 'erroring' in l or 'fallback' in l])
        self.assertFalse(os.path.exists(os.path.join(env.state_dir(self.product),
                                                     cloud.Breaker.FILE)))
        self.assertEqual(cloud.capacity_clause(cfg, self.product), 'cloud 0/2')

    def test_off_launches_no_cloud_session_and_drains(self):
        cfg = dict(self.cfg, cloud=dict(ON, mode='off'))
        live = [{'job': 'x', 'account': 'acct-a'}]
        launched, waits, _ = self.run_wave([feature_row('spec-1')], cfg=cfg, live=live,
                                           ready=None)
        self.assertEqual(launched, [])
        self.assertEqual(waits[0][1], 'pool full — accounts at cap: acct-a 1/1')  # acct-c kept off
        self.assertEqual(cloud.capacity_clause(cfg, self.product),
                         'cloud 0/2 off (no new launch; live runs drain)')
        self.assertEqual(cloud.doctor_rows(cfg, self.product),
                         [(False, True, 'cloud.mode: off — no new cloud launch; live cloud runs '
                                        'drain')])

    def test_the_mode_flips_on_the_next_tick(self):
        """One line in the product file, re-read by the next tick's ``load_product`` — nothing
        cached across ticks."""
        path = env.product_path('sample')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        base = (f'repo_dir: {self.repo}\nmain: main\nrepo_slug: o/r\njob_grants:\n'
                f'  - {self.grant}\ncloud:\n  enabled: true\n  max_inflight: 2\n'
                f'  rows: any\n  accounts: [acct-c]\n  launch_wait_s: 0\n')
        cfg = dict(self.cfg, cloud={})
        got = []
        for n, mode in enumerate(('primary', 'overflow', 'off', 'local', 'primary'), 1):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(base + f'  mode: {mode}\n')
            self.product = env.load_product('sample')
            launched, _w, _l = self.run_wave([feature_row(f'spec-{n}', item=f'F-000{n}')],
                                             cfg=cfg)
            got.append(self.lanes(launched)[0][1])
        self.assertEqual(got, ['cloud', 'local', 'local', 'local', 'cloud'])

    def test_doctor_names_the_split(self):
        with mock.patch('asf.capacity.host_size', return_value=(10, 32)):
            p = env.Product('sample', dict(self.product._data, capacity={'sessions': 'auto'}))
            line = cloud.lane_split(self.cfg, p)
        self.assertTrue(line.startswith(
            'mode primary (cloud first; local takes local-only rows and the fallback) — local '
            'sessions 5 (auto: min(ceiling 8, 10 cores/2, 32 GB/4)), cloud max_inflight 2, '
            'local only: groom, groom-clerk, close, corrections that rebase (conflict, copies, '
            'naming) and cards marked local_only'), line)
        off = cloud.lane_split({}, self.product)
        self.assertIn('mode overflow (local first; the cloud takes what local cannot)', off)
        self.assertIn('cloud lane off', off)
        from asf import doctor
        self.assertEqual(doctor.check_lane_split(self.cfg, self.product),
                         cloud.lane_split(self.cfg, self.product))



class LogGh(FakeGh):
    """FakeGh that also answers ``gh run view <id> --log-failed`` / ``--log`` from ``log``."""

    def __init__(self, log='', **kw):
        super().__init__(**kw)
        self.log = log

    def __call__(self, argv, **kw):
        if argv[1:3] == ['run', 'view'] and argv[-1] in ('--log-failed', '--log'):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if self.log else 1, stdout=self.log,
                                               stderr='' if self.log else 'no log')
        return super().__call__(argv, **kw)

    def log_reads(self):
        return [c for c in self.calls if c[-1] in ('--log-failed', '--log')]


class _DeadSync(Lanes):
    launch = Sync.launch
    sync = Sync.sync
    push_as_the_job = Sync.push_as_the_job


class ADeadReasonCarriesTheRefusal(_DeadSync):
    """F-0266 S-64357: the sync passes the run's own refusal to ``classify``, and the composed
    sentence reaches ``cloud-sessions.json`` and the ``cloud <job> dead:`` line."""

    def test_a_naming_correction_on_the_run_is_named(self):
        rec = self.launch()
        fake = LogGh()
        self.sync(fake)
        pool_mod.update_session(self.product, 'spec-1', correction={
            'kind': 'naming', 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'text': 'commits do not name F-0001: every commit subject on the branch names its item'})
        fake.view_ = {'status': 'completed', 'conclusion': 'succeeded'}
        lines = []
        (_job, status, why), = self.sync(fake, lines=lines)
        self.assertEqual(status, cloud.DEAD)
        self.assertTrue(why.startswith('run 500 ended succeeded without the report commit — last '
                                       'ASF refusal (naming, 0m before): commits do not name '
                                       'F-0001'), why)
        self.assertEqual(cloudpid.why(rec['pid']), why)
        self.assertIn(f'cloud    spec-1                   dead: {why}', lines)
        self.assertEqual(fake.log_reads(), [])  # the record answered: no log read


class TheRunLogIsReadOnce(_DeadSync):
    """F-0266 S-64358: with the ledger and the record empty, the dead run's log is read once."""

    PUSH_ALLOW = 'asf: push refused — an ASF session pushes only to factory branches, not: main'

    def test_a_refusal_in_the_log_names_the_death_and_the_log_is_read_once(self):
        rec = self.launch()
        fake = LogGh(log=f'step\n{self.PUSH_ALLOW}\nexit 1\n')
        self.sync(fake)
        self.assertEqual(fake.log_reads(), [])  # working: no read
        fake.view_ = {'status': 'completed', 'conclusion': 'failure'}
        (_job, status, why), = self.sync(fake)
        self.assertEqual(status, cloud.DEAD)
        self.assertIn('last ASF refusal (push-allow): asf: push refused', why)
        self.assertEqual(len(fake.log_reads()), 1)
        self.assertEqual(fake.log_reads()[0][1:4], ['run', 'view', '500'])
        pool_mod.update_session(self.product, 'spec-1', ended='2026-10-07T10:00:00Z')
        self.assertEqual(self.sync(fake), [])  # an ended run is not synced again
        self.assertEqual(len(fake.log_reads()), 1)
        self.assertEqual(cloudpid.why(rec['pid']), why)

    def test_no_log_leaves_the_reason_bare(self):
        self.launch()
        fake = LogGh(log='')
        self.sync(fake)
        fake.view_ = {'status': 'completed', 'conclusion': 'failure'}
        (_job, _status, why), = self.sync(fake)
        self.assertEqual(why, 'run 500 ended failure without the report commit')

    def test_the_ledger_answers_first(self):
        from asf.workers import refusals
        self.launch()
        fake = LogGh(log=self.PUSH_ALLOW)
        self.sync(fake)
        with open(refusals.env_for(self.product, 'spec-1')['ASF_REFUSAL_LOG'], 'w') as f:
            f.write(json.dumps({'at': '2026-10-07T10:00:00Z', 'kind': 'hook refused',
                                'line': 'pre-push: lint failed'}) + '\n')
        fake.view_ = {'status': 'completed', 'conclusion': 'failure'}
        (_job, _status, why), = self.sync(fake)
        self.assertIn('(hook refused', why)
        self.assertEqual(fake.log_reads(), [])

    def test_the_remote_leg_uses_run_log_summary(self):
        from asf.workers import remote
        run = {'remote_session_id': 's', 'pid': 'remote:t'}
        with mock.patch.object(remote, 'run_log_summary', return_value=self.PUSH_ALLOW) as rls:
            self.assertEqual(cloud._run_log_tail(run, None, 'client', None), self.PUSH_ALLOW)
        rls.assert_called_once_with(run, None, 'client')
        with mock.patch.object(remote, 'run_log_summary', side_effect=RuntimeError('x')):
            self.assertEqual(cloud._run_log_tail(run, None, 'client', None), '')
