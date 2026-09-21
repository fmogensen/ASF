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
* stdout and stderr go to ``<ASF_HOME>/logs/tick-<product>-<steps>.log``, so a job that fails
  leaves a trace the doctor can read back.

The ``kind`` comes from ``config.yaml``'s ``scheduler.kind`` (default ``launchd``). ``launchd``
is implemented end to end; ``cron`` renders the crontab line for an operator to install; any
other kind renders a ``NEEDS OPERATOR`` marker rather than guessing.

Steps name what a job ticks: ``render(product, ['record'], 600)`` is the record clock,
``render(product, ['health', 'wave', 'prs', 'batch'], 600)`` the dispatch clock. The step
``daily`` is the one special name — it renders ``asf tick --daily`` rather than ``--steps``.
"""
import fnmatch
import os
import plistlib
import re
import subprocess
import sys

from asf import env

# this repo's own cutover script (relative to the ASF checkout) — named once, here, not per mention
CUTOVER_TOOL = os.path.join('tools', 'cutover.sh')

DEFAULT_INTERVAL_S = 600
DEFAULT_LABEL_PREFIX = 'asf'
DAILY_STEP = 'daily'
DAILY_CALENDAR = {'Hour': 6, 'Minute': 50}


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


def interval_s(cfg=None):
    cfg = env.load_config() if cfg is None else cfg
    value = _sched(cfg).get('interval_s')
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


def steps_slug(steps):
    return '-'.join(steps)


def label_for(product_name, steps, cfg=None):
    return f'{label_prefix(cfg)}.{product_name}.{steps_slug(steps)}'


def launch_agents_dir():
    return os.path.expanduser('~/Library/LaunchAgents')


def plist_path(label):
    return os.path.join(launch_agents_dir(), f'{label}.plist')


def log_path(product_name, steps):
    return os.path.join(env.ASF_HOME, 'logs', f'tick-{product_name}-{steps_slug(steps)}.log')


def _absolute_path_entries():
    """The current ``PATH``'s absolute entries, order preserved, duplicates dropped.

    A relative entry (``.``, ``bin``) resolves against the *job's* cwd under a scheduler, not
    the shell's — it would silently mean something else, so it is dropped rather than carried.
    """
    out, seen = [], set()
    for d in os.environ.get('PATH', '').split(os.pathsep):
        if d and os.path.isabs(d) and d not in seen:
            seen.add(d)
            out.append(d)
    return os.pathsep.join(out)


def tick_argv(product_name, steps):
    """``asf tick`` as the scheduler will run it — absolute interpreter, module form."""
    argv = [sys.executable, '-m', 'asf.cli', 'tick', '--product', product_name]
    if list(steps) == [DAILY_STEP]:
        argv.append('--daily')
    else:
        argv += ['--steps', ','.join(steps)]
    return argv


# ---- render -----------------------------------------------------------------


def render(product, steps, interval=None, cfg=None, calendar=None):
    """The job definition for one clock: ``{kind, label, path, plist|line, log, argv}``.

    ``product`` is a :class:`asf.env.Product` or a bare product name. ``steps`` is the step
    list this clock ticks; ``interval`` its period in seconds. Pass ``calendar``
    (``{'Hour': 6, 'Minute': 50}``) for a wall-clock job instead of an interval — the ``daily``
    step defaults to :data:`DAILY_CALENDAR`.
    """
    cfg = env.load_config() if cfg is None else cfg
    name = getattr(product, 'name', product)
    steps = list(steps)
    if not steps:
        raise SchedulerError('render: no steps given')
    if interval is None:
        interval = interval_s(cfg)
    if calendar is None and steps == [DAILY_STEP]:
        calendar = dict(DAILY_CALENDAR)

    label = label_for(name, steps, cfg)
    argv = tick_argv(name, steps)
    log = log_path(name, steps)
    root = repo_root()
    job_kind = kind(cfg)

    job = {'kind': job_kind, 'label': label, 'log': log, 'argv': argv, 'product': name,
           'steps': steps}

    if job_kind == 'launchd':
        plist = {
            'Label': label,
            'ProgramArguments': argv,
            'WorkingDirectory': root,
            'EnvironmentVariables': {
                'PATH': _absolute_path_entries(),
                'HOME': os.path.expanduser('~'),
                'PYTHONPATH': root,
                'ASF_HOME': env.ASF_HOME,
            },
            'StandardOutPath': log,
            'StandardErrorPath': log,
            'RunAtLoad': True,
        }
        if calendar:
            plist['StartCalendarInterval'] = dict(calendar)
        else:
            plist['StartInterval'] = int(interval)
        job['plist'] = plist
        job['path'] = plist_path(label)
        return job

    if job_kind == 'cron':
        if calendar:
            schedule = f"{calendar.get('Minute', 0)} {calendar.get('Hour', 0)} * * *"
        else:
            minutes = max(1, int(interval) // 60)
            schedule = f'*/{minutes} * * * *'
        envs = f"PATH={_absolute_path_entries()} PYTHONPATH={root} ASF_HOME={env.ASF_HOME}"
        command = ' '.join(argv)
        job['line'] = f"{schedule} cd {root} && {envs} {command} >> {log} 2>&1"
        return job

    job['needs_operator'] = (
        f"NEEDS OPERATOR: scheduler kind {job_kind!r} has no adapter — install a job running "
        f"`{' '.join(argv)}` every {int(interval)}s and log it to {log}"
    )
    return job


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


def bootstrap(path):
    """Load an existing plist by path — what a rollback does to a job cutover booted out."""
    rc, _out, err = _launchctl(['bootstrap', f'gui/{_uid()}', path])
    return rc == 0, (err.strip() if rc else '')


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


# ---- the CLI ----------------------------------------------------------------


def _steps_arg(value):
    return [s.strip() for s in (value or '').split(',') if s.strip()]


def register(sub):
    """``asf scheduler render|install|status|list`` — wired into ``asf.cli``'s subparsers."""
    p = sub.add_parser('scheduler', help='the factory clock: render, install and read back jobs')
    p.add_argument('scheduler_command', choices=['render', 'install', 'status', 'list'])
    p.add_argument('--product')
    p.add_argument('--steps', default='record',
                   help="comma-separated tick steps this job runs (default 'record'; "
                        "'daily' renders `asf tick --daily`)")
    p.add_argument('--interval', type=int, help='seconds between runs (default scheduler.interval_s)')
    p.add_argument('--label', help='status: the label to read (default: the one --steps renders)')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    return p


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

    product_name = args.product or env.default_product_name()
    steps = _steps_arg(args.steps)
    if not steps:
        print('scheduler: --steps is empty', file=sys.stderr)
        return 2

    if command == 'status':
        label = args.label or label_for(product_name, steps, cfg)
        info = status(label)
        if args.json:
            print(_json.dumps(info, indent=2, sort_keys=True, default=str))
            return 0
        if not info.get('loaded'):
            print(f'{label}  not loaded')
            return 1
        exit_text = 'never exited' if info.get('never_exited') else info.get('last_exit')
        print(f"{label}  state={info.get('state')}  runs={info.get('runs')}  "
              f"last-exit={exit_text}  program={info.get('program')}")
        return 0

    job = render(env.load_product(product_name), steps, args.interval, cfg=cfg)
    if command == 'render':
        if args.json:
            print(_json.dumps(job, indent=2, sort_keys=True, default=str))
        elif job['kind'] == 'launchd':
            print(render_plist(job), end='')
        elif job['kind'] == 'cron':
            print(job['line'])
        else:
            print(job['needs_operator'])
        return 0

    for line in install(job):
        print(line)
    return 0 if job['kind'] == 'launchd' else 3


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
