"""A batch in flight survives everything that is not a verdict on its own code
(:mod:`asf.merge_queue`).

Five ways a live batch was lost or kept expensive (2026-10-05, overnight):

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
   and the queue never re-runs one.
"""
import time
import unittest

from asf import ci_queue, merge_queue
from asf.harvest import lane

from tests.test_lane import sh
from tests.test_merge_queue import SLUG, NoVerdictGH, QueueRepo, run_check, stamp
from asf import github
from tests import contracts
from unittest import mock

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
