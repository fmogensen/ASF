import dataclasses
import unittest

from asf import budget
from asf import tokens
from asf.conventions import Conventions
from asf.workers import runtime as runtime_mod


class ItemBudgetTests(unittest.TestCase):
    """F-0092 §2.2, T2: the verdict, both measures, and the line."""

    def test_of_resolves_default_then_product_then_item(self):
        default = budget.of(Conventions(), {})
        self.assertEqual((default.sessions, default.usd), (3, 10))

        product = budget.of(Conventions.from_mapping({'budget': {'sessions': 5, 'usd': 20}}), {})
        self.assertEqual((product.sessions, product.usd), (5, 20))

        item = budget.of(Conventions(), {'budget_sessions': 12, 'budget_usd': 25})
        self.assertEqual((item.sessions, item.usd), (12, 25))
        self.assertEqual(item.source, 'item')

    def test_each_item_override_wins_on_its_own(self):
        sessions_only = budget.of(Conventions(), {'budget_sessions': 12})
        self.assertEqual((sessions_only.sessions, sessions_only.usd), (12, 10))
        usd_only = budget.of(Conventions(), {'budget_usd': 25})
        self.assertEqual((usd_only.sessions, usd_only.usd), (3, 25))

    def test_an_epic_gets_a_bare_budget_whatever_its_budget_usd(self):
        b = budget.of(Conventions(), {'type': 'epic', 'budget_usd': 500})
        self.assertEqual(b, budget.Budget())

    def test_over_on_sessions(self):
        s = budget.spent(Conventions(), {'cost': {'sessions': 9, 'usd': 13.53}})
        self.assertTrue(s.over)
        self.assertEqual(s.measure, 'sessions')

    def test_over_on_usd(self):
        s = budget.spent(Conventions(), {'cost': {'sessions': 2, 'usd': 16.59}})
        self.assertTrue(s.over)
        self.assertEqual(s.measure, 'usd')

    def test_over_at_exactly_the_cap(self):
        # >= is over: the third session is the last one the budget buys.
        s = budget.spent(Conventions(), {'cost': {'sessions': 3, 'usd': 1}})
        self.assertTrue(s.over)
        self.assertEqual(s.measure, 'sessions')

    def test_an_unmeasured_dollar_is_not_a_spent_one(self):
        s = budget.spent(Conventions(), {'cost': {'sessions': 2, 'usd': None}})
        self.assertFalse(s.over)

    def test_a_card_with_no_cost_block_is_not_over(self):
        s = budget.spent(Conventions(), {})
        self.assertFalse(s.over)

    def test_sessions_off_leaves_only_the_money_measure(self):
        c = Conventions.from_mapping({'budget': {'sessions': 'off'}})
        under = budget.spent(c, {'cost': {'sessions': 1000, 'usd': 1}})
        self.assertFalse(under.over)
        over = budget.spent(c, {'cost': {'sessions': 1000, 'usd': 16.59}})
        self.assertTrue(over.over)
        self.assertEqual(over.measure, 'usd')

    def test_the_line_format(self):
        s = budget.spent(Conventions(), {'cost': {'sessions': 9, 'usd': 13.53}})
        self.assertEqual(budget.line('T-0021', s),
                         'OVER BUDGET T-0021 — 9/3 sessions, $13.53/$10')

    def test_the_line_prints_a_dash_for_an_off_cap(self):
        c = Conventions.from_mapping({'budget': {'usd': 'off'}})
        s = budget.spent(c, {'cost': {'sessions': 9, 'usd': 16.59}})
        self.assertEqual(budget.line('T-0021', s),
                         'OVER BUDGET T-0021 — 9/3 sessions, $16.59/$—')


class EpicBudgetTests(unittest.TestCase):
    """F-0052 §2.1, T1: the Epic verdict, still a leaf."""

    def test_over_at_512_40_of_500(self):
        s = budget.epic_spend('E-0001', 512.40, 500)
        self.assertTrue(s.over)
        self.assertEqual(budget.epic_line(s),
                          'OVER BUDGET E-0001 — $512.40/$500 spent, new work held')

    def test_not_over_just_under(self):
        self.assertFalse(budget.epic_spend('E-0001', 499.99, 500).over)

    def test_over_at_exactly_the_cap(self):
        self.assertTrue(budget.epic_spend('E-0001', 500, 500).over)

    def test_never_over_and_never_raising(self):
        for spend, cap in [(None, 500), (512.40, None), (None, None),
                            ('lots', 500), (512.40, 'plenty')]:
            with self.subTest(spend=spend, cap=cap):
                self.assertFalse(budget.epic_spend('E-0001', spend, cap).over)

    def test_money_prints_a_whole_and_a_fractional_dollar(self):
        self.assertEqual(budget.epic_line(budget.epic_spend('E-0001', 10, 10)),
                          'OVER BUDGET E-0001 — $10/$10 spent, new work held')
        self.assertEqual(budget.epic_line(budget.epic_spend('E-0001', 512.4, 512.4)),
                          'OVER BUDGET E-0001 — $512.40/$512.40 spent, new work held')

    def test_epic_spend_is_frozen(self):
        s = budget.epic_spend('E-0001', 512.40, 500)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.usd = 0

    def test_an_epic_still_has_no_item_budget(self):
        b = budget.of(Conventions(), {'type': 'epic', 'budget_usd': 500})
        self.assertEqual(b, budget.Budget())

    def test_epic_over_and_epic_reason_texts(self):
        s = budget.epic_spend('E-0001', 512.40, 500)
        self.assertEqual(budget.epic_over(s), 'E-0001 over $500')
        self.assertEqual(budget.epic_reason(s),
                          'E-0001 over budget: $512.40/$500 spent — '
                          'raise it, reshape it or close its work')


class RunCapTests(unittest.TestCase):
    """F-0092 §2.2/§2.8, T7's pure half: the run's wall clock and its turns."""

    def test_214_minutes_over_180(self):
        self.assertEqual(budget.run_over(214, 0, (180, 600)), ('run_minutes', 214, 180))

    def test_180_exactly_is_not_over(self):
        self.assertIsNone(budget.run_over(180, 0, (180, 600)))

    def test_601_turns_over_600(self):
        self.assertEqual(budget.run_over(0, 601, (180, 600)), ('run_turns', 601, 600))

    def test_the_wall_clock_is_named_first_when_both_are_over(self):
        self.assertEqual(budget.run_over(214, 601, (180, 600))[0], 'run_minutes')

    def test_a_none_elapsed_is_judged_on_turns_alone(self):
        self.assertEqual(budget.run_over(None, 601, (180, 600)), ('run_turns', 601, 600))
        self.assertIsNone(budget.run_over(None, 0, (180, 600)))

    def test_off_on_both_is_never_over(self):
        self.assertIsNone(budget.run_over(10000, 10000, (None, None)))

    def test_run_caps_reads_the_product(self):
        self.assertEqual(budget.run_caps(Conventions()), (180, 600))
        c = Conventions.from_mapping({'budget': {'run_minutes': 'off'}})
        self.assertEqual(budget.run_caps(c), (None, 600))

    def test_the_run_cap_text(self):
        self.assertEqual(budget.run_cap_text('code', 'run_minutes', 240, 180),
                         'run cap: run_minutes 240 over the 180 cap for a code job')

    def test_the_asf_run_cap_objects_shape(self):
        rec = budget.run_cap_result('code', 'run_minutes', 240, 180, '2026-09-28T04:12:00Z')
        self.assertTrue(rec['is_error'])
        self.assertEqual(rec['result'], 'run cap: run_minutes 240 over the 180 cap for a code '
                                        'job — stopped by the factory')
        self.assertEqual(rec['asf']['run_cap'],
                         {'kind': 'code', 'measure': 'run_minutes', 'value': 240, 'limit': 180,
                          'at': '2026-09-28T04:12:00Z'})

    def test_failure_reason_of_a_run_cap_record_is_run_cap(self):
        rec = budget.run_cap_result('code', 'run_minutes', 240, 180, '2026-09-28T04:12:00Z')
        self.assertEqual(runtime_mod.failure_reason(rec), budget.RUN_CAP)

    def test_failure_reason_of_a_token_cap_record_is_still_token_cap(self):
        rec = tokens.cap_result('code', 'input', 9_000_000, 8_000_000,
                                {'input': 9_000_000, 'output': None, 'cache_read': None,
                                 'cache_write': None}, '2026-09-28T04:12:00Z')
        self.assertEqual(runtime_mod.failure_reason(rec), tokens.TOKEN_CAP)


if __name__ == '__main__':
    unittest.main()
