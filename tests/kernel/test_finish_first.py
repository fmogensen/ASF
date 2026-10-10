"""Finish first (2026-10-10, measured 09:29Z: Review held 40 items while every one of 18 seats ran
— 4 builds, 6 plans, 7 reviews, 1 spec — and Ready held 23 more: new work started while finished
work waited on a reviewer).

- When seats are scarce they fill in one code-driven order: fix rounds and rebases on open PRs,
  then reviews, then builds of Tasks/Bugs, then plans, then specs; within a class the existing
  rank and impact order holds.
- The WIP cap ``kernel.launch.max_open_prs`` (default 30; 0: off): while the items in Review
  plus Landing exceed it, no new build, plan or spec launches — their seats go to finishing work.
  The plan carries why (``Plan.wip``) and the tick logs it on one line.
"""
import unittest

from asf import env
from asf.kernel import loop, settings
from asf.kernel import ports as P
from asf.kernel.decide import IDLE_REASONS, decide

try:
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

State = B.State


def _mix():
    """One of each class, the newest work ranked best: a spec, a plan, a build, a review and a
    fix round (a review asked for changes)."""
    items = [B.item('F-0001', rank=1),                      # no spec yet: spec
             B.item('F-0002', rank=1),                      # spec landed: plan
             B.task('T-0001', rank=1),                      # Ready Task: build
             B.task('T-0002', state=State.REVIEW, rank=9),  # open PR, no verdict: review
             B.task('T-0003', state=State.REVIEW, rank=9)]  # changes asked: fix round
    prs = [B.pr(1, 'T-0002'), B.pr(2, 'T-0003')]
    return B.facts(items, prs=prs, specs_landed={'F-0002': ''},
                   reviews=[B.review('T-0003', verdict='changes')])


def _kinds(plan):
    return [(a.kind, a.item_id) for a in plan.actions if type(a).__name__ == 'Launch']


class FinishFirstOrder(unittest.TestCase):

    def test_seats_fill_fix_review_build_plan_spec(self):
        plan = decide(_mix(), B.config(max_sessions=5))
        self.assertEqual(_kinds(plan), [('build', 'T-0003'), ('review', 'T-0002'),
                                        ('build', 'T-0001'), ('plan', 'F-0002'),
                                        ('spec', 'F-0001')])

    def test_scarce_seats_go_to_finishing_work(self):
        plan = decide(_mix(), B.config(max_sessions=2))
        self.assertEqual(_kinds(plan), [('build', 'T-0003'), ('review', 'T-0002')])

    def test_a_build_takes_a_seat_before_a_plan_and_a_plan_before_a_spec(self):
        items = [B.item('F-0001', rank=1), B.item('F-0002', rank=2), B.task('T-0001', rank=3)]
        f = B.facts(items, specs_landed={'F-0002': ''})
        self.assertEqual(_kinds(decide(f, B.config(max_sessions=1))), [('build', 'T-0001')])
        self.assertEqual(_kinds(decide(f, B.config(max_sessions=2))),
                         [('build', 'T-0001'), ('plan', 'F-0002')])

    def test_rank_still_orders_within_a_class(self):
        items = [B.task('T-0001', rank=3), B.task('T-0002', rank=1), B.task('T-0003', rank=2)]
        plan = decide(B.facts(items), B.config(max_sessions=2))
        self.assertEqual(B.launched(plan, 'build'), ['T-0002', 'T-0003'])

    def test_a_rebase_round_goes_before_a_review(self):
        items = [B.task('T-0001', state=State.REVIEW, rank=1),
                 B.task('T-0002', state=State.REVIEW, rank=9)]
        prs = [B.pr(1, 'T-0001'), B.pr(2, 'T-0002', conflicting=True)]
        plan = decide(B.facts(items, prs=prs), B.config(max_sessions=1))
        self.assertEqual(_kinds(plan), [('build', 'T-0002')])


class WipCap(unittest.TestCase):

    def _facts(self, open_prs=3):
        items = [B.task('T-%04d' % n, state=State.REVIEW, rank=5) for n in range(1, open_prs + 1)]
        items += [B.task('T-0100', rank=1), B.item('F-0001', rank=1), B.item('F-0002', rank=1)]
        prs = [B.pr(n, 'T-%04d' % n) for n in range(1, open_prs + 1)]
        return B.facts(items, prs=prs, specs_landed={'F-0002': ''})

    def test_over_the_cap_new_work_waits_and_reviews_take_the_seats(self):
        plan = decide(self._facts(3), B.config(max_sessions=10, max_open_prs=2))
        self.assertEqual(B.launched(plan, 'review'), ['T-0001', 'T-0002', 'T-0003'])
        self.assertEqual(B.launched(plan, 'build') + B.launched(plan, 'plan')
                         + B.launched(plan, 'spec'), [])
        self.assertEqual(plan.wip, {'open': 3, 'cap': 2, 'held': 3})

    def test_at_the_cap_new_work_still_launches(self):
        plan = decide(self._facts(2), B.config(max_sessions=10, max_open_prs=2))
        self.assertEqual(B.launched(plan, 'build'), ['T-0100'])
        self.assertEqual(B.launched(plan, 'plan'), ['F-0002'])
        self.assertEqual(B.launched(plan, 'spec'), ['F-0001'])
        self.assertIsNone(plan.wip)

    def test_no_cap_no_hold(self):
        plan = decide(self._facts(3), B.config(max_sessions=10))
        self.assertEqual(B.launched(plan, 'build'), ['T-0100'])
        self.assertIsNone(plan.wip)

    def test_landing_counts_toward_the_cap(self):
        items = [B.task('T-0001', state=State.REVIEW, rank=5),
                 B.task('T-0002', state=State.LANDING, rank=5), B.task('T-0100', rank=1)]
        prs = [B.pr(1, 'T-0001'), B.pr(2, 'T-0002', auto_merge=True)]
        f = B.facts(items, prs=prs, reviews=[B.review('T-0002')])
        plan = decide(f, B.config(max_sessions=10, max_open_prs=1))
        self.assertEqual(B.launched(plan, 'build'), [])
        self.assertEqual(plan.wip['open'], 2)

    def test_a_fix_round_is_never_held_by_the_cap(self):
        items = [B.task('T-0001', state=State.REVIEW, rank=5),
                 B.task('T-0002', state=State.REVIEW, rank=5), B.task('T-0100', rank=1)]
        prs = [B.pr(1, 'T-0001'), B.pr(2, 'T-0002')]
        f = B.facts(items, prs=prs, reviews=[B.review('T-0002', verdict='changes')])
        plan = decide(f, B.config(max_sessions=10, max_open_prs=1))
        self.assertEqual(_kinds(plan), [('build', 'T-0002'), ('review', 'T-0001')])

    def test_held_work_is_not_limbo_and_the_idle_alarm_names_the_cap(self):
        items = [B.task('T-0001', state=State.REVIEW, rank=5), B.task('T-0100', rank=1)]
        f = B.facts(items, prs=[B.pr(1, 'T-0001')],
                    sessions=[B.session('review-t-0001-1', 'T-0001', kind='review')])
        plan = decide(f, B.config(max_sessions=4, max_open_prs=0))
        self.assertEqual(B.launched(plan), [])
        self.assertNotIn('T-0100', plan.limbo)
        self.assertIn((IDLE_REASONS['wip'], 1), plan.idle['reasons'])

    def test_the_tick_logs_the_reason_once(self):
        plan = decide(self._facts(3), B.config(max_sessions=10, max_open_prs=2))
        lines = []
        loop.print_summary(loop.summarize(plan, self._facts(3)), lines.append)
        wip = [line for line in lines if line.startswith('WIP CAP')]
        self.assertEqual(wip, ['WIP CAP: 3 open PRs (Review + Landing) > 2 — 3 new '
                               'build/plan/spec launch(es) wait; seats go to finishing work'])


class RealSeats(unittest.TestCase):
    """Seats are what the host can launch on now (``Facts.seats``: local seats plus the cloud
    seats its accounts and breaker allow), never the configured lane sizes alone."""

    def test_the_host_capacity_bounds_the_launches(self):
        items = [B.task('T-%04d' % n, rank=n) for n in range(1, 6)]
        f = B.facts(items, seats=3, sessions=[B.session('j1', 'T-0009')])
        plan = decide(f, B.config(max_sessions=10))
        self.assertEqual(B.launched(plan), ['T-0001', 'T-0002'])

    def test_unknown_capacity_keeps_max_sessions(self):
        items = [B.task('T-%04d' % n, rank=n) for n in range(1, 6)]
        plan = decide(B.facts(items), B.config(max_sessions=4))
        self.assertEqual(len(B.launched(plan)), 4)

    def test_read_facts_takes_the_session_port_capacity(self):
        from asf.kernel.facts import read_facts
        try:
            from kernel import fakes as F
        except ImportError:  # pragma: no cover
            from tests.kernel import fakes as F
        s = F.FakeSessions()
        self.assertIsNone(read_facts(F.ports(sessions=s)).seats)
        s.capacity = lambda: 7
        self.assertEqual(read_facts(F.ports(sessions=s)).seats, 7)


class Knob(unittest.TestCase):

    def test_the_knob_defaults_to_30_and_0_turns_it_off(self):
        self.assertEqual(settings.read(None)['launch']['max_open_prs'], 30)
        self.assertEqual(P.config_for(env.Product('sample', {})).max_open_prs, 30)
        off = env.Product('sample', {'kernel': {'launch': {'max_open_prs': 0}}})
        self.assertIsNone(P.config_for(off).max_open_prs)
        self.assertEqual(settings.problems({'launch': {'max_open_prs': -1}})[0][0][0],
                         'kernel.launch.max_open_prs')


if __name__ == '__main__':
    unittest.main()
