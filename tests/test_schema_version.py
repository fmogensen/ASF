import json
import os
import shutil
import unittest

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


if __name__ == '__main__':
    unittest.main()
