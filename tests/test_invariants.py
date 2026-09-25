"""R9–R13 — the invariant registry (:mod:`asf.invariants`), its three check points in the tick,
and ``asf check --invariants``.

- R9: every check point fails soft. record → the offending paths are put back, the rest commits,
  one Bug per ``(invariant, path)``; feeder → the violating row is dropped and logged; lane →
  reported. A check that raises is a line, never an aborted tick.
- R10: I4 is "at most one launching row per branch, of the kind its lane state allows" — the
  REVIEW launch is legal.
- R11: I2 runs after ingest's restamp (``tests/test_record_stage.py``; re-asserted here through
  the registry).
- R12: I9 is an event, not a violation: a human merge in PR mode is legitimate.
- R13: I6 is daily/deep only; I12 and I13 are tests, never tick checks.

One positive and one negative case per tick invariant (I4, I5, I7, I8), plus I6 and I9.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, invariants, redact
from asf.feeder.rows import LAUNCH, Row
from asf.record import frontmatter, stage
from asf.tick import tick

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_invariants` does not
    from test_tick import TickTestCase, _git
except ImportError:  # pragma: no cover - import shape only
    from tests.test_tick import TickTestCase, _git


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return None


def row(kind, item, branch, brief='task', feature='', launches=True):
    return Row(tier=2, kind=kind, item_id=item, feature_id=feature,
               action=LAUNCH if launches else 'WAITS ON x', brief_kind=brief, branch=branch,
               reason='test')


def feeder(rows, **kw):
    return invariants.FeederContext(rows=rows, **kw)


def ids(findings):
    return [(f.invariant, f.subject) for f in findings]


class R10I4OneLaunchPerBranchOfItsKind(unittest.TestCase):
    def test_r10_the_review_launch_on_a_review_branch_is_legal(self):
        rows = [row('PUSHED → REVIEW', 'T-0001', 'worker/T-0001', brief='review')]
        ctx = feeder(rows, lanes={'worker/T-0001': {'state': 'REVIEW'}})
        self.assertEqual(invariants.check_i4(ctx), [])

    def test_r10_a_coder_on_a_branch_the_lane_holds_is_dropped(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001'),
                row('PLAN → CODE', 'T-0002', 'worker/T-0002')]
        ctx = feeder(rows, lanes={'worker/T-0001': {'state': 'GATE'},
                                  'worker/T-0002': {'state': 'MERGED'}})
        self.assertEqual(ids(invariants.check_i4(ctx)),
                         [('I4', 'PLAN → CODE T-0001 @worker/T-0001')])

    def test_r10_back_takes_a_correction_not_a_review(self):
        lanes = {'worker/T-0001': {'state': 'BACK'}}
        ok = [row('FIX → CORRECT', 'T-0001', 'worker/T-0001', brief='correct')]
        bad = [row('PUSHED → REVIEW', 'T-0001', 'worker/T-0001', brief='review')]
        self.assertEqual(invariants.check_i4(feeder(ok, lanes=lanes)), [])
        self.assertEqual(len(invariants.check_i4(feeder(bad, lanes=lanes))), 1)

    def test_r10_at_most_one_launching_row_per_branch(self):
        rows = [row('FIX → CORRECT', 'T-0001', 'worker/T-0001', brief='correct'),
                row('CONFLICT → REBASE', 'T-0001', 'worker/T-0001', brief='rebase'),
                row('PLAN → CODE', 'T-0001', 'worker/T-0001', launches=False)]
        found = invariants.check_i4(feeder(rows))
        self.assertEqual(ids(found), [('I4', 'CONFLICT → REBASE T-0001 @worker/T-0001')])

    def test_r10_occupancy_answers_for_a_branch_the_lane_has_no_record_of(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001'),
                row('PUSHED → REVIEW', 'T-0002', 'worker/T-0002', brief='review')]
        occ = {'busy': {'T-0001': 'live run', 'T-0002': 'pushed'}, 'waiting_landing': {},
               'corrections': {}}
        self.assertEqual(ids(invariants.check_i4(feeder(rows, occupancy=occ))),
                         [('I4', 'PLAN → CODE T-0001 @worker/T-0001')])


    def test_r10_a_correction_on_another_branch_does_not_hold_the_groom_branch(self):
        # 2026-09-25, a product tick: GROOM → ADJUDICATE F-0007 @groom/2026-09-25 was dropped every tick
        # because F-0007 had a land-spec correction pending on cloud/spec-brand-alignment — a
        # different branch. The occupancy fallback matched the item, not the branch.
        rows = [Row(tier=2, kind='GROOM → ADJUDICATE', item_id='F-0007', feature_id='F-0007',
                    action=LAUNCH, brief_kind='groom', branch='groom/2026-09-25',
                    reason='26 groom questions no rule answers, oldest F-0007',
                    groom_date='2026-09-25')]
        occ = {'busy': {}, 'waiting_landing': {},
               'corrections': {'F-0007': {'kind': 'land-spec', 'text': 'Land the spec',
                                          'rounds': 1, 'branch': 'cloud/spec-brand-alignment'}}}
        self.assertEqual(invariants.check_i4(feeder(rows, occupancy=occ)), [])

    def test_r10_a_correction_on_the_same_branch_still_holds_it(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001')]
        occ = {'busy': {}, 'waiting_landing': {},
               'corrections': {'T-0001': {'kind': 'fix', 'rounds': 0, 'branch': 'worker/T-0001'}}}
        self.assertEqual(ids(invariants.check_i4(feeder(rows, occupancy=occ))),
                         [('I4', 'PLAN → CODE T-0001 @worker/T-0001')])


class I5NoCodeWithoutTheSpecOnTheTrunk(unittest.TestCase):
    def test_i5_a_coder_row_whose_spec_is_on_a_branch_is_dropped(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001', feature='F-0001'),
                row('PLAN → CODE', 'T-0002', 'worker/T-0002', feature='F-0002')]
        docs = {'F-0001': {'spec': False, 'plan': True}, 'F-0002': {'spec': True, 'plan': None}}
        self.assertEqual(ids(invariants.check_i5(feeder(rows, docs_on_trunk=docs))),
                         [('I5', 'PLAN → CODE T-0001 @worker/T-0001')])

    def test_i5_a_features_own_correction_on_its_spec_branch_is_not_a_code_row(self):
        # 2026-09-25, a product tick: FIX → CORRECT F-0092/F-0097/F-0090 on their cloud/spec-* branches
        # were dropped every tick — "spec not on the trunk" — though the correction exists to
        # land that spec. The Feature's own row works its documents; only a Task's code needs them.
        rows = [Row(tier=2, kind='FIX → CORRECT', item_id=f, feature_id=f, action=LAUNCH,
                    brief_kind='correct', branch=b,
                    reason='harvest held it (land-spec), round 0: back to a session',
                    correction=f'The spec for {f} is approved but not on the trunk: land it.')
                for f, b in (('F-0092', 'cloud/spec-tinkerer-mode'),
                             ('F-0097', 'cloud/spec-voice-parity'),
                             ('F-0090', 'cloud/spec-team-staffing'))]
        rows.append(row('FIX → CORRECT', 'T-0009', 'worker/T-0009', brief='correct',
                        feature='F-0092'))
        docs = {f: {'spec': False, 'plan': False} for f in ('F-0092', 'F-0097', 'F-0090')}
        self.assertEqual(ids(invariants.check_i5(feeder(rows, docs_on_trunk=docs))),
                         [('I5', 'FIX → CORRECT T-0009 @worker/T-0009')])

    def test_i5_a_document_whose_place_is_unknown_is_not_judged(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001', feature='F-0001'),
                row('CARD → SPEC', 'F-0003', 'spec/F-0003', brief='spec', feature='F-0003')]
        docs = {'F-0001': {'spec': None, 'plan': None}, 'F-0003': {'spec': False}}
        self.assertEqual(invariants.check_i5(feeder(rows, docs_on_trunk=docs)), [])

    def test_i5_the_fact_reads_the_trunk_or_ingests_evidence(self):
        repo = tempfile.mkdtemp(prefix='inv_i5_')
        self.addCleanup(shutil.rmtree, repo, True)
        for args in (['init', '-q', '-b', 'main', repo],):
            subprocess.run(['git', *args], check=True, capture_output=True)
        os.makedirs(os.path.join(repo, 'specs'))
        with open(os.path.join(repo, 'specs', 'one.md'), 'w') as f:
            f.write('x\n')
        git = ['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(git + ['add', '-A'], check=True, capture_output=True)
        subprocess.run(git + ['commit', '-q', '-m', 'spec'], check=True, capture_output=True)
        subprocess.run(git + ['update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
        product = env.Product('sample', {'repo_dir': repo, 'main': 'main'})
        items = {'F-0001': {'links': {'spec': 'specs/one.md', 'plan': 'plans/one.md'}},
                 'F-0002': {'links': {'spec': 'specs/two.md'}, 'evidence': ['spec on origin/main']},
                 'F-0003': {'evidence': ['spec on spec/F-0003 (review r1 APPROVED)']}}
        facts = invariants.docs_on_trunk(product, items, ['F-0001', 'F-0002', 'F-0003'])
        self.assertEqual(facts['F-0001'], {'spec': True, 'plan': False})
        self.assertEqual(facts['F-0002'], {'spec': True, 'plan': None})
        self.assertEqual(facts['F-0003'], {'spec': False, 'plan': None})


class I7OneSessionPerBranchAndWorktree(unittest.TestCase):
    def test_i7_two_live_runs_in_one_worktree_case_folded(self):
        runs = [{'job': 'a', 'branch': 'worker/T-0001', 'worktree_key': '/x/.asf/wt/t1'},
                {'job': 'b', 'branch': 'worker/T-0002', 'worktree_key': '/x/.asf/wt/t1'}]
        found = invariants.check_i7(feeder([], runs=runs))
        self.assertEqual(ids(found), [('I7', 'worktree /x/.asf/wt/t1')])

    def test_i7_the_worktree_key_is_the_case_folded_path(self):
        from asf.workers import lifecycle
        tmp = tempfile.mkdtemp(prefix='inv_i7_')
        self.addCleanup(shutil.rmtree, tmp, True)
        runs = [{'job': 'a', 'worktree': tmp}, {'job': 'b', 'worktree': tmp.upper()}]
        found = invariants.check_i7(feeder([], runs=runs))
        folded = lifecycle.path_key(tmp) == lifecycle.path_key(tmp.upper())
        self.assertEqual(len(found), 1 if folded else 0)

    def test_i7_a_launch_onto_a_branch_a_live_run_holds_is_dropped(self):
        runs = [{'job': 'coder-t-0001', 'branch': 'worker/T-0001'}]
        rows = [row('FIX → CORRECT', 'T-0001', 'worker/T-0001', brief='correct'),
                row('PLAN → CODE', 'T-0002', 'worker/T-0002')]
        self.assertEqual(ids(invariants.check_i7(feeder(rows, runs=runs))),
                         [('I7', 'FIX → CORRECT T-0001 @worker/T-0001')])


class I8EveryLaneBranchInOneState(unittest.TestCase):
    NOW = 1_800_000_000.0

    def lane(self, **kw):
        return invariants.LaneContext(now=self.NOW, stale_after_s=2 * 86400, **kw)

    def test_i8_a_silent_old_wait_and_an_unknown_state_are_reported(self):
        lanes = {'worker/T-0001': {'state': 'WAITING', 'at': self.NOW - 3 * 86400},
                 'worker/T-0002': {'state': 'LIMBO', 'at': self.NOW},
                 'worker/T-0003': {'state': 'WAITING', 'at': self.NOW - 3 * 86400,
                                   'reason': 'trunk-red'},
                 'worker/T-0004': {'state': 'MERGED', 'at': self.NOW - 30 * 86400}}
        found = invariants.check_i8(self.lane(lanes=lanes, branches={'worker/T-0005'}))
        self.assertEqual(ids(found), [('I8', 'worker/T-0005'), ('I8', 'worker/T-0001'),
                                      ('I8', 'worker/T-0002')])

    def test_i8_every_branch_in_a_fresh_state_holds(self):
        lanes = {'worker/T-0001': {'state': 'GATE', 'at': '2027-01-15T08:00:00Z'}}
        ctx = invariants.LaneContext(lanes=lanes, branches={'worker/T-0001'},
                                     now=invariants._epoch('2027-01-15T09:00:00Z'),
                                     stale_after_s=86400)
        self.assertEqual(invariants.check_i8(ctx), [])


class R12I9IsAnEvent(unittest.TestCase):
    def test_r12_a_human_merge_is_an_event_not_a_violation(self):
        history = {'feature/T-0001': [{'state': 'PUSHED'}, {'state': 'PR_OPEN', 'pr': 7},
                                      {'state': 'MERGED', 'pr': 7, 'method': 'external'}],
                   'feature/T-0002': [{'state': 'PUSHED'}, {'state': 'MERGING', 'pr': 8},
                                      {'state': 'MERGED', 'pr': 8, 'method': 'external'}]}
        ctx = invariants.LaneContext(history=history,
                                     lanes={b: h[-1] for b, h in history.items()})
        self.assertEqual(invariants.run(ctx), [], 'I9 is never a violation')
        self.assertNotIn('I9', [i.id for i in invariants.INVARIANTS])
        events = invariants.i9_events(ctx)
        self.assertEqual([(e['branch'], e['pr']) for e in events], [('feature/T-0001', 7)],
                         'our own MERGING intent makes the later merge ours')

    def test_r12_the_lane_report_logs_the_event_and_returns_no_finding(self):
        state = tempfile.mkdtemp(prefix='inv_i9_')
        self.addCleanup(shutil.rmtree, state, True)
        path = os.path.join(state, 'sessions.jsonl')
        with open(path, 'w') as f:
            for line in ({'job': 'coder-t-0001', 'branch': 'feature/T-0001', 'started': 'x'},
                         {'job': 'coder-t-0001', 'lane': {'state': 'PR_OPEN', 'pr': 7}},
                         {'job': 'coder-t-0001',
                          'lane': {'state': 'MERGED', 'pr': 7, 'method': 'external'}}):
                f.write(json.dumps(line) + '\n')
        seen, lines = [], []
        with mock.patch.object(invariants, '_sessions_path', return_value=path), \
                mock.patch.object(invariants, 'lane_records',
                                  return_value={'feature/T-0001': {'state': 'MERGED'}}):
            findings, events = invariants.lane_report(
                env.Product('sample', {}), out=lines.append,
                event=lambda kind, **f: seen.append((kind, f)))
        self.assertEqual(findings, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(lines, ['EVENT I9: feature/T-0001 merged outside the lane (no MERGING intent)'])
        self.assertEqual(seen, [('foreign-merge', {'branch': 'feature/T-0001', 'pr': 7})])


class R13TestsOnlyInvariants(unittest.TestCase):
    def test_r13_i6_i12_i13_are_never_tick_checks(self):
        registered = {i.id for i in invariants.INVARIANTS}
        self.assertEqual(registered & {'I6', 'I9', 'I12', 'I13'}, set())
        self.assertEqual(set(invariants.TEST_ONLY), {'I6', 'I12', 'I13'})
        self.assertEqual({i.id for i in invariants.INVARIANTS},
                         {'I1', 'I2', 'I3', 'I4', 'I5', 'I7', 'I8', 'I10', 'I11'})

    def test_r13_i13_an_explicit_type_decides_the_minted_type(self):
        self.assertEqual(invariants.check_i13('bug', 'bug'), [])
        self.assertEqual(invariants.check_i13(None, 'feature'), [])
        self.assertEqual(ids(invariants.check_i13('bug', 'feature')), [('I13', 'feature')])

    def test_r13_i6_deep_only_rederives_a_copy_and_names_a_non_idempotent_field(self):
        root = tempfile.mkdtemp(prefix='inv_i6_')
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, 'features'))
        card = os.path.join(root, 'features', 'F-0001.md')
        with open(card, 'w') as f:
            f.write('---\nid: F-0001\ntype: feature\ntitle: t\n# ---- machine ----\n'
                    'state: New\n---\n## Description\n')
        before = _read(card)

        def derive(r, drift=False):
            path = os.path.join(r, 'features', 'F-0001.md')
            frontmatter.merge_machine(path, {'state': 'Active', 'stage': 'spec-draft'})
            if drift:  # a derivation that reads its own output: never a fixed point
                n = len(_read(os.path.join(r, 'n')) or '')
                with open(os.path.join(r, 'n'), 'a') as f:
                    f.write('x')
                frontmatter.merge_machine(path, {'evidence': [f'seen {n}']})

        self.assertEqual(invariants.check_i6(root, ingest=derive), [])
        found = invariants.check_i6(root, ingest=lambda r: derive(r, drift=True))
        self.assertEqual(ids(found), [('I6', 'F-0001')])
        self.assertEqual(_read(card), before, 'I6 never writes the record it checks')
        # the tick's check points never run it: --deep does
        with mock.patch.object(invariants, 'check_i6', return_value=[]) as i6:
            from asf.record import check
            check.invariant_findings(root, None, deep=False, out=lambda s: None)
            i6.assert_not_called()
            check.invariant_findings(root, None, deep=True, out=lambda s: None)
            i6.assert_called_once()


class R9SoftFailure(unittest.TestCase):
    def test_r9_a_check_that_raises_is_a_line_never_an_abort(self):
        def boom(ctx):
            raise RuntimeError('no facts')
        lines = []
        with mock.patch.object(invariants, 'INVARIANTS', [invariants.Invariant('I4', 'feeder', boom)]):
            self.assertEqual(invariants.run({}, out=lines.append), [])
        self.assertEqual(lines, ['INVARIANT I4: check failed (RuntimeError: no facts)'])

    def test_r9_feeder_drops_the_violating_row_and_logs_it(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001'),
                row('PLAN → CODE', 'T-0002', 'worker/T-0002'),
                row('PLAN → CODE', 'T-0003', 'worker/T-0003', launches=False)]
        ctx = feeder(rows, lanes={'worker/T-0001': {'state': 'PR_OPEN'}})
        lines = []
        with mock.patch.object(invariants, 'feeder_context', return_value=ctx):
            kept = invariants.feeder_gate(env.Product('sample', {}), rows, {}, out=lines.append)
        self.assertEqual([r.item_id for r in kept], ['T-0002', 'T-0003'])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('INVARIANT I4: PLAN → CODE T-0001 @worker/T-0001 — '),
                        lines)

    def test_r9_feeder_facts_that_fail_keep_every_row(self):
        rows = [row('PLAN → CODE', 'T-0001', 'worker/T-0001')]
        lines = []
        with mock.patch.object(invariants, 'feeder_context', side_effect=OSError('gone')):
            kept = invariants.feeder_gate(env.Product('sample', {}), rows, {}, out=lines.append)
        self.assertEqual(kept, rows)
        self.assertEqual(lines, ['INVARIANT feeder: not checked (OSError: gone)'])

    def test_r9_the_lane_facts_the_lane_cannot_give_yet_are_no_finding(self):
        lines = []
        findings, events = invariants.lane_report(env.Product('sample', {}), out=lines.append)
        self.assertEqual((findings, events, lines), ([], [], []))


def _card(root, iid, writes, state='Active'):
    os.makedirs(os.path.join(root, 'tasks'), exist_ok=True)
    rel = f'tasks/{iid}.md'
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write(f'---\nid: {iid}\ntype: task\ntitle: {iid}\nparent: F-0001\nwrites: [{writes}]\n'
                f'# ---- machine ----\nschema_version: 1\nstate: {state}\n---\n'
                '## Description\n\n## History\n- 2026-01-01: created\n')
    return rel


class R9RecordStepInTheTick(TickTestCase):
    """A writer of the record step writes one card that intersects an Active Task's ``writes:``
    (I3) and one that does not. The tick puts back only the first, commits the second, files
    one Bug for ``(I3, the path)`` — and a second tick files no second Bug that day."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(redact, 'default_patterns', return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(stage.drain)

        def step0(root, product, fresh=False):
            if not os.path.exists(os.path.join(root, 'tasks', 'T-0001.md')):
                _card(root, 'T-0001', 'src/a.py')

            def widen(r):
                _card(r, 'T-0002', 'src/a.py')     # intersects T-0001: refused
                _card(r, 'T-0003', 'src/c.py')     # sound: kept
            stage.guarded(root, 'widen', widen, product=product)
        patcher = mock.patch.object(tick, 'run_step0', step0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def origin_files(self, folder):
        out = _git(['ls-tree', '--name-only', 'main', f'{folder}/'], self.origin)
        return sorted(os.path.basename(p) for p in out.splitlines())

    def test_r9_record_reverts_only_the_offending_path_and_files_one_bug(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0, out)
        self.assertIn('INVARIANT I3: widen refused tasks/T-0002.md', out)
        self.assertEqual(self.origin_files('tasks'), ['T-0001.md', 'T-0003.md'])
        bugs = self.origin_files('bugs')
        self.assertEqual(len(bugs), 1, out)
        text = _git(['show', f'main:bugs/{bugs[0]}'], self.origin)
        self.assertIn('signature: "invariant I3: tasks/T-0002.md"', text)
        # the same refusal the same day bumps nothing and files nothing new
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.origin_files('bugs'), bugs)


if __name__ == '__main__':
    unittest.main()
