"""The wave's one per-row filter (asf.tick.step_wave.screen): the live wave and the status
preview run it alike, so "Ready to launch N" is the N rows the wave would start (2026-10-04: the
status said 2 ready while the wave launched 0 — a held approval and a relaunch-cap park)."""
import types
import unittest
from unittest import mock

from asf import env
from asf.feeder import rows as feeder_rows
from asf.tick import step_wave
from asf.workers import pool as pool_mod


def row(iid, action='would launch'):
    return feeder_rows.Row(tier=2, kind=feeder_rows.PLAN_CODE, item_id=iid, feature_id='F-0001',
                           action=action, brief_kind='task', branch='', reason='')


PRODUCT = env.Product('p', {'main': 'main'})
ITEMS = {'T-0001': {}, 'T-0002': {}, 'T-0003': {}, 'T-0004': {}, 'T-0005': {},
         'T-0009': {'severity': 'S1'}}


def capped_for(*items):
    def assessment(_product, _row, wrow):
        wrow.cause = 'c'
        return ((f'{wrow.job} launched 2 time(s) on an unchanged head', '', None)
                if wrow.item in items else (None, '', None))
    return assessment


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(step_wave, 'preview_row',
                              side_effect=lambda _p, r, _i: pool_mod.Row(
                                  step_wave.job_name('task', r.item_id), r.item_id)),
            mock.patch.object(step_wave.trunkclose, 'closes_before_launch', return_value=False),
            mock.patch.object(step_wave.pool_mod, 'update_session'),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def both(self, planned, held=None, seats=2, running=(), host=None, bypass_open=False):
        """``(live, preview)``: the same plan screened as the wave acts and as status reads."""
        built = []

        def build(r, bypass):
            built.append(r.item_id)
            return (pool_mod.Row(step_wave.job_name('task', r.item_id), r.item_id),
                    types.SimpleNamespace(text='brief', kind='task'))
        ctx = types.SimpleNamespace(event=lambda *a, **k: None)
        live = step_wave.screen(PRODUCT, planned, ITEMS, list(running), held or {}, seats, host,
                                bypass_open, act=True, out=lambda _l: None, build=build, ctx=ctx)
        preview = step_wave.screen(PRODUCT, planned, ITEMS, list(running), held or {}, seats,
                                   host, bypass_open, act=False)
        return live, preview

    def verdicts(self, screened):
        return [(s.row.item_id, s.kind) for s in screened]

    def test_held_capped_and_seatless_rows_never_count_and_both_paths_agree(self):
        planned = [row('T-0001'), row('T-0002'), row('T-0003'), row('T-0004'), row('T-0005'),
                   row('T-0006', 'WAITS ON T-0001')]
        with mock.patch.object(step_wave, 'relaunch_assessment', side_effect=capped_for('T-0002')):
            live, preview = self.both(planned, held={'T-0001': ('touch_amendable_set',
                                                                'human-now')}, seats=2)
        want = [('T-0001', step_wave.HELD), ('T-0002', step_wave.CAPPED),
                ('T-0003', step_wave.STARTS), ('T-0004', step_wave.STARTS),
                ('T-0005', step_wave.NO_SEAT), ('T-0006', step_wave.WAITS)]
        self.assertEqual(self.verdicts(live), want)
        self.assertEqual(self.verdicts(preview), want)
        self.assertEqual([s.row.item_id for s in preview if s.starts], ['T-0003', 'T-0004'])

    def test_the_preview_parks_nothing(self):
        with mock.patch.object(step_wave, 'relaunch_assessment', side_effect=capped_for('T-0001')):
            step_wave.screen(PRODUCT, [row('T-0001')], ITEMS, [], {}, 2, act=False)
        step_wave.pool_mod.update_session.assert_not_called()

    def test_a_host_hold_holds_every_row_but_one_s1_bypass(self):
        planned = [row('T-0001'), row('T-0009'), row('T-0002')]
        with mock.patch.object(step_wave, 'relaunch_assessment', side_effect=capped_for()):
            live, preview = self.both(planned, seats=3, host=(True, 'host pressure load 9/1'),
                                      bypass_open=True)
        want = [('T-0001', step_wave.HOST), ('T-0009', step_wave.STARTS),
                ('T-0002', step_wave.HOST)]
        self.assertEqual(self.verdicts(live), want)
        self.assertEqual(self.verdicts(preview), want)
        self.assertTrue(preview[1].bypass)

    def test_a_capped_row_gives_its_seat_to_the_next(self):
        planned = [row('T-0001'), row('T-0002')]
        with mock.patch.object(step_wave, 'relaunch_assessment', side_effect=capped_for('T-0001')):
            live, preview = self.both(planned, seats=1)
        self.assertEqual([s.row.item_id for s in live if s.starts], ['T-0002'])
        self.assertEqual([s.row.item_id for s in preview if s.starts], ['T-0002'])


class TuneCappedTests(unittest.TestCase):
    """B-84835: the live wave runs every starting row through ``tune_mod.place`` *after*
    ``screen`` (:func:`asf.tick.step_wave.launch`); :func:`step_wave.would_start` — what the
    dwell watchdog and ``asf status`` read — never did, so a row held back only by its kind's
    tuned seat share still read STARTS, "would launch", there for as long as the tick kept
    holding it back."""

    def test_a_tune_held_row_never_reads_starts(self):
        with mock.patch.object(step_wave, 'preview_row',
                              side_effect=lambda _p, r, _i: pool_mod.Row(
                                  step_wave.job_name('task', r.item_id), r.item_id,
                                  kind='coder')):
            screened = step_wave.screen(PRODUCT, [row('T-0001'), row('T-0002')], ITEMS, [], {}, 2,
                                        act=False)
        self.assertEqual([s.kind for s in screened], [step_wave.STARTS, step_wave.STARTS])
        from asf import tune as tune_mod
        cfg = {'tune': {'enabled': True, 'bounds': {'coder': {'seats': [1, 1]}}}}
        with mock.patch.object(tune_mod, 'load_state',
                              return_value={'kinds': {'coder': {'seats': 1}}}):
            out = step_wave.tune_capped(PRODUCT, screened, [], ITEMS, cfg=cfg)
        self.assertEqual([(s.row.item_id, s.kind) for s in out],
                         [('T-0001', step_wave.STARTS), ('T-0002', step_wave.TUNE_HELD)])
        self.assertTrue(out[0].starts)
        self.assertFalse(out[1].starts)
        self.assertIn('tune: coder seat share', out[1].why)

    def test_untuned_rows_pass_through_unchanged(self):
        screened = [step_wave.Screened(row('T-0001'), wrow=pool_mod.Row('task-t-0001', 'T-0001'))]
        out = step_wave.tune_capped(PRODUCT, screened, [], ITEMS, cfg={})
        self.assertEqual([(s.row.item_id, s.kind) for s in out], [('T-0001', step_wave.STARTS)])


if __name__ == '__main__':
    unittest.main()
