import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

from asf.views import tokens


def write_day(root, day, rows):
    d = os.path.join(root, 'metrics', 'sessions')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, day + '.jsonl'), 'w', encoding='utf-8') as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + '\n')


def row(kind, in_tokens, turns):
    return {'kind': kind, 'in_tokens': in_tokens, 'turns': turns}


class TokensViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def by_kind(self, split='2026-09-23', days=14):
        return {r['kind']: r for r in tokens.compute(self.root, split, days)}

    def test_windows(self):
        before, after = tokens.windows('2026-09-23', 3)
        self.assertEqual(before, ['2026-09-20', '2026-09-21', '2026-09-22'])
        self.assertEqual(after, ['2026-09-23', '2026-09-24', '2026-09-25'])

    def test_split_day_counts_as_after(self):
        write_day(self.root, '2026-09-22', [row('code', 1000, 10)])
        write_day(self.root, '2026-09-23', [row('code', 250, 4)])
        r = self.by_kind()['code']
        self.assertEqual((r['before'], r['after']), (1, 1))
        self.assertEqual(r['delta_pct'], -75)

    def test_median_not_mean(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1), row('code', 200, 3), row('code', 12000000, 5)])
        write_day(self.root, '2026-09-24', [row('code', 100, 1)])
        r = self.by_kind()['code']
        self.assertEqual(r['before_in'], 200)
        self.assertEqual(r['before_turns'], 3)

    def test_all_row_spans_kinds(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1), row('spec', 300, 1)])
        rows = tokens.compute(self.root, '2026-09-23', 14)
        self.assertEqual([r['kind'] for r in rows], ['code', 'spec', 'all'])
        self.assertEqual(rows[-1]['before'], 2)
        self.assertEqual(rows[-1]['before_in'], 200)

    def test_empty_window_prints_dashes_and_no_delta(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1)])
        r = self.by_kind()['code']
        self.assertIsNone(r['after_in'])
        self.assertIsNone(r['delta_pct'])
        out = tokens.render(tokens.compute(self.root, '2026-09-23', 14), '2026-09-23', 14)
        self.assertIn('| code | 1 | 100 | 0 | — | — | 1 → — |', out)

    def test_zero_before_has_no_delta(self):
        write_day(self.root, '2026-09-20', [row('code', 0, 1)])
        write_day(self.root, '2026-09-24', [row('code', 50, 1)])
        self.assertIsNone(self.by_kind()['code']['delta_pct'])

    def test_no_files_and_bad_lines_are_not_errors(self):
        self.assertEqual([r['kind'] for r in tokens.compute(self.root, '2026-09-23', 14)], ['all'])
        write_day(self.root, '2026-09-20', ['not json', '', row('code', 10, 2)])
        self.assertEqual(self.by_kind()['code']['before'], 1)

    def test_rows_without_the_keys_count_but_have_no_reading(self):
        write_day(self.root, '2026-09-20', [{'kind': 'code'}])
        r = self.by_kind()['code']
        self.assertEqual(r['before'], 1)
        self.assertIsNone(r['before_in'])

    def test_table_shape(self):
        write_day(self.root, '2026-09-20', [row('code', 4118000, 78)])
        write_day(self.root, '2026-09-24', [row('code', 1050000, 31)])
        out = tokens.render(tokens.compute(self.root, '2026-09-23', 14), '2026-09-23', 14)
        self.assertTrue(out.startswith('TOKENS — input tokens per session, 14 d either side of 2026-09-23\n'))
        self.assertIn('| code | 1 | 4,118,000 | 1 | 1,050,000 | −75 % | 78 → 31 |', out)

    def test_json_is_unformatted(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1)])
        args = argparse.Namespace(split='2026-09-23', days=14, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(tokens.cmd_tokens(args, self.root), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data[0]['before_in'], 100)
        self.assertIsNone(data[0]['delta_pct'])


if __name__ == '__main__':
    unittest.main()
