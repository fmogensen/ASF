"""W6 of the 0.1.3 fix package — a worker session is isolated from the operator: its environment
is an allow-list (not the tick's minus a deny-list), its HOME is its account's own, seeded only
from ``home_seed``, the doctor's ``worker env`` row is red on a leak, and the product's
``worktree_setup`` runs in every fresh worktree under that same environment.

Every session here is a real process (a stand-in binary that dumps its environment), launched
through :func:`asf.workers.spawn.spawn` the way a tick launches one."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import doctor, env, hermetic
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod

from tests.test_workers import Home, s1_row


def _dump_binary(directory, dump):
    """A runtime stand-in: reads the brief, writes its whole environment to ``dump``, reports."""
    path = os.path.join(directory, 'agent')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\ncat >/dev/null\n'
                f'env > "{dump}.tmp" && mv "{dump}.tmp" "{dump}"\n'
                'echo \'{"type":"result","subtype":"success","result":"ok"}\'\n')
    os.chmod(path, 0o755)
    return path


def _read_env(path, timeout=15):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    out = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            key, sep, value = line.rstrip('\n').partition('=')
            if sep:
                out[key] = value
    return out


class IsolatedSession(Home):
    """A tick whose environment carries a secret and the operator's HOME."""

    def setUp(self):
        super().setUp()
        self.operator_home = tempfile.mkdtemp(prefix='asf-operator-home-')
        self.addCleanup(shutil.rmtree, self.operator_home, ignore_errors=True)
        os.makedirs(os.path.join(self.operator_home, '.config', 'gh'))
        os.makedirs(os.path.join(self.operator_home, '.ssh'))
        for rel, text in (('.gitconfig', '[user]\n\tname = op\n'),
                          ('.config/gh/hosts.yml', 'github.com: {}\n'),
                          ('.ssh/id_test', 'PRIVATE\n')):
            with open(os.path.join(self.operator_home, rel), 'w', encoding='utf-8') as f:
                f.write(text)
        patcher = mock.patch.dict(os.environ, {'FAKE_SECRET': 'x', 'HOME': self.operator_home,
                                               'HTTPS_PROXY': 'http://proxy.example:3128',
                                               'LC_ALL': 'C'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bin = tempfile.mkdtemp(prefix='asf-agent-bin-')
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.dump = os.path.join(self.bin, 'env.txt')

    def product_with(self, command):
        return env.Product('sample', dict(self.product._data, conventions={'worktree_setup': command}))

    def spawn(self, account, cfg_extra=None, job='j1', product=None):
        cfg = dict(self.cfg)
        cfg['worker_pool'] = dict(cfg['worker_pool'], **(cfg_extra or {}))
        rt = runtime_mod.ClaudeCodeRuntime(binary=_dump_binary(self.bin, self.dump))
        rec = spawn_mod.spawn(product or self.product, s1_row(job=job), account, 'do it\n',
                              runtime=rt, cfg=cfg)
        return rec, _read_env(self.dump)

    def test_a_worker_sees_neither_the_ticks_secret_nor_the_operators_home(self):
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')
        _rec, seen = self.spawn(acct)
        self.assertNotIn('FAKE_SECRET', seen)
        self.assertNotIn('HTTPS_PROXY', seen)          # not named in env_passthrough
        self.assertNotEqual(seen['HOME'], self.operator_home)
        self.assertEqual(seen['HOME'], os.path.join(env.ASF_HOME, 'state', 'homes', 'acct-a'))
        self.assertTrue(os.path.isdir(seen['HOME']))
        self.assertEqual(seen['CLAUDE_CONFIG_DIR'], '/cfg/acct-a')   # the runtime's own: unchanged
        self.assertEqual(seen['LC_ALL'], 'C')
        self.assertIn('PATH', seen)
        # only the allow-list, ASF's own variables and git's pinned config reach the session
        extra = {k for k in seen if not k.startswith(('ASF_', 'GIT_CONFIG_', 'LC_'))} - {
            'PATH', 'LANG', 'TERM', 'TMPDIR', 'USER', 'SHELL', 'HOME', 'CLAUDE_CONFIG_DIR',
            'BACKLOG_ID_RANGE', 'PWD', 'SHLVL', 'OLDPWD', '_', '__CF_USER_TEXT_ENCODING',
            *env.DEFAULT_WORKER_ENV}   # the host-load caps every local worker gets
        self.assertEqual(extra, set(), seen)
        # nothing seeded, nothing copied: only ASF's own identity-only .gitconfig
        # beside it, at most the link to the factory's installed CLI (hotfix cd1adb0)
        self.assertEqual(sorted(set(os.listdir(seen['HOME'])) - {'.local'}), ['.gitconfig'])
        if os.path.lexists(os.path.join(seen['HOME'], '.local')):
            self.assertTrue(os.path.islink(os.path.join(seen['HOME'], '.local', 'bin', 'asf')))
            self.assertEqual(os.listdir(os.path.join(seen['HOME'], '.local', 'bin')), ['asf'])
        with open(os.path.join(seen['HOME'], '.gitconfig'), encoding='utf-8') as f:
            # F-0116: the session's .gitconfig names the agent, never the operator ("op") whose
            # own global config seeded this home's HOME above
            self.assertEqual(f.read(), runtime_mod.GITCONFIG_MARK + '\n[user]\n\tname = asf worker\n'
                             '\temail = asf-worker@localhost\n')

    def test_passthrough_names_reach_the_session(self):
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')
        _rec, seen = self.spawn(acct, {'env_passthrough': ['HTTPS_PROXY']})
        self.assertEqual(seen['HTTPS_PROXY'], 'http://proxy.example:3128')
        self.assertNotIn('FAKE_SECRET', seen)

    def test_the_home_holds_exactly_what_home_seed_lists(self):
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a', home_seed=[
            os.path.join(self.operator_home, '.gitconfig'),
            os.path.join(self.operator_home, '.config', 'gh')])
        _rec, seen = self.spawn(acct)
        home = seen['HOME']
        with open(os.path.join(home, '.gitconfig'), encoding='utf-8') as f:
            self.assertIn('name = op', f.read())
        self.assertTrue(os.path.isfile(os.path.join(home, '.config', 'gh', 'hosts.yml')))
        self.assertFalse(os.path.exists(os.path.join(home, '.ssh')))   # never listed, never there

    def test_an_explicit_home_is_used_and_isolate_home_false_keeps_the_operators(self):
        explicit = os.path.join(self.tmp, 'explicit-home')
        _rec, seen = self.spawn(pool_mod.Account('acct-a', home=explicit))
        self.assertEqual(seen['HOME'], explicit)
        os.remove(self.dump)
        _rec, seen = self.spawn(pool_mod.Account('acct-b', isolate_home=False), job='j2')
        self.assertEqual(seen['HOME'], self.operator_home)
        self.assertNotIn('FAKE_SECRET', seen)          # the allow-list still holds

    def test_the_gate_environment_stays_the_ticks_minus_the_deny_list(self):
        from asf.harvest import harvest
        gate = harvest.gate_env('/wt')
        self.assertEqual(gate['FAKE_SECRET'], 'x')      # a product's tests may need the machine
        self.assertEqual(gate['HOME'], self.operator_home)


class WorktreeSetup(IsolatedSession):
    def test_the_setup_command_runs_in_a_fresh_worktree_under_the_worker_env(self):
        product = self.product_with('printf "%s|%s" "$HOME" "${FAKE_SECRET:-none}" > .setup-marker')
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')
        rec, _seen = self.spawn(acct, product=product)
        with open(os.path.join(rec['worktree'], '.setup-marker'), encoding='utf-8') as f:
            home, secret = f.read().split('|')
        self.assertEqual(home, os.path.join(env.ASF_HOME, 'state', 'homes', 'acct-a'))
        self.assertEqual(secret, 'none')
        self.assertIsInstance(rec['setup_s'], float)

    def test_a_failing_setup_refuses_the_launch_and_removes_the_worktree(self):
        product = self.product_with('echo "lockfile is out of date" >&2; exit 3')
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')
        with self.assertRaises(spawn_mod.SpawnError) as cm:
            self.spawn(acct, product=product)
        self.assertIn('lockfile is out of date', str(cm.exception))
        self.assertIn('exit 3', str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(spawn_mod.worktrees_dir(product), 'j1')))
        self.assertEqual(pool_mod.load_sessions(product), {})   # nothing launched

    def test_no_command_no_setup(self):
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')
        rec, _seen = self.spawn(acct)
        self.assertNotIn('setup_s', rec)


class WorktreeSetupMarker(unittest.TestCase):
    """``setup_marker_path`` / ``setup_done`` / ``_record_setup`` on their own: a git worktree and
    nothing else — no account, no runtime stand-in, no spawn (F-0127, PD6)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-setup-marker-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', '-b', 'main', self.repo], check=True)
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            subprocess.run(['git', 'config', k, v], cwd=self.repo, check=True)
        with open(os.path.join(self.repo, 'README'), 'w', encoding='utf-8') as f:
            f.write('seed\n')
        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'seed'], cwd=self.repo, check=True)
        self.wt = os.path.join(self.tmp, 'wt')
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'feature', self.wt, 'main'],
                       cwd=self.repo, check=True)

    def test_setup_marker_path_resolves_through_the_trees_own_admin_dir(self):
        admin = spawn_mod._admin_dir(self.wt)
        self.assertEqual(spawn_mod.setup_marker_path(self.wt),
                         os.path.join(admin, spawn_mod.SETUP_MARKER))

    def test_a_suffixed_admin_dir_resolves_to_its_own_worktree_not_the_firsts(self):
        # a second worktree whose directory shares the first's basename gets its admin dir
        # suffixed by git (`wt`, `wt_1`, ...); the path must resolve to its own, never the first's
        other = os.path.join(self.tmp, 'elsewhere')
        os.makedirs(other)
        wt2 = os.path.join(other, 'wt')
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'feature2', wt2, 'main'],
                       cwd=self.repo, check=True)
        admin1, admin2 = spawn_mod._admin_dir(self.wt), spawn_mod._admin_dir(wt2)
        self.assertNotEqual(admin1, admin2)
        self.assertEqual(spawn_mod.setup_marker_path(self.wt), os.path.join(admin1, spawn_mod.SETUP_MARKER))
        self.assertEqual(spawn_mod.setup_marker_path(wt2), os.path.join(admin2, spawn_mod.SETUP_MARKER))

    def test_setup_done_is_false_with_no_marker(self):
        self.assertFalse(spawn_mod.setup_done(self.wt, 'pnpm install'))

    def test_record_setup_then_setup_done_is_true_for_that_command(self):
        command = 'pnpm install'
        path = spawn_mod._record_setup(self.wt, command, 1.5)
        self.assertEqual(path, spawn_mod.setup_marker_path(self.wt))
        self.assertTrue(spawn_mod.setup_done(self.wt, command))
        with open(path, encoding='utf-8') as f:
            obj = json.load(f)
        self.assertEqual(obj, {'command': command, 'at': obj.get('at'), 'took_s': 1.5})
        self.assertEqual(set(obj), {'command', 'at', 'took_s'})

    def test_setup_done_is_false_for_a_different_command(self):
        spawn_mod._record_setup(self.wt, 'pnpm install', 1.0)
        self.assertFalse(spawn_mod.setup_done(self.wt, 'make deps'))

    def test_setup_done_is_false_for_a_truncated_or_non_json_or_scalar_marker(self):
        path = spawn_mod.setup_marker_path(self.wt)
        for contents in ('{"command": "pnpm install"', 'not json',
                         '"pnpm install"', '["pnpm install"]'):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(contents)
            self.assertFalse(spawn_mod.setup_done(self.wt, 'pnpm install'), contents)

    def test_a_path_with_no_admin_dir_is_not_set_up_and_records_nothing(self):
        not_a_worktree = os.path.join(self.tmp, 'not-a-worktree')
        os.makedirs(not_a_worktree)
        self.assertEqual(spawn_mod.setup_marker_path(not_a_worktree), '')
        self.assertFalse(spawn_mod.setup_done(not_a_worktree, 'pnpm install'))
        self.assertEqual(spawn_mod._record_setup(not_a_worktree, 'pnpm install', 1.0), '')

    def test_record_setup_returns_empty_and_raises_nothing_when_the_admin_dir_is_unwritable(self):
        with mock.patch.object(spawn_mod, 'setup_marker_path',
                               return_value=os.path.join(self.tmp, 'no-such-dir', 'asf-setup')):
            self.assertEqual(spawn_mod._record_setup(self.wt, 'pnpm install', 1.0), '')

    def test_a_set_up_worktree_has_a_clean_git_status_and_the_marker_is_outside_the_tree(self):
        spawn_mod._record_setup(self.wt, 'pnpm install', 1.0)
        st = subprocess.run(['git', 'status', '--porcelain'], cwd=self.wt, capture_output=True,
                            text=True)
        self.assertEqual(st.stdout, '')
        self.assertFalse(spawn_mod.setup_marker_path(self.wt).startswith(self.wt + os.sep))


class WorktreeSetupRunsOnce(IsolatedSession):
    """Once per worktree, and again when the command changes (F-0127)."""

    def test_a_second_launch_skips_it_a_changed_command_reruns_it(self):
        counter = os.path.join(self.tmp, 'counter')
        product = self.product_with(f'printf x >> {counter}')
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')

        rec1, _seen = self.spawn(acct, product=product, job='j1')
        with open(counter, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'x')
        self.assertIsInstance(rec1['setup_s'], float)
        with open(spawn_mod.setup_marker_path(rec1['worktree']), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['command'], product.conventions.worktree_setup)
        # the run is ended the way a correction's relaunch finds it, so make_worktree reuses it
        pool_mod.update_session(product, 'j1', ended=pool_mod.now_iso())

        log_mtime = os.stat(spawn_mod.setup_log_path(product, 'j1')).st_mtime
        os.remove(self.dump)
        rec2, _seen = self.spawn(acct, product=product, job='j1')
        self.assertEqual(rec2['worktree'], rec1['worktree'])
        with open(counter, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'x')           # nothing ran a second time
        self.assertNotIn('setup_s', rec2)
        self.assertEqual(os.stat(spawn_mod.setup_log_path(product, 'j1')).st_mtime, log_mtime)
        pool_mod.update_session(product, 'j1', ended=pool_mod.now_iso())

        other = self.product_with(f'printf y >> {counter}')
        os.remove(self.dump)
        rec3, _seen = self.spawn(acct, product=other, job='j1')
        self.assertEqual(rec3['worktree'], rec1['worktree'])
        with open(counter, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'xy')
        self.assertIsInstance(rec3['setup_s'], float)
        with open(spawn_mod.setup_marker_path(rec3['worktree']), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['command'], other.conventions.worktree_setup)


class WorktreeSetupAfterReclaim(IsolatedSession):
    """The card's own test: a stale orphan reclaimed and recreated at the job's own path gets its
    setup, instead of handing a session a checkout with no dependencies (F-0127)."""

    def setUp(self):
        super().setUp()
        # the setup command's own output is what a real one's dependency install is: gitignored,
        # so an orphan's leftover-commit (asf.workers.spawn._commit_leftovers) finds nothing to
        # commit and the reclaimed branch stays exactly at origin's tip
        with open(os.path.join(self.repo, '.gitignore'), 'a', encoding='utf-8') as f:
            f.write('installed\nfrom-before-the-reclaim\n')
        subprocess.run(['git', 'add', '.gitignore'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.email=ci@example.com', '-c', 'user.name=ci', 'commit',
                        '-q', '-m', 'ignore the test setup command\'s own output'],
                       cwd=self.repo, check=True)
        subprocess.run(['git', 'push', '-q', 'origin', self.product.main], cwd=self.repo,
                       check=True)

    def test_a_stale_orphan_reclaimed_and_recreated_at_the_same_path_gets_its_setup(self):
        product = self.product_with('touch installed')
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')

        rec1, _seen = self.spawn(acct, product=product, job='j1')
        self.assertTrue(os.path.exists(os.path.join(rec1['worktree'], 'installed')))
        canary = os.path.join(rec1['worktree'], 'from-before-the-reclaim')
        with open(canary, 'w', encoding='utf-8') as f:
            f.write('x')

        # no run recorded for j1 any more (an ORPHAN, PD4), made stale enough to reclaim at once
        with open(pool_mod.sessions_path(product), 'w', encoding='utf-8'):
            pass
        os.remove(self.dump)
        with mock.patch.object(spawn_mod, 'ORPHAN_GRACE_S', 0):
            rec2, _seen = self.spawn(acct, product=product, job='j1')

        self.assertEqual(rec2['worktree'], rec1['worktree'])
        self.assertFalse(os.path.exists(canary))          # the old tree really was discarded
        self.assertTrue(os.path.exists(os.path.join(rec2['worktree'], 'installed')))  # setup reran
        with open(spawn_mod.setup_marker_path(rec2['worktree']), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['command'], product.conventions.worktree_setup)
        self.assertIsInstance(rec2['setup_s'], float)

    def test_a_branch_taken_over_from_origin_with_no_local_worktree_gets_its_setup_too(self):
        product = self.product_with('touch installed')
        acct = pool_mod.Account('acct-a', config_dir='/cfg/acct-a')

        rec1, _seen = self.spawn(acct, product=product, job='j1')
        branch = rec1['branch']
        subprocess.run(['git', 'push', '-q', 'origin', branch], cwd=rec1['worktree'], check=True)
        spawn_mod._discard(product, rec1['worktree'])   # a worktree reaped elsewhere, branch kept

        row2 = pool_mod.Row('j2', 'B-0001', state='BUG', action='FIX', severity='S1', branch=branch)
        os.remove(self.dump)
        rt = runtime_mod.ClaudeCodeRuntime(binary=_dump_binary(self.bin, self.dump))
        rec2 = spawn_mod.spawn(product, row2, acct, 'do it\n', runtime=rt, cfg=self.cfg)

        self.assertNotEqual(rec2['worktree'], rec1['worktree'])
        self.assertTrue(os.path.exists(os.path.join(rec2['worktree'], 'installed')))
        self.assertIsInstance(rec2['setup_s'], float)


class WorkerEnvDoctorRow(unittest.TestCase):
    def cfg(self, passthrough=(), **acct):
        return {'worker_pool': {'env_passthrough': list(passthrough),
                                'accounts': [dict({'name': 'acct-a', 'auth_env': {
                                    'CLAUDE_CODE_OAUTH_TOKEN': '/secrets/acct-a.token'}}, **acct)]}}

    def test_green_with_isolated_homes_and_a_harmless_passthrough(self):
        ok, detail = doctor.check_worker_env(self.cfg(
            ['HTTPS_PROXY', 'SSH_AUTH_SOCK', 'LANG_X'],
            auth_env={'CLAUDE_CODE_OAUTH_TOKEN': '/secrets/acct-a.token'}))
        self.assertTrue(ok, detail)
        self.assertIn('acct-a:', detail)

    def test_a_credential_looking_passthrough_is_red(self):
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'OPENAI_API_KEY',
                     'DB_PASSWORD', 'NPM_AUTH', 'SOME_APIKEY', 'SIGNING_KEY'):
            ok, detail = doctor.check_worker_env(self.cfg(['HTTPS_PROXY', name]))
            self.assertFalse(ok, name)
            self.assertIn(name, detail)
            self.assertNotIn('HTTPS_PROXY', detail.split('— ')[0].split(': ', 1)[1])

    def test_isolate_home_false_is_red(self):
        ok, detail = doctor.check_worker_env(self.cfg(isolate_home=False))
        self.assertFalse(ok)
        self.assertIn('acct-a has isolate_home: false', detail)

    def test_the_row_is_required_in_the_doctor_table(self):
        with mock.patch.object(doctor, 'check_worker_env', return_value=(False, 'x')):
            rows = [r for r in _doctor_rows() if r[0] == 'worker env']
        self.assertEqual(rows, [('worker env', True, False, 'x')])
        self.assertTrue(doctor.is_red(rows))

    def test_credential_names(self):
        for name in ('PATH', 'HTTPS_PROXY', 'SSH_AUTH_SOCK', 'KEYBOARD_LAYOUT', 'LANG'):
            self.assertFalse(hermetic.looks_like_credential(name), name)


def _doctor_rows():
    """``doctor.run`` over a minimal sound product, every probe that leaves the process stubbed."""
    tmp = tempfile.mkdtemp(prefix='asf-doctor-')
    try:
        product = env.Product('sample', {'repo_dir': tmp, 'backlog_dir': tmp,
                                         'ci': {'provider': 'none'}})
        with mock.patch.object(doctor, 'check_config', return_value=(True, 'ok', {}, product)), \
                mock.patch.object(doctor, 'check_repo', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_backlog', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_scheduler', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_one_factory', return_value=(True, '')), \
                mock.patch.object(doctor.approvals, 'check_doctor', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_redaction_hooks', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_approvals_hook', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_drift', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_worker_secrets', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_clock_code', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_clock_installs', return_value=[]), \
                mock.patch.object(doctor, 'check_product_loads_under_venv', return_value=[]), \
                mock.patch.object(doctor, 'check_cli_dispatcher', return_value=[]):
            return doctor.run('sample')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
