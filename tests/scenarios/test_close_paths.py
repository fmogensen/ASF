"""Every close path × every host behaviour, on one world (W6-PR0a; the harness is
``tests/scenarios/__init__.py``).

:data:`ROWS` is the table: ``(path, behaviour, expected, gap, edit)``. ``expected`` is one of

* ``none`` — the path closes nothing: no landing stamp, no close it writes;
* ``decoy`` — the path closes the Task on the trunk commit covering its ``writes:`` (the
  REPORT's sha), never on the PR's own head: a PR the host closed unmerged is neither open work
  nor a landing;
* ``closes`` — the path closes the Task (the positive control: the host merged PR #1).

``gap`` names the plan item that fixes a row the code still fails today (``W4-PR2``: Unknown
never closes; ``W4-PR5``: a voided landing is no merge fact). Such a row asserts the defect is
still there, so the PR that fixes it turns the row red here and drops the marker in the same
change — the row is that PR's acceptance. A new path or behaviour is one entry in the harness
and rows here; one test method is generated per row.
"""
import json
import re
import subprocess
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.scenarios…` does not
    import scenarios as S
except ImportError:  # pragma: no cover - import shape only
    from tests import scenarios as S

CB = 'trunkclose.closes_before_launch'
CP = 'trunkclose.close_parked'
RC = 'relaunch.assess → step_wave park/close'
IN = 'ingest.derive via merge_facts'
AP = 'approvals.close_landed'
GR = 'groom/policy.close_*'
RL = 'release notes (I12)'
MF = 'evidence.merge_facts'

#: ``(path, behaviour, expected, gap, edit)``
ROWS = (
    # the wave's trunk check before a launch: the open PR is the Task's own unmerged work
    (CB, 'open', 'none', None, None),
    (CB, 'rate-limit', 'none', 'W4-PR2', None),
    (CB, 'close-unmerged', 'decoy', None, None),
    (CB, 'merged', 'decoy', None, None),
    # a park carrying the verified evidence
    (CP, 'open', 'none', None, None),
    (CP, 'rate-limit', 'none', 'W4-PR2', None),
    (CP, 'close-unmerged', 'decoy', None, None),
    (CP, 'merged', 'decoy', None, None),
    # the relaunch cap: park, or close on evidence that re-verifies with the host
    (RC, 'open', 'none', None, None),
    (RC, 'rate-limit', 'none', 'W4-PR2', None),
    (RC, 'close-unmerged', 'decoy', None, None),
    (RC, 'merged', 'decoy', None, None),
    # the record's ingest: a PR closed unmerged is no landing, a refused host no answer
    (IN, 'open', 'none', None, None),
    (IN, 'rate-limit', 'none', None, None),
    (IN, 'close-unmerged', 'none', None, None),
    (IN, 'merged', 'closes', None, None),
    # an open hold is closed only when the record says the Task landed
    (AP, 'open', 'none', None, None),
    (AP, 'rate-limit', 'none', None, None),
    (AP, 'close-unmerged', 'none', None, None),
    (AP, 'merged', 'closes', None, None),
    # the groom never closes a decided Task a session has run on, whatever the host says
    (GR, 'open', 'none', None, None),
    (GR, 'rate-limit', 'none', None, None),
    (GR, 'close-unmerged', 'none', None, None),
    (GR, 'merged', 'none', None, None),
    # the release note lists under "landed" only what the record calls Resolved or Closed
    (RL, 'open', 'none', None, None),
    (RL, 'rate-limit', 'none', None, None),
    (RL, 'close-unmerged', 'none', None, None),
    (RL, 'merged', 'closes', None, None),
    # the lane's merge facts are run lines: the host's word alone never writes one
    (MF, 'open', 'none', None, None),
    (MF, 'rate-limit', 'none', None, None),
    (MF, 'close-unmerged', 'none', None, None),
    (MF, 'merged', 'none', None, None),
    # the voided landing: a reset names the landing's (pr, head) — no merge fact from it, even
    # while the host says the PR merged
    (MF, 'merged', 'none', None, 'voided-landing'),
)


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')


def _on_trunk(f, sha):
    return subprocess.run(['git', 'merge-base', '--is-ancestor', sha, 'main'],
                          cwd=f.repo_origin, capture_output=True).returncode == 0


class ClosePaths(unittest.TestCase):
    """One generated test per row of :data:`ROWS` (``test_<path>__<behaviour>[__<edit>]``)."""

    def check(self, path, behaviour, expected, gap, edit):
        scenario, o = S.run(path, behaviour, edit)
        self.assertEqual(scenario.host_behaviour, behaviour)
        said = f'{path} × {behaviour}{" + " + edit if edit else ""}: {o}\n' + '\n'.join(o.lines)
        holds = (not o.closed) if expected == 'none' else (
            o.closed and (expected != 'decoy' or o.stamp == S.WORLD.decoy))
        if gap:
            self.assertFalse(holds, f'{gap} has landed — this row now holds: drop its gap marker.'
                                    f'\n{said}')
            return
        self.assertTrue(holds, f'expected {expected}\n{said}')


def _make(row):
    def test(self):
        self.check(*row)
    path, behaviour, expected, gap, edit = row
    test.__doc__ = f'{path} × {behaviour}{" + " + edit if edit else ""} → {expected}' + (
        f' (still fails: {gap})' if gap else '')
    return test


for _row in ROWS:
    _name = f'test_{_slug(_row[0])}__{_slug(_row[1])}' + (f'__{_slug(_row[4])}' if _row[4] else '')
    assert not hasattr(ClosePaths, _name), _name
    setattr(ClosePaths, _name, _make(_row))


class Table(unittest.TestCase):
    """The table itself: every path meets every behaviour, and the rate-limit row closes nothing
    anywhere once its gaps land."""

    def test_every_path_meets_every_behaviour(self):
        seen = {(p, b) for p, b, _e, _g, edit in ROWS if not edit}
        missing = [(p, b) for p in S.PATHS for b in S.BEHAVIOURS if (p, b) not in seen]
        self.assertEqual(missing, [])

    def test_the_rate_limit_rows_expect_no_close_anywhere(self):
        self.assertEqual([r for r in ROWS if r[1] == 'rate-limit' and r[2] != 'none'], [])

    def test_the_gaps_are_named_plan_items(self):
        for row in ROWS:
            self.assertIn(row[3], (None, 'W4-PR2', 'W4-PR5'), row)


class TheWorld(unittest.TestCase):
    """What every row stands on, checked once: the fake host's two behaviours do what they say."""

    def test_the_closed_prs_head_never_reaches_the_trunk(self):
        f = S.WORLD.fork()
        S.BEHAVIOURS['close-unmerged'](f)
        pr = f.prs(S.PR_HEAD)[0]
        self.assertEqual((pr['state'], pr['mergedAt'], pr['mergeCommit']), ('CLOSED', None, None))
        self.assertTrue(f.on_origin(S.PR_HEAD))  # the host keeps the branch
        self.assertFalse(_on_trunk(f, S.WORLD.hand))
        self.assertTrue(_on_trunk(f, S.WORLD.decoy))

    def test_the_closed_pr_reads_as_the_recorded_host_shows_one(self):
        f = S.WORLD.fork()
        S.BEHAVIOURS['close-unmerged'](f)
        _rc, out, _err = S.contracts.load('pr-closed-unmerged', 'pr-view')
        p = f.gh('pr', 'view', '1', '-R', S.SLUG, '--json', 'state,mergedAt,mergeCommit')
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, json.loads(out)))

    def test_a_rate_limited_host_refuses_every_call_but_its_own_switch(self):
        f = S.WORLD.fork()
        S.BEHAVIOURS['rate-limit'](f)
        rc, _out, err = S.contracts.load('rate-limit', 'run-list')
        p = f.gh('pr', 'list', '-R', S.SLUG, '--state', 'all', '--json', 'number')
        self.assertEqual((p.returncode, p.stderr), (rc, err))
        self.assertIn('API rate limit exceeded', p.stderr)
        self.assertEqual(f.gh('e2e', 'rate-limit', 'off').returncode, 0)
        p = f.gh('pr', 'list', '-R', S.SLUG, '--state', 'all', '--json', 'number')
        self.assertEqual((p.returncode, p.stdout.strip()), (0, '[{"number": 1}]'))


if __name__ == '__main__':
    unittest.main()
