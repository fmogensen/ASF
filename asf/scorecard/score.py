"""asf.scorecard.score — per-Feature rows, weekly rows and the headline, over one :class:`Facts`.

Pure: no file, no clock (the reading's clock is ``facts.as_of``).

* **cost** — a Feature's sessions' USD and tokens, and the CI minutes (``metrics/ci`` runner
  minutes plus the local gate's seconds) of every card in its subtree;
* **lead time** — days from the Feature's first History line to landed, and to production;
* **repair load** — repair sessions (correct / adjudicate / review / rebase / relaunch …) on its
  subtree, plus every session on a Bug attributed to it; correction rounds; review send-backs;
  reopens; the Bugs attributed to it (parented under it, or naming it), S1 counted apart;
* **clutter** — product-wide: stale PRs, branches no PR carries, dead sessions in the window.

A week's ``usd_per_feature`` is *all-in*: every session the week paid for, over the Features that
landed in it — the number the factory exists to lower. ``own_usd_per_feature`` is the mean of the
landed Features' own subtree spend.
"""
import datetime
import re
import statistics

from asf.scorecard.facts import ids_in, iso, to_dt

#: A session whose job name starts with one of these is repair work, not first-time work.
REPAIR_PREFIXES = ('correct', 'adjudicate', 'review', 'rereview', 'prereview', 'rebase', 'remerge',
                   'relaunch', 'bounce', 'revise', 'hotfix')
CORRECTION_PREFIXES = ('correct', 'bounce', 'revise')
#: A run that ended with one of these reasons — and did not land — is a dead session.
DEAD_PREFIXES = ('dead', 'failed', 'stopped', 'stalled', 'killed', 'timeout', 'timed out')
_JOB_RE = re.compile(r'^(.*?)-[a-z]-\d{4}')
TOKEN_KEYS = ('tokens_input', 'tokens_output', 'tokens_cache_read', 'tokens_cache_write')


# ------------------------------------------------------------ helpers --

def session_kind(ev):
    """The job's kind as its name says it (``correct-b-0101`` → ``correct``), else the stream's
    ``kind``, else ``other``."""
    task = str(ev.get('task') or '')
    m = _JOB_RE.match(task)
    if m and m.group(1):
        return m.group(1)
    return ev.get('kind') or 'other'


def is_repair(kind):
    return kind.startswith(REPAIR_PREFIXES)


def is_correction(kind):
    return any(kind.startswith(p) for p in CORRECTION_PREFIXES)


def failure_class(reason):
    """``failed: not pushed: 3 uncommitted`` → ``failed: not pushed``: the first two clauses,
    digits dropped, so one cause is one class."""
    r = re.sub(r'\d+', '', str(reason or '').strip().lower())
    parts = [p.strip() for p in r.split(':') if p.strip()]
    return ': '.join(parts[:2])[:48] or 'unknown'


def is_dead(run):
    return (not run.landed) and failure_class(run.end_reason).startswith(DEAD_PREFIXES)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def tokens_of(ev):
    """The four token dimensions summed; an older event that carries none of them falls back to
    its ``in_tokens`` (the prompt side) — never zero for a session that plainly ran."""
    if any(isinstance(ev.get(k), int) for k in TOKEN_KEYS):
        return sum(_num(ev.get(k)) for k in TOKEN_KEYS)
    return _num(ev.get('in_tokens'))


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def days_between(a, b):
    da, db = to_dt(a), to_dt(b)
    if da is None or db is None:
        return None
    return round(max(0.0, (db - da).total_seconds() / 86400), 1)


def in_window(stamp, start, end):
    d = to_dt(stamp)
    return d is not None and start <= d < end


# ------------------------------------------------------------ the tree --

def children_map(items):
    kids = {}
    for iid, it in items.items():
        if it.get('parent'):
            kids.setdefault(it['parent'], []).append(iid)
    return kids


def subtree(kids, iid):
    out, todo = [iid], list(kids.get(iid, ()))
    while todo:
        c = todo.pop()
        if c not in out:
            out.append(c)
            todo.extend(kids.get(c, ()))
    return out


def feature_of(items, iid):
    seen = set()
    while iid in items and iid not in seen:
        seen.add(iid)
        if items[iid].get('type') == 'feature':
            return iid
        iid = items[iid].get('parent')
    return None


def attributed_bugs(items, fid):
    """The Bugs a Feature caused or needed: parented under it, or naming it in their own text,
    and carded no earlier than the Feature."""
    f = items.get(fid) or {}
    out = []
    pat = re.compile(r'(?<![A-Za-z0-9])' + re.escape(fid) + r'(?![A-Za-z0-9])')
    for iid, it in items.items():
        if it.get('type') != 'bug' or it.get('removed'):
            continue
        if feature_of(items, iid) == fid or pat.search(it.get('text') or ''):
            if f.get('created') and it.get('created') and it['created'] < f['created']:
                continue
            out.append(iid)
    return sorted(out)


# ------------------------------------------------------------ per item --

def per_item(facts):
    """``{item: {usd, sessions, tokens, repair, corrections, ci_min}}`` — every session, CI run and
    gate matched to a card, the CI minutes split evenly over the cards a run names."""
    acc = {}

    def cell(i):
        return acc.setdefault(i, {'usd': 0.0, 'sessions': 0, 'tokens': 0, 'repair': 0,
                                  'corrections': 0, 'ci_min': 0.0})
    for s in facts.sessions:
        c = cell(s.get('item'))
        kind = session_kind(s)
        c['usd'] += _num(s.get('usd'))
        c['sessions'] += 1
        c['tokens'] += tokens_of(s)
        c['repair'] += 1 if is_repair(kind) else 0
        c['corrections'] += 1 if is_correction(kind) else 0
    for r in facts.ci:
        ids = r.get('items') or [None]
        for i in ids:
            cell(i)['ci_min'] += _num(r.get('minutes')) / len(ids)
    for g in facts.gates:
        ids = g.get('items') or ids_in(' '.join(g.get('branches') or ())) or [None]
        for i in ids:
            cell(i)['ci_min'] += _num(g.get('seconds')) / 60 / len(ids)
    return acc


def feature_rows(facts, only_landed=True):
    """One row per Feature (landed ones only, by default), newest landing first."""
    items = facts.items
    kids = children_map(items)
    agg = per_item(facts)
    rows = []
    for fid, f in items.items():
        if f.get('type') != 'feature' or f.get('removed'):
            continue
        if only_landed and not f.get('landed'):
            continue
        ids = subtree(kids, fid)
        bugs = attributed_bugs(items, fid)
        own = [agg[i] for i in ids if i in agg]
        on_bugs = [agg[b] for b in bugs if b in agg and b not in ids]
        rows.append({
            'id': fid, 'title': f.get('title', ''), 'created': f.get('created'),
            'lane': lane_of(f), 'ab_pair': f.get('ab_pair'),
            'landed': f.get('landed'), 'prod': f.get('prod'),
            'lead_days': days_between(f.get('created'), f.get('landed')),
            'prod_days': days_between(f.get('created'), f.get('prod')),
            'usd': round(sum(c['usd'] for c in own), 2),
            'bug_usd': round(sum(c['usd'] for c in on_bugs), 2),
            'sessions': sum(c['sessions'] for c in own),
            'tokens': sum(c['tokens'] for c in own),
            'ci_min': round(sum(c['ci_min'] for c in own + on_bugs), 1),
            'repair_sessions': sum(c['repair'] for c in own) + sum(c['sessions'] for c in on_bugs),
            'corrections': sum(c['corrections'] for c in own),
            'send_backs': sum(items[i].get('send_backs', 0) for i in ids if i in items),
            'reopens': sum(items[i].get('reopens', 0) for i in ids if i in items),
            'bugs': len(bugs),
            's1': sum(1 for b in bugs if str(items[b].get('severity') or '').upper() == 'S1'),
        })
    rows.sort(key=lambda r: r['landed'] or '', reverse=True)
    return rows


# ------------------------------------------------------------ windows --

def window_row(facts, start, end, rows=None):
    """The aggregate over ``[start, end)``: what landed and reached production in it, what the
    window paid for, and the repair and clutter it carried."""
    rows = feature_rows(facts) if rows is None else rows
    shipped = [r for r in rows if in_window(r['landed'], start, end)]
    on_prod = [r for r in rows if in_window(r['prod'], start, end)]
    sessions = [s for s in facts.sessions if in_window(s.get('ts'), start, end)]
    usd = sum(_num(s.get('usd')) for s in sessions)
    repair = sum(1 for s in sessions if is_repair(session_kind(s)))
    ci_min = (sum(_num(r.get('minutes')) for r in facts.ci if in_window(r.get('ts'), start, end))
              + sum(_num(g.get('seconds')) / 60 for g in facts.gates if in_window(g.get('ts'), start, end)))
    dead = [r for r in facts.runs if is_dead(r) and in_window(r.ended, start, end)]
    tasks = [t for t in facts.items.values()
             if t.get('type') == 'task' and in_window(t.get('landed'), start, end)]
    n = len(shipped)
    return {
        'week': start.date().isoformat(), 'start': iso(start), 'end': iso(end),
        'landed': n, 'on_prod': len(on_prod),
        'median_lead_days': median([r['lead_days'] for r in shipped]),
        'median_prod_days': median([r['prod_days'] for r in on_prod]),
        'tasks_landed': len(tasks),
        'median_task_days': median([days_between(t.get('created'), t.get('landed')) for t in tasks]),
        'usd': round(usd, 2), 'sessions': len(sessions),
        'tokens': sum(tokens_of(s) for s in sessions), 'ci_min': round(ci_min, 1),
        'usd_per_feature': round(usd / n, 2) if n else None,
        'own_usd_per_feature': round(sum(r['usd'] for r in shipped) / n, 2) if n else None,
        'repair_sessions': repair,
        'repair_per_feature': round(repair / n, 1) if n else None,
        'send_backs': sum(r['send_backs'] for r in shipped),
        'bugs': sum(r['bugs'] for r in shipped), 's1': sum(r['s1'] for r in shipped),
        'dead_sessions': len(dead),
        'dead_usd': round(sum(r.usd or 0.0 for r in dead), 2),
    }


def week_start(d):
    d = d.astimezone(datetime.timezone.utc)
    monday = (d - datetime.timedelta(days=d.weekday())).date()
    return datetime.datetime(monday.year, monday.month, monday.day, tzinfo=datetime.timezone.utc)


def weekly(facts, weeks=4, rows=None):
    """One :func:`window_row` per ISO week (Monday 00:00 UTC), the current week first."""
    rows = feature_rows(facts) if rows is None else rows
    first = week_start(to_dt(facts.as_of))
    out = []
    for k in range(weeks):
        start = first - datetime.timedelta(days=7 * k)
        out.append(window_row(facts, start, start + datetime.timedelta(days=7), rows))
    return out


def headline(facts, days=7, rows=None):
    """The rolling ``days`` window ending at ``facts.as_of`` — the ``Value`` row of ``asf status``."""
    end = to_dt(facts.as_of) + datetime.timedelta(seconds=1)
    return window_row(facts, end - datetime.timedelta(days=days), end, rows)


# ------------------------------------------------------------ lanes --

#: The two build routes a Feature takes: ``lane: direct`` (one session end to end) or the full
#: pipeline (spec → review → plan → Tasks → coder/review/correct → land).
DIRECT, FULL = 'direct', 'full'
LANES = (DIRECT, FULL)
#: The per-Feature numbers a pair compares, in print order.
PAIR_METRICS = ('lead_days', 'cost', 'sessions', 'repair_sessions', 'ci_min')


def lane_of(feature):
    """``direct`` for a ``lane: direct`` Feature, else ``full``."""
    return DIRECT if str((feature or {}).get('lane') or '').strip().lower() == DIRECT else FULL


def cost_of(row):
    """A Feature's own spend plus the spend on the Bugs attributed to it — a lane that ships
    cheaper but breaks more pays for it here."""
    return round((row.get('usd') or 0.0) + (row.get('bug_usd') or 0.0), 2)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def by_lane(facts, start, end, rows=None):
    """``{lane: {...}}`` over the Features landed in ``[start, end)``, per lane: how many, the
    median lead time from card to landed, and per Feature the $ (own + its Bugs), the sessions,
    the repair sessions and the CI minutes."""
    rows = feature_rows(facts) if rows is None else rows
    out = {}
    for lane in LANES:
        shipped = [r for r in rows if r['lane'] == lane and in_window(r['landed'], start, end)]
        out[lane] = {
            'lane': lane, 'landed': len(shipped), 'ids': [r['id'] for r in shipped],
            'median_lead_days': median([r['lead_days'] for r in shipped]),
            'usd_per_feature': _mean([cost_of(r) for r in shipped]),
            'sessions_per_feature': _mean([r['sessions'] for r in shipped]),
            'repair_per_feature': _mean([r['repair_sessions'] for r in shipped]),
            'ci_min_per_feature': _mean([r['ci_min'] for r in shipped]),
        }
    return out


def _pair_side(row):
    if row is None:
        return None
    return {'id': row['id'], 'title': row['title'], 'landed': row['landed'],
            'lead_days': row['lead_days'] if row['landed'] else None, 'cost': cost_of(row),
            'sessions': row['sessions'], 'repair_sessions': row['repair_sessions'],
            'ci_min': row['ci_min']}


def pair_table(facts, rows=None):
    """One entry per ``ab_pair`` name: its direct Feature and its full one (landed or not), each
    with its numbers, and the delta (direct − full) of every number both have — so one outlier
    Feature is read against its own partner, not pooled away. A pair that is not one direct and
    one full keeps the first of each lane by id and says so in ``note``."""
    rows = feature_rows(facts, only_landed=False) if rows is None else rows
    groups = {}
    for r in sorted(rows, key=lambda r: r['id']):
        if r.get('ab_pair'):
            groups.setdefault(r['ab_pair'], []).append(r)
    out = []
    for name in sorted(groups):
        members = groups[name]
        pick = {lane: next((r for r in members if r['lane'] == lane), None) for lane in LANES}
        d, f = _pair_side(pick[DIRECT]), _pair_side(pick[FULL])
        entry = {'pair': name, 'direct': d, 'full': f, 'delta': {}, 'note': ''}
        if len(members) != 2 or d is None or f is None:
            entry['note'] = (', '.join(f"{r['id']} {r['lane']}" for r in members)
                             + ' — a pair is one direct and one full Feature')
        if d and f:
            entry['delta'] = {k: round(d[k] - f[k], 2) for k in PAIR_METRICS
                              if d.get(k) is not None and f.get(k) is not None}
        out.append(entry)
    return out


def lanes_line(lanes, days):
    """``lanes 7 d: direct 2 landed, lead 1.5 d, $8.00/f, 2 sessions/f, 0 repair/f, 12 CI
    min/f · full …`` — the daily rollup's one line."""
    parts = []
    for lane in LANES:
        v = lanes[lane]
        if not v['landed']:
            parts.append(f"{lane} 0 landed")
            continue
        parts.append(f"{lane} {v['landed']} landed, lead {_days(v['median_lead_days'])}, "
                     f"{_money(v['usd_per_feature'])}/f, {v['sessions_per_feature']:g} sessions/f, "
                     f"{v['repair_per_feature']:g} repair/f, {v['ci_min_per_feature']:g} CI min/f")
    return f"lanes {days} d: " + ' · '.join(parts)


def _money(v):
    return '—' if v is None else f'${v:,.2f}'


def _days(v):
    return '—' if v is None else f'{v:g} d'


def headline_line(h, clutter=None):
    """``3 on prod / 5 landed (7 d) · lead 2.1 d (task 0.4 d) · $41.20/feature all-in · 3.4 repair
    sessions/feature``"""
    parts = [f"{h['on_prod']} on prod / {h['landed']} landed (7 d)",
             f"lead {_days(h['median_lead_days'])} (task {_days(h.get('median_task_days'))})",
             f"{_money(h['usd_per_feature'])}/feature all-in"
             if h['usd_per_feature'] is not None else f"{_money(h['usd'])} spent, nothing landed",
             f"{'—' if h['repair_per_feature'] is None else format(h['repair_per_feature'], 'g')} "
             "repair sessions/feature"]
    if clutter and clutter.get('stale_prs'):
        parts.append(f"{clutter['stale_prs']} stale PRs")
    return ' · '.join(parts)
