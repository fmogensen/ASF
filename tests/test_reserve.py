"""asf.reserve — B-0151: two lane branches must never claim the same next number in a declared
sequence (a migration file, a decision band). `compute_next` reproduces the incident directly
(main and an open lane branch both carry a number; the next one skips both), and `reserve`
reproduces the actual race: two sessions calling around the same time, before either branch is
even pushed, so a git-only scan would hand out the same number twice. No test shells out."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env, reserve


class PatternRegexTests(unittest.TestCase):

    def test_captures_the_number_field_and_its_width(self):
        regex, width = reserve.pattern_regex('NNNN_*.sql')
        self.assertEqual(width, 4)
        m = regex.match('0292_bot_locale.sql')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '0292')
        self.assertIsNone(regex.match('readme.sql'))

    def test_no_number_field_is_an_error(self):
        with self.assertRaises(ValueError):
            reserve.pattern_regex('bands.md')


class ComputeNextTests(unittest.TestCase):

    def test_skips_numbers_on_an_open_lane_branch_not_just_main(self):
        # the incident: main only carries 0289, but an in-flight lane branch already claimed
        # 0290 for its own migration — a main-only scan would hand 0290 right back out.
        trees = {
            'origin/main': ['0289_a.sql'],
            'origin/cloud/T-0386': ['0290_b.sql'],
        }
        n, width = reserve.compute_next('db/migrations/NNNN_*.sql', trees)
        self.assertEqual((n, width), (291, 4))

    def test_prior_reservations_count_too(self):
        n, _width = reserve.compute_next('db/migrations/NNNN_*.sql', {}, reserved=[291, 292])
        self.assertEqual(n, 293)

    def test_format_number_zero_pads_to_the_pattern_width(self):
        self.assertEqual(reserve.format_number(293, 4), '0293')


class ReserveTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='reserve_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.old_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(tmp, 'home')

    def tearDown(self):
        env.ASF_HOME = self.old_home

    def product(self):
        return env.Product('sample', {
            'repo_dir': '/nonexistent', 'repo_slug': 'sample/product', 'main': 'main',
            'conventions': {'sequences': {'migrations': 'db/migrations/NNNN_*.sql'}},
        })

    def test_unknown_sequence_refuses(self):
        with self.assertRaises(SystemExit):
            reserve.reserve(self.product(), 'no-such-sequence')

    def test_second_call_does_not_repeat_the_first_before_any_branch_is_pushed(self):
        # both sessions see the same git state — neither has pushed yet — so only the recorded
        # claim from the first call keeps the second one from picking 0292 again.
        trees = {'origin/main': ['0291_bot_locale.sql']}
        with mock.patch.object(reserve, 'open_branch_trees', return_value=trees):
            first = reserve.reserve(self.product(), 'migrations')
            second = reserve.reserve(self.product(), 'migrations')
        self.assertEqual(first, '0292')
        self.assertEqual(second, '0293')

    def test_claim_survives_a_fresh_product_object(self):
        trees = {'origin/main': ['0291_bot_locale.sql']}
        with mock.patch.object(reserve, 'open_branch_trees', return_value=trees):
            first = reserve.reserve(self.product(), 'migrations')
            second = reserve.reserve(self.product(), 'migrations')  # a new session, same product
        self.assertEqual([first, second], ['0292', '0293'])
        path = reserve._reservations_path(self.product(), 'migrations')
        with open(path) as f:
            self.assertEqual(json.load(f), [292, 293])


if __name__ == '__main__':
    unittest.main()
