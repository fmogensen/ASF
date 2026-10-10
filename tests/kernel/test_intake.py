"""Intake each tick (2026-10-10: the 0.2 kernel had no decision step — the groom minted inbox
cards "awaiting a decision → answer: ____" and dozens sat undecided for 8-23 h, and every inbox
note filed that day sat unminted behind its shape question).

- The tick runs the groom's own minting path, so an inbox note becomes a card.
- A card whose text names its kind and parent (a spec's Story, a plan's Task, a Bug filed by its
  signature), or whose work is Done, or that is parked, is decided by code — no session.
- Every other undecided card or inbox note gets one light ``intake-decide`` session, at most
  ``kernel.intake.decide_per_tick`` a tick, on seats every finishing and building launch left
  idle. It returns a structured verdict that code validates and applies through the groom's own
  answer grammar; ``need|nice|later`` set the card's priority.
- Each decision logs ``INTAKE <id> -> <decision>``; the tick counts them.
"""
import os
import tempfile
import unittest

from asf import env
from asf.groom import groom as groom_mod
from asf.groom import inbox as inbox_mod
from asf.kernel import actions as A
from asf.kernel import briefs, intake, loop, settings
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

GUESSED = '## History\n- 2026-10-10: created (inbox) — shape: default → feature\n'
SIGNED = '## History\n- 2026-10-10: created (inbox) — shape: signature → bug\n'
DECLARED = '## History\n- 2026-10-09: created (kernel: declared) — shape: parent-feature → story\n'

GOOD = ('INTAKE-DECIDE\ndecision: nice\nkind: feature\nparent: E-0001\nseverity: S2  # bugs only\n'
        'reason: a real gap, not urgent\n')


def cfg(**kw):
    kw.setdefault('intake', True)
    kw.setdefault('max_sessions', 4)
    return B.config(**kw)


def epic():
    return B.item('E-0001')  # unranked: its Features get no spec launch


def feature(iid='F-0010', **kw):
    kw.setdefault('parent', 'E-0001')
    kw.setdefault('body', GUESSED)
    kw.setdefault('decided', False)
    kw.setdefault('created', '2026-10-10')
    return B.item(iid, **kw)


def note(name='a-note.md', title='a note', body='# a note\n\nbody\n', question='Which Epic?'):
    return B.item(intake.note_key(name), type=intake.NOTE, title=title, body=body,
                  question=question)


def world(*items, notes=(), **kw):
    return B.facts([epic()] + list(items), notes={n.id: n for n in notes}, **kw)


class Verdicts(unittest.TestCase):

    def test_a_good_block_is_read_and_a_feature_drops_its_severity(self):
        v, why = intake.parse_verdict('chat\n```\n' + GOOD + '```\n', by='job-1')
        self.assertEqual(why, '')
        self.assertEqual((v.decision, v.kind, v.parent, v.severity, v.by),
                         ('nice', 'feature', 'E-0001', '', 'job-1'))
        self.assertTrue(v.set_priority)

    def test_a_block_outside_its_words_is_rejected_whole(self):
        for bad, why in (('decision: maybe', 'decision'), ('kind: story', 'kind'),
                         ('parent: the big one', 'parent'), ('reason: ', 'reason')):
            text = '\n'.join(bad if ln.split(':')[0] == bad.split(':')[0] else ln
                             for ln in GOOD.splitlines())
            v, got = intake.parse_verdict(text)
            self.assertIsNone(v, bad)
            self.assertTrue(got.startswith(why), got)
        self.assertEqual(intake.parse_verdict('no block')[1], 'no INTAKE-DECIDE block')
        v, why = intake.parse_verdict(GOOD.replace('feature', 'bug').replace('S2', 'S9'))
        self.assertIsNone(v)
        self.assertIn('severity', why)

    def test_none_is_no_parent(self):
        v, _ = intake.parse_verdict(GOOD.replace('E-0001', 'none'))
        self.assertEqual(v.parent, '')

    def test_check_validates_the_parent_against_the_record(self):
        items = {'E-0001': epic(), 'F-0001': B.item('F-0001'), 'E-0009': B.item(
            'E-0009', state=State.DONE)}
        card = feature()
        v = intake.Verdict('need', 'feature', 'F-0001', reason='x')
        self.assertIn('is a feature', intake.check(v, card, items))
        v.parent = 'E-0404'
        self.assertIn('not on the record', intake.check(v, card, items))
        v.parent = 'E-0009'
        self.assertIn('is Done', intake.check(v, card, items))
        v.parent = 'E-0001'
        self.assertEqual(intake.check(v, card, items), '')
        v.parent = ''
        self.assertIn('needs a parent', intake.check(v, note(), items))
        self.assertEqual(intake.check(intake.Verdict('close', reason='x'), note(), items), '')


class Grammar(unittest.TestCase):
    """The words the verdict is applied with are the groom's own grammar."""

    def test_a_card_answer_is_words_the_groom_parses(self):
        card = B.item('B-0001', parent='E-0001', severity='S3', decided=False)
        v = intake.Verdict('need', 'bug', 'F-0001', 'S1', 'x', 'job', True)
        words = intake.answer_words(card, v)
        self.assertEqual(words, ['parent F-0001', 'S1', 'yes'])
        self.assertEqual([groom_mod._parse_answer(w)[0] for w in words],
                         ['parent', 'severity', 'decided'])
        close = intake.answer_words(card, intake.Verdict('close', reason='obsolete under 0.2'))
        self.assertEqual(groom_mod._parse_answer(close[0]), ('removed', 'intake: obsolete under 0.2'))

    def test_a_note_answer_is_clauses_the_inbox_parses(self):
        n = note(title='x fails; badly')
        v = intake.Verdict('need', 'bug', 'E-0001', 'S2', 'x', 'job', True)
        clauses = intake.note_clauses(n, v)
        self.assertEqual(clauses, 'bug x fails, badly; parent E-0001; S2')
        self.assertEqual(inbox_mod.parse_answer(clauses),
                         ({'signature': 'x fails, badly', 'parent': 'E-0001', 'severity': 'S2'},
                          None))
        self.assertEqual(inbox_mod.parse_answer(intake.note_clauses(
            n, intake.Verdict('need', 'feature', 'E-0001', reason='x')))[0],
            {'type': 'feature', 'parent': 'E-0001'})
        self.assertEqual(intake.note_clauses(n, intake.Verdict('close', reason='x')), 'close')

    def test_priority_is_set_by_a_session_and_by_spent_tries_only(self):
        self.assertEqual(intake.priority_of(intake.Verdict('later', set_priority=True)), 'later')
        self.assertEqual(intake.priority_of(intake.Verdict('need')), '')
        self.assertEqual(intake.priority_of(intake.Verdict('close', set_priority=True)), '')


class Shortcut(unittest.TestCase):

    def test_a_declared_story_is_decided_by_code_with_its_parents_priority(self):
        items = {'F-0001': B.item('F-0001', priority='nice'), 'E-0001': epic()}
        s = B.item('S-0001', parent='F-0001', body=DECLARED, decided=False)
        v = intake.shortcut(s, items)
        self.assertEqual((v.decision, v.by, v.set_priority), ('nice', 'code', False))

    def test_a_guessed_feature_goes_to_a_session(self):
        self.assertIsNone(intake.shortcut(feature(), {'E-0001': epic()}))

    def test_a_signed_bug_under_its_epic_is_decided_by_code(self):
        bug = B.item('B-0001', parent='E-0001', signature='x: y', body=SIGNED, decided=False)
        self.assertEqual(intake.shortcut(bug, {'E-0001': epic()}).decision, 'need')

    def test_a_story_whose_parent_is_done_or_missing_goes_to_a_session(self):
        s = B.item('S-0001', parent='F-0001', decided=False)
        self.assertIsNone(intake.shortcut(s, {}))
        self.assertIsNone(intake.shortcut(s, {'F-0001': B.item('F-0001', state=State.DONE)}))

    def test_done_work_is_decided_by_code(self):
        v = intake.shortcut(feature(state=State.DONE), {})
        self.assertEqual((v.decision, v.reason), ('need', 'its work is done'))


class Plan(unittest.TestCase):

    def test_code_decides_now_and_a_session_judges_the_rest_within_its_budget(self):
        guessed = [feature('F-%04d' % n) for n in range(10, 15)]
        story = B.item('S-0001', parent='F-0010', body=DECLARED, decided=False)
        plan = decide(world(story, *guessed), cfg(max_sessions=10))
        self.assertEqual([(a.item_id, a.decision, a.by) for a in B.of(plan, A.Decide)],
                         [('S-0001', 'need', 'code')])
        self.assertEqual(B.launched(plan, intake.KIND), ['F-0010', 'F-0011', 'F-0012'])
        self.assertEqual(B.of(plan, A.Launch)[0].branch, 'intake-decide/F-0010')

    def test_sessions_take_only_seats_every_other_launch_left_idle(self):
        tasks = [B.task('T-%04d' % n, parent='F-0001') for n in range(1, 4)]
        f = B.item('F-0001', parent='E-0001')
        plan = decide(world(f, feature(), *tasks), cfg(max_sessions=4))
        kinds = [a.kind for a in B.of(plan, A.Launch)]
        self.assertEqual(kinds, ['build', 'build', 'build', intake.KIND])
        plan = decide(world(f, feature(), *tasks), cfg(max_sessions=3))
        self.assertNotIn(intake.KIND, [a.kind for a in B.of(plan, A.Launch)])

    def test_notes_first_then_bugs_then_the_oldest(self):
        bug = B.item('B-0001', parent='E-0001', decided=False, body=GUESSED, created='2026-10-11')
        old = feature('F-0020', created='2026-10-01')
        new = feature('F-0010', created='2026-10-09')
        _decisions, asks = intake.plan(world(new, old, bug, notes=[note()]), cfg(), free=3)
        self.assertEqual(asks, ['inbox.a-note', 'B-0001', 'F-0020'])
        plan = decide(world(new, old, bug, notes=[note()]), cfg(max_sessions=10))
        self.assertEqual(B.launched(plan, intake.KIND), ['inbox.a-note', 'F-0020', 'F-0010'],
                         'a Bug a build launch takes this tick is asked next tick')

    def test_a_key_with_a_live_session_is_left_alone(self):
        s = B.session('intake-decide-f-0010', 'F-0010', kind=intake.KIND, alive=True)
        plan = decide(world(feature(), sessions=[s]), cfg())
        self.assertEqual(B.launched(plan, intake.KIND), [])
        self.assertEqual(B.of(plan, A.Decide), [])

    def test_spent_tries_park_a_card_and_leave_a_note_alone(self):
        plan = decide(world(feature(), notes=[note()],
                            intake_tries={'F-0010': 2, 'inbox.a-note': 2}), cfg())
        self.assertEqual(B.launched(plan, intake.KIND), [])
        [d] = B.of(plan, A.Decide)
        self.assertEqual((d.item_id, d.decision, d.set_priority), ('F-0010', 'later', True))

    def test_a_parked_card_is_decided_later_by_code(self):
        e = B.item('E-0002', priority='later')
        plan = decide(world(e, feature('F-0010', parent='E-0002')), cfg())
        self.assertEqual([(a.item_id, a.decision) for a in B.of(plan, A.Decide)],
                         [('F-0010', 'later')])
        self.assertEqual(B.launched(plan, intake.KIND), [])

    def test_paused_or_off_launches_nothing(self):
        self.assertEqual(B.launched(decide(world(feature(), paused=True), cfg()), intake.KIND), [])
        plan = decide(world(feature(), B.item('S-0001', parent='F-0010', decided=False)),
                      cfg(intake=False))
        self.assertEqual((B.launched(plan, intake.KIND), B.of(plan, A.Decide)), ([], []))

    def test_an_intake_session_never_moves_the_items_state(self):
        f = B.item('F-0001', parent='E-0001', rank=1)
        t = B.task('T-0001', parent='F-0001', decided=False, body=GUESSED)
        s = B.session('intake-decide-t-0001', 'T-0001', kind=intake.KIND, alive=True)
        plan = decide(world(f, t, sessions=[s]), cfg())
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.launched(plan, 'build'), ['T-0001'],
                         'the build launches beside the live intake session')


def _end(report, *items, notes=(), job='intake-decide-f-0010', key='F-0010'):
    s = B.session(job, key, kind=intake.KIND, alive=False, ended=True, result='report',
                  report=report)
    rec = F.FakeRecord([epic()] + list(items), notes=notes)
    sess = F.FakeSessions([s])
    facts = world(*items, notes=notes, sessions=[s])
    lines = []
    apply(decide(facts, cfg(max_sessions=0)), facts, F.ports(record=rec, sessions=sess),
          log=lines.append)
    return rec, lines


class Apply(unittest.TestCase):

    def test_the_launch_counts_a_try_and_leaves_the_state(self):
        rec = F.FakeRecord([epic(), feature()])
        sess = F.FakeSessions()
        facts = world(feature())
        apply(decide(facts, cfg()), facts, F.ports(record=rec, sessions=sess),
              log=lambda *_: None)
        self.assertEqual([(k, i) for k, i, _b, _t in sess.launched], [(intake.KIND, 'F-0010')])
        self.assertEqual(rec.tries, {'F-0010': 1})
        self.assertNotIn(P.STATE, rec.fields['F-0010'])

    def test_a_note_launches_with_its_text_in_the_brief(self):
        n = note()
        rec = F.FakeRecord([epic()], notes=[n])
        sess = F.FakeSessions()
        facts = world(notes=[n])
        apply(decide(facts, cfg()), facts, F.ports(record=rec, sessions=sess),
              log=lambda *_: None)
        self.assertEqual([(k, i, b) for k, i, b, _t in sess.launched],
                         [(intake.KIND, 'inbox.a-note', 'intake-decide/inbox.a-note')])
        self.assertEqual(rec.tries, {'inbox.a-note': 1})

    def test_a_valid_verdict_is_applied_and_logged(self):
        rec, lines = _end(GOOD, feature())
        [(key, v)] = rec.decided
        self.assertEqual((key, v.decision, v.parent, v.by), ('F-0010', 'nice', 'E-0001',
                                                              'intake-decide-f-0010'))
        self.assertIn('INTAKE F-0010 -> nice (intake-decide-f-0010: a real gap, not urgent)',
                      lines)

    def test_a_rejected_verdict_applies_nothing_and_says_why(self):
        rec, lines = _end(GOOD.replace('E-0001', 'F-0404'), feature())
        self.assertEqual(rec.decided, [])
        self.assertTrue(any('intake verdict rejected: parent F-0404 is not on the record' in ln
                            for ln in lines), lines)

    def test_a_note_verdict_is_applied_to_the_note(self):
        n = note()
        rec, lines = _end(GOOD.replace('kind: feature', 'kind: bug'), notes=[n],
                          job='intake-decide-inbox.a-note', key=n.id)
        [(key, v)] = rec.decided
        self.assertEqual((key, v.kind), ('inbox.a-note', 'bug'))

    def test_code_decisions_are_applied_and_counted(self):
        story = B.item('S-0001', parent='F-0001', body=DECLARED, decided=False)
        f = B.item('F-0001', parent='E-0001')
        rec = F.FakeRecord([epic(), f, story])
        facts = world(f, story)
        lines = []
        plan = decide(facts, cfg())
        apply(plan, facts, F.ports(record=rec, sessions=F.FakeSessions()), log=lines.append)
        self.assertEqual([k for k, _v in rec.decided], ['S-0001'])
        self.assertTrue(any(ln.startswith('INTAKE S-0001 -> need (code:') for ln in lines))
        self.assertEqual(loop.summarize(plan, facts)['intake'],
                         {'decided': 1, 'launched': 0})


class Brief(unittest.TestCase):

    def test_the_brief_carries_the_card_the_goals_the_rule_and_the_schema(self):
        product = env.Product('sample', {'main': 'main'})
        launch = A.Launch(intake.KIND, 'F-0010', 'intake-decide/F-0010',
                          ['E-0001 (rank 1) the kernel'])
        b = briefs.build(product, launch, feature(title='a gap', body='## Description\nwhy\n'),
                         launch.findings)
        self.assertEqual(b.kind, intake.KIND)
        self.assertEqual(b.model, settings.LIGHT_MODEL)
        for part in ('a gap', 'why', 'E-0001 (rank 1) the kernel', intake.VERDICT_SCHEMA,
                     'obsolete under the 0.2 kernel'):
            self.assertIn(part, b.text)
        self.assertLess(len(b.text), 6000)

    def test_the_goals_are_the_open_epics_by_rank(self):
        items = {'E-0002': B.item('E-0002', title='two', rank=2),
                 'E-0001': B.item('E-0001', title='one', rank=1, priority='need'),
                 'E-0003': B.item('E-0003', title='gone', state=State.DONE),
                 'E-0004': B.item('E-0004', title='unranked')}
        self.assertEqual(intake.goals(items), ['E-0001 (rank 1, need) one', 'E-0002 (rank 2) two',
                                               'E-0004 (unranked) unranked'])


class Settings(unittest.TestCase):

    def test_the_intake_block_and_its_defaults(self):
        k = settings.read(None)
        self.assertEqual(k['intake'], {'enabled': True, 'decide_per_tick': 3, 'max_tries': 2})
        self.assertEqual(k['models'][intake.KIND], settings.LIGHT_MODEL)

    def test_the_stop_gate_never_holds_an_intake_session_for_a_push(self):
        from asf.workers import lifecycle
        self.assertFalse(lifecycle.lands({'kind': intake.KIND, 'branch': 'intake-decide/F-1'}))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class RealRecordIntake(unittest.TestCase):
    """The record port mints the inbox and applies a verdict through the groom's own writers."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        _write(os.path.join(self.root, 'epics', 'E-0001.md'),
               '---\nid: E-0001\ntype: epic\ntitle: the factory\ndecided: true\n---\n'
               '## Description\n\n## History\n- 2026-10-01: created\n')
        _write(os.path.join(self.root, 'features', 'F-0010.md'),
               '---\nid: F-0010\ntype: feature\ntitle: a gap\nparent: E-0001\ndecided: false\n'
               '---\n## Description\nwhy\n\n## Acceptance\n- [ ] \n\n' + GUESSED)
        self.product = env.Product('sample', {'backlog_dir': self.root,
                                              'conventions': {'default_bug_epic': 'E-0001'}})
        self.state = tempfile.mkdtemp()

    def rec(self):
        return P.RealRecord(self.product, state_dir=self.state)

    def card(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as f:
            return f.read()

    def test_a_verdict_decides_the_card_and_sets_its_priority(self):
        v = intake.Verdict('later', 'feature', '', '', 'not now', 'job-1', True)
        self.rec().decide_intake('F-0010', v)
        text = self.card('features/F-0010.md')
        self.assertIn('decided: true', text)
        self.assertIn('priority: later', text)
        self.assertIn('groom: decided → true (adjudicator, job-1)', text)
        it = self.rec().items()['F-0010']
        self.assertTrue(it.decided)
        self.assertEqual(it.priority, 'later')

    def test_close_retires_the_card(self):
        self.rec().decide_intake('F-0010', intake.Verdict('close', reason='obsolete', by='j',
                                                          set_priority=True))
        self.assertIn('intake: obsolete', self.card('features/F-0010.md'))
        self.assertEqual(self.rec().items()['F-0010'].state, State.DONE)

    def test_an_undecided_card_reads_undecided(self):
        self.assertFalse(self.rec().items()['F-0010'].decided)
        self.assertTrue(self.rec().items()['E-0001'].decided)

    def test_the_inbox_is_minted_and_a_question_is_a_note(self):
        _write(os.path.join(self.root, 'inbox', 'fine.md'),
               '# the factory gains a clock\n\nparent: E-0001\n\nnew work\n')
        _write(os.path.join(self.root, 'inbox', 'odd.md'),
               '# frobnicate the widgets\n\nsomething\n')
        rec = self.rec()
        created = rec.mint_inbox()
        self.assertEqual(len(created), 1)
        self.assertEqual(rec.items()[created[0]].title, 'the factory gains a clock')
        notes = rec.notes()
        self.assertEqual(list(notes), ['inbox.odd'])
        self.assertEqual(notes['inbox.odd'].title, 'frobnicate the widgets')
        self.assertIn('Which Epic', notes['inbox.odd'].question)

    def test_a_note_verdict_mints_the_card_and_decides_it(self):
        _write(os.path.join(self.root, 'inbox', 'odd.md'),
               '# frobnicate the widgets\n\nsomething\n')
        rec = self.rec()
        rec.mint_inbox()
        v = intake.Verdict('nice', 'feature', 'E-0001', '', 'worth it', 'job-2', True)
        note = rec.decide_intake('inbox.odd', v)
        self.assertRegex(note, r'minted F-\d+')
        rec = self.rec()
        [card] = [it for it in rec.items().values() if it.title == 'frobnicate the widgets']
        self.assertTrue(card.decided)
        self.assertEqual((card.parent, card.priority), ('E-0001', 'nice'))
        self.assertEqual(rec.notes(), {})

    def test_tries_are_counted_in_the_state_dir(self):
        rec = self.rec()
        rec.count_intake('F-0010')
        rec.count_intake('F-0010')
        self.assertEqual(self.rec().intake_tries(), {'F-0010': 2})


if __name__ == '__main__':
    unittest.main()
