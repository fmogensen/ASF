"""asf.tick.step_wave — the tick's ``wave`` step: what the feeder says to start, launched.

1. the index from the record clone (the tick's own, made once per tick — :class:`Context`);
2. ``inflight``: the sessions in ``~/.ASF/state/<product>/sessions.jsonl`` with no ``ended``; and
   ``busy``, the items whose pushed branch waits for harvest — no slot, but no second session;
3. ``feeder.plan_rows(index, product, inflight, capacity)`` — ``capacity`` is ``config.yaml
   feeder.capacity`` (default 4); the feeder takes the sessions in flight off it itself;
4. per launching row, a brief (``asf.briefs.build``) with the facts of its branch on the product
   repo's origin — whether it is pushed and its last commit, two ``git`` calls at most;
5. one ``workers.wave`` over every briefed row, so the pool's S1 reserve sees them all; it prints
   the ``launched`` / ``waits`` lines (``reserved for S1`` among them). A row the feeder holds
   back (``WAITS ON …``, no slot) prints its own ``waits`` line here.

Each launch appends a ``launch`` event (item, account, model, brief kind) to ``metrics/events``.
"""
import subprocess

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

DEFAULT_CAPACITY = 4


def capacity():
    v = (env.load_config().get('feeder') or {}).get('capacity')
    return v if isinstance(v, int) and v >= 0 else DEFAULT_CAPACITY


def inflight(product):
    """The feeder's ``inflight`` list off the session ledger — :func:`asf.workers.lifecycle.inflight`."""
    return lifecycle.inflight(pool_mod.sessions_path(product))


def attempts(product):
    """``{item: runs the ledger holds for it}`` — :func:`asf.workers.lifecycle.attempts`."""
    return lifecycle.attempts(pool_mod.sessions_path(product))


def awaiting_harvest(product):
    """Items whose pushed branch waits for harvest: busy, but holding no slot
    (:func:`asf.workers.lifecycle.awaiting_harvest`)."""
    return lifecycle.awaiting_harvest(pool_mod.sessions_path(product))


def corrections(product):
    """``{item: {kind, text, at, rounds}}`` — the newest correction still waiting for its session
    (:func:`asf.workers.lifecycle.corrections`)."""
    return lifecycle.corrections(pool_mod.sessions_path(product))


def _git(repo, args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def repo_facts(product, branch):
    """``{branch, pushed, remote_sha, last_commit}`` for ``branch`` on the product repo's origin."""
    repo = product.repo_dir
    if not repo or not branch:
        return {'branch': branch, 'pushed': False, 'remote_sha': '', 'last_commit': ''}
    heads = _git(repo, ['ls-remote', '--heads', 'origin', branch])
    sha = heads.split()[0] if heads else ''
    last = _git(repo, ['log', '-1', '--format=%H %cI %s', f'origin/{branch}']) if sha else ''
    return {'branch': branch, 'pushed': bool(sha), 'remote_sha': sha, 'last_commit': last}


def _build(*a, **kw):
    from asf import briefs
    return briefs.build(*a, **kw)


def _wave(*a, **kw):
    from asf.workers import wave
    return wave.wave(*a, **kw)


def job_name(brief_kind, item_id):
    return f'{brief_kind}-{item_id}'.lower()


def worker_row(row, brief, items):
    """The workers' row for a feeder row and its brief."""
    item = items.get(row.item_id) or {}
    state, _, action = row.kind.partition(' → ')
    return pool_mod.Row(job_name(brief.kind, row.item_id), row.item_id, state=state,
                        action=action, title=item.get('title', ''), model=brief.model,
                        kind=brief.kind, severity=item.get('severity'),
                        feature=row.feature_id or None, branch=row.branch or None)


def run(ctx, out=print):
    from asf.feeder import rows as feeder_rows
    from asf.views import index_reader
    product = ctx.product
    items, _generated = index_reader.load(ctx.record_root())
    running = inflight(product)
    planned = feeder_rows.plan_rows(items, product, running, capacity(), attempts=attempts(product),
                                     corrections=corrections(product), busy=awaiting_harvest(product))
    worker_rows, texts, kinds = [], {}, {}
    for row in planned:
        if not row.launches:
            out(f"waits    {'-':<24} {row.item_id:<10} — {row.action}")
            continue
        brief = _build(product, row, items, running,
                       repo_facts=repo_facts(product, row.branch))
        wrow = worker_row(row, brief, items)
        worker_rows.append(wrow)
        texts[wrow.job] = brief.text
        kinds[wrow.job] = brief.kind
    if not worker_rows:
        out('wave: nothing to launch')
        return 0
    launched, _waits = _wave(product, worker_rows, len(worker_rows),
                             brief_fn=lambda r: texts[r.job], out=out)
    for wrow, rec in launched:
        ctx.event('launch', item=wrow.item, job=wrow.job,
                  model=rec.get('model'), brief_kind=kinds[wrow.job])
    ctx.counts['launches'] += len(launched)
    return 0
