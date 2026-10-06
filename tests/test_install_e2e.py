"""tests/test_install_e2e.py — T-0409/S-32360: zero to green, end to end (D12 of
``docs/specs/f-0107.md``, the card's own CI-test bullet).

A temp ``HOME`` and ``ASF_HOME``, ``sample/`` published to two bare origins the way
``tests/test_sample_product.py`` already publishes it, one real ``asf install`` — no ``pipx``, no
network, no tag (PD17) — a green ``asf doctor``, one dry run that launches nothing, then the same
command run again writing nothing new. Every other step of ``asf install`` (the config template,
the account plan, the twelve steps' own idempotence) is proved under mocks by
``tests.test_install``; this is the one place they all run together, for real, from nothing.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import env, hermetic, init

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_install_e2e` does not
    from test_scheduler import fake_clis
    from test_sample_product import _git, _publish
    from gitfixture import executable_asf
except ImportError:  # pragma: no cover - import shape only
    from tests.test_scheduler import fake_clis
    from tests.test_sample_product import _git, _publish
    from tests.gitfixture import executable_asf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, 'sample')

#: A product repo's origin discovery only turns into ``repo_slug`` (``asf.init.slug_from_url``)
#: when it reads as a hosted URL — a bare local path (what ``_publish`` sets it to) does not. Every
#: doctor row this test proves green needs one (``asf.doctor.has_pr_host``: undiscovered CI stays
#: "has a PR host" until a product says ``ci: {provider: none}`` by hand, which nothing here does),
#: so the repo's remote is rewritten to a scp-style URL that reads as one — unreachable, and never
#: reached: nothing in this test's path fetches or pushes it. The record's own origin stays the
#: real bare path the dry run's ``git clone`` needs.
FAKE_ORIGIN = 'git@example.invalid:asf-sample/repo.git'


def _snapshot(root):
    """``{relpath: sha256}`` for every file under ``root``, ``.git`` internals excluded — a git
    command that changes nothing still rewrites a reflog timestamp, which would read as drift a
    second identical run did not make."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, 'rb') as f:
                out[os.path.relpath(full, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


class InstallFromZeroTests(unittest.TestCase):
    """One ``setUpClass``: the fixture, the first ``asf install``, a bare ``asf doctor``, then a
    second ``asf install`` snapshotted around it — every ``test_`` reads what it left."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_install_e2e_'))
        sample = os.path.join(cls.tmp, 'sample')
        shutil.copytree(SAMPLE, sample)
        cls.repo = os.path.join(sample, 'repo')
        cls.record = os.path.join(sample, 'backlog')
        cls.repo_origin = os.path.join(cls.tmp, 'repo.git')
        cls.record_origin = os.path.join(cls.tmp, 'backlog.git')
        _publish(cls.repo, cls.repo_origin)
        _publish(cls.record, cls.record_origin)
        _git(['remote', 'set-url', 'origin', FAKE_ORIGIN], cwd=cls.repo)
        cls.record_head = _git(['rev-parse', 'main'], cwd=cls.record_origin)

        cls.home = os.path.join(cls.tmp, 'home')
        os.makedirs(cls.home)
        cls.asf_home = os.path.join(cls.home, '.ASF')
        stub_dir = os.path.join(cls.tmp, 'bin')
        fake_clis(stub_dir, names=('claude',), rc=1)  # a real `claude` on PATH would answer the
        # plugin probe and rewrite ~/.claude/settings.json in its own key order: not hermetic
        fake_clis(stub_dir)  # every doctor CLI answers at once, gh included: no ci.provider to
        # discover is undiscovered, not `none`, so `asf doctor`'s cli:gh row stays required
        base = dict(os.environ, ASF_HOME=cls.asf_home, PYTHONPATH=ROOT,
                    GIT_AUTHOR_NAME='sample', GIT_AUTHOR_EMAIL='sample@example.com',
                    GIT_COMMITTER_NAME='sample', GIT_COMMITTER_EMAIL='sample@example.com',
                    GH_TOKEN='', ASF_HOST_READING='0 1 0',
                    PATH=stub_dir + os.pathsep + os.environ.get('PATH', ''))
        cls.env = hermetic.build(base, home=cls.home)

        cls.install_argv = ('install', '--product', 'sample', '--repo', cls.repo,
                            '--record', cls.record, '--scheduler', 'none', '--fake-workers',
                            '--allow-checkout', '--yes', '--console-permissions', 'user')
        cls.first = cls.asf(*cls.install_argv)
        cls.doctor = cls.asf('doctor', '--product', 'sample')
        cls.before_second = cls._snapshot_all()
        cls.second = cls.asf(*cls.install_argv)
        cls.after_second = cls._snapshot_all()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def asf(cls, *argv):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *argv], cwd=cls.tmp, env=cls.env,
                              capture_output=True, text=True, timeout=300)

    @classmethod
    def _snapshot_all(cls):
        return {'home': _snapshot(cls.home), 'repo': _snapshot(cls.repo),
                'record': _snapshot(cls.record)}

    # ---- the first run: zero to green -----------------------------------------------

    def test_the_runtime_writes_under_the_temp_home_never_the_callers_config_dir(self):
        # a worker session's CLAUDE_CONFIG_DIR, inherited here, sent the runtime CLI's
        # `plugin marketplace add` into that account's live settings file
        self.assertEqual(self.env['HOME'], self.home)
        self.assertNotIn(hermetic.RUNTIME_CONFIG_DIR, self.env)

    def test_first_run_exits_0(self):
        self.assertEqual(self.first.returncode, 0, self.first.stdout + self.first.stderr)

    def test_first_run_writes_the_operator_config(self):
        path = os.path.join(self.asf_home, 'config.yaml')
        self.assertTrue(os.path.isfile(path), path)
        with mock.patch.object(env, 'ASF_HOME', self.asf_home):
            cfg = env.load_config()
        self.assertEqual(cfg['worker_pool']['backend'], 'fake')
        self.assertEqual(cfg['scheduler']['kind'], 'none')

    def test_first_run_writes_the_product_file(self):
        path = os.path.join(self.asf_home, 'products', 'sample.yaml')
        self.assertTrue(os.path.isfile(path), path)
        with mock.patch.object(env, 'ASF_HOME', self.asf_home):
            product = env.load_product('sample')
        self.assertEqual(product.repo_slug, 'asf-sample/repo')  # discovered from FAKE_ORIGIN
        self.assertEqual(product.repo_dir, self.repo)
        self.assertEqual(product.backlog_dir, self.record)

    def test_first_run_adopts_the_record(self):
        self.assertIn('init: adopted 10 items', self.first.stdout)
        for folder in init.ITEM_FOLDERS:
            self.assertTrue(os.path.isdir(os.path.join(self.record, folder)), folder)
        with open(os.path.join(self.record, 'index.json'), encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)['items']), 10)

    def test_first_run_writes_the_plugin_tree_under_asf_home(self):
        plugin = os.path.join(self.asf_home, 'plugin')
        self.assertTrue(os.path.isfile(os.path.join(plugin, '.claude-plugin', 'marketplace.json')))
        self.assertTrue(os.path.isfile(os.path.join(plugin, 'plugin', '.claude-plugin', 'plugin.json')))

    def test_first_run_doctor_step_is_green(self):
        self.assertIn('step 11: the doctor: ok', self.first.stdout, self.first.stdout)

    def test_doctor_on_its_own_exits_0(self):
        # a status, not a grep — asf/doctor.py:1079-1091
        self.assertEqual(self.doctor.returncode, 0, self.doctor.stdout + self.doctor.stderr)

    def test_first_run_dry_run_launches_and_pushes_nothing(self):
        self.assertIn('step 12: the dry run: ok', self.first.stdout)
        self.assertIn('== wave', self.first.stdout)
        self.assertIsNone(re.search(r'(?m)^launched ', self.first.stdout),
                          'the dry run launched a session')
        self.assertEqual(_git(['rev-parse', 'main'], cwd=self.record_origin), self.record_head)

    def test_first_run_summary_is_every_step_passed(self):
        self.assertIn('every step passed (12/12)', self.first.stdout)
        self.assertIn('asf install: done', self.first.stdout)

    # ---- the second run: writes nothing new -----------------------------------------

    def test_second_run_exits_0(self):
        self.assertEqual(self.second.returncode, 0, self.second.stdout + self.second.stderr)

    def test_second_run_writes_nothing_new(self):
        self.assertEqual(self.before_second, self.after_second)

    def test_second_run_every_step_ok_or_already_in_place(self):
        self.assertNotIn('FAILED', self.second.stdout, self.second.stdout)
        self.assertIn('step 3: the operator config: already in place', self.second.stdout)
        self.assertIn('step 8: the clocks: already in place', self.second.stdout)
        self.assertIn('every step passed (12/12)', self.second.stdout)

    def test_second_run_record_origin_untouched(self):
        self.assertEqual(_git(['rev-parse', 'main'], cwd=self.record_origin), self.record_head)


class RecordTickAgainstALocalNonBareOriginTests(unittest.TestCase):
    """install-clean CI run 37429353185, both ``install-clean-*`` jobs: unlike
    ``InstallFromZeroTests`` above, whose ``backlog_dir`` is a working copy of a *bare* origin
    (``_publish``, the way a real hosted record is), ``tools/install-clean.sh``'s own record
    directory IS its ``backlog_dir`` — a plain, non-bare checkout with no separate origin at
    all. Two things are then required for the record clock's very first tick to push its own
    state successfully: ``receive.denyCurrentBranch=updateInstead`` on it (any local, non-bare
    origin needs this to take a push to its checked-out branch at all —
    ``tests/test_shadow.py``'s own ``test_record_push_is_not_refused`` sets it by hand for the
    same reason), and the record `asf init`'s adopt just laid down committed before that tick
    runs — the docs tell a real operator "commit and push that layout yourself", and a tick's
    own commit touches the same ``index.json`` adopt left untracked, so an uncommitted adopt
    conflicts with the tick's own push of it. Skip either and the push is refused — nothing to
    do with ``gh``, which already degrades to "not configured" (also proved here, directly:
    ``gh`` is stubbed to fail the way an unconfigured one does) — and ``asf doctor``'s SCHEDULER
    row would read that clock RED. Proved here without launchd, a tty or a scheduled clock: one
    real ``asf init``, then one real ``asf tick --steps record`` run twice — once against the
    adopt as it lands (uncommitted), reproducing the CI failure, and once more after the commit
    ``tools/install-clean.sh`` now makes, showing the fix is exactly that commit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='record_tick_local_origin_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'repo')
        self.record = os.path.join(self.tmp, 'record')
        os.makedirs(self.repo)
        _git(['init', '-q', '-b', 'main'], cwd=self.repo)
        with open(os.path.join(self.repo, 'README'), 'w', encoding='utf-8') as f:
            f.write('a sample product\n')
        _git(['add', 'README'], cwd=self.repo)
        _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'README'],
            cwd=self.repo)
        _git(['remote', 'add', 'origin', 'https://github.com/example-org/example-product.git'],
            cwd=self.repo)
        os.makedirs(self.record)
        _git(['init', '-q', '-b', 'main'], cwd=self.record)
        _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
             '-m', 'the record'], cwd=self.record)
        _git(['config', 'receive.denyCurrentBranch', 'updateInstead'], cwd=self.record)

        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.asf_home = os.path.join(self.home, '.ASF')
        stub_dir = os.path.join(self.tmp, 'bin')
        # an unconfigured `gh`, exactly the CI run's own symptom — degrades, never a tick failure
        fake_clis(stub_dir, names=('gh',), rc=1)
        # the record's own `.githooks/pre-commit` (written by `asf init`) shells out to `asf
        # check`/`asf redact`; this test commits by hand (as `tools/install-clean.sh`'s own
        # operator-run `asf` does for real), and only needs that hook to exit 0, never a real
        # check
        executable_asf(stub_dir)
        base = dict(os.environ, ASF_HOME=self.asf_home, PYTHONPATH=ROOT,
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@t', GH_TOKEN='', ASF_PRODUCT='sample',
                   PATH=stub_dir + os.pathsep + os.environ.get('PATH', ''))
        self.env = hermetic.build(base, home=self.home)

    def asf(self, *argv):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *argv], cwd=self.tmp,
                              env=self.env, capture_output=True, text=True, timeout=120)

    def test_an_uncommitted_adopt_gets_its_first_tick_push_refused(self):
        init_r = self.asf('init', '--product', 'sample', '--repo', self.repo,
                          '--backlog', self.record)
        self.assertEqual(init_r.returncode, 0, init_r.stdout + init_r.stderr)
        self.assertIn('laid down a new record', init_r.stdout)
        self.assertTrue(_git(['status', '--porcelain'], cwd=self.record),
                        'adopt should leave the record uncommitted, as the docs say it does')

        tick_r = self.asf('tick', '--product', 'sample', '--steps', 'record')
        self.assertNotEqual(tick_r.returncode, 0, tick_r.stdout + tick_r.stderr)
        self.assertIn('push refused', tick_r.stdout)

    def test_committing_the_adopt_first_makes_the_same_tick_push_clean(self):
        init_r = self.asf('init', '--product', 'sample', '--repo', self.repo,
                          '--backlog', self.record)
        self.assertEqual(init_r.returncode, 0, init_r.stdout + init_r.stderr)
        # the record's own `.githooks/pre-commit` runs here — with `self.env`'s PATH, where the
        # stub `asf` it shells out to lives
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A'],
                       cwd=self.record, env=self.env, check=True, capture_output=True, text=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q',
                        '-m', 'adopt'], cwd=self.record, env=self.env, check=True,
                       capture_output=True, text=True)

        tick_r = self.asf('tick', '--product', 'sample', '--steps', 'record')
        self.assertEqual(tick_r.returncode, 0, tick_r.stdout + tick_r.stderr)
        self.assertIn('state committed and pushed', tick_r.stdout)
        self.assertEqual(_git(['status', '--porcelain'], cwd=self.record), '')


if __name__ == '__main__':
    unittest.main()
