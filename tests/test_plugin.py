import glob
import os
import re
import subprocess
import unittest
from unittest import mock

from asf import __version__
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
                self.assertIn(f'asf {name} $ARGUMENTS', text)
                self.assertTrue(name in commands or name in PENDING_VIEWS)
                for pat in patterns:
                    self.assertIsNone(pat.search(text), pat.pattern)

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
        self.assertIn(__version__, r.stdout)
        self.assertIn('Autonomous Software Factory', r.stdout)


if __name__ == '__main__':
    unittest.main()
