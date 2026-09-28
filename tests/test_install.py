"""Install and upgrade: the package metadata, ``asf init``, the schema check and
``asf schema-migrate``, ``asf upgrade``, ``asf hooks install`` / ``asf hook``."""
import argparse
import contextlib
import datetime
import glob
import hashlib
import importlib.util
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
from asf import cli, console_perms, conventions, env, hooks, init, install, schema, upgrade
from tests.gitfixture import executable_asf

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(args, cwd=None):
    env = {k: v for k, v in os.environ.items() if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _publish(tree, origin):
    """``tree`` becomes a git repo whose ``main`` is pushed to a new bare ``origin`` —
    ``tests/test_sample_product.py``'s own fixture, needed here too for a product whose
    ``repo_dir``/``backlog_dir`` are real git repos (D12's shape, run through ``asf install``
    instead of ``asf init``)."""
    _git(['init', '-q', '--bare', '-b', 'main', origin])
    _git(['init', '-q', '-b', 'main'], cwd=tree)
    _git(['config', 'user.email', 'sample@example.com'], cwd=tree)
    _git(['config', 'user.name', 'sample'], cwd=tree)
    _git(['add', '-A'], cwd=tree)
    _git(['commit', '-q', '-m', 'sample'], cwd=tree)
    _git(['remote', 'add', 'origin', origin], cwd=tree)
    _git(['push', '-q', '-u', 'origin', 'main'], cwd=tree)
    _git(['remote', 'set-head', 'origin', 'main'], cwd=tree)


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


def _keys(data, prefix=''):
    """Every dotted key path a parsed config carries — a list of maps (``worker_pool.accounts``)
    unions its items' keys under the same path, index dropped, so one entry missing a key the
    others carry still reads as documented."""
    out = set()
    if isinstance(data, dict):
        for k, v in data.items():
            dotted = f'{prefix}.{k}' if prefix else k
            out.add(dotted)
            out |= _keys(v, dotted)
    elif isinstance(data, list):
        for item in data:
            out |= _keys(item, prefix)
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

    def test_a_red_head_writes_no_marker_and_clears_the_one_it_holds(self):
        """B-0141 (live, 2026-09-25 21:44–22:10Z): main's head was red, which the install
        refuses — but the deferral marked it pending anyway and parked every other product's
        ticks until the marker expired. 0 sessions ran, 8 were ready. A marker is written only
        for a target the upgrade will actually install, and one the moved head has made
        uninstallable goes at once."""
        upgrade.write_pending('c' * 40, 'factory')
        run = FakeRun(ticks='4242\n', ci=json.dumps([{'conclusion': 'failure'}]))
        rc, out, _err = self.run_upgrade(run)
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertIsNone(upgrade.read_pending())
        self.assertFalse(os.path.exists(upgrade.pending_path()))
        self.assertIn(f'remote CI is red at {self.SHA[:7]}', out)
        self.assertFalse(upgrade.waiting('other', out=lambda _l: None, installed='d' * 40))

    def test_a_green_head_still_writes_the_marker(self):
        rc, _out, _err = self.run_upgrade(FakeRun(ticks='4242\n',
                                                  ci=json.dumps([{'conclusion': 'success'}])))
        self.assertEqual(rc, upgrade.DEFERRED)
        self.assertEqual(upgrade.read_pending()['sha'], self.SHA)

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

    def test_the_pending_ttl_is_ten_minutes(self):
        """B-0141: a marker nothing clears held the whole factory for half an hour."""
        self.assertEqual(upgrade.PENDING_TTL_S, 10 * 60)

    def test_held_reports_a_wait_to_exactly_the_products_that_are_waiting(self):
        """B-0141 review round 1, C1 and C2: `held` is what the doctor and the status Cron row
        read, so it answers the same question `waiting` does — for the owner, whose ticks go on,
        and for the markers no tick honours, the answer is no wait at all."""
        upgrade.write_pending(self.SHA, 'factory')
        self.assertIsNone(upgrade.held('factory'))  # the owner drains and installs; it ticks
        self.assertEqual(upgrade.held('other')['sha'], self.SHA)
        self.assertEqual(upgrade.held()['sha'], self.SHA)  # no product named: the marker itself

        # each of the three below is a marker still on disk that no tick honours: `held` reads it
        # and reports no wait, rather than leaving the doctor to announce one nobody is serving
        upgrade.clear_pending()  # an operator's wait whose process was killed
        upgrade.write_pending(self.SHA, None)
        data = upgrade.read_pending()
        data['pid'] = 999999
        upgrade._write_json(upgrade.pending_path(), data)
        self.assertIsNone(upgrade.held('other'))
        self.assertFalse(upgrade.waiting('other', out=lambda _l: None, installed='c' * 40))

        upgrade.clear_pending()  # a clock step dated the marker in the future
        upgrade.clear_expired()  # the drop above left no cool-down, but the one below would
        self.assertIsNotNone(upgrade.write_pending(self.SHA, 'factory', now=time.time() + 3600))
        self.assertIsNone(upgrade.held('other'))

        upgrade.clear_pending()  # past the TTL, every tick resumes
        self.assertIsNotNone(upgrade.write_pending(
            self.SHA, 'factory', now=time.time() - upgrade.PENDING_TTL_S - 1))
        self.assertIsNone(upgrade.held('other'))

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


class EnvRun(FakeRun):
    """``FakeRun`` whose ``ps eww`` answers each pid's environment from a dict (pid -> env text);
    a pid missing from it prints nothing, as for a process ``ps`` cannot read."""

    def __init__(self, envs, pgreps, **kw):
        super().__init__(**kw)
        self.envs, self.pgreps = envs, list(pgreps)

    def __call__(self, cmd, **kw):
        if cmd[:2] == ['pgrep', '-f']:
            self.calls.append(cmd)
            answer = self.pgreps.pop(0) if len(self.pgreps) > 1 else self.pgreps[0]
            return mock.Mock(returncode=0 if answer else 1, stdout=answer)
        if cmd[:2] == ['ps', 'eww']:
            self.calls.append(cmd)
            pids = cmd[-1].split(',')
            return mock.Mock(returncode=0, stdout=''.join(
                f'{p} {self.envs[int(p)]}\n' for p in pids if int(p) in self.envs))
        return super().__call__(cmd, **kw)


class DrainIgnoresTestsTest(HomeCase):
    """A test suite's ``asf.cli tick --product sample`` under a temp ASF home is no factory
    process of this install: the drain never waits on it (the 2026-09-26 17:10 freeze)."""
    SHA = 'b' * 40

    def envs(self):
        test_home = os.path.join(self.tmp, 'asf_sample_x')
        return {
            # a test child of tools/run_tests.py: its own temp HOME and ASF_HOME
            701: (f'/usr/bin/python3 -m asf.cli tick --product sample --fresh '
                  f'HOME={test_home} ASF_HOME={test_home}/asf-home ASF_TESTS_HOME=/t'),
            # a test child that set only HOME: its ASF home is HOME/.ASF, not ours
            702: f'/usr/bin/python3 -m asf.cli tick --product sample HOME={test_home} USER=x',
            # the real harvest of this install
            703: (f'/usr/bin/python3 -m asf.tick.step_harvest --product asf '
                  f'HOME=/Users/x ASF_HOME={env.ASF_HOME}'),
        }

    def test_other_ticks_counts_only_this_installs_home(self):
        run = EnvRun(self.envs(), ['701\n702\n703\n'])
        self.assertEqual(upgrade.other_ticks(run, me=set()), [703])

    def test_a_process_whose_env_cannot_be_read_still_counts(self):
        run = EnvRun({}, ['704\n'])
        self.assertEqual(upgrade.other_ticks(run, me=set()), [704])

    def test_a_default_home_process_counts(self):
        # no ASF_HOME in its env: HOME/.ASF, which is ours
        home = os.path.dirname(self.tmp)
        with mock.patch.object(env, 'ASF_HOME', os.path.join(home, '.ASF')):
            run = EnvRun({705: f'/usr/bin/python3 -m asf.cli tick --product asf HOME={home}'},
                         ['705\n'])
            self.assertEqual(upgrade.other_ticks(run, me=set()), [705])

    def test_test_children_never_block_a_manual_wait(self):
        envs = self.envs()
        del envs[703]
        run = EnvRun(envs, ['701\n702\n'], installed=FakeRun.HEAD)
        slept = []
        rc, out, _err = _quiet(upgrade.cmd_upgrade, argparse.Namespace(
            skip_pipx=False, ref=None, owner=None, wait=540, sleep=slept.append), run=run)
        self.assertEqual(rc, 0, out)
        self.assertEqual(slept, [])
        self.assertEqual(len(run.installs()), 1)
        self.assertIsNone(upgrade.read_pending())

    def test_an_interrupted_manual_wait_lifts_its_mark(self):
        run = SequencedRun(['54171\n'], installed=FakeRun.HEAD)

        def sleep(_s):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            _quiet(upgrade.cmd_upgrade, argparse.Namespace(
                skip_pipx=False, ref=None, owner=None, wait=540, sleep=sleep), run=run)
        self.assertIsNone(upgrade.read_pending())  # never leaves the factory parked


class DeadWaiterTest(HomeCase):
    """A killed ``asf upgrade --wait`` never cleared its mark: the next tick drops it at once
    instead of parking every product until the TTL (the 17:25 kill)."""
    SHA = 'b' * 40

    def test_an_operator_mark_records_its_pid(self):
        self.assertEqual(upgrade.write_pending(self.SHA, None)['pid'], os.getpid())
        self.assertNotIn('pid', upgrade.write_pending(self.SHA, 'factory'))

    def test_a_dead_waiters_mark_no_longer_parks_the_ticks(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        upgrade.write_pending(self.SHA, None)
        data = upgrade.read_pending()
        data['pid'] = dead.pid
        upgrade._write_json(upgrade.pending_path(), data)
        lines = []
        self.assertFalse(upgrade.waiting('other', out=lines.append, installed='c' * 40))
        self.assertIsNone(upgrade.read_pending())
        self.assertIn('is gone', lines[0])

    def test_a_live_waiters_mark_still_parks_them(self):
        upgrade.write_pending(self.SHA, None)
        self.assertTrue(upgrade.waiting('other', out=lambda _l: None, installed='c' * 40))


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

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='install_sh_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin_dir = os.path.join(self.tmp, 'bin')
        self.home = os.path.join(self.tmp, 'home')
        self.asf_home = os.path.join(self.home, '.ASF')
        self.log = os.path.join(self.tmp, 'calls.log')
        self.status_calls = os.path.join(self.tmp, 'status_calls')
        self.doctor_calls = os.path.join(self.tmp, 'doctor_calls')
        os.makedirs(self.bin_dir)
        os.makedirs(os.path.join(self.asf_home, 'products'))
        for rel in ('config.yaml', os.path.join('products', 'demo.yaml')):
            open(os.path.join(self.asf_home, rel), 'w').close()

    def _write_scripts(self, hooks_rc, status_results=(('0', ''),), pipx_log=None,
                       doctor_results=(('0', ''),)):
        """``status_results``: ``[(rc, stdout), ...]`` for successive ``scheduler status`` calls
        (the last entry repeats for any call beyond the list) — how install.sh's own retry
        of the bootstrap sees the clock check."""
        pipx_body = '#!/bin/sh\n'
        if pipx_log:
            pipx_body += f'echo "$*" >> "{pipx_log}"\n'
        pipx_body += 'exit 0\n'

        cases = []
        for i, (rc, out) in enumerate(status_results, start=1):
            echo = f'printf %s\\\\n {shlex.quote(out)}; ' if out else ''
            cases.append(f'      {i}) {echo}exit {rc} ;;')
        last_rc, last_out = status_results[-1]
        echo = f'printf %s\\\\n {shlex.quote(last_out)}; ' if last_out else ''
        cases.append(f'      *) {echo}exit {last_rc} ;;')
        status_case = '\n'.join(cases)
        doctor_case = self._case(doctor_results)

        scripts = {
            'pipx': pipx_body,
            'asf': ('#!/bin/sh\n'
                    f'echo "$*" >> "{self.log}"\n'
                    'if [ "$1 $2" = "scheduler status" ]; then\n'
                    f'  n=0; [ -f "{self.status_calls}" ] && n=$(cat "{self.status_calls}")\n'
                    f'  n=$((n + 1)); echo "$n" > "{self.status_calls}"\n'
                    '  case "$n" in\n'
                    f'{status_case}\n'
                    '  esac\n'
                    'fi\n'
                    'if [ "$1" = "doctor" ]; then\n'
                    f'  n=0; [ -f "{self.doctor_calls}" ] && n=$(cat "{self.doctor_calls}")\n'
                    f'  n=$((n + 1)); echo "$n" > "{self.doctor_calls}"\n'
                    '  case "$n" in\n'
                    f'{doctor_case}\n'
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
            path = os.path.join(self.bin_dir, name)
            with open(path, 'w') as f:
                f.write(body)
            os.chmod(path, 0o755)

    @staticmethod
    def _case(results):
        """The ``case`` arms a stub answers its n-th call with: ``[(rc, stdout), ...]``, the last
        entry repeating for any call beyond the list."""
        arms = []
        for i, (rc, out) in enumerate(list(results) + [results[-1]], start=1):
            echo = f'printf %s\\\\n {shlex.quote(out)}; ' if out else ''
            arms.append(f'      {"*" if i > len(results) else i}) {echo}exit {rc} ;;')
        return '\n'.join(arms)

    def _env(self, **extra):
        return dict(os.environ, HOME=self.home, ASF_HOME=self.asf_home,
                    PATH=self.bin_dir + os.pathsep + os.environ.get('PATH', ''), **extra)

    def _run(self, hooks_rc=0, status_results=(('0', ''),), pipx_log=None, extra_env=None,
             doctor_results=(('0', ''),)):
        self._write_scripts(hooks_rc, status_results=status_results, pipx_log=pipx_log,
                            doctor_results=doctor_results)
        r = subprocess.run(['bash', INSTALL_SH, 'demo', 'deadbeef'], capture_output=True,
                           text=True, env=self._env(**(extra_env or {})), timeout=60)
        with open(self.log) as f:
            calls = [line.split()[0] for line in f if line.strip()]
        return r, calls

    def test_a_failed_hooks_step_still_runs_the_scheduler_and_the_doctor(self):
        r, calls = self._run(hooks_rc=2)
        self.assertEqual(calls, ['doctor', '--version', 'hooks', 'scheduler', 'scheduler', 'doctor',
                                 'console-permissions'], r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 3: asf hooks install --product demo (exit 2)', r.stderr)
        self.assertNotIn('FAILED step 4', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)

    def test_every_step_green_exits_zero(self):
        r, calls = self._run(hooks_rc=0)
        self.assertEqual(calls, ['doctor', '--version', 'hooks', 'scheduler', 'scheduler', 'doctor',
                                 'console-permissions'], r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)

    def test_an_unloaded_clock_is_retried_once_and_then_succeeds(self):
        """B-0136: the first read-back finds the clock not loaded; install.sh retries the
        bootstrap once and, once that clock is loaded, the step is not a failure."""
        r, calls = self._run(status_results=[('1', self.NOT_LOADED), ('0', '')])
        self.assertEqual(calls, ['doctor', '--version', 'hooks',
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
        self.assertEqual(calls, ['doctor', '--version', 'hooks', 'scheduler', 'scheduler',
                                 'scheduler', 'scheduler', 'doctor', 'console-permissions'],
                         r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 4: asf scheduler install --product demo (exit 1)',
                      r.stderr)
        self.assertIn('install: NEEDS OPERATOR: clock(s) still not loaded after retrying the '
                      'bootstrap: asf.demo.record-health-wave-prs-harvest', r.stderr)
        self.assertIn('/plugin install asf@asf', r.stdout)  # steps 5 and 6 still ran

    #: A doctor table with two REDs, as ``ASF_TABLES=md asf doctor`` prints it.
    DOCTOR_KNOWN = ('== DOCTOR demo\n'
                    'config               ok    config.yaml\n'
                    'one-factory          RED   ci-health.sh in product repo\n'
                    'console permissions  RED   missing Bash(launchctl bootout gui/*/asf.*)\n'
                    '\n== SCHEDULER demo\n'
                    'ok      asf.demo.tick  state=running')

    def test_a_doctor_red_that_was_red_before_the_install_is_a_warning(self):
        """Every install of a day ended red on the same three known checks, none of them the
        install's doing: a RED the doctor already showed before the install is named as
        pre-existing and does not fail it."""
        r, calls = self._run(doctor_results=[('1', self.DOCTOR_KNOWN)])
        self.assertEqual(calls[0], 'doctor')  # the baseline, read before the package moves
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('FAILED', r.stderr)
        self.assertIn('install: WARN doctor RED before this install too (not caused by it): '
                      'one-factory, console permissions', r.stderr)

    def test_a_doctor_red_the_install_caused_fails_it_naming_the_check(self):
        after = self.DOCTOR_KNOWN + '\nRED     asf.demo.batch  declared but not loaded'
        after = after.replace('config               ok ', 'config               RED')
        r, _calls = self._run(doctor_results=[('1', self.DOCTOR_KNOWN), ('1', after)])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 5: asf doctor --product demo (exit 1)', r.stderr)
        self.assertIn('install: doctor RED caused by this install: config, '
                      'scheduler:asf.demo.batch', r.stderr)
        self.assertIn('install: WARN doctor RED before this install too (not caused by it): '
                      'one-factory, console permissions', r.stderr)

    def test_no_asf_before_the_install_every_doctor_red_counts(self):
        # a first install has no baseline: a doctor RED is the install's to answer
        self._write_scripts(0, doctor_results=[('1', self.DOCTOR_KNOWN)])
        asf, pending = os.path.join(self.bin_dir, 'asf'), os.path.join(self.tmp, 'asf.pending')
        os.rename(asf, pending)
        with open(os.path.join(self.bin_dir, 'pipx'), 'w') as f:  # pipx puts asf on PATH
            f.write(f'#!/bin/sh\ncp "{pending}" "{asf}"\nexit 0\n')
        r = subprocess.run(['bash', INSTALL_SH, 'demo', 'deadbeef'], capture_output=True,
                           text=True, env=self._env(), timeout=60)
        with open(self.log) as f:
            calls = [line.split()[0] for line in f if line.strip()]
        self.assertEqual(calls.count('doctor'), 1, calls)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('install: FAILED step 5', r.stderr)
        self.assertIn('install: doctor RED caused by this install: one-factory, '
                      'console permissions', r.stderr)

    def test_step_6_offers_the_console_permissions_and_writes_nothing(self):
        r, calls = self._run(hooks_rc=0)
        self.assertIn('console-permissions: allow Bash(asf:*)', r.stdout)
        self.assertIn('run one to write it: asf console-permissions install --product demo '
                      '--scope user|repo', r.stdout)

    def test_install_waits_on_a_held_tick_lock(self):
        """B-0135: an install that races a running tick's ``pipx install --force`` tears it —
        half the modules old, half new. The lock the tick holds for the whole of its run
        (``asf.tick.tick.lock_path``) is the same one the install waits on before it touches
        the package, so the two can never overlap."""
        lock_path = os.path.join(self.asf_home, 'state', 'demo', 'tick.lock')
        os.makedirs(os.path.dirname(lock_path))
        pipx_log = os.path.join(self.tmp, 'pipx.log')
        self._write_scripts(hooks_rc=0, pipx_log=pipx_log)

        import fcntl
        held = open(lock_path, 'a')
        fcntl.flock(held, fcntl.LOCK_EX)
        released_at = []

        def release_after(delay):
            time.sleep(delay)
            released_at.append(time.monotonic())
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

        import threading
        t = threading.Thread(target=release_after, args=(1.0,))
        started = time.monotonic()
        t.start()
        try:
            r = subprocess.run(
                ['bash', INSTALL_SH, 'demo', 'deadbeef'], capture_output=True, text=True,
                env=self._env(ASF_INSTALL_LOCK_WAIT_S='30', ASF_INSTALL_LOCK_POLL_S='0.1'),
                timeout=60)
        finally:
            t.join()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(pipx_log), r.stderr)
        with open(pipx_log) as f:
            pipx_calls = f.read()
        self.assertIn('install', pipx_calls)
        self.assertGreaterEqual(released_at[0], started + 1.0)
        self.assertIn('install: a running tick holds the lock', r.stderr)

    def test_the_script_rewritten_during_the_lock_wait_still_runs_as_read(self):
        """install.sh waits up to ten minutes on a tick's lock, run from a checkout other
        sessions fast-forward. bash reads a script as it goes: a rewrite during the wait resumed
        it at a stale byte offset mid-line (``line 84: the: command not found``) and the steps
        after it failed, where the same commands by hand worked. The script is read whole
        before its first command runs."""
        script = os.path.join(self.tmp, 'install.sh')
        shutil.copy(INSTALL_SH, script)
        lock_path = os.path.join(self.asf_home, 'state', 'demo', 'tick.lock')
        os.makedirs(os.path.dirname(lock_path))
        self._write_scripts(hooks_rc=0)
        import fcntl
        held = open(lock_path, 'a')
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen(['bash', script, 'demo', 'deadbeef'], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env=self._env(ASF_INSTALL_LOCK_WAIT_S='30',
                                              ASF_INSTALL_LOCK_POLL_S='0.1'))
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                time.sleep(0.2)
                if os.path.exists(self.log):  # the baseline doctor ran: the wait is next
                    break
            time.sleep(0.5)
            with open(INSTALL_SH) as f:
                text = f.read()
            with open(script, 'w') as f:  # a checkout moving under the running install
                f.write('# ' + 'x' * 97 + '\n' + '# the checkout moved\n' * 40 + text)
            fcntl.flock(held, fcntl.LOCK_UN)
            out, err = proc.communicate(timeout=60)
        finally:
            held.close()
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn('command not found', err)
        self.assertIn('install: done', out)

    def test_a_stuck_tick_lock_times_out(self):
        """The wait is bounded (B-0135): a tick lock nobody ever releases must not hang the
        install forever — it gives up and asks the operator instead."""
        lock_path = os.path.join(self.asf_home, 'state', 'demo', 'tick.lock')
        os.makedirs(os.path.dirname(lock_path))
        pipx_log = os.path.join(self.tmp, 'pipx.log')
        self._write_scripts(hooks_rc=0, pipx_log=pipx_log)

        import fcntl
        held = open(lock_path, 'a')
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            r = subprocess.run(
                ['bash', INSTALL_SH, 'demo', 'deadbeef'], capture_output=True, text=True,
                env=self._env(ASF_INSTALL_LOCK_WAIT_S='0.5', ASF_INSTALL_LOCK_POLL_S='0.1'),
                timeout=60)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(pipx_log))
        self.assertIn('NEEDS OPERATOR', r.stderr)
        self.assertIn('tick', r.stderr)

    def test_the_lock_is_held_across_the_install_itself_not_just_before_it(self):
        """B-0135 C1: checking the lock and then releasing it before ``pipx install --force``
        leaves a tick that starts during the install free to race it. The lock must be held
        through the install, not just before it."""
        lock_path = os.path.join(self.asf_home, 'state', 'demo', 'tick.lock')
        os.makedirs(os.path.dirname(lock_path))
        pipx_log = os.path.join(self.tmp, 'pipx.log')
        pipx_body = f'#!/bin/sh\necho "$*" >> "{pipx_log}"\nsleep 1\nexit 0\n'
        with open(os.path.join(self.bin_dir, 'pipx'), 'w') as f:
            f.write(pipx_body)
        os.chmod(os.path.join(self.bin_dir, 'pipx'), 0o755)
        with open(os.path.join(self.bin_dir, 'asf'), 'w') as f:
            f.write('#!/bin/sh\necho "$*" >> "%s"\ncase "$1" in\n'
                    '  --version) echo "asf 0.0.0 (stub)";;\nesac\nexit 0\n' % self.log)
        os.chmod(os.path.join(self.bin_dir, 'asf'), 0o755)

        import fcntl
        proc = subprocess.Popen(['bash', INSTALL_SH, 'demo', 'deadbeef'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=self._env())
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(pipx_log) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(pipx_log), 'pipx was never invoked')
            probe = open(lock_path, 'a')
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                probe.close()
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        held = open(lock_path, 'a')
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(held, fcntl.LOCK_UN)
        finally:
            held.close()


def _install_args(**over):
    base = dict(product='demo', repo=None, repo_url=None, record=None, record_url=None,
               scheduler=None, account=None, fake_workers=False, console_permissions=None,
               allow_checkout=False, yes=True)
    base.update(over)
    return argparse.Namespace(**base)


class InstallCommandTests(HomeCase):
    """``asf install``'s own surface: every flag the parser carries, the required ``--product``,
    the checkout refusal, the missing-flag rule and the version line."""

    def _parser(self):
        top = argparse.ArgumentParser()
        sub = top.add_subparsers(dest='command')
        install.register(sub)
        return top

    def test_parser_carries_every_flag(self):
        top = self._parser()
        args = top.parse_args([
            'install', '--product', 'demo', '--repo', '/r', '--repo-url', 'https://x/r.git',
            '--record', '/b', '--record-url', 'https://x/b.git', '--scheduler', 'cron',
            '--account', 'a:~/x', '--account', 'b', '--fake-workers',
            '--console-permissions', 'repo', '--allow-checkout', '--yes',
        ])
        self.assertEqual(args.product, 'demo')
        self.assertEqual(args.repo, '/r')
        self.assertEqual(args.repo_url, 'https://x/r.git')
        self.assertEqual(args.record, '/b')
        self.assertEqual(args.record_url, 'https://x/b.git')
        self.assertEqual(args.scheduler, 'cron')
        self.assertEqual(args.account, ['a:~/x', 'b'])
        self.assertTrue(args.fake_workers)
        self.assertEqual(args.console_permissions, 'repo')
        self.assertTrue(args.allow_checkout)
        self.assertTrue(args.yes)
        self.assertIs(args.run, install.cmd_install)

    def test_scheduler_and_console_permissions_reject_an_unknown_value(self):
        top = self._parser()
        with self.assertRaises(SystemExit):
            top.parse_args(['install', '--product', 'demo', '--scheduler', 'systemd'])
        with self.assertRaises(SystemExit):
            top.parse_args(['install', '--product', 'demo', '--console-permissions', 'global'])

    def test_product_is_required(self):
        top = self._parser()
        with self.assertRaises(SystemExit):
            top.parse_args(['install'])

    def test_checkout_install_refuses_without_allow_checkout_and_runs_with_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli, '_checkout_root', return_value=tmp):
                rc, detail = install._step_asf(_install_args(allow_checkout=False))
                self.assertEqual(rc, 1)
                self.assertIn('checkout', detail)
                rc, detail = install._step_asf(_install_args(allow_checkout=True))
                self.assertEqual(rc, 0)

    def test_version_line_names_what_version_string_returns(self):
        with mock.patch.object(cli, '_checkout_root', return_value=None), \
                mock.patch.object(cli, 'version_string', return_value='v9.9.9 (deadbee)'):
            rc, detail = install._step_asf(_install_args())
        self.assertEqual((rc, detail), (0, 'v9.9.9 (deadbee)'))

    def test_tty_prompt_asks_once_per_missing_flag_and_takes_the_default_on_empty_answer(self):
        # a default for --record too, so an empty answer at each of the three prompts is taken
        self.write(env.product_path('demo'), 'product: demo\nbacklog_dir: /existing/backlog\n')
        args = _install_args(repo=None, record=None, scheduler=None, yes=False)
        with mock.patch('sys.stdin.isatty', return_value=True), \
                mock.patch('builtins.input', side_effect=['', '', '']) as fake_input:
            ok = install._resolve_missing(args)
        self.assertTrue(ok)
        self.assertEqual(fake_input.call_count, 3)
        self.assertEqual(args.repo, os.getcwd())
        self.assertEqual(args.scheduler, 'launchd')
        self.assertEqual(args.record, '/existing/backlog')

    def test_yes_off_tty_with_no_default_is_needs_operator_and_writes_nothing(self):
        args = _install_args(repo=None, record=None, scheduler=None, yes=True)
        result, out, err = _quiet(install.cmd_install, args)
        self.assertEqual(result, 2)
        self.assertIn('NEEDS OPERATOR', err)
        self.assertIn('--record', err)
        self.assertFalse(os.path.exists(env.config_path()))
        self.assertEqual(os.listdir(os.path.join(env.ASF_HOME, 'products')), [])


class InstallConfigTemplateTests(HomeCase):
    """The operator config: written only when absent, from :data:`install.CONFIG_TEMPLATE`, and
    every key it writes is a key ``docs/config.example.yaml`` documents active — read from that
    file, never a hand-kept list (D4)."""

    def _plan(self, fake=False):
        args = _install_args(scheduler='launchd', fake_workers=fake)
        return args, install._account_plan(args)

    def test_written_when_absent(self):
        args, plan = self._plan()
        rc, detail = install._step_config(args, plan)
        self.assertEqual(rc, 0)
        self.assertTrue(detail.startswith('wrote '))
        self.assertTrue(os.path.isfile(env.config_path()))

    def test_not_rewritten_when_present_and_the_diff_not_made_is_printed(self):
        args, plan = self._plan()
        self.write(env.config_path(), 'default_product: something-else\n')
        result, out, err = _quiet(install._step_config, args, plan)
        self.assertEqual(result, (0, 'already in place'))
        with open(env.config_path(), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'default_product: something-else\n')  # never rewritten
        self.assertIn('left as is', out)
        self.assertIn('default_product', out)

    def test_every_key_the_template_writes_is_documented_in_the_example(self):
        with open(os.path.join(REPO, 'docs', 'config.example.yaml'), encoding='utf-8') as f:
            example_keys = _keys(env.loads(f.read()))
        for fake in (False, True):
            args, plan = self._plan(fake=fake)
            rendered = install._render_config(args, plan)
            rendered_keys = _keys(env.loads(rendered))
            self.assertLessEqual(rendered_keys, example_keys, rendered_keys - example_keys)

    def test_env_loads_and_load_config_accept_what_it_writes(self):
        args, plan = self._plan()
        rc, detail = install._step_config(args, plan)
        self.assertEqual(rc, 0)
        cfg = env.load_config()
        self.assertEqual(cfg['default_product'], 'demo')
        self.assertEqual(cfg['scheduler']['kind'], 'launchd')
        self.assertEqual(cfg['capacity']['total']['sessions'], install.DEFAULT_SESSIONS)


class InstallAccountsTests(HomeCase):
    """``--account`` (repeatable), detection under ``<ASF_HOME>/accounts/``, and the one-stanza
    fallback — never a credential read (D10)."""

    def test_one_account_flag(self):
        args = _install_args(account=['acct-a:~/x'])
        plan = install._account_plan(args)
        self.assertEqual(plan['names'], ['acct-a'])
        self.assertIn(f"config_dir: {os.path.expanduser('~/x')}", plan['stanzas'][0])
        self.assertIsNone(plan['operator_line'])

    def test_account_flag_with_no_dir_defaults_under_asf_home(self):
        args = _install_args(account=['acct-a'])
        plan = install._account_plan(args)
        self.assertIn(f"config_dir: {os.path.join(env.ASF_HOME, 'accounts', 'acct-a')}",
                     plan['stanzas'][0])

    def test_two_account_flags(self):
        args = _install_args(account=['a:~/a', 'b:~/b'])
        plan = install._account_plan(args)
        self.assertEqual(plan['names'], ['a', 'b'])
        self.assertEqual(len(plan['stanzas']), 2)

    def test_detection_finds_only_the_credentialled_directory(self):
        accounts = os.path.join(env.ASF_HOME, 'accounts')
        credentialled, bare = os.path.join(accounts, 'has-cred'), os.path.join(accounts, 'bare')
        os.makedirs(credentialled)
        os.makedirs(bare)
        self.write(os.path.join(credentialled, install.RUNTIME_CREDENTIAL_FILE), '{}')
        self.assertEqual(install.detect_accounts(env.ASF_HOME), [('has-cred', credentialled)])
        args = _install_args()
        plan = install._account_plan(args)
        self.assertEqual(plan['names'], ['has-cred'])
        self.assertIsNone(plan['operator_line'])

    def test_neither_flag_nor_detection_writes_one_stanza_and_needs_operator_but_returns_0(self):
        args = _install_args()
        plan = install._account_plan(args)
        self.assertEqual(plan['names'], ['acct-a'])
        self.assertIsNotNone(plan['operator_line'])
        result, out, err = _quiet(install._step_account, args, plan)
        self.assertEqual(result[0], 0)
        self.assertIn('NEEDS OPERATOR', err)
        self.assertIn('claude setup-token', err)

    def test_fake_workers_writes_backend_fake_and_no_stanza(self):
        args = _install_args(fake_workers=True)
        plan = install._account_plan(args)
        self.assertEqual(plan['backend'], 'fake')
        self.assertEqual(plan['stanzas'], [])
        self.assertIsNone(plan['operator_line'])
        result, out, err = _quiet(install._step_account, args, plan)
        self.assertEqual(result, (0, 'backend: fake, no account'))
        self.assertEqual(err, '')

    def test_no_credential_file_is_ever_opened(self):
        config_dir = os.path.join(env.ASF_HOME, 'accounts', 'acct-a')
        os.makedirs(config_dir)
        self.write(os.path.join(config_dir, install.RUNTIME_CREDENTIAL_FILE), '{"secret": "x"}')
        real_open = open

        def spy(path, *a, **kw):
            self.assertNotEqual(os.path.basename(str(path)), install.RUNTIME_CREDENTIAL_FILE)
            return real_open(path, *a, **kw)

        with mock.patch('builtins.open', side_effect=spy):
            found = install.detect_accounts(env.ASF_HOME)
        self.assertEqual(found, [('acct-a', config_dir)])


class InstallStepsTests(HomeCase):
    """The step framework (abort stops, record-and-continue does not, the summary names each
    failure, a non-zero exit), and steps 1-6: the host, this ``asf``, the config, the repos, the
    adopt, the account."""

    # ---- the framework -------------------------------------------------------------

    def test_an_abort_step_that_fails_stops_the_run(self):
        calls = []
        steps = [
            install.Step('a', lambda: (1, 'bad'), True),
            install.Step('b', lambda: (calls.append('b'), (0, 'ok'))[1], True),
        ]
        rc = install.run_steps(steps, out=lambda *a: None)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [])

    def test_a_non_abort_step_that_fails_records_and_continues(self):
        calls = []
        steps = [
            install.Step('a', lambda: (1, 'bad'), False),
            install.Step('b', lambda: (calls.append('b'), (0, 'ok'))[1], False),
        ]
        rc = install.run_steps(steps, out=lambda *a: None)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ['b'])

    def test_the_summary_names_each_failure_and_the_exit_status_is_non_zero(self):
        lines = []
        steps = [
            install.Step('step 1: ok one', lambda: (0, 'ok'), True),
            install.Step('step 2: broke', lambda: (3, 'broke'), False),
        ]
        rc = install.run_steps(steps, out=lines.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any('step 2: broke' in l and 'exit 3' in l for l in lines))

    def test_every_step_passing_exits_0(self):
        steps = [install.Step('a', lambda: (0, 'ok'), True), install.Step('b', lambda: (0, 'ok'), False)]
        self.assertEqual(install.run_steps(steps, out=lambda *a: None), 0)

    # ---- step 1: the host -----------------------------------------------------------

    def test_step_1_missing_git_is_needs_operator_and_fails(self):
        with mock.patch.object(install.shutil, 'which', return_value=None):
            result, out, err = _quiet(install._step_host, _install_args())
        self.assertEqual(result[0], 1)
        self.assertIn('NEEDS OPERATOR', err)

    def test_step_1_ok_when_both_tools_are_on_path(self):
        with mock.patch.object(install.shutil, 'which', return_value='/usr/bin/x'):
            rc, detail = install._step_host(_install_args())
        self.assertEqual((rc, detail), (0, 'ok'))

    def test_step_1_gh_is_optional_for_a_product_with_no_pr_host(self):
        self.write(env.product_path('demo'), 'product: demo\nci: none\n')

        def which(name):
            return '/usr/bin/git' if name == 'git' else None

        with mock.patch.object(install.shutil, 'which', side_effect=which):
            rc, detail = install._step_host(_install_args())
        self.assertEqual((rc, detail), (0, 'ok'))

    # ---- step 2: this asf -------------------------------------------------------------

    def test_step_2_ok_off_a_checkout(self):
        with mock.patch.object(cli, '_checkout_root', return_value=None):
            rc, detail = install._step_asf(_install_args())
        self.assertEqual(rc, 0)

    # ---- step 4: the repos -------------------------------------------------------------

    def test_step_4_clones_only_into_a_directory_that_does_not_exist(self):
        record = os.path.join(self.tmp, 'record')
        origin = os.path.join(self.tmp, 'record-origin.git')
        _git(['init', '-q', '--bare', origin])
        rc, detail = install._step_repos(_install_args(record=record, record_url=origin))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(os.path.join(record, '.git')))
        marker = os.path.join(record, 'marker')
        open(marker, 'w').close()
        rc, detail = install._step_repos(_install_args(record=record, record_url=origin))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(marker), 'an existing directory is read, never written to')

    def test_step_4_is_a_no_op_with_no_url(self):
        rc, detail = install._step_repos(_install_args())
        self.assertEqual(rc, 0)

    # ---- step 5: the adopt ------------------------------------------------------------

    def test_adopt_step_leaves_an_existing_product_file_and_prints_the_diff(self):
        path = env.product_path('demo')
        self.write(path, '# a hand-edited product file\nproduct: demo\n')
        record = os.path.join(self.tmp, 'record')
        result, out, err = _quiet(install._step_adopt, _install_args(record=record))
        self.assertEqual(result, (0, 'ok'))
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), '# a hand-edited product file\nproduct: demo\n')
        self.assertIn('exists', out)

    def test_adopt_step_aborts_when_no_backlog_dir_and_no_record(self):
        result, out, err = _quiet(install._step_adopt, _install_args())
        self.assertEqual(result[0], 2)
        self.assertIn('NEEDS OPERATOR', err)

    # ---- step 6: the account -----------------------------------------------------------

    def test_step_6_never_fails_even_with_no_account_found(self):
        args = _install_args()
        plan = install._account_plan(args)
        rc, detail = install._step_account(args, plan)
        self.assertEqual(rc, 0)

    # ---- the whole pipeline: order and abort ------------------------------------------

    def test_cmd_install_runs_steps_in_order_and_an_abort_stops_the_rest(self):
        args = _install_args(repo='/r', record='/b', scheduler='launchd')
        calls = []

        def make(label, result):
            def fn(*a):
                calls.append(label)
                return result
            return fn

        with mock.patch.object(install, '_step_host', make('host', (0, 'ok'))), \
                mock.patch.object(install, '_step_asf', make('asf', (0, 'v1'))), \
                mock.patch.object(install, '_step_config', make('config', (1, 'nope'))), \
                mock.patch.object(install, '_step_repos', make('repos', (0, 'ok'))), \
                mock.patch.object(install, '_step_adopt', make('adopt', (0, 'ok'))), \
                mock.patch.object(install, '_step_account', make('account', (0, 'ok'))), \
                mock.patch.object(install, '_probe_plugin_command', return_value=None):
            rc, out, err = _quiet(install.cmd_install, args)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ['host', 'asf', 'config'])

    def test_baseline_is_read_before_any_step_runs(self):
        calls = []
        args = _install_args(repo='/r', record='/b', scheduler='none', fake_workers=True)

        def fake_baseline(product_name):
            calls.append('baseline')
            return set()

        def fake_host(*a):
            calls.append('step 1')
            return 1, 'stop here'

        with mock.patch.object(install, '_doctor_red_keys', side_effect=fake_baseline), \
                mock.patch.object(install, '_step_host', side_effect=fake_host), \
                mock.patch.object(install, '_probe_plugin_command', return_value=None):
            _quiet(install.cmd_install, args)
        self.assertEqual(calls, ['baseline', 'step 1'])

    # ---- step 7: the hooks -------------------------------------------------------------

    def test_step_7_calls_hooks_install_in_process(self):
        args = _install_args()
        with mock.patch.object(install.env, 'load_product', return_value='THE-PRODUCT'), \
                mock.patch.object(install.hooks, 'install',
                                  return_value=(0, 'hooks: ok')) as inst:
            result, out, err = _quiet(install._step_hooks, args)
        self.assertEqual(result, (0, 'ok'))
        inst.assert_called_once_with('THE-PRODUCT')
        self.assertIn('hooks: ok', out)

    def test_step_7_a_refusal_is_recorded_not_raised(self):
        args = _install_args()
        with mock.patch.object(install.env, 'load_product', return_value='THE-PRODUCT'), \
                mock.patch.object(install.hooks, 'install',
                                  return_value=(2, 'NEEDS OPERATOR: no repo_dir')):
            result, out, err = _quiet(install._step_hooks, args)
        self.assertEqual(result[0], 2)
        self.assertIn('NEEDS OPERATOR', err)

    # ---- step 8: the clocks, with their read-back retry ----------------------------------

    def test_step_8_scheduler_none_installs_and_reads_back_nothing(self):
        args = _install_args(scheduler='none')
        with mock.patch.object(install.scheduler, 'cmd_scheduler') as cmd:
            result = install._step_scheduler(args)
        self.assertEqual(result, (0, 'already in place'))
        cmd.assert_not_called()

    def test_step_8_installs_and_reads_back_a_loaded_clock(self):
        args = _install_args(scheduler='launchd')
        with mock.patch.object(install.scheduler, 'cmd_scheduler', return_value=0) as cmd, \
                mock.patch.object(install.env, 'load_config',
                                  return_value={'scheduler': {'kind': 'launchd'}}), \
                mock.patch.object(install, '_clock_labels', return_value=['asf.demo.tick']), \
                mock.patch.object(install.scheduler, 'status', return_value={'loaded': True}):
            result = install._step_scheduler(args)
        self.assertEqual(result, (0, 'ok'))
        cmd.assert_called_once()

    def test_step_8_an_unloaded_clock_is_retried_once_and_then_succeeds(self):
        """B-0136's own shape: the first read-back finds the clock not loaded; one retried
        bootstrap, and the clock loaded after it is not a failure."""
        args = _install_args(scheduler='launchd')
        with mock.patch.object(install.scheduler, 'cmd_scheduler', return_value=0) as cmd, \
                mock.patch.object(install.env, 'load_config',
                                  return_value={'scheduler': {'kind': 'launchd'}}), \
                mock.patch.object(install, '_clock_labels', return_value=['asf.demo.tick']), \
                mock.patch.object(install.scheduler, 'status',
                                  side_effect=[{'loaded': False}, {'loaded': True}]):
            result, out, err = _quiet(install._step_scheduler, args)
        self.assertEqual(result, (0, 'repaired (clock retried)'))
        self.assertEqual(cmd.call_count, 2)
        self.assertIn('retrying the bootstrap once', err)

    def test_step_8_still_missing_after_retry_fails_loudly_naming_it(self):
        args = _install_args(scheduler='launchd')
        with mock.patch.object(install.scheduler, 'cmd_scheduler', return_value=0), \
                mock.patch.object(install.env, 'load_config',
                                  return_value={'scheduler': {'kind': 'launchd'}}), \
                mock.patch.object(install, '_clock_labels', return_value=['asf.demo.tick']), \
                mock.patch.object(install.scheduler, 'status', return_value={'loaded': False}):
            result, out, err = _quiet(install._step_scheduler, args)
        self.assertEqual(result[0], 1)
        self.assertIn('asf.demo.tick', result[1])
        self.assertIn('NEEDS OPERATOR: clock(s) still not loaded after retrying the bootstrap: '
                     'asf.demo.tick', err)

    def test_step_8_install_failure_is_not_masked_by_the_read_back(self):
        args = _install_args(scheduler='launchd')
        with mock.patch.object(install.scheduler, 'cmd_scheduler', return_value=1):
            result = install._step_scheduler(args)
        self.assertEqual(result, (1, 'asf scheduler install failed'))

    # ---- step 9: the plugin -----------------------------------------------------------

    def test_step_9_calls_plugin_install(self):
        with mock.patch.object(install.plugin_build, 'install', return_value=0) as inst:
            result = install._step_plugin(_install_args())
        self.assertEqual(result, (0, 'ok'))
        inst.assert_called_once_with(out=print)

    # ---- step 10: the console permissions offer ----------------------------------------

    def test_step_10_offers_when_console_permissions_not_given(self):
        args = _install_args(console_permissions=None)
        with mock.patch.object(install.console_perms, 'cmd_console_permissions',
                              return_value=0) as cmd:
            result = install._step_console_permissions(args)
        self.assertEqual(result, (0, 'offered'))
        ns = cmd.call_args.args[0]
        self.assertEqual(ns.console_permissions_command, 'offer')

    def test_step_10_installs_at_the_given_scope(self):
        args = _install_args(console_permissions='repo')
        with mock.patch.object(install.console_perms, 'cmd_console_permissions',
                              return_value=0) as cmd:
            result = install._step_console_permissions(args)
        self.assertEqual(result, (0, 'wrote'))
        ns = cmd.call_args.args[0]
        self.assertEqual((ns.console_permissions_command, ns.scope), ('install', 'repo'))

    # ---- step 11 and 12, and the tail --------------------------------------------------

    def test_doctor_baseline_before_anything_is_configured_is_the_one_config_row(self):
        self.assertEqual(install._doctor_red_keys('demo'), {'config'})

    def test_doctor_and_dry_run_tail(self):
        args = _install_args()

        # a pre-existing RED warns and does not fail the step
        with mock.patch.object(install.doctor, 'cmd_doctor', return_value=1), \
                mock.patch.object(install, '_doctor_red_keys', return_value={'one-factory'}):
            result, out, err = _quiet(install._step_doctor, args, {'one-factory'})
        self.assertEqual(result, (0, 'pre-existing RED only: one-factory'))
        self.assertIn('WARN doctor RED before this install too (not caused by it): one-factory',
                      err)

        # a new RED fails the step and is named, distinctly from the pre-existing one
        with mock.patch.object(install.doctor, 'cmd_doctor', return_value=1), \
                mock.patch.object(install, '_doctor_red_keys',
                                  return_value={'one-factory', 'drift'}):
            result, out, err = _quiet(install._step_doctor, args, {'one-factory'})
        self.assertEqual(result, (1, 'RED caused by this install: drift'))
        self.assertIn('doctor RED caused by this install: drift', err)
        self.assertIn('WARN doctor RED before this install too (not caused by it): one-factory',
                      err)

        # a green doctor passes without even reading the baseline
        with mock.patch.object(install.doctor, 'cmd_doctor', return_value=0), \
                mock.patch.object(install, '_doctor_red_keys') as red_keys:
            result = install._step_doctor(args, set())
        self.assertEqual(result, (0, 'ok'))
        red_keys.assert_not_called()

        # a non-zero doctor with no RED row read at all is a failure: the doctor itself broke
        with mock.patch.object(install.doctor, 'cmd_doctor', return_value=2), \
                mock.patch.object(install, '_doctor_red_keys', return_value=set()):
            result = install._step_doctor(args, set())
        self.assertEqual(result, (2, 'doctor exited non-zero with no RED row read'))

        # the dry run is called with --dry-run's own semantics, and its exit status is the step's
        with mock.patch.object(install.dry_run, 'run', return_value=0) as run, \
                mock.patch.object(install.env, 'load_product', return_value='THE-PRODUCT'):
            result = install._step_dry_run(args)
        self.assertEqual(result, (0, 'ok'))
        run.assert_called_once_with('THE-PRODUCT', out=print)

        with mock.patch.object(install.dry_run, 'run', return_value=1), \
                mock.patch.object(install.env, 'load_product', return_value='THE-PRODUCT'):
            result = install._step_dry_run(args)
        self.assertEqual(result, (1, 'dry run failed'))

        # the two /plugin lines carry the written path
        with mock.patch.object(install, '_probe_plugin_command', return_value=None):
            lines = install._tail_lines('/the/asf/home/plugin')
        self.assertIn('  /plugin marketplace add /the/asf/home/plugin', lines)
        self.assertIn('  /plugin install asf@asf', lines)

        # the probe, when it finds a supported command, runs it and says so
        with mock.patch.object(install, '_probe_plugin_command', return_value='/usr/bin/claude'), \
                mock.patch.object(install.subprocess, 'run') as run:
            lines = install._tail_lines('/the/asf/home/plugin')
        self.assertEqual(run.call_count, 2)
        self.assertTrue(any('/usr/bin/claude' in line for line in lines), lines)

        # the probe itself: an absent claude never shells out
        with mock.patch.object(install.shutil, 'which', return_value=None):
            self.assertIsNone(install._probe_plugin_command())

    def test_idempotent_second_run(self):
        """The whole command, run twice over one temp HOME/ASF_HOME against ``sample/``'s repo
        and record, published to two bare origins (``tests/test_sample_product.py``'s own
        shape, run through ``asf install`` instead of ``asf init``): both runs exit 0 with every
        step ``ok``/``already in place``, and the second changes no byte on disk."""
        sample = os.path.join(self.tmp, 'sample')
        shutil.copytree(os.path.join(REPO, 'sample'), sample)
        repo = os.path.join(sample, 'repo')
        backlog = os.path.join(sample, 'backlog')
        _publish(repo, os.path.join(self.tmp, 'repo.git'))
        _publish(backlog, os.path.join(self.tmp, 'backlog.git'))

        with open(os.path.join(sample, 'product.yaml'), encoding='utf-8') as f:
            product_yaml = f.read().replace('@REPO@', repo).replace('@BACKLOG@', backlog)
        self.write(env.product_path('sample'), product_yaml)
        with open(os.path.join(sample, 'config.yaml'), encoding='utf-8') as f:
            config_yaml = f.read().replace('@SAMPLE@', sample)
        self.write(env.config_path(), config_yaml)

        def _snapshot():
            digests = {}
            for root in (env.ASF_HOME, repo, backlog):
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d != '.git']
                    for name in filenames:
                        path = os.path.join(dirpath, name)
                        with open(path, 'rb') as fh:
                            digests[path] = hashlib.sha256(fh.read()).hexdigest()
            return digests

        args = _install_args(repo=repo, record=backlog, scheduler='none', fake_workers=True,
                             allow_checkout=True, console_permissions=None)

        with mock.patch.object(install, '_probe_plugin_command', return_value=None):
            rc1, out1, err1 = _quiet(install.cmd_install, args)
            self.assertEqual(rc1, 0, out1 + err1)
            self.assertNotIn('FAILED', out1 + err1)

            snapshot_after_first = _snapshot()

            rc2, out2, err2 = _quiet(install.cmd_install, args)
        self.assertEqual(rc2, 0, out2 + err2)
        self.assertNotIn('FAILED', out2 + err2)
        self.assertIn('every step passed', out2)
        self.assertEqual(_snapshot(), snapshot_after_first,
                         'a second run must change no byte on disk')


class PackageDataTests(unittest.TestCase):
    """PD11 — a wheel built from this tree, installed into a temporary prefix, carries every
    procedure skill and the plugin manifest (`pyproject.toml`'s `asf.procedures` package-data
    line, F-0062 Task 3). The build never touches the network (`--no-build-isolation`): it uses
    whichever `setuptools` this interpreter already has, and skips rather than fails when there
    is none — a missing build tool is a gap in this environment, not in the package."""

    @unittest.skipUnless(importlib.util.find_spec('setuptools'),
                         'setuptools is not installed in this interpreter')
    def test_a_built_wheel_installed_into_a_temp_prefix_carries_every_skill_and_the_manifest(self):
        from asf import procedures
        tmp = tempfile.mkdtemp(prefix='asf-wheel-')
        self.addCleanup(shutil.rmtree, tmp, True)
        built = subprocess.run([sys.executable, '-m', 'pip', 'wheel', REPO, '--no-deps',
                                '--no-build-isolation', '-w', tmp],
                               capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        wheels = glob.glob(os.path.join(tmp, '*.whl'))
        self.assertEqual(len(wheels), 1, wheels)
        prefix = os.path.join(tmp, 'install')
        installed = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps',
                                    '--target', prefix, wheels[0]],
                                   capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        skills_dir = os.path.join(prefix, 'asf', 'procedures', 'skills')
        for name in procedures.skill_names():
            with self.subTest(skill=name):
                self.assertTrue(os.path.isfile(os.path.join(skills_dir, name, 'SKILL.md')))
        self.assertTrue(os.path.isfile(
            os.path.join(prefix, 'asf', 'procedures', '.claude-plugin', 'plugin.json')))


if __name__ == '__main__':
    unittest.main()
