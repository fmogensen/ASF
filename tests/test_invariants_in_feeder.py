"""The feeder's invariants decide before the cut, never after it.

2026-09-27 04:22–04:30 (a product tick): the local lane sat at 0/9 while ``asf next`` said "8 would
launch". Every one of those eight rows ranked first and every one violated a launch-time
invariant — I5 (``FIX → CORRECT T-0038 @cloud/tinkerer-mode-t1 — F-0092's spec and plan not on
the trunk``) or I4 (``STARVED → PLAN F-0111 @cloud/plan-F-0111 — … in occupancy
waiting_landing``). The capacity cut gave them the seats, the wave's gate dropped them, and the
views kept calling them ready. So the feeder evaluates I4/I5/I7 before selection: a violating
row is a WAITS row carrying the invariant's reason, takes no seat, and the seat goes to the next
valid row. The wave's own gate stays as a safety net and says so if it ever fires.
"""
import argparse
import contextlib
import io
import os
import unittest
from unittest import mock

from asf import capacity, invariants
from asf.feeder import render
from asf.feeder import rows as R
from asf.tick import step_wave

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.…` does not
    from test_tick_steps import StepsTestCase, _brief
except ImportError:  # pragma: no cover - import shape only
    from tests.test_tick_steps import StepsTestCase, _brief


def violators():
    corr = [R.Row(2, R.FIX_CORRECT, iid, fid, R.LAUNCH, 'correct', branch, 'correction pending')
            for iid, fid, branch in (('T-0038', 'F-0092', 'cloud/tinkerer-mode-t1'),
                                     ('T-0039', 'F-0092', 'cloud/tinkerer-mode-t2'),
                                     ('T-0072', 'F-0097', 'cloud/voice-parity-t2'))]
    plan = [R.Row(2, R.STARVED_PLAN, 'F-0111', 'F-0111', R.LAUNCH, 'plan', 'cloud/plan-F-0111',
                  'starved')]
    return corr + plan


def valid(n=3):
    return [R.Row(2, R.CARD_SPEC, f'F-02{i:02d}', f'F-02{i:02d}', R.LAUNCH, 'spec', '', 'card')
            for i in range(n)]


def context(rows):
    return invariants.FeederContext(
        rows=list(rows),
        occupancy={'busy': {}, 'corrections': {},
                   'waiting_landing': {'F-0111': {'branch': 'cloud/plan-F-0111'}}},
        docs_on_trunk={'F-0092': {'spec': False, 'plan': False},
                       'F-0097': {'spec': False, 'plan': False}})


def patched_context():
    return mock.patch.object(invariants, 'feeder_context',
                             lambda product, rows, items: context(rows))


class ViolatingRowsWait(unittest.TestCase):
    def test_a_violating_row_becomes_a_waits_row_with_the_invariants_reason(self):
        rows = violators() + valid(1)
        lines = []
        with patched_context():
            out = invariants.feeder_waits(None, rows, {}, out=lines.append)
        by = {r.item_id: r for r in out}
        self.assertEqual(by['T-0038'].action, 'WAITS ON F-0092 spec+plan on the trunk')
        self.assertEqual(by['T-0072'].action, 'WAITS ON F-0097 spec+plan on the trunk')
        self.assertEqual(by['F-0111'].action, 'WAITS ON landing: cloud/plan-F-0111')
        self.assertFalse(any(by[i].launches for i in ('T-0038', 'T-0039', 'T-0072', 'F-0111')))
        self.assertIn('I5', by['T-0038'].reason)
        self.assertTrue(by['F-0200'].launches)
        self.assertEqual([r.item_id for r in out], [r.item_id for r in rows], 'order kept')
        self.assertEqual(lines, [], 'a row the feeder waits is no INVARIANT line')

    def test_facts_that_fail_keep_every_row(self):
        rows = violators()
        with mock.patch.object(invariants, 'feeder_context', side_effect=OSError('gone')):
            self.assertEqual(invariants.feeder_waits(None, rows, {}, out=lambda _l: None), rows)

    def test_violating_rows_ranked_first_give_their_seats_to_the_next_valid_rows(self):
        cands = violators() + valid(3)
        with patched_context(), \
                mock.patch.object(R, 'candidates', lambda *a, **kw: list(cands)):
            gate = step_wave.invariant_gate(None)
            rows = R.plan_rows({}, None, [], 3, gate=gate)
        launching = [r.item_id for r in rows if r.launches]
        self.assertEqual(launching, ['F-0200', 'F-0201', 'F-0202'])
        waits = {r.item_id: r.action for r in rows if not r.launches}
        self.assertEqual(waits['T-0038'], 'WAITS ON F-0092 spec+plan on the trunk')
        self.assertEqual(waits['F-0111'], 'WAITS ON landing: cloud/plan-F-0111')


class TheWaveAndNextSeeTheSameRows(StepsTestCase):
    def setUp(self):
        super().setUp()
        self.launched = []

        def build(product, row, index, inflight, repo_facts=None):
            return _brief(row.brief_kind, row.item_id)

        def wave(product, rows, n, brief_fn=None, out=print, **_kw):
            self.launched += [r.job for r in rows]
            return [(r, {'model': 'opus'}) for r in rows], []
        for name, fn in (('_build', build), ('_wave', wave),
                         ('lane_pass', lambda ctx, out, **_kw: {})):
            p = mock.patch.object(step_wave, name, fn)
            p.start()
            self.addCleanup(p.stop)
        cands = violators() + valid(3)
        for p in (patched_context(),
                  mock.patch.object(R, 'candidates', lambda *a, **kw: list(cands))):
            p.start()
            self.addCleanup(p.stop)

    def resolved(self, sessions):
        return capacity.Resolved(sessions=sessions, sessions_bound='product', ci=None,
                                 ci_bound=None, ci_inflight=None, batch={}, reserve={},
                                 ceiling=sessions, fair_share=None, usable=sessions, active=1,
                                 borrowed=0)

    def test_the_wave_launches_the_valid_rows_and_its_gate_never_fires(self):
        with mock.patch.object(capacity, 'resolve', lambda *a, **kw: self.resolved(3)):
            step_wave.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.launched, ["spec-f-0200", "spec-f-0201", "spec-f-0202"])
        self.assertFalse([ln for ln in self.lines if ln.startswith('INVARIANT')], self.lines)

    def test_next_shows_a_violating_row_as_waiting_with_its_reason(self):
        buf = io.StringIO()
        args = argparse.Namespace(product='sample', capacity=3, inflight=None, all=False,
                                  json=False)
        with contextlib.redirect_stdout(buf):
            render.cmd_next(args, root=self.ctx().record_root())
        text = buf.getvalue()
        self.assertIn('3 would launch', text)
        self.assertIn('| FIX → CORRECT | T-0038 | F-0092 | WAITS ON F-0092 spec+plan on the trunk |',
                      text)
        self.assertIn('| STARVED → PLAN | F-0111 | F-0111 | WAITS ON landing: cloud/plan-F-0111 |',
                      text)
        self.assertNotIn('would launch correct', text)


if __name__ == '__main__':
    unittest.main()
