"""A main move costs minutes, not hours: every move of the trunk's head is measured — the
UpdateBranch, rebases, CI reruns and session minutes it causes (:mod:`asf.kernel.mainmoves`)."""
import datetime
import json
import os
import tempfile
import types
import unittest

from asf.kernel import actions as A
from asf.kernel import loop, mainmoves, settings
from asf.kernel.decide import rebase_finding_for
from asf.kernel.model import Facts, MainCommit

try:
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def at(minutes):
    return mainmoves._iso(T0 + datetime.timedelta(minutes=minutes))


def now(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


def facts(head, prs=(), sessions=()):
    return Facts(main=[MainCommit(sha=h) for h in ([head] if isinstance(head, str) else head)],
                 prs=list(prs), sessions=list(sessions))


def run(state, f, actions, minutes, **kw):
    kw.setdefault('alarm_minutes', 5)
    kw.setdefault('window_ticks', 4)
    return mainmoves.observe(state, f, A.Plan(actions=list(actions)), B.config(),
                             now(minutes), **kw)


class Detect(unittest.TestCase):

    def test_the_first_tick_only_records_the_head(self):
        state, lines, done = run({}, facts('aaa'), [], 0)
        self.assertEqual((state['head'], lines, done), ('aaa', [], []))
        self.assertEqual(state['moves'], [])

    def test_an_unchanged_or_unread_head_is_no_move(self):
        state, _, _ = run({}, facts('aaa'), [], 0)
        state, lines, _ = run(state, facts('aaa'), [], 2)
        self.assertEqual((lines, state['moves']), ([], []))
        state, lines, _ = run(state, Facts(main=[]), [], 4)
        self.assertEqual((state['head'], state['moves']), ('aaa', []))

    def test_a_new_head_is_a_move_and_counts_the_commits_since(self):
        state, _, _ = run({}, facts('aaa'), [], 0)
        state, lines, _ = run(state, facts(['ccc', 'bbb', 'aaa']), [], 2)
        (m,) = state['moves']
        self.assertEqual((m['sha'], m['commits'], m['at']), ('ccc', 2, at(2)))
        self.assertEqual(lines, ['MAIN MOVE ccc cost: updated 0, rebases 0, reruns 0, '
                                 'rebase 0.0m, total 0.0m'])


class Cost(unittest.TestCase):

    def setUp(self):
        self.state, _, _ = run({}, facts('aaa', [B.pr(7, 'T-1'), B.pr(8, 'T-2')]), [], 0)

    def test_update_branch_counts_and_its_ci_minutes_are_measured(self):
        s, lines, _ = run(self.state, facts('bbb', [B.pr(7, 'T-1', behind=True)]),
                          [A.UpdateBranch(7)], 2)
        self.assertIn('updated 1', lines[0])
        # the update is pushed: new head, checks running
        running = B.pr(7, 'T-1', head='new', checks=[B.check(status='in_progress')])
        s, lines, _ = run(s, facts('bbb', [running]), [], 4)
        s, lines, _ = run(s, facts('bbb', [running]), [], 6)
        self.assertIn('total 4.0m', lines[0])
        done = B.pr(7, 'T-1', head='new', checks=[B.check()])
        s, lines, _ = run(s, facts('bbb', [done]), [], 8)
        self.assertIn('total 6.0m', lines[-1])
        self.assertEqual(s['moves'][0]['ci_open'], {})

    def test_a_pr_dirty_after_the_move_costs_a_rebase_and_its_session_minutes(self):
        dirty = B.pr(7, 'T-1', conflicting=True)
        launch = A.Launch('build', 'T-1', 'worker/T-1', findings=[rebase_finding_for(7)])
        s, lines, _ = run(self.state, facts('bbb', [dirty]), [launch], 2)
        self.assertIn('rebases 1', lines[0])
        sess = B.session('j1', 'T-1', started=at(3))
        s, _, _ = run(s, facts('bbb', [dirty], [sess]), [], 4)
        s, lines, _ = run(s, facts('bbb', [dirty], [sess]), [], 6)
        self.assertIn('rebase 3.0m', lines[0])
        sess = B.session('j1', 'T-1', started=at(3), alive=False, ended=True)
        s, lines, _ = run(s, facts('bbb', [dirty], [sess]), [], 8)
        self.assertIn('rebase 5.0m', lines[0])
        s, lines, done = run(s, facts('bbb', [dirty], [sess]), [], 10)
        self.assertEqual(done[0]['rebase_minutes'], 5.0, 'a dead session stops counting')

    def test_a_pr_already_dirty_before_the_move_is_not_the_moves(self):
        dirty = B.pr(7, 'T-1', conflicting=True)
        state, _, _ = run({}, facts('aaa', [dirty]), [], 0)
        launch = A.Launch('build', 'T-1', 'worker/T-1', findings=[rebase_finding_for(7)])
        _, lines, _ = run(state, facts('bbb', [dirty]), [launch], 2)
        self.assertIn('rebases 0', lines[0])

    def test_a_rerun_on_an_unchanged_head_counts_a_first_run_does_not(self):
        base = facts('aaa', [B.pr(7, 'T-1', checks=[B.check(run_id=10)])])
        state, _, _ = run({}, base, [], 0)
        rerun = B.pr(7, 'T-1', checks=[B.check(run_id=10, attempt=2, status='in_progress')])
        s, lines, _ = run(state, facts('bbb', [rerun, B.pr(9, 'T-9')]), [], 2)
        self.assertIn('reruns 1', lines[0])
        again, lines, _ = run(s, facts('bbb', [rerun]), [], 4)
        self.assertEqual(again['moves'][0]['reruns'], 1, 'counted once')

    def test_a_changed_head_is_a_new_pr_state_not_a_rerun(self):
        state, _, _ = run({}, facts('aaa', [B.pr(7, 'T-1', checks=[B.check(run_id=10)])]), [], 0)
        pushed = B.pr(7, 'T-1', head='h2', checks=[B.check(run_id=11)])
        s, _, _ = run(state, facts('bbb', [pushed]), [], 2)
        self.assertEqual(s['moves'][0]['reruns'], 0)


class Windows(unittest.TestCase):

    def test_a_move_closes_at_the_next_move_and_finalizes_when_its_work_ends(self):
        state, _, _ = run({}, facts('aaa', [B.pr(7, 'T-1')]), [], 0)
        s, _, _ = run(state, facts('bbb', [B.pr(7, 'T-1')]), [A.UpdateBranch(7)], 2)
        s, lines, done = run(s, facts('ccc', [B.pr(7, 'T-1')]), [A.UpdateBranch(7)], 4)
        first, second = s['moves']
        self.assertTrue(first['closed'])
        self.assertEqual((first['updated'], second['updated']), ([7], [7]),
                         'the later update belongs to the later move')

    def test_a_quiet_move_is_written_when_its_window_ends(self):
        state, _, _ = run({}, facts('aaa'), [], 0)
        s, _, done = run(state, facts('bbb'), [], 2)
        for m in (4, 6, 8):
            s, _, done = run(s, facts('bbb'), [], m)
            self.assertEqual(done, [])
        s, lines, done = run(s, facts('bbb'), [], 10)
        (rec,) = done
        self.assertEqual((rec['sha'], rec['at'], rec['prs_updated'], rec['rebases'],
                          rec['reruns'], rec['rebase_minutes'], rec['total_minutes']),
                         ('bbb', at(2), 0, 0, 0, 0.0, 0.0))
        self.assertEqual(s['moves'], [])
        self.assertIn('MAIN MOVE bbb cost:', lines[-1])


class Alarm(unittest.TestCase):

    def _dirty_move(self, alarm=5):
        state, _, _ = run({}, facts('aaa', [B.pr(7, 'T-1')]), [], 0)
        dirty = B.pr(7, 'T-1', conflicting=True)
        launch = A.Launch('build', 'T-1', 'worker/T-1', findings=[rebase_finding_for(7)])
        s, _, _ = run(state, facts('bbb', [dirty]), [launch], 2, alarm_minutes=alarm)
        sess = B.session('j1', 'T-1', started=at(2))
        return run(s, facts('bbb', [dirty], [sess]), [], 10, alarm_minutes=alarm)

    def test_over_the_limit_alarms_once(self):
        s, lines, _ = self._dirty_move()
        self.assertTrue(any(x.startswith('MAIN MOVE ALARM bbb') for x in lines), lines)
        self.assertTrue(s['moves'][0]['alarmed'])
        sess = B.session('j1', 'T-1', started=at(2))
        dirty = B.pr(7, 'T-1', conflicting=True)
        _, lines, _ = run(s, facts('bbb', [dirty], [sess]), [], 12)
        self.assertFalse(any('ALARM' in x for x in lines))

    def test_under_the_limit_stays_quiet(self):
        _, lines, _ = self._dirty_move(alarm=60)
        self.assertFalse(any('ALARM' in x for x in lines))

    def test_the_alarm_is_in_the_record_and_surfaced(self):
        tmp = tempfile.mkdtemp()
        s, _, _ = self._dirty_move()
        mainmoves.write_state(tmp, s)
        self.assertIn('MAIN MOVE ALARM bbb', mainmoves.alarm_line(tmp, now=now(12)))
        mainmoves.append(tmp, [dict(mainmoves.record_of(s['moves'][0], now(12)))])
        os.remove(os.path.join(tmp, mainmoves.STATE_FILE))
        self.assertIn('MAIN MOVE ALARM bbb', mainmoves.alarm_line(tmp, now=now(12)))
        self.assertEqual(mainmoves.alarm_line(tmp, now=now(60 * 30)), '')

    def test_status_and_watch_carry_the_alarm(self):
        from asf.kernel import status
        out = status.render([], {}, [], head='MAIN MOVE ALARM bbb 8.0m > 5m')
        self.assertIn('MAIN MOVE ALARM bbb', out)


class View(unittest.TestCase):

    def test_moves_per_hour_and_percentiles(self):
        tmp = tempfile.mkdtemp()
        recs = [{'sha': 's%d' % i, 'at': at(i * 6), 'prs_updated': 0, 'rebases': 0, 'reruns': 0,
                 'rebase_minutes': 0.0, 'total_minutes': float(c)} for i, c in
                enumerate([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])]
        mainmoves.append(tmp, recs)
        rep = mainmoves.report(mainmoves.read(tmp), now=now(60), since=now(0))
        self.assertEqual(rep['moves'], 10)
        self.assertAlmostEqual(rep['per_hour'], 10.0)
        self.assertEqual((rep['p50'], rep['p90']), (4.0, 8.0))
        text = mainmoves.render(rep)
        self.assertIn('10 moves', text)
        self.assertIn('p50 4.0m', text)

    def test_an_empty_ledger_says_so(self):
        rep = mainmoves.report([], now=now(60))
        self.assertIn('no main moves', mainmoves.render(rep))


PRODUCT = types.SimpleNamespace(kernel={'main_move': {'alarm_minutes': 5, 'window_ticks': 4}})


class Wired(unittest.TestCase):

    def test_settings_default_and_docs(self):
        k = settings.read(None)['main_move']
        self.assertEqual((k['alarm_minutes'], k['window_ticks']), (5, 15))
        self.assertEqual(settings.problems({'main_move': {'alarm_minutes': 'x'}})[0][0][0],
                         'kernel.main_move.alarm_minutes')

    def test_measure_writes_the_line_and_the_ledger(self):
        tmp = tempfile.mkdtemp()
        out = []
        loop.measure_main_moves(None, tmp, facts('aaa'), A.Plan(), B.config(), out=out.append,
                                now=now(0))
        lines = loop.measure_main_moves(PRODUCT, tmp, facts('bbb'), A.Plan(), B.config(),
                                        out=out.append, now=now(2))
        self.assertEqual(len(lines), 1)
        for m in (4, 6, 8, 10):
            lines = loop.measure_main_moves(PRODUCT, tmp, facts('bbb'), A.Plan(), B.config(),
                                            out=out.append, now=now(m))
        with open(os.path.join(tmp, mainmoves.LEDGER_FILE), encoding='utf-8') as f:
            (rec,) = [json.loads(x) for x in f]
        self.assertEqual(rec['sha'], 'bbb')

    def test_a_dry_run_writes_nothing(self):
        tmp = tempfile.mkdtemp()
        loop.measure_main_moves(None, tmp, facts('aaa'), A.Plan(), B.config(), write=False,
                                out=print, now=now(0))
        self.assertEqual(os.listdir(tmp), [])
