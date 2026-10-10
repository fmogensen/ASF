import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import conventions as conv_mod
from asf import env
from asf.conventions import Conventions
from asf.harvest import lane as lane_mod
from asf.harvest import regression
from asf.workers import lifecycle

#: a default-conventions instance: `fix/` is a `fix` branch, `worker/` a `code` one, `plan/` and
#: `spec/` their own kinds, `cloud/direct-` a `direct` one — none of it set by this test.
DEFAULT_CONV = Conventions()

MIXED_FILES = ['tests/test_a.py', 'src/b_test.py', 'ui/c.test.tsx', 'data/fixtures/sample.json',
               'src/d.py']


class PredicateTest(unittest.TestCase):
    """Design §1 — `gated`, `test_files`, `failing_cases` and `verdict`: no git, no test run, no
    product. S-80454's eleven acceptance lines, one assertion each."""

    def test_a_fix_branch_found_in_prod_with_a_test_and_a_non_test_change_is_gated(self):
        items = {'B-0001': {'found_in': 'prod'}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'src/a.py'])
        self.assertEqual((ok, why), (True, ''))

    def test_the_same_branch_found_in_ci_is_not_gated_and_why_names_found_in_ci(self):
        items = {'B-0001': {'found_in': 'ci'}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'src/a.py'])
        self.assertFalse(ok)
        self.assertEqual(why, 'found_in: ci')

    def test_a_code_plan_spec_or_direct_branch_is_not_gated_whatever_its_card_says(self):
        items = {'B-0001': {'found_in': 'prod'}}
        for branch in ('worker/B-0001', 'plan/F-0001', 'spec/F-0001', 'cloud/direct-B-0001'):
            ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', branch,
                                        ['tests/test_a.py', 'src/a.py'])
            self.assertFalse(ok, branch)
            self.assertTrue(why, branch)

    def test_a_card_with_no_found_in_field_at_all_is_gated(self):
        items = {'B-0001': {}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'src/a.py'])
        self.assertEqual((ok, why), (True, ''))

    def test_a_fix_branch_whose_whole_diff_is_test_files_is_not_gated(self):
        items = {'B-0001': {'found_in': 'prod'}}
        ok, why = regression.gated(DEFAULT_CONV, items, 'B-0001', 'fix/B-0001',
                                    ['tests/test_a.py', 'tests/test_b.py'])
        self.assertFalse(ok)
        self.assertIn('no change to be red against', why)

    def test_test_files_keeps_order_and_leaves_a_fixture_data_file_alone(self):
        self.assertEqual(regression.test_files(MIXED_FILES),
                          ['tests/test_a.py', 'src/b_test.py', 'ui/c.test.tsx'])

    def test_failing_cases_reads_unittest_fail_and_error_and_the_node_vitest_and_tap_shapes(self):
        log = ('FAIL: test_x (a.b.C.test_x)\n'
               'ERROR: test_y (a.b.C.test_y)\n'
               '✖ a node case\n'
               '✗ a vitest case\n'
               '× another vitest case (12ms)\n'
               'not ok 3 - a tap case\n')
        self.assertEqual(regression.failing_cases(log),
                          ['test_x', 'test_y', 'a node case', 'a vitest case',
                           'another vitest case', 'a tap case'])

    def test_verdict_returns_proved_when_a_case_is_red_before_and_absent_after(self):
        before = (1, 'FAIL: test_x (a.b.C.test_x)\nRan 3 tests\nFAILED (failures=1)')
        after = (0, 'Ran 3 tests\nOK')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual(state, 'proved')
        self.assertEqual(cases, ['test_x'])
        self.assertTrue(line)

    def test_verdict_returns_not_red_when_the_pre_fix_run_exits_zero_naming_no_case(self):
        before = (0, 'Ran 3 tests\nOK')
        after = (0, 'Ran 3 tests\nOK')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual((state, cases), ('not-red', []))
        self.assertTrue(line)

    def test_verdict_returns_inconclusive_when_the_pre_fix_run_exits_non_zero_naming_no_case(self):
        before = (1, "ImportError: cannot import name 'thing'\nFAILED (errors=1)")
        after = (0, 'Ran 3 tests\nOK')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual((state, cases), ('inconclusive', []))
        self.assertTrue(line)

    def test_verdict_returns_red_both_when_every_case_red_before_is_red_after_too(self):
        before = (1, 'FAIL: test_x (a.b.C.test_x)\nFAILED (failures=1)')
        after = (1, 'FAIL: test_x (a.b.C.test_x)\nFAILED (failures=1)')
        state, cases, line = regression.verdict(before, after)
        self.assertEqual((state, cases), ('red-both', ['test_x']))
        self.assertTrue(line)


#: a fresh identity for every commit this module makes — never the box's own git config
_IDENT = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
         'GIT_COMMITTER_EMAIL': 't@t'}


def _git(repo, *args):
    from asf.harvest import harvest as H
    r = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True,
                       env=dict(H.clean_env(), **_IDENT))
    if r.returncode != 0:
        raise AssertionError(f'git {" ".join(args)} in {repo} failed: {r.stderr}')
    return r.stdout.strip()


def _write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


_CALC_TEST = ('import unittest\n\nfrom calc import add\n\n\n'
             'class CalcTest(unittest.TestCase):\n'
             '    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n')


def build_fixture(tmp):
    """A real git fixture (D2, D3): ``trunk_sha`` holds a buggy ``add()`` and no test for it;
    ``branch_sha`` (``fix/B-0001``) fixes it and adds ``tests/test_calc.py``, which fails on
    ``trunk_sha`` and passes on ``branch_sha``."""
    repo = os.path.join(tmp, 'repo')
    os.makedirs(repo)
    _git(repo, 'init', '-q', '-b', 'main')
    _write(repo, 'calc.py', 'def add(a, b):\n    return a - b  # P1: the defect\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', 'trunk: add() subtracts instead of adding')
    trunk_sha = _git(repo, 'rev-parse', 'HEAD')
    _git(repo, 'checkout', '-q', '-b', 'fix/B-0001')
    _write(repo, 'calc.py', 'def add(a, b):\n    return a + b\n')
    _write(repo, 'tests/test_calc.py', _CALC_TEST)
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', "fix(B-0001): add() adds, and the test that would have "
                                     "caught it")
    branch_sha = _git(repo, 'rev-parse', 'HEAD')
    _git(repo, 'checkout', '-q', 'main')
    return repo, trunk_sha, branch_sha


class _RecordingShell:
    """Wraps :func:`regression._run_shell_fallback` (a real subprocess) and, on its first call
    — by which point :func:`regression.run_graft` has already grafted and committed — records
    the worktree path, what differs from ``trunk_sha`` and the grafted commit's own sha, so the
    test can assert on the worktree state before it is torn down."""

    def __init__(self, trunk_sha):
        self.trunk_sha = trunk_sha
        self.calls = []
        self.path = self.first_diff = self.first_parent = self.grafted_sha = None

    def __call__(self, command, cwd, timeout):
        if not self.calls:
            self.path = cwd
            self.first_diff = _git(cwd, 'diff', '--name-only', self.trunk_sha)
            self.first_parent = _git(cwd, 'rev-parse', 'HEAD~1')
            self.grafted_sha = _git(cwd, 'rev-parse', 'HEAD')
        self.calls.append((command, cwd, timeout))
        return regression._run_shell_fallback(command, cwd, timeout)


_COMMAND = 'python3 -m unittest discover -s tests -t . -v'


class GraftedRunTest(unittest.TestCase):
    """Design §2 — the grafted worktree, the detached job, the lock and the result store,
    function for function after `asf/harvest/rebuild_check.py` (D2, D3, D6, D7; P4). S-80455's
    seven acceptance lines, one assertion each, over the real git fixture of :func:`build_fixture`."""

    def test_the_worktree_is_detached_at_the_trunk_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, trunk_sha, branch_sha = build_fixture(tmp)
            spy = _RecordingShell(trunk_sha)
            regression.run_graft(repo, os.path.join(tmp, 'state'), trunk_sha, branch_sha,
                                 _COMMAND, run_shell=spy)
            self.assertEqual(spy.first_parent, trunk_sha)

    def test_only_the_branchs_test_files_differ_from_the_trunk_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, trunk_sha, branch_sha = build_fixture(tmp)
            spy = _RecordingShell(trunk_sha)
            regression.run_graft(repo, os.path.join(tmp, 'state'), trunk_sha, branch_sha,
                                 _COMMAND, run_shell=spy)
            self.assertEqual(spy.first_diff.splitlines(), ['tests/test_calc.py'])

    def test_the_grafted_commit_is_unreachable_and_the_worktree_is_gone_pass_or_fail(self):
        for command in (_COMMAND, 'exit 1'):
            with tempfile.TemporaryDirectory() as tmp:
                repo, trunk_sha, branch_sha = build_fixture(tmp)
                spy = _RecordingShell(trunk_sha)
                regression.run_graft(repo, os.path.join(tmp, 'state'), trunk_sha, branch_sha,
                                     command, run_shell=spy)
                self.assertEqual(_git(repo, 'branch', '--contains', spy.grafted_sha), '',
                                 command)
                self.assertFalse(os.path.exists(spy.path), command)

    def test_the_command_receives_the_pre_fix_sha_for_the_base_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, trunk_sha, branch_sha = build_fixture(tmp)
            spy = _RecordingShell(trunk_sha)
            command = f'{_COMMAND}  # base={trunk_sha}'
            regression.run_graft(repo, os.path.join(tmp, 'state'), trunk_sha, branch_sha,
                                 command, run_shell=spy)
            self.assertIn(trunk_sha, spy.calls[0][0])

    def test_the_store_key_is_stable_across_a_content_free_rebase_and_differs_on_a_test_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, trunk_sha, branch_sha = build_fixture(tmp)
            k1 = regression.key(repo, trunk_sha, branch_sha, _COMMAND)
            self.assertIsNotNone(k1)
            _git(repo, 'checkout', '-q', 'fix/B-0001')
            _git(repo, 'commit', '-q', '--amend', '-m', 'fix(B-0001): reworded, no content change')
            branch_sha2 = _git(repo, 'rev-parse', 'HEAD')
            self.assertNotEqual(branch_sha, branch_sha2)
            self.assertEqual(k1, regression.key(repo, trunk_sha, branch_sha2, _COMMAND))
            _write(repo, 'tests/test_calc.py', _CALC_TEST.replace('2, 2', '3, 3'))
            _git(repo, 'add', '.')
            _git(repo, 'commit', '-q', '-m', 'fix(B-0001): another case')
            branch_sha3 = _git(repo, 'rev-parse', 'HEAD')
            self.assertNotEqual(k1, regression.key(repo, trunk_sha, branch_sha3, _COMMAND))
            _git(repo, 'checkout', '-q', 'main')

    def test_a_second_check_while_the_first_holds_the_lock_names_the_running_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, trunk_sha, branch_sha = build_fixture(tmp)
            state_dir = os.path.join(tmp, 'state')
            lock = regression.try_lock(state_dir)
            self.addCleanup(lock.close)
            running_sha = 'c' * 40
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps({'key': 'another-key', 'trunk': trunk_sha,
                                  'branch': running_sha, 'since': 'now', 'pid': 1}))
            lock.flush()
            state, line = regression.check(object(), repo, state_dir, trunk_sha, branch_sha,
                                            _COMMAND)
            self.assertIsNone(state)
            self.assertIn(running_sha[:9], line)

    def test_the_first_pass_defers_and_says_the_check_started_in_the_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, trunk_sha, branch_sha = build_fixture(tmp)
            state_dir = os.path.join(tmp, 'state')
            state, line = regression.check(object(), repo, state_dir, trunk_sha, branch_sha,
                                            _COMMAND, spawn_fn=lambda *a, **k: 4242)
            self.assertIsNone(state)
            self.assertIn('started in the background', line)


# ---- the lane: a stubbed verdict drives real `branch_facts` (PD11) -----------------------------

GREEN = ('import unittest\n\n\nclass T(unittest.TestCase):\n'
        '    def test_ok(self):\n        self.assertTrue(True)\n')


class LaneHoldFixture(unittest.TestCase):
    """A bare origin, the product's checkout, and a worker clone that pushes a `fix/` branch —
    `regression.check` itself is mocked (PD11): the four states are `verdict`'s to decide and
    `PredicateTest`'s to prove; this fixture proves only what the lane does with each."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='regr_lane_')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        self.worker = os.path.join(self.base, 'worker')
        self.state_dir = os.path.join(self.base, 'state')
        os.makedirs(self.state_dir)
        os.makedirs(self.origin)
        _git(self.origin, 'init', '-q', '--bare', '-b', 'main')
        _git(self.base, 'clone', '-q', self.origin, self.repo)
        _write(self.repo, 'a.txt', 'a\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'init')
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        _git(self.base, 'clone', '-q', self.origin, self.worker)

    def product(self, check=True, **conv_extra):
        conv = {'test_command': 'true', 'specs_dir': 'specs', 'plans_dir': 'plans',
               'reviews_dir': 'reviews', 'lane': {'review': {'code': 'none'}},
               'branch_prefixes': {'code': 'worker/', 'plan': 'plan/', 'spec': 'spec/',
                                  'fix': 'fix/'},
               'flags': {'regression': {'check': check}}}
        conv.update(conv_extra)
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main', 'conventions': conv,
                                      'steps': {'batch': 'off'}})

    def push_fix(self, branch='fix/B-0142', item='B-0142', body=''):
        _git(self.worker, 'fetch', '-q', 'origin')
        _git(self.worker, 'checkout', '-q', '-B', branch, 'origin/main')
        _write(self.worker, 'tests/test_b.py', GREEN)
        _write(self.worker, 'b.py', 'x = 1\n')
        _git(self.worker, 'add', '-A')
        args = ['commit', '-q', '-m', f'fix({item}): a fix'] + (['-m', body] if body else [])
        _git(self.worker, *args)
        _git(self.worker, 'push', '-q', '-f', 'origin', branch)

    def add_decision(self, did='D-0012'):
        _write(self.repo, f'docs/decisions/{did}.md', f'# {did}\n\nkept.\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', f'docs({did}): kept')
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')

    def session(self, item, branch):
        job = f'coder-{item.lower()}'
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'kind': 'coder',
                                'pid': 999999, 'started': '2026-10-10T00:00:00Z'}) + '\n')
            f.write(json.dumps({'job': job, 'ended': '2026-10-10T00:05:00Z',
                                'end_reason': 'finished', 'rc': 0}) + '\n')

    def lane_of(self, branch):
        """The branch's folded run (`lifecycle.by_branch`): `['lane']['state']` is the lane's
        own transition, `['correction']`/`['rounds']` the hold — two different jsonl lines of
        the same run, folded into one dict."""
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl')).get(branch) or {}
        out = dict(run)
        out['state'] = (run.get('lane') or {}).get('state')
        return out

    def run_pass(self, product=None, items=None, out=None):
        return lane_mod.lane_pass(product or self.product(), self.state_dir, items=items,
                                  out=out or (lambda *_: None))

    #: the card `ask` reads: `found_in: prod` so `gated` covers it (D1)
    ITEMS = {'B-0142': {'found_in': 'prod', 'type': 'task'}}


class LaneHoldTest(LaneHoldFixture):
    """Design §3, §4 — the ask in `branch_facts`, `hold_text`, the new `lifecycle.REGRESSION`
    cause (D4, D5, D9; P2, P12). S-80456's seven acceptance lines."""

    def test_a_not_red_verdict_goes_back_to_its_session_with_cause_regression(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check', return_value=(
               'not-red', "the branch's tests pass on the pre-fix tree: nothing on it would "
                         "have caught this defect")):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        self.assertEqual(rec.get('state'), lane_mod.BACK)
        self.assertEqual((rec.get('correction') or {}).get('kind'), lifecycle.REGRESSION)

    def test_the_holds_text_names_the_test_file_the_pre_fix_sha_and_the_command_verbatim(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check',
                               return_value=('not-red', 'the pre-fix run named no case')):
            self.run_pass(items=self.ITEMS)
        text = (self.lane_of('fix/B-0142').get('correction') or {}).get('text') or ''
        trunk_sha = _git(self.origin, 'rev-parse', 'main')
        self.assertIn('tests/test_b.py', text)
        self.assertIn(trunk_sha[:9], text)
        self.assertIn('true', text)  # conventions.test_command, verbatim: the command the lane ran

    def test_the_hold_advances_rounds_by_one_and_the_cause_is_not_mechanical(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check',
                               return_value=('not-red', 'the pre-fix run named no case')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        self.assertEqual(rec.get('rounds'), 1)
        self.assertNotIn(lifecycle.REGRESSION, lifecycle.MECHANICAL)

    def test_a_proved_verdict_takes_the_transition_the_trunk_takes_today(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check', return_value=(
               'proved', 'test_ok red before the fix and absent after: proved')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        self.assertEqual(rec.get('state'), lane_mod.GATE)
        self.assertNotEqual((rec.get('correction') or {}).get('kind'), lifecycle.REGRESSION)

    def test_a_deferred_verdict_holds_nothing_and_prints_the_deferral(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        lines = []
        with mock.patch.object(regression, 'check', return_value=(
               None, 'the regression check started in the background on 1a2b3c4 (pid 1) — '
                    'the next pass reads its result')):
            self.run_pass(items=self.ITEMS, out=lines.append)
        rec = self.lane_of('fix/B-0142')
        self.assertNotEqual((rec.get('correction') or {}).get('kind'), lifecycle.REGRESSION)
        self.assertTrue(any('started in the background' in l for l in lines))

    def test_an_inconclusive_verdict_holds_naming_the_non_zero_run_that_named_no_case(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check', return_value=(
               'inconclusive', 'the pre-fix run exited non-zero naming no failing case: the '
                              'grafted test did not run as a test on the pre-fix tree')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        text = (rec.get('correction') or {}).get('text') or ''
        self.assertEqual(rec.get('state'), lane_mod.BACK)
        self.assertIn('exited non-zero', text)
        self.assertIn('naming no failing case', text)

    def test_a_red_both_verdict_holds_naming_the_cases_red_on_both_sides(self):
        self.push_fix()
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check', return_value=(
               'red-both', 'test_x red on both sides: not a regression test for this fix')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        text = (rec.get('correction') or {}).get('text') or ''
        self.assertEqual(rec.get('state'), lane_mod.BACK)
        self.assertIn('test_x', text)
        self.assertIn('red on both sides', text)


class WaiverTest(LaneHoldFixture):
    """Design §3 — the `Regression-waived: D-nnnn` trailer against the decision register
    (D10). S-80457's four acceptance lines."""

    def test_a_waiver_naming_a_decision_the_register_holds_lands_the_branch(self):
        self.add_decision('D-0012')
        self.push_fix(body='Regression-waived: D-0012')
        self.session('B-0142', 'fix/B-0142')
        lines = []
        with mock.patch.object(regression, 'check',
                               return_value=('not-red', 'the pre-fix run named no case')):
            self.run_pass(items=self.ITEMS, out=lines.append)
        rec = self.lane_of('fix/B-0142')
        self.assertEqual(rec.get('state'), lane_mod.GATE)
        self.assertTrue(any('D-0012' in l for l in lines))

    def test_the_same_trailer_naming_an_id_the_register_lacks_holds_the_branch(self):
        self.push_fix(body='Regression-waived: D-9999')
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check',
                               return_value=('not-red', 'the pre-fix run named no case')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        text = (rec.get('correction') or {}).get('text') or ''
        self.assertEqual(rec.get('state'), lane_mod.BACK)
        self.assertIn('D-9999', text)
        self.assertIn('not in the decision', text)

    def test_a_prose_excuse_with_no_trailer_holds_as_a_plainly_missing_test(self):
        self.push_fix(body='No regression test: this cannot be caught by a test, trust me')
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check',
                               return_value=('not-red', 'the pre-fix run named no case')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        self.assertEqual(rec.get('state'), lane_mod.BACK)
        self.assertEqual((rec.get('correction') or {}).get('kind'), lifecycle.REGRESSION)

    def test_a_waived_branch_spends_no_round(self):
        self.add_decision('D-0012')
        self.push_fix(body='Regression-waived: D-0012')
        self.session('B-0142', 'fix/B-0142')
        with mock.patch.object(regression, 'check',
                               return_value=('not-red', 'the pre-fix run named no case')):
            self.run_pass(items=self.ITEMS)
        rec = self.lane_of('fix/B-0142')
        self.assertFalse(rec.get('rounds'))


class _FakeLane:
    def __init__(self, conv, items=None):
        self.conv, self.items = conv, items or {}
        self.out = lambda *_: None
        self.repo = self.state_dir = self.trunk_sha = self.product = None
        self.trunk = 'main'


class FlagsTest(unittest.TestCase):
    """Design §5 — the four `regression.*` flags, off by default (D1, D7, D8; P11). S-80458's
    five acceptance lines."""

    def test_the_check_flag_defaults_to_off_and_a_fix_branch_is_not_gated_and_says_so(self):
        f = {'branch': 'fix/B-0001', 'item': 'B-0001', 'files': ['tests/test_a.py', 'src/a.py']}
        state, line = regression.ask(_FakeLane(Conventions()), f)
        self.assertEqual(state, 'skipped')
        self.assertIn('regression.check is off', line)

    def test_all_four_flags_are_registered_as_known_flags(self):
        for name in ('regression.check', 'regression.exempt_found_in', 'regression.command',
                    'regression.fail_re'):
            self.assertIn(name, conv_mod.KNOWN_FLAGS)

    def test_command_falls_back_to_test_command_and_substitutes_base_either_way(self):
        c = Conventions.from_mapping({'test_command': 'make test'})
        self.assertEqual(regression.resolved_command(c, 'deadbee'), 'make test')
        c2 = Conventions.from_mapping({'test_command': 'make test',
                                      'flags': {'regression': {'command': 'run {base} now'}}})
        self.assertEqual(regression.resolved_command(c2, 'deadbee'), 'run deadbee now')

    def test_a_non_string_command_is_a_named_problem_not_a_runner_crash(self):
        probs = dict(conv_mod.validate_mapping({'flags': {'regression': {'command': 7}}}))
        self.assertIn('flags.regression.command', probs)

    def test_a_non_list_of_strings_exempt_found_in_is_a_named_problem(self):
        probs = dict(conv_mod.validate_mapping(
            {'flags': {'regression': {'exempt_found_in': 'ci'}}}))
        self.assertIn('flags.regression.exempt_found_in', probs)


if __name__ == '__main__':
    unittest.main()
