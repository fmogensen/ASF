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
    if cmd[:2] == ['git', 'log'] and '..origin/main' in cmd[-1]:
        return ''  # prod is at main: nothing merged is missing from it
    if cmd[:2] == ['git', 'log']:
        return 'aaa1\tp0\tfeat: shiny thing (#7)\nbbb2\tp1\tchore: docs only (#8)'
    if cmd[:2] == ['git', 'diff-tree']:
        return 'apps/web/src/page.tsx' if cmd[-1] == 'aaa1' else 'tools/x.py'
    return 'abc123456'


SITE = 'e' * 40


def _gap_sh(cmd, cwd=None, timeout=60):
    """The site lags main: a merge-queue merge of legal pages (named only by #N in its subject)
    is on main and on prod, not on the site."""
    if cmd[:2] == ['git', 'log'] and '--first-parent' in cmd:
        if cmd[-1] == f'{SITE}..origin/main':
            return 'mmm1\tp0 b1\tmerge-queue: #718 (cloud/legal @ b1)\nccc2\tp1\tchore: ci (#9)'
        return ''
    if cmd[:2] == ['git', 'log'] and '--reverse' in cmd:
        return 'feat(legal): the legal pages\nfix: review'
    if cmd[:2] == ['git', 'log']:
        return ''
    if cmd[:2] == ['git', 'diff-tree']:
        return ('apps/site/app/legal/contact/page.tsx\napps/site/lib/x.test.ts'
                if cmd[-1] == 'mmm1' else '.github/workflows/ci.yml')
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


class PerTargetGap(unittest.TestCase):
    def test_a_customer_visible_change_missing_from_a_target_is_listed(self):
        from asf.harvest import deploy
        facts = {'deployed': SITE, 'paths': ['apps/site/**'], 'relevant': 1, 'mode': 'manual',
                 'error': None}
        with mock.patch.object(prod, '_sh', _gap_sh), \
                mock.patch.object(prod, '_deploy_sha', return_value=('aaa1', None)), \
                mock.patch.object(deploy, 'applies', return_value=True), \
                mock.patch.object(deploy, 'states', return_value=[('site', facts)]), \
                mock.patch.object(deploy, 'view_line', return_value='deploy site: MANUAL'):
            out = prod.render(None, _product(['apps/web/**', 'apps/site/**']))
        self.assertIn('**Site** `eeeeeeeee` 1 relevant behind (manual)', out)
        self.assertIn('| site | the legal pages | NOT LIVE — merged, site `eeeeeeeee` lacks it |'
                      ' #718 |', out)
        self.assertNotIn('#9', out)
        self.assertNotIn('no unticked', out)


if __name__ == '__main__':
    unittest.main()
