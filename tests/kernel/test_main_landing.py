"""Landing without "require branches to be up to date" (2026-10-10: the operator turned the
ruleset's strict flag off; GitHub merges a green PR behind its base).

- The strict flag is a fact read off the trunk's rules each tick: strict keeps the merge train;
  not strict, a merely BEHIND PR is never updated (that only burns CI) and nothing queues, while a
  DIRTY one still goes to its rebase session.
- A PR green, CLEAN and approved whose enabled auto-merge has not fired is merged directly
  (``MERGE direct #n (auto-merge idle)``, ``--match-head-commit``).
- The main safety net: a red trunk new since its last green commit reverts its one candidate PR
  (the item back to Ready) or files a Bug for a fix session naming the candidates; an infra or
  flaky red is rerun once first. Every red tick logs ``MAIN RED <sha> -> <action>``.
"""
import json
import subprocess
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop, mainline, settings, waits
from asf.kernel import ports as P
from asf.kernel.apply import apply
from asf.kernel.decide import decide, rebase_finding_for, RELAUNCH
from asf.kernel.facts import read_facts
from asf.kernel.model import MainCommit

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
REQ = ('tests (3.12)', 'tests (3.13)')


def cfg(**kw):
    kw.setdefault('required_checks', REQ)
    kw.setdefault('main_red_revert', True)
    return B.config(**kw)


def green(run_id=1):
    return [B.check(n, run_id=run_id) for n in REQ]


def red(run_id=7, attempt=1, files=(), tail=''):
    return [B.check(REQ[0], 'failure', run_id=run_id, attempt=attempt,
                    failing_files=list(files), log_tail=tail),
            B.check(REQ[1], run_id=run_id)]


def commit(sha, pr=None, item='', checks=None, files=('src/a.py',), branch=None):
    return MainCommit(sha=sha, headline='change %s' % sha, pr=pr,
                      branch=branch or ('worker/%s' % item if item else ''), item_id=item,
                      files=list(files), checks=list(checks if checks is not None else green()))


def landing_world(strict, **pr_kw):
    pr_kw.setdefault('behind', True)
    p = B.pr(10, 'T-1', checks=green(), auto_merge=True, **pr_kw)
    return B.facts([B.task('T-1', State.LANDING)], prs=[p], reviews=[B.review('T-1')],
                   strict=strict)


class Strict(unittest.TestCase):

    def test_strict_on_a_behind_pr_is_updated_by_the_train(self):
        plan = decide(landing_world(True), cfg())
        self.assertEqual([a.pr for a in B.of(plan, A.UpdateBranch)], [10])

    def test_strict_off_a_behind_pr_is_never_updated_and_never_queued(self):
        f = landing_world(False)
        plan = decide(f, cfg(update_parallel=0))
        self.assertEqual(B.of(plan, A.UpdateBranch), [])
        self.assertEqual(B.state(plan, 'T-1'), State.LANDING)
        self.assertEqual(plan.notes, {})
        self.assertNotIn('T-1', plan.limbo)
        self.assertEqual(waits.classify('T-1', State.LANDING, None, f, cfg())[0], 'merge')

    def test_strict_off_a_dirty_pr_still_gets_its_rebase_session(self):
        f = landing_world(False, conflicting=True)
        plan = decide(f, cfg())
        self.assertEqual(B.of(plan, A.UpdateBranch), [])
        [launch] = B.of(plan, A.Launch)
        self.assertEqual(launch.branch, 'worker/T-1')
        self.assertIn(rebase_finding_for(10), launch.findings)

    def test_strict_off_a_green_pr_idle_past_its_merge_target_is_not_updated(self):
        try:
            from kernel import test_limbo as L
        except ImportError:  # pragma: no cover - import shape only
            from tests.kernel import test_limbo as L
        for strict, updated in ((True, [7]), (False, [])):
            pr, rv = L.approved('T-1', 7, auto_merge=True)
            f = L.facts([B.task('T-1', state=State.LANDING)], prs=[pr], reviews=[rv],
                        waits={'T-1': ('merge', L.ago(25))}, strict=strict)
            plan = decide(f, L.config())
            self.assertEqual([a.pr for a in B.of(plan, A.UpdateBranch)], updated)

    def test_strict_is_read_off_the_rules_once_with_the_required_checks(self):
        rules = [{'type': 'required_status_checks', 'parameters': {
            'strict_required_status_checks_policy': False,
            'required_status_checks': [{'context': c} for c in REQ]}}]
        calls = []

        def run(argv, **kw):
            calls.append(argv[1:3])
            if argv[1:3] == ['api', 'repos/o/r/rules/branches/main']:
                return subprocess.CompletedProcess(argv, 0, json.dumps(rules), '')
            return subprocess.CompletedProcess(argv, 1, '', 'unexpected')
        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run)
        self.assertEqual(gh.required_checks(), REQ)
        self.assertFalse(gh.strict())
        self.assertEqual(calls.count(['api', 'repos/o/r/rules/branches/main']), 1)
        rules[0]['parameters']['strict_required_status_checks_policy'] = True
        self.assertTrue(P.strict_from_rules(rules))
        self.assertFalse(P.strict_from_rules([{'type': 'deletion'}]))

    def test_unreadable_rules_keep_the_train(self):
        f = read_facts(F.ports(F.FakeRecord([B.task('T-1')]), F.FakeGitHub()))
        self.assertTrue(f.strict)
        f = read_facts(F.ports(F.FakeRecord([B.task('T-1')]), F.FakeGitHub(strict=False)))
        self.assertFalse(f.strict)

    def test_the_knob_defaults_on(self):
        self.assertTrue(settings.read(None)['landing']['main_red_revert'])
        self.assertFalse(settings.read({'landing': {'main_red_revert': False}})['landing'][
            'main_red_revert'])
        self.assertTrue(P.config_for(env.Product('sample', {})).main_red_revert)
        self.assertEqual(P.config_for(env.Product('sample', {})).revert_branch, 'revert/')


class DirectMerge(unittest.TestCase):

    def test_an_idle_auto_merge_on_a_clean_green_pr_is_merged_directly(self):
        f = landing_world(False, behind=False, clean=True)
        plan = decide(f, cfg())
        self.assertEqual(B.of(plan, A.MergePR), [A.MergePR(10, 'head-1')])
        self.assertEqual(A.describe(B.of(plan, A.MergePR)[0]),
                         'MERGE direct #10 (auto-merge idle)')
        gh = F.FakeGitHub(prs=f.prs)
        apply(plan, f, F.ports(F.FakeRecord(list(f.items.values())), gh), log=lambda _l: None)
        self.assertEqual(gh.merged, [(10, 'head-1')])

    def test_no_direct_merge_before_auto_merge_had_a_tick_or_while_not_clean_or_green(self):
        f = landing_world(False, behind=False, clean=True)
        f.prs[0].auto_merge = False
        self.assertEqual(B.of(decide(f, cfg()), A.MergePR), [])
        self.assertEqual(B.of(decide(landing_world(False, behind=False), cfg()), A.MergePR), [])
        f = landing_world(False, behind=False, clean=True)
        f.prs[0].checks = [B.check(REQ[0]), B.check(REQ[1], status='in_progress')]
        self.assertEqual(B.of(decide(f, cfg()), A.MergePR), [])

    def test_the_port_merges_with_match_head_commit(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv[1:])
            return subprocess.CompletedProcess(argv, 0, '', '')
        P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run).merge(10, 'abc')
        self.assertEqual(calls[-1], ['pr', 'merge', '10', '-R', 'o/r', '--squash',
                                     '--match-head-commit', 'abc'])


class MainRed(unittest.TestCase):

    def world(self, main, items=None, **kw):
        items = items or [B.task('T-1', State.DONE), B.task('T-2', State.DONE)]
        prs = [B.pr(20, 'T-1', merged=True), B.pr(21, 'T-2', merged=True)]
        return B.facts(items, prs=prs, main=main, strict=False, **kw)

    def test_green_main_does_nothing(self):
        plan = decide(self.world([commit('c2', 21, 'T-2'), commit('c1', 20, 'T-1')]), cfg())
        self.assertIsNone(plan.main)
        self.assertEqual(B.of(plan, A.RevertPR) + B.of(plan, A.FileBug), [])

    def test_one_culprit_is_reverted_and_its_item_goes_back_to_ready(self):
        f = self.world([commit('c2', 21, 'T-2', red(tail='AssertionError')),
                        commit('c1', 20, 'T-1')])
        plan = decide(f, cfg())
        [rv] = B.of(plan, A.RevertPR)
        self.assertEqual((rv.item_id, rv.pr, rv.sha, rv.branch), ('T-2', 21, 'c2', 'revert/T-2'))
        self.assertEqual(B.state(plan, 'T-2'), State.READY)
        self.assertEqual(B.launched(plan), [])
        self.assertEqual(plan.main['sha'], 'c2')
        out = []
        loop.print_summary(loop.summarize(plan, f), out.append)
        self.assertIn('MAIN RED c2 -> revert #21 of T-2 (c2) on revert/T-2', out)
        # applied: the revert PR with auto-merge, the card reverted, Ready, its finding queued
        rec, gh = F.FakeRecord(list(f.items.values())), F.FakeGitHub(prs=f.prs, main=f.main)
        apply(plan, f, F.ports(rec, gh), log=lambda _l: None)
        self.assertEqual(gh.reverts[0][:2], ('c2', 'revert/T-2'))
        self.assertIn(('auto_merge', 951), gh.calls)
        self.assertEqual(rec.fields['T-2'][P.REVERTED], [21])
        self.assertEqual(rec.fields['T-2'][P.STATE], 'ready')
        self.assertTrue(rec.fields['T-2'][P.ATTEMPTS][-1].startswith(RELAUNCH))
        # the next tick: the reverted merge no longer makes it Done; it relaunches with the red
        f2 = read_facts(F.ports(rec, gh))
        plan2 = decide(f2, cfg())
        self.assertEqual(B.of(plan2, A.RevertPR), [])
        self.assertEqual(plan2.main['action'], 'waits on the revert of #21 (T-2)')
        [launch] = B.of(plan2, A.Launch)
        self.assertEqual(launch.item_id, 'T-2')
        self.assertIn('turned main red', launch.findings[0])

    def test_several_culprits_file_one_bug_for_a_fix_session(self):
        f = self.world([commit('c3', 21, 'T-2', red()), commit('c2', 20, 'T-1', checks=[]),
                        commit('c1', None)])
        plan = decide(f, cfg())
        self.assertEqual(B.of(plan, A.RevertPR), [])
        [bug] = B.of(plan, A.FileBug)
        self.assertIn('PR #21', bug.body)
        self.assertIn('PR #20', bug.body)
        self.assertIn('main-red: c1..c3', bug.body)
        rec = F.FakeRecord(list(f.items.values()))
        apply(plan, f, F.ports(rec, F.FakeGitHub(prs=f.prs)), log=lambda _l: None)
        [bid] = [i for i in rec._items if i.startswith('B-')]
        f2 = read_facts(F.ports(rec, F.FakeGitHub(prs=f.prs, main=f.main)))
        plan2 = decide(f2, cfg())
        self.assertEqual(B.of(plan2, A.FileBug), [])
        self.assertEqual(plan2.main['action'], 'waits on fix session %s' % bid)
        self.assertIn(bid, B.launched(plan2))

    def test_knob_off_files_a_bug_instead_of_reverting(self):
        f = self.world([commit('c2', 21, 'T-2', red()), commit('c1', 20, 'T-1')])
        plan = decide(f, cfg(main_red_revert=False))
        self.assertEqual(B.of(plan, A.RevertPR), [])
        self.assertEqual(len(B.of(plan, A.FileBug)), 1)

    def test_an_infra_or_flaky_red_is_rerun_once_first(self):
        infra = red(tail='##[error]The runner has received a shutdown signal.')
        plan = decide(self.world([commit('c2', 21, 'T-2', infra), commit('c1', 20, 'T-1')]),
                      cfg())
        self.assertEqual([a.run_id for a in B.of(plan, A.Rerun)], [7])
        self.assertEqual(B.of(plan, A.RevertPR), [])
        off = red(files=['tests/test_elsewhere.py'])
        plan = decide(self.world([commit('c2', 21, 'T-2', off), commit('c1', 20, 'T-1')]), cfg())
        self.assertEqual([a.run_id for a in B.of(plan, A.Rerun)], [7])
        rerun_red = red(files=['tests/test_elsewhere.py'], attempt=2)
        plan = decide(self.world([commit('c2', 21, 'T-2', rerun_red), commit('c1', 20, 'T-1')]),
                      cfg())
        self.assertEqual(B.of(plan, A.Rerun), [])
        self.assertEqual(len(B.of(plan, A.RevertPR)), 1)

    def test_a_pending_head_over_a_red_commit_still_judges_the_red_and_a_revert_is_not_reverted(
            self):
        pending = [B.check(n, status='in_progress') for n in REQ]
        f = self.world([commit('c3', 99, 'T-2', pending, branch='revert/T-2'),
                        commit('c2', 21, 'T-2', red()), commit('c1', 20, 'T-1')])
        self.assertEqual(len(B.of(decide(f, cfg()), A.RevertPR)), 1)
        f = self.world([commit('v', 99, '', red(), branch='revert/T-2'),
                        commit('c1', 20, 'T-1')])
        plan = decide(f, cfg())
        self.assertEqual(B.of(plan, A.RevertPR), [])
        self.assertEqual(len(B.of(plan, A.FileBug)), 1)

    def test_verdicts(self):
        c = cfg()
        self.assertEqual(mainline.verdict(commit('a'), c), 'green')
        self.assertEqual(mainline.verdict(commit('a', checks=red()), c), 'red')
        self.assertEqual(mainline.verdict(commit('a', checks=[B.check(REQ[0])]), c), 'pending')


class MainPort(unittest.TestCase):

    def test_main_commits_read_in_one_graphql_call(self):
        data = {'data': {'repository': {'ref': {'target': {'history': {'nodes': [
            {'oid': 'c2', 'messageHeadline': 'T-2 thing (#21)',
             'associatedPullRequests': {'nodes': [
                 {'number': 21, 'headRefName': 'worker/T-0002', 'merged': True,
                  'files': {'nodes': [{'path': 'src/a.py'}]}}]},
             'statusCheckRollup': {'contexts': {'nodes': [
                 {'__typename': 'CheckRun', 'name': 'tests (3.12)', 'status': 'COMPLETED',
                  'conclusion': 'SUCCESS', 'detailsUrl': 'https://x/actions/runs/5/job/1'}]}}},
            {'oid': 'c1', 'messageHeadline': 'revert', 'associatedPullRequests': {'nodes': [
                {'number': 22, 'headRefName': 'revert/T-0002', 'merged': True,
                 'files': {'nodes': []}}]}, 'statusCheckRollup': None}]}}}}}}

        def run(argv, **kw):
            if argv[1:3] == ['api', 'graphql']:
                return subprocess.CompletedProcess(argv, 0, json.dumps(data), '')
            return subprocess.CompletedProcess(argv, 1, '', 'unexpected')
        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r'}), run=run)
        c2, c1 = gh.main_commits()
        self.assertEqual((c2.sha, c2.pr, c2.item_id, c2.files), ('c2', 21, 'T-0002', ['src/a.py']))
        self.assertEqual([(k.name, k.conclusion, k.run_id) for k in c2.checks],
                         [('tests (3.12)', 'success', 5)])
        self.assertEqual((c1.pr, c1.item_id), (22, ''))


if __name__ == '__main__':
    unittest.main()
