"""asf.reservations — F-0208: a plan's pull request landed a migration band on the trunk while
an open code branch, already in a pull request of its own, held the same number; nothing had
looked at what every open ref booked. `Bookings`/`Holdings`/`Refusal`/`Snapshot` reproduce the
merge and the decision directly, mock only — no test shells out. `AgainstGit` is the one
exception: a real git fixture (`tests.gitfixture.Template`), because it is the reader `git diff`
itself must answer to."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, reservations, reserve
from tests.gitfixture import Template


def _product(sequences=None, conventions=None, repo_dir='/nonexistent'):
    conv = dict(conventions or {})
    conv.setdefault('sequences', sequences if sequences is not None
                    else {'bands': 'db/migrations/NNNN_*.sql'})
    return env.Product('sample', {'repo_dir': repo_dir, 'repo_slug': 'sample/product',
                                   'main': 'main', 'conventions': conv})


class Bookings(unittest.TestCase):

    def test_an_added_booking_per_number_under_the_sequence_dir_and_none_outside_it(self):
        seqs = reservations.sequences(_product().conventions)
        with mock.patch.object(reservations, 'added_files',
                                return_value=['db/migrations/0289_b.sql', 'README.md']), \
             mock.patch.object(reservations.evidence, 'read_trees', return_value={}):
            out = reservations.added_bookings(_product(), seqs, ['main', 'worker/T-0100'])
        self.assertEqual(out, [reservations.Booking('bands', 289, 'worker/T-0100',
                                                     reservations.ADDED)])

    def test_the_trunks_whole_listing_is_booked_as_the_trunks(self):
        seqs = reservations.sequences(_product().conventions)
        trees = {'origin/main:db/migrations': {'0201_a.sql': 'x', '0289_a.sql': 'y'}}
        with mock.patch.object(reservations.evidence, 'read_trees', return_value=trees):
            out = reservations.added_bookings(_product(), seqs, ['main'])
        self.assertEqual(sorted(out), sorted([
            reservations.Booking('bands', 201, 'main', reservations.ADDED),
            reservations.Booking('bands', 289, 'main', reservations.ADDED)]))

    def test_a_writes_glob_books_the_features_plan_branch_while_the_plan_is_off_the_trunk(self):
        items = {
            'F-0113': {'id': 'F-0113', 'type': 'feature', 'evidence': ['plan on plan/F-0113']},
            'T-0100': {'id': 'T-0100', 'type': 'task', 'parent': 'F-0113', 'state': 'New',
                       'writes': ['db/migrations/0289_x.sql']},
        }
        seqs = reservations.sequences(_product().conventions)
        out = reservations.writes_bookings(_product(), items, seqs)
        self.assertEqual(out, [reservations.Booking('bands', 289, 'plan/F-0113',
                                                     reservations.WRITES)])

    def test_a_writes_glob_books_the_cards_own_branch_once_the_plan_has_landed(self):
        items = {
            'F-0113': {'id': 'F-0113', 'type': 'feature', 'evidence': []},
            'T-0100': {'id': 'T-0100', 'type': 'task', 'parent': 'F-0113', 'state': 'New',
                       'writes': ['db/migrations/0289_x.sql']},
        }
        seqs = reservations.sequences(_product().conventions)
        out = reservations.writes_bookings(_product(), items, seqs)
        self.assertEqual(out, [reservations.Booking('bands', 289, 'worker/T-0100',
                                                     reservations.WRITES)])

    def test_a_closed_card_books_nothing(self):
        items = {'T-0100': {'id': 'T-0100', 'type': 'task', 'state': 'Closed',
                            'writes': ['db/migrations/0289_x.sql']}}
        seqs = reservations.sequences(_product().conventions)
        self.assertEqual(reservations.writes_bookings(_product(), items, seqs), [])

    def test_a_bare_directory_glob_books_nothing(self):
        items = {'T-0100': {'id': 'T-0100', 'type': 'task', 'state': 'New',
                            'writes': ['db/migrations/*']}}
        seqs = reservations.sequences(_product().conventions)
        self.assertEqual(reservations.writes_bookings(_product(), items, seqs), [])

    def test_a_number_field_that_is_itself_a_pattern_books_nothing(self):
        items = {'T-0100': {'id': 'T-0100', 'type': 'task', 'state': 'New',
                            'writes': ['db/migrations/029[01]_*.sql']}}
        seqs = reservations.sequences(_product().conventions)
        self.assertEqual(reservations.writes_bookings(_product(), items, seqs), [])

    def test_command_bookings_parses_stdout_and_expands_a_range(self):
        with mock.patch.object(reservations.H, 'sh_timed',
                               return_value=(0, 'worker/T-1 bands 0289-0290\n', '')):
            bookings, why = reservations.command_bookings(
                _product(conventions={'reservations': 'a-product-script'}), ['worker/T-1'])
        self.assertEqual(why, '')
        self.assertEqual(sorted(bookings), sorted([
            reservations.Booking('bands', 289, 'worker/T-1', reservations.COMMAND),
            reservations.Booking('bands', 290, 'worker/T-1', reservations.COMMAND)]))

    def test_command_bookings_skips_an_unreadable_line_and_counts_it(self):
        with mock.patch.object(reservations.H, 'sh_timed',
                               return_value=(0, 'not a booking line\nworker/T-1 bands 0300\n',
                                             '')):
            bookings, why = reservations.command_bookings(
                _product(conventions={'reservations': 'a-product-script'}), ['worker/T-1'])
        self.assertEqual(bookings, [reservations.Booking('bands', 300, 'worker/T-1',
                                                          reservations.COMMAND)])
        self.assertTrue(why)

    def test_command_bookings_a_non_zero_exit_is_no_bookings_and_a_why(self):
        with mock.patch.object(reservations.H, 'sh_timed', return_value=(1, '', 'boom')):
            bookings, why = reservations.command_bookings(
                _product(conventions={'reservations': 'a-product-script'}), [])
        self.assertEqual(bookings, [])
        self.assertTrue(why)

    def test_command_bookings_a_timeout_is_no_bookings_and_a_why(self):
        with mock.patch.object(reservations.H, 'sh_timed', return_value=(None, '', '')):
            bookings, why = reservations.command_bookings(
                _product(conventions={'reservations': 'a-product-script'}), [])
        self.assertEqual(bookings, [])
        self.assertTrue(why)

    def test_no_command_configured_is_no_bookings_and_a_why(self):
        bookings, why = reservations.command_bookings(_product(), [])
        self.assertEqual(bookings, [])
        self.assertTrue(why)

    def test_declared_reads_only_the_numbered_globs(self):
        seqs = reservations.sequences(
            _product(sequences={'bands': 'docs/decisions/NNNN-*.md'}).conventions)
        item = {'writes': ['docs/decisions/0289-x.md', 'docs/decisions/*', 'asf/x.py']}
        self.assertEqual(reservations.declared(item, seqs), [('bands', 289)])

    def test_writes_bookings_reads_the_same_numbers_as_declared(self):
        """D10: one reader, so the brief and the hold can never name different numbers."""
        seqs = reservations.sequences(
            _product(sequences={'bands': 'docs/decisions/NNNN-*.md'}).conventions)
        items = {'T-0100': {'id': 'T-0100', 'type': 'task', 'state': 'Active',
                            'writes': ['docs/decisions/0289-x.md', 'docs/decisions/*']}}
        bookings = reservations.writes_bookings(
            _product(sequences={'bands': 'docs/decisions/NNNN-*.md'}), items, seqs)
        self.assertEqual(sorted((b.sequence, b.number) for b in bookings),
                         reservations.declared(items['T-0100'], seqs))


class Honour(unittest.TestCase):

    def test_a_closed_sequence_refuses_a_number_the_card_does_not_hold(self):
        self.assertEqual(reservations.unhonoured([('bands', 320)], [('bands', 319)]),
                         [('bands', [320], 319)])

    def test_a_sequence_the_card_names_no_number_in_is_open(self):
        self.assertEqual(reservations.unhonoured([], [('bands', 319)]), [])

    def test_a_card_that_has_not_booked_yet_is_clean(self):
        self.assertEqual(reservations.unhonoured([('bands', 320)], []), [])

    def test_honour_refusal_names_the_reserved_number_first(self):
        kind, text = reservations.honour_refusal(
            [('bands', [320], 319)], {'bands': ('docs/decisions', None, 4)})
        self.assertEqual(kind, reservations.RESERVED)
        self.assertIn('holds bands 0320', text)
        self.assertIn('books 0319', text)


class Holdings(unittest.TestCase):

    def test_rank_order_is_trunk_then_pull_request_number_then_ref_name(self):
        prs = {'worker/T-2': {'number': 5}, 'worker/T-3': {'number': 2}}
        refs = ['worker/T-2', 'worker/T-3', 'main', 'worker/T-4', 'worker/T-1']
        ranked = sorted(refs, key=lambda r: reservations.rank(r, 'main', prs))
        self.assertEqual(ranked, ['main', 'worker/T-3', 'worker/T-2', 'worker/T-1', 'worker/T-4'])

    def test_holder_names_the_lowest_ranked(self):
        bookings = [reservations.Booking('bands', 289, 'worker/T-2', reservations.ADDED),
                    reservations.Booking('bands', 289, 'worker/T-3', reservations.ADDED)]
        ranked = {'worker/T-2': (0, 5), 'worker/T-3': (0, 2)}
        holds = reservations.holdings(bookings, ranked)
        self.assertEqual(reservations.holder(holds, 'bands', 289), 'worker/T-3')

    def test_holdings_order_is_deterministic(self):
        bookings = [reservations.Booking('bands', 289, 'worker/T-2', reservations.ADDED),
                    reservations.Booking('bands', 289, 'worker/T-3', reservations.ADDED)]
        ranked = {'worker/T-2': (0, 5), 'worker/T-3': (0, 2)}
        first = reservations.holdings(bookings, ranked)
        second = reservations.holdings(list(reversed(bookings)), ranked)
        self.assertEqual(first, second)

    def test_clashes_are_in_sequence_then_number_order(self):
        bookings = [
            reservations.Booking('migrations', 512, 'plan/F-0190', reservations.WRITES),
            reservations.Booking('migrations', 512, 'worker/T-9', reservations.ADDED),
            reservations.Booking('bands', 289, 'worker/T-361', reservations.ADDED),
            reservations.Booking('bands', 289, 'plan/F-0113', reservations.WRITES)]
        ranked = {'plan/F-0190': (1, 'plan/F-0190'), 'worker/T-9': (0, 3),
                  'worker/T-361': (0, 1), 'plan/F-0113': (1, 'plan/F-0113')}
        holds = reservations.holdings(bookings, ranked)
        self.assertEqual(reservations.clashes(holds, 'plan/F-0113'),
                          [('bands', 289, 'worker/T-361')])
        self.assertEqual(reservations.clashes(holds, 'plan/F-0190'),
                          [('migrations', 512, 'worker/T-9')])
        self.assertEqual(reservations.clashes(holds, 'worker/T-361'), [])

    def test_next_free_is_one_past_the_highest_held_zero_padded(self):
        bookings = [reservations.Booking('bands', 289, 'main', reservations.ADDED),
                    reservations.Booking('bands', 291, 'worker/T-2', reservations.ADDED)]
        ranked = {'main': (-1, ''), 'worker/T-2': (1, 'worker/T-2')}
        holds = reservations.holdings(bookings, ranked)
        seqs = {'bands': ('db/migrations', None, 4)}
        self.assertEqual(reservations.next_free(holds, seqs, 'bands'), '0292')

    def test_a_sequence_naming_no_number_field_is_skipped_not_raised(self):
        conv = _product(sequences={'bands': 'db/migrations/NNNN_*.sql',
                                    'notes': 'docs/notes.md'}).conventions
        self.assertEqual(set(reservations.sequences(conv)), {'bands'})


class Refusal(unittest.TestCase):

    def _snapshot(self):
        return {
            'trunk': 'main',
            'prs': {'worker/T-0361': {'number': 844, 'state': 'OPEN'}},
            'sequences': {'bands': {'width': 4, 'held': {
                '289': ['worker/T-0361', 'plan/F-0113'],
                '290': ['worker/T-0361', 'plan/F-0113']}}},
        }

    def test_the_cards_own_case_names_the_sequence_numbers_holder_pr_and_next_free(self):
        kind, text = reservations.refusal(self._snapshot(), 'plan/F-0113')
        self.assertEqual(kind, reservations.RESERVED)
        self.assertIn('bands', text)
        self.assertIn('0289', text)
        self.assertIn('0290', text)
        self.assertIn('worker/T-0361', text)
        self.assertIn('PR 844', text)
        self.assertIn('0291', text)  # one past the highest held (0290) — C7's next-free rule

    def test_two_numbers_in_one_sequence_held_by_two_different_refs_each_name_their_own(self):
        snap = {
            'trunk': 'main',
            'prs': {'worker/A': {'number': 10, 'state': 'OPEN'},
                    'worker/B': {'number': 20, 'state': 'OPEN'}},
            'sequences': {'bands': {'width': 4, 'held': {
                '100': ['worker/A', 'plan/X'],
                '200': ['worker/B', 'plan/X']}}},
        }
        _kind, text = reservations.refusal(snap, 'plan/X')
        self.assertIn('bands 0100 is held by worker/A (PR 10)', text)
        self.assertIn('bands 0200 is held by worker/B (PR 20)', text)

    def test_the_holder_itself_is_refused_nothing(self):
        self.assertIsNone(reservations.refusal(self._snapshot(), 'worker/T-0361'))

    def test_a_number_the_trunk_holds_refuses_the_branch_that_adds_it_again(self):
        snap = {'trunk': 'main', 'prs': {},
                'sequences': {'bands': {'width': 4, 'held': {'289': ['main', 'worker/T-0500']}}}}
        kind, text = reservations.refusal(snap, 'worker/T-0500')
        self.assertEqual(kind, reservations.RESERVED)
        self.assertIn('main', text)

    def test_a_holder_with_no_pull_request_says_so_without_an_empty_pr(self):
        snap = {'trunk': 'main', 'prs': {},
                'sequences': {'bands': {'width': 4, 'held': {'289': ['plan/F-0001',
                                                                     'worker/T-9']}}}}
        _kind, text = reservations.refusal(snap, 'worker/T-9')
        self.assertNotIn('(PR', text)

    def test_no_clash_is_none(self):
        snap = {'trunk': 'main', 'prs': {},
                'sequences': {'bands': {'width': 4, 'held': {'289': ['worker/T-1']}}}}
        self.assertIsNone(reservations.refusal(snap, 'worker/T-9'))


class Snapshot(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='reservations_snapshot_')
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_save_then_load_round_trips(self):
        snap = {'trunk': 'main', 'sequences': {'bands': {'width': 4, 'held': {}}}, 'errors': []}
        reservations.save(self.d, snap)
        self.assertEqual(reservations.load(self.d), snap)

    def test_a_missing_file_loads_as_empty(self):
        self.assertEqual(reservations.load(self.d), {})

    def test_a_truncated_file_loads_as_empty(self):
        with open(os.path.join(self.d, reservations.FILE), 'w', encoding='utf-8') as f:
            f.write('{"trunk": "ma')
        self.assertEqual(reservations.load(self.d), {})

    def test_an_unparseable_file_loads_as_empty(self):
        with open(os.path.join(self.d, reservations.FILE), 'w', encoding='utf-8') as f:
            f.write('not json at all')
        self.assertEqual(reservations.load(self.d), {})

    def test_brief_lines_renders_the_cards_own_case(self):
        snap = {'prs': {'worker/T-0361': {'number': 844}},
                'sequences': {'bands': {'width': 4, 'held': {'289': ['worker/T-0361'],
                                                              '290': ['worker/T-0361']}}}}
        self.assertEqual(reservations.brief_lines(snap),
                          ['bands 0289, 0290 (worker/T-0361, PR 844)'])

    def test_brief_lines_caps_at_the_limit_with_plus_n_more(self):
        numbers = list(range(0, reservations.BRIEF_LIMIT + 3))
        held = {str(n): ['worker/T-1'] for n in numbers}
        snap = {'prs': {}, 'sequences': {'bands': {'width': 4, 'held': held}}}
        [line] = reservations.brief_lines(snap)
        self.assertIn('+3 more', line)

    def test_brief_lines_is_empty_for_an_empty_map(self):
        self.assertEqual(reservations.brief_lines({}), [])


def _git(args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(root, relpath, text):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _build_fixture(root):
    """A bare origin and one clone (``wt``): a trunk carrying ``db/migrations/0201_a.sql``; a
    branch adding ``0289_b.sql``; a second adding ``0289_c.sql``; a third that only *edits*
    ``0201_a.sql``; a fourth adding nothing under ``db/migrations``."""
    origin, wt = os.path.join(root, 'origin.git'), os.path.join(root, 'wt')
    _git(['init', '-q', '--bare', '-b', 'main', origin], root)
    _git(['clone', '-q', origin, wt], root)
    for k, v in (('user.name', 'T'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
        _git(['config', k, v], wt)

    _write(wt, 'db/migrations/0201_a.sql', 'a\n')
    _git(['add', '-A'], wt)
    _git(['commit', '-qm', 'seed'], wt)
    _git(['push', '-q', 'origin', 'HEAD:main'], wt)

    _git(['checkout', '-q', '-b', 'worker/T-0289-b'], wt)
    _write(wt, 'db/migrations/0289_b.sql', 'b\n')
    _git(['add', '-A'], wt)
    _git(['commit', '-qm', 'add 0289 b'], wt)
    _git(['push', '-q', '-u', 'origin', 'worker/T-0289-b'], wt)

    _git(['checkout', '-q', 'main'], wt)
    _git(['checkout', '-q', '-b', 'worker/T-0289-c'], wt)
    _write(wt, 'db/migrations/0289_c.sql', 'c\n')
    _git(['add', '-A'], wt)
    _git(['commit', '-qm', 'add 0289 c'], wt)
    _git(['push', '-q', '-u', 'origin', 'worker/T-0289-c'], wt)

    _git(['checkout', '-q', 'main'], wt)
    _git(['checkout', '-q', '-b', 'worker/T-0201-edit'], wt)
    _write(wt, 'db/migrations/0201_a.sql', 'a edited\n')
    _git(['add', '-A'], wt)
    _git(['commit', '-qm', 'edit 0201'], wt)
    _git(['push', '-q', '-u', 'origin', 'worker/T-0201-edit'], wt)

    _git(['checkout', '-q', 'main'], wt)
    _git(['checkout', '-q', '-b', 'worker/T-0400-none'], wt)
    _write(wt, 'README.md', 'adds nothing under db/migrations\n')
    _git(['add', '-A'], wt)
    _git(['commit', '-qm', 'unrelated'], wt)
    _git(['push', '-q', '-u', 'origin', 'worker/T-0400-none'], wt)

    _git(['checkout', '-q', 'main'], wt)
    _git(['fetch', '-q', 'origin'], wt)


FIXTURE = Template(_build_fixture, prefix='reservations_')


class AgainstGit(unittest.TestCase):
    """The reader that would have caught the card: real git, no forge — every pull-request
    number below is handed in by hand (PD9), never fetched."""

    def setUp(self):
        self.root = FIXTURE.fresh()
        self.wt = os.path.join(self.root, 'wt')

    def product(self):
        return env.Product('sample', {
            'repo_dir': self.wt, 'repo_slug': 'sample/product', 'main': 'main',
            'conventions': {'sequences': {'bands': 'db/migrations/NNNN_*.sql'}}})

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.wt, check=True, capture_output=True,
                              text=True).stdout.strip()

    def holdings_for(self, refs, prs):
        seqs = reservations.sequences(self.product().conventions)
        bookings = reservations.added_bookings(self.product(), seqs, refs)
        ranked = {ref: reservations.rank(ref, 'main', prs) for ref in refs}
        return reservations.holdings(bookings, ranked)

    def test_added_files_lists_the_additions_and_not_the_edit(self):
        self.assertEqual(reservations.added_files(self.wt, 'main', 'worker/T-0289-b'),
                          ['db/migrations/0289_b.sql'])
        self.assertEqual(reservations.added_files(self.wt, 'main', 'worker/T-0201-edit'), [])

    def test_the_two_0289_branches_clash_and_the_lower_pull_request_number_holds(self):
        refs = ['main', 'worker/T-0289-b', 'worker/T-0289-c']
        prs = {'worker/T-0289-b': {'number': 5, 'state': 'OPEN'},
               'worker/T-0289-c': {'number': 9, 'state': 'OPEN'}}
        holds = self.holdings_for(refs, prs)
        self.assertEqual(reservations.holder(holds, 'bands', 289), 'worker/T-0289-b')
        self.assertEqual(reservations.clashes(holds, 'worker/T-0289-c'),
                          [('bands', 289, 'worker/T-0289-b')])
        self.assertEqual(reservations.clashes(holds, 'worker/T-0289-b'), [])

    def test_the_editing_branch_clashes_with_nothing(self):
        refs = ['main', 'worker/T-0289-b', 'worker/T-0201-edit']
        prs = {'worker/T-0289-b': {'number': 5, 'state': 'OPEN'}}
        holds = self.holdings_for(refs, prs)
        self.assertEqual(reservations.clashes(holds, 'worker/T-0201-edit'), [])

    def test_after_0289_merges_the_still_open_branch_is_refused_by_the_trunk(self):
        self.git('merge', '--no-ff', '-q', '-m', 'merge b', 'worker/T-0289-b')
        self.git('push', '-q', 'origin', 'main')
        self.git('fetch', '-q', 'origin')
        refs = ['main', 'worker/T-0289-c']
        prs = {'worker/T-0289-c': {'number': 9, 'state': 'OPEN'}}
        holds = self.holdings_for(refs, prs)
        self.assertEqual(reservations.holder(holds, 'bands', 289), 'main')
        self.assertEqual(reservations.clashes(holds, 'worker/T-0289-c'),
                          [('bands', 289, 'main')])


class PrePushRev(unittest.TestCase):
    """The two pre-push cases (P8/P15): a revision not yet on origin, read directly rather than
    through ``origin/<ref>`` — the whole reason :func:`asf.reservations._added` and
    :func:`asf.reservations.booked` take any revision (PD9)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='reservations_prepush_')
        self.addCleanup(shutil.rmtree, self.root, True)

    def _repo_with_trunk(self):
        origin, repo = os.path.join(self.root, 'origin.git'), os.path.join(self.root, 'repo')
        _git(['init', '-q', '--bare', '-b', 'main', origin], self.root)
        _git(['clone', '-q', origin, repo], self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            _git(['config', k, v], repo)
        self._commit(repo, 'docs/decisions/0288-a.md')
        _git(['push', '-q', 'origin', 'HEAD:main'], repo)
        return repo

    def _write(self, repo, relpath):
        _write(repo, relpath, 'x\n')

    def _commit(self, repo, relpath):
        self._write(repo, relpath)
        _git(['add', '-A'], repo)
        _git(['commit', '-qm', relpath], repo)

    def test_booked_reads_a_branch_that_is_on_no_remote(self):
        """P8/P15: the pre-push moment. HEAD in this worktree, never origin/<ref>."""
        repo = self._repo_with_trunk()                       # origin/main carries 0288-*.md
        self._commit(repo, 'docs/decisions/0289-x.md')       # committed, never pushed
        seqs = reservations.sequences(
            _product(sequences={'bands': 'docs/decisions/NNNN-*.md'}).conventions)
        self.assertEqual(reservations.booked(repo, 'main', 'HEAD', seqs), [('bands', 289)])

    def test_pending_added_sees_an_uncommitted_file(self):
        repo = self._repo_with_trunk()
        self._write(repo, 'docs/decisions/0290-y.md')        # never added
        seqs = reservations.sequences(
            _product(sequences={'bands': 'docs/decisions/NNNN-*.md'}).conventions)
        self.assertEqual(reservations.pending_added(repo, seqs), [('bands', 290)])


if __name__ == '__main__':
    unittest.main()
