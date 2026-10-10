"""asf.console_feed — B-0121: every product console shows the FACTORY STATUS table on
``console.status_every``'s own clock, instead of the operator typing ``/loop 5m /asf:status``
by hand every session."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import console_feed, env
from asf.tick import shadow

DAY = '2026-09-25'


def _at(hhmmss):
    h, m, s = (int(x) for x in hhmmss.split(':'))
    return datetime.datetime(2026, 9, 25, h, m, s, tzinfo=datetime.timezone.utc)


class ParseEveryTests(unittest.TestCase):
    def test_bare_number_is_minutes(self):
        self.assertEqual(console_feed.parse_every(5), 300)

    def test_unit_suffixed_string(self):
        self.assertEqual(console_feed.parse_every('5m'), 300)
        self.assertEqual(console_feed.parse_every('30s'), 30)
        self.assertEqual(console_feed.parse_every('1h'), 3600)

    def test_off_and_zero_disable_it(self):
        self.assertIsNone(console_feed.parse_every('off'))
        self.assertIsNone(console_feed.parse_every(0))
        self.assertIsNone(console_feed.parse_every('0'))

    def test_none_falls_back_to_the_default(self):
        self.assertEqual(console_feed.parse_every(None), console_feed.parse_every(console_feed.DEFAULT_EVERY))

    def test_an_unreadable_value_falls_back_to_the_default_rather_than_going_silent(self):
        self.assertEqual(console_feed.parse_every('whatever'), console_feed.parse_every(console_feed.DEFAULT_EVERY))


class ResolveEveryTests(unittest.TestCase):
    def test_product_override_wins_over_the_operators_config(self):
        product = env.Product('p', {'conventions': {'flags': {'console_status_every': '10m'}}})
        self.assertEqual(console_feed.resolve_every(product, {'console': {'status_every': '1m'}}), 600)

    def test_falls_back_to_the_operators_config(self):
        product = env.Product('p', {})
        self.assertEqual(console_feed.resolve_every(product, {'console': {'status_every': '1m'}}), 60)

    def test_falls_back_to_the_default_with_neither_set(self):
        product = env.Product('p', {})
        self.assertEqual(console_feed.resolve_every(product, {}), console_feed.parse_every(console_feed.DEFAULT_EVERY))

    def test_off_at_the_product_wins_even_if_the_operator_set_one(self):
        product = env.Product('p', {'conventions': {'flags': {'console_status_every': 'off'}}})
        self.assertIsNone(console_feed.resolve_every(product, {'console': {'status_every': '1m'}}))


class HintLineTests(unittest.TestCase):
    def test_names_the_loop_and_the_skill_at_the_resolved_interval(self):
        line = console_feed.hint_line(300)
        self.assertIn('/loop 5m /asf:console-feed', line)

    def test_a_sub_minute_interval_is_named_in_seconds(self):
        line = console_feed.hint_line(30)
        self.assertIn('/loop 30s /asf:console-feed', line)


class RunOnceTests(unittest.TestCase):
    """``run_once`` is what `/asf:console-feed` prints: the table plus the tick deltas, when
    ``console.status_every`` says it is due — on an injected clock, never the wall one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='console_feed_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.addCleanup(self._restore)
        self.product = env.Product('p', {'repo_dir': self.tmp, 'main': 'main',
                                         'conventions': {'flags': {'console_status_every': '5m'}}})
        self.ticks_dir = os.path.join(shadow.record_dir(self.product), 'metrics', 'ticks')
        os.makedirs(self.ticks_dir)
        self.cfg_patch = mock.patch.object(env, 'load_config', return_value={})
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)

    def _restore(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def append_tick(self, ts, **fields):
        rec = dict({'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0,
                   'steps': []}, ts=ts, **fields)
        path = os.path.join(self.ticks_dir, f'{DAY}.jsonl')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')

    def test_the_first_call_is_always_due_and_prints_the_table_with_no_deltas(self):
        out = console_feed.run_once(self.product, self.tmp, _at('09:00:00'),
                                    render_status=lambda: '**FACTORY STATUS**\n')
        self.assertIn('FACTORY STATUS', out)
        self.assertNotIn('TICK', out)

    def test_a_second_call_inside_the_interval_prints_nothing(self):
        console_feed.run_once(self.product, self.tmp, _at('09:00:00'),
                              render_status=lambda: '**FACTORY STATUS**\n')
        out = console_feed.run_once(self.product, self.tmp, _at('09:02:00'),
                                    render_status=lambda: '**FACTORY STATUS**\n')
        self.assertEqual(out, '')

    def test_a_call_past_the_interval_prints_the_table_and_the_ticks_since_the_last_one(self):
        console_feed.run_once(self.product, self.tmp, _at('09:00:00'),
                              render_status=lambda: '**FACTORY STATUS**\n')
        self.append_tick('2026-09-25T09:02:00Z', launches=1,
                         steps=[{'step': 'record', 'ok': True, 'seconds': 1.0}])
        out = console_feed.run_once(self.product, self.tmp, _at('09:05:00'),
                                    render_status=lambda: '**FACTORY STATUS**\n')
        self.assertIn('FACTORY STATUS', out)
        self.assertIn('2026-09-25T09:02:00Z TICK — record ok', out)

    def test_off_never_prints(self):
        product = env.Product('p', {'repo_dir': self.tmp, 'main': 'main',
                                     'conventions': {'flags': {'console_status_every': 'off'}}})
        out = console_feed.run_once(product, self.tmp, _at('09:00:00'),
                                    render_status=lambda: '**FACTORY STATUS**\n')
        self.assertEqual(out, '')


class HintCommandTests(unittest.TestCase):
    """``console-feed-hint`` — the SessionStart hook's own command: silent with no product, or
    with the feed off; one line otherwise."""

    def test_no_product_resolves_prints_nothing(self):
        with mock.patch.object(env, 'load_product', side_effect=env.ConfigError('no product')):
            rc = console_feed.cmd_console_feed_hint(mock.Mock(product=None))
        self.assertEqual(rc, 0)

    def test_a_resolved_product_with_the_feed_off_prints_nothing(self):
        product = env.Product('p', {'conventions': {'flags': {'console_status_every': 'off'}}})
        with mock.patch.object(env, 'load_product', return_value=product), \
                mock.patch.object(env, 'load_config', return_value={}), \
                mock.patch('builtins.print') as mock_print:
            rc = console_feed.cmd_console_feed_hint(mock.Mock(product=None))
        self.assertEqual(rc, 0)
        mock_print.assert_not_called()

    def test_a_resolved_product_prints_the_hint(self):
        product = env.Product('p', {})
        with mock.patch.object(env, 'load_product', return_value=product), \
                mock.patch.object(env, 'load_config', return_value={}), \
                mock.patch('builtins.print') as mock_print:
            rc = console_feed.cmd_console_feed_hint(mock.Mock(product=None))
        self.assertEqual(rc, 0)
        mock_print.assert_called_once()
        self.assertIn('/asf:console-feed', mock_print.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
