"""asf.ci_pool's ``ci.reserve`` against fake runners: which runners carry the PR-only label (all
but ``keep_free`` of those carrying ``of``, the free ones spread one per box, ``prefer`` first),
idempotence, re-balancing as runners come and go, add-before-remove, the reconcile plan and the
doctor leaving the label alone, the status Runners row, and the CI queue sizing a PR start on
the labelled runners only while the trunk sees them all. No test shells out."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import ci_pool, ci_queue, env
from asf.ci_pool import Runner

BASE = ['self-hosted', 'Linux', 'X64']
LABEL = 'class-pr-heavy'


def runner(name, *labels, online=True, busy=False):
    return Runner(name=name, online=online, labels=BASE + list(labels), id=name, busy=busy,
                  fixed=frozenset(l.lower() for l in BASE))


# a product's shape: 7 alpha-heavy runners on 5 boxes (two boxes hold two), 5 beta-heavy
# on their own boxes, light runners beside them.
HEAVY = [('ci-1', 'box-1', 'alpha-heavy'), ('ci-2', 'box-2', 'alpha-heavy'),
         ('ci-3', 'box-3', 'alpha-heavy'), ('ci-4', 'box-4', 'alpha-heavy'),
         ('ci-4b', 'box-4', 'alpha-heavy'), ('ci-5', 'box-5', 'alpha-heavy'),
         ('ci-5b', 'box-5', 'alpha-heavy')] + \
        [(f'ci-h{i}', f'hbox-{i}', 'beta-heavy') for i in range(1, 6)]


def pool_data():
    return ([{'runner': n, 'box': b, 'provider': 'x', 'role': 'heavy'} for n, b, _l in HEAVY]
            + [{'runner': 'ci-1b', 'box': 'box-1', 'provider': 'x', 'role': 'light'}])


def hosts(**extra):
    """The live runners; ``extra``: runner name → labels added on top."""
    out = [runner(n, 'heavy', sub, *extra.get(n, ())) for n, _b, sub in HEAVY]
    return out + [runner('ci-1b', 'light', *extra.get('ci-1b', ()))]


def product(reserve=None, pool=None, name='p'):
    ci = {'provider': 'github-actions', 'workflow': 'ci.yml',
          'pool': pool_data() if pool is None else pool}
    ci['reserve'] = ({'label': LABEL, 'of': 'heavy', 'keep_free': 3, 'prefer': 'alpha-heavy'}
                     if reserve is None else reserve)
    return env.Product(name, {'repo_slug': 'o/r', 'ci': ci})


class FakeBackend(ci_pool.Backend):
    def __init__(self, runners, runs_on=()):
        self._runners = {r.name: r for r in runners}
        self._runs_on = list(runs_on)
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


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        self.lines = []

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def labelled(self, backend):
        return sorted(r.name for r in backend.runners() if LABEL in r.norm_labels())


class Validation(unittest.TestCase):
    def test_a_good_reserve_validates_and_loads(self):
        p = product()
        self.assertEqual(ci_pool.reserve_problems(p.ci), [])
        self.assertEqual(ci_pool.load_reserve(p), [ci_pool.Reserve(LABEL, 'heavy', 3, 'box',
                                                                   'alpha-heavy')])
        self.assertEqual(ci_pool.reserve_labels(p), {LABEL})

    def test_bad_fields_are_named(self):
        bad = {'label': 'heavy', 'of': 'heavy', 'keep_free': -1, 'spread_by': 'rack', 'x': 1}
        keys = {k for k, _w in ci_pool.reserve_problems({'reserve': bad})}
        self.assertEqual(keys, {'ci.reserve.label', 'ci.reserve.keep_free',
                                'ci.reserve.spread_by', 'ci.reserve.x'})
        self.assertEqual(ci_pool.load_reserve(product(reserve=bad)), [])
        self.assertTrue(ci_pool.reserve_problems({'reserve': 'yes'}))
        self.assertEqual({k for k, _w in ci_pool.reserve_problems({'reserve': [{}]})},
                         {'ci.reserve[0].label', 'ci.reserve[0].of', 'ci.reserve[0].keep_free'})

    def test_the_product_file_accepts_ci_reserve(self):
        text = ('repo_slug: o/r\nci:\n  provider: github-actions\n  reserve: {label: class-pr, '
                'of: heavy, keep_free: 2}\n')
        self.assertEqual(env.validate_product_text(text), [])
        text = text.replace('keep_free: 2', 'keep_free: two')
        self.assertTrue(any('keep_free' in str(p) for p in env.validate_product_text(text)))


class Plan(unittest.TestCase):
    def plan(self, runners, **kw):
        p = product(**kw)
        return ci_pool.reserve_plan(ci_pool.load_reserve(p)[0], ci_pool.load_pool(p), runners)

    def test_nine_of_twelve_carry_it_three_kept_free_one_per_box_preferring_alpha(self):
        p = self.plan(hosts())
        self.assertEqual(len(p.candidates), 12)
        self.assertEqual(len(p.labeled), 9)
        # the two-runner boxes first (each keeps one for PRs), then a single one; last name first
        self.assertEqual(p.reserved, ['ci-3', 'ci-4b', 'ci-5b'])
        self.assertEqual(sorted(p.add), sorted(p.labeled))
        self.assertEqual(p.remove, [])
        self.assertEqual(p.text(), 'pr-heavy 9/12 (3 reserved for main)')

    def test_without_prefer_the_free_ones_still_sit_on_distinct_boxes(self):
        p = self.plan(hosts(), reserve={'label': LABEL, 'of': 'heavy', 'keep_free': 3})
        boxes = {dict((n, b) for n, b, _l in HEAVY)[n] for n in p.reserved}
        self.assertEqual(len(boxes), 3)

    def test_spread_none_ignores_boxes(self):
        p = self.plan(hosts(), reserve={'label': LABEL, 'of': 'heavy', 'keep_free': 2,
                                        'spread_by': 'none', 'prefer': 'alpha-heavy'})
        self.assertEqual(p.reserved, ['ci-5', 'ci-5b'])  # one box may hold both

    def test_settled_runners_give_an_empty_plan(self):
        first = self.plan(hosts())
        after = hosts(**{n: [LABEL] for n in first.labeled})
        again = self.plan(after)
        self.assertTrue(again.settled)
        self.assertEqual(again.reserved, first.reserved)

    def test_an_existing_free_runner_stays_free_no_churn(self):
        # the operator's current split keeps ci-1 free instead of ci-3
        free = {'ci-1', 'ci-4b', 'ci-5b'}
        now = hosts(**{n: [LABEL] for n, _b, _l in HEAVY if n not in free})
        p = self.plan(now)
        self.assertTrue(p.settled)
        self.assertEqual(set(p.reserved), free)

    def test_a_runner_going_offline_rebalances(self):
        first = self.plan(hosts())
        now = hosts(**{n: [LABEL] for n in first.labeled})
        for r in now:
            if r.name == 'ci-4b':
                r.online = False
        p = self.plan(now)
        self.assertEqual(len(p.candidates), 11)
        self.assertEqual(len(p.reserved), 3)
        self.assertEqual(len(p.labeled), 8)
        self.assertEqual(len(p.remove), 1)                 # one more kept free
        self.assertNotIn('ci-4b', p.add + p.remove)  # an offline runner is not touched
        self.assertEqual(len({dict((n, b) for n, b, _l in HEAVY)[n] for n in p.reserved}), 3)

    def test_fewer_runners_than_keep_free_labels_none(self):
        p = self.plan([runner('a', 'heavy'), runner('b', 'heavy')])
        self.assertEqual(p.labeled, [])
        self.assertEqual(p.reserved, ['a', 'b'])

    def test_the_label_on_a_runner_without_of_comes_off(self):
        p = self.plan(hosts(**{'ci-1b': [LABEL]}))
        self.assertIn('ci-1b', p.remove)

    def test_pr_runners_leaves_out_the_kept_free(self):
        prod = product()
        now = hosts(**{n: [LABEL] for n in self.plan(hosts()).labeled})
        names = {r.name for r in ci_pool.pr_runners(now, ci_pool.load_reserve(prod))}
        self.assertEqual(len(names), 10)                   # 9 heavy + the light one
        self.assertNotIn('ci-3', names)
        self.assertIn('ci-1b', names)


class Apply(Home):
    def test_the_tick_applies_it_once_and_is_idempotent(self):
        p = product()
        b = FakeBackend(hosts())
        ci_pool.tick_reserve(p, b, out=self.lines.append)
        self.assertEqual(len(self.labelled(b)), 9)
        self.assertEqual(len(b.writes), 9)
        self.assertIn('ci reserve: pr-heavy 9/12 (3 reserved for main)', self.lines)
        n = len(b.writes)
        plans = ci_pool.tick_reserve(p, b, out=self.lines.append)
        self.assertEqual(len(b.writes), n)                 # nothing more to write
        self.assertTrue(plans[0].settled)

    def test_adds_come_before_removes(self):
        p = product()
        # every heavy runner labelled: three must come off; a runner came online without it
        now = hosts(**{n: [LABEL] for n, _b, _l in HEAVY if n != 'ci-h5'})
        b = FakeBackend(now)
        plans = ci_pool.reserve_plans(p, b.runners())
        ci_pool.apply_reserve(plans, b, b.runners(), out=self.lines.append)
        kinds = [w[0] for w in b.writes]
        self.assertEqual(kinds, sorted(kinds))
        self.assertEqual(len(self.labelled(b)), 9)

    def test_the_tick_writes_nothing_when_the_host_is_unreadable(self):
        class Dead(FakeBackend):
            def runners(self):
                raise ci_pool.BackendError('down')
        self.assertEqual(ci_pool.tick_reserve(product(), Dead([]), out=self.lines.append), [])
        self.assertIn('cannot read the CI host', self.lines[-1])

    def test_cmd_reserve_dry_run_writes_nothing_and_apply_writes(self):
        import argparse
        p = product(name='p')
        with open(os.path.join(self.tmp, 'products', 'p.yaml'), 'w') as f:
            f.write('repo_slug: o/r\nci:\n  provider: github-actions\n  pool:\n'
                    + ''.join(f"    - {{runner: {e['runner']}, box: {e['box']}, provider: x, "
                              f"role: {e['role']}}}\n" for e in pool_data())
                    + '  reserve: {label: class-pr-heavy, of: heavy, keep_free: 3, '
                      'prefer: alpha-heavy}\n')
        b = FakeBackend(hosts())
        rc = ci_pool.cmd_reserve(argparse.Namespace(product='p', apply=False), backend=b,
                                 out=self.lines.append)
        self.assertEqual((rc, b.writes), (0, []))
        self.assertIn('trunk only (reserved)', '\n'.join(self.lines))
        rc = ci_pool.cmd_reserve(argparse.Namespace(product='p', apply=True), backend=b,
                                 out=self.lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.labelled(b)), 9)
        del p


class ReconcileAndDoctor(Home):
    def test_reconcile_keeps_the_reserve_label_where_it_belongs(self):
        p = product()
        first = ci_pool.reserve_plans(p, hosts())[0]
        now = hosts(**{n: [LABEL] for n in first.labeled})
        runs_on = [ci_pool.RunsOn('ci.yml', 'pr', frozenset({'self-hosted', 'heavy', LABEL})),
                   ci_pool.RunsOn('ci.yml', 'main', frozenset({'self-hosted', 'heavy'}))]
        steps = ci_pool.plan(ci_pool.load_pool(p), now, runs_on, reserves=ci_pool.load_reserve(p))
        for s in steps:
            self.assertNotIn(LABEL, s.remove + s.add + s.blocked, s.runner)
        # a runner that should be free loses it — never "blocked" by the PR runs-on
        now = hosts(**{n: [LABEL] for n, _b, _l in HEAVY})
        steps = {s.runner: s for s in ci_pool.plan(ci_pool.load_pool(p), now, runs_on,
                                                   reserves=ci_pool.load_reserve(p))}
        self.assertIn(LABEL, steps['ci-3'].remove)
        self.assertEqual(steps['ci-3'].blocked, [])

    def test_the_doctor_does_not_call_it_a_stray_class_and_reports_the_split(self):
        p = product()
        first = ci_pool.reserve_plans(p, hosts())[0]
        b = FakeBackend(hosts(**{n: [LABEL] for n in first.labeled}),
                        [ci_pool.RunsOn('ci.yml', 'j', frozenset({'self-hosted', 'heavy'})),
                         ci_pool.RunsOn('ci.yml', 'k', frozenset({'self-hosted', 'light'}))])
        rows = ci_pool.doctor_rows(p, backend=b)
        text = '\n'.join(d for _r, _ok, d in rows)
        self.assertNotIn('class label:', text)
        self.assertIn((False, True, 'reserve: pr-heavy 9/12 (3 reserved for main)'), rows)
        b2 = FakeBackend(hosts(), b.runs_on())
        row = [r for r in ci_pool.doctor_rows(p, backend=b2) if r[2].startswith('reserve:')][0]
        self.assertFalse(row[1])
        self.assertIn('the tick applies it', row[2])

    def test_runners_row_names_the_split(self):
        p = product()
        first = ci_pool.reserve_plans(p, hosts())[0]
        now = hosts(**{n: [LABEL] for n in first.labeled})
        text = ci_queue.runners_text(now, ci_pool.load_pool(p), product=p)
        self.assertTrue(text.endswith('pr-heavy 9/12 (3 reserved for main)'), text)
        self.assertNotIn('pr-heavy', ci_queue.runners_text(now, ci_pool.load_pool(p)))


# ---- the queue ---------------------------------------------------------------------------------

def qproduct():
    ci = {'provider': 'github-actions', 'workflow': 'ci.yml',
          'pool': [{'runner': f'h{i}', 'provider': 'a', 'role': 'heavy'} for i in range(1, 5)]
          + [{'runner': 'l1', 'provider': 'a', 'role': 'light'}],
          'reserve': {'label': 'class-pr', 'of': 'heavy', 'keep_free': 1}}
    return env.Product('p', {'repo_slug': 'o/r', 'ci': ci})


class QGh:
    """``gh``: four heavy runners, h1..h3 carrying ``class-pr``; three past runs asking for all
    four heavy at once."""

    def __init__(self, busy=()):
        self.busy = set(busy)

    def __call__(self, argv, **_kw):
        out = ''
        if argv[:2] == ['gh', 'api'] and any('actions/runners' in a for a in argv):
            rows = []
            for i, name in enumerate(['h1', 'h2', 'h3', 'h4', 'l1']):
                labels = ['self-hosted', 'light' if name == 'l1' else 'heavy']
                labels += ['class-pr'] if name in ('h1', 'h2', 'h3') else []
                rows.append({'name': name, 'id': i, 'busy': name in self.busy, 'status': 'online',
                             'labels': [{'name': l} for l in labels]})
            out = '\n'.join(json.dumps(r) for r in rows)
        elif argv[:3] == ['gh', 'run', 'list'] and '--status' in argv:
            out = json.dumps([{'databaseId': i, 'conclusion': 'success', 'attempt': 1}
                              for i in (1, 2, 3)])
        elif argv[:3] == ['gh', 'run', 'list']:
            out = '0'
        elif argv[:2] == ['gh', 'api'] and any('/jobs' in a for a in argv):
            t = '2026-09-25T11:00:00Z'
            out = '\n'.join(json.dumps({'id': k, 'runner_name': f'h{k}', 'conclusion': 'success',
                                        'created_at': t, 'started_at': t,
                                        'completed_at': '2026-09-25T11:10:00Z'})
                            for k in range(1, 5))
        return subprocess.CompletedProcess(argv, 0, out, '')


class Queue(Home):
    def q(self, gh):
        p = qproduct()
        return ci_queue.Queue(p, source=ci_queue.GitHubSource(p, run=gh), out=self.lines.append,
                              now=datetime.datetime(2026, 9, 25, 12, 0,
                                                    tzinfo=datetime.timezone.utc))

    def test_a_pr_sees_the_labelled_runners_the_trunk_sees_all(self):
        q = self.q(QGh())
        self.assertEqual(q.free(), {'heavy': 4, 'light': 1})
        self.assertEqual(q.free('pr'), {'heavy': 3, 'light': 1})
        self.assertEqual(q.entry_needs({'kind': 'trunk', 'workflow': 'ci.yml'}), {'heavy': 4})
        # a PR run can never be sized past the runners it may land on
        self.assertEqual(q.entry_needs({'kind': 'pr', 'workflow': 'ci.yml'}), {'heavy': 3})

    def test_a_pr_starts_while_only_the_reserved_runner_is_busy(self):
        d = ci_queue.admit(qproduct(), 'pr:a', 'pr', item='T-1', queue=self.q(QGh(busy={'h4'})))
        self.assertTrue(d.admitted)

    def test_a_pr_waits_when_a_labelled_runner_is_busy(self):
        d = ci_queue.admit(qproduct(), 'pr:b', 'pr', item='T-2', queue=self.q(QGh(busy={'h1'})))
        self.assertFalse(d.admitted)
        self.assertIn('heavy 2 free, needs 3', self.lines[-1])




class Flaky(unittest.TestCase):
    def test_the_reserve_label_is_no_runner_class(self):
        from asf.tick import flaky
        labels = ['self-hosted', 'heavy', LABEL]
        self.assertEqual(flaky._runner_class(labels, {LABEL}), 'heavy')
        self.assertEqual(flaky._runner_class(labels), 'pr-heavy')   # unconfigured: as before


if __name__ == '__main__':
    unittest.main()
