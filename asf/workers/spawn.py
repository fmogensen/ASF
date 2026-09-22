"""asf.workers.spawn — one job: worktree, id range, brief, run, ledger line.

``spawn(product, row, account, brief_text)``:

1. a worktree ``~/.ASF/state/<product>/worktrees/<job>`` on a new branch (the row's, else
   ``<branch prefix for the kind>/<job>``) off ``origin/<main>`` of ``Product.repo_dir`` — an
   ended run's worktree on that branch, or a branch already on origin (a held branch sent back
   for another round, ``correct`` or ``adjudicate`` alike), is reused instead, rebased onto
   ``origin/<main>``; only a live run's worktree refuses (:func:`make_worktree`);
2. an id range reserved for the job in ``~/.ASF/state/<product>/id-ranges.tsv`` and handed to
   the session as ``BACKLOG_ID_RANGE`` (so parallel writers never mint the same id — see
   ``asf.record.ids``);
3. the brief written to ``~/.ASF/state/<product>/briefs/<job>.md`` (a ``fix-bug`` brief gets the
   named-test header harvest checks for);
4. ``Runtime.run`` with ``--add-dir`` per entry of the product yaml's ``job_grants``;
5. one line in ``sessions.jsonl``: job, item, feature, kind, account, model, pid, worktree,
   branch, started.
"""
import fcntl
import os
import re
import subprocess

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

DEFAULT_ID_PREFIXES = ['S', 'T', 'B']
DEFAULT_ID_START = 5000
DEFAULT_ID_SIZE = 50


class SpawnError(Exception):
    pass


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


def _repo_lock_path(repo):
    return os.path.join(repo, '.git', 'asf-worktree.lock')


def _with_repo_lock(repo, fn):
    """Serialises worktree creation per repo (B-0016): ``git worktree add`` and the branch
    create it does along the way both write ``.git/config``, and parallel spawns racing on
    that file killed jobs before their first turn. One retry absorbs a lock still held by
    something outside this lock (a plain concurrent ``git`` call) once it clears; ``fn`` must
    be safe to call twice."""
    lock_path = _repo_lock_path(repo)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, 'w') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                return fn()
            except SpawnError as e:
                if 'lock config file' not in str(e):
                    raise
                return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def make_worktree(product, job, branch):
    """The worktree a run starts in — :func:`asf.workers.lifecycle.may_launch` decides whether
    one that already exists may be taken over.

    A worktree already holding ``branch`` (this job's own, or an ended job's that a correction on
    the same branch follows) is reused as it stands — its tree and its branch, rebased onto the
    fetched trunk (a conflict is left in place for the session to resolve) — as long as the run
    that recorded it has ended; a live run's worktree is never touched (B-0025, B-0051). A branch
    already on origin with no worktree left (any row kind: a held branch sent back for another
    round, B-0046, B-0048) gets a worktree on it, rebased the same way. Otherwise a fresh branch
    off ``origin/<main>``. Returns the worktree path."""
    repo = product.repo_dir
    if not repo or not os.path.isdir(repo):
        raise SpawnError(f'product repo_dir missing: {repo!r}')
    registry = pool_mod.sessions_path(product)
    path = os.path.join(worktrees_dir(product), job)

    def add():
        _git(['fetch', '-q', 'origin', product.main], repo)
        held = _holding_worktree(repo, branch)
        for candidate in dict.fromkeys(p for p in (path, held) if p and os.path.exists(p)):
            ok, why = lifecycle.may_launch(registry, job, candidate)
            if not ok:
                raise SpawnError(why)
            if candidate == path or _worktree_branch(candidate) == branch:
                subprocess.run(['git', 'rebase', '-q', f'origin/{product.main}'], cwd=candidate,
                               capture_output=True, text=True)
                return candidate
        if _branch_exists_on_origin(repo, branch):
            _git(['fetch', '-q', 'origin', branch], repo)
            if held:
                # a stale worktree of an ended run still holds the branch and is not reusable here
                _git(['worktree', 'remove', '--force', held], repo)
            _git(['worktree', 'add', '-q', '-B', branch, path, f'origin/{branch}'], repo)
            subprocess.run(['git', 'rebase', '-q', f'origin/{product.main}'], cwd=path,
                           capture_output=True, text=True)
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

    return _with_repo_lock(repo, add)


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


def spawn(product, row, account, brief_text, runtime=None, cfg=None):
    """Launch one row on ``account``. Returns the session record written to the ledger."""
    cfg = load_cfg() if cfg is None else cfg
    wp = cfg.get('worker_pool') or {}
    runtime = runtime or runtime_mod.from_config(cfg)
    model = model_arg(row.model, cfg)
    branch = branch_for(product, row)
    worktree = make_worktree(product, row.job, branch)
    id_range = reserve_id_range(product, row.job,
                                prefixes=wp.get('id_range_prefixes') or DEFAULT_ID_PREFIXES,
                                start=int(wp.get('id_range_start', DEFAULT_ID_START)),
                                size=int(wp.get('id_range_size', DEFAULT_ID_SIZE)))
    brief_path = write_brief(product, row.job, brief_for(row, brief_text))
    add_dirs = [os.path.expanduser(d) for d in (product._get('job_grants') or [])]
    job = runtime_mod.Job(product.name, row.job, worktree, brief_path, model,
                          account=account, add_dirs=add_dirs,
                          permission_mode=wp.get('permission_mode')
                          or runtime_mod.DEFAULT_PERMISSION_MODE,
                          env={'BACKLOG_ID_RANGE': id_range},
                          settings_file=settings_file(wp))
    result = runtime.run(job)
    record = {'job': row.job, 'item': row.item, 'feature': row.feature, 'kind': row.kind,
              'account': account.name if account else None, 'model': job.model,
              'pid': result.pid, 'worktree': worktree, 'branch': branch,
              'started': pool_mod.now_iso(), 'log': result.log_path, 'brief': brief_path,
              'id_range': id_range, 'runtime': runtime.name}
    # a launch line is a new run: the fold opens a run at every launch line, so the previous
    # run's terminal fields never reach this one (B-0041 — see asf.workers.lifecycle)
    pool_mod.append_session(product, record)
    return record


def load_cfg():
    try:
        return env.load_config()
    except env.ConfigError:
        return {}
