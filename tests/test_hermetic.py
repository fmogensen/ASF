"""asf.hermetic — the one environment builder (F-0087, class "environment leaking into gates and
tests": B-0033, B-0038, B-0043, B-0047). The harvest gate, a worker session and the suite's own
subprocesses all go through :func:`asf.hermetic.build`; these tests pin what it strips, what it
pins and what it puts first — under every base environment a generator can throw at it."""
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import env as env_mod

from asf import hermetic
from asf.harvest import harvest
from asf.tick import step_harvest
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

LEAKS = {'ASF_PRODUCT': 'live-product', 'ASF_JOB': 'someone-elses-job',
         'BACKLOG_ID_RANGE': 'S:1-2', 'GIT_DIR': '/elsewhere/.git',
         'GIT_WORK_TREE': '/elsewhere', 'GIT_INDEX_FILE': '/elsewhere/index',
         'GIT_PREFIX': 'sub/', 'GIT_OBJECT_DIRECTORY': '/elsewhere/objects',
         'GIT_ALTERNATE_OBJECT_DIRECTORIES': '/elsewhere/alt-objects',
         'GIT_QUARANTINE_PATH': '/elsewhere/quarantine'}


def bases():
    """Every subset of the leaking variables on top of a small clean base, plus a PYTHONPATH
    and a GIT_CONFIG_COUNT the base may already carry."""
    keys = sorted(LEAKS)
    for n in range(len(keys) + 1):
        for subset in itertools.combinations(keys, n):
            for extra in ({}, {'PYTHONPATH': '/base/pp'}, {'GIT_CONFIG_COUNT': '1',
                                                             'GIT_CONFIG_KEY_0': 'user.name',
                                                             'GIT_CONFIG_VALUE_0': 'x'}):
                yield dict({'PATH': '/bin', 'HOME': '/me'}, **{k: LEAKS[k] for k in subset}, **extra)


class BuildInvariants(unittest.TestCase):
    def test_no_caller_identity_and_no_hook_variable_survives_any_base(self):
        for base in bases():
            env = hermetic.build(base)
            for var in hermetic.CALLER_IDENTITY + hermetic.GIT_HOOK:
                self.assertNotIn(var, env, base)

    def test_the_default_branch_is_pinned_after_the_bases_own_git_config(self):
        for base in bases():
            env = hermetic.build(base, trunk='trunk')
            n = int(env['GIT_CONFIG_COUNT'])
            pairs = [(env[f'GIT_CONFIG_KEY_{i}'], env[f'GIT_CONFIG_VALUE_{i}']) for i in range(n)]
            self.assertEqual(pairs[-1], ('init.defaultBranch', 'trunk'), base)
            if base.get('GIT_CONFIG_COUNT'):
                self.assertEqual(pairs[0], ('user.name', 'x'))

    def test_the_callers_hooks_path_never_reaches_a_child(self):
        # B-0114: a worker session runs under its own core.hooksPath (F-0076) in GIT_CONFIG_*, and
        # git applies that to every repo, not just the session's — a gate or a suite that inherits
        # it reads the caller's hooks in a repo the child created itself.
        for base in bases():
            caller = dict(base)
            hermetic._git_config(caller, [('core.hooksPath', '/callers/.ASF/state/p/githooks')])
            env = hermetic.build(caller, trunk='trunk')
            keys = [k.lower() for k, _ in hermetic.git_config_pairs(env)]
            self.assertNotIn('core.hookspath', keys, base)
            self.assertIn('init.defaultbranch', keys, base)
            if base.get('GIT_CONFIG_COUNT'):  # the base's own pairs are kept, and stay in order
                self.assertEqual(hermetic.git_config_pairs(env)[0], ('user.name', 'x'), base)

    def test_a_caller_that_asks_for_a_hooks_path_still_gets_one(self):
        # the session spawner passes its own through git_config= — stripping the *inherited* one
        # must not take that with it (asf.workers.runtime.build_env, F-0076)
        caller = hermetic._git_config({'PATH': '/bin'}, [('core.hooksPath', '/inherited')])
        env = hermetic.build(caller, git_config=[('core.hooksPath', '/mine')])
        self.assertEqual([p for p in hermetic.git_config_pairs(env) if p[0] == 'core.hooksPath'],
                         [('core.hooksPath', '/mine')])

    def test_stripping_leaves_the_remaining_pairs_contiguous(self):
        # git reads KEY_0..KEY_<count-1>; a hole where the dropped pair was loses every pair after
        env = {'PATH': '/bin'}
        hermetic._git_config(env, [('user.name', 'x'), ('core.hooksPath', '/h'),
                                   ('user.email', 'e')])
        hermetic.strip_git_config(env)
        self.assertEqual(hermetic.git_config_pairs(env),
                         [('user.name', 'x'), ('user.email', 'e')])
        self.assertEqual(env['GIT_CONFIG_COUNT'], '2')
        self.assertNotIn('GIT_CONFIG_KEY_2', env)

    def test_stripping_a_base_that_carries_no_git_config_adds_none(self):
        env = {'PATH': '/bin'}
        hermetic.strip_git_config(env)
        self.assertEqual(env, {'PATH': '/bin'})

    def test_the_worktree_is_first_on_pythonpath_then_the_running_package_then_the_base(self):
        for base in bases():
            env = hermetic.build(base, worktree='/wt')
            parts = env['PYTHONPATH'].split(os.pathsep)
            self.assertEqual(parts[0], '/wt')
            self.assertEqual(parts[1], hermetic.package_parent())
            if base.get('PYTHONPATH'):
                self.assertEqual(parts[2], '/base/pp')

    def test_identity_is_set_never_inherited_and_home_moves(self):
        env = hermetic.build(dict(LEAKS, HOME='/me'), identity={'ASF_PRODUCT': 'p', 'ASF_JOB': 'j',
                                                                'BACKLOG_ID_RANGE': None},
                             home='/homes/a')
        self.assertEqual((env['ASF_PRODUCT'], env['ASF_JOB'], env['HOME']), ('p', 'j', '/homes/a'))
        self.assertNotIn('BACKLOG_ID_RANGE', env)

    def test_a_child_given_a_home_never_keeps_the_callers_runtime_config_dir(self):
        # the install e2e ran a real runtime CLI in a temp HOME, but under a worker session's
        # CLAUDE_CONFIG_DIR: its `plugin marketplace add` landed in that account's live settings
        base = {'PATH': '/bin', 'HOME': '/me', hermetic.RUNTIME_CONFIG_DIR: '/me/.cfg/acct-a'}
        for mode in hermetic.MODES:
            env = hermetic.build(base, home='/tmp/t/home', mode=mode,
                                 passthrough=(hermetic.RUNTIME_CONFIG_DIR,))
            self.assertNotIn(hermetic.RUNTIME_CONFIG_DIR, env, mode)
        # no home of its own: the caller's runtime is the child's, as before
        self.assertEqual(hermetic.build(base)[hermetic.RUNTIME_CONFIG_DIR], '/me/.cfg/acct-a')

    def test_git_init_under_the_built_env_ignores_the_hosts_default_branch(self):
        # B-0038 as a live check: a global gitconfig says master; a child git says the trunk
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        gitconfig = os.path.join(tmp, 'gitconfig')
        with open(gitconfig, 'w') as f:
            f.write('[init]\n\tdefaultBranch = master\n')
        base = {'PATH': os.environ.get('PATH', ''), 'HOME': tmp, 'GIT_CONFIG_GLOBAL': gitconfig,
                'GIT_CONFIG_NOSYSTEM': '1'}
        repo = os.path.join(tmp, 'r')
        subprocess.run(['git', 'init', '-q', repo], env=base, check=True)
        head = subprocess.run(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=repo, env=base,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, 'master')  # the control: the host setting is in effect
        repo2 = os.path.join(tmp, 'r2')
        env = hermetic.build(base, trunk='main')
        subprocess.run(['git', 'init', '-q', repo2], env=env, check=True)
        head = subprocess.run(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=repo2, env=env,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, 'main')


class WorkerModeTests(unittest.TestCase):
    """``mode='worker'``: an allow-list, not the base minus a deny-list (W6)."""

    BASE = {'PATH': '/bin', 'HOME': '/me', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'C', 'TERM': 'xterm',
            'TMPDIR': '/tmp/x', 'USER': 'op', 'SHELL': '/bin/zsh', 'FAKE_SECRET': 'x',
            'GH_TOKEN': 'ghp_x', 'HTTPS_PROXY': 'http://p', 'PYTHONPATH': '/pp',
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.extraheader',
            'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: x', **LEAKS}

    def test_only_the_allow_list_survives(self):
        env = hermetic.build(self.BASE, home='/homes/a', pythonpath=False, mode='worker')
        kept = {k for k in env if not k.startswith('GIT_CONFIG_')}
        self.assertEqual(kept, {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'USER', 'SHELL'})
        self.assertEqual(env['HOME'], '/homes/a')
        # the base's own git config (an auth header) is gone; only the pinned branch remains
        self.assertEqual((env['GIT_CONFIG_COUNT'], env['GIT_CONFIG_KEY_0']),
                         ('1', 'init.defaultBranch'))

    def test_passthrough_names_are_kept_and_home_stays_without_one_of_its_own(self):
        env = hermetic.build(self.BASE, pythonpath=False, mode='worker',
                             passthrough=('HTTPS_PROXY', 'NOT_SET'))
        self.assertEqual(env['HTTPS_PROXY'], 'http://p')
        self.assertNotIn('NOT_SET', env)
        self.assertEqual(env['HOME'], '/me')           # isolate_home: false
        self.assertNotIn('GH_TOKEN', env)

    def test_gate_mode_is_unchanged(self):
        env = hermetic.build(self.BASE)
        self.assertEqual(env['FAKE_SECRET'], 'x')
        self.assertEqual(env['HOME'], '/me')

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            hermetic.build(self.BASE, mode='other')


class OneBuilderTests(unittest.TestCase):
    """The gate and the worker session are the builder, not their own copies of its rules."""

    def test_the_harvest_gate_env_is_hermetic_build_with_the_worktree_first(self):
        base = dict(LEAKS, PATH='/bin', HOME='/me', PYTHONPATH='/base/pp')
        self.assertEqual(harvest.gate_env('/wt', base=base), hermetic.build(base, worktree='/wt'))

    def test_harvests_clean_env_is_hermetic_git_env(self):
        base = dict(LEAKS, PATH='/bin', HOME='/me')
        self.assertEqual(harvest.clean_env(base), hermetic.git_env(base))
        self.assertEqual(harvest.GIT_HOOK_VARS, hermetic.GIT_HOOK)

    def test_spawn_backgrounds_env_is_hermetic_build_with_the_package_parent_first(self):
        product = env_mod.Product('sample', {})
        with mock.patch.object(step_harvest.detach, 'spawn', return_value=123) as spawn:
            pid = step_harvest.spawn_background(product)
        self.assertEqual(pid, 123)
        self.assertEqual(spawn.call_args.kwargs['env'],
                         hermetic.build(worktree=hermetic.package_parent(),
                                        identity={'ASF_HOME': env_mod.ASF_HOME}))

    def test_the_worker_env_is_hermetic_build_with_the_jobs_identity(self):
        acct = pool_mod.Account('acct-a', home='/homes/a', config_dir='/cfg/a')
        job = runtime_mod.Job('sample', 'j1', '/wt', '/b.md', 'opus', account=acct,
                              env={'BACKLOG_ID_RANGE': 'S:5000-5049'})
        base = dict(LEAKS, PATH='/bin', HOME='/me', FAKE_SECRET='x')
        env = runtime_mod.build_env(job, base=base)
        self.assertEqual(env, dict(hermetic.build(base, home='/homes/a', pythonpath=False,
                                                  mode='worker'),
                                   CLAUDE_CONFIG_DIR='/cfg/a', ASF_PRODUCT='sample', ASF_JOB='j1',
                                   BACKLOG_ID_RANGE='S:5000-5049', ASF_HOME=env_mod.ASF_HOME))
        # what a session inherits from the tick never reaches it: its identity is its own
        self.assertEqual(env['ASF_JOB'], 'j1')
        self.assertNotIn('GIT_DIR', env)
        self.assertNotIn('FAKE_SECRET', env)

    def test_an_isolated_session_finds_the_factorys_own_home(self):
        # the session's HOME is its own, so ~/.ASF there is empty: without ASF_HOME the approvals
        # hook reads <session home>/.ASF/products/<p>.yaml, finds nothing, and refuses every tool
        acct = pool_mod.Account('acct-a', home='/homes/a', config_dir='/cfg/a')
        job = runtime_mod.Job('sample', 'j1', '/wt', '/b.md', 'opus', account=acct)
        env = runtime_mod.build_env(job, base={'PATH': '/bin', 'HOME': '/me'})
        self.assertEqual(env['HOME'], '/homes/a')
        self.assertEqual(env['ASF_HOME'], env_mod.ASF_HOME)

    def test_the_suite_runs_hermetic_too(self):
        # B-0043: the suite's home is never the operator's; the CI matrix runs it under env -i
        # with an operator-like ASF_HOME and ASF_PRODUCT set (see .github/workflows/tests.yml)
        from asf import env as env_mod
        self.assertNotEqual(os.path.realpath(env_mod.ASF_HOME),
                            os.path.realpath(os.path.expanduser('~/.ASF')))
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               '.github', 'workflows', 'tests.yml'), encoding='utf-8') as f:
            wf = f.read()
        self.assertIn('env -i', wf)
        self.assertIn('ASF_PRODUCT=asf', wf)
        self.assertIn('init.defaultBranch master', wf)

    def test_both_of_the_suites_entry_points_are_hermetic(self):
        # The suite has two guards — tests/__init__.py for `python -m unittest tests.<mod>` and
        # `discover -t .`, tests/test_00_home.py for `discover -s tests` — and every leak they
        # exist for was once fixed in one of them only: the operator's home (B-0043), the
        # caller's identity (B-0055), the caller's core.hooksPath (B-0114). Both are proved here
        # from a child carrying all three, so neither twin can be left behind again.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = dict(os.environ, PYTHONPATH=root, ASF_PRODUCT='live-product',
                    ASF_JOB='someone-elses-job', ASF_SESSION='s1', BACKLOG_ID_RANGE='S:1-2')
        base.pop('ASF_TESTS_HOME', None)  # else the home below is the caller's choice, not a temp
        base.pop('ASF_HOME', None)
        hermetic.strip_git_config(base)  # start clean, then plant exactly the pair under test
        hermetic._git_config(base, [('core.hooksPath', '/callers/githooks')])
        probe = ('import os, json;'
                 'from asf import env, hermetic;'
                 'print(json.dumps({'
                 '"git": [k.lower() for k, _ in hermetic.git_config_pairs(os.environ)],'
                 '"identity": [v for v in hermetic.CALLER_IDENTITY if v in os.environ],'
                 '"home": env.ASF_HOME,'
                 '"runtime": os.environ.get(hermetic.RUNTIME_CONFIG_DIR, "")}))')
        base[hermetic.RUNTIME_CONFIG_DIR] = os.path.join(tempfile.gettempdir(), 'callers-runtime')
        for entry in ('import tests', 'import tests.test_00_home'):
            out = subprocess.run([sys.executable, '-c', f'{entry}; {probe}'], cwd=root, env=base,
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            got = json.loads(out.stdout.splitlines()[-1])
            self.assertNotIn('core.hookspath', got['git'], entry)
            self.assertEqual(got['identity'], [], entry)
            self.assertNotEqual(os.path.realpath(got['home']),
                                os.path.realpath(os.path.expanduser('~/.ASF')), entry)
            self.assertTrue(got['runtime'].startswith(got['home']), entry)
        # Both entries above import tests/__init__.py first — Python cannot reach
        # tests.test_00_home without it — so the package guard masks its twin's, and this loop
        # alone stays green with tests/test_00_home.py's own strip deleted. `discover -s tests`
        # is the mode that twin exists for: it loads the module top-level (the ids it prints are
        # `test_00_home.HomeIsHermetic…`, not `tests.test_00_home…`) and never runs the package.
        # HomeIsHermetic asserts the same three things, so running it here pins the other twin.
        out = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                              '-p', 'test_00_home.py'], cwd=root, env=base,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


class OperatorFilesGuardTests(unittest.TestCase):
    """Both of the suite's entry points snapshot the operator's own files and exit
    :data:`asf.hermetic.LEAK_EXIT` when a process wrote a path of its own into one — here against
    a fake operator home (:data:`asf.hermetic.OPERATOR_HOME_VAR`), never the real one."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def setUp(self):
        self.op = tempfile.mkdtemp(prefix='asf-operator-')
        self.addCleanup(shutil.rmtree, self.op, True)
        os.makedirs(os.path.join(self.op, '.ASF', 'products'))
        with open(os.path.join(self.op, '.ASF', 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write('worker_pool:\n  accounts:\n    - name: a\n      config_dir: ~/workers/a\n')
        self.settings = os.path.join(self.op, 'workers', 'a', 'settings.json')
        os.makedirs(os.path.dirname(self.settings))
        with open(self.settings, 'w', encoding='utf-8') as f:
            f.write('{"theme": "dark"}\n')
        self.base = dict(os.environ, PYTHONPATH=self.ROOT, **{hermetic.OPERATOR_HOME_VAR: self.op})
        self.base.pop('ASF_TESTS_HOME', None)
        self.base.pop('ASF_HOME', None)

    def run_child(self, entry, write):
        code = (f'{entry}\nimport json, os, tempfile\n'
                f'path = {self.settings!r}\n{write}\n')
        return subprocess.run([sys.executable, '-c', code], cwd=self.ROOT, env=self.base,
                              capture_output=True, text=True)

    LEAK = ('json.dump({"extraKnownMarketplaces": {"asf": {"path": os.path.join('
            'tempfile.gettempdir(), "asf_install_e2e_x", "home", ".ASF", "plugin")}}}, '
            'open(path, "w"))')

    ENTRIES = ('import tests',
               'import sys; sys.path.insert(0, "tests"); import test_00_home')

    def test_the_operator_config_names_the_files_watched(self):
        files = hermetic.operator_files(self.op)
        self.assertIn(self.settings, files)
        self.assertIn(os.path.join(self.op, '.ASF', 'config.yaml'), files)

    def test_a_process_that_writes_a_suite_path_into_an_operator_file_fails(self):
        for entry in self.ENTRIES:
            with open(self.settings, 'w', encoding='utf-8') as f:
                f.write('{"theme": "dark"}\n')
            out = self.run_child(entry, self.LEAK)
            self.assertEqual(out.returncode, hermetic.LEAK_EXIT, entry + out.stderr)
            self.assertIn(self.settings, out.stderr, entry)

    def test_a_live_change_without_a_suite_path_and_no_change_both_pass(self):
        for entry in self.ENTRIES:
            for write in ('pass', 'open(path, "w").write(\'{"theme": "light"}\')'):
                out = self.run_child(entry, write)
                self.assertEqual(out.returncode, 0, entry + write + out.stderr)

    def test_an_earlier_leak_already_in_the_file_is_not_this_runs(self):
        leaked = self.run_child(self.ENTRIES[0], self.LEAK)
        self.assertEqual(leaked.returncode, hermetic.LEAK_EXIT, leaked.stderr)
        out = self.run_child(self.ENTRIES[0], 'open(path, "a").write("\\n")')
        self.assertEqual(out.returncode, 0, out.stderr)


class SuiteGitIdentityTests(unittest.TestCase):
    """F-0281: the suite's git identity never leaves its fixtures. Both entry points point git's
    global config at a suite-owned file holding ``Test <test@example.com>``; a repo-local identity
    is set only through ``gitfixture.identity``, which refuses a repo outside the temp roots; and a
    product repo's ``.git/config`` that gains the identity is a leak at exit."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_both_entry_points_own_the_global_git_config(self):
        for entry in OperatorFilesGuardTests.ENTRIES:
            code = (f'{entry}\nimport os, subprocess\n'
                    'print(os.environ["GIT_CONFIG_GLOBAL"])\n'
                    'print(os.environ["GIT_CONFIG_NOSYSTEM"])\n'
                    'print(subprocess.run(["git", "config", "--global", "user.email"], '
                    'capture_output=True, text=True).stdout.strip())\n')
            env = dict(os.environ, PYTHONPATH=self.ROOT)
            env.pop('GIT_CONFIG_GLOBAL', None)
            out = subprocess.run([sys.executable, '-c', code], cwd=self.ROOT, env=env,
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, entry + out.stderr)
            path, nosystem, email = out.stdout.split('\n')[:3]
            self.assertTrue(os.path.realpath(path).startswith(
                os.path.realpath(tempfile.gettempdir())), path)
            self.assertEqual(nosystem, '1')
            self.assertEqual(email, hermetic.SUITE_GIT_EMAIL)

    def test_a_fixture_commits_with_no_identity_of_its_own(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(['git', 'init', '-q', repo], check=True)
            subprocess.run(['git', '-C', repo, 'commit', '-q', '--allow-empty', '-m', 'x'],
                           check=True)
            author = subprocess.run(['git', '-C', repo, 'log', '-1', '--format=%an <%ae>'],
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(author, f'{hermetic.SUITE_GIT_NAME} <{hermetic.SUITE_GIT_EMAIL}>')
            with open(os.path.join(repo, '.git', 'config')) as f:
                self.assertNotIn('[user]', f.read())

    def test_identity_refuses_a_repo_outside_the_suite(self):
        from tests import gitfixture
        with self.assertRaises(gitfixture.OutsideSuite):
            gitfixture.identity(self.ROOT)  # the checkout under test is no fixture

    def test_identity_sets_it_in_a_temp_repo(self):
        from tests import gitfixture
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(['git', 'init', '-q', repo], check=True)
            gitfixture.identity(repo, 'Other', 'other@example.com')
            got = subprocess.run(['git', '-C', repo, 'config', '--local', 'user.email'],
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(got, 'other@example.com')

    def test_a_product_repos_git_config_that_gains_the_identity_is_a_leak(self):
        op = tempfile.mkdtemp(prefix='asf-operator-')
        self.addCleanup(shutil.rmtree, op, True)
        record = os.path.join(op, 'record')
        os.makedirs(os.path.join(record, '.git'))
        cfg = os.path.join(record, '.git', 'config')
        with open(cfg, 'w') as f:
            f.write('[core]\n\tbare = false\n')
        os.makedirs(os.path.join(op, '.ASF', 'products'))
        with open(os.path.join(op, '.ASF', 'products', 'p.yaml'), 'w') as f:
            f.write(f'product: p\nbacklog_dir: {record}\n')
        self.assertIn(cfg, hermetic.operator_files(op))
        before = hermetic.snapshot_operator_files(op)
        with open(cfg, 'a') as f:
            f.write(f'[user]\n\tname = Test\n\temail = {hermetic.SUITE_GIT_EMAIL}\n')
        self.assertEqual(hermetic.leaked_files(before, op), [cfg])
        # one that already carried it (an earlier leak) is not blamed on this run
        before = hermetic.snapshot_operator_files(op)
        with open(cfg, 'a') as f:
            f.write('\n')
        self.assertEqual(hermetic.leaked_files(before, op), [])

    #: ``git config user.*`` sites per test file — the ratchet (F-0281): a file may only go
    #: down, a new file starts at zero. A test that needs a repo-local author calls
    #: ``gitfixture.identity`` (it refuses a repo outside the suite); most need none, since the
    #: suite's global config carries one. The zero sweep is a later change.
    IDENTITY_SITES = {
        'test_backlog.py': 2,
        'test_brief_facts.py': 4,
        'test_check_generic.py': 2,
        'test_ci_cancels.py': 2,
        'test_clock_install.py': 6,
        'test_customer_content.py': 2,
        'test_drift.py': 2,
        'test_gitfixture.py': 2,
        'test_groom_adjudicate_reach.py': 2,
        'test_harvest.py': 6,
        'test_ingest.py': 2,
        'test_install.py': 12,
        'test_landing_stamp.py': 4,
        'test_lane_off_tick.py': 2,
        'test_operator_checkout_sync.py': 8,
        'test_path_resolution.py': 4,
        'test_product_checks.py': 2,
        'test_progress.py': 2,
        'test_readonly.py': 2,
        'test_redact.py': 6,
        'test_release_preview.py': 2,
        'test_sample_product.py': 4,
        'test_session_identity.py': 2,
        'test_shadow.py': 5,
        'test_tick.py': 4,
        'test_tick_steps.py': 2,
        'test_version.py': 6,
    }

    def test_no_new_git_identity_site(self):
        pat = re.compile(re.escape("'config', ") + r"'user\.")
        here = os.path.dirname(os.path.abspath(__file__))
        over = []
        for dirpath, _dirs, files in os.walk(here):
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, here)
                with open(path, encoding='utf-8') as f:
                    n = len(pat.findall(f.read()))
                if n > self.IDENTITY_SITES.get(rel, 0):
                    over.append(f'{rel}: {n} > {self.IDENTITY_SITES.get(rel, 0)}')
        self.assertEqual(over, [], 'a new `git config user.*` site in the suite — the suite\'s '
                                   'global config already sets an identity; if a test needs '
                                   'another author, call gitfixture.identity(repo, ...)')


class GitEnvTests(unittest.TestCase):
    """asf.hermetic.git_env — the whole environment for a ``git`` child: every variable a git
    hook exports gone, nothing else touched (F-0013 D4: the same list :func:`build` strips,
    strict widening from the three ``asf.harvest.harvest`` and ``asf.hermetic`` used to strip
    on their own)."""

    def test_every_hook_variable_is_stripped_and_nothing_else_is_touched(self):
        base = dict(LEAKS, PATH='/bin', HOME='/me', ASF_PRODUCT='p')
        env = hermetic.git_env(base)
        for var in hermetic.GIT_HOOK:
            self.assertNotIn(var, env, base)
        self.assertEqual(env['PATH'], '/bin')
        self.assertEqual(env['ASF_PRODUCT'], 'p')  # git_env strips no caller identity, no config

    def test_the_default_base_is_the_process_environment(self):
        with mock.patch.dict(os.environ, {'GIT_DIR': '/elsewhere/.git', 'PATH': '/bin'}):
            env = hermetic.git_env()
        self.assertNotIn('GIT_DIR', env)
        self.assertEqual(env['PATH'], '/bin')

    def test_the_base_is_not_mutated(self):
        base = {'GIT_DIR': '/x', 'PATH': '/bin'}
        hermetic.git_env(base)
        self.assertEqual(base, {'GIT_DIR': '/x', 'PATH': '/bin'})


if __name__ == '__main__':
    unittest.main()
