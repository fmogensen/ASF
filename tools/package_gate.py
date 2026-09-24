#!/usr/bin/env python3
"""tools/package_gate.py — the fix package's release gate, by code (plan §11).

The review checklist (plan §10, R1–R26) is verified by tests, never by a reviewer's say-so.
This gate runs, in order:

1. the full suite (``tools/run_tests.py``);
2. both lints (``tools/check_generic.sh``, ``tools/check_conventions.sh``);
3. ``tests/test_e2e_lane.py`` and every test the R-line table maps, each module in its own
   hermetic process (as the suite runs it), recording every test's own outcome;
4. the checked-in table ``docs/plans/fix-package-rlines.md``: every R-line R1..R26 appears
   once, as ``test`` (its tests must exist and pass in this run), ``pending`` (owned by a
   stream not landed yet — a gap until its tests pass), ``check`` (a named check of the
   branch's own git history, run here), ``pending-live`` (a live step no suite can witness —
   a gap until its recorded result exists: for R21, the smoke record
   ``docs/plans/fix-package-smoke.txt`` that ``tools/smoke_isolated_session.sh`` writes on
   PASS) or ``n/a`` (a reason, no test).

Any gap — a red suite or lint, an R-line missing, a mapped test that does not exist, did not
run, failed, was skipped or is still an expected failure — is printed and the exit status is 1.
The tag waits for exit 0. Python 3 stdlib only.

    python3 tools/package_gate.py              the gate
    python3 tools/package_gate.py --no-suite   skip steps 1–2 (a quick look at the table only)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TABLE = os.path.join(ROOT, 'docs', 'plans', 'fix-package-rlines.md')
#: R21's recorded result: written by ``tools/smoke_isolated_session.sh`` on ALL PASS, committed.
SMOKE_RECORD = os.path.join(ROOT, 'docs', 'plans', 'fix-package-smoke.txt')
SMOKE_KEYS = ('date', 'account', 'sha', 'result')
E2E_MODULE = 'tests.test_e2e_lane'
HOME_MODULE = 'tests.test_00_home'
R_LINES = tuple(f'R{n}' for n in range(1, 27))
STATUSES = ('test', 'pending', 'check', 'pending-live', 'n/a')
NO_TEST = ('pending-live', 'n/a')  # a reason, never a test id
ROW_RE = re.compile(r'^\|\s*(R\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$')
TEST_ID_RE = re.compile(r'`([^`]+)`')
PASSED = 'passed'


# ---- the table -----------------------------------------------------------------------------

def dotted(test_id):
    """``tests/test_x.py::Class::test_y`` → ``tests.test_x.Class.test_y`` (a dotted id passes)."""
    if '::' not in test_id:
        return test_id
    path, *rest = test_id.split('::')
    return '.'.join([path[:-3].replace('/', '.') if path.endswith('.py') else path] + rest)


def read_table(path=TABLE):
    """``{R-line: {'status', 'tests': [dotted ids], 'why'}}`` and the problems of the table
    itself (a duplicate, a bad status)."""
    rows, problems = {}, []
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = ROW_RE.match(line.rstrip('\n'))
            if not m:
                continue
            rline, status, tests, why = m.groups()
            status = status.strip().lower()
            if rline in rows:
                problems.append(f'{rline}: listed twice in the table')
                continue
            if status not in STATUSES:
                problems.append(f'{rline}: status {status!r} is not one of {", ".join(STATUSES)}')
                continue
            named = TEST_ID_RE.findall(tests)
            rows[rline] = {'status': status,
                           'tests': named if status == 'check' else [dotted(t) for t in named],
                           'why': why.strip()}
    return rows, problems


def table_gaps(rows):
    """What the table itself leaves open: an R-line missing, an ``n/a``/``pending-live`` with no
    reason or with tests, a ``test``/``pending``/``check`` line naming none, an unknown check."""
    gaps = []
    for rline in R_LINES:
        row = rows.get(rline)
        if row is None:
            gaps.append(f'{rline}: not in the table')
        elif row['status'] in NO_TEST and (not row['why'] or row['tests']):
            gaps.append(f'{rline}: {row["status"]} needs a reason and no test')
        elif row['status'] not in NO_TEST and not row['tests']:
            gaps.append(f'{rline}: {row["status"]} names no test')
        elif row['status'] == 'check':
            gaps += [f'{rline}: unknown check {c!r}' for c in row['tests']
                     if c.split(':', 1)[0] not in CHECKS]
    return gaps


# ---- the checks of the branch's own history (status ``check``) ------------------------------

def _git(args):
    p = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout


def check_ancestor(sha, git=_git):
    """``ancestor:<sha>`` — the branch contains ``sha`` (R15: cut from a base including it)."""
    rc, _o = git(['merge-base', '--is-ancestor', sha, 'HEAD'])
    return None if rc == 0 else f'{sha} is not an ancestor of HEAD'


def check_merged_not_rebased(cut, git=_git, main='origin/main'):
    """``merged-not-rebased:<cut>`` — R18: main reached the branch only through merges. A rebase
    onto main puts main's commits since the cut on the branch's first-parent chain; a merge
    brings them in through its second parent, never onto that chain."""
    rc, chain = git(['rev-list', '--first-parent', f'{cut}..HEAD'])
    rc2, trunk = git(['rev-list', f'{cut}..{main}'])
    if rc or rc2:
        return f'cannot read {cut}..HEAD or {cut}..{main}'
    on_chain = set(chain.split()) & set(trunk.split())
    if not on_chain:
        return None
    return (f'{len(on_chain)} commit(s) of {main} on the first-parent chain (rebased): '
            + ', '.join(sorted(c[:7] for c in on_chain)[:5]))


CHECKS = {'ancestor': check_ancestor, 'merged-not-rebased': check_merged_not_rebased}


def check_gaps(rows, git=_git):
    """Per ``check`` line, every named check that does not hold."""
    gaps = []
    for rline in R_LINES:
        row = rows.get(rline)
        if not row or row['status'] != 'check':
            continue
        for name in row['tests']:
            kind, _s, arg = name.partition(':')
            fn = CHECKS.get(kind)
            why = fn(arg, git=git) if fn else f'unknown check {name!r}'
            if why:
                gaps.append(f'{rline}: {name} — {why}')
    return gaps


# ---- the live steps (status ``pending-live``) ------------------------------------------------

def read_smoke(path=SMOKE_RECORD):
    """``{key: value}`` of the smoke record, or None when there is none."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep and not line.lstrip().startswith('#'):
            out[key.strip()] = value.strip()
    return out


def smoke_gap(path=SMOKE_RECORD, git=_git):
    """Why the recorded live smoke does not count, or None: it must exist, carry every key of
    :data:`SMOKE_KEYS`, say ``result: PASS``, and name a sha this branch contains (the code the
    smoke ran is the code being tagged, or older)."""
    rec = read_smoke(path)
    if rec is None:
        return (f'no recorded live smoke ({os.path.basename(path)}): run '
                f'tools/smoke_isolated_session.sh <product> [account] and commit what it writes')
    missing = [k for k in SMOKE_KEYS if not rec.get(k)]
    if missing:
        return f'the smoke record lacks {", ".join(missing)}'
    if rec['result'] != 'PASS':
        return f"the smoke record says result: {rec['result']}"
    if check_ancestor(rec['sha'], git=git):
        return f"the smoke ran {rec['sha'][:12]}, which this branch does not contain"
    return None


def live_gaps(rows, path=SMOKE_RECORD, git=_git):
    """Per ``pending-live`` line, a gap until its recorded result exists (R21: the smoke)."""
    gaps = []
    for rline in R_LINES:
        row = rows.get(rline)
        if row and row['status'] == 'pending-live':
            why = smoke_gap(path, git)
            if why:
                gaps.append(f'{rline}: pending-live — {why}')
    return gaps


def line_gaps(rows, outcomes):
    """Per R-line, the mapped tests that did not pass this run. A ``pending`` line is a gap
    until every one of its tests passes; then it is done, and says so."""
    gaps, notes = [], []
    for rline in R_LINES:
        row = rows.get(rline)
        if row is None or row['status'] in NO_TEST + ('check',):
            continue
        bad = [(t, outcomes.get(t, 'did not run')) for t in row['tests']
               if outcomes.get(t) != PASSED]
        if not bad:
            if row['status'] == 'pending':
                notes.append(f'{rline}: pending, but every mapped test passes — mark it test')
            continue
        head = f'{rline}: {"pending (lane) — " if row["status"] == "pending" else ""}'
        gaps.append(head + '; '.join(f'{t} {o}' for t, o in bad))
    return gaps, notes


# ---- running the tests ---------------------------------------------------------------------

class _Outcomes(unittest.TestResult):
    """Every test's own outcome by its id."""

    def __init__(self):
        super().__init__()
        self.by_id = {}

    def addSuccess(self, test):
        self.by_id[test.id()] = PASSED

    def addFailure(self, test, err):
        self.by_id[test.id()] = 'failed'

    def addError(self, test, err):
        self.by_id[test.id()] = 'error'

    def addSkip(self, test, reason):
        self.by_id[test.id()] = f'skipped ({reason})'

    def addExpectedFailure(self, test, err):
        self.by_id[test.id()] = 'expected failure'

    def addUnexpectedSuccess(self, test):
        self.by_id[test.id()] = 'unexpected success'


def child(out_path, names):
    """In a hermetic child: load and run ``names`` (a module or a test id each), write
    ``{id: outcome}`` — a name that does not load is ``missing``."""
    __import__(HOME_MODULE)
    loader = unittest.TestLoader()
    suite, outcomes = unittest.TestSuite(), {}
    for name in names:
        try:
            loaded = loader.loadTestsFromName(name)
        except Exception as e:  # noqa: BLE001 — a name that does not load is the finding
            outcomes[name] = f'missing ({type(e).__name__})'
            continue
        if loaded.countTestCases() == 0:
            outcomes[name] = 'missing (no test)'
            continue
        if any(type(t).__name__ == '_FailedTest' for t in _flatten(loaded)):
            outcomes[name] = 'missing'
            continue
        suite.addTest(loaded)
    result = _Outcomes()
    with open(os.devnull, 'w') as sink:
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = sink
        try:
            suite.run(result)
        finally:
            sys.stdout, sys.stderr = stdout, stderr
    outcomes.update(result.by_id)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(outcomes, f)
    return 0


def _flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _flatten(t)
        else:
            yield t


def _module_of(name):
    parts = name.split('.')
    return '.'.join(parts[:2]) if parts[0] == 'tests' else parts[0]


def run_tests(names, out=print):
    """Run ``names`` grouped by module, one hermetic process per module, all at once (bounded by
    the cores). ``{test id: outcome}``."""
    from asf import hermetic
    groups = {}
    for name in names:
        groups.setdefault(_module_of(name), []).append(name)
    for module, members in groups.items():
        if module in members:  # the whole module runs: its named tests are in it, run once
            groups[module] = [module]
    tmp = tempfile.mkdtemp(prefix='asf-gate-')
    procs, outcomes = [], {}
    try:
        for i, (module, members) in enumerate(sorted(groups.items())):
            env = hermetic.build(worktree=ROOT)
            env['ASF_TESTS_HOME'] = os.path.join(tmp, f'home-{i}')
            os.makedirs(env['ASF_TESTS_HOME'])
            path = os.path.join(tmp, f'out-{i}.json')
            procs.append((module, members, path, subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), '--child', path, *members],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)))
        for module, members, path, proc in procs:
            output = proc.communicate()[0]
            try:
                with open(path, encoding='utf-8') as f:
                    outcomes.update(json.load(f))
            except (OSError, ValueError):
                out(f'--- {module}: the run wrote no outcomes (rc {proc.returncode})')
                out(output.rstrip())
                outcomes.update({m: 'did not run' for m in members})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return outcomes


def _step(name, argv, out):
    out(f'== {name}: {" ".join(argv)}')
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    tail = (p.stdout + p.stderr).rstrip().splitlines()[-3:]
    for line in tail:
        out(f'   {line}')
    return p.returncode


def gate(no_suite=False, table=TABLE, out=print, smoke=SMOKE_RECORD):
    """The gate. Returns the exit status."""
    gaps = []
    if not no_suite:
        if _step('suite', [sys.executable, 'tools/run_tests.py'], out):
            gaps.append('the full suite is red')
        for lint in ('tools/check_generic.sh', 'tools/check_conventions.sh'):
            if _step('lint', ['bash', lint], out):
                gaps.append(f'{lint} is red')
    rows, problems = read_table(table)
    gaps += problems + table_gaps(rows)
    mapped = sorted({t for r in rows.values() if r['status'] in ('test', 'pending')
                     for t in r['tests']})
    out(f'== tests: {E2E_MODULE} and {len(mapped)} mapped test(s)')
    outcomes = run_tests([E2E_MODULE] + mapped, out)
    e2e = {k: v for k, v in outcomes.items() if k.startswith(E2E_MODULE + '.')}
    red = sorted(k for k, v in e2e.items() if v not in (PASSED, 'expected failure'))
    if red:
        gaps.append(f'{E2E_MODULE}: red — ' + ', '.join(red))
    xfail = sorted(k for k, v in e2e.items() if v == 'expected failure')
    out(f'   {sum(v == PASSED for v in e2e.values())} passed, {len(xfail)} expected failure(s), '
        f'{len(red)} red')
    lgaps, notes = line_gaps(rows, outcomes)
    cgaps, vgaps = check_gaps(rows), live_gaps(rows, smoke)
    gaps += lgaps + cgaps + vgaps
    out('== R-lines')
    for rline in R_LINES:
        row = rows.get(rline) or {'status': 'missing', 'tests': []}
        failing = any(g.startswith(f'{rline}:') for g in cgaps + vgaps)
        if row['status'] == 'n/a':
            mark = 'n/a'
        elif row['status'] == 'check':
            mark = 'check fails' if failing else 'done'
        elif row['status'] == 'pending-live':
            mark = 'pending-live' if failing else 'recorded'
        else:
            mark = ('done' if all(outcomes.get(t) == PASSED for t in row['tests']) and row['tests']
                    else row['status'])
        out(f'   {rline:<4} {mark}')
    for note in notes:
        out(f'note: {note}')
    if gaps:
        out(f'== GAPS ({len(gaps)})')
        for g in gaps:
            out(f'   {g}')
        out('package gate: FAILED')
        return 1
    out('package gate: OK')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='package_gate.py', description=__doc__.split('\n\n')[0])
    p.add_argument('--no-suite', action='store_true', help='skip the full suite and the lints')
    p.add_argument('--table', default=TABLE, help='the R-line table (default: %(default)s)')
    p.add_argument('--smoke', default=SMOKE_RECORD,
                   help="R21's recorded live smoke (default: %(default)s)")
    p.add_argument('--child', nargs='+', metavar=('OUT', 'NAME'), help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.child:
        return child(args.child[0], args.child[1:])
    return gate(no_suite=args.no_suite, table=args.table, smoke=args.smoke)


if __name__ == '__main__':
    sys.exit(main())
