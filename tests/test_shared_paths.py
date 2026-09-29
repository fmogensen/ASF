"""F-0187 — ``conventions.shared_paths`` (and ``shared_regenerate``): the one reader of the key
and the exemption that stops at the shared set.

``SharedGlobsTests`` and ``IsSharedTests`` are Task 1's — the accessor and the narrowed
predicate. ``RecordCheckTests`` (Task 2), ``SharedSerialisationTests`` (Task 4) and
``RegenerateSharedTests`` (Task 5) extend this module as the plan's later Tasks land.
"""
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
