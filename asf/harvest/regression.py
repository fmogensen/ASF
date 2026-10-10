"""asf.harvest.regression — the test that would have caught it, run on the tree that had the
defect.

The predicate: four pure functions, no git and no subprocess, no product import beyond
:mod:`asf.feeder.widen` and the ``conv`` passed in. ``gated`` decides which fix branches the
regression gate covers and says why one is skipped — a silent skip is how the gap went unnoticed.
``verdict`` is the one place the four states (``proved``, ``not-red``, ``inconclusive``,
``red-both``) are decided, so the hold's text and the tick line can never disagree about the
same branch.

The grafted run (F-0312 §2): ``asf/harvest/rebuild_check.py``'s shape, function for function — a
throwaway worktree detached at the trunk, the branch's test files checked out and committed
inside it (never pushed), the command run there and again on the real head, the two outputs
handed to :func:`verdict`. Too heavy for the tick itself, so it runs detached under the
product's one ``regression-check`` lock, its result stored by content; a first pass answers
"deferred" and a later one reads the stored verdict.

The lane's ask and the hold (§3, §4): :func:`ask` is what ``lane.branch_facts`` calls — gated
first, a ``Regression-waived:`` trailer against the decision register next, then the grafted
check — and :func:`hold_text` is the session's instruction back, never a verdict shouted with no
way to reproduce it.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from asf import env
from asf.feeder import widen

#: a result older than this is dropped (its branch moved on long ago) — `rebuild_check.KEEP_S`'s
#: own value, the same reasoning
KEEP_S = 2 * 24 * 3600
#: seconds one grafted run (the pre-fix half, or the head half) may take before its process
#: group is killed — `lane.PRE_PUSH_CHECK_TIMEOUT_S`'s own value
TIMEOUT_S = 900
STORE_DIR = 'regression-check'
LOCK = 'running.lock'

#: the `found_in` values the gate does not cover. The `flags.regression.exempt_found_in`
#: override lands with the flag's registration in `conventions.KNOWN_FLAGS` and not before: a
#: name read here that the registry lacks is one `asf doctor` calls unknown on the very product
#: that set it, which is what `tests.test_conventions_flags` refuses.
DEFAULT_EXEMPT_FOUND_IN = ('ci',)

#: a failing case's line in a test runner's log: `unittest`'s ``FAIL:``/``ERROR:``, node:test
#: ``✖``, vitest ``✗``/``×``, TAP ``not ok N -``. A separate parse from `lane._FAIL_RE` on
#: purpose (D8): that one feeds what the factory calls a red test on the trunk.
_CASE_RE = re.compile(
    r'^\s*(?:(?:FAIL|ERROR):\s*(?P<a>\S.*)'
    r'|[✖✗×]\s+(?P<b>.+)'
    r'|not ok\s+\d+\s*-?\s*(?P<c>.+))$'
)
_TRAILING_RE = re.compile(r'\s*\([^()]*\)\s*$')


def _case_name(raw):
    """``raw``, its trailing parenthesised dotted path and any duration stripped."""
    name = raw.strip()
    while True:
        stripped = _TRAILING_RE.sub('', name).strip()
        if stripped == name:
            return name
        name = stripped


def gated(conv, items, item, branch, files):
    """``(True, '')`` when this branch is one the regression gate covers, else ``(False, why)``:
    a `fix` lane branch (`conv.branch_kind(branch) == 'fix'`) whose card's `found_in` is not in
    :data:`DEFAULT_EXEMPT_FOUND_IN`, carrying both a test file and a non-test change (D1, D11)."""
    kind = conv.branch_kind(branch)
    if kind != 'fix':
        return False, f'not a fix branch: {kind or "none"}'
    card = (items or {}).get(item or '') or {}
    found_in = card.get('found_in')
    exempt = conv.flag('regression.exempt_found_in', list(DEFAULT_EXEMPT_FOUND_IN))
    if found_in in exempt:
        return False, f'found_in: {found_in}'
    tests = test_files(files)
    if not tests:
        return False, 'no test file in the diff: no change to be red against'
    if tests == list(files or []):
        return False, 'the whole diff is test files: no change to be red against'
    return True, ''


def test_files(files):
    """The branch's test files, in order — `widen.is_test_path` of each (D3, P5)."""
    return [f for f in (files or []) if widen.is_test_path(f)]


def failing_cases(log, pattern=None):
    """The test case names a run's output reports as not passing, in order, each once:
    `unittest`'s ``FAIL: <case>`` and ``ERROR: <case>``, node:test ``✖``, vitest ``✗``/``×``,
    TAP ``not ok N -`` — or `pattern`'s first group when the product names one (D8). The name is
    the case's own, trailing parenthesised dotted path and duration stripped."""
    out = []
    rx = None
    if pattern:
        rx = pattern if hasattr(pattern, 'search') else re.compile(pattern)
    for raw in (log or '').splitlines():
        if rx is not None:
            m = rx.search(raw)
            name = _case_name(m.group(1)) if m and m.group(1) else None
        else:
            m = _CASE_RE.match(raw)
            name = _case_name(next(g for g in m.group('a', 'b', 'c') if g)) if m else None
        if name and name not in out:
            out.append(name)
    return out


def verdict(before, after, pattern=None):
    """``(state, cases, line)`` — `'proved'` with the case names red before and green after
    (D5); `'not-red'` when the pre-fix run names no failing case and exited 0; `'inconclusive'`
    when it exited non-zero naming none (D4); `'red-both'` when every case red before is red
    after. One place decides, so the hold's text and the tick line cannot disagree."""
    before_rc, before_out = before
    _after_rc, after_out = after
    before_cases = failing_cases(before_out, pattern)
    if not before_cases:
        if before_rc == 0:
            return 'not-red', [], (
                "the branch's tests pass on the pre-fix tree: nothing on it would have caught "
                "this defect")
        return 'inconclusive', [], (
            'the pre-fix run exited non-zero naming no failing case: the grafted test did not '
            'run as a test on the pre-fix tree')
    after_cases = failing_cases(after_out, pattern)
    proved = [c for c in before_cases if c not in after_cases]
    if proved:
        return 'proved', proved, (
            f'{", ".join(proved)} red before the fix and absent after: proved')
    return 'red-both', before_cases, (
        f'{", ".join(before_cases)} red on both sides: not a regression test for this fix')


# ---- the grafted run, its store and its lock (F-0312 §2; D2, D3, D6, D7; P4) -------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def store(state_dir):
    return os.path.join(state_dir, STORE_DIR)


def _diff_files(repo, a, b):
    """The files ``b`` changed since ``a`` — by sha, not by branch name: :func:`key` and
    :func:`run_job` read a throwaway git fixture that carries no ``origin/*`` refs at all."""
    from asf import gitops
    r = gitops.git(['diff', '--name-only', f'{a}...{b}'], repo)
    return [l for l in (r.data or '').splitlines() if l.strip()] if r.ok else []


def key(repo, trunk_sha, branch_sha, command):
    """``<pre-fix tree>-<test tree>-<command hash>`` — the three facts that decide the answer,
    read as trees and blobs, never shas: a rebase that changes no content is a cache hit, and a
    changed test file is a miss. ``<test tree>`` is the content of exactly the files
    :func:`run_job` grafts (:func:`test_files` of the branch's own diff) — not the branch's whole
    tree, which a non-test commit would also move with no effect on what the graft checks out.
    None when git cannot read the trunk sha's tree."""
    from asf import gitops
    r = gitops.git(['rev-parse', f'{trunk_sha}^{{tree}}'], repo)
    pre_tree = (r.data or '').strip() if r.ok else ''
    if not pre_tree:
        return None
    blobs = []
    for path in test_files(_diff_files(repo, trunk_sha, branch_sha)):
        rr = gitops.git(['rev-parse', f'{branch_sha}:{path}'], repo)
        if rr.ok and (rr.data or '').strip():
            blobs.append(f'{path}:{rr.data.strip()}')
    test_tree = hashlib.sha256('\0'.join(sorted(blobs)).encode()).hexdigest()[:12]
    h = hashlib.sha256((command or '').encode()).hexdigest()[:12]
    return f'{pre_tree}-{test_tree}-{h}'


def result_path(state_dir, k):
    return os.path.join(store(state_dir), f'{k}.json')


def read_result(state_dir, k):
    try:
        with open(result_path(state_dir, k), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and 'state' in data else None


def write_result(state_dir, k, state, cases, line, trunk_sha, branch_sha):
    d = store(state_dir)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f'.{k}.{os.getpid()}.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'state': state, 'cases': cases, 'line': line, 'trunk': trunk_sha,
                  'branch': branch_sha, 'at': _now()}, f)
    os.replace(tmp, result_path(state_dir, k))


def prune(state_dir, now=None):
    now = now or time.time()
    d = store(state_dir)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        if n.endswith('.json'):
            try:
                if now - os.path.getmtime(p) > KEEP_S:
                    os.remove(p)
            except OSError:
                pass


def try_lock(state_dir):
    """The product's one regression-check lock (an open file holding ``flock``), or None when a
    check already runs."""
    d = store(state_dir)
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, LOCK), 'a+')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def running(state_dir):
    """``{key, trunk, branch, since, pid}`` of the check that holds the lock, or None when none
    runs."""
    lock = try_lock(state_dir)
    if lock is not None:
        lock.close()
        return None
    try:
        with open(os.path.join(store(state_dir), LOCK), encoding='utf-8') as f:
            data = json.loads(f.read() or '{}')
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def run_job(product, trunk_sha, branch_sha, k, out=print, state_dir=None, timeout=TIMEOUT_S):
    """What the detached job does, under the product's one lock: the graft, the two runs, the
    verdict, the result. 0 always — a check another check holds the lock for waits for the next
    pass to start it again, same as a check that found no checkout to run in."""
    from asf.harvest.lane import _run_shell
    state_dir = os.path.abspath(state_dir or env.state_dir(product))
    lock = try_lock(state_dir)
    if lock is None:
        out(f'regression check: another check of {product.name} runs — {branch_sha[:9]} waits')
        return 0
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({'key': k, 'trunk': trunk_sha, 'branch': branch_sha, 'since': _now(),
                               'pid': os.getpid()}))
        lock.flush()
        conv = product.conventions
        command = resolved_command(conv, trunk_sha)
        pattern = conv.flag('regression.fail_re') or None
        repo = product.repo_dir
        setup = getattr(conv, 'worktree_setup', None)
        state, cases, line = run_graft(repo, state_dir, trunk_sha, branch_sha, command, setup,
                                       pattern=pattern, timeout=timeout, run_shell=_run_shell)
        if state is None:  # no checkout: nothing judged — the next pass starts it again
            out(f'regression check {branch_sha[:9]}: {line}')
            return 0
        write_result(state_dir, k, state, cases, line, trunk_sha, branch_sha)
        out(f'regression check {branch_sha[:9]}: {state}')
        return 0
    finally:
        lock.close()


def run_graft(repo, state_dir, trunk_sha, branch_sha, command, setup=None, pattern=None,
              timeout=TIMEOUT_S, run_shell=None):
    """``(state, cases, line)`` over a real checkout — `asf/harvest/rebuild_check.py`'s shape:
    one throwaway worktree detached at ``trunk_sha`` under ``<state>/regression-check/``, the
    product's ``worktree_setup``, then the branch's own test files checked out onto it and
    committed inside the worktree (never pushed — D2, D3), the command run there (the pre-fix
    half), the same worktree forced to ``branch_sha`` and the command run again (the head half —
    not a second concept, the gate's own command on the rebased head), and :func:`verdict` over
    the two. ``(None, [], why)`` when no checkout could be made at all: nothing was judged, and
    the worktree — if one was made — is gone before this returns, pass or fail."""
    from asf.harvest import harvest as H
    run_shell = run_shell or _run_shell_fallback
    holder = os.path.join(state_dir, STORE_DIR)
    try:
        os.makedirs(holder, exist_ok=True)
        path = tempfile.mkdtemp(prefix='wt-', dir=holder)
        os.rmdir(path)
    except OSError as e:
        return None, [], f'no checkout to run `{command}` in: {e}'
    add = H.sh(['git', 'worktree', 'add', '-q', '--detach', path, trunk_sha],  # client-exempt: D2/D6, as lane.pre_push_check_at's own throwaway worktree
              cwd=repo)
    if add.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None, [], f'no checkout of {trunk_sha[:9]} to run `{command}` in: {H.tail(add.stderr)}'
    try:
        if setup:
            rc, out = run_shell(setup, path, timeout)
            if rc != 0:
                why = f'timed out after {timeout}s' if rc is None else f'exit {rc}'
                return None, [], f'`{setup}` (worktree_setup) failed on {trunk_sha[:9]}: {why}'
        files = test_files(_diff_files(repo, trunk_sha, branch_sha))
        if files:
            co = H.sh(['git', 'checkout', branch_sha, '--'] + files, cwd=path)  # client-exempt: D3, the graft itself
            if co.returncode != 0:
                return None, [], (f'could not graft {", ".join(files)} onto {trunk_sha[:9]}: '
                                  f'{H.tail(co.stderr)}')
            commit = H.sh(['git', '-c', 'user.name=asf', '-c', 'user.email=asf@local', 'commit',  # client-exempt: D2, a commit that exists only in this worktree, never pushed
                          '-q', '-m', "regression check: the branch's tests on the pre-fix tree"],
                         cwd=path)
            if commit.returncode != 0:
                return None, [], f'could not commit the grafted test files: {H.tail(commit.stderr)}'
        before_rc, before_out = run_shell(command, path, timeout)
        co2 = H.sh(['git', 'checkout', '-q', '--force', branch_sha], cwd=path)  # client-exempt: the head half, same throwaway worktree
        if co2.returncode != 0:
            return None, [], (f'could not check out {branch_sha[:9]} to run the head half: '
                              f'{H.tail(co2.stderr)}')
        after_rc, after_out = run_shell(command, path, timeout)
        state, cases, line = verdict((before_rc, before_out), (after_rc, after_out), pattern)
        tail = H.tail(before_out, 4)
        detail = f'{line} — `{command}`: {tail}' if tail else f'{line} — `{command}`'
        return state, cases, detail
    finally:
        H.sh(['git', 'worktree', 'remove', '--force', path], cwd=repo)  # client-exempt: as lane.pre_push_check_at's own cleanup
        shutil.rmtree(path, ignore_errors=True)


def _run_shell_fallback(command, cwd, timeout):
    """Used only when :func:`run_graft` is called with no ``run_shell`` (the standalone CLI
    path, :func:`main`) — `lane._run_shell`'s own shape, kept here so this module never imports
    `asf.harvest.lane` at the top (`lane` imports this module)."""
    from asf.harvest import harvest as H
    p = subprocess.Popen(command, shell=True, cwd=cwd, env=H.clean_env(dict(os.environ)),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ''
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except (ProcessLookupError, PermissionError):
            p.kill()
        out, _ = p.communicate()
        return None, out or ''


def resolved_command(conv, trunk_sha):
    """``flags.regression.command`` with ``{base}`` substituted, defaulting to
    ``conventions.test_command`` (D7) — the base substituted either way."""
    tpl = conv.flag('regression.command') or conv.test_command
    return (tpl or '').replace('{base}', trunk_sha)


def check(product, repo, state_dir, trunk_sha, branch_sha, command, setup=None, pattern=None,
         spawn_fn=None, dry_run=False):
    """``(state, line)``: the stored verdict when a job already judged this content, else
    ``(None, why)`` — started now in the background, or another check still running — and the
    caller goes on without waiting (D6)."""
    k = key(repo, trunk_sha, branch_sha, command)
    if k is None:
        return None, f'no tree for {trunk_sha[:9]} — the regression check waits for the next pass'
    got = read_result(state_dir, k)
    if got is not None:
        return got['state'], got.get('line') or ''
    busy = running(state_dir)
    if busy is not None:
        what = 'this content' if busy.get('key') == k else f"{str(busy.get('branch') or '?')[:9]}"
        return None, (f'the regression check runs in the background on {what} (since '
                      f'{busy.get("since") or "?"}) — the next pass reads its result')
    if dry_run:
        return None, f'DRY: would start the regression check on {branch_sha[:9]} in the background'
    prune(state_dir)
    pid = (spawn_fn or spawn)(product, trunk_sha, branch_sha, k, state_dir)
    got = read_result(state_dir, k)
    if got is not None:  # a job that finished already (a spawn that runs it in place)
        return got['state'], got.get('line') or ''
    return None, (f'the regression check started in the background on {branch_sha[:9]} (pid '
                  f'{pid}) — the next pass reads its result')


def spawn(product, trunk_sha, branch_sha, k, state_dir=None):
    """Start :func:`main` detached (never the tick's child); its output goes to
    ``logs/regression-check-<product>.log``. Returns its pid."""
    from asf import detach, hermetic
    argv = [sys.executable, '-m', 'asf.harvest.regression', '--product', product.name,
           '--trunk', trunk_sha, '--branch', branch_sha, '--key', k] + (
           ['--state-dir', state_dir] if state_dir else [])
    child_env = hermetic.build(worktree=hermetic.package_parent(),
                               identity={'ASF_HOME': env.ASF_HOME})
    log = os.path.join(env.ASF_HOME, 'logs', f'regression-check-{product.name}.log')
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, 'ab') as out, open(os.devnull, 'rb') as null:
        return detach.spawn(argv, cwd=os.path.abspath(env.state_dir(product)), env=child_env,
                            stdin=null, stdout=out, stderr=subprocess.STDOUT)


def main(argv=None):
    p = argparse.ArgumentParser(prog='asf.harvest.regression')
    p.add_argument('--product', required=True)
    p.add_argument('--trunk', required=True)
    p.add_argument('--branch', required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--state-dir')
    a = p.parse_args(argv)
    return run_job(env.load_product(a.product), a.trunk, a.branch, a.key, state_dir=a.state_dir)


if __name__ == '__main__':
    sys.exit(main())


# ---- where it is asked, and the hold (F-0312 §3, §4; D4, D5, D9, D10; P2, P12) -----------------

#: a `Regression-waived: D-nnnn` commit trailer — the same parse shape `asf.proves`' own
#: trailers use (`NOT_PROVED_RE`): a leading quote/bullet marker allowed, the id and nothing else
#: on the line.
_WAIVER_RE = re.compile(
    r'^[ \t>*-]*Regression-waived:[ \t]*(?P<id>D-\d{4,})[ \t]*$', re.M | re.I)

_WAIVER_SENTENCE = (
    "If this defect genuinely cannot be caught by a test, that is a decision, not a note: it "
    "needs a D- card in the register and a `Regression-waived: D-nnnn` trailer on a commit of "
    "this branch.")


def waiver_id(repo, trunk, branch):
    """The `Regression-waived: D-nnnn` id named on ``origin/<branch>``'s own commits above
    ``origin/<trunk>``, read with the same ``git log --format=%B`` the lane already parses a
    trailer out of — or None when the branch carries no such trailer at all (D10)."""
    from asf import gitops
    r = gitops.git(['log', '--format=%B', f'origin/{trunk}..origin/{branch}'], repo)
    m = _WAIVER_RE.search(r.data or '') if r.ok else None
    return m.group('id').upper() if m else None


def ask(lane, f):
    """``(state, line)`` for ``lane.branch_facts``, or ``None`` while deferred — printed here,
    since nothing else will (D6). `gated` first, and when it says no, the why is printed as
    `not gated — <why>` and nothing is held. When it says yes: the grafted check: a `proved`,
    `not-red`, `inconclusive` or `red-both` verdict — or, on anything but `proved`, a
    `Regression-waived:` trailer against the decision register first (`waived`, or
    `unknown-waiver` naming the id not found)."""
    conv, b = lane.conv, f['branch']
    if not conv.flag('regression.check', False):
        return 'skipped', 'not gated — regression.check is off'
    ok, why = gated(conv, lane.items, f.get('item'), b, f.get('files') or [])
    if not ok:
        lane.out(f"lane: {b}: regression: not gated — {why}")
        return 'skipped', f'not gated — {why}'
    trunk_sha, branch_sha = lane.trunk_sha, f.get('head')
    command = resolved_command(conv, trunk_sha)
    f['regression_command'] = command
    pattern = conv.flag('regression.fail_re') or None
    setup = getattr(conv, 'worktree_setup', None)
    state, line = check(lane.product, lane.repo, lane.state_dir, trunk_sha, branch_sha, command,
                        setup=setup, pattern=pattern)
    if state is None:
        lane.out(f'lane: {b}: regression: {line}')
        return None
    if state != 'proved':
        did = waiver_id(lane.repo, lane.trunk, b)
        if did is not None:
            from asf.record import decisions
            known = {d.upper() for d in decisions.register(lane.items, lane.product)}
            if did in known:
                lane.out(f'lane: {b}: regression: waived by {did}: {line}')
                return 'waived', f'regression-waived by {did}: {line}'
            return 'unknown-waiver', (f'Regression-waived: {did} — {did} is not in the decision '
                                      f'register: {line}')
    return state, line


def hold_text(f):
    """The session's instruction, never a verdict shouted with no way to reproduce it (P1): the
    test file to write, the pre-fix sha and the command the lane ran, verbatim — and the run's
    own line, which already names the cases it saw. One text per state (D9); the `unknown-waiver`
    text already names its own escape hatch and needs no waiver sentence appended twice."""
    state, line = f.get('regression') or (None, '')
    if state == 'unknown-waiver':
        return line
    item = f.get('item') or 'this card'
    trunk = f.get('trunk') or '?'
    short = trunk[:9] if trunk != '?' else trunk
    tests = test_files(f.get('files') or [])
    test_file = tests[0] if tests else 'a test file'
    command = f.get('regression_command') or '(no command configured)'
    if state == 'not-red':
        head = (f"no regression test: the branch's tests pass on origin/main at {short}, so "
               f"nothing on it would have caught {item}. Write the case that fails without your "
               f"fix and passes with it, in {test_file}, and push the same branch.")
    elif state == 'inconclusive':
        head = (f"no regression test: the grafted test did not run as a test on the pre-fix "
               f"tree at {short} — {line} Fix {test_file} so it fails cleanly on origin/main "
               f"before your fix, and push the same branch.")
    elif state == 'red-both':
        head = (f"no regression test: {line} Write a case in {test_file} that fails on "
               f"origin/main at {short} and passes on your head, and push the same branch.")
    else:
        head = f"no regression test: {line or 'the branch carries none'}"
    ran = f"The lane ran, in a checkout of {short} with {test_file} taken from your branch:\n  {command}"
    return f"{head}\n{ran}\n{_WAIVER_SENTENCE}"
