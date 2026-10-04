"""asf.dwell — the dwell-time watchdog: each watched state aged against its limit, the two safe
actions (re-run a cancelled check once per head, drop an ungrantable hold), and the alarms.
Hermetic: every host read is a fake :class:`asf.dwell.Facts`; git runs on local fixtures only."""
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import approvals, dwell, env, github
from asf.feeder import rows as feeder_rows
from asf.tick import step_wave

NOW = 1_800_000_000.0


def iso(epoch):
    return dwell._iso(epoch)


class FakeFacts(dwell.Facts):
    """No host: each read is what the test sets."""

    def __init__(self, product, now=NOW, **kw):
        super().__init__(product, root=None, now=now)
        self.kw = kw

    def tick(self):
        return self.kw.get('tick')

    def lane(self):
        return self.kw.get('lane', {})

    def queue_on(self):
        return self.kw.get('queue_on', True)

    def batches(self):
        return self.kw.get('batches', [])

    def requests(self):
        return self.kw.get('requests', {})

    def required(self):
        return self.kw.get('required', ('tests',))

    def open_prs(self):
        return self.kw.get('prs')

    def checks_at(self, sha):
        return self.kw.get('checks', {}).get(sha)

    def trunk_sha(self):
        return self.kw.get('trunk')

    def runners(self):
        return self.kw.get('runners')

    def checkouts(self):
        return self.kw.get('checkouts', [])

    def would_start(self):
        return self.kw.get('wave')

    def holds(self):
        return self.kw.get('holds', []) if 'holds' in self.kw else super().holds()


class DwellTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(env, 'ASF_HOME', self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.product = env.Product('p', {'main': 'main', 'repo_slug': 'o/r',
                                         'conventions': {'merge': 'queue'}})

    def found(self, facts, act=False):
        return dwell.check(self.product, facts=facts, act=act, out=lambda _l: None)

    def by_state(self, found, state):
        return [f for f in found if f.state == state]


class LimitsTests(DwellTestCase):
    def test_the_defaults_are_the_table(self):
        lim = dwell.limits(self.product)
        self.assertEqual(lim['tick_running'], 15)
        self.assertEqual(lim['pr_green_not_landing'], 30)
        self.assertEqual(lim['check_cancelled'], 10)
        self.assertEqual(lim['chain_no_cut'], 20)
        self.assertEqual(lim['ungrantable_hold'], 0)
        self.assertEqual(set(lim), set(dwell.NAMES))

    def test_a_product_sets_or_turns_off_each_limit(self):
        product = env.Product('p', {'conventions': {'watchdog': {
            'tick_running': 40, 'runner_offline': 'off', 'branch_no_pr': 'soon'}}})
        lim = dwell.limits(product)
        self.assertEqual(lim['tick_running'], 40)
        self.assertIsNone(lim['runner_offline'])
        self.assertEqual(lim['branch_no_pr'], 10)          # not minutes: the default stands

    def test_an_off_state_is_not_probed(self):
        product = env.Product('p', {'conventions': {'watchdog': {'runner_offline': 'off'}}})
        found = dwell.check(product, facts=FakeFacts(product, runners=[('r1', False)]),
                            out=lambda _l: None)
        self.assertEqual(self.by_state(found, 'runner_offline'), [])


class AgingTests(DwellTestCase):
    def test_a_fact_without_its_own_time_ages_from_the_first_pass_that_saw_it(self):
        runners = [('r1', False)]
        first = self.by_state(self.found(FakeFacts(self.product, runners=runners)),
                              'runner_offline')
        self.assertEqual(first[0].age_s, 0)
        self.assertFalse(first[0].breach)
        later = self.by_state(self.found(FakeFacts(self.product, now=NOW + 11 * 60,
                                                   runners=runners)), 'runner_offline')
        self.assertTrue(later[0].breach)
        self.assertIn('runner_offline r1 — 11 min, limit 10 min (owner ci host)', later[0].line())

    def test_a_state_that_ended_starts_again_from_zero(self):
        self.found(FakeFacts(self.product, runners=[('r1', False)]))
        self.found(FakeFacts(self.product, now=NOW + 60, runners=[('r1', True)]))
        again = self.by_state(self.found(FakeFacts(self.product, now=NOW + 20 * 60,
                                                   runners=[('r1', False)])), 'runner_offline')
        self.assertEqual(again[0].age_s, 0)

    def test_an_unreadable_probe_keeps_its_first_seen_times(self):
        self.found(FakeFacts(self.product, runners=[('r1', False)]))
        lines = []

        class Broken(FakeFacts):
            def runners(self):
                raise OSError('host down')
        dwell.check(self.product, facts=Broken(self.product, now=NOW + 60), out=lines.append)
        self.assertIn('watchdog: runner_offline unreadable (OSError: host down)', lines)
        later = self.by_state(self.found(FakeFacts(self.product, now=NOW + 12 * 60,
                                                   runners=[('r1', False)])), 'runner_offline')
        self.assertTrue(later[0].breach)


class ProbeTests(DwellTestCase):
    def test_a_tick_holding_the_lock_past_15_minutes(self):
        f = self.by_state(self.found(FakeFacts(self.product, tick=(NOW - 16 * 60, 4242))),
                          'tick_running')[0]
        self.assertTrue(f.breach)
        self.assertIn('tick pid 4242', f.detail)

    def test_the_tick_stamp_and_the_lock(self):
        from asf.tick import tick as tick_mod
        facts = dwell.Facts(self.product, now=NOW)
        self.assertIsNone(facts.tick())                  # no lock file: no tick
        dwell.mark_tick(self.product, now=NOW - 60)
        lock = tick_mod.acquire_lock(self.product)
        try:
            since, pid = dwell.Facts(self.product, now=NOW).tick()
        finally:
            lock.close()
        self.assertEqual((since, pid), (NOW - 60, os.getpid()))
        self.assertIsNone(dwell.Facts(self.product, now=NOW).tick())   # released

    def test_a_green_pr_not_landing_carries_the_lanes_reason(self):
        lane = {'worker/t-1': {'state': 'WAITING', 'head': 'abc1234567', 'pr': 7,
                               'green': {'head': 'abc1234567', 'trunk': 'x'},
                               'reason': 'merge queue: 2 batch(es) in flight'},
                'worker/t-2': {'state': 'WAITING', 'head': 'def', 'pr': 8,
                               'green': {'head': 'older'}},        # green for an older head
                'worker/t-3': {'state': 'QUEUED', 'head': 'aaa', 'pr': 9,
                               'green': {'head': 'aaa'}}}          # in a batch: watched there
        got = self.by_state(self.found(FakeFacts(self.product, now=NOW, lane=lane)),
                            'pr_green_not_landing')
        self.assertEqual([f.key for f in got], ['#7'])
        self.assertIn('WAITING: merge queue: 2 batch(es) in flight', got[0].detail)

    def test_a_batch_green_on_the_trunk_tip_behind_another(self):
        batches = [{'ref': 'batch/1', 'sha': 'p' * 40, 'base': 'T' * 40, 'members': []},
                   {'ref': 'batch/2', 'sha': 'g' * 40, 'base': 'T' * 40, 'members': []},
                   {'ref': 'batch/3', 'sha': 'h' * 40, 'base': 'g' * 40, 'members': []}]
        green = [{'name': 'tests', 'status': 'completed', 'conclusion': 'success'}]
        got = self.by_state(self.found(FakeFacts(
            self.product, batches=batches, trunk='T' * 40,
            checks={'g' * 40: green, 'h' * 40: green})), 'green_batch_blocked')
        self.assertEqual([f.key for f in got], ['batch/2'])     # batch/3 is stacked, not on tip
        self.assertIn('waits behind batch/1', got[0].detail)

    def test_a_pending_batch_on_the_tip_is_not_a_breach(self):
        batches = [{'ref': 'batch/1', 'sha': 'p' * 40, 'base': 'T' * 40, 'members': []},
                   {'ref': 'batch/2', 'sha': 'g' * 40, 'base': 'T' * 40, 'members': []}]
        pending = [{'name': 'tests', 'status': 'in_progress', 'conclusion': None}]
        got = self.by_state(self.found(FakeFacts(self.product, batches=batches, trunk='T' * 40,
                                                 checks={'g' * 40: pending})),
                            'green_batch_blocked')
        self.assertEqual(got, [])

    def test_green_prs_waiting_and_no_cut(self):
        lane = {'worker/t-1': {'state': 'WAITING', 'head': 'a', 'green': {'head': 'a'}}}
        batches = [{'ref': 'batch/1', 'sha': 's', 'cut_at': iso(NOW - 3600), 'members': []}]
        reqs = {'5': {'pr': 5, 'branch': 'ci/fix'}, '6': {'pr': 6, 'branch': 'x', 'red': {'head': 'h'}}}
        self.found(FakeFacts(self.product, lane=lane, batches=batches, requests=reqs))
        got = self.by_state(self.found(FakeFacts(self.product, now=NOW + 21 * 60, lane=lane,
                                                 batches=batches, requests=reqs)), 'chain_no_cut')
        self.assertTrue(got[0].breach)
        self.assertIn('2 green PR(s) wait (ci/fix, worker/t-1)', got[0].detail)
        # a new cut is a new wait: it starts from zero
        batches2 = batches + [{'ref': 'batch/2', 'sha': 't', 'cut_at': iso(NOW + 21 * 60),
                               'members': []}]
        again = self.by_state(self.found(FakeFacts(self.product, now=NOW + 22 * 60, lane=lane,
                                                   batches=batches2)), 'chain_no_cut')
        self.assertFalse(again[0].breach)

    def test_the_merge_queue_states_need_the_queue(self):
        product = env.Product('p', {'main': 'main'})
        lane = {'worker/t-1': {'state': 'WAITING', 'head': 'a', 'green': {'head': 'a'}}}
        found = dwell.check(product, facts=FakeFacts(product, queue_on=False, lane=lane),
                            out=lambda _l: None)
        self.assertEqual(self.by_state(found, 'chain_no_cut'), [])

    def test_a_launchable_row_with_a_free_seat_names_the_waves_reason(self):
        mk = lambda iid, action='would launch': feeder_rows.Row(
            tier=2, kind=feeder_rows.PLAN_CODE, item_id=iid, feature_id='F-1', action=action,
            brief_kind='task', branch='', reason='')
        screened = [step_wave.Screened(mk('T-1'), 'held touch_amendable_set (human-now)',
                                       step_wave.HELD),
                    step_wave.Screened(mk('T-2')),
                    step_wave.Screened(mk('T-3', 'WAITS ON T-1'), 'WAITS ON T-1',
                                       step_wave.WAITS)]
        got = self.by_state(self.found(FakeFacts(self.product, wave=(screened, 4, [{}]))),
                            'launchable_idle')
        self.assertEqual([f.key for f in got], ['T-1', 'T-2'])
        self.assertIn('held touch_amendable_set (human-now); 3 seat(s) free', got[0].detail)
        none_free = self.by_state(self.found(FakeFacts(self.product,
                                                       wave=(screened, 1, [{}]))),
                                  'launchable_idle')
        self.assertEqual(none_free, [])

    def test_a_pushed_branch_with_no_pr_ages_from_its_push(self):
        lane = {'worker/t-1': {'state': 'PUSHED', 'head': 'abc', 'at': iso(NOW - 11 * 60)},
                'worker/t-2': {'state': 'PR_OPEN', 'head': 'abc', 'pr': 3,
                               'at': iso(NOW - 99 * 60)}}
        got = self.by_state(self.found(FakeFacts(self.product, lane=lane)), 'branch_no_pr')
        self.assertEqual([f.key for f in got], ['worker/t-1'])
        self.assertTrue(got[0].breach)

    def test_a_record_checkout_behind_its_origin(self):
        origin = os.path.join(self.tmp, 'origin.git')
        clone = os.path.join(self.tmp, 'clone')
        other = os.path.join(self.tmp, 'other')

        def git(*a, cwd=None):
            subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True)
        git('init', '-q', '--bare', '-b', 'main', origin)
        git('clone', '-q', origin, clone)
        for d in (clone,):
            git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
                '-m', 'one', cwd=d)
            git('push', '-q', 'origin', 'HEAD:main', cwd=d)
            git('branch', '-q', '--set-upstream-to', 'origin/main', cwd=d)
        git('clone', '-q', origin, other)
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
            '-m', 'two', cwd=other)
        git('push', '-q', 'origin', 'HEAD:main', cwd=other)
        got = self.by_state(self.found(FakeFacts(self.product,
                                                 checkouts=[('backlog_dir', clone)])),
                            'record_behind')
        self.assertEqual(len(got), 1)
        self.assertIn('0 ahead, 1 behind origin/main', got[0].detail)


def pr(number, sha, checks):
    return {'number': number, 'headRefName': f'worker/t-{number}', 'headRefOid': sha,
            'isDraft': False, 'statusCheckRollup': checks}


def check(name, conclusion, status='COMPLETED', at=NOW - 15 * 60, run='991'):
    return {'name': name, 'status': status, 'conclusion': conclusion,
            'completedAt': iso(at),
            'detailsUrl': f'https://host/o/r/actions/runs/{run}/job/1'}


class CancelledCheckTests(DwellTestCase):
    def test_a_cancelled_required_check_is_rerun_once_per_head(self):
        prs = [pr(1, 'a' * 40, [check('tests', 'CANCELLED'), check('lint', 'SUCCESS')])]
        calls = []

        def gh(args, **_kw):
            calls.append(args)
            return github.Result(True, '')
        with mock.patch.object(github, 'gh', side_effect=gh):
            f = self.by_state(self.found(FakeFacts(self.product, prs=prs), act=True),
                              'check_cancelled')[0]
            self.assertTrue(f.breach)                   # aged from the check's own end
            self.assertEqual(calls, [['run', 'rerun', '991', '--failed', '-R', 'o/r']])
            self.assertIn('re-ran failed jobs of run(s) 991', f.action)
            again = self.by_state(self.found(FakeFacts(self.product, now=NOW + 60, prs=prs),
                                             act=True), 'check_cancelled')[0]
        self.assertEqual(len(calls), 1)                 # once per head: the second only alarms
        self.assertIn('needs a look', again.detail)

    def test_a_new_head_may_be_rerun_again(self):
        calls = []
        with mock.patch.object(github, 'gh',
                               side_effect=lambda a, **k: calls.append(a) or github.Result(True)):
            self.found(FakeFacts(self.product, prs=[pr(1, 'a' * 40,
                                                      [check('tests', 'TIMED_OUT')])]), act=True)
            self.found(FakeFacts(self.product, prs=[pr(1, 'b' * 40,
                                                      [check('tests', 'CANCELLED')])]), act=True)
        self.assertEqual(len(calls), 2)

    def test_nothing_is_rerun_while_a_check_still_runs_or_before_the_limit(self):
        running = [pr(1, 'a' * 40, [check('tests', 'CANCELLED'),
                                   check('e2e', None, status='IN_PROGRESS')])]
        fresh = [pr(2, 'b' * 40, [check('tests', 'CANCELLED', at=NOW - 60)])]
        with mock.patch.object(github, 'gh') as gh:
            found = self.found(FakeFacts(self.product, prs=running + fresh), act=True)
        gh.assert_not_called()
        got = self.by_state(found, 'check_cancelled')
        self.assertEqual([(f.key, f.breach) for f in got], [(f"#2@{'b' * 9}", False)])

    def test_a_check_the_landing_does_not_require_is_not_watched(self):
        prs = [pr(1, 'a' * 40, [check('optional-lint', 'CANCELLED')])]
        self.assertEqual(self.by_state(self.found(FakeFacts(self.product, prs=prs)),
                                       'check_cancelled'), [])

    def test_the_report_alone_never_reruns(self):
        prs = [pr(1, 'a' * 40, [check('tests', 'CANCELLED')])]
        with mock.patch.object(github, 'gh') as gh:
            f = self.by_state(self.found(FakeFacts(self.product, prs=prs), act=False),
                              'check_cancelled')[0]
        gh.assert_not_called()
        self.assertTrue(f.breach)
        self.assertEqual(f.action, '')


class UngrantableHoldTests(DwellTestCase):
    def test_an_ungrantable_hold_is_dropped_and_a_grantable_one_alarms_never(self):
        approvals.refuse(self.product, 'T-1', 'touch_amendable_set', 'human-now', 'job', 'Edit',
                         'writes the amendable set')
        found = self.found(FakeFacts(self.product), act=True)
        got = self.by_state(found, 'ungrantable_hold')
        self.assertEqual([f.key for f in got], ['T-1/touch_amendable_set'])
        self.assertEqual(got[0].action, 'dropped T-1/touch_amendable_set')
        self.assertEqual(approvals.open_holds(self.product), [])
        self.assertEqual(approvals.holds(self.product)['T-1/touch_amendable_set']['resolution'],
                         'dropped')

    def test_a_report_drops_nothing(self):
        approvals.refuse(self.product, 'T-1', 'touch_amendable_set', 'human-now', 'job', 'Edit',
                         'writes the amendable set')
        self.found(FakeFacts(self.product), act=False)
        self.assertEqual(len(approvals.open_holds(self.product)), 1)


class StepAndCliTests(DwellTestCase):
    def test_the_step_prints_each_breach_as_an_event_and_files_it(self):
        events, lines = [], []
        ctx = types.SimpleNamespace(product=self.product, record_root=lambda: self.tmp,
                                    event=lambda kind, **f: events.append((kind, f)))
        facts = FakeFacts(self.product, tick=(NOW - 20 * 60, 1), holds=[])
        with mock.patch.object(dwell, 'file_cards') as cards:
            rc = dwell.run_step(ctx, out=lines.append, facts=facts)
        self.assertEqual(rc, 0)
        self.assertTrue(any(l.startswith('watchdog: BREACH tick_running tick') for l in lines))
        self.assertEqual(events[0][0], 'watchdog')
        self.assertEqual(events[0][1]['state'], 'tick_running')
        self.assertEqual([f.state for f in cards.call_args[0][2]], ['tick_running'])

    def test_a_breach_is_one_bug_per_state_and_key(self):
        f = dwell.Finding('runner_offline', 'r1', 'runner r1 is offline')
        f.limit_min, f.age_s = 10, 900
        info = dwell.bug_info(f)
        self.assertEqual(info['severity'], 'S3')        # an alarm, never an auto-launched fix
        with mock.patch('asf.tick.file_bugs._file_or_bump_bug', return_value='filed') as file_, \
                mock.patch('asf.record.core.load_items', return_value=({}, [])), \
                mock.patch('asf.record.index.do_index'), \
                mock.patch.object(approvals, 'level_of', return_value='auto'):
            out = dwell.file_cards(self.product, self.tmp, [f, f], out=lambda _l: None)
        self.assertEqual(out, {'watchdog runner_offline: r1': 'filed'})
        self.assertEqual(file_.call_count, 1)

    def test_a_held_file_bug_level_files_nothing(self):
        f = dwell.Finding('runner_offline', 'r1', 'x')
        lines = []
        with mock.patch.object(approvals, 'level_of', return_value='human-now'), \
                mock.patch('asf.tick.file_bugs._file_or_bump_bug') as file_:
            dwell.file_cards(self.product, self.tmp, [f], out=lines.append)
        file_.assert_not_called()
        self.assertEqual(lines, ['held file_bug on watchdog runner_offline: r1 — widen '
                                 'approvals: file_bug in products/<p>.yaml'])

    def test_the_cli_reports_and_exits_1_on_a_breach(self):
        lines = []
        args = types.SimpleNamespace(product='p', json=True)
        with mock.patch.object(env, 'load_product', return_value=self.product), \
                mock.patch.object(dwell, 'Facts',
                                  lambda product, root=None: FakeFacts(
                                      product, runners=[('r1', False)],
                                      tick=(NOW - 20 * 60, 1), holds=[])):
            rc = dwell.cmd_watchdog(args, out=lines.append)
        self.assertEqual(rc, 1)
        data = json.loads(lines[-1])
        self.assertEqual({(d['state'], d['breach']) for d in data},
                         {('runner_offline', False), ('tick_running', True)})

    def test_the_cli_is_registered(self):
        from asf import cli
        args = cli.build_parser().parse_args(['watchdog', '--product', 'p', '--json'])
        self.assertIs(args.func, dwell.cmd_watchdog)


if __name__ == '__main__':
    unittest.main()
