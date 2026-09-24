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

    def test_models_flow_map_and_bad_shape(self):
        """`conventions: models: {review: light}` is a map the brief builder reads; a models value
        of any other shape is the defaults plus a doctor finding — never an exception (it once
        failed every launch)."""
        from asf import doctor
        import importlib
        build = importlib.import_module('asf.briefs.build')

        def product(models_line):
            return env.Product('sample', env.loads(
                'product: sample\nmain: main\nconventions:\n  specs_dir: docs/specs\n'
                + models_line))

        flow = product('  models: {review: heavy, coder: light}\n')
        self.assertEqual(build.model_for(flow, 'review'), 'heavy')
        self.assertEqual(flow.conventions.shape_findings(), [])
        self.assertEqual(doctor.check_convention_shapes(flow), [])
        default = build.model_for(product(''), 'review')
        for bad in ('  models: light\n', '  models: [a, b]\n', '  models: 3\n'):
            with self.subTest(bad=bad):
                p = product(bad)
                self.assertEqual(build.model_for(p, 'review'), default)
                rows = doctor.check_convention_shapes(p)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0][0])
                self.assertIn('conventions.models must be a map', rows[0][1])
        # a misshapen branch_prefixes keeps the default prefixes, never a string's .get
        p = product('  branch_prefixes: worker/\n')
        self.assertEqual(p.conventions.prefix('code'), env.Product('x', {}).conventions.prefix('code'))
        self.assertTrue(p.conventions.shape_findings())

    def test_bad_shape_fails_loud(self):
        """A map-valued key given as anything but a map — `landing_checks_missing: '{docs: wait}'`
        quoted into a string once silently became local-gate — is a RED doctor row naming the key
        and its line; the runtime reader still never raises on it."""
        from asf import doctor
        from asf.harvest import harvest
        text = ("product: sample\nmain: main\nconventions:\n  specs_dir: docs/specs\n"
                "  landing_checks_missing: '{docs: wait}'\n  models: light\n")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(text)
            with mock.patch.object(env, 'ASF_HOME', home):
                p = env.load_product('sample')
                rows = doctor.check_convention_shapes(p)
        details = [d for _ok, d in rows]
        self.assertEqual(len(rows), 2, details)
        self.assertTrue(any('products/sample.yaml:5: conventions.landing_checks_missing' in d
                            for d in details), details)
        self.assertTrue(any('products/sample.yaml:6: conventions.models must be a map' in d
                            for d in details), details)
        self.assertTrue(doctor.is_red([('conventions', True, ok, d) for ok, d in rows]))
        harvest.missing_policy(p.conventions, 'docs')  # never raises
        # the words and a map of them are sound
        for good in ('wait', 'local-gate', '{docs: wait, code: local-gate}'):
            conv = env.Product('x', env.loads(
                f'conventions:\n  landing_checks_missing: {good}\n')).conventions
            self.assertEqual(conv.shape_findings(), [], good)

    def test_the_config_loads_a_flow_map_as_a_map(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('quota_guards: {five_hour_pct: 80, weekly_pct: 90}\n')
            with mock.patch.object(env, 'ASF_HOME', home):
                cfg = env.load_config()
        self.assertEqual(cfg['quota_guards'], {'five_hour_pct': 80, 'weekly_pct': 90})


if __name__ == '__main__':
    unittest.main()
