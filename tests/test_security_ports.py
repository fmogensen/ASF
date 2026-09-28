""":mod:`asf.security.ports` (T-0364): the nightly probe of every box's exposed ports, and the
address it never writes down. ``targets`` unions ``ci.pool`` with ``security.ports.boxes``;
``probe`` connects over TCP with an injectable connector and never returns an address; ``dispatch``
and ``latest`` talk to a fake ``gh``; ``violations`` turns a probe's own results into the three
``R-0010`` line shapes."""
import contextlib
import datetime
import io
import json
import os
import socket
import unittest
from unittest import mock

from asf import env
from asf.security import ports


def product(ci_pool=None, ports_cfg=None, name='p'):
    data = {'repo_slug': 'o/r', 'main': 'main'}
    if ci_pool is not None:
        data['ci'] = {'pool': ci_pool}
    if ports_cfg is not None:
        data['conventions'] = {'security': {'ports': ports_cfg}}
    return env.Product(name, data)


class Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """A ``subprocess.run``-shaped callable that records every argv and answers through a
    handler; no test here shells out."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return self.handler(args, **kwargs)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(ports.TARGETS_ENV, None)

    def tearDown(self):
        os.environ.pop(ports.TARGETS_ENV, None)
        if self._saved is not None:
            os.environ[ports.TARGETS_ENV] = self._saved

    def test_targets_env_names_the_variable_once(self):
        self.assertEqual(ports.TARGETS_ENV, 'ASF_PORT_TARGETS')

    def test_targets_unions_ci_pool_boxes_and_security_ports_boxes(self):
        p = product(ci_pool=[{'runner': 'ci-1', 'box': 'box-1', 'provider': 'acme', 'role': 'heavy'}],
                   ports_cfg={'boxes': ['box-2']})
        os.environ[ports.TARGETS_ENV] = json.dumps({'box-1': '10.0.0.1', 'box-2': '10.0.0.2'})
        self.assertEqual(ports.targets(p), [('box-1', '10.0.0.1'), ('box-2', '10.0.0.2')])

    def test_a_configured_box_with_no_address_comes_back_unprobed(self):
        p = product(ci_pool=[{'runner': 'ci-1', 'box': 'box-1', 'provider': 'acme', 'role': 'heavy'}])
        self.assertEqual(ports.targets(p), [('box-1', '')])

    def test_a_malformed_targets_env_reads_as_no_addresses(self):
        p = product(ports_cfg={'boxes': ['box-1']})
        os.environ[ports.TARGETS_ENV] = 'not json'
        self.assertEqual(ports.targets(p), [('box-1', '')])

    def test_a_box_named_by_both_sources_is_listed_once(self):
        p = product(ci_pool=[{'runner': 'ci-1', 'box': 'box-1', 'provider': 'acme', 'role': 'heavy'}],
                   ports_cfg={'boxes': ['box-1']})
        self.assertEqual(ports.targets(p), [('box-1', '')])

    def test_no_pool_and_no_ports_block_is_no_targets(self):
        self.assertEqual(ports.targets(product()), [])


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen(1)
        self.open_port = self.srv.getsockname()[1]
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(('127.0.0.1', 0))
        self.closed_port = closed.getsockname()[1]
        closed.close()

    def tearDown(self):
        self.srv.close()

    def test_reports_the_listening_port_open_and_the_unused_one_closed(self):
        result = ports.probe([('box-1', '127.0.0.1')], [self.open_port, self.closed_port])
        by_port = {r['port']: r['open'] for r in result['results']}
        self.assertEqual(by_port, {self.open_port: True, self.closed_port: False})
        self.assertTrue(all(r['box'] == 'box-1' for r in result['results']))

    def test_a_box_with_no_address_yields_no_result_rows(self):
        result = ports.probe([('box-1', '')], [self.open_port])
        self.assertEqual(result['results'], [])

    def test_the_serialized_result_names_no_address_anywhere(self):
        result = ports.probe([('box-1', '127.0.0.1')], [self.open_port, self.closed_port])
        self.assertNotIn('127.0.0.1', json.dumps(result))

    def test_probe_honours_the_timeout(self):
        seen = []

        def fake_connect(address, port, timeout):
            seen.append((address, port, timeout))
            return False

        ports.probe([('box-1', '10.0.0.9')], [9999], connect=fake_connect, timeout=0.01)
        self.assertEqual(seen, [('10.0.0.9', 9999, 0.01)])

    def test_from_carries_a_runner_label_not_an_address(self):
        result = ports.probe([('box-1', '127.0.0.1')], [self.open_port])
        self.assertTrue(result['from'])
        self.assertNotEqual(result['from'], '127.0.0.1')


class DispatchTests(unittest.TestCase):
    def test_dispatch_calls_gh_workflow_run_with_the_configured_workflow(self):
        p = product(ports_cfg={'boxes': ['box-1'], 'workflow': 'security-ports.yml',
                               'artifact': 'security-ports'})
        fake = FakeRun(lambda args, **kw: Result(0))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ports.dispatch(p, run=fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [['gh', 'workflow', 'run', 'security-ports.yml',
                                       '-R', 'o/r', '--ref', 'main']])

    def test_a_product_with_no_ports_block_prints_no_boxes_configured_and_returns_0(self):
        fake = FakeRun(lambda args, **kw: Result(0))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ports.dispatch(product(), run=fake)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), 'no boxes configured')
        self.assertEqual(fake.calls, [])

    def test_a_refused_dispatch_returns_1(self):
        p = product(ports_cfg={'workflow': 'security-ports.yml', 'artifact': 'security-ports'})
        fake = FakeRun(lambda args, **kw: Result(1, stderr='refused'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ports.dispatch(p, run=fake)
        self.assertEqual(rc, 1)
        self.assertIn('refused', out.getvalue())

    def test_latest_reads_the_newest_successful_runs_artifact(self):
        p = product(ports_cfg={'workflow': 'security-ports.yml', 'artifact': 'security-ports'})
        payload = {'ts': '2026-01-02T00:00:00+00:00', 'from': 'runner-1', 'results': []}

        def handler(args, **kw):
            if args[1:3] == ['run', 'list']:
                return Result(0, stdout=json.dumps([{'databaseId': 42, 'createdAt': 'x'}]))
            if args[1:3] == ['run', 'download']:
                self.assertEqual(args[3], '42')
                d = args[args.index('-D') + 1]
                with open(os.path.join(d, 'ports.json'), 'w', encoding='utf-8') as f:
                    json.dump(payload, f)
                return Result(0)
            raise AssertionError(args)

        data, why = ports.latest(p, run=handler)
        self.assertIsNone(why)
        self.assertEqual(data, payload)

    def test_latest_removes_its_temp_dir(self):
        p = product(ports_cfg={'workflow': 'security-ports.yml', 'artifact': 'security-ports'})
        captured = {}

        def handler(args, **kw):
            if args[1:3] == ['run', 'list']:
                return Result(0, stdout=json.dumps([{'databaseId': 1, 'createdAt': 'x'}]))
            d = args[args.index('-D') + 1]
            captured['dir'] = d
            with open(os.path.join(d, 'ports.json'), 'w', encoding='utf-8') as f:
                json.dump({'ts': 'x', 'from': 'r', 'results': []}, f)
            return Result(0)

        ports.latest(p, run=handler)
        self.assertFalse(os.path.exists(captured['dir']))

    def test_latest_returns_none_why_when_there_is_no_successful_run(self):
        p = product(ports_cfg={'workflow': 'security-ports.yml', 'artifact': 'security-ports'})
        data, why = ports.latest(p, run=lambda args, **kw: Result(0, stdout='[]'))
        self.assertIsNone(data)
        self.assertEqual(why, 'no successful run')

    def test_latest_returns_none_why_when_gh_run_list_fails(self):
        p = product(ports_cfg={'workflow': 'security-ports.yml', 'artifact': 'security-ports'})
        data, why = ports.latest(p, run=lambda args, **kw: Result(1, stderr='boom'))
        self.assertIsNone(data)
        self.assertIn('boom', why)

    def test_latest_for_a_product_with_no_ports_block(self):
        data, why = ports.latest(product(), run=lambda args, **kw: Result(0))
        self.assertIsNone(data)
        self.assertEqual(why, 'no boxes configured')


class ViolationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)

    def test_an_open_port_is_one_sev_s1_sig_port_line(self):
        p = product(ports_cfg={'boxes': ['box-1'], 'max_age_h': 30})
        data = {'ts': self.now.isoformat(), 'from': 'runner-1',
               'results': [{'box': 'box-1', 'port': 5432, 'open': True}]}
        with mock.patch.object(ports, 'latest', return_value=(data, None)):
            lines = ports.violations(p, now=self.now)
        self.assertEqual(lines, [f"R-0010 exposed port box-1:5432 open from a runner at "
                                 f"{data['ts']} sev=S1 sig=port-box-1-5432"])

    def test_an_unprobed_box_is_one_sev_s1_sig_unprobed_line(self):
        p = product(ports_cfg={'boxes': ['box-1', 'box-2'], 'max_age_h': 30})
        data = {'ts': self.now.isoformat(), 'from': 'runner-1',
               'results': [{'box': 'box-1', 'port': 5432, 'open': False}]}
        with mock.patch.object(ports, 'latest', return_value=(data, None)):
            lines = ports.violations(p, now=self.now)
        self.assertEqual(lines, ["R-0010 box box-2 was not probed (no address in the probe's "
                                 "targets) sev=S1 sig=unprobed-box-2"])

    def test_a_40h_old_probe_is_one_probe_stale_line(self):
        p = product(ports_cfg={'boxes': ['box-1'], 'max_age_h': 30})
        old = self.now - datetime.timedelta(hours=40)
        data = {'ts': old.isoformat(), 'from': 'runner-1',
               'results': [{'box': 'box-1', 'port': 5432, 'open': False}]}
        with mock.patch.object(ports, 'latest', return_value=(data, None)):
            lines = ports.violations(p, now=self.now)
        self.assertEqual(lines, ['R-0010 the last port probe is 40h old (older than 30h) '
                                 'sev=S2 sig=probe-stale'])

    def test_a_clean_fresh_probe_is_no_lines(self):
        p = product(ports_cfg={'boxes': ['box-1'], 'max_age_h': 30})
        fresh = self.now - datetime.timedelta(hours=1)
        data = {'ts': fresh.isoformat(), 'from': 'runner-1',
               'results': [{'box': 'box-1', 'port': 5432, 'open': False}]}
        with mock.patch.object(ports, 'latest', return_value=(data, None)):
            lines = ports.violations(p, now=self.now)
        self.assertEqual(lines, [])

    def test_no_successful_run_is_one_probe_stale_line(self):
        p = product(ports_cfg={'boxes': ['box-1'], 'max_age_h': 30})
        with mock.patch.object(ports, 'latest', return_value=(None, 'no successful run')):
            lines = ports.violations(p, now=self.now)
        self.assertEqual(lines, ['R-0010 the last port probe is unreadable (no successful run) '
                                 'sev=S2 sig=probe-stale'])


if __name__ == '__main__':
    unittest.main()
