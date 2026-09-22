"""asf.tick.summary — the two tables the tick ends with, over the ledger and the index."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.tick import shadow, summary
from asf.workers import pool as pool_mod

NOW = '2026-09-22T12:00:00Z'
INDEX = {'generated': '', 'items': {
    'F-0001': {'id': 'F-0001', 'type': 'feature', 'title': 'a feature', 'folder': 'features',
               'state': 'New', 'decided': True, 'rank': 1},
    'B-0002': {'id': 'B-0002', 'type': 'bug', 'title': 'a bug', 'folder': 'bugs',
               'state': 'New', 'decided': True, 'rank': 1, 'parent': 'F-0001'},
}}


class SummaryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='summary_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.addCleanup(self._restore)
        self.product = env.Product('p', {'repo_dir': self.tmp, 'main': 'main',
                                         'ci': {'provider': 'none'}, 'deploy_sha': 'none'})
        os.makedirs(shadow.record_dir(self.product))
        with open(os.path.join(shadow.record_dir(self.product), 'index.json'), 'w') as f:
            json.dump(INDEX, f)

    def _restore(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def launch(self, job, item='F-0001', kind='spec', started='2026-09-22T11:48:00Z', **extra):
        pool_mod.append_session(self.product, dict(
            job=job, item=item, kind=kind, feature='F-0001', account='acct-a', model='opus',
            pid=101, branch=f'{kind}/{item}', started=started, **extra))

    def render(self, chosen=None, alive=lambda pid: True, lines=None):
        lines = [] if lines is None else lines
        summary.run(_ctx(self.product), chosen, out=lines.append, now=NOW, alive=alive)
        return '\n'.join(lines)


class _ctx:
    def __init__(self, product):
        self.product = product
        self.has_record = False


class ShapeTests(SummaryTestCase):
    def test_empty_ledger_prints_two_none_headers(self):
        text = self.render()
        self.assertEqual(text.split('\n'), [
            '', 'IN FLIGHT — none', '',
            'DONE since 2026-09-21T12:00:00Z — none (first tick on this clock)'])

    def test_in_flight_row_carries_the_title_and_the_age(self):
        self.launch('spec-f-0001')
        text = self.render()
        self.assertIn('IN FLIGHT — 1 session', text)
        self.assertIn('job', text.split('\n')[2])           # the column line follows the header
        row = [ln for ln in text.split('\n') if ln.startswith('spec-f-0001')][0]
        for cell in ('F-0001', 'spec', 'acct-a', 'opus', 'working', '12m'):
            self.assertIn(cell, row)
        self.assertTrue(row.endswith('  a feature'), row)   # what is last, never padded

    def test_dead_pid_is_named_in_flight(self):
        self.launch('spec-f-0001')
        text = self.render(alive=lambda pid: False)
        row = [ln for ln in text.split('\n') if ln.startswith('spec-f-0001')][0]
        self.assertIn('dead pid', row)

    def test_columns_pad_to_the_widest_cell(self):
        self.launch('spec-f-0001')
        self.launch('fix-b-0002-a-long-job-name', item='B-0002', kind='fix-bug',
                    started='2026-09-22T09:00:00Z')
        lines = [ln for ln in self.render().split('\n') if ln.startswith(('job', 'spec-', 'fix-'))]
        off = lines[0].index('item')                          # the header's column offset
        self.assertEqual([ln[off:off + 6] for ln in lines[1:]], ['B-0002', 'F-0001'])
        self.assertEqual(lines[1].split()[0], 'fix-b-0002-a-long-job-name')  # oldest first

    def test_unknown_item_and_missing_index_render_a_dash(self):
        self.launch('task-t-0009', item='T-0009', kind='task')
        row = [ln for ln in self.render().split('\n') if ln.startswith('task-t-0009')][0]
        self.assertTrue(row.endswith('  —'), row)
        os.remove(os.path.join(shadow.record_dir(self.product), 'index.json'))
        row = [ln for ln in self.render().split('\n') if ln.startswith('task-t-0009')][0]
        self.assertTrue(row.endswith('  —'), row)


class DoneTests(SummaryTestCase):
    def test_ended_in_the_window_is_done_with_result_and_took(self):
        self.launch('fix-b-0002', item='B-0002', kind='fix-bug', started='2026-09-22T11:00:00Z')
        pool_mod.update_session(self.product, 'fix-b-0002', ended='2026-09-22T11:41:00Z',
                                end_reason='finished', rc=0)
        pool_mod.update_session(self.product, 'fix-b-0002', harvested='d9a1df0abcdef')
        text = self.render()
        self.assertIn('IN FLIGHT — none', text)
        self.assertIn('DONE since 2026-09-21T12:00:00Z — 1 session', text)
        row = [ln for ln in text.split('\n') if ln.startswith('fix-b-0002')][0]
        self.assertIn('finished, landed d9a1df0', row)
        self.assertIn('  41m  ', row)
        self.assertTrue(row.endswith('  a bug'), row)

    def test_failed_and_dead_results_are_the_ledgers_words(self):
        self.launch('a', started='2026-09-22T11:00:00Z')
        self.launch('b', started='2026-09-22T11:00:00Z')
        pool_mod.update_session(self.product, 'a', ended='2026-09-22T11:30:00Z',
                                end_reason='failed: rate-limit')
        pool_mod.update_session(self.product, 'b', ended='2026-09-22T11:31:00Z', end_reason='dead pid')
        text = self.render()
        self.assertIn('failed: rate-limit', text)
        self.assertIn('dead pid', text)
        self.assertNotIn('landed', text)

    def test_window_is_since_the_previous_stamp_half_open(self):
        summary.write_stamp(self.product, 'all', '2026-09-22T11:30:00Z')
        for job, ended in (('old', '2026-09-22T11:30:00Z'), ('edge', '2026-09-22T11:30:01Z'),
                           ('new', '2026-09-22T11:59:00Z'), ('now', NOW)):
            self.launch(job, started='2026-09-22T11:00:00Z')
            pool_mod.update_session(self.product, job, ended=ended, end_reason='finished')
        text = self.render()
        self.assertIn('DONE since 2026-09-22T11:30:00Z — 3 sessions', text)
        self.assertNotIn('(first tick', text)
        jobs = [ln.split()[0] for ln in text.split('\n') if ln.startswith(('old', 'edge', 'new', 'now'))]
        self.assertEqual(jobs, ['edge', 'new', 'now'])         # oldest first, `old` excluded

    def test_a_relaunched_job_reports_its_ended_run_and_stays_in_flight(self):
        # B-0041: a launch line opens a NEW run; the ended run before it is still a session that
        # finished in this window. `pool.load_sessions` is `lifecycle.latest` and would drop it.
        self.launch('spec-f-0001', started='2026-09-22T11:00:00Z')
        pool_mod.update_session(self.product, 'spec-f-0001', ended='2026-09-22T11:20:00Z',
                                end_reason='failed: rate-limit')
        self.launch('spec-f-0001', started='2026-09-22T11:48:00Z')   # relaunched, still running
        text = self.render()
        self.assertIn('IN FLIGHT — 1 session', text)
        self.assertIn('DONE since 2026-09-21T12:00:00Z — 1 session', text)
        done = text.split('DONE')[1]
        self.assertIn('failed: rate-limit', done)
        self.assertIn('  20m  ', done)
        inflight = text.split('DONE')[0]
        self.assertIn('working', inflight)
        self.assertNotIn('failed', inflight)      # the ended run is not in flight

    def test_stamp_is_written_as_now_after_the_tables(self):
        self.render(chosen=['record'])
        self.assertEqual(summary.read_stamp(self.product, 'record'), NOW)
        self.assertIsNone(summary.read_stamp(self.product, 'all'))
        with open(summary.stamp_path(self.product, 'record')) as f:
            self.assertEqual(f.read(), NOW + '\n')

    def test_each_clock_has_its_own_window(self):
        self.launch('a', started='2026-09-22T11:00:00Z')
        pool_mod.update_session(self.product, 'a', ended='2026-09-22T11:50:00Z', end_reason='finished')
        self.assertIn('— 1 session', self.render(chosen=['record']).split('DONE')[1])
        self.assertIn('— 1 session', self.render(chosen=['health', 'wave']).split('DONE')[1])
        self.assertIn('— none', self.render(chosen=['record']).split('DONE')[1])


class ClockTests(unittest.TestCase):
    def test_clock_slug_matches_the_schedulers(self):
        from asf import scheduler
        self.assertEqual(summary.clock(None), 'all')
        self.assertEqual(summary.clock(['health', 'wave']), scheduler.steps_slug(['health', 'wave']))
        self.assertEqual(summary.clock(['daily']), 'daily')

    def test_age_buckets(self):
        self.assertEqual(summary.age('2026-09-22T11:59:30Z', NOW), '<1m')
        self.assertEqual(summary.age('2026-09-22T11:48:00Z', NOW), '12m')
        self.assertEqual(summary.age('2026-09-22T10:57:00Z', NOW), '1h03m')
        self.assertEqual(summary.age('2026-09-20T08:00:00Z', NOW), '2d04h')
        self.assertEqual(summary.age(None, NOW), '?')
        self.assertEqual(summary.age('yesterday', NOW), '?')


class FailureTests(SummaryTestCase):
    def test_a_render_failure_is_one_line_and_the_stamp_is_kept(self):
        summary.write_stamp(self.product, 'all', '2026-09-22T11:30:00Z')
        # NOT a directory in place of the ledger: `lifecycle.read_lines` returns [] for a path
        # that is not a file, so that renders two empty tables and is no failure at all.
        lines = []
        with mock.patch.object(summary, 'render', side_effect=ValueError('boom')):
            summary.run(_ctx(self.product), None, out=lines.append, now=NOW)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0], 'tick: summary not rendered (boom)')
        self.assertEqual(summary.read_stamp(self.product, 'all'), '2026-09-22T11:30:00Z')

    def test_an_unreadable_ledger_is_survived_not_raised(self):
        with mock.patch.object(summary.lifecycle, 'runs', side_effect=OSError('nope')):
            lines = []
            summary.run(_ctx(self.product), None, out=lines.append, now=NOW)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('tick: summary not rendered ('), lines[0])
