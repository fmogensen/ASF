"""asf.harvest.mechanical — the causes code settles before a correction is written (W2-PR3a).

Scenarios on a fixture repo (:class:`tests.test_lane.LaneFixture`): under
``conventions.flags.mechanical: on`` a conflict git rebases clean is pushed by the lane, set
PUSHED with reason ``mechanical:conflict`` and the event on the run, no correction; a textual
conflict is a correction carrying what the lane tried; a footprint red is never pushed on — the
hold names the widening rule's verdict; with the flag off every row is today's.
"""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from asf import env
from asf.feeder import widen
from asf.harvest import harvest, lane, mechanical
from asf.workers import lifecycle
from tests import test_lane as TL

sh = TL.sh

ON = {'mechanical': 'on'}


class Flag(unittest.TestCase):

    def product(self, flags=None):
        conv = {'test_command': 'true'}
        if flags is not None:
            conv['flags'] = flags
        return env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main',
                                      'conventions': conv})

    def test_default_is_off(self):
        self.assertFalse(mechanical.enabled(self.product()))
        self.assertFalse(mechanical.enabled(None))

    def test_on_words(self):
        for v in ('on', 'true', True, 'yes', 'ON'):
            self.assertTrue(mechanical.enabled(self.product({'mechanical': v})), v)
        for v in ('off', False, 'no', ''):
            self.assertFalse(mechanical.enabled(self.product({'mechanical': v})), v)

    def test_the_flag_is_known(self):
        from asf import conventions
        self.assertIn(mechanical.FLAG, conventions.KNOWN_FLAGS)

    def test_handles_only_table_kinds(self):
        p = self.product(ON)
        self.assertTrue(mechanical.handles(p, 'conflict'))
        self.assertTrue(mechanical.handles(p, lifecycle.REBASE_CONFLICT))
        self.assertTrue(mechanical.handles(p, lifecycle.FOOTPRINT))
        self.assertFalse(mechanical.handles(p, 'review'))
        self.assertFalse(mechanical.handles(self.product(), 'conflict'))

    def test_a_dry_run_applies_nothing(self):
        ln = SimpleNamespace(product=self.product(ON), dry_run=True)
        self.assertIsNone(mechanical.apply(ln, {'branch': 'b'}, {'kind': 'conflict'}))


class Rebase(TL.LaneFixture):
    """conflict → rebase, through :func:`asf.harvest.lane.send_back` (the fixture's helpers are
    :class:`tests.test_lane.GitMechanicsNeverSpawnASession`'s, borrowed — not its tests)."""

    _G = TL.GitMechanicsNeverSpawnASession
    B, AUTHOR = _G.B, _G.AUTHOR
    tip, commit, own, sessions = _G.tip, _G.commit, _G.own, _G.sessions
    branch, archive, facts = _G.branch, _G.archive, _G.facts

    def setUp(self):
        super().setUp()
        self.lines = []

    def run_of(self, job='coder-t-0001'):
        return harvest.read_sessions(self.state_dir)[job]

    def test_a_clean_conflict_is_rebased_pushed_and_logged(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        trunk = self.origin_main()
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        f = self.facts(old)
        got = lane.send_back(ln, f, 'conflict', 'PR #7 merge refused — not mergeable', [])
        self.assertEqual(got, 'mechanical')
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~1'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(self.archive('rebase', old), old)
        rec = self.lane_of(self.B)
        self.assertEqual((rec['state'], rec['reason']), (lane.PUSHED, 'mechanical:conflict'))
        self.assertEqual((rec['event'], rec['mechanical'], rec['head']),
                         ('mechanical', 'conflict', new))
        ev = self.run_of()['mechanical']
        self.assertEqual((ev['event'], ev['kind'], ev['head'], ev['resolved']),
                         ('mechanical', 'conflict', new, True))
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertEqual(ln.results[self.B], 'mechanical')
        self.assertTrue(any(l.startswith(f'mechanical:conflict {self.B}: resolved')
                            for l in self.lines), self.lines)
        self.assertEqual(self.origin_main(), trunk)

    def test_a_pending_conflict_correction_is_cleared(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        f = dict(self.facts(old), correction={'kind': 'conflict', 'text': 'x'})
        self.assertEqual(lane.send_back(ln, f, 'conflict', 'x', []), 'mechanical')
        self.assertIsNone(f['correction'])
        self.assertIsNone(self.run_of().get('correction'))

    def test_a_textual_conflict_is_a_correction_carrying_why(self):
        self.push_main({'c.txt': 'base\n'}, 'chore: c')
        old = self.branch({'c.txt': 'branch\n'})
        self.push_main({'c.txt': 'trunk\n'}, 'fix: c on the trunk (#813)')
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'PR #7 merge refused', [])
        self.assertEqual(got, 'held')
        self.assertEqual(self.tip(), old)
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertEqual(corr['kind'], 'conflict')
        self.assertIn('git stops at', corr['text'])
        self.assertIn('conflicts in c.txt', corr['text'])
        self.assertEqual(self.lane_of(self.B)['state'], lane.BACK)
        ev = self.run_of()['mechanical']
        self.assertEqual((ev['kind'], ev['resolved'], ev['head']), ('conflict', False, old))
        self.assertTrue(any(l.startswith(f'mechanical:conflict {self.B}: residue')
                            for l in self.lines), self.lines)

    def test_a_red_pre_push_check_is_residue_and_pushes_nothing(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(flags=ON, pre_push_check='echo lint finding; exit 3'),
                       self.state_dir, out=self.lines.append)
        self.assertEqual(lane.send_back(ln, self.facts(old), 'conflict', 'x', []), 'held')
        self.assertEqual(self.tip(), old)
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertIn('pre-push check fails', corr['text'])
        self.assertIn('lint finding', corr['text'])

    def test_a_conflict_with_a_batch_ahead_is_never_rebased(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'x', ['a.txt'], rebase=False)
        self.assertEqual(got, 'held')
        self.assertEqual(self.tip(), old)
        self.assertNotIn('mechanical', self.run_of())

    def test_flag_off_keeps_todays_rows(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'PR #7 merge refused', [])
        self.assertEqual(got, 'rebased')
        rec = self.lane_of(self.B)
        self.assertEqual((rec['state'], rec['reason']),
                         (lane.PUSHED, 'rebased onto main by the lane (no session)'))
        self.assertNotIn('event', rec)
        self.assertNotIn('mechanical', self.run_of())
        self.assertFalse(any(l.startswith('mechanical:') for l in self.lines), self.lines)


class Footprint(unittest.TestCase):
    """footprint → the widening rule's verdict: a red head is never pushed on."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='mech_')
        self.addCleanup(shutil.rmtree, self.state, True)
        self.record = {'job': 'coder-t-0080', 'item': 'T-0080', 'branch': 'worker/T-0080'}
        self.lines = []
        with open(harvest.sessions_path(self.state), 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(dict(self.record, kind='coder', started='2026-10-01T00:00:00Z',
                                     ended='2026-10-01T01:00:00Z', end_reason='finished'))
                     + '\n')
        self.items = {
            'F-0001': {'type': 'feature'},
            'T-0080': {'type': 'task', 'parent': 'F-0001', 'writes': ['src/a.py']},
        }

    def lane(self, flags=None, max_files=None):
        conv = {'test_command': 'true'}
        if flags is not None:
            conv['flags'] = flags
        if max_files is not None:
            conv['widen_max_files'] = max_files
        product = env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main',
                                         'conventions': conv})
        return SimpleNamespace(product=product, items=self.items, dry_run=False,
                               state_dir=self.state, path=harvest.sessions_path(self.state),
                               out=self.lines.append, results={}, trunk='main')

    def hold(self, ln):
        f = {'branch': 'worker/T-0080', 'item': 'T-0080', 'head': 'a' * 40, 'run': self.record}
        return lane.hold_with_correction(
            self.state, 'worker/T-0080', self.record, 'gate', 'FAIL: test_other', self.lines.append,
            ['tests/test_other.py'], ['src/a.py'], ['src/a.py'], own=True,
            read=lambda p: {'tests/test_other.py': 'from src.a import f\n'}.get(p), lane=ln, f=f)

    def run_of(self):
        return harvest.read_sessions(self.state)['coder-t-0080']

    def test_a_footprint_red_holds_naming_the_verdict(self):
        self.assertEqual(self.hold(self.lane(ON)), 'held')
        run = self.run_of()
        corr = run['correction']
        self.assertEqual((corr['kind'], corr['needs']), ('footprint', ['tests/test_other.py']))
        self.assertIn('widening rule says widen +tests/test_other.py', corr['text'])
        ev = run['mechanical']
        self.assertEqual((ev['kind'], ev['resolved']), ('footprint', False))
        self.assertNotIn('lane', run)  # never set PUSHED: the same head would gate red again
        self.assertTrue(any(l.startswith('mechanical:footprint worker/T-0080: residue')
                            for l in self.lines), self.lines)

    def test_over_the_cap_the_verdict_is_reshape(self):
        self.assertEqual(self.hold(self.lane(ON, max_files=0)), 'held')
        self.assertIn('widening rule says reshape', self.run_of()['correction']['text'])

    def test_flag_off_keeps_todays_footprint_hold(self):
        self.assertEqual(self.hold(self.lane()), 'held')
        run = self.run_of()
        self.assertEqual(run['correction']['kind'], 'footprint')
        self.assertNotIn('widening rule', run['correction']['text'])
        self.assertNotIn('mechanical', run)
        self.assertFalse(any(l.startswith('mechanical:') for l in self.lines), self.lines)

    def test_without_a_lane_the_hold_is_todays(self):
        lane.hold_with_correction(
            self.state, 'worker/T-0080', self.record, 'gate', 'FAIL: test_other', self.lines.append,
            ['tests/test_other.py'], ['src/a.py'], ['src/a.py'], own=True,
            read=lambda p: {'tests/test_other.py': 'from src.a import f\n'}.get(p))
        self.assertNotIn('mechanical', self.run_of())


class Widen(unittest.TestCase):
    """:func:`asf.feeder.widen.widen` — the one composition of the rule's facts."""

    ITEMS = {
        'F-1': {'type': 'feature'},
        'T-1': {'type': 'task', 'parent': 'F-1', 'writes': ['src/a.py']},
        'T-2': {'type': 'task', 'parent': 'F-1', 'writes': ['src/b.py']},
        'T-9': {'type': 'task', 'state': 'Resolved', 'writes': ['lib/done.py']},
    }

    def test_within_the_cap(self):
        v = widen.widen(self.ITEMS, 'T-1', ['tests/test_x.py'], limit=2)
        self.assertEqual((v.kind, v.paths), (widen.WIDEN, ('tests/test_x.py',)))

    def test_over_the_cap_is_reshape(self):
        v = widen.widen(self.ITEMS, 'T-1', ['x.py', 'y.py'], limit=1)
        self.assertEqual(v.kind, widen.RESHAPE)

    def test_inside_the_feature_passes_the_cap(self):
        v = widen.widen(self.ITEMS, 'T-1', ['src/b.py'], limit=0)
        self.assertEqual(v.kind, widen.WIDEN)

    def test_a_delivered_path_does_not_count(self):
        v = widen.widen(self.ITEMS, 'T-1', ['lib/done.py'], limit=0)
        self.assertEqual(v.kind, widen.WIDEN)

    def test_a_live_owner_makes_it_wait(self):
        v = widen.widen(self.ITEMS, 'T-1', ['src/b.py'], limit=5,
                        running=[('T-2', ['src/b.py'])])
        self.assertEqual((v.kind, v.detail), (widen.WAITS, 'T-2'))

    def test_a_second_widening_is_reshape(self):
        v = widen.widen(self.ITEMS, 'T-1', ['x.py'], limit=5, widened_before=1)
        self.assertEqual(v.kind, widen.RESHAPE)


if __name__ == '__main__':
    unittest.main()
