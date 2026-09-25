"""asf.workers.headroom — the 5-hour window as a budget a wave spends, and the account a session
limit stopped.

A quota reading says how full an account's window is *now*; a wave that launches five Opus specs
onto an account at 51% learns what they cost only when all five die on ``You've hit your session
limit``. So each launch carries an estimate of its share of the window, and the pool places a
launch on an account only while ``five_h_pct + this wave's committed launches + an allowance for
its running sessions + this launch`` stays under the 5h guard (:meth:`asf.workers.pool.Pool.headroom`).

**The estimate** (:class:`CostTable`), per ``(kind, model family)``, percent of the window:

* the fixed :data:`DEFAULT_COST` table while history is thin (fewer than :data:`MIN_RUNS` runs of
  that kind and family with a known cost, or no dollar value for a window);
* else the median dollars (``total_cost_usd`` of a run's ``result`` line) of its recent runs over
  ``quota_guards.five_h_usd`` — the dollars of 100% of a window. Not configured, it is estimated
  from history (:func:`five_h_usd_from_samples`): the pool's own ``five_h_pct`` readings
  (``~/.ASF/state/quota-samples.jsonl``, one line per account read by a tick's wave) against the
  dollars the account's runs spent between two readings of one window.

A running session holds :data:`RUNNING_ALLOWANCE` (``quota_guards.running_allowance``) of its
own estimate — what it has spent so far is already in the reading.

**A session limit.** A run whose result is the CLI's session/usage-limit message ends
``failed: quota-exhausted`` (:data:`QUOTA_EXHAUSTED`): no hold, no round, no attempt counted.
Its account is stopped until the reset the message names (:func:`reset_at`; unnamed →
:data:`DEFAULT_HOLD`) in ``~/.ASF/state/quota-limits.json`` — one file for the machine, since the
accounts are — and the item relaunches after that reset or on another account, in the worktree
its partial work sits in.
"""
import calendar
import datetime
import json
import os
import re
import statistics

from asf import env

QUOTA_EXHAUSTED = 'quota-exhausted'
#: The CLI's texts for a spent window: the session limit, the usage limit, a rate limit.
LIMIT_RE = re.compile(r"hit your (?:\w+ )?limit|(?:session|usage|weekly) limit|rate limit|"
                      r"quota (?:exceeded|exhausted)", re.I)
RESET_RE = re.compile(r'resets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?'
                      r'(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?'
                      r'(?:\s*\((?P<tz>[^)]+)\))?', re.I)
MONTHS = {calendar.month_abbr[i].lower(): i for i in range(1, 13)}
#: How long an account stays stopped when the limit message names no reset.
DEFAULT_HOLD = datetime.timedelta(hours=1)
#: A reset this far in the past is taken as today's (the message was read late), not tomorrow's.
LATE_READ = datetime.timedelta(hours=1)

#: Percent of a 5h window one launch spends, per model family and kind (``*``: any other).
DEFAULT_COST = {
    'opus': {'spec': 10, 'plan': 10, 'review': 6, 'adjudicate': 6, 'groom': 6, 'reshape': 6,
             '*': 8},
    'sonnet': {'*': 4},
    'haiku': {'*': 1},
    '*': {'*': 6},
}
FAMILIES = ('opus', 'sonnet', 'haiku')
RUNNING_ALLOWANCE = 0.5
MIN_RUNS = 3          # runs of one (kind, family) with a known cost before history replaces the table
RECENT_RUNS = 15      # the runs of one (kind, family) the median is taken over
MIN_PAIRS = 3         # sample intervals before a window's dollars are estimated
MIN_DELTA = 2         # points a reading must move for an interval to count
MAX_GAP = datetime.timedelta(minutes=90)
HISTORY = datetime.timedelta(days=7)
SAMPLES_KEEP = 5000


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def family(model):
    m = str(model or '').lower()
    return next((f for f in FAMILIES if f in m), '*')


def _num(x):
    return int(x) if float(x).is_integer() else round(float(x), 1)


# ---- a spent window ---------------------------------------------------------------

def exhausted(text):
    return bool(LIMIT_RE.search(text or ''))


def _zone(name):
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name.strip())
        except Exception:  # noqa: BLE001 — an unknown zone name reads as the machine's own
            pass
    return datetime.datetime.now().astimezone().tzinfo


def reset_at(text, now):
    """The UTC datetime the limit message ``text`` says the window resets, or None when it names
    none. ``resets 3:20pm (Europe/Copenhagen)`` is the next 15:20 in that zone — today's when it
    passed less than :data:`LATE_READ` ago (the message was read late), else tomorrow's. A dated
    reset (``resets Oct 2, 3pm``) is that day's."""
    m = RESET_RE.search(text or '')
    if not m:
        return None
    h, mi = int(m.group('h')), int(m.group('m') or 0)
    ap = (m.group('ap') or '').replace('.', '').lower()
    if ap == 'pm' and h < 12:
        h += 12
    elif ap == 'am' and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    tz = _zone(m.group('tz'))
    local = now.astimezone(tz)
    if m.group('mon'):
        month = MONTHS.get(m.group('mon').lower()[:3])
        if not month:
            return None
        try:
            cand = local.replace(month=month, day=int(m.group('day')), hour=h, minute=mi,
                                 second=0, microsecond=0)
        except ValueError:
            return None
        if cand < local - datetime.timedelta(days=180):
            cand = cand.replace(year=cand.year + 1)
    else:
        cand = local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand < local - LATE_READ:
            cand += datetime.timedelta(days=1)
    return cand.astimezone(datetime.timezone.utc)


def limits_path():
    return os.path.join(env.ASF_HOME, 'state', 'quota-limits.json')


def _read_limits():
    try:
        with open(limits_path(), encoding='utf-8') as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def record_limit(account, until, job='', product=''):
    """Stop ``account`` until ``until`` (a later record for the same account wins only when it
    reaches further)."""
    table = _read_limits()
    prev = parse_ts((table.get(account) or {}).get('until'))
    if prev is not None and prev >= until:
        return
    table[account] = {'until': iso(until), 'job': job, 'product': product, 'at': iso(now_utc())}
    path = limits_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(table, f, sort_keys=True, indent=1)
    os.replace(tmp, path)


def active_limits(now=None):
    """``{account: {until, job, product, at}}`` for every account still before its reset."""
    now = now or now_utc()
    return {a: v for a, v in _read_limits().items()
            if isinstance(v, dict) and (parse_ts(v.get('until')) or now) > now}


def note_exhausted(product, run, rec, now=None):
    """Record the stop a quota-exhausted run's result names on its account; the line to print."""
    now = now or now_utc()
    text = str((rec or {}).get('result') or '')
    until = reset_at(text, now) or now + DEFAULT_HOLD
    acct = (run or {}).get('account') or ''
    if acct:
        record_limit(acct, until, job=(run or {}).get('job', ''),
                     product=getattr(product, 'name', product) or '')
    local = until.astimezone().strftime('%H:%M')
    return (f'{acct or "?"} stopped until {local} — the item relaunches after the reset or on '
            f'another account, in its own worktree; no round spent')


def reset_label(until):
    return parse_ts(until).astimezone().strftime('%H:%M') if parse_ts(until) else '?'


# ---- the cost of one launch ---------------------------------------------------------

class CostTable:
    """``cost(kind, model)`` → percent of a 5h window. ``shares`` overrides :data:`DEFAULT_COST`
    per ``(kind, family)``; ``source`` is ``default`` or ``history``."""

    def __init__(self, shares=None, five_h_usd=None, allowance=RUNNING_ALLOWANCE):
        self.shares = dict(shares or {})
        self.five_h_usd = five_h_usd
        self.allowance = float(allowance)
        self.source = 'history' if self.shares else 'default'

    def cost(self, kind, model):
        fam = family(model)
        got = self.shares.get((kind, fam))
        if got is not None:
            return got
        table = DEFAULT_COST.get(fam) or DEFAULT_COST['*']
        return table.get(kind, table['*'])


def estimate(runs, five_h_usd=None, allowance=RUNNING_ALLOWANCE):
    """A :class:`CostTable` from ``runs`` (dicts with ``kind``, ``model``, ``usd`` and, for the
    order, ``started``): each ``(kind, family)`` with :data:`MIN_RUNS` known costs gets the median
    of its :data:`RECENT_RUNS` newest over ``five_h_usd``, rounded to half a point, at least 1."""
    if not five_h_usd or five_h_usd <= 0:
        return CostTable(five_h_usd=None, allowance=allowance)
    groups = {}
    for r in sorted(runs, key=lambda r: str(r.get('started') or '')):
        if r.get('usd') is None or not r.get('kind'):
            continue
        groups.setdefault((r['kind'], family(r.get('model'))), []).append(float(r['usd']))
    shares = {}
    for key, usd in groups.items():
        if len(usd) < MIN_RUNS:
            continue
        pct = statistics.median(usd[-RECENT_RUNS:]) * 100.0 / five_h_usd
        shares[key] = max(1, _num(round(pct * 2) / 2))
    return CostTable(shares, five_h_usd=five_h_usd, allowance=allowance)


def five_h_usd_from_samples(samples, runs):
    """The dollars of a whole 5h window, from ``samples`` (``{ts, account, five_h_pct}``) and
    ``runs`` (``{account, started, ended, usd}``): for two consecutive readings of one account in
    one window (the reading rose by at least :data:`MIN_DELTA`, at most :data:`MAX_GAP` apart),
    the dollars its runs spent between them — each run's cost spread evenly over its span — per
    point moved. An interval a run of unknown cost overlaps (still running, no result) is
    skipped. The median over at least :data:`MIN_PAIRS` intervals × 100, else None."""
    by_acct = {}
    for s in samples:
        t = parse_ts(s.get('ts'))
        if t is None or s.get('five_h_pct') is None or not s.get('account'):
            continue
        by_acct.setdefault(s['account'], []).append((t, float(s['five_h_pct'])))
    spans = {}
    for r in runs:
        start = parse_ts(r.get('started'))
        if start is None or not r.get('account'):
            continue
        spans.setdefault(r['account'], []).append((start, parse_ts(r.get('ended')), r.get('usd')))
    ratios = []
    for acct, pts in by_acct.items():
        pts.sort()
        for (ta, pa), (tb, pb) in zip(pts, pts[1:]):
            if not (datetime.timedelta(0) < tb - ta <= MAX_GAP) or pb - pa < MIN_DELTA:
                continue
            usd, known = 0.0, True
            for start, end, cost in spans.get(acct, ()):
                if end is not None and (end <= ta or start >= tb):
                    continue
                if start >= tb:
                    continue
                if end is None or cost is None:
                    known = False
                    break
                span = max((end - start).total_seconds(), 1.0)
                lap = (min(end, tb) - max(start, ta)).total_seconds()
                usd += float(cost) * max(lap, 0.0) / span
            if known and usd > 0:
                ratios.append(usd / (pb - pa))
    if len(ratios) < MIN_PAIRS:
        return None
    return round(statistics.median(ratios) * 100.0, 2)


# ---- history off disk -----------------------------------------------------------------

def samples_path():
    return os.path.join(env.ASF_HOME, 'state', 'quota-samples.jsonl')


def record_samples(usage, now=None):
    """Append one ``{ts, account, five_h_pct}`` line per account read (``{name: usage|None}``);
    the file is cut back to its newest :data:`SAMPLES_KEEP` lines when it grows past twice
    that. A write that fails only loses the sample."""
    ts = iso(now or now_utc())
    lines = [json.dumps({'ts': ts, 'account': a, 'five_h_pct': u.get('five_h_pct')})
             for a, u in sorted(usage.items())
             if isinstance(u, dict) and u.get('five_h_pct') is not None]
    if not lines:
        return
    path = samples_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        if os.path.getsize(path) > SAMPLES_KEEP * 2 * 80:
            with open(path, encoding='utf-8') as f:
                keep = f.readlines()[-SAMPLES_KEEP:]
            with open(path, 'w', encoding='utf-8') as f:
                f.writelines(keep)
    except OSError:
        pass


def read_samples(since=None):
    out = []
    try:
        with open(samples_path(), encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and (since is None or (parse_ts(rec.get('ts')) or since) >= since):
                    out.append(rec)
    except OSError:
        pass
    return out


def _costs_cache_path():
    return os.path.join(env.ASF_HOME, 'state', 'run-costs.json')


def segment_cost(log_path, session):
    """``(usd, exhausted)`` of the run whose log segment opens with ``session``'s ``asf`` line:
    its last ``result`` line's ``total_cost_usd`` (None when there is none)."""
    usd, hit, inside = None, False, False
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"type": "asf"' in line or '"type":"asf"' in line:
                    try:
                        inside = json.loads(line).get('session') == session
                    except ValueError:
                        inside = False
                    continue
                if not inside or ('"type":"result"' not in line and '"type": "result"' not in line):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('type') != 'result':
                    continue
                cost = rec.get('total_cost_usd')
                usd = float(cost) if isinstance(cost, (int, float)) else None
                hit = exhausted(str(rec.get('result') or '')) and bool(rec.get('is_error'))
    except OSError:
        return None, False
    return usd, hit


def history_runs(state_root=None, now=None):
    """Every ended run of the last :data:`HISTORY` over every product's ledger, as ``{kind,
    model, account, started, ended, usd}`` — ``usd`` None for a run with no cost or one a limit
    cut short. Costs are cached per session in ``~/.ASF/state/run-costs.json`` (a run's log
    segment never changes once it ended)."""
    from asf.workers import lifecycle  # local: lifecycle imports runtime, which imports this
    state_root = state_root or os.path.join(env.ASF_HOME, 'state')
    now = now or now_utc()
    since = now - HISTORY
    try:
        with open(_costs_cache_path(), encoding='utf-8') as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}
    dirty, out = False, []
    try:
        names = sorted(os.listdir(state_root))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(state_root, name, 'sessions.jsonl')
        if not os.path.isfile(path):
            continue
        for rs in lifecycle.runs(path).values():
            for r in rs:
                start, end = parse_ts(r.get('started')), parse_ts(r.get('ended'))
                if start is None or start < since:
                    continue
                sid = r.get('session')
                usd = None
                if end is not None and sid and r.get('log'):
                    got = cache.get(sid)
                    if not isinstance(got, dict):
                        cost, hit = segment_cost(r['log'], sid)
                        got = cache[sid] = {'usd': cost, 'exhausted': hit}
                        dirty = True
                    usd = None if got.get('exhausted') else got.get('usd')
                out.append({'kind': r.get('kind'), 'model': r.get('model'),
                            'account': r.get('account'), 'started': r.get('started'),
                            'ended': r.get('ended'), 'usd': usd})
    if dirty:
        try:
            with open(_costs_cache_path(), 'w', encoding='utf-8') as f:
                json.dump(cache, f)
        except OSError:
            pass
    return out


def table_from_config(cfg, now=None):
    """The :class:`CostTable` a tick's wave places by: ``quota_guards.five_h_usd`` when set, else
    the window's dollars estimated from the samples; the fixed table when neither is known."""
    g = (cfg or {}).get('quota_guards') or {}
    g = g if isinstance(g, dict) else {}
    allowance = g.get('running_allowance', RUNNING_ALLOWANCE)
    try:
        runs = history_runs(now=now)
    except Exception:  # noqa: BLE001 — history is an improvement on the table, never a blocker
        return CostTable(allowance=allowance)
    usd = g.get('five_h_usd')
    if not usd:
        usd = five_h_usd_from_samples(read_samples(since=(now or now_utc()) - HISTORY), runs)
    return estimate(runs, five_h_usd=float(usd) if usd else None, allowance=allowance)
