"""asf.kernel.mainmoves — a main move must cost minutes, not hours (ASF 0.2).

The operator's invariant: when the trunk's head moves (a PR lands), what that causes — branches
updated, rebases, CI reruns — stays small. Since main is non-strict the kernel no longer updates
BEHIND PRs; this module *measures* that it stays true, from facts the tick already read
(``Facts.main``, :mod:`asf.kernel.mainline`) and the plan it applied. Pure :func:`observe`: the
saved state, facts, plan and a clock in; the new state, the log lines and the finished records out.

A *move* is a tick that finds the trunk's newest commit different from the last tick's (``commits``
counts those newer than the old head). Until the next move, or ``kernel.main_move.window_ticks``
ticks, what happens is attributed to it:

- ``prs_updated``: an applied :class:`~asf.kernel.actions.UpdateBranch`, plus the minutes the PR's
  required checks then ran (CI minutes) on the new head;
- ``rebases``: a rebase round launched for a PR that turned conflicting since the last tick (it was
  not DIRTY before), and ``rebase_minutes``: the minutes its session ran (read at tick resolution,
  from the session's start to the tick that found it ended);
- ``reruns``: a new CI run or attempt on a PR head that did not change since the last tick, plus
  the minutes it ran.

``total_minutes`` is ``rebase_minutes`` plus the CI minutes. A finished move (window over, nothing
of it still running) is one line of ``state/<product>/kernel-main-moves.jsonl``: ``sha``, ``at``,
``prs_updated``, ``rebases``, ``reruns``, ``rebase_minutes``, ``ci_minutes``, ``total_minutes``,
``alarm``. The tick logs ``MAIN MOVE <sha> cost: ...`` when a move's cost changed, and
``MAIN MOVE ALARM <sha> ...`` once when one passes ``kernel.main_move.alarm_minutes``
(:func:`alarm_line` puts it in ``asf kernel status`` and ``asf kernel watch``). ``asf kernel
main-moves`` shows moves per hour and the p50/p90 cost.
"""
import datetime
import json
import os

from asf.kernel import actions as A
from asf.kernel.waits import percentile

LEDGER_FILE = 'kernel-main-moves.jsonl'
STATE_FILE = 'kernel-main-moves-state.json'

#: the defaults of ``kernel.main_move``
ALARM_MINUTES = 5.0
WINDOW_TICKS = 15

#: a move whose work still runs this many windows past its start is finished anyway
HARD_FACTOR = 2

#: the window ``asf kernel main-moves`` and the alarm line look back over, hours
LOOKBACK_H = 24


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(text):
    try:
        return datetime.datetime.strptime(str(text), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _minutes(a, b):
    return max(0.0, (b - a).total_seconds() / 60)


def _short(sha):
    return str(sha or '')[:9]


def _new_move(sha, commits, now):
    return {'sha': sha, 'at': _iso(now), 'commits': commits, 'ticks': 0, 'closed': False,
            'dirty': [], 'updated': [], 'rebase_items': {}, 'reruns': 0, 'ci_open': {},
            'ci_done_minutes': 0.0, 'alarmed': False, 'logged': ''}


def _pr_state(pr):
    runs = sorted('%s:%s' % (c.run_id, c.attempt) for c in pr.checks if c.run_id)
    return {'head': pr.head_sha, 'conflicting': bool(pr.conflicting), 'runs': runs}


def _ci_done(pr, config):
    """Whether every required check of ``pr`` has completed (none reported: not yet)."""
    req = [c for c in pr.checks if not config.required_checks or c.name in config.required_checks]
    return bool(req) and all(c.status == 'completed' for c in req)


def _rebase_minutes(move, now):
    total = 0.0
    for entry in move['rebase_items'].values():
        for j in entry['jobs'].values():
            start = _parse(j['start']) or now
            end = _parse(j.get('end')) or now
            total += _minutes(start, end)
    return total


def _ci_minutes(move, now):
    total = move['ci_done_minutes']
    for o in move['ci_open'].values():
        total += _minutes(_parse(o['start']) or now, now)
    return total


def costs(move, now):
    """``(rebase minutes, CI minutes, total minutes)`` of ``move`` at ``now``."""
    rb, ci = _rebase_minutes(move, now), _ci_minutes(move, now)
    return round(rb, 1), round(ci, 1), round(rb + ci, 1)


def _running(move):
    return bool(move['ci_open']) or any(
        not j.get('end') for e in move['rebase_items'].values() for j in e['jobs'].values())


def record_of(move, now, alarm_minutes=ALARM_MINUTES):
    """The ledger line of ``move`` as it stands at ``now``."""
    rb, ci, total = costs(move, now)
    return {'sha': move['sha'], 'at': move['at'], 'commits': move['commits'],
            'prs_updated': len(move['updated']), 'rebases': len(move['rebase_items']),
            'reruns': move['reruns'], 'rebase_minutes': rb, 'ci_minutes': ci,
            'total_minutes': total, 'alarm': bool(move['alarmed']),
            'alarm_minutes': alarm_minutes}


def cost_line(rec):
    return 'MAIN MOVE %s cost: updated %d, rebases %d, reruns %d, rebase %.1fm, total %.1fm' % (
        _short(rec['sha']), rec['prs_updated'], rec['rebases'], rec['reruns'],
        rec['rebase_minutes'], rec['total_minutes'])


def alarm_text(rec):
    return 'MAIN MOVE ALARM %s cost %.1fm > %gm: updated %d, rebases %d, reruns %d' % (
        _short(rec['sha']), rec['total_minutes'], rec['alarm_minutes'], rec['prs_updated'],
        rec['rebases'], rec['reruns'])


def observe(state, facts, plan, config, now, alarm_minutes=ALARM_MINUTES,
            window_ticks=WINDOW_TICKS):
    """``(state, lines, finished)``: the saved ``state`` after this tick's ``facts`` and applied
    ``plan`` (see the module doc), the log lines to print, and the finished ledger records."""
    state = json.loads(json.dumps(state or {}))
    moves = state.setdefault('moves', [])
    fresh = None
    prev_prs = state.get('prs')
    prs = {p.number: _pr_state(p) for p in facts.prs}
    main = list(getattr(facts, 'main', None) or [])
    head = main[0].sha if main else None
    if head and state.get('head') and head != state['head']:
        shas = [c.sha for c in main]
        commits = shas.index(state['head']) if state['head'] in shas else len(shas)
        for m in moves:
            m['closed'] = True
        fresh = _new_move(head, commits, now)
        moves.append(fresh)
    if head:
        state['head'] = head
    active = next((m for m in reversed(moves) if not m['closed']), None)
    sessions = list(facts.sessions)
    if active is not None and prev_prs is not None:
        by_num = {p.number: p for p in facts.prs}
        by_item = {p.item_id: p for p in facts.prs if p.item_id}
        for num, p in by_num.items():
            was = prev_prs.get(str(num)) or prev_prs.get(num)
            if was is None:
                continue
            if p.conflicting and not was['conflicting'] and num not in active['dirty']:
                active['dirty'].append(num)
            now_runs = prs[num]['runs']
            if was['head'] == p.head_sha and was['runs'] and set(now_runs) - set(was['runs']):
                active['reruns'] += len(set(now_runs) - set(was['runs']))
                active['ci_open'].setdefault(str(num), {'start': _iso(now), 'head': p.head_sha,
                                                        'rerun': True})
        for a in plan.actions:
            if isinstance(a, A.UpdateBranch):
                if a.pr not in active['updated']:
                    active['updated'].append(a.pr)
                p = by_num.get(a.pr)
                active['ci_open'].setdefault(str(a.pr), {'start': _iso(now),
                                                         'head': p.head_sha if p else ''})
            elif isinstance(a, A.Launch):
                from asf.kernel.decide import rebase_finding
                p = by_item.get(a.item_id)
                if (p is not None and p.number in active['dirty']
                        and any(rebase_finding(f, p.number) for f in a.findings or [])
                        and a.item_id not in active['rebase_items']):
                    active['rebase_items'][a.item_id] = {
                        'pr': p.number, 'known': [s.job for s in sessions
                                                  if s.item_id == a.item_id], 'jobs': {}}
    for m in moves:
        for iid, entry in m['rebase_items'].items():
            for s in sessions:
                if s.item_id != iid or s.job in entry['known']:
                    continue
                j = entry['jobs'].setdefault(s.job, {'start': getattr(s, 'started', '') or
                                                     _iso(now)})
                if not s.alive and not j.get('end'):
                    j['end'] = _iso(now)
        for num in list(m['ci_open']):
            o = m['ci_open'][num]
            p = next((x for x in facts.prs if str(x.number) == num), None)
            if prev_prs is None or _parse(o['start']) >= now:
                continue
            changed = o.get('rerun') or (p is not None and p.head_sha != o['head'])
            if p is None or (changed and _ci_done(p, config)):
                m['ci_done_minutes'] += _minutes(_parse(o['start']), now)
                del m['ci_open'][num]
        if m is not fresh:
            m['ticks'] += 1
        if m['ticks'] >= window_ticks:
            m['closed'] = True
    lines, finished, keep = [], [], []
    for m in moves:
        rec = record_of(m, now, alarm_minutes)
        over = rec['total_minutes'] > alarm_minutes
        if over and not m['alarmed']:
            m['alarmed'] = rec['alarm'] = True
            lines.append(alarm_text(rec))
        end = m['closed'] and (not _running(m) or m['ticks'] >= HARD_FACTOR * window_ticks)
        sig = cost_line(rec)
        if end:
            m['ci_done_minutes'] = _ci_minutes(m, now)
            m['ci_open'] = {}
            rec = record_of(m, now, alarm_minutes)
            finished.append(rec)
            lines.append(cost_line(rec))
        else:
            if sig != m['logged']:
                lines.append(sig)
                m['logged'] = sig
            keep.append(m)
    state['moves'] = keep
    state['prs'] = prs
    state['at'] = _iso(now)
    return state, lines, finished


def settings_of(product):
    """``(alarm_minutes, window_ticks)`` from ``product.kernel['main_move']``, else the defaults."""
    try:
        k = product.kernel['main_move']
        return float(k['alarm_minutes']), int(k['window_ticks'])
    except (AttributeError, KeyError, TypeError, ValueError):
        return ALARM_MINUTES, WINDOW_TICKS


def read_state(state_dir):
    try:
        with open(os.path.join(state_dir, STATE_FILE), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state_dir, state):
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, STATE_FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(state, f, sort_keys=True)
    os.replace(path + '.tmp', path)


def append(state_dir, records):
    """Append ``records`` to the ledger, one line each."""
    if not records:
        return
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, LEDGER_FILE), 'a', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + '\n')


def read(state_dir):
    """The ledger's records, oldest first (an unreadable line is skipped)."""
    out = []
    try:
        with open(os.path.join(state_dir, LEDGER_FILE), encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get('sha'):
                    out.append(r)
    except OSError:
        pass
    return out


def alarm_line(state_dir, now=None):
    """The newest ``MAIN MOVE ALARM ...`` of the last day (a finished move's record, or a move
    still open), plus ``(+N more)``; '' when none."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    lo = now - datetime.timedelta(hours=LOOKBACK_H)
    alarms = [r for r in read(state_dir) if r.get('alarm')]
    st = read_state(state_dir)
    limit = ALARM_MINUTES
    for m in st.get('moves') or []:
        if m.get('alarmed'):
            alarms.append(record_of(m, now))
    alarms = [r for r in alarms if (_parse(r.get('at')) or now) >= lo]
    if not alarms:
        return ''
    alarms.sort(key=lambda r: r.get('at') or '')
    r = dict(alarms[-1])
    r.setdefault('alarm_minutes', limit)
    return alarm_text(r) + (' (+%d more in 24h)' % (len(alarms) - 1) if len(alarms) > 1 else '')


def report(records, now=None, since=None):
    """``{'moves', 'hours', 'per_hour', 'p50', 'p90', 'max', 'alarms'}`` over the records at or
    after ``since`` (default: 24 h before ``now``)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = since or now - datetime.timedelta(hours=LOOKBACK_H)
    rows = [r for r in records if (_parse(r.get('at')) or now) >= since]
    hours = max((now - since).total_seconds() / 3600, 1e-9)
    costs_ = [float(r.get('total_minutes') or 0) for r in rows]
    return {'moves': len(rows), 'hours': hours, 'per_hour': len(rows) / hours,
            'p50': percentile(costs_, 50), 'p90': percentile(costs_, 90),
            'max': max(costs_) if costs_ else None,
            'alarms': [r for r in rows if r.get('alarm')], 'rows': rows}


def render(rep):
    """The ``asf kernel main-moves`` text."""
    if not rep['moves']:
        return 'no main moves measured in the last %.1fh' % rep['hours']
    f = lambda v: '-' if v is None else '%.1fm' % v  # noqa: E731
    out = ['%d moves in %.1fh (%.1f per hour), cost per move: p50 %s, p90 %s, max %s'
           % (rep['moves'], rep['hours'], rep['per_hour'], f(rep['p50']), f(rep['p90']),
              f(rep['max']))]
    if rep['alarms']:
        out.append('%d over the alarm limit:' % len(rep['alarms']))
        out += ['  ' + alarm_text(dict(r, alarm_minutes=r.get('alarm_minutes', ALARM_MINUTES)))
                for r in rep['alarms'][-5:]]
    return '\n'.join(out)


def main_moves(product, since=None, state_dir=None, out=print, now=None):
    """``asf kernel main-moves``: print :func:`render` of the product's ledger (no network)."""
    from asf import env
    from asf.kernel import settings
    from asf.kernel.loop import _product
    product = _product(product)
    sd = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
    t0 = settings.parse_time(since) if since else None
    if since and t0 is None:
        raise ValueError('--since must be an ISO-8601 time, not %r' % (since,))
    rep = report(read(sd), now=now, since=t0)
    text = render(rep)
    line = alarm_line(sd, now=now)
    out(text + ('\n' + line if line and 'over the alarm' not in text else ''))
    return rep
