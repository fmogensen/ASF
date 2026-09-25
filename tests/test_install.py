"""Install and upgrade: the package metadata, ``asf init``, the schema check and
``asf schema-migrate``, ``asf upgrade``, ``asf hooks install`` / ``asf hook``."""
import argparse
import contextlib
import datetime
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock

import asf
from asf import console_perms, conventions, env, hooks, init, schema, upgrade
from tests.gitfixture import executable_asf

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(args, cwd=None):
    env = {k: v for k, v in os.environ.items() if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, out.getvalue(), err.getvalue()


def _files(root):
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        out.update(os.path.relpath(os.path.join(dirpath, f), root) for f in filenames)
    return out


#: A literal that stands in for a secret in ``GitHookTests`` below — no real vendor shape, no
#: real name, so ``check_generic.sh``/``check_conventions.sh`` stay clean on this file itself.
_MARKER = 'THE-SECRET-MARKER'


def _stub_asf(bin_dir):
    """A stand-in ``asf`` executable for ``GitHookTests``: ``asf.redact`` (Task 8350 of
    ``docs/plans/f-0075.md``) is not yet in this checkout, so this script answers
    ``redact --pre-commit|--pre-push --product <p>`` in its place — refusing when the change it
    is asked about contains :data:`_MARKER`, exactly as the real scanner will (D8: the marker
    itself is never printed). It exercises this Task's own surface — is the right hook written,
    does a refusal really block a commit or a push, is the index a hook sees the one git is
    committing (D12) — not the scanner's patterns, which are Task 8350's to test."""
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, 'asf')
    with open(path, 'w') as f:
        f.write(f'''#!/usr/bin/env python3
import subprocess, sys

def refuse():
    print('redact: refused — 1 finding(s)')
    print('x:1: secret (rule:stand-in)')
    sys.exit(1)

if sys.argv[1:3] == ['redact', '--pre-commit']:
    diff = subprocess.run(['git', 'diff', '--cached', '-U0'], capture_output=True, text=True).stdout
    refuse() if {_MARKER!r} in diff else sys.exit(0)
elif sys.argv[1:3] == ['redact', '--pre-push']:
    found = False
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] == '0' * 40:
            continue
        shown = subprocess.run(['git', 'show', parts[1]], capture_output=True, text=True).stdout
        found = found or {_MARKER!r} in shown
    refuse() if found else sys.exit(0)
else:
    sys.exit(0)
''')
    os.chmod(path, 0o755)
    return path


class HomeCase(unittest.TestCase):
    """A temp ``ASF_HOME``; nothing under the operator's real one is read or written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='install_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)


# ---- 1. the package ---------------------------------------------------------

class PackageTest(unittest.TestCase):
    def test_pyproject_declares_the_asf_script(self):
        with open(os.path.join(REPO, 'pyproject.toml'), 'rb') as f:
            data = tomllib.load(f)
        self.assertEqual(data['project']['scripts']['asf'], 'asf.cli:main')
        self.assertEqual(data['project']['name'], upgrade.PACKAGE_NAME)
        self.assertIn('version', data['project']['dynamic'])
        self.assertEqual(data['tool']['setuptools']['dynamic']['version'], {'attr': 'asf.__version__'})
        self.assertEqual(data['project'].get('dependencies', []), [])

    def test_pre_commit_hook_runs_the_suite_without_the_git_hook_environment(self):
        """The hook exports GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE; the suite it launches must not
        inherit them, or a test's temporary repository resolves to the real one (B-0011)."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = os.path.join(tmp, 'repo')
        os.makedirs(os.path.join(repo, 'tools'))
        os.makedirs(os.path.join(repo, 'bin'))
        _git(['init', '-q', repo])
        with open(os.path.join(repo, 'tools', 'check_generic.sh'), 'w') as f:
            f.write('exit 0\n')
        seen = os.path.join(tmp, 'seen')
        stub = os.path.join(repo, 'bin', 'python3')
        with open(stub, 'w') as f:
            f.write('#!/bin/sh\nenv | grep -E "^GIT_(DIR|WORK_TREE|INDEX_FILE)=" > "%s"\nexit 0\n' % seen)
        os.chmod(stub, 0o755)
        env = {**os.environ, 'PATH': os.path.join(repo, 'bin') + os.pathsep + os.environ['PATH'],
               'GIT_DIR': os.path.join(repo, '.git'), 'GIT_WORK_TREE': repo,
               'GIT_INDEX_FILE': os.path.join(repo, '.git', 'index')}
        r = subprocess.run(['bash', os.path.join(REPO, '.githooks', 'pre-commit')], cwd=repo,
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(seen) as f:
            self.assertEqual(f.read(), '')

    def test_version_is_semver(self):
        self.assertRegex(asf.__version__, r'^\d+\.\d+\.\d+$')
        self.assertRegex('v' + asf.__version__, r'^v\d+\.\d+\.\d+$')

    def test_version_flag(self):
        from asf import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(['--version'])
        self.assertEqual(out.getvalue().strip(), f'asf {cli.version_string()}')
        self.assertRegex(cli.version_string(), r'^(v\d+\.\d+\.\d+(\+\d+)?|\d+\.\d+\.\d+)( \(\w+\))?$')

    def test_workflow_installs_and_runs_the_suite(self):
        with open(os.path.join(REPO, '.github', 'workflows', 'tests.yml')) as f:
            text = f.read()
        self.assertIn('pipx install', text)
        self.assertIn('asf --version', text)
        self.assertIn('unittest discover -s tests', text)

    def test_register_commands_wires_every_command(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='command')
        init.register_commands(sub)
        for argv, fn in ((['init', '--product', 'p'], init.cmd_init),
                         (['schema-migrate', '--all', '--drain'], schema.cmd_schema_migrate),
                         (['upgrade'], upgrade.cmd_upgrade),
                         (['hooks', 'install', '--product', 'p'], hooks.cmd_hooks),
                         (['hook', 'r0001'], hooks.cmd_hook),
                         (['console-permissions', 'offer'], console_perms.cmd_console_permissions)):
            self.assertIs(p.parse_args(argv).run, fn)


# ---- 2. asf init ------------------------------------------------------------

class InitTest(HomeCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(self.repo, '.github', 'workflows'))
        os.makedirs(os.path.join(self.repo, 'docs', 'specs'))
        self.write(os.path.join(self.repo, 'package.json'), '{"scripts": {"test": "vitest"}}')
        _git(['init', '-q', '-b', 'trunk', self.repo])
        _git(['remote', 'add', 'origin', 'git@example.com:acme/sample.git'], self.repo)
        self.backlog = os.path.join(self.tmp, 'backlog')

    def run_init(self, **kw):
        a = argparse.Namespace(product='sample', repo=self.repo, backlog=self.backlog)
        for k, v in kw.items():
            setattr(a, k, v)
        return _quiet(init.cmd_init, a)

    def test_discovery_writes_the_product_yaml(self):
        rc, out, err = self.run_init()
        self.assertEqual(rc, 0, err)
        with open(env.product_path('sample')) as f:
            text = f.read()
        data = env.loads(text)
        self.assertEqual(data['repo_slug'], 'acme/sample')
        self.assertEqual(data['main'], 'trunk')
        self.assertEqual(data['backlog_dir'], self.backlog)
        self.assertEqual(data['conventions']['specs_dir'], 'docs/specs')
        self.assertIsNone(data['conventions']['plans_dir'])
        self.assertIn('plans_dir:   # TODO', text)
        self.assertEqual(data['ci'], {'provider': 'gh-actions', 'test_command': 'npm test'})

    def test_b0051_discovery_writes_every_branch_prefix_so_none_is_forgotten(self):
        # a product yaml that only lists the prefix an operator happened to override loses the
        # rest of `conventions.all_prefixes()` — a branch harvest never learns to look for
        # (B-0051). `asf init` writes every kind explicitly so there is nothing left to forget.
        rc, out, err = self.run_init()
        self.assertEqual(rc, 0, err)
        with open(env.product_path('sample')) as f:
            data = env.loads(f.read())
        expected = {k: v for k, v in conventions.DEFAULT_BRANCH_PREFIXES.items() if k != 'legacy'}
        self.assertEqual(data['conventions']['branch_prefixes'], expected)

    def test_new_record_is_laid_down(self):
        rc, out, err = self.run_init()
        self.assertEqual(rc, 0, err)
        self.assertIn('init: laid down a new record', out)
        self.assertIn('ROADMAP', out)
        for folder in init.ITEM_FOLDERS + init.STREAM_FOLDERS:
            self.assertTrue(os.path.isdir(os.path.join(self.backlog, folder)), folder)
        self.assertTrue(os.path.isfile(os.path.join(self.backlog, 'README.md')))
        hook = os.path.join(self.backlog, '.githooks', 'pre-commit')
        self.assertTrue(os.access(hook, os.X_OK))
        with open(hook) as f:
            self.assertIn('asf check', f.read())
        self.assertEqual(_git(['config', 'core.hooksPath'], self.backlog), '.githooks')
        with open(os.path.join(self.backlog, 'index.json')) as f:
            idx = json.load(f)
        self.assertEqual(idx['schema_version'], schema.SCHEMA_VERSION)
        self.assertEqual(idx['items'], {})
        self.assertTrue(init.is_asf_record(self.backlog))

    def test_existing_yaml_is_never_overwritten(self):
        path = env.product_path('sample')
        self.write(path, f'repo_slug: other/thing\nbacklog_dir: {self.backlog}\n')
        with open(path, 'rb') as f:
            before = f.read()
        rc, out, err = self.run_init(backlog=None)
        self.assertEqual(rc, 0, err)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertIn('exists — left as is', out)
        self.assertIn('-repo_slug: other/thing', out)
        self.assertIn('+repo_slug: acme/sample', out)

    def test_existing_record_is_adopted_as_is(self):
        for folder in init.ITEM_FOLDERS:
            os.makedirs(os.path.join(self.backlog, folder))
        self.write(os.path.join(self.backlog, 'epics', 'E-0001.md'),
                   '---\nid: E-0001\ntype: epic\ntitle: A goal\nrank: 1\n# ---- machine ----\nstate: New\n---\n## Description\n\n'
                   '## Children\n\n## Backlinks\n')
        idx_path = os.path.join(self.backlog, 'index.json')
        self.write(idx_path, '{"items": {}}')  # a record from before schema stamps
        _quiet(__import__('asf.record.index', fromlist=['do_index']).do_index, self.backlog)
        with open(idx_path) as f:
            before_idx = json.load(f)
        self.assertNotIn('schema_version', before_idx)
        before_files = _files(self.backlog)
        with open(os.path.join(self.backlog, 'epics', 'E-0001.md'), 'rb') as f:
            before_card = f.read()

        rc, out, err = self.run_init()
        self.assertEqual(rc, 0, err)
        self.assertIn('init: adopted 1 items', out)
        self.assertEqual(_files(self.backlog), before_files)
        with open(os.path.join(self.backlog, 'epics', 'E-0001.md'), 'rb') as f:
            self.assertEqual(f.read(), before_card)
        with open(idx_path) as f:
            after_idx = json.load(f)
        self.assertEqual(after_idx.pop('schema_version'), schema.SCHEMA_VERSION)
        self.assertEqual(after_idx, before_idx)

    def test_adopt_keeps_an_older_stamp(self):
        for folder in init.ITEM_FOLDERS:
            os.makedirs(os.path.join(self.backlog, folder))
        self.write(os.path.join(self.backlog, 'index.json'), json.dumps({'items': {}, 'schema_version': 1}))
        with mock.patch.object(schema, 'SCHEMA_VERSION', 2):
            rc, out, err = self.run_init()
        self.assertEqual(rc, 0, err)
        self.assertEqual(schema.record_version(self.backlog), 1)

    def test_no_backlog_is_an_operator_question(self):
        rc, out, err = self.run_init(backlog=None)
        self.assertEqual(rc, 2)
        self.assertIn('NEEDS OPERATOR: no backlog dir', err)

    def test_slug_from_url(self):
        self.assertEqual(init.slug_from_url('https://example.com/acme/sample.git'), 'acme/sample')
        self.assertEqual(init.slug_from_url('git@example.com:acme/sample'), 'acme/sample')
        self.assertIsNone(init.slug_from_url('/tmp/origin.git'))
        self.assertIsNone(init.slug_from_url(None))

    def test_specs_and_plans_found_wherever_the_product_keeps_them(self):
        with tempfile.TemporaryDirectory() as repo:
            for d in ('specs', 'plans'):  # at the repo root
                os.makedirs(os.path.join(repo, d))
            self.assertEqual(init._first_dir(repo, init.SPECS_GLOBS), 'specs')
            self.assertEqual(init._first_dir(repo, init.PLANS_GLOBS), 'plans')
        with tempfile.TemporaryDirectory() as repo:
            for d in ('docs/team/specs', 'docs/team/plans'):  # nested under docs/, any name
                os.makedirs(os.path.join(repo, d))
            self.assertEqual(init._first_dir(repo, init.SPECS_GLOBS), 'docs/team/specs')
            self.assertEqual(init._first_dir(repo, init.PLANS_GLOBS), 'docs/team/plans')


# ---- 3. the schema check and asf schema-migrate -----------------------------

class SchemaTest(HomeCase):
    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, 'origin.git')
        seed = os.path.join(self.tmp, 'seed')
        _git(['init', '-q', '--bare', '-b', 'main', self.origin])
        _git(['clone', '-q', self.origin, seed])
        _git(['config', 'user.email', 'seed@example.com'], seed)
        _git(['config', 'user.name', 'seed'], seed)
        self.write(os.path.join(seed, 'index.json'), json.dumps({'items': {}, 'schema_version': 1}))
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'seed'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)
        self.operator = os.path.join(self.tmp, 'operator')
        _git(['clone', '-q', self.origin, self.operator])
        self.write(env.product_path('sample'), f'repo_slug: x/y\nbacklog_dir: {self.operator}\n')
        self.write(env.config_path(), 'default_product: sample\n')
        self.product = env.load_product('sample')

    def origin_log(self):
        return _git(['log', '--format=%s', 'main'], self.origin).splitlines()

    def sessions(self, lines):
        self.write(os.path.join(env.ASF_HOME, 'state', 'sample', 'sessions.jsonl'),
                   ''.join(json.dumps(l) + '\n' for l in lines))

    def test_match_passes(self):
        self.assertEqual(schema.check(self.product), (True, 'schema 1'))
        self.assertTrue(schema.require(self.product))

    def test_mismatch_exits_3_with_the_operator_line(self):
        with mock.patch.object(schema, 'SCHEMA_VERSION', 2):
            ok, detail = schema.check(self.product)
            self.assertFalse(ok)
            self.assertIn('sample backlog at schema 1', detail)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                schema.require(self.product)
        self.assertEqual(cm.exception.code, 3)
        self.assertRegex(err.getvalue(), r'^NEEDS OPERATOR: run asf schema-migrate — package at schema 2, ')

    def test_config_mismatch_and_unstamped_record(self):
        self.write(env.config_path(), 'default_product: sample\nschema_version: 0\n')
        ok, detail = schema.check(self.product)
        self.assertFalse(ok)
        self.assertIn('config.yaml at schema 0', detail)
        self.write(env.config_path(), '')
        self.write(os.path.join(self.operator, 'index.json'), '{"items": {}}')
        self.assertIn('backlog at schema 0', schema.check(self.product)[1])

    def test_noop_migration_commits_nothing(self):
        before = self.origin_log()
        rc, msg = schema.migrate_product(self.product)
        self.assertEqual(rc, 0)
        self.assertIn('nothing to do', msg)
        self.assertEqual(self.origin_log(), before)

    def test_a_migration_bumps_the_stamp_and_commits(self):
        touched = []

        def to_2(record_dir):
            touched.append(record_dir)
            with open(os.path.join(record_dir, 'MIGRATED'), 'w') as f:
                f.write('2\n')

        with mock.patch.object(schema, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(schema.MIGRATIONS, {2: to_2}):
            rc, msg = schema.migrate_product(self.product)
            self.assertEqual(rc, 0, msg)
            self.assertIn('migrated 1 → 2', msg)
            self.assertEqual(self.origin_log()[0], 'migrate: schema 1 → 2')
            clone = os.path.join(env.ASF_HOME, 'state', 'sample', 'record')
            self.assertEqual(schema.record_version(clone), 2)
            # idempotent: a second run finds nothing to do and commits nothing
            before = self.origin_log()
            rc, msg = schema.migrate_product(self.product)
            self.assertIn('nothing to do', msg)
            self.assertEqual(self.origin_log(), before)
        self.assertEqual(len(touched), 1)

    def test_an_unstamped_record_is_stamped_by_migration_1(self):
        clone = os.path.join(self.tmp, 'bare-record')
        os.makedirs(clone)
        self.write(os.path.join(clone, 'index.json'), '{"items": {}}')
        msgs = []
        self.assertEqual(schema.migrate_dir(clone, commit=msgs.append), [(0, 1)])
        self.assertEqual(msgs, ['migrate: schema 0 → 1'])
        self.assertEqual(schema.migrate_dir(clone, commit=msgs.append), [])

    def test_a_newer_record_refuses(self):
        clone = os.path.join(self.tmp, 'bare-record')
        self.write(os.path.join(clone, 'index.json'), '{"items": {}, "schema_version": 9}')
        with self.assertRaises(env.ConfigError):
            schema.migrate_dir(clone)

    # The ledger in the tick's own shape: a launch line (job, started, pid), a finish line keyed
    # by ``job`` with ``ended``, a correction line with no pid.
    def launch(self, job, pid, **kw):
        return dict({'job': job, 'started': '2026-09-24T10:00:00Z', 'pid': pid,
                     'product': 'sample'}, **kw)

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        return p.pid

    def test_inflight_session_refuses(self):
        self.sessions([self.launch('a', os.getpid()), self.launch('b', os.getpid()),
                       {'job': 'b', 'ended': '2026-09-24T10:05:00Z', 'end_reason': 'finished'}])
        rc, msg = schema.migrate_product(self.product)
        self.assertEqual(rc, 1)
        self.assertIn('1 session(s) in flight (a)', msg)
        self.assertIn('--drain', msg)
        self.assertFalse(os.path.isdir(os.path.join(env.ASF_HOME, 'state', 'sample', 'record')))

    def test_a_job_keyed_ended_line_closes_the_run(self):
        self.sessions([self.launch('a', os.getpid()),
                       {'job': 'a', 'ended': '2026-09-24T10:05:00Z', 'end_reason': 'finished'}])
        self.assertEqual(schema.sessions_in_flight('sample'), [])

    def test_a_correction_line_with_no_pid_is_not_a_session(self):
        self.sessions([{'job': 'a', 'correction': {'text': 'fix it', 'at': 't'}},
                       {'job': 'b', 'corrected': True}])
        self.assertEqual(schema.sessions_in_flight('sample'), [])

    def test_a_dead_pid_is_not_in_flight(self):
        self.sessions([self.launch('a', self.dead_pid())])
        self.assertEqual(schema.sessions_in_flight('sample'), [])

    def test_an_ok_result_in_the_job_log_is_not_in_flight(self):
        log = os.path.join(self.tmp, 'a.jsonl')
        self.write(log, json.dumps({'type': 'system', 'subtype': 'init'}) + '\n'
                   + json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                 'result': 'done'}) + '\n')
        self.sessions([self.launch('a', os.getpid(), log=log)])
        self.assertEqual(schema.sessions_in_flight('sample'), [])

    def test_a_live_pid_is_in_flight_and_the_drain_waits(self):
        self.sessions([self.launch('a', os.getpid()), self.launch('b', self.dead_pid()),
                       {'job': 'c', 'correction': {'text': 'x', 'at': 't'}}])
        self.assertEqual(schema.sessions_in_flight('sample'), ['a'])
        polls = []

        def sleep(s):
            polls.append(s)
            if len(polls) == 2:
                self.sessions([self.launch('a', os.getpid()),
                               {'job': 'a', 'ended': '2026-09-24T10:05:00Z'}])

        with mock.patch.object(schema, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(schema.MIGRATIONS, {2: lambda d: None}):
            rc, msg = schema.migrate_product(self.product, drain=True, sleep=sleep)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(polls, [schema.DRAIN_POLL_S] * 2)
        self.assertEqual(self.origin_log()[0], 'migrate: schema 1 → 2')

    def test_drain_gives_up_at_the_timeout(self):
        self.sessions([self.launch('a', os.getpid())])
        t = [0]

        def sleep(s):
            t[0] += s

        rc, msg = schema.migrate_product(self.product, drain=True, drain_timeout=60, sleep=sleep,
                                         clock=lambda: t[0])
        self.assertEqual(rc, 1)
        self.assertIn('gave up after 60s', msg)
        self.assertIn('still in flight: a', msg)

    def test_cmd_updates_the_config_stamp(self):
        self.write(env.config_path(), 'default_product: sample\nschema_version: 0\n')
        args = argparse.Namespace(product='sample', all=False, drain=False, drain_timeout=10)
        rc, out, _err = _quiet(schema.cmd_schema_migrate, args)
        self.assertEqual(rc, 0)
        self.assertEqual(schema.config_version(), 1)
        self.assertIn('config.yaml schema_version → 1', out)


# ---- 4. asf upgrade, asf hooks install, asf hook ----------------------------

class FakeRun:
    """``subprocess.run`` for the upgrade: answers by the command's first words."""
    HEAD = 'a' * 40

    def __init__(self, ticks='', ci='[]', installed=None, pipx_rc=0):
        self.calls = []
        self.answers = {
            ('pipx', 'list'): json.dumps({'venvs': {'asf-factory': {'metadata': {'main_package': {
                'package_or_url': 'git+https://github.com/o/r.git@1234567'}}}}}),
            ('git', 'ls-remote'): f'{self.HEAD}\trefs/heads/main\n',
            ('pgrep', '-f'): ticks,
            ('gh', 'run'): ci,
            ('pipx', 'environment'): '/venvs\n',
            ('/venvs/asf-factory/bin/python', '-c'): (installed or self.HEAD) + '\n',
            ('launchctl', 'list'): 'PID\tStatus\tLabel\n',
        }
        self.pipx_rc = pipx_rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:2] == ['pipx', 'install']:
            return mock.Mock(returncode=self.pipx_rc, stdout='')
        return mock.Mock(returncode=0, stdout=self.answers.get(tuple(cmd[:2]), ''))

    def installs(self):
        return [c for c in self.calls if c[:2] == ['pipx', 'install']]


class UpgradeTest(HomeCase):
    def run_upgrade(self, run, ref=None):
        return _quiet(upgrade.cmd_upgrade, argparse.Namespace(skip_pipx=False, ref=ref), run=run)

    def test_the_command_reinstalls_the_pin_at_a_named_commit(self):
        self.assertEqual(upgrade.upgrade_command('https://github.com/o/r.git', 'abc'),
                         ['pipx', 'install', '--force', 'git+https://github.com/o/r.git@abc'])

    def test_runs_pipx_at_mains_head_then_prints_the_table(self):
        good, old = os.path.join(self.tmp, 'good'), os.path.join(self.tmp, 'old')
        self.write(os.path.join(good, 'index.json'), '{"items": {}, "schema_version": 1}')
        self.write(os.path.join(old, 'index.json'), '{"items": {}}')
        self.write(env.product_path('alpha'), f'backlog_dir: {good}\n')
        self.write(env.product_path('beta'), f'backlog_dir: {old}\n')
        run = FakeRun()
        rc, out, _err = self.run_upgrade(run)
        self.assertEqual(rc, 0)
        self.assertEqual(run.installs(), [['pipx', 'install', '--force',
                                           f'git+https://github.com/o/r.git@{FakeRun.HEAD}']])
        self.assertIn(f'upgrade: installed {FakeRun.HEAD[:7]}', out)
        self.assertIn('| product | record schema | package | action |', out)
        self.assertIn('| alpha | 1 | 1 | none |', out)
        self.assertIn('| beta | 0 | 1 | asf schema-migrate --product beta |', out)

    def test_the_tick_installs_the_head_drift_read(self):
        run = FakeRun(installed='b' * 40)
        rc, _out, _err = self.run_upgrade(run, ref='b' * 40)
        self.assertEqual(rc, 0)
        self.assertEqual(run.installs()[0][-1], f'git+https://github.com/o/r.git@{"b" * 40}')

    def test_another_running_tick_defers_it(self):
        run = FakeRun(ticks='4242\n')
        rc, out, _err = self.run_upgrade(run)
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertEqual(run.installs(), [])
        self.assertIn('upgrade: deferred', out)
        self.assertIn('4242', out)
        self.assertIn('rerun with --wait', out)  # a manual run is never silently deferred

    def test_the_pattern_counts_the_background_harvest(self):
        import re
        pat = re.compile(upgrade.TICK_PATTERN)
        self.assertTrue(pat.search('/usr/bin/python3 -m asf.tick.step_harvest --product asf '
                                   '--items /Users/x/.ASF/state/asf/harvest-items.json'))
        self.assertFalse(pat.search('/usr/bin/python3 -m asf.cli status'))

    def test_the_pattern_matches_ticks_not_shells_that_name_them(self):
        import re
        pat = re.compile(upgrade.TICK_PATTERN)
        self.assertTrue(pat.search('/usr/bin/python3 -m asf.cli tick --product asf --steps record'))
        self.assertTrue(pat.search('/usr/bin/python3 /Users/x/.local/bin/asf tick --product asf'))
        self.assertFalse(pat.search('zsh -c until ! pgrep -f "asf.cli tick"; do sleep 5; done'))

    def test_its_own_tick_does_not_defer_it(self):
        run = FakeRun(ticks=f'{os.getpid()}\n')
        rc, _out, _err = self.run_upgrade(run)
        self.assertEqual(rc, 0)
        self.assertEqual(len(run.installs()), 1)

    def test_a_red_head_is_not_installed(self):
        run = FakeRun(ci='[{"conclusion": "success"}, {"conclusion": "failure"}]')
        rc, out, _err = self.run_upgrade(run)
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertEqual(run.installs(), [])
        self.assertIn('remote CI is red', out)

    def test_an_install_that_did_not_move_fails(self):
        run = FakeRun(installed='c' * 40)
        rc, out, _err = self.run_upgrade(run)
        self.assertEqual(rc, 1)
        self.assertIn('upgrade: FAILED', out)
        self.assertNotIn('| product |', out)

    def test_a_failed_pipx_stops(self):
        rc, out, _err = self.run_upgrade(FakeRun(pipx_rc=1))
        self.assertEqual(rc, 1)
        self.assertNotIn('| product |', out)

    def test_an_unloaded_clock_is_reloaded_and_a_loaded_one_left_alone(self):
        agents = os.path.join(self.tmp, 'LaunchAgents')
        for label in ('asf.alpha.tick', 'asf.alpha.daily', 'asf.beta.tick'):
            self.write(os.path.join(agents, f'{label}.plist'), '')
        self.write(env.config_path(), '')
        run = FakeRun()
        run.answers[('launchctl', 'list')] = '-\t0\tasf.alpha.daily\n'
        with mock.patch('asf.scheduler.launch_agents_dir', return_value=agents), \
                mock.patch('asf.scheduler.bootstrap', return_value=(True, '')) as boot:
            lines = upgrade.reload_clocks(['alpha'], run=run)
        boot.assert_called_once_with(os.path.join(agents, 'asf.alpha.tick.plist'))
        self.assertEqual(lines, ['upgrade: reloaded clock asf.alpha.tick'])


class PendingUpgradeTest(HomeCase):
    """A deferred upgrade marks itself pending, so the other ticks stop starting and a gap comes."""
    SHA = 'b' * 40

    def run_upgrade(self, run, owner='factory'):
        return _quiet(upgrade.cmd_upgrade,
                      argparse.Namespace(skip_pipx=False, ref=self.SHA, owner=owner), run=run)

    def test_a_deferral_writes_the_marker(self):
        rc, out, _err = self.run_upgrade(FakeRun(ticks='4242\n'))
        self.assertEqual(rc, upgrade.DEFERRED)
        data = upgrade.read_pending()
        self.assertEqual((data['sha'], data['owner']), (self.SHA, 'factory'))
        self.assertLess(abs(data['at'] - time.time()), 60)
        self.assertIn(f'upgrade: pending {self.SHA[:7]}', out)

    def test_a_manual_deferral_writes_none(self):
        self.run_upgrade(FakeRun(ticks='4242\n'), owner=None)
        self.assertIsNone(upgrade.read_pending())

    def test_a_later_deferral_keeps_the_first_timestamp(self):
        upgrade.write_pending('c' * 40, 'factory', now=1000.0)
        upgrade.write_pending(self.SHA, 'factory', now=5000.0)
        self.assertEqual(upgrade.read_pending()['at'], 1000.0)
        self.assertEqual(upgrade.read_pending()['sha'], self.SHA)

    def test_other_products_ticks_wait_and_the_owners_go_on(self):
        upgrade.write_pending(self.SHA, 'factory')
        lines = []
        self.assertTrue(upgrade.waiting('other', out=lines.append, installed='c' * 40))
        self.assertEqual(lines, [f'tick: waiting — upgrade to {self.SHA[:7]} pending'])
        self.assertFalse(upgrade.waiting('factory', out=lines.append, installed='c' * 40))

    def test_a_waiting_tick_starts_no_step(self):
        upgrade.write_pending(self.SHA, 'factory')
        product = env.Product('other', {'repo_dir': self.tmp, 'main': 'main', 'ci': {'provider': 'none'}})
        from asf.tick import tick
        with mock.patch('asf.env.load_product', return_value=product), \
                mock.patch('asf.tick.steps.resolve', return_value=[('harvest', 'asf', None)]), \
                mock.patch('asf.tick.steps.check_owned'), \
                mock.patch('asf.drift.installed_commit', return_value='c' * 40), \
                mock.patch.object(tick, 'acquire_lock') as lock, \
                mock.patch.object(tick, '_run_locked') as ran:
            rc, out, _err = _quiet(tick.cmd_tick, argparse.Namespace(product='other', steps=None))
        self.assertEqual(rc, 0)
        self.assertIn('tick: waiting — upgrade to', out)
        lock.assert_not_called()
        ran.assert_not_called()

    def test_the_upgrade_clears_the_marker(self):
        upgrade.write_pending(self.SHA, 'factory')
        rc, _out, _err = self.run_upgrade(FakeRun(installed=self.SHA))
        self.assertEqual(rc, 0)
        self.assertIsNone(upgrade.read_pending())
        self.assertFalse(os.path.exists(upgrade.pending_path()))

    def test_the_upgrading_tick_does_not_count_itself(self):
        upgrade.write_pending(self.SHA, 'factory')
        rc, _out, _err = self.run_upgrade(FakeRun(ticks=f'{os.getpid()}\n{os.getppid()}\n',
                                                  installed=self.SHA))
        self.assertEqual(rc, 0)
        self.assertIsNone(upgrade.read_pending())

    def test_a_stale_marker_is_ignored_and_removed(self):
        upgrade.write_pending(self.SHA, 'factory', now=time.time() - upgrade.PENDING_TTL_S - 1)
        self.assertFalse(upgrade.waiting('other', out=lambda _l: None, installed='c' * 40))
        self.assertFalse(os.path.exists(upgrade.pending_path()))

    def test_a_marker_whose_sha_is_installed_is_ignored_and_removed(self):
        upgrade.write_pending(self.SHA, 'factory')
        self.assertFalse(upgrade.waiting('other', out=lambda _l: None, installed=self.SHA))
        self.assertFalse(os.path.exists(upgrade.pending_path()))
        self.assertIsNone(upgrade.cooling())  # an installed marker is no expiry

    def test_the_pending_ttl_is_at_most_twenty_minutes(self):
        self.assertLessEqual(upgrade.PENDING_TTL_S, 20 * 60)

    def test_a_timed_out_marker_resumes_the_parked_ticks_loudly_and_does_not_repark_them(self):
        upgrade.write_pending(self.SHA, 'factory', now=time.time() - upgrade.PENDING_TTL_S - 60)
        lines = []
        self.assertFalse(upgrade.waiting('other', out=lines.append, installed='c' * 40))
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(f'NEEDS OPERATOR: upgrade to {self.SHA[:7]} pending'),
                        lines)
        self.assertIn('the parked ticks resume', lines[0])
        # the owner's next deferral does not park them again straight away
        rc, out, _err = self.run_upgrade(FakeRun(ticks='4242\n'))
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertIsNone(upgrade.read_pending())
        self.assertIn('no pending mark until', out)
        self.assertFalse(upgrade.waiting('other', out=lines.append, installed='c' * 40))
        # once the cool-down has passed, a deferral parks them again
        self.assertIsNotNone(upgrade.write_pending(self.SHA, 'factory',
                                                   now=time.time() + upgrade.PENDING_TTL_S + 1))


class AncestorPendingTest(HomeCase):
    """A newer install can carry the pending sha as an ancestor rather than as its exact head
    (B-0128 live incident, 2026-09-26): the marker must clear as soon as that install lands,
    never ride out the TTL and fire a false NEEDS OPERATOR."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        _git(['init', '-q', '-b', 'main'], self.repo)
        _git(['config', 'user.email', 't@example.com'], self.repo)
        _git(['config', 'user.name', 't'], self.repo)
        self.write(os.path.join(self.repo, 'root.txt'), 'root\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'root'], self.repo)
        root = _git(['rev-parse', 'HEAD'], self.repo)
        self.write(os.path.join(self.repo, 'a.txt'), 'one\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'first'], self.repo)
        self.pending_sha = _git(['rev-parse', 'HEAD'], self.repo)
        self.write(os.path.join(self.repo, 'b.txt'), 'two\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'second'], self.repo)
        self.installed_sha = _git(['rev-parse', 'HEAD'], self.repo)  # pending_sha is its ancestor
        _git(['checkout', '-q', '-b', 'other', root], self.repo)  # diverges before pending_sha
        self.write(os.path.join(self.repo, 'c.txt'), 'three\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'unrelated'], self.repo)
        self.unrelated_sha = _git(['rev-parse', 'HEAD'], self.repo)  # does not contain pending_sha
        _git(['checkout', '-q', 'main'], self.repo)
        patch = mock.patch('asf.drift.factory_root', return_value=self.repo)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_newer_install_containing_the_pending_sha_clears_the_marker(self):
        upgrade.write_pending(self.pending_sha, 'factory')
        lines = []
        self.assertFalse(upgrade.waiting('other', out=lines.append, installed=self.installed_sha))
        self.assertEqual(lines, [])  # no park, no NEEDS OPERATOR
        self.assertIsNone(upgrade.read_pending())

    def test_an_already_expired_marker_thats_actually_installed_clears_silently(self):
        # a leftover expired.json (a prior, unrelated cool-down) must not survive either
        old_at = time.time() - upgrade.PENDING_TTL_S - 60
        upgrade._write_json(upgrade.pending_path(),
                            {'sha': self.pending_sha, 'owner': 'factory', 'at': old_at})
        upgrade._write_json(upgrade.expired_path(),
                            {'sha': 'deadbeef' * 5, 'owner': 'factory', 'at': time.time() - 5})
        lines = []
        self.assertFalse(upgrade.waiting('other', out=lines.append, installed=self.installed_sha))
        self.assertEqual(lines, [])  # never NEEDS OPERATOR
        self.assertIsNone(upgrade.read_pending())
        self.assertFalse(os.path.exists(upgrade.expired_path()))

    def test_an_unrelated_install_leaves_the_marker_pending(self):
        upgrade.write_pending(self.pending_sha, 'factory')
        lines = []
        self.assertTrue(upgrade.waiting('other', out=lines.append, installed=self.unrelated_sha))
        self.assertEqual(lines, [f'tick: waiting — upgrade to {self.pending_sha[:7]} pending'])
        self.assertIsNotNone(upgrade.read_pending())

    def test_an_older_install_leaves_the_marker_pending(self):
        upgrade.write_pending(self.installed_sha, 'factory')  # pending is ahead of the install
        lines = []
        self.assertTrue(upgrade.waiting('other', out=lines.append, installed=self.pending_sha))
        self.assertIsNotNone(upgrade.read_pending())

    def test_a_git_error_falls_back_to_the_prefix_check(self):
        upgrade.write_pending(self.pending_sha, 'factory')
        with mock.patch('asf.drift.factory_root', return_value=os.path.join(self.tmp, 'no-such-repo')):
            lines = []
            self.assertTrue(upgrade.waiting('other', out=lines.append, installed=self.installed_sha))
        self.assertIsNotNone(upgrade.read_pending())  # the ancestor check errored — prefix says no

    def test_no_factory_repo_falls_back_to_the_prefix_check(self):
        upgrade.write_pending(self.pending_sha, 'factory')
        with mock.patch('asf.drift.factory_root', return_value=None):
            lines = []
            self.assertTrue(upgrade.waiting('other', out=lines.append, installed=self.installed_sha))
        self.assertIsNotNone(upgrade.read_pending())

    def test_exact_match_still_works_without_touching_git(self):
        upgrade.write_pending(self.pending_sha, 'factory')
        with mock.patch('asf.drift.factory_root', side_effect=AssertionError('should not be called')):
            self.assertFalse(upgrade.waiting('other', out=lambda _l: None,
                                             installed=self.pending_sha))
        self.assertIsNone(upgrade.read_pending())


class SequencedRun(FakeRun):
    """``FakeRun`` whose ``pgrep`` answers from a list, one per call (the last one repeats)."""

    def __init__(self, pgreps, **kw):
        super().__init__(**kw)
        self.pgreps = list(pgreps)

    def __call__(self, cmd, **kw):
        if cmd[:2] == ['pgrep', '-f']:
            self.calls.append(cmd)
            answer = self.pgreps.pop(0) if len(self.pgreps) > 1 else self.pgreps[0]
            return mock.Mock(returncode=0 if answer else 1, stdout=answer)
        if cmd[:1] == ['ps']:
            self.calls.append(cmd)
            return mock.Mock(returncode=0, stdout=(
                '54171   10:09 /usr/bin/python3 -m asf.tick.step_harvest --product asf\n'))
        return super().__call__(cmd, **kw)


class DrainTest(HomeCase):
    """The owner's tick drains the floor for up to ``upgrade.drain_wait_s``, then installs."""
    SHA = 'b' * 40

    def upgrade(self, run, owner='factory', wait=None, ref=SHA):
        slept = []
        rc, out, _err = _quiet(upgrade.cmd_upgrade, argparse.Namespace(
            skip_pipx=False, ref=ref, owner=owner, wait=wait, sleep=slept.append), run=run)
        return rc, out, slept

    def test_the_owner_drains_a_live_background_harvest_then_installs(self):
        # a background harvest (pid 54171) is running at the owner's start; it ends two polls on
        run = SequencedRun(['54171\n', '54171\n', '54171\n', ''], installed=self.SHA)
        rc, out, slept = self.upgrade(run, wait=180)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(run.installs()), 1)
        self.assertEqual(len(slept), 3)
        self.assertLessEqual(sum(slept), 180)
        self.assertIn('upgrade: waiting up to 180s for 1 asf process(es) to end:', out)
        self.assertIn('54171 (10:09) -m asf.tick.step_harvest --product asf', out)
        self.assertIn(f'upgrade: installed {self.SHA[:7]}', out)
        self.assertIsNone(upgrade.read_pending())  # the install clears the mark

    def test_the_owner_marks_pending_before_it_waits(self):
        seen = []

        def sleep(_s):
            seen.append(upgrade.read_pending())
        run = SequencedRun(['4242\n', ''], installed=self.SHA)
        _quiet(upgrade.cmd_upgrade, argparse.Namespace(
            skip_pipx=False, ref=self.SHA, owner='factory', wait=60, sleep=sleep), run=run)
        self.assertEqual(seen[0]['sha'], self.SHA)  # parked the others while it drained

    def test_the_wait_is_bounded_and_keeps_the_mark_for_the_next_start(self):
        run = SequencedRun(['4242\n'], installed=self.SHA)
        rc, out, slept = self.upgrade(run, wait=30)
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertEqual(run.installs(), [])
        self.assertEqual(sum(slept), 30)
        self.assertEqual(upgrade.read_pending()['sha'], self.SHA)
        self.assertIn('upgrade: deferred to the next tick', out)

    def test_the_drain_wait_reads_the_operator_config(self):
        self.assertEqual(upgrade.drain_wait_s({}), upgrade.DEFAULT_DRAIN_WAIT_S)
        self.assertEqual(upgrade.drain_wait_s({'upgrade': {'drain_wait_s': 45}}), 45)
        self.assertEqual(upgrade.drain_wait_s({'upgrade': {'drain_wait_s': -1}}),
                         upgrade.DEFAULT_DRAIN_WAIT_S)

    def test_a_manual_wait_names_what_it_waits_on_and_installs(self):
        run = SequencedRun(['54171\n', ''], installed=FakeRun.HEAD)
        rc, out, slept = self.upgrade(run, owner=None, wait=600, ref=None)
        self.assertEqual(rc, 0, out)
        self.assertEqual(run.installs()[0][-1], f'git+https://github.com/o/r.git@{FakeRun.HEAD}')
        self.assertIn('54171 (10:09) -m asf.tick.step_harvest', out)
        self.assertEqual(len(slept), 1)
        self.assertIsNone(upgrade.read_pending())

    def test_a_manual_wait_parks_every_product_while_it_waits(self):
        seen = []

        def sleep(_s):
            seen.append(upgrade.waiting('other', out=lambda _l: None, installed='c' * 40))
        run = SequencedRun(['54171\n', ''], installed=FakeRun.HEAD)
        _quiet(upgrade.cmd_upgrade, argparse.Namespace(
            skip_pipx=False, ref=None, owner=None, wait=600, sleep=sleep), run=run)
        self.assertEqual(seen, [True])

    def test_a_manual_wait_that_times_out_says_so_and_lifts_its_mark(self):
        run = SequencedRun(['54171\n'], installed=FakeRun.HEAD)
        rc, out, slept = self.upgrade(run, owner=None, wait=20, ref=None)
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertEqual(run.installs(), [])
        self.assertEqual(sum(slept), 20)
        self.assertIn('upgrade: NOT installed', out)
        self.assertIsNone(upgrade.read_pending())  # never leaves the factory parked

    def test_the_cli_takes_wait_with_and_without_seconds(self):
        p = argparse.ArgumentParser()
        upgrade.register(p.add_subparsers())
        self.assertEqual(p.parse_args(['upgrade', '--wait']).wait, upgrade.DEFAULT_MANUAL_WAIT_S)
        self.assertEqual(p.parse_args(['upgrade', '--wait', '90']).wait, 90)
        self.assertIsNone(p.parse_args(['upgrade']).wait)


class HooksTest(HomeCase):
    def setUp(self):
        super().setUp()
        self.rules = os.path.join(self.tmp, 'rules')
        self.write(os.path.join(self.rules, 'R-0001.md'),
                   '---\nid: R-0001\ntype: rule\ntitle: guard\nhook: [PreToolUse, Stop]\n---\n')
        self.write(os.path.join(self.rules, 'R-0002.md'),
                   '---\nid: R-0002\ntype: rule\ntitle: no hook\nenforced: false\n---\n')
        self.repo = os.path.join(self.tmp, 'repo')
        _git(['init', '-q', self.repo])  # ensure_git_hooks (F-0075) needs a real git repo
        self.settings = os.path.join(self.repo, '.claude', 'settings.json')
        self.product = env.Product('sample', {'repo_dir': self.repo})
        self.asf = executable_asf(os.path.join(self.tmp, 'bin'))
        self.which = lambda name: self.asf
        self.write(env.config_path(), '')  # an empty config.yaml: no real account is touched

    def read(self):
        with open(self.settings) as f:
            return json.load(f)

    def test_declared_hooks(self):
        self.assertEqual(hooks.declared_hooks(self.rules), [('PreToolUse', 'r0001'), ('Stop', 'r0001')])
        self.assertEqual(hooks.declared_hooks(os.path.join(self.tmp, 'none')), [])

    def test_no_rules_declare_a_hook(self):
        rc, msg = hooks.install(self.product, rules_dir=os.path.join(self.tmp, 'none'), which=self.which)
        self.assertEqual((rc, msg),
                         (0, f'hooks: 0 rule hooks in {self.settings}; approvals in 0 worker accounts; '
                             'pre-commit, pre-push in 1 repos'))
        self.assertFalse(os.path.exists(self.settings))

    def test_merge_is_idempotent_and_keeps_unrelated_keys(self):
        self.write(self.settings, json.dumps({
            'permissions': {'allow': ['Bash(ls)']},
            'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'other'}]}]},
        }))
        rc, msg = hooks.install(self.product, rules_dir=self.rules, which=self.which)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(msg, f'hooks: 2 rule hooks in {self.settings}; approvals in 0 worker accounts; '
                              'pre-commit, pre-push in 1 repos')
        first = self.read()
        self.assertEqual(first['permissions'], {'allow': ['Bash(ls)']})
        pre = first['hooks']['PreToolUse']
        self.assertEqual(pre[0], {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'other'}]})
        self.assertEqual(pre[1]['hooks'][0]['command'], f'{self.asf} hook r0001 --product sample')
        self.assertEqual(first['hooks']['Stop'][0]['hooks'][0]['command'],
                         f'{self.asf} hook r0001 --product sample')
        with open(self.settings, 'rb') as f:
            before = f.read()
        rc, msg = hooks.install(self.product, rules_dir=self.rules, which=self.which)
        self.assertEqual(rc, 0, msg)
        with open(self.settings, 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_a_moved_asf_replaces_its_own_entry(self):
        hooks.install(self.product, rules_dir=self.rules, which=self.which)
        moved = executable_asf(os.path.join(self.tmp, 'new', 'bin'))
        hooks.install(self.product, rules_dir=self.rules, which=lambda n: moved)
        stop = self.read()['hooks']['Stop']
        self.assertEqual(len(stop), 1)
        self.assertEqual(stop[0]['hooks'], [{'type': 'command', 'command': f'{moved} hook r0001 --product sample'}])

    def test_asf_not_on_path(self):
        rc, msg = hooks.install(self.product, rules_dir=self.rules, which=lambda n: None)
        self.assertEqual(rc, 2)
        self.assertTrue(msg.startswith('NEEDS OPERATOR: asf is not on PATH — pipx install'))

    def test_hook_runs_the_check_script(self):
        backlog = os.path.join(self.tmp, 'backlog')
        self.write(env.product_path('sample'), f'backlog_dir: {backlog}\n')
        self.write(os.path.join(backlog, 'tools', 'checks', 'r0001.sh'), 'exit 7\n')
        self.assertEqual(hooks.cmd_hook(argparse.Namespace(name='r0001', product='sample')), 7)
        self.assertEqual(hooks.cmd_hook(argparse.Namespace(name='r0099', product='sample')), 0)
        self.assertIsNone(hooks.check_script('../x', 'sample'))


class ConsolePermissionsTest(HomeCase):
    """B-0131: `asf console-permissions offer` shows exactly the documented allow/deny list, and
    `install` writes it — idempotently, into the scope the operator picked — without touching
    unrelated keys."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.write(env.product_path('sample'), f'repo_dir: {self.repo}\nmain: trunk\n')

    def test_allow_rules_are_exactly_the_documented_ones(self):
        product = env.load_product('sample')
        # the five fixed rules from the card, verbatim, plus one push rule per lane branch
        # prefix this product declares (never a literal branch name)
        self.assertEqual(console_perms.allow_rules(product), (
            'Bash(asf:*)',
            'Bash(bash tools/install.sh:*)',
            'Bash(launchctl bootout gui/*/asf.*)',
            'Bash(launchctl bootstrap gui/*)',
            'Bash(git worktree:*)',
        ) + tuple(f'Bash(git push origin {p}*)' for p in product.conventions.all_prefixes()))
        self.assertEqual(console_perms.deny_rules(product),
                         ('Bash(git push --force* origin trunk)',))

    def test_offer_prints_every_rule_and_writes_nothing(self):
        product = env.load_product('sample')
        text = console_perms.offer_text(product)
        for rule in console_perms.allow_rules(product):
            self.assertIn(rule, text)
        for rule in console_perms.deny_rules(product):
            self.assertIn(rule, text)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'home', '.claude', 'settings.json')))
        self.assertFalse(os.path.exists(os.path.join(self.repo, '.claude', 'settings.json')))

    def test_install_writes_the_user_scope_by_default_and_is_idempotent(self):
        fake_home = os.path.join(self.tmp, 'fake-home')
        os.makedirs(fake_home)
        with mock.patch.dict(os.environ, {'HOME': fake_home}):
            rc = console_perms.cmd_console_permissions(
                argparse.Namespace(console_permissions_command='install', product='sample',
                                   scope='user'))
            self.assertEqual(rc, 0)
            written = console_perms.settings_paths(env.load_product('sample'))[0]
        self.assertEqual(written, os.path.join(fake_home, '.claude', 'settings.json'))
        with open(written) as f:
            data = json.load(f)
        product = env.load_product('sample')
        self.assertEqual(set(data['permissions']['allow']), set(console_perms.allow_rules(product)))
        self.assertEqual(data['permissions']['deny'], list(console_perms.deny_rules(product)))
        with open(written, 'rb') as f:
            before = f.read()
        with mock.patch.dict(os.environ, {'HOME': fake_home}):
            rc = console_perms.cmd_console_permissions(
                argparse.Namespace(console_permissions_command='install', product='sample',
                                   scope='user'))
        self.assertEqual(rc, 0)
        with open(written, 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_install_repo_scope_keeps_unrelated_keys(self):
        settings = os.path.join(self.repo, '.claude', 'settings.json')
        self.write(settings, json.dumps({'permissions': {'allow': ['Bash(ls)']}}))
        rc = console_perms.cmd_console_permissions(
            argparse.Namespace(console_permissions_command='install', product='sample', scope='repo'))
        self.assertEqual(rc, 0)
        with open(settings) as f:
            data = json.load(f)
        self.assertIn('Bash(ls)', data['permissions']['allow'])
        for rule in console_perms.allow_rules(env.load_product('sample')):
            self.assertIn(rule, data['permissions']['allow'])
        before = data
        rc = console_perms.cmd_console_permissions(
            argparse.Namespace(console_permissions_command='install', product='sample', scope='repo'))
        self.assertEqual(rc, 0)
        with open(settings) as f:
            self.assertEqual(json.load(f), before)

    def test_repo_scope_without_a_repo_dir_is_a_needs_operator_refusal(self):
        self.write(env.product_path('bare'), 'repo_slug: x/y\n')
        rc = console_perms.cmd_console_permissions(
            argparse.Namespace(console_permissions_command='install', product='bare', scope='repo'))
        self.assertEqual(rc, 2)

    def test_doctor_check_names_missing_rules_by_name(self):
        product = env.load_product('sample')
        ok, detail = console_perms.check_doctor(product, home=os.path.join(self.tmp, 'no-such-home'))
        self.assertFalse(ok)
        self.assertIn('Bash(asf:*)', detail)
        self.assertIn('Bash(git push --force* origin trunk)', detail)


class GitHookTests(unittest.TestCase):
    """T-0025 (F-0075 Task 8353, ``docs/plans/f-0075.md``): the git hooks ``asf hooks install``
    writes for ``asf redact``, and that an installed hook really refuses a commit or a push. See
    :func:`_stub_asf` for why these stand a fake ``asf`` in for the scanner Task 8350 owns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='git_hooks_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.asf_path = _stub_asf(os.path.join(self.tmp, 'bin'))
        self.which = lambda name: self.asf_path if name == 'asf' else None

    def _repo(self, name, bare=False):
        path = os.path.join(self.tmp, name)
        args = ['init', '-q', '-b', 'main']
        if bare:
            args.append('--bare')
        _git(args + [path])
        return path

    def _product(self, repo_dir=None, backlog_dir=None, name='sample'):
        data = {}
        if repo_dir:
            data['repo_dir'] = repo_dir
        if backlog_dir:
            data['backlog_dir'] = backlog_dir
        return env.Product(name, data)

    def test_a_hook_is_never_written_naming_an_asf_that_is_not_there(self):
        # review-b-0111: a fixture hook naming /x/asf reached a worker commit and refused it
        repo = self._repo('repo')
        product = self._product(repo_dir=repo)
        hooks_dir = hooks.git_hooks_dir(repo)
        missing = os.path.join(self.tmp, 'nowhere', 'asf')
        plain = os.path.join(self.tmp, 'plain', 'asf')
        os.makedirs(os.path.dirname(plain))
        with open(plain, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(plain, 0o644)                     # there, but not executable
        for bad in (missing, plain, os.path.dirname(plain)):
            ok, detail = hooks.ensure_git_hooks(product, which=lambda _n, bad=bad: bad)
            self.assertFalse(ok, bad)
            self.assertIn(f'NEEDS OPERATOR: {bad} is not an executable asf', detail)
            for name in hooks.GIT_HOOK_NAMES:
                self.assertFalse(os.path.exists(os.path.join(hooks_dir, name)), (bad, name))
            rc, msg = hooks.install(product, rules_dir=os.path.join(self.tmp, 'none'),
                                    which=lambda _n, bad=bad: bad, cfg={})
            self.assertEqual(rc, 2, msg)
            self.assertFalse(os.path.exists(os.path.join(hooks_dir, 'pre-commit')), bad)

    def test_a_git_dir_inherited_from_a_hook_never_redirects_the_write(self):
        # a suite run from another repo's hook carries its GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE:
        # the hooks go to the repo asked for, never the caller's
        caller, repo = self._repo('caller'), self._repo('repo')
        product = self._product(repo_dir=repo)
        leaked = {'GIT_DIR': os.path.join(caller, '.git'), 'GIT_WORK_TREE': caller,
                  'GIT_INDEX_FILE': os.path.join(caller, '.git', 'index')}
        with mock.patch.dict(os.environ, leaked):
            self.assertEqual(os.path.realpath(hooks.git_hooks_dir(repo)),
                             os.path.realpath(os.path.join(repo, '.git', 'hooks')))
            ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isfile(os.path.join(repo, '.git', 'hooks', 'pre-commit')))
        self.assertFalse(os.path.exists(os.path.join(caller, '.git', 'hooks', 'pre-commit')))

    def test_install_writes_pre_commit_and_pre_push_in_repo_and_record(self):
        repo, record = self._repo('repo'), self._repo('record')
        product = self._product(repo_dir=repo, backlog_dir=record)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, 'pre-commit, pre-push in 2 repos')
        for d in (repo, record):
            for name in ('pre-commit', 'pre-push'):
                path = os.path.join(d, '.git', 'hooks', name)
                self.assertTrue(os.access(path, os.X_OK), path)
                with open(path) as f:
                    text = f.read()
                self.assertIn(f'exec "{self.asf_path}" redact --{name} --product sample', text)

    def test_install_honours_core_hooks_path(self):
        repo = self._repo('repo')
        custom = os.path.join(repo, 'custom-hooks')
        os.makedirs(custom)
        _git(['config', 'core.hooksPath', 'custom-hooks'], repo)
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.isfile(os.path.join(custom, 'pre-commit')))
        self.assertTrue(os.path.isfile(os.path.join(custom, 'pre-push')))
        self.assertFalse(os.path.isfile(os.path.join(repo, '.git', 'hooks', 'pre-commit')))

    def test_install_is_idempotent(self):
        repo = self._repo('repo')
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        path = os.path.join(repo, '.git', 'hooks', 'pre-commit')
        with open(path, 'rb') as f:
            before = f.read()
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_a_foreign_hook_is_not_touched_and_is_one_needs_operator_line(self):
        repo = self._repo('repo')
        foreign = os.path.join(repo, '.git', 'hooks', 'pre-push')
        with open(foreign, 'w') as f:
            f.write('#!/bin/sh\necho not asf\n')
        os.chmod(foreign, 0o755)
        with open(foreign, 'rb') as f:
            before = f.read()
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('NEEDS OPERATOR: '), detail)
        self.assertIn(foreign, detail)
        self.assertIn(f'"{self.asf_path}" redact --pre-push --product sample', detail)
        with open(foreign, 'rb') as f:
            self.assertEqual(f.read(), before)
        # the pre-commit hook, which was not in the way, is still written
        self.assertTrue(os.path.isfile(os.path.join(repo, '.git', 'hooks', 'pre-commit')))

    def test_an_asf_init_record_hook_is_asfs_own_and_a_new_product_starts_green(self):
        """The card: asf init writes a pre-commit that doctor then calls foreign."""
        from asf import doctor, init
        record = self._repo('record')
        init.lay_down(record)
        product = self._product(backlog_dir=record)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        ok, detail = doctor.check_redaction_hooks(product)
        self.assertTrue(ok, detail)
        with open(os.path.join(record, '.githooks', 'pre-commit')) as f:
            self.assertEqual(f.read(), init.PRE_COMMIT)   # left as asf init wrote it

    def test_an_older_asf_init_hook_is_brought_up_to_date_not_called_foreign(self):
        from asf import doctor, init
        record = self._repo('record')
        init.lay_down(record)
        old = init.PRE_COMMIT.split('# the redaction gate')[0].replace(' || exit 1', '')
        path = os.path.join(record, '.githooks', 'pre-commit')
        with open(path, 'w') as f:
            f.write(old)
        product = self._product(backlog_dir=record)
        ok, detail = doctor.check_redaction_hooks(product)
        self.assertFalse(ok)
        self.assertIn("asf init's, from before it ran the redaction gate", detail)
        self.assertNotIn('foreign', detail)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        with open(path) as f:
            self.assertEqual(f.read(), init.PRE_COMMIT)
        self.assertTrue(doctor.check_redaction_hooks(product)[0])

    def _clone_with_installed_hooks(self):
        origin = self._repo('origin', bare=True)
        repo = os.path.join(self.tmp, 'clone')
        _git(['clone', '-q', origin, repo])
        _git(['config', 'user.email', 'test@example.com'], repo)
        _git(['config', 'user.name', 'test'], repo)
        with open(os.path.join(repo, 'seed'), 'w') as f:
            f.write('seed\n')
        _git(['add', 'seed'], repo)
        _git(['commit', '-q', '-m', 'seed'], repo)
        _git(['push', '-q', 'origin', 'HEAD:main'], repo)
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertTrue(ok, detail)
        worktree = os.path.join(self.tmp, 'wt')
        _git(['worktree', 'add', '-q', '-b', 'wtbranch', worktree], repo)
        _git(['config', 'user.email', 'test@example.com'], worktree)
        _git(['config', 'user.name', 'test'], worktree)
        return origin, worktree

    def test_a_commit_in_a_worktree_is_refused_by_the_installed_hook(self):
        origin, worktree = self._clone_with_installed_hooks()
        with open(os.path.join(worktree, 'secret.txt'), 'w') as f:
            f.write(_MARKER + '\n')
        _git(['add', 'secret.txt'], worktree)
        before = _git(['rev-parse', 'HEAD'], worktree)
        r = subprocess.run(['git', 'commit', '-q', '-m', 'wip'], cwd=worktree,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('secret', r.stdout + r.stderr)
        self.assertNotIn(_MARKER, r.stdout + r.stderr)
        self.assertEqual(_git(['rev-parse', 'HEAD'], worktree), before)  # no commit was made

    def test_a_push_from_a_worktree_is_refused_by_the_installed_hook(self):
        origin, worktree = self._clone_with_installed_hooks()
        with open(os.path.join(worktree, 'secret.txt'), 'w') as f:
            f.write(_MARKER + '\n')
        _git(['add', 'secret.txt'], worktree)
        _git(['commit', '-q', '--no-verify', '-m', 'wip'], worktree)  # past pre-commit, so push is what's under test
        r = subprocess.run(['git', 'push', 'origin', 'wtbranch'], cwd=worktree,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('secret', r.stdout + r.stderr)
        self.assertNotIn(_MARKER, r.stdout + r.stderr)
        self.assertEqual(_git(['for-each-ref', 'refs/heads/wtbranch'], origin), '')  # unchanged origin

    def test_a_commit_only_path_is_scanned_against_the_index_git_is_committing(self):
        # D12: `git commit --only <path>` builds a *temporary* index (HEAD's tree plus only the
        # named path) and runs the hook against it. `dirty.txt` is staged in the *real* index —
        # elsewhere, not part of this commit — so a hook that scanned the real index instead of
        # the temporary one git actually built would wrongly refuse the first commit below.
        origin, worktree = self._clone_with_installed_hooks()
        with open(os.path.join(worktree, 'clean.txt'), 'w') as f:
            f.write('clean\n')
        _git(['add', 'clean.txt'], worktree)
        with open(os.path.join(worktree, 'dirty.txt'), 'w') as f:
            f.write(_MARKER + '\n')
        _git(['add', 'dirty.txt'], worktree)
        r = subprocess.run(['git', 'commit', '--only', '-m', 'clean only', 'clean.txt'],
                           cwd=worktree, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        with open(os.path.join(worktree, 'clean.txt'), 'w') as f:
            f.write(_MARKER + '\n')
        _git(['add', 'clean.txt'], worktree)
        r = subprocess.run(['git', 'commit', '--only', '-m', 'now dirty', 'clean.txt'],
                           cwd=worktree, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('secret', r.stdout + r.stderr)
        self.assertNotIn(_MARKER, r.stdout + r.stderr)

    def test_the_tracked_pre_push_hook_refuses_an_unpublished_secret(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        origin = os.path.join(tmp, 'origin.git')
        _git(['init', '-q', '--bare', '-b', 'main', origin])
        repo = os.path.join(tmp, 'repo')
        _git(['clone', '-q', origin, repo])
        _git(['config', 'user.email', 'test@example.com'], repo)
        _git(['config', 'user.name', 'test'], repo)
        shutil.copytree(os.path.join(REPO, '.githooks'), os.path.join(repo, '.githooks'))
        # AWS-shaped access key, built from parts so this test file itself stays clean.
        secret = 'AKIA' + 'Q' * 16
        with open(os.path.join(repo, 'creds.txt'), 'w') as f:
            f.write(secret + '\n')
        _git(['add', 'creds.txt'], repo)
        _git(['commit', '-q', '-m', 'wip'], repo)
        # core.hooksPath is set only now: setting it before the commit above would make that
        # commit run the real, unstubbed pre-commit hook — the whole suite, recursively — since
        # this fixture repo has no tests/ dir of its own and `discover -s tests` falls back to
        # whatever `tests` package PYTHONPATH resolves to.
        _git(['config', 'core.hooksPath', '.githooks'], repo)
        r = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=repo,
                           env={**os.environ, 'PYTHONPATH': REPO},
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('secret', r.stdout + r.stderr)
        self.assertNotIn(secret, r.stdout + r.stderr)
        # nothing was published: ask for the refs that exist, never `log main` — on a bare repo
        # with no commits that is an unknown revision, and git exits 128 (B-0038's class again)
        published = _git(['for-each-ref', '--format=%(refname)', 'refs/heads/'], origin)
        self.assertEqual(published, '')


class NoCheckoutPathsTest(unittest.TestCase):
    """The install modules name no tools directory under the operator's home and no checkout."""

    def test_no_tools_dir_or_checkout_path(self):
        for mod in ('init', 'schema', 'upgrade', 'hooks'):
            with open(os.path.join(REPO, 'asf', f'{mod}.py'), encoding='utf-8') as f:
                text = f.read()
            self.assertNotRegex(text, r"\.ASF/tools|ASF_HOME, 'tools'|~/Code/", mod)


def cli_release(described):
    from asf import cli
    return cli._described_release(described)


class VersionStringTest(unittest.TestCase):
    """``asf --version`` names the release and the commit it was built from. The release: a git
    install's requested tag, else a checkout's nearest release tag (``+N`` past it), else the static
    ``__version__``. The commit: a checkout's HEAD, else ``direct_url.json``'s."""

    def test_a_checkout_names_its_own_head(self):
        from asf import cli
        head = _git(['rev-parse', '--short', 'HEAD'], REPO)
        self.assertTrue(cli.version_string().endswith(f' ({head})'))

    def _no_checkout(self):
        failed = subprocess.CompletedProcess([], 128, '', 'not a git repository')
        return mock.patch('asf.cli.subprocess.run', return_value=failed)

    def _dist(self, vcs_info):
        dist = mock.Mock()
        dist.read_text.return_value = json.dumps(
            {'url': 'https://example.invalid/asf.git', 'vcs_info': {'vcs': 'git', **vcs_info}})
        return mock.patch('importlib.metadata.distribution', return_value=dist)

    def test_a_git_install_of_a_tag_is_that_release(self):
        from asf import cli
        with self._no_checkout(), self._dist({'requested_revision': 'v0.1.1',
                                              'commit_id': '47bab2d' + '0' * 33}):
            self.assertEqual(cli.version_string(), 'v0.1.1 (47bab2d)')

    def test_a_git_install_of_a_sha_falls_to_the_static_version(self):
        from asf import cli
        sha = 'abcdef0123456789abcdef0123456789abcdef01'
        with self._no_checkout(), self._dist({'requested_revision': sha, 'commit_id': sha}), \
                mock.patch('asf.cli._build_describe', return_value=''):
            self.assertEqual(cli.version_string(), f'{asf.__version__} (abcdef0)')

    def test_a_git_install_of_a_sha_reads_the_describe_its_build_stamped(self):
        """``install.sh`` pins a sha, so ``asf status`` said ``running 0.1.0 (205123f)`` for
        v0.1.9-4-g205123f: the build's stamp is the release."""
        from asf import cli
        sha = '205123fb967285035c2106194884aece17071cb0'
        with self._no_checkout(), self._dist({'requested_revision': '205123f', 'commit_id': sha}):
            for described, want in (('v0.1.9-4-g205123f', 'v0.1.9+4 (205123f)'),
                                    ('v0.1.9', 'v0.1.9 (205123f)')):
                with self.subTest(described=described), \
                        mock.patch('asf.cli._build_describe', return_value=described):
                    self.assertEqual(cli.version_string(), want)

    def test_the_build_stamps_git_describe_into_the_package_it_builds(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('asf_setup', os.path.join(REPO, 'setup.py'))
        setup_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup_mod)     # not __main__: no setuptools, no setup()
        with tempfile.TemporaryDirectory() as tmp:
            src, lib = os.path.join(tmp, 'src'), os.path.join(tmp, 'lib')
            os.makedirs(src)
            self._repo_with_tag(src, past=2)
            path = setup_mod.stamp(src, lib)
            self.assertEqual(path, os.path.join(lib, 'asf', '_build.py'))
            ns = {}
            with open(path, encoding='utf-8') as f:
                exec(f.read(), ns)
            self.assertEqual(cli_release(ns['DESCRIBE']), 'v0.1.1+2')
            self.assertEqual(os.listdir(src), ['.git'])     # the checkout is never written

    def test_a_git_install_of_a_sha_in_a_checkout_falls_to_describe(self):
        from asf import cli
        with tempfile.TemporaryDirectory() as tmp:
            self._repo_with_tag(tmp, past=0)
            sha = 'abcdef0123456789abcdef0123456789abcdef01'
            direct = {'vcs_info': {'requested_revision': sha, 'commit_id': sha}}
            self.assertEqual(cli._release(tmp, direct), 'v0.1.1')

    def _repo_with_tag(self, tmp, past):
        _git(['init', '-q', '-b', 'main'], tmp)
        for i in range(past + 1):
            _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
                  '-m', f'c{i}'], tmp)
            if i == 0:
                _git(['tag', 'v0.1.0'], tmp)
                _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q',
                      '--allow-empty', '-m', 'release'], tmp)
                _git(['tag', 'not-a-release'], tmp)
                _git(['tag', 'v0.1.1'], tmp)

    def test_a_checkout_at_a_tag_is_that_release(self):
        from asf import cli
        with tempfile.TemporaryDirectory() as tmp:
            self._repo_with_tag(tmp, past=0)
            self.assertEqual(cli._release(tmp, {}), 'v0.1.1')

    def test_a_checkout_past_a_tag_counts_the_commits(self):
        from asf import cli
        with tempfile.TemporaryDirectory() as tmp:
            self._repo_with_tag(tmp, past=3)
            self.assertEqual(cli._release(tmp, {}), 'v0.1.1+3')

    def test_a_checkout_without_a_release_tag_has_none(self):
        from asf import cli
        with tempfile.TemporaryDirectory() as tmp:
            _git(['init', '-q', '-b', 'main'], tmp)
            _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
                  '-m', 'c'], tmp)
            _git(['tag', 'release-2026-09-24-abc'], tmp)
            self.assertIsNone(cli._release(tmp, {}))

    def test_the_latest_release_is_the_newest_version_tag_and_its_date(self):
        from asf import cli
        with tempfile.TemporaryDirectory() as tmp:
            self._repo_with_tag(tmp, past=2)
            _git(['tag', 'v0.1.10'], tmp)                       # numeric, not lexical: 10 > 1
            with mock.patch.object(cli, '_checkout_root', return_value=tmp):
                tag, when = cli.latest_release()
        self.assertEqual(tag, 'v0.1.10')
        self.assertLess(abs((datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()), 600)

    def test_the_latest_release_of_a_git_install_is_its_remote_newest_tag(self):
        from asf import cli
        listed = subprocess.CompletedProcess([], 0, 'aa\trefs/tags/v0.1.2\nbb\trefs/tags/v0.1.10\n'
                                                    'cc\trefs/tags/release-x\n', '')
        with mock.patch.object(cli, '_checkout_root', return_value=None), self._dist({}), \
                mock.patch('asf.cli.subprocess.run', return_value=listed):
            self.assertEqual(cli.latest_release(), ('v0.1.10', None))
        with mock.patch.object(cli, '_checkout_root', return_value=None), \
                mock.patch.object(cli, '_direct_url', return_value={}):
            self.assertIsNone(cli.latest_release())

    def test_status_shows_the_running_version_and_the_latest_release(self):
        from asf import cli
        from asf.views import status
        now = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(cli, 'version_string', return_value='v0.1.1+42 (abc1234)'), \
                mock.patch.object(cli, 'latest_release',
                                  return_value=('v0.1.2', now - datetime.timedelta(minutes=5))):
            self.assertEqual(status.version_cell(now),
                             'running v0.1.1+42 (abc1234) · latest release v0.1.2 (5m ago)')
        with mock.patch.object(cli, 'version_string', return_value='v0.1.2 (abc1234)'), \
                mock.patch.object(cli, 'latest_release', return_value=None):
            self.assertEqual(status.version_cell(now), 'running v0.1.2 (abc1234) · latest release —')
        with mock.patch.object(status, 'version_cell', return_value='running X'), \
                mock.patch.object(status, 'runners_cell', return_value='r'):
            table = status.render('/nonexistent', None, cfg={})
        self.assertIn('| Version | running X |', table)

    def test_a_pipx_git_install_names_the_direct_url_commit(self):
        from asf import cli
        dist = mock.Mock()
        dist.read_text.return_value = json.dumps(
            {'url': 'https://example.invalid/asf.git',
             'vcs_info': {'vcs': 'git', 'commit_id': 'abcdef0123456789abcdef0123456789abcdef01'}})
        with self._no_checkout(), mock.patch('importlib.metadata.distribution', return_value=dist):
            self.assertEqual(cli.version_string(), f'{asf.__version__} (abcdef0)')
        dist.read_text.assert_called_with('direct_url.json')

    def test_neither_known_is_the_bare_version(self):
        import importlib.metadata
        from asf import cli
        with self._no_checkout(), mock.patch('importlib.metadata.distribution',
                                             side_effect=importlib.metadata.PackageNotFoundError):
            self.assertEqual(cli.version_string(), asf.__version__)
        dist = mock.Mock()
        dist.read_text.return_value = json.dumps({'url': 'file:///x', 'dir_info': {'editable': True}})
        with self._no_checkout(), mock.patch('importlib.metadata.distribution', return_value=dist):
            self.assertEqual(cli.version_string(), asf.__version__)


#: The one-product install script, as the suite runs it.
INSTALL_SH = os.path.join(REPO, 'tools', 'install.sh')


class InstallScriptTest(unittest.TestCase):
    """``install.sh`` on a stubbed PATH: a failing hooks step (a foreign hook, exit 2) does not
    abort the run — the scheduler install and the doctor still run, the failed step is named,
    and the script exits non-zero."""

    #: A clock still on disk but not in `launchctl list` — exactly the B-0136 incident.
    NOT_LOADED = 'asf.demo.record-health-wave-prs-harvest  not loaded'

    def _run(self, hooks_rc=0, status_results=(('0', ''),)):
        """``status_results``: ``[(rc, stdout), ...]`` for successive ``scheduler status`` calls
        (the last entry repeats for any call beyond the list) — how install.sh's own retry
        of the bootstrap sees the clock check."""
        tmp = tempfile.mkdtemp(prefix='install_sh_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir, home = os.path.join(tmp, 'bin'), os.path.join(tmp, 'home')
        asf_home, log = os.path.join(home, '.ASF'), os.path.join(tmp, 'calls.log')
        counter = os.path.join(tmp, 'status_calls')
        os.makedirs(bin_dir)
        os.makedirs(os.path.join(asf_home, 'products'))
        for rel in ('config.yaml', os.path.join('products', 'demo.yaml')):
            open(os.path.join(asf_home, rel), 'w').close()

        cases = []
        for i, (rc, out) in enumerate(status_results, start=1):
            echo = f'printf %s\\\\n {shlex.quote(out)}; ' if out else ''
            cases.append(f'      {i}) {echo}exit {rc} ;;')
        last_rc, last_out = status_results[-1]
        echo = f'printf %s\\\\n {shlex.quote(last_out)}; ' if last_out else ''
        cases.append(f'      *) {echo}exit {last_rc} ;;')
        status_case = '\n'.join(cases)

        scripts = {
            'pipx': '#!/bin/sh\nexit 0\n',
            'asf': ('#!/bin/sh\n'
                    f'echo "$*" >> "{log}"\n'
                    'if [ "$1 $2" = "scheduler status" ]; then\n'
                    f'  n=0; [ -f "{counter}" ] && n=$(cat "{counter}")\n'
                    f'  n=$((n + 1)); echo "$n" > "{counter}"\n'
                    '  case "$n" in\n'
                    f'{status_case}\n'
                    '  esac\n'
                    'fi\n'
                    'case "$1" in\n'
                    '  --version) echo "asf 0.0.0 (stub)";;\n'
                    f'  hooks) echo "NEEDS OPERATOR: pre-push is not asf\'s" >&2; exit {hooks_rc};;\n'
                    '  console-permissions) echo "console-permissions: allow Bash(asf:*)";;\n'
                    'esac\n'
                    'exit 0\n'),
        }
        for name, body in scripts.items():
            path = os.path.join(bin_dir, name)
            with open(path, 'w') as f:
                f.write(body)
            os.chmod(path, 0o755)
        run_env = dict(os.environ, HOME=home, ASF_HOME=asf_home,
                       PATH=bin_dir + os.pathsep + os.environ.get('PATH', ''))
        r = subprocess.run(['bash', INSTALL_SH, 'demo', 'deadbeef'], capture_output=True,
                           text=True, env=run_env, timeout=60)
        with open(log) as f:
            calls = [line.split()[0] for line in f if line.strip()]
        return r, calls

    def test_a_failed_hooks_step_still_runs_the_scheduler_and_the_doctor(self):
        r, calls = self._run(hooks_rc=2)
        self.assertEqual(calls, ['--version', 'hooks', 'scheduler', 'scheduler', 'doctor',
                                 'console-permissions'], r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 3: asf hooks install --product demo (exit 2)', r.stderr)
        self.assertNotIn('FAILED step 4', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)

    def test_every_step_green_exits_zero(self):
        r, calls = self._run(hooks_rc=0)
        self.assertEqual(calls, ['--version', 'hooks', 'scheduler', 'scheduler', 'doctor',
                                 'console-permissions'], r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)

    def test_an_unloaded_clock_is_retried_once_and_then_succeeds(self):
        """B-0136: the first read-back finds the clock not loaded; install.sh retries the
        bootstrap once and, once that clock is loaded, the step is not a failure."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED), ('0', '')])
        self.assertEqual(calls, ['--version', 'hooks',
                                 'scheduler', 'scheduler',   # install, then the failing status
                                 'scheduler', 'scheduler',   # the retried install, then status
                                 'doctor', 'console-permissions'], r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)
        self.assertIn('retrying the bootstrap once', r.stdout + r.stderr)

    def test_an_unloaded_clock_still_missing_after_retry_fails_loudly_naming_it(self):
        """B-0136's own acceptance: a clock still not loaded after the retry fails the install
        loudly, naming the missing label — not just a bare non-zero exit."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED)])
        self.assertEqual(calls, ['--version', 'hooks', 'scheduler', 'scheduler',
                                 'scheduler', 'scheduler', 'doctor', 'console-permissions'],
                         r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 4: asf scheduler install --product demo (exit 1)',
                      r.stderr)
        self.assertIn('install: NEEDS OPERATOR: clock(s) still not loaded after retrying the '
                      'bootstrap: asf.demo.record-health-wave-prs-harvest', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)  # steps 5 and 6 still ran

    def test_step_6_offers_the_console_permissions_and_writes_nothing(self):
        r, calls = self._run(hooks_rc=0)
        self.assertIn('console-permissions: allow Bash(asf:*)', r.stdout)
        self.assertIn('run one to write it: asf console-permissions install --product demo '
                      '--scope user|repo', r.stdout)

    def test_an_unloaded_clock_is_retried_once_and_then_succeeds(self):
        """B-0136: the first read-back finds the clock not loaded; install.sh retries the
        bootstrap once and, once that clock is loaded, the step is not a failure."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED), ('0', '')])
        self.assertEqual(calls, ['--version', 'hooks',
                                 'scheduler', 'scheduler',   # install, then the failing status
                                 'scheduler', 'scheduler',   # the retried install, then status
                                 'doctor', 'console-permissions'], r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)
        self.assertIn('retrying the bootstrap once', r.stdout + r.stderr)

    def test_an_unloaded_clock_still_missing_after_retry_fails_loudly_naming_it(self):
        """B-0136's own acceptance: a clock still not loaded after the retry fails the install
        loudly, naming the missing label — not just a bare non-zero exit."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED)])
        self.assertEqual(calls, ['--version', 'hooks', 'scheduler', 'scheduler',
                                 'scheduler', 'scheduler', 'doctor', 'console-permissions'], r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 4: asf scheduler install --product demo (exit 1)',
                      r.stderr)
        self.assertIn('install: NEEDS OPERATOR: clock(s) still not loaded after retrying the '
                      'bootstrap: asf.demo.record-health-wave-prs-harvest', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)  # step 6 still ran

    def test_step_6_offers_the_console_permissions_and_writes_nothing(self):
        r, calls = self._run(hooks_rc=0)
        self.assertIn('console-permissions: allow Bash(asf:*)', r.stdout)
        self.assertIn('run one to write it: asf console-permissions install --product demo '
                      '--scope user|repo', r.stdout)

    def test_an_unloaded_clock_is_retried_once_and_then_succeeds(self):
        """B-0136: the first read-back finds the clock not loaded; install.sh retries the
        bootstrap once and, once that clock is loaded, the step is not a failure."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED), ('0', '')])
        self.assertEqual(calls, ['--version', 'hooks',
                                 'scheduler', 'scheduler',   # install, then the failing status
                                 'scheduler', 'scheduler',   # the retried install, then status
                                 'doctor', 'console-permissions'], r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)
        self.assertIn('retrying the bootstrap once', r.stdout + r.stderr)

    def test_an_unloaded_clock_still_missing_after_retry_fails_loudly_naming_it(self):
        """B-0136's own acceptance: a clock still not loaded after the retry fails the install
        loudly, naming the missing label — not just a bare non-zero exit."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED)])
        self.assertEqual(calls, ['--version', 'hooks', 'scheduler', 'scheduler',
                                 'scheduler', 'scheduler', 'doctor', 'console-permissions'], r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 4: asf scheduler install --product demo (exit 1)',
                      r.stderr)
        self.assertIn('install: NEEDS OPERATOR: clock(s) still not loaded after retrying the '
                      'bootstrap: asf.demo.record-health-wave-prs-harvest', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)  # step 6 still ran

    def test_step_6_offers_the_console_permissions_and_writes_nothing(self):
        r, calls = self._run(hooks_rc=0)
        self.assertIn('console-permissions: allow Bash(asf:*)', r.stdout)
        self.assertIn('run one to write it: asf console-permissions install --product demo '
                      '--scope user|repo', r.stdout)

    def test_an_unloaded_clock_is_retried_once_and_then_succeeds(self):
        """B-0136: the first read-back finds the clock not loaded; install.sh retries the
        bootstrap once and, once that clock is loaded, the step is not a failure."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED), ('0', '')])
        self.assertEqual(calls, ['--version', 'hooks',
                                 'scheduler', 'scheduler',   # install, then the failing status
                                 'scheduler', 'scheduler',   # the retried install, then status
                                 'doctor', 'console-permissions'], r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)
        self.assertIn('retrying the bootstrap once', r.stdout + r.stderr)

    def test_an_unloaded_clock_still_missing_after_retry_fails_loudly_naming_it(self):
        """B-0136's own acceptance: a clock still not loaded after the retry fails the install
        loudly, naming the missing label — not just a bare non-zero exit."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED)])
        self.assertEqual(calls, ['--version', 'hooks', 'scheduler', 'scheduler',
                                 'scheduler', 'scheduler', 'doctor', 'console-permissions'],
                         r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 4: asf scheduler install --product demo (exit 1)',
                      r.stderr)
        self.assertIn('install: NEEDS OPERATOR: clock(s) still not loaded after retrying the '
                      'bootstrap: asf.demo.record-health-wave-prs-harvest', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)  # steps 5 and 6 still ran

if __name__ == '__main__':
    unittest.main()
