"""tests.test_approvals — F-0031, Task 1: the catalogue, the matrix, the classifier and the hold
ledger. ``CatalogueTest`` and ``MatrixTest`` are the plan's A1; ``LedgerTest`` is the unit
coverage behind D9/D14 the plan asks this Task to add.
"""
import os
import shutil
import tempfile
import unittest

from asf import approvals, env
from asf.env import Product

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _product(name='demo', **data):
    return Product(name, data)


class CatalogueTest(unittest.TestCase):
    def test_every_class_is_named_and_mapped_in_the_example_and_sample(self):
        names = {c.name for c in approvals.CLASSES}
        for path in (
            os.path.join(REPO_ROOT, 'docs', 'products.example.yaml'),
            os.path.join(REPO_ROOT, 'sample', 'product.yaml'),
        ):
            with open(path, encoding='utf-8') as f:
                data = env.loads(f.read())
            product = Product('x', data)
            self.assertEqual(set(product.approvals.keys()), names, path)
            for level in product.approvals.values():
                self.assertIn(level, approvals.LEVELS, path)

    def test_every_class_has_a_read_point_and_a_known_default(self):
        known_readers = {'hook', 'harvest', 'file_bugs'}
        for c in approvals.CLASSES:
            self.assertIn(c.default, approvals.LEVELS, c.name)
            self.assertTrue(c.read_by, c.name)
            self.assertTrue(set(c.read_by) <= known_readers, c.name)


class MatrixTest(unittest.TestCase):
    def test_unmapped_class_takes_its_catalogue_default(self):
        product = _product(approvals={'merge_routine_pr': 'groom'})
        m = approvals.matrix(product)
        self.assertEqual(m['merge_routine_pr'], ('groom', 'yaml'))
        for c in approvals.CLASSES:
            if c.name == 'merge_routine_pr':
                continue
            self.assertEqual(m[c.name], (c.default, 'default'), c.name)

    def test_unknown_class_or_level_is_a_config_error(self):
        with self.assertRaises(env.ConfigError):
            approvals.matrix(_product(approvals={'frobnicate': 'auto'}))
        with self.assertRaises(env.ConfigError):
            approvals.matrix(_product(approvals={'touch_production': 'maybe'}))

    def test_signal_under_an_unknown_class_is_a_config_error(self):
        with self.assertRaises(env.ConfigError):
            approvals.signals(_product(approval_signals={'not_a_class': {'paths': ['x']}}))
        with self.assertRaises(env.ConfigError):
            approvals.signals(_product(approval_signals={'spend_money': {'globs': ['x']}}))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = 'demo'

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _refuse(self):
        return approvals.refuse(
            self.product, 'F-0031', 'touch_production', 'human-now', 'code-F-0031',
            'hook', 'git push origin HEAD:main')

    def test_refuse_opens_a_hold(self):
        hold = self._refuse()
        self.assertEqual(hold, 'F-0031/touch_production')
        self.assertEqual([h['hold'] for h in approvals.open_holds(self.product)], [hold])
        self.assertFalse(approvals.is_granted(self.product, hold))
        entry = approvals.holds(self.product)[hold]
        self.assertEqual(entry['item'], 'F-0031')
        self.assertEqual(entry['class'], 'touch_production')
        self.assertEqual(entry['count'], 1)
        self.assertIsNone(entry['resolution'])

    def test_resolve_closes_the_hold(self):
        hold = self._refuse()
        approvals.resolve(self.product, hold, 'granted')
        self.assertEqual(approvals.open_holds(self.product), [])
        self.assertTrue(approvals.is_granted(self.product, hold))

    def test_repeat_refusal_bumps_count(self):
        hold = self._refuse()
        self._refuse()
        self.assertEqual(approvals.holds(self.product)[hold]['count'], 2)

    def test_resolve_of_an_invalid_resolution_is_a_value_error(self):
        hold = self._refuse()
        with self.assertRaises(ValueError):
            approvals.resolve(self.product, hold, 'maybe')

    def test_a_malformed_line_is_skipped(self):
        hold = self._refuse()
        with open(approvals.ledger_path(self.product), 'a', encoding='utf-8') as f:
            f.write('not json\n')
        self.assertEqual([h['hold'] for h in approvals.open_holds(self.product)], [hold])


if __name__ == '__main__':
    unittest.main()
