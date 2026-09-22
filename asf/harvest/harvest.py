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
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

from asf import env
from asf.conventions import Conventions
from asf.workers import lifecycle
from asf.workers.health import pid_alive
from asf.workers.pool import now_iso

#: The conventions a caller with no Product reads: trunk `main`, code branches `worker/`, no
#: test command (so the gate is `asf check` alone).
DEFAULTS = Conventions()

CONFLICT_START_RE = re.compile(r'^<{7}(?: |$)')
CONFLICT_MID_RE = re.compile(r'^={7}$')
CONFLICT_END_RE = re.compile(r'^>{7}(?: |$)')
MEMORY_CLAUSE_RE = re.compile(r'; memory [^;"]+')
MAX_REBASE_STEPS = 100

#: B-0031 (interim): a tick runs on a clock, and CI races the landings it triggers — gating every
#: eligible branch in one tick can run past the clock and pile up races. Cap how many a single
#: tick gates; the rest sit as still-eligible and are picked up by the next tick.
MAX_BRANCHES_PER_TICK = 3


def cap_to_tick(items, out):
    """``items`` trimmed to :data:`MAX_BRANCHES_PER_TICK`, printing the cap line via ``out``
    when there were more eligible than that — the rest wait for the next tick."""
    if len(items) > MAX_BRANCHES_PER_TICK:
        out(f'harvest: {len(items)} branches eligible — capping this tick at '
            f'{MAX_BRANCHES_PER_TICK}, the rest wait for the next')
        return items[:MAX_BRANCHES_PER_TICK]
    return items


GIT_HOOK_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')


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


# The variables that name the CALLER — the tick's product, a worker session's job and mint
# range. None may reach the branch's own test run: the gate is the branch's result, not the
# caller's (B-0033: the tick's ASF_PRODUCT made ASF's own "no product configured" tests read
# the live product and go red). ASF_HOME stays: it is the operator's home, not an identity.
CALLER_IDENTITY_VARS = ('ASF_PRODUCT', 'ASF_JOB', 'BACKLOG_ID_RANGE')


def gate_env(worktree=None):
    """The environment the gate and index regeneration run in: the caller's own, minus the
    caller's identity (:data:`CALLER_IDENTITY_VARS`) — BACKLOG_ID_RANGE names the calling
    session's own mint range and must never leak into a branch it didn't spawn (a worker
    session invoking `--dry-run` against the live repo would otherwise misjudge an unrelated
    branch's tests as failing); ASF_PRODUCT/ASF_JOB name the tick or session running the gate.

    PYTHONPATH: the gated ``worktree`` first, then the package that is harvesting. A test in
    the worktree that runs ``python -m asf.cli`` from some other cwd must get the worktree's
    own code — the branch under test — not the harvester's (B-0033: the sample-product test
    ran main's package against the branch's fixtures); `asf index`/`asf check` from a product
    repo with no ``asf`` package of its own still find the harvester's."""
    env = clean_env()
    for var in CALLER_IDENTITY_VARS:
        env.pop(var, None)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parts = ([os.path.abspath(worktree)] if worktree else []) + [pkg_parent]
    if env.get('PYTHONPATH'):
        parts.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(parts)
    return env


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


def is_eligible(record):
    """What harvest may gate: :func:`asf.workers.lifecycle.eligible` — ended ``finished`` (which
    health writes only for a pushed branch), not landed, not handed to the PR lane."""
    return lifecycle.eligible(record)


def mark_harvested(state_dir, job, sha):
    """One appended registry line: this job's branch has landed. The registry is append-only —
    a later line for the same job is a field update (see asf.workers.pool)."""
    path = sessions_path(state_dir)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'job': job, 'harvested': sha}, sort_keys=True) + '\n')


def reap_hold(record):
    """Why a job's worktree may not be reaped, or None. Both must hold: the job's own record
    carries a finished line (``ended``), and the process id in it is dead (B-0010)."""
    if not record or not record.get('ended'):
        return 'no finished line in its record'
    pid = record.get('pid')
    if pid_alive(pid):
        return f'pid {pid} is still alive'
    return None


def reap(repo, state_dir, job, branch, sha=None):
    """Remove the job's worktree, branch and registry row — but only once :func:`reap_hold`
    clears it; otherwise print one hold line and touch nothing. True when reaped."""
    why = reap_hold(read_sessions(state_dir).get(job))
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


def rebase_and_resolve(tmp, trunk='main'):
    """Rebase HEAD onto ``origin/<trunk>``, resolving only machine-owned conflicts.

    Returns (ok, reason). On failure the rebase has already been aborted.
    """
    sh(['git', 'rebase', f'origin/{trunk}'], cwd=tmp)
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
    if conv.test_command:
        t = sh(shlex.split(str(conv.test_command)), cwd=tmp, env=env)
        if t.returncode != 0:
            return False, f'tests failed: {tail(t.stderr or t.stdout)}'
    c = sh(asf_cmd('check'), cwd=tmp, env=env)
    if c.returncode != 0:
        return False, f'asf check failed: {tail(c.stdout or c.stderr)}'
    return True, None


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

def harvest_branch(repo, state_dir, is_record, job, branch, dry_run, conv=None):
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
                gate_ok, gate_reason = run_gate(tmp, conv)
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

            reap(repo, state_dir, job, branch, sha)
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


def run_harvest(repo, state_dir, dry_run, conv=None):
    conv = conv or DEFAULTS
    repo = os.path.abspath(repo)
    state_dir = os.path.abspath(state_dir)
    is_record = is_record_repo(repo)
    sessions = read_sessions(state_dir)
    sh(['git', 'fetch', '-q', 'origin', conv.main], cwd=repo)
    eligible = []
    for branch in worker_branches(repo, conv):
        job = conv.strip_prefix(branch)
        if not is_eligible(sessions.get(job)):
            continue
        ahead = sh(['git', 'rev-list', '--count', f'origin/{conv.main}..{branch}'],
                   cwd=repo).stdout.strip()
        if ahead in ('', '0'):
            continue
        eligible.append((job, branch))
    for job, branch in cap_to_tick(eligible, print):
        why = reap_hold(sessions.get(job))
        if why:  # never land what cannot then be reaped: a live job still owns its worktree
            hold(job, f'not reaped: {why}')
            continue
        harvest_branch(repo, state_dir, is_record, job, branch, dry_run, conv)
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


def remote_branches(repo, conv):
    """Every ``origin/<prefix>*`` branch, the prefix stripped of ``origin/``, for every prefix the
    product's branches can carry."""
    out = []
    for prefix in conv.all_prefixes():
        r = sh(['git', 'for-each-ref', '--format=%(refname:short)',
                f'refs/remotes/origin/{prefix}*'], cwd=repo)
        out.extend(l[len('origin/'):] for l in r.stdout.splitlines() if l.startswith('origin/'))
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


def product_gate(tmp, conv, asf_repo):
    """``(ok, first failing line)``: the product's test command, then — on asf's own repo — its
    generic and conventions checks."""
    cmds = []
    if conv.test_command:
        cmds.append(shlex.split(str(conv.test_command)))
    if asf_repo:
        cmds += [['bash', os.path.join('tools', name + '.sh')] for name in ASF_GATE_SCRIPTS]
    env = gate_env(tmp)
    for cmd in cmds:
        r = sh(cmd, cwd=tmp, env=env)
        if r.returncode != 0:
            return False, first_failing_line((r.stdout or '') + '\n' + (r.stderr or ''))
    return True, None


def hold_with_correction(state_dir, branch, record, kind, text, out):
    """Hold ``branch`` and hand it back to its session: :func:`asf.workers.lifecycle.hold` says
    what goes on the run (the failing output as ``correction``, the rounds over every session of
    the item, the cap the feeder switches an ADJUDICATE row on at) and what to print."""
    job = record.get('job') or branch
    fields, line = lifecycle.hold(sessions_path(state_dir), dict(record, branch=branch, job=job),
                                  kind, text, now_iso())
    mark_session(state_dir, job, **fields)
    out(line)
    return 'held'


def land_ff(repo, state_dir, branch, record, item, conv, asf_repo, bug_root, dry_run, out):
    trunk = conv.main
    job = record.get('job') or branch
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
            ok, line = product_gate(tmp, conv, asf_repo)
            if not ok:
                return hold_with_correction(state_dir, branch, record, 'gate', line, out)
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


def run_product_harvest(product, state_dir=None, dry_run=False, bug_root=None, out=print):
    """Land (or hand to the PR lane) every finished lane branch on the product repo's origin.
    ``bug_root`` is the record a red gate files its Bug in — a path, or a callable returning
    one (the tick's record clone, made only when needed); None prints ``BUG:`` instead.
    Returns ``{branch: outcome}``."""
    conv = product.conventions
    repo = os.path.abspath(product.repo_dir)
    if is_record_repo(repo):  # the record's own gate and index regeneration, as before
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
    for branch in remote_branches(repo, conv):
        record = sessions.get(branch)
        if not is_eligible(record) or record.get('harvest') == 'pr':
            continue
        ahead = sh(['git', 'rev-list', '--count', f'origin/{trunk}..origin/{branch}'],
                   cwd=repo).stdout.strip()
        if ahead in ('', '0'):
            continue
        eligible.append((branch, record))
    for branch, record in cap_to_tick(eligible, out):
        item = item_of(branch, record)
        if not item or not commits_name_item(repo, trunk, branch, item):
            out(f'held {branch}: commits do not name {item or "an item id"}')
            results[branch] = 'held'
            continue
        if mode == LANDING_PR:
            if not dry_run:
                mark_session(state_dir, record.get('job') or branch, harvest='pr')
            out(f'pr-lane {branch}')
            results[branch] = 'pr'
            continue
        if asf_repo is None:
            asf_repo = is_asf_repo(repo)
        results[branch] = land_ff(repo, state_dir, branch, record, item, conv, asf_repo,
                                  bug_root, dry_run, out)
    if any(r == 'landed' for r in results.values()):
        sync_checkout(repo, trunk, out)
    return results


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
    run_product_harvest(product, state_dir, args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
