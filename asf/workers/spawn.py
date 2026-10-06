"""asf.workers.spawn — one job: worktree, id range, brief, run, ledger line.

``spawn(product, row, account, brief_text)``:

1. the product repo's git push gate confirmed in place (:func:`asf.hooks.ensure_git_hooks`) —
   before any worktree is touched, so no session is ever launched into a repo with no gate
   (F-0075, D10) — then a worktree ``~/.ASF/state/<product>/worktrees/<job>`` on a new branch
   (the row's, else ``<branch prefix for the kind>/<job>``) off ``origin/<main>`` of
   ``Product.repo_dir`` — an ended run's worktree on that branch, or a branch already on origin
   (a held branch sent back for another round, ``correct`` or ``adjudicate`` alike), is reused
   instead, rebased onto ``origin/<main>``; only a live run's worktree refuses
   (:func:`make_worktree`); the product's ``conventions.worktree_setup`` then runs in it unless
   the worktree already records that exact command (:func:`run_worktree_setup`,
   :func:`setup_done`) — a failure refuses the launch and removes the worktree;
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
   branch, started, session, product — plus ``host_load_bypass: true`` when the row carries it
   (the wave step's S1 load-hold bypass, :mod:`asf.tick.step_wave`), so a later wave can see the
   bypass is still live.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

from asf import ci_flight
from asf import env, refguard
from asf import hooks
from asf import progress
from asf.workers import githooks
from asf.workers import heartbeat
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import pushlog
from asf.workers import runtime as runtime_mod
from asf.workers import stopgate

DEFAULT_ID_PREFIXES = ['S', 'T', 'B']
DEFAULT_ID_START = 5000
DEFAULT_ID_SIZE = 50

#: :func:`trunk_rebase_needed`'s third return value — the card's exception (a), named so the
#: launch gate and the docstring cannot drift from each other (F-0203 PD4).
CONFLICT_REASON = 'does not merge cleanly into the trunk'


class SpawnError(Exception):
    """A launch refused. ``clear`` is the one command that clears it, when there is one."""

    def __init__(self, msg, clear=''):
        super().__init__(msg)
        self.clear = clear


class WorktreeBusy(SpawnError):
    """The worktree is held by a live run: the item is already at work — a wait, not a fault."""


class WorktreeExternal(SpawnError):
    """The branch is checked out in a worktree ASF did not create (outside its own
    ``worktrees_dir``) — someone else's checkout, still at work on it. A wait, never an
    operator matter, and never reclaimed or removed (B-0142)."""


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
    with _STATE_LOCK:  # a wave's launches run concurrently: two never read the same top block
        return _reserve_id_range(product, job, prefixes, start, size)


def _reserve_id_range(product, job, prefixes, start, size):
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
    claimed = claim_id_blocks(product, job, prefixes, start, size, top)
    if claimed:
        rng = claimed
    else:
        parts = []
        for p in prefixes:
            lo = max(start, top.get(p, start - 1) + 1)
            parts.append(f'{p}:{lo:04d}-{lo + size - 1:04d}')
        rng = ','.join(parts)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'{job}\t{rng}\t{pool_mod.now_iso()}\n')
    return rng


def claim_repo(product):
    """The record repo whose origin holds the id claims: the tick's record clone when it
    exists, else the product's backlog checkout. None when neither has an ``origin``."""
    from asf.record import idclaim
    from asf.tick import shadow
    for d in (shadow.record_dir(product), getattr(product, 'backlog_dir', None)):
        if d and idclaim.has_origin(d):
            return d
    return None


def claim_id_blocks(product, job, prefixes, start, size, local_top=None):
    """Claim the job's blocks on the record repo's origin (:mod:`asf.record.idclaim`), above
    every local reservation (``local_top``) and every id the record holds. Returns the
    ``BACKLOG_ID_RANGE`` text, or None when claims are off or there is no origin — the caller
    keeps the local-only block. A claim that fails after its bounded attempts is a SpawnError:
    a launch never goes out with a block nobody holds."""
    from asf.record import idclaim
    from asf.record.ids import record_top
    if not idclaim.enabled(product):
        return None
    repo = claim_repo(product)
    if not repo:
        return None
    floors = {p: max((local_top or {}).get(p, 0), record_top(repo, p)) for p in prefixes}
    try:
        blocks = idclaim.claim(repo, {p: size for p in prefixes}, job, floors=floors, start=start)
    except idclaim.ClaimError as e:
        raise SpawnError(f'id claim for {job}: {e}') from None
    return idclaim.range_text(blocks, order=list(prefixes))


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


#: the session kinds that never push: the only ones that may work on a non-factory branch
READ_ONLY_KINDS = frozenset({'review'})


def _land_requested(product, item):
    """The PR number when ``item`` is a ``PR-<n>`` the product asked ``asf land`` to take, else
    None (never raises: an unreadable request file is no request)."""
    from asf.harvest import lane as lane_mod   # local: the lane imports the workers
    m = lane_mod.PR_ITEM_RE.match(str(item or ''))
    if not m:
        return None
    try:
        from asf import merge_queue
        reqs = merge_queue.load_requests(env.state_dir(product))
    except Exception:  # noqa: BLE001
        return None
    n = int(m.group(1))
    return n if any(int(r.get('pr') or 0) == n for r in reqs.values()) else None


def _kind_prefix(product, kind):
    """The prefix a ``kind`` session's own branch is minted under (:func:`branch_for`)."""
    try:
        return f'{product.branch_prefix(kind)}/' if kind else ''
    except Exception:  # noqa: BLE001
        return ''


def _factory_branch(product, branch, kind=None):
    """A branch under the factory's prefixes, or the one a ``kind`` session is minted under."""
    conv = getattr(product, 'conventions', None)
    of = getattr(conv, 'branch_kind', None)
    mine = _kind_prefix(product, kind)
    return bool(branch) and (bool(callable(of) and of(branch))
                             or bool(mine and branch.startswith(mine)))


def _foreign(row):
    """A row for a PR no factory item made (``PR-<n>``, the lane's foreign-PR adoption): the
    one way a session reaches a branch the factory did not cut."""
    from asf.harvest import lane as lane_mod   # local: the lane imports the workers
    return bool(row.branch) and lane_mod.is_pr_item(row.item)


def spawn_refusal(product, row, branch):
    """Why ``row`` must not launch, or '' — the spawn guard. An ``asf land`` request's PR is the
    merge queue's to land, never factory work; and a session never works on a branch outside the
    factory's prefixes (a person's or a product session's own branch: ``ci/…``) unless it is a
    kind that never pushes (:data:`READ_ONLY_KINDS`)."""
    n = _land_requested(product, row.item)
    if n:
        return (f'{row.job}: PR #{n} is an asf land request — the merge queue lands it as it '
                f'is; it is never factory work')
    if (_foreign(row) and not _factory_branch(product, branch, row.kind)
            and row.kind not in READ_ONLY_KINDS):
        return (f'{row.job}: {branch} is not a factory branch — an ASF session never pushes to '
                f'a branch outside the factory prefixes')
    return ''


def push_allow(product, row, branch):
    """``ASF_PUSH_ALLOW``: the branch prefixes (and the session's own factory-minted branch) a
    session's ``pre-push`` hook lets it push to (:data:`asf.workers.githooks.ASF_HOOK`) —
    every other ``refs/heads/`` ref is refused. '' when the product names no prefixes."""
    conv = getattr(product, 'conventions', None)
    allow = list(getattr(conv, 'all_prefixes', lambda: ())() or ()) if conv is not None else []
    if not allow:
        return ''
    allow.append(_kind_prefix(product, row.kind))
    if branch and not _foreign(row):   # a factory item's own branch, whatever its prefix
        allow.append(branch)
    return ' '.join(dict.fromkeys(a for a in allow if a and ' ' not in a))


def _holding_worktree(repo, branch):
    """The worktree that holds ``branch``: the one with it checked out, or — what ``git worktree
    list`` shows only as *detached* — one with a rebase of it in progress (2026-09-26: a dead
    run's stuck rebase held its branch, and ``worktree add -B`` failed every tick)."""
    out = _git(['worktree', 'list', '--porcelain'], repo)
    path = None
    detached = []
    for line in out.splitlines():
        if line.startswith('worktree '):
            path = line[len('worktree '):]
        elif line == f'branch refs/heads/{branch}':
            return path
        elif line == 'detached' and path:
            detached.append(path)
    for p in detached:
        if _in_progress_head(p) == f'refs/heads/{branch}':
            return p
    return None


def _under(path, parent):
    """Whether ``path`` sits inside ``parent`` (a worktree ASF's own ``worktrees_dir``, so an
    ORPHAN found outside it is someone else's checkout, not one the factory ever owned)."""
    child, base = lifecycle.path_key(path), lifecycle.path_key(parent)
    return child == base or child.startswith(base + os.sep)


def _admin_dir(path):
    """A linked worktree's admin dir (``<repo>/.git/worktrees/<name>``), read from its ``.git``
    file; '' when there is none."""
    try:
        with open(os.path.join(path, '.git'), encoding='utf-8') as f:
            head = f.read().strip()
    except OSError:
        return ''
    if not head.startswith('gitdir: '):
        return ''
    d = head[len('gitdir: '):]
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(path, d))


def _in_progress_head(path):
    """The ref a rebase in progress in the worktree at ``path`` will land on, or ''."""
    admin = _admin_dir(path)
    for sub in ('rebase-merge', 'rebase-apply'):
        try:
            with open(os.path.join(admin, sub, 'head-name'), encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            continue
    return ''


def _archive_ref(branch, sha):
    return f'archive/{branch.replace("/", "-")}-wip-{sha[:9]}'


def _on_origin(repo, sha):
    p = subprocess.run(['git', 'for-each-ref', '--contains', sha, '--count=1',
                        '--format=%(refname)', 'refs/remotes/origin'],
                       cwd=repo, capture_output=True, text=True)
    return p.returncode == 0 and bool(p.stdout.strip())


def _new_patches(repo, tip, upstreams):
    """The ``git cherry`` lines of ``tip``'s commits no upstream in ``upstreams`` carries an
    equivalent patch of (same patch-id: a reworded or rebased copy counts as carried). An
    upstream git cannot read is skipped; with none readable every commit is new. Merge commits
    are not listed by ``git cherry`` — the caller archives the tip before trusting this."""
    new = None
    for up in upstreams:
        if not up:
            continue
        p = subprocess.run(['git', 'cherry', '-v', up, tip], cwd=repo, capture_output=True,
                           text=True)
        if p.returncode != 0:
            continue
        plus = [ln.strip() for ln in p.stdout.splitlines() if ln.startswith('+')]
        keys = {ln.split()[1] for ln in plus if len(ln.split()) > 1}
        if new is None:
            new = {ln.split()[1]: ln for ln in plus if len(ln.split()) > 1}
        else:
            new = {sha: ln for sha, ln in new.items() if sha in keys}
    if new is None:
        return [f'+ {tip} (no upstream git could compare against)']
    return list(new.values())


def _archive_tip(product, repo, branch, tip, timeout):
    """Push ``tip`` to its ``archive/<branch>-wip-<sha9>`` ref and verify it with
    ``ls-remote``; the ref name. Raises :class:`SpawnError` when it is not there after the push —
    nothing is deleted without its archive."""
    from asf import gitpush
    ref = _archive_ref(branch, tip)
    if refguard.refusal(ref, f'archive {branch}', product.main, None):
        raise SpawnError(f'retire {branch} refused: {ref} is a protected ref')
    r = gitpush.push(['-q', 'origin', f'{tip}:refs/heads/{ref}'], repo, refs_only=True,
                     timeout=timeout, guard=refguard.guard_for(product, repo))
    have = _git(['ls-remote', '--heads', 'origin', f'refs/heads/{ref}'], repo).split()
    if r.returncode != 0 or tip not in have:
        raise SpawnError(f'retire {branch} refused: {tip[:9]} could not be archived to {ref}: '
                         f'{(r.stderr or "").strip() or "not on origin after the push"}')
    return ref


def _archive_unpushed(product, repo, holder, branch):
    """Push every commit the dead ``holder`` carries that origin does not — its HEAD (a WIP the
    factory committed detached, B-0094) and the local ``branch`` tip — to an ``archive/`` ref.
    An archive carries no new code, so the product's pre-push hook is skipped
    (:func:`asf.gitpush.push`, ``refs_only``). Returns the refs pushed; a push that fails
    refuses the reclaim (the worktree is then left as it stands)."""
    from asf import gitpush
    subprocess.run(['git', 'fetch', '-q', '--prune', 'origin'], cwd=repo, capture_output=True)
    tips = []
    for rev, cwd in (('HEAD', holder), (f'refs/heads/{branch}', repo)):
        p = subprocess.run(['git', 'rev-parse', '--verify', '-q', f'{rev}^{{commit}}'], cwd=cwd,
                           capture_output=True, text=True)
        sha = p.stdout.strip() if p.returncode == 0 else ''
        if sha and sha not in tips and not _on_origin(repo, sha):
            tips.append(sha)
    # a tip another one already carries needs no ref of its own
    tips = [t for t in tips if not any(
        o != t and subprocess.run(['git', 'merge-base', '--is-ancestor', t, o], cwd=repo,
                                  capture_output=True).returncode == 0 for o in tips)]
    refs = []
    for sha in tips:
        ref = _archive_ref(branch, sha)
        if refguard.refusal(ref, f'archive {branch}', product.main, None):
            raise SpawnError(f'reclaim of {holder} refused: {ref} is a protected ref')
        r = gitpush.push(['-q', 'origin', f'{sha}:refs/heads/{ref}'], repo, refs_only=True,
                         timeout=gitpush.push_timeout(getattr(product, 'conventions', None)),
                         guard=refguard.guard_for(product, repo))
        if r.returncode != 0:
            raise SpawnError(f'reclaim of {holder} refused: {sha[:9]} could not be archived '
                             f'to {ref}: {(r.stderr or "").strip()}')
        refs.append(ref)
    return refs


#: how long a worktree no run recorded (an ORPHAN in ASF's own worktrees dir) must sit untouched
#: before a spawn that needs its path or its branch reclaims it. Younger, it may be a launch whose
#: ledger line is not written yet; older, nothing is at work in it, and refusing it only turned a
#: stale holder into a NEEDS OPERATOR every tick for ever.
ORPHAN_GRACE_S = 1800


def _last_touched(path):
    """The newest mtime of the worktree at ``path``: the tree itself, its ``.git`` file and its
    admin dir's HEAD and index (a commit or a checkout moves those). 0 when none can be read."""
    admin = _admin_dir(path)
    newest = 0.0
    for p in (path, os.path.join(path, '.git'),
              *((os.path.join(admin, 'HEAD'), os.path.join(admin, 'index')) if admin else ())):
        try:
            newest = max(newest, os.stat(p).st_mtime)
        except OSError:
            continue
    return newest


def _stale_orphan(path, now=None):
    """Whether the orphan worktree at ``path`` is old enough to reclaim (:data:`ORPHAN_GRACE_S`)."""
    now = time.time() if now is None else now
    return now - _last_touched(path) >= ORPHAN_GRACE_S


def _commit_leftovers(holder):
    """Commit whatever an orphan's tree holds uncommitted (tracked and untracked), hooks off, so
    :func:`_archive_unpushed` keeps it under an ``archive/`` ref before the tree goes. A tree
    with nothing to commit is left as it is."""
    st = subprocess.run(['git', 'status', '--porcelain'], cwd=holder, capture_output=True,
                        text=True)
    if st.returncode != 0 or not st.stdout.strip():
        return
    subprocess.run(['git', 'add', '-A'], cwd=holder, capture_output=True)
    subprocess.run(['git', '-c', 'user.name=asf', '-c', 'user.email=asf@localhost', 'commit',
                    '-q', '--no-verify', '-m', 'wip: an orphan worktree, reclaimed by the factory'],
                   cwd=holder, capture_output=True)


def _reclaim(product, repo, job, holder, branch, now=None):
    """Free ``branch`` from ``holder``, a worktree whose run is dead: archive its commits not on
    origin (:func:`_archive_unpushed`), abort a rebase or merge in progress, move the tree to the
    trash (:mod:`asf.workers.trash`) and prune. A live run's worktree is never touched
    (:class:`WorktreeBusy`). One no run recorded (an orphan) is reclaimed the same way — its
    uncommitted files committed first, so the archive keeps them — once it has sat untouched for
    :data:`ORPHAN_GRACE_S`; younger, it is refused as before (a launch may still be writing its
    ledger line). A stale holder never blocks a spawn for ever."""
    registry = pool_mod.sessions_path(product)
    if os.path.exists(holder):
        what, why = lifecycle.launch_verdict(registry, job, holder)
        if what == lifecycle.BUSY:
            raise WorktreeBusy(why)
        orphan = what == lifecycle.ORPHAN
        if what and not (orphan and _stale_orphan(holder, now)):
            raise SpawnError(why, clear=f'git -C {repo} worktree remove --force {holder}'
                                        f'  # after checking nothing in it is wanted')
        run = lifecycle.by_worktree(registry).get(lifecycle.path_key(holder)) or {}
        if orphan:
            _commit_leftovers(holder)
        refs = _archive_unpushed(product, repo, holder, branch)
        admin = _admin_dir(holder)
        for state, args in (('rebase-merge', ['rebase', '--abort']),
                            ('rebase-apply', ['rebase', '--abort']),
                            ('MERGE_HEAD', ['merge', '--abort'])):
            if admin and os.path.exists(os.path.join(admin, state)):
                subprocess.run(['git', *args], cwd=holder, capture_output=True)
        ok, why = _discard(product, holder)
        if not ok:
            raise SpawnError(f'reclaim of {holder} failed: {why}')
        owner = 'an orphan (no run recorded)' if orphan else f'dead {run.get("job") or "?"}'
        print(f'reclaimed {holder} from {owner}: archived '
              f'{", ".join(refs) or "nothing"}', file=sys.stderr)
    subprocess.run(['git', 'worktree', 'prune'], cwd=repo, capture_output=True)


_HELD_BY = re.compile(r"already (?:used|checked out) by worktree at '?([^'\n]+?)'?\s*$", re.M)


def _origin_sha(repo, branch):
    out = _git(['ls-remote', '--heads', 'origin', f'refs/heads/{branch}'], repo).strip()
    return out.split()[0] if out else ''


def retire_dead_branch(product, repo, job, branch, holders=()):
    """A restart of an item whose PR was closed unmerged (a registry reset,
    :func:`asf.workers.lifecycle.reset_of_branch`) starts from the trunk, never from the closed
    work: while ``origin/<branch>`` still sits at the reset's head, that head is kept as its
    ``archive/pr-<N>`` tag (pushed here when the closer left none), every dead worktree holding
    the branch is reclaimed (:func:`_reclaim`: its unpushed commits archived), the local branch
    is dropped and the remote one deleted under a lease on that very head. The launch then cuts
    ``branch`` fresh from the trunk. True when it retired the branch; False when there is no
    reset for it or origin moved past the dead head (new work: the branch is alive)."""
    from asf import gitpush
    reset = lifecycle.reset_of_branch(pool_mod.sessions_path(product), branch)
    dead = (reset or {}).get('head') or ''
    if not dead or _origin_sha(repo, branch) != dead:
        return False
    guard = refguard.refusal(branch, f'retire {branch}', product.main, None)
    if guard:
        raise SpawnError(guard)
    limit = gitpush.push_timeout(getattr(product, 'conventions', None))
    tag = reset.get('archive') or f"archive/pr-{reset.get('pr')}"
    have = _git(['ls-remote', '--tags', 'origin', f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}'],
                repo).split()
    if have and dead not in have:
        tag = f'{tag}-{dead[:9]}'  # the PR's own tag keeps its head; this later push gets its own
        have = _git(['ls-remote', '--tags', 'origin', f'refs/tags/{tag}'], repo).split()
    if dead not in have:
        r = gitpush.push(['-q', 'origin', f'{dead}:refs/tags/{tag}'], repo, refs_only=True,
                         timeout=limit, guard=refguard.guard_for(product, repo))
        if r.returncode != 0:
            raise SpawnError(f'retire {branch} refused: {dead[:9]} could not be kept as {tag}: '
                             f'{(r.stderr or "").strip()}')
    for holder in dict.fromkeys(h for h in holders if h and os.path.exists(h)):
        _reclaim(product, repo, job, holder, branch)
    if _local_branch_exists(repo, branch):
        tip = _git(['rev-parse', f'refs/heads/{branch}'], repo).strip()
        fast = tip == dead or subprocess.run(
            ['git', 'merge-base', '--is-ancestor', tip, dead], cwd=repo,
            capture_output=True).returncode == 0 or _on_origin(repo, tip)
        if not fast:
            # archive first: whatever the patch-id check below decides, the local tip is on
            # origin before anything is deleted or refused (git cherry skips merge commits)
            ref = _archive_tip(product, repo, branch, tip, limit)
            new = _new_patches(repo, tip, (dead, f'origin/{product.main}'))
            if new:
                raise SpawnError(f'branch {branch} exists locally with commits past the closed '
                                 f'PR #{reset.get("pr")} and not on origin (archived as {ref}); '
                                 f'git cherry: {"; ".join(new[:3])}'
                                 + (f' and {len(new) - 3} more' if len(new) > 3 else '')
                                 + ' — look before relaunching')
            print(f'retire {branch}: local tip {tip[:9]} carries no new patch (reworded or '
                  f'rebased copy); archived as {ref}', file=sys.stderr)
        _git(['branch', '-D', branch], repo)
    r = gitpush.push(['-q', f'--force-with-lease=refs/heads/{branch}:{dead}', 'origin',
                      f':refs/heads/{branch}'], repo, refs_only=True, timeout=limit,
                     guard=refguard.guard_for(product, repo))
    if r.returncode != 0:
        raise SpawnError(f'retire {branch} refused: {(r.stderr or "").strip() or "push failed"}')
    print(f'retired {branch}: PR #{reset.get("pr")} closed unmerged, its head {dead[:9]} kept '
          f'as {tag}; cut fresh from origin/{product.main}', file=sys.stderr)
    return True


def _add_worktree(product, repo, job, branch, args):
    """``git worktree add <args>``; when another worktree holds ``branch`` (a hold
    :func:`_holding_worktree` did not see) and its run is dead, it is reclaimed and the add
    retried once."""
    p = subprocess.run(['git', 'worktree', 'add', *args], cwd=repo, capture_output=True,
                       text=True)
    if p.returncode == 0:
        return
    m = _HELD_BY.search(p.stderr or '')
    if not m:
        raise SpawnError(f"git worktree add {' '.join(args)}: {p.stderr.strip()}")
    _reclaim(product, repo, job, m.group(1), branch)
    _git(['worktree', 'add', *args], repo)


def _archived_on_origin(repo, branch):
    """True when the local ``branch``'s tip is kept on origin under an ``archive/<branch>…``
    head (the lane's archive of a superseded branch, or a reclaim's ``-wip-`` one): dropping
    the local branch loses nothing origin does not hold."""
    from asf import gitops
    tip = gitops.rev_parse(repo, f'refs/heads/{branch}')
    ls = gitops.git(['ls-remote', '--heads', 'origin'], repo)
    if not tip or not ls.ok:
        return False
    prefix = f'refs/heads/archive/{branch}'
    for line in ls.data.splitlines():
        sha, _, ref = line.strip().partition('\t')
        ref = ref.strip()
        if not sha or not (ref == prefix or ref.startswith(prefix + '-')):
            continue
        if sha == tip:
            return True
        if gitops.is_ancestor(repo, tip, sha) is None:  # its archive not fetched here yet
            gitops.fetch(repo, 'origin', ref)
        if gitops.is_ancestor(repo, tip, sha):
            return True
    return False


def _branch_exists_on_origin(repo, branch):
    """The check a held branch is reused on: not the row's kind (B-0048 — an ADJUDICATE row's
    kind is ``adjudicate``, not ``correct``, so keying on kind alone missed it and spawned it
    fresh off main, silently losing the branch's own history) but whether ``branch`` is already
    a ref on origin."""
    from asf import gitops  # the exact ref: ``archive/<branch>`` is not ``<branch>``
    return bool(gitops.head_sha(
        _git(['ls-remote', '--heads', 'origin', gitops.head_ref(branch)], repo), branch))


def make_worktree(product, job, branch, kind=None, severity=None, flight=None):
    """The worktree a run starts in — :func:`asf.workers.lifecycle.may_launch` decides whether
    one that already exists may be taken over.

    A worktree already holding ``branch`` (this job's own, or an ended job's that a correction on
    the same branch follows) is reused as it stands — its tree and its branch, rebased onto the
    fetched trunk (a conflict is aborted: no worktree is handed over mid-rebase) — as long as the run
    that recorded it has ended; a live run's worktree is never touched (B-0025, B-0051). A branch
    already on origin with no worktree left (any row kind: a held branch sent back for another
    round, B-0046, B-0048) gets a worktree on it, rebased the same way. Otherwise a fresh branch
    off ``origin/<main>``. Returns the worktree path.

    A branch already on origin whose PR has a CI run in flight is **not** rebased or published —
    the run finishes first (F-0203, C6) — and the session starts on the branch as origin holds it.

    Before any of that, :func:`asf.hooks.ensure_git_hooks` confirms the product repo's push gate
    is in place — missing hooks are written, a foreign one refuses the whole launch (F-0075,
    D10) — so no path below can reach ``git worktree add`` without it.

    A reused worktree first catches up with ``origin/<branch>`` (:func:`_catch_up`): a session
    never starts on, and the factory never publishes, a head older than origin's. A ``review``
    (``kind``) starts on the remote head itself — it has no commits of its own yet."""
    repo = product.repo_dir
    if not repo or not os.path.isdir(repo):
        raise SpawnError(f'product repo_dir missing: {repo!r}')
    with repo_lock(repo):  # the repo's shared refs and worktree list: one launch at a time
        path, checkout, rebase = _place_worktree(product, repo, job, branch)
    # the rest runs inside this launch's own worktree, on its own branch — the slow part (the
    # rebase's publish runs the product's pre-push hook), so a wave's launches overlap here
    _settle(product, repo, path, branch)  # a dead run's rebase or merge is never inherited
    if checkout:
        _checkout_branch(path, branch, product.main)
    if rebase:
        _rebase_onto_trunk(path, branch, product.main, kind, severity=severity, product=product,
                           flight=flight)
    _settle(product, repo, path, branch)
    if _in_progress(path):
        raise SpawnError(f'worktree {path} is still mid-rebase or mid-merge after an abort; '
                         f'no session is handed it',
                         clear=f'git -C {path} status  # abort what is in progress, then relaunch')
    return path


#: What a worktree can be left in the middle of, and the command that abandons it.
_IN_PROGRESS = (('rebase-merge', ['rebase', '--abort']), ('rebase-apply', ['rebase', '--abort']),
                ('MERGE_HEAD', ['merge', '--abort']),
                ('CHERRY_PICK_HEAD', ['cherry-pick', '--abort']))


def _in_progress(path):
    """The operations in progress in the worktree at ``path`` (``rebase-merge``, …), [] when
    none."""
    admin = _admin_dir(path) or os.path.join(path, '.git')
    return [state for state, _ in _IN_PROGRESS if os.path.exists(os.path.join(admin, state))]


def _settle(product, repo, path, branch):
    """No worktree is handed to a session mid-rebase (a product's F-0037, 2026-09-27: a session
    arrived to a rebase onto a stale report commit replaying 257 unrelated commits). Whatever is
    in progress is abandoned — after the commits it holds that origin lacks are archived
    (:func:`_archive_unpushed`)."""
    states = _in_progress(path)
    if not states:
        return
    try:
        _archive_unpushed(product, repo, path, branch)
    except SpawnError as e:
        print(f'settle {path}: {e}', file=sys.stderr)
    for state, args in _IN_PROGRESS:
        if state in states:
            subprocess.run(['git', *args], cwd=path, capture_output=True)
    print(f'settled {path}: aborted the {", ".join(states)} left in progress', file=sys.stderr)


#: one lock per product repo (:func:`repo_lock`): a wave's launches run concurrently
#: (:func:`asf.workers.wave.wave`), and a ``fetch``/``worktree add`` on one repo must not
_REPO_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
#: the state files every launch reads and writes (the id ranges, the hook shims)
_STATE_LOCK = threading.Lock()


def repo_lock(repo):
    key = os.path.realpath(repo)
    with _LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(key, threading.Lock())


def _place_worktree(product, repo, job, branch):
    """:func:`make_worktree`'s part on the shared repo: ``(path, checkout, rebase)`` — the
    worktree, and whether it still needs its branch checked out and a rebase onto the trunk."""
    ok, detail = hooks.ensure_git_hooks(product)
    if not ok:
        raise SpawnError(detail)
    registry = pool_mod.sessions_path(product)
    path = os.path.join(worktrees_dir(product), job)
    _git(['fetch', '-q', 'origin', product.main], repo)
    held = _holding_worktree(repo, branch)
    if held and lifecycle.path_key(held) == lifecycle.path_key(path):
        held = path  # one directory, spelled ~/.ASF by git and ~/.asf by us (or the reverse)
    if held and held != path and not _under(held, worktrees_dir(product)):
        # a worktree ASF did not create holds the branch (a console agent's own checkout,
        # still at work in it): never NEEDS OPERATOR, never reclaimed (B-0142)
        raise WorktreeExternal(f'branch checked out in an external worktree '
                               f"{os.path.realpath(held)}; waits until it's released")
    if retire_dead_branch(product, repo, job, branch, (path, held)):
        held = None  # the dead branch's worktrees and refs are gone: cut fresh below
    for candidate in dict.fromkeys(p for p in (path, held) if p and os.path.exists(p)):
        what, why = lifecycle.launch_verdict(registry, job, candidate)
        if what == lifecycle.BUSY:
            raise WorktreeBusy(why)
        if what == lifecycle.ORPHAN and _stale_orphan(candidate):
            # an orphan nothing has touched in ORPHAN_GRACE_S: reclaimed (archived, trashed),
            # never a NEEDS OPERATOR every tick for ever
            _reclaim(product, repo, job, candidate, branch)
            if candidate == held:
                held = None
            continue
        if what:
            raise SpawnError(why, clear=f'git -C {repo} worktree remove --force {candidate}'
                                        f'  # after checking nothing in it is wanted')
        current = _worktree_branch(candidate)
        # another branch checked out (a detached tree; one mid-rebase is settled before launch)
        checkout = candidate == path and current not in (branch, 'HEAD', '')
        if candidate == path or current == branch:
            return candidate, checkout, True
    if _branch_exists_on_origin(repo, branch):
        _git(['fetch', '-q', 'origin', branch], repo)
        if held:
            # a stale worktree of an ended run still holds the branch and is not reusable here
            _reclaim(product, repo, job, held, branch)
        _add_worktree(product, repo, job, branch, ['-q', '-B', branch, path, f'origin/{branch}'])
        return path, False, True
    if held:
        _reclaim(product, repo, job, held, branch)  # its unpushed commits archived first
        _git(['branch', '-D', branch], repo)
    elif _local_branch_exists(repo, branch):
        # a branch with no worktree and not on origin (a reaped one): reused when it carries
        # nothing, refused with the count when it does (B-0025) — never silently reset. A tip
        # origin still keeps under ``archive/<branch>`` (the lane archived and pruned it — a
        # revived card's old branch) is not lost by cutting fresh: it is dropped too
        ahead = _git(['rev-list', '--count', f'origin/{product.main}..{branch}'], repo)
        if ahead not in ('', '0') and not _archived_on_origin(repo, branch):
            raise SpawnError(f'branch {branch} exists locally with {ahead} commit(s) not on '
                             f'origin/{product.main} and no worktree — look before relaunching')
        _git(['branch', '-D', branch], repo)
    # origin does not hold the branch: a tracking ref the fetches never pruned is no head of it
    # (it would read as the launch head, and a session told to fetch it fails every tick)
    from asf import gitops
    gitops.git(['update-ref', '-d', f'refs/remotes/origin/{branch}'], repo)
    _add_worktree(product, repo, job, branch, ['-q', '-b', branch, path, f'origin/{product.main}'])
    return path, False, False


def _catch_up(path, branch, remote_sha, kind=None, main=None):
    """Bring a reused worktree up to ``origin/<branch>`` at ``remote_sha`` before anything is
    rebased or published (2026-09-25: a review's worktree sat at an older head, the takeover
    published it over a person's newer commit, and the review then judged and pushed on top of
    the stale head). A review is reset to the remote head — it reviews what origin holds. Any
    other kind: a head behind origin is fast-forwarded; a head that has diverged is rebased onto
    origin's — its own commits only, never onto a report commit — and a conflict is aborted. A
    head already a rebase of origin's tip onto a newer trunk is left for publish. True when the
    worktree now holds every commit origin does."""
    def git(*args):
        return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)
    git('fetch', '-q', 'origin', branch)
    if git('cat-file', '-e', f'{remote_sha}^{{commit}}').returncode != 0:
        return False
    if kind == 'review':
        return git('reset', '-q', '--hard', remote_sha).returncode == 0
    if git('merge-base', '--is-ancestor', remote_sha, 'HEAD').returncode == 0:
        return True
    if git('merge-base', '--is-ancestor', 'HEAD', remote_sha).returncode == 0:
        return git('merge', '-q', '--ff-only', remote_sha).returncode == 0
    head = git('rev-parse', 'HEAD').stdout.strip()
    if lifecycle.lost_commits(path, head, remote_sha, branch) == []:
        return True  # origin's commits are here as rebased copies
    if main and lifecycle.rebase_of(path, head, remote_sha, main):
        return True  # a rebase of origin's tip onto a newer trunk: the factory publishes it
    # only the head's own commits are replayed — never the trunk commits a head rebased onto a
    # newer trunk carries past origin's tip — and never onto a report commit (F-0037)
    onto = _past_reports(path, remote_sha)
    base = git('merge-base', 'HEAD', f'origin/{main}').stdout.strip() if main else ''
    args = ['rebase', '-q', '--onto', onto, base] if base else ['rebase', '-q', onto]
    if git(*args).returncode == 0:
        return True
    git('rebase', '--abort')  # a conflict is never left for the session
    return False


#: The subject a cloud session's report commit carries (asf.workers.cloud.cloud_brief).
REPORT_SUBJECT = 'asf: report '


def _past_reports(path, sha):
    """``sha`` peeled past its trailing empty ``asf: report`` commits (first parent): a report
    commit is a session's end marker, never a base to rebase onto. One that carries work is a
    commit like any other."""
    for _ in range(20):
        p = subprocess.run(['git', 'log', '-1', '--format=%s%n%T%n%P', sha], cwd=path,
                           capture_output=True, text=True)
        subject, tree, parents = (p.stdout.split('\n') + ['', '', ''])[:3]
        parent = parents.split()[0] if parents.split() else ''
        if p.returncode != 0 or not subject.startswith(REPORT_SUBJECT) or not parent:
            return sha
        ptree = subprocess.run(['git', 'rev-parse', f'{parent}^{{tree}}'], cwd=path,
                               capture_output=True, text=True).stdout.strip()
        if ptree != tree:
            return sha
        sha = parent
    return sha


def trunk_rebase_needed(path, main):
    """Why the worktree's head must be rebased onto ``origin/<main>`` before a session starts on
    a branch origin already holds, or ``''`` when it need not be. Every such rebase is published
    at once, and every publish is a push that starts the product's CI on the PR and cancels the
    run already going there: measured on a product's PRs (2026-09-29), 25 of 57 superseded CI
    runs were superseded by this launch rebase alone, the branch's own changes byte-identical —
    each one heavy e2e minutes thrown away. A branch behind its trunk needs no rebase to be
    reviewed, tested or landed: a pull request's CI runs on the merge with its base, the merge
    queue builds its own batch refs, and a PR that stops merging cleanly is a CONFLICT → REBASE
    row of its own. So the head is rebased only for what a rebase alone clears:

    * trunk history on it — merge commits, or copies of trunk commits (``git cherry`` ``-``)
      past ``origin/<main>`` — which the lane would otherwise hold (B-0056, ``drop_copies``);
    * a head that does not merge cleanly into ``origin/<main>`` (``git merge-tree``): the
      session resolves on the trunk it must land on. A rebase that conflicts is still aborted.
    """
    def git(*args):
        return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)
    trunk = f'origin/{main}'
    merges = git('rev-list', '--merges', f'{trunk}..HEAD')
    if merges.returncode != 0 or merges.stdout.strip():
        return 'merge commits above the trunk'
    cherry = git('cherry', trunk, 'HEAD')
    if cherry.returncode != 0 or any(l.startswith('-') for l in cherry.stdout.splitlines()):
        return 'copies of trunk commits above the trunk'
    mt = git('merge-tree', '--write-tree', '--quiet', trunk, 'HEAD')
    if mt.returncode != 0:
        return CONFLICT_REASON
    return ''


def _rebase_onto_trunk(path, branch, main, kind=None, severity=None, product=None, flight=None):
    """Rebase the worktree onto the fetched trunk — for a branch already on origin only when
    :func:`trunk_rebase_needed` says so (a fresh, unpublished branch always: rebasing it pushes
    nothing). A conflict is aborted, never left in place. A rebase that completes and moves a
    branch already on origin is published by the factory at once
    (:func:`asf.workers.lifecycle.publish`, B-0056): the session then starts on a branch that
    origin holds, and its own pushes are fast-forwards — it never faces the non-fast-forward
    that made sessions merge their stale remote. A head the rebase is skipped for is origin's
    own (or a fast-forward of it), so the session's pushes are fast-forwards too.

    A branch already on origin with a ``ci.workflow`` run in flight is deferred instead
    (:func:`asf.ci_flight.verdict`, F-0203): the head is left exactly as origin holds it, so the
    deferral preserves the same fast-forward property the immediate publish existed to buy,
    because nothing moved. The one exception the launch site can claim is ``CONFLICT`` — a head
    that does not merge cleanly into the trunk is rebased at once regardless, S1 or not."""
    from asf import gitops  # the exact ref: ``archive/<branch>`` is not ``<branch>``
    ls = subprocess.run(['git', 'ls-remote', '--heads', 'origin', gitops.head_ref(branch)],
                        cwd=path, capture_output=True, text=True)
    remote_sha = gitops.head_sha(ls.stdout, branch) if ls.returncode == 0 else ''
    if remote_sha and not _catch_up(path, branch, remote_sha, kind, main):
        return  # behind origin and not caught up: never rebased or published from here
    why = trunk_rebase_needed(path, main) if remote_sha else 'a fresh branch'
    if remote_sha and why:
        needed = ci_flight.CONFLICT if why == CONFLICT_REASON else None
        line = ci_flight.verdict(product, branch, 'rebase', needed=needed, severity=severity,
                                 flight=flight)
        if line:
            print(line, file=sys.stderr)   # the launch's own channel, as its other lines
            return                          # the session starts on the branch origin holds (C6)
    if not remote_sha or why:
        r = subprocess.run(['git', 'rebase', '-q', f'origin/{main}'], cwd=path,
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(['git', 'rebase', '--abort'], cwd=path, capture_output=True)
            return
    if not remote_sha:
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


#: A label with no entry of its own falls back to this one. ``cheap`` is new in F-0093 and a pool
#: configured before it has no entry; falling back to ``light`` keeps that pool running at the
#: price it already paid, where refusing would stop the factory over a saving.
MODEL_FALLBACK = {'cheap': 'light'}


def model_arg(model, cfg=None):
    """``worker_pool.models: {heavy: <id>, light: <id>, cheap: <id>}`` maps a row's model label.
    A label with no entry falls back per :data:`MODEL_FALLBACK`; a label with neither is refused —
    the literal label is not a model id the runtime knows, and the session dies at once."""
    if not model:
        return None
    table = (((cfg or {}).get('worker_pool') or {}).get('models')) or {}
    if not isinstance(table, dict):  # a misshapen value is no entry, never a TypeError
        table = {}
    if model not in table:
        alt = MODEL_FALLBACK.get(model)
        if alt and alt in table:
            return table[alt]
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


#: The worktree's own record that the product's setup command ran in it: a JSON file in the
#: worktree's **git admin dir** (``<repo>/.git/worktrees/<name>/asf-setup``), never in the tree.
#: An untracked file in the tree would read as uncommitted work for ever, and no worktree would be
#: reaped again (:func:`asf.workers.worktrees.safety`); the admin dir is deleted with the tree by
#: ``git worktree remove`` and ``git worktree prune``, which is what makes a recreated path count
#: as fresh (F-0127, D1/D2).
SETUP_MARKER = 'asf-setup'


def setup_marker_path(worktree):
    """``worktree``'s setup marker, resolved through the tree's own ``.git`` file
    (:func:`_admin_dir`) so a suffixed admin dir resolves to its own; '' when there is none."""
    admin = _admin_dir(worktree)
    return os.path.join(admin, SETUP_MARKER) if admin else ''


def setup_done(worktree, command):
    """Whether ``command`` has already run in ``worktree``: its marker parses and names exactly
    this command.

    This is what *fresh* means (F-0127). A worktree with no marker has not been set up, whatever
    its path held before the launch — a reclaimed orphan is a new checkout at an old path, and the
    old admin dir went with the old tree (D1/D2). A marker naming a different command is a tree the
    product's current command has not run in (D3). An unreadable or malformed marker, or a tree
    with no admin dir, is not set up: running an idempotent command again is the safe answer, and
    handing a session a tree that may hold no dependencies is not (D8)."""
    path = setup_marker_path(worktree)
    if not path:
        return False
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('command') == command
    except (OSError, ValueError, AttributeError):
        return False


def _record_setup(worktree, command, took_s):
    """Write the marker: the command verbatim, the UTC time, the seconds it took. Returns the path,
    or '' when it could not be written — which is not a failed launch (D7): the setup did run, and
    the only cost of a lost marker is one repeat in that worktree."""
    path = setup_marker_path(worktree)
    if not path:
        return ''
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'command': command, 'at': pool_mod.now_iso(), 'took_s': took_s},
                      f, sort_keys=True)
    except OSError:
        return ''
    return path


def run_worktree_setup(product, job, worktree, account=None, passthrough=(),
                       timeout=WORKTREE_SETUP_TIMEOUT_S):
    """Run the product's ``conventions.worktree_setup`` (a shell command) in ``worktree``, under
    the environment the session itself will have (:func:`asf.workers.runtime.build_env`, worker
    mode: the allow-list, the account's HOME). Its output goes to ``briefs/<job>.setup.log``.
    Returns the seconds it took, or None when the product declares no command or this worktree
    already carries the marker for it (:func:`setup_done`) — a reused tree keeps the install it
    already has. A failure or a timeout removes the worktree — a tree without its setup is never
    handed over, nor reused as if it had one — and raises :class:`SpawnError` naming the command
    and its first line of error output."""
    command = getattr(product.conventions, 'worktree_setup', None)
    if not command:
        return None
    if setup_done(worktree, command):
        return None          # this tree already ran exactly this command (F-0127, D6)
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
        took = round(time.monotonic() - started, 1)
        _record_setup(worktree, command, took)
        return took
    with repo_lock(product.repo_dir):
        _discard(product, worktree)
    raise SpawnError(f'worktree_setup `{command}` failed in {worktree} ({why}) — log: {log}',
                     clear=f'fix `{command}` (conventions.worktree_setup in '
                           f'products/{product.name}.yaml), then relaunch')


def _discard(product, path):
    """``git worktree remove --force`` without the wait: the tree leaves git at once and the disk
    in the background (:mod:`asf.workers.trash`)."""
    from asf.workers import trash
    return trash.discard(product.repo_dir, env.state_dir(product), path, check_clean=False)


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
    refusal = spawn_refusal(product, row, branch)
    if refusal:  # before anything is made: a refused launch leaves nothing behind
        raise SpawnError(refusal)
    worktree = make_worktree(product, row.job, branch, kind=row.kind, severity=row.severity)
    cloud = getattr(runtime, 'lane', 'local') == 'cloud'
    setup_s = None
    if cloud:
        # the session runs off this host (asf.workers.cloud): nothing is set up or built here,
        # and the branch it checks out must be on origin — a fresh one is published first
        _publish_fresh_branch(product, worktree, branch)
    else:
        # whether this worktree is fresh is the worktree's own record, not whether its path
        # existed a moment ago: a reclaimed orphan is a new checkout at an old path, and that
        # launch used to skip its setup and hand over a tree with no dependencies (F-0127)
        setup_s = run_worktree_setup(product, row.job, worktree, account, passthrough)
    id_range = reserve_id_range(product, row.job,
                                prefixes=wp.get('id_range_prefixes') or DEFAULT_ID_PREFIXES,
                                start=int(wp.get('id_range_start', DEFAULT_ID_START)),
                                size=int(wp.get('id_range_size', DEFAULT_ID_SIZE)))
    # a correction judged on a head the branch has since moved past: the brief names the real
    # head and the commits since (the worktree's fetch above made origin/<branch> current)
    from asf.workers import judged
    brief_text = brief_text + judged.launch_section(pool_mod.sessions_path(product),
                                                    product.repo_dir, product.main, row.kind,
                                                    row.item, branch)
    started = pool_mod.now_iso()
    sid = lifecycle.session_id(product.name, row.job, started)
    # every runtime beats (asf.workers.heartbeat): the runtime hands the session the rule from
    # job.heartbeat — a local one after the brief, a cloud one in its CLOUD block
    beat = heartbeat.settings(cfg, product, 'cloud' if cloud else 'local')
    brief_path = write_brief(product, row.job, brief_for(row, brief_text))
    stopgate.clear(product, row.job)  # a correction round arrives with a fresh bound
    pushlog.clear(product, row.job)   # ... and counts its own pushes (one per correction round)
    add_dirs = [os.path.expanduser(d) for d in (product._get('job_grants') or [])]
    for d in getattr(row, 'add_dirs', None) or ():  # the row's own grants are the factory's dirs
        d = os.path.expanduser(d)
        os.makedirs(d, exist_ok=True)
        if d not in add_dirs:
            add_dirs.append(d)
    with _STATE_LOCK:
        hooks_dir = githooks.ensure(product)
        from asf import hooks as hooks_mod  # local: hooks imports pool, the launch's side
        hooks_mod.ensure_account_hooks(account)  # the Stop gate binds every session
    job = runtime_mod.Job(product.name, row.job, worktree, brief_path, model,
                          account=account, add_dirs=add_dirs,
                          permission_mode=wp.get('permission_mode')
                          or runtime_mod.DEFAULT_PERMISSION_MODE,
                          env={**env.worker_env(cfg, product),
                               **githooks.item_env(getattr(product, 'conventions', None),
                                                   row.item, branch),
                               **pushlog.env_for(product, row.job, row.kind),
                               'ASF_PUSH_ALLOW': push_allow(product, row, branch),
                               'BACKLOG_ID_RANGE': id_range, 'ASF_SESSION': sid},
                          settings_file=settings_file(wp), hooks_dir=hooks_dir,
                          passthrough=passthrough, product_auth_env=product_auth_env,
                          branch=branch, base=product.main,
                          setup=getattr(product.conventions, 'worktree_setup', None))
    job.heartbeat = beat
    result = runtime.run(job)
    record = {'job': row.job, 'item': row.item, 'feature': row.feature, 'kind': row.kind,
              'account': account.name if account else None, 'model': job.model,
              'pid': result.pid, 'pgid': result.pid, 'worktree': worktree, 'branch': branch,
              'started': started, 'log': result.log_path, 'brief': brief_path,
              'id_range': id_range, 'runtime': runtime.name, 'session': sid,
              'product': product.name, 'card_digest': getattr(row, 'card_digest', '') or '',
              'cause': getattr(row, 'cause', '') or '',
              'heartbeat_min': beat.interval_min}
    launch_head = _launch_head(product.repo_dir, branch)
    if launch_head:
        # the head a held branch was handed back on: the loop guard counts launches on one sha
        # (asf.workers.lifecycle.same_head_loop)
        record['launch_head'] = launch_head
    if setup_s is not None:
        record['setup_s'] = setup_s
    if getattr(row, 'host_load_bypass', False):
        # the S1 load-hold bypass (asf.tick.step_wave): at most one live at a time, across every
        # product — the field a later wave's s1_bypass_live() reads off the live ledger
        record['host_load_bypass'] = True
    record.update({k: v for k, v in (getattr(result, 'extra', None) or {}).items() if v is not None})
    # a launch line is a new run: the fold opens a run at every launch line, so the previous
    # run's terminal fields never reach this one (B-0041 — see asf.workers.lifecycle)
    pool_mod.append_session(product, record)
    pid = progress.start(product, record, cfg=cfg, cloud=cloud)
    if pid:
        pool_mod.update_session(product, row.job, progress_pid=pid)
    return record


def _launch_head(repo, branch):
    """``origin/<branch>``'s sha in ``repo`` as the launch found it (:func:`make_worktree` has
    fetched it), or None for a branch origin does not hold."""
    p = subprocess.run(['git', 'rev-parse', '--verify', '-q', f'refs/remotes/origin/{branch}'],
                       cwd=repo, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _publish_fresh_branch(product, worktree, branch):
    """A cloud session checks its branch out from origin: a branch origin does not hold yet is
    pushed there by the factory (:func:`asf.workers.lifecycle.publish`, the product's own pre-push
    hook included) before the launch. A refused push refuses the launch."""
    if _branch_exists_on_origin(worktree, branch):
        return
    ok, line = lifecycle.publish(worktree, branch, '', main=product.main,
                                 protected=refguard.listed(product.conventions))
    if not ok:
        raise SpawnError(f'cloud lane: {line}')


def load_cfg():
    try:
        return env.load_config()
    except env.ConfigError:
        return {}
