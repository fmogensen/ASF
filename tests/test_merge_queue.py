"""``conventions.merge: queue`` — the lane's serialized merge queue (:mod:`asf.merge_queue`).

The invariant under test: nothing reaches the trunk unless that exact sha has green required
checks. Green PRs are merged onto the trunk tip as one batch ref, the product's CI runs once on
that sha, and only that sha is fast-forwarded onto the trunk. A member head that moves, a trunk
that moves past the batch, a red or skipped required check: the batch is dropped, never merged.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import conventions, env, merge_queue, stale_ref
from asf.harvest import harvest, lane
from asf.views import status
from asf.workers import lifecycle

from tests.test_lane import HEAD, LaneFixture, facts, rec, sh

SLUG = 'o/p'
REQUIRED_FILE = 'scripts/safe-merge.sh'


def check_run(name, conclusion='success', status_='completed'):
    return {'name': name, 'status': status_, 'conclusion': conclusion,
            'html_url': f'https://x/actions/runs/1/job/{name}'}


class FakeGH:
    """``gh`` as the queue calls it: check runs per sha, PR states, and every call recorded."""

    def __init__(self):
        self.checks = {}      # sha -> [check_run]
        self.pr_state = {}    # number -> OPEN|MERGED
        self.mergeable = {}   # number -> CONFLICTING|MERGEABLE (default UNKNOWN)
        self.head_oid = {}    # number -> current head sha (when a test reads it)
        self.annotations = {}  # job id -> [annotation]
        self.calls = []
        self.logs, self.steps = {}, {}

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == 'api' and args[1].endswith('/annotations'):
            return 0, json.dumps(self.annotations.get(args[1].split('/')[-2], [])), ''
        if args[0] == 'api' and '/check-runs' in args[1]:
            sha = args[1].split('/commits/')[1].split('/')[0]
            return 0, json.dumps({'check_runs': self.checks.get(sha, [])}), ''
        if args[:2] == ['pr', 'view']:
            n = int(args[2])
            out = {'state': self.pr_state.get(n, 'OPEN'), 'mergeable': self.mergeable.get(n, 'UNKNOWN')}
            if n in self.head_oid:
                out['headRefOid'] = self.head_oid[n]
            return 0, json.dumps(out), ''
        if args[:2] == ['pr', 'close']:
            self.pr_state[int(args[2])] = 'CLOSED'
            return 0, '', ''
        if args[:2] == ['run', 'list']:
            return 0, '[]', ''
        if args[0] == 'api' and '/actions/jobs/' in args[-1]:
            job = args[-1].split('/actions/jobs/')[1].split('/')[0]
            if args[-1].endswith('/logs'):
                return (0, self.logs[job], '') if job in self.logs else (1, '', 'not found')
            if job in self.steps:
                return 0, json.dumps({'steps': [{'name': self.steps[job], 'conclusion': 'failure'}],
                                      'run_id': 7, 'name': 'rules', 'head_sha': ''}), ''
        return 0, '', ''

    logs = {}     # job id -> its log
    steps = {}    # job id -> its failed step


class QueueRepo(LaneFixture):
    """A PR product on a local origin: ``gh`` faked, git real."""

    def setUp(self):
        super().setUp()
        # stale_ref reads a run once a process: a run id one test saw must not answer the next's
        stale_ref._RUNS.clear()
        self.addCleanup(stale_ref._RUNS.clear)
        self.gh = FakeGH()
        patch = mock.patch.object(harvest, '_gh', side_effect=self.gh)
        patch.start()
        self.addCleanup(patch.stop)
        st = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir)
        st.start()
        self.addCleanup(st.stop)
        # the required set's single source: a shell file in the product repo, read at the sha
        self.write(self.repo, REQUIRED_FILE, 'REQUIRED_CHECKS="${REQUIRED_CHECKS:-gate gate-tests}"\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'required checks'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        self.no_sleep = mock.patch.object(merge_queue.time, 'sleep', lambda *_: None)
        self.no_sleep.start()
        self.addCleanup(self.no_sleep.stop)

    def product(self, **conventions_):
        conv = {'landing': 'pull-request', 'landing_checks': ['gate'], 'landing_checks_missing': 'wait',
                'merge': 'queue', 'merge_queue': {'ref_prefix': 'batch/', 'batch_size': 3},
                'specs_dir': 'specs', 'plans_dir': 'plans', 'reviews_dir': 'reviews',
                'lane': {'review': {'code': 'none'}},
                'branch_prefixes': {'code': 'worker/', 'plan': 'plan/', 'spec': 'spec/'}}
        conv.update(conventions_)
        return env.Product('sample', {
            'repo_dir': self.repo, 'repo_slug': SLUG, 'main': 'main', 'conventions': conv,
            'deploy_sha': {'prod': {'required_jobs_from': {'file': REQUIRED_FILE,
                                                           'var': 'REQUIRED_CHECKS'}}}})

    def lane(self, product=None):
        self.lines = []
        ln = lane.Lane(product or self.product(), self.state_dir, out=self.lines.append, items={})
        sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=self.repo)
        return ln

    def entry(self, branch, number, item='T-0001', files=('a.txt',)):
        head = sh(['git', 'rev-parse', branch], cwd=self.origin).stdout.strip()
        return {'branch': branch, 'item': item, 'kind': 'code', 'head': head, 'run': None,
                'prev': {'state': lane.GATE, 'head': head, 'pr': number, 'reason': ''},
                'pr': {'number': number, 'state': 'OPEN'}, 'files': list(files),
                'class': lane.CODE, 'how': 'ci'}

    def heads(self):
        return {name: sha for sha, name in (l.split() for l in sh(
            ['git', 'for-each-ref', '--format=%(objectname) %(refname:short)', 'refs/heads'],
            cwd=self.origin).stdout.strip().splitlines())}

    def batches(self):
        return merge_queue.load(self.state_dir)['batches']

    def green(self, sha, names=('gate', 'gate-tests')):
        self.gh.checks[sha] = [check_run(n) for n in names]

    def queue_pass(self, ln, ready):
        with mock.patch.object(lane, 'send_back', side_effect=self.record_back):
            merge_queue.run(ln, ready)
        return ln

    def record_back(self, ln, f, kind, text, files, **_kw):
        ln.results[f['branch']] = 'back'
        self.backs.append((f['branch'], kind, text, list(files)))

    backs = ()

    def setUpBacks(self):
        self.backs = []


class GreenBatch(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat: b')

    def test_a_cut_batch_queues_its_members_on_one_ref_and_lands_nothing_yet(self):
        ln = self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                    self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        heads = self.heads()
        self.assertIn(batch['ref'], heads, heads)
        self.assertEqual(heads[batch['ref']], batch['sha'])
        self.assertEqual(heads['main'], batch['base'])       # the trunk did not move
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001', 'worker/T-0002'])
        for b in ('worker/T-0001', 'worker/T-0002'):
            r = self.lane_of(b)
            self.assertEqual((r['state'], r['batch'], r['sha']), (lane.QUEUED, batch['ref'], batch['sha']))
        self.assertEqual(ln.results, {'worker/T-0001': 'queued', 'worker/T-0002': 'queued'})
        # each member head is a parent of the batch: GitHub marks the PRs merged on the ff
        for m in batch['members']:
            self.assertTrue(ln.is_ancestor(m['head'], batch['sha']))

    def test_a_green_batch_fast_forwards_the_trunk_to_the_gated_sha(self):
        ln = self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1), self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.assertEqual(self.heads()['main'], batch['base'])
        # pending: nothing moves
        self.gh.checks[batch['sha']] = [check_run('gate'), check_run('gate-tests', None, 'in_progress')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.QUEUED)
        # green on every required check at that sha: the trunk is that sha now
        self.green(batch['sha'])
        self.gh.pr_state = {1: 'MERGED', 2: 'MERGED'}
        ln = self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        for b in ('worker/T-0001', 'worker/T-0002'):
            r = self.lane_of(b)
            self.assertEqual((r['state'], r['sha'], r['method']), (lane.MERGED, batch['sha'], 'queue'))
        self.assertEqual(ln.results, {'worker/T-0001': 'landed', 'worker/T-0002': 'landed'})
        self.assertEqual(self.batches(), [])
        self.assertNotIn(batch['ref'], self.heads())
        self.assertNotIn('worker/T-0001', self.heads())     # the members are deleted
        self.assertEqual([c for c in self.gh.calls if c[:2] == ['pr', 'close']], [])
        with open(os.path.join(self.state_dir, 'gates.jsonl'), encoding='utf-8') as fh:
            ledger = [json.loads(l) for l in fh]
        self.assertEqual([(g['sha'], g['ok']) for g in ledger], [(batch['sha'], True)])

    def test_a_pr_the_host_did_not_mark_merged_is_closed_with_the_landed_sha(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.green(batch['sha'])
        self.queue_pass(self.lane(), [])
        closes = [c for c in self.gh.calls if c[:2] == ['pr', 'close']]
        self.assertEqual(len(closes), 1, self.gh.calls)
        self.assertEqual(closes[0][2], '1')
        self.assertIn(batch['sha'][:12], ' '.join(closes[0]))

    def test_the_required_set_is_read_from_the_product_file_at_the_batch_sha(self):
        """landing_checks names ``gate`` only; the product's own file adds ``gate-tests``. Green on
        ``gate`` alone is not green — the file at the sha, not the yaml, is the set."""
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.QUEUED)
        self.assertTrue(any('gate-tests' in l and 'pending' in l for l in self.lines), self.lines)

    def test_a_member_can_add_a_required_check_but_never_remove_one(self):
        self.push_lane('worker/T-0003', {REQUIRED_FILE: 'REQUIRED_CHECKS="gate"\n'}, 'shrink the set')
        self.queue_pass(self.lane(), [self.entry('worker/T-0003', 3, 'T-0003', files=(REQUIRED_FILE,))])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])        # gate-tests still required
        self.assertEqual(self.lane_of('worker/T-0003')['state'], lane.QUEUED)

    def test_a_member_that_adds_a_required_check_is_held_to_it(self):
        self.push_lane('worker/T-0004', {REQUIRED_FILE: 'REQUIRED_CHECKS="gate gate-tests site"\n'}, 'grow')
        self.queue_pass(self.lane(), [self.entry('worker/T-0004', 4, 'T-0004', files=(REQUIRED_FILE,))])
        (grown,) = self.batches()
        self.green(grown['sha'])                                    # gate and gate-tests only
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], grown['base'])
        self.assertTrue(any('site' in l and 'pending' in l for l in self.lines), self.lines)

    def test_a_skipped_required_check_is_red_never_green(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate'), check_run('gate-tests', 'skipped')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])
        self.assertEqual(len(self.backs), 1, self.lines)
        b, kind, text, _files = self.backs[0]
        self.assertEqual((b, kind), ('worker/T-0001', 'gate'))
        self.assertIn('gate-tests', text)
        self.assertIn('skipped', text)
        self.assertEqual(self.batches(), [])
        self.assertNotIn(batch['ref'], self.heads())

    def test_dry_run_cuts_nothing(self):
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append if hasattr(self, 'lines')
                       else print, dry_run=True, items={})
        self.lines = []
        ln.out = self.lines.append
        merge_queue.run(ln, [self.entry('worker/T-0001', 1)])
        self.assertEqual(self.batches(), [])
        self.assertEqual(set(self.heads()), {'main', 'worker/T-0001', 'worker/T-0002'})
        self.assertTrue(any(l.startswith('DRY') for l in self.lines), self.lines)


class RedBatch(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        for i, n in enumerate('abc', 1):
            self.push_lane(f'worker/T-000{i}', {f'{n}.txt': f'{n}\n'}, f'feat: {n}')

    def entries(self):
        return [self.entry(f'worker/T-000{i}', i, f'T-000{i}') for i in (1, 2, 3)]

    def test_a_red_batch_splits_in_two_halves_each_cut_on_the_trunk(self):
        self.queue_pass(self.lane(), self.entries())
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        ln = self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])
        self.assertNotIn(batch['ref'], self.heads())
        first, second = self.batches()
        self.assertEqual([m['branch'] for m in first['members']], ['worker/T-0001'])
        self.assertEqual([m['branch'] for m in second['members']], ['worker/T-0002', 'worker/T-0003'])
        self.assertEqual((first['base'], second['base']), (batch['base'], batch['base']))
        self.assertIsNone(second['base_ref'])           # not stacked: its run answers for itself
        for b in ('worker/T-0001', 'worker/T-0002', 'worker/T-0003'):
            self.assertEqual(self.lane_of(b)['state'], lane.QUEUED)
        self.assertEqual(self.backs, [])
        self.assertEqual(ln.results['worker/T-0001'], 'queued')
        # the red half goes back, the green half lands; a bisection costs one cycle per level
        self.gh.checks[first['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.green(second['sha'])
        self.gh.pr_state = {2: 'MERGED', 3: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], second['sha'])
        self.assertEqual([b for b, _k, _t, _f in self.backs], ['worker/T-0001'])
        self.assertEqual(self.batches(), [])

    def test_a_red_batch_of_one_goes_back_to_its_session_with_the_red_checks(self):
        self.queue_pass(self.lane(), self.entries()[:1])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.backs), 1)
        b, kind, text, _files = self.backs[0]
        self.assertEqual((b, kind), ('worker/T-0001', 'gate'))
        self.assertIn('gate', text)
        self.assertIn(batch['sha'][:12], text)
        self.assertEqual(self.batches(), [])
        self.assertEqual(self.heads()['main'], batch['base'])

    def stacked_pair(self):
        """Two batches cut one after the other: the second stacked on the first."""
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 2})
        self.queue_pass(self.lane(product), self.entries())
        first, second = self.batches()
        self.assertEqual(second['base'], first['sha'])
        self.assertEqual(second['base_ref'], first['ref'])
        return product, first, second

    def test_a_red_lower_batch_drops_the_batch_stacked_on_it(self):
        product, first, second = self.stacked_pair()
        self.green(second['sha'])       # the stacked one is green, but its base is red
        self.gh.checks[first['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.queue_pass(self.lane(product), [])
        self.assertEqual(self.heads()['main'], first['base'])   # nothing landed
        self.assertNotIn(second['ref'], self.heads())
        self.assertEqual(self.backs, [])                        # two members: split, not back
        halves = self.batches()
        self.assertEqual([[m['branch'] for m in b['members']] for b in halves],
                         [['worker/T-0001'], ['worker/T-0002'], ['worker/T-0003']][:len(halves)])
        self.assertTrue(all(b['base'] == first['base'] for b in halves))
        self.assertEqual(self.lane_of('worker/T-0003')['state'],
                         lane.QUEUED if len(halves) == 3 else lane.WAITING)

    def test_a_green_stacked_batch_lands_after_its_base(self):
        product, first, second = self.stacked_pair()
        self.green(second['sha'])
        self.queue_pass(self.lane(product), [])                 # green above a pending base: waits
        self.assertEqual(self.heads()['main'], first['base'])
        self.assertEqual(len(self.batches()), 2)
        self.green(first['sha'])
        self.gh.pr_state = {1: 'MERGED', 2: 'MERGED', 3: 'MERGED'}
        self.queue_pass(self.lane(product), [])
        self.assertEqual(self.heads()['main'], second['sha'])
        self.assertEqual(self.batches(), [])
        for b in ('worker/T-0001', 'worker/T-0002', 'worker/T-0003'):
            r = self.lane_of(b)
            self.assertEqual(r['state'], lane.MERGED)
        self.assertEqual(self.lane_of('worker/T-0001')['sha'], first['sha'])
        self.assertEqual(self.lane_of('worker/T-0003')['sha'], second['sha'])


def job_run(name, job, conclusion='failure'):
    """A check run with a numeric job id (flake triage and the log reader key on it)."""
    return {'name': name, 'status': 'completed', 'conclusion': conclusion,
            'html_url': f'https://github.com/o/p/actions/runs/7/job/{job}'}


def rules_log(*findings):
    """A failed ``rules`` job's log: the step's header group, its output, the runner's error."""
    body = ['==> gate:fast: lint (0s)', 'Checked 2869 files in 2s. No fixes applied.',
            '==> gate:fast: brand parity (strict) (7s)', 'stale-tokens: 0 finding(s)']
    if findings:
        body.append(f'unaliased-literal: {len(findings)} finding(s)')
        body += [f'  {f} — oklch(' for f in findings]
    body.append(f'brand parity: {len(findings)} finding(s) across its four rules')
    stamp = '2026-10-02T06:37:57.1639137Z '
    lines = ['##[group]Run ./scripts/gate-fast.sh --all', 'shell: /usr/bin/bash -e {0}',
             '##[endgroup]'] + body + ['##[error]Process completed with exit code 1.',
                                       'Post job cleanup.']
    return '\n'.join('\ufeff' * (i == 0) + stamp + l for i, l in enumerate(lines)) + '\n'


class Culprit(QueueRepo):
    """One member's real defect fails the batch: the failing job's log names its files, so it
    goes back alone (job, step, lines) and the innocent members are cut again at once."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'apps/site/day-bars.tsx': 'x\n'}, 'feat: bars')
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat: b')
        self.push_lane('worker/T-0003', {'c.txt': 'c\n'}, 'feat: c')

    def entries(self):
        return [self.entry('worker/T-0001', 1, 'T-0001', files=('apps/site/day-bars.tsx',)),
                self.entry('worker/T-0002', 2, 'T-0002', files=('b.txt',)),
                self.entry('worker/T-0003', 3, 'T-0003', files=('c.txt',))]

    def red_upstream(self, sha, job):
        """``rules`` (not required) failed; the required jobs behind it were skipped."""
        self.gh.checks[sha] = [job_run('rules', job), check_run('gate', 'skipped'),
                               check_run('gate-tests', 'skipped')]
        self.gh.logs[job] = rules_log('apps/site/day-bars.tsx:10', 'apps/site/day-bars.tsx:11')
        self.gh.steps[job] = 'gate:fast (brand parity)'

    def test_a_culprit_found_by_its_files_goes_back_and_the_rest_land(self):
        self.queue_pass(self.lane(), self.entries())
        (batch,) = self.batches()
        self.red_upstream(batch['sha'], '101')
        # red once: flake triage re-runs the failed upstream job first — nobody blamed yet
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertIn(['run', 'rerun', '--job', '101', '-R', SLUG], self.gh.calls)
        # red again on its re-run: a real defect, and the log names T-0001's file
        self.red_upstream(batch['sha'], '102')
        ln = self.queue_pass(self.lane(), [])
        self.assertEqual([(b, k) for b, k, _t, _f in self.backs], [('worker/T-0001', 'gate')])
        _b, _k, text, files = self.backs[0]
        self.assertEqual(files, ['apps/site/day-bars.tsx'])
        for want in ('rules failed', 'gate:fast (brand parity)', 'unaliased-literal: 2 finding(s)',
                     'apps/site/day-bars.tsx:10 — oklch(', 'apps/site/day-bars.tsx:11 — oklch(',
                     batch['sha'][:12]):
            self.assertIn(want, text)
        self.assertNotIn('b.txt', text)
        (again,) = self.batches()
        self.assertNotEqual(again['ref'], batch['ref'])
        self.assertEqual([m['branch'] for m in again['members']], ['worker/T-0002', 'worker/T-0003'])
        self.assertEqual(again['base'], batch['base'])
        for b in ('worker/T-0002', 'worker/T-0003'):
            self.assertEqual((self.lane_of(b)['state'], ln.results[b]), (lane.QUEUED, 'queued'))
        self.assertTrue(any('cut again without it' in l for l in self.lines), self.lines)
        # the innocent members land on their own run
        self.green(again['sha'])
        self.gh.pr_state = {2: 'MERGED', 3: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], again['sha'])
        self.assertEqual(self.lane_of('worker/T-0002')['state'], lane.MERGED)
        self.assertEqual(len(self.backs), 1)

    def test_a_culprit_found_by_bisect_when_the_log_names_no_file(self):
        """The log names no file: the batch splits in halves, each cut on the trunk, until the
        red member stands alone — the innocent halves land on the way."""
        self.queue_pass(self.lane(), self.entries()[1:] + self.entries()[:1])   # T2, T3, T1
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        first, second = self.batches()
        self.assertEqual([m['branch'] for m in first['members']], ['worker/T-0002'])
        self.assertEqual([m['branch'] for m in second['members']], ['worker/T-0003', 'worker/T-0001'])
        self.green(first['sha'])
        self.gh.checks[second['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.gh.pr_state = {2: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], first['sha'])
        self.assertEqual(self.backs, [])
        # the landing made the red half stale: it is cut again on the new tip, and splits there
        (pair,) = self.batches()
        self.assertEqual([m['branch'] for m in pair['members']], ['worker/T-0003', 'worker/T-0001'])
        self.gh.checks[pair['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        (left, right) = self.batches()
        self.assertEqual([[m['branch'] for m in b['members']] for b in (left, right)],
                         [['worker/T-0003'], ['worker/T-0001']])
        self.green(left['sha'])
        self.gh.checks[right['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.gh.pr_state = {2: 'MERGED', 3: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], left['sha'])
        (alone,) = self.batches()                       # stale again: cut alone on the new tip
        self.assertEqual([m['branch'] for m in alone['members']], ['worker/T-0001'])
        self.gh.checks[alone['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual([b for b, _k, _t, _f in self.backs], ['worker/T-0001'])
        self.assertIn('this PR alone', self.backs[0][2])
        self.assertEqual(self.batches(), [])

    def test_a_lone_culprit_is_told_the_failed_upstream_job_not_only_the_skipped_ones(self):
        self.queue_pass(self.lane(), self.entries()[:1])
        (batch,) = self.batches()
        self.red_upstream(batch['sha'], '101')
        self.queue_pass(self.lane(), [])            # re-run first
        self.red_upstream(batch['sha'], '102')
        self.queue_pass(self.lane(), [])
        ((b, kind, text, files),) = self.backs
        self.assertEqual((b, kind, files), ('worker/T-0001', 'gate', ['apps/site/day-bars.tsx']))
        self.assertIn('rules failed', text)
        self.assertIn('apps/site/day-bars.tsx:10 — oklch(', text)
        self.assertIn('this PR alone', text)

    def test_a_flake_blames_no_member(self):
        self.queue_pass(self.lane(), self.entries())
        (batch,) = self.batches()
        self.red_upstream(batch['sha'], '101')
        self.queue_pass(self.lane(), [])
        # the re-run went green, and the jobs behind it ran green
        self.gh.checks[batch['sha']] = [job_run('rules', '102', 'success'), check_run('gate'),
                                        check_run('gate-tests')]
        self.gh.pr_state = {1: 'MERGED', 2: 'MERGED', 3: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        for b in ('worker/T-0001', 'worker/T-0002', 'worker/T-0003'):
            self.assertEqual(self.lane_of(b)['state'], lane.MERGED)

    def test_red_on_the_trunk_too_blames_no_member(self):
        self.queue_pass(self.lane(), self.entries())
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('rules', 'failure'), check_run('gate', 'skipped'),
                                        check_run('gate-tests', 'skipped')]
        with mock.patch.object(lane.GitHubHost, 'trunk_red',
                               lambda _self, names: {'rules': 'f' * 40} if 'rules' in names else {}):
            self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        self.assertEqual(self.batches(), [])            # dropped, never split
        self.assertNotIn(batch['ref'], self.heads())
        for b in ('worker/T-0001', 'worker/T-0002', 'worker/T-0003'):
            r = self.lane_of(b)
            self.assertEqual(r['state'], lane.WAITING)
            self.assertIn('red on main too', r['reason'])
        self.assertEqual(self.heads()['main'], batch['base'])

    def test_every_member_named_is_no_verdict_the_batch_splits(self):
        self.queue_pass(self.lane(), self.entries()[:2])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [job_run('rules', '101'), check_run('gate', 'skipped'),
                                        check_run('gate-tests', 'skipped')]
        self.gh.logs['101'] = rules_log('apps/site/day-bars.tsx:10', 'b.txt:1')
        with mock.patch.object(merge_queue.flake, 'triage', lambda *a, **k: (['rules'], [])):
            self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        self.assertEqual([[m['branch'] for m in b['members']] for b in self.batches()],
                         [['worker/T-0001'], ['worker/T-0002']])


class EscapeSequences(unittest.TestCase):
    """``gh api`` 2.101 refuses a coloured job log unless asked: the read retries with the flag."""

    def test_a_refused_log_is_read_again_with_the_flag(self):
        seen = []

        def run(cmd, **_kw):
            seen.append(cmd)
            if harvest.GH_ESCAPES in cmd:
                return subprocess.CompletedProcess(cmd, 0, 'the log\x1b[0m', '')
            return subprocess.CompletedProcess(cmd, 1, '', 'the response contains terminal escape '
                                               'sequences; pass --allow-escape-sequences to output it')
        with mock.patch.object(harvest.subprocess, 'run', side_effect=run):
            rc, out, _err = harvest._gh(['api', 'repos/o/p/actions/jobs/1/logs'])
        self.assertEqual((rc, out), (0, 'the log\x1b[0m'))
        self.assertEqual(seen[1], ['gh', 'api', harvest.GH_ESCAPES, 'repos/o/p/actions/jobs/1/logs'])

    def test_any_other_failure_is_not_retried(self):
        with mock.patch.object(harvest.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['gh'], 1, '', 'HTTP 404')) as run:
            self.assertEqual(harvest._gh(['api', 'repos/o/p/x'])[0], 1)
        self.assertEqual(run.call_count, 1)


class MovingParts(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat: b')

    def test_a_member_head_that_moves_aborts_the_batch(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1), self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.push_lane('worker/T-0001', {'a.txt': 'a2\n'}, 'feat: a again')
        self.green(batch['sha'])
        ln = self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])
        self.assertNotIn(batch['ref'], self.heads())
        moved = self.lane_of('worker/T-0001')
        self.assertEqual(moved['state'], lane.WAITING)
        self.assertIn('head moved', moved['reason'])
        # the other member is cut again on its own
        (recut,) = self.batches()
        self.assertEqual([m['branch'] for m in recut['members']], ['worker/T-0002'])
        self.assertEqual(self.lane_of('worker/T-0002')['batch'], recut['ref'])
        self.assertEqual(ln.results['worker/T-0002'], 'queued')

    def test_a_trunk_that_moves_forces_a_re_cut_never_a_merge_of_the_ungated_sha(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1), self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.push_main({'hot.txt': 'fix\n'}, 'hotfix')
        trunk = self.heads()['main']
        self.green(batch['sha'])                       # green, but on a sha behind the trunk
        self.queue_pass(self.lane(), [])
        heads = self.heads()
        self.assertEqual(heads['main'], trunk)         # never moved to the old batch
        self.assertNotIn(batch['ref'], heads)
        (recut,) = self.batches()
        self.assertEqual(recut['base'], trunk)
        self.assertNotEqual(recut['sha'], batch['sha'])
        self.assertEqual([m['branch'] for m in recut['members']], ['worker/T-0001', 'worker/T-0002'])
        # the new batch, green at its own sha, lands — and only then
        self.green(recut['sha'])
        self.gh.pr_state = {1: 'MERGED', 2: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], recut['sha'])

    def test_a_batch_whose_ref_was_moved_on_origin_is_dropped(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', 'tmp', f"origin/{batch['ref']}"], cwd=self.worker)
        self.write(self.worker, 'evil.txt', 'x\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'ride along'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', f"tmp:{batch['ref']}"], cwd=self.worker)
        self.green(batch['sha'])
        self.green(self.heads()[batch['ref']])
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['base'])
        self.assertTrue(any('moved on origin' in l for l in self.lines), self.lines)

    def test_two_conflicting_members_never_share_a_batch_the_later_waits(self):
        self.push_lane('worker/T-0003', {'a.txt': 'other a\n'}, 'feat: a too')
        ln = self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                    self.entry('worker/T-0003', 3, 'T-0003', files=('a.txt',)),
                                    self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001', 'worker/T-0002'])
        self.assertEqual(self.backs, [])               # not failed, not sent back
        waiting = self.lane_of('worker/T-0003')
        self.assertEqual(waiting['state'], lane.WAITING)
        self.assertIn('waits on #1 (conflicting files: a.txt)', waiting['reason'])
        self.assertTrue(any('waits on #1' in l for l in self.lines), self.lines)

    def test_a_conflict_found_only_by_the_merge_still_defers_the_later_member(self):
        self.push_lane('worker/T-0003', {'a.txt': 'other a\n'}, 'feat: a too')
        # the entry's file list does not name a.txt: git's own conflict names the partner
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1, files=('z.txt',)),
                                      self.entry('worker/T-0003', 3, 'T-0003', files=('y.txt',))])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001'])
        self.assertEqual(self.backs, [])
        self.assertIn('waits on #1', self.lane_of('worker/T-0003')['reason'])

    def test_a_member_conflicting_with_a_batch_ahead_waits_on_it_then_is_cut_after(self):
        self.push_lane('worker/T-0003', {'a.txt': 'other a\n'}, 'feat: a too')
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (first,) = self.batches()
        self.queue_pass(self.lane(), [self.entry('worker/T-0003', 3, 'T-0003', files=('a.txt',))])
        self.assertEqual(self.backs, [])
        self.assertEqual(len(self.batches()), 1)
        self.assertIn(f"waits on #1 (conflicting files: a.txt) in batch {first['ref']}",
                      self.lane_of('worker/T-0003')['reason'])

    def test_a_conflict_with_the_trunk_behind_a_batch_goes_to_the_lane_rebuild(self):
        # the trunk conflict is told apart from a batch ahead: it is the lane's rebuild's (rebase on)
        self.push_lane('worker/T-0003', {'a.txt': 'other a\n'}, 'feat: a too')
        self.push_main({'a.txt': 'trunk a\n'}, 'trunk a')
        self.queue_pass(self.lane(), [self.entry('worker/T-0002', 2, 'T-0002', files=('b.txt',))])
        (first,) = self.batches()
        sent = []
        with mock.patch.object(lane, 'send_back', side_effect=lambda ln, f, k, t, fl, **kw: (
                sent.append((k, t, list(fl), kw)), ln.results.__setitem__(f['branch'], 'back'))):
            merge_queue.run(self.lane(), [self.entry('worker/T-0003', 3, 'T-0003', files=('q.txt',))])
        (kind, text, files, kw), = sent
        self.assertEqual((kind, files), ('conflict', ['a.txt']))
        self.assertTrue(kw.get('rebase', True), kw)
        self.assertNotIn('a batch ahead', text)
        self.assertEqual(len(self.batches()), 1)

    def test_a_conflict_with_the_trunk_alone_still_goes_back(self):
        self.push_lane('worker/T-0003', {'a.txt': 'other a\n'}, 'feat: a too')
        self.push_main({'a.txt': 'trunk a\n'}, 'trunk a')
        self.queue_pass(self.lane(), [self.entry('worker/T-0003', 3, 'T-0003', files=('a.txt',))])
        self.assertEqual([(b, k) for b, k, _t, _f in self.backs], [('worker/T-0003', 'conflict')])
        self.assertIn('git rebase origin/main', self.backs[0][2])

    def test_the_queue_is_bounded_and_the_rest_wait_with_their_green_kept(self):
        for i in range(3, 8):
            self.push_lane(f'worker/T-000{i}', {f'{i}.txt': 'x\n'}, f'feat: {i}')
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 2, 'inflight': 2})
        ready = [self.entry(f'worker/T-000{i}', i, f'T-000{i}') for i in range(1, 8)]
        for f in ready:
            f['green'] = {'head': f['head'], 'trunk': 'x' * 40}
        ln = self.queue_pass(self.lane(product), ready)
        self.assertEqual(len(self.batches()), 2)
        first, second = self.batches()
        self.assertEqual(second['base'], first['sha'])
        self.assertEqual([r for r in ln.results.values() if r == 'queued'].count('queued'), 4)
        waiting = self.lane_of('worker/T-0005')
        self.assertEqual(waiting['state'], lane.WAITING)
        self.assertIn('merge queue', waiting['reason'])
        self.assertEqual(waiting['green']['head'], ready[4]['head'])


class NotOptedIn(QueueRepo):
    """``merge: auto`` and ``manual`` are untouched: the direct ``gh pr merge`` path, no batch."""

    def test_gate_set_merges_directly_under_auto(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        ln = self.lane(self.product(merge='auto'))
        merges = []
        ln.host.merge = lambda b, n, subject=None: (merges.append((b, n)) or ('c' * 40, 'squash'))
        ln.host.recheck = lambda f, n: None
        ln.host.cancel_ci = lambda b: 0
        with mock.patch.object(merge_queue, 'run') as q:
            lane.gate_set(ln, [self.entry('worker/T-0001', 1)])
        q.assert_not_called()
        self.assertEqual(merges, [('worker/T-0001', 1)])
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, merge_queue.QUEUE_FILE)))

    def test_gate_set_hands_the_green_to_the_queue_under_queue(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        ln = self.lane()
        ln.host.merge = lambda *a, **k: self.fail('a queue product never calls gh pr merge')
        ln.host.recheck = lambda f, n: None
        with mock.patch.object(merge_queue, 'run') as q:
            lane.gate_set(ln, [self.entry('worker/T-0001', 1)])
        q.assert_called_once()
        self.assertEqual([f['branch'] for f in q.call_args[0][1]], ['worker/T-0001'])

    def test_gate_pass_finishes_batches_when_nothing_new_is_ready(self):
        ln = self.lane()
        with mock.patch.object(merge_queue, 'run') as q:
            lane.gate_pass(ln.product, self.state_dir, items={}, lane=ln, found={})
        q.assert_called_once_with(ln, [])
        ln = self.lane(self.product(merge='auto'))
        with mock.patch.object(merge_queue, 'run') as q:
            lane.gate_pass(ln.product, self.state_dir, items={}, lane=ln, found={})
        q.assert_not_called()


class Transitions(unittest.TestCase):
    def test_queued_by_our_own_queue_is_the_queue_modules_to_move(self):
        ours = rec(lane.QUEUED, reason='batch/x', batch='batch/x', sha='c' * 40)
        open_pr = facts(mode='pr', pr={'number': 7, 'state': 'OPEN', 'queued': False})
        self.assertEqual(lane.next_state(ours, open_pr), (lane.QUEUED, 'batch/x'))
        # the host's own queue: rejected still waits (R5)
        theirs = rec(lane.QUEUED, reason='q')
        self.assertEqual(lane.next_state(theirs, open_pr), (lane.WAITING, 'queue-rejected'))

    def test_a_member_the_host_marked_merged_is_merged_by_the_queue(self):
        ours = rec(lane.QUEUED, batch='batch/x')
        merged = facts(mode='pr', pr={'number': 7, 'state': 'MERGED', 'head': HEAD})
        self.assertEqual(lane.next_state(ours, merged), (lane.MERGED, 'method=queue'))

    def test_a_moved_head_leaves_the_queue(self):
        ours = rec(lane.QUEUED, batch='batch/x')
        moved = facts(mode='pr', head='b' * 40, pr={'number': 7, 'state': 'OPEN'})
        self.assertEqual(lane.next_state(ours, moved)[0], lane.PUSHED)


class Convention(unittest.TestCase):
    def test_queue_is_a_merge_mode_that_merges_itself(self):
        c = conventions.Conventions.from_mapping({'merge': 'queue'})
        self.assertTrue(c.merge_auto())
        self.assertTrue(c.merge_queue())
        self.assertEqual(c.shape_findings(), [])
        self.assertFalse(conventions.Conventions.from_mapping({'merge': 'auto'}).merge_queue())
        self.assertFalse(conventions.Conventions.from_mapping({}).merge_queue())
        self.assertFalse(conventions.Conventions.from_mapping({}).merge_auto())

    def test_the_status_cell_names_it(self):
        p = env.Product('p', {'conventions': {'merge': 'queue'}})
        self.assertEqual(status.merge_cell(p), 'queue')
        self.assertEqual(status.merge_cell(env.Product('p', {'conventions': {'merge': 'auto'}})), 'auto')

    def test_settings_have_defaults_and_a_scalar_is_misshapen(self):
        c = conventions.Conventions.from_mapping({'merge_queue': {'batch_size': 5}})
        s = merge_queue.settings(c)
        self.assertEqual((s['batch_size'], s['inflight'], s['ref_prefix']),
                         (5, merge_queue.DEFAULTS['inflight'], merge_queue.DEFAULTS['ref_prefix']))
        bad = conventions.Conventions.from_mapping({'merge_queue': 'yes'})
        self.assertEqual([k for k, _ in bad.shape_findings()], ['merge_queue'])
        self.assertEqual(merge_queue.settings(bad), merge_queue.DEFAULTS)


class StartKind(QueueRepo):
    """The batch push is admitted to the CI start queue as a trunk start unless the product
    says ``batch``: it is the trunk's next sha, and runs where the trunk runs."""

    def admitted_as(self, **mq):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        product = self.product(merge_queue=dict({'ref_prefix': 'batch/'}, **mq))
        calls = []

        def admit(product_, key, kind, **kw):
            calls.append((key, kind))
            return merge_queue.ci_queue.Decision(True, '')
        with mock.patch.object(merge_queue.ci_queue, 'admit', side_effect=admit):
            self.queue_pass(self.lane(product), [self.entry('worker/T-0001', 1)])
        return calls

    def test_trunk_by_default(self):
        self.assertEqual(self.admitted_as(), [('trunk:worker/T-0001', 'trunk')])

    def test_batch_when_the_product_says_so(self):
        self.assertEqual(self.admitted_as(start_kind='batch'), [('batch:worker/T-0001', 'batch')])
        self.assertEqual(merge_queue.settings(conventions.Conventions.from_mapping(
            {'merge_queue': {'start_kind': 'nonsense'}}))['start_kind'], 'trunk')


class Verdict(unittest.TestCase):
    def test_every_required_check_must_conclude_success(self):
        req = ('gate', 'gate-tests')
        self.assertEqual(merge_queue.verdict([check_run('gate'), check_run('gate-tests')], req),
                         ('green', ''))
        state, why = merge_queue.verdict([check_run('gate')], req)
        self.assertEqual(state, 'pending')
        self.assertIn('gate-tests', why)
        state, why = merge_queue.verdict([check_run('gate'), check_run('gate-tests', None, 'queued')], req)
        self.assertEqual(state, 'pending')
        for bad in ('skipped', 'failure', 'timed_out', 'neutral', 'action_required'):
            state, why = merge_queue.verdict([check_run('gate'), check_run('gate-tests', bad)], req)
            self.assertEqual(state, 'red', bad)
            self.assertIn(bad, why)
        # a cancelled run judged no code: never red
        state, why = merge_queue.verdict([check_run('gate'), check_run('gate-tests', 'cancelled')],
                                         req)
        self.assertEqual(state, 'pending')
        self.assertIn('cancelled', why)
        # a matrix leg names the job first; a red one the product does not require is noise
        self.assertEqual(merge_queue.verdict([check_run('gate (ubuntu)'), check_run('gate-tests'),
                                              check_run('m2-e2e', 'failure')], req)[0], 'green')
        self.assertEqual(merge_queue.verdict([], ())[0], 'pending')


class DirectConflictAsksGit(unittest.TestCase):
    """The direct path (``merge: auto``): a refusal whose text hides the conflict and whose host
    reads ``mergeable: UNKNOWN`` (GitHub computes it lazily after the trunk moves) asks git — a
    branch that does not merge onto the trunk goes BACK, never WAITING → merge → refused again."""

    AUTO = 'To have the pull request merged after all the requirements have been met, add the `--auto` flag.'

    def _merge(self, how, files, conflicting=False):
        runner = lane.Lane.__new__(lane.Lane)
        runner.out, runner.dry_run, runner.results, runner.repo = (lambda *_: None), False, {}, None
        runner.trunk = 'main'
        fake = mock.Mock(in_queue=0, merged=0)
        fake.slots.return_value = (None, '')
        fake.recheck.return_value = None
        fake.merge.return_value = (None, how)
        fake.conflicting.return_value = conflicting
        runner.host = fake
        f = {'branch': 'worker/x', 'class': lane.CODE, 'kind': 'code', 'item': 'T-0001',
             'head': HEAD, 'prev': rec(lane.GATE, pr=842)}
        with mock.patch.object(lane.Lane, 'ci_admits', return_value=True), \
                mock.patch.object(lane.Lane, 'set'), \
                mock.patch.object(lane, 'conflict_files', return_value=files), \
                mock.patch.object(lane, 'send_back') as sb, \
                mock.patch.object(lane, 'wait') as wt:
            lane.merge_prs(runner, [f])
        return sb, wt

    def test_git_names_the_conflict_the_text_and_the_host_hid(self):
        sb, wt = self._merge(self.AUTO, ['a.txt'])
        wt.assert_not_called()
        self.assertEqual(sb.call_args[0][2], 'conflict')
        self.assertEqual(list(sb.call_args[0][4]), ['a.txt'])

    def test_a_refusal_git_reads_as_clean_still_waits(self):
        sb, wt = self._merge(self.AUTO, [])
        sb.assert_not_called()
        wt.assert_called_once()


if __name__ == '__main__':
    unittest.main()


class RegateHolds(QueueRepo):
    """A landing-gate hold the flake triage never judged is cleared and the branch gated again."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('spec/F-1', {'specs/f1.md': 'x\n'}, 'spec: f1')
        self.head = self.heads()['spec/F-1']
        self.batch_sha = 'a4e3330dac07d62db5b036cde63a0ff0e8e66cf1'
        self.session_job('adjudicate-f-1')

    def session_job(self, job):
        self.session(job, 'F-1', 'spec/F-1', kind='spec')
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))['spec/F-1']
        fields, _line = lifecycle.hold(
            os.path.join(self.state_dir, 'sessions.jsonl'), dict(run, branch='spec/F-1', job=job),
            lane.LANDING_GATE,
            f'the spec turns the gate red on main - batch b @ {self.batch_sha[:12]} checks red: '
            f'gate (failure)', '2026-10-02T07:22:59Z', head=self.head,
            finding=['gate (failure)'])
        harvest.mark_session(self.state_dir, job, **fields)
        f = {'branch': 'spec/F-1', 'run': run, 'prev': {}, 'head': self.head, 'item': 'F-1'}
        ln = self.lane()
        ln.set(f, lane.BACK, 'kind=landing-gate', head=self.head, pr=7)

    def pass_(self):
        return self.queue_pass(self.lane(), [])

    def test_a_hold_on_a_job_nobody_re_ran_is_gated_again(self):
        self.gh.checks[self.batch_sha[:12]] = [{'name': 'gate', 'status': 'completed',
                                               'conclusion': 'failure', 'id': 5}]
        self.pass_()
        self.assertEqual(self.lane_of('spec/F-1')['state'], lane.PUSHED)
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))['spec/F-1']
        self.assertFalse((run.get('correction') or {}).get('text'))

    def test_a_hold_the_triage_re_ran_stands(self):
        from asf import flake
        data = flake.load(self.state_dir)
        data['reruns'][f'{self.batch_sha}|gate'] = {'name': 'gate', 'sha': self.batch_sha,
                                                    'attempts': 1, 'at': flake._iso(flake._now())}
        flake.save(self.state_dir, data)
        self.pass_()
        self.assertEqual(self.lane_of('spec/F-1')['state'], lane.BACK)


class StaleBatchAheadConflict(RegateHolds):
    def session_job(self, job):
        self.session(job, 'F-1', 'spec/F-1', kind='coder')
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))['spec/F-1']
        fields, _l = lifecycle.hold(
            os.path.join(self.state_dir, 'sessions.jsonl'), dict(run, branch='spec/F-1', job=job),
            'conflict', 'PR #7 does not merge onto b (a batch ahead of it) in the merge queue',
            '2026-10-02T07:22:59Z', head=self.head)
        harvest.mark_session(self.state_dir, job, **fields)
        ln = self.lane()
        ln.set({'branch': 'spec/F-1', 'run': run, 'prev': {}, 'head': self.head, 'item': 'F-1'},
               lane.BACK, 'kind=conflict', head=self.head, pr=7)

    def test_a_clean_branch_is_left_alone_and_a_trunk_conflict_goes_to_the_rebuild(self):
        sent = []
        with mock.patch.object(lane, 'send_back', side_effect=lambda *a, **k: sent.append(a)):
            merge_queue.run(self.lane(), [])
        self.assertEqual(sent, [])                      # merges onto the trunk: nothing owed
        self.push_main({'specs/f1.md': 'trunk\n'}, 'trunk f1')
        with mock.patch.object(lane, 'send_back', side_effect=lambda *a, **k: sent.append(a)):
            merge_queue.run(self.lane(), [])
        (a,) = sent
        self.assertEqual((a[2], a[4]), ('conflict', ['specs/f1.md']))

    test_a_hold_on_a_job_nobody_re_ran_is_gated_again = None
    test_a_hold_the_triage_re_ran_stands = None
