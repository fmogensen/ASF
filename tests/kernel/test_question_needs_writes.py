"""A build session's REPORT names ``needs writes: <paths>`` outside its card's ``writes:`` and
asks the operator whether to widen it (:mod:`asf.kernel.resolvers` ``needs-writes``). Code
answers it by a fixed policy, in order:

1. a requested path in the ``writes:`` of an unfinished item this card has ``after:`` on: "hold:
   <path> belongs to <id>, wait for it" — nothing is granted; the item waits via its after edge;
2. else a requested path in the ``writes:`` of a Building or Review item: granted, and the
   kernel's overlap rule serialises the two;
3. else: granted.

Granting writes the widened ``writes:`` to the card through the record's write path
(:meth:`asf.kernel.ports.RealRecord.widen_writes`) and answers "granted <paths>; nothing else
outside writes". A granted path that matches ``kernel.risk.high`` makes the item high-risk.

The fixtures are the three the console answered by hand on 2026-10-10
(``state/asf/operator-answers.jsonl``): T-0196 granted tests/test_e2e_lane.py, T-0123 granted
asf/harvest/lane.py (its other writer serialised by the overlap rule), T-81134 held on
asf/conventions.py, which its unfinished ``after:`` dependency T-81133 owns."""
import os
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import ports as P
from asf.kernel import resolvers as R
from asf.kernel import settings
from asf.kernel.apply import apply

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

#: build-t-0196-1791625923, answered by hand 10:25:22Z
NW_0196 = ('tests/test_e2e_lane.py — add a `Proves: S-0001 line 1 — tests/test_lines.py::'
           'LinesTests::test_last_line` trailer to: (1) `branch_by_hand()`\'s commit (scenario 5 / '
           'fault 9, used by PreexistingPRFF and PreexistingPRPR), and (2) FootprintPartialFF\'s '
           'queued `coder-t-0001` commit')
Q_0196 = ('tests/test_e2e_lane.py needs the trailer added in the two spots above to clear PR '
          '#1332\'s red CI; outside this item\'s adjudicated writes (asf/harvest/lane.py, '
          'tests/test_lane.py), so it needs its own ruling/widen rather than a silent edit here.')
#: build-t-81134-1791627452, answered by hand 10:45:16Z
NW_81134 = 'asf/conventions.py tests/test_env.py docs/guide/product-config.md'
Q_81134 = ('whether to grant the three needs-writes paths to this card (registering '
           'regression.check / regression.command here) or to hold T-81134 behind Task 5, which '
           'owns that registry')
#: build-t-0123-1791629158, answered by hand 10:55:33Z
NW_0123 = 'asf/harvest/lane.py'
Q_0123 = ('a ruling on T-0123\'s writes:/grant: — either widen it to include asf/harvest/lane.py '
          'so Task 3/4\'s screen/archive/re-admission logic and its tests can be written where '
          'that code now actually lives, or amend docs/plans/f-0026.md\'s Task 3/4 Files/Steps')


def ended(iid, nw, q, status='partial'):
    return B.session('build-%s-1' % iid.lower(), iid, alive=False, ended=True,
                     result='question', status=status, question=q,
                     fields={'status': status, 'needs writes': nw})


def config(**kw):
    kw.setdefault('resolve_needs_writes', True)
    return B.config(**kw)


def t0196():
    return B.task('T-0196', state=State.BUILDING, writes=['asf/harvest/lane.py',
                                                          'tests/test_lane.py'])


def t81134():
    return [B.task('T-81134', state=State.BUILDING, after=['T-81133'],
                   writes=['asf/briefs/templates/fix-bug.md', 'asf/briefs/build.py',
                           'tests/test_briefs.py']),
            B.task('T-81133', state=State.NEW, after=[],
                   writes=['asf/conventions.py', 'tests/test_conventions.py',
                           'tests/test_regression_gate.py'])]


def t0123():
    return B.task('T-0123', state=State.BUILDING, writes=['asf/boundary.py', 'asf/harvest/harvest.py'])


class Requested(unittest.TestCase):

    def test_the_paths_before_the_prose(self):
        self.assertEqual(R.requested_writes(NW_0196, []), ['tests/test_e2e_lane.py'])
        self.assertEqual(R.requested_writes(NW_81134, []),
                         ['asf/conventions.py', 'tests/test_env.py', 'docs/guide/product-config.md'])

    def test_paths_already_in_writes_are_not_requested(self):
        self.assertEqual(R.requested_writes(NW_81134, ['asf/*.py']),
                         ['tests/test_env.py', 'docs/guide/product-config.md'])
        self.assertEqual(R.requested_writes('none', []), [])
        self.assertEqual(R.requested_writes('', []), [])


class Policy(unittest.TestCase):
    """The three answers the console gave, decided by code."""

    def decide(self, items, iid, nw, q, **kw):
        f = B.facts(items, sessions=[ended(iid, nw, q)])
        return D.decide(f, config(**kw))

    def test_t0196_a_path_nobody_holds_is_granted(self):
        plan = self.decide([t0196()], 'T-0196', NW_0196, Q_0196)
        a = B.of(plan, A.ApplyAnswer)
        self.assertEqual([(x.by, x.writes) for x in a],
                         [(R.NEEDS_WRITES, ['tests/test_e2e_lane.py'])])
        self.assertTrue(a[0].text.startswith(
            'granted tests/test_e2e_lane.py; nothing else outside writes'), a[0].text)
        self.assertEqual(B.state(plan, 'T-0196'), State.READY)

    def test_t0123_a_path_a_building_item_holds_is_granted_and_serialised(self):
        plan = self.decide([t0123(), t0196()], 'T-0123', NW_0123, Q_0123)
        a = B.of(plan, A.ApplyAnswer)
        self.assertEqual([(x.by, x.writes) for x in a],
                         [(R.NEEDS_WRITES, ['asf/harvest/lane.py'])])
        self.assertIn('granted asf/harvest/lane.py; nothing else outside writes', a[0].text)
        self.assertIn('T-0196', a[0].text)
        self.assertIn('overlap rule serialises', a[0].text)

    def test_t81134_a_path_its_unfinished_after_dependency_owns_is_held(self):
        plan = self.decide(t81134(), 'T-81134', NW_81134, Q_81134)
        a = B.of(plan, A.ApplyAnswer)
        self.assertEqual([(x.by, x.writes) for x in a], [(R.NEEDS_WRITES, [])])
        self.assertTrue(a[0].text.startswith(
            'hold: asf/conventions.py belongs to T-81133, wait for it'), a[0].text)
        self.assertEqual(B.state(plan, 'T-81134'), State.NEW, 'waits through its after edge')
        self.assertNotIn('T-81134', B.launched(plan))

    def test_a_finished_dependency_holds_nothing(self):
        items = t81134()
        items[1].state = State.DONE
        plan = self.decide(items, 'T-81134', NW_81134, Q_81134)
        a = B.of(plan, A.ApplyAnswer)
        self.assertEqual(a[0].writes, ['asf/conventions.py', 'tests/test_env.py',
                                       'docs/guide/product-config.md'])

    def test_a_partial_report_without_a_question_is_answered_too(self):
        f = B.facts([t0196()], sessions=[ended('T-0196', NW_0196, None)])
        a = B.of(D.decide(f, config()), A.ApplyAnswer)
        self.assertEqual([x.writes for x in a], [['tests/test_e2e_lane.py']])

    def test_a_recorded_stuck_naming_needs_writes_is_answered(self):
        it = B.task('T-0196', state=State.STUCK, writes=['asf/harvest/lane.py'],
                    stuck=B.M.Stuck('partial: needs writes: tests/test_e2e_lane.py — the trailer',
                                    'operator'))
        a = B.of(D.decide(B.facts([it]), config()), A.ApplyAnswer)
        self.assertEqual([(x.by, x.writes) for x in a],
                         [(R.NEEDS_WRITES, ['tests/test_e2e_lane.py'])])

    def test_an_answer_already_on_the_card_is_not_given_again(self):
        it = t0196()
        first = B.of(self.decide([it], 'T-0196', NW_0196, Q_0196), A.ApplyAnswer)[0]
        it.answers = [first.text]
        self.assertEqual(B.of(self.decide([it], 'T-0196', NW_0196, Q_0196), A.ApplyAnswer), [])

    def test_a_granted_path_on_a_high_risk_glob_says_so_and_makes_the_item_high_risk(self):
        plan = self.decide([t0196()], 'T-0196', NW_0196, Q_0196, risk_high=('tests/test_e2e_*',))
        a = B.of(plan, A.ApplyAnswer)[0]
        self.assertIn('high-risk', a.text)
        widened = t0196()
        widened.writes += a.writes
        self.assertTrue(D.high_risk(widened, None, config(risk_high=('tests/test_e2e_*',))))

    def test_the_knob_off_leaves_the_console(self):
        plan = self.decide([t0196()], 'T-0196', NW_0196, Q_0196, resolve_needs_writes=False)
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.stuck(plan, 'T-0196').owner, 'operator')

    def test_the_log_line(self):
        a = B.of(self.decide([t0196()], 'T-0196', NW_0196, Q_0196), A.ApplyAnswer)[0]
        self.assertTrue(A.describe(a).startswith('RESOLVED T-0196 needs-writes -> granted '
                                                 'tests/test_e2e_lane.py'), A.describe(a))


class Apply(unittest.TestCase):

    def test_a_grant_widens_the_card_then_answers(self):
        it = t0196()
        rec = F.FakeRecord([it])
        f = B.facts([it], sessions=[ended('T-0196', NW_0196, Q_0196)])
        plan = D.decide(f, config())
        apply(plan, f, F.ports(record=rec), log=lambda _l: None)
        self.assertEqual(rec.widened, [('T-0196', ['tests/test_e2e_lane.py'])])
        self.assertIn('tests/test_e2e_lane.py', rec.items()['T-0196'].writes)
        self.assertTrue(rec.fields['T-0196'][P.ANSWERS][0].startswith('granted'))

    def test_a_refused_widening_leaves_the_question_unanswered(self):
        it = t0196()
        rec = F.FakeRecord([it], fail={('widen', 'T-0196')})
        f = B.facts([it], sessions=[ended('T-0196', NW_0196, Q_0196)])
        logged = []
        r = apply(D.decide(f, config()), f, F.ports(record=rec), log=logged.append)
        self.assertEqual([type(x[0]) for x in r.failed], [A.ApplyAnswer])
        self.assertNotIn(P.ANSWERS, rec.fields['T-0196'])
        self.assertEqual(rec.fields['T-0196'][P.STATE], State.STUCK.value)
        self.assertEqual(rec.fields['T-0196'][P.STUCK_OWNER], 'operator')
        self.assertIn('refused to widen writes', rec.fields['T-0196'][P.STUCK_REASON])
        self.assertNotIn(P.ATTEMPTS, rec.fields['T-0196'], 'no relaunch without the grant')

    def test_a_hold_writes_no_writes(self):
        items = t81134()
        rec = F.FakeRecord(items)
        f = B.facts(items, sessions=[ended('T-81134', NW_81134, Q_81134)])
        apply(D.decide(f, config()), f, F.ports(record=rec), log=lambda _l: None)
        self.assertEqual(rec.widened, [])
        self.assertTrue(rec.fields['T-81134'][P.ANSWERS][0].startswith('hold: '))


class RealRecordWidens(unittest.TestCase):

    def test_widen_writes_through_the_record_writer(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, 'tasks'))
        with open(os.path.join(root, 'tasks', 'T-0001.md'), 'w') as f:
            f.write('---\nid: T-0001\ntype: task\ntitle: one\nparent: F-0001\n'
                    'writes: [asf/a.py, tests/test_a.py]\n---\n## Description\nwork\n\n'
                    '## History\n- 2026-10-10: created\n')
        product = env.Product('sample', {'backlog_dir': root})
        P.RealRecord(product, state_dir=tempfile.mkdtemp()).widen_writes(
            'T-0001', ['tests/test_e2e_lane.py', 'asf/a.py'])
        it = P.RealRecord(product, state_dir=tempfile.mkdtemp()).items()['T-0001']
        self.assertEqual(it.writes, ['asf/a.py', 'tests/test_a.py', 'tests/test_e2e_lane.py'])
        self.assertIn('kernel: writes widened +tests/test_e2e_lane.py', it.body)


class Settings(unittest.TestCase):

    def test_the_knob_defaults_on_and_reaches_the_config(self):
        self.assertIs(settings.read(None)['resolve']['needs_writes'], True)


if __name__ == '__main__':
    unittest.main()
