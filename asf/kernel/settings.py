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
                   rank: inherit,             # inherit: a Task takes its nearest ancestor's rank;
                                              # own: only an item's own rank orders it
                   max_open_prs: 30}          # the WIP cap: while items in Review + Landing
                                              # exceed it, no new build, plan or spec launches;
                                              # their seats go to fix rounds and reviews (0: off).
                                              # Seats always fill finish-first: fix rounds and
                                              # rebases, reviews, builds, plans, specs
      github:     {retry_delays_s: [2, 5, 10],   # a transient GitHub read (5xx, timeout,
                                              # secondary rate limit) is retried after each
                                              # delay; still failing, the tick is blind: no PR
                                              # action, crashed sessions still ended, exit 0
                   slow_below: 0.15}          # core calls left under this share of the hourly
                                              # limit (read once per tick off a call's headers):
                                              # the tick reads no new change or failed log
      models:     {coder: claude-sonnet-5, fix-bug: claude-sonnet-5, correct: claude-sonnet-5,
                   review: claude-sonnet-5, light-review: claude-sonnet-5,
                   groom-fill: claude-sonnet-5,
                   spec: claude-opus-5, plan: claude-opus-5}   # per brief kind: a model id, or
                                              # a worker_pool.models label (heavy, light, cheap)
      review:     {light_paths: ['docs/**', '*.md']}   # a PR changing only these (or the
                                              # product's document dirs) gets a light review
      briefs:     {max_appended_chars: 4000}  # the kernel's findings + answers in a brief,
                                              # newest kept (0: no cap)
      landing:    {update_parallel: 2,        # the merge train: Landing PRs updated at once,
                                              # most items unblocked (after:) first, then rank;
                                              # only while the trunk's ruleset is strict
                                              # (require branches up to date), read each tick
                   max_wait_h: 2,             # an auto-merge PR waiting this long since
                                              # auto-merge was enabled is first in line
                   main_red_revert: true}     # main red on a required check, new since its
                                              # last green commit, with exactly one PR merged
                                              # in between: revert it (revert/<item>, auto-merge)
                                              # and send the item back to Ready; false, or
                                              # several candidates: a Bug for a fix session
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
      resolve:    {trunk_tests: true,         # a question whether named tests are red on the
                                              # factory host or only in a cloud sandbox: the
                                              # kernel runs them on a fresh worktree of origin's
                                              # trunk and answers (asf.kernel.resolvers)
                   symbols: true,             # a question whether a symbol it says does not
                                              # exist exists: read at trunk and answered
                   test_timeout_s: 600,       # the test run is killed past this: no answer
                   python: python3,           # the interpreter the named tests run under
                   test_runs_per_tick: 1,     # test/gate runs per tick; the rest wait (cached
                                              # by question, state/<product>/kernel-resolve.json)
                   gates: true,               # a repo gate script the session could not run
                                              # (only a pre_push_check step) is run on its branch
                                              # head; answered with its last line and the sha
                   inbox_bugs: true,          # a Bug card the session could not mint is filed
                                              # through the inbox; answered with its path
                   needs_writes: true}        # a REPORT's `needs writes:` paths: held behind
                                              # an unfinished after: item that writes one, else
                                              # granted (the card's writes: widened)
      gate:       {window_h: 24, first_push_green_min: 0.7, landed_min: 5,
                   silent_stuck_max: 0, since: 2026-10-09T14:00:00Z,   # asf kernel gate
                   satisfied: true}           # before a fresh Task/Bug launches: the tests it
                                              # names, absent when it was planned, run green on
                                              # a fresh worktree of the trunk -> Done ("satisfied
                                              # on main at <sha>"), nothing launched
                                              # (asf.kernel.needed; code, never an LLM)
      waits:      {targets: {seat: 10m, ci: 10m, review: 30m, train: 30m, merge: 10m,
                             conflict: 0m, stuck: 0m}}   # per wait class (asf.kernel.waits):
                                              # a wait older than its class's target is ⚠ in
                                              # ``asf kernel waits`` and counted on the tick line;
                                              # after and parked have none unless set. A duration
                                              # is 30s, 10m, 2h, 1d or whole seconds
                   breach: true,              # a wait over its target takes a breach action
                                              # (launch first, review local first, front of the
                                              # train, the Stuck's escalation); the tick logs
                                              # BREACH <item> <class> <age> -> <action>
                   max_session_age: 3h,       # a live session older than this is stopped and its
                                              # item relaunched (worktree kept)
                   max_review_session_age: 90m,   # the same for a review session (a review
                                              # that runs this long has hung): relaunched on a
                                              # local seat first
                   max_ci_age: 1h,            # a PR's required CI running this long is
                                              # cancelled and rerun. The three ages are only
                                              # fallbacks: with bound_min_samples finished spells
                                              # on the wait ledger the bound is measured — a
                                              # session past its class's p90 with no push (2x
                                              # with one), CI past 2x the ci p90
                   bound_min_samples: 20}
      main_move:  {alarm_minutes: 5,          # a main move costing more than this logs MAIN MOVE
                                              # ALARM (asf.kernel.mainmoves); the cost is
                                              # measured over window_ticks ticks
                   window_ticks: 15}
      dor:        {enabled: false,            # the Definition of Ready: a Task or Bug starts only
                                              # when its card names a test (Acceptance, or its
                                              # Gate block), its writes (on the trunk or in
                                              # creates:), after: ids on the record and not
                                              # parked, and a parent that is not Done; else it
                                              # stays New (dor: <missing>) and a groom-fill
                                              # session (models.groom-fill) fills the card
                   fill_per_tick: 3,          # groom-fills a tick (after fixes, reviews, builds)
                   max_concurrent: 2,         # groom-fill seats held at any time
                   max_fills: 2}              # groom-fill sessions per card, then Stuck
      risk:       {high: [],                  # path globs: an item whose writes (or PR files)
                                              # hit one is high-risk — its review runs on
                                              # stuck.strong_model, no second high PR with
                                              # overlapping writes lands beside it, and after a
                                              # high merge the next waits until the trunk's
                                              # required checks on that merge are green
                   large_lines: 800}          # ... and so is a PR whose diff is over this
      floor:      {close_orphan_prs: true,    # an open PR on a kernel branch whose item is not on
                                              # the record, Done or retired is closed (comment)
                   cancel_stale_ci: true}     # a queued/running CI run whose PRs are all closed,
                                              # or whose head is no longer its PR's head, is
                                              # cancelled (the runs API, under the rate guard)
      install:    {shadow: true,              # asf kernel install dry-runs the new venv's tick on
                                              # live facts first: a crash keeps the old plists
                   max_state_changes: 25,     # more items changing state than this (or Launch
                                              # dropping to 0 with Ready work) needs --accept-diff
                   lock_timeout_s: 600}       # install waits this long for a running tick to
                                              # end (the kernel lock), then holds the lock while
                                              # launchd switches the plists; past it: refused

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
               'review': LIGHT_MODEL, 'light-review': LIGHT_MODEL, 'groom-fill': LIGHT_MODEL,
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
                               'words'),
               'max_open_prs': (30, int)},
    'github': {'retry_delays_s': ((2, 5, 10), 'seconds'), 'slow_below': (0.15, float)},
    'models': {k: (v, 'text') for k, v in MODEL_KINDS.items()},
    'review': {'light_paths': (('docs/**', '*.md'), 'globs')},
    'briefs': {'max_appended_chars': (4000, int)},
    'landing': {'update_parallel': (2, int), 'max_wait_h': (2.0, float),
                'main_red_revert': (True, bool)},
    'watch': {'interval_s': (600, int), 'stale_after_s': (900, int)},
    'idle_alarm': {'enabled': (True, bool), 'min_free_seats': (1, int)},
    'stuck': {'escalate_after_h': (0, float), 'rebuild_after_h': (0, float),
              'strong_model': (HEAVY_MODEL, 'text'), 'id_claim_answer': (True, bool),
              'id_claim_prefixes': (('S', 'T'), 'words')},
    'resolve': {'trunk_tests': (True, bool), 'symbols': (True, bool),
                'test_timeout_s': (600, int), 'python': ('python3', 'text'),
                'test_runs_per_tick': (1, int), 'gates': (True, bool),
                'inbox_bugs': (True, bool), 'needs_writes': (True, bool)},
    'gate': {'window_h': (24, float), 'first_push_green_min': (0.7, float), 'landed_min': (5, int),
             'silent_stuck_max': (0, int), 'since': (None, 'time'), 'satisfied': (True, bool)},
    'waits': {'targets': (WAIT_TARGETS, 'targets'), 'breach': (True, bool),
              'max_session_age': ('3h', 'duration'),
              'max_review_session_age': ('90m', 'duration'),
              'max_ci_age': ('1h', 'duration'), 'bound_min_samples': (20, int)},
    'main_move': {'alarm_minutes': (5, float), 'window_ticks': (15, int)},
    'dor': {'enabled': (False, bool), 'fill_per_tick': (3, int), 'max_fills': (2, int),
            'max_concurrent': (2, int)},
    'risk': {'high': ((), 'globs'), 'large_lines': (800, int)},
    'floor': {'close_orphan_prs': (True, bool), 'cancel_stale_ci': (True, bool)},
    'install': {'shadow': (True, bool), 'max_state_changes': (25, int),
                'lock_timeout_s': (600, int)},
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
    if kind == 'duration':
        return '' if parse_duration(value) is not None else 'must be a duration like 3h or 90m'
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
    for key in ('max_session_age', 'max_review_session_age', 'max_ci_age'):
        out['waits'][key] = parse_duration(out['waits'][key])
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
