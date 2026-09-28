import glob
import os
import re
import subprocess
import unittest
from unittest import mock

from asf.cli import build_parser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = sorted(glob.glob(os.path.join(REPO_ROOT, 'plugin', 'skills', '*', 'SKILL.md')))
# a skill may precede its view: `next` lands with the feeder
from asf import plugin_build

PENDING_VIEWS = set()


def forbidden_patterns():
    with open(os.path.join(REPO_ROOT, 'tools', 'forbidden-names.txt'), encoding='utf-8') as f:
        return [re.compile(l.strip(), re.I) for l in f if l.strip() and not l.startswith('#')]


def registered_commands():
    sub = [a for a in build_parser()._actions if a.dest == 'command'][0]
    return set(sub.choices)


def frontmatter_keys(text):
    head = text.split('---')[1]
    return {l.split(':', 1)[0] for l in head.strip().splitlines() if ':' in l}


class PluginTests(unittest.TestCase):
    def test_skills_exist(self):
        names = {os.path.basename(os.path.dirname(p)) for p in SKILLS}
        self.assertTrue({'roadmap', 'backlog', 'parity', 'prod', 'sessions', 'status'} <= names)

    def test_plugin_is_generated_from_the_cli(self):
        # the tree on disk is exactly what `asf plugin build` writes; every view has a skill,
        # every skill is a view or a dialogue, and no stray skill directory exists
        self.assertEqual(plugin_build.diff(), [], 'run `asf plugin build`')
        names = {os.path.basename(os.path.dirname(p)) for p in SKILLS}
        self.assertEqual(names, set(plugin_build.skill_names()))
        self.assertTrue(set(plugin_build.skill_names()) <= registered_commands())

    def test_marketplace_names_the_plugin(self):
        import json
        with open(os.path.join(REPO_ROOT, '.claude-plugin', 'marketplace.json'), encoding='utf-8') as f:
            m = json.load(f)
        self.assertEqual(m['name'], 'asf')
        self.assertEqual(m['plugins'][0]['source'], './plugin')
        self.assertEqual(m['plugins'][0]['name'], 'asf')

    def test_b0047_plugin_dir_comes_from_the_checkout_not_the_package(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # a checkout layout somewhere else, the package untouched: cwd decides
            os.makedirs(os.path.join(tmp, '.claude-plugin'))
            with open(os.path.join(tmp, '.claude-plugin', 'marketplace.json'), 'w') as f:
                f.write('{}')
            self.assertEqual(plugin_build.default_plugin_dir(tmp), os.path.join(tmp, 'plugin'))
            self.assertEqual(plugin_build.run_action('build', cwd=tmp, out=lambda s: None), 0)
            self.assertEqual(plugin_build.run_action('check', cwd=tmp, out=lambda s: None), 0)
            # a cwd with no checkout and a package with no plugin beside it (a plain install):
            # refuse with one line, never report every skill as stale
            elsewhere = os.path.join(tmp, 'elsewhere')
            os.makedirs(elsewhere)
            with mock.patch.object(plugin_build, 'PACKAGE_ROOT', os.path.join(tmp, 'venv')):
                self.assertIsNone(plugin_build.default_plugin_dir(elsewhere))
                self.assertEqual(plugin_build.run_action('check', cwd=elsewhere, out=lambda s: None), 2)

    def test_build_and_check_roundtrip_in_a_temp_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, 'plugin')
            self.assertEqual(plugin_build.check(d, out=lambda s: None), 1)
            self.assertEqual(plugin_build.build(d, out=lambda s: None), 0)
            self.assertEqual(plugin_build.check(d, out=lambda s: None), 0)
            os.makedirs(os.path.join(d, 'skills', 'stray'))
            self.assertEqual(plugin_build.check(d, out=lambda s: None), 1)

    def test_every_skill_is_well_formed(self):
        commands = registered_commands()
        patterns = forbidden_patterns()
        for path in SKILLS:
            name = os.path.basename(os.path.dirname(path))
            text = open(path, encoding='utf-8').read()
            with self.subTest(skill=name):
                self.assertEqual(frontmatter_keys(text), {'name', 'description', 'allowed-tools'})
                self.assertIn(f'name: {name}\n', text)
                self.assertIn('allowed-tools: Bash', text)
                self.assertIn(f'"$ASF_BIN" {name} $ARGUMENTS', text)
                self.assertNotIn('asf-live', text)
                self.assertTrue(name in commands or name in PENDING_VIEWS)
                for pat in patterns:
                    self.assertIsNone(pat.search(text), pat.pattern)

    def test_b0090_plugin_ships_a_hook_for_the_console_rules(self):
        # B-0090: the operator's console rules lived in assistant memory only — the plugin
        # shipped no hooks. `asf plugin build` now generates plugin/hooks/hooks.json with a
        # SessionStart hook, so every console that installs the plugin gets them in code.
        import json
        path = os.path.join(REPO_ROOT, 'plugin', 'hooks', 'hooks.json')
        self.assertTrue(os.path.isfile(path), 'plugin/hooks/hooks.json is missing — run `asf plugin build`')
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        session_start = data['hooks']['SessionStart']
        commands = [h['command'] for group in session_start for h in group['hooks']]
        self.assertTrue(any('delegate' in c.lower() for c in commands),
                        'no SessionStart hook prints the console rules')
        self.assertEqual(plugin_build.diff(), [], 'run `asf plugin build`')

    def test_plugin_json(self):
        import json
        with open(os.path.join(REPO_ROOT, 'plugin', '.claude-plugin', 'plugin.json'),
                  encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['name'], 'asf')
        for key in ('description', 'version', 'author'):
            self.assertIn(key, data)

    def test_wrapper_prints_version_from_any_cwd(self):
        r = subprocess.run([os.path.join(REPO_ROOT, 'tools', 'asf'), '--version'], cwd='/',
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        from asf.cli import version_string
        self.assertEqual(r.stdout.strip(), f'asf {version_string()}')
        self.assertRegex(r.stdout, r'^asf (v\d+\.\d+\.\d+(\+\d+)?|\d+\.\d+\.\d+)')


class PluginInstallTests(unittest.TestCase):
    """`asf plugin install` (T-0405, B-0047): the marketplace tree from the installed package,
    reading nothing from a checkout — so a `pipx` install has the `/asf:*` skills."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'ASF_HOME')
        self.cwd = os.path.join(self.tmp.name, 'no-checkout-here')
        os.makedirs(self.cwd)
        self.env_patch = mock.patch.object(plugin_build.env, 'ASF_HOME', self.home)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self.old_cwd)

    def test_writes_every_expected_file_under_installed_plugin_dir(self):
        dest = plugin_build.installed_plugin_dir()
        self.assertEqual(dest, os.path.join(self.home, 'plugin'))
        self.assertEqual(plugin_build.run_action('install', out=lambda s: None), 0)
        plugin_dir = os.path.join(dest, 'plugin')
        expected = plugin_build.expected_files(plugin_dir)
        self.assertTrue(expected)
        for path, text in expected.items():
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), text, path)

    def test_second_run_changes_no_byte(self):
        plugin_build.run_action('install', out=lambda s: None)
        dest = plugin_build.installed_plugin_dir()
        plugin_dir = os.path.join(dest, 'plugin')
        before = {}
        for root, _, names in os.walk(dest):
            for n in names:
                p = os.path.join(root, n)
                with open(p, 'rb') as f:
                    before[p] = f.read()
        plugin_build.run_action('install', out=lambda s: None)
        after = {}
        for root, _, names in os.walk(dest):
            for n in names:
                p = os.path.join(root, n)
                with open(p, 'rb') as f:
                    after[p] = f.read()
        self.assertEqual(before, after)
        self.assertTrue(before)
        del plugin_dir

    def test_stray_skill_is_removed(self):
        dest = plugin_build.installed_plugin_dir()
        plugin_build.run_action('install', out=lambda s: None)
        plugin_dir = os.path.join(dest, 'plugin')
        stray = os.path.join(plugin_dir, 'skills', 'stray')
        os.makedirs(stray)
        with open(os.path.join(stray, 'SKILL.md'), 'w') as f:
            f.write('x')
        plugin_build.run_action('install', out=lambda s: None)
        self.assertFalse(os.path.exists(stray))

    def test_dir_overrides_the_destination(self):
        override = os.path.join(self.tmp.name, 'elsewhere')
        self.assertEqual(plugin_build.run_action('install', plugin_dir=override,
                                                  out=lambda s: None), 0)
        plugin_dir = os.path.join(override, 'plugin')
        for path in plugin_build.expected_files(plugin_dir):
            self.assertTrue(os.path.isfile(path), path)
        self.assertFalse(os.path.exists(plugin_build.installed_plugin_dir()))

    def test_default_plugin_dir_never_called_on_the_install_path(self):
        with mock.patch.object(plugin_build, 'default_plugin_dir') as m:
            self.assertEqual(plugin_build.run_action('install', out=lambda s: None), 0)
            m.assert_not_called()

    def test_registered_as_a_third_action(self):
        sub = [a for a in build_parser()._actions if a.dest == 'command'][0]
        p = sub.choices['plugin']
        action = [a for a in p._actions if a.dest == 'action'][0]
        self.assertEqual(action.choices, ['build', 'check', 'install'])


if __name__ == '__main__':
    unittest.main()
