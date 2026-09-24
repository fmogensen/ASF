import unittest

from asf.savings import landed


def session(ts, branch='worker/T-1', minutes=10.0, usd=None, round=None, kind='code', item='T-1', in_tokens=None):
    return {'ts': ts, 'branch': branch, 'minutes': minutes, 'usd': usd, 'round': round, 'kind': kind,
            'item': item, 'in_tokens': in_tokens}


def landing(ts='2026-09-21T12:00:00Z', branch='worker/T-1', kind='code', item='T-1', job='j1', sha='abc'):
    return {'ts': ts, 'job': job, 'sha': sha, 'branch': branch, 'kind': kind, 'item': item}


def gate(ts, branches, seconds):
    return {'ts': ts, 'branches': branches, 'seconds': seconds}


class LandedTests(unittest.TestCase):
    def test_four_sessions_one_landing_sum_to_one_row(self):
        ss = [session('2026-09-21T09:00:00Z', minutes=5, round=1, usd=1.0, in_tokens=100),
              session('2026-09-21T10:00:00Z', minutes=7, round=2, usd=0.5, in_tokens=50),
              session('2026-09-21T11:00:00Z', minutes=3, kind='review'),
              session('2026-09-21T11:30:00Z', minutes=4, kind='fix'),
              session('2026-09-21T09:00:00Z', branch='worker/T-2', minutes=99)]
        (row,) = landed.landed_changes([landing()], ss, [])
        self.assertEqual((row.sessions, row.rounds, row.minutes), (4, 2, 19))
        self.assertEqual((row.usd, row.in_tokens), (1.5, 150))

    def test_session_after_the_landing_is_not_counted(self):
        ss = [session('2026-09-21T11:00:00Z', minutes=5), session('2026-09-21T13:00:00Z', minutes=50)]
        (row,) = landed.landed_changes([landing()], ss, [])
        self.assertEqual((row.sessions, row.minutes), (1, 5))

    def test_combined_gate_splits_over_its_branches(self):
        gs = [gate('2026-09-21T11:00:00Z', ['worker/T-1', 'worker/T-2'], 600),
              gate('2026-09-21T11:30:00Z', ['worker/T-1'], 60),
              gate('2026-09-21T12:30:00Z', ['worker/T-1'], 600)]
        rows = landed.landed_changes([landing(), landing(branch='worker/T-2', job='j2', sha='def')], [], gs)
        self.assertEqual((rows[0].gate_minutes, rows[0].gates), (6.0, 2))
        self.assertEqual((rows[1].gate_minutes, rows[1].gates), (5.0, 1))

    def test_usd_stays_none_when_no_session_carried_one(self):
        (row,) = landed.landed_changes([landing()], [session('2026-09-21T09:00:00Z')], [])
        self.assertIsNone(row.usd)
        self.assertIsNone(row.in_tokens)

    def test_no_branch_falls_back_to_item_and_kind(self):
        ss = [session('2026-09-21T09:00:00Z', branch=None, minutes=4),
              session('2026-09-21T09:30:00Z', branch=None, minutes=6, kind='fix'),
              session('2026-09-21T09:40:00Z', branch=None, minutes=8, item='T-2')]
        (row,) = landed.landed_changes([landing(branch=None)], ss, [])
        self.assertEqual((row.sessions, row.minutes, row.gates), (1, 4, 0))

    def test_per_kind_medians_and_the_all_row(self):
        def row(kind, minutes, usd, rounds):
            return landed.Landed('j', 'b', kind, None, 's', 't', 1, rounds, minutes, usd, None, 1.0, 1)
        rows = [row('code', 10, 1.0, 1), row('code', 30, None, 3), row('code', 20, 3.0, 2), row('fix', 5, None, 1)]
        out = landed.per_kind(rows)
        self.assertEqual(set(out), {'code', 'fix', 'all'})
        self.assertEqual((out['code']['landings'], out['code']['minutes'], out['code']['usd'],
                          out['code']['rounds']), (3, 20, 2.0, 2))
        self.assertEqual((out['all']['landings'], out['all']['minutes']), (4, 15.0))
        self.assertIsNone(out['all']['in_tokens'])

    def test_a_kind_with_one_landing_has_a_median(self):
        rows = landed.landed_changes([landing()], [session('2026-09-21T09:00:00Z', minutes=8, usd=2.0)], [])
        cell = landed.per_kind(rows)['code']
        self.assertEqual((cell['landings'], cell['minutes'], cell['usd'], cell['rounds']), (1, 8, 2.0, 1))
        self.assertIsNone(landed.per_kind([])['all']['minutes'])


if __name__ == '__main__':
    unittest.main()
