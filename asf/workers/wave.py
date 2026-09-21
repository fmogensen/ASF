"""asf.workers.wave — launch up to ``n`` of the feeder's rows, honouring pool, quota and the S1
reserve. Prints one ``launched`` or ``waits`` line per row considered:

    launched fix-b-0012   B-0012  → acct-a (opus) pid 4242
    waits    spec-f-0031  F-0031  — reserved for S1

``BUG → FIX`` rows go first (S1 before the rest), then the feeder's own order. A row whose job
already has a live session waits with ``already running``; once ``n`` launched, the remaining
rows wait with ``wave full``.
"""
from asf.workers import pool as pool_mod
from asf.workers import spawn as spawn_mod


def default_brief(row):
    lines = [f'{row.state} → {row.action} {row.item} "{row.title}"'.strip()]
    if row.feature:
        lines.append(f'Feature: {row.feature}')
    return '\n'.join(lines) + '\n'


def order(rows):
    return sorted(rows, key=lambda r: (0 if r.is_s1_fix else 1 if r.is_fix else 2))


def wave(product, rows, n, pool=None, runtime=None, cfg=None, brief_fn=default_brief, out=print,
         spawn_fn=None):
    """Returns ``(launched, waits)``: lists of ``(row, record)`` and ``(row, reason)``."""
    cfg = spawn_mod.load_cfg() if cfg is None else cfg
    pool = pool or pool_mod.Pool.from_config(cfg, product)
    spawn_fn = spawn_fn or spawn_mod.spawn
    s1 = pool_mod.s1_open(rows)
    running = {s.get('job') for s in pool.live}
    launched, waits = [], []
    for row in order(rows):
        if len(launched) >= n:
            reason = 'wave full'
        elif row.job in running:
            reason = 'already running'
        else:
            acct, reason = pool.pick_account(row.kind, row.model, is_fix=row.is_fix,
                                              s1_is_open=s1, lane=row.lane)
            if acct is not None:
                try:
                    rec = spawn_fn(product, row, acct, brief_fn(row), runtime=runtime, cfg=cfg)
                except spawn_mod.SpawnError as e:
                    reason = str(e) if str(e).startswith('NEEDS OPERATOR') else f'spawn failed: {e}'
                else:
                    pool.take(acct, rec.get('model'), row.job)
                    running.add(row.job)
                    launched.append((row, rec))
                    out(f"launched {row.job:<24} {row.item:<10} → {acct.name} "
                        f"({rec.get('model')}) pid {rec.get('pid')}")
                    continue
        waits.append((row, reason))
        out(f"waits    {row.job:<24} {row.item:<10} — {reason}")
    return launched, waits
