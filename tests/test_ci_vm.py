"""asf.ci_vm — the config half of ``ci.provider: vm``: ``ci.hosts``/``ci.jobs`` parsed with their
defaults, every way of saying either block wrong refused by dotted key, and the product file's
own delegation into it (asf.env.validate_product_text)."""
import os
import textwrap
import unittest

from asf import ci_vm, env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dedent(text):
    return textwrap.dedent(text).strip('\n') + '\n'


def _product(ci=None, conventions=None):
    data = {'repo_slug': 'o/r'}
    if ci is not None:
        data['ci'] = ci
    if conventions is not None:
        data['conventions'] = conventions
    return env.Product('p', data)


class Config(unittest.TestCase):
    # ---- hosts()/jobs(), defaults applied ------------------------------------------------

    def test_hosts_parsed_with_every_default_applied(self):
        p = _product(ci={'provider': 'vm', 'hosts': [{'name': 'ci-1', 'ssh': 'ci-1.example'}]})
        self.assertEqual(ci_vm.hosts(p),
                         [ci_vm.VmHost(name='ci-1', ssh='ci-1.example', labels=frozenset(),
                                      slots=1, root='.asf-ci')])

    def test_hosts_keep_their_own_slots_root_and_labels(self):
        p = _product(ci={'provider': 'vm', 'hosts': [
            {'name': 'ci-1', 'ssh': 'ci-1.example', 'labels': ['linux', 'heavy'],
             'slots': 2, 'root': '/srv/asf-ci'}]})
        self.assertEqual(ci_vm.hosts(p),
                         [ci_vm.VmHost(name='ci-1', ssh='ci-1.example',
                                      labels=frozenset({'linux', 'heavy'}), slots=2,
                                      root='/srv/asf-ci')])

    def test_hosts_empty_with_no_hosts_declared(self):
        self.assertEqual(ci_vm.hosts(_product(ci={'provider': 'vm'})), [])

    def test_jobs_parsed_with_every_default_applied(self):
        p = _product(ci={'provider': 'vm', 'jobs': {'gate': {'command': 'make test'}}})
        self.assertEqual(ci_vm.jobs(p),
                         {'gate': ci_vm.VmJob(name='gate', command='make test', required=False,
                                              timeout_min=45, labels=frozenset())})

    def test_jobs_keep_their_own_required_timeout_and_labels(self):
        p = _product(ci={'provider': 'vm', 'jobs': {
            'e2e': {'command': 'make e2e', 'required': True, 'timeout_min': 60,
                    'labels': ['heavy']}}})
        self.assertEqual(ci_vm.jobs(p),
                         {'e2e': ci_vm.VmJob(name='e2e', command='make e2e', required=True,
                                             timeout_min=60, labels=frozenset({'heavy'}))})

    # ---- enabled() -------------------------------------------------------------------------

    def test_enabled_true_only_for_vm(self):
        self.assertTrue(ci_vm.enabled(_product(ci={'provider': 'vm'})))
        self.assertTrue(ci_vm.enabled(_product(ci={'provider': ' VM '})))
        self.assertFalse(ci_vm.enabled(_product(ci={'provider': 'gh-actions'})))
        self.assertFalse(ci_vm.enabled(_product(ci='none')))
        self.assertFalse(ci_vm.enabled(_product(ci=None)))
        self.assertFalse(ci_vm.enabled(_product()))

    # ---- config_problems(): every way of saying it wrong ------------------------------------

    def test_an_unknown_host_field_is_refused(self):
        problems = ci_vm.config_problems({'hosts': [{'name': 'ci-1', 'ssh': 'x', 'bogus': 1}]})
        self.assertIn(('ci.hosts[0].bogus',
                      f"is not a field of a ci.hosts entry ({', '.join(ci_vm.HOST_FIELDS)})"),
                     problems)

    def test_a_missing_ssh_is_refused(self):
        problems = ci_vm.config_problems({'hosts': [{'name': 'ci-1'}]})
        self.assertIn(('ci.hosts[0].ssh', 'is required'), problems)

    def test_a_missing_command_is_refused(self):
        problems = ci_vm.config_problems({'jobs': {'gate': {}}})
        self.assertIn(('ci.jobs.gate.command', 'is required'), problems)

    def test_a_duplicate_host_name_is_refused(self):
        problems = ci_vm.config_problems(
            {'hosts': [{'name': 'ci-1', 'ssh': 'a'}, {'name': 'ci-1', 'ssh': 'b'}]})
        self.assertIn(('ci.hosts[1].name', "'ci-1' is declared twice"), problems)

    def test_non_positive_slots_is_refused(self):
        problems = ci_vm.config_problems({'hosts': [{'name': 'ci-1', 'ssh': 'a', 'slots': 0}]})
        self.assertIn(('ci.hosts[0].slots', 'must be a whole number > 0, not 0'), problems)

    def test_non_positive_timeout_min_is_refused(self):
        problems = ci_vm.config_problems(
            {'jobs': {'gate': {'command': 'make test', 'timeout_min': -1}}})
        self.assertIn(('ci.jobs.gate.timeout_min', 'must be a whole number > 0, not -1'), problems)

    def test_a_job_label_no_host_carries_is_refused(self):
        problems = ci_vm.config_problems({
            'hosts': [{'name': 'ci-1', 'ssh': 'a', 'labels': ['light']}],
            'jobs': {'gate': {'command': 'make test', 'labels': ['heavy']}},
        })
        self.assertIn(('ci.jobs.gate.labels', "['heavy'] is satisfied by no declared ci.hosts entry"),
                     problems)

    def test_a_job_with_no_labels_is_satisfied_by_any_host(self):
        problems = ci_vm.config_problems({
            'hosts': [{'name': 'ci-1', 'ssh': 'a', 'labels': ['light']}],
            'jobs': {'gate': {'command': 'make test'}},
        })
        self.assertEqual(problems, [])

    def test_provider_vm_with_no_hosts_or_jobs_is_refused(self):
        problems = ci_vm.config_problems({'provider': 'vm'})
        self.assertIn(('ci.hosts', 'ci.provider: vm needs at least one host'), problems)
        self.assertIn(('ci.jobs', 'ci.provider: vm needs at least one job'), problems)

    def test_not_a_dict_is_no_problem(self):
        self.assertEqual(ci_vm.config_problems('none'), [])
        self.assertEqual(ci_vm.config_problems(None), [])

    # ---- required_names() -------------------------------------------------------------------

    def test_required_names_from_landing_checks_when_named(self):
        p = _product(ci={'provider': 'vm', 'jobs': {'lint': {'command': 'x', 'required': True}}},
                     conventions={'landing_checks': ['gate']})
        self.assertEqual(ci_vm.required_names(p), ('gate',))

    def test_required_names_from_landing_checks_as_a_lone_string(self):
        p = _product(ci={'provider': 'vm'}, conventions={'landing_checks': 'gate'})
        self.assertEqual(ci_vm.required_names(p), ('gate',))

    def test_required_names_from_required_jobs_otherwise(self):
        p = _product(ci={'provider': 'vm', 'jobs': {
            'lint': {'command': 'make lint', 'required': True},
            'e2e': {'command': 'make e2e', 'required': False},
            'gate': {'command': 'make test', 'required': True},
        }})
        self.assertEqual(ci_vm.required_names(p), ('lint', 'gate'))

    # ---- validate_product_text() ------------------------------------------------------------

    def test_validate_product_text_over_a_full_vm_product(self):
        text = _dedent("""
            repo_slug: o/r
            ci:
              provider: vm
              hosts:
                - {name: ci-1, ssh: ci-1.example, labels: [linux, heavy], slots: 2}
              jobs:
                gate: {command: 'make test', required: true, labels: [heavy]}
            """)
        self.assertEqual(env.validate_product_text(text), [])

    def test_the_documented_example_validates(self):
        path = os.path.join(REPO_ROOT, 'docs', 'products.example.yaml')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(env.validate_product_text(text), [])

    def test_ci_artifacts_and_ci_post_status_are_not_fields(self):
        text = _dedent("""
            repo_slug: o/r
            ci:
              provider: vm
              hosts:
                - {name: ci-1, ssh: ci-1.example}
              jobs:
                gate: {command: 'make test', required: true}
              artifacts:
                retention_days: 7
              post_status: false
            """)
        keys = {k for _, k, _ in env.validate_product_text(text)}
        self.assertIn('ci.artifacts', keys)
        self.assertIn('ci.post_status', keys)


if __name__ == '__main__':
    unittest.main()
