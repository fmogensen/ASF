"""The tick's asf steps — health, wave, prs, daily — and the ordered tick around them.

Workers, feeder, briefs and ``gh`` are stubbed (module attributes); git is real: a bare origin
for the record (``TickTestCase``) and a bare origin + clone for the product repo.
"""
import contextlib
import io
import json
import os
import subprocess
import time
import types
import unittest
from unittest import mock

from asf import capacity, env
from asf.feeder import rows as feeder_rows
from asf.harvest import harvest as harvest_mod
from asf.metrics import metrics
from asf.metrics import metrics as metrics_mod
from asf.groom import answers
from asf.tick import shadow, step_daily, step_groom, step_harvest, step_health, step_prs, step_wave, steps, tick
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from tests.test_tick import TickTestCase, _git, steps_only

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

    @classmethod
    def build_repos(cls, tmp):
        super().build_repos(tmp)
        seed = os.path.join(tmp, 'seed')
        os.makedirs(os.path.join(seed, 'bugs'))
        with open(os.path.join(seed, 'bugs', 'B-0001.md'), 'w') as f:
            f.write(CARD)
        with open(os.path.join(seed, 'index.json'), 'w') as f:
            json.dump(INDEX, f)
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'index'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)

        repo_origin = os.path.join(tmp, 'repo.git')
        repo = os.path.join(tmp, 'repo')
        _git(['init', '-q', '--bare', '-b', 'main', repo_origin])
        _git(['clone', '-q', repo_origin, repo])
        _git(['config', 'user.email', 'r@example.com'], repo)
        _git(['config', 'user.name', 'r'], repo)
        with open(os.path.join(repo, 'README'), 'w') as f:
            f.write('r\n')
        _git(['add', '-A'], repo)
        _git(['commit', '-q', '-m', 'init'], repo)
        _git(['push', '-q', 'origin', 'HEAD:main'], repo)

    def setUp(self):
        super().setUp()
        self.repo_origin = os.path.join(self.tmp, 'repo.git')
        self.repo = os.path.join(self.tmp, 'repo')
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
                mock.patch.object(step_groom, 'run', ok('groom')), \
                mock.patch.object(step_wave, 'run', ok('wave')), \
                mock.patch.object(step_prs, 'run', ok('prs')), \
                mock.patch.object(step_harvest, 'run', ok('harvest')), \
                mock.patch.object(step_daily, 'run', ok('daily')), \
                mock.patch.object(shadow, 'ensure_clone', wraps=shadow.ensure_clone) as clone:
            rc, out = self.run_tick()
        self.assertEqual(rc, 1)
        self.assertEqual(ran, ['health', 'groom', 'wave', 'prs', 'harvest', 'daily'])
        lines = steps_only(out).splitlines()
        self.assertEqual(lines[0], '[step:health] FAILED health blew up')
        self.assertEqual(lines[1], '[command:batch] batch ran')
        self.assertEqual(lines[-1], f'tick: state committed and pushed ({self.record_path()})')
        self.assertEqual(clone.call_count, 1)  # one record clone per tick, however many steps

        # one commit per tick: the derived state and the step log land together
        self.assertEqual(self.origin_commits(), 3)  # seed, index, this tick
        self.assertTrue(_git(['log', '-1', '--format=%s', 'main'], self.origin).startswith('tick: state '))
        files = _git(['show', '--name-only', '--format=', 'main'], self.origin).splitlines()
        self.assertIn('state/rollup.md', files)
        # the stream is dated in UTC (metrics.today()); %cs is the committer's local date and
        # differs from it for two hours a day in a CET zone
        day = metrics_mod.today()
        self.assertIn(f'metrics/ticks/{day}.jsonl', files)
        tick_log = _git(['show', f'main:metrics/ticks/{day}.jsonl'], self.origin)
        line = json.loads(tick_log.splitlines()[-1])
        self.assertEqual([(s['step'], s['ok']) for s in line['steps']],
                         [('record', True), ('health', False), ('groom', True), ('wave', True), ('prs', True),
                          ('harvest', True), ('batch', True), ('daily', True)])
        self.assertEqual(line['product'], 'sample')
        # the line keeps the ticks stream's schema: the scorecard reads it without a KeyError
        metrics.scorecard_rows([], [], [line])

    def test_b0083_a_failed_record_step_stops_the_tick_and_launches_nothing(self):
        boom = subprocess.CalledProcessError(
            1, ['asf', 'check'], stderr='tasks/T-0042.md:4: continuation line in frontmatter (rule: typed-field)\n')
        later = [mock.patch.object(m, 'run', return_value=0)
                 for m in (step_health, step_groom, step_wave, step_prs, step_harvest, step_daily)]
        mocks = [p.start() for p in later]
        for p in later:
            self.addCleanup(p.stop)
        with mock.patch.object(tick, 'run_step0', side_effect=boom):
            rc, out = self.run_tick()
        self.assertNotEqual(rc, 0)
        for m in mocks:
            m.assert_not_called()
        self.assertNotIn('[command:batch]', out)
        self.assertIn('RECORD STALE — tasks/T-0042.md:4: continuation line in frontmatter (rule: typed-field)', out)
        self.assertLess(out.index('RECORD STALE'), out.index('IN FLIGHT'))

    def test_daily_stamped_only_when_it_passed(self):
        with mock.patch.object(step_daily, 'run', side_effect=RuntimeError('daily parts failed: rollup')):
            rc, out = self.run_tick(steps='daily')
        self.assertEqual(rc, 1)
        self.assertIn('[step:daily] FAILED daily parts failed: rollup', out)
        self.assertFalse(os.path.exists(steps.stamp_path(self.product)))

    def test_command_steps_alone_do_not_clone_the_record_to_log(self):
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertEqual(steps_only(out), '[command:batch] batch ran\n')
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
        self.assertIn('DEAD  fix-b-0001               correction launched as '
                      'fix-b-0001-correction', self.lines)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn('CORRECTION: the step failed with:', fake.calls[0][1])
        self.assertEqual(self.events(ctx), [])

    def test_b0039_corrected_session_is_recorded_finished_as_its_own_job(self):
        self.dead_session()
        fake = runtime_mod.FakeRuntime([{'ok': True}])
        # tick one launches the correction, tick two judges it (B-0085): nothing waits inside a step
        step_health.run(self.ctx(), out=self.lines.append, runtime_fn=lambda: fake)
        step_health.run(self.ctx(), out=self.lines.append, runtime_fn=lambda: fake)
        sessions = pool_mod.load_sessions(self.product)
        # the dead run's own record stands — it is not rewritten as the one that passed
        self.assertEqual(sessions['fix-b-0001']['end_reason'], 'dead pid')
        s = sessions['fix-b-0001-correction']
        self.assertEqual(s['end_reason'], 'finished')
        self.assertTrue(s.get('ended'))

    def test_b0039_failed_correction_is_recorded_failed_as_its_own_job(self):
        self.dead_session()
        fake = runtime_mod.FakeRuntime([{'ok': False, 'result': 'still broken'}])
        step_health.run(self.ctx(), out=self.lines.append, runtime_fn=lambda: fake)
        step_health.run(self.ctx(), out=self.lines.append, runtime_fn=lambda: fake)
        sessions = pool_mod.load_sessions(self.product)
        self.assertEqual(sessions['fix-b-0001']['end_reason'], 'dead pid')
        s = sessions['fix-b-0001-correction']
        self.assertEqual(s['end_reason'], 'failed')

    def test_b0062_already_corrected_is_held_not_a_question(self):
        # "a manual question for me is a bug": a twice-dead session is a correction on the run
        # and a round on the item, so the lane's own loop carries it to the cap
        self.dead_session(corrected=1)
        ctx = self.ctx()
        step_health.run(ctx, out=self.lines.append, runtime_fn=lambda: self.fail('no rerun'))
        evs = self.events(ctx)
        self.assertEqual([e['kind'] for e in evs], ['held'])
        self.assertFalse(any(ln.startswith('NEEDS OPERATOR:') for ln in self.lines), self.lines)
        held = [ln for ln in self.lines if ln.startswith('held ')]
        self.assertEqual(len(held), 1, self.lines)
        self.assertIn('died twice: the session and its cold retry both ended without a result', held[0])
        self.assertTrue(held[0].endswith('— back to its session (round 1)'), held[0])
        s = pool_mod.load_sessions(self.product)['fix-b-0001']
        self.assertEqual((s['correction']['kind'], s['rounds']), ('died', 1))
        self.assertFalse(s.get('operator_flagged'))
        # the next tick does not hold it again while the correction is pending
        self.assertEqual(step_health.handle_dead(ctx, dict(s, job='fix-b-0001'), out=self.lines.append), 'held')
        self.assertEqual(pool_mod.load_sessions(self.product)['fix-b-0001']['rounds'], 1)

    def test_b0064_an_adjudicate_runs_ruling_is_filed_on_the_card_by_the_factory(self):
        ctx = self.ctx()
        root = ctx.record_root()
        os.makedirs(os.path.join(root, 'bugs'), exist_ok=True)
        card = os.path.join(root, 'bugs', 'B-0001.md')
        with open(card, 'w') as f:
            f.write('---\nid: B-0001\ntype: bug\ntitle: x\nseverity: S1\n# ---- machine ----\nstate: Active\n---\n'
                    '## Symptom\ns\n\n## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n')
        log = os.path.join(self.tmp, 'adj.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': 'REPORT\nitem: B-0001\nkind: adjudicate\nstatus: done\n'
                                          'branch: fix/B-0001\npushed: yes abc\ncommits: none\ntests: none\n'
                                          'left out: none\nruling: no open finding; the fix stands\n```\n'}) + '\n')
        self.session(job='adjudicate-b-0001', item='B-0001', kind='adjudicate', pid=DEAD_PID, log=log,
                     started='2026-09-22T10:00:00Z', ended='2026-09-22T10:30:00Z', end_reason='finished')
        filed = step_health.file_rulings(ctx, out=self.lines.append)
        self.assertEqual(filed, ['adjudicate-b-0001'])
        with open(card) as f:
            body = f.read()
        self.assertIn('adjudicate (adjudicate-b-0001): no open finding; the fix stands', body)
        self.assertTrue(pool_mod.load_sessions(self.product)['adjudicate-b-0001'].get('adjudicated'))
        self.assertEqual([e['kind'] for e in self.events(ctx)], ['ruling'])
        self.assertTrue(self.lines[0].startswith('ruling adjudicate-b-0001 filed on B-0001:'), self.lines)
        # filed once: a second pass finds nothing to do, and the card gains no second line
        self.assertEqual(step_health.file_rulings(ctx, out=self.lines.append), [])
        with open(card) as f:
            self.assertEqual(f.read().count('adjudicate (adjudicate-b-0001)'), 1)

    def test_b0062_failed_correction_holds_the_run(self):
        self.dead_session()
        fake = runtime_mod.FakeRuntime([{'ok': False, 'result': 'still broken'}])
        ctx = self.ctx()
        step_health.run(ctx, out=self.lines.append, runtime_fn=lambda: fake)
        self.assertEqual([e['kind'] for e in self.events(ctx)], [],
                         'the correction is running; a hold would pre-judge it (B-0085)')
        step_health.run(ctx, out=self.lines.append, runtime_fn=lambda: fake)
        self.assertEqual([e['kind'] for e in self.events(ctx)], ['held'])

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
        self.session(job='task-t-0001', item='T-0001', kind='task', account='acct-b',
                     pid=os.getpid())
        self.session(job='old', item='T-0009', kind='task', ended='2026-01-01T00:00:00Z')
        self.write_config('feeder:\n  capacity: 3\n')
        seen = {}

        def plan(index, product, inflight, capacity, attempts=None, corrections=None, busy=None,
                groom_state=None, unlanded=None, open_branches=None):
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
        # T-0005: the wave step now also writes one `capacity` event every tick (§4.3)
        self.assertEqual([e['kind'] for e in evs], ['capacity', 'launch'])
        launch_ev = evs[1]
        self.assertEqual((launch_ev['kind'], launch_ev['item'], launch_ev['model'], launch_ev['brief_kind']),
                         ('launch', 'B-0001', 'opus', 'fix-bug'))
        # the record is public: an event never carries an account name (B-0023)
        self.assertNotIn('account', launch_ev)
        self.assertEqual(ctx.counts['launches'], 1)

    def test_the_wave_plans_over_the_plans_order(self):
        # defence in depth: the wave overlays the plan's order before the feeder sees the index
        seen = {}

        def overlay(items, read_plan):
            seen['read'] = read_plan
            return dict(items, overlaid={'id': 'overlaid', 'type': 'task'})

        def plan(index, *a, **kw):
            seen['ids'] = sorted(index)
            return []
        with mock.patch.object(step_wave.plan_order, 'overlay', overlay), \
                mock.patch.object(feeder_rows, 'plan_rows', plan):
            step_wave.run(self.ctx(), out=self.lines.append)
        self.assertIn('overlaid', seen['ids'])
        self.assertIsNone(seen['read']('docs/plans/no-such-plan.md'))

    def test_unpushed_branch_facts(self):
        facts = step_wave.repo_facts(self.product, 'fix/B-9999')
        self.assertEqual(facts, {'branch': 'fix/B-9999', 'pushed': False, 'remote_sha': '',
                                 'last_commit': ''})

    def test_job_name_takes_a_key_over_the_item_id(self):
        # PD9/D7: the groom brief's job is `groom-<date>`, stable while the row's item_id (the
        # oldest open question) drifts as questions get answered.
        self.assertEqual(step_wave.job_name('groom', 'F-0001', key='2026-09-22'),
                         'groom-2026-09-22')
        self.assertEqual(step_wave.job_name('fix-bug', 'B-0001'), 'fix-bug-b-0001')

    def test_groom_state_is_none_with_no_groom_file(self):
        self.assertIsNone(step_wave.groom_state(self.product, self.ctx().record_root()))

    def test_groom_state_reads_the_newest_groom_file_and_the_ledger_attempts(self):
        root = self.ctx().record_root()
        groom_dir = os.path.join(root, 'groom')
        os.makedirs(groom_dir, exist_ok=True)
        with open(os.path.join(groom_dir, '2026-09-20.md'), 'w') as f:
            f.write('- [ ] F-0001 old day — undecided 3d → answer: ____\n')
        with open(os.path.join(groom_dir, '2026-09-22.md'), 'w') as f:
            f.write('- [ ] F-0001 x — undecided 3d → answer: ____\n'
                    '- [ ] F-0002 y — undecided 4d → answer: ____\n')
        self.session(job='groom-2026-09-22', item='F-0001', kind='groom', account='acct-a',
                     pid=DEAD_PID, started='t1')
        self.session(job='groom-2026-09-22', ended='t2', end_reason='finished')  # not a 2nd attempt
        state = step_wave.groom_state(self.product, root)
        self.assertEqual(state['date'], '2026-09-22')
        self.assertEqual(state['open'], ['F-0001', 'F-0002'])
        self.assertEqual(state['oldest'], 'F-0001')
        self.assertEqual(state['attempts'], 1)
        self.assertTrue(state['file'].endswith('2026-09-22.md'))
        self.assertTrue(state['answers'].endswith(os.path.join('groom', '2026-09-22.answers')))

    def test_groom_state_leaves_out_what_the_factory_already_acts_on(self):
        # a groom file written before its answers reached the index kept asking about work the
        # feeder already has a row for; the adjudicator must not be launched to answer it again
        root = self.ctx().record_root()
        os.makedirs(os.path.join(root, 'groom'), exist_ok=True)
        with open(os.path.join(root, 'groom', '2026-09-22.md'), 'w') as f:
            f.write('- [ ] B-0001 the first bug — auto-filed, count 2 → answer: ____\n'
                    '- [ ] F-0001 x — undecided 3d → answer: ____\n')
        state = step_wave.groom_state(self.product, root)
        self.assertEqual(state['open'], ['F-0001'])

    def test_nothing_to_launch(self):
        with mock.patch.object(feeder_rows, 'plan_rows', lambda *a, **kw: []):
            step_wave.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.lines, ['wave: nothing to launch'])
        self.assertEqual(self.waved, [])


class WaveStep(StepsTestCase):
    """T-0005: the wave step's cut comes from ``asf.capacity.resolve``, and one ``capacity``
    event is written every tick, bound or not."""

    def other_product_inflight(self, n=1):
        """A second product, ``other``, with ``n`` live sessions — the ledger
        ``inflight_sessions_elsewhere`` reads."""
        with open(os.path.join(env.ASF_HOME, 'products', 'other.yaml'), 'w') as f:
            f.write('repo_slug: x/y\n')
        for i in range(n):
            pool_mod.append_session('other', {'job': f'w{i}', 'item': 'X', 'account': 'acct-b',
                                              'started': pool_mod.now_iso(), 'pid': 1})

    def test_capacity_comes_from_the_resolver(self):
        seen = {}

        def plan(index, product, inflight, capacity, attempts=None, corrections=None, busy=None,
                groom_state=None, unlanded=None, open_branches=None):
            seen['capacity'] = capacity
            return []

        resolved = capacity.Resolved(sessions=7, sessions_bound='product', ci=None,
                                     ci_bound=None, ci_inflight=None, batch={}, reserve={})
        with mock.patch.object(feeder_rows, 'plan_rows', plan), \
             mock.patch.object(capacity, 'resolve', lambda *a, **kw: resolved):
            step_wave.run(self.ctx(), out=self.lines.append)
        self.assertEqual(seen['capacity'], 7)

    def test_the_operator_total_cuts_the_wave(self):
        self.other_product_inflight(1)
        self.write_config('capacity:\n  total:\n    sessions: 1\n')
        seen = {}

        def plan(index, product, inflight, capacity, attempts=None, corrections=None, busy=None,
                groom_state=None, unlanded=None, open_branches=None):
            seen['capacity'] = capacity
            return []

        with mock.patch.object(feeder_rows, 'plan_rows', plan):
            step_wave.run(self.ctx(), out=self.lines.append)
        self.assertEqual(seen['capacity'], 0)

    def test_a_capacity_event_names_what_bound_the_cut(self):
        self.other_product_inflight(1)
        self.write_config('capacity:\n  total:\n    sessions: 1\n')
        ctx = self.ctx()
        with mock.patch.object(feeder_rows, 'plan_rows', lambda *a, **kw: []):
            step_wave.run(ctx, out=self.lines.append)
        evs = [e for e in self.events(ctx) if e['kind'] == 'capacity']
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]['sessions'], 0)
        self.assertEqual(evs[0]['sessions_inflight'], 0)
        self.assertEqual(evs[0]['sessions_bound_by'], 'operator total')
        self.assertIsNone(evs[0]['ci'])
        self.assertIsNone(evs[0]['ci_inflight'])
        self.assertIsNone(evs[0]['ci_bound_by'])


# ---- prs --------------------------------------------------------------------------

class PrsStepTests(StepsTestCase):
    """A ``pull-request`` landing: the PR is the mechanism, so the step opens one per finished
    branch. (``batch: off`` alone would land by fast-forward — see the last two tests.)"""
    product_extra = ('steps:\n  batch: off\nconventions:\n  landing: pull-request\n'
                     '  branch_prefixes:\n    code: worker/\n    fix-bug: fix/\n')

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

    def test_no_pr_host_is_one_line_no_gh_no_hygiene(self):
        with open(env.product_path('sample'), 'w') as f:  # no repo_slug; origin is a local path
            f.write(f'backlog_dir: {self.operator}\nrepo_dir: {self.repo}\n{self.product_extra}')
        self.product = env.load_product('sample')
        self.push_branch('worker/one')
        self.finished('one', 'worker/one')
        self.assertEqual(step_prs.run(self.ctx(), out=self.lines.append), 0)
        self.assertEqual(self.lines, ['prs: no PR host (no repo_slug, origin is not a hosted repo) — '
                                      '1 finished branch(es) left as they are'])
        self.assertEqual((self.gh_calls, self.hygiene), ([], []))

    def test_slug_comes_from_a_hosted_origin_when_the_yaml_has_none(self):
        _git(['remote', 'set-url', 'origin', 'git@example.com:owner/name.git'], self.repo)
        self.assertEqual(step_prs.repo_slug(env.Product('p', {'repo_dir': self.repo})), 'owner/name')

    def test_prs_step_prefixes_come_from_the_product(self):
        product = env.Product('p', {'conventions': {
            'branch_prefixes': {'code': 'worker/', 'fix': 'fix/', 'spec': 'spec/', 'plan': 'plan/',
                                 'fix-bug': 'feature/'}}})
        self.assertEqual(sorted(step_prs.branch_prefixes(product)),
                          sorted(product.conventions.all_prefixes()))
        self.assertIn('feature/', step_prs.branch_prefixes(product))

    def test_fast_forward_landing_opens_nothing(self):
        """B-0029: ``asf`` lands by fast-forward (``batch: off``, no ``landing``) — a PR is never
        the mechanism there, so the step opens none, says so once, runs hygiene and exits 0."""
        self.write_product(f'repo_dir: {self.repo}\nsteps:\n  batch: off\n'
                           'conventions:\n  branch_prefixes:\n    fix-bug: fix/\n')
        self.product = env.load_product('sample')
        self.push_branch('fix/B-0001')
        self.finished('fix-bug-b-0001', 'fix/B-0001')
        self.assertEqual(step_prs.run(self.ctx(), out=self.lines.append), 0)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.lines, ['prs: landing is fast-forward — harvest lands the branches'])
        self.assertEqual(self.hygiene, ['sample'])

    def test_harvested_and_at_trunk_branches_are_not_candidates(self):
        """B-0029: a session already ``harvested`` has landed; a pushed branch with no commits
        past the trunk has nothing to open. Neither reaches ``gh``, and the step does not fail."""
        self.push_branch('fix/B-0001')
        self.finished('fix-bug-b-0001', 'fix/B-0001')
        self.session(job='fix-bug-b-0001', harvested='abc123')
        _git(['push', '-q', 'origin', 'main:refs/heads/fix/B-0002'], self.repo)  # at the trunk head
        self.finished('fix-bug-b-0002', 'fix/B-0002', item='B-0002')
        self.assertEqual(step_prs.run(self.ctx(), out=self.lines.append), 0)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.lines, ['prs: fix/B-0002 at trunk — nothing to open', 'prs: none to open'])
        self.assertEqual(self.hygiene, ['sample'])


# ---- harvest ----------------------------------------------------------------------

class HarvestStepTests(StepsTestCase):
    """``batch: off`` → the lane lands by fast-forward, after ``prs`` — in a harvest of its own
    that the tick starts and never waits on."""

    def setUp(self):
        super().setUp()
        self.spawned = []

    def inline(self, sink=None):
        """A spawn that runs the background harvest to its end, here, before returning."""
        def spawn(product, items_file):
            self.spawned.append(items_file)
            step_harvest.background(product, items_file, out=(sink or (lambda line: None)))
            return 4242
        return spawn

    def origin_main(self):
        return _git(['rev-parse', 'main'], self.repo_origin)

    def finished_branch(self):
        self.push_branch('fix/B-0001')  # its commit is `work on fix/B-0001`: names the item
        _git(['branch', '-D', 'fix/B-0001'], self.repo)  # remote-only, as a session leaves it
        self.session(job='fix-bug-b-0001', item='B-0001', branch='fix/B-0001', pid=DEAD_PID,
                     started='2026-09-21T00:00:00Z')
        self.session(job='fix-bug-b-0001', ended='2026-09-21T00:05:00Z', end_reason='finished',
                     rc=0)

    def test_finished_fix_branch_lands_through_the_tick(self):
        self.finished_branch()
        with mock.patch.object(step_harvest, 'spawn_background', self.inline(print)):
            rc, out = self.run_tick(steps='harvest')
        self.assertEqual(rc, 0)
        sha = self.origin_main()
        self.assertIn(f'landed fix/B-0001 → {sha}', out)
        self.assertIn('harvest: started in the background (pid 4242)', out)
        self.assertEqual(_git(['log', '-1', '--format=%s', 'main'], self.repo_origin),
                         'work on fix/B-0001')
        self.assertEqual(_git(['branch', '--list', 'fix/B-0001'], self.repo_origin), '')
        self.assertEqual(pool_mod.load_sessions(self.product)['fix-bug-b-0001']['harvested'], sha)

    def test_a_running_gate_is_neither_waited_on_nor_doubled(self):
        self.finished_branch()
        lock = harvest_mod.try_lock(env.state_dir(self.product))  # a gate mid-run elsewhere
        self.addCleanup(lock.close)
        step_harvest.write_status(self.product, {'pid': 777, 'started': '2026-09-23T22:31:00Z'})
        before = self.origin_main()
        spawn = mock.Mock(side_effect=AssertionError('a second gate'))
        with mock.patch.object(harvest_mod, 'run_product_harvest',
                               side_effect=AssertionError('gated in the tick')):
            step_harvest.run(self.ctx(), out=self.lines.append, spawn=spawn)
            # the background run a second spawn would start refuses too
            step_harvest.background(self.product, out=self.lines.append)
        spawn.assert_not_called()
        self.assertEqual(self.lines, [
            'harvest: gate running (pid 777, since 2026-09-23T22:31:00Z) — lands when it '
            'finishes, this tick does not wait',
            'harvest: another harvest of sample holds the lock — skipped'])
        self.assertEqual(self.origin_main(), before)

    def test_the_tick_after_a_green_gate_reports_the_landing_once(self):
        self.finished_branch()
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=self.inline())
        sha = self.origin_main()
        self.assertEqual(_git(['log', '-1', '--format=%s', 'main'], self.repo_origin),
                         'work on fix/B-0001')
        self.lines.clear()
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=mock.Mock(return_value=4243))
        self.assertTrue(self.lines[0].startswith('harvest: last run '), self.lines)
        self.assertIn(f'landed fix/B-0001 → {sha}', self.lines)
        self.lines.clear()
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=mock.Mock(return_value=4244))
        self.assertNotIn(f'landed fix/B-0001 → {sha}', self.lines)  # once

    def test_the_tick_after_a_red_gate_reports_the_hold_and_nothing_lands(self):
        self.write_product(f'repo_dir: {self.repo}\n{self.product_extra}'
                           'conventions:\n  test_command: "python3 -c \'raise SystemExit(\\"FAILED red\\")\'"\n')
        self.product = env.load_product('sample')
        self.finished_branch()
        before = self.origin_main()
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=self.inline())
        self.assertEqual(self.origin_main(), before)
        self.lines.clear()
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=mock.Mock(return_value=1))
        self.assertTrue(any(l.startswith('held fix/B-0001') for l in self.lines), self.lines)
        self.assertNotIn('fix-bug-b-0001', {j for j, r in pool_mod.load_sessions(self.product).items()
                                            if r.get('harvested')})

    def test_the_step_hands_the_record_index_to_the_background_run(self):
        ctx = self.ctx()
        ctx.record_root()
        step_harvest.run(ctx, out=self.lines.append, spawn=self.inline())
        with open(self.spawned[0], encoding='utf-8') as f:
            self.assertIn('B-0001', json.load(f))

    def test_a_real_background_harvest_lands_and_the_tick_does_not_wait(self):
        self.finished_branch()
        t0 = time.monotonic()
        step_harvest.run(self.ctx(), out=self.lines.append)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(self.lines[-1].startswith('harvest: started in the background (pid '),
                        self.lines)
        deadline = time.monotonic() + 90
        while not step_harvest.read_status(self.product).get('finished'):
            self.assertLess(time.monotonic(), deadline, 'the background harvest never finished')
            time.sleep(0.2)
        self.assertEqual(_git(['log', '-1', '--format=%s', 'main'], self.repo_origin),
                         'work on fix/B-0001')

    def test_nothing_finished_is_one_line(self):
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=self.inline())
        step_harvest.run(self.ctx(), out=self.lines.append, spawn=mock.Mock(return_value=1))
        self.assertIn('harvest: none to land', self.lines)

    def test_no_repo_dir_is_one_line(self):
        ctx = tick.Context(env.Product('p', {}))
        step_harvest.run(ctx, out=self.lines.append)
        self.assertEqual(self.lines, ['harvest: no repo_dir — nothing to harvest'])

    def test_a_hand_run_harvest_waits_for_no_one_and_gates_nothing_while_one_runs(self):
        lock = harvest_mod.try_lock(env.state_dir(self.product))
        self.addCleanup(lock.close)
        out = io.StringIO()
        with mock.patch.object(harvest_mod, 'run_product_harvest',
                               side_effect=AssertionError('gated')), \
                contextlib.redirect_stdout(out):
            self.assertEqual(harvest_mod.main(['--product', 'sample']), 0)
        self.assertEqual(out.getvalue(), 'harvest: another harvest of sample is running — skipped\n')

    def test_shadow_never_runs_harvest(self):
        with mock.patch.object(step_harvest, 'run', side_effect=AssertionError('ran')), \
                mock.patch.object(tick, 'run_step0', lambda root, product, fresh=False: None), \
                mock.patch.object(tick, 'render_tables', return_value={}):
            rc, out = self.run_tick(shadow=True)
        self.assertEqual(rc, 0)
        self.assertIn('tick --shadow:', out)

    def test_harvest_runs_after_prs_before_batch(self):
        self.assertEqual(steps.STEPS[steps.STEPS.index('prs') + 1], 'harvest')
        self.assertEqual(steps.STEPS[steps.STEPS.index('harvest') + 1], 'batch')
        self.assertIn(('harvest', 'asf', None), steps.resolve(self.product))


# ---- batch (the CI hold) and the capacity overlay ----------------------------------

class BatchStep(StepsTestCase):
    """``batch`` is a command step, held at the CI ceiling before it runs."""

    product_extra = 'steps:\n  batch: python3 -c \'print("batch ran")\'\n'

    def resolved(self, ci, ci_inflight):
        return capacity.Resolved(sessions=4, sessions_bound='default', ci=ci, ci_bound='product',
                                  ci_inflight=ci_inflight, batch={}, reserve={})

    def test_at_the_ci_ceiling_the_command_is_skipped_with_a_waits_line(self):
        with mock.patch.object(capacity, 'resolve', return_value=self.resolved(2, 3)):
            rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertIn('waits    batch — at ci capacity (3/2)', out)
        self.assertNotIn('[command:batch]', out)

    def test_under_the_ceiling_the_command_runs(self):
        with mock.patch.object(capacity, 'resolve', return_value=self.resolved(2, 1)):
            rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertIn('[command:batch] batch ran', out)

    def test_an_unknown_ci_count_runs_the_command(self):
        with mock.patch.object(capacity, 'resolve', return_value=self.resolved(2, None)):
            rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertIn('[command:batch] batch ran', out)

    def test_no_ci_capacity_configured_makes_no_gh_call(self):
        # the product declares no `ci:` block, so the resolver's own CI law never picks CiRuns —
        # this proves it end to end, with the real resolver, not the stub above
        with mock.patch('subprocess.run') as gh:
            rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        gh.assert_not_called()
        self.assertIn('[command:batch] batch ran', out)


class CommandStep(StepsTestCase):
    """Every command step — not only ``batch`` — carries the capacity overlay."""

    product_extra = ('steps:\n  batch: off\n'
                      '  health: python3 -c \'import os; '
                      'print(os.environ["ASF_CAPACITY_SESSIONS"])\'\n')

    def test_the_capacity_overlay_is_in_the_command_environment(self):
        rc, out = self.run_tick(steps='health')
        self.assertEqual(rc, 0)
        self.assertIn('[command:health] 4', out)


# ---- daily ------------------------------------------------------------------------

class DailyStepTests(StepsTestCase):
    def fake_parts(self, fail=()):
        def parts(product, root, event=None):
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
            return [(n, make(n)) for n in ('stale', 'rollup')]
        return mock.patch.object(step_daily, 'parts', parts)

    def test_one_line_per_part_and_no_commit_of_its_own(self):
        before = self.origin_commits()
        ctx = self.ctx()
        with self.fake_parts():
            step_daily.run(ctx, out=self.lines.append)
        self.assertEqual(self.lines, ['daily: stale ok — stale summary',
                                      'daily: rollup ok — rollup summary'])
        self.assertTrue(os.path.exists(os.path.join(ctx.record_root(), 'rollup.out')))
        self.assertEqual(self.origin_commits(), before)  # the tick's one commit carries it
        self.assertEqual(tick.finish(ctx, [{'step': 'daily', 'ok': True, 'seconds': 0}]), 0)
        self.assertEqual(_git(['show', 'main:rollup.out'], self.origin), 'rollup')
        self.assertTrue(_git(['log', '-1', '--format=%s', 'main'], self.origin).startswith('tick: state '))

    def test_a_failing_part_does_not_stop_the_others(self):
        with self.fake_parts(fail=('stale',)):
            with self.assertRaises(RuntimeError) as cm:
                step_daily.run(self.ctx(), out=self.lines.append)
        self.assertIn('stale', str(cm.exception))
        self.assertIn('daily: stale FAILED — stale broke', self.lines)
        self.assertIn('daily: rollup ok — rollup summary', self.lines)


class DailyPartsTests(StepsTestCase):
    def test_the_daily_is_two_parts_and_only_two(self):
        names = [n for n, _ in step_daily.parts(self.product, self.tmp)]
        self.assertEqual(names, ['stale', 'rollup'])
        self.assertEqual(step_daily.yesterday(__import__('datetime').date(2026, 3, 1)), '2026-02-28')

    def test_asf_tick_daily_prints_those_two_and_stamps_the_day(self):
        rc, out = self.run_tick(steps='daily')
        lines = [ln for ln in steps_only(out).splitlines() if ln.startswith('daily:')]
        self.assertEqual([ln.split()[1] for ln in lines], ['stale', 'rollup'])
        self.assertNotIn('daily: groom', out)
        self.assertNotIn('daily: file-bugs', out)
        self.assertTrue(os.path.exists(steps.stamp_path(self.product)))


class GroomStepTests(StepsTestCase):
    def test_the_step_is_in_its_place_and_owned_by_asf(self):
        self.assertEqual(steps.STEPS, ['record', 'health', 'groom', 'wave', 'prs', 'harvest',
                                       'batch', 'daily'])
        rows = {s: (owner, cmd) for s, owner, cmd in steps.resolve(self.product)}
        self.assertEqual(rows['groom'][0], 'asf')
        self.assertEqual(steps.ASF_CALLABLES['groom'], 'asf.tick.step_groom:run')

    def test_the_manifest_lists_it(self):
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertRegex(out, r'groom\s+asf\s+asf\.tick\.step_groom:run')

    def test_off_is_not_refused(self):
        self.write_product(f'repo_dir: {self.repo}\nsteps:\n  batch: off\n  groom: off\n')
        rc, out = self.run_tick(steps='groom')
        self.assertEqual(rc, 0)
        self.assertIn('tick: step groom off', out)

    def test_tick_steps_groom_runs_it_alone(self):
        seen = []
        with mock.patch.object(step_groom, 'run', lambda ctx, out=print: seen.append(1) or 0):
            rc, out = self.run_tick(steps='groom')
        self.assertEqual((rc, seen), (0, [1]))

    def test_the_step_runs_cmd_groom_applied_and_a_failure_raises(self):
        calls = []

        def fake(args, root):
            calls.append(args)
            return 3
        ctx = self.ctx()
        with mock.patch('asf.groom.groom.cmd_groom', fake), \
                mock.patch.object(answers, 'apply_pending_answers', lambda *a, **k: 0):
            with self.assertRaises(tick.StepFailed):
                step_groom.run(ctx, out=self.lines.append)
        self.assertTrue(calls[0].apply)
        self.assertIsNone(calls[0].answers_file)
        self.assertEqual(calls[0].event, ctx.event)


class GroomAnswersTests(StepsTestCase):
    """An adjudicate session's answers reach the cards on the next tick, not the next morning:
    the brief says "the next tick reads the answers file and applies it", but only the daily step
    passed it to ``groom`` — decided cards waited up to a day for their CARD → SPEC row. And an
    answers file a session had to stage in its worktree (its sandbox refused the state dir) is
    carried to the state dir once the session is over."""

    product_extra = 'steps:\n  batch: off\napprovals:\n  groom: auto\n'

    def answers_dir(self):
        return os.path.join(env.state_dir(self.product), 'groom')

    def write_answers(self, date, where=None):
        d = where or self.answers_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{date}.answers')
        with open(path, 'w') as f:
            f.write(f'- [ ] F-0001 sample — why → answer: adjudicator: yes ({date})\n')
        return path

    def applied(self, **kw):
        calls = []

        def fake_cmd_groom(args, root):
            calls.append(args)
            os.rename(args.answers_file, args.answers_file + '.done')
            print('groom 2026-01-03: applied 1')
            return 0
        sink = object()
        with mock.patch('asf.groom.groom.cmd_groom', fake_cmd_groom):
            n = answers.apply_pending_answers(self.product, self.tmp, event=sink,
                                                 out=self.lines.append, **kw)
        return n, calls, sink

    def test_every_pending_answers_file_is_applied_oldest_first(self):
        self.write_answers('2026-01-02')
        self.write_answers('2026-01-01')
        n, calls, sink = self.applied()
        self.assertEqual(n, 2)
        self.assertEqual([os.path.basename(c.answers_file) for c in calls],
                         ['2026-01-01.answers', '2026-01-02.answers'])
        self.assertTrue(all(c.event is sink and not c.apply for c in calls))
        self.assertEqual(self.lines[0], 'groom: answers 2026-01-01 applied — groom 2026-01-03: applied 1')

    def test_a_day_older_than_one_already_applied_is_superseded_not_applied(self):
        # the next day's adjudicator ruled the same questions afresh: the older ranks and closes
        # must not land on top of them (groom-2026-09-22's answers reached the state dir late)
        self.write_answers('2026-01-02')
        self.applied()
        older = self.write_answers('2026-01-01')
        self.lines.clear()
        n, calls, _ = self.applied()
        self.assertEqual((n, calls), (0, []))
        self.assertTrue(os.path.exists(older + '.superseded'))
        self.assertEqual(self.lines, ['groom: answers 2026-01-01 superseded by 2026-01-02 — not applied'])

    def test_nothing_without_the_groom_gate(self):
        self.write_product(f'repo_dir: {self.repo}\nsteps:\n  batch: off\n')
        self.product = env.load_product('sample')
        self.write_answers('2026-01-01')
        n, calls, _ = self.applied()
        self.assertEqual((n, calls), (0, []))

    def test_an_answers_file_staged_in_an_ended_groom_worktree_is_carried_and_applied(self):
        wt = os.path.join(self.tmp, 'wt-groom')
        staged = self.write_answers('2026-01-01', where=wt)
        self.session(job='groom-2026-01-01', kind='groom', item='F-0001', pid=DEAD_PID,
                     started='t1', worktree=wt, branch='groom/2026-01-01')
        self.session(job='groom-2026-01-01', ended='t2', end_reason='failed')
        n, calls, _ = self.applied()
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(staged))
        self.assertTrue(os.path.exists(os.path.join(self.answers_dir(), '2026-01-01.answers.done')))
        self.assertIn(f'groom: carried {staged} to the state dir', self.lines)

    def test_a_live_groom_sessions_worktree_is_left_alone(self):
        wt = os.path.join(self.tmp, 'wt-groom')
        staged = self.write_answers('2026-01-01', where=wt)
        self.session(job='groom-2026-01-01', kind='groom', item='F-0001', pid=os.getpid(),
                     started='t1', worktree=wt, branch='groom/2026-01-01')
        n, calls, _ = self.applied()
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(staged))

    def test_a_live_session_sent_back_into_the_groom_worktree_keeps_it(self):
        wt = os.path.join(self.tmp, 'wt-groom')
        staged = self.write_answers('2026-01-01', where=wt)
        self.session(job='groom-2026-01-01', kind='groom', item='F-0001', pid=DEAD_PID,
                     started='t1', worktree=wt, branch='groom/2026-01-01')
        self.session(job='groom-2026-01-01', ended='t2', end_reason='failed')
        self.session(job='correct-f-0001', kind='correct', item='F-0001', pid=os.getpid(),
                     started='t3', worktree=wt, branch='groom/2026-01-01')
        n, calls, _ = self.applied()
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(staged))



class AnswersOwnershipTests(StepsTestCase):
    """The answers helpers live in ``asf.groom.answers``; the record step no longer applies a file,
    the ``groom`` step does."""

    product_extra = 'steps:\n  batch: off\napprovals:\n  groom: auto\n'

    def write_answers(self, date):
        d = os.path.join(env.state_dir(self.product), 'groom')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{date}.answers')
        with open(path, 'w') as f:
            f.write(f'- [ ] F-0001 sample — why → answer: adjudicator: yes ({date})\n')
        return path

    def test_the_daily_module_no_longer_carries_them(self):
        for name in ('apply_pending_answers', 'carry_staged_answers', 'pending_answers_files',
                     'groom_every_tick'):
            self.assertFalse(hasattr(step_daily, name), name)
            self.assertEqual(hasattr(answers, name), name != 'groom_every_tick')

    def test_a_record_tick_leaves_a_pending_file_and_a_groom_tick_applies_it(self):
        path = self.write_answers('2026-01-01')
        applied = []

        def fake(args, root):
            applied.append(args.answers_file)
            if args.answers_file:
                os.rename(args.answers_file, args.answers_file + '.done')
            return 0
        with mock.patch('asf.groom.groom.cmd_groom', fake):
            self.run_tick(steps='record')
            self.assertTrue(os.path.exists(path))
            self.assertEqual(applied, [])
            self.run_tick(steps='groom')
        self.assertFalse(os.path.exists(path))
        self.assertIn(path, applied)


if __name__ == '__main__':
    unittest.main()
