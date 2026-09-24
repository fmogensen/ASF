import json
import os
import shutil
import types
import unittest
from unittest import mock

from asf import schema
from asf.init import STREAM_FOLDERS
from asf.record import frontmatter
from tests.test_backlog import make_repo, run, write_item


def card_version(root, folder, id_):
    with open(os.path.join(root, folder, f'{id_}.md'), encoding='utf-8') as f:
        meta, _body = frontmatter.parse(f.read(), path=f'{id_}.md')
    return meta.get('schema_version')


class RecordCarriesSchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        # B-0005: `check` now requires the layout's stream folders; this test is about the stamp
        for f in STREAM_FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_index_and_every_card_carry_schema_version(self):
        """B-0004: a record written from scratch is stamped — index.json and each card — and
        `check` fails a card without a stamp; the schema migration stamps the cards lacking one."""
        machine = ['schema_version: %s' % schema.SCHEMA_VERSION, 'state: New',
                   'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z']
        write_item(self.root, 'E-0001', 'epic', 'Ship it', machine_lines=machine)
        write_item(self.root, 'F-0001', 'feature', 'Shipping', parent='E-0001', machine_lines=machine)
        r = run(['new', 'story', '--title', 'Ship it', '--parent', 'F-0001', '--acceptance', 'x'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(card_version(self.root, 'stories', 'S-0001'), schema.SCHEMA_VERSION)

        r = run(['index'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['schema_version'], schema.SCHEMA_VERSION)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

        # an unstamped card is a finding
        write_item(self.root, 'E-0002', 'epic', 'Unstamped',
                   machine_lines=('state: New', 'stage_since: 2026-01-01T00:00:00Z',
                                  'updated: 2026-01-01T00:00:00Z'))
        self.assertIsNone(card_version(self.root, 'epics', 'E-0002'))
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn('E-0002.md:1: missing schema_version', r.stdout)

        # the migration reads the stamp: it fills the card that lacks one, leaves the rest be
        schema.stamp_cards(self.root, schema.SCHEMA_VERSION)
        self.assertEqual(card_version(self.root, 'epics', 'E-0002'), schema.SCHEMA_VERSION)
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)


class IngestKeepsTheStamp(unittest.TestCase):
    """The record step rewrote every card's machine block from the keys it derives and dropped
    ``schema_version`` — a migrated record then failed `check` everywhere. Ingest carries every
    machine key it does not derive, and a migrated record's stripped cards come back by themselves
    on the next ingest (no hand rerun of the migration, which stays safe to rerun)."""

    UNSTAMPED = ('state: New', 'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z')

    def setUp(self):
        self.root = make_repo()
        for f in STREAM_FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Ship it', machine_lines=self.UNSTAMPED)
        write_item(self.root, 'F-0001', 'feature', 'Shipping', parent='E-0001',
                   machine_lines=self.UNSTAMPED + ('spend_usd: 1.5',))
        with open(os.path.join(self.root, 'index.json'), 'w', encoding='utf-8') as f:
            f.write('{"items": {}}\n')  # a record from before the stamp: schema 0
        self.assertEqual(schema.record_version(self.root), 0)

    def ingest(self):
        from asf.record import ingest
        from tests.test_ingest import EMPTY_EV
        with mock.patch.object(ingest.evidence, 'load', return_value=dict(EMPTY_EV, ids={})):
            self.assertEqual(ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root), 0)

    def assert_stamped_and_clean(self):
        for folder, iid in (('epics', 'E-0001'), ('features', 'F-0001')):
            self.assertEqual(card_version(self.root, folder, iid), schema.SCHEMA_VERSION, iid)
        r = run(['check'], self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_migrate_then_ingest_keeps_schema_version(self):
        schema.migrate_dir(self.root)
        self.ingest()
        self.assert_stamped_and_clean()
        with open(os.path.join(self.root, 'features', 'F-0001.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('rule: ', text)          # ingest did rewrite the machine block ...
        self.assertIn('spend_usd: 1.5', text)  # ... and kept the key it does not derive
        self.assertEqual(schema.migrate_dir(self.root), [])  # rerunning the migration: a no-op

    def test_a_stripped_migrated_record_is_restored_by_the_next_ingest(self):
        schema.migrate_dir(self.root)
        for folder, iid in (('epics', 'E-0001'), ('features', 'F-0001')):  # what the old ingest did
            path = os.path.join(self.root, folder, f'{iid}.md')
            with open(path, encoding='utf-8') as f:
                meta, _b = frontmatter.parse(f.read(), path=path)
            _typed, machine = frontmatter.split_machine(meta)
            machine.pop('schema_version')
            frontmatter.write_machine(path, machine)
            self.assertIsNone(card_version(self.root, folder, iid))
        self.ingest()
        self.assert_stamped_and_clean()

    def test_an_unmigrated_record_is_left_for_the_migration(self):
        self.ingest()
        self.assertIsNone(card_version(self.root, 'epics', 'E-0001'))


if __name__ == '__main__':
    unittest.main()
