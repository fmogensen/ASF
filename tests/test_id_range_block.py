# tests/test_id_range_block.py — new, the spec's module in full
"""F-0261: a job's claimed id block is its own. The next id inside ``BACKLOG_ID_RANGE`` is the
next free number *of the block* — the record's tip, which a block claimed later pushes above
this one's ``hi``, never exhausts a block nobody has used. Hermetic: temp dirs, no git, no
network, and the ambient ``BACKLOG_ID_RANGE`` of a ranged worker never leaks in (B-0012)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf.record import idcheck, idclaim, ids

RANGE = 'S:60954-61003,T:60950-60999,B:60950-60999'
FOLDER = {'S': 'stories', 'T': 'tasks', 'B': 'bugs'}


class Record:
    """A bare record directory holding one card file per id given — no git, no frontmatter
    beyond the id, because ``mint_id`` reads nothing else off the folder."""

    def __init__(self, tc, *iids):
        self.root = tempfile.mkdtemp(prefix='idrange_')
        tc.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for folder in FOLDER.values():
            os.makedirs(os.path.join(self.root, folder))
        for iid in iids:
            self.hold(iid)

    def hold(self, iid):
        with open(os.path.join(self.root, FOLDER[iid[0]], f'{iid}.md'), 'w',
                  encoding='utf-8') as fh:
            fh.write(f'---\nid: {iid}\n---\n')
        return iid

    def mint(self, type_='story', canonical=None):
        return ids.mint_id(self.root, canonical or {}, type_)


class TheBlockIsNotTheRecordsTip(unittest.TestCase):
    def test_a_tip_above_the_block_does_not_exhaust_it(self):
        r = Record(self, 'S-61200')  # a block claimed after ours landed its Stories first
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            self.assertEqual(r.mint(), 'S-60954')

    def test_the_block_is_spent_in_order(self):
        r = Record(self, 'S-61200')
        got = []
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            for _ in range(3):
                got.append(r.hold(r.mint()))
        self.assertEqual(got, ['S-60954', 'S-60955', 'S-60956'])

    def test_a_number_inside_the_block_is_stepped_over(self):
        r = Record(self, 'S-60960', 'S-61200')
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            self.assertEqual(r.mint(), 'S-60961')

    def test_a_hole_in_the_block_is_never_refilled(self):
        r = Record(self, 'S-60954', 'S-60956')
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            self.assertEqual(r.mint(), 'S-60957')

    def test_an_id_only_the_canonical_map_holds_counts_as_taken(self):
        r = Record(self)
        canonical = {'S-60954': {'meta': {'type': 'story', 'id': 'S-60954'}}}
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            self.assertEqual(r.mint(canonical=canonical), 'S-60955')

    def test_every_prefix_reads_its_own_block(self):
        r = Record(self, 'T-61500', 'B-61500')
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            self.assertEqual(r.mint('task'), 'T-60950')
            self.assertEqual(r.mint('bug'), 'B-60950')


class AFullBlockIsStillRefused(unittest.TestCase):
    def test_a_block_whose_every_number_is_taken_is_exhausted(self):
        r = Record(self, *[f'S-{n}' for n in range(60954, 61004)])
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            with self.assertRaises(SystemExit) as cm:
                r.mint()
        self.assertIn('S:60954-61003 exhausted', str(cm.exception))
        self.assertIn('every number of the block is in the record', str(cm.exception))

    def test_the_last_number_of_the_block_is_mintable(self):
        r = Record(self, *[f'S-{n}' for n in range(60954, 61003)])
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            self.assertEqual(r.mint(), 'S-61003')


class TheHelper(unittest.TestCase):
    def test_an_untouched_block_starts_at_lo(self):
        self.assertEqual(ids.next_in_block({1, 2, 70000}, 60954, 61003), 60954)

    def test_a_used_block_goes_above_its_own_top(self):
        self.assertEqual(ids.next_in_block({60954, 60955, 99999}, 60954, 61003), 60956)

    def test_a_full_block_returns_the_number_past_hi(self):
        self.assertEqual(ids.next_in_block(set(range(60954, 61004)), 60954, 61003), 61004)


class TheMintedIdLands(unittest.TestCase):
    """``idcheck`` refuses an id no claim covers. The number the block hands out must be one the
    land gate accepts — and the number the record's tip would have handed out must not be."""

    CLAIM = idclaim.Claim('S', 60954, 61003, 'spec-f-0261', 'refs/asf/ids/S-60954')

    def test_the_block_number_is_covered_by_the_claim(self):
        r = Record(self, 'S-61200')
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': RANGE}):
            iid = r.mint()
        self.assertEqual(idcheck.check_doc(f'- {iid}: a Story\n', {}, [self.CLAIM],
                                           'spec-f-0261'), [])

    def test_the_tip_plus_one_would_not_have_been(self):
        bad = idcheck.check_doc('- S-61201: a Story\n', {}, [self.CLAIM], 'spec-f-0261')
        self.assertEqual(len(bad), 1)
        self.assertIn('no claim covers', bad[0])


class NothingOutsideTheRangedBranchChanges(unittest.TestCase):
    def test_without_a_range_the_record_tip_still_decides(self):
        r = Record(self, 'S-61200')
        with mock.patch.dict(os.environ):
            os.environ.pop('BACKLOG_ID_RANGE', None)
            self.assertEqual(r.mint(), 'S-61201')

    def test_a_range_without_this_prefix_is_no_range_at_all(self):
        r = Record(self, 'S-61200')
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': 'T:60950-60999'}):
            self.assertEqual(r.mint(), 'S-61201')


class TheGuideSaysIt(unittest.TestCase):
    def test_the_id_claim_section_states_the_block_rule(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'docs', 'guide', 'product-config.md'),
                  encoding='utf-8') as fh:
            text = fh.read()
        section = text.split('### Ids are claimed by push', 1)[1].split('\n## ', 1)[0]
        for needle in ('Inside a block', 'next free number', "record's tip", 'exhausted'):
            self.assertIn(needle, section)


if __name__ == '__main__':
    unittest.main()
