"""asf.snapshot — the scheduled tick runs from an immutable snapshot, never the live checkout.

When ``asf`` runs from a development checkout (the self-hosting product: the factory ticks its
own repo), that checkout moves under the clock: harvest fast-forwards it, the operator pulls,
rebases and edits in it. A tick that imports while files are being rewritten gets a torn tree —
one module new, its dependency still old — and every step fails with an ImportError.

So the scheduler (:func:`asf.scheduler.render`) does not start ``python -m asf.cli tick`` in the
checkout. It starts this file, copied to ``<state>/<product>/code/launch.py`` at install, which:

1. resolves the checkout's ``HEAD`` to a commit sha — one atomic ref read;
2. makes sure a detached worktree of that sha exists at ``<state>/<product>/code/<sha>``
   (created once, under a lock, and marked complete only after ``git worktree add`` finished;
   reused while ``HEAD`` stays put — a commit's tree never changes);
3. records the sha it runs in ``code/current`` (what ``asf doctor`` shows) and stamps the
   snapshot's use, then prunes to at most :data:`KEEP_MAX` snapshots plus the ones nothing has
   used for :data:`KEEP_UNUSED_S` (a long tick still running from the previous one keeps it —
   the :data:`IN_USE_S` grace);
4. brings its own installed copy up to date with the snapshot's ``asf/snapshot.py``, for the
   next launch — nothing else ever rewrites it;
5. ``exec``s the interpreter with the given arguments, ``cwd`` and ``PYTHONPATH`` the snapshot.

A snapshot that cannot be made refuses the tick (exit 2 with the reason): running from the live
checkout instead is exactly the failure this exists to prevent, and the clock's log and the
doctor's scheduler row make the refusal visible.

Standard library only, and no import of ``asf``: the launcher must never itself import from the
tree it protects.
"""
import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time

#: A snapshot nothing has launched from for this long is pruned (the current one never is).
KEEP_UNUSED_S = 24 * 3600

#: Snapshots kept beyond the current one, by last use (C4).
KEEP_MAX = 4

#: A snapshot launched from this recently is never removed — a tick may still be running from it.
IN_USE_S = 3600

#: The file in the code dir that names the sha the clock last ran.
CURRENT = 'current'

LAUNCHER = 'launch.py'

_SHA_RE = re.compile(r'^[0-9a-f]{40}$|^[0-9a-f]{64}$')

#: A hook's variables would point a child ``git`` at another repo.
_GIT_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')


class SnapshotError(Exception):
    pass


def _git(args, cwd):
    env = {k: v for k, v in os.environ.items() if k not in _GIT_VARS}
    p = subprocess.run(['git'] + list(args), cwd=cwd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def is_checkout(root):
    """True when ``root`` is a git working tree (``.git`` a directory, or a linked worktree's
    file) — the case where the package can change under a running clock."""
    return bool(root) and os.path.exists(os.path.join(root, '.git'))


def source_checkout(path):
    """When ``path`` is one of these snapshots (``…/code/<sha>``, a linked worktree), the main
    checkout it was made from; else None."""
    if not (_SHA_RE.match(os.path.basename(os.path.normpath(path)))
            and os.path.basename(os.path.dirname(os.path.normpath(path))) == 'code'):
        return None
    try:
        common = _git(['rev-parse', '--path-format=absolute', '--git-common-dir'], path)
    except SnapshotError:
        return None
    return os.path.dirname(common) if os.path.basename(common) == '.git' else None


def head_sha(repo):
    """The commit ``HEAD`` of ``repo`` points at."""
    sha = _git(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], repo)
    if not _SHA_RE.match(sha):
        raise SnapshotError(f'HEAD of {repo} is not a commit: {sha!r}')
    return sha


def _marker(code_dir, sha):
    return os.path.join(code_dir, f'{sha}.ok')


def snapshots(code_dir):
    """``[sha]`` of every complete snapshot under ``code_dir``."""
    if not os.path.isdir(code_dir):
        return []
    return sorted(n for n in os.listdir(code_dir)
                  if _SHA_RE.match(n) and os.path.isfile(_marker(code_dir, n)))


def current(code_dir):
    """``(sha, when)`` the clock last ran from, or ``(None, None)``."""
    path = os.path.join(code_dir, CURRENT)
    try:
        with open(path, encoding='utf-8') as f:
            sha = f.read().strip()
        return (sha or None), os.path.getmtime(path)
    except OSError:
        return None, None


def _write(path, text):
    tmp = f'{path}.tmp-{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _remove(repo, path):
    try:
        _git(['worktree', 'remove', '--force', path], repo)
    except SnapshotError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def refresh_launcher(snapshot_dir, launcher):
    """Bring the installed launcher up to date with the snapshot's own ``asf/snapshot.py``.

    The clock runs a copy (``<code>/launch.py``) written once by ``asf clock install``; without
    this, a fix to this file would ship and never be what the clock executes. The copy is
    replaced for the *next* launch — this process goes on running the code it was started with.

    Never called when this file is running as the package module: the source is another sha's
    copy and the target is ``__file__``, which would be a developer's working file.
    """
    source = os.path.join(snapshot_dir, 'asf', 'snapshot.py')
    try:
        with open(source, encoding='utf-8') as f:
            new = f.read()
        with open(launcher, encoding='utf-8') as f:
            if f.read() == new:
                return False
        _write(launcher, new)
    except OSError:
        return False
    return True


def prune(repo, code_dir, keep=(), now=None, keep_unused_s=KEEP_UNUSED_S,
          keep_max=KEEP_MAX, in_use_s=IN_USE_S):
    """Remove the snapshots that are neither current, in use, nor among the most recent.

    A snapshot survives when it is in ``keep`` (the sha this tick runs), when something launched
    from it within ``in_use_s`` (a tick may still be inside it — the marker is stamped at launch
    and not again), or when it is one of the ``keep_max`` most recently used. Everything else
    goes, however young: the trunk of a self-hosting product takes ~150 commits a day and each
    snapshot is a whole working tree, so age alone is not a bound (C4). ``keep_unused_s`` stays
    the second rule, so a pool under the cap still empties when the clock stops.
    """
    now = time.time() if now is None else now
    pairs = []
    for name in sorted(os.listdir(code_dir)) if os.path.isdir(code_dir) else ():
        if not _SHA_RE.match(name) or name in keep:
            continue
        marker = _marker(code_dir, name)
        try:
            used = os.path.getmtime(marker if os.path.exists(marker) else os.path.join(code_dir, name))
        except OSError:
            continue
        pairs.append((name, used))
    survivors = ({n for n, u in pairs if now - u < in_use_s}
                 | {n for n, _ in sorted(pairs, key=lambda p: (p[1], p[0]), reverse=True)[:keep_max]})
    removed = []
    for name, used in pairs:
        if name in survivors and now - used < keep_unused_s:
            continue
        marker = _marker(code_dir, name)
        _remove(repo, os.path.join(code_dir, name))
        if os.path.exists(marker):
            os.remove(marker)
        removed.append(name)
    if removed:
        try:
            _git(['worktree', 'prune'], repo)
        except SnapshotError:
            pass
    return removed


def ensure(repo, code_dir, sha=None, now=None):
    """The snapshot of ``repo`` at ``sha`` (default its HEAD): its path under ``code_dir``,
    created when missing, reused when complete. Records it as :data:`CURRENT` and prunes the
    unused ones. Raises :class:`SnapshotError` when it cannot be made."""
    os.makedirs(code_dir, exist_ok=True)
    sha = sha or head_sha(repo)
    path = os.path.join(code_dir, sha)
    marker = _marker(code_dir, sha)
    with open(os.path.join(code_dir, '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not os.path.isfile(marker):
            if os.path.exists(path):  # an interrupted add: never trusted, made again
                _remove(repo, path)
                try:
                    _git(['worktree', 'prune'], repo)
                except SnapshotError:
                    pass
            # no hook of the checkout's runs for the factory's own snapshot
            _git(['-c', f'core.hooksPath={os.devnull}', 'worktree', 'add', '--detach', '-q',
                  path, sha], repo)
            got = _git(['rev-parse', 'HEAD'], path)
            if got != sha:
                raise SnapshotError(f'snapshot {path} is at {got}, not {sha}')
            _write(marker, sha + '\n')
        else:
            os.utime(marker)
        _write(os.path.join(code_dir, CURRENT), sha + '\n')
        prune(repo, code_dir, keep={sha}, now=now)
    return path


def launch_env(snapshot, base=None):
    """The launched tick's environment: ``base`` with ``PYTHONPATH`` the snapshot alone."""
    env = dict(os.environ if base is None else base)
    for var in _GIT_VARS:
        env.pop(var, None)
    env['PYTHONPATH'] = snapshot
    return env


def main(argv=None):
    """``launch.py --repo <checkout> --code-dir <dir> -- <python args>``: snapshot, then exec."""
    parser = argparse.ArgumentParser(prog='asf-snapshot-launch')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--code-dir', required=True)
    parser.add_argument('rest', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.rest[1:] if args.rest[:1] == ['--'] else args.rest
    if not rest:
        parser.error('nothing to run after --')
    try:
        snap = ensure(args.repo, args.code_dir)
    except (SnapshotError, OSError) as e:
        print(f'snapshot: REFUSED — no snapshot of {args.repo}: {e}', file=sys.stderr)
        return 2
    print(f'snapshot: {os.path.basename(snap)[:12]} ({snap})', flush=True)
    me = os.path.abspath(__file__)
    if os.path.basename(me) == LAUNCHER and refresh_launcher(snap, me):
        print(f'snapshot: launcher refreshed from {os.path.basename(snap)[:12]}', flush=True)
    os.chdir(snap)
    os.execve(sys.executable, [sys.executable] + rest, launch_env(snap))
    return 0  # pragma: no cover - execve does not return


if __name__ == '__main__':
    sys.exit(main())
