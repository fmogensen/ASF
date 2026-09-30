"""One GraphQL query per tick reads every open PR's head checks (:mod:`asf.harvest.pr_graph`), and
what it derives is what the two REST reads (``gh pr checks`` + ``commits/<sha>/check-runs``) derive.

* equivalence on fixtures: a CI-queue dispatched run, a heavy job skipped before the approval and
  run after it, a cancelled run superseded by a newer one;
* one ``api graphql`` call however many PRs ask; no per-PR ``pr checks`` / ``check-runs`` call;
* a PR the snapshot cannot vouch for (other head, cut-off page, not listed) or a failed query is
  read the REST way; a rate limit propagates.
"""
import json
import unittest
from unittest import mock

from asf import gh_limit
from asf.harvest import lane, pr_graph

HEAD = 'a' * 40
PULL = 'https://github.com/o/p/actions/runs/100/job/{}'
DISPATCH = 'https://github.com/o/p/actions/runs/200/job/{}'


def rollup_row(name, bucket, job, at, wf='ci', link=PULL):
    return {'name': name, 'bucket': bucket, 'link': link.format(job), 'workflow': wf,
            'startedAt': at}


def rest_run(name, conclusion, job, at, link=DISPATCH, status='completed'):
    return {'name': name, 'status': status, 'conclusion': conclusion,
            'html_url': link.format(job), 'started_at': at}


def gql_run(name, conclusion, job, at, link=DISPATCH, status='COMPLETED'):
    return {'name': name, 'status': status, 'conclusion': conclusion and conclusion.upper(),
            'startedAt': at, 'detailsUrl': link.format(job)}


def gql_pr(number, suites, head=HEAD, more=False, labels=('heavy',)):
    return {'number': number, 'headRefOid': head, 'mergeable': 'MERGEABLE',
            'labels': {'nodes': [{'name': n} for n in labels]},
            'commits': {'nodes': [{'commit': {
                'oid': head, 'status': {'contexts': []},
                'checkSuites': {'pageInfo': {'hasNextPage': more}, 'nodes': [
                    {'workflowRun': {'workflow': {'name': wf}},
                     'checkRuns': {'pageInfo': {'hasNextPage': False}, 'nodes': runs}}
                    for wf, runs in suites]}}}]}}


def gql_reply(*prs):
    return json.dumps({'data': {'repository': {'pullRequests': {
        'pageInfo': {'hasNextPage': False}, 'nodes': list(prs)}}}})


# scenario: a pull_request run cancelled by the CI queue, then the dispatched run of the same
# workflow that replaced it (gate green, gate-tests red); heavy p1-e2e skipped before the
# approval (07:00), run after it (07:20) — and m5-soak only ever skipped before it
REST_ROLLUP = [
    rollup_row('gate', 'cancel', 1, '2026-09-30T07:03:25Z'),
    rollup_row('gate-tests', 'cancel', 2, '2026-09-30T06:53:46Z'),
    rollup_row('p1-e2e', 'skipping', 3, '2026-09-30T07:00:00Z'),
    rollup_row('m5-soak', 'skipping', 4, '2026-09-30T07:00:01Z'),
]
REST_RUNS = {'check_runs': [
    rest_run('gate', 'cancelled', 1, '2026-09-30T07:03:25Z', link=PULL),
    rest_run('gate-tests', 'cancelled', 2, '2026-09-30T06:53:46Z', link=PULL),
    rest_run('p1-e2e', 'skipped', 3, '2026-09-30T07:00:00Z', link=PULL),
    rest_run('m5-soak', 'skipped', 4, '2026-09-30T07:00:01Z', link=PULL),
    rest_run('gate', 'success', 11, '2026-09-30T07:29:56Z'),
    rest_run('gate-tests', 'failure', 12, '2026-09-30T07:30:27Z'),
    rest_run('p1-e2e', 'success', 13, '2026-09-30T07:20:25Z'),
]}
GQL_SUITES = [
    ('ci', [gql_run('gate', 'cancelled', 1, '2026-09-30T07:03:25Z', link=PULL),
            gql_run('gate-tests', 'cancelled', 2, '2026-09-30T06:53:46Z', link=PULL),
            gql_run('p1-e2e', 'skipped', 3, '2026-09-30T07:00:00Z', link=PULL),
            gql_run('m5-soak', 'skipped', 4, '2026-09-30T07:00:01Z', link=PULL)]),
    ('ci', [gql_run('gate', 'success', 11, '2026-09-30T07:29:56Z'),
            gql_run('gate-tests', 'failure', 12, '2026-09-30T07:30:27Z'),
            gql_run('p1-e2e', 'success', 13, '2026-09-30T07:20:25Z')]),
]


def fake_gh(graphql=None, rollup=REST_ROLLUP, runs=REST_RUNS, calls=None, fail_graphql=False):
    def gh(args):
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ['api', 'graphql']:
            return (1, '', 'boom') if fail_graphql else (0, graphql, '')
        if args[:2] == ['pr', 'checks']:
            return 0, json.dumps(rollup), ''
        if args[0] == 'api' and '/check-runs' in args[1]:
            return 0, json.dumps(runs), ''
        return 1, '', 'unexpected'
    return gh


def key(checks):
    return sorted((c['name'], c['bucket'], c['workflow'], c['link'], c['startedAt'])
                  for c in checks)


class PrGraph(unittest.TestCase):
    def setUp(self):
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)

    def rest(self, required=(), **kw):
        with mock.patch.object(lane.H, '_gh', fake_gh(fail_graphql=True, **kw)):
            gh_limit.reset()
            return lane.pr_checks('o/p', 7, required, head=HEAD)

    def graph(self, pr, required=(), calls=None):
        with mock.patch.object(lane.H, '_gh', fake_gh(gql_reply(pr), calls=calls)):
            return lane.pr_checks('o/p', pr['number'], required, head=HEAD)

    def test_equivalent_on_dispatched_skipped_and_superseded(self):
        for required in ((), ('gate', 'gate-tests'), ('gate', 'p1-e2e', 'm5-soak')):
            with self.subTest(required=required):
                want = self.rest(required)
                got = self.graph(gql_pr(7, GQL_SUITES), required)
                self.assertEqual(want[:2], got[:2])
                self.assertEqual(key(want[2]), key(got[2]))
                self.gh_reset()

    def gh_reset(self):
        gh_limit.reset()

    def test_cancelled_run_is_superseded_by_the_newer_dispatched_one(self):
        state, detail, checks = self.graph(gql_pr(7, GQL_SUITES), ('gate',))
        self.assertEqual(state, 'green')
        gate = [c for c in checks if c['name'] == 'gate']
        self.assertEqual([c['bucket'] for c in gate], ['pass'])
        self.assertEqual(self.graph(gql_pr(7, GQL_SUITES), ('gate-tests',))[0], 'red')

    def test_heavy_skipped_before_approval_and_after(self):
        # before the approval the heavy job only ever skipped; after it, the newer run passed
        only_skipped = [GQL_SUITES[0]]
        rest = self.rest(('p1-e2e',), rollup=REST_ROLLUP,
                         runs={'check_runs': REST_RUNS['check_runs'][:4]})
        gh_limit.reset()
        got = self.graph(gql_pr(7, only_skipped), ('p1-e2e',))
        self.assertEqual(key(rest[2]), key(got[2]))
        self.assertEqual([c['bucket'] for c in got[2] if c['name'] == 'p1-e2e'], ['skipping'])
        gh_limit.reset()
        after = self.graph(gql_pr(7, GQL_SUITES), ('p1-e2e',))
        self.assertEqual([c['bucket'] for c in after[2] if c['name'] == 'p1-e2e'], ['pass'])

    def test_one_graphql_call_serves_every_open_pr(self):
        calls = []
        reply = gql_reply(gql_pr(7, GQL_SUITES), gql_pr(8, GQL_SUITES),
                          gql_pr(9, GQL_SUITES))
        with mock.patch.object(lane.H, '_gh', fake_gh(reply, calls=calls)):
            for n in (7, 8, 9, 7):
                lane.pr_checks('o/p', n, ('gate',), head=HEAD)
        self.assertEqual([c[:2] for c in calls], [['api', 'graphql']])

    def test_snapshot_carries_head_labels_and_mergeable(self):
        with mock.patch.object(lane.H, '_gh', fake_gh(gql_reply(gql_pr(7, GQL_SUITES)))):
            snap = pr_graph.snapshot('o/p')
        self.assertEqual((snap[7]['head'], snap[7]['labels'], snap[7]['mergeable']),
                         (HEAD, ['heavy'], 'MERGEABLE'))

    def test_fallback_to_rest_when_graphql_fails(self):
        calls = []
        with mock.patch.object(lane.H, '_gh', fake_gh(calls=calls, fail_graphql=True)):
            state, _d, checks = lane.pr_checks('o/p', 7, ('gate',), head=HEAD)
        self.assertEqual(state, 'green')
        self.assertIn(['pr', 'checks'], [c[:2] for c in calls])

    def test_other_head_cut_off_page_or_unlisted_pr_read_by_rest(self):
        for pr, n in ((gql_pr(7, GQL_SUITES, head='c' * 40), 7),
                      (gql_pr(7, GQL_SUITES, more=True), 7), (gql_pr(9, GQL_SUITES), 7)):
            gh_limit.reset()
            calls = []
            with mock.patch.object(lane.H, '_gh', fake_gh(gql_reply(pr), calls=calls)):
                lane.pr_checks('o/p', n, ('gate',), head=HEAD)
            self.assertIn(['pr', 'checks'], [c[:2] for c in calls])

    def test_rate_limit_propagates_and_is_not_a_fallback(self):
        calls = []

        def gh(args):
            calls.append(args[:2])
            gh_limit.trip('API rate limit exceeded', args)

        with mock.patch.object(lane.H, '_gh', gh):
            with self.assertRaises(gh_limit.RateLimited):
                lane.pr_checks('o/p', 7, ('gate',), head=HEAD)
        self.assertEqual(calls, [['api', 'graphql']])

    def test_status_contexts_are_rows(self):
        pr = gql_pr(7, GQL_SUITES)
        pr['commits']['nodes'][0]['commit']['status']['contexts'] = [
            {'context': 'dco', 'state': 'FAILURE', 'targetUrl': 'https://x/y',
             'createdAt': '2026-09-30T07:00:00Z'}]
        _s, _d, checks = self.graph(pr, ())
        self.assertEqual([(c['name'], c['bucket']) for c in checks if c['name'] == 'dco'],
                         [('dco', 'fail')])


if __name__ == '__main__':
    unittest.main()
