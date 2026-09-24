"""config-reader-a-yaml-flow-style-map: the config reader parses a flow-style (inline) map or
list, nested either way, or refuses it with the key and the line — never loads it as a string
(a quota guard reading ``quota_guards: {…}`` as text crashed on it)."""
import os
import tempfile
import unittest
from unittest import mock

from asf import env


class FlowStyle(unittest.TestCase):
    def test_a_flow_map_is_parsed(self):
        data = env.loads('quota_guards: {max_sessions: 3, accounts: [a, b], window: {hours: 5}}\n')
        self.assertEqual(data['quota_guards'],
                         {'max_sessions': 3, 'accounts': ['a', 'b'], 'window': {'hours': 5}})

    def test_flow_values_nest_in_lists_and_list_items(self):
        data = env.loads('l: [a, {b: 2}, [c]]\nx:\n  - {a: 1, q: "x, y"}\n  - k: {z: [1, 2]}\n'
                         'e: {}\n')
        self.assertEqual(data['l'], ['a', {'b': 2}, ['c']])
        self.assertEqual(data['x'], [{'a': 1, 'q': 'x, y'}, {'k': {'z': [1, 2]}}])
        self.assertEqual(data['e'], {})

    def test_strings_that_only_hold_braces_stay_strings(self):
        data = env.loads('a: "{quoted}"\nb: http://host/{id}\nc: {reviews_dir}/{n}-{slug}.md\n')
        self.assertEqual(data, {'a': '{quoted}', 'b': 'http://host/{id}',
                                'c': '{reviews_dir}/{n}-{slug}.md'})

    def test_an_unreadable_flow_value_is_refused_with_its_key_and_line(self):
        cases = {
            'a: 1\nquota_guards: {max: 3\n': ('line 2', 'quota_guards', 'unbalanced'),
            'quota_guards: {max 3}\n': ('line 1', 'quota_guards', 'cannot read'),
            'x:\n  - [a, b\n': ('line 2', 'unbalanced'),
        }
        for text, words in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(env.ConfigError) as cm:
                    env.loads(text)
                for w in words:
                    self.assertIn(w, str(cm.exception))

    def test_the_config_loads_a_flow_map_as_a_map(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('quota_guards: {five_hour_pct: 80, weekly_pct: 90}\n')
            with mock.patch.object(env, 'ASF_HOME', home):
                cfg = env.load_config()
        self.assertEqual(cfg['quota_guards'], {'five_hour_pct': 80, 'weekly_pct': 90})


if __name__ == '__main__':
    unittest.main()
