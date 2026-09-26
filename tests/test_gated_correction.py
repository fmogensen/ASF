"""A correction on a branch the lane holds in a landing wait — a product's T-0026, 2026-09-27:
PR #707 (``cloud/team-staffing-t3``) sat at GATE, red, and a ``redact`` correction was stamped
on its run (``update_session``) — exactly what would turn the gate green. ``asf next`` showed a
``WAITS ON landing: PR #707 GATE`` row with an empty correction and nothing launched:

* the lane only turns a pending correction into BACK from no state or BACK itself
  (:func:`asf.harvest.lane.next_state`), so the GATE record stood;
* occupancy put every item whose branch sits in a busy lane state in ``waiting_landing`` —
  the feeder's ``busy`` — and :func:`asf.feeder.rows.correction_rows` skips a busy item.

The rule: a pending correction written at or after the lane first saw the branch's current head
(``head_at``) turns a landing wait (PR_OPEN, REVIEW, GATE, WAITING_CI, WAITING) BACK, kind = the
correction's kind; the feeder's FIX → CORRECT row carries its text. A correction the head has
moved past since is answered, and health's own cap hold (``at_cap``) is left to the round cap.
"""
import json
import os
import shutil
import tempfile
import unittest

from asf.feeder import rows as feeder_rows
from asf.harvest import lane
from asf.workers import lifecycle as lc

HEAD = '9b2f650c796c950f3eaa05588f83eef96e7341f7'
BRANCH = 'cloud/team-staffing-t3'
REDACT = 'Content cleanup before publish: replace worker account names with lane-N'
ITEMS = {'T-0026': {'id': 'T-0026', 'type': 'task', 'state': 'Active', 'parent': 'F-0010'},
         'F-0010': {'id': 'F-0010', 'type': 'feature', 'state': 'Active', 'decided': True}}


def gate_rec(state=lane.GATE, **kw):
    r = {'state': state, 'head': HEAD, 'pr': 707, 'at': '2026-09-26T23:02:00Z',
         'reason': 'review: none', 'item': 'T-0026'}
    r.update(kw)
    return r


def facts(**kw):
    f = {'branch': BRANCH, 'item': 'T-0026', 'head': HEAD, 'ended': True, 'landed': False,
         'live': False, 'ahead': 1, 'mode': 'pr', 'host': True, 'now': 1_800_000_000.0,
         'stale_after': 2 * 86400, 'review_required': False,
         'pr': {'number': 707, 'state': 'OPEN', 'head': HEAD}}
    f.update(kw)
    return f


def corr(at='2026-09-26T23:04:01Z', **kw):
    return dict({'kind': 'redact', 'text': REDACT, 'at': at}, **kw)


class NextState(unittest.TestCase):
    def test_a_fresh_correction_turns_every_landing_wait_back(self):
        for s in (lane.PR_OPEN, lane.REVIEW, lane.GATE, lane.WAITING_CI, lane.WAITING):
            with self.subTest(state=s):
                self.assertEqual(lane.next_state(gate_rec(s), facts(correction=corr())),
                                 (lane.BACK, 'kind=redact'))

    def test_without_a_correction_the_gate_is_unchanged(self):
        self.assertEqual(lane.next_state(gate_rec(), facts()), (lane.GATE, 'review: none'))

    def test_a_correction_older_than_the_head_is_answered(self):
        r = gate_rec(head_at='2026-09-26T23:30:00Z')
        self.assertEqual(lane.next_state(r, facts(correction=corr()))[0], lane.GATE)

    def test_health_s_cap_hold_is_left_to_the_round_cap(self):
        self.assertEqual(lane.next_state(gate_rec(), facts(correction=corr(at_cap=True)))[0],
                         lane.GATE)

    def test_merging_and_queued_are_not_interrupted(self):
        for s in (lane.MERGING, lane.QUEUED):
            with self.subTest(state=s):
                f = facts(correction=corr(), harvest_running=True,
                          pr={'number': 707, 'state': 'OPEN', 'head': HEAD, 'queued': True})
                self.assertEqual(lane.next_state(gate_rec(s), f)[0], s)

    def test_head_at_holds_while_the_head_does_not_move(self):
        self.assertEqual(lane.head_since({'head': HEAD, 'head_at': 't0', 'at': 't1'}, HEAD, 't2'),
                         't0')
        self.assertEqual(lane.head_since({'head': HEAD, 'at': 't1'}, HEAD, 't2'), 't1')
        self.assertEqual(lane.head_since({'head': HEAD, 'head_at': 't0'}, 'c' * 40, 't2'), 't2')
        self.assertEqual(lane.head_since(None, HEAD, 't2'), 't2')


class Feeder(unittest.TestCase):
    def registry(self, correction=None, rounds=None, lane_rec=None):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        run = {'job': 'adopt-t-0026', 'item': 'T-0026', 'kind': 'coder', 'branch': BRANCH,
               'pid': None, 'started': '2026-09-25T19:13:32Z', 'ended': '2026-09-25T19:13:32Z',
               'end_reason': 'finished', 'adopted': True, 'lane': lane_rec or gate_rec()}
        lines = [run]
        if correction:
            upd = {'job': 'adopt-t-0026', 'correction': correction}
            if rounds is not None:
                upd['rounds'] = rounds
            lines.append(upd)
        with open(path, 'w') as f:
            f.write(''.join(json.dumps(ln) + '\n' for ln in lines))
        return path

    def rows(self, path):
        occ = lc.occupancy(path, alive=lambda pid: False)
        busy = set(occ['busy']) | set(occ['waiting_landing'])
        got, spoken = feeder_rows.correction_rows(ITEMS, None, busy, occ['corrections'])
        return got + feeder_rows.lane_rows(ITEMS, None, set(occ['busy']) | spoken, occ)

    def test_gated_red_item_with_a_fresh_correction_is_fix_correct_with_its_text(self):
        got = self.rows(self.registry(corr(), rounds=1))
        self.assertEqual([(r.kind, r.item_id, r.branch, r.correction) for r in got],
                         [(feeder_rows.FIX_CORRECT, 'T-0026', BRANCH, REDACT)])
        self.assertTrue(got[0].launches)

    def test_gated_item_without_a_correction_waits_on_landing(self):
        got = self.rows(self.registry())
        self.assertEqual([r.kind for r in got], [feeder_rows.PUSHED_LAND])
        self.assertIn('PR #707 GATE', got[0].action)

    def test_a_correction_answered_by_a_newer_head_gives_no_row(self):
        path = self.registry(corr(), rounds=1,
                             lane_rec=gate_rec(head_at='2026-09-26T23:30:00Z'))
        got = self.rows(path)
        self.assertEqual([r.kind for r in got], [feeder_rows.PUSHED_LAND])

    def test_the_cap_on_the_same_finding_still_adjudicates(self):
        occ = lc.occupancy(self.registry(corr(), rounds=lc.ROUND_CAP), alive=lambda pid: False)
        self.assertNotIn('T-0026', occ['waiting_landing'])
        c = dict(occ['corrections']['T-0026'], same=feeder_rows.CORRECTION_ROUNDS)
        got, _ = feeder_rows.correction_rows(ITEMS, None, set(), {'T-0026': c})
        self.assertEqual([r.kind for r in got], [feeder_rows.STALEMATE])


if __name__ == '__main__':
    unittest.main()
