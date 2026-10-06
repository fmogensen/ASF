"""asf.metrics.log — the factory's one event vocabulary and its one append path.

Every ``metrics/events`` line has a head (``kind``, ``key``, ``product``, ``tick``, ``item``,
``job``, ``account``, ``branch``, ``text``) plus a ``fields`` bag for whatever the kind carries
beyond that — kind-specific, never rendered, never counted. A line's identity within its day file
is ``(kind, key)`` (:func:`asf.metrics.metrics.natural_key`); a second line with the same pair is
a no-op. Two kinds re-report an open condition every tick (``stall``, ``refusal``) and are keyed
with the tick prefixed (:func:`keyed`), so a retried tick stays idempotent without undercounting.

``emit`` is the one writer: it validates the line against the ``events`` schema and appends it.
A line that fails validation is printed and dropped — the step that raised it is never aborted by
a bad event.
"""
import re
import sys

from asf.metrics import metrics

#: every `kind` an events line may carry — anything else is refused at `emit`
KINDS = ('launch', 'merge', 'relaunch', 'stall', 'refusal', 'needs-operator', 'grant',
         'held', 'hold-resolved', 'ruling', 'triage', 'widened', 'reshape', 'reverted',
         'capacity', 'host_pressure', 'feature-on-prod', 'groom_answer', 'intake')
#: the two kinds that re-report an open condition every tick (PD8): their `key` carries the tick
TICK_KEYED = ('stall', 'refusal')


def keyed(kind, tick, key):
    """`key`, prefixed with `tick` for the two kinds a retried tick would otherwise double-count."""
    return f"{tick}:{key}" if kind in TICK_KEYED else key


def _clip(text):
    return re.sub(r'\s+', ' ', text.strip())[:200]


def emit(root, product, kind, key, *, tick=None, item=None, job=None, account=None,
         branch=None, text=None, items=None, **fields):
    """Validate and append one `events` line. Returns the stored event, or None — printed, not
    raised — when the line fails validation."""
    ev = {
        'kind': kind,
        'key': key,
        'tick': tick,
        'item': item,
        'job': job,
        'account': account,
        'branch': branch,
        'text': _clip(text) if text is not None else None,
        'fields': fields,
    }
    try:
        ev = metrics.validate('events', ev, items or {}, product=product)
        metrics.append_event(root, 'events', ev)
    except metrics.SchemaError as e:
        print(f"error: {e}", file=sys.stderr)
        return None
    return ev
