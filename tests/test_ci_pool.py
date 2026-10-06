"""asf.ci_pool — the declared runner pool against a fake CI host: validation, the doctor's drift
rows, the reconcile plan and its add-before-remove order, a trial's pass and rollback, and the CI
ceiling the pool sets. No test shells out: the host is :class:`FakeBackend`."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import capacity, ci_census, ci_pool, env
from asf.ci_pool import Runner, RunsOn


def pool_data():
    return [
        {'runner': 'ci-1', 'box': 'box-1', 'provider': 'alpha', 'size': 'l', 'role': 'heavy', 'slots': 2},
        {'runner': 'ci-1b', 'box': 'box-1', 'provider': 'alpha', 'size': 'l', 'role': 'light'},
        {'runner': 'ci-h1', 'box': 'box-9', 'provider': 'beta', 'size': 'l', 'role': 'heavy'},
    ]


def product(pool=None, cap=None, name='p'):
    data = {'repo_slug': 'o/r', 'ci': {'provider': 'github-actions', 'pool': pool_data() if pool is None else pool}}
    if cap is not None:
        data['capacity'] = cap
    return env.Product(name, data)


BASE = ['self-hosted', 'Linux', 'X64']


class FakeBackend(ci_pool.Backend):
    """Runners and runs-on in memory; every label write is logged in order."""

    def __init__(self, runners, runs_on, jobs=None):
        self._runners = {r.name: r for r in runners}
        self._runs_on = runs_on
        self.jobs = jobs or {}
        self.writes = []

    def runners(self):
        return list(self._runners.values())

    def runs_on(self):
        return list(self._runs_on)

    def add_labels(self, runner, labels):
        self.writes.append(('add', runner.name, tuple(labels)))
        r = self._runners[runner.name]
        r.labels = r.labels + [l for l in labels if l not in r.labels]

    def remove_label(self, runner, label):
        self.writes.append(('remove', runner.name, label))
        r = self._runners[runner.name]
        r.labels = [l for l in r.labels if ci_pool._norm(l) != label]

    def jobs_on(self, runner_name, since):
        return [j for j in self.jobs.get(runner_name, []) if j['started_at'] >= since]


def runner(name, *labels, online=True):
    return Runner(name=name, online=online, labels=BASE + list(labels), id=name,
                  fixed=frozenset(l.lower() for l in BASE))


def ro(*labels, wf='ci.yml', job='j'):
    return RunsOn(wf, job, frozenset(ci_pool._norm(l) for l in labels))


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)


class Validation(unittest.TestCase):
    def problems(self, pool):
        return [k for k, _why in ci_pool.pool_problems({'pool': pool})]

    def test_a_well_formed_pool_has_no_problems(self):
        self.assertEqual(self.problems(pool_data()), [])

    def test_missing_fields_unknown_keys_bad_slots_and_duplicates(self):
        bad = [{'runner': 'a', 'provider': 'x'},                        # no role
               {'runner': 'b', 'provider': 'x', 'role': 'heavy', 'labels': ['y']},
               {'runner': 'c', 'provider': 'x', 'role': 'heavy', 'slots': 0},
               {'runner': 'c', 'provider': 'x', 'role': 'light'}]
        self.assertEqual(self.problems(bad), ['ci.pool[0].role', 'ci.pool[1].labels',
                                              'ci.pool[2].slots', 'ci.pool[3].runner'])

    def test_a_role_must_be_a_capability_not_a_provider(self):
        for role in ('provider-x', 'x', 'self-hosted'):
            self.assertEqual(self.problems([{'runner': 'a', 'provider': 'x', 'role': role}]),
                             ['ci.pool[0].role'], role)

    def test_the_product_file_refuses_a_bad_pool_on_load(self):
        text = ('product: p\nci:\n  provider: github-actions\n  pool:\n'
                '    - {runner: a, provider: x, role: heavy, slots: -1}\n')
        keys = [k for _ln, k, _w in env.validate_product_text(text)]
        self.assertEqual(keys, ['ci.pool[0].slots'])
        ok = 'product: p\nci:\n  pool:\n    - {runner: a, box: b, provider: x, size: s, role: heavy}\n'
        self.assertEqual(env.validate_product_text(ok), [])


class Namespace(unittest.TestCase):
    """``asf-`` is ASF's own label namespace: a product file may not place a tier by hand."""

    def problems(self, pool):
        return dict(ci_pool.pool_problems({'pool': pool}))

    def test_a_declared_role_beginning_asf_is_refused_and_named(self):
        problems = self.problems([{'runner': 'a', 'provider': 'x', 'role': 'asf-fast'}])
        self.assertEqual(list(problems), ['ci.pool[0].role'])
        self.assertIn("ASF's own namespace", problems['ci.pool[0].role'])

    def test_a_declared_class_beginning_asf_is_refused_and_named(self):
        problems = self.problems([{'runner': 'a', 'provider': 'x', 'role': 'heavy',
                                   'class': 'asf-x'}])
        self.assertEqual(list(problems), ['ci.pool[0].class'])
        self.assertIn("ASF's own namespace", problems['ci.pool[0].class'])

    def test_a_role_or_class_outside_the_namespace_is_unaffected(self):
        self.assertEqual(self.problems([{'runner': 'a', 'provider': 'x', 'role': 'heavy',
                                        'class': 'heavy-fast'}]), {})

    def test_the_product_file_refuses_a_namespaced_role(self):
        text = ('product: p\nci:\n  provider: github-actions\n  pool:\n'
                '    - {runner: a, provider: x, role: asf-fast}\n')
        keys = [k for _ln, k, _w in env.validate_product_text(text)]
        self.assertEqual(keys, ['ci.pool[0].role'])


class RunsOnParser(unittest.TestCase):
    def test_flow_scalar_block_and_expressions(self):
        text = '\n'.join([
            'on: push', 'jobs:', '  gate:',
            '    runs-on: [self-hosted, "${{ vars.X || \'heavy\' }}"]  # comment',
            '    steps:', '      - run: echo', '  hosted:', '    runs-on: ubuntu-latest',
            '  mat:', '    runs-on: ${{ matrix.os }}', '  blk:', '    runs-on:',
            '      - self-hosted', '      - light', ''])
        rows = {r.job: r for r in ci_pool.parse_runs_on('ci.yml', text)}
        got = {job: r.labels for job, r in rows.items()}
        self.assertEqual(got, {'gate': None, 'hosted': frozenset({'ubuntu-latest'}),
                               'mat': None, 'blk': frozenset({'self-hosted', 'light'})})
        self.assertEqual(rows['gate'].needs_vars, frozenset({'X'}))


class FakeRun:
    """A fake ``gh``: a workflow directory listing plus each file's raw text, keyed by the
    ``contents/<path>`` fragment ``_api`` builds, and — when given — the repo's variables.
    Every argv is logged."""

    def __init__(self, files, texts, variables=None):
        self.files = files
        self.texts = texts
        self.variables = variables
        self.calls = []

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        target = argv[2]
        if any('actions/variables' in a for a in argv):
            if self.variables is None:
                return subprocess.CompletedProcess(argv, 1, '', 'not found')
            out = '\n'.join(json.dumps({'name': k, 'value': v})
                            for k, v in self.variables.items())
            return subprocess.CompletedProcess(argv, 0, out, '')
        if 'workflows?ref=' in target:
            out = '\n'.join(json.dumps({'path': p}) for p in self.files)
            return subprocess.CompletedProcess(argv, 0, out, '')
        for path, text in self.texts.items():
            if f'contents/{path}?ref=' in target:
                return subprocess.CompletedProcess(argv, 0, text, '')
        return subprocess.CompletedProcess(argv, 1, '', 'not found')


INCIDENT_WORKFLOW = '\n'.join([
    'on: push', 'jobs:', '  gate:',
    '    runs-on: [self-hosted, "${{ vars.CI_REQUIRED_LABEL || \'heavy\' }}"]',
    '    steps:', '      - run: echo', ''])


class VarsInRunsOn(unittest.TestCase):
    def test_a_set_variable_is_the_label_the_job_runs_on(self):
        got = ci_pool.parse_runs_on('ci.yml', INCIDENT_WORKFLOW,
                                    {'CI_REQUIRED_LABEL': 'vendor-heavy'})[0]
        self.assertEqual(got.labels, frozenset({'self-hosted', 'vendor-heavy'}))
        self.assertEqual(got.needs_vars, frozenset())

    def test_a_name_the_host_did_not_give_is_unresolved_not_the_default(self):
        for variables in ({}, None, {'OTHER': 'x'}):
            got = ci_pool.parse_runs_on('ci.yml', INCIDENT_WORKFLOW, variables)[0]
            self.assertIsNone(got.labels, variables)
            self.assertEqual(got.needs_vars, frozenset({'CI_REQUIRED_LABEL'}), variables)

    def test_an_empty_value_falls_to_the_expressions_own_default(self):
        got = ci_pool.parse_runs_on('ci.yml', INCIDENT_WORKFLOW,
                                    {'CI_REQUIRED_LABEL': ''})[0]
        self.assertEqual(got.labels, frozenset({'self-hosted', 'heavy'}))
        self.assertEqual(got.needs_vars, frozenset())

        text = '\n'.join(['on: push', 'jobs:', '  gate:',
                          '    runs-on: [self-hosted, "${{ vars.CI_REQUIRED_LABEL }}"]', ''])
        got = ci_pool.parse_runs_on('ci.yml', text, {'CI_REQUIRED_LABEL': ''})[0]
        self.assertIsNone(got.labels)
        self.assertEqual(got.needs_vars, frozenset())

    def test_the_name_is_matched_the_way_the_host_matches_it(self):
        got = ci_pool.parse_runs_on('ci.yml', INCIDENT_WORKFLOW,
                                    {'ci_required_label': 'vendor-heavy'})[0]
        self.assertEqual(got.labels, frozenset({'self-hosted', 'vendor-heavy'}))

    def test_a_bare_reference_resolves_and_a_block_list_resolves(self):
        scalar = '\n'.join(['on: push', 'jobs:', '  gate:',
                            '    runs-on: ${{ vars.X }}', ''])
        block = '\n'.join(['on: push', 'jobs:', '  gate:', '    runs-on:',
                           '      - self-hosted', '      - ${{ vars.X }}', ''])
        for text in (scalar, block):
            resolved = ci_pool.parse_runs_on('ci.yml', text, {'X': 'vendor-heavy'})[0]
            self.assertIsNotNone(resolved.labels, text)
            self.assertIn('vendor-heavy', resolved.labels)
            self.assertEqual(resolved.needs_vars, frozenset())

            unresolved = ci_pool.parse_runs_on('ci.yml', text, {})[0]
            self.assertIsNone(unresolved.labels, text)
            self.assertEqual(unresolved.needs_vars, frozenset({'X'}))

    def test_every_other_expression_reads_as_it_did(self):
        text = '\n'.join([
            'on: push', 'jobs:',
            '  mat:', '    runs-on: ${{ matrix.os }}',
            '  inp:', '    runs-on: ${{ inputs.r || \'heavy\' }}',
            '  env:', '    runs-on: ${{ env.R || \'heavy\' }}', ''])
        rows = {r.job: r for r in ci_pool.parse_runs_on('ci.yml', text)}
        self.assertIsNone(rows['mat'].labels)
        self.assertEqual(rows['mat'].needs_vars, frozenset())
        self.assertEqual(rows['inp'].labels, frozenset({'heavy'}))
        self.assertEqual(rows['env'].labels, frozenset({'heavy'}))

    def test_the_backend_reads_the_repos_variables_once_and_resolves_with_them(self):
        run = FakeRun(['ci.yml'], {'ci.yml': INCIDENT_WORKFLOW},
                      variables={'CI_REQUIRED_LABEL': 'vendor-heavy'})
        backend = ci_pool.GitHubBackend(product(), run=run)
        got = backend.runs_on()
        var_calls = [c for c in run.calls if any('actions/variables' in a for a in c)]
        self.assertEqual(len(var_calls), 1)
        self.assertEqual(got[0].labels, frozenset({'self-hosted', 'vendor-heavy'}))
        self.assertIsNone(backend.vars_error)

    def test_a_variables_read_that_fails_is_no_variables_and_its_reason(self):
        run = FakeRun(['ci.yml'], {'ci.yml': INCIDENT_WORKFLOW})
        backend = ci_pool.GitHubBackend(product(), run=run)
        got = backend.runs_on()
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0].labels)
        self.assertIsNotNone(backend.vars_error)
        self.assertIn('not found', backend.vars_error)


class JobTimeouts(unittest.TestCase):
    def test_the_eight_shapes(self):
        text = '\n'.join([
            'on: push', 'jobs:',
            '  a:', '    timeout-minutes: 45', '    runs-on: ubuntu-latest',
            '    steps:', '      - run: echo',
            '  b:', '    runs-on: ubuntu-latest',
            '  c:', '    runs-on: ubuntu-latest', '    timeout-minutes: 20',
            '    steps:', '      - name: test', '        timeout-minutes: 5',
            '  d:', '    name: Deploy Job', '    runs-on: ubuntu-latest',
            '    timeout-minutes: 15',
            '  e:', '    runs-on: ubuntu-latest', '    timeout-minutes: ${{ matrix.timeout }}',
            '  f:', '    runs-on: ubuntu-latest', '    timeout-minutes: soon', ''])
        self.assertEqual(ci_pool.parse_timeouts(text),
                         {'a': 45, 'c': 20, 'd': 15, 'Deploy Job': 15})

    def test_a_zero_timeout_is_no_declared_timeout(self):
        # #26: `timeout-minutes: 0` is no limit of the workflow's own, never one already passed
        text = '\n'.join(['on: push', 'jobs:', '  site:', '    runs-on: x',
                          '    timeout-minutes: 0', '  b:', '    timeout-minutes: 7', ''])
        self.assertEqual(ci_pool.parse_timeouts(text), {'b': 7})

    def test_no_jobs_at_all(self):
        self.assertEqual(ci_pool.parse_timeouts('on: push\n'), {})

    def test_this_repos_own_workflow_declares_none(self):
        with open('.github/workflows/tests.yml', encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(ci_pool.parse_timeouts(text), {})

    def test_default_job_timeout_min(self):
        self.assertEqual(ci_pool.DEFAULT_JOB_TIMEOUT_MIN, 360)

    def test_github_backend_merges_across_files_and_shares_the_walk_with_runs_on(self):
        files = ['ci.yml', 'other.yml']
        texts = {
            'ci.yml': '\n'.join(['jobs:', '  tests:', '    runs-on: ubuntu-latest',
                                 '    timeout-minutes: 30', '']),
            'other.yml': '\n'.join(['jobs:', '  tests:', '    runs-on: ubuntu-latest',
                                    '    timeout-minutes: 45', '  extra:',
                                    '    runs-on: ubuntu-latest', '']),
        }
        run = FakeRun(files, texts)
        backend = ci_pool.GitHubBackend(product(), run=run)
        self.assertEqual(backend.timeouts(), {'tests': 45})
        walked = {c[2] for c in run.calls if 'workflows?ref=' not in c[2]}
        self.assertEqual(len(walked), 2)
        got = {r.job for r in backend.runs_on()}
        self.assertEqual(got, {'tests', 'extra'})

    def test_backend_error_with_no_repo_slug(self):
        p = env.Product('p2', {'ci': {'provider': 'github-actions'}})
        backend = ci_pool.GitHubBackend(p, run=FakeRun([], {}))
        with self.assertRaises(ci_pool.BackendError):
            backend.timeouts()


class Drift(unittest.TestCase):
    def findings(self, runners, runs_on, pool=None):
        return [d for ok, d in ci_pool.drift(ci_pool.load_pool(product(pool)), runners, runs_on)
                if not ok]

    def test_a_runner_whose_labels_no_job_asks_for_is_stranded(self):
        runners = [runner('ci-1', 'heavy', 'provider-alpha'), runner('ci-1b', 'light', 'provider-alpha'),
                   runner('ci-h1', 'beta-heavy')]
        got = self.findings(runners, [ro('self-hosted', 'heavy'), ro('self-hosted', 'light')])
        self.assertTrue(any(d.startswith('stranded: ci-h1') for d in got), got)
        self.assertFalse(any(d.startswith('stranded: ci-1') for d in got), got)
        self.assertTrue(any(d.startswith("role: ci-h1 lacks its role 'heavy'") for d in got), got)

    def test_a_provider_label_in_runs_on_is_flagged_and_its_set_unsatisfiable(self):
        runners = [runner('ci-1', 'heavy'), runner('ci-1b', 'light'), runner('ci-h1', 'heavy')]
        got = self.findings(runners, [ro('self-hosted', 'alpha', 'heavy'),
                                      ro('self-hosted', 'provider-beta')])
        self.assertIn("provider-like label in runs-on: ci.yml asks for 'alpha', not a declared "
                      "role (heavy, light) — ask for a role", got)
        self.assertTrue(any("'provider-beta', a provider label" in d for d in got), got)
        self.assertTrue(any(d.startswith('unsatisfiable: ci.yml:j') for d in got), got)

    def test_hosted_and_unresolvable_jobs_are_never_judged(self):
        runners = [runner('ci-1', 'heavy'), runner('ci-1b', 'light'), runner('ci-h1', 'heavy')]
        got = self.findings(runners, [ro('self-hosted', 'heavy'), ro('self-hosted', 'light'),
                                      ro('ubuntu-latest'), RunsOn('x.yml', 'm', None)])
        self.assertEqual(got, [])

    def test_missing_offline_and_undeclared_runners(self):
        runners = [runner('ci-1', 'heavy'), runner('ci-1b', 'light', online=False),
                   runner('stray', 'heavy')]
        got = self.findings(runners, [ro('self-hosted', 'heavy'), ro('self-hosted', 'light')])
        self.assertIn('missing: ci-h1 (box-9, beta) is declared but not registered on the CI host', got)
        self.assertIn('offline: ci-1b (box-1, alpha) — 1 light slot(s) lost', got)
        self.assertIn('undeclared: stray is registered but not in ci.pool', got)

    def test_a_sound_pool_is_one_ok_row(self):
        runners = [runner('ci-1', 'heavy'), runner('ci-1b', 'light'), runner('ci-h1', 'heavy')]
        rows = ci_pool.drift(ci_pool.load_pool(product()), runners,
                             [ro('self-hosted', 'heavy'), ro('self-hosted', 'light')])
        self.assertEqual(rows, [(True, '3 runners declared, all online and reachable (heavy 3, light 1)')])

    def test_no_pool_no_rows_and_no_host_call(self):
        class Boom(ci_pool.Backend):
            def runners(self):
                raise AssertionError('called')
        self.assertEqual(ci_pool.doctor_rows(product(pool=[]), backend=Boom()), [])

    def test_an_unreadable_host_is_one_unknown_row(self):
        class Down(ci_pool.Backend):
            def runners(self):
                raise ci_pool.BackendError('401')
        rows = ci_pool.doctor_rows(product(), backend=Down())
        self.assertEqual([(r, ok) for r, ok, _d in rows], [(False, None)])


def discovered(reserve=None, name='p'):
    ci = {'provider': 'github-actions', 'pool': 'discover'}
    if reserve is not None:
        ci['reserve'] = reserve
    return env.Product(name, {'repo_slug': 'o/r', 'ci': ci})


class RunsOnRouting(Home):
    """A self-hosted ``runs-on`` on a discovered pool asking for a label that is neither a tier
    nor a reservation is a required red row naming the workflow, the job and the label (I7)."""

    POOL = [ci_pool.PoolEntry(runner='ci-1', provider='', role='asf-fast'),
           ci_pool.PoolEntry(runner='ci-2', provider='', role='asf-bulk')]
    RUNNERS = [runner('ci-1', 'asf-fast'), runner('ci-2', 'asf-bulk')]

    def findings(self, runs_on, reserve=None):
        p = discovered(reserve=reserve)
        owned = ci_pool.reserve_labels(p)
        return [d for ok, d in ci_pool.drift(self.POOL, self.RUNNERS, runs_on, owned=owned,
                                             product=p) if not ok]

    def test_a_label_with_no_tier_and_no_reservation_is_a_required_row(self):
        got = self.findings([ro('self-hosted', 'heavy')])
        self.assertIn("runs-on: ci.yml:j asks for 'heavy' — no tier ASF places (asf-fast, "
                      "asf-bulk) and no reservation; ASF cannot route it", got)

    def test_a_tier_label_produces_no_routing_row(self):
        got = self.findings([ro('self-hosted', 'asf-fast')])
        self.assertEqual([d for d in got if d.startswith('runs-on:')], [])

    def test_a_reserve_label_produces_no_routing_row(self):
        label = ci_pool.default_reserve_label('asf-fast')
        got = self.findings([ro('self-hosted', label)], reserve={'of': 'asf-fast', 'keep_free': 1})
        self.assertEqual([d for d in got if d.startswith('runs-on:')], [])

    def test_a_declared_pool_still_gets_todays_provider_like_row_and_nothing_new(self):
        runners = [runner('ci-1', 'heavy'), runner('ci-1b', 'light'), runner('ci-h1', 'heavy')]
        got = [d for ok, d in ci_pool.drift(ci_pool.load_pool(product()), runners,
                                            [ro('self-hosted', 'stray')]) if not ok]
        self.assertTrue(any(d.startswith('provider-like label in runs-on:') for d in got), got)
        self.assertFalse(any(d.startswith('runs-on:') for d in got), got)

    def test_doctor_rows_marks_the_new_row_required(self):
        p = discovered()
        path = os.path.join(env.state_dir(p), 'ci-census.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'v': ci_census.CENSUS_VERSION,
                      'runners': [{'runner': 'ci-1', 'tier': 'asf-fast'},
                                  {'runner': 'ci-2', 'tier': 'asf-bulk'}]}, f)
        b = FakeBackend(self.RUNNERS, [ro('self-hosted', 'heavy')])
        rows = ci_pool.doctor_rows(p, backend=b)
        hit = [(r, ok, d) for r, ok, d in rows if d.startswith('runs-on:')]
        self.assertEqual([(r, ok) for r, ok, _d in hit], [(True, False)])


class Plan(Home):
    def setUp(self):
        super().setUp()
        # the incident: every job asks for a provider label; the beta box lacks it and is stranded
        self.runs_on = [ro('self-hosted', 'alpha', 'heavy'), ro('self-hosted', 'alpha')]
        self.backend = FakeBackend(
            [runner('ci-1', 'alpha', 'heavy'), runner('ci-1b', 'alpha', 'light'),
             runner('ci-h1', 'beta-heavy')], self.runs_on)
        self.p = product()

    def steps(self):
        return ci_pool.plan(ci_pool.load_pool(self.p), self.backend.runners(), self.backend.runs_on(),
                            trials=ci_pool.load_trials(self.p.name))

    def test_the_plan_adds_the_role_and_provider_label_and_blocks_what_runs_on_needs(self):
        by = {s.runner: s for s in self.steps()}
        self.assertEqual(by['ci-1'].add, ['provider-alpha'])
        self.assertEqual(by['ci-1'].remove, [])
        self.assertEqual(by['ci-1'].blocked, ['alpha'])
        self.assertIn('blocked until workflows migrate', by['ci-1'].action)
        self.assertEqual(by['ci-1'].target, ['linux', 'self-hosted', 'x64', 'heavy', 'provider-alpha'])
        # the stranded box: role and provider added, its stale label removed, on trial
        self.assertEqual(by['ci-h1'].add, ['heavy', 'provider-beta'])
        self.assertEqual(by['ci-h1'].remove, ['beta-heavy'])
        self.assertTrue(by['ci-h1'].trial)
        self.assertIn('trial: one heavy job', by['ci-h1'].action)
        table = ci_pool.plan_table(self.steps())
        self.assertTrue(table.splitlines()[0].startswith('runner'))
        self.assertIn('current labels', table.splitlines()[0])

    def test_a_dry_run_writes_nothing(self):
        self.steps()
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(ci_pool.load_trials(self.p.name), {})

    def test_apply_adds_everywhere_before_it_removes_anything(self):
        failed = ci_pool.apply(self.steps(), self.backend, self.p.name, out=lambda *_: None)
        self.assertEqual(failed, 0)
        kinds = [w[0] for w in self.backend.writes]
        self.assertEqual(kinds, sorted(kinds))          # every 'add' before the first 'remove'
        self.assertIn(('remove', 'ci-h1', 'beta-heavy'), self.backend.writes)
        self.assertNotIn('alpha', [w[2] for w in self.backend.writes if w[0] == 'remove'])
        self.assertEqual(set(ci_pool.load_trials(self.p.name)), {'ci-h1'})

    def test_once_workflows_ask_for_roles_the_provider_label_goes(self):
        self.backend._runs_on = [ro('self-hosted', 'heavy'), ro('self-hosted', 'light')]
        by = {s.runner: s for s in self.steps()}
        self.assertEqual(by['ci-1'].remove, ['alpha'])
        self.assertEqual(by['ci-1'].blocked, [])

    def test_an_undeclared_runner_is_left_alone(self):
        self.backend._runners['stray'] = runner('stray', 'whatever')
        by = {s.runner: s for s in self.steps()}
        self.assertEqual(by['stray'].action, 'not in ci.pool — untouched')
        ci_pool.apply(self.steps(), self.backend, self.p.name, out=lambda *_: None)
        self.assertFalse(any(w[1] == 'stray' for w in self.backend.writes))


def incident_pool():
    return [{'runner': 'h1', 'box': 'b1', 'provider': 'alpha', 'size': 'l', 'role': 'heavy'}]


INCIDENT_RUNS_ON_UNRESOLVED = [RunsOn('ci.yml', 'gate', None, frozenset({'CI_REQUIRED_LABEL'}))]


class AHeldRemoval(Home):
    """``plan`` holds what it cannot judge, and ``drift`` stops calling the runner stranded on a
    job it could not read — the incident's own fixture (F-0183), unresolved and resolved."""

    def setUp(self):
        super().setUp()
        self.p = product(incident_pool())
        self.runner = runner('h1', 'heavy', 'vendor-heavy', 'vendor', 'provider-alpha')

    def test_the_incident_apply_removes_nothing_when_the_variable_is_unresolved(self):
        backend = FakeBackend([self.runner], INCIDENT_RUNS_ON_UNRESOLVED)
        steps = ci_pool.plan(ci_pool.load_pool(self.p), backend.runners(), backend.runs_on())
        step = steps[0]
        self.assertEqual(step.remove, [])
        self.assertEqual(sorted(step.held), ['vendor', 'vendor-heavy'])
        self.assertIn('keep vendor, vendor-heavy — held: a runs-on names a variable this reader '
                      'could not resolve', step.action)
        failed = ci_pool.apply(steps, backend, self.p.name, out=lambda *_: None)
        self.assertEqual(failed, 0)
        self.assertEqual(backend.writes, [])

    def test_the_incident_apply_keeps_the_label_the_resolved_runs_on_uses(self):
        resolved = [ro('self-hosted', 'vendor-heavy', wf='ci.yml', job='gate')]
        backend = FakeBackend([self.runner], resolved)
        steps = ci_pool.plan(ci_pool.load_pool(self.p), backend.runners(), backend.runs_on())
        step = steps[0]
        self.assertEqual(step.blocked, ['vendor-heavy'])
        self.assertEqual(step.remove, ['vendor'])
        self.assertEqual(step.held, [])
        failed = ci_pool.apply(steps, backend, self.p.name, out=lambda *_: None)
        self.assertEqual(failed, 0)
        self.assertEqual(backend.writes, [('remove', 'h1', 'vendor')])

    def test_a_hold_never_stops_an_add_or_its_trial(self):
        bare = runner('h1')  # no role, no provider label yet
        backend = FakeBackend([bare], INCIDENT_RUNS_ON_UNRESOLVED)
        steps = ci_pool.plan(ci_pool.load_pool(self.p), backend.runners(), backend.runs_on())
        step = steps[0]
        self.assertEqual(step.add, ['heavy', 'provider-alpha'])
        self.assertTrue(step.trial)
        failed = ci_pool.apply(steps, backend, self.p.name, out=lambda *_: None)
        self.assertEqual(failed, 0)
        self.assertEqual(backend.writes, [('add', 'h1', ('heavy', 'provider-alpha'))])
        self.assertEqual(set(ci_pool.load_trials(self.p.name)), {'h1'})

    def test_a_stale_class_label_and_a_reserve_label_are_removed_through_a_hold(self):
        stray = runner('h1', 'heavy', 'provider-alpha', 'class-old', 'pr-heavy')
        backend = FakeBackend([stray], INCIDENT_RUNS_ON_UNRESOLVED)
        reserve = ci_pool.Reserve(label='pr-heavy', of='heavy', keep_free=1)
        steps = ci_pool.plan(ci_pool.load_pool(self.p), backend.runners(), backend.runs_on(),
                             reserves=[reserve])
        step = steps[0]
        self.assertEqual(sorted(step.remove), ['class-old', 'pr-heavy'])
        self.assertEqual(step.held, [])

    def test_the_doctor_names_the_variable_and_does_not_call_the_runner_stranded(self):
        why = 'gh api repos: HTTP 403 (Resource not accessible by integration)'
        got = ci_pool.drift(ci_pool.load_pool(self.p), [self.runner], INCIDENT_RUNS_ON_UNRESOLVED,
                            why=why)
        self.assertEqual(got, [(False, "runs-on unresolved: ci.yml:gate asks for "
                                       "vars.CI_REQUIRED_LABEL — " + why +
                                       "; no label is removed while it stands "
                                       "(a stale class- label aside)")])
        self.assertFalse(any(d.startswith('stranded:') for _ok, d in got))

    def test_a_resolved_pool_is_judged_exactly_as_before(self):
        runners = [runner('ci-1', 'alpha', 'heavy'), runner('ci-1b', 'alpha', 'light'),
                  runner('ci-h1', 'beta-heavy')]
        runs_on = [ro('self-hosted', 'alpha', 'heavy'), ro('self-hosted', 'alpha')]
        steps = ci_pool.plan(ci_pool.load_pool(product()), runners, runs_on)
        self.assertTrue(all(s.held == [] for s in steps))
        by = {s.runner: s for s in steps}
        self.assertEqual(by['ci-1'].add, ['provider-alpha'])
        self.assertEqual(by['ci-1'].remove, [])
        self.assertEqual(by['ci-1'].blocked, ['alpha'])


class Trials(Home):
    def setUp(self):
        super().setUp()
        self.p = product()
        self.backend = FakeBackend([runner('ci-1', 'heavy'), runner('ci-1b', 'light'),
                                    runner('ci-h1', 'beta-heavy')],
                                   [ro('self-hosted', 'heavy'), ro('self-hosted', 'light')])
        steps = ci_pool.plan(ci_pool.load_pool(self.p), self.backend.runners(),
                             self.backend.runs_on())
        ci_pool.apply(steps, self.backend, self.p.name, out=lambda *_: None)
        self.since = ci_pool.load_trials(self.p.name)['ci-h1']['since']
        self.backend.writes.clear()

    def job(self, status='completed', conclusion='success', name='e2e'):
        return {'name': name, 'status': status, 'conclusion': conclusion,
                'url': f'https://ci/{name}', 'started_at': self.since}

    def test_no_job_yet_waits(self):
        got = ci_pool.check_trials(self.p.name, self.backend, apply_changes=True, out=lambda *_: None)
        self.assertEqual([v for _n, v, _d in got], ['waiting'])

    def test_a_running_job_withdraws_the_role_so_only_one_job_lands(self):
        self.backend.jobs['ci-h1'] = [self.job(status='in_progress', conclusion=None)]
        ci_pool.check_trials(self.p.name, self.backend, apply_changes=True, out=lambda *_: None)
        self.assertEqual(self.backend.writes, [('remove', 'ci-h1', 'heavy')])
        self.backend.jobs['ci-h1'] = [self.job()]
        got = ci_pool.check_trials(self.p.name, self.backend, apply_changes=True, out=lambda *_: None)
        self.assertEqual([v for _n, v, _d in got], ['passed'])
        self.assertEqual(self.backend.writes[-1], ('add', 'ci-h1', ('heavy',)))
        self.assertEqual(ci_pool.load_trials(self.p.name), {})

    def test_a_passed_trial_keeps_the_runner(self):
        self.backend.jobs['ci-h1'] = [self.job(conclusion='cancelled', name='x'), self.job()]
        got = ci_pool.check_trials(self.p.name, self.backend, apply_changes=True, out=lambda *_: None)
        self.assertEqual(got[0][1], 'passed')
        self.assertEqual(self.backend.writes, [])
        self.assertIn('heavy', self.backend._runners['ci-h1'].norm_labels())

    def test_a_failed_trial_rolls_back_and_the_tick_files_one_bug(self):
        self.backend.jobs['ci-h1'] = [self.job(conclusion='failure')]
        got = ci_pool.check_trials(self.p.name, self.backend, apply_changes=True, out=lambda *_: None)
        self.assertEqual(got[0][1], 'failed')
        self.assertEqual(self.backend._runners['ci-h1'].norm_labels(),
                         {'self-hosted', 'linux', 'x64', 'beta-heavy'})
        self.assertEqual(ci_pool.load_trials(self.p.name)['ci-h1']['state'], 'rolled_back')

        filed = []

        class Ctx:
            product = self.p

            def record_root(self_inner):
                raise AssertionError('patched below')

        from unittest import mock
        from asf.tick import file_bugs

        def fake_file(root, canonical, sig, info, date, default_bug_epic=None):
            filed.append((sig, info['severity'], info['runs']))
            return 'filed'

        root = tempfile.mkdtemp(dir=self.tmp)
        with mock.patch.object(file_bugs, '_file_or_bump_bug', fake_file), \
                mock.patch('asf.approvals.level_of', return_value='auto'), \
                mock.patch('asf.record.index.do_index'), \
                mock.patch.object(Ctx, 'record_root', lambda self_inner: root):
            ci_pool.tick(Ctx(), backend=self.backend, out=lambda *_: None)
        self.assertEqual(filed, [('ci trial failed: ci-h1 as heavy', 'S2', ['https://ci/e2e'])])
        self.assertEqual(ci_pool.load_trials(self.p.name), {})

    def test_a_dry_run_judges_but_writes_nothing(self):
        self.backend.jobs['ci-h1'] = [self.job(conclusion='failure')]
        got = ci_pool.check_trials(self.p.name, self.backend, apply_changes=False)
        self.assertEqual(got[0][1], 'failed')
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(ci_pool.load_trials(self.p.name)['ci-h1']['state'], 'pending')

    def test_a_runner_on_trial_is_not_trialled_again(self):
        steps = ci_pool.plan(ci_pool.load_pool(self.p), self.backend.runners(),
                             self.backend.runs_on(), trials=ci_pool.load_trials(self.p.name))
        self.assertFalse(any(s.trial for s in steps))


class CapacityFromPool(Home):
    def test_the_pool_slots_are_the_ci_ceiling(self):
        self.assertEqual(capacity.product_ci(product(), {}), (4, 'ci.pool: heavy 3, light 1'))

    def test_an_explicit_capacity_ci_overrides_the_pool_and_says_so(self):
        self.assertEqual(capacity.product_ci(product(cap={'ci': 2}), {}),
                         (2, 'product (overrides ci.pool 4)'))

    def test_the_pool_beats_the_operator_default(self):
        cfg = {'capacity': {'per_product': {'ci': 9}}}
        self.assertEqual(capacity.product_ci(product(), cfg)[0], 4)
        self.assertEqual(capacity.product_ci(product(pool=[]), cfg), (9, 'operator default'))

    def test_resolve_and_the_view_name_the_source(self):
        from asf.views import capacity as view

        class Src:
            def read(self, product):
                return 1
        r = capacity.resolve(product(), {}, ci_source=Src())
        self.assertEqual((r.ci, r.ci_bound, r.ci_inflight), (4, 'ci.pool: heavy 3, light 1', 1))
        from unittest import mock
        with mock.patch.object(capacity, 'resolve', return_value=r), \
                mock.patch.object(capacity, 'inflight_sessions', return_value=0):
            text = view.render([product()], {})
        self.assertIn('ci from', text)
        self.assertIn('ci.pool: heavy 3, light 1', text)


def classed_pool():
    return [
        {'runner': 'ci-1', 'provider': 'alpha', 'role': 'heavy', 'slots': 2, 'class': 'heavy-fast'},
        {'runner': 'ci-2', 'provider': 'alpha', 'role': 'heavy', 'class': 'heavy-fast'},
        {'runner': 'ci-h1', 'provider': 'beta', 'role': 'heavy', 'class': 'heavy-slow'},
        {'runner': 'ci-1b', 'provider': 'alpha', 'role': 'light'},
    ]


class RunnerClass(Home):
    """``class:`` per runner: a derived ``class:<name>`` label, the doctor's counts and warning,
    and the capacity view's grouping — against a fake CI host."""

    def setUp(self):
        super().setUp()
        self.p = product(classed_pool())
        self.backend = FakeBackend(
            [runner('ci-1', 'heavy', 'provider-alpha'), runner('ci-2', 'heavy', 'provider-alpha',
                                                             'class-heavy-slow'),
             runner('ci-h1', 'heavy', 'provider-beta'), runner('ci-1b', 'light', 'provider-alpha')],
            [ro('self-hosted', 'heavy'), ro('self-hosted', 'light')])

    def steps(self):
        return ci_pool.plan(ci_pool.load_pool(self.p), self.backend.runners(), self.backend.runs_on(),
                            trials=ci_pool.load_trials(self.p.name))

    def test_class_is_validated_as_one_label(self):
        self.assertEqual(ci_pool.pool_problems({'pool': classed_pool()}), [])
        bad = [{'runner': 'a', 'provider': 'x', 'role': 'heavy', 'class': 'heavy fast'}]
        self.assertEqual([k for k, _w in ci_pool.pool_problems({'pool': bad})], ['ci.pool[0].class'])

    def test_reconcile_plans_the_class_label_and_drops_a_stale_one_dry_run(self):
        by = {s.runner: s for s in self.steps()}
        self.assertEqual(by['ci-1'].add, ['class-heavy-fast'])
        self.assertEqual(by['ci-2'].add, ['class-heavy-fast'])
        self.assertEqual(by['ci-2'].remove, ['class-heavy-slow'])
        self.assertEqual(by['ci-h1'].add, ['class-heavy-slow'])
        self.assertEqual((by['ci-1b'].add, by['ci-1b'].remove), ([], []))   # unclassed: untouched
        self.assertFalse(any(s.trial for s in by.values()))                 # a class is no new role
        self.assertEqual(self.backend.writes, [])

    def test_apply_writes_the_class_labels(self):
        self.assertEqual(ci_pool.apply(self.steps(), self.backend, self.p.name, out=lambda *_: None), 0)
        labels = {n: r.norm_labels() for n, r in self.backend._runners.items()}
        self.assertIn('class-heavy-fast', labels['ci-2'])
        self.assertNotIn('class-heavy-slow', labels['ci-2'])
        self.assertIn('class-heavy-slow', labels['ci-h1'])
        self.assertEqual([s.action for s in self.steps()], ['ok'] * 4)

    def test_doctor_counts_classes_and_warns_on_an_unclassed_runner(self):
        rows = ci_pool.doctor_rows(self.p, backend=self.backend)
        self.assertIn((False, True, 'classes: heavy-fast 2 runners, heavy-slow 1 runner'), rows)
        self.assertIn((False, False, 'class: ci-1b declares no class while others do — '
                                     'RUNNER_CLASS is empty on it'), rows)
        self.assertTrue(any(req and not ok and d.startswith("class label: ci-2 should carry "
                                                            "'class-heavy-fast'")
                            for req, ok, d in rows), rows)

    def test_no_class_anywhere_adds_no_rows(self):
        self.assertEqual(ci_pool.class_rows(ci_pool.load_pool(product())), [])

    def test_the_capacity_view_groups_runners_by_class(self):
        from unittest import mock
        from asf.views import capacity as view

        class Src:
            def read(self, product):
                return 0
        r = capacity.resolve(self.p, {}, ci_source=Src())
        with mock.patch.object(capacity, 'resolve', return_value=r), \
                mock.patch.object(capacity, 'inflight_sessions', return_value=0):
            text = view.render([self.p], {})
            data = view.as_json([self.p], {})
        self.assertIn('ci classes: p heavy-fast 2 runners/3 slots [ci-1, ci-2]; heavy-slow 1 runner/1 '
                      'slots [ci-h1]; (no class) 1 runner/1 slots [ci-1b]', text)
        self.assertEqual([g['class'] for g in data[0]['ci']['classes']],
                         ['heavy-fast', 'heavy-slow', None])


if __name__ == '__main__':
    unittest.main()
