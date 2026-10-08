import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import env, schema
from asf.init import STREAM_FOLDERS
from asf.record import frontmatter
from tests.test_backlog import make_repo, run, write_item
from tests.test_install import HomeCase, SchemaTest


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


class RequireRefusesOnlyANonAdditiveGapTests(SchemaTest):
    """F-0114 §1, the card's first acceptance clause: ``require`` refuses on a non-additive gap,
    or a file stamped newer than the package, and never on an additive one or an absent stamp."""

    def test_matching_passes(self):
        self.assertTrue(schema.require(self.product))

    def test_additive_gap_passes_and_writes_nothing(self):
        self.write(os.path.join(self.operator, 'index.json'), '{"items": {}}')  # unstamped: 0
        before = self.origin_log()
        self.assertTrue(schema.require(self.product))
        self.assertEqual(self.origin_log(), before)  # require only checks; it never migrates

    def test_non_additive_record_gap_exits_3_with_the_operator_line(self):
        def to_2(record_dir):
            pass
        with mock.patch.object(schema, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(schema.MIGRATIONS,
                                {2: schema.Migration(to_2, additive=False, note='x')}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                schema.require(self.product)
        self.assertEqual(cm.exception.code, schema.EXIT_MISMATCH)
        self.assertRegex(err.getvalue(), r'^NEEDS OPERATOR: run asf schema-migrate — ')

    def test_non_additive_config_gap_exits_3_independently_of_the_record(self):
        self.write(env.config_path(), 'default_product: sample\n')  # config: absent -> 0

        def to_2(paths):
            pass
        with mock.patch.object(schema, 'CONFIG_VERSION', 2), \
                mock.patch.dict(schema.CONFIG_MIGRATIONS,
                                {1: schema.CONFIG_MIGRATIONS[1],
                                 2: schema.Migration(to_2, additive=False, note='x')}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                schema.require(self.product)
        self.assertEqual(cm.exception.code, schema.EXIT_MISMATCH)
        self.assertIn('NEEDS OPERATOR: run asf schema-migrate — ', err.getvalue())

    def test_a_newer_record_exits_3_with_migrate_dirs_forward_only_wording(self):
        self.write(os.path.join(self.operator, 'index.json'), '{"items": {}, "schema_version": 9}')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            schema.require(self.product)
        self.assertEqual(cm.exception.code, schema.EXIT_MISMATCH)
        self.assertIn('forward-only', err.getvalue())

    def test_an_absent_config_stamp_never_refuses(self):
        self.write(env.config_path(), 'default_product: sample\n')  # no schema_version at all
        self.assertTrue(schema.require(self.product))


class ConfigMigrationTests(HomeCase):
    """F-0114 §1's config fence (its ``tests.test_env.ConfigSchemaVersionTests`` half lands with
    the reader, in a later card): :func:`schema.pending`, :func:`schema.migrate_config` and
    their snapshot/restore over ``config.yaml`` and every ``products/*.yaml``."""

    def setUp(self):
        super().setUp()
        self.write(env.config_path(), '# operator notes\nfoo: bar\n')
        self.write(env.product_path('alpha'), '# alpha notes\nrepo_slug: a/a\n')
        self.write(env.product_path('beta'), '# beta notes\nrepo_slug: b/b\n')
        self.no_record = env.Product('none', {})  # a product with no record_dirs of its own

    def _read_all(self):
        out = {}
        for p in schema.config_files():
            with open(p, encoding='utf-8') as f:
                out[p] = f.read()
        return out

    def test_pending_is_one_additive_step_over_every_unstamped_file(self):
        self.assertEqual(schema.pending(self.no_record)['config'],
                         [(0, 1, schema.CONFIG_MIGRATIONS[1])])

    def test_migrate_config_stamps_all_three_byte_identical_otherwise(self):
        before = self._read_all()
        applied = schema.migrate_config()
        self.assertEqual(applied, [(0, 1, schema.CONFIG_MIGRATIONS[1])])
        for p, old in before.items():
            with open(p, encoding='utf-8') as f:
                text = f.read()
            self.assertEqual(text, 'schema_version: 1\n' + old)  # the stamp lands as the first key

    def test_migrate_config_is_idempotent(self):
        schema.migrate_config()
        self.assertEqual(schema.migrate_config(), [])

    def test_non_additive_entry_snapshots_first_and_the_snapshot_holds_the_pre_step_bytes(self):
        before = self._read_all()

        def to_2(paths):
            pass
        with mock.patch.object(schema, 'CONFIG_VERSION', 2), \
                mock.patch.dict(schema.CONFIG_MIGRATIONS,
                                {1: schema.CONFIG_MIGRATIONS[1],
                                 2: schema.Migration(to_2, additive=False, note='x')}):
            schema.migrate_config()
        backups = schema.snapshots()
        self.assertEqual(len(backups), 1)
        version, _stamp, backup_dir = backups[0]
        self.assertEqual(version, 1)  # the step landing at 2 snapshots the state before it: 1
        for p, old in before.items():
            with open(os.path.join(backup_dir, os.path.basename(p)), encoding='utf-8') as f:
                self.assertEqual(f.read(), 'schema_version: 1\n' + old)

    def test_restore_puts_the_backup_back(self):
        before = self._read_all()

        def to_2(paths):
            pass
        with mock.patch.object(schema, 'CONFIG_VERSION', 2), \
                mock.patch.dict(schema.CONFIG_MIGRATIONS,
                                {1: schema.CONFIG_MIGRATIONS[1],
                                 2: schema.Migration(to_2, additive=False, note='x')}):
            schema.migrate_config()
            self.assertIsNotNone(schema.restore(None, 1))
        for p, old in before.items():
            with open(p, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'schema_version: 1\n' + old)


class EveryMigrationIsAdditiveOrBumpsTheMinorTests(HomeCase):
    """D10 as a fence over the tables, not over one run: every entry today is additive, and a
    non-additive entry's ``apply`` is reached only through a code path that snapshots first."""

    def test_every_entry_today_is_additive(self):
        self.assertTrue(all(m.additive for m in schema.MIGRATIONS.values()), schema.MIGRATIONS)
        self.assertTrue(all(m.additive for m in schema.CONFIG_MIGRATIONS.values()),
                        schema.CONFIG_MIGRATIONS)

    def test_a_fabricated_non_additive_entry_is_snapshotted_before_its_apply_runs(self):
        self.write(env.config_path(), 'schema_version: 1\n')
        order = []
        real_snapshot = schema.snapshot

        def spy(product, version):
            order.append('snapshot')
            return real_snapshot(product, version)

        def to_2(paths):
            order.append('apply')
        with mock.patch.object(schema, 'CONFIG_VERSION', 2), \
                mock.patch.object(schema, 'snapshot', side_effect=spy), \
                mock.patch.dict(schema.CONFIG_MIGRATIONS,
                                {2: schema.Migration(to_2, additive=False, note='x')}):
            schema.migrate_config()
        self.assertEqual(order, ['snapshot', 'apply'])

    def test_an_additive_entry_never_snapshots(self):
        self.write(env.config_path(), 'schema_version: 1\n')

        def to_2(paths):
            pass
        with mock.patch.object(schema, 'CONFIG_VERSION', 2), \
                mock.patch.object(schema, 'snapshot') as snap, \
                mock.patch.dict(schema.CONFIG_MIGRATIONS,
                                {2: schema.Migration(to_2, additive=True, note='x')}):
            schema.migrate_config()
        snap.assert_not_called()


if __name__ == '__main__':
    unittest.main()
