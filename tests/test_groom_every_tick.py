"""tests.test_groom_every_tick — the groom's intake and policy pass run on every tick, not once a
day: a card filed in the evening is typed on the next tick, a card edited after its question is
read again, today's groom file only gains lines, and new questions reach an adjudicator the same
day."""
import argparse
import os
import shutil
import unittest
from unittest import mock

from asf import env
from asf.feeder import rows
from asf.groom import groom as groom_mod
from asf.groom import inbox as inbox_mod
from asf.record.core import canonicalize, load_items, today
from asf.tick import step_daily, step_wave, tick
from tests.test_feeder import fixture_index, product
from tests.test_inbox_shape import make_record, seed
from tests.test_tick_steps import StepsTestCase

DEFECT_Q = 'This reads as a defect.'


def canonical_of(root):
    return canonicalize(load_items(root)[0])[0]


def intake(root, asked=None):
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop('BACKLOG_ID_RANGE', None)
        return inbox_mod.process_inbox(root, canonical_of(root), today(), asked=asked)


class ChangedCardsAreReadAgainTests(unittest.TestCase):
    def setUp(self):
        self.root = make_record()
        seed(self.root)
        self.path = os.path.join(self.root, 'inbox', 'pay.md')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('# Checkout is broken\nparent: E-0001\n\nCustomers cannot pay.\n')
        asked = []
        self.assertEqual(intake(self.root, asked), [])
        self.assertEqual(asked, ['pay.md'])
        with open(self.path, encoding='utf-8') as f:
            self.asked_text = f.read()
        self.assertIn(DEFECT_Q, self.asked_text)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def edit(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_an_unchanged_card_is_left_as_it_is(self):
        asked = []
        self.assertEqual(intake(self.root, asked), [])
        self.assertEqual(asked, [])
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(f.read(), self.asked_text)

    def test_a_signature_added_after_the_question_types_it_a_bug(self):
        self.edit(self.asked_text.replace('parent: E-0001\n',
                                          'signature: test_pay\nparent: E-0001\n'))
        self.assertEqual(intake(self.root), ['B-0001'])
        self.assertFalse(os.path.exists(self.path))
        with open(os.path.join(self.root, 'bugs', 'B-0001.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('signature: test_pay', text)
        self.assertNotIn(DEFECT_Q, text)

    def test_an_acceptance_list_below_the_question_types_it_a_feature_and_keeps_the_notes(self):
        self.edit(self.asked_text + '\nUpdate: still broken after the last fix.\n\n'
                  '## Acceptance\n- [ ] a customer can pay\n')
        created = intake(self.root)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].startswith('F-'))
        with open(os.path.join(self.root, 'features', f'{created[0]}.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('- [ ] a customer can pay', text)
        self.assertIn('Update: still broken after the last fix.', text)
        self.assertNotIn(DEFECT_Q, text)

    def test_a_card_that_still_asks_something_else_gets_the_new_question_once(self):
        self.edit(self.asked_text.replace('parent: E-0001\n', 'parent: E-0404\n'))
        asked = []
        self.assertEqual(intake(self.root, asked), [])
        self.assertEqual(asked, ['pay.md'])
        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(text.count('## Question'), 1)
        self.assertIn('`parent: E-0404` does not exist', text)
        self.assertNotIn(DEFECT_Q, text)
        self.assertEqual(intake(self.root, asked), [])
        self.assertEqual(asked, ['pay.md'])


def groom(root, incremental):
    args = argparse.Namespace(date=None, apply=False, product=None, default_bug_epic=None,
                              answers_file=None, event=None, incremental=incremental)
    with mock.patch.object(env, 'load_product', side_effect=env.ConfigError('none')):
        return groom_mod.cmd_groom(args, root)


class IncrementalGroomFileTests(unittest.TestCase):
    def setUp(self):
        self.root = make_record()
        seed(self.root)
        os.environ.pop('BACKLOG_ID_RANGE', None)
        self.groom_path = os.path.join(self.root, 'groom', f'{today()}.md')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def card(self, name, text):
        with open(os.path.join(self.root, 'inbox', name), 'w', encoding='utf-8') as f:
            f.write(text)

    def read(self):
        with open(self.groom_path, encoding='utf-8') as f:
            return f.read()

    def test_nothing_new_and_no_file_yet_writes_nothing(self):
        self.assertEqual(groom(self.root, True), 0)
        self.assertFalse(os.path.exists(self.groom_path))

    def test_new_cards_are_added_beside_what_the_file_already_holds_and_rerunning_changes_nothing(self):
        self.card('a.md', '# Billing invoices\nparent: E-0001\n\n## Acceptance\n- [ ] invoices\n')
        self.assertEqual(groom(self.root, False), 0)
        # the operator (or a session) answered in today's file: the tick must not drop it
        before = self.read().replace('awaiting a decision → answer: ____',
                                     'awaiting a decision → answer: rank 3', 1)
        with open(self.groom_path, 'w', encoding='utf-8') as f:
            f.write(before)
        self.card('b.md', '# Billing refunds\nparent: E-0001\n\n## Acceptance\n- [ ] refunds\n')
        self.card('c.md', '# Checkout is broken\nparent: E-0001\n\nCustomers cannot pay.\n')
        self.assertEqual(groom(self.root, True), 0)
        after = self.read()
        for line in before.splitlines():
            if line.strip() and line.strip() != '(none)':
                self.assertIn(line, after)
        self.assertIn('answer: rank 3', after)
        self.assertIn('Billing refunds — from inbox as feature (default), awaiting a decision', after)
        self.assertIn('## Inbox cards with a question', after)
        self.assertIn('- [ ] inbox:c.md Checkout is broken — This reads as a defect.', after)
        self.assertEqual(groom(self.root, True), 0)
        self.assertEqual(self.read(), after)

    def test_a_question_whose_card_was_typed_since_is_settled_not_left_open(self):
        self.card('c.md', '# Checkout is broken\nparent: E-0001\n\nCustomers cannot pay.\n')
        self.assertEqual(groom(self.root, False), 0)
        self.assertIn('inbox:c.md', self.read())
        path = os.path.join(self.root, 'inbox', 'c.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text.replace('parent: E-0001\n', 'signature: test_pay\nparent: E-0001\n'))
        self.assertEqual(groom(self.root, True), 0)
        after = self.read()
        self.assertIn('inbox:c.md', after)
        self.assertIn(groom_mod.SETTLED_SUFFIX, after)
        self.assertNotIn('inbox:c.md', ' '.join(
            iid for iid, _l in groom_mod.policy.open_questions(after)))
        self.assertIn('B-0001 Checkout is broken', after)


class MergeGroomTextTests(unittest.TestCase):
    def test_a_line_is_new_per_section_and_none_is_replaced(self):
        existing = ('# Groom d\n\n## Inbox cards to decide\n\n- [ ] F-0001 a — x → answer: yes\n\n'
                    '## Undecided > 3 days\n\n(none)\n')
        text, added = groom_mod.merge_groom_text(existing, {
            'inbox': ['- [ ] F-0001 a — x → answer: ____', '- [ ] F-0002 b — x → answer: ____'],
            'undecided3': ['- [ ] F-0001 a — undecided 4d → answer: ____']})
        self.assertEqual(added, 2)
        self.assertIn('- [ ] F-0001 a — x → answer: yes\n- [ ] F-0002 b', text)
        self.assertNotIn('(none)', text)
        self.assertEqual(groom_mod.merge_groom_text(text, {
            'inbox': ['- [ ] F-0002 b — x → answer: ____']}), (text, 0))


class SameDayAdjudicateTests(unittest.TestCase):
    def setUp(self):
        self.index = fixture_index()
        self.auto = product(approvals={'groom': 'auto'})

    def groom_rows(self, **over):
        state = {'date': '2026-09-22', 'open': ['F-0001', 'F-0003'], 'oldest': 'F-0001',
                 'attempts': 1, 'file': 'groom/2026-09-22.md', 'answers': 'a',
                 'lines': ['- [ ] F-0001 … → answer: ____', '- [ ] F-0003 … → answer: ____']}
        state.update(over)
        return [r for r in rows.candidates(self.index, self.auto, [], groom_state=state)
                if r.kind == rows.GROOM_ADJUDICATE]

    def test_no_second_session_for_the_questions_the_first_was_given(self):
        self.assertEqual(self.groom_rows(new=[]), [])

    def test_a_second_session_for_questions_asked_since(self):
        self.assertEqual(len(self.groom_rows(new=['F-0003'])), 1)
        self.assertEqual(len(self.groom_rows(new=['F-0003'], attempts=5)), 1)

    def test_bounded_by_the_per_day_cap(self):
        self.assertEqual(self.groom_rows(new=['F-0003'], attempts=6), [])
        p = product(approvals={'groom': 'auto'}, groom={'adjudicate_per_day': 2})
        state = {'date': 'd', 'open': ['F-0001'], 'oldest': 'F-0001', 'attempts': 2,
                 'new': ['F-0001'], 'lines': []}
        self.assertEqual([r for r in rows.candidates(self.index, p, [], groom_state=state)
                          if r.kind == rows.GROOM_ADJUDICATE], [])

    def test_the_first_session_of_the_day_needs_nothing_new(self):
        self.assertEqual(len(self.groom_rows(attempts=0, new=[])), 1)


class GroomStateTests(StepsTestCase):
    product_extra = 'steps:\n  batch: off\napprovals:\n  groom: auto\n'

    def write_groom(self, root, lines):
        os.makedirs(os.path.join(root, 'groom'), exist_ok=True)
        with open(os.path.join(root, 'groom', '2026-09-22.md'), 'w') as f:
            f.write(''.join(f'- [ ] {iid} t — undecided 3d → answer: ____\n' for iid in lines))

    def test_attempts_are_sessions_and_new_is_what_the_last_brief_lacked(self):
        root = self.ctx().record_root()
        self.write_groom(root, ['F-0001', 'F-0002'])
        self.session(job='groom-2026-09-22', item='F-0001', kind='groom', account='a',
                     pid=999999, started='t1')
        self.session(job='groom-2026-09-22', ended='t2', end_reason='finished')
        self.session(job='groom-2026-09-22', harvested='abc')
        briefs = os.path.join(env.state_dir(self.product), 'briefs')
        os.makedirs(briefs, exist_ok=True)
        with open(os.path.join(briefs, 'groom-2026-09-22.md'), 'w') as f:
            f.write('## Your job\n\n- [ ] F-0001 t — undecided 3d → answer: ____\n')
        state = step_wave.groom_state(self.product, root)
        self.assertEqual(state['attempts'], 1)
        self.assertEqual(state['new'], ['F-0002'])

    def test_the_record_step_runs_the_groom_every_tick(self):
        seen = []
        ctx = self.ctx()
        with mock.patch.object(step_daily, 'apply_pending_answers', lambda *a, **k: 0), \
                mock.patch.object(step_daily, 'groom_every_tick',
                                  lambda product, root, event=None, out=print:
                                  seen.append((root, event)) or 0):
            self.assertEqual(tick.run_record_step(self.product, ctx=ctx), 0)
        self.assertEqual(seen, [(ctx.record_root(), ctx.event)])

    def test_groom_every_tick_runs_cmd_groom_incrementally_and_is_quiet_when_nothing_changed(self):
        calls = []

        def fake(args, root):
            calls.append(args)
            return 0
        with mock.patch('asf.groom.groom.cmd_groom', fake):
            self.assertEqual(step_daily.groom_every_tick(self.product, self.tmp,
                                                         out=self.lines.append), 0)
        self.assertTrue(calls[0].incremental)
        self.assertFalse(calls[0].apply)
        self.assertEqual(self.lines, [])

    def test_a_staged_answers_file_of_a_later_session_the_same_day_is_carried(self):
        d = os.path.join(env.state_dir(self.product), 'groom')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '2026-01-01.answers.done'), 'w') as f:
            f.write('first session\n')
        wt = os.path.join(self.tmp, 'wt-groom')
        os.makedirs(wt, exist_ok=True)
        with open(os.path.join(wt, '2026-01-01.answers'), 'w') as f:
            f.write('second session\n')
        self.session(job='groom-2026-01-01', kind='groom', item='F-0001', pid=999999,
                     started='t1', worktree=wt, branch='groom/2026-01-01')
        self.session(job='groom-2026-01-01', ended='t2', end_reason='finished')
        moved = step_daily.carry_staged_answers(self.product, out=self.lines.append)
        self.assertEqual(moved, [os.path.join(d, '2026-01-01.answers')])


if __name__ == '__main__':
    unittest.main()
