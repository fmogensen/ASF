"""asf.hermetic — the one environment for anything ASF runs outside itself.

Three things run as subprocesses with an environment of their own: the harvest gate (a branch's
test command and ``asf check``), a worker session, and — in the suite — every ``python -m
asf.cli`` a test starts. Each used to build its environment by hand, and each leaked something
in that the next Bug was about: the tick's ``ASF_PRODUCT`` into the gate (B-0033), the
harvester's own package ahead of the gated worktree (B-0033b), the host's ``init.defaultBranch``
into a fixture (B-0038), the operator's live ``~/.ASF`` into the suite (B-0043), a git hook's
``GIT_DIR`` into a child that then wrote into the wrong repo. :func:`build` is the one place
those rules live.

Always: no git-hook variable, no caller identity (:data:`CALLER_IDENTITY`), and git's
``init.defaultBranch`` pinned to the trunk through ``GIT_CONFIG_*`` (a child never learns the
branch name from the host). Then what the caller asks for: ``worktree`` first on ``PYTHONPATH``
(then the package that is running, then whatever the base had), ``identity`` for a worker (its
own ``ASF_PRODUCT``/``ASF_JOB``/``BACKLOG_ID_RANGE`` — set, not inherited), ``home`` to point
``HOME`` somewhere else (a test's temp home; a worker account's own).
"""
import os

#: The variables that name the caller — the tick's product, a session's job and mint range.
#: None may reach a child that is not that caller: the gate is the branch's result, a worker's
#: session its own.
CALLER_IDENTITY = ('ASF_PRODUCT', 'ASF_JOB', 'BACKLOG_ID_RANGE')

#: What a git hook exports; a ``git`` child that inherits them ignores its ``cwd``.
GIT_HOOK = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')

DEFAULT_TRUNK = 'main'


def package_parent():
    """The directory the running ``asf`` package is imported from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def build(base=None, worktree=None, identity=None, home=None, trunk=DEFAULT_TRUNK,
          pythonpath=True):
    """The environment for a child of ASF.

    ``base``: the environment to start from (default the process's own). ``worktree``: a
    checkout whose own code must win — first on ``PYTHONPATH``. ``identity``: a worker's own
    ``{ASF_PRODUCT, ASF_JOB, BACKLOG_ID_RANGE, …}``, set after the inherited ones are gone.
    ``home``: ``HOME`` for the child. ``trunk``: the branch name pinned as
    ``init.defaultBranch``. ``pythonpath=False`` leaves ``PYTHONPATH`` as the base had it."""
    env = dict(os.environ if base is None else base)
    for var in GIT_HOOK + CALLER_IDENTITY:
        env.pop(var, None)
    _git_config(env, [('init.defaultBranch', trunk or DEFAULT_TRUNK)])
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
