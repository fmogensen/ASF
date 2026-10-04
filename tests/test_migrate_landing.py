"""``asf migrate-landing`` (W4-PR3a): stamp the Tasks/Bugs/Stories closed before the ``landing:``
stamp existed — from their own ``evidence:`` — with ``by: migration``; idempotent, and
``--revert`` removes those stamps and only those."""
import contextlib
import io
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf.cli import build_parser
from asf.record import frontmatter
from asf.tick import migrate_landing as ml

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'feature': 'features', 'story': 'stories', 'task': 'tasks', 'bug': 'bugs'}
BODY = ("## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n"
        "- 2026-01-01: created\n\n## Children\n\n## Backlinks\n")
A, B = 'a' * 40, 'b' * 40


def write(root, id_, type_, state, *machine, typed=()):
    lines = [f'id: {id_}', f'type: {type_}', f'title: {id_} title', *typed,
             '# ---- machine ----', 'schema_version: 1', f'state: {state}', *machine,
             'stage_since: 2026-01-03T04:05:06Z', 'updated: 2026-01-03T04:05:06Z']
    rel = f'{FOLDER_OF[type_]}/{id_}.md'
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n' + BODY)
    return rel


class MigrateLanding(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='migrate_landing_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        self.cards = {
            # a short sha the product repo spells in full
            'T-0001': write(self.root, 'T-0001', 'task', 'Closed',
                            'evidence: ["commit aaaaaaa names T-0001", "rule: landed-green"]'),
            # a full sha in a merged-PR line
            'T-0002': write(self.root, 'T-0002', 'task', 'Resolved',
                            f'evidence: ["PR #4 merged ({B})", "rule: landed"]'),
            # nothing derivable: sha '' and the pre-I14 note
            'B-0001': write(self.root, 'B-0001', 'bug', 'Closed',
                            'evidence: ["held Closed", "rule: closed-terminal"]'),
            'S-0001': write(self.root, 'S-0001', 'story', 'Closed',
                            'evidence: ["matrix status done (X-1)", "rule: matrix-done"]'),
            # left alone: open, already stamped, a Feature
            'T-0003': write(self.root, 'T-0003', 'task', 'Active',
                            'evidence: ["commit bbbbbbb names T-0003", "rule: landed"]'),
            'T-0004': write(self.root, 'T-0004', 'task', 'Closed',
                            f'landing: {{sha: {B}, as_of: 2026-01-02T00:00:00Z, by: names}}'),
            'F-0001': write(self.root, 'F-0001', 'feature', 'Closed',
                            'evidence: ["commit aaaaaaa names F-0001", "rule: landed-green"]'),
        }
        self.product = types.SimpleNamespace(repo_dir=self.root, main='main')

    def text(self):
        out = {}
        for iid, rel in self.cards.items():
            with open(os.path.join(self.root, rel), encoding='utf-8') as f:
                out[iid] = f.read()
        return out

    def landing(self, iid):
        with open(os.path.join(self.root, self.cards[iid]), encoding='utf-8') as f:
            meta = frontmatter.parse(f.read(), path=self.cards[iid])[0]
        return frontmatter.split_machine(meta)[1].get('landing')

    def run_it(self, **kw):
        lines = []
        with mock.patch('asf.evidence.evidence.full_shas',
                        lambda _p, shas: {s: A for s in shas if A.startswith(s)}), \
                contextlib.redirect_stdout(io.StringIO()):
            rc, n = ml.migrate(self.root, self.product, out=lines.append, **kw)
        return rc, n, lines

    def test_the_dry_run_writes_nothing(self):
        before = self.text()
        rc, n, lines = self.run_it()
        self.assertEqual((rc, n), (0, 0))
        self.assertEqual(self.text(), before)
        self.assertIn('migrate-landing: 4 card(s) would be stamped', lines[-1])

    def test_apply_stamps_each_closed_card_from_its_evidence(self):
        rc, n, _lines = self.run_it(apply=True)
        self.assertEqual((rc, n), (0, 4))
        self.assertEqual(self.landing('T-0001'),
                         {'sha': A, 'as_of': '2026-01-03T04:05:06Z', 'by': 'migration'})
        self.assertEqual(self.landing('T-0002')['sha'], B)
        self.assertEqual(self.landing('B-0001'), {'sha': '', 'as_of': '2026-01-03T04:05:06Z',
                                                  'by': 'migration', 'note': 'pre-I14'})
        self.assertEqual(self.landing('S-0001')['note'], 'pre-I14')
        self.assertIsNone(self.landing('T-0003'))
        self.assertEqual(self.landing('T-0004')['by'], 'names')
        self.assertIsNone(self.landing('F-0001'))

    def test_apply_twice_is_no_diff(self):
        self.run_it(apply=True)
        after = self.text()
        rc, n, _lines = self.run_it(apply=True)
        self.assertEqual((rc, n), (0, 0))
        self.assertEqual(self.text(), after)

    def test_revert_removes_the_migrations_stamps_and_only_those(self):
        before = self.text()
        self.run_it(apply=True)
        rc, n, _lines = self.run_it(revert=True)
        self.assertEqual((rc, n), (0, 4))
        self.assertEqual(self.text(), before)
        self.assertEqual(self.landing('T-0004')['by'], 'names')

    def test_the_stamped_record_passes_check(self):
        from asf.record import check
        from asf.record.core import canonicalize, load_items
        self.run_it(apply=True)
        canonical, _d = canonicalize(load_items(self.root)[0])
        found = []
        check.check_landing(canonical, lambda _r, _l, msg: found.append(msg), lambda _r, _k: 1)
        self.assertEqual(found, [])

    def test_the_command_is_registered(self):
        args = build_parser().parse_args(['migrate-landing', '--product', 'p', '--apply'])
        self.assertEqual((args.command, args.apply, args.revert), ('migrate-landing', True, False))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(['migrate-landing', '--apply', '--revert'])


if __name__ == '__main__':
    unittest.main()
