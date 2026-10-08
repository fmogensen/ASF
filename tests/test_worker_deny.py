"""F-0062 Task 2 — the per-role worker settings file (T9's own module).

:class:`SettingsTests` is this Task's: :mod:`asf.workers.worker_settings`, built on
``console_perms``'s shape. ``RedactOutTests`` is Task 5's and ``TranscriptScrubTests`` is Task
7's — neither lands here.
"""
import json
import os
import shutil
import tempfile
import unittest

from asf import env
from asf.roles import launch as launch_mod
from asf.workers import worker_settings


class SettingsTests(unittest.TestCase):
    """T9 — the deny rules are in every worker configuration."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='worker_deny_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(self._restore_home)
        self.product = env.Product('sample', {})

    def _restore_home(self):
        env.ASF_HOME = self.home

    def write_json(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return path

    def test_every_role_carries_every_rule_of_deny_rules_including_the_fixed_ones(self):
        for role, launch in launch_mod.TABLE.items():
            with self.subTest(role=role):
                obj = worker_settings.settings(self.product, launch)
                deny = obj['permissions']['deny']
                for rule in worker_settings.FIXED_DENY:
                    self.assertIn(rule, deny)
                for rule in worker_settings.deny_rules(self.product, launch):
                    self.assertIn(rule, deny)
                self.assertIn('Bash(env)', deny)
                self.assertIn('Read(**/.env)', deny)
                self.assertIn('Bash(git push --no-verify:*)', deny)
                self.assertIn('Bash(git push --force:*)', deny)
                self.assertIn('Bash(git push origin main:*)', deny)
                for rule in worker_settings.allow_rules(self.product, launch):
                    self.assertIn(rule, obj['permissions']['allow'])

    def test_deny_rules_reads_the_trunk_from_conventions_not_a_literal(self):
        other = env.Product('other', {'main': 'trunk'})
        rules = worker_settings.deny_rules(other, launch_mod.TABLE['asf-reviewer'])
        self.assertIn('Bash(git push origin trunk:*)', rules)
        self.assertNotIn('Bash(git push origin main:*)', rules)

    def test_operator_file_with_its_own_rules_and_unrelated_keys_survives_the_merge(self):
        operator_path = self.write_json('operator.json', {
            'permissions': {'allow': ['Bash(operator-allow:*)'], 'deny': ['Bash(operator-deny:*)']},
            'env': {'FOO': 'bar'},
            'hooks': {'PreToolUse': []},
        })
        launch = launch_mod.TABLE['asf-reviewer']
        obj = worker_settings.settings(self.product, launch, operator_path)
        self.assertEqual(obj['env'], {'FOO': 'bar'})
        self.assertEqual(obj['hooks'], {'PreToolUse': []})
        self.assertIn('Bash(operator-allow:*)', obj['permissions']['allow'])
        self.assertIn('Bash(operator-deny:*)', obj['permissions']['deny'])
        for rule in worker_settings.deny_rules(self.product, launch):
            self.assertIn(rule, obj['permissions']['deny'])

    def test_a_second_settings_over_the_written_file_changes_no_byte(self):
        launch = launch_mod.TABLE['asf-prober']
        operator_path = self.write_json('operator.json', {'permissions': {'allow': ['Bash(x:*)']}})
        first = worker_settings.settings(self.product, launch, operator_path)
        written = self.write_json('written.json', first)
        second = worker_settings.settings(self.product, launch, written)
        self.assertEqual(first, second)

    def test_path_is_per_role_and_two_roles_never_share_one(self):
        reviewer = worker_settings.path(self.product, launch_mod.TABLE['asf-reviewer'], {})
        coder = worker_settings.path(self.product, launch_mod.TABLE['asf-coder'], {})
        self.assertNotEqual(reviewer, coder)
        self.assertEqual(reviewer, worker_settings.settings_path(
            self.product, launch_mod.TABLE['asf-reviewer']))
        self.assertTrue(os.path.isfile(reviewer))
        self.assertTrue(os.path.isfile(coder))

    def test_path_writes_only_when_the_content_differs(self):
        launch = launch_mod.TABLE['asf-coder']
        written = worker_settings.path(self.product, launch, {})
        mtime_1 = os.path.getmtime(written)
        worker_settings.path(self.product, launch, {})
        mtime_2 = os.path.getmtime(written)
        self.assertEqual(mtime_1, mtime_2)

    def test_missing_names_a_rule_deleted_by_hand(self):
        launch = launch_mod.TABLE['asf-reviewer']
        written = worker_settings.path(self.product, launch, {})
        with open(written, encoding='utf-8') as f:
            obj = json.load(f)
        obj['permissions']['deny'].remove('Bash(env)')
        missing_allow, missing_deny = worker_settings.missing(obj, self.product, launch)
        self.assertEqual(missing_allow, [])
        self.assertIn('Bash(env)', missing_deny)

    def test_missing_is_empty_when_every_rule_is_present(self):
        launch = launch_mod.TABLE['asf-prober']
        obj = worker_settings.settings(self.product, launch)
        missing_allow, missing_deny = worker_settings.missing(obj, self.product, launch)
        self.assertEqual((missing_allow, missing_deny), ([], []))


if __name__ == '__main__':
    unittest.main()
