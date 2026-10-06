"""asf.harvest.rebuild_check — the product's pre-push check on a head the lane rebuilt, never
inside the tick.

2026-10-06: a product's tick held its lock for 57 minutes, 13+ of them inside one synchronous
``pre_push_check`` the lane ran on a branch it had rebuilt on the trunk (``drop copies … rebuilt
on origin/main``) — 15 rows launchable, 1 of 8 seats busy. ``lane.rebuild_check`` says where that
check runs instead:

* ``background`` (default) — :func:`check` starts it as a detached job (:func:`main`) and returns
  at once: "deferred". The job holds the product's one rebuild-check lock (one check at a time:
  a product's gate is heavy), runs :func:`asf.harvest.lane.pre_push_check_at` and writes its
  result to ``state/<p>/rebuild-check/<key>.json``. A later pass rebuilds the same branch, finds
  the result and acts on it. The key is the rebuilt head's **tree** and the command: a rebuild
  makes a new commit sha every pass, the content it builds is what the check judged.
* ``session`` — the lane runs no check: the rebuilt branch goes back to its session, which
  rebuilds it, runs the check and pushes (the correction a failed check would have written).
* ``off`` — the rebuilt head is pushed unchecked; the product's CI gates it.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time

from asf import env

STORE_DIR = 'rebuild-check'
LOCK = 'running.lock'
#: a result older than this is dropped (its branch moved on long ago)
KEEP_S = 2 * 24 * 3600


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def store(state_dir):
    return os.path.join(state_dir, STORE_DIR)


def key(repo, sha, command, setup=None):
    """``<tree>-<command hash>`` of ``sha``, or None when git cannot read its tree."""
    from asf import gitops
    r = gitops.git(['rev-parse', f'{sha}^{{tree}}'], repo)
    tree = (r.data or '').strip() if r.ok else ''
    if not tree:
        return None
    h = hashlib.sha256(f'{setup or ""}\0{command}'.encode()).hexdigest()[:12]
    return f'{tree}-{h}'


def result_path(state_dir, k):
    return os.path.join(store(state_dir), f'{k}.json')


def read_result(state_dir, k):
    try:
        with open(result_path(state_dir, k), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and 'ok' in data else None


def write_result(state_dir, k, ok, line, sha):
    d = store(state_dir)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f'.{k}.{os.getpid()}.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'ok': ok, 'line': line, 'sha': sha, 'at': _now()}, f)
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
    """The product's rebuild-check lock (an open file holding ``flock``), or None when a check
    already runs."""
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
    """``{key, sha, since}`` of the check that holds the lock, or None when none runs."""
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


def spawn(product, sha, k, state_dir=None):
    """Start :func:`main` detached (never the tick's child); its output goes to
    ``logs/rebuild-check-<product>.log``. Returns its pid."""
    from asf import detach, hermetic
    argv = [sys.executable, '-m', 'asf.harvest.rebuild_check', '--product', product.name,
            '--sha', sha, '--key', k] + (['--state-dir', state_dir] if state_dir else [])
    child_env = hermetic.build(worktree=hermetic.package_parent(),
                               identity={'ASF_HOME': env.ASF_HOME})
    log = os.path.join(env.ASF_HOME, 'logs', f'rebuild-check-{product.name}.log')
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, 'ab') as out, open(os.devnull, 'rb') as null:
        return detach.spawn(argv, cwd=os.path.abspath(env.state_dir(product)), env=child_env,
                            stdin=null, stdout=out, stderr=subprocess.STDOUT)


def check(product, repo, state_dir, sha, command, setup=None, spawn_fn=None, dry_run=False):
    """``(ok, line)`` for the lane, in ``background`` mode: the check's result when a job already
    judged this content (``ok`` True/False), else ``(None, why)`` — started now in the
    background, or another check still running — and the pass goes on without waiting."""
    k = key(repo, sha, command, setup)
    if k is None:
        return None, f'no tree for {sha[:9]} — the pre-push check waits for the next pass'
    got = read_result(state_dir, k)
    if got is not None:
        return got['ok'], got.get('line') or ''
    busy = running(state_dir)
    if busy is not None:
        what = 'this head' if busy.get('key') == k else f"{str(busy.get('sha') or '?')[:9]}"
        return None, (f"the pre-push check runs in the background on {what} (since "
                      f"{busy.get('since') or '?'}) — the next pass reads its result")
    if dry_run:
        return None, f'DRY: would start the pre-push check on {sha[:9]} in the background'
    prune(state_dir)
    pid = (spawn_fn or spawn)(product, sha, k, state_dir)
    got = read_result(state_dir, k)
    if got is not None:  # a job that finished already (a spawn that runs it in place)
        return got['ok'], got.get('line') or ''
    return None, (f'the pre-push check started in the background on {sha[:9]} (pid {pid}) — '
                  f'the next pass reads its result')


def run_job(product, sha, k, out=print, state_dir=None):
    """What the detached job does: under the lock, the check, its result. 0, or 0 with one line
    when another check holds the lock (the next pass starts this one again)."""
    from asf import approvals
    from asf.harvest import lane as lane_mod
    state_dir = os.path.abspath(state_dir or env.state_dir(product))
    lock = try_lock(state_dir)
    if lock is None:
        out(f'rebuild check: another check of {product.name} runs — {sha[:9]} waits')
        return 0
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({'key': k, 'sha': sha, 'since': _now(), 'pid': os.getpid()}))
        lock.flush()
        command = approvals.pre_push_check(product)
        if not command:
            write_result(state_dir, k, True, '', sha)
            return 0
        repo = product.repo_dir
        setup = getattr(product.conventions, 'worktree_setup', None)
        ok, line = lane_mod.pre_push_check_at(repo, state_dir, sha, command, setup)
        if ok is None:  # no checkout: nothing judged — the next pass starts it again
            out(f'rebuild check {sha[:9]}: {line}')
            return 0
        write_result(state_dir, k, ok, line, sha)
        out(f"rebuild check {sha[:9]}: {'passed' if ok else 'failed'}")
        return 0
    finally:
        lock.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog='asf.harvest.rebuild_check')
    p.add_argument('--product', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--state-dir')
    a = p.parse_args(argv)
    return run_job(env.load_product(a.product), a.sha, a.key, state_dir=a.state_dir)


if __name__ == '__main__':
    sys.exit(main())
