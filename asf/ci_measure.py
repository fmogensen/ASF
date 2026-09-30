"""asf.ci_measure — per runner per job kind, a median, a ratio, a green rate, a baseline and a
regression.

The source is the metrics ``ci`` stream (:mod:`asf.metrics.metrics`). Given the window's events,
:func:`readings` groups every job by ``(runner, job kind)``, :func:`scores` turns that into one
:class:`Score` per runner — a median duration per kind, a ratio against the fleet's best over the
kinds two or more boxes share, and a green rate — and :func:`baselines` / :func:`regressions`
publish a timing baseline per runner per job kind and flag a green job that has quietly grown past
its own.

This module knows nothing of a tier, a census or a product's CI host: it is a pure reader of
hand-built event dicts, plus one small published state file (:data:`BASELINES_FILE`) and the
``asf ci baseline`` command that reads it back.
"""
import collections
import dataclasses
import datetime
import json
import math
import os
import re

from asf import env

#: only a green job says how fast a box is
MEASURED = frozenset({'success'})
#: and only these two say whether it is flaky (D14)
RATED = frozenset({'success', 'failure'})
#: readings older than this say nothing about the box as it is now
WINDOW_DAYS = 14
#: fewest green readings before a runner is scored for a job kind (D7, D12)
MIN_READINGS = 5
#: below this green rate a runner is flaky, whatever its speed (D15)
MIN_GREEN_RATE = 0.85
#: a green reading this much past its own baseline is a row (D19)
REGRESSION_FACTOR = 1.5
_MATRIX = re.compile(r'\s*\([^()]*\)\s*$')


def job_kind(name):
    """``test (3.12)`` → ``test`` (D11); a name with no trailing parenthesis is itself."""
    return _MATRIX.sub('', name or '')


# ------------------------------------------------------------------ time --

def _now(now):
    return now or datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(stamp):
    """A stream ``ts`` as a UTC-aware time, ``Z`` or an offset; a stamp with no zone is UTC."""
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    s = stamp.strip()
    if s[-1:] in ('Z', 'z'):
        s = s[:-1] + '+00:00'
    try:
        t = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc)


def _iso(d):
    return d.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _p50(values):
    """The median of ``values``, nearest rank: a value some run actually reached, never an
    interpolation — the same convention :func:`asf.ci_queue._percentile` uses. ``None`` when
    ``values`` is empty."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.5 * len(ordered)) - 1)]


# ------------------------------------------------------------------ readings --

@dataclasses.dataclass(frozen=True)
class Reading:
    seconds: float
    conclusion: str
    queued_s: object   # float or None


class Series(list):
    """One ``(runner, kind)``'s readings, oldest first — so a series's last :data:`MEASURED`
    entry is the latest one :func:`regressions` compares against the baseline."""


def readings(events, now=None, window_days=WINDOW_DAYS):
    """``{(runner, kind): Series}`` from ``ci`` events in the window: every job with a ``runner``,
    its ``seconds`` (``minutes * 60`` when absent — D5), its conclusion, its ``queued_s``."""
    now = _now(now)
    start = now - datetime.timedelta(days=window_days)
    dated = []
    for ev in events or ():
        ts = _parse_ts(ev.get('ts'))
        if ts is None or ts < start:
            continue
        dated.append((ts, ev))
    dated.sort(key=lambda pair: pair[0])
    out = {}
    for _, ev in dated:
        for j in ev.get('jobs') or ():
            runner = j.get('runner')
            if not runner:
                continue
            kind = job_kind(j.get('name') or '')
            secs = j.get('seconds')
            if not isinstance(secs, (int, float)) or isinstance(secs, bool):
                secs = (j.get('minutes') or 0) * 60
            reading = Reading(seconds=float(secs), conclusion=j.get('conclusion'),
                               queued_s=j.get('queued_s'))
            out.setdefault((runner, kind), Series()).append(reading)
    return out


# ------------------------------------------------------------------ scores --

@dataclasses.dataclass(frozen=True)
class Score:
    runner: str
    medians: dict     # {kind: median green seconds}
    ratios: dict      # {kind: this runner's median / the fleet's best for that kind}, D12
    ratio: float      # the median of ``ratios`` (D13); None with no comparable kind
    n: int            # green readings in the window
    green_rate: float # greens / (greens + reds) over RATED (D14)
    flaky: bool       # green_rate < MIN_GREEN_RATE with n >= MIN_READINGS (D15)


def scores(readings):
    """``{runner: Score}``. A kind fewer than two runners have :data:`MIN_READINGS` of is left out
    of every ratio (D12); a runner left with no ratio has ``ratio=None``."""
    runner_all = collections.defaultdict(list)
    green_by = {}   # (kind, runner) -> [seconds]
    for (runner, kind), series in readings.items():
        runner_all[runner].extend(series)
        greens = [r.seconds for r in series if r.conclusion in MEASURED]
        if greens:
            green_by[(kind, runner)] = greens

    medians = {key: _p50(vals) for key, vals in green_by.items()}
    kinds_of = collections.defaultdict(set)
    for kind, runner in green_by:
        kinds_of[runner].add(kind)

    eligible = collections.defaultdict(list)
    for (kind, runner), vals in green_by.items():
        if len(vals) >= MIN_READINGS:
            eligible[kind].append(runner)
    fleet_best = {kind: min(medians[(kind, r)] for r in rs)
                  for kind, rs in eligible.items() if len(rs) >= 2}

    out = {}
    for runner in sorted(runner_all):
        m = {kind: medians[(kind, runner)] for kind in kinds_of[runner]}
        ratios = {kind: medians[(kind, runner)] / fleet_best[kind]
                  for kind in kinds_of[runner]
                  if kind in fleet_best and runner in eligible[kind]}
        ratio = _p50(list(ratios.values()))
        green = [r for r in runner_all[runner] if r.conclusion in MEASURED]
        rated = [r for r in runner_all[runner] if r.conclusion in RATED]
        n = len(green)
        green_rate = round(len(green) / len(rated), 3) if rated else 0.0
        flaky = green_rate < MIN_GREEN_RATE and n >= MIN_READINGS
        out[runner] = Score(runner=runner, medians=m, ratios=ratios, ratio=ratio,
                             n=n, green_rate=green_rate, flaky=flaky)
    return out


def baselines(readings, tiers):
    """``{runner: {kind: (p50_seconds, n)}}`` plus a ``{tier: {kind: (p50_seconds, n)}}`` fallback
    from every runner in the tier (D18)."""
    per = {}
    tier_secs = collections.defaultdict(lambda: collections.defaultdict(list))
    for (runner, kind), series in readings.items():
        greens = [r.seconds for r in series if r.conclusion in MEASURED]
        if not greens:
            continue
        if len(greens) >= MIN_READINGS:
            per.setdefault(runner, {})[kind] = (_p50(greens), len(greens))
        t = tiers.get(runner)
        if t:
            tier_secs[t][kind].extend(greens)
    tier = {t: {k: (_p50(v), len(v)) for k, v in kinds.items()} for t, kinds in tier_secs.items()}
    return per, tier


def regressions(readings, base):
    """``[(runner, kind, latest_s, baseline_s)]`` where the latest green reading is at least
    :data:`REGRESSION_FACTOR` times the baseline (D19)."""
    out = []
    for (runner, kind), series in readings.items():
        baseline = (base.get(runner) or {}).get(kind)
        if not baseline:
            continue
        base_s = baseline[0]
        latest = None
        for reading in series:
            if reading.conclusion in MEASURED:
                latest = reading.seconds
        if latest is not None and base_s and latest >= REGRESSION_FACTOR * base_s:
            out.append((runner, kind, latest, base_s))
    return sorted(out)


def run_wall_p50(events, now=None, window_days=WINDOW_DAYS):
    """The p50 of the window's ``wall_minutes`` over runs that have one (D5), or None — what I10
    paces off."""
    now = _now(now)
    start = now - datetime.timedelta(days=window_days)
    vals = []
    for ev in events or ():
        ts = _parse_ts(ev.get('ts'))
        if ts is None or ts < start:
            continue
        wm = ev.get('wall_minutes')
        if isinstance(wm, (int, float)) and not isinstance(wm, bool) and wm > 0:
            vals.append(wm)
    return _p50(vals)


# ------------------------------------------------------------------ the published baseline --

BASELINES_FILE = 'ci-baselines.json'
BASELINES_VERSION = 1
#: the census's own state file (asf.ci_census, Task 3): read here only as a plain, best-effort
#: JSON peek for a runner's current tier — never imported, so ``asf ci baseline`` needs no other
#: module and works before a census has ever been taken (D2's degradation, applied here too).
_CENSUS_FILE = 'ci-census.json'


def _baselines_path(product):
    return os.path.join(env.state_dir(product), BASELINES_FILE)


def write_baselines(product, readings, tiers, now=None):
    """Write ``<state dir>/ci-baselines.json`` (§2.5's shape) from ``readings`` and the current
    ``{runner: tier}`` map, through a write-then-rename so a half-written file is never read."""
    now = _now(now)
    per, tier = baselines(readings, tiers)
    doc = {
        'v': BASELINES_VERSION,
        'taken': _iso(now),
        'window_days': WINDOW_DAYS,
        'per': {r: {k: {'p50_s': s, 'n': n} for k, (s, n) in kinds.items()}
                for r, kinds in per.items()},
        'tier': {t: {k: {'p50_s': s, 'n': n} for k, (s, n) in kinds.items()}
                 for t, kinds in tier.items()},
    }
    path = _baselines_path(product)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def read_baselines(product):
    """The published baselines, or ``{}`` on a missing, unreadable or wrong-version file — never
    raises: a state file being unreadable must not break a doctor row or a guard."""
    try:
        with open(_baselines_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get('v') != BASELINES_VERSION:
        return {}
    return data


def _runner_tiers(product):
    """Best-effort ``{runner: tier}`` off ``ci-census.json``'s ``runners`` list — ``{}`` on a
    missing, unreadable or malformed file, and never a network call."""
    try:
        with open(os.path.join(env.state_dir(product), _CENSUS_FILE), encoding='utf-8') as f:
            data = json.load(f)
        return {r['runner']: r['tier'] for r in data.get('runners') or []
                if isinstance(r, dict) and r.get('runner') and r.get('tier')}
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return {}


def baseline_for(product, kind, runner, tiers=None):
    """``(seconds, n, 'runner'|'tier')``: the runner's own published p50 for ``kind``, else its
    tier's, else ``None`` — a guard that cannot get a baseline should skip its check, not invent
    one. ``tiers`` is the caller's ``{runner: tier}``; when not given it is read off the census
    file (best effort, D2's degradation)."""
    data = read_baselines(product)
    own = (data.get('per') or {}).get(runner, {}).get(kind)
    if own:
        return (own['p50_s'], own['n'], 'runner')
    tiers = tiers if tiers is not None else _runner_tiers(product)
    t = tiers.get(runner)
    tier_reading = ((data.get('tier') or {}).get(t) or {}).get(kind) if t else None
    if tier_reading:
        return (tier_reading['p50_s'], tier_reading['n'], 'tier')
    return None


def cmd_baseline(args, out=print):
    """``asf ci baseline``: one number on stdout, or ``--json``'s ``{"seconds", "n", "from"}`` —
    and exit 1 with no output when neither the runner nor its tier has a reading."""
    product = getattr(args, 'product', None) or env.default_product_name()
    got = baseline_for(product, args.job, args.runner)
    if got is None:
        return 1
    seconds, n, source = got
    if getattr(args, 'json', False):
        out(json.dumps({'seconds': seconds, 'n': n, 'from': source}))
    else:
        out(seconds)
    return 0


def register(sub):
    p = sub.add_parser('baseline', help="a runner's (or its tier's) measured p50 for a job kind")
    env.add_product_arg(p)
    p.add_argument('--job', required=True, help='the job kind (see job_kind)')
    p.add_argument('--runner', required=True, help='the runner to look up; falls back to its tier')
    p.add_argument('--json', action='store_true', help='print {"seconds", "n", "from"} instead')
    p.set_defaults(run=cmd_baseline)
    return p
