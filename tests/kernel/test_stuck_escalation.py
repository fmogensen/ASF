"""Stuck never sits (``kernel.stuck``): a Stuck the kernel can resolve by its reason class is
resolved on the tick it appears (``escalate_after_h`` / ``rebuild_after_h``, default 0) — a red
off the PR gets one more rerun then a fix round; a stopped session one relaunch with its report;
the fix-round cap one extra round on the strong model, then a rebuild; a conflict the rebase
could not resolve a rebuild (archive the branch, close the PR, Ready afresh, once per item). A
question for the operator is never resolved by the kernel, and status puts it on top."""
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import ports as P
from asf.kernel import settings
from asf.kernel import status
from asf.kernel.apply import apply
from asf.kernel.decide import decide

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
NOW = '2026-10-10T12:00:00Z'
STRONG = 'claude-opus-5'


def cfg(esc=0, rebuild=0, **kw):
    return B.config(escalate_after_h=esc, rebuild_after_h=rebuild, strong_model=STRONG, **kw)


def facts(items, **kw):
    kw.setdefault('now', NOW)
    return B.facts(items, **kw)


def ago(hours):
    return '2026-10-10T%02d:00:00Z' % (12 - hours)


def red(attempt):
    return B.check('tests', 'failure', run_id=77, attempt=attempt, failing_files=['tests/x.py'],
                   failed_step='unit', log_tail='FAIL: test_x')


class Settings(unittest.TestCase):

    def test_the_stuck_block_defaults_to_zero_hours_and_the_strong_model(self):
        block = settings.read(None)['stuck']
        self.assertEqual(block, {'escalate_after_h': 0, 'rebuild_after_h': 0,
                                 'strong_model': settings.HEAVY_MODEL})
        self.assertEqual(settings.read({'stuck': {'escalate_after_h': 2}})['stuck']
                         ['escalate_after_h'], 2)
        errors, _ = settings.problems({'stuck': {'rebuild_after_h': -1}})
        self.assertEqual([k for k, _ in errors], ['kernel.stuck.rebuild_after_h'])

    def test_the_product_config_carries_them(self):
        c = P.config_for(env.Product('sample', {'kernel': {'stuck': {'escalate_after_h': 2,
                                                                      'rebuild_after_h': 6}}}),
                         github=F.FakeGitHub())
        self.assertEqual((c.escalate_after_h, c.rebuild_after_h, c.strong_model),
                         (2.0, 6.0, settings.HEAVY_MODEL))
        c = P.config_for(env.Product('sample', {}), github=F.FakeGitHub())
        self.assertEqual((c.escalate_after_h, c.rebuild_after_h), (0.0, 0.0))


class RedOffThePR(unittest.TestCase):

    def plan(self, attempt, it=None, config=None):
        it = it or B.task('T-0001', state=State.REVIEW)
        return decide(facts([it], prs=[B.pr(7, 'T-0001', checks=[red(attempt)])]),
                      config or cfg())

    def test_one_more_rerun_then_a_fix_round_on_the_same_tick(self):
        plan = self.plan(2)  # max_reruns (1) spent: one more
        self.assertEqual([a.run_id for a in B.of(plan, A.Rerun)], [77])
        self.assertNotEqual(B.state(plan, 'T-0001'), State.STUCK)
        plan = self.plan(3)  # the extra rerun spent too: the PR's to fix
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        [launch] = B.of(plan, A.Launch)
        self.assertEqual((launch.kind, launch.branch), ('build', 'worker/T-0001'))
        self.assertTrue(launch.findings[0].startswith('red: tests'), launch.findings)

    def test_off_the_rule_waits_on_ci_as_before(self):
        plan = self.plan(2, config=B.config())
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'ci')

    def test_with_hours_set_a_recorded_red_waits_until_it_is_that_old(self):
        def recorded(hours):
            return B.task('T-0001', state=State.STUCK, stuck_since=ago(hours),
                          stuck=B.M.Stuck(D.RED_OFF + '1 rerun(s): tests', 'ci'))
        plan = self.plan(2, recorded(1), cfg(esc=2))
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'ci')
        self.assertEqual(B.of(plan, A.Rerun), [])
        plan = self.plan(2, recorded(3), cfg(esc=2))
        self.assertEqual([a.run_id for a in B.of(plan, A.Rerun)], [77])


class StoppedSession(unittest.TestCase):

    def ended(self, iid='T-0001'):
        return B.session('j1', iid, alive=False, ended=True, result='report', status='partial',
                         fields={'status': 'partial', 'left out': 'the migration test'})

    def test_a_partial_session_is_relaunched_once_with_its_report(self):
        it = B.task('T-0001', state=State.BUILDING)
        plan = decide(facts([it], sessions=[self.ended()]), cfg())
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        [clear] = B.of(plan, A.ClearStuck)
        self.assertTrue(clear.attempt.startswith(D.RELAUNCH + D.ESCALATED))
        self.assertIn('the migration test', clear.attempt)
        self.assertEqual(B.of(plan, A.Launch), [], 'its session ended this tick: next tick')
        nxt = B.task('T-0001', state=State.READY, attempts=[clear.attempt])
        [launch] = B.of(decide(facts([nxt]), cfg()), A.Launch)
        self.assertEqual(launch.findings, [clear.attempt[len(D.RELAUNCH):]])

    def test_the_second_stop_is_stuck_on_the_session(self):
        marker = D.RELAUNCH + D.ESCALATED + 'x'
        it = B.task('T-0001', state=State.BUILDING, attempts=[marker])
        plan = decide(facts([it], sessions=[self.ended()]), cfg())
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'session')
        self.assertEqual(B.of(plan, A.ClearStuck), [])

    def test_a_recorded_session_stuck_is_relaunched_once_it_is_old_enough(self):
        def recorded(hours):
            return B.task('T-0001', state=State.STUCK, stuck_since=ago(hours),
                          stuck=B.M.Stuck('partial: the migration test', 'session'))
        plan = decide(facts([recorded(1)]), cfg(esc=2))
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        plan = decide(facts([recorded(3)]), cfg(esc=2))
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(len(B.of(plan, A.ClearStuck)), 1)

    def test_a_done_stuck_whose_branch_is_pushed_still_gets_its_pr_not_a_relaunch(self):
        it = B.task('T-0001', state=State.STUCK, stuck_since=ago(3),
                    stuck=B.M.Stuck(D.NO_PUSH_STUCK + 'yes abc1234', 'session'))
        plan = decide(facts([it], branches=[B.M.Branch('worker/T-0001', 'T-0001', 'abc1234')]),
                      cfg())
        self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['worker/T-0001'])
        self.assertEqual(B.of(plan, A.ClearStuck), [])


class FixRoundCap(unittest.TestCase):

    def world(self, **kw):
        it = B.task('T-0001', state=State.REVIEW, fix_rounds=2, findings=['older finding'], **kw)
        pr = B.pr(7, 'T-0001', head='head-7')
        return facts([it], prs=[pr], reviews=[B.review('T-0001', verdict='changes',
                                                       findings=['newest finding'])])

    def test_the_cap_gets_one_more_round_on_the_strong_model_with_every_finding(self):
        plan = decide(self.world(), cfg())
        self.assertEqual([a.attempt for a in B.of(plan, A.ClearStuck)], [D.STRONG_ROUND])
        [launch] = B.of(plan, A.Launch)
        self.assertEqual((launch.branch, launch.model), ('worker/T-0001', STRONG))
        self.assertIn('older finding', launch.findings)
        self.assertEqual(B.of(plan, A.ArchiveAndReset), [])

    def test_when_the_strong_round_fails_too_the_item_is_rebuilt_once(self):
        plan = decide(facts([B.task('T-0001', state=State.REVIEW, fix_rounds=3,
                                    attempts=[D.STRONG_ROUND])],
                            prs=[B.pr(7, 'T-0001', head='head-7')],
                            reviews=[B.review('T-0001', verdict='changes')]), cfg())
        [rebuild] = B.of(plan, A.ArchiveAndReset)
        self.assertEqual((rebuild.pr, rebuild.branch, rebuild.head_sha), (7, 'worker/T-0001',
                                                                          'head-7'))
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)
        self.assertEqual(B.of(plan, A.Launch), [])
        again = facts([B.task('T-0001', state=State.REVIEW, fix_rounds=3, rebuilds=1,
                              attempts=[D.STRONG_ROUND])],
                      prs=[B.pr(7, 'T-0001')], reviews=[B.review('T-0001', verdict='changes')])
        plan = decide(again, cfg())
        self.assertEqual(B.of(plan, A.ArchiveAndReset), [])
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')

    def test_off_the_rule_the_cap_waits_on_the_operator_as_before(self):
        plan = decide(self.world(), B.config())
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')
        self.assertEqual(B.of(plan, A.ClearStuck), [])


class Conflict(unittest.TestCase):

    def world(self, **kw):
        kw.setdefault('state', State.REVIEW)
        it = B.task('T-0001', findings=[D.rebase_finding_for(7)], **kw)
        return facts([it], prs=[B.pr(7, 'T-0001', head='head-7', conflicting=True)])

    def test_a_conflict_the_rebase_could_not_resolve_is_rebuilt_on_the_tick(self):
        plan = decide(self.world(), cfg())
        [rebuild] = B.of(plan, A.ArchiveAndReset)
        self.assertEqual(rebuild.pr, 7)
        self.assertIn('could not resolve', rebuild.reason)
        self.assertEqual(B.state(plan, 'T-0001'), State.READY)

    def test_once_per_item(self):
        plan = decide(self.world(rebuilds=1), cfg())
        self.assertEqual(B.of(plan, A.ArchiveAndReset), [])
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')

    def test_with_hours_set_a_recorded_conflict_is_rebuilt_once_it_is_that_old(self):
        reason = ('conflict the rebase session could not resolve: PR #7 still conflicts on '
                  'head head-7')
        for hours, rebuilt in ((1, 0), (7, 1)):
            plan = decide(self.world(state=State.STUCK, stuck=B.M.Stuck(reason, 'operator'),
                                     stuck_since=ago(hours)), cfg(rebuild=6))
            self.assertEqual(len(B.of(plan, A.ArchiveAndReset)), rebuilt, hours)


class Questions(unittest.TestCase):

    def test_an_operator_question_is_never_resolved_by_the_kernel(self):
        asked = B.task('T-0001', state=State.STUCK, stuck_since=ago(10),
                       stuck=B.M.Stuck('done: NEEDS OPERATOR: which key?', 'operator'))
        plan = decide(facts([asked, B.task('T-0002', question='which id?')]), cfg())
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')
        self.assertEqual(B.stuck(plan, 'T-0002').owner, 'operator')
        for cls in (A.ClearStuck, A.ArchiveAndReset, A.Launch):
            self.assertEqual(B.of(plan, cls), [], cls.__name__)

    def test_status_puts_them_on_top_with_their_age(self):
        rows = [('F-0001', 'done: NEEDS OPERATOR: which key?', 'operator', 0, '3h'),
                ('T-0002', 'red off the PR after 1 rerun(s): tests', 'ci', 0, '5h')]
        text = status.render(rows, {}, [], needs_after_h=0)
        self.assertTrue(text.startswith('## Needs you'))
        self.assertIn('| F-0001 | 3h | done: NEEDS OPERATOR: which key? |', text)
        self.assertNotIn('| T-0002 | 5h', text.split('## Stuck')[0])
        self.assertFalse(status.render(rows, {}, [], needs_after_h=4).startswith('## Needs you'))
        self.assertTrue(status.render(rows[:1], {}, [], needs_after_h=2).startswith('## Needs'))

    def test_the_status_command_shows_the_block_from_minute_zero(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.STUCK,
                                   stuck=B.M.Stuck('NEEDS OPERATOR: which key?', 'operator'))])
        rec.fields['T-0001'] = {P.STATE: 'stuck', P.STUCK_REASON: 'NEEDS OPERATOR: which key?',
                                P.STUCK_OWNER: 'operator', P.STUCK_SINCE: P.now_iso()}
        text = status.status(env.Product('sample', {}), ports=F.ports(record=rec), config=cfg(),
                             out=lambda _t: None, live=True)
        self.assertTrue(text.startswith('## Needs you'))
        self.assertIn('| T-0001 | 0h |', text)


class Apply(unittest.TestCase):

    def test_archive_and_reset_archives_closes_deletes_and_clears_the_card(self):
        it = B.task('T-0001', state=State.STUCK, fix_rounds=3, extra_rounds=1,
                    attempts=[D.STRONG_ROUND], findings=['f'],
                    stuck=B.M.Stuck('review: changes requested after 3 fix rounds', 'operator'))
        rec = F.FakeRecord([it])
        rec.fields['T-0001'] = {P.STATE: 'stuck', P.STUCK_OWNER: 'operator',
                                P.STUCK_REASON: it.stuck.reason, P.FIX_ROUNDS: 3,
                                P.EXTRA_ROUNDS: 1, P.ATTEMPTS: [D.STRONG_ROUND],
                                P.FINDINGS: ['f']}
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', head='head-7')])
        ports = F.ports(record=rec, github=gh)
        f = facts(list(rec.items().values()), prs=gh.prs())
        plan = A.Plan(states={'T-0001': (State.READY, None)}, actions=[
            A.ArchiveAndReset('T-0001', 7, 'worker/T-0001', 'head-7', 'the cap')])
        result = apply(plan, f, ports, log=lambda _t: None)
        self.assertEqual(result.failed, [])
        [(pr, branch, head, comment)] = gh.archived
        self.assertEqual((pr, branch, head), (7, 'worker/T-0001', 'head-7'))
        self.assertIn('archive/worker/T-0001', comment)
        card = rec.card_fields('T-0001')
        self.assertEqual(card.get(P.REBUILDS), 1)
        self.assertEqual(card.get(P.STATE), 'ready')
        for key in (P.ATTEMPTS, P.FINDINGS, P.STUCK_REASON):
            self.assertNotIn(key, card)
        self.assertEqual((card.get(P.FIX_ROUNDS), card.get(P.EXTRA_ROUNDS)), (0, 0))

    def test_the_next_tick_builds_it_fresh_from_the_trunk(self):
        it = B.task('T-0001', state=State.READY, rebuilds=1)
        [launch] = B.of(decide(facts([it]), cfg()), A.Launch)
        self.assertEqual((launch.kind, launch.branch, launch.model), ('build', 'worker/T-0001', ''))

    def test_a_launch_model_overrides_the_kind_model_in_the_brief(self):
        try:
            from kernel.test_speedups import product
            from kernel.test_go_live import _briefer
        except ImportError:  # pragma: no cover - import shape only
            from tests.kernel.test_speedups import product
            from tests.kernel.test_go_live import _briefer
        item = B.task('T-0001', state=State.REVIEW)
        launch = A.Launch('build', 'T-0001', 'worker/T-0001', [], STRONG)
        b = _briefer(product())(item, launch, [], B.pr(7, 'T-0001'))
        self.assertEqual(b.model, STRONG)


if __name__ == '__main__':
    unittest.main()
