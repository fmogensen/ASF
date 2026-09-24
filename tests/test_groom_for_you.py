"""The groom digest's **For you** holds only human-now approval actions (package §9 item 9).

The groom card behind it: For you read "379 for you" on a first install — near-duplicates of
templated Tasks, cards barred by a keyword in their title, and groom housekeeping. Each is
now decided elsewhere: a near-duplicate Task by rule (the younger closed when parent and
footprint match, else no flag, with a raised threshold for templated titles); a card decision
by no approval class (the approvals hook guards the work); housekeeping in its own section. A
real human-now action — a new Epic — still reaches For you.
"""
import datetime
import re
import shutil
import unittest

from asf.env import Product
from asf.groom import digest, groom, policy
from asf.record.core import canonicalize, compute_derived, load_items
from tests.test_groom import make_repo, write_item

DATE = '2026-09-24'
NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)
OLD = ['state: New', 'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z']
FRESH = ['state: New', 'stage_since: 2026-09-24T00:00:00Z', 'updated: 2026-09-24T00:00:00Z']


def for_you_count(text):
    return int(re.search(r'· (\d+) for you', text).group(1))


class GroomForYouTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, True)
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Parity with the old app', parent='E-0009',
                   typed_lines=['decided: true'],
                   machine_lines=['state: Active', 'stage: plan-approved'] + FRESH[1:])
        # keyword-laden Stories: a title that reads as money, security or legal bars nothing
        for sid, title in (('S-0001', 'Stripe payment tokens are refunded'),
                           ('S-0002', 'Privacy consent and GDPR data exports'),
                           ('S-0003', 'OAuth credentials rotate in production')):
            write_item(self.root, sid, 'story', title, parent='F-0001',
                       typed_lines=['decided: true'], machine_lines=FRESH)
        # templated Tasks: T-0004 is T-0001 again (same parent, same footprint) — closed by
        # rule; T-0002/T-0003 share the template but not the footprint or the slot — no flag
        tasks = (('T-0001', 'Parity: the sign-up form shows the free plan', 'src/signup.py'),
                 ('T-0002', 'Parity: the sign-up form shows the paid plan', 'src/paid.py'),
                 ('T-0003', 'Parity: the sign-up form shows the team plan', 'src/team.py'),
                 ('T-0004', 'Parity: the sign-up form shows the free plan', 'src/signup.py'))
        for tid, title, writes in tasks:
            write_item(self.root, tid, 'task', title, parent='F-0001',
                       typed_lines=[f'decided: {"false" if tid == "T-0004" else "true"}', f'writes: [{writes}]'],
                       machine_lines=FRESH)
        # groom housekeeping: a Feature with no Stories
        write_item(self.root, 'F-0002', 'feature', 'Sidebar rows show unread counts',
                   parent='E-0009', typed_lines=['decided: true'], machine_lines=FRESH)

    def digest(self, approvals, attempts=2, cap=2):
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        derived = compute_derived(canonical)
        product = Product('sample', {'approvals': approvals})
        sections = groom.build_groom_sections(canonical, derived, DATE)
        sections, _barred = groom.run_policy_pass(
            sections, canonical, derived, product,
            policy.Ctx(date=DATE, now=NOW, approvals=approvals))
        text = groom.render_groom_file(DATE, sections)
        return sections, digest.render_digest(self.root, DATE, canonical, text, [], attempts, cap)

    def test_for_you_is_zero_on_templated_duplicates_and_keyword_stories(self):
        sections, text = self.digest({'groom': 'auto', 'spend_money': 'human-now',
                                      'touch_security': 'human-now', 'touch_legal': 'human-now'})
        self.assertEqual(for_you_count(text), 0, text)
        # the duplicate Task pair is answered by rule; the other templated Tasks are no question
        self.assertEqual(len(sections['dupes']), 1, sections['dupes'])
        self.assertIn('T-0004', sections['dupes'][0])
        self.assertIn('controller: close_duplicate_task no: duplicate of T-0001', sections['dupes'][0])
        self.assertNotIn('barred', groom.render_groom_file(DATE, sections))
        # the housekeeping has its own section
        self.assertIn('## Housekeeping', text)
        self.assertIn('F-0002', text[text.index('## Housekeeping'):])

    def test_a_new_epic_still_reaches_for_you(self):
        write_item(self.root, 'E-0010', 'epic', 'A new goal for the year', machine_lines=OLD)
        _sections, text = self.digest({'groom': 'auto', 'new_epic': 'human-now'})
        self.assertEqual(for_you_count(text), 1, text)
        for_you = text[text.index('## For you'):]
        self.assertIn('NEEDS OPERATOR: E-0010', for_you)
        self.assertIn('approvals.new_epic is not auto', for_you)

    def test_a_task_pair_without_one_footprint_is_no_question(self):
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        self.assertIsNone(policy.task_duplicate_of('T-0002', canonical['T-0002'], canonical))
        self.assertEqual(policy.task_duplicate_of('T-0004', canonical['T-0004'], canonical), 'T-0001')
        self.assertTrue(policy.templated('Parity: a', 'Parity: b'))
        self.assertEqual(policy.near_duplicate_threshold('Parity: a', 'Parity: b'),
                         policy.TEMPLATED_OVERLAP)
        self.assertEqual(policy.near_duplicate_threshold('Free plan', 'Free plan redesign'),
                         policy.NEAR_DUPLICATE_OVERLAP)


if __name__ == '__main__':
    unittest.main()
