import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf.record import core
from asf.record import frontmatter


def _card(iid, title='Card', extra=''):
    return (
        "---\n"
        f"id: {iid}\n"
        "type: task\n"
        f"title: {title}\n"
        "links:\n"
        "  runs: [1, 2]\n"
        "tags: [a, b]\n"
        f"{extra}"
        "---\n"
        "## Description\n"
        "body text\n"
    )


class CardParseCache(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.tasks = os.path.join(self.root, 'tasks')
        os.makedirs(self.tasks)
        core.clear_parse_cache()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(core.clear_parse_cache)

    def _write(self, iid, **kw):
        path = os.path.join(self.tasks, f'{iid}.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_card(iid, **kw))
        return path

    def test_three_cards_two_calls_parse_three_times_not_six(self):
        for iid in ('T-0001', 'T-0002', 'T-0003'):
            self._write(iid)
        with mock.patch.object(frontmatter, 'parse', wraps=frontmatter.parse) as spy:
            core.load_items(self.root, folders=['tasks'])
            core.load_items(self.root, folders=['tasks'])
            self.assertEqual(spy.call_count, 3)

    def test_rewriting_one_card_parses_exactly_that_one(self):
        for iid in ('T-0001', 'T-0002', 'T-0003'):
            self._write(iid)
        core.load_items(self.root, folders=['tasks'])
        self._write('T-0002', title='Changed')
        with mock.patch.object(frontmatter, 'parse', wraps=frontmatter.parse) as spy:
            core.load_items(self.root, folders=['tasks'])
            self.assertEqual(spy.call_count, 1)

    def test_rewriting_with_identical_text_parses_nothing(self):
        self._write('T-0001')
        core.load_items(self.root, folders=['tasks'])
        self._write('T-0001')  # byte-identical content, new mtime
        with mock.patch.object(frontmatter, 'parse', wraps=frontmatter.parse) as spy:
            core.load_items(self.root, folders=['tasks'])
            self.assertEqual(spy.call_count, 0)

    def test_utime_on_unchanged_card_parses_nothing(self):
        path = self._write('T-0001')
        core.load_items(self.root, folders=['tasks'])
        os.utime(path, (1000000000, 1000000000))
        with mock.patch.object(frontmatter, 'parse', wraps=frontmatter.parse) as spy:
            core.load_items(self.root, folders=['tasks'])
            self.assertEqual(spy.call_count, 0)

    def test_two_calls_return_different_meta_objects_mutation_does_not_leak(self):
        self._write('T-0001')
        by_id1, _ = core.load_items(self.root, folders=['tasks'])
        meta1 = by_id1['T-0001'][0]['meta']
        meta1['blockedBy'] = ['T-9999']
        meta1['links']['runs'].append(3)
        meta1.machine_keys = {'stage'}

        by_id2, _ = core.load_items(self.root, folders=['tasks'])
        meta2 = by_id2['T-0001'][0]['meta']

        self.assertIsNot(meta1, meta2)
        self.assertIsNot(meta1['links'], meta2['links'])
        self.assertNotIn('blockedBy', meta2)
        self.assertEqual(meta2['links']['runs'], [1, 2])
        self.assertEqual(meta2.machine_keys, set())

    def test_render_of_loaded_record_reproduces_original_bytes(self):
        text = _card('T-0001')
        self._write('T-0001')
        by_id, _ = core.load_items(self.root, folders=['tasks'])
        rec = by_id['T-0001'][0]
        self.assertEqual(frontmatter.render(rec['meta'], rec['body']), text)

    def test_render_of_mutated_clone_matches_mutated_fresh_parse(self):
        text = _card('T-0001')
        self._write('T-0001')
        by_id, _ = core.load_items(self.root, folders=['tasks'])
        cached_meta = by_id['T-0001'][0]['meta']
        cached_meta['links']['runs'].append(3)
        cached_rendered = frontmatter.render(cached_meta, by_id['T-0001'][0]['body'])

        fresh_meta, fresh_body = frontmatter.parse(text)
        fresh_meta['links']['runs'].append(3)
        fresh_rendered = frontmatter.render(fresh_meta, fresh_body)

        # `render` replays a key's raw line while `meta[key] is e.value` — true of a fresh parse
        # too, so an in-place nested mutation is dropped by both, identically.
        self.assertEqual(cached_rendered, fresh_rendered)
        self.assertEqual(cached_rendered, text)

    def test_broken_frontmatter_reported_every_call(self):
        path = os.path.join(self.tasks, 'T-0001.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("---\nid: T-0001\nnot a valid line without colon or list\n")
        with mock.patch.object(frontmatter, 'parse', wraps=frontmatter.parse) as spy:
            _by_id1, errors1 = core.load_items(self.root, folders=['tasks'])
            _by_id2, errors2 = core.load_items(self.root, folders=['tasks'])
            self.assertEqual(len(errors1), 1)
            self.assertEqual(len(errors2), 1)
            self.assertEqual(spy.call_count, 2)

    def test_clear_parse_cache_empties_the_store(self):
        self._write('T-0001')
        core.load_items(self.root, folders=['tasks'])
        self.assertTrue(core._PARSE_CACHE)
        core.clear_parse_cache()
        self.assertEqual(core._PARSE_CACHE, {})


if __name__ == '__main__':
    unittest.main()
