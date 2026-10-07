"""F-0260 §1 / T-0815: the operator's answer in today's groom file is carried out of the record
checkout and applied by the next tick — and only once.

A bare origin and the operator's record checkout (``backlog_dir``), both temp. The operator fills
an answer slot in ``groom/<date>.md`` and commits nothing; the groom step's
:func:`asf.groom.answers.apply_pending_answers` must copy that line into the state dir as
``<date>.operator.answers`` and apply it on the same tick, never writing in the checkout."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.groom import answers

ID = ['-c', 'user.name=groom', '-c', 'user.email=groom@example.com']
DATE = '2026-10-06'
OPEN = '- [ ] F-11111 a feature — undecided 3d → answer: ____\n'


def git(cwd, *args):
    return subprocess.run(['git', *ID, *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


class OperatorAnswersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-operator-answers-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = os.path.join(self.tmp, 'origin.git')
        self.checkout = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        git(self.tmp, 'clone', '-q', origin, self.checkout)
        git(self.checkout, 'config', 'core.hooksPath', os.devnull)
        git(self.checkout, 'checkout', '-q', '-b', 'main')
        self.write_groom(OPEN)
        git(self.checkout, 'add', '-A')
        git(self.checkout, 'commit', '-q', '-m', 'groom')
        name = self.id().rsplit('.', 1)[-1][:40]
        self.product = env.Product(name, {'backlog_dir': self.checkout,
                                          'approvals': {'groom': 'auto'}})
        self.state = os.path.join(env.state_dir(self.product), 'groom')
        self.addCleanup(shutil.rmtree, env.state_dir(self.product), True)

    # ---- the operator's side ----
    def groom_path(self):
        return os.path.join(self.checkout, 'groom', f'{DATE}.md')

    def write_groom(self, text):
        os.makedirs(os.path.join(self.checkout, 'groom'), exist_ok=True)
        with open(os.path.join(self.checkout, 'groom', f'{DATE}.md'), 'w') as f:
            f.write(text)

    def answer_by_hand(self, answer):
        self.write_groom(OPEN.replace('____', answer))

    def checkout_view(self):
        with open(self.groom_path()) as f:
            text = f.read()
        return (git(self.checkout, 'rev-parse', 'HEAD'),
                git(self.checkout, 'status', '--porcelain'), text)

    # ---- the tick's side ----
    def carry(self):
        out = io.StringIO()
        written = answers.carry_checkout_answers(self.product, out=lambda s: out.write(s + '\n'))
        return written, out.getvalue()

    def state_files(self):
        return sorted(os.listdir(self.state)) if os.path.isdir(self.state) else []

    def write_state(self, name, text):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, name), 'w') as f:
            f.write(text)

    def apply(self):
        applied, lines = [], []

        def fake(args, root):
            applied.append(os.path.basename(args.answers_file))
            os.rename(args.answers_file, args.answers_file + '.done')
            return 0
        with mock.patch('asf.groom.groom.cmd_groom', fake):
            n = answers.apply_pending_answers(self.product, self.tmp, out=lines.append)
        return n, applied, '\n'.join(lines)

    # ---- the cases ----
    def test_an_answer_written_by_hand_in_todays_file_is_carried_and_applied_on_the_same_tick(self):
        self.answer_by_hand('yes')
        n, applied, out = self.apply()
        self.assertEqual(n, 1)
        self.assertEqual(applied, [f'{DATE}.operator.answers'])
        self.assertIn('carried 1 answered line from the record checkout — groom/2026-10-06.md', out)
        with open(os.path.join(self.state, f'{DATE}.operator.answers.done')) as f:
            self.assertEqual(f.read(), OPEN.replace('____', 'yes'))

    def test_the_tick_never_writes_in_the_operator_checkout(self):
        self.answer_by_hand('yes')
        before = self.checkout_view()
        self.apply()
        self.assertEqual(self.checkout_view(), before)

    def test_an_unfilled_slot_is_not_carried(self):
        self.write_groom(OPEN)
        self.assertEqual(self.carry()[0], [])
        self.assertEqual(self.state_files(), [])

    def test_a_line_the_checkouts_head_already_carries_is_not_carried(self):
        self.answer_by_hand('yes')
        git(self.checkout, 'commit', '-q', '-am', 'the operator committed it themselves')
        self.assertEqual(self.carry()[0], [])

    def test_the_same_answer_is_carried_once_and_not_again_every_tick(self):
        """D3: the edit stays in the operator's tree — a second pass must not write or apply."""
        self.answer_by_hand('yes')
        self.assertEqual(self.apply()[0], 1)
        first = self.state_files()
        n, applied, _out = self.apply()
        self.assertEqual((n, applied), (0, []))
        self.assertEqual(self.state_files(), first)

    def test_a_new_answer_beside_an_applied_one_is_carried_alone(self):
        self.answer_by_hand('yes')
        self.apply()
        second = '- [ ] F-11112 another — undecided 3d → answer: no — not now\n'
        with open(self.groom_path(), 'a') as f:
            f.write(second)
        written, _out = self.carry()
        with open(written[0]) as f:
            self.assertEqual(f.read(), second)

    def test_the_record_clone_itself_is_never_read_as_the_checkout(self):
        from asf.tick import shadow
        with mock.patch.object(shadow, 'record_dir', return_value=self.checkout):
            self.answer_by_hand('yes')
            self.assertEqual(self.carry()[0], [])

    def test_the_pending_scanner_sees_a_half_named_answers_file(self):
        """P16: the scanner reads the applier's names, so an appliable file is never invisible."""
        self.write_state(f'{DATE}.operator.answers', OPEN.replace('____', 'yes'))
        self.assertEqual([(os.path.basename(p), d) for p, d in
                          answers.pending_answers_files(self.product)],
                         [(f'{DATE}.operator.answers', DATE)])

    def test_the_date_of_a_half_named_file_is_the_date_not_the_basename(self):
        """P17: the supersede comparison is a date against a date."""
        self.write_state('2026-10-07.answers.done', '')
        self.write_state(f'{DATE}.operator.answers', OPEN.replace('____', 'yes'))
        n, applied, out = self.apply()
        self.assertEqual((n, applied), (0, []))
        self.assertIn('superseded by 2026-10-07', out)

    def test_an_operator_answers_file_and_an_adjudicators_for_one_day_both_apply(self):
        """D9: two carriers, two names, neither overwriting the other."""
        self.write_state(f'{DATE}.answers', '- [ ] E-10001 x → answer: yes\n')
        self.answer_by_hand('yes')
        n, applied, _out = self.apply()
        self.assertEqual(n, 2)
        self.assertEqual(sorted(applied), [f'{DATE}.answers', f'{DATE}.operator.answers'])


if __name__ == '__main__':
    unittest.main()
