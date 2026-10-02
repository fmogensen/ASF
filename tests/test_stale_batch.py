"""A stale or stuck merge-queue batch never blocks the queue.

2026-10-01, a product: the CI start queue cancelled a batch run for a trunk run; its re-run was
refused three times, the fresh dispatch too (the batch ref was gone, the run on an old ci.yml),
so the record went STUCK and asked again every tick — at the head of the line, with nothing
behind it landing for 19 h. Now a cancelled run on a batch ref is the merge queue's: once it is
stuck, refused :data:`asf.ci_queue.RERUN_REFUSALS_MAX` times or made on a workflow the trunk has
changed since, the start queue drops its record and place in line and asks the merge queue to
rebuild the batch (:func:`asf.merge_queue.request_rebuild`); the next pass cuts the members again
on the trunk's tip — a new sha, a fresh run.
"""
import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import ci_queue, env, merge_queue

from tests.test_lane import sh
from tests.test_merge_queue import QueueRepo


T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
REF = 'batch/20261001-100411-d97e438'


def product():
    return env.Product('p', {'repo_slug': 'o/r', 'main': 'main', 'conventions': {
        'landing': 'pull-request', 'merge': 'queue', 'merge_queue': {'ref_prefix': 'batch/'}}})


class FakeSrc:
    """A source whose run ``rid`` was made on workflow blob ``old`` and the trunk holds ``new``;
    every ``gh`` write refused."""

    def __init__(self, old='1111111', new='1111111'):
        self.old, self.new, self.calls = old, new, []

    def run_workflow(self, rid):
        return ('a' * 40, '.github/workflows/ci.yml')

    def blob(self, path, ref):
        return self.old if ref == 'a' * 40 else self.new

    def gh_try(self, args):
        self.calls.append(args)
        return None, 'refused'

    def _gh(self, args):
        self.calls.append(args)
        return None


class FakeQ:
    def __init__(self, relief):
        self.data = {'relief': relief, 'entries': {ci_queue._rerun_key(r): {'kind': 'batch'}
                                                  for r in relief}}

    def admit(self, *_a, **_k):     # never reached for a stale batch
        raise AssertionError('a stale batch asked for a place in line')


def record(**kw):
    rec = {'id': 36846800027, 'kind': 'batch', 'item': 'batch', 'label': 'batch',
           'workflow': 'ci.yml', 'branch': REF, 'sha': 'd' * 40, 'prio': 4,
           'at': (T0 - datetime.timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')}
    rec.update(kw)
    return rec


class StartQueueHandsBack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = os.path.join(self.tmp, 'state')
        p = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir)
        p.start()
        self.addCleanup(p.stop)
        self.lines = []
        merge_queue.save(self.state_dir, {'batches': [
            {'ref': REF, 'sha': 'd' * 40, 'base': 'b' * 40, 'base_ref': None,
             'members': [{'branch': 'worker/T-1', 'pr': 1, 'head': 'e' * 40}]}]})

    def run_pass(self, rec, src, started=lambda _r: None):
        q = FakeQ([rec])
        n = ci_queue._rerun_cancelled(q, src, product(), started, False, self.lines.append, T0)
        return q, n

    def assert_rebuilt(self, q, why):
        self.assertEqual(q.data['relief'], [])
        self.assertEqual(q.data['entries'], {})                 # no place in line held
        asked = merge_queue.load_rebuilds(self.state_dir)
        self.assertIn(REF, asked)
        self.assertIn(why, asked[REF]['why'])
        self.assertTrue(any(l.startswith(f'ci queue: batch {REF} (cancelled run 36846800027) '
                                         f'dropped') and 'rebuilds it on main' in l
                            for l in self.lines), self.lines)

    def test_a_batch_run_on_an_old_ci_yml_is_rebuilt_not_replayed(self):
        src = FakeSrc(old='33933d1aaa', new='577d8c9bbb')
        q, _n = self.run_pass(record(), src)
        self.assert_rebuilt(q, 'old ci.yml (33933d1→577d8c9)')
        self.assertEqual(src.calls, [])                         # no rerun, no dispatch

    def test_a_stuck_batch_is_rebuilt_at_once_not_every_30_min(self):
        stuck = {'since': '2026-10-01T11:00:00Z', 'tried': '2026-10-01T11:59:00Z',
                 'why': 'fresh run refused too'}
        q, _n = self.run_pass(record(stuck=stuck, refusals=3), FakeSrc())
        self.assert_rebuilt(q, 'STUCK')

    def test_the_last_refused_rerun_rebuilds_instead_of_a_fresh_dispatch(self):
        src = FakeSrc()
        q = FakeQ([record(refusals=2, refused_why='refused')])
        q.admit = lambda *_a, **_k: mock.Mock(admitted=True, line=None)
        ci_queue._rerun_cancelled(q, src, product(), lambda _r: ({'startedAt': None}, 'main'),
                                  False, self.lines.append, T0)
        self.assertEqual([c[:2] for c in src.calls], [['run', 'rerun']])   # no dispatch
        self.assert_rebuilt(q, 're-run refused 3 times')

    def test_a_batch_the_merge_queue_dropped_leaves_the_line(self):
        merge_queue.save(self.state_dir, {'batches': []})
        q, _n = self.run_pass(record(stuck={'since': '2026-10-01T11:00:00Z',
                                            'tried': '2026-10-01T11:59:00Z', 'why': 'x'}),
                              FakeSrc())
        self.assertEqual((q.data['relief'], q.data['entries']), ([], {}))
        self.assertEqual(merge_queue.load_rebuilds(self.state_dir), {})
        self.assertTrue(any('no longer holds it' in l for l in self.lines), self.lines)

    def test_a_fresh_batch_run_still_waits_for_its_protected_run(self):
        q, _n = self.run_pass(record(), FakeSrc())             # same workflow, not stuck
        self.assertEqual(len(q.data['relief']), 1)
        self.assertEqual(merge_queue.load_rebuilds(self.state_dir), {})

    def test_a_pr_run_is_never_handed_to_the_merge_queue(self):
        rec = record(branch='worker/T-9', kind='pr', stuck={'since': '2026-10-01T11:00:00Z',
                                                            'tried': '2026-10-01T11:59:00Z',
                                                            'why': 'x'})
        q, _n = self.run_pass(rec, FakeSrc(old='1', new='2'))
        self.assertEqual(len(q.data['relief']), 1)
        self.assertEqual(merge_queue.load_rebuilds(self.state_dir), {})


class MergeQueueRebuilds(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat: a')

    def test_a_rebuild_request_drops_the_batch_and_cuts_it_again_in_the_same_pass(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1)])
        (old,) = self.batches()
        # the trunk moved meanwhile (a new ci.yml): the rebuilt batch is cut on its tip
        sh(['git', 'checkout', '-q', 'main'], cwd=self.repo)
        sh(['git', 'pull', '-q', 'origin', 'main'], cwd=self.repo)
        self.write(self.repo, 'ci.yml', 'v2\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'ci: v2'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        tip = self.heads()['main']
        self.assertTrue(merge_queue.request_rebuild(self.state_dir, old['ref'], 'STUCK — x'))
        self.queue_pass(self.lane(), [])
        (new,) = self.batches()
        self.assertNotEqual(new['sha'], old['sha'])
        self.assertEqual(new['base'], tip)
        self.assertEqual([m['branch'] for m in new['members']], ['worker/T-0001'])
        self.assertNotIn(old['ref'], self.heads())
        self.assertIn(new['ref'], self.heads())                 # its push starts a fresh run
        self.assertTrue(any(f"batch {old['ref']} dropped — rebuilt on main — STUCK" in l
                            for l in self.lines), self.lines)
        self.assertEqual(merge_queue.load_rebuilds(self.state_dir), {})

    def test_a_rebuild_for_a_batch_not_held_is_not_written(self):
        self.assertFalse(merge_queue.request_rebuild(self.state_dir, 'batch/nope', 'x'))
        self.assertEqual(merge_queue.load_rebuilds(self.state_dir), {})


if __name__ == '__main__':
    unittest.main()
