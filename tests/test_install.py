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
                         (0, f'hooks: 0 rule hooks in {self.settings}; approvals in 0 worker accounts'))
        self.assertFalse(os.path.exists(self.settings))

    def test_merge_is_idempotent_and_keeps_unrelated_keys(self):
        self.write(self.settings, json.dumps({
            'permissions': {'allow': ['Bash(ls)']},
            'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'other'}]}]},
        }))
        rc, msg = hooks.install(self.product, rules_dir=self.rules, which=self.which)
        self.assertEqual(rc, 0, msg)
        self.assertEqual(msg, f'hooks: 2 rule hooks in {self.settings}; approvals in 0 worker accounts')
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


class NoCheckoutPathsTest(unittest.TestCase):
    """The install modules name no tools directory under the operator's home and no checkout."""

    def test_no_tools_dir_or_checkout_path(self):
        for mod in ('init', 'schema', 'upgrade', 'hooks'):
            with open(os.path.join(REPO, 'asf', f'{mod}.py'), encoding='utf-8') as f:
                text = f.read()
            self.assertNotRegex(text, r"\.ASF/tools|ASF_HOME, 'tools'|~/Code/", mod)


if __name__ == '__main__':
    unittest.main()
