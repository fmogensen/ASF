"""asf.kernel.settings — the product file's ``kernel:`` block: every knob of the kernel's
operation, with its documented default (ASF 0.2).

What used to live in a hand-written script, two hand-written plists and a note is a key here::

    kernel:
      tick:       {interval_s: 120}           # the tick job's StartInterval
      launch:     {max_sessions: 6,           # live sessions the tick launches up to
                   local_max: 6,              # local seats (unset: max_sessions)
                   cloud_max: 0,              # cloud-lane seats (the product's ``cloud:`` lane);
                                              # the tick launches up to local_max + cloud_max
                   cloud_kinds: [coder, fix-bug, spec, plan, review, light-review],
                                              # the brief kinds that may go to cloud; every other
                                              # (correct, rebases) takes a local seat. A cloud
                                              # review reports on asf-reviews/<job>, never
                                              # the PR branch, so it restarts no CI
                   rank: inherit}             # inherit: a Task takes its nearest ancestor's rank;
                                              # own: only an item's own rank orders it
      github:     {retry_delays_s: [2, 5, 10],   # a transient GitHub read (5xx, timeout,
                                              # secondary rate limit) is retried after each
                                              # delay; still failing, the tick is blind: no PR
                                              # action, crashed sessions still ended, exit 0
                   slow_below: 0.15}          # core calls left under this share of the hourly
                                              # limit (read once per tick off a call's headers):
                                              # the tick reads no new change or failed log
      models:     {coder: claude-sonnet-5, fix-bug: claude-sonnet-5, correct: claude-sonnet-5,
                   review: claude-sonnet-5, light-review: claude-sonnet-5,
                   spec: claude-opus-5, plan: claude-opus-5}   # per brief kind: a model id, or
                                              # a worker_pool.models label (heavy, light, cheap)
      review:     {light_paths: ['docs/**', '*.md']}   # a PR changing only these (or the
                                              # product's document dirs) gets a light review
      briefs:     {max_appended_chars: 4000}  # the kernel's findings + answers in a brief,
                                              # newest kept (0: no cap)
      landing:    {update_parallel: 2,        # the merge train: Landing PRs updated at once,
                                              # most items unblocked (after:) first, then rank
                   max_wait_h: 2}             # an auto-merge PR waiting this long since
                                              # auto-merge was enabled is first in line
      watch:      {interval_s: 600,           # the keep-alive job's StartInterval
                   stale_after_s: 900}        # a plan older than this, and no tick running: kick
      idle_alarm: {enabled: true,             # Plan.idle when seats are free and nothing launches
                   min_free_seats: 1}
      stuck:      {escalate_after_h: 0,       # a Stuck owned by a session or CI is resolved this
                                              # old: a red off the PR one more rerun then a fix
                                              # round; a stopped session one relaunch with its
                                              # report; the fix-round cap one extra round on
                                              # strong_model (0: on the tick it appears)
                   rebuild_after_h: 0,        # a conflict the rebase could not resolve (or the cap
                                              # after the strong round) this old: archive the
                                              # branch, close the PR, rebuild fresh — once per item
                   strong_model: claude-opus-5,   # the extra round's model; operator questions
                                              # are never resolved: status shows them on top —
                   id_claim_answer: true,     # except one that only asks whether an id claim
                                              # covers ids it cites: the kernel reads the claim
                                              # refs and answers (keep, or re-mint)
                   id_claim_prefixes: [S, T]} # the id prefixes such a question is checked for
      gate:       {window_h: 24, first_push_green_min: 0.7, landed_min: 5,
                   silent_stuck_max: 0, since: 2026-10-09T14:00:00Z}   # asf kernel gate
      waits:      {targets: {seat: 10m, ci: 10m, review: 30m, train: 30m, merge: 10m,
                             conflict: 0m, stuck: 0m}}   # per wait class (asf.kernel.waits):
                                              # a wait older than its class's target is ⚠ in
                                              # ``asf kernel waits`` and counted on the tick line;
                                              # after and parked have none unless set. A duration
                                              # is 30s, 10m, 2h, 1d or whole seconds
      install:    {shadow: true,              # asf kernel install dry-runs the new venv's tick on
                                              # live facts first: a crash keeps the old plists
                   max_state_changes: 25}     # more items changing state than this (or Launch
                                              # dropping to 0 with Ready work) needs --accept-diff

:func:`problems` validates the block for :func:`asf.env.product_problems` (a value of the wrong
type refuses the load; an unknown key is a warning); :func:`read` returns the block with every
default filled in. Pure stdlib: :mod:`asf.env` imports it.
"""
import copy
import datetime

#: the default model of each brief kind a kernel launch writes (:func:`asf.kernel.briefs.brief_kind`;
#: ``light-review`` is a docs-only PR's review): judgement over a change runs on the faster model,
#: a spec and a plan on the deeper one
LIGHT_MODEL, HEAVY_MODEL = 'claude-sonnet-5', 'claude-opus-5'
MODEL_KINDS = {'coder': LIGHT_MODEL, 'fix-bug': LIGHT_MODEL, 'correct': LIGHT_MODEL,
               'review': LIGHT_MODEL, 'light-review': LIGHT_MODEL,
               'spec': HEAVY_MODEL, 'plan': HEAVY_MODEL}

#: every key, its default and its kind: ``int``/``float`` (non-negative), ``bool``, a tuple of
#: allowed words, ``'time'`` (an ISO-8601 time, or unset), ``'text'`` (a non-empty string) or
#: ``'globs'`` (a list of path globs), ``'words'`` (a list of names) or ``'seconds'`` (a list of
#: non-negative numbers). A default of None is "unset" (``launch.local_max``:
#: ``max_sessions``).
#: the default target of each wait class (:mod:`asf.kernel.waits`), as a duration
WAIT_TARGETS = {'seat': '10m', 'ci': '10m', 'review': '30m', 'train': '30m', 'merge': '10m',
                'conflict': '0m', 'stuck': '0m'}

#: the wait classes a target may name (``stuck`` covers every ``stuck:<owner>``)
WAIT_CLASSES = ('seat', 'ci', 'review', 'train', 'merge', 'conflict', 'stuck', 'after', 'parked')

_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}


def parse_duration(value):
    """Seconds of ``value`` (``30s``, ``10m``, ``2h``, ``1d``, or whole seconds), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if value >= 0 else None
    text = str(value or '').strip().lower()
    if text[-1:] in _UNITS:
        num, unit = text[:-1].strip(), _UNITS[text[-1]]
    else:
        num, unit = text, 1
    try:
        n = float(num)
    except ValueError:
        return None
    return n * unit if n >= 0 else None


SPEC = {
    'tick': {'interval_s': (120, int)},
    'launch': {'max_sessions': (6, int), 'rank': ('inherit', ('inherit', 'own')),
               'local_max': (None, int), 'cloud_max': (0, int),
               'cloud_kinds': (('coder', 'fix-bug', 'spec', 'plan', 'review', 'light-review'),
                               'words')},
    'github': {'retry_delays_s': ((2, 5, 10), 'seconds'), 'slow_below': (0.15, float)},
    'models': {k: (v, 'text') for k, v in MODEL_KINDS.items()},
    'review': {'light_paths': (('docs/**', '*.md'), 'globs')},
    'briefs': {'max_appended_chars': (4000, int)},
    'landing': {'update_parallel': (2, int), 'max_wait_h': (2.0, float)},
    'watch': {'interval_s': (600, int), 'stale_after_s': (900, int)},
    'idle_alarm': {'enabled': (True, bool), 'min_free_seats': (1, int)},
    'stuck': {'escalate_after_h': (0, float), 'rebuild_after_h': (0, float),
              'strong_model': (HEAVY_MODEL, 'text'), 'id_claim_answer': (True, bool),
              'id_claim_prefixes': (('S', 'T'), 'words')},
    'gate': {'window_h': (24, float), 'first_push_green_min': (0.7, float), 'landed_min': (5, int),
             'silent_stuck_max': (0, int), 'since': (None, 'time')},
    'waits': {'targets': (WAIT_TARGETS, 'targets')},
    'install': {'shadow': (True, bool), 'max_state_changes': (25, int)},
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
    if kind == 'text':
        return '' if isinstance(value, str) and value.strip() else 'must be a non-empty string'
    if kind == 'words':
        ok = isinstance(value, (list, tuple)) and all(isinstance(g, str) and g.strip()
                                                      for g in value)
        return '' if ok else 'must be a list of brief kinds (coder, fix-bug, spec, plan, ...)'
    if kind == 'targets':
        ok = isinstance(value, dict) and all(
            k in WAIT_CLASSES and (v is None or parse_duration(v) is not None)
            for k, v in value.items())
        return '' if ok else ('must map wait classes (%s) to durations like 10m'
                              % ', '.join(WAIT_CLASSES))
    if kind == 'seconds':
        ok = isinstance(value, (list, tuple)) and all(
            isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 for n in value)
        return '' if ok else 'must be a list of non-negative seconds, like [2, 5, 10]'
    if kind == 'globs':
        ok = isinstance(value, (list, tuple)) and all(isinstance(g, str) and g.strip()
                                                      for g in value)
        return '' if ok else 'must be a list of path globs'
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
        for key, (default, kind) in keys.items():
            v = given.get(key)
            if v is not None and not _bad(v, kind):
                out[section][key] = (dict(default, **v) if kind == 'targets' else
                                     parse_time(v) if kind == 'time' else
                                     [str(g).strip() for g in v] if kind in ('globs', 'words') else
                                     list(v) if kind == 'seconds' else
                                     v.strip() if kind == 'text' else v)
    out['waits']['targets'] = {k: parse_duration(v) for k, v in out['waits']['targets'].items()
                               if v is not None}
    out['review']['light_paths'] = list(out['review']['light_paths'])
    out['github']['retry_delays_s'] = list(out['github']['retry_delays_s'])
    out['launch']['cloud_kinds'] = list(out['launch']['cloud_kinds'])
    out['stuck']['id_claim_prefixes'] = list(out['stuck']['id_claim_prefixes'])
    if out['launch']['local_max'] is None:
        out['launch']['local_max'] = out['launch']['max_sessions']
    return out


def seats(block):
    """``(local_max, cloud_max)`` of a :func:`read` block."""
    launch = block['launch']
    local = launch['local_max']
    return (launch['max_sessions'] if local is None else local), launch['cloud_max']


def for_product(product):
    """:func:`read` of ``product``'s ``kernel:`` block."""
    return read(product._get('kernel'))
