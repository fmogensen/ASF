"""F-0113: the S1 gate says its own name, and one plan feeds every view — the S1 lane's own
invariant, held to a test: an S1 nobody and nothing is working cuts no tier-2 row."""
import datetime as dt
import unittest

from asf.env import Product
from asf.feeder import rows, tiers


def product(**extra):
    conv = {'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}
    conv.update(extra.pop('conventions', {}))
    return Product('sample', dict({'conventions': conv}, **extra))


def since(days):
    """``days`` ago, an hour clear of the day boundary. The gate's sentence carries an age, and
    an age is measured against now: a pinned ``stage_since`` reads one day older every day the
    suite runs and turns the gate red on a date nobody chose."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days, hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')


def ten_tier_2_rows_behind(bug):
    items = {'B-0057': bug}
    for n in range(1, 11):
        fid = f'F-{n:04d}'
        items[fid] = {'id': fid, 'type': 'feature', 'title': f'Feature {n}', 'decided': True,
                      'rank': 11 - n, 'stage': 'card', 'state': 'New'}
    return {'items': items}


class AnUnworkedS1HoldsNothing(unittest.TestCase):
    """P4: an S1 that is not being worked — blocked, Active, past the attempt limit, undecided,
    or parked — already holds nothing: none of these rows launches, so none of them takes the
    S1 lane's first seat, and the cut behind it never fires. Asserted directly against
    :func:`asf.feeder.rows.plan_rows`, at a capacity with no free seat, so the day someone widens
    ``s1_rows`` past ``r.launches`` this suite says so."""

    def _plans(self, bug, **kw):
        idx = ten_tier_2_rows_behind(bug)
        cut = rows.plan_rows(idx, product(), [], 0, **kw)
        uncut = rows.plan_rows(idx, product(), [], 0, s1_first=False, **kw)
        return cut, uncut

    def _assert_holds_nothing(self, cut, uncut):
        launching = {(r.item_id, r.kind) for r in uncut if r.launches}
        self.assertTrue(launching <= {(r.item_id, r.kind) for r in cut if r.launches})
        g = tiers.gate(cut, uncut)
        self.assertEqual(g.held, 0)
        return g

    def test_a_blocked_s1_holds_nothing(self):
        bug = {'id': 'B-0057', 'type': 'bug', 'severity': 'S1', 'decided': True, 'state': 'New',
              'blocked': True, 'blocked_by_open': ['B-0041']}
        cut, uncut = self._plans(bug)
        g = self._assert_holds_nothing(cut, uncut)
        self.assertEqual([e[0] for e in g.unworked], ['B-0057'])
        self.assertEqual(tiers.needs_operator(g, product()),
                         ['NEEDS OPERATOR: B-0057 is S1 and nothing is working it — blocked by '
                          'B-0041 — the S1 lane holds 0 of 0 tier-2 rows behind it — asf set '
                          'B-0057 blockedBy= --product sample'])

    def test_an_active_s1_holds_nothing(self):
        # its fixer branch/PR is the work (D5, WORKED): a CONFLICT/STALE row speaks for it, so
        # this Bug's own WAITS ON branch row rides in neither holders nor unworked — no gate at
        # all, which is the strongest form of "holds nothing"
        bug = {'id': 'B-0057', 'type': 'bug', 'severity': 'S1', 'decided': True, 'state': 'Active'}
        cut, uncut = self._plans(bug)
        launching = {(r.item_id, r.kind) for r in uncut if r.launches}
        self.assertTrue(launching <= {(r.item_id, r.kind) for r in cut if r.launches})
        self.assertIsNone(tiers.gate(cut, uncut))

    def test_an_s1_past_the_attempt_limit_holds_nothing(self):
        bug = {'id': 'B-0057', 'type': 'bug', 'severity': 'S1', 'decided': True, 'state': 'New'}
        cut, uncut = self._plans(bug, attempts={'B-0057': 4})
        g = self._assert_holds_nothing(cut, uncut)
        self.assertEqual([e[0] for e in g.unworked], ['B-0057'])
        self.assertEqual(tiers.needs_operator(g, product()),
                         ['NEEDS OPERATOR: B-0057 is S1 and nothing is working it — adjudicated '
                          'after 4 sessions — the S1 lane holds 0 of 0 tier-2 rows behind it — '
                          'asf set B-0057 severity=S2 --product sample'])

    def test_an_undecided_s1_holds_nothing(self):
        bug = {'id': 'B-0057', 'type': 'bug', 'severity': 'S1', 'decided': False, 'state': 'New',
              'stage_since': since(270)}
        cut, uncut = self._plans(bug)
        g = self._assert_holds_nothing(cut, uncut)
        self.assertEqual([e[0] for e in g.unworked], ['B-0057'])
        self.assertEqual(tiers.needs_operator(g, product()),
                         ['NEEDS OPERATOR: B-0057 is S1 and nothing is working it — undecided '
                          '270d — the S1 lane holds 0 of 0 tier-2 rows behind it — asf set '
                          'B-0057 decided=true --product sample'])

    def test_a_parked_s1_holds_nothing(self):
        bug = {'id': 'B-0057', 'type': 'bug', 'severity': 'S1', 'decided': True, 'state': 'New'}
        cut, uncut = self._plans(bug, held=['B-0057'])
        g = tiers.gate(cut, uncut, held=['B-0057'])
        self.assertEqual(g.held, 0)
        self.assertEqual([e[0] for e in g.unworked], ['B-0057'])
