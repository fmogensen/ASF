"""The defects of the first pin attempt (2026-10-03), each pinned by a test so the retry holds:

1. a pinned render kept every non-asf PATH entry (Homebrew's prefix is a git checkout too) and
   refuses — NEEDS OPERATOR, exit 2, no plist — when a clock's tool does not resolve on it;
2. every agent home's ``$HOME/.local/bin/asf`` follows the operator's path through any move, is
   relinked by ``asf hooks install``, and the doctor names a home whose link does not resolve;
3. the redaction hooks asf writes run where no asf is installed (a cloud container): the
   repository's own check, else the commit or push is refused, loudly (a pushed branch is public
   before any landing re-scan); a commit marked by the trailer is still re-scanned at landing;
4. the move's smoke replays each written plist's exact env and interpreter (tests/test_upgrade
   covers the rollback it triggers).

Everything under temp dirs — a fake PATH, fake homes, fake venvs, throwaway git repos."""
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import dispatch, doctor, env, hooks, redact, scheduler
from asf.harvest import harvest as H
from asf.harvest import lane as lane_mod
from asf.scheduler import Clock
from asf.workers import githooks, runtime
from tests import test_scheduler as ts

RECORD = Clock('record', ['record'], False, 600, None)


def stub(bindir, name, body='exit 0'):
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, name)
    with open(path, 'w') as f:
        f.write(f'#!/bin/sh\n{body}\n')
    os.chmod(path, 0o755)
    return path


def git(cwd, *args, env_extra=None):
    e = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
             GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
    e.update(env_extra or {})
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=e)


# ---- 1. the rendered PATH ---------------------------------------------------------------------

class PinnedPathTest(ts.SchedulerTestCase):

    def setUp(self):
        super().setUp()
        venv = os.path.join(self.tmp, 'venvs', 'asf-factory-sample-abc1234')
        os.makedirs(os.path.join(venv, 'lib', 'python3.12', 'site-packages', 'asf'))
        stub(os.path.join(venv, 'bin'), 'python')
        state = os.path.join(self.asf_home, 'state', 'sample')
        os.makedirs(state)
        with open(os.path.join(state, 'install.json'), 'w') as f:
            f.write('{"sha": "%s", "venv": "%s", "policy": "pinned"}' % ('a' * 40, venv))
        # a tool prefix that is itself a git checkout (as Homebrew's is), holding gh
        self.brew = os.path.join(self.tmp, 'brew')
        os.makedirs(os.path.join(self.brew, '.git'))
        self.brew_bin = os.path.join(self.brew, 'bin')
        stub(self.brew_bin, 'gh')
        self.tools = os.path.join(self.tmp, 'tools')
        stub(self.tools, 'git')
        stub(self.tools, 'claude')

    def set_path(self, *dirs):
        os.environ['PATH'] = os.pathsep.join(dirs)

    def test_a_tool_prefix_that_is_a_git_checkout_stays_on_the_pinned_path(self):
        self.set_path(self.bindir, self.brew_bin, self.tools)
        job = scheduler.render('sample', RECORD)
        self.assertIn(self.brew_bin, job['plist']['EnvironmentVariables']['PATH'].split(':'))

    def test_a_pinned_render_refuses_when_gh_does_not_resolve(self):
        self.set_path(self.bindir, self.tools)
        with self.assertRaises(scheduler.SchedulerError) as caught:
            scheduler.render('sample', RECORD)
        self.assertIn('clock PATH lacks gh', str(caught.exception))

    def test_install_refuses_with_needs_operator_exit_2_and_writes_no_plist(self):
        self.write_product('  record:\n    steps: [record]\n    every: 5m\n')
        self.set_path(self.bindir, self.brew_bin)          # git and claude are missing
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = scheduler.main(['install', '--product', 'sample'])
        self.assertEqual(rc, 2)
        self.assertIn('NEEDS OPERATOR', out.getvalue())
        self.assertIn('git, claude', out.getvalue())
        agents = os.path.join(self.home, 'Library', 'LaunchAgents')
        self.assertFalse(os.path.isdir(agents) and os.listdir(agents))

    def test_the_product_lockfile_adds_its_toolchain(self):
        open(os.path.join(self.repo_dir, 'pnpm-lock.yaml'), 'w').close()
        product = env.load_product('sample')
        self.assertEqual(scheduler.required_tools(product, {}),
                         ['git', 'gh', 'claude', 'node', 'pnpm'])
        self.set_path(self.bindir, self.brew_bin, self.tools)
        with self.assertRaises(scheduler.SchedulerError) as caught:
            scheduler.render(product, RECORD)
        self.assertIn('node, pnpm', str(caught.exception))
        stub(self.tools, 'node')
        stub(self.tools, 'pnpm')
        scheduler.render(product, RECORD)

    def test_the_runtime_binary_comes_from_the_pool_and_a_fake_backend_needs_none(self):
        self.assertIn('/opt/rt/bin/agent', scheduler.required_tools(
            'sample', {'worker_pool': {'binary': '/opt/rt/bin/agent'}}))
        self.assertEqual(scheduler.required_tools('sample', {'worker_pool': {'backend': 'fake'}}),
                         ['git', 'gh'])


class SmokeTest(ts.SchedulerTestCase):
    """:func:`asf.scheduler.smoke` replays each written plist: its interpreter, its env, its cwd."""

    def write_plist(self, label, python, path_dirs, args=('tick',)):
        agents = os.path.join(self.home, 'Library', 'LaunchAgents')
        os.makedirs(agents, exist_ok=True)
        plist = {'Label': label, 'ProgramArguments': [python, '-m', 'asf.cli', *args],
                 'WorkingDirectory': self.tmp,
                 'EnvironmentVariables': {'PATH': os.pathsep.join(path_dirs), 'HOME': self.home,
                                          'ASF_HOME': self.asf_home}}
        with open(os.path.join(agents, f'{label}.plist'), 'wb') as f:
            plistlib.dump(plist, f)

    def setUp(self):
        super().setUp()
        self.tools = os.path.join(self.tmp, 'tools')
        self.log = os.path.join(self.tmp, 'calls.log')
        for name in ('git', 'gh', 'claude'):
            stub(self.tools, name, f'echo "{name} $* PATH=$PATH" >> {self.log}')
        self.python = stub(os.path.join(self.tmp, 'venv', 'bin'), 'python',
                           f'echo "python $3" >> {self.log}; '
                           f'[ "$1" = -m ] && echo "clock $*" >> {self.log}; exit 0')

    def test_every_command_runs_under_the_plists_own_env(self):
        self.write_plist('asf.sample.tick', self.python, [self.tools])
        ok, failures = scheduler.smoke('sample')
        self.assertEqual(failures, [])
        with open(self.log) as f:
            calls = f.read()
        self.assertIn('python sample', calls)
        self.assertIn('gh auth status --active', calls)
        self.assertIn('git --version', calls)
        self.assertIn(f'PATH={self.tools}', calls)      # the plist's PATH, not this process's
        self.assertEqual(len(ok), 5)

    def test_the_tick_clocks_own_command_runs_not_only_the_daily(self):
        """2026-10-04 canary: the daily and the tick plist share interpreter, env and cwd, so
        the smoke replayed the daily alone — a tick broken by its own argv would have resumed.
        Each clock's exact command runs in its no-op form: ``tick … --manifest``, ``ci queue``
        without ``--apply``."""
        self.write_plist('asf.sample.daily', self.python, [self.tools],
                         ('tick', '--product', 'sample', '--steps', 'daily'))
        self.write_plist('asf.sample.tick', self.python, [self.tools],
                         ('tick', '--product', 'sample', '--steps', 'record,wave'))
        self.write_plist('asf.sample.shadow', self.python, [self.tools],
                         ('tick', '--product', 'sample', '--shadow'))
        self.write_plist('asf.sample.ci-queue', self.python, [self.tools],
                         ('ci', 'queue', '--apply', '--product', 'sample'))
        ok, failures = scheduler.smoke('sample')
        self.assertEqual(failures, [])
        with open(self.log) as f:
            calls = [ln for ln in f.read().splitlines() if ln.startswith('clock ')]
        self.assertEqual(sorted(calls), sorted([
            'clock -m asf.cli tick --product sample --steps daily --manifest',
            'clock -m asf.cli tick --product sample --steps record,wave --manifest',
            'clock -m asf.cli tick --product sample --manifest',
            'clock -m asf.cli ci queue --product sample --help']))
        self.assertTrue(any(line.startswith('asf.sample.tick:') and '--manifest' in line
                            for line in ok), ok)

    def test_a_tick_whose_own_command_fails_is_a_failure(self):
        stub(os.path.join(self.tmp, 'venv', 'bin'), 'python',
             'case "$*" in *record,wave*) echo "bad step" >&2; exit 2;; esac; exit 0')
        self.write_plist('asf.sample.daily', self.python, [self.tools],
                         ('tick', '--product', 'sample', '--steps', 'daily'))
        self.write_plist('asf.sample.tick', self.python, [self.tools],
                         ('tick', '--product', 'sample', '--steps', 'record,wave'))
        _ok, failures = scheduler.smoke('sample')
        self.assertEqual(len(failures), 1, failures)
        self.assertIn('asf.sample.tick:', failures[0])
        self.assertIn('--manifest exit 2 bad step', failures[0])

    def test_a_tool_missing_from_the_plist_path_or_failing_is_a_failure(self):
        stub(self.tools, 'gh', 'echo "not logged in" >&2; exit 1')
        os.remove(os.path.join(self.tools, 'claude'))
        self.write_plist('asf.sample.tick', self.python, [self.tools])
        _ok, failures = scheduler.smoke('sample')
        self.assertTrue(any('claude not on the plist PATH' in x for x in failures), failures)
        self.assertTrue(any('gh auth status --active exit 1 not logged in' in x
                            for x in failures), failures)

    def test_no_plist_on_disk_is_a_failure(self):
        _ok, failures = scheduler.smoke('sample')
        self.assertEqual(failures, ['no clock plist of sample on disk'])


# ---- 2. agent homes ---------------------------------------------------------------------------

class AgentHomesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-homes-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.object(env, 'ASF_HOME', os.path.join(self.tmp, 'ASF'))
        patch.start()
        self.addCleanup(patch.stop)
        self.op = os.path.join(self.tmp, 'op')
        self.venv1 = stub(os.path.join(self.tmp, 'venv1', 'bin'), 'asf')
        self.op_cli = os.path.join(self.op, '.local', 'bin', 'asf')
        os.makedirs(os.path.dirname(self.op_cli))
        os.symlink(self.venv1, self.op_cli)                 # a pipx-style link into a venv
        self.homes = [os.path.join(runtime.homes_dir(), n) for n in ('a', 'b')]
        for h in self.homes:
            os.makedirs(h)

    def test_the_link_names_the_operators_path_and_survives_a_move(self):
        link = runtime.link_factory_cli(self.homes[0], self.op)
        self.assertEqual(os.readlink(link), self.op_cli)
        venv2 = stub(os.path.join(self.tmp, 'venv2', 'bin'), 'asf')
        os.remove(self.op_cli)
        os.symlink(venv2, self.op_cli)                      # the move: a new venv, the old gone
        shutil.rmtree(os.path.join(self.tmp, 'venv1'))
        self.assertEqual(os.path.realpath(link), venv2)
        self.assertEqual(runtime.home_cli_problems(), [(self.homes[1], 'no .local/bin/asf')])

    def test_link_all_homes_links_every_home_and_replaces_a_dangling_link(self):
        bad = os.path.join(self.homes[1], '.local', 'bin', 'asf')
        os.makedirs(os.path.dirname(bad))
        os.symlink(os.path.join(self.tmp, 'gone', 'bin', 'asf'), bad)
        self.assertEqual([h for h, _why in runtime.home_cli_problems()], self.homes)
        linked = runtime.link_all_homes(self.op)
        self.assertEqual([h for h, link in linked if link], self.homes)
        self.assertEqual(runtime.home_cli_problems(), [])

    def test_the_doctor_row_is_red_naming_the_home_then_green(self):
        (required, ok, detail), = doctor.check_agent_homes()
        self.assertEqual((required, ok), (True, False))
        self.assertIn('a: no .local/bin/asf', detail)
        runtime.link_all_homes(self.op)
        (required, ok, detail), = doctor.check_agent_homes()
        self.assertEqual((required, ok), (True, True), detail)
        (required, ok, _d), = doctor.check_agent_homes({'worker_pool': {'backend': 'fake'}})
        self.assertEqual((required, ok), (False, None))   # no agent session runs a hook

    def test_hooks_install_with_the_dispatcher_links_every_home(self):
        from asf import dispatch
        from asf.env import Product
        repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'init', '-q', repo], check=True)
        product = Product('demo', env.loads(f'product: demo\nrepo_dir: {repo}\n'))
        cfg = {'worker_pool': {'accounts': [{'name': 'a',
                                             'config_dir': os.path.join(self.tmp, 'acct')}]}}
        dispatcher = dispatch.default_path(self.op)
        with mock.patch.object(dispatch, 'install', return_value=(0, 'dispatcher: kept')):
            rc, msg = hooks.install(product, rules_dir=os.path.join(self.tmp, 'none'),
                                    which=lambda n: self.op_cli, cfg=cfg, dispatcher=dispatcher)
        self.assertEqual(rc, 0, msg)
        self.assertIn('agent homes: 2/2 linked', msg)
        for h in self.homes:
            self.assertEqual(os.readlink(os.path.join(h, '.local', 'bin', 'asf')), dispatcher)


# ---- 3. the redaction hooks where no asf is installed -----------------------------------------

class CloudSafeHooksTest(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-cloudhook-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.hooks_dir = os.path.join(self.repo, '.githooks')    # tracked, as a product keeps it
        os.makedirs(self.hooks_dir)
        git(self.repo, 'config', 'core.hooksPath', '.githooks')
        self.home = os.path.join(self.tmp, 'cloud-home')
        os.makedirs(self.home)
        for name in hooks.GIT_HOOK_NAMES:
            path = os.path.join(self.hooks_dir, name)
            with open(path, 'w') as f:
                f.write(hooks._git_hook_body(name, '/operator/.local/bin/asf', 'demo'))
            os.chmod(path, 0o755)

    def commit(self, name='f.txt'):
        with open(os.path.join(self.repo, name), 'w') as f:
            f.write('x\n')
        git(self.repo, 'add', name)
        return git(self.repo, 'commit', '-qm', 'add', env_extra={
            'HOME': self.home, 'PATH': '/usr/bin:/bin'})

    def test_the_body_is_still_recognised_as_asfs(self):
        for name in hooks.GIT_HOOK_NAMES:
            body = hooks._git_hook_body(name, '/operator/.local/bin/asf', 'demo')
            self.assertTrue(hooks.is_git_hook_ours(body, name))
            self.assertIn(f'exec "/operator/.local/bin/asf" redact --{name} --product demo', body)

    def test_no_asf_and_no_repo_check_refuses_the_commit_loudly(self):
        p = self.commit()
        self.assertNotEqual(p.returncode, 0, p.stderr)
        self.assertIn('REDACTION REFUSED (pre-commit)', p.stderr)
        self.assertEqual(git(self.repo, 'rev-list', '--all').stdout.strip(), '')  # nothing made
        flag = git(self.repo, 'rev-parse', '--git-path', redact.UNCHECKED_FLAG).stdout.strip()
        self.assertFalse(os.path.isfile(os.path.join(self.repo, flag)))

    def test_no_asf_and_no_repo_check_refuses_the_push_loudly(self):
        hook = os.path.join(self.hooks_dir, 'pre-push')
        p = subprocess.run(['sh', hook, 'origin', 'x'], cwd=self.repo, input='', text=True,
                           capture_output=True, env={'HOME': self.home, 'PATH': '/usr/bin:/bin'})
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn('REDACTION REFUSED (pre-push)', p.stderr)

    def test_the_repositorys_own_check_runs_and_can_refuse(self):
        stub(os.path.join(self.repo, 'tools', 'checks'), 'redact.sh',
             'echo "repo check $*" >&2; exit 1')
        p = self.commit()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('repo check --pre-commit', p.stderr)
        self.assertNotIn('REDACTION REFUSED', p.stderr)

    def test_an_agent_homes_asf_is_taken_first(self):
        stub(os.path.join(self.home, '.local', 'bin'), 'asf', 'echo "home asf $*" >&2; exit 0')
        p = self.commit()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('home asf redact --pre-commit --product demo', p.stderr)

    def test_the_session_wrapper_turns_the_flag_into_the_trailer(self):
        hook = os.path.join(self.tmp, 'asf-hook')
        with open(hook, 'w') as f:
            f.write(githooks.ASF_HOOK)
        os.chmod(hook, 0o755)
        flag = os.path.join(self.repo, '.git', redact.UNCHECKED_FLAG)
        with open(flag, 'w') as f:
            f.write('2026-10-03T00:00:00Z\n')
        msg = os.path.join(self.tmp, 'MSG')
        with open(msg, 'w') as f:
            f.write('fix: a thing\n')
        clean = {k: v for k, v in os.environ.items() if not k.startswith('ASF_')}
        subprocess.run(['sh', hook, 'commit-msg', msg], cwd=self.repo, env=clean, check=True,
                       capture_output=True)
        with open(msg) as f:
            self.assertIn(redact.UNCHECKED_TRAILER, f.read())
        self.assertFalse(os.path.exists(flag))
        self.assertIn('asf-redaction-unchecked', githooks.ASF_HOOK)   # the names stay in step
        self.assertIn(f'"{redact.UNCHECKED_TRAILER}"', githooks.ASF_HOOK)


# ---- 5. the hook checks the asf it is about to exec while its product is pinned (F-0283) ------

class PinGuardHookBodyTest(unittest.TestCase):
    """S-76254: while a product is pinned, a hook written for it runs a candidate ``asf`` only
    when that file carries the dispatcher's marker (F-0283); unpinned, the body is inert and
    behaves exactly as before. Reuses :class:`CloudSafeHooksTest`'s fixture — a throwaway repo
    with a tracked ``.githooks`` as ``core.hooksPath`` and a fake ``HOME`` — plus a fake
    ``ASF_HOME`` so ``ensure_git_hooks`` can be driven across a pin being added and removed."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-pinguard-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.hooks_dir = os.path.join(self.repo, '.githooks')
        os.makedirs(self.hooks_dir)
        git(self.repo, 'config', 'core.hooksPath', '.githooks')
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.asf_home = os.path.join(self.tmp, 'ASF')
        os.makedirs(self.asf_home)
        patch = mock.patch.object(env, 'ASF_HOME', self.asf_home)
        patch.start()
        self.addCleanup(patch.stop)
        self.product = env.Product('demo', {'repo_dir': self.repo})

    def dispatcher(self, path, tag='DISPATCHER'):
        """A file that reads as the dispatcher's to :func:`asf.hooks._git_hook_body`'s guard —
        built from :func:`asf.dispatch.render`'s own first three lines (where the marker lands,
        P7), never a hand-copied marker string — and otherwise just echoes its argv."""
        head = dispatch.render('/x', '/y', '', '', '/usr/bin/python3').splitlines()[:3]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('\n'.join(head) + f'\necho "{tag} $*" >&2\nexit 0\n')
        os.chmod(path, 0o755)
        return path

    def write_hook(self, name, asf_path, pin_required, product='demo'):
        path = os.path.join(self.hooks_dir, name)
        with open(path, 'w') as f:
            f.write(hooks._git_hook_body(name, asf_path, product, pin_required))
        os.chmod(path, 0o755)
        return path

    def run_hook(self, path):
        return subprocess.run(['sh', path], cwd=self.repo, input='', text=True,
                              capture_output=True, env={'HOME': self.home, 'PATH': '/usr/bin:/bin'})

    def pin(self, product='demo'):
        state = os.path.join(self.asf_home, 'state', product)
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, 'install.json'), 'w') as f:
            f.write('{"sha": "%s", "venv": "%s"}' % ('a' * 40, os.path.join(self.tmp, 'venv')))

    def unpin(self, product='demo'):
        shutil.rmtree(os.path.join(self.asf_home, 'state', product), ignore_errors=True)

    # ---- P12: the four host states a pinned hook runs under ----

    def test_a_dispatcher_at_the_path_runs(self):
        asf_path = self.dispatcher(os.path.join(self.tmp, 'opt', 'asf'))
        path = self.write_hook('pre-commit', asf_path, True)
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('DISPATCHER redact --pre-commit --product demo', p.stderr)

    def test_a_non_dispatcher_with_the_repos_own_check_warns_once_and_runs_it(self):
        shared = stub(os.path.join(self.tmp, 'shared'), 'asf')
        stub(os.path.join(self.repo, 'tools', 'checks'), 'redact.sh',
             'echo "repo check $*" >&2; exit 0')
        path = self.write_hook('pre-commit', shared, True)
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr.count("is not asf's dispatcher"), 1, p.stderr)
        self.assertIn('repo check --pre-commit', p.stderr)
        self.assertNotIn('REDACTION REFUSED', p.stderr)

    def test_a_non_dispatcher_with_no_repo_check_warns_then_refuses(self):
        shared = stub(os.path.join(self.tmp, 'shared'), 'asf')
        path = self.write_hook('pre-commit', shared, True)
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stderr.count("is not asf's dispatcher"), 1, p.stderr)
        self.assertIn("REDACTION REFUSED (pre-commit) — no asf that is demo's build and no "
                      "tools/checks/redact.sh here", p.stderr)

    def test_no_asf_at_all_refuses_with_the_standing_line(self):
        path = self.write_hook('pre-push', os.path.join(self.tmp, 'gone', 'asf'), True)
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("REDACTION REFUSED (pre-push) — no asf that is demo's build and no "
                      "tools/checks/redact.sh here", p.stderr)
        self.assertNotIn("is not asf's dispatcher", p.stderr)  # no file there: nothing to warn about

    # ---- C4: at most one warning line per hook run ----

    def test_both_candidates_resolving_to_the_same_file_warn_once(self):
        shared = os.path.join(self.home, '.local', 'bin', 'asf')
        stub(os.path.dirname(shared), os.path.basename(shared))
        path = self.write_hook('pre-commit', shared, True)
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 1, p.stderr)  # no redact.sh here either: refused
        self.assertEqual(p.stderr.count("is not asf's dispatcher"), 1, p.stderr)

    def test_a_home_symlink_to_the_dispatcher_passes_with_no_warning(self):
        real = self.dispatcher(os.path.join(self.tmp, 'opt', 'asf'))
        link = os.path.join(self.home, '.local', 'bin', 'asf')
        os.makedirs(os.path.dirname(link))
        os.symlink(real, link)
        path = self.write_hook('pre-commit', '/operator/.local/bin/asf', True)  # absent: silent
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("is not asf's dispatcher", p.stderr)
        self.assertIn('DISPATCHER redact --pre-commit --product demo', p.stderr)

    # ---- C5: the unpinned body is inert ----

    def test_an_unpinned_body_runs_a_non_dispatcher_unchanged(self):
        shared = stub(os.path.join(self.tmp, 'shared'), 'asf')
        path = self.write_hook('pre-push', shared, False)
        p = self.run_hook(path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("is not asf's dispatcher", p.stderr)

    # ---- P11: the recognisers read both bodies as they read the old one ----

    def test_the_recognisers_read_both_pin_states_of_both_bodies(self):
        for pin_required in (True, False):
            for name in hooks.GIT_HOOK_NAMES:
                body = hooks._git_hook_body(name, '/opt/p/bin/asf', 'demo', pin_required)
                self.assertTrue(hooks.is_git_hook_ours(body, name), (pin_required, name))
                self.assertEqual(hooks.hook_entry(body, name), '/opt/p/bin/asf',
                                 (pin_required, name))
                self.assertIsNone(hooks.init_hook_upgrade(body, name), (pin_required, name))
                self.assertIsNone(hooks.staged_check_upgrade(body, name), (pin_required, name))

    # ---- stale_own_hook across a pin added and removed ----

    def test_stale_own_hook_rewrites_across_a_pin_added_and_removed(self):
        asf_path = self.dispatcher(os.path.join(self.tmp, 'opt', 'asf'))
        unpinned = hooks._git_hook_body('pre-push', asf_path, 'demo', False)
        pinned = hooks._git_hook_body('pre-push', asf_path, 'demo', True)
        self.assertNotEqual(unpinned, pinned)
        self.assertEqual(hooks.stale_own_hook(unpinned, 'pre-push', asf_path, 'demo', True), pinned)
        self.assertEqual(
            hooks.stale_own_hook(pinned, 'pre-push', asf_path, 'demo', False), unpinned)
        self.assertIsNone(hooks.stale_own_hook(pinned, 'pre-push', asf_path, 'demo', True))
        self.assertIsNone(hooks.stale_own_hook(unpinned, 'pre-push', asf_path, 'demo', False))

    # ---- ensure_git_hooks bakes the guard exactly when state/<p>/install.json exists ----

    def test_ensure_git_hooks_bakes_the_guard_exactly_when_pinned(self):
        asf_path = self.dispatcher(os.path.join(self.tmp, 'opt', 'asf'))
        path = os.path.join(self.hooks_dir, 'pre-commit')

        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path)
        self.assertTrue(rc, detail)
        with open(path) as f:
            unpinned_text = f.read()
        self.assertIn("pin_marker=''", unpinned_text)
        self.assertEqual(unpinned_text, hooks._git_hook_body('pre-commit', asf_path, 'demo', False))

        self.pin()
        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path)
        self.assertTrue(rc, detail)
        with open(path) as f:
            pinned_text = f.read()
        self.assertNotIn("pin_marker=''", pinned_text)
        self.assertEqual(pinned_text, hooks._git_hook_body('pre-commit', asf_path, 'demo', True))

        # a second run, still pinned: changes nothing
        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path)
        self.assertTrue(rc, detail)
        with open(path) as f:
            self.assertEqual(f.read(), pinned_text)

        self.unpin()
        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path)
        self.assertTrue(rc, detail)
        with open(path) as f:
            self.assertEqual(f.read(), unpinned_text)

        # a second run, still unpinned: changes nothing
        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path)
        self.assertTrue(rc, detail)
        with open(path) as f:
            self.assertEqual(f.read(), unpinned_text)

    def test_a_foreign_hook_is_left_untouched_and_refused_even_when_pinned(self):
        self.pin()
        path = os.path.join(self.hooks_dir, 'pre-commit')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\necho mine\n')
        os.chmod(path, 0o755)
        asf_path = self.dispatcher(os.path.join(self.tmp, 'opt', 'asf'))
        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path)
        self.assertFalse(rc)
        self.assertIn('NEEDS OPERATOR', detail)
        with open(path) as f:
            self.assertEqual(f.read(), '#!/bin/sh\necho mine\n')

    def test_a_withheld_path_is_still_skipped_when_pinned(self):
        self.pin()
        asf_path = self.dispatcher(os.path.join(self.tmp, 'opt', 'asf'))
        path = os.path.join(self.hooks_dir, 'pre-commit')
        rc, detail = hooks.ensure_git_hooks(self.product, which=lambda n: asf_path,
                                            withhold=(path,))
        self.assertTrue(rc, detail)
        self.assertFalse(os.path.exists(path))


class LandingRecheckTest(unittest.TestCase):
    """A branch carrying ``Redaction: unchecked`` is re-scanned before it merges."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-recheck-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = os.path.join(self.tmp, 'origin.git')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', origin], check=True)
        self.repo = os.path.join(self.tmp, 'repo')
        subprocess.run(['git', 'clone', '-q', origin, self.repo], check=True,
                       capture_output=True)
        git(self.repo, 'checkout', '-q', '-b', 'main')
        self.write('README', 'hello\n')
        git(self.repo, 'commit', '-qam', 'init')
        git(self.repo, 'push', '-q', 'origin', 'main')
        git(self.repo, 'checkout', '-q', '-b', 'task/t-1')
        self.lines = []
        self.lane = mock.Mock(repo=self.repo, trunk='main', out=self.lines.append, results={})
        pats = [redact.Pattern('name', 'test', re.compile('SECRETWORD'))]
        patch = mock.patch.object(redact, 'patterns', return_value=pats)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, text):
        with open(os.path.join(self.repo, name), 'w') as f:
            f.write(text)
        git(self.repo, 'add', name)

    def head(self, text, trailer=True):
        self.write('f.txt', text)
        msg = 'task(T-1): change' + (f'\n\n{redact.UNCHECKED_TRAILER}' if trailer else '')
        git(self.repo, 'commit', '-qm', msg)
        return git(self.repo, 'rev-parse', 'HEAD').stdout.strip()

    def recheck(self, head):
        f = {'branch': 'task/t-1', 'head': head}
        with mock.patch.object(lane_mod, 'send_back') as back, \
                mock.patch.object(lane_mod, 'wait') as wait:
            res = lane_mod.redaction_recheck(self.lane, f)
        return res, back, wait

    def test_an_unmarked_branch_is_not_scanned_again(self):
        head = self.head('SECRETWORD\n', trailer=False)
        with mock.patch.object(H, 'scan_beyond_trunk') as scan:
            res, _back, _wait = self.recheck(head)
        self.assertIsNone(res)
        scan.assert_not_called()

    def test_a_marked_clean_branch_merges_and_says_so(self):
        head = self.head('fine\n')
        self.assertEqual(redact.unchecked_commits(self.repo, 'origin/main', head), [head])
        res, back, _wait = self.recheck(head)
        self.assertIsNone(res)
        back.assert_not_called()
        self.assertTrue(any('re-scan clean' in x for x in self.lines), self.lines)

    def test_a_marked_branch_with_a_finding_is_sent_back(self):
        head = self.head('SECRETWORD\n')
        res, back, _wait = self.recheck(head)
        self.assertEqual(res, 'held')
        (lane, f, kind, text, files), kw = back.call_args
        self.assertEqual(kind, 'redact')
        self.assertIn('redaction: 1 finding', text)
        self.assertNotIn('SECRETWORD', text)                 # never the matched text
        self.assertEqual(files, ['f.txt'])
        self.assertEqual(kw, {'rebase': False})

    def test_an_unreadable_range_holds_rather_than_merges(self):
        res, _back, wait = self.recheck('f' * 40)
        self.assertEqual(res, 'held')
        self.assertIn('redaction re-check failed', wait.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
