#!/usr/bin/env python3
"""harvest.py — land a finished lane branch on the trunk with no hand merge.

The entry point of the landing: :func:`run_product_harvest` for a product repo, whose lane is the
state machine in :mod:`asf.harvest.lane` (both landing modes, one gate), and :func:`run_harvest`
for the record repo's own local worker branches. This module keeps what both lean on: the git
plumbing, the rebase that resolves only machine-owned conflicts, and the product gate.

Machine-owned conflicts (README.md "The machine block"): ``index.json`` (regenerated wholesale by
``asf index``, so a conflicted copy is discarded), a ``## Children``/``## Backlinks`` section
(main's side; ``asf index`` regenerates it), and a Rule's ``source:`` line (the ``; memory …``
clauses of both sides, unioned). Anything else conflicting aborts the rebase and holds the branch
with one line naming the file — never a silent guess at someone's intent.

The gate (:func:`product_gate`) is the product's ``conventions.test_command`` — leading
``NAME=value`` tokens lifted into its environment (§12) — plus, on asf's own repo, its generic and
conventions checks, each within ``harvest.gate_timeout_s`` (B-0072). Landing on the trunk is
fast-forward only: ``git push --force*`` is never used. Python 3 stdlib only.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time


from asf import env, hermetic, refguard
from asf.conventions import Conventions
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers.pool import now_iso

#: The conventions a caller with no Product reads: trunk `main`, code branches `worker/`, no
#: test command (so the gate is `asf check` alone).
DEFAULTS = Conventions()

CONFLICT_START_RE = re.compile(r'^<{7}(?: |$)')
CONFLICT_MID_RE = re.compile(r'^={7}$')
CONFLICT_END_RE = re.compile(r'^>{7}(?: |$)')
MEMORY_CLAUSE_RE = re.compile(r'; memory [^;"]+')
MAX_REBASE_STEPS = 100

#: ``conventions.harvest.gate``: one gate over the combined head (B-0040), or one per branch.
GATE_COMBINED = 'combined'
GATE_PER_BRANCH = 'per-branch'


def cap_to_tick(items, out, conv=None):
    """``items`` trimmed to the product's ``branches_per_tick`` (B-0031: a tick runs on a clock,
    and CI races the landings it triggers — a safety valve, now that the gate runs once for all
    of them), printing the cap line via ``out`` when there were more eligible than that — the
    rest wait for the next tick."""
    cap = int((conv or DEFAULTS).branches_per_tick)
    if len(items) > cap:
        out(f'harvest: {len(items)} branches eligible — capping this tick at '
            f'{cap}, the rest wait for the next')
        return items[:cap]
    return items


GIT_HOOK_VARS = hermetic.GIT_HOOK
CALLER_IDENTITY_VARS = hermetic.CALLER_IDENTITY


def clean_env(env=None):
    """`env` (default: the caller's) minus the variables a git hook exports. Harvest runs from
    the tick, but also from a pre-commit hook or a worker session under one; a `git` child
    that inherits GIT_DIR/GIT_WORK_TREE ignores its `cwd` and writes into the hook's repo."""
    env = dict(os.environ if env is None else env)
    for var in GIT_HOOK_VARS:
        env.pop(var, None)
    return env


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=clean_env(env))


def sh_timed(cmd, cwd, env, timeout):
    """``cmd`` in a process group of its own, killed whole when ``timeout`` seconds pass
    (B-0072: a gate that never ends must not become a tick that never ends — and its
    children, a nested suite, a hook, must go with it). ``(returncode, stdout, stderr)``;
    ``returncode`` is None when it timed out."""
    p = subprocess.Popen(cmd, cwd=cwd, env=clean_env(env), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        out, err = p.communicate()
        return None, out, err
    return p.returncode, out, err


def gate_timeout(conv):
    """The seconds one gate command may take: ``conventions.harvest.gate_timeout_s``."""
    try:
        return max(1, int((conv or DEFAULTS).gate_timeout_s))
    except (TypeError, ValueError):
        return int(DEFAULTS.gate_timeout_s)


TIMED_OUT = 'gate timed out'


def timed_out_line(cmd, timeout):
    return f'gate timed out after {timeout} s: {" ".join(cmd)}'


def gate_env(worktree=None, base=None):
    """The environment the gate and index regeneration run in: :func:`asf.hermetic.build` —
    the caller's own minus the caller's identity (B-0033: the tick's ASF_PRODUCT made ASF's own
    "no product configured" tests read the live product and go red) and the git-hook variables,
    the gated ``worktree`` first on PYTHONPATH (B-0033b: a test in the worktree that runs
    ``python -m asf.cli`` must get the branch under test, not the harvester's package), the
    default branch pinned (B-0038). ASF_HOME stays: it is the operator's home, not an identity."""
    return hermetic.build(base, worktree=worktree)


def tail(text, n=1):
    lines = [l for l in (text or '').strip().splitlines() if l.strip()]
    return ' / '.join(lines[-n:]) if lines else ''


# ------------------------------------------------------------- job records --

def sessions_path(state_dir):
    return os.path.join(state_dir, 'sessions.jsonl')


def read_sessions(state_dir):
    """``{job: its latest run}`` — :func:`asf.workers.lifecycle.latest` over the registry at
    ``state_dir`` (a temp state directory in a test reads exactly as the operator's)."""
    return lifecycle.latest(sessions_path(state_dir))


def is_eligible(record, path=None):
    """What harvest may gate. The run's own verdict is *not* the test (B-0079): a branch with
    commits ahead of the trunk that no live run owns is work, whatever the session that made it
    said about itself. Sessions end `failed: empty branch` or `failed: not pushed` with their
    commits already on origin — the ledger read the tree a moment too early, or the run was
    superseded — and those branches were then invisible for ever, while `harvest: none to land`
    printed every tick.

    So: not live, not landed, not handed to the PR lane. The caller has already established that
    the branch is ahead of the trunk; the gate and the lane refusal decide the rest, exactly as
    they do for a branch whose session ended cleanly."""
    return (record is not None and not lifecycle.is_live(record)
            and not lifecycle.landed(record) and record.get('harvest') != 'pr'
            # …except a branch already sent back for a correction that no session has answered
            # and is still below the round cap: that one is owned by the round to come, not by
            # the gate (D-0048). Re-gating an untouched branch every tick bumped its round with
            # no session having tried anything, and marched items to adjudication for nothing.
            # At the cap the item belongs to the adjudicate row, and the hold keeps printing.
            # ``path`` is what tells a correction apart from an *answered* correction: without
            # it a branch held once is skipped for ever, because the correction text never goes
            # away (my own B-0079 regression, found holding three green branches).
            and not (lifecycle.pending_correction(record, path)
                     and (record.get('rounds') or 0) < lifecycle.ROUND_CAP))


def mark_harvested(state_dir, job, sha):
    """One appended registry line: this job's branch has landed. The registry is append-only —
    a later line for the same job is a field update (see asf.workers.pool)."""
    path = sessions_path(state_dir)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'job': job, 'harvested': sha}, sort_keys=True) + '\n')


def record_gate(state_dir, branches, sha, ok, seconds, line):
    """One appended ``gates.jsonl`` line: a landing gate ran, over ``branches``, at ``sha`` — the
    same append-only file and directory :func:`mark_harvested` writes to. ``at`` is the moment
    the gate **started**, ``seconds`` back from now. :func:`red_on_trunk` gates the trunk alone,
    with no branches under it (PD6) — it is not one of the two places a landing gate runs, and
    takes no line here. A write that fails is printed, never raised: a gate must not be lost
    because its ledger could not be appended to, exactly as ``write_tick_line`` already reasons."""
    path = os.path.join(state_dir, 'gates.jsonl')
    at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    entry = {'at': at.strftime('%Y-%m-%dT%H:%M:%SZ'), 'seconds': seconds,
             'branches': list(branches), 'sha': sha, 'ok': ok, 'line': line}
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, sort_keys=True) + '\n')
    except OSError as e:
        print(f'harvest: gate not recorded ({e})')


def reap_hold(record, alive=None, session_source=None):
    """Why a job's worktree may not be reaped, or None. Both must hold: the job's own record
    carries a finished line (``ended``), and the process id in it is dead — identity, not just
    the pid (B-0010, F-0076 D7/D11)."""
    if not record or not record.get('ended'):
        return 'no finished line in its record'
    if alive is None:
        alive = health_mod.alive_for(None, [record], session_source)
    pid = record.get('pid')
    if alive(pid):
        return f'pid {pid} is still alive'
    return None


def reap(repo, state_dir, job, branch, sha=None, alive=None, session_source=None):
    """Remove the job's worktree, branch and registry row — but only once :func:`reap_hold`
    clears it; otherwise print one hold line and touch nothing. True when reaped."""
    sessions = read_sessions(state_dir)
    if alive is None:
        alive = health_mod.alive_for(None, sessions.values(), session_source)
    why = reap_hold(sessions.get(job), alive=alive, session_source=session_source)
    if why:
        hold(job, f'not reaped: {why}')
        return False
    wt_path = os.path.join(state_dir, 'worktrees', job)
    if os.path.isdir(wt_path):  # out of git now, off the disk in the background
        from asf.workers import trash
        trash.discard(repo, state_dir, wt_path, check_clean=False)
    sh(['git', 'branch', '-D', branch], cwd=repo)
    mark_harvested(state_dir, job, sha)
    return True


# ------------------------------------------------------- conflict resolution --

def list_conflicted(tmp):
    r = sh(['git', 'diff', '--name-only', '--diff-filter=U'], cwd=tmp)
    return [l for l in r.stdout.splitlines() if l.strip()]


def git_dir(tmp):
    d = sh(['git', 'rev-parse', '--git-dir'], cwd=tmp).stdout.strip()
    return d if os.path.isabs(d) else os.path.join(tmp, d)


def rebase_in_progress(tmp):
    d = git_dir(tmp)
    return os.path.isdir(os.path.join(d, 'rebase-merge')) or os.path.isdir(os.path.join(d, 'rebase-apply'))


def find_conflict_hunks(text):
    lines = text.split('\n')
    hunks = []
    i = 0
    n = len(lines)
    while i < n:
        if CONFLICT_START_RE.match(lines[i]):
            start = i
            i += 1
            ours = []
            while i < n and not CONFLICT_MID_RE.match(lines[i]):
                ours.append(lines[i])
                i += 1
            i += 1  # past the '======='
            theirs = []
            while i < n and not CONFLICT_END_RE.match(lines[i]):
                theirs.append(lines[i])
                i += 1
            end = i  # index of the '>>>>>>>' line
            i += 1
            hunks.append({'start': start, 'end': end, 'ours': ours, 'theirs': theirs})
        else:
            i += 1
    return lines, hunks


def heading_before(lines, idx):
    for i in range(idx - 1, -1, -1):
        if lines[i].startswith('## '):
            return lines[i].strip()
    return None


def merge_source_hunk(ours_line, theirs_line):
    def split(line):
        q = re.search(r'"(.*)"', line)
        val = q.group(1) if q else line.split(':', 1)[1].strip()
        clauses = MEMORY_CLAUSE_RE.findall(val)
        base = MEMORY_CLAUSE_RE.sub('', val)
        return base, clauses
    base, oclauses = split(ours_line)
    _tbase, tclauses = split(theirs_line)
    merged = list(oclauses)
    for c in tclauses:
        if c not in merged:
            merged.append(c)
    return [f'source: "{base}{"".join(merged)}"']


def resolve_hunks(relpath, lines, hunks):
    """Return the fully resolved text, or None if some hunk is not machine-owned."""
    is_rule = relpath.startswith('rules' + os.sep) or relpath.startswith('rules/')
    resolved = []
    for h in hunks:
        heading = heading_before(lines, h['start'])
        if heading in ('## Children', '## Backlinks'):
            resolved.append(h['ours'])  # main's side; backlog.py index regenerates it for real
            continue
        if (is_rule and len(h['ours']) == 1 and len(h['theirs']) == 1
                and h['ours'][0].lstrip().startswith('source:')
                and h['theirs'][0].lstrip().startswith('source:')):
            resolved.append(merge_source_hunk(h['ours'][0], h['theirs'][0]))
            continue
        return None
    out = []
    prev_end = 0
    for h, r in zip(hunks, resolved):
        out.extend(lines[prev_end:h['start']])
        out.extend(r)
        prev_end = h['end'] + 1
    out.extend(lines[prev_end:])
    return '\n'.join(out)


def try_resolve_conflict(tmp, relpath):
    """Resolve one conflicted file in place and `git add` it; False if it needs a human."""
    if os.path.basename(relpath) == 'index.json':
        ours = sh(['git', 'checkout', '--ours', '--', relpath], cwd=tmp)
        if ours.returncode != 0:
            return False
        sh(['git', 'add', '--', relpath], cwd=tmp)
        return True
    full = os.path.join(tmp, relpath)
    if not os.path.isfile(full):
        return False  # add/delete conflict or similar — not a text conflict we own
    with open(full, encoding='utf-8') as f:
        text = f.read()
    lines, hunks = find_conflict_hunks(text)
    if not hunks:
        return False
    merged = resolve_hunks(relpath, lines, hunks)
    if merged is None:
        return False
    with open(full, 'w', encoding='utf-8') as f:
        f.write(merged)
    sh(['git', 'add', '--', relpath], cwd=tmp)
    return True


def rebase_and_resolve(tmp, trunk='main', onto=None, tip=None):
    """Rebase HEAD onto ``origin/<trunk>``, resolving only machine-owned conflicts.

    With ``tip`` and ``onto``: replay ``tip``'s commits above ``origin/<trunk>`` onto the commit
    ``onto`` (the combined head of B-0040) — the worktree ends detached at the new tip.

    Returns (ok, reason). On failure the rebase has already been aborted and the worktree is
    back where it was.
    """
    was = sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
    cmd = ['git', 'rebase']
    if onto:
        cmd += ['--onto', onto]
    cmd.append(f'origin/{trunk}')
    if tip:
        cmd.append(tip)
    ok, reason = _rebase_loop(tmp, cmd)
    if not ok and tip:  # an abort returns to ``tip``, not to where the worktree was
        sh(['git', 'checkout', '-q', '--detach', was], cwd=tmp)
    return ok, reason


def _rebase_loop(tmp, cmd):
    sh(cmd, cwd=tmp)
    for _ in range(MAX_REBASE_STEPS):
        conflicted = list_conflicted(tmp)
        if conflicted:
            for relpath in conflicted:
                if not try_resolve_conflict(tmp, relpath):
                    sh(['git', 'rebase', '--abort'], cwd=tmp)
                    return False, f'conflict in {relpath}'
            cont = sh(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=tmp)
            if cont.returncode != 0 and not list_conflicted(tmp) and rebase_in_progress(tmp):
                sh(['git', 'rebase', '--skip'], cwd=tmp)
            continue
        if rebase_in_progress(tmp):
            cont = sh(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=tmp)
            if cont.returncode != 0 and not list_conflicted(tmp) and rebase_in_progress(tmp):
                sh(['git', 'rebase', '--skip'], cwd=tmp)
            continue
        return True, None
    sh(['git', 'rebase', '--abort'], cwd=tmp)
    return False, 'rebase did not converge'


# --------------------------------------------------------------------- gate --

def asf_cmd(*args):
    """``asf <args>`` as this interpreter runs it — the module, not a binary on PATH, so a
    worktree is gated by the code that is harvesting it."""
    return [sys.executable, '-m', 'asf.cli', *args]


def run_gate(tmp, conv=None):
    """The product's test command, then ``asf check``. A product that configures no
    ``conventions.test_command`` is gated by ``asf check`` alone — never by a command this
    package guessed at."""
    conv = conv or DEFAULTS
    env = gate_env(tmp)
    timeout = gate_timeout(conv)
    if conv.test_command:
        cmd, extra = command_env(shlex.split(str(conv.test_command)))
        rc, out, err = sh_timed(cmd, tmp, dict(env, **extra), timeout)
        if rc is None:
            return False, timed_out_line(cmd, timeout), []
        if rc != 0:
            return (False, f'tests failed: {tail(err or out)}',
                    gate_files((out or '') + '\n' + (err or '')))
    rc, out, err = sh_timed(asf_cmd('check'), tmp, env, timeout)
    if rc is None:
        return False, timed_out_line(asf_cmd('check'), timeout), []
    if rc != 0:
        return (False, f'asf check failed: {tail(out or err)}',
                gate_files((out or '') + '\n' + (err or '')))
    return True, None, []


# --------------------------------------------------------------------- push --

def push_ff(repo, sha, trunk='main'):
    """Fast-forward-only push of `sha` to ``origin/<trunk>``. Returns (pushed, not_ff)."""
    sh(['git', 'fetch', '-q', 'origin', trunk], cwd=repo)
    origin_sha = sh(['git', 'rev-parse', f'origin/{trunk}'], cwd=repo).stdout.strip()
    anc = sh(['git', 'merge-base', '--is-ancestor', origin_sha, sha], cwd=repo)
    if anc.returncode != 0:
        return False, True
    push = sh(['git', 'push', 'origin', f'{sha}:refs/heads/{trunk}'], cwd=repo)
    return push.returncode == 0, False


def remote_head(repo, branch):
    """``origin/<branch>``'s head as origin holds it now (``''`` when the branch is not there).
    Read before any rebase or merge work, it is the lease :func:`push_branch` pushes against."""
    ls = sh(['git', 'ls-remote', '--heads', 'origin', branch], cwd=repo)
    return ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''


def push_branch(repo, sha, branch, expected, main='main', protected=None):
    """Push ``sha`` to ``origin/<branch>`` over ``expected`` — the head read before any rebase
    (:func:`remote_head`), never one a fetch just before the push supplied: a lease taken from
    that fetch matches whatever origin holds, a plain force (2026-09-25: a stale local branch
    erased a person's newer commit). The push is refused, before it is made, when ``sha`` lacks
    a commit ``expected`` holds (:func:`asf.workers.lifecycle.lost_commits`), and by git when
    origin moved since ``expected`` was read. The trunk and a protected ref are never a target
    (:mod:`asf.refguard`). ``(ok, reason)``."""
    from asf import refguard
    guard = refguard.refusal(branch, f'push branch {branch}', main, protected)
    if guard:
        return False, guard
    if expected:
        lost = lifecycle.lost_commits(repo, sha, expected, branch)
        if lost is None or lost:
            return False, lifecycle.loss_refusal(branch, lost)
    push = sh(['git', 'push', f'--force-with-lease=refs/heads/{branch}:{expected}', 'origin',
               f'{sha}:refs/heads/{branch}'], cwd=repo)
    if push.returncode != 0:
        return False, f'push branch failed: {tail(push.stderr or push.stdout)}'
    return True, None


def repo_slug(repo):
    url = sh(['git', 'remote', 'get-url', 'origin'], cwd=repo).stdout.strip()
    m = re.search(r'[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$', url)
    return m.group(1) if m else url


def pr_create_line(repo, tmp, branch, trunk='main'):
    slug = repo_slug(repo)
    subj = sh(['git', 'log', '-1', '--format=%s'], cwd=tmp).stdout.strip() or branch
    title = subj[:69] + '…' if len(subj) > 70 else subj
    title = title.replace('"', '\\"')
    body = f'Opened by the tick (harvest.py) from {branch}.'
    return (f'PR: gh pr create -R {slug} --base {trunk} --head {branch} '
            f'--title "{title}" --body "{body}"')


# ---------------------------------------------------------------- per-branch --

def docs_only_move(tmp, since, until, branch_sha, conv):
    """B-0110: True when everything ``origin/<trunk>`` gained between the sha a green gate last
    ran against (``since``) and its current tip (``until``) is documentation
    (:func:`asf.harvest.lane.landing_class`: ``specs_dir``/``plans_dir``/``reviews_dir``/
    ``doc_paths``) and none of it is a path the branch itself touches (``since...branch_sha``).
    A branch already gated green is not gated again for a trunk move that cannot have turned its
    tests red."""
    if since == until:
        return True
    moved = sh(['git', 'diff', '--name-only', since, until], cwd=tmp).stdout.splitlines()
    moved = [l for l in moved if l.strip()]
    if not moved:
        return True
    branch_files = sh(['git', 'diff', '--name-only', f'{since}...{branch_sha}'],
                      cwd=tmp).stdout.splitlines()
    if set(moved) & {l for l in branch_files if l.strip()}:
        return False
    from asf.harvest import lane as lane_mod  # local: lane imports this module (§ landing/external_ci)
    return lane_mod.landing_class(conv, moved) == lane_mod.DOCS


def harvest_branch(repo, state_dir, is_record, job, branch, dry_run, conv=None, alive=None,
                   session_source=None):
    conv = conv or DEFAULTS
    trunk = conv.main
    gated_sha = None  # the trunk sha the last green gate here ran against (is_record only)
    for _attempt in (1, 2):
        holder = tempfile.mkdtemp(prefix=f'harvest-{job}-')
        tmp = os.path.join(holder, 'wt')
        try:
            branch_sha = sh(['git', 'rev-parse', branch], cwd=repo).stdout.strip()
            expected = remote_head(repo, branch)  # before any rebase: the push's lease
            add = sh(['git', 'worktree', 'add', '--detach', tmp, branch_sha], cwd=repo)
            if add.returncode != 0:
                return hold(job, f'worktree add failed: {tail(add.stderr)}')

            sh(['git', 'fetch', '-q', 'origin', trunk], cwd=tmp)
            trunk_sha = sh(['git', 'rev-parse', f'origin/{trunk}'], cwd=tmp).stdout.strip()
            skip_gate = bool(is_record and gated_sha
                             and docs_only_move(tmp, gated_sha, trunk_sha, branch_sha, conv))
            ok, reason = rebase_and_resolve(tmp, trunk)
            if not ok:
                return hold(job, reason)

            if is_record:
                idx = sh(asf_cmd('index'), cwd=tmp, env=gate_env(tmp))
                if idx.returncode != 0:
                    return hold(job, f'asf index failed: {tail(idx.stderr or idx.stdout)}')
                if sh(['git', 'status', '--porcelain'], cwd=tmp).stdout.strip():
                    sh(['git', 'add', '-A'], cwd=tmp)
                    commit = sh(['git', '-c', 'core.editor=true', 'commit', '-qm',
                                 'harvest: regenerate index.json'], cwd=tmp)
                    if commit.returncode != 0:
                        return hold(job, f'index regen commit failed: {tail(commit.stderr or commit.stdout)}')
                if skip_gate:
                    print(f'harvest: {job} was green at its head, {trunk} moved on docs only — '
                          f'not gated again')
                else:
                    gate_ok, gate_reason, _files = run_gate(tmp, conv)
                    if not gate_ok:
                        return hold(job, gate_reason)
                    gated_sha = trunk_sha

            sha = sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()

            if dry_run:
                dest = trunk if is_record else f'origin/{branch}'
                print(f'DRY: would push {job} {sha} -> {dest}')
                return 'dry'

            if is_record:
                pushed, not_ff = push_ff(repo, sha, trunk)
                if not_ff:
                    continue  # the trunk moved under us — retry the whole cycle once
                if not pushed:
                    return hold(job, f'push to {trunk} failed (not a fast-forward)')
                sh(['git', 'fetch', '-q', 'origin', trunk], cwd=repo)
                landed = sh(['git', 'merge-base', '--is-ancestor', sha, f'origin/{trunk}'], cwd=repo)
                if landed.returncode != 0:
                    return hold(job, f'{sha} is not on origin/{trunk} after the push — nothing reaped')
            else:
                pushed, why = push_branch(repo, sha, branch, expected, trunk,
                                          refguard.listed(conv))
                if not pushed:
                    return hold(job, why)
                print(pr_create_line(repo, tmp, branch, trunk))

            reap(repo, state_dir, job, branch, sha, alive=alive, session_source=session_source)
            print(f'HARVEST OK {job} {sha}')
            return 'ok'
        finally:
            sh(['git', 'worktree', 'remove', '--force', tmp], cwd=repo)
    return hold(job, f'{trunk} moved again on retry')


def hold(job, reason):
    print(f'HARVEST HOLD {job} {reason}')
    return 'held'


# -------------------------------------------------------------------- main --

def worker_branches(repo, conv=None):
    """Every local branch under the product's code prefix (and its retired ones)."""
    conv = conv or DEFAULTS
    prefixes = (conv.prefix('code'),) + conv.legacy_prefixes()
    out = []
    for prefix in prefixes:
        r = sh(['git', 'branch', '--list', f'{prefix}*', '--format=%(refname:short)'], cwd=repo)
        out.extend(l for l in r.stdout.splitlines() if l.strip())
    return sorted(dict.fromkeys(out))


def is_record_repo(repo):
    """The record repo carries the generated ``index.json`` at its root; a product repo does
    not, and is pushed under its own branch instead of landed on the trunk."""
    return os.path.isfile(os.path.join(repo, 'index.json'))


def is_tracked(repo, path):
    """True when git tracks ``path`` in ``repo``. A record's ``index.json`` is committed; a stray,
    untracked one in a product checkout (an index run with the wrong cwd) must not turn the
    product's harvest into the record's — every lane branch then went unseen."""
    return sh(['git', 'ls-files', '--error-unmatch', path], cwd=repo).returncode == 0


def run_harvest(repo, state_dir, dry_run, conv=None, session_source=None):
    conv = conv or DEFAULTS
    repo = os.path.abspath(repo)
    state_dir = os.path.abspath(state_dir)
    is_record = is_record_repo(repo)
    sessions = read_sessions(state_dir)
    # one alive callable, one `ps` read, for every job this run judges (F-0076 D7) — not one
    # per job
    alive = health_mod.alive_for(None, sessions.values(), session_source)
    sh(['git', 'fetch', '-q', 'origin', conv.main], cwd=repo)
    eligible = []
    for branch in worker_branches(repo, conv):
        job = conv.strip_prefix(branch)
        if not is_eligible(sessions.get(job), sessions_path(state_dir)):
            continue
        ahead = sh(['git', 'rev-list', '--count', f'origin/{conv.main}..{branch}'],
                   cwd=repo).stdout.strip()
        if ahead in ('', '0'):
            continue
        eligible.append((job, branch))
    for job, branch in cap_to_tick(eligible, print, conv):
        why = reap_hold(sessions.get(job), alive=alive, session_source=session_source)
        if why:  # never land what cannot then be reaped: a live job still owns its worktree
            hold(job, f'not reaped: {why}')
            continue
        harvest_branch(repo, state_dir, is_record, job, branch, dry_run, conv, alive=alive,
                       session_source=session_source)
    return 0


# ------------------------------------------------------------ product repo --
#
# A product repo's lane branches live on its origin, pushed by the sessions. Their lifecycle —
# pushed, a PR, a review, the gate, merged or back — is :mod:`asf.harvest.lane`'s. This module
# keeps the gate the lane runs, the fast-forward push, and the ``gh`` seam.

LANDING_FF = 'fast-forward'
LANDING_PR = 'pull-request'
FAIL_LINE_RE = re.compile(r'^(FAIL|ERROR)\b|\b(failed|FAILED|error|Error|violation)\b')
#: asf's own gate scripts, run on top of the test command only when the repo harvested is this
#: package's own (under its ``tools`` directory) — no product carries them.
ASF_GATE_SCRIPTS = ('check_generic', 'check_conventions')
#: A leading ``NAME=value`` token of the test command: an environment assignment, not argv (§12).
ENV_TOKEN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def command_env(cmd):
    """``(argv, env)``: ``cmd`` with its leading ``NAME=value`` tokens lifted into ``env`` — the
    shell's ``VAR=x cmd`` form, which argv would otherwise run as a program named ``VAR=x``."""
    extra = {}
    while cmd and ENV_TOKEN_RE.match(cmd[0]):
        name, _, value = cmd[0].partition('=')
        extra[name] = value
        cmd = cmd[1:]
    return list(cmd), extra


def landing(product):
    """``conventions.landing``, else by the ``batch`` step (:func:`asf.harvest.lane.landing`)."""
    from asf.harvest import lane
    return lane.landing(product)


def external_ci(product):
    """True when the product's code PRs are gated by its external CI
    (:func:`asf.harvest.lane.external_ci`) — read off the config alone, no forge call."""
    from asf.harvest import lane
    return lane.external_ci(product)


def local_gate(product):
    """True when the product lands fast-forward with a ``test_command`` set: harvest's own gate
    (:func:`product_gate`) runs that full suite on the combined head before anything lands, and a
    worker's own run of it before pushing (B-0127: 6 sessions, 6 full suites on the host) only
    duplicates that — never true for a product :func:`external_ci` already covers."""
    if product is None or external_ci(product):
        return False
    return landing(product) == LANDING_FF and bool(product.conventions.test_command)


def sessions_by_branch(state_dir):
    """``{branch: the latest run on it}`` — :func:`asf.workers.lifecycle.by_branch`: a job
    relaunched on another branch starts a fresh run; the old branch keeps the one it had."""
    return lifecycle.by_branch(sessions_path(state_dir))


def mark_session(state_dir, job, **fields):
    path = sessions_path(state_dir)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(dict(fields, job=job), sort_keys=True) + '\n')



def is_asf_repo(repo):
    """True when ``repo`` is this package's own repo (same real path, or the same git common
    dir — a linked worktree of it counts)."""
    import asf
    own = os.path.dirname(os.path.dirname(os.path.realpath(asf.__file__)))
    if os.path.realpath(repo) == own:
        return True

    def common(path):
        d = sh(['git', 'rev-parse', '--git-common-dir'], cwd=path).stdout.strip()
        return os.path.realpath(os.path.join(path, d)) if d else None
    theirs = common(repo)
    return theirs is not None and theirs == common(own)


def first_failing_line(text):
    lines = [l.strip() for l in (text or '').splitlines() if l.strip()]
    for l in lines:
        if FAIL_LINE_RE.search(l):
            return l
    return lines[-1] if lines else 'no output'


PATH_RE = re.compile(r'(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.(?:py|md|tsx?|jsx?|sh|ya?ml|json|toml))\b')
DOTTED_RE = re.compile(r'\b(tests?(?:\.\w+)+)\b')


def gate_files(text, cap=20):
    """The repo-relative files a gate's output names, first-seen order, de-duplicated, at most
    ``cap``: every path-like token, and each dotted unittest id as its module file
    (``tests.test_feeder.X`` → ``tests/test_feeder.py``). No classification, no owner guessing."""
    found = []
    for m in re.finditer(PATH_RE.pattern + '|' + DOTTED_RE.pattern, text or ''):
        if m.group(1):
            path = m.group(1)
        else:
            parts = m.group(2).split('.')
            path = '/'.join(parts[:2]) + '.py' if len(parts) > 1 else None
        if path and path not in found:
            found.append(path)
    return found[:cap]


#: The line a test command prints naming its red modules (asf's own suite runner: ``red: a, b``).
RED_MODULES_RE = re.compile(r'^red: (.+)$', re.M)
#: The variable :func:`product_gate` names a subset of modules in for the test command to run
#: (asf's own suite runner honours it); a command that ignores it runs whole — still correct.
ONLY_VAR = 'ASF_GATE_MODULES'
#: The summary shape of asf's own suite runner: ``Ran X tests in Ys (N module(s), …)``.
MODULE_COUNT_RE = re.compile(r'^Ran \d+ tests? in [\d.]+s \((\d+) module', re.M)


def red_modules(text):
    """The modules the test command's *last* ``red:`` line names, in order; ``()`` when none."""
    found = None
    for found in RED_MODULES_RE.finditer(text or ''):
        pass
    if not found:
        return ()
    return tuple(dict.fromkeys(n for n in re.split(r'[\s,+]+', found.group(1)) if n))


#: Where a red gate's output is kept (under ``logs/``): the harvest log's one timing line names
#: the red modules, never why — a red nobody can read was bisected and re-gated blind.
GATE_RED_LOG = 'harvest-gate-red.log'
#: The lines of each red gate's output kept, and the size the file is rotated at.
GATE_RED_TAIL = 300
GATE_RED_MAX_BYTES = 4 << 20


def keep_red_output(text, verdict, only=None):
    """Append the tail of a red gate's output to ``logs/`` :data:`GATE_RED_LOG` under one header
    line; the path, or None when it could not be written (a log is never a reason to fail)."""
    try:
        path = os.path.join(env.log_dir(), GATE_RED_LOG)
        if os.path.exists(path) and os.path.getsize(path) > GATE_RED_MAX_BYTES:
            os.replace(path, path + '.1')
        lines = (text or '').rstrip().splitlines()[-GATE_RED_TAIL:]
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'===== {now_iso()} gate {verdict}'
                    + (f' (only: {" ".join(only)})' if only else '') + '\n')
            f.write('\n'.join(lines) + '\n')
        return path
    except OSError:
        return None


def product_gate(tmp, conv, asf_repo, out=None, only=None):
    """``(ok, first failing line, files, red modules)``: the product's test command, then — on
    asf's own repo — its generic and conventions checks. Each within ``harvest.gate_timeout_s``
    (B-0072). With ``only`` (module names): just those modules of the test command, named in
    :data:`ONLY_VAR`, and no checks — a bisection's targeted re-run; the full gate confirms
    whatever lands. ``out`` gets one timing line per gate: ``gate: <n> modules, <s>s, red: …``."""
    env = gate_env(tmp)
    cmds = []
    if conv.test_command:
        cmd, extra = command_env(shlex.split(str(conv.test_command)))
        cmds.append(cmd)
        env.update(extra)
    if only:
        env[ONLY_VAR] = ' '.join(only)
    elif asf_repo:
        cmds += [['bash', os.path.join('tools', name + '.sh')] for name in ASF_GATE_SCRIPTS]
    timeout = gate_timeout(conv)
    started = time.monotonic()
    count = [len(only) if only else '?']

    def timed(verdict):
        if out:
            out(f'gate: {count[0]} modules, {time.monotonic() - started:.0f}s, {verdict}')

    for i, cmd in enumerate(cmds):
        rc, stdout, err = sh_timed(cmd, tmp, env, timeout)
        whole = (stdout or '') + '\n' + (err or '')
        if i == 0 and conv.test_command:
            m = MODULE_COUNT_RE.search(whole)
            if m:
                count[0] = int(m.group(1))
        if rc is None:  # B-0072: killed with its whole process group; held, never waited for
            timed('timed out')
            return False, timed_out_line(cmd, timeout), [], ()
        if rc != 0:
            red = red_modules(whole) if i == 0 and conv.test_command else ()
            verdict = 'red: ' + (' '.join(red) if red else os.path.basename(cmd[-1]))
            keep_red_output(whole, verdict, only)
            timed(verdict)
            return False, first_failing_line(whole), gate_files(whole), red  # P8: the whole output
    timed('green')
    return True, None, [], ()



def _gh(args):
    """Run ``gh`` with ``args``; ``(rc, stdout, stderr)``. Never in the product checkout (a
    ``--delete-branch`` there would switch its branch) — every call names ``-R <slug>``."""
    p = subprocess.run(['gh', *args], capture_output=True, text=True, env=clean_env())
    return p.returncode, p.stdout, p.stderr


def gh_json(args, default):
    rc, stdout, _err = _gh(args)
    try:
        return json.loads(stdout) if rc == 0 and stdout.strip() else default
    except json.JSONDecodeError:
        return default


def merged_sha(slug, number):
    """The sha PR ``number`` merged at (the host's ``mergeCommit``), or ''."""
    rc, stdout, _err = _gh(['pr', 'view', str(number), '-R', slug, '--json', 'mergeCommit',
                            '-q', '.mergeCommit.oid'])
    return stdout.strip() if rc == 0 else ''


def sync_checkout(repo, trunk, out=print):
    """Fast-forward the checkout at ``repo`` to ``origin/<trunk>`` whenever that is ahead of it.
    The scheduler runs that checkout (an editable install: its working tree is the code), and a
    fix that reached only origin — a landing (B-0036) or any direct push (B-0042) — left the
    factory running the code from before it. Not ahead: silent. Only when the checkout is on the
    trunk with a clean tree, and only ``--ff-only`` — never a reset, never a force; anything else
    is left alone and named in one line. True when moved."""
    behind = sh(['git', 'rev-list', '--count', f'HEAD..origin/{trunk}'], cwd=repo)
    if behind.returncode != 0 or behind.stdout.strip() in ('', '0'):
        return False
    head = sh(['git', 'symbolic-ref', '-q', '--short', 'HEAD'], cwd=repo).stdout.strip()
    if head != trunk:
        out(f'harvest: {repo} not fast-forwarded — on {head or "a detached HEAD"}, not {trunk}')
        return False
    if sh(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=repo).stdout.strip():
        out(f'harvest: {repo} not fast-forwarded — working tree has local changes')
        return False
    merge = sh(['git', 'merge', '-q', '--ff-only', f'origin/{trunk}'], cwd=repo)
    if merge.returncode != 0:
        out(f'harvest: {repo} not fast-forwarded — {tail(merge.stderr or merge.stdout)}')
        return False
    sha = sh(['git', 'rev-parse', 'HEAD'], cwd=repo).stdout.strip()
    out(f'harvest: {repo} fast-forwarded to {sha}')
    return True



def run_product_harvest(product, state_dir=None, dry_run=False, bug_root=None, out=print,
                        items=None, lane_pass=True):
    """Land every lane branch of the product repo that is ready, through the lane
    (:mod:`asf.harvest.lane`). ``lane_pass``: also run the in-process pass first (a hand-run
    ``asf harvest``); the tick's detached harvest passes False — the tick ran it before the wave
    (R2) — and decides only the gate's outcomes. ``items`` is the record's index (``{id:
    card}``, removed cards included). Returns ``{branch: outcome}``."""
    from asf.harvest import lane as lane_mod
    conv = product.conventions
    repo = os.path.abspath(product.repo_dir)
    if is_record_repo(repo) and is_tracked(repo, 'index.json'):  # the record's own gate
        run_harvest(repo, state_dir or env.state_dir(product), dry_run, conv)
        return {}
    lane = lane_mod.Lane(product, state_dir, out, dry_run, items)
    sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=repo)
    if not dry_run:
        sync_checkout(repo, conv.main, out)  # a direct push to the trunk too (B-0042)
    found = None
    if lane_pass:
        _results, found = lane_mod.lane_pass(product, lane=lane)
    lane_mod.gate_pass(product, lane=lane, items=items, found=found)
    if any(r == 'landed' for r in lane.results.values()):
        sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=repo)
        sync_checkout(repo, conv.main, out)
    return lane.results


def lock_path(state_dir):
    return os.path.join(state_dir, 'harvest.lock')


def try_lock(state_dir):
    """The product's harvest lock (an open file holding ``flock``), or None when another harvest
    holds it. Every product harvest runs under it — the tick's background run and a hand-run
    ``asf harvest`` alike — so two gates never run at once. It dies with its process."""
    import fcntl
    os.makedirs(state_dir, exist_ok=True)
    f = open(lock_path(state_dir), 'a')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None



def try_lock_held(state_dir):
    """True while another process holds the product's harvest lock (a gate is running)."""
    lock = try_lock(state_dir)
    if lock is None:
        return True
    lock.close()
    return False


def record_items(root):
    """``{id: card}`` from the record at ``root`` — removed cards included, since a removed
    card's branch is exactly what :func:`superseded_by` must see (B-0065) — or None when the
    record has no index."""
    path = os.path.join(root or '', 'index.json')
    if not root or not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        index = json.load(f)
    raw = index.get('items') if isinstance(index.get('items'), dict) else index
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def build_parser():
    p = argparse.ArgumentParser(prog='harvest.py')
    p.add_argument('--repo', default=None,
                    help="a repo to harvest its local code branches in (the record repo's "
                         "path); default: the product's repo_dir, its origin's lane branches")
    env.add_product_arg(p)
    p.add_argument('--state-dir', default=None,
                    help="the product's state directory (default: ~/.ASF/state/<product>)")
    p.add_argument('--dry-run', action='store_true')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    product = env.load_product(args.product)
    state_dir = args.state_dir or env.state_dir(product)
    if args.repo:
        return run_harvest(args.repo, state_dir, args.dry_run, product.conventions)
    if not product.repo_dir:
        print(f'harvest: product {product.name} has no repo_dir — nothing to harvest')
        return 0
    lock = try_lock(os.path.abspath(state_dir))
    if lock is None:
        print(f'harvest: another harvest of {product.name} is running — skipped')
        return 0
    try:
        run_product_harvest(product, state_dir, args.dry_run)
    finally:
        lock.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
