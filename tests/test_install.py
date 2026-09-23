"""Install and upgrade: the package metadata, ``asf init``, the schema check and
``asf schema-migrate``, ``asf upgrade``, ``asf hooks install`` / ``asf hook``."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

import asf
from asf import conventions, env, hooks, init, schema, upgrade

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
        self.assertEqual(out.getvalue().strip(), f'ASF — Autonomous Software Factory {asf.__version__}')

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
                         (['hook', 'r0001'], hooks.cmd_hook)):
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

    def test_inflight_session_refuses(self):
        self.sessions([{'id': 'a', 'started': 't0'}, {'id': 'b', 'started': 't0'},
                       {'id': 'b', 'ended': 't1'}])
        rc, msg = schema.migrate_product(self.product)
        self.assertEqual(rc, 1)
        self.assertIn('1 session(s) in flight (a)', msg)
        self.assertIn('--drain', msg)
        self.assertFalse(os.path.isdir(os.path.join(env.ASF_HOME, 'state', 'sample', 'record')))

    def test_drain_waits_for_the_session_to_end(self):
        self.sessions([{'id': 'a', 'started': 't0'}])
        polls = []

        def sleep(s):
            polls.append(s)
            self.sessions([{'id': 'a', 'started': 't0'}, {'id': 'a', 'ended': 't1'}])

        with mock.patch.object(schema, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(schema.MIGRATIONS, {2: lambda d: None}):
            rc, msg = schema.migrate_product(self.product, drain=True, sleep=sleep)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(polls, [schema.DRAIN_POLL_S])
        self.assertEqual(self.origin_log()[0], 'migrate: schema 1 → 2')

    def test_drain_gives_up_at_the_timeout(self):
        self.sessions([{'started': 't0'}])
        t = [0]

        def sleep(s):
            t[0] += s

        rc, msg = schema.migrate_product(self.product, drain=True, drain_timeout=60, sleep=sleep,
                                         clock=lambda: t[0])
        self.assertEqual(rc, 1)
        self.assertIn('gave up after 60s', msg)

    def test_cmd_updates_the_config_stamp(self):
        self.write(env.config_path(), 'default_product: sample\nschema_version: 0\n')
        args = argparse.Namespace(product='sample', all=False, drain=False, drain_timeout=10)
        rc, out, _err = _quiet(schema.cmd_schema_migrate, args)
        self.assertEqual(rc, 0)
        self.assertEqual(schema.config_version(), 1)
        self.assertIn('config.yaml schema_version → 1', out)


# ---- 4. asf upgrade, asf hooks install, asf hook ----------------------------

class UpgradeTest(HomeCase):
    def test_the_command(self):
        self.assertEqual(upgrade.upgrade_command(), ['pipx', 'upgrade', 'asf-factory'])

    def test_runs_pipx_then_prints_the_table(self):
        good, old = os.path.join(self.tmp, 'good'), os.path.join(self.tmp, 'old')
        self.write(os.path.join(good, 'index.json'), '{"items": {}, "schema_version": 1}')
        self.write(os.path.join(old, 'index.json'), '{"items": {}}')
        self.write(env.product_path('alpha'), f'backlog_dir: {good}\n')
        self.write(env.product_path('beta'), f'backlog_dir: {old}\n')
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        rc, out, _err = _quiet(upgrade.cmd_upgrade, argparse.Namespace(skip_pipx=False), run=run)
        self.assertEqual(rc, 0)
        run.assert_called_once_with(['pipx', 'upgrade', 'asf-factory'])
        self.assertIn('| product | record schema | package | action |', out)
        self.assertIn('| alpha | 1 | 1 | none |', out)
        self.assertIn('| beta | 0 | 1 | asf schema-migrate --product beta |', out)

    def test_a_failed_pipx_stops(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1))
        rc, out, _err = _quiet(upgrade.cmd_upgrade, argparse.Namespace(skip_pipx=False), run=run)
        self.assertEqual(rc, 1)
        self.assertNotIn('| product |', out)


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
        self.which = lambda name: '/opt/bin/asf'
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
        self.assertEqual(pre[1]['hooks'][0]['command'], '/opt/bin/asf hook r0001 --product sample')
        self.assertEqual(first['hooks']['Stop'][0]['hooks'][0]['command'],
                         '/opt/bin/asf hook r0001 --product sample')
        with open(self.settings, 'rb') as f:
            before = f.read()
        rc, msg = hooks.install(self.product, rules_dir=self.rules, which=self.which)
        self.assertEqual(rc, 0, msg)
        with open(self.settings, 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_a_moved_asf_replaces_its_own_entry(self):
        hooks.install(self.product, rules_dir=self.rules, which=self.which)
        hooks.install(self.product, rules_dir=self.rules, which=lambda n: '/new/bin/asf')
        stop = self.read()['hooks']['Stop']
        self.assertEqual(len(stop), 1)
        self.assertEqual(stop[0]['hooks'], [{'type': 'command', 'command': '/new/bin/asf hook r0001 --product sample'}])

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


if __name__ == '__main__':
    unittest.main()
