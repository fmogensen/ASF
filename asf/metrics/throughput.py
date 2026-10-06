"""asf.metrics.throughput — the factory's throughput metrics, with history: how much of what it
had it used, how often its first try held, how fast it noticed and moved.

Eight readings, each over a window of the record's streams (``metrics/<stream>/<day>.jsonl``) and
the facts the scorecard already loads, each with a per-day trend over the last
:data:`TREND_DAYS` days and a status — ``ok``, ``ALARM`` or ``n/a`` (the product has no such
lane: no forge, no self-hosted runner, no merge queue, no cloud lane, no quota reader, no price):

1. **seats** — busy seats / available seats (the local share plus the cloud lane's maximum) from
   each tick line's ``seats`` record (:func:`seats_record`, written by the wave); hourly and daily
   %; the *idle stretches*: ``release.seats.idle_min`` minutes or more below
   ``release.seats.min_pct`` while the wave had launchable rows. A quiet factory (nothing
   launchable) is never idle. Alarm: any idle stretch in the window.
2. **first_pass** — PRs whose first CI attempt (the runs of the first head CI saw, attempt 1) was
   green / PRs CI saw in the window. ``n/a`` with no forge.
3. **false_close** — landed cards later reopened / landings, in the window. Alarm: above
   ``false_close_max`` (default 0).
4. **detect** — time-to-detect: per watchdog breach (``metrics/events`` ``kind: watchdog``), the
   minutes from the fact's first observable moment to its first breach line; p50 / p90.
5. **cloud** — the cloud lane's ended runs against the local ones: finished / dead / timeout
   rate, median minutes, $ per run (``n/a`` when no run carries a price). ``n/a`` with no cloud run.
6. **merge** — PR green → landed minutes (p50 / p90) from ``ci`` and ``landings``; batches per
   hour and the red batch rate from ``gates`` (``n/a`` with no merge queue).
7. **runners** — busy runner-minutes / available per runner class of the declared ``ci.pool``
   (``n/a`` with no self-hosted runner); queue wait p50 / p90 per class from the jobs' ``queued_s``.
8. **quota** — per account, the 5-hour window's burn in %/hour and the projected time to its stop
   from successive readings (``state/quota-samples.jsonl``). ``n/a`` with no quota reader.

Beside them, :mod:`asf.metrics.reds`: the red PR runs the host shows, each told apart (real,
flaky, infra, ours, check), per day and over the last ``reds_window`` PR runs, and the run-window
targets — first pass over the last ``first_pass_window`` PRs, runner-class reds, trunk green —
whose breach lines join the alarms. The day-window first-pass row reads; its alarm is the target's.

Thresholds: the seat ones are ``release.seats.*`` (:mod:`asf.release` reads the same series for
its criterion 10); the rest are ``improve: {scorecard: {throughput: {…}}}`` (:data:`DEFAULTS`),
each ``None`` (no alarm) unless named.

Pure over its inputs; :func:`load` is the only reader. Nothing is written.
"""
import collections
import datetime
import math

UTC = datetime.timezone.utc
TREND_DAYS = 7
OK, ALARM, NA = 'ok', 'ALARM', 'n/a'

#: ``release.seats.*`` — the seat-utilisation thresholds (criterion 10 and the seats alarm).
SEAT_DEFAULTS = {'min_pct': 60, 'idle_min': 30, 'max_gap_min': 15}

#: ``improve.scorecard.throughput.*`` — the other alarms; ``None`` is no alarm.
DEFAULTS = {
    'false_close_max': 0,          # reopened / landed, as a fraction: above it is an alarm
    'first_pass_min': 0.9,         # first-pass green rate over the last first_pass_window PRs
    'first_pass_window': 50,       # … the PRs it is measured over (asf.metrics.reds)
    'runner_reds_max': 0,          # runner-class reds (runner loss, OOM, timeout) allowed …
    'runner_reds_window': 100,     # … in the last this many CI runs
    'trunk_green_window': 20,      # the last this many trunk runs of the required check: all green
    'reds_window': 100,            # visible reds are also counted over the last this many PR runs
    'detect_p90_max_min': None,    # time-to-detect p90 minutes
    'merge_wait_p90_max_min': None,  # PR green → landed p90 minutes
    'runner_queue_p90_max_min': None,  # CI queue wait p90 minutes, any class
    'cloud_dead_max': None,        # the cloud lane's dead + timeout rate, a fraction
    'quota_cap_hours_min': 1.0,    # an account projected to reach its stop sooner is an alarm
    'quota_window_min': 60,        # the readings the burn rate is taken over
    # the classes of a visible red (asf.metrics.reds); matched case-insensitively as substrings
    'check_patterns': ['dco', 'sign-off', 'signoff', 'rules', 'notes', 'conventions', 'lint',
                       'format'],      # a failed job or step that is a deterministic repo check
    'infra_steps': ['set up job', 'out of memory', 'oom', 'no space left', 'lost communication',
                    'runner'],         # a failed step that is the runner's, not the code's
    # the cancel-ledger causes (asf.ci_queue.claim_cancel) that are the factory's own cancels;
    # 'superseded' also counts a run a newer run on its branch replaced
    'own_cancel_causes': ['relief', 'stall', 'duplicate-push', 'merged-pr', 'mq-dropped',
                          'mq-reaped', 'superseded'],
}
#: the :data:`DEFAULTS` keys that are lists
LIST_KEYS = ('check_patterns', 'infra_steps', 'own_cancel_causes')

METRICS = ('seats', 'first_pass', 'false_close', 'detect', 'cloud', 'merge', 'runners', 'quota')
NAMES = {'seats': 'Seat utilisation', 'first_pass': 'First-pass PR CI green',
         'false_close': 'False-close rate', 'detect': 'Time-to-detect',
         'cloud': 'Cloud run outcomes', 'merge': 'Merge wait and throughput',
         'runners': 'CI runner use and queue wait', 'quota': 'Quota burn'}


# ------------------------------------------------------------------ helpers --

def to_dt(stamp):
    if isinstance(stamp, datetime.datetime):
        return stamp.astimezone(UTC)
    if not stamp:
        return None
    s = str(stamp).strip()
    try:
        d = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def iso(d):
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _in(stamp, start, end):
    d = to_dt(stamp)
    return d is not None and start <= d < end


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def percentile(values, p):
    """Nearest rank: a value some reading actually reached; ``None`` when there is none."""
    vals = sorted(v for v in values if _num(v))
    if not vals:
        return None
    return round(vals[max(0, math.ceil(p * len(vals)) - 1)], 1)


def _rate(a, b):
    return round(a / b, 3) if b else None


def settings(product):
    """``(seats, throughput)`` — :data:`SEAT_DEFAULTS` under ``release.seats`` and
    :data:`DEFAULTS` under ``improve.scorecard.throughput``."""
    rel = getattr(product, 'release', None)
    rel = rel if isinstance(rel, dict) else {}
    seats = dict(SEAT_DEFAULTS)
    seats.update({k: v for k, v in _numbers(rel.get('seats')).items() if k in SEAT_DEFAULTS})
    imp = getattr(product, 'improve', None)
    sc = imp.get('scorecard') if isinstance(imp, dict) else None
    tp = sc.get('throughput') if isinstance(sc, dict) else None
    out = dict(DEFAULTS)
    if isinstance(tp, dict):
        for k in DEFAULTS:
            if k in tp and (tp[k] is None or str(tp[k]).strip().lower() in ('off', 'none')):
                out[k] = None
        out.update({k: v for k, v in _numbers(tp).items() if k in DEFAULTS and k not in LIST_KEYS})
        for k in LIST_KEYS:
            if isinstance(tp.get(k), list):
                out[k] = [str(x) for x in tp[k]]
    for k in LIST_KEYS:
        out[k] = list(out[k] or ())
    return seats, out


def _numbers(block):
    out = {}
    for k, v in (block.items() if isinstance(block, dict) else ()):
        if _num(v):
            out[k] = v
        elif isinstance(v, str):
            try:
                out[k] = float(v)
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------ 1. seats --

def seats_record(local_busy, local_seats, cloud_busy, cloud_seats, launchable, cause=''):
    """The tick line's ``seats`` record (the wave writes it): busy and available per lane, the
    launchable rows the wave saw, and the top reason a launchable row waited."""
    return {'local_busy': int(local_busy), 'local_seats': int(local_seats),
            'cloud_busy': int(cloud_busy), 'cloud_seats': int(cloud_seats),
            'launchable': int(launchable), 'cause': str(cause or '')[:120]}


def seat_points(ticks):
    """``[(time, busy, available, launchable, cause)]``, oldest first, from the tick lines that
    carry a ``seats`` record with at least one seat."""
    out = []
    for t in ticks or ():
        s = t.get('seats')
        d = to_dt(t.get('ts'))
        if not isinstance(s, dict) or d is None:
            continue
        avail = int(s.get('local_seats') or 0) + int(s.get('cloud_seats') or 0)
        if avail <= 0:
            continue
        busy = int(s.get('local_busy') or 0) + int(s.get('cloud_busy') or 0)
        out.append((d, min(busy, avail), avail, int(s.get('launchable') or 0), s.get('cause') or ''))
    out.sort(key=lambda p: p[0])
    return out


def idle_stretches(points, cfg):
    """Every stretch of consecutive ticks below ``min_pct`` with launchable rows, lasting
    ``idle_min`` minutes or more (first tick to last); a gap over ``max_gap_min`` between two
    ticks ends a stretch (no reading is no evidence). Each: ``{start, end, minutes, busy,
    available, cause}`` — ``busy``/``available`` the stretch's mean, ``cause`` its commonest."""
    min_pct, idle_min, gap = cfg['min_pct'], cfg['idle_min'], cfg['max_gap_min']
    out, cur = [], []

    def close():
        if cur:
            minutes = (cur[-1][0] - cur[0][0]).total_seconds() / 60
            if minutes >= idle_min:
                causes = collections.Counter(p[4] for p in cur if p[4])
                out.append({'start': iso(cur[0][0]), 'end': iso(cur[-1][0]),
                            'minutes': round(minutes, 1),
                            'busy': round(sum(p[1] for p in cur) / len(cur), 1),
                            'available': round(sum(p[2] for p in cur) / len(cur), 1),
                            'cause': causes.most_common(1)[0][0] if causes else ''})
        cur.clear()

    for p in points:
        idle = p[3] > 0 and p[1] * 100 < min_pct * p[2]
        if cur and (p[0] - cur[-1][0]).total_seconds() / 60 > gap:
            close()
        if idle:
            cur.append(p)
        else:
            close()
    close()
    return out


def live_breach(ticks, cfg):
    """The breach line of an idle stretch still running at the newest reading, else ``None`` —
    what the tick prints each time while capacity sits wasted (:func:`asf.tick.tick.write_tick_line`)."""
    points = seat_points(ticks)
    if not points:
        return None
    stretches = idle_stretches(points, cfg)
    last = stretches[-1] if stretches else None
    if not last or to_dt(last['end']) != points[-1][0]:
        return None
    return (f"metrics: BREACH seats — {last['minutes']:g} min at {last['busy']:g}/{last['available']:g} "
            f"seats busy with launchable rows since {last['start'][11:16]}, limit {cfg['min_pct']:g} % "
            f"for {cfg['idle_min']:g} min" + (f" — {last['cause']}" if last['cause'] else ''))


def seat_util(points, start, end):
    """Busy / available over the ticks in ``[start, end)``, as a percentage; ``None`` with none."""
    pts = [p for p in points if start <= p[0] < end]
    avail = sum(p[2] for p in pts)
    return round(100 * sum(p[1] for p in pts) / avail, 1) if avail else None


def hourly(points, start, end):
    """``{hour ISO: util %}`` for each hour of ``[start, end)`` with a reading."""
    out = {}
    for p in points:
        if start <= p[0] < end:
            h = p[0].replace(minute=0, second=0, microsecond=0)
            b, a = out.get(h, (0, 0))
            out[h] = (b + p[1], a + p[2])
    return {iso(h): round(100 * b / a, 1) for h, (b, a) in sorted(out.items()) if a}


def seats_metric(ticks, start, end, cfg):
    points = seat_points(ticks)
    if not any(start <= p[0] < end for p in points):
        return _row('seats', NA, 'no tick carries a seats reading in the window')
    stretches = [s for s in idle_stretches(points, cfg)
                 if start <= to_dt(s['start']) < end or start <= to_dt(s['end']) < end]
    util = seat_util(points, start, end)
    longest = max(stretches, key=lambda s: s['minutes'], default=None)
    detail = f"{util:g} % busy; {len(stretches)} idle stretch(es)"
    if longest:
        detail += (f"; longest {longest['start'][:16]} {longest['minutes']:g} min at "
                   f"{longest['busy']:g}/{longest['available']:g}"
                   + (f" — {longest['cause']}" if longest['cause'] else ''))
    return _row('seats', ALARM if stretches else OK, detail, value=util, unit='%',
                stretches=stretches, hourly=hourly(points, start, end),
                limit=f"< {cfg['min_pct']:g} % for {cfg['idle_min']:g} min with launchable rows")


# ------------------------------------------------------------------ 2. first pass --

def first_pass(ci, start, end):
    """``(green, opened)``: per PR whose first CI run is in the window, the runs of its first head
    at attempt 1 — green when every one of them concluded success."""
    by_pr = collections.defaultdict(list)
    for r in ci or ():
        if r.get('pr') is None or r.get('batch'):
            continue
        by_pr[r['pr']].append(r)
    green = opened = 0
    for runs in by_pr.values():
        runs = sorted(runs, key=lambda r: str(r.get('ts') or ''))
        first = runs[0]
        if not _in(first.get('ts'), start, end):
            continue
        head = [r for r in runs if r.get('sha') == first.get('sha') and (r.get('attempt') or 1) == 1]
        opened += 1
        green += all(r.get('conclusion') == 'success' for r in head)
    return green, opened


def first_pass_metric(ci, start, end, cfg, forge=True):
    if not forge:
        return _row('first_pass', NA, 'no forge: PR CI is not measured')
    green, opened = first_pass(ci, start, end)
    if not opened:
        return _row('first_pass', NA, 'no PR CI run in the window')
    rate = _rate(green, opened)
    # the alarm is the run-window target's (asf.metrics.reds.targets): one reading, one breach line
    return _row('first_pass', OK, f'{green}/{opened} PRs green on the first attempt', value=rate,
                unit='rate')


# ------------------------------------------------------------------ 3. false close --

def false_close(items, start, end):
    """``(reopens, closes, ids)`` — landings (``closes`` stamps) and reopenings (``reopened``
    stamps, :func:`asf.scorecard.facts.timeline`) of every card, in the window."""
    reopens = closes = 0
    ids = []
    for iid, c in (items or {}).items():
        n = sum(1 for s in c.get('reopened') or () if _in(s, start, end))
        reopens += n
        closes += sum(1 for s in c.get('closes') or () if _in(s, start, end))
        if n:
            ids.append(iid)
    return reopens, closes, sorted(ids)


def false_close_metric(items, start, end, cfg):
    reopens, closes, ids = false_close(items, start, end)
    if not closes and not reopens:
        return _row('false_close', NA, 'nothing landed in the window')
    rate = _rate(reopens, max(closes, reopens))
    lim = cfg.get('false_close_max')
    st = ALARM if lim is not None and rate > lim else OK
    return _row('false_close', st, f'{reopens} reopened / {closes} landed'
                + (': ' + ', '.join(ids[:5]) + (' …' if len(ids) > 5 else '') if ids else ''),
                value=rate, unit='rate', limit=None if lim is None else f'≤ {lim:g}')


# ------------------------------------------------------------------ 4. detect --

def detect_minutes(events, start, end):
    """Per watched ``(state, key)`` whose first breach line is in the window, its age then: the
    minutes from the fact's first observable moment to the moment ASF said so."""
    first = {}
    for e in sorted((e for e in events or () if e.get('kind') == 'watchdog'),
                    key=lambda e: str(e.get('ts') or '')):
        k = (e.get('state'), e.get('key'))
        if k not in first:
            first[k] = e
    return [e.get('age_min') for e in first.values()
            if _in(e.get('ts'), start, end) and _num(e.get('age_min'))]


def detect_metric(events, start, end, cfg):
    mins = detect_minutes(events, start, end)
    if not mins:
        return _row('detect', NA, 'no watchdog breach in the window')
    p50, p90 = percentile(mins, 0.5), percentile(mins, 0.9)
    lim = cfg.get('detect_p90_max_min')
    st = ALARM if lim is not None and p90 > lim else OK
    return _row('detect', st, f'{len(mins)} breach(es): p50 {p50:g} min, p90 {p90:g} min',
                value=p90, unit='min', p50=p50, p90=p90, limit=None if lim is None else f'p90 ≤ {lim:g}')


# ------------------------------------------------------------------ 5. cloud --

TIMEOUT = 'timeout'
TIMEOUT_REASONS = (TIMEOUT, 'timed out')


def _states():
    from asf.workers import lifecycle
    return lifecycle.FINISHED, lifecycle.DEAD


FINISHED, DEAD = _states()


def outcome(run):
    """:data:`FINISHED` | :data:`DEAD` | :data:`TIMEOUT` for one ended run: a landed run finished;
    a timeout is its own class; any other run :func:`asf.scorecard.score.is_dead` names is dead."""
    from asf.scorecard import score
    if getattr(run, 'landed', False):
        return FINISHED
    if score.failure_class(getattr(run, 'end_reason', '')).startswith(TIMEOUT_REASONS):
        return TIMEOUT
    return DEAD if score.is_dead(run) else FINISHED


def lane_outcomes(runs):
    n = len(runs)
    c = collections.Counter(outcome(r) for r in runs)
    priced = [r.usd for r in runs if _num(getattr(r, 'usd', None))]
    mins = sorted(r.minutes for r in runs if _num(getattr(r, 'minutes', None)))
    return {'runs': n, FINISHED: _rate(c[FINISHED], n), DEAD: _rate(c[DEAD], n),
            TIMEOUT: _rate(c[TIMEOUT], n), 'median_min': percentile(mins, 0.5),
            'usd_per_run': round(sum(priced) / len(priced), 2) if priced else None}


def cloud_metric(runs, start, end, cfg):
    win = [r for r in runs or () if _in(getattr(r, 'ended', None), start, end)]
    cloud = [r for r in win if getattr(r, 'cloud', False)]
    if not cloud:
        return _row('cloud', NA, 'no cloud run ended in the window')
    c, l = lane_outcomes(cloud), lane_outcomes([r for r in win if not getattr(r, 'cloud', False)])

    def side(name, v):
        if not v['runs']:
            return f'{name}: none'
        usd = 'n/a' if v['usd_per_run'] is None else f"${v['usd_per_run']:,.2f}"
        return (f"{name} {v['runs']} runs: {v[FINISHED] * 100:.0f} % finished, "
                f"{v[DEAD] * 100:.0f} % dead, {v[TIMEOUT] * 100:.0f} % timeout, "
                f"median {v['median_min'] if v['median_min'] is not None else '—'} min, {usd}/run")
    bad = (c[DEAD] or 0) + (c[TIMEOUT] or 0)
    lim = cfg.get('cloud_dead_max')
    st = ALARM if lim is not None and bad > lim else OK
    return _row('cloud', st, side('cloud', c) + '; ' + side('local', l), value=round(bad, 3),
                unit='rate', cloud=c, local=l, limit=None if lim is None else f'dead+timeout ≤ {lim:g}')


# ------------------------------------------------------------------ 6. merge --

def merge_waits(ci, landings, start, end):
    """Per landing in the window whose branch had a green CI run before it: minutes from the
    last such green run to the landing."""
    green = collections.defaultdict(list)
    for r in ci or ():
        if r.get('conclusion') == 'success' and r.get('branch'):
            d = to_dt(r.get('ts'))
            if d:
                green[r['branch']].append(d)
    out = []
    for la in landings or ():
        d = to_dt(la.get('ts'))
        if d is None or not (start <= d < end):
            continue
        before = [g for g in green.get(la.get('branch') or '', ()) if g <= d]
        if before:
            out.append(round((d - max(before)).total_seconds() / 60, 1))
    return out


def merge_metric(ci, landings, gates, start, end, cfg, forge=True):
    waits = merge_waits(ci, landings, start, end) if forge else []
    batches = [g for g in gates or () if _in(g.get('ts'), start, end)]
    if not waits and not batches:
        return _row('merge', NA, 'no landing after a green PR run and no batch in the window')
    hours = max((end - start).total_seconds() / 3600, 1e-9)
    p50, p90 = percentile(waits, 0.5), percentile(waits, 0.9)
    red = sum(1 for g in batches if g.get('conclusion') != 'success')
    parts = [f'green → landed p50 {p50:g} min, p90 {p90:g} min ({len(waits)})' if waits
             else 'green → landed: n/a']
    parts.append(f'{len(batches) / hours:.2f} batches/h, {red}/{len(batches)} red' if batches
                 else 'batches: n/a (no merge queue)')
    lim = cfg.get('merge_wait_p90_max_min')
    st = ALARM if lim is not None and p90 is not None and p90 > lim else OK
    return _row('merge', st, '; '.join(parts), value=p90, unit='min', p50=p50, p90=p90,
                batches_per_hour=round(len(batches) / hours, 3) if batches else None,
                red_batch_rate=_rate(red, len(batches)),
                limit=None if lim is None else f'p90 ≤ {lim:g}')


# ------------------------------------------------------------------ 7. runners --

def runner_use(ci, start, end, classes, capacity):
    """``({class: busy runner-minutes}, {class: available runner-minutes}, {class: [queue min]})``.
    ``classes`` maps a runner name to its class; a job on an undeclared runner is ``hosted``.
    ``capacity`` is ``{class: slots}``."""
    busy, queue = collections.Counter(), collections.defaultdict(list)
    for r in ci or ():
        if not _in(r.get('ts'), start, end):
            continue
        for j in r.get('jobs') or ():
            cls = classes.get(j.get('runner') or '', 'hosted')
            if cls != 'hosted':
                busy[cls] += j.get('minutes') or 0
            q = j.get('queued_s')
            if _num(q):
                queue[cls].append(q / 60)
    minutes = (end - start).total_seconds() / 60
    avail = {cls: slots * minutes for cls, slots in capacity.items()}
    return dict(busy), avail, dict(queue)


def runners_metric(ci, start, end, cfg, pool=()):
    classes, capacity = {}, collections.Counter()
    for e in pool or ():
        cls = getattr(e, 'cls', '') or f"{getattr(e, 'provider', '')}/{getattr(e, 'role', '')}"
        classes[e.runner] = cls
        capacity[cls] += getattr(e, 'slots', 1) or 1
    busy, avail, queue = runner_use(ci, start, end, classes, capacity)
    parts, worst = [], None
    for cls in sorted(capacity):
        parts.append(f"{cls} {100 * busy.get(cls, 0) / avail[cls]:.0f} % busy" if avail[cls] else f'{cls} —')
    if not capacity:
        parts.append('runner use n/a (no self-hosted runner)')
    for cls in sorted(queue):
        p50, p90 = percentile(queue[cls], 0.5), percentile(queue[cls], 0.9)
        worst = p90 if worst is None or p90 > worst else worst
        parts.append(f'{cls} queue p50 {p50:g} min, p90 {p90:g} min')
    if not capacity and not queue:
        return _row('runners', NA, 'no self-hosted runner and no queue reading in the window')
    lim = cfg.get('runner_queue_p90_max_min')
    st = ALARM if lim is not None and worst is not None and worst > lim else OK
    return _row('runners', st, '; '.join(parts), value=worst, unit='min',
                busy={k: round(v, 1) for k, v in busy.items()},
                available={k: round(v, 1) for k, v in avail.items()},
                limit=None if lim is None else f'queue p90 ≤ {lim:g}')


# ------------------------------------------------------------------ 8. quota --

def burn(samples, now, window_min, stop=95.0):
    """``{account: {pct, per_hour, hours_to_stop}}`` from the readings of the last
    ``window_min`` minutes: per account, the run of readings since its last drop (a window that
    reset), the slope of its first to its last. ``hours_to_stop`` is ``None`` when it is not
    climbing."""
    since = now - datetime.timedelta(minutes=window_min)
    by = collections.defaultdict(list)
    for s in samples or ():
        d = to_dt(s.get('ts'))
        if d is not None and since <= d <= now and _num(s.get('five_h_pct')) and s.get('account'):
            by[s['account']].append((d, float(s['five_h_pct'])))
    out = {}
    for acct, rs in sorted(by.items()):
        rs.sort()
        seg = [rs[0]]
        for r in rs[1:]:
            seg = [r] if r[1] < seg[-1][1] else seg + [r]
        (t0, p0), (t1, p1) = seg[0], seg[-1]
        h = (t1 - t0).total_seconds() / 3600
        rate = round((p1 - p0) / h, 1) if h > 0 else None
        left = None
        if rate and rate > 0:
            left = round(max(0.0, stop - p1) / rate, 2)
        out[acct] = {'pct': p1, 'per_hour': rate, 'hours_to_stop': left}
    return out


def quota_metric(samples, now, cfg, stop=95.0):
    b = burn(samples, now, cfg.get('quota_window_min') or 60, stop)
    if not b:
        return _row('quota', NA, 'no quota reading (no quota reader configured)')
    lim = cfg.get('quota_cap_hours_min')
    near = {a: v for a, v in b.items()
            if lim is not None and v['hours_to_stop'] is not None and v['hours_to_stop'] < lim}
    parts = [f"{a} {v['pct']:g} %" + (f", {v['per_hour']:+g} %/h" if v['per_hour'] is not None else '')
             + (f", stop in {v['hours_to_stop']:g} h" if v['hours_to_stop'] is not None else '')
             for a, v in b.items()]
    soonest = min((v['hours_to_stop'] for v in b.values() if v['hours_to_stop'] is not None), default=None)
    return _row('quota', ALARM if near else OK, '; '.join(parts), value=soonest, unit='h',
                accounts=b, limit=None if lim is None else f'time to stop ≥ {lim:g} h')


# ------------------------------------------------------------------ rows --

def _row(key, status, detail, value=None, unit='', limit=None, **extra):
    return dict({'key': key, 'name': NAMES[key], 'status': status, 'value': value, 'unit': unit,
                 'detail': detail, 'limit': limit}, **extra)


def breach_line(row):
    """The one dwell-style alarm line of a metric in alarm."""
    return (f"metrics: BREACH {row['key']} — {row['detail']}"
            + (f" (limit {row['limit']})" if row.get('limit') else ''))


def evaluate(f, start, end, seat_cfg, cfg):
    """Every metric's row over ``[start, end)`` from the gathered facts ``f`` (see :func:`load`)."""
    return [
        seats_metric(f['ticks'], start, end, seat_cfg),
        first_pass_metric(f['ci'], start, end, cfg, forge=f['forge']),
        false_close_metric(f['items'], start, end, cfg),
        detect_metric(f['events'], start, end, cfg),
        cloud_metric(f['runs'], start, end, cfg),
        merge_metric(f['ci'], f['landings'], f['gates'], start, end, cfg, forge=f['forge']),
        runners_metric(f['ci'], start, end, cfg, f['pool']),
        quota_metric(f['quota'], end, cfg, f.get('stop', 95.0)),
    ]


def trend(f, end, seat_cfg, cfg, days=TREND_DAYS):
    """``{metric: [value per day]}`` for the last ``days`` days, oldest first (``None`` = n/a)."""
    out = {k: [] for k in METRICS}
    day_end = end.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    for k in range(days - 1, -1, -1):
        e = day_end - datetime.timedelta(days=k)
        s = e - datetime.timedelta(days=1)
        for row in evaluate(f, s, min(e, end), seat_cfg, cfg):
            out[row['key']].append(None if row['status'] == NA else row['value'])
    return out


def compute(f, now, seat_cfg, cfg, days=TREND_DAYS):
    end = to_dt(now) + datetime.timedelta(seconds=1)
    start = end - datetime.timedelta(days=days)
    rows = evaluate(f, start, end, seat_cfg, cfg)
    tr = trend(f, end, seat_cfg, cfg, days)
    for r in rows:
        r['trend'] = tr[r['key']]
    from asf.metrics import reds
    rd = reds.compute(f, now, cfg, days)
    return {'start': iso(start), 'end': iso(end), 'rows': rows, 'reds': rd,
            'alarms': [breach_line(r) for r in rows if r['status'] == ALARM] + rd['alarms']}


def _cell(v, unit):
    if v is None:
        return '—'
    if unit == 'rate':
        return f'{v * 100:.0f}%'
    return f'{v:g}'


def render(d):
    """The scorecard's ``Throughput`` table: status, the window's reading, the per-day trend."""
    out = ['**Throughput** — last 7 days, trend oldest → today', '',
           '| Metric | Status | Reading | Trend (per day) |', '|---|---|---|---|']
    for r in d['rows']:
        trend_ = ' '.join(_cell(v, r['unit']) for v in r.get('trend') or ())
        out.append(f"| {r['name']} | {r['status']} | {r['detail'].replace('|', '/')} | {trend_} |")
    out += [''] + d['alarms'] if d['alarms'] else []
    if d.get('reds'):
        from asf.metrics import reds
        out += [''] + reds.render(d['reds']).rstrip('\n').split('\n')
    return '\n'.join(out) + '\n'


# ------------------------------------------------------------------ the reader --

def load(root, product, facts=None):
    """Every input :func:`evaluate` reads, for ``product`` and its record ``root``. A source that
    cannot be read is empty — its metric reads ``n/a``."""
    from asf.metrics.metrics import read_stream
    from asf.scorecard import facts as sfacts

    def stream(name):
        try:
            return read_stream(root, name)
        except (OSError, ValueError):
            return []
    if facts is None:
        facts = sfacts.load(root, product, forge=False)
    try:
        from asf import ci_pool
        pool = ci_pool.load_pool(product)
    except Exception:  # noqa: BLE001 — an unreadable pool is no self-hosted runner
        pool = []
    try:
        from asf import env
        from asf.workers import headroom, quota as quota_mod
        samples = headroom.read_samples()
        stop = float(quota_mod.guards_from_config(env.load_config())['stop'].get('five_h', 95))
    except Exception:  # noqa: BLE001 — no reader, no readings
        samples, stop = [], 95.0
    return {'ticks': stream('ticks'), 'events': stream('events'), 'landings': stream('landings'),
            'ci': facts.ci, 'gates': facts.gates, 'items': facts.items, 'runs': facts.runs,
            'forge': bool(getattr(product, 'repo_slug', None)), 'pool': pool,
            'main': getattr(product, 'main', None), 'claims': _claims(product),
            'quota': samples, 'stop': stop, 'as_of': facts.as_of}


def _claims(product):
    from asf.metrics import reds
    return reds.load_claims(product)


def for_product(root, product, facts=None):
    seat_cfg, cfg = settings(product)
    f = load(root, product, facts)
    return compute(f, f['as_of'], seat_cfg, cfg)
