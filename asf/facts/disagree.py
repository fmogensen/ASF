"""asf.facts.disagree — the shadow's log: where the old decider and the fact disagreed.

One jsonl record per disagreement in ``state/<product>/facts-disagree.jsonl``, appended through
the store (:func:`asf.state.store.append`: ``O_APPEND`` under the file's lock)::

    {"ts": "…Z", "fact": "landed", "decider": "trunkclose", "key": "T-0001",
     "old": true, "new": {"fact": "NotLanded", "why": "…", "hint_sha": "", "as_of": {…}}}

A ``new`` of ``"error:<Type>"`` is a fact that raised. The log is the cutover's evidence:
:func:`count` per ``(fact, decider)``, the scorecard's ``facts_disagree`` and the status row.
Writing it never raises into a decider — a log that cannot be written is one stderr line.
"""
import dataclasses
import datetime
import sys

from asf.facts.types import AsOf
from asf.state import store

NAME = 'facts-disagree.jsonl'
#: The status row's window.
STATUS_HOURS = 24


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def render(value):
    """``value`` as json: a fact dataclass becomes ``{"fact": <type>, <fields>…}``."""
    if isinstance(value, AsOf):
        return {'head': value.head, 'at': value.at}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {'fact': type(value).__name__}
        for f in dataclasses.fields(value):
            out[f.name] = render(getattr(value, f.name))
        return out
    if isinstance(value, (list, tuple)):
        return [render(v) for v in value]
    if isinstance(value, dict):
        return {str(k): render(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def log(product, fact, key, old, new, *, decider=''):
    """Append one disagreement. Never raises: a failed write is one ``FACTS:`` stderr line."""
    rec = {'ts': _now(), 'fact': fact, 'decider': decider or '', 'key': str(key),
           'old': render(old), 'new': render(new)}
    try:
        store.append(product, NAME, rec)
    except Exception as e:  # noqa: BLE001 — the shadow's log never takes a decider down
        print(f'FACTS: disagreement not logged ({type(e).__name__}: {e}): {rec}',
              file=sys.stderr, flush=True)
    return rec


def records(product, since=None):
    """Every logged disagreement (oldest first), those at or after ``since`` (an ISO ``…Z``)
    when given. An unreadable log reads as none."""
    try:
        got = store.read(product, NAME, default=[]).data or []
    except Exception:  # noqa: BLE001 — a reader of the log never fails its caller
        return []
    out = [r for r in got if isinstance(r, dict)]
    return [r for r in out if str(r.get('ts') or '') >= since] if since else out


def count(product, since=None):
    """``{(fact, decider): n}`` over :func:`records`."""
    out = {}
    for r in records(product, since):
        k = (r.get('fact') or '', r.get('decider') or '')
        out[k] = out.get(k, 0) + 1
    return out


def status_cell(product, hours=STATUS_HOURS):
    """``N disagreement(s) in 24h: landed/trunkclose 3, …`` — None (no row) when there is none."""
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
             ).strftime('%Y-%m-%dT%H:%M:%SZ')
    got = count(product, since)
    if not got:
        return None
    n = sum(got.values())
    parts = ', '.join(f"{f}/{d or '-'} {k}" for (f, d), k in
                      sorted(got.items(), key=lambda kv: (-kv[1], kv[0])))
    return f"{n} disagreement{'s' if n != 1 else ''} in {hours}h (facts shadow): {parts}"
