"""Customer content (:mod:`asf.customer_content`): a page a customer reads never carries an
internal note. The landing gate refuses a branch that adds a forbidden marker under
``customer_content.paths`` (file:line in the hold), the deploy pass refuses to dispatch a
candidate whose tree carries one, the review of such a diff owes a ``read as the customer`` row,
and the doctor names a product that deploys with no customer content configured."""
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import conventions as conv_mod
from asf import customer_content as cc
from asf.harvest import deploy, lane

CONV = {'customer_content': {'paths': ['site/legal/**', 'site/contact.md']},
        'ci_workflow': 'ci.yml'}


def _git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w') as fh:
        fh.write(text)


class Repo(unittest.TestCase):
    """A repo whose ``origin/main`` holds clean legal pages and whose ``origin/worker/T-0001``
    adds lines to them."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='cc-')
        self.addCleanup(shutil.rmtree, self.repo, True)
        _git(self.repo, 'init', '-q', '-b', 'main')
        _git(self.repo, 'config', 'user.email', 't@example.com')
        _git(self.repo, 'config', 'user.name', 't')
        _git(self.repo, 'config', 'core.hooksPath', '/dev/null')
        _write(self.repo, 'site/legal/terms.md', '# Terms\n\nYou may cancel at any time.\n')
        _write(self.repo, 'site/contact.md', 'Write to us.\n')
        _write(self.repo, 'app/main.py', 'x = 1\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'T-0001 base')
        self.base = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/main', self.base)

    def branch(self, files):
        _git(self.repo, 'checkout', '-q', '-B', 'worker/T-0001', self.base)
        for path, text in files.items():
            _write(self.repo, path, text)
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'T-0001 pages')
        head = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/worker/T-0001', head)
        return head


class MarkerGate(Repo):
    def test_a_branch_adding_a_legal_note_to_a_customer_page_is_refused_with_file_line(self):
        self.branch({'site/legal/terms.md': '# Terms\n\nYou may cancel at any time.\n'
                                            '[legal: check the notice period with counsel]\n',
                     'app/main.py': 'x = 1  # TODO later\n'})
        got = lane.lane_refusal(self.repo, 'main', 'worker/T-0001', 'T-0001', CONV)
        self.assertIsNotNone(got)
        kind, text = got
        self.assertEqual(kind, cc.KIND)
        self.assertIn('site/legal/terms.md:4', text)
        self.assertNotIn('app/main.py', text)  # not customer content: never the gate's business

    def test_a_clean_branch_and_a_product_without_customer_content_pass(self):
        self.branch({'site/legal/terms.md': '# Terms\n\nYou may cancel within 30 days.\n'})
        self.assertIsNone(lane.lane_refusal(self.repo, 'main', 'worker/T-0001', 'T-0001', CONV))
        self.branch({'site/legal/terms.md': '[TODO: pricing]\n'})
        self.assertIsNone(lane.lane_refusal(self.repo, 'main', 'worker/T-0001', 'T-0001',
                                            {'ci_workflow': 'ci.yml'}))

    def test_only_added_lines_count_a_marker_already_on_the_trunk_is_the_deploy_checks(self):
        _write(self.repo, 'site/contact.md', 'Write to us. TODO: address\n')
        _git(self.repo, 'commit', '-qam', 'T-0001 old note')
        self.base = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/main', self.base)
        self.branch({'site/legal/terms.md': '# Terms\n\nFinal copy.\n'})
        self.assertEqual(cc.added_hits(self.repo, 'origin/main', 'origin/worker/T-0001', CONV), [])
        self.assertEqual(cc.tree_hits(self.repo, 'origin/main', CONV),
                         [('site/contact.md', 1, 'TODO')])

    def test_the_gate_holds_a_branch_already_past_pushed(self):
        """A branch in GATE before the marker gate existed is refused at the gate too."""
        product = types.SimpleNamespace(conventions=CONV)
        fake = types.SimpleNamespace(product=product, conv=CONV, out=lambda *_: None,
                                     repo=self.repo, trunk='main', dry_run=False,
                                     enter_back=mock.Mock())
        self.branch({'site/contact.md': 'Write to us at {{SUPPORT_EMAIL}}.\n'})
        f = {'branch': 'worker/T-0001', 'item': 'T-0001', 'files': ['site/contact.md']}
        ready = lane.precheck(fake, [f])
        self.assertEqual(ready, [])
        fake.enter_back.assert_called_once_with(f, f'kind={cc.KIND}')
        self.assertIn('site/contact.md:1', f['refusal'][1])


class Markers(unittest.TestCase):
    def test_the_defaults_catch_internal_notes_todos_lorem_and_placeholders(self):
        pats = cc.markers({})
        for bad in ('[legal: confirm with lawyer]', '[Lawyer: rewrite]', '[internal: x]',
                    '[TBD: price]', 'TODO fill in', 'FIXME', 'XXX', 'Lorem ipsum dolor',
                    'Contact {{COMPANY_NAME}}', '[Insert date]', 'Dear <<CUSTOMER NAME>>'):
            self.assertIsNotNone(cc.scan_line(pats, bad), bad)
        for fine in ('Please note: fees apply.', 'your to-do list', 'style={{color: 1}}',
                     'See [our privacy policy](/privacy).', 'Hello {{ user.name }}'):
            self.assertIsNone(cc.scan_line(pats, fine), fine)

    def test_forbidden_markers_replace_the_defaults(self):
        conv = {'customer_content': {'paths': ['x/**'], 'forbidden_markers': ['DRAFT']}}
        self.assertIsNotNone(cc.scan_line(cc.markers(conv), 'DRAFT copy'))
        self.assertIsNone(cc.scan_line(cc.markers(conv), 'TODO'))

    def test_a_bad_regex_is_a_conventions_problem(self):
        probs = conv_mod.validate_mapping({'customer_content': {'paths': ['a/**'],
                                                                'forbidden_markers': ['(']}})
        self.assertEqual(probs[0][0], 'customer_content.forbidden_markers')


class DeployRefusal(unittest.TestCase):
    PROD, GREEN = 'a' * 40, 'b' * 40

    def _sh(self):
        calls = []

        def sh(cmd, cwd=None, timeout=60):
            calls.append(cmd)
            if cmd[:3] == ['gh', 'run', 'list']:
                wf = cmd[cmd.index('--workflow') + 1]
                sha = self.PROD if wf != 'ci.yml' else self.GREEN
                return json.dumps([{'databaseId': 1, 'headSha': sha, 'status': 'completed',
                                    'conclusion': 'success', 'updatedAt': '2026-09-24T21:32:10Z'}])
            if 'rev-parse' in cmd:
                return 'd' * 40
            if 'rev-list' in cmd:
                return '3'
            return ''
        sh.calls = calls
        return sh

    def _product(self, conv, **targets):
        d = {'prod': {'mode': 'auto', 'workflow': 'deploy-prod.yml'}}
        if targets:
            d['targets'] = targets
        return types.SimpleNamespace(repo_slug='o/r', repo_dir='/repo', main='main',
                                     conventions=dict(conv, ci_workflow='ci.yml'), deploy_sha=d)

    def _tick(self, product, hits):
        sh, lines = self._sh(), []
        with mock.patch.object(cc, 'tree_hits', return_value=hits) as scan:
            sent = deploy.tick(product, out=lines.append, sh=sh)
        return sent, lines, [c for c in sh.calls if c[:3] == ['gh', 'workflow', 'run']], scan

    def test_markers_in_the_candidate_tree_refuse_every_dispatch_with_one_loud_line(self):
        product = self._product(CONV, site={'mode': 'auto', 'workflow': 'site.yml'})
        sent, lines, runs, scan = self._tick(product, [('site/legal/terms.md', 4, '[legal:')])
        self.assertEqual((sent, runs), ({}, []))
        refused = [l for l in lines if 'DISPATCH REFUSED' in l]
        self.assertEqual(len(refused), 2)  # prod and the named site target
        self.assertTrue(any(l.startswith('deploy site: DISPATCH REFUSED') for l in refused))
        self.assertIn('site/legal/terms.md:4', refused[0])
        scan.assert_called_with('/repo', self.GREEN, product.conventions)

    def test_a_clean_tree_dispatches_and_an_unreadable_one_does_not(self):
        sent, _, runs, _ = self._tick(self._product(CONV), [])
        self.assertEqual(sent, {'prod': self.GREEN})
        sent, lines, runs, _ = self._tick(self._product(CONV), None)
        self.assertEqual((sent, runs), ({}, []))
        self.assertIn('could not be read', lines[-1])

    def test_no_customer_content_no_scan(self):
        sent, _, _, scan = self._tick(self._product({}), [('x', 1, 'TODO')])
        self.assertEqual(sent, {'prod': self.GREEN})
        scan.assert_not_called()


class Doctor(unittest.TestCase):
    def _product(self, conv):
        return types.SimpleNamespace(
            repo_slug='o/r', repo_dir='/repo', main='main',
            conventions=dict(conv, ci_workflow='ci.yml'),
            deploy_sha={'targets': {'site': {'mode': 'manual', 'workflow': 'site.yml'}}})

    def test_a_deploy_target_without_customer_content_is_a_finding(self):
        rows = cc.findings(self._product({}))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0][0])
        self.assertIn('customer_content', rows[0][1])

    def test_configured_is_green(self):
        rows = cc.findings(self._product(CONV))
        self.assertEqual([ok for ok, _ in rows], [True])

    def test_no_deploy_no_row(self):
        p = types.SimpleNamespace(repo_slug='o/r', repo_dir='/repo', main='main',
                                  conventions={}, deploy_sha={})
        self.assertEqual(cc.findings(p), [])


if __name__ == '__main__':
    unittest.main()
