"""The host-pressure guard: a loaded host holds new launches (a deterministic policy, no LLM).

Incident: a 15-minute load of 99 and swap 12.6/13.3 GB — two full suites at once on one host.
The tick must not start another session into that; the sessions already running are left alone.
"""
import os
import unittest
from unittest import mock

from asf.workers import host


class GuardsFromConfig(unittest.TestCase):
    def test_defaults_when_the_key_is_absent(self):
        self.assertEqual(host.guards_from_config({}),
                         {'load_per_core': host.DEFAULT_LOAD_PER_CORE,
                          'swap_pct': host.DEFAULT_SWAP_PCT})
        self.assertEqual(host.DEFAULT_LOAD_PER_CORE, 2.0)
        self.assertEqual(host.DEFAULT_SWAP_PCT, 85)

    def test_the_operator_config_wins(self):
        g = host.guards_from_config({'host_guards': {'load_per_core': 3, 'swap_pct': 95}})
        self.assertEqual(g, {'load_per_core': 3.0, 'swap_pct': 95.0})

    def test_zero_turns_one_guard_off(self):
        g = host.guards_from_config({'host_guards': {'load_per_core': 0}})
        self.assertIsNone(g['load_per_core'])
        self.assertEqual(g['swap_pct'], host.DEFAULT_SWAP_PCT)


class Judge(unittest.TestCase):
    G = {'load_per_core': 2.0, 'swap_pct': 85}

    def test_the_incident_is_held_and_says_why(self):
        held, why = host.judge({'load15': 90.4, 'cores': 12, 'swap_pct': 87.2}, self.G)
        self.assertTrue(held)
        self.assertEqual(why, 'host pressure load 90/cores 12, swap 87%')

    def test_load_alone_holds(self):
        held, why = host.judge({'load15': 25, 'cores': 12, 'swap_pct': 10}, self.G)
        self.assertTrue(held)
        self.assertIn('load 25/cores 12', why)

    def test_swap_alone_holds(self):
        held, _why = host.judge({'load15': 1, 'cores': 12, 'swap_pct': 90}, self.G)
        self.assertTrue(held)

    def test_a_quiet_host_is_free(self):
        self.assertEqual(host.judge({'load15': 23.9, 'cores': 12, 'swap_pct': 84}, self.G),
                         (False, ''))

    def test_an_unreadable_value_never_holds(self):
        # unknown is not pressure: a probe that cannot read must not stop the factory
        self.assertEqual(host.judge({'load15': None, 'cores': None, 'swap_pct': None}, self.G),
                         (False, ''))
        self.assertEqual(host.judge(None, self.G), (False, ''))

    def test_a_guard_turned_off_never_holds(self):
        g = {'load_per_core': None, 'swap_pct': None}
        self.assertEqual(host.judge({'load15': 900, 'cores': 1, 'swap_pct': 100}, g), (False, ''))


class Probes(unittest.TestCase):
    def test_macos_swapusage_is_parsed(self):
        text = 'total = 13312.00M  used = 12595.25M  free = 716.75M  (encrypted)'
        self.assertAlmostEqual(host.parse_swapusage(text), 94.6, places=1)

    def test_no_swap_is_zero(self):
        self.assertEqual(host.parse_swapusage('total = 0.00M  used = 0.00M  free = 0.00M'), 0.0)
        self.assertIsNone(host.parse_swapusage('garbage'))

    def test_linux_meminfo_is_parsed(self):
        text = 'MemTotal: 100 kB\nSwapTotal:  1000 kB\nSwapFree:  150 kB\n'
        self.assertAlmostEqual(host.parse_meminfo(text), 85.0)
        self.assertEqual(host.parse_meminfo('SwapTotal: 0 kB\nSwapFree: 0 kB\n'), 0.0)
        self.assertIsNone(host.parse_meminfo('MemTotal: 1 kB\n'))

    def test_the_fixed_reading_env_stands_in_for_the_host(self):
        with mock.patch.dict(os.environ, {host.READING_ENV: '90 12 87'}):
            self.assertEqual(host.probe().read(), {'load15': 90.0, 'cores': 12, 'swap_pct': 87.0})

    def test_the_system_probe_reads_something_and_never_raises(self):
        r = host.SystemProbe().read()
        self.assertEqual(set(r), {'load15', 'cores', 'swap_pct', 'mem_pct'})

    def test_the_suite_runs_on_a_quiet_host(self):
        # hermetic: a loaded developer machine must not turn every launching test red
        self.assertFalse(host.pressure({})[0])


if __name__ == '__main__':
    unittest.main()


class MemoryPressureReading(unittest.TestCase):
    """Where the host reports memory pressure, the memory guard judges it, not sticky swap."""

    def test_memory_reading_replaces_swap_when_present(self):
        g = {'load_per_core': 3.0, 'swap_pct': 85}
        self.assertEqual(host.judge({'load15': 1, 'cores': 10, 'swap_pct': 90, 'mem_pct': 31}, g),
                         (False, ''))
        held, why = host.judge({'load15': 1, 'cores': 10, 'swap_pct': 10, 'mem_pct': 92}, g)
        self.assertTrue(held)
        self.assertIn('memory 92%', why)

    def test_swap_still_judged_without_a_memory_reading(self):
        held, why = host.judge({'load15': 1, 'cores': 10, 'swap_pct': 90},
                               {'load_per_core': 3.0, 'swap_pct': 85})
        self.assertTrue(held)
        self.assertIn('swap 90%', why)
