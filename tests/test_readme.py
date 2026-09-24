"""tests.test_readme — the span grammar and the renderer (asf.views.readme)."""
import unittest

from asf.views import readme

PAGE = ('# Title\n'
        '\n'
        'We ran <!--asf:n sessions-->1,204<!--/asf:n--> sessions.\n'
        '\n'
        'Prose between.\n'
        '\n'
        '<!--asf:block scoreboard-->\n'
        '| Metric | Value |\n'
        '| a | 1 |\n'
        '<!--/asf:block-->\n'
        '\n'
        'The end.\n')


def facts_from(found):
    return {'numbers': {s.key: {'text': s.body} for s in found}}


class SpanTests(unittest.TestCase):
    def raises(self, text, *needles):
        with self.assertRaises(ValueError) as cm:
            readme.spans(text)
        for n in needles:
            self.assertIn(n, str(cm.exception))

    def test_no_marker_gives_empty(self):
        self.assertEqual(readme.spans('just prose\n'), [])

    def test_round_trip(self):
        found = readme.spans(PAGE)
        self.assertEqual([(s.kind, s.key) for s in found],
                         [('n', 'sessions'), ('block', 'scoreboard')])
        self.assertEqual(found[0].body, '1,204')
        self.assertEqual(found[1].body, '| Metric | Value |\n| a | 1 |\n')
        self.assertEqual(readme.render(PAGE, facts_from(found)), PAGE)

    def test_unclosed(self):
        self.raises('a <!--asf:n x-->1\n', "'x'", 'line 1')

    def test_unclosed_block(self):
        self.raises('a\n<!--asf:block b-->\nrow\n', "'b'", 'line 2')

    def test_close_with_no_open(self):
        self.raises('a\nb <!--/asf:n--> c\n', 'line 2')

    def test_nested(self):
        self.raises('<!--asf:n a-->1 <!--asf:n b-->2<!--/asf:n--><!--/asf:n-->', "'b'", "'a'")

    def test_inline_inside_block(self):
        self.raises('<!--asf:block a-->\nx <!--asf:n b-->1<!--/asf:n-->\n<!--/asf:block-->\n',
                    "'b'")

    def test_duplicate_key(self):
        self.raises('<!--asf:n a-->1<!--/asf:n--> <!--asf:n a-->2<!--/asf:n-->', "'a'")

    def test_block_marker_shares_line(self):
        self.raises('text <!--asf:block a-->\nrow\n<!--/asf:block-->\n', 'line 1')

    def test_block_close_shares_line(self):
        self.raises('<!--asf:block a-->\nrow <!--/asf:block-->\n', 'line 2')


class RenderTests(unittest.TestCase):
    def test_inline_leaves_sentence(self):
        out = readme.render(PAGE, {'numbers': {'sessions': {'text': '9'},
                                               'scoreboard': {'text': '| x |\n'}}})
        self.assertIn('We ran <!--asf:n sessions-->9<!--/asf:n--> sessions.\n', out)

    def test_block_rows_replaced_markers_stay(self):
        out = readme.render(PAGE, {'numbers': {'sessions': {'text': '1,204'},
                                               'scoreboard': {'text': '| x |\n| y |\n'}}})
        self.assertIn('<!--asf:block scoreboard-->\n| x |\n| y |\n<!--/asf:block-->\n', out)
        self.assertTrue(out.startswith('# Title\n') and out.endswith('The end.\n'))

    def test_missing_fact_names_key(self):
        with self.assertRaises(ValueError) as cm:
            readme.render(PAGE, {'numbers': {'sessions': {'text': '1'}}})
        self.assertIn('scoreboard', str(cm.exception))

    def test_unused_fact_names_key(self):
        facts = facts_from(readme.spans(PAGE))
        facts['numbers']['ghost'] = {'text': '0'}
        with self.assertRaises(ValueError) as cm:
            readme.render(PAGE, facts)
        self.assertIn('ghost', str(cm.exception))

    def test_idempotent(self):
        facts = {'numbers': {'sessions': {'text': '7'}, 'scoreboard': {'text': '| z |'}}}
        once = readme.render(PAGE, facts)
        self.assertEqual(readme.render(once, facts), once)


if __name__ == '__main__':
    unittest.main()
