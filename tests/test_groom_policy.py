"""asf.groom.policy — the gate (T1), and the open-question grammar it reads (PD4).

The policy pass itself, its four rules, the suppression pass, the answers file and the digest
all arrive in later Tasks of F-0085's plan; this file gains their test classes as those Tasks
land. For now: the single gate, ``open_questions``, and the threshold readers.
"""
import os
import shutil
import unittest

from asf.env import Product
from asf.groom import policy
from asf.record.core import today
from tests.test_groom import make_repo, run, write_item


def product(approvals=None, groom=None):
    data = {}
    if approvals is not None:
        data['approvals'] = approvals
    if groom is not None:
        data['groom'] = groom
    return Product('sample', data)


class GateTests(unittest.TestCase):
    """T1: a product without ``approvals.groom: auto`` behaves exactly as today."""

    def test_auto_turns_the_gate_on(self):
        self.assertTrue(policy.groom_auto(product({'groom': 'auto'})))

    def test_auto_is_case_insensitive(self):
        self.assertTrue(policy.groom_auto(product({'groom': 'AUTO'})))

    def test_unset_is_off(self):
        self.assertFalse(policy.groom_auto(product({})))

    def test_no_approvals_at_all_is_off(self):
        self.assertFalse(policy.groom_auto(product()))

    def test_the_word_groom_is_off(self):
        self.assertFalse(policy.groom_auto(product({'groom': 'groom'})))

    def test_human_now_is_off(self):
        self.assertFalse(policy.groom_auto(product({'groom': 'human-now'})))

    def test_no_product_is_off(self):
        self.assertFalse(policy.groom_auto(None))

    def test_a_gate_off_cmd_groom_writes_the_same_file_as_before(self):
        """Task 1 wires only the gate; the policy pass itself is a later Task. This pins
        today's plain ``asf groom`` output as the baseline the gate must not disturb while it
        is off."""
        root = make_repo()
        try:
            write_item(root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
            write_item(root, 'F-0001', 'feature', 'Lonely feature', parent='E-0009',
                      typed_lines=['decided: true'])
            run(['index'], root)
            r = run(['groom'], root)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(root, 'groom', today() + '.md')) as f:
                text = f.read()
            self.assertIn('F-0001 Lonely feature — no Stories', text)
            self.assertNotIn('controller:', text)
            self.assertNotIn('by rule', r.stdout)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class OpenQuestionsTests(unittest.TestCase):
    """PD4: a ``- [ ] … → answer: ____`` line is open; nothing else is."""

    def test_open_lines_in_file_order(self):
        text = ("# Groom 2026-09-22\n\n## Undecided > 3 days\n\n"
               "- [ ] F-0001 Some idea — undecided 4d → answer: ____\n"
               "- [ ] F-0002 Another — undecided 5d → answer: ____\n")
        self.assertEqual(policy.open_questions(text),
                         [('F-0001', "- [ ] F-0001 Some idea — undecided 4d → answer: ____"),
                          ('F-0002', "- [ ] F-0002 Another — undecided 5d → answer: ____")])

    def test_an_answered_line_is_not_open(self):
        text = "- [ ] F-0001 Some idea — undecided 4d → answer: yes\n"
        self.assertEqual(policy.open_questions(text), [])

    def test_a_spoken_for_line_is_not_open(self):
        text = "- [x] F-0001 Some idea — no Stories → answer: (spoken for: CARD → SPEC)\n"
        self.assertEqual(policy.open_questions(text), [])

    def test_a_barred_line_is_not_open(self):
        text = "- [ ] E-0004 New goal — reads as a new Epic → answer: ____ (barred: approvals.new_epic)\n"
        self.assertEqual(policy.open_questions(text), [])


class ThresholdTests(unittest.TestCase):
    """D11's defaults, and a product yaml's own values overriding them."""

    def test_defaults(self):
        p = product()
        self.assertEqual(policy.duplicate_overlap(p), 0.95)
        self.assertEqual(policy.recurring_bug_count(p), 2)
        self.assertEqual(policy.adjudicate_attempts(p), 2)

    def test_overrides(self):
        p = product(groom={'duplicate_overlap': 0.8, 'recurring_bug_count': 5,
                           'adjudicate_attempts': 4})
        self.assertEqual(policy.duplicate_overlap(p), 0.8)
        self.assertEqual(policy.recurring_bug_count(p), 5)
        self.assertEqual(policy.adjudicate_attempts(p), 4)

    def test_an_invalid_override_falls_back_to_the_default(self):
        p = product(groom={'adjudicate_attempts': 0})
        self.assertEqual(policy.adjudicate_attempts(p), 2)

    def test_policy_on_default_is_on(self):
        self.assertTrue(policy.policy_on(product(), 'close_exact_duplicate'))

    def test_policy_off_skips_it(self):
        p = product(groom={'policies': {'close_exact_duplicate': 'off'}})
        self.assertFalse(policy.policy_on(p, 'close_exact_duplicate'))
        self.assertTrue(policy.policy_on(p, 'decide_recurring_bug'))


if __name__ == '__main__':
    unittest.main()
