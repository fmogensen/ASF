"""asf.ci_vm — the config half of ``ci.provider: vm``: ``ci.hosts``/``ci.jobs`` parsed with their
defaults, every way of saying either block wrong refused by dotted key, the product file's own
delegation into it (asf.env.validate_product_text) — and the transport, the store and the pass
that dispatches, collects, supersedes and times out a job on a fake, ssh-free host."""
import functools
import os
import subprocess
import tempfile
import textwrap
import time
import unittest

from asf import ci_vm, env, mutation_guard

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


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True)


class FakeSsh(unittest.TestCase):
    """No network and no real ssh anywhere: ``ssh`` on ``PATH`` is a local ``sh`` script that
    strips the connection flags and the target off its own argv and runs whatever is left —
    ``sh -s`` reading the real stdin for :data:`ci_vm.REMOTE_START`/``POLL``/``CANCEL``, or a
    git-invoked ``git-receive-pack`` command for :func:`ci_vm.push_sha` — against a real
    directory, so every script in ``ci_vm`` is actually executed and the assertions are about
    what it did. One real git repo with one commit stands in for the product's own checkout,
    pushed to one real bare repo that stands in for ``origin``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

        self._asf_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'asf-home')
        self.addCleanup(lambda: setattr(env, 'ASF_HOME', self._asf_home))

        bin_dir = os.path.join(self.tmp, 'bin')
        os.makedirs(bin_dir)
        ssh_path = os.path.join(bin_dir, 'ssh')
        with open(ssh_path, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\nexec sh -c "$*"\n')
        os.chmod(ssh_path, 0o755)
        fake_env = dict(os.environ)
        fake_env['PATH'] = bin_dir + os.pathsep + fake_env.get('PATH', '')
        self.run = functools.partial(subprocess.run, env=fake_env)

        self.origin_dir = os.path.join(self.tmp, 'origin.git')
        _git(['init', '-q', '--bare', self.origin_dir], cwd=self.tmp)
        self.repo_dir = os.path.join(self.tmp, 'repo')
        _git(['init', '-q', '-b', 'main', self.repo_dir], cwd=self.tmp)
        with open(os.path.join(self.repo_dir, 'f.txt'), 'w', encoding='utf-8') as f:
            f.write('x')
        _git(['add', 'f.txt'], cwd=self.repo_dir)
        _git(['-c', 'user.email=t@test', '-c', 'user.name=t', 'commit', '-q', '-m', 'init'],
             cwd=self.repo_dir)
        _git(['remote', 'add', 'origin', self.origin_dir], cwd=self.repo_dir)
        _git(['push', '-q', 'origin', 'main'], cwd=self.repo_dir)
        self.sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo_dir,
                                  capture_output=True, text=True, check=True).stdout.strip()

        self.machine_root = os.path.join(self.tmp, 'ci-1')
        self.ci = {'provider': 'vm',
                   'hosts': [{'name': 'ci-1', 'ssh': 'ci-1', 'slots': 1, 'root': self.machine_root}],
                   'jobs': {}}
        self.product = env.Product('p', {'repo_dir': self.repo_dir, 'repo_slug': 'o/r',
                                         'main': 'main', 'ci': self.ci})
        self.lines = []

    def out(self, line):
        self.lines.append(line)

    def job(self, name, command, required=False, timeout_min=45, labels=None):
        entry = {'command': command, 'required': required, 'timeout_min': timeout_min}
        if labels:
            entry['labels'] = labels
        self.ci.setdefault('jobs', {})[name] = entry

    def add_host(self, name, slots=1, labels=None, root=None):
        entry = {'name': name, 'ssh': name, 'slots': slots, 'root': root or os.path.join(self.tmp, name)}
        if labels:
            entry['labels'] = labels
        self.ci.setdefault('hosts', []).append(entry)

    def host_root(self, name='ci-1'):
        return next(h['root'] for h in self.ci['hosts'] if h['name'] == name)

    def row(self, job_name, sha=None):
        data = ci_vm.load(self.product)
        return data['runs'].get(sha or self.sha, {}).get(job_name)

    def remote_exists(self, relpath, host_name='ci-1'):
        return os.path.exists(os.path.join(self.host_root(host_name), relpath))

    def push_branch(self, branch, message='c'):
        """Commits on ``branch`` (creating it if new) in the product's own checkout and pushes
        it to origin; returns the new sha."""
        exists = subprocess.run(['git', 'rev-parse', '--verify', branch], cwd=self.repo_dir,
                                capture_output=True, text=True).returncode == 0
        _git(['checkout', '-q', branch] if exists else ['checkout', '-q', '-B', branch],
             cwd=self.repo_dir)
        _git(['commit', '-q', '--allow-empty', '-m', message], cwd=self.repo_dir)
        _git(['push', '-q', 'origin', branch], cwd=self.repo_dir)
        sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo_dir, capture_output=True,
                             text=True, check=True).stdout.strip()
        _git(['checkout', '-q', 'main'], cwd=self.repo_dir)
        return sha

    def wait_for_exit(self, job_name='gate', sha=None, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self.row(job_name, sha)
            if row:
                root = self.host_root(row['host'])
                if os.path.exists(os.path.join(root, 'runs', row['run_id'], 'exit')):
                    return
            time.sleep(0.05)
        raise AssertionError(f'{job_name} did not exit within {timeout}s')


class OneRun(FakeSsh):
    def test_a_green_job_runs_the_command_at_the_sha_and_concludes_passed(self):
        self.job('gate', command='printf ok > out.txt; exit 0')
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (1, 0))
        self.assertEqual(self.row('gate')['state'], ci_vm.RUNNING)
        self.wait_for_exit()                        # the detached command's own exit file
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (0, 1))
        row = self.row('gate')
        self.assertEqual((row['state'], row['exit']), (ci_vm.PASSED, 0))
        self.assertFalse(self.remote_exists(f"runs/{row['run_id']}"))   # cleaned up (C6)

    def test_a_failing_job_concludes_failed_with_its_log_tail_bounded_to_40_lines(self):
        cmd = 'i=0; while [ $i -lt 500 ]; do echo "line$i"; i=$((i+1)); done; exit 7'
        self.job('gate', command=cmd)
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (1, 0))
        self.wait_for_exit()
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (0, 1))
        row = self.row('gate')
        self.assertEqual((row['state'], row['exit']), (ci_vm.FAILED, 7))
        self.assertEqual(len(row['log']), 40)
        self.assertEqual(row['log'][-1], 'line499')
        self.assertFalse(self.remote_exists(f"runs/{row['run_id']}"))

    def test_a_job_past_its_timeout_is_cancelled_with_its_tail_kept(self):
        self.job('gate', command='sleep 60; exit 0', timeout_min=1)
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (1, 0))
        row = self.row('gate')
        self.assertEqual(row['state'], ci_vm.RUNNING)
        started = row['started']
        dispatched, concluded = ci_vm.vm_pass(self.product, out=self.out, run=self.run,
                                              now=started + 120)
        self.assertEqual((dispatched, concluded), (0, 1))
        row = self.row('gate')
        self.assertEqual(row['state'], ci_vm.TIMEOUT)
        self.assertTrue(row['ever_red'])
        self.assertFalse(self.remote_exists(f"runs/{row['run_id']}"))

    def test_a_superseded_branch_run_is_cancelled_and_the_new_head_dispatched(self):
        old_sha = self.push_branch('worker/T-0700')
        self.job('gate', command='sleep 60; exit 0')
        self.ci['hosts'][0]['slots'] = 2
        dispatched, _ = ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        self.assertEqual(dispatched, 2)   # trunk + the branch
        trunk_row = self.row('gate', sha=self.sha)
        branch_row = self.row('gate', sha=old_sha)
        self.assertEqual(trunk_row['state'], ci_vm.RUNNING)
        self.assertEqual(branch_row['state'], ci_vm.RUNNING)

        new_sha = self.push_branch('worker/T-0700', message='moved')
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        branch_row = self.row('gate', sha=old_sha)
        new_row = self.row('gate', sha=new_sha)
        self.assertEqual(branch_row['state'], ci_vm.CANCELLED)
        self.assertEqual(branch_row['superseded_by'], new_sha)
        self.assertEqual(new_row['state'], ci_vm.RUNNING)
        self.assertEqual(self.row('gate', sha=self.sha)['state'], ci_vm.RUNNING)  # untouched

    def test_two_jobs_one_slot_dispatches_one_now_the_other_next_pass(self):
        self.job('lint', command='exit 0')
        self.job('gate', command='exit 0')
        dispatched, _ = ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        self.assertEqual(dispatched, 1)
        self.assertIsNotNone(self.row('lint'))
        self.assertIsNone(self.row('gate'))
        self.wait_for_exit('lint')
        # the same pass collects lint, freeing the slot, and dispatches gate into it
        dispatched, concluded = ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        self.assertEqual((dispatched, concluded), (1, 1))
        self.assertIsNotNone(self.row('gate'))

    def test_a_job_whose_labels_only_the_second_host_satisfies_goes_there(self):
        self.ci['hosts'][0]['labels'] = ['light']
        self.add_host('ci-2', labels=['heavy'])
        self.job('e2e', command='exit 0', labels=['heavy'])
        dispatched, _ = ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        self.assertEqual(dispatched, 1)
        self.assertEqual(self.row('e2e')['host'], 'ci-2')

    def test_push_sha_puts_only_the_sha_on_the_remote(self):
        host = ci_vm.hosts(self.product)[0]
        self.assertEqual(ci_vm.ensure_repo(host, run=self.run)[0], True)
        ok, why = ci_vm.push_sha(self.product, host, self.sha, 'abc123-gate-1', run=self.run)
        self.assertEqual((ok, why), (True, ''))
        p = subprocess.run(['git', 'ls-remote', os.path.join(self.machine_root, 'repo.git')],
                           capture_output=True, text=True, check=True)
        refs = [line.split() for line in p.stdout.splitlines()]
        self.assertEqual(refs, [[self.sha, 'refs/asf/abc123-gate-1']])

    def test_a_dry_run_refuses_every_write_and_leaves_the_remote_untouched(self):
        host = ci_vm.hosts(self.product)[0]
        with mutation_guard.active():
            rc, stdout, stderr = ci_vm.run_ssh(host, 'echo should-not-run', kind='start',
                                               run=self.run)
        self.assertEqual((rc, stdout), (1, ''))
        self.assertTrue(stderr.startswith('DRY-RUN guard: refused — would run ssh'))

        self.job('gate', command='exit 0')
        dispatched, concluded = ci_vm.vm_pass(self.product, out=self.out, run=self.run,
                                              dry_run=True)
        self.assertEqual((dispatched, concluded), (0, 0))
        self.assertFalse(os.path.exists(self.machine_root))
        self.assertTrue(any('DRY-RUN guard' in l for l in self.lines))


if __name__ == '__main__':
    unittest.main()
