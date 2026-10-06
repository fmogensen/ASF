"""tests.test_retire — ``asf retire <item> --why "…" [--landed <ref>…]``: an item that landed by
hand is taken off the board through the record's own publish path, with no hand edit.

A groomed card gets ``removed:`` (and ``landed:`` for a sha) written through the ``asf set``
writer, a History line, and one signed-off record commit pushed to origin with the index
regenerated. An inbox note nobody groomed yet moves to ``<intake>/done/`` with the reason in
it. Either way the feeder has no row for it afterwards, and the groom never mints it again — not
from the moved note, and not from the same note filed a second time."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf.env import Product
from asf.feeder import rows
from asf.record import frontmatter
from asf.views import index_reader
from tests.test_backlog import FOLDERS, run, write_item

DATE = '2026-10-06'
SIG = 'TypeError: cannot read property of undefined'


def git(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def product():
    return Product('sample', {'conventions': {
        'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}})


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='retire_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.root = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@x')):
            git(self.root, 'config', k, v)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f), exist_ok=True)
            with open(os.path.join(self.root, f, '.keep'), 'w', encoding='utf-8'):
                pass
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        self.bug = write_item(self.root, 'B-0001', 'bug', 'Crash on load', parent='E-0001',
                              typed_lines=('severity: S2', 'decided: true', 'found_in: dev',
                                           f'signature: "{SIG}"'))
        r = run(['index'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.commit('seed')

    def commit(self, message):
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', message)
        git(self.root, 'push', '-q', 'origin', 'HEAD:main')

    def inbox(self, name, text):
        d = os.path.join(self.root, 'inbox')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
            f.write(text)
        self.commit(f'inbox {name}')

    def meta(self, path):
        with open(path, encoding='utf-8') as f:
            return frontmatter.parse(f.read())

    def published(self):
        """The checkout is clean and origin has what it committed."""
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'),
                         git(self.origin, 'rev-parse', 'main'))

    def feeder_ids(self):
        items, _generated = index_reader.load(self.root)
        return {r.item_id for r in rows.candidates(items, product(), [])}

    def cards(self):
        out = set()
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            out |= {n for n in os.listdir(d) if n.endswith('.md')}
        return out

    def groom(self):
        r = run(['groom', '--date', DATE], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    # ---- a groomed card ------------------------------------------------------------------

    def test_retire_a_groomed_card_writes_removed_and_landed_through_the_publish_path(self):
        self.assertIn('B-0001', self.feeder_ids())
        r = run(['retire', 'B-0001', '--why', 'fixed by hand', '--landed', '123',
                 '--landed', 'abc1234'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        meta, body = self.meta(self.bug)
        self.assertIn('fixed by hand', meta['removed'])
        self.assertIn('#123', meta['removed'])
        self.assertEqual(meta['landed'], 'abc1234')
        self.assertIn('retired', body.split('## History', 1)[1])
        # one signed-off record commit, pushed; the index regenerated in it
        self.published()
        log = git(self.root, 'log', '-1', '--format=%B')
        self.assertIn('record: retire B-0001', log)
        self.assertIn('Signed-off-by', log)
        self.assertIn('index.json', git(self.root, 'show', '--name-only', '--format=', 'HEAD'))
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            self.assertTrue(json.load(f)['items']['B-0001'].get('removed'))
        # the feeder has no row for it
        self.assertNotIn('B-0001', self.feeder_ids())

    def test_retire_refuses_without_a_reason_or_an_unknown_item(self):
        r = run(['retire', 'B-0001', '--why', '  '], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn('removed', self.meta(self.bug)[0])
        r = run(['retire', 'B-0999', '--why', 'gone'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('B-0999', r.stderr)

    def test_retire_twice_is_refused(self):
        self.assertEqual(run(['retire', 'B-0001', '--why', 'done'], self.root).returncode, 0)
        r = run(['retire', 'B-0001', '--why', 'again'], self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn('already', r.stderr)

    def test_a_retired_cards_signature_is_never_minted_again_by_the_groom(self):
        self.assertEqual(run(['retire', 'B-0001', '--why', 'fixed by hand'], self.root).returncode, 0)
        before = self.cards()
        self.inbox('crash-again.md', f'# Crash on load again\nparent: E-0001\nsignature: {SIG}\n\nSeen again.\n')
        self.groom()
        self.assertEqual(self.cards(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'inbox', 'crash-again.md')))
        with open(os.path.join(self.root, 'inbox', 'done', 'crash-again.md'), encoding='utf-8') as f:
            self.assertIn('B-0001', f.read())

    # ---- an inbox note nobody groomed ----------------------------------------------------

    NOTE = '# Speed up the slow export\nparent: E-0001\n\nThe export takes a minute.\n'

    def test_retire_an_inbox_note_moves_it_to_done_with_the_reason(self):
        self.inbox('speed-up-the-slow-export.md', self.NOTE)
        r = run(['retire', 'speed-up-the-slow-export.md', '--why', 'landed by hand',
                 '--landed', '77'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.root, 'inbox', 'speed-up-the-slow-export.md')))
        with open(os.path.join(self.root, 'inbox', 'done', 'speed-up-the-slow-export.md'),
                  encoding='utf-8') as f:
            done = f.read()
        self.assertIn('retired', done)
        self.assertIn('landed by hand', done)
        self.assertIn('#77', done)
        self.assertIn(self.NOTE, done)
        self.published()
        self.assertIn('record: retire', git(self.root, 'log', '-1', '--format=%B'))

    def test_an_inbox_note_is_found_by_its_path_or_its_bare_name(self):
        for i, ref in enumerate(('inbox/note-a.md', 'note-b')):
            name = 'note-a.md' if i == 0 else 'note-b.md'
            self.inbox(name, f'# Note {i}\n\nbody\n')
            r = run(['retire', ref, '--why', 'not needed'], self.root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(os.path.isfile(os.path.join(self.root, 'inbox', 'done', name)))

    def test_a_retired_inbox_note_is_never_minted_by_the_groom(self):
        self.inbox('speed-up-the-slow-export.md', self.NOTE)
        self.assertEqual(run(['retire', 'speed-up-the-slow-export', '--why', 'landed by hand'],
                             self.root).returncode, 0)
        before = self.cards()
        self.groom()
        self.assertEqual(self.cards(), before)
        # the same note filed again is not minted either: it was retired once already
        self.inbox('speed-up-the-slow-export.md', self.NOTE)
        self.groom()
        self.assertEqual(self.cards(), before)
        self.assertNotIn('speed-up-the-slow-export.md', os.listdir(os.path.join(self.root, 'inbox')))
        self.assertEqual(self.feeder_ids() - {'B-0001'}, set())

    def test_an_unretired_note_is_still_minted(self):
        self.inbox('speed-up-the-slow-export.md', self.NOTE)
        before = self.cards()
        self.groom()
        self.assertEqual(len(self.cards() - before), 1)


class SetRemovedTests(unittest.TestCase):
    """``asf set <id> removed=<reason>`` is the field-level form of the same write."""

    def test_set_removed_takes_a_reason(self):
        from tests.test_backlog import make_repo
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write_item(root, 'E-0001', 'epic', 'Factory')
        path = write_item(root, 'F-0001', 'feature', 'Feat', parent='E-0001')
        r = run(['set', 'F-0001', 'removed=landed by hand'], root)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path, encoding='utf-8') as f:
            self.assertEqual(frontmatter.parse(f.read())[0]['removed'], 'landed by hand')


if __name__ == '__main__':
    unittest.main()
