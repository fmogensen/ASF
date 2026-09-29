"""F-0187 — ``conventions.shared_paths`` (and ``shared_regenerate``): the one reader of the key
and the exemption that stops at the shared set.

``SharedGlobsTests`` and ``IsSharedTests`` are Task 1's — the accessor and the narrowed
predicate. ``RecordCheckTests`` (Task 2), ``SharedSerialisationTests`` (Task 4) and
``RegenerateSharedTests`` (Task 5) extend this module as the plan's later Tasks land.
"""
import os
import shutil
import unittest

from asf import env
from asf.conventions import Conventions
from asf.feeder import footprint


class SharedGlobsTests(unittest.TestCase):
    def test_a_product_answers_through_its_conventions(self):
        product = env.Product('p', {'conventions': {'shared_paths': ['uv.lock']}})
        self.assertEqual(footprint.shared_globs(product), ('uv.lock',))

    def test_a_conventions_object_answers_from_its_field(self):
        conv = Conventions(shared_paths=['uv.lock'])
        self.assertEqual(footprint.shared_globs(conv), ('uv.lock',))

    def test_a_plain_mapping_answers_the_same(self):
        self.assertEqual(footprint.shared_globs({'shared_paths': ['uv.lock']}), ('uv.lock',))

    def test_none_is_empty(self):
        self.assertEqual(footprint.shared_globs(None), ())

    def test_a_product_with_no_conventions_block_is_empty(self):
        product = env.Product('p', {})
        self.assertEqual(footprint.shared_globs(product), ())

    def test_a_product_declaring_none_is_empty(self):
        product = env.Product('p', {'conventions': {'shared_paths': []}})
        self.assertEqual(footprint.shared_globs(product), ())


class IsSharedTests(unittest.TestCase):
    def test_a_literal_glob_matches_itself(self):
        self.assertTrue(footprint.is_shared('uv.lock', ['uv.lock']))

    def test_a_narrower_glob_is_covered_by_a_wider_shared_glob(self):
        self.assertTrue(footprint.is_shared('apps/web/package-lock.json',
                                            ['**/package-lock.json']))

    def test_star_is_not_shared_by_a_declared_lockfile(self):
        # P9: matched both ways, `*` would be covered by any declared lockfile — the footprint
        # gate off for the widest footprint there is.
        self.assertFalse(footprint.is_shared('*', ['uv.lock']))

    def test_double_star_is_not_shared_by_a_declared_lockfile(self):
        self.assertFalse(footprint.is_shared('**', ['uv.lock']))

    def test_a_glob_wider_than_the_shared_set_is_not_shared(self):
        self.assertFalse(footprint.is_shared('*.lock', ['uv.lock']))

    def test_overlaps_still_catches_a_wildcard_writer(self):
        # the regression the card's exemption would otherwise open (P9)
        self.assertEqual(footprint.overlaps(['*'], ['src/a.py'], shared=['uv.lock']),
                         ('*', 'src/a.py'))

    def test_empty_shared_set_shares_nothing(self):
        self.assertFalse(footprint.is_shared('x', ()))
        self.assertFalse(footprint.is_shared('x', None))


class RecordCheckTests(unittest.TestCase):
    """Task 2, S-37302: ``record_findings``'s Active-pair loop skips a shared glob on either
    side — the record half of ``asf check`` and I3 agreeing with the feeder. ``SharedPathI3Tests``
    (``tests/test_record_stage.py``) is the I3 / ``set_typed`` half."""

    def setUp(self):
        from asf.init import STREAM_FOLDERS
        from tests.test_record_stage import make_record, write
        self.root = make_record()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for folder in STREAM_FOLDERS:
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
        write(self.root, 'E-0001', 'epic')
        write(self.root, 'F-0001', 'feature', parent='E-0001')
        write(self.root, 'T-0001', 'task', parent='F-0001', typed=('writes: [src/a.py, uv.lock]',),
              machine=('schema_version: 1', 'state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                       'updated: 2026-01-01T00:00:00Z'))
        write(self.root, 'T-0002', 'task', parent='F-0001', typed=('writes: [src/b.py, uv.lock]',),
              machine=('schema_version: 1', 'state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                       'updated: 2026-01-01T00:00:00Z'))
        from asf.record.index import do_index
        do_index(self.root)

    def test_two_active_tasks_sharing_only_the_lockfile_are_no_overlap(self):
        from asf.record.check import record_findings
        findings, _w, _iw = record_findings(self.root, shared=('uv.lock',))
        self.assertFalse(any('intersects Active task' in msg for _p, _l, msg in findings), findings)
        # no product: today's answer, which refuses (D4)
        findings, _w, _iw = record_findings(self.root)
        self.assertTrue(any('intersects Active task' in msg for _p, _l, msg in findings), findings)
