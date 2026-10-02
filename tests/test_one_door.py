"""One door to the trunk.

* ``asf land <pr>`` (:mod:`asf.merge_queue`): a PR no factory item made enters the merge queue,
  is cut into a batch only once its required checks are success on its exact head, and lands
  only when the batch sha is green — with the queue's subject and trailer on its merge commit.
* :mod:`asf.trunk_watch`: a trunk commit that did not come through the queue is flagged (a log
  line, a red ``asf doctor`` row, a red ``asf status`` row with sha, author and PR); a queue
  commit is not.
* :mod:`asf.runner_classes`: a required job whose ``runs-on`` reaches runners of two classes is
  red, so is a runner carrying a label of a provider other than its declared one.
"""
import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import ci_pool, env, merge_queue, runner_classes, trunk_watch
from asf.ci_pool import RunsOn, Runner
from asf.harvest import lane
from asf.views import status

from tests.test_lane import sh
from tests.test_merge_queue import QueueRepo, check_run


class AsfLand(QueueRepo):
    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('hotfix/fix-x', {'x.txt': 'x\n'}, 'hotfix: x')
        self.head = self.heads()['hotfix/fix-x']
        merge_queue.add_request(self.state_dir, 7, 'hotfix/fix-x')

    def test_asf_land_lands_a_hotfix_through_the_queue_only_on_green(self):
        # pending on its head: not cut
        self.gh.checks[self.head] = [check_run('gate'), check_run('gate-tests', None, 'in_progress')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.batches(), [])
        self.assertTrue(any('PR #7 pending' in l for l in self.lines), self.lines)
        # green on its exact head: cut into a batch, no lane record of its own
        self.green(self.head)
        ln = self.queue_pass(self.lane(), [])
        (batch,) = self.batches()
        self.assertEqual([(m['branch'], m.get('requested')) for m in batch['members']],
                         [('hotfix/fix-x', True)])
        self.assertEqual(ln.results, {'hotfix/fix-x': 'queued'})
        self.assertEqual(self.lane_of('hotfix/fix-x'), {})
        self.assertEqual(self.heads()['main'], batch['base'])        # nothing landed yet
        # the batch sha green: the trunk is that sha, the request is gone
        self.green(batch['sha'])
        self.gh.pr_state = {7: 'MERGED'}
        ln = self.queue_pass(self.lane(), [])
        self.assertEqual(self.heads()['main'], batch['sha'])
        self.assertEqual(ln.results, {'hotfix/fix-x': 'landed'})
        self.assertEqual(merge_queue.load_requests(self.state_dir), {})
        msg = sh(['git', 'log', '-1', '--format=%s%n%b', batch['sha']], cwd=self.origin).stdout
        subject, _nl, body = msg.partition('\n')
        self.assertTrue(subject.startswith('merge-queue: #7 (hotfix/fix-x @'), subject)
        self.assertIn(merge_queue.TRAILER, body)
        self.assertTrue(trunk_watch.is_queue_commit(subject, body))

    def test_a_red_head_is_marked_and_never_cut_until_it_moves(self):
        self.gh.checks[self.head] = [check_run('gate'), check_run('gate-tests', 'failure')]
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.batches(), [])
        req = merge_queue.load_requests(self.state_dir)['7']
        self.assertEqual(req['red']['head'], self.head)
        self.green(self.head)            # the same head is not judged again
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.batches(), [])
        self.assertEqual(self.backs, [])  # no session to send it back to

    def test_a_conflicting_pr_is_red_at_once_and_shown_in_queue_and_status(self):
        # a conflict means GitHub never starts its CI: no check runs at all
        self.gh.mergeable[7] = 'CONFLICTING'
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.batches(), [])
        req = merge_queue.load_requests(self.state_dir)['7']
        self.assertEqual(req['red']['kind'], 'conflict')
        self.assertEqual(req['red']['head'], self.head)
        self.assertEqual(req['red']['why'], 'conflicts with main — merge or rebase it')
        mine = [l for l in self.lines if 'PR #7' in l]
        self.assertEqual(len(mine), 1, self.lines)
        self.assertIn('conflicts with main — merge or rebase it', mine[0])
        self.assertEqual(self.backs, [])
        self.assertIn('asf land #7 (red: conflicts with main — merge or rebase it)',
                      trunk_watch.waiting(self.product()))
        self.assertEqual(status.land_red_cell(self.product()),
                         'RED #7 conflicts with main — merge or rebase it')
        # not judged again until the head moves
        self.queue_pass(self.lane(), [])
        self.assertEqual(len([l for l in self.lines if 'PR #7' in l]), 0)

    def test_a_pending_pr_that_does_not_conflict_is_not_marked(self):
        self.gh.mergeable[7] = 'MERGEABLE'
        self.queue_pass(self.lane(), [])
        self.assertNotIn('red', merge_queue.load_requests(self.state_dir)['7'])
        self.assertIsNone(status.land_red_cell(self.product()))

    def test_a_conflicting_factory_branch_is_left_to_the_lane(self):
        self.push_lane('worker/T-0009', {'y.txt': 'y\n'}, 'feat: y')
        merge_queue.add_request(self.state_dir, 9, 'worker/T-0009')
        self.gh.mergeable[9] = 'CONFLICTING'
        self.queue_pass(self.lane(), [])
        self.assertNotIn('red', merge_queue.load_requests(self.state_dir)['9'])

    def test_a_land_request_and_a_factory_member_that_conflict_never_share_a_batch(self):
        self.push_lane('worker/T-0001', {'x.txt': 'factory x\n'}, 'feat: x')
        self.green(self.head)
        factory = self.entry('worker/T-0001', 1, files=('x.txt',))
        self.queue_pass(self.lane(), [factory])
        (batch,) = self.batches()
        # the factory member is first (ready list first); the land request waits on it
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001'])
        self.assertEqual(self.backs, [])
        self.assertTrue(any('waits on #1 (conflicting files: x.txt)' in l for l in self.lines),
                        self.lines)
        self.assertNotIn('red', merge_queue.load_requests(self.state_dir)['7'])

    def test_withdrawn_request_leaves_the_queue(self):
        self.assertTrue(merge_queue.drop_request(self.state_dir, 7))
        self.green(self.head)
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.batches(), [])

    def test_priority_request_goes_first_in_the_batch_and_the_view(self):
        self.push_lane('hotfix/fix-y', {'y.txt': 'y\n'}, 'hotfix: y')
        merge_queue.add_request(self.state_dir, 9, 'hotfix/fix-y', priority=True)
        self.assertEqual(merge_queue.load_requests(self.state_dir)['9']['priority'], True)
        self.assertNotIn('priority', merge_queue.load_requests(self.state_dir)['7'])
        self.assertEqual([r['pr'] for _k, r in merge_queue.ordered_requests(
            merge_queue.load_requests(self.state_dir))], [9, 7])
        self.assertEqual(trunk_watch.waiting(self.product), ['asf land #9 (priority)', 'asf land #7'])
        for b in ('hotfix/fix-x', 'hotfix/fix-y'):
            self.green(self.heads()[b])
        self.queue_pass(self.lane(), [])
        (batch,) = self.batches()
        self.assertEqual([m['pr'] for m in batch['members']], [9, 7])

    def test_a_priority_request_is_cut_ahead_of_a_full_chain_and_lands_first(self):
        mq = {'merge_queue': {'ref_prefix': 'batch/', 'batch_size': 3, 'inflight': 1}}
        self.green(self.head)
        self.queue_pass(self.lane(self.product(**mq)), [])
        (ahead,) = self.batches()                          # #7 fills the one in-flight slot
        self.push_lane('hotfix/fix-y', {'y.txt': 'y\n'}, 'hotfix: y')
        merge_queue.add_request(self.state_dir, 9, 'hotfix/fix-y', priority=True)
        self.green(self.heads()['hotfix/fix-y'])
        trunk = self.heads()['main']
        self.queue_pass(self.lane(self.product(**mq)), [])
        first, second = self.batches()                     # cut, and put first in the chain
        self.assertEqual([m['pr'] for m in first['members']], [9])
        self.assertTrue(first['members'][0]['priority'])
        self.assertEqual((first['base'], first['base_ref']), (trunk, None))
        self.assertEqual(second['ref'], ahead['ref'])
        self.assertTrue(any('priority #9 cut ahead of 1 batch(es) in flight' in l
                            for l in self.lines), self.lines)
        # a second pass cuts nothing more past the limit: one priority batch at a time
        self.queue_pass(self.lane(self.product(**mq)), [])
        self.assertEqual(len(self.batches()), 2)
        # it lands first; the batch it jumped is cut again on the new tip
        self.green(first['sha'])
        self.gh.pr_state = {9: 'MERGED'}
        self.queue_pass(self.lane(self.product(**mq)), [])
        self.assertEqual(self.heads()['main'], first['sha'])
        (again,) = self.batches()
        self.assertEqual([m['pr'] for m in again['members']], [7])
        self.assertEqual(again['base'], first['sha'])

    def test_land_priority_flag_is_parsed_and_persisted(self):
        from asf import cli
        p = cli.build_parser()
        for flag in ('--priority', '--front'):
            self.assertTrue(p.parse_args(['land', '7', flag]).priority)
        self.assertFalse(p.parse_args(['land', '7']).priority)

    def test_land_is_a_cli_command_with_help(self):
        from asf import cli
        p = cli.build_parser() if hasattr(cli, 'build_parser') else None
        if p is None:
            self.skipTest('no parser builder')
        args = p.parse_args(['land', '7', '--product', 'sample'])
        self.assertIs(args.func, merge_queue.cmd_land)
        self.assertIn('merge queue', merge_queue.LAND_DESCRIPTION)


class TrunkWatch(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='trunkwatch_')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        self.state_dir = os.path.join(self.base, 'state')
        self.ident = dict(os.environ, GIT_AUTHOR_NAME='Ann Author', GIT_AUTHOR_EMAIL='a@x',
                          GIT_COMMITTER_NAME='Ann Author', GIT_COMMITTER_EMAIL='a@x')
        sh(['git', 'init', '-q', '--bare', '-b', 'main', self.origin])
        sh(['git', 'clone', '-q', self.origin, self.repo])
        self.commit('init.txt', 'init')
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        p = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir)
        p.start()
        self.addCleanup(p.stop)
        # the ruleset read: none active unless a test says so (never the real host)
        self.ruleset = None
        rs = mock.patch.object(trunk_watch, 'ruleset_since', lambda *_a, **_k: (
            (self.ruleset, 42) if self.ruleset is not None else (None, None)))
        rs.start()
        self.addCleanup(rs.stop)
        self.product = env.Product('sample', {
            'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
            'conventions': {'landing': 'pull-request', 'merge': 'queue'}})

    def commit(self, rel, subject):
        with open(os.path.join(self.repo, rel), 'w', encoding='utf-8') as f:
            f.write(subject + '\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', subject], cwd=self.repo, env_=self.ident)

    def merge(self, branch, rel, message):
        sh(['git', 'checkout', '-q', '-b', branch], cwd=self.repo)
        self.commit(rel, f'work on {branch}')
        sh(['git', 'checkout', '-q', 'main'], cwd=self.repo)
        sh(['git', 'merge', '-q', '--no-ff', *message, branch], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return sh(['git', 'rev-parse', 'HEAD'], cwd=self.repo).stdout.strip()

    def test_a_bypass_commit_is_flagged_and_a_queue_commit_is_not(self):
        queued = self.merge('worker/T-1', 'a.txt',
                            ['-m', 'merge-queue: #1 (worker/T-1 @ abc)', '-m', merge_queue.TRAILER])
        bypass = self.merge('hotfix/x', 'b.txt',
                            ['-m', 'Merge pull request #9 from o/hotfix/x'])
        lines = []
        got = trunk_watch.tick(self.product, out=lines.append)
        self.assertEqual([b['sha'] for b in got], [bypass])
        self.assertNotIn(queued, [b['sha'] for b in got])
        self.assertEqual((got[0]['author'], got[0]['pr']), ('Ann Author', 9))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(bypass[:9], lines[0])
        self.assertIn('PR #9', lines[0])
        # a second look finds nothing new: one log line per commit, ever
        lines.clear()
        trunk_watch.tick(self.product, out=lines.append)
        self.assertEqual(lines, [])
        rows = trunk_watch.doctor_rows(self.product)
        self.assertEqual(len(rows), 1)
        required, ok, detail = rows[0]
        self.assertEqual((required, ok), (True, False))
        for part in (bypass[:9], 'Ann Author', 'PR #9'):
            self.assertIn(part, detail)
        cell = status.bypass_cell(self.product)
        self.assertTrue(cell.startswith('RED 1 commit(s)'), cell)
        self.assertIn(bypass[:9], cell)

    def test_only_queue_commits_is_green_everywhere(self):
        self.merge('worker/T-1', 'a.txt', ['-m', 'merge-queue: #1 (worker/T-1 @ abc)'])
        self.assertEqual(trunk_watch.tick(self.product, out=lambda _l: None), [])
        ((required, ok, _detail),) = trunk_watch.doctor_rows(self.product)
        self.assertEqual((required, ok), (True, True))
        self.assertIsNone(status.bypass_cell(self.product))

    def commit_at(self, branch, rel, message, when):
        """A merge on ``main`` dated ``when`` (epoch seconds)."""
        stamp = f'@{int(when)} +0000'
        prev = dict(self.ident)
        self.ident.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        try:
            return self.merge(branch, rel, message)
        finally:
            self.ident = prev

    def test_bypasses_before_the_ruleset_was_active_never_count(self):
        """2026-10-01, a product: 14 merges from before the trunk ruleset kept the bypass row
        red for days after the ruleset made a bypass impossible."""
        import time as _t
        now = _t.time()
        self.merge('worker/T-1', 'a.txt', ['-m', 'merge-queue: #1 (worker/T-1 @ abc)'])
        old = self.commit_at('hotfix/old', 'b.txt', ['-m', 'Merge pull request #8 from o/x'],
                             now - 7200)
        new = self.commit_at('hotfix/new', 'c.txt', ['-m', 'Merge pull request #9 from o/y'],
                             now - 600)
        self.ruleset = int(now - 3600)                   # active an hour ago
        lines = []
        got = trunk_watch.tick(self.product, out=lines.append, now=now)
        self.assertEqual([b['sha'] for b in got], [new])
        self.assertNotIn(old, ' '.join(lines))
        self.assertIn(new[:9], ' '.join(lines))
        cell = status.bypass_cell(self.product)
        self.assertTrue(cell.startswith('RED 1 commit(s)'), cell)
        self.assertNotIn(old[:9], cell)

    def test_merge_queue_watch_since_overrides_the_ruleset(self):
        import time as _t
        now = _t.time()
        self.merge('worker/T-1', 'a.txt', ['-m', 'merge-queue: #1 (worker/T-1 @ abc)'])
        self.commit_at('hotfix/old', 'b.txt', ['-m', 'Merge pull request #8 from o/x'],
                       now - 7200)
        since = datetime.datetime.fromtimestamp(now - 3600, datetime.timezone.utc)
        product = env.Product('sample', {
            'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
            'conventions': {'landing': 'pull-request', 'merge': 'queue',
                            'merge_queue': {'watch_since': since.isoformat()}}})
        self.assertEqual(trunk_watch.tick(product, out=lambda _l: None, now=now), [])
        ((required, ok, detail),) = trunk_watch.doctor_rows(product, now=now)
        self.assertEqual((required, ok), (True, True))
        self.assertIn('merge_queue.watch_since', detail)
        self.assertIsNone(status.bypass_cell(product))

    def test_a_trunk_standing_still_while_landings_wait_is_a_red_stall(self):
        import time as _t
        now = _t.time()
        self.commit_at('worker/T-1', 'a.txt', ['-m', 'merge-queue: #1 (worker/T-1 @ abc)'],
                       now - 5 * 3600)
        # nothing waits: no alarm however old the tip
        lines = []
        trunk_watch.tick(self.product, out=lines.append, now=now)
        self.assertFalse([l for l in lines if 'STALL' in l], lines)
        self.assertIsNone(status.trunk_stall_cell(self.product))
        ((_r, ok, _d),) = trunk_watch.stall_rows(self.product, now=now)
        self.assertTrue(ok)
        # a batch in flight and an asf land request: 5h > 4h (the default) is red, every tick
        merge_queue.save(self.state_dir, {'batches': [
            {'ref': 'batch/x', 'sha': 'a' * 40, 'members': [{'branch': 'w', 'pr': 5}]}]})
        merge_queue.add_request(self.state_dir, 7, 'hotfix/y')
        for _ in range(2):
            lines.clear()
            trunk_watch.tick(self.product, out=lines.append, now=now)
            stall = [l for l in lines if l.startswith('trunk watch: STALL main has not moved')]
            self.assertEqual(len(stall), 1, lines)
        for part in ('5.0h', '> 4h', 'trunk_stall_hours', '2 landing(s)', 'batch/x (#5)',
                     'asf land #7'):
            self.assertIn(part, stall[0])
        cell = status.trunk_stall_cell(self.product)
        self.assertTrue(cell.startswith('RED main has not moved'), cell)
        from asf import doctor
        ((required, ok, detail),) = doctor.check_trunk_stall(self.product)
        self.assertEqual((required, ok), (True, False))
        self.assertIn('has not moved', detail)
        # a higher limit: quiet again
        product = env.Product('sample', {
            'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
            'conventions': {'landing': 'pull-request', 'merge': 'queue',
                            'ci': {'trunk_stall_hours': 6}}})
        self.assertIsNone(trunk_watch.stall(product, now))
        # the trunk moves: the stall clears
        self.merge('worker/T-2', 'b.txt', ['-m', 'merge-queue: #2 (worker/T-2 @ def)'])
        lines.clear()
        trunk_watch.tick(self.product, out=lines.append, now=now + 60)
        self.assertFalse([l for l in lines if 'STALL' in l], lines)
        self.assertIsNone(status.trunk_stall_cell(self.product))

    def test_a_product_not_on_the_queue_is_not_watched(self):
        product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                         'conventions': {'merge': 'auto'}})
        self.assertIsNone(trunk_watch.tick(product, out=lambda _l: None))
        self.assertEqual(trunk_watch.doctor_rows(product), [])


def runner(name, *labels):
    return Runner(name=name, online=True, labels=['self-hosted', 'linux', 'x64', *labels], id=name)


POOL = [ci_pool.PoolEntry(runner='h-1', provider='acme', role='heavy'),
        ci_pool.PoolEntry(runner='h-2', provider='acme', role='heavy'),
        ci_pool.PoolEntry(runner='c-1', provider='globex', role='heavy')]


class RunnerClasses(unittest.TestCase):
    def test_a_required_job_on_one_class_is_green(self):
        runners = [runner('h-1', 'heavy', 'acme-heavy'), runner('h-2', 'heavy', 'acme-heavy'),
                   runner('c-1', 'heavy', 'globex-heavy')]
        ros = [RunsOn('ci.yml', 'gate', frozenset({'self-hosted', 'acme-heavy'}))]
        rows = runner_classes.judge(['gate'], ros, POOL, runners, 'ci.yml')
        self.assertEqual(rows, [(True, '1 required job(s) on one runner class each: '
                                       'gate=acme/heavy')])

    def test_mixed_runner_classes_turn_red_naming_the_job(self):
        runners = [runner('h-1', 'heavy'), runner('h-2', 'heavy'), runner('c-1', 'heavy')]
        ros = [RunsOn('ci.yml', 'gate', frozenset({'self-hosted', 'heavy'}))]
        ((ok, detail),) = runner_classes.judge(['gate'], ros, POOL, runners, 'ci.yml')
        self.assertFalse(ok)
        for part in ('ci.yml:gate', '2 runner classes', 'acme/heavy: h-1, h-2',
                     'globex/heavy: c-1'):
            self.assertIn(part, detail)

    def test_a_mislabelled_runner_turns_red_naming_runner_and_label(self):
        runners = [runner('h-1', 'heavy', 'acme'), runner('h-2', 'heavy', 'acme'),
                   runner('c-1', 'heavy', 'acme')]       # a globex box labelled acme
        rows = runner_classes.judge([], [], POOL, runners)
        self.assertEqual(len(rows), 1, rows)
        ok, detail = rows[0]
        self.assertFalse(ok)
        self.assertIn('c-1', detail)
        self.assertIn("'acme'", detail)
        self.assertIn('globex', detail)

    def test_a_matrix_cell_check_resolves_to_its_job(self):
        ros = [RunsOn('ci.yml', 'p1-e2e', frozenset({'self-hosted', 'heavy'}))]
        self.assertEqual(runner_classes.job_for('p1-e2e-b', ros, 'ci.yml').job, 'p1-e2e')

    def test_from_json_format_runs_on_is_read_as_a_push_run(self):
        value = ("${{ fromJSON(format('[\"self-hosted\",\"{0}\"{1}]', vars.REQ || vars.HEAVY || "
                 "'heavy', github.event_name == 'pull_request' && ',\"class-pr\"' || '')) }}")
        self.assertEqual(runner_classes.format_labels(value, {'REQ': '', 'HEAVY': ''}),
                         frozenset({'self-hosted', 'heavy'}))
        self.assertEqual(runner_classes.format_labels(value, {'REQ': 'acme-heavy'}),
                         frozenset({'self-hosted', 'acme-heavy'}))
        self.assertIsNone(runner_classes.format_labels(value, {}))   # unknowable: not judged

    def test_doctor_rows_are_required_and_read_only(self):
        product = env.Product('sample', {'main': 'main', 'repo_slug': 'o/p', 'ci': {
            'workflow': 'ci.yml',
            'pool': [{'runner': 'h-1', 'provider': 'acme', 'role': 'heavy'},
                     {'runner': 'c-1', 'provider': 'globex', 'role': 'heavy'}]}})
        from tests.test_ci_pool import FakeBackend
        backend = FakeBackend([runner('h-1', 'heavy'), runner('c-1', 'heavy', 'acme')],
                              [RunsOn('ci.yml', 'gate', frozenset({'self-hosted', 'heavy'}))])
        rows = runner_classes.doctor_rows(product, backend=backend, required=['gate'])
        self.assertTrue(all(r[0] for r in rows))
        self.assertEqual([r[1] for r in rows], [False, False])
        self.assertEqual(backend.writes, [])


if __name__ == '__main__':
    unittest.main()
