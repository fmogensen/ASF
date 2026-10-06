"""asf.workers.loops — the loop guard's two rules (same report, daily cap), their flag reader,
and a replay pin over an anonymised slice of the sessions a relaunch cap on head and card alone
still let through (F-0101 §G3)."""
import json
import os
import unittest
from unittest import mock

from asf.env import Product
from asf.workers import loops

HERE = os.path.dirname(os.path.abspath(__file__))
REPLAY_SESSIONS = os.path.join(HERE, 'fixtures', 'replay', 'loop_sessions.jsonl')


def _load_sessions(path=REPLAY_SESSIONS):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


class ReportKeyTests(unittest.TestCase):
    def test_masks_shas_and_folds_whitespace(self):
        a = 'REPORT\nstatus: done\ncommits: abcdef1234567\n'
        b = 'REPORT\nstatus: done\ncommits:    89abcdef01234  \n'
        self.assertEqual(loops.report_key(a), loops.report_key(b))

    def test_no_report_block_is_empty(self):
        self.assertEqual(loops.report_key('no report here'), '')

    def test_only_the_last_report_block_counts(self):
        text = 'REPORT\nstatus: partial\nleft out: x\n```\nmore text\nREPORT\nstatus: done\n'
        self.assertNotEqual(loops.report_key(text), '')

    def test_different_bodies_differ(self):
        a = loops.report_key('REPORT\nstatus: done\nleft out: none\n')
        b = loops.report_key('REPORT\nstatus: done\nleft out: a person must publish it\n')
        self.assertNotEqual(a, b)


class JudgeTests(unittest.TestCase):
    NOW = 1_800_000_000  # an arbitrary fixed "now" in epoch seconds

    def _run(self, started, report_key='rk', card='card1', ended=True):
        return {'started': started, 'ended': started if ended else '', 'card_digest': card,
                'report_key': report_key}

    def test_no_runs_is_never_refused(self):
        self.assertIsNone(loops.judge([], card='card1', now=self.NOW))

    def test_same_report_twice_on_the_same_card_refuses(self):
        runs = [self._run('2026-09-29T06:00:00Z'), self._run('2026-09-29T06:30:00Z')]
        got = loops.judge(runs, card='card1', now=self.NOW)
        self.assertEqual(got[0], loops.SAME_REPORT_RULE)

    def test_a_different_report_does_not_refuse(self):
        runs = [self._run('2026-09-29T06:00:00Z', report_key='rk1'),
               self._run('2026-09-29T06:30:00Z', report_key='rk2')]
        self.assertIsNone(loops.judge(runs, card='card1', now=self.NOW))

    def test_an_empty_report_key_never_refuses(self):
        runs = [self._run('2026-09-29T06:00:00Z', report_key=''),
               self._run('2026-09-29T06:30:00Z', report_key='')]
        self.assertIsNone(loops.judge(runs, card='card1', now=self.NOW))

    def test_a_changed_card_does_not_refuse(self):
        runs = [self._run('2026-09-29T06:00:00Z', card='card1'),
               self._run('2026-09-29T06:30:00Z', card='card1')]
        self.assertIsNone(loops.judge(runs, card='card2', now=self.NOW))

    def test_same_report_off_disables_the_rule(self):
        runs = [self._run('2026-09-29T06:00:00Z'), self._run('2026-09-29T06:30:00Z')]
        self.assertIsNone(loops.judge(runs, card='card1', now=self.NOW, same_report=None))

    def test_daily_cap_at_the_threshold_refuses(self):
        now = loops._ts('2026-09-30T00:00:00Z')
        runs = [self._run(f'2026-09-29T0{i}:00:00Z', report_key=f'rk{i}') for i in range(1, 7)]
        got = loops.judge(runs, card='card1', now=now, same_report=None, daily_cap=6)
        self.assertEqual(got[0], loops.DAILY_RULE)

    def test_daily_cap_under_the_threshold_does_not_refuse(self):
        now = loops._ts('2026-09-30T00:00:00Z')
        runs = [self._run(f'2026-09-29T0{i}:00:00Z', report_key=f'rk{i}') for i in range(5)]
        self.assertIsNone(loops.judge(runs, card='card1', now=now, same_report=None, daily_cap=6))

    def test_a_launch_older_than_24h_does_not_count_toward_the_cap(self):
        now = loops._ts('2026-09-30T00:00:00Z')
        runs = [self._run('2026-09-27T00:00:00Z', report_key='old')] + \
            [self._run(f'2026-09-29T0{i}:00:00Z', report_key=f'rk{i}') for i in range(5)]
        self.assertIsNone(loops.judge(runs, card='card1', now=now, same_report=None, daily_cap=6))

    def test_daily_cap_off_disables_the_rule(self):
        now = loops._ts('2026-09-30T00:00:00Z')
        runs = [self._run(f'2026-09-29T0{i}:00:00Z', report_key=f'rk{i}') for i in range(6)]
        self.assertIsNone(loops.judge(runs, card='card1', now=now, same_report=None,
                                      daily_cap=None))


class FlagReaderTests(unittest.TestCase):
    def test_settings_defaults(self):
        self.assertEqual(loops.settings(Product('p', {})),
                         {'same_report': 2, 'daily_cap': 6})

    def test_settings_overrides_and_off(self):
        p = Product('p', {'conventions': {'flags': {
            'relaunch_same_report': 3, 'relaunch_daily_cap': 'off'}}})
        self.assertEqual(loops.settings(p), {'same_report': 3, 'daily_cap': None})

    def test_an_invalid_value_falls_back_to_the_default(self):
        p = Product('p', {'conventions': {'flags': {'relaunch_daily_cap': 'bogus'}}})
        self.assertEqual(loops.settings(p)['daily_cap'], loops.DAILY_CAP)

    def test_no_product_is_every_default(self):
        self.assertEqual(loops.settings(None), {'same_report': 2, 'daily_cap': 6})


class ReplayPinTests(unittest.TestCase):
    """A replay of the anonymised 2026-09-29T06:00Z–2026-10-06T06:10Z slice
    (``tests/fixtures/replay/loop_sessions.jsonl``): job ``a`` is the "13 sessions on 7 heads,
    one report" loop (F-0101's own example), job ``b`` is a loop the same-report rule misses
    (a fresh report every launch) that the daily cap still catches."""

    def setUp(self):
        self.sessions = _load_sessions()

    def test_fixture_shape(self):
        self.assertEqual(len(self.sessions), 22)
        self.assertEqual({s['item'] for s in self.sessions}, {'a-0042', 'b-0042'})

    def test_same_report_rule_avoids_eleven_of_thirteen_launches(self):
        out = loops.replay(self.sessions)
        a_rules = [rule for s, rule in out if s['item'] == 'a-0042']
        self.assertEqual(len(a_rules), 11)
        self.assertTrue(all(r == loops.SAME_REPORT_RULE for r in a_rules))

    def test_daily_cap_rule_avoids_three_of_nine_launches(self):
        out = loops.replay(self.sessions)
        b_rules = [rule for s, rule in out if s['item'] == 'b-0042']
        self.assertEqual(len(b_rules), 3)
        self.assertTrue(all(r == loops.DAILY_RULE for r in b_rules))

    def test_fourteen_launches_avoided_in_all(self):
        self.assertEqual(len(loops.replay(self.sessions)), 14)

    def test_turning_off_both_rules_avoids_nothing(self):
        out = loops.replay(self.sessions, same_report=None, daily_cap=None)
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
