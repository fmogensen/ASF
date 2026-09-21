import os
import tempfile
import unittest

from asf import env
from asf.conventions import Conventions

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSuiteIsolatedFromOperatorHome(unittest.TestCase):
    def test_asf_home_is_a_temp_dir_not_the_operators(self):
        real = os.path.realpath(os.path.expanduser('~/.ASF'))
        home = os.path.realpath(env.ASF_HOME)
        self.assertNotEqual(home, real)
        if not os.environ.get('ASF_TESTS_HOME'):
            self.assertTrue(home.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep))

    def test_default_product_ignores_the_real_home_config(self):
        saved = os.environ.pop('ASF_PRODUCT', None)
        try:
            real_cfg = os.path.join(os.path.expanduser('~/.ASF'), 'config.yaml')
            self.assertNotEqual(os.path.realpath(env.config_path()), os.path.realpath(real_cfg))
            if not os.path.exists(env.config_path()):
                with self.assertRaises(env.ConfigError):
                    env.default_product_name()
        finally:
            if saved is not None:
                os.environ['ASF_PRODUCT'] = saved


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


def _dedent(text):
    lines = [l for l in text.splitlines() if l.strip() != '']
    if not lines:
        return text
    indent = min(len(l) - len(l.lstrip(' ')) for l in lines)
    return '\n'.join(l[indent:] for l in lines)


if __name__ == '__main__':
    unittest.main()
