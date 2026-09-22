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


def handle_dead(ctx, session, runtime_fn=_runtime, out=print):
    """``corrected`` | ``operator`` | ``flagged`` (already raised) for one dead session."""
    product = ctx.product
    job = session['job']
    if session.get('operator_flagged'):
        return 'flagged'
    if not session.get('corrected'):
        try:
            ok = stall_mod.correct_once(product, session, error_text(session), runtime_fn())
        except (OSError, KeyError) as e:
            out(f"DEAD  {job:<24} correction could not start: {e}")
            ok = False
        if ok:
            out(f"DEAD  {job:<24} corrected — relaunched cold as {job}-correction")
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


def run(ctx, out=print, runtime_fn=_runtime):
    product = ctx.product
    found = health_mod.health(product, fix=True, out=out)
    stalled = stall_mod.stall(product, out=out)
    ctx.counts['stalls'] += len(stalled)
    sessions = pool_mod.load_sessions(product)
    for job in dead_jobs(found, stalled):
        session = sessions.get(job)
        if session is not None:
            handle_dead(ctx, dict(session, job=job), runtime_fn=runtime_fn, out=out)
    return 0
