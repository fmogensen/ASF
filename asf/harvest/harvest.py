#!/usr/bin/env python3
"""harvest.py — land a finished job's green branch on the trunk with no hand merge.

Run by the tick, in step 0 after the pull. For every branch under the product's code prefix
(``conventions.branch_prefixes``, ``worker/`` by default) whose session in the registry
(``~/.ASF/state/<product>/sessions.jsonl``) is ``pushed`` — ended ``finished``, which health
writes only for a pushed branch (:mod:`asf.workers.lifecycle`) — and which carries commits not
on the trunk:
rebase it onto ``origin/<trunk>`` in a throwaway worktree (never the job's own, never the main
checkout — that branch is already checked out there), resolve only machine-owned conflicts,
gate it, then land it.

Machine-owned conflicts (README.md "The machine block"): `index.json` (regenerated wholesale
by `asf index` at the end, so a conflicted copy is just discarded — either side would be
overwritten), a `## Children`/`## Backlinks` section (same reasoning: take main's side of the
hunk, `asf index` regenerates it for real afterwards), and a Rule's `source:` line
(unioned — every `; memory …` clause either side carries, deduplicated, README.md's typed-field
grammar makes this a one-line scalar so the merge is a single-hunk text splice). Anything else
conflicting aborts the rebase and holds the branch with one line naming the file — never a
silent guess at someone's intent.

The gate is the product's ``conventions.test_command`` (nothing runs when it configures none)
followed by ``asf check``, run only when ``--repo`` is the record repo itself (an ``index.json``
at its root). A product repo has no such gate; its branch is rebased and pushed under its own
name — never merged to the trunk, the merge queue owns that (README.md "Non-goals") — and
harvest prints the `gh pr create` line for the tick to run.

Landing on the trunk is fast-forward only: `git push --force*` is never used (a rewritten trunk
is exactly the near-miss this card exists to prevent). Instead: fetch, verify `origin/<trunk>`
is an ancestor of the rebased tip, plain `git push origin <sha>:refs/heads/<trunk>` (which git
itself refuses if that ever turns out false), retried once — re-rebasing onto the new tip — if
the trunk moved in between.

Cleanup (the worktree, the branch, the registry line) only happens after a push lands; a held
branch is never touched. Python 3 stdlib only.

One gate per tick (B-0040, ``conventions.harvest.gate: combined``, the default): every eligible
branch is rebased in turn onto one throwaway worktree that starts at ``origin/<trunk>``, the gate
runs once on the combined head, and that head is pushed as the trunk's new tip — every branch in
it lands at that sha. A branch that conflicts on its rebase is held as before and never enters
the set. A red gate bisects: the set is split in half, each half re-stacked and gated, recursing
until a branch is red on its own — that one is held with its failing line, and everything else
still lands in the tick. ``harvest.gate: per-branch`` keeps one gate and one push per landing.
When the test command names its red modules (a ``red: a, b`` line, as asf's own suite runner
prints), the halves re-run only those modules (``ASF_GATE_MODULES``) and the set kept is gated
in full once more before it is pushed; when those modules are red on the trunk alone too, no
branch is blamed or held — ``harvest: red on trunk too — <modules>`` — and the next tick retries.
When every branch of a red set is green alone, they land one at a time: the first in wave order
(S1 first, then oldest) goes on alone and the rest re-gate on top of it — this tick while under
one gate timeout has passed, else the next — so a pair truly at odds goes red alone on the new
trunk and back to its session, and no set is held whole to be combined again forever.

A branch whose diff is Markdown under the document trees only (:func:`is_inert` — a spec or plan
branch) cannot turn a test red: it is gated by the checks alone, in a set of its own that lands
whatever the code branches' gate says, and a red gate never sends its session a round. A red
naming only files outside a branch's footprint — its item's ``writes:``, else its diff — is
``foreign``: no correction, re-gated next tick — unless a red test imports a file the branch
changed, or the branch is red alone on a trunk green on those modules: that red is the branch's.
When it sits in files outside the Task's ``writes:`` (a sibling suite the change turned red), the
branch is held for ``widen_footprint`` (:mod:`asf.feeder.widen`), not sent back for a round its
session could never pass inside its footprint.
"""
import argparse
import dataclasses
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from asf import approvals, env, hermetic
from asf.conventions import Conventions
from asf.feeder import footprint, widen
from asf.feeder.rows import LANDING_GATE
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
    if os.path.isdir(wt_path):
        sh(['git', 'worktree', 'remove', '--force', wt_path], cwd=repo)
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
        cmd = shlex.split(str(conv.test_command))
        rc, out, err = sh_timed(cmd, tmp, env, timeout)
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


def push_branch(repo, sha, branch):
    sh(['git', 'fetch', '-q', 'origin', branch], cwd=repo)
    push = sh(['git', 'push', '--force-with-lease', 'origin', f'{sha}:refs/heads/{branch}'], cwd=repo)
    return push.returncode == 0


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

def harvest_branch(repo, state_dir, is_record, job, branch, dry_run, conv=None, alive=None,
                   session_source=None):
    conv = conv or DEFAULTS
    trunk = conv.main
    for _attempt in (1, 2):
        holder = tempfile.mkdtemp(prefix=f'harvest-{job}-')
        tmp = os.path.join(holder, 'wt')
        try:
            branch_sha = sh(['git', 'rev-parse', branch], cwd=repo).stdout.strip()
            add = sh(['git', 'worktree', 'add', '--detach', tmp, branch_sha], cwd=repo)
            if add.returncode != 0:
                return hold(job, f'worktree add failed: {tail(add.stderr)}')

            sh(['git', 'fetch', '-q', 'origin', trunk], cwd=tmp)
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
                gate_ok, gate_reason, _files = run_gate(tmp, conv)
                if not gate_ok:
                    return hold(job, gate_reason)

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
                if not push_branch(repo, sha, branch):
                    return hold(job, 'push branch failed')
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
# A product repo's lane branches (every prefix: code, fix, spec, plan, legacy) live on its
# origin, pushed by the session; the registry keys a session by its job and records the branch
# it pushed. Harvest finds each ``origin/<prefix>*`` branch's session by that ``branch`` field,
# checks it is finished and that every commit names the branch's item, then lands it by the
# product's ``landing`` convention:
#
# * ``fast-forward`` (the default when the product's ``batch`` step is off or unset — nothing
#   else would ever land it): rebase onto ``origin/<main>`` in a throwaway worktree, gate it,
#   push ``HEAD:<main>`` fast-forward only, mark the session ``harvested``, delete the branch;
# * ``pull-request`` (the default when a ``batch`` step runs a merge queue): never touch the
#   trunk — the ``prs`` step already opened the PR — and only mark the session ``harvest: pr``.

LANDING_FF = 'fast-forward'
LANDING_PR = 'pull-request'
ITEM_ID_RE = re.compile(r'\b([A-Za-z]+-\d{4})\b')
FAIL_LINE_RE = re.compile(r'^(FAIL|ERROR)\b|\b(failed|FAILED|error|Error|violation)\b')
#: asf's own gate scripts, run on top of the test command only when the repo harvested is this
#: package's own (under its ``tools`` directory) — no product carries them.
ASF_GATE_SCRIPTS = ('check_generic', 'check_conventions')


def landing(product):
    """``conventions.landing``, else ``fast-forward`` when ``steps.batch`` is off or unset, else
    ``pull-request``."""
    value = product.conventions.get('landing')
    if value:
        return str(value).strip().lower()
    batch = (product._get('steps') or {}).get('batch')
    if batch is None or batch is False or str(batch).strip().lower() == 'off':
        return LANDING_FF
    return LANDING_PR


def sessions_by_branch(state_dir):
    """``{branch: the latest run on it}`` — :func:`asf.workers.lifecycle.by_branch`: a job
    relaunched on another branch starts a fresh run; the old branch keeps the one it had."""
    return lifecycle.by_branch(sessions_path(state_dir))


def mark_session(state_dir, job, **fields):
    path = sessions_path(state_dir)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(dict(fields, job=job), sort_keys=True) + '\n')


def remote_branches(repo, conv, known=()):
    """Every ``origin/<prefix>*`` branch, the prefix stripped of ``origin/``, for every prefix the
    product's branches can carry — plus every branch in ``known`` (the registry's, B-0067: eight
    finished coder branches sat under a prefix no convention named and were never looked at)
    that is on origin."""
    out = []
    for prefix in conv.all_prefixes():
        r = sh(['git', 'for-each-ref', '--format=%(refname:short)',
                f'refs/remotes/origin/{prefix}*'], cwd=repo)
        out.extend(l[len('origin/'):] for l in r.stdout.splitlines() if l.startswith('origin/'))
    for branch in known:
        if branch and branch != conv.main and branch not in out:
            r = sh(['git', 'for-each-ref', '--format=%(refname:short)',
                    f'refs/remotes/origin/{branch}'], cwd=repo)
            if r.stdout.strip() == f'origin/{branch}':
                out.append(branch)
    return sorted(dict.fromkeys(out))


def item_of(branch, record):
    """The branch's item id: the session's ``item``, else the id token in the branch name."""
    item = (record or {}).get('item')
    if item:
        return str(item)
    m = ITEM_ID_RE.search(branch.rsplit('/', 1)[-1]) or ITEM_ID_RE.search(branch)
    return m.group(1).upper() if m else None


def commits_name_item(repo, trunk, branch, item):
    """True when every commit subject on ``origin/<branch>`` not on ``origin/<trunk>`` names
    ``item`` as a token."""
    subjects = sh(['git', 'log', '--no-merges', '--format=%s',
                   f'origin/{trunk}..origin/{branch}'], cwd=repo).stdout.splitlines()
    token = re.compile(r'(?<![\w-])' + re.escape(item) + r'(?![\w])', re.I)
    return bool(subjects) and all(token.search(s) for s in subjects)


ADJUDICATE_SUBJECT_RE = re.compile(r'^adjudicate\(')


def has_adjudicate_commit(repo, trunk, branch):
    """True when a commit on ``origin/<branch>`` not on ``origin/<trunk>`` opens with
    ``adjudicate(`` — an invented ruling committed to the product repo instead of the record
    (B-0054): the brief says a ruling belongs in a decision or the item's ``## History``, never
    a commit, so harvest refuses to land one rather than trust it as a normal fix."""
    subjects = sh(['git', 'log', '--no-merges', '--format=%s',
                   f'origin/{trunk}..origin/{branch}'], cwd=repo).stdout.splitlines()
    return any(ADJUDICATE_SUBJECT_RE.match(s) for s in subjects)


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
        cmds.append(shlex.split(str(conv.test_command)))
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


def merge_commits(repo, trunk, branch):
    """The merge commits on ``origin/<branch>`` above the trunk, as ``<sha> <subject>``."""
    r = sh(['git', 'log', '--merges', '--format=%h %s', f'origin/{trunk}..origin/{branch}'],
           cwd=repo)
    return [l for l in r.stdout.splitlines() if l.strip()]


def lane_refusal(repo, trunk, branch, item):
    """``(kind, text)`` for a branch harvest refuses before any gate, or None. A lane branch is
    straight commits on the trunk: a merge commit on it (B-0056 — a session merging its own
    stale remote or the trunk, because no push it may make publishes a rebase) and a commit not
    naming the item are each a correction back to the session, never a silent hold. The
    factory publishes the rewritten branch (:func:`asf.workers.lifecycle.publish`)."""
    merges = merge_commits(repo, trunk, branch)
    if merges:
        return 'merge', (f'merge commit on a lane branch: {merges[0]} — a lane branch is straight '
                         f'commits on origin/{trunk}: rebase onto it, never merge origin/{branch} '
                         f'or origin/{trunk} into it; the factory publishes the rebased branch')
    if not item or not commits_name_item(repo, trunk, branch, item):
        return 'naming', (f'commits do not name {item or "an item id"}: every commit subject on '
                          f'the branch names its item — reword them; the factory publishes the '
                          f'rewritten branch')
    return None


def touched_files(repo, trunk, branch):
    """The files ``origin/<branch>`` changed since it left the trunk."""
    r = sh(['git', 'diff', '--name-only', f'origin/{trunk}...origin/{branch}'], cwd=repo)
    return [l for l in r.stdout.splitlines() if l.strip()]


def doc_roots(conv):
    """The top-level directories the product keeps its documents and record drops in — the first
    segment of every document tree the conventions name (specs, plans, reviews, briefs, intake)."""
    roots = set()
    for key in ('specs_dir', 'plans_dir', 'reviews_dir', 'briefs_dir', 'intake_dir'):
        value = conv.get(key)
        if value:
            roots.add(str(value).strip('/').split('/')[0])
    return roots


def is_inert(conv, files):
    """True when every one of ``files`` is a Markdown document at the repo's root or under one of
    :func:`doc_roots` — prose no test imports or executes, so it cannot turn a test red. A
    ``.md`` inside a package (a brief template, a generated skill) is not inert, nor is any other
    file under the document tree (an example yaml a test reads)."""
    roots = doc_roots(conv)
    return bool(files) and all(
        f.endswith('.md') and ('/' not in f or f.split('/')[0] in roots) for f in files)


def docs_only(conv):
    """``conv`` with no test command: the gate an inert branch passes is the checks alone."""
    return dataclasses.replace(conv, test_command=None)


def deliverable_of(conv, branch, item):
    """A ``spec/`` or ``plan/`` branch delivers one document (its template: "create only that
    file"); every other lane delivers what it touched."""
    kind = conv.branch_kind(branch)
    if kind in ('spec', 'plan') and item:
        return f'{conv.doc_dir(kind)}/{item.lower()}.md'
    return None


def already_on_trunk(repo, trunk, branch, conv, item):
    """``(landed, extras)`` — B-0057: a branch is landed when its changes are already on the
    trunk, whatever its commit count says. Every file it touched is identical between
    ``origin/<trunk>`` and ``origin/<branch>``; or, for a spec/plan branch, its one deliverable
    is — ``extras`` then names what else it carried, which is not the item's and goes with the
    branch. Counting commits held such a branch for ever: the rebase replayed commits adding a
    file the trunk already had."""
    files = touched_files(repo, trunk, branch)
    same = [f for f in files
            if sh(['git', 'diff', '--quiet', f'origin/{trunk}', f'origin/{branch}', '--', f],
                  cwd=repo).returncode == 0]
    deliverable = deliverable_of(conv, branch, item)
    if deliverable and deliverable in same:
        return True, [f for f in files if f not in same]
    if len(same) == len(files):
        return True, []
    return False, []


def land_already(repo, state_dir, branch, record, trunk, extras, dry_run, out):
    """Mark a branch whose changes are on the trunk landed at the trunk's tip, and remove it as
    after any landing (B-0057)."""
    sha = sh(['git', 'rev-parse', f'origin/{trunk}'], cwd=repo).stdout.strip()
    note = f'; not its deliverable, dropped with the branch: {", ".join(extras)}' if extras else ''
    if dry_run:
        out(f'DRY: would mark {branch} landed — already on {trunk} at {sha[:7]}{note}')
        return 'dry'
    mark_session(state_dir, record.get('job') or branch, harvested=sha, correction=None)
    sh(['git', 'push', '-q', 'origin', '--delete', branch], cwd=repo)
    out(f'landed {branch}: already on {trunk} at {sha[:7]}{note}')
    return 'landed'


def close_merged(repo, state_dir, branch, record, trunk, dry_run, out):
    """A finished run whose branch is 0 ahead of the trunk, or gone from origin: it reached the
    trunk some other way (a direct push). Its ledger is closed at the trunk's tip — left open,
    ``awaiting_harvest`` held its item busy and its ``writes:`` blocked every sibling for ever."""
    sha = sh(['git', 'rev-parse', '--verify', '-q', f'origin/{trunk}'], cwd=repo).stdout.strip()
    if not sha:
        return None
    if dry_run:
        out(f'DRY: would mark {branch} landed — already on {trunk} at {sha[:7]}')
        return 'dry'
    mark_harvested(state_dir, record.get('job') or branch, sha)
    out(f'landed {branch}: already on {trunk} at {sha[:7]}')
    return 'landed'


SUPERSEDED = 'superseded'


def superseded_by(items, item):
    """The state that supersedes a branch, or None: a fix branch of a Bug the record already
    holds Closed or Resolved is another fix's leftover (B-0057), and a card the groom removed
    (``removed:`` set — merged into another, superseded by a ruling) has no work to land
    whatever its type (B-0065). Never work to land."""
    card = (items or {}).get(item or '') or {}
    if card.get('removed'):
        return 'removed'
    if card.get('type') == 'bug' and card.get('state') in ('Closed', 'Resolved'):
        return card['state']
    return None


def archive_commit(repo, branch, message):
    """One empty commit on top of ``origin/<branch>`` carrying ``message`` — the tip the
    archive ref is pushed as (B-0066). A push runs the workflow file *of the pushed commit*; a
    branch cut before fix(B-0053) carries ``on: push`` for every ref, so pushing its tip under
    ``archive/`` fired a red run and a mail. The head commit's ``[skip ci]`` is what the CI
    provider reads; the branch's own history sits untouched underneath. The sha, or ''."""
    tip = sh(['git', 'rev-parse', f'origin/{branch}'], cwd=repo).stdout.strip()
    if not tip:
        return ''
    made = sh(['git', 'commit-tree', f'{tip}^{{tree}}', '-p', tip, '-m', message], cwd=repo)
    return made.stdout.strip() if made.returncode == 0 else ''


def archive_superseded(repo, state_dir, branch, record, item, state, dry_run, out):
    """Move a superseded branch to ``archive/<branch>`` on origin — its commits stay reachable,
    nothing is deleted unseen — and take it out of the lane (B-0057). The archive ref's tip is
    an empty ``[skip ci]`` commit over the branch's, so the push fires no run (B-0066)."""
    if dry_run:
        out(f'DRY: would archive {branch} — {item} is {state} in the record')
        return 'dry'
    sha = archive_commit(repo, branch, f'archive({item}): {branch} — {item} is {state} in the '
                                       f'record; superseded, kept for reference [skip ci]')
    if not sha:
        out(f'held {branch}: archive commit could not be made')
        return 'held'
    keep = sh(['git', 'push', '-q', 'origin', f'{sha}:refs/heads/archive/{branch}'], cwd=repo)
    if keep.returncode != 0:
        out(f'held {branch}: archive push refused: {tail(keep.stderr)}')
        return 'held'
    mark_session(state_dir, record.get('job') or branch, harvested=SUPERSEDED, correction=None)
    sh(['git', 'push', '-q', 'origin', '--delete', branch], cwd=repo)
    out(f'superseded {branch}: {item} is {state} in the record — archived as archive/{branch}')
    return SUPERSEDED


def widen_candidates(files, item_writes, touched=(), read=None, own=False):
    """``(needs, tests, exercised)`` — the gate's facts for ``widen_footprint``
    (:mod:`asf.feeder.widen`), read off the files a red gate named.

    ``exercised``: the failing test files that import a file the branch changed (``touched``) —
    whatever footprint they sit in, that red is the branch's own. ``tests``: the failing test
    files that are the branch's — every one when ``own`` (red alone on a trunk green on those
    modules), else the exercised ones. ``needs``: of those, the ones outside ``item_writes``,
    plus every other file the output names outside ``item_writes`` that such a test imports (a
    module the fix must reach). ``read(path)``: the file's text on the branch, or None when it
    is not there — a path the tree does not hold is never a need. No ``read``: no imports are
    known, and every named file is taken to exist."""
    named = [f for f in files or () if read is None or read(f) is not None]
    stems = {}
    for f in named:
        if widen.is_test_path(f):
            stems[f] = widen.import_stems((read(f) if read else '') or '', f)
    exercised = [t for t, s in stems.items() if touched and widen.imported(s, touched)]
    tests = list(stems) if own else exercised
    reached = [f for f in named if f not in stems
               and widen.imported([s for t in tests for s in stems[t]], [f])]
    needs = widen.outside(tests + reached, item_writes) if item_writes else []
    return needs, tests, exercised


def gate_reader(repo, branch):
    """``read(path)`` over ``origin/<branch>``'s tree in ``repo`` — the text, or None."""
    def read(path):
        r = sh(['git', 'show', f'origin/{branch}:{path}'], cwd=repo)
        return r.stdout if r.returncode == 0 else None
    return read


def hold_with_correction(state_dir, branch, record, kind, text, out, files=(), item_writes=(),
                         touched=(), conv=None, own=False, read=None):
    """Hold ``branch`` and hand it back to its session: :func:`asf.workers.lifecycle.hold` says
    what goes on the run (the failing output as ``correction``, the rounds over every session of
    the item, the cap the feeder switches an ADJUDICATE row on at) and what to print.

    A red gate is not the branch's when it names only files outside its footprint — the item's
    ``writes:``, or, with none (a spec or plan branch), the files the branch's diff actually
    ``touched`` — nor ever when that diff is :func:`is_inert` (docs no test runs): no
    correction, no round, re-gated next tick.

    ``own``: the red is this branch's whatever files it names — the branch was gated alone on a
    trunk already green on those very modules. A test file the branch never wrote is red because
    of code the branch did write; the footprint rule would call that foreign and re-gate (and
    re-bisect) it every tick, never handing it back."""
    job = record.get('job') or branch
    if kind == 'gate' and text.startswith(TIMED_OUT):  # B-0082: a clock is not a defect — no round,
        out(f'{TIMED_OUT} {branch}: {text} — retried next tick')  # no correction, eligible again
        return 'timed-out'
    if kind == 'gate' and touched and is_inert(conv or DEFAULTS, touched) \
            and not footprint.overlaps(touched, files):
        out(f'foreign {branch}: gate red, its diff is docs only — re-gated next tick')
        return 'foreign'
    item_writes = widen.norm_writes(item_writes)
    reach, what = (item_writes, 'writes') if item_writes else (touched, 'diff')
    needs, tests, exercised = (widen_candidates(files, item_writes, touched, read, own)
                               if kind == 'gate' and files else ([], [], []))
    # a red test that imports a file the branch changed is the branch's red, wherever it sits
    if kind == 'gate' and not own and not exercised and files and reach \
            and footprint.overlaps(reach, files) is None:
        out(f'foreign {branch}: gate red outside its {what}: {files[0]} — re-gated next tick')
        return 'foreign'  # D8: not this item's red — no correction, no round
    if needs:  # the branch's red, in files its Task may not write: widen_footprint's to answer
        fact = f'gate: {", ".join(tests) or files[0]} red alone, trunk green'
        fields, line = lifecycle.footprint_hold(
            dict(record, branch=branch, job=job), needs, fact,
            f'{text}\nfootprint: the red is outside writes: — needs {" ".join(needs)}',
            now_iso(), tests=tests)
        mark_session(state_dir, job, **fields)
        out(line)
        return 'held'
    fields, line = lifecycle.hold(sessions_path(state_dir), dict(record, branch=branch, job=job),
                                  kind, text, now_iso())
    mark_session(state_dir, job, **fields)
    out(line)
    return 'held'


def land_ff(repo, state_dir, branch, record, item, conv, asf_repo, bug_root, dry_run, out,
            item_writes=()):
    trunk = conv.main
    job = record.get('job') or branch
    touched = touched_files(repo, trunk, branch)
    gate_conv = docs_only(conv) if is_inert(conv, touched) else conv  # docs run no test
    for _attempt in (1, 2):
        holder = tempfile.mkdtemp(prefix='harvest-')
        tmp = os.path.join(holder, 'wt')
        try:
            add = sh(['git', 'worktree', 'add', '--detach', tmp, f'origin/{branch}'], cwd=repo)
            if add.returncode != 0:
                out(f'held {branch}: worktree add failed: {tail(add.stderr)}')
                return 'held'
            ok, reason = rebase_and_resolve(tmp, trunk)
            if not ok:
                return hold_with_correction(state_dir, branch, record, 'conflict', reason, out)
            ok, line, files, _red = product_gate(tmp, gate_conv, asf_repo, out)
            if not ok:
                return hold_with_correction(state_dir, branch, record, 'gate', line, out,
                                            files, item_writes, touched, conv,
                                            read=gate_reader(repo, branch))
            sha = sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
            if dry_run:
                out(f'DRY: would land {branch} → {sha}')
                return 'dry'
            pushed, not_ff = push_ff(repo, sha, trunk)
            if not_ff:
                continue  # the trunk moved under us — rebase again, once
            if not pushed:
                out(f'held {branch}: push to {trunk} refused')
                return 'held'
            sh(['git', 'fetch', '-q', 'origin', trunk], cwd=repo)
            if sh(['git', 'merge-base', '--is-ancestor', sha, f'origin/{trunk}'], cwd=repo).returncode != 0:
                out(f'held {branch}: {sha} is not on origin/{trunk} after the push')
                return 'held'
            mark_session(state_dir, job, harvested=sha, correction=None)
            sh(['git', 'push', '-q', 'origin', '--delete', branch], cwd=repo)
            out(f'landed {branch} → {sha}')
            return 'landed'
        finally:
            sh(['git', 'worktree', 'remove', '--force', tmp], cwd=repo)
    out(f'held {branch}: {trunk} moved again on retry')
    return 'held'


# ------------------------------------------------------- the PR lane, native --
#
# In ``pull-request`` landing the ``prs`` step opens a PR for every finished branch, and harvest
# lands every one of them itself — no product merge-queue script is needed (a ``batch`` step, if
# set, still runs; a PR it merged is simply found merged and its session closed). A PR merges
# when the approvals matrix allows it (checked before this lane), its checks are green (none at
# all counts as green), the product's own gate is green on its head rebased onto the trunk (or,
# under ``landing_checks_missing: wait``, its required checks ran and passed — see "what green
# means" below) and — for anything but a docs-only spec/plan branch, whose merge is what
# approves its document — the newest ASF review of its item on the branch reads
# ``verdict: approved``. Then ``gh pr merge --squash --delete-branch`` (the repo's next allowed
# method when squash is refused), or ``--auto`` when the trunk has a GitHub merge queue, and the
# session is marked ``harvested``. No review, or pending checks: wait for the next tick. Red
# checks: hold the branch and send it back to its session. Hotfix and S1 branches go first; at
# most ``capacity.batch.per_run`` merges a tick and ``parallel`` PRs in the merge queue at once.

#: The merge methods tried in order; a repo that refuses one is offered the next.
MERGE_METHODS = ('--squash', '--merge', '--rebase')
#: ``gh pr checks`` buckets that make a PR red.
RED_BUCKETS = ('fail', 'cancel')


def _gh(args):
    """Run ``gh`` with ``args``; ``(rc, stdout, stderr)``. Never in the product checkout (a
    ``--delete-branch`` there would switch its branch) — every call names ``-R <slug>``."""
    p = subprocess.run(['gh', *args], capture_output=True, text=True, env=clean_env())
    return p.returncode, p.stdout, p.stderr


def docs_dirs(conv):
    """The product's document directories a spec or plan branch may write: specs, plans, reviews."""
    return tuple(str(v).strip('/') for v in (conv.get(k) for k in
                                             ('specs_dir', 'plans_dir', 'reviews_dir')) if v)


def is_docs_branch(conv, branch, files):
    """True for a ``spec``/``plan`` branch whose every changed path is under :func:`docs_dirs`."""
    dirs = docs_dirs(conv)
    return (conv.branch_kind(branch) in ('spec', 'plan') and bool(files) and bool(dirs)
            and all(any(f.startswith(d + '/') for d in dirs) for f in files))


def _gh_json(args, default):
    rc, stdout, _err = _gh(args)
    try:
        return json.loads(stdout) if rc == 0 and stdout.strip() else default
    except json.JSONDecodeError:
        return default


VERDICT_LINE_RE = re.compile(r'^[\s*#>|-]*verdict\s*:\s*(.+?)\s*\|?\s*$', re.I | re.M)


def review_verdict(repo, conv, branch, item):
    """``(verdict, path)`` of the newest ASF review of ``item`` on ``origin/<branch>`` — the
    ``review_pattern`` file with the highest round, its ``verdict:`` line lower-cased — or
    ``('', None)`` when the branch carries none."""
    if not item:
        return '', None
    rx = re.compile(re.escape(str(conv.review_pattern))
                    .replace(re.escape('{reviews_dir}'), re.escape(conv.reviews_dir.strip('/')))
                    .replace(re.escape('{slug}'), re.escape(item.lower()))
                    .replace(re.escape('{n}'), r'(?P<n>\d+)'))
    names = sh(['git', 'ls-tree', '-r', '--name-only', f'origin/{branch}', '--',
                conv.reviews_dir], cwd=repo).stdout.splitlines()
    rounds = sorted((int(m.group('n')), n) for n in names for m in [rx.fullmatch(n)] if m)
    if not rounds:
        return '', None
    path = rounds[-1][1]
    text = sh(['git', 'show', f'origin/{branch}:{path}'], cwd=repo).stdout
    m = VERDICT_LINE_RE.search(text)
    return (m.group(1).strip().strip('`*').lower() if m else ''), path


class PrLane:
    """One harvest's view of the product's PR host: its open PRs (one ``gh pr list``), whether
    the trunk has a merge queue, and the tick's merge budget (``capacity.batch``)."""

    def __init__(self, product, slug, trunk):
        from asf import capacity
        self.slug, self.trunk = slug, trunk
        prs = _gh_json(['pr', 'list', '-R', slug, '--state', 'open', '--limit', '500', '--json',
                        'number,headRefName,autoMergeRequest'], [])
        self.open = {p.get('headRefName'): p for p in prs if isinstance(p, dict)}
        shape = capacity.batch_shape(product, None)
        self.per_run = shape.get('per_run')
        self.parallel = shape.get('parallel')
        self.merged = 0
        self.in_queue = sum(1 for p in self.open.values() if p.get('autoMergeRequest'))
        self._queue = None
        self._required = None

    def has_queue(self):
        """True when ``trunk`` has a GitHub merge queue (merges go in with ``--auto``)."""
        if self._queue is None:
            owner, _, name = self.slug.partition('/')
            q = ('query($o:String!,$n:String!,$b:String!){repository(owner:$o,name:$n)'
                 '{mergeQueue(branch:$b){id}}}')
            data = _gh_json(['api', 'graphql', '-f', f'query={q}', '-F', f'o={owner}',
                             '-F', f'n={name}', '-F', f'b={self.trunk}'], {})
            self._queue = bool((((data or {}).get('data') or {}).get('repository') or {})
                               .get('mergeQueue'))
        return self._queue

    def merged_pr(self, branch):
        """``(number, sha)`` of a merged PR whose head is ``branch``, or None."""
        prs = _gh_json(['pr', 'list', '-R', self.slug, '--head', branch, '--state', 'merged',
                        '--json', 'number,mergeCommit'], [])
        if not prs:
            return None
        return prs[0].get('number'), ((prs[0].get('mergeCommit') or {}).get('oid') or '')

    def required_checks(self, conv, state_dir):
        """The checks that must have run and passed on a PR: ``conventions.landing_checks``,
        else the trunk's branch protection (:func:`protected_checks`, cached)."""
        named = conv.get('landing_checks')
        if named:
            return (str(named),) if isinstance(named, str) else tuple(str(n) for n in named)
        if self._required is None:
            self._required = protected_checks(self.slug, self.trunk, state_dir)
        return self._required

    def slots(self):
        """``(n, why)``: how many more PRs may be merged (or queued) this tick, and the setting
        that caps it; ``(None, None)`` when nothing does."""
        room, why = None, None
        if self.per_run is not None:
            room, why = max(0, self.per_run - self.merged), 'capacity.batch.per_run'
        if self.parallel is not None and self.has_queue():
            left = max(0, self.parallel - self.in_queue)
            if room is None or left < room:
                room, why = left, 'capacity.batch.parallel'
        return room, why

    def budget_left(self):
        """``None`` when a merge may go in now, else why not."""
        if self.per_run is not None and self.merged >= self.per_run:
            return f'{self.merged} merged this tick (capacity.batch.per_run)'
        if self.has_queue() and self.parallel is not None and self.in_queue >= self.parallel:
            return f'{self.in_queue} in the merge queue (capacity.batch.parallel)'
        return None


def pr_order(entries, items=None):
    """``entries`` (``[(branch, record)]``) hotfix first, then S1, S2, the rest — stable."""
    def rank(entry):
        branch, record = entry
        if 'hotfix' in branch.lower():
            return 0
        card = (items or {}).get(item_of(branch, record) or '') or {}
        return {'S1': 1, 'S2': 2}.get(card.get('severity'), 3)
    return sorted(entries, key=rank)


def pr_checks(slug, number):
    """``('green'|'pending'|'red'|'unknown', detail, checks)`` for PR ``number``'s checks —
    ``checks`` the ``gh pr checks`` list (``[{name, bucket}]``). No checks at all is green; any
    failed or cancelled one is red; else any not finished is pending."""
    rc, stdout, err = _gh(['pr', 'checks', str(number), '-R', slug, '--json', 'name,bucket'])
    if 'no checks reported' in f'{stdout}\n{err}':
        return 'green', 'no checks', []
    try:
        checks = [c for c in json.loads(stdout) if isinstance(c, dict)]
    except (json.JSONDecodeError, TypeError):
        return 'unknown', tail(err) or f'gh pr checks exited {rc}', []
    red = [c.get('name') or '?' for c in checks if c.get('bucket') in RED_BUCKETS]
    if red:
        return 'red', ', '.join(red), checks
    pending = [c.get('name') or '?' for c in checks if c.get('bucket') == 'pending']
    if pending:
        return 'pending', ', '.join(pending), checks
    return 'green', f'{len(checks)} check(s)', checks


def merge_pr(slug, number):
    """``(ok, detail)``: merge PR ``number``, squash first, deleting its branch."""
    err = ''
    for method in MERGE_METHODS:
        rc, _out, err = _gh(['pr', 'merge', str(number), '-R', slug, method, '--delete-branch'])
        if rc == 0:
            return True, method
        if 'not allowed' not in (err or '').lower():
            break
    return False, tail(err) or 'gh pr merge failed'


def merged_sha(slug, number):
    rc, stdout, _err = _gh(['pr', 'view', str(number), '-R', slug, '--json', 'mergeCommit',
                            '-q', '.mergeCommit.oid'])
    return stdout.strip() if rc == 0 else ''


# -- what "green" means before a native merge ------------------------------------------------
#
# A PR's checks being green is not the trunk staying green: a product's CI is often
# path-filtered, so on a docs-only PR its gate job never runs and "green" is some trivial check
# alone. So no PR is merged on its checks alone. Every one the checks allow is gated locally —
# its head rebased onto the trunk, under the same :func:`product_gate` fast-forward landing runs
# (the product's test command, and asf's own checks on asf's repo) — every mergeable PR of the
# tick stacked into one combined head and gated once, bisecting on red, under the product's
# harvest lock (:func:`try_lock`), so one gate runs at a time. Only a green head is merged.
#
# The *required* checks — ``conventions.landing_checks: [job names]``, else the trunk's branch
# protection (``gh api``, cached for :data:`REQUIRED_TTL_S`) — must have run and passed; one
# that did not run (path-filtered: absent, or ``skipping``) is not green. What harvest does then
# is ``conventions.landing_checks_missing`` — one value, or a map per landing class
# (``{docs: local-gate, code: wait}``):
#
# * ``local-gate`` (the default): the local gate stands in for it — it runs for every PR anyway.
# * ``wait``: CI is the gate, not this machine (a product whose gate is too heavy to run here).
#   A PR whose required checks all ran and passed merges without a local gate; one missing a
#   required check waits for it — never for ever: a required check with no run after
#   ``conventions.landing_checks_wait_min`` minutes (default 30) on the same head will never
#   come (path-filtered), and the PR is gated locally instead. With no required check declared
#   there is nothing to wait for: the local gate.

#: ``conventions.landing_checks_missing`` values.
MISSING_LOCAL_GATE = 'local-gate'
MISSING_WAIT = 'wait'
#: Minutes a ``wait`` waits for a required check that never got a run, before gating locally.
DEFAULT_LANDING_WAIT_MIN = 30
#: Seconds the trunk's branch-protection required checks are cached for.
REQUIRED_TTL_S = 3600
#: State files (in the product's state dir): the protection cache, and when each PR head was
#: first seen missing a required check.
REQUIRED_CACHE = 'landing-required-checks.json'
MISSING_SINCE = 'landing-missing-since.json'
#: ``gh pr checks`` buckets that count as a check having run and passed.
PASS_BUCKETS = ('pass',)


def landing_class(docs):
    """A PR's landing class for per-class settings: ``docs`` or ``code``."""
    return 'docs' if docs else 'code'


def missing_policy(conv, cls):
    """``conventions.landing_checks_missing`` for landing class ``cls``: a single value, or a map
    per class (``{docs: …, code: …}``, ``default:`` for the rest); anything else is local-gate."""
    value = conv.get('landing_checks_missing')
    if isinstance(value, dict):
        value = value.get(cls, value.get('default'))
    return MISSING_WAIT if str(value or '').strip().lower() == MISSING_WAIT else MISSING_LOCAL_GATE


def landing_wait_s(conv):
    try:
        return max(0.0, float(conv.get('landing_checks_wait_min', DEFAULT_LANDING_WAIT_MIN))) * 60
    except (TypeError, ValueError):
        return DEFAULT_LANDING_WAIT_MIN * 60.0


def _read_state(state_dir, name):
    try:
        with open(os.path.join(state_dir, name), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state_dir, name, data):
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, name)
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(data, f, sort_keys=True)
        os.replace(path + '.tmp', path)
    except OSError:
        pass  # a cache is never a reason to fail


def protected_checks(slug, trunk, state_dir, now=None):
    """The status checks the trunk's branch protection requires, cached in ``state_dir`` for
    :data:`REQUIRED_TTL_S`; ``()`` when it requires none or cannot be read."""
    now = time.time() if now is None else now
    key = f'{slug}@{trunk}'
    cache = _read_state(state_dir, REQUIRED_CACHE)
    hit = cache.get(key)
    if isinstance(hit, dict) and now - float(hit.get('at') or 0) < REQUIRED_TTL_S:
        return tuple(hit.get('checks') or ())
    data = _gh_json(['api', f'repos/{slug}/branches/{trunk}/protection/required_status_checks'],
                    {})
    names = []
    if isinstance(data, dict):
        names = [str(c) for c in data.get('contexts') or []]
        names += [str(c.get('context')) for c in data.get('checks') or []
                  if isinstance(c, dict) and c.get('context')]
    names = list(dict.fromkeys(names))
    cache[key] = {'at': now, 'checks': names}
    _write_state(state_dir, REQUIRED_CACHE, cache)
    return tuple(names)


def missing_since(state_dir, branch, head, now, write=True):
    """When ``branch`` at ``head`` was first seen missing a required check (epoch seconds) — now,
    the first time, and again whenever its head moves (a new push gets a fresh wait)."""
    data = _read_state(state_dir, MISSING_SINCE)
    seen = data.get(branch)
    if isinstance(seen, dict) and seen.get('head') == head:
        return float(seen.get('since') or now)
    if write:
        data[branch] = {'head': head, 'since': now}
        _write_state(state_dir, MISSING_SINCE, data)
    return now


def forget_missing(state_dir, branch):
    data = _read_state(state_dir, MISSING_SINCE)
    if data.pop(branch, None) is not None:
        _write_state(state_dir, MISSING_SINCE, data)


#: What :func:`land_pr` hands on for a PR its checks allow: gate it locally, or merge it on CI.
READY_GATE = 'gate'
READY_CI = 'ci'


def land_pr(repo, state_dir, branch, record, lane, conv, docs, dry_run, out, now=None):
    """The first half of landing ``branch``'s PR through ``lane`` (a :class:`PrLane`); see the
    section notes above. ``docs``: a docs-only spec/plan branch, which needs no review. Returns
    an outcome (``landed``, ``waiting``, ``held``, …), or ``(READY_GATE|READY_CI, number)`` for
    a PR :func:`land_ready` merges — after a local gate, or on its required checks alone."""
    job = record.get('job') or branch
    slug = lane.slug
    pr = lane.open.get(branch)
    if pr is None:
        merged = lane.merged_pr(branch)
        if merged:  # merged by the queue, the product's batch step or a person
            number, sha = merged
            sha = sha or f'PR #{number}'
            if dry_run:
                out(f'DRY: would mark {branch} landed — PR #{number} merged')
                return 'dry'
            mark_session(state_dir, job, harvested=sha, correction=None)
            out(f'landed {branch} → PR #{number} {sha} (merged)')
            return 'landed'
        out(f'waiting {branch}: no open PR yet')
        return 'waiting'
    number = pr.get('number')
    if pr.get('autoMergeRequest'):
        out(f'queued {branch}: PR #{number} in the merge queue')
        return 'queued'
    if not docs:
        verdict, path = review_verdict(repo, conv, branch, item_of(branch, record))
        if verdict != 'approved':
            why = f'{path} reads {verdict or "no verdict"}' if path else 'no ASF review yet'
            out(f'waiting {branch}: PR #{number} not approved — {why}')
            return 'waiting'
    state, detail, checks = pr_checks(slug, number)
    if state == 'pending':
        out(f'waiting {branch}: PR #{number} checks pending — {detail}')
        return 'waiting'
    if state == 'unknown':
        out(f'waiting {branch}: PR #{number} checks unreadable — {detail}')
        return 'waiting'
    if state == 'red':
        if dry_run:
            out(f'DRY: would hold {branch}: PR #{number} checks red: {detail}')
            return 'dry'
        return hold_with_correction(state_dir, branch, record, 'gate',
                                    f'PR #{number} checks red: {detail}', out)
    if missing_policy(conv, landing_class(docs)) == MISSING_LOCAL_GATE:
        return READY_GATE, number
    required = lane.required_checks(conv, state_dir)
    if not required:  # nothing CI is trusted with: the local gate
        return READY_GATE, number
    passed = {c.get('name') for c in checks if c.get('bucket') in PASS_BUCKETS}
    missing = [name for name in required if name not in passed]
    if not missing:
        return READY_CI, number
    now = time.time() if now is None else now
    head = sh(['git', 'rev-parse', f'origin/{branch}'], cwd=repo).stdout.strip()
    waited = now - missing_since(state_dir, branch, head, now, write=not dry_run)
    limit = landing_wait_s(conv)
    if waited < limit:
        out(f'waiting {branch}: PR #{number} required check(s) not run — '
            f'{", ".join(missing)} (landing_checks_missing: wait, '
            f'{int(waited // 60)}/{int(limit // 60)} min)')
        return 'waiting'
    out(f'harvest: {branch}: PR #{number} required check(s) never ran in {int(limit // 60)} min '
        f'— {", ".join(missing)}: gating locally')
    return READY_GATE, number


def land_ready(repo, state_dir, ready, lane, conv, asf_repo, dry_run, out, items=None):
    """The second half: merge every PR of ``ready`` (``[(branch, record, number, how, docs)]``,
    in wave order) the tick's merge budget has room for. Those ``how == READY_GATE`` are gated
    first, all together on one combined head (:func:`gate_prs`); only the green are merged.
    ``{branch: outcome}``."""
    results = {}
    room, why = lane.slots()
    if room is not None and len(ready) > room:
        for branch, _record, number, _how, _docs in ready[room:]:
            out(f'waiting {branch}: PR #{number} green — no merge room this tick ({why})')
            results[branch] = 'waiting'
        ready = ready[:room]
    if not ready:
        return results
    if dry_run:
        for branch, _record, number, how, _docs in ready:
            gate = 'gate locally and ' if how == READY_GATE else ''
            out(f'DRY: would {gate}merge {branch} (PR #{number})')
            results[branch] = 'dry'
        return results
    to_gate = [(b, r) for b, r, _n, how, _d in ready if how == READY_GATE]
    docs = {b for b, _r, _n, _h, d in ready if d}
    green = gate_prs(repo, state_dir, to_gate, conv, asf_repo, out, items, results, docs) \
        if to_gate else set()
    for branch, record, number, how, _docs in ready:
        if how == READY_GATE and branch not in green:
            continue
        results[branch] = merge_ready(state_dir, branch, record, lane, number, out)
    return results


def gate_prs(repo, state_dir, entries, conv, asf_repo, out, items, results, docs=()):
    """Gate ``entries`` (``[(branch, record)]``) as one combined head on the trunk, bisecting on
    red (:func:`confirmed_group`); returns the branches confirmed green. A branch red on its own
    is sent back (:func:`send_back`) — once the trunk alone is seen green, so no PR pays for a
    red trunk. Every other outcome goes into ``results``."""
    trunk = conv.main
    held = []

    def hold(entry, kind, text, files=(), own=False):
        held.append((entry, kind, text, files))

    holder = tempfile.mkdtemp(prefix='harvest-')
    tmp = os.path.join(holder, 'wt')
    try:
        add = sh(['git', 'worktree', 'add', '--detach', tmp, f'origin/{trunk}'], cwd=repo)
        if add.returncode != 0:
            for branch, _record in entries:
                out(f'held {branch}: worktree add failed: {tail(add.stderr)}')
                results[branch] = 'held'
            return set()
        try:
            landing_set, _sha, deferred = confirmed_group(tmp, trunk, entries, conv, asf_repo,
                                                          hold, out, results)
        except TrunkRed as red:
            for branch, _record in entries:
                out(f'waiting {branch}: {trunk} is red alone — {" ".join(red.modules)}')
                results[branch] = 'waiting'
            return set()
        for branch, _record in deferred:
            out(f'waiting {branch}: green alone, one merges ahead of it — gated on the new '
                f'{trunk} next tick')
            results[branch] = 'waiting'
        trunk_red = None
        for entry, kind, text, files in held:
            if kind == 'gate' and not text.startswith(TIMED_OUT):
                if trunk_red is None:  # once a tick, and only when something is red
                    sh(['git', 'checkout', '-q', '--detach', f'origin/{trunk}'], cwd=tmp)
                    trunk_red = not product_gate(tmp, conv, asf_repo, out)[0]
                if trunk_red:
                    out(f'waiting {entry[0]}: gate red, and {trunk} is red alone too — '
                        f'gated again next tick')
                    results[entry[0]] = 'waiting'
                    continue
            results[entry[0]] = send_back(repo, state_dir, entry, kind, text, files, conv, out,
                                          items, entry[0] in docs)
        return {branch for branch, _record in (landing_set or [])}
    finally:
        sh(['git', 'worktree', 'remove', '--force', tmp], cwd=repo)


def send_back(repo, state_dir, entry, kind, text, files, conv, out, items, docs):
    """Hand a PR the local gate refused back to a session. A code PR: its session, like a red
    fast-forward gate (the red is its own — the trunk alone was green). A docs-only spec/plan
    PR: a :data:`LANDING_GATE` correction, which the feeder turns into a STARVED → SPEC/PLAN
    session on that branch, naming the failing gate line."""
    branch, record = entry
    if not docs or (kind == 'gate' and text.startswith(TIMED_OUT)):
        card = (items or {}).get(item_of(branch, record)) or {}
        return hold_with_correction(state_dir, branch, record, kind, text, out, files,
                                    card.get('writes') or (),
                                    touched_files(repo, conv.main, branch), conv, own=True,
                                    read=gate_reader(repo, branch))
    job = record.get('job') or branch
    doc = conv.branch_kind(branch) or 'document'
    what = 'does not rebase cleanly onto' if kind == 'conflict' else 'turns the gate red on'
    note = (f'the {doc} {what} {conv.main} — {text}. Change the {doc} so the product gate '
            f'passes on {conv.main}; nothing is merged until it does')
    fields, line = lifecycle.hold(sessions_path(state_dir), dict(record, branch=branch, job=job),
                                  LANDING_GATE, note, now_iso())
    mark_session(state_dir, job, **fields)
    out(line)
    return 'held'


def merge_ready(state_dir, branch, record, lane, number, out):
    """Merge PR ``number`` (``--auto`` into a merge queue), and mark the session harvested."""
    job = record.get('job') or branch
    slug = lane.slug
    spent = lane.budget_left()
    if spent:
        out(f'waiting {branch}: PR #{number} green — {spent}')
        return 'waiting'
    if lane.has_queue():
        rc, _o, err = _gh(['pr', 'merge', str(number), '-R', slug, '--auto'])
        if rc != 0:
            out(f'held {branch}: PR #{number} merge queue refused — {tail(err) or rc}')
            return 'held'
        lane.merged += 1
        lane.in_queue += 1
        out(f'queued {branch}: PR #{number} added to the merge queue')
        return 'queued'
    ok, how = merge_pr(slug, number)
    if not ok:
        out(f'held {branch}: PR #{number} merge refused — {how}')
        return 'held'
    lane.merged += 1
    forget_missing(state_dir, branch)
    sha = merged_sha(slug, number) or f'PR #{number}'
    mark_session(state_dir, job, harvested=sha, correction=None)
    out(f'landed {branch} → PR #{number} {sha}')
    return 'landed'


# ------------------------------------------------------------- one gate --

def combined_head(tmp, trunk, entries, hold):
    """Reset the throwaway worktree to ``origin/<trunk>`` and replay each entry's branch on top,
    in order. Returns the entries that applied; one that conflicts is handed to ``hold`` with
    the reason and never enters the set. ``entries``: ``[(branch, record)]``."""
    sh(['git', 'checkout', '-q', '--detach', f'origin/{trunk}'], cwd=tmp)
    stacked = []
    for entry in entries:
        head = sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
        ok, reason = rebase_and_resolve(tmp, trunk, onto=head, tip=f'origin/{entry[0]}')
        if ok:
            stacked.append(entry)
        else:
            hold(entry, 'conflict', reason)
    return stacked


class TrunkRed(Exception):
    """The modules a combined head is red on are red on the trunk alone too: no branch is to
    blame, none is held, the set is gated again next tick."""

    def __init__(self, modules):
        super().__init__(' '.join(modules))
        self.modules = tuple(modules)


def red_on_trunk(tmp, trunk, conv, asf_repo, out, modules):
    """True when any of ``modules`` is red on ``origin/<trunk>`` alone (a targeted run)."""
    sh(['git', 'checkout', '-q', '--detach', f'origin/{trunk}'], cwd=tmp)
    return not product_gate(tmp, conv, asf_repo, out, only=modules)[0]


def gate_groups(tmp, trunk, entries, conv, asf_repo, hold, out, announce=False, only=None,
                trunk_green=False):
    """Gate ``entries`` as one combined head; on red, bisect (B-0040). Returns the green groups
    as ``[(entries, sha, full)]`` — each a set that was gated together, the head it was gated
    at, and whether that gate was the full one (False: only ``only``, the modules the full gate
    found red, were re-run — a candidate the caller confirms in full before it lands). A branch
    red on its own is handed to ``hold`` with the gate's first failing line. When the full gate
    names its red modules and they are red on the trunk alone too, :class:`TrunkRed` is raised
    and nothing is bisected; when they are green there (``trunk_green``, carried down the
    bisection), a branch red alone on them is held as its own red, never ``foreign``."""
    stacked = combined_head(tmp, trunk, entries, hold)
    if not stacked:
        return []
    if announce:
        out(f'harvest: {len(stacked)} branch(es), one gate')
    ok, line, files, red = product_gate(tmp, conv, asf_repo, out, only)
    if ok:
        return [(stacked, sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip(), not only)]
    if not only and red:
        if red_on_trunk(tmp, trunk, conv, asf_repo, out, red):
            raise TrunkRed(red)
        trunk_green = True
    if len(stacked) == 1:
        hold(stacked[0], 'gate', line, files, own=trunk_green)
        return []
    out(f'harvest: bisecting {len(stacked)} branches')
    mid = len(stacked) // 2
    narrowed = red or only
    return (gate_groups(tmp, trunk, stacked[:mid], conv, asf_repo, hold, out, only=narrowed,
                        trunk_green=trunk_green)
            + gate_groups(tmp, trunk, stacked[mid:], conv, asf_repo, hold, out, only=narrowed,
                          trunk_green=trunk_green))


#: Full gates one landing may spend: the first, and the confirmations after each bisection.
CONFIRM_ROUNDS = 3


def confirmed_group(tmp, trunk, entries, conv, asf_repo, hold, out, results):
    """The one set of ``entries`` green under the *full* gate, as ``(entries, sha, deferred)``;
    ``entries`` and ``sha`` are None when no set was confirmed. A red gate bisects (targeted);
    the green candidates are stacked again and gated in full, so what lands has always passed
    the whole gate. Green apart but red together: holding them all only combined the same set
    again next tick, forever (a load-flaky module red in the combined gate alone) — so they
    land one at a time: the first in wave order goes on alone and the rest are ``deferred``,
    for the caller to re-gate on top of it. :class:`TrunkRed` propagates."""
    candidates = list(entries)
    deferred = []
    for _round in range(CONFIRM_ROUNDS):
        groups = gate_groups(tmp, trunk, candidates, conv, asf_repo, hold, out, announce=True)
        if not groups:
            return None, None, deferred
        if len(groups) == 1 and groups[0][2]:
            return groups[0][0], groups[0][1], deferred
        union = [e for group, _sha, _full in groups for e in group]
        if len(union) == len(candidates) and len(union) > 1:  # nothing held: green apart
            out(f'harvest: {len(union)} branches green alone, red together — '
                f'landing {union[0][0]} first, the rest re-gate on top of it')
            deferred = union[1:] + deferred
            union = union[:1]
        candidates = union
    for branch, _record in candidates:
        out(f'held {branch}: green on the red modules, not confirmed in full — next tick')
        results[branch] = 'held'
    return None, None, deferred


def regate_now(started, conv):
    """True when the branches deferred behind a landing still have time to be re-gated this
    tick: less than one gate timeout has gone by since the set's first gate began."""
    return time.monotonic() - started < gate_timeout(conv)


def land_combined(repo, state_dir, entries, conv, asf_repo, dry_run, out, items=None):
    """Land every entry of ``entries`` (``[(branch, record)]``) behind one gate on the combined
    head, bisecting on red; ``{branch: outcome}``. The head is pushed fast-forward as the
    trunk's new tip and every branch in it is marked harvested at that sha, then deleted. The
    set is taken in wave order (:func:`pr_order`: S1 first, then oldest)."""
    trunk = conv.main
    entries = pr_order(entries, items)
    touched = {branch: touched_files(repo, trunk, branch) for branch, _record in entries}
    docs = [e for e in entries if is_inert(conv, touched[e[0]])]
    code = [e for e in entries if e not in docs]
    results = {}
    if docs:  # docs cannot turn a test red: they land on their own, behind the checks alone
        results.update(land_set(repo, state_dir, docs, conv, docs_only(conv), asf_repo, dry_run,
                                out, items, touched))
    if code:
        results.update(land_set(repo, state_dir, code, conv, conv, asf_repo, dry_run, out, items,
                                touched))
    return results


def land_set(repo, state_dir, entries, conv, gate_conv, asf_repo, dry_run, out, items, touched):
    """:func:`land_combined` for one set of ``entries``, gated under ``gate_conv``. Branches
    deferred behind a green-alone landing are gated again on the new trunk while
    :func:`regate_now`, else held for the next tick — each round lands or holds one branch at
    least, so the set shrinks and none waits twice on the same combination."""
    trunk = conv.main
    results = {}

    def hold(entry, kind, text, files=(), own=False):
        card = (items or {}).get(item_of(entry[0], entry[1])) or {}  # P7: no index, no footprint
        results[entry[0]] = hold_with_correction(state_dir, entry[0], entry[1], kind, text, out,
                                                 files, card.get('writes') or (),
                                                 touched.get(entry[0]) or (), conv, own=own,
                                                 read=gate_reader(repo, entry[0]))

    started = time.monotonic()
    pending = list(entries)
    while pending:
        deferred, go_on = land_group(repo, state_dir, pending, trunk, gate_conv, asf_repo,
                                     dry_run, out, hold, results)
        if not deferred:
            break
        if go_on and not dry_run and regate_now(started, gate_conv):
            out(f'harvest: re-gating {len(deferred)} branch(es) on the new {trunk}')
            pending = deferred
            continue
        for branch, _record in deferred:
            out(f'held {branch}: green alone, one landed ahead of it — re-gated on the new '
                f'{trunk} next tick')
            results[branch] = 'held'
        break
    return results


def land_group(repo, state_dir, pending, trunk, gate_conv, asf_repo, dry_run, out, hold, results):
    """Gate ``pending`` in one throwaway worktree and push the confirmed set; ``(deferred,
    go_on)`` — the entries deferred behind it (:func:`confirmed_group`), and whether they may be
    re-gated this tick (False when the trunk is red or a push failed)."""
    deferred = []
    for _attempt in (1, 2):
        holder = tempfile.mkdtemp(prefix='harvest-')
        tmp = os.path.join(holder, 'wt')
        try:
            add = sh(['git', 'worktree', 'add', '--detach', tmp, f'origin/{trunk}'], cwd=repo)
            if add.returncode != 0:
                for branch, _record in pending + deferred:
                    out(f'held {branch}: worktree add failed: {tail(add.stderr)}')
                    results[branch] = 'held'
                return [], False
            try:
                landing, sha, more = confirmed_group(tmp, trunk, pending, gate_conv, asf_repo,
                                                     hold, out, results)
            except TrunkRed as red:  # no branch's doing: hold nothing, gate again next tick
                out(f'harvest: red on trunk too — {" ".join(red.modules)}')
                return [], False
            deferred = more + deferred
            if landing is None:
                return deferred, True
            if dry_run:
                for branch, _record in landing:
                    out(f'DRY: would land {branch} → {sha}')
                    results[branch] = 'dry'
                return deferred, False
            pushed, not_ff = push_ff(repo, sha, trunk)
            if not_ff:
                pending = landing  # the trunk moved under us — stack and gate again, once
                continue
            if not pushed:
                for branch, _record in landing:
                    out(f'held {branch}: push to {trunk} refused')
                    results[branch] = 'held'
                return deferred, False
            sh(['git', 'fetch', '-q', 'origin', trunk], cwd=repo)
            if sh(['git', 'merge-base', '--is-ancestor', sha, f'origin/{trunk}'], cwd=repo).returncode != 0:
                for branch, _record in landing:
                    out(f'held {branch}: {sha} is not on origin/{trunk} after the push')
                    results[branch] = 'held'
                return deferred, False
            for branch, record in landing:
                mark_session(state_dir, record.get('job') or branch, harvested=sha, correction=None)
                sh(['git', 'push', '-q', 'origin', '--delete', branch], cwd=repo)
                out(f'landed {branch} → {sha}')
                results[branch] = 'landed'
            return deferred, True
        finally:
            sh(['git', 'worktree', 'remove', '--force', tmp], cwd=repo)
    for branch, _record in pending:
        out(f'held {branch}: {trunk} moved again on retry')
        results[branch] = 'held'
    return deferred, False


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
                        items=None):
    """Land (or hand to the PR lane) every finished lane branch on the product repo's origin.
    ``bug_root`` is the record a red gate files its Bug in — a path, or a callable returning
    one (the tick's record clone, made only when needed); None prints ``BUG:`` instead.
    ``items`` is the record's index (``{id: card}``) when the caller has one: a Bug's branch is
    superseded by its card being Closed or Resolved (B-0057). Returns ``{branch: outcome}``."""
    conv = product.conventions
    repo = os.path.abspath(product.repo_dir)
    if is_record_repo(repo) and is_tracked(repo, 'index.json'):  # the record's own gate
        run_harvest(repo, state_dir or env.state_dir(product), dry_run, conv)
        return {}
    state_dir = os.path.abspath(state_dir or env.state_dir(product))
    trunk = conv.main
    mode = landing(product)
    sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=repo)
    if not dry_run:
        sync_checkout(repo, trunk, out)  # a direct push to the trunk too (B-0042)
    sessions = sessions_by_branch(state_dir)
    asf_repo = None
    results = {}
    eligible = []
    on_origin = remote_branches(repo, conv, known=sessions)
    slug = None
    if mode == LANDING_PR:  # on a PR host, harvest lands the PRs itself (the native PR lane)
        from asf.tick.step_prs import repo_slug
        slug = repo_slug(product)
    for branch in on_origin:
        record = sessions.get(branch)
        if record is None or lifecycle.is_live(record):
            continue
        if record.get('harvest') == 'pr':  # handed to the PR lane — which is harvest's own now
            if not slug:
                continue
            record = dict(record, harvest=None)
        ahead = sh(['git', 'rev-list', '--count', f'origin/{trunk}..origin/{branch}'],
                   cwd=repo).stdout.strip()
        if ahead in ('', '0'):
            if ahead == '0' and lifecycle.eligible(record):  # reached the trunk by a direct push
                closed = close_merged(repo, state_dir, branch, record, trunk, dry_run, out)
                if closed:
                    results[branch] = closed
            continue
        # B-0061: content decides before the run's verdict does — a branch whose changes are on
        # the trunk is landed, a Closed Bug's branch is archived, whatever the run ended as (a
        # hand-written "superseded", a failed retry); only the gated landing needs `finished`
        item = item_of(branch, record)
        done, extras = already_on_trunk(repo, trunk, branch, conv, item)
        if done:
            results[branch] = land_already(repo, state_dir, branch, record, trunk, extras,
                                           dry_run, out)
            continue
        state = superseded_by(items, item)
        if state:
            results[branch] = archive_superseded(repo, state_dir, branch, record, item, state,
                                                 dry_run, out)
            continue
        if not is_eligible(record, sessions_path(state_dir)):
            continue
        eligible.append((branch, record))
    for branch, record in sorted(sessions.items()):  # its branch gone from origin
        if branch not in on_origin and branch != trunk and lifecycle.eligible(record):
            closed = close_merged(repo, state_dir, branch, record, trunk, dry_run, out)
            if closed:
                results[branch] = closed
    to_land = []
    lane = None
    ready = []  # PRs its checks allow, merged after one local gate over them all
    for branch, record in cap_to_tick(pr_order(eligible, items) if slug else eligible, out, conv):
        item = item_of(branch, record)
        if has_adjudicate_commit(repo, trunk, branch):
            out(f'held {branch}: ruling belongs in the record')
            results[branch] = 'held'
            continue
        refusal = lane_refusal(repo, trunk, branch, item)
        if refusal:
            if dry_run:  # a dry run writes nothing — not even a hold
                out(f'DRY: would hold {branch}: {refusal[1]}')
                results[branch] = 'dry'
                continue
            results[branch] = hold_with_correction(state_dir, branch, record, *refusal, out)
            continue
        cls, matched_file = approvals.merge_class(product, touched_files(repo, trunk, branch))
        level = approvals.level_of(product, cls)
        if level != 'auto' and not approvals.is_granted(product, f'{item}/{cls}'):
            detail = matched_file or 'routine'
            if not dry_run:
                approvals.refuse(product, item, cls, level, record.get('job') or branch,
                                 'harvest', detail)
            out(f'held {branch}: {cls} ({level}) — {detail}')
            results[branch] = 'held'
            continue
        if slug:
            lane = lane or PrLane(product, slug, trunk)
            docs = is_docs_branch(conv, branch, touched_files(repo, trunk, branch))
            verdict = land_pr(repo, state_dir, branch, record, lane, conv, docs, dry_run, out)
            if isinstance(verdict, tuple):  # its checks allow it: the local gate, or CI's
                ready.append((branch, record, verdict[1], verdict[0], docs))
            else:
                results[branch] = verdict
            continue
        if mode == LANDING_PR:
            if not dry_run:
                mark_session(state_dir, record.get('job') or branch, harvest='pr')
            out(f'pr-lane {branch}')
            results[branch] = 'pr'
            continue
        if asf_repo is None:
            asf_repo = is_asf_repo(repo)
        if str(conv.harvest_gate).strip().lower() == GATE_PER_BRANCH:
            results[branch] = land_ff(repo, state_dir, branch, record, item, conv, asf_repo,
                                      bug_root, dry_run, out,
                                      item_writes=((items or {}).get(item) or {}).get('writes') or ())
        else:
            to_land.append((branch, record))
    if ready:
        if asf_repo is None:
            asf_repo = is_asf_repo(repo)
        results.update(land_ready(repo, state_dir, ready, lane, conv, asf_repo, dry_run, out,
                                  items=items))
    if to_land:  # B-0040: one gate over the combined head, bisecting on red
        results.update(land_combined(repo, state_dir, to_land, conv, asf_repo, dry_run, out,
                                     items=items))
    if any(r == 'landed' for r in results.values()):
        if slug:
            sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=repo)
        sync_checkout(repo, trunk, out)
    return results


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
