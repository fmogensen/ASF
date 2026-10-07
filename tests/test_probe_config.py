import copy
import unittest

from asf import env
from asf.probe import config as probe_config

VALID_BLOCK = {
    'workflow': 'probe.yml',
    'role': 'light',
    'identity': 'probe@example.com',
    'secrets': {'identity': 'PROBE_PASSWORD', 'mailbox': 'PROBE_MAILBOX'},
    'mailbox': {
        'from': 'no-reply@example.com',
        'subject': 'sign-in code',
        'code': r'\b(\d{6})\b',
        'max_age_s': 300,
    },
    'journeys': ['sign-up', 'create-and-share', 'billing'],
    'origins': ['https://sample.example.com'],
    'artifact': 'probe-results',
    'timeout': '30m',
}
POOL_ROLES = ('light', 'heavy')
APP_HOST = 'https://sample.example.com'


def _block(**overrides):
    block = copy.deepcopy(VALID_BLOCK)
    for key, value in overrides.items():
        if value is _REMOVE:
            block.pop(key, None)
        else:
            block[key] = value
    return block


_REMOVE = object()


class LoadTests(unittest.TestCase):
    def test_no_probe_block_is_off(self):
        product = env.Product('sample', {'app_host': 'https://sample.example.com'})
        self.assertIsNone(probe_config.load(product))

    def test_the_documented_block_loads_with_the_documented_shape(self):
        product = env.Product('sample', {'app_host': 'https://other.example.com',
                                         'probe': VALID_BLOCK})
        probe = probe_config.load(product)
        self.assertEqual(probe.workflow, 'probe.yml')
        self.assertEqual(probe.role, 'light')
        self.assertEqual(probe.identity, 'probe@example.com')
        self.assertEqual(dict(probe.secrets), VALID_BLOCK['secrets'])
        self.assertEqual(dict(probe.mailbox), VALID_BLOCK['mailbox'])
        self.assertEqual(probe.journeys, ('sign-up', 'create-and-share', 'billing'))
        self.assertEqual(probe.origins, ('https://sample.example.com',))
        self.assertEqual(probe.artifact, 'probe-results')
        self.assertEqual(probe.timeout, '30m')

    def test_origins_defaults_to_app_host_when_unset(self):
        block = _block(origins=_REMOVE)
        product = env.Product('sample', {'app_host': 'https://sample.example.com',
                                         'probe': block})
        probe = probe_config.load(product)
        self.assertEqual(probe.origins, ('https://sample.example.com',))

    def test_origins_empty_with_no_app_host_is_empty(self):
        block = _block(origins=_REMOVE)
        product = env.Product('sample', {'probe': block})
        probe = probe_config.load(product)
        self.assertEqual(probe.origins, ())


class ConfigProblemsTests(unittest.TestCase):
    def _problems(self, block, app_host=APP_HOST, pool=POOL_ROLES):
        return probe_config.config_problems(block, app_host, pool)

    def test_the_documented_block_has_no_problem(self):
        self.assertEqual(self._problems(VALID_BLOCK), [])

    def test_none_is_not_a_problem_the_feature_is_simply_off(self):
        self.assertEqual(self._problems(None), [])

    def test_a_block_that_is_not_a_map(self):
        problems = self._problems('probe.yml')
        self.assertEqual([key for key, _why in problems], ['probe'])

    def test_no_workflow(self):
        problems = self._problems(_block(workflow=_REMOVE))
        self.assertIn('probe.workflow', [key for key, _why in problems])

    def test_a_role_no_ci_pool_runner_carries(self):
        problems = self._problems(_block(role='nonexistent'))
        self.assertIn('probe.role', [key for key, _why in problems])

    def test_secrets_missing_mailbox_key(self):
        problems = self._problems(_block(secrets={'identity': 'PROBE_PASSWORD'}))
        self.assertIn('probe.secrets.mailbox', [key for key, _why in problems])

    def test_secrets_missing_identity_key(self):
        problems = self._problems(_block(secrets={'mailbox': 'PROBE_MAILBOX'}))
        self.assertIn('probe.secrets.identity', [key for key, _why in problems])

    def test_a_secret_value_that_is_not_a_secret_name(self):
        problems = self._problems(_block(secrets={'identity': 'not a name!',
                                                    'mailbox': 'PROBE_MAILBOX'}))
        self.assertIn('probe.secrets.identity', [key for key, _why in problems])

    def test_mailbox_missing_code(self):
        mailbox = {k: v for k, v in VALID_BLOCK['mailbox'].items() if k != 'code'}
        problems = self._problems(_block(mailbox=mailbox))
        self.assertIn('probe.mailbox.code', [key for key, _why in problems])

    def test_a_code_with_no_capturing_group(self):
        problems = self._problems(_block(mailbox={**VALID_BLOCK['mailbox'], 'code': r'\d{6}'}))
        self.assertIn('probe.mailbox.code', [key for key, _why in problems])

    def test_a_code_that_will_not_compile(self):
        problems = self._problems(_block(mailbox={**VALID_BLOCK['mailbox'], 'code': r'(\d{6}'}))
        self.assertIn('probe.mailbox.code', [key for key, _why in problems])

    def test_max_age_s_of_zero_negative_one_and_a_duration_string(self):
        for bad in (0, -1, '5m'):
            problems = self._problems(
                _block(mailbox={**VALID_BLOCK['mailbox'], 'max_age_s': bad}))
            self.assertIn('probe.mailbox.max_age_s', [key for key, _why in problems], bad)

    def test_journeys_not_a_list(self):
        problems = self._problems(_block(journeys='sign-up'))
        self.assertIn('probe.journeys', [key for key, _why in problems])

    def test_journeys_with_blank_entries(self):
        problems = self._problems(_block(journeys=['', ' ']))
        self.assertIn('probe.journeys', [key for key, _why in problems])

    def test_an_origin_that_is_not_https(self):
        problems = self._problems(_block(origins=['http://x']))
        self.assertIn('probe.origins[0]', [key for key, _why in problems])

    def test_an_origin_with_no_scheme(self):
        problems = self._problems(_block(origins=['x.example.com']))
        self.assertIn('probe.origins[0]', [key for key, _why in problems])

    def test_a_timeout_that_is_not_n_s_m_or_h(self):
        problems = self._problems(_block(timeout='soon'))
        self.assertIn('probe.timeout', [key for key, _why in problems])

    def test_neither_origins_nor_app_host(self):
        problems = self._problems(_block(origins=_REMOVE), app_host=None)
        self.assertIn('probe.origins', [key for key, _why in problems])

    def test_origins_set_with_no_app_host_is_fine(self):
        problems = self._problems(VALID_BLOCK, app_host=None)
        self.assertEqual(problems, [])

    def test_app_host_set_with_no_origins_is_fine(self):
        problems = self._problems(_block(origins=_REMOVE), app_host=APP_HOST)
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
