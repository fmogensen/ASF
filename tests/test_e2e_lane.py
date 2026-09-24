"""The PR lane end to end: one sample product, a fake PR host, scripted sessions, the real tick.

Each case lays a product down (:class:`e2e.factory.Factory`: bare origins, an operator home, the
fake ``gh`` first on ``PATH``), then runs whole ticks — record, health, wave, prs, harvest — in
this process, with the harvest inline, and asserts what the ledger, the record, the feeder's rows
and the forge say. The sessions are scripted (``tests/e2e/scripts``): they write, commit and push
in their real worktrees and end with the typed REPORT.

The scenarios run twice, under ``landing: pull-request`` and under ``landing: fast-forward``: the
lane is meant to be one machine with two hosts (package plan §2), so both modes are held to one
outcome wherever it means anything in both. A case the code of the day gets wrong is marked
``expectedFailure`` with the defect it proves named beside it; the workstream that fixes the
defect makes it hard. The assertions say what the lane must do, not what it does today.

What the tests prove, by the package checklist (§10):

* R19 (ticks in process, harvest inline, scenarios 1, 2, 4, 5 in both modes): ``test_r19_s1_*``,
  ``test_r19_s2_*``, ``test_r19_s4_*``, ``test_r19_s5_*``;
* R8 (a tick that dies between an external merge and its ledger write is recovered):
  ``test_r8_crash_between_merge_and_ledger_is_recoverable``;
* R12 (a human merge in PR mode is a landing): ``test_r12_human_merge_is_landed_not_foreign``;
* R7 (a docs branch the gate refuses goes back to a STARVED → SPEC/PLAN session):
  ``test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges``.

And the lane faults a product saw along real PR lifecycles, as cases (``fault<n>`` in the name,
or the scenario that carries it):

1. a docs PR merged on green checks while the product gate would be red →
   ``test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges``;
2. a merged spec/plan PR derived the Feature landed → ``test_r19_s1_*`` (never done before its
   Task, one plan session);
3. a squash-merged PR later seen as an "empty branch" → ``test_r12_*`` (branch left behind) and
   ``test_r19_s4_*`` (no round after the squash);
4. an open code PR never got a review session → ``test_r19_s1_*`` and ``test_r19_s5_*`` (PR mode);
5. an open fix PR still offered BUG → FIX → ``test_fault5_open_fix_pr_gets_no_second_fix_session``;
6. a footprint-partial coder looped on corrections → ``test_fault6_footprint_partial_widens_then_lands``;
7. a widening into another Active Task's writes refused every record commit →
   ``test_fault7_widening_never_overlaps_an_active_sibling``;
8. a push refused by the product's pre-push looped as "unpushed work" without the hook's output →
   ``test_fault8_refused_push_carries_the_hooks_output``;
9. PRs opened before the lane knew them were invisible → ``test_r19_s5_*``.
"""
import atexit
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_e2e_lane` does not
    from e2e.factory import Factory
except ImportError:  # pragma: no cover - import shape only
    from tests.e2e.factory import Factory

PR = 'pull-request'
FF = 'fast-forward'
DONE = ('Resolved', 'Closed')
#: A correction the lane writes to ask for a review is a request, not a round.
NOT_A_ROUND = ('review-wanted',)

RED_TEST = ('import unittest\n\n\nclass TrunkTests(unittest.TestCase):\n'
            '    def test_trunk(self):\n        self.assertEqual(1, 2)\n')
LINES_PY = 'def count_lines(text):\n    return len(text.splitlines())\n'
LINES_TEST = ('import unittest\n\n\nclass LinesTests(unittest.TestCase):\n'
              '    def test_last_line(self):\n'
              '        self.assertEqual(len(\'a\\nb\'.splitlines()), 2)\n')


def expected_failure(fn):
    """``unittest.expectedFailure`` — unless ``ASF_E2E_STRICT`` is set, which runs every case as
    a plain test, so each defect's own failure (the assertion it trips) can be read."""
    return fn if os.environ.get('ASF_E2E_STRICT') else unittest.expectedFailure(fn)


class Crash(BaseException):
    """The tick's process dies here: nothing after this point of the tick runs."""


def _git_show(repo, rev, path):
    """``path`` at ``rev`` of ``repo``, or '' when it is not there."""
    return subprocess.run(['git', 'show', f'{rev}:{path}'], cwd=repo, capture_output=True,
                          text=True).stdout


def _git_ls(repo, rev, path):
    out = subprocess.run(['git', 'ls-tree', '--name-only', f'{rev}:{path}'], cwd=repo,
                         capture_output=True, text=True).stdout
    return out.split()


#: ``{(landing, stage): Factory}`` — a product ticked until T-0001's branch waits only for its
#: landing, built once per module run and forked per test (:meth:`Factory.fork`).
_READY = {}


class LaneCase(unittest.TestCase):
    landing = PR
    stage = None
    #: ``'ready'``: start from T-0001's branch ready to land (:meth:`ready_to_land`), forked
    #: from one built once; ``None``: a fresh product at ``stage``.
    start = None

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='asf-e2e-')
        self.addCleanup(shutil.rmtree, tmp, True)
        if self.start == 'ready':
            self.f = self.ready_template().fork(tmp)
        else:
            self.f = Factory(tmp, landing=self.landing, stage=self.stage).setup()

    def ready_template(self):
        key = (self.landing, self.stage)
        if key not in _READY:
            root = tempfile.mkdtemp(prefix='asf-e2e-ready-')
            atexit.register(shutil.rmtree, root, True)
            self.f = Factory(root, landing=self.landing, stage=self.stage).setup()
            self.ready_to_land('T-0001')
            _READY[key] = self.f
        return _READY[key]

    # ---- reading the ledger ------------------------------------------------------------------

    def lines(self):
        from asf.workers import lifecycle
        path = os.path.join(self.f.state_dir, 'sessions.jsonl')
        return lifecycle.read_lines(path) if os.path.exists(path) else []

    def rounds(self, item):
        """Every correction written on a run of ``item`` that is not a review request."""
        items = {r['job']: r.get('item') for r in self.lines() if r.get('item')}
        return [r['correction'] for r in self.lines()
                if items.get(r.get('job')) == item and isinstance(r.get('correction'), dict)
                and r['correction'].get('kind') not in NOT_A_ROUND]

    def launches(self, item=None, kind=None):
        return [c for c in self.f.runtime.calls
                if (item is None or c['item'] == item) and (kind is None or c['kind'] == kind)]

    def harvested(self, item):
        """The ``harvested`` value of every run of ``item`` that has one."""
        return [r['harvested'] for r in self.f.snapshot().ledger.values()
                if r.get('item') == item and r.get('harvested')]

    def done(self, item):
        return self.f.snapshot().state(item) in DONE

    # ---- ticking -----------------------------------------------------------------------------

    def check_tick(self, t):
        """What holds after every tick (I4): it ran clean, it launched at most one session per
        item, no two live runs hold one branch, and the record took its commit."""
        self.assertEqual(t.rc, 0, '\n'.join(t.lines))
        self.assertFalse([ln for ln in t.lines if 'Traceback' in ln or ' FAILED' in ln],
                         '\n'.join(t.lines))
        items = [c['item'] for c in t.calls]
        self.assertEqual(len(items), len(set(items)), f'tick {t.n} launched {t.launched}')
        live = [r.get('branch') for r in t.snap.ledger.values() if not r.get('ended')]
        self.assertEqual(len(live), len(set(live)), f'tick {t.n}: two live runs on one branch')
        self.assertTrue(t.find('tick: state committed and pushed') or t.find('tick: no change'),
                        f'tick {t.n}: the record step did not commit:\n' + '\n'.join(t.lines))

    def tick(self):
        t = self.f.tick()
        self.check_tick(t)
        return t

    def until(self, done, limit, what, each=None):
        """Tick until ``done()`` (no tick when it already is); ``each(tick)`` after every tick;
        fail naming ``what`` after ``limit`` ticks."""
        if done():
            return None
        for _ in range(limit):
            t = self.tick()
            if each:
                each(t)
            if done():
                return t
        self.fail(f'{what}: not reached in {limit} ticks — the last tick said:\n'
                  + '\n'.join(self.f.ticks[-1].lines))

    # ---- hand-made states --------------------------------------------------------------------

    def red_trunk(self):
        return self.f.push('main', {'tests/test_trunk.py': RED_TEST},
                           'test: a direct push that turns the trunk red')

    def green_trunk(self):
        return self.f.push('main', {'tests/test_trunk.py': None}, 'test: the trunk is green again')

    def branch_by_hand(self, branch, item, title):
        """``branch`` pushed and its PR opened by someone outside the factory."""
        head = self.f.push(branch, {'src/lines.py': LINES_PY, 'tests/test_lines.py': LINES_TEST},
                           f'feat({item}): {title}, by hand')
        return head, self.f.open_pr(branch, f'{item} — {title}')

    def ready_to_land(self, item, limit=4):
        """Tick until ``item``'s branch waits only for its landing: reviewed in PR mode,
        pushed by its coder in fast-forward mode."""
        if self.landing == PR:
            self.until(lambda: self.launches(item=item, kind='review'), limit,
                       f'{item} is reviewed')
        else:
            self.until(lambda: self.launches(item=item, kind='coder'), limit,
                       f'{item}\'s coder pushed')


# ======================================================================================
# Scenario 1 — the happy path: card → spec → plan → Task → coder → review → merge
# ======================================================================================

class HappyPath:
    """A decided Feature card walks the whole lane: the spec lands (docs: no review), the plan
    lands and mints its Task, a coder builds it, a review session approves its head once, the
    lane merges it, the Task and then the Feature are done — and never before (fault 2)."""

    def test_r19_s1_card_to_merge_resolves_the_feature(self):
        f = self.f

        def feature_not_done_early(t):  # fault 2: a merged spec/plan is not a landed Feature
            if not self.done('T-0001'):
                self.assertNotIn(t.snap.state('F-0001'), DONE, f'tick {t.n}: F-0001 done early')
                self.assertNotEqual(t.snap.stage('F-0001'), 'landed', f'tick {t.n}')
        self.until(lambda: self.harvested('T-0001'), 11, 'T-0001 lands',
                   each=feature_not_done_early)
        # fault 4 / T3: the code is reviewed, once, before it lands
        self.assertEqual([c['item'] for c in self.launches(kind='review')], ['T-0001'],
                         'the code lane reviews the Task once (lane.review.code: required)')
        self.until(lambda: self.done('F-0001'), 2, 'F-0001 is done', each=feature_not_done_early)
        snap = f.snapshot()
        self.assertEqual([c['job'] for c in self.launches(kind='spec')], ['spec-f-0001'])
        self.assertEqual([c['job'] for c in self.launches(kind='plan')], ['plan-f-0001'],
                         'a merged plan is never written again (fault 2: STARVED relaunch)')
        self.assertEqual([c['job'] for c in self.launches(kind='coder')], ['coder-t-0001'])
        self.assertEqual(self.rounds('T-0001'), [])
        self.assertEqual(self.rounds('F-0001'), [])
        landed = self.harvested('T-0001')
        self.assertTrue(f.is_ancestor(landed[-1], 'main'), landed)
        self.assertIn('lines.py', _git_ls(f.repo_origin, 'main', 'src'))
        self.assertIn(snap.state('T-0001'), DONE)
        self.assertEqual([r for r in f.next_rows() if r.feature_id == 'F-0001' and r.launches],
                         [], 'a done Feature plans no more work')
        if self.landing == PR:
            prs = f.prs()
            self.assertEqual(sorted(p['headRefName'] for p in prs),
                             ['feature/T-0001', 'plan/F-0001', 'spec/F-0001'], 'one PR a branch')
            self.assertEqual({(p['state'], p.get('mergeMethod')) for p in prs},
                             {('MERGED', 'squash')})


class HappyPathPR(HappyPath, LaneCase):
    landing = PR


class HappyPathFF(HappyPath, LaneCase):
    landing = FF

    # DEFECT (package plan §2 T3 and "one path for both landing modes"): fast-forward landing
    # has no review stage — `land_combined` gates and pushes a code branch no review session
    # ever read, whatever `conventions.lane.review.code` says; only the PR path (`land_pr` →
    # `request_review`) asks for one.
    @expected_failure
    def test_r19_s1_card_to_merge_resolves_the_feature(self):
        super().test_r19_s1_card_to_merge_resolves_the_feature()


# ======================================================================================
# Scenario 2 — a red trunk: the branch waits, it is never sent back
# ======================================================================================

class RedTrunk:
    """T-0001's branch is ready to land when a direct push turns the trunk red. The branch is
    not to blame: it waits — no correction, no round, no correcting session, no Bug against it,
    no landing onto the red trunk — and lands once the trunk is green again."""

    stage = 'planned'
    start = 'ready'

    def test_r19_s2_red_trunk_waits_then_lands(self):
        f = self.f
        self.ready_to_land('T-0001')
        red = self.red_trunk()
        t = self.tick()
        self.assertFalse(t.find('landed feature/T-0001'), t.lines)
        self.assertFalse(self.harvested('T-0001'), 'landed onto a red trunk')
        self.assertEqual(self.rounds('T-0001'), [], 'a red trunk spent a round on the branch')
        self.assertEqual(self.launches(item='T-0001', kind='correct'), [])
        self.assertEqual(f.snapshot().of_type('bug'), {}, 'a Bug was filed for the trunk\'s red')
        self.assertTrue(f.on_origin('feature/T-0001'))
        self.green_trunk()
        self.until(lambda: self.harvested('T-0001'), 2, 'T-0001 lands once the trunk is green')
        self.assertTrue(f.is_ancestor(red, 'main'))
        self.assertEqual(self.rounds('T-0001'), [])
        self.assertEqual(len(self.launches(item='T-0001', kind='coder')), 1)


class RedTrunkPR(RedTrunk, LaneCase):
    landing = PR


class RedTrunkFF(RedTrunk, LaneCase):
    landing = FF

    # DEFECT (T8: "the trunk alone is red → WAITING, never a correction"): fast-forward landing
    # checks the trunk alone only when the gate names its red modules (`red_modules`, asf's own
    # runner); a product's plain `unittest` names none, so `land_set` holds the branch with a
    # `gate` correction and a round for the trunk's red. The PR path's `gate_prs` checks the
    # trunk for every red and waits.
    @expected_failure
    def test_r19_s2_red_trunk_waits_then_lands(self):
        super().test_r19_s2_red_trunk_waits_then_lands()


# ======================================================================================
# Scenario 4 — a squash merge lands the Task; an open sibling keeps the Feature open
# ======================================================================================

class SquashMerge:
    """T-0001 lands; T-0002 (``after: T-0001``) is still New. The Task is done from the landing
    fact, the Feature stays open while a sibling Task is, no round is spent after the landing
    (fault 3), and the sibling is the next work. On the PR host the merge is a squash whose sha
    does not descend from the branch head, with a subject that does not name the item — only
    the merge fact ties the two (T10)."""

    stage = 'planned'
    start = 'ready'

    def test_r19_s4_landed_task_closes_open_sibling_keeps_feature_open(self):
        f = self.f
        if self.landing == PR:
            f.gh('e2e', 'squash-subject', 'Count lines (#{number})')
        self.until(lambda: self.harvested('T-0001'), 6, 'T-0001 lands')
        self.until(lambda: self.done('T-0001'), 2, 'the record sees T-0001 landed')
        snap = f.snapshot()
        self.assertNotIn(snap.state('T-0002'), DONE)
        self.assertNotIn(snap.state('F-0001'), DONE, 'an open sibling Task keeps the Feature open')
        self.assertTrue((snap.stage('F-0001') or '').startswith('building'), snap.stage('F-0001'))
        if self.landing == PR:
            pr = f.prs('feature/T-0001')[0]
            sha = pr['mergeCommit']['oid']
            self.assertFalse(f.is_ancestor(pr['headRefOid'], sha), 'the merge is a squash')
            self.assertEqual(self.harvested('T-0001'), [sha], 'harvested at the squash sha')
        self.until(lambda: self.launches(item='T-0002', kind='coder'), 2, 'T-0002 starts')
        self.assertEqual(self.rounds('T-0001'), [], 'a round after the landing (fault 3)')
        self.assertEqual(len(self.launches(item='T-0001', kind='coder')), 1)


class SquashMergePR(SquashMerge, LaneCase):
    landing = PR


class SquashMergeFF(SquashMerge, LaneCase):
    landing = FF


# ======================================================================================
# Scenario 5 — a PR already open on the Task's branch is adopted (fault 9)
# ======================================================================================

class PreexistingPR:
    """Before the first tick someone pushed ``feature/T-0001`` and opened its PR. The lane adopts
    it: no second PR, no coder for T-0001; the adopted head is reviewed (fault 4) and lands."""

    stage = 'planned'

    def test_r19_s5_open_pr_is_adopted_not_rebuilt(self):
        f = self.f
        _head, number = self.branch_by_hand('feature/T-0001', 'T-0001', 'count lines')
        opened_by_hand = len(f.gh_calls('pr', 'create'))
        self.until(lambda: self.harvested('T-0001'), 5, 'the adopted PR lands')
        self.assertEqual(self.launches(item='T-0001', kind='coder'), [], 'a second coder')
        self.assertEqual([p['number'] for p in f.prs('feature/T-0001')], [number], 'a second PR')
        self.assertEqual(f.gh_calls('pr', 'create')[opened_by_hand:], [], 'the lane opened a PR')
        self.assertEqual(len(self.launches(item='T-0001', kind='review')), 1,
                         'the adopted head is reviewed once')
        self.assertEqual(self.rounds('T-0001'), [])
        if self.landing == PR:
            pr = f.prs('feature/T-0001')[0]
            self.assertEqual(pr['state'], 'MERGED')
            self.assertEqual(self.harvested('T-0001'), [pr['mergeCommit']['oid']])
        self.until(lambda: self.done('T-0001'), 2, 'the record sees T-0001 landed')


class PreexistingPRPR(PreexistingPR, LaneCase):
    landing = PR


class PreexistingPRFF(PreexistingPR, LaneCase):
    landing = FF

    # DEFECT (package plan §2 T2 adoption; fault 9): under fast-forward landing nothing adopts
    # a branch no run of the ledger holds — `step_wave.pr_heads` answers only for pull-request
    # landing, and harvest skips a branch with no run — so no review is asked, nothing lands,
    # and T-0001 (Active off its branch, no longer New) never gets a session: stuck for good.
    @expected_failure
    def test_r19_s5_open_pr_is_adopted_not_rebuilt(self):
        super().test_r19_s5_open_pr_is_adopted_not_rebuilt()


# ======================================================================================
# R8 — a tick that dies between the external merge and its ledger write
# ======================================================================================

class CrashAfterMerge:
    """The merge reached the forge (PR mode: ``gh pr merge``; fast-forward: the push onto the
    trunk), then the tick's process died before the run was marked. The next tick closes the run
    at the sha that merge wrote: no second merge, no correction, no second session."""

    stage = 'planned'
    start = 'ready'

    def crash_point(self):
        """``(module, attribute)``: the call right after the external side effect."""
        from asf.harvest import harvest
        return (harvest, 'merged_sha') if self.landing == PR else (harvest, 'push_ff')

    def test_r8_crash_between_merge_and_ledger_is_recoverable(self):
        f = self.f
        self.ready_to_land('T-0001')
        module, name = self.crash_point()
        real = getattr(module, name)

        def dies(*a, **kw):
            real(*a, **kw)
            raise Crash(name)
        before = f.trunk_log()[0][0]
        with mock.patch.object(module, name, dies):
            with self.assertRaises(Crash):
                f.tick()
        merged = f.trunk_log()[0][0]
        self.assertNotEqual(merged, before, 'the merge reached the trunk')
        self.assertFalse(self.harvested('T-0001'), 'the crash came before the ledger write')
        self.until(lambda: self.harvested('T-0001'), 2, 'the next tick closes the run')
        self.assertEqual(self.harvested('T-0001'), [merged],
                         'the run is closed at the sha its merge wrote, not the trunk\'s tip')
        self.assertEqual(self.rounds('T-0001'), [])
        self.assertEqual(len(self.launches(item='T-0001', kind='coder')), 1)
        self.assertEqual(len([s for s, _subj in f.trunk_log() if s == merged]), 1)
        if self.landing == PR:
            self.assertEqual(len(f.gh_calls('pr', 'merge')), 1, 'merged once')
        self.until(lambda: self.done('T-0001'), 2, 'the record sees T-0001 landed')


class CrashAfterMergePR(CrashAfterMerge, LaneCase):
    landing = PR

    # DEFECT (R3/R8, T11): no merge intent is written before `gh pr merge`, and the recovery
    # path is `close_merged` (the branch is gone after `--delete-branch`), which closes the run
    # at the trunk's tip after the next tick's release commit — not at the merge sha.
    @expected_failure
    def test_r8_crash_between_merge_and_ledger_is_recoverable(self):
        super().test_r8_crash_between_merge_and_ledger_is_recoverable()


class CrashAfterMergeFF(CrashAfterMerge, LaneCase):
    landing = FF

    # DEFECT (R8, T11): after a push onto the trunk with no ledger line, the branch is 0 ahead
    # and `close_merged` closes the run at the trunk's tip (the next tick's release commit),
    # not at the sha the landing pushed.
    @expected_failure
    def test_r8_crash_between_merge_and_ledger_is_recoverable(self):
        super().test_r8_crash_between_merge_and_ledger_is_recoverable()


# ======================================================================================
# R12 — a person merges the PR (and leaves its branch): a landing, never foreign (fault 3)
# ======================================================================================

class HumanMergePR(LaneCase):
    landing = PR
    stage = 'planned'
    start = 'ready'

    # DEFECT (T11): a PR merged outside the lane is closed by `close_merged` / `land_already` at
    # the trunk's tip, not at its merge sha; the lane never asks the host which commit merged
    # it while the branch it left behind still reads as work on origin.
    @expected_failure
    def test_r12_human_merge_is_landed_not_foreign(self):
        f = self.f
        self.until(lambda: f.prs('feature/T-0001'), 3, 'the PR is opened')
        number = f.prs('feature/T-0001')[0]['number']
        p = f.gh('pr', 'merge', str(number), '-R', 'example/sample', '--squash')  # branch kept
        self.assertEqual(p.returncode, 0, p.stderr)
        sha = f.prs('feature/T-0001')[0]['mergeCommit']['oid']
        self.until(lambda: self.harvested('T-0001'), 2, 'the human merge is seen')
        self.until(lambda: self.done('T-0001'), 2, 'the record sees T-0001 landed')
        self.assertEqual(self.rounds('T-0001'), [], 'the squash read as an empty branch (fault 3)')
        self.assertEqual(len(self.launches(item='T-0001', kind='coder')), 1)
        self.assertEqual(self.harvested('T-0001'), [sha], 'closed at the human\'s merge sha')


# ======================================================================================
# The faults of real PR lifecycles, as cases
# ======================================================================================

class DocsRedOnGate:
    """Fault 1 / R7: a plan PR's checks are green (the product's CI skips docs PRs) but the
    product gate reads the documents, and the plan cites a retired name. That plan never reaches
    the trunk: it goes back to a plan session (a correction on F-0001), and the trunk stays
    green."""

    stage = 'specced'

    def test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges(self):
        f = self.f
        f.gh('e2e', 'default-checks', 'ci=pass')
        f.runtime.queue('plan', {
            'writes': {'{plan_path}': '# {item} — plan\n\nUses count_words_v0 as before.\n\n'
                                      '### Task 1: count_lines\nstories: S-0001\n'
                                      'writes: src/lines.py, tests/test_lines.py\nafter: none\n'},
            'commit': 'plan({item}): count lines too'})
        self.until(lambda: self.launches(item='F-0001', kind='plan'), 2, 'the plan is written')
        for _ in range(2):  # the plan is gated (its PR opened first, in PR mode)
            self.tick()
            self.assertNotIn('count_words_v0', _git_show(f.repo_origin, 'main', 'plans/f-0001.md'),
                             'a plan the product gate refuses reached the trunk')
        self.until(lambda: self.rounds('F-0001'), 1, 'the plan is sent back')
        merged = [p['mergeCommit']['oid'] for p in f.prs('plan/F-0001') if p['state'] == 'MERGED']
        self.assertFalse([sha for sha in merged
                          if 'count_words_v0' in _git_show(f.repo_origin, sha, 'plans/f-0001.md')],
                         'the refused plan was merged')


class DocsRedOnGatePR(DocsRedOnGate, LaneCase):
    landing = PR


class DocsRedOnGateFF(DocsRedOnGate, LaneCase):
    landing = FF

    # DEFECT (fault 1): fast-forward landing gates a docs-only branch with `docs_only(conv)` —
    # "docs cannot turn a test red" — so a plan the product's own gate refuses lands on the trunk.
    @expected_failure
    def test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges(self):
        super().test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges()


class OpenFixPR:
    """Fault 5: an S2 Bug's fix branch is pushed and its PR open before the lane looked. The
    feeder offers no BUG → FIX for it — the PR is the fix; it is reviewed (PR mode) instead."""

    stage = ('planned', 'bug')

    def test_fault5_open_fix_pr_gets_no_second_fix_session(self):
        self.branch_by_hand('bugfix/B-0001', 'B-0001', 'return 0 on an empty text')
        for _ in range(2):
            t = self.tick()
            self.assertFalse([r for r in t.rows if r.item_id == 'B-0001' and r.launches
                              and r.brief_kind == 'fix-bug'], [vars(r) for r in t.rows])
        self.assertEqual(self.launches(item='B-0001', kind='fix-bug'), [], 'a duplicate fix')
        if self.landing == PR:
            self.assertEqual(len(self.launches(item='B-0001', kind='review')), 1)


class OpenFixPRPR(OpenFixPR, LaneCase):
    landing = PR


class OpenFixPRFF(OpenFixPR, LaneCase):
    landing = FF


class FootprintPartialFF(LaneCase):
    """Fault 6: the coder reports ``status: partial`` with ``needs writes: src/count.py`` (a file
    no other Task writes). The rule widens T-0001's ``writes:``, the same run gets one
    correction, and the widened branch lands — no loop."""

    landing = FF
    stage = 'planned'

    def test_fault6_footprint_partial_widens_then_lands(self):
        f = self.f
        f.runtime.queue('coder-t-0001', {
            'writes': {'{w0}': LINES_PY, '{w1}': LINES_TEST},
            'commit': 'feat({item}): count lines, src/count.py still to change',
            'status': 'partial', 'needs_writes': 'src/count.py',
            'left_out': 'src/count.py: count must share the splitter'})
        f.runtime.queue('correct', {
            'writes': {'src/count.py': 'def count(text):\n    return len(text.split())\n\n\n'
                                       'SPLIT = str.split\n'},
            'commit': 'feat({item}): count shares the splitter'})
        self.until(lambda: self.harvested('T-0001'), 5, 'the widened T-0001 lands')
        self.assertIn('src/count.py', f.snapshot().record['T-0001'].get('writes') or [])
        self.assertEqual(len(self.launches(item='T-0001', kind='correct')), 1, 'one correction')
        self.assertEqual(len(self.launches(item='T-0001', kind='coder')), 1)


class WideningOverlapFF(LaneCase):
    """Fault 7: T-0001 and T-0002 run side by side; T-0001's coder says it needs
    ``src/chars.py`` — T-0002's. The widening never lands in the record while T-0002 is open (the
    record never holds intersecting writes, I3), and every tick's record commit still goes in."""

    landing = FF
    stage = ('planned', 'parallel')

    def test_fault7_widening_never_overlaps_an_active_sibling(self):
        f = self.f
        f.runtime.queue('coder-t-0001', {
            'writes': {'{w0}': LINES_PY, '{w1}': LINES_TEST},
            'commit': 'feat({item}): count lines, src/chars.py still to change',
            'status': 'partial', 'needs_writes': 'src/chars.py'})
        f.runtime.queue('coder-t-0002', {'writes': {'{w0}': 'def count_chars(text):\n'
                                                            '    return len(text)\n'},
                                         'commit': 'feat({item}): count chars', 'running': True})

        def disjoint(t):  # check_tick asserts the record commit went in, every tick
            writes = {i: set(v.get('writes') or ()) for i, v in t.snap.of_type('task').items()}
            if t.snap.state('T-0002') not in DONE:
                self.assertFalse(writes.get('T-0001', set()) & writes.get('T-0002', set()),
                                 f'tick {t.n}: intersecting writes {writes}')
        for _ in range(3):
            disjoint(self.tick())
        self.assertEqual(len(self.launches(item='T-0001', kind='coder')), 1)


class RefusedPushFF(LaneCase):
    """Fault 8: the product's pre-push refuses the coder's push (the trunk is red). The run is
    held, and its correction carries the hook's own output, so the next session can act on it
    instead of pushing into the same refusal."""

    landing = FF
    stage = 'planned'

    # DEFECT (T9: "a pre-push hook refused (with the hook's output)"): health judges the run
    # `failed: unpushed work` and holds it with the generic "commit and push what you have"
    # text; the REPORT's `pushed: no — <the hook's output>` never reaches the correction, so the
    # next session pushes into the same refusal.
    @expected_failure
    def test_fault8_refused_push_carries_the_hooks_output(self):
        f = self.f
        f.refuse_pushes('pre-push: the trunk is red (tests/test_trunk.py) — push refused')
        f.runtime.queue('coder-t-0001', {'writes': {'{w0}': LINES_PY, '{w1}': LINES_TEST},
                                         'commit': 'feat({item}): count lines', 'hooks': True})
        self.until(lambda: self.rounds('T-0001'), 2, 'the refused push is held')
        texts = ' '.join(str(c.get('text') or '') for c in self.rounds('T-0001'))
        self.assertIn('the trunk is red', texts, 'the correction drops the hook\'s output')


if __name__ == '__main__':
    unittest.main()
