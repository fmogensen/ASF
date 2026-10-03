"""asf.clockinstall (F-0104) — which install a clock runs, read from that clock's own plist.

The arrangement this module answers for: a machine holding a pinned live install beside an
editable dev one — F-0111's finding that those are not the same install. No case here needs a
network or a real ``pipx``; every ``pipx`` answer is a fake ``run``, and every venv is a real
directory tree with a real ``direct_url.json`` (the reader reads disk, not a mock)."""
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import clockinstall, doctor, drift, env, scheduler, snapshot


def _git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _make_checkout(base):
    """A real one-commit git checkout at ``base``; returns its HEAD sha."""
    os.makedirs(base, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', base], check=True)
    _git(base, 'config', 'user.email', 't@example.com')
    _git(base, 'config', 'user.name', 't')
    with open(os.path.join(base, 'f'), 'w', encoding='utf-8') as f:
        f.write('x')
    _git(base, 'add', '-A')
    _git(base, 'commit', '-q', '-m', 'c')
    return _git(base, 'rev-parse', 'HEAD')


def _make_venv(venvs_dir, name, direct_url):
    """A pipx-shaped venv tree at ``<venvs_dir>/<name>``, its ``direct_url.json`` for this
    distribution under a real ``site-packages`` — read off disk, so the fixture is disk."""
    site = os.path.join(venvs_dir, name, 'lib', 'python3.11', 'site-packages')
    dist_info = os.path.join(site, 'asf_factory-0.1.0.dist-info')
    os.makedirs(dist_info, exist_ok=True)
    with open(os.path.join(dist_info, 'direct_url.json'), 'w', encoding='utf-8') as f:
        json.dump(direct_url, f)
    return os.path.join(venvs_dir, name, 'bin', 'python')


def _write_plist(path, argv, env_vars=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        plistlib.dump({'Label': os.path.basename(path)[:-len('.plist')],
                       'ProgramArguments': argv, 'EnvironmentVariables': env_vars or {}}, f)


class _FakeRun:
    """``subprocess.run`` for ``venvs_dir``: answers ``pipx environment`` by name, counts calls,
    and raises like a missing ``pipx`` when built with ``venvs_dir=None``."""

    def __init__(self, venvs_dir=None):
        self.venvs_dir = venvs_dir
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.venvs_dir is None:
            raise FileNotFoundError('pipx not found')
        return mock.Mock(returncode=0, stdout=self.venvs_dir + '\n')


class ClockInstallReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='clockinstall_read_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.agents = os.path.join(self.tmp, 'LaunchAgents')
        os.makedirs(self.agents)
        self.venvs = os.path.join(self.tmp, 'venvs')
        os.makedirs(self.venvs)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        self.addCleanup(setattr, env, 'ASF_HOME', self._orig_home)
        self.patch_agents = mock.patch.object(scheduler, 'launch_agents_dir',
                                              return_value=self.agents)
        self.patch_agents.start()
        self.addCleanup(self.patch_agents.stop)

    # ---- read() ---------------------------------------------------------

    def test_read_returns_the_argv_and_env_vars_of_a_real_plist(self):
        path = os.path.join(self.agents, 'x.plist')
        _write_plist(path, ['/usr/bin/python3', '-m', 'asf.cli'], {'PATH': '/bin'})
        argv, env_vars = clockinstall.read(path)
        self.assertEqual(argv, ['/usr/bin/python3', '-m', 'asf.cli'])
        self.assertEqual(env_vars, {'PATH': '/bin'})

    def test_read_answers_empty_for_a_missing_plist(self):
        self.assertEqual(clockinstall.read(os.path.join(self.agents, 'missing.plist')), ([], {}))

    def test_read_answers_empty_for_an_unreadable_plist(self):
        path = os.path.join(self.agents, 'bad.plist')
        with open(path, 'wb') as f:
            f.write(b'this is not a plist')
        self.assertEqual(clockinstall.read(path), ([], {}))

    def test_read_answers_empty_for_a_non_dict_plist(self):
        path = os.path.join(self.agents, 'list.plist')
        with open(path, 'wb') as f:
            plistlib.dump(['a', 'b'], f)
        self.assertEqual(clockinstall.read(path), ([], {}))

    # ---- classify() ------------------------------------------------------

    def test_a_pinned_venv_classifies_pinned(self):
        sha = 'a' * 40
        python = _make_venv(self.venvs, 'asf-factory', {
            'url': 'git+https://github.com/o/r.git',
            'vcs_info': {'vcs': 'git', 'commit_id': sha, 'requested_revision': 'main'}})
        path = os.path.join(self.agents, 'asf.sample.fast.plist')
        _write_plist(path, [python, '-m', 'asf.cli', 'tick'])
        argv, env_vars = clockinstall.read(path)
        inst = clockinstall.classify(argv, env_vars, self.venvs)
        self.assertEqual(inst.kind, 'pinned')
        self.assertEqual(inst.venv, 'asf-factory')
        self.assertEqual(inst.suffix, '')
        self.assertEqual(inst.sha, sha)

    def test_a_suffixed_venv_carries_its_suffix(self):
        sha = 'b' * 40
        python = _make_venv(self.venvs, 'asf-factory-live', {
            'url': 'git+https://github.com/o/r.git',
            'vcs_info': {'vcs': 'git', 'commit_id': sha}})
        argv, env_vars = [python, '-m', 'asf.cli', 'tick'], {}
        inst = clockinstall.classify(argv, env_vars, self.venvs)
        self.assertEqual(inst.kind, 'pinned')
        self.assertEqual(inst.venv, 'asf-factory-live')
        self.assertEqual(inst.suffix, '-live')
        self.assertEqual(inst.sha, sha)

    def test_an_editable_venv_names_the_sha_and_the_checkout(self):
        checkout = os.path.join(self.tmp, 'checkout')
        sha = _make_checkout(checkout)
        python = _make_venv(self.venvs, 'asf-factory', {
            'url': f'file://{checkout}', 'dir_info': {'editable': True}})
        inst = clockinstall.classify([python, '-m', 'asf.cli', 'tick'], {}, self.venvs)
        self.assertEqual(inst.kind, 'editable')
        self.assertEqual(inst.venv, 'asf-factory')
        self.assertEqual(inst.sha, sha)
        self.assertEqual(inst.repo, checkout)

    def test_a_snapshot_launcher_argv_names_the_repo_and_the_sha_it_last_ran(self):
        code_dir = os.path.join(self.tmp, 'code')
        os.makedirs(code_dir)
        sha = 'c' * 40
        with open(os.path.join(code_dir, snapshot.CURRENT), 'w', encoding='utf-8') as f:
            f.write(sha + '\n')
        argv = ['/usr/bin/python3', os.path.join(code_dir, snapshot.LAUNCHER), '--repo',
                '/the/checkout', '--code-dir', code_dir, '--', '/usr/bin/python3', '-m',
                'asf.cli', 'tick']
        inst = clockinstall.classify(argv, {}, self.venvs)
        self.assertEqual(inst.kind, 'snapshot')
        self.assertEqual(inst.repo, '/the/checkout')
        self.assertEqual(inst.sha, sha)

    def test_a_snapshot_launcher_with_no_tick_yet_answers_no_sha_not_a_raise(self):
        code_dir = os.path.join(self.tmp, 'code-fresh')
        os.makedirs(code_dir)
        argv = ['/usr/bin/python3', os.path.join(code_dir, snapshot.LAUNCHER), '--repo',
                '/the/checkout', '--code-dir', code_dir, '--', '/usr/bin/python3']
        inst = clockinstall.classify(argv, {}, self.venvs)
        self.assertEqual(inst.kind, 'snapshot')
        self.assertEqual(inst.sha, '')

    def test_a_hand_written_checkout_plist_classifies_checkout(self):
        # render() never writes a plist naming a checkout interpreter while also carrying
        # PYTHONPATH (it deletes PYTHONPATH the moment it rewrites to the snapshot launcher) —
        # this reproduces a plist an older asf wrote, or one written by hand (PD7).
        checkout = os.path.join(self.tmp, 'bare-checkout')
        sha = _make_checkout(checkout)
        argv = [os.path.join(checkout, 'bin', 'python3'), '-m', 'asf.cli', 'tick']
        env_vars = {'PYTHONPATH': checkout}
        inst = clockinstall.classify(argv, env_vars, '')
        self.assertEqual(inst.kind, 'checkout')
        self.assertEqual(inst.repo, checkout)
        self.assertEqual(inst.sha, sha)

    def test_unknown_when_pipx_is_not_on_path(self):
        inst = clockinstall.classify(['/usr/bin/python3', '-m', 'asf.cli', 'tick'], {}, '')
        self.assertEqual(inst.kind, 'unknown')
        self.assertIn('pipx is not on PATH', inst.why)

    def test_unknown_names_the_interpreter_when_it_is_placeable(self):
        inst = clockinstall.classify(['/usr/bin/python3', '-m', 'asf.cli', 'tick'], {},
                                     self.venvs)
        self.assertEqual(inst.kind, 'unknown')
        self.assertIn('/usr/bin/python3', inst.why)

    def test_venv_commit_answers_empty_for_a_missing_dist_info(self):
        empty_venv = os.path.join(self.tmp, 'no-dist')
        os.makedirs(empty_venv)
        self.assertEqual(clockinstall.venv_commit(empty_venv), ('', False, ''))

    def test_venvs_dir_answers_empty_when_pipx_is_not_there(self):
        self.assertEqual(clockinstall.venvs_dir(_FakeRun(venvs_dir=None)), '')

    def test_venvs_dir_reads_pipx_environment(self):
        self.assertEqual(clockinstall.venvs_dir(_FakeRun(venvs_dir=self.venvs)), self.venvs)

    # ---- for_product() ----------------------------------------------------

    def test_for_product_sorts_by_label_and_reads_venvs_dir_once(self):
        sha_a, sha_b = 'd' * 40, 'e' * 40
        python_a = _make_venv(self.venvs, 'asf-factory', {
            'url': 'git+https://github.com/o/r.git', 'vcs_info': {'commit_id': sha_a}})
        _write_plist(os.path.join(self.agents, 'asf.sample.zzz.plist'),
                    [python_a, '-m', 'asf.cli', 'tick'])
        _write_plist(os.path.join(self.agents, 'asf.sample.aaa.plist'),
                    [python_a, '-m', 'asf.cli', 'tick'])
        run = _FakeRun(venvs_dir=self.venvs)
        insts = clockinstall.for_product('sample', cfg={}, run=run)
        self.assertEqual([i.label for i in insts], ['asf.sample.aaa', 'asf.sample.zzz'])
        self.assertEqual(len(run.calls), 1)

    def test_for_product_answers_empty_with_no_plist(self):
        run = _FakeRun(venvs_dir=self.venvs)
        self.assertEqual(clockinstall.for_product('nobody', cfg={}, run=run), [])
        self.assertEqual(run.calls, [])

    def test_for_product_answers_empty_for_a_non_launchd_scheduler_kind(self):
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    ['/usr/bin/python3', '-m', 'asf.cli', 'tick'])
        run = _FakeRun(venvs_dir=self.venvs)
        cfg = {'scheduler': {'kind': 'cron'}}
        self.assertEqual(clockinstall.for_product('sample', cfg=cfg, run=run), [])
        self.assertEqual(run.calls, [])

    def test_for_product_answers_empty_when_the_config_will_not_load(self):
        run = _FakeRun(venvs_dir=self.venvs)
        with mock.patch.object(env, 'load_config', side_effect=env.ConfigError('bad')):
            self.assertEqual(clockinstall.for_product('sample', cfg=None, run=run), [])


class TrunkDistanceTests(unittest.TestCase):
    """The trunk, the distance, and whether an install is merged (spec §B)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='clockinstall_trunk_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', '-b', 'main', self.repo], check=True)
        _git(self.repo, 'config', 'user.email', 't@example.com')
        _git(self.repo, 'config', 'user.name', 't')
        self.c0 = self._commit('a')
        self.c1 = self._commit('b')
        self.c2 = self._commit('c')
        self.c3 = self._commit('d')
        self.head = self.c3
        _git(self.repo, 'branch', 'side', self.c1)
        _git(self.repo, 'checkout', '-q', 'side')
        self.side = self._commit('e')
        _git(self.repo, 'checkout', '-q', 'main')
        clockinstall._TRUNK_CACHE.clear()
        self._orig_home = env.ASF_HOME

    def tearDown(self):
        env.ASF_HOME = self._orig_home

    def _commit(self, name):
        with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
            f.write(name)
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', name)
        return _git(self.repo, 'rev-parse', 'HEAD')

    # ---- against_trunk() ---------------------------------------------------

    def test_a_sha_that_is_the_head_reads_behind_0_merged(self):
        self.assertEqual(clockinstall.against_trunk(self.head, self.repo, self.head), (0, True))

    def test_a_sha_three_commits_behind_reads_the_count_merged(self):
        self.assertEqual(clockinstall.against_trunk(self.c0, self.repo, self.head), (3, True))

    def test_a_side_branch_sha_counts_behind_but_is_not_merged(self):
        behind, merged = clockinstall.against_trunk(self.side, self.repo, self.head)
        self.assertIsInstance(behind, int)
        self.assertFalse(merged)

    def test_a_sha_the_repo_has_never_seen_answers_none_none(self):
        self.assertEqual(clockinstall.against_trunk('f' * 40, self.repo, self.head), (None, None))

    def test_empty_sha_repo_or_head_answers_none_none(self):
        self.assertEqual(clockinstall.against_trunk('', self.repo, self.head), (None, None))
        self.assertEqual(clockinstall.against_trunk(self.head, '', self.head), (None, None))
        self.assertEqual(clockinstall.against_trunk(self.head, self.repo, ''), (None, None))

    def test_two_calls_for_one_sha_head_shell_git_once(self):
        real_run = subprocess.run
        with mock.patch('asf.clockinstall.subprocess.run', side_effect=real_run) as run_mock:
            clockinstall.against_trunk(self.c0, self.repo, self.head)
            clockinstall.against_trunk(self.c0, self.repo, self.head)
        self.assertEqual(run_mock.call_count, 2)  # rev-list, merge-base — once, not twice

    def test_no_fetch_and_no_ls_remote_over_any_call(self):
        real_run = subprocess.run
        with mock.patch('asf.clockinstall.subprocess.run', side_effect=real_run) as run_mock:
            clockinstall.against_trunk(self.c0, self.repo, self.head)
            clockinstall.trunk()
        for call in run_mock.call_args_list:
            argv = call.args[0]
            self.assertNotIn('fetch', argv)
            self.assertNotIn('ls-remote', argv)

    # ---- trunk() ------------------------------------------------------------

    def test_trunk_reads_origin_main_over_main_when_both_resolve(self):
        _git(self.repo, 'update-ref', 'refs/remotes/origin/main', self.c1)
        with mock.patch.object(drift, 'factory_root', return_value=self.repo):
            self.assertEqual(clockinstall.trunk(), (self.repo, self.c1))

    def test_trunk_falls_back_to_the_local_main_with_no_origin(self):
        with mock.patch.object(drift, 'factory_root', return_value=self.repo):
            self.assertEqual(clockinstall.trunk(), (self.repo, self.head))

    def test_trunk_answers_empty_with_no_factory_root_and_no_configured_product(self):
        env.ASF_HOME = os.path.join(self.tmp, 'home-empty')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        with mock.patch.object(drift, 'factory_root', return_value=None):
            self.assertEqual(clockinstall.trunk(), ('', ''))

    def test_trunk_falls_back_to_a_configured_product_whose_repo_is_the_factory_source(self):
        env.ASF_HOME = os.path.join(self.tmp, 'home-product')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        with open(os.path.join(self.repo, 'pyproject.toml'), 'w', encoding='utf-8') as f:
            f.write('[project]\nname = "asf-factory"\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'pyproject')
        self.head = _git(self.repo, 'rev-parse', 'HEAD')
        with open(env.product_path('sample'), 'w', encoding='utf-8') as f:
            f.write(f'repo_dir: {self.repo}\nmain: main\n')
        with mock.patch.object(drift, 'factory_root', return_value=None):
            self.assertEqual(clockinstall.trunk(), (self.repo, self.head))


class DoctorClockInstallRowTests(unittest.TestCase):
    """``asf doctor``'s ``clock install`` row (F-0104 spec §3): one row per clock, R1-R4 each
    proved, ``is_red`` true for the reds and false for the two ``skip`` rows, and the existing
    ``clock code`` row untouched beside it (C4)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='clockinstall_doctor_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.agents = os.path.join(self.tmp, 'LaunchAgents')
        os.makedirs(self.agents)
        self.venvs = os.path.join(self.tmp, 'venvs')
        os.makedirs(self.venvs)
        self.patch_agents = mock.patch.object(scheduler, 'launch_agents_dir',
                                              return_value=self.agents)
        self.patch_agents.start()
        self.addCleanup(self.patch_agents.stop)
        self.patch_venvs = mock.patch.object(clockinstall, 'venvs_dir',
                                             return_value=self.venvs)
        self.patch_venvs.start()
        self.addCleanup(self.patch_venvs.stop)
        self.patch_home = mock.patch.object(env, 'ASF_HOME', os.path.join(self.tmp, 'asf-home'))
        self.patch_home.start()                 # the pin record is read under a temp home
        self.addCleanup(self.patch_home.stop)

        self.repo = os.path.join(self.tmp, 'trunk')
        os.makedirs(self.repo)
        subprocess.run(['git', 'init', '-q', '-b', 'main', self.repo], check=True)
        _git(self.repo, 'config', 'user.email', 't@example.com')
        _git(self.repo, 'config', 'user.name', 't')
        self.c0 = self._commit('a')
        self._commit('b')
        self._commit('c')
        self.head = self._commit('d')
        _git(self.repo, 'branch', 'side', self.c0)
        _git(self.repo, 'checkout', '-q', 'side')
        self.side = self._commit('e')
        _git(self.repo, 'checkout', '-q', 'main')
        clockinstall._TRUNK_CACHE.clear()
        self.patch_trunk = mock.patch.object(clockinstall, 'trunk',
                                             return_value=(self.repo, self.head))
        self.patch_trunk.start()
        self.addCleanup(self.patch_trunk.stop)

        self.product = env.Product('sample', {'repo_dir': self.tmp, 'backlog_dir': self.tmp,
                                              'ci': {'provider': 'none'}})

    def _commit(self, name):
        with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
            f.write(name)
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', name)
        return _git(self.repo, 'rev-parse', 'HEAD')

    def _rows(self, cfg=None):
        return doctor.check_clock_installs(cfg if cfg is not None else {}, self.product)

    # ---- R4: ok -------------------------------------------------------------

    def test_a_pinned_current_clock_reads_ok_and_current(self):
        python = _make_venv(self.venvs, 'asf-factory-live', {
            'url': 'git+https://github.com/o/r.git', 'vcs_info': {'commit_id': self.head}})
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertTrue(ok)
        self.assertIn('pinned asf-factory-live', detail)
        self.assertIn(self.head[:7], detail)
        self.assertIn(f'origin/main {self.head[:7]}', detail)
        self.assertIn('current', detail)

    def test_a_pinned_clock_behind_the_trunk_names_the_count(self):
        python = _make_venv(self.venvs, 'asf-factory-live', {
            'url': 'git+https://github.com/o/r.git', 'vcs_info': {'commit_id': self.c0}})
        _write_plist(os.path.join(self.agents, 'asf.sample.daily.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertTrue(ok)
        self.assertIn('behind by 3', detail)

    def test_no_asf_checkout_on_this_host_answers_ok_and_says_it_could_not_tell(self):
        python = _make_venv(self.venvs, 'asf-factory-live', {
            'url': 'git+https://github.com/o/r.git', 'vcs_info': {'commit_id': self.head}})
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        with mock.patch.object(clockinstall, 'trunk', return_value=('', '')):
            required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertTrue(ok)
        self.assertIn('no ASF checkout on this host', detail)
        self.assertIn('distance not measured', detail)

    # ---- R1: editable, checkout, unknown -------------------------------------

    def test_an_editable_clock_is_red(self):
        checkout = os.path.join(self.tmp, 'editable-checkout')
        sha = _make_checkout(checkout)
        python = _make_venv(self.venvs, 'asf-factory', {
            'url': f'file://{checkout}', 'dir_info': {'editable': True}})
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('editable asf-factory', detail)
        self.assertIn(sha[:7], detail)
        self.assertIn('the clock ticks a working tree', detail)
        self.assertIn('reinstall it pinned: install.sh sample', detail)
        self.assertNotIn('tools/install.sh', detail)  # PD3 — a forbidden token in asf/**/*.py

    def test_an_unknown_clock_is_red(self):
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    ['/usr/bin/python3', '-m', 'asf.cli', 'tick'])
        required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('unknown', detail)
        self.assertIn('/usr/bin/python3', detail)

    # ---- R2: a snapshot on a product that is not the factory's own source ---

    def test_a_snapshot_clock_on_a_non_factory_product_is_red(self):
        code_dir = os.path.join(self.tmp, 'code')
        os.makedirs(code_dir)
        with open(os.path.join(code_dir, snapshot.CURRENT), 'w', encoding='utf-8') as f:
            f.write(self.head + '\n')
        checkout = os.path.join(self.tmp, 'a-customer-repo')
        argv = ['/usr/bin/python3', os.path.join(code_dir, snapshot.LAUNCHER), '--repo',
                checkout, '--code-dir', code_dir, '--', '/usr/bin/python3', '-m', 'asf.cli',
                'tick']
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'), argv)
        self.product = env.Product('sample', {'repo_dir': checkout, 'backlog_dir': self.tmp,
                                              'ci': {'provider': 'none'}})
        required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('snapshot of', detail)
        self.assertIn(f'origin/main {self.head[:7]}', detail)
        self.assertIn("this product is not the factory's own source", detail)
        self.assertIn('reinstall it pinned: install.sh sample', detail)

    # ---- R3: a sha that is not an ancestor of origin/main --------------------

    def test_a_snapshot_clock_off_the_trunk_is_red_even_for_the_factory_product(self):
        code_dir = os.path.join(self.tmp, 'code2')
        os.makedirs(code_dir)
        with open(os.path.join(code_dir, snapshot.CURRENT), 'w', encoding='utf-8') as f:
            f.write(self.side + '\n')
        argv = ['/usr/bin/python3', os.path.join(code_dir, snapshot.LAUNCHER), '--repo',
                self.repo, '--code-dir', code_dir, '--', '/usr/bin/python3', '-m', 'asf.cli',
                'tick']
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'), argv)
        self.product = env.Product('sample', {'repo_dir': doctor.package_root(),
                                              'backlog_dir': self.tmp,
                                              'ci': {'provider': 'none'}})
        required, ok, detail = self._rows()[0]
        self.assertTrue(required)
        self.assertFalse(ok)
        self.assertIn('NOT on origin/main', detail)
        self.assertIn('unmerged work is ticking this product', detail)
        self.assertNotIn('install.sh', detail)  # already pinned — the repair is not a reinstall

    # ---- the two rows that are not judgements --------------------------------

    def test_no_clock_plist_is_a_skip_row_not_a_red(self):
        required, ok, detail = self._rows()[0]
        self.assertFalse(required)
        self.assertIsNone(ok)
        self.assertIn('no clock plist', detail)
        self.assertFalse(doctor.is_red([('clock install', required, ok, detail)]))

    def test_a_non_launchd_scheduler_kind_is_a_skip_row(self):
        required, ok, detail = self._rows(cfg={'scheduler': {'kind': 'cron'}})[0]
        self.assertFalse(required)
        self.assertIsNone(ok)
        self.assertIn('kind:cron', detail)
        self.assertFalse(doctor.is_red([('clock install', required, ok, detail)]))

    # ---- is_red, as run() shapes the rows -------------------------------------

    def test_is_red_true_for_each_of_the_four_reds(self):
        cases = []
        checkout = os.path.join(self.tmp, 'editable-checkout')
        _make_checkout(checkout)
        python = _make_venv(self.venvs, 'asf-factory', {
            'url': f'file://{checkout}', 'dir_info': {'editable': True}})
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        cases.append(self._rows()[0])
        for required, ok, detail in cases:
            rows = [('clock install', required, ok, detail)]
            self.assertTrue(doctor.is_red(rows), detail)

    # ---- the memo -------------------------------------------------------------

    def test_two_clocks_on_one_install_shell_git_once_for_the_pair(self):
        python = _make_venv(self.venvs, 'asf-factory-live', {
            'url': 'git+https://github.com/o/r.git', 'vcs_info': {'commit_id': self.c0}})
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        _write_plist(os.path.join(self.agents, 'asf.sample.daily.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        real_run = subprocess.run
        with mock.patch('asf.clockinstall.subprocess.run', side_effect=real_run) as run_mock:
            rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(run_mock.call_count, 2)  # rev-list, merge-base — once, not twice

    # ---- the existing row beside the new ones ----------------------------------

    def test_clock_code_row_stays_present_and_green_beside_the_new_rows(self):
        python = _make_venv(self.venvs, 'asf-factory-live', {
            'url': 'git+https://github.com/o/r.git', 'vcs_info': {'commit_id': self.head}})
        _write_plist(os.path.join(self.agents, 'asf.sample.fast.plist'),
                    [python, '-m', 'asf.cli', 'tick'])
        with mock.patch.object(doctor, 'check_config',
                              return_value=(True, 'ok', {}, self.product)), \
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
                mock.patch.object(doctor, 'check_clock_code', return_value=(True, 'stubbed')), \
                mock.patch.object(doctor, 'check_product_loads_under_venv', return_value=[]), \
                mock.patch.object(doctor, 'check_cli_dispatcher', return_value=[]):
            rows = doctor.run('sample')
        clock_code = [r for r in rows if r[0] == 'clock code']
        clock_install = [r for r in rows if r[0] == 'clock install']
        self.assertEqual(len(clock_code), 1)
        self.assertFalse(clock_code[0][1])   # required=False, unmoved (C4)
        self.assertTrue(clock_code[0][2])    # still green
        self.assertEqual(len(clock_install), 1)
        self.assertTrue(clock_install[0][1])  # required=True
        self.assertTrue(clock_install[0][2])  # ok — pinned and current
