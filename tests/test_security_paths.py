"""``conventions.security`` and :mod:`asf.security.paths` (T-0358): which paths are sensitive.
``classes``/``touched``/``describe`` answer which of a product's own named classes a diff's files
fall under; ``validate_mapping`` and the three ``Conventions`` readers keep the block loud when
malformed and quiet (nothing sensitive) when unset — the code itself names none of the classes
(D3)."""
import inspect
import re
import unittest

from asf import conventions as conv_mod
from asf.conventions import Conventions, validate_mapping
from asf.security import paths as sec_paths

#: §2.1's documented block, byte-identical to the card's example.
DOCUMENTED_BLOCK = {
    'security': {
        'paths': {
            'auth':           ['apps/api/auth/**', 'asf/approvals.py', 'asf/hooks.py'],
            'billing':        ['apps/api/billing/**'],
            'mail':           ['apps/api/mail/**'],
            'connectors':     ['apps/api/connectors/**'],
            'secrets':        ['asf/redact.py', '**/*.env.example'],
            'ci_credentials': ['.github/workflows/**', 'asf/ci_pool.py'],
        },
        'alerts': {'max_age_h': 24},
        'ports': {
            'boxes': ['plane-1'],
            'workflow': 'security-ports.yml',
            'artifact': 'security-ports',
            'max_age_h': 30,
        },
    },
}
DOCUMENTED_CLASSES = ('auth', 'billing', 'mail', 'connectors', 'secrets', 'ci_credentials')


class ClassTests(unittest.TestCase):
    def test_classes_is_empty_for_a_conv_with_no_block(self):
        self.assertEqual(sec_paths.classes(Conventions()), {})

    def test_classes_is_empty_for_a_conv_that_is_none(self):
        self.assertEqual(sec_paths.classes(None), {})

    def test_classes_is_the_six_class_map_in_configuration_order(self):
        conv = Conventions.from_mapping(DOCUMENTED_BLOCK)
        classes = sec_paths.classes(conv)
        self.assertEqual(tuple(classes), DOCUMENTED_CLASSES)
        self.assertEqual(classes['billing'], ['apps/api/billing/**'])

    def test_touched_puts_a_file_under_every_matching_class_and_leaves_an_unmatched_one_out(self):
        conv = Conventions.from_mapping(DOCUMENTED_BLOCK)
        files = ['apps/api/auth/login.py', 'apps/api/billing/invoice.py', 'apps/web/home.py']
        hit = sec_paths.touched(conv, files)
        self.assertEqual(hit, {'auth': ['apps/api/auth/login.py'],
                               'billing': ['apps/api/billing/invoice.py']})

    def test_a_file_matching_two_classes_appears_under_both(self):
        conv = Conventions.from_mapping(
            {'security': {'paths': {'a': ['x/**'], 'b': ['x/y.py']}}})
        hit = sec_paths.touched(conv, ['x/y.py', 'x/z.py', 'other.py'])
        self.assertEqual(hit, {'a': ['x/y.py', 'x/z.py'], 'b': ['x/y.py']})

    def test_describe_caps_at_ten_with_a_count_and_orders_classes_as_configured(self):
        hit = {'auth': [f'f{i}.py' for i in range(12)], 'billing': ['c.py']}
        self.assertEqual(sec_paths.describe(hit),
                         "auth: f0.py, f1.py, f2.py, f3.py, f4.py, f5.py, f6.py, f7.py, f8.py, "
                         "f9.py and 2 more · billing: c.py")

    def test_describe_names_no_more_when_under_the_cap(self):
        self.assertEqual(sec_paths.describe({'auth': ['a.py', 'b.py']}), 'auth: a.py, b.py')


class SecurityBlockTests(unittest.TestCase):
    def test_the_documented_block_carries_no_problem(self):
        self.assertEqual(validate_mapping(DOCUMENTED_BLOCK), [])

    def test_security_not_a_map(self):
        probs = validate_mapping({'security': 'nope'})
        self.assertEqual([k for k, _ in probs], ['security'])

    def test_paths_not_a_path_list(self):
        probs = validate_mapping({'security': {'paths': {'auth': 'not-a-list'}}})
        self.assertEqual([k for k, _ in probs], ['security.paths.auth'])

    def test_max_age_h_zero_negative_and_a_word(self):
        for bad in (0, -1, 'nightly'):
            probs = validate_mapping({'security': {'alerts': {'max_age_h': bad}}})
            self.assertEqual([k for k, _ in probs], ['security.alerts.max_age_h'], bad)
            probs = validate_mapping({'security': {'ports': {'max_age_h': bad}}})
            self.assertEqual([k for k, _ in probs], ['security.ports.max_age_h'], bad)

    def test_ports_ports_holding_a_bad_value(self):
        for bad in ([0], [70000], ['5432']):
            probs = validate_mapping({'security': {'ports': {'ports': bad}}})
            self.assertEqual([k for k, _ in probs], ['security.ports.ports'], bad)

    def test_an_empty_ports_workflow(self):
        probs = validate_mapping({'security': {'ports': {'workflow': ''}}})
        self.assertEqual([k for k, _ in probs], ['security.ports.workflow'])

    # ---- the readers -----------------------------------------------------

    def test_security_paths_defaults_to_empty_when_the_block_is_absent(self):
        self.assertEqual(Conventions().security_paths(), {})

    def test_security_paths_defaults_to_empty_when_malformed(self):
        conv = Conventions.from_mapping({'security': {'paths': 'nope'}})
        self.assertEqual(conv.security_paths(), {})

    def test_security_alerts_default(self):
        self.assertEqual(Conventions().security_alerts(),
                         {'max_age_h': conv_mod.DEFAULT_ALERT_MAX_AGE_H})

    def test_security_alerts_default_when_malformed(self):
        conv = Conventions.from_mapping({'security': {'alerts': {'max_age_h': 'nightly'}}})
        self.assertEqual(conv.security_alerts(), {'max_age_h': conv_mod.DEFAULT_ALERT_MAX_AGE_H})

    def test_security_ports_defaults(self):
        ports = Conventions().security_ports()
        self.assertEqual(ports['max_age_h'], conv_mod.DEFAULT_PROBE_MAX_AGE_H)
        self.assertEqual(ports['ports'], list(conv_mod.DEFAULT_PROBE_PORTS))
        self.assertEqual(ports['boxes'], [])
        self.assertIsNone(ports['workflow'])
        self.assertIsNone(ports['artifact'])

    def test_security_ports_uses_default_ports_when_ports_ports_is_unset(self):
        conv = Conventions.from_mapping({'security': {'ports': {'boxes': ['plane-1']}}})
        self.assertEqual(conv.security_ports()['ports'], list(conv_mod.DEFAULT_PROBE_PORTS))

    def test_security_ports_defaults_when_ports_ports_is_malformed(self):
        conv = Conventions.from_mapping({'security': {'ports': {'ports': [0, 70000]}}})
        self.assertEqual(conv.security_ports()['ports'], list(conv_mod.DEFAULT_PROBE_PORTS))

    def test_documented_block_readers_keep_the_configured_values(self):
        conv = Conventions.from_mapping(DOCUMENTED_BLOCK)
        self.assertEqual(conv.security_alerts(), {'max_age_h': 24})
        ports = conv.security_ports()
        self.assertEqual((ports['boxes'], ports['workflow'], ports['artifact'], ports['max_age_h']),
                         (['plane-1'], 'security-ports.yml', 'security-ports', 30))

    # ---- D3: the code names none of the classes ---------------------------

    def test_the_code_names_none_of_the_six_classes(self):
        with open(inspect.getsourcefile(conv_mod), encoding='utf-8') as f:
            conv_src = f.read()

        def slice_between(text, start, end):
            i = text.index(start)
            return text[i:text.index(end, i)]

        security_block = slice_between(conv_src, "``security: {paths, alerts, ports}``",
                                       "#: ``branch_retention:``")
        validate_block = slice_between(conv_src, "sec = data.get('security')",
                                       "feeder = data.get('feeder')")
        readers = ''.join(inspect.getsource(getattr(Conventions, name))
                          for name in ('security_paths', 'security_alerts', 'security_ports'))
        paths_src = inspect.getsource(sec_paths)
        for label, text in (('the security block', security_block),
                            ('validate_mapping', validate_block),
                            ('the readers', readers),
                            ('asf/security/paths.py', paths_src)):
            for name in DOCUMENTED_CLASSES:
                self.assertIsNone(re.search(r'\b' + re.escape(name) + r'\b', text, re.I),
                                 f'{name!r} named in {label}')


if __name__ == '__main__':
    unittest.main()
