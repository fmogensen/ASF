"""A report that asks nothing is never a question for a person; a reshape's "does not split" is an
answer the factory takes; and a widening reaches the append-only files every Task shares and the
files a landed Task already delivered.

Live case (a product, 2026-10-01): the factory sat idle overnight with ~40 runs flagged "needs
input", many of them ``needs input — none``; five Tasks parked on their reshape session's own
"does not split — answer no on the split line"; and Tasks held for a shared registry file
(``needs writes:`` a band table every Task appends to) or for a file another Feature's landed Task
had delivered.
"""
import json
import os
import unittest

from asf import briefs
from asf.feeder import rows as feeder_rows
from asf.feeder import widen
from asf.tick import rejudge, widen_footprint
from asf.workers import lifecycle, report
from asf.workers import pool as pool_mod
from asf.workers import stall
from tests.test_tick import _git
from tests.test_widen import REPORT, TASK, WidenStepBase

DONE = ('REPORT\nitem: T-0001\nkind: {kind}\nstatus: done\nbranch: worker/T-0001\n'
        'pushed: {pushed}\ncommits: none\ntests: ok\nleft out: none\nneeds writes: none\n'
        'proves: none\n{operator}\n```\n')


class NeedsInputTests(unittest.TestCase):
    """:func:`report.needs_input` — what a report declares, read deterministically."""

    def ask(self, operator, **kw):
        return report.needs_input(DONE.format(kind=kw.get('kind', 'review'),
                                              pushed=kw.get('pushed', 'yes abc'),
                                              operator=operator))

    def test_none_in_every_spelling_asks_nothing(self):
        for line in ('NEEDS OPERATOR: none', 'NEEDS OPERATOR: None.',
                     'NEEDS OPERATOR: none — resolved on retry, no action required',
                     'NEEDS OPERATOR: none - the push hook flagged a soft check',
                     'NEEDS OPERATOR: omit', 'NEEDS OPERATOR: n/a', 'NEEDS OPERATOR: `none`'):
            self.assertIsNone(self.ask(line), line)

    def test_a_real_question_is_still_a_question(self):
        self.assertEqual(self.ask('NEEDS OPERATOR: decide whether X ships'),
                         'decide whether X ships')
        # a word that only starts like "none" is a question
        self.assertEqual(self.ask('NEEDS OPERATOR: nonetheless pick one'), 'nonetheless pick one')

    def test_a_none_line_before_a_real_one_does_not_hide_it(self):
        self.assertEqual(self.ask('NEEDS OPERATOR: none\nNEEDS OPERATOR: grant the deploy key'),
                         'grant the deploy key')

    def test_a_no_split_answer_is_not_a_question_and_is_read_back(self):
        old = 'NEEDS OPERATOR: T-0001 does not split along x — answer no on the split line'
        new = 'NO SPLIT: T-0001 does not split along x — one atomic change'
        for line in (old, new):
            self.assertIsNone(self.ask(line, kind='reshape'), line)
            self.assertTrue(report.no_split(DONE.format(kind='reshape', pushed='no', operator=line))
                            .startswith('T-0001 does not split along x'))
        self.assertEqual(report.no_split(DONE.format(kind='reshape', pushed='no',
                                                     operator='NEEDS OPERATOR: none')), '')

    def test_a_needs_writes_claim_is_the_widening_rules_not_a_persons(self):
        text = REPORT.format(left='see needs', needs='lib/x.py') + \
            'NEEDS OPERATOR: someone with write access to lib/x.py must change it\n'
        self.assertIsNone(report.needs_input(text))
        blocked = text.replace('status: partial', 'status: blocked')
        self.assertIsNone(report.needs_input(blocked))


class DecideReachTests(unittest.TestCase):
    """:func:`widen.decide` with ``attributed`` and ``whole``."""

    SIX = [f'lib/{i}.py' for i in range(6)]

    def test_a_landed_owners_paths_lift_the_cap_and_an_earlier_widening(self):
        self.assertEqual(widen.decide('T-1', self.SIX, limit=5).kind, widen.RESHAPE)
        self.assertEqual(widen.decide('T-1', self.SIX, limit=5, attributed=self.SIX[:1]).kind,
                         widen.WIDEN)
        self.assertEqual(widen.decide('T-1', ['a.py'], widened_before=1).kind, widen.RESHAPE)
        self.assertEqual(widen.decide('T-1', ['a.py'], widened_before=1,
                                      attributed=['a.py']).kind, widen.WIDEN)

    def test_a_live_owner_is_still_a_wait(self):
        v = widen.decide('T-1', ['a.py'], running=[('T-2', ['a.py'])], attributed=['a.py'])
        self.assertEqual((v.kind, v.detail), (widen.WAITS, 'T-2'))

    def test_a_whole_task_is_widened_never_reshaped(self):
        self.assertEqual(widen.decide('T-1', self.SIX, limit=5, widened_before=2,
                                      whole=True).kind, widen.WIDEN)

    def test_attributed_paths_needs_a_landed_owner_and_no_live_one(self):
        items = {'T-1': {'type': 'task', 'writes': ['x.py']},
                 'T-2': {'type': 'task', 'writes': ['a.py b.py'], 'state': 'Closed'},
                 'T-3': {'type': 'task', 'writes': ['b.py'], 'state': 'Active'},
                 'T-4': {'type': 'task', 'writes': ['c.py'], 'state': 'New'},
                 'T-5': {'type': 'task', 'writes': ['a.py'], 'state': 'New'}}
        got = widen.attributed_paths(items, 'T-1', ['a.py', 'b.py', 'c.py', 'd.py'],
                                     live={'T-3'})
        # a.py: landed by T-2 (T-5 not started); b.py: T-3 is live; c.py: never landed; d.py: new
        self.assertEqual(got, ['a.py'])


class RejudgeBase(WidenStepBase):
    def reshape_run(self, operator, end_reason='failed: empty branch: nothing to land',
                    park=lifecycle.BLOCKED, job='reshape-t-0001'):
        log = os.path.join(self.tmp, f'{job}.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': DONE.format(kind='reshape', pushed='no — nothing',
                                                      operator=operator)}) + '\n')
        self.session(job=job, item='T-0001', kind='reshape', branch='plan/T-0001', pid=999999,
                     log=log, started='2026-09-24T09:00:00Z')
        self.session(job=job, ended='2026-09-24T09:30:00Z', end_reason=end_reason)
        if park:
            self.session(job=job, correction={
                'kind': park, 'text': operator, 'at': '2026-09-24T09:31:00Z', 'parked': True,
                'reason': f'its last report: needs input — {operator}'}, operator_flagged=1)

    def set_card(self, extra):
        path = os.path.join(self.root, 'tasks', 'T-0001.md')
        with open(path) as f:
            text = f.read()
        with open(path, 'w') as f:
            f.write(text.replace('writes: [', extra + 'writes: [', 1))
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'card'], self.root)

    def rejudge(self):
        return rejudge.run(self.ctx_, self.items(), out=self.lines.append)

    def task_rows(self):
        """The Feature's Task rows (:func:`feeder_rows.task_rows`) — the fixture's Feature has no
        stage of its own, so the board's walk would not reach them."""
        items = self.items()
        return feeder_rows.task_rows(items, self.product, items['F-0001'], set(), [])

    def latest(self, job):
        path = pool_mod.sessions_path(self.product)
        return lifecycle.latest(path)[job], path


class NoSplitTests(RejudgeBase):
    def test_a_groom_split_answered_no_split_goes_back_to_its_own_row(self):
        self.set_card('reshape: "split src | tests (groom 2026-09-30)"\n')
        self.assertEqual([r.kind for r in self.task_rows() if r.item_id == 'T-0001'],
                         [feeder_rows.PLAN_CODE, feeder_rows.RESHAPE])
        self.reshape_run('NEEDS OPERATOR: T-0001 does not split along src — answer no on the '
                         'split line')
        taken, _released = self.rejudge()
        self.assertIn('T-0001', taken)
        item = self.items()['T-0001']
        self.assertFalse(item.get('reshape'))
        self.assertIn(feeder_rows.WHOLE, item['reshape_declined'])
        self.assertIn('split T-0001 src | tests', item['reshape_declined'])  # the groom's own key
        self.assertIn('reshape declined: T-0001 does not split along src', self.card_text('T-0001'))
        run, path = self.latest('reshape-t-0001')
        self.assertIsNone(lifecycle.pending_correction(run, path))  # the park is released
        rows = [r for r in self.task_rows() if r.item_id == 'T-0001']
        self.assertEqual([(r.kind, r.launches) for r in rows], [(feeder_rows.PLAN_CODE, True)],
                         rows)
        # taken once: the next tick changes nothing
        self.assertEqual(self.rejudge(), ({}, {}))

    def test_a_footprint_reshape_answered_no_split_is_widened_instead(self):
        self.finished('coder-t-0001', 'tests/test_b.py tests/test_c.py lib/shared.py lib/x.py '
                                      'lib/y.py lib/z.py')
        self.assertEqual(self.tick()[1], {'coder-t-0001': widen.RESHAPE})
        self.reshape_run('NO SPLIT: T-0001 does not split along lib — one change', park=None,
                         end_reason='finished')
        self.rejudge()
        self.assertFalse(self.items()['T-0001'].get('reshape'))
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertIn('lib/z.py', self.writes())
        rows = self.rows()
        self.assertEqual([(r.kind, r.brief_kind, r.launches) for r in rows],
                         [(feeder_rows.FIX_CORRECT, 'correct', True)], rows)

    def test_a_no_writes_recut_answered_no_split_is_not_launched_again(self):
        path = os.path.join(self.root, 'tasks', 'T-0001.md')
        with open(path, 'w') as f:
            f.write(TASK.format(id='T-0001', writes=''))
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'no writes'], self.root)
        self.assertIn(feeder_rows.RESHAPE, [r.kind for r in self.task_rows()])
        self.reshape_run('NEEDS OPERATOR: T-0001 does not split along store-vs-caller — answer '
                         'no on the split line', end_reason='failed: unpushed work')
        self.rejudge()
        rows = [r for r in self.task_rows() if r.item_id == 'T-0001']
        self.assertEqual([(r.kind, r.action) for r in rows],
                         [(feeder_rows.PLAN_CODE, 'WAITS ON writes')], rows)


class ReleaseAnsweredTests(RejudgeBase):
    def coder_run(self, operator, end_reason='failed: empty branch: nothing to land'):
        log = os.path.join(self.tmp, 'coder-t-0001.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': DONE.format(kind='coder', pushed='yes abc',
                                                      operator=operator)}) + '\n')
        self.session(job='coder-t-0001', item='T-0001', kind='coder', branch='worker/T-0001',
                     pid=999999, log=log, started='2026-09-24T09:00:00Z')
        self.session(job='coder-t-0001', ended='2026-09-24T09:30:00Z', end_reason=end_reason)
        self.session(job='coder-t-0001', correction={
            'kind': lifecycle.BLOCKED, 'text': operator, 'at': '2026-09-24T09:31:00Z',
            'parked': True, 'reason': 'parked'}, operator_flagged=1)

    def test_a_park_on_needs_input_none_is_released_and_judged_on_its_report(self):
        self.coder_run('NEEDS OPERATOR: none')
        _taken, released = self.rejudge()
        self.assertEqual(released, {'coder-t-0001': lifecycle.NOTHING_TO_LAND})
        run, path = self.latest('coder-t-0001')
        self.assertIsNone(lifecycle.pending_correction(run, path))
        self.assertEqual(run['end_reason'], lifecycle.NOTHING_TO_LAND)
        self.assertTrue(run['unparked'])
        # nor is it an INPUT row for anyone to look at
        self.assertEqual(stall._input_rows(self.product, set()), [])

    def test_a_park_on_a_real_question_stands(self):
        self.coder_run('NEEDS OPERATOR: paste the API key for the sandbox')
        self.assertEqual(self.rejudge(), ({}, {}))
        run, path = self.latest('coder-t-0001')
        self.assertTrue(lifecycle.pending_correction(run, path)['parked'])
        self.assertEqual([j for j, _q in stall._input_rows(self.product, set())],
                         ['coder-t-0001'])


class SharedWritesTests(WidenStepBase):
    """``conventions.shared_writes``: an append-only file every Task may add to."""

    product_extra = ('steps:\n  batch: off\nconventions:\n  shared_writes: [lib/shared.py]\n')

    def test_a_report_naming_only_a_shared_file_is_no_widening_and_no_hold(self):
        self.active('T-0002', 'lib/shared.py')
        self.finished('coder-t-0001', 'lib/shared.py')
        held, verdicts = self.tick()
        self.assertEqual((held, verdicts), ([], {}), self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])

    def test_a_pending_correction_on_a_shared_file_goes_back_without_waiting(self):
        self.active('T-0002', 'lib/shared.py')
        self.finished('coder-t-0001', 'lib/shared.py')
        self.session(job='coder-t-0001', footprint_read=1, correction={
            'kind': lifecycle.FOOTPRINT, 'text': 'needs lib/shared.py', 'needs': ['lib/shared.py'],
            'fact': 'hook refused: registry', 'verdict': widen.WAITS, 'detail': 'T-0002',
            'at': '2026-09-24T08:31:00Z'})
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        rows = self.rows()
        self.assertEqual([(r.kind, r.launches) for r in rows], [(feeder_rows.FIX_CORRECT, True)])

    def test_the_brief_names_the_shared_files_inside_the_boundary(self):
        self.finished('coder-t-0001', 'none')
        row = feeder_rows.Row(2, feeder_rows.PLAN_CODE, 'T-0001', 'F-0001', 'launch', 'task',
                              'worker/T-0001', 'r')
        text = briefs.build(self.product, row, self.items(), []).text
        self.assertIn('THE BOUNDARY IS `writes:` — src/a.py, tests/test_a.py — plus the shared '
                      'append-only files any Task may add to: lib/shared.py', text)


class LiveOverlapTests(WidenStepBase):
    def test_a_record_refusal_on_a_live_task_is_a_wait_on_that_task(self):
        # T-0001's own writes: is one packed entry, so the overlap with T-0002 shows only once
        # the widening splits it — the record's I3 refuses, and the row waits on T-0002
        self.active('T-0001', 'src/a.py tests/test_a.py lib/x.py')
        self.active('T-0002', 'lib/x.py')
        self.finished('coder-t-0001', 'tests/test_b.py')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WAITS}, self.lines)
        rows = self.rows()
        self.assertEqual([r.action for r in rows], ['WAITS ON T-0002'], rows)


if __name__ == '__main__':
    unittest.main()
