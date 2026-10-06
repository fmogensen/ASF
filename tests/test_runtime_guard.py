"""tests/test_runtime_guard.py — the runtime CLI never writes into a worker account's live config
dir from a child that moved HOME.

2026-10-06: a review session ran the suite of a branch cut before the install e2e dropped the
inherited ``CLAUDE_CONFIG_DIR``; that e2e's ``asf install`` found the real runtime CLI and its
``plugin marketplace add`` wrote the test's temp plugin path into the account's live settings
file. The branch's own code cannot be fixed after the fact, so the session's environment is:
:func:`asf.workers.runtime.guard_env` puts a guard first on every session's ``PATH``, and
``asf install`` hands the runtime CLI :func:`asf.hermetic.runtime_env`.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import hermetic, install
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

#: A stand-in runtime CLI: prints the config dir it was given and the HOME it ran under.
REPORTER = '#!/bin/sh\necho "ccd=${CLAUDE_CONFIG_DIR:-} home=${HOME:-} argv=$*"\n'


def _bin(directory, name, text):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-guard-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.real = _bin(os.path.join(self.tmp, 'bin'), 'agent', REPORTER)
        self.session_home = os.path.join(self.tmp, 'session')
        self.cfg = os.path.join(self.tmp, 'workers', 'a')     # the account's live config dir
        os.makedirs(self.session_home)
        os.makedirs(self.cfg)
        homes = mock.patch.object(runtime_mod, 'homes_dir',
                                  return_value=os.path.join(self.tmp, 'homes'))
        homes.start()
        self.addCleanup(homes.stop)
        self.job_env = {'PATH': os.pathsep.join([os.path.dirname(self.real), '/usr/bin', '/bin']),
                        'HOME': self.session_home, hermetic.RUNTIME_CONFIG_DIR: self.cfg}

    def call_by_name(self, env, home=None):
        """A child of the session calling the runtime by name — under its own HOME if given."""
        env = dict(env, HOME=home) if home else env
        out = subprocess.run(['sh', '-c', 'agent plugin marketplace add /x'], env=env,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_a_child_that_moved_home_never_reaches_the_accounts_config_dir(self):
        guarded, binary = runtime_mod.guard_env(self.job_env, 'agent')
        temp_home = os.path.join(self.tmp, 'suite', 'home')
        # without the guard (today's leak): the moved HOME still writes the live config dir
        self.assertIn(f'ccd={self.cfg} ', self.call_by_name(self.job_env, home=temp_home))
        # with it: the runtime runs under the child's HOME and no config dir of the session's
        self.assertEqual(self.call_by_name(guarded, home=temp_home),
                         f'ccd= home={temp_home} argv=plugin marketplace add /x')

    def test_the_session_and_its_own_children_keep_the_config_dir(self):
        guarded, binary = runtime_mod.guard_env(self.job_env, 'agent')
        self.assertEqual(binary, self.real)    # the session itself runs the real binary
        self.assertEqual(guarded[runtime_mod.SESSION_HOME_VAR], self.session_home)
        self.assertTrue(guarded['PATH'].startswith(
            os.path.join(self.tmp, 'homes', runtime_mod.GUARD_DIR) + os.pathsep))
        self.assertIn(f'ccd={self.cfg} home={self.session_home}', self.call_by_name(guarded))

    def test_a_guard_on_path_already_is_never_resolved_as_the_runtime(self):
        guarded, _ = runtime_mod.guard_env(self.job_env, 'agent')
        again, binary = runtime_mod.guard_env(guarded, 'agent')
        self.assertEqual(binary, self.real)
        self.assertEqual(again['PATH'], guarded['PATH'])

    def test_no_config_dir_or_no_binary_leaves_the_env_alone(self):
        bare = {k: v for k, v in self.job_env.items() if k != hermetic.RUNTIME_CONFIG_DIR}
        self.assertEqual(runtime_mod.guard_env(bare, 'agent'), (bare, 'agent'))
        self.assertEqual(runtime_mod.guard_env(self.job_env, 'no-such-runtime'),
                         (self.job_env, 'no-such-runtime'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'homes', runtime_mod.GUARD_DIR,
                                                     'no-such-runtime')))

    def test_a_launch_runs_the_session_through_the_guarded_env(self):
        # the session sees its config dir; `agent` by name inside it is the guard
        _bin(os.path.dirname(self.real), 'agent',
             '#!/bin/sh\ncat >/dev/null\n'
             'echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",'
             '\\"result\\":\\"$CLAUDE_CONFIG_DIR|$(command -v agent)\\"}"\n')
        brief = os.path.join(self.tmp, 'b.md')
        with open(brief, 'w', encoding='utf-8') as f:
            f.write('do it\n')
        acct = pool_mod.Account('a', home=self.session_home, config_dir=self.cfg)
        job = runtime_mod.Job('sample', 'j1', self.tmp, brief, 'opus', account=acct,
                              log_path=os.path.join(self.tmp, 'j1.jsonl'))
        with mock.patch.dict(os.environ, PATH=self.job_env['PATH']):
            r = runtime_mod.ClaudeCodeRuntime(binary='agent').run(job, wait=True)
        self.assertTrue(r.ok, r.text)
        self.assertEqual(r.text, self.cfg + '|' + os.path.join(
            self.tmp, 'homes', runtime_mod.GUARD_DIR, 'agent'))


class InstallRuntimeEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-guard-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, 'calls.log')
        self.claude = _bin(os.path.join(self.tmp, 'bin'), 'claude',
                           f'#!/bin/sh\necho "ccd=${{CLAUDE_CONFIG_DIR:-}}" >> {self.log}\n')
        self.home = os.path.join(self.tmp, 'home')

    def tail(self, ccd):
        with mock.patch.dict(os.environ, {'HOME': self.home, hermetic.RUNTIME_CONFIG_DIR: ccd}), \
                mock.patch.object(install, '_probe_plugin_command', return_value=self.claude):
            install._tail_lines(os.path.join(self.home, '.ASF', 'plugin'))
        with open(self.log, encoding='utf-8') as f:
            lines = f.read().split()
        os.remove(self.log)
        return lines

    def test_the_plugin_install_never_writes_a_config_dir_outside_the_runs_home(self):
        # a test's temp HOME with a worker session's config dir inherited: dropped
        self.assertEqual(self.tail(os.path.join(self.tmp, 'live', 'workers', 'a')),
                         ['ccd=', 'ccd='])

    def test_a_config_dir_under_the_runs_home_is_the_operators_choice(self):
        own = os.path.join(self.home, '.claude-a')
        self.assertEqual(self.tail(own), [f'ccd={own}', f'ccd={own}'])

    def test_runtime_config_dir_owned(self):
        self.assertTrue(hermetic.runtime_config_dir_owned({'HOME': '/h'}))
        self.assertTrue(hermetic.runtime_config_dir_owned(
            {'HOME': '/h', hermetic.RUNTIME_CONFIG_DIR: '/h/.claude-x'}))
        self.assertFalse(hermetic.runtime_config_dir_owned(
            {'HOME': '/tmp/t/home', hermetic.RUNTIME_CONFIG_DIR: '/h/.claude-x'}))
        self.assertFalse(hermetic.runtime_config_dir_owned(
            {'HOME': '/hx', hermetic.RUNTIME_CONFIG_DIR: '/hxy/.c'}))


if __name__ == '__main__':
    unittest.main()
