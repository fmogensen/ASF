"""asf.metrics.reds — the visible red PR runs, each told apart into one class from fixture CI
streams; the run-window targets and their breach lines; release criterion 12; the status row.
Hermetic: every stream and every claim is a fixture; the ledger is read from a temp state dir."""
import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from asf import env, release
from asf.metrics import metrics, reds, throughput as tp

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CFG = dict(tp.DEFAULTS)


def at(minutes=0, hours=0, days=0):
    return (NOW - datetime.timedelta(minutes=minutes, hours=hours, days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def job(name='tests', conclusion='success', failed_step=None, cause=None):
    return {'name': name, 'conclusion': conclusion, 'runner': 'r', 'minutes': 3,
            'failed_step': failed_step, 'cause': cause}


def run(rid, ts, conclusion='success', pr=1, sha='a', attempt=1, jobs=None, branch='feat/x',
        superseded=False, batch=None):
    return {'run': rid, 'ts': ts, 'conclusion': conclusion, 'pr': pr, 'sha': sha, 'attempt': attempt,
            'branch': branch, 'batch': batch, 'superseded': superseded,
            'jobs': jobs if jobs is not None else [job(conclusion='failure' if conclusion == 'failure'
                                                       else conclusion, failed_step='run tests'
                                                       if conclusion == 'failure' else None)]}


def cls(ci, claims=None, cfg=CFG):
    return {x['run']: x['cls'] for x in reds.classify(ci, claims or {}, cfg)}


class ClassifyTest(unittest.TestCase):
    def test_real_a_new_commit_was_needed(self):
        ci = [run(1, at(hours=3), 'failure', sha='a'), run(2, at(hours=2), 'success', sha='b')]
        self.assertEqual(cls(ci), {1: reds.REAL})

    def test_flaky_the_same_head_went_green_on_rerun(self):
        ci = [run(1, at(hours=3), 'failure', sha='a'),
              run(1, at(hours=2), 'success', sha='a', attempt=2)]
        self.assertEqual(cls(ci), {1: reds.FLAKY})

    def test_infra_runner_loss_oom_timeout_startup(self):
        ci = [run(1, at(hours=5), 'cancelled', pr=1, jobs=[job(conclusion='cancelled', cause='runner-loss')]),
              run(2, at(hours=4), 'failure', pr=2, jobs=[job(conclusion='failure', failed_step='Set up job')]),
              run(3, at(hours=4), 'failure', pr=3, jobs=[job(conclusion='failure',
                                                             failed_step='tests: Out of memory')]),
              run(4, at(hours=3), 'timed_out', pr=4),
              run(5, at(hours=3), 'startup_failure', pr=5, jobs=[]),
              run(6, at(hours=3), 'cancelled', pr=6, jobs=[job(conclusion='cancelled', cause='timeout')])]
        self.assertEqual(set(cls(ci).values()), {reds.INFRA})

    def test_ours_asf_cancelled_it_from_the_cancel_ledger(self):
        ci = [run(1, at(hours=3), 'cancelled', pr=1, jobs=[job(conclusion='cancelled', cause='failure')]),
              run(2, at(hours=3), 'cancelled', pr=2, jobs=[job(conclusion='cancelled', cause='failure')]),
              run(3, at(hours=3), 'cancelled', pr=3, jobs=[]),
              run(4, at(hours=3), 'cancelled', pr=4, jobs=[], superseded=True)]
        claims = {'1': {'cause': 'relief'}, '2': {'cause': 'duplicate-push'}, '3': {'cause': 'mq-reaped'}}
        self.assertEqual(set(cls(ci, claims).values()), {reds.OURS})

    def test_deterministic_check(self):
        ci = [run(1, at(hours=3), 'failure', jobs=[job('tests', 'failure', failed_step='check conventions')]),
              run(2, at(hours=3), 'failure', pr=2, jobs=[job('DCO', 'failure')]),
              run(3, at(hours=3), 'failure', pr=3, jobs=[job('notes', 'failure', failed_step='the notes line')])]
        self.assertEqual(set(cls(ci).values()), {reds.CHECK})

    def test_a_hand_cancel_is_unclassified_and_a_failure_nothing_followed_is_pending(self):
        ci = [run(1, at(hours=3), 'cancelled', pr=1, jobs=[job(conclusion='cancelled', cause='failure')]),
              run(2, at(hours=3), 'failure', pr=2)]
        self.assertEqual(cls(ci), {1: reds.UNCLASSIFIED, 2: reds.PENDING})

    def test_only_pr_runs_are_visible_reds_and_green_is_not_red(self):
        ci = [run(1, at(hours=3), 'failure', pr=None, branch='main'),
              run(2, at(hours=3), 'failure', pr=None, batch='b/1'),
              run(3, at(hours=3), 'success')]
        self.assertEqual(cls(ci), {})

    def test_patterns_and_own_causes_are_config(self):
        ci = [run(1, at(hours=3), 'failure', jobs=[job('tests', 'failure', failed_step='check conventions')]),
              run(2, at(hours=3), 'cancelled', pr=2, jobs=[]),
              run(3, at(hours=2), 'success', pr=2, sha='b')]
        p = env.Product('p', {'improve': {'scorecard': {'throughput': {
            'check_patterns': ['nothing-matches'], 'own_cancel_causes': 'off'}}}})
        _s, cfg = tp.settings(p)
        self.assertEqual(cfg['own_cancel_causes'], [])
        self.assertEqual(cls(ci, {'2': {'cause': 'relief'}}, cfg),
                         {1: reds.PENDING, 2: reds.UNCLASSIFIED})


class NoiseTest(unittest.TestCase):
    def test_asf_cancels_counted_apart_so_a_silent_skip_reads_as_fewer_reds(self):
        claims = {'1': {'cause': 'relief'}, '2': {'cause': 'stall'}}
        noisy = [run(1, at(hours=3), 'cancelled', pr=1, jobs=[]), run(2, at(hours=2), 'cancelled', pr=2, jobs=[]),
                 run(3, at(hours=1), 'failure', pr=3, sha='c'), run(4, at(minutes=30), 'success', pr=3, sha='d')]
        d = reds.compute({'ci': noisy, 'claims': claims, 'forge': True, 'main': 'main'}, at(0), CFG)
        self.assertEqual((d['day']['total'], d['day']['ours'], d['day']['real']), (3, 2, 1))
        quiet = [dict(r, conclusion='skipped') if r['run'] in (1, 2) else r for r in noisy]
        d = reds.compute({'ci': quiet, 'claims': claims, 'forge': True, 'main': 'main'}, at(0), CFG)
        self.assertEqual((d['day']['total'], d['day']['ours']), (1, 0))
        self.assertEqual(reds.summary(d['day']), '1 (real 1 · flaky 0 · ours 0)')


class WindowTest(unittest.TestCase):
    def test_day_trend_and_last_n_pr_runs(self):
        ci = [run(i, at(days=i % 7, hours=1), 'failure', pr=i, sha=f's{i}') for i in range(1, 15)]
        d = reds.compute({'ci': ci, 'claims': {}, 'forge': True, 'main': 'main'}, at(0),
                         dict(CFG, reds_window=5))
        self.assertEqual(len(d['trend']), 7)
        self.assertEqual(sum(t['total'] for t in d['trend']), 14)
        self.assertEqual(d['runs'], 5)
        self.assertEqual(d['window']['total'], 5)
        self.assertEqual(d['day']['total'], 2)          # runs 7 and 14 at 1 h ago


class TargetsTest(unittest.TestCase):
    def rows(self, ci, cfg=CFG, forge=True):
        return {r['key']: r for r in reds.targets(ci, {}, cfg, forge=forge, main='main')}

    def test_first_pass_over_the_last_n_prs_alarms_below_the_default_0_9(self):
        ci = [run(i, at(hours=100 - i), 'success', pr=i, sha=f's{i}') for i in range(1, 60)]
        ci += [run(100 + i, at(hours=10 - i), 'failure', pr=200 + i, sha=f'f{i}') for i in range(6)]
        r = self.rows(ci)['first_pass']
        self.assertEqual((r['status'], r['value']), (tp.ALARM, 0.88))   # 44/50
        self.assertIn('44/50', r['detail'])
        self.assertTrue(reds.breach_line(r).startswith('metrics: BREACH first_pass — '))
        ok = self.rows(ci[:-1])['first_pass']                             # 45/50
        self.assertEqual(ok['status'], tp.OK)
        self.assertEqual(self.rows(ci, dict(CFG, first_pass_min=None))['first_pass']['status'], tp.OK)

    def test_runner_reds_in_the_last_100_runs(self):
        ci = [run(1, at(hours=200), 'timed_out', pr=1)]
        ci += [run(i, at(hours=150 - i), 'success', pr=i, sha=f's{i}') for i in range(2, 102)]
        self.assertEqual(self.rows(ci)['runner_reds']['status'], tp.OK)   # fell out of the window
        ci.append(run(500, at(0), 'failure', pr=500, jobs=[job(conclusion='failure', failed_step='Set up job')]))
        r = self.rows(ci)['runner_reds']
        self.assertEqual((r['status'], r['value']), (tp.ALARM, 1))
        self.assertEqual(self.rows(ci, dict(CFG, runner_reds_max=1))['runner_reds']['status'], tp.OK)

    def test_the_last_20_trunk_runs_green(self):
        ci = [run(i, at(hours=50 - i), 'success', pr=None, branch='main', sha=f't{i}') for i in range(25)]
        self.assertEqual(self.rows(ci)['trunk_green']['status'], tp.OK)
        ci[10]['conclusion'] = 'failure'
        self.assertEqual(self.rows(ci)['trunk_green']['status'], tp.ALARM)
        self.assertEqual(self.rows(ci, dict(CFG, trunk_green_window=10))['trunk_green']['status'], tp.OK)
        self.assertEqual(self.rows(ci, dict(CFG, trunk_green_window=None))['trunk_green']['status'], tp.NA)

    def test_no_forge_is_na_and_never_alarms(self):
        self.assertEqual({r['status'] for r in self.rows([run(1, at(1), 'failure')], forge=False).values()},
                         {tp.NA})

    def test_defaults_and_off(self):
        _s, cfg = tp.settings(env.Product('p', {}))
        self.assertEqual((cfg['first_pass_min'], cfg['first_pass_window'], cfg['runner_reds_max'],
                          cfg['runner_reds_window'], cfg['trunk_green_window']), (0.9, 50, 0, 100, 20))
        _s, cfg = tp.settings(env.Product('p', {'improve': {'scorecard': {'throughput': {
            'first_pass_min': 'off', 'first_pass_window': 30}}}}))
        self.assertIsNone(cfg['first_pass_min'])
        self.assertEqual(cfg['first_pass_window'], 30)


class ScorecardTest(unittest.TestCase):
    def test_the_scorecard_shows_reds_targets_and_their_breach_lines(self):
        ci = [run(1, at(hours=3), 'failure', sha='a'), run(2, at(hours=2), 'success', sha='b')]
        f = {'ticks': [], 'events': [], 'landings': [], 'ci': ci, 'gates': [], 'items': {}, 'runs': [],
             'forge': True, 'pool': [], 'quota': [], 'stop': 95.0, 'claims': {}, 'main': 'main'}
        d = tp.compute(f, at(0), dict(tp.SEAT_DEFAULTS), CFG)
        text = tp.render(d)
        self.assertIn('**CI reds**', text)
        self.assertIn('| real (a new commit was needed) | 1 | 1 | 0 0 0 0 0 0 1 |', text)
        self.assertIn('**CI targets**', text)
        self.assertIn('metrics: BREACH first_pass', text)
        self.assertIn(reds.breach_line(d['reds']['targets'][0]), d['alarms'])

    def test_a_minimal_product_has_no_reds_section_alarm(self):
        f = {'ticks': [], 'events': [], 'landings': [], 'ci': [], 'gates': [], 'items': {}, 'runs': [],
             'forge': False, 'pool': [], 'quota': [], 'stop': 95.0}
        d = tp.compute(f, at(0), dict(tp.SEAT_DEFAULTS), CFG)
        self.assertEqual(d['alarms'], [])
        self.assertIn('**CI reds** — n/a', tp.render(d))


class CriterionTest(unittest.TestCase):
    def crit(self, product, ci, claims=None):
        return release.pr_ci_criterion(product, ci, at(0), claims=claims or {})

    def test_na_without_a_forge_or_ci(self):
        self.assertTrue(self.crit(env.Product('p', {}), []).met)
        c = self.crit(env.Product('p', {'repo_slug': 'o/r', 'ci': 'none'}), [run(1, at(1), 'failure')])
        self.assertTrue(c.met)
        self.assertIn('n/a', c.evidence)

    def test_no_pr_run_yet_is_pending_never_met(self):
        c = self.crit(env.Product('p', {'repo_slug': 'o/r'}), [])
        self.assertFalse(c.met)
        self.assertIn('pending', c.evidence)

    def test_met_unmet_on_first_pass_and_unclassified(self):
        p = env.Product('p', {'repo_slug': 'o/r', 'main': 'main'})
        green = [run(i, at(hours=60 - i), 'success', pr=i, sha=f's{i}') for i in range(1, 51)]
        self.assertTrue(self.crit(p, green).met)
        hand = green + [run(99, at(0), 'cancelled', pr=99, jobs=[])]
        c = self.crit(p, hand)
        self.assertFalse(c.met)
        self.assertIn('1 unclassified', c.evidence)
        self.assertTrue(self.crit(p, hand, claims={'99': {'cause': 'relief'}}).met)
        low = green[:40] + [run(200 + i, at(hours=5), 'failure', pr=200 + i, sha=f'f{i}') for i in range(10)]
        self.assertFalse(self.crit(p, low).met)


class StatusTest(unittest.TestCase):
    def test_one_row_from_the_record_and_the_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'metrics', 'ci'))
            ci = [run(1, at(hours=3), 'failure', sha='a'), run(2, at(hours=2), 'success', sha='b'),
                  run(3, at(hours=2), 'cancelled', pr=3, jobs=[]),
                  run(4, at(hours=2), 'failure', pr=4, sha='c'), run(4, at(hours=1), 'success', pr=4,
                                                                      sha='c', attempt=2)]
            with open(metrics.stream_path(d, 'ci', NOW.date().isoformat()), 'w') as fh:
                for r in ci:
                    fh.write(json.dumps(r) + '\n')
            state = os.path.join(d, 'state')
            os.makedirs(state)
            with open(os.path.join(state, 'ci-cancels.json'), 'w') as fh:
                json.dump({'3': {'cause': 'relief', 'at': at(hours=2)}}, fh)
            p = env.Product('p', {'repo_slug': 'o/r', 'main': 'main'})
            with mock.patch.object(env, 'state_dir', return_value=state):
                cell = reds.status_cell(d, p, now=NOW)
        self.assertTrue(cell.startswith('reds 24h: 3 (real 1 · flaky 1 · ours 1) · last 5 PR runs: 3 red · BREACH first_pass'), cell)
        self.assertIsNone(reds.status_cell(d, env.Product('p', {})))

    def test_status_renders_the_row(self):
        from asf.views import status
        with mock.patch.object(reds, 'status_cell', return_value='reds 24h: 0 (real 0 · flaky 0 · ours 0)'):
            self.assertEqual(status.reds_cell('/r', env.Product('p', {})),
                             'reds 24h: 0 (real 0 · flaky 0 · ours 0)')


if __name__ == '__main__':
    unittest.main()
