"""parent-closed and children-closed never prove each other.

The incident's shape: a Feature and its two Tasks, none with evidence of its own, all Closed —
the Tasks by ``parent-closed`` (descent), the Feature by ``children-closed`` over those very
Tasks. The terminal hold kept each one up: a Task re-derived alone still met its Closed parent,
the Feature re-derived alone still counted its held-Closed Tasks, and ``asf reopen`` refused all
three with "current evidence still says Closed". An unrelated landing commit (another Feature's
Task's merge) sits in the evidence and must vouch for none of them.

A close that descent gave (the card's own ``rule: parent-closed``, or a landing stamped
``by: descent``) is borrowed: it is never held by the terminal hold and never counts toward its
parent's ``children-closed``. While the parent stays Closed on evidence of its own, descent
gives the close back every pass; once it does not, the borrowed close is gone."""
import unittest

from asf.evidence import closing
from asf.record import ingest
from asf.record.core import canonicalize, load_items
from tests.test_reopen import EMPTY_EV, ReopenTestCase, meta, write

UNRELATED = 'f' * 40          #: another Feature's Task landed here
OWN = 'a' * 40                #: the Feature's own landing, in the legitimate cases
FEV = {'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md', 'spec_branch': None,
       'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
       'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': []}
CLOSED_AT = ('schema_version: 1', 'state: Closed', 'stage_since: 2026-01-01T00:00:00Z')


def machine(*evidence_lines, landing=None):
    lines = list(CLOSED_AT) + ['evidence:'] + [f'  - "{l}"' for l in evidence_lines]
    if landing:
        lines += ['landing: {sha: "%s", as_of: 2026-01-01T00:00:00Z, by: %s}' % landing]
    return tuple(lines + ['updated: 2026-01-01T00:00:00Z'])


def ids(iid, sha, green=True):
    return {iid: {'branches': [], 'open_prs': [], 'commit': sha, 'pr': None, 'green': green}}


class CircularCloseTests(ReopenTestCase):
    def trio(self):
        write(self.root, 'F-0001', 'feature', parent=None,
              machine=machine('2/2 tasks Closed', 'rule: children-closed'))
        for t in ('T-0001', 'T-0002'):
            write(self.root, t, 'task', parent='F-0001',
                  machine=machine(f'F-0001 Closed (commit {UNRELATED[:7]})', 'rule: parent-closed'))
        write(self.root, 'F-0002', 'feature')
        write(self.root, 'T-0009', 'task', parent='F-0002')

    def ev(self):
        return dict(EMPTY_EV, ci=True, ids=ids('T-0009', UNRELATED))

    def derive(self, bypass=()):
        canonical = canonicalize(load_items(self.root)[0])[0]
        return ingest.derive(canonical, self.ev(), None, bypass_sticky=bypass)

    def test_the_feature_reopens_its_tasks_counted_closed_only_through_it(self):
        self.trio()
        self.assertEqual(self.reopen('F-0001', ev=self.ev()), 0)
        self.assertNotIn(meta(self.root, 'features/F-0001.md')['state'], ('Resolved', 'Closed'))

    def test_the_cycle_has_no_proof_the_feature_re_derives_open(self):
        self.trio()
        _ns, closings, derived, *_ = self.derive(bypass={'F-0001'})
        self.assertNotEqual(closings['F-0001'].state, closing.CLOSED)
        self.assertNotEqual(closings['F-0001'].rule, 'children-closed')

    def test_a_task_alone_is_still_refused_while_its_parent_stays_closed(self):
        # its own close is descent's: the Feature, held Closed, still gives it — reopen the Feature
        self.trio()
        self.assertEqual(self.reopen('T-0001', ev=self.ev()), 2)

    def test_after_the_feature_reopens_the_tasks_lose_the_borrowed_close_on_ingest(self):
        self.trio()
        self.assertEqual(self.reopen('F-0001', ev=self.ev()), 0)
        new_state, closings, *_ = self.derive()
        self.assertEqual(new_state['T-0001'], closing.NEW)
        self.assertEqual(new_state['T-0002'], closing.NEW)
        self.assertEqual(new_state['T-0009'], closing.CLOSED)   # the real landing still stands

    def test_a_descent_stamp_marks_the_close_borrowed_too(self):
        write(self.root, 'F-0001', 'feature', machine=machine('rule: children-closed'))
        write(self.root, 'T-0001', 'task', parent='F-0001',
              machine=machine('rule: closed-terminal', landing=(UNRELATED, 'descent')))
        self.assertEqual(self.reopen('F-0001', ev=dict(EMPTY_EV, ci=True)), 0)

    def test_an_unrelated_commit_is_never_named_as_a_descended_tasks_landing(self):
        # the Feature closes on its Tasks' landings; a removed Task descended onto names no
        # sibling's merge as its own
        write(self.root, 'F-0001', 'feature')
        write(self.root, 'T-0001', 'task', parent='F-0001')
        write(self.root, 'T-0002', 'task', parent='F-0001',
              typed=('removed: merged into T-0001 (groom 2026-01-02)',))
        canonical = canonicalize(load_items(self.root)[0])[0]
        ev = dict(EMPTY_EV, ci=True, features={'f-0001': FEV}, ids=ids('T-0001', UNRELATED))
        _ns, closings, derived, _sv, task_ev, _evs = ingest.derive(canonical, ev, None)
        self.assertEqual(closings['F-0001'].state, closing.CLOSED)
        self.assertEqual(closings['T-0002'].rule, 'parent-closed')
        self.assertFalse(any(UNRELATED[:7] in l for l in closings['T-0002'].lines))
        sha, by = ingest.landing_of('T-0002', 'parent-closed', canonical, ev, task_ev, derived)
        self.assertEqual((sha, by), ('', 'descent'))


class LegitimateClosesTests(ReopenTestCase):
    def test_a_child_of_a_feature_closed_on_its_own_landing_still_closes(self):
        write(self.root, 'F-0001', 'feature', machine=machine('rule: landed-green'))
        rel = write(self.root, 'S-0001', 'story', parent='F-0001')
        canonical = canonicalize(load_items(self.root)[0])[0]
        ev = dict(EMPTY_EV, ci=True, ids=ids('F-0001', OWN))
        _ns, closings, derived, _sv, task_ev, _evs = ingest.derive(canonical, ev, None)
        self.assertEqual((closings['S-0001'].state, closings['S-0001'].rule),
                         (closing.CLOSED, 'parent-closed'))
        self.assertIn(f'F-0001 Closed (commit {OWN[:7]})', closings['S-0001'].lines)
        self.assertEqual(ingest.landing_of('S-0001', 'parent-closed', canonical, ev, task_ev,
                                           derived), (OWN, 'descent'))
        self.assertTrue(rel)

    def test_a_held_descended_child_stays_closed_while_its_parent_has_its_own_evidence(self):
        write(self.root, 'F-0001', 'feature', machine=machine('rule: landed-green'))
        write(self.root, 'S-0001', 'story', parent='F-0001',
              machine=machine(f'F-0001 Closed (commit {OWN[:7]})', 'rule: parent-closed'))
        canonical = canonicalize(load_items(self.root)[0])[0]
        new_state, closings, *_ = ingest.derive(canonical, dict(EMPTY_EV, ci=True,
                                                                ids=ids('F-0001', OWN)), None)
        self.assertEqual((new_state['S-0001'], closings['S-0001'].rule),
                         (closing.CLOSED, 'parent-closed'))

    def test_a_feature_whose_tasks_landed_closes_children_closed(self):
        write(self.root, 'F-0001', 'feature')
        write(self.root, 'T-0001', 'task', parent='F-0001')
        write(self.root, 'T-0002', 'task', parent='F-0001')
        canonical = canonicalize(load_items(self.root)[0])[0]
        ev = dict(EMPTY_EV, ci=True, ids={**ids('T-0001', OWN), **ids('T-0002', 'b' * 40)})
        new_state, closings, *_ = ingest.derive(canonical, ev, None)
        self.assertEqual((new_state['F-0001'], closings['F-0001'].rule),
                         (closing.CLOSED, 'children-closed'))

    def test_a_feature_closed_on_its_tasks_landings_is_still_refused_a_reopen(self):
        write(self.root, 'F-0001', 'feature', machine=machine('rule: children-closed'))
        write(self.root, 'T-0001', 'task', parent='F-0001', machine=machine('rule: landed-green'))
        ev = dict(EMPTY_EV, ci=True, ids=ids('T-0001', OWN))
        self.assertEqual(self.reopen('F-0001', ev=ev), 2)
        self.assertEqual(meta(self.root, 'features/F-0001.md')['state'], 'Closed')


if __name__ == '__main__':
    unittest.main()
