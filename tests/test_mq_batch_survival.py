"""A batch in flight survives everything that is not a verdict on its own code
(:mod:`asf.merge_queue`).

Six ways a live batch was lost or kept expensive (2026-10-05, overnight):

1. a ``--priority`` land request must queue behind (or stack on) the batches in CI — never
   destroy them;
2. a batch ref the queue drops or replaces has its live CI run cancelled in the same step — a run
   judging a sha nobody will land holds heavy runners for nothing;
3. a member already on the trunk (landed by an earlier batch) whose branch then leaves origin is
   pruned from the batch, never a reason to drop it;
4. a required job lost with its runner (``The operation was canceled.``, an OOM kill) is no
   verdict: its failed jobs are re-run once on the batch sha, then the batch is cut again —
   never red, never held for hours;
5. only the required checks judge: a red job the product does not require never drops a batch,
   and the queue never re-runs one;
6. a workflow run the host cut short (a cancel, a timeout) is no verdict on the batch, whatever
   its skipped required jobs say — the run is re-run whole once, then the batch is cut again, and
   a contention cancel the CI start queue already made is its re-run to own.
"""
import copy
import datetime
import json
import os
import time
import unittest
from unittest import mock

from asf import ci_queue, flake, merge_queue
from asf.harvest import lane

from tests.test_lane import sh
from tests.test_merge_queue import SLUG, NoVerdictGH, QueueRepo, run_check, stamp
from asf import github
from tests import contracts

CANCELED = [{'message': 'The operation was canceled.', 'annotation_level': 'failure'}]


def _next_second():
    """Let the clock pass the current second: a re-cut within the cut's second makes the same
    merge commits (git's one-second dates) — the same sha and ref."""
    s = int(time.time())
    while int(time.time()) == s:
        pass


class Survival(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.gh = NoVerdictGH()
        patch = mock.patch.object(github, 'call', side_effect=contracts.as_call(self.gh))
        patch.start()
        self.addCleanup(patch.stop)
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat: b')
        self.push_lane('worker/T-0003', {'c.txt': 'c\n'}, 'feat: c')

    def cancels(self):
        return [c for c in self.gh.calls if c[:2] == ['run', 'cancel']]

    def dropped_lines(self):
        return [l for l in self.lines if 'dropped' in l]

    def red_ledger_rows(self):
        path = os.path.join(self.state_dir, 'gates.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as fh:
            return [json.loads(l)['line'] for l in fh]


class PriorityNeverDestroys(Survival):
    """Defect 1: a priority request cut ahead of two batches in CI and both were lost."""

    def two_in_flight(self, product):
        self.queue_pass(self.lane(product), [self.entry('worker/T-0001', 1),
                                             self.entry('worker/T-0002', 2, 'T-0002')])
        first, second = self.batches()
        self.assertEqual(second['base_ref'], first['ref'])
        return first, second

    def ask_priority(self):
        head = self.heads()['worker/T-0003']
        self.green(head)
        merge_queue.add_request(self.state_dir, 3, 'worker/T-0003', priority=True)
        return head

    def test_a_priority_request_waits_behind_a_full_chain(self):
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 1,
                                            'inflight': 2})
        first, second = self.two_in_flight(product)
        self.ask_priority()
        self.queue_pass(self.lane(product), [])
        self.assertEqual([b['ref'] for b in self.batches()], [first['ref'], second['ref']])
        self.assertEqual(self.dropped_lines(), [])
        self.assertIn(first['ref'], self.heads())
        self.assertIn(second['ref'], self.heads())
        self.assertEqual(self.cancels(), [])
        self.assertTrue(any('worker/T-0003' in l and 'waits' in l for l in self.lines),
                        self.lines)

    def test_a_priority_request_stacks_on_the_chain_when_there_is_room(self):
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 1,
                                            'inflight': 3})
        first, second = self.two_in_flight(product)
        self.ask_priority()
        self.queue_pass(self.lane(product), [])
        got = self.batches()
        self.assertEqual([b['ref'] for b in got[:2]], [first['ref'], second['ref']])
        self.assertEqual(len(got), 3)
        self.assertEqual(got[2]['base_ref'], second['ref'])
        self.assertEqual([m['pr'] for m in got[2]['members']], [3])
        self.assertEqual(self.dropped_lines(), [])


class DropCancelsTheRun(Survival):
    """Defect 2: a dropped or replaced batch's CI run ran on for 10–80 min."""

    def live(self, batch, run_id=600001):
        self.gh.wf[batch['sha']] = [
            {'id': run_id, 'status': 'in_progress', 'conclusion': None,
             'head_branch': batch['ref']},
            {'id': run_id + 1, 'status': 'completed', 'conclusion': 'success',
             'head_branch': batch['ref']}]

    def test_a_dropped_batch_has_its_live_run_cancelled(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                      self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.live(batch)
        self.push_lane('worker/T-0001', {'a.txt': 'a2\n'}, 'feat: a again')   # head moves
        self.queue_pass(self.lane(), [])
        self.assertNotIn(batch['ref'], self.heads())
        self.assertEqual(self.cancels(), [['run', 'cancel', '600001', '-R', SLUG]])
        claim = ci_queue.load_claims(self.state_dir)['600001']
        self.assertEqual((claim['cause'], claim['ref'], claim['sha']),
                         (merge_queue.MQ_DROPPED, batch['ref'], batch['sha']))
        self.assertTrue(any('cancelled run 600001' in l for l in self.lines), self.lines)

    def test_a_queued_run_of_a_dropped_batch_is_force_cancelled(self):
        # a plain cancel on a queued run is accepted and does nothing: the run started later
        # and held its runners anyway (2026-10-05)
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.live(batch, 600021)
        self.gh.wf[batch['sha']][0]['status'] = 'queued'
        self.push_lane('worker/T-0001', {'a.txt': 'a2\n'}, 'feat: a again')   # head moves
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.cancels(), [])
        self.assertIn(['api', '-X', 'POST', f'repos/{SLUG}/actions/runs/600021/force-cancel'],
                      self.gh.calls)
        self.assertTrue(any('cancelled run 600021' in l for l in self.lines), self.lines)

    def test_a_replaced_batch_has_its_live_run_cancelled(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.live(batch, 600011)
        self.push_main({'hot.txt': 'fix\n'}, 'hotfix')        # stale: cut again on the new tip
        _next_second()
        self.queue_pass(self.lane(), [])
        (recut,) = self.batches()
        self.assertNotEqual(recut['ref'], batch['ref'])
        self.assertEqual(self.cancels(), [['run', 'cancel', '600011', '-R', SLUG]])

    def test_a_run_of_another_ref_on_the_same_sha_is_left_alone(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.gh.wf[batch['sha']] = [{'id': 600021, 'status': 'queued', 'conclusion': None,
                                     'head_branch': 'batch/someone-else'}]
        self.push_main({'hot.txt': 'fix\n'}, 'hotfix')
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.cancels(), [])


class MergedMemberIsPruned(Survival):
    """Defect 3: a batch was dropped as "head moved" for a member already landed."""

    def test_a_member_already_on_the_trunk_is_pruned_and_the_batch_lands(self):
        landed = self.heads()['worker/T-0001']
        # T-0001 landed (an earlier batch): the trunk contains its head
        sh(['git', 'push', '-q', 'origin', f'{landed}:refs/heads/main'], cwd=self.worker)
        self.queue_pass(self.lane(), [self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        # the batch was cut with T-0001 still listed (an asf land request not yet consumed)
        data = merge_queue.load(self.state_dir)
        data['batches'][0]['members'].insert(0, {'branch': 'worker/T-0001', 'pr': 1,
                                                 'head': landed, 'requested': True})
        merge_queue.save(self.state_dir, data)
        merge_queue.add_request(self.state_dir, 1, 'worker/T-0001')
        sh(['git', 'push', '-q', 'origin', ':worker/T-0001'], cwd=self.worker)   # branch deleted
        self.green(batch['sha'])
        self.gh.pr_state = {1: 'MERGED', 2: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.dropped_lines(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertTrue(any('pruned' in l and 'worker/T-0001' in l for l in self.lines),
                        self.lines)

    def test_a_head_already_on_the_base_is_never_cut(self):
        landed = self.heads()['worker/T-0001']
        sh(['git', 'push', '-q', 'origin', f'{landed}:refs/heads/main'], cwd=self.worker)
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                      self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0002'])


class RunnerLossIsNoVerdict(Survival):
    """Defect 4: a required job killed with its runner ("The operation was canceled.") is a
    failure on the host; it judged no code — re-run once, then cut again."""

    def lost(self, batch, completed=None):
        self.gh.checks[batch['sha']] = [run_check('gate', 'failure', completed=completed),
                                        run_check('gate-tests')]
        self.gh.annotations['500002'] = CANCELED
        self.gh.wf[batch['sha']] = [{'id': 500001, 'status': 'completed',
                                     'conclusion': 'failure', 'head_branch': batch['ref']}]

    def test_lost_runner_is_rerun_once_then_recut_never_red(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                      self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        self.lost(batch)
        self.queue_pass(self.lane(), [])
        reruns = self.gh.reruns()
        self.assertEqual(reruns, [['run', 'rerun', '--job', '500002', '-R', SLUG]])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertEqual(self.backs, [])
        self.assertEqual(self.dropped_lines(), [])
        # the re-run lost its runner again: no verdict twice — cut again, nobody blamed
        self.lost(batch, completed=stamp(120))
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.gh.reruns()), 1)
        self.assertEqual(self.backs, [])
        self.assertTrue(any('ALARM' in l and batch['ref'] in l for l in self.lines), self.lines)
        (recut,) = self.batches()
        self.assertNotEqual(recut['sha'], batch['sha'])
        self.assertEqual(self.heads()['main'], batch['base'])

    def test_a_real_failure_is_still_red(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [run_check('gate', 'failure'), run_check('gate-tests')]
        self.gh.annotations['500002'] = [{'message': 'Process completed with exit code 1.',
                                          'annotation_level': 'failure'}]
        self.queue_pass(self.lane(), [])
        claims = ci_queue.load_claims(self.state_dir)
        self.assertFalse(any(c.get('cause') == merge_queue.MQ_RERUN for c in claims.values()))
        self.assertTrue(any('flake triage' in l and 'gate' in l for l in self.lines), self.lines)


class EveryAttemptCounts(Survival):
    """The host's default check-runs listing shows only the newest attempt: a re-run cancelled
    under a runner kill hid an earlier attempt already green on the same sha. The verdict takes
    the newest completed attempt that judged code, per check."""

    def test_a_cancelled_rerun_never_hides_a_green_attempt(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        green = dict(run_check('gate', completed=stamp(-600)), id=700001)
        rerun = dict(run_check('gate', 'cancelled', completed=stamp(-60), run=500011),
                     id=700002)
        self.gh.checks[batch['sha']] = [green, rerun, dict(run_check('gate-tests'), id=700003)]
        self.gh.pr_state = {1: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertTrue(any('filter=all' in ' '.join(c) for c in self.gh.calls))

    def test_settled_runs_takes_the_newest_judged_attempt(self):
        a = {'name': 'gate', 'id': 1, 'status': 'completed', 'conclusion': 'failure'}
        b = {'name': 'gate', 'id': 2, 'status': 'completed', 'conclusion': 'success'}
        c = {'name': 'gate', 'id': 3, 'status': 'completed', 'conclusion': 'cancelled'}
        d = {'name': 'gate', 'id': 4, 'status': 'in_progress', 'conclusion': None}
        self.assertEqual(merge_queue.settled_runs([a, b, c]), [b])
        self.assertEqual(merge_queue.settled_runs([b, a]), [b])          # by id, not order
        self.assertEqual(merge_queue.settled_runs([c, d]), [d])          # none judged: newest
        self.assertEqual(merge_queue.settled_runs([c]), [c])
        self.assertEqual(merge_queue.settled_runs([a, d]), [d])          # a re-run of a red: pending
        self.assertEqual(merge_queue.settled_runs([b, d]), [b])          # green already judged


class OnlyRequiredChecksJudge(Survival):
    """Defect 5: a red ``site`` (not required) must never drop a batch, and the queue never
    re-runs it."""

    def cut_one(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (batch,) = self.batches()
        return batch

    def test_a_red_unrequired_job_never_drops_a_pending_batch(self):
        batch = self.cut_one()
        self.gh.checks[batch['sha']] = [run_check('site', 'failure', run=500101),
                                        run_check('gate', None, 'in_progress'),
                                        run_check('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertEqual(self.dropped_lines(), [])
        self.assertEqual(self.gh.reruns(), [])
        self.assertFalse(any(c[:3] == ['run', 'rerun', '--job'] for c in self.gh.calls))

    def test_a_red_unrequired_job_never_holds_a_green_batch(self):
        batch = self.cut_one()
        self.gh.checks[batch['sha']] = [run_check('site', 'failure', run=500101),
                                        run_check('gate'), run_check('gate-tests')]
        self.gh.pr_state = {1: 'MERGED'}
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertEqual(self.gh.reruns(), [])

    def test_a_cancelled_required_job_reruns_only_that_job(self):
        batch = self.cut_one()
        # one workflow run: gate cancelled, site (not required) failed beside it
        self.gh.checks[batch['sha']] = [
            run_check('gate', 'cancelled'), run_check('gate-tests'),
            {**run_check('site', 'failure'),
             'html_url': 'https://github.com/o/p/actions/runs/500001/job/500099',
             'details_url': 'https://github.com/o/p/actions/runs/500001/job/500099'}]
        self.gh.wf[batch['sha']] = [{'id': 500001, 'status': 'completed',
                                     'conclusion': 'failure', 'head_branch': batch['ref']}]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [['run', 'rerun', '--job', '500002', '-R', SLUG]])
        self.assertEqual(self.dropped_lines(), [])


if __name__ == '__main__':
    unittest.main()


WORKFLOW = """name: ci
on: [push]
jobs:
  rules:
    runs-on: ubuntu-latest
    steps: [{run: 'true'}]
  site:
    runs-on: ubuntu-latest
    steps: [{run: 'true'}]
  gate:
    needs: [rules]
    if: "!cancelled() && needs.rules.result == 'success'"
    runs-on: ubuntu-latest
    steps: [{run: 'true'}]
  gate-tests:
    name: gate-tests${{ matrix.suffix }}
    needs:
      - gate
    runs-on: ubuntu-latest
    steps: [{run: 'true'}]
"""


def job_check(name, conclusion, job, run=600001, completed=None):
    return {'name': name, 'status': 'completed', 'conclusion': conclusion, 'id': job,
            'details_url': f'https://github.com/o/p/actions/runs/{run}/job/{job}',
            'html_url': f'https://github.com/o/p/actions/runs/{run}/job/{job}',
            'completed_at': completed or stamp(-3600)}


class SkippedBehindUnrequired(Survival):
    """2026-10-05: a required job skipped because a job it ``needs:`` (one the product does not
    require) was cut short read as red, and the batch was blamed. The workflow's ``needs:`` graph
    names the cause: a cancelled upstream is a non-verdict (re-run once, then cut again); a failed
    one is the real cause — said, and the only failure the triage and the blame read."""

    def setUp(self):
        super().setUp()
        self.write(self.repo, '.github/workflows/ci.yml', WORKFLOW)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'ci'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

    def cut_one(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                      self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        return batch

    def upstream(self, batch, conclusion, completed=None, extra=()):
        self.gh.checks[batch['sha']] = [
            job_check('rules', conclusion, 600002, completed=completed),
            job_check('gate', 'skipped', 600003, completed=completed),
            job_check('gate-tests (a)', 'skipped', 600004, completed=completed), *extra]
        self.gh.wf[batch['sha']] = [{'id': 600001, 'status': 'completed',
                                     'conclusion': 'cancelled' if conclusion == 'cancelled'
                                     else 'failure', 'head_branch': batch['ref'],
                                     'path': '.github/workflows/ci.yml'}]

    def test_a_cancelled_unrequired_upstream_is_rerun_once_then_recut_never_red(self):
        batch = self.cut_one()
        self.upstream(batch, 'cancelled')
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [['run', 'rerun', '--job', '600002', '-R', SLUG]])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertEqual((self.backs, self.dropped_lines()), ([], []))
        self.assertTrue(any('skipped behind rules (cancelled)' in l for l in self.lines),
                        self.lines)
        # its re-run cut short again: no verdict twice — cut again, nobody blamed
        self.upstream(batch, 'cancelled', completed=stamp(120))
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.gh.reruns()), 1)
        self.assertEqual(self.backs, [])
        self.assertTrue(any('ALARM' in l and batch['ref'] in l for l in self.lines), self.lines)
        (recut,) = self.batches()
        self.assertNotEqual(recut['sha'], batch['sha'])

    def test_a_failed_unrequired_upstream_is_named_and_is_the_only_failure_read(self):
        batch = self.cut_one()
        self.upstream(batch, 'failure', extra=[job_check('site', 'failure', 600005)])
        self.queue_pass(self.lane(), [])
        self.assertTrue(any('skipped behind rules (failure)' in l for l in self.lines),
                        self.lines)
        reruns = [c for c in self.gh.calls if c[:3] == ['run', 'rerun', '--job']]
        self.assertIn(['run', 'rerun', '--job', '600002', '-R', SLUG], reruns)
        # site is not upstream of any required job: its red is no cause, never re-run here
        self.assertNotIn(['run', 'rerun', '--job', '600005', '-R', SLUG], reruns)

    def test_needs_graph_reads_scalar_flow_and_block_lists_transitively(self):
        jobs = merge_queue.workflow_jobs(WORKFLOW)
        self.assertEqual(jobs['gate']['needs'], ['rules'])
        self.assertEqual(jobs['gate-tests']['needs'], ['gate'])
        self.assertEqual(merge_queue.upstream_jobs(jobs, 'gate-tests'), {'gate', 'rules'})
        self.assertEqual(merge_queue.job_of(jobs, 'gate-tests (a)'), 'gate-tests')
        self.assertEqual(merge_queue.job_of(jobs, 'rules'), 'rules')
        scalar = merge_queue.workflow_jobs('jobs:\n  a:\n    x: 1\n  b:\n    needs: a\n')
        self.assertEqual(scalar['b']['needs'], ['a'])


def _cut_short_check(name, conclusion='skipped', run=600001):
    return {'name': name, 'status': 'completed', 'conclusion': conclusion,
            'details_url': f'https://github.com/o/p/actions/runs/{run}/job/{run + 1}'}


def _cut_short_listing(run_id, conclusion, status='completed'):
    return {'id': run_id, 'status': status, 'conclusion': conclusion}


class CutShortReads(unittest.TestCase):
    """Design §1's two pure readers, over hand-written check runs and run listings — no repo, no
    fake host, no batch: the one piece of this change assertable without either (S-91805)."""

    def test_a_cancelled_run_comes_back_marked_with_its_run_id(self):
        runs = [_cut_short_check('gate'), _cut_short_check('gate-tests')]
        why = 'gate (skipped), gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        marked, ids = merge_queue.run_cut_short(runs, why, wf)
        self.assertEqual(ids, ['600001'])
        self.assertEqual([r['conclusion'] for r in marked], ['cancelled', 'cancelled'])

    def test_a_timed_out_run_comes_back_the_same_way(self):
        runs = [_cut_short_check('gate'), _cut_short_check('gate-tests')]
        why = 'gate (skipped), gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'timed_out')]
        marked, ids = merge_queue.run_cut_short(runs, why, wf)
        self.assertEqual(ids, ['600001'])
        self.assertEqual([r['conclusion'] for r in marked], ['cancelled', 'cancelled'])

    def test_a_check_a_runner_judged_beside_skipped_ones_is_nothing_at_all(self):
        runs = [_cut_short_check('gate', 'failure'), _cut_short_check('gate-tests')]
        why = 'gate (failure), gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_a_judged_leg_beside_a_skipped_sibling_leg_is_nothing_at_all(self):
        runs = [_cut_short_check('gate-tests (a)', 'failure'), _cut_short_check('gate-tests (b)')]
        why = 'gate-tests (failure), gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_a_skipped_check_on_a_failed_run_is_nothing_at_all(self):
        runs = [_cut_short_check('gate')]
        why = 'gate (skipped)'
        wf = [_cut_short_listing(600001, 'failure')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_a_skipped_check_on_a_successful_run_is_nothing_at_all(self):
        runs = [_cut_short_check('gate')]
        why = 'gate (skipped)'
        wf = [_cut_short_listing(600001, 'success')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_a_skipped_check_with_no_run_id_is_nothing_at_all(self):
        runs = [{'name': 'gate', 'status': 'completed', 'conclusion': 'skipped',
                'details_url': 'https://github.com/o/p/pull/1'}]
        why = 'gate (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_a_skipped_check_whose_run_is_absent_is_nothing_at_all(self):
        runs = [_cut_short_check('gate')]
        why = 'gate (skipped)'
        wf = [_cut_short_listing(600099, 'cancelled')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_skipped_checks_across_two_runs_come_back_with_both_ids_sorted(self):
        runs = [_cut_short_check('gate', run=600003), _cut_short_check('gate-tests', run=600001)]
        why = 'gate (skipped), gate-tests (skipped)'
        wf = [_cut_short_listing(600003, 'cancelled'), _cut_short_listing(600001, 'timed_out')]
        marked, ids = merge_queue.run_cut_short(runs, why, wf)
        self.assertEqual(ids, ['600001', '600003'])

    def test_one_healthy_leg_beside_a_cut_short_leg_of_the_same_job_is_nothing_at_all(self):
        runs = [_cut_short_check('gate-tests (a)', run=600001),
                _cut_short_check('gate-tests (b)', run=600002)]
        why = 'gate-tests (skipped), gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled'), _cut_short_listing(600002, 'success')]
        self.assertIsNone(merge_queue.run_cut_short(runs, why, wf))

    def test_an_unrequired_check_is_neither_read_nor_remarked(self):
        runs = [_cut_short_check('gate'), _cut_short_check('site', 'failure')]
        why = 'gate (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        marked, _ids = merge_queue.run_cut_short(runs, why, wf)
        site = next(r for r in marked if r['name'] == 'site')
        self.assertEqual(site['conclusion'], 'failure')

    def test_a_matrix_leg_is_read_by_its_job_key(self):
        runs = [_cut_short_check('gate-tests (a)')]
        why = 'gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        marked, _ids = merge_queue.run_cut_short(runs, why, wf)
        self.assertEqual(marked[0]['conclusion'], 'cancelled')

    def test_the_input_is_not_mutated(self):
        runs = [_cut_short_check('gate'), _cut_short_check('gate-tests')]
        why = 'gate (skipped), gate-tests (skipped)'
        wf = [_cut_short_listing(600001, 'cancelled')]
        before = copy.deepcopy(runs)
        marked, _ids = merge_queue.run_cut_short(runs, why, wf)
        self.assertEqual(runs, before)
        self.assertIsNot(marked, runs)
        self.assertEqual([r['conclusion'] for r in marked], ['cancelled', 'cancelled'])

    def test_green_pending_or_no_skip_is_nothing_at_all(self):
        runs = [_cut_short_check('gate', 'success')]
        wf = [_cut_short_listing(600001, 'cancelled')]
        self.assertIsNone(merge_queue.run_cut_short(runs, '', wf))
        self.assertIsNone(merge_queue.run_cut_short(runs, 'gate (failure)', wf))


class HostCancelIsNoVerdict(Survival):
    """2026-10-08: a batch whose required jobs were only skipped on a host-cancelled run was
    dropped as red and split — PRs #1294, #1295 and #1254 lost. A run the host cut short judges
    no code one level up from a single skipped check (S-91806)."""

    def setUp(self):
        super().setUp()
        self.write(self.repo, '.github/workflows/ci.yml', WORKFLOW)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'ci'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

    def cut_one(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                      self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        return batch

    def cancel(self, batch, run_id=600001, conclusion='cancelled', completed=None):
        self.gh.checks[batch['sha']] = [
            job_check('gate', 'skipped', 600003, run=run_id, completed=completed),
            job_check('gate-tests (a)', 'skipped', 600004, run=run_id, completed=completed)]
        self.gh.wf[batch['sha']] = [{'id': run_id, 'status': 'completed', 'conclusion': conclusion,
                                     'head_branch': batch['ref'],
                                     'path': '.github/workflows/ci.yml'}]

    def _run_reads(self):
        return [c for c in self.gh.calls if c[0] == 'api' and '/actions/runs?head_sha=' in c[1]]

    def test_a_host_cancelled_run_is_rerun_whole_once_then_recut_never_red(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [['run', 'rerun', '600001', '-R', SLUG]])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertEqual((self.backs, self.dropped_lines()), ([], []))
        self.assertEqual(self.red_ledger_rows(), [])
        claim = ci_queue.load_claims(self.state_dir)['600001']
        self.assertEqual((claim['cause'], claim['sha']), (merge_queue.MQ_RERUN, batch['sha']))
        self.assertTrue(any('host cut short run(s) 600001' in l
                            and 'the required jobs judged no code' in l for l in self.lines),
                        self.lines)
        # its re-run cut short again: no verdict twice — cut again, nobody blamed
        self.cancel(batch, completed=stamp(120))
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.gh.reruns()), 1)
        self.assertEqual(self.backs, [])
        self.assertEqual(self.red_ledger_rows(), [])
        self.assertTrue(any('ALARM' in l and batch['ref'] in l for l in self.lines), self.lines)
        (recut,) = self.batches()
        self.assertNotEqual(recut['sha'], batch['sha'])
        self.assertEqual(self.heads()['main'], batch['base'])

    def test_no_member_is_blamed_and_no_batch_is_split(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.queue_pass(self.lane(), [])
        self.cancel(batch, completed=stamp(120))
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        (recut,) = self.batches()
        self.assertEqual({m['branch'] for m in recut['members']},
                         {'worker/T-0001', 'worker/T-0002'})

    def test_a_judged_upstream_on_a_cut_short_run_is_rerun_whole_not_announced(self):
        batch = self.cut_one()
        self.gh.checks[batch['sha']] = [
            job_check('rules', 'failure', 600002),
            job_check('gate', 'skipped', 600003),
            job_check('gate-tests (a)', 'skipped', 600004)]
        self.gh.wf[batch['sha']] = [{'id': 600001, 'status': 'completed',
                                     'conclusion': 'cancelled', 'head_branch': batch['ref'],
                                     'path': '.github/workflows/ci.yml'}]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [['run', 'rerun', '600001', '-R', SLUG]])
        self.assertFalse(any('the real cause' in l for l in self.lines), self.lines)
        self.assertEqual((self.backs, self.dropped_lines()), ([], []))

    def test_an_ordinary_red_reads_the_listing_not_at_all(self):
        batch = self.cut_one()
        self.gh.checks[batch['sha']] = [run_check('gate', 'failure'), run_check('gate-tests')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self._run_reads(), [])

    def test_a_skip_only_red_reads_the_listing_once(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self._run_reads()), 1)

    def test_skipped_behind_a_cancelled_upstream_reads_the_listing_once(self):
        batch = self.cut_one()
        self.gh.checks[batch['sha']] = [
            job_check('rules', 'cancelled', 600002),
            job_check('gate', 'skipped', 600003),
            job_check('gate-tests (a)', 'skipped', 600004)]
        self.gh.wf[batch['sha']] = [{'id': 600001, 'status': 'completed',
                                     'conclusion': 'cancelled', 'head_branch': batch['ref'],
                                     'path': '.github/workflows/ci.yml'}]
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self._run_reads()), 1)

    def test_an_unreadable_listing_leaves_the_batch_judged_as_before(self):
        batch = self.cut_one()
        self.cancel(batch)
        with mock.patch.object(merge_queue, '_workflow_runs', return_value=None):
            self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [])
        self.assertFalse(any('host cut short' in l for l in self.lines), self.lines)

    def test_no_flake_settle_for_skipped_checks(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.queue_pass(self.lane(), [])
        data = flake.load(self.state_dir)
        self.assertEqual(data['reruns'], {})
        self.assertEqual(data['quarantine'], [])


class _ContentionSrc:
    """:func:`asf.ci_queue._contention`'s ``src``: a fixed p50 and a fixed ``gh run rerun``
    answer, honoured or refused."""

    def __init__(self, ok=True):
        self.ok = ok

    def job_p50_min(self, _wf, _job):
        return 3

    def _gh(self, _args):
        return (0, '', '') if self.ok else None


class ContentionIsTheStartQueuesRerun(Survival):
    """2026-10-08: the merge queue and the CI start queue both asked the host to re-run the same
    cut-short run, each unaware of the other's claim. A contention cancel the start queue already
    made is its re-run to own (S-91807)."""

    def setUp(self):
        super().setUp()
        self.write(self.repo, '.github/workflows/ci.yml', WORKFLOW)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'ci'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

    def cut_one(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1),
                                      self.entry('worker/T-0002', 2, 'T-0002')])
        (batch,) = self.batches()
        return batch

    def cancel(self, batch, run_id=600001):
        self.gh.checks[batch['sha']] = [job_check('gate', 'skipped', 600003, run=run_id),
                                        job_check('gate-tests (a)', 'skipped', 600004, run=run_id)]
        self.gh.wf[batch['sha']] = [{'id': run_id, 'status': 'completed',
                                     'conclusion': 'cancelled', 'head_branch': batch['ref'],
                                     'path': '.github/workflows/ci.yml'}]

    def claim(self, batch, cause=None, run_id='600001', age_s=0, **extra):
        at = ci_queue._now() - datetime.timedelta(seconds=age_s)
        fields = {'sha': batch['sha'], 'branch': batch['ref'], 'job': 'rules', 'p50': 3,
                  'timeout': 10, 'attempt': 1}
        fields.update(extra)
        ci_queue.claim_cancel(self.state_dir, run_id, cause or ci_queue.CONTENTION_RERUN, at,
                              **fields)

    def test_with_a_contention_rerun_claim_the_judge_asks_the_host_for_nothing(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch)
        before = ci_queue.load_claims(self.state_dir)['600001']
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        claims = ci_queue.load_claims(self.state_dir)
        self.assertEqual(claims['600001'], before)
        self.assertNotIn(f"mq:{batch['sha']}", claims)
        self.assertTrue(any('runner contention' in l and 'the CI start queue' in l
                            for l in self.lines), self.lines)

    def test_a_contention_rerun_claim_past_grace_with_nothing_live_cuts_the_batch_again(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch, age_s=merge_queue.RERUN_GRACE_S + 1)
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.backs, [])
        self.assertTrue(any('ALARM' in l and batch['ref'] in l for l in self.lines), self.lines)
        (recut,) = self.batches()
        self.assertNotEqual(recut['sha'], batch['sha'])

    def test_a_contention_rerun_claim_past_grace_with_a_run_still_live_stays_pending(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.gh.wf[batch['sha']].append({'id': 600005, 'status': 'in_progress',
                                         'conclusion': None, 'head_branch': batch['ref']})
        self.claim(batch, age_s=merge_queue.RERUN_GRACE_S + 1)
        self.queue_pass(self.lane(), [])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertEqual(self.backs, [])

    def test_a_contention_refused_claim_cuts_the_batch_again_never_red(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch, cause=ci_queue.CONTENTION_REFUSED)
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.red_ledger_rows(), [])
        (recut,) = self.batches()
        self.assertNotEqual(recut['sha'], batch['sha'])

    def test_a_contention_alarm_claim_cuts_the_batch_again_never_red(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch, cause=ci_queue.CONTENTION_ALARM)
        _next_second()
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.red_ledger_rows(), [])
        (recut,) = self.batches()
        self.assertNotEqual(recut['sha'], batch['sha'])

    def test_a_cutting_contention_claim_writes_the_alarm_claim_and_leaves_the_start_queues_claim(
            self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch, cause=ci_queue.CONTENTION_REFUSED)
        before = dict(ci_queue.load_claims(self.state_dir)['600001'])
        self.queue_pass(self.lane(), [])
        claims = ci_queue.load_claims(self.state_dir)
        self.assertEqual(claims['600001'], before)
        alarm = claims[f"mq:{batch['sha']}"]
        self.assertEqual(alarm['cause'], merge_queue.MQ_ALARM)
        rows = merge_queue.doctor_rows(self.product())
        self.assertTrue(any(batch['ref'] in d for _ok, _red, d in rows), rows)

    def test_a_contention_claim_for_another_sha_is_ignored(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch, run_id='700001', sha='f' * 40)
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.reruns(), [['run', 'rerun', '600001', '-R', SLUG]])

    def test_the_newest_claim_decides_when_more_than_one_names_the_sha(self):
        batch = self.cut_one()
        self.cancel(batch)
        self.claim(batch, cause=ci_queue.CONTENTION_ALARM, run_id='600001', age_s=100)
        self.claim(batch, cause=ci_queue.CONTENTION_RERUN, run_id='600002', age_s=1)
        self.queue_pass(self.lane(), [])
        self.assertEqual([b['ref'] for b in self.batches()], [batch['ref']])
        self.assertEqual(self.gh.reruns(), [])

    def test_the_refused_cause_is_a_named_constant_written_and_read_back(self):
        out = []
        run = {'databaseId': 900001, 'headBranch': 'worker/T-0001', 'headSha': 'a' * 40,
              'attempt': 1}
        ci_queue._contention(self.product(), _ContentionSrc(ok=False), 'ci.yml', run, 'rules',
                             10, {}, self.state_dir, 'run 900001', out.append, False,
                             ci_queue._now())
        claim = ci_queue.load_claims(self.state_dir)['900001']
        self.assertEqual(claim['cause'], ci_queue.CONTENTION_REFUSED)
        self.assertEqual(claim['cause'], 'contention-refused')

    def test_the_alarm_cause_is_the_existing_constant_unchanged_on_disk(self):
        out = []
        run = {'databaseId': 900002, 'headBranch': 'worker/T-0001', 'headSha': 'b' * 40,
              'attempt': 1}
        ci_queue._contention(self.product(), _ContentionSrc(), 'ci.yml', run, 'rules', 10, {},
                             self.state_dir, 'run 900002', out.append, False, ci_queue._now())
        claims = ci_queue.load_claims(self.state_dir)
        run2 = dict(run, databaseId=900003)
        ci_queue._contention(self.product(), _ContentionSrc(), 'ci.yml', run2, 'rules', 10,
                             claims, self.state_dir, 'run 900003', out.append, False,
                             ci_queue._now())
        claim = ci_queue.load_claims(self.state_dir)['900003']
        self.assertEqual(claim['cause'], ci_queue.CONTENTION_ALARM)
        self.assertEqual(claim['cause'], 'contention-alarm')

    def test_all_three_contention_constants_are_read_by_the_existing_tables(self):
        from asf import ci_cancels
        from asf.metrics import reds
        for cause in (ci_queue.CONTENTION_RERUN, ci_queue.CONTENTION_REFUSED,
                      ci_queue.CONTENTION_ALARM):
            self.assertEqual(ci_cancels.HONOURED[cause], 'timeout')
            self.assertIn(cause, reds.RUNNER_CLAIMS)
