"""The tick's asf steps — health, wave, prs, daily — and the ordered tick around them.

Workers, feeder, briefs and ``gh`` are stubbed (module attributes); git is real: a bare origin
for the record (``TickTestCase``) and a bare origin + clone for the product repo.
"""
import json
import os
import types
import unittest
from unittest import mock

from asf import env
from asf.feeder import rows as feeder_rows
from asf.metrics import metrics
from asf.tick import shadow, step_daily, step_health, step_prs, step_wave, steps, tick
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from tests.test_tick import TickTestCase, _git

DEAD_PID = 999999
CARD = ('---\nid: B-0001\ntype: bug\ntitle: the first bug\n---\n## Description\nx\n\n'
        '## Acceptance\n- [ ] the named test passes\n- [x] no regression\n\n## History\n- made\n')
INDEX = {'generated': '', 'items': {
    'B-0001': {'id': 'B-0001', 'type': 'bug', 'title': 'the first bug', 'folder': 'bugs',
               'severity': 'S1', 'state': 'New', 'decided': True},
    'F-0001': {'id': 'F-0001', 'type': 'feature', 'title': 'sample', 'folder': 'features'},
}}


class StepsTestCase(TickTestCase):
    """The record origin gains an index and a card; a product repo with its own bare origin."""

    product_extra = 'steps:\n  batch: off\n'

    def setUp(self):
        super().setUp()
        seed = os.path.join(self.tmp, 'seed')
        os.makedirs(os.path.join(seed, 'bugs'))
        with open(os.path.join(seed, 'bugs', 'B-0001.md'), 'w') as f:
            f.write(CARD)
        with open(os.path.join(seed, 'index.json'), 'w') as f:
            json.dump(INDEX, f)
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'index'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)

        self.repo_origin = os.path.join(self.tmp, 'repo.git')
        self.repo = os.path.join(self.tmp, 'repo')
        _git(['init', '-q', '--bare', '-b', 'main', self.repo_origin])
        _git(['clone', '-q', self.repo_origin, self.repo])
        _git(['config', 'user.email', 'r@example.com'], self.repo)
        _git(['config', 'user.name', 'r'], self.repo)
        with open(os.path.join(self.repo, 'README'), 'w') as f:
            f.write('r\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'init'], self.repo)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.write_product(f'repo_dir: {self.repo}\n{self.product_extra}')
        self.product = env.load_product('sample')
        self.lines = []

    def push_branch(self, branch):
        _git(['checkout', '-q', '-b', branch, 'main'], self.repo)
        with open(os.path.join(self.repo, branch.replace('/', '_')), 'w') as f:
            f.write(branch + '\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', f'work on {branch}'], self.repo)
        _git(['push', '-q', 'origin', branch], self.repo)
        _git(['checkout', '-q', 'main'], self.repo)

    def session(self, **rec):
        pool_mod.append_session(self.product, rec)

    def ctx(self):
        return tick.Context(self.product)

    def events(self, ctx):
        d = os.path.join(ctx.record_root(), 'metrics', 'events')
        out = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, name)) as f:
                out += [json.loads(ln) for ln in f if ln.strip()]
        return out


# ---- the ordered tick -------------------------------------------------------------

class OrderedTickTests(StepsTestCase):
    product_extra = 'steps:\n  batch: python3 -c \'print("batch ran")\'\n'

    def test_a_failing_step_never_stops_the_rest_and_exits_1(self):
        ran = []

        def boom(ctx):
            ran.append('health')
            raise RuntimeError('health blew up\nsecond line')

        def ok(name):
            def run(ctx):
                ran.append(name)
                ctx.record_root()
                return 0
            return run
        with mock.patch.object(step_health, 'run', boom), \
                mock.patch.object(step_wave, 'run', ok('wave')), \
                mock.patch.object(step_prs, 'run', ok('prs')), \
                mock.patch.object(step_daily, 'run', ok('daily')), \
                mock.patch.object(shadow, 'ensure_clone', wraps=shadow.ensure_clone) as clone:
            rc, out = self.run_tick()
        self.assertEqual(rc, 1)
        self.assertEqual(ran, ['health', 'wave', 'prs', 'daily'])
        lines = out.splitlines()
        self.assertEqual(lines[0], f'tick: state committed and pushed ({self.record_path()})')
        self.assertEqual(lines[1], '[step:health] FAILED health blew up')
        self.assertEqual(lines[2], '[command:batch] batch ran')
        self.assertEqual(clone.call_count, 1)  # one record clone per tick, however many steps

        day = _git(['log', '-1', '--format=%cs', 'main'], self.origin)
        tick_log = _git(['show', f'main:metrics/ticks/{day}.jsonl'], self.origin)
        line = json.loads(tick_log.splitlines()[-1])
        self.assertEqual([(s['step'], s['ok']) for s in line['steps']],
                         [('record', True), ('health', False), ('wave', True), ('prs', True),
                          ('batch', True), ('daily', True)])
        self.assertEqual(line['product'], 'sample')
        # the line keeps the ticks stream's schema: the scorecard reads it without a KeyError
        metrics.scorecard_rows([], [], [line])

    def test_daily_stamped_only_when_it_passed(self):
        with mock.patch.object(step_daily, 'run', side_effect=RuntimeError('daily parts failed: rollup')):
            rc, out = self.run_tick(steps='daily')
        self.assertEqual(rc, 1)
        self.assertIn('[step:daily] FAILED daily parts failed: rollup', out)
        self.assertFalse(os.path.exists(steps.stamp_path(self.product)))

    def test_command_steps_alone_do_not_clone_the_record_to_log(self):
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertEqual(out, '[command:batch] batch ran\n')
        self.assertFalse(os.path.exists(self.record_path()))


# ---- health -----------------------------------------------------------------------

class HealthStepTests(StepsTestCase):
    def dead_session(self, **extra):
        log = os.path.join(self.tmp, 'job.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'system'}) + '\n')
        brief = os.path.join(self.tmp, 'job.md')
        with open(brief, 'w') as f:
            f.write('do the thing\n')
        self.session(job='fix-b-0001', item='B-0001', pid=DEAD_PID, log=log, brief=brief,
                     model='opus', account=None, **extra)

    def test_dead_session_is_corrected_first(self):
        self.dead_session()
        fake = runtime_mod.FakeRuntime([{'ok': True}])
        ctx = self.ctx()
        step_health.run(ctx, out=self.lines.append, runtime_fn=lambda: fake)
        self.assertIn('ended     fix-b-0001               dead pid', self.lines)
        self.assertIn('DEAD  fix-b-0001               corrected in the same session', self.lines)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn('CORRECTION: the step failed with:', fake.calls[0][1])
        self.assertEqual(self.events(ctx), [])

    def test_already_corrected_is_one_needs_operator_event(self):
        self.dead_session(corrected=1)
        ctx = self.ctx()
        step_health.run(ctx, out=self.lines.append, runtime_fn=lambda: self.fail('no rerun'))
        evs = self.events(ctx)
        self.assertEqual([e['kind'] for e in evs], ['needs-operator'])
        self.assertTrue(evs[0]['text'].startswith('NEEDS OPERATOR: session fix-b-0001'))
        self.assertTrue(any(ln.startswith('NEEDS OPERATOR:') for ln in self.lines))
        # the next tick does not raise it again
        self.assertEqual(step_health.handle_dead(
            ctx, dict(pool_mod.load_sessions(self.product)['fix-b-0001']), out=self.lines.append),
            'flagged')

    def test_failed_correction_raises_the_operator(self):
        self.dead_session()
        fake = runtime_mod.FakeRuntime([{'ok': False, 'result': 'still broken'}])
        ctx = self.ctx()
        step_health.run(ctx, out=self.lines.append, runtime_fn=lambda: fake)
        self.assertEqual([e['kind'] for e in self.events(ctx)], ['needs-operator'])

    def test_stall_dead_counts_and_clean_ledger(self):
        ctx = self.ctx()
        step_health.run(ctx, out=self.lines.append)
        self.assertEqual(self.lines, ['health: clean', 'stall: none'])
        self.assertEqual(ctx.counts['stalls'], 0)


# ---- wave -------------------------------------------------------------------------

def _brief(kind, item_id):
    return types.SimpleNamespace(kind=kind, item_id=item_id, text=f'brief for {item_id}\n',
                                 model='Opus', add_dirs=[], id_ranges_needed=[])


class WaveStepTests(StepsTestCase):
    def setUp(self):
        super().setUp()
        self.push_branch('fix/B-0001')
        self.rows = [
            feeder_rows.Row(0, 'BUG → FIX', 'B-0001', '', 'would launch fix-b-0001 (Opus)',
                            'fix-bug', 'fix/B-0001', 'S1 open'),
            feeder_rows.Row(2, 'PLAN → CODE', 'T-0002', 'F-0001', 'WAITS ON T-0001', 'task',
                            'task/T-0002', 'footprint'),
        ]
        self.built, self.waved = [], []

        def build(product, row, index, inflight, repo_facts=None):
            self.built.append((row.item_id, sorted(index), inflight, repo_facts))
            return _brief('fix-bug', row.item_id)

        def wave(product, rows, n, brief_fn=None, out=print):
            self.waved.append(([(r.job, r.item, r.state, r.action, r.severity, r.model) for r in rows],
                               n, [brief_fn(r) for r in rows]))
            out(f'launched {rows[0].job:<24} {rows[0].item:<10} → acct-a (opus) pid 1')
            return [(rows[0], {'account': 'acct-a', 'model': 'opus', 'pid': 1})], []
        for name, fn in (('_build', build), ('_wave', wave)):
            p = mock.patch.object(step_wave, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_rows_briefed_launched_and_logged(self):
        self.session(job='task-t-0001', item='T-0001', kind='task', account='acct-b')
        self.session(job='old', item='T-0009', kind='task', ended='2026-01-01T00:00:00Z')
        self.write_config('feeder:\n  capacity: 3\n')
        seen = {}

        def plan(index, product, inflight, capacity):
            seen.update(capacity=capacity, inflight=[s['item'] for s in inflight], ids=sorted(index))
            return self.rows
        ctx = self.ctx()
        with mock.patch.object(feeder_rows, 'plan_rows', plan):
            step_wave.run(ctx, out=self.lines.append)
        self.assertEqual(seen, {'capacity': 3, 'inflight': ['T-0001'], 'ids': ['B-0001', 'F-0001']})
        self.assertEqual(len(self.built), 1)
        facts = self.built[0][3]
        self.assertTrue(facts['pushed'])
        self.assertTrue(facts['last_commit'].endswith('work on fix/B-0001'))
        self.assertEqual(self.waved, [([('fix-bug-b-0001', 'B-0001', 'BUG', 'FIX', 'S1', 'Opus')], 1,
                                       ['brief for B-0001\n'])])
        self.assertEqual(self.lines, ['waits    -                        T-0002     — WAITS ON T-0001',
                                      'launched fix-bug-b-0001           B-0001     → acct-a (opus) pid 1'])
        evs = self.events(ctx)
        self.assertEqual([(e['kind'], e['item'], e['account'], e['model'], e['brief_kind']) for e in evs],
                         [('launch', 'B-0001', 'acct-a', 'opus', 'fix-bug')])
        self.assertEqual(ctx.counts['launches'], 1)

    def test_unpushed_branch_facts(self):
        facts = step_wave.repo_facts(self.product, 'fix/B-9999')
        self.assertEqual(facts, {'branch': 'fix/B-9999', 'pushed': False, 'remote_sha': '',
                                 'last_commit': ''})

    def test_nothing_to_launch(self):
        with mock.patch.object(feeder_rows, 'plan_rows', lambda *a: []):
            step_wave.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.lines, ['wave: nothing to launch'])
        self.assertEqual(self.waved, [])


# ---- prs --------------------------------------------------------------------------

class PrsStepTests(StepsTestCase):
    product_extra = 'steps:\n  batch: off\nconventions:\n  branch_prefixes:\n    fix-bug: fix/\n'

    def setUp(self):
        super().setUp()
        self.gh_calls, self.open_prs, self.hygiene = [], set(), []

        def gh(args):
            self.gh_calls.append(args)
            if args[:2] == ['pr', 'list']:
                head = args[args.index('--head') + 1]
                return 0, json.dumps([{'number': 7}] if head in self.open_prs else []), ''
            return 0, f'https://example.invalid/pr/{len(self.gh_calls)}\n', ''
        for name, fn in (('_gh', gh), ('_hygiene', lambda product: self.hygiene.append(product.name))):
            p = mock.patch.object(step_prs, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def finished(self, job, branch, item='B-0001', started='2026-09-21T10:00:00Z', **kw):
        self.session(job=job, item=item, branch=branch, started=started)
        self.session(job=job, ended='2026-09-21T11:00:00Z', end_reason=kw.get('reason', 'finished'))

    def creates(self):
        return [c for c in self.gh_calls if c[:2] == ['pr', 'create']]

    def test_opens_a_pr_with_item_title_and_acceptance(self):
        self.push_branch('fix/B-0001')
        self.finished('fix-bug-b-0001', 'fix/B-0001')
        step_prs.run(self.ctx(), out=self.lines.append)
        (argv,) = self.creates()
        self.assertEqual(argv[:8], ['pr', 'create', '-R', 'x/y', '--base', 'main', '--head', 'fix/B-0001'])
        self.assertEqual(argv[argv.index('--title') + 1], 'B-0001 — the first bug')
        body = argv[argv.index('--body') + 1]
        self.assertIn('Card: [B-0001](', body)
        self.assertIn('bugs/B-0001.md', body)
        self.assertIn('- [ ] the named test passes\n- [ ] no regression\n', body)
        self.assertEqual(self.hygiene, ['sample'])
        self.assertEqual(self.lines[-1], 'prs: opened https://example.invalid/pr/2 — B-0001 — the first bug')

    def test_skips_unpushed_failed_open_and_foreign_branches(self):
        self.push_branch('worker/has-pr')
        self.push_branch('other/x')
        self.push_branch('worker/failed')
        self.open_prs.add('worker/has-pr')
        self.finished('a', 'worker/has-pr')
        self.finished('b', 'other/x')
        self.finished('c', 'worker/never-pushed')
        self.finished('d', 'worker/failed', reason='failed')
        self.session(job='e', item='B-0001', branch='fix/B-0001')  # still running
        step_prs.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.lines, ['prs: none to open'])
        self.assertEqual(self.hygiene, ['sample'])

    def test_cap_per_tick(self):
        self.write_product(f'repo_dir: {self.repo}\n{self.product_extra}  prs_per_tick: 1\n')
        self.product = env.load_product('sample')
        self.push_branch('worker/one')
        self.push_branch('worker/two')
        self.finished('one', 'worker/one', started='2026-09-21T09:00:00Z')
        self.finished('two', 'worker/two', started='2026-09-21T10:00:00Z')
        step_prs.run(self.ctx(), out=self.lines.append)
        self.assertEqual([c[c.index('--head') + 1] for c in self.creates()], ['worker/one'])
        self.assertIn('prs: cap 1 reached — the rest next tick', self.lines)

    def test_gh_refusal_fails_the_step_after_hygiene(self):
        self.push_branch('worker/one')
        self.finished('one', 'worker/one')
        with mock.patch.object(step_prs, '_gh', lambda args: (0, '[]', '') if args[1] == 'list'
                               else (1, '', 'no permission\nmore')):
            with self.assertRaises(RuntimeError):
                step_prs.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.lines, ['prs: worker/one not opened — no permission'])
        self.assertEqual(self.hygiene, ['sample'])


# ---- daily ------------------------------------------------------------------------

class DailyStepTests(StepsTestCase):
    def fake_parts(self, fail=()):
        def parts(product, root):
            def make(name):
                def thunk():
                    if name in fail:
                        raise ValueError(f'{name} broke')
                    with open(os.path.join(root, f'{name}.out'), 'w') as f:
                        f.write(name)
                    print(f'{name} first line')
                    print(f'{name} summary')
                    return 0
                return thunk
            return [(n, make(n)) for n in ('groom', 'stale', 'file-bugs', 'rollup')]
        return mock.patch.object(step_daily, 'parts', parts)

    def test_one_line_per_part_then_commit_and_push(self):
        with self.fake_parts():
            step_daily.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.lines, ['daily: groom ok — groom summary', 'daily: stale ok — stale summary',
                                      'daily: file-bugs ok — file-bugs summary',
                                      'daily: rollup ok — rollup summary',
                                      'daily: record committed and pushed'])
        self.assertEqual(_git(['show', 'main:rollup.out'], self.origin), 'rollup')
        self.assertTrue(_git(['log', '-1', '--format=%s', 'main'], self.origin).startswith('tick: daily '))

    def test_a_failing_part_does_not_stop_the_others(self):
        with self.fake_parts(fail=('stale',)):
            with self.assertRaises(RuntimeError) as cm:
                step_daily.run(self.ctx(), out=self.lines.append)
        self.assertIn('stale', str(cm.exception))
        self.assertIn('daily: stale FAILED — stale broke', self.lines)
        self.assertIn('daily: rollup ok — rollup summary', self.lines)

    def test_real_parts_are_wired(self):
        names = [n for n, _ in step_daily.parts(self.product, self.tmp)]
        self.assertEqual(names, ['groom', 'stale', 'file-bugs', 'rollup'])
        self.assertEqual(step_daily.yesterday(__import__('datetime').date(2026, 3, 1)), '2026-02-28')


if __name__ == '__main__':
    unittest.main()
