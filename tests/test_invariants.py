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
import re
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

    def test_r10_the_ceilings_adjudicate_on_a_review_branch_is_legal(self):
        # S-36504, plan F-0224 Task 3 (PD5): the code review ceiling's row launches on a branch
        # the lane still holds in REVIEW — legal, like the review session itself.
        rows = [row('STALEMATE → ADJUDICATE', 'T-0001', 'worker/T-0001', brief='adjudicate')]
        ctx = feeder(rows, lanes={'worker/T-0001': {'state': 'REVIEW'}})
        self.assertEqual(invariants.check_i4(ctx), [])

    def test_r10_the_ceilings_adjudicate_beside_a_review_row_is_a_second_launch_not_a_kind_finding(self):
        rows = [row('STALEMATE → ADJUDICATE', 'T-0001', 'worker/T-0001', brief='adjudicate'),
                row('PUSHED → REVIEW', 'T-0001', 'worker/T-0001', brief='review')]
        ctx = feeder(rows, lanes={'worker/T-0001': {'state': 'REVIEW'}})
        self.assertEqual(ids(invariants.check_i4(ctx)),
                         [('I4', 'PUSHED → REVIEW T-0001 @worker/T-0001')])

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


class R12AnI9EventIsSaidOncePerBranch(unittest.TestCase):
    def _sessions(self, lines):
        state = tempfile.mkdtemp(prefix='inv_i9_seen_')
        self.addCleanup(shutil.rmtree, state, True)
        path = os.path.join(state, 'sessions.jsonl')
        with open(path, 'w') as f:
            for line in lines:
                f.write(json.dumps(line) + '\n')
        return path

    @staticmethod
    def _foreign_merge(branch, pr):
        job = f'coder-{branch.replace("/", "-")}'
        return [{'job': job, 'branch': branch, 'started': 'x'},
                {'job': job, 'lane': {'state': 'PR_OPEN', 'pr': pr}},
                {'job': job, 'lane': {'state': 'MERGED', 'pr': pr, 'method': 'external'}}]

    def _report(self, path, lane_recs, out, event=None):
        with mock.patch.object(invariants, '_sessions_path', return_value=path), \
                mock.patch.object(invariants, 'lane_records', return_value=lane_recs):
            return invariants.lane_report(env.Product('sample', {}), out=out, event=event)

    def test_the_same_foreign_merge_is_said_once_across_two_reports(self):
        path = self._sessions(self._foreign_merge('feature/T-0001', 7))
        lane_recs = {'feature/T-0001': {'state': 'MERGED'}}
        lines, seen = [], []
        findings, events = self._report(path, lane_recs, lines.append,
                                         lambda kind, **f: seen.append((kind, f)))
        self.assertEqual(findings, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(lines, ['EVENT I9: feature/T-0001 merged outside the lane (no MERGING intent)'])
        self.assertEqual(seen, [('foreign-merge', {'branch': 'feature/T-0001', 'pr': 7})])

        lines, seen = [], []
        findings, events = self._report(path, lane_recs, lines.append,
                                         lambda kind, **f: seen.append((kind, f)))
        self.assertEqual((findings, events, lines, seen), ([], [], [], []))

    def test_the_ledger_names_the_branch_and_its_pr(self):
        path = self._sessions(self._foreign_merge('feature/T-0001', 7))
        lane_recs = {'feature/T-0001': {'state': 'MERGED'}}
        self._report(path, lane_recs, lambda s: None)
        with mock.patch.object(invariants, '_sessions_path', return_value=path):
            data = invariants.i9_seen(env.Product('sample', {}))
        self.assertIn('feature/T-0001', data)
        self.assertEqual(data['feature/T-0001']['pr'], 7)
        self.assertTrue(data['feature/T-0001']['at'])

    def test_a_branch_merged_outside_the_lane_later_is_still_said(self):
        path = self._sessions(self._foreign_merge('feature/T-0001', 7))
        lane_recs = {'feature/T-0001': {'state': 'MERGED'}, 'feature/T-0002': {'state': 'MERGED'}}
        self._report(path, lane_recs, lambda s: None)

        with open(path, 'a') as f:
            for line in self._foreign_merge('feature/T-0002', 9):
                f.write(json.dumps(line) + '\n')
        lines = []
        findings, events = self._report(path, lane_recs, lines.append)
        self.assertEqual(findings, [])
        self.assertEqual([(e['branch'], e['pr']) for e in events], [('feature/T-0002', 9)])
        self.assertEqual(lines, ['EVENT I9: feature/T-0002 merged outside the lane (no MERGING intent)'])

    def test_an_unreadable_ledger_says_it_again_rather_than_swallowing_it(self):
        path = self._sessions(self._foreign_merge('feature/T-0001', 7))
        ledger = os.path.join(os.path.dirname(path), invariants.I9_SEEN)
        with open(ledger, 'w') as f:
            f.write('not json')
        with mock.patch.object(invariants, '_sessions_path', return_value=path):
            self.assertEqual(invariants.i9_seen(env.Product('sample', {})), {})
        lines = []
        findings, events = self._report(path, {'feature/T-0001': {'state': 'MERGED'}}, lines.append)
        self.assertEqual(findings, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(lines, ['EVENT I9: feature/T-0001 merged outside the lane (no MERGING intent)'])

    def test_an_unwritable_ledger_is_not_an_error(self):
        path = self._sessions(self._foreign_merge('feature/T-0001', 7))
        ledger = os.path.join(os.path.dirname(path), invariants.I9_SEEN)
        os.makedirs(ledger)
        lines = []
        findings, events = self._report(path, {'feature/T-0001': {'state': 'MERGED'}}, lines.append)
        self.assertEqual(len(events), 1)
        self.assertIn('EVENT I9: feature/T-0001 merged outside the lane (no MERGING intent)', lines)
        self.assertFalse(any('not checked' in line for line in lines), lines)

    def test_the_fold_itself_stays_pure_and_the_audit_lists_everything(self):
        path = self._sessions(self._foreign_merge('feature/T-0001', 7))
        lane_recs = {'feature/T-0001': {'state': 'MERGED'}}
        with mock.patch.object(invariants, '_sessions_path', return_value=path), \
                mock.patch.object(invariants, 'lane_records', return_value=lane_recs):
            ctx = invariants.lane_context(env.Product('sample', {}))
            before = invariants.i9_events(ctx)
            self._report(path, lane_recs, lambda s: None)
            after = invariants.i9_events(ctx)
        self.assertEqual([(e['branch'], e['pr']) for e in before], [('feature/T-0001', 7)])
        self.assertEqual(before, after, 'i9_events is a pure fold: a report never changes its answer')

    def test_a_quiet_tick_derives_no_path_and_cannot_be_darkened(self):
        lines = []
        with mock.patch.object(invariants, '_i9_path', side_effect=OSError('gone')):
            findings, events = invariants.lane_report(env.Product('sample', {}), out=lines.append)
        self.assertEqual((findings, events), ([], []))
        self.assertFalse(any('not checked' in line for line in lines), lines)


class R13TestsOnlyInvariants(unittest.TestCase):
    def test_r13_i6_i12_i13_are_never_tick_checks(self):
        registered = {i.id for i in invariants.INVARIANTS}
        self.assertEqual(registered & {'I6', 'I9', 'I12', 'I13'}, set())
        self.assertEqual(set(invariants.TEST_ONLY), {'I6', 'I12', 'I13'})
        self.assertEqual({i.id for i in invariants.INVARIANTS},
                         {'I1', 'I2', 'I3', 'I4', 'I5', 'I7', 'I8', 'I10', 'I11', 'I14'})

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


def _card(root, iid, writes, state='Active', after=(), removed=None):
    os.makedirs(os.path.join(root, 'tasks'), exist_ok=True)
    rel = f'tasks/{iid}.md'
    typed = [f'id: {iid}', 'type: task', f'title: {iid}', 'parent: F-0001', f'writes: [{writes}]']
    if after:
        typed.append(f"after: [{', '.join(after)}]")
    if removed:
        typed.append(f'removed: {removed}')
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(typed) +
                f'\n# ---- machine ----\nschema_version: 1\nstate: {state}\n---\n'
                '## Description\n\n## History\n- 2026-01-01: created\n')
    return rel


class R9RecordStepInTheTick(TickTestCase):
    """A writer of the record step writes one card that intersects an Active Task's ``writes:``
    (I3) and one that does not. The tick puts back only the first, commits the second, files
    one Bug for ``(I3, the cause)`` — and a second tick files no second Bug that day."""

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
        # one Bug per (invariant, cause): the cause has its ids taken out, the path is evidence
        self.assertIn('signature: "invariant I3: writes: intersects Active task …\'s writes:"', text)
        self.assertIn('- tasks/T-0002.md — T-0002:', text)
        # the same refusal the same day bumps nothing and files nothing new
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.origin_files('bugs'), bugs)


class OrderedOverlapTests(unittest.TestCase):
    """The spec's acceptance 1: an overlap violates I3 only when the record has not ordered the
    pair (C1), and a writer that un-orders a live overlap is refused at the card it wrote (C3)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='ordered_overlap_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def check(self, paths, before=None):
        staged = stage.Staged('test', tuple(paths), before or {p: None for p in paths})
        return invariants.check_i3(stage.RecordContext(self.root, staged))

    def subjects(self, findings):
        return sorted(f.subject for f in findings)

    def test_a_pair_with_no_order_is_two_findings(self):
        p1 = _card(self.root, 'T-0001', 'lib/x.py')
        p2 = _card(self.root, 'T-0002', 'lib/x.py')
        self.assertEqual(self.subjects(self.check([p1, p2])), ['T-0001', 'T-0002'])

    def test_the_same_pair_ordered_either_direction_is_none(self):
        p1 = _card(self.root, 'T-0001', 'lib/x.py')
        p2 = _card(self.root, 'T-0002', 'lib/x.py', after=('T-0001',))
        self.assertEqual(self.check([p1, p2]), [])

        p1 = _card(self.root, 'T-0001', 'lib/x.py', after=('T-0002',))
        p2 = _card(self.root, 'T-0002', 'lib/x.py')
        self.assertEqual(self.check([p1, p2]), [])

    def test_a_chain_through_a_closed_task_is_ordered(self):
        _card(self.root, 'T-0001', 'lib/x.py')
        _card(self.root, 'T-0002', '', state='Closed', after=('T-0001',))
        p3 = _card(self.root, 'T-0003', 'lib/x.py', after=('T-0002',))
        self.assertEqual(self.check([p3]), [])

    def test_a_chain_through_a_removed_task_is_ordered(self):
        # PD4's live shape: T-0058 after [T-0057 (removed)], T-0057 after [T-0056]
        _card(self.root, 'T-0001', 'lib/x.py')
        _card(self.root, 'T-0002', '', after=('T-0001',), removed='merged into T-0001')
        p3 = _card(self.root, 'T-0003', 'lib/x.py', after=('T-0002',))
        self.assertEqual(self.check([p3]), [])

    def test_a_writer_that_removes_the_order_is_refused_at_the_card_it_wrote(self):
        _card(self.root, 'T-0001', 'lib/x.py')
        p2 = _card(self.root, 'T-0002', 'lib/x.py', after=('T-0001',))
        before_text = _read(os.path.join(self.root, p2))
        _card(self.root, 'T-0002', 'lib/x.py')  # the after: id is gone, writes: unchanged
        self.assertEqual(self.subjects(self.check([p2], before={p2: before_text})), ['T-0002'])

    def test_a_writer_that_only_adds_after_is_not_refused(self):
        _card(self.root, 'T-0001', 'lib/x.py')
        p2 = _card(self.root, 'T-0002', 'lib/x.py')
        before_text = _read(os.path.join(self.root, p2))
        _card(self.root, 'T-0002', 'lib/x.py', after=('T-0001',))
        self.assertEqual(self.check([p2], before={p2: before_text}), [])


def _task(writes, evidence=()):
    return {'type': 'task', 'state': 'Active', 'writes': writes, 'after': (), 'removed': None,
            'evidence': evidence}


class OverlapYieldsToProgressTests(unittest.TestCase):
    """B-0037: the hold an unordered ``writes:`` overlap gets never falls on the Task that
    already has a pushed branch or an open PR — the one the record shows no sign of starting
    yields, whichever id was minted first or last."""

    def test_the_lower_id_with_no_progress_yields_to_the_higher_id_with_an_open_pr(self):
        # T-0001 is lexicographically first — the old rule always held it on T-0002. Here
        # T-0002 is the one with a pushed branch and an open PR: it must be the owner.
        tasks = {'T-0001': _task(['lib/x.py']),
                 'T-0002': _task(['lib/x.py'], evidence=['branch worker/T-0002, PR #9 OPEN'])}
        pairs = invariants.unordered_overlaps(tasks)
        self.assertEqual(pairs, [('T-0002', 'T-0001', 'lib/x.py', 'lib/x.py')])

    def test_a_branch_with_no_pr_yet_still_outranks_an_unstarted_sibling(self):
        tasks = {'T-0001': _task(['lib/x.py']),
                 'T-0002': _task(['lib/x.py'], evidence=['branch worker/T-0002 exists'])}
        owner, held, *_ = invariants.unordered_overlaps(tasks)[0]
        self.assertEqual((owner, held), ('T-0002', 'T-0001'))

    def test_neither_side_has_progress_the_lower_id_stays_owner(self):
        tasks = {'T-0001': _task(['lib/x.py']), 'T-0002': _task(['lib/x.py'])}
        owner, held, *_ = invariants.unordered_overlaps(tasks)[0]
        self.assertEqual((owner, held), ('T-0001', 'T-0002'))

    def test_both_sides_have_progress_the_lower_id_stays_owner(self):
        tasks = {'T-0001': _task(['lib/x.py'], evidence=['PR #1 OPEN']),
                 'T-0002': _task(['lib/x.py'], evidence=['PR #2 OPEN'])}
        owner, held, *_ = invariants.unordered_overlaps(tasks)[0]
        self.assertEqual((owner, held), ('T-0001', 'T-0002'))

    def test_has_pushed_work_reads_the_card_s_own_evidence_only(self):
        self.assertFalse(invariants.has_pushed_work(_task(['x'])))
        self.assertFalse(invariants.has_pushed_work(_task(['x'], evidence=['in plan p (1), no branch yet'])))
        self.assertTrue(invariants.has_pushed_work(_task(['x'], evidence=['branch worker/T-1 exists'])))
        self.assertTrue(invariants.has_pushed_work(_task(['x'], evidence=['PR #3 OPEN'])))


def _closing(root, rel, iid, state, *, typ='task', parent='F-0001', landing=None, reopened=(),
             created='2026-09-01'):
    """Write card ``rel`` (``iid``, ``state``) with an optional ``landing:`` stamp."""
    os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
    machine = [f'schema_version: 1', f'state: {state}']
    if landing is not None:
        machine.append('landing:')
        machine += [f'  {k}: {v}' for k, v in landing.items()]
    if reopened:
        machine.append('reopened:')
        machine += [f"  - '{r}'" for r in reopened]
    typed = [f'id: {iid}', f'type: {typ}', f'title: {iid}', f'created: {created}']
    if parent:
        typed.append(f'parent: {parent}')
    text = ('---\n' + '\n'.join(typed) + '\n# ---- machine ----\n' + '\n'.join(machine) +
            '\n---\n## Description\n\n## History\n- 2026-01-01: created\n')
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write(text)
    return text


class _I14(unittest.TestCase):
    """A record clone, a product repository whose ``origin/main`` holds ``self.sha`` (and not
    ``self.off_trunk``), a temporary ASF home, and ``print`` captured in ``self.lines``."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='i14_')
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.root = os.path.join(self.d, 'record')
        self.repo = os.path.join(self.d, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q')
        self.commit('task(T-0001): the work')
        self.sha = self.git('rev-parse', 'HEAD')
        self.commit('task(T-0002): not on the trunk yet')
        self.off_trunk = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', self.sha)
        p = mock.patch.object(env, 'ASF_HOME', os.path.join(self.d, 'home'))
        p.start()
        self.addCleanup(p.stop)
        self.lines = []
        p = mock.patch('builtins.print', lambda *a, **_k: self.lines.append(' '.join(map(str, a))))
        p.start()
        self.addCleanup(p.stop)

    def git(self, *a):
        return subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, msg):
        self.git('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
                 '-m', msg)

    def product(self, mode):
        flags = {} if mode is None else {'i14': mode}
        return env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                 'conventions': {'flags': flags}})

    def stamp(self, sha=None, by='pr-merge', as_of='2026-10-01T00:00:00Z'):
        return {'sha': sha or self.sha, 'as_of': as_of, 'by': by}

    def close(self, mode, landing=None, before_state='Active', typ='task', rel='tasks/T-0001.md',
              iid='T-0001', writer='ingest', **kw):
        """Stage one writer that moved ``rel`` from ``before_state`` to Closed; the findings."""
        before = _closing(self.root, rel, iid, before_state, typ=typ, **kw)
        _closing(self.root, rel, iid, 'Closed', typ=typ, landing=landing, **kw)
        staged = stage.Staged(writer, (rel,), {rel: before})
        return invariants.check_i14(stage.RecordContext(self.root, staged, self.product(mode)))

    def reported(self):
        return invariants.i14_report_lines(self.product('report'))


class I14CloseNeedsALandingFact(_I14):
    """I14 (W4-PR3b): a Task/Bug/Story closes only on a ``landing:`` stamp naming a full sha on
    ``origin/<main>``; ``flags.i14`` off by default, ``report`` writes a line and a jsonl record,
    ``refuse`` returns the finding with the parents the writer cascaded."""

    def test_off_by_default_judges_nothing(self):
        self.assertEqual(invariants.i14_mode(self.product(None)), 'off')
        self.assertEqual(invariants.i14_mode(self.product(False)), 'off')  # yaml `off`
        self.assertEqual(self.close(None), [])
        self.assertEqual(self.close('off'), [])
        self.assertEqual(self.lines, [])
        self.assertEqual(self.reported(), 0)

    def test_report_mode_prints_and_records_and_the_write_stands(self):
        self.assertEqual(self.close('report'), [])
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertTrue(self.lines[0].startswith('INVARIANT I14 (report): ingest closed '
                                                 'tasks/T-0001.md — closed with no landing'))
        self.assertEqual(self.reported(), 1)
        with open(os.path.join(env.ASF_HOME, 'state', 'p', 'invariants-report.jsonl')) as f:
            rec = json.loads(f.readline())
        self.assertEqual((rec['invariant'], rec['item'], rec['writer']), ('I14', 'T-0001', 'ingest'))

    def test_refuse_mode_returns_the_finding(self):
        found = self.close('refuse')
        self.assertEqual(ids(found), [('I14', 'T-0001')])
        self.assertEqual(found[0].paths, ('tasks/T-0001.md',))
        self.assertEqual(self.reported(), 0)

    def test_a_stamped_close_on_the_trunk_holds(self):
        for by in ('pr-merge', 'names', 'trunkclose/names', 'trunkclose/pr', 'console', 'groom',
                   'children', 'matrix', 'descent'):
            self.assertEqual(self.close('refuse', self.stamp(by=by)), [], by)
        self.assertEqual(self.close('refuse', {'sha': "''", 'as_of': '2026-10-01', 'by': 'migration',
                                               'note': 'pre-I14'}), [])

    def test_a_bad_stamp_is_a_finding(self):
        for stamp, said in ((self.stamp(by='covers'), 'landing.by covers'),
                            (self.stamp(sha=self.sha[:9]), 'not a full sha'),
                            (self.stamp(as_of='never'), 'as_of'),
                            (self.stamp(sha=self.off_trunk), 'is not on origin/main'),
                            ({'sha': "''", 'as_of': '2026-10-01T00:00:00Z', 'by': 'reverted',
                              'reverts': self.sha}, 'was reverted'),
                            ({'sha': "''", 'as_of': '2026-10-01T00:00:00Z', 'by': 'children'},
                             'not a full sha')):
            found = self.close('refuse', stamp)
            self.assertEqual(len(found), 1, stamp)
            self.assertIn(said, found[0].message)

    def test_an_unfetched_sha_defers_silently(self):
        # no origin to fetch from: the object stays unknown — no finding, no line, no Bug
        found = self.close('refuse', self.stamp(sha='f' * 40))
        self.assertEqual(found, [])
        self.assertEqual(self.close('report', self.stamp(sha='f' * 40)), [])
        self.assertEqual(self.reported(), 0)

    def test_a_stamp_older_than_the_last_reopen_is_a_finding(self):
        found = self.close('refuse', self.stamp(as_of='2026-09-20T00:00:00Z'),
                           reopened=('2026-09-25T10:00:00Z: wrong close — Closed → New',))
        self.assertIn('predates the last reopen', found[0].message)
        self.assertEqual(self.close('refuse', self.stamp(as_of='2026-09-26T00:00:00Z'),
                                    reopened=('2026-09-25T10:00:00Z: x — Closed → New',)), [])

    def test_a_voided_sha_is_a_finding(self):
        from asf.workers import lifecycle
        void = {'head': self.sha, 'by': 'operator', 'at': '2026-10-01T00:00:00Z'}
        with mock.patch.object(lifecycle, 'voided_sha', return_value=void):
            found = self.close('refuse', self.stamp())
        self.assertIn('voided by operator', found[0].message)

    def test_only_the_close_transition_of_a_task_bug_or_story_is_judged(self):
        self.assertEqual(self.close('refuse', before_state='Closed'), [])     # already closed
        self.assertEqual(self.close('refuse', typ='feature', rel='features/F-0009.md',
                                    iid='F-0009', parent=''), [])
        self.assertEqual(len(self.close('refuse', typ='bug', rel='bugs/B-0001.md',
                                        iid='B-0001')), 1)
        self.assertEqual(len(self.close('refuse', typ='story', rel='stories/S-0001.md',
                                        iid='S-0001')), 1)

    def test_refuse_reverts_the_parents_the_writer_cascaded(self):
        task, feat, other = 'tasks/T-0001.md', 'features/F-0001.md', 'tasks/T-0003.md'
        tb = _closing(self.root, task, 'T-0001', 'Active')
        fb = _closing(self.root, feat, 'F-0001', 'Active', typ='feature', parent='')
        ob = _closing(self.root, other, 'T-0003', 'Active')
        _closing(self.root, task, 'T-0001', 'Closed')                        # no stamp
        _closing(self.root, feat, 'F-0001', 'Resolved', typ='feature', parent='')
        _closing(self.root, other, 'T-0003', 'Active', parent='F-0001')     # an unrelated edit
        staged = stage.Staged('ingest', (feat, task, other), {task: tb, feat: fb, other: ob})
        found = invariants.check_i14(stage.RecordContext(self.root, staged, self.product('refuse')))
        self.assertEqual(sorted(found[0].paths), [feat, task])
        restored = stage.refuse(self.root, staged, found)
        self.assertEqual(sorted(restored), [feat, task])
        self.assertEqual(_read(os.path.join(self.root, feat)), fb)
        self.assertEqual(_read(os.path.join(self.root, task)), tb)

    def test_the_audit_lists_in_report_mode_and_writes_nothing(self):
        _closing(self.root, 'tasks/T-0001.md', 'T-0001', 'Closed')
        _closing(self.root, 'tasks/T-0002.md', 'T-0002', 'Closed', landing=self.stamp())
        found = [f for f in invariants.record_audit(self.root, self.product('report'))
                 if f.invariant == 'I14']
        self.assertEqual(ids(found), [('I14', 'T-0001')])
        self.assertEqual(self.reported(), 0)
        self.assertEqual([ln for ln in self.lines if 'I14' in ln], [])

    def test_a_close_the_ingest_stamps_holds_and_an_unstamped_one_is_refused(self):
        # the closing writer itself (asf ingest, W4-PR3a's stamp) staged and judged end to end
        import types
        from asf.record import ingest
        for folder in ('epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules'):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
        _closing(self.root, 'tasks/T-0001.md', 'T-0001', 'New', parent='')
        ev = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
              'checked': set(), 'main_sha': None, 'merged': {}, 'branches': [], 'ci': True,
              'ids': {'T-0001': {'branches': [], 'open_prs': [], 'commit': self.sha,
                                 'pr': None, 'green': True}}}

        def run_ingest(root):
            with mock.patch.object(ingest.evidence, 'load', return_value=ev):
                return ingest.cmd_ingest(types.SimpleNamespace(fresh=False, registry=False),
                                         root)
        staged = stage.stage(self.root, 'ingest', run_ingest)
        self.assertIn('tasks/T-0001.md', staged.paths)
        ctx = stage.RecordContext(self.root, staged, self.product('refuse'))
        self.assertEqual([f for f in invariants.check_i14(ctx)], [])
        # the same close with the stamp taken off is a finding
        rel = os.path.join(self.root, 'tasks/T-0001.md')
        text = _read(rel)
        with open(rel, 'w', encoding='utf-8') as f:
            f.write(re.sub(r'landing:.*\n(  .*\n)*', '', text))
        ctx = stage.RecordContext(self.root, staged, self.product('refuse'))
        self.assertEqual(ids(invariants.check_i14(ctx)), [('I14', 'T-0001')])

    def test_the_report_count_shows_in_status_and_the_scorecard(self):
        from asf.scorecard import program
        from asf.views import status
        self.assertIsNone(status.i14_cell(self.product('off')))
        self.close('report')
        self.assertEqual(status.i14_cell(self.product('report')),
                         '1 report line(s) in 24 h (i14: report)')
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        row = program.row([], now - datetime.timedelta(days=1), now + datetime.timedelta(seconds=1),
                          product=self.product('report'))
        self.assertEqual(row['i14_report_lines'], 1)
        self.assertIsNone(program.row([], now, now)['i14_report_lines'])


class I14CoversRule(_I14):
    """S-M16: a ``trunkclose/covers`` close holds only with a ``done`` REPORT, no open work with
    gh known, and the covering commit newer than the card."""

    def setUp(self):
        super().setUp()
        from asf.workers import landing, lifecycle, relaunch
        self.run = {'job': 'coder-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001',
                    'harvested': self.sha, 'trunk_closed': 'verified', 'started': '2026-10-01'}
        self.text = 'status: done'
        self.work = ''
        for target, name, fn in (
                (lifecycle, 'item_runs', lambda _p, _i: [self.run]),
                (relaunch, '_result_text', lambda _r: self.text),
                (relaunch, 'terminal', lambda t: 'status done' if 'done' in t else 'status partial'),
                (landing, 'open_work', lambda *_a, **_k: self.work)):
            p = mock.patch.object(target, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def covers(self, created='2020-01-01'):
        return self.close('refuse', self.stamp(by='trunkclose/covers'), created=created)

    def test_covers_holds_with_its_three_facts(self):
        self.assertEqual(self.covers(), [])

    def test_covers_without_a_done_report_is_a_finding(self):
        self.text = 'status: partial'
        self.assertIn('did not report done', self.covers()[0].message)

    def test_covers_with_open_work_is_a_finding_and_unknown_defers(self):
        self.work = 'worker/T-0001'
        self.assertIn('still open on worker/T-0001', self.covers()[0].message)
        self.work = None
        self.assertEqual(self.covers(), [])

    def test_covers_by_a_commit_older_than_the_card_is_a_finding(self):
        self.assertIn('is not newer than the card', self.covers(created='2999-01-01')[0].message)

    def test_covers_with_no_trunk_closed_run_is_a_finding(self):
        self.run = dict(self.run, trunk_closed=None)
        self.assertIn('no trunk-closed run', self.covers()[0].message)


if __name__ == '__main__':
    unittest.main()
