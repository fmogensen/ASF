"""Every inbox note is heard (2026-10-10: two notes sat silent in the inbox for hours).

- ``kernel-story-limbo-…``: its intake-decide sessions both said ``kind: feature`` under
  F-0334 — a Feature — and the kernel rejected the verdict ("a feature hangs under epic"). The
  note's two tries were then spent and it stayed in the inbox with no line, no Stuck, nothing.
  A note judged a Feature under a Feature is a Story candidate under it: the code corrects that
  verdict to a Story (the fix is determined). A rejection the code cannot correct is kept and
  handed to the next session as a finding; once the tries are spent the note is Stuck on the
  operator with the reason.
- ``adopt-github-s-work-item-model-…``: a Feature-shaped note (``parent: E-0003``) whose
  ``## Acceptance`` list was plain ``- `` bullets. Intake read only ``- [ ]`` checkboxes, so the
  note had no acceptance, its words ("fails") read as a defect, and it sat on a question waiting
  for a seat that never came free. Plain bullets are acceptance lines; its ``type:`` bullet in the
  body is not a header.
- The rule: each tick, every inbox note is minted, decided, in a session, or named — ``INTAKE
  STUCK`` (owner and reason) or ``INTAKE WAIT`` (why) — never silent.
"""
import os
import tempfile
import unittest

from asf import env
from asf.groom import inbox as inbox_mod
from asf.groom import shape
from asf.kernel import actions as A
from asf.kernel import intake, loop
from asf.kernel import ports as P
from asf.kernel.apply import apply
from asf.kernel.decide import decide

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

from tests.test_inbox_shape import seeded_canonical

FEATURE_UNDER_FEATURE = ('INTAKE-DECIDE\ndecision: need\nkind: feature\nparent: F-0001\n'
                         'reason: under the limbo Feature\n')
UNKNOWN_PARENT = ('INTAKE-DECIDE\ndecision: need\nkind: feature\nparent: E-0404\n'
                  'reason: a guess\n')

ADOPT = """# Adopt GitHub's work-item model: type, parent, milestone, priority
parent: E-0001

Adopt GitHub's work-item model on the flat-file record.

## Model
- `type:` epic | feature | story | task | bug (exists).
- `parent:` the hierarchy Epic > Feature > Story > Task. An Epic never has a parent.
- `milestone:` a version name; an item naming an undeclared milestone fails `asf check`.

## Acceptance
- `asf check` rejects an Epic with a parent (test per rule).
- A child without `milestone:` reports its parent's milestone (test).
"""


def cfg(**kw):
    kw.setdefault('intake', True)
    kw.setdefault('max_sessions', 4)
    return B.config(**kw)


def note(name='a-note.md', title='a note', question='Which Epic is this under?'):
    return B.item(intake.note_key(name), type=intake.NOTE, title=title, body='# %s\n' % title,
                  question=question)


def world(*items, notes=(), **kw):
    return B.facts([B.item('E-0001'), B.item('F-0001', parent='E-0001')] + list(items),
                   notes={n.id: n for n in notes}, **kw)


def _end(report, n):
    s = B.session('intake-decide-' + n.id, n.id, kind=intake.KIND, alive=False, ended=True,
                  result='report', report=report)
    rec = F.FakeRecord([B.item('E-0001'), B.item('F-0001', parent='E-0001')], notes=[n])
    facts = world(notes=[n], sessions=[s])
    lines = []
    apply(decide(facts, cfg(max_sessions=0)), facts,
          F.ports(record=rec, sessions=F.FakeSessions([s])), log=lines.append)
    return rec, lines


class RejectedVerdict(unittest.TestCase):

    def test_a_note_judged_a_feature_under_a_feature_is_corrected_to_a_story_under_it(self):
        rec, lines = _end(FEATURE_UNDER_FEATURE, note())
        [(key, v)] = rec.decided
        self.assertEqual((key, v.kind, v.parent, v.decision), ('inbox.a-note', 'story',
                                                               'F-0001', 'need'))
        self.assertTrue(any('corrected by code' in ln for ln in lines), lines)
        self.assertEqual(intake.note_clauses(note(), v), 'story; parent F-0001')

    def test_story_is_an_inbox_clause_and_a_session_word(self):
        self.assertEqual(inbox_mod.parse_answer('story; parent F-0001'),
                         ({'type': 'story', 'parent': 'F-0001'}, None))
        v, why = intake.parse_verdict(FEATURE_UNDER_FEATURE.replace('kind: feature',
                                                                    'kind: story'))
        self.assertEqual((why, v.kind), ('', 'story'))
        items = world().items
        self.assertEqual(intake.check(v, note(), items), '')
        self.assertIn('Feature', intake.check(intake.Verdict('need', 'story', '', reason='x'),
                                              note(), items))

    def test_a_rejection_the_code_cannot_correct_is_kept_for_the_next_session(self):
        rec, lines = _end(UNKNOWN_PARENT, note())
        self.assertEqual(rec.decided, [])
        self.assertIn('parent E-0404 is not on the record', rec.rejections['inbox.a-note'])

    def test_a_kept_rejection_reaches_the_note_and_so_the_brief(self):
        root, state = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(root, 'inbox'))
        with open(os.path.join(root, 'inbox', 'odd.md'), 'w', encoding='utf-8') as f:
            f.write('# frobnicate the widgets\n\nsomething\n\n## Question\nWhich Epic?\n')
        rec = P.RealRecord(env.Product('sample', {'backlog_dir': root}), state_dir=state)
        rec.intake_rejected('inbox.odd', 'parent F-0001 is a feature')
        self.assertIn('parent F-0001 is a feature', rec.notes()['inbox.odd'].question)
        self.assertEqual(P.RealRecord(env.Product('sample', {'backlog_dir': root}),
                                      state_dir=state).intake_rejections(),
                         {'inbox.odd': 'parent F-0001 is a feature'})


class RealRecordStory(unittest.TestCase):

    def test_a_feature_note_corrected_to_a_story_is_minted_under_its_feature(self):
        root, state = tempfile.mkdtemp(), tempfile.mkdtemp()
        for rel, text in (
                ('epics/E-0001.md', '---\nid: E-0001\ntype: epic\ntitle: the factory\n'
                                    'decided: true\n---\n## Description\n\n## History\n'),
                ('features/F-0001.md', '---\nid: F-0001\ntype: feature\ntitle: limbo\n'
                                       'parent: E-0001\ndecided: true\n---\n## Description\n'
                                       '\n## History\n'),
                ('inbox/story-limbo.md', '# Story limbo is counted\ntype: feature\n\nwhy\n\n'
                                         '## Acceptance\n- a test for it\n\n## Question\n'
                                         'Which Epic is this under?\n')):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
                f.write(text)
        rec = P.RealRecord(env.Product('sample', {'backlog_dir': root}), state_dir=state)
        v, _ = intake.parse_verdict(FEATURE_UNDER_FEATURE, by='job')
        fixed, _how = intake.correct(v, rec.notes()['inbox.story-limbo'], rec.items())
        self.assertRegex(rec.decide_intake('inbox.story-limbo', fixed), r'minted S-\d+')
        rec = P.RealRecord(env.Product('sample', {'backlog_dir': root}), state_dir=state)
        [s] = [it for it in rec.items().values() if it.type == 'story']
        self.assertEqual((s.parent, s.decided), ('F-0001', True))
        self.assertEqual(rec.notes(), {})


class AcceptanceBullets(unittest.TestCase):

    def test_plain_bullets_under_acceptance_are_acceptance_lines(self):
        c = inbox_mod.parse_inbox_file(ADOPT)
        self.assertEqual(len(c.acceptance), 2)
        self.assertEqual(c.headers, {'parent': 'E-0001'}, 'a `type:` bullet is no header')

    def test_the_milestone_note_is_a_feature_under_its_epic_not_a_defect_or_an_epic(self):
        got = shape.derive(inbox_mod.parse_inbox_file(ADOPT), seeded_canonical())
        self.assertEqual((got.type, got.parent), ('feature', 'E-0001'))


class NeverSilent(unittest.TestCase):

    def heard(self, facts, config):
        plan = decide(facts, config)
        return {k: (owner, why) for k, owner, why in intake.unheard(facts, config, plan.actions)}

    def test_a_note_in_a_session_or_launched_is_heard(self):
        n = note()
        self.assertEqual(self.heard(world(notes=[n]), cfg()), {})
        s = B.session('intake-decide-x', n.id, kind=intake.KIND, alive=True)
        self.assertEqual(self.heard(world(notes=[n], sessions=[s]), cfg()), {})

    def test_a_note_with_no_seat_is_named_waiting(self):
        got = self.heard(world(notes=[note()]), cfg(max_sessions=0))
        owner, why = got['inbox.a-note']
        self.assertEqual(owner, '')
        self.assertIn('seat', why)

    def test_a_note_whose_tries_are_spent_is_stuck_on_the_operator_with_the_reason(self):
        n = note(question='Which Epic? (the last intake verdict was rejected: parent F-0334 is '
                          'a feature)')
        got = self.heard(world(notes=[n], intake_tries={n.id: 2}), cfg())
        owner, why = got['inbox.a-note']
        self.assertEqual(owner, 'operator')
        self.assertIn('2 intake-decide session(s)', why)
        self.assertIn('parent F-0334 is a feature', why)

    def test_paused_is_named(self):
        got = self.heard(world(notes=[note()], paused=True), cfg())
        self.assertIn('paused', got['inbox.a-note'][1])

    def test_the_tick_says_each_unheard_note_and_files_the_stuck_one(self):
        n = note()
        rec = F.FakeRecord([B.item('E-0001'), B.item('F-0001', parent='E-0001')], notes=[n],
                           tries={n.id: 2})
        lines, state = [], tempfile.mkdtemp()
        product = env.Product('sample', {'backlog_dir': tempfile.mkdtemp()})
        loop.tick(product, ports=F.ports(record=rec, sessions=F.FakeSessions()), config=cfg(),
                  state_dir=state, out=lines.append)
        self.assertTrue(any(ln.startswith('INTAKE STUCK inbox.a-note [operator]')
                            for ln in lines), lines)
        import json
        with open(os.path.join(state, loop.PLAN_FILE), encoding='utf-8') as f:
            row = json.load(f)['states']['inbox.a-note']
        self.assertEqual((row['state'], row['owner']), ('stuck', 'operator'))


class OperatorAnswersANote(unittest.TestCase):
    """A Stuck inbox note has an answer path: ``asf answer <note key> --text …`` (2026-10-10:
    the Stuck row named ``inbox.<slug>`` and ``asf answer`` said "names no job or item")."""

    def setUp(self):
        self.root, self.state = tempfile.mkdtemp(), tempfile.mkdtemp()
        for rel, text in (
                ('epics/E-0001.md', '---\nid: E-0001\ntype: epic\ntitle: the factory\n'
                                    'decided: true\n---\n## Description\n\n## History\n'),
                ('features/F-0001.md', '---\nid: F-0001\ntype: feature\ntitle: limbo\n'
                                       'parent: E-0001\ndecided: true\n---\n## Description\n'
                                       '\n## History\n'),
                ('inbox/story-limbo.md', '# Story limbo is counted\ntype: feature\n\nwhy\n\n'
                                         '## Acceptance\n- a test for it\n\n## Question\n'
                                         'Which Epic is this under?\n')):
            os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
            with open(os.path.join(self.root, rel), 'w', encoding='utf-8') as f:
                f.write(text)
        self.product = env.Product('sample', {'backlog_dir': self.root})

    def rec(self):
        return P.RealRecord(self.product, state_dir=self.state)

    def test_the_operator_words_become_the_inbox_clauses(self):
        self.assertEqual(intake.operator_clauses('kind: story; parent: F-0346'),
                         ('story; parent F-0346', ''))
        self.assertEqual(intake.operator_clauses('feature, parent E-0003, S2')[0],
                         'feature; parent E-0003; S2')
        self.assertEqual(intake.operator_clauses('close')[0], 'close')
        self.assertTrue(intake.operator_clauses('make it nice')[1])

    def test_note_keys_in_every_spelling_name_the_note(self):
        for t in ('inbox.story-limbo', 'story-limbo.md', 'inbox/story-limbo.md', 'story-limbo'):
            self.assertEqual(self.rec().note_of(t), 'inbox.story-limbo', t)
        self.assertEqual(self.rec().note_of('F-0001'), '')

    def test_an_answer_is_applied_by_code_and_the_next_mint_makes_the_story(self):
        rec = self.rec()
        rec.count_intake('inbox.story-limbo')
        rec.count_intake('inbox.story-limbo')
        self.assertEqual(rec.answer_note('inbox.story-limbo', 'kind: story; parent: F-0001'), '')
        self.assertEqual(self.rec().intake_tries(), {}, 'an answer is a fresh start')
        [sid] = self.rec().mint_inbox()
        it = self.rec().items()[sid]
        self.assertEqual((it.type, it.parent), ('story', 'F-0001'))

    def test_asf_answer_takes_the_note_key(self):
        from unittest import mock
        from asf.workers import answer
        args = mock.Mock(target='inbox.story-limbo', text='kind: story; parent: F-0001',
                         file=None, product='sample')
        with mock.patch.object(env, 'load_product', return_value=self.product), \
                mock.patch.object(env, 'state_dir', return_value=self.state):
            self.assertEqual(answer.cmd_answer(args), 0)
        with open(os.path.join(self.root, 'inbox', 'story-limbo.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('## Question', text)
        self.assertIn('type: story', text)
        self.assertIn('parent: F-0001', text)


if __name__ == '__main__':
    unittest.main()
