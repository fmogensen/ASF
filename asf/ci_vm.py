"""asf.ci_vm — ``ci.provider: vm``: a product's own machines and commands as its CI, instead of
the PR host's Actions runs.

The config half: ``ci.hosts`` (ssh targets, their labels, slots and root), ``ci.jobs`` (named
commands, which are required, their timeout and labels) and the problems a product file can
state in either block. The transport half: one ``ssh`` and one ``git push`` per run, three small
remote scripts (start, poll, cancel) over a disposable ``git worktree``, and the results store
(``ci-vm.json``) a pass dispatches into, collects from, supersedes and times out — under a lock,
behind the dry-run guard. It has no runner registration, no start queue, no workflow and no
artifact store. ``<root>`` (``ci.hosts[].root``, default :data:`DEFAULT_ROOT`, relative to the
ssh user's home) is the only place on a host this provider ever writes: ``repo.git`` (a bare
repo ASF pushes shas to) and ``runs/<run_id>/`` (removed at conclusion — nothing accumulates).
"""
import contextlib
import dataclasses
import json
import os
import re
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


# ---- the transport: one ssh, one git push, three remote scripts (§2) ---------------------------

def ssh_argv(host, timeout_s=CONNECT_TIMEOUT_S):
    """``['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o',
    f'ConnectTimeout={timeout_s}', host.ssh, 'sh', '-s']`` — the whole flag set and no more
    (C3): no ``-i``, no ``-p``, no user, no ``ProxyJump`` — that is the operator's own
    ``~/.ssh/config``."""
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', f'ConnectTimeout={timeout_s}', host.ssh, 'sh', '-s']


#: an ssh that changes the remote (C17) — the predicate lives here, not in ``mutation_guard``,
#: because there is no ssh argv to inspect: the caller already knows whether it is starting a
#: job or only reading a file
_MUTATING = ('start', 'cancel', 'cleanup')


def run_ssh(host, script, kind='read', timeout_s=None, run=subprocess.run):
    """``(rc, stdout, stderr)`` for ``script`` on ``host``'s stdin — never argv. ``kind`` other
    than ``'read'`` is a write: a dry run in progress refuses it, printing
    :func:`asf.mutation_guard.would_line`, and returns ``(1, '', line)`` before any process
    starts (C17). Never raises: an ``OSError`` or a timeout is ``(124, '', <why>)``."""
    timeout_s = timeout_s or CONNECT_TIMEOUT_S
    argv = ssh_argv(host, timeout_s)
    if kind in _MUTATING and mutation_guard.is_active():
        line = mutation_guard.would_line('ssh', argv[1:])
        print(line)
        return 1, '', line
    try:
        r = run(argv, input=script, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        return 124, '', str(e) or 'ssh timed out'
    except OSError as e:
        return 124, '', str(e) or type(e).__name__
    return r.returncode, r.stdout, r.stderr


def _repo_url(host):
    """``ssh://<host.ssh>/<root>/repo.git`` — ``<root>`` as an absolute path verbatim, else
    relative to the ssh user's home (``~``)."""
    tail = host.root if host.root.startswith('/') else f'/~/{host.root}'
    return f'ssh://{host.ssh}{tail}/repo.git'


def push_sha(product, host, sha, run_id):
    """``git push --force <ssh://…/repo.git> <sha>:refs/asf/<run_id>`` from the product's own
    checkout (C5): ``(True, '')`` or ``(False, why)``. A write — refused under a dry run,
    through the same guard, with the same line shape. ``refs/asf/<run_id>`` is never the
    product's trunk or a protected ref, so :func:`asf.refguard.guard_for` never refuses it."""
    from asf import gitpush, refguard
    r = gitpush.push(['--force', _repo_url(host), f'{sha}:refs/asf/{run_id}'], product.repo_dir,
                     guard=refguard.guard_for(product, product.repo_dir),
                     timeout=CONNECT_TIMEOUT_S + 30)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or '').strip().splitlines()
        return False, (lines[-1] if lines else f'git push exited {r.returncode}')
    return True, ''


def _slug(name):
    """``name`` reduced to the characters a path segment and a git ref name can both carry —
    what a ``run_id`` is built from, so neither needs quoting once assigned to a shell
    variable."""
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', str(name)).strip('-') or 'job'


#: the three scripts (§2), each a ``sh`` program with ``{}`` slots filled by :func:`_fill`
#: (``shlex.quote``). No ``timeout(1)`` anywhere (C7) — the pass owns the clock. Nothing is
#: written outside ``<root>``, which holds exactly ``repo.git`` and ``runs/`` (no
#: ``artifacts/`` — this cut carries none).
REMOTE_START = """set -e
root={root}
mkdir -p "$root/runs"
root=$(cd "$root" && pwd)
rundir="$root/runs/{run_id}"
mkdir -p "$rundir"
[ -d "$root/repo.git" ] || git init --bare -q "$root/repo.git"
git -C "$root/repo.git" worktree add -q --detach "$rundir/src" "refs/asf/{run_id}"
cmd={cmd}
printf '%s' "$cmd" > "$rundir/cmd"
cat > "$rundir/run.sh" <<'ASF_RUN_EOF'
cd "$(dirname "$0")/src"
sh "$(dirname "$0")/cmd" >"$(dirname "$0")/log" 2>&1
echo $? > "$(dirname "$0")/exit"
ASF_RUN_EOF
(
set -m
sh "$rundir/run.sh" >/dev/null 2>&1 &
echo $! > "$rundir/pid"
) >/dev/null 2>&1
pid=$(cat "$rundir/pid")
echo "$pid"
"""

#: one call per host per pass whatever the number of runs: ``<run_id> <exit-or-dash> <pid>
#: <bytes-of-log>`` per directory under ``<root>/runs``
REMOTE_POLL = """root={root}
for d in "$root"/runs/*/; do
  [ -d "$d" ] || continue
  run_id=$(basename "$d")
  exit_val=-
  [ -f "$d/exit" ] && exit_val=$(cat "$d/exit")
  pid_val=-
  [ -f "$d/pid" ] && pid_val=$(cat "$d/pid")
  log_bytes=0
  [ -f "$d/log" ] && log_bytes=$(wc -c < "$d/log" | tr -d ' ')
  printf '%s %s %s %s\\n' "$run_id" "$exit_val" "$pid_val" "$log_bytes"
done
"""

REMOTE_CANCEL = """root={root}
rundir="$root/runs/{run_id}"
if [ -f "$rundir/pid" ]; then
  pid=$(cat "$rundir/pid")
  kill -TERM "-$pid" 2>/dev/null || true
  sleep {grace}
  kill -KILL "-$pid" 2>/dev/null || true
  printf 'killed {run_id} (pgid %s)\\n' "$pid"
else
  printf 'no pid for {run_id}\\n'
fi
"""

#: inlined, small enough not to need its own top-level name in the spec — removes the worktree,
#: the run directory and the pushed ref, nothing moved anywhere first (no artifacts, this cut)
REMOTE_CLEANUP = """root={root}
root=$(cd "$root" 2>/dev/null && pwd || echo "$root")
rundir="$root/runs/{run_id}"
git -C "$root/repo.git" worktree remove --force "$rundir/src" 2>/dev/null || true
rm -rf "$rundir"
git -C "$root/repo.git" update-ref -d "refs/asf/{run_id}" 2>/dev/null || true
"""

#: bounded off the remote before any trimming rule runs (C18)
REMOTE_READ_LOG = """root={root}
f="$root/runs/{run_id}/log"
if [ -f "$f" ]; then
  tail -c {nbytes} "$f"
fi
"""

#: dispatch's own companion to ``push_sha`` — the bare repo a sha is pushed into must exist
#: *before* the push, so dispatch runs this first; REMOTE_START repeats the same check
#: (idempotent) for the host that was provisioned another way
REMOTE_ENSURE_REPO = """root={root}
mkdir -p "$root/runs"
[ -d "$root/repo.git" ] || git init --bare -q "$root/repo.git"
"""

CANCEL_GRACE_S = 2  #: the grace between SIGTERM and SIGKILL in REMOTE_CANCEL


def _fill(tmpl, **kw):
    return tmpl.format(**{k: shlex.quote(str(v)) for k, v in kw.items()})


def _read_tail(host, run_id, run=subprocess.run):
    """The run's log tail off the remote, bounded to :data:`RED_LOG_TAIL_BYTES` (C18); ``''``
    unreadable or absent."""
    rc, out, _err = run_ssh(host, _fill(REMOTE_READ_LOG, root=host.root, run_id=run_id,
                                        nbytes=RED_LOG_TAIL_BYTES), kind='read', run=run)
    return out if rc == 0 else ''


def _trimmed_log(text):
    """``text`` through :func:`asf.harvest.lane.red_log_lines` (PD11: imported lazily — ``lane``
    is never imported at this module's top level) — at most ``RED_LOG_LINES`` kept (C18)."""
    from asf.harvest import lane
    return lane.red_log_lines(text)


def _duration(started, ended):
    if not started or not ended:
        return '?'
    secs = max(0, int(ended - started))
    m, s = divmod(secs, 60)
    return f'{m}m{s:02d}s'


# ---- the store (§3) ------------------------------------------------------------------------

STORE_FILE = 'ci-vm.json'
RUNNING, PASSED, FAILED, TIMEOUT, CANCELLED = 'running', 'passed', 'failed', 'timeout', 'cancelled'
#: a concluded state the trunk walk never forgives, carried forward in ``ever_red`` (PD4/C10)
RED_STATES = (FAILED, TIMEOUT, CANCELLED)
#: state → the ``bucket`` a check dict carries (P2), so nothing downstream learns these names
BUCKETS = {RUNNING: 'pending', PASSED: 'pass', FAILED: 'fail', TIMEOUT: 'fail',
           CANCELLED: 'cancel'}
#: ``checks_at``'s own red buckets (T-0711): a vm run the pass itself cancelled or timed out is
#: red, unlike a cancelled Actions run the CI queue holds for a re-run
RED_BUCKETS = ('fail', 'cancel')


def _store_path(product):
    return os.path.join(env.state_dir(product.name), STORE_FILE)


def load(product):
    """The store, never raising: an absent or unreadable file is ``{'version': 1, 'at': 0,
    'runs': {}}`` — the safe answer, and what makes PD13's "every required name pending" the
    automatic one."""
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
    """Atomic: ``<file>.<pid>`` then ``os.replace``."""
    path = _store_path(product)
    tmp = f'{path}.{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def rows_at(product, sha):
    """``{job: row}`` at ``sha``; ``{}`` when there is none (including a falsy ``sha``)."""
    if not sha:
        return {}
    runs = load(product)['runs'].get(sha)
    return runs if isinstance(runs, dict) else {}


def in_flight(product):
    """``[(sha, job, row), ...]`` for every ``RUNNING`` row — the pass's own slot count, and
    capacity's (C14)."""
    out = []
    for sha, jobs_at in load(product)['runs'].items():
        if not isinstance(jobs_at, dict):
            continue
        for job, row in jobs_at.items():
            if isinstance(row, dict) and row.get('state') == RUNNING:
                out.append((sha, job, row))
    return out


def _host_by_name(product, name):
    return next((h for h in hosts(product) if h.name == name), None)


# ---- which shas matter, with no Lane (PD10) and the trunk's own history (PD3) ------------------

GREEN_TRUNK_LIMIT = 20  #: shas `trunk_shas` reads, newest first — matches today's gh-actions
                        #: `gh run list --limit 20` depth


def heads(product):
    """``{branch: sha}`` from one ``git ls-remote --heads origin`` (P16) — no ``Lane``, no
    record. ``{}`` on any failure."""
    from asf import gitops
    repo_dir = product.repo_dir
    if not repo_dir:
        return {}
    r = gitops.git(['ls-remote', '--heads', 'origin'], repo_dir, timeout=30)
    if not r.ok:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition('\t')
        if sha and ref.startswith('refs/heads/'):
            out[ref[len('refs/heads/'):]] = sha.strip()
    return out


def trunk_shas(product, limit=GREEN_TRUNK_LIMIT):
    """The trunk's own ``--first-parent`` history, newest first, at most ``limit`` shas (PD3):
    one bounded ``git rev-list`` — the reader ``evidence.ci_green_runs`` has none of its own.
    ``--first-parent`` matches :meth:`asf.harvest.lane.GitHubHost.trunk_red`'s own walk, so both
    readers of trunk history agree on what "the trunk's commits" are. ``[]`` on any failure."""
    from asf import gitops
    repo_dir = product.repo_dir
    if not repo_dir:
        return []
    r = gitops.git(['rev-list', '--first-parent', '-n', str(limit),
                    f'origin/{product.conventions.main}'], repo_dir, timeout=30)
    if not r.ok:
        return []
    return [s for s in r.stdout.split() if s]


def dispatch_targets(product):
    """``[(sha, branch_or_None)]`` in C9's order: the trunk's own head first (branch ``None`` —
    never superseded, C8), then every lane branch by name with its head (PD10, mirroring
    :meth:`asf.harvest.lane.Lane.remote_heads`'s filter, P16, with no ``Lane``). ``[]`` when the
    trunk's own sha cannot be read."""
    from asf import gitops
    repo_dir = product.repo_dir
    out = []
    if not repo_dir:
        return out
    conv = product.conventions
    r = gitops.git(['rev-parse', f'origin/{conv.main}'], repo_dir, timeout=30)
    trunk_sha = r.data if r.ok else ''
    if trunk_sha:
        out.append((trunk_sha, None))
    h = heads(product)
    for branch in sorted(h):
        if branch == conv.main or not conv.branch_kind(branch):
            continue
        out.append((h[branch], branch))
    return out


# ---- the pass (§5, cut to four steps: collect, supersede, time out, dispatch) ------------------

PASS_LOCK = 'ci-vm.lock'  #: the pass's own lock, so the lane's call and the clock's are one pass


def _acquire_lock(product, wait_s=0):
    """The pass's lock (an open file holding ``flock``), or None when another pass of the
    product holds it after ``wait_s``. It dies with its process."""
    import fcntl
    f = open(os.path.join(env.state_dir(product.name), PASS_LOCK), 'a')
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


def _collect(product, out, now, run):
    """One ``REMOTE_POLL`` per host with a ``running`` row. An ``exit`` file concludes: ``passed``
    on 0, else ``failed``; its log tail is read and trimmed (C18); the run directory is removed.
    The number concluded."""
    if mutation_guard.is_active():
        return 0
    data = load(product)
    by_host = {}
    for sha, jobs_at in data['runs'].items():
        if not isinstance(jobs_at, dict):
            continue
        for job, row in jobs_at.items():
            if isinstance(row, dict) and row.get('state') == RUNNING and row.get('host'):
                by_host.setdefault(row['host'], []).append((sha, job, row))
    n, changed = 0, False
    for host_name, entries in by_host.items():
        host = _host_by_name(product, host_name)
        if host is None:
            continue
        rc, stdout, _err = run_ssh(host, _fill(REMOTE_POLL, root=host.root), kind='read', run=run)
        if rc != 0:
            continue
        polled = {}
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) == 4:
                polled[parts[0]] = parts[1]
        for sha, job, row in entries:
            exit_val = polled.get(row.get('run_id'))
            if exit_val is None or exit_val == '-':
                continue
            try:
                code = int(exit_val)
            except ValueError:
                continue
            state = PASSED if code == 0 else FAILED
            row['log'] = _trimmed_log(_read_tail(host, row['run_id'], run=run))
            row['exit'] = code
            row['state'] = state
            row['ended'] = now
            if state in RED_STATES:
                row['ever_red'] = True
            run_ssh(host, _fill(REMOTE_CLEANUP, root=host.root, run_id=row['run_id']),
                   kind='cleanup', run=run)
            out(f"ci vm: {row.get('branch') or product.conventions.main} {sha[:9]} {job} "
                f"{state} on {host_name} ({_duration(row.get('started'), now)})")
            n += 1
            changed = True
    if changed:
        data['at'] = now
        save(product, data)
    return n


def _supersede(product, out, now, run):
    """A ``running`` run whose branch's head has moved is cancelled (C8): ``REMOTE_CANCEL``,
    cleanup, ``state: cancelled``, ``superseded_by`` the new sha. A trunk run (``branch`` None)
    is never superseded."""
    if mutation_guard.is_active():
        return
    data = load(product)
    current = heads(product)
    changed = False
    for sha, jobs_at in data['runs'].items():
        if not isinstance(jobs_at, dict):
            continue
        for job, row in jobs_at.items():
            if not isinstance(row, dict) or row.get('state') != RUNNING:
                continue
            branch = row.get('branch')
            if not branch:
                continue
            new_sha = current.get(branch)
            if not new_sha or new_sha == sha:
                continue
            host = _host_by_name(product, row.get('host'))
            if host is not None and row.get('run_id'):
                run_ssh(host, _fill(REMOTE_CANCEL, root=host.root, run_id=row['run_id'],
                                    grace=CANCEL_GRACE_S), kind='cancel', run=run)
                run_ssh(host, _fill(REMOTE_CLEANUP, root=host.root, run_id=row['run_id']),
                       kind='cleanup', run=run)
            row['state'] = CANCELLED
            row['ended'] = now
            row['ever_red'] = True
            row['superseded_by'] = new_sha
            out(f"ci vm: {branch} {sha[:9]} {job} superseded by {new_sha[:9]} on "
                f"{row.get('host')}")
            changed = True
    if changed:
        data['at'] = now
        save(product, data)


def _time_out(product, out, now, run):
    """A ``running`` run past its job's ``timeout_min`` on the pass's own clock (C7): the tail
    collected first, then cancelled, cleaned, ``state: timeout``. The number timed out."""
    if mutation_guard.is_active():
        return 0
    data = load(product)
    job_cfg = jobs(product)
    n, changed = 0, False
    for sha, jobs_at in data['runs'].items():
        if not isinstance(jobs_at, dict):
            continue
        for job, row in jobs_at.items():
            if not isinstance(row, dict) or row.get('state') != RUNNING:
                continue
            limit_min = job_cfg[job].timeout_min if job in job_cfg else DEFAULT_TIMEOUT_MIN
            started = row.get('started')
            if not started or now - started <= limit_min * 60:
                continue
            host = _host_by_name(product, row.get('host'))
            if host is not None and row.get('run_id'):
                row['log'] = _trimmed_log(_read_tail(host, row['run_id'], run=run))
                run_ssh(host, _fill(REMOTE_CANCEL, root=host.root, run_id=row['run_id'],
                                    grace=CANCEL_GRACE_S), kind='cancel', run=run)
                run_ssh(host, _fill(REMOTE_CLEANUP, root=host.root, run_id=row['run_id']),
                       kind='cleanup', run=run)
            row['state'] = TIMEOUT
            row['ended'] = now
            row['ever_red'] = True
            out(f"ci vm: {row.get('branch') or product.conventions.main} {sha[:9]} {job} "
                f"timed out after {limit_min}m on {row.get('host')}")
            n += 1
            changed = True
    if changed:
        data['at'] = now
        save(product, data)
    return n


def _dispatch(product, out, now, run):
    """For every sha that matters (C9: the trunk first, then branches by name) and every job
    with no row at it: a host whose labels satisfy the job's and that has a free slot,
    :func:`push_sha`, ``REMOTE_START``, the ``running`` row. The number dispatched."""
    data = load(product)
    job_cfg = jobs(product)
    host_list = hosts(product)
    if not job_cfg or not host_list:
        return 0
    free = {h.name: h.slots for h in host_list}
    for jobs_at in data['runs'].values():
        if not isinstance(jobs_at, dict):
            continue
        for row in jobs_at.values():
            if isinstance(row, dict) and row.get('state') == RUNNING and row.get('host') in free:
                free[row['host']] -= 1
    total = sum(h.slots for h in host_list)
    n, changed = 0, False
    for sha, branch in dispatch_targets(product):
        existing = data['runs'].get(sha, {})
        label = branch or product.conventions.main
        for job_name, job in job_cfg.items():  # ci.jobs declaration order (C9)
            if job_name in existing:
                continue
            host = next((h for h in host_list
                        if job.labels <= h.labels and free.get(h.name, 0) > 0), None)
            if host is None:
                out(f"ci vm: dispatch {label} {sha[:9]} {job_name} — no free host fits "
                    f"{', '.join(sorted(job.labels)) or 'any labels'}")
                continue
            run_id = f'{sha[:12]}-{_slug(job_name)}-1'
            rc, _o, err = run_ssh(host, _fill(REMOTE_ENSURE_REPO, root=host.root), kind='start',
                                  run=run)
            if rc != 0:
                out(f"ci vm: dispatch {label} {sha[:9]} {job_name} — could not prepare "
                    f"{host.name}: {err.strip() or rc}")
                continue
            ok, why = push_sha(product, host, sha, run_id)
            if not ok:
                out(f"ci vm: dispatch {label} {sha[:9]} {job_name} — push failed: {why}")
                continue
            rc, stdout, err = run_ssh(host, _fill(REMOTE_START, root=host.root, run_id=run_id,
                                                  cmd=job.command), kind='start', run=run)
            if rc != 0:
                out(f"ci vm: dispatch {label} {sha[:9]} {job_name} — start failed on "
                    f"{host.name}: {(err or stdout or '').strip().splitlines()[-1:] or rc}")
                continue
            pid = (stdout or '').strip().splitlines()[-1] if stdout.strip() else ''
            data['runs'].setdefault(sha, {})[job_name] = {
                'state': RUNNING, 'host': host.name, 'run_id': run_id, 'pid': pid,
                'branch': branch, 'attempt': 1, 'started': now, 'ended': None, 'exit': None,
                'log': [], 'superseded_by': None, 'ever_red': False,
            }
            free[host.name] -= 1
            changed = True
            n += 1
            out(f"ci vm: dispatch {label} {sha[:9]} {job_name} → {host.name} "
                f"(slot {total - sum(free.values())}/{total})")
    if changed:
        data['at'] = now
        save(product, data)
    return n


def vm_pass(product, out=print, dry_run=False, now=None, run=subprocess.run, wait_s=0):
    """One pass under ``<state_dir>/ci-vm.lock``: collect, supersede, time out, dispatch — in
    that order, because each frees what the next needs. ``(dispatched, concluded)``; ``None``
    when another pass holds the lock. Not a vm product: ``(0, 0)``, no ssh and no git call."""
    if not enabled(product):
        return 0, 0
    lock = _acquire_lock(product, wait_s)
    if lock is None:
        return None
    try:
        guard = mutation_guard.active() if dry_run else contextlib.nullcontext()
        with guard:
            now = time.time() if now is None else now
            concluded = _collect(product, out=out, now=now, run=run)
            _supersede(product, out=out, now=now, run=run)
            concluded += _time_out(product, out=out, now=now, run=run)
            dispatched = _dispatch(product, out=out, now=now, run=run)
        return dispatched, concluded
    finally:
        lock.close()


# ---- `asf ci vm [--apply] [--dry-run]` ---------------------------------------------------------

def cmd_vm(args, out=print):
    product = env.load_product(args.product)
    if not enabled(product):
        out(f'ci vm: {product.name} is not ci.provider: vm')
        return 0
    if getattr(args, 'apply', False):
        got = vm_pass(product, out=out, dry_run=getattr(args, 'dry_run', False))
        if got is None:
            out('ci vm: another pass is running — try again shortly')
            return 0
        dispatched, concluded = got
        out(f'ci vm: {dispatched} dispatched, {concluded} concluded')
        return 0
    out(f'== CI VM {product.name} (view only — nothing written)')
    running = in_flight(product)
    for host in hosts(product):
        busy = [r for _s, _j, r in running if r.get('host') == host.name]
        out(f"{host.name} ({host.ssh}): {len(busy)}/{host.slots} slot(s) busy"
            + (f", labels {', '.join(sorted(host.labels))}" if host.labels else ''))
        for r in busy:
            out(f"  {r.get('branch') or product.conventions.main} {r.get('run_id')} running "
               f"since {r.get('started')}")
    return 0


def register(ci_subparsers):
    """``asf ci vm [--apply] [--dry-run]`` — the view (every host, its slots, what is running
    and at which sha) without ``--apply``; the pass (:func:`vm_pass`) with it."""
    p = ci_subparsers.add_parser('vm', help="ci.provider: vm — the hosts' slots and what is "
                                            'running, or (--apply) the pass itself')
    env.add_product_arg(p)
    p.add_argument('--apply', action='store_true',
                   help='run the pass: dispatch, collect, supersede, time out')
    p.add_argument('--dry-run', action='store_true',
                   help='with --apply, refuse every write and print what would run (C17)')
    p.set_defaults(run=cmd_vm)
    return p


# ---- the judging seam (§4): the store rendered in the vocabulary the lane already judges -------

def checks_at(product, sha, required=()):
    """``(state, detail, checks)`` — :func:`asf.harvest.lane.pr_checks`'s own triple, for
    ``sha`` (C1). One check dict per row (``name``, ``bucket``, ``link`` ``''``, ``workflow``
    ``'vm'``, ``startedAt``, ``log``, ``host`` — PD9's seven keys) plus one ``pending`` dict per
    required name with no row at the sha at all — a name that matches no ``ci.jobs`` entry says
    so in the overall ``detail`` (C4). ``red``, then ``pending``, then ``green``; never
    ``unknown`` — a falsy ``sha``, an unreadable store, a missing sha entry and a missing
    required name are four spellings of ``pending`` (PD13), because the safe answer to "did CI
    pass" is "not yet"."""
    from asf.harvest import lane
    runs = rows_at(product, sha)
    declared = jobs(product)
    checks = []
    for job_name, row in runs.items():
        state = row.get('state')
        checks.append({
            'name': job_name, 'bucket': BUCKETS.get(state, 'pending'), 'link': '',
            'workflow': PROVIDER, 'startedAt': _iso(row.get('started')), 'log': row.get('log')
            or [], 'host': row.get('host') or '',
        })
    missing_labels = {}
    for name in required:
        if name not in runs:
            checks.append({'name': name, 'bucket': 'pending', 'link': '', 'workflow': PROVIDER,
                           'startedAt': '', 'log': [], 'host': ''})
            if name not in declared:
                missing_labels[name] = f'{name} (names no ci.jobs job)'
    judged = ([c for c in checks if lane.required_name(c.get('name'), required)] if required
              else checks)
    red = [c.get('name') for c in judged if c.get('bucket') in RED_BUCKETS]
    if red:
        return 'red', ', '.join(dict.fromkeys(red)), checks
    pending = [missing_labels.get(c.get('name'), c.get('name')) for c in judged
              if c.get('bucket') == 'pending']
    if pending:
        return 'pending', ', '.join(dict.fromkeys(pending)), checks
    return 'green', f'{len(checks)} check(s)', checks


def _iso(ts):
    if not ts:
        return ''
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def trunk_red_at(product, sha, names):
    """``(red, concluded)`` — two ``frozenset``s (PD1): the subset of ``names`` whose concluded
    row at ``sha`` is red (a state in :data:`RED_STATES`, or ``ever_red`` — B-0129, PD4), and the
    subset with any concluded row at all (red *and* green) —
    :meth:`asf.harvest.lane.GitHubHost.trunk_red`'s two questions per commit (C10)."""
    runs = rows_at(product, sha)
    red, concluded = set(), set()
    for n in names:
        row = runs.get(n)
        if not row:
            continue
        if row.get('state') != RUNNING:
            concluded.add(n)
        if row.get('state') in RED_STATES or row.get('ever_red'):
            red.add(n)
    return frozenset(red), frozenset(concluded)


def green_trunk_shas(product, trunk_shas_, required=()):
    """The shas of ``trunk_shas_`` all of whose ``required`` names passed, input order kept —
    :func:`asf.evidence.evidence.ci_green_runs`'s answer under ``vm`` (C11). With ``required``
    empty, a sha is green when it has at least one row and none of them is non-passed — never on
    an empty store, which would make every trunk sha a prod-deploy candidate."""
    out = []
    for sha in trunk_shas_:
        runs = rows_at(product, sha)
        if not runs:
            continue
        if required:
            if all(runs.get(n, {}).get('state') == PASSED for n in required):
                out.append(sha)
        elif all(r.get('state') == PASSED for r in runs.values()):
            out.append(sha)
    return out
