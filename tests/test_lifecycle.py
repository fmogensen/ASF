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
import itertools
import json
import os
import random
import shutil
import subprocess
import tempfile
import unittest

from asf.workers import lifecycle as lc

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
        self.assertEqual(lc.derive(at_cap, lc.Evidence()).name, lc.ADJUDICATE)
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

    def test_b0128_an_adjudicate_run_does_not_answer_the_correction(self):
        """B-0128: an adjudicate session rules, it does not correct — the branch stays held (not
        bounced BACK → PUSHED, restarting review) and the correction is marked ``settled`` once
        the ruling is in, so the feeder asks for no second adjudicate session over the same hold."""
        lines = [{'job': 'a', 'pid': 1, 'started': 't1', 'item': 'B-0001', 'branch': 'b',
                  'ended': 't2', 'end_reason': 'finished'},
                 {'job': 'a', 'rounds': 3, 'correction': {'kind': 'review', 'text': 'x', 'at': 't3'}}]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 's.jsonl')
        with open(path, 'w') as f:
            f.write('\n'.join(json.dumps(ln) for ln in lines) + '\n')
        run = lc.latest(path)['a']
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.ADJUDICATE)
        self.assertFalse(lc.corrections(path)['B-0001']['settled'])
        with open(path, 'a') as f:
            f.write(json.dumps({'job': 'adjudicate-b-0001', 'item': 'B-0001', 'branch': 'b',
                                'kind': 'adjudicate', 'pid': os.getpid(), 'started': 't4',
                                'ended': 't5', 'end_reason': 'finished'}) + '\n')
        # still ADJUDICATE, not CORRECTED: the ruling did not touch the branch
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.ADJUDICATE)
        self.assertEqual(lc.corrections(path)['B-0001']['rounds'], 3)
        self.assertTrue(lc.corrections(path)['B-0001']['settled'])


class HoldInvariants(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 's.jsonl')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def test_rounds_climb_to_the_cap_then_adjudicate_then_flag(self):
        # B-0048: the counter never passes the cap; the second hold at the cap flags the operator
        run = {'job': 'a', 'item': 'B-0001', 'branch': 'fix/B-0001', 'pid': 1, 'started': 't'}
        self.write(run)
        for n in range(1, lc.ROUND_CAP + 1):
            fields, line = lc.hold(self.path, run, 'gate', 'FAIL', f't{n}')
            self.assertEqual(fields['rounds'], n)
            self.assertEqual(line, f'held fix/B-0001: FAIL — back to its session (round {n})')
            self.write(dict(fields, job='a'))
        fields, line = lc.hold(self.path, run, 'gate', 'FAIL', 'tx')
        self.assertNotIn('rounds', fields)
        self.assertTrue(fields['correction']['at_cap'])
        self.assertNotIn('operator_flagged', fields)
        self.assertEqual(line, 'held fix/B-0001: FAIL — adjudicate pending')
        self.write(dict(fields, job='a'))
        fields, _ = lc.hold(self.path, run, 'gate', 'FAIL', 'ty')
        self.assertEqual(fields.get('operator_flagged'), 1)
        self.assertEqual(lc.rounds_of(self.path, 'B-0001'), lc.ROUND_CAP)
        self.assertEqual(lc.derive(lc.latest(self.path)['a'], lc.Evidence()).name, lc.ADJUDICATE)

    def test_rounds_count_over_every_job_of_the_item(self):
        self.write({'job': 'a', 'item': 'B-0001', 'branch': 'b', 'pid': 1, 'started': 't1', 'rounds': 2},
                   {'job': 'c', 'item': 'B-0001', 'branch': 'b', 'pid': 2, 'started': 't2'})
        fields, _ = lc.hold(self.path, lc.latest(self.path)['c'], 'conflict', 'x', 't3')
        self.assertEqual(fields['rounds'], 3)


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
        # B-0019: finished, pushed, with commits, reached the trunk — and nothing short of it
        fin = {'job': 'j', 'ended': 't', 'end_reason': 'finished', 'pid': 1}
        full = lc.Evidence(remote_sha='s', head_on_remote=True, has_commits=True, in_trunk=True)
        self.assertEqual(lc.reap_verdict(fin, full, 'main', dead), ('reapable', 'ended'))
        for short in (dict(remote_sha=''), dict(head_on_remote=False), dict(has_commits=False),
                      dict(in_trunk=False), dict(uncommitted=1)):
            ev = lc.Evidence(**dict(dataclassfields(full), **short))
            self.assertEqual(lc.reap_verdict(fin, ev, 'main', dead)[0], 'keep', short)


def dataclassfields(ev):
    return {f: getattr(ev, f) for f in ev.__dataclass_fields__}


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
        # rebased onto the trunk, and origin gained a commit the rebase never saw
        newer = self._push_from_elsewhere('newer-work')
        ok, line = lc.publish(self.repo, 'fix/B-9999', newer, main='main')
        self.assertTrue(ok, line)
        self.assertTrue(self._on_remote(newer))

    def test_a_rebase_holding_copies_of_every_remote_commit_still_publishes(self):
        newer = self._push_from_elsewhere('newer-work')
        self.sh(['fetch', '-q', 'origin', 'fix/B-9999'], self.repo)
        self.sh(['rebase', '-q', 'origin/fix/B-9999'], self.repo)
        self.sh(['rebase', '-q', 'origin/main'], self.repo)
        ok, line = lc.publish(self.repo, 'fix/B-9999', newer, main='main')
        self.assertTrue(ok, line)

    def test_a_stale_head_refusal_is_not_a_hook_refusal(self):
        # never retried as a push: the branch is held for a rebase onto the remote head
        line = ('publish fix/B-9999 refused: would lose 1 commit(s) on origin/fix/B-9999 '
                '(abc123456) — rebase onto origin/fix/B-9999, then push')
        self.assertIsNone(lc.push_failure(line))
        self.assertTrue(lc.stale_head(line))

    def test_a_branch_never_pushed_is_counted_against_the_trunk(self):
        self.assertEqual(lc.unpushed_commits(self.repo, '', 'main'), 1)


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

    def test_the_failing_classes_are_all_but_finished(self):
        self.assertEqual(set(lc.FAILING_CLASSES), set(lc.OUTCOME_CLASSES) - {lc.FINISHED})

    def test_the_vocabulary_is_the_specs_in_the_specs_order(self):
        self.assertEqual(lc.OUTCOME_CLASSES, (
            'finished', 'not pushed', 'empty branch', 'dead pid', 'pushed after stop',
            'unpushed work', 'unknown model', 'auth', 'quota-exhausted', 'permission', 'hook refused',
            'network error', 'other'))


if __name__ == '__main__':
    unittest.main()
