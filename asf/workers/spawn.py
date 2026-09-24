"""asf.workers.spawn — one job: worktree, id range, brief, run, ledger line.

``spawn(product, row, account, brief_text)``:

1. the product repo's git push gate confirmed in place (:func:`asf.hooks.ensure_git_hooks`) —
   before any worktree is touched, so no session is ever launched into a repo with no gate
   (F-0075, D10) — then a worktree ``~/.ASF/state/<product>/worktrees/<job>`` on a new branch
   (the row's, else ``<branch prefix for the kind>/<job>``) off ``origin/<main>`` of
   ``Product.repo_dir`` — an ended run's worktree on that branch, or a branch already on origin
   (a held branch sent back for another round, ``correct`` or ``adjudicate`` alike), is reused
   instead, rebased onto ``origin/<main>``; only a live run's worktree refuses
   (:func:`make_worktree`); a *fresh* worktree then runs the product's
   ``conventions.worktree_setup`` under the session's own environment
   (:func:`run_worktree_setup`) — a failure refuses the launch and removes the worktree;
2. an id range reserved for the job in ``~/.ASF/state/<product>/id-ranges.tsv`` and handed to
   the session as ``BACKLOG_ID_RANGE`` (so parallel writers never mint the same id — see
   ``asf.record.ids``);
3. the brief written to ``~/.ASF/state/<product>/briefs/<job>.md`` (a ``fix-bug`` brief gets the
   named-test header harvest checks for);
4. ``Runtime.run`` with ``--add-dir`` per entry of the product yaml's ``job_grants``, its
   environment carrying the session's id (``ASF_SESSION``, minted from ``started`` before the
   run) and its ``hooks_dir`` (:func:`asf.workers.githooks.ensure`), so every commit it makes
   carries an ``ASF-Session`` trailer (F-0076);
5. one line in ``sessions.jsonl``: job, item, feature, kind, account, model, pid, worktree,
   branch, started, session, product.
"""
import os
import re
import subprocess
import time

from asf import env
from asf import hooks
from asf.workers import githooks
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

DEFAULT_ID_PREFIXES = ['S', 'T', 'B']
DEFAULT_ID_START = 5000
DEFAULT_ID_SIZE = 50


class SpawnError(Exception):
    """A launch refused. ``clear`` is the one command that clears it, when there is one."""

    def __init__(self, msg, clear=''):
        super().__init__(msg)
        self.clear = clear


class WorktreeBusy(SpawnError):
    """The worktree is held by a live run: the item is already at work — a wait, not a fault."""


def _git(args, cwd):
    p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SpawnError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


# ---- id ranges --------------------------------------------------------------

def id_ranges_path(product):
    return os.path.join(env.state_dir(product), 'id-ranges.tsv')


def _read_ranges(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if len(parts) >= 2 and parts[0] and not parts[0].startswith('#'):
                    rows.append((parts[0], parts[1]))
    return rows


def reserve_id_range(product, job, prefixes=None, start=DEFAULT_ID_START, size=DEFAULT_ID_SIZE):
    """The job's ``BACKLOG_ID_RANGE`` (``S:5000-5049,T:5000-5049``): reused if the job already
    holds one, else the next free block per prefix after every block the tsv has handed out."""
    prefixes = prefixes or DEFAULT_ID_PREFIXES
    path = id_ranges_path(product)
    rows = _read_ranges(path)
    for j, rng in rows:
        if j == job:
            return rng
    top = {}
    for _j, rng in rows:
        for m in re.finditer(r'([A-Z]):(\d+)-(\d+)', rng):
            top[m.group(1)] = max(top.get(m.group(1), -1), int(m.group(3)))
    parts = []
    for p in prefixes:
        lo = max(start, top.get(p, start - 1) + 1)
        parts.append(f'{p}:{lo:04d}-{lo + size - 1:04d}')
    rng = ','.join(parts)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'{job}\t{rng}\t{pool_mod.now_iso()}\n')
    return rng


def release_id_range(product, job):
    """Drop ``job``'s row from ``id-ranges.tsv``. Called once nothing can mint against the
    range any more (the worktree is gone — see ``asf.workers.health``), so a finished job does
    not hold its block forever. Returns whether a row was actually dropped."""
    path = id_ranges_path(product)
    if not os.path.exists(path):
        return False
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    kept = [ln for ln in lines if ln.split('\t', 1)[0] != job]
    if len(kept) == len(lines):
        return False
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(kept)
    return True


# ---- worktree + brief -------------------------------------------------------

def worktrees_dir(product):
    d = os.path.join(env.state_dir(product), 'worktrees')
    os.makedirs(d, exist_ok=True)
    return d


def briefs_dir(product):
    d = os.path.join(env.state_dir(product), 'briefs')
    os.makedirs(d, exist_ok=True)
    return d


def branch_for(product, row):
    return row.branch or f'{product.branch_prefix(row.kind)}/{row.job}'


def _holding_worktree(repo, branch):
    out = _git(['worktree', 'list', '--porcelain'], repo)
    path = None
    for line in out.splitlines():
        if line.startswith('worktree '):
            path = line[len('worktree '):]
        elif line == f'branch refs/heads/{branch}':
            return path
    return None


def _branch_exists_on_origin(repo, branch):
    """The check a held branch is reused on: not the row's kind (B-0048 — an ADJUDICATE row's
    kind is ``adjudicate``, not ``correct``, so keying on kind alone missed it and spawned it
    fresh off main, silently losing the branch's own history) but whether ``branch`` is already
    a ref on origin."""
    return bool(_git(['ls-remote', '--heads', 'origin', branch], repo).strip())


def make_worktree(product, job, branch):
    """The worktree a run starts in — :func:`asf.workers.lifecycle.may_launch` decides whether
    one that already exists may be taken over.

    A worktree already holding ``branch`` (this job's own, or an ended job's that a correction on
    the same branch follows) is reused as it stands — its tree and its branch, rebased onto the
    fetched trunk (a conflict is left in place for the session to resolve) — as long as the run
    that recorded it has ended; a live run's worktree is never touched (B-0025, B-0051). A branch
    already on origin with no worktree left (any row kind: a held branch sent back for another
    round, B-0046, B-0048) gets a worktree on it, rebased the same way. Otherwise a fresh branch
    off ``origin/<main>``. Returns the worktree path.

    Before any of that, :func:`asf.hooks.ensure_git_hooks` confirms the product repo's push gate
    is in place — missing hooks are written, a foreign one refuses the whole launch (F-0075,
    D10) — so no path below can reach ``git worktree add`` without it."""
    repo = product.repo_dir
    if not repo or not os.path.isdir(repo):
        raise SpawnError(f'product repo_dir missing: {repo!r}')
    ok, detail = hooks.ensure_git_hooks(product)
    if not ok:
        raise SpawnError(detail)
    registry = pool_mod.sessions_path(product)
    path = os.path.join(worktrees_dir(product), job)
    _git(['fetch', '-q', 'origin', product.main], repo)
    held = _holding_worktree(repo, branch)
    if held and lifecycle.path_key(held) == lifecycle.path_key(path):
        held = path  # one directory, spelled ~/.ASF by git and ~/.asf by us (or the reverse)
    for candidate in dict.fromkeys(p for p in (path, held) if p and os.path.exists(p)):
        what, why = lifecycle.launch_verdict(registry, job, candidate)
        if what == lifecycle.BUSY:
            raise WorktreeBusy(why)
        if what:
            raise SpawnError(why, clear=f'git -C {repo} worktree remove --force {candidate}'
                                        f'  # after checking nothing in it is wanted')
        if candidate == path and _worktree_branch(candidate) not in (branch, 'HEAD', ''):
            # another branch checked out (a detached or mid-rebase tree is left for the session)
            _checkout_branch(candidate, branch, product.main)
        if candidate == path or _worktree_branch(candidate) == branch:
            _rebase_onto_trunk(candidate, branch, product.main)
            return candidate
    if _branch_exists_on_origin(repo, branch):
        _git(['fetch', '-q', 'origin', branch], repo)
        if held:
            # a stale worktree of an ended run still holds the branch and is not reusable here
            _git(['worktree', 'remove', '--force', held], repo)
        _git(['worktree', 'add', '-q', '-B', branch, path, f'origin/{branch}'], repo)
        _rebase_onto_trunk(path, branch, product.main)
        return path
    if held:
        _git(['worktree', 'remove', '--force', held], repo)
        _git(['branch', '-D', branch], repo)
    elif _local_branch_exists(repo, branch):
        # a branch with no worktree and not on origin (a reaped one): reused when it carries
        # nothing, refused with the count when it does (B-0025) — never silently reset
        ahead = _git(['rev-list', '--count', f'origin/{product.main}..{branch}'], repo)
        if ahead not in ('', '0'):
            raise SpawnError(f'branch {branch} exists locally with {ahead} commit(s) not on '
                             f'origin/{product.main} and no worktree — look before relaunching')
        _git(['branch', '-D', branch], repo)
    _git(['worktree', 'add', '-q', '-b', branch, path, f'origin/{product.main}'], repo)
    return path


def _rebase_onto_trunk(path, branch, main):
    """Rebase the worktree onto the fetched trunk. A conflict is left in place for the session.
    A rebase that completes and moves a branch already on origin is published by the factory at
    once (:func:`asf.workers.lifecycle.publish`, B-0056): the session then starts on a branch
    that origin holds, and its own pushes are fast-forwards — it never faces the non-fast-forward
    that made sessions merge their stale remote."""
    ls = subprocess.run(['git', 'ls-remote', '--heads', 'origin', branch], cwd=path,
                        capture_output=True, text=True)
    remote_sha = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''
    r = subprocess.run(['git', 'rebase', '-q', f'origin/{main}'], cwd=path,
                       capture_output=True, text=True)
    if r.returncode != 0 or not remote_sha:
        return
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=path, capture_output=True,
                          text=True).stdout.strip()
    if head and head != remote_sha:
        lifecycle.publish(path, branch, remote_sha, main=main)


def _checkout_branch(path, branch, main):
    """An ended run's worktree reused for ``branch`` when it has another one checked out: the
    branch as it stands locally, else origin's, else a fresh one off the trunk. A checkout the
    tree refuses (uncommitted work in the way) refuses the launch with what to do."""
    if _local_branch_exists(path, branch):
        args = ['checkout', '-q', branch]
    elif _branch_exists_on_origin(path, branch):
        subprocess.run(['git', 'fetch', '-q', 'origin', branch], cwd=path, capture_output=True)
        args = ['checkout', '-q', '-B', branch, f'origin/{branch}']
    else:
        args = ['checkout', '-q', '-b', branch, f'origin/{main}']
    p = subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)
    if p.returncode != 0:
        raise SpawnError(f'worktree {path} holds {_worktree_branch(path)} and will not check out '
                         f'{branch}: {p.stderr.strip()}',
                         clear=f'git -C {path} status  # commit or discard, then relaunch')


def _local_branch_exists(repo, branch):
    p = subprocess.run(['git', 'rev-parse', '--verify', '-q', f'refs/heads/{branch}'], cwd=repo,
                       capture_output=True, text=True)
    return p.returncode == 0


def _worktree_branch(path):
    p = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=path,
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def brief_for(row, brief_text):
    """The brief as written: a ``fix-bug`` brief leads with the kind and the test harvest will
    require, so the session knows the fix lands only with that test."""
    if row.kind != 'fix-bug':
        return brief_text
    test = row.test or '(name the failing test you add, in your report)'
    head = (f'Kind: fix-bug — {row.item}' + (f' ({row.severity})' if row.severity else '') + '\n'
            f'Harvest requires the named test: {test}\n'
            'Write the failing test first, then the fix.\n\n')
    return head + brief_text


def write_brief(product, job, text):
    path = os.path.join(briefs_dir(product), f'{job}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def model_arg(model, cfg=None):
    """``worker_pool.models: {Opus: <id>}`` maps a row's model label. A label with no entry is
    refused — the literal label is not a model id the runtime knows, and the session dies at once."""
    if not model:
        return None
    table = (((cfg or {}).get('worker_pool') or {}).get('models')) or {}
    if not isinstance(table, dict):  # a misshapen value is no entry, never a TypeError
        table = {}
    if model not in table:
        raise SpawnError(f'NEEDS OPERATOR: worker_pool.models has no entry for {model} '
                         '— add it to config.yaml')
    return table[model]


# ---- spawn ------------------------------------------------------------------

def settings_file(wp):
    """``worker_pool.settings_file`` expanded, or None. Refuses a path that does not exist: a
    worker launched without its deny rules is worse than no worker."""
    raw = wp.get('settings_file')
    if not raw:
        return None
    path = os.path.expanduser(raw)
    if not os.path.isfile(path):
        raise FileNotFoundError(f'worker_pool.settings_file not found: {path}')
    return path


#: How long ``conventions.worktree_setup`` may run in a fresh worktree before the launch is
#: refused.
WORKTREE_SETUP_TIMEOUT_S = 900


def setup_log_path(product, job):
    return os.path.join(briefs_dir(product), f'{job}.setup.log')


def run_worktree_setup(product, job, worktree, account=None, passthrough=(),
                       timeout=WORKTREE_SETUP_TIMEOUT_S):
    """Run the product's ``conventions.worktree_setup`` (a shell command) in the fresh
    ``worktree``, under the environment the session itself will have
    (:func:`asf.workers.runtime.build_env`, worker mode: the allow-list, the account's HOME).
    Its output goes to ``briefs/<job>.setup.log``. Returns the seconds it took, or None when the
    product declares no command. A failure or a timeout removes the worktree — a fresh one
    without its setup is never handed over, nor reused as if it had one — and raises
    :class:`SpawnError` naming the command and its first line of error output."""
    command = getattr(product.conventions, 'worktree_setup', None)
    if not command:
        return None
    runtime_mod.seed_home(account)
    product_auth_env = env.product_auth_env(product)
    job_env = runtime_mod.build_env(runtime_mod.Job(product.name, job, worktree, None, None,
                                                    account=account, passthrough=passthrough,
                                                    product_auth_env=product_auth_env))
    secrets = runtime_mod.auth_env_values(account, product_auth_env)
    log = setup_log_path(product, job)
    started = time.monotonic()
    why = None
    try:
        try:
            p = subprocess.run(command, shell=True, cwd=worktree, env=job_env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
            stdout, stderr = p.stdout or b'', p.stderr or b''
        except subprocess.TimeoutExpired as e:
            p, stdout, stderr = None, e.stdout or b'', e.stderr or b''
        # the log never carries an auth_env value, whatever the command printed
        stderr_text = runtime_mod.mask(stderr.decode('utf-8', 'replace'), secrets)
        with open(log, 'w', encoding='utf-8') as out:
            out.write(runtime_mod.mask(stdout.decode('utf-8', 'replace'), secrets) + stderr_text)
        if p is None:
            raise subprocess.TimeoutExpired(command, timeout)
        if p.returncode != 0:
            lines = [l for l in stderr_text.splitlines() if l.strip()]
            why = f'exit {p.returncode}' + (f': {lines[0].strip()}' if lines else '')
    except subprocess.TimeoutExpired:
        why = f'timed out after {timeout}s'
    if why is None:
        return round(time.monotonic() - started, 1)
    subprocess.run(['git', 'worktree', 'remove', '--force', worktree], cwd=product.repo_dir,
                   capture_output=True)
    raise SpawnError(f'worktree_setup `{command}` failed in {worktree} ({why}) — log: {log}',
                     clear=f'fix `{command}` (conventions.worktree_setup in '
                           f'products/{product.name}.yaml), then relaunch')


def spawn(product, row, account, brief_text, runtime=None, cfg=None):
    """Launch one row on ``account``. Returns the session record written to the ledger."""
    cfg = load_cfg() if cfg is None else cfg
    wp = cfg.get('worker_pool') or {}
    passthrough = env.env_passthrough(cfg)
    runtime = runtime or runtime_mod.from_config(cfg)
    product_auth_env = env.product_auth_env(product)
    try:  # the account's credential files, read before anything is made: a refusal leaves nothing
        runtime_mod.auth_env_values(account, product_auth_env)
    except runtime_mod.AuthEnvError as e:
        raise SpawnError(str(e), clear=e.clear) from None
    model = model_arg(row.model, cfg)
    branch = branch_for(product, row)
    own_path = os.path.join(worktrees_dir(product), row.job)
    fresh = not os.path.exists(own_path)
    worktree = make_worktree(product, row.job, branch)
    setup_s = None
    if fresh and worktree == own_path:  # a new worktree, not an ended run's reused one
        setup_s = run_worktree_setup(product, row.job, worktree, account, passthrough)
    id_range = reserve_id_range(product, row.job,
                                prefixes=wp.get('id_range_prefixes') or DEFAULT_ID_PREFIXES,
                                start=int(wp.get('id_range_start', DEFAULT_ID_START)),
                                size=int(wp.get('id_range_size', DEFAULT_ID_SIZE)))
    brief_path = write_brief(product, row.job, brief_for(row, brief_text))
    add_dirs = [os.path.expanduser(d) for d in (product._get('job_grants') or [])]
    for d in getattr(row, 'add_dirs', None) or ():  # the row's own grants are the factory's dirs
        d = os.path.expanduser(d)
        os.makedirs(d, exist_ok=True)
        if d not in add_dirs:
            add_dirs.append(d)
    started = pool_mod.now_iso()
    sid = lifecycle.session_id(product.name, row.job, started)
    hooks_dir = githooks.ensure(product)
    job = runtime_mod.Job(product.name, row.job, worktree, brief_path, model,
                          account=account, add_dirs=add_dirs,
                          permission_mode=wp.get('permission_mode')
                          or runtime_mod.DEFAULT_PERMISSION_MODE,
                          env={'BACKLOG_ID_RANGE': id_range, 'ASF_SESSION': sid},
                          settings_file=settings_file(wp), hooks_dir=hooks_dir,
                          passthrough=passthrough, product_auth_env=product_auth_env)
    result = runtime.run(job)
    record = {'job': row.job, 'item': row.item, 'feature': row.feature, 'kind': row.kind,
              'account': account.name if account else None, 'model': job.model,
              'pid': result.pid, 'pgid': result.pid, 'worktree': worktree, 'branch': branch,
              'started': started, 'log': result.log_path, 'brief': brief_path,
              'id_range': id_range, 'runtime': runtime.name, 'session': sid,
              'product': product.name}
    if setup_s is not None:
        record['setup_s'] = setup_s
    # a launch line is a new run: the fold opens a run at every launch line, so the previous
    # run's terminal fields never reach this one (B-0041 — see asf.workers.lifecycle)
    pool_mod.append_session(product, record)
    return record


def load_cfg():
    try:
        return env.load_config()
    except env.ConfigError:
        return {}
