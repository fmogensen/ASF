import unittest

from src.count import count


class CountTests(unittest.TestCase):
    def test_words(self):
        self.assertEqual(count('one two  three'), 3)


if __name__ == '__main__':
    unittest.main()
