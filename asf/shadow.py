"""asf.shadow — the one ledger every code decider writes while it runs in shadow.

A decider that replaces a model session runs first beside it: ``conventions.flags.<flag>:
shadow`` computes the code's answer, the session still decides, and one record per decision
goes to ``state/<product>/<decider>-disagree.jsonl`` (:func:`log`)::

    {"ts": "…Z", "decider": "mechanical", "key": "worker/T-0001", "code": {…},
     "actual": {…}, "verdict": "same" | "diff" | "pending"}

``pending`` is a decision whose other half is not known yet (the session has not ended); a
later :func:`log` with the same ``key`` and a ``same``/``diff`` verdict settles it. The cutover
reads :func:`tally`: ``N`` settled decisions with ``0`` diffs over the last ``N`` is the
plan's bar. Writing never raises into a decider — a log that cannot be written is one stderr
line. :func:`mode` reads the flag: ``off`` (default), ``shadow`` or ``on``.
"""
import datetime
import sys

from asf.state import store

#: The file name of a decider's ledger.
SUFFIX = '-disagree.jsonl'
OFF, SHADOW, ON = 'off', 'shadow', 'on'
ON_WORDS = ('on', 'true', 'yes', '1')
SAME, DIFF, PENDING = 'same', 'diff', 'pending'


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def name(decider):
    return f'{decider}{SUFFIX}'


def _flag_reader(product):
    if product is None:
        return None
    flag = getattr(product, 'flag', None)
    if flag is None:
        flag = getattr(getattr(product, 'conventions', None), 'flag', None)
    return flag


def mode(product, flag, default=OFF):
    """``conventions.flags.<flag>`` as one of ``off`` / ``shadow`` / ``on`` — an "on" word
    (``on``/``true``/``yes``/``1``, or True) is ``on``, ``shadow`` is ``shadow``, anything else
    (unset, ``off``, a typo) is ``default``."""
    read = _flag_reader(product)
    value = read(flag, None) if read is not None else None
    if value is True:
        return ON
    text = str(value if value is not None else '').strip().lower()
    if text in ON_WORDS:
        return ON
    if text == SHADOW:
        return SHADOW
    if text == OFF or value is False:
        return OFF
    return default


def log(product, decider, key, *, code=None, actual=None, verdict=PENDING, **extra):
    """Append one shadow decision of ``decider`` on ``key``. Never raises."""
    rec = {'ts': _now(), 'decider': decider, 'key': str(key), 'code': code,
           'actual': actual, 'verdict': verdict}
    rec.update(extra)
    pname = getattr(product, 'name', product)
    try:
        store.append(pname, name(decider), rec)
    except Exception as e:  # noqa: BLE001 — the shadow's log never takes a decider down
        print(f'SHADOW: {decider} not logged ({type(e).__name__}: {e})', file=sys.stderr,
              flush=True)
    return rec


def records(product, decider, since=None):
    """Every record of ``decider`` (oldest first); at or after ``since`` (ISO ``…Z``) when
    given. An unreadable ledger reads as none."""
    pname = getattr(product, 'name', product)
    try:
        got = store.read(pname, name(decider), default=[]).data or []
    except Exception:  # noqa: BLE001 — a reader of the log never fails its caller
        return []
    out = [r for r in got if isinstance(r, dict)]
    return [r for r in out if str(r.get('ts') or '') >= since] if since else out


def settled(product, decider, since=None):
    """``{key: record}``: the newest ``same``/``diff`` record per key."""
    out = {}
    for r in records(product, decider, since):
        if r.get('verdict') in (SAME, DIFF):
            out[r.get('key')] = r
    return out


def pending(product, decider):
    """``{key: record}``: the newest record per key that no later ``same``/``diff`` settled."""
    out = {}
    for r in records(product, decider):
        k = r.get('key')
        if r.get('verdict') == PENDING:
            out[k] = r
        elif r.get('verdict') in (SAME, DIFF):
            out.pop(k, None)
    return out


def tally(product, decider, last=None):
    """``{'n': settled decisions, 'diff': how many of them disagreed}`` over the newest
    ``last`` settled ones (all when None) — the cutover's bar is ``n >= N`` and ``diff == 0``."""
    rows = sorted(settled(product, decider).values(), key=lambda r: str(r.get('ts') or ''))
    if last:
        rows = rows[-last:]
    return {'n': len(rows), 'diff': sum(1 for r in rows if r.get('verdict') == DIFF)}


def ready(product, decider, n):
    """True when the newest ``n`` settled decisions of ``decider`` show no diff."""
    t = tally(product, decider, last=n)
    return t['n'] >= n and t['diff'] == 0
