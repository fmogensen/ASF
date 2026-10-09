"""asf.console_feed — B-0121: the FACTORY STATUS table and the tick digest since the console's
own last print, on a fixed clock."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import cli, console_feed, env
from asf.tick import shadow

DAY = '2026-09-25'


def _on_day():
    return datetime.datetime(2026, 9, 25, 9, 30, tzinfo=datetime.timezone.utc)


class ConsoleFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='console_feed_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.addCleanup(self._restore)
        self.product = env.Product('p', {'repo_dir': self.tmp, 'main': 'main',
                                         'ci': {'provider': 'none'}, 'deploy_sha': 'none'})
        self.ticks_dir = os.path.join(shadow.record_dir(self.product), 'metrics', 'ticks')
        os.makedirs(self.ticks_dir)
        self.root = os.path.join(self.tmp, 'record')
        os.makedirs(self.root)

    def _restore(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def append(self, **fields):
        rec = dict({'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0,
                   'steps': []}, **fields)
        path = os.path.join(self.ticks_dir, f'{DAY}.jsonl')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')

    def feed_once(self):
        lines = []
        console_feed.run(self.product, self.root, out=lines.append, now=_on_day)
        return lines

    def test_prints_the_factory_status_table(self):
        lines = self.feed_once()
        self.assertTrue(any(l.startswith('**FACTORY STATUS') for l in lines), lines)

    def test_names_the_interval_to_call_it_again_at(self):
        lines = self.feed_once()
        self.assertIn('console-feed: call `asf console-feed --product p` again in 5m to keep '
                      'this table current — no operator loop needed.', lines)

    def test_no_ticks_yet_carries_no_digest_section(self):
        lines = self.feed_once()
        self.assertNotIn('since the last print:', lines)

    def test_a_tick_already_on_disk_before_the_first_call_is_not_replayed(self):
        self.append(ts='2026-09-25T09:00:00Z', launches=1,
                   steps=[{'step': 'record', 'ok': True, 'seconds': 1.0}])
        lines = self.feed_once()
        self.assertNotIn('since the last print:', lines)

    def test_a_tick_landed_between_two_calls_is_printed_once(self):
        self.feed_once()   # establishes this feed's own starting position
        self.append(ts='2026-09-25T09:00:00Z', launches=2, merges=1, relaunches=1,
                   steps=[{'step': 'record', 'ok': True, 'seconds': 3.2},
                          {'step': 'harvest', 'ok': False, 'seconds': 1.0}])
        first_again = self.feed_once()
        self.assertIn('since the last print:', first_again)
        self.assertIn('2026-09-25T09:00:00Z TICK — record ok, harvest FAILED', first_again)
        # a third call with no new tick since: no digest section, the line is never replayed
        third = self.feed_once()
        self.assertNotIn('since the last print:', third)

    def test_status_every_off_skips_the_table_entirely(self):
        cfg = {'console': {'status_every': 'off'}}
        with mock.patch('asf.env.load_config', return_value=cfg):
            lines = []
            console_feed.run(self.product, self.root, out=lines.append, now=_on_day)
        self.assertEqual(lines, ['console-feed: console.status_every is off for p — nothing to do'])

    def test_product_flag_overrides_the_global_interval(self):
        product = env.Product('p', {'repo_dir': self.tmp, 'main': 'main',
                                     'ci': {'provider': 'none'}, 'deploy_sha': 'none',
                                     'conventions': {'flags': {'status_every': '1m'}}})
        self.assertEqual(console_feed.every_s(product), 60)

    def test_zero_disables_the_feed(self):
        self.assertIsNone(console_feed.every_s(self.product, cfg={'console': {'status_every': 0}}))

    def test_unset_falls_back_to_the_default(self):
        self.assertEqual(console_feed.every_s(self.product, cfg={}), console_feed.DEFAULT_EVERY_S)


class CliTests(unittest.TestCase):
    def test_asf_console_feed_dispatches_to_console_feed_run(self):
        with mock.patch.object(console_feed, 'run', return_value=0) as run, \
                mock.patch('asf.env.load_product', return_value='the-product'), \
                mock.patch('asf.cli.resolve_record', return_value='the-root'):
            rc = cli._main(['console-feed', '--product', 'p'])
        self.assertEqual(rc, 0)
        run.assert_called_once_with('the-product', 'the-root')


if __name__ == '__main__':
    unittest.main()
