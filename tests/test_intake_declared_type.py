"""tests.test_intake_declared_type — acceptance tests for F-0134: a header line is never a
title, a refusal names the line that would settle the card, and I13 is measured against cards
the factory really minted."""
import os
import shutil
import unittest

from asf import invariants
from asf.groom import inbox, shape
from asf.record import frontmatter

from tests.test_inbox_shape import make_record, seed, run


class HeaderFirstIsNeverATitle(unittest.TestCase):
    """S-39250: a header line is never read as a title, first position or last (C1)."""

    def setUp(self):
        self.root = make_record()
        seed(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_f_0134_reproduction_mints_nothing_and_leaves_a_question(self):
        path = os.path.join(self.root, 'inbox', 'urgent.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("type: bug\nparent: E-0001\nseverity: S1\n\nBilling is down for every customer.\n")
        before_features = sorted(os.listdir(os.path.join(self.root, 'features')))
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, 'features'))), before_features)
        self.assertEqual(os.listdir(os.path.join(self.root, 'bugs')), [])
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('## Question', text)
        self.assertIn('# <title>', text)

    def test_the_same_card_titled_still_mints_a_bug(self):
        path = os.path.join(self.root, 'inbox', 'urgent.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Billing is down\ntype: bug\nparent: E-0001\nseverity: S1\n\n"
                    "Billing is down for every customer.\n")
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        bugs = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(bugs, ['B-0001.md'])
        with open(os.path.join(self.root, 'bugs', 'B-0001.md'), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read(), path='bugs/B-0001.md')
        self.assertEqual(meta['severity'], 'S1')
        self.assertEqual(meta['signature'], 'Billing is down')

    def test_parse_inbox_file_over_a_leading_header_block(self):
        c = inbox.parse_inbox_file("type: bug\nparent: E-0001\nseverity: S1\n\nBilling is down.\n")
        self.assertEqual(c.title, '')
        self.assertEqual(c.headers, {'type': 'bug', 'parent': 'E-0001', 'severity': 'S1'})

    def test_a_bare_first_line_is_still_the_title(self):
        c = inbox.parse_inbox_file("New idea\ntype: bug\nparent: E-0001\n\nBody.\n")
        self.assertEqual(c.title, 'New idea')
        self.assertEqual(c.headers.get('type'), 'bug')

    def test_pd7_five_first_line_forms(self):
        titles = ('S1: Billing is down', 'Note: something')
        for first in titles:
            with self.subTest(first=first):
                c = inbox.parse_inbox_file(f"{first}\n\nBody.\n")
                self.assertEqual(c.title, first)
        headers = ('type: bug', 'Writes: the docs', 'after: T-0001')
        for first in headers:
            with self.subTest(first=first):
                c = inbox.parse_inbox_file(f"{first}\n\nBody.\n")
                self.assertEqual(c.title, '')

    def test_question_lines_renders_untitled_card_by_file_name(self):
        path = os.path.join(self.root, 'inbox', 'urgent.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("type: bug\nparent: E-0001\n\nBilling is down.\n")
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = inbox.question_lines(self.root, intake_dir='inbox')
        self.assertEqual(len(lines), 1)
        self.assertIn('inbox:urgent.md urgent.md —', lines[0])

    def test_apply_answer_on_the_untitled_card_leaves_one_type_line(self):
        path = os.path.join(self.root, 'inbox', 'urgent.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("type: bug\nparent: E-0001\nseverity: S1\n\nBilling is down.\n")
        applied, reason = inbox.apply_answer(self.root, 'urgent.md', 'feature', '2026-10-06', 'op',
                                              intake_dir='inbox')
        self.assertTrue(applied, reason)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(text.count('type:'), 1)
        self.assertIn('type: feature', text)

    def test_pd4_an_after_first_card_gets_its_answer_above_that_line(self):
        path = os.path.join(self.root, 'inbox', 'blocked.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("after: T-0001\n\nSome body.\n")
        applied, reason = inbox.apply_answer(self.root, 'blocked.md', 'feature', '2026-10-06', 'op',
                                              intake_dir='inbox')
        self.assertTrue(applied, reason)
        with open(path, encoding='utf-8') as f:
            lines = [l for l in f.read().split('\n') if l.strip()]
        self.assertEqual(lines[0], 'type: feature')
        self.assertIn('after: T-0001', lines)
        self.assertLess(lines.index('type: feature'), lines.index('after: T-0001'))

    def test_pd5_an_answer_cannot_settle_a_no_title_refusal(self):
        path = os.path.join(self.root, 'inbox', 'urgent.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("type: bug\nparent: E-0001\nseverity: S1\n\nBilling is down.\n")
        r1 = run(['groom'], self.root)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        with open(path, encoding='utf-8') as f:
            text1 = f.read()
        self.assertIn('## Question', text1)

        applied, reason = inbox.apply_answer(self.root, 'urgent.md', 'feature', '2026-10-06', 'op',
                                              intake_dir='inbox')
        self.assertTrue(applied, reason)

        r2 = run(['groom'], self.root)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        with open(path, encoding='utf-8') as f:
            text2 = f.read()
        self.assertIn('## Question', text2)
        self.assertIn('# <title>', text2)

        r3 = run(['groom'], self.root)
        self.assertEqual(r3.returncode, 0, r3.stderr)
        with open(path, encoding='utf-8') as f:
            text3 = f.read()
        self.assertEqual(text2, text3)


class RefusalNamesTheMissingLine(unittest.TestCase):
    """S-39251: a declared type the card is not shaped like is refused with the one line that
    would settle it (C4), never minted as the other type."""

    def setUp(self):
        self.root = make_record()
        seed(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _folders(self):
        return {f: sorted(os.listdir(os.path.join(self.root, f)))
                for f in ('features', 'bugs', 'tasks', 'stories')}

    def _run_case(self, body, declared_type, needs_text, against):
        path = os.path.join(self.root, 'inbox', 'thing.md')
        before = self._folders()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._folders(), before)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('## Question', text)
        self.assertIn(f'type: {declared_type}', text)
        self.assertIn('is read', text)
        self.assertNotIn('is not read', text)
        self.assertIn(needs_text, text)
        self.assertIn(f'reads as {against}', text)

    def test_story_with_an_epic_parent_asks_for_a_feature_parent_and_acceptance(self):
        self._run_case(
            "# Billing checkout\ntype: story\nparent: E-0001\n\nSomething new.\n",
            'story', shape.NEEDS['story'], 'feature')

    def test_task_with_neither_writes_nor_parent_asks_for_writes(self):
        self._run_case(
            "# Billing checkout\ntype: task\n\nSomething new.\n",
            'task', shape.NEEDS['task'], 'feature')

    def test_a_declared_epic_needs_no_features_list_and_is_minted_an_epic(self):
        # 2026-10-10 (F-0346): `type: epic` is the declaration; the candidate list stays body
        path = os.path.join(self.root, 'inbox', 'thing.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Billing overhaul\ntype: epic\n\nCandidates:\n1. a\n2. b\n")
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self._folders()['features'], ['F-0001.md'])
        self.assertIn('E-0002.md', os.listdir(os.path.join(self.root, 'epics')))

    def test_feature_carrying_a_signature_asks_for_no_signature(self):
        self._run_case(
            "# Billing checkout\ntype: feature\nsignature: test_pay\nparent: E-0001\n\nSomething new.\n",
            'feature', shape.NEEDS['feature'], 'bug')

    def test_bug_with_a_writes_line_asks_for_no_writes(self):
        self._run_case(
            "# Billing checkout\ntype: bug\nwrites: asf/a.py\nparent: F-0001\n\nSomething new.\n",
            'bug', shape.NEEDS['bug'], 'task')

    def test_unknown_type_gets_the_sizes_sentence_and_mints_nothing(self):
        path = os.path.join(self.root, 'inbox', 'thing.md')
        before = self._folders()
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Billing checkout\ntype: chore\nparent: E-0001\n\nSomething new.\n")
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._folders(), before)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('## Question', text)
        for size in shape.SIZE:
            self.assertIn(size, text)


class I13OverRealMints(unittest.TestCase):
    """S-39253: I13 measured against cards the factory really minted, not two literal strings."""

    def setUp(self):
        self.root = make_record()
        seed(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mint(self, body, folder):
        path = os.path.join(self.root, 'inbox', 'thing.md')
        before = set(os.listdir(os.path.join(self.root, folder)))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = set(os.listdir(os.path.join(self.root, folder)))
        new = sorted(after - before)
        self.assertEqual(len(new), 1, new)
        with open(os.path.join(self.root, folder, new[0]), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read())
        return meta['type']

    def test_bug_with_a_signature(self):
        minted = self._mint(
            "# Checkout is broken\ntype: bug\nsignature: test_pay\nparent: E-0001\nseverity: S2\n\n"
            "Customers cannot pay.\n", 'bugs')
        self.assertEqual(invariants.check_i13('bug', minted), [])
        self.assertEqual('bug', minted)

    def test_bug_without_a_signature(self):
        minted = self._mint(
            "# Billing plans page is down\ntype: bug\nparent: E-0001\nseverity: S1\n\n"
            "Nothing loads.\n", 'bugs')
        self.assertEqual(invariants.check_i13('bug', minted), [])
        self.assertEqual('bug', minted)

    def test_feature_with_an_epic_parent(self):
        minted = self._mint(
            "# Billing invoices\ntype: feature\nparent: E-0001\n\nSomething new.\n", 'features')
        self.assertEqual(invariants.check_i13('feature', minted), [])
        self.assertEqual('feature', minted)

    def test_task_with_writes_and_a_feature_parent(self):
        minted = self._mint(
            "# Widen the retry window\ntype: task\nwrites: asf/groom/**\nparent: F-0001\n\n"
            "Something new.\n", 'tasks')
        self.assertEqual(invariants.check_i13('task', minted), [])
        self.assertEqual('task', minted)

    def test_epic_with_a_two_item_features_list(self):
        minted = self._mint(
            "# A bigger billing outcome\ntype: epic\n\n## Features\n- Plan tiers\n- Invoicing\n",
            'epics')
        self.assertEqual(invariants.check_i13('epic', minted), [])
        self.assertEqual('epic', minted)

    def test_a_refused_card_mints_nothing_so_i13_cannot_pass_vacuously(self):
        folders = ('features', 'bugs', 'tasks', 'stories', 'epics')
        path = os.path.join(self.root, 'inbox', 'thing.md')
        before = {f: sorted(os.listdir(os.path.join(self.root, f))) for f in folders}
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Billing checkout\ntype: chore\nparent: E-0001\n\nSomething new.\n")
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = {f: sorted(os.listdir(os.path.join(self.root, f))) for f in folders}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
