"""One door to the trunk, host side: the merge queue posts ``ci.queue_status`` (``asf/queue``) =
success on the exact batch sha before it moves the trunk there, and the doctor reads the trunk's
ruleset that requires it (:mod:`asf.trunk_ruleset`)."""
import json
import unittest
from unittest import mock

from asf import conventions, env, gh_limit, merge_queue, trunk_ruleset
from asf.harvest import harvest

from tests import test_merge_queue as tmq


def _statuses(calls):
    return [c for c in calls if c[:3] == ['api', '-X', 'POST'] and '/statuses/' in c[3]]


def _field(call, key):
    for i, a in enumerate(call):
        if a == '-f' and call[i + 1].startswith(f'{key}='):
            return call[i + 1].split('=', 1)[1]
    return None


class QueueAttests(tmq.QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')

    def land_one(self, product=None):
        self.queue_pass(self.lane(product), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.green(batch['sha'])
        self.queue_pass(self.lane(product), [])
        return batch

    def test_the_batch_sha_carries_asf_queue_success_before_the_trunk_moves(self):
        batch = self.land_one()
        self.assertEqual(self.heads()['main'], batch['sha'])
        (post,) = _statuses(self.gh.calls)
        self.assertEqual(post[3], f"repos/o/p/statuses/{batch['sha']}")
        self.assertEqual(_field(post, 'state'), 'success')
        self.assertEqual(_field(post, 'context'), 'asf/queue')
        self.assertIn(batch['ref'], _field(post, 'description'))
        self.assertEqual(_field(post, 'target_url'), 'https://x/actions/runs/1')

    def test_the_post_precedes_the_push(self):
        order = []
        real_push = merge_queue.gitpush.push

        def push(args, *a, **k):
            if any(str(x).endswith(':refs/heads/main') for x in args):
                order.append('push')
            return real_push(args, *a, **k)
        inner = self.gh

        def gh(args):
            if args[:3] == ['api', '-X', 'POST'] and '/statuses/' in args[3]:
                order.append('status')
            return inner(args)
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.green(batch['sha'])
        with mock.patch.object(harvest, '_gh', side_effect=gh), \
                mock.patch.object(merge_queue.gitpush, 'push', side_effect=push):
            self.queue_pass(self.lane(), [])
        self.assertEqual(order, ['status', 'push'])
        self.assertEqual(self.heads()['main'], batch['sha'])

    def test_the_context_is_configurable(self):
        p = self.product(ci={'queue_status': 'gate/door'})
        self.land_one(p)
        (post,) = _statuses(self.gh.calls)
        self.assertEqual(_field(post, 'context'), 'gate/door')

    def test_no_status_is_posted_while_the_batch_is_pending(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        self.queue_pass(self.lane(), [])
        self.assertEqual(_statuses(self.gh.calls), [])


class Convention(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(conventions.Conventions.from_mapping({}).queue_status(), 'asf/queue')
        self.assertEqual(conventions.Conventions.from_mapping({'ci': {'queue_status': ' x/y '}}).queue_status(),
                         'x/y')
        self.assertEqual(conventions.Conventions.from_mapping({'ci': {'queue_status': ''}}).queue_status(),
                         'asf/queue')


def _product(merge='queue', ci=None):
    conv = {'merge': merge}
    if ci:
        conv['ci'] = ci
    return env.Product('sample', {'repo_dir': '/nowhere', 'repo_slug': 'o/p', 'main': 'main',
                                  'conventions': conv})


class FakeHost:
    def __init__(self, rules=(), sets=(), rc=0):
        self.rules, self.sets, self.rc, self.calls = list(rules), list(sets), rc, []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.rc:
            return self.rc, '', 'HTTP 500'
        if args[1].endswith('/rules/branches/main'):
            return 0, json.dumps(self.rules), ''
        if args[1].endswith('/rulesets'):
            return 0, json.dumps(self.sets), ''
        return 1, '', 'unexpected'


def _rule(rid, context):
    return {'type': 'required_status_checks', 'ruleset_id': rid,
            'parameters': {'required_status_checks': [{'context': context}]}}


class DoctorRow(unittest.TestCase):
    def test_off_the_queue_no_row_and_no_call(self):
        host = FakeHost()
        self.assertEqual(trunk_ruleset.doctor_rows(_product(merge='auto'), gh=host), [])
        self.assertEqual(host.calls, [])

    def test_green_names_the_ruleset_and_the_break_glass_call(self):
        host = FakeHost(rules=[{'type': 'non_fast_forward', 'ruleset_id': 7}, _rule(7, 'asf/queue')])
        ((req, ok, detail),) = trunk_ruleset.doctor_rows(_product(), gh=host)
        self.assertEqual((req, ok), (True, True))
        self.assertIn('ruleset 7', detail)
        self.assertIn('gh api -X PUT repos/o/p/rulesets/7 -f enforcement=disabled', detail)
        self.assertIn('gh api -X PUT repos/o/p/rulesets/7 -f enforcement=active', detail)
        self.assertEqual(len(host.calls), 1)

    def test_a_ruleset_requiring_another_context_is_red(self):
        host = FakeHost(rules=[_rule(7, 'gate')])
        ((_r, ok, detail),) = trunk_ruleset.doctor_rows(_product(), gh=host)
        self.assertIs(ok, False)
        self.assertIn('no active ruleset requires asf/queue', detail)

    def test_the_configured_context_is_the_one_read(self):
        host = FakeHost(rules=[_rule(7, 'gate/door')])
        ((_r, ok, _d),) = trunk_ruleset.doctor_rows(_product(ci={'queue_status': 'gate/door'}),
                                                   gh=host)
        self.assertIs(ok, True)

    def test_a_disabled_ruleset_is_red_and_named_with_its_enable_call(self):
        host = FakeHost(sets=[{'id': 9, 'name': 'one door', 'target': 'branch',
                               'enforcement': 'disabled'}])
        ((_r, ok, detail),) = trunk_ruleset.doctor_rows(_product(), gh=host)
        self.assertIs(ok, False)
        self.assertIn('ruleset 9 (one door) is disabled', detail)
        self.assertIn('rulesets/9 -f enforcement=active', detail)

    def test_an_unreadable_host_is_unknown(self):
        ((_r, ok, _d),) = trunk_ruleset.doctor_rows(_product(), gh=FakeHost(rc=1))
        self.assertIsNone(ok)

    def test_a_rate_limit_is_unknown_not_red(self):
        def gh(_args):
            raise gh_limit.RateLimited('rate limited')
        ((_r, ok, _d),) = trunk_ruleset.doctor_rows(_product(), gh=gh)
        self.assertIsNone(ok)


if __name__ == '__main__':
    unittest.main()
