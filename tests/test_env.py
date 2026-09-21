import os
import tempfile
import unittest

from asf import env


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
