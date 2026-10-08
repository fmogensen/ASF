"""asf.ci_vm — the config half of ``ci.provider: vm``: ``ci.hosts``/``ci.jobs`` parsed with their
defaults, every way of saying either block wrong refused by dotted key, and the product file's
own delegation into it (asf.env.validate_product_text); the transport, the store and the pass
that dispatches, collects, supersedes and times out (``OneRun``); the store rendered in the
check vocabulary (``AsChecks``); and the pass's three entry points (``Wiring``)."""
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
import unittest.mock

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


def _sh(args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f'{" ".join(args)} failed: {r.stdout}\n{r.stderr}')
    return r.stdout


class FakeSsh(unittest.TestCase):
    """A real git repo with one commit, a real bare origin (reached over ``file://``, so
    :func:`asf.ci_vm.heads`/``dispatch_targets``/``trunk_shas`` need no network), and a fake
    ``ssh`` on ``PATH``: it strips the flags and the target off its own argv and runs the
    remaining command — a real ``sh -s`` reading :mod:`asf.ci_vm`'s scripts off stdin for the
    transport's own calls, and a real ``git-receive-pack`` for :func:`asf.ci_vm.push_sha`'s
    ``git push`` (which also goes through ``ssh`` on a ``ssh://`` URL) — so ``REMOTE_START``,
    ``REMOTE_POLL`` and ``REMOTE_CANCEL`` are *actually executed*, with no network and no real
    ssh anywhere."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # ``asf.env.state_dir`` is keyed by product *name* alone and reads the module-level
        # ``ASF_HOME`` constant directly — isolate each test's ``ci-vm.json``/``ci-vm.lock``
        # (and keep them off the real ``~/.ASF``) by patching that constant for the test's life
        self._old_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'asf-home')
        self.addCleanup(self._restore_home)
        bin_dir = os.path.join(self.tmp, 'bin')
        os.makedirs(bin_dir)
        ssh_path = os.path.join(bin_dir, 'ssh')
        with open(ssh_path, 'w', encoding='utf-8') as f:
            # strip ssh's own `-o <val>` pairs and the target, then hand the rest to a shell —
            # `git push` passes its remote command as one already-quoted argument, exactly as a
            # real ssh would relay it to the remote login shell
            f.write('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\n'
                   'exec sh -c "$*"\n')
        os.chmod(ssh_path, 0o755)
        self._old_path = os.environ.get('PATH', '')
        os.environ['PATH'] = bin_dir + os.pathsep + self._old_path
        self.addCleanup(self._restore_path)

        self.host_root = os.path.join(self.tmp, 'host-root')
        os.makedirs(self.host_root)
        origin_dir = os.path.join(self.tmp, 'origin.git')
        self.repo_dir = os.path.join(self.tmp, 'repo')
        _sh(['git', 'init', '-q', '--bare', origin_dir])
        _sh(['git', 'clone', '-q', origin_dir, self.repo_dir])
        _sh(['git', 'config', 'user.email', 'test@example.com'], cwd=self.repo_dir)
        _sh(['git', 'config', 'user.name', 'Test'], cwd=self.repo_dir)
        _sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=self.repo_dir)
        with open(os.path.join(self.repo_dir, 'README.md'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        _sh(['git', 'add', '.'], cwd=self.repo_dir)
        _sh(['git', 'commit', '-q', '-m', 'init'], cwd=self.repo_dir)
        _sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo_dir)
        self.sha = _sh(['git', 'rev-parse', 'HEAD'], cwd=self.repo_dir).strip()

        self.ci = {'provider': 'vm',
                  'hosts': [{'name': 'ci-1', 'ssh': 'ci-1.example', 'root': self.host_root,
                             'slots': 1}],
                  'jobs': {}}
        self.product = env.Product('p', {'repo_slug': 'o/r', 'repo_dir': self.repo_dir,
                                         'main': 'main', 'ci': self.ci})
        self.lines = []
        self.run = subprocess.run

    def _restore_path(self):
        os.environ['PATH'] = self._old_path

    def _restore_home(self):
        env.ASF_HOME = self._old_home

    def out(self, line):
        self.lines.append(line)

    def job(self, name, command, required=False, timeout_min=45, labels=None):
        self.ci['jobs'][name] = {'command': command, 'required': required,
                                 'timeout_min': timeout_min, 'labels': labels or []}

    def add_host(self, name, ssh=None, labels=None, slots=1, root=None):
        root = root or os.path.join(self.tmp, f'host-root-{name}')
        os.makedirs(root, exist_ok=True)
        self.ci['hosts'].append({'name': name, 'ssh': ssh or f'{name}.example', 'root': root,
                                 'slots': slots, 'labels': labels or []})
        return root

    def push_branch(self, branch, sha=None):
        """Push ``sha`` (default the repo's own HEAD) to ``origin`` as ``branch`` — a lane
        branch's head, for :func:`asf.ci_vm.heads`/``dispatch_targets``/``supersede`` to read."""
        sha = sha or self.sha
        _sh(['git', 'push', '-f', 'origin', f'{sha}:refs/heads/{branch}'], cwd=self.repo_dir)

    def commit(self, message='more'):
        """A new commit on the local checkout's current branch; returns its sha. Not pushed."""
        path = os.path.join(self.repo_dir, f'{message}.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(message)
        _sh(['git', 'add', '.'], cwd=self.repo_dir)
        _sh(['git', 'commit', '-q', '-m', message], cwd=self.repo_dir)
        return _sh(['git', 'rev-parse', 'HEAD'], cwd=self.repo_dir).strip()

    def row(self, job_name, sha=None):
        return ci_vm.rows_at(self.product, sha or self.sha).get(job_name)

    def wait_for_exit(self, job='gate', sha=None, host_root=None, timeout=5):
        row = self.row(job, sha=sha)
        run_id = row['run_id']
        path = os.path.join(host_root or self.host_root, 'runs', run_id, 'exit')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not os.path.exists(path):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(path), f'{path} never appeared')

    def remote_exists(self, relpath, host_root=None):
        return os.path.exists(os.path.join(host_root or self.host_root, relpath))

    def remote_read(self, relpath, host_root=None):
        with open(os.path.join(host_root or self.host_root, relpath), encoding='utf-8') as f:
            return f.read()


class OneRun(FakeSsh):
    def test_a_green_job_runs_the_command_at_the_sha_and_concludes_passed(self):
        self.job('gate', command='printf ok > out.txt; exit 0')
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (1, 0))
        row = self.row('gate')
        self.assertEqual(row['state'], ci_vm.RUNNING)
        self.wait_for_exit()
        # the command really ran, at the sha, in its own worktree — checked before cleanup
        self.assertEqual(self.remote_read(f"runs/{row['run_id']}/src/out.txt"), 'ok')
        self.assertEqual(ci_vm.vm_pass(self.product, out=self.out, run=self.run), (0, 1))
        row = self.row('gate')
        self.assertEqual((row['state'], row['exit']), (ci_vm.PASSED, 0))
        self.assertFalse(self.remote_exists(f"runs/{row['run_id']}"))  # cleaned up (C6)

    def test_a_non_zero_command_concludes_failed_with_a_bounded_log_tail(self):
        self.job('gate', command='i=0; while [ $i -lt 500 ]; do echo "line $i"; i=$((i+1)); '
                                 'done; exit 7')
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        self.wait_for_exit()
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        row = self.row('gate')
        self.assertEqual((row['state'], row['exit']), (ci_vm.FAILED, 7))
        self.assertEqual(len(row['log']), 40)
        self.assertEqual(row['log'][-1], 'line 499')
        self.assertTrue(row['ever_red'])

    def test_a_sleep_past_timeout_min_is_cancelled_as_a_timeout(self):
        self.job('gate', command='sleep 30', timeout_min=1)
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        run_id = self.row('gate')['run_id']
        self.assertTrue(self.remote_exists(f'runs/{run_id}'))
        # the pass's own clock, not the remote's: force it past the limit without a real sleep
        ci_vm.vm_pass(self.product, out=self.out, run=self.run, now=time.time() + 120)
        row = self.row('gate')
        self.assertEqual(row['state'], ci_vm.TIMEOUT)
        self.assertTrue(row['ever_red'])
        self.assertFalse(self.remote_exists(f'runs/{run_id}'))

    def test_a_branch_head_move_supersedes_the_old_run_not_the_trunk(self):
        self.ci['hosts'][0]['slots'] = 2  # room to dispatch the trunk and the branch together
        self.job('gate', command='sleep 30')
        old_branch_sha = self.commit('branch-v1')
        self.push_branch('worker/T-1', sha=old_branch_sha)
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)  # trunk + branch dispatched
        self.assertEqual(self.row('gate', sha=self.sha)['state'], ci_vm.RUNNING)
        self.assertEqual(self.row('gate', sha=old_branch_sha)['state'], ci_vm.RUNNING)
        new_sha = self.commit('branch-v2')
        self.push_branch('worker/T-1', sha=new_sha)
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        old = ci_vm.rows_at(self.product, old_branch_sha)['gate']
        self.assertEqual(old['state'], ci_vm.CANCELLED)
        self.assertEqual(old['superseded_by'], new_sha)
        self.assertEqual(ci_vm.rows_at(self.product, new_sha)['gate']['state'], ci_vm.RUNNING)
        # the trunk's own run, at a sha no branch claims, is never superseded (C8)
        self.assertEqual(ci_vm.rows_at(self.product, self.sha)['gate']['state'], ci_vm.RUNNING)

    def test_two_jobs_one_slot_one_now_one_next_pass_trunk_first(self):
        self.job('gate', command='exit 0')
        self.job('lint', command='exit 0')
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        runs = ci_vm.rows_at(self.product, self.sha)
        self.assertEqual(len(runs), 1)
        self.assertIn('gate', runs)  # ci.jobs declaration order (C9)
        self.wait_for_exit('gate')
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        runs = ci_vm.rows_at(self.product, self.sha)
        self.assertIn('lint', runs)

    def test_a_job_whose_labels_only_the_second_host_satisfies_goes_there(self):
        self.add_host('ci-2', labels=['heavy'])
        self.job('e2e', command='exit 0', labels=['heavy'])
        ci_vm.vm_pass(self.product, out=self.out, run=self.run)
        row = self.row('e2e')
        self.assertEqual(row['host'], 'ci-2')

    def test_push_sha_puts_the_sha_and_nothing_else_on_the_remote(self):
        host = ci_vm.hosts(self.product)[0]
        ci_vm.run_ssh(host, ci_vm._fill(ci_vm.REMOTE_ENSURE_REPO, root=host.root), kind='start',
                      run=self.run)
        ok, why = ci_vm.push_sha(self.product, host, self.sha, 'abc123456789-gate-1')
        self.assertTrue(ok, why)
        refs = _sh(['git', 'for-each-ref'], cwd=os.path.join(host.root, 'repo.git'))
        self.assertEqual(refs.strip(), f'{self.sha} commit\trefs/asf/abc123456789-gate-1')

    def test_a_dry_run_dispatches_nothing_and_touches_no_remote(self):
        self.job('gate', command='exit 0')
        got = ci_vm.vm_pass(self.product, out=self.out, dry_run=True, run=self.run)
        self.assertEqual(got, (0, 0))
        self.assertEqual(ci_vm.rows_at(self.product, self.sha), {})
        self.assertTrue(any('DRY-RUN guard: refused — would run ssh' in l for l in self.lines),
                        self.lines)
        self.assertFalse(mutation_guard.is_active())  # the guard does not leak past the pass


def _row(state, **kw):
    row = {'state': state, 'host': 'ci-1', 'run_id': 'r', 'pid': 1, 'branch': None, 'attempt': 1,
          'started': 0, 'ended': 0, 'exit': (0 if state == ci_vm.PASSED else 1), 'log': [],
          'superseded_by': None, 'ever_red': state in ci_vm.RED_STATES}
    row.update(kw)
    return row


class AsChecks(unittest.TestCase):
    """``checks_at``/``trunk_red_at``/``green_trunk_shas`` — the store rendered in the check
    vocabulary (§4), with no transport at all: every row is written straight into the store."""

    def setUp(self):
        self._old_home = env.ASF_HOME
        self.tmp = tempfile.mkdtemp()
        env.ASF_HOME = self.tmp
        self.addCleanup(self._restore)
        self.product = _product(ci={
            'provider': 'vm', 'hosts': [{'name': 'ci-1', 'ssh': 'x'}],
            'jobs': {'gate': {'command': 'x', 'required': True},
                    'lint': {'command': 'y', 'required': True}}})

    def _restore(self):
        env.ASF_HOME = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_rows(self, sha, rows):
        data = ci_vm.load(self.product)
        data['runs'][sha] = rows
        ci_vm.save(self.product, data)

    def test_one_passed_one_running_required_is_pending_with_both_dicts(self):
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED), 'lint': _row(ci_vm.RUNNING)})
        state, detail, checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual(state, 'pending')
        self.assertEqual(detail, 'lint')
        self.assertEqual(len(checks), 2)

    def test_both_passed_is_green(self):
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED), 'lint': _row(ci_vm.PASSED)})
        state, detail, _checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual((state, detail), ('green', '2 check(s)'))

    def test_one_failed_is_red(self):
        self._set_rows('s1', {'gate': _row(ci_vm.FAILED), 'lint': _row(ci_vm.PASSED)})
        state, detail, _checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual((state, detail), ('red', 'gate'))

    def test_a_cancelled_row_is_red_by_red_buckets(self):
        self._set_rows('s1', {'gate': _row(ci_vm.CANCELLED), 'lint': _row(ci_vm.PASSED)})
        state, detail, checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual((state, detail), ('red', 'gate'))
        self.assertEqual(next(c for c in checks if c['name'] == 'gate')['bucket'],
                         ci_vm.BUCKETS[ci_vm.CANCELLED])
        self.assertIn(ci_vm.BUCKETS[ci_vm.CANCELLED], ci_vm.RED_BUCKETS)

    def test_a_required_name_with_no_row_at_all_is_pending(self):
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED)})
        state, detail, checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual(state, 'pending')
        self.assertIn('lint', detail)
        self.assertEqual(sum(1 for c in checks if c['name'] == 'lint'), 1)

    def test_a_required_name_matching_no_ci_jobs_job_says_so_in_the_detail(self):
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED)})
        state, detail, _checks = ci_vm.checks_at(self.product, 's1',
                                                  required=('gate', 'deploy-prod'))
        self.assertEqual(state, 'pending')
        self.assertIn('deploy-prod (names no ci.jobs job)', detail)

    def test_an_unreadable_store_is_every_required_name_pending(self):
        path = os.path.join(env.state_dir(self.product.name), ci_vm.STORE_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('not json{{{')
        state, _detail, checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual(state, 'pending')
        self.assertEqual({c['name'] for c in checks}, {'gate', 'lint'})

    def test_a_falsy_sha_is_pending_never_unknown(self):
        state, _detail, checks = ci_vm.checks_at(self.product, None, required=('gate',))
        self.assertEqual(state, 'pending')
        self.assertEqual(len(checks), 1)

    def test_vocabulary_judged_by_the_real_lane_helpers(self):
        from asf.harvest import lane
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED), 'lint': _row(ci_vm.FAILED)})
        _state, _detail, checks = ci_vm.checks_at(self.product, 's1', required=('gate', 'lint'))
        self.assertEqual(lane.passed_names(checks, ('gate', 'lint')), {'gate'})
        self.assertEqual(lane.not_required_red(checks, ('gate', 'lint')), [])
        self.assertEqual(lane.required_name('lint', ('gate', 'lint')), 'lint')

    def test_trunk_red_at_missing_one_name_reports_complete_without_it(self):
        self._set_rows('s1', {'gate': _row(ci_vm.FAILED)})
        red, concluded = ci_vm.trunk_red_at(self.product, 's1', ('gate', 'lint'))
        self.assertEqual(red, frozenset({'gate'}))
        self.assertEqual(concluded, frozenset({'gate'}))  # `lint` absent: the walk continues

    def test_trunk_red_at_ever_red_survives_a_green_retry(self):
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED, ever_red=True)})
        red, concluded = ci_vm.trunk_red_at(self.product, 's1', ('gate',))
        self.assertEqual((red, concluded), (frozenset({'gate'}), frozenset({'gate'})))

    def test_green_trunk_shas_keeps_order_and_drops_a_non_passed_one(self):
        self._set_rows('s1', {'gate': _row(ci_vm.PASSED)})
        self._set_rows('s2', {'gate': _row(ci_vm.FAILED)})
        self._set_rows('s3', {'gate': _row(ci_vm.PASSED)})
        self.assertEqual(ci_vm.green_trunk_shas(self.product, ['s1', 's2', 's3'], ('gate',)),
                         ['s1', 's3'])


class Wiring(unittest.TestCase):
    """The pass's entry points: ``asf ci vm`` / ``--apply`` under the ``ci`` group (P17), and
    the lane's own call at the end of ``lane_pass`` (T-0710 — the scheduler's one-minute clock
    is cut from this card's footprint, replan F-0194-87b236e237ad). A real tiny repo, landing
    ``fast-forward`` (no ``steps.batch``, so :func:`asf.harvest.lane.landing` is ``ff`` and the
    lane's host needs no PR host and makes no ``gh`` call) — only the pass's own tail matters
    here, not the harvest it follows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'asf-home')
        self.addCleanup(self._restore_home)
        origin = os.path.join(self.tmp, 'origin.git')
        self.repo_dir = os.path.join(self.tmp, 'repo')
        _sh(['git', 'init', '-q', '--bare', origin])
        _sh(['git', 'clone', '-q', origin, self.repo_dir])
        _sh(['git', 'config', 'user.email', 'test@example.com'], cwd=self.repo_dir)
        _sh(['git', 'config', 'user.name', 'Test'], cwd=self.repo_dir)
        _sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=self.repo_dir)
        with open(os.path.join(self.repo_dir, 'README.md'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        _sh(['git', 'add', '.'], cwd=self.repo_dir)
        _sh(['git', 'commit', '-q', '-m', 'init'], cwd=self.repo_dir)
        _sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo_dir)

    def _restore_home(self):
        env.ASF_HOME = self._old_home

    def _product(self, ci):
        return env.Product('p', {'repo_dir': self.repo_dir, 'main': 'main', 'ci': ci})

    def test_registered_under_the_ci_group_beside_queue(self):
        import argparse
        from asf import ci_pool
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        ci_pool.register(sub)
        args = p.parse_args(['ci', 'vm', '--product', 'p'])
        self.assertEqual(args.ci_command, 'vm')
        self.assertFalse(args.apply)
        args = p.parse_args(['ci', 'vm', '--apply', '--product', 'p'])
        self.assertTrue(args.apply)
        args = p.parse_args(['ci', 'queue', '--product', 'p'])
        self.assertEqual(args.ci_command, 'queue')

    def test_lane_pass_calls_vm_pass_once_after_the_queue(self):
        from asf.harvest import lane
        product = self._product({'provider': 'vm', 'hosts': [{'name': 'ci-1', 'ssh': 'x'}],
                                 'jobs': {'gate': {'command': 'x'}}})
        calls = []
        with unittest.mock.patch('asf.ci_vm.vm_pass', lambda p, out=print, dry_run=False:
                                 calls.append('vm_pass') or (0, 0)), \
            unittest.mock.patch('asf.ci_queue.queue_pass', lambda *a, **k:
                                calls.append('queue_pass') or (0, 0)):
            lane.lane_pass(product, out=lambda _l: None)
        self.assertEqual(calls, ['queue_pass', 'vm_pass'])

    def test_lane_pass_prints_when_the_vm_pass_is_already_running(self):
        from asf.harvest import lane
        product = self._product({'provider': 'vm', 'hosts': [{'name': 'ci-1', 'ssh': 'x'}],
                                 'jobs': {'gate': {'command': 'x'}}})
        lines = []
        with unittest.mock.patch('asf.ci_vm.vm_pass', lambda p, out=print, dry_run=False: None), \
            unittest.mock.patch('asf.ci_queue.queue_pass', lambda *a, **k: (0, 0)):
            lane.lane_pass(product, out=lines.append)
        self.assertIn('ci vm: its own pass is running — the lane leaves the CI pass to it',
                      lines)

    def test_lane_pass_on_a_non_vm_product_calls_it_with_no_ssh_and_no_git(self):
        from asf.harvest import lane
        product = self._product({'provider': 'github-actions'})
        with unittest.mock.patch('asf.ci_queue.queue_pass', lambda *a, **k: (0, 0)):
            lane.lane_pass(product, out=lambda _l: None)  # no ssh, no git: ci_vm.vm_pass == (0, 0)


if __name__ == '__main__':
    unittest.main()
