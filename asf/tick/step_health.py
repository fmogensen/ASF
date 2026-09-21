"""asf.tick.step_health — the tick's ``health`` step: reconcile the sessions, then look for stalls.

``workers.health(product, fix=True)`` ends the sessions whose log carries a result or whose pid is
gone and reaps the worktrees that pass the reap rule; ``workers.stall(product)`` then lists the
live sessions that went silent (``STALL``) or lost their pid (``DEAD``). Both print their own
lines (``ended`` / ``orphan`` / ``reaped`` / ``keep`` …, ``STALL`` / ``DEAD``).

A dead session — ``DEAD`` from the stall check, or ended ``dead pid`` by health this tick — gets
the workers module's same-session correction (``correct_once``) first. Only a session whose
correction already ran (or just ran and failed again) becomes a ``needs-operator`` event, once:
the session is marked ``operator_flagged`` so the next tick does not raise it again.
"""
from asf.workers import health as health_mod
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
            out(f"DEAD  {job:<24} corrected in the same session")
            return 'corrected'
    line = operator_line(session)
    ctx.event('needs-operator', job=job, item=session.get('item'), text=line)
    pool_mod.update_session(product, job, operator_flagged=1)
    out(line)
    return 'operator'


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
