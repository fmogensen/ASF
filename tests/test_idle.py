"""asf.feeder.idle: the accounting is total (every root in exactly one bucket), its residue is
named, and the render is one to three lines — F-0096 §3.2."""
import copy
import datetime as dt
import unittest

from asf.env import Product
from asf.feeder import idle, rows, tiers
try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from occfixture import occ
except ImportError:  # pragma: no cover - import shape only
    from tests.occfixture import occ

CAPACITY = 2
SESSION = {'item': 'T-0030', 'kind': 'task', 'account': 'w1', 'age': '12m', 'job': 'task-t-0030'}


def product(**conv):
    return Product('sample', {'conventions': dict(conv)})


def since(days):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days, hours=1)
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def feature(fid, rank, **extra):
    item = {'id': fid, 'type': 'feature', 'rank': rank, 'state': 'Active', 'decided': True,
            'stage': 'plan-approved', 'stage_since': since(6), 'children': []}
    item.update(extra)
    return item


def task(tid, parent, **extra):
    item = {'id': tid, 'type': 'task', 'parent': parent, 'rank': 1, 'state': 'New'}
    item.update(extra)
    return item


def index():
    """One root for every bucket but `other`."""
    items = [
        feature('F-0001', 1, children=['T-0031']), task('T-0031', 'F-0001', writes=['a.py']),
        feature('F-0002', 2, children=['T-0030']), task('T-0030', 'F-0002', writes=['b.py']),
        feature('F-0003', 3, children=['T-0040']), task('T-0040', 'F-0003', writes=['c.py']),
        feature('F-0004', 4, children=['T-0050'], stage='building 0/1'),
        task('T-0050', 'F-0004', after=['T-0029']),
        feature('F-0005', 5, blocked=True, blocked_by_open=['B-0031']),
        feature('F-0006', 6, decided=False), feature('F-0007', 7, decided=False),
        feature('F-0008', 8, decided=False),
        feature('F-0009', 9, children=['T-0033']), task('T-0033', 'F-0009', after=['T-0029']),
        feature('F-0010', 10, stage='landed'),
    ]
    return {v['id']: v for v in items}


def held():
    return {'F-0004': ('touch_production', 'human-now')}


def account(items, candidates=None, busy=(), **kw):
    prod = product()
    if candidates is None:
        candidates = rows.candidates({'items': items}, prod, [SESSION], occupancy=occ(busy=busy))
    selected = tiers.select(candidates, [SESSION], CAPACITY)
    kw.setdefault('held', held())
    return idle.account(items, prod, candidates, selected, [SESSION], set(busy), CAPACITY, **kw), candidates


def roots_of(items):
    return [i for i, v in items.items() if v['type'] == 'feature']


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.items = index()
        self.idle, _ = account(self.items)

    def why(self, bucket):
        return dict(self.idle.buckets[bucket])

    def test_buckets_are_in_classification_order_and_empty_ones_are_kept(self):
        self.assertEqual(tuple(self.idle.buckets), idle.BUCKETS)
        self.assertEqual(self.idle.buckets['other'], [])

    def test_a_launching_feature(self):
        self.assertEqual(self.why('launching'), {'F-0001': 'T-0031 PLAN → CODE'})

    def test_a_feature_whose_task_is_in_flight(self):
        self.assertEqual(self.why('in flight'), {'F-0002': 'task-t-0030 12m'})

    def test_a_root_awaiting_harvest_says_so(self):
        i, _ = account(self.items, busy={'T-0040'})
        self.assertEqual(dict(i.buckets['in flight'])['F-0003'], 'awaiting harvest')

    def test_a_row_the_capacity_cut_dropped(self):
        self.assertEqual(self.why('no slot'), {'F-0003': f'would launch: capacity {CAPACITY}'})

    def test_a_held_root(self):
        self.assertEqual(self.why('held'), {'F-0004': 'held touch_production (human-now)'})

    def test_a_held_root_reads_waiting_in_a_view_with_no_ledger(self):
        i, _ = account(self.items, held=None)
        self.assertEqual(dict(i.buckets['waiting'])['F-0004'], 'WAITS ON T-0029')

    def test_a_blocked_root(self):
        self.assertEqual(self.why('blocked'), {'F-0005': 'blocked by B-0031'})

    def test_three_undecided_roots(self):
        self.assertEqual(sorted(self.why('undecided')), ['F-0006', 'F-0007', 'F-0008'])
        self.assertRegex(self.why('undecided')['F-0006'], r'^undecided 6d, rank 6$')

    def test_a_root_waiting_on_a_task(self):
        self.assertEqual(self.why('waiting'), {'F-0009': 'WAITS ON T-0029'})

    def test_a_landed_root(self):
        self.assertEqual(self.why('landed'), {'F-0010': 'landed'})

    def test_every_root_lands_once_and_only_once(self):
        placed = [i for v in self.idle.buckets.values() for i, _ in v]
        self.assertEqual(len(placed), len(roots_of(self.items)))
        self.assertEqual(sorted(placed), sorted(roots_of(self.items)))

    def test_slots_are_free_slots_less_the_launches(self):
        self.assertEqual((self.idle.launched, self.idle.slots), (1, 0))
        self.assertFalse(self.idle.dry)

    def test_undecided_is_every_one_ranked_and_shown_is_the_cap(self):
        self.assertEqual([u[:2] for u in self.idle.undecided],
                         [('F-0006', 6), ('F-0007', 7), ('F-0008', 8)])
        self.assertEqual(self.idle.shown, 3)

    def test_a_bug_root_is_accounted_only_at_s1_or_s2(self):
        self.items['B-0001'] = {'id': 'B-0001', 'type': 'bug', 'severity': 'S3', 'state': 'New'}
        self.items['B-0002'] = {'id': 'B-0002', 'type': 'bug', 'severity': 'S2', 'state': 'New',
                                'stage_since': since(2)}
        i, _ = account(self.items)
        self.assertIn('B-0002', dict(i.buckets['undecided']))
        self.assertNotIn('B-0001', [x for v in i.buckets.values() for x, _ in v])

    def test_a_closed_feature_is_not_a_root(self):
        self.items['F-0010']['state'] = 'Closed'
        i, _ = account(self.items)
        self.assertEqual(sum(len(v) for v in i.buckets.values()), len(self.items) - 6)


class ResidueTests(unittest.TestCase):
    """D3, standing: a card that produces no session and no explanation is a failing test."""

    def setUp(self):
        self.items = index()
        self.before, self.candidates = account(self.items)

    def test_the_full_fixture_leaves_no_residue_and_prints_two_lines(self):
        self.assertEqual(self.before.buckets['other'], [])
        self.assertEqual(len(idle.lines(self.before)), 2)

    def test_a_root_no_rule_matches_is_named(self):
        mutated = copy.deepcopy(self.items)
        mutated['F-0010'].update(stage='spec-draft')  # decided, open, unblocked — and no row for it
        after, _ = account(mutated, candidates=self.candidates)
        self.assertEqual(after.buckets['other'], [('F-0010', 'no row, no reason')])
        third = idle.lines(after)[2]
        self.assertIn('F-0010', third)
        self.assertIn('no row and no reason', third)
        self.assertEqual(len(idle.lines(after)), 3)
        for bucket in idle.BUCKETS:
            if bucket not in ('landed', 'other'):
                self.assertEqual(after.buckets[bucket], self.before.buckets[bucket], bucket)
        self.assertEqual(after.buckets['landed'], [])


def make(slots=4, launched=0, n=73, **buckets):
    b = {k: [] for k in idle.BUCKETS}
    b.update(buckets)
    undecided = [(f'F-{i:04d}', i, '6d') for i in range(1, n + 1)]
    b['undecided'] = [(u[0], 'undecided') for u in undecided]
    return idle.Idle(slots=slots, launched=launched, buckets=b, undecided=undecided, shown=5)


class LinesTests(unittest.TestCase):
    def test_the_counts_line_lists_only_non_empty_buckets_in_bucket_order(self):
        i = make(n=73, **{'in flight': [('F-1000', 'x')] * 3, 'held': [('F-2000', 'x')],
                          'launching': [('F-3000', 'x')], 'blocked': [('F-4000', 'x')] * 5})
        self.assertEqual(idle.lines(i)[0], 'wave: 0 launched, 4 slots free — 1 launching, '
                         '3 in flight, 1 held, 5 blocked, 73 undecided')

    def test_the_decisions_line_names_the_capped_ids_with_rank_and_age(self):
        line = idle.lines(make())[1]
        self.assertEqual(line, 'wave: decide next — F-0001 (rank 1, undecided 6d) · '
                         'F-0002 (rank 2, undecided 6d) · F-0003 (rank 3, undecided 6d) · '
                         'F-0004 (rank 4, undecided 6d) · F-0005 (rank 5, undecided 6d) '
                         '— +68 more (asf next --all)')

    def test_top_overrides_the_cap(self):
        self.assertIn('— +70 more', idle.lines(make(), top=3)[1])
        self.assertNotIn('more', idle.lines(make(n=3), top=3)[1])

    def test_the_cap_is_the_products_decision_rows(self):
        self.assertIn('— +71 more', idle.lines(make(), product(decision_rows=2))[1])

    def test_nothing_undecided_means_no_decisions_line(self):
        out = idle.lines(make(n=0))
        self.assertEqual(out, ['wave: 0 launched, 4 slots free'])

    def test_never_more_than_three_lines(self):
        i = make(other=[('F-9999', 'no row, no reason')])
        self.assertEqual(len(idle.lines(i)), 3)

    def test_dry(self):
        self.assertTrue(make(slots=4, launched=0).dry)
        self.assertFalse(make(slots=0, launched=0).dry)
        self.assertFalse(make(slots=4, launched=2).dry)

    def test_needs_operator_names_the_file_the_count_the_slots_and_a_runnable_command(self):
        s = idle.needs_operator(make(), product(), 'groom/2026-09-24.md')
        self.assertIn('73 cards await a decision and 4 slots are idle', s)
        self.assertIn('groom/2026-09-24.md', s)
        self.assertIn('`asf set F-0001 decided=true --product sample`', s)

    def test_needs_operator_is_none_when_nothing_is_undecided(self):
        self.assertIsNone(idle.needs_operator(make(n=0), product(), 'groom/x.md'))


if __name__ == '__main__':
    unittest.main()
