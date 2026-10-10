"""F-0286: the CI stall watch inside the package (asf.ci_stall) — a busy slot silent past its
job's measured p95 x factor and using no CPU is cancelled, claimed as ``stall`` and re-run once;
a busy job is never one; it refuses to act while the operator script still runs."""
import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from asf import ci_heartbeat, ci_measure, ci_queue, ci_stall, env

NOW = 1_800_000_000.0


def stamp(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def product():
    pool = [{'runner': 'r-1', 'box': 'box-1', 'provider': 'alpha', 'role': 'heavy'},
            {'runner': 'r-2', 'box': 'box-2', 'provider': 'alpha', 'role': 'heavy'}]
    return env.Product('p', {'repo_slug': 'o/r', 'ci': {'provider': 'github-actions',
                                                        'workflow': 'ci.yml', 'pool': pool}})


def hb(runner='r-1', job='gate-tests', elapsed=4000, silence=600, cpu=100.0, busy=True,
       run_id=77, job_id=88, written=NOW - 10, repo='o/r'):
    return {'runner': runner, 'busy': busy, 'job': job, 'run_id': run_id, 'job_id': job_id,
            'repo': repo, 'written_at': stamp(written), 'started_at': stamp(NOW - elapsed),
            'last_output_at': stamp(NOW - silence), 'tree_cpu_s': cpu}


def events(kind='gate-tests', runner='r-1', secs=(600,) * 10):
    ts = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    return [{'ts': ts, 'jobs': [{'runner': runner, 'name': kind, 'seconds': s,
                                 'conclusion': 'success'}]} for s in secs]


class Gh:
    """``gh_try`` for the watch: every argv logged; the run reads ``status``."""

    def __init__(self, status='in_progress'):
        self.calls, self.status = [], status

    def gh_try(self, args):
        self.calls.append(list(args))
        if args[:2] == ['run', 'view']:
            return self.status, ''
        return '', ''

    def _gh(self, args):
        self.calls.append(list(args))
        if 'status' in ' '.join(args) or args[:2] == ['run', 'view']:
            return self.status
        return ''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(self._restore)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for k in ('CI_HB_MODE', 'CI_HB_STATE'):
            os.environ.pop(k, None)
        self.lines = []
        os.makedirs(ci_stall.hb_dir(), exist_ok=True)

    def _restore(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_mode(self, m):
        with open(os.path.join(ci_stall.hb_dir(), 'mode'), 'w') as f:
            f.write(m + '\n')

    def run_pass(self, beats, gh=None, now=NOW, apply=True, evs=None):
        gh = gh or Gh()

        def fetch(box, target):
            return beats.get(box, ([], None)) if isinstance(beats.get(box), tuple) \
                else (beats.get(box) or [], None)
        with mock.patch.object(ci_queue.run_cancel, 'time', mock.Mock()):
            got = ci_stall.watch(product(), apply=apply, fetch_fn=fetch, src=gh, now=now,
                                 out=self.lines.append, events=events() if evs is None else evs)
        return got, gh

    def cancels(self, gh):
        return [c for c in gh.calls if c[:2] == ['run', 'cancel'] or 'force-cancel' in ' '.join(c)
                or (c[:1] == ['api'] and 'cancel' in ' '.join(c))]


class TheMax(Base):
    def test_max_is_the_measured_p95_times_the_factor(self):
        data = ci_stall.load(product())
        ci_stall.refresh_max(product(), data, NOW, events(secs=list(range(100, 1100, 100))))
        mx, src = ci_stall.job_max(data, 'gate-tests', 'r-1')
        self.assertEqual((mx, src), (1000 * ci_stall.FACTOR, 'runner p95'))
        # another runner of the same kind reads the fleet's; an unmeasured kind the default
        self.assertEqual(ci_stall.job_max(data, 'gate-tests', 'r-9')[1], 'fleet p95')
        self.assertEqual(ci_stall.job_max(data, 'site', 'r-1'),
                         (float(ci_stall.DEFAULT_MAX_S), 'default'))

    def test_the_table_is_refreshed_daily_not_every_pass(self):
        data = ci_stall.load(product())
        self.assertTrue(ci_stall.refresh_max(product(), data, NOW, events()))
        self.assertFalse(ci_stall.refresh_max(product(), data, NOW + 3600, events(secs=[9] * 9)))
        self.assertTrue(ci_stall.refresh_max(product(), data, NOW + ci_stall.REFRESH_S + 1,
                                             events(secs=[9] * 9)))
        self.assertEqual(data['max']['runner']['r-1']['gate-tests'], 9)

    def test_the_factor_is_the_config_keys(self):
        with open(env.config_path(), 'w') as f:
            f.write('ci:\n  stall_watch:\n    factor: 3\n')
        data = ci_stall.load(product())
        ci_stall.refresh_max(product(), data, NOW, events())
        self.assertEqual(ci_stall.job_max(data, 'gate-tests', 'r-1')[0], 1800.0)


class AStallIsCancelledClaimedAndRerunOnce(Base):
    def test_first_pass_has_no_cpu_rate_and_is_never_a_stall(self):
        self.set_mode('act')
        (stalls, cancels), gh = self.run_pass({'box-1': [hb()]})
        self.assertEqual((stalls, cancels), (0, 0))
        self.assertEqual(self.cancels(gh), [])

    def test_a_silent_idle_job_past_its_max_is_cancelled_and_claimed_as_stall(self):
        self.set_mode('act')
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW - 60)
        (stalls, cancels), gh = self.run_pass({'box-1': [hb(cpu=100.5)]})
        self.assertEqual((stalls, cancels), (1, 1))
        self.assertTrue(self.cancels(gh), gh.calls)
        self.assertTrue(any(l.startswith('ci stall: STALL gate-tests on r-1 (box-1), run 77')
                            for l in self.lines), self.lines)
        claims = ci_queue.load_claims(env.state_dir('p'))
        self.assertEqual(claims['77']['cause'], 'stall')
        # the next pass with the same stall cancels nothing again
        self.lines.clear()
        (_, cancels), gh = self.run_pass({'box-1': [hb(cpu=100.5)]}, now=NOW + 60)
        self.assertEqual(cancels, 0)
        self.assertEqual(self.cancels(gh), [])

    def test_cpu_above_the_floor_is_never_a_stall(self):
        self.set_mode('act')
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW - 60)
        (stalls, _), gh = self.run_pass({'box-1': [hb(cpu=160.0)]})   # a whole core
        self.assertEqual(stalls, 0)
        self.assertEqual(self.cancels(gh), [])

    def test_within_its_max_or_not_silent_is_no_stall(self):
        self.set_mode('act')
        self.run_pass({'box-1': [hb(elapsed=1000)]}, now=NOW - 60)
        (stalls, _), _ = self.run_pass({'box-1': [hb(elapsed=1000)]})
        self.assertEqual(stalls, 0)                 # 1000 s is under 600 x 2
        self.run_pass({'box-1': [hb(silence=30)]}, now=NOW - 60)
        (stalls, _), _ = self.run_pass({'box-1': [hb(silence=30)]})
        self.assertEqual(stalls, 0)

    def test_a_cancelled_run_is_rerun_once_when_completed(self):
        self.set_mode('act')
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW - 60)
        self.run_pass({'box-1': [hb(cpu=100.0)]})
        (_, _), gh = self.run_pass({'box-1': []}, gh=Gh(status='completed'), now=NOW + 60)
        reruns = [c for c in gh.calls if c[:2] == ['run', 'rerun']]
        self.assertEqual(reruns, [['run', 'rerun', '77', '-R', 'o/r', '--failed']])
        (_, _), gh = self.run_pass({'box-1': []}, gh=Gh(status='completed'), now=NOW + 120)
        self.assertEqual([c for c in gh.calls if c[:2] == ['run', 'rerun']], [])

    def test_report_mode_and_no_apply_cancel_nothing(self):
        self.set_mode('report')
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW - 60)
        (stalls, cancels), gh = self.run_pass({'box-1': [hb(cpu=100.0)]})
        self.assertEqual((stalls, cancels), (1, 0))
        self.set_mode('act')
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW - 60, apply=False)
        (stalls, cancels), gh = self.run_pass({'box-1': [hb(cpu=100.0)]}, apply=False)
        self.assertEqual((stalls, cancels), (1, 0))
        self.assertEqual(self.cancels(gh), [])

    def test_another_repos_job_and_a_dead_heartbeat_are_not_judged(self):
        self.set_mode('act')
        beats = {'box-1': [hb(repo='x/y')], 'box-2': [hb(runner='r-2', written=NOW - 900)]}
        self.run_pass(beats, now=NOW - 60)
        (stalls, _), _ = self.run_pass(beats)
        self.assertEqual(stalls, 0)
        self.assertTrue(any('ALARM heartbeat dead on r-2' in l for l in self.lines), self.lines)

    def test_an_unreachable_box_is_an_alarm(self):
        (_, _), _ = self.run_pass({'box-1': (None, 'ssh timeout')})
        self.assertIn('ci stall: ALARM box box-1 unreachable — ssh timeout', self.lines)


class ThePassStampsItself(Base):
    def test_the_counts_and_at_match_what_the_pass_returned_and_printed(self):
        self.set_mode('report')
        pool = [{'runner': 'r-1', 'box': 'box-1', 'provider': 'alpha', 'role': 'heavy'},
                {'runner': 'r-2', 'box': 'box-2', 'provider': 'alpha', 'role': 'heavy'},
                {'runner': 'r-3', 'box': 'box-3', 'provider': 'alpha', 'role': 'heavy'}]
        prod = env.Product('p', {'repo_slug': 'o/r', 'ci': {'provider': 'github-actions',
                                                             'workflow': 'ci.yml', 'pool': pool}})
        beats = {'box-1': [hb()], 'box-2': (None, 'ssh timeout'), 'box-3': []}

        def fetch(box, target):
            v = beats.get(box)
            return v if isinstance(v, tuple) else (v or [], None)
        with mock.patch.object(ci_queue.run_cancel, 'time', mock.Mock()):
            stalls, cancels = ci_stall.watch(prod, apply=False, fetch_fn=fetch, now=NOW,
                                             out=self.lines.append, events=events())
        blk = ci_stall.load(prod)['pass']
        self.assertEqual(blk['at'], NOW)
        self.assertEqual(blk['mode'], 'report')
        self.assertEqual((blk['stalls'], blk['cancels']), (stalls, cancels))
        self.assertEqual((blk['boxes'], blk['unreachable']), (1, 2))
        self.assertEqual(blk['boxes'] + blk['unreachable'], len(ci_heartbeat.boxes(prod)))
        self.assertEqual(sum('ALARM' in l for l in self.lines), 2)

    def test_a_pass_held_back_records_the_legacy_age_and_mode_report(self):
        self.set_mode('act')
        path = os.path.join(ci_stall.hb_dir(), 'claims.json')
        with open(path, 'w') as f:
            json.dump({}, f)
        os.utime(path, (NOW - 42, NOW - 42))
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW)
        blk = ci_stall.load(product())['pass']
        self.assertEqual(blk['mode'], 'report')
        self.assertEqual(blk['legacy'], 42)
        line = next(l for l in self.lines if 'report only' in l)
        self.assertIn(f"claims.json {blk['legacy']}s ago", line)

    def test_an_acting_pass_records_legacy_none(self):
        self.set_mode('act')
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW)
        blk = ci_stall.load(product())['pass']
        self.assertEqual(blk['mode'], 'act')
        self.assertIsNone(blk['legacy'])

    def test_load_on_a_file_with_no_pass_key_returns_an_empty_mapping(self):
        ci_stall.save(product(), {'max': {}, 'cpu': {}, 'claims': {}})
        self.assertEqual(ci_stall.load(product())['pass'], {})

    def test_load_on_a_file_whose_pass_is_a_list_returns_an_empty_mapping(self):
        ci_stall.save(product(), {'max': {}, 'cpu': {}, 'claims': {}, 'pass': [1, 2]})
        self.assertEqual(ci_stall.load(product())['pass'], {})


class OneWatcherAtATime(Base):
    def test_it_refuses_to_act_while_the_operator_scripts_claims_moved(self):
        self.set_mode('act')
        path = os.path.join(ci_stall.hb_dir(), 'claims.json')
        with open(path, 'w') as f:
            json.dump({}, f)
        os.utime(path, (NOW - 60, NOW - 60))
        self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW - 60)
        (stalls, cancels), gh = self.run_pass({'box-1': [hb(cpu=100.0)]})
        self.assertEqual((stalls, cancels), (1, 0))
        self.assertEqual(self.cancels(gh), [])
        self.assertTrue(any('report only — the operator watchdog' in l for l in self.lines))
        # quiet for over 5 min: it acts
        os.utime(path, (NOW - 400, NOW - 400))
        self.lines.clear()
        (_, cancels), _ = self.run_pass({'box-1': [hb(cpu=100.0)]}, now=NOW + 60)
        self.assertEqual(cancels, 1)

    def test_the_mode_env_is_over_the_file(self):
        self.set_mode('report')
        os.environ['CI_HB_MODE'] = 'act'
        self.assertEqual(ci_stall.mode(), 'act')

    def test_the_shared_directory_is_under_the_asf_home(self):
        self.assertEqual(ci_stall.hb_dir(), os.path.join(self.tmp, 'state', 'ci-heartbeat'))


class DoctorNamesWhichWatcherActs(Base):
    """S-76005: ``ci_stall.doctor_rows`` — which watcher acts on a CI stall, when the last pass
    ran, and every cancel of this watch's that was not re-run."""

    def _pool(self, n):
        pool = [{'runner': f'r-{i}', 'box': f'box-{i}', 'provider': 'alpha', 'role': 'heavy'}
                for i in range(n)]
        return env.Product('p', {'repo_slug': 'o/r', 'ci': {'provider': 'github-actions',
                                                             'workflow': 'ci.yml', 'pool': pool}})

    def _stamp(self, prod, at=NOW, mode='act', stalls=0, cancels=0, boxes=2, unreachable=0,
               legacy=None):
        data = ci_stall.load(prod)
        data['pass'] = {'at': at, 'mode': mode, 'stalls': stalls, 'cancels': cancels,
                        'boxes': boxes, 'unreachable': unreachable, 'legacy': legacy}
        ci_stall.save(prod, data)

    def _claim(self, prod, key, **fields):
        data = ci_stall.load(prod)
        data['claims'][key] = fields
        ci_stall.save(prod, data)

    def test_no_pool_and_no_pass_block_is_empty(self):
        self.assertEqual(ci_stall.doctor_rows(self._pool(0), now=NOW), [])

    def test_the_held_timer_row_while_the_operator_watchdog_still_runs(self):
        self.set_mode('act')
        self._stamp(product(), at=NOW)          # a fresh pass: nothing else should fire
        path = os.path.join(ci_stall.hb_dir(), 'claims.json')
        with open(path, 'w') as f:
            json.dump({}, f)
        os.utime(path, (NOW - 42, NOW - 42))
        rows = ci_stall.doctor_rows(product(), now=NOW)
        self.assertEqual(len(rows), 1, rows)
        required, ok, detail = rows[0]
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('the operator watchdog still holds the timer', detail)
        self.assertIn('42s', detail)
        self.assertIn('not claimed in ci-cancels.json', detail)
        self.assertIn('not re-run', detail)
        self.assertIn('asf ci stall-watch --apply', detail)

    def test_a_pool_with_no_pass_block_is_nobody_watching(self):
        prod = self._pool(4)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertEqual(len(rows), 1, rows)
        required, ok, detail = rows[0]
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('no stall-watch pass', detail)
        self.assertIn('never', detail)
        self.assertIn('4 box(es) of ci.pool are unwatched', detail)

    def test_a_stale_pass_is_red_against_the_default_a_fresh_one_is_not(self):
        prod = self._pool(4)
        self._stamp(prod, at=NOW - 83 * 60, boxes=4)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertTrue(any('83 min (limit 15)' in d for _r, _o, d in rows), rows)
        self._stamp(prod, at=NOW - 2 * 60, boxes=4)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertFalse(any('no stall-watch pass' in d for _r, _o, d in rows), rows)

    def test_the_threshold_moves_with_the_config_key(self):
        with open(env.config_path(), 'w') as f:
            f.write('ci:\n  stall_watch:\n    pass_stale_s: 300\n')
        prod = self._pool(4)
        self._stamp(prod, at=NOW - 6 * 60, boxes=4)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertTrue(any('no stall-watch pass' in d for _r, _o, d in rows), rows)
        self._stamp(prod, at=NOW - 4 * 60, boxes=4)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertFalse(any('no stall-watch pass' in d for _r, _o, d in rows), rows)

    def test_a_claim_in_each_bad_state_draws_one_row_each(self):
        prod = product()
        self._stamp(prod, at=NOW - 60)
        for state, run_id, tries in (('cancel-failed', 101, 2), ('rerun-failed', 102, 1),
                                     ('gave-up', 103, 3)):
            self._claim(prod, f'{state}-key', run=run_id, job='gate-tests', runner='r-1',
                       state=state, tries=tries, claimed_at=NOW - 19 * 60,
                       cancelled_at=NOW - 19 * 60)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertEqual(len(rows), 3, rows)
        for required, ok, detail in rows:
            self.assertTrue(required)
            self.assertFalse(ok)
            self.assertIn('it has not been re-run', detail)
        details = ' '.join(d for _r, _o, d in rows)
        for run_id in (101, 102, 103):
            self.assertIn(str(run_id), details)
        for state in ('cancel-failed', 'rerun-failed', 'gave-up'):
            self.assertIn(state, details)
        self.assertIn('gate-tests on r-1', details)
        self.assertIn('2 tries', details)
        self.assertIn('19 min', details)

    def test_a_claim_in_rerun_draws_no_row_of_its_own(self):
        prod = product()
        self._stamp(prod, at=NOW - 60, stalls=1, cancels=1)
        self._claim(prod, 'rerun-key', run=104, job='gate-tests', runner='r-1', state='rerun',
                   tries=1, claimed_at=NOW - 60, cancelled_at=NOW - 60, rerun_at=NOW - 30)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertEqual(len(rows), 1, rows)
        required, ok, detail = rows[0]
        self.assertTrue(ok)
        self.assertIn('mode act', detail)

    def test_a_fresh_act_pass_with_no_stall_and_no_bad_claim_is_one_ok_row(self):
        prod = product()
        self._stamp(prod, at=NOW - 2 * 60, boxes=2)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertEqual(len(rows), 1, rows)
        required, ok, detail = rows[0]
        self.assertTrue(required)
        self.assertTrue(ok)
        self.assertIn('mode act', detail)
        self.assertIn('last pass', detail)
        self.assertIn('0 stalled', detail)
        self.assertIn('0 cancelled', detail)
        self.assertIn('2 box(es)', detail)

    def test_the_same_ok_row_in_report_mode_says_reporting_only(self):
        prod = product()
        self._stamp(prod, at=NOW - 2 * 60, mode='report', boxes=2)
        rows = ci_stall.doctor_rows(prod, now=NOW)
        self.assertEqual(len(rows), 1, rows)
        _, _, detail = rows[0]
        self.assertIn('reporting only, nothing is cancelled', detail)


class TheSubcommand(Base):
    def test_asf_ci_stall_watch_is_registered(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        ci_queue.register(sub)
        args = parser.parse_args(['stall-watch', '--apply'])
        self.assertIs(args.run, ci_stall.cmd_stall_watch)
        self.assertTrue(args.apply)


if __name__ == '__main__':
    unittest.main()
