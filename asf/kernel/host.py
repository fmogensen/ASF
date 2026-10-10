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

The shadow preflight (:func:`preflight`, ``kernel.install.shadow``, default on): before a job is
switched to another venv, the NEW venv's ``asf kernel tick --product P --dry-run`` runs on the
live facts (read-only: no lock, no card write, no push, no launch, no record commit, no fetch).
A crash or a non-zero exit refuses the switch and the old plists stay. Its plan is then compared
with the live one — the old venv's own dry run, else the last tick's ``kernel-plan.json`` — and a
short diff printed: actions by type and items by state, old -> new, and every item whose state
differs. Launch dropping to 0 while items are Ready, any other action type dropping from
:data:`DROP_MIN` or more to 0, or more items changing state than
``kernel.install.max_state_changes`` (default 25) refuses the switch unless ``--accept-diff``.

A reinstall never cuts a running tick mid-apply (:func:`tick_lock`): once the preflight passed,
install waits up to ``kernel.install.lock_timeout_s`` (default 600) for the product's kernel lock
and holds it while launchd boots the old jobs out and the new ones in; a tick still running at
the deadline refuses the switch and the old plists stay.

Install keeps ASF's own footprint clean (:func:`prune_venvs`): of this venv's family (the pipx
venvs named like it but for the trailing short sha, ``asf-factory-asf-kernel-<sha>``) it keeps
this one and the :data:`KEEP_PREVIOUS` newest others for a rollback, and any venv a LaunchAgent,
a product's ``install.json`` pin or the ``~/.local/bin/asf`` dispatcher still names; the rest
are removed with their dangling ``~/.local/bin`` links.

``asf kernel watch`` (:func:`watch`) is the keep-alive that replaced a hand-written shell script:
the tick job not loaded -> load it (install it when its plist is gone); the last plan older than
``kernel.watch.stale_after_s`` and no tick running (the product's kernel lock is free) ->
``launchctl kickstart`` it. A clock paused through ``asf scheduler pause`` is left paused. One
line per run goes to ``logs/kernel-watch-<p>.log``. Each pass also runs the worktree reaper
(:func:`asf.workers.worktrees.reap`): a worktree under ``state/<p>/worktrees`` whose session is
over and whose work is on origin or the trunk is removed — the ended sessions the tick could not
free, and orphans no session names (anything holding unpushed work is kept).
"""
import contextlib
import datetime
import fcntl
import os
import json
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

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


#: the venvs of this one's family kept for a rollback beside the running one
KEEP_PREVIOUS = 2

#: a pipx venv name ending in a short sha: the family is everything before it
_FAMILY_RE = re.compile(r'^(?P<family>.+-)[0-9a-f]{7,40}$')


def _referenced(text_paths, venvs_dir):
    """The venv names under ``venvs_dir`` any of ``text_paths`` names."""
    names = set()
    pattern = re.compile(re.escape(venvs_dir.rstrip('/') + '/') + r'([^/"\'<\s]+)')
    for path in text_paths:
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                names.update(pattern.findall(f.read()))
        except OSError:
            continue
    return names


def _pins():
    """Every file that may name a venv still in use: the LaunchAgents, each product's
    ``install.json`` pin and the ``~/.local/bin/asf`` dispatcher."""
    from asf import env
    home = os.path.expanduser('~')
    out = [os.path.join(home, '.local', 'bin', 'asf')]
    for d, suffix in ((os.path.join(home, 'Library', 'LaunchAgents'), '.plist'),):
        try:
            out += [os.path.join(d, n) for n in os.listdir(d) if n.endswith(suffix)]
        except OSError:
            pass
    state = os.path.join(env.ASF_HOME, 'state')
    try:
        out += [os.path.join(state, n, 'install.json') for n in os.listdir(state)]
    except OSError:
        pass
    return out


def prune_venvs(python=None, keep=KEEP_PREVIOUS, bin_dir=None, pins=None, out=print):
    """Remove the old venvs of ``python``'s family (see the module doc) and the ``bin_dir``
    links into them (``~/.local/bin``); returns the removed venv names. A ``python`` outside a
    family venv prunes nothing."""
    venv = os.path.dirname(os.path.dirname(python or sys.executable))
    venvs_dir, name = os.path.dirname(venv), os.path.basename(venv)
    m = _FAMILY_RE.match(name)
    if not m or not os.path.isdir(venvs_dir):
        return []
    family = m.group('family')
    siblings = [n for n in os.listdir(venvs_dir) if n != name and n.startswith(family)
                and _FAMILY_RE.match(n) and _FAMILY_RE.match(n).group('family') == family
                and os.path.isdir(os.path.join(venvs_dir, n))]
    siblings.sort(key=lambda n: os.path.getmtime(os.path.join(venvs_dir, n)), reverse=True)
    held = _referenced(_pins() if pins is None else pins, venvs_dir)
    doomed = [n for n in siblings[keep:] if n not in held]
    removed = []
    for n in doomed:
        try:
            shutil.rmtree(os.path.join(venvs_dir, n))
        except OSError as e:
            out('kernel: venv %s kept: %s' % (n, e))
            continue
        removed.append(n)
    bin_dir = bin_dir or os.path.join(os.path.expanduser('~'), '.local', 'bin')
    gone = tuple(os.path.join(venvs_dir, n) + os.sep for n in removed)
    try:
        links = os.listdir(bin_dir) if gone else []
    except OSError:
        links = []
    for link in links:
        path = os.path.join(bin_dir, link)
        if os.path.islink(path) and os.readlink(path).startswith(gone) and not os.path.exists(path):
            os.unlink(path)
    if removed:
        out('kernel: pruned %d old venv(s) of %s* (kept %s + %d previous)'
            % (len(removed), family, name, keep))
    return removed


def _on_disk(path):
    try:
        with open(path, 'rb') as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


#: a shadow dry run past this many seconds counts as a crash
SHADOW_TIMEOUT_S = 900

#: an action type (other than Launch) the old plan has this many of, dropping to 0, refuses
DROP_MIN = 5

#: the changed items the diff names (the rest are counted)
DIFF_ITEMS = 20

DRY_LINE, ACTIONS_LINE = 'kernel tick (dry run): ', 'actions: '


def run_dry(python, product_name, plan_out=None, timeout=SHADOW_TIMEOUT_S):
    """``(exit code, output)`` of ``<python> -m asf.cli kernel tick --product P --dry-run``
    (``--plan-out`` when given) under this ``ASF_HOME``; a spawn failure or a timeout is 127/124."""
    from asf import env
    argv = [python, '-m', 'asf.cli', 'kernel', 'tick', '--product', product_name, '--dry-run']
    if plan_out:
        argv += ['--plan-out', plan_out]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           cwd=env.ASF_HOME if os.path.isdir(env.ASF_HOME) else None,
                           env=dict(os.environ, ASF_HOME=env.ASF_HOME))
    except subprocess.TimeoutExpired:
        return 124, 'timed out after %ds' % timeout
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def _pairs(text):
    """``{name: n}`` of ``'Ready 3, Stuck 1'`` (``none``/``no items``: ``{}``)."""
    out = {}
    for part in text.split(','):
        name, _, n = part.strip().rpartition(' ')
        if name and n.isdigit():
            out[name] = int(n)
    return out


def parse_dry(text):
    """A dry run's printed summary as a plan: ``{'counts', 'actions', 'states': None}``."""
    plan = {'counts': None, 'actions': None, 'states': None}
    for line in text.splitlines():
        if line.startswith(DRY_LINE):
            plan['counts'] = _pairs(line[len(DRY_LINE):])
        elif line.startswith(ACTIONS_LINE) and plan['actions'] is None:
            plan['actions'] = _pairs(line[len(ACTIONS_LINE):])
    return plan


def shadow_plan(python, product_name, run=None):
    """``(exit code, output, plan)`` of ``python``'s dry run: the plan from its ``--plan-out``
    file, else (a venv older than that flag) parsed from what it printed."""
    run = run or run_dry
    tmp = tempfile.mkdtemp(prefix='asf-shadow-')
    try:
        path = os.path.join(tmp, 'plan.json')
        rc, text = run(python, product_name, path)
        if rc == 2 and '--plan-out' in text:
            rc, text = run(python, product_name, None)
        try:
            with open(path, encoding='utf-8') as f:
                plan = json.load(f)
        except (OSError, ValueError):
            plan = parse_dry(text)
    finally:
        shutil.rmtree(tmp, True)
    return rc, text, plan


def live_plan(product_name):
    """The last tick's plan (``state/<p>/kernel-plan.json``): states and counts, no actions."""
    from asf import env
    from asf.kernel.loop import PLAN_FILE
    try:
        with open(os.path.join(env.ASF_HOME, 'state', product_name, PLAN_FILE),
                  encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    states = {iid: str((v or {}).get('state')) for iid, v in (data.get('states') or {}).items()}
    counts = {}
    for st in states.values():
        counts[st] = counts.get(st, 0) + 1
    return {'states': states, 'counts': counts, 'actions': None}


def _row(old, new):
    keys = sorted(set(old or {}) | set(new or {}))
    return ', '.join('%s %s -> %d' % (k, '?' if old is None else (old or {}).get(k, 0),
                                       (new or {}).get(k, 0)) for k in keys) or 'none'


def compare(old, new, max_changes):
    """``(diff lines, refusal reasons)`` of the live plan ``old`` (None: none known) against the
    new venv's ``new`` (see the module doc)."""
    old = old or {}
    lines = ['shadow: actions (old -> new): %s' % _row(old.get('actions'), new.get('actions')),
             'shadow: states  (old -> new): %s' % _row(old.get('counts'), new.get('counts'))]
    reasons = []
    o_states, n_states = old.get('states'), new.get('states')
    changed = []
    if o_states is not None and n_states is not None:
        changed = [(iid, o_states.get(iid, '-'), n_states.get(iid, '-'))
                   for iid in sorted(set(o_states) | set(n_states))
                   if o_states.get(iid) != n_states.get(iid)]
        lines.append('shadow: %d item(s) change state' % len(changed))
        lines += ['  %s: %s -> %s' % c for c in changed[:DIFF_ITEMS]]
        if len(changed) > DIFF_ITEMS:
            lines.append('  ... and %d more' % (len(changed) - DIFF_ITEMS))
    if len(changed) > max_changes:
        reasons.append('%d items change state (kernel.install.max_state_changes %d)'
                       % (len(changed), max_changes))
    o_act, n_act = old.get('actions'), new.get('actions') or {}
    ready = (new.get('counts') or {}).get('ready', 0)
    if o_act is not None:
        if o_act.get('Launch', 0) > 0 and not n_act.get('Launch') and ready > 0:
            reasons.append('Launch drops %d -> 0 while %d item(s) are Ready'
                           % (o_act['Launch'], ready))
        for kind, n in sorted(o_act.items()):
            if kind != 'Launch' and n >= DROP_MIN and not n_act.get(kind):
                reasons.append('%s drops %d -> 0' % (kind, n))
    return lines, reasons


def _switches(job):
    """The venv the job's plist on disk runs, when this install moves it to another; '' for a
    first install; None when the job stays on its venv."""
    old = _on_disk(job['path'])
    if old is None:
        return ''
    argv = old.get('ProgramArguments') or ['']
    return None if argv[0] == job['argv'][0] else argv[0]


def preflight(product, pending, out=print, accept_diff=False, run=None):
    """The shadow check (see the module doc) before ``pending`` jobs switch venv: True to go on."""
    k = product.kernel['install']
    moves = [(j, _switches(j)) for j in pending]
    moves = [(j, old) for j, old in moves if old is not None]
    if not moves or not k['shadow']:
        return True
    new_py, old_py = moves[0][0]['argv'][0], moves[0][1]
    out('shadow: dry-running %s on live facts' % new_py)
    rc, text, new = shadow_plan(new_py, product.name, run)
    if rc != 0:
        out('shadow: the new venv\'s dry run failed (exit %d) — switch refused, old plists kept'
            % rc)
        for line in text.strip().splitlines()[-15:]:
            out('  ' + line)
        return False
    old = None
    if old_py and os.path.exists(old_py):
        orc, _otext, old = shadow_plan(old_py, product.name, run)
        if orc != 0:
            out('shadow: the old venv\'s dry run failed (exit %d); comparing with the last tick'
                % orc)
            old = None
    live = live_plan(product.name)
    if old is None:
        old = live
    elif old.get('states') is None and live:
        old = dict(old, states=live['states'])
    lines, reasons = compare(old, new, int(k['max_state_changes']))
    for line in lines:
        out(line)
    if not reasons:
        return True
    for r in reasons:
        out('shadow: %s' % r)
    if accept_diff:
        out('shadow: --accept-diff given — switching')
        return True
    out('shadow: switch refused, old plists kept (rerun with --accept-diff to take this diff)')
    return False


def install(product, dry_run=False, cfg=None, python=None, out=print, accept_diff=False,
            run=None):
    """Write and load both jobs (see the module doc). Returns 0, or 1 when a load failed or the
    shadow preflight refused the switch. ``run`` replaces :func:`run_dry` (tests)."""
    from asf import scheduler
    rc = 0
    pending = []
    for job in jobs(product, cfg, python):
        if dry_run:
            out('# %s -> %s' % (job['label'], job['path']))
            out(scheduler.render_plist(job))
            continue
        if _on_disk(job['path']) == job['plist'] and scheduler._launchd_status(job['label'])['loaded']:
            out('kernel: %s unchanged (%s)' % (job['label'], job['argv'][0]))
            continue
        pending.append(job)
    if pending and not preflight(product, pending, out, accept_diff, run):
        return 1
    if pending:
        from asf import env
        state_dir = os.path.join(env.ASF_HOME, 'state', product.name)
        timeout = product.kernel['install']['lock_timeout_s']
        with tick_lock(state_dir, timeout, out) as held:
            if not held:
                out('kernel: install refused — a tick still running after %ds '
                    '(kernel.install.lock_timeout_s); the old plists stay' % timeout)
                return 1
            for job in pending:
                for line in scheduler.install(job):
                    out(line)
                    rc = 1 if 'failed' in line else rc
    if not dry_run:
        try:
            prune_venvs(python, out=out)
        except OSError as e:  # the jobs are installed; a prune failure is one line
            out('kernel: venv prune failed — %s' % e)
    return rc


#: seconds between two tries for the kernel lock while :func:`install` waits for a tick
LOCK_POLL_S = 2.0


@contextlib.contextmanager
def tick_lock(state_dir, timeout, out=print, clock=time.monotonic, sleep=time.sleep):
    """Hold the product's kernel lock (:data:`asf.kernel.loop.LOCK_FILE`) for the ``with`` block,
    waiting up to ``timeout`` seconds for a running tick to release it: a reinstall's launchd
    ``bootout`` would otherwise kill a tick mid-apply (cards half written, a launch without its
    record). Yields True once held, False when the tick still held it at the deadline. A tick
    the new job starts meanwhile finds the lock taken and skips its turn."""
    from asf.kernel.loop import LOCK_FILE
    os.makedirs(state_dir, exist_ok=True)
    fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline, said = clock() + max(0, timeout), False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if clock() >= deadline:
                    yield False
                    return
                if not said:
                    out('kernel: waiting for the running tick to end (up to %ds)' % timeout)
                    said = True
                sleep(LOCK_POLL_S)
        yield True
    finally:
        os.close(fd)


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


def reap_worktrees(product):
    """One worktree reaper pass (see the module doc): ``worktrees_reaped=N`` when any went,
    ``worktrees_reap_failed`` when the pass failed, else ''."""
    from asf.workers import spawn, worktrees
    try:
        repo = getattr(product, 'repo_dir', '') or ''
        if not (os.path.exists(os.path.join(repo, '.git'))
                and os.path.isdir(spawn.worktrees_dir(product))):
            return ''
        got = worktrees.reap(product, out=lambda _line: None)
    except Exception:  # noqa: BLE001 — the keep-alive never stops on a reap
        return 'worktrees_reap_failed'
    return 'worktrees_reaped=%d' % len(got['removed']) if got['removed'] else ''


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
    reaped = reap_worktrees(product)
    tail = _tail(tick_log(product.name))
    last = next((x for x in reversed(tail) if x.startswith(TICK_LINE)), '')
    line = '%s %s plan_age=%s%s tracebacks_last%d=%d | %s' % (
        now.strftime('%Y-%m-%dT%H:%M:%SZ'), msg, '-' if age is None else '%ds' % age,
        ' %s' % reaped if reaped else '', TAIL, sum('Traceback' in x for x in tail), last)
    path = watch_log(product.name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    return rc, line
