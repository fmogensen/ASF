"""asf.tick.widen_footprint — ``widen_footprint`` applied: facts read, the rule run, its answer written.

Run by the ``health`` step, after health has ended the sessions and before the wave plans:

1. :func:`report_facts` — a finished coder or correct run of a Task whose REPORT names paths
   outside the Task's ``writes:`` (``needs writes:``, else ``left out:`` of a partial report,
   :func:`asf.workers.report.footprint_claim`), each resolved to the one tracked repo path it
   names, is held with a ``footprint`` correction (:func:`asf.workers.lifecycle.footprint_hold`)
   — before harvest can land a Task its own session says is not whole. Harvest writes the same
   correction for the gate's fact (:func:`asf.harvest.harvest.widen_candidates`).
   :func:`refusal_facts` does the same for a push the repo's hook refused whose refusal names
   paths outside ``writes:`` (T-0338) — only a refused push, never advisory output of one that
   went through (T-0349).
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
   * ``waits``: a path overlaps an open Task's ``writes:`` (Active in the record, or a run in
     play — ``asf check``'s intersection), or a path a widening earlier in the same pass added —
     re-decided every tick until it is free.
   A path inside the Task's **Feature footprint** (:func:`asf.feeder.widen.delivery_footprint`,
   the union of its sibling Tasks' ``writes:``) is widened whatever the cap or an earlier
   widening say — the plan gave the Feature that path already — and when the run's own branch
   diff already carries every widened path, no correction is spawned: the widening is recorded
   and the run goes on to review as it is.

   :func:`diff_facts` reads the same fact off the diff itself, before any REPORT: a finished run
   whose branch touches paths outside ``writes:`` but all inside the Feature footprint gets them
   added to ``writes:`` (recorded: History line, ledger ``widened``), no hold and no correction.
   A diff reaching outside the Feature footprint keeps today's rules.
3. :func:`revert_overlaps` — a widening already in the record whose paths intersect an open
   Task's ``writes:`` is undone: those paths leave ``writes:``, the Task gets ``after: <owner>``
   and a History line ``footprint widening reverted: overlaps <owner>``, one commit.
"""
import os
import re
import subprocess

from asf import approvals
from asf.feeder import footprint
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


def branch_diff(product, branch):
    """The paths ``origin/<branch>`` changes against its merge base with ``origin/<main>``, or
    None when git cannot say (no repo, no such branch)."""
    repo = getattr(product, 'repo_dir', None)
    if not repo or not branch or not os.path.isdir(repo):
        return None
    listed = _git(repo, ['diff', '--name-only', '--no-renames',
                         f'origin/{product.conventions.main}...origin/{branch}'])
    return None if listed is None else [l for l in listed.splitlines() if l.strip()]


def _task(items, item_id):
    item = (items or {}).get(item_id or '') or {}
    return item if item.get('type') == 'task' and not lifecycle.closed_state(items, item_id) else None


def reach(product, task):
    """What ``task`` may write without a widening: its ``writes:`` and the product's
    ``conventions.shared_writes`` — the append-only files every Task may add to."""
    writes = widen.norm_writes((task or {}).get('writes'))
    return writes + [w for w in footprint.shared_writes(product) if w not in writes]


#: The record stage's I3 refusal, read back: the Active Task a widening would intersect.
I3_RE = re.compile(r"I3: writes: intersects Active task (\S+?)'s writes:")


def refused_by(err):
    """The Task id an I3 refusal (:func:`write_card`'s error) names, or ''."""
    m = I3_RE.search(err or '')
    return m.group(1) if m else ''


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
                                  reach(product, task))
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


def diff_facts(ctx, items, out=print, diff_fn=branch_diff):
    """Widen every finished coder/correct run whose branch diff touches paths outside its Task's
    ``writes:`` that all sit inside the Task's Feature footprint: the card's ``writes:`` grows by
    them (one History line, one commit — :func:`write_card`), the run carries ``widened``, and no
    correction is written — the session's work goes on to review as it is. A diff reaching
    outside the Feature footprint, a path under an ungranted approval, or one an open Task
    writes is left to today's rules. A run is read once (``diff_read``). ``items`` is updated in
    place. Returns ``{job: [paths]}``."""
    product = ctx.product
    path = pool_mod.sessions_path(product)
    stamp = pool_mod.now_iso()[:16].replace('T', ' ')
    done, in_play = {}, None
    for job, run in sorted(lifecycle.latest(path).items()):
        if run.get('kind') not in CLAIM_KINDS or run.get('diff_read') or not run.get('branch'):
            continue
        if run.get('end_reason') != lifecycle.FINISHED or lifecycle.landed(run):
            continue
        if lifecycle.pending_correction(run, path):
            continue
        item_id = run.get('item')
        task = _task(items, item_id)
        if task is None:
            continue
        files = diff_fn(product, run['branch'])
        if files is None:  # a branch gone or unreadable: nothing to widen from, read once
            pool_mod.update_session(product, job, diff_read=1)
            continue
        writes = widen.norm_writes(task.get('writes'))
        extra = widen.outside(files, reach(product, task))
        feature = widen.delivery_footprint(items, item_id)
        if not extra or not widen.inside_feature(extra, feature):
            pool_mod.update_session(product, job, diff_read=1)
            continue
        if in_play is None:
            in_play = open_footprints(product, items)
        v = widen.decide(item_id, extra, widen.max_files(product),
                         protected_paths(product, item_id, extra), in_play, 0,
                         footprint.shared_globs(product), in_feature=True)
        if v.kind != widen.WIDEN:
            pool_mod.update_session(product, job, diff_read=1)
            out(f'footprint {item_id}: the diff reaches {" ".join(extra)} — {v.kind} '
                f'{v.detail}'.rstrip() + ', left to review')
            continue
        wider = writes + list(v.paths)
        fact = 'diff: inside the Feature footprint'
        err = write_card(ctx.record_root(), item_id, {'writes': wider}, stamp,
                         widen.HISTORY.format(paths=' '.join(v.paths), fact=fact),
                         product=product)
        if err:
            pool_mod.update_session(product, job, diff_read=1)
            out(f'widen {job}: {item_id} unchanged — {err}')
            continue
        if items is not None:
            items[item_id] = dict(task, writes=wider)
        in_play.append((item_id, list(v.paths)))
        pool_mod.update_session(product, job, diff_read=1, widened=list(v.paths))
        ctx.event('widened', job=job, item=item_id, text=' '.join(v.paths))
        out(f'widened writes: +{" ".join(v.paths)} — {fact} ({item_id}; no correction, on to '
            f'review)')
        done[job] = list(v.paths)
    if done:
        err = _reindex(ctx.record_root())
        if err:
            out(f'widen: index not re-derived — {err}')
    return done


def diff_carries(product, branch, paths, diff_fn=branch_diff):
    """True when ``origin/<branch>``'s own diff already changes every one of ``paths``."""
    files = diff_fn(product, branch) if branch else None
    return bool(files) and all(p in files for p in paths or ())


#: How much of a refusal the ``footprint widened`` line and History carry as its reason.
REASON_MAX = 160


def refusal_reason(text):
    """The refusal's own words, one line, cut to :data:`REASON_MAX`: what the History and the
    ``widened writes:`` line name as the reason."""
    words = ' '.join(str(text or '').split())
    for lead in ('the push was refused by the repo\'s own hook — ', 'no — ', 'no - '):
        if words.startswith(lead):
            words = words[len(lead):]
    words = words.split(' — fix what it names')[0]
    return words if len(words) <= REASON_MAX else words[:REASON_MAX - 1].rstrip() + '…'


def refusal_facts(ctx, items, out=print, tracked_fn=tracked_paths):
    """Turn every pending ``hook refused`` correction whose refusal names tracked paths outside
    its Task's ``writes:`` into a ``footprint`` correction (T-0338): the push was refused (health
    writes this class only for a run whose publish failed on the repo's hook,
    :func:`asf.workers.health.push_retry`), the session rightly left the file alone, and a round
    spent on it would only be refused again. The paths are parsed off the refusal and the
    session's ``pushed:`` line, each resolved to the one tracked repo path it names
    (:func:`asf.feeder.widen.resolve`) — no model reads it. :func:`apply` then widens, or waits on
    the Task that writes them. Returns the jobs turned."""
    product = ctx.product
    path = pool_mod.sessions_path(product)
    turned = []
    for job, run in sorted(lifecycle.latest(path).items()):
        if run.get('kind') not in CLAIM_KINDS or lifecycle.landed(run):
            continue
        corr = lifecycle.pending_correction(run, path)
        if not corr or corr.get('kind') != lifecycle.HOOK_REFUSED or corr.get('footprint_read'):
            continue
        task = _task(items, run.get('item'))
        if task is None:
            continue
        tokens = widen.path_tokens(corr.get('text'))
        needs = widen.outside(widen.resolve(tokens, tracked_fn(product, run.get('branch'))),
                              reach(product, task)) if tokens else []
        if not needs:  # the hook names the session's own files: the plain correction stands
            pool_mod.update_session(product, job, correction=dict(corr, footprint_read=1))
            continue
        fact = f'hook refused: {refusal_reason(corr.get("text"))}'
        fields, line = lifecycle.footprint_hold(
            run, needs, fact,
            f'{corr.get("text")}\nfootprint: the refusal names {" ".join(needs)} outside writes:',
            corr.get('at') or pool_mod.now_iso())
        pool_mod.update_session(product, job, **fields)
        ctx.event('held', job=job, item=run.get('item'), text=line)
        out(line)
        turned.append(job)
    return turned


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


def open_footprints(product, items):
    """``[(task_id, writes)]`` of every open Task a widening must not overlap: the Tasks in play
    (:func:`running`) first, then every Task ``Active`` in the record or with a correction pending
    — ``asf check`` refuses two Active Tasks whose ``writes:`` intersect, running or not."""
    path = pool_mod.sessions_path(product)
    out = running(product, items)
    seen = {t for t, _w in out}
    pending = set(lifecycle.corrections(path))
    for iid, t in sorted((items or {}).items()):
        if iid in seen or t.get('type') != 'task' or not t.get('writes') \
                or lifecycle.closed_state(items, iid):
            continue
        if t.get('state') == 'Active' or iid in pending:
            out.append((iid, list(t['writes'])))
    return out


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


def write_card(root, item_id, updates, stamp, note, product=None):
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
    # validated before the commit below (I3 through the record stage): a widening that would
    # leave two Active Tasks' writes: intersecting is refused, the card unchanged — unless the
    # intersection is only a path `product`'s conventions.shared_paths declares, which I3 already
    # exempts (P8)
    err = set_typed(rec, updates, writer='widen', product=product)
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


def widenings(ctx, path, item_id):
    """How many widenings of ``item_id`` stand: its widened runs, less the widenings the record
    says were reverted (a reverted widening is re-decided, not counted toward a reshape)."""
    n = lifecycle.widenings(path, item_id)
    if n:
        rec = _card(ctx.record_root(), item_id)
        n -= (rec.get('text') or '').count(widen.REVERTED.split('{')[0]) if rec else 0
    return max(n, 0)


#: ``apply``'s answer for a footprint correction it let go: a stale report claim, or a
#: widening a person dropped — the run goes on as it is.
DROPPED = 'dropped'


def widen_subject(paths):
    """What a widening's approval asks about: its path set, order-free."""
    return 'widen writes: ' + ' '.join(sorted(set(paths)))


def claims(run):
    """Whether ``run``'s REPORT still claims footprint (:func:`asf.workers.report.footprint_claim`)."""
    rec = runtime_mod.read_result(run.get('log'))
    text = rec.get('result') if isinstance(rec, dict) else ''
    return bool(report_mod.footprint_claim(text)[1])


def apply(ctx, items, out=print, diff_fn=branch_diff):
    """The rule's verdict on every pending ``footprint`` correction; ``{job: verdict}``. Paths
    inside the Task's Feature footprint are widened past the cap and a second widening; when
    the run's branch already changes them all, no correction is spawned (:func:`diff_carries`)."""
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
        fact = corr.get('fact') or 'report'
        if fact.startswith('report:') and not claims(run):
            # T-0349: the REPORT no longer claims a path (done and pushed: advisory output is
            # never a widening) — the hold was a misread; it goes on to review
            pool_mod.update_session(product, job, correction=None)
            closed = approvals.withdraw(product, item_id, 'widen', 'widening withdrawn')
            out(f'widen {job}: {item_id} claims no footprint (its session is done and pushed) — '
                f'correction dropped, on to review' + (f'; closed {" ".join(closed)}' if closed
                                                       else ''))
            done[job] = DROPPED
            continue
        writes = widen.norm_writes(task.get('writes'))
        needs = widen.outside(corr.get('needs'), reach(product, task))
        if not needs:  # the record already carries every path (or the product shares it with
            # every Task, conventions.shared_writes): a plain correction on the wider footprint
            text = correction_text(writes, corr.get('needs') or (), fact, corr.get('tests') or (),
                                   corr.get('text'))
            pool_mod.update_session(product, job, correction=dict(corr, verdict=widen.WIDEN,
                                                                 text=text))
            done[job] = widen.WIDEN
            continue
        if in_play is None:
            in_play = open_footprints(product, items)
        own = widen.delivery_footprint(items, item_id)
        feature = widen.inside_feature(needs, own)
        attributed = widen.attributed_paths(
            items, item_id, needs, {t for t, _w in in_play if t != item_id},
            closed=lambda iid: ((items or {}).get(iid) or {}).get('state') in lifecycle.DONE_STATES)
        v = widen.decide(item_id, needs, widen.max_files(product),
                         protected_paths(product, item_id, needs), in_play,
                         widenings(ctx, path, item_id), footprint.shared_globs(product),
                         in_feature=feature, attributed=attributed,
                         whole=bool(corr.get('whole')) or feeder_rows.recut_declined(task))
        if v.kind == widen.WIDEN:
            wider = writes + list(v.paths)
            note = widen.HISTORY.format(paths=' '.join(v.paths), fact=fact)
            err = write_card(ctx.record_root(), item_id, {'writes': wider}, stamp, note,
                             product=product)
            owner = refused_by(err)
            if owner:
                # the record's own guard found a live Task writing the same file: a wait on
                # that Task, re-decided every tick — never a refusal repeated with no owner named
                if corr.get('verdict') != widen.WAITS or corr.get('detail') != owner:
                    pool_mod.update_session(product, job, correction=dict(
                        corr, verdict=widen.WAITS, detail=owner))
                out(f'waits    {item_id}: widening +{" ".join(v.paths)} overlaps {owner} '
                    f'(the record\'s I3)')
                done[job] = widen.WAITS
                continue
            if err:
                out(f'widen {job}: {item_id} unchanged — {err}')
                continue
            reindex = True
            in_play.append((item_id, list(v.paths)))  # a later widening this pass waits on it
            if items is not None and item_id in items:
                items[item_id] = dict(items[item_id], writes=wider)
            if feature and diff_carries(product, run.get('branch'), v.paths, diff_fn):
                # the session already made the change; only the plan's cut was wrong
                pool_mod.update_session(product, job, widened=list(v.paths), correction=None)
                ctx.event('widened', job=job, item=item_id, text=' '.join(v.paths))
                out(f'widened writes: +{" ".join(v.paths)} — {fact}, inside the Feature '
                    f'footprint and already in the diff ({item_id}; no correction, on to review)')
                done[job] = v.kind
                continue
            text = correction_text(wider, v.paths, fact, corr.get('tests') or (), corr.get('text'))
            pool_mod.update_session(product, job, widened=list(v.paths),
                                    correction=dict(corr, verdict=v.kind, text=text))
            ctx.event('widened', job=job, item=item_id, text=' '.join(v.paths))
            out(f'widened writes: +{" ".join(v.paths)} — {fact} ({item_id}; {job} back as a '
                f'correction, no round spent)')
        elif v.kind == widen.RESHAPE:
            note = f'reshape → {v.detail} ({fact})'
            err = write_card(ctx.record_root(), item_id, {'reshape': v.detail}, stamp, note,
                             product=product)
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
            detail = f'widen writes: +{" ".join(v.paths)} ({fact})'
            subject = widen_subject(v.paths)
            if approvals.dropped(product, hold, subject, detail):
                # a person dropped this very widening: never asked again; the Task goes on
                # without it (to review) — a request for other paths is a new question
                pool_mod.update_session(product, job, correction=None)
                out(f'widen {job}: {hold} was dropped for +{" ".join(v.paths)} — not asked '
                    f'again; {item_id} goes on without the widening')
                done[job] = DROPPED
                continue
            if not any(h['item'] == item_id and h['class'] == v.detail
                       for h in approvals.open_holds(product)):
                approvals.ask(product, item_id, v.detail, v.level, job, 'widen', detail,
                              subject=subject)
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


def revert_overlaps(ctx, items, out=print):
    """Undo every widening whose paths intersect another open Task's ``writes:`` (the state
    ``asf check`` refuses): only the paths a ``footprint widened: +…`` History line added are
    removed, never a plan-declared one; the Task gets ``after: <owner>`` and one History line
    ``footprint widening reverted: overlaps <owner>``, committed alone. A pending correction of
    it waits on the owner again, and the reverted widening is not counted (:func:`widenings`), so
    the rule re-decides once the owner is done. ``items`` is updated in place. Returns ``{task: owner}``."""
    product = ctx.product
    root = ctx.record_root()
    path = pool_mod.sessions_path(product)
    stamp = pool_mod.now_iso()[:16].replace('T', ' ')
    done = {}
    in_play = open_footprints(product, items)
    shared = footprint.shared_globs(product)
    for iid, writes in list(in_play):
        rec = _card(root, iid)
        if rec is None:
            continue
        widened = widen.widened_paths(rec.get('text'))
        if not widened:
            continue
        others = [(t, w) for t, w in in_play if t != iid]
        hit = widen.overlapping_widenings(list(rec['meta'].get('writes') or ()), widened, others,
                                          shared)
        if not hit:
            continue
        owner, paths = hit
        kept = [w for w in rec['meta'].get('writes') or () if w not in paths]
        after = list(rec['meta'].get('after') or ())
        owner_after = ((items or {}).get(owner) or {}).get('after') or ()
        if owner not in after and iid not in owner_after:  # never a cycle of after:
            after.append(owner)
        updates = {'writes': kept}
        if after != list(rec['meta'].get('after') or ()):
            updates['after'] = after
        err = write_card(root, iid, updates, stamp, widen.REVERTED.format(owner=owner),
                         product=product)
        if err:
            out(f'widen: {iid} not reverted — {err}')
            continue
        done[iid] = owner
        in_play[:] = [(t, kept if t == iid else w) for t, w in in_play]
        if items is not None and iid in items:
            items[iid] = dict(items[iid], **updates)
        for job, run in sorted(lifecycle.latest(path).items()):
            if run.get('item') != iid:
                continue
            corr = lifecycle.pending_correction(run, path)
            if corr and corr.get('kind') == lifecycle.FOOTPRINT:
                pool_mod.update_session(product, job, correction=dict(
                    corr, verdict=widen.WAITS, detail=owner))
        ctx.event('reverted', item=iid, text=f'-{" ".join(paths)} overlaps {owner}')
        out(f'reverted {iid}: writes: -{" ".join(paths)} overlaps {owner} — after: {owner}')
    if done:
        err = _reindex(root)
        if err:
            out(f'widen: index not re-derived — {err}')
    return done


def serialize_overlaps(ctx, items, out=print):
    """Order every standing overlap the record has not ordered: for each pair of Active Tasks whose
    ``writes:`` intersect with no ``after:`` either way (:func:`asf.invariants.unordered_overlaps`),
    the later-minted card gets ``after: <the earlier>`` and one History line
    ``serialized behind <owner>: writes: overlaps <glob>``, committed alone (:func:`write_card`).

    Never a cycle: an edge is proposed only where neither Task reaches the other, so the edge it
    adds cannot close one. Idempotent: the ordered pair is not a candidate again. A pair whose
    only common glob is a path the product's ``conventions.shared_paths`` covers is never ordered
    here either: harvest serialises it at merge instead (:func:`asf.feeder.footprint.shared_globs`).
    ``items`` is updated in place; the index is re-derived once when anything was written. Returns
    ``{task: owner}``."""
    from asf import invariants
    from asf.record.core import canonicalize, load_items
    product = ctx.product
    root = ctx.record_root()
    stamp = pool_mod.now_iso()[:16].replace('T', ' ')
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    tasks = {iid: rec['meta'] for iid, rec in canonical.items()}
    edges = invariants._after_edges(tasks)
    done = {}
    reindex = False
    for owner, held, _glob_owner, glob in invariants.unordered_overlaps(
            invariants.overlap_tasks(tasks), footprint.shared_globs(product)):
        if invariants.ordered(edges, owner, held):
            continue  # an edge written earlier in this pass has already ordered the pair
        rec = _card(root, held)
        if rec is None:
            continue
        updates = {'after': list(rec['meta'].get('after') or ()) + [owner]}
        note = widen.SERIALIZED.format(owner=owner, glob=glob)
        err = write_card(root, held, updates, stamp, note, product=product)
        if err:
            out(f'widen: {held} not serialized — {err}')
            continue
        edges.setdefault(held, []).append(owner)
        done[held] = owner
        if items is not None and held in items:
            items[held] = dict(items[held], **updates)
        reindex = True
        ctx.event('serialized', item=held, text=f'after: {owner} — writes: overlaps {glob}')
        out(f'serialized {held}: after: {owner} — writes: overlaps {glob}')
    if reindex:
        err = _reindex(root)
        if err:
            out(f'widen: index not re-derived — {err}')
    return done


def run(ctx, out=print, items=None):
    """Both halves, in order: the REPORT facts, then the rule — and last, the repair of any
    widening already in the record that overlaps an open Task, then the order written over what
    is left."""
    diff_facts(ctx, items, out=out)
    held = report_facts(ctx, items, out=out) + refusal_facts(ctx, items, out=out)
    verdicts = apply(ctx, items, out=out)
    revert_overlaps(ctx, items, out=out)
    serialize_overlaps(ctx, items, out=out)
    return held, verdicts
