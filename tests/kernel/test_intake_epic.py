"""Intake mints an Epic (2026-10-10: an inbox note declaring an Epic — a release's candidate
Feature list — had no way in: ``asf new epic`` sends new work to the inbox, and intake could mint
only Features, Bugs, Stories and Tasks. The note became F-0345, a Feature under E-0001 (the first
of several Epics tied on the words ``ASF``/``0``), and a spec session launched on it).

The hierarchy is Epic > Feature > Story > Task; a Bug hangs under an Epic, a Feature or a Story.

- A note declaring ``type: epic`` and no ``parent:`` is minted an Epic by code (shape rule
  ``declared``), its body kept; with a ``parent:`` it is a question — an Epic has no parent.
- An intake-decide verdict may say ``kind: epic`` for a note; an Epic verdict naming a parent is
  rejected, and only a note (or an Epic) can be judged an Epic.
- An undecided Epic with no parent is decided by code — no session, and no spec or plan lane.
- A Feature's parent is guessed only from open Epics, and never on a tie.
"""
import os
import shutil
import tempfile
import unittest

from asf import env
from asf.groom import inbox as inbox_mod
from asf.groom import shape
from asf.kernel import intake
from asf.kernel import ports as P
from asf.record.core import tokenize

try:
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

from tests.test_inbox_shape import card, seeded_canonical, _record

State = B.State

DECLARED_EPIC = '## History\n- 2026-10-10: created (inbox) — shape: declared → epic\n'

EPIC_VERDICT = ('INTAKE-DECIDE\ndecision: need\nkind: epic\nparent: none\n'
                'reason: a release, several Features\n')


class DeclaredEpicShape(unittest.TestCase):

    def test_type_epic_with_no_parent_is_an_epic(self):
        c = card('ASF 0.4 — speed and economy', headers={'type': 'epic'},
                 description='Candidate Features:\n1. a\n2. b')
        self.assertEqual(shape.derive(c, seeded_canonical()),
                         shape.Shape('epic', 'declared', None))

    def test_type_epic_with_a_parent_is_a_question_never_a_feature(self):
        c = card('ASF 0.4', headers={'type': 'epic', 'parent': 'E-0001'})
        got = shape.derive(c, seeded_canonical())
        self.assertIsInstance(got, shape.Question)
        self.assertIn('no parent', got.text)

    def test_a_feature_is_never_guessed_under_a_closed_epic(self):
        canonical = {'E-0001': _record(['id: E-0001', 'type: epic', 'title: billing']),
                     'E-0002': _record(['id: E-0002', 'type: epic', 'title: onboarding'])}
        canonical['E-0001']['meta']['state'] = 'Resolved'
        self.assertIsNone(shape.infer_parent_epic(canonical, tokenize('billing tiers')))

    def test_a_tie_between_epics_is_no_guess(self):
        canonical = {'E-0001': _record(['id: E-0001', 'type: epic', 'title: ASF 0.1 factory']),
                     'E-0002': _record(['id: E-0002', 'type: epic', 'title: ASF 0.2 learning'])}
        self.assertIsNone(shape.infer_parent_epic(canonical, tokenize('ASF 0.4 speed')))
        self.assertEqual(shape.infer_parent_epic(canonical, tokenize('ASF factory speed')),
                         'E-0001')


class EpicVerdict(unittest.TestCase):

    def items(self):
        return {'E-0001': B.item('E-0001'), 'F-0001': B.item('F-0001', parent='E-0001')}

    def note(self):
        return B.item(intake.note_key('x.md'), type=intake.NOTE, title='ASF 0.4',
                      body='# ASF 0.4\n', question='Which Epic?')

    def test_kind_epic_is_a_verdict_and_applies_to_a_note_as_the_epic_clause(self):
        v, why = intake.parse_verdict(EPIC_VERDICT, by='job')
        self.assertEqual(why, '')
        self.assertEqual((v.kind, v.parent), ('epic', ''))
        self.assertEqual(intake.check(v, self.note(), self.items()), '')
        clauses = intake.note_clauses(self.note(), v)
        self.assertEqual(clauses, 'epic')
        self.assertEqual(inbox_mod.parse_answer(clauses), ({'type': 'epic'}, None))

    def test_an_epic_never_has_a_parent(self):
        v = intake.Verdict('need', 'epic', 'E-0001', reason='x')
        self.assertIn('no parent', intake.check(v, self.note(), self.items()))
        e = B.item('E-0007', decided=False)
        self.assertIn('no parent', intake.check(v, e, self.items()))

    def test_only_a_note_or_an_epic_is_judged_an_epic(self):
        v = intake.Verdict('need', 'epic', '', reason='x')
        self.assertIn('epic', intake.check(v, self.items()['F-0001'], self.items()))

    def test_only_a_feature_or_a_bug_hangs_under_an_epic(self):
        items = self.items()
        for kind in ('story', 'task'):
            it = B.item('S-0001' if kind == 'story' else 'T-0001', type=kind, decided=False)
            v = intake.Verdict('need', kind, 'E-0001', reason='x')
            self.assertIn('is a epic', intake.check(v, it, items), kind)
        for kind in ('feature', 'bug'):
            v = intake.Verdict('need', kind, 'E-0001', reason='x')
            self.assertEqual(intake.check(v, self.note(), items), '', kind)

    def test_a_declared_epic_is_decided_by_code(self):
        e = B.item('E-0007', body=DECLARED_EPIC, decided=False)
        v = intake.shortcut(e, {})
        self.assertEqual((v.decision, v.kind, v.parent, v.by), ('need', 'epic', '', 'code'))

    def test_an_epic_carrying_a_parent_is_not_decided_by_code(self):
        e = B.item('E-0007', parent='E-0001', body=DECLARED_EPIC, decided=False)
        self.assertIsNone(intake.shortcut(e, {'E-0001': B.item('E-0001')}))


class RealRecordEpic(unittest.TestCase):
    """The record port mints a declared Epic from the inbox, its body kept, and decides it."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'epics'))
        with open(os.path.join(self.root, 'epics', 'E-0001.md'), 'w', encoding='utf-8') as f:
            f.write('---\nid: E-0001\ntype: epic\ntitle: ASF 0.1 factory\ndecided: true\n---\n'
                    '## Description\n\n## History\n- 2026-10-01: created\n')
        os.makedirs(os.path.join(self.root, 'inbox'))
        with open(os.path.join(self.root, 'inbox', 'asf-0-4.md'), 'w', encoding='utf-8') as f:
            f.write('# ASF 0.4 — speed and economy\ntype: epic\n\n'
                    'Candidate Features, in rank order:\n1. Review loop\n2. Tick speed\n')
        self.product = env.Product('sample', {'backlog_dir': self.root,
                                              'conventions': {'default_bug_epic': 'E-0001'}})
        self.state = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def test_the_note_is_minted_an_epic_with_its_body_and_decided_by_code(self):
        rec = P.RealRecord(self.product, state_dir=self.state)
        [new] = rec.mint_inbox()
        self.assertTrue(new.startswith('E-'), new)
        epic = rec.items()[new]
        self.assertEqual((epic.type, epic.parent or ''), ('epic', ''))
        self.assertIn('2. Tick speed', epic.body)
        v = intake.shortcut(epic, rec.items())
        self.assertIsNotNone(v)
        rec.decide_intake(new, v)
        self.assertTrue(P.RealRecord(self.product, state_dir=self.state).items()[new].decided)

    def test_a_declared_priority_is_kept_on_the_epic(self):
        with open(os.path.join(self.root, 'inbox', 'asf-0-4.md'), 'w', encoding='utf-8') as f:
            f.write('# Speed and economy\ntype: epic\npriority: need\n\nCandidates:\n1. a\n')
        rec = P.RealRecord(self.product, state_dir=self.state)
        [new] = rec.mint_inbox()
        v = intake.shortcut(rec.items()[new], rec.items())
        self.assertEqual(v.decision, 'need')
        rec.decide_intake(new, v)
        epic = P.RealRecord(self.product, state_dir=self.state).items()[new]
        self.assertEqual((epic.type, epic.priority, epic.decided), ('epic', 'need', True))
        self.assertNotIn('priority: need', epic.body)


if __name__ == '__main__':
    unittest.main()
