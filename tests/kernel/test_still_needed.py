"""The still-needed gate (asf.kernel.needed): before any Ready Task or Bug launches, the tests its
card names as its proof — new since it was planned — are run on the trunk, and an item already
done there is Done, never rebuilt. A done REPORT that changed nothing and names a landed sha is
Done the same way (no empty commit, PR or review), an open kernel PR that changes nothing is
closed, and CI for a closed PR or a superseded head is cancelled. The card shapes are the live
record's (2026-10-10): B-0097's Bug card, T-0453's plan-copied Files lines, #1308's empty PR."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf.kernel import actions as A
from asf.kernel import apply as AP
from asf.kernel import decide as D
from asf.kernel import facts as K
from asf.kernel import needed as N
from asf.kernel import needed_probe as NP
from asf.kernel import model as M
from asf.kernel import loop as L
from asf.kernel import resolvers as R
from asf.kernel import trunk as T

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

#: B-0097's card body, with an acceptance that names its test ids
BUG_BODY = """## Description
2026-09-24 03:00: a DNS blip made `publish plan/F-0101 refused`.

## Acceptance
- [ ] A push that fails with a transient network error is retried (tests.test_health.PushRetryTests).
- [ ] Only a non-transient refusal is a failure.

## History
- 2026-09-24: created (inbox) — shape: signature → bug
"""

#: T-0453's body: Files lines copied from its plan — never a source of proof
TASK_BODY = """## Description
writes: asf/env.py, tests/test_env.py

**Files**

- `tests/test_env.py` — `ConfigSchemaVersionTests`, `TolerantReaderTests`; every existing class
  unchanged in expectation
"""

GREEN = {'sha': 'a' * 40, 'ran': 3, 'failed': [], 'ok': True}
RED = {'sha': 'a' * 40, 'ran': 3, 'failed': ['tests.test_health.PushRetryTests.test_a'],
       'ok': False}
IDS = ['tests.test_health.PushRetryTests']


def cfg(**kw):
    kw.setdefault('needed_satisfied', True)
    kw.setdefault('cancel_stale_ci', True)
    return B.config(**kw)


def bug(**kw):
    kw.setdefault('body', BUG_BODY)
    return B.task('B-0097', type='bug', **kw)


class ProvingTests(unittest.TestCase):

    def test_acceptance_ids_count_and_plan_copied_files_lines_do_not(self):
        self.assertEqual(N.test_ids(bug()), IDS)
        self.assertEqual(N.test_ids(B.task('T-0453', body=TASK_BODY,
                                           writes=['asf/env.py', 'tests/test_env.py'])), [])

    def test_proves_lines_stories_and_a_bugs_fixes_and_signature(self):
        story = B.item('S-1', body='## Acceptance\n- [ ] tests.test_s.STests.test_one is green\n')
        t = B.task('T-1', stories=['S-1'], body='proves: tests/test_a.py::ATests\n')
        self.assertEqual(N.test_ids(t, {'S-1': story, 'T-1': t}),
                         ['tests.test_a.ATests', 'tests.test_s.STests.test_one'])
        b = B.task('B-1', type='bug', body='Fixes: tests.test_b.BTests\n',
                   signature='tests.test_c.CTests red')
        self.assertEqual(N.test_ids(b), ['tests.test_b.BTests', 'tests.test_c.CTests'])

    def test_an_id_another_extends_is_dropped(self):
        t = B.task('T-1', body='## Acceptance\n- tests.test_a.ATests and tests.test_a.ATests.test_x\n')
        self.assertEqual(N.test_ids(t), ['tests.test_a.ATests.test_x'])


class Satisfied(unittest.TestCase):

    def plan(self, probe, **kw):
        it = kw.pop('item', None) or bug()
        needed = {it.id: dict({'ids': IDS, 'baseline': 'b' * 40}, **probe)}
        return D.decide(B.facts([it], needed=needed, **kw), cfg())

    def test_green_on_main_is_done_and_nothing_launches(self):
        plan = self.plan({'tests': GREEN})
        self.assertEqual(B.state(plan, 'B-0097'), State.DONE)
        self.assertEqual(B.launched(plan), [])
        sat = B.of(plan, A.Satisfied)
        self.assertEqual((sat[0].item_id, sat[0].sha, sat[0].tests), ('B-0097', 'a' * 40, IDS))
        self.assertEqual(N.gate_line(plan.gate[0]),
                         'SATISFIED B-0097: satisfied on main at aaaaaaaaa: '
                         'tests.test_health.PushRetryTests')

    def test_a_relaunch_after_an_answer_is_checked_too(self):
        it = bug(attempts=[D.RELAUNCH + 'operator answer: go'], findings=['old'])
        plan = self.plan({'tests': GREEN}, item=it)
        self.assertEqual((B.state(plan, 'B-0097'), B.launched(plan)), (State.DONE, []))

    def test_red_or_unloadable_launches_as_before(self):
        for result in (RED, {'error': 'a test id does not load at trunk'}):
            plan = self.plan({'tests': result})
            self.assertEqual(B.launched(plan), ['B-0097'])
            self.assertEqual(B.of(plan, A.Satisfied), [])

    def test_a_check_not_run_yet_holds_the_launch_one_tick(self):
        plan = self.plan({})
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(B.state(plan, 'B-0097'), State.READY)
        self.assertIn(N.PENDING, plan.notes['B-0097'])
        s = L.summarize(plan, B.facts([bug()]))
        self.assertEqual(s['pending'], ['B-0097'])

    def test_off_launches_without_looking(self):
        it = bug()
        f = B.facts([it], needed={it.id: {'ids': IDS, 'tests': GREEN}})
        self.assertEqual(B.launched(D.decide(f, cfg(needed_satisfied=False))), ['B-0097'])

    def test_an_item_with_an_open_pr_or_a_live_session_is_not_gated(self):
        it = bug()
        needed = {it.id: {'ids': IDS, 'tests': GREEN}}
        f = B.facts([it], needed=needed, sessions=[B.session('build-b', 'B-0097', alive=True)])
        self.assertEqual(B.of(D.decide(f, cfg()), A.Satisfied), [])
        f = B.facts([it], needed=needed, prs=[B.pr(7, 'B-0097', branch='fix/B-0097')])
        self.assertEqual(B.of(D.decide(f, cfg()), A.Satisfied), [])

    def test_the_applier_writes_done_and_the_note(self):
        it = bug()
        f = B.facts([it], needed={it.id: {'ids': IDS, 'tests': GREEN}})
        plan = D.decide(f, cfg())
        record = F.FakeRecord([it])
        AP.apply(plan, f, F.ports(record=record), log=lambda _l: None)
        fields = record.fields['B-0097']
        self.assertEqual(fields['kernel_state'], 'done')
        self.assertEqual(fields['kernel_notes'],
                         ['satisfied on main at aaaaaaaaa: tests.test_health.PushRetryTests'])


def done_session(**fields):
    base = {'status': 'done', 'pushed': 'no — nothing to change',
            'commits': 'none — the fix landed in e48caa821',
            'tests': 'python3 -m unittest tests.test_health.PushRetryTests -> OK (3 tests)'}
    base.update(fields)
    return B.session('build-b-0097-1', 'B-0097', alive=False, ended=True, status='done',
                     fields=base, unpushed='c' * 40, branch='fix/B-0097')


class FoundDoneBySession(unittest.TestCase):

    def test_a_done_report_with_a_landed_sha_and_green_tests_is_done(self):
        s = done_session()
        it = bug(state=State.BUILDING)
        f = B.facts([it], sessions=[s], landed_shas={'e48caa821': 'e' * 40})
        plan = D.decide(f, cfg())
        self.assertEqual(B.state(plan, 'B-0097'), State.DONE)
        self.assertEqual([(a.item_id, a.sha, a.tests) for a in B.of(plan, A.Satisfied)],
                         [('B-0097', 'e' * 40, IDS)])
        self.assertEqual((B.of(plan, A.MarkStuck), B.of(plan, A.OpenPR), B.launched(plan)),
                         ([], [], []))
        sessions = F.FakeSessions([s])
        AP.apply(plan, f, F.ports(record=F.FakeRecord([it]), sessions=sessions),
                 log=lambda _l: None)
        self.assertEqual(sessions.pushed, [])  # no empty commit published

    def test_a_sha_off_main_red_tests_or_a_push_are_judged_as_before(self):
        it = bug(state=State.BUILDING)
        for s, landed in ((done_session(), {'e48caa821': ''}),
                          (done_session(tests='FAILED (failures=1)'), {'e48caa821': 'e' * 40}),
                          (done_session(pushed='yes ' + 'c' * 40), {'e48caa821': 'e' * 40})):
            plan = D.decide(B.facts([it], sessions=[s], landed_shas=landed), cfg())
            self.assertEqual(B.of(plan, A.Satisfied), [])


class EmptyPR(unittest.TestCase):

    def facts(self, probe=None):
        it = bug(state=State.REVIEW)
        pr = B.pr(1308, 'B-0097', branch='fix/B-0097', files=[], head='h1')
        pr.files = []
        return B.facts([it], prs=[pr], needed={'B-0097': probe} if probe else {})

    def test_closed_and_relaunched_with_a_finding_when_not_green_on_main(self):
        plan = D.decide(self.facts(), cfg())
        self.assertEqual([(a.pr, a.reason) for a in B.of(plan, A.ClosePR)],
                         [(1308, 'it changes nothing (an empty diff)')])
        self.assertEqual(B.launched(plan), [])  # no review of nothing
        clear = B.of(plan, A.ClearStuck)
        self.assertTrue(clear[0].attempt.startswith(D.RELAUNCH + 'PR #1308 changed nothing'))
        self.assertEqual(B.state(plan, 'B-0097'), State.READY)

    def test_closed_and_done_when_its_tests_are_green_on_main(self):
        plan = D.decide(self.facts({'ids': IDS, 'tests': GREEN}), cfg())
        self.assertEqual(len(B.of(plan, A.ClosePR)), 1)
        self.assertEqual(B.state(plan, 'B-0097'), State.DONE)
        self.assertEqual(len(B.of(plan, A.Satisfied)), 1)

    def test_a_pr_whose_ci_has_not_reported_is_left(self):
        f = self.facts()
        f.prs[0].checks = [B.check(status='queued')]
        self.assertEqual(B.of(D.decide(f, cfg()), A.ClosePR), [])


def run(rid, sha, prs, event='pull_request'):
    return M.CIRun(run_id=rid, head_sha=sha, branch='worker/T-1', event=event, prs=list(prs))


class StaleCI(unittest.TestCase):

    def test_closed_pr_and_superseded_head_are_cancelled_the_current_head_kept(self):
        f = B.facts([], open_heads={5: 'new'},
                    ci_runs=[run(1, 'old', [5]), run(2, 'new', [5]), run(3, 'x', [9]),
                             run(4, 'y', [], 'pull_request'), run(5, 'z', [9], 'push')])
        got = N.stale_runs(f, cfg())
        self.assertEqual([(a.run_id, a.why) for a in got],
                         [(1, "head old is no longer PR #5's head"), (3, 'PR #9 closed')])

    def test_unread_heads_or_off_cancel_nothing_and_closing_now_counts(self):
        f = B.facts([], open_heads=None, ci_runs=[run(1, 'old', [5])])
        self.assertEqual(N.stale_runs(f, cfg()), [])
        f = B.facts([], open_heads={5: 'new'}, ci_runs=[run(1, 'new', [5])])
        self.assertEqual(N.stale_runs(f, cfg(cancel_stale_ci=False)), [])
        self.assertEqual([a.run_id for a in N.stale_runs(f, cfg(), closing={5})], [1])

    def test_decide_cancels_and_the_applier_calls_the_port(self):
        f = B.facts([], open_heads={5: 'new'}, ci_runs=[run(11, 'old', [5])])
        plan = D.decide(f, cfg())
        gh = F.FakeGitHub()
        AP.apply(plan, f, F.ports(github=gh), log=lambda _l: None)
        self.assertIn(('cancel_run', 11), gh.calls)

    def test_runs_are_read_before_the_prs(self):
        order = []

        class GH(F.FakeGitHub):
            open_heads = {5: 'new'}

            def active_runs(self):
                order.append('runs')
                return [run(1, 'old', [5])]

            def prs(self):
                order.append('prs')
                return []

        f = K.read_facts(F.ports(github=GH()))
        self.assertEqual(order[:2], ['runs', 'prs'])
        self.assertEqual((len(f.ci_runs), f.open_heads), (1, {5: 'new'}))


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Probe(unittest.TestCase):
    """The host's reads on a real repo: only ids new since the plan count, and they run on the
    trunk through the trunk probe."""

    def write(self, path, text, msg):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', msg)
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        _git(self.tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        _git(self.tmp, 'clone', '-q', origin, self.repo)
        for k, v in (('user.email', 't@example.com'), ('user.name', 't')):
            _git(self.repo, 'config', k, v)
        self.write('tests/__init__.py', '', 'init')
        self.write('tests/test_x.py', 'import unittest\n\n\nclass OldTests(unittest.TestCase):\n'
                   '    def test_a(self):\n        pass\n', 'old tests')
        self.write('docs/plans/f-1.md', 'plan\n', 'the plan')
        self.write('tests/test_x.py', 'import unittest\n\n\nclass OldTests(unittest.TestCase):\n'
                   '    def test_a(self):\n        pass\n\n\nclass NewTests(unittest.TestCase):\n'
                   '    def test_b(self):\n        pass\n', 'the work')
        _git(self.repo, 'fetch', '-q', 'origin')
        self.trunk = T.TrunkProbe(self.repo, 'main', os.path.join(self.tmp, 'state'),
                                  python=sys.executable, timeout_s=60)

    def card(self, ids):
        return B.task('T-1', plan='docs/plans/f-1.md', created='2026-01-01',
                      body='## Acceptance\n' + ''.join('- %s green\n' % i for i in ids))

    def test_only_tests_new_since_the_plan_count_and_they_run_on_the_trunk(self):
        it = self.card(['tests.test_x.OldTests', 'tests.test_x.NewTests'])
        got = NP.NeededProbe(self.trunk).read({'T-1': it})
        self.assertEqual(got['T-1']['ids'], ['tests.test_x.NewTests'])
        self.assertTrue(got['T-1']['tests']['ok'])
        self.assertEqual(N.satisfied(got['T-1'])[1], ['tests.test_x.NewTests'])

    def test_a_card_naming_only_old_tests_or_missing_ones_is_not_probed(self):
        for ids in (['tests.test_x.OldTests'], ['tests.test_x.NewTests', 'tests.test_y.Gone']):
            self.assertEqual(NP.NeededProbe(self.trunk).read({'T-1': self.card(ids)}), {})

    def test_the_landed_shas_a_done_report_names(self):
        head = _git(self.repo, 'rev-parse', 'HEAD')
        s = done_session(commits='none — landed in %s' % head[:9], pushed='no')
        got = NP.NeededProbe(self.trunk).landed([s])
        self.assertEqual(got[head[:9]], head)
        s = done_session(commits='none — see deadbee', pushed='no')
        self.assertEqual(NP.NeededProbe(self.trunk).landed([s]), {'deadbee': ''})

    def test_the_cap_is_shared_with_the_questions_of_the_same_tick(self):
        q = R.Probe(R.TRUNK_TESTS, ('tests.test_x.OldTests',))
        self.trunk.probe([q])
        got = NP.NeededProbe(self.trunk).read({'T-1': self.card(['tests.test_x.NewTests'])})
        self.assertNotIn('tests', got['T-1'])  # the one run this tick went to the question


if __name__ == '__main__':
    unittest.main()
