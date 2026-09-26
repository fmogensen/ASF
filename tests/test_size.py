"""asf.size — the classifier: a footprint and a threshold table become a class, a kind and a
sentence (F-0041 §2.1, §3.1)."""
import unittest

from asf import size


def files(n, prefix='asf/mod'):
    return [f'{prefix}{i}.py' for i in range(n)]


class ClassesAndKindsTests(unittest.TestCase):
    """The names the rest of the repository may switch on."""

    def test_classes(self):
        self.assertEqual(size.CLASSES, (size.SMALL, size.MEDIUM, size.LARGE))
        self.assertEqual(size.CLASSES, ('small', 'medium', 'large'))

    def test_kinds(self):
        self.assertEqual(size.KINDS, ('docs', 'test', 'config', 'copy', 'code'))


class CountableTests(unittest.TestCase):

    def test_none_footprint(self):
        self.assertEqual(size.countable(None), (None, 'no writes: footprint'))

    def test_empty_footprint(self):
        self.assertEqual(size.countable([]), (None, 'no writes: footprint'))

    def test_literal_entries_are_counted(self):
        self.assertEqual(size.countable(['a.py', 'b.py', 'c.py']), (3, None))

    def test_wildcard_entry_is_not_countable(self):
        n, why = size.countable(['a.py', 'b.py', 'asf/**'])
        self.assertIsNone(n)
        self.assertEqual(why, "'asf/**' is a glob, not a file")

    def test_cross_repo_entry_is_not_countable(self):
        n, why = size.countable(['a.py', 'record:features/**'])
        self.assertIsNone(n)
        self.assertEqual(why, "'record:features/**' names another repository")


class KindOfTests(unittest.TestCase):

    def test_all_documents_is_docs(self):
        writes = ['docs/a.md', 'docs/b.md']
        self.assertEqual(size.kind_of(writes, {'docs': ['docs/**']}), 'docs')

    def test_nine_docs_and_one_module_is_code(self):
        writes = [f'docs/{i}.md' for i in range(9)] + ['asf/mod.py']
        self.assertEqual(size.kind_of(writes, {'docs': ['docs/**']}), 'code')

    def test_no_glob_matches_is_code(self):
        self.assertEqual(size.kind_of(['weird/path.xyz'], {'docs': ['docs/**']}), 'code')

    def test_empty_footprint_is_code(self):
        self.assertEqual(size.kind_of([], {'docs': ['docs/**']}), 'code')


class ClassifyTests(unittest.TestCase):

    def test_three_files_is_small(self):
        cls, kind, why = size.classify(files(3), size.SizeConfig())
        self.assertEqual((cls, kind), (size.SMALL, 'code'))

    def test_sixteen_files_is_large(self):
        cls, kind, why = size.classify(files(16), size.SizeConfig())
        self.assertEqual(cls, size.LARGE)

    def test_ten_files_is_medium(self):
        cls, kind, why = size.classify(files(10), size.SizeConfig())
        self.assertEqual(cls, size.MEDIUM)

    def test_small_boundary_at_exactly_three(self):
        self.assertEqual(size.classify(files(3), size.SizeConfig())[0], size.SMALL)
        self.assertEqual(size.classify(files(4), size.SizeConfig())[0], size.MEDIUM)

    def test_medium_boundary_at_exactly_fifteen(self):
        self.assertEqual(size.classify(files(15), size.SizeConfig())[0], size.MEDIUM)
        self.assertEqual(size.classify(files(16), size.SizeConfig())[0], size.LARGE)

    def test_wildcard_entry_among_literals_is_never_small(self):
        cls, kind, why = size.classify(['a.py', 'b.py', 'asf/**'], size.SizeConfig())
        self.assertEqual(cls, size.LARGE)
        self.assertEqual(why, "'asf/**' is a glob, not a file")

    def test_cross_repo_entry_is_large(self):
        cls, kind, why = size.classify(['a.py', 'record:features/**'], size.SizeConfig())
        self.assertEqual(cls, size.LARGE)

    def test_empty_footprint_is_large(self):
        cls, kind, why = size.classify([], size.SizeConfig())
        self.assertEqual(cls, size.LARGE)
        self.assertEqual(why, 'no writes: footprint')

    def test_never_small_entry_floors_a_small_count_to_medium(self):
        cfg = size.SizeConfig(never_small_globs=('db/migrate/**',))
        cls, kind, why = size.classify(['a.py', 'db/migrate/0001.sql'], cfg)
        self.assertEqual(cls, size.MEDIUM)
        self.assertEqual(why, "'db/migrate/0001.sql' matches never_small_paths")

    def test_never_small_entry_does_not_lower_a_large_count(self):
        cfg = size.SizeConfig(never_small_globs=('db/migrate/**',))
        writes = files(16) + ['db/migrate/0001.sql']
        cls, kind, why = size.classify(writes, cfg)
        self.assertEqual(cls, size.LARGE)

    def test_docs_kind_raises_the_small_threshold(self):
        cfg = size.SizeConfig(kind_globs={'docs': ['docs/**']})
        docs_writes = [f'docs/{i}.md' for i in range(15)]
        cls, kind, why = size.classify(docs_writes, cfg)
        self.assertEqual((cls, kind), (size.SMALL, 'docs'))

    def test_same_count_of_modules_is_medium(self):
        cfg = size.SizeConfig(kind_globs={'docs': ['docs/**']})
        code_writes = files(15)
        cls, kind, why = size.classify(code_writes, cfg)
        self.assertEqual((cls, kind), (size.MEDIUM, 'code'))


if __name__ == '__main__':
    unittest.main()
