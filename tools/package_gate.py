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
   stream not landed yet — a gap until its tests pass) or ``n/a`` (a reason, no test).

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
E2E_MODULE = 'tests.test_e2e_lane'
HOME_MODULE = 'tests.test_00_home'
R_LINES = tuple(f'R{n}' for n in range(1, 27))
STATUSES = ('test', 'pending', 'n/a')
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
            rows[rline] = {'status': status, 'tests': [dotted(t) for t in TEST_ID_RE.findall(tests)],
                           'why': why.strip()}
    return rows, problems


def table_gaps(rows):
    """What the table itself leaves open: an R-line missing, an ``n/a`` with no reason or with
    tests, a ``test``/``pending`` line naming no test."""
    gaps = []
    for rline in R_LINES:
        row = rows.get(rline)
        if row is None:
            gaps.append(f'{rline}: not in the table')
        elif row['status'] == 'n/a' and (not row['why'] or row['tests']):
            gaps.append(f'{rline}: n/a needs a reason and no test')
        elif row['status'] != 'n/a' and not row['tests']:
            gaps.append(f'{rline}: {row["status"]} names no test')
    return gaps


def line_gaps(rows, outcomes):
    """Per R-line, the mapped tests that did not pass this run. A ``pending`` line is a gap
    until every one of its tests passes; then it is done, and says so."""
    gaps, notes = [], []
    for rline in R_LINES:
        row = rows.get(rline)
        if row is None or row['status'] == 'n/a':
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


def gate(no_suite=False, table=TABLE, out=print):
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
    mapped = sorted({t for r in rows.values() if r['status'] != 'n/a' for t in r['tests']})
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
    gaps += lgaps
    out('== R-lines')
    for rline in R_LINES:
        row = rows.get(rline) or {'status': 'missing', 'tests': []}
        mark = 'n/a' if row['status'] == 'n/a' else (
            'done' if all(outcomes.get(t) == PASSED for t in row['tests']) and row['tests']
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
    p.add_argument('--child', nargs='+', metavar=('OUT', 'NAME'), help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.child:
        return child(args.child[0], args.child[1:])
    return gate(no_suite=args.no_suite, table=args.table)


if __name__ == '__main__':
    sys.exit(main())
