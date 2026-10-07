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
``home`` to point ``HOME`` somewhere else (a test's temp home; a worker account's own) — and
with it the caller's runtime config dir (:data:`RUNTIME_CONFIG_DIR`) is dropped: it lives under
the caller's home, and a child given a home of its own that kept it wrote its runtime settings
into the caller's (the install e2e's ``plugin marketplace add`` landed in every worker
account's live settings file).

Two modes. ``gate`` (the default — the harvest gate, the suite) starts from the whole base
environment and removes what is listed above: a product's tests may need the machine's tools.
``worker`` (a worker session and its worktree setup command) starts from nothing and keeps only
:data:`WORKER_ALLOW` (plus any ``LC_*``) and the names ``worker_pool.env_passthrough`` lists: an
operator's token, cloud credential or agent socket in the tick's environment never reaches a
session unless the operator names it.
"""
import atexit
import glob
import os
import pwd
import re
import sys
import tempfile

#: The variables that name the caller — the tick's product, a session's job, its session id and
#: mint range. None may reach a child that is not that caller: the gate is the branch's result, a
#: worker's session its own.
CALLER_IDENTITY = ('ASF_PRODUCT', 'ASF_JOB', 'ASF_SESSION', 'BACKLOG_ID_RANGE', 'ASF_ITEM',
                   'ASF_ITEM_KIND', 'ASF_SIGNOFF')

#: What a git hook exports; a ``git`` child that inherits them ignores its ``cwd``, and under a
#: ``pre-push`` quarantine writes its objects where they are discarded. The list is
#: :mod:`asf.gitpush`'s, which has been the complete one in production (F-0013 D4).
GIT_HOOK = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_PREFIX', 'GIT_OBJECT_DIRECTORY',
            'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_QUARANTINE_PATH')

#: The suite's hooks-dir confine (F-0143): an absolute root outside which
#: :func:`asf.hooks.git_hooks_dir` refuses to resolve. Set by both suite entry points to the temp
#: root; **unset in every operator install**, where the branch it drives is one comparison that
#: never fires.
#:
#: review-b-0111: a fixture ``pre-commit`` naming a nonexistent ``/x/asf`` reached a hooks dir a
#: worker's commit ran, and refused it. On a factory host ``core.hooksPath`` makes that one
#: directory shared by every worktree and every lane, so a single stray file stops other sessions.
#: The four tests that caused it were fixed one by one (``3d0d0bb8d``); this is the rail that makes
#: the fifth impossible, and it constrains the *resolution* because a hand-built path cannot fake
#: that step (D1).
#:
#: It is an environment variable, not a monkeypatch, because the tests most likely to install hooks
#: run ``asf`` as a subprocess and only the environment crosses that boundary (D2). ``gate`` mode
#: carries it; ``worker`` mode strips it with every other unlisted name, and stays unconfined (D8).
HOOKS_CONFINE = 'ASF_HOOKS_CONFINE'


def hooks_confine(env=None):
    """The confine root in ``env`` (default the process's), ``realpath``'d — or None when unset.

    Resolved, because the temp root is reached through a symlink on some platforms while
    ``mkdtemp`` hands back the unresolved form: comparing raw strings passes by luck and refuses a
    legitimate temp repo the moment either side is resolved (D4)."""
    root = (os.environ if env is None else env).get(HOOKS_CONFINE)
    return os.path.realpath(root) if root else None


def confine_hooks_to_temp(env=None):
    """Point :data:`HOOKS_CONFINE` at the temp root, and return it. Called by **both** suite entry
    points — ``tests/__init__.py`` and its twin ``tests/test_00_home.py``, which cannot import each
    other (D6, D7). ``tests/test_hermetic.py`` proves neither is missing it."""
    env = os.environ if env is None else env
    env[HOOKS_CONFINE] = tempfile.gettempdir()
    return env[HOOKS_CONFINE]


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

#: The variable that moves the agent runtime's user config dir off ``HOME``. It is the caller's
#: own — a worker session's is its account's — so a child :func:`build` gives a ``home`` of its
#: own never keeps it: the child's runtime then writes under that home. A caller that means a
#: child to use an account's config dir sets it after :func:`build`
#: (:func:`asf.workers.runtime.build_env`).
RUNTIME_CONFIG_DIR = 'CLAUDE_CONFIG_DIR'


def runtime_config_dir_owned(environ):
    """True when ``environ``'s :data:`RUNTIME_CONFIG_DIR` is unset or sits under its ``HOME``.
    A config dir outside the HOME is a caller's that a child moved away from (a test's temp
    HOME inheriting a worker session's config dir): the runtime must not write there."""
    ccd, home = environ.get(RUNTIME_CONFIG_DIR), environ.get('HOME')
    if not ccd:
        return True
    if not home:
        return False
    ccd, home = os.path.realpath(os.path.expanduser(ccd)), os.path.realpath(home)
    return ccd == home or ccd.startswith(home.rstrip(os.sep) + os.sep)


def runtime_env(environ=None):
    """``environ`` (default ``os.environ``) for a runtime CLI child: the config dir dropped
    unless :func:`runtime_config_dir_owned` — so the child writes under the run's own HOME."""
    out = dict(os.environ if environ is None else environ)
    if not runtime_config_dir_owned(out):
        out.pop(RUNTIME_CONFIG_DIR, None)
    return out


def package_parent():
    """The directory the running ``asf`` package is imported from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git_env(base=None):
    """``base`` (default the caller's) minus every variable a git hook exports. The whole
    environment for a ``git`` child — :func:`build` is for a child that runs *code*."""
    env = dict(os.environ if base is None else base)
    for var in GIT_HOOK:
        env.pop(var, None)
    return env


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
        env.pop(RUNTIME_CONFIG_DIR, None)  # the caller's home's, never the child's
    for key, value in (identity or {}).items():
        if value is not None:
            env[key] = str(value)
    return env


# ---- the suite's guard over the operator's own files -------------------------------

#: Names the operator's real home for :func:`guard_operator_files` — set only by a test of the
#: guard itself; otherwise the home is the user's passwd entry, never ``HOME`` (a test moves it).
OPERATOR_HOME_VAR = 'ASF_SUITE_OPERATOR_HOME'

#: The exit status a suite process ends with when it wrote into an operator file.
LEAK_EXIT = 3

_guard = {}


def operator_home():
    return os.environ.get(OPERATOR_HOME_VAR) or pwd.getpwuid(os.getuid()).pw_dir


def _under(home, path):
    path = str(path)
    if path == '~' or path.startswith('~/'):
        path = home + path[1:]
    return os.path.join(home, path) if not os.path.isabs(path) else path


#: A ``config_dir:`` or ``home:`` line of the operator config, at any depth. Read by a pattern, not
#: :mod:`asf.env`: the guard runs before the suite has moved ``ASF_HOME``, and importing
#: :mod:`asf.env` then would bind it to the operator's. A line that is not an account's only
#: widens what is watched, which is harmless: a file is flagged only when the suite wrote into it.
ACCOUNT_DIR_RE = re.compile(r'^\s*(?:-\s*)?(config_dir|home):\s*([\'"]?)([^\s#\'"]+)\2\s*(?:#.*)?$',
                            re.M)


def _account_dirs(config):
    try:
        with open(config, encoding='utf-8') as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    return [(m.group(1), m.group(3)) for m in ACCOUNT_DIR_RE.finditer(text)]


#: A product file's ``repo_dir:`` / ``backlog_dir:`` line — read by a pattern for the same reason
#: as :data:`ACCOUNT_DIR_RE`.
PRODUCT_DIR_RE = re.compile(r'^(repo_dir|backlog_dir):\s*([\'"]?)([^\s#\'"]+)\2\s*(?:#.*)?$', re.M)


def _product_repo_configs(products):
    """``<dir>/.git/config`` of every ``repo_dir`` and ``backlog_dir`` the product files name."""
    out = set()
    for path in products:
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for m in PRODUCT_DIR_RE.finditer(text):
            out.add(os.path.join(os.path.expanduser(m.group(3)), '.git', 'config'))
    return out


def operator_files(home=None):
    """The operator's own files a suite must never write: the operator config and every product
    file under ``<home>/.ASF``, the runtime's user settings under ``<home>``, the settings file in
    every worker account's ``config_dir`` (and ``home``) that config names, and the git config of
    every product repo and record (F-0281: a test's ``user.name=Test`` once landed in a real
    record's ``.git/config``) — paths read from the config itself, so nothing here knows where an
    operator keeps their accounts or their repos."""
    home = home or operator_home()
    asf_home = os.path.join(home, '.ASF')
    config = os.path.join(asf_home, 'config.yaml')
    out = {config, os.path.join(home, '.claude', 'settings.json')}
    products = glob.glob(os.path.join(asf_home, 'products', '*.yaml'))
    out.update(products)
    for key, value in _account_dirs(config):
        if key == 'config_dir':
            out.add(os.path.join(_under(home, value), 'settings.json'))
        else:
            out.add(os.path.join(_under(home, value), '.claude', 'settings.json'))
    out.update(_product_repo_configs(products))
    return sorted(out)


#: The git identity the suite commits as (:func:`suite_git_identity`) — and so the mark a git
#: config the suite wrote into carries.
SUITE_GIT_NAME = 'Test'
SUITE_GIT_EMAIL = 'test@example.com'


def suite_git_identity(root, environ=None):
    """Point git's *global* config at a file the suite owns (``<root>/gitconfig``), holding
    :data:`SUITE_GIT_NAME` <:data:`SUITE_GIT_EMAIL`>, and drop the system config (F-0281). A
    fixture then needs no ``git config user.*`` of its own — the call that, run with a cwd that
    resolved to a real repo, once wrote the suite's identity into an operator's record — and no
    test ever reads the caller's global config. Called by both suite entry points. Returns the
    file's path."""
    environ = os.environ if environ is None else environ
    path = os.path.join(root, 'gitconfig')
    os.makedirs(root, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'[user]\n\tname = {SUITE_GIT_NAME}\n\temail = {SUITE_GIT_EMAIL}\n'
                '[safe]\n\tdirectory = *\n')
    environ['GIT_CONFIG_GLOBAL'] = path
    environ['GIT_CONFIG_NOSYSTEM'] = '1'
    return path


def _identity_marks(path, data):
    """A git config holding the suite's identity is marked as the suite's: it names no temp
    path, so :func:`_suite_paths` alone would never blame it."""
    if not data or not str(path).endswith(os.path.join('.git', 'config')):
        return set()
    return {b'identity:' + SUITE_GIT_EMAIL.encode()} if SUITE_GIT_EMAIL.encode() in data else set()


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError:
        return None


def suite_markers():
    """What a path the suite made starts with: this process's temp dir (both spellings) and the
    suite's own ``ASF_HOME``."""
    tmp = tempfile.gettempdir()
    marks = {tmp, os.path.realpath(tmp)}
    if os.environ.get('ASF_HOME'):
        marks.add(os.environ['ASF_HOME'])
    return sorted(m for m in marks if m and m != os.sep)


def _suite_paths(data, marks):
    """Every path in ``data`` (bytes) that starts with one of ``marks``."""
    if not data:
        return set()
    out = set()
    for mark in marks:
        out.update(re.findall(re.escape(mark.encode()) + rb'[^\s"\',;)]*', data))
    return out


def snapshot_operator_files(home=None):
    """``{path: (content, suite paths it names)}`` for every :func:`operator_files` path."""
    marks = suite_markers()
    return {p: (data, _suite_paths(data, marks) | _identity_marks(p, data))
            for p in operator_files(home) for data in [_read(p)]}


def leaked_files(before, home=None):
    """The operator files (:func:`operator_files`) whose content changed since ``before`` (a
    :func:`snapshot_operator_files`) *and* gained a path under a :func:`suite_markers` root: the
    suite wrote it. One the live factory changed meanwhile gains none, and one that already
    named such a path (an earlier leak) is not blamed on this run."""
    marks = suite_markers()
    out = []
    for path in sorted(set(before) | set(operator_files(home))):
        old, old_paths = before.get(path, (None, set()))
        data = _read(path)
        gained = (_suite_paths(data, marks) | _identity_marks(path, data)) - old_paths
        if data is not None and data != old and gained:
            out.append(path)
    return out


def _check_guard():
    leaks = leaked_files(_guard['before'], _guard['home'])
    if leaks:
        sys.stderr.write('hermetic: the suite wrote into the operator\'s own files: '
                         + ', '.join(leaks) + '\n')
        sys.stderr.flush()
        os._exit(LEAK_EXIT)


def guard_operator_files():
    """Snapshot :func:`operator_files` now and check them as the process exits: a suite process
    that wrote a path of its own into one ends with :data:`LEAK_EXIT`, whatever its tests said.
    Called first by both of the suite's entry points (``tests/__init__.py``,
    ``tests/test_00_home.py``); a second call in one process is a no-op. Registered before the
    temp home's own cleanup, so it runs after it."""
    if _guard:
        return
    home = operator_home()
    _guard.update(home=home, before=snapshot_operator_files(home))
    atexit.register(_check_guard)


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
