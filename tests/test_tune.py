"""asf.tune — the self-tuning loop: per kind, the model and the seat share, tuned from the
session registry within the operator's bounds, one trial at a time, reverted on regress.

Every stream below is a fixture: runs written by hand (or a registry file for the end-to-end
case), so each verdict is worked out from the numbers beside it."""
import datetime
import io
import json
import os
import unittest
import uuid
from contextlib import redirect_stdout
from unittest import mock

from asf import env, release, tune
from asf.improve.measure import Run
from asf.workers import pool as pool_mod

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)    # the trial starts
T1 = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=UTC)    # the trial is judged
T2 = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)    # the keep is watched
MODELS = {'heavy': 'model-h', 'light': 'model-l', 'cheap': 'model-c'}


def cfg(bounds=None, enabled=True, **kw):
    t = dict({'enabled': enabled, 'window_days': 7, 'min_samples': 10, 'trial_samples': 10,
              'min_gain': 0.1, 'max_regress': 0.1},
             bounds=bounds if bounds is not None else {'review': {'models': ['heavy', 'light']}}, **kw)
    return {'tune': t, 'worker_pool': {'models': dict(MODELS)}}


def product():
    return env.Product(f'tune-{uuid.uuid4().hex[:8]}', {})


def runs(n, day, model, kind='review', minutes=60.0, landed=True, relaunch=0, dead=0, tag='a'):
    """``n`` runs of ``kind`` on ``model`` started on ``day`` (``YYYY-MM-DD``), one item each;
    the first ``relaunch`` are a job's second run, the first ``dead`` died."""
    out = []
    for i in range(n):
        out.append(Run(job=f'{kind}-{tag}-{i}', kind=kind, model=model, item=f'T-{tag}{i:03d}',
                       started=f'{day}T{i % 24:02d}:00:00Z', ended=f'{day}T{i % 24:02d}:30:00Z',
                       minutes=minutes, landed=landed and i >= dead,
                       end_reason='dead: stalled' if i < dead else 'finished', usd=None,
                       attempt=2 if i < relaunch else 1))
    return out


BASELINE = runs(10, '2026-09-30', 'model-h')                 # repair 0, 10 landed / 600 min


def quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


class Row:
    def __init__(self, job, kind, model, severity=None):
        self.job, self.item, self.kind, self.model, self.severity = job, job.upper(), kind, model, severity


class TrialTests(unittest.TestCase):
    def test_a_cheaper_model_that_keeps_repair_flat_is_kept(self):
        p, c = product(), cfg()
        wrote = quiet(tune.step, p, c, runs=BASELINE, now=T0)
        self.assertEqual([(r['event'], r['knob'], r['from'], r['to']) for r in wrote],
                         [('trial', 'model', 'heavy', 'light')])
        # 10 runs on the light model at half the minutes, repair still 0: objective +100 %
        trial = runs(10, '2026-10-02', 'model-l', minutes=30.0, tag='b')
        wrote = quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        self.assertEqual([(r['event'], r['from'], r['to']) for r in wrote], [('keep', 'heavy', 'light')])
        self.assertIn('objective +100% per minutes', wrote[0]['reason'])
        self.assertEqual(tune.load_state(p)['kinds']['review']['model'], 'light')
        # the placement hook: a review row on the kind's own label runs light; S1 is untouched
        rows = [Row('review-1', 'review', 'heavy'), Row('review-2', 'review', 'heavy', 'S1'),
                Row('coder-3', 'coder', 'light')]
        kept = tune.place(p, rows, [], cfg=c, out=lambda _l: None)
        self.assertEqual([r.model for r in kept], ['light', 'heavy', 'light'])

    def test_a_trial_whose_repair_rises_is_reverted(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        # cheaper and faster, but 5 of 10 are relaunches: repair 0.5 vs 0 — a regression
        trial = runs(10, '2026-10-02', 'model-l', minutes=20.0, relaunch=5, tag='b')
        wrote = quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        self.assertEqual([(r['event'], r['from'], r['to'], r['regress']) for r in wrote],
                         [('revert', 'light', 'heavy', True)])
        self.assertIn('regress: repair 0.5 vs 0', wrote[0]['reason'])
        ks = tune.load_state(p)['kinds']['review']
        self.assertNotIn('model', ks)
        self.assertNotIn('trial', ks)
        kept = tune.place(p, [Row('review-1', 'review', 'heavy')], [], cfg=c, out=lambda _l: None)
        self.assertEqual(kept[0].model, 'heavy')

    def test_a_failure_rate_regress_reverts_too(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        trial = runs(10, '2026-10-02', 'model-l', minutes=10.0, dead=3, tag='b')
        wrote = quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        self.assertEqual(wrote[0]['event'], 'revert')
        self.assertIn('failure rate', wrote[0]['reason'])

    def test_no_gain_is_reverted_and_not_tried_again_in_the_window(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        trial = runs(10, '2026-10-02', 'model-l', minutes=58.0, tag='b')   # +3 %: under 10 %
        wrote = quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        self.assertEqual(wrote[0]['event'], 'revert')
        self.assertTrue(wrote[0]['reason'].startswith('no gain'))
        more = runs(10, '2026-10-04', 'model-h', tag='c')
        self.assertEqual(quiet(tune.step, p, c, runs=BASELINE + trial + more, now=T2), [])

    def test_a_trial_waits_for_its_samples(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        trial = runs(4, '2026-10-02', 'model-l', tag='b')
        self.assertEqual(quiet(tune.step, p, c, runs=BASELINE + trial, now=T1), [])
        self.assertIn('trial', tune.load_state(p)['kinds']['review'])

    def test_a_kept_change_that_regresses_after_is_reverted(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        trial = runs(10, '2026-10-02', 'model-l', minutes=30.0, tag='b')
        quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        after = runs(10, '2026-10-04', 'model-l', minutes=30.0, relaunch=4, tag='c')
        wrote = quiet(tune.step, p, c, runs=BASELINE + trial + after, now=T2)
        self.assertEqual([(r['event'], r['from'], r['to']) for r in wrote], [('revert', 'light', 'heavy')])
        self.assertTrue(wrote[0]['reason'].startswith('after keep — regress'))
        self.assertEqual(tune.load_state(p)['kinds']['review']['model'], 'heavy')


class GuardTests(unittest.TestCase):
    def test_no_change_under_min_samples(self):
        p = product()
        self.assertEqual(quiet(tune.step, p, cfg(), runs=BASELINE[:9], now=T0), [])
        self.assertEqual(tune.ledger(p), [])

    def test_off_by_default_and_a_no_op(self):
        p = product()
        self.assertFalse(tune.settings({}, p)['enabled'])
        self.assertEqual(quiet(tune.step, p, {}, runs=BASELINE, now=T0), [])
        rows = [Row('review-1', 'review', 'heavy')]
        self.assertIs(tune.place(p, rows, [], cfg={}), rows)
        self.assertIsNone(tune.status_cell(p, cfg={}))

    def test_a_product_turns_it_on_for_itself(self):
        c = cfg(enabled=False)
        c['tune']['products'] = {'own': {'enabled': True, 'bounds': {'coder': {'seats': [1, 2]}}}}
        self.assertTrue(tune.settings(c, 'own')['enabled'])
        self.assertFalse(tune.settings(c, 'other')['enabled'])
        self.assertEqual(set(tune.settings(c, 'own')['bounds']), {'review', 'coder'})

    def test_bounds_are_respected(self):
        p = product()
        # the kind's own label is the only one its bounds allow: nothing to try
        c = cfg(bounds={'review': {'models': ['heavy']}})
        self.assertEqual(quiet(tune.step, p, c, runs=BASELINE, now=T0), [])
        # an unbounded kind is never touched, whatever its numbers
        c = cfg(bounds={'coder': {'models': ['light', 'cheap']}})
        self.assertEqual(quiet(tune.step, p, c, runs=BASELINE, now=T0), [])
        # a tuned value the bounds no longer hold is dropped by the hook and reverted by the pass
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        narrowed = cfg(bounds={'review': {'models': ['heavy']}})
        kept = tune.place(p, [Row('review-1', 'review', 'heavy')], [], cfg=narrowed, out=lambda _l: None)
        self.assertEqual(kept[0].model, 'heavy')
        wrote = quiet(tune.step, p, narrowed, runs=BASELINE, now=T1)
        self.assertEqual(wrote[0]['reason'], 'outside the operator bounds')

    def test_seat_share_trial_holds_rows_past_it(self):
        p = product()
        c = cfg(bounds={'coder': {'seats': [1, 2]}})
        base = runs(10, '2026-09-30', 'model-l', kind='coder')
        wrote = quiet(tune.step, p, c, runs=base, now=T0)
        self.assertEqual([(r['knob'], r['from'], r['to']) for r in wrote], [('seats', 2, 1)])
        said = []
        rows = [Row('coder-1', 'coder', 'light'), Row('coder-2', 'coder', 'light'),
                Row('coder-3', 'coder', 'light', 'S1')]
        kept = tune.place(p, rows, [], cfg=c, out=said.append)
        self.assertEqual([r.job for r in kept], ['coder-1', 'coder-3'])
        self.assertIn('tune: coder seat share 1 (live 1)', said[0])
        kept = tune.place(p, rows[:1], [{'kind': 'coder'}], cfg=c, out=said.append)
        self.assertEqual(kept, [])

    def test_one_live_trial_per_kind(self):
        p = product()
        c = cfg(bounds={'review': {'models': ['heavy', 'light'], 'seats': [1, 3]}})
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        self.assertEqual(quiet(tune.step, p, c, runs=BASELINE, now=T1), [])
        self.assertEqual([r['event'] for r in tune.ledger(p)], ['trial'])

    def test_freeze_stops_trials(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        quiet(tune.freeze, p, True, now=T0)
        events = [(r['event'], r['reason']) for r in tune.ledger(p)]
        self.assertEqual(events, [('trial', events[0][1]), ('revert', 'frozen'), ('freeze', 'operator')])
        self.assertNotIn('trial', tune.load_state(p)['kinds']['review'])
        self.assertEqual(quiet(tune.step, p, c, runs=BASELINE, now=T1), [])
        self.assertIn('frozen', tune.status_cell(p, cfg=c))
        quiet(tune.freeze, p, False, now=T1)
        self.assertEqual(len(quiet(tune.step, p, c, runs=BASELINE, now=T1)), 0)  # heavy→light left behind
        self.assertFalse(tune.load_state(p)['frozen'])


class SurfaceTests(unittest.TestCase):
    def test_status_history_and_events(self):
        p, c = product(), cfg()
        events = []
        quiet(tune.step, p, c, runs=BASELINE, now=T0, event=lambda k, **f: events.append((k, f)))
        self.assertEqual(events[0][0], 'tune')
        self.assertEqual(events[0][1]['session_kind'], 'review')
        trial = runs(4, '2026-10-02', 'model-l', tag='b')
        cell = tune.status_cell(p, cfg=c, runs=BASELINE + trial)
        self.assertEqual(cell, 'tune: review heavy→light trial 4/10, repair 0 vs 0')
        full = runs(10, '2026-10-02', 'model-l', minutes=30.0, tag='b')
        quiet(tune.step, p, c, runs=BASELINE + full, now=T1)
        text = tune.render_history(tune.ledger(p))
        self.assertIn('| 2026-10-01T12:00 | review | trial | model | heavy→light | baseline 10 runs', text)
        self.assertIn('| review | keep | model | heavy→light | objective +100% per minutes', text)
        self.assertIn('last: review model heavy→light keep', tune.status_cell(p, cfg=c))

    def test_cli(self):
        from asf.cli import build_parser
        args = build_parser().parse_args(['tune', 'history', '--product', 'x', '--json'])
        self.assertEqual((args.tune_command, args.product, args.json), ('history', 'x', True))
        p = product()
        with mock.patch.object(env, 'load_product', return_value=p):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(tune.cmd_tune(build_parser().parse_args(['tune', 'freeze'])), 0)
                self.assertEqual(tune.cmd_tune(build_parser().parse_args(['tune', 'history'])), 0)
        self.assertIn('| * | freeze |', out.getvalue())
        self.assertTrue(tune.load_state(p)['frozen'])

    def test_end_to_end_off_the_session_registry(self):
        """``step`` with no runs reads the product's own registry (``sessions.jsonl``)."""
        p, c = product(), cfg()
        path = pool_mod.sessions_path(p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for r in BASELINE:
                f.write(json.dumps({'job': r.job, 'item': r.item, 'kind': r.kind, 'model': r.model,
                                    'started': r.started, 'pid': 1}) + '\n')
                f.write(json.dumps({'job': r.job, 'ended': r.ended, 'end_reason': 'finished',
                                    'harvested': True}) + '\n')
        now = datetime.datetime.now(UTC)
        with mock.patch.object(tune, '_since', return_value='2026-09-01T00:00:00Z'):
            wrote = quiet(tune.step, p, c, now=now)
        self.assertEqual([(r['event'], r['to']) for r in wrote], [('trial', 'light')])


class CriterionTests(unittest.TestCase):
    def test_off_is_unmet(self):
        self.assertEqual(tune.criterion(product(), 7, now=T1, cfg={}), (False, 'tune.enabled is off'))

    def test_a_kept_change_and_no_regression_is_met(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        trial = runs(10, '2026-10-02', 'model-l', minutes=30.0, tag='b')
        quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        met, ev = tune.criterion(p, 7, now=T1, cfg=c, runs=BASELINE + trial)
        self.assertTrue(met)
        self.assertIn('1 kept change(s), 0 unreverted regression(s) in 7 d', ev)

    def test_a_regression_the_pass_has_not_reverted_is_unmet(self):
        p, c = product(), cfg()
        quiet(tune.step, p, c, runs=BASELINE, now=T0)
        trial = runs(10, '2026-10-02', 'model-l', minutes=30.0, tag='b')
        quiet(tune.step, p, c, runs=BASELINE + trial, now=T1)
        after = runs(10, '2026-10-04', 'model-l', relaunch=5, tag='c')
        met, ev = tune.criterion(p, 7, now=T2, cfg=c, runs=BASELINE + trial + after)
        self.assertFalse(met)
        self.assertIn('1 unreverted regression(s)', ev)

    def test_release_appends_it_as_its_own_criterion(self):
        p = product()
        with mock.patch.object(tune, 'criterion', return_value=(True, 'ev')):
            c = release.tune_criterion(p, 7, '2026-10-03T12:00:00Z')
        self.assertEqual((c.key, c.met, c.evidence), ('tune', True, 'ev'))
        with mock.patch.object(tune, 'criterion', side_effect=OSError('x')):
            self.assertFalse(release.tune_criterion(p, 7, '2026-10-03T12:00:00Z').met)


if __name__ == '__main__':
    unittest.main()
