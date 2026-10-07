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
#: health's findings that change what the wave may launch
FREES = ('ended', 'held', 'parked', 'released', 'landed', 're-judged', 'quota', 'auth')


def _runtime():
    return runtime_mod.from_config(spawn_mod.load_cfg())


def log_tail(path, n=LOG_TAIL_LINES):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = [ln.rstrip('\n') for ln in f if ln.strip()]
    except (OSError, TypeError):
        return ''
    return '\n'.join(lines[-n:])


def wrote_a_result(session):
    """True when the session's log ends its last run in a result record — whatever that record
    says. A run that wrote one did not die: B-0123's two adjudicate logs both ended in a
    ``"type":"result"`` carrying a full ruling while the hold said they "both ended without a
    result" and then quoted one of them (F-0176). This is the presence of the record, never its
    verdict — ``ran_to_its_end`` reads the verdict, and an ``is_error`` result slipped past it."""
    return runtime_mod.read_result(session.get('log')) is not None


def outcome_text(session):
    """What a run that wrote a result actually came to: the ledger's own ``end_reason``, else the
    failure the result declares (:func:`asf.workers.runtime.failure_reason`), else that it
    declared none."""
    reason = (session.get('end_reason') or '').strip()
    if not reason:
        reason = runtime_mod.failure_reason(runtime_mod.read_result(session.get('log'))) or ''
    return reason or 'no failure declared'


def error_text(session):
    tail = log_tail(session.get('log'))
    if wrote_a_result(session):
        head = (f"the session (pid {session.get('pid')}) wrote a result and then failed: "
                f"{outcome_text(session)}")
    else:
        head = f"the session's process (pid {session.get('pid')}) died before it wrote a result"
    return head + (f"; the last lines of its log:\n{tail}" if tail else '.')


def died_text(session):
    """The correction a twice-dead session hands its next run (B-0062) — or, when its log does
    carry a result, what that run actually came to (F-0176). The phrase "without a result" is
    never written about a log that holds one."""
    last = (error_text(session).strip().splitlines() or ['no log lines'])[-1].strip()
    if wrote_a_result(session):
        return f'the retry ended with a result, not a death: {outcome_text(session)} — {last}'
    return 'died twice: the session and its cold retry both ended without a result — ' + last


def operator_line(session):
    job = session.get('job')
    return (f"NEEDS OPERATOR: session {job} ({session.get('item') or '?'}) died twice — "
            f"read its log {session.get('log') or '?'}, then relaunch it or close its item")


def dead_jobs(found, stalled):
    """Job names that died, in first-seen order: health's ``ended dead pid``, stall's ``DEAD``."""
    jobs = [job for job, what, detail in found if what == 'ended' and detail == 'dead pid']
    jobs += [job for job, state, _quiet in stalled if state == 'DEAD']
    return list(dict.fromkeys(jobs))


def died_before(path, job):
    """True when ``job``'s run before its latest one also ended on a dead pid."""
    rs = lifecycle.runs(path).get(job) or []
    return len(rs) >= 2 and rs[-2].get('end_reason') == 'dead pid'


#: a review session's kind: its death re-runs the review, never a correction (F-0275)
REVIEW_KIND = 'review'
#: a review that died this many times in a row is the operator's, not re-run again
REVIEW_DEATHS_MAX = 3


def dead_streak(path, job):
    """How many of ``job``'s latest runs in a row ended on a dead pid."""
    n = 0
    for r in reversed(lifecycle.runs(path).get(job) or []):
        if r.get('end_reason') != 'dead pid':
            break
        n += 1
    return n


def handle_dead(ctx, session, runtime_fn=_runtime, out=print, items=None, reaped_empty=False):
    """``corrected`` | ``operator`` | ``flagged`` (already raised) | ``closed`` | ``released``
    for one dead session. ``items`` is the record's index: a dead run of a removed or done item is
    only ended (health did that) — no cold retry, no hold, nothing sent back to a session.
    ``reaped_empty``: health reaped the run's worktree with nothing in it (no commit, no branch)
    — a first death has nothing to retry or correct, and the item's own feeder row relaunches it
    (spec-f-1129: a hold there queued a correct session on a branch that never existed); a
    second death in a row is held as ever (B-0062)."""
    product = ctx.product
    job = session['job']
    if session.get('operator_flagged'):
        return 'flagged'
    if lifecycle.closed_state(items, session.get('item')):
        return 'closed'
    if reaped_empty and not str(job).endswith('-correction') \
            and not died_before(pool_mod.sessions_path(product), job):
        out(f"DEAD  {job:<24} reaped empty: nothing to retry or correct — "
            f"{session.get('item') or '?'} goes back to its own row")
        return 'released'
    if session.get('kind') == REVIEW_KIND:
        # F-0275: a review that died judged nothing — the branch's head is what it was, so the
        # review re-runs on it (the lane still wants its round, the feeder's review row
        # relaunches it). Never a correction: that would send the coder back for no finding
        if dead_streak(pool_mod.sessions_path(product), job) >= REVIEW_DEATHS_MAX:
            pool_mod.update_session(product, job, operator_flagged=1)
            out(f"DEAD  {job:<24} NEEDS OPERATOR: the review died {REVIEW_DEATHS_MAX} times in "
                f"a row on the same head — not re-run again")
            return 'operator'
        out(f"DEAD  {job:<24} review died — re-runs on the same head, no correction")
        return 'released'
    # a correction is never corrected again (B-0085): its own failure is what holds the item, and
    # the round is counted there. Without this the tick would correct the correction for ever.
    is_correction = str(job).endswith('-correction')
    if not session.get('corrected') and not is_correction and not reaped_empty:  # no worktree
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
                                  died_text(session), pool_mod.now_iso(), main=product.main)
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


def rejudge(ctx, items, out=print):
    """:mod:`asf.tick.rejudge`: a park written for a question the report never asked is
    released, and a reshape's "does not split" answer is taken — before the widening rule, which
    reads what they hand back. A failure here is one line; the sessions' health stands."""
    from asf.tick import rejudge as rejudge_mod
    try:
        return rejudge_mod.run(ctx, items, out=out)
    except Exception as e:  # noqa: BLE001 — never takes the health step down
        out(f'rejudge: skipped — {type(e).__name__}: {e}')
        return None


def close_landed_parks(ctx, out=print):
    """A park whose reason carries verified trunk evidence closes its card on that sha
    (:func:`asf.workers.trunkclose.close_parked`) instead of waiting for a person. A failure
    here is one line; the sessions' health stands."""
    from asf.workers import trunkclose
    try:
        return trunkclose.close_parked(ctx.product, out=out)
    except Exception as e:  # noqa: BLE001 — never takes the health step down
        out(f'trunk: parks not read — {type(e).__name__}: {e}')
        return []


def run(ctx, out=print, runtime_fn=_runtime):
    product = ctx.product
    items = health_mod.record_items(product)
    spare, alive = spare_this_waves_runs(ctx)
    found = health_mod.health(product, fix=True, out=out, items=items, alive=alive, spare=spare)
    reap_worktrees(ctx, out=out)
    file_rulings(ctx, out=out)  # B-0064
    rejudge(ctx, items, out=out)
    widen_footprints(ctx, items, out=out)
    close_landed_parks(ctx, out=out)
    stalled = stall_mod.stall(product, out=out, alive=alive)
    ctx.counts['stalls'] += len(stalled)
    sessions = pool_mod.load_sessions(product)
    empty = {job for job, what, detail in found if what == 'reaped' and detail == 'empty'}
    # a run ended, held, parked, released or re-judged: a seat or a correction row the wave
    # launches on this tick when it ran before health (asf.tick.tick: the second wave)
    ctx.health_freed = (bool(dead_jobs(found, stalled))
                        or any(what in FREES for _j, what, _d in found))
    for job in dead_jobs(found, stalled):
        session = sessions.get(job)
        if session is not None:
            handle_dead(ctx, dict(session, job=job), runtime_fn=runtime_fn, out=out,
                        items=items, reaped_empty=job in empty)
    hold_failed_corrections(ctx, sessions, out=out, items=items)
    ci_trials(ctx, out=out)
    branch_retention(ctx, items, out=out)
    stale_acts(ctx, out=out)
    return 0


def spare_this_waves_runs(ctx):
    """What health and the stall check spare, when this tick's wave ran
    before them (``tick.wave_first``): a run the wave launched seconds ago counts as alive, so it
    is judged by the next tick — as it was when health ran first — never ended (or reaped) in the
    tick that started it. ``(jobs, alive)``: the jobs health leaves alone and the pid reading
    the stall check and the reaper go by; ``((), None)`` (the checks' own) when no wave ran first."""
    before = getattr(ctx, 'runs_before_wave', None)
    if not getattr(ctx, 'wave_started_at', None) or before is None:
        return (), None
    from asf.tick.tick import run_identity
    sessions = pool_mod.load_sessions(ctx.product)
    fresh = {job: s.get('pid') for job, s in sessions.items()
             if not s.get('ended') and before.get(job) != run_identity(s)}
    if not fresh:
        return (), None
    pids = {pid for pid in fresh.values() if pid}
    base = health_mod.alive_for(ctx.product, list(sessions.values()))
    return set(fresh), (lambda pid: pid in pids or base(pid))


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


def stale_acts(ctx, out=print):
    """Stale means act (:mod:`asf.stale_act`): a lane-STALE PR past its deadline archived and
    closed, a stuck Task far behind the trunk re-planned — acted on where
    ``conventions.stale.act`` holds (default false for every product), the ``would …`` lines elsewhere.
    Printed, never raised: the next tick tries again."""
    from asf import stale_act
    try:
        return stale_act.run(ctx.product, ctx.record_root(), out=out)
    except Exception as e:  # noqa: BLE001 — a clean-up never stops a tick
        out(f'stale: skipped — {type(e).__name__}: {e}')
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


def ran_to_its_end(rec):
    """True when a run's result says it finished its turn (``success``, no error) — whatever its
    REPORT then asked for, it did not die (B-0150)."""
    return bool(rec) and not rec.get('is_error') and rec.get('subtype', 'success') == 'success'


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
        # B-0150: a run launched after the correction is not the one it corrected
        if (original.get('started') or '') > (run_rec.get('started') or ''):
            continue
        # B-0150: a correction that wrote its result ran to the end — whatever failed after
        # it (a push, unpushed work) is publish's to route, never "died twice"
        if ran_to_its_end(runtime_mod.read_result(run_rec.get('log'))):
            continue
        if lifecycle.closed_state(items, original.get('item')):
            continue  # a removed or done item's run is never held
        original_job = job[:-len('-correction')]
        fields, line = lifecycle.hold(path, dict(original, job=original_job), 'died',
                                      died_text(run_rec), pool_mod.now_iso(), main=product.main)
        ctx.event('held', job=original_job, item=original.get('item'), text=line)
        pool_mod.update_session(product, original_job, **fields)
        out(line)
    return 0
