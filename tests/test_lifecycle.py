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
            f.write(json.dumps({'job': 'correct-b-0001', 'pid': 2, 'started': 't4', 'item': 'B-0001', 'branch': 'b'}) + '\n')
        self.assertEqual(lc.derive(run, lc.Evidence(), path=path).name, lc.CORRECTED)
        self.assertEqual(lc.corrections(path), {})
        self.assertEqual(lc.inflight(path), [{'item': 'B-0001', 'kind': None, 'account': None,
                                              'job': 'correct-b-0001', 'started': 't4'}])
        self.assertEqual(lc.attempts(path), {'B-0001': 2})


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
        self.assertEqual(lc.may_launch(self.path, 'fix-b-0001', self.wt), (False, f'worktree already exists: {self.wt}'))
        self.write({'job': 'fix-b-0001', 'pid': 1, 'started': 't1', 'worktree': self.wt})
        self.assertFalse(lc.may_launch(self.path, 'fix-b-0001', self.wt)[0])
        self.write({'job': 'fix-b-0001', 'ended': 't2', 'end_reason': 'failed: not pushed: 3 uncommitted file(s), 0 unpushed commit(s)'})
        self.assertEqual(lc.may_launch(self.path, 'fix-b-0001', self.wt), (True, ''))
        # another job may take over the ended run's worktree (a correction on the same branch)
        self.assertEqual(lc.may_launch(self.path, 'correct-b-0001', self.wt), (True, ''))
        self.write({'job': 'correct-b-0001', 'pid': 2, 'started': 't3', 'worktree': self.wt})
        # …and while it runs, nobody else does — by worktree, not by directory name
        self.assertFalse(lc.may_launch(self.path, 'fix-b-0001', self.wt)[0])
        self.assertTrue(lc.may_launch(self.path, 'other', os.path.join(self.d, 'wt', 'other'))[0])

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


if __name__ == '__main__':
    unittest.main()
