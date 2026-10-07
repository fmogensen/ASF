"""tests.test_groom_operator_answers — F-0260 A1: the operator's answer, written by hand into
``groom/<today>.md`` in the record checkout, is carried out of that checkout and applied by the
next tick; the carry reads and never writes there (D1); it carries once, not every tick (D3); and
one answers-file shape serves both the pending scanner and the applier (D9, P16, P17)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.groom import answers
from asf.record.core import today
from tests.test_tick import TickTestCase, _git

F1111_CARD = ('---\nid: F-1111\ntype: feature\ntitle: from the inbox\nparent: E-0001\n'
              'decided: false\n---\n## Description\n\n## Acceptance\n- [ ] \n\n'
              '## History\n- made\n')
E0001_CARD = ('---\nid: E-0001\ntype: epic\ntitle: an epic\ndecided: false\n---\n'
              '## Description\n\n## History\n- made\n')
#: ``_groom``'s ``auto`` branch reads ``index.json`` whether or not anything was applied this
#: tick (it is rebuilt only when something was) — a fresh record needs one seeded, the same shape
#: ``tests/test_tick_steps.py``'s ``INDEX`` constant uses.
INDEX = {'generated': '', 'items': {
    'F-1111': {'id': 'F-1111', 'type': 'feature', 'title': 'from the inbox', 'folder': 'features',
               'state': 'New', 'decided': False},
    'E-0001': {'id': 'E-0001', 'type': 'epic', 'title': 'an epic', 'folder': 'epics',
               'state': 'New', 'decided': False},
}}

_FOLDER = {'F': 'features', 'E': 'epics', 'B': 'bugs'}


class OperatorAnswersFixture(TickTestCase):
    """A bare origin seeded with F-1111 (a Feature) and E-0001 (an Epic), the operator's own
    checkout cloned from it (``self.operator``, ``TickTestCase``'s ``backlog_dir``) and the
    tick's own clone — the shape ``tests/test_operator_checkout_sync.py:25-49`` already builds,
    plus the two cards an answer needs to land on."""

    product_yaml = 'conventions:\n  default_bug_epic: E-0001\napprovals:\n  groom: auto\n'

    @classmethod
    def build_repos(cls, tmp):
        origin = os.path.join(tmp, 'origin.git')
        seed = os.path.join(tmp, 'seed')
        _git(['init', '-q', '--bare', '-b', 'main', origin])
        _git(['clone', '-q', origin, seed])
        _git(['config', 'user.email', 'seed@example.com'], seed)
        _git(['config', 'user.name', 'seed'], seed)
        os.makedirs(os.path.join(seed, 'features'))
        os.makedirs(os.path.join(seed, 'epics'))
        with open(os.path.join(seed, 'features', 'F-1111.md'), 'w') as f:
            f.write(F1111_CARD)
        with open(os.path.join(seed, 'epics', 'E-0001.md'), 'w') as f:
            f.write(E0001_CARD)
        with open(os.path.join(seed, 'index.json'), 'w') as f:
            json.dump(INDEX, f)
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'seed'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)
        _git(['clone', '-q', origin, os.path.join(tmp, 'operator')])

    def setUp(self):
        super().setUp()
        self.product = env.load_product('sample')

    def today(self):
        return today()

    def run_tick(self, **kw):
        rc, out = super().run_tick(**kw)
        self._last_tick_out = out
        return rc, out

    def tick_output(self):
        return self._last_tick_out

    def _groom_path(self, date=None):
        return os.path.join(self.operator, 'groom', f'{date or self.today()}.md')

    def write_groom_line(self, line, date=None):
        path = self._groom_path(date)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line.rstrip('\n') + '\n')

    def answer_by_hand(self, iid, answer, date=None):
        self.write_groom_line(f'- [ ] {iid} x — undecided 3d → answer: {answer}', date)

    def answer_by_hand_inbox(self, name, answer, date=None):
        self._push_to_origin(f'inbox/{name}', 'the sync stalls\n\nSeen on the trunk.\n',
                              'seed an intake card')
        self.write_groom_line(f'- [ ] inbox:{name} x — q → answer: {answer}', date)

    def commit_in_checkout(self, path):
        _git(['add', path], self.operator)
        _git(['commit', '-q', '-m', f'operator: {path}'], self.operator)

    def _push_to_origin(self, rel_path, content, message):
        scratch = tempfile.mkdtemp(prefix='seed_')
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        _git(['clone', '-q', self.origin, scratch])
        _git(['config', 'user.email', 's@example.com'], scratch)
        _git(['config', 'user.name', 's'], scratch)
        full = os.path.join(scratch, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(content)
        _git(['add', '-A'], scratch)
        _git(['commit', '-q', '-m', message], scratch)
        _git(['push', '-q', 'origin', 'HEAD:main'], scratch)

    def stage_adjudicator_answers(self, iid, answer, date=None):
        d = os.path.join(env.state_dir(self.product), 'groom')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{date or self.today()}.answers')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'- [ ] {iid} x — undecided 3d → answer: {answer}\n')

    def write_state_answers(self, name, text):
        d = os.path.join(env.state_dir(self.product), 'groom')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def state_groom_files(self):
        d = os.path.join(env.state_dir(self.product), 'groom')
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def pending_operator_answers(self):
        return [n for n in self.state_groom_files() if n.endswith('.operator.answers')]

    def origin_card(self, iid):
        out = subprocess.run(['git', 'show', f'main:{_FOLDER[iid[0]]}/{iid}.md'],
                             cwd=self.origin, capture_output=True, text=True)
        return out.stdout if out.returncode == 0 else ''

    def origin_has_a_bug_titled(self, title):
        names = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'main', 'bugs'],
                               cwd=self.origin, capture_output=True, text=True).stdout.splitlines()
        return any(f'title: {title}' in self.origin_card(os.path.basename(n)[:-3])
                   for n in names)

    def checkout_head(self):
        return _git(['rev-parse', 'HEAD'], self.operator)

    def checkout_status(self):
        return _git(['status', '--porcelain'], self.operator)

    def checkout_groom_text(self, date=None):
        path = self._groom_path(date)
        if not os.path.isfile(path):
            return None
        with open(path, encoding='utf-8') as f:
            return f.read()


class GroomOperatorAnswersTests(OperatorAnswersFixture):
    def test_an_answer_written_by_hand_in_todays_file_is_applied_by_the_next_tick(self):
        """The card's first sentence. The operator's checkout holds the answer and nothing else
        does; the tick reads it out, applies it in its clone and pushes the card."""
        self.answer_by_hand('F-1111', 'yes')          # groom/<today>.md in the operator checkout
        self.assertNotIn('decided: true', self.origin_card('F-1111'))
        rc, out = self.run_tick(steps='record,groom')
        self.assertEqual(rc, 0)
        self.assertIn('decided: true', self.origin_card('F-1111'))
        self.assertIn('carried 1 answered line from the record checkout', out)

    def test_the_tick_never_writes_in_the_operator_checkout(self):
        """D1: the carry reads. The checkout's HEAD, its index and its working tree are untouched."""
        self.answer_by_hand('F-1111', 'yes')
        before = (self.checkout_head(), self.checkout_status(), self.checkout_groom_text())
        self.run_tick(steps='record,groom')
        self.assertEqual((self.checkout_head(), self.checkout_status(),
                          self.checkout_groom_text()), before)

    def test_an_unfilled_slot_is_not_carried(self):
        self.write_groom_line('- [ ] F-1111 x — undecided 3d → answer: ____')
        self.run_tick(steps='record,groom')
        self.assertEqual(self.pending_operator_answers(), [])
        self.assertNotIn('decided: true', self.origin_card('F-1111'))

    def test_a_line_the_checkouts_head_already_carries_is_not_carried(self):
        """D2: the comparison is HEAD, so only the uncommitted edit is the operator's answer."""
        self.answer_by_hand('F-1111', 'yes')
        self.commit_in_checkout('groom')              # the operator committed it themselves
        self.run_tick(steps='record,groom')
        self.assertEqual(self.pending_operator_answers(), [])
        self.assertNotIn('decided: true', self.origin_card('F-1111'))

    def test_the_same_answer_is_carried_once_and_not_again_every_tick(self):
        """D3: the edit stays in the tree (C2) — the carry must not write a file per tick."""
        self.answer_by_hand('F-1111', 'yes')
        self.run_tick(steps='record,groom')
        first = self.state_groom_files()
        _rc, out = self.run_tick(steps='record,groom')
        self.assertEqual(self.state_groom_files(), first)
        self.assertNotIn('carried', out)

    def test_an_inbox_answer_in_todays_file_types_the_intake_card(self):
        """The row's own case: an `inbox:<name>` line, answered by hand, applied by the tick."""
        self.answer_by_hand_inbox('p1-e2e-on-main.md', 'bug the sync stalls')
        self.run_tick(steps='record,groom')
        self.assertTrue(self.origin_has_a_bug_titled('the sync stalls'))

    def test_an_operator_answers_file_and_an_adjudicators_for_one_day_both_apply(self):
        """D9: two carriers, two names, neither overwriting the other."""
        self.stage_adjudicator_answers('E-0001', 'yes')          # <date>.answers
        self.answer_by_hand('F-1111', 'yes')                     # <date>.operator.answers
        self.run_tick(steps='record,groom')
        self.assertIn('decided: true', self.origin_card('E-0001'))
        self.assertIn('decided: true', self.origin_card('F-1111'))

    def test_the_pending_scanner_sees_a_half_named_answers_file(self):
        """P16: the two regexes agreed, so an appliable file is no longer invisible."""
        self.write_state_answers('2026-10-06.operator.answers', '- [ ] F-1111 x → answer: yes\n')
        self.assertEqual([os.path.basename(p) for p, _d in
                          answers.pending_answers_files(self.product)],
                         ['2026-10-06.operator.answers'])

    def test_the_date_of_a_half_named_file_is_the_date_not_the_basename(self):
        """P17: `[:-len('.answers')]` made the date `2026-10-06.operator`, so the supersede
        comparison compared a date against a date-and-a-word."""
        self.write_state_answers('2026-10-07.answers.done', '')
        self.write_state_answers('2026-10-06.operator.answers', '- [ ] F-1111 x → answer: yes\n')
        self.run_tick(steps='record,groom')
        self.assertIn('superseded by 2026-10-07', self.tick_output())


if __name__ == '__main__':
    unittest.main()
