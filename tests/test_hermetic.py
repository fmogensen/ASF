"""asf.hermetic — the one environment builder (F-0087, class "environment leaking into gates and
tests": B-0033, B-0038, B-0043, B-0047). The harvest gate, a worker session and the suite's own
subprocesses all go through :func:`asf.hermetic.build`; these tests pin what it strips, what it
pins and what it puts first — under every base environment a generator can throw at it."""
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import env as env_mod

from asf import hermetic
from asf.harvest import harvest
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

LEAKS = {'ASF_PRODUCT': 'live-product', 'ASF_JOB': 'someone-elses-job',
         'BACKLOG_ID_RANGE': 'S:1-2', 'GIT_DIR': '/elsewhere/.git',
         'GIT_WORK_TREE': '/elsewhere', 'GIT_INDEX_FILE': '/elsewhere/index'}


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
                 '"home": env.ASF_HOME}))')
        for entry in ('import tests', 'import tests.test_00_home'):
            out = subprocess.run([sys.executable, '-c', f'{entry}; {probe}'], cwd=root, env=base,
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            got = json.loads(out.stdout.splitlines()[-1])
            self.assertNotIn('core.hookspath', got['git'], entry)
            self.assertEqual(got['identity'], [], entry)
            self.assertNotEqual(os.path.realpath(got['home']),
                                os.path.realpath(os.path.expanduser('~/.ASF')), entry)
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


if __name__ == '__main__':
    unittest.main()
