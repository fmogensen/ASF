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
              'web/app/items/[id]/sibling.test.ts')


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


class WidenStepTests(StepsTestCase):
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

    def active(self, iid, writes, history=''):
        """A Task Active in the record (no session), its History carrying ``history``."""
        with open(os.path.join(self.root, 'tasks', f'{iid}.md'), 'w') as f:
            f.write(TASK.format(id=iid, writes=writes)
                    .replace('\n---\n## Description', '\n# ---- machine ----\nstate: Active\n'
                             '---\n## Description', 1)
                    .replace('- made\n', '- made\n' + history))
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', f'{iid}'], self.root)

    def card_text(self, iid):
        with open(os.path.join(self.root, 'tasks', f'{iid}.md')) as f:
            return f.read()

    def check(self):
        """``asf check``'s findings on the record."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_check(types.SimpleNamespace(paths=None), self.root)
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
        head = _git(['rev-parse', 'HEAD'], self.root)
        self.tick()
        self.assertEqual(self.writes(), ['src/a.py', 'tests/test_a.py', 'lib/shared.py'])
        self.assertNotIn('reverted', self.card_text('T-0001'))
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.root), head)

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


if __name__ == '__main__':
    unittest.main()
