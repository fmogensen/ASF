"""asf.workers.lifecycle — the lane's state machine, pinned by invariant tests over *generated*
registries and evidence (F-0087): the same instance of a Bug cannot recur, and the next one of
its class cannot either.

Classes and the Bugs behind them:

* registry semantics — B-0028, B-0041, B-0051: every launch line opens a run; a run's terminal
  fields never fold into the next; ``finished`` means pushed;
* branch lifecycle — B-0025, B-0046, B-0048, B-0049: launched → pushed → held(n) → corrected →
  landed → reaped, every transition reachable, none terminal but reaped, a launch refused only
  on a live worktree, reap on landed or empty.
"""
import ast
import glob
import itertools
import json
import os
import random
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import env, gitpush, redact
from asf.scorecard import score
from asf.workers import lifecycle as lc
from asf.workers import observe

OK = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'}
ERR = {'type': 'result', 'subtype': 'error', 'is_error': True, 'result': 'boom'}

# every field of a run that a later line may write, and the values it may take
UPDATE_FIELDS = {
    'ended': ['2026-01-01T00:00:00Z'],
    'end_reason': ['finished', 'failed', 'dead pid', 'failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)'],
    'rc': [0, 1],
    'corrected': [1],
    'operator_flagged': [1],
    'harvested': ['abc123'],
    'correction': [{'kind': 'gate', 'text': 'FAIL: t', 'at': '2026-01-01T00:01:00Z'}],
    'rounds': [1, 2, 3],
    'dead_class': ['gone', 'reused', 'unknown'],
}


def generate(rng, jobs=3, lines=40):
    """A registry: launch lines and update lines for a few jobs, interleaved at random. Returns
    the lines and, per job, the list of ``(launch, [updates])`` the generator meant."""
    out, meant = [], {f'job-{i}': [] for i in range(jobs)}
    t = 0
    for _ in range(lines):
        job = rng.choice(sorted(meant))
        if not meant[job] or rng.random() < 0.3:
            t += 1
            launch = {'job': job, 'item': f'B-{int(job[-1]) + 1:04d}', 'branch': f'fix/{job}',
                      'pid': 1000 + t, 'started': f'2026-01-01T00:{t:02d}:00Z',
                      'worktree': f'/wt/{job}'}
            meant[job].append((launch, []))
            out.append(launch)
        else:
            field = rng.choice(sorted(UPDATE_FIELDS))
            upd = {'job': job, field: rng.choice(UPDATE_FIELDS[field])}
            meant[job][-1][1].append(upd)
            out.append(upd)
    return out, meant


class RegistryFoldInvariants(unittest.TestCase):
    """Property tests over generated registries."""

    def test_every_launch_line_opens_a_run_and_updates_land_on_the_latest(self):
        for seed in range(200):
            rng = random.Random(seed)
            lines, meant = generate(rng)
            runs = lc.fold(lines)
            for job, expected in meant.items():
                with self.subTest(seed=seed, job=job):
                    self.assertEqual(len(runs[job]), len(expected))
                    for run, (launch, updates) in zip(runs[job], expected):
                        want = dict(launch)
                        for u in updates:
                            want.update(u)
                        want = {k: v for k, v in want.items()
                                if not (k in lc.RUN_FIELDS and v is None)}
                        self.assertEqual(run, want)

    def test_a_runs_terminal_fields_never_fold_into_the_next_run(self):
        # B-0041 — a relaunch line must start clean whatever the launcher wrote or forgot
        for seed in range(200):
            rng = random.Random(seed)
            lines, meant = generate(rng)
            runs = lc.fold(lines)
            for job, expected in meant.items():
                for i, (run, (launch, updates)) in enumerate(zip(runs[job], expected)):
                    written = {k for u in updates for k in u if k != 'job'}
                    leaked = {k for k in lc.RUN_FIELDS if k in run and k not in written}
                    self.assertEqual(leaked, set(), (seed, job, i, run))

    def test_the_relaunch_line_needs_no_nulls_to_start_clean(self):
        lines = [{'job': 'j', 'pid': 1, 'started': 't1', 'branch': 'b'},
                 {'job': 'j', 'ended': 't2', 'end_reason': 'failed', 'rc': 1, 'corrected': 1,
                  'operator_flagged': 1, 'harvested': 'sha'},
                 {'job': 'j', 'pid': 2, 'started': 't3', 'branch': 'b'}]
        run = lc.fold(lines)['j'][-1]
        self.assertEqual(run, {'job': 'j', 'pid': 2, 'started': 't3', 'branch': 'b'})
        self.assertTrue(lc.is_live(run))

    def test_a_null_run_field_is_dropped_and_a_hand_line_before_any_launch_opens_a_run(self):
        lines = [{'job': 'j', 'harvested': 'sha', 'correction': None}]
        self.assertEqual(lc.fold(lines), {'j': [{'job': 'j', 'harvested': 'sha'}]})

    def test_an_update_naming_its_branch_lands_on_that_branchs_run_of_the_job(self):
        """A product's F-0011: one job name launched on the spec branch, then on the plan
        branch. An update line naming the spec branch is the spec run's, not the job's latest
        (the plan run): read as the latest, the two branches' lane records swapped every tick."""
        lines = [{'job': 'j', 'pid': 1, 'started': 't1', 'branch': 'spec/F'},
                 {'job': 'j', 'pid': 2, 'started': 't2', 'branch': 'plan/F'},
                 {'job': 'j', 'branch': 'spec/F', 'lane': {'state': 'REVIEW', 'head': 's'}},
                 {'job': 'j', 'branch': 'plan/F', 'lane': {'state': 'GATE', 'head': 'p'}},
                 {'job': 'j', 'harvested': 'x'},             # no branch: the latest, as before
                 {'job': 'j', 'branch': 'gone/F', 'note': 'n'}]  # no run on it: the latest
        spec, plan = lc.fold(lines)['j']
        self.assertEqual(spec['lane'], {'state': 'REVIEW', 'head': 's'})
        self.assertEqual(plan['lane'], {'state': 'GATE', 'head': 'p'})
        self.assertEqual((spec.get('harvested'), plan.get('harvested')), (None, 'x'))
        self.assertEqual((spec['branch'], plan['branch']), ('spec/F', 'gone/F'))

    def test_by_branch_is_the_last_launch_naming_the_branch(self):
        lines = [{'job': 'fix-b-0001', 'pid': 1, 'started': 't1', 'branch': 'fix/B-0001', 'item': 'B-0001'},
                 {'job': 'fix-b-0001', 'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'fix-b-0001', 'rounds': 1, 'correction': {'text': 'FAIL', 'at': 't3'}},
                 {'job': 'correct-b-0001', 'pid': 2, 'started': 't4', 'branch': 'fix/B-0001', 'item': 'B-0001'}]
        path = self._write(lines)
        run = lc.by_branch(path)['fix/B-0001']
        self.assertEqual(run['job'], 'correct-b-0001')
        self.assertNotIn('correction', run)
        self.assertEqual(lc.rounds_of(path, 'B-0001'), 1)
        # the correction is answered: a run on the item started after it
        self.assertEqual(lc.corrections(path), {})

    def test_by_branch_is_the_latest_launch_when_a_job_name_comes_back(self):
        """B-0148, a product's T-0360/T-0097: correct-t-0360 ran, then adjudicate-t-0360, then
        correct-t-0360 again ×30. The fold keys runs by job, so the adjudicate run — its job
        first seen later — stayed the owner: the lane wrote every hold on it, the loop guard
        counted adjudicate launches (none) and the finding never climbed (an adjudicate run
        rules, it does not answer). The owner is the latest launch, whatever its job name."""
        lines = [{'job': 'correct-t-1', 'pid': 1, 'started': 't1', 'branch': 'cloud/T-1', 'kind': 'correct'},
                 {'job': 'correct-t-1', 'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'adjudicate-t-1', 'pid': 2, 'started': 't3', 'branch': 'cloud/T-1', 'kind': 'adjudicate'},
                 {'job': 'adjudicate-t-1', 'ended': 't4', 'end_reason': 'finished'},
                 {'job': 'correct-t-1', 'pid': 3, 'started': 't5', 'branch': 'cloud/T-1', 'kind': 'correct'}]
        run = lc.by_branch(self._write(lines))['cloud/T-1']
        self.assertEqual((run['job'], run['pid']), ('correct-t-1', 3))

    def test_the_loop_guard_parks_correct_launches_after_an_adjudicate_ruling(self):
        """B-0148: three correct sessions on one head after an adjudicate run park the item on
        the lane's next review hold — the hold lands on the branch's latest run."""
        lines = [{'job': 'correct-t-1', 'pid': 1, 'started': 't1', 'branch': 'cloud/T-1',
                  'item': 'T-1', 'kind': 'correct', 'launch_head': 'a' * 40},
                 {'job': 'adjudicate-t-1', 'pid': 2, 'started': 't2', 'branch': 'cloud/T-1',
                  'item': 'T-1', 'kind': 'adjudicate', 'launch_head': 'a' * 40}]
        for n in (3, 4, 5):
            lines += [{'job': 'correct-t-1', 'pid': n, 'started': f't{n}', 'branch': 'cloud/T-1',
                       'item': 'T-1', 'kind': 'correct', 'launch_head': 'b' * 40},
                      {'job': 'correct-t-1', 'ended': f'e{n}', 'end_reason': 'finished'}]
        path = self._write(lines)
        run = lc.by_branch(path)['cloud/T-1']
        fields, line = lc.hold(path, run, lc.REVIEW, 'r.md reads changes requested', 'now',
                               head='b' * 40)
        self.assertIs(fields['correction'].get('parked'), True, line)

    def test_a_review_answered_without_a_commit(self):
        """B-0149: the review hold's correction, then a correct run launched on the head the
        hold named that finished there — nothing changed, the session says it is resolved."""
        H = 'd' * 40
        hold = {'job': 'adjudicate-t-1', 'correction': {
            'kind': 'review', 'at': '2026-01-01T07:17:39Z',
            'text': 'rv/4-t-1.md reads changes requested: answer its C list on cloud/T-1'}}
        lines = [{'job': 'adjudicate-t-1', 'pid': 1, 'started': '2026-01-01T07:00:00Z',
                  'branch': 'cloud/T-1', 'item': 'T-1', 'kind': 'adjudicate'},
                 {'job': 'adjudicate-t-1', 'ended': 't', 'end_reason': 'finished'}, hold]
        path = self._write(lines)
        self.assertIsNone(lc.review_answered(path, 'T-1', 'rv/4-t-1.md', H))
        run = {'job': 'correct-t-1', 'pid': 2, 'started': '2026-01-01T07:18:39Z',
               'branch': 'cloud/T-1', 'item': 'T-1', 'kind': 'correct', 'launch_head': H}
        path = self._write(lines + [run])
        self.assertIsNone(lc.review_answered(path, 'T-1', 'rv/4-t-1.md', H))  # still running
        done = {'job': 'correct-t-1', 'ended': 't2', 'end_reason': 'finished'}
        path = self._write(lines + [run, done])
        self.assertEqual(lc.review_answered(path, 'T-1', 'rv/4-t-1.md', H), 'correct-t-1')
        self.assertIsNone(lc.review_answered(path, 'T-1', 'rv/4-t-1.md', 'e' * 40))  # moved
        self.assertIsNone(lc.review_answered(path, 'T-1', 'rv/5-t-1.md', H))  # another review
        failed = dict(done, end_reason='failed: rc 1')
        path = self._write(lines + [run, failed])
        self.assertIsNone(lc.review_answered(path, 'T-1', 'rv/4-t-1.md', H))

    def test_corrected_on_a_head_is_a_correct_run_that_ended_there(self):
        """T5n: a correct run launched on the head, ended — with no commit, as one with nothing
        to correct ends — has looked at the branch as it stands."""
        H = 'd' * 40
        run = {'job': 'correct-t-1', 'pid': 2, 'started': '2026-01-01T07:18:39Z',
               'branch': 'cloud/T-1', 'item': 'T-1', 'kind': 'correct', 'launch_head': H}
        self.assertIsNone(lc.corrected_on(self._write([run]), 'T-1', H))  # still running
        path = self._write([run, {'job': 'correct-t-1', 'ended': 't2', 'end_reason': 'failed'}])
        self.assertEqual(lc.corrected_on(path, 'T-1', H), 'correct-t-1')
        self.assertIsNone(lc.corrected_on(path, 'T-1', 'e' * 40))  # another head
        self.assertIsNone(lc.corrected_on(path, 'T-2', H))

    def test_by_worktree_is_the_last_run_that_recorded_the_directory(self):
        lines = [{'job': 'fix-b-0001', 'pid': 1, 'started': 't1', 'worktree': '/wt/fix-b-0001'},
                 {'job': 'fix-b-0001', 'ended': 't2', 'end_reason': 'failed: not pushed: 2 uncommitted file(s), 0 unpushed commit(s)'},
                 {'job': 'correct-b-0001', 'pid': 2, 'started': 't3', 'worktree': '/wt/fix-b-0001'}]
        path = self._write(lines)
        self.assertEqual(lc.by_worktree(path)['/wt/fix-b-0001']['job'], 'correct-b-0001')

    def _write(self, lines):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        with open(path, 'w') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path


class ReviewAttemptsTests(unittest.TestCase):
    """``review_attempts`` (S-36503): how many reviewers a head has already had — folded over
    every run on the branch, not just its latest (:func:`lc.by_branch` would cap every count at
    1, PD8)."""

    def _write(self, lines):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        with open(path, 'w') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path

    def test_counts_only_ended_review_runs_on_the_branch_at_or_after_head_at(self):
        lines = [
            # started before head_at: does not count
            {'job': 'review-1', 'pid': 1, 'started': '2026-01-01T00:00:00Z',
             'branch': 'worker/T-1', 'kind': 'review'},
            {'job': 'review-1', 'ended': '2026-01-01T00:01:00Z', 'end_reason': 'finished'},
            # at/after head_at, ended, kind review: counts
            {'job': 'review-2', 'pid': 2, 'started': '2026-01-02T00:00:00Z',
             'branch': 'worker/T-1', 'kind': 'review'},
            {'job': 'review-2', 'ended': '2026-01-02T00:01:00Z', 'end_reason': 'finished'},
            # another kind: does not count
            {'job': 'correct-1', 'pid': 3, 'started': '2026-01-02T00:02:00Z',
             'branch': 'worker/T-1', 'kind': 'correct'},
            {'job': 'correct-1', 'ended': '2026-01-02T00:03:00Z', 'end_reason': 'finished'},
            # live, no ended: does not count
            {'job': 'review-3', 'pid': 4, 'started': '2026-01-02T00:04:00Z',
             'branch': 'worker/T-1', 'kind': 'review'},
            # another branch: does not count
            {'job': 'review-4', 'pid': 5, 'started': '2026-01-02T00:05:00Z',
             'branch': 'worker/T-2', 'kind': 'review'},
            {'job': 'review-4', 'ended': '2026-01-02T00:06:00Z', 'end_reason': 'finished'},
        ]
        path = self._write(lines)
        lanes = {'worker/T-1': {'head_at': '2026-01-02T00:00:00Z'}}
        self.assertEqual(lc.review_attempts(path, lanes), {'worker/T-1': 1})

    def test_two_qualifying_runs_under_different_jobs_count_two(self):
        # by_branch keeps one run per branch and would cap this at 1 (PD8): review_attempts
        # folds `runs`, not `by_branch`
        lines = [
            {'job': 'review-1', 'pid': 1, 'started': '2026-01-02T00:00:00Z',
             'branch': 'worker/T-1', 'kind': 'review'},
            {'job': 'review-1', 'ended': '2026-01-02T00:01:00Z', 'end_reason': 'finished'},
            {'job': 'review-2', 'pid': 2, 'started': '2026-01-02T00:02:00Z',
             'branch': 'worker/T-1', 'kind': 'review'},
            {'job': 'review-2', 'ended': '2026-01-02T00:03:00Z', 'end_reason': 'finished'},
        ]
        path = self._write(lines)
        lanes = {'worker/T-1': {'head_at': '2026-01-02T00:00:00Z'}}
        self.assertEqual(lc.review_attempts(path, lanes), {'worker/T-1': 2})

    def test_a_branch_with_no_head_at_in_lanes_yields_zero(self):
        lines = [{'job': 'review-1', 'pid': 1, 'started': '2026-01-02T00:00:00Z',
                  'branch': 'worker/T-1', 'kind': 'review'},
                 {'job': 'review-1', 'ended': '2026-01-02T00:01:00Z', 'end_reason': 'finished'}]
        path = self._write(lines)
        self.assertEqual(lc.review_attempts(path, {}), {})
        self.assertEqual(lc.review_attempts(path, {'worker/T-1': {}}), {})

    def test_occupancy_carries_the_attempts_count_on_its_review_entry_including_zero(self):
        lines = [{'job': 'review-1', 'pid': 1, 'started': '2026-01-02T00:00:00Z',
                  'branch': 'worker/T-1', 'item': 'T-1', 'kind': 'review'},
                 {'job': 'review-1', 'ended': '2026-01-02T00:01:00Z', 'end_reason': 'finished'}]
        path = self._write(lines)
        lanes = {'worker/T-1': {'state': 'REVIEW', 'round': 2,
                                'head_at': '2026-01-02T00:00:00Z'}}
        out = lc.occupancy(path, lanes=lanes)
        self.assertEqual(out['review']['T-1']['attempts'], 1)

        lanes2 = {'worker/T-1': {'state': 'REVIEW', 'round': 2,
                                 'head_at': '2026-01-03T00:00:00Z'}}
        out2 = lc.occupancy(path, lanes=lanes2)
        self.assertEqual(out2['review']['T-1']['attempts'], 0)


class RegistryReadOnceInvariants(unittest.TestCase):
    """The registry is parsed once per content, not once per question (the tick's own CPU: the
    feeder's ``corrections`` asked ``item_runs`` per run per item, each a full re-read and
    re-fold of ``sessions.jsonl`` — thousands of parses a tick). The cached answer must equal
    the full read's, whatever was written in between, and be the caller's own to mutate."""

    def _path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return os.path.join(d, 'sessions.jsonl')

    @staticmethod
    def _write(path, lines, mode='w'):
        with open(path, mode) as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    @staticmethod
    def _full(path):
        return lc.fold(lc.read_lines(path))

    @staticmethod
    def _full_item_runs(path, item):
        return [r for rs in lc.fold(lc.read_lines(path)).values() for r in rs
                if item and r.get('item') == item]

    def test_runs_and_item_runs_equal_the_full_read_across_appends(self):
        path = self._path()
        for seed in range(40):
            rng = random.Random(seed)
            lines, _meant = generate(rng, jobs=4, lines=30)
            self._write(path, lines[:10])
            for cut in (10, 20, 30):
                if cut > 10:
                    self._write(path, lines[cut - 10:cut], mode='a')
                with self.subTest(seed=seed, cut=cut):
                    self.assertEqual(lc.runs(path), self._full(path))
                    self.assertEqual(lc.latest(path),
                                     {j: rs[-1] for j, rs in self._full(path).items()})
                    for item in ('B-0001', 'B-0002', 'B-0003', 'B-0004', 'B-0009', None, ''):
                        self.assertEqual(lc.item_runs(path, item),
                                         self._full_item_runs(path, item))
                    self.assertEqual(lc.corrections(path), self._uncached(lc.corrections, path))
                    self.assertEqual(lc.attempts(path), self._uncached(lc.attempts, path))

    def _uncached(self, fn, path):
        """``fn(path)`` with the cache off: every read re-parses, as before the cache."""
        from unittest import mock
        with mock.patch.object(lc, '_REGISTRY_CACHE', _NeverHits()):
            return fn(path)

    def test_a_same_size_rewrite_is_read_again(self):
        # a coarse filesystem clock (ext4 on CI) can leave mtime and size unchanged across a
        # rewrite: the content itself is what the cache is keyed on
        path = self._path()
        self._write(path, [{'job': 'j', 'pid': 11, 'started': 't1', 'item': 'B-0001'}])
        self.assertEqual(lc.runs(path)['j'][0]['pid'], 11)
        st = os.stat(path)
        self._write(path, [{'job': 'j', 'pid': 12, 'started': 't1', 'item': 'B-0001'}])
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(path).st_size, st.st_size)
        self.assertEqual(lc.runs(path)['j'][0]['pid'], 12)
        self.assertEqual(lc.item_runs(path, 'B-0001')[0]['pid'], 12)

    def test_every_answer_is_the_callers_own_to_mutate(self):
        path = self._path()
        self._write(path, [{'job': 'j', 'pid': 1, 'started': 't1', 'item': 'B-0001',
                            'correction': {'text': 'FAIL', 'at': 't0'}}])
        a = lc.runs(path)
        a['j'][0]['correction']['text'] = 'changed'
        a['j'][0]['pid'] = 99
        a['j'].append({'job': 'j'})
        b = lc.item_runs(path, 'B-0001')
        b[0]['correction']['at'] = 'changed'
        self.assertEqual(lc.runs(path), self._full(path))
        self.assertEqual(lc.item_runs(path, 'B-0001'), self._full_item_runs(path, 'B-0001'))
        self.assertEqual(lc.latest(path)['j']['correction'], {'text': 'FAIL', 'at': 't0'})

    def test_a_missing_registry_is_empty_and_a_new_one_is_read(self):
        path = self._path()
        self.assertEqual(lc.runs(path), {})
        self.assertEqual(lc.item_runs(path, 'B-0001'), [])
        self._write(path, [{'job': 'j', 'pid': 1, 'started': 't1', 'item': 'B-0001'}])
        self.assertEqual(len(lc.item_runs(path, 'B-0001')), 1)
        os.remove(path)
        self.assertEqual(lc.runs(path), {})

    def test_corrections_parses_the_registry_once(self):
        path = self._path()
        lines = []
        for i in range(30):
            item = f'B-{i:04d}'
            lines += [{'job': f'fix-{i}', 'pid': i, 'started': f't{i:03d}', 'item': item,
                       'branch': f'fix/{item}'},
                      {'job': f'fix-{i}', 'ended': 'x', 'end_reason': 'failed',
                       'correction': {'text': 'FAIL', 'at': f't{i:03d}z'}, 'rounds': 1}]
        self._write(path, lines)
        from unittest import mock
        with mock.patch.object(lc, '_parse_registry', wraps=lc._parse_registry) as parse:
            got = lc.corrections(path)
            lc.occupancy(path)
        self.assertEqual(len(got), 30)
        self.assertEqual(got, self._uncached(lc.corrections, path))
        self.assertLessEqual(parse.call_count, 1)


class LingerGraceTests(unittest.TestCase):
    """T-0196/F-0227: the grace, the predicate and the index that reads it instead of its own
    arithmetic — one number, so the stop (:func:`asf.workers.health.settle_ended`) and the
    pool's reader (:func:`asf.workers.lifecycle.spent_sessions`) can never disagree about when a
    leftover process starts."""

    ENDED = '2026-01-01T00:00:00Z'

    def setUp(self):
        self.ended_ts = lc.headroom.parse_ts(self.ENDED).timestamp()

    def _root(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    @staticmethod
    def _write(root, product, lines):
        d = os.path.join(root, product)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'sessions.jsonl'), 'w') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def test_a_live_run_is_never_spent(self):
        self.assertFalse(lc.linger_spent({'job': 'j', 'pid': 1},
                                          now=self.ended_ts + lc.LINGER_GRACE_S * 10))

    def test_false_one_second_after_ended(self):
        run = {'ended': self.ENDED, 'session': 's'}
        self.assertFalse(lc.linger_spent(run, now=self.ended_ts + 1))

    def test_false_one_second_short_of_the_grace(self):
        run = {'ended': self.ENDED, 'session': 's'}
        self.assertFalse(lc.linger_spent(run, now=self.ended_ts + lc.LINGER_GRACE_S - 1))

    def test_true_at_exactly_the_grace(self):
        run = {'ended': self.ENDED, 'session': 's'}
        self.assertTrue(lc.linger_spent(run, now=self.ended_ts + lc.LINGER_GRACE_S))

    def test_true_well_past_the_grace(self):
        run = {'ended': self.ENDED, 'session': 's'}
        self.assertTrue(lc.linger_spent(run, now=self.ended_ts + lc.LINGER_GRACE_S * 10))

    def test_false_for_an_unreadable_ended_stamp(self):
        for bad in ('', None, 'not a stamp'):
            with self.subTest(ended=bad):
                run = {'ended': bad, 'session': 's'}
                self.assertFalse(lc.linger_spent(run, now=self.ended_ts + lc.LINGER_GRACE_S * 10))

    def test_grace_s_none_reads_the_module_constant(self):
        run = {'ended': self.ENDED, 'session': 's'}
        with mock.patch.object(lc, 'LINGER_GRACE_S', 10):
            self.assertTrue(lc.linger_spent(run, now=self.ended_ts + 10, grace_s=None))
            self.assertFalse(lc.linger_spent(run, now=self.ended_ts + 9, grace_s=None))

    def test_spent_sessions_over_two_products_registries(self):
        root = self._root()
        grace = 100
        ended_inside_grace = '2026-01-01T00:00:50Z'
        self._write(root, 'sample', [
            {'job': 'j1', 'ended': self.ENDED, 'session': 'sample/j1@A'},
            {'job': 'j2', 'pid': 1},
            {'job': 'j3', 'ended': ended_inside_grace, 'session': 'sample/j3@A'},
            {'job': 'j4', 'ended': self.ENDED},
        ])
        self._write(root, 'other', [
            {'job': 'k1', 'ended': self.ENDED, 'session': 'other/k1@A'},
        ])
        got = lc.spent_sessions(root, now=self.ended_ts + grace, grace_s=grace)
        self.assertEqual(got, {'sample/j1@A', 'other/k1@A'})

    def test_spent_sessions_on_a_missing_root_is_empty(self):
        root = self._root()
        self.assertEqual(lc.spent_sessions(os.path.join(root, 'does-not-exist')), set())

    def test_health_keeps_one_number(self):
        from asf.workers import health
        self.assertEqual(health.LINGER_GRACE_S, lc.LINGER_GRACE_S)


class LaneOfInvariants(unittest.TestCase):
    """``lane_of`` is the one guard every reader of ``run['lane']`` takes (F-lane-collision): a
    cloud runtime's launch line written before its own marker moved to ``runtime_lane`` still
    carries a bare string on the harvest lane state machine's key (``"lane": "cloud"``), and
    nothing that expects a map may raise on it."""

    def test_a_dict_lane_passes_through(self):
        rec = {'state': 'QUEUED', 'pr': 7}
        self.assertEqual(lc.lane_of({'lane': rec}), rec)

    def test_a_string_lane_reads_as_absent(self):
        self.assertEqual(lc.lane_of({'lane': 'cloud'}), {})

    def test_no_lane_and_no_run_read_as_absent(self):
        self.assertEqual(lc.lane_of({}), {})
        self.assertEqual(lc.lane_of(None), {})

    def test_occupancy_never_raises_on_a_string_lane(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        with open(path, 'w') as f:
            f.write(json.dumps({'job': 'remote-1', 'item': 'T-0002', 'branch': 'cloud/remote-1',
                                'pid': None, 'started': 't', 'lane': 'cloud'}) + '\n')
        out = lc.occupancy(path)  # must not raise AttributeError: 'str' object has no attribute 'get'
        self.assertNotIn('T-0002', out['busy'])
        self.assertNotIn('T-0002', out['waiting_landing'])


class _NeverHits(dict):
    """A registry cache that never holds anything: every read is a full parse."""

    def __setitem__(self, key, value):
        pass


def evidences():
    """Every combination of the evidence fields the judgement reads."""
    for result, alive, remote, uncommitted, unpushed in itertools.product(
            (None, OK, ERR), (True, False), ('', 'sha'), (0, 2), (0, 1)):
        yield lc.Evidence(result=result, alive=alive, remote_sha=remote, uncommitted=uncommitted,
                          unpushed=unpushed, head_on_remote=bool(remote) and unpushed == 0,
                          has_commits=True, worktree=True)


class JudgementInvariants(unittest.TestCase):
    RUN = {'job': 'j', 'branch': 'fix/B-0001', 'item': 'B-0001', 'pid': 1, 'started': 't'}

    def test_finished_means_pushed_under_every_evidence(self):
        # B-0051: `finished` is written only when origin/<branch> holds everything
        for ev in evidences():
            with self.subTest(ev=ev):
                verdict = lc.judge(self.RUN, ev)
                if verdict == lc.FINISHED:
                    self.assertTrue(ev.pushed)
                    self.assertEqual(ev.result, OK)
                if ev.result == OK and not ev.pushed:
                    self.assertEqual(verdict, 'failed: ' + lc.push_gap(ev))

    def test_no_result_is_running_while_alive_and_dead_pid_after(self):
        for ev in evidences():
            if ev.result is None:
                self.assertEqual(lc.judge(self.RUN, ev), None if ev.alive else lc.DEAD_PID)

    def test_a_run_with_no_branch_is_judged_on_the_result_alone(self):
        run = {'job': 'j', 'pid': 1, 'started': 't'}
        self.assertEqual(lc.judge(run, lc.Evidence(result=OK)), lc.FINISHED)
        self.assertEqual(lc.judge(run, lc.Evidence(result=ERR)), 'failed')

    def test_a_runtime_error_text_is_a_failure_with_its_signature(self):
        rec = dict(OK, result='Invalid API key · Please run /login')
        self.assertEqual(lc.judge(self.RUN, lc.Evidence(result=rec, remote_sha='s')), 'failed: auth')

    def test_b0076_a_pushed_branch_never_committed_to_is_failed_empty_branch(self):
        # a run that says ok on a branch that is pushed but was never itself committed to has
        # nothing for harvest to land — read `finished` it sits `eligible` forever and the item
        # it worked stays busy for ever, blocking every task waiting on its footprint
        ev = lc.Evidence(result=OK, remote_sha='s', head_on_remote=True, in_trunk=True,
                         has_commits=False, worktree=True)
        self.assertEqual(lc.judge(self.RUN, ev), 'failed: empty branch: nothing to land')
        self.assertEqual(lc.derive(self.RUN, ev).name, lc.ENDED)
        # a branch fast-forward-landed onto the trunk is also `in_trunk`, but it was committed to
        landed_ev = lc.Evidence(result=OK, remote_sha='s', head_on_remote=True, in_trunk=True,
                                has_commits=True, worktree=True)
        self.assertEqual(lc.judge(self.RUN, landed_ev), lc.FINISHED)


class LivenessVerdictTests(unittest.TestCase):
    """F-0234 §1: `lifecycle.liveness`, one row per verdict, each stating only the facts it
    means and leaving every other argument at its default. The function has no caller yet
    (Task 1 of the plan) — this pins its own contract in isolation."""

    @staticmethod
    def _observed(pid, session):
        return observe.Observed(pid=pid, ppid=None, account=None, session=session,
                                 product=None, job=None, owner=None, cwd=None)

    def test_the_os_has_the_last_word_whatever_the_observation_says(self):
        # a pid the OS does not answer for is GONE even though the observation carries the
        # run's own session — this is the row that proves the order (P3)
        run = {'pid': 1, 'session': 'p/j@t'}
        seen = self._observed(1, 'p/j@t')
        self.assertEqual(lc.liveness(run, seen, exists=lambda p: False), lc.GONE)

    def test_an_observed_session_carrying_the_runs_own_id_is_alive(self):
        run = {'pid': 1, 'session': 'p/j@t'}
        seen = self._observed(1, 'p/j@t')
        self.assertEqual(lc.liveness(run, seen, exists=lambda p: True), lc.ALIVE)

    def test_a_run_with_no_session_of_its_own_observed_at_all_is_alive(self):
        # F-0076's rule for a run recorded before sessions existed, kept whole
        run = {'pid': 1}
        seen = self._observed(1, 'p/other@t')
        self.assertEqual(lc.liveness(run, seen, exists=lambda p: True), lc.ALIVE)

    def test_an_observed_session_naming_a_different_id_is_reused(self):
        run = {'pid': 1, 'session': 'p/j@t'}
        seen = self._observed(1, 'p/other@t')
        self.assertEqual(lc.liveness(run, seen, exists=lambda p: True), lc.REUSED)

    def test_not_ours_with_no_observation_is_reused(self):
        run = {'pid': 1, 'session': 'p/j@t'}
        self.assertEqual(lc.liveness(run, None, exists=lambda p: True, is_ours=False), lc.REUSED)

    def test_ours_silent_within_the_bound_is_unknown(self):
        # the live worker that is no longer a corpse — the card's whole reason
        run = {'pid': 1, 'session': 'p/j@t'}
        self.assertEqual(lc.liveness(run, None, exists=lambda p: True, is_ours=True,
                                      silent_min=30, silent_for=5.0), lc.UNKNOWN)

    def test_ours_silent_past_the_bound_is_gone(self):
        run = {'pid': 1, 'session': 'p/j@t'}
        self.assertEqual(lc.liveness(run, None, exists=lambda p: True, is_ours=True,
                                      silent_min=30, silent_for=31.0), lc.GONE)

    def test_a_cloud_token_follows_cloudpid_alive_not_exists(self):
        # exists is patched to answer the opposite of cloudpid.alive, so a pass here proves the
        # cloud branch comes first and never consults exists at all
        run = {'pid': lc.cloudpid.token('1')}
        with mock.patch.object(lc.cloudpid, 'alive', return_value=True):
            self.assertEqual(lc.liveness(run, None, exists=lambda p: False), lc.ALIVE)
        with mock.patch.object(lc.cloudpid, 'alive', return_value=False):
            self.assertEqual(lc.liveness(run, None, exists=lambda p: True), lc.GONE)

    def test_an_unmeasured_silence_is_not_a_silence(self):
        # PD1, PD4: is_ours=True alone does not make a missing bound a silence
        run = {'pid': 1, 'session': 'p/j@t'}
        self.assertEqual(lc.liveness(run, None, exists=lambda p: True, is_ours=True,
                                      silent_min=30, silent_for=None), lc.UNKNOWN)
        self.assertEqual(lc.liveness(run, None, exists=lambda p: True, is_ours=True,
                                      silent_min=None, silent_for=31.0), lc.UNKNOWN)

    def test_the_vocabulary(self):
        self.assertEqual(set(lc.LIVENESS), {lc.ALIVE, lc.GONE, lc.REUSED, lc.UNKNOWN})
        self.assertLessEqual(set(lc.LIVE_VERDICTS), set(lc.LIVENESS))


class GatherLivenessTests(unittest.TestCase):
    """F-0234 §3: `gather`'s new `liveness=` fills `Evidence.liveness` — a null default like
    every other field (P2), so a caller that asks nothing of it changes nothing it reads."""
    RUN = {'job': 'j', 'pid': 1, 'started': 't'}

    def test_no_liveness_callable_leaves_the_field_empty(self):
        ev = lc.gather(None, self.RUN, alive=lambda pid: True)
        self.assertEqual(ev.liveness, '')

    def test_a_liveness_callable_fills_the_field(self):
        ev = lc.gather(None, self.RUN, alive=lambda pid: True, liveness=lambda pid: lc.GONE)
        self.assertEqual(ev.liveness, lc.GONE)


class NoLandingRunInvariants(unittest.TestCase):
    """A groom (adjudicate) session rules into an answers file and is told "the repository is not
    your work": its branch never carries a commit. Judged like a lane it always failed — `not
    pushed` over its staged answers file, or `empty branch` — and was sent a correction that told
    it to commit what its brief forbids (groom-2026-09-22 → correct-f-0080)."""
    GROOM = {'job': 'groom-2026-09-22', 'kind': 'groom', 'branch': 'groom/2026-09-22',
             'item': 'F-0080', 'pid': 1, 'started': 't'}

    def test_a_groom_run_is_judged_on_its_result_alone(self):
        dirty = lc.Evidence(result=OK, remote_sha='s', uncommitted=1, has_commits=False,
                            worktree=True)
        empty = lc.Evidence(result=OK, remote_sha='s', head_on_remote=True, in_trunk=True,
                            has_commits=False, worktree=True)
        for ev in (dirty, empty, lc.Evidence(result=OK)):
            self.assertEqual(lc.judge(self.GROOM, ev), lc.FINISHED)
        self.assertEqual(lc.judge(self.GROOM, lc.Evidence(result=ERR)), 'failed')

    def test_a_groom_report_saying_pushed_no_is_not_unpushed_work(self):
        rec = dict(OK, result='REPORT\nitem: F-0080\nkind: groom\nstatus: done\n'
                              'pushed: no — a ruling is not a commit\n')
        self.assertEqual(lc.judge(self.GROOM, lc.Evidence(result=rec)), lc.FINISHED)

    def test_a_run_sent_back_on_a_groom_branch_lands_nothing_either(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, 'sessions.jsonl')
        corr = {'job': 'correct-f-0080', 'kind': 'correct', 'branch': 'groom/2026-09-22',
                'item': 'F-0080', 'pid': 2, 'started': 't2'}
        with open(path, 'w') as f:
            for rec in (self.GROOM, {'job': self.GROOM['job'], 'ended': 't1'}, corr):
                f.write(json.dumps(rec) + '\n')
        self.assertFalse(lc.lands(corr, path))
        self.assertTrue(lc.lands({'job': 'coder-t-1', 'kind': 'coder', 'branch': 'worker/T-1'},
                                 path))
        ev = lc.Evidence(result=OK, remote_sha='s', uncommitted=1, has_commits=False, worktree=True)
        self.assertEqual(lc.derive(corr, ev, path=path).name, lc.PUSHED)


class ReviewRunInvariants(unittest.TestCase):
    """B-0276: a review session writes ``docs/reviews/<n>-<item>.md`` in its worktree and leaves
    it uncommitted for the factory to file off the branch (:mod:`asf.evidence.review_store`),
    because a push to the branch moves its PR head and restarts the PR's whole CI. Judged like a
    lane that must push, such a run was read ``not pushed`` over that one file and sent a
    correction telling it to commit what its brief forbids: the review looped to the daily
    relaunch cap and was parked, no approval was ever written, and PRs green for hours (#842,
    #848, #849, #858, #859 on 2026-10-06) waited. It is never held for a push — but it still owes
    a review, so one that filed none is still ``empty branch``."""
    REVIEW = {'job': 'review-t-0589', 'kind': 'review', 'branch': 'worker/T-0589',
              'item': 'T-0589', 'pid': 1, 'started': 't'}
    FILED = dict(REVIEW, review_filed='/store/1-t-0589.md')

    def test_a_review_run_lands_nothing(self):
        self.assertFalse(lc.lands(self.REVIEW))

    def test_a_review_run_is_not_held_for_its_uncommitted_review(self):
        dirty = lc.Evidence(result=OK, remote_sha='s', uncommitted=1, has_commits=False,
                            worktree=True)
        clean = lc.Evidence(result=OK, remote_sha='s', head_on_remote=True, has_commits=False,
                            worktree=True)
        for ev in (dirty, clean, lc.Evidence(result=OK)):
            self.assertEqual(lc.judge(self.FILED, ev), lc.FINISHED)
        self.assertEqual(lc.judge(self.FILED, lc.Evidence(result=ERR)), 'failed')

    def test_a_review_report_saying_pushed_no_is_not_unpushed_work(self):
        rec = dict(OK, result='REPORT\nitem: T-0589\nkind: review\nstatus: done\n'
                              'pushed: no — the review is left for the factory to file\n')
        self.assertEqual(lc.judge(self.FILED, lc.Evidence(result=rec, remote_sha='s')),
                         lc.FINISHED)

    def test_a_review_run_that_filed_no_review_is_still_empty(self):
        """The push is forgiven, the review is not: a session that left none did nothing, and
        the round that says so is what gets the item a verdict."""
        dirty = lc.Evidence(result=OK, remote_sha='s', uncommitted=1, has_commits=False,
                            worktree=True)
        self.assertEqual(lc.judge(self.REVIEW, dirty), f'failed: {lc.EMPTY_BRANCH}')

    def test_the_reviewed_branchs_own_runs_are_still_held(self):
        """The exemption is the kind, never the branch: a review runs on the item's own branch,
        so the code and correction runs that share it must still be held for their work."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, 'sessions.jsonl')
        coder = {'job': 'coder-t-0589', 'kind': 'coder', 'branch': 'worker/T-0589',
                 'item': 'T-0589', 'pid': 2, 'started': 't2'}
        with open(path, 'w') as f:
            for rec in (self.REVIEW, {'job': self.REVIEW['job'], 'ended': 't1'}, coder):
                f.write(json.dumps(rec) + '\n')
        self.assertFalse(lc.lands(self.REVIEW, path))
        self.assertTrue(lc.lands(coder, path))
        dirty = lc.Evidence(result=OK, remote_sha='s', uncommitted=1, has_commits=False,
                            worktree=True)
        self.assertEqual(lc.judge(coder, dirty, landing=lc.lands(coder, path)),
                         f'failed: {lc.push_gap(dirty)}')


class StateMachineInvariants(unittest.TestCase):
    def test_every_state_but_reaped_has_a_successor_and_all_successors_are_states(self):
        self.assertEqual(set(lc.TRANSITIONS), set(lc.STATES))
        for state, nxt in lc.TRANSITIONS.items():
            self.assertTrue(set(nxt) <= set(lc.STATES), state)
            if state == lc.REAPED:
                self.assertEqual(nxt, ())
            else:
                self.assertTrue(nxt, f'{state} is terminal')

    def test_derive_only_yields_named_states_over_generated_runs_and_evidence(self):
        for seed in range(100):
            rng = random.Random(seed)
            lines, _ = generate(rng)
            for run in (r for rs in lc.fold(lines).values() for r in rs):
                for ev in evidences():
                    s = lc.derive(run, ev)
                    self.assertIn(s.name, lc.STATES, (seed, run, ev))
                    if s.name == lc.PUSHED:
                        self.assertEqual(s.reason, lc.FINISHED)

    def test_the_lane_walk(self):
        """launched → running → ended → pushed → held → corrected → landed → reaped, on one branch."""
        run = {'job': 'fix-b-0001', 'branch': 'fix/B-0001', 'item': 'B-0001', 'pid': 1, 'started': 't1'}
        self.assertEqual(lc.derive(run, lc.Evidence(alive=True, worktree=True)).name, lc.LAUNCHED)
        self.assertEqual(lc.derive(run, lc.Evidence(alive=True, worktree=True, has_commits=True)).name, lc.RUNNING)
        ok_unpushed = lc.Evidence(result=OK, worktree=True, has_commits=True, unpushed=1)
        self.assertEqual(str(lc.derive(run, ok_unpushed)),
                         'ended(failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s))')
        pushed = lc.Evidence(result=OK, worktree=True, has_commits=True, remote_sha='s', head_on_remote=True)
        self.assertEqual(str(lc.derive(run, pushed)), 'pushed(finished)')
        # health records the transition; from here the registry carries it
        run = dict(run, ended='t2', end_reason='finished')
        self.assertEqual(lc.derive(run, lc.Evidence()).name, lc.PUSHED)
        held = dict(run, rounds=1, correction={'kind': 'gate', 'text': 'FAIL: t', 'at': 't3'})
        self.assertEqual(str(lc.derive(held, lc.Evidence())), 'held(FAIL: t)')
        at_cap = dict(held, rounds=lc.ROUND_CAP)
        self.assertEqual(lc.derive(at_cap, lc.Evidence()).name, lc.HELD)  # mixed rounds: correct
        same = dict(held, rounds=lc.ROUND_CAP,
                    correction=dict(held['correction'], same=lc.ROUND_CAP))
        self.assertEqual(lc.derive(same, lc.Evidence()).name, lc.ADJUDICATE)
        landed = dict(run, harvested='deadbeef')
        self.assertEqual(lc.derive(landed, lc.Evidence(worktree=True)).name, lc.LANDED)
        self.assertEqual(lc.derive(landed, lc.Evidence(worktree=False)).name, lc.REAPED)

    def test_a_correction_is_answered_by_a_later_run_on_the_item(self):
        lines = [{'job': 'a', 'pid': 1, 'started': 't1', 'item': 'B-0001', 'branch': 'b',
                  'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'a', 'rounds': 1, 'correction': {'kind': 'gate', 'text': 'x', 'at': 't3'}}]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 's.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(ln) for ln in lines) + '\n')
        run = lc.latest(path)['a']
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.HELD)
        self.assertEqual(lc.corrections(path)['B-0001']['rounds'], 1)
        self.assertEqual(lc.corrections(path)['B-0001']['branch'], 'b')
        with open(path, 'a') as f:
            f.write(json.dumps({'job': 'correct-b-0001', 'pid': os.getpid(), 'started': 't4',
                                'item': 'B-0001', 'branch': 'b'}) + '\n')
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.CORRECTED)
        self.assertEqual(lc.corrections(path), {})
        self.assertEqual(lc.inflight(path), [{'item': 'B-0001', 'kind': None, 'account': None,
                                              'job': 'correct-b-0001', 'started': 't4'}])
        self.assertEqual(lc.attempts(path), {'B-0001': 2})

    def _ruling_log(self, d, text):
        """A fake session log whose REPORT's ``ruling:`` field is ``text`` — what
        ``settled_prs`` reads (:func:`asf.workers.report.ruling`)."""
        log = os.path.join(d, 'ruling.jsonl')
        rec = {'type': 'result', 'subtype': 'success', 'is_error': False,
               'result': f'REPORT\nitem: B-0001\nstatus: done\nruling: {text}\n'}
        with open(log, 'w') as f:
            f.write(json.dumps(rec) + '\n')
        return log

    def test_b0128_an_adjudicate_run_does_not_answer_the_correction(self):
        """B-0128: an adjudicate session rules, it does not correct — the branch stays held (not
        bounced BACK → PUSHED, restarting review) and the correction is marked ``settled`` once
        the ruling is in, so the feeder asks for no second adjudicate session over the same hold.
        The ruling's PR numbers (``#773``, ``#775``) come back too, for the WAITS ON merge row."""
        lines = [{'job': 'a', 'pid': 1, 'started': 't1', 'item': 'B-0001', 'branch': 'b',
                  'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'a', 'rounds': 3, 'correction': {'kind': 'review', 'text': 'x', 'at': 't3',
                                                           'same': 3}}]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 's.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(ln) for ln in lines) + '\n')
        run = lc.latest(path)['a']
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.ADJUDICATE)
        self.assertFalse(lc.corrections(path)['B-0001']['settled'])
        log = self._ruling_log(d, 'the work is done — waits on merge of #773, #775')
        with open(path, 'a') as f:
            f.write(json.dumps({'job': 'adjudicate-b-0001', 'item': 'B-0001', 'branch': 'b',
                                'kind': 'adjudicate', 'pid': os.getpid(), 'started': 't4',
                                'ended': 't5', 'end_reason': 'finished', 'log': log}) + '\n')
        # still ADJUDICATE, not CORRECTED: the ruling did not touch the branch
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.ADJUDICATE)
        self.assertEqual(lc.corrections(path)['B-0001']['rounds'], 3)
        self.assertTrue(lc.corrections(path)['B-0001']['settled'])
        self.assertEqual(lc.corrections(path)['B-0001']['prs'], ['773', '775'])

    def test_b0128_a_crashed_adjudicate_run_delivers_no_ruling_and_does_not_settle(self):
        """B-0128 C1: ``settled`` must gate on :func:`lc.finished`, not the bare ``ended`` flag —
        an adjudicate session that crashed, was stopped, or ran out of quota before it ruled ended
        without a ``finished`` reason and delivered no ruling, so it must not settle the hold
        forever."""
        lines = [{'job': 'a', 'pid': 1, 'started': 't1', 'item': 'B-0001', 'branch': 'b',
                  'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'a', 'rounds': 3, 'correction': {'kind': 'review', 'text': 'x', 'at': 't3'}}]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 's.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(ln) for ln in lines) + '\n')
        with open(path, 'a') as f:
            f.write(json.dumps({'job': 'adjudicate-b-0001', 'item': 'B-0001', 'branch': 'b',
                                'kind': 'adjudicate', 'pid': os.getpid(), 'started': 't4',
                                'ended': 't5', 'end_reason': 'crashed'}) + '\n')
        self.assertFalse(lc.corrections(path)['B-0001']['settled'])
        self.assertEqual(lc.corrections(path)['B-0001']['prs'], [])


class SessionStateInvariants(unittest.TestCase):
    """§2.1-§2.2's classifier, over hand-built runs and Evidence — no git, no clock (F-0098)."""

    RUN = {'job': 'fix-bug-b-0080', 'branch': 'fix/B-0080', 'item': 'B-0080', 'pid': 1,
           'started': 't'}

    def test_a_result_that_says_ok_on_a_pushed_branch_is_ended_awaiting_tick_not_dead(self):
        # the four sessions of 2026-09-23: no `ended`, pid gone, a result that says ok, pushed
        ev = lc.Evidence(result=OK, alive=False, remote_sha='s', head_on_remote=True,
                         has_commits=True)
        status = lc.classify(self.RUN, ev)
        self.assertEqual(status.name, lc.ENDED_AWAITING_TICK)
        self.assertEqual(status.result, lc.FINISHED)
        self.assertNotEqual(status.name, lc.DEAD)

    def test_a_result_that_says_ok_but_unpushed_gives_the_same_text_health_writes_later(self):
        ev = lc.Evidence(result=OK, alive=False, remote_sha='s', has_commits=True, unpushed=2)
        status = lc.classify(self.RUN, ev)
        self.assertEqual(status.name, lc.ENDED_AWAITING_TICK)
        self.assertEqual(status.result, lc.judge(self.RUN, ev))
        self.assertEqual(status.result,
                         'failed: not pushed: 0 uncommitted file(s), 2 unpushed commit(s)')

    def test_no_result_but_a_pushed_branch_with_commits_is_pushed_no_report(self):
        ev = lc.Evidence(result=None, alive=False, remote_sha='s', has_commits=True)
        status = lc.classify(self.RUN, ev)
        self.assertEqual((status.name, status.result, status.source),
                         (lc.ENDED_AWAITING_TICK, lc.PUSHED_NO_REPORT, 'branch'))

    def test_no_result_and_nothing_on_origin_is_dead_no_record(self):
        ev = lc.Evidence(result=None, alive=False, remote_sha='')
        status = lc.classify(self.RUN, ev)
        self.assertEqual((status.name, status.reason), (lc.DEAD, lc.NO_RECORD))

    def test_b0076_a_pushed_branch_never_committed_to_is_dead_not_ended_awaiting_tick(self):
        ev = lc.Evidence(result=None, alive=False, remote_sha='s', has_commits=False)
        status = lc.classify(self.RUN, ev)
        self.assertEqual(status.name, lc.DEAD)

    def test_a_recorded_end_reads_the_ledger(self):
        finished = dict(self.RUN, ended='t2', end_reason='finished')
        self.assertEqual(lc.classify(finished, lc.Evidence()).name, lc.FINISHED)
        stopped = dict(self.RUN, ended='t2', end_reason='stopped')
        status = lc.classify(stopped, lc.Evidence())
        self.assertEqual(status.name, lc.DEAD)
        self.assertEqual(status.label, 'dead: stopped by the operator')
        failed = dict(self.RUN, ended='t2', end_reason='failed: quota')
        status = lc.classify(failed, lc.Evidence())
        self.assertEqual(status.name, lc.FAILED)
        self.assertEqual(status.label, 'failed: quota')

    def test_a_retired_dead_pid_line_is_dead_and_a_later_result_is_re_judged(self):
        # B-0028: the result may land after the check; both the retired and the honest text answer
        dead_pid = dict(self.RUN, ended='t2', end_reason=lc.DEAD_PID)
        self.assertEqual(lc.classify(dead_pid, lc.Evidence()).name, lc.DEAD)
        self.assertEqual(lc.classify(dead_pid, lc.Evidence()).reason, lc.NO_RECORD)
        ev = lc.Evidence(result=OK, remote_sha='s', head_on_remote=True, has_commits=True)
        status = lc.classify(dead_pid, ev)
        self.assertEqual(status.name, lc.ENDED_AWAITING_TICK)
        self.assertEqual(status.result, lc.judge(dead_pid, ev))
        honest = dict(self.RUN, ended='t2', end_reason=f'{lc.DEAD}: {lc.NO_RECORD}')
        self.assertEqual(lc.classify(honest, lc.Evidence()).name, lc.DEAD)
        self.assertEqual(lc.classify(honest, ev).name, lc.ENDED_AWAITING_TICK)

    def test_classify_returns_a_named_state_over_generated_runs_and_evidence(self):
        for seed in range(100):
            rng = random.Random(seed)
            lines, _ = generate(rng)
            for run in (r for rs in lc.fold(lines).values() for r in rs):
                for ev in evidences():
                    status = lc.classify(run, ev)
                    self.assertIn(status.name, lc.SESSION_STATES, (seed, run, ev))

    def test_holds_seat_is_true_for_working_and_false_for_the_rest(self):
        self.assertTrue(lc.holds_seat(lc.Status(lc.WORKING)))
        for name in (lc.ENDED_AWAITING_TICK, lc.FINISHED, lc.FAILED, lc.DEAD):
            self.assertFalse(lc.holds_seat(lc.Status(name)), name)
        # a row may carry its state as a bare name string too
        self.assertTrue(lc.holds_seat(lc.WORKING))
        self.assertFalse(lc.holds_seat(lc.DEAD))

    def test_seats_counts_a_row_with_no_state_as_holding_one(self):
        rows = [{'state': lc.Status(lc.WORKING)}, {'state': lc.Status(lc.DEAD)}, {}]
        self.assertEqual(lc.seats(rows), 2)

    def test_state_of_gathers_no_git_for_a_recorded_run_or_a_live_one(self):
        def raises(*a, **k):
            raise AssertionError('gather must not be called')
        recorded = dict(self.RUN, ended='t2', end_reason='finished')
        self.assertEqual(lc.state_of(None, recorded, gather_fn=raises).name, lc.FINISHED)
        self.assertEqual(lc.state_of(None, self.RUN, alive=lambda pid: True,
                                     gather_fn=raises).name, lc.WORKING)

    def test_state_of_reads_the_logs_result_line_for_a_recorded_death(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        log = os.path.join(d, 'log.jsonl')
        with open(log, 'w') as f:
            f.write(json.dumps(OK) + '\n')
        dead_pid = dict(self.RUN, log=log, ended='t2', end_reason=lc.DEAD_PID)

        def raises(*a, **k):
            raise AssertionError('gather must not be called')
        status = lc.state_of(None, dead_pid, gather_fn=raises)
        self.assertEqual(status.name, lc.ENDED_AWAITING_TICK)

    def test_state_of_calls_gather_only_once_the_process_is_gone(self):
        called = []

        def fake_gather(product, run, alive=None):
            called.append(run['job'])
            return lc.Evidence(remote_sha='s', has_commits=True)
        status = lc.state_of(None, self.RUN, alive=lambda pid: False, gather_fn=fake_gather)
        self.assertEqual(called, [self.RUN['job']])
        self.assertEqual(status.name, lc.ENDED_AWAITING_TICK)


class LiveStatusTest(unittest.TestCase):
    """F-0199: the rung below `state_of` — a run still live on the ledger, judged on its pid and
    its log and nothing else."""

    RUN = {'job': 'j', 'item': 'F-0001', 'kind': 'spec', 'branch': 'spec/F-0001', 'pid': 101,
           'started': 't1'}

    def status(self, alive, rec):
        return lc.live_status(self.RUN, alive=lambda pid: alive, result=lambda run: rec)

    def test_a_live_pid_is_working(self):
        self.assertEqual(self.status(True, None).name, lc.WORKING)
        self.assertEqual(self.status(True, OK).name, lc.WORKING)   # the pid wins

    def test_an_exit_on_a_success_result_is_finished_and_says_it_is_the_report(self):
        status = self.status(False, OK)
        self.assertEqual((status.name, status.result, status.source),
                         (lc.FINISHED, lc.FINISHED, 'report'))

    def test_an_exit_on_a_failing_result_is_ended_awaiting_tick(self):
        bad = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
               'result': 'boom'}
        status = self.status(False, bad)
        self.assertEqual((status.name, status.source), (lc.ENDED_AWAITING_TICK, 'report'))

    def test_an_exit_with_no_result_is_dead_with_no_record(self):
        status = self.status(False, None)
        self.assertEqual((status.name, status.reason), (lc.DEAD, lc.NO_RECORD))

    def test_it_never_reaches_git(self):
        def raises(*a, **k):
            raise AssertionError('gather must not be called')
        with mock.patch.object(lc, 'gather', raises), mock.patch.object(lc, '_git', raises):
            for rec in (None, OK, {'is_error': True, 'result': 'boom'}):
                self.assertIn(lc.live_status(self.RUN, lambda pid: False,
                                             lambda run: rec).name, lc.SESSION_STATES)

    def test_only_working_holds_a_seat(self):
        self.assertTrue(lc.holds_seat(self.status(True, None)))
        for rec in (None, OK):
            self.assertFalse(lc.holds_seat(self.status(False, rec)))

    def test_inflight_word_covers_every_session_state(self):
        for name in lc.SESSION_STATES:
            self.assertIn(lc.inflight_word(name),
                          (lc.WORKING, lc.FINISHED, lc.EXITED_AWAITING, lc.DEAD))
        self.assertEqual(lc.inflight_word(lc.Status(lc.ENDED_AWAITING_TICK)),
                         'exited — awaiting harvest')
        self.assertEqual(lc.inflight_word(lc.DEAD), 'dead')       # never 'dead pid'


class OneClassifierTest(unittest.TestCase):
    """§2.7: no module but ``lifecycle`` derives a session state. ``NOT_YET`` names the readers
    this plan has not converted yet — Task 2 removes its two, Task 3 its three, Task 4 the last
    four and asserts ``NOT_YET == ()``, the moment this becomes the fence §3.6 asks for."""

    #: Task 2 empties the first two, Task 3 the next three, Task 4 the next four. The last three,
    #: `score.py`, `cloudpid.py` and `widen_footprint.py`, are readers F-0098's plan does not name
    #: and no Task's `writes:` covers (T-0087's REPORT: `needs writes:`) — they stay exempt until
    #: a Task claims them. F-0199 converted `asf/tick/summary.py` ahead of Task 3, so that group
    #: is two entries here, not three.
    NOT_YET = (
        'asf/capacity.py',
        'asf/feeder/tiers.py',
        'asf/views/sessions.py',
        'asf/views/status.py',
        'asf/workers/stall.py',
        'asf/workers/health.py',
        'asf/tick/step_health.py',
        'asf/harvest/harvest.py',
        'asf/scorecard/score.py',
        'asf/workers/cloudpid.py',
        'asf/tick/widen_footprint.py',
    )

    #: exactly the words §2.7 names. ``finished``/``failed`` are ordinary English words used all
    #: over the tree for unrelated outcomes (rule checks, lane status) and were never the defect
    #: this spec fixes — a bare literal is fine there, so only these four are guarded.
    STATE_WORDS = (lc.DEAD, lc.DEAD_PID, lc.WORKING, lc.ENDED_AWAITING_TICK)

    #: the three table headings named as the one allowed exception (P11) — kept verbatim even
    #: though title-casing already keeps them clear of :data:`STATE_WORDS`.
    ALLOWED = {('asf/views/sessions.py', 'Working'),
              ('asf/views/sessions.py', 'Ended, awaiting tick'),
              ('asf/views/sessions.py', 'Dead')}

    @staticmethod
    def _reads_end_reason(node):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            return node.slice.value == 'end_reason'
        if isinstance(node, ast.Attribute):
            return node.attr == 'end_reason'
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and node.args
                and isinstance(node.args[0], ast.Constant)):
            return node.args[0].value == 'end_reason'
        return False

    def _violations(self, relpath, tree):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in self.STATE_WORDS and (relpath, node.value) not in self.ALLOWED:
                    out.append(f'{relpath}:{node.lineno}: {node.value!r} as a literal')
            elif isinstance(node, ast.Compare):
                sides = [node.left, *node.comparators]
                if (any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops)
                        and any(self._reads_end_reason(s) for s in sides)):
                    out.append(f'{relpath}:{node.lineno}: a direct end_reason comparison')
        return out

    def test_no_module_but_lifecycle_derives_a_state_word(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        violations = []
        for path in sorted(glob.glob(os.path.join(root, 'asf', '**', '*.py'), recursive=True)):
            relpath = os.path.relpath(path, root).replace(os.sep, '/')
            if relpath == 'asf/workers/lifecycle.py' or relpath in self.NOT_YET:
                continue
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
            violations += self._violations(relpath, tree)
        self.assertEqual(violations, [])


class EveryHoldHandsTheGuardTheTrunk(unittest.TestCase):
    """F-0217/S-37952: every call to :func:`lc.hold` passes ``main=`` — the one argument that
    lets a worktree rebased onto a newer trunk (:func:`lc.rebase_of`) count as a moved head when
    the hold's evidence carries no ``head=`` at all (a session that committed and was never
    published, where ``ev.remote_sha`` is ``''``). A caller importing ``hold`` bare from
    :mod:`asf.workers.lifecycle` would route around the attribute match, so that shape is
    matched too — distinct from the local ``hold(job, …)`` closures ``asf/harvest/harvest.py``
    and ``asf/harvest/lane.py`` define for themselves, which this walk must not flag as
    offenders it can never fix."""

    @staticmethod
    def _sites(root):
        sites = []
        for path in sorted(glob.glob(os.path.join(root, 'asf', '**', '*.py'), recursive=True)):
            relpath = os.path.relpath(path, root).replace(os.sep, '/')
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
            imports_bare_hold = any(
                isinstance(node, ast.ImportFrom) and node.module == 'asf.workers.lifecycle'
                and any(alias.name == 'hold' for alias in node.names)
                for node in ast.walk(tree))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_attr = (isinstance(func, ast.Attribute) and func.attr == 'hold'
                           and isinstance(func.value, ast.Name) and func.value.id == 'lifecycle')
                is_bare = imports_bare_hold and isinstance(func, ast.Name) and func.id == 'hold'
                if is_attr or is_bare:
                    has_main = any(kw.arg == 'main' for kw in node.keywords)
                    sites.append((relpath, node.lineno, has_main))
        return sites

    def test_no_hold_site_decides_a_loop_from_launch_time_heads_alone(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sites = self._sites(root)
        offenders = [f'{relpath}:{lineno}' for relpath, lineno, has_main in sites if not has_main]
        self.assertEqual(offenders, [])
        self.assertEqual(len(sites), 9, sites)


class HoldInvariants(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def test_same_finding_climbs_to_the_cap_then_adjudicate_then_flag(self):
        # B-0048: the rounds counter never passes the cap; the second hold at the cap flags the
        # operator. The cap is counted on the SAME finding, each repeat answered by a session.
        for n in range(1, lc.ROUND_CAP + 1):
            run = {'job': f'j{n}', 'item': 'B-0001', 'branch': 'fix/B-0001', 'pid': n,
                   'started': f't{n}a', 'kind': 'coder' if n == 1 else 'correct'}
            self.write(run)
            fields, line = lc.hold(self.path, run, 'gate', 'FAIL', f't{n}b')
            self.assertEqual(fields['rounds'], n)
            self.assertEqual(fields['correction']['same'], n)
            if n < lc.ROUND_CAP:
                self.assertEqual(line, f'held fix/B-0001: FAIL — back to its session (round {n})')
            self.write(dict(fields, job=f'j{n}'))
        self.assertTrue(fields['correction']['at_cap'])
        self.assertNotIn('operator_flagged', fields)
        self.assertIn('adjudicate pending', line)
        self.assertEqual(lc.derive(lc.latest(self.path)[f'j{lc.ROUND_CAP}'], lc.Evidence()).name,
                         lc.ADJUDICATE)
        for n, flagged in ((1, None), (2, 1)):  # the ruling's attempt fails; then it flags
            run = {'job': f'adj{n}', 'item': 'B-0001', 'branch': 'fix/B-0001', 'pid': 9 + n,
                   'started': f'tx{n}a', 'kind': 'adjudicate'}
            self.write(run)
            fields, line = lc.hold(self.path, run, 'gate', 'FAIL', f'tx{n}b')
            self.write(dict(fields, job=f'adj{n}'))
            self.assertNotIn('rounds', fields)
            self.assertIn('adjudicate pending', line)
            self.assertEqual(fields.get('operator_flagged'), flagged)
        self.assertEqual(lc.rounds_of(self.path, 'B-0001'), lc.ROUND_CAP)

    def test_rounds_count_over_every_job_of_the_item(self):
        self.write({'job': 'a', 'item': 'B-0001', 'branch': 'b', 'pid': 1, 'started': 't1', 'rounds': 2},
                   {'job': 'c', 'item': 'B-0001', 'branch': 'b', 'pid': 2, 'started': 't2'})
        fields, _ = lc.hold(self.path, lc.latest(self.path)['c'], 'conflict', 'x', 't3')
        self.assertEqual(fields['rounds'], 3)


class SameFindingEscalation(unittest.TestCase):
    """Operator policy 2026-09-27: an item goes to ADJUDICATE only after CORRECT failed twice on
    the SAME finding (hold kind, review C-item, red check set) — a different finding resets it
    to CORRECT. A product's last 24 h: 100 of 245 repair sessions were adjudicate, most of them
    after three rounds of unrelated holds."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')
        self.n = 0

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def session(self, kind, hold_kind, text, finding=None):
        """Launch a ``kind`` session on T-0001 and hold it; return the hold's fields."""
        self.n += 1
        run = {'job': f'{kind}-{self.n}', 'item': 'T-0001', 'branch': 'task/T-0001',
               'pid': self.n, 'started': f't{self.n:02d}a', 'kind': kind}
        self.write(run)
        fields, _ = lc.hold(self.path, run, hold_kind, text, f't{self.n:02d}b', finding=finding)
        self.write(dict(fields, job=run['job']))
        return fields

    def pending(self):
        return lc.corrections(self.path)['T-0001']

    RED_A = 'PR #7 checks red: gate, gate-tests\nlint: 3 finding(s)'
    RED_B = 'PR #7 checks red: e2e'

    def test_two_corrects_on_the_same_finding_is_adjudicate(self):
        self.session('coder', 'gate', self.RED_A)
        self.session('correct', 'gate', 'PR #9 checks red: gate-tests, gate\nlint: 1 finding(s)')
        self.assertEqual(self.pending()['same'], 2)
        fields = self.session('correct', 'gate', self.RED_A)
        self.assertTrue(fields['correction']['at_cap'])
        self.assertEqual(self.pending()['same'], lc.ROUND_CAP)

    def test_a_correct_on_finding_a_then_b_is_still_correct(self):
        self.session('coder', 'gate', self.RED_A)
        self.session('correct', 'gate', self.RED_A)
        fields = self.session('correct', 'gate', self.RED_B)  # a new red check: reset
        self.assertNotIn('at_cap', fields['correction'])
        self.assertEqual(self.pending()['same'], 1)
        self.assertEqual(self.pending()['rounds'], 3)  # three rounds, mixed: still correct
        fields = self.session('correct', 'rebase conflict', 'rebase conflicts in: a.ts. Rebase')
        self.assertEqual(self.pending()['same'], 1)
        self.assertNotIn('at_cap', fields['correction'])

    def test_a_rehold_no_session_answered_counts_no_failure(self):
        self.session('coder', 'gate', self.RED_A)
        run = lc.latest(self.path)['coder-1']
        fields, _ = lc.hold(self.path, run, 'gate', self.RED_A, 't01c')
        self.assertEqual(fields['correction']['same'], 1)

    def test_an_adjudicate_run_answers_nothing(self):
        self.session('coder', 'gate', self.RED_A)
        self.session('adjudicate', 'gate', self.RED_A)
        self.assertEqual(self.pending()['same'], 1)

    def test_a_review_c_item_carried_over_is_the_same_finding(self):
        self.session('coder', 'review', 'r1 reads changes requested', finding=['a.ts', 'b.ts'])
        self.session('correct', 'review', 'r2 reads changes requested', finding=['b.ts', 'c.ts'])
        self.assertEqual(self.pending()['same'], 2)
        self.session('correct', 'review', 'r3 reads changes requested', finding=['a.ts', 'd.ts'])
        self.assertEqual(self.pending()['same'], 1)  # b.ts was fixed: a new finding

    def test_mechanical_holds_never_count(self):
        self.session('coder', 'gate', self.RED_A)
        fields = self.session('correct', lc.NAMING, 'commits do not name T-0001')
        self.assertNotIn('same', fields['correction'])

    def test_the_finding_of_a_hold(self):
        self.assertEqual(lc.finding_of('gate', self.RED_A), ['gate', 'gate-tests'])
        self.assertEqual(lc.finding_of('rebase conflict',
                                       "conflicted — rebase conflicts in: b/x.ts, a.md. Rebase"),
                         ['a.md', 'b/x.ts'])
        self.assertEqual(lc.finding_of('unpushed', 'not pushed: 1 unpushed commit(s) abc1234'),
                         lc.finding_of('unpushed', 'not pushed: 7 unpushed commit(s) 9f9f9f9'))
        self.assertEqual(lc.finding_of('review', 'x', ['b', 'a', 'b']), ['a', 'b'])


class HookRefusalEscalation(unittest.TestCase):
    """B-0140: a push the repo's own pre-push hook refuses is not tried a third time blind — the
    first refusal spends no round, but a second identical refusal in a row routes by what the
    hook said instead of asking a session to retry the same push it already failed."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')
        self.n = 0

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def hold(self, text, retried=False):
        self.n += 1
        run = {'job': f'fix-{self.n}', 'item': 'B-0140', 'branch': 'fix/B-0140',
               'pid': self.n, 'started': f't{self.n:02d}a', 'kind': 'fix-bug'}
        self.write(run)
        fields, line = lc.hook_refusal_hold(self.path, run, text, f't{self.n:02d}b',
                                            retried=retried)
        self.write(dict(fields, job=run['job']))
        return fields, line

    LINT = ("the push was refused by the repo's own hook — pre-push: lint failed on x.py:12 — "
            "fix what it names, commit, and push again")
    REDACT = ("the push was refused by the repo's own hook — redact: plan.md:3 names a worker "
              "account — fix what it names, commit, and push again")

    def test_the_first_refusal_spends_no_round(self):
        fields, line = self.hold(self.LINT)
        self.assertEqual(fields['correction']['same'], 1)
        self.assertNotIn('at_cap', fields['correction'])
        self.assertNotIn('parked', fields['correction'])
        self.assertIn('(no round spent, gate)', line)

    def test_the_correction_carries_the_hooks_own_tail(self):
        fields, _ = self.hold(self.LINT)
        self.assertIn('lint failed on x.py:12', fields['correction']['text'])

    def test_a_second_identical_refusal_goes_to_adjudicate(self):
        self.hold(self.LINT)
        fields, line = self.hold(self.LINT)
        self.assertEqual(fields['correction']['same'], 2)
        self.assertTrue(fields['correction']['at_cap'])
        self.assertIn('adjudicate pending', line)
        self.assertEqual(lc.derive(lc.latest(self.path)['fix-2'], lc.Evidence()).name,
                         lc.ADJUDICATE)

    def test_the_first_redaction_refusal_is_a_security_hold(self):
        # a product's correct-f-0086: 100 launches on one redaction finding, the "same" count
        # reset each time by a lane hold in between — the first refusal parks
        fields, line = self.hold(self.REDACT)
        self.assertTrue(fields['correction']['parked'])
        self.assertEqual(fields.get('operator_flagged'), 1)
        self.assertIn('security hold', line)

    def test_the_products_redaction_gate_wording_is_a_security_hold(self):
        fields, _ = self.hold("the push was refused by the repo's own hook — no — pre-push "
                              "redaction gate refuses (42 findings) on worker-lane labels")
        self.assertTrue(fields['correction']['parked'])

    def test_a_second_identical_redaction_refusal_is_a_security_hold(self):
        self.hold(self.REDACT)
        fields, line = self.hold(self.REDACT)
        self.assertTrue(fields['correction']['parked'])
        self.assertEqual(fields.get('operator_flagged'), 1)
        self.assertIn('security hold', line)

    def test_a_different_refusal_resets_the_count(self):
        self.hold(self.LINT)
        fields, _ = self.hold(self.REDACT)
        self.assertEqual(fields['correction']['same'], 1)
        self.assertNotIn('at_cap', fields['correction'])

    def test_a_retried_first_lint_hold_goes_straight_to_adjudicate(self):
        # F-0235: the session already said it retried in-run, so the first hold is the cap
        fields, line = self.hold(self.LINT, retried=True)
        self.assertEqual(fields['correction']['same'], 1)
        self.assertTrue(fields['correction']['at_cap'])
        self.assertIn('adjudicate pending', line)
        self.assertIs(fields['correction']['retried'], True)

    def test_a_retried_first_redaction_hold_is_still_a_security_hold(self):
        fields, line = self.hold(self.REDACT, retried=True)
        self.assertTrue(fields['correction']['parked'])
        self.assertEqual(fields.get('operator_flagged'), 1)
        self.assertIn('security hold', line)
        self.assertIs(fields['correction']['retried'], True)

    def test_an_unretried_correction_carries_no_retried_key_at_all(self):
        fields, _ = self.hold(self.LINT)
        self.assertNotIn('retried', fields['correction'])

    def test_the_cap_is_still_two_and_the_unretried_free_relaunch_stands(self):
        # D5: the `published` population's free first relaunch is not touched by F-0235
        self.assertEqual(lc.HOOK_REFUSAL_CAP, 2)
        fields, line = self.hold(self.LINT)
        self.assertNotIn('at_cap', fields['correction'])
        self.assertIn('(no round spent', line)

    def test_refusal_class_is_redaction_for_a_redact_text(self):
        fields, _ = self.hold(self.REDACT)
        self.assertEqual(fields['correction']['refusal'], 'redaction')

    def test_refusal_class_is_gate_for_a_lint_text(self):
        fields, _ = self.hold(self.LINT)
        self.assertEqual(fields['correction']['refusal'], 'gate')

    def test_refusal_class_holds_on_the_second_identical_redaction_refusal_too(self):
        self.hold(self.REDACT)
        fields, _ = self.hold(self.REDACT)
        self.assertEqual(fields['correction']['refusal'], 'redaction')

    def test_refusal_class_holds_on_the_second_identical_lint_refusal_too(self):
        self.hold(self.LINT)
        fields, _ = self.hold(self.LINT)
        self.assertEqual(fields['correction']['refusal'], 'gate')

    def test_the_three_refusal_classes_are_disjoint(self):
        fields, _ = self.hold(self.REDACT)
        self.assertNotEqual(fields['correction']['refusal'], 'gate')
        self.assertNotEqual(fields['correction']['refusal'], 'footprint')
        fields, _ = self.hold(self.LINT)
        self.assertNotEqual(fields['correction']['refusal'], 'redaction')
        self.assertNotEqual(fields['correction']['refusal'], 'footprint')


class EmptyEndsTests(unittest.TestCase):
    """F-0095 §2.3: an empty end is its own kind, it is counted, and the second one parks."""
    EMPTY_END = 'failed: ' + lc.EMPTY_BRANCH

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def run_of(self, job, n, end_reason=None, item='T-0001'):
        """Launch ``job`` and, given an ``end_reason``, end it; return its folded run."""
        self.write({'job': job, 'item': item, 'branch': 'worker/' + item, 'pid': n, 'started': 't%d' % n})
        if end_reason:
            self.write({'job': job, 'ended': 'e%d' % n, 'end_reason': end_reason})
        return lc.latest(self.path)[job]

    def test_it_counts_empty_ends_across_jobs_and_ignores_the_rest(self):
        self.run_of('a', 1, self.EMPTY_END)
        self.run_of('b', 2, 'failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)')
        self.run_of('c', 3, 'finished')
        self.run_of('d', 4)
        self.run_of('e', 5, self.EMPTY_END)
        self.run_of('f', 6, self.EMPTY_END, item='T-0002')
        self.assertEqual(lc.empty_ends(self.path, 'T-0001'), 2)
        self.assertEqual(lc.empty_ends(self.path, 'T-0002'), 1)
        self.assertEqual(lc.empty_ends(self.path, 'T-0003'), 0)
        self.assertEqual(lc.empty_ends(self.path, None), 0)

    def test_the_empty_and_unpushed_corrections_name_the_already_landed_commit(self):
        # a session whose item is already on the trunk needs a commit naming the id, not a report
        for text in (lc.empty_branch_text(),
                     lc.unpushed_text('failed: not pushed: 0 uncommitted file(s), 0 unpushed commit(s)')):
            self.assertIn('commit and push what you have', text)
            self.assertIn('--allow-empty', text)
            self.assertIn('already landed in <sha>', text)

    def test_the_first_empty_end_is_an_ordinary_hold(self):
        run = self.run_of('a', 1, self.EMPTY_END)
        fields, line = lc.hold(self.path, run, lc.EMPTY, lc.empty_branch_text(), 'tn')
        self.assertEqual(fields['rounds'], 1)
        self.assertNotIn('parked', fields['correction'])
        self.assertNotIn('operator_flagged', fields)
        self.assertEqual(fields['correction']['kind'], lc.EMPTY)
        self.assertEqual(line, 'held worker/T-0001: %s — back to its session (round 1)'
                         % lc.empty_branch_text())

    def test_the_second_empty_end_parks_and_spends_no_round(self):
        first = self.run_of('a', 1, self.EMPTY_END)
        fields, _ = lc.hold(self.path, first, lc.EMPTY, 'x', 't1')
        self.write(dict(fields, job='a'))
        second = self.run_of('b', 2, self.EMPTY_END)
        fields, line = lc.hold(self.path, second, lc.EMPTY, 'x', 't2')
        corr = fields['correction']
        self.assertNotIn('rounds', fields)
        self.assertIs(corr['parked'], True)
        self.assertEqual(corr['kind'], lc.EMPTY)
        self.assertIn('2 times', corr['reason'])
        self.assertIn('asf unpark', corr['reason'])
        self.assertEqual(fields['operator_flagged'], 1)
        self.assertEqual(line, 'parked worker/T-0001: ' + corr['reason'])
        self.write(dict(fields, job='b'))
        self.assertEqual(lc.rounds_of(self.path, 'T-0001'), 1)

    def test_the_cap_is_a_parameter(self):
        self.run_of('a', 1, self.EMPTY_END)
        second = self.run_of('b', 2, self.EMPTY_END)
        fields, _ = lc.hold(self.path, second, lc.EMPTY, 'x', 't2', empty_cap=3)
        self.assertNotIn('parked', fields['correction'])
        self.assertEqual(fields['rounds'], 1)
        third = self.run_of('c', 3, self.EMPTY_END)
        fields, _ = lc.hold(self.path, third, lc.EMPTY, 'x', 't3', empty_cap=3)
        self.assertIs(fields['correction']['parked'], True)
        self.assertIn('3 times', fields['correction']['reason'])

    def test_a_not_pushed_hold_still_spends_rounds_and_never_parks(self):
        self.run_of('a', 1, self.EMPTY_END)
        second = self.run_of('b', 2, self.EMPTY_END)
        fields, _ = lc.hold(self.path, second, lc.UNPUSHED, 'x', 't2')
        self.assertEqual(fields['rounds'], 1)
        self.assertNotIn('parked', fields['correction'])
        self.assertNotIn('operator_flagged', fields)

    def test_derive_over_a_parked_run_is_held_not_adjudicate(self):
        self.run_of('a', 1, self.EMPTY_END)
        second = self.run_of('b', 2, self.EMPTY_END)
        fields, _ = lc.hold(self.path, second, lc.EMPTY, 'x', 't2')
        self.write(dict(fields, job='b', rounds=1))
        run = lc.latest(self.path)['b']
        self.assertEqual(lc.derive(run, lc.Evidence(), path=self.path).name, lc.HELD)


class BlockedParkTests(unittest.TestCase):
    """F-0126 §5: a run that ended with nothing to land while its own report declared a
    question for a person is parked, not handed to another session — until its card changes."""

    def product(self):
        return env.Product('sample', {'backlog_dir': '/nonexistent'})

    def test_blocked_park_returns_the_six_correction_keys(self):
        fields, line = lc.blocked_park('why is this stuck?', 'T-0001', 'deadbeef01234567', 't1')
        corr = fields['correction']
        self.assertEqual(set(corr), {'kind', 'text', 'at', 'parked', 'reason', 'card'})
        self.assertEqual(corr['kind'], lc.BLOCKED)
        self.assertEqual(corr['text'], 'why is this stuck?')
        self.assertEqual(corr['at'], 't1')
        self.assertIs(corr['parked'], True)
        self.assertEqual(corr['card'], 'deadbeef01234567')
        self.assertEqual(fields['operator_flagged'], 1)
        self.assertEqual(line, f'parked T-0001: {corr["reason"]}')

    def test_the_text_quotes_the_question_and_names_the_way_out(self):
        fields, _ = lc.blocked_park('why is this stuck?', 'T-0001', 'card1', 't1')
        reason = fields['correction']['reason']
        self.assertIn('why is this stuck?', reason)
        self.assertIn('asf unpark T-0001', reason)
        self.assertIn('RESHAPE → PLAN', reason)

    def test_a_probe_is_attached_to_the_correction_and_named_in_the_reason(self):
        # B-0042: a read-only command harvest ran itself over the NEEDS OPERATOR question is
        # attached to the park — the operator opens it already answered.
        probe = {'command': 'git status --short', 'output': 'M a.py'}
        fields, line = lc.blocked_park('check `git status --short`', 'T-0001', 'card1', 't1',
                                       probe=probe)
        self.assertEqual(fields['correction']['probe'], probe)
        self.assertIn('git status --short', fields['correction']['reason'])
        self.assertIn('git status --short', line)

    def test_no_probe_is_the_same_six_keys_as_before(self):
        fields, _ = lc.blocked_park('why is this stuck?', 'T-0001', 'card1', 't1', probe=None)
        self.assertEqual(set(fields['correction']), {'kind', 'text', 'at', 'parked', 'reason', 'card'})

    def test_card_fingerprint_is_stable_and_reacts_to_writes_and_after(self):
        product = self.product()
        card = {'id': 'T-0001', 'title': 'a task', 'writes': ['a.py'], 'after': 'none'}
        items = {'T-0001': card}
        first = lc.card_fingerprint(product, 'T-0001', items)
        self.assertTrue(first)
        self.assertEqual(lc.card_fingerprint(product, 'T-0001', items), first)
        widened = {'T-0001': dict(card, writes=['a.py', 'b.py'])}
        self.assertNotEqual(lc.card_fingerprint(product, 'T-0001', widened), first)
        reordered = {'T-0001': dict(card, after='T-0000')}
        self.assertNotEqual(lc.card_fingerprint(product, 'T-0001', reordered), first)

    def test_card_fingerprint_ignores_state_evidence_stage_since_and_updated(self):
        product = self.product()
        card = {'id': 'T-0001', 'title': 'a task', 'writes': ['a.py'], 'state': 'New',
                'evidence': ['x'], 'stage_since': 't0', 'updated': 't0'}
        items = {'T-0001': card}
        before = lc.card_fingerprint(product, 'T-0001', items)
        moved = {'T-0001': dict(card, state='Active', evidence=['x', 'y'], stage_since='t1',
                                updated='t1')}
        self.assertEqual(lc.card_fingerprint(product, 'T-0001', moved), before)

    def test_card_fingerprint_is_empty_for_none_items_or_no_id(self):
        product = self.product()
        items = {'T-0001': {'id': 'T-0001'}}
        self.assertEqual(lc.card_fingerprint(product, 'T-0001', None), '')
        self.assertEqual(lc.card_fingerprint(product, '', items), '')
        self.assertEqual(lc.card_fingerprint(product, None, items), '')
        self.assertEqual(lc.card_fingerprint(product, 'T-9999', items), '')


class PublishedShaIsTheFactorysOwnPush(unittest.TestCase):
    """F-0217: :func:`lc.published_sha` is the pure helper behind what a health pass folds onto
    a run's ledger line — the branch's ``ls-remote`` sha, never :func:`lc.publish`'s own
    nine-character ``at <head>``, and never on a refusal or a head that did not move."""

    def test_a_new_sha_after_an_empty_before_is_the_new_sha(self):
        self.assertEqual(lc.published_sha('', 'b733' * 10, 'published worker/X at b733'),
                          'b733' * 10)

    def test_a_branch_already_on_origin_that_moved_gives_the_new_sha(self):
        self.assertEqual(lc.published_sha('a' * 40, 'b' * 40, 'published worker/X at abcdef0'),
                          'b' * 40)

    def test_before_equal_to_after_gives_nothing(self):
        self.assertEqual(lc.published_sha('a' * 40, 'a' * 40, 'published worker/X at abcdef0'), '')

    def test_a_refusal_line_gives_nothing(self):
        self.assertEqual(lc.published_sha('a' * 40, 'b' * 40,
                                          'publish worker/X refused: redact: a.txt:1'), '')

    def test_an_empty_after_gives_nothing(self):
        self.assertEqual(lc.published_sha('a' * 40, '', 'published worker/X at abcdef0'), '')

    def test_an_empty_line_gives_nothing(self):
        self.assertEqual(lc.published_sha('a' * 40, 'b' * 40, ''), '')


class SameHeadLoopGuard(unittest.TestCase):
    """A product, 2026-09-26: ``adjudicate-b-1377`` launched 14 times and ``correct-t-0338`` 12
    times, each on a head no session had moved. The same kind handed the same head
    :data:`lc.LOOP_CAP` times in a row parks the item with the reason in the row, instead of
    burning a fourth session."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def run_of(self, n, kind='correct', head='a' * 40, item='T-0001', published=None):
        job = f'{kind}-{item.lower()}'
        ln = {'job': job, 'item': item, 'branch': 'worker/' + item, 'kind': kind, 'pid': n,
              'started': 't%02d' % n}
        if head:
            ln['launch_head'] = head
        self.write(ln, {'job': job, 'ended': 'e%02d' % n,
                        'end_reason': 'failed: not pushed: 0 uncommitted file(s), 2 unpushed commit(s)'})
        if published is not None:
            self.write({'job': job, 'published_head': published})
        return lc.latest(self.path)[job]

    def test_the_third_launch_on_one_head_parks_the_item(self):
        for n in (1, 2):
            fields, line = lc.hold(self.path, self.run_of(n), 'rebase conflict', 'x', 't%02dz' % n)
            self.assertNotIn('parked', fields['correction'], line)
            self.write(dict(fields, job='correct-t-0001'))
        fields, line = lc.hold(self.path, self.run_of(3), 'rebase conflict', 'x', 't03z')
        corr = fields['correction']
        self.assertIs(corr['parked'], True)
        self.assertEqual(fields['operator_flagged'], 1)
        self.assertIn('correct launched 3 times on aaaaaaaaa', corr['reason'])
        self.assertIn('asf unpark T-0001', corr['reason'])
        self.assertEqual(line, 'parked worker/T-0001: ' + corr['reason'])
        self.write(dict(fields, job='correct-t-0001'))
        occ = lc.corrections(self.path)['T-0001']
        self.assertTrue(occ['parked'])
        from asf.feeder import rows as feeder_rows
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active'}}
        product = mock.Mock()
        product.conventions.branch_kind.return_value = 'code'
        rows, _ = feeder_rows.correction_rows(items, product, set(), {'T-0001': occ})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].action.startswith(feeder_rows.PARKED), rows[0].action)
        self.assertFalse(rows[0].launches)

    def test_a_naming_hold_is_no_exemption(self):
        for n in (1, 2):
            self.run_of(n)
        fields, _ = lc.hold(self.path, self.run_of(3), lc.NAMING, 'x', 'h3')
        self.assertIs(fields['correction']['parked'], True)

    def test_a_moved_head_a_known_other_head_another_kind_or_no_record_does_not_park(self):
        cases = {'moved': [dict(head='a' * 40), dict(head='b' * 40), dict(head='b' * 40)],
                 'kinds': [dict(kind='correct'), dict(kind='adjudicate'), dict(kind='adjudicate')],
                 'unrecorded': [dict(head=None), dict(), dict()],
                 'two': [dict(), dict()]}
        for name, runs in cases.items():
            with self.subTest(name):
                self.setUp()
                for n, kw in enumerate(runs, 1):
                    last = self.run_of(n, **kw)
                fields, _ = lc.hold(self.path, last, 'review', 'x', 'h')
                self.assertNotIn('parked', fields['correction'])
        self.setUp()
        for n in (1, 2, 3):
            last = self.run_of(n)
        fields, _ = lc.hold(self.path, last, 'review', 'x', 'h', head='c' * 40)
        self.assertNotIn('parked', fields['correction'])  # the last session pushed: not a loop
        fields, _ = lc.hold(self.path, last, 'review', 'x', 'h', head='a' * 40)
        self.assertIs(fields['correction']['parked'], True)

    def test_a_published_head_different_from_the_launch_head_is_not_a_loop(self):
        # F-0217/C1: a factory publish recorded on the ledger moved the branch, with no
        # worktree left to read — the record is the witness, and no `head`/`main` is given
        for n in (1, 2):
            self.run_of(n)
        launch_head = 'a' * 40
        published_head = 'b' * 40
        self.assertEqual(len(published_head), len(launch_head))  # PD2: width agreement
        last = self.run_of(3, published=published_head)
        fields, _ = lc.hold(self.path, last, 'review', 'x', 'h')
        self.assertNotIn('parked', fields['correction'])
        self.assertNotIn('operator_flagged', fields)

    def test_a_published_head_equal_to_the_launch_head_still_parks(self):
        # C4: a comparison, not a presence test — a publish that pushed the very head the
        # launches were read on moved nothing
        for n in (1, 2):
            self.run_of(n)
        last = self.run_of(3, published='a' * 40)
        fields, _ = lc.hold(self.path, last, 'review', 'x', 'h')
        self.assertIs(fields['correction']['parked'], True)


class AwaitingHarvestInvariants(unittest.TestCase):
    def test_a_pushed_branch_holds_its_item_busy_until_harvest_lands_or_holds_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 's.jsonl')

        def write(rec):
            with open(path, 'a') as f:
                f.write(json.dumps(rec) + '\n')
        write({'job': 'a', 'item': 'B-0001', 'branch': 'fix/B-0001', 'pid': 1, 'started': 't1'})
        self.assertEqual(lc.awaiting_harvest(path), set())            # live: a session holds it
        write({'job': 'a', 'ended': 't2', 'end_reason': 'finished'})
        self.assertEqual(lc.awaiting_harvest(path), {'B-0001'})       # pushed: harvest's turn
        write({'job': 'a', 'rounds': 1, 'correction': {'kind': 'gate', 'text': 'x', 'at': 't3'}})
        self.assertEqual(lc.awaiting_harvest(path), set())            # held: the feeder's turn
        write({'job': 'c', 'item': 'B-0001', 'branch': 'fix/B-0001', 'pid': 2, 'started': 't4',
               'ended': 't5', 'end_reason': 'finished'})
        self.assertEqual(lc.awaiting_harvest(path), {'B-0001'})
        write({'job': 'c', 'harvested': 'sha'})
        self.assertEqual(lc.awaiting_harvest(path), set())            # landed


class LaunchAndReapInvariants(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')
        self.wt = os.path.join(self.d, 'wt', 'fix-b-0001')
        os.makedirs(self.wt)

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def test_a_launch_is_refused_only_on_a_live_worktree(self):
        # B-0025 / B-0051: an ended run's worktree is reused; an orphan is refused
        self.assertEqual(lc.may_launch(self.path, 'fix-b-0001', self.wt), (False, f'worktree already exists: {self.wt} — no run recorded it'))
        self.write({'job': 'fix-b-0001', 'pid': 1, 'started': 't1', 'worktree': self.wt})
        self.assertFalse(lc.may_launch(self.path, 'fix-b-0001', self.wt)[0])
        self.write({'job': 'fix-b-0001', 'ended': 't2', 'end_reason': 'failed: not pushed: 3 uncommitted file(s), 0 unpushed commit(s)'})
        self.assertEqual(lc.may_launch(self.path, 'fix-b-0001', self.wt), (True, ''))
        # another job may take over the ended run's worktree (a correction on the same branch)
        self.assertEqual(lc.may_launch(self.path, 'correct-b-0001', self.wt), (True, ''))
        self.write({'job': 'correct-b-0001', 'pid': os.getpid(), 'started': 't3', 'worktree': self.wt})
        # …and while it runs, nobody else does — by worktree, not by directory name
        self.assertFalse(lc.may_launch(self.path, 'fix-b-0001', self.wt)[0])
        self.assertTrue(lc.may_launch(self.path, 'other', os.path.join(self.d, 'wt', 'other'))[0])

    def _other_case(self, p):
        """``p`` spelled with the case of its home segment swapped, as ``~/.ASF`` vs ``~/.asf``."""
        head, tail = os.path.split(self.d)
        return os.path.join(head, tail.swapcase()) + p[len(self.d):]

    def test_an_ended_runs_worktree_under_a_differently_cased_home_is_reused(self):
        other = self._other_case(self.wt)
        if not os.path.exists(other):
            self.skipTest('case-sensitive filesystem: the two spellings are two directories')
        dead = lambda pid: False
        # the coder run recorded the worktree as ~/.ASF/…, ended unpushed; the correction ran
        # in it under another job name, and its pid is gone
        self.write({'job': 'coder-t-0133', 'pid': 1, 'started': 't1', 'worktree': self.wt},
                   {'job': 'coder-t-0133', 'ended': 't2', 'end_reason': 'failed: unpushed work'},
                   {'job': 'correct-t-0133', 'pid': 2, 'started': 't3', 'worktree': self.wt})
        self.assertEqual(lc.path_key(other), lc.path_key(self.wt))
        self.assertEqual(lc.may_launch(self.path, 'correct-t-0133', other, alive=dead), (True, ''))
        self.assertEqual(lc.may_launch(self.path, 'coder-t-0133', other, alive=dead), (True, ''))

    def test_a_live_run_in_a_differently_cased_worktree_refuses_every_other_job(self):
        other = self._other_case(self.wt)
        if not os.path.exists(other):
            self.skipTest('case-sensitive filesystem: the two spellings are two directories')
        alive = lambda pid: True
        self.write({'job': 'coder-t-0133', 'pid': 1, 'started': 't1', 'worktree': self.wt},
                   {'job': 'coder-t-0133', 'ended': 't2', 'end_reason': 'failed: unpushed work'},
                   {'job': 'correct-t-0133', 'pid': 2, 'started': 't3', 'worktree': self.wt})
        # before: by realpath the ~/.asf spelling missed the correction's record and fell back to
        # coder-t-0133's ended run — a second session into a live worktree
        what, why = lc.launch_verdict(self.path, 'coder-t-0133', other, alive=alive)
        self.assertEqual(what, lc.BUSY)
        self.assertIn('held by live run correct-t-0133 (pid 2)', why)

    def test_an_orphan_is_refused_under_either_spelling(self):
        self.write({'job': 'somebody-else', 'pid': 1, 'started': 't1',
                    'worktree': os.path.join(self.d, 'wt', 'elsewhere')})
        for p in {self.wt, self._other_case(self.wt)}:
            if not os.path.exists(p):
                continue
            what, why = lc.launch_verdict(self.path, 'fix-b-0001', p)
            self.assertEqual(what, lc.ORPHAN)
            self.assertIn('no run recorded it', why)

    def test_reap_on_landed_or_empty_and_only_then(self):
        dead = lambda pid: False
        # B-0049: landed is the evidence, whatever the worktree's tip says
        landed = {'job': 'j', 'ended': 't', 'end_reason': 'finished', 'harvested': 'sha', 'pid': 1}
        for ev in evidences():
            self.assertEqual(lc.reap_verdict(landed, ev, 'main', dead), ('reapable', 'landed sha'))
        # B-0025: ended, not finished, nothing to lose
        failed = {'job': 'j', 'ended': 't', 'end_reason': 'dead pid', 'pid': 1}
        self.assertEqual(lc.reap_verdict(failed, lc.Evidence(in_trunk=True), 'main', dead), ('reapable', 'empty'))
        self.assertEqual(lc.reap_verdict(failed, lc.Evidence(in_trunk=True, uncommitted=1), 'main', dead),
                         ('keep', 'ended: session dead pid, not finished'))
        self.assertEqual(lc.reap_verdict(failed, lc.Evidence(in_trunk=False), 'main', dead)[0], 'keep')
        # a live run is never reapable, whatever the evidence
        live = {'job': 'j', 'pid': 1, 'started': 't'}
        for ev in evidences():
            self.assertNotEqual(lc.reap_verdict(live, ev, 'main', dead)[0], 'reapable')
        # an alive pid is never reapable either
        for ev in evidences():
            self.assertEqual(lc.reap_verdict(landed, ev, 'main', lambda pid: True)[0], 'keep')
        # B-0019/F-0201: finished, with commits of its own, and the trunk holds them — whatever
        # origin still has of the branch. The two origin-shaped rows are the landing path: the
        # lane lands a rebased or squashed tip and deletes the branch, so this tree's HEAD may
        # appear on no origin branch at all and its own row may carry no landing.
        fin = {'job': 'j', 'ended': 't', 'end_reason': 'finished', 'pid': 1}
        full = lc.Evidence(remote_sha='s', head_on_remote=True, has_commits=True, in_trunk=True)
        self.assertEqual(lc.reap_verdict(fin, full, 'main', dead), ('reapable', 'ended'))
        for short in (dict(remote_sha=''), dict(head_on_remote=False)):
            ev = lc.Evidence(**dict(dataclassfields(full), **short))
            self.assertEqual(lc.reap_verdict(fin, ev, 'main', dead), ('reapable', 'ended'), short)
        for short, why in ((dict(has_commits=False), 'ended: no commits yet'),
                           (dict(in_trunk=False), 'ended: not in origin/main'),
                           (dict(uncommitted=1), 'ended: uncommitted changes')):
            ev = lc.Evidence(**dict(dataclassfields(full), **short))
            self.assertEqual(lc.reap_verdict(fin, ev, 'main', dead), ('keep', why), short)


def dataclassfields(ev):
    return {f: getattr(ev, f) for f in ev.__dataclass_fields__}


class ALandingOnTheBranch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def test_branch_landings_over_a_written_registry(self):
        # a branch whose latest run carries `harvested` maps to that sha
        self.write({'job': 'j1', 'branch': 'plan/F-0201', 'started': 't1', 'pid': 1},
                   {'job': 'j1', 'ended': 't2', 'end_reason': 'finished', 'harvested': 'sha1'})
        # an earlier run on this branch landed; its later run has not — absent (C3, PD6)
        self.write({'job': 'j2', 'branch': 'fix/B-0001', 'started': 't1', 'pid': 1},
                   {'job': 'j2', 'ended': 't2', 'end_reason': 'finished', 'harvested': 'sha2'},
                   {'job': 'j3', 'branch': 'fix/B-0001', 'started': 't3', 'pid': 2},
                   {'job': 'j3', 'ended': 't4', 'end_reason': 'finished'})
        # `harvested: 'superseded'` is no landing (C5)
        self.write({'job': 'j4', 'branch': 'fix/B-0002', 'started': 't1', 'pid': 1},
                   {'job': 'j4', 'ended': 't2', 'end_reason': 'finished', 'harvested': 'superseded'})
        # nobody landed this branch
        self.write({'job': 'j5', 'branch': 'fix/B-0003', 'started': 't1', 'pid': 1},
                   {'job': 'j5', 'ended': 't2', 'end_reason': 'dead pid'})
        # a run with no branch contributes nothing
        self.write({'job': 'j6', 'started': 't1', 'pid': 1},
                   {'job': 'j6', 'ended': 't2', 'end_reason': 'finished', 'harvested': 'sha6'})
        self.assertEqual(lc.branch_landings(self.path), {'plan/F-0201': 'sha1'})

    def test_the_in_trunk_ground(self):
        dead = lambda pid: False
        fin = {'job': 'j', 'ended': 't', 'end_reason': 'finished', 'pid': 1}
        clean = lc.Evidence(remote_sha='', head_on_remote=False, has_commits=True, in_trunk=True)
        self.assertEqual(lc.reap_verdict(fin, clean, 'main', dead), ('reapable', 'ended'))
        dirty = lc.Evidence(**dict(dataclassfields(clean), uncommitted=1))
        self.assertEqual(lc.reap_verdict(fin, dirty, 'main', dead), ('keep', 'ended: uncommitted changes'))
        fresh = lc.Evidence(**dict(dataclassfields(clean), has_commits=False))
        self.assertEqual(lc.reap_verdict(fin, fresh, 'main', dead), ('keep', 'ended: branch not pushed'))

    def test_the_branch_landed_ground(self):
        dead = lambda pid: False
        fin = {'job': 'j', 'ended': 't', 'end_reason': 'finished', 'pid': 1}
        ev = lc.Evidence(has_commits=True, in_trunk=False)
        self.assertEqual(lc.reap_verdict(fin, ev, 'main', dead, branch_landed='4d2e1b9c0'),
                         ('reapable', 'landed 4d2e1b9c0 on its branch'))
        self.assertEqual(lc.reap_verdict(fin, ev, 'main', dead, branch_landed=''),
                         ('keep', 'ended: branch not pushed'))
        pushed = lc.Evidence(**dict(dataclassfields(ev), remote_sha='s', head_on_remote=True))
        self.assertEqual(lc.reap_verdict(fin, pushed, 'main', dead, branch_landed=''),
                         ('keep', 'ended: not in origin/main'))
        dirty = lc.Evidence(**dict(dataclassfields(ev), uncommitted=1))
        self.assertEqual(lc.reap_verdict(fin, dirty, 'main', dead, branch_landed='4d2e1b9c0'),
                         ('keep', 'ended: uncommitted changes'))

    def test_the_guards_hold_under_any_branch_landed(self):
        dead = lambda pid: False
        live = {'job': 'j', 'pid': 1, 'started': 't'}
        for ev in evidences():
            self.assertNotEqual(lc.reap_verdict(live, ev, 'main', dead, branch_landed='sha')[0], 'reapable')
        fin = {'job': 'j', 'ended': 't', 'end_reason': 'finished', 'pid': 1}
        for ev in evidences():
            self.assertEqual(lc.reap_verdict(fin, ev, 'main', lambda pid: True, branch_landed='sha')[0], 'keep')

    def test_the_orphan_strings_are_unchanged(self):
        dead = lambda pid: False
        self.assertEqual(lc.reap_verdict(None, lc.Evidence(), 'main', dead),
                         ('keep', 'orphan: branch not pushed'))
        self.assertEqual(lc.reap_verdict(None, lc.Evidence(remote_sha='s'), 'main', dead),
                         ('keep', 'orphan: no commits yet'))
        self.assertEqual(lc.reap_verdict(None, lc.Evidence(has_commits=True, in_trunk=True), 'main', dead),
                         ('reapable', 'orphan'))


class UnpushedAfterARebaseTest(unittest.TestCase):
    """B-0053: harvest rebases a branch onto the trunk after the session pushed it. Counting
    ``origin/<branch>..HEAD`` by sha then calls the trunk's own commits this session's unpushed
    work and its already-pushed commit missing — and no push a session may make clears it (a
    plain push is not a fast-forward; a force is forbidden), so the branch is held every round.
    The count is by patch and above the trunk instead."""

    def sh(self, cmd, cwd):
        env = {**os.environ}
        for var in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            env.pop(var, None)
        r = subprocess.run(['git', *cmd], cwd=cwd, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, f"git {cmd}:\n{r.stdout}\n{r.stderr}")
        return r.stdout.strip()

    def commit(self, name, text):
        with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
            f.write(text)
        self.sh(['add', '-A'], self.repo)
        self.sh(['commit', '-qm', text], self.repo)
        return self.sh(['rev-parse', 'HEAD'], self.repo)

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_rebase_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, self.repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], self.repo)
        self.commit('seed', 'seed')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        # the session's branch, committed and pushed — this is the state harvest picks up
        self.sh(['checkout', '-q', '-b', 'fix/B-9999'], self.repo)
        self.fix_sha = self.commit('fix', 'the fix')
        self.sh(['push', '-q', 'origin', 'fix/B-9999'], self.repo)
        self.remote_sha = self.sh(['rev-parse', 'origin/fix/B-9999'], self.repo)
        # the trunk moves on under it, then harvest rebases the branch onto the new trunk
        self.sh(['checkout', '-q', 'main'], self.repo)
        self.commit('trunk', 'trunk moved')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sh(['checkout', '-q', 'fix/B-9999'], self.repo)
        self.sh(['rebase', '-q', 'origin/main'], self.repo)

    def test_the_rebase_gave_the_fix_a_new_sha_but_the_same_patch(self):
        self.assertNotEqual(self.sh(['rev-parse', 'HEAD'], self.repo), self.fix_sha)
        self.assertEqual(int(self.sh(['rev-list', '--count', f'{self.remote_sha}..HEAD'],
                                     self.repo)), 2, 'the raw sha count that caused B-0053')

    def test_a_rebased_branch_whose_work_is_on_origin_is_not_unpushed(self):
        self.assertEqual(lc.unpushed_commits(self.repo, self.remote_sha, 'main'), 0)

    def test_work_committed_after_the_rebase_is_still_counted(self):
        self.commit('more', 'new work the remote has never seen')
        self.assertEqual(lc.unpushed_commits(self.repo, self.remote_sha, 'main'), 1)

    def test_b0056_the_factory_publishes_the_rebased_branch_under_a_lease(self):
        # no push a session may make brings a rebased branch to origin; the factory's does
        ok, line = lc.publish(self.repo, 'fix/B-9999', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.sh(['rev-parse', 'HEAD'], self.repo),
                         self.sh(['ls-remote', '--heads', 'origin', 'fix/B-9999'], self.repo).split()[0])
        self.assertIn('published fix/B-9999 at', line)
        self.assertIn('lease held', line)

    def test_b0056_a_lease_that_moved_is_refused_and_nothing_is_overwritten(self):
        # origin moved after the evidence was gathered: the push is refused, the branch stays
        other = os.path.join(os.path.dirname(self.repo), 'other')
        self.sh(['clone', '-q', '-b', 'fix/B-9999', self.sh(['remote', 'get-url', 'origin'], self.repo), other],
                os.path.dirname(self.repo))
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com')):
            self.sh(['config', k, v], other)
        with open(os.path.join(other, 'late'), 'w') as f:
            f.write('late')
        self.sh(['add', '-A'], other)
        self.sh(['commit', '-qm', 'late work on origin'], other)
        self.sh(['push', '-q', 'origin', 'fix/B-9999'], other)
        moved = self.sh(['rev-parse', 'HEAD'], other)
        ok, line = lc.publish(self.repo, 'fix/B-9999', self.remote_sha, main='main')
        self.assertFalse(ok)
        self.assertTrue(line.startswith('publish fix/B-9999 refused:'), line)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', 'fix/B-9999'], self.repo).split()[0], moved)

    def test_b0056_publish_never_targets_the_trunk(self):
        ok, line = lc.publish(self.repo, 'main', '', main='main')
        self.assertFalse(ok)
        self.assertIn('not a lane branch', line)

    def test_b0056_a_branch_not_on_origin_is_published_plainly(self):
        self.sh(['checkout', '-q', '-b', 'fix/B-9998'], self.repo)
        self.commit('new', 'new work')
        ok, line = lc.publish(self.repo, 'fix/B-9998', '', main='main')
        self.assertTrue(ok, line)
        self.assertEqual(line, 'published fix/B-9998 at ' + self.sh(['rev-parse', '--short', 'HEAD'], self.repo))

    def test_b0097_a_network_blip_is_retried_in_place_with_backoff_and_then_succeeds(self):
        # B-0097: a DNS blip on the branch's own push is retried right here, with backoff,
        # rather than failing the tick on the first attempt
        self.sh(['checkout', '-q', '-b', 'fix/B-9997'], self.repo)
        self.commit('new', 'new work')
        blip = subprocess.CompletedProcess([], 1, '',
            "fatal: unable to access 'url': Could not resolve host: github.com\n")
        calls, slept = [], []
        real_push = gitpush.push

        def flaky(args, wt, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                return blip
            return real_push(args, wt, **kwargs)

        with mock.patch('asf.gitpush.push', side_effect=flaky):
            ok, line = lc.publish(self.repo, 'fix/B-9997', '', main='main', sleep=slept.append)
        self.assertTrue(ok, line)
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, list(lc.PUBLISH_RETRY_BACKOFF_S))

    def test_b0097_a_hook_refusal_is_never_retried(self):
        # a non-network refusal (the hook, here) returns on its first try: retrying it would
        # just ask the hook the same question again
        self.sh(['checkout', '-q', '-b', 'fix/B-9996'], self.repo)
        self.commit('new', 'new work')
        refused = subprocess.CompletedProcess([], 1, '', 'remote rejected (pre-push hook declined)\n')
        calls, slept = [], []

        def hooked(args, wt, **kwargs):
            calls.append(1)
            return refused

        with mock.patch('asf.gitpush.push', side_effect=hooked):
            ok, line = lc.publish(self.repo, 'fix/B-9996', '', main='main', sleep=slept.append)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(slept, [])

    def test_b0097_a_network_blip_that_outlasts_the_backoff_falls_through(self):
        # exhausted, the refusal is returned as any other — the next tick's health pass
        # (health.push_retry) is what retries it from here
        self.sh(['checkout', '-q', '-b', 'fix/B-9995'], self.repo)
        self.commit('new', 'new work')
        blip = subprocess.CompletedProcess([], 1, '',
            "fatal: unable to access 'url': Could not resolve host: github.com\n")
        calls, slept = [], []

        def always_blips(args, wt, **kwargs):
            calls.append(1)
            return blip

        with mock.patch('asf.gitpush.push', side_effect=always_blips):
            ok, line = lc.publish(self.repo, 'fix/B-9995', '', main='main', sleep=slept.append)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1 + len(lc.PUBLISH_RETRY_BACKOFF_S))
        self.assertEqual(slept, list(lc.PUBLISH_RETRY_BACKOFF_S))
        self.assertEqual(lc.push_failure(line.split('refused: ', 1)[1]), lc.NETWORK_ERROR)

    def _push_from_elsewhere(self, subject):
        """A person pushes ``subject`` onto ``origin/fix/B-9999`` from another clone; this repo
        never fetches it. Returns the new remote head."""
        other = os.path.join(os.path.dirname(self.repo), 'person')
        if not os.path.isdir(other):
            self.sh(['clone', '-q', '-b', 'fix/B-9999',
                     self.sh(['remote', 'get-url', 'origin'], self.repo), other],
                    os.path.dirname(self.repo))
            for k, v in (('user.name', 'Person'), ('user.email', 'p@example.com')):
                self.sh(['config', k, v], other)
        with open(os.path.join(other, subject), 'w') as f:
            f.write(subject)
        self.sh(['add', '-A'], other)
        self.sh(['commit', '-qm', subject], other)
        self.sh(['push', '-q', 'origin', 'fix/B-9999'], other)
        return self.sh(['rev-parse', 'HEAD'], other)

    def _remote(self):
        return self.sh(['ls-remote', '--heads', 'origin', 'fix/B-9999'], self.repo).split()[0]

    def _on_remote(self, sha):
        return subprocess.run(['git', 'merge-base', '--is-ancestor', sha, self._remote()],
                              cwd=self.repo).returncode == 0

    def test_a_stale_worktree_never_overwrites_newer_remote_commits(self):
        # 2026-09-25: a worktree at an older head published over a person's newer commit. The
        # factory now rebases onto the remote head first: the newer commit survives.
        self.sh(['reset', '-q', '--hard', self.fix_sha], self.repo)  # the stale worktree
        newer = self._push_from_elsewhere('newer-work')
        ok, line = lc.publish(self.repo, 'fix/B-9999', newer, main='main')
        self.assertTrue(ok, line)
        self.assertIn('rebased onto origin/fix/B-9999 (+1 remote commits) and pushed', line)
        self.assertTrue(self._on_remote(newer))

    def test_a_stale_dirty_worktree_is_refused_and_never_rebased(self):
        self.sh(['reset', '-q', '--hard', self.fix_sha], self.repo)
        newer = self._push_from_elsewhere('newer-work')
        with open(os.path.join(self.repo, 'dirty'), 'w') as f:
            f.write('dirty')
        ok, line = lc.publish(self.repo, 'fix/B-9999', newer, main='main')
        self.assertFalse(ok, line)
        self.assertIn('would lose 1 commit', line)
        self.assertIn(newer[:9], line)
        self.assertEqual(self._remote(), newer)

    def test_a_stale_rebased_worktree_never_overwrites_newer_remote_commits(self):
        # rebased onto the trunk, and origin gained a commit the rebase never saw: the factory
        # carries it onto the rebase (never the rebase back onto the stale remote, which put a
        # copy of the trunk commit under the branch for the lane to drop again)
        newer = self._push_from_elsewhere('newer-work')
        ok, line = lc.publish(self.repo, 'fix/B-9999', newer, main='main')
        self.assertTrue(ok, line)
        self.assertIn('carried 1 commit', line)
        remote = self._remote()
        self.assertEqual(self.sh(['log', '-1', '--format=%s %ae', remote], self.repo),
                         'newer-work p@example.com')
        self.assertEqual(self.sh(['cherry', 'origin/main', remote], self.repo).count('- '), 0)
        self.assertEqual(self.sh(['merge-base', remote, 'origin/main'], self.repo),
                         self.sh(['rev-parse', 'origin/main'], self.repo))
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin',
                                  lc.copies_archive('fix/B-9999', newer)], self.repo).split()[0],
                         newer)

    def test_a_rebase_holding_copies_of_every_remote_commit_still_publishes(self):
        newer = self._push_from_elsewhere('newer-work')
        self.sh(['fetch', '-q', 'origin', 'fix/B-9999'], self.repo)
        self.sh(['rebase', '-q', 'origin/fix/B-9999'], self.repo)
        self.sh(['rebase', '-q', 'origin/main'], self.repo)
        ok, line = lc.publish(self.repo, 'fix/B-9999', newer, main='main')
        self.assertTrue(ok, line)

    def test_the_factorys_own_redact_refusal_is_a_hook_refusal(self):
        # F-0003: its correction must reach the session, never a generic "commit and push"
        line = ('publish cloud/spec-x refused: redact: .sdd-input/reviews/r2.md:20 names a '
                'worker account — replace with lane-N')
        self.assertEqual(lc.push_failure(line.split('refused: ', 1)[1]), lc.HOOK_REFUSED)

    def test_the_factorys_own_refusals_are_not_hook_refusals(self):
        for line in ('would lose 2 commit(s) on origin/fix/B-9999 (abc, def) — rebase onto '
                     'origin/fix/B-9999, then push',
                     'rebase conflicts in: a.py',
                     'fix/B-9999 is not a lane branch; publish refused',
                     'archive/x is a protected ref, refused',
                     'the old tip could not be archived (refused)'):
            self.assertIsNone(lc.push_failure(line), line)
        self.assertEqual(lc.push_failure("remote rejected (pre-push hook declined)"),
                         lc.HOOK_REFUSED)

    def test_a_stale_head_refusal_is_not_a_hook_refusal(self):
        # never retried as a push: the branch is held for a rebase onto the remote head
        line = ('publish fix/B-9999 refused: would lose 1 commit(s) on origin/fix/B-9999 '
                '(abc123456) — rebase onto origin/fix/B-9999, then push')
        self.assertIsNone(lc.push_failure(line))
        self.assertTrue(lc.stale_head(line))

    def test_a_branch_never_pushed_is_counted_against_the_trunk(self):
        self.assertEqual(lc.unpushed_commits(self.repo, '', 'main'), 1)


class GitErrorTests(unittest.TestCase):
    """F-0176: a publish or commit refusal names git's own ``fatal:``/``error:``/``remote:``
    lines, never the ``failed to push some refs`` summary and never a fast-forward hint — both
    of which git prints *after* the line a reader needs."""

    sh = UnpushedAfterARebaseTest.sh

    def test_a_stale_info_rejection_keeps_the_rejected_line_and_drops_the_summary(self):
        output = (" ! [rejected]        HEAD -> fix/B-9999 (stale info)\n"
                  "error: failed to push some refs to 'origin'\n")
        self.assertEqual(lc.git_error(output),
                         '! [rejected]        HEAD -> fix/B-9999 (stale info)')

    def test_a_permission_denial_keeps_both_lines_in_gits_own_order(self):
        output = ('remote: Permission to org/asf.git denied to lane-2.\n'
                  "fatal: unable to access 'url': The requested URL returned error: 403\n")
        self.assertEqual(lc.git_error(output),
                         "remote: Permission to org/asf.git denied to lane-2.; "
                         "fatal: unable to access 'url': The requested URL returned error: 403")

    def test_an_unprefixed_local_hook_line_is_the_fallback(self):
        output = 'pre-push: lint failed on x.py:12\nerror: failed to push some refs to \'origin\'\n'
        self.assertEqual(lc.git_error(output), 'pre-push: lint failed on x.py:12')

    def test_empty_and_whitespace_only_output_is_push_failed(self):
        self.assertEqual(lc.git_error(''), 'push failed')
        self.assertEqual(lc.git_error('   \n\n  \n'), 'push failed')

    def test_a_summary_plus_hint_block_with_nothing_else_falls_back_past_both(self):
        # no line survives the hint/summary filter, so the raw last line is what is left —
        # a hint, here, because git printed nothing but the summary and its hints
        output = ("error: failed to push some refs to 'origin'\n"
                  "hint: Updates were rejected because the tip of your current branch is behind\n"
                  "hint: its remote counterpart.\n")
        self.assertEqual(lc.git_error(output), 'hint: its remote counterpart.')

    def test_more_than_the_limit_of_named_lines_is_cut(self):
        output = 'fatal: a\nfatal: b\nfatal: c\nfatal: d\n'
        self.assertEqual(lc.git_error(output), 'fatal: a; fatal: b; fatal: c')

    def test_push_failures_retry_classification_is_unchanged_on_every_row(self):
        # PD10: the text git_error keeps still drives push_failure the same way it always did
        self.assertIsNone(lc.push_failure(
            '! [rejected]        HEAD -> fix/B-9999 (stale info)'))
        self.assertEqual(lc.push_failure(
            "remote: Permission to org/asf.git denied to lane-2.; "
            "fatal: unable to access 'url': The requested URL returned error: 403"),
            lc.NETWORK_ERROR)
        self.assertEqual(lc.push_failure('pre-push: lint failed on x.py:12'), lc.HOOK_REFUSED)

    def test_a_live_lease_rejection_names_gits_own_rejection_not_its_summary(self):
        # b0056_a_lease_that_moved_is_refused_and_nothing_is_overwritten's fixture: origin moved
        # after the evidence was gathered, and the force-with-lease is refused
        base = tempfile.mkdtemp(prefix='lifecycle_git_error_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin, repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], repo)
        with open(os.path.join(repo, 'seed'), 'w') as f:
            f.write('seed')
        self.sh(['add', '-A'], repo)
        self.sh(['commit', '-qm', 'seed'], repo)
        self.sh(['push', '-q', 'origin', 'HEAD:main'], repo)
        self.sh(['checkout', '-q', '-b', 'fix/B-9999'], repo)
        with open(os.path.join(repo, 'fix'), 'w') as f:
            f.write('fix')
        self.sh(['add', '-A'], repo)
        self.sh(['commit', '-qm', 'the fix'], repo)
        self.sh(['push', '-q', 'origin', 'fix/B-9999'], repo)
        remote_sha = self.sh(['rev-parse', 'origin/fix/B-9999'], repo)
        other = os.path.join(base, 'other')
        self.sh(['clone', '-q', '-b', 'fix/B-9999', origin, other], base)
        for k, v in (('user.name', 'Person'), ('user.email', 'p@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], other)
        with open(os.path.join(other, 'late'), 'w') as f:
            f.write('late')
        self.sh(['add', '-A'], other)
        self.sh(['commit', '-qm', 'late work on origin'], other)
        self.sh(['push', '-q', 'origin', 'fix/B-9999'], other)

        ok, line = lc.publish(repo, 'fix/B-9999', remote_sha, main='main')

        self.assertFalse(ok, line)
        self.assertIn('! [rejected]', line)
        self.assertIn('stale info', line)
        self.assertNotIn('failed to push some refs', line)

    def test_commit_leftovers_reads_the_hooks_stdout_a_stderr_only_summary_used_to_hide(self):
        base = tempfile.mkdtemp(prefix='lifecycle_git_error_commit_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin, repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], repo)
        with open(os.path.join(repo, 'seed'), 'w') as f:
            f.write('seed')
        self.sh(['add', '-A'], repo)
        self.sh(['commit', '-qm', 'seed'], repo)
        self.sh(['push', '-q', 'origin', 'HEAD:main'], repo)
        self.sh(['checkout', '-q', '-b', 'fix/B-9999'], repo)
        hooks = os.path.join(base, 'refusing-hooks')
        os.makedirs(hooks)
        with open(os.path.join(hooks, 'pre-commit'), 'w') as f:
            f.write('#!/bin/sh\necho "commit refused" >&2\n'
                    'echo "pre-commit: lint failed on x.py:12"\nexit 1\n')
        os.chmod(os.path.join(hooks, 'pre-commit'), 0o755)
        self.sh(['config', 'core.hooksPath', hooks], repo)
        with open(os.path.join(repo, 'work.txt'), 'w') as f:
            f.write('half done')

        ok, line = lc.commit_leftovers(repo, 'fix/B-9999')

        self.assertFalse(ok, line)
        self.assertEqual(line, 'commit fix/B-9999 refused: pre-commit: lint failed on x.py:12')


class PublishACopiesRebaseTest(unittest.TestCase):
    """2026-09-26: a lane branch carrying copies of trunk commits is held back (kind copies)
    with "rebase onto origin/main, resolving <files>; the factory publishes the rebased branch".
    The session's conflict-resolved commits no longer match origin's by patch, so publish
    counted them lost, rebased back onto the copy-laden remote, conflicted, and told the session
    "rebase onto origin/<branch>" — the opposite instruction (a product's T-0338). A remote
    commit that is a copy of a trunk commit, or that the head carries under the same author and
    subject, is not lost: the old tip is archived and the rebased head published."""

    sh, commit = UnpushedAfterARebaseTest.sh, UnpushedAfterARebaseTest.commit

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_copies_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, self.repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], self.repo)
        self.commit('c', 'base c')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        # a branch: a copy of the trunk commit `x`, then its own change to `c`
        self.sh(['checkout', '-q', '-b', 'fix/B-7777'], self.repo)
        self.commit('x', 'x lands on the trunk')      # the copy
        self.commit('c', 'own change to c')          # its own commit
        self.sh(['push', '-q', 'origin', 'fix/B-7777'], self.repo)
        self.remote_sha = self.sh(['rev-parse', 'HEAD'], self.repo)
        # the trunk lands x under another sha, then changes c itself
        self.sh(['checkout', '-q', '-B', 'tmp', 'origin/main'], self.repo)
        self.commit('x', 'x lands on the trunk')
        self.commit('c', 'trunk change to c')
        self.sh(['push', '-q', 'origin', 'tmp:main'], self.repo)
        # the session answers the hold: rebase onto origin/main, resolving c by hand
        self.sh(['checkout', '-q', 'fix/B-7777'], self.repo)
        self.sh(['fetch', '-q', 'origin'], self.repo)
        r = subprocess.run(['git', 'rebase', '-q', 'origin/main'], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)  # the conflict the lane named
        with open(os.path.join(self.repo, 'c'), 'w', encoding='utf-8') as f:
            f.write('resolved c')
        self.sh(['add', 'c'], self.repo)
        subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=self.repo,
                       capture_output=True, text=True, check=True)

    def test_the_rebased_head_is_published_and_the_old_tip_archived(self):
        head = self.sh(['rev-parse', 'HEAD'], self.repo)
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', 'fix/B-7777'],
                                 self.repo).split()[0], head)
        archive = f'archive/fix/B-7777-copies-{self.remote_sha[:9]}'
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', archive],
                                 self.repo).split()[0], self.remote_sha)
        self.assertIn(archive, line)

    def test_a_remote_commit_the_head_does_not_carry_is_still_lost(self):
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)  # the own commit dropped
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main')
        self.assertFalse(ok, line)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', 'fix/B-7777'],
                                 self.repo).split()[0], self.remote_sha)


class RebaseOverAReportCommitTest(unittest.TestCase):
    """A product's F-0037, 2026-09-27: a correct session rebased its lane branch cleanly onto
    the trunk (22 own commits atop origin/main) and made no commit of its own; origin's tip was
    the cloud session's empty ``asf: report`` commit, and one of the branch's commits (a plan
    file) was dropped because the trunk had since landed its own version of that file. Publish
    counted that commit lost, rebased back onto the report commit, conflicted, and the loop
    guard parked the item as "no session added a commit" after three rounds. A head that is a
    rebase of origin's tip on a newer base is progress: publish archives the old tip and pushes
    it with a lease, and the loop guard counts it as a moved head."""

    sh = UnpushedAfterARebaseTest.sh

    def commit(self, name, text, msg=None):
        with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
            f.write(text)
        self.sh(['add', '-A'], self.repo)
        self.sh(['commit', '-qm', msg or text], self.repo)
        return self.sh(['rev-parse', 'HEAD'], self.repo)

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_report_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, self.repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], self.repo)
        self.commit('c', 'base c')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sh(['checkout', '-q', '-b', 'cloud/x'], self.repo)
        self.commit('a', 'own a', 'spec(X): own a')
        self.commit('plan.md', 'branch plan', 'plan(X): the plan')
        self.commit('b', 'own b', 'spec(X): own b')
        self.sh(['commit', '-q', '--allow-empty', '-m', 'asf: report correct-x'], self.repo)
        self.sh(['push', '-q', 'origin', 'cloud/x'], self.repo)
        self.remote_sha = self.sh(['rev-parse', 'HEAD'], self.repo)
        # the trunk lands its own plan at the same path, then moves on
        self.sh(['checkout', '-q', '-B', 'tmp', 'origin/main'], self.repo)
        self.commit('plan.md', 'trunk plan', 'plan(X): the human plan (#739)')
        self.commit('t', 'trunk t')
        self.sh(['push', '-q', 'origin', 'tmp:main'], self.repo)
        # the session: rebase onto origin/main, keeping the trunk's plan (the commit drops)
        self.sh(['checkout', '-q', 'cloud/x'], self.repo)
        self.sh(['reset', '-q', '--hard', 'HEAD~1'], self.repo)  # the local head: no report
        self.sh(['fetch', '-q', 'origin'], self.repo)
        r = subprocess.run(['git', 'rebase', '-q', 'origin/main'], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)  # plan.md: add/add
        r = subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--skip'], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.head = self.sh(['rev-parse', 'HEAD'], self.repo)
        self.assertEqual(self.sh(['merge-base', 'HEAD', 'origin/main'], self.repo),
                         self.sh(['rev-parse', 'origin/main'], self.repo))

    def remote(self):
        return self.sh(['ls-remote', '--heads', 'origin', 'cloud/x'], self.repo).split()[0]

    def test_the_rebase_is_progress(self):
        self.assertTrue(lc.rebase_of(self.repo, self.head, self.remote_sha, main='main'))
        self.assertFalse(lc.rebase_of(self.repo, self.remote_sha, self.remote_sha, main='main'))

    def test_publish_archives_the_old_tip_and_pushes_the_rebase(self):
        ok, line = lc.publish(self.repo, 'cloud/x', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), self.head)
        archive = lc.copies_archive('cloud/x', self.remote_sha)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', archive],
                                 self.repo).split()[0], self.remote_sha)
        self.assertFalse(os.path.isdir(os.path.join(self.repo, '.git', 'rebase-merge')))

    def test_a_dropped_commit_of_its_own_is_carried_back_never_lost(self):
        # own b dropped as well: its file is not the trunk's, so it is work — not a rebase of
        # the tip (rebase_of), and never pushed over: the factory carries it back onto the head
        self.sh(['reset', '-q', '--hard', 'HEAD~1'], self.repo)
        self.assertFalse(lc.rebase_of(self.repo, self.sh(['rev-parse', 'HEAD'], self.repo),
                                      self.remote_sha, main='main'))
        ok, line = lc.publish(self.repo, 'cloud/x', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertIn('carried 1 commit', line)
        self.assertIn('spec(X): own b', line)
        remote = self.remote()
        self.assertEqual(remote, self.sh(['rev-parse', 'HEAD'], self.repo))
        self.assertEqual(self.sh(['show', f'{remote}:b'], self.repo), 'own b')
        self.assertEqual(self.sh(['merge-base', remote, 'origin/main'], self.repo),
                         self.sh(['rev-parse', 'origin/main'], self.repo))

    def _three_runs(self):
        path = os.path.join(self.base, 's.jsonl')
        with open(path, 'w') as f:
            for n in (1, 2, 3):
                f.write(json.dumps({'job': 'correct-f-0037', 'item': 'F-0037', 'kind': 'correct',
                                    'branch': 'cloud/x', 'pid': n, 'started': 't%02d' % n,
                                    'launch_head': self.remote_sha,
                                    'worktree': self.repo}) + '\n')
                f.write(json.dumps({'job': 'correct-f-0037', 'ended': 'e%02d' % n,
                                    'end_reason': 'failed: not pushed: 0 uncommitted file(s), '
                                                  '11 unpushed commit(s)'}) + '\n')
        return path, lc.latest(path)['correct-f-0037']

    def test_the_loop_guard_counts_the_rebase_as_a_moved_head(self):
        path, run = self._three_runs()
        fields, line = lc.hold(path, run, lc.REBASE_CONFLICT, 'x', 'now', main='main')
        self.assertNotIn('parked', fields['correction'], line)

    def test_the_loop_guard_still_parks_an_unmoved_head(self):
        path, run = self._three_runs()
        self.sh(['reset', '-q', '--hard', self.remote_sha], self.repo)
        fields, line = lc.hold(path, run, lc.REBASE_CONFLICT, 'x', 'now', main='main')
        self.assertIs(fields['correction'].get('parked'), True, line)


class RebaseOffRewordedTrunkCopiesTest(unittest.TestCase):
    """A product's T-0338/T-0349, 2026-09-27: the lane branch carried copies of trunk commits
    whose patches no longer matched the trunk's (the trunk's own landing differed) and whose
    subjects the lane had reworded (``task(T-0338): hotfix(hooks): … (#804)``). The session's
    rebase onto the trunk dropped them — they are the trunk's — yet publish counted them lost
    and the loop guard parked both items. A remote commit whose subject, reword prefix or not,
    is a trunk commit's since the remote's base is a trunk copy."""

    sh = UnpushedAfterARebaseTest.sh
    commit = RebaseOverAReportCommitTest.commit

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_reword_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, self.repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], self.repo)
        self.commit('c', 'base c')
        self.commit('h', 'hook v0')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sh(['checkout', '-q', '-b', 'cloud/T-0338'], self.repo)
        self.commit('a', 'own a', 'task(T-0338): own a')
        self.commit('h', 'hook v1', 'task(T-0338): hotfix(hooks): scan pushed files (#804)')
        self.sh(['push', '-q', 'origin', 'cloud/T-0338'], self.repo)
        self.remote_sha = self.sh(['rev-parse', 'HEAD'], self.repo)
        self.sh(['checkout', '-q', '-B', 'tmp', 'origin/main'], self.repo)
        self.commit('h', 'hook v2', 'hotfix(hooks): scan pushed files (#804)')
        self.sh(['push', '-q', 'origin', 'tmp:main'], self.repo)
        self.sh(['checkout', '-q', 'cloud/T-0338'], self.repo)
        self.sh(['fetch', '-q', 'origin'], self.repo)
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)
        self.sh(['cherry-pick', self.sh(['rev-parse', f'{self.remote_sha}~1'], self.repo)],
                self.repo)
        self.head = self.sh(['rev-parse', 'HEAD'], self.repo)

    def test_the_rebase_is_progress_and_published(self):
        self.assertTrue(lc.rebase_of(self.repo, self.head, self.remote_sha, main='main'))
        ok, line = lc.publish(self.repo, 'cloud/T-0338', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertIn(lc.copies_archive('cloud/T-0338', self.remote_sha), line)

    def test_a_subject_the_trunk_never_had_is_still_lost(self):
        self.sh(['commit', '-q', '--amend', '-m', 'task(T-0338): own a, reworded'], self.repo)
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)
        self.assertFalse(lc.rebase_of(self.repo, self.sh(['rev-parse', 'HEAD'], self.repo),
                                      self.remote_sha, main='main'))


class RebaseOffSupersededBranchWorkTest(unittest.TestCase):
    """A product's F-0014/F-0094, 2026-09-27: a spec branch's own commits had since been carried
    onto the trunk in another form (a later revision of the plan the branch added; the spec and
    its band rows landed by a sibling lane's "carry the approved spec" commit). The correct
    sessions rebased onto the trunk as told and git dropped those commits, yet publish counted
    them lost: a follow-up edit to the branch's own file is not an add, and a band row in a
    shared register is not a copy of any one trunk commit. The factory rebased back onto the old
    tip, conflicted, and the loop guard parked both items after three rounds. A commit is
    accounted for when every file it touched is the branch's own (absent at its trunk base) and
    the new head reads the trunk's copy; the whole old tip is when its net change against its
    trunk base is already in the new head."""

    sh = UnpushedAfterARebaseTest.sh
    commit = RebaseOverAReportCommitTest.commit

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_superseded_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, self.repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], self.repo)
        self.commit('bands.md', 'row 1\n', 'base bands')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sh(['checkout', '-q', '-b', 'cloud/x'], self.repo)

    def push_branch(self):
        self.sh(['push', '-q', 'origin', 'cloud/x'], self.repo)
        self.remote_sha = self.sh(['rev-parse', 'HEAD'], self.repo)

    def land_on_trunk(self, files, msg):
        self.sh(['checkout', '-q', '-B', 'tmp', 'origin/main'], self.repo)
        for name, text in files.items():
            with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
                f.write(text)
        self.sh(['add', '-A'], self.repo)
        self.sh(['commit', '-qm', msg], self.repo)
        self.sh(['push', '-q', 'origin', 'tmp:main'], self.repo)
        self.sh(['checkout', '-q', 'cloud/x'], self.repo)
        self.sh(['fetch', '-q', 'origin'], self.repo)

    def rebase_keeping_trunk(self):
        """The session's ``git rebase origin/main``: every conflict resolved to the trunk's."""
        r = subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '-q', '-X', 'ours',
                            'origin/main'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.head = self.sh(['rev-parse', 'HEAD'], self.repo)

    def test_a_follow_up_edit_to_a_file_the_trunk_took_over_is_not_lost(self):
        self.commit('plan.md', 'plan v1\n', 'plan(X): the plan')
        self.commit('plan.md', 'plan v1, path fixed\n', 'plan(X): correct a path')
        self.commit('own', 'own\n', 'spec(X): own')
        self.push_branch()
        self.land_on_trunk({'plan.md': 'plan v8, carried, path fixed\n'}, 'docs: carry the plan')
        self.rebase_keeping_trunk()
        self.assertTrue(lc.rebase_of(self.repo, self.head, self.remote_sha, main='main'))
        ok, line = lc.publish(self.repo, 'cloud/x', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertIn(lc.copies_archive('cloud/x', self.remote_sha), line)

    def test_branch_work_the_trunk_carried_whole_is_not_lost(self):
        self.commit('bands.md', 'row 1\nrow X\n', 'docs(spec): book row X')
        self.commit('spec.md', 'spec v2\n', 'docs(spec): the spec')
        self.commit('review.md', 'approved\n', 'docs(review): approved')
        self.push_branch()
        self.land_on_trunk({'bands.md': 'row 0\nrow 1\nrow X\n', 'spec.md': 'spec v2\n'},
                           'docs: carry the approved spec for x-t2')
        self.rebase_keeping_trunk()
        self.assertTrue(lc.rebase_of(self.repo, self.head, self.remote_sha, main='main'))
        ok, line = lc.publish(self.repo, 'cloud/x', self.remote_sha, main='main')
        self.assertTrue(ok, line)

    def test_an_edit_to_a_shared_file_the_head_dropped_is_still_lost(self):
        self.commit('bands.md', 'row 1\nrow X\n', 'docs(spec): book row X')
        self.commit('spec.md', 'spec v2\n', 'docs(spec): the spec')
        self.push_branch()
        self.land_on_trunk({'other': 'trunk\n'}, 'trunk moves on')
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)
        self.commit('spec.md', 'spec v2\n', 'docs(spec): the spec')  # row X dropped
        head = self.sh(['rev-parse', 'HEAD'], self.repo)
        self.assertFalse(lc.rebase_of(self.repo, head, self.remote_sha, main='main'))
        lc.publish(self.repo, 'cloud/x', self.remote_sha, main='main')
        self.sh(['fetch', '-q', 'origin'], self.repo)
        self.assertIn('row X', self.sh(['show', 'origin/cloud/x:bands.md'], self.repo))

    def test_a_follow_up_edit_the_trunk_never_took_over_is_still_lost(self):
        self.commit('plan.md', 'plan v1\n', 'plan(X): the plan')
        self.commit('plan.md', 'plan v1, path fixed\n', 'plan(X): correct a path')
        self.push_branch()
        self.land_on_trunk({'other': 'trunk\n'}, 'trunk moves on')
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)
        self.commit('plan.md', 'plan v1\n', 'plan(X): the plan')  # the fix dropped
        head = self.sh(['rev-parse', 'HEAD'], self.repo)
        self.assertFalse(lc.rebase_of(self.repo, head, self.remote_sha, main='main'))


def _build_ahead(root):
    """A bare origin, a clone holding the lane branch ``lane/x`` at one commit (pushed), and a
    second clone that pushed one more commit onto it — the clone is the stale worktree."""
    def git(*args, cwd):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)
    origin, wt, other = (os.path.join(root, n) for n in ('origin.git', 'wt', 'other'))
    git('init', '-q', '--bare', '-b', 'main', origin, cwd=root)
    git('clone', '-q', origin, wt, cwd=root)
    for k, v in (('user.name', 'T'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
        git('config', k, v, cwd=wt)
    for name in ('shared', 'mine'):
        with open(os.path.join(wt, name), 'w') as f:
            f.write('base\n')
    git('add', '-A', cwd=wt)
    git('commit', '-qm', 'seed', cwd=wt)
    git('push', '-q', 'origin', 'HEAD:main', cwd=wt)
    git('checkout', '-q', '-b', 'lane/x', cwd=wt)
    git('push', '-q', 'origin', 'lane/x', cwd=wt)
    git('clone', '-q', '-b', 'lane/x', origin, other, cwd=root)
    for k, v in (('user.name', 'P'), ('user.email', 'p@example.com'), ('commit.gpgsign', 'false')):
        git('config', k, v, cwd=other)
    with open(os.path.join(other, 'shared'), 'w') as f:
        f.write('theirs\n')
    git('commit', '-qam', 'remote work', cwd=other)
    git('push', '-q', 'origin', 'lane/x', cwd=other)


try:
    from tests.gitfixture import Template
except ImportError:  # run from inside tests/
    from gitfixture import Template

AHEAD = Template(_build_ahead, prefix='lifecycle_ahead_')


class FactoryRebaseTests(unittest.TestCase):
    """The push guard found origin ahead: the factory rebases a clean worktree onto
    ``origin/<branch>`` itself and pushes; a conflict is aborted and named, never a merge or a
    force, and the first such hold spends no round."""

    def setUp(self):
        self.root = AHEAD.fresh()
        self.wt = os.path.join(self.root, 'wt')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.wt, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, name, text):
        with open(os.path.join(self.wt, name), 'w') as f:
            f.write(text)
        self.git('commit', '-qam', f'edit {name}')
        return self.git('rev-parse', 'HEAD')

    def remote(self):
        return self.git('ls-remote', '--heads', 'origin', 'lane/x').split()[0]

    def test_a_clean_rebase_is_pushed_and_logged(self):
        remote = self.remote()
        self.commit('mine', 'my work\n')
        ok, line = lc.publish(self.wt, 'lane/x', remote, main='main')
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith('rebased onto origin/lane/x (+1 remote commits) and pushed'),
                        line)
        self.assertEqual(self.remote(), self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.git('rev-parse', 'HEAD~1'), remote)  # a fast-forward: no merge
        self.assertEqual(self.git('rev-list', '--merges', '--count', 'HEAD'), '0')

    def test_a_conflict_is_aborted_and_names_the_files(self):
        remote = self.remote()
        mine = self.commit('shared', 'mine\n')
        ok, line = lc.publish(self.wt, 'lane/x', remote, main='main')
        self.assertFalse(ok, line)
        self.assertIn('rebase conflicts in: shared', line)
        self.assertTrue(lc.rebase_conflict(line))
        self.assertIsNone(lc.push_failure(line))
        self.assertEqual(self.git('rev-parse', 'HEAD'), mine)  # aborted: as it was
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse(os.path.isdir(os.path.join(self.wt, '.git', 'rebase-merge')))
        self.assertEqual(self.remote(), remote)
        text = lc.rebase_conflict_text('lane/x', line)
        self.assertIn('rebase conflicts in: shared', text)

    def test_the_first_conflict_hold_spends_no_round_the_second_does(self):
        path = os.path.join(self.root, 's.jsonl')
        run = {'job': 'a', 'item': 'B-0001', 'branch': 'lane/x', 'pid': 1, 'started': 't1'}
        with open(path, 'w') as f:
            f.write(json.dumps(run) + '\n')
        fields, line = lc.rebase_conflict_hold(path, run, 'rebase conflicts in: shared', 't2')
        self.assertNotIn('rounds', fields)
        self.assertEqual(fields['correction']['kind'], lc.REBASE_CONFLICT)
        self.assertIn('no round spent', line)
        again = {'job': 'b', 'item': 'B-0001', 'branch': 'lane/x', 'pid': 2, 'started': 't3'}
        with open(path, 'a') as f:
            f.write(json.dumps(dict(fields, job='a')) + '\n')
            f.write(json.dumps(again) + '\n')
        fields, line = lc.rebase_conflict_hold(path, again, 'rebase conflicts in: shared', 't4')
        self.assertEqual(fields['rounds'], 1)


class NothingToLandTests(unittest.TestCase):
    """F-0157: a fourth thing an ended run can be — not a failure, and not a landing. ``finished``
    means "pushed commits"; this means there was nothing to push and the session said so."""

    def test_the_constant(self):
        self.assertEqual(lc.NOTHING_TO_LAND, 'nothing to land')

    def test_it_is_in_outcome_classes_and_not_in_failing_classes(self):
        self.assertIn(lc.NOTHING_TO_LAND, lc.OUTCOME_CLASSES)
        self.assertNotIn(lc.NOTHING_TO_LAND, lc.FAILING_CLASSES)
        for c in lc.OUTCOME_CLASSES:
            if c not in (lc.FINISHED, lc.NOTHING_TO_LAND):
                self.assertIn(c, lc.FAILING_CLASSES)

    def test_outcome_class_returns_it(self):
        self.assertEqual(lc.outcome_class('nothing to land'), lc.NOTHING_TO_LAND)

    def test_a_real_failure_naming_the_same_words_is_not_it(self):
        # a failure whose signature nobody named still reads OTHER, not this class
        self.assertEqual(lc.outcome_class('failed: nothing to land'), lc.OTHER)

    def test_pd1_the_two_index_invariants_outcome_class_reads_by_position(self):
        # outcome_class's prefix loop reads OUTCOME_CLASSES[2] for the empty-branch prefix
        self.assertEqual(lc.OUTCOME_CLASSES[2], 'empty branch')
        # outcome_class's membership fallback reads OUTCOME_CLASSES[3:-1]
        self.assertEqual(lc.OUTCOME_CLASSES[3:-1], (
            'dead pid', 'pushed after stop', 'unpushed work', 'unknown model', 'auth',
            'quota-exhausted', 'permission', 'overloaded', 'hook refused', 'network error',
            'token cap', 'run cap', 'nothing to land'))

    def test_classify_on_a_run_ended_with_it(self):
        run = {'ended': 't1', 'end_reason': lc.NOTHING_TO_LAND}
        status = lc.classify(run, lc.Evidence())
        self.assertEqual(status.name, lc.NOTHING_TO_LAND)
        self.assertEqual(status.result, lc.NOTHING_TO_LAND)
        self.assertEqual(status.label, lc.NOTHING_TO_LAND)
        self.assertEqual(status.source, 'ledger')
        self.assertNotEqual(status.name, lc.FAILED)
        self.assertNotEqual(status.name, lc.DEAD)

    def test_derive_on_the_same_run(self):
        run = {'ended': 't1', 'end_reason': lc.NOTHING_TO_LAND}
        state = lc.derive(run, lc.Evidence())
        self.assertEqual(state.name, lc.PUSHED)
        self.assertEqual(state.reason, lc.NOTHING_TO_LAND)

    def test_the_scorecards_reading_is_untouched(self):
        # P8: the new end_reason starts with none of DEAD_PREFIXES, so is_dead is false for it,
        # and 'failed: empty branch' keeps its old meaning for every run that still ends that way
        self.assertFalse(score.failure_class(lc.NOTHING_TO_LAND).startswith(score.DEAD_PREFIXES))
        self.assertFalse(score.is_dead(
            types.SimpleNamespace(landed=False, end_reason=lc.NOTHING_TO_LAND)))
        self.assertEqual(score.failure_class(f'failed: {lc.EMPTY_BRANCH}'), 'failed: empty branch')


class DeliveredOffBranchTests(unittest.TestCase):
    """F-0157 P3, P4(c): the pure proof that an empty branch is not a failure — a ``ruling:`` for
    an ``adjudicate`` run, the item's latest ``REVIEW`` hold for a ``correct`` run, ``None`` for
    every other kind and for any report that is not ``status: done`` with ``commits:`` claiming
    nothing and no ``NEEDS OPERATOR``."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')

    def write(self, ln, path=None):
        with open(path or self.path, 'a') as f:
            f.write(json.dumps(ln) + '\n')

    RULING = 'REPORT\nitem: T-0001\nstatus: done\ncommits: none\nruling: overruled — no defect\n'

    def test_an_adjudicate_ruling_is_believed(self):
        run = {'kind': 'adjudicate', 'item': 'T-0001'}
        self.assertEqual(lc.delivered_off_branch(None, run, self.RULING),
                         'ruling filed: overruled — no defect…')

    def test_an_adjudicate_run_with_no_ruling_is_not_believed(self):
        text = 'REPORT\nitem: T-0001\nstatus: done\ncommits: none\n'
        run = {'kind': 'adjudicate', 'item': 'T-0001'}
        self.assertIsNone(lc.delivered_off_branch(None, run, text))

    def test_a_correct_run_answering_a_cleared_review_is_believed(self):
        self.write({'job': 'correct-t-0001', 'item': 'T-0001', 'correction':
                    {'kind': lc.REVIEW, 'text': 'docs/reviews/r-t-0001.md needs another pass',
                     'at': 't1'}})
        run = {'kind': 'correct', 'item': 'T-0001'}
        text = 'REPORT\nitem: T-0001\nstatus: done\ncommits: none\n'
        self.assertEqual(lc.delivered_off_branch(self.path, run, text),
                         'docs/reviews/r-t-0001.md: the finding is gone')

    def test_a_correct_run_over_a_footprint_or_a_gate_hold_is_not_believed(self):
        for kind in (lc.FOOTPRINT, 'gate'):
            with self.subTest(kind=kind):
                path = os.path.join(self.d, f'{kind}.jsonl')
                self.write({'job': 'correct-t-0001', 'item': 'T-0001', 'correction':
                           {'kind': kind, 'text': 'x', 'at': 't1'}}, path=path)
                run = {'kind': 'correct', 'item': 'T-0001'}
                text = 'REPORT\nitem: T-0001\nstatus: done\ncommits: none\n'
                self.assertIsNone(lc.delivered_off_branch(path, run, text))

    def test_a_coder_a_spec_and_a_plan_run_with_a_clean_report_are_never_believed(self):
        text = 'REPORT\nitem: T-0001\nstatus: done\ncommits: none\n'
        for kind in ('coder', 'spec', 'plan', 'fix-bug', 'review', 'reshape', 'close'):
            with self.subTest(kind=kind):
                run = {'kind': kind, 'item': 'T-0001'}
                self.assertIsNone(lc.delivered_off_branch(self.path, run, text))

    def test_the_three_report_gates(self):
        run = {'kind': 'adjudicate', 'item': 'T-0001'}
        partial = 'REPORT\nitem: T-0001\nstatus: partial\ncommits: none\nruling: overruled\n'
        self.assertIsNone(lc.delivered_off_branch(None, run, partial))
        claims_a_sha = 'REPORT\nitem: T-0001\nstatus: done\ncommits: deadbeef fix\nruling: overruled\n'
        self.assertIsNone(lc.delivered_off_branch(None, run, claims_a_sha))
        # pinned against an adjudicate run with a real ruling: F-0126's blocked park still wins
        needs_operator = self.RULING + 'NEEDS OPERATOR: pick a knob\n'
        self.assertIsNone(lc.delivered_off_branch(None, run, needs_operator))

    def test_every_no_claim_spelling_of_commits_passes(self):
        run = {'kind': 'adjudicate', 'item': 'T-0001'}
        for value in ('', 'none', 'n/a', '-', '—'):
            with self.subTest(commits=value):
                text = f'REPORT\nitem: T-0001\nstatus: done\ncommits: {value}\nruling: overruled\n'
                self.assertEqual(lc.delivered_off_branch(None, run, text),
                                 'ruling filed: overruled…')


class DeliveredReadersTests(unittest.TestCase):
    """F-0157 P4: three shipped mechanisms — ``settled`` (B-0128), ``overruling`` (B-1377) and
    ``review_answered`` (B-0149) — proved reachable for the first time now that each gates on
    :func:`lc.delivered` instead of :func:`lc.finished`, which a run ended
    :data:`lc.NOTHING_TO_LAND` can never be. Each case also pins the before-picture: the same run
    read as ``failed: empty branch: nothing to land`` — what every one of these runs read before
    this card — finds nothing."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _log(self, name, text):
        log = os.path.join(self.d, name)
        rec = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}
        with open(log, 'w') as f:
            f.write(json.dumps(rec) + '\n')
        return log

    def _write(self, lines, name):
        path = os.path.join(self.d, name)
        with open(path, 'w') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path

    def test_settled_over_a_ruling_that_committed_nothing(self):
        hold_at = '2026-01-01T07:17:39Z'
        lines = [{'job': 'h', 'pid': 1, 'started': 't1', 'item': 'B-0001', 'branch': 'b',
                  'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'h', 'rounds': 1, 'correction': {'kind': lc.REVIEW, 'text': 'x', 'at': hold_at}}]
        log = self._log('ruling.jsonl',
                        'REPORT\nitem: B-0001\nstatus: done\ncommits: none\nruling: overruled — no defect\n')
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'branch': 'b', 'kind': 'adjudicate',
               'pid': 2, 'started': '2026-01-01T07:18:00Z', 'ended': '2026-01-01T07:19:00Z',
               'end_reason': lc.NOTHING_TO_LAND, 'log': log}
        path = self._write(lines + [run], 'settled.jsonl')
        self.assertEqual(lc._settling_run(path, 'B-0001', hold_at).get('job'), 'adjudicate-b-0001')
        self.assertTrue(lc.settled(path, 'B-0001', hold_at))
        self.assertTrue(lc.corrections(path)['B-0001']['settled'])
        # the before-picture: the empty-branch failure this card replaces settled nothing
        before = dict(run, end_reason=f'failed: {lc.EMPTY_BRANCH}')
        path = self._write(lines + [before], 'unsettled.jsonl')
        self.assertIsNone(lc._settling_run(path, 'B-0001', hold_at))
        self.assertFalse(lc.settled(path, 'B-0001', hold_at))
        self.assertFalse(lc.corrections(path)['B-0001']['settled'])

    def test_overruling_over_its_commits_none_clause(self):
        head = 'a' * 40
        log = self._log('overrule.jsonl',
                        'REPORT\nitem: B-0001\nstatus: done\ncommits: none\nruling: overruled — no defect\n')
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': lc.NOTHING_TO_LAND,
               'launch_head': head, 'log': log}
        path = self._write([run], 'overruled.jsonl')
        self.assertEqual(lc.overruling(path, 'B-0001', head), 'adjudicate-b-0001')
        # the before-picture: :685-687's own clause, unreachable while such a run could never finish
        before = dict(run, end_reason=f'failed: {lc.EMPTY_BRANCH}')
        path = self._write([before], 'not-overruled.jsonl')
        self.assertIsNone(lc.overruling(path, 'B-0001', head))

    def test_overruling_the_pushed_sha_and_same_code_paths_still_work(self):
        sha = 'c' * 12
        pushed_ruling = ('REPORT\nitem: B-0001\nstatus: done\ncommits: deadbeef fix\n'
                         f'ruling: upheld — fixed\npushed: yes {sha}\n')
        log = self._log('pushed.jsonl', pushed_ruling)
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': 'finished', 'log': log}
        path = self._write([run], 'pushed-sha.jsonl')
        other_head = 'b' * 40
        self.assertIsNone(lc.overruling(path, 'B-0001', other_head))
        self.assertEqual(lc.overruling(path, 'B-0001', sha + 'f' * 28), 'adjudicate-b-0001')
        self.assertEqual(
            lc.overruling(path, 'B-0001', other_head, same_code=lambda s: s == sha),
            'adjudicate-b-0001')

    def test_review_answered_over_a_correct_run_with_nothing_to_change(self):
        head = 'd' * 40
        hold = {'kind': lc.REVIEW, 'at': 't3', 'text': 'rv/4-b-0001.md reads changes requested'}
        lines = [{'job': 'h', 'pid': 1, 'started': 't1', 'item': 'B-0001', 'branch': 'b',
                  'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'h', 'correction': hold}]
        run = {'job': 'correct-b-0001', 'item': 'B-0001', 'kind': lc.CORRECT, 'pid': 2,
               'started': 't4', 'ended': 't5', 'end_reason': lc.NOTHING_TO_LAND,
               'launch_head': head}
        path = self._write(lines + [run], 'answered.jsonl')
        self.assertEqual(lc.review_answered(path, 'B-0001', 'rv/4-b-0001.md', head),
                         'correct-b-0001')
        # the before-picture: B-0149's own case, unreachable while such a run could never finish
        before = dict(run, end_reason=f'failed: {lc.EMPTY_BRANCH}')
        path = self._write(lines + [before], 'not-answered.jsonl')
        self.assertIsNone(lc.review_answered(path, 'B-0001', 'rv/4-b-0001.md', head))
        # still refused: a spent window, and a run launched on a different head
        quota = dict(run, end_reason=lc.QUOTA_EXHAUSTED_REASON)
        path = self._write(lines + [quota], 'quota.jsonl')
        self.assertIsNone(lc.review_answered(path, 'B-0001', 'rv/4-b-0001.md', head))
        other_head = dict(run, launch_head='e' * 40)
        path = self._write(lines + [other_head], 'other-head.jsonl')
        self.assertIsNone(lc.review_answered(path, 'B-0001', 'rv/4-b-0001.md', head))


class RulingAcrossARebaseTests(unittest.TestCase):
    """F-0173: :func:`lc.overruling` measures both shas a ruling offers — the ``pushed:`` claim
    and, for a run that committed nothing, the ``launch_head`` fact — with a caller-supplied
    ``same_code`` predicate, not sha identity alone. A stub predicate and no fixture repo: the
    lane's own ``same_code`` wiring is F-0173's block A/B, in ``tests/test_lane.py``."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _log(self, name, text):
        log = os.path.join(self.d, name)
        rec = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}
        with open(log, 'w') as f:
            f.write(json.dumps(rec) + '\n')
        return log

    def _write(self, lines, name):
        path = os.path.join(self.d, name)
        with open(path, 'w') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')
        return path

    def test_the_pushed_sha_through_the_predicate(self):
        sha = 'c' * 12
        text = ('REPORT\nitem: B-0001\nstatus: done\ncommits: deadbeef fix\n'
                'ruling: upheld — fixed\npushed: yes ' + sha + '\n')
        log = self._log('pushed-log.jsonl', text)
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': 'finished', 'log': log}
        path = self._write([run], 'pushed.jsonl')
        other_head = 'b' * 40
        self.assertEqual(
            lc.overruling(path, 'B-0001', other_head, same_code=lambda s: s == sha),
            'adjudicate-b-0001')

    def test_the_launch_head_fact_through_the_predicate_when_the_pushed_sha_is_unreadable(self):
        # B-1377's own last rulings, after a rebase: the pushed: sha is garbage, commits: none,
        # and it is launch_head — the fact behind the claim — the predicate must be reached for.
        head = 'a' * 40
        text = ('REPORT\nitem: B-0001\nstatus: done\ncommits: none\n'
                'ruling: overruled — no defect\npushed: yes deadbeef99999999\n')
        log = self._log('rebased-log.jsonl', text)
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': 'finished', 'launch_head': head,
               'log': log}
        path = self._write([run], 'rebased.jsonl')
        new_head = 'b' * 40
        self.assertEqual(
            lc.overruling(path, 'B-0001', new_head, same_code=lambda s: s == head),
            'adjudicate-b-0001')

    def test_a_run_that_committed_offers_no_launch_head_candidate(self):
        head = 'a' * 40
        text = ('REPORT\nitem: B-0001\nstatus: done\ncommits: abc1234 fix: C1\n'
                'ruling: upheld — fixed\npushed: yes deadbeef99999999\n')
        log = self._log('committed-log.jsonl', text)
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': 'finished', 'launch_head': head,
               'log': log}
        path = self._write([run], 'committed.jsonl')
        new_head = 'b' * 40
        self.assertIsNone(lc.overruling(path, 'B-0001', new_head, same_code=lambda s: s == head))

    def test_a_predicate_that_accepts_nothing_is_not_called_when_the_head_already_matches(self):
        sha = 'c' * 12
        text = ('REPORT\nitem: B-0001\nstatus: done\ncommits: deadbeef fix\n'
                'ruling: upheld — fixed\npushed: yes ' + sha + '\n')
        log = self._log('matches-log.jsonl', text)
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': 'finished', 'log': log}
        path = self._write([run], 'matches.jsonl')
        calls = []

        def never(s):
            calls.append(s)
            return False

        self.assertEqual(
            lc.overruling(path, 'B-0001', sha + 'f' * 28, same_code=never),
            'adjudicate-b-0001')
        self.assertEqual(calls, [])
        other_head = 'b' * 40
        self.assertIsNone(lc.overruling(path, 'B-0001', other_head, same_code=never))
        self.assertEqual(calls, [sha])

    def test_same_code_none_is_sha_identity_only(self):
        sha = 'c' * 12
        text = ('REPORT\nitem: B-0001\nstatus: done\ncommits: deadbeef fix\n'
                'ruling: upheld — fixed\npushed: yes ' + sha + '\n')
        log = self._log('identity-log.jsonl', text)
        run = {'job': 'adjudicate-b-0001', 'item': 'B-0001', 'kind': 'adjudicate', 'pid': 1,
               'started': 't1', 'ended': 't2', 'end_reason': 'finished', 'log': log}
        path = self._write([run], 'identity.jsonl')
        self.assertEqual(lc.overruling(path, 'B-0001', sha + 'f' * 28), 'adjudicate-b-0001')
        other_head = 'b' * 40
        self.assertIsNone(lc.overruling(path, 'B-0001', other_head))


class OutcomeClassTests(unittest.TestCase):
    """T-0201, §2.1: one owner for what a session's end means."""

    def test_finished(self):
        self.assertEqual(lc.outcome_class('finished'), lc.FINISHED)

    def test_dead_pid(self):
        self.assertEqual(lc.outcome_class('dead pid'), 'dead pid')

    def test_a_push_gap(self):
        ev = lc.Evidence(result=OK, uncommitted=2, unpushed=1)
        self.assertEqual(lc.outcome_class(f'failed: {lc.push_gap(ev)}'), 'not pushed')

    def test_push_gap_still_spells_the_same_bytes(self):
        ev = lc.Evidence(result=OK, uncommitted=2, unpushed=1)
        self.assertEqual(lc.push_gap(ev), 'not pushed: 2 uncommitted file(s), 1 unpushed commit(s)')

    def test_empty_branch(self):
        self.assertEqual(lc.outcome_class(f'failed: {lc.EMPTY_BRANCH}'), 'empty branch')

    def test_a_cli_signature(self):
        self.assertEqual(lc.outcome_class('failed: quota-exhausted'), 'quota-exhausted')

    def test_unpushed_work(self):
        self.assertEqual(lc.outcome_class('failed: unpushed work'), 'unpushed work')

    def test_pushed_after_stop(self):
        self.assertEqual(lc.outcome_class(f'failed: {lc.PUSHED_AFTER_STOP}'), 'pushed after stop')

    def test_an_unnamed_failure_is_other(self):
        self.assertEqual(lc.outcome_class('failed: something nobody named'), lc.OTHER)

    def test_a_bare_failed_is_other(self):
        self.assertEqual(lc.outcome_class('failed'), lc.OTHER)

    def test_lines_that_describe_no_outcome(self):
        for line in ('running', 'unknown', '', None):
            with self.subTest(line=line):
                self.assertIsNone(lc.outcome_class(line))

    def test_pd7_stopped_is_an_operators_decision_not_a_class(self):
        self.assertIsNone(lc.outcome_class(lc.STOPPED))
        self.assertNotIn(lc.STOPPED, lc.OUTCOME_CLASSES)

    def test_whitespace_and_case(self):
        self.assertEqual(lc.outcome_class('  FAILED: Quota-Exhausted  '), 'quota-exhausted')

    def test_every_string_judge_can_return_classes_to_a_member(self):
        ev = lc.Evidence(result=OK, uncommitted=3, unpushed=0)
        names = [name for name, _ in lc.runtime_mod.FAILURE_SIGNATURES]
        reasons = [lc.push_gap(ev), lc.EMPTY_BRANCH, lc.runtime_mod.report.UNPUSHED, *names]
        strings = {lc.FINISHED, lc.DEAD_PID, 'failed'} | {f'failed: {r}' for r in reasons}
        for s in sorted(strings):
            with self.subTest(end_reason=s):
                self.assertIn(lc.outcome_class(s), lc.OUTCOME_CLASSES)

    def test_the_failing_classes_are_all_but_the_two_that_are_not_failures(self):
        self.assertEqual(set(lc.FAILING_CLASSES),
                         set(lc.OUTCOME_CLASSES) - {lc.FINISHED, lc.NOTHING_TO_LAND})

    def test_the_vocabulary_is_the_specs_in_the_specs_order(self):
        self.assertEqual(lc.OUTCOME_CLASSES, (
            'finished', 'not pushed', 'empty branch', 'dead pid', 'pushed after stop',
            'unpushed work', 'unknown model', 'auth', 'quota-exhausted', 'permission', 'overloaded',
            'hook refused', 'network error', 'token cap', 'run cap', 'nothing to land', 'other'))

    def test_a_run_ended_by_its_token_cap(self):
        self.assertIn(lc.tokens.TOKEN_CAP, lc.OUTCOME_CLASSES)
        self.assertEqual(lc.outcome_class(f'failed: {lc.tokens.TOKEN_CAP}'), lc.tokens.TOKEN_CAP)

    def test_a_run_ended_by_its_run_cap(self):
        self.assertIn(lc.budget.RUN_CAP, lc.OUTCOME_CLASSES)
        self.assertEqual(lc.outcome_class(f'failed: {lc.budget.RUN_CAP}'), lc.budget.RUN_CAP)

    def test_b0294_an_overloaded_api_error_is_its_own_class_not_other(self):
        self.assertEqual(lc.outcome_class(f'failed: {lc.runtime_mod.OVERLOADED}'),
                         lc.runtime_mod.OVERLOADED)
        self.assertIn(lc.runtime_mod.OVERLOADED, lc.OUTCOME_CLASSES)
        self.assertIn(lc.runtime_mod.OVERLOADED, lc.FAILING_CLASSES)


class PublishRedactionTests(unittest.TestCase):
    """F-0035: a worker session's own account name landed in a product spec, and the product's
    own redact pre-push hook refused the push with nothing more useful than "push refused" — 7+
    rounds on one item. :func:`lc.publish` now runs the same scan itself before it ever pushes,
    so the branch is held with a precise correction the first time, not after a wasted push."""

    def sh(self, cmd, cwd):
        r = subprocess.run(['git', *cmd], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {cmd}:\n{r.stdout}\n{r.stderr}")
        return r.stdout.strip()

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_redact_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.home = os.path.join(base, 'home')
        os.makedirs(self.home)
        self.account = 'acct-' + 'lifecycle'
        with open(os.path.join(self.home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write(f'worker_pool:\n  accounts:\n    - name: {self.account}\n')
        origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', origin], base)
        self.sh(['clone', '-q', origin, self.repo], base)
        for k, v in (('user.name', 'Test'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], self.repo)
        with open(os.path.join(self.repo, 'seed.txt'), 'w', encoding='utf-8') as f:
            f.write('seed\n')
        self.sh(['add', '-A'], self.repo)
        self.sh(['commit', '-qm', 'seed'], self.repo)
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sh(['checkout', '-q', '-b', 'fix/B-9997'], self.repo)

        old_home = env.ASF_HOME
        env.ASF_HOME = self.home
        self.addCleanup(lambda: setattr(env, 'ASF_HOME', old_home))
        redact._DEFAULT_CACHE.clear()
        self.addCleanup(redact._DEFAULT_CACHE.clear)

    def write_commit(self, name, text, message):
        with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
            f.write(text)
        self.sh(['add', '-A'], self.repo)
        self.sh(['commit', '-qm', message], self.repo)

    def test_a_protected_name_holds_the_push_with_a_precise_correction(self):
        with open(os.path.join(self.home, 'redact-names.txt'), 'w', encoding='utf-8') as f:
            f.write('operator-' + 'private\n')
        redact._DEFAULT_CACHE.clear()
        self.write_commit('plan.md', 'a plan naming operator-' + 'private directly\n', 'a plan')

        ok, line = lc.publish(self.repo, 'fix/B-9997', '', main='main')

        self.assertFalse(ok, line)
        self.assertIn('publish fix/B-9997 refused:', line)
        self.assertIn('redact: plan.md:1 names a protected name — remove it', line)
        self.assertNotIn('operator-' + 'private', line)  # never the matched text (D8)
        # nothing was pushed: the branch does not exist on origin at all
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', 'fix/B-9997'], self.repo), '')

    def test_a_publish_refused_after_the_name_rewrite_leaves_the_worktree_as_it_was(self):
        # #340 moved HEAD to the rewritten chain even when another finding then refused the push
        with open(os.path.join(self.home, 'redact-names.txt'), 'w', encoding='utf-8') as f:
            f.write('operator-' + 'private\n')
        redact._DEFAULT_CACHE.clear()
        self.write_commit('plan.md', f'a plan naming {self.account}\n', 'a plan')
        self.write_commit('other.md', 'operator-' + 'private\n', 'another file')
        head = self.sh(['rev-parse', 'HEAD'], self.repo)

        ok, line = lc.publish(self.repo, 'fix/B-9997', '', main='main')

        self.assertFalse(ok, line)
        self.assertIn('redact: other.md:1', line)
        self.assertEqual(self.sh(['rev-parse', 'HEAD'], self.repo), head)
        self.assertEqual(self.sh(['status', '--porcelain'], self.repo), '')
        with open(os.path.join(self.repo, 'plan.md'), encoding='utf-8') as f:
            self.assertIn(self.account, f.read())

    def test_a_worker_account_name_fixed_in_a_later_commit_is_rewritten_and_published(self):
        # the loop: the session was told "replace with lane-N", fixed the file in a new commit,
        # and the per-commit scan still read the first — refused every run. The factory rewrites
        # the unpublished commits and publishes; no commit on origin names the account.
        self.write_commit('plan.md', f'a plan naming {self.account} directly\n',
                          f'a plan for {self.account}')
        self.write_commit('other.md', 'untouched\n', 'another file')
        self.write_commit('plan.md', 'a plan naming lane-1 directly\n', 'replace with lane-N')
        before = self.sh(['rev-list', '--count', 'origin/main..HEAD'], self.repo)

        ok, line = lc.publish(self.repo, 'fix/B-9997', '', main='main')

        self.assertTrue(ok, line)
        self.assertIn('worker-account names rewritten to lane-N', line)
        self.assertNotIn(self.account, line)
        remote = self.sh(['rev-parse', 'origin/fix/B-9997'], self.repo)
        self.assertEqual(remote, self.sh(['rev-parse', 'HEAD'], self.repo))
        self.assertEqual(self.sh(['rev-list', '--count', f'origin/main..{remote}'], self.repo),
                         before)
        history = self.sh(['log', '-p', '--format=%B%an', f'origin/main..{remote}'], self.repo)
        self.assertNotIn(self.account, history)
        self.assertIn('a plan for lane-1', history)
        self.assertIn('Test', history)  # the author is kept
        self.assertEqual(redact.scan_unpublished(self.repo, 'HEAD', redact.default_patterns(
            self.repo), published=(remote,)), [])
        with open(os.path.join(self.repo, 'other.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'untouched\n')

    def test_uncommitted_work_is_never_rewritten_under_the_session(self):
        self.write_commit('plan.md', f'a plan naming {self.account} directly\n', 'a plan')
        with open(os.path.join(self.repo, 'seed.txt'), 'w', encoding='utf-8') as f:
            f.write('work in progress\n')

        ok, line = lc.publish(self.repo, 'fix/B-9997', '', main='main')

        self.assertFalse(ok, line)
        self.assertIn('redact: plan.md:1 names a worker account — replace with lane-N', line)
        with open(os.path.join(self.repo, 'seed.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'work in progress\n')

    def test_a_clean_branch_is_still_published(self):
        with open(os.path.join(self.repo, 'plan.md'), 'w', encoding='utf-8') as f:
            f.write('a perfectly generic plan\n')
        self.sh(['add', '-A'], self.repo)
        self.sh(['commit', '-qm', 'a plan'], self.repo)

        ok, line = lc.publish(self.repo, 'fix/B-9997', '', main='main')

        self.assertTrue(ok, line)
        self.assertIn('published fix/B-9997 at', line)


class RedactionCorrectionTests(unittest.TestCase):
    """The generic fallback (:func:`lc._redaction_findings` runs the same scan a repo's own
    pre-push hook does, so the two cases below cover the same ground the hook-output parser
    exists for): a product whose only scanner is its pre-push hook still gets the precise
    ``redact: <file>:<line> …`` correction, parsed back out of the hook's own captured output."""

    def test_a_worker_account_finding_line_becomes_the_lane_n_correction(self):
        findings = redact.parse_finding_lines(
            'docs/plans/p.md:12: name (worker_pool.accounts)\n'
            'redact: refused — 1 finding(s); no line above is printed as it was\n')
        self.assertEqual(len(findings), 1)
        self.assertEqual(redact.correction(findings),
                         ['redact: docs/plans/p.md:12 names a worker account — replace with lane-N'])

    def test_a_secret_finding_line_becomes_a_removal_correction(self):
        findings = redact.parse_finding_lines('f.txt:4: secret (rule:aws-access-key)\n')
        self.assertEqual(redact.correction(findings),
                         ['redact: f.txt:4 is a secret — remove it before pushing'])

    def test_lines_that_are_not_a_finding_are_ignored(self):
        text = ('Enumerating objects: 3, done.\n'
                'To origin\n'
                ' ! [remote rejected] fix/B-1 -> fix/B-1 (pre-push hook declined)\n')
        self.assertEqual(redact.parse_finding_lines(text), [])


if __name__ == '__main__':
    unittest.main()


class _RebaseShape(unittest.TestCase):
    """A bare origin and a clone with a lane branch on it; helpers to move either side."""

    sh = UnpushedAfterARebaseTest.sh
    branch = 'cloud/T-0338'

    def setUp(self):
        base = tempfile.mkdtemp(prefix='lifecycle_pingpong_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        self.origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        self.sh(['init', '-q', '--bare', '-b', 'main', self.origin], base)
        self._no_housekeeping(self.origin)  # B-0403: a detached gc on a push would otherwise
        self.sh(['clone', '-q', self.origin, self.repo], base)  # still be repacking origin.git
        self._no_housekeeping(self.repo)    # when push_from_elsewhere clones it moments later
        self.identity(self.repo)
        self.commit('c', 'base c\n', 'base c')
        self.commit('b', 'b v0\n', 'base b')
        self.commit('h', 'hook v0\n', 'base h')
        self.sh(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sh(['checkout', '-q', '-b', self.branch], self.repo)

    def identity(self, repo, email='t@example.com'):
        for k, v in (('user.name', 'Test'), ('user.email', email), ('commit.gpgsign', 'false')):
            self.sh(['config', k, v], repo)

    def _no_housekeeping(self, repo):
        """Turn off git's own background writer on ``repo`` (B-0403): a detached ``gc --auto``
        a push starts can still be repacking ``self.origin`` when ``push_from_elsewhere`` clones
        it moments later, and the clone's own copy of a pack file git is mid-rename on then
        fails — confirmed live in CI (docs/reviews/1-b-0356.md's I list)."""
        for k, v in (('gc.auto', '0'), ('gc.autoDetach', 'false'), ('maintenance.auto', 'false')):
            self.sh(['config', k, v], repo)

    def commit(self, name, text, msg, repo=None):
        repo = repo or self.repo
        with open(os.path.join(repo, name), 'w', encoding='utf-8') as f:
            f.write(text)
        self.sh(['add', '-A'], repo)
        self.sh(['commit', '-qm', msg], repo)
        return self.sh(['rev-parse', 'HEAD'], repo)

    def push_branch(self):
        self.sh(['push', '-q', 'origin', self.branch], self.repo)
        self.remote_sha = self.sh(['rev-parse', 'HEAD'], self.repo)
        return self.remote_sha

    def land_on_trunk(self, *commits):
        """``(name, text, msg)`` commits landed on origin/main from a throwaway branch."""
        self.sh(['checkout', '-q', '-B', 'tmp', 'origin/main'], self.repo)
        for name, text, msg in commits:
            self.commit(name, text, msg)
        self.sh(['push', '-q', 'origin', 'tmp:main'], self.repo)
        self.sh(['checkout', '-q', self.branch], self.repo)
        self.sh(['fetch', '-q', 'origin'], self.repo)

    def push_from_elsewhere(self, name, text, msg, email='person@example.com'):
        """A commit on origin/<branch> made by someone else, from another clone."""
        other = os.path.join(self.base, 'other-' + name)
        self.sh(['clone', '-q', '-b', self.branch, self.origin, other], self.base)
        self._no_housekeeping(other)
        self.identity(other, email)
        sha = self.commit(name, text, msg, repo=other)
        self.sh(['push', '-q', 'origin', self.branch], other)
        return sha

    def rebase_resolving(self, resolved, skip=()):
        """``git rebase origin/main``; a conflict in a file of ``resolved`` is resolved to that
        text, a commit whose subject is in ``skip`` is dropped (it is the trunk's)."""
        r = subprocess.run(['git', 'rebase', '-q', 'origin/main'], cwd=self.repo,
                           capture_output=True, text=True)
        for _ in range(20):
            if r.returncode == 0:
                return self.sh(['rev-parse', 'HEAD'], self.repo)
            subject = open(os.path.join(self.repo, '.git', 'rebase-merge', 'message'),
                           encoding='utf-8').read().split('\n', 1)[0]
            if subject in skip:
                r = subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--skip'],
                                   cwd=self.repo, capture_output=True, text=True)
                continue
            files = self.sh(['diff', '--name-only', '--diff-filter=U'], self.repo).split()
            for name in files:
                self.assertIn(name, resolved, f'unexpected conflict in {name} at {subject!r}')
                with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
                    f.write(resolved[name])
                self.sh(['add', name], self.repo)
            r = subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'],
                               cwd=self.repo, capture_output=True, text=True)
        self.fail('the rebase never finished')

    def remote(self):
        return self.sh(['ls-remote', '--heads', 'origin', self.branch], self.repo).split()[0]

    def on_origin(self, ref):
        out = self.sh(['ls-remote', '--heads', 'origin', ref], self.repo)
        return out.split()[0] if out else ''

    def head(self):
        return self.sh(['rev-parse', 'HEAD'], self.repo)

    def mid_rebase(self):
        return os.path.isdir(os.path.join(self.repo, '.git', 'rebase-merge'))


class PublishRebasePingPongTest(_RebaseShape):
    """A product's T-0338, 2026-09-26/27 — sixty runs in 27 hours. The lane held the branch
    "trunk history under the branch: rebase the branch's own commits onto origin/main"; the
    session did, resolving the conflicts the lane named, and its head then sat on a newer
    trunk than origin/<branch> did. Publish counted the remote commits the rebase rewrote or
    dropped as lost and — the ping-pong — rebased the fresh head back onto the stale remote:
    the trunk commits it carried replayed over their own copies, conflicted, and the hold said
    "rebase onto origin/<branch>", the opposite instruction. 155 runs ended "not pushed", 179
    publishes were refused for "rebase conflicts", about 312 hours of lead time.

    The shape: origin/main gained ``X`` (a patch the branch also carries), ``H'`` (a commit the
    branch carries as a reworded copy with another patch, ``task(T-0338): hotfix(hooks): …
    (#804)``) and its own change to ``b`` (so the branch's ``B`` conflicts); origin/<branch>
    holds ``X``, ``H``, ``A``, ``B`` on the old base; the head is ``A``, ``B`` resolved and a
    new ``C`` on the new trunk. A head past the remote on the trunk is never rebased onto the
    remote: it is published under a lease with the old tip archived when every commit it drops
    is accounted for; one that is not is carried onto the head by the factory, and refused
    with the trunk named — never the branch — when that conflicts."""

    def setUp(self):
        super().setUp()
        self.commit('x', 'x\n', 'chore: x lands on the trunk')
        self.commit('h', 'hook v1\n', 'task(T-0338): hotfix(hooks): scan pushed files (#804)')
        self.commit('a', 'own a\n', 'task(T-0338): own a')
        self.commit('b', 'b v1\n', 'task(T-0338): own b')
        self.push_branch()
        self.land_on_trunk(('x', 'x\n', 'chore: x lands on the trunk'),
                           ('h', 'hook v2\n', 'hotfix(hooks): scan pushed files (#804)'),
                           ('b', 'b trunk\n', 'chore: the trunk changes b'))
        self.rebase_resolving({'b': 'b resolved\n'},
                              skip=('task(T-0338): hotfix(hooks): scan pushed files (#804)',))
        self.new = self.commit('c', 'own c\n', 'task(T-0338): own c')
        self.assertEqual(self.sh(['merge-base', 'HEAD', 'origin/main'], self.repo),
                         self.sh(['rev-parse', 'origin/main'], self.repo))
        self.assertEqual(self.sh(['rev-list', '--count', 'origin/main..HEAD'], self.repo), '3')

    def test_the_head_sits_past_the_remote_on_the_trunk(self):
        self.assertTrue(lc.past_remote_on_trunk(self.repo, self.head(), self.remote_sha, 'main'))
        self.assertFalse(lc.past_remote_on_trunk(self.repo, self.remote_sha, self.head(), 'main'))
        self.assertFalse(lc.past_remote_on_trunk(self.repo, self.remote_sha, self.remote_sha,
                                                 'main'))

    def test_the_t0338_shape_is_published_under_a_lease_with_the_old_tip_archived(self):
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), head)
        self.assertEqual(self.head(), head)  # never rebased onto the stale remote
        self.assertFalse(self.mid_rebase())
        archive = lc.copies_archive(self.branch, self.remote_sha)
        self.assertEqual(self.on_origin(archive), self.remote_sha)
        self.assertIn(archive, line)
        self.assertNotIn('rebase conflicts', line)

    def test_a_foreign_commit_that_applies_is_carried_onto_the_head_and_published(self):
        # someone pushed to origin/<branch> after the session's rebase: the factory carries it
        foreign = self.push_from_elsewhere('d', 'd by a person\n', "fix: a person's own fix")
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, foreign, main='main')
        self.assertTrue(ok, line)
        self.assertIn(f"carried 1 commit(s) the rebase dropped ({foreign[:9]} fix: a person's "
                      f"own fix)", line)
        remote = self.remote()
        self.assertEqual(remote, self.head())
        self.assertEqual(self.sh(['rev-parse', 'HEAD~1'], self.repo), head)  # on top of it
        self.assertEqual(self.sh(['log', '-1', '--format=%s %ae', remote], self.repo),
                         "fix: a person's own fix person@example.com")
        self.assertEqual(self.sh(['cherry', 'origin/main', remote], self.repo).count('- '), 0)
        self.assertEqual(self.on_origin(lc.copies_archive(self.branch, foreign)), foreign)

    def test_a_foreign_commit_that_conflicts_is_never_dropped_and_the_trunk_is_named(self):
        # someone pushed to origin/<branch> after the session's rebase; it touches `b`, so
        # neither carrying it nor a rebase onto the remote applies — the session is told the
        # trunk, never the branch
        foreign = self.push_from_elsewhere('b', 'b by a person\n', "fix: a person's own fix")
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, foreign, main='main')
        self.assertFalse(ok, line)
        self.assertEqual(self.remote(), foreign)              # nothing clobbered
        self.assertEqual(self.head(), head)                   # the worktree as it was
        self.assertFalse(self.mid_rebase())
        self.assertEqual(self.on_origin(lc.copies_archive(self.branch, foreign)), '')
        self.assertTrue(lc.stale_head(line), line)
        self.assertFalse(lc.rebase_conflict(line), line)     # the ping-pong's refusal
        self.assertIsNone(lc.push_failure(line))
        self.assertIn('would lose 1 commit', line)
        self.assertIn(foreign[:9], line)
        self.assertIn("a person's own fix", line)
        self.assertNotIn('own b', line)      # accounted for: resolved by hand, carried
        self.assertNotIn('(#804)', line)     # accounted for: the trunk's, reworded
        self.assertIn('rebase onto origin/main', line)
        self.assertIn(f'cherry-pick {foreign[:9]}', line)
        self.assertNotIn(f'rebase onto origin/{self.branch}', line)

    def test_the_hold_never_contradicts_the_lane(self):
        foreign = self.push_from_elsewhere('b', 'b by a person\n', "fix: a person's own fix")
        ok, line = lc.publish(self.repo, self.branch, foreign, main='main')
        self.assertFalse(ok, line)
        text = lc.stale_head_text(self.branch, line)
        self.assertIn('rebase onto origin/main', text)
        self.assertIn(foreign[:9], text)
        self.assertNotIn(f'rebase onto origin/{self.branch}', text)
        self.assertIn('never a force', text)

    def test_a_foreign_commit_carried_onto_the_head_is_not_lost(self):
        foreign = self.push_from_elsewhere('d', 'd by a person\n', "fix: a person's own fix")
        self.sh(['fetch', '-q', 'origin'], self.repo)
        self.sh(['cherry-pick', foreign], self.repo)  # the session carried it, as told
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, foreign, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), head)
        self.assertEqual(self.sh(['log', '-1', '--format=%ae', head], self.repo),
                         'person@example.com')

    def test_a_redaction_finding_refuses_before_the_old_tip_is_archived(self):
        home = os.path.join(self.base, 'home')
        os.makedirs(home)
        account = 'acct-' + 'pingpong'
        with open(os.path.join(home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write(f'worker_pool:\n  accounts:\n    - name: {account}\n')
        old_home = env.ASF_HOME
        env.ASF_HOME = home
        self.addCleanup(lambda: setattr(env, 'ASF_HOME', old_home))
        redact._DEFAULT_CACHE.clear()
        self.addCleanup(redact._DEFAULT_CACHE.clear)
        # a secret, not a worker-account name: an account name is rewritten, never refused
        self.commit('plan.md', 'a plan quoting AKIA' + 'ABCDEFGHIJKLMNOP\n', 'task(T-0338): a plan')
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertFalse(ok, line)
        self.assertIn('redact: plan.md:1', line)
        self.assertEqual(self.remote(), self.remote_sha)
        self.assertEqual(self.on_origin(lc.copies_archive(self.branch, self.remote_sha)), '')


class PublishSupersededByTrunkTest(_RebaseShape):
    """A product's #529 (F-0097's plan branch), 2026-09-27: origin/<branch> held spec commits
    whose content later landed on the trunk through another lane (#843). The rebased head reads
    the trunk's copy of every file they touch; none is a patch copy of a trunk commit, each
    conflicts when cherry-picked onto the head, so every publish refused "would lose 7 commits".
    A dropped commit whose every file the head reads as the trunk has it, and whose branch
    change to that file merges onto the trunk cleanly to the trunk's own file (the trunk holds
    every line of it), is accounted for. A change the trunk does not hold — another hunk,
    another file, a line the trunk wrote its own way, a person's clause inside the block both
    sides wrote — is still carried, or refused: never published away (the 90%-agreement rule
    with ``-X ours`` dropped the last two)."""

    def spec(self, body=True, l10='line 10', l1='line 1', **clauses):
        """A skeleton of twelve lines; ``body``: twenty clauses of the spec after line 5, clause
        ``cN`` read from ``clauses`` when given."""
        out = [f'line {n}' for n in range(1, 13)]
        out[9] = l10
        out[0] = l1
        if body:
            out[5:5] = [clauses.get(f'c{k}', f'spec clause {k}') for k in range(1, 21)]
        return ''.join(f'{ln}\n' for ln in out)

    def setUp(self):
        super().setUp()
        self.land_on_trunk(('spec.md', self.spec(body=False), 'docs(spec): skeleton'))
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)
        self.commit('spec.md', self.spec(c3='clause 3, draft'), 'docs(spec): voice parity rev 1')
        self.commit('spec.md', self.spec(c3='clause 3, revised', c7='clause 7, revised'),
                    'docs(spec): voice parity rev 2')

    def rebase_onto_trunk_as_the_trunk_has_it(self):
        """The session's rebase: the head is the trunk plus the session's own plan commit."""
        self.sh(['reset', '-q', '--hard', 'origin/main'], self.repo)
        return self.commit('plan.md', 'the plan\n', 'docs(plan): voice parity plan')

    def land_the_newer_form(self, c3='clause 3, revised'):
        # the same spec, landed through another lane, with the trunk's own edit to line 1
        self.land_on_trunk(('spec.md', self.spec(c3=c3, c7='clause 7, revised',
                                                 l1='line 1, the trunk'),
                            'spec(F-0097): the spec, landed (#843)'))

    def test_a_line_the_trunk_wrote_its_own_way_is_never_accounted_for(self):
        # clause 3 landed in the trunk's own final form: which one survives is a person's call
        self.push_branch()
        self.land_the_newer_form(c3='clause 3, final')
        head = self.rebase_onto_trunk_as_the_trunk_has_it()
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(len(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost,
                                                    'main')), 2)
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertFalse(ok, line)
        self.assertEqual(self.remote(), self.remote_sha)

    def test_a_persons_clause_inside_the_block_both_sides_wrote_is_never_published_away(self):
        # 18 of the branch's 20 lines are the trunk's: a ratio called this superseded, and
        # `-X ours` dropped the person's clause 15 from the published branch
        self.commit('spec.md', self.spec(c3='clause 3, revised', c7='clause 7, revised',
                                         c15='clause 15, a person'),
                    "docs(spec): a person's clause")
        self.push_branch()
        self.land_the_newer_form(c3='clause 3, final')
        head = self.rebase_onto_trunk_as_the_trunk_has_it()
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(len(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost,
                                                    'main')), 3)
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertFalse(ok, line)
        self.assertIn('would lose 3 commit', line)
        self.assertEqual(self.remote(), self.remote_sha)
        self.assertEqual(self.head(), head)

    def test_a_superseded_spec_commit_is_accounted_for(self):
        self.push_branch()
        self.land_the_newer_form()
        head = self.rebase_onto_trunk_as_the_trunk_has_it()
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(len(lost), 2)
        self.assertEqual(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost, 'main'), [])
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), head)
        self.assertEqual(self.on_origin(lc.copies_archive(self.branch, self.remote_sha)),
                         self.remote_sha)

    def test_a_unique_commit_beside_the_superseded_ones_is_still_carried(self):
        self.commit('notes.md', 'a person\'s notes\n', "docs: a person's own notes")
        self.push_branch()
        self.land_the_newer_form()
        head = self.rebase_onto_trunk_as_the_trunk_has_it()
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost, 'main'),
                         [self.remote_sha[:9]])
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertIn("carried 1 commit(s) the rebase dropped", line)
        self.assertEqual(self.sh(['rev-parse', 'HEAD~1'], self.repo), head)
        with open(os.path.join(self.repo, 'notes.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), "a person's notes\n")

    def test_a_unique_hunk_the_trunk_did_not_rewrite_is_never_accounted_for(self):
        # the branch also changed line 10, which the trunk's newer form left alone: the file's
        # branch change is not covered, so no commit touching it is the trunk's
        self.commit('spec.md', self.spec(c3='clause 3, revised', c7='clause 7, revised',
                                         l10='a person'), "docs(spec): a person's own line")
        self.push_branch()
        self.land_the_newer_form()
        head = self.rebase_onto_trunk_as_the_trunk_has_it()
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(len(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost,
                                                    'main')), 3)
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertFalse(ok, line)
        self.assertIn('would lose 3 commit', line)
        self.assertEqual(self.remote(), self.remote_sha)
        self.assertEqual(self.head(), head)

    def test_a_branch_change_the_trunk_never_touched_is_never_accounted_for(self):
        # the head dropped the branch's spec work and the trunk never rewrote it: lost work
        self.push_branch()
        head = self.rebase_onto_trunk_as_the_trunk_has_it()
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(len(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost,
                                                    'main')), 2)

    def test_a_head_that_edits_the_file_itself_is_not_the_trunks(self):
        self.push_branch()
        self.land_the_newer_form()
        self.rebase_onto_trunk_as_the_trunk_has_it()
        head = self.commit('spec.md', self.spec(c3='clause 3, revised', c7='the session',
                                                l1='line 1, the trunk'),
                           'docs(spec): the session edits the spec')
        lost = lc.lost_commits(self.repo, head, self.remote_sha, self.branch)
        self.assertEqual(len(lc.unaccounted_commits(self.repo, head, self.remote_sha, lost,
                                                    'main')), 2)


class PublishRewrittenOwnCommitsTest(_RebaseShape):
    """The other shapes publish must hold: a plain new commit (a fast-forward), the session's own
    commits rewritten by an amend or a squash after a rebase onto the trunk (published), a
    squash that lost work (refused, the trunk named), and a stale head on the remote's own
    base — origin/<branch> moved, the head did not — which is still rebased onto the remote by
    the factory, and told "rebase onto origin/<branch>" when that conflicts: the one case where
    that instruction is the right one."""

    def setUp(self):
        super().setUp()
        self.commit('a', 'own a\n', 'task(T-0338): own a')
        self.commit('b', 'b v1\n', 'task(T-0338): own b')
        self.push_branch()

    def test_a_new_commit_on_top_of_the_remote_is_a_fast_forward(self):
        head = self.commit('c', 'own c\n', 'task(T-0338): own c')
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), head)
        self.assertEqual(self.on_origin(lc.copies_archive(self.branch, self.remote_sha)), '')

    def test_an_amended_own_commit_after_a_rebase_is_published(self):
        self.land_on_trunk(('x', 'x\n', 'chore: x'))
        self.rebase_resolving({})
        self.commit('b', 'b v2\n', 'task(T-0338): own b')  # a new commit first, then fold it
        self.sh(['reset', '-q', '--soft', 'HEAD~1'], self.repo)
        self.sh(['commit', '-q', '--amend', '--no-edit'], self.repo)  # own b, amended
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), head)

    def test_own_commits_squashed_after_a_rebase_are_published(self):
        self.land_on_trunk(('x', 'x\n', 'chore: x'))
        self.rebase_resolving({})
        self.sh(['reset', '-q', '--soft', 'origin/main'], self.repo)
        self.sh(['commit', '-qm', 'task(T-0338): a and b, squashed'], self.repo)
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertEqual(self.remote(), head)

    def test_a_rewrite_that_dropped_a_commit_gets_it_carried_back(self):
        self.land_on_trunk(('x', 'x\n', 'chore: x'))
        self.rebase_resolving({})
        self.sh(['reset', '-q', '--hard', 'HEAD~1'], self.repo)  # own b gone
        self.sh(['commit', '-q', '--amend', '-m', 'task(T-0338): a, reworded'], self.repo)
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertTrue(ok, line)
        # `a, reworded` keeps its patch: only `own b` was dropped, and it alone is carried
        self.assertIn(f'carried 1 commit(s) the rebase dropped ({self.remote_sha[:9]} '
                      f'task(T-0338): own b)', line)
        self.assertEqual(self.remote(), self.head())
        self.assertEqual(self.sh(['rev-parse', 'HEAD~1'], self.repo), head)
        with open(os.path.join(self.repo, 'b'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'b v1\n')

    def test_a_dropped_commit_that_no_longer_applies_is_refused_with_the_trunk_named(self):
        self.land_on_trunk(('x', 'x\n', 'chore: x'))
        self.rebase_resolving({})
        self.sh(['reset', '-q', '--hard', 'HEAD~1'], self.repo)  # own b gone …
        self.commit('b', 'b rewritten\n', 'task(T-0338): b, another way')  # … and b redone
        head = self.head()
        ok, line = lc.publish(self.repo, self.branch, self.remote_sha, main='main')
        self.assertFalse(ok, line)
        self.assertEqual(self.remote(), self.remote_sha)
        self.assertEqual(self.head(), head)                    # every pick undone
        self.assertEqual(self.sh(['status', '--porcelain'], self.repo), '')
        self.assertFalse(self.mid_rebase())
        self.assertTrue(lc.stale_head(line), line)
        self.assertIn('would lose 1 commit', line)
        self.assertIn(f'{self.remote_sha[:9]} task(T-0338): own b', line)
        self.assertIn('rebase onto origin/main', line)
        self.assertNotIn(f'rebase onto origin/{self.branch}', line)

    def test_a_stale_head_on_the_remotes_base_is_rebased_onto_the_remote(self):
        foreign = self.push_from_elsewhere('d', 'd by a person\n', "fix: a person's own fix")
        self.commit('c', 'own c\n', 'task(T-0338): own c')
        ok, line = lc.publish(self.repo, self.branch, foreign, main='main')
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith(f'rebased onto origin/{self.branch} (+1 remote commits)'),
                        line)
        self.assertEqual(self.remote(), self.head())
        self.assertEqual(self.sh(['rev-parse', 'HEAD~1'], self.repo), foreign)

    def test_a_stale_head_on_the_remotes_base_whose_rebase_conflicts_is_told_the_branch(self):
        foreign = self.push_from_elsewhere('b', 'b by a person\n', "fix: a person's own fix")
        head = self.commit('b', 'b v2\n', 'task(T-0338): own b again')
        ok, line = lc.publish(self.repo, self.branch, foreign, main='main')
        self.assertFalse(ok, line)
        self.assertTrue(lc.rebase_conflict(line), line)
        self.assertIn('rebase conflicts in: b', line)
        self.assertEqual(self.head(), head)
        self.assertFalse(self.mid_rebase())
        self.assertEqual(self.remote(), foreign)
        text = lc.rebase_conflict_text(self.branch, line)
        self.assertIn(f'Rebase onto origin/{self.branch}', text)
        self.assertNotIn('rebase onto origin/main', text)


def _git_config(repo, key):
    return subprocess.run(['git', 'config', key], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


class RebaseShapeHousekeepingOffTests(_RebaseShape):
    """B-0403: CI red, 'the suite is hermetic' — ``push_from_elsewhere`` clones ``self.origin``
    right after ``setUp``'s own ``push_branch`` pushed into it; with housekeeping left on its
    defaults, that push can start a detached ``git gc --auto`` on the bare origin that is still
    repacking when the clone runs, and the clone's own copy of a pack file git is mid-rename on
    then fails (confirmed-live in CI run 37432074961: ``fatal: failed to copy file to
    '.../other-d/.git/objects/pack/.tmp-236278-pack-f9cc3cfba....pack': No such file or
    directory``, flagged non-blocking in docs/reviews/1-b-0356.md's I list since this shape,
    unlike :mod:`tests.gitfixture`'s ``_no_housekeeping``, never turns it off)."""

    def test_the_bare_origin_and_the_clone_never_start_background_housekeeping(self):
        for repo in (self.origin, self.repo):
            for key, value in (('gc.auto', '0'), ('gc.autoDetach', 'false'),
                               ('maintenance.auto', 'false')):
                self.assertEqual(_git_config(repo, key), value, f'{repo} {key}')


def _build_unpublished(root):
    """A bare origin and a clone on ``lane/x``, pushed and clean — the base every case in
    :class:`UnpublishedTests` mutates from."""
    def git(*args, cwd):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)
    origin, wt = os.path.join(root, 'origin.git'), os.path.join(root, 'wt')
    git('init', '-q', '--bare', '-b', 'main', origin, cwd=root)
    git('clone', '-q', origin, wt, cwd=root)
    for k, v in (('user.name', 'T'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
        git('config', k, v, cwd=wt)
    with open(os.path.join(wt, 'seed'), 'w') as f:
        f.write('base\n')
    git('add', '-A', cwd=wt)
    git('commit', '-qm', 'seed', cwd=wt)
    git('push', '-q', 'origin', 'HEAD:main', cwd=wt)
    git('checkout', '-q', '-b', 'lane/x', cwd=wt)
    git('push', '-q', '-u', 'origin', 'lane/x', cwd=wt)


UNPUBLISHED = Template(_build_unpublished, prefix='lifecycle_unpublished_')


class UnpublishedTests(unittest.TestCase):
    """T1 fence: :func:`lc.unpublished` is the one owner of "is this branch's work on origin" —
    over a real worktree rather than gathered :class:`lc.Evidence` — and :func:`health.push_gap`
    delegates to it byte for byte."""

    def setUp(self):
        self.root = UNPUBLISHED.fresh()
        self.wt = os.path.join(self.root, 'wt')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.wt, check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(self, *names):
        for name in names:
            with open(os.path.join(self.wt, name), 'w') as f:
                f.write('x\n')

    def test_a_clean_pushed_branch_is_published(self):
        self.assertEqual(lc.unpublished(self.wt, 'lane/x', main='main'), (True, ''))

    def test_uncommitted_files_are_counted(self):
        self.write('a', 'b')
        self.assertEqual(
            lc.unpublished(self.wt, 'lane/x', main='main'),
            (False, 'not pushed: 2 uncommitted file(s), 0 unpushed commit(s)'))

    def test_an_unpushed_commit_above_a_pushed_head_is_counted(self):
        self.write('c')
        self.git('add', '-A')
        self.git('commit', '-qm', 'local work')
        self.assertEqual(
            lc.unpublished(self.wt, 'lane/x', main='main'),
            (False, 'not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'))

    def test_a_branch_never_pushed_is_not_ok_even_with_a_clean_tree(self):
        self.git('checkout', '-q', '-b', 'lane/never')
        ok, detail = lc.unpublished(self.wt, 'lane/never', main='main')
        self.assertFalse(ok)
        self.assertEqual(detail, 'not pushed: 0 uncommitted file(s), 0 unpushed commit(s)')

    def test_a_detached_head_answers_no_branch(self):
        self.git('checkout', '-q', '--detach', 'HEAD')
        self.assertEqual(lc.unpublished(self.wt, '', main='main'), (False, 'no branch'))

    def test_detail_is_byte_identical_to_push_gap_over_gathered_evidence(self):
        self.write('a', 'b')
        ok, detail = lc.unpublished(self.wt, 'lane/x', main='main')
        self.assertEqual(detail, lc.push_gap(lc.Evidence(uncommitted=2, unpushed=0)))

    def test_health_push_gap_delegates_to_unpublished(self):
        from asf.workers import health
        self.assertEqual(health.push_gap(self.wt, 'lane/x', 'main'),
                          lc.unpublished(self.wt, 'lane/x', 'main'))
        self.write('a', 'b')
        self.assertEqual(health.push_gap(self.wt, 'lane/x', 'main'),
                          lc.unpublished(self.wt, 'lane/x', 'main'))


class WorktreeStatusSnapshot(unittest.TestCase):
    """§3.2: one ``git status --porcelain`` per worktree per *version* of that worktree — D7's
    key, read off the files with no subprocess of its own, over a real linked worktree.

    Built fresh per test rather than through :class:`tests.gitfixture.Template`: a linked
    worktree's own ``.git`` *file* (never ``config``/``gitdir``/``commondir``) carries an
    absolute path back to its admin dir, which the template's path rewrite does not touch — a
    copy would share one physical worktree admin dir with every other copy."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='lifecycle_status_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        origin, self.main_repo, self.wt = (os.path.join(self.root, n)
                                           for n in ('origin.git', 'main', 'wt'))
        self.git('init', '-q', '--bare', '-b', 'main', origin, cwd=self.root)
        self.git('clone', '-q', origin, self.main_repo, cwd=self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.git('config', k, v, cwd=self.main_repo)
        with open(os.path.join(self.main_repo, 'seed'), 'w') as f:
            f.write('seed\n')
        self.git('add', '-A', cwd=self.main_repo)
        self.git('commit', '-qm', 'seed', cwd=self.main_repo)
        self.git('push', '-q', 'origin', 'HEAD:main', cwd=self.main_repo)
        self.git('worktree', 'add', '-q', '-b', 'lane/x', self.wt, 'origin/main',
                 cwd=self.main_repo)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.wt, check=True,
                              capture_output=True, text=True).stdout.strip()

    @staticmethod
    def _count_status(spy):
        return sum(1 for c in spy.call_args_list if c.args[0] == ['status', '--porcelain'])

    def test_two_calls_on_an_unchanged_worktree_make_one_process(self):
        status = lc.WorktreeStatus()
        with mock.patch.object(lc, '_git', wraps=lc._git) as spy:
            first = status.lines(self.wt)
            second = status.lines(self.wt)
        self.assertEqual(self._count_status(spy), 1)
        self.assertEqual(first, second)
        self.assertEqual(first, [])

    def test_a_commit_between_two_calls_makes_a_second_process(self):
        status = lc.WorktreeStatus()
        with mock.patch.object(lc, '_git', wraps=lc._git) as spy:
            status.lines(self.wt)
            with open(os.path.join(self.wt, 'seed'), 'w') as f:
                f.write('changed\n')
            self.git('add', '-A')
            self.git('commit', '-qm', 'changed')
            status.lines(self.wt)
        self.assertEqual(self._count_status(spy), 2)

    def test_an_add_that_rewrites_the_index_makes_a_second_process(self):
        status = lc.WorktreeStatus()
        with mock.patch.object(lc, '_git', wraps=lc._git) as spy:
            status.lines(self.wt)
            with open(os.path.join(self.wt, 'untracked'), 'w') as f:
                f.write('new\n')
            self.git('add', 'untracked')
            lines = status.lines(self.wt)
        self.assertEqual(self._count_status(spy), 2)
        self.assertEqual(lines, ['A  untracked'])

    def test_a_path_that_is_not_a_worktree_is_asked_every_time_and_never_cached(self):
        status = lc.WorktreeStatus()
        not_a_repo = tempfile.mkdtemp(prefix='lifecycle_status_not_a_repo_')
        self.addCleanup(shutil.rmtree, not_a_repo, ignore_errors=True)
        with mock.patch.object(lc, '_git', wraps=lc._git) as spy:
            first = status.lines(not_a_repo)
            second = status.lines(not_a_repo)
        self.assertEqual(self._count_status(spy), 2)
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(status._cache, {})

    def test_two_different_worktrees_never_share_an_entry(self):
        other = os.path.join(self.root, 'other')
        self.git('worktree', 'add', '-q', '-b', 'lane/y', other, 'origin/main', cwd=self.main_repo)
        status = lc.WorktreeStatus()
        with mock.patch.object(lc, '_git', wraps=lc._git) as spy:
            status.lines(self.wt)
            status.lines(other)
            status.lines(self.wt)
            status.lines(other)
        self.assertEqual(self._count_status(spy), 2)
        self.assertEqual(len(status._cache), 2)

    def test_gather_through_the_snapshot_sees_the_mid_pass_commit(self):
        # P5: gather on a worktree with an uncommitted file, then commit_leftovers, then gather
        # again through the same snapshot — the mutation is seen, not answered from a stale count
        with open(os.path.join(self.wt, 'leftover'), 'w') as f:
            f.write('half done\n')
        status = lc.WorktreeStatus()
        run = {'worktree': self.wt, 'branch': 'lane/x'}
        ev = lc.gather(None, run, status=status)
        self.assertEqual(ev.uncommitted, 1)
        ok, line = lc.commit_leftovers(self.wt, 'lane/x')
        self.assertTrue(ok, line)
        ev = lc.gather(None, run, status=status)
        self.assertEqual(ev.uncommitted, 0)

    def test_gather_with_no_status_runs_its_own_exactly_as_every_existing_caller_did(self):
        with open(os.path.join(self.wt, 'leftover'), 'w') as f:
            f.write('half done\n')
        run = {'worktree': self.wt, 'branch': 'lane/x'}
        with mock.patch.object(lc, '_git', wraps=lc._git) as spy:
            ev = lc.gather(None, run)
        self.assertEqual(ev.uncommitted, 1)
        self.assertEqual(self._count_status(spy), 1)


class GatherHasCommitsOnAFreshCheckoutOfAPushedBranchTests(unittest.TestCase):
    """B-0584: a branch a prior pass already pushed past the trunk — a cloud session's push, or a
    worktree this one never saw before — reads `has_commits=False` the first time *this*
    worktree checks it out: its own reflog holds nothing but `branch: Created from
    origin/<branch>` (the filtered entry), though the branch itself carries real work. `judge`
    then reads a report that truthfully says there is nothing new to commit as an empty branch,
    and F-0097's replan parked twice in 24h on work that was already on origin."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='lifecycle_gather_fresh_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        origin, main_repo, self.wt = (os.path.join(self.root, n)
                                      for n in ('origin.git', 'main', 'wt'))
        self.git('init', '-q', '--bare', '-b', 'main', origin, cwd=self.root)
        self.git('clone', '-q', origin, main_repo, cwd=self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            self.git('config', k, v, cwd=main_repo)
        self._commit(main_repo, 'seed')
        self.git('push', '-q', 'origin', 'HEAD:main', cwd=main_repo)
        # a prior pass pushed its work straight to the branch, past the trunk
        self.git('checkout', '-q', '-b', 'lane/x', cwd=main_repo)
        self._commit(main_repo, 'the work')
        self.git('push', '-q', 'origin', 'lane/x', cwd=main_repo)
        self.git('checkout', '-q', 'main', cwd=main_repo)
        self.git('branch', '-q', '-D', 'lane/x', cwd=main_repo)
        # this worktree checks the branch out for the first time, after that commit is already
        # on origin — its own reflog is just the branch's creation
        self.git('worktree', 'add', '-q', '-b', 'lane/x', self.wt, 'origin/lane/x', cwd=main_repo)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.wt, check=True,
                              capture_output=True, text=True).stdout.strip()

    def _commit(self, repo, name):
        with open(os.path.join(repo, name), 'w') as f:
            f.write('x\n')
        self.git('add', '-A', cwd=repo)
        self.git('commit', '-qm', name, cwd=repo)

    def test_a_fresh_checkout_of_a_branch_already_ahead_of_trunk_has_commits(self):
        reflog = self.git('reflog', 'show', '--format=%gs', 'refs/heads/lane/x')
        self.assertEqual(reflog, 'branch: Created from origin/lane/x')
        ev = lc.gather(None, {'worktree': self.wt, 'branch': 'lane/x'})
        self.assertTrue(ev.has_commits, 'the branch is ahead of main on origin')

    def test_judge_does_not_call_it_an_empty_branch(self):
        run = {'worktree': self.wt, 'branch': 'lane/x', 'kind': 'replan'}
        ev = lc.gather(None, run)
        ev.result = OK
        self.assertEqual(lc.judge(run, ev), lc.FINISHED)

    def test_a_branch_truly_never_pushed_past_trunk_still_has_no_commits(self):
        wt2 = os.path.join(self.root, 'wt2')
        self.git('worktree', 'add', '-q', '-b', 'lane/never', wt2, 'origin/main',
                 cwd=os.path.join(self.root, 'main'))
        ev = lc.gather(None, {'worktree': wt2, 'branch': 'lane/never'})
        self.assertFalse(ev.has_commits)
