"""asf.run_cancel — the one way the factory cancels a CI run, and reads that it took.

``gh run cancel`` (``POST …/actions/runs/{id}/cancel``) on a run that is still *queued* is
accepted and does nothing: the run stays queued, holds its place in the runner queue, and later
starts as if never cancelled (2026-10-05: relief and merge-queue drops "cancelled" queued runs
that went on to run for an hour). A queued run is ended with ``POST …/force-cancel``; a run in
progress gets the plain cancel (its jobs' ``always()`` steps still run). Either way the run is
read back afterwards: it must read ``completed``. A plain cancel the run still reads ``queued``
after is followed by one force-cancel. A run that will not read completed is reported, never
claimed done.

Every caller hands in its own ``gh`` as ``call(args) -> (ok, stdout)`` so the dry-run guard
(:mod:`asf.mutation_guard`, which refuses the ``api -X POST`` too) and each caller's test fakes
stay where they are. Never raises — but a rate limit
(:class:`asf.gh_limit.RateLimited`) passes through: the caller's pass stops there.
"""
import time

from asf import gh_limit

#: the statuses a run waits in before a runner takes it — a plain cancel there is a no-op
QUEUED = frozenset({'queued', 'waiting', 'pending', 'requested'})
#: every status the host reports for a run
STATUSES = QUEUED | {'in_progress', 'completed'}
#: reads of the run after a force-cancel before it is reported as not confirmed
CONFIRM_READS = 3
CONFIRM_GAP_S = 2.0


class Cancelled:
    """What one cancel did: ``ok`` (the host accepted a cancel), ``forced`` (force-cancel was
    used), ``status`` (the run's status read back, None when unreadable) and ``detail`` (one
    line)."""

    def __init__(self, ok, forced, status, detail):
        self.ok, self.forced, self.status, self.detail = ok, forced, status, detail

    @property
    def confirmed(self):
        """True when the run reads completed — or, after a plain cancel, in progress (winding
        down: the host completes it once its jobs stop)."""
        return self.status == 'completed' or (self.ok and not self.forced
                                               and self.status == 'in_progress')

    def __bool__(self):
        return bool(self.ok)


def status_of(call, slug, run_id):
    """The run's status as the host reads it now, or None when unreadable."""
    gh_limit.forget()                   # never a memoised read from before the cancel
    try:
        ok, out = call(['api', f'repos/{slug}/actions/runs/{run_id}', '--jq', '.status'])
    except gh_limit.RateLimited:
        raise                           # the pass stops at a rate limit, never swallowed
    except Exception:  # noqa: BLE001 — unreadable
        return None
    s = str(out or '').strip().strip('"')
    return s if ok and s in STATUSES else None


def _post(call, slug, run_id, verb):
    try:
        ok, _out = call(['api', '-X', 'POST', f'repos/{slug}/actions/runs/{run_id}/{verb}'])
    except gh_limit.RateLimited:
        raise
    except Exception:  # noqa: BLE001 — refused
        return False
    return bool(ok)


def _plain(call, slug, run_id):
    try:
        ok, _out = call(['run', 'cancel', str(run_id), '-R', slug])
    except gh_limit.RateLimited:
        raise
    except Exception:  # noqa: BLE001 — refused
        return False
    return bool(ok)


def _settle(call, slug, run_id, sleep, reads, gap):
    """Read the run until it is completed, at most ``reads`` times; stop at an unreadable read
    (no use waiting on what cannot be read)."""
    s = None
    for i in range(max(1, reads)):
        if i:
            sleep(gap)
        s = status_of(call, slug, run_id)
        if s is None or s == 'completed':
            return s
    return s


def cancel(call, slug, run_id, status=None, sleep=time.sleep, reads=CONFIRM_READS,
           gap=CONFIRM_GAP_S):
    """Cancel run ``run_id`` of ``slug`` and read it back. ``status``: the run's status as the
    caller last listed it (read here when not given). A queued run: force-cancel. Else the plain
    cancel; read back still queued (the listing was stale), force-cancel once. A
    :class:`Cancelled` — truthy when the host accepted a cancel."""
    if status is None:
        status = status_of(call, slug, run_id)
    if status == 'completed':
        return Cancelled(True, False, 'completed', f'run {run_id} already completed')
    forced = status in QUEUED
    ok = _post(call, slug, run_id, 'force-cancel') if forced else _plain(call, slug, run_id)
    if not ok:
        return Cancelled(False, forced, status,
                         f"run {run_id}: {'force-cancel' if forced else 'cancel'} refused")
    after = _settle(call, slug, run_id, sleep, reads if forced else 1, gap)
    if not forced and after in QUEUED:
        forced = True
        if not _post(call, slug, run_id, 'force-cancel'):
            return Cancelled(True, True, after, f'run {run_id} still {after} after its cancel, '
                                                'and the force-cancel was refused')
        after = _settle(call, slug, run_id, sleep, reads, gap)
    c = Cancelled(True, forced, after, '')
    how = 'force-cancelled' if forced else 'cancelled'
    if c.confirmed:
        c.detail = f'run {run_id} {how}, reads {after}'
    elif after is None:
        c.detail = f'run {run_id} {how}, not confirmed (its status did not read)'
    else:
        c.detail = f'run {run_id} {how} but still reads {after} — not confirmed'
    return c


def unconfirmed(c):
    """The line to log for an accepted cancel the run did not confirm, or '' — a run that will
    not read completed is said, never taken as done."""
    return c.detail if c.ok and c.status is not None and not c.confirmed else ''
