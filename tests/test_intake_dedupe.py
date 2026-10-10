"""Intake dedupe for machine-filed cards (Stage 1, part 1d; 2026-10-10: the watchdog had filed one
Bug per instance — ``watchdog launchable_idle: T-0073``, ``…: T-0329``, ``…: PR-0772`` — a dozen
cards for one symptom).

- The signature is the symptom class (``watchdog launchable_idle``), never the instance id.
- A repeat of an open card's class appends its evidence and adds one to that card's ``count:``
  instead of filing a new card — in the record's intake (:func:`asf.groom.inbox.process_inbox`)
  and in the watchdog's own filer (:func:`asf.dwell.file_cards`).
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import approvals, dwell
from asf.groom import inbox
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items


def _bug(root, iid, sig, extra=''):
    os.makedirs(os.path.join(root, 'bugs'), exist_ok=True)
    with open(os.path.join(root, 'bugs', iid + '.md'), 'w') as f:
        f.write('---\nid: %s\ntype: bug\ntitle: one\nseverity: S3\nsignature: "%s"\ncount: 1\n'
                '%s# ---- machine ----\nstate: New\n---\n## Description\nEvidence:\n- first\n\n'
                '## Acceptance\n- [ ] \n\n## History\n- 2026-10-01: created\n' % (iid, sig, extra))


def _canonical(root):
    by_id, _errors = load_items(root)
    return canonicalize(by_id)[0]


def _meta(root, iid):
    with open(os.path.join(root, 'bugs', iid + '.md')) as f:
        return frontmatter.parse(f.read())


class SymptomClass(unittest.TestCase):

    def test_the_class_drops_the_instance(self):
        c = inbox.symptom_class
        self.assertEqual(c('watchdog launchable_idle: T-0073'), 'watchdog launchable_idle')
        self.assertEqual(c('watchdog check_cancelled: #593@62be9d372'), 'watchdog check_cancelled')
        self.assertEqual(c('watchdog launchable_idle'), 'watchdog launchable_idle')
        self.assertEqual(c('PR #12 at abc1234f: T-1, T-2 red'), 'PR … at …: … red')
        self.assertEqual(c('trunk red: test (3.12)'), 'trunk red: test (3.12)')
        self.assertEqual(c('merge 1234567 ok'), 'merge 1234567 ok', 'a number is no sha')

    def test_an_open_card_of_the_class_is_found_a_closed_one_is_not(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        _bug(root, 'B-0001', 'watchdog launchable_idle: T-0073', 'removed: retired: done\n')
        _bug(root, 'B-0002', 'watchdog launchable_idle: T-0329')
        rec = inbox.open_bug_of_class(_canonical(root), 'watchdog launchable_idle: PR-0772')
        self.assertEqual(rec['meta']['id'], 'B-0002')
        self.assertIsNone(inbox.open_bug_of_class(_canonical(root), 'watchdog record_behind'))


class Intake(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, 'inbox'))

    def note(self, name, sig, body='seen again on T-0450'):
        with open(os.path.join(self.root, 'inbox', name), 'w') as f:
            f.write('# Watchdog: a launchable row\ntype: bug\nsignature: %s\n\n%s\n' % (sig, body))

    def test_a_repeat_appends_evidence_and_counts_on_the_open_card(self):
        _bug(self.root, 'B-0002', 'watchdog launchable_idle: T-0329')
        self.note('a.md', 'watchdog launchable_idle: T-0450')
        made = inbox.process_inbox(self.root, _canonical(self.root), '2026-10-10')
        self.assertEqual(made, [])
        meta, body = _meta(self.root, 'B-0002')
        self.assertEqual(meta['count'], 2)
        self.assertIn('- 2026-10-10 inbox: count 2 — also a.md: seen again on T-0450', body)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'inbox', 'a.md')))
        with open(os.path.join(self.root, 'inbox', 'done', 'a.md')) as f:
            self.assertTrue(f.read().startswith('→ B-0002 (repeat of `watchdog launchable_idle`'))

    def test_a_first_card_is_minted_with_the_class_as_its_signature(self):
        os.makedirs(os.path.join(self.root, 'epics'))
        with open(os.path.join(self.root, 'epics', 'E-0001.md'), 'w') as f:
            f.write('---\nid: E-0001\ntype: epic\ntitle: bugs\n---\n## Description\n')
        self.note('a.md', 'watchdog launchable_idle: T-0450')
        made = inbox.process_inbox(self.root, _canonical(self.root), '2026-10-10',
                                   default_bug_parent='E-0001')
        self.assertEqual(len(made), 1, os.listdir(os.path.join(self.root, 'inbox')))
        rec = _canonical(self.root)[made[0]]
        self.assertEqual(rec['meta']['signature'], 'watchdog launchable_idle')


class Watchdog(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.product = mock.Mock()
        self.product.conventions.get.return_value = None

    def breach(self, key):
        f = dwell.Finding('launchable_idle', key, 'row %s launchable' % key)
        f.limit_min, f.age_s = 10, 900
        return f

    def file(self, *keys):
        with mock.patch.object(approvals, 'level_of', return_value='auto'), \
                mock.patch('asf.record.index.do_index'):
            return dwell.file_cards(self.product, self.root, [self.breach(k) for k in keys],
                                    out=lambda _l: None)

    def test_the_signature_is_the_state(self):
        self.assertEqual(dwell.signature(self.breach('T-0073')), 'watchdog launchable_idle')
        self.assertNotIn('T-0073', dwell.bug_info(self.breach('T-0073'))['title'])

    def test_one_bug_per_state_then_new_keys_append_and_count(self):
        self.assertEqual(self.file('T-1', 'T-2'), {'watchdog launchable_idle': 'filed'})
        bugs = [r for r in _canonical(self.root).values() if r['meta'].get('type') == 'bug']
        self.assertEqual(len(bugs), 1)
        iid = bugs[0]['meta']['id']
        self.assertEqual(bugs[0]['meta']['signature'], 'watchdog launchable_idle')
        self.assertEqual(self.file('T-1'), {'watchdog launchable_idle': 'skipped'},
                         'a key already on the card adds nothing')
        self.assertEqual(self.file('T-1', 'T-3'), {'watchdog launchable_idle': 'bumped'})
        meta, body = _meta(self.root, iid)
        self.assertEqual(meta['count'], 2)
        self.assertIn('also T-3 — ', body)
        self.assertEqual(len([r for r in _canonical(self.root).values()
                              if r['meta'].get('type') == 'bug']), 1)

    def test_an_open_card_with_an_old_instance_signature_takes_the_repeat(self):
        _bug(self.root, 'B-0009', 'watchdog launchable_idle: T-0073')
        self.assertEqual(self.file('T-0329'), {'watchdog launchable_idle': 'bumped'})
        self.assertEqual(_meta(self.root, 'B-0009')[0]['count'], 2)


if __name__ == '__main__':
    unittest.main()
