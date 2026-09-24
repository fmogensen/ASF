"""asf.tick.widen_footprint — ``widen_footprint`` applied: facts read, the rule run, its answer written.

Run by the ``health`` step, after health has ended the sessions and before the wave plans:

1. :func:`report_facts` — a finished coder or correct run of a Task whose REPORT names paths
   outside the Task's ``writes:`` (``needs writes:``, else ``left out:`` of a partial report,
   :func:`asf.workers.report.footprint_claim`), each resolved to the one tracked repo path it
   names, is held with a ``footprint`` correction (:func:`asf.workers.lifecycle.footprint_hold`)
   — before harvest can land a Task its own session says is not whole. Harvest writes the same
   correction for the gate's fact (:func:`asf.harvest.harvest.widen_candidates`).
2. :func:`apply` — every pending ``footprint`` correction gets the rule's verdict
   (:func:`asf.feeder.widen.decide`):

   * ``widen``: the paths are added to the Task's ``writes:`` in the record clone through the
     parser (:func:`asf.record.setfield.set_typed`), with one History line ``footprint widened:
     +<paths> (<fact>)``, committed alone; the index is re-derived so this tick's wave briefs the
     wider footprint; the run's correction now carries the new ``writes:`` and the failing tests,
     and the feeder relaunches the same run as its FIX → CORRECT row;
   * ``reshape`` (a second widening, or more than ``conventions.widen_max_files`` paths): the
     Task's ``reshape:`` is set to ``footprint: needs <paths>`` and the feeder gives it the RESHAPE
     row — the existing reshape machinery, never an operator question;
   * ``approval``: a path under an approvals-protected glob — a hold of that class is refused into
     the approvals ledger (``asf approvals resolve <item>/<class> granted`` lets the widening go
     on next tick);
   * ``waits``: a path overlaps a running Task's ``writes:`` — re-decided every tick until it is free.
"""
import os
import subprocess

from asf import approvals
from asf.feeder import rows as feeder_rows
from asf.feeder import widen
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import report as report_mod
from asf.workers import runtime as runtime_mod

#: The run kinds whose REPORT may claim footprint: a coder building a Task, a correction of it.
CLAIM_KINDS = ('coder', 'task', 'correct')
#: Verdicts the rule does not revisit: the record already carries them.
FINAL = (widen.WIDEN, widen.RESHAPE)


def _git(repo, args):
    p = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def tracked_paths(product, branch):
    """The paths ``origin/<branch>`` tracks in the product repo (else ``origin/<main>``'s), as a
    set; None when there is no repo to read."""
    repo = getattr(product, 'repo_dir', None)
    if not repo or not os.path.isdir(repo):
        return None
    for ref in (f'origin/{branch}' if branch else None, f'origin/{product.conventions.main}'):
        if ref:
            listed = _git(repo, ['ls-tree', '-r', '--name-only', ref])
            if listed is not None:
                return set(l for l in listed.splitlines() if l.strip())
    return None


def _task(items, item_id):
    item = (items or {}).get(item_id or '') or {}
    return item if item.get('type') == 'task' and not lifecycle.closed_state(items, item_id) else None


def report_facts(ctx, items, out=print, tracked_fn=tracked_paths):
    """Hold every finished coder/correct run whose REPORT claims paths outside its Task's
    ``writes:``. A run whose claim was read is marked ``footprint_read`` and not read again (one
    that claims nothing costs one log read a tick while it waits for harvest). Returns the jobs
    held."""
    product = ctx.product
    path = pool_mod.sessions_path(product)
    held = []
    for job, run in sorted(lifecycle.latest(path).items()):
        if run.get('kind') not in CLAIM_KINDS or run.get('footprint_read'):
            continue
        if run.get('end_reason') != lifecycle.FINISHED or lifecycle.landed(run):
            continue
        if lifecycle.pending_correction(run, path):
            continue
        task = _task(items, run.get('item'))
        if task is None:
            continue
        rec = runtime_mod.read_result(run.get('log'))
        text = rec.get('result') if isinstance(rec, dict) else ''
        source, tokens = report_mod.footprint_claim(text)
        needs = []
        if tokens:
            needs = widen.outside(widen.resolve(tokens, tracked_fn(product, run.get('branch'))),
                                  task.get('writes'))
        if not needs:
            if tokens:  # a claim read and found inside the footprint: not read again
                pool_mod.update_session(product, job, footprint_read=1)
            continue
        fields, line = lifecycle.footprint_hold(
            run, needs, f'report: {source}',
            f'the session reports it needs {" ".join(needs)} outside writes: ({source})',
            pool_mod.now_iso())
        pool_mod.update_session(product, job, footprint_read=1, **fields)
        ctx.event('held', job=job, item=run.get('item'), text=line)
        out(line)
        held.append(job)
    return held


def protected_paths(product, item_id, paths):
    """``{path: (class, level)}`` for the paths under an approvals-protected glob whose hold on
    ``item_id`` has not been granted."""
    out = {}
    for p in paths:
        hit = approvals.path_class(product, p)
        if hit and not approvals.is_granted(product, f'{item_id}/{hit[0]}'):
            out[p] = hit
    return out


def running(product, items):
    """``[(task_id, writes)]`` of the Tasks in play — a live session, or a pushed branch waiting
    for harvest (:func:`asf.feeder.rows.running_footprints`)."""
    path = pool_mod.sessions_path(product)
    busy = feeder_rows.inflight_ids(lifecycle.inflight(path)) | lifecycle.awaiting_harvest(path)
    return feeder_rows.running_footprints(items or {}, busy)


def correction_text(writes, paths, fact, tests, before):
    """The correction a widened run is relaunched with: the new ``writes:``, what was added and
    why, the failing tests, then the failure it was held with."""
    lines = [f'footprint widened: +{" ".join(paths)} ({fact}).',
             f'writes: is now {" ".join(writes)} — change those files as the failure asks.']
    if tests:
        lines.append(f'failing tests: {" ".join(tests)}')
    if before:
        lines.append(before)
    return '\n'.join(lines)


def _card(root, item_id):
    from asf.record.core import canonicalize, load_items
    by_id, _errors = load_items(root)
    return canonicalize(by_id)[0].get(item_id)


def write_card(root, item_id, updates, stamp, note):
    """Write ``updates`` on ``item_id``'s card through the parser, append the History line
    ``- <stamp> <note>``, and commit that card alone as ``<item>: <note>`` (in a git checkout).
    None, or why the card is unchanged."""
    history = f'- {stamp} {note}'
    from asf.record import frontmatter
    from asf.record.ingest import append_history_lines
    from asf.record.setfield import set_typed
    rec = _card(root, item_id)
    if rec is None:
        return f'no card for {item_id} in the record'
    err = set_typed(rec, updates)
    if err:
        return err
    with open(rec['path'], encoding='utf-8') as f:
        meta, body = frontmatter.parse(f.read(), path=rec['relpath'])
    with open(rec['path'], 'w', encoding='utf-8') as f:
        f.write(frontmatter.render(meta, append_history_lines(body, [history])))
    if _git(root, ['rev-parse', '--git-dir']) is not None:
        _git(root, ['add', '--', rec['relpath']])
        _git(root, ['commit', '-q', '-s', '-m', f'{item_id}: {note}', '--only',
                    '--', rec['relpath']])
    return None


def _reindex(root):
    from asf.record.index import do_index
    try:
        do_index(root)
    except Exception as e:  # the next record step re-derives it; a widening is never undone
        return str(e)
    return None


def apply(ctx, items, out=print):
    """The rule's verdict on every pending ``footprint`` correction; ``{job: verdict}``."""
    product = ctx.product
    path = pool_mod.sessions_path(product)
    stamp = pool_mod.now_iso()[:16].replace('T', ' ')
    done, reindex = {}, False
    in_play = None
    for job, run in sorted(lifecycle.latest(path).items()):
        corr = lifecycle.pending_correction(run, path)
        if not corr or corr.get('kind') != lifecycle.FOOTPRINT or corr.get('verdict') in FINAL:
            continue
        item_id = run.get('item')
        task = _task(items, item_id)
        if task is None:
            continue
        writes = widen.norm_writes(task.get('writes'))
        needs = widen.outside(corr.get('needs'), writes)
        fact = corr.get('fact') or 'report'
        if not needs:  # the record already carries every path: a plain correction on the wider footprint
            text = correction_text(writes, corr.get('needs') or (), fact, corr.get('tests') or (),
                                   corr.get('text'))
            pool_mod.update_session(product, job, correction=dict(corr, verdict=widen.WIDEN,
                                                                 text=text))
            done[job] = widen.WIDEN
            continue
        if in_play is None:
            in_play = running(product, items)
        v = widen.decide(item_id, needs, widen.max_files(product),
                         protected_paths(product, item_id, needs), in_play,
                         lifecycle.widenings(path, item_id))
        if v.kind == widen.WIDEN:
            wider = writes + list(v.paths)
            note = widen.HISTORY.format(paths=' '.join(v.paths), fact=fact)
            err = write_card(ctx.record_root(), item_id, {'writes': wider}, stamp, note)
            if err:
                out(f'widen {job}: {item_id} unchanged — {err}')
                continue
            reindex = True
            text = correction_text(wider, v.paths, fact, corr.get('tests') or (), corr.get('text'))
            pool_mod.update_session(product, job, widened=list(v.paths),
                                    correction=dict(corr, verdict=v.kind, text=text))
            ctx.event('widened', job=job, item=item_id, text=' '.join(v.paths))
            out(f'widened {item_id}: writes: +{" ".join(v.paths)} ({fact}) — {job} back as a correction')
        elif v.kind == widen.RESHAPE:
            note = f'reshape → {v.detail} ({fact})'
            err = write_card(ctx.record_root(), item_id, {'reshape': v.detail}, stamp, note)
            if err:
                out(f'widen {job}: {item_id} unchanged — {err}')
                continue
            reindex = True
            pool_mod.update_session(product, job, correction=dict(corr, verdict=v.kind,
                                                                 detail=v.detail))
            ctx.event('reshape', job=job, item=item_id, text=v.detail)
            out(f'reshape {item_id}: {v.detail}')
        elif v.kind == widen.APPROVAL:
            hold = f'{item_id}/{v.detail}'
            if not any(h['item'] == item_id and h['class'] == v.detail
                       for h in approvals.open_holds(product)):
                approvals.refuse(product, item_id, v.detail, v.level, job, 'widen',
                                 f'widen writes: +{" ".join(v.paths)} ({fact})')
            pool_mod.update_session(product, job, correction=dict(corr, verdict=v.kind,
                                                                 detail=v.detail))
            out(f'held {item_id}: widening needs {v.detail} ({v.level}) — asf approvals resolve '
                f'{hold} granted')
        else:
            if corr.get('verdict') != v.kind or corr.get('detail') != v.detail:
                pool_mod.update_session(product, job, correction=dict(corr, verdict=v.kind,
                                                                     detail=v.detail))
            out(f'waits    {item_id}: widening +{" ".join(v.paths)} overlaps {v.detail}')
        done[job] = v.kind
    if reindex:
        err = _reindex(ctx.record_root())
        if err:
            out(f'widen: index not re-derived — {err}')
    return done


def run(ctx, out=print, items=None):
    """Both halves, in order: the REPORT facts, then the rule."""
    held = report_facts(ctx, items, out=out)
    verdicts = apply(ctx, items, out=out)
    return held, verdicts
