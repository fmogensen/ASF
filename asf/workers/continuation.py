"""asf.workers.continuation — may this row be answered by continuing a session (F-0039).

One rule, one reason string, three readers: the wave, ``asf brief --continue`` and the tests.
The rule makes exactly one git call (:func:`resumable`, the trailers on the branch above the
trunk) and no decision the feeder could have made, because the feeder may read neither the
ledger nor git (P5).

``target(product, row, root)`` is the question the wave asks before it builds a brief:
``(run, session_id, round, review_path)`` when the row's branch has a writer whose conversation
may be continued, else ``(None, None, 0, why)`` — and the row launches as it always did.

``resumable(product, run, now)`` runs these tests in this order, each with the reason it prints:

* no run on the branch → ``no session on <branch>``
* :func:`dead` — live, and its log silent past :func:`heartbeat_min` → ``heartbeat stale (14m > 6m)``
* still live → ``still running``
* no ``runtime_session`` recorded on the run → ``no runtime session id recorded``
* the run's worktree is gone → ``worktree reaped``
* an ``ASF-Session`` trailer other than the run's own above the trunk → ``another session moved
  the branch``
* harvested → ``already landed``
* the run's account has left ``worker_pool.accounts`` → ``account <name> no longer in the pool``

A run that *ended* is not dead: its conversation is exactly what a correction wants, and the
heartbeat is the liveness test for a run that is still supposed to be running.
"""
import os
import re
import subprocess
import time

from asf.feeder import rows as feeder_rows
from asf.views import index_reader
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import spawn as spawn_mod

#: Row kinds that may answer a branch by continuing its session. Everything else launches.
ANSWERING = ('correct', 'spec', 'plan')
#: Never continued, whatever the branch says (§1.4).
NEVER = ('adjudicate', 'review', 'groom', 'reshape', 'rebase', 'close', 'fix-bug', 'task')

DEFAULT_HEARTBEAT_MIN = 6

_DURATION_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$')
_UNIT_MIN = {'s': 1 / 60, 'm': 1, '': 1, 'h': 60, 'd': 1440}


def heartbeat_min(product):
    """``stage_limits.heartbeat_min`` — an int is minutes, a duration string
    (``90s``/``6m``/``1h``) works too, exactly as ``stall.silent_minutes`` reads its own."""
    v = (product.stage_limits or {}).get('heartbeat_min', DEFAULT_HEARTBEAT_MIN)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _DURATION_RE.match(str(v))
    if not m:
        return float(DEFAULT_HEARTBEAT_MIN)
    return float(m.group(1)) * _UNIT_MIN[m.group(2)]


def writer(product, branch):
    """The latest run on ``branch`` (:func:`lifecycle.by_branch`), or None. A branch's writer is
    whoever holds it now — a held ``spec/F-0039`` is corrected on ``spec/F-0039`` (the lane state
    machine's own ruling), so this is the same run the correction was written on."""
    return lifecycle.by_branch(pool_mod.sessions_path(product)).get(branch)


def dead(run, product, now):
    """The card's dead session: live (no ``ended`` line) and its log silent past
    :func:`heartbeat_min`. ``(True, 'heartbeat stale (14m > 6m)')`` or ``(False, '')``."""
    if not lifecycle.is_live(run):
        return False, ''
    try:
        mtime = os.path.getmtime(run.get('log'))
    except (OSError, TypeError):
        return False, ''
    limit = heartbeat_min(product)
    silent = (now - mtime) / 60
    if silent <= limit:
        return False, ''
    return True, f'heartbeat stale ({int(silent)}m > {limit:g}m)'


def _other_session(product, run, repo):
    """True when a commit on ``origin/<branch>`` above the trunk carries an ``ASF-Session``
    trailer that is not this run's own (P6). The module's one git call."""
    repo = repo or product.repo_dir
    if not repo:
        return False
    p = subprocess.run(
        ['git', '-C', repo, 'log', '--format=%(trailers:key=ASF-Session,valueonly)',
         f'origin/{product.main}..origin/{run["branch"]}'],
        capture_output=True, text=True)
    if p.returncode != 0:
        return False
    return any(line.strip() and line.strip() != run.get('session')
               for line in p.stdout.splitlines())


def resumable(product, run, now, repo=None, cfg=None, branch=None):
    """``(session_id, '')`` when ``run``'s conversation may be continued, else ``(None, why)``.
    The tests, in order, are the module docstring's. ``branch`` names the branch a missing
    ``run`` was looked up under; ``cfg`` is the operator config (default: the file)."""
    if not run:
        return None, f'no session on {branch}' if branch else 'no session on the branch'
    is_dead, why = dead(run, product, now)
    if is_dead:
        return None, why
    if lifecycle.is_live(run):
        return None, 'still running'
    if not run.get('runtime_session'):
        return None, 'no runtime session id recorded'
    if not run.get('worktree') or not os.path.isdir(run['worktree']):
        return None, 'worktree reaped'
    if _other_session(product, run, repo):
        return None, 'another session moved the branch'
    if lifecycle.landed(run):
        return None, 'already landed'
    name = run.get('account')
    if name:
        cfg = spawn_mod.load_cfg() if cfg is None else cfg
        if name not in {a.name for a in pool_mod.accounts_from_config(cfg)}:
            return None, f'account {name} no longer in the pool'
    return run['runtime_session'], ''


def target(product, row, root, runs=None, now=None, repo=None, cfg=None):
    """``(run, session_id, round, review_path)`` for a row that may be continued, else
    ``(None, None, 0, why)``. ``row.brief_kind`` in ``NEVER`` → ``'kind <k> never continues'``;
    a ``spec``/``plan`` row whose Feature is not at ``<doc>-review rN`` → ``'no review round'``
    (nothing to send: that row is a first draft, not an answer). ``runs`` is a
    ``{branch: run}`` map (default: the registry's); the round is N, never N+1 (PD13)."""
    kind = row.brief_kind
    if kind in NEVER or kind not in ANSWERING:
        return None, None, 0, f'kind {kind} never continues'
    rnd, review_path = 0, ''
    if kind in ('spec', 'plan'):
        try:
            items, _ = index_reader.load(root)
        except (OSError, ValueError, KeyError):
            items = {}
        doc, rnd = feeder_rows.review_round(items.get(row.feature_id or row.item_id) or {})
        if doc != kind:
            return None, None, 0, 'no review round'
        review_path = product.conventions.review_path(row.item_id.lower(), rnd)
    now = time.time() if now is None else now
    run = writer(product, row.branch) if runs is None else runs.get(row.branch)
    sid, why = resumable(product, run, now, repo=repo, cfg=cfg, branch=row.branch)
    if not sid:
        return None, None, 0, why
    return run, sid, rnd, review_path
