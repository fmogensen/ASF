"""asf.workers.wave — launch up to ``n`` of the feeder's rows, honouring pool, quota and the S1
reserve. Prints one ``launched`` or ``waits`` line per row considered:

    launched fix-b-0012   B-0012  → acct-a (opus) pid 4242
    waits    spec-f-0031  F-0031  — reserved for S1

A launch goes to an account only while its 5h window has room for it — the reading, this wave's
launches, an allowance for its running sessions and this launch's estimate stay under the guard
(:meth:`asf.workers.pool.Pool.headroom`); a row that fits nowhere waits::

    waits    spec-f-0031  F-0031  — headroom: acct-a would exceed 65% (now 51%, +10% committed, +10% this launch)

``BUG → FIX`` rows go first (S1 before the rest), then the feeder's own order. A row whose job
already has a live session *of this product* waits with ``already running``: the check is on
``(product, job)``, so ``spec-f-0001`` live under product ``b`` never blocks ``a``'s own
(F-0076 D5). Once ``n`` launched, the remaining rows wait with ``wave full``.

A row the wave step (:mod:`asf.tick.step_wave`) marked ``host_load_bypass`` — the one S1 row that
passed the host guard's LOAD hold — prints ``(S1: passes host load hold)`` after its ``launched``
line, so the bypass is visible, not just inferred.

**Launches run concurrently** (F-0161). The seats are decided serially, in row order — each
planned launch holds its seat (:meth:`asf.workers.pool.Pool.take`) — and then the launches'
setup (worktree, the rebase's publish through the product's pre-push hook, the worktree setup,
the cloud create) runs up to ``worker_pool.launch_concurrency`` (default 4) at a time. A launch
that fails frees its seat (:meth:`~asf.workers.pool.Pool.untake`), and the rows it could have gone
to are decided again after it, as a serial wave would have; two rows on one branch never launch
in one wave (the second ``already running: branch …``). The lines print in row order, and each
launch then prints what its setup took::

    launch fix-b-0012: setup 41.3s

The pool's load still spans every product and the machine's own sessions. When that session
table could not be read, the wave prints the degraded-count line once, before the first row::

    pool: sessions unreadable (<why>) — counting registered sessions only
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from asf import env
from asf.workers import cloud as cloud_mod
from asf.workers import headroom as headroom_mod
from asf.workers import pool as pool_mod
from asf.workers import spawn as spawn_mod


def default_brief(row):
    lines = [f'{row.state} → {row.action} {row.item} "{row.title}"'.strip()]
    if row.feature:
        lines.append(f'Feature: {row.feature}')
    return '\n'.join(lines) + '\n'


class Failures:
    """The spawn failures of past waves, per job, in ``<state>/spawn-failures.json``: a spawn
    that fails the same way tick after tick is not news. The first failure prints as it is; the
    second identical one prints once as ``NEEDS OPERATOR`` with the one command that clears it;
    after that the row waits silently until the failure changes or the spawn succeeds. A state
    dir that cannot be read or written only loses the memory — every failure prints."""

    def __init__(self, product):
        try:
            self.path = os.path.join(env.state_dir(product), 'spawn-failures.json')
            with open(self.path, encoding='utf-8') as f:
                self.seen = json.load(f)
        except (OSError, ValueError, TypeError, AttributeError):
            self.seen = {}
            self.path = getattr(self, 'path', None)
        if not isinstance(self.seen, dict):
            self.seen = {}

    def _save(self):
        if not self.path:
            return
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.seen, f, sort_keys=True, indent=1)
        except OSError:
            pass

    def note(self, job, reason, clear=''):
        """The line to print for ``job`` failing with ``reason`` — or None to print nothing."""
        prev = self.seen.get(job) or {}
        count = prev.get('count', 0) + 1 if prev.get('reason') == reason else 1
        self.seen[job] = {'reason': reason, 'count': count}
        self._save()
        if count == 1 or reason.startswith('NEEDS OPERATOR'):
            return reason
        if count == 2:
            gap = reason.split('spawn failed: ', 1)[-1]
            fix = clear or f'asf sessions  # then clear what holds {job}'
            return f'NEEDS OPERATOR: {job} fails to spawn each tick: {gap} — {fix}'
        return None

    def clear(self, job):
        if self.seen.pop(job, None) is not None:
            self._save()


def order(rows):
    return sorted(rows, key=lambda r: (0 if r.is_s1_fix else 1 if r.is_fix else 2))


def wave(product, rows, n, pool=None, runtime=None, cfg=None, brief_fn=default_brief, out=print,
         spawn_fn=None, local_hold='', cloud_runtime=None, cloud_ready=None):
    """Returns ``(launched, waits)``: lists of ``(row, record)`` and ``(row, reason)``.

    ``local_hold`` (host pressure's reason) keeps every row off the local lane — never off the
    cloud lane. With ``cloud.default: true`` a row :func:`asf.workers.cloud.first` names goes to
    the cloud lane first and to the local lane when the cloud is full; otherwise a row the local
    lane cannot take goes to the cloud lane when it is on and the row is eligible
    (:mod:`asf.workers.cloud`). Cloud launches go through ``cloud_runtime`` (default: the
    configured one); the line reads ``launched … → <acct> (<model>) cloud <session url>``.

    ``cloud_ready`` is the lane's ``(ready, why)`` (:func:`asf.workers.cloud.readiness`), read
    once per tick by the caller; ``None`` reads it here, once for the whole wave. An unready lane
    prints ``cloud lane unready: <why> — local lane only`` once and takes no row.

    ``cloud.max_creates_per_tick`` (0: no limit) bounds the cloud launches one wave tries — each
    holds the tick while it runs; the rows past it wait for the next tick."""
    cfg = spawn_mod.load_cfg() if cfg is None else cfg
    cloud = cloud_mod.settings(cfg, product)
    if cloud.on:
        ready, why = cloud_ready if cloud_ready is not None else cloud_mod.readiness(cfg, product)
        if not ready:
            out(f'cloud lane unready: {why} — local lane only')
    else:
        ready = False
    cloud_open = cloud.on and ready
    cloud_cap, cloud_tries = cloud.max_creates_per_tick, 0
    sample = pool is None  # a tick's own pool: its readings are history (asf.workers.headroom)
    pool = pool or pool_mod.Pool.from_config(cfg, product)
    spawn_fn = spawn_fn or spawn_mod.spawn
    s1 = pool_mod.s1_open(rows)
    running = {(s.get('product') or product.name, s.get('job')) for s in pool.live}
    launched, waits = [], []
    failures = Failures(product)
    if getattr(pool, 'unreadable', ''):
        out(f'pool: sessions unreadable ({pool.unreadable}) — counting registered sessions only')
    workers = launch_concurrency(cfg)
    pending = order(rows)
    branches = {}          # branch → the job launching on it in this wave
    idx = 0
    while idx < len(pending):
        # 1. the seats, decided serially in row order; each planned launch holds its seat
        batch, said = [], []   # batch: planned launches; said: [row, wait reason | None]
        while idx < len(pending):
            row = pending[idx]
            if len(launched) + len(batch) >= n:
                if batch:          # a launch in flight may fail and free its seat: decide later
                    break
                reason = 'wave full'
            elif (product.name, row.job) in running:
                reason = 'already running'
            elif _branch(product, row) in branches:
                b = _branch(product, row)
                reason = f'already running: branch {b} launches in this wave ({branches[b]})'
            else:
                lane, acct, creason = 'local', None, ''
                capped = cloud_open and bool(cloud_cap) and cloud_tries >= cloud_cap
                cloud_now = cloud_open and not capped
                first = cloud_now and cloud_mod.first(row, cloud)
                if first:                           # cloud.default: the cloud lane before the local
                    acct, creason = pool.pick_cloud(row.kind, row.model, cloud)
                    if acct is not None:
                        lane = 'cloud'
                if acct is None:
                    if local_hold:
                        acct, reason = None, f'held: {local_hold}'
                    else:
                        acct, reason = pool.pick_account(
                            row.kind, row.model, is_fix=row.is_fix, s1_is_open=s1,
                            lane=row.lane or ('local' if cloud.on else None))
                    if acct is None and first:
                        reason = f'{creason}; {reason}'
                if acct is None and capped and cloud_mod.eligible(row, cloud):
                    reason = (f'{reason}; cloud lane: {cloud_tries} launches this tick '
                              f'(cloud.max_creates_per_tick)')
                if acct is None and not first and cloud_now and cloud_mod.eligible(row, cloud):
                    cacct, creason = pool.pick_cloud(row.kind, row.model, cloud)
                    if cacct is not None:
                        acct, lane = cacct, 'cloud'
                    else:
                        reason = f'{reason}; {creason}'
                if acct is not None:
                    rt = runtime
                    if lane == 'cloud':
                        rt = cloud_runtime or cloud_mod.lane_runtime(cloud, product)
                        cloud_tries += 1
                    seat = dict(model=_model(row, cfg), job=row.job, product=product.name,
                                kind=row.kind, lane=lane if lane == 'cloud' else None)
                    pool.take(acct, **seat)     # held while the launch runs, freed if it fails
                    running.add((product.name, row.job))
                    branches[_branch(product, row)] = row.job
                    batch.append((row, acct, lane, rt, seat))
                    said.append([row, None])
                    idx += 1
                    continue
                if batch:
                    # no seat while launches are in flight: one of them may fail and free one —
                    # the row is decided again after them (``tentative``), as a serial wave would
                    said.append([row, reason, 'tentative'])
                    idx += 1
                    continue
            said.append([row, reason])
            idx += 1
        # 2. the launches' setup, concurrently: worktree, publish, install, cloud create
        done = _launch_all(batch, lambda b: spawn_fn(product, b[0], b[1], brief_fn(b[0]),
                                                     runtime=b[3], cfg=cfg), workers)
        # 3. the outcomes
        outcome, timings, raised, freed = {}, [], None, False
        for (row, acct, lane, rt, seat), (rec, err, secs) in zip(batch, done):
            if err is None:
                failures.clear(row.job)
                if rec.get('model') != seat['model']:  # the seat carries the launch's own model
                    pool.untake(acct, **seat)
                    pool.take(acct, **dict(seat, model=rec.get('model')))
                launched.append((row, rec))
                url = rec.get('cloud_url') or rec.get('actions_run_name') or 'dispatched'
                where = f'cloud {url}' if lane == 'cloud' else f"pid {rec.get('pid')}"
                bypass = ' (S1: passes host load hold)' if getattr(row, 'host_load_bypass',
                                                                   False) else ''
                outcome[row.job] = (None, f"launched {row.job:<24} {row.item:<10} → "
                                          f"{acct.name} ({rec.get('model')}) {where}{bypass}")
                timings.append(f'launch {row.job}: setup {secs:.1f}s')
                continue
            pool.untake(acct, **seat)
            running.discard((product.name, row.job))
            branches.pop(_branch(product, row), None)
            freed = True
            if not isinstance(err, spawn_mod.SpawnError):
                raised = raised or err
                outcome[row.job] = (None, '')
            elif isinstance(err, spawn_mod.WorktreeBusy):
                outcome[row.job] = (f'already running: {err}', None)  # a live run holds it
            else:
                reason = (str(err) if str(err).startswith('NEEDS OPERATOR')
                          else f'spawn failed: {err}')
                reason = failures.note(row.job, reason, getattr(err, 'clear', ''))
                # None: reported already and nothing changed — a wait that says nothing
                outcome[row.job] = ((f'spawn failed: {err}', '') if reason is None
                                    else (reason, None))
        again = []
        for entry in said:
            row = entry[0]
            if len(entry) == 3 and freed and raised is None:
                again.append(row)          # a seat came free: decided once more, next round
                continue
            reason, line = outcome.get(row.job, (entry[1], None))
            if reason is not None:
                waits.append((row, reason))
                line = f"waits    {row.job:<24} {row.item:<10} — {reason}" if line is None else line
            if line:
                out(line)
        for line in timings:
            out(line)
        if raised is not None:
            raise raised
        pending[idx:idx] = again
    if sample:
        headroom_mod.record_samples(dict(pool._usage))
    return launched, waits


#: launches whose setup runs at once when ``worker_pool.launch_concurrency`` is not set
DEFAULT_LAUNCH_CONCURRENCY = 4


def launch_concurrency(cfg):
    """``worker_pool.launch_concurrency`` (default :data:`DEFAULT_LAUNCH_CONCURRENCY`; 1: one
    launch at a time)."""
    raw = ((cfg or {}).get('worker_pool') or {}).get('launch_concurrency',
                                                    DEFAULT_LAUNCH_CONCURRENCY)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_LAUNCH_CONCURRENCY


def _branch(product, row):
    try:
        return spawn_mod.branch_for(product, row)
    except Exception:  # a product with no branch prefixes: the job is its own key
        return f'job:{row.job}'


def _model(row, cfg):
    """The model a launch of ``row`` records (:func:`asf.workers.spawn.model_arg`) — the one its
    seat is held under while the launch runs; a label with no entry keeps the label (the launch
    is refused and its seat freed)."""
    try:
        return spawn_mod.model_arg(row.model, cfg)
    except spawn_mod.SpawnError:
        return row.model


def _launch_all(batch, launch, workers):
    """``[(record, error, seconds)]`` for ``batch``, in its order: each item through ``launch``,
    up to ``workers`` at a time."""
    def one(item):
        started = time.monotonic()
        try:
            return launch(item), None, time.monotonic() - started
        except Exception as e:  # noqa: BLE001 — the caller sorts a refusal from a fault
            return None, e, time.monotonic() - started
    if workers <= 1 or len(batch) <= 1:
        return [one(item) for item in batch]
    with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as ex:
        return list(ex.map(one, batch))
