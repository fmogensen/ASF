"""Every wait is measured: the wait ledger, the per-class percentiles, the targets and the
biggest wait on top of ``asf kernel status`` (:mod:`asf.kernel.waits`)."""
import datetime
import json
import os
import tempfile
import unittest

from asf import env
from asf.kernel import loop, settings, status, waits
from asf.kernel import actions as A
from asf.kernel.model import Facts, Stuck

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 10, 8, 0, tzinfo=UTC)


def at(minutes):
    return waits._iso(T0 + datetime.timedelta(minutes=minutes))


def rec(item, reason, minutes, to_state='ready'):
    return {'item': item, 'from_state': None, 'to_state': to_state, 'reason': reason,
            'at': at(minutes)}


class Ledger(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _record(self, plan, facts, minutes):
        return waits.record(self.tmp, plan, facts, B.config(),
                            now=T0 + datetime.timedelta(minutes=minutes))[0]

    def _lines(self):
        with open(os.path.join(self.tmp, waits.LEDGER_FILE), encoding='utf-8') as f:
            return [json.loads(x) for x in f]

    def test_the_ledger_writes_only_on_a_change(self):
        facts = Facts(items={'T-1': B.task('T-1'), 'T-2': B.task('T-2', state=State.NEW)})
        plan = A.Plan(states={'T-1': (State.READY, None), 'T-2': (State.NEW, None)})
        self.assertEqual(len(self._record(plan, facts, 0)), 2)
        self.assertEqual(self._record(plan, facts, 2), [], 'unchanged: nothing written')
        plan.states['T-1'] = (State.BUILDING, None)
        new = self._record(plan, facts, 4)
        self.assertEqual(new, [{'item': 'T-1', 'from_state': 'ready', 'to_state': 'building',
                                'reason': 'building', 'at': at(4)}])
        self.assertEqual([r['reason'] for r in self._lines()], ['seat', 'after', 'building'])

    def test_a_reason_change_in_the_same_state_is_recorded(self):
        items = {'T-1': B.task('T-1', state=State.LANDING)}
        running = B.pr(7, 'T-1', checks=[B.check(status='in_progress')])
        plan = A.Plan(states={'T-1': (State.LANDING, None)})
        self.assertEqual(self._record(plan, Facts(items=items, prs=[running]), 0)[0]['reason'],
                         'ci')
        green = B.pr(7, 'T-1', checks=[B.check()])
        new = self._record(plan, Facts(items=items, prs=[green]), 5)
        self.assertEqual([(r['from_state'], r['to_state'], r['reason']) for r in new],
                         [('landing', 'landing', 'merge')])

    def test_the_classes(self):
        items = {i: B.task(i) for i in ('A', 'B', 'C', 'D', 'E', 'F')}
        prs = [B.pr(1, 'A', behind=True), B.pr(2, 'B', conflicting=True), B.pr(3, 'C')]
        facts = Facts(items=items, prs=prs)
        cases = {'A': (State.LANDING, None, 'train'), 'B': (State.REVIEW, None, 'conflict'),
                 'C': (State.REVIEW, None, 'review'), 'D': (State.STUCK, Stuck('x', 'ci'),
                                                           'stuck:ci'),
                 'E': (State.PARKED, None, 'parked'), 'F': (State.READY, None, 'seat')}
        for iid, (state, st, cls) in cases.items():
            with self.subTest(iid=iid):
                self.assertEqual(waits.classify(iid, state, st, facts, B.config())[0], cls)

    def test_a_feature_is_not_followed(self):
        facts = Facts(items={'F-1': B.item('F-1', type='feature')})
        plan = A.Plan(states={'F-1': (State.READY, None)})
        self.assertEqual(self._record(plan, facts, 0), [])


class Measures(unittest.TestCase):

    def test_percentiles(self):
        self.assertEqual(waits.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertEqual(waits.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9)
        self.assertEqual(waits.percentile([7], 90), 7)
        self.assertIsNone(waits.percentile([], 50))

    def test_per_class_times_and_item_hours(self):
        records = [rec('A', 'ci', 0), rec('A', 'merge', 10), rec('B', 'ci', 0),
                   rec('B', 'merge', 30), rec('C', 'ci', 40), rec('A', 'done', 20, 'done')]
        rep = waits.report(records, now=T0 + datetime.timedelta(minutes=60),
                           since=T0, targets={'ci': 600})
        ci = next(c for c in rep['classes'] if c['class'] == 'ci')
        self.assertEqual((ci['now'], ci['p50'], ci['p90'], ci['max']), (1, 1200, 1800, 1800))
        self.assertAlmostEqual(ci['item_s'], 600 + 1800 + 1200)
        self.assertEqual(ci['over'], 1, 'C has waited 20 m in ci, the target is 10 m')

    def test_the_target_flag_and_the_biggest_wait(self):
        records = [rec('A', 'seat', 0), rec('B', 'review', 50), rec('C', 'review', 55)]
        rep = waits.report(records, now=T0 + datetime.timedelta(minutes=60),
                           targets=settings.read(None)['waits']['targets'])
        self.assertEqual([(w['item'], w['class'], w['over']) for w in rep['top']],
                         [('A', 'seat', True), ('B', 'review', False), ('C', 'review', False)])
        self.assertEqual(rep['over'], 1)
        self.assertEqual(rep['biggest']['class'], 'seat')
        self.assertEqual(rep['biggest']['item'], 'A')
        text = waits.render(rep)
        self.assertIn('| A | seat | 1.0h ⚠ |', text)
        self.assertTrue(text.startswith('biggest wait (2h): seat — 1.0 item-h; oldest A 1.0h ⚠'))
        self.assertEqual(waits.tick_line(rep), 'over-target waits: 1, biggest: seat')

    def test_a_stuck_wait_takes_the_stuck_target(self):
        rep = waits.report([rec('A', 'stuck:operator', 0)],
                           now=T0 + datetime.timedelta(minutes=1), targets={'stuck': 0})
        self.assertTrue(rep['top'][0]['over'])

    def test_the_targets_are_validated(self):
        errors, _ = settings.problems({'waits': {'targets': {'sea': '10m'}}})
        self.assertEqual(errors[0][0], 'kernel.waits.targets')
        errors, _ = settings.problems({'waits': {'targets': {'ci': 'soon'}}})
        self.assertEqual(errors[0][0], 'kernel.waits.targets')
        self.assertEqual(settings.problems({'waits': {'targets': {'ci': '5m', 'after': 3600}}}),
                         ([], []))
        t = settings.read({'waits': {'targets': {'ci': '5m'}}})['waits']['targets']
        self.assertEqual((t['ci'], t['review'], t['stuck']), (300, 1800, 0))


class Hooks(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'main': 'main'})
        self.lines = []

    def test_a_tick_writes_the_ledger_and_prints_the_wait_line(self):
        rec_ = F.FakeRecord([B.task('T-0001', state=State.REVIEW)])
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', checks=[B.check(status='in_progress')])],
                          reviews=[B.review('T-0001')])
        summary = loop.tick(self.product, ports=F.ports(record=rec_, github=gh),
                            config=B.config(), state_dir=self.tmp, out=self.lines.append)
        self.assertEqual(summary['waits'], 'over-target waits: 0, biggest: ci')
        self.assertIn('over-target waits: 0, biggest: ci', self.lines)
        ledger = waits.read_ledger(self.tmp)
        self.assertEqual([(r['item'], r['to_state'], r['reason']) for r in ledger],
                         [('T-0001', 'landing', 'ci')])

    def test_status_shows_the_biggest_wait_on_top(self):
        now = datetime.datetime.now(UTC)
        with open(os.path.join(self.tmp, waits.LEDGER_FILE), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'item': 'T-0001', 'from_state': None, 'to_state': 'ready',
                                'reason': 'seat',
                                'at': waits._iso(now - datetime.timedelta(minutes=30))}) + '\n')
        rec_ = F.FakeRecord([B.task('T-0001')])
        text = status.status(self.product, ports=F.ports(record=rec_), config=B.config(),
                             out=lambda *_: None, live=True, state_dir=self.tmp)
        self.assertTrue(text.startswith('biggest wait (2h): seat — 0.5 item-h; oldest T-0001 30m ⚠'),
                        text.splitlines()[0])

    def test_waits_prints_the_view_without_a_network_call(self):
        with open(os.path.join(self.tmp, waits.LEDGER_FILE), 'w', encoding='utf-8') as f:
            f.write(json.dumps(rec('T-0001', 'ci', 0)) + '\n')
        out = []
        rep = waits.waits(self.product, state_dir=self.tmp, out=out.append,
                          now=T0 + datetime.timedelta(minutes=15))
        self.assertEqual(rep['biggest']['class'], 'ci')
        self.assertIn('| ci | 1 ⚠1 | 15m | 15m | 15m | 0.2 | 10m |', out[0])


if __name__ == '__main__':
    unittest.main()
