"""A correction written on a held run after its item went to adjudication — a product's F-0035,
2026-09-26: a decided Feature (state New, stage plan-draft) held on its spec branch twice
(``unpushed``, then ``died`` at round 3), an adjudicate session launched, and then an instruction
(``redact``: replace worker names with lane-N) was written onto the held run with
``update_session`` — a correction carrying no ``at``. ``asf next`` never showed it:

* the correction with no ``at`` read as answered at once: ``pending_correction`` takes every run
  of the item started at or after ``''`` — every run — as its answer, so the older ``died``
  correction stayed the item's pending one;
* and at round 3 the ``redact`` instruction (had it been read) would only have asked for a
  second adjudicate session over the ruling the first one was there to give.

The rule: a correction's time is never empty (the registry stamps it with the newest time it
had recorded when the line was written; ``update_session`` stamps it outright), and a correction
that is not health's own cap hold, written after an adjudicate session on the item started, is
that ruling's instruction — FIX → CORRECT, whatever the round. Health's next hold on it is at the
cap (``at_cap``) and goes to adjudication again: the usual round cap and loop guard.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf.feeder import rows as feeder_rows
from asf.workers import lifecycle as lc
from asf.workers import pool as pool_mod

REDACT = 'replace every worker account name with lane-N, then push'


def f0035_registry(adjudicate_ended=True):
    """The F-0035 registry, trimmed: two correct runs, the round-3 ``died`` hold, the adjudicate
    launch, and the ``redact`` line written with no ``at``."""
    adj = {'job': 'adjudicate-f-0035', 'item': 'F-0035', 'kind': 'adjudicate',
           'branch': 'cloud/spec-mobile-pass', 'pid': 1, 'started': '2026-09-26T22:41:10Z'}
    if adjudicate_ended:
        adj.update(ended='2026-09-26T22:50:00Z', end_reason='crashed')
    return [
        {'job': 'correct-f-0035', 'item': 'F-0035', 'kind': 'correct', 'pid': 1,
         'branch': 'cloud/spec-mobile-pass', 'started': '2026-09-26T22:16:55Z'},
        {'job': 'correct-f-0035', 'ended': '2026-09-26T22:27:14Z', 'end_reason': 'dead pid'},
        {'job': 'correct-f-0035-correction', 'item': 'F-0035', 'kind': 'correct', 'pid': 1,
         'branch': 'cloud/spec-mobile-pass', 'started': '2026-09-26T22:27:50Z'},
        {'job': 'correct-f-0035-correction', 'ended': '2026-09-26T22:38:35Z',
         'end_reason': 'failed: unpushed work'},
        {'job': 'correct-f-0035-correction', 'rounds': 2,
         'correction': {'kind': 'unpushed', 'text': 'push it', 'at': '2026-09-26T22:38:35Z'}},
        {'job': 'correct-f-0035', 'rounds': 3,
         'correction': {'kind': 'died', 'text': 'died twice', 'at': '2026-09-26T22:39:28Z'}},
        adj,
        {'job': 'correct-f-0035-correction', 'correction': {'kind': 'redact', 'text': REDACT}},
    ]


F0035 = {'F-0035': {'id': 'F-0035', 'type': 'feature', 'state': 'New', 'stage': 'plan-draft',
                    'decided': True, 'parent': 'E-0011'},
         'E-0011': {'id': 'E-0011', 'type': 'epic', 'state': 'Active', 'decided': True}}


class RuledCorrection(unittest.TestCase):
    def registry(self, lines):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        with open(path, 'w') as f:
            f.write(''.join(json.dumps(ln) + '\n' for ln in lines))
        return path

    def test_a_correction_with_no_time_is_as_new_as_the_registry_when_written(self):
        path = self.registry(f0035_registry())
        c = lc.corrections(path)['F-0035']
        self.assertEqual((c['kind'], c['text']), ('redact', REDACT))
        self.assertEqual(c['at'], '2026-09-26T22:50:00Z')
        self.assertEqual(c['branch'], 'cloud/spec-mobile-pass')

    def test_the_ruling_instruction_is_a_fix_correct_row_on_a_new_plan_draft_feature(self):
        path = self.registry(f0035_registry())
        corr = lc.corrections(path)
        self.assertTrue(corr['F-0035']['ruled'])
        got, ids = feeder_rows.correction_rows(F0035, None, set(), corr)
        self.assertEqual(ids, {'F-0035'})
        self.assertEqual([(r.kind, r.brief_kind, r.branch, r.correction) for r in got],
                         [(feeder_rows.FIX_CORRECT, 'correct', 'cloud/spec-mobile-pass', REDACT)])

    def test_while_the_adjudicate_session_runs_the_item_is_busy(self):
        path = self.registry(f0035_registry(adjudicate_ended=False))
        occ = lc.occupancy(path, alive=lambda pid: True)
        self.assertIn('F-0035', occ['busy'])
        got, _ = feeder_rows.correction_rows(F0035, None, set(occ['busy']), occ['corrections'])
        self.assertEqual(got, [])

    def test_health_s_own_cap_hold_after_the_ruling_still_adjudicates(self):
        lines = f0035_registry() + [
            {'job': 'correct-f-0035-r', 'item': 'F-0035', 'kind': 'correct', 'pid': 1,
             'branch': 'cloud/spec-mobile-pass', 'started': '2026-09-26T23:00:00Z',
             'ended': '2026-09-26T23:10:00Z', 'end_reason': 'failed: unpushed work'},
            {'job': 'correct-f-0035-r', 'correction': {
                'kind': 'unpushed', 'text': 'again', 'at': '2026-09-26T23:10:00Z',
                'at_cap': True}}]
        corr = lc.corrections(self.registry(lines))
        self.assertFalse(corr['F-0035']['ruled'])
        got, _ = feeder_rows.correction_rows(F0035, None, set(), corr)
        self.assertEqual([r.kind for r in got], [feeder_rows.STALEMATE])

    def test_a_correction_before_any_adjudicate_is_counted_on_its_own_finding(self):
        # operator policy 2026-09-27: round 3 of mixed holds (unpushed, died, redact) is not a
        # stalemate — only CORRECT failing the same finding twice is
        lines = [ln for ln in f0035_registry() if ln.get('kind') != 'adjudicate']
        corr = lc.corrections(self.registry(lines))
        self.assertFalse(corr['F-0035']['ruled'])
        self.assertEqual(corr['F-0035']['same'], 1)
        got, _ = feeder_rows.correction_rows(F0035, None, set(), corr)
        self.assertEqual([r.kind for r in got], [feeder_rows.FIX_CORRECT])

    def test_update_session_stamps_a_correction_that_carries_no_time(self):
        seen = []
        with mock.patch.object(pool_mod, 'append_session', lambda p, rec: seen.append(rec)):
            pool_mod.update_session('p', 'j', correction={'kind': 'redact', 'text': 'x'})
            pool_mod.update_session('p', 'j', correction=None)
            pool_mod.update_session('p', 'j', correction={'kind': 'k', 'text': 'y', 'at': 't'})
        self.assertRegex(seen[0]['correction']['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertIsNone(seen[1]['correction'])
        self.assertEqual(seen[2]['correction']['at'], 't')

    def test_harvest_does_not_regate_the_branch_while_the_ruling_waits_for_its_session(self):
        from asf.harvest import harvest as harvest_mod
        path = self.registry(f0035_registry())
        adj = lc.latest(path)['adjudicate-f-0035']
        self.assertFalse(harvest_mod.is_eligible(adj, path))
        lines = [ln for ln in f0035_registry() if 'redact' not in json.dumps(ln)]
        path = self.registry(lines)
        self.assertTrue(harvest_mod.is_eligible(lc.latest(path)['adjudicate-f-0035'], path))


if __name__ == '__main__':
    unittest.main()
