"""Pushed work gets its pull request: a build session that ended ``done`` and pushed its branch,
with no PR open for it, makes the kernel open one (:class:`asf.kernel.actions.OpenPR`), titled
``<ITEM-ID> — <card title>``; the item is then in Review and the review launch follows. A PR
already open is never duplicated, and an item left Stuck on a ``done:`` reason whose branch is on
origin is re-judged to an OpenPR (B-0093, B-0098)."""
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel.decide import decide
from asf.kernel.model import Branch, Stuck

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State


def _done(job='j1', iid='B-0098', branch='fix/B-0098'):
    return B.session(job, iid, alive=False, ended=True, result='pushed', status='done',
                     branch=branch, fields={'status': 'done', 'pushed': 'yes 2b865de1d',
                                            'tests': 'tests/test_x.py OK'})


def _bug(state=State.BUILDING, **kw):
    return B.item('B-0098', title='Cards keep their notes', rank=1, state=state, **kw)


class Decide(unittest.TestCase):

    def test_a_done_and_pushed_session_with_no_pr_opens_one_and_the_item_is_in_review(self):
        plan = decide(B.facts([_bug()], sessions=[_done()]), B.config())
        opened = B.of(plan, A.OpenPR)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].branch, 'fix/B-0098')
        self.assertTrue(opened[0].title.startswith('B-0098 — '))
        self.assertEqual(opened[0].title, 'B-0098 — Cards keep their notes')
        self.assertIn('B-0098', opened[0].body)
        self.assertIn('pushed: yes 2b865de1d', opened[0].body)
        self.assertEqual(B.state(plan, 'B-0098'), State.REVIEW)
        self.assertEqual(B.launched(plan), [])  # never a fresh build on the same work

    def test_an_open_pr_for_the_branch_is_never_duplicated(self):
        plan = decide(B.facts([_bug()], sessions=[_done()],
                              prs=[B.pr(12, 'B-0098', branch='fix/B-0098')],
                              branches=[Branch('fix/B-0098', 'B-0098', 'abc')]), B.config())
        self.assertEqual(B.of(plan, A.OpenPR), [])
        self.assertEqual(B.state(plan, 'B-0098'), State.REVIEW)

    def test_a_done_stuck_item_with_a_pushed_branch_is_rejudged_to_open_pr(self):
        stuck = Stuck('done: NEEDS OPERATOR: should the note be kept?', 'operator')
        it = _bug(state=State.STUCK, stuck=stuck)
        plan = decide(B.facts([it], branches=[Branch('fix/B-0098', 'B-0098', 'b4860fed8')]),
                      B.config())
        self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['fix/B-0098'])
        self.assertEqual(B.state(plan, 'B-0098'), State.REVIEW)
        self.assertEqual(B.of(plan, A.MarkStuck), [])

    def test_a_done_stuck_item_without_a_pushed_branch_stays_stuck(self):
        stuck = Stuck('done: NEEDS OPERATOR: should the note be kept?', 'operator')
        plan = decide(B.facts([_bug(state=State.STUCK, stuck=stuck)]), B.config())
        self.assertEqual(B.of(plan, A.OpenPR), [])
        self.assertEqual(B.state(plan, 'B-0098'), State.STUCK)

    def test_another_stuck_or_a_stale_branch_of_a_ready_item_opens_nothing(self):
        other = _bug(state=State.STUCK, stuck=Stuck('partial: left out: x', 'session'))
        ready = B.task('T-0001', state=State.READY)
        br = [Branch('fix/B-0098', 'B-0098', 'a'), Branch('worker/T-0001', 'T-0001', 'b')]
        plan = decide(B.facts([other, ready], branches=br), B.config())
        self.assertEqual(B.of(plan, A.OpenPR), [])

    def test_a_building_card_with_its_branch_pushed_and_no_session_gets_its_pr(self):
        plan = decide(B.facts([_bug()], branches=[Branch('fix/B-0098', 'B-0098', 'a')]),
                      B.config())
        self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['fix/B-0098'])


class ReportedPush(unittest.TestCase):
    """A session that pushed itself (no host push-log entry) and reported ``pushed: yes <sha>``,
    a bare sha or ``rebased <sha>`` is pushed once its branch is on origin (B-0106, B-0112,
    B-0117); ``no …`` / ``none`` is not."""

    SHA = 'a1db77e34cc73fc67f4ac21ae528991a5038e91c'
    ORIGIN = [Branch('fix/B-0098', 'B-0098', SHA)]

    def _self_pushed(self, pushed):
        return B.session('j1', 'B-0098', alive=False, ended=True, result='report',
                         status='done', branch='fix/B-0098',
                         fields={'status': 'done', 'pushed': pushed})

    def test_the_claim_parse(self):
        from asf.kernel import reports as R
        for value, want in (('yes ' + self.SHA, (True, self.SHA)), ('yes', (True, '')),
                            ('yes: 2B865DE1D', (True, '2b865de1d')), (self.SHA, (True, self.SHA)),
                            ('rebased 616e0b67b — the factory publishes', (True, '616e0b67b')),
                            ('no — committed only', (False, '')), ('none', (False, '')),
                            ('', (False, '')), (None, (False, '')), ('nope', (False, ''))):
            self.assertEqual(R.pushed_claim(value), want, value)

    def test_each_claimed_form_on_origin_opens_the_pr(self):
        for pushed in ('yes ' + self.SHA, 'yes', self.SHA, 'rebased a1db77e34'):
            plan = decide(B.facts([_bug()], sessions=[self._self_pushed(pushed)],
                                  branches=self.ORIGIN), B.config())
            self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['fix/B-0098'], pushed)
            self.assertEqual(B.of(plan, A.MarkStuck), [], pushed)
            self.assertEqual(B.state(plan, 'B-0098'), State.REVIEW, pushed)

    def test_a_claimed_sha_is_checked_against_the_origin_head(self):
        # a push the hook refused, reported as `pushed: yes <sha>`: the branch is on origin from
        # an earlier round, at another head. The claim is not a push, and no PR is opened on the
        # previous round's code (B-82960)
        stale = [Branch('fix/B-0098', 'B-0098', 'f' * 40)]
        plan = decide(B.facts([_bug()], sessions=[self._self_pushed('yes ' + self.SHA)],
                              branches=stale), B.config())
        self.assertEqual(B.of(plan, A.OpenPR), [])
        self.assertTrue(B.stuck(plan, 'B-0098').reason.startswith('done without a push'))
        # a head origin never read holds nothing against the claim
        unread = [Branch('fix/B-0098', 'B-0098', '')]
        plan = decide(B.facts([_bug()], sessions=[self._self_pushed('yes ' + self.SHA)],
                              branches=unread), B.config())
        self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['fix/B-0098'])

    def test_a_not_pushed_line_or_no_branch_on_origin_is_stuck(self):
        for pushed, branches in (('no — committed only', self.ORIGIN), ('none', self.ORIGIN),
                                 ('yes ' + self.SHA, [])):
            plan = decide(B.facts([_bug()], sessions=[self._self_pushed(pushed)],
                                  branches=branches), B.config())
            self.assertEqual(B.of(plan, A.OpenPR), [], pushed)
            self.assertTrue(B.stuck(plan, 'B-0098').reason.startswith('done without a push'))

    def test_an_item_stuck_on_a_claimed_push_is_rejudged_to_open_pr(self):
        stuck = Stuck('done without a push: pushed: yes ' + self.SHA, 'session')
        plan = decide(B.facts([_bug(state=State.STUCK, stuck=stuck)], branches=self.ORIGIN),
                      B.config())
        self.assertEqual([a.branch for a in B.of(plan, A.OpenPR)], ['fix/B-0098'])
        self.assertEqual(B.state(plan, 'B-0098'), State.REVIEW)
        # no branch on origin, or a "no" line: it stays Stuck
        for reason, branches in (('done without a push: pushed: yes ' + self.SHA, []),
                                 ('done without a push: pushed: no', self.ORIGIN)):
            it = _bug(state=State.STUCK, stuck=Stuck(reason, 'session'))
            plan = decide(B.facts([it], branches=branches), B.config())
            self.assertEqual(B.of(plan, A.OpenPR), [], reason)
            self.assertEqual(B.state(plan, 'B-0098'), State.STUCK, reason)


class Loop(unittest.TestCase):

    def tick(self, ports):
        return loop.tick(env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'}),
                         ports=ports, config=B.config(), state_dir=tempfile.mkdtemp(),
                         out=lambda _line: None)

    def test_open_then_review_launch_on_the_next_tick(self):
        rec = F.FakeRecord([_bug()])
        gh = F.FakeGitHub(branches=[Branch('fix/B-0098', 'B-0098', '2b865de1d')])
        sess = F.FakeSessions([_done()])
        self.tick(F.ports(record=rec, github=gh, sessions=sess))
        self.assertEqual(len(gh.opened), 1)
        self.assertTrue(gh.opened[0][2].startswith('B-0098 — '))
        self.assertEqual(rec.fields['B-0098'][P.STATE], 'review')
        self.assertEqual(sess.launched, [])
        self.tick(F.ports(record=rec, github=gh, sessions=sess))
        self.assertEqual(len(gh.opened), 1)  # the PR is there now: no second one
        self.assertEqual([(k, i, b) for k, i, b, _ in sess.launched],
                         [('review', 'B-0098', 'fix/B-0098')])

    def test_the_stuck_live_example_clears_and_gets_its_pr(self):
        rec = F.FakeRecord([_bug(state=State.STUCK,
                                 stuck=Stuck('done: NEEDS OPERATOR: ok?', 'operator'))])
        rec.fields['B-0098'] = {P.STATE: 'stuck', P.STUCK_REASON: 'done: NEEDS OPERATOR: ok?',
                                P.STUCK_OWNER: 'operator'}
        gh = F.FakeGitHub(branches=[Branch('fix/B-0098', 'B-0098', 'b4860fed8')])
        self.tick(F.ports(record=rec, github=gh))
        self.assertEqual([o[0] for o in gh.opened], ['fix/B-0098'])
        self.assertEqual(rec.fields['B-0098'].get(P.STATE), 'review')
        self.assertNotIn(P.STUCK_REASON, rec.fields['B-0098'])


class RealPort(unittest.TestCase):

    def gh(self, replies):
        calls = []

        def run(args, **_kw):
            calls.append(args)
            for key, out in replies:
                if key in ' '.join(args):
                    return out
            raise AssertionError(args)
        return calls, run

    def port(self, run):
        from asf import github
        product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})
        port = P.RealGitHub.__new__(P.RealGitHub)
        port.product, port.slug, port._env, port._run, port._logs = product, 'o/r', None, None, 0
        port._gh = lambda args, json=True: run(args, json=json)
        return port, github

    def test_an_existing_pr_is_recorded_not_created(self):
        from asf.github import Result
        calls, run = self.gh([('pr list', Result(True, [{'number': 41}]))])
        port, _ = self.port(run)
        self.assertEqual(port.open_pr('fix/B-0098', '', 'B-0098 — t', 'b'), 41)
        self.assertFalse(any('create' in a for a in calls))

    def test_a_new_pr_is_created_against_the_trunk(self):
        from asf.github import Result
        calls, run = self.gh([('pr list', Result(True, [])),
                              ('pr create', Result(True, 'x', 0,
                                                   'https://github.com/o/r/pull/77\n'))])
        port, _ = self.port(run)
        self.assertEqual(port.open_pr('fix/B-0098', '', 'B-0098 — t', 'b'), 77)
        create = next(a for a in calls if 'create' in a)
        self.assertEqual(create[create.index('--base') + 1], 'main')
        self.assertEqual(create[create.index('--title') + 1], 'B-0098 — t')


if __name__ == '__main__':
    unittest.main()
