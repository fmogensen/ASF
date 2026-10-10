"""asf.release — the release channel's own upgrade and rollback lines (F-0112), counted by
``asf release-readiness``'s upgrade-safety criterion exactly as the trunk channel's are (PD7)."""
import unittest

from asf import release, upgrade

SINCE = '2026-09-18T12:00:00Z'


class ReleaseChannelUpgradesAreCountedTests(unittest.TestCase):
    """PD7: the sha-shaped line the release channel writes is counted by
    :func:`asf.release.parse_tick_log` and :func:`asf.release.upgrade_facts` as an upgrade, and
    the rollback line as a rollback (D9, P7). Built from the same formatter the channel uses
    (:func:`asf.upgrade._tick_upgrade_line`), never a copy of the string, so the two cannot
    drift apart."""

    OLD_SHA = 'a1b2c3d4e5' + '0' * 30
    NEW_SHA = 'b1c2d3e4f5' + '0' * 30

    def test_the_upgrade_line_is_counted_as_an_upgrade(self):
        line = upgrade._tick_upgrade_line('v0.1.62', self.OLD_SHA, 'v0.1.63', self.NEW_SHA)
        events = release.parse_tick_log(line)
        self.assertEqual(events, [('upgrade', self.OLD_SHA[:7], self.NEW_SHA[:7], 0)])
        dates = {self.NEW_SHA[:7]: '2026-09-20T00:00:00Z'}
        facts = release.upgrade_facts([('tick-p.log', events)], dates, SINCE)
        self.assertEqual(len(facts['upgrades']), 1)
        self.assertEqual(facts['upgrades'][0]['to'], self.NEW_SHA[:7])
        self.assertEqual(facts['failed'], [])

    def test_the_rollback_line_is_counted_as_a_rollback(self):
        upgrade_line = upgrade._tick_upgrade_line('v0.1.62', self.OLD_SHA, 'v0.1.63', self.NEW_SHA)
        rollback_line = 'rollback: doctor RED after v0.1.63 — reinstalling v0.1.62'
        log = upgrade_line + '\n' + rollback_line
        events = release.parse_tick_log(log)
        self.assertEqual([e[0] for e in events], ['upgrade', 'rollback'])
        dates = {self.NEW_SHA[:7]: '2026-09-20T00:00:00Z'}
        facts = release.upgrade_facts([('tick-p.log', events)], dates, SINCE)
        self.assertEqual(len(facts['rollbacks']), 1)


if __name__ == '__main__':
    unittest.main()
