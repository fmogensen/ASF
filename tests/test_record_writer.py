"""asf.record.writer: every card write is one ``os.replace`` — a reader never sees half a card.

The record's writers opened the card with ``open(path, 'w')``: the file is truncated first and
filled after, so a reader between the two (a tick's ``load_items``, a session's ``asf show``,
``git add``) saw an empty or half-written card. The race test runs a real record writer
(``frontmatter.write_typed``) in a thread 500 times while the main thread reads the card back,
and requires every read to be one of the two whole texts. The lint test is
``tools/check_record_writers.sh``: no ``open(..., 'w')`` under ``asf/record/`` outside the writer.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest

from asf.record import frontmatter, writer

try:
    import pinned
except ImportError:  # pragma: no cover - import shape only
    from tests import pinned

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CARD = ("---\nid: T-0001\ntype: task\ntitle: one\nparent: \nafter: []\n"
        "# ---- machine ----\nschema_version: 1\nstate: New\n"
        "stage_since: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n---\n"
        "## Description\n\n" + "a long body line that makes the card span pages\n" * 400
        + "## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n- 2026-01-01: created\n\n"
        "## Children\n\n## Backlinks\n")


class TmpDir(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='asf-writer-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, 'tasks', 'T-0001.md')
        os.makedirs(os.path.dirname(self.path))


class ReaderNeverSeesATornCard(TmpDir):

    def test_a_reader_racing_a_record_writer_sees_one_whole_card_or_the_other(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(CARD)
        texts = set()
        done = threading.Event()

        def write():
            try:
                for i in range(500):
                    frontmatter.write_typed(self.path, {'title': 'two' if i % 2 else 'one'})
            finally:
                done.set()

        t = threading.Thread(target=write)
        t.start()
        torn = []
        while not done.is_set():
            with open(self.path, encoding='utf-8') as f:
                text = f.read()
            texts.add(text)
            if not text.endswith('## Backlinks\n'):
                torn.append(len(text))
        t.join()
        self.assertEqual(torn, [], f'{len(torn)} torn reads (lengths {sorted(set(torn))[:5]})')
        self.assertTrue(texts <= {CARD, CARD.replace('title: one', 'title: two')})

    def test_no_temp_file_is_left_beside_the_card(self):
        writer.write_card(self.path, CARD)
        writer.write_card(self.path, CARD)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ['T-0001.md'])

    def test_a_failed_write_leaves_the_old_card_and_no_temp_file(self):
        writer.write_card(self.path, CARD)
        with self.assertRaises(UnicodeEncodeError):
            writer.write_card(self.path, CARD + '\udcff')
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(f.read(), CARD)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ['T-0001.md'])


class SameBytesSameMode(TmpDir):

    def test_the_bytes_are_the_text_utf8_with_no_newline_translation(self):
        text = CARD.replace('one', 'naïve — ünïcode')
        writer.write_card(self.path, text)
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), text.encode('utf-8'))

    def test_an_existing_card_keeps_its_mode(self):
        writer.write_card(self.path, CARD)
        os.chmod(self.path, 0o640)
        writer.write_card(self.path, CARD + '\n')
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_a_new_card_gets_the_mode_open_would_give_it(self):
        writer.write_card(self.path, CARD)
        plain = os.path.join(self.dir, 'plain.md')
        with open(plain, 'w', encoding='utf-8') as f:
            f.write(CARD)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode),
                         stat.S_IMODE(os.stat(plain).st_mode))

    def test_a_symlinked_card_is_written_through_the_link(self):
        real = os.path.join(self.dir, 'real.md')
        writer.write_card(real, CARD)
        os.symlink(real, self.path)
        writer.write_card(self.path, CARD + 'x\n')
        self.assertTrue(os.path.islink(self.path))
        with open(real, encoding='utf-8') as f:
            self.assertEqual(f.read(), CARD + 'x\n')

    def test_the_directory_is_created(self):
        path = os.path.join(self.dir, 'bugs', 'B-0001.md')
        writer.write_card(path, CARD)
        self.assertTrue(os.path.isfile(path))


class OneWriterLint(unittest.TestCase):

    def test_no_record_module_opens_a_file_for_writing_outside_the_writer(self):
        done = subprocess.run(['bash', os.path.join(ROOT, 'tools', 'check_record_writers.sh')],
                              capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_the_lint_fails_on_an_in_place_writer(self):
        tree = tempfile.mkdtemp(prefix='asf-writer-lint-')
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        os.makedirs(os.path.join(tree, 'asf', 'record'))
        os.makedirs(os.path.join(tree, 'tools'))
        shutil.copy(os.path.join(ROOT, 'tools', 'check_record_writers.sh'),
                    os.path.join(tree, 'tools'))
        with open(os.path.join(tree, 'asf', 'record', 'x.py'), 'w', encoding='utf-8') as f:
            f.write("def f(p, t):\n    with open(p, 'w', encoding='utf-8') as f:\n"
                    "        f.write(t)\n")
        done = subprocess.run(['bash', os.path.join(tree, 'tools', 'check_record_writers.sh')],
                              capture_output=True, text=True, cwd=tree, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('asf/record/x.py:2', done.stdout + done.stderr)


class PinnedReaderLoadsAWrittenCard(TmpDir):
    """The pinned venv's loader parses a card this writer wrote (the bytes are unchanged)."""

    CODE = r'''
import os, sys, tempfile
from asf.record.core import load_items
root = tempfile.mkdtemp()
for d in ("epics", "features", "stories", "tasks", "bugs", "decisions", "rules"):
    os.makedirs(os.path.join(root, d))
with open(os.path.join(root, "tasks", "T-0001.md"), "wb") as f:
    f.write(sys.stdin.buffer.read())
by_id, errors = load_items(root)
assert not errors, errors
print(by_id["T-0001"][0]["meta"]["title"])
'''

    def test_the_pinned_reader(self):
        writer.write_card(self.path, CARD)
        frontmatter.write_typed(self.path, {'title': 'two'})
        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        for sha in pinned.pinned_shas():
            done = pinned.run_pinned(sha, self.CODE, stdin=text)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), 'two')


if __name__ == '__main__':
    unittest.main()
