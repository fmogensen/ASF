"""asf.ci_vm — ``ci.provider: vm``: a product's own machines and commands as its CI, instead of
the PR host's Actions runs.

The config half: ``ci.hosts`` (ssh targets, their labels, slots and root), ``ci.jobs`` (named
commands, which are required, their timeout and labels) and the problems a product file can
state in either block.

The transport and the pass: one ssh connection and one ``git push`` move a sha onto a machine
and run it in a disposable ``git worktree`` (:func:`ssh_argv`, :func:`run_ssh`,
:func:`push_sha`); the results land in :data:`STORE_FILE`, keyed sha → job → row
(:func:`load`/:func:`save`); :func:`vm_pass` is the one entry point that dispatches, collects,
supersedes and times out, under its own lock, with every remote write behind the dry-run guard
(:mod:`asf.mutation_guard`). No runner registration, no start queue, no workflow and no
artifact store: ``<root>`` (``ci.hosts[].root``, default :data:`DEFAULT_ROOT`, relative to the
ssh user's home) is the only place on a host this provider ever writes, and the run directory is
removed at conclusion — nothing to prune.
"""
import contextlib
import dataclasses
import fcntl
import json
import os
import shlex
import subprocess
import time

from asf import env, mutation_guard

PROVIDER = 'vm'                 #: the ``ci.provider`` value this module answers for
HOST_FIELDS = ('name', 'ssh', 'labels', 'slots', 'root')
HOST_REQUIRED = ('name', 'ssh')
JOB_FIELDS = ('command', 'required', 'timeout_min', 'labels')
JOB_REQUIRED = ('command',)
DEFAULT_ROOT = '.asf-ci'        #: relative to the ssh user's home — never an absolute path here
DEFAULT_SLOTS = 1
DEFAULT_TIMEOUT_MIN = 45
CONNECT_TIMEOUT_S = 15
RED_LOG_TAIL_BYTES = 262144     #: how much of a log is read off the remote before trimming (C18)

STORE_FILE = 'ci-vm.json'       #: ``<state_dir>/ci-vm.json`` — the results store (§3)
LOCK_FILE = 'ci-vm.lock'        #: the pass's own lock (C13)
RUNNING, PASSED, FAILED, TIMEOUT, CANCELLED = 'running', 'passed', 'failed', 'timeout', 'cancelled'
#: a concluding state the trunk walk must keep remembering even past a green retry (PD4/B-0129)
RED_STATES = (FAILED, TIMEOUT, CANCELLED)
#: state → the ``bucket`` a check dict carries (P2), so nothing downstream learns these names
BUCKETS = {RUNNING: 'pending', PASSED: 'pass', FAILED: 'fail', TIMEOUT: 'fail',
          CANCELLED: 'cancel'}
CANCEL_GRACE_S = 1               #: the pause between ``TERM`` and ``KILL`` in :data:`REMOTE_CANCEL`
GREEN_TRUNK_LIMIT = 20           #: :func:`trunk_shas`' depth (PD3) — matches today's ``gh run list``


@dataclasses.dataclass(frozen=True)
class VmHost:
    """One ``ci.hosts`` entry: an ssh target with labels, slots and a root directory."""
    name: str
    ssh: str
    labels: frozenset = frozenset()
    slots: int = DEFAULT_SLOTS
    root: str = DEFAULT_ROOT


@dataclasses.dataclass(frozen=True)
class VmJob:
    """One ``ci.jobs`` entry: a named command, whether it is required, and its bounds."""
    name: str
    command: str
    required: bool = False
    timeout_min: int = DEFAULT_TIMEOUT_MIN
    labels: frozenset = frozenset()


def _ci_of(product):
    ci = getattr(product, 'ci', None)
    return ci if isinstance(ci, dict) else {}


def enabled(product):
    """True when this product's ``ci.provider`` is ``vm``."""
    ci = _ci_of(product)
    return str(ci.get('provider') or '').strip().lower() == PROVIDER


def _hosts_from(ci):
    raw = ci.get('hosts') if isinstance(ci, dict) else None
    out = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get('name')
        if not isinstance(name, str) or not name:
            continue
        ssh = entry.get('ssh')
        labels = entry.get('labels')
        labels = frozenset(str(l) for l in labels) if isinstance(labels, list) else frozenset()
        slots = entry.get('slots')
        slots = slots if isinstance(slots, int) and not isinstance(slots, bool) and slots > 0 \
            else DEFAULT_SLOTS
        root = entry.get('root')
        root = str(root) if isinstance(root, str) and root else DEFAULT_ROOT
        out.append(VmHost(name=name, ssh=str(ssh) if isinstance(ssh, str) else '',
                          labels=labels, slots=slots, root=root))
    return out


def _jobs_from(ci):
    raw = ci.get('jobs') if isinstance(ci, dict) else None
    out = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get('command')
        timeout_min = entry.get('timeout_min')
        timeout_min = timeout_min if isinstance(timeout_min, int) and not isinstance(timeout_min, bool) \
            and timeout_min > 0 else DEFAULT_TIMEOUT_MIN
        labels = entry.get('labels')
        labels = frozenset(str(l) for l in labels) if isinstance(labels, list) else frozenset()
        out[str(name)] = VmJob(name=str(name), command=str(command) if isinstance(command, str) else '',
                               required=bool(entry.get('required', False)),
                               timeout_min=timeout_min, labels=labels)
    return out


def hosts(product):
    """[:class:`VmHost`], declaration order; [] when there are none."""
    return _hosts_from(_ci_of(product))


def jobs(product):
    """{name: :class:`VmJob`}, declaration order preserved; {} when there are none."""
    return _jobs_from(_ci_of(product))


def config_problems(ci):
    """[(dotted key, problem)] for ``ci.hosts``/``ci.jobs``: an unknown field, a missing one
    required, a duplicate host name, a non-positive ``slots``/``timeout_min``, a ``ci.jobs``
    label no declared host can satisfy, and — when ``provider: vm`` — no hosts or no jobs at
    all. The label check is satisfiability: ``job.labels <= host.labels`` for at least one
    host, and a job with no labels is satisfied by any host."""
    if not isinstance(ci, dict):
        return []
    out = []
    raw_hosts = ci.get('hosts')
    if raw_hosts is not None and not isinstance(raw_hosts, list):
        out.append(('ci.hosts', f'must be a list of hosts, not {raw_hosts!r}'))
    elif isinstance(raw_hosts, list):
        seen = set()
        for i, entry in enumerate(raw_hosts):
            key = f'ci.hosts[{i}]'
            if not isinstance(entry, dict):
                out.append((key, f'must be a map {{{", ".join(HOST_FIELDS)}}}, not {entry!r}'))
                continue
            for k in entry:
                if k not in HOST_FIELDS:
                    out.append((f'{key}.{k}',
                               f"is not a field of a ci.hosts entry ({', '.join(HOST_FIELDS)})"))
            for k in HOST_REQUIRED:
                if entry.get(k) in (None, ''):
                    out.append((f'{key}.{k}', 'is required'))
            slots = entry.get('slots')
            if slots is not None and (isinstance(slots, bool) or not isinstance(slots, int) or slots <= 0):
                out.append((f'{key}.slots', f'must be a whole number > 0, not {slots!r}'))
            name = entry.get('name')
            if isinstance(name, str) and name:
                if name in seen:
                    out.append((f'{key}.name', f'{name!r} is declared twice'))
                seen.add(name)

    raw_jobs = ci.get('jobs')
    if raw_jobs is not None and not isinstance(raw_jobs, dict):
        out.append(('ci.jobs', f'must be a map of jobs, not {raw_jobs!r}'))
    elif isinstance(raw_jobs, dict):
        host_labels = [h.labels for h in _hosts_from(ci)]
        for name, entry in raw_jobs.items():
            key = f'ci.jobs.{name}'
            if not isinstance(entry, dict):
                out.append((key, f'must be a map {{{", ".join(JOB_FIELDS)}}}, not {entry!r}'))
                continue
            for k in entry:
                if k not in JOB_FIELDS:
                    out.append((f'{key}.{k}',
                               f"is not a field of a ci.jobs entry ({', '.join(JOB_FIELDS)})"))
            for k in JOB_REQUIRED:
                if entry.get(k) in (None, ''):
                    out.append((f'{key}.{k}', 'is required'))
            timeout_min = entry.get('timeout_min')
            if timeout_min is not None and (isinstance(timeout_min, bool)
                                            or not isinstance(timeout_min, int) or timeout_min <= 0):
                out.append((f'{key}.timeout_min', f'must be a whole number > 0, not {timeout_min!r}'))
            labels = entry.get('labels')
            if labels is not None:
                if not (isinstance(labels, list) and all(isinstance(l, str) for l in labels)):
                    out.append((f'{key}.labels', f'must be a list of labels, not {labels!r}'))
                elif host_labels and not any(set(labels) <= hl for hl in host_labels):
                    out.append((f'{key}.labels', f'{labels!r} is satisfied by no declared ci.hosts entry'))

    if str(ci.get('provider') or '').strip().lower() == PROVIDER:
        if not raw_hosts:
            out.append(('ci.hosts', 'ci.provider: vm needs at least one host'))
        if not raw_jobs:
            out.append(('ci.jobs', 'ci.provider: vm needs at least one job'))
    return out


def required_names(product):
    """The job names a vm product's landing gate requires: ``conventions.landing_checks`` when
    named, else every :class:`VmJob` with ``required`` (P4, C4)."""
    named = product.conventions.get('landing_checks')
    if named:
        return (str(named),) if isinstance(named, str) else tuple(str(n) for n in named)
    return tuple(j.name for j in jobs(product).values() if j.required)


# ---- the transport (§2) ------------------------------------------------------------------------

#: ``run_ssh``/``push_sha`` kinds that change the remote — refused under a dry run (C17); a poll
#: (``kind='read'``) is never touched.
_MUTATING = ('start', 'cancel', 'cleanup')


def ssh_argv(host, timeout_s=CONNECT_TIMEOUT_S):
    """``['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o',
    f'ConnectTimeout={timeout_s}', host.ssh, 'sh', '-s']`` — the whole flag set and no more
    (C3). No ``-i``, no ``-p``, no user, no ``ProxyJump``: that is the operator's own ssh
    config."""
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', f'ConnectTimeout={timeout_s}', host.ssh, 'sh', '-s']


def run_ssh(host, script, kind='read', timeout_s=None, run=subprocess.run):
    """``(rc, stdout, stderr)`` for ``script`` on ``host``'s stdin, via :func:`ssh_argv`.
    ``kind`` other than ``'read'`` is a write: a dry run in progress refuses it — prints
    :func:`asf.mutation_guard.would_line` and returns ``(1, '', line)`` before any process
    starts (C17). Never raises: an ``OSError`` or a timeout is ``(124, '', <why>)``."""
    timeout_s = timeout_s or CONNECT_TIMEOUT_S
    argv = ssh_argv(host, timeout_s)
    if kind in _MUTATING and mutation_guard.is_active():
        line = mutation_guard.would_line('ssh', argv)
        print(line)
        return 1, '', line
    try:
        p = run(argv, input=script, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as ex:
        return 124, '', f'ssh timed out after {timeout_s}s: {ex}'
    except OSError as ex:
        return 124, '', f'ssh failed to start: {ex}'
    return p.returncode, p.stdout, p.stderr


#: ensures ``<root>/repo.git`` exists before the first push to a host (idempotent; also
#: re-checked by :data:`REMOTE_START`, harmless either way).
_ENSURE_REPO = ('mkdir -p {root} && (git -C {repo} rev-parse --git-dir >/dev/null 2>&1 || '
               'git init -q --bare {repo})')


def ensure_repo(host, run=subprocess.run):
    """``mkdir -p <root>``, ``git init --bare`` ``<root>/repo.git`` if absent. A write: refused
    under a dry run (C17). ``(ok, why)``."""
    rc, _out, err = run_ssh(host, _ENSURE_REPO.format(root=shlex.quote(host.root),
                                                       repo=shlex.quote(f'{host.root}/repo.git')),
                            kind='start', run=run)
    return rc == 0, err


def push_sha(product, host, sha, run_id, run=subprocess.run):
    """``git push --force`` ``sha`` to ``<root>/repo.git`` on ``host`` as
    ``refs/asf/<run_id>`` (C5), from the product's own checkout — so a CI host never holds a
    credential for the code host. A write: refused under a dry run through the same guard, with
    the same line shape (C17). ``(True, '')`` / ``(False, why)``."""
    repo = f'{host.root}/repo.git'
    target = f'{host.ssh}:{repo}'
    refspec = f'{sha}:refs/asf/{run_id}'
    if mutation_guard.is_active():
        line = mutation_guard.would_line('git', ['push', '--force', target, refspec])
        print(line)
        return False, line
    argv = ['git', 'push', '--force', target, refspec]
    try:
        p = run(argv, cwd=product.repo_dir, capture_output=True, text=True)
    except OSError as ex:
        return False, f'git push failed to start: {ex}'
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or f'git push exited {p.returncode}').strip()
    return True, ''


def _paths(host, run_id):
    root = host.root
    repo = f'{root}/repo.git'
    rundir = f'{root}/runs/{run_id}'
    return {'runs_dir': f'{root}/runs', 'repo': repo, 'rundir': rundir, 'src': f'{rundir}/src',
           'cmdfile': f'{rundir}/cmd', 'log': f'{rundir}/log', 'pidfile': f'{rundir}/pid',
           'exitfile': f'{rundir}/exit', 'ref': f'refs/asf/{run_id}'}


def _qpaths(host, run_id):
    return {k: shlex.quote(v) for k, v in _paths(host, run_id).items()}


#: starts the job's command detached in its own process group (``set -m``), stdout and stderr
#: to ``<rundir>/log``, the exit code to ``<rundir>/exit`` on completion, the pid printed and
#: kept at ``<rundir>/pid``. No ``timeout(1)`` anywhere (C7) — the pass owns the clock.
REMOTE_START = '''set -e
mkdir -p {runs_dir}
if [ ! -d {repo}/objects ]; then git init -q --bare {repo}; fi
mkdir -p {rundir}
git -C {repo} worktree add -q --detach {src} {ref}
printf '%s\\n' {cmd} > {cmdfile}
set -m
( set +e; cd {src} && sh {cmdfile} > {log} 2>&1 < /dev/null; echo $? > {exitfile} ) > /dev/null 2>&1 < /dev/null &
pid=$!
echo "$pid" > {pidfile}
echo "$pid"
'''

#: one call per host per pass whatever the number of runs: ``<run_id> <exit-or-dash> <pid>
#: <bytes-of-log>`` per directory under ``<root>/runs``.
REMOTE_POLL = '''for d in {runs_dir}/*/; do
  [ -d "$d" ] || continue
  rid=$(basename "$d")
  ec=-
  [ -f "$d/exit" ] && ec=$(cat "$d/exit")
  pid=$(cat "$d/pid" 2>/dev/null) || pid=-
  bytes=$(wc -c < "$d/log" 2>/dev/null) || bytes=0
  printf '%s %s %s %s\\n' "$rid" "$ec" "$pid" "$bytes"
done
'''

#: ``kill -TERM -<pgid>``, a grace, ``kill -KILL -<pgid>``.
REMOTE_CANCEL = '''pid=$(cat {pidfile} 2>/dev/null)
if [ -n "$pid" ]; then
  kill -TERM -"$pid" 2>/dev/null || true
  sleep {grace}
  kill -KILL -"$pid" 2>/dev/null || true
  echo "killed $pid"
else
  echo "no pid"
fi
'''

#: ``git worktree remove``, ``rm -rf`` the run directory, delete the pushed ref — nothing moved
#: anywhere first (the artifact move is cut, this replan's own T-0709).
_CLEANUP = '''git -C {repo} worktree remove --force {src} >/dev/null 2>&1 || true
rm -rf {rundir}
git -C {repo} update-ref -d {ref} >/dev/null 2>&1 || true
'''

_TAIL = 'tail -c {n} {log} 2>/dev/null || true'


def _start_script(host, run_id, command):
    q = _qpaths(host, run_id)
    return REMOTE_START.format(cmd=shlex.quote(command), **q)


def _cleanup_remote(host, run_id, run):
    q = _qpaths(host, run_id)
    return run_ssh(host, _CLEANUP.format(repo=q['repo'], src=q['src'], rundir=q['rundir'],
                                         ref=q['ref']), kind='cleanup', run=run)


def _cancel_remote(host, run_id, run):
    q = _qpaths(host, run_id)
    run_ssh(host, REMOTE_CANCEL.format(pidfile=q['pidfile'], grace=CANCEL_GRACE_S),
           kind='cancel', run=run)
    _cleanup_remote(host, run_id, run)


def _remote_tail(host, run_id, run):
    q = _qpaths(host, run_id)
    rc, stdout, _err = run_ssh(host, _TAIL.format(n=RED_LOG_TAIL_BYTES, log=q['log']),
                               kind='read', run=run)
    return stdout if rc == 0 else ''


def _red_log_lines(raw):
    from asf.harvest.lane import red_log_lines  # PD11 — lazy, symmetric with lane's own import
    return red_log_lines(raw)


# ---- the store (§3) -----------------------------------------------------------------------------

def _store_path(product):
    return os.path.join(env.state_dir(product.name), STORE_FILE)


def load(product):
    """The store: ``{'version': 1, 'at': …, 'runs': {sha: {job: row}}}``. Never raises — an
    unreadable or absent file is the empty store, which is what makes every required name
    ``pending`` the automatic answer (PD13)."""
    try:
        with open(_store_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('version', 1)
    data.setdefault('at', 0)
    if not isinstance(data.get('runs'), dict):
        data['runs'] = {}
    return data


def save(product, data):
    """Atomic: ``<file>.<pid>`` then :func:`os.replace`."""
    path = _store_path(product)
    tmp = f'{path}.{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def rows_at(product, sha):
    """``{job: row}`` at ``sha``; ``{}`` when the store has none."""
    return dict(load(product).get('runs', {}).get(sha, {}))


def in_flight(product):
    """Every ``running`` row, for capacity (C14): ``[{**row, 'sha': sha, 'job': job}]``."""
    out = []
    for sha, jobs_at in load(product).get('runs', {}).items():
        for job, row in jobs_at.items():
            if row.get('state') == RUNNING:
                out.append(dict(row, sha=sha, job=job))
    return out


# ---- which shas matter, with no Lane (PD10) --------------------------------------------------

def heads(product, run=subprocess.run):
    """``{branch: sha}`` of every head on origin — one ``git ls-remote --heads origin`` in the
    product's own checkout. ``{}`` on any failure (PD10)."""
    try:
        p = run(['git', 'ls-remote', '--heads', 'origin'], cwd=product.repo_dir,
               capture_output=True, text=True)
    except OSError:
        return {}
    if p.returncode != 0:
        return {}
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/heads/'):
            out[parts[1][len('refs/heads/'):]] = parts[0]
    return out


def dispatch_targets(product, run=subprocess.run):
    """``[(sha, branch)]`` in C9's order: the trunk head first (``branch`` ``None``, never
    superseded), then every lane branch's head sorted by name (P16's filter, against this
    product's own conventions, no record read). ``[]`` when ``heads`` reads nothing."""
    h = heads(product, run=run)
    if not h:
        return []
    conv = product.conventions
    targets = []
    trunk_sha = h.get(product.main)
    if trunk_sha:
        targets.append((trunk_sha, None))
    for b in sorted(h):
        if b == product.main or not conv.branch_kind(b):
            continue
        targets.append((h[b], b))
    return targets


def trunk_shas(product, limit=GREEN_TRUNK_LIMIT, run=subprocess.run):
    """The trunk's last ``limit`` commits, newest first — ``git rev-list --first-parent`` on
    ``origin/<main>`` (PD3). ``[]`` on any failure."""
    try:
        p = run(['git', 'rev-list', '--first-parent', '-n', str(limit), f'origin/{product.main}'],
               cwd=product.repo_dir, capture_output=True, text=True)
    except OSError:
        return []
    if p.returncode != 0:
        return []
    return [line for line in p.stdout.splitlines() if line.strip()]


# ---- the pass (§5) ------------------------------------------------------------------------------

def _host_for_row(product, row):
    return next((h for h in hosts(product) if h.name == row.get('host')), None)


def _elapsed_text(row, now):
    started = row.get('started')
    secs = max(0, int(now - started)) if started is not None else 0
    return f'{secs // 60}m{secs % 60:02d}s'


def _collect(product, data, out, run, now):
    """One ``REMOTE_POLL`` per host with a ``running`` row; an ``exit`` file concludes
    ``passed`` on 0 else ``failed``, its tail trimmed (C18) and the run directory removed."""
    hosts_map = {h.name: h for h in hosts(product)}
    by_host = {}
    for sha, jobs_at in data['runs'].items():
        for job, row in jobs_at.items():
            if row.get('state') == RUNNING:
                by_host.setdefault(row.get('host'), []).append((sha, job, row))
    concluded = 0
    for host_name, rows in by_host.items():
        host = hosts_map.get(host_name)
        if host is None:
            continue
        rc, stdout, _err = run_ssh(host, REMOTE_POLL.format(runs_dir=shlex.quote(f'{host.root}/runs')),
                                   kind='read', run=run)
        polled = {}
        if rc == 0:
            for line in stdout.splitlines():
                parts = line.split()
                if len(parts) == 4:
                    polled[parts[0]] = parts
        for sha, job, row in rows:
            info = polled.get(row['run_id'])
            if not info or info[1] == '-':
                continue
            exit_code = int(info[1])
            row['log'] = _red_log_lines(_remote_tail(host, row['run_id'], run))
            row['state'] = PASSED if exit_code == 0 else FAILED
            row['exit'] = exit_code
            row['ended'] = now
            if row['state'] in RED_STATES:
                row['ever_red'] = True
            _cleanup_remote(host, row['run_id'], run)
            out(f"ci vm: {row.get('branch') or product.main} {sha[:9]} {job} "
                f"{row['state']} on {host_name} ({_elapsed_text(row, now)})")
            concluded += 1
    return concluded


def _supersede(product, data, out, run, now):
    """A ``running`` run whose branch's head moved is cancelled, cleaned, ``superseded_by`` the
    new sha. A trunk run (``branch`` falsy) is never superseded (C8)."""
    remote_heads = heads(product, run=run)
    cancelled = 0
    for sha, jobs_at in list(data['runs'].items()):
        for job, row in list(jobs_at.items()):
            if row.get('state') != RUNNING or not row.get('branch'):
                continue
            new_sha = remote_heads.get(row['branch'])
            if not new_sha or new_sha == sha:
                continue
            host = _host_for_row(product, row)
            if host:
                _cancel_remote(host, row['run_id'], run)
            row['state'] = CANCELLED
            row['ended'] = now
            row['superseded_by'] = new_sha
            row['ever_red'] = True
            out(f"ci vm: {row['branch']} {sha[:9]} {job} superseded by {new_sha[:9]}")
            cancelled += 1
    return cancelled


def _time_out(product, data, out, run, now):
    """A ``running`` run past its job's ``timeout_min`` on the pass's own clock: its tail
    collected first, then cancelled, cleaned, ``timeout`` (C7)."""
    jobs_map = jobs(product)
    timed_out = 0
    for sha, jobs_at in list(data['runs'].items()):
        for job, row in list(jobs_at.items()):
            if row.get('state') != RUNNING:
                continue
            job_cfg = jobs_map.get(job)
            limit_min = job_cfg.timeout_min if job_cfg else DEFAULT_TIMEOUT_MIN
            started = row.get('started')
            if started is None or now - started <= limit_min * 60:
                continue
            host = _host_for_row(product, row)
            if host:
                row['log'] = _red_log_lines(_remote_tail(host, row['run_id'], run))
                _cancel_remote(host, row['run_id'], run)
            row['state'] = TIMEOUT
            row['ended'] = now
            row['ever_red'] = True
            out(f"ci vm: {row.get('branch') or product.main} {sha[:9]} {job} timeout past "
                f"{limit_min}m on {row.get('host')}")
            timed_out += 1
    return timed_out


def _dispatch(product, data, out, run, now):
    """For every target of :func:`dispatch_targets`, in order, and every job with no row at its
    sha: a host whose labels satisfy the job's and that has a free slot, :func:`push_sha`,
    :data:`REMOTE_START`, the ``running`` row (C9)."""
    targets = dispatch_targets(product, run=run)
    if not targets:
        return 0
    hosts_list = hosts(product)
    jobs_map = jobs(product)
    used = {}
    for jobs_at in data['runs'].values():
        for row in jobs_at.values():
            if row.get('state') == RUNNING and row.get('host'):
                used[row['host']] = used.get(row['host'], 0) + 1
    dispatched = 0
    for sha, branch in targets:
        rows_here = data['runs'].setdefault(sha, {})
        for job_name, job_cfg in jobs_map.items():
            if job_name in rows_here:
                continue
            host = next((h for h in hosts_list if job_cfg.labels <= h.labels
                        and used.get(h.name, 0) < h.slots), None)
            if host is None:
                out(f"ci vm: no free host for {branch or product.main} {sha[:9]} {job_name} "
                    f"— tried next pass")
                continue
            run_id = f'{sha[:12]}-{job_name}-1'
            ok, why = ensure_repo(host, run=run)
            if not ok:
                out(f"ci vm: provisioning {host.name} failed — {why}")
                continue
            ok, why = push_sha(product, host, sha, run_id, run=run)
            if not ok:
                out(f"ci vm: push of {sha[:9]} to {host.name} failed — {why}")
                continue
            rc, _out, err = run_ssh(host, _start_script(host, run_id, job_cfg.command),
                                    kind='start', run=run)
            if rc != 0:
                out(f"ci vm: start of {job_name} at {sha[:9]} on {host.name} failed — {err}")
                continue
            used[host.name] = used.get(host.name, 0) + 1
            rows_here[job_name] = {
                'state': RUNNING, 'host': host.name, 'run_id': run_id, 'pid': None,
                'branch': branch, 'attempt': 1, 'started': now, 'ended': None, 'exit': None,
                'log': [], 'superseded_by': None, 'ever_red': False,
            }
            out(f"ci vm: dispatch {branch or product.main} {sha[:9]} {job_name} → {host.name} "
                f"(slot {used[host.name]}/{host.slots})")
            dispatched += 1
    return dispatched


def _acquire_lock(product, wait_s=0):
    f = open(os.path.join(env.state_dir(product.name), LOCK_FILE), 'a')
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(0.5)


def vm_pass(product, out=print, dry_run=False, now=None, run=subprocess.run, wait_s=0):
    """One pass under ``<state_dir>/ci-vm.lock``: collect, supersede, time out, dispatch —
    four steps, each freeing what the next needs. ``(dispatched, concluded)``; ``None`` when
    another pass holds the lock. Not a vm product: ``(0, 0)``, no ssh and no git call."""
    if not enabled(product):
        return 0, 0
    lock = _acquire_lock(product, wait_s)
    if lock is None:
        out(f'ci vm: a pass of {product.name} runs already — skipped')
        return None
    try:
        now = now if now is not None else time.time()
        data = load(product)
        with (mutation_guard.active() if dry_run else contextlib.nullcontext()):
            concluded = _collect(product, data, out, run, now)
            concluded += _supersede(product, data, out, run, now)
            concluded += _time_out(product, data, out, run, now)
            dispatched = _dispatch(product, data, out, run, now)
        data['at'] = now
        save(product, data)
        return dispatched, concluded
    finally:
        lock.close()


def cmd_vm(args, out=print):
    """``asf ci vm``: the hosts, their slots, what is running and at which sha. Writes nothing.
    ``--apply``: run the pass instead (:func:`vm_pass`) — the scheduler's ``ci-vm`` job."""
    product = env.load_product(args.product)
    if not enabled(product):
        out(f'ci vm: {product.name} is not ci.provider: vm')
        return 0
    if getattr(args, 'apply', False):
        got = vm_pass(product, out=out, dry_run=getattr(args, 'dry_run', False))
        if got is None:
            out(f'ci vm: a pass of {product.name} runs already — skipped')
        return 0
    running = in_flight(product)
    out(f'== CI VM {product.name} ({len(hosts(product))} host(s), {len(running)} running)')
    for h in hosts(product):
        used = sum(1 for r in running if r.get('host') == h.name)
        out(f'{h.name}: {used}/{h.slots} slots — {h.ssh}')
    for r in running:
        out(f"  {r['sha'][:9]} {r['job']} on {r.get('host')} since {r.get('started')}")
    return 0


def register(ci_subparsers):
    """``asf ci vm [--apply] [--dry-run]``: the view without ``--apply``, the pass with it.
    T-0710 wires this into the ``ci`` group beside ``queue``/``reserve``/``reconcile``."""
    v = ci_subparsers.add_parser('vm', help='ci.provider: vm — the hosts, what is running, and '
                                 'the pass that dispatches, collects, supersedes and times out')
    env.add_product_arg(v)
    v.add_argument('--apply', action='store_true', help='run the pass (asf.<product>.ci-vm)')
    v.add_argument('--dry-run', action='store_true', help='refuse every write; see what would run')
    v.set_defaults(run=cmd_vm)
    return v
