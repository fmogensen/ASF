"""asf.kernel.settings — the product file's ``kernel:`` block: every knob of the kernel's
operation, with its documented default (ASF 0.2).

What used to live in a hand-written script, two hand-written plists and a note is a key here::

    kernel:
      tick:       {interval_s: 120}           # the tick job's StartInterval
      launch:     {max_sessions: 6,           # live sessions the tick launches up to
                   rank: inherit}             # inherit: a Task takes its nearest ancestor's rank;
                                              # own: only an item's own rank orders it
      landing:    {update_parallel: 2}        # the merge train: Landing PRs updated at once
      watch:      {interval_s: 600,           # the keep-alive job's StartInterval
                   stale_after_s: 900}        # a plan older than this, and no tick running: kick
      idle_alarm: {enabled: true,             # Plan.idle when seats are free and nothing launches
                   min_free_seats: 1}
      gate:       {window_h: 24, first_push_green_min: 0.7, landed_min: 5,
                   silent_stuck_max: 0, since: 2026-10-09T14:00:00Z}   # asf kernel gate

:func:`problems` validates the block for :func:`asf.env.product_problems` (a value of the wrong
type refuses the load; an unknown key is a warning); :func:`read` returns the block with every
default filled in. Pure stdlib: :mod:`asf.env` imports it.
"""
import copy
import datetime

#: every key, its default and its kind: ``int``/``float`` (non-negative), ``bool``, a tuple of
#: allowed words, or ``'time'`` (an ISO-8601 time, or unset)
SPEC = {
    'tick': {'interval_s': (120, int)},
    'launch': {'max_sessions': (6, int), 'rank': ('inherit', ('inherit', 'own'))},
    'landing': {'update_parallel': (2, int)},
    'watch': {'interval_s': (600, int), 'stale_after_s': (900, int)},
    'idle_alarm': {'enabled': (True, bool), 'min_free_seats': (1, int)},
    'gate': {'window_h': (24, float), 'first_push_green_min': (0.7, float), 'landed_min': (5, int),
             'silent_stuck_max': (0, int), 'since': (None, 'time')},
}

#: the intervals launchd is handed must be at least this many seconds
MIN_INTERVAL_S = 10

UNKNOWN = 'is not a kernel key'


def parse_time(value):
    """An ISO-8601 time (``2026-10-09T14:00:00Z``, ``2026-10-09 14:00``, a date) as an aware UTC
    datetime; a ``datetime`` passes through; None for anything unreadable."""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day, tzinfo=datetime.timezone.utc)
    text = str(value or '').strip()
    if not text:
        return None
    try:
        t = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def _bad(value, kind):
    """Why ``value`` is not of ``kind``, or '' when it is."""
    if kind is bool:
        return '' if isinstance(value, bool) else 'must be true or false'
    if kind in (int, float):
        ok = isinstance(value, (int, float) if kind is float else int) and not isinstance(value, bool)
        return '' if ok and value >= 0 else 'must be a non-negative %s' % (
            'number' if kind is float else 'whole number')
    if kind == 'time':
        return '' if parse_time(value) else 'must be an ISO-8601 time'
    return '' if str(value) in kind else 'must be one of %s' % ' | '.join(kind)


def problems(block):
    """``(errors, warnings)``: each a list of ``(dotted key, problem)`` for the ``kernel:`` block
    (None or absent is fine: every default applies)."""
    if block is None:
        return [], []
    if not isinstance(block, dict):
        return [('kernel', 'must be a map, not %r' % (block,))], []
    errors, warnings = [], []
    for section, value in block.items():
        if section not in SPEC:
            warnings.append(('kernel.%s' % section, UNKNOWN))
            continue
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(('kernel.%s' % section, 'must be a map, not %r' % (value,)))
            continue
        for key, v in value.items():
            dotted = 'kernel.%s.%s' % (section, key)
            if key not in SPEC[section]:
                warnings.append((dotted, UNKNOWN))
            elif v is not None and _bad(v, SPEC[section][key][1]):
                errors.append((dotted, '%s, not %r' % (_bad(v, SPEC[section][key][1]), v)))
    for section in ('tick', 'watch'):
        v = (block.get(section) or {}).get('interval_s') if isinstance(block.get(section), dict) \
            else None
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < MIN_INTERVAL_S:
            errors.append(('kernel.%s.interval_s' % section, 'must be at least %d' % MIN_INTERVAL_S))
    v = (block.get('landing') or {}).get('update_parallel') \
        if isinstance(block.get('landing'), dict) else None
    if isinstance(v, int) and not isinstance(v, bool) and v < 1:
        errors.append(('kernel.landing.update_parallel', 'must be at least 1'))
    return errors, warnings


def read(block):
    """The ``kernel:`` block (a mapping, or None) with every default filled in — a key whose value
    is malformed reads as its default (:func:`problems` already refused the load)."""
    out = {s: {k: copy.copy(d) for k, (d, _kind) in keys.items()} for s, keys in SPEC.items()}
    for section, keys in SPEC.items():
        given = (block or {}).get(section) if isinstance(block, dict) else None
        if not isinstance(given, dict):
            continue
        for key, (_d, kind) in keys.items():
            v = given.get(key)
            if v is not None and not _bad(v, kind):
                out[section][key] = parse_time(v) if kind == 'time' else v
    return out


def for_product(product):
    """:func:`read` of ``product``'s ``kernel:`` block."""
    return read(product._get('kernel'))
