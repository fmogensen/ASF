"""asf.connectors — the one seam between ASF and each external service it drives.

A **connector** is the implementation of one *kind* of external concern: ``forge`` (pull
requests, reviews, checks, merges), ``ci`` (runs, jobs, reruns), ``runtime`` (an agent session),
``scheduler`` (the clocks), ``quota`` (an account's usage) and ``secrets`` (resolving an
``auth_env`` reference). Each kind has a :mod:`typing.Protocol` in
:mod:`asf.connectors.protocols`, one in-tree default, and a registry entry here; the rest of ASF
asks this module for the active one (:func:`get`) and never names the service itself.

Which implementation is active is config, ``config.yaml``::

    connectors:
      forge: github            # a name: an in-tree implementation, or a Python entry point
      ci:
        command: my-ci-bridge  # the command form: a shell command with a JSON contract
                               # (:mod:`asf.connectors.command`)

A name resolves, in order, to an implementation registered in-process (:func:`register` — a
test's fake), an in-tree one (:data:`BUILTIN`), then a third-party one published under the Python
entry-point group ``asf.connectors.<kind>``. An unset kind is its :data:`DEFAULTS` entry — unless
an older key already chose for it (``worker_pool.backend`` for the runtime, ``scheduler.kind``
for the scheduler, ``worker_pool.quota_command`` for quota), which is still honoured, so a config
written before connectors existed behaves exactly as it did. A name that resolves nowhere is an
error (:class:`ConnectorError`) — never a silent fall back to the default, which would act on a
service the operator did not choose.

Every factory is called with the operator config (``cfg``, a dict) and returns the connector.
"""
import importlib
import os

#: The kinds, in the order ``asf doctor`` lists them.
KINDS = ('forge', 'ci', 'runtime', 'scheduler', 'quota', 'secrets')

#: The entry-point group a third-party implementation of ``kind`` is published under.
ENTRY_POINT_GROUP = 'asf.connectors.{kind}'

#: The name that selects the command form (also selected by a mapping with ``command:``).
COMMAND = 'command'

#: Each kind's implementation when nothing is configured: today's behaviour.
DEFAULTS = {
    'forge': 'github',
    'ci': 'github-actions',
    'runtime': 'claude-code',
    'quota': 'none',
    'scheduler': 'launchd',
    'secrets': 'file',
}

#: The in-tree implementations: ``{kind: {name: 'module:factory'}}``, imported on first use.
BUILTIN = {
    'forge': {
        'github': 'asf.connectors.github_forge:GitHubForge',
        'fake': 'asf.connectors.fakes:FakeForge',
    },
    'ci': {
        'github-actions': 'asf.connectors.ci_github:GitHubActionsCI',
        'fake': 'asf.connectors.fakes:FakeCI',
    },
    'runtime': {
        'claude-code': 'asf.connectors.claude_code:connector',
        'fake': 'asf.connectors.claude_code:fake',
    },
    'quota': {
        'none': 'asf.connectors.quota:none',
        'command': 'asf.connectors.quota:command',
        'fake': 'asf.connectors.quota:fake',
    },
    'secrets': {
        'file': 'asf.connectors.secrets:file',
        'command': 'asf.connectors.secrets:command',
    },
    'scheduler': {
        'launchd': 'asf.connectors.launchd:LaunchdScheduler',
        'cron': 'asf.connectors.launchd:CronScheduler',
        'none': 'asf.connectors.launchd:NoScheduler',
        'systemd': 'asf.connectors.systemd:SystemdScheduler',
        'fake': 'asf.connectors.fakes:FakeScheduler',
    },
}

#: The kinds whose command form is the generic JSON contract of :mod:`asf.connectors.command`
#: (``quota`` and ``secrets`` keep their own one-line contracts; see their modules).
GENERIC_COMMAND_KINDS = ('forge', 'ci', 'runtime', 'scheduler')

_registered = {}
_instances = {}
_config_cache = {}


class ConnectorError(Exception):
    """A configured connector that cannot be resolved or built."""


# ---- the config ---------------------------------------------------------------------------

def _operator_config():
    """The operator config (``config.yaml``) as parsed — ``{}`` when absent or unreadable. Read
    once per change of the file (path and mtime), so a hot path pays a ``stat``."""
    from asf import env
    path = env.config_path()
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    hit = _config_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        cfg = env.load_file(path) or {}
    except Exception:  # noqa: BLE001 — a malformed config is doctor's to name, not a crash here
        cfg = {}
    _config_cache.clear()
    _config_cache[path] = (stamp, cfg)
    return cfg


def _legacy(kind, cfg):
    """The name an older config key already chose for ``kind`` (with that key), or None."""
    cfg = cfg or {}
    pool = cfg.get('worker_pool') or {}
    if kind == 'runtime' and pool.get('backend'):
        # as before connectors: ``fake`` replays, any other backend is the coding-agent CLI
        backend = str(pool['backend']).replace('_', '-')
        return ('fake' if backend == 'fake' else DEFAULTS['runtime']), 'worker_pool.backend'
    if kind == 'scheduler':
        sched = cfg.get('scheduler') or {}
        if sched.get('kind') or sched.get('provider'):
            return str(sched.get('kind') or sched.get('provider')), 'scheduler.kind'
    if kind == 'quota' and pool.get('quota_command'):
        return COMMAND, 'worker_pool.quota_command'
    return None


def configured(kind, cfg=None):
    """``(name, source, spec)`` for ``kind``: the implementation's name, where that choice came
    from (``connectors.<kind>``, a legacy key, or ``default``) and the raw config value (a name,
    or the command form's mapping). ``cfg`` None reads the operator config."""
    if kind not in KINDS:
        raise ConnectorError(f'unknown connector kind {kind!r} (kinds: {", ".join(KINDS)})')
    if cfg is None:
        cfg = _operator_config()
    block = (cfg or {}).get('connectors') or {}
    spec = block.get(kind) if isinstance(block, dict) else None
    if isinstance(spec, dict):
        if spec.get('command'):
            return COMMAND, f'connectors.{kind}', spec
        name = spec.get('name')
        if name:
            return str(name), f'connectors.{kind}', spec
        raise ConnectorError(f'connectors.{kind}: a mapping needs `command:` or `name:`')
    if spec:
        return str(spec), f'connectors.{kind}', spec
    legacy = _legacy(kind, cfg)
    if legacy:
        return legacy[0], legacy[1], None
    return DEFAULTS[kind], 'default', None


# ---- resolving a name ---------------------------------------------------------------------

def _load(target):
    module, _, attr = target.partition(':')
    obj = importlib.import_module(module)
    for part in attr.split('.'):
        obj = getattr(obj, part)
    return obj


def entry_point(kind, name):
    """The factory a third-party package publishes as ``name`` in the group
    ``asf.connectors.<kind>``, or None."""
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group=ENTRY_POINT_GROUP.format(kind=kind))
    except Exception:  # noqa: BLE001 — no metadata to read is no third-party connector
        return None
    for ep in eps:
        if ep.name == name:
            return ep.load()
    return None


def factory(kind, name):
    """The factory for ``kind``/``name``: registered, in-tree, then entry point. Raises
    :class:`ConnectorError` when none is found."""
    if (kind, name) in _registered:
        return _registered[(kind, name)]
    target = BUILTIN.get(kind, {}).get(name)
    if target:
        return _load(target)
    found = entry_point(kind, name)
    if found is not None:
        return found
    known = sorted(set(BUILTIN.get(kind, {})) | {n for k, n in _registered if k == kind})
    raise ConnectorError(
        f'connectors.{kind}: no implementation named {name!r} (in-tree: '
        f'{", ".join(known) or "none"}; or install a package publishing the entry point '
        f'{ENTRY_POINT_GROUP.format(kind=kind)}:{name})')


def build(kind, cfg=None):
    """A new connector for ``kind`` as configured (``cfg`` None: the operator config)."""
    if cfg is None:
        cfg = _operator_config()
    name, _source, spec = configured(kind, cfg)
    if name == COMMAND and kind in GENERIC_COMMAND_KINDS and (kind, COMMAND) not in _registered:
        from asf.connectors import command
        if kind == 'runtime':
            return command.CommandRuntime(command.CommandConnector.from_spec(kind, spec))
        if kind == 'ci':
            return command.CommandCI.from_spec(kind, spec)
        return command.CommandConnector.from_spec(kind, spec)
    return factory(kind, name)(cfg)


def get(kind, cfg=None):
    """The active connector for ``kind``. With ``cfg`` None (the usual call) the operator config
    decides and the instance is kept until that choice changes; with an explicit ``cfg`` a new
    one is built for it."""
    if cfg is not None:
        return build(kind, cfg)
    key = (kind, repr(configured(kind)))
    inst = _instances.get(key)
    if inst is None:
        inst = _instances[key] = build(kind)
    return inst


def forge(cfg=None):
    """The active ``forge`` connector (:class:`asf.connectors.protocols.Forge`)."""
    return get('forge', cfg)


def ci(cfg=None):
    """The active ``ci`` connector (:class:`asf.connectors.protocols.CI`)."""
    return get('ci', cfg)


def runtime(cfg=None):
    """The active ``runtime`` connector (:class:`asf.connectors.protocols.Runtime`)."""
    return get('runtime', cfg)


def active(cfg=None):
    """``[(kind, name, source)]`` for every kind, as configured — what ``asf doctor`` lists."""
    if cfg is None:
        cfg = _operator_config()
    out = []
    for kind in KINDS:
        try:
            name, source, _spec = configured(kind, cfg)
        except ConnectorError as e:
            name, source = f'error: {e}', f'connectors.{kind}'
        out.append((kind, name, source))
    return out


def problems(cfg=None):
    """``[(kind, message)]`` for each configured kind whose implementation cannot be found."""
    if cfg is None:
        cfg = _operator_config()
    out = []
    for kind in KINDS:
        try:
            name, _source, spec = configured(kind, cfg)
            if name == COMMAND:
                if not (isinstance(spec, dict) and spec.get('command')) and _legacy(kind, cfg) is None:
                    out.append((kind, f'connectors.{kind}: command form needs `command:`'))
                continue
            factory(kind, name)
        except ConnectorError as e:
            out.append((kind, str(e)))
        except Exception as e:  # noqa: BLE001 — a third-party import that fails is a finding
            out.append((kind, f'connectors.{kind}: {type(e).__name__}: {e}'))
    return out


# ---- in-process registration (a test's fake, an embedding program) ------------------------

def register(kind, name, fn):
    """Make ``fn`` (``fn(cfg) -> connector``) the implementation named ``name`` for ``kind`` in
    this process, ahead of in-tree and entry-point ones."""
    if kind not in KINDS:
        raise ConnectorError(f'unknown connector kind {kind!r}')
    _registered[(kind, name)] = fn
    _instances.clear()


def unregister(kind, name):
    _registered.pop((kind, name), None)
    _instances.clear()


def reset():
    """Forget registrations, built instances and the cached config (tests)."""
    _registered.clear()
    _instances.clear()
    _config_cache.clear()
