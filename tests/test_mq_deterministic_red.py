"""A deterministic red is never re-run, and a red flake triage will re-run never drops the chain
(:mod:`asf.merge_queue`, :mod:`asf.flake`).

* A registers / pre-cut check red reaching CI is deterministic: re-running it costs a heavy run and
  answers the same. A red job named in ``merge_queue.deterministic_jobs``, or one whose failed step
  runs a ``merge_queue.precut_check`` command, goes straight to the blame — the member whose rows
  the log names is sent back, the rest are cut again — with no ``gh run rerun``.
* A required job red while its workflow run is still in progress cannot be re-run yet (the host
  refuses a job re-run on a live run, whatever words it uses): the batch waits, it is never red on
  a refusal. Its stacked batches stay in flight; only a red re-run drops the chain.
"""
import json
import unittest
from unittest import mock

from asf import flake, github, merge_queue
from asf.harvest import lane

from tests import contracts
from tests.test_merge_queue import QueueRepo, check_run, job_run
from tests.test_queue_precut_check import REG, RegisterRepo, register_log


def reruns(gh):
    return [c for c in gh.calls if c[:2] == ['run', 'rerun']]


class DeterministicRed(RegisterRepo):
    """A duplicate-row red reached CI on the ``rules`` job: the log names the file and the rows."""

    FINDING = (f'{REG}: migration 0100-0109 (plan-bravo) overlaps migration 0100 (main-row) '
               f'- the same id claimed twice')

    def setUp(self):
        super().setUp()
        self.book('worker/T-0001', '| migration | 0200-0209 | plan-alpha | reserved |')
        self.book('worker/T-0002', '| migration | 0100-0109 | plan-bravo | reserved |')
        self.book('worker/T-0003', '| migration | 0300-0309 | plan-charlie | reserved |')

    def cut_and_red(self, product):
        self.queue_pass(self.lane(product), [self.entry(f'worker/T-000{i}', i, f'T-000{i}',
                                                        files=(REG,)) for i in (1, 2, 3)])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [job_run('rules', '201'), check_run('gate', 'skipped'),
                                        check_run('gate-tests', 'skipped')]
        self.gh.logs['201'] = register_log(self.FINDING)
        self.gh.steps['201'] = 'gate:fast (registers)'
        self.queue_pass(self.lane(product), [])
        return batch

    def assert_blamed_without_rerun(self):
        self.assertEqual(reruns(self.gh), [])
        self.assertEqual([(b, k) for b, k, _t, _f in self.backs], [('worker/T-0002', 'gate')])
        (again,) = self.batches()
        self.assertEqual([m['branch'] for m in again['members']],
                         ['worker/T-0001', 'worker/T-0003'])
        self.assertFalse(any('re-run before' in l for l in self.lines), self.lines)
        self.assertTrue(any('deterministic' in l for l in self.lines), self.lines)

    def test_a_job_named_deterministic_is_blamed_at_once_never_rerun(self):
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 3,
                                            'deterministic_jobs': ['rules']})
        self.cut_and_red(product)
        self.assert_blamed_without_rerun()

    def test_a_failed_step_running_the_precut_check_is_deterministic(self):
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 3,
                                            'precut_check': 'make precut-check'})
        # the cut's own check passes (it books per member against main only); CI's run of it does not
        with mock.patch.object(merge_queue, 'precut_check_red', return_value=None), \
                mock.patch.object(lane, 'red_step',
                                  return_value=('gate:fast (registers)', 'make precut-check')):
            self.cut_and_red(product)
        self.assert_blamed_without_rerun()

    def test_a_culprit_is_never_batched_again_until_its_head_changes(self):
        # 2026-10-05: the same duplicate-row collision re-batched 5× over 3 h — the same tree
        # answers the same; only a new head is a new answer
        product = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 3,
                                            'deterministic_jobs': ['rules']})
        self.cut_and_red(product)
        self.lines.clear()
        self.queue_pass(self.lane(product), [self.entry('worker/T-0002', 2, 'T-0002',
                                                        files=(REG,))])
        self.assertFalse(any(m['branch'] == 'worker/T-0002' for b in self.batches()
                             for m in b['members']))
        self.assertTrue(any('held out' in l and 'until its head changes' in l
                            for l in self.lines), self.lines)
        # its correct round moved the head: a new tree, a new answer — it is cut again
        self.book('worker/T-0002', '| migration | 0400-0409 | plan-bravo | reserved |')
        self.queue_pass(self.lane(product), [self.entry('worker/T-0002', 2, 'T-0002',
                                                        files=(REG,))])
        self.assertTrue(any(m['branch'] == 'worker/T-0002' for b in self.batches()
                            for m in b['members']))

    def test_the_hold_is_config(self):
        conv = type('C', (), {'map_of': lambda self, k: {'hold_culprit': 'off'}})()
        self.assertFalse(merge_queue.settings(conv)['hold_culprit'])
        conv = type('C', (), {'map_of': lambda self, k: {}})()
        self.assertTrue(merge_queue.settings(conv)['hold_culprit'])

    def test_an_unnamed_job_is_still_rerun_first(self):
        self.cut_and_red(self.product())
        self.assertEqual(len(reruns(self.gh)), 1)
        self.assertEqual(self.backs, [])

    def test_settings_read_deterministic_jobs(self):
        conv = type('C', (), {'map_of': lambda self, k: {'deterministic_jobs': ['rules', '', 3]}})()
        self.assertEqual(merge_queue.settings(conv)['deterministic_jobs'], ('rules',))
        conv = type('C', (), {'map_of': lambda self, k: {'deterministic_jobs': 'rules'}})()
        self.assertEqual(merge_queue.settings(conv)['deterministic_jobs'], ('rules',))
        conv = type('C', (), {'map_of': lambda self, k: {}})()
        self.assertEqual(merge_queue.settings(conv)['deterministic_jobs'], ())


class TriageNeverRerunsDeterministic(unittest.TestCase):
    def test_a_deterministic_name_is_a_defect_with_no_rerun(self):
        import tempfile, shutil
        from asf import env
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        calls = []
        product = env.Product('p', {'repo_slug': 'o/p'})
        defects, held = flake.triage(
            product, d, 'o/p', 'a' * 40,
            [{'name': 'rules', 'link': 'https://x/actions/runs/7/job/9'}],
            out=lambda *_: None, gh=lambda a: calls.append(a) or (0, '[]', ''),
            deterministic=('rules',))
        self.assertEqual((defects, held), (['rules'], []))
        self.assertFalse([c for c in calls if c[:2] == ['run', 'rerun']])


class LiveRunGH:
    """A host that refuses a job re-run on a live run in words triage never listed."""

    def __init__(self, status):
        self.status, self.calls = status, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ['run', 'rerun']:
            return 1, '', 'HTTP 403: This job cannot be re-run right now'
        if args[0] == 'api' and args[1].endswith('/annotations'):
            return 0, '[]', ''
        if args[0] == 'api' and '/actions/runs/' in args[1]:
            return 0, json.dumps({'status': self.status}), ''
        return 1, '', 'unexpected'


class RefusedOnALiveRun(unittest.TestCase):
    def triage(self, status):
        import tempfile, shutil
        from asf import env
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        gh = LiveRunGH(status)
        got = flake.triage(env.Product('p', {'repo_slug': 'o/p'}), d, 'o/p', 'a' * 40,
                           [{'name': 'gate-tests', 'link': 'https://x/actions/runs/7/job/9'}],
                           out=lambda *_: None, gh=gh)
        return got, gh

    def test_a_refused_rerun_on_a_run_still_in_progress_is_held(self):
        (defects, held), gh = self.triage('in_progress')
        self.assertEqual((defects, held), ([], ['gate-tests']))
        self.assertIn(['api', 'repos/o/p/actions/runs/7'], gh.calls)

    def test_a_refused_rerun_on_a_completed_run_is_a_defect_as_before(self):
        (defects, held), _gh = self.triage('completed')
        self.assertEqual((defects, held), (['gate-tests'], []))


class StackSurvivesTriage(QueueRepo):
    """Two stacked batches; the base goes red on one required job while its run still runs."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        for i, n in enumerate('abc', 1):
            self.push_lane(f'worker/T-000{i}', {f'{n}.txt': f'{n}\n'}, f'feat: {n}')
        self.product_ = self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 2})
        self.queue_pass(self.lane(self.product_),
                        [self.entry(f'worker/T-000{i}', i, f'T-000{i}') for i in (1, 2, 3)])
        self.first, self.second = self.batches()
        self.assertEqual(self.second['base_ref'], self.first['ref'])
        self.refuse = True
        fake = self.gh

        def gh(args):
            if args[:2] == ['run', 'rerun'] and self.refuse:
                fake.calls.append(list(args))
                return 1, '', 'HTTP 403: This job cannot be re-run right now'
            if args[0] == 'api' and args[1].endswith('/actions/runs/7'):
                fake.calls.append(list(args))
                return 0, json.dumps({'status': 'in_progress' if self.refuse else 'completed'}), ''
            return fake(args)
        patch = mock.patch.object(github, 'call', side_effect=contracts.as_call(gh))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_refused_rerun_keeps_both_batches_and_a_red_rerun_recuts(self):
        sha1, sha2 = self.first['sha'], self.second['sha']
        self.gh.checks[sha1] = [check_run('gate'), job_run('gate-tests', '301'),
                                check_run('m-e2e', status_='in_progress')]
        self.gh.checks[sha2] = [check_run('gate', status_='queued'),
                                check_run('gate-tests', status_='queued')]
        self.queue_pass(self.lane(self.product_), [])
        self.assertEqual([b['ref'] for b in self.batches()], [self.first['ref'], self.second['ref']])
        self.assertEqual(self.backs, [])
        # the run completes: the re-run is taken, and goes red again — a defect: the chain re-cuts
        self.refuse = False
        self.gh.checks[sha1] = [check_run('gate'), job_run('gate-tests', '301'),
                                check_run('m-e2e')]
        self.queue_pass(self.lane(self.product_), [])
        self.assertEqual(len(self.batches()), 2)
        self.assertEqual(self.backs, [])
        self.gh.checks[sha1] = [check_run('gate'), job_run('gate-tests', '302'),
                                check_run('m-e2e')]
        self.queue_pass(self.lane(self.product_), [])
        self.assertNotIn(self.second['ref'], [b['ref'] for b in self.batches()])
        self.assertEqual([b for b, _k, _t, _f in self.backs], ['worker/T-0001'][:len(self.backs)])


class DeterministicHoldStands(unittest.TestCase):
    """A landing-gate hold over a deterministic job is never cleared as 'never re-run'."""

    def why(self, jobs):
        import tempfile, shutil
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        conv = type('C', (), {'map_of': lambda _s, k: {'deterministic_jobs': jobs}})()
        ln = mock.Mock(conv=conv, state_dir=d, slug='o/p')
        run = {'correction': {'kind': lane.LANDING_GATE, 'finding': ['rules (failure)'],
                              'text': 'batch batch/x @ abcdef123456 checks red: rules (failure)'}}
        with mock.patch.object(merge_queue, 'required_set', return_value=(('rules',), None)), \
                mock.patch.object(merge_queue, 'check_runs', return_value=[]):
            return merge_queue._hold_regate_why(ln, run, 'h' * 40, 't' * 40)

    def test_a_deterministic_job_hold_stands(self):
        self.assertIsNone(self.why(['rules']))

    def test_an_unnamed_job_never_rerun_is_cleared_as_before(self):
        self.assertIn('never re-run', self.why([]))


if __name__ == '__main__':
    unittest.main()
