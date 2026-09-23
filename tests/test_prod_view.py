"""B-0095 — the PROD view reads ``customer_paths`` as the list of globs the schema declares."""
import types
import unittest
from unittest import mock

from asf.views import prod


def _product(paths):
    return types.SimpleNamespace(
        repo_dir='.', main='main', repo_slug='o/r', customer_paths=paths,
        deploy_sha={'prod': {'source': 'vercel'}})


def _sh(cmd, cwd=None, timeout=60):
    if cmd[:2] == ['git', 'log']:
        return 'aaa1 feat: shiny thing (#7)\nbbb2 chore: docs only (#8)'
    if cmd[:2] == ['git', 'show']:
        return (' apps/web/src/page.tsx | 2 +-' if cmd[-1] == 'aaa1'
                else ' tools/x.py | 2 +-')
    return 'abc123456'


class ProdCustomerPathsList(unittest.TestCase):
    def test_prod_render_with_list_customer_paths(self):
        with mock.patch.object(prod, '_sh', _sh), \
                mock.patch.object(prod, '_deploy_sha', return_value=('aaa1', '2026-09-23T10:00:00Z')):
            out = prod.render(None, _product(['apps/web/**', 'apps/site/**']))
        self.assertIn('shiny thing', out)
        self.assertIn('#7', out)
        self.assertNotIn('#8', out)

    def test_empty_list_renders(self):
        with mock.patch.object(prod, '_sh', _sh), \
                mock.patch.object(prod, '_deploy_sha', return_value=('aaa1', None)):
            out = prod.render(None, _product([]))
        self.assertIn('no unticked customer-visible change', out)


if __name__ == '__main__':
    unittest.main()
