"""asf.reserve — B-0151: two lane branches must never claim the same next number in a declared
sequence (a migration file, a decision band). `compute_next` reproduces the incident directly
(main and an open lane branch both carry a number; the next one skips both), and `reserve`
reproduces the actual race: two sessions calling around the same time, before either branch is
even pushed, so a git-only scan would hand out the same number twice — including two cloud
sessions on separate, freshly checked-out runners (review round 1, C1), which is why the claim
`reserve` races against is `remote_reserved_numbers`/`claim` (a ref on origin), never a local
file. No test shells out."""
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


class ReservationRefTests(unittest.TestCase):

    def test_the_ref_carries_the_sequence_and_the_number(self):
        self.assertEqual(reserve._reservation_ref('migrations', 293),
                         'refs/heads/reservations/migrations/293')


class RemoteReservedNumbersTests(unittest.TestCase):
    """`remote_reserved_numbers` is one `ls-remote` against `origin` — the read half of the
    claim every runner shares, a fresh cloud checkout included (C1): no local file, no product
    state, nothing this process wrote earlier."""

    def product(self):
        return mock.Mock(repo_dir='/repo')

    def test_parses_ls_remote_into_the_claimed_numbers(self):
        out = mock.Mock(stdout='deadbeef\trefs/heads/reservations/migrations/291\n'
                               'cafef00d\trefs/heads/reservations/migrations/292\n')
        with mock.patch.object(reserve.H, 'sh', return_value=out) as sh:
            numbers = reserve.remote_reserved_numbers(self.product(), 'migrations')
        self.assertEqual(sorted(numbers), [291, 292])
        sh.assert_called_once_with(
            ['git', 'ls-remote', '--heads', 'origin',
             'refs/heads/reservations/migrations/*'], cwd='/repo')

    def test_no_claims_yet_is_empty(self):
        with mock.patch.object(reserve.H, 'sh', return_value=mock.Mock(stdout='')):
            self.assertEqual(reserve.remote_reserved_numbers(self.product(), 'migrations'), [])


class ClaimTests(unittest.TestCase):
    """`claim` is the write half: a fresh, parentless commit pushed to a brand new ref — a push
    that lands is the claim; a push git refuses (the ref is already there) is not."""

    def product(self):
        return mock.Mock(repo_dir='/repo')

    def test_a_push_that_lands_is_a_claim(self):
        with mock.patch.object(reserve.H, 'sh',
                               return_value=mock.Mock(stdout='deadbeef\n')) as sh, \
             mock.patch.object(reserve.gitpush, 'push',
                               return_value=mock.Mock(returncode=0)) as push:
            self.assertTrue(reserve.claim(self.product(), 'migrations', 293))
        sh.assert_called_once_with(
            ['git', 'commit-tree', reserve.EMPTY_TREE, '-m', 'reserve migrations 293'],
            cwd='/repo')
        push.assert_called_once_with(
            ['origin', 'deadbeef:refs/heads/reservations/migrations/293'], '/repo',
            refs_only=True)

    def test_a_refused_push_is_not_a_claim(self):
        with mock.patch.object(reserve.H, 'sh',
                               return_value=mock.Mock(stdout='deadbeef\n')), \
             mock.patch.object(reserve.gitpush, 'push',
                               return_value=mock.Mock(returncode=1)):
            self.assertFalse(reserve.claim(self.product(), 'migrations', 293))


class ReserveTests(unittest.TestCase):

    def product(self):
        return env.Product('sample', {
            'repo_dir': '/nonexistent', 'repo_slug': 'sample/product', 'main': 'main',
            'conventions': {'sequences': {'migrations': 'db/migrations/NNNN_*.sql'}},
        })

    def test_unknown_sequence_refuses(self):
        with self.assertRaises(SystemExit):
            reserve.reserve(self.product(), 'no-such-sequence')

    def test_second_call_does_not_repeat_the_first_before_any_branch_is_pushed(self):
        # both sessions see the same git state — neither has pushed yet — and neither runner's
        # own process holds the other's claim: `store` stands in for `origin` itself (the one
        # place a fresh cloud runner and this one both reach, C1), not a local file or a shared
        # Product object.
        trees = {'origin/main': ['0291_bot_locale.sql']}
        store = set()

        def fake_claim(product, sequence, n):
            if n in store:
                return False
            store.add(n)
            return True

        with mock.patch.object(reserve, 'open_branch_trees', return_value=trees), \
             mock.patch.object(reserve, 'remote_reserved_numbers',
                               side_effect=lambda p, s: sorted(store)), \
             mock.patch.object(reserve, 'claim', side_effect=fake_claim):
            first = reserve.reserve(self.product(), 'migrations')
            second = reserve.reserve(self.product(), 'migrations')  # a fresh runner, same origin
        self.assertEqual([first, second], ['0292', '0293'])

    def test_a_collision_moves_on_to_the_next_candidate(self):
        # 0292 is free by every read `reserve` makes before it pushes — the collision is a race
        # `claim`'s own push loses, not a stale scan (a second session's claim for 0292 lands on
        # origin in between). `reserve` must retry forward onto the next free number, never
        # repeat the number that just lost, never give up.
        trees = {'origin/main': ['0291_bot_locale.sql']}
        store = set()

        def fake_claim(product, sequence, n):
            if n == 292:
                store.add(292)  # another session's claim wins the race right here
                return False
            store.add(n)
            return True

        with mock.patch.object(reserve, 'open_branch_trees', return_value=trees), \
             mock.patch.object(reserve, 'remote_reserved_numbers',
                               side_effect=lambda p, s: sorted(store)), \
             mock.patch.object(reserve, 'claim', side_effect=fake_claim):
            self.assertEqual(reserve.reserve(self.product(), 'migrations'), '0293')

    def test_sustained_contention_gives_up_loudly(self):
        trees = {'origin/main': ['0291_bot_locale.sql']}
        with mock.patch.object(reserve, 'open_branch_trees', return_value=trees), \
             mock.patch.object(reserve, 'remote_reserved_numbers', return_value=[]), \
             mock.patch.object(reserve, 'claim', return_value=False) as claim:
            with self.assertRaises(SystemExit):
                reserve.reserve(self.product(), 'migrations')
        self.assertEqual(claim.call_count, reserve.MAX_ATTEMPTS)


if __name__ == '__main__':
    unittest.main()
