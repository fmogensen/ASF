"""asf.tables — markdown tables stay markdown in files and pipes; the console gets box tables."""
import contextlib
import io
import os
import unittest
from unittest import mock

from asf import tables

MD = ("**ROADMAP** (generated 17:06)\n"
      "\n"
      "| # | Epic | Next |\n"
      "|---|---|---|\n"
      "| 1 | E-0001 a working factory | F-0075 Redaction is a gate on every write path \\| twice |\n"
      "| 2 | E-0002 | — |\n"
      "\n"
      "0 PRs named\n")


class Render(unittest.TestCase):
    def test_a_table_becomes_a_box_and_the_prose_passes(self):
        out = tables.render(MD, total=60)
        lines = out.split('\n')
        self.assertEqual(lines[0], 'ROADMAP (generated 17:06)')
        self.assertTrue(lines[2].startswith('┌') and lines[2].endswith('┐'))
        self.assertIn('╞', out)                        # the header rule
        self.assertIn('0 PRs named', lines[-2])
        self.assertNotIn('|---', out)
        self.assertIn('| twice', out)                  # an escaped bar is a literal bar

    def test_every_box_line_fits_the_width_and_lines_up(self):
        out = tables.render(MD, total=60)
        box = [l for l in out.split('\n') if l and l[0] in '┌│╞├└']
        self.assertTrue(all(tables._w(l) <= 60 for l in box), box)
        self.assertEqual(len({tables._w(l) for l in box}), 1)

    def test_a_long_word_is_split_not_overflowed(self):
        out = tables.box(['| a | ' + 'x' * 80 + ' |'], total=40)
        self.assertTrue(all(tables._w(l) <= 40 for l in out))

    def test_single_line_rows_get_no_rule_between_them(self):
        out = tables.box(['| a | b |', '|---|---|', '| 1 | 2 |', '| 3 | 4 |'], total=40)
        self.assertEqual(len(out), 6)                  # top, header, header rule, 2 rows, bottom
        self.assertFalse(any(l.startswith('├') for l in out))

    def test_text_without_a_table_is_unchanged(self):
        self.assertEqual(tables.render('plain\nlines\n'), 'plain\nlines\n')


class Stream(unittest.TestCase):
    def test_streamed_writes_match_the_whole_render(self):
        sink = io.StringIO()
        s = tables.BoxStream(sink, total=60)
        for chunk in (MD[:10], MD[10:47], MD[47:]):
            s.write(chunk)
        s.close()
        self.assertEqual(sink.getvalue(), tables.render(MD, total=60))

    def test_a_table_at_the_very_end_is_drawn_on_close(self):
        sink = io.StringIO()
        s = tables.BoxStream(sink, total=40)
        s.write('| a | b |')
        s.close()
        self.assertTrue(sink.getvalue().startswith('┌'))


class Mode(unittest.TestCase):
    def test_a_pipe_stays_markdown_unless_asked(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ASF_TABLES', None)
            self.assertEqual(tables.mode(io.StringIO()), 'md')
            os.environ['ASF_TABLES'] = 'box'
            self.assertEqual(tables.mode(io.StringIO()), 'box')

    def test_md_is_never_redrawn(self):
        tty = mock.Mock(isatty=lambda: True)
        with mock.patch.dict(os.environ, {'ASF_TABLES': 'md'}):
            self.assertEqual(tables.mode(tty), 'md')

    def test_install_wraps_and_finish_restores(self):
        sink = io.StringIO()
        with mock.patch.dict(os.environ, {'ASF_TABLES': 'box', 'ASF_TABLE_WIDTH': '50'}), \
                contextlib.redirect_stdout(sink):
            finish = tables.install()
            print('| a | b |')
            finish()
            import sys
            self.assertIs(sys.stdout, sink)
        self.assertTrue(sink.getvalue().startswith('┌'))


if __name__ == '__main__':
    unittest.main()
