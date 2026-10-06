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

    def test_a_hand_cancel_is_unclassified_and_a_failure_nothing_followed_is_real(self):
        ci = [run(1, at(hours=3), 'cancelled', pr=1, jobs=[job(conclusion='cancelled', cause='failure')]),
              run(2, at(hours=3), 'failure', pr=2)]
        self.assertEqual(cls(ci), {1: reds.UNCLASSIFIED, 2: reds.REAL})

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
                         {1: reds.REAL, 2: reds.UNCLASSIFIED})


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
        self.assertIn('| real (a new commit is needed) | 1 | 1 | 0 0 0 0 0 0 1 |', text)
        self.assertIn('**CI targets**', text)
        self.assertEqual(text.count('metrics: BREACH first_pass'), 1)
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


# ---- ground truth: the 24 h PR failure analysis of 2026-10-05/06, rebuilt as fixture streams ----

T0 = NOW - datetime.timedelta(hours=24)


def t(minutes):
    return (T0 + datetime.timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


def ev(rid, minute, conclusion, pr, sha, wf='tests', jobs=None, attempt=1, batch=None, branch=None,
       superseded=False):
    return {'run': rid, 'ts': t(minute), 'conclusion': conclusion, 'pr': pr, 'sha': sha,
            'attempt': attempt, 'workflow': wf, 'branch': branch or f'b/{pr}', 'batch': batch,
            'superseded': superseded, 'jobs': jobs or []}


def red_job(name, step):
    return [{'name': name, 'conclusion': 'failure', 'runner': 'r', 'minutes': 4, 'failed_step': step}]


def green_job(name):
    return [{'name': name, 'conclusion': 'success', 'runner': 'r', 'minutes': 4, 'failed_step': None}]


class ClassesTest(unittest.TestCase):
    def test_replay_the_same_red_on_an_unchanged_head(self):
        ci = [ev(1, 0, 'failure', 1, 'a', jobs=red_job('tests', 'tests')),
              ev(2, 30, 'failure', 1, 'a', jobs=red_job('tests', 'tests')),     # a reopen
              ev(2, 40, 'failure', 1, 'a', jobs=red_job('tests', 'tests'), attempt=2)]  # a blind re-run
        self.assertEqual([x['cls'] for x in reds.classify(ci, {}, CFG)], [reds.REAL, reds.REPLAY, reds.REPLAY])

    def test_tooling_not_on_branch_old_branches_red_newer_ones_green(self):
        old = [ev(10 + i, 60 + i, 'failure', 10 + i, f'o{i}', wf='release', jobs=red_job('notes', 'notes line'))
               for i in range(4)]
        new = [ev(20 + i, 120 + i, 'success', 20 + i, f'n{i}', wf='release', jobs=green_job('notes'))
               for i in range(3)]
        first = [ev(30 + i, i, 'success', 10 + i, f'p{i}', jobs=green_job('tests')) for i in range(4)]
        got = reds.classify(first + old + new, {}, CFG)
        self.assertEqual({x['cls'] for x in got}, {reds.TOOLING})
        # a defect spread across old and new branches does not separate: real
        mixed = old[:2] + [ev(40, 61, 'failure', 40, 'm', wf='release', jobs=red_job('notes', 'notes line'))]
        mixed += new + [ev(41, 0, 'success', 41, 'x', wf='release', jobs=green_job('notes'))]
        mixed += [ev(42, 0, 'success', 40, 'y', jobs=green_job('tests'))]
        self.assertNotIn(reds.TOOLING, {x['cls'] for x in reds.classify(first + mixed, {}, CFG)})

    def test_a_job_that_never_got_a_runner_is_infra(self):
        jobs = [{'name': 'DCO', 'conclusion': 'cancelled', 'runner': None, 'minutes': 0, 'failed_step': None,
                 'cause': 'failure'}] + green_job('tests')
        self.assertEqual(reds.classify([ev(1, 0, 'failure', 1, 'a', jobs=jobs)], {}, CFG)[0]['cls'], reds.INFRA)

    def test_flaky_needs_the_failed_job_itself_green_not_skipped(self):
        red = ev(1, 0, 'failure', 1, 'a', jobs=red_job('e2e', 'docker') + green_job('unit'))
        skipped_twin = ev(2, 10, 'success', 1, 'a', jobs=green_job('unit'))
        self.assertEqual(reds.classify([red, skipped_twin], {}, CFG)[0]['cls'], reds.REAL)
        real_twin = ev(3, 10, 'success', 1, 'a', jobs=green_job('unit') + green_job('e2e'))
        self.assertEqual(reds.classify([red, real_twin], {}, CFG)[0]['cls'], reds.FLAKY)


def asf_window(hidden_attempts=False):
    """The factory's own repo, 24 h: 60 failed PR runs — 34 real defects (11 of them replays of the same red
    on an unchanged head after a close/reopen), 20 release-notes reds on branches cut before the
    notes tool existed, 6 `check generic` reds — and, with ``hidden_attempts``, the 21 earlier
    re-run attempts the run list does not show (20 red again, 1 green)."""
    ci, rid, minute = [], 1000, 0

    def nxt():
        nonlocal rid, minute
        rid += 1
        minute += 5
        return rid, minute
    # 20 branches cut before the notes tool: their release run is red, their tests run green
    for i in range(20):
        r, m = nxt()
        ci.append(ev(r, m, 'success', 100 + i, f'old{i}', jobs=green_job('tests')))
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 100 + i, f'old{i}', wf='release', jobs=red_job('notes', 'the notes line')))
    # newer branches: the notes job green
    for i in range(23 + 6 + 3):
        r, m = nxt()
        ci.append(ev(r, m, 'success', 200 + i, f'new{i}', wf='release', jobs=green_job('notes')))
    # 20 real defects on their own PRs (tests red), and 3 PRs whose red is replayed 11 times
    for i in range(20):
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 200 + i, f'new{i}', jobs=red_job('tests', 'tests')))
        if hidden_attempts:
            ci.append(ev(r, m + 2, 'failure', 200 + i, f'new{i}', jobs=red_job('tests', 'tests'), attempt=2))
    for i, n in enumerate((6, 4, 4)):
        for _k in range(n):
            r, m = nxt()
            ci.append(ev(r, m, 'failure', 220 + i, f'new{20 + i}', jobs=red_job('tests', 'tests')))
    # 6 forbidden-name reds: the deterministic check
    for i in range(6):
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 223 + i, f'new{23 + i}', jobs=red_job('tests', 'check generic')))
    if hidden_attempts:
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 229, 'new29', jobs=red_job('tests', 'tests')))
        ci.append(ev(r, m + 2, 'success', 229, 'new29', jobs=green_job('tests'), attempt=2))
    return ci


def second_product_window():
    """A second product, 24 h: 49 failed runs, 16 of them on merge-queue batch refs (not PRs), and 65
    cancelled PR runs every one of which the cancel ledger claims. The 33 PR reds: 12 inherit the
    trunk's red e2e test, 8 are the sessions' own defects (one red three times on one head: 2
    replays), 9 had their sign-off job never get a runner, 4 went green on the same head."""
    ci, claims, rid, minute = [], {}, 5000, 0

    def nxt():
        nonlocal rid, minute
        rid += 1
        minute += 3
        return rid, minute
    for i in range(3):                                    # the gate has been green on older PRs
        r, m = nxt()
        ci.append(ev(r, m, 'success', 290 + i, f'g{i}', wf='ci', jobs=green_job('gate-tests')))
    for i in range(12):                                   # the trunk red, inherited
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 300 + i, f't{i}', wf='ci', jobs=red_job('site', 'e2e')))
    for i in range(4):                                    # own defects
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 320 + i, f'd{i}', wf='ci', jobs=red_job('gate-tests', 'pnpm test')))
    for _k in range(3):                                   # one more, red 3x on one head
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 330, 'd-same', wf='ci', jobs=red_job('gate-tests', 'pnpm test')))
    r, m = nxt()                                          # … and an eighth, fixed by a new commit
    ci.append(ev(r, m, 'failure', 331, 'd7', wf='ci', jobs=red_job('gate-tests', 'pnpm test')))
    r, m = nxt()
    ci.append(ev(r, m, 'success', 331, 'd7b', wf='ci', jobs=green_job('gate-tests')))
    for i in range(9):                                    # sign-off job never got a runner
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 340 + i, f's{i}', wf='ci', jobs=[
            {'name': 'DCO', 'conclusion': 'cancelled', 'runner': None, 'minutes': 0, 'failed_step': None,
             'cause': 'failure'}] + green_job('gate-tests')))
    for i in range(4):                                    # flaky, green on the same head
        r, m = nxt()
        ci.append(ev(r, m, 'failure', 350 + i, f'f{i}', wf='ci', jobs=red_job('e2e-docker', 'drill')))
        ci.append(ev(r, m + 1, 'success', 350 + i, f'f{i}', wf='ci', jobs=green_job('e2e-docker'), attempt=2))
    for i in range(16):                                   # merge-queue batch refs: not PRs
        r, m = nxt()
        ci.append(ev(r, m, 'failure', None, f'bt{i}', wf='ci', batch=f'batch-{i}',
                     jobs=red_job('registers', 'registers-check')))
    causes = (['job-timeout'] * 14 + ['superseded'] * 14 + ['cancelled-left'] * 11 + ['relief'] * 11
              + ['mq-dropped'] * 9 + ['orphan-rerun'] * 5 + ['merged-pr'] * 1)
    for i, cause in enumerate(causes):                    # 65 cancels, each claimed in the ledger
        r, m = nxt()
        ci.append(ev(r, m, 'cancelled', 400 + i, f'c{i}', wf='ci', jobs=[
            {'name': 'gate-tests', 'conclusion': 'cancelled', 'runner': 'r', 'minutes': 2,
             'failed_step': None, 'cause': 'failure'}]))
        claims[str(r)] = {'cause': cause}
    return ci, claims


class GroundTruthTest(unittest.TestCase):
    def counts(self, ci, claims=None):
        return reds.counts(reds.classify(ci, claims or {}, CFG))

    def test_asf_60_reds(self):
        c = self.counts(asf_window())
        self.assertEqual(c['total'], 60)
        self.assertEqual({k: c[k] for k in (reds.REAL, reds.REPLAY, reds.TOOLING, reds.CHECK)},
                         {reds.REAL: 23, reds.REPLAY: 11, reds.TOOLING: 20, reds.CHECK: 6})
        self.assertEqual(c[reds.REPLAY] + c[reds.TOOLING], 31)     # about half of the reds

    def test_asf_with_the_hidden_rerun_attempts(self):
        c = self.counts(asf_window(hidden_attempts=True))
        self.assertEqual(c['total'], 81)
        self.assertEqual((c[reds.REPLAY], c[reds.FLAKY], c[reds.REAL]), (31, 1, 23))

    def test_a_second_product_49_reds_16_on_batch_refs_and_65_own_cancels(self):
        ci, claims = second_product_window()
        self.assertEqual(sum(1 for r in ci if r['conclusion'] == 'failure'
                             and not (r['attempt'] == 2)), 49)
        c = self.counts(ci, claims)
        self.assertEqual(c[reds.OURS], 65)
        self.assertEqual(c['total'], 33 + 65)
        self.assertEqual({k: c[k] for k in (reds.REAL, reds.REPLAY, reds.INFRA, reds.FLAKY, reds.UNCLASSIFIED)},
                         {reds.REAL: 18, reds.REPLAY: 2, reds.INFRA: 9, reds.FLAKY: 4, reds.UNCLASSIFIED: 0})
        d = reds.compute({'ci': ci, 'claims': claims, 'forge': True, 'main': 'main'}, NOW, CFG)
        self.assertEqual(reds.summary(d['day']), '98 (real 18 · flaky 13 · ours 65 · replay 2)')


class BaseTimeTest(unittest.TestCase):
    def test_the_heads_trunk_base_orders_tooling_reds_and_is_read_through_one_git_door(self):
        import types
        calls = []

        def git(args, cwd):
            calls.append(args)
            out = {'rev-parse': 'x', 'merge-base': f'base-{args[1]}'}.get(args[0])
            if args[0] == 'show':
                out = {'base-o': '2026-10-05T10:00:00Z', 'base-n': '2026-10-05T20:00:00Z'}.get(args[-1])
            return types.SimpleNamespace(ok=out is not None, data=out)
        with tempfile.TemporaryDirectory() as d:
            bt = reds.git_base_time(d, 'main', git=git)
            self.assertEqual(bt('o'), datetime.datetime(2026, 10, 5, 10, 0, tzinfo=UTC))
            bt('o')
            self.assertEqual(sum(1 for a in calls if a[0] == 'merge-base'), 1)     # cached
            self.assertIsNone(bt('unknown'))
        self.assertIsNone(reds.git_base_time(None, 'main'))
        # three PRs first seen *after* the green one, yet based before the tool: tooling by base
        red = [ev(10 + i, 60 + i, 'failure', 10 + i, f'o{i}', wf='build', jobs=red_job('pack', 'bundle'))
               for i in range(3)]
        green = [ev(20 + i, i, 'success', 20 + i, f'n{i}', wf='build', jobs=green_job('pack')) for i in range(2)]
        bases = {f'o{i}': datetime.datetime(2026, 10, 5, 10, tzinfo=UTC) for i in range(3)}
        bases.update({f'n{i}': datetime.datetime(2026, 10, 5, 20, tzinfo=UTC) for i in range(2)})
        self.assertEqual({x['cls'] for x in reds.classify(red + green, {}, CFG)}, {reds.REAL})
        self.assertEqual({x['cls'] for x in reds.classify(red + green, {}, CFG, base_time=bases.get)},
                         {reds.TOOLING})
