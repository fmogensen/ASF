"""asf.ci_pool — the declared runner pool against a fake CI host: validation, the doctor's drift
rows, the reconcile plan and its add-before-remove order, a trial's pass and rollback, and the CI
ceiling the pool sets. No test shells out: the host is :class:`FakeBackend`."""
import os
import shutil
import tempfile
import unittest

from asf import capacity, ci_pool, env
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


class RunsOnParser(unittest.TestCase):
    def test_flow_scalar_block_and_expressions(self):
        text = '\n'.join([
            'on: push', 'jobs:', '  gate:',
            '    runs-on: [self-hosted, "${{ vars.X || \'heavy\' }}"]  # comment',
            '    steps:', '      - run: echo', '  hosted:', '    runs-on: ubuntu-latest',
            '  mat:', '    runs-on: ${{ matrix.os }}', '  blk:', '    runs-on:',
            '      - self-hosted', '      - light', ''])
        got = {r.job: r.labels for r in ci_pool.parse_runs_on('ci.yml', text)}
        self.assertEqual(got, {'gate': frozenset({'self-hosted', 'heavy'}),
                               'hosted': frozenset({'ubuntu-latest'}), 'mat': None,
                               'blk': frozenset({'self-hosted', 'light'})})


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


if __name__ == '__main__':
    unittest.main()
