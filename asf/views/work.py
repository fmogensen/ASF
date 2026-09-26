"""asf.views.work — what the factory is working on: Bugs and Features, counted in one place.

Two callers read this module and no third computes these numbers itself: the FACTORY STATUS
table's ``Bugs`` and ``Features`` rows (:mod:`asf.views.status`) and the tick's ``WORK since``
block (:mod:`asf.tick.summary`), which prints the same counts as deltas over the tick's window.

Everything comes from two sources and nothing else:

* the record's ``index.json``, through :mod:`asf.views.index_reader` — a Bug's ``state`` and
  ``severity``, a Feature's ``stage`` and ``decided``, and for both the one clock ingest keeps,
  ``stage_since`` (F-0097 P2: rewritten only when the tracked field changed, so for a Bug it is
  the moment of its last state change and for a Feature the moment it entered its stage);
* the session ledger, through :mod:`asf.workers.lifecycle` — what is being fixed right now, when
  a Feature's first session started, and which Features started in a window.

Read-only and stdlib only: no git, no ``gh``, no subprocess, no clock but the ``now`` it is given.
"""
import dataclasses
import datetime as dt
import os

from asf import conventions
from asf.feeder import rows
from asf.views import index_reader as ix
from asf.workers import lifecycle

DONE = rows.DONE_STATES                      # P7: the one spelling
SEVERITIES = ('S1', 'S2', 'S3')
UNTYPED = 'S?'
SPEC_PLAN_STAGES = ('spec-draft', 'spec-review', 'spec-approved',
                    'plan-draft', 'plan-review', 'plan-approved')
LANDED_STAGES = ('landed', 'on-prod')
DEFAULT_LAND_WINDOW_DAYS = conventions.DEFAULT_LAND_WINDOW_DAYS


@dataclasses.dataclass
class Bugs:
    open_by_sev: dict      # {'S1': 1, 'S2': 4, 'S3': 9, 'S?': 0}
    in_fix: list           # bug ids with a live run, sorted
    in_fix_s1: list         # the S1 subset — the ids the row names
    fixed_today: int
    oldest: tuple           # (id, severity, seconds) of the oldest open S1/S2, or ()
    total: int              # every Bug in the record: the row says so when it is 0


@dataclasses.dataclass
class Features:
    landed_today: list     # ids, newest stage_since first
    building: int
    spec_plan: int
    decided_waiting: int
    undecided: int
    median_to_land: object  # seconds, or None
    measured: int           # the sample the median rests on
    window_days: int
    total: int


def is_open(item):
    return item.get('state', 'New') not in DONE


def severity(bug):
    sev = bug.get('severity')
    return sev if sev in SEVERITIES else UNTYPED


def on_day(ts, now):
    """True when ``ts`` falls on ``now``'s LOCAL day. ``now`` is aware; an unparseable or
    absent ``ts`` is False, never today."""
    t = ix.parse_ts(ts)
    return bool(t) and t.astimezone().date() == now.astimezone().date()


def within(ts, since, now):
    """Half-open ``since < ts <= now``, the window ``summary.done_rows`` already uses — both
    boundaries and ``ts`` run through :func:`asf.views.index_reader.parse_ts` (PD7) so a
    ``stage_since`` written with an offset still sorts against the ledger's ``Z`` window."""
    t = ix.parse_ts(ts)
    if t is None:
        return False
    s, n = ix.parse_ts(since), ix.parse_ts(now)
    return s is not None and n is not None and s < t <= n


def _feature_of(item_id, items):
    """The Feature above ``item_id``, or None — the run's own ``feature`` field is tried first;
    this is only the fallback for a run that predates it."""
    item = items.get(item_id)
    seen = set()
    while item and item['id'] not in seen:
        if item['type'] == 'feature':
            return item['id']
        seen.add(item['id'])
        item = items.get(item.get('parent'))
    return None


def first_started(items, runs):
    """``{feature id: earliest started}`` over every run in the ledger, folded up from the run's
    own ``feature`` field and, failing that, by walking ``parent`` through the index — so a
    Task's run counts for its Feature."""
    out = {}
    for run_list in runs.values():
        for run in run_list:
            started = run.get('started')
            if not started:
                continue
            feature_id = run.get('feature') or _feature_of(run.get('item'), items)
            if not feature_id:
                continue
            if feature_id not in out or started < out[feature_id]:
                out[feature_id] = started
    return out


def time_to_land(feature_id, items, first_started_map):
    """``(seconds, landed_at)`` for one Feature, or None when the ledger never saw it.

    From the first session the ledger holds for the Feature or anything beneath it, to the moment
    it entered ``landed``/``on-prod`` (its ``stage_since``, P2). NOT from the card's creation: the
    index carries no creation timestamp (P6), and this is the number the two sources can answer —
    how long the *factory* took, not how long the card sat.
    """
    started = first_started_map.get(feature_id)
    feature = items.get(feature_id)
    if not started or not feature:
        return None
    t_start, landed_at = ix.parse_ts(started), feature.get('stage_since')
    t_land = ix.parse_ts(landed_at)
    if t_start is None or t_land is None:
        return None
    seconds = (t_land - t_start).total_seconds()
    return (seconds, landed_at) if seconds >= 0 else None


def bugs(items, runs, now):
    all_bugs = ix.of_type(items, 'bug')
    by_id = {b['id']: b for b in all_bugs}
    open_by_sev = {s: 0 for s in SEVERITIES + (UNTYPED,)}
    fixed_today = 0
    open_high = []  # (parsed stage_since, id, severity) — open S1/S2 with a readable stage_since
    for b in all_bugs:
        if is_open(b):
            sev = severity(b)
            open_by_sev[sev] += 1
            if sev in ('S1', 'S2'):
                t = ix.parse_ts(b.get('stage_since'))
                if t is not None:
                    open_high.append((t, b['id'], sev))
        elif on_day(b.get('stage_since'), now):
            fixed_today += 1

    oldest = ()
    if open_high:
        t, bug_id, sev = min(open_high, key=lambda x: (x[0], x[1]))
        oldest = (bug_id, sev, (now - t).total_seconds())

    live_ids = {run.get('item') for run_list in runs.values() for run in run_list
                if lifecycle.is_live(run)}
    in_fix = sorted(live_ids & by_id.keys())
    in_fix_s1 = sorted(i for i in in_fix if severity(by_id[i]) == 'S1')

    return Bugs(open_by_sev=open_by_sev, in_fix=in_fix, in_fix_s1=in_fix_s1,
                fixed_today=fixed_today, oldest=oldest, total=len(all_bugs))


def features(items, runs, now, window_days):
    all_features = ix.of_type(items, 'feature')
    building = spec_plan = decided_waiting = undecided = 0
    landed_today = []  # (parsed stage_since, id)
    for f in all_features:
        stage = f.get('stage')
        if stage in LANDED_STAGES and on_day(f.get('stage_since'), now):
            landed_today.append((ix.parse_ts(f.get('stage_since')), f['id']))
        if stage == 'building':
            building += 1
        elif stage in SPEC_PLAN_STAGES:
            spec_plan += 1
        elif stage in (None, 'card'):
            if f.get('decided') is True:
                decided_waiting += 1
            else:
                undecided += 1
    landed_today.sort(key=lambda x: x[0], reverse=True)

    fs_map = first_started(items, runs)
    window_seconds = window_days * 86400
    samples = []
    for f in all_features:
        if f.get('stage') not in LANDED_STAGES:
            continue
        t = ix.parse_ts(f.get('stage_since'))
        if t is None or (now - t).total_seconds() > window_seconds:
            continue
        result = time_to_land(f['id'], items, fs_map)
        if result is not None:
            samples.append(result[0])

    measured = len(samples)
    if measured == 0:
        median_to_land = None
    else:
        samples.sort()
        mid = measured // 2
        median_to_land = (samples[mid] if measured % 2
                           else (samples[mid - 1] + samples[mid]) / 2)

    return Features(landed_today=[fid for _t, fid in landed_today], building=building,
                     spec_plan=spec_plan, decided_waiting=decided_waiting, undecided=undecided,
                     median_to_land=median_to_land, measured=measured, window_days=window_days,
                     total=len(all_features))


def digest(items, runs, since, now):
    """``('bugs      +1 filed, 2 fixed', 'features  1 landed (F-0095), 3 started')``."""
    all_bugs = ix.of_type(items, 'bug')
    filed = sum(1 for b in all_bugs if b.get('state', 'New') == 'New'
                and within(b.get('stage_since'), since, now))
    fixed = sum(1 for b in all_bugs if b.get('state') in DONE
                and within(b.get('stage_since'), since, now))

    all_features = ix.of_type(items, 'feature')
    landed = sorted(
        ((ix.parse_ts(f.get('stage_since')), f['id']) for f in all_features
         if f.get('stage') in LANDED_STAGES and within(f.get('stage_since'), since, now)),
        key=lambda x: x[0], reverse=True)

    started_features = set()
    for run_list in runs.values():
        for run in run_list:
            if not within(run.get('started'), since, now):
                continue
            feature_id = run.get('feature') or _feature_of(run.get('item'), items)
            if feature_id:
                started_features.add(feature_id)

    bugs_line = f"{'bugs':<10}+{filed} filed, {fixed} fixed"
    landed_clause = f"{len(landed)} landed" + (f" ({landed[0][1]})" if landed else "")
    features_line = f"{'features':<10}{landed_clause}, {len(started_features)} started"
    return bugs_line, features_line


def _runs(product):
    from asf.workers import pool as pool_mod
    try:
        return lifecycle.runs(pool_mod.sessions_path(product))
    except OSError:
        return {}


def bugs_cell(root, product, now=None):
    """``open S1 1 · S2 4 · S3 9 · in fix 2 (S1 B-0091) · fixed today 3 · oldest open S1/S2 B-0091 S1 4d``"""
    from asf.views import status
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return status.not_configured('backlog_dir (no index.json)')
    items, _generated = ix.load(root)
    b = bugs(items, _runs(product), now or dt.datetime.now(dt.timezone.utc))
    if b.total == 0:
        return "no Bugs in the record"
    parts = [f"open S1 {b.open_by_sev['S1']} · S2 {b.open_by_sev['S2']} · S3 {b.open_by_sev['S3']}"]
    if b.open_by_sev[UNTYPED]:
        parts[0] += f" · S? {b.open_by_sev[UNTYPED]}"
    in_fix = f"in fix {len(b.in_fix)}"
    if b.in_fix_s1:
        in_fix += f" (S1 {' '.join(b.in_fix_s1)})"
    parts.append(in_fix)
    parts.append(f"fixed today {b.fixed_today}")
    if b.oldest:
        bug_id, sev, seconds = b.oldest
        parts.append(f"oldest open S1/S2 {bug_id} {sev} {ix.span(seconds)}")
    else:
        parts.append("oldest open S1/S2 none")
    return ' · '.join(parts)


def features_cell(root, product, now=None):
    """``landed today 2 (F-0095) · building 3 · spec/plan 4 · decided, waiting 6 · undecided 11 ·
    median to land 3d (7d, n=5)``"""
    from asf.views import status
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return status.not_configured('backlog_dir (no index.json)')
    items, _generated = ix.load(root)
    window_days = product.conventions.get('land_window_days')
    f = features(items, _runs(product), now or dt.datetime.now(dt.timezone.utc), window_days)
    if f.total == 0:
        return "no Features in the record"
    landed = f"landed today {len(f.landed_today)}"
    if f.landed_today:
        landed += f" ({f.landed_today[0]})"
    parts = [landed, f"building {f.building}", f"spec/plan {f.spec_plan}",
             f"decided, waiting {f.decided_waiting}", f"undecided {f.undecided}"]
    if f.median_to_land is None:
        parts.append(f"median to land unknown ({f.window_days}d, n={f.measured})")
    else:
        parts.append(f"median to land {ix.span(f.median_to_land)} ({f.window_days}d, n={f.measured})")
    return ' · '.join(parts)
