"""``asf ruleset install|status|break-glass`` (:mod:`asf.trunk_ruleset`): the desired shape,
idempotence against a fake host, the dry-run payload, the break-glass log. Never a real host."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from asf import env, trunk_ruleset as tr

CHECKS = ['tests (3.12)', 'tests (3.13)']


def _product(checks=CHECKS, slug='o/p'):
    conv = {'landing': 'pull-request'}
    if checks is not None:
        conv['landing_checks'] = checks
    data = {'repo_dir': '/nowhere', 'main': 'main', 'conventions': conv}
    if slug:
        data['repo_slug'] = slug
    return env.Product('sample', data)


class Host:
    """A fake ``gh``: one repo's rulesets, with POST/PUT/DELETE applied."""

    def __init__(self, sets=None, rc=0):
        self.sets = {s['id']: dict(s) for s in sets or ()}
        self.calls, self.bodies, self.rc, self.next_id = [], [], rc, 100

    def __call__(self, args):
        self.calls.append(list(args))
        if self.rc:
            return self.rc, '', 'HTTP 500'
        method = args[args.index('-X') + 1] if '-X' in args else 'GET'
        ep = args[args.index('api') + 1 if args[0] != 'api' else 1]
        ep = args[1] if args[1] != '-X' else args[3]
        if method == 'GET' and ep.endswith('/rulesets'):
            return 0, json.dumps([{k: v for k, v in s.items() if k != 'rules'}
                                  for s in self.sets.values()]), ''
        if method == 'GET':
            return 0, json.dumps(self.sets[int(ep.rsplit('/', 1)[1])]), ''
        if method in ('POST', 'PUT'):
            with open(args[args.index('--input') + 1], encoding='utf-8') as f:
                body = json.load(f)
            self.bodies.append(body)
            if method == 'POST':
                self.next_id += 1
                self.sets[self.next_id] = dict(body, id=self.next_id)
            else:
                rid = int(ep.rsplit('/', 1)[1])
                self.sets[rid] = dict(body, id=rid)
            return 0, '{}', ''
        if method == 'DELETE':
            self.sets.pop(int(ep.rsplit('/', 1)[1]))
            return 0, '', ''
        return 1, '', 'unexpected'

    def mutations(self):
        return [c for c in self.calls if '-X' in c and c[c.index('-X') + 1] != 'GET']


def run(fn, *a, **k):
    out = []
    rc = fn(*a, say=out.append, **k)
    return rc, '\n'.join(out)


class Desired(unittest.TestCase):
    def test_the_shape(self):
        d = tr.desired(_product())
        self.assertEqual((d['name'], d['target'], d['enforcement'], d['bypass_actors']),
                         ('asf trunk', 'branch', 'active', []))
        self.assertEqual(d['conditions']['ref_name'],
                         {'include': ['refs/heads/main'], 'exclude': []})
        rules = {r['type']: r.get('parameters') for r in d['rules']}
        self.assertEqual(set(rules), {'deletion', 'non_fast_forward', 'required_status_checks',
                                      'pull_request'})
        rsc = rules['required_status_checks']
        self.assertTrue(rsc['strict_required_status_checks_policy'])
        self.assertEqual([c['context'] for c in rsc['required_status_checks']], CHECKS)
        self.assertEqual(rules['pull_request']['required_approving_review_count'], 0)

    def test_a_lone_string_is_one_check(self):
        d = tr.desired(_product(checks='gate'))
        self.assertEqual(d['rules'][2]['parameters']['required_status_checks'],
                         [{'context': 'gate'}])

    def test_no_landing_check_refuses(self):
        with self.assertRaises(ValueError):
            tr.desired(_product(checks=None))


class Install(unittest.TestCase):
    def test_creates_once_then_is_idempotent(self):
        host = Host()
        rc, out = run(tr.install, _product(), gh=host)
        self.assertEqual(rc, 0, out)
        (call,) = host.mutations()
        self.assertEqual(call[:4], ['api', '-X', 'POST', 'repos/o/p/rulesets'])
        self.assertEqual(host.bodies, [tr.desired(_product())])
        self.assertIn('created', out)
        rc, out = run(tr.install, _product(), gh=host)
        self.assertEqual(rc, 0)
        self.assertEqual(len(host.mutations()), 1, 'second run wrote nothing')
        self.assertIn('already matches', out)

    def test_a_drifted_ruleset_is_put_by_name(self):
        old = dict(tr.desired(_product()), id=7)
        old['rules'] = [r for r in old['rules'] if r['type'] != 'pull_request']
        old['enforcement'] = 'disabled'
        host = Host([old])
        rc, out = run(tr.install, _product(), gh=host)
        self.assertEqual(rc, 0, out)
        (call,) = host.mutations()
        self.assertEqual(call[:4], ['api', '-X', 'PUT', 'repos/o/p/rulesets/7'])
        self.assertEqual(host.sets[7]['enforcement'], 'active')

    def test_dry_run_prints_the_payload_and_writes_nothing(self):
        host = Host()
        rc, out = run(tr.install, _product(), gh=host, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(host.mutations(), [])
        self.assertIn('POST repos/o/p/rulesets', out)
        payload = json.loads(out[out.index('{'):out.rindex('}') + 1])
        self.assertEqual(payload, tr.desired(_product()))
        self.assertIn('would be created', out)

    def test_dry_run_names_the_diff_against_the_host(self):
        old = dict(tr.desired(_product()), id=7)
        old['rules'] = [r for r in old['rules'] if r['type'] != 'pull_request']
        host = Host([old])
        rc, out = run(tr.install, _product(), gh=host, dry_run=True)
        self.assertIn('PUT repos/o/p/rulesets/7', out)
        self.assertIn('+ rule pull_request', out)
        self.assertEqual(host.mutations(), [])

    def test_another_active_ruleset_is_noted(self):
        other = {'id': 3, 'name': 'one door to main (asf/queue)', 'target': 'branch',
                 'enforcement': 'active', 'rules': []}
        rc, out = run(tr.install, _product(), gh=Host([other]), dry_run=True)
        self.assertIn('another active ruleset', out)

    def test_no_slug_or_no_checks_or_host_down(self):
        self.assertEqual(run(tr.install, _product(slug=None), gh=Host())[0], 2)
        self.assertEqual(run(tr.install, _product(checks=None), gh=Host())[0], 2)
        host = Host(rc=1)
        self.assertEqual(run(tr.install, _product(), gh=host)[0], 1)
        self.assertEqual(host.mutations(), [])

    def test_status(self):
        host = Host()
        self.assertEqual(run(tr.status, _product(), gh=host)[0], 1)
        run(tr.install, _product(), gh=host)
        rc, out = run(tr.status, _product(), gh=host)
        self.assertEqual(rc, 0)
        self.assertIn('matches', out)


class BreakGlass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(env, 'ASF_HOME', self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def log(self):
        path = os.path.join(self.tmp, 'state', 'sample', 'break-glass.log')
        with open(path, encoding='utf-8') as f:
            return f.read().splitlines()

    def test_off_deletes_loudly_and_logs_and_on_reinstalls(self):
        host = Host()
        product = _product()
        run(tr.install, product, gh=host)
        rc, out = run(tr.break_glass_off, product, gh=host)
        self.assertEqual(rc, 0)
        self.assertIn('BREAK-GLASS', out)
        self.assertEqual(host.sets, {})
        self.assertEqual(len(self.log()), 1)
        self.assertIn('OFF', self.log()[0])
        args = mock.Mock(action='break-glass', on=True, off=False, dry_run=False, product=None)
        with mock.patch.object(env, 'load_product', return_value=product), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tr.cmd_ruleset(args, gh=host), 0)
        self.assertEqual(len(host.sets), 1)
        self.assertIn('ON', self.log()[1])

    def test_off_when_absent_is_a_no_op(self):
        rc, out = run(tr.break_glass_off, _product(), gh=Host())
        self.assertEqual(rc, 0)
        self.assertIn('already open', out)

    def test_exactly_one_of_on_off(self):
        args = mock.Mock(action='break-glass', on=False, off=False, dry_run=False, product=None)
        with mock.patch.object(env, 'load_product', return_value=_product()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tr.cmd_ruleset(args, gh=Host()), 2)


class Reads(unittest.TestCase):
    def test_queue_only_ruleset_diffs(self):
        queue = {'id': 1, 'name': 'asf trunk', 'target': 'branch', 'enforcement': 'active',
                 'bypass_actors': [], 'conditions': {'ref_name': {'include': ['refs/heads/main'],
                                                                   'exclude': []}},
                 'rules': [{'type': 'deletion'}, {'type': 'non_fast_forward'},
                           {'type': 'required_status_checks', 'parameters': {
                               'strict_required_status_checks_policy': False,
                               'do_not_enforce_on_create': False,
                               'required_status_checks': [{'context': 'asf/queue'}]}}]}
        lines = tr.diff(queue, tr.desired(_product()))
        self.assertTrue(any(l.startswith('~ rule required_status_checks') for l in lines))
        self.assertTrue(any(l.startswith('+ rule pull_request') for l in lines))


if __name__ == '__main__':
    unittest.main()
