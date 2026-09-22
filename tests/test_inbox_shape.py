"""tests.test_inbox_shape — acceptance tests for F-0073: the shape rules decide an inbox card's
type from what it carries (header lines, sections, its parent's type), not from words or a
hand-set `type:` line."""
import unittest

from asf.groom import shape
from asf.record import frontmatter

MACHINE_LINES = ('state: New', 'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z')


def card(title, headers=None, description='', features=(), acceptance=()):
    return shape.Card(title, dict(headers or {}), description, list(features), list(acceptance))


def _record(header_lines):
    text = ("---\n" + "\n".join(header_lines) + "\n# ---- machine ----\n"
            + "\n".join(MACHINE_LINES) + "\n---\n## Description\n")
    meta, body = frontmatter.parse(text)
    return {'meta': meta, 'body': body}


def seeded_canonical():
    """The record every class in this module starts from: E-0001 (epic, decided, title
    `Factory billing`), F-0001 (feature under E-0001, title `Billing plans`) and S-0001 (story
    under F-0001)."""
    return {
        'E-0001': _record(['id: E-0001', 'type: epic', 'title: Factory billing', 'decided: true']),
        'F-0001': _record(['id: F-0001', 'type: feature', 'title: Billing plans', 'parent: E-0001']),
        'S-0001': _record(['id: S-0001', 'type: story', 'title: Billing plan tiers', 'parent: F-0001']),
    }


class ShapeRulesTest(unittest.TestCase):
    def setUp(self):
        self.canonical = seeded_canonical()

    def test_signature_is_a_bug(self):
        c = card('Checkout fails', headers={'signature': 'test_checkout::test_pay', 'parent': 'E-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('bug', 'signature', 'E-0001'))

    def test_signature_uses_the_default_bug_parent(self):
        c = card('Checkout fails', headers={'signature': 'test_checkout::test_pay'})
        result = shape.derive(c, self.canonical, default_bug_parent='E-0001')
        self.assertEqual(result, shape.Shape('bug', 'signature', 'E-0001'))

    def test_writes_is_a_task(self):
        c = card('Widen the retry window', headers={'writes': 'asf/groom/**', 'parent': 'F-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('task', 'writes', 'F-0001'))

    def test_two_features_is_an_epic(self):
        c = card('A bigger billing outcome', features=['Plan tiers', 'Invoicing'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('epic', 'features-list', None))

    def test_one_feature_bullet_is_a_feature(self):
        c = card('Billing invoices', features=['Invoicing'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))

    def test_parent_feature_with_acceptance_is_a_story(self):
        c = card('Add plan tiers', headers={'parent': 'F-0001'}, acceptance=['python3 -m unittest tests.x'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('story', 'parent-feature', 'F-0001'))

    def test_plain_card_is_a_feature_under_the_named_epic(self):
        c = card('Something new', headers={'parent': 'E-0001'}, description='A description.')
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))

    def test_words_do_not_type(self):
        c = card('A new goal for the quarter', headers={'parent': 'E-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))


class ShapeQuestionTest(unittest.TestCase):
    def setUp(self):
        self.canonical = seeded_canonical()

    def test_signature_and_writes(self):
        c = card('One or the other', headers={'signature': 'x', 'writes': 'a/**', 'parent': 'E-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('A card is one thing', result.text)

    def test_story_without_acceptance(self):
        c = card('Add plan tiers', headers={'parent': 'F-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('one acceptance list', result.text)

    def test_under_a_story_needs_writes(self):
        c = card('Something under a story', headers={'parent': 'S-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('only a Task hangs', result.text)

    def test_epic_with_parent(self):
        c = card('A bigger outcome', headers={'parent': 'E-0001'}, features=['Plan tiers', 'Invoicing'])
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('An Epic has no parent', result.text)

    def test_defect_words_without_signature(self):
        c = card('Checkout is broken', description='Customers cannot pay.')
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('A Bug carries a signature', result.text)

    def test_defect_words_with_acceptance_is_work(self):
        c = card('Checkout is broken', headers={'parent': 'E-0001'}, description='Customers cannot pay.',
                  acceptance=['python3 -m unittest tests.x'])
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('feature', 'default', 'E-0001'))

    def test_type_line_disagreeing(self):
        c = card('Checkout fails', headers={'type': 'feature', 'signature': 'x', 'parent': 'E-0001'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn("is not read — the card's shape reads as bug (signature", result.text)

    def test_type_line_agreeing_is_filed(self):
        c = card('Checkout fails', headers={'type': 'bug', 'signature': 'x', 'parent': 'E-0001'})
        self.assertEqual(shape.derive(c, self.canonical), shape.Shape('bug', 'signature', 'E-0001'))

    def test_missing_parent(self):
        c = card('Something', headers={'parent': 'E-0999'})
        result = shape.derive(c, self.canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('does not exist', result.text)


if __name__ == '__main__':
    unittest.main()
