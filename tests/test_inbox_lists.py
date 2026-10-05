"""tests.test_inbox_lists — intake reads a list header's own syntax: ``writes: [a, b]`` is the two
globs ``a`` and ``b``, never the one string ``"[a, b]"`` (an inbox Task's footprint was filed
double-wrapped, so no write ever matched it)."""
import os
import shutil
import unittest
from unittest import mock

from asf.groom import inbox as inbox_mod
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, today
from tests.test_inbox_shape import make_record, seed


class HeaderListTests(unittest.TestCase):
    def test_list_syntaxes(self):
        for raw, want in [('[a, b]', ['a', 'b']), ('a, b', ['a', 'b']), ('[a]', ['a']),
                          ("['a/**', \"b.py\"]", ['a/**', 'b.py']), ('a', ['a']),
                          ('', []), ('[]', []), ('[[S-0001]]', ['S-0001']),
                          ('[[[S-0001]], [[S-0002]]]', ['S-0001', 'S-0002'])]:
            self.assertEqual(inbox_mod.header_list(raw), want, raw)


class InboxTaskListTests(unittest.TestCase):
    def setUp(self):
        self.root = make_record()
        seed(self.root)
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_bracketed_writes_line_files_each_glob(self):
        with open(os.path.join(self.root, 'inbox', 'w.md'), 'w', encoding='utf-8') as f:
            f.write('# Split the plan reader\nparent: S-0001\nwrites: [asf/a.py, tests/test_a.py]\n'
                    'stories: [S-0001]\n\nSplit it.\n')
        canonical = canonicalize(load_items(self.root)[0])[0]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BACKLOG_ID_RANGE', None)
            [tid] = inbox_mod.process_inbox(self.root, canonical, today())
        with open(os.path.join(self.root, 'tasks', f'{tid}.md'), encoding='utf-8') as f:
            meta, _ = frontmatter.parse(f.read())
        self.assertEqual(list(meta['writes']), ['asf/a.py', 'tests/test_a.py'])
        self.assertEqual(list(meta['stories']), ['S-0001'])


if __name__ == '__main__':
    unittest.main()
