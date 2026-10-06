"""asf.scheduler — the scheduler adapter: render a job definition, install it, read it back.

A factory that is installed but cannot run is worse than one that was never installed: the
clock fires, nothing happens, and the tables go quiet without a single error anywhere. That is
what a job definition with a bare ``python3``, no working directory and no ``PYTHONPATH``
produces — the interpreter the scheduler finds is not the one the operator tested with, and the
package is not importable from the scheduler's cwd. So the job definition is built here, from
the running interpreter and the installed package, and never written by hand:

* ``ProgramArguments`` starts with :data:`sys.executable` — an absolute interpreter, the one
  this process runs under.
* ``WorkingDirectory`` and ``PYTHONPATH`` are the package's repo root, derived from
  ``asf.__file__``.
* ``PATH`` is the current ``PATH``'s absolute entries (a relative entry means something
  different under the scheduler than it does in a shell), ``HOME`` is the current home.
* stdout and stderr go to ``<ASF_HOME>/logs/tick-<product>-<clock>.log``, so a job that fails
  leaves a trace the doctor can read back.
* When the package runs from a git checkout (a development checkout, the self-hosting
  product), the job never imports from it: it runs the launcher :mod:`asf.snapshot`, copied to
  ``<ASF_HOME>/state/<product>/code/launch.py`` at install, which ticks from an immutable
  worktree of the checkout's HEAD sha (``code/<sha>``). ``WorkingDirectory`` is that code dir
  and the launcher sets ``PYTHONPATH`` to the snapshot.
* When the product is **pinned** (``<ASF_HOME>/state/<product>/install.json`` names a venv),
  the job is rendered from *that* venv, not from the process running the install: its
  ``bin/python`` is the interpreter, its ``site-packages`` the working directory, there is **no**
  ``PYTHONPATH`` (it would sit before the venv's own ``site-packages`` and import the installing
  process's ``asf`` instead of the pinned one), and ``PATH`` drops every entry inside a checkout
  of asf itself — and nothing else (Homebrew's prefix is a git checkout too). A pinned product
  whose venv is not on disk refuses to render (:class:`SchedulerError` ``pinned venv missing``),
  and so does one whose rendered ``PATH`` does not resolve every tool its clocks run
  (:func:`required_tools`: ``git``, ``gh``, the session runtime, the lockfile's toolchain):
  ``install`` writes no plist for it and exits 2 with ``NEEDS OPERATOR``.

The ``kind`` comes from ``config.yaml``'s ``scheduler.kind`` (default ``launchd``). ``launchd``
is implemented end to end; ``cron`` renders the crontab line for an operator to install; any
other kind renders a ``NEEDS OPERATOR`` marker rather than guessing.

A clock is a job: ``products/<product>.yaml``'s ``clocks:`` block names each one, the steps it
ticks (or ``shadow: true``) and exactly one of ``every: <duration>`` or ``at: "HH:MM"``. Each
clock renders one job, labelled ``<label_prefix>.<product>.<clock-name>`` and logging to
``<ASF_HOME>/logs/tick-<product>-<clock-name>.log``. The step ``daily`` is the one special
name only to the tick, not here: a clock whose only step is ``daily`` renders ``--steps daily``
like any other (the daily stamp keeps it once a day); ``--daily`` alone would run every step.
An installed job still carrying the old ``--daily`` form reads back as the daily clock.

A product whose CI start queue is on also gets the queue's own job, never declared:
:data:`QUEUE_CLOCK` (``asf.<product>.ci-queue``), ``asf ci queue --apply`` every
:data:`QUEUE_EVERY_S` seconds (:func:`asf.ci_queue.apply`: the CI start queue's pass and the merge
queue's own, :func:`asf.merge_queue.own_pass`). ``install`` retires it once both queues are off.
"""
import fnmatch
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections import namedtuple

from asf import env
from asf import installs
from asf import snapshot

# this repo's own cutover script (relative to the ASF checkout) — named once, here, not per mention
CUTOVER_TOOL = os.path.join('tools', 'cutover.sh')

DEFAULT_LABEL_PREFIX = 'asf'
DAILY_STEP = 'daily'

CLOCK_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
AT_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
_CLOCK_KEYS = {'steps', 'shadow', 'every', 'at'}
MIN_EVERY_S = 60
_CRON_HOUR_DIVISORS = (1, 2, 3, 4, 6, 8, 12)

#: ``command``: None for a tick clock; :data:`QUEUE_COMMAND` for the CI queue's own job
Clock = namedtuple('Clock', 'name steps shadow interval_s calendar command', defaults=(None,))

#: the CI start queue's own job (:func:`asf.ci_queue.apply`): every product whose queue is on
#: (a ``ci.pool``, ``ci.queue.mode`` not ``off``) gets it beside its declared clocks — the pass
#: must not wait for a tick (7–24 min, none while an upgrade is pending) to start idle runners
QUEUE_CLOCK = 'ci-queue'
QUEUE_COMMAND = 'ci-queue'
QUEUE_EVERY_S = 60

#: the host-level probe clock (W1-PR3a): one per host, outside the ``asf.<product>.*`` glob, so
#: no product's install, retire or reload touches it; written only when ``network.probe`` is on
HOST_CLOCK = 'net-probe'
HOST_PRODUCT = 'host'


class SchedulerError(Exception):
    pass


# ---- the pieces a job definition is made of ---------------------------------


def repo_root():
    """The directory the ``asf`` package is installed under — the job's cwd and PYTHONPATH."""
    import asf
    return os.path.dirname(os.path.dirname(os.path.abspath(asf.__file__)))


def _sched(cfg):
    return (cfg or {}).get('scheduler') or {}


def kind(cfg=None):
    cfg = env.load_config() if cfg is None else cfg
    s = _sched(cfg)
    return s.get('kind') or s.get('provider') or 'launchd'


def label_prefix(cfg=None):
    cfg = env.load_config() if cfg is None else cfg
    return _sched(cfg).get('label_prefix') or DEFAULT_LABEL_PREFIX


def legacy_labels(cfg=None):
    cfg = env.load_config() if cfg is None else cfg
    labels = _sched(cfg).get('legacy_labels') or []
    return [labels] if isinstance(labels, str) else list(labels)


def steps_slug(steps):
    return '-'.join(steps)


def label_for(product_name, clock_name, cfg=None):
    return f'{label_prefix(cfg)}.{product_name}.{clock_name}'


def launch_agents_dir():
    return os.path.expanduser('~/Library/LaunchAgents')


def plist_path(label):
    return os.path.join(launch_agents_dir(), f'{label}.plist')


def log_path(product_name, clock_name):
    return os.path.join(env.ASF_HOME, 'logs', f'tick-{product_name}-{clock_name}.log')


def _checkout_root(directory):
    """The git work tree ``directory`` sits in (the nearest it or a parent holding ``.git``),
    else None."""
    d = os.path.abspath(directory)
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _in_asf_checkout(directory):
    """Whether ``directory`` sits inside a git checkout *of asf itself* — a work tree whose root
    carries the ``asf`` package (``asf/__init__.py``), the moving code a pinned clock must not
    reach. Any other git work tree on ``PATH`` is a tool's install, not asf's code: Homebrew's
    prefix is itself a git checkout (``/opt/homebrew/.git``), and dropping it took ``gh`` off a
    pinned product's clocks (2026-10-03: "lane: pass failed — No such file or directory: 'gh'")."""
    root = _checkout_root(directory)
    return root is not None and os.path.isfile(os.path.join(root, 'asf', '__init__.py'))


def _absolute_path_entries(drop_checkouts=False):
    """The current ``PATH``'s absolute entries, order preserved, duplicates dropped.

    A relative entry (``.``, ``bin``) resolves against the *job's* cwd under a scheduler, not
    the shell's — it would silently mean something else, so it is dropped rather than carried.
    With ``drop_checkouts`` (a pinned product) an entry inside a checkout of asf itself
    (:func:`_in_asf_checkout`) is dropped too: a pinned clock must not reach the moving checkout's
    scripts through ``PATH``. Every other entry is kept.
    """
    out, seen = [], set()
    for d in os.environ.get('PATH', '').split(os.pathsep):
        if d and os.path.isabs(d) and d not in seen:
            seen.add(d)
            if drop_checkouts and _in_asf_checkout(d):
                continue
            out.append(d)
    return os.pathsep.join(out)


# ---- the tools a clock runs -------------------------------------------------

#: Every clock shells out to these: the lane and retention call ``gh``, every step calls ``git``.
BASE_TOOLS = ('git', 'gh')

#: A lockfile in the product repo → the tools its gate and sessions run.
LOCKFILE_TOOLS = (
    ('pnpm-lock.yaml', ('node', 'pnpm')),
    ('package-lock.json', ('node', 'npm')),
    ('yarn.lock', ('node', 'yarn')),
    ('bun.lock', ('bun',)),
    ('bun.lockb', ('bun',)),
)


def required_tools(product, cfg=None):
    """The commands a product's clocks need on their ``PATH``: :data:`BASE_TOOLS`, the session
    runtime's binary (``worker_pool.binary``, default ``claude``; none for the ``fake`` backend),
    and the toolchain the product repo's lockfile names (:data:`LOCKFILE_TOOLS`)."""
    tools = list(BASE_TOOLS)
    pool = (cfg or {}).get('worker_pool') or {}
    if str(pool.get('backend') or 'claude-code') != 'fake':
        tools.append(str(pool.get('binary') or 'claude'))
    repo = getattr(product, 'repo_dir', None)
    if repo and os.path.isdir(repo):
        for lock, names in LOCKFILE_TOOLS:
            if os.path.exists(os.path.join(repo, lock)):
                tools.extend(names)
    out = []
    for t in tools:
        if t not in out:
            out.append(t)
    return out


def missing_tools(path, tools):
    """The ``tools`` that do not resolve to an executable on ``path`` (an absolute tool is
    checked as a file)."""
    missing = []
    for t in tools:
        if os.path.isabs(t):
            if not (os.path.isfile(t) and os.access(t, os.X_OK)):
                missing.append(t)
        elif shutil.which(t, path=path) is None:
            missing.append(t)
    return missing


def check_tools(product, path, cfg=None):
    """Raise :class:`SchedulerError` (``NEEDS OPERATOR``) naming every tool of
    :func:`required_tools` that does not resolve on the rendered ``path`` — a clock that cannot
    find ``gh`` stops the landing lane without a single error the tables show."""
    name = getattr(product, 'name', product)
    missing = missing_tools(path, required_tools(product, cfg))
    if missing:
        raise SchedulerError(
            f'clock PATH lacks {", ".join(missing)}: product {name}\'s clocks need them, and the '
            f'PATH they would run with does not resolve them ({path or "(empty)"}) — run the '
            f'install from a shell whose PATH finds them; no plist is written')


# ---- the product's pinned venv ----------------------------------------------


def pinned_venv(product_name):
    """The venv directory the product is pinned to (:func:`asf.installs.read`), or None for an
    unpinned product. A bare venv name is taken under pipx's venvs directory, as the
    dispatcher takes it."""
    inst = installs.read(product_name)
    if inst is None:
        return None
    venv = os.path.expanduser(inst.venv)
    if not os.path.isabs(venv):
        venv = os.path.join(installs.venvs_root(), venv)
    return os.path.abspath(venv)


def venv_site_packages(venv):
    """The venv's ``lib/python*/site-packages`` — the job's cwd — else the venv itself."""
    return installs.site_packages(venv) or venv


def check_venv(product_name, venv):
    """Raise :class:`SchedulerError` unless ``venv`` is on disk with a runnable interpreter."""
    python = installs.interpreter(venv)
    if not os.path.isdir(venv) or not os.access(python, os.X_OK):
        raise SchedulerError(
            f'pinned venv missing: product {product_name} is pinned to {venv} '
            f'({installs.record_path(product_name)}) but {python} is not on disk')


# ---- the snapshot the clock runs from ---------------------------------------


def snapshot_repo(root=None):
    """The checkout the clock must snapshot — the package's own root when it is a git working
    tree (a development checkout that moves under the clock) — else None: an installed package
    does not change while a tick imports it."""
    root = root or repo_root()
    if not snapshot.is_checkout(root):
        return None
    # run from a snapshot itself (a tick installing its own clock): snapshot the checkout it
    # was made from, or the clock would stay on this sha for ever
    return snapshot.source_checkout(root) or root


def code_dir(product_name):
    """``<ASF_HOME>/state/<product>/code``: the launcher and the snapshots."""
    return os.path.join(env.ASF_HOME, 'state', product_name, 'code')


def launcher_path(product_name):
    return os.path.join(code_dir(product_name), snapshot.LAUNCHER)


def write_launcher(product_name):
    """Copy :mod:`asf.snapshot` to :func:`launcher_path` (only when it differs) — the clock
    runs the copy, so not even the launcher is read from the moving checkout."""
    with open(snapshot.__file__, encoding='utf-8') as f:
        text = f.read()
    path = launcher_path(product_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return path
    except OSError:
        pass
    tmp = f'{path}.tmp-{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def clock_code(product_name):
    """``{snapshot, sha, at, head}`` for the doctor: whether the clock runs from a snapshot, the
    sha it last ran (``None`` before its first tick), when, and the checkout's HEAD now."""
    repo = snapshot_repo()
    if repo is None:
        return {'snapshot': False, 'root': repo_root()}
    sha, at = snapshot.current(code_dir(product_name))
    try:
        head = snapshot.head_sha(repo)
    except snapshot.SnapshotError:
        head = None
    return {'snapshot': True, 'root': repo, 'sha': sha, 'at': at, 'head': head}


def tick_argv(product_name, clock, interpreter=None):
    """``asf tick`` as the scheduler will run it — absolute interpreter (``interpreter``, else
    this process's), module form. The queue's clock runs ``asf ci queue --apply`` instead: no
    ``tick`` in it, so the upgrade drain neither skips nor counts it
    (:data:`asf.upgrade.TICK_PATTERN`)."""
    python = interpreter or sys.executable
    if clock.command == QUEUE_COMMAND:
        return [python, '-m', 'asf.cli', 'ci', 'queue', '--apply', '--product', product_name]
    argv = [python, '-m', 'asf.cli', 'tick', '--product', product_name]
    if clock.shadow:
        argv.append('--shadow')
    else:  # the daily clock too: a bare ``--daily`` ran every step (B: two ticks, one clone)
        argv += ['--steps', ','.join(clock.steps)]
    return argv


# ---- clocks -------------------------------------------------------------------


def _format_duration(seconds):
    """Seconds back into the largest whole unit a clock's ``every:`` could have been written in."""
    seconds = int(seconds)
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds and seconds % size == 0:
            return f'{seconds // size}{unit}'
    return f'{seconds}s'


def _parse_at(value):
    m = AT_RE.match(str(value).strip())
    return {'Hour': int(m.group(1)), 'Minute': int(m.group(2))}


def _clock_refusal(name, entry, product, tick_steps, stale):
    """The reason ``name: entry`` is not a usable clock, or ``None`` when it is fine."""
    if not CLOCK_NAME_RE.match(name):
        return 'name must match [a-z0-9][a-z0-9-]*'
    if not isinstance(entry, dict):
        return f'must be a map, not {entry!r}'
    unknown = sorted(set(entry) - _CLOCK_KEYS)
    if unknown:
        return f'unknown key {unknown[0]!r} (steps, shadow, every, at)'

    shadow = bool(entry.get('shadow'))
    raw_steps = entry.get('steps')
    if shadow and raw_steps is not None:
        return 'shadow and steps are mutually exclusive'
    if not shadow:
        step_list = raw_steps if isinstance(raw_steps, list) else ([raw_steps] if raw_steps else [])
        if not step_list:
            return 'steps is missing or empty'
        for step in step_list:
            if step not in tick_steps.STEPS:
                return f"step {step} is not a known step ({', '.join(tick_steps.STEPS)})"
        try:
            tick_steps.check_owned(tick_steps.resolve(product, step_list), product)
        except tick_steps.StepError as e:
            return str(e)

    every, at = entry.get('every'), entry.get('at')
    if (every is None) == (at is None):
        return 'exactly one of every or at is required'
    if every is not None:
        try:
            secs = stale.limit_seconds(every)
        except ValueError:
            return f'every {every!r} is not a valid duration (<n><s|m|h|d>)'
        if secs < MIN_EVERY_S:
            return f'every {every} is under the {MIN_EVERY_S}s minimum'
    else:
        if not AT_RE.match(str(at).strip()):
            return f'at {at!r} is not HH:MM, 24h'
    return None


def _build_clock(name, entry, stale):
    shadow = bool(entry.get('shadow'))
    raw_steps = entry.get('steps')
    steps_val = [] if shadow else (raw_steps if isinstance(raw_steps, list) else [raw_steps])
    every, at = entry.get('every'), entry.get('at')
    if every is not None:
        return Clock(name, steps_val, shadow, stale.limit_seconds(every), None)
    return Clock(name, steps_val, shadow, None, _parse_at(at))


def clocks(product):
    """``[Clock, ...]`` in file order, from ``products/<p>.yaml``'s ``clocks:`` block.

    Raises :class:`SchedulerError` naming every refused clock (spec D8, one ``clock <name>:
    <why>`` line each) — nothing is returned for a file with even one bad entry. A missing or
    empty ``clocks:`` is the D5 ``NEEDS OPERATOR`` refusal.
    """
    from asf.tick import stale, steps as tick_steps

    name = getattr(product, 'name', product)
    declared = product._get('clocks') if hasattr(product, '_get') else None
    if not declared:
        raise SchedulerError(
            f'NEEDS OPERATOR: products/{name}.yaml declares no clocks — add a clocks: block '
            f'(see docs/products.example.yaml)')

    errors, built = [], []
    for clock_name, entry in declared.items():
        why = _clock_refusal(clock_name, entry, product, tick_steps, stale)
        if why:
            errors.append(f'clock {clock_name}: {why}')
        else:
            built.append(_build_clock(clock_name, entry, stale))

    if errors:
        raise SchedulerError('\n'.join(errors))
    if QUEUE_CLOCK not in declared and hasattr(product, '_get'):
        from asf import ci_queue
        conv = getattr(product, 'conventions', None)
        if ci_queue.mode(product) != 'off' or (conv is not None and conv.merge_queue()):
            built.append(Clock(QUEUE_CLOCK, [], False, QUEUE_EVERY_S, None, QUEUE_COMMAND))
    return built


# ---- render -----------------------------------------------------------------


def _cron_schedule(clock):
    """The crontab line for ``clock``, or ``None`` when it can't be expressed (D9)."""
    if clock.calendar:
        return f"{clock.calendar['Minute']} {clock.calendar['Hour']} * * *"
    seconds = clock.interval_s
    if seconds % 60 == 0:
        minutes = seconds // 60
        if 1 <= minutes < 60:
            return f'*/{minutes} * * * *'
        if minutes % 60 == 0:
            hours = minutes // 60
            if hours in _CRON_HOUR_DIVISORS:
                return f'0 */{hours} * * *'
            if hours == 24:
                return '0 0 * * *'
    return None


def _clock_when(clock):
    if clock.calendar:
        return f"at {clock.calendar['Hour']:02d}:{clock.calendar['Minute']:02d}"
    return f'every {_format_duration(clock.interval_s)}'


_PIN = object()


def render(product, clock, cfg=None, venv=_PIN):
    """The job definition for one clock: ``{kind, label, path, plist|line, log, argv}``.

    ``product`` is a :class:`asf.env.Product` or a bare product name. ``clock`` is one
    :class:`Clock` from :func:`clocks`. ``venv`` is the venv the clock runs from; left out it
    is the product's pin (:func:`pinned_venv`), and ``None`` renders from this process (the
    unpinned path). A venv that is not on disk raises :class:`SchedulerError`.
    """
    cfg = env.load_config() if cfg is None else cfg
    name = getattr(product, 'name', product)
    if venv is _PIN:
        venv = pinned_venv(name)
    if venv is not None:
        venv = os.path.abspath(venv)
        check_venv(name, venv)

    label = label_for(name, clock.name, cfg)
    log = log_path(name, clock.name)
    job_kind = kind(cfg)
    if venv is not None:
        # the pinned venv's own interpreter finds its own ``asf``; a PYTHONPATH would put the
        # installing process's package in front of it, and the pin would be a label only
        argv = tick_argv(name, clock, interpreter=installs.interpreter(venv))
        root = venv_site_packages(venv)
        snap_repo = None
        env_vars = {'PATH': _absolute_path_entries(drop_checkouts=True),
                    'HOME': os.path.expanduser('~'), 'ASF_HOME': env.ASF_HOME}
        check_tools(product, env_vars['PATH'], cfg)
    else:
        argv = tick_argv(name, clock)
        root = repo_root()
        snap_repo = snapshot_repo(root)
        env_vars = {'PATH': _absolute_path_entries(), 'HOME': os.path.expanduser('~'),
                    'PYTHONPATH': root, 'ASF_HOME': env.ASF_HOME}
    launcher = None
    if snap_repo is not None:
        # the tick runs from a snapshot of the checkout's HEAD, never the checkout itself
        launcher = launcher_path(name)
        cwd = code_dir(name)
        argv = [sys.executable, launcher, '--repo', snap_repo, '--code-dir', cwd, '--'] + argv[1:]
        root = cwd
        del env_vars['PYTHONPATH']

    job = {'kind': job_kind, 'label': label, 'log': log, 'argv': argv, 'product': name,
           'clock': clock.name, 'steps': clock.steps}
    if venv is not None:
        job['venv'] = venv
    if launcher:
        job['launcher'] = launcher
    if clock.interval_s is not None:
        job['every_s'] = clock.interval_s
    else:
        job['at'] = clock.calendar

    if job_kind == 'launchd':
        plist = {
            'Label': label,
            'ProgramArguments': argv,
            'WorkingDirectory': root,
            'EnvironmentVariables': env_vars,
            'StandardOutPath': log,
            'StandardErrorPath': log,
        }
        if clock.calendar:
            # a timed clock fires only at its declared time — RunAtLoad would also run it the
            # moment install loads the job, hours outside that window (B-0115)
            plist['StartCalendarInterval'] = dict(clock.calendar)
        else:
            plist['RunAtLoad'] = True
            plist['StartInterval'] = int(clock.interval_s)
        job['plist'] = plist
        job['path'] = plist_path(label)
        return job

    if job_kind == 'cron':
        schedule = _cron_schedule(clock)
        if schedule is None:
            job['needs_operator'] = (
                f"NEEDS OPERATOR: clock {clock.name}: {_clock_when(clock)} cannot be expressed "
                f"as a cron schedule — install a job running `{' '.join(argv)}` "
                f"{_clock_when(clock)} and log it to {log}")
            return job
        envs = ' '.join(f'{k}={v}' for k, v in env_vars.items() if k != 'HOME')
        command = ' '.join(argv)
        job['line'] = f"{schedule} cd {root} && {envs} {command} >> {log} 2>&1"
        return job

    job['needs_operator'] = (
        f"NEEDS OPERATOR: scheduler kind {job_kind!r} has no adapter — install a job running "
        f"`{' '.join(argv)}` {_clock_when(clock)} and log it to {log}"
    )
    return job


def render_host(cfg=None):
    """The job definition of the host's network probe clock — ``asf.host.net-probe``, ``asf
    net-probe`` every 60 s — in :func:`render`'s shape, or None while ``config.yaml
    network.probe`` is not on. Log only: the job probes and writes ``state/network.json``.

    It runs the dispatcher (``~/.local/bin/asf``, :mod:`asf.dispatch`) when one this factory
    wrote is there: the dispatcher picks the default product's pin at run time, so a move or a
    rollback of that product needs no re-render (#695/#701 baked in the venv that installed the
    clock). No ``PYTHONPATH`` then — it would shadow the pinned venv's own package. Without a
    dispatcher it runs this process's interpreter, as before, and a move re-renders it
    (:func:`refresh_host`)."""
    from asf import dispatch
    from asf.tick import network
    cfg = env.load_config() if cfg is None else cfg
    if not network.enabled(cfg):
        return None
    label = label_for(HOST_PRODUCT, HOST_CLOCK, cfg)
    log = os.path.join(env.ASF_HOME, 'logs', f'{HOST_CLOCK}.log')
    dispatcher = dispatch.default_path()
    env_vars = {'PATH': _absolute_path_entries(drop_checkouts=True),
                'HOME': os.path.expanduser('~'), 'ASF_HOME': env.ASF_HOME}
    if dispatch.is_ours(dispatcher) and os.access(dispatcher, os.X_OK):
        argv, cwd = [dispatcher, 'net-probe'], env.ASF_HOME
    else:
        argv, cwd = [sys.executable, '-m', 'asf.cli', 'net-probe'], repo_root()
        env_vars = {'PATH': _absolute_path_entries(), 'HOME': os.path.expanduser('~'),
                    'PYTHONPATH': repo_root(), 'ASF_HOME': env.ASF_HOME}
    job = {'kind': kind(cfg), 'label': label, 'log': log, 'argv': argv, 'product': HOST_PRODUCT,
           'clock': HOST_CLOCK, 'steps': [], 'every_s': network.PROBE_EVERY_S}
    if job['kind'] == 'launchd':
        job['plist'] = {
            'Label': label, 'ProgramArguments': argv, 'WorkingDirectory': cwd,
            'EnvironmentVariables': env_vars,
            'StandardOutPath': log, 'StandardErrorPath': log,
            'RunAtLoad': True, 'StartInterval': network.PROBE_EVERY_S,
        }
        job['path'] = plist_path(label)
    else:
        job['needs_operator'] = (f"NEEDS OPERATOR: scheduler kind {job['kind']!r} has no adapter — "
                                 f"run `{' '.join(argv)}` every {network.PROBE_EVERY_S} s and log "
                                 f"it to {log}")
    return job


def host_label(cfg=None):
    return label_for(HOST_PRODUCT, HOST_CLOCK, cfg)


def uninstall_host(cfg=None):
    """Boot out and delete the host clock's plist and clear its pause — the inverse of
    :func:`install_host`. Nothing installed is one line, not a failure."""
    label = host_label(cfg)
    path = plist_path(label)
    paused = bool(read_pauses(HOST_PRODUCT))
    if not os.path.exists(path) and not paused and not status(label).get('loaded'):
        return [f'scheduler: {label} is not installed']
    lines = uninstall(label)
    if HOST_CLOCK in read_pauses(HOST_PRODUCT):
        pauses = read_pauses(HOST_PRODUCT)
        pauses.pop(HOST_CLOCK, None)
        _write_pauses(HOST_PRODUCT, pauses)
    return lines


def install_host(cfg=None):
    """Render and install the host clocks (``asf scheduler install --host``). Idempotent: the
    plist is rewritten and the label booted out and in again, so a second run leaves the same
    state. While ``network.probe`` is off the clock is removed instead — the flag going off
    takes the job with it."""
    cfg = env.load_config() if cfg is None else cfg
    job = render_host(cfg)
    if job is None:
        if os.path.exists(plist_path(host_label(cfg))) or status(host_label(cfg)).get('loaded'):
            return uninstall_host(cfg)
        return ['scheduler: network.probe is off (config.yaml) — no host clock to install']
    return install(job)


def refresh_host(cfg=None):
    """:func:`install_host` when the host clock on disk is not what a render writes now — after
    a move or rollback (:func:`asf.upgrade._host_clocks`). ``[]`` while ``network.probe`` is off
    (the move never installs nor removes it) or the plist is already current."""
    cfg = env.load_config() if cfg is None else cfg
    job = render_host(cfg)
    if job is None or job.get('kind') != 'launchd':
        return []
    try:
        with open(job['path'], 'rb') as f:
            if plistlib.load(f) == job['plist']:
                return []
    except (OSError, plistlib.InvalidFileException, ValueError):
        pass
    return install_host(cfg)


# ---- the smoke a move runs before it resumes the clocks ----------------------

#: Run by the plist's own interpreter: imports the CLI it ticks and loads the product file with
#: that venv's loader — read-only, any sha.
_SMOKE_IMPORT = ('import sys, asf.cli\nfrom asf import env\nenv.load_product(sys.argv[1])\n'
                 'print(asf.__file__)')

SMOKE_TIMEOUT_S = 60


def _tool_probe(tool):
    if os.path.basename(tool) == 'gh':
        return [tool, 'auth', 'status', '--active']
    return [tool, '--version']


def noop_command(argv):
    """The clock's own command in a form that changes nothing, else ``None``: ``asf tick …``
    with ``--manifest`` (argv parsed, product loaded, ``--steps`` resolved, the table printed,
    nothing run; ``--shadow`` dropped, as it would run step 0 first), ``asf ci queue …`` with
    ``--help`` in place of ``--apply``. Any interpreter or launcher before ``asf.cli`` is kept."""
    argv = [str(a) for a in argv]
    if 'asf.cli' not in argv:
        return None
    at = argv.index('asf.cli') + 1
    head, rest = argv[:at], argv[at:]
    if rest[:1] == ['tick']:
        return head + [a for a in rest if a != '--shadow'] + ['--manifest']
    if rest[:2] == ['ci', 'queue']:
        return head + [a for a in rest if a != '--apply'] + ['--help']
    return None


def smoke(product, cfg=None, run=subprocess.run, timeout=SMOKE_TIMEOUT_S):
    """Run read-only commands under each installed clock plist's *exact* environment, working
    directory and interpreter — what the clock will run, not what this shell has: the plist's
    interpreter imports ``asf.cli`` and loads the product file, ``gh auth status --active``, and
    ``<tool> --version`` for every other tool of :func:`required_tools`, each resolved on the
    plist's ``PATH`` — once per interpreter/env/cwd — and then every clock's *own* command in its
    no-op form (:func:`noop_command`), so a tick broken by its own argv rolls the move back too
    (2026-10-04: the daily and the tick shared one interpreter, and only the daily was smoked). Returns ``(ok_lines, failures)``; a failure names the clock and the command.
    Only launchd plists are read (a cron line has no plist to replay); none on disk is a
    failure — a move that leaves no clock has nothing ticking."""
    cfg = env.load_config() if cfg is None else cfg
    name = getattr(product, 'name', product)
    if kind(cfg) != 'launchd':
        return [f'smoke: scheduler kind {kind(cfg)} — no plist to replay'], []
    paths = sorted(p for p in (plist_path(label) for label in product_labels(name, cfg))
                   if os.path.isfile(p))
    if not paths:
        return [], [f'no clock plist of {name} on disk']
    ok, failures, seen = [], [], set()
    tools = required_tools(product, cfg)
    for path in paths:
        try:
            with open(path, 'rb') as f:
                plist = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError) as e:
            failures.append(f'{os.path.basename(path)}: unreadable ({e})')
            continue
        argv = plist.get('ProgramArguments') or []
        job_env = {str(k): str(v) for k, v in (plist.get('EnvironmentVariables') or {}).items()}
        cwd = plist.get('WorkingDirectory') or '/'
        key = (tuple(argv[:1]), tuple(sorted(job_env.items())), cwd)
        if not argv:
            continue
        label = plist.get('Label') or os.path.basename(path)
        commands = []
        if key not in seen:
            seen.add(key)
            commands.append([argv[0], '-c', _SMOKE_IMPORT, name])
            for tool in tools:
                found = shutil.which(tool, path=job_env.get('PATH', ''))
                if found is None:
                    failures.append(f'{label}: {tool} not on the plist PATH')
                    continue
                commands.append(_tool_probe(found))
        own = noop_command(argv)
        if own is not None:
            commands.append(own)
        for cmd in commands:
            shown = ' '.join(cmd[:1] + ['-c', '<import asf.cli; load product>'] + cmd[3:]) \
                if cmd[1:2] == ['-c'] else ' '.join(cmd)
            try:
                p = run(cmd, env=job_env, cwd=cwd if os.path.isdir(cwd) else '/',
                        capture_output=True, text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as e:
                failures.append(f'{label}: {shown} did not run ({e})')
                continue
            if p.returncode != 0:
                tail = ((p.stderr or p.stdout or '').strip().splitlines() or [''])[-1]
                failures.append(f'{label}: {shown} exit {p.returncode} {tail}'.rstrip())
            else:
                ok.append(f'{label}: {shown} ok')
    return ok, failures


def render_plist(job):
    """The launchd job definition as the XML that goes on disk."""
    if job.get('kind') != 'launchd':
        raise SchedulerError(f"render_plist: not a launchd job ({job.get('kind')!r})")
    return plistlib.dumps(job['plist']).decode('utf-8')


# ---- launchctl --------------------------------------------------------------


def _uid():
    return os.getuid()


def _launchctl(args, timeout=30):
    try:
        p = subprocess.run(['launchctl'] + list(args), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or ''), (p.stderr or '')
    except FileNotFoundError:
        return 127, '', 'launchctl not found'
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def install(job):
    """Write the job definition and load it. Returns a list of the lines to print.

    launchd: writes ``~/Library/LaunchAgents/<label>.plist``, ``bootout``s any job already
    holding the label (a failure there is the ordinary case — it was not loaded) and
    ``bootstrap``s the new one. cron and the unadapted kinds only print; nothing on this
    machine can install them for the operator.
    """
    job_kind = job.get('kind')
    if job.get('launcher'):
        write_launcher(job['product'])
    if job_kind == 'cron':
        return [f"cron: add this line to the operator's crontab (`crontab -e`):", job['line'],
                f"NEEDS OPERATOR: install the cron line above — crontab -e"]
    if job_kind != 'launchd':
        return [job.get('needs_operator', f"NEEDS OPERATOR: scheduler kind {job_kind!r}")]

    label, path = job['label'], job['path']
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.dirname(job['log']), exist_ok=True)
    with open(path, 'wb') as f:
        plistlib.dump(job['plist'], f)
    lines = [f'scheduler: wrote {path}']
    record = pause_record(label)
    if record is not None:
        lines.append(f'scheduler: {label} {pause_text(record)} — not loaded; '
                     f'{resume_hint(label)} to start it')
        return lines
    _launchctl(['bootout', f'gui/{_uid()}/{label}'])  # not loaded yet is the ordinary case
    rc, _out, err = _launchctl(['bootstrap', f'gui/{_uid()}', path])
    if rc == 0:
        lines.append(f'scheduler: bootstrapped {label}')
    else:
        lines.append(f'scheduler: bootstrap {label} failed ({err.strip() or rc})')
    return lines


def uninstall(label, remove_plist=True):
    """``bootout`` the label and (by default) delete its plist — the inverse of :func:`install`."""
    lines = []
    rc, _out, err = _launchctl(['bootout', f'gui/{_uid()}/{label}'])
    lines.append(f'scheduler: booted out {label}' if rc == 0
                 else f'scheduler: bootout {label} failed ({err.strip() or rc})')
    path = plist_path(label)
    if remove_plist and os.path.exists(path):
        os.remove(path)
        lines.append(f'scheduler: removed {path}')
    return lines


def bootstrap(path, force=False):
    """Load an existing plist by path — what a rollback does to a job cutover booted out. A
    paused clock (:func:`pause_record`) is never loaded here: it returns ``(False, 'paused since
    …')``. Only :func:`resume` passes ``force``, after it has cleared the pause."""
    label = os.path.basename(path)[:-len('.plist')] if path.endswith('.plist') else ''
    record = None if force else pause_record(label)
    if record is not None:
        return False, pause_text(record)
    rc, _out, err = _launchctl(['bootstrap', f'gui/{_uid()}', path])
    return rc == 0, (err.strip() if rc else '')


# ---- the durable pause ------------------------------------------------------
#
# Booting a clock out by hand leaves no trace: the next install, upgrade or doctor repair sees a
# plist that launchd does not hold and loads it again, restarting a factory the operator stopped.
# ``asf scheduler pause`` records the pause (reason, who, when) under the product's state dir
# before it boots the clock out, and every path that loads a clock asks :func:`pause_record`
# first. ``asf scheduler resume`` clears the record and loads the clock again.

PAUSE_FILE = 'paused-clocks.json'


def pause_path(product_name):
    return os.path.join(env.ASF_HOME, 'state', product_name, PAUSE_FILE)


def read_pauses(product_name):
    """``{clock: {'reason', 'by', 'at'}}`` — the product's paused clocks; ``{}`` when none."""
    import json
    try:
        with open(pause_path(product_name), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) \
        else {}


def _write_pauses(product_name, pauses):
    import json
    path = pause_path(product_name)
    if not pauses:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(pauses, f, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def split_label(label, cfg=None):
    """``(product, clock)`` for ``<prefix>.<product>.<clock>``, else ``None``."""
    try:
        prefix = label_prefix(cfg)
    except env.ConfigError:
        prefix = DEFAULT_LABEL_PREFIX
    if not label or not label.startswith(prefix + '.'):
        return None
    product_name, _, clock = label[len(prefix) + 1:].rpartition('.')
    return (product_name, clock) if product_name and clock else None


def pause_record(label, cfg=None):
    """The pause record holding ``label`` unloaded, or ``None`` when it is not paused."""
    parts = split_label(label, cfg)
    if parts is None:
        return None
    record = read_pauses(parts[0]).get(parts[1])
    return dict(record, label=label) if record is not None else None


def pause_text(record):
    return (f"paused since {record.get('at') or '?'} ({record.get('reason') or 'no reason'}; "
            f"by {record.get('by') or '?'})")


def product_labels(product_name, cfg=None):
    """Every clock label of ``product_name`` with a plist in LaunchAgents, sorted."""
    import glob
    pattern = os.path.join(launch_agents_dir(), f'{label_prefix(cfg)}.{product_name}.*.plist')
    return sorted(os.path.basename(p)[:-len('.plist')] for p in glob.glob(pattern))


def resume_hint(label, cfg=None):
    parts = split_label(label, cfg)
    if parts is None:
        return '`asf scheduler resume`'
    return f'`asf scheduler resume --product {parts[0]} --clock {parts[1]}`'


def pause(product_name, clock_names, reason, by, cfg=None, now=None, bootout=True, pid=None):
    """Record the pause of each clock, then boot it out. The record is written first, so an
    upgrade or install racing this call finds it before it could load the clock again.

    ``bootout=False`` writes the record only: ``launchctl bootout`` kills the job's running
    process, so a move records the pause, drains the running ticks, and only then calls
    :func:`bootout_clocks` (2026-10-04: a move's pause killed a tick mid-wave).

    ``pid``: the pausing process, recorded so a pause it never lifted — killed before its
    resume — is found stale once that process is gone (:func:`asf.upgrade.lift_stale_pauses`)."""
    import datetime
    at = (now or datetime.datetime.now()).astimezone().isoformat(timespec='seconds')
    pauses = read_pauses(product_name)
    for clock in clock_names:
        pauses[clock] = {'reason': reason, 'by': by, 'at': at}
        if pid is not None:
            pauses[clock]['pid'] = pid
    _write_pauses(product_name, pauses)
    if not bootout:
        return [f'scheduler: paused {label_for(product_name, clock, cfg)} (recorded; still '
                f'loaded until its running process ends) — {reason}' for clock in clock_names]
    return [f'scheduler: paused {label} ({state}) — {reason}'
            for label, state in _bootout_each(product_name, clock_names, cfg)]


def _bootout_each(product_name, clock_names, cfg=None):
    for clock in clock_names:
        label = label_for(product_name, clock, cfg)
        rc, _out, _err = _launchctl(['bootout', f'gui/{_uid()}/{label}'])
        yield label, ('booted out' if rc == 0 else 'was not loaded')


def bootout_clocks(product_name, clock_names, cfg=None):
    """``launchctl bootout`` each clock — which kills a running process of it, so a caller
    that must not kill one drains first (:func:`asf.upgrade._quiesced_switch`)."""
    return [f'scheduler: {label} {state}'
            for label, state in _bootout_each(product_name, clock_names, cfg)]


def resume(product_name, clock_names, cfg=None):
    """Clear each clock's pause and load its plist again (one already loaded is left alone)."""
    pauses = read_pauses(product_name)
    for clock in clock_names:
        pauses.pop(clock, None)
    _write_pauses(product_name, pauses)
    lines = []
    for clock in clock_names:
        label = label_for(product_name, clock, cfg)
        path = plist_path(label)
        if not os.path.exists(path):
            lines.append(f'scheduler: resumed {label} — no plist at {path}; '
                         f'`asf scheduler install --product {product_name}` writes it')
            continue
        if status(label).get('loaded'):
            lines.append(f'scheduler: resumed {label} (already loaded)')
            continue
        ok, err = bootstrap(path, force=True)
        lines.append(f'scheduler: resumed {label}' if ok
                     else f'scheduler: resume {label}: bootstrap failed ({err})')
    return lines


_STATE_RE = re.compile(r'^\s*state\s*=\s*(.+?)\s*$')
_RUNS_RE = re.compile(r'^\s*runs\s*=\s*(\d+)\s*$')
_EXIT_RE = re.compile(r'^\s*last exit code\s*=\s*(.+?)\s*$')
_PROGRAM_RE = re.compile(r'^\s*program\s*=\s*(.+?)\s*$')
_PATH_RE = re.compile(r'^\s*path\s*=\s*(.+?)\s*$')


def parse_print(text):
    """``launchctl print gui/<uid>/<label>`` → ``{state, runs, last_exit, program, path}``.

    ``last_exit`` is ``None`` when launchd reports ``(never exited)`` — a job that has been
    loaded for several intervals and still never exited is a job that never ran, which is
    exactly the failure this module exists to make visible.
    """
    out = {'state': None, 'runs': None, 'last_exit': None, 'program': None, 'path': None,
           'never_exited': False}
    for line in text.splitlines():
        if out['state'] is None:
            m = _STATE_RE.match(line)
            if m:
                out['state'] = m.group(1)
                continue
        if out['runs'] is None:
            m = _RUNS_RE.match(line)
            if m:
                out['runs'] = int(m.group(1))
                continue
        if out['last_exit'] is None and not out['never_exited']:
            m = _EXIT_RE.match(line)
            if m:
                raw = m.group(1)
                if re.fullmatch(r'-?\d+', raw):
                    out['last_exit'] = int(raw)
                else:
                    out['never_exited'] = True
                continue
        if out['program'] is None:
            m = _PROGRAM_RE.match(line)
            if m:
                out['program'] = m.group(1)
                continue
        if out['path'] is None:
            m = _PATH_RE.match(line)
            if m:
                out['path'] = m.group(1)
    return out


def status(label):
    """The loaded job's state, or ``{'loaded': False}`` when launchd does not know the label."""
    rc, out, err = _launchctl(['print', f'gui/{_uid()}/{label}'])
    if rc != 0:
        return {'label': label, 'loaded': False, 'detail': (err or out).strip().splitlines()[:1]}
    parsed = parse_print(out)
    parsed.update({'label': label, 'loaded': True})
    return parsed


def parse_list(text):
    """``launchctl list`` → the labels in its third column."""
    labels = []
    for line in text.splitlines():
        parts = line.rstrip('\n').split('\t')
        if len(parts) < 3:
            continue
        label = parts[2].strip()
        if not label or label == 'Label':
            continue
        labels.append(label)
    return labels


def job_paths(plist_data):
    """Every absolute path a job definition points at — its arguments and its cwd.

    This is what "still in use" means for a legacy directory: a loaded job whose argv (or
    working directory) sits under it stops working the moment the directory moves.
    """
    paths, seen = [], set()
    for value in list(plist_data.get('ProgramArguments') or []) + [
            plist_data.get('WorkingDirectory')]:
        if not value:
            continue
        candidate = os.path.expanduser(str(value))
        if not candidate.startswith('/'):
            continue
        candidate = os.path.normpath(candidate)
        if candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _read_plist(path):
    try:
        with open(path, 'rb') as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _plist_for(label):
    path = plist_path(label)
    if os.path.exists(path):
        return path
    rc, out, _err = _launchctl(['print', f'gui/{_uid()}/{label}'])
    if rc != 0:
        return None
    found = parse_print(out).get('path')
    return found if found and os.path.exists(found) else None


def matches(label, patterns, prefix):
    if prefix and label.startswith(prefix + '.'):
        return True
    return any(fnmatch.fnmatch(label, p) for p in patterns)


def loaded_jobs(pattern=None, cfg=None):
    """The loaded factory jobs: ours (by label prefix) plus ``scheduler.legacy_labels``.

    Each is ``{label, plist, paths, argv}``; ``paths`` is what the job's arguments reference,
    read back off the plist on disk rather than guessed from the label.
    """
    cfg = env.load_config() if cfg is None else cfg
    patterns = [pattern] if pattern else legacy_labels(cfg)
    prefix = None if pattern else label_prefix(cfg)
    rc, out, _err = _launchctl(['list'])
    if rc != 0:
        return []
    jobs = []
    for label in parse_list(out):
        if not matches(label, patterns, prefix):
            continue
        path = _plist_for(label)
        data = _read_plist(path) if path else None
        jobs.append({
            'label': label,
            'plist': path,
            'argv': list((data or {}).get('ProgramArguments') or []),
            'paths': job_paths(data or {}),
        })
    return jobs


def jobs_using(directory, jobs):
    """[(label, path)] for every job whose paths sit at or under ``directory``."""
    target = os.path.normpath(os.path.expanduser(directory))
    hits = []
    for job in jobs:
        for path in job['paths']:
            if path == target or path.startswith(target + os.sep):
                hits.append((job['label'], path))
    return hits


def retire_candidates(product_name, declared_labels, cfg=None):
    """The loaded labels under this product's prefix that are not among ``declared_labels``.

    The trailing dot in ``<prefix>.<product>.`` keeps another product's jobs
    (``<prefix>.other.*``) and a ``legacy_labels`` match out — only this product's own jobs are
    ever retired by a whole-file install (D4).
    """
    cfg = env.load_config() if cfg is None else cfg
    prefix = f'{label_prefix(cfg)}.{product_name}.'
    declared = set(declared_labels)
    return [job['label'] for job in loaded_jobs(cfg=cfg)
            if job['label'].startswith(prefix) and job['label'] not in declared]


# ---- the installed job read back as the clock it was rendered from -----------------------
#
# A job installed before its clock lived in the product file (T-0043) left the two disagreeing:
# ``status`` said "declares no clocks" over a job firing every ten minutes. The file stays the
# source: status names every installed job no clock declares, with the entry that would, and
# ``install`` adopts those entries into the file before it renders — so what it installs and
# what the file says are the same clocks.


def clock_of_plist(label, data, product_name, cfg=None):
    """The :class:`Clock` a job definition was rendered from, or None when it is not one of
    :func:`render`'s (no ``tick``, no interval, a name no clock can carry)."""
    prefix = f'{label_prefix(cfg)}.{product_name}.'
    name = label[len(prefix):] if label.startswith(prefix) else ''
    argv = list((data or {}).get('ProgramArguments') or [])
    every = (data or {}).get('StartInterval')
    if (name == QUEUE_CLOCK and argv[-5:-2] == ['ci', 'queue', '--apply']
            and isinstance(every, int) and every > 0):
        return Clock(name, [], False, every, None, QUEUE_COMMAND)
    if not CLOCK_NAME_RE.match(name) or 'tick' not in argv:
        return None
    rest = argv[argv.index('tick') + 1:]
    shadow = '--shadow' in rest
    if shadow:
        steps = []
    elif '--daily' in rest:
        steps = [DAILY_STEP]
    elif '--steps' in rest and rest.index('--steps') + 1 < len(rest):
        steps = [x for x in rest[rest.index('--steps') + 1].split(',') if x]
    else:
        return None
    cal, every = data.get('StartCalendarInterval'), data.get('StartInterval')
    if isinstance(cal, dict) and 'Hour' in cal and 'Minute' in cal:
        return Clock(name, steps, shadow, None, {'Hour': int(cal['Hour']),
                                                'Minute': int(cal['Minute'])})
    if isinstance(every, int) and every > 0:
        return Clock(name, steps, shadow, every, None)
    return None


def installed_clocks(product_name, cfg=None):
    """``[(label, Clock | None)]`` for every loaded job under ``<prefix>.<product>.``."""
    cfg = env.load_config() if cfg is None else cfg
    prefix = f'{label_prefix(cfg)}.{product_name}.'
    out = []
    for job in loaded_jobs(cfg=cfg):
        if job['label'].startswith(prefix):
            data = _read_plist(job['plist']) if job['plist'] else None
            out.append((job['label'], clock_of_plist(job['label'], data, product_name, cfg)))
    return out


def clocks_yaml(clock_list):
    """A ``clocks:`` block declaring ``clock_list`` — the product file's own syntax."""
    lines = ['clocks:']
    for c in clock_list:
        lines.append(f'  {c.name}:')
        lines.append('    shadow: true' if c.shadow else f"    steps: [{', '.join(c.steps)}]")
        lines.append(f"    at: \"{c.calendar['Hour']:02d}:{c.calendar['Minute']:02d}\""
                     if c.calendar else f'    every: {_format_duration(c.interval_s)}')
    return '\n'.join(lines) + '\n'


def _undeclared(product_name, declared_names, cfg):
    """The installed jobs of this product no declared clock renders: ``[(label, Clock|None)]``."""
    declared = {label_for(product_name, n, cfg) for n in declared_names}
    return [(label, c) for label, c in installed_clocks(product_name, cfg)
            if label not in declared]


def _undeclared_lines(product_name, undeclared):
    lines = [f'scheduler: {label} is installed but not a clock in products/{product_name}.yaml'
             + ('' if c else ' (not a job this adapter renders)') for label, c in undeclared]
    # the queue's job is never declared: `install` retires it once the queue is off
    adoptable = [c for _label, c in undeclared if c and not c.command]
    if adoptable:
        lines.append(f'scheduler: declare it in products/{product_name}.yaml '
                     f'(`asf scheduler install` does):')
        lines.append(clocks_yaml(adoptable).rstrip('\n'))
    return lines


def adopt(product_name, undeclared):
    """Append the ``clocks:`` block for the installed jobs to a product file that has none —
    appended, so every byte above it stays as the operator wrote it. Returns the lines to print."""
    adoptable = [(label, c) for label, c in undeclared if c and not c.command]
    if not adoptable:
        return []
    path = env.product_path(product_name)
    with open(path, encoding='utf-8') as f:
        text = f.read()
    with open(path, 'a', encoding='utf-8') as f:
        f.write(('' if not text or text.endswith('\n') else '\n')
                + clocks_yaml([c for _label, c in adoptable]))
    return [f'scheduler: declared clock {c.name} in products/{product_name}.yaml from the '
            f'installed {label}' for label, c in adoptable]


# ---- the CLI ----------------------------------------------------------------


def register(sub):
    """``asf scheduler render|install|status|list`` — wired into ``asf.cli``'s subparsers."""
    p = sub.add_parser('scheduler', help='the factory clock: render, install and read back jobs')
    p.add_argument('scheduler_command',
                   choices=['render', 'install', 'status', 'list', 'pause', 'resume',
                            'uninstall'])
    p.add_argument('--product')
    p.add_argument('--host', action='store_true',
                   help='the host-level clocks (asf.host.net-probe, while config.yaml '
                        'network.probe is on) instead of a product\'s')
    p.add_argument('--clock', help='the clock name from products/<p>.yaml (default: every clock); '
                                   'pause/resume take a comma-separated list')
    p.add_argument('--reason', help='pause: why the clocks are stopped (recorded, required)')
    p.add_argument('--by', help='pause: who paused them (default: $USER)')
    p.add_argument('--label', help='status: read this label directly instead of a declared clock')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    return p


def _status_line(info):
    if not info.get('loaded'):
        record = pause_record(info['label'])
        if record is not None:
            return f"{info['label']}  PAUSED — {pause_text(record)}"
        return f"{info['label']}  not loaded"
    exit_text = 'never exited' if info.get('never_exited') else info.get('last_exit')
    return (f"{info['label']}  state={info.get('state')}  runs={info.get('runs')}  "
            f"last-exit={exit_text}  program={info.get('program')}")


def _cmd_pause_resume(args, command, product_name, cfg):
    if kind(cfg) != 'launchd':
        print(f'scheduler: {command} needs scheduler kind launchd, not {kind(cfg)!r}')
        return 2
    names = [c.strip() for c in (args.clock or '').split(',') if c.strip()]
    bad = [c for c in names if not CLOCK_NAME_RE.match(c)]
    if bad:
        print(f'scheduler: not a clock name: {", ".join(bad)}')
        return 2
    prefix = f'{label_prefix(cfg)}.{product_name}.'
    if command == 'pause':
        reason = (getattr(args, 'reason', None) or '').strip()
        if not reason:
            print('scheduler: pause needs --reason "<why>" — it is recorded with the pause')
            return 2
        names = names or [label[len(prefix):] for label in product_labels(product_name, cfg)]
        if not names:
            print(f'scheduler: no clock of {product_name} to pause')
            return 2
        by = getattr(args, 'by', None) or os.environ.get('USER') or '?'
        lines = pause(product_name, names, reason, by, cfg=cfg)
    else:
        if not names:
            names = sorted(set(read_pauses(product_name)) | {
                label[len(prefix):] for label in product_labels(product_name, cfg)})
        lines = resume(product_name, names, cfg=cfg)
    for line in lines:
        print(line)
    return 1 if any('failed' in line for line in lines) else 0


def _cmd_host(args, command, cfg):
    if kind(cfg) != 'launchd':
        print(f'scheduler: --host needs scheduler kind launchd, not {kind(cfg)!r}')
        return 2
    if command in ('pause', 'resume'):
        args.product, args.clock = HOST_PRODUCT, HOST_CLOCK
        return _cmd_pause_resume(args, command, HOST_PRODUCT, cfg)
    if command == 'install':
        lines = install_host(cfg)
    elif command == 'uninstall':
        lines = uninstall_host(cfg)
    elif command == 'status':
        info = status(host_label(cfg))
        info['label'] = host_label(cfg)
        lines = [_status_line(info)]
        if not info.get('loaded') and pause_record(host_label(cfg)) is None:
            print(lines[0])
            return 1
    else:
        job = render_host(cfg)
        if job is None:
            lines = ['scheduler: network.probe is off (config.yaml) — no host clock']
        else:
            lines = [f"# {job['label']}", render_plist(job).rstrip('\n')] \
                if job['kind'] == 'launchd' else [job['needs_operator']]
    for line in lines:
        print(line)
    # a bootout of a clock launchd never held is the ordinary case, not a failure
    return 1 if any('failed' in line and 'bootout' not in line for line in lines) else 0


def cmd_scheduler(args, root=None):
    import json as _json
    cfg = env.load_config()
    command = args.scheduler_command

    if command == 'list':
        jobs = loaded_jobs(cfg=cfg)
        if args.json:
            print(_json.dumps(jobs, indent=2, sort_keys=True))
            return 0
        if not jobs:
            print('scheduler: no factory jobs loaded')
            return 0
        for job in jobs:
            print(f"{job['label']}  {job['plist'] or '(no plist found)'}")
            for path in job['paths']:
                print(f"    {path}")
        return 0

    if command == 'uninstall' and not getattr(args, 'host', False):
        print('scheduler: uninstall only takes --host (a product clock is retired by install)')
        return 2
    if getattr(args, 'host', False):
        return _cmd_host(args, command, cfg)

    product_name = args.product or env.default_product_name()

    if command == 'status':
        # a move killed before it resumed the clocks it paused: its pause outlives it, and the
        # factory sits idle until someone looks — the look lifts it
        from asf import upgrade
        for line in upgrade.lift_stale_pauses(product_name):
            print(line)

    if command in ('pause', 'resume'):
        return _cmd_pause_resume(args, command, product_name, cfg)

    if command == 'status' and args.label:
        info = status(args.label)
        info['label'] = args.label
        if args.json:
            print(_json.dumps(info, indent=2, sort_keys=True, default=str))
            return 0
        print(_status_line(info))
        return 0 if info.get('loaded') else 1

    product = env.load_product(product_name)
    undeclared = []
    if not product._get('clocks') and kind(cfg) == 'launchd':
        undeclared = _undeclared(product_name, (), cfg)
        if undeclared and command == 'status':
            infos = [dict(status(label), label=label) for label, _c in undeclared]
            if args.json:
                print(_json.dumps(infos, indent=2, sort_keys=True, default=str))
            else:
                for line in [_status_line(i) for i in infos] + _undeclared_lines(product_name,
                                                                               undeclared):
                    print(line)
            return 1
        if undeclared and command == 'install':
            for line in adopt(product_name, undeclared):
                print(line)
            product = env.load_product(product_name)
    try:
        clock_list = clocks(product)
    except SchedulerError as e:
        print(str(e))
        for line in _undeclared_lines(product_name, undeclared):
            print(line)
        return 2

    if args.clock:
        selected = [c for c in clock_list if c.name == args.clock]
        if not selected:
            names = ', '.join(c.name for c in clock_list)
            print(f'scheduler: no clock {args.clock} in products/{product_name}.yaml '
                  f'(clocks: {names})')
            return 2
    else:
        selected = clock_list

    try:
        jobs = [render(product, c, cfg=cfg) for c in selected] \
            if command in ('render', 'install') else []
    except SchedulerError as e:
        # every clock renders before any is written: a refused render leaves the plists as
        # they are, never half of them moved
        print(f'NEEDS OPERATOR: {e}')
        return 2

    if command == 'render':
        if args.json:
            print(_json.dumps(jobs, indent=2, sort_keys=True, default=str))
            return 0
        for job in jobs:
            print(f"# {job['label']}")
            if job['kind'] == 'launchd':
                print(render_plist(job), end='')
            elif job['kind'] == 'cron':
                print(job['line'])
            else:
                print(job['needs_operator'])
        return 0

    if command == 'status':
        infos = []
        for c in selected:
            info = status(label_for(product_name, c.name, cfg))
            info['clock'] = c.name
            infos.append(info)
        extra = [] if args.clock or kind(cfg) != 'launchd' else \
            _undeclared(product_name, [c.name for c in clock_list], cfg)
        if args.json:
            print(_json.dumps(infos[0] if args.clock else infos, indent=2, sort_keys=True,
                              default=str))
        else:
            for info in infos:
                print(_status_line(info))
            for line in _undeclared_lines(product_name, extra):
                print(line)
        return 1 if extra or any(not i.get('loaded') and pause_record(
            label_for(product_name, i['clock'], cfg)) is None for i in infos) else 0

    # install
    job_kind = kind(cfg)
    lines = []
    for job in jobs:
        lines.extend(install(job))
    if not args.clock and job_kind == 'launchd':
        declared_labels = [label_for(product_name, c.name, cfg) for c in clock_list]
        for label in retire_candidates(product_name, declared_labels, cfg):
            lines.extend(uninstall(label))
            lines.append(f'scheduler: retired {label} (not a clock in '
                         f'products/{product_name}.yaml)')
    for line in lines:
        print(line)
    return 0 if job_kind == 'launchd' else 3


def main(argv=None):
    """``python3 -m asf.scheduler <command>`` — the same surface as ``asf scheduler``.

    ``asf.cli`` wires :func:`register` into its subparsers and dispatches here; this entry
    point is the same code reached without going through the top-level parser, which is what
    the cutover script (:data:`CUTOVER_TOOL`) falls back to when it is run against a checkout
    whose ``cli.py`` does not have the subcommand wired yet.
    """
    import argparse
    parser = argparse.ArgumentParser(prog='asf scheduler')
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(['scheduler'] + list(argv if argv is not None else sys.argv[1:]))
    return cmd_scheduler(args)


if __name__ == '__main__':
    sys.exit(main())
