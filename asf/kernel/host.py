"""asf.kernel.host — the kernel's two launchd jobs and their keep-alive (ASF 0.2).

``asf kernel install --product P`` (:func:`install`) writes and loads two jobs from the
product's ``kernel:`` block (:mod:`asf.kernel.settings`), each running this venv's interpreter
(``sys.executable``) with ``ASF_HOME``, ``HOME`` and ``PATH`` — the old floor's plist
environment, rendered by the old scheduler's writer (:class:`asf.connectors.launchd.
LaunchdScheduler`, :func:`asf.scheduler.install`):

- ``asf.<p>.kernel``: ``asf kernel tick --product P`` every ``kernel.tick.interval_s``;
- ``asf.<p>.kernel-watch``: ``asf kernel watch --product P`` every ``kernel.watch.interval_s``.

Idempotent: a job whose plist on disk is the one a render writes now, and that launchd holds, is
left alone; any other (another venv, another interval, not loaded) is rewritten and booted out
and in again. ``--dry-run`` prints the plists and touches nothing.

``asf kernel watch`` (:func:`watch`) is the keep-alive that replaced a hand-written shell script:
the tick job not loaded -> load it (install it when its plist is gone); the last plan older than
``kernel.watch.stale_after_s`` and no tick running (the product's kernel lock is free) ->
``launchctl kickstart`` it. A clock paused through ``asf scheduler pause`` is left paused. One
line per run goes to ``logs/kernel-watch-<p>.log``.
"""
import datetime
import fcntl
import os
import plistlib
import sys

TICK = 'kernel'
WATCH = 'kernel-watch'

#: the tick log lines the watch line quotes / counts
TICK_LINE = 'kernel tick:'
TAIL = 200


def tick_log(name):
    from asf import env
    return os.path.join(env.ASF_HOME, 'logs', 'kernel-%s.log' % name)


def watch_log(name):
    from asf import env
    return os.path.join(env.ASF_HOME, 'logs', 'kernel-watch-%s.log' % name)


def jobs(product, cfg=None, python=None):
    """The two launchd job definitions (``{label, path, plist, log, argv, ...}``), tick first."""
    from asf import env, scheduler
    from asf.connectors import launchd
    cfg = env.load_config() if cfg is None else cfg
    k = product.kernel
    python = python or sys.executable
    env_vars = {'ASF_HOME': env.ASF_HOME, 'HOME': os.path.expanduser('~'),
                'PATH': scheduler._absolute_path_entries(drop_checkouts=True)}
    out = []
    for clock, command, every, log in ((TICK, 'tick', k['tick']['interval_s'], tick_log),
                                       (WATCH, 'watch', k['watch']['interval_s'], watch_log)):
        job = {'kind': 'launchd', 'label': scheduler.label_for(product.name, clock, cfg),
               'log': log(product.name), 'product': product.name, 'clock': clock, 'steps': [],
               'argv': [python, '-m', 'asf.cli', 'kernel', command, '--product', product.name],
               'every_s': int(every)}
        out.append(launchd.LaunchdScheduler(cfg).render(job, env.ASF_HOME, dict(env_vars)))
    return out


def _on_disk(path):
    try:
        with open(path, 'rb') as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def install(product, dry_run=False, cfg=None, python=None, out=print):
    """Write and load both jobs (see the module doc). Returns 0, or 1 when a load failed."""
    from asf import scheduler
    rc = 0
    for job in jobs(product, cfg, python):
        if dry_run:
            out('# %s -> %s' % (job['label'], job['path']))
            out(scheduler.render_plist(job))
            continue
        if _on_disk(job['path']) == job['plist'] and scheduler._launchd_status(job['label'])['loaded']:
            out('kernel: %s unchanged (%s)' % (job['label'], job['argv'][0]))
            continue
        for line in scheduler.install(job):
            out(line)
            rc = 1 if 'failed' in line else rc
    return rc


def tick_running(state_dir):
    """Whether a tick holds the product's kernel lock (:data:`asf.kernel.loop.LOCK_FILE`)."""
    from asf.kernel.loop import LOCK_FILE
    path = os.path.join(state_dir, LOCK_FILE)
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _tail(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read().splitlines()[-TAIL:]
    except OSError:
        return []


def watch(product, now=None, cfg=None, python=None, state_dir=None):
    """One keep-alive pass (see the module doc): ``(exit code, the logged line)``."""
    from asf import env, scheduler
    from asf.kernel.loop import PLAN_FILE
    now = now or datetime.datetime.now(datetime.timezone.utc)
    k = product.kernel
    tick_job = jobs(product, cfg, python)[0]
    label = tick_job['label']
    state_dir = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
    try:
        age = int(now.timestamp() - os.path.getmtime(os.path.join(state_dir, PLAN_FILE)))
    except OSError:
        age = None
    rc, msg = 0, 'ok'
    paused = scheduler.pause_record(label, cfg)
    if paused:
        msg = 'PAUSED (%s)' % scheduler.pause_text(paused)
    elif not scheduler._launchd_status(label)['loaded']:
        if os.path.exists(tick_job['path']):
            ok, err = scheduler.bootstrap(tick_job['path'])
            msg = 'RELOADED (job was not loaded)' if ok else 'RELOAD FAILED (%s)' % err
        else:
            lines = scheduler.install(tick_job)
            ok = not any('failed' in x for x in lines)
            msg = 'INSTALLED (no plist)' if ok else 'INSTALL FAILED (%s)' % lines[-1]
        rc = 0 if ok else 1
    elif (age is None or age > k['watch']['stale_after_s']) and not tick_running(state_dir):
        code, _o, err = scheduler._launchctl(['kickstart', 'gui/%d/%s' % (scheduler._uid(), label)])
        msg = ('KICKED (no tick for %s)' % ('ever' if age is None else '%ds' % age) if code == 0
               else 'KICK FAILED (%s)' % (err.strip() or code))
        rc = 0 if code == 0 else 1
    tail = _tail(tick_log(product.name))
    last = next((x for x in reversed(tail) if x.startswith(TICK_LINE)), '')
    line = '%s %s plan_age=%s tracebacks_last%d=%d | %s' % (
        now.strftime('%Y-%m-%dT%H:%M:%SZ'), msg, '-' if age is None else '%ds' % age, TAIL,
        sum('Traceback' in x for x in tail), last)
    path = watch_log(product.name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    return rc, line
