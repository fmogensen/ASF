"""tests.test_quota_pressure — F-0068: the seats, the keys, the count, the rule, the line, the
lane, the instrument.

Every fence in this module runs from the repository root. None of them launches a session,
reaches a network or spends money.
"""
import datetime
import itertools
import unittest

from asf import capacity
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod

UTC = datetime.timezone.utc

FREE = {'five_h_pct': 0, 'seven_d_pct': 0}
COOLDOWN = {'five_h_pct': 91, 'seven_d_pct': 0}
STOP = {'five_h_pct': 95, 'seven_d_pct': 0}


class CountingQuotaSource(quota_mod.FakeQuotaSource):
    """A :class:`~asf.workers.quota.FakeQuotaSource` that counts its own ``read`` calls, so a
    test can prove an account was asked at most once."""

    def __init__(self, table=None):
        super().__init__(table)
        self.calls = 0

    def read(self, account):
        self.calls += 1
        return super().read(account)


class SeatsTests(unittest.TestCase):
    """``Pool.banded_seats()`` — §3.1, §3.2."""

    def accounts(self):
        return [pool_mod.Account('a', cap=3), pool_mod.Account('b', cap=2),
                pool_mod.Account('c', cap=1)]

    def pool(self, table=None, limits=None, source=None):
        source = source or CountingQuotaSource(table)
        return source, pool_mod.Pool(self.accounts(), quota_source=source,
                                     guards=quota_mod.guards_from_config({}), limits=limits)

    def test_all_free(self):
        _source, p = self.pool()
        self.assertEqual(p.banded_seats(), (0, 6))

    def test_one_stopped(self):
        _source, p = self.pool({'a': STOP})
        self.assertEqual(p.banded_seats(), (3, 6))

    def test_one_cooling(self):
        _source, p = self.pool({'a': COOLDOWN})
        self.assertEqual(p.banded_seats(), (2, 6))

    def test_reading_source_asked_once_per_account_across_two_calls(self):
        source, p = self.pool()
        p.banded_seats()
        p.banded_seats()
        self.assertEqual(source.calls, 3)

    def test_a_session_limit_stop_never_reads_quota_and_counts_its_full_cap(self):
        until = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        source, p = self.pool(limits={'a': {'until': until}})
        banded, caps = p.banded_seats()
        p.banded_seats()
        self.assertEqual(banded, 3)  # a's full cap, though never read
        self.assertEqual(caps, 6)
        self.assertEqual(source.calls, 2)  # b and c only, across both calls

    def test_caps_less_banded_is_usable_slots(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'a', 'cap': 3}, {'name': 'b', 'cap': 2},
                                             {'name': 'c', 'cap': 1}]}}
        for a_state, b_state, c_state in itertools.product((FREE, COOLDOWN, STOP), repeat=3):
            table = {'a': a_state, 'b': b_state, 'c': c_state}
            source = quota_mod.FakeQuotaSource(table)
            accounts = pool_mod.accounts_from_config(cfg)
            p = pool_mod.Pool(accounts, quota_source=source, guards=quota_mod.guards_from_config({}))
            banded, caps = p.banded_seats()
            self.assertEqual(caps - banded, capacity.usable_slots(cfg, source),
                             (a_state, b_state, c_state))


if __name__ == '__main__':
    unittest.main()
