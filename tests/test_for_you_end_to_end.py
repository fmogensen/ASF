"""T-0772 — the card's acceptance, through the command an operator runs: the customer complaint
the file is named for, ``379 for you`` on a first install, driven end to end through a gated
``asf groom`` over the 379-shaped fixture, asserting the record after the run and not only the
rendered line. ``tests/test_groom_for_you.py`` runs the same fixture's shape at the seam,
calling :func:`asf.groom.digest.render_digest` in-process; this file runs it through the CLI the
operator actually types, and reads the cards and the digest file it writes to disk.
"""
import os
import unittest

from tests.test_groom import run, write_item
from tests.test_groom_policy import GroomAutoTestCase

DATE = '2026-09-24'
DATE2 = '2026-09-25'
#: An item whose ``stage_since`` sits on ``DATE`` — not the real wall clock ``undecided3``/
#: ``undecided14`` measure age against (``tests.test_groom_policy``'s ``_fresh_machine_lines``
#: docstring) — so it does not also surface there as a second, unrelated open question.
FRESH = ['state: New', f'stage_since: {DATE}T00:00:00Z', f'updated: {DATE}T00:00:00Z']


class ForYouEndToEndTests(GroomAutoTestCase):
    def fixture(self):
        write_item(self.root, 'F-0001', 'feature', 'Parity with the old app', parent='E-0009',
                   typed_lines=['decided: true'],
                   machine_lines=['state: Active', 'stage: plan-approved'] + FRESH[1:])
        for sid, title in (('S-0001', 'Stripe payment tokens are refunded'),
                           ('S-0002', 'Privacy consent and GDPR data exports'),
                           ('S-0003', 'OAuth credentials rotate in production')):
            write_item(self.root, sid, 'story', title, parent='F-0001',
                       typed_lines=['decided: true'], machine_lines=FRESH)
        tasks = (('T-0001', 'Parity: the sign-up form shows the free plan', 'src/signup.py'),
                 ('T-0002', 'Parity: the sign-up form shows the paid plan', 'src/paid.py'),
                 ('T-0003', 'Parity: the sign-up form shows the team plan', 'src/team.py'),
                 ('T-0004', 'Parity: the sign-up form shows the free plan', 'src/signup.py'))
        for tid, title, writes in tasks:
            write_item(self.root, tid, 'task', title, parent='F-0001',
                       typed_lines=[f'decided: {"false" if tid == "T-0004" else "true"}',
                                   f'writes: [{writes}]'], machine_lines=FRESH)
        write_item(self.root, 'F-0002', 'feature', 'Sidebar rows show unread counts',
                   parent='E-0009', typed_lines=['decided: true'], machine_lines=FRESH)
        run(['index'], self.root)

    def _digest(self, date):
        with open(os.path.join(self.root, 'groom', f'{date}-digest.md'), encoding='utf-8') as f:
            return f.read()

    def test_the_ordinary_day_is_zero_for_you(self):
        # structural:ask keeps the fixture's no-Stories/no-Tasks gaps as questions, so this
        # still exercises the full housekeeping/spoken-for pipeline (G1's default, report,
        # drops them before any of this runs — tested on its own in test_groom.py)
        self.write_product(approvals={'groom': 'auto', 'spend_money': 'human-now',
                                      'touch_security': 'human-now', 'touch_legal': 'human-now'},
                           flags={'groom': {'structural': 'ask'}})
        self.fixture()
        r = self.run_groom(['--date', DATE])
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._digest(DATE)
        self.assertIn('· 0 for you', text, text)
        for_you = text[text.index('## For you'):]
        self.assertTrue(for_you.startswith('## For you\n(none)'), for_you)
        self.assertNotIn(' housekeeping', text.split('\n\n')[1], text)

    def test_the_duplicate_is_removed_in_the_record_not_merely_flagged(self):
        self.write_product(approvals={'groom': 'auto', 'spend_money': 'human-now',
                                      'touch_security': 'human-now', 'touch_legal': 'human-now'},
                           flags={'groom': {'structural': 'ask'}})
        self.fixture()
        r = self.run_groom(['--date', DATE])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.root, 'tasks', 'T-0004.md'), encoding='utf-8') as f:
            t4 = f.read()
        self.assertIn('removed:', t4, t4)
        self.assertRegex(t4, r'- \d{4}-\d{2}-\d{2} groom: removed → .*\(controller, close_duplicate_task\)')
        for tid in ('T-0002', 'T-0003'):
            with open(os.path.join(self.root, 'tasks', f'{tid}.md'), encoding='utf-8') as f:
                self.assertNotIn('removed:', f.read())

    def test_no_groom_line_reads_barred(self):
        self.write_product(approvals={'groom': 'auto', 'spend_money': 'human-now',
                                      'touch_security': 'human-now', 'touch_legal': 'human-now'},
                           flags={'groom': {'structural': 'ask'}})
        self.fixture()
        r = self.run_groom(['--date', DATE])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.root, 'groom', f'{DATE}.md'), encoding='utf-8') as f:
            groom_text = f.read()
        self.assertNotIn('barred', groom_text)

    def test_the_housekeeping_is_spoken_for_under_the_cap(self):
        self.write_product(approvals={'groom': 'auto', 'spend_money': 'human-now',
                                      'touch_security': 'human-now', 'touch_legal': 'human-now'},
                           flags={'groom': {'structural': 'ask'}})
        self.fixture()
        r = self.run_groom(['--date', DATE])
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._digest(DATE)
        spoken_for = text[text.index('## Spoken for'):text.index('## For you')]
        self.assertIn('F-0002 no Stories', spoken_for, spoken_for)
        self.assertIn('(spoken for: GROOM → ADJUDICATE)', spoken_for, spoken_for)
        self.assertNotIn('## Housekeeping', text, text)

    def test_past_the_cap_the_housekeeping_has_its_own_section(self):
        self.write_product(approvals={'groom': 'auto', 'spend_money': 'human-now',
                                      'touch_security': 'human-now', 'touch_legal': 'human-now'},
                           groom_cfg={'adjudicate_attempts': 1},
                           flags={'groom': {'structural': 'ask'}})
        self.fixture()
        r1 = self.run_groom(['--date', DATE])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        sessions_path = os.path.join(self.asf_home, 'state', 'sample', 'sessions.jsonl')
        os.makedirs(os.path.dirname(sessions_path), exist_ok=True)
        with open(sessions_path, 'a', encoding='utf-8') as f:
            f.write('{"job": "groom-%s", "kind": "groom", "started": "2026-09-25T00:00:00Z", '
                   '"pid": 999999}\n' % DATE2)
        r2 = self.run_groom(['--date', DATE2])
        self.assertEqual(r2.returncode, 0, r2.stderr)
        text = self._digest(DATE2)
        self.assertIn('· 0 for you', text, text)
        self.assertTrue(text.split('\n\n')[1].rstrip().endswith('· 3 housekeeping'), text)
        housekeeping = text[text.index('## Housekeeping'):]
        for sid in ('S-0001', 'S-0002', 'S-0003'):
            self.assertIn(sid, housekeeping, housekeeping)
        self.assertEqual(housekeeping.count('(Stories without Tasks after plan-approved)'), 3,
                         housekeeping)
        for_you = text[text.index('## For you'):text.index('## Housekeeping')]
        self.assertTrue(for_you.startswith('## For you\n(none)'), for_you)
        spoken_for = text[text.index('## Spoken for'):text.index('## For you')]
        self.assertIn('F-0002 no Stories', spoken_for, spoken_for)
        self.assertNotIn('F-0002', text[text.index('## Housekeeping'):], text)

    def test_a_real_human_now_action_still_reaches_for_you(self):
        self.write_product(approvals={'groom': 'auto', 'new_epic': 'human-now'})
        write_item(self.root, 'E-0010', 'epic', 'A new goal for the year',
                  machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                                'updated: 2026-09-01T00:00:00Z'])
        run(['index'], self.root)
        r = self.run_groom(['--date', DATE])
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._digest(DATE)
        self.assertIn('· 1 for you', text, text)
        for_you = text[text.index('## For you'):text.index('\n\n', text.index('## For you') + 1)]
        lines = [l for l in for_you.splitlines() if l.startswith('NEEDS OPERATOR:')]
        self.assertEqual(len(lines), 1, for_you)
        self.assertTrue(lines[0].startswith('NEEDS OPERATOR: E-0010 — '), lines[0])
        self.assertTrue(lines[0].endswith('; approvals.new_epic is not auto.'), lines[0])


if __name__ == '__main__':
    unittest.main()
