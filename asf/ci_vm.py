"""asf.ci_vm — ``ci.provider: vm``: a product's own machines and commands as its CI, instead of
the PR host's Actions runs.

This module owns the *config* half only: ``ci.hosts`` (ssh targets, their labels, slots and
root), ``ci.jobs`` (named commands, which are required, their timeout and labels) and the
problems a product file can state in either block. It has no runner registration, no start
queue, no workflow and no artifact store — it never calls ``ssh``, never shells out and never
reads or writes a results file. ``<root>`` (``ci.hosts[].root``, default :data:`DEFAULT_ROOT`,
relative to the ssh user's home) is documented here as the only place on a host this provider
will ever write, for the transport that lands afterward.
"""
import dataclasses

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
