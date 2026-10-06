"""tests.test_checked — F-0233 Task 1: the checked list gets one owner, a per-product home, and
a format a person can read and edit. ``TheList`` is S-36850's unit: `path`, `read`, `matches`,
`add`, `remove`, `waiting_line` and `held`, each against a tmp `ASF_HOME` so the operator's own
files are never touched."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.evidence import checked

PRODUCT = 'sample'


class TheList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='checked_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.addCleanup(setattr, env, 'ASF_HOME', self._orig_home)
        self.legacy = os.path.join(self.tmp, 'legacy.txt')
        patcher = mock.patch.object(checked, 'LEGACY_PATH', self.legacy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_legacy(self, text):
        with open(self.legacy, 'w', encoding='utf-8') as f:
            f.write(text)

    # ---- path -------------------------------------------------------------------------------

    def test_path_is_under_the_products_state_dir_and_ends_checked_txt(self):
        p = checked.path(PRODUCT)
        self.assertTrue(p.startswith(env.state_dir(PRODUCT)), p)
        self.assertTrue(p.endswith('checked.txt'), p)

    # ---- read / the line formats -------------------------------------------------------------

    def test_read_returns_a_set_of_str(self):
        checked.add(PRODUCT, [('869', '', '')])
        out = checked.read(PRODUCT)
        self.assertIsInstance(out, set)
        self.assertTrue(all(isinstance(t, str) for t in out))
        self.assertEqual(out, {'869'})

    def test_hash_pr_bare_pr_and_sha_lines_all_parse(self):
        sha = 'a' * 40
        with open(checked.path(PRODUCT), 'w', encoding='utf-8') as f:
            f.write('2026-01-01T00:00Z  -  #869  one\n')
            f.write(f'2026-01-01T00:00Z  -  871  two\n')
            f.write(f'2026-01-01T00:00Z  -  {sha}  three\n')
        self.assertEqual(checked.read(PRODUCT), {'869', '871', sha})

    def test_a_comment_line_is_not_a_tick(self):
        with open(checked.path(PRODUCT), 'w', encoding='utf-8') as f:
            f.write('# a note\n')
        self.assertEqual(checked.read(PRODUCT), set())

    def test_hash_digit_followed_by_prose_is_still_a_tick(self):
        with open(checked.path(PRODUCT), 'w', encoding='utf-8') as f:
            f.write('#869 was broken\n')
        self.assertEqual(checked.read(PRODUCT), {'869'})

    def test_six_hex_characters_is_not_a_tick(self):
        with open(checked.path(PRODUCT), 'w', encoding='utf-8') as f:
            f.write('abc123\n')
        self.assertEqual(checked.read(PRODUCT), set())

    # ---- matches ------------------------------------------------------------------------------

    def test_matches_true_for_a_ticked_pr(self):
        self.assertTrue(checked.matches({'869'}, 869, ''))

    def test_matches_true_for_a_sha_prefix_at_7_12_and_40_characters(self):
        sha = 'b' * 40
        for n in (7, 12, 40):
            self.assertTrue(checked.matches({sha[:n]}, None, sha), n)

    def test_matches_false_for_a_6_character_prefix(self):
        sha = 'b' * 40
        self.assertFalse(checked.matches({sha[:6]}, None, sha))

    def test_matches_false_for_pr_none_against_the_string_none(self):
        self.assertFalse(checked.matches({'None'}, None, ''))

    def test_matches_false_for_any_token_against_an_empty_sha(self):
        self.assertFalse(checked.matches({'b' * 40}, None, ''))

    # ---- the legacy union -----------------------------------------------------------------

    def test_read_product_unions_in_the_legacy_file(self):
        self.write_legacy('#555  legacy tick\n')
        checked.add(PRODUCT, [('869', '', '')])
        self.assertEqual(checked.read(PRODUCT), {'555', '869'})

    def test_remove_never_touches_the_legacy_file(self):
        legacy_text = '#555  legacy tick\n'
        self.write_legacy(legacy_text)
        checked.add(PRODUCT, [('555', '', ''), ('869', '', '')])
        checked.remove(PRODUCT, {'555'})
        with open(self.legacy, encoding='utf-8') as f:
            self.assertEqual(f.read(), legacy_text)

    def test_checked_file_argument_reads_only_that_file(self):
        self.write_legacy('#555  legacy tick\n')
        checked.add(PRODUCT, [('869', '', '')])
        absent = os.path.join(self.tmp, 'none.txt')
        self.assertEqual(checked.read(PRODUCT, checked_file=absent), set())

    # ---- add ------------------------------------------------------------------------------

    def test_add_writes_the_header_once_one_line_per_landing_and_nothing_twice(self):
        written = checked.add(PRODUCT, [('869', 'F-0001', 'first'), ('871', 'F-0001', 'second')])
        self.assertEqual(written, ['869', '871'])
        with open(checked.path(PRODUCT), encoding='utf-8') as f:
            lines = f.readlines()
        self.assertEqual(lines[0], checked.HEADER)
        self.assertEqual(len(lines), 3)

        again = checked.add(PRODUCT, [('869', 'F-0001', 'first'), ('871', 'F-0001', 'second')])
        self.assertEqual(again, [])
        with open(checked.path(PRODUCT), encoding='utf-8') as f:
            self.assertEqual(len(f.readlines()), 3)

    def test_add_skips_a_token_the_legacy_file_already_carries(self):
        self.write_legacy('555\n')
        written = checked.add(PRODUCT, [('555', '', ''), ('869', '', '')])
        self.assertEqual(written, ['869'])

    # ---- remove -----------------------------------------------------------------------------

    def test_remove_leaves_the_other_lines_byte_for_byte(self):
        checked.add(PRODUCT, [('869', 'F-0001', 'one'), ('871', 'F-0002', 'two')])
        with open(checked.path(PRODUCT), encoding='utf-8') as f:
            before = f.readlines()
        dropped = checked.remove(PRODUCT, {'869'})
        self.assertEqual(dropped, ['869'])
        with open(checked.path(PRODUCT), encoding='utf-8') as f:
            after = f.readlines()
        self.assertEqual(after, [before[0], before[2]])

    # ---- waiting_line / held --------------------------------------------------------------

    def test_waiting_line_renders_a_pr_and_a_9_character_sha(self):
        line = checked.waiting_line('F-0119', [(869, None), (None, 'c' * 40)])
        self.assertEqual(line, 'waiting on your check: #869, ccccccccc — asf checked --item F-0119')

    def test_held_reads_index_json_and_empty_without_one(self):
        root = os.path.join(self.tmp, 'record')
        os.makedirs(root)
        self.assertEqual(checked.held(root), {})

        index = {
            'generated': '2026-01-01T00:00:00Z',
            'items': {
                'F-0119': {
                    'id': 'F-0119', 'state': 'Resolved',
                    'evidence': [
                        'landed: abc1234',
                        'waiting on your check: #869, #871 — asf checked --item F-0119',
                        'rule: children-closed',
                    ],
                },
            },
        }
        with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump(index, f)
        self.assertEqual(checked.held(root), {'F-0119': ['#869', '#871']})
