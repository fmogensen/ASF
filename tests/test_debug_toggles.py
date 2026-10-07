"""Debug toggles (:mod:`asf.debug_toggles`): a branch never lands a check its session switched
off — a focused or disabled test, a breakpoint, a debug flag left on. The landing gate refuses a
branch that *adds* one, documentation excluded, at both of the marker gate's existing seams, and
one line is made legal by a waiver that carries a reason."""
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import conventions as conv_mod
from asf import customer_content as cc
from asf import debug_toggles as dt
from asf.harvest import lane

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: a conv that declares the three docs roots and a ``doc_paths`` glob — the doc exclusion cannot
#: be tested against a conv that names no docs roots.
CONV = {'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans', 'reviews_dir': 'docs/reviews',
        'doc_paths': ['guide/**']}


def _toggle(*parts):
    """One of the seven default marker fixtures, joined at runtime from pieces no single one of
    which is itself a marker — never one literal, so this file's own compiled ``.pyc`` (the tree
    self-check below walks every file under ``tests/``, that one included) never carries the
    whole thing as one contiguous constant."""
    return ''.join(parts)


def _git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w') as fh:
        fh.write(text)


class Repo(unittest.TestCase):
    """A repo whose ``origin/main`` holds clean code and whose ``origin/worker/T-0001`` adds
    lines to it."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='dt-')
        self.addCleanup(shutil.rmtree, self.repo, True)
        _git(self.repo, 'init', '-q', '-b', 'main')
        _git(self.repo, 'config', 'user.email', 't@example.com')
        _git(self.repo, 'config', 'user.name', 't')
        _git(self.repo, 'config', 'core.hooksPath', '/dev/null')
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
        _git(self.repo, 'commit', '-qm', 'T-0001 toggle')
        head = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/worker/T-0001', head)
        return head


class Markers(unittest.TestCase):
    def test_every_default_marker_catches_its_own_toggle(self):
        pats = dt.markers({})
        self.assertIsNotNone(dt.scan_line(pats, _toggle('debugger', ';')))
        self.assertIsNotNone(dt.scan_line(pats, _toggle('breakpoint', '(', ')')))
        self.assertIsNotNone(dt.scan_line(pats, _toggle('pdb.set_trace', '(')))
        self.assertIsNotNone(dt.scan_line(pats, _toggle('it.only', '(')))
        self.assertIsNotNone(dt.scan_line(pats, _toggle('describe.skip', '(')))
        self.assertIsNotNone(dt.scan_line(pats, _toggle('xit', '(')))
        self.assertIsNotNone(dt.scan_line(pats, _toggle('DEBUG', ' = ', 'True')))

    def test_the_defaults_do_not_match_this_repos_legitimate_code(self):
        pats = dt.markers({})
        safe = ('def fit(sections, limit):', "fit({'kind': 'x'})",
                "@unittest.skipUnless(os.environ.get('X'), 'why')",
                'unittest.expectedFailure(fn)', 'exit(2)', "self.skipTest('why')")
        for ok in safe:
            self.assertIsNone(dt.scan_line(pats, ok), ok)


class Waiver(unittest.TestCase):
    def _marker(self):
        return _toggle('breakpoint', '(', ')')

    def test_a_marker_with_a_reason_is_waived(self):
        allow = dt.waiver({})
        reason = 'debug-ok: pinned while T-0001 bisects the flake'
        self.assertTrue(dt.waived(allow, f'{self._marker()}  # {reason}'))

    def test_a_bare_debug_ok_with_no_reason_still_counts_as_a_hit(self):
        allow = dt.waiver({})
        bare = 'debug-ok:'
        self.assertFalse(dt.waived(allow, f'{self._marker()}  # {bare}'))

    def test_a_product_waiver_that_does_not_compile_waives_nothing(self):
        allow = dt.waiver({'debug_toggles': {'waiver': '('}})
        self.assertIsNone(allow)
        reason = 'debug-ok: because'
        self.assertFalse(dt.waived(allow, f'{self._marker()}  # {reason}'))


class Scope(Repo):
    def test_a_marker_under_a_docs_root_is_not_a_hit_but_the_same_line_under_code_is(self):
        line = _toggle('breakpoint', '(', ')') + '\n'
        self.branch({'docs/specs/f-0001.md': line, 'app/main.py': line})
        hits = dt.added_hits(self.repo, 'origin/main', 'origin/worker/T-0001', CONV)
        self.assertEqual(hits, [('app/main.py', 1, _toggle('breakpoint', '(', ')'))])

    def test_a_doc_paths_glob_also_excludes(self):
        self.branch({'guide/x.md': _toggle('breakpoint', '(', ')') + '\n'})
        self.assertEqual(dt.added_hits(self.repo, 'origin/main', 'origin/worker/T-0001', CONV), [])

    def test_paths_empty_switches_the_gate_off(self):
        conv = dict(CONV, debug_toggles={'paths': []})
        self.branch({'app/main.py': _toggle('breakpoint', '(', ')') + '\n'})
        self.assertEqual(dt.added_hits(self.repo, 'origin/main', 'origin/worker/T-0001', conv), [])


class Added(Repo):
    def test_only_added_lines_count_a_toggle_already_on_the_trunk_is_no_branch_fault(self):
        _write(self.repo, 'app/old.py', _toggle('breakpoint', '(', ')') + '\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'old toggle')
        self.base = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'update-ref', 'refs/remotes/origin/main', self.base)
        self.branch({'app/new.py': 'x = 1\n'})
        self.assertEqual(dt.added_hits(self.repo, 'origin/main', 'origin/worker/T-0001', CONV), [])

    def test_a_toggle_added_twice_in_two_files_is_two_hits_with_the_right_file_line_each(self):
        toggle = _toggle('breakpoint', '(', ')')
        self.branch({'app/a.py': f'x = 1\n{toggle}\n', 'app/b.py': f'{toggle}\n'})
        hits = dt.added_hits(self.repo, 'origin/main', 'origin/worker/T-0001', CONV)
        self.assertEqual(sorted(hits), [('app/a.py', 2, toggle), ('app/b.py', 1, toggle)])


class Gate(Repo):
    def test_lane_refusal_returns_the_debug_toggle_pair_naming_file_line(self):
        toggle = _toggle('breakpoint', '(', ')')
        self.branch({'app/main.py': f'x = 1\n{toggle}\n'})
        got = lane.lane_refusal(self.repo, 'main', 'worker/T-0001', 'T-0001', CONV)
        self.assertIsNotNone(got)
        kind, text = got
        self.assertEqual(kind, dt.KIND)
        self.assertIn('app/main.py:2', text)

    def test_a_customer_content_hit_and_a_debug_toggle_both_return_the_customer_content_pair(self):
        conv = dict(CONV, customer_content={'paths': ['site/**']})
        self.branch({'site/legal.md': '[TODO: x]\n',
                     'app/main.py': _toggle('breakpoint', '(', ')') + '\n'})
        got = lane.lane_refusal(self.repo, 'main', 'worker/T-0001', 'T-0001', conv)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], cc.KIND)

    def test_a_clean_branch_returns_none(self):
        self.branch({'app/main.py': 'x = 2\n'})
        self.assertIsNone(lane.lane_refusal(self.repo, 'main', 'worker/T-0001', 'T-0001', CONV))


class AtTheGate(Repo):
    def test_the_gate_holds_a_branch_already_past_pushed(self):
        """A branch in GATE before the debug-toggle gate existed is refused at the gate too."""
        product = types.SimpleNamespace(conventions=CONV)
        fake = types.SimpleNamespace(product=product, conv=CONV, out=lambda *_: None,
                                     repo=self.repo, trunk='main', dry_run=False,
                                     enter_back=mock.Mock())
        toggle = _toggle('breakpoint', '(', ')')
        self.branch({'app/main.py': f'x = 1\n{toggle}\n'})
        f = {'branch': 'worker/T-0001', 'item': 'T-0001', 'files': ['app/main.py']}
        ready = lane.precheck(fake, [f])
        self.assertEqual(ready, [])
        fake.enter_back.assert_called_once_with(f, f'kind={dt.KIND}')
        self.assertIn('app/main.py:2', f['refusal'][1])


class Conventions(unittest.TestCase):
    def test_a_misshapen_block_is_named(self):
        probs = conv_mod.validate_mapping({'debug_toggles': 'oops'})
        self.assertEqual(probs,
                         [('debug_toggles', "must be a map (paths, markers, waiver), not 'oops'")])

    def test_each_misshapen_field_is_named(self):
        probs = conv_mod.validate_mapping(
            {'debug_toggles': {'paths': 'x', 'markers': ['('], 'waiver': 123}})
        self.assertEqual([k for k, _ in probs],
                         ['debug_toggles.paths', 'debug_toggles.markers', 'debug_toggles.waiver'])

    def test_debug_toggles_is_in_the_docstrings_checked_keys(self):
        self.assertIn('debug_toggles', conv_mod.validate_mapping.__doc__)


class MarkerLiteralFence(unittest.TestCase):
    def test_the_marker_defaults_appear_nowhere_else_under_asf(self):
        hits = []
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, 'asf')):
            for name in files:
                if name.endswith('.py'):
                    path = os.path.join(root, name)
                    with open(path, encoding='utf-8') as fh:
                        text = fh.read()
                    for pattern in conv_mod.DEFAULT_DEBUG_TOGGLES:
                        if pattern in text:
                            hits.append((os.path.relpath(path, REPO_ROOT), pattern))
        self.assertTrue(hits)
        for rel, pattern in hits:
            self.assertEqual(rel, os.path.join('asf', 'conventions.py'), (rel, pattern))


class TreeSelfCheck(unittest.TestCase):
    def test_this_repo_adds_no_unwaived_debug_toggle(self):
        pats, allow, hits = dt.markers({}), dt.waiver({}), []
        for top in ('asf', 'tests', 'tools', 'evals', 'plugin', 'sample'):
            for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, top)):
                for name in files:
                    path = os.path.join(root, name)
                    with open(path, encoding='utf-8', errors='replace') as fh:
                        for n, line in enumerate(fh, 1):
                            if dt.scan_line(pats, line) and not dt.waived(allow, line):
                                hits.append((os.path.relpath(path, REPO_ROOT), n, line.strip()))
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
