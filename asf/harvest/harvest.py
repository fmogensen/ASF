#!/usr/bin/env python3
"""harvest.py — land green `worker/*` branches on `main` with no hand merge.

Run by the tick (`~/.claude-workers/backlog-tick`), in step 0 after the pull. For every
`worker/<job>` branch of a repo whose `~/.claude-workers/logs/<job>.meta` says `finished=` and
`rc=0` and which carries commits not on `origin/main`: rebase it onto `origin/main` in a
throwaway worktree (never the job's own, never the main checkout — that branch is already
checked out there), resolve only machine-owned conflicts, gate it, then land it.

Machine-owned conflicts (README.md "The machine block"): `index.json` (regenerated wholesale
by `backlog.py index` at the end, so a conflicted copy is just discarded — either side would be
overwritten), a `## Children`/`## Backlinks` section (same reasoning: take main's side of the
hunk, `backlog.py index` regenerates it for real afterwards), and a Rule's `source:` line
(unioned — every `; memory …` clause either side carries, deduplicated, README.md's typed-field
grammar makes this a one-line scalar so the merge is a single-hunk text splice). Anything else
conflicting aborts the rebase and holds the branch with one line naming the file — never a
silent guess at someone's intent.

The gate is `python3 -m unittest discover -s tools -p 'test_*.py'` + `backlog.py check`, run
only when `--repo` looks like the record repo itself (a `tools/backlog.py` on disk). A product
repo has no such gate; its branch is rebased and pushed under its own name — never merged to
main, the merge queue owns that (README.md "Non-goals") — and harvest prints the `gh pr create`
line for the tick to run, same as `open-prs.sh` does today.

Landing on `main` is fast-forward only: `git push --force*` to `main` is never used (a rewritten
main is exactly the 2026-09-21 near-miss this card exists to prevent). Instead: fetch, verify
`origin/main` is an ancestor of the rebased tip, plain `git push origin <sha>:refs/heads/main`
(which git itself refuses if that ever turns out false), retried once — re-rebasing onto the new
tip — if `main` moved in between.

Cleanup (worktree, branch, the `worktrees.tsv` row) only happens after a push lands; a held
branch is never touched. Python 3 stdlib only.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

from asf import env

HOME = os.path.expanduser('~')
DEFAULT_WORKERS_DIR = os.path.join(HOME, '.claude-workers')

CONFLICT_START_RE = re.compile(r'^<{7}(?: |$)')
CONFLICT_MID_RE = re.compile(r'^={7}$')
CONFLICT_END_RE = re.compile(r'^>{7}(?: |$)')
MEMORY_CLAUSE_RE = re.compile(r'; memory [^;"]+')
MAX_REBASE_STEPS = 100


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


def gate_env():
    """The environment the gate and index regeneration run in: the caller's own, minus
    BACKLOG_ID_RANGE — that variable names the calling session's own mint range (or the
    tick's, if it ever carries one) and must never leak into a branch it didn't spawn; a
    worker session invoking `--dry-run` against the live repo, as this card's own report
    does, would otherwise misjudge an unrelated branch's tests as failing."""
    env = clean_env()
    env.pop('BACKLOG_ID_RANGE', None)
    return env


def tail(text, n=1):
    lines = [l for l in (text or '').strip().splitlines() if l.strip()]
    return ' / '.join(lines[-n:]) if lines else ''


# ------------------------------------------------------------- job records --

def read_job_meta(workers_dir, job):
    """Parse `logs/<job>.meta` (`key=value` tokens, one line at start, one appended at finish)."""
    path = os.path.join(workers_dir, 'logs', f'{job}.meta')
    if not os.path.isfile(path):
        return None
    fields = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            for tok in line.split():
                if '=' in tok:
                    k, _, v = tok.partition('=')
                    fields[k] = v
    return fields


def is_eligible(meta):
    return bool(meta) and meta.get('finished') and meta.get('rc') == '0'


def drop_tsv_row(workers_dir, job):
    path = os.path.join(workers_dir, 'worktrees.tsv')
    if not os.path.isfile(path):
        return
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    kept = [l for l in lines if l.split('\t', 1)[0] != job]
    if kept != lines:
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(kept)


def reap(repo, workers_dir, job, branch):
    wt_path = os.path.join(workers_dir, 'worktrees', job)
    if os.path.isdir(wt_path):
        sh(['git', 'worktree', 'remove', '--force', wt_path], cwd=repo)
    sh(['git', 'branch', '-D', branch], cwd=repo)
    drop_tsv_row(workers_dir, job)


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


def rebase_and_resolve(tmp):
    """Rebase HEAD onto origin/main, resolving only machine-owned conflicts.

    Returns (ok, reason). On failure the rebase has already been aborted.
    """
    sh(['git', 'rebase', 'origin/main'], cwd=tmp)
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

def run_gate(tmp):
    env = gate_env()
    t = sh([sys.executable, '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_*.py'], cwd=tmp, env=env)
    if t.returncode != 0:
        return False, f'tests failed: {tail(t.stderr or t.stdout)}'
    c = sh([sys.executable, os.path.join('tools', 'backlog.py'), 'check'], cwd=tmp, env=env)
    if c.returncode != 0:
        return False, f'backlog.py check failed: {tail(c.stdout or c.stderr)}'
    return True, None


# --------------------------------------------------------------------- push --

def push_ff(repo, sha):
    """Fast-forward-only push of `sha` to origin/main. Returns (pushed, not_ff)."""
    sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=repo)
    origin_sha = sh(['git', 'rev-parse', 'origin/main'], cwd=repo).stdout.strip()
    anc = sh(['git', 'merge-base', '--is-ancestor', origin_sha, sha], cwd=repo)
    if anc.returncode != 0:
        return False, True
    push = sh(['git', 'push', 'origin', f'{sha}:refs/heads/main'], cwd=repo)
    return push.returncode == 0, False


def push_branch(repo, sha, branch):
    sh(['git', 'fetch', '-q', 'origin', branch], cwd=repo)
    push = sh(['git', 'push', '--force-with-lease', 'origin', f'{sha}:refs/heads/{branch}'], cwd=repo)
    return push.returncode == 0


def repo_slug(repo):
    url = sh(['git', 'remote', 'get-url', 'origin'], cwd=repo).stdout.strip()
    m = re.search(r'[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$', url)
    return m.group(1) if m else url


def pr_create_line(repo, tmp, branch):
    slug = repo_slug(repo)
    subj = sh(['git', 'log', '-1', '--format=%s'], cwd=tmp).stdout.strip() or branch
    title = subj[:69] + '…' if len(subj) > 70 else subj
    title = title.replace('"', '\\"')
    body = f'Opened by the tick (harvest.py) from {branch}.'
    return (f'PR: gh pr create -R {slug} --base main --head {branch} '
            f'--title "{title}" --body "{body}"')


# ---------------------------------------------------------------- per-branch --

def harvest_branch(repo, workers_dir, is_backlog, job, branch, dry_run):
    for _attempt in (1, 2):
        holder = tempfile.mkdtemp(prefix=f'harvest-{job}-')
        tmp = os.path.join(holder, 'wt')
        try:
            branch_sha = sh(['git', 'rev-parse', branch], cwd=repo).stdout.strip()
            add = sh(['git', 'worktree', 'add', '--detach', tmp, branch_sha], cwd=repo)
            if add.returncode != 0:
                return hold(job, f'worktree add failed: {tail(add.stderr)}')

            sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=tmp)
            ok, reason = rebase_and_resolve(tmp)
            if not ok:
                return hold(job, reason)

            if is_backlog:
                idx = sh([sys.executable, os.path.join('tools', 'backlog.py'), 'index'], cwd=tmp, env=gate_env())
                if idx.returncode != 0:
                    return hold(job, f'backlog.py index failed: {tail(idx.stderr or idx.stdout)}')
                if sh(['git', 'status', '--porcelain'], cwd=tmp).stdout.strip():
                    sh(['git', 'add', '-A'], cwd=tmp)
                    commit = sh(['git', '-c', 'core.editor=true', 'commit', '-qm',
                                 'harvest: regenerate index.json'], cwd=tmp)
                    if commit.returncode != 0:
                        return hold(job, f'index regen commit failed: {tail(commit.stderr or commit.stdout)}')
                gate_ok, gate_reason = run_gate(tmp)
                if not gate_ok:
                    return hold(job, gate_reason)

            sha = sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()

            if dry_run:
                dest = 'main' if is_backlog else f'origin/{branch}'
                print(f'DRY: would push {job} {sha} -> {dest}')
                return 'dry'

            if is_backlog:
                pushed, not_ff = push_ff(repo, sha)
                if not_ff:
                    continue  # origin/main moved under us — retry the whole cycle once
                if not pushed:
                    return hold(job, 'push to main failed (not a fast-forward)')
            else:
                if not push_branch(repo, sha, branch):
                    return hold(job, 'push branch failed')
                print(pr_create_line(repo, tmp, branch))

            reap(repo, workers_dir, job, branch)
            print(f'HARVEST OK {job} {sha}')
            return 'ok'
        finally:
            sh(['git', 'worktree', 'remove', '--force', tmp], cwd=repo)
    return hold(job, 'main moved again on retry')


def hold(job, reason):
    print(f'HARVEST HOLD {job} {reason}')
    return 'held'


# -------------------------------------------------------------------- main --

def worker_branches(repo):
    r = sh(['git', 'branch', '--list', 'worker/*', '--format=%(refname:short)'], cwd=repo)
    return sorted(l for l in r.stdout.splitlines() if l.strip())


def run_harvest(repo, workers_dir, dry_run):
    repo = os.path.abspath(repo)
    workers_dir = os.path.abspath(workers_dir)
    is_backlog = os.path.isfile(os.path.join(repo, 'tools', 'backlog.py'))
    sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=repo)
    for branch in worker_branches(repo):
        job = branch[len('worker/'):]
        meta = read_job_meta(workers_dir, job)
        if not is_eligible(meta):
            continue
        ahead = sh(['git', 'rev-list', '--count', f'origin/main..{branch}'], cwd=repo).stdout.strip()
        if ahead in ('', '0'):
            continue
        harvest_branch(repo, workers_dir, is_backlog, job, branch, dry_run)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog='harvest.py')
    p.add_argument('--repo', default=None,
                    help="backlog repo path (default: the product's backlog_dir, see --product)")
    env.add_product_arg(p)
    p.add_argument('--workers-dir', default=DEFAULT_WORKERS_DIR)
    p.add_argument('--dry-run', action='store_true')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    repo = args.repo or env.load_product(args.product).backlog_dir
    return run_harvest(repo, args.workers_dir, args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
