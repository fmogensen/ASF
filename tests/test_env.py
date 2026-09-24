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
        p = self.product({'ci': {'workflow': 'ci.yml', 'dev_job': 'test'},
                          'deploy_sha': {'workflow': 'deploy-prod.yml'}})
        self.assertEqual(p.conventions.ci_workflow, 'ci.yml')
        self.assertEqual(p.conventions.ci_dev_job, 'test')
        self.assertEqual(p.conventions.deploy_workflow, 'deploy-prod.yml')

    def test_a_conventions_key_wins_over_the_fold(self):
        p = self.product({'ci': {'workflow': 'ci.yml'},
                          'deploy_sha': {'workflow': 'deploy-prod.yml'},
                          'conventions': {'ci_workflow': 'other.yml',
                                          'deploy_workflow': 'other-deploy.yml'}})
        self.assertEqual(p.conventions.ci_workflow, 'other.yml')
        self.assertEqual(p.conventions.deploy_workflow, 'other-deploy.yml')

    def test_ci_none_folds_nothing(self):
        p = self.product({'ci': 'none', 'deploy_sha': 'none'})
        self.assertIsNone(p.conventions.ci_workflow)
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
        self.assertIn("'repo'", msg)
        self.assertIn('line 2', msg)
        self.assertIn("'backlog'", msg)
        self.assertIn("'stage_limits'", msg)

    def test_nested_ci_keys_are_checked_too(self):
        with self.assertRaises(env.ConfigError) as cm:
            self._load("""
            repo_slug: a/b
            ci:
              provider: gh-actions
              runner_labels: [x]
            """)
        self.assertIn("'ci.runner_labels'", str(cm.exception))
        self.assertIn('line 4', str(cm.exception))

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

    def test_an_undeclared_top_level_key_still_fails_next_to_a_valid_groom_block(self):
        with self.assertRaises(env.ConfigError) as cm:
            self._load("""
            repo_slug: a/b
            groom:
              adjudicate_attempts: 3
            bogus_top_level_key: 1
            """)
        self.assertIn("'bogus_top_level_key'", str(cm.exception))

    def test_the_documented_example_validates(self):
        self.assertEqual(env.validate_product_text(open(os.path.join(
            os.path.dirname(__file__), '..', 'docs', 'products.example.yaml')).read()), [])


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


def _dedent(text):
    lines = [l for l in text.splitlines() if l.strip() != '']
    if not lines:
        return text
    indent = min(len(l) - len(l.lstrip(' ')) for l in lines)
    return '\n'.join(l[indent:] for l in lines)


if __name__ == '__main__':
    unittest.main()
