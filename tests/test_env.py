import json
import os
import tempfile
import unittest

from asf import env
from asf.conventions import Conventions

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestExampleConfigsParse(unittest.TestCase):
    def test_config_example_parses(self):
        path = os.path.join(REPO_ROOT, 'docs', 'config.example.yaml')
        data = env.loads(open(path, encoding='utf-8').read())
        self.assertEqual(data['default_product'], 'sample')
        self.assertIn('worker_pool', data)

    def test_products_example_parses_into_a_product(self):
        path = os.path.join(REPO_ROOT, 'docs', 'products.example.yaml')
        data = env.loads(open(path, encoding='utf-8').read())
        product = env.Product('sample', data)
        self.assertEqual(product.repo_slug, 'acme/sample')
        self.assertEqual(product.branch_prefix('task'), 'task')
        self.assertEqual(product.conventions['design_spec_name'], 'design.md')
        self.assertEqual(product.stage_limits['task_active'], '45m')
        self.assertEqual(product.approvals['spend_money'], 'human-now')
        self.assertEqual(product.groom['adjudicate_attempts'], 2)
        self.assertEqual(product.branch_prefix('groom'), 'groom')


class DocumentedModelsTests(unittest.TestCase):
    """F-0093 §2.5.1: `conventions.models` documented, and the measured table beside the
    `worker_pool` block it argues about."""

    def test_products_example_documents_conventions_models(self):
        text = open(os.path.join(REPO_ROOT, 'docs', 'products.example.yaml'), encoding='utf-8').read()
        self.assertEqual(env.validate_product_text(text), [])
        self.assertIn('conventions.models', text)
        for label in ('heavy', 'light', 'cheap'):
            self.assertIn(label, text)

    def test_config_example_documents_the_measured_table(self):
        path = os.path.join(REPO_ROOT, 'docs', 'config.example.yaml')
        text = open(path, encoding='utf-8').read()
        data = env.loads(text)
        self.assertEqual(set(data['worker_pool']['models']), {'heavy', 'light', 'cheap'})
        self.assertIn('min/landing', text)
        for kind in ('plan', 'correct', 'fix-bug', 'adjudicate', 'coder', 'spec'):
            self.assertIn(kind, text)


class TestYamlSubset(unittest.TestCase):
    def test_scalars_and_nesting(self):
        text = """
        product: sample
        repo_dir: ~/Code/sample
        main: main
        conventions:
          specs_dir: docs/specs
          branch_prefixes:
            spec: spec
            task: task
          review_pattern: "docs/reviews/{n}.md"
        customer_paths: [app/, api/]
        stage_limits:
          - stage: spec
            hours: 24
          - stage: plan
            hours: 12
        """
        data = env.loads(_dedent(text))
        self.assertEqual(data['product'], 'sample')
        self.assertEqual(data['conventions']['specs_dir'], 'docs/specs')
        self.assertEqual(data['conventions']['branch_prefixes']['task'], 'task')
        self.assertEqual(data['customer_paths'], ['app/', 'api/'])
        self.assertEqual(data['stage_limits'][0]['stage'], 'spec')
        self.assertEqual(data['stage_limits'][1]['hours'], 12)

    def test_comments_and_bools(self):
        text = """
        enabled: true  # on
        disabled: false
        missing: null
        """
        data = env.loads(_dedent(text))
        self.assertIs(data['enabled'], True)
        self.assertIs(data['disabled'], False)
        self.assertIsNone(data['missing'])

    def test_quoted_keys(self):
        text = """
        ci:
          budgets_minutes:
            site: 40
            "*e2e*": 30
            '*soak*': 35
            default: 20
          pools:
            - "gpu-*": 2
              note: "quoted key opening a list item"
        """
        data = env.loads(_dedent(text))
        budgets = data['ci']['budgets_minutes']
        self.assertEqual(budgets, {'site': 40, '*e2e*': 30, '*soak*': 35, 'default': 20})
        self.assertEqual(data['ci']['pools'][0]['gpu-*'], 2)

    def test_quoted_key_with_a_colon_in_it(self):
        data = env.loads(_dedent("""
        labels:
          "a: b": 1
        """))
        self.assertEqual(data['labels'], {'a: b': 1})


class TestProductConventions(unittest.TestCase):
    """`Product.conventions` is the dataclass, defaults filled in — and still a mapping for the
    callers that read it as one."""

    def product(self, data):
        return env.Product('sample', data)

    def test_the_yaml_block_becomes_a_conventions_object(self):
        p = self.product({'main': 'trunk', 'conventions': {'specs_dir': 'specs',
                                                           'branch_prefixes': {'code': 'feature/'}}})
        self.assertIsInstance(p.conventions, Conventions)
        self.assertEqual(p.conventions.specs_dir, 'specs')
        self.assertEqual(p.conventions.plans_dir, 'docs/plans')          # documented default
        self.assertEqual(p.conventions.branch('code', 'j1'), 'feature/j1')

    def test_a_product_with_no_conventions_block_gets_the_defaults(self):
        p = self.product({'repo_slug': 'acme/sample'})
        self.assertEqual(p.conventions.main, 'main')
        self.assertEqual(p.conventions.branch('code', 'j1'), 'worker/j1')

    def test_top_level_main_stage_limits_and_test_command_feed_the_conventions(self):
        p = self.product({'main': 'trunk', 'stage_limits': {'spec': 4},
                          'ci': {'test_command': 'make test'}})
        self.assertEqual(p.conventions.main, 'trunk')
        self.assertEqual(p.conventions.stage_limits, {'spec': 4})
        self.assertEqual(p.conventions.test_command, 'make test')
        # a conventions: key of the same name wins over the top-level one
        p2 = self.product({'main': 'trunk', 'conventions': {'main': 'mainline'}})
        self.assertEqual(p2.conventions.main, 'mainline')

    def test_the_mapping_face_the_existing_callers_use(self):
        p = self.product({'conventions': {'preamble_max_lines': 40, 'rules_tail': 'be nice'}})
        self.assertEqual(p.conventions.get('preamble_max_lines'), 40)
        self.assertEqual(p.conventions.get('rules_tail'), 'be nice')     # an extra key survives
        self.assertEqual(p.conventions['specs_dir'], 'docs/specs')
        self.assertEqual((p.conventions or {}).get('prs_per_tick'), 6)

    def test_branch_prefix_keeps_returning_a_prefix_without_its_separator(self):
        # asf.workers.spawn composes `<prefix>/<job>` itself
        p = self.product({'conventions': {'branch_prefixes': {'code': 'feature/'}}})
        self.assertEqual(p.branch_prefix('code'), 'feature')
        self.assertEqual(p.branch_prefix('task'), 'task')

    def test_groom_defaults_to_empty_when_the_product_sets_none(self):
        self.assertEqual(self.product({}).groom, {})

    def test_groom_reads_the_block(self):
        p = self.product({'groom': {'adjudicate_attempts': 3, 'policies': {'close_exact_duplicate': 'off'}}})
        self.assertEqual(p.groom['adjudicate_attempts'], 3)
        self.assertEqual(p.groom['policies']['close_exact_duplicate'], 'off')

    def test_workflow_names_fold_into_conventions(self):
        p = self.product({'ci': {'workflow': 'ci.yml', 'workflows': ['lint.yml'], 'dev_job': 'test'},
                          'deploy_sha': {'workflow': 'deploy-prod.yml'}})
        self.assertEqual(p.conventions.ci_workflow, 'ci.yml')
        self.assertEqual(p.conventions.ci_workflows, ['lint.yml'])
        self.assertEqual(p.conventions.ci_dev_job, 'test')
        self.assertEqual(p.conventions.deploy_workflow, 'deploy-prod.yml')

    def test_a_conventions_key_wins_over_the_fold(self):
        p = self.product({'ci': {'workflow': 'ci.yml', 'workflows': ['lint.yml']},
                          'deploy_sha': {'workflow': 'deploy-prod.yml'},
                          'conventions': {'ci_workflow': 'other.yml', 'ci_workflows': ['other2.yml'],
                                          'deploy_workflow': 'other-deploy.yml'}})
        self.assertEqual(p.conventions.ci_workflow, 'other.yml')
        self.assertEqual(p.conventions.ci_workflows, ['other2.yml'])
        self.assertEqual(p.conventions.deploy_workflow, 'other-deploy.yml')

    def test_ci_none_folds_nothing(self):
        p = self.product({'ci': 'none', 'deploy_sha': 'none'})
        self.assertIsNone(p.conventions.ci_workflow)
        self.assertEqual(p.conventions.ci_workflows, [])
        self.assertIsNone(p.conventions.ci_dev_job)
        self.assertIsNone(p.conventions.deploy_workflow)


class TestProduct(unittest.TestCase):
    def test_load_product_from_tmp_home(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(_dedent("""
                repo_slug: acme/sample
                repo_dir: /tmp/sample
                main: main
                conventions:
                  branch_prefixes:
                    task: task
                """))
            old = env.ASF_HOME
            env.ASF_HOME = home
            try:
                product = env.load_product('sample')
                self.assertEqual(product.repo_slug, 'acme/sample')
                self.assertEqual(product.branch_prefix('task'), 'task')
                self.assertEqual(product.branch_prefix('spec'), 'spec')
            finally:
                env.ASF_HOME = old


class TestProductSchema(unittest.TestCase):
    def _load(self, body):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(_dedent(body))
            old = env.ASF_HOME
            env.ASF_HOME = home
            try:
                return env.load_product('sample')
            finally:
                env.ASF_HOME = old

    def test_product_file_that_does_not_match_the_schema_is_refused_with_key_and_line(self):
        body = """
        product: sample
        repo:
          dir: /tmp/sample
          slug: acme/sample
        backlog:
          dir: /tmp/backlog
        stage_limits: TODO
        """
        with self.assertRaises(env.ConfigError) as cm:
            self._load(body)
        msg = str(cm.exception)
        self.assertIn("'stage_limits'", msg)
        self.assertIn('line 7', msg)
        # the unknown keys are warnings: they never refuse the load, so the message is the error
        self.assertNotIn("'repo'", msg)
        self.assertNotIn("'backlog'", msg)

    def test_nested_ci_keys_are_checked_too(self):
        p = self._load("""
            repo_slug: a/b
            ci:
              provider: gh-actions
              runner_labels: [x]
            """)
        self.assertEqual(p.warnings, ((4, 'ci.runner_labels', env.UNKNOWN_KEY),))

    def test_groom_must_be_a_map(self):
        with self.assertRaises(env.ConfigError) as cm:
            self._load("""
            repo_slug: a/b
            groom: TODO
            """)
        self.assertIn("'groom'", str(cm.exception))

    def test_an_undeclared_key_inside_the_groom_block_is_not_itself_checked_but_its_shape_is(self):
        # `groom:` is one field of the product file (a map); what an operator puts inside it is
        # this card's own reader's business (asf.groom.policy), not the schema's — the schema
        # only refuses a top-level key it does not declare.
        p = self._load("""
            repo_slug: a/b
            groom:
              made_up_key: 1
            """)
        self.assertEqual(p.groom['made_up_key'], 1)

    def test_an_undeclared_top_level_key_is_a_warning_next_to_a_valid_groom_block(self):
        p = self._load("""
            repo_slug: a/b
            groom:
              adjudicate_attempts: 3
            bogus_top_level_key: 1
            """)
        self.assertEqual(p.warnings, ((4, 'bogus_top_level_key', env.UNKNOWN_KEY),))
        self.assertEqual(p.groom['adjudicate_attempts'], 3)

    def test_the_documented_example_validates(self):
        self.assertEqual(env.validate_product_text(open(os.path.join(
            os.path.dirname(__file__), '..', 'docs', 'products.example.yaml')).read()), [])


class CiTargetsFieldTests(unittest.TestCase):
    """T-0216 T1: ``ci.provider`` and ``ci.targets`` fold into the conventions (F-0043 §2.2)."""

    def test_provider_and_targets_fold_in_and_the_unnamed_keys_keep_their_defaults(self):
        p = env.Product('sample', {'ci': {'provider': 'gh-actions',
                                           'targets': {'cancelled_pct': 8, 'min_runs': 5}}})
        self.assertEqual(p.conventions.ci_provider, 'gh-actions')
        self.assertEqual(p.conventions.ci_cancelled_target_pct, 8)
        self.assertEqual(p.conventions.ci_min_runs, 5)
        self.assertEqual((p.conventions.ci_red_target_pct, p.conventions.ci_min_minutes), (15, 60))

    def test_a_conventions_key_of_the_same_name_still_wins(self):
        p = env.Product('sample', {'ci': {'provider': 'gh-actions',
                                           'targets': {'cancelled_pct': 8}},
                                   'conventions': {'ci_provider': 'other',
                                                   'ci_cancelled_target_pct': 1}})
        self.assertEqual(p.conventions.ci_provider, 'other')
        self.assertEqual(p.conventions.ci_cancelled_target_pct, 1)

    def test_validate_product_text_accepts_targets_as_a_map(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            ci:
              targets:
                cancelled_pct: 8
                min_runs: 5
            """))
        self.assertEqual(problems, [])

    def test_validate_product_text_rejects_a_misshapen_targets(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            ci:
              targets: 3
            """))
        self.assertIn((3, 'ci.targets', 'must be a map, not 3'), problems)

    def test_validate_product_text_still_rejects_an_unknown_ci_key(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            ci:
              made_up_key: 1
            """))
        self.assertIn((3, 'ci.made_up_key', env.UNKNOWN_KEY), problems)

    def test_provider_none_is_the_literal_string_once_loaded(self):
        # P6: `ci.provider: none` (a product without CI) loads as the four-character string,
        # not a parsed falsy value — the guard a provider-label reader must apply is its own.
        data = env.loads(_dedent("""
            repo_slug: a/b
            ci:
              provider: none
            """))
        self.assertEqual(data['ci']['provider'], 'none')


class ProductValidation(unittest.TestCase):
    def test_capacity_is_a_declared_field(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            capacity:
              sessions: 3
              ci: 2
              batch:
                per_run: 8
                parallel: 2
                runners: 4
            """))
        self.assertEqual(problems, [])

    def test_token_caps_is_accepted(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            token_caps:
              default:
                input: 8000000
              spec:
                cache_read: off
            """))
        self.assertEqual(problems, [])

    def test_an_unknown_capacity_key_is_reported_with_its_line(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            capacity:
              sessions: 3
              made_up_key: 1
            """))
        self.assertIn((4, 'capacity.made_up_key', 'is not a field of the product file'), problems)

    def test_capacity_batch_must_be_a_map(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            capacity:
              batch: TODO
            """))
        self.assertIn((3, 'capacity.batch', "must be a map, not 'TODO'"), problems)

    def test_an_improve_block_validates_and_an_unknown_key_under_it_is_reported(self):
        well_formed = _dedent("""
            repo_slug: a/b
            improve:
              thresholds:
                non_landing_sessions: 0.30
              epic: E-0001
              window_days: null
              premium_models: [claude-opus-5]
            """)
        self.assertEqual(env.validate_product_text(well_formed), [])
        problems = env.validate_product_text(well_formed + '\n  made_up_key: 1\n')
        self.assertEqual(problems, [(8, 'improve.made_up_key', 'is not a field of the product file')])
        self.assertEqual(env.Product('p', env.loads(well_formed)).improve['epic'], 'E-0001')


class ProductFieldTests(unittest.TestCase):
    def test_credentials_is_a_field_and_must_be_a_list_of_names(self):
        body = _dedent("""
            repo_slug: a/b
            credentials: [a, b]
            """)
        self.assertEqual(env.validate_product_text(body), [])
        product = env.Product('p', env.loads(body))
        self.assertEqual(product.credentials, ['a', 'b'])

        not_a_list = env.validate_product_text(_dedent("""
            repo_slug: a/b
            credentials:
              a: b
            """))
        self.assertTrue(not_a_list)
        self.assertTrue(all(key == 'credentials' for _line, key, _why in not_a_list))

        duplicate = env.validate_product_text(_dedent("""
            repo_slug: a/b
            credentials: [a, a]
            """))
        self.assertTrue(duplicate)
        self.assertTrue(all(key == 'credentials' for _line, key, _why in duplicate))


class DeployProviderTests(unittest.TestCase):
    """T-0232: ``deploy_sha.provider`` gets a validator row — documented and unread until now
    (docs/products.example.yaml:237)."""

    def test_every_documented_provider_and_none_validate(self):
        for provider in ('github-deployments', 'fly', 'vercel', 'script', 'none'):
            problems = env.validate_product_text(_dedent(f"""
                repo_slug: a/b
                deploy_sha:
                  provider: {provider}
                """))
            self.assertEqual(problems, [], provider)

    def test_a_typo_is_refused(self):
        problems = env.validate_product_text(_dedent("""
            repo_slug: a/b
            deploy_sha:
              provider: heroku
            """))
        self.assertIn((2, 'deploy_sha.provider',
                      "must be one of github-deployments | fly | vercel | script | none, "
                      "not 'heroku'"), problems)


class UpgradeKeyTests(unittest.TestCase):
    """F-0112/T-0431: the ``conventions.flags.upgrade`` switch — three words, a default, and a
    typo that loads rather than refusing the whole product. A flag (not a top-level
    PRODUCT_FIELDS key, PR #675): see PinnedReader below for why."""

    def test_upgrade_auto_validates(self):
        body = _dedent("""
            repo_slug: a/b
            conventions:
              flags:
                upgrade: auto
            """)
        self.assertEqual(env.validate_product_text(body), [])
        product = env.Product('p', env.loads(body))
        self.assertEqual(product.upgrade, 'auto')

    def test_no_upgrade_key_reads_notify(self):
        product = env.Product('p', env.loads(_dedent("""
            repo_slug: a/b
            """)))
        self.assertEqual(product.upgrade, 'notify')
        self.assertIsNone(product.upgrade_declared)

    def test_empty_upgrade_value_reads_notify(self):
        product = env.Product('p', env.loads(_dedent("""
            repo_slug: a/b
            conventions:
              flags:
                upgrade:
            """)))
        self.assertEqual(product.upgrade, 'notify')

    def test_documented_example_names_the_key_and_still_validates(self):
        text = open(os.path.join(REPO_ROOT, 'docs', 'products.example.yaml'), encoding='utf-8').read()
        self.assertIn('upgrade:', text)
        self.assertEqual(env.validate_product_text(text), [])


def _dedent(text):
    lines = [l for l in text.splitlines() if l.strip() != '']
    if not lines:
        return text
    indent = min(len(l) - len(l.lstrip(' ')) for l in lines)
    return '\n'.join(l[indent:] for l in lines)


if __name__ == '__main__':
    unittest.main()


class ProductOfCwdTests(unittest.TestCase):
    """A bare ``asf status`` from a product's checkout resolves that product, not the
    operator's ``default_product`` (a product repo read the default product's record)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(root, 'home')
        os.makedirs(os.path.join(self.home, 'products'))
        self.asf_repo = os.path.join(root, 'ASF')
        self.asf_record = os.path.join(root, 'ASF-backlog')   # a sibling sharing the prefix
        self.bot_repo = os.path.join(root, 'bot', 'bot')
        self.bot_record = os.path.join(root, 'bot', 'backlog')
        for d in (self.asf_repo, self.asf_record, os.path.join(self.bot_repo, 'apps', 'web'), self.bot_record):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(self.home, 'config.yaml'), 'w') as f:
            f.write('default_product: asf\n')
        for name, repo, rec in (('asf', self.asf_repo, self.asf_record), ('bot', self.bot_repo, self.bot_record)):
            with open(os.path.join(self.home, 'products', f'{name}.yaml'), 'w') as f:
                f.write(f'product: {name}\nrepo_dir: {repo}\nbacklog_dir: {rec}\n')
        with open(os.path.join(self.home, 'products', 'broken.yaml'), 'w') as f:
            f.write(': : [\n')
        self.old_home, env.ASF_HOME = env.ASF_HOME, self.home
        self.old_env = os.environ.pop('ASF_PRODUCT', None)
        self.old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.old_cwd)
        env.ASF_HOME = self.old_home
        if self.old_env is not None:
            os.environ['ASF_PRODUCT'] = self.old_env
        self.tmp.cleanup()

    def test_cwd_inside_a_product_repo_resolves_that_product(self):
        os.chdir(os.path.join(self.bot_repo, 'apps', 'web'))
        self.assertEqual(env.default_product_name(), 'bot')
        os.chdir(self.bot_record)
        self.assertEqual(env.default_product_name(), 'bot')

    def test_prefix_sibling_is_not_inside(self):
        os.chdir(self.asf_record)
        self.assertEqual(env.product_of_dir(), 'asf')
        self.assertIsNone(env.product_of_dir(self.asf_repo + '-other'))

    def test_outside_every_product_falls_back_to_default(self):
        os.chdir(self.tmp.name)
        self.assertIsNone(env.product_of_dir())
        self.assertEqual(env.default_product_name(), 'asf')

    def test_env_var_still_wins(self):
        os.chdir(self.bot_repo)
        os.environ['ASF_PRODUCT'] = 'asf'
        try:
            self.assertEqual(env.default_product_name(), 'asf')
        finally:
            del os.environ['ASF_PRODUCT']

    def test_nested_product_resolves_to_the_inner_one(self):
        nested_repo = os.path.join(self.asf_repo, 'vendor', 'bot')
        os.makedirs(nested_repo)
        with open(os.path.join(self.home, 'products', 'nested.yaml'), 'w') as f:
            f.write('product: nested\nrepo_dir: %s\n' % nested_repo)
        self.assertEqual(env.product_of_dir(nested_repo), 'nested')
        self.assertEqual(env.product_of_dir(self.asf_repo), 'asf')

    def test_root_spelled_in_another_case_still_matches(self):
        probe = os.path.join(self.tmp.name, 'CaseProbe')
        os.mkdir(probe)
        if not os.path.isdir(os.path.join(self.tmp.name, 'caseprobe')):
            self.skipTest('needs a case-insensitive filesystem')
        cased_root = os.path.join(self.tmp.name, 'Cased', 'Repo')
        os.makedirs(cased_root)
        with open(os.path.join(self.home, 'products', 'cased.yaml'), 'w') as f:
            f.write('product: cased\nrepo_dir: %s\n' % cased_root)
        other_case = os.path.join(self.tmp.name, 'cased', 'repo')
        self.assertNotEqual(
            os.path.commonpath([os.path.realpath(other_case), cased_root]), cased_root
        )
        self.assertEqual(env.product_of_dir(other_case), 'cased')

    def test_root_that_does_not_exist_is_skipped_not_raised(self):
        with open(os.path.join(self.home, 'products', 'ghost.yaml'), 'w') as f:
            f.write('product: ghost\nrepo_dir: %s\n' % os.path.join(self.tmp.name, 'nowhere'))
        self.assertEqual(env.product_of_dir(self.bot_repo), 'bot')

    def test_state_dir_root_resolves_its_product(self):
        state_dir = os.path.join(self.home, 'state', 'bot')
        os.makedirs(state_dir)
        self.assertEqual(env.product_of_dir(state_dir), 'bot')


class ProductResolutionOrderTests(unittest.TestCase):
    """T-0422: one function walks the order and names the input that chose the product."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(root, 'home')
        os.makedirs(os.path.join(self.home, 'products'))
        self.asf_repo = os.path.join(root, 'ASF')
        self.bot_repo = os.path.join(root, 'bot')
        for d in (self.asf_repo, self.bot_repo):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(self.home, 'config.yaml'), 'w') as f:
            f.write('default_product: asf\n')
        for name, repo in (('asf', self.asf_repo), ('bot', self.bot_repo)):
            with open(os.path.join(self.home, 'products', f'{name}.yaml'), 'w') as f:
                f.write(f'product: {name}\nrepo_dir: {repo}\n')
        self.old_home, env.ASF_HOME = env.ASF_HOME, self.home
        self.old_env = os.environ.pop('ASF_PRODUCT', None)
        self.old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.old_cwd)
        env.ASF_HOME = self.old_home
        os.environ.pop('ASF_PRODUCT', None)
        if self.old_env is not None:
            os.environ['ASF_PRODUCT'] = self.old_env
        self.tmp.cleanup()

    def test_explicit_wins_over_everything(self):
        os.chdir(self.asf_repo)
        os.environ['ASF_PRODUCT'] = 'asf'
        self.assertEqual(env.resolve_product('bot'), env.Resolution('bot', '--product'))

    def test_env_var_wins_over_cwd_and_default(self):
        os.chdir(self.bot_repo)
        os.environ['ASF_PRODUCT'] = 'asf'
        self.assertEqual(env.resolve_product(), env.Resolution('asf', '$ASF_PRODUCT'))

    def test_cwd_wins_over_default(self):
        os.chdir(self.bot_repo)
        self.assertEqual(env.resolve_product(), env.Resolution('bot', 'cwd'))

    def test_default_product_is_the_last_resort(self):
        os.chdir(self.tmp.name)
        self.assertEqual(env.resolve_product(), env.Resolution('asf', 'default_product'))

    def test_none_answering_names_all_four(self):
        with open(os.path.join(self.home, 'config.yaml'), 'w') as f:
            f.write('worker_pool: 1\n')
        os.chdir(self.tmp.name)
        with self.assertRaises(env.ConfigError) as ctx:
            env.resolve_product()
        message = str(ctx.exception)
        for token in ('--product', '$ASF_PRODUCT', self.tmp.name, 'default_product'):
            self.assertIn(token, message)

    def test_default_product_name_matches_resolve_product_name(self):
        scenarios = [
            (self.asf_repo, {'ASF_PRODUCT': 'asf'}),
            (self.bot_repo, {'ASF_PRODUCT': 'asf'}),
            (self.bot_repo, {}),
            (self.tmp.name, {}),
        ]
        for cwd, extra_env in scenarios:
            os.chdir(cwd)
            os.environ.pop('ASF_PRODUCT', None)
            os.environ.update(extra_env)
            self.assertEqual(env.default_product_name(), env.resolve_product().name)


class ConfigTolerance(unittest.TestCase):
    """An unknown key is a warning, never a refusal; a wrong shape still refuses the load."""

    def _load(self, body):
        return TestProductSchema._load(self, body)

    def test_an_unknown_top_level_key_is_a_warning_and_the_file_loads(self):
        text = _dedent("""
            product: sample
            repo_slug: a/b
            install: {sha: abc1234}
            """)
        errors, warnings = env.product_problems(text)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [(3, 'install', env.UNKNOWN_KEY)])
        p = self._load(text)
        self.assertEqual(p.repo_slug, 'a/b')
        self.assertEqual(p.warnings, ((3, 'install', env.UNKNOWN_KEY),))

    def test_an_unknown_key_in_a_checked_section_is_a_warning(self):
        errors, warnings = env.product_problems('repo_slug: a/b\nfeeder:\n  x: 1\n')
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [(3, 'feeder.x', env.UNKNOWN_KEY)])
        self.assertEqual(self._load('repo_slug: a/b\nfeeder:\n  x: 1\n').warnings,
                         ((3, 'feeder.x', env.UNKNOWN_KEY),))

    def test_a_type_error_still_raises_beside_a_warning(self):
        text = 'repo_slug: a/b\nmade_up: 1\nfeeder:\n  hold: features\n'
        errors, warnings = env.product_problems(text)
        self.assertEqual([k for _l, k, _w in errors], ['feeder.hold'])
        self.assertEqual([k for _l, k, _w in warnings], ['made_up'])
        with self.assertRaises(env.ConfigError) as cm:
            self._load(text)
        self.assertIn("'feeder.hold'", str(cm.exception))
        self.assertNotIn("'made_up'", str(cm.exception))

    def test_validate_product_text_still_reports_every_finding(self):
        # the old contract, unchanged: every reader (and a pinned one) compares like with like
        text = 'repo_slug: a/b\nmade_up: 1\nfeeder:\n  hold: features\n'
        self.assertEqual(sorted(k for _l, k, _w in env.validate_product_text(text)),
                         ['feeder.hold', 'made_up'])

    def test_a_well_formed_file_has_no_warnings(self):
        self.assertEqual(self._load('repo_slug: a/b\n').warnings, ())
        self.assertEqual(env.Product('p', {}).warnings, ())


class ProductFlags(unittest.TestCase):
    def test_flag_default_when_unset(self):
        p = env.Product('p', {'repo_slug': 'a/b'})
        self.assertIsNone(p.flag('mechanical'))
        self.assertEqual(p.flag('plan_ahead', 0), 0)
        self.assertEqual(p.conventions.flags, {})

    def test_flag_override(self):
        p = env.Product('p', env.loads(_dedent("""
            conventions:
              flags:
                mechanical: true
                plan_ahead: 2
                verdict_block: warn
            """)))
        self.assertIs(p.flag('mechanical', False), True)
        self.assertEqual(p.flag('plan_ahead', 0), 2)
        self.assertEqual(p.flag('verdict_block', 'off'), 'warn')
        self.assertEqual(p.flag('roots', 'off'), 'off')

    def test_a_dotted_flag_reads_written_dotted_or_nested(self):
        dotted = env.Product('p', {'conventions': {'flags': {'models.cheap_kinds': ['review']}}})
        nested = env.Product('p', {'conventions': {'flags': {'models': {'cheap_kinds': ['review']}}}})
        self.assertEqual(dotted.flag('models.cheap_kinds'), ['review'])
        self.assertEqual(nested.flag('models.cheap_kinds'), ['review'])
        self.assertEqual(nested.conventions.unknown_flags(), [])

    def test_flags_that_are_not_a_map_read_as_defaults(self):
        p = env.Product('p', {'conventions': {'flags': 'on'}})
        self.assertEqual(p.flag('mechanical', False), False)
        self.assertEqual(env.product_problems('conventions:\n  flags: on\n'), ([], []))

    def test_unknown_flag_names_are_listed(self):
        p = env.Product('p', {'conventions': {'flags': {'mechanical': True, 'mechanicl': True,
                                                        'models': {'cheap_kinds': [], 'x': 1}}}})
        self.assertEqual(p.conventions.unknown_flags(), ['mechanicl', 'models.x'])

    def test_flags_are_a_passthrough_not_a_field(self):
        conv = Conventions.from_mapping({'flags': {'i14': 'report'}})
        self.assertNotIn('flags', Conventions.field_names())
        self.assertEqual(conv.get('flags'), {'i14': 'report'})
        from asf import conventions as conventions_mod
        self.assertEqual(conventions_mod.validate_mapping({'flags': {'anything': [1, {}]}}), [])


#: Every flag this program plans, set under ``conventions.flags`` — the product file a newer asf
#: may write while a product's venv is still pinned to an older sha.
_PLANNED_FLAG_VALUES = {
    'mechanical': 'true', 'verdict_block': 'warn', 'plan_ahead': '2', 'roots': 'true',
    'i14': 'report', 'i16': 'report', 'facts': 'shadow', 'refguard': 'warn',
    'console_wait': 'false', 'groom_rules': 'true', 'relaunch_cap': '3', 'loop_cap': '4',
    'head_capped': 'true', 'unknown_holds': 'true', 'models.cheap_kinds': '[review, groom]',
    'roots_unverified_hours': '24', 'roots_park_stale_days': '3', 'roots_min_dependants': '5',
    'queue_store': 'on', 'store_shared': 'on', 'docs_review': 'skip', 'id_claim': 'push',
    'correction_rebase_behind': '300', 'stale_ref_reopens': '1', 'stale_ref_job_paths': '{}',
    'flake_skip_reproduced': 'on', 'trunk_red_trunk_runs': 'on',
    'relaunch_same_report': '2', 'relaunch_daily_cap': '6',
    'groom.reask_days': '7', 'groom.rank_owner': 'code', 'groom.structural': 'report',
    'health_opens_pr': 'true', 'push_auth_preflight': 'true',
    'operator_readonly_verbs': '[terraform plan]', 'upgrade': 'notify',
    'rule_pass': 'on',
}
PLANNED_PRODUCT = _dedent("""
    product: sample
    repo_slug: acme/sample
    repo_dir: /tmp/sample
    main: main
    conventions:
      specs_dir: docs/specs
      flags:
    """) + '\n' + '\n'.join(f'    {k}: {v}' for k, v in _PLANNED_FLAG_VALUES.items()) + '\n'

#: The product-file schema of the oldest pinned reader (``tools/pinned-readers.txt``),
#: replicated so the check runs with no git at all: its top-level fields, the keys of each
#: section it checks, and the ``conventions:`` keys it validates (everything else under
#: ``conventions:`` it keeps verbatim). A key outside these sets strands a product pinned there.
PINNED_SHA = '8d5e25a62d954a5ac46b46ec496330c01de2759b'
PINNED_PRODUCT_FIELDS = frozenset({
    'product', 'repo_slug', 'repo_dir', 'main', 'backlog_dir', 'app_host', 'conventions', 'ci',
    'deploy_sha', 'customer_paths', 'stage_limits', 'size_classes', 'approvals',
    'approval_signals', 'steps', 'job_grants', 'groom', 'capacity', 'clocks', 'token_caps',
    'feeder', 'improve', 'release', 'cloud', 'credentials'})
PINNED_NESTED_FIELDS = {
    'ci': frozenset({'provider', 'workflow', 'test_command', 'budgets', 'runner_org', 'labels',
                     'dev_job', 'deploy_workflow', 'pool', 'queue', 'reserve', 'flake_triage',
                     'reference_class', 'quarantine_days'}),
    'capacity': frozenset({'sessions', 'ci', 'weight', 'batch'}),
    'feeder': frozenset({'hold'}),
    'improve': frozenset({'thresholds', 'epic', 'window_days', 'premium_models', 'scorecard'}),
    'release': frozenset({'window_days', 'max_hand_fixes', 'max_repair_per_feature', 'ci_runs',
                          'min_upgrades', 'hand_types', 'readme_sections', 'ci_steps',
                          'requires', 'blocking'}),
}
#: Nested-section keys the live schema carries that PINNED_SHA does not: each is a checked
#: field its own loader never declared, so it is UNKNOWN_KEY there too — a warning on that
#: pinned product's `ci` row, never a refusal (verified against the pinned reader itself,
#: T-0216). A product pinned to PINNED_SHA still loads a file carrying one; it just does not
#: get the new behaviour until it upgrades. Grows only with a key proven to warn, not refuse.
PINNED_WARNS_NOT_REFUSES = {
    'ci': frozenset({'targets'}),  # F-0043 §2.2: ci.targets, folded by Product.conventions
}
PINNED_VALIDATED_CONVENTIONS = frozenset({
    'doc_paths', 'shared_paths', 'shared_writes', 'pre_push_check', 'worktree_setup',
    'auth_env', 'full_suite_commands', 'customer_content', 'security', 'feeder', 'lane',
    'branch_retention'})

_PINNED_LOAD = r"""
import json, os, sys
from asf import env
text = sys.stdin.read()
os.makedirs(os.path.join(env.ASF_HOME, 'products'))
with open(os.path.join(env.ASF_HOME, 'products', 'sample.yaml'), 'w') as f:
    f.write(text)
p = env.load_product('sample')
print(json.dumps({'problems': env.validate_product_text(text), 'repo_slug': p.repo_slug,
                  'flags': p.conventions.get('flags')}))
"""


class PinnedReader(unittest.TestCase):
    """A product file this code writes must load under the oldest pinned reader."""

    def test_product_file_with_every_planned_key_loads(self):
        from asf import conventions as conventions_mod
        self.assertEqual(set(_PLANNED_FLAG_VALUES), set(conventions_mod.KNOWN_FLAGS))
        # under this code: no error, no warning, every flag read back
        self.assertEqual(env.product_problems(PLANNED_PRODUCT), ([], []))
        p = TestProductSchema._load(self, PLANNED_PRODUCT)
        self.assertEqual(p.warnings, ())
        self.assertEqual(p.flag('plan_ahead'), 2)
        self.assertEqual(p.flag('models.cheap_kinds'), ['review', 'groom'])
        self.assertEqual(p.conventions.unknown_flags(), [])
        # under the replicated pinned schema (no git needed)
        data = env.loads(PLANNED_PRODUCT)
        self.assertLessEqual(set(data), PINNED_PRODUCT_FIELDS)
        self.assertNotIn('flags', PINNED_VALIDATED_CONVENTIONS)
        # under the pinned sha's own loader, in its own process
        from tests import pinned
        self.assertIn(PINNED_SHA, pinned.pinned_shas())
        for sha in pinned.pinned_shas():
            with self.subTest(sha=sha):
                proc = pinned.run_pinned(sha, _PINNED_LOAD, PLANNED_PRODUCT)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                self.assertEqual(out['problems'], [])
                self.assertEqual(out['repo_slug'], 'acme/sample')
                self.assertEqual(out['flags']['plan_ahead'], 2)

    def test_the_replicated_schema_matches_the_pinned_loader(self):
        # the replica above is what runs when git is not there; prove it against the real one
        from tests import pinned
        code = ("import json; from asf import env; print(json.dumps({'top': sorted(env.PRODUCT_FIELDS),"
                " 'nested': {k: sorted(v) for k, v in env.NESTED_FIELDS.items()}}))")
        proc = pinned.run_pinned(PINNED_SHA, code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(set(out['top']), PINNED_PRODUCT_FIELDS)
        self.assertEqual({k: set(v) for k, v in out['nested'].items()},
                         {k: set(v) for k, v in PINNED_NESTED_FIELDS.items()})

    def test_no_product_key_the_pinned_reader_would_refuse(self):
        # A new top-level or checked-section key strands a product pinned to PINNED_SHA (its
        # loader refuses it) unless it is in PINNED_WARNS_NOT_REFUSES — proven there to be a
        # warning, not a refusal. Put a new switch under conventions.flags instead; drop the sha
        # from tools/pinned-readers.txt only once no product runs it.
        from tests import pinned
        if PINNED_SHA not in pinned.pinned_shas():
            self.skipTest(f'{PINNED_SHA} is no longer a pinned reader')
        self.assertLessEqual(set(env.PRODUCT_FIELDS), PINNED_PRODUCT_FIELDS)
        for section, fields in env.NESTED_FIELDS.items():
            allowed = (PINNED_NESTED_FIELDS.get(section, frozenset())
                      | PINNED_WARNS_NOT_REFUSES.get(section, frozenset()))
            self.assertLessEqual(set(fields), allowed, section)

    def test_ci_targets_only_warns_at_the_pinned_reader(self):
        # proves the PINNED_WARNS_NOT_REFUSES entry above: a file carrying ci.targets still
        # loads clean under PINNED_SHA — an unknown-key warning, never a refusal (T-0216).
        from tests import pinned
        if PINNED_SHA not in pinned.pinned_shas():
            self.skipTest(f'{PINNED_SHA} is no longer a pinned reader')
        code = ("import json, sys; from asf import env\n"
                "print(json.dumps(env.validate_product_text(sys.stdin.read())))")
        product = _dedent("""
            product: sample
            repo_slug: acme/sample
            repo_dir: /tmp/sample
            main: main
            ci:
              provider: gh-actions
              targets:
                cancelled_pct: 8
            """)
        proc = pinned.run_pinned(PINNED_SHA, code, product)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        problems = json.loads(proc.stdout)
        self.assertEqual(problems, [[7, 'ci.targets', env.UNKNOWN_KEY]])
