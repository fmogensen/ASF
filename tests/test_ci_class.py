"""tests.test_ci_class — T-0141: the classifier's pure functions and the workflow reader."""
import os
import unittest

from asf import ci

GLOBS = ['docs/*.md', 'docs/**/*.md', 'README.md', 'scripts/**', 'tests/test_one.py']

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ClassifyTest(unittest.TestCase):
    def test_classes_are_the_two_names(self):
        self.assertEqual(ci.CLASSES, ('FACTORY_ONLY', 'FULL'))
        self.assertEqual(ci.CLASSES, (ci.FACTORY_ONLY, ci.FULL))

    def test_all_matched_is_factory_only(self):
        self.assertEqual(ci.classify(['README.md', 'docs/a.md', 'tests/test_one.py'], GLOBS),
                         ci.FACTORY_ONLY)

    def test_one_unmatched_among_ten_is_full(self):
        paths = ['docs/n%d.md' % i for i in range(10)] + ['asf/core.py']
        self.assertEqual(ci.classify(paths, GLOBS), ci.FULL)
        self.assertEqual(ci.unmatched(paths, GLOBS), ['asf/core.py'])

    def test_empty_paths_is_full(self):
        self.assertEqual(ci.classify([], GLOBS), ci.FULL)

    def test_empty_globs_is_full(self):
        self.assertEqual(ci.classify(['README.md'], []), ci.FULL)

    def test_unmatched_first_seen_order(self):
        paths = ['z.py', 'README.md', 'a.py', 'z.py']
        self.assertEqual(ci.unmatched(paths, GLOBS), ['z.py', 'a.py', 'z.py'])

    def test_leading_dot_slash_is_stripped(self):
        self.assertEqual(ci.classify(['./README.md'], GLOBS), ci.FACTORY_ONLY)


class MatchesTest(unittest.TestCase):
    def test_bare_name_matches_base_name_anywhere(self):
        self.assertTrue(ci.matches('deep/dir/README.md', ['README.md']))

    def test_directory_glob(self):
        self.assertTrue(ci.matches('scripts/run.sh', ['scripts/**']))

    def test_nested_glob(self):
        self.assertTrue(ci.matches('docs/a/b/c.md', ['docs/**/*.md']))

    def test_slash_glob_is_not_base_name(self):
        self.assertFalse(ci.matches('other/a.md', ['docs/*.md']))

    def test_none_match(self):
        self.assertFalse(ci.matches('asf/core.py', GLOBS))


class WorkflowGlobsTest(unittest.TestCase):
    TEXT = (
        'name: t\n'
        'env:\n'
        '  OTHER: x\n'
        '  FACTORY_ONLY_PATHS: |\n'
        '    # the workflow copy\n'
        '    docs/*.md\n'
        '\n'
        '    README.md\n'
        '    scripts/**\n'
        '  AFTER: y\n'
        'jobs: {}\n'
    )

    def test_block_read_back_as_list(self):
        self.assertEqual(ci.globs_from_workflow(self.TEXT),
                         ['docs/*.md', 'README.md', 'scripts/**'])

    def test_no_block_is_empty(self):
        self.assertEqual(ci.globs_from_workflow('name: t\nenv:\n  OTHER: x\n'), [])

    def test_block_at_end_of_file(self):
        self.assertEqual(ci.globs_from_workflow('env:\n  FACTORY_ONLY_PATHS: |\n    a.md\n'),
                         ['a.md'])

    def test_real_workflow(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'tests.yml'), encoding='utf-8') as f:
            globs = ci.globs_from_workflow(f.read())
        for g in globs:
            self.assertIsInstance(g, str)
            self.assertTrue(g)
            self.assertFalse(g.startswith('-'))


if __name__ == '__main__':
    unittest.main()
