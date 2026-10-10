"""asf.pause — the record, the predicate and the two verbs that write it (F-0137 §1), the two
doors that ask it (§2, §3), and the three readers that say it (§4). Each class below is a plain
``unittest.TestCase`` with its own tmp ``ASF_HOME`` (F-0137 PD14) — never a subclass of an
existing tick harness.
"""
import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import capacity, cli, env, pause
from asf.tick import step_health, step_wave
from asf.feeder import rows as feeder_rows_mod
from asf.feeder import tiers
from asf import invariants
from asf.views import index_reader


class FakeCtx:
    """A lightweight stand-in for :class:`asf.tick.tick.Context` — the pieces the wave and
    health steps touch (``product``, ``counts``, ``event``, ``record_root``), with no record
    clone and no git: a plain object, not a subclass of the tick's own harness."""

    def __init__(self, product, root=None):
        self.product = product
        self._root = root
        self.counts = {'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}
        self.events = []
        self.seats = None

    def record_root(self):
        return self._root

    def event(self, kind, **fields):
        rec = dict(fields, kind=kind)
        self.events.append(rec)
        return rec


class TheRecord(unittest.TestCase):
    """``pause.held``/``read``/``pause``/``resume``/``text``/``hold_reason`` — the leaf module,
    no CLI, no product file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_held_is_none_with_no_file(self):
        self.assertIsNone(pause.held('sample'))
        self.assertIsNone(pause.read('sample'))

    def test_pause_then_held_returns_exactly_the_reason_author_and_timestamp(self):
        now = datetime.datetime(2026, 10, 7, 9, 30, 0)
        lines = pause.pause('sample', 'release freeze until 0.2 ships', 'op1', now=now)
        self.assertTrue(lines)
        record = pause.held('sample')
        self.assertIsNotNone(record)
        self.assertEqual(record['reason'], 'release freeze until 0.2 ships')
        self.assertEqual(record['by'], 'op1')
        self.assertEqual(record['at'], now.astimezone().isoformat(timespec='seconds'))
        # held() also takes a Product or a stand-in with a .name, per PD4
        product = env.Product('sample', {})
        self.assertEqual(pause.held(product), record)
        stand_in = types.SimpleNamespace(name='sample', repo_dir='')
        self.assertEqual(pause.held(stand_in), record)
        # an object with no usable name is not paused, never an exception
        self.assertIsNone(pause.held(types.SimpleNamespace()))
        self.assertIsNone(pause.held(None))

    def test_a_truncated_or_non_json_file_is_no_pause(self):
        path = pause.pause_path('sample')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"reason": "x", "by": ')  # truncated
        self.assertIsNone(pause.held('sample'))
        with open(path, 'w', encoding='utf-8') as f:
            f.write('[]')  # valid JSON, not a dict
        self.assertIsNone(pause.held('sample'))

    def test_resume_removes_it_and_a_second_resume_is_rc_0_with_a_line_not_an_error(self):
        pause.pause('sample', 'x', 'op1')
        self.assertIsNotNone(pause.held('sample'))
        lines = pause.resume('sample')
        self.assertIsNone(pause.held('sample'))
        self.assertTrue(lines)
        # resuming what is not paused is not an error — it is the operator making sure
        lines2 = pause.resume('sample')
        self.assertTrue(lines2)
        self.assertIsNone(pause.held('sample'))

    def test_text_and_hold_reason_both_name_the_reason_the_author_and_the_date(self):
        now = datetime.datetime(2026, 10, 7, 9, 30, 0)
        pause.pause('sample', 'release freeze', 'op1', now=now)
        record = pause.held('sample')
        at = now.astimezone().isoformat(timespec='seconds')
        self.assertEqual(pause.text(record), f'paused since {at} (release freeze; by op1)')
        self.assertEqual(pause.hold_reason(record),
                         f'launches paused: release freeze (by op1, since {at})')

    def test_pause_overwrites_rather_than_refuses(self):
        pause.pause('sample', 'first reason', 'op1')
        pause.pause('sample', 'second reason', 'alice')
        record = pause.held('sample')
        self.assertEqual(record['reason'], 'second reason')
        self.assertEqual(record['by'], 'alice')


class TheCommands(unittest.TestCase):
    """``asf pause`` / ``asf resume`` through ``cli.main`` — the shape ``tests/test_cli.py``
    drives commands with: stdout captured, ``cli.main([...])``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w', encoding='utf-8') as f:
            f.write('product: sample\n')

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_pause_with_no_reason_is_rc_2_with_the_reason_line(self):
        rc, out = self._run(['pause', '--product', 'sample'])
        self.assertEqual(rc, 2)
        self.assertIn('asf pause needs --reason "<why>" — it is recorded with the pause', out)
        self.assertIsNone(pause.held('sample'))

    def test_pause_with_a_reason_writes_the_file_and_prints_three_lines(self):
        rc, out = self._run(['pause', '--product', 'sample', '--reason', 'release freeze',
                             '--by', 'op1'])
        self.assertEqual(rc, 0)
        record = pause.held('sample')
        self.assertIsNotNone(record)
        self.assertEqual(record['reason'], 'release freeze')
        self.assertEqual(record['by'], 'op1')
        lines = out.strip('\n').splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('sample launches paused — release freeze (by op1)', lines[0])
        self.assertIn('session', lines[1])
        self.assertIn('asf resume --product sample', lines[2])

    def test_resume_removes_the_file(self):
        self._run(['pause', '--product', 'sample', '--reason', 'release freeze'])
        self.assertIsNotNone(pause.held('sample'))
        rc, out = self._run(['resume', '--product', 'sample'])
        self.assertEqual(rc, 0)
        self.assertIsNone(pause.held('sample'))
        self.assertIn('sample launches resumed', out)

    def test_by_defaults_to_user_env(self):
        with mock.patch.dict(os.environ, {'USER': 'env-user'}):
            rc, _out = self._run(['pause', '--product', 'sample', '--reason', 'x'])
        self.assertEqual(rc, 0)
        self.assertEqual(pause.held('sample')['by'], 'env-user')


class TheOtherDoor(unittest.TestCase):
    """``step_health.handle_dead`` — the cold retry's own door (F-0137 §3): a pause takes the
    same spend-nothing outcome health already has for an empty reap, never the round-spending
    hold."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {})
        self.lines = []

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_dead_session_of_a_paused_product_takes_no_cold_retry_and_spends_no_round(self):
        pause.pause('sample', 'release freeze', 'op1')
        ctx = FakeCtx(self.product)
        session = {'job': 'fix-bug-b-0001', 'item': 'B-0001'}
        with mock.patch.object(step_health.stall_mod, 'correct_once',
                                lambda *a, **k: self.fail('cold retry started while paused')):
            result = step_health.handle_dead(ctx, session, out=self.lines.append, items={})
        self.assertEqual(result, 'paused')
        self.assertTrue(any('fix-bug-b-0001' in ln and 'B-0001' in ln and 'launches paused' in ln
                            for ln in self.lines), self.lines)
        self.assertEqual(ctx.counts['relaunches'], 0)
        self.assertEqual(ctx.events, [])  # no held event, no round written to the ledger

    def test_an_unpaused_dead_session_is_unaffected(self):
        ctx = FakeCtx(self.product)
        session = {'job': 'fix-bug-b-0001', 'item': 'B-0001'}
        with mock.patch.object(step_health.stall_mod, 'correct_once', lambda *a, **k: True):
            result = step_health.handle_dead(ctx, session, out=self.lines.append, items={})
        self.assertEqual(result, 'corrected')
        self.assertEqual(ctx.counts['relaunches'], 1)


def _sample_rows():
    return [
        feeder_rows_mod.Row(tier=tiers.TIER_REST, kind='BUG → FIX', item_id='B-0001',
                            feature_id='', action='would launch fix-b-0001',
                            brief_kind='fix-bug', branch='fix/B-0001', reason=''),
        feeder_rows_mod.Row(tier=tiers.TIER_REST, kind='BUG → FIX', item_id='B-0002',
                            feature_id='', action='would launch fix-b-0002',
                            brief_kind='fix-bug', branch='fix/B-0002', reason=''),
        # the S1 row: a pause holds it exactly like any other — no load-hold bypass applies
        feeder_rows_mod.Row(tier=tiers.TIER_S1, kind='BUG → FIX', item_id='B-0003',
                            feature_id='', action='would launch fix-b-0003',
                            brief_kind='fix-bug', branch='fix/B-0003', reason=''),
    ]


def _wave_patches(rows, wave_fn, lane_pass_calls, push_deferred_calls, running=(),
                  write_demand_calls=None):
    """Everything :func:`step_wave.launch` touches besides the pause itself, stubbed out so the
    one thing under test is the hold (:class:`TheWaveHolds`) or the demand it writes
    (:class:`TheSlotsAreLent`)."""
    resolved = capacity.Resolved(sessions=5, sessions_bound='product', ci=None, ci_bound=None,
                                 ci_inflight=None, batch={}, reserve={})
    cloud = types.SimpleNamespace(on=False, max_inflight=0)
    brief = types.SimpleNamespace(model='test-model', text='brief text', add_dirs=(),
                                  card_digest='deadbeef', kind='fix-bug')
    patches = [
        mock.patch.object(index_reader, 'load', return_value=({}, False)),
        mock.patch.object(step_wave.approvals, 'raise_holds', lambda ctx, out: {}),
        mock.patch.object(step_wave.approvals, 'announce_console_amends', lambda *a, **k: None),
        mock.patch.object(feeder_rows_mod, 'plan_rows', lambda *a, **k: list(rows)),
        mock.patch.object(invariants, 'feeder_gate',
                          lambda product, planned, items, out=print: list(planned)),
        mock.patch.object(step_wave.capacity_mod, 'resolve', lambda product, *a, **k: resolved),
        mock.patch.object(step_wave, 'cloud_settings', lambda product: cloud),
        mock.patch.object(step_wave, 'cloud_readiness',
                          lambda product, cl: (False, 'cloud lane off')),
        mock.patch.object(step_wave, 'plan_inputs', lambda product, root, items: {}),
        mock.patch.object(step_wave, 'demand', lambda *a, **k: 3),
        mock.patch.object(step_wave, 'host_hold', lambda planned: (False, '', {})),
        mock.patch.object(step_wave, 'inflight', lambda product: list(running)),
        mock.patch.object(step_wave.tune_mod, 'wave_hook',
                          lambda product, rows, running, out=print, event=None: rows),
        mock.patch.object(step_wave, '_build', lambda *a, **k: brief),
        mock.patch.object(step_wave, 'relaunch_capped', lambda product, row, wrow, out=print: ''),
        mock.patch.object(step_wave, 'lane_pass', lambda ctx, out, **k: lane_pass_calls.append(1)),
        mock.patch.object(step_wave, 'push_deferred',
                          lambda ctx, out=print: push_deferred_calls.append(1)),
        mock.patch.object(step_wave, '_wave', wave_fn),
    ]
    if write_demand_calls is not None:
        patches.append(mock.patch.object(
            step_wave.capacity_mod, 'write_demand',
            lambda name, inflight, wanted: write_demand_calls.append((name, inflight, wanted))))
    return patches


class TheWaveHolds(unittest.TestCase):
    """``step_wave.launch`` over a paused product (F-0137 §2): plans, holds every launching
    row — S1 included — emits one ``paused`` event and returns 0 without ever reaching
    ``_wave``. Everything launch() touches besides the pause itself is stubbed out, so the one
    thing under test is the hold — the same approach :func:`TheOtherDoor` takes for the other
    door, scaled up to the wave's much larger function."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {})
        self.lines = []
        self.lane_pass_calls = []
        self.push_deferred_calls = []

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, rows, wave_fn, running=()):
        ctx = FakeCtx(self.product, root=self.tmp)
        patches = _wave_patches(rows, wave_fn, self.lane_pass_calls, self.push_deferred_calls,
                                running=running)
        for p in patches:
            p.start()
        try:
            rc = step_wave.run(ctx, out=self.lines.append)
        finally:
            for p in patches:
                p.stop()
        return ctx, rc

    def test_a_paused_product_holds_every_launching_row_s1_included_and_never_waves(self):
        pause.pause('sample', 'release freeze', 'op1')
        rows = _sample_rows()

        def fail_wave(*a, **k):
            self.fail('_wave was called while the product is paused')

        ctx, rc = self._run(rows, fail_wave)

        self.assertEqual(rc, 0)
        for row in rows:
            job = f'{row.brief_kind}-{row.item_id}'.lower()
            self.assertTrue(any(ln.startswith(f'waits    {job}') and
                                'held: launches paused: release freeze' in ln
                                for ln in self.lines), (row.item_id, self.lines))
        self.assertTrue(any(ln.startswith('wave:') and 'no new session this tick' in ln and
                            'recording and harvesting go on' in ln and
                            'sessions in flight finish and land' in ln
                            for ln in self.lines), self.lines)
        paused_events = [e for e in ctx.events if e['kind'] == 'paused']
        self.assertEqual(len(paused_events), 1)
        self.assertEqual(paused_events[0]['reason'], 'release freeze')
        self.assertEqual(paused_events[0]['by'], 'op1')
        self.assertEqual(paused_events[0]['rows'], 3)
        self.assertEqual(self.lane_pass_calls, [1])
        self.assertEqual(self.push_deferred_calls, [1])
        self.assertEqual(ctx.counts['launches'], 0)

    def test_the_same_plan_unpaused_calls_wave(self):
        # no pause written: this half is what makes the first half mean something
        rows = _sample_rows()
        waved = []

        def fake_wave(product, worker_rows, n, brief_fn=None, out=print, **kw):
            waved.extend(worker_rows)
            return ([(worker_rows[0], {'account': 'acct-a', 'model': 'test-model', 'pid': 1})]
                    if worker_rows else [], [])

        ctx, rc = self._run(rows, fake_wave)

        self.assertEqual(rc, 0)
        self.assertTrue(waved, self.lines)
        self.assertEqual(ctx.counts['launches'], 1)
        self.assertEqual([e['kind'] for e in ctx.events if e['kind'] == 'paused'], [])


class TheReadyPreviewHolds(unittest.TestCase):
    """``step_wave.would_start`` over a paused product (B-84833): the preview ``asf status``'s
    Ready-to-launch count and ``asf.dwell``'s ``launchable_idle`` watchdog both read must hold
    every row exactly as the live wave does — else a pause reads, to both, as a seat sitting
    idle on a row that "passes the wave's filter, not started yet", and the watchdog alarms on
    an operator's own intentional hold."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {})

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _preview(self, rows):
        resolved = capacity.Resolved(sessions=5, sessions_bound='product', ci=None,
                                     ci_bound=None, ci_inflight=None, batch={}, reserve={})
        cloud = types.SimpleNamespace(on=False, max_inflight=0)
        with mock.patch.object(index_reader, 'load', return_value=({}, False)), \
                mock.patch.object(feeder_rows_mod, 'plan_rows', lambda *a, **k: list(rows)), \
                mock.patch.object(invariants, 'feeder_gate',
                                  lambda product, planned, items, out=print: list(planned)), \
                mock.patch.object(step_wave.capacity_mod, 'resolve',
                                  lambda product, *a, **k: resolved), \
                mock.patch.object(step_wave, 'cloud_settings', lambda product: cloud), \
                mock.patch.object(step_wave, 'cloud_readiness',
                                  lambda product, cl: (False, 'cloud lane off')), \
                mock.patch.object(step_wave, 'plan_inputs', lambda product, root, items: {}), \
                mock.patch.object(step_wave, 'host_hold', lambda planned: (False, '', {})), \
                mock.patch.object(step_wave, 'inflight', lambda product: []):
            return step_wave.would_start(self.product, self.tmp)

    def test_a_paused_products_preview_holds_every_row_none_of_them_start(self):
        pause.pause('sample', 'release freeze', 'op1')
        rows = _sample_rows()
        screened, seats, running = self._preview(rows)
        self.assertEqual(seats, 5)
        self.assertEqual(running, [])
        self.assertFalse(any(s.starts for s in screened), screened)
        for s in screened:
            self.assertEqual(s.kind, step_wave.PAUSED)
            self.assertIn('launches paused: release freeze', s.why)

    def test_the_same_plan_unpaused_starts(self):
        # no pause written: this half is what makes the first half mean something
        rows = _sample_rows()
        screened, _seats, _running = self._preview(rows)
        self.assertTrue(all(s.starts for s in screened), screened)


class TheSlotsAreLent(unittest.TestCase):
    """A paused product's demand reads ``wanted 0`` (F-0137 §2, C4/P8) — so a partner's
    :func:`asf.capacity.claim` borrows the difference — while its own live sessions still
    hold their seats."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        self.product = env.Product('sample', {})
        self.lines = []

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_paused_launch_writes_wanted_zero_at_the_live_inflight_count(self):
        from asf.workers import pool as pool_mod
        pause.pause('sample', 'release freeze', 'op1')
        rows = _sample_rows()
        running = [{'job': 'fix-bug-b-9000', 'pid': 1}, {'job': 'fix-bug-b-9001', 'pid': 1}]
        write_demand_calls = []
        ctx = FakeCtx(self.product, root=self.tmp)
        patches = _wave_patches(rows, lambda *a, **k: self.fail('_wave called while paused'),
                                [], [], running=running, write_demand_calls=write_demand_calls)
        for p in patches:
            p.start()
        try:
            rc = step_wave.launch(ctx, out=self.lines.append)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, 0)
        self.assertEqual(write_demand_calls, [('sample', 2, 0)])

    def test_a_partner_borrows_the_difference_while_live_sessions_keep_their_seats(self):
        from asf.workers import pool as pool_mod
        # 'bots' is paused: its last wave recorded 2 in flight, 0 wanted (C4, P8)
        capacity.write_demand('bots', 2, 0)
        for i in range(2):
            pool_mod.append_session('bots', {'job': f'task-t-900{i}', 'started': pool_mod.now_iso(),
                                             'pid': 1})
        self.assertEqual(capacity.inflight_sessions('bots'), 2)
        # a partner with a share of 5 borrows the 3 slots 'bots' is not using
        self.assertEqual(capacity.claim('bots', 5), 2)
        # 'bots' own two live sessions are never lent out from under it
        self.assertEqual(capacity.claim('bots', 1), 1)
        self.assertEqual(capacity.claim('bots', 2), 2)


class TheOperatorCanSee(unittest.TestCase):
    """The three places an operator looks when nothing is happening (F-0137 §4):
    ``status.paused_cell``, ``doctor.scheduler_rows``, ``dry_run._wave_rows``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {})
        self.lines = []

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paused_cell_names_the_launch_pause_first_then_the_clock_pauses_else_none(self):
        from asf import scheduler
        from asf.views import status
        self.assertIsNone(status.paused_cell({}, self.product))
        pause.pause('sample', 'release freeze', 'op1')
        cell = status.paused_cell({}, self.product)
        self.assertIn('launches paused since', cell)
        self.assertIn('release freeze; by op1', cell)
        self.assertIn('asf resume --product sample', cell)
        self.assertIn('the tick still records and harvests', cell)
        scheduler.pause('sample', ['tick'], 'operator reset', 'op')
        cell = status.paused_cell({}, self.product)
        # the launch pause's sentence comes first, the clock pause's folded in after it
        self.assertLess(cell.index('launches paused since'), cell.index('asf.sample.tick'))
        self.assertIn('operator reset; by op', cell)
        pause.resume('sample')
        scheduler.resume('sample', ['tick'])
        self.assertIsNone(status.paused_cell({}, self.product))

    def test_doctor_carries_the_yellow_row_for_launchd_and_non_launchd_kinds(self):
        from asf import doctor
        pause.pause('sample', 'release freeze', 'op1')
        rows = doctor.scheduler_rows({}, self.product, jobs=[])
        paused_rows = [r for r in rows if r[1] == 'PAUSED']
        self.assertEqual(len(paused_rows), 1, rows)
        self.assertEqual(paused_rows[0][0], doctor.YELLOW)
        self.assertIn('launches paused since', paused_rows[0][2])
        self.assertIn('asf resume --product sample', paused_rows[0][2])
        # PD5: the row is above the early return for a product whose scheduler is not launchd
        rows = doctor.scheduler_rows({'scheduler': {'kind': 'cron'}}, self.product)
        paused_rows = [r for r in rows if r[1] == 'PAUSED']
        self.assertEqual(len(paused_rows), 1, rows)
        self.assertEqual(paused_rows[0][0], doctor.YELLOW)

    def test_dry_run_wave_rows_prints_the_held_line_and_the_wave_line(self):
        from asf import invariants as invariants_mod
        from asf.tick import dry_run, step_wave
        pause.pause('sample', 'release freeze', 'op1')
        row = feeder_rows_mod.Row(tier=0, kind='BUG → FIX', item_id='B-0001', feature_id='',
                                  action='would launch fix-bug-b-0001', brief_kind='fix-bug',
                                  branch='fix/B-0001', reason='')
        product = types.SimpleNamespace(name='sample', repo_dir='')
        root = tempfile.mkdtemp(prefix='asf-dry-wave-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        open(os.path.join(root, 'index.json'), 'w').close()
        with mock.patch.object(index_reader, 'load', return_value=({}, None)), \
                mock.patch.object(step_wave, 'inflight', return_value={}), \
                mock.patch.object(step_wave.capacity_mod, 'resolve',
                                  return_value=types.SimpleNamespace(sessions=2)), \
                mock.patch.object(step_wave, 'plan_inputs', return_value={}), \
                mock.patch.object(feeder_rows_mod, 'plan_rows', lambda *a, **kw: [row]), \
                mock.patch.object(invariants_mod, 'feeder_gate', lambda p, rows, *a, **kw: rows):
            dry_run._wave_rows(product, root, self.lines.append)
        self.assertFalse([l for l in self.lines if l.startswith('would launch')], self.lines)
        self.assertTrue(any(l.startswith('waits        fix-bug-b-0001') and
                            'held: launches paused: release freeze' in l
                            for l in self.lines), self.lines)
        self.assertEqual(self.lines[-1],
                         'wave: launches paused: release freeze (by op1, since ' +
                         pause.held('sample')['at'] + ') — no new session this tick; '
                         'recording and harvesting go on')


class ThePreviewAgrees(unittest.TestCase):
    """B-84029: ``step_wave.would_start`` is the preview ``asf status``'s Ready-to-launch cell
    and ``asf.dwell``'s ``launchable_idle`` watchdog both read — it must hold a paused row the
    same way the live wave does (C2, ``docs/reviews/1-t-0651.md``), never report it as starting
    while a seat sits free."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pause_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {})

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_paused_products_preview_holds_the_row_it_never_starts_it(self):
        pause.pause('sample', 'release freeze', 'op1')
        rows = _sample_rows()
        resolved = capacity.Resolved(sessions=5, sessions_bound='product', ci=None,
                                     ci_bound=None, ci_inflight=None, batch={}, reserve={})
        cloud = types.SimpleNamespace(on=False, max_inflight=0)
        with mock.patch.object(step_wave, 'inflight', lambda product: []), \
                mock.patch.object(step_wave.capacity_mod, 'resolve',
                                  lambda product, *a, **k: resolved), \
                mock.patch.object(step_wave, 'cloud_settings', lambda product: cloud), \
                mock.patch.object(step_wave, 'cloud_readiness',
                                  lambda product, cl: (False, 'cloud lane off')), \
                mock.patch.object(step_wave, 'plan_inputs', lambda product, root, items: {}), \
                mock.patch.object(step_wave, 'gated_plan',
                                  lambda items, product, running, seats, inputs, out=print:
                                  (list(rows), set())), \
                mock.patch.object(step_wave, 'host_hold', lambda planned: (False, '', {})):
            screened, seats, running = step_wave.would_start(self.product, self.tmp, items={})
        self.assertTrue(screened)
        for s in screened:
            self.assertEqual(s.kind, step_wave.PAUSED, (s.row.item_id, s.kind, s.why))
            self.assertFalse(s.starts)
            self.assertIn('held: launches paused: release freeze', s.why)

    def test_the_same_preview_unpaused_starts_every_row(self):
        rows = _sample_rows()
        resolved = capacity.Resolved(sessions=5, sessions_bound='product', ci=None,
                                     ci_bound=None, ci_inflight=None, batch={}, reserve={})
        cloud = types.SimpleNamespace(on=False, max_inflight=0)
        with mock.patch.object(step_wave, 'inflight', lambda product: []), \
                mock.patch.object(step_wave.capacity_mod, 'resolve',
                                  lambda product, *a, **k: resolved), \
                mock.patch.object(step_wave, 'cloud_settings', lambda product: cloud), \
                mock.patch.object(step_wave, 'cloud_readiness',
                                  lambda product, cl: (False, 'cloud lane off')), \
                mock.patch.object(step_wave, 'plan_inputs', lambda product, root, items: {}), \
                mock.patch.object(step_wave, 'gated_plan',
                                  lambda items, product, running, seats, inputs, out=print:
                                  (list(rows), set())), \
                mock.patch.object(step_wave, 'host_hold', lambda planned: (False, '', {})):
            screened, seats, running = step_wave.would_start(self.product, self.tmp, items={})
        self.assertEqual([s.row.item_id for s in screened if s.starts],
                         [r.item_id for r in rows])
