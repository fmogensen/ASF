"""A required check red on the trunk itself (:mod:`asf.trunk_red`).

* the same check red (after triage) on two unrelated landings — no PR in common, neither diff
  touching a file the failing logs name — is trunk red, suspected: ``TRUNK RED: <check> (seen on
  #a, #b)`` in ``asf status`` and ``asf doctor``, no batch member blamed, no PR sent to a correct
  round;
* the stall alarm or a suspicion dispatches the trunk's full workflow on its tip once per tip (a
  ``workflow_dispatch`` run: never the attested skip); red there confirms it (one line, one S1 fix
  card through the intake, an open card with the same signature linked instead), green clears it;
* the safety net: a full run at least every ``ci.trunk_full_every_hours``.

Hermetic: ``gh`` is a fake source, git a local origin.
"""
import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from asf import doctor, env, flake, merge_queue, trunk_red, trunk_watch
from asf.harvest import harvest, lane
from asf.views import status

from tests.test_lane import sh
from tests.test_merge_queue import FakeGH, QueueRepo, check_run

#: ten minutes ahead of the fixture commits (the trunk watch dates a first-read tip by its commit)
NOW = int(time.time()) + 600


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


class FakeSource:
    """The start queue's ``gh`` door as :mod:`asf.trunk_red` calls it."""

    def __init__(self):
        self.calls, self.runs, self.view, self.refuse = [], [], None, None

    def gh_try(self, args):
        self.calls.append(list(args))
        if args[:2] == ['workflow', 'run']:
            return (None, self.refuse) if self.refuse else ('', '')
        if args[:2] == ['run', 'list']:
            ev = args[args.index('-e') + 1] if '-e' in args else None
            return json.dumps([r for r in self.runs if not ev or r.get('event') == ev]), ''
        if args[:2] == ['run', 'view']:
            return json.dumps(self.view), ''
        return None, 'unexpected'

    def dispatches(self):
        return [c for c in self.calls if c[:2] == ['workflow', 'run']]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='trunkred_')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        self.record = os.path.join(self.base, 'record')
        os.makedirs(self.record)
        self.state_dir = os.path.join(self.base, 'state')
        os.makedirs(self.state_dir)
        self.ident = dict(os.environ, GIT_AUTHOR_NAME='A', GIT_AUTHOR_EMAIL='a@x',
                          GIT_COMMITTER_NAME='A', GIT_COMMITTER_EMAIL='a@x')
        sh(['git', 'init', '-q', '--bare', '-b', 'main', self.origin])
        sh(['git', 'clone', '-q', self.origin, self.repo])
        with open(os.path.join(self.repo, 'init.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'init'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        self.tip = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        p = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir)
        p.start()
        self.addCleanup(p.stop)
        rs = mock.patch.object(trunk_watch, 'ruleset_since', lambda *_a, **_k: (None, None))
        rs.start()
        self.addCleanup(rs.stop)
        self.src = FakeSource()
        self.lines = []
        self.product = self.make()

    def make(self, **ci):
        return env.Product('sample', {
            'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
            'backlog_dir': self.record, 'ci': {'workflow': 'ci.yml'},
            'conventions': {'landing': 'pull-request', 'merge': 'queue', 'ci': ci}})

    def look(self, now=NOW, product=None):
        """One trunk watch tick (it records the tip, and runs the trunk red pass)."""
        self.lines.clear()
        trunk_watch.tick(product or self.product, out=self.lines.append, now=now, src=self.src)
        return self.lines

    def seen(self, key, prs, files, fail_files=(), check='p1-e2e', now=NOW):
        trunk_red.observe(self.product, [check], key, prs, 'h' + key.strip('#'), self.tip, files,
                          fail_files, now=now)


class Suspicion(Fixture):
    def setUp(self):
        super().setUp()
        self.look(product=self.make(trunk_full_every_hours=0))    # the tip is read; nothing due

    def test_one_landing_is_its_own_red(self):
        self.seen('#1030', [1030], ['scripts/a.mjs'], ['e2e/p1b-picker.spec.ts'])
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e']), {})
        self.assertIsNone(status.trunk_red_cell(self.product))

    def test_two_unrelated_landings_red_on_one_check_is_trunk_red(self):
        self.seen('#1030', [1030], ['scripts/a.mjs'], ['e2e/p1b-picker.spec.ts'], now=NOW + 1)
        self.seen('#1034', [1034], ['apps/web/lib/clock.ts'], ['e2e/p1b-picker.spec.ts'],
                  now=NOW + 2)
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e', 'gate']), {'p1-e2e': self.tip})
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e (shard 2)']),
                         {'p1-e2e (shard 2)': self.tip})
        cell = status.trunk_red_cell(self.product)
        self.assertTrue(cell.startswith('RED TRUNK RED: p1-e2e (seen on #1030, #1034)'), cell)
        self.assertIn('suspected', cell)
        ((required, ok, detail),) = doctor.check_trunk_red(self.product)
        self.assertEqual((required, ok), (True, False))
        self.assertTrue(detail.startswith('TRUNK RED: p1-e2e (seen on #1030, #1034)'), detail)

    def test_a_diff_that_touches_the_failing_file_is_not_unrelated(self):
        self.seen('#1030', [1030], ['scripts/a.mjs'], ['e2e/p1b-picker.spec.ts'])
        self.seen('#1034', [1034], ['e2e/p1b-picker.spec.ts'], ['e2e/p1b-picker.spec.ts'])
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e']), {})

    def test_a_shared_pr_is_not_unrelated(self):
        self.seen('batch b/1', [5, 6], ['a.txt'])
        self.seen('batch b/2', [6], ['a.txt'])
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e']), {})
        # no file named by either log: two diffs sharing a file are not unrelated either
        self.seen('#7', [7], ['a.txt'])
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e']), {})
        self.seen('#8', [8], ['z.txt'])
        self.assertIn('p1-e2e', trunk_red.held(self.product, ['p1-e2e']))

    def test_a_trunk_move_clears_it(self):
        self.seen('#1', [1], ['a.txt'], now=NOW - 10)
        self.seen('#2', [2], ['b.txt'], now=NOW - 5)
        self.assertIn('p1-e2e', trunk_red.held(self.product, ['p1-e2e'], now=NOW))
        with open(os.path.join(self.repo, 'n.txt'), 'w', encoding='utf-8') as f:
            f.write('n\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'merge-queue: #3 (w @ x)'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        self.look(now=NOW + 60, product=self.make(trunk_full_every_hours=0))
        self.assertEqual(trunk_red.held(self.product, ['p1-e2e'], now=NOW + 60), {})

    def test_not_on_the_queue_nothing_is_recorded(self):
        product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                         'conventions': {'merge': 'auto'}})
        trunk_red.observe(product, ['p1-e2e'], '#1', [1], 'h', self.tip, ['a'])
        self.assertEqual(trunk_red.load(self.state_dir)['seen'], [])
        self.assertEqual(trunk_red.held(product, ['p1-e2e']), {})


class BatchNotBlamed(QueueRepo):
    """A batch red (after triage) on a check one unrelated landing was already red on: the trunk's,
    so no member is blamed or split out — they wait — and the batch is recorded."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.push_lane('worker/T-0001', {'a1.txt': 'x\n'}, 'feat: a1')
        self.push_lane('worker/T-0002', {'b1.txt': 'b\n'}, 'feat: b1')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        tip = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        trunk_watch.save(self.state_dir, {'checked': tip, 'moved_at': 0})
        self.tip = tip

    def red_pass(self):
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1, 'T-0001', files=('a1.txt',)),
                                      self.entry('worker/T-0002', 2, 'T-0002', files=('b1.txt',))])
        (batch,) = self.batches()
        self.gh.checks[batch['sha']] = [check_run('gate', 'failure'), check_run('gate-tests')]
        with mock.patch.object(merge_queue.flake, 'triage', lambda *a, **k: (['gate'], [])):
            self.queue_pass(self.lane(), [])
        return batch

    def test_the_second_unrelated_red_blames_no_member(self):
        trunk_red.observe(self.product(), ['gate'], '#99', [99], 'h99', self.tip, ['zz.txt'],
                          now=time.time() - 60)
        batch = self.red_pass()
        self.assertEqual(self.backs, [])
        self.assertEqual(self.batches(), [])                # dropped, never split
        for b in ('worker/T-0001', 'worker/T-0002'):
            r = self.lane_of(b)
            self.assertEqual(r['state'], lane.WAITING)
            self.assertIn('red on main too', r['reason'])
        seen = trunk_red.load(self.state_dir)['seen']
        self.assertEqual([(s['key'], s['prs']) for s in seen],
                         [('#99', [99]), (f"batch {batch['ref']}", [1, 2])])
        self.assertEqual(sorted(seen[1]['files']), ['a1.txt', 'b1.txt'])

    def test_with_no_other_landing_red_the_batch_is_judged_as_before(self):
        self.red_pass()
        self.assertEqual(self.backs, [])
        self.assertEqual(len(self.batches()), 2)            # split in halves, as ever
        self.assertEqual(len(trunk_red.load(self.state_dir)['seen']), 1)


class LandRequest(QueueRepo):
    def test_an_asf_land_head_red_is_recorded_and_named_trunk_red(self):
        self.push_lane('hotfix/x', {'x.txt': 'x\n'}, 'hotfix: x')
        head = self.heads()['hotfix/x']
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        tip = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        trunk_watch.save(self.state_dir, {'checked': tip, 'moved_at': 0})
        trunk_red.observe(self.product(), ['gate-tests'], '#98', [98], 'h98', tip, ['q.txt'],
                          now=time.time() - 60)
        merge_queue.add_request(self.state_dir, 7, 'hotfix/x')
        self.gh.checks[head] = [check_run('gate'), check_run('gate-tests', 'failure')]
        self.queue_pass(self.lane(), [])
        red = merge_queue.load_requests(self.state_dir)['7']['red']
        self.assertIn('trunk red too (gate-tests)', red['why'])
        keys = [s['key'] for s in trunk_red.load(self.state_dir)['seen']]
        self.assertEqual(keys, ['#98', '#7'])
        self.assertIn('TRUNK RED: gate-tests (seen on #7, #98)', status.trunk_red_cell(self.product()))


class FactoryHead(unittest.TestCase):
    """A factory PR's head red after triage, on a check trunk red holds: no correct round."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state_dir = self.base
        p = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir)
        p.start()
        self.addCleanup(p.stop)
        self.product = env.Product('p', {'repo_slug': 'o/p', 'repo_dir': self.base, 'main': 'main',
                                         'conventions': {'landing': 'pull-request', 'merge': 'queue',
                                                         'landing_checks': ['gate', 'gate-tests']}})
        trunk_watch.save(self.state_dir, {'checked': 't' * 40, 'moved_at': 0})

    def head_red(self):
        host = lane.GitHubHost(self.product)
        lines = []
        host.lane = mock.Mock(state_dir=self.state_dir, repo=None, out=lines.append)
        f = {'branch': 'cloud/T-1', 'head': 'a' * 40, 'pr': {'number': 902, 'head': 'a' * 40},
             'class': lane.CODE, 'files': ['src/a.ts']}
        checks = [{'name': 'gate', 'bucket': 'pass', 'link': 'https://x/runs/1/job/11'},
                  {'name': 'gate-tests', 'bucket': 'fail', 'link': 'https://x/runs/1/job/12'}]
        with mock.patch.object(lane, 'pr_checks', return_value=('red', '', checks)), \
                mock.patch.object(host, 'merge_required', return_value=(('gate', 'gate-tests'), None)), \
                mock.patch.object(host, 'trunk_red', return_value={}), \
                mock.patch.object(flake, 'settle'), \
                mock.patch.object(flake, 'triage', return_value=(['gate-tests'], [])), \
                mock.patch.object(merge_queue, 'failure_findings', return_value=[]):
            return host.head_red(f, 902), lines

    def test_alone_it_goes_to_its_correct_round(self):
        red, _lines = self.head_red()
        self.assertEqual(red['names'], ['gate-tests'])
        (s,) = trunk_red.load(self.state_dir)['seen']
        self.assertEqual((s['key'], s['files']), ('#902', ['src/a.ts']))

    def test_after_an_unrelated_landing_red_on_it_no_correct_round(self):
        trunk_red.observe(self.product, ['gate-tests'], '#77', [77], 'h', 't' * 40, ['b.ts'],
                          now=time.time() - 60)
        red, lines = self.head_red()
        self.assertIsNone(red)
        self.assertTrue(any('trunk red (gate-tests)' in l for l in lines), lines)


class FullRun(Fixture):
    """The stall alarm or a suspicion dispatches the full run once per tip; its verdict confirms
    or clears."""

    def stall(self):
        """A trunk that has stood still 5h while an asf land request waits."""
        merge_queue.add_request(self.state_dir, 7, 'hotfix/y')
        self.look(now=NOW, product=self.make(trunk_full_every_hours=0))
        st = trunk_watch.load(self.state_dir)
        st['moved_at'] = NOW - 5 * 3600
        trunk_watch.save(self.state_dir, st)

    def completed(self, jobs):
        self.src.runs = [{'databaseId': 555, 'headSha': self.tip, 'status': 'completed',
                          'conclusion': 'failure', 'createdAt': iso(NOW + 5),
                          'url': 'https://x/runs/555', 'event': 'workflow_dispatch'}]
        self.src.view = {'status': 'completed', 'conclusion': 'failure',
                         'url': 'https://x/runs/555', 'jobs': jobs}

    def test_the_stall_alarm_dispatches_the_full_run_once_per_tip(self):
        self.stall()
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.assertEqual(self.src.dispatches(),
                         [['workflow', 'run', 'ci.yml', '--ref', 'main', '-R', 'o/p']])
        self.assertTrue(any('dispatched the full ci.yml run on main' in l and 'stall alarm' in l
                            for l in self.lines), self.lines)
        # judged green: never dispatched again on this tip, however long the stall
        self.src.runs = [{'databaseId': 555, 'headSha': self.tip, 'status': 'completed',
                          'createdAt': iso(NOW + 5), 'url': 'u', 'event': 'workflow_dispatch'}]
        self.src.view = {'status': 'completed', 'url': 'u',
                         'jobs': [{'name': 'p1-e2e', 'conclusion': 'success', 'url': 'j'}]}
        with mock.patch.object(trunk_red, '_required', return_value=('p1-e2e',)):
            for i in range(1, 4):
                self.look(now=NOW + i * trunk_red.READ_EVERY_S, product=product)
        self.assertEqual(len(self.src.dispatches()), 1)
        self.assertEqual(trunk_red.load(self.state_dir)['full']['state'], 'green')

    def test_a_refused_dispatch_is_told_and_asked_again(self):
        self.stall()
        self.src.refuse = 'HTTP 403: Resource not accessible'
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.assertTrue(any('refused — HTTP 403' in l for l in self.lines), self.lines)
        self.src.refuse = None
        self.look(now=NOW + 60, product=product)
        self.assertEqual(len(self.src.dispatches()), 1)       # not every tick
        self.look(now=NOW + trunk_red.LIST_EVERY_S + 1, product=product)
        self.assertEqual(len(self.src.dispatches()), 2)
        self.assertEqual(trunk_red.load(self.state_dir)['full']['state'], 'dispatched')

    def test_red_on_the_full_run_confirms_and_files_one_s1_card(self):
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.seen('#1030', [1030], ['scripts/a.mjs'], ['e2e/p1b-picker.spec.ts'], now=NOW + 1)
        self.seen('#1034', [1034], ['apps/x.ts'], ['e2e/p1b-picker.spec.ts'], now=NOW + 2)
        self.look(now=NOW + 3, product=product)              # suspected: dispatched
        (d,) = self.src.dispatches()
        self.assertTrue(any('trunk red suspected: p1-e2e (seen on #1030, #1034)' in l
                            for l in self.lines), self.lines)
        self.completed([{'name': 'p1-e2e', 'conclusion': 'failure', 'url': 'https://x/job/9'},
                        {'name': 'gate', 'conclusion': 'success', 'url': 'https://x/job/8'}])
        found = [{'name': 'p1-e2e', 'tests': ['e2e/p1b-picker.spec.ts:155 › picks a model'],
                  'step': 'Run e2e', 'lines': [f'line {i}' for i in range(40)], 'paths': []}]
        with mock.patch.object(trunk_red, '_required', return_value=('gate', 'p1-e2e')), \
                mock.patch.object(flake, 'triage', return_value=(['p1-e2e'], [])), \
                mock.patch.object(merge_queue, 'failure_findings', return_value=found):
            self.look(now=NOW + trunk_red.READ_EVERY_S + 3, product=product)
            confirm = [l for l in self.lines if 'TRUNK RED p1-e2e on main' in l]
            self.assertEqual(len(confirm), 1, self.lines)
            self.assertIn('https://x/runs/555', confirm[0])
            self.assertIn('seen on #1030, #1034', confirm[0])
            intake = os.path.join(self.record, product.conventions.intake_dir)
            (name,) = os.listdir(intake)
            self.assertIn(name, confirm[0])
            with open(os.path.join(intake, name), encoding='utf-8') as fh:
                card = fh.read()
            for want in ('# Trunk red: p1-e2e fails on main @ ' + self.tip[:9], 'type: bug',
                         'severity: S1', 'signature: trunk-red p1-e2e',
                         'e2e/p1b-picker.spec.ts:155 › picks a model', 'step `Run e2e`',
                         'https://x/job/9', f'first red sha: {self.tip}', 'line 39',
                         '## Acceptance'):
                self.assertIn(want, card)
            self.assertNotIn('line 9\n', card)               # the tail only
            # judged once: a later tick files nothing more, dispatches nothing more
            for i in range(2, 5):
                self.look(now=NOW + i * trunk_red.READ_EVERY_S + 3, product=product)
        self.assertEqual(len(os.listdir(intake)), 1)
        self.assertEqual(len(self.src.dispatches()), 1)
        cell = status.trunk_red_cell(product)
        self.assertTrue(cell.startswith('RED TRUNK RED: p1-e2e (seen on #1030, #1034) — confirmed'),
                        cell)
        self.assertIn(name, cell)
        self.assertEqual(trunk_red.held(product, ['p1-e2e']), {'p1-e2e': self.tip})

    def test_an_open_fix_already_filed_is_linked_not_filed_twice(self):
        intake = os.path.join(self.record, self.product.conventions.intake_dir)
        os.makedirs(intake)
        with open(os.path.join(intake, 'p1b-picker-hotfix.md'), 'w', encoding='utf-8') as fh:
            fh.write('# p1b-picker hotfix\ntype: bug\nsignature: trunk-red p1-e2e\n\nbody\n')
        self.stall()
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.completed([{'name': 'p1-e2e', 'conclusion': 'failure', 'url': 'https://x/job/9'}])
        with mock.patch.object(trunk_red, '_required', return_value=('p1-e2e',)), \
                mock.patch.object(flake, 'triage', return_value=(['p1-e2e'], [])), \
                mock.patch.object(merge_queue, 'failure_findings', return_value=[]):
            self.look(now=NOW + trunk_red.READ_EVERY_S, product=product)
        self.assertEqual(os.listdir(intake), ['p1b-picker-hotfix.md'])
        self.assertTrue(any('TRUNK RED p1-e2e' in l and 'p1b-picker-hotfix.md' in l
                            for l in self.lines), self.lines)

    def test_green_on_the_full_run_clears_the_suspicion(self):
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.seen('#1', [1], ['a.txt'], now=NOW + 1)
        self.seen('#2', [2], ['b.txt'], now=NOW + 2)
        self.look(now=NOW + 3, product=product)
        self.assertIn('p1-e2e', trunk_red.held(product, ['p1-e2e'], now=NOW + 3))
        self.completed([{'name': 'p1-e2e', 'conclusion': 'success', 'url': 'j'}])
        with mock.patch.object(trunk_red, '_required', return_value=('p1-e2e',)):
            self.look(now=NOW + trunk_red.READ_EVERY_S + 3, product=product)
        self.assertTrue(any("landings' reds are their own" in l for l in self.lines), self.lines)
        self.assertEqual(trunk_red.held(product, ['p1-e2e'], now=NOW + 400), {})
        self.assertIsNone(status.trunk_red_cell(product))
        # a later red landing on this tip is its own again: no new suspicion, no new dispatch
        self.seen('#3', [3], ['c.txt'], now=NOW + 500)
        self.assertEqual(trunk_red.held(product, ['p1-e2e'], now=NOW + 501), {})
        self.look(now=NOW + 2 * trunk_red.READ_EVERY_S + 3, product=product)
        self.assertEqual(len(self.src.dispatches()), 1)

    def test_a_flaky_red_is_re_run_before_any_verdict(self):
        self.stall()
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.completed([{'name': 'p1-e2e', 'conclusion': 'failure', 'url': 'https://x/job/9'}])
        with mock.patch.object(trunk_red, '_required', return_value=('p1-e2e',)), \
                mock.patch.object(flake, 'triage', return_value=([], ['p1-e2e'])):
            self.look(now=NOW + trunk_red.READ_EVERY_S, product=product)
        self.assertEqual(trunk_red.load(self.state_dir)['full']['state'], 'running')
        self.assertIsNone(status.trunk_red_cell(product))


class SafetyNet(Fixture):
    def test_a_full_run_at_least_every_n_hours(self):
        # a scheduled full run 1h ago: nothing due
        self.src.runs = [{'event': 'schedule', 'createdAt': iso(NOW - 3600), 'headSha': 'x'},
                         {'event': 'push', 'createdAt': iso(NOW - 60), 'headSha': 'y'}]
        self.look(now=NOW)
        self.assertEqual(self.src.dispatches(), [])
        self.assertEqual(trunk_red.load(self.state_dir)['full_at'], NOW - 3600)
        ((_r, ok, detail),) = doctor.check_trunk_red(self.product)
        self.assertTrue(ok)
        self.assertIn('every 6h', detail)
        # 6h after it: dispatched on the tip, the safety net named
        self.look(now=NOW - 3600 + 6 * 3600 + 1)
        self.assertEqual(len(self.src.dispatches()), 1)
        self.assertTrue(any('safety net' in l and 'trunk_full_every_hours' in l
                            for l in self.lines), self.lines)

    def test_none_on_record_dispatches_at_once_and_the_setting_turns_it_off(self):
        self.look(now=NOW, product=self.make(trunk_full_every_hours=0))
        self.assertEqual(self.src.dispatches(), [])
        self.look(now=NOW, product=self.make(trunk_full_every_hours=3))
        self.assertEqual(len(self.src.dispatches()), 1)

    def test_no_workflow_named_is_no_dispatch(self):
        product = env.Product('sample', {'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
                                         'conventions': {'landing': 'pull-request', 'merge': 'queue'}})
        self.look(now=NOW, product=product)
        self.assertEqual(self.src.calls, [])



class RunsGH(FakeGH):
    """The queue's ``gh`` plus the workflow runs a check's link names, and close/reopen."""

    def __init__(self):
        super().__init__()
        self.wf_runs = {}     # run id -> {event, created_at}

    def __call__(self, args):
        if args[0] == 'api' and '/actions/runs/' in args[1] and '/job' not in args[1]:
            self.calls.append(list(args))
            rid = args[1].rsplit('/', 1)[1]
            return (0, json.dumps(self.wf_runs[rid]), '') if rid in self.wf_runs else (1, '', '404')
        if args[:2] == ['pr', 'reopen']:
            self.calls.append(list(args))
            self.pr_state[int(args[2])] = 'OPEN'
            return 0, '', ''
        return super().__call__(args)

    def of(self, verb):
        return [c for c in self.calls if c[:2] == ['pr', verb]]


def red_run(name, rid, conclusion='failure', started=None):
    return {'name': name, 'status': 'completed', 'conclusion': conclusion,
            'html_url': f'https://github.com/o/p/actions/runs/{rid}/job/{rid}0',
            **({'started_at': started} if started else {})}


class StaleMergeRef(QueueRepo):
    """2026-10-02: two hotfix PRs red ~12 h on a check the trunk had fixed — their runs' merge
    refs predated the fix, and re-runs replay the old ref. A red on a merge ref from before the
    trunk moved is never kept red and never re-run: the PR is reopened for a fresh run, and only a
    red on that fresh ref counts."""

    def setUp(self):
        super().setUp()
        from asf import stale_ref
        stale_ref._RUNS.clear()
        self.addCleanup(stale_ref._RUNS.clear)
        self.gh = RunsGH()
        patch = mock.patch.object(harvest, '_gh', side_effect=self.gh)
        patch.start()
        self.addCleanup(patch.stop)
        self.push_lane('hotfix/clock', {'x.txt': 'x\n'}, 'hotfix: clock')
        self.head = self.heads()['hotfix/clock']
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        self.tip = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        # the fix landed on the trunk an hour ago; the PR's run is from two hours ago
        self.moved = int(time.time()) - 3600
        trunk_watch.save(self.state_dir, {'checked': self.tip, 'moved_at': self.moved,
                                          'at': int(time.time())})
        self.gh.wf_runs['501'] = {'event': 'pull_request', 'created_at': iso(self.moved - 3600)}
        merge_queue.add_request(self.state_dir, 1034, 'hotfix/clock')

    def test_a_red_on_an_old_merge_ref_gets_a_fresh_run_never_a_re_run_or_a_red(self):
        self.gh.checks[self.head] = [check_run('gate'), red_run('gate-tests', 501)]
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.gh.of('close')), 1)
        self.assertEqual(self.gh.of('reopen'), [['pr', 'reopen', '1034', '-R', 'o/p']])
        self.assertIn('fresh run', self.gh.of('close')[0][-1])
        self.assertFalse([c for c in self.gh.calls if c[:2] == ['run', 'rerun']])
        self.assertNotIn('red', merge_queue.load_requests(self.state_dir)['1034'])
        self.assertTrue(any('PR #1034 pending' in l and 'merge ref' in l for l in self.lines),
                        self.lines)
        self.assertEqual(trunk_red.load(self.state_dir)['seen'], [])   # no trunk-red count
        # the fresh run has not shown yet: waits, never closed twice, never red
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.gh.of('close')), 1)
        self.assertNotIn('red', merge_queue.load_requests(self.state_dir)['1034'])
        # the fresh run (a new merge ref, created after the trunk moved) is green: it supersedes
        self.gh.wf_runs['502'] = {'event': 'pull_request', 'created_at': iso(int(time.time()))}
        self.gh.checks[self.head] = [check_run('gate'),
                                     red_run('gate-tests', 501, started='2026-10-02T09:49:00Z'),
                                     red_run('gate-tests', 502, 'success',
                                             started='2026-10-03T07:00:00Z')]
        self.queue_pass(self.lane(), [])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['hotfix/clock'])

    def test_red_again_on_the_fresh_merge_ref_is_its_own(self):
        self.gh.checks[self.head] = [check_run('gate'), red_run('gate-tests', 501)]
        self.queue_pass(self.lane(), [])
        self.gh.wf_runs['502'] = {'event': 'pull_request', 'created_at': iso(int(time.time()))}
        self.gh.checks[self.head] = [check_run('gate'),
                                     red_run('gate-tests', 501, started='2026-10-02T09:49:00Z'),
                                     red_run('gate-tests', 502, started='2026-10-03T07:00:00Z')]
        self.queue_pass(self.lane(), [])
        red = merge_queue.load_requests(self.state_dir)['1034']['red']
        self.assertEqual((red['head'], red['kind']), (self.head, 'checks'))
        self.assertEqual(len(self.gh.of('close')), 1)
        self.assertEqual([s['key'] for s in trunk_red.load(self.state_dir)['seen']], ['#1034'])

    def test_a_request_kept_red_from_before_the_trunk_moved_is_read_again(self):
        reqs = merge_queue.load_requests(self.state_dir)
        reqs['1034']['red'] = {'head': self.head, 'kind': 'checks', 'why': 'gate-tests (failure)',
                               'at': iso(self.moved - 1800)}
        merge_queue.save_requests(self.state_dir, reqs)
        self.gh.checks[self.head] = [check_run('gate'), red_run('gate-tests', 501)]
        # the stall reaction's first step: the fresh run, before the trunk is suspected
        self.assertIn('#1034', trunk_red.stale_first(self.product(), self.tip,
                                                     now=self.moved + 60))
        self.queue_pass(self.lane(), [])
        self.assertEqual(len(self.gh.of('close')), 1)
        self.assertNotIn('red', merge_queue.load_requests(self.state_dir)['1034'])

    def test_a_red_judged_after_the_trunk_moved_stays_red_until_the_head_moves(self):
        reqs = merge_queue.load_requests(self.state_dir)
        reqs['1034']['red'] = {'head': self.head, 'kind': 'checks', 'why': 'gate-tests (failure)',
                               'at': iso(int(time.time()) + 60)}    # after the tip arrived
        merge_queue.save_requests(self.state_dir, reqs)
        self.queue_pass(self.lane(), [])
        self.assertEqual(self.gh.of('close'), [])
        self.assertIn('red', merge_queue.load_requests(self.state_dir)['1034'])


class StaleFactoryHead(unittest.TestCase):
    """A factory PR red on an old merge ref: reopened for a fresh run — no flake re-run (it would
    replay the old ref), no correct round."""

    def setUp(self):
        from asf import stale_ref
        stale_ref._RUNS.clear()
        self.addCleanup(stale_ref._RUNS.clear)
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state_dir = os.path.join(self.base, 'state')
        os.makedirs(self.state_dir)
        p = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir)
        p.start()
        self.addCleanup(p.stop)
        origin, self.repo = os.path.join(self.base, 'o.git'), os.path.join(self.base, 'r')
        sh(['git', 'init', '-q', '--bare', '-b', 'main', origin])
        sh(['git', 'clone', '-q', origin, self.repo])
        ident = dict(os.environ, GIT_AUTHOR_NAME='A', GIT_AUTHOR_EMAIL='a@x',
                     GIT_COMMITTER_NAME='A', GIT_COMMITTER_EMAIL='a@x')
        sh(['git', 'commit', '-q', '--allow-empty', '-m', 'init'], cwd=self.repo, env_=ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        tip = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        self.moved = int(time.time()) - 600
        trunk_watch.save(self.state_dir, {'checked': tip, 'moved_at': self.moved})
        self.product = env.Product('p', {'repo_slug': 'o/p', 'repo_dir': self.repo, 'main': 'main',
                                         'conventions': {'landing': 'pull-request', 'merge': 'queue',
                                                         'landing_checks': ['gate', 'gate-tests']}})
        self.gh = RunsGH()

    def head_red(self, created):
        self.gh.wf_runs['7'] = {'event': 'pull_request', 'created_at': iso(created)}
        host = lane.GitHubHost(self.product)
        lines = []
        host.lane = mock.Mock(state_dir=self.state_dir, repo=self.repo, out=lines.append)
        f = {'branch': 'cloud/T-1', 'head': 'a' * 40, 'pr': {'number': 902, 'head': 'a' * 40},
             'class': lane.CODE, 'files': ['src/a.ts']}
        checks = [{'name': 'gate', 'bucket': 'pass', 'link': 'https://x/actions/runs/7/job/11'},
                  {'name': 'gate-tests', 'bucket': 'fail',
                   'link': 'https://x/actions/runs/7/job/12'}]
        with mock.patch.object(harvest, '_gh', side_effect=self.gh), \
                mock.patch.object(lane, 'pr_checks', return_value=('red', '', checks)), \
                mock.patch.object(host, 'merge_required', return_value=(('gate', 'gate-tests'), None)), \
                mock.patch.object(host, 'trunk_red', return_value={}), \
                mock.patch.object(flake, 'settle'), \
                mock.patch.object(flake, 'triage', return_value=(['gate-tests'], [])) as triage, \
                mock.patch.object(merge_queue, 'failure_findings', return_value=[]):
            return host.head_red(f, 902), lines, triage

    def test_an_old_merge_ref_is_reopened_never_triaged_or_corrected(self):
        red, lines, triage = self.head_red(self.moved - 3600)
        self.assertIsNone(red)
        triage.assert_not_called()
        self.assertEqual(len(self.gh.of('close')), 1)
        self.assertEqual(len(self.gh.of('reopen')), 1)
        self.assertTrue(any('stale' in l or 'merge ref' in l for l in lines), lines)

    def test_a_run_on_todays_trunk_is_judged_as_before(self):
        red, _lines, triage = self.head_red(int(time.time()) + 60)   # after the tip arrived
        self.assertEqual(red['names'], ['gate-tests'])
        triage.assert_called_once()
        self.assertEqual(self.gh.of('close'), [])


class StallWaitsForFreshRuns(Fixture):
    def test_a_pr_reopened_for_a_fresh_run_holds_the_full_run(self):
        from asf import stale_ref
        merge_queue.add_request(self.state_dir, 7, 'hotfix/y')
        self.look(now=NOW, product=self.make(trunk_full_every_hours=0))
        st = trunk_watch.load(self.state_dir)
        st['moved_at'] = NOW - 5 * 3600
        trunk_watch.save(self.state_dir, st)
        stale_ref.save(self.state_dir, {'7': {'head': 'h', 'tip': self.tip, 'at': NOW - 60}})
        product = self.make(trunk_full_every_hours=0)
        self.look(now=NOW, product=product)
        self.assertEqual(self.src.dispatches(), [])
        self.assertTrue(any('PR #7 red on a merge ref' in l and 'full run waits' in l
                            for l in self.lines), self.lines)
        # its fresh run had its time: the stall reaction goes on to the full run
        self.look(now=NOW + stale_ref.WAIT_S, product=product)
        self.assertEqual(len(self.src.dispatches()), 1)

if __name__ == '__main__':
    unittest.main()
