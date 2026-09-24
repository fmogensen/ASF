"""tests.test_closing — the definition of done is total. No git, no clock."""
import dataclasses
import itertools
import unittest

from asf.evidence import closing as c
from asf.evidence.closing import Ev, Closing, state_of, sticky

TYPES = ('task', 'story', 'bug', 'feature', 'epic')
STATES = (c.NEW, c.ACTIVE, c.RESOLVED, c.CLOSED)
SAMPLE = {
    'commit': 'abc1234', 'green': True, 'merged_sha': 'def5678', 'branch': 'worker/x',
    'pr_state': 'open', 'open_prs': (1,), 'children': (c.ACTIVE,), 'child_evidence': True,
    'spec_on_main': True, 'plan_approved': True, 'matrix_status': 'done', 'in_prod': True,
    'signature': 'sig', 'quiet_for': 5.0, 'quiet_limit': 1.0, 'landed': 'abc1234',
    'parent_closed': True, 'typed_closed': True,
}


# fields that only mean something together are switched together, so the sweep stays a few
# thousand Evs per type: presence/absence of every group, crossed
GROUPS = (
    ('commit',), ('green',), ('merged_sha',), ('branch', 'pr_state', 'open_prs'),
    ('children',), ('child_evidence',), ('spec_on_main',), ('plan_approved',),
    ('matrix_status',), ('in_prod',), ('signature', 'quiet_for', 'quiet_limit'),
    ('landed',), ('parent_closed',), ('typed_closed',),
)


def every_ev():
    for mask in itertools.product((False, True), repeat=len(GROUPS)):
        yield Ev(**{n: SAMPLE[n] for g, on in zip(GROUPS, mask) if on for n in g})


def rule_names(type_):
    return {r[1] for r in c.RULES if r[0] in ('any', type_)}


class TotalityTests(unittest.TestCase):
    def test_every_pair_yields_a_state_and_a_known_rule(self):
        seen = 0
        for type_ in TYPES:
            for ev in every_ev():
                got = state_of(type_, ev, c.NEW)
                self.assertIsInstance(got, Closing)
                self.assertIn(got.state, STATES)
                self.assertIn(got.rule, rule_names(type_) | {c.NO_RULE})
                seen += 1
        self.assertEqual(seen, len(TYPES) * 2 ** len(GROUPS))

    def test_no_rule_carries_the_current_state(self):
        got = state_of('story', Ev(), c.ACTIVE)
        self.assertEqual((got.state, got.rule), (c.ACTIVE, c.NO_RULE))

    def test_hole_story_with_no_task_and_a_closed_parent(self):
        got = state_of('story', Ev(parent_closed=True), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'parent-closed'))

    def test_hole_feature_with_no_children_and_no_naming_commit(self):
        got = state_of('feature', Ev(parent_closed=True), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'parent-closed'))
        got = state_of('feature', Ev(landed='abc1234', green=True), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'reconciled'))

    def test_hole_feature_landed_green(self):
        got = state_of('feature', Ev(commit='abc', green=True, in_prod=True), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'landed-green'))
        got = state_of('feature', Ev(commit='abc'), c.NEW)
        self.assertEqual((got.state, got.rule), (c.RESOLVED, 'landed'))

    def test_hole_bug_past_quiet(self):
        ev = Ev(merged_sha='abc', green=True, signature='s', quiet_for=300.0, quiet_limit=200.0)
        got = state_of('bug', ev, c.RESOLVED)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'quiet'))

    def test_hole_epic_children_closed(self):
        got = state_of('epic', Ev(children=(c.CLOSED, c.CLOSED)), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'children-closed'))

    def test_bug_without_signature_closes_on_green_alone(self):
        got = state_of('bug', Ev(merged_sha='abc', green=True), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'quiet'))

    def test_bug_signature_inside_the_limit_stays_resolved(self):
        ev = Ev(merged_sha='abc', green=True, signature='s', quiet_for=100.0, quiet_limit=200.0)
        got = state_of('bug', ev, c.NEW)
        self.assertEqual((got.state, got.rule), (c.RESOLVED, 'fixed'))

    def test_epic_typed_closed_is_an_early_close(self):
        got = state_of('epic', Ev(children=(c.ACTIVE, c.CLOSED), typed_closed=True), c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'typed-closed'))

    def test_sticky_holds_closed(self):
        got = sticky(c.CLOSED, Closing(c.ACTIVE, 'in-flight'))
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'closed-terminal'))
        self.assertEqual(len(got.lines), 1)
        self.assertIn('in-flight', got.lines[0])
        self.assertIn(c.ACTIVE, got.lines[0])

    def test_sticky_passes_every_other_transition_untouched(self):
        for old, new in itertools.product(STATES, STATES):
            if old == c.CLOSED and new != c.CLOSED:
                continue
            closing = Closing(new, 'r')
            self.assertIs(sticky(old, closing), closing, (old, new))

    def test_task_with_merged_commit_and_open_pr_is_closed_not_active(self):
        ev = Ev(commit='abc', green=True, pr_state='open', branch='worker/x', open_prs=(3,))
        got = state_of('task', ev, c.NEW)
        self.assertEqual((got.state, got.rule), (c.CLOSED, 'landed-green'))

    def test_parent_closed_does_not_override_own_evidence(self):
        got = state_of('task', Ev(parent_closed=True, branch='worker/x'), c.NEW)
        self.assertEqual((got.state, got.rule), (c.ACTIVE, 'in-flight'))

    def test_ev_is_null_per_field(self):
        self.assertEqual(dataclasses.asdict(Ev())['commit'], '')


class PredatesTests(unittest.TestCase):
    """§2.5's four clauses, and the marker line the groom reads (P13)."""
    SINCE = '2026-09-21'

    def item(self, **kw):
        return {'id': 'F-0031', 'state': 'New', 'created': '2026-09-11', **kw}

    def test_created_before_the_marker_and_named_by_nothing_predates(self):
        self.assertTrue(c.predates(self.item(), Ev(), self.SINCE))

    def test_unset_marker_is_never(self):
        self.assertFalse(c.predates(self.item(), Ev(), None))
        self.assertFalse(c.predates(self.item(), Ev(), ''))

    def test_created_on_or_after_the_marker_does_not(self):
        self.assertFalse(c.predates(self.item(created='2026-09-21'), Ev(), self.SINCE))
        self.assertFalse(c.predates(self.item(created='2026-09-30'), Ev(), self.SINCE))

    def test_stage_since_stands_in_for_created(self):
        item = {'state': 'New', 'stage_since': '2026-09-01T00:00:00Z'}
        self.assertTrue(c.predates(item, Ev(), self.SINCE))
        self.assertFalse(c.predates({'state': 'New'}, Ev(), self.SINCE))

    def test_a_commit_a_pr_or_a_branch_does_not(self):
        for ev in (Ev(commit='abc1234'), Ev(merged_sha='abc1234'), Ev(branch='worker/x'),
                   Ev(pr_state='OPEN'), Ev(open_prs=(3,))):
            self.assertFalse(c.predates(self.item(), ev, self.SINCE), ev)

    def test_closed_does_not(self):
        self.assertFalse(c.predates(self.item(state='Closed'), Ev(), self.SINCE))

    def test_any_typed_answer_does_not(self):
        for key, value in (('landed', '9f2ac41'), ('reconciled', '2026-09-22'),
                           ('removed', 'groom 2026-09-22')):
            self.assertFalse(c.predates(self.item(**{key: value}), Ev(), self.SINCE), key)

    def test_marked_reads_the_line_and_stops_at_an_answer(self):
        line = c.PREDATES_LINE % '2026-09-11'
        self.assertTrue(c.predates_marked({'evidence': ['x', line, 'rule: y']}))
        self.assertFalse(c.predates_marked({'evidence': ['x']}))
        self.assertFalse(c.predates_marked({}))
        self.assertFalse(c.predates_marked({'evidence': [line], 'reconciled': '2026-09-22'}))
        self.assertFalse(c.predates_marked({'evidence': [line], 'landed': 1234567}))


if __name__ == '__main__':
    unittest.main()
