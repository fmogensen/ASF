"""B-0275 — a naming reword must not throw away a green, land-requested PR's CI.

Two halves, the acceptance's three bullets:

* the reword defers while the PR carries an ``asf land`` request, a green required check on its
  head, or a run in flight (:class:`RewordDefers`);
* a head whose root tree is byte-identical to an earlier green head of the same PR is read green
  by the merge queue and by the landing gate, and said so — ``green carried from <old head>
  (identical tree)``; a head whose tree differs carries nothing (:class:`GreenCarries`).

Round 1's review asked for two more, each the half above read at the wrong moment: the green guard
must ask the head itself where this lane's memory of it is necessarily empty
(:class:`RewordReadsTheHead`, C1), and a carry must never answer for a head the
conflicts-with-trunk guard would have stopped (:class:`ConflictBeforeCarry`, C2). Round 3 asked
for the first of those to be pinned to the sha that earned the green, as the landing gate pins it
(:class:`RewordReadsTheHead`, round 3's C1).
"""
import json
import os
import unittest
from unittest import mock

from asf import ci_flight, env, merge_queue, tree_green
from asf.harvest import harvest, lane
from asf.workers import lifecycle

from tests.test_lane import LaneFixture, NamingRepair, sh
from tests.test_merge_queue import QueueRepo, check_run


class RewordDefers(LaneFixture):
    """The lane rewords a branch whose subjects do not name its item — but never one whose CI the
    rewrite would throw away. The tip is untouched and the next pass asks again."""

    PR = 7
    B = NamingRepair.B
    AUTHOR = NamingRepair.AUTHOR
    push_commits = NamingRepair.push_commits
    tip = NamingRepair.tip
    sessions = NamingRepair.sessions

    def held_for_naming(self, old, pr=None):
        """The session record the lane rewords off: BACK on a naming correction, at ``old``."""
        self.session('coder-t-0001', 'T-0001', self.B)
        with open(self.sessions(), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': 'coder-t-0001', 'rounds': 3,
                                'lane': {'state': lane.BACK, 'head': old,
                                         'pr': self.PR if pr is None else pr,
                                         'at': '2026-09-21T00:06:00Z', 'reason': 'kind=naming',
                                         'item': 'T-0001'},
                                'correction': {'kind': lifecycle.NAMING,
                                               'text': 'commits do not name T-0001',
                                               'at': '2026-09-21T00:06:00Z'}}) + '\n')

    def one_pass(self):
        lines = []
        lane.lane_pass(self.product(), self.state_dir, out=lines.append)
        return lines

    def test_a_land_requested_pr_is_never_reworded(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.held_for_naming(old)
        merge_queue.add_request(self.state_dir, self.PR, self.B, priority=True)
        lines = self.one_pass()
        self.assertEqual(self.tip(), old)                      # the head stands: its CI stands
        self.assertTrue(any(l.startswith(f'reword {self.B}: deferred') and 'land request' in l
                            for l in lines), lines)
        # deferred, not held: no session is spawned and no round is spent
        self.assertIn('T-0001', lifecycle.corrections(self.sessions()))
        self.assertFalse(any(l.startswith('held ') for l in lines), lines)

    def test_a_head_with_a_green_required_check_is_never_reworded(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.held_for_naming(old)
        tree_green.remember(self.state_dir, self.PR, old,
                            tree_green.tree_at(self.origin, old), ['gate'])
        lines = self.one_pass()
        self.assertEqual(self.tip(), old)
        self.assertTrue(any(l.startswith(f'reword {self.B}: deferred') and 'green' in l
                            for l in lines), lines)

    def test_a_branch_with_a_run_in_flight_is_never_reworded(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.held_for_naming(old)
        with mock.patch.object(ci_flight, 'run_in_flight',
                               return_value={'id': 4242, 'status': 'in_progress'}):
            lines = self.one_pass()
        self.assertEqual(self.tip(), old)
        self.assertIn(ci_flight.DEFER_FMT.format(what='reword', branch=self.B, run=4242),
                      ' '.join(lines))

    def test_with_none_of_the_three_the_reword_still_happens(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.held_for_naming(old)
        lines = self.one_pass()
        self.assertIn(f'reword {self.B}: 1 subjects, trees identical — pushed', lines)
        self.assertNotEqual(self.tip(), old)


class GreenCarries(QueueRepo):
    """An ``asf land`` head whose tree is an earlier green head's is green for the merge queue."""

    B = 'worker/T-0001'
    PR = 1

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane(self.B, {'a.txt': 'a\n'}, 'feat(T-0001): a')
        merge_queue.add_request(self.state_dir, self.PR, self.B, priority=True)

    def reword(self):
        """A reword of the branch's one commit: a new head, byte-identical tree."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, f'origin/{self.B}'], cwd=self.worker)
        sh(['git', 'commit', '-q', '--amend', '-m', 'feat(T-0001): a, reworded'], cwd=self.worker,
           env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        return self.heads()[self.B]

    def ready(self):
        ln = self.lane()
        heads = self.heads()
        return merge_queue.requested_ready(ln, heads, heads['main']), self.lines

    def test_a_green_head_is_remembered_with_its_tree(self):
        old = self.heads()[self.B]
        self.green(old)
        out, _lines = self.ready()
        self.assertEqual([e['head'] for e in out], [old])
        rec = tree_green.green_on(self.state_dir, self.PR, old)
        self.assertEqual(rec['tree'], tree_green.tree_at(self.origin, old))
        self.assertEqual(rec['passed'], ['gate', 'gate-tests'])

    def test_green_is_carried_to_a_reworded_head_with_an_identical_tree(self):
        old = self.heads()[self.B]
        self.green(old)
        self.ready()                      # the green read, remembered
        new = self.reword()               # no checks at all on the new head
        self.assertNotEqual(new, old)
        self.assertEqual(tree_green.tree_at(self.origin, new),
                         tree_green.tree_at(self.origin, old))
        out, lines = self.ready()
        self.assertEqual([e['head'] for e in out], [new], lines)
        self.assertTrue(any(tree_green.CARRIED_FMT.format(old=old[:12]) in l for l in lines),
                        lines)

    def test_a_head_whose_tree_differs_carries_no_green(self):
        old = self.heads()[self.B]
        self.green(old)
        self.ready()
        # a real content change, not a reword: its own CI must run
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, f'origin/{self.B}'], cwd=self.worker)
        self.write(self.worker, 'a.txt', 'different\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-q', '--amend', '-m', 'feat(T-0001): a'], cwd=self.worker,
           env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        new = self.heads()[self.B]
        self.assertNotEqual(tree_green.tree_at(self.origin, new),
                            tree_green.tree_at(self.origin, old))
        out, lines = self.ready()
        self.assertEqual(out, [], lines)
        self.assertTrue(any(f'pending at {new[:12]}' in l for l in lines), lines)
        self.assertFalse(any('identical tree' in l for l in lines), lines)


class RewordReadsTheHead(QueueRepo):
    """B-0275 review C1 — the green guard asks the head's own checks when nothing was written
    down, and writes the answer down.

    ``tree_green.green_on`` is a read of this lane's memory, and no writer of that memory is
    reachable while a naming correction is pending: the gate writes it
    (:meth:`asf.harvest.lane.GitHubHost.check_gate`) and the correction keeps the branch from
    reaching the gate. So on the pass right after a run completes green — the very pass the
    in-flight guard hands the reword to — the memory is empty and the reword would push that
    green away. The guard reads the head instead."""

    B = 'worker/T-0001'
    PR = 11

    def setUp(self):
        super().setUp()
        self.push_lane(self.B, {'a.txt': 'a\n'}, 'tidy up')     # a subject naming no item

    def held(self, *checks, pr_head=None):
        """``reword_held``'s answer for the branch as the pass hands it over, with ``checks``
        what ``gh pr checks`` reports on its head now — nothing in flight, nothing remembered.

        ``pr_head``: the PR's own ``headRefOid`` when that is a sha other than the branch head
        (:func:`asf.harvest.lane.exact_head` is ``None`` there); by default the PR is at the
        branch head, as it is on every pass that is not racing a push. The ``gh`` mock is kept on
        ``self.gh``: what the guard read is as much the assertion as what it answered."""
        ln = self.lane()
        pr = {'number': self.PR, 'state': 'OPEN'}
        if pr_head:
            pr['head'] = pr_head
        f = {'branch': self.B, 'head': self.heads()[self.B], 'kind': 'code', 'item': 'T-0001',
             'class': lane.CODE, 'files': ['a.txt'],
             'pr': pr, 'prev': {'pr': self.PR}}
        with mock.patch.object(ci_flight, 'run_in_flight', return_value=None), \
                mock.patch.object(harvest, '_gh',
                                  return_value=(0, json.dumps(list(checks)), '')) as gh:
            self.gh = gh
            return ln.reword_held(f)

    def test_a_run_that_completed_green_defers_the_reword_though_nothing_was_remembered(self):
        head = self.heads()[self.B]
        self.assertIsNone(tree_green.green_on(self.state_dir, self.PR, head))
        why = self.held({'name': 'gate', 'bucket': 'pass'},
                        {'name': 'gate-tests', 'bucket': 'pass'})
        self.assertTrue(why.startswith(f'reword {self.B}: deferred'), why)
        self.assertIn(f'green on {head[:9]}', why)
        self.assertIn('gate, gate-tests', why)

    def test_the_green_it_read_is_written_down_so_a_later_rewrite_carries_it(self):
        head = self.heads()[self.B]
        self.held({'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'pass'})
        rec = tree_green.green_on(self.state_dir, self.PR, head)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['tree'], tree_green.tree_at(self.origin, head))
        self.assertEqual(rec['passed'], ['gate', 'gate-tests'])
        # and so the reword, when it finally happens, costs that green nothing
        self.assertIsNotNone(tree_green.carried(self.state_dir, self.PR, 'b' * 40,
                                                tree_green.tree_at(self.origin, head),
                                                ['gate', 'gate-tests']))

    def test_a_pr_whose_head_is_another_sha_is_not_read_and_nothing_is_written_down(self):
        """Round 3's C1 — a green belongs to the sha that earned it.

        ``gh pr checks`` reports the checks of the head the **PR** is at. When that is not the
        branch head, judging them green and writing the answer down against the branch head mints
        a green for content that never ran them, and :func:`asf.tree_green.carried` then hands it
        to every later head of the PR carrying that tree — a false green on the landing path. The
        gate pins this read to :func:`asf.harvest.lane.exact_head` (:meth:`check_gate`), and so
        does the guard: it reads nothing at all rather than read the wrong sha."""
        head = self.heads()[self.B]
        self.assertIsNone(lane.exact_head({'head': head, 'pr': {'head': 'c' * 40}}))
        self.assertEqual(self.held({'name': 'gate', 'bucket': 'pass'},
                                   {'name': 'gate-tests', 'bucket': 'pass'},
                                   pr_head='c' * 40), '')
        self.assertEqual(self.gh.call_args_list, [])     # not judged, because not even read
        self.assertIsNone(tree_green.green_on(self.state_dir, self.PR, head))
        # and so no later head of this PR with that tree reads green off it either
        self.assertIsNone(tree_green.carried(self.state_dir, self.PR, 'd' * 40,
                                             tree_green.tree_at(self.origin, head),
                                             ['gate', 'gate-tests']))

    def test_a_required_check_still_running_defers_nothing(self):
        # no green to throw away: the reword happens, exactly as it did before this guard
        self.assertEqual(self.held({'name': 'gate', 'bucket': 'pass'},
                                   {'name': 'gate-tests', 'bucket': 'pending'}), '')

    def test_a_head_with_no_checks_at_all_defers_nothing(self):
        # ``pr_checks`` reads "no checks reported" as green; there is no green there to lose,
        # and nothing is written down for a head that earned none
        self.assertEqual(self.held(), '')
        self.assertIsNone(tree_green.green_on(self.state_dir, self.PR, self.heads()[self.B]))

    def test_without_a_pr_host_the_guard_reads_nothing(self):
        # a fast-forward product has no PR, no checks and no host to ask: unchanged behaviour
        ln = lane.Lane(self.product(landing='fast-forward'), self.state_dir,
                       out=lambda *_: None, items={})
        f = {'branch': self.B, 'head': self.heads()[self.B], 'kind': 'code', 'item': 'T-0001',
             'class': lane.CODE, 'files': ['a.txt'], 'prev': {'pr': self.PR}}
        with mock.patch.object(ci_flight, 'run_in_flight', return_value=None), \
                mock.patch.object(harvest, '_gh') as gh:
            self.assertEqual(ln.reword_held(f), '')
        self.assertEqual(gh.call_args_list, [])


class ConflictBeforeCarry(QueueRepo):
    """B-0275 review C2 — the carry is asked only of a head the conflicts-with-trunk guard let
    through.

    A PR that conflicts with the trunk gets no ``pull_request`` CI from GitHub at all, so its
    checks read as the same *pending* a carry answers. Granted above the guard, a carry made that
    guard unreachable and emitted a conflicting PR into the batch as green, silently."""

    B = 'mine/own-work'           # under no factory prefix: the guard's own case
    PR = 3

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane(self.B, {'a.txt': 'a\n'}, 'feat: my own work')
        merge_queue.add_request(self.state_dir, self.PR, self.B, priority=True)

    def reword(self):
        """A reword of the branch's one commit: a new head, byte-identical tree, no checks."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, f'origin/{self.B}'], cwd=self.worker)
        sh(['git', 'commit', '-q', '--amend', '-m', 'feat: my own work, reworded'],
           cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        return self.heads()[self.B]

    def ready(self, conflicts=False):
        ln = self.lane()
        heads = self.heads()
        with mock.patch.object(merge_queue, 'conflicts_with_trunk', return_value=conflicts):
            return merge_queue.requested_ready(ln, heads, heads['main']), self.lines

    def test_a_carry_never_admits_a_pr_that_conflicts_with_the_trunk(self):
        old = self.heads()[self.B]
        self.green(old)
        self.ready()                       # green, remembered
        new = self.reword()
        self.assertEqual(tree_green.tree_at(self.origin, new),
                         tree_green.tree_at(self.origin, old))
        out, lines = self.ready(conflicts=True)
        self.assertEqual(out, [], lines)
        self.assertTrue(any('conflicts with main — merge or rebase it' in l for l in lines),
                        lines)
        self.assertFalse(any('identical tree' in l for l in lines), lines)

    def test_with_no_conflict_the_same_head_carries_its_green(self):
        old = self.heads()[self.B]
        self.green(old)
        self.ready()
        new = self.reword()
        out, lines = self.ready(conflicts=False)
        self.assertEqual([e['head'] for e in out], [new], lines)
        self.assertTrue(any(tree_green.CARRIED_FMT.format(old=old[:12]) in l for l in lines),
                        lines)


class LandingGateCarries(LaneFixture):
    """The lane's own landing gate (:meth:`asf.harvest.lane.GitHubHost.check_gate`) carries the
    same green: a reworded head whose tree an earlier green head of this PR carried lands on its
    checks, with no rerun."""

    B = 'worker/T-0001'
    PR = 7

    def setUp(self):
        super().setUp()
        self.lines = []
        self.product_ = env.Product('sample', {
            'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
            'conventions': {'landing': 'pull-request', 'landing_checks': ['gate'],
                            'landing_checks_missing': 'wait',
                            'branch_prefixes': {'code': 'worker/'}}})
        runner = lane.Lane.__new__(lane.Lane)
        runner.product, runner.conv, runner.out = self.product_, self.product_.conventions, \
            self.lines.append
        runner.dry_run, runner.results, runner.now = False, {}, 1_800_000_000.0
        runner.state_dir, runner.repo, runner.trunk = self.state_dir, self.repo, 'main'
        self.runner = runner
        self.host = lane.GitHubHost(self.product_, None)
        self.host.lane = runner
        self.host.slug = 'o/p'

    def head(self):
        sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=self.repo)
        return sh(['git', 'rev-parse', self.B], cwd=self.origin).stdout.strip()

    def gate(self, *checks):
        """``check_gate`` with ``gh pr checks`` answering ``checks`` on the branch's head now."""
        f = {'branch': self.B, 'prev': {'state': lane.GATE, 'head': None, 'pr': self.PR,
                                        'at': '2027-01-15T08:00:00Z', 'reason': ''},
             'class': lane.CODE, 'head': self.head(), 'kind': 'code'}
        with mock.patch.object(harvest, '_gh', return_value=(0, json.dumps(list(checks)), '')), \
                mock.patch.object(lane.Lane, 'set', lambda s_, f_, st, r, result=None, **kw:
                                  s_.results.update({f_['branch']: (st, r)})), \
                mock.patch.object(lane, 'wait', lambda ln, f_, why, **kw:
                                  ln.results.update({f_['branch']: ('wait', why)})):
            return self.host.check_gate(f, self.PR, ['a.txt'])

    def reword(self):
        """A reword of the branch's one commit: a new head, byte-identical tree."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, f'origin/{self.B}'], cwd=self.worker)
        sh(['git', 'commit', '-q', '--amend', '-m', 'feat(T-0001): a, reworded'], cwd=self.worker,
           env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        return self.head()

    def test_a_reworded_head_lands_on_the_green_the_old_head_earned(self):
        self.push_lane(self.B, {'a.txt': 'a\n'}, 'feat(T-0001): a')
        old = self.head()
        self.assertEqual(self.gate({'name': 'gate', 'bucket': 'pass'}), 'ci')
        self.assertIsNotNone(tree_green.green_on(self.state_dir, self.PR, old))
        new = self.reword()
        self.assertNotEqual(new, old)
        # the new head's own check has not even started: the green is carried, not waited for
        self.lines.clear()
        self.assertEqual(self.gate({'name': 'gate', 'bucket': 'pending'}), 'ci')
        self.assertTrue(any(tree_green.CARRIED_FMT.format(old=old[:12]) in l
                            for l in self.lines), self.lines)

    def test_a_head_whose_tree_differs_waits_for_its_own_checks(self):
        self.push_lane(self.B, {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.assertEqual(self.gate({'name': 'gate', 'bucket': 'pass'}), 'ci')
        self.push_lane(self.B, {'a.txt': 'different\n'}, 'feat(T-0001): a again')
        self.lines.clear()
        self.assertIsNone(self.gate({'name': 'gate', 'bucket': 'pending'}))
        self.assertFalse(any('identical tree' in l for l in self.lines), self.lines)
        self.assertEqual(self.runner.results[self.B][0], 'wait')


class Carry(unittest.TestCase):
    """:func:`asf.tree_green.carried` itself: the same tree, the same PR, the required set."""

    def setUp(self):
        import shutil
        import tempfile
        self.state_dir = tempfile.mkdtemp(prefix='treegreen_')
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)

    def test_a_remembered_tree_carries_within_its_own_pr_only(self):
        tree_green.remember(self.state_dir, 7, 'a' * 40, 't' * 40, ['gate'])
        self.assertEqual(tree_green.carried(self.state_dir, 7, 'b' * 40, 't' * 40,
                                            ['gate'])['head'], 'a' * 40)
        self.assertIsNone(tree_green.carried(self.state_dir, 8, 'b' * 40, 't' * 40, ['gate']))

    def test_the_head_that_earned_the_green_is_not_a_carry(self):
        tree_green.remember(self.state_dir, 7, 'a' * 40, 't' * 40, ['gate'])
        self.assertIsNone(tree_green.carried(self.state_dir, 7, 'a' * 40, 't' * 40, ['gate']))

    def test_a_required_set_that_grew_carries_nothing(self):
        tree_green.remember(self.state_dir, 7, 'a' * 40, 't' * 40, ['gate'])
        self.assertIsNone(tree_green.carried(self.state_dir, 7, 'b' * 40, 't' * 40,
                                             ['gate', 'gate-tests']))

    def test_a_green_older_than_keep_s_carries_nothing(self):
        import time
        tree_green.remember(self.state_dir, 7, 'a' * 40, 't' * 40, ['gate'],
                            now=time.time() - tree_green.KEEP_S - 1)
        self.assertIsNone(tree_green.carried(self.state_dir, 7, 'b' * 40, 't' * 40, ['gate']))

    def test_an_unreadable_file_remembers_nothing(self):
        with open(tree_green.path(self.state_dir), 'w', encoding='utf-8') as fh:
            fh.write('{ not json')
        self.assertEqual(tree_green.load(self.state_dir), {})
        self.assertIsNone(tree_green.carried(self.state_dir, 7, 'b' * 40, 't' * 40))


if __name__ == '__main__':
    unittest.main()
