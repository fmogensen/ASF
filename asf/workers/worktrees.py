"""asf.workers.worktrees — the worktree reaper: ended sessions' worktrees go, with a cap.

Every worker worktree under ``~/.ASF/state/<product>/worktrees/`` carries a full checkout and,
for a product that installs dependencies there, its own dependency tree — gigabytes each. The
health pass (:mod:`asf.workers.health`) reaps only a worktree whose work has reached the trunk;
one whose branch is pushed and waiting for review, harvest or a correction stays until then, and
they pile up. This pass removes those too, under one rule — nothing in a removed worktree exists
only there:

* **live** — a run on the ledger with no ``ended`` line yet, or whose pid still answers, is at
  work (or not yet judged by health): never touched;
* **keep** (with the reason) — uncommitted or untracked files, a rebase or merge in progress,
  commits that are neither on ``origin/<branch>`` nor on the trunk (by ancestry or by patch), or
  an origin head that could not be read; an orphan with no session younger than
  :data:`ORPHAN_GRACE_S` is being set up;
* **remove** — a clean tree whose HEAD is on origin (pushed) or on the trunk, or whose run the
  lane has landed (``harvested``).

A safe worktree a queued run will reuse — a pending correction on its run, or a finished run
still waiting to land — is ``spare``: kept while the cap allows, since a correction reusing its
worktree keeps its dependency install and its session. The cap: worktrees ≤ live sessions +
``worker_pool.worktree_buffer`` (default :data:`DEFAULT_BUFFER`); the kept ones count against it
first, then the spare ones, most recently used first — the rest are removed, least recently used
first. A relaunch on a removed branch recreates the worktree from ``origin/<branch>``
(:func:`asf.workers.spawn.make_worktree`).

Removal is ``git worktree remove`` (no force: git itself refuses a tree with changes) and a
``git worktree prune`` — never a branch delete. Each pass writes ``worktrees.json`` in the
product's state dir — the count, the sizes (``du -sk``, cached, at most
:data:`SIZE_MEASURES_PER_PASS` fresh measurements a pass) and what is removable — which is what
``asf doctor``'s ``worktrees`` row reads, so the doctor measures nothing itself.
"""
import dataclasses
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import spawn as spawn_mod

DEFAULT_BUFFER = 2
#: An orphan worktree (no run on the ledger) younger than this is a launch being set up.
ORPHAN_GRACE_S = 3600
#: A cached size older than this is measured again.
SIZE_TTL_S = 12 * 3600
#: ``du`` runs at most this many times a pass (the reaped ones are measured regardless).
SIZE_MEASURES_PER_PASS = 4
DU_TIMEOUT_S = 120
STATE_FILE = 'worktrees.json'

LIVE, KEEP, SPARE, REMOVE = 'live', 'keep', 'spare', 'remove'


@dataclasses.dataclass
class Verdict:
    name: str
    path: str
    action: str
    reason: str
    job: str = ''
    branch: str = ''
    used: float = 0.0
    wanted: bool = False


def buffer_of(cfg):
    """``worker_pool.worktree_buffer`` as a non-negative int, :data:`DEFAULT_BUFFER` when unset
    or unreadable."""
    raw = ((cfg or {}).get('worker_pool') or {}).get('worktree_buffer', DEFAULT_BUFFER)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUFFER
    return max(0, n)


def _git(args, cwd, timeout=60):
    try:
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, '', str(e))


def remote_heads(repo):
    """``{branch: sha}`` of origin's heads (one ``ls-remote``), or None when origin is
    unreadable — then no worktree is judged pushed."""
    p = _git(['ls-remote', '--heads', 'origin'], repo)
    if p.returncode != 0:
        return None
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/heads/'):
            out[parts[1][len('refs/heads/'):]] = parts[0]
    return out


def _head_branch(path):
    p = _git(['rev-parse', '--abbrev-ref', 'HEAD'], path)
    b = p.stdout.strip() if p.returncode == 0 else ''
    return '' if b in ('', 'HEAD') else b


def _in_progress(path):
    for name in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD'):
        p = _git(['rev-parse', '--git-path', name], path)
        where = p.stdout.strip()
        if p.returncode == 0 and where:
            if not os.path.isabs(where):
                where = os.path.join(path, where)
            if os.path.exists(where):
                return name.split('-')[0].split('_')[0].lower()
    return ''


def _has_object(path, sha):
    return _git(['cat-file', '-e', f'{sha}^{{commit}}'], path).returncode == 0


def safety(path, branch, main, heads, run=None, fetch=False):
    """``(safe, reason)``: ``safe`` when removing the worktree at ``path`` loses nothing — a
    clean tree whose HEAD is on ``origin/<branch>`` or on ``origin/<main>`` (by ancestry, or
    every own commit by patch), or whose run the lane landed. ``heads`` is
    :func:`remote_heads`' map (None: origin unreadable). ``fetch``: an origin head this repo
    lacks the objects of is fetched before it is compared (else the worktree is kept)."""
    st = _git(['status', '--porcelain'], path)
    if st.returncode != 0:
        return False, 'not a git worktree'
    dirty = [ln for ln in st.stdout.splitlines() if ln.strip()]
    if dirty:
        return False, f'{len(dirty)} uncommitted file(s)'
    busy = _in_progress(path)
    if busy:
        return False, f'{busy} in progress'
    if lifecycle.landed(run):
        return True, f'landed {str(run["harvested"])[:9]}'
    trunk = f'origin/{main}'
    if _git(['merge-base', '--is-ancestor', 'HEAD', trunk], path).returncode == 0:
        return True, f'on {trunk}'
    cherry = _git(['cherry', trunk, 'HEAD'], path)
    if cherry.returncode != 0:
        return False, f'{trunk} unreadable'
    off_trunk = _plus(cherry.stdout)  # own commits whose patch the trunk lacks
    if not off_trunk:
        return True, f'on {trunk} (by patch)'
    remote = (heads or {}).get(branch) if branch else None
    if remote:
        if not _has_object(path, remote) and fetch:
            _git(['fetch', '-q', 'origin', branch], path)
        if not _has_object(path, remote):
            return False, f'origin/{branch} not fetched'
        if _git(['merge-base', '--is-ancestor', 'HEAD', remote], path).returncode == 0:
            return True, 'pushed'
        on_remote = _git(['cherry', remote, 'HEAD'], path)
        if on_remote.returncode != 0:
            return False, f'origin/{branch} unreadable'
        # a commit is lost only when its patch is on neither origin/<branch> nor the trunk
        m = len(off_trunk & _plus(on_remote.stdout))
        if m == 0:
            return True, 'pushed (by patch)'
        return False, f'{m} unpushed commit(s)'
    why = 'origin unreadable' if heads is None else 'branch not on origin'
    return False, f'{len(off_trunk)} unpushed commit(s) ({why})'


def _plus(cherry_out):
    return {ln[2:].strip() for ln in cherry_out.splitlines() if ln.startswith('+')}


def _epoch(iso):
    try:
        return datetime.strptime(str(iso), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def plan(product, buffer=DEFAULT_BUFFER, alive=None, fetch=False, now=None):
    """Every worktree's :class:`Verdict`, in directory order — the dry run: nothing is touched
    (``fetch`` aside, which only reads origin's objects in)."""
    alive = alive or lifecycle.pid_alive
    now = time.time() if now is None else now
    wdir = spawn_mod.worktrees_dir(product)
    registry = pool_mod.sessions_path(product)
    sessions = pool_mod.load_sessions(product)
    owners = lifecycle.by_worktree(registry)
    main = getattr(product, 'main', 'main') or 'main'
    heads = remote_heads(product.repo_dir)
    out = []
    for name in sorted(os.listdir(wdir)):
        path = os.path.join(wdir, name)
        if not os.path.isdir(path):
            continue
        run = owners.get(lifecycle.path_key(path)) or sessions.get(name)
        branch = _head_branch(path) or (run or {}).get('branch') or ''
        used = max(_epoch((run or {}).get('ended')), _epoch((run or {}).get('started')))
        v = Verdict(name, path, KEEP, '', job=(run or {}).get('job', name), branch=branch,
                    used=used or _mtime(path))
        out.append(v)
        if run is not None and (lifecycle.is_live(run) or alive(run.get('pid'))):
            v.action, v.reason = LIVE, 'live session'
            continue
        if run is None and now - _mtime(path) < ORPHAN_GRACE_S:
            v.reason = 'no session yet (new)'
            continue
        safe, why = safety(path, branch, main, heads, run=run, fetch=fetch)
        if not safe:
            v.reason = why
            continue
        v.wanted = run is not None and not why.startswith(('landed', 'on ')) and bool(
            lifecycle.pending_correction(run, registry) or lifecycle.eligible(run))
        v.action, v.reason = REMOVE, why
    _cap(out, buffer)
    return out


def _cap(verdicts, buffer):
    """Worktrees ≤ live + ``buffer``: the kept ones take the room first, then the wanted
    safe ones most recently used first; a wanted one beyond the room is removed."""
    room = max(0, buffer - sum(1 for v in verdicts if v.action == KEEP))
    wanted = sorted((v for v in verdicts if v.action == REMOVE and v.wanted),
                    key=lambda v: (v.used, v.name), reverse=True)
    for i, v in enumerate(wanted):
        if i < room:
            v.action, v.reason = SPARE, f'buffer: a queued run reuses it ({v.reason})'
        else:
            v.reason = f'{v.reason}; beyond the buffer of {buffer} (least recently used)'


# ---- sizes ------------------------------------------------------------------------------

def _state_path(product):
    return os.path.join(env.state_dir(product), STATE_FILE)


def read_state(product):
    try:
        with open(_state_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(product, data):
    path = _state_path(product)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, indent=1)
    os.replace(tmp, path)


def du_kb(path):
    """``du -sk`` of ``path`` in KiB, or None when it did not answer in time."""
    try:
        p = subprocess.run(['du', '-sk', path], capture_output=True, text=True,
                           timeout=DU_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return int(p.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def _gb(kb):
    return f'{kb / (1024 * 1024):.1f} GB'


def _sizes(verdicts, cached, now, budget, measure=du_kb):
    """``{name: {'kb', 'at'}}`` for the worktrees that still exist, a stale or missing entry
    measured while ``budget`` lasts (oldest first)."""
    sizes = {v.name: cached[v.name] for v in verdicts
             if isinstance(cached.get(v.name), dict) and 'kb' in cached[v.name]}
    stale = sorted((v for v in verdicts
                    if now - (sizes.get(v.name) or {}).get('at', 0) > SIZE_TTL_S),
                   key=lambda v: (sizes.get(v.name) or {}).get('at', 0))
    for v in stale[:budget]:
        kb = measure(v.path)
        if kb is not None:
            sizes[v.name] = {'kb': kb, 'at': int(now)}
    return sizes


def reap(product, buffer=None, alive=None, fix=True, out=print, now=None, measure=du_kb):
    """One pass: :func:`plan` with origin's missing objects fetched, then (``fix``) every
    ``remove`` verdict removed. Prints one line when anything was reaped — ``worktrees: reaped
    N (X GB), kept M`` — and records the state the doctor reads. Returns ``{'removed': [...],
    'failed': [(verdict, why)], 'verdicts': [...]}``."""
    if buffer is None:
        buffer = buffer_of(spawn_mod.load_cfg())
    now = time.time() if now is None else now
    verdicts = plan(product, buffer=buffer, alive=alive, fetch=fix, now=now)
    state = read_state(product) or {}
    cached = state.get('sizes') if isinstance(state.get('sizes'), dict) else {}
    removed, failed = [], []
    reaped_kb = 0
    for v in verdicts:
        if v.action != REMOVE or not fix:
            continue
        kb = (cached.get(v.name) or {}).get('kb') if isinstance(cached.get(v.name), dict) else None
        if kb is None:
            kb = measure(v.path) or 0
        p = _git(['worktree', 'remove', v.path], product.repo_dir, timeout=600)
        if p.returncode != 0:
            failed.append((v, (p.stderr or p.stdout).strip().splitlines()[-1:] or ['refused']))
            continue
        removed.append(v)
        reaped_kb += kb
        spawn_mod.release_id_range(product, v.job)
    if removed:
        _git(['worktree', 'prune'], product.repo_dir)
    remaining = [v for v in verdicts if v not in removed]
    sizes = _sizes(remaining, cached, now, SIZE_MEASURES_PER_PASS, measure)
    for v, why in failed:
        out(f'worktrees: could not remove {v.name}: {why[0]}')
    if removed:
        out(f'worktrees: reaped {len(removed)} ({_gb(reaped_kb)}), kept {len(remaining)}')
    _write_state(product, {
        'at': datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count': len(remaining),
        'removable': sum(1 for v in remaining if v.action == REMOVE),
        'reaped': len(removed), 'reaped_kb': reaped_kb,
        'kept': {v.name: v.reason for v in remaining if v.action == KEEP},
        'sizes': sizes})
    return {'removed': removed, 'failed': failed, 'verdicts': verdicts}


# ---- the doctor row ---------------------------------------------------------------------

def pnpm_note(product, environ=None):
    """For a product with a ``pnpm-lock.yaml``: which package store its worktrees share —
    ``npm_config_store_dir`` from the environment, a ``store-dir`` line in the repo's or the
    home's ``.npmrc``, else pnpm's default global store. '' for any other product."""
    repo = getattr(product, 'repo_dir', None) or ''
    if not os.path.isfile(os.path.join(repo, 'pnpm-lock.yaml')):
        return ''
    environ = os.environ if environ is None else environ
    for key in ('npm_config_store_dir', 'NPM_CONFIG_STORE_DIR'):
        if environ.get(key):
            return f'pnpm store: {environ[key]}'
    for rc in (os.path.join(repo, '.npmrc'), os.path.expanduser('~/.npmrc')):
        try:
            with open(rc, encoding='utf-8') as f:
                for line in f:
                    k, _, val = line.partition('=')
                    if k.strip() == 'store-dir' and val.strip():
                        return f'pnpm store: {val.strip()} ({os.path.basename(rc)})'
        except OSError:
            continue
    return 'pnpm store: default (shared per user)'


def doctor_line(product):
    """``(ok, 'worktrees: N, X GB, R removable …')``: the count now, the sizes and the removable
    count as the last reaper pass recorded them. ``ok`` is False while the last pass left some
    removable or the count exceeds what the cap allows. Never measures or touches anything."""
    wdir = os.path.join(env.state_dir(product), 'worktrees')
    try:
        names = [n for n in os.listdir(wdir) if os.path.isdir(os.path.join(wdir, n))]
    except OSError:
        names = []
    state = read_state(product)
    parts = [f'worktrees: {len(names)}']
    ok = True
    if state:
        sizes = state.get('sizes') if isinstance(state.get('sizes'), dict) else {}
        known = [sizes[n]['kb'] for n in names if isinstance(sizes.get(n), dict)
                 and isinstance(sizes[n].get('kb'), int)]
        if known:
            approx = '' if len(known) == len(names) else f' ({len(known)} measured)'
            parts.append(f'{_gb(sum(known))}{approx}')
        removable = int(state.get('removable') or 0)
        parts.append(f'{removable} removable')
        ok = removable == 0
        parts.append(f'last pass reaped {state.get("reaped", 0)} at {state.get("at", "?")}')
    else:
        parts.append('no reaper pass yet')
    note = pnpm_note(product)
    if note:
        parts.append(note)
    return ok, ', '.join(parts)


def dry_run(product, buffer=None, alive=None):
    """What a pass would do now, read-only (no fetch, no removal): ``(verdicts, totals)`` with
    ``totals`` = ``{'count', 'live', 'keep', 'spare', 'remove'}``."""
    if buffer is None:
        buffer = buffer_of(spawn_mod.load_cfg())
    verdicts = plan(product, buffer=buffer, alive=alive, fetch=False)
    totals = {'count': len(verdicts), LIVE: 0, KEEP: 0, SPARE: 0, REMOVE: 0}
    for v in verdicts:
        totals[v.action] += 1
    return verdicts, totals


def report(verdicts, out=print):
    """One line per worktree that is not a live session's: its verdict and why."""
    for v in verdicts:
        if v.action != LIVE:
            out(f'worktree {v.action:<6} {v.name:<28} {v.reason}')
