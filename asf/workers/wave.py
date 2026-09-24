"""asf.workers.wave — launch up to ``n`` of the feeder's rows, honouring pool, quota and the S1
reserve. Prints one ``launched`` or ``waits`` line per row considered:

    launched fix-b-0012   B-0012  → acct-a (opus) pid 4242
    waits    spec-f-0031  F-0031  — reserved for S1

``BUG → FIX`` rows go first (S1 before the rest), then the feeder's own order. A row whose job
already has a live session *of this product* waits with ``already running``: the check is on
``(product, job)``, so ``spec-f-0001`` live under product ``b`` never blocks ``a``'s own
(F-0076 D5). Once ``n`` launched, the remaining rows wait with ``wave full``.

The pool's load still spans every product and the machine's own sessions. When that session
table could not be read, the wave prints the degraded-count line once, before the first row::

    pool: sessions unreadable (<why>) — counting registered sessions only
"""
import json
import os

from asf import env
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
         spawn_fn=None):
    """Returns ``(launched, waits)``: lists of ``(row, record)`` and ``(row, reason)``."""
    cfg = spawn_mod.load_cfg() if cfg is None else cfg
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
            acct, reason = pool.pick_account(row.kind, row.model, is_fix=row.is_fix,
                                              s1_is_open=s1, lane=row.lane)
            if acct is not None:
                try:
                    rec = spawn_fn(product, row, acct, brief_fn(row), runtime=runtime, cfg=cfg)
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
                    pool.take(acct, rec.get('model'), row.job, product=product.name)
                    running.add((product.name, row.job))
                    launched.append((row, rec))
                    out(f"launched {row.job:<24} {row.item:<10} → {acct.name} "
                        f"({rec.get('model')}) pid {rec.get('pid')}")
                    continue
        waits.append((row, reason))
        out(f"waits    {row.job:<24} {row.item:<10} — {reason}")
    return launched, waits
