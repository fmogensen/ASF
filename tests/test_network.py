"""asf.tick.network — the reachability probe: a fake probe stands in for the socket, so nothing
here touches the network."""
import socket
import unittest
from unittest import mock

from asf.tick import network, tick


class ReachableTest(unittest.TestCase):
    def setUp(self):
        network.forget()
        self.addCleanup(network.forget)

    def test_a_probe_that_connects_is_reachable(self):
        r = network.reachable(probe=lambda h, t: (True, ''))
        self.assertIs(r.ok, True)
        self.assertFalse(r.offline)
        self.assertEqual(r.label(), 'reachable')

    def test_a_dns_failure_is_offline_with_its_reason(self):
        r = network.reachable(probe=lambda h, t: (False, 'DNS: nodename nor servname provided'))
        self.assertIs(r.ok, False)
        self.assertTrue(r.offline)
        self.assertEqual(r.reason, 'DNS: nodename nor servname provided')

    def test_a_timeout_is_unknown_and_a_raising_probe_too(self):
        self.assertIsNone(network.reachable(probe=lambda h, t: (None, 'timed out')).ok)
        network.forget()

        def boom(h, t):
            raise RuntimeError('socket gone')
        r = network.reachable(probe=boom)
        self.assertIsNone(r.ok)
        self.assertEqual(r.label(), 'unknown')
        self.assertIn('socket gone', r.reason)

    def test_one_answer_per_process_within_the_memo(self):
        probe = mock.Mock(return_value=(True, ''))
        network.reachable(probe=probe, now=100.0)
        network.reachable(probe=probe, now=100.0 + network.MEMO_S)
        self.assertEqual(probe.call_count, 1)
        network.reachable(probe=probe, now=101.0 + network.MEMO_S)
        self.assertEqual(probe.call_count, 2)

    def test_the_socket_probe_reads_dns_then_connect(self):
        with mock.patch.object(network.socket, 'getaddrinfo',
                               side_effect=socket.gaierror(8, 'nodename nor servname provided')):
            ok, reason = network._probe('github.com', 1)
        self.assertIs(ok, False)
        self.assertIn('DNS', reason)
        addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        sock = mock.MagicMock()
        sock.__enter__.return_value.connect.side_effect = socket.timeout()
        with mock.patch.object(network.socket, 'getaddrinfo', return_value=addr), \
                mock.patch.object(network.socket, 'socket', return_value=sock):
            ok, reason = network._probe('github.com', 1)
        self.assertIsNone(ok)
        self.assertIn('timed out', reason)
        sock.__enter__.return_value.connect.side_effect = OSError(51, 'Network is unreachable')
        with mock.patch.object(network.socket, 'getaddrinfo', return_value=addr), \
                mock.patch.object(network.socket, 'socket', return_value=sock):
            ok, reason = network._probe('github.com', 1)
        self.assertIs(ok, False)
        sock.__enter__.return_value.connect.side_effect = None
        with mock.patch.object(network.socket, 'getaddrinfo', return_value=addr), \
                mock.patch.object(network.socket, 'socket', return_value=sock):
            self.assertEqual(network._probe('github.com', 1), (True, ''))


class OfflineTextTest(unittest.TestCase):
    def test_git_errors_that_mean_no_route_out(self):
        self.assertTrue(network.is_offline_text("fatal: Could not resolve host: github.com"))
        self.assertTrue(network.is_offline_text('ssh: connect to host x port 22: Network is unreachable'))
        self.assertFalse(network.is_offline_text('fatal: refusing to merge unrelated histories'))
        self.assertFalse(network.is_offline_text(None))

    def test_the_tick_reason_is_the_same_list(self):
        self.assertIs(tick._OFFLINE_MARKERS, network.OFFLINE_MARKERS)
        self.assertEqual(tick._reason('fatal: unable to access: Could not resolve host: x\nmore'), 'offline')
        self.assertEqual(tick._reason('fatal: bad object\n'), 'fatal: bad object')


if __name__ == '__main__':
    unittest.main()
