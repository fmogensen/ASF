"""asf.workers.cloudpid — what stands in for a pid when a session runs in the cloud lane.

A local session is a process on this host, and every reader of the ledger asks "is it alive?" of
its pid. A cloud session (:mod:`asf.workers.cloud`) is no process here, so its run records a
*token*: ``pid: actions:<workflow run id>`` (``actions:<ASF session>`` until the run id is
known), or ``pid: remote:<routine trigger id>`` for the ``claude-remote`` runtime
(:mod:`asf.workers.remote`). Runs of the refused ``claude-cloud`` runtime recorded
``cloud:<ASF session>``; those still read as tokens. Every liveness check that meets a token answers from the cloud status file
``<ASF_HOME>/state/cloud-sessions.json``, which :func:`asf.workers.cloud.sync` writes each health
pass: ``{token: {status, why, updated, …}}`` with ``status`` one of ``working``, ``finished``,
``dead``. A token the file does not know yet is a launch the sync has not seen: working. This
module imports nothing of the workers, so the ledger's lowest layer (:mod:`asf.workers.lifecycle`)
can ask it without a cycle.
"""
import json
import os
import tempfile
import threading

from asf import env

PREFIX = 'actions:'
#: a ``claude-remote`` run's token: ``remote:<trigger id>``
REMOTE_PREFIX = 'remote:'
#: every token prefix a ledger may hold: the live ones, and the refused runtime's
PREFIXES = (PREFIX, REMOTE_PREFIX, 'cloud:')
WORKING = 'working'
FINISHED = 'finished'
DEAD = 'dead'
STATES = (WORKING, FINISHED, DEAD)


def token(ref):
    """The pid-shaped token of the cloud run ``ref`` (a workflow run id, or the ASF session)."""
    return f'{PREFIX}{ref}'


def is_token(pid):
    return isinstance(pid, str) and pid.startswith(PREFIXES)


def cache_path():
    return os.path.join(env.ASF_HOME, 'state', 'cloud-sessions.json')


def load():
    """``{token: record}``; ``{}`` when the file is missing or unreadable."""
    try:
        with open(cache_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(tok, status, why='', **fields):
    """Write ``tok``'s status (atomically: a reader never sees half a file)."""
    with _LOCK:  # a wave's cloud launches record from threads: none loses another's entry
        return _record(tok, status, why, **fields)


_LOCK = threading.Lock()


def _record(tok, status, why='', **fields):
    data = load()
    rec = dict(data.get(tok) or {})
    rec.update({k: v for k, v in fields.items() if v is not None})
    rec.update(status=status, why=why)
    data[tok] = rec
    path = cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.cloud-', dir=os.path.dirname(path))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, indent=1)
    os.replace(tmp, path)
    return rec


def status(tok):
    """``tok``'s last recorded status: :data:`WORKING` when none is recorded yet."""
    return (load().get(tok) or {}).get('status') or WORKING


def alive(tok):
    """A cloud run holds its seat while it is working."""
    return status(tok) == WORKING
