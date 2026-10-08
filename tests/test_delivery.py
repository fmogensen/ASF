import unittest

from asf.groom import delivery
from asf.groom.delivery import Member


def card(id_, type_, title='Title', writes=None, rank=None, decided=True, state='New',
         parent=None, extra=None, body=''):
    meta = {'id': id_, 'type': type_, 'title': title, 'decided': decided, 'state': state}
    if writes is not None:
        meta['writes'] = writes
    if rank is not None:
        meta['rank'] = rank
    if parent is not None:
        meta['parent'] = parent
    if extra:
        meta.update(extra)
    return {'meta': meta, 'body': body}


class SmallTest(unittest.TestCase):
    def test_size_field_wins(self):
        canonical = {}
        small = card('F-0001', 'feature', writes=['a', 'b', 'c', 'd'], extra={'size': 'small'})
        self.assertTrue(delivery.is_small(small, canonical, 2))
        not_small = card('F-0002', 'feature', writes=['a', 'b', 'c', 'd'])
        self.assertFalse(delivery.is_small(not_small, canonical, 2))

    def test_bug_severity(self):
        canonical = {}
        for severity in ('S2', 'S3'):
            rec = card('B-0001', 'bug', writes=['a', 'b', 'c', 'd', 'e'],
                       extra={'severity': severity})
            self.assertTrue(delivery.is_small(rec, canonical, 2), severity)
        s1 = card('B-0002', 'bug', writes=['a'], extra={'severity': 'S1'})
        self.assertFalse(delivery.is_small(s1, canonical, 2))

    def test_feature_with_children_is_not_small(self):
        child = card('S-0001', 'story', parent='F-0001')
        canonical = {'S-0001': child}
        rec = card('F-0001', 'feature', writes=['a'])
        self.assertFalse(delivery.is_small(rec, canonical, 2))


class EligibleTest(unittest.TestCase):
    def test_eligible_item(self):
        rec = card('B-0001', 'bug', writes=['asf/x.py'], extra={'severity': 'S2'})
        canonical = {'B-0001': rec}
        members = delivery.deliverable_items(canonical, 2)
        self.assertEqual([m.id for m in members], ['B-0001'])

    def test_undecided_blocked_active_after_or_no_writes_are_not(self):
        undecided = card('B-0001', 'bug', writes=['a'], decided=False, extra={'severity': 'S2'})
        blocked = card('B-0002', 'bug', writes=['a'], extra={'severity': 'S2',
                       'blockedBy': ['B-0099']})
        blocker = card('B-0099', 'bug', writes=['x'], extra={'severity': 'S2'})
        active = card('B-0003', 'bug', writes=['a'], state='Active', extra={'severity': 'S2'})
        after = card('B-0004', 'bug', writes=['a'], extra={'severity': 'S2', 'after': ['B-0005']})
        no_writes = card('B-0005', 'bug', writes=[], extra={'severity': 'S2'})
        canonical = {r['meta']['id']: r for r in
                     (undecided, blocked, blocker, active, after, no_writes)}
        members = delivery.deliverable_items(canonical, 2)
        self.assertNotIn('B-0001', [m.id for m in members])
        self.assertNotIn('B-0002', [m.id for m in members])
        self.assertNotIn('B-0003', [m.id for m in members])
        self.assertNotIn('B-0004', [m.id for m in members])
        self.assertNotIn('B-0005', [m.id for m in members])

    def test_feature_past_card_stage_is_not(self):
        rec = card('F-0001', 'feature', writes=['a'], extra={'stage': 'spec-draft'})
        canonical = {'F-0001': rec}
        self.assertEqual(delivery.deliverable_items(canonical, 2), [])

    def test_item_already_in_a_delivery_is_not(self):
        rec = card('B-0001', 'bug', writes=['a'],
                   extra={'severity': 'S2', 'delivered_by': 'F-0097'})
        canonical = {'B-0001': rec}
        self.assertEqual(delivery.deliverable_items(canonical, 2), [])


class GroupTest(unittest.TestCase):
    def test_overlap_groups(self):
        a = Member('B-0001', 'bug', 'A', ['asf/groom/**'], 1, 0)
        b = Member('B-0002', 'bug', 'B', ['asf/groom/groom.py'], 2, 0)
        comps = delivery.group([a, b], 2)
        self.assertEqual(len(comps), 1)
        self.assertEqual([m.id for m in comps[0]], ['B-0001', 'B-0002'])

    def test_shared_area_groups(self):
        a = Member('B-0001', 'bug', 'A', ['asf/feeder/rows/a.py'], 1, 0)
        b = Member('B-0002', 'bug', 'B', ['asf/feeder/tiers/b.py'], 2, 0)
        self.assertEqual(len(delivery.group([a, b], 2)), 1)
        self.assertEqual(delivery.group([a, b], 3), [])

    def test_unrelated_stays_alone(self):
        a = Member('B-0001', 'bug', 'A', ['unrelated/path.py'], 1, 0)
        self.assertEqual(delivery.group([a], 2), [])


class BudgetTest(unittest.TestCase):
    def test_items_ceiling(self):
        members = [Member(f'B-000{i}', 'bug', 't', ['a'], i, 0) for i in range(6)]
        cut = delivery.budget_cut(members, 4, 6, 60000)
        self.assertEqual([m.id for m in cut], ['B-0000', 'B-0001', 'B-0002', 'B-0003'])

    def test_globs_ceiling(self):
        too_big = Member('B-0001', 'bug', 't', ['a', 'b', 'c'], 1, 0)
        fits_1 = Member('B-0002', 'bug', 't', ['d'], 2, 0)
        fits_2 = Member('B-0003', 'bug', 't', ['e'], 3, 0)
        cut = delivery.budget_cut([too_big, fits_1, fits_2], 4, 2, 60000)
        self.assertEqual([m.id for m in cut], ['B-0002', 'B-0003'])

    def test_tokens_ceiling(self):
        body = '## Description\n' + ('x' * 200000) + '\n'
        rec = card('B-0001', 'bug', writes=['a'], extra={'severity': 'S2'}, body=body)
        self.assertEqual(delivery.estimate_tokens(rec, ['a']), 50000)
        heavy = Member('B-0001', 'bug', 't', ['a'], 1, 50000)
        light1 = Member('B-0002', 'bug', 't', ['b'], 2, 100)
        light2 = Member('B-0003', 'bug', 't', ['c'], 3, 100)
        cut = delivery.budget_cut([heavy, light1, light2], 4, 6, 5000)
        self.assertEqual([m.id for m in cut], ['B-0002', 'B-0003'])

    def test_zero_items_disables(self):
        members = [Member('B-0001', 'bug', 't', ['a'], 1, 0), Member('B-0002', 'bug', 't', ['a'], 2, 0)]
        self.assertIsNone(delivery.budget_cut(members, 0, 6, 60000))

    def test_one_item_is_no_delivery(self):
        members = [Member('B-0001', 'bug', 't', ['a'], 1, 0)]
        self.assertIsNone(delivery.budget_cut(members, 4, 6, 60000))


class ReleasableTest(unittest.TestCase):
    def test_member_without_a_commit_is_releasable(self):
        lead = card('F-0097', 'feature', state='Closed', extra={'delivers': ['F-0097', 'B-0034']})
        member = card('B-0034', 'bug', state='New')
        canonical = {'F-0097': lead, 'B-0034': member}
        self.assertEqual(delivery.releasable(canonical), [('F-0097', 'B-0034')])

    def test_nothing_landed_yet_is_not(self):
        lead = card('F-0097', 'feature', state='New', extra={'delivers': ['F-0097', 'B-0034']})
        member = card('B-0034', 'bug', state='New')
        canonical = {'F-0097': lead, 'B-0034': member}
        self.assertEqual(delivery.releasable(canonical), [])

    def test_released_lead_ends_the_delivery(self):
        lead = card('F-0097', 'feature', state='New',
                    extra={'delivers': ['F-0097', 'B-0034', 'S-0055']})
        landed_member = card('B-0034', 'bug', state='Closed')
        open_member = card('S-0055', 'story', state='New')
        canonical = {'F-0097': lead, 'B-0034': landed_member, 'S-0055': open_member}
        self.assertEqual(delivery.releasable(canonical), [('F-0097', 'S-0055')])


if __name__ == '__main__':
    unittest.main()
