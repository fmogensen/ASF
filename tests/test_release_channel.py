import unittest

from asf import env


class ProductUpgradePolicyTests(unittest.TestCase):
    """F-0112/T-0431: the ``conventions.flags.upgrade`` switch's three words, its default, and
    the raw word the doctor must quote back for a typo."""

    def _product(self, declared):
        data = {'repo_slug': 'a/b'}
        if declared is not None:
            data['conventions'] = {'flags': {'upgrade': declared}}
        return env.Product('p', data)

    def test_the_three_words_round_trip(self):
        for word in env.UPGRADE_POLICIES:
            self.assertEqual(self._product(word).upgrade, word)

    def test_case_and_whitespace_are_folded(self):
        for declared in ('AUTO', ' auto ', 'Auto'):
            self.assertEqual(self._product(declared).upgrade, 'auto')

    def test_a_typo_reads_as_notify_and_is_quoted_back_raw(self):
        product = self._product('atuo')
        self.assertEqual(product.upgrade, env.UPGRADE_DEFAULT)
        self.assertEqual(product.upgrade_declared, 'atuo')

    def test_an_absent_key_reads_notify_and_declares_none(self):
        product = self._product(None)
        self.assertEqual(product.upgrade, env.UPGRADE_DEFAULT)
        self.assertIsNone(product.upgrade_declared)


if __name__ == '__main__':
    unittest.main()
