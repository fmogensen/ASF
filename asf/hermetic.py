"""asf.hermetic — the one environment for anything ASF runs outside itself.

Three things run as subprocesses with an environment of their own: the harvest gate (a branch's
test command and ``asf check``), a worker session, and — in the suite — every ``python -m
asf.cli`` a test starts. Each used to build its environment by hand, and each leaked something
in that the next Bug was about: the tick's ``ASF_PRODUCT`` into the gate (B-0033), the
harvester's own package ahead of the gated worktree (B-0033b), the host's ``init.defaultBranch``
into a fixture (B-0038), the operator's live ``~/.ASF`` into the suite (B-0043), a git hook's
``GIT_DIR`` into a child that then wrote into the wrong repo. :func:`build` is the one place
those rules live.

Always: no git-hook variable, no caller identity (:data:`CALLER_IDENTITY`), none of the config
keys a child never inherits (:data:`GIT_CONFIG_NOT_INHERITED` — the caller session's own
``core.hooksPath``), and git's ``init.defaultBranch`` pinned to the trunk through
``GIT_CONFIG_*`` (a child never learns the branch name from the host). Then what the caller
asks for: ``worktree`` first on ``PYTHONPATH``
(then the package that is running, then whatever the base had), ``identity`` for a worker (its
own ``ASF_PRODUCT``/``ASF_JOB``/``ASF_SESSION``/``BACKLOG_ID_RANGE`` — set, not inherited),
``home`` to point ``HOME`` somewhere else (a test's temp home; a worker account's own).

Two modes. ``gate`` (the default — the harvest gate, the suite) starts from the whole base
environment and removes what is listed above: a product's tests may need the machine's tools.
``worker`` (a worker session and its worktree setup command) starts from nothing and keeps only
:data:`WORKER_ALLOW` (plus any ``LC_*``) and the names ``worker_pool.env_passthrough`` lists: an
operator's token, cloud credential or agent socket in the tick's environment never reaches a
session unless the operator names it.
"""
import os
import re

#: The variables that name the caller — the tick's product, a session's job, its session id and
#: mint range. None may reach a child that is not that caller: the gate is the branch's result, a
#: worker's session its own.
CALLER_IDENTITY = ('ASF_PRODUCT', 'ASF_JOB', 'ASF_SESSION', 'BACKLOG_ID_RANGE')

#: What a git hook exports; a ``git`` child that inherits them ignores its ``cwd``.
GIT_HOOK = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')

#: Config keys a child never inherits through the base's ``GIT_CONFIG_*`` — lowercase, the way
#: git compares a section and a key. ``core.hooksPath`` is a caller session's own hook dir
#: (:func:`asf.workers.githooks.ensure`, F-0076), and it binds *every* repo the child touches,
#: not just the session's: a gate or a suite that inherits it sees the caller's hooks in a
#: fixture repo it just created (B-0114). A caller that means to set one passes ``git_config``.
#: This is the ``GIT_CONFIG_COUNT``/``KEY_n`` channel only — git also honours
#: ``GIT_CONFIG_PARAMETERS`` (what ``git -c`` exports to its own descendants) and
#: ``GIT_CONFIG_GLOBAL``; nothing in ASF spawns a child through either today.
GIT_CONFIG_NOT_INHERITED = ('core.hookspath',)

#: ``GIT_CONFIG_KEY_3``/``GIT_CONFIG_VALUE_3`` — one entry of git's environment config.
GIT_CONFIG_VAR_RE = re.compile(r'^GIT_CONFIG_(?:KEY|VALUE)_\d+$')

DEFAULT_TRUNK = 'main'

#: What a worker session keeps from the base environment (``mode='worker'``): the few names a
#: shell and a toolchain need to run at all. Everything else must be named in
#: ``worker_pool.env_passthrough``. ``HOME`` is not here: a worker's HOME is its account's own
#: (:func:`asf.workers.runtime.session_home`), and the operator's only under
#: ``isolate_home: false``.
WORKER_ALLOW = ('PATH', 'LANG', 'TERM', 'TMPDIR', 'USER', 'SHELL')

#: Also kept in worker mode: every variable with this prefix (``LC_ALL``, ``LC_CTYPE``, …).
WORKER_ALLOW_PREFIX = 'LC_'

MODES = ('gate', 'worker')


def package_parent():
    """The directory the running ``asf`` package is imported from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git_config_pairs(env):
    """The ``(key, value)`` pairs ``env`` carries as ``GIT_CONFIG_COUNT`` + ``KEY_n``/``VALUE_n``,
    in git's own order. An unreadable or missing count reads as none."""
    try:
        n = int(env.get('GIT_CONFIG_COUNT') or 0)
    except ValueError:
        return []
    out = []
    for i in range(max(n, 0)):
        key = env.get(f'GIT_CONFIG_KEY_{i}')
        if key is not None:  # git itself would fail on the gap; ASF just drops it
            out.append((key, env.get(f'GIT_CONFIG_VALUE_{i}', '')))
    return out


def strip_git_config(env, keys=GIT_CONFIG_NOT_INHERITED):
    """Removes every ``GIT_CONFIG_*`` entry of ``env`` whose key is in ``keys``, and rewrites what
    is left as a contiguous run — git reads ``KEY_0``…``KEY_<count-1>`` and a hole loses the rest.
    Keys compare lowercased, the way git compares a section and a key; no entry here has a
    subsection, which git would compare case-sensitively instead. A base that carried a count
    keeps one even when every pair went, which git reads the same as none. Mutates and returns
    ``env``."""
    drop = {k.strip().lower() for k in keys}
    kept = [(k, v) for k, v in git_config_pairs(env) if k.strip().lower() not in drop]
    had = 'GIT_CONFIG_COUNT' in env
    for name in [n for n in env if n == 'GIT_CONFIG_COUNT' or GIT_CONFIG_VAR_RE.match(n)]:
        env.pop(name, None)
    if not kept and not had:  # a base with no git config keeps none — not a count of zero
        return env
    return _git_config(env, kept)


def _git_config(env, pairs):
    """Append ``pairs`` to git's environment config (``GIT_CONFIG_COUNT`` + ``KEY_n``/``VALUE_n``),
    after whatever the base already carried."""
    n = 0
    try:
        n = int(env.get('GIT_CONFIG_COUNT') or 0)
    except ValueError:
        n = 0
    for key, value in pairs:
        env[f'GIT_CONFIG_KEY_{n}'] = key
        env[f'GIT_CONFIG_VALUE_{n}'] = value
        n += 1
    env['GIT_CONFIG_COUNT'] = str(n)
    return env


def worker_base(base=None, passthrough=(), keep_home=False):
    """The allow-listed part of ``base`` (default the process's own environment): the names in
    :data:`WORKER_ALLOW`, every ``LC_*``, the ``passthrough`` names, and ``HOME`` only when
    ``keep_home``. Nothing else survives — no deny-list to fall behind."""
    base = os.environ if base is None else base
    keep = set(WORKER_ALLOW) | set(passthrough or ())
    if keep_home:
        keep.add('HOME')
    return {k: v for k, v in base.items() if k in keep or k.startswith(WORKER_ALLOW_PREFIX)}


def build(base=None, worktree=None, identity=None, home=None, trunk=DEFAULT_TRUNK,
          pythonpath=True, git_config=(), mode='gate', passthrough=()):
    """The environment for a child of ASF.

    ``base``: the environment to start from (default the process's own). ``worktree``: a
    checkout whose own code must win — first on ``PYTHONPATH``. ``identity``: a worker's own
    ``{ASF_PRODUCT, ASF_JOB, ASF_SESSION, BACKLOG_ID_RANGE, …}``, set after the inherited ones
    are gone. ``home``: ``HOME`` for the child. ``trunk``: the branch name pinned as
    ``init.defaultBranch``. ``pythonpath=False`` leaves ``PYTHONPATH`` as the base had it.
    ``git_config``: more ``(key, value)`` pairs appended after ``init.defaultBranch`` (a
    session's ``core.hooksPath``, F-0076). ``mode``: ``gate`` (the base minus the rules above)
    or ``worker`` (the allow-list, :func:`worker_base`, with ``passthrough`` — and the base's
    ``HOME`` only when no ``home`` is given)."""
    if mode not in MODES:
        raise ValueError(f'hermetic.build: mode {mode!r} is not one of {MODES}')
    if mode == 'worker':
        env = worker_base(base, passthrough, keep_home=not home)
    else:
        env = dict(os.environ if base is None else base)
    for var in GIT_HOOK + CALLER_IDENTITY:
        env.pop(var, None)
    strip_git_config(env)  # the caller's own core.hooksPath is the caller's, never the child's
    _git_config(env, [('init.defaultBranch', trunk or DEFAULT_TRUNK), *git_config])
    if pythonpath:
        parts = ([os.path.abspath(worktree)] if worktree else []) + [package_parent()]
        if env.get('PYTHONPATH'):
            parts.append(env['PYTHONPATH'])
        env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(parts))
    if home:
        env['HOME'] = os.path.expanduser(home)
    for key, value in (identity or {}).items():
        if value is not None:
            env[key] = str(value)
    return env


#: The words that make a variable name look like a credential (``worker_pool.env_passthrough``
#: is checked against them by ``asf doctor``'s ``worker env`` row): a name any ``_``-separated
#: part of which is one of these, or contains one of the longer ones.
CREDENTIAL_PARTS = ('KEY', 'PAT', 'PASS', 'AUTH', 'COOKIE', 'SESSION')
CREDENTIAL_WORDS = ('TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'PASSPHRASE', 'CREDENTIAL',
                    'APIKEY', 'PRIVATE')

#: Names that match a credential word but carry none: a path or a socket, not a secret.
NOT_CREDENTIALS = ('SSH_AUTH_SOCK',)


def looks_like_credential(name):
    """True when the variable ``name`` reads as a secret (``GH_TOKEN``, ``AWS_SECRET_ACCESS_KEY``,
    ``NPM_AUTH``, ``DB_PASSWORD``, ``OPENAI_API_KEY``) — a name, never a value, is judged."""
    upper = str(name or '').upper()
    if not upper or upper in NOT_CREDENTIALS:
        return False
    parts = [p for p in upper.split('_') if p]
    return (any(p in CREDENTIAL_PARTS for p in parts)
            or any(w in p for p in parts for w in CREDENTIAL_WORDS))
