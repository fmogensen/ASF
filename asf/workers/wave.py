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

The pool's load still spans every product and the machine's own sessions. When that session
table could not be read, the wave prints the degraded-count line once, before the first row::

    pool: sessions unreadable (<why>) — counting registered sessions only
"""
import json
import os

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
    prints ``cloud lane unready: <why> — local lane only`` once and takes no row."""
    cfg = spawn_mod.load_cfg() if cfg is None else cfg
    cloud = cloud_mod.settings(cfg, product)
    if cloud.on:
        ready, why = cloud_ready if cloud_ready is not None else cloud_mod.readiness(cfg, product)
        if not ready:
            out(f'cloud lane unready: {why} — local lane only')
    else:
        ready = False
    cloud_open = cloud.on and ready
    sample = pool is None  # a tick's own pool: its readings are history (asf.workers.headroom)
    pool = pool or pool_mod.Pool.from_config(cfg, product)
    spawn_fn = spawn_fn or spawn_mod.spawn
    s1 = pool_mod.s1_open(rows)
    running = {(s.get('product') or product.name, s.get('job')) for s in pool.live}
    launched, waits = [], []
    failures = Failures(product)
    if getattr(pool, 'unreadable', ''):
        out(f'pool: sessions unreadable ({pool.unreadable}) — counting registered sessions only')
    for row in order(rows):
        if len(launched) >= n:
            reason = 'wave full'
        elif (product.name, row.job) in running:
            reason = 'already running'
        else:
            lane, acct, creason = 'local', None, ''
            first = cloud_open and cloud_mod.first(row, cloud)
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
            if acct is None and not first and cloud_open and cloud_mod.eligible(row, cloud):
                cacct, creason = pool.pick_cloud(row.kind, row.model, cloud)
                if cacct is not None:
                    acct, lane = cacct, 'cloud'
                else:
                    reason = f'{reason}; {creason}'
            if acct is not None:
                rt = runtime
                if lane == 'cloud':
                    rt = cloud_runtime or cloud_mod.lane_runtime(cloud, product)
                try:
                    rec = spawn_fn(product, row, acct, brief_fn(row), runtime=rt, cfg=cfg)
                except spawn_mod.WorktreeBusy as e:
                    reason = f'already running: {e}'  # a live run holds the item: a wait
                except spawn_mod.SpawnError as e:
                    reason = str(e) if str(e).startswith('NEEDS OPERATOR') else f'spawn failed: {e}'
                    reason = failures.note(row.job, reason, getattr(e, 'clear', ''))
                    if reason is None:  # reported already, nothing changed: say nothing
                        waits.append((row, f'spawn failed: {e}'))
                        continue
                else:
                    failures.clear(row.job)
                    pool.take(acct, rec.get('model'), row.job, product=product.name,
                              kind=row.kind, lane=lane if lane == 'cloud' else None)
                    running.add((product.name, row.job))
                    launched.append((row, rec))
                    url = rec.get('cloud_url') or rec.get('actions_run_name') or 'dispatched'
                    where = f'cloud {url}' if lane == 'cloud' else f"pid {rec.get('pid')}"
                    bypass = ' (S1: passes host load hold)' if getattr(row, 'host_load_bypass',
                                                                       False) else ''
                    out(f"launched {row.job:<24} {row.item:<10} → {acct.name} "
                        f"({rec.get('model')}) {where}{bypass}")
                    continue
        waits.append((row, reason))
        out(f"waits    {row.job:<24} {row.item:<10} — {reason}")
    if sample:
        headroom_mod.record_samples(dict(pool._usage))
    return launched, waits
