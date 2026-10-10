"""A value with a newline (a CI log, a review finding) never makes a card unreadable: the writer
renders it on one line, quoted with ``\\n`` escapes, and reads it back exactly; the parser reads
the multi-line shape an older writer left on disk; ``asf record-repair`` rewrites those cards on
one line through the record's normal write path."""
import os
import subprocess
import sys
import tempfile
import unittest

from asf.record import frontmatter as fm
from asf.record import repair

LOG = ("red: tests (3.12) — step 'UNKNOWN STEP' failed:\n"
       "[command]/usr/bin/git config --local --name-only --get-regexp core\\.sshCommand\n"
       "env:\n  PARTS: 4\n"
       "sh -c \"git config --local --unset-all 'core.sshCommand' || :\"\n"
       "tab\there, a [bracket, a {brace, # not a comment\r\nend")
TRUNCATED = 'NEEDS OPERATOR: `python3 -c \\"from asf import env; rows = feeder_rows(env.load_pr…'

CARD = ('---\nid: T-0001\ntype: task\ntitle: one\n# ---- machine ----\nschema_version: 1\n'
        'state: New\n---\n## Description\n')

# what the old writer left on disk: the newline inside a quoted list element, raw
OLD_SHAPE = ('---\nid: T-0001\ntype: task\ntitle: one\n# ---- machine ----\nschema_version: 1\n'
             'state: Active\nkernel_findings: ["red: tests (3.12) — step \'UNKNOWN STEP\' failed:\n'
             'env:\n  PARTS: 4\n'
             '[command]/usr/bin/git submodule foreach --recursive sh -c \\"git config '
             '\'core\\\\.sshCommand\' || :\\"", "second"]\n'
             'kernel_fix_rounds: 1\n---\n## Description\n')
OLD_VALUE = ["red: tests (3.12) — step 'UNKNOWN STEP' failed:\nenv:\n  PARTS: 4\n"
             "[command]/usr/bin/git submodule foreach --recursive sh -c \"git config "
             "'core\\.sshCommand' || :\"", 'second']
# a one-line value whose quoted text holds an unbalanced bracket and an escaped quote
OLD_ONE_LINE = ('---\nid: F-0001\ntype: feature\ntitle: one\n# ---- machine ----\n'
                'kernel_notes: ["spec: `python3 -c \\"from x import y; rows = f(env.load_pr…", '
                '"PR #1305: red [not required"]\n---\n')

# an element an older reader split on an escaped quote, written back bare with a stray quote
# (F-0327): no strict reading exists, the list reads the way that older reader split it
STRAY_QUOTE = ('---\nid: F-0002\ntype: feature\ntitle: two\n# ---- machine ----\n'
               'kernel_notes: ["first", "\\"spec: -c \\\\\\"from asf import rows", dependents; '
               'rows = f(env.load_pr…", "PR #1305: red but not required — ignored"]\n'
               'kernel_fix_rounds: 3\n---\n')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _card(tmp, text, name='T-0001.md'):
    path = os.path.join(tmp, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


class Writer(unittest.TestCase):

    def test_every_written_value_is_one_line_and_reads_back_exactly(self):
        for value in (LOG, [LOG, 'plain', TRUNCATED, 'a, b', ''], TRUNCATED, 'x\\ny', '\\'):
            line = fm._format_entry('kernel_findings', value)
            self.assertNotIn('\n', line, value)
            meta, _ = fm.parse('---\n%s\n---\n' % line)
            self.assertEqual(meta['kernel_findings'], value)

    def test_merge_machine_round_trips_a_multi_line_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card(tmp, CARD)
            fm.merge_machine(path, {'kernel_findings': [LOG, TRUNCATED],
                                    'kernel_notes': [LOG]})
            text = _read(path)
            meta, _ = fm.parse(text)
            self.assertEqual(meta['kernel_findings'], [LOG, TRUNCATED])
            self.assertEqual(meta['kernel_notes'], [LOG])
            self.assertEqual(text.split('\n---\n')[0].count('\n'), CARD.split('\n---\n')[0]
                             .count('\n') + 2)
            self.assertFalse(fm.merge_machine(path, {'kernel_findings': [LOG, TRUNCATED]}))

    def test_block_lists_and_typed_writes_stay_one_line_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card(tmp, CARD)
            fm.merge_machine(path, {'evidence': [LOG]})
            fm.write_typed(path, {'note': LOG})
            meta, _ = fm.parse(_read(path))
            self.assertEqual((meta['evidence'], meta['note']), ([LOG], LOG))


class Parser(unittest.TestCase):

    def test_the_multi_line_shape_already_on_disk_reads(self):
        meta, body = fm.parse(OLD_SHAPE, path='T-0001.md')
        self.assertEqual(meta['kernel_findings'], OLD_VALUE)
        self.assertEqual(meta['kernel_fix_rounds'], 1)
        self.assertNotIn('env', meta)
        self.assertEqual(body, '## Description\n')
        self.assertEqual(fm.render(meta, body), OLD_SHAPE)  # unchanged: byte for byte

    def test_a_quoted_unbalanced_bracket_and_escaped_quote_read(self):
        meta, _ = fm.parse(OLD_ONE_LINE)
        self.assertEqual(meta['kernel_notes'],
                         ['spec: `python3 -c "from x import y; rows = f(env.load_pr…',
                          'PR #1305: red [not required'])

    def test_merge_machine_on_the_old_shape_replaces_the_whole_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card(tmp, OLD_SHAPE)
            fm.merge_machine(path, {'kernel_findings': ['fixed'], 'kernel_fix_rounds': 2})
            text = _read(path)
            self.assertNotIn('PARTS', text)
            meta, _ = fm.parse(text)
            self.assertEqual((meta['kernel_findings'], meta['kernel_fix_rounds']), (['fixed'], 2))

    def test_merge_machine_dropping_a_key_after_the_old_shape_keeps_it_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card(tmp, OLD_SHAPE)
            fm.merge_machine(path, {'stage': 'card'})
            meta, _ = fm.parse(_read(path))
            self.assertEqual(meta['kernel_findings'], OLD_VALUE)

    def test_a_stray_quote_an_older_reader_left_reads_leniently_and_normalizes(self):
        meta, _ = fm.parse(STRAY_QUOTE, path='F-0002.md')
        self.assertEqual(meta['kernel_fix_rounds'], 3)
        self.assertEqual(meta['kernel_notes'][0], 'first')
        self.assertIn('PR #1305: red but not required — ignored', meta['kernel_notes'][-1])
        fixed = fm.normalize(STRAY_QUOTE)
        self.assertNotEqual(fixed, STRAY_QUOTE)
        again, _ = fm.parse(fixed)
        self.assertEqual(again['kernel_notes'], meta['kernel_notes'])
        self.assertEqual(fm.normalize(fixed), fixed)

    def test_a_truly_unclosed_list_still_raises(self):
        with self.assertRaises(fm.FrontmatterError):
            fm.parse('---\nblockedBy: [F-0001, F-0002\n---\nbody\n', path='x.md')
        with self.assertRaises(fm.FrontmatterError):
            fm.parse('---\nnote: ["open\nid: x\n---\nbody\n', path='x.md')


class Repair(unittest.TestCase):

    def _record(self, tmp):
        for folder in ('tasks', 'features'):
            os.makedirs(os.path.join(tmp, folder))
        old = _card(os.path.join(tmp, 'tasks'), OLD_SHAPE)
        fine = _card(os.path.join(tmp, 'tasks'), CARD.replace('T-0001', 'T-0002'), 'T-0002.md')
        return old, fine

    def test_repair_rewrites_only_multi_line_values_on_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, fine = self._record(tmp)
            before_fine = _read(fine)
            self.assertEqual(repair.plan(tmp), ['tasks/T-0001.md'])
            self.assertEqual(repair.repair(tmp), ['tasks/T-0001.md'])
            text = _read(old)
            header = text.split('\n---\n')[0]
            self.assertEqual(len(header.split('\n')), 9)  # one line per key
            meta, body = fm.parse(text)
            self.assertEqual(meta['kernel_findings'], OLD_VALUE)
            self.assertEqual(meta['kernel_fix_rounds'], 1)
            self.assertEqual(body, '## Description\n')
            self.assertEqual(_read(fine), before_fine)
            self.assertEqual(repair.plan(tmp), [])
            self.assertEqual(repair.repair(tmp), [])

    def test_the_command_dry_runs_then_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, _fine = self._record(tmp)
            lines = []
            self.assertEqual(repair.run(tmp, apply=False, out=lines.append), 0)
            self.assertEqual(_read(old), OLD_SHAPE)
            self.assertIn('would rewrite tasks/T-0001.md', lines)
            self.assertEqual(repair.run(tmp, apply=True, out=lines.append), 0)
            self.assertNotEqual(_read(old), OLD_SHAPE)
            self.assertIn('rewrote tasks/T-0001.md', lines)


if __name__ == '__main__':
    unittest.main()
