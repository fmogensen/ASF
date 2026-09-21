import glob
import os
import re
import subprocess
import unittest

from asf import __version__
from asf.cli import build_parser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = sorted(glob.glob(os.path.join(REPO_ROOT, 'plugin', 'skills', '*', 'SKILL.md')))
# a skill may precede its view: `next` lands with the feeder
PENDING_VIEWS = {'next'}


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
