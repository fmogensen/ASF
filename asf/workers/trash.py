"""asf.workers.trash — a removed worktree leaves git at once and the disk in the background.

Deleting a worker worktree is slow: a pnpm/node tree is ~115 000 files and ~1.9 GB, and
``git worktree remove`` (or ``rm -rf``) spends ~8 s on it on an idle Mac and ~60 s on a loaded
one — a product tick reaping ten spent 611 s in its health step, with the wave and harvest
waiting behind it. Only the bookkeeping has to happen inside the tick, so :func:`discard`:

1. refuses a tree with uncommitted or untracked files (``check_clean``, what ``git worktree
   remove`` without ``--force`` refuses);
2. renames the directory into ``<state>/trash/<name>-<stamp>`` — the same filesystem, so O(1);
3. ``git worktree prune``: the worktree's admin dir goes and its branch is free at once (a later
   ``worktree add -B`` of it succeeds);
4. :func:`kick`: a detached, ``nice``'d deleter empties the trash after the tick has moved on.

The deleter is a stdlib-only ``python -c`` script (never ``asf tick`` nor
``asf.tick.step_harvest``, so the upgrade drain never waits on it, and a reinstall cannot tear
it). One runs at a time (``flock`` on ``trash/.lock``); it re-lists the trash until a pass
removes nothing, so what arrives while it runs goes too. An entry it cannot delete stays in the
trash and the next kick — every reaper pass kicks while the trash is not empty — tries again.
"""
import os
import subprocess
import sys
import time

from asf import detach

TRASH = 'trash'
LOCK = '.lock'

#: The deleter: ``argv[1]`` is the trash dir. One at a time; each entry ``rm -rf``'d; loops while
#: a pass removes something (new arrivals); what it cannot remove stays for the next kick.
DELETER = r'''
import fcntl, os, subprocess, sys
trash = sys.argv[1]
try:
    os.nice(19)
except OSError:
    pass
fd = os.open(os.path.join(trash, '.lock'), os.O_CREAT | os.O_RDWR, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)
failed = set()
while True:
    names = [n for n in sorted(os.listdir(trash)) if n != '.lock' and n not in failed]
    if not names:
        break
    for n in names:
        p = os.path.join(trash, n)
        subprocess.run(['rm', '-rf', p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.lexists(p):
            failed.add(n)
'''


def trash_dir(state_dir):
    return os.path.join(state_dir, TRASH)


def pending(state_dir):
    """The entries still waiting in the trash (the lock file aside)."""
    try:
        return sorted(n for n in os.listdir(trash_dir(state_dir)) if n != LOCK)
    except OSError:
        return []


def _git(args, cwd, timeout=60):
    try:
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, '', str(e))


def discard(repo, state_dir, path, check_clean=True, kick_deleter=True, now=None):
    """Take the worktree at ``path`` out of ``repo`` and into the trash. ``(ok, why)``: ok with
    the trash path, or not with the reason (nothing touched). ``check_clean`` refuses a tree with
    changes, as ``git worktree remove`` without ``--force`` does. A rename that fails (another
    filesystem) falls back to ``git worktree remove`` inline."""
    if check_clean:
        st = _git(['status', '--porcelain'], path)
        if st.returncode != 0:
            return False, (st.stderr or 'not a git worktree').strip().splitlines()[-1]
        if st.stdout.strip():
            return False, f'{len(st.stdout.strip().splitlines())} uncommitted file(s)'
    tdir = trash_dir(state_dir)
    os.makedirs(tdir, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() if now is None else now))
    dest = os.path.join(tdir, f'{os.path.basename(path.rstrip(os.sep))}-{stamp}')
    n = 1
    while os.path.lexists(dest):
        n += 1
        dest = os.path.join(tdir, f'{os.path.basename(path.rstrip(os.sep))}-{stamp}-{n}')
    try:
        os.rename(path, dest)
    except OSError:
        args = ['worktree', 'remove'] + ([] if check_clean else ['--force']) + [path]
        p = _git(args, repo, timeout=600)
        if p.returncode != 0:
            return False, ((p.stderr or p.stdout).strip().splitlines() or ['refused'])[-1]
        return True, path
    _git(['worktree', 'prune'], repo)
    if kick_deleter:
        kick(state_dir)
    return True, dest


def kick(state_dir, spawn=None):
    """Start the background deleter when the trash holds anything; the pid, or None (nothing to
    delete, or it could not start — the next kick tries again). Never raises."""
    if not pending(state_dir):
        return None
    spawn = spawn or detach.spawn
    try:
        return spawn([sys.executable, '-I', '-S', '-c', DELETER, trash_dir(state_dir)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    except OSError:
        return None
