"""The product's gate reads its documents too: a spec or plan may not cite a retired name."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETIRED = ('count_words_v0',)


class DocsTests(unittest.TestCase):
    def test_documents_cite_no_retired_name(self):
        for folder in ('specs', 'plans'):
            for name in sorted(os.listdir(os.path.join(ROOT, folder))):
                with open(os.path.join(ROOT, folder, name), encoding='utf-8') as f:
                    text = f.read()
                for word in RETIRED:
                    self.assertNotIn(word, text, f'{folder}/{name} cites {word}')


if __name__ == '__main__':
    unittest.main()
