#!/usr/bin/env python3
"""rules.py — run the factory's rule checks.

Every `rules/R-nnnn.md` card carries either a `check:` script or an honest
`enforced: false` + `reason:`. `rules.py check` runs every check script in
parallel with a 10 s timeout each and prints one line per violation, then one
``rule check timed out: R-nnnn`` / ``rule check failed: R-nnnn`` line per check that
could say neither pass nor violation (``broken`` in ``--json``; never a violation):

    == RULES <n> checked, <v> violations, <u> unenforced
    R-0042 <what> <where> <since>

Exit 1 when there are violations or broken checks, 0 when there are none. `--verbose` adds the
unenforced list with reasons; `--json` prints the machine form T10's Bug filer
reads. python3 stdlib only, no third-party imports.

Rules are read from `index.json` (README: scripts read the index, never the md
files), so a stale index is a stale rule set — `backlog.py check` guards that.
"""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys

def _timeout():
    """Seconds one check may run: ``$ASF_RULE_CHECK_TIMEOUT``, else 10."""
    try:
        return max(1, int(os.environ.get('ASF_RULE_CHECK_TIMEOUT') or 10))
    except ValueError:
        return 10


TIMEOUT = _timeout()
MAX_WORKERS = 8


class RulesError(Exception):
    pass


def repo_root():
    """The backlog repo root. `factory-health.sh` calls this file by absolute
    path from an unrelated cwd, so the root comes from the file's own location
    unless BACKLOG_ROOT says otherwise."""
    env = os.environ.get('BACKLOG_ROOT')
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def core_rules_dir():
    """The core check scripts the ASF repo ships (`rules/`, generic — paths come from the
    environment, ids from index.json). $ASF_CORE_RULES_DIR overrides it for tests."""
    env = os.environ.get('ASF_CORE_RULES_DIR')
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'rules')


def resolve_script(root, script):
    """A card's `check:` path: the product's own script first, else the core script of the
    same file name. None when neither exists (the caller reports it as a violation)."""
    path = os.path.join(root, script)
    if os.path.isfile(path):
        return path
    core = os.path.join(core_rules_dir(), os.path.basename(script))
    return core if os.path.isfile(core) else None


# ---------------------------------------------------------------- loading --

def load_rules(root):
    """Return the live rule entries from index.json, ordered by id — a card carrying
    ``removed:`` or ``moved_to:`` is retired and skipped."""
    index_path = os.path.join(root, 'index.json')
    if not os.path.isfile(index_path):
        raise RulesError('index.json is missing (run `backlog.py index`)')
    with open(index_path, encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise RulesError(f'index.json is not valid JSON: {e}')
    items = (data or {}).get('items') or {}
    rules = []
    for iid, entry in sorted(items.items()):
        if entry.get('type') != 'rule':
            continue
        if entry.get('removed') or entry.get('moved_to'):
            continue  # a retired card: its check no longer binds this record
        rule = dict(entry)
        rule.setdefault('id', iid)
        rules.append(rule)
    return rules


def partition(rules):
    """Split rules into (enforced, unenforced, malformed)."""
    enforced, unenforced, malformed = [], [], []
    for rule in rules:
        if rule.get('enforced') is False:
            unenforced.append({
                'rule': rule['id'],
                'reason': rule.get('reason') or '(no reason given)',
            })
        elif rule.get('check'):
            enforced.append(rule)
        else:
            malformed.append(rule)
    return enforced, unenforced, malformed


# ---------------------------------------------------------------- running --

def broken(rid, kind, line):
    """One check failure: the check could not say pass or violation (``kind`` is
    ``timed out`` or ``failed``). It is the factory's problem, not the product's."""
    return {'rule': rid, 'kind': kind, 'line': line}


def run_check(root, rule):
    """Run one rule's check script. Returns ``(violations, broken)``.

    The check contract: exit 0 is a pass, exit 1 with one line per place is a violation. A
    check that times out, that cannot be run, or that exits with anything else (or exits 1
    with no line) said neither — it is a *check failure*, returned in ``broken``, never as
    a violation of the rule: a slow or crashed check is not evidence the product broke the
    rule. A card whose script is missing everywhere stays a violation (B-0021) — that is
    the record pointing at nothing, which the product's record can fix.
    """
    rid = rule['id']
    script = rule.get('check')
    path = resolve_script(root, script)
    if path is None:
        return [f"{rid} check script missing {script}"], []

    env = dict(os.environ)
    env['BACKLOG_ROOT'] = root
    try:
        proc = subprocess.run(
            ['bash', path],
            cwd=root, env=env, timeout=TIMEOUT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except subprocess.TimeoutExpired:
        return [], [broken(rid, 'timed out', f"{rid} check timed out after {TIMEOUT}s {script}")]
    except OSError as e:
        return [], [broken(rid, 'failed', f"{rid} check could not run {script}: {e}")]

    if proc.returncode == 0:
        return [], []
    if proc.returncode != 1:
        stderr_lines = [l for l in (proc.stderr or '').splitlines() if l.strip()]
        detail = stderr_lines[0].strip() if stderr_lines else ''
        line = f"{rid} check failed exit {proc.returncode} {script}"
        return [], [broken(rid, 'failed', f"{line} {detail}".rstrip())]

    lines = [l.rstrip() for l in (proc.stdout or '').splitlines() if l.strip()]
    if not lines:
        return [], [broken(rid, 'failed', f"{rid} check exited 1 with no violation line {script}")]
    return [l if l.split(' ', 1)[0] == rid else f"{rid} {l}" for l in lines], []


def run_all(root, enforced):
    """Run every enforced rule's check in parallel; returns ``(violations, broken)``."""
    if not enforced:
        return [], []
    workers = min(MAX_WORKERS, len(enforced))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda rule: run_check(root, rule), enforced))
    violations, failures = [], []
    for lines, fails in results:
        violations.extend(lines)
        failures.extend(fails)
    violations.sort()
    failures.sort(key=lambda b: b['rule'])
    return violations, failures


def failure_line(b):
    """The one line a check failure prints as: ``rule check timed out: R-0042``."""
    return f"rule check {b['kind']}: {b['rule']}"


# ------------------------------------------------------------------ check --

def rule_of(line):
    return line.split(' ', 1)[0]


def cmd_check(args, root):
    try:
        rules = load_rules(root)
    except RulesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    enforced, unenforced, malformed = partition(rules)
    violations, failures = run_all(root, enforced)
    for rule in malformed:
        violations.append(
            f"{rule['id']} rule card has neither check: nor enforced: false")
    violations.sort()
    unenforced.sort(key=lambda u: u['rule'])

    if args.json:
        payload = {
            'violations': [{'rule': rule_of(l), 'line': l} for l in violations],
            'unenforced': unenforced,
            'broken': failures,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if violations or failures else 0

    broken_part = f", {len(failures)} checks broken" if failures else ''
    print(f"== RULES {len(enforced)} checked, {len(violations)} violations, "
          f"{len(unenforced)} unenforced{broken_part}")
    for line in violations:
        print(line)
    for b in failures:
        print(f"{failure_line(b)} ({b['line']})")
    if args.verbose:
        print(f"-- unenforced ({len(unenforced)})")
        for u in unenforced:
            print(f"{u['rule']} {u['reason']}")
    return 1 if violations or failures else 0


# -------------------------------------------------------------------- cli --

def product_root(args, root):
    """`--product <p>` runs against that product's backlog_dir, else the given root."""
    if getattr(args, 'product', None):
        from asf import env
        return env.load_product(args.product).backlog_dir
    return root


def build_parser():
    p = argparse.ArgumentParser(prog='rules.py')
    sub = p.add_subparsers(dest='command', required=True)
    p_check = sub.add_parser('check', help="run every rule's check script")
    p_check.add_argument('--product', help="the product whose backlog's rules run with the core set")
    p_check.add_argument('--json', action='store_true',
                         help='machine form for the Bug filer')
    p_check.add_argument('--verbose', action='store_true',
                         help='also list the unenforced rules and their reasons')
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = repo_root()
    if args.command == 'check':
        root = product_root(args, root)
        return cmd_check(args, root)
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
