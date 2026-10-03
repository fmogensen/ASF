"""The host network probe clock (log-only): fake resolver, probe and tools — nothing here touches
the network, launchd or the real ``ASF_HOME``."""
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

from asf import doctor, scheduler, surface
from asf.tick import network

ROUTE = "   route to: default\n    gateway: 192.168.1.1\n  interface: en0\n"
DNS = "resolver #1\n  nameserver[0] : 1.1.1.1\n  nameserver[1] : 8.8.8.8\nresolver #2\n  nameserver[0] : 1.1.1.1\n"
NC = '* (Connected)   ABC-1 PPP --> L2TP   "Work VPN"   [PPP:L2TP]\n'
TS = json.dumps({'BackendState': 'Running', 'Self': {'Online': True}})


def resolver_ok(host, port, type=0):
    return [(2, 1, 6, '', ('203.0.113.7', port))]


def resolver_down(host, port, type=0):
    raise socket.gaierror(8, 'nodename nor servname provided')


def tools(route=ROUTE, dns=DNS, nc=NC, ts=TS):
    table = {'route': route, 'scutil --dns': dns, 'scutil --nc': nc, 'tailscale': ts}

    def run(argv):
        key = argv[0] if argv[0] in ('route', 'tailscale') else ' '.join(argv[:2])
        return table[key]
    return run


class ProbeRecordTest(unittest.TestCase):
    def test_reachable_record_has_every_layer(self):
        rec = network.probe_record(1000, probe=lambda h, t: (True, ''), run=tools(),
                                   resolve=resolver_ok)
        self.assertTrue(rec['ok'])
        self.assertIsNone(rec['failing'])
        self.assertEqual(rec['route'], {'interface': 'en0', 'gateway': '192.168.1.1'})
        self.assertEqual(rec['resolvers'], ['1.1.1.1', '8.8.8.8'])
        self.assertEqual(rec['vpn'], [{'name': 'Work VPN', 'state': 'Connected'}])
        self.assertEqual(rec['tailscale'], {'state': 'Running', 'online': True})
        self.assertEqual(set(rec['dns']), set(network.DEFAULT_HOSTS))

    def test_dns_down_with_a_route_is_the_dns_layer(self):
        rec = network.probe_record(1, probe=lambda h, t: self.fail('no connect without DNS'),
                                   run=tools(), resolve=resolver_down)
        self.assertFalse(rec['ok'])
        self.assertEqual(rec['failing'], 'dns')
        self.assertIn('not tried', rec['tcp']['github.com']['reason'])

    def test_dns_down_with_a_dropped_vpn_names_the_vpn(self):
        nc = '  (Disconnecting)   ABC-1 PPP --> L2TP   "Work VPN"\n'
        rec = network.probe_record(1, run=tools(nc=nc), resolve=resolver_down)
        self.assertEqual(rec['failing'], 'vpn')

    def test_dns_down_with_tailscale_stopped_names_tailscale(self):
        ts = json.dumps({'BackendState': 'Stopped'})
        rec = network.probe_record(1, run=tools(ts=ts), resolve=resolver_down)
        self.assertEqual(rec['failing'], 'tailscale')

    def test_no_route_is_the_interface_then_the_route_layer(self):
        rec = network.probe_record(1, run=tools(route=''), resolve=resolver_down)
        self.assertEqual(rec['failing'], 'interface')
        rec = network.probe_record(1, run=tools(route='  interface: en0\n'), resolve=resolver_down)
        self.assertEqual(rec['failing'], 'route')

    def test_resolves_but_cannot_connect_is_tcp(self):
        rec = network.probe_record(1, probe=lambda h, t: (False, 'connect: refused'),
                                   run=tools(), resolve=resolver_ok)
        self.assertEqual(rec['failing'], 'tcp')

    def test_a_missing_tool_and_a_raising_probe_never_raise(self):
        def boom(h, t):
            raise RuntimeError('x')
        rec = network.probe_record(1, probe=boom, run=lambda argv: None, resolve=resolver_ok)
        self.assertFalse(rec['ok'])
        self.assertIsNone(rec['tailscale'])
        self.assertEqual(rec['route'], {})


class LogTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.home, ignore_errors=True))

    def probe(self, now, up=True):
        return network.watchdog(self.home, {'network': {'probe': 'on'}}, now=now,
                                probe=lambda h, t: (up, ''), run=tools(),
                                resolve=resolver_ok)

    def test_off_probes_nothing_and_force_probes(self):
        self.assertIsNone(network.watchdog(self.home, {}, now=1, run=tools()))
        self.assertFalse(os.path.exists(os.path.join(self.home, 'state')))
        rec = network.watchdog(self.home, {}, now=1, force=True, probe=lambda h, t: (True, ''),
                               run=tools(), resolve=resolver_ok)
        self.assertTrue(rec['ok'])

    def test_each_probe_appends_and_replaces_the_latest(self):
        self.probe(100)
        self.probe(160, up=False)
        with open(os.path.join(self.home, 'state', 'network.jsonl')) as f:
            log = f.read().splitlines()
        self.assertEqual([json.loads(x)['at'] for x in log], [100, 160])
        self.assertEqual(network.read_latest(self.home)['at'], 160)

    def test_rotation_drops_records_older_than_seven_days(self):
        self.probe(0)
        self.probe(100)
        self.probe(network.KEEP_S + 150)
        ats = [r['at'] for r in network.read_log(self.home)]
        self.assertNotIn(0, ats)
        self.assertIn(network.KEEP_S + 150, ats)

    def test_offline_ticks_counts_the_window_by_layer(self):
        self.probe(60)
        self.probe(120, up=False)
        self.probe(180, up=False)
        self.probe(240)
        got = network.offline_ticks(self.home, since=100, until=200)
        self.assertEqual((got['probes'], got['offline'], got['offline_s']), (2, 2, 120))
        self.assertEqual(got['by_layer'], {'tcp': 2})
        self.assertEqual(network.offline_ticks(self.home, since=500)['probes'], 0)

    def test_log_line_names_the_failing_layer(self):
        rec = network.probe_record(1, run=tools(), resolve=resolver_down)
        line = network.log_line(rec)
        self.assertIn('OFFLINE failing=dns', line)
        self.assertIn('en0', line)

    def test_doctor_row(self):
        on = {'network': {'probe': 'on'}}
        self.assertIsNone(network.doctor_row(self.home, {}, now=1))
        self.assertFalse(network.doctor_row(self.home, on, now=1)[0])  # on, never ran
        self.probe(1000)
        self.assertTrue(network.doctor_row(self.home, on, now=1030)[0])
        ok, detail = network.doctor_row(self.home, on, now=1000 + network.STALE_S + 60)
        self.assertFalse(ok)
        self.assertIn('stopped', detail)
        self.probe(2000, up=False)
        ok, detail = network.doctor_row(self.home, on, now=2010)
        self.assertFalse(ok)
        self.assertIn('failing=tcp', detail)

    def test_doctor_check_reads_the_asf_home(self):
        with mock.patch.object(doctor.env, 'ASF_HOME', self.home):
            self.assertIsNone(doctor.check_network({}))


class ConfigTest(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(network.config_problems({}), [])
        self.assertEqual(network.config_problems({'network': {'probe': 'on', 'recover': 'off'}}), [])
        self.assertEqual([k for k, _ in network.config_problems({'network': {'probe': 'maybe'}})],
                         ['network.probe'])
        self.assertEqual([k for k, _ in network.config_problems({'network': 'on'})], ['network'])
        self.assertEqual([k for k, _ in network.config_problems({'network': {'hosts': 'x'}})],
                         ['network.hosts'])

    def test_enabled_and_hosts(self):
        self.assertTrue(network.enabled({'network': {'probe': 'on'}}))
        self.assertTrue(network.enabled({'network': {'probe': True}}))
        self.assertFalse(network.enabled({'network': {'probe': 'off'}}))
        self.assertEqual(network.hosts({'network': {'hosts': ['a.example']}}), ('a.example',))
        self.assertEqual(network.hosts({}), network.DEFAULT_HOSTS)


class ClockTest(unittest.TestCase):
    def test_off_renders_nothing(self):
        self.assertIsNone(scheduler.render_host({'network': {'probe': 'off'}}))

    def test_on_renders_a_host_label_outside_the_product_glob(self):
        job = scheduler.render_host({'network': {'probe': 'on'}})
        self.assertEqual(job['label'], 'asf.host.net-probe')
        self.assertEqual(job['argv'][-1], 'net-probe')
        self.assertEqual(job['plist']['StartInterval'], 60)
        self.assertEqual(job['every_s'], 60)
        self.assertTrue(job['path'].endswith('asf.host.net-probe.plist'))

    def test_the_command_is_declared_on_the_surface(self):
        self.assertIn('net-probe', surface.COMMANDS)


if __name__ == '__main__':
    unittest.main()
