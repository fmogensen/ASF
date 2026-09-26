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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait

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
         spawn_fn=None, local_hold='', cloud_runtime=None, cloud_ready=None, refresh=None,
         local_seats=None):
    """Returns ``(launched, waits)``: lists of ``(row, record)`` and ``(row, reason)``.

    ``local_seats`` (None: no bound): the local lane's free seats this wave — the share less the
    local sessions live. ``n`` counts the cloud lane's seats beside the local ones, so without it
    the local accounts would take them too (2026-09-26: 10 local sessions on a share of 9); a
    row past it waits on ``no local seat`` or overflows to the cloud lane. A ``refresh`` row
    widens it as it widens ``n``.

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
    holds the tick while it runs; the rows past it wait for the next tick.

    ``refresh(known_jobs)`` (the wave step's re-read of the record, :mod:`asf.tick.step_wave`)
    returns rows that arrived since the wave began — an S1 minted mid-wave. It is asked every
    :data:`REFRESH_S` while launches run and once more before the wave ends; its rows go ahead
    of every row not yet decided and widen ``n`` by their count, so they launch in this wave."""
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
    pending = order(rows)      # rows not decided yet, in the order they are decided
    known = {r.job for r in rows}
    branches = {}              # branch → the job launching on it in this wave
    said = []                  # [row, reason] per decided row (reason None: a launch), in order
    tentative = []             # said entries of rows that found no seat while launches ran
    outcome, timings = {}, {}
    inflight = {}              # future → (row, acct, lane, seat, entry)
    raised = None
    last_refresh = time.monotonic()
    local_taken = 0            # local launches held (in flight or done) against ``local_seats``

    def fresh_rows():
        """``refresh``'s new rows (an S1 minted since the wave began), each job once."""
        nonlocal s1, n, last_refresh, local_seats
        last_refresh = time.monotonic()
        try:
            got = [r for r in (refresh(set(known)) or ()) if r.job not in known]
        except Exception as e:  # noqa: BLE001 — a failed re-read keeps the wave as it is
            out(f'wave: re-read for new S1 rows failed — {type(e).__name__}: {e}')
            return []
        for r in got:
            known.add(r.job)
        if got:
            s1 = s1 or pool_mod.s1_open(got)
            n += len(got)
            if local_seats is not None:
                local_seats += len(got)
        return order(got)

    def decide(row):
        """``(acct, lane, rt, reason)`` for one row: a seat, or why it waits."""
        nonlocal cloud_tries
        if len(launched) + len(inflight) >= n:
            return None, None, None, 'wave full'
        if (product.name, row.job) in running:
            return None, None, None, 'already running'
        b = _branch(product, row)
        if b in branches:
            return None, None, None, (f'already running: branch {b} launches in this wave '
                                      f'({branches[b]})')
        lane, acct, creason, reason = 'local', None, '', ''
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
            elif local_seats is not None and local_taken >= local_seats:
                acct, reason = None, (f'no local seat — the share has {local_seats} free this '
                                      f'wave, all taken')
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
        if acct is None:
            return None, None, None, reason
        rt = runtime
        if lane == 'cloud':
            rt = cloud_runtime or cloud_mod.lane_runtime(cloud, product)
            cloud_tries += 1
        return acct, lane, rt, ''

    def settle(fut):
        """One finished launch: its seat kept, or freed. True when a seat came free."""
        nonlocal raised, local_taken
        row, acct, lane, seat, entry, _seq = inflight.pop(fut)
        rec, err, secs = fut.result()
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
            outcome[id(entry)] = (None, f"launched {row.job:<24} {row.item:<10} → "
                                        f"{acct.name} ({rec.get('model')}) {where}{bypass}")
            timings[id(entry)] = f'launch {row.job}: setup {secs:.1f}s'
            return False
        pool.untake(acct, **seat)
        if lane != 'cloud':
            local_taken -= 1
        running.discard((product.name, row.job))
        branches.pop(_branch(product, row), None)
        if not isinstance(err, spawn_mod.SpawnError):
            raised = raised or err
            outcome[id(entry)] = (None, '')
        elif isinstance(err, spawn_mod.WorktreeBusy):
            outcome[id(entry)] = (f'already running: {err}', None)  # a live run holds the item
        else:
            reason = str(err) if str(err).startswith('NEEDS OPERATOR') else f'spawn failed: {err}'
            reason = failures.note(row.job, reason, getattr(err, 'clear', ''))
            # None: reported already and nothing changed — a wait that says nothing
            outcome[id(entry)] = ((f'spawn failed: {err}', '') if reason is None
                                  else (reason, None))
        return True

    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        while True:
            # 1. the seats, decided serially in row order — only as a launch slot is free, so a
            #    row that arrives mid-wave (an S1, ``refresh``) is decided ahead of the rest
            while pending and raised is None and len(inflight) < workers:
                if inflight and len(launched) + len(inflight) >= n:
                    break                       # full for now: a launch in flight may fail
                row = pending.pop(0)
                acct, lane, rt, reason = decide(row)
                entry = [row, reason]
                said.append(entry)
                if acct is not None:
                    seat = dict(model=_model(row, cfg), job=row.job, product=product.name,
                                kind=row.kind, lane=lane if lane == 'cloud' else None)
                    pool.take(acct, **seat)     # held while the launch runs, freed if it fails
                    if lane != 'cloud':
                        local_taken += 1
                    running.add((product.name, row.job))
                    branches[_branch(product, row)] = row.job
                    entry[1] = None
                    fut = ex.submit(_timed, spawn_fn, product, row, acct, brief_fn(row), rt, cfg)
                    inflight[fut] = (row, acct, lane, seat, entry, len(said))
                elif inflight and reason != 'wave full' and not reason.startswith('already'):
                    tentative.append(entry)     # a launch in flight may fail and free a seat
            if not inflight:
                if raised is None and not pending and refresh is not None:
                    pending.extend(fresh_rows())   # the last look before the wave ends
                if raised is None and pending:
                    continue
                break
            # 2. the launches' setup runs concurrently; each one settles as it ends
            done, _ = futures_wait(list(inflight), timeout=REFRESH_S, return_when=FIRST_COMPLETED)
            freed = False
            for fut in sorted(done, key=lambda f: inflight[f][5]):
                freed = settle(fut) or freed
            if freed and tentative:         # decided once more, as a serial wave would have
                again = [e[0] for e in tentative]
                for e in tentative:
                    said.remove(e)
                tentative.clear()
                pending[:0] = again
            if (refresh is not None and raised is None
                    and time.monotonic() - last_refresh >= REFRESH_S):
                pending[:0] = fresh_rows()
    finally:
        ex.shutdown(wait=True)
        pos = {e[0].job: i for i, e in enumerate(said)}
        launched.sort(key=lambda lr: pos.get(lr[0].job, len(pos)))   # row order, not end order
        for entry in said:
            reason, line = outcome.get(id(entry), (entry[1], None))
            row = entry[0]
            if reason is not None:
                waits.append((row, reason))
                if line is None:
                    line = f"waits    {row.job:<24} {row.item:<10} — {reason}"
            if line:
                out(line)
        for entry in said:
            if id(entry) in timings:
                out(timings[id(entry)])
    if raised is not None:
        raise raised
    if sample:
        headroom_mod.record_samples(dict(pool._usage))
    return launched, waits


#: how often a running wave asks ``refresh`` for rows that arrived since it began
REFRESH_S = 30

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


def _timed(spawn_fn, product, row, acct, brief, runtime, cfg):
    """``(record, error, seconds)`` of one launch — run on the wave's launch pool."""
    started = time.monotonic()
    try:
        rec = spawn_fn(product, row, acct, brief, runtime=runtime, cfg=cfg)
        return rec, None, time.monotonic() - started
    except Exception as e:  # noqa: BLE001 — the wave sorts a refusal from a fault
        return None, e, time.monotonic() - started
