"""asf.hermetic — the one environment builder (F-0087, class "environment leaking into gates and
tests": B-0033, B-0038, B-0043, B-0047). The harvest gate, a worker session and the suite's own
subprocesses all go through :func:`asf.hermetic.build`; these tests pin what it strips, what it
pins and what it puts first — under every base environment a generator can throw at it."""
import itertools
import os
import random
import shutil
import subprocess
import tempfile
import unittest

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


class OneBuilderTests(unittest.TestCase):
    """The gate and the worker session are the builder, not their own copies of its rules."""

    def test_the_harvest_gate_env_is_hermetic_build_with_the_worktree_first(self):
        base = dict(LEAKS, PATH='/bin', HOME='/me', PYTHONPATH='/base/pp')
        self.assertEqual(harvest.gate_env('/wt', base=base), hermetic.build(base, worktree='/wt'))

    def test_the_worker_env_is_hermetic_build_with_the_jobs_identity(self):
        acct = pool_mod.Account('acct-a', home='/homes/a', config_dir='/cfg/a')
        job = runtime_mod.Job('sample', 'j1', '/wt', '/b.md', 'opus', account=acct,
                              env={'BACKLOG_ID_RANGE': 'S:5000-5049'})
        base = dict(LEAKS, PATH='/bin', HOME='/me')
        env = runtime_mod.build_env(job, base=base)
        self.assertEqual(env, dict(hermetic.build(base, home='/homes/a', pythonpath=False),
                                   CLAUDE_CONFIG_DIR='/cfg/a', ASF_PRODUCT='sample', ASF_JOB='j1',
                                   BACKLOG_ID_RANGE='S:5000-5049'))
        # what a session inherits from the tick never reaches it: its identity is its own
        self.assertEqual(env['ASF_JOB'], 'j1')
        self.assertNotIn('GIT_DIR', env)

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


if __name__ == '__main__':
    unittest.main()
