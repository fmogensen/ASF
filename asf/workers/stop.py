"""asf.workers.stop — ``asf stop <job|item>``: end a running session on purpose (F-0275).

Until this command the only way to stop a session was a ``kill`` by hand, and the factory read
that as a death: ``dead pid``, a cold correction (:func:`asf.workers.stall.correct_once`), a hold
counted on the item's rounds, a launch counted by the relaunch cap. A stop is the operator's
decision, not a fault of the work, so it is recorded as what it is:

* the session's whole process group gets SIGTERM, then SIGKILL, and is verified quiet
  (:func:`asf.workers.lifecycle.stop`); a cloud run's workflow run is cancelled or its
  routine disabled (:func:`asf.workers.cloud.stop`);
* the run's ledger line ends ``stopped`` (:data:`asf.workers.lifecycle.STOPPED`) with
  ``stopped_by: operator`` — the seat is released by that line (a run with ``ended`` holds none),
  health sends nothing back to a session for it (no correction, no round), and the relaunch cap
  does not count it (:func:`asf.workers.relaunch.counted`).

``<job|item>``: a job name stops that job's live run; an item id stops every live run of it.
"""
import re

from asf import env
from asf.workers import cloudpid
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

#: The ``stopped_by`` an operator stop writes.
OPERATOR = 'operator'
_ITEM_RE = re.compile(r'[A-Za-z]+-\d{4,}')


def targets(product, what):
    """The live runs ``what`` names: the job of that name, else every live run of the item."""
    live = [r for r in pool_mod.load_sessions(product).values() if lifecycle.is_live(r)]
    by_job = [r for r in live if r.get('job') == what]
    if by_job or not _ITEM_RE.fullmatch(what or ''):
        return by_job
    return [r for r in live if str(r.get('item') or '').upper() == what.upper()]


def stop_run(product, run, stop_fn=None):
    """Stop one live run — :func:`asf.workers.lifecycle.stop`: its whole process group TERM then
    KILL and verified quiet, or the cloud lane's cancel — which writes its ``ended: stopped``
    line; then ``stopped_by: operator`` on it. ``(ok, detail)``; a stop that could not be
    verified ends nothing."""
    ok, detail = (stop_fn or lifecycle.stop)(pool_mod.sessions_path(product), run)
    if ok or cloudpid.is_token(run.get('pid')):  # a cloud run's line is written either way
        pool_mod.update_session(product, run['job'], stopped_by=OPERATOR)
    return ok, detail


def cmd_stop(args, stop_fn=None, out=print):
    product = env.load_product(getattr(args, 'product', None))
    what = (args.target or '').strip()
    runs = targets(product, what)
    if not runs:
        out(f'asf stop: no running session for {what}')
        return 1
    rc = 0
    for run in runs:
        ok, detail = stop_run(product, run, stop_fn=stop_fn)
        if not ok and not cloudpid.is_token(run.get('pid')):
            out(f'asf stop: {run["job"]} not stopped — {detail}')
            rc = 1
            continue
        out(f'stopped {run["job"]} ({detail}): ended {lifecycle.STOPPED}, its seat released — '
            f'no correction, no round, not counted by the relaunch cap')
    return rc


def register(sub):
    """``asf stop <job|item> [--product P]``."""
    p = sub.add_parser('stop', help='stop a running session (a job, or every live run of an '
                                    'item): SIGTERM then SIGKILL, ended "stopped", no correction')
    p.add_argument('target', help='a job name, or an item id')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_stop)
