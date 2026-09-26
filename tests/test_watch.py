"""asf.tick.watch — ``asf watch``: tail the ticks stream, one digest per tick line."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import cli, env
from asf.tick import shadow, watch

DAY = '2026-09-25'


def _on_day():
    """The clock ``watch`` reads the day's file by — pinned to ``DAY``, never the wall clock."""
    return datetime.datetime(2026, 9, 25, 9, 30, tzinfo=datetime.timezone.utc)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='watch_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.addCleanup(self._restore)
        self.product = env.Product('p', {'repo_dir': self.tmp, 'main': 'main',
                                         'ci': {'provider': 'none'}, 'deploy_sha': 'none'})
        self.ticks_dir = os.path.join(shadow.record_dir(self.product), 'metrics', 'ticks')
        os.makedirs(self.ticks_dir)

    def _restore(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def append(self, **fields):
        rec = dict({'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0,
                   'steps': []}, **fields)
        path = os.path.join(self.ticks_dir, f'{DAY}.jsonl')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')

    def watch_once(self):
        lines = []
        watch.run(self.product, out=lines.append, sleep=lambda s: None, forever=False,
                  now=_on_day)
        return lines

    def test_no_ticks_yet_prints_only_the_tail_line(self):
        lines = self.watch_once()
        self.assertEqual(len(lines), 1)
        self.assertIn('watch: tailing', lines[0])

    def test_a_tick_already_on_disk_when_watch_starts_is_not_replayed(self):
        self.append(ts='2026-09-25T09:00:00Z', launches=1,
                   steps=[{'step': 'record', 'ok': True, 'seconds': 1.0}])
        lines = self.watch_once()
        self.assertEqual(len(lines), 1)          # only the tail line — nothing replayed

    def test_a_tick_appended_while_watching_is_printed(self):
        """``sleep`` stands in for the poll wait: the first call appends a tick line (as the
        daemon would between polls), the second stops the loop — so the digest it prints came
        from a line that landed after ``watch`` had already started, never a replay."""
        calls = []

        def fake_sleep(_seconds):
            calls.append(1)
            if len(calls) == 1:
                self.append(ts='2026-09-25T09:00:00Z', launches=2, merges=1, relaunches=1,
                           steps=[{'step': 'record', 'ok': True, 'seconds': 3.2},
                                  {'step': 'harvest', 'ok': False, 'seconds': 1.0}])
                return
            raise SystemExit

        lines = []
        with self.assertRaises(SystemExit):
            watch.run(self.product, out=lines.append, sleep=fake_sleep, now=_on_day)
        self.assertEqual(lines[1:], [
            '2026-09-25T09:00:00Z TICK — record ok, harvest FAILED',
            '2026-09-25T09:00:00Z launches 2, merges 1, relaunches 1',
        ])

    def test_digest_lines_render_ts_then_the_tick_digest(self):
        rec = dict(ts='2026-09-25T09:00:00Z', launches=2, merges=1, stalls=0, refusals=0,
                  relaunches=1, steps=[{'step': 'record', 'ok': True, 'seconds': 3.2},
                                       {'step': 'harvest', 'ok': False, 'seconds': 1.0}])
        lines = watch.digest_lines(rec)
        self.assertEqual(lines, [
            '2026-09-25T09:00:00Z TICK — record ok, harvest FAILED',
            '2026-09-25T09:00:00Z launches 2, merges 1, relaunches 1',
        ])

    def test_a_malformed_line_is_skipped(self):
        path = os.path.join(self.ticks_dir, f'{DAY}.jsonl')
        with open(path, 'a', encoding='utf-8') as f:
            f.write('not json\n')
        lines = []
        watch.run(self.product, out=lines.append, sleep=lambda s: None, forever=False,
                  now=_on_day)
        self.assertEqual(len(lines), 1)  # the malformed line and the tail line, minus the bad one


class CliTests(unittest.TestCase):
    def test_asf_watch_dispatches_to_watch_run(self):
        with mock.patch.object(watch, 'run', return_value=0) as run, \
                mock.patch('asf.env.load_product', return_value='the-product'):
            rc = cli._main(['watch', '--product', 'p', '--poll', '2'])
        self.assertEqual(rc, 0)
        run.assert_called_once_with('the-product', poll=2)


if __name__ == '__main__':
    unittest.main()
