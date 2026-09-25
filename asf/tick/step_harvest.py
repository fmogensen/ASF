"""asf.tick.step_harvest — the tick's ``harvest`` step: land the finished lane branches.

Runs after ``prs`` and before ``batch``. Every lane branch the in-process lane pass brought to
GATE (:mod:`asf.harvest.lane`) is gated and landed by the product's ``landing`` convention
(:func:`asf.harvest.harvest.run_product_harvest` with ``lane_pass=False``, R2): the combined head
fast-forwarded onto the trunk, or its PR merged. A red gate holds the branch and hands it back to
its session; a red trunk, a timeout or a spent budget only waits.

The gate is a test run that can take many minutes, and the tick runs on a clock that never
overlaps itself: a tick that waited on it launched nothing until it ended. So the harvest runs on
its own clock — the step starts it as a detached background process (:func:`spawn_background`,
``python -m asf.tick.step_harvest``) and returns at once. That process takes the product's
harvest lock (:func:`asf.harvest.harvest.try_lock`) for its whole run, so there is still one
gate per landing batch, never two at a time, and landing stays fast-forward only. While it holds
the lock a tick prints one ``harvest: gate running`` line and starts nothing; the first tick
after it ends prints what it printed (``landed …``, ``held …``) once, then starts the next.

A held branch is not a failed step: it is one line, and the next harvest looks again.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

from asf import detach, env
from asf.harvest import harvest


def status_path(product):
    return os.path.join(env.state_dir(product), 'harvest.json')


def items_path(product):
    return os.path.join(env.state_dir(product), 'harvest-items.json')


def log_path(product):
    return os.path.join(env.log_dir(), f'harvest-{product.name}.log')


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_status(product):
    try:
        with open(status_path(product), encoding='utf-8') as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(product, rec):
    path = status_path(product)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rec, f, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def report_last(ctx, out):
    """The last background run's lines, once: the first tick after it finished prints them, and
    however many branches it landed count into ``ctx.counts['merges']`` — the tick that reports
    them is the one the digest credits, since the landing itself ran on the harvest's own clock."""
    product = ctx.product
    rec = read_status(product)
    if not rec.get('finished') or rec.get('reported'):
        return
    out(f"harvest: last run {rec.get('started', '?')} → {rec['finished']}")
    for line in rec.get('lines') or []:
        out(line)
    ctx.counts['merges'] += rec.get('merges', 0)
    write_status(product, dict(rec, reported=True))


def background(product, items_file=None, out=print):
    """One whole harvest under the product's harvest lock — what the detached process runs.
    Another harvest holding the lock: one line, nothing gated. Writes ``harvest.json`` as it
    starts and as it ends (its lines, for the next tick to print). Returns 0, or 1 when the
    harvest itself raised."""
    state_dir = os.path.abspath(env.state_dir(product))
    lock = harvest.try_lock(state_dir)
    if lock is None:
        out(f'harvest: another harvest of {product.name} holds the lock — skipped')
        return 0
    rc = 0
    try:
        rec = {'pid': os.getpid(), 'started': _stamp()}
        write_status(product, rec)
        lines = []

        def emit(line):
            lines.append(line)
            out(line)
        items = None
        if items_file:
            try:
                with open(items_file, encoding='utf-8') as f:
                    items = json.load(f)
            except (OSError, ValueError):
                items = None
        merges = 0
        try:
            # R2: the tick ran the lane's feeder-visible pass before the wave; this detached
            # run decides only the gate's outcomes
            results = harvest.run_product_harvest(product, state_dir, out=emit, items=items,
                                                  lane_pass=False)
            if not results:
                emit('harvest: none to land')
            else:
                merges = sum(1 for r in results.values() if r == 'landed')
        except Exception as e:  # noqa: BLE001 — named in the status, never a silent death
            emit(f'harvest: FAILED {(str(e) or type(e).__name__).strip().splitlines()[0]}')
            rc = 1
        write_status(product, dict(rec, finished=_stamp(), lines=lines, reported=False,
                                   merges=merges))
    finally:
        lock.close()
    return rc


def spawn_background(product, items_file=None):
    """Start :func:`background` as a detached process (never the tick's child); its output goes
    to ``logs/harvest-<product>.log``. Returns its pid."""
    import asf
    argv = [sys.executable, '-m', 'asf.tick.step_harvest', '--product', product.name]
    if items_file:
        argv += ['--items', items_file]
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(asf.__file__)))
    path = os.pathsep.join(p for p in (pkg_parent, os.environ.get('PYTHONPATH')) if p)
    child_env = dict(os.environ, ASF_HOME=env.ASF_HOME, PYTHONPATH=path)
    with open(log_path(product), 'ab') as log, open(os.devnull, 'rb') as null:
        return detach.spawn(argv, cwd=os.path.abspath(env.state_dir(product)), env=child_env,
                            stdin=null, stdout=log, stderr=subprocess.STDOUT)


def run(ctx, out=print, spawn=None):
    """The step: report the last background run, then start the next one unless one still
    holds the lock. Never waits on a gate."""
    product = ctx.product
    if not product.repo_dir:
        out('harvest: no repo_dir — nothing to harvest')
        return 0
    lock = harvest.try_lock(os.path.abspath(env.state_dir(product)))
    if lock is None:
        rec = read_status(product)
        out(f"harvest: gate running (pid {rec.get('pid', '?')}, since {rec.get('started', '?')})"
            f" — lands when it finishes, this tick does not wait")
        return 0
    lock.close()
    report_last(ctx, out)
    from asf.tick import step_wave  # R2: the lane pass is in-process — here when no wave ran it
    step_wave.lane_pass(ctx, out)
    items_file = None
    items = harvest.record_items(ctx.record_root()) if ctx.has_record else None
    if items is not None:
        items_file = items_path(product)
        with open(items_file, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False)
    pid = (spawn or spawn_background)(product, items_file)
    out(f'harvest: started in the background (pid {pid}) — {log_path(product)}')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='asf.tick.step_harvest')
    p.add_argument('--product', required=True)
    p.add_argument('--items')
    args = p.parse_args(argv)
    from asf.cli import line_buffered
    line_buffered(sys.stdout, sys.stderr)
    product = env.load_product(args.product)
    os.environ['ASF_PRODUCT'] = product.name
    print(f'harvest: {_stamp()} start (pid {os.getpid()})')
    return background(product, args.items,
                      out=lambda line: print(f'{_stamp()} {line}'))


if __name__ == '__main__':
    sys.exit(main())
