"""asf.tick.step_health — the tick's ``health`` step: reconcile the sessions, then look for stalls.

``workers.health(product, fix=True)`` ends the sessions whose log carries a result or whose pid is
gone and reaps the worktrees that pass the reap rule; ``workers.stall(product)`` then lists the
live sessions that went silent (``STALL``) or lost their pid (``DEAD``). Both print their own
lines (``ended`` / ``orphan`` / ``reaped`` / ``keep`` …, ``STALL`` / ``DEAD``).

A dead session — ``DEAD`` from the stall check, or ended ``dead pid`` by health this tick — gets
one cold retry first (``correct_once``): its own job, its own ledger line, so the dead run's
``dead pid`` record stands as what actually happened and the retry's outcome is never folded
onto it (D-0048, part b). Only a session whose correction already ran (or just ran and failed
again) becomes a ``needs-operator`` event, once: the session is marked ``operator_flagged`` so
the next tick does not raise it again.
"""
import os

from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stall as stall_mod

LOG_TAIL_LINES = 5


def _runtime():
    return runtime_mod.from_config(spawn_mod.load_cfg())


def log_tail(path, n=LOG_TAIL_LINES):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = [ln.rstrip('\n') for ln in f if ln.strip()]
    except (OSError, TypeError):
        return ''
    return '\n'.join(lines[-n:])


def error_text(session):
    tail = log_tail(session.get('log'))
    return (f"the session's process (pid {session.get('pid')}) died before it wrote a result"
            + (f"; the last lines of its log:\n{tail}" if tail else '.'))


def died_text(session):
    """The correction a twice-dead session hands its next run (B-0062)."""
    tail = error_text(session).strip().splitlines()
    return 'died twice: the session and its cold retry both ended without a result — ' + \
        (tail[-1].strip() if tail else 'no log lines')


def operator_line(session):
    job = session.get('job')
    return (f"NEEDS OPERATOR: session {job} ({session.get('item') or '?'}) died twice — "
            f"read its log {session.get('log') or '?'}, then relaunch it or close its item")


def dead_jobs(found, stalled):
    """Job names that died, in first-seen order: health's ``ended dead pid``, stall's ``DEAD``."""
    jobs = [job for job, what, detail in found if what == 'ended' and detail == 'dead pid']
    jobs += [job for job, state, _quiet in stalled if state == 'DEAD']
    return list(dict.fromkeys(jobs))


def handle_dead(ctx, session, runtime_fn=_runtime, out=print, items=None):
    """``corrected`` | ``operator`` | ``flagged`` (already raised) | ``closed`` for one dead
    session. ``items`` is the record's index: a dead run of a removed or done item is only ended
    (health did that) — no cold retry, no hold, nothing sent back to a session."""
    product = ctx.product
    job = session['job']
    if session.get('operator_flagged'):
        return 'flagged'
    if lifecycle.closed_state(items, session.get('item')):
        return 'closed'
    # a correction is never corrected again (B-0085): its own failure is what holds the item, and
    # the round is counted there. Without this the tick would correct the correction for ever.
    is_correction = str(job).endswith('-correction')
    if not session.get('corrected') and not is_correction:
        try:
            ok = stall_mod.correct_once(product, session, error_text(session), runtime_fn())
        except (OSError, KeyError) as e:
            out(f"DEAD  {job:<24} correction could not start: {e}")
            ok = False
        if ok:
            # launched, not finished (B-0085): the correction is running with its own registry
            # line, and the next tick judges it. Nothing waits for it here.
            out(f"DEAD  {job:<24} correction launched as {job}-correction")
            ctx.counts['relaunches'] += 1
            return 'corrected'
    # B-0062: a session that died twice is held like a red gate — a correction on the run, a
    # round on the item, the ADJUDICATE row at the cap — never a question to the operator
    # (D-0049: the factory decides, the operator is informed). A hold already pending stands.
    if lifecycle.pending_correction(session, pool_mod.sessions_path(product)):
        return 'held'
    fields, line = lifecycle.hold(pool_mod.sessions_path(product), session, 'died',
                                  died_text(session), pool_mod.now_iso())
    ctx.event('held', job=job, item=session.get('item'), text=line)
    pool_mod.update_session(product, job, **fields)
    out(line)
    return 'held'


def file_rulings(ctx, out=print):
    """B-0064: an adjudicate session's ruling is the ``ruling:`` line of its REPORT; the factory
    files it on the item's card in the record clone as a ``## History`` line and marks the run
    ``adjudicated`` — the session never commits a ruling or mints an id. A run whose report
    carries no ruling is marked too (``adjudicated: none``) and named once. Returns the jobs
    filed."""
    from asf.record.core import TYPES
    from asf.record import frontmatter
    from asf.record.ingest import append_history_lines
    from asf.workers import report as report_mod
    product = ctx.product
    done = []
    for job, run in pool_mod.load_sessions(product).items():
        if run.get('kind') != 'adjudicate' or not run.get('ended') or run.get('adjudicated'):
            continue
        rec = runtime_mod.read_result(run.get('log'))
        if rec is None:
            continue
        text = report_mod.ruling(rec.get('result') if isinstance(rec, dict) else '')
        item = run.get('item') or ''
        folder = next((f for t, (f, p) in TYPES.items() if item.startswith(p + '-')), None)
        path = os.path.join(ctx.record_root(), folder, f'{item}.md') if folder else ''
        stamp = pool_mod.now_iso()[:16].replace('T', ' ')
        if text and path and os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                meta, body = frontmatter.parse(f.read(), path=path)
            line = f'- {stamp} adjudicate ({job}): {" ".join(text.split())}'
            new_body = append_history_lines(body, [line])
            with open(path, 'w', encoding='utf-8') as f:
                f.write(frontmatter.render(meta, new_body))
            pool_mod.update_session(product, job, adjudicated=stamp)
            ctx.event('ruling', job=job, item=item, text=text)
            out(f'ruling {job} filed on {item}: {text[:120]}')
        else:
            why = 'no ruling in its report' if not text else f'no card for {item or "?"} in the record'
            pool_mod.update_session(product, job, adjudicated='none')
            out(f'ruling {job}: {why} — marked, not filed')
        done.append(job)
    return done


def widen_footprints(ctx, items, out=print):
    """``widen_footprint`` (:mod:`asf.tick.widen_footprint`): a finished run whose REPORT, or
    whose red gate, names paths outside its Task's ``writes:`` gets the rule's verdict before the
    wave plans. A failure here is one line; the sessions' health stands."""
    from asf.tick import widen_footprint
    try:
        return widen_footprint.run(ctx, out=out, items=items)
    except Exception as e:  # noqa: BLE001 — the rule must never take the health step down
        out(f'widen: skipped — {type(e).__name__}: {e}')
        return None


def run(ctx, out=print, runtime_fn=_runtime):
    product = ctx.product
    items = health_mod.record_items(product)
    found = health_mod.health(product, fix=True, out=out, items=items)
    reap_worktrees(ctx, out=out)
    file_rulings(ctx, out=out)  # B-0064
    widen_footprints(ctx, items, out=out)
    stalled = stall_mod.stall(product, out=out)
    ctx.counts['stalls'] += len(stalled)
    sessions = pool_mod.load_sessions(product)
    for job in dead_jobs(found, stalled):
        session = sessions.get(job)
        if session is not None:
            handle_dead(ctx, dict(session, job=job), runtime_fn=runtime_fn, out=out,
                        items=items)
    hold_failed_corrections(ctx, sessions, out=out, items=items)
    ci_trials(ctx, out=out)
    branch_retention(ctx, items, out=out)
    return 0


def reap_worktrees(ctx, out=print):
    """The worktree reaper (:mod:`asf.workers.worktrees`): ended sessions' worktrees whose work
    is on origin or the trunk are removed, within the cap of live sessions +
    ``worker_pool.worktree_buffer``. One line when it reaps; never raised — the next tick
    reaps again."""
    from asf.workers import worktrees
    try:
        return worktrees.reap(ctx.product, out=out)
    except Exception as e:  # noqa: BLE001 — a reap never stops a tick
        out(f'worktrees: skipped — {type(e).__name__}: {e}')
        return None


def branch_retention(ctx, items, out=print):
    """Origin's expired ``archive/*`` and retired-prefix heads deleted, at most ``per_tick`` a
    tick, and the unowned heads counted for the doctor (:mod:`asf.workers.retention`). Printed,
    never raised: a sweep never stops a tick."""
    from asf.workers import retention
    try:
        return retention.sweep(ctx.product, fix=True, out=out, items=items)
    except Exception as e:  # noqa: BLE001 — the next tick sweeps again
        out(f'retention: skipped — {type(e).__name__}: {e}')
        return None


def ci_trials(ctx, out=print):
    """A CI runner on trial (``asf ci reconcile --apply`` enabled it for a role): judge its
    first job, keep or roll it back, and file the Bug of a rollback (:func:`asf.ci_pool.tick`).
    Nothing on trial, no CI host call. Printed, never raised: a trial never stops a tick."""
    from asf import ci_pool
    try:
        ci_pool.tick(ctx, out=out)
    except Exception as e:  # noqa: BLE001 — the next tick judges it again
        out(f"ci trial: not judged ({type(e).__name__}: {e})")


def hold_failed_corrections(ctx, sessions, out=print, items=None):
    """A correction that has ended without finishing holds the run it was correcting (B-0085).

    The hold used to happen in the same pass that launched the correction, because that pass
    waited for it. Nothing waits now, so the failure is discovered here, one tick later: the
    correction is ended, it did not finish, and its original run is held exactly as before —
    a correction on the run, a round on the item, the ADJUDICATE row at the cap (B-0062)."""
    product = ctx.product
    path = pool_mod.sessions_path(product)
    for job, run_rec in sorted(sessions.items()):
        if not job.endswith('-correction') or not run_rec.get('ended'):
            continue
        if run_rec.get('end_reason') == lifecycle.FINISHED or run_rec.get('harvested'):
            continue
        if lifecycle.quota_exhausted(run_rec):
            continue  # a spent window, not a failure: it relaunches, and holds nothing
        original = sessions.get(job[:-len('-correction')])
        if original is None or lifecycle.pending_correction(original, path):
            continue
        if lifecycle.closed_state(items, original.get('item')):
            continue  # a removed or done item's run is never held
        original_job = job[:-len('-correction')]
        fields, line = lifecycle.hold(path, dict(original, job=original_job), 'died',
                                      died_text(run_rec), pool_mod.now_iso())
        ctx.event('held', job=original_job, item=original.get('item'), text=line)
        pool_mod.update_session(product, original_job, **fields)
        out(line)
    return 0
