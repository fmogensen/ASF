"""widen_footprint — an under-scoped Task's ``writes:`` widened by rule, over facts, never a question.

The facts: the session's REPORT (``needs writes:``, else ``left out:`` of a partial report) and
the harvest gate (the branch red alone in test files outside ``writes:``). The rule
(:func:`asf.feeder.widen.decide`) widens, reshapes, holds for approval, or waits; the tick's
health step applies it (:mod:`asf.tick.widen_footprint`) and the feeder's correction rows read
its verdict.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest

from asf import approvals, briefs
from asf.feeder import rows as feeder_rows
from asf.feeder import widen
from asf.harvest import harvest, lane
from asf.record.check import cmd_check
from asf.record.index import do_index
from asf.tick import widen_footprint
from asf.views import index_reader
from asf.workers import lifecycle, report
from asf.workers import pool as pool_mod
from tests.test_tick import _git
from tests.test_tick_steps import StepsTestCase
try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from occfixture import occ
except ImportError:  # pragma: no cover - import shape only
    from tests.occfixture import occ

TASK = ('---\nid: {id}\ntype: task\ntitle: task {id}\nparent: F-0001\ndecided: true\n'
        'writes: [{writes}]\n---\n## Description\nx\n\n## Acceptance\n- [ ] it works\n\n'
        '## History\n- made\n')
REPORT = ('done my part\n\nREPORT\nitem: T-0001\nkind: coder\nstatus: partial\n'
          'branch: worker/T-0001\npushed: yes abc\ncommits: abc task(T-0001): x\n'
          'tests: ok\nleft out: {left}\nneeds writes: {needs}\n```\n')
REPO_FILES = ('src/a.py', 'tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py',
              'lib/shared.py', 'lib/x.py', 'lib/y.py', 'lib/z.py', 'LICENSE',
              'web/app/items/[id]/sibling.test.ts', 'uv.lock')


class ReportClaimTests(unittest.TestCase):
    def test_needs_writes_is_a_field_of_the_contract(self):
        self.assertIn('needs writes', report.FIELDS)
        text = REPORT.format(left='none', needs='tests/test_b.py lib/shared.py')
        self.assertEqual(report.parse(text)['needs writes'], 'tests/test_b.py lib/shared.py')
        self.assertEqual(report.footprint_claim(text),
                         ('needs writes', ['tests/test_b.py', 'lib/shared.py']))

    def test_needs_writes_none_is_a_claim_of_none(self):
        text = REPORT.format(left='`tests/test_b.py` fails', needs='none')
        self.assertEqual(report.footprint_claim(text), ('needs writes', []))

    def test_left_out_of_a_partial_report_names_its_paths(self):
        text = REPORT.replace('needs writes: {needs}\n', '').format(
            left='- `sibling.test.ts` is outside the footprint, so I left it.\n'
                 '- `web/lib/shared.ts` should carry the constant.\n\nAssumptions:\n- `x/y.ts` used')
        self.assertEqual(report.footprint_claim(text),
                         ('left out', ['sibling.test.ts', 'web/lib/shared.ts']))

    def test_a_done_and_pushed_report_claims_nothing_even_in_needs_writes(self):
        text = REPORT.replace('partial', 'done').format(left='none', needs='lib/shared.py')
        self.assertEqual(report.footprint_claim(text), (None, []))
        refused = text.replace('pushed: yes abc', 'pushed: no — hook refused')
        self.assertEqual(report.footprint_claim(refused), ('needs writes', ['lib/shared.py']))

    def test_a_done_report_claims_nothing_from_left_out(self):
        text = REPORT.replace('needs writes: {needs}\n', '').replace('partial', 'done').format(
            left='`tests/test_b.py` untouched')
        self.assertEqual(report.footprint_claim(text), (None, []))

    def test_tokens_resolve_to_the_one_tracked_path_they_name(self):
        tracked = set(REPO_FILES) | {'other/lib/x.py'}
        self.assertEqual(widen.resolve(['sibling.test.ts', 'lib/x.py', 'x.py', 'nope.py'],
                                       tracked),
                         ['web/app/items/[id]/sibling.test.ts', 'lib/x.py'])

    def test_a_route_segment_is_text_not_a_character_class(self):
        writes = ['web/app/items/[id]/actions.ts web/lib/other.ts']  # one entry, two paths
        self.assertTrue(widen.covered('web/app/items/[id]/actions.ts', writes))
        self.assertTrue(widen.covered('web/lib/other.ts', writes))
        self.assertFalse(widen.covered('web/app/(app)/bots/i/actions.ts', writes))


class DecideTests(unittest.TestCase):
    def test_the_rule(self):
        self.assertEqual(widen.decide('T-1', ['a.py', 'b.py']).kind, widen.WIDEN)
        self.assertEqual(widen.decide('T-1', [f'{i}.py' for i in range(6)]).kind, widen.RESHAPE)
        self.assertEqual(widen.decide('T-1', ['a.py'], widened_before=1).kind, widen.RESHAPE)
        v = widen.decide('T-1', ['LICENSE'], protected={'LICENSE': ('touch_legal', 'human-now')})
        self.assertEqual((v.kind, v.detail, v.level), (widen.APPROVAL, 'touch_legal', 'human-now'))
        v = widen.decide('T-1', ['lib/a.py'], running=[('T-2', ['lib/*.py']), ('T-1', ['lib/a.py'])])
        self.assertEqual((v.kind, v.detail), (widen.WAITS, 'T-2'))

    def test_a_widening_overlaps_by_asf_checks_own_test(self):
        from asf.record.core import writes_intersect
        self.assertTrue(writes_intersect('lib/*.py', 'lib/a.py'))
        self.assertEqual(widen.overlapping_widenings(['a.py', 'lib/a.py'], ['lib/a.py'],
                                                     [('T-2', ['lib/*.py'])]),
                         ('T-2', ['lib/a.py']))
        self.assertIsNone(widen.overlapping_widenings(['a.py', 'lib/a.py'], ['a.py'],
                                                      [('T-2', ['lib/*.py'])]))
        self.assertEqual(widen.widened_paths('- x footprint widened: +a.py b/c.py (report: x)\n'
                                             '- y footprint widened: +d.py (gate)\n'),
                         ['a.py', 'b/c.py', 'd.py'])

    def test_a_shared_path_widens_where_it_would_otherwise_wait(self):
        # S-37301: the widening the rule allows, where the same call with no `shared` waits
        v = widen.decide('T-0002', ['uv.lock'], running=[('T-0001', ['src/a.py', 'uv.lock'])])
        self.assertEqual((v.kind, v.detail), (widen.WAITS, 'T-0001'))
        v = widen.decide('T-0002', ['uv.lock'], running=[('T-0001', ['src/a.py', 'uv.lock'])],
                         shared=['uv.lock'])
        self.assertEqual(v.kind, widen.WIDEN)

    def test_a_shared_path_costs_nothing_against_the_cap(self):
        five = [f'{i}.py' for i in range(5)]
        # five real paths plus the lockfile: WIDEN with the set, RESHAPE without it (D3)
        v = widen.decide('T-1', five + ['uv.lock'], limit=5, shared=['uv.lock'])
        self.assertEqual(v.kind, widen.WIDEN)
        v = widen.decide('T-1', five + ['uv.lock'], limit=5)
        self.assertEqual(v.kind, widen.RESHAPE)
        # six real paths plus the lockfile: still a RESHAPE, and the reason names every path (PD9)
        six = [f'{i}.py' for i in range(6)]
        v = widen.decide('T-1', six + ['uv.lock'], limit=5, shared=['uv.lock'])
        self.assertEqual(v.kind, widen.RESHAPE)
        self.assertEqual(v.detail, widen.RESHAPE_REASON.format(
            paths=' '.join(six + ['uv.lock'])))

    def test_a_shared_path_is_still_an_approval_and_a_second_widening_is_still_a_reshape(self):
        v = widen.decide('T-1', ['uv.lock'], protected={'uv.lock': ('touch_legal', 'human-now')},
                         shared=['uv.lock'])
        self.assertEqual((v.kind, v.detail, v.level), (widen.APPROVAL, 'touch_legal', 'human-now'))
        v = widen.decide('T-1', ['uv.lock'], widened_before=1, shared=['uv.lock'])
        self.assertEqual(v.kind, widen.RESHAPE)

    def test_a_shared_path_is_never_named_as_a_widening_to_revert(self):
        self.assertIsNone(widen.overlapping_widenings(
            ['src/b.py', 'uv.lock'], ['uv.lock'], [('T-0001', ['src/a.py', 'uv.lock'])],
            shared=['uv.lock']))
        # the exemption is what changed: with no shared set, the same call reverts it
        self.assertEqual(widen.overlapping_widenings(
            ['src/b.py', 'uv.lock'], ['uv.lock'], [('T-0001', ['src/a.py', 'uv.lock'])]),
            ('T-0001', ['uv.lock']))


class WidenStepBase(StepsTestCase):
    """The health step's half: a record clone with two Tasks, a product repo that tracks the
    paths a REPORT may name, a finished coder run whose REPORT claims some of them."""

    def setUp(self):
        super().setUp()
        for rel in REPO_FILES:
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write('x\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'files'], self.repo)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.ctx_ = self.ctx()
        self.root = self.ctx_.record_root()
        os.makedirs(os.path.join(self.root, 'tasks'), exist_ok=True)
        os.makedirs(os.path.join(self.root, 'features'), exist_ok=True)
        with open(os.path.join(self.root, 'features', 'F-0001.md'), 'w') as f:
            f.write('---\nid: F-0001\ntype: feature\ntitle: sample\n---\n## Description\nx\n\n'
                    '## History\n- made\n')
        self.card('T-0001', 'src/a.py, tests/test_a.py')
        self.card('T-0002', 'lib/shared.py')
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'tasks'], self.root)

    def card(self, iid, writes):
        with open(os.path.join(self.root, 'tasks', f'{iid}.md'), 'w') as f:
            f.write(TASK.format(id=iid, writes=writes))

    def active(self, iid, writes, history=''):
        """A Task Active in the record (no session), its History carrying ``history``."""
        with open(os.path.join(self.root, 'tasks', f'{iid}.md'), 'w') as f:
            f.write(TASK.format(id=iid, writes=writes)
                    .replace('\n---\n## Description', '\n# ---- machine ----\nstate: Active\n'
                             '---\n## Description', 1)
                    .replace('- made\n', '- made\n' + history))
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', f'{iid}'], self.root)

    def items(self):
        do_index(self.root)
        return index_reader.load(self.root)[0]

    def finished(self, job, needs, kind='coder', started='2026-09-24T08:00:00Z'):
        log = os.path.join(self.tmp, f'{job}.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': REPORT.format(left='see needs', needs=needs)}) + '\n')
        self.session(job=job, item='T-0001', kind=kind, branch='worker/T-0001', pid=999999,
                     log=log, started=started)
        self.session(job=job, ended='2026-09-24T08:30:00Z', end_reason='finished')

    def tick(self):
        items = self.items()
        held, verdicts = widen_footprint.run(self.ctx_, out=self.lines.append, items=items)
        return held, verdicts

    def rows(self):
        path = pool_mod.sessions_path(self.product)
        items = self.items()
        return [r for r in feeder_rows.candidates(items, self.product,
                                                  lifecycle.inflight(path, alive=lambda _p: False),
                                                  occupancy=occ(corrections=lifecycle.corrections(path)))
                if r.item_id == 'T-0001']

    def writes(self):
        return self.items()['T-0001']['writes']

    def card_text(self, iid):
        with open(os.path.join(self.root, 'tasks', f'{iid}.md')) as f:
            return f.read()


class WidenStepTests(WidenStepBase):
    def test_a_report_naming_two_paths_widens_writes_and_relaunches_as_a_correction(self):
        self.finished('coder-t-0001', 'tests/test_b.py lib/shared.py')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'tests/test_b.py',
                                         'lib/shared.py'])
        with open(os.path.join(self.root, 'tasks', 'T-0001.md')) as f:
            self.assertIn('footprint widened: +tests/test_b.py lib/shared.py (report: needs writes)',
                          f.read())
        subject = _git(['log', '-1', '--format=%s'], self.root).strip()
        self.assertEqual(subject, 'T-0001: footprint widened: +tests/test_b.py lib/shared.py '
                                  '(report: needs writes)')
        self.assertEqual(_git(['show', '--name-only', '--format=', 'HEAD'], self.root).split(),
                         ['tasks/T-0001.md'])  # one commit, the card alone
        rows = self.rows()
        self.assertEqual([(r.kind, r.brief_kind, r.launches) for r in rows],
                         [(feeder_rows.FIX_CORRECT, 'correct', True)], rows)
        self.assertIn('writes: is now src/a.py tests/test_a.py tests/test_b.py lib/shared.py',
                      rows[0].correction)
        brief = briefs.build(self.product, rows[0], self.items(), [])
        self.assertIn('THE BOUNDARY IS `writes:` — src/a.py, tests/test_a.py, tests/test_b.py, '
                      'lib/shared.py', brief.text)
        self.assertIn('footprint widened: +tests/test_b.py lib/shared.py', brief.text)
        # decided once: the next tick changes nothing
        self.assertEqual(self.tick(), ([], {}))

    def test_six_paths_are_a_reshape_not_a_wider_task(self):
        self.finished('coder-t-0001', 'tests/test_b.py tests/test_c.py lib/shared.py lib/x.py '
                                      'lib/y.py lib/z.py')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.RESHAPE})
        items = self.items()
        self.assertEqual(items['T-0001']['writes'], ['src/a.py', 'tests/test_a.py'])
        self.assertTrue(items['T-0001']['reshape'].startswith('footprint: needs tests/test_b.py'))
        rows = self.rows()
        self.assertEqual([(r.kind, r.brief_kind, r.launches) for r in rows],
                         [(feeder_rows.RESHAPE, 'reshape', True)], rows)
        self.assertTrue(rows[0].reason.startswith('footprint: needs '))
        self.assertFalse(any('NEEDS OPERATOR' in l for l in self.lines), self.lines)

    def test_a_protected_path_goes_to_its_approval_class(self):
        self.finished('coder-t-0001', 'LICENSE')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.APPROVAL})
        self.assertEqual([(h['item'], h['class']) for h in approvals.open_holds(self.product)],
                         [('T-0001', 'touch_legal')])
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        rows = self.rows()
        self.assertEqual([(r.action, r.launches) for r in rows],
                         [('WAITS ON approval touch_legal', False)])
        # granted: the next tick widens
        approvals.resolve(self.product, 'T-0001/touch_legal', 'granted')
        self.assertEqual(self.tick()[1], {'coder-t-0001': widen.WIDEN})
        self.assertIn('LICENSE', self.writes())

    def pending(self, job='coder-t-0001'):
        path = pool_mod.sessions_path(self.product)
        return lifecycle.pending_correction(lifecycle.latest(path)[job], path)

    def test_a_dropped_widening_is_never_asked_again_and_the_task_moves_on(self):
        self.finished('coder-t-0001', 'LICENSE')
        self.assertEqual(self.tick()[1], {'coder-t-0001': widen.APPROVAL})
        approvals.resolve(self.product, 'T-0001/touch_legal', 'dropped')
        for _ in range(3):  # tick after tick: the drop stands
            self.tick()
            self.assertEqual(approvals.open_holds(self.product), [], self.lines)
        entry = approvals.holds(self.product)['T-0001/touch_legal']
        self.assertEqual((entry['count'], entry['resolution']), (1, 'dropped'))
        self.assertIsNone(self.pending())  # no correction: the branch goes on to review
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertTrue(any('not asked again' in l for l in self.lines), self.lines)

    def test_a_dropped_widening_asks_again_for_a_different_path_set(self):
        self.finished('coder-t-0001', 'LICENSE')
        self.tick()
        approvals.resolve(self.product, 'T-0001/touch_legal', 'dropped')
        self.tick()
        # a later correction session claims more: a new question
        self.finished('correct-t-0001', 'LICENSE tests/test_b.py', kind='correct',
                      started='2026-09-24T09:00:00Z')
        self.tick()
        self.assertEqual([h['hold'] for h in approvals.open_holds(self.product)],
                         ['T-0001/touch_legal'], self.lines)
        self.assertEqual(approvals.holds(self.product)['T-0001/touch_legal']['count'], 2)

    def test_a_stale_report_claim_off_a_done_pushed_run_is_dropped_and_its_hold_closed(self):
        # T-0349 live: a footprint correction written before the done-and-pushed rule, its
        # approval hold dropped, re-asked each tick. The REPORT (done, pushed) claims nothing.
        log = os.path.join(self.tmp, 'coder-t-0001.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': REPORT.replace('status: partial', 'status: done')
                                .format(left='none', needs='LICENSE')}) + '\n')
        self.session(job='coder-t-0001', item='T-0001', kind='coder', branch='worker/T-0001',
                     pid=999999, log=log, started='2026-09-24T08:00:00Z')
        self.session(job='coder-t-0001', ended='2026-09-24T08:30:00Z', end_reason='finished')
        fields, _line = lifecycle.footprint_hold(
            {'branch': 'worker/T-0001'}, ['LICENSE'], 'report: needs writes', 'needs LICENSE',
            '2026-09-24T08:31:00Z')
        fields['correction'].update(verdict=widen.APPROVAL, detail='touch_legal')
        self.session(job='coder-t-0001', footprint_read=1, **fields)
        approvals.refuse(self.product, 'T-0001', 'touch_legal', 'human-now', 'coder-t-0001',
                         'widen', 'widen writes: +LICENSE (report: needs writes)')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen_footprint.DROPPED}, self.lines)
        self.assertIsNone(self.pending())
        self.assertEqual(approvals.open_holds(self.product), [])
        self.assertEqual(approvals.holds(self.product)['T-0001/touch_legal']['resolution'],
                         'done')
        self.assertEqual(self.tick(), ([], {}))  # nothing re-raised
        self.assertEqual(approvals.open_holds(self.product), [])

    def test_a_path_a_running_task_writes_waits_on_that_task(self):
        self.session(job='coder-t-0002', item='T-0002', kind='coder', branch='worker/T-0002',
                     pid=os.getpid(), started='2026-09-24T08:10:00Z')
        self.finished('coder-t-0001', 'lib/shared.py')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WAITS})
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        rows = self.rows()
        self.assertEqual([(r.action, r.waits_on, r.launches) for r in rows],
                         [('WAITS ON T-0002', 'T-0002', False)])

    def check(self):
        """``asf check``'s findings on the record, over ``self.product`` (named explicitly, since
        the test's cwd resolves no product of its own) — so a declared ``shared_paths`` exempts
        the same overlaps here as it does for the widen tick itself."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_check(types.SimpleNamespace(paths=None, product=self.product.name), self.root)
        return buf.getvalue()

    def test_a_path_an_active_task_writes_waits_though_no_session_runs_it(self):
        self.active('T-0002', 'lib/shared.py')
        self.finished('coder-t-0001', 'lib/shared.py')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WAITS}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertEqual([(r.action, r.waits_on, r.launches) for r in self.rows()],
                         [('WAITS ON T-0002', 'T-0002', False)])

    def test_two_widenings_in_one_pass_on_one_path_only_the_first_widens(self):
        self.card('T-0003', 'src/c.py')
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'T-0003'], self.root)
        self.finished('coder-t-0001', 'lib/x.py')
        log = os.path.join(self.tmp, 'coder-t-0003.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': REPORT.replace('T-0001', 'T-0003').format(
                                    left='see needs', needs='lib/x.py')}) + '\n')
        self.session(job='coder-t-0003', item='T-0003', kind='coder', branch='worker/T-0003',
                     pid=999999, log=log, started='2026-09-24T08:00:00Z')
        self.session(job='coder-t-0003', ended='2026-09-24T08:30:00Z', end_reason='finished')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN, 'coder-t-0003': widen.WAITS},
                         self.lines)
        items = self.items()
        self.assertIn('lib/x.py', items['T-0001']['writes'])
        self.assertNotIn('lib/x.py', items['T-0003']['writes'])

    def test_a_widened_overlap_in_the_record_is_reverted_and_waits_on_the_owner(self):
        self.active('T-0002', 'lib/shared.py')
        self.active('T-0001', 'src/a.py, tests/test_a.py, lib/shared.py',
                    '- 2026-09-24 08:40 footprint widened: +lib/shared.py (report: needs writes)\n')
        self.assertIn("intersects Active task", self.check())
        before = _git(['rev-list', '--count', 'HEAD'], self.root).strip()
        self.tick()
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertEqual(self.items()['T-0001']['after'], ['T-0002'])
        self.assertIn('footprint widening reverted: overlaps T-0002', self.card_text('T-0001'))
        self.assertEqual(int(_git(['rev-list', '--count', 'HEAD'], self.root)), int(before) + 1)
        self.assertEqual(_git(['show', '--name-only', '--format=', 'HEAD'], self.root).split(),
                         ['tasks/T-0001.md'])
        self.assertNotIn('intersects Active task', self.check())
        self.assertIn('lib/shared.py', self.items()['T-0002']['writes'])  # the owner untouched
        # repaired once: the next pass changes nothing
        self.tick()
        self.assertEqual(int(_git(['rev-list', '--count', 'HEAD'], self.root)), int(before) + 1)

    def test_a_plan_declared_overlap_is_never_reverted(self):
        self.active('T-0002', 'lib/shared.py')
        self.active('T-0001', 'src/a.py, tests/test_a.py, lib/shared.py',
                    '- 2026-09-24 08:40 footprint widened: +tests/test_a.py (report: needs writes)\n')
        self.tick()
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'lib/shared.py'])
        self.assertNotIn('reverted', self.card_text('T-0001'))
        # not reverted — the plan-declared overlap still stands between two Active Tasks, so the
        # tick's order pass (T-0545) serializes it onto the later card instead of a person
        self.assertEqual(self.items()['T-0002']['after'], ['T-0001'])

    def test_a_second_widening_is_a_reshape(self):
        self.finished('coder-t-0001', 'tests/test_b.py')
        self.assertEqual(self.tick()[1], {'coder-t-0001': widen.WIDEN})
        # the correction ran (launched after the hold) and again needs more
        self.finished('correct-t-0001', 'lib/x.py', kind='correct', started='2999-01-01T00:00:00Z')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'correct-t-0001': widen.RESHAPE}, self.lines)
        items = self.items()
        self.assertEqual(items['T-0001']['reshape'], 'footprint: needs lib/x.py')
        self.assertNotIn('lib/x.py', items['T-0001']['writes'])
        self.assertEqual([r.kind for r in self.rows()], [feeder_rows.RESHAPE])


class FeatureFootprintTests(WidenStepBase):
    """A path inside the Task's Feature footprint (its sibling Tasks' ``writes:``) is widened by
    rule — past the cap and a second widening — and a diff that already carries it spawns no
    correction. Outside the Feature footprint, today's rules stand."""

    def branch(self, *paths):
        """Push ``worker/T-0001`` changing ``paths`` on top of main."""
        _git(['checkout', '-q', '-b', 'worker/T-0001', 'main'], self.repo)
        for rel in paths:
            full = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'a') as f:
                f.write('changed\n')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'task(T-0001): work'], self.repo)
        _git(['push', '-q', 'origin', 'worker/T-0001'], self.repo)
        _git(['checkout', '-q', 'main'], self.repo)

    def test_the_feature_footprint_is_the_union_of_its_tasks_writes(self):
        items = {'F-1': {'type': 'feature'}, 'S-1': {'type': 'story', 'parent': 'F-1'},
                 'T-1': {'type': 'task', 'parent': 'F-1', 'writes': ['a.py']},
                 'T-2': {'type': 'task', 'parent': 'S-1', 'writes': ['b/ c.py']},
                 'T-3': {'type': 'task', 'parent': 'F-1', 'writes': ['x.py'], 'removed': 'y'},
                 'T-4': {'type': 'task', 'parent': 'F-2', 'writes': ['z.py']}}
        self.assertEqual(widen.delivery_footprint(items, 'T-1'), ['a.py', 'b/', 'c.py'])
        self.assertEqual(widen.delivery_footprint(items, 'T-4'), [])
        self.assertTrue(widen.inside_feature(['b/q.py', 'a.py'], ['a.py', 'b/']))
        self.assertFalse(widen.inside_feature(['z.py'], ['a.py']))
        self.assertFalse(widen.inside_feature([], ['a.py']))

    def test_inside_the_feature_neither_the_cap_nor_a_second_widening_reshapes(self):
        v = widen.decide('T-1', ['a', 'b', 'c'], limit=1, widened_before=1, in_feature=True)
        self.assertEqual(v.kind, widen.WIDEN)
        self.assertEqual(widen.decide('T-1', ['a', 'b'], limit=1).kind, widen.RESHAPE)

    def test_a_second_widening_inside_the_feature_widens(self):
        self.finished('coder-t-0001', 'tests/test_b.py')
        self.assertEqual(self.tick()[1], {'coder-t-0001': widen.WIDEN})
        self.finished('correct-t-0001', 'lib/shared.py', kind='correct',
                      started='2999-01-01T00:00:00Z')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'correct-t-0001': widen.WIDEN}, self.lines)
        self.assertIn('lib/shared.py', self.writes())
        self.assertFalse(self.items()['T-0001'].get('reshape'))

    def test_a_diff_inside_the_feature_footprint_widens_with_no_correction(self):
        self.branch('src/a.py', 'lib/shared.py')
        self.finished('coder-t-0001', 'none')
        self.tick()
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'lib/shared.py'])
        self.assertIn('footprint widened: +lib/shared.py (diff: inside the Feature footprint)',
                      self.card_text('T-0001'))
        path = pool_mod.sessions_path(self.product)
        self.assertEqual(lifecycle.corrections(path), {})
        run = lifecycle.latest(path)['coder-t-0001']
        self.assertEqual(run['widened'], ['lib/shared.py'])
        self.assertEqual(self.rows(), [])  # nothing launched: it goes on to review as it is
        self.assertTrue(any('no correction' in l for l in self.lines), self.lines)
        self.assertEqual(self.tick(), ([], {}))  # read once

    def test_a_diff_outside_the_feature_footprint_keeps_todays_behaviour(self):
        self.branch('src/a.py', 'lib/x.py')
        self.finished('coder-t-0001', 'none')
        self.tick()
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertNotIn('footprint widened', self.card_text('T-0001'))
        self.assertEqual(lifecycle.corrections(pool_mod.sessions_path(self.product)), {})

    def test_a_diff_a_running_sibling_writes_waits_and_is_left_to_review(self):
        self.active('T-0002', 'lib/shared.py')
        self.branch('src/a.py', 'lib/shared.py')
        self.finished('coder-t-0001', 'none')
        self.tick()
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertTrue(any('waits T-0002' in l for l in self.lines), self.lines)

    def test_a_report_claim_the_diff_already_carries_is_widened_with_no_correction(self):
        self.branch('src/a.py', 'lib/shared.py')
        self.finished('coder-t-0001', 'lib/shared.py')
        pool_mod.update_session(self.product, 'coder-t-0001', diff_read=1)  # the REPORT's path
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertIn('lib/shared.py', self.writes())
        path = pool_mod.sessions_path(self.product)
        self.assertEqual(lifecycle.corrections(path), {})
        self.assertEqual(self.rows(), [])


class DeliveryLeadFootprintTests(WidenStepBase):
    """A delivery lead's branch carries its members' work: the footprint gate measures it
    against the union of the lead's and its delivered members' ``writes:``, not the lead's own
    alone (a lead held every tick for a member's path)."""

    def lead(self):
        with open(os.path.join(self.root, 'tasks', 'T-0001.md'), 'w') as f:
            f.write(TASK.format(id='T-0001', writes='src/a.py, tests/test_a.py')
                    .replace('decided: true\n', 'decided: true\ndelivers: [T-0001, T-0002]\n'))
        with open(os.path.join(self.root, 'tasks', 'T-0002.md'), 'w') as f:
            f.write(TASK.format(id='T-0002', writes='lib/shared.py')
                    .replace('decided: true\n', 'decided: true\ndelivered_by: T-0001\n'))
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'delivery'], self.root)

    def test_reach_is_the_union_of_the_lead_and_its_members(self):
        self.lead()
        items = self.items()
        got = widen_footprint.reach(self.product, items['T-0001'], items)
        for w in ('src/a.py', 'tests/test_a.py', 'lib/shared.py'):
            self.assertIn(w, got)
        # a member alone reaches its own writes only
        self.assertNotIn('src/a.py', widen_footprint.reach(self.product, items['T-0002'], items))

    def test_the_lane_gate_counts_a_member_named_only_by_delivered_by(self):
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'writes': ['src/a.py'],
                            'delivers': ['T-0001']},
                 'T-0002': {'id': 'T-0002', 'type': 'task', 'writes': ['lib/shared.py'],
                            'delivered_by': 'T-0001'}}
        self.assertEqual(lane.item_footprint(items, 'T-0001'), ['src/a.py', 'lib/shared.py'])
        self.assertIn('lib/shared.py', widen_footprint.reach(self.product, items['T-0001'], items))

    def test_a_lead_claiming_a_members_path_is_not_held(self):
        self.lead()
        self.finished('coder-t-0001', 'lib/shared.py')
        held, verdicts = self.tick()
        self.assertEqual((held, verdicts), ([], {}), self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])


class SharedPathWidenTests(WidenStepBase):
    """S-37301: a shared path widens freely, through the whole tick — the widen rule's exemption
    and I3's have to hold together, or the write the rule allows is refused before it lands
    (PD1)."""

    product_extra = 'steps:\n  batch: off\nconventions:\n  shared_paths: [uv.lock]\n'

    active = WidenStepTests.active
    check = WidenStepTests.check

    def test_a_shared_path_widens_and_the_widening_stands(self):
        self.active('T-0002', 'lib/shared.py, uv.lock')
        self.finished('coder-t-0001', 'uv.lock')
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertIn('uv.lock', self.writes())
        self.assertIn('footprint widened: +uv.lock', self.card_text('T-0001'))
        self.assertNotIn('intersects Active task', self.check())
        before = int(_git(['rev-list', '--count', 'HEAD'], self.root))
        # a second tick neither reverts the widening (P6) nor serializes it behind T-0002 (T-0545)
        self.tick()
        self.assertEqual(int(_git(['rev-list', '--count', 'HEAD'], self.root)), before)
        self.assertNotIn('footprint widening reverted', self.card_text('T-0001'))
        self.assertFalse(self.items()['T-0001'].get('after'))


class SerializeOverlapTests(WidenStepBase):
    """T-0545: the tick's third pass orders every standing Active×Active overlap the record has
    not ordered (:func:`asf.invariants.unordered_overlaps`) — the later-minted card gets
    ``after: <the earlier>``, one History line, one commit — re-asking reachability against its
    own edges before every write so a batch of candidates never closes a cycle (PD3), and reading
    the record rather than ``items`` so a chain through a removed card still counts as ordered
    (PD4)."""

    check = WidenStepTests.check

    def rows(self, iid):
        """``WidenStepBase.rows`` filters on ``T-0001``; the held card here is ``T-0002`` (PD10)."""
        path = pool_mod.sessions_path(self.product)
        items = self.items()
        return [r for r in feeder_rows.candidates(
                    items, self.product, lifecycle.inflight(path, alive=lambda _p: False),
                    occupancy=occ(corrections=lifecycle.corrections(path)))
                if r.item_id == iid]

    def _edit(self, iid, fn, msg=None):
        path = os.path.join(self.root, 'tasks', f'{iid}.md')
        with open(path) as f:
            text = f.read()
        with open(path, 'w') as f:
            f.write(fn(text))
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', msg or iid], self.root)

    def _after(self, iid, ids):
        self._edit(iid, lambda t: t.replace(
            'decided: true\n', f"decided: true\nafter: [{', '.join(ids)}]\n", 1))

    def _close(self, iid):
        self._edit(iid, lambda t: t.replace('state: Active\n', 'state: Closed\n', 1),
                   f'{iid} closed')

    def _remove(self, iid, note):
        self._edit(iid, lambda t: t.replace(
            'decided: true\n', f"decided: true\nremoved: {note}\n", 1))

    def test_an_unordered_pair_is_serialized_onto_the_later_minted_card(self):
        self.active('T-0001', 'lib/shared.py')
        self.active('T-0002', 'lib/shared.py')
        self.assertIn('intersects Active task', self.check())
        held, verdicts = self.tick()
        self.assertEqual((held, verdicts), ([], {}))
        self.assertIsNone(self.items()['T-0001'].get('after'))
        self.assertEqual(self.items()['T-0002']['after'], ['T-0001'])
        self.assertIn('serialized behind T-0001: writes: overlaps lib/shared.py',
                      self.card_text('T-0002'))
        self.assertNotIn('serialized', self.card_text('T-0001'))
        subject = _git(['log', '-1', '--format=%s'], self.root).strip()
        self.assertEqual(subject,
                         'T-0002: serialized behind T-0001: writes: overlaps lib/shared.py')
        self.assertEqual(_git(['show', '--name-only', '--format=', 'HEAD'], self.root).split(),
                         ['tasks/T-0002.md'])  # one commit, the held card alone
        self.assertNotIn('intersects Active task', self.check())

    def test_the_held_tasks_rows_wait_then_launch_once_the_owner_lands(self):
        self.active('T-0001', 'lib/shared.py')
        self.active('T-0002', 'lib/shared.py')
        self.tick()
        rows = self.rows('T-0002')
        self.assertTrue(rows)
        self.assertEqual([(r.action, r.waits_on, r.reason) for r in rows],
                         [('WAITS ON T-0001', 'T-0001', 'after: T-0001 has not landed')])
        self._close('T-0001')
        rows = self.rows('T-0002')
        self.assertTrue(rows)
        self.assertEqual(rows[0].action, feeder_rows.LAUNCH)

    def test_a_second_pass_writes_nothing(self):
        self.active('T-0001', 'lib/shared.py')
        self.active('T-0002', 'lib/shared.py')
        self.tick()
        before = _git(['rev-parse', 'HEAD'], self.root)
        self.assertEqual(self.tick(), ([], {}))
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.root), before)
        self.assertEqual(self.items()['T-0002']['after'], ['T-0001'])

    def test_a_pair_already_ordered_the_other_way_is_untouched(self):
        self.active('T-0001', 'lib/shared.py')
        self.active('T-0002', 'lib/shared.py')
        self._after('T-0001', ['T-0002'])
        head = _git(['rev-parse', 'HEAD'], self.root)
        self.tick()
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.root), head)
        self.assertEqual(self.items()['T-0001']['after'], ['T-0002'])
        self.assertIsNone(self.items()['T-0002'].get('after'))

    def test_a_candidate_batch_never_closes_a_cycle(self):
        # PD3: three Tasks A < C < B by id (T-0001 < T-0002 < T-0003), all pairwise overlapping,
        # with a pre-existing low->high edge A after B (T-0001 after: [T-0003]) — the shape a
        # plan's ordinal-based backfill leaves in the live record. A pass that trusts one
        # snapshot of candidates proposes C->A and B->C against the *original* graph and closes
        # A->B->C->A; re-asking reachability after every write must propose fewer edges than
        # there are candidate pairs and leave no cycle.
        self.card('T-0003', 'lib/shared.py')
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'T-0003'], self.root)
        self.active('T-0001', 'lib/shared.py')
        self._after('T-0001', ['T-0003'])
        self.active('T-0002', 'lib/shared.py')
        self.active('T-0003', 'lib/shared.py')
        self.assertEqual(self.check().count('intersects Active task'), 2)
        self.tick()
        items = self.items()
        self.assertEqual({iid: items[iid].get('after') or [] for iid in
                          ('T-0001', 'T-0002', 'T-0003')},
                         {'T-0001': ['T-0003'], 'T-0002': ['T-0001'], 'T-0003': []})
        self.assertNotIn('intersects Active task', self.check())

    def test_a_pair_ordered_through_a_removed_intermediate_gets_no_commit(self):
        # PD4: B after [X], X after [A], X removed — the index drops a removed card, but the
        # record still reads the chain, and this pass must read the record.
        self.active('T-0001', 'lib/shared.py')
        self.active('T-0002', 'lib/shared.py')
        self.active('T-0003', 'lib/shared.py')
        self._after('T-0002', ['T-0001'])
        self._remove('T-0002', 'merged into T-0001')
        self._after('T-0003', ['T-0002'])
        head = _git(['rev-parse', 'HEAD'], self.root)
        self.tick()
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.root), head)

    def test_a_widened_overlap_still_reverts_rather_than_serializes(self):
        self.active('T-0002', 'lib/shared.py')
        self.active('T-0001', 'src/a.py, tests/test_a.py, lib/shared.py',
                    '- 2026-09-24 08:40 footprint widened: +lib/shared.py (report: needs writes)\n')
        before = _git(['rev-list', '--count', 'HEAD'], self.root).strip()
        self.tick()
        self.assertIn('footprint widening reverted: overlaps T-0002', self.card_text('T-0001'))
        self.assertNotIn('serialized', self.card_text('T-0001'))
        self.assertNotIn('serialized', self.card_text('T-0002'))
        # one commit only: the revert, not a serialization on top of it
        self.assertEqual(int(_git(['rev-list', '--count', 'HEAD'], self.root)), int(before) + 1)


class RefusalWidenTests(WidenStepBase):
    """T-0338: a push the product's hook refused over a doc outside ``writes:`` widens the Task
    and sends it back with no round spent. T-0349: advisory paths off a push that went through
    never do."""

    REFUSAL = ("no — pre-push docs-check hook fails: lib/shared.py:27 quotes src/a.py's "
               "`pattern=` line verbatim, and the required change makes that quote stale. The "
               "file is outside writes:, so it is not touched. publish worker/T-0001 refused: "
               "error: failed to push some refs to 'https://github.com/o/r.git'")

    def refused(self, job='coder-t-0001', text=REFUSAL):
        self.session(job=job, item='T-0001', kind='coder', branch='worker/T-0001', pid=999999,
                     started='2026-09-24T08:00:00Z')
        self.session(job=job, ended='2026-09-24T08:30:00Z',
                     end_reason=f'failed: {lifecycle.HOOK_REFUSED}: {text}')
        self.session(job=job, correction={
            'kind': lifecycle.HOOK_REFUSED, 'at': '2026-09-24T08:30:00Z', 'refusal': 'gate',
            'text': f"the push was refused by the repo's own hook — {text} — fix what it "
                    f"names, commit, and push again"})

    def rounds(self):
        path = pool_mod.sessions_path(self.product)
        return lifecycle.latest(path)['coder-t-0001'].get('rounds')

    def test_a_refusal_naming_a_path_outside_writes_widens_and_resends_with_no_round(self):
        self.refused()
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'lib/shared.py'])
        self.assertIn('footprint widened: +lib/shared.py (hook refused: pre-push docs-check',
                      self.card_text('T-0001'))
        widened = [l for l in self.lines if l.startswith('widened writes:')]
        self.assertEqual(len(widened), 1, self.lines)
        self.assertTrue(widened[0].startswith(
            'widened writes: +lib/shared.py — hook refused: pre-push docs-check hook fails'),
            widened)
        self.assertFalse(self.rounds())  # no round spent
        rows = self.rows()
        self.assertEqual([(r.kind, r.brief_kind, r.launches) for r in rows],
                         [(feeder_rows.FIX_CORRECT, 'correct', True)], rows)
        self.assertIn('writes: is now src/a.py tests/test_a.py lib/shared.py', rows[0].correction)
        self.assertEqual(self.tick(), ([], {}))  # decided once

    def test_a_refused_path_another_task_writes_waits_on_that_task(self):
        self.session(job='coder-t-0002', item='T-0002', kind='coder', branch='worker/T-0002',
                     pid=os.getpid(), started='2026-09-24T08:10:00Z')
        self.refused()
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WAITS}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertEqual([(r.action, r.waits_on, r.launches) for r in self.rows()],
                         [('WAITS ON T-0002', 'T-0002', False)])

    def test_a_refused_path_an_active_task_writes_waits_though_no_session_runs_it(self):
        # the card's acceptance: an overlap with a Task Active in the record — no session at
        # all — holds the Task instead of widening it (open_footprints reads the record too)
        self.active('T-0002', 'lib/shared.py')
        self.refused()
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WAITS}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        self.assertEqual([(r.action, r.waits_on, r.launches) for r in self.rows()],
                         [('WAITS ON T-0002', 'T-0002', False)])

    def test_a_refused_path_a_parked_later_task_writes_widens_and_corrects(self):
        # B-82960: the only holder is an Active Task under a ``priority: later`` Feature whose
        # writes: intersect — it holds no footprint, so the FIX → CORRECT row never waits on it
        with open(os.path.join(self.root, 'features', 'F-0002.md'), 'w') as f:
            f.write('---\nid: F-0002\ntype: feature\ntitle: parked\npriority: later\n---\n'
                    '## Description\nx\n\n## History\n- made\n')
        self.active('T-0002', 'lib/shared.py')
        with open(os.path.join(self.root, 'tasks', 'T-0002.md')) as f:
            text = f.read().replace('parent: F-0001', 'parent: F-0002')
        with open(os.path.join(self.root, 'tasks', 'T-0002.md'), 'w') as f:
            f.write(text)
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'parked'], self.root)
        self.assertEqual(self.items()['T-0002'].get('state'), 'Active')
        self.refused()
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'lib/shared.py'])
        self.assertEqual([(r.kind, r.waits_on, r.launches) for r in self.rows()],
                         [(feeder_rows.FIX_CORRECT, '', True)])

    def test_a_refusal_at_the_cap_widens_rather_than_going_to_adjudication(self):
        # B-0140 marks the second identical refusal at_cap, and lifecycle.derive routes an
        # at_cap correction to ADJUDICATE. The widening runs first (step_health: health, then
        # widen_footprints) and footprint_hold writes a fresh correction, so the Task comes
        # back as a correction — it is never marked dead. The feeder's row is FIX → CORRECT
        # either way (P11), so derive is what discriminates.
        self.refused()
        path = pool_mod.sessions_path(self.product)
        corr = lifecycle.latest(path)['coder-t-0001']['correction']
        self.session(job='coder-t-0001', correction=dict(corr, at_cap=True, same=2))
        run = lifecycle.latest(path)['coder-t-0001']
        self.assertEqual(lifecycle.derive(run, lifecycle.Evidence(), path=path).name,
                         lifecycle.ADJUDICATE)  # what the at_cap correction alone would be
        _held, verdicts = self.tick()
        self.assertEqual(verdicts, {'coder-t-0001': widen.WIDEN}, self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'lib/shared.py'])
        run = lifecycle.latest(path)['coder-t-0001']
        self.assertEqual(run['correction']['kind'], lifecycle.FOOTPRINT)
        self.assertNotIn('at_cap', run['correction'])
        self.assertEqual(lifecycle.derive(run, lifecycle.Evidence(), path=path).name,
                         lifecycle.HELD)
        self.assertFalse(self.rounds())  # no round spent
        self.assertEqual([(r.kind, r.brief_kind, r.launches) for r in self.rows()],
                         [(feeder_rows.FIX_CORRECT, 'correct', True)])

    def test_a_refusal_naming_only_its_own_files_stays_a_plain_hook_correction(self):
        self.refused(text='no — pre-push lint fails: src/a.py:3 unused import')
        held, verdicts = self.tick()
        self.assertEqual((held, verdicts), ([], {}), self.lines)
        path = pool_mod.sessions_path(self.product)
        corr = lifecycle.latest(path)['coder-t-0001']['correction']
        self.assertEqual(corr['kind'], lifecycle.HOOK_REFUSED)
        # the footprint_read path leaves the plain correction's class as hook_refusal_hold
        # wrote it (P11's "keeps first claim") — refusal_facts never touches it here
        self.assertEqual(corr['refusal'], 'gate')
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])

    def test_a_turned_correction_carries_the_footprint_refusal_class(self):
        # F-0235, P11: refusal_facts's own claim overwrites whatever hook_refusal_hold wrote,
        # the moment a refusal is turned into a footprint hold
        self.refused()
        items = self.items()
        turned = widen_footprint.refusal_facts(self.ctx_, items, out=self.lines.append)
        self.assertEqual(turned, ['coder-t-0001'], self.lines)
        path = pool_mod.sessions_path(self.product)
        corr = lifecycle.latest(path)['coder-t-0001']['correction']
        self.assertEqual(corr['kind'], lifecycle.FOOTPRINT)
        self.assertEqual(corr['refusal'], 'footprint')

    def test_advisory_paths_off_a_push_that_went_through_never_widen(self):
        # T-0349: done, pushed; an advisory "touched-vs-listed FAIL" row named paths
        log = os.path.join(self.tmp, 'coder-t-0001.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': REPORT.replace('status: partial', 'status: done')
                                .format(left='none', needs='lib/shared.py tests/test_b.py')})
                    + '\n')
        self.session(job='coder-t-0001', item='T-0001', kind='coder', branch='worker/T-0001',
                     pid=999999, log=log, started='2026-09-24T08:00:00Z')
        self.session(job='coder-t-0001', ended='2026-09-24T08:30:00Z', end_reason='finished')
        held, verdicts = self.tick()
        self.assertEqual((held, verdicts), ([], {}), self.lines)
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py'])
        path = pool_mod.sessions_path(self.product)
        self.assertIsNone(lifecycle.pending_correction(lifecycle.latest(path)['coder-t-0001'],
                                                       path))


class HarvestWidenTests(unittest.TestCase):
    """Point 4 without a gate: a red test that imports a file the branch changed is the branch's
    red wherever it sits — not foreign; outside ``writes:`` it goes to widening."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='widen_')
        self.addCleanup(shutil.rmtree, self.state, True)
        self.record = {'job': 'coder-t-0080', 'item': 'T-0080', 'branch': 'worker/T-0080'}
        self.lines = []

    def hold(self, sources, own=False):
        return lane.hold_with_correction(
            self.state, 'worker/T-0080', self.record, 'gate', 'FAIL: test_other', self.lines.append,
            ['tests/test_other.py'], ['src/a.py'], ['src/a.py'], own=own,
            read=lambda p: sources.get(p))

    def test_a_red_test_importing_a_changed_file_is_widening_not_foreign(self):
        got = self.hold({'tests/test_other.py': 'from src.a import f\n'})
        self.assertEqual(got, 'held', self.lines)
        corr = harvest.read_sessions(self.state)['coder-t-0080']['correction']
        self.assertEqual((corr['kind'], corr['needs']), ('footprint', ['tests/test_other.py']))
        self.assertFalse(any(l.startswith('foreign ') for l in self.lines), self.lines)
        self.assertNotIn('rounds', harvest.read_sessions(self.state)['coder-t-0080'])

    def test_a_red_test_that_imports_nothing_changed_stays_foreign(self):
        self.assertEqual(self.hold({'tests/test_other.py': 'import os\n'}), 'foreign')

    def test_js_imports_resolve_relative_and_aliased(self):
        stems = widen.import_stems("import { other } from '../lib/other'\nimport x from '@/lib/y'\n",
                                   'web/app/x.test.ts')
        self.assertEqual(widen.imported(stems, ['web/lib/other.ts', 'web/lib/y.tsx', 'web/z.ts']),
                         ['web/lib/other.ts', 'web/lib/y.tsx'])


# S-77505's last bullet — Task 2 (``Brief.writes_boundary`` → ``pool.Row`` → ``spawn``'s env)
# touches neither ``asf.tick.widen_footprint`` nor ``asf.feeder.widen``, so the tick still widens
# and still holds exactly what it holds today — is a no-change assertion. The plan writes it as
# ``git diff --exit-code origin/main -- asf/tick/widen_footprint.py asf/feeder/widen.py`` in the
# Gate, plus this module's existing cases green, and "not as a new behavioural test of code this
# Task does not touch" (f-0301.md:342-346). A part job's checkout is shallow and single-ref, so no
# in-process git call can resolve the trunk to diff against.


if __name__ == '__main__':
    unittest.main()
