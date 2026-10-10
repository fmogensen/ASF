"""Waiting for hours without acting is a bug: every non-terminal item has an automatic next action
this tick or a session / CI run in flight (else it is in LIMBO, :func:`asf.kernel.decide.limbo`);
a wait over its class's target takes a breach action (:func:`asf.kernel.decide.breaches`); an
open kernel PR whose item is gone, Done or retired is closed (:class:`asf.kernel.actions.ClosePR`).
"""
import json
import os
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import loop, settings
from asf.kernel import model as M

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
NOW = '2026-10-10T10:00:00Z'
TARGETS = {'seat': 600, 'ci': 600, 'review': 1800, 'train': 1800, 'merge': 600,
           'conflict': 0, 'stuck': 0}


def ago(minutes):
    """The ISO time ``minutes`` before :data:`NOW`."""
    h, m = divmod(10 * 60 - minutes, 60)
    return '2026-10-10T%02d:%02d:00Z' % (h, m)


def facts(items=(), **kw):
    kw.setdefault('now', NOW)
    return B.facts(items, **kw)


def config(**kw):
    kw.setdefault('wait_targets', TARGETS)
    kw.setdefault('close_floor', True)
    return B.config(**kw)


def approved(iid, number, **kw):
    return B.pr(number, iid, **kw), B.review(iid)


class Limbo(unittest.TestCase):

    def test_a_launched_ready_item_is_not_in_limbo(self):
        plan = D.decide(facts([B.task('T-1')]), config())
        self.assertEqual(B.launched(plan), ['T-1'])
        self.assertEqual(plan.limbo, {})

    def test_a_ready_item_queued_for_a_seat_is_not_in_limbo(self):
        items = [B.task('T-1'), B.task('T-2', rank=2)]
        plan = D.decide(facts(items), config(max_sessions=1))
        self.assertEqual(B.launched(plan), ['T-1'])
        self.assertEqual(plan.limbo, {})

    def test_a_building_item_with_a_live_session_is_not_in_limbo(self):
        items = [B.task('T-1', state=State.BUILDING)]
        plan = D.decide(facts(items, sessions=[B.session('j1', 'T-1')]), config())
        self.assertEqual(plan.limbo, {})

    def test_a_loop_stuck_with_no_action_is_in_limbo_with_its_reason(self):
        st = M.Stuck('launch: no account', 'loop')
        items = [B.task('T-1', state=State.STUCK, stuck=st)]
        plan = D.decide(facts(items), config())
        self.assertEqual(list(plan.limbo), ['T-1'])
        self.assertIn('loop', plan.limbo['T-1'])
        self.assertIn('no account', plan.limbo['T-1'])

    def test_an_operator_stuck_waits_on_a_console_question_not_limbo(self):
        st = M.Stuck('NEEDS OPERATOR: which schema?', 'operator')
        plan = D.decide(facts([B.task('T-1', state=State.STUCK, stuck=st)]), config())
        self.assertEqual(plan.limbo, {})

    def test_a_stuck_the_kernel_escalates_this_tick_is_not_in_limbo(self):
        st = M.Stuck('partial: ran out of turns', 'session')
        items = [B.task('T-1', state=State.STUCK, stuck=st)]
        plan = D.decide(facts(items), config(escalate_after_h=0))
        self.assertTrue(B.of(plan, A.ClearStuck))
        self.assertEqual(plan.limbo, {})

    def test_ready_while_paused_is_in_limbo(self):
        plan = D.decide(facts([B.task('T-1')], paused=True), config())
        self.assertIn('paused', plan.limbo['T-1'])

    def test_a_review_with_a_live_reviewer_is_not_in_limbo(self):
        items = [B.task('T-1', state=State.REVIEW)]
        f = facts(items, prs=[B.pr(7, 'T-1')],
                  sessions=[B.session('r1', 'T-1', kind='review')])
        self.assertEqual(D.decide(f, config()).limbo, {})

    def test_landing_with_a_required_check_never_reported_is_in_limbo(self):
        pr, rv = approved('T-1', 7, auto_merge=True, checks=[B.check('lint')])
        f = facts([B.task('T-1', state=State.LANDING)], prs=[pr], reviews=[rv])
        plan = D.decide(f, config(required_checks=('gate',)))
        self.assertEqual(B.state(plan, 'T-1'), State.LANDING)
        self.assertIn('gate', plan.limbo['T-1'])

    def test_landing_with_ci_running_is_not_in_limbo(self):
        pr, rv = approved('T-1', 7, auto_merge=True, checks=[B.check(status='in_progress')])
        f = facts([B.task('T-1', state=State.LANDING)], prs=[pr], reviews=[rv])
        self.assertEqual(D.decide(f, config()).limbo, {})

    def test_a_parked_item_is_never_in_limbo(self):
        items = [B.task('T-1', state=State.STUCK, priority='later',
                        stuck=M.Stuck('x', 'operator'))]
        self.assertEqual(D.decide(facts(items), config()).limbo, {})


class Breach(unittest.TestCase):

    def test_ready_over_target_launches_first(self):
        items = [B.task('T-1', rank=1), B.task('T-2', rank=9)]
        f = facts(items, waits={'T-2': ('seat', ago(25)), 'T-1': ('seat', ago(1))})
        plan = D.decide(f, config(max_sessions=1))
        self.assertEqual(B.launched(plan), ['T-2'])
        self.assertEqual([(b['item'], b['class'], b['action']) for b in plan.breaches],
                         [('T-2', 'seat', 'launch build T-2 on worker/T-2')])
        self.assertEqual(plan.breaches[0]['age_s'], 25 * 60)

    def test_under_target_no_breach(self):
        f = facts([B.task('T-1')], waits={'T-1': ('seat', ago(5))})
        self.assertEqual(D.decide(f, config()).breaches, [])

    def test_a_wait_of_another_class_is_no_breach(self):
        f = facts([B.task('T-1')], waits={'T-1': ('review', ago(90))})
        self.assertEqual(D.decide(f, config()).breaches, [])

    def test_no_targets_no_breach(self):
        f = facts([B.task('T-1')], waits={'T-1': ('seat', ago(90))})
        self.assertEqual(D.decide(f, config(wait_targets={})).breaches, [])

    def test_review_over_target_relaunches_its_review_local_first(self):
        items = [B.task('T-1', state=State.REVIEW), B.task('T-2', state=State.REVIEW)]
        f = facts(items, prs=[B.pr(7, 'T-1'), B.pr(8, 'T-2')],
                  waits={'T-2': ('review', ago(45))})
        plan = D.decide(f, config(max_sessions=1))
        launches = B.of(plan, A.Launch)
        self.assertEqual([(a.kind, a.item_id, a.local) for a in launches],
                         [('review', 'T-2', True)])
        self.assertEqual(plan.breaches[0]['class'], 'review')

    def test_train_over_target_goes_to_the_front_of_the_train(self):
        items, prs, rvs = [], [], []
        for n, iid in enumerate(('T-1', 'T-2', 'T-3'), 1):
            items.append(B.task(iid, state=State.LANDING, rank=n))
            pr, rv = approved(iid, n, behind=True, auto_merge=True)
            prs.append(pr)
            rvs.append(rv)
        f = facts(items, prs=prs, reviews=rvs, waits={'T-3': ('train', ago(40))})
        plan = D.decide(f, config(update_parallel=1))
        self.assertEqual([a.pr for a in B.of(plan, A.UpdateBranch)], [3])
        self.assertEqual([(b['item'], b['action']) for b in plan.breaches],
                         [('T-3', 'update-branch #3')])

    def test_a_green_pr_not_merged_past_its_target_is_updated_first(self):
        pr, rv = approved('T-1', 7, auto_merge=True)  # the listing says clean; GitHub won't merge
        f = facts([B.task('T-1', state=State.LANDING)], prs=[pr], reviews=[rv],
                  waits={'T-1': ('merge', ago(25))})
        plan = D.decide(f, config())
        self.assertEqual([a.pr for a in B.of(plan, A.UpdateBranch)], [7])
        self.assertEqual(plan.breaches[0]['action'], 'update-branch #7')
        self.assertEqual(plan.limbo, {})

    def test_a_session_past_its_max_age_is_ended(self):
        items = [B.task('T-1', state=State.BUILDING)]
        old = B.session('j1', 'T-1', started=ago(200))
        f = facts(items, sessions=[old])
        plan = D.decide(f, config(max_session_age_h=3))
        self.assertEqual(B.of(plan, A.EndSession), [A.EndSession('j1', free_worktree=False)])
        self.assertEqual([(b['item'], b['class'], b['action']) for b in plan.breaches],
                         [('T-1', 'building', 'end session j1')])

    def test_a_hung_review_session_is_ended_and_its_review_relaunched_local_first(self):
        items = [B.task('T-1', state=State.REVIEW)]
        f = facts(items, prs=[B.pr(7, 'T-1')], waits={'T-1': ('review', ago(150))},
                  sessions=[B.session('r1', 'T-1', kind='review', started=ago(100))])
        plan = D.decide(f, config(max_session_age_h=3, max_review_age_h=1.5))
        self.assertEqual(B.of(plan, A.EndSession), [A.EndSession('r1', free_worktree=False)])
        self.assertEqual([(b['item'], b['class']) for b in plan.breaches],
                         [('T-1', 'session:review')])
        f.sessions = []  # the next tick: the reviewer is gone
        again = D.decide(f, config(max_session_age_h=3, max_review_age_h=1.5))
        self.assertEqual([(a.kind, a.local) for a in B.of(again, A.Launch)], [('review', True)])

    def test_a_young_session_is_left_alone(self):
        f = facts([B.task('T-1', state=State.BUILDING)],
                  sessions=[B.session('j1', 'T-1', started=ago(30))])
        plan = D.decide(f, config(max_session_age_h=3))
        self.assertEqual(B.of(plan, A.EndSession), [])

    def test_a_stuck_breach_names_its_escalation(self):
        st = M.Stuck('partial: ran out of turns', 'session')
        items = [B.task('T-1', state=State.STUCK, stuck=st, stuck_since=ago(20))]
        f = facts(items, waits={'T-1': ('stuck:session', ago(20))})
        plan = D.decide(f, config(escalate_after_h=0))
        self.assertEqual(len(plan.breaches), 1)
        self.assertTrue(plan.breaches[0]['action'].startswith('re-judge T-1'))

    def test_an_operator_stuck_breach_says_it_has_no_action(self):
        items = [B.task('T-1', state=State.STUCK, stuck=M.Stuck('which?', 'operator'))]
        f = facts(items, waits={'T-1': ('stuck:operator', ago(20))})
        plan = D.decide(f, config())
        self.assertEqual(plan.breaches[0]['action'], 'none: waits on the operator')


class AfterEdges(unittest.TestCase):
    """A waiter on ``after:`` is only as alive as what it waits on."""

    def test_a_blocker_whose_pr_was_closed_unmerged_is_relaunched(self):
        # its card still says Review, the PR is gone (closed unmerged), no branch on origin
        items = [B.task('T-1', state=State.REVIEW),
                 B.task('T-2', state=State.NEW, after=['T-1'])]
        plan = D.decide(facts(items), config())
        self.assertEqual(B.launched(plan), ['T-1'])
        self.assertEqual(B.state(plan, 'T-2'), State.NEW)
        self.assertEqual(plan.limbo, {})

    def test_a_retired_blocker_stops_blocking(self):
        items = [B.task('T-1', state=State.DONE, priority='later'),
                 B.task('T-2', state=State.NEW, after=['T-1'])]
        plan = D.decide(facts(items), config())
        self.assertEqual(B.launched(plan), ['T-2'])
        self.assertEqual(plan.limbo, {})

    def test_a_blocker_with_no_action_puts_its_waiters_in_limbo(self):
        items = [B.task('T-1', state=State.STUCK, stuck=M.Stuck('launch: no account', 'loop')),
                 B.task('T-2', state=State.NEW, after=['T-1']),
                 B.task('T-3', state=State.NEW, after=['T-2'])]
        plan = D.decide(facts(items), config())
        self.assertEqual(sorted(plan.limbo), ['T-1', 'T-2', 'T-3'])
        self.assertIn('after: T-1', plan.limbo['T-3'])

    def test_an_after_cycle_is_limbo(self):
        items = [B.task('T-1', state=State.NEW, after=['T-2']),
                 B.task('T-2', state=State.NEW, after=['T-1'])]
        plan = D.decide(facts(items), config())
        self.assertEqual(sorted(plan.limbo), ['T-1', 'T-2'])


class MeasuredBounds(unittest.TestCase):
    """Live processes are bounded by the wait ledger's p90s, the knobs only a fallback."""

    def test_a_session_past_the_measured_p90_with_no_push_is_ended(self):
        f = facts([B.task('T-1', state=State.BUILDING)], bounds={'building': 40 * 60},
                  sessions=[B.session('j1', 'T-1', started=ago(50))])
        plan = D.decide(f, config(max_session_age_h=3))
        self.assertEqual(B.of(plan, A.EndSession), [A.EndSession('j1', free_worktree=False)])

    def test_a_session_that_pushed_gets_twice_the_p90(self):
        f = facts([B.task('T-1', state=State.BUILDING)], bounds={'building': 40 * 60},
                  sessions=[B.session('j1', 'T-1', started=ago(50))],
                  branches=[M.Branch('worker/T-1', 'T-1', 'abc')])
        self.assertEqual(B.of(D.decide(f, config(max_session_age_h=3)), A.EndSession), [])
        f.sessions = [B.session('j1', 'T-1', started=ago(81))]
        self.assertEqual(len(B.of(D.decide(f, config(max_session_age_h=3)), A.EndSession)), 1)

    def test_without_a_measure_the_fallback_knob_bounds_it(self):
        f = facts([B.task('T-1', state=State.BUILDING)],
                  sessions=[B.session('j1', 'T-1', started=ago(50))])
        self.assertEqual(B.of(D.decide(f, config(max_session_age_h=3)), A.EndSession), [])

    def test_ci_past_twice_its_p90_is_cancelled_then_rerun(self):
        pr, rv = approved('T-1', 7, auto_merge=True,
                          checks=[B.check('gate', status='in_progress', run_id=55)])
        f = facts([B.task('T-1', state=State.LANDING)], prs=[pr], reviews=[rv],
                  waits={'T-1': ('ci', ago(45))}, bounds={'ci': 20 * 60})
        plan = D.decide(f, config(required_checks=('gate',)))
        self.assertEqual(B.of(plan, A.Rerun), [A.Rerun(55, cancel=True)])
        self.assertEqual([(b['item'], b['class']) for b in plan.breaches], [('T-1', 'ci')])
        self.assertEqual(plan.limbo, {})
        f.prs = [B.pr(7, 'T-1', auto_merge=True,
                      checks=[B.check('gate', conclusion='cancelled', run_id=55)])]
        f.waits = {'T-1': ('merge', ago(1))}
        f.bounds = {}  # the fallback knob alone turns the rerun on too
        self.assertEqual(B.of(D.decide(f, config(required_checks=('gate',), max_ci_age_h=1)),
                              A.Rerun),
                         [A.Rerun(55)])

    def test_ci_within_its_bound_is_left_alone(self):
        pr, rv = approved('T-1', 7, auto_merge=True,
                          checks=[B.check('gate', status='in_progress', run_id=55)])
        f = facts([B.task('T-1', state=State.LANDING)], prs=[pr], reviews=[rv],
                  waits={'T-1': ('ci', ago(30))}, bounds={'ci': 20 * 60})
        self.assertEqual(B.of(D.decide(f, config(required_checks=('gate',))), A.Rerun), [])

    def test_the_ledger_measures_p90_only_with_enough_spells(self):
        from asf.kernel import waits
        recs = []
        for n in range(10):
            recs += [{'item': 'T-%d' % n, 'reason': 'building', 'to_state': 'building',
                      'at': ago(200 - n)},
                     {'item': 'T-%d' % n, 'reason': 'review', 'to_state': 'review',
                      'at': ago(200 - n - 10 * (n + 1))}]
        import datetime
        now = datetime.datetime(2026, 10, 10, 10, 0, tzinfo=datetime.timezone.utc)
        got = waits.bounds(recs, now=now, min_samples=10)
        self.assertEqual(got['building'], 90 * 60)
        self.assertEqual(waits.bounds(recs, now=now, min_samples=11), {})


class CloseFloor(unittest.TestCase):

    def test_an_open_kernel_pr_with_no_item_is_closed(self):
        orphan = B.pr(5, 'T-9')
        plan = D.decide(facts([], orphan_prs=[orphan]), config())
        self.assertEqual([(a.pr, a.item_id) for a in B.of(plan, A.ClosePR)], [(5, 'T-9')])
        self.assertIn('not on the record', B.of(plan, A.ClosePR)[0].reason)

    def test_an_unreadable_cards_pr_is_never_closed_and_the_card_is_in_limbo(self):
        f = facts([], orphan_prs=[B.pr(5, 'T-9')],
                  unreadable={'T-9': 'tasks/T-9.md: unbalanced inline collection'})
        plan = D.decide(f, config())
        self.assertEqual(B.of(plan, A.ClosePR), [])
        self.assertEqual(list(plan.limbo), ['T-9'])
        self.assertIn('unreadable', plan.limbo['T-9'])

    def test_a_hand_made_branch_naming_a_done_item_is_never_closed(self):
        items = [B.task('B-1', state=State.DONE, priority='later')]
        plan = D.decide(facts(items, prs=[B.pr(7, 'B-1', branch='fix/B-1-kernel-0.2')]), config())
        self.assertEqual(B.of(plan, A.ClosePR), [])

    def test_a_pr_off_the_kernel_prefixes_is_never_closed(self):
        orphan = B.pr(5, 'T-9', branch='release/T-9')
        plan = D.decide(facts([], orphan_prs=[orphan]), config())
        self.assertEqual(B.of(plan, A.ClosePR), [])

    def test_a_done_items_open_pr_is_closed_and_the_item_stays_done(self):
        items = [B.task('T-1', state=State.DONE)]
        plan = D.decide(facts(items, prs=[B.pr(7, 'T-1')]), config())
        self.assertEqual([a.pr for a in B.of(plan, A.ClosePR)], [7])
        self.assertEqual(B.state(plan, 'T-1'), State.DONE)
        self.assertEqual(B.launched(plan), [])

    def test_a_retired_items_open_pr_is_closed(self):
        items = [B.task('T-1', state=State.DONE, priority='later')]
        plan = D.decide(facts(items, prs=[B.pr(7, 'T-1')]), config())
        self.assertEqual([a.pr for a in B.of(plan, A.ClosePR)], [7])

    def test_a_reopened_items_pr_is_kept(self):
        items = [B.task('T-1', state=State.DONE, reopened=True)]
        plan = D.decide(facts(items, prs=[B.pr(7, 'T-1')]), config())
        self.assertEqual(B.of(plan, A.ClosePR), [])

    def test_a_features_plan_pr_after_its_spec_landed_is_kept(self):
        items = [B.item('F-1', state=State.DONE, rank=1)]
        prs = [B.pr(3, 'F-1', branch='spec/F-1', merged=True), B.pr(4, 'F-1', branch='plan/F-1')]
        plan = D.decide(facts(items, prs=prs), config())
        self.assertEqual(B.of(plan, A.ClosePR), [])

    def test_off_by_default_in_the_bare_model(self):
        plan = D.decide(facts([], orphan_prs=[B.pr(5, 'T-9')]), B.config())
        self.assertEqual(B.of(plan, A.ClosePR), [])


class Tick(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lines = []
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})

    def tick(self, ports, **kw):
        return loop.tick(self.product, ports=ports, config=config(**kw), state_dir=self.tmp,
                         out=self.lines.append)

    def _tick_line(self):
        return next(l for l in self.lines if l.startswith('kernel tick'))

    def test_the_tick_line_says_limbo_and_lists_it(self):
        st = M.Stuck('launch: no account', 'loop')
        rec = F.FakeRecord([B.task('T-1', state=State.STUCK, stuck=st)])
        summary = self.tick(F.ports(record=rec))
        self.assertIn('LIMBO 1', self._tick_line())
        self.assertTrue(any(l.strip().startswith('limbo T-1:') for l in self.lines))
        self.assertEqual(summary['limbo'], {'T-1': D.limbo_reason_stuck(st)})
        with open(os.path.join(self.tmp, loop.PLAN_FILE), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['limbo'], summary['limbo'])

    def test_limbo_zero_is_printed_too(self):
        self.tick(F.ports(record=F.FakeRecord([B.task('T-1')])))
        self.assertIn('LIMBO 0', self._tick_line())

    def test_breaches_are_logged_from_the_ledger(self):
        with open(os.path.join(self.tmp, 'kernel-waits.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'item': 'T-2', 'to_state': 'ready', 'reason': 'seat',
                                'at': '2026-01-01T00:00:00Z'}) + '\n')
        rec = F.FakeRecord([B.task('T-1'), B.task('T-2', rank=9)])
        ports = F.ports(record=rec)
        self.tick(ports, max_sessions=1)
        self.assertEqual([i for _k, i, _b, _t in ports.sessions.launched], ['T-2'])
        self.assertTrue(any(l.startswith('BREACH T-2 seat ') and l.endswith('-> launch build T-2 on worker/T-2')
                            for l in self.lines), self.lines)

    def test_close_pr_goes_through_the_port_with_a_comment(self):
        rec = F.FakeRecord([B.task('T-1', state=State.DONE)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-1')])
        self.tick(F.ports(record=rec, github=gh))
        self.assertEqual(gh.calls, [('close_pr', 7)])
        self.assertIn('Done', gh.closed[0][1])

    def test_an_orphan_pr_reaches_decide_through_the_facts(self):
        gh = F.FakeGitHub(prs=[B.pr(5, 'T-9')])
        self.tick(F.ports(record=F.FakeRecord([]), github=gh))
        self.assertEqual(gh.calls, [('close_pr', 5)])

    def test_an_over_age_session_is_ended_without_freeing_its_worktree(self):
        rec = F.FakeRecord([B.task('T-1', state=State.BUILDING)])
        sess = F.FakeSessions([B.session('j1', 'T-1', started='2026-01-01T00:00:00Z')])
        self.tick(F.ports(record=rec, sessions=sess), max_session_age_h=3)
        self.assertEqual(sess.ended, [('j1', False)])
        self.assertEqual(rec.fields['T-1'][loop.P.ATTEMPTS], [D.OVER_AGE])


class UnreadableCards(unittest.TestCase):

    def test_the_real_record_names_a_card_it_cannot_parse(self):
        from asf.kernel import ports as P
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, 'tasks'))
        with open(os.path.join(root, 'tasks', 'T-0009.md'), 'w', encoding='utf-8') as f:
            f.write('---\nid: T-0009\ntype: task\nkernel_findings: ["red: a\nb"]\n---\nbody\n')
        product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main', 'backlog_dir': root})
        rec = P.RealRecord(product, state_dir=tempfile.mkdtemp())
        self.assertNotIn('T-0009', rec.items())
        self.assertEqual(list(rec.unreadable()), ['T-0009'])


class Settings(unittest.TestCase):

    def test_the_new_knobs_have_defaults(self):
        k = settings.read(None)
        self.assertEqual(k['waits']['max_session_age'], 3 * 3600)
        self.assertEqual(k['waits']['max_review_session_age'], 90 * 60)
        self.assertTrue(k['waits']['breach'])
        self.assertTrue(k['floor']['close_orphan_prs'])

    def test_a_bad_max_age_is_refused(self):
        errors, _ = settings.problems({'waits': {'max_session_age': 'soon'}})
        self.assertEqual([e[0] for e in errors], ['kernel.waits.max_session_age'])

    def test_config_for_carries_them(self):
        from asf.kernel import ports as P
        product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main',
                                         'kernel': {'waits': {'max_session_age': '2h'}}})
        cfg = P.config_for(product)
        self.assertEqual(cfg.max_session_age_h, 2.0)
        self.assertEqual(cfg.wait_targets['review'], 1800)
        self.assertTrue(cfg.close_floor)
        off = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main',
                                     'kernel': {'waits': {'breach': False}}})
        self.assertEqual(P.config_for(off).wait_targets, {})
        self.assertIsNone(P.config_for(off).max_session_age_h)


if __name__ == '__main__':
    unittest.main()
