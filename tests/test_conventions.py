import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import conventions as conv_mod
from asf.conventions import Conventions

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DefaultsTests(unittest.TestCase):
    def test_every_documented_default_is_what_the_card_says(self):
        c = Conventions()
        self.assertEqual(c.branch_prefixes,
                         {'code': 'worker/', 'fix': 'fix/', 'spec': 'spec/', 'plan': 'plan/',
                          'direct': 'cloud/direct-', 'legacy': []})
        self.assertEqual((c.specs_dir, c.plans_dir, c.reviews_dir),
                         ('docs/specs', 'docs/plans', 'docs/reviews'))
        self.assertEqual(c.review_pattern, '{reviews_dir}/{n}-{slug}.md')
        self.assertEqual((c.task_heading, c.intake_dir), ('### Task', 'inbox'))
        self.assertEqual((c.goals_file, c.default_bug_epic, c.test_command), (None, None, None))
        self.assertEqual((c.main, c.preamble_max_lines, c.prs_per_tick, c.land_window_days),
                         ('main', 120, 6, 7))
        self.assertEqual((c.harvest_gate, c.branches_per_tick, c.gate_timeout_s), ('combined', 12, 600))
        self.assertEqual(c.stage_limits, {})

    def test_the_new_fields_default_to_none(self):
        c = Conventions()
        new_fields = ('briefs_dir', 'matrix_path', 'design_spec_name', 'decisions_file',
                      'reports_dir', 'report_pattern', 'ci_workflow', 'ci_dev_job', 'deploy_workflow')
        for name in new_fields:
            self.assertIsNone(getattr(c, name), name)
        for name in new_fields:
            self.assertIn(name, Conventions.field_names())

    def test_two_instances_do_not_share_their_mutable_defaults(self):
        a, b = Conventions(), Conventions()
        a.branch_prefixes['code'] = 'feature/'
        a.stage_limits['spec'] = 4
        self.assertEqual(b.branch_prefixes['code'], 'worker/')
        self.assertEqual(b.stage_limits, {})


class FromMappingTests(unittest.TestCase):
    def test_a_second_products_conventions_override_the_defaults(self):
        c = Conventions.from_mapping({
            'branch_prefixes': {'code': 'feature/', 'fix': 'bugfix/'},
            'specs_dir': 'specs', 'plans_dir': 'plans', 'main': 'trunk',
            'default_bug_epic': 'E-0042', 'prs_per_tick': 2,
        })
        self.assertEqual(c.branch('code', 'add-login'), 'feature/add-login')
        self.assertEqual(c.branch('fix', 'B-0007'), 'bugfix/B-0007')
        self.assertEqual(c.branch('spec', 'x'), 'spec/x')       # not overridden: still the default
        self.assertEqual((c.specs_dir, c.plans_dir, c.main), ('specs', 'plans', 'trunk'))
        self.assertEqual((c.default_bug_epic, c.prs_per_tick), ('E-0042', 2))

    def test_the_harvest_block_names_the_gate_and_the_cap(self):
        """``harvest: {gate: per-branch, branches_per_tick: 3}`` is how a product yaml spells
        the two harvest fields (B-0040); a key under it nobody reads is kept, not rejected."""
        c = Conventions.from_mapping({'harvest': {'gate': 'per-branch', 'branches_per_tick': 3,
                                                  'gate_timeout_s': 60, 'later': 'x'}})
        self.assertEqual((c.harvest_gate, c.branches_per_tick, c.gate_timeout_s),
                         ('per-branch', 3, 60))
        self.assertEqual(c.extra, {'harvest': {'later': 'x'}})
        self.assertEqual(Conventions.from_mapping({'harvest': {}}), Conventions())
        self.assertEqual(Conventions.from_mapping({'harvest_gate': 'per-branch'}).harvest_gate,
                         'per-branch')

    def test_an_unknown_key_is_kept_not_rejected(self):
        c = Conventions.from_mapping({'slack_channel': '#asf', 'specs_dir': 'specs'})
        self.assertEqual(c.extra, {'slack_channel': '#asf'})
        self.assertEqual(c.get('slack_channel'), '#asf')
        self.assertEqual(c['slack_channel'], '#asf')
        self.assertIn('slack_channel', c)
        self.assertIsNone(c.get('no_such_key'))

    def test_it_still_answers_as_a_mapping(self):
        c = Conventions.from_mapping({'specs_dir': 'specs'})
        self.assertEqual(c.get('specs_dir'), 'specs')
        self.assertEqual(c.get('reviews_dir'), 'docs/reviews')
        self.assertEqual(c.get('goals_file', 'goals.md'), 'goals.md')   # None falls back
        self.assertEqual(c['prs_per_tick'], 6)
        self.assertIn('specs_dir', dict(c.items()))
        with self.assertRaises(KeyError):
            c['nope']

    def test_none_and_an_empty_mapping_are_all_defaults(self):
        self.assertEqual(Conventions.from_mapping(None), Conventions())
        self.assertEqual(Conventions.from_mapping({}), Conventions())


class BranchTests(unittest.TestCase):
    def test_a_prefix_without_a_separator_gets_one(self):
        c = Conventions.from_mapping({'branch_prefixes': {'code': 'feature', 'batch': 'm-'}})
        self.assertEqual(c.branch('code', 'j1'), 'feature/j1')
        self.assertEqual(c.branch('batch', '0006'), 'm-0006')      # `-` is a separator too

    def test_an_unknown_kind_falls_back_to_the_kind_itself(self):
        self.assertEqual(Conventions().branch('probe', 'j1'), 'probe/j1')

    def test_branch_kind_and_strip_prefix_read_the_longest_match(self):
        c = Conventions.from_mapping({'branch_prefixes': {'code': 'feature/', 'batch': 'feature/batch-'}})
        self.assertEqual(c.branch_kind('feature/batch-7'), 'batch')
        self.assertEqual(c.branch_kind('feature/add-login'), 'code')
        self.assertIsNone(c.branch_kind('main'))
        self.assertEqual(c.strip_prefix('feature/batch-7'), '7')
        self.assertEqual(c.strip_prefix('nothing-known'), 'nothing-known')

    def test_a_retired_prefix_is_recognised_but_never_minted(self):
        c = Conventions.from_mapping({'branch_prefixes': {'code': 'feature/', 'legacy': ['old/', 'older/']}})
        self.assertEqual(c.branch_kind('old/thing'), 'legacy')
        self.assertEqual(c.strip_prefix('old/thing'), 'thing')
        self.assertEqual(c.branch('code', 'thing'), 'feature/thing')
        self.assertEqual(c.legacy_prefixes(), ('old/', 'older/'))

    def test_the_trunk_is_the_products_own(self):
        self.assertTrue(Conventions().is_trunk('main'))
        self.assertFalse(Conventions(main='trunk').is_trunk('main'))
        self.assertTrue(Conventions(main='trunk').is_trunk('trunk'))


class PathTests(unittest.TestCase):
    def test_review_path_substitutes_the_reviews_dir_the_round_and_the_slug(self):
        self.assertEqual(Conventions().review_path('add-login', 2), 'docs/reviews/2-add-login.md')
        c = Conventions.from_mapping({'reviews_dir': 'reviews',
                                      'review_pattern': '{reviews_dir}/{slug}-r{n}.md'})
        self.assertEqual(c.review_path('add-login', 3), 'reviews/add-login-r3.md')

    def test_doc_dir(self):
        c = Conventions.from_mapping({'specs_dir': 'specs'})
        self.assertEqual((c.doc_dir('spec'), c.doc_dir('plan')), ('specs', 'docs/plans'))


class ForbiddenPatternsTests(unittest.TestCase):
    def test_one_pattern_per_path_shaped_default(self):
        patterns = conv_mod.forbidden_patterns()
        self.assertEqual(len(patterns), 11)
        self.assertIn("['\"]" + re.escape('cloud/direct-'), patterns)
        self.assertIn("['\"]" + re.escape(conv_mod.DEFAULT_RELEASE_INSTALL), patterns)
        self.assertIn("['\"]worker/", patterns)
        self.assertIn("['\"]docs/specs\\b", patterns)
        self.assertIn("['\"]" + re.escape('{reviews_dir}/{n}-{slug}.md') + '\\b', patterns)
        self.assertIn("['\"]" + re.escape('### Task') + '\\b', patterns)
        for pattern in patterns:
            self.assertIsNone(re.search(pattern, 'main'))
            self.assertIsNone(re.search(pattern, 'inbox'))

    def test_a_copied_default_matches_and_prose_does_not(self):
        patterns = conv_mod.forbidden_patterns()

        def matches_any(text):
            return any(re.search(p, text) for p in patterns)

        self.assertTrue(matches_any("X = 'docs/specs'"))
        self.assertTrue(matches_any('default="fix/"'))
        self.assertTrue(matches_any("f'worker/{iid}'"))
        self.assertFalse(matches_any('the worker/ prefix'))

    def test_the_module_prints_them(self):
        result = subprocess.run([sys.executable, '-m', 'asf.conventions', '--forbidden'],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), conv_mod.forbidden_patterns())

    def test_the_dict_valued_savings_default_adds_no_pattern(self):
        # forbidden_patterns() walks string defaults containing '/', '{' or '#' (P14); a
        # dict-valued default like DEFAULT_SAVINGS is never a path-shaped literal.
        self.assertIsInstance(conv_mod.DEFAULT_SAVINGS, dict)
        self.assertEqual(len(conv_mod.forbidden_patterns()), 11)


class CheckConventionsScriptTests(unittest.TestCase):
    """The check itself: a literal that belongs in the product yaml fails the build."""

    def script(self, *args):
        return subprocess.run(['bash', os.path.join(REPO_ROOT, 'tools', 'check_conventions.sh'), *args],
                              cwd=REPO_ROOT, capture_output=True, text=True)

    def test_the_pattern_list_is_the_cards_inventory(self):
        with open(os.path.join(REPO_ROOT, 'tools', 'forbidden-conventions.txt'), encoding='utf-8') as f:
            patterns = [l.split('#')[0].strip() for l in f]
        patterns = [p for p in patterns if p]
        for want in ('cloud/', 'worktree-m-', 'docs/superpowers', r'\.sdd-input', r'goals\.txt',
                     'feature-matrix', r'session-results\.jsonl', r'\.claude-workers', '/tmp/',
                     'refs/heads/hb', r'deploy-prod\.yml', r'\bci\.yml\b', 'deploy-dev',
                     'hotfix-.*-report', 'ci-diag-'):
            self.assertIn(want, patterns)

    def test_the_ci_diag_pattern_spares_the_severity_parser(self):
        # the trailing hyphen is what keeps the pattern on report *names*: the non-goal keeps
        # hotfix_bug_fields as it is, and its bare `'ci-diag'` prefix test must not match
        with open(os.path.join(REPO_ROOT, 'asf', 'tick', 'migrate.py'), encoding='utf-8') as f:
            source = f.read()
        self.assertIn("startswith('ci-diag')", source)
        self.assertNotRegex(source, 'ci-diag-')

    def test_the_merged_patterns_include_the_generated_ones(self):
        lines = self.script('--print-patterns').stdout.splitlines()
        for pattern in conv_mod.forbidden_patterns():
            self.assertIn(pattern, lines)
        self.assertIn("['\"]" + re.escape('### Task') + '\\b', lines)

    def test_only_the_four_adapters_are_excluded(self):
        with open(os.path.join(REPO_ROOT, 'tools', 'check_conventions.sh'), encoding='utf-8') as f:
            text = f.read()
        block = re.search(r'excludes=\((.*?)\)', text, re.S).group(1)
        entries = re.findall(r'"([^"]+)"', block)
        self.assertEqual(len(entries), 4, entries)
        for name in ('evidence.py', 'match.py', 'ingest.py', 'stale.py'):
            self.assertNotIn(name, block)

    def test_a_copied_default_fails_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(os.path.join(REPO_ROOT, 'tools'), os.path.join(tmp, 'tools'))
            asf_dir = os.path.join(tmp, 'asf')
            os.makedirs(asf_dir)
            shutil.copy(os.path.join(REPO_ROOT, 'asf', '__init__.py'), asf_dir)
            shutil.copy(os.path.join(REPO_ROOT, 'asf', 'conventions.py'), asf_dir)
            with open(os.path.join(asf_dir, 'x.py'), 'w', encoding='utf-8') as f:
                f.write("SPECS = 'docs/specs'\n")
            subprocess.run(['git', 'init', '-q'], cwd=tmp, check=True)
            subprocess.run(['git', 'add', '-A'], cwd=tmp, check=True)
            r = subprocess.run(['bash', os.path.join(tmp, 'tools', 'check_conventions.sh')],
                               cwd=tmp, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn('asf/x.py:1', r.stdout)

    def test_a_missing_generator_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = os.path.join(tmp, 'python3')
            with open(stub, 'w', encoding='utf-8') as f:
                f.write('#!/bin/sh\nexit 1\n')
            os.chmod(stub, 0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get('PATH', ''))
            r = subprocess.run(['bash', os.path.join(REPO_ROOT, 'tools', 'check_conventions.sh')],
                               cwd=REPO_ROOT, capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn('could not read the defaults', r.stderr)

    def test_the_conventions_module_itself_is_exempt(self):
        # the defaults are literals by definition; the check must not see its own source
        with open(os.path.join(REPO_ROOT, 'asf', 'conventions.py'), encoding='utf-8') as f:
            self.assertIn('worker/', f.read())
        out = self.script().stdout
        self.assertNotIn('asf/conventions.py:', out)
        self.assertNotIn('asf/metrics/import_sessions.py:', out)

    def test_the_modules_this_card_owns_carry_no_convention(self):
        hits = [l for l in self.script().stdout.splitlines()
                if l.startswith(('asf/conventions.py', 'asf/env.py', 'asf/metrics/', 'asf/harvest/',
                                 'asf/tick/file_bugs.py'))]
        self.assertEqual(hits, [])

    def test_the_whole_package_is_clean(self):
        """The purge is complete: no product convention anywhere in `asf/` outside the
        documented adapters (the script's own exclusion list)."""
        r = self.script()
        self.assertEqual(r.returncode, 0, r.stdout)


class ModuleConstantsTests(unittest.TestCase):
    def test_the_defaults_are_exported_one_per_field(self):
        c = Conventions()
        self.assertEqual(conv_mod.DEFAULT_BRANCH_PREFIXES['code'], c.branch_prefixes['code'])
        self.assertEqual(conv_mod.DEFAULT_SPECS_DIR, c.specs_dir)
        self.assertEqual(conv_mod.DEFAULT_PRS_PER_TICK, c.prs_per_tick)
        self.assertEqual(conv_mod.DEFAULT_LAND_WINDOW_DAYS, c.land_window_days)
        self.assertEqual(conv_mod.DEFAULT_HARVEST_GATE, c.harvest_gate)
        self.assertEqual(conv_mod.DEFAULT_BRANCHES_PER_TICK, c.branches_per_tick)
        self.assertEqual(conv_mod.DEFAULT_GATE_TIMEOUT_S, c.gate_timeout_s)
        self.assertEqual(conv_mod.DEFAULT_SAVINGS, c.savings)


class AmendableFieldsTests(unittest.TestCase):
    def test_evals_dir_defaults_to_evals(self):
        self.assertEqual(Conventions().evals_dir, 'evals')
        self.assertEqual(Conventions.from_mapping({'evals_dir': 'qa'}).evals_dir, 'qa')

    def test_amendable_paths_has_three_states(self):
        self.assertIsNone(Conventions().amendable_paths)
        self.assertIsNone(Conventions.from_mapping({}).amendable_paths)
        self.assertIsNone(Conventions.from_mapping({'amendable_paths': None}).amendable_paths)
        got = Conventions.from_mapping({'amendable_paths': ['a/*']}).amendable_paths
        self.assertEqual(got, ['a/*'])
        self.assertEqual(Conventions.from_mapping({'amendable_paths': []}).amendable_paths, [])


class SavingsDefaultsTests(unittest.TestCase):
    def test_the_defaults_are_what_the_card_says(self):
        self.assertEqual(conv_mod.DEFAULT_SAVINGS, {
            'window_days': 7, 'min_landings': 5, 'preamble_ratio': 2.0, 'gate_minutes_max': 20.0,
            'rounds_per_landing': 1.3, 'step_duration_ratio': 1.5, 'spend_ratio': 1.5,
            'failure_class_count': 3,
        })

    def test_a_fresh_conventions_carries_the_defaults(self):
        self.assertEqual(Conventions().savings, conv_mod.DEFAULT_SAVINGS)

    def test_two_instances_do_not_share_their_savings_dict(self):
        a, b = Conventions(), Conventions()
        a.savings['window_days'] = 14
        self.assertEqual(b.savings['window_days'], 7)

    def test_a_product_naming_none_gets_all_eight(self):
        c = Conventions.from_mapping({})
        for key in conv_mod.DEFAULT_SAVINGS:
            self.assertEqual(conv_mod.savings_for(c, key), conv_mod.DEFAULT_SAVINGS[key])

    def test_a_product_overriding_one_key_keeps_the_other_seven(self):
        c = Conventions.from_mapping({'savings': {'gate_minutes_max': 30.0}})
        self.assertEqual(conv_mod.savings_for(c, 'gate_minutes_max'), 30.0)
        for key, default in conv_mod.DEFAULT_SAVINGS.items():
            if key != 'gate_minutes_max':
                self.assertEqual(conv_mod.savings_for(c, key), default)


if __name__ == '__main__':
    unittest.main()
