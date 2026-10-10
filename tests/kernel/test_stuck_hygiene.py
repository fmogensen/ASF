"""A Stuck leaves the card when the item stops being work: a retired card (``removed:``) or a
parked one (``priority: later``) drops ``kernel_state: stuck`` and every ``kernel_stuck_*``
field on the next tick, so neither Stuck nor "Needs you" shows it and an unretire or unpark
judges it afresh instead of resurrecting the old Stuck. And when GitHub's rollup carries several
runs of one check, only the newest decides red or green."""
import os
import tempfile
import unittest

from asf import env
from asf.kernel import model as M
from asf.kernel import ports as P
from asf.kernel.apply import apply
from asf.kernel.decide import decide

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
STUCK_KEYS = (P.STUCK_REASON, P.STUCK_OWNER, P.STUCK_NEXT, P.STUCK_SINCE)

RETIRED = """---
id: B-0001
type: bug
title: noise
removed: "retired: noise"
# ---- machine ----
schema_version: 1
state: New
kernel_state: stuck
kernel_stuck_reason: "conflict the rebase session could not resolve: PR #9"
kernel_stuck_owner: operator
kernel_stuck_next: "rebase PR #9 by hand"
kernel_stuck_since: 2026-10-10T01:00:00Z
---
## Description
"""


def _tick(items):
    ports = F.ports(record=F.FakeRecord(items))
    facts = B.facts(ports.record.items().values(), now='2026-10-10T12:00:00Z')
    plan = decide(facts, B.config())
    apply(plan, facts, ports, log=lambda _t: None)
    return plan, ports.record


def _stuck(**kw):
    return B.task('T-0001', state=State.STUCK,
                  stuck=M.Stuck('review: changes requested after 2 fix rounds', 'operator', 'x'),
                  stuck_since='2026-10-10T00:00:00Z', **kw)


class Hygiene(unittest.TestCase):

    def test_a_parked_stuck_item_drops_its_stuck_fields(self):
        plan, record = _tick([_stuck(priority='later')])
        self.assertIs(B.state(plan, 'T-0001'), State.PARKED)
        cleared, = [f for iid, f in record.writes if iid == 'T-0001']
        for k in (P.STATE,) + STUCK_KEYS:
            self.assertIn(k, cleared)
            self.assertIsNone(cleared[k])

    def test_a_parked_item_that_was_not_stuck_is_not_written(self):
        _plan, record = _tick([B.task('T-0001', state=State.READY, priority='later')])
        self.assertEqual([w for w in record.writes if w[0] == 'T-0001'], [])

    def test_a_retired_card_with_a_stale_stuck_reads_as_done_and_is_cleared(self):
        it = B.task('B-0001', type='bug', state=State.DONE, priority='later', stale_stuck=True)
        plan, record = _tick([it])
        self.assertIs(B.state(plan, 'B-0001'), State.DONE)
        self.assertNotIn('B-0001', [s for s in plan.states if plan.states[s][1]])
        w, = [f for iid, f in record.writes if iid == 'B-0001']
        self.assertEqual(w[P.STATE], 'done')
        for k in STUCK_KEYS:
            self.assertIsNone(w[k])


class RetiredCard(unittest.TestCase):

    def test_the_record_marks_a_retired_cards_stuck_as_stale_and_a_write_clears_it(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, 'bugs'))
        path = os.path.join(root, 'bugs', 'B-0001.md')
        with open(path, 'w') as f:
            f.write(RETIRED)
        record = P.RealRecord(env.Product('sample', {'backlog_dir': root}),
                              state_dir=tempfile.mkdtemp())
        it = record.items()['B-0001']
        self.assertEqual((it.state, it.stuck, it.stale_stuck), (State.DONE, None, True))
        ports = F.ports(record=record)
        facts = B.facts([it], now='2026-10-10T12:00:00Z')
        apply(decide(facts, B.config()), facts, ports, log=lambda _t: None)
        with open(path) as f:
            text = f.read()
        self.assertIn('kernel_state: done', text)
        self.assertNotIn('kernel_stuck', text)
        again = P.RealRecord(env.Product('sample', {'backlog_dir': root}),
                             state_dir=tempfile.mkdtemp()).items()['B-0001']
        self.assertFalse(again.stale_stuck)


class NewestRunDecides(unittest.TestCase):

    def roll(self, *runs):
        return P.newest_checks([
            {'__typename': 'CheckRun', 'name': n, 'status': 'COMPLETED', 'conclusion': c,
             'detailsUrl': 'https://x/actions/runs/%d/job/1' % rid,
             'startedAt': at, 'completedAt': at} for n, c, rid, at in runs])

    def test_an_older_red_run_never_hides_a_newer_green_one(self):
        c, = self.roll(('tests', 'FAILURE', 10, '2026-10-10T01:00:00Z'),
                       ('tests', 'SUCCESS', 11, '2026-10-10T02:00:00Z'))
        self.assertEqual((c.conclusion, c.run_id), ('success', 11))

    def test_an_older_skipped_run_never_hides_a_newer_red_one(self):
        c, = self.roll(('tests', 'FAILURE', 12, '2026-10-10T02:00:00Z'),
                       ('tests', 'SKIPPED', 11, '2026-10-10T03:00:00Z'))
        self.assertEqual((c.conclusion, c.run_id), ('failure', 12))

    def test_same_run_the_later_completion_wins_and_names_stay_apart(self):
        got = self.roll(('tests', 'FAILURE', 5, '2026-10-10T01:00:00Z'),
                        ('tests', 'SUCCESS', 5, '2026-10-10T02:00:00Z'),
                        ('lint', 'FAILURE', 3, '2026-10-10T00:00:00Z'))
        self.assertEqual(sorted((c.name, c.conclusion) for c in got),
                         [('lint', 'failure'), ('tests', 'success')])

    def test_a_newer_run_still_in_progress_decides_over_an_older_red(self):
        got = P.newest_checks([
            {'__typename': 'CheckRun', 'name': 'tests', 'status': 'COMPLETED',
             'conclusion': 'FAILURE', 'detailsUrl': 'https://x/actions/runs/7/job/1',
             'startedAt': '2026-10-10T01:00:00Z', 'completedAt': '2026-10-10T01:10:00Z'},
            {'__typename': 'CheckRun', 'name': 'tests', 'status': 'IN_PROGRESS',
             'conclusion': None, 'detailsUrl': 'https://x/actions/runs/8/job/1',
             'startedAt': '2026-10-10T02:00:00Z', 'completedAt': None}])
        c, = got
        self.assertEqual((c.status, c.run_id), ('in_progress', 8))


if __name__ == '__main__':
    unittest.main()
