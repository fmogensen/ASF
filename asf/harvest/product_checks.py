"""asf.harvest.product_checks — the product's own ``conventions.check_commands``, run by the
harvest itself (G2 ask 3), never inside a session: a review or correct brief attaches the result
(:mod:`asf.briefs.build`) instead of telling the session to run them.

Modelled on :mod:`asf.harvest.rebuild_check`: the same background job, the same tree+content
key, the same one-check-at-a-time lock per product, and the same
``state/<product>/<STORE_DIR>/<key>.json`` store, pruned the same way. The one difference is what
runs — every one of ``check_commands``, not just ``pre_push_check`` — and every command runs
whatever an earlier one's exit was: the brief reports all of them, never stops at the first red
one (:func:`asf.harvest.lane.run_check_commands`).
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

STORE_DIR = 'product-checks'
LOCK = 'running.lock'
#: a result older than this is dropped (its branch moved on long ago)
KEEP_S = 2 * 24 * 3600


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def store(state_dir):
    return os.path.join(state_dir, STORE_DIR)


def key(repo, sha, commands, setup=None):
    """``<tree>-<content hash>`` of ``sha``, or None when git cannot read its tree."""
    from asf import gitops
    r = gitops.git(['rev-parse', f'{sha}^{{tree}}'], repo)
    tree = (r.data or '').strip() if r.ok else ''
    if not tree:
        return None
    h = hashlib.sha256(f'{setup or ""}\0{chr(0).join(commands)}'.encode()).hexdigest()[:12]
    return f'{tree}-{h}'


def result_path(state_dir, k):
    return os.path.join(store(state_dir), f'{k}.json')


def read_result(state_dir, k):
    try:
        with open(result_path(state_dir, k), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('results'), list) else None


def write_result(state_dir, k, results, sha):
    d = store(state_dir)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f'.{k}.{os.getpid()}.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'results': results, 'sha': sha, 'at': _now()}, f)
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
    """The product's product-checks lock (an open file holding ``flock``), or None when a check
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
    ``logs/product-checks-<product>.log``. Returns its pid."""
    from asf import detach, hermetic
    argv = [sys.executable, '-m', 'asf.harvest.product_checks', '--product', product.name,
            '--sha', sha, '--key', k] + (['--state-dir', state_dir] if state_dir else [])
    child_env = hermetic.build(worktree=hermetic.package_parent(),
                               identity={'ASF_HOME': env.ASF_HOME})
    log = os.path.join(env.ASF_HOME, 'logs', f'product-checks-{product.name}.log')
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, 'ab') as out, open(os.devnull, 'rb') as null:
        return detach.spawn(argv, cwd=os.path.abspath(env.state_dir(product)), env=child_env,
                            stdin=null, stdout=out, stderr=subprocess.STDOUT)


def ensure(product, repo, state_dir, sha, commands, setup=None, spawn_fn=None, dry_run=False):
    """``[{command, rc, tail}]`` for ``commands`` run on ``sha`` — a job already judged this
    content, else None: started now in the background (or another check already runs), and the
    caller never waits. ``None``/empty ``commands`` is always None, with nothing started."""
    if not commands:
        return None
    k = key(repo, sha, commands, setup)
    if k is None:
        return None
    got = read_result(state_dir, k)
    if got is not None:
        return got['results']
    busy = running(state_dir)
    if busy is not None:
        return None
    if dry_run:
        return None
    prune(state_dir)
    (spawn_fn or spawn)(product, sha, k, state_dir)
    got = read_result(state_dir, k)  # a spawn that runs it in place (tests) may finish already
    return got['results'] if got is not None else None


def run_job(product, sha, k, out=print, state_dir=None):
    """What the detached job does: under the lock, every declared check, its result. Always 0 —
    a check that fails is still a result, not a job error; only another check holding the lock
    skips this pass (the next one starts it again)."""
    from asf.harvest import lane as lane_mod
    state_dir = os.path.abspath(state_dir or env.state_dir(product))
    lock = try_lock(state_dir)
    if lock is None:
        out(f'product checks: another check of {product.name} runs — {sha[:9]} waits')
        return 0
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({'key': k, 'sha': sha, 'since': _now(), 'pid': os.getpid()}))
        lock.flush()
        commands = product.conventions.get('check_commands')
        if not isinstance(commands, list) or not commands:
            write_result(state_dir, k, [], sha)
            return 0
        repo = product.repo_dir
        setup = getattr(product.conventions, 'worktree_setup', None)
        results, line = lane_mod.run_check_commands(repo, state_dir, sha, commands, setup)
        if results is None:  # no checkout: nothing judged — the next pass starts it again
            out(f'product checks {sha[:9]}: {line}')
            return 0
        write_result(state_dir, k, results, sha)
        failed = sum(1 for r in results if r['rc'] != 0)
        out(f'product checks {sha[:9]}: {len(results) - failed}/{len(results)} passed')
        return 0
    finally:
        lock.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog='asf.harvest.product_checks')
    p.add_argument('--product', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--state-dir')
    a = p.parse_args(argv)
    return run_job(env.load_product(a.product), a.sha, a.key, state_dir=a.state_dir)


if __name__ == '__main__':
    sys.exit(main())
