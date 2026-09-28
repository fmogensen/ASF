"""F-0062 Task 1 — the launch table, the capability probe and the agents file.

Three modules, three classes: :class:`LaunchTableTests` over `asf.roles.launch` (the table
covers the roles and the roles cover the table, T1), :class:`CapabilityTests` over
`asf.workers.capability` (what the installed binary accepts, T2), :class:`AgentFileTests` over
`write_agents_file` (T4). Nothing here launches a real session or invokes a real runtime binary —
`CapabilityTests`' own fixture is a small executable script on a tmpdir `PATH`.
"""
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from asf import briefs as briefs_mod
from asf import env
from asf.roles import launch as launch_mod
from asf.roles import roles
from asf.workers import capability

VALID_ROLE = """---
name: asf-demo
purpose: keep a small thing true
---

## Identity

You are the demo role, and you exist so a test has something to mutate.

## Doctrine

- The first rule cites the first incident (B-0051).
- The second rule cites another (B-0062).
- The third rule cites a prior-art finding (b109).
- The fourth rule cites a decision (D-0025).

## Output

The named side file, and only it.

## Economy

Read what the brief names. Do not survey.

## Boundaries

You write the one file and nothing else.
"""

INVALID_ROLE = VALID_ROLE.replace('purpose: keep a small thing true\n', '')

_WRITE_PLACEHOLDER = re.compile(
    r'^(\{(?:reviews_dir|specs_dir|plans_dir|reports_dir)\}|<record>)(/.*)?$')


class LaunchTableTests(unittest.TestCase):
    """T1 — the table covers the roles, and the roles cover the table."""

    def test_every_role_file_has_exactly_one_row_and_every_row_names_a_file(self):
        self.assertEqual(set(launch_mod.TABLE), set(roles.load_all()))
        for name, row in launch_mod.TABLE.items():
            with self.subTest(role=name):
                self.assertEqual(row.role, name)

    def test_every_permission_mode_and_effort_is_one_of_the_runtimes(self):
        for name, row in launch_mod.TABLE.items():
            with self.subTest(role=name):
                self.assertIn(row.permission_mode, launch_mod.PERMISSION_MODES)
                self.assertIn(row.effort, launch_mod.EFFORTS)

    def test_launch_for_resolves_every_brief_kind_with_no_role_error(self):
        for kind in briefs_mod.KINDS:
            with self.subTest(kind=kind):
                launched = launch_mod.launch_for(kind)
                self.assertEqual(launched.role, roles.BINDINGS[kind])
                self.assertIn(launched.role, launch_mod.TABLE)

    def test_writes_carries_no_literal_path(self):
        for name, row in launch_mod.TABLE.items():
            for pattern in row.writes:
                with self.subTest(role=name, pattern=pattern):
                    self.assertTrue(_WRITE_PLACEHOLDER.match(pattern), pattern)

    def test_connections_is_empty_for_every_role(self):
        for name, row in launch_mod.TABLE.items():
            with self.subTest(role=name):
                self.assertEqual(row.connections, ())

    def test_resolve_writes_fills_the_conventions_placeholders(self):
        product = env.Product('sample', {
            'backlog_dir': '/repo/backlog',
            'conventions': {'reviews_dir': 'reviews', 'specs_dir': 'specs',
                            'plans_dir': 'plans', 'reports_dir': 'reports'},
        })
        self.assertEqual(launch_mod.resolve_writes(launch_mod.TABLE['asf-reviewer'], product),
                         ('reviews/**',))
        self.assertEqual(launch_mod.resolve_writes(launch_mod.TABLE['asf-prober'], product),
                         ('reports/**',))
        self.assertEqual(launch_mod.resolve_writes(launch_mod.TABLE['asf-writer'], product),
                         ('specs/**', 'plans/**', '/repo/backlog/**'))
        self.assertEqual(launch_mod.resolve_writes(launch_mod.TABLE['asf-interrogator'], product),
                         ('/repo/backlog/**',))
        self.assertEqual(launch_mod.resolve_writes(launch_mod.TABLE['asf-coder'], product), ())

    def test_launch_for_applies_only_effort_and_permission_mode_overrides(self):
        product = env.Product('sample', {
            'conventions': {'roles': {'asf-coder': {'effort': 'medium',
                                                     'permission_mode': 'manual'}}}})
        launched = launch_mod.launch_for('coder', product)
        self.assertEqual((launched.effort, launched.permission_mode), ('medium', 'manual'))
        self.assertEqual(launched.role, 'asf-coder')

    def test_launch_for_refuses_an_override_of_tools_writes_or_connections(self):
        for key, value in (('tools', ('Bash',)), ('writes', ('x/**',)), ('connections', ('mcp',))):
            product = env.Product('sample', {'conventions': {'roles': {'asf-coder': {key: value}}}})
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    launch_mod.launch_for('coder', product)
                self.assertIn(key, str(ctx.exception))
                self.assertIn('amendable', str(ctx.exception))

    def test_launch_for_with_no_override_returns_the_table_row_unchanged(self):
        product = env.Product('sample', {})
        self.assertEqual(launch_mod.launch_for('review', product), launch_mod.TABLE['asf-reviewer'])
        self.assertEqual(launch_mod.launch_for('review'), launch_mod.TABLE['asf-reviewer'])

    def test_effort_for_a_non_mechanical_kind_is_the_rows_own_regardless_of_caps(self):
        self.assertEqual(launch_mod.effort_for('review'), launch_mod.TABLE['asf-reviewer'].effort)
        self.assertEqual(launch_mod.effort_for('review', caps=()),
                         launch_mod.TABLE['asf-reviewer'].effort)

    def test_effort_for_a_mechanical_kind_is_the_lowest_offered_level(self):
        for kind in launch_mod.MECHANICAL:
            with self.subTest(kind=kind):
                self.assertEqual(launch_mod.effort_for(kind, caps=('medium', 'high')), 'medium')
                self.assertEqual(launch_mod.effort_for(kind, caps=('low', 'medium', 'high')),
                                 'low')

    def test_effort_for_a_mechanical_kind_is_empty_with_no_effort_flag(self):
        self.assertEqual(launch_mod.effort_for('close', caps=()), '')

    def test_agents_path_is_a_sibling_of_the_briefs_directory(self):
        tmp = tempfile.mkdtemp(prefix='asf-launch-')
        self.addCleanup(shutil.rmtree, tmp, True)
        home = env.ASF_HOME
        env.ASF_HOME = tmp
        try:
            product = env.Product('sample', {})
            path = launch_mod.agents_path(product, 'j1')
            self.assertEqual(path, os.path.join(env.state_dir(product), 'agents', 'j1.json'))
            self.assertEqual(os.path.dirname(os.path.dirname(path)), env.state_dir(product))
        finally:
            env.ASF_HOME = home


class CapabilityTests(unittest.TestCase):
    """T2 — the probe: which of the newer flags the installed binary accepts."""

    HELP_TEXT = (
        'Usage: fixture [options]\n\n'
        'Options:\n'
        '  --agent <name>  The default agent for this session\n'
        '  --agents <file>  A JSON file of agent definitions\n'
        '  --allowedTools, --allowed-tools <tools...>  Allow specified tools\n'
        '  --disallowedTools, --disallowed-tools <tools...>  Deny specified tools\n'
        '  --effort <level>  Effort level for the current session\n'
        '                    (low, medium, high, xhigh, max)\n'
        '  --max-budget-usd <dollars>  Maximum spend for this session\n'
        '  --model <model>  The model to use\n'
        '  --permission-mode <mode>  Permission mode to use\n'
        '  --plugin-dir <path>  Load a plugin from this directory\n'
        '  --resume <id>  Resume a previous session\n'
        '  --restricted  Run with a restricted tool set\n'
        '  --strict-mcp-config  Only use MCP servers from --mcp-config\n'
        '  --tools <tools...>  Comma or space separated list of tool names\n'
    )

    EXPECTED_FLAGS = {
        'agent', 'agents', 'allowedTools', 'allowed-tools', 'disallowedTools',
        'disallowed-tools', 'effort', 'max-budget-usd', 'model', 'permission-mode',
        'plugin-dir', 'resume', 'restricted', 'strict-mcp-config', 'tools',
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-capability-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        capability.reset()
        self.addCleanup(capability.reset)
        self._path = os.environ.get('PATH', '')

    def _script(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def _fixture(self, name='fixture', text=None):
        marker = 'HELPTEXTEOF'
        return self._script(name, f"#!/bin/sh\ncat <<'{marker}'\n{text or self.HELP_TEXT}{marker}\n")

    def _on_path(self, name):
        patcher = mock.patch.dict(os.environ, {'PATH': self.tmp + os.pathsep + self._path})
        patcher.start()
        self.addCleanup(patcher.stop)
        return name

    def test_flags_parses_every_name_and_both_aliases_of_a_two_name_line(self):
        self._fixture('fixture')
        self.assertEqual(capability.flags(self._on_path('fixture')), self.EXPECTED_FLAGS)

    def test_supports_and_missing_read_the_same_probe(self):
        self._fixture('fixture')
        binary = self._on_path('fixture')
        self.assertTrue(capability.supports('effort', binary))
        self.assertFalse(capability.supports('no-such-flag', binary))
        self.assertEqual(capability.missing(('effort', 'no-such-flag', 'tools'), binary),
                         ('no-such-flag',))

    def test_an_absent_binary_is_an_empty_set_not_an_exception(self):
        self.assertEqual(capability.flags('asf-capability-test-does-not-exist'), set())

    def test_a_non_zero_exit_is_an_empty_set(self):
        self._script('bad-exit', '#!/bin/sh\necho "--effort <level>"\nexit 3\n')
        self.assertEqual(capability.flags(self._on_path('bad-exit')), set())

    def test_empty_output_is_an_empty_set(self):
        self._script('quiet', '#!/bin/sh\ntrue\n')
        self.assertEqual(capability.flags(self._on_path('quiet')), set())

    def test_a_hanging_binary_times_out_to_an_empty_set(self):
        self._script('slow', '#!/bin/sh\nsleep 5\n')
        binary = self._on_path('slow')
        with mock.patch.object(capability, 'PROBE_TIMEOUT_S', 0.05):
            self.assertEqual(capability.flags(binary), set())

    def test_the_result_is_cached_until_the_fixture_changes_and_reset_clears_it(self):
        self._fixture('fixture', text='Options:\n  --agent <name>  x\n')
        binary = self._on_path('fixture')
        self.assertEqual(capability.flags(binary), {'agent'})
        # rewritten with a later mtime and a different size: a fresh probe, not the cached one
        path = os.path.join(self.tmp, 'fixture')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("#!/bin/sh\ncat <<'EOF'\nOptions:\n  --agent <name>  x\n  --effort <l>  y\nEOF\n")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(capability.flags(binary), {'agent', 'effort'})
        capability.reset()
        self.assertEqual(capability._CACHE, {})

    def test_effort_levels_parses_the_parenthesised_list_on_the_continuation_line(self):
        self._fixture('fixture')
        self.assertEqual(capability.effort_levels(self._on_path('fixture')),
                         ('low', 'medium', 'high', 'xhigh', 'max'))

    def test_effort_levels_falls_back_when_the_entry_carries_no_parenthesised_list(self):
        self._fixture('fixture', text='Options:\n  --effort <level>  Effort level, no choices given\n')
        self.assertEqual(capability.effort_levels(self._on_path('fixture')),
                         capability._FALLBACK_EFFORTS)

    def test_effort_levels_is_empty_with_no_effort_option_at_all(self):
        self._fixture('fixture', text='Options:\n  --model <model>  The model to use\n')
        self.assertEqual(capability.effort_levels(self._on_path('fixture')), ())

    def test_effort_levels_is_empty_for_an_absent_binary(self):
        self.assertEqual(capability.effort_levels('asf-capability-test-does-not-exist'), ())


class AgentFileTests(unittest.TestCase):
    """T4 — the agents file: one JSON object per launch, a function of the role and the row."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-agents-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(setattr, env, 'ASF_HOME', self._home)
        self.product = env.Product('sample', {})

    def test_every_shipped_role_writes_valid_json_keyed_by_the_role(self):
        for name, row in launch_mod.TABLE.items():
            with self.subTest(role=name):
                path = launch_mod.write_agents_file(self.product, f'job-{name}', row)
                with open(path, encoding='utf-8') as f:
                    obj = json.load(f)
                role = roles.load(name)
                self.assertEqual(list(obj), [name])
                self.assertEqual(obj[name]['description'], role.purpose)
                self.assertEqual(obj[name]['prompt'], roles.block(role, roles.MAX_LINES))
                want_tools = sorted(t for t in row.tools if t not in row.deny_tools)
                self.assertEqual(sorted(obj[name]['tools']), want_tools)

    def test_two_calls_over_an_unchanged_role_and_row_are_byte_identical(self):
        row = launch_mod.TABLE['asf-coder']
        p1 = launch_mod.write_agents_file(self.product, 'job-a', row)
        p2 = launch_mod.write_agents_file(self.product, 'job-b', row)
        with open(p1, 'rb') as f:
            b1 = f.read()
        with open(p2, 'rb') as f:
            b2 = f.read()
        self.assertEqual(b1, b2)

    def test_the_file_lands_under_the_jobs_state_and_is_removed_with_it(self):
        row = launch_mod.TABLE['asf-coder']
        path = launch_mod.write_agents_file(self.product, 'job-a', row)
        self.assertTrue(path.startswith(env.state_dir(self.product)))
        self.assertTrue(os.path.exists(path))
        shutil.rmtree(env.state_dir(self.product))
        self.assertFalse(os.path.exists(path))

    def test_an_invalid_role_raises_before_anything_is_written(self):
        roles_dir = tempfile.mkdtemp(prefix='asf-bad-roles-')
        self.addCleanup(shutil.rmtree, roles_dir, True)
        with open(os.path.join(roles_dir, 'asf-demo.md'), 'w', encoding='utf-8') as f:
            f.write(INVALID_ROLE)
        bad = launch_mod.Launch('asf-demo', 'plan', 'low')
        with mock.patch.dict(os.environ, {'ASF_ROLES_DIR': roles_dir}):
            with self.assertRaises(roles.RoleError) as ctx:
                launch_mod.write_agents_file(self.product, 'job-a', bad)
        self.assertIn('asf-demo', str(ctx.exception))
        self.assertIn('frontmatter', str(ctx.exception))
        self.assertFalse(os.path.exists(launch_mod.agents_path(self.product, 'job-a')))


if __name__ == '__main__':
    unittest.main()
