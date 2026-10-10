import unittest

from asf import boundary
from asf.conventions import Conventions


class CoversTests(unittest.TestCase):
    def test_an_exact_path(self):
        self.assertTrue(boundary.covers('asf/harvest/harvest.py', 'asf/harvest/harvest.py'))
        self.assertFalse(boundary.covers('asf/harvest/harvest.py', 'asf/harvest/other.py'))

    def test_a_directory_with_and_without_a_trailing_slash(self):
        self.assertTrue(boundary.covers('asf/harvest', 'asf/harvest/harvest.py'))
        self.assertTrue(boundary.covers('asf/harvest/', 'asf/harvest/harvest.py'))

    def test_a_sibling_sharing_a_prefix_but_not_a_segment(self):
        self.assertFalse(boundary.covers('asf/x', 'asf/xy.py'))

    def test_star_does_not_cross_a_slash(self):
        self.assertFalse(boundary.covers('asf/*.py', 'asf/a/b.py'))
        self.assertTrue(boundary.covers('asf/*.py', 'asf/a.py'))

    def test_double_star_crosses_a_slash(self):
        self.assertTrue(boundary.covers('asf/**', 'asf/a/b.py'))

    def test_question_mark_is_one_non_slash_character(self):
        self.assertTrue(boundary.covers('asf/?.py', 'asf/a.py'))
        self.assertFalse(boundary.covers('asf/?.py', 'asf/ab.py'))
        self.assertFalse(boundary.covers('asf/?.py', 'asf/a/b.py'))

    def test_a_character_class(self):
        self.assertTrue(boundary.covers('asf/[ab].py', 'asf/a.py'))
        self.assertTrue(boundary.covers('asf/[ab].py', 'asf/b.py'))
        self.assertFalse(boundary.covers('asf/[ab].py', 'asf/c.py'))

    def test_a_path_with_no_directory_at_all(self):
        self.assertTrue(boundary.covers('README.md', 'README.md'))
        self.assertFalse(boundary.covers('README.md', 'docs/README.md'))

    def test_an_empty_glob_or_path_covers_nothing(self):
        self.assertFalse(boundary.covers('', 'asf/x.py'))
        self.assertFalse(boundary.covers('asf/*', ''))


class OutsideTests(unittest.TestCase):
    def test_preserves_order_and_deduplicates(self):
        paths = ['asf/other.py', 'asf/harvest/harvest.py', 'asf/other.py', 'tests/test_env.py']
        self.assertEqual(boundary.outside(paths, ['asf/harvest/**']),
                         ['asf/other.py', 'tests/test_env.py'])

    def test_empty_for_a_fully_covered_list(self):
        self.assertEqual(boundary.outside(['asf/harvest/a.py', 'asf/harvest/b.py'],
                                          ['asf/harvest/**']), [])

    def test_an_empty_grant_leaves_every_path_outside(self):
        self.assertEqual(boundary.outside(['a.py', 'b.py'], []), ['a.py', 'b.py'])
        self.assertEqual(boundary.outside(['a.py'], None), ['a.py'])


class WithinTests(unittest.TestCase):
    def test_accepts_a_narrower_inner_glob(self):
        self.assertIsNone(boundary.within(['asf/harvest/harvest.py'], ['asf/harvest/**']))
        self.assertIsNone(boundary.within(['asf/harvest/**'], ['asf/**']))

    def test_rejects_a_glob_the_outer_does_not_contain(self):
        self.assertEqual(boundary.within(['asf/other.py'], ['asf/harvest/**']), 'asf/other.py')

    def test_returns_the_first_offender(self):
        self.assertEqual(boundary.within(['asf/harvest/a.py', 'asf/other.py'],
                                         ['asf/harvest/**']), 'asf/other.py')

    def test_an_empty_inner_or_outer(self):
        self.assertIsNone(boundary.within([], ['asf/**']))
        self.assertEqual(boundary.within(['asf/x.py'], []), 'asf/x.py')


class GrantForTests(unittest.TestCase):
    def setUp(self):
        self.conv = Conventions()

    def test_the_cards_own_grant_wins(self):
        card = {'grant': ['asf/boundary.py'], 'writes': ['asf/**']}
        self.assertEqual(boundary.grant_for(self.conv, 'worker/T-0080', 'T-0080', card),
                         ['asf/boundary.py', 'docs/reviews/*-t-0080.md'])

    def test_falls_back_to_the_footprint(self):
        card = {'writes': ['asf/harvest/**', 'tests/test_harvest.py']}
        self.assertEqual(boundary.grant_for(self.conv, 'worker/T-0080', 'T-0080', card),
                         ['asf/harvest/**', 'tests/test_harvest.py',
                          'docs/reviews/*-t-0080.md'])

    def test_every_grant_holds_the_items_own_review_files(self):
        """A review round writes its file on the branch it reviews, whatever the lane's kind,
        and no card declares that path — so a grant without it would hold every branch a
        reviewer has read."""
        for card in ({'writes': ['asf/boundary.py']}, {'grant': ['asf/boundary.py']}):
            self.assertIsNone(boundary.refusal(
                self.conv, 'worker/T-0080', 'T-0080', card,
                ['asf/boundary.py', 'docs/reviews/1-t-0080.md']), card)
        # another item's review file is outside it, like any other path
        edge = boundary.refusal(self.conv, 'worker/T-0080', 'T-0080',
                                {'writes': ['asf/boundary.py']},
                                ['docs/reviews/1-t-0099.md'])
        self.assertEqual(boundary.outside_of(edge), ('docs/reviews/1-t-0099.md',))

    def test_a_grant_is_never_only_the_review_glob(self):
        self.assertIsNone(boundary.grant_for(self.conv, 'worker/T-0080', 'T-0080', {}))

    def test_a_spec_branch_with_no_card_grant(self):
        got = boundary.grant_for(self.conv, 'spec/F-0026', 'F-0026', {})
        self.assertEqual(got, ['docs/specs/f-0026.md', 'docs/reviews/*-f-0026.md'])

    def test_a_plan_branch_with_no_card_grant(self):
        got = boundary.grant_for(self.conv, 'plan/F-0026', 'F-0026', {})
        self.assertEqual(got, ['docs/plans/f-0026.md', 'docs/reviews/*-f-0026.md'])

    def test_none_not_empty_list_for_a_card_with_neither(self):
        got = boundary.grant_for(self.conv, 'fix/B-0007', 'B-0007', {})
        self.assertIsNone(got)
        self.assertIsNone(boundary.refusal(self.conv, 'fix/B-0007', 'B-0007', {},
                                           ['asf/anything.py']))


class ModeTests(unittest.TestCase):
    def test_the_three_values(self):
        for value in boundary.MODES:
            conv = Conventions.from_mapping({'boundary': value})
            self.assertEqual(boundary.mode(conv), value)

    def test_hold_is_the_default(self):
        self.assertEqual(boundary.mode(Conventions()), boundary.HOLD)
        self.assertIsNone(boundary.mode_warning(Conventions()))

    def test_an_unknown_value_reads_as_hold_and_is_named(self):
        conv = Conventions.from_mapping({'boundary': 'revert'})
        self.assertEqual(boundary.mode(conv), boundary.HOLD)
        warning = boundary.mode_warning(conv)
        self.assertIn('revert', warning)
        self.assertIn('hold', warning)


if __name__ == '__main__':
    unittest.main()
