#!/usr/bin/env python3
"""metrics.py — the factory's event streams, the daily scorecard, releases and budget.

    metrics.py append <ci|sessions|ticks> '<json>' | --stdin   validate, match to items, append one line
    metrics.py rollup [<day>]                                  metrics/daily/<day>.md, item cost:, releases/, budget
    metrics.py backfill --days N                               fill the streams from the CI API and the registry

Streams are JSONL under metrics/<stream>/<YYYY-MM-DD>.jsonl: one object per line, UTC ISO timestamps,
keys sorted, no trailing spaces. See README.md "Metrics". python3 stdlib only; `gh` and `git` by subprocess.

The `sessions` stream's source is the factory's own bookkeeping: the session registry
``~/.ASF/state/<product>/sessions.jsonl`` (:mod:`asf.workers.pool` writes it as a wave launches and
as a session ends) plus each job's log ``~/.ASF/logs/jobs/<product>/<job>.jsonl``, whose result line
carries the cost and the wall clock. A *previous* runner's logs are imported once with
``asf import-sessions`` (:mod:`asf.metrics.import_sessions`), never read live.

An item's `cost:` counts only the events matched to that id (:mod:`asf.record.match`); a Feature's row
in the scorecard, and an Epic's `spend_usd`, sum the item's subtree, so nothing is counted twice.
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from asf import env
from asf.conventions import Conventions
from asf.record import frontmatter
from asf.record import match
from asf.record.index import do_index

STREAMS = ('ci', 'sessions', 'ticks')
KINDS = ('spec', 'review', 'fix', 'code', 'plan', 'preflight', 'probe', 'rebase', 'relaunch', 'launch',
         'tick', 'other')
KIND_PREFIXES = (
    (r'(spec)', 'spec'), (r'(review|prereview|rr\d*)', 'review'), (r'(fix|bouncefix|revise|hotfix)', 'fix'),
    (r'(plan)', 'plan'), (r'(preflight)', 'preflight'), (r'(probe|diag)', 'probe'),
    (r'(rebase|remerge)', 'rebase'), (r'(relaunch)', 'relaunch'), (r'(launch)', 'launch'), (r'(tick)', 'tick'),
    (r'(code)', 'code'), (r'(adjudicate|job|bounce)', 'other'),
)
#: The conventions a caller that has no Product (a test, `metrics.py` run by hand) reads.
DEFAULTS = Conventions()
#: A session record's ``kind`` (a feeder row kind) → the `sessions` stream's `kind`.
RECORD_KINDS = {'fix-bug': 'fix', 'fix': 'fix', 'task': 'code', 'code': 'code', 'spec': 'spec',
                'plan': 'plan', 'review': 'review', 'rebase': 'rebase', 'relaunch': 'relaunch'}
TS_RE = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
DAY_RE = re.compile(r'^\d{4}-\d\d-\d\d$')
ID_RE = re.compile(r'^[EFSTBDR]-\d{4}$')


# ------------------------------------------------------------------ time --

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def today():
    return now_utc().strftime('%Y-%m-%d')


def days_back(day, n):
    """The n days ending at `day`, oldest first."""
    d = dt.date.fromisoformat(day)
    return [(d - dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def parse_ts(s):
    """An ISO timestamp, 'Z', '+0200' or '+02:00' offsets, naive = local → aware datetime."""
    if not s:
        return None
    s = str(s).strip()
    try:
        d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        try:
            d = dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S%z')
        except ValueError:
            return None
    return d.astimezone() if d.tzinfo is None else d


# ---------------------------------------------------------------- schema --

class SchemaError(Exception):
    pass


REQ = object()
MATCH = object()
TS = object()

_CHECK = {
    'str': lambda v: isinstance(v, str),
    'int': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'num': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    'bool': lambda v: isinstance(v, bool),
    'list': lambda v: isinstance(v, list),
    'dict': lambda v: isinstance(v, dict),
    'null': lambda v: v is None,
}

# key: (allowed kinds, default — REQ = required; MATCH = run the matcher; TS = now)
SCHEMAS = {
    'ci': {
        'ts': (('str',), TS), 'run': (('int',), REQ), 'workflow': (('str',), REQ), 'sha': (('str',), REQ),
        'branch': (('str',), REQ), 'pr': (('int', 'null'), None), 'batch': (('str', 'bool', 'null'), None),
        'conclusion': (('str',), REQ), 'attempt': (('int',), REQ), 'minutes': (('num',), REQ),
        'jobs': (('list',), REQ), 'cancelled_minutes': (('num',), 0), 'superseded': (('bool',), False),
        'items': (('list', 'null'), MATCH), 'item_reason': (('str', 'null'), None),
    },
    'sessions': {
        'ts': (('str',), TS), 'task': (('str',), REQ), 'account': (('str',), REQ), 'model': (('str', 'null'), None),
        'kind': (('str',), MATCH), 'item': (('str', 'null'), MATCH), 'item_reason': (('str', 'null'), None),
        'branch': (('str', 'null'), None), 'result': (('str',), REQ), 'reason': (('str', 'null'), None),
        'round': (('int', 'null'), MATCH), 'pushes': (('int', 'null'), None), 'minutes': (('num', 'null'), None),
        'usd': (('num', 'null'), None), 'session': (('str', 'null'), None),
        'in_tokens': (('int', 'null'), None), 'turns': (('int', 'null'), None),
    },
    'ticks': {
        'ts': (('str',), TS), 'tick': (('int',), REQ), 'duration_s': (('num', 'null'), None),
        'launches': (('int',), REQ), 'merges': (('int',), REQ), 'stalls': (('int',), REQ),
        'refusals': (('int',), REQ), 'relaunches': (('int',), REQ), 'quota': (('dict',), {}),
        'refused_files': (('dict',), {}),
    },
}
JOB_SCHEMA = {'name': (('str',), REQ), 'conclusion': (('str',), REQ), 'runner': (('str', 'null'), None),
              'minutes': (('num',), REQ), 'failed_step': (('str', 'null'), None)}
HINTS_KEY = 'match_hints'   # {title, body, prs, files, pr_info}: matcher input, never stored


def _apply(schema, obj, where):
    """Type-check obj against schema, fill defaults; returns a new dict. Raises SchemaError."""
    unknown = sorted(set(obj) - set(schema))
    if unknown:
        raise SchemaError(f"{where}: unknown key(s) {', '.join(unknown)}")
    out = {}
    for key, (kinds, default) in schema.items():
        if key in obj:
            v = obj[key]
            if not any(_CHECK[k](v) for k in kinds):
                raise SchemaError(f"{where}: {key!r} must be {'|'.join(kinds)}, got {type(v).__name__} {v!r}")
            out[key] = v
        elif default is REQ:
            raise SchemaError(f"{where}: missing required key {key!r}")
        elif default in (MATCH, TS):
            out[key] = default
        else:
            out[key] = json.loads(json.dumps(default))
    return out


def derive_kind(task):
    for pat, kind in KIND_PREFIXES:
        if re.match(pat + r'-', task or ''):
            return kind
    return 'code'


def derive_round(task):
    m = re.search(r'-r(\d+)[a-z]?$', task or '')
    return int(m.group(1)) if m else None


def pick_item(items, ids):
    """One id out of several matches: the deepest type first (Task, Bug, Story, Feature, Epic), then id order."""
    order = ['task', 'bug', 'story', 'feature', 'epic', 'decision', 'rule']
    return sorted(ids, key=lambda i: (order.index(items[i].get('type')) if items[i].get('type') in order else 9, i))[0]


def validate(stream, obj, items, now=None):
    """The normalised event for `stream`: schema-checked, ts and derived fields filled, item(s) matched.
    Raises SchemaError with the reason."""
    if stream not in SCHEMAS:
        raise SchemaError(f"unknown stream {stream!r} (want {', '.join(STREAMS)})")
    if not isinstance(obj, dict):
        raise SchemaError(f"{stream}: an event is a JSON object")
    obj = dict(obj)
    hints = obj.pop(HINTS_KEY, None) or {}
    if not isinstance(hints, dict):
        raise SchemaError(f"{HINTS_KEY} must be an object")
    ev = _apply(SCHEMAS[stream], obj, stream)
    if ev['ts'] is TS:
        ev['ts'] = iso(now or now_utc())
    if not TS_RE.match(ev['ts']) or parse_ts(ev['ts']) is None:
        raise SchemaError(f"{stream}: 'ts' must be a UTC ISO timestamp like 2026-09-21T06:40:00Z, got {ev['ts']!r}")
    if stream == 'ci':
        if ev['attempt'] < 1:
            raise SchemaError("ci: 'attempt' must be >= 1")
        ev['jobs'] = [_apply(JOB_SCHEMA, j, f"ci.jobs[{n}]") if isinstance(j, dict)
                      else _bad(f"ci.jobs[{n}] must be an object") for n, j in enumerate(ev['jobs'])]
        if ev['items'] is MATCH:
            ids, why = match.match_event(
                items, branch=ev['branch'], pr=ev['pr'], title=hints.get('title'), body=hints.get('body'),
                prs=hints.get('prs'), pr_info=hints.get('pr_info'), files=hints.get('files') or ())
            if ev['batch'] and not hints.get('prs') and not ids:
                why = 'batch run without a PR list'
            ev['items'] = ids or None
            ev['item_reason'] = None if ids else why
    elif stream == 'sessions':
        if ev['kind'] is MATCH:
            ev['kind'] = derive_kind(ev['task'])
        if ev['kind'] not in KINDS:
            raise SchemaError(f"sessions: 'kind' must be one of {', '.join(KINDS)}, got {ev['kind']!r}")
        if ev['round'] is MATCH:
            ev['round'] = derive_round(ev['task'])
        if ev['item'] is MATCH:
            ids, why = match.match_event(items, task=ev['task'], branch=ev['branch'], title=hints.get('title'),
                                         body=hints.get('body'), pr=hints.get('pr'), files=hints.get('files') or ())
            ev['item'] = pick_item(items, ids) if ids else None
            ev['item_reason'] = (None if len(ids) <= 1 and ids else why if not ids
                                 else f"ambiguous: {', '.join(ids)}")
    else:
        for k in ('launches', 'merges', 'stalls', 'refusals', 'relaunches'):
            if ev[k] < 0:
                raise SchemaError(f"ticks: {k!r} must be >= 0")
        for acct, q in ev['quota'].items():
            if not isinstance(q, dict) or not all(k in q and _CHECK['num'](q[k]) for k in ('h5', 'd7')) or set(q) - {'h5', 'd7'}:
                raise SchemaError(f"ticks: quota[{acct!r}] must be {{\"h5\": pct, \"d7\": pct}}")
        for f, n in ev['refused_files'].items():
            if not _CHECK['int'](n):
                raise SchemaError(f"ticks: refused_files[{f!r}] must be an integer")
    if stream != 'ticks':
        ids = ev['items'] if stream == 'ci' else ([ev['item']] if ev['item'] else [])
        for i in ids or []:
            if not isinstance(i, str) or not ID_RE.match(i):
                raise SchemaError(f"{stream}: {i!r} is not an item id")
            if items and i not in items:
                raise SchemaError(f"{stream}: {i} is not in index.json — an id is never invented")
    if stream == 'ci' and ev['items'] is None and not ev['item_reason']:
        ev['item_reason'] = 'no item given'
    if stream == 'ci' and ev['items'] is not None and not ev['items']:
        ev['items'], ev['item_reason'] = None, ev['item_reason'] or 'no item given'
    return ev


def _bad(msg):
    raise SchemaError(msg)


# --------------------------------------------------------------- streams --

def stream_path(root, stream, day):
    return os.path.join(root, 'metrics', stream, f"{day}.jsonl")


def dumps(ev):
    return json.dumps(ev, sort_keys=True, ensure_ascii=False)


def natural_key(stream, ev):
    if stream == 'ci':
        return (ev['run'], ev['attempt'])
    if stream == 'sessions':
        return (ev['task'], ev['ts'])
    return (ev['tick'],)


def read_file(path):
    out = []
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def read_stream(root, stream, days=None):
    """All events of a stream, or those of the given days."""
    if days is None:
        files = sorted(glob.glob(os.path.join(root, 'metrics', stream, '*.jsonl')))
    else:
        files = [stream_path(root, stream, d) for d in days]
    out = []
    for p in files:
        out.extend(read_file(p))
    return out


def append_event(root, stream, ev):
    """Append a normalised event to its day's file. Returns ('appended'|'exists', path)."""
    path = stream_path(root, stream, ev['ts'][:10])
    key = natural_key(stream, ev)
    if any(natural_key(stream, e) == key for e in read_file(path)):
        return 'exists', path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prefix = ''
    if os.path.isfile(path) and os.path.getsize(path):
        with open(path, 'rb') as f:
            f.seek(-1, os.SEEK_END)
            prefix = '' if f.read(1) == b'\n' else '\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(prefix + dumps(ev) + '\n')
    return 'appended', path


def cmd_append(args, root):
    raw = [sys.stdin.read()] if args.stdin else [args.json]
    if not args.stdin and args.json is None:
        print("error: give the event as JSON, or --stdin", file=sys.stderr)
        return 2
    lines = [l for chunk in raw for l in chunk.splitlines() if l.strip()]
    if not lines:
        print("error: no event given", file=sys.stderr)
        return 2
    items = match.load_index(root)
    rc = 0
    for n, line in enumerate(lines, 1):
        try:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SchemaError(f"not JSON: {e}")
            ev = validate(args.stream, obj, items)
        except SchemaError as e:
            print(f"error: {e}" if len(lines) == 1 else f"error: line {n}: {e}", file=sys.stderr)
            rc = 2
            continue
        status, path = append_event(root, args.stream, ev)
        rel = os.path.relpath(path, root)
        if status == 'exists':
            print(f"no-op: already in {rel} (natural key {natural_key(args.stream, ev)})")
        else:
            print(f"appended to {rel}")
    return rc


# ---------------------------------------------------------------- rollup --

def descendants(items, iid):
    return match._descendants(items, iid)


def feature_of(items, iid):
    """The Feature at or above an item; None when it hangs off no Feature (an Epic, or a Bug filed
    straight under the product's `conventions.default_bug_epic`)."""
    seen = set()
    while iid in items and iid not in seen:
        seen.add(iid)
        if items[iid].get('type') == 'feature':
            return iid
        iid = items[iid].get('parent')
    return None


def compute_costs(ci, sessions):
    """{item id or None: {sessions, fix_rounds, runner_min (float), usd (float|None)}} from event lists.
    A CI run's minutes split evenly over the items it names; an event with no item is filed under None."""
    costs = collections.defaultdict(lambda: {'sessions': 0, 'fix_rounds': 0, 'runner_min': 0.0, 'usd': None})
    for s in sessions:
        c = costs[s.get('item')]
        c['sessions'] += 1
        c['fix_rounds'] += 1 if s.get('kind') == 'fix' else 0
        if s.get('usd') is not None:
            c['usd'] = (c['usd'] or 0.0) + s['usd']
    for r in ci:
        ids = r.get('items') or [None]
        for i in ids:
            costs[i]['runner_min'] += (r.get('minutes') or 0) / len(ids)
    return costs


def subtree_cost(items, costs, iid):
    total = {'sessions': 0, 'fix_rounds': 0, 'runner_min': 0.0, 'usd': None}
    for i in [iid] + descendants(items, iid):
        c = costs.get(i)
        if not c:
            continue
        total['sessions'] += c['sessions']
        total['fix_rounds'] += c['fix_rounds']
        total['runner_min'] += c['runner_min']
        if c['usd'] is not None:
            total['usd'] = (total['usd'] or 0.0) + c['usd']
    return total


def esc(s):
    return str(s).replace('|', '\\|').replace('\n', ' ')


def _kind_of_branch(ev, conv=None):
    """Which lane a CI run's minutes were spent in: the trunk, a merge batch, or a branch.
    `conv.branch_prefixes['batch']` names the batch lane; a product with no merge queue has
    none and only the `batch` field of the event marks one."""
    conv = conv or DEFAULTS
    b = ev.get('branch') or ''
    if ev.get('batch') or conv.branch_kind(b) == 'batch':
        return 'batch'
    return 'trunk' if conv.is_trunk(b) else 'branch'


def _pct(a, b):
    return 100 * a // max(1, b)


def _minutes(a, b):
    return round((b - a).total_seconds() / 60)


def _median_p90(mins):
    """Median and nearest-rank p90 of a non-empty list, in whole minutes."""
    xs = sorted(mins)
    n = len(xs)
    mid = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return f"median {round(mid)} min, p90 {xs[-(-9 * n // 10) - 1]} min"


def intake_latency_rows(events, conv=None):
    """The two latency rows over the `events` stream: `asf inbox` → decided (the `intake` event's
    `filed` to the item's first `groom_answer` deciding it) and decided → first session (that
    answer's `ts` to the item's first `launch` at or after it). Only pairs with both ends count;
    the note says how many could not be measured."""
    filed, decided, launches = {}, {}, collections.defaultdict(list)
    for ev in events:
        item, ts = ev.get('item'), parse_ts(ev.get('ts'))
        if not item or ts is None:
            continue
        kind = ev.get('kind')
        if kind == 'intake':
            f = parse_ts(ev.get('filed'))
            if f is not None and item not in filed:
                filed[item] = f
        elif kind == 'groom_answer' and ev.get('field') == 'decided' and str(ev.get('value')).lower() == 'true':
            if item not in decided or ts < decided[item]:
                decided[item] = ts
        elif kind == 'launch':
            launches[item].append(ts)
    intake_n = sum(1 for ev in events if ev.get('kind') == 'intake')
    to_decided = [_minutes(filed[i], d) for i, d in decided.items() if i in filed]
    to_launch = []
    for i, d in decided.items():
        after = [t for t in launches.get(i, ()) if t >= d]
        if after:
            to_launch.append(_minutes(d, min(after)))
    a = _median_p90(to_decided) if to_decided else 'no pairs measured'
    b = _median_p90(to_launch) if to_launch else 'no pairs measured'
    na = f"{len(to_decided)} of {intake_n} cards measured ({intake_n - len(to_decided)} not decided by a groom answer)" if intake_n else ''
    nb = f"{len(to_launch)} of {len(decided)} decided cards launched" if decided else ''
    return [('intake → decided', a, na), ('decided → first session', b, nb)]


def scorecard_rows(ci, sessions, ticks, conv=None, events=()):
    """The rows of the waste table, computed from the streams."""
    conv = conv or DEFAULTS
    rows = []
    n = len(ci)
    green = sum(1 for r in ci if r['conclusion'] == 'success')
    red = sum(1 for r in ci if r['conclusion'] == 'failure')
    reruns = sum(1 for r in ci if r['attempt'] > 1)
    rows.append(('ci runs', f"{n} — {green} green, {red} red, {reruns} reruns", f"red rate {_pct(red, n)} %"))
    total = sum(r['minutes'] for r in ci)
    cancelled = collections.Counter()
    for r in ci:
        cancelled[_kind_of_branch(r, conv)] += r.get('cancelled_minutes') or 0
    canc_total = sum(cancelled.values())
    useful = round(100 * (total - canc_total) / max(1, total))
    note = ', '.join(f"{k} {v} ({_pct(v, total)} %)" for k, v in sorted(cancelled.items(), key=lambda x: (-x[1], x[0])) if v)
    rows.append(('runner-minutes', f"{total} ({total // 60} h) — useful {useful} %",
                 f"cancelled: {note or 'none'}" + (f" — {_pct(canc_total, total)} % of the minutes" if canc_total else '')))
    byjob = collections.defaultdict(lambda: [0, 0])
    sig = collections.Counter()
    for r in ci:
        for j in r['jobs']:
            byjob[j['name']][0] += 1
            if j['conclusion'] == 'failure':
                byjob[j['name']][1] += 1
                sig[f"{j['name']}: {(j.get('failed_step') or '?')[:50]}"] += 1
    for name, (t, f) in sorted(((k, v) for k, v in byjob.items() if v[1]), key=lambda x: (-x[1][1], x[0]))[:4]:
        rows.append((f"red job: {name}", f"{f}/{t} ({_pct(f, t)} %)", ''))
    for s, c in sorted(sig.items(), key=lambda x: (-x[1], x[0]))[:4]:
        rows.append(('red signature', f"{c}×", s))
    cut = {r['batch'] for r in ci if isinstance(r.get('batch'), str)}
    refused = sum(t['refusals'] for t in ticks)
    files = collections.Counter()
    for t in ticks:
        files.update(t.get('refused_files') or {})
    ftxt = ', '.join(f"{f} ×{c}" for f, c in sorted(files.items(), key=lambda x: (-x[1], x[0]))[:3]) or '—'
    rows.append(('batches', f"{len(cut)} cut, {refused} refused", f"refused on: {ftxt}"))
    launches = sum(t['launches'] for t in ticks)
    merges = sum(t['merges'] for t in ticks)
    relaunch = sum(t['relaunches'] for t in ticks)
    rows.append(('agents', f"{launches} launches in {len(ticks)} ticks, {relaunch} relaunches",
                 f"{merges} PRs merged → {round(launches / max(1, merges), 1)} launches per merged PR"))
    spec = collections.defaultdict(int)
    for s in sessions:
        if s['kind'] == 'review' and ('spec' in s['task'] or 'spec' in (s.get('branch') or '')):
            k = s.get('item') or s.get('branch') or s['task']
            spec[k] = max(spec[k], s.get('round') or 1)
    rounds = sorted(spec.values())
    rows.append(('spec quality', f"{len(rounds)} specs reviewed, {sum(1 for x in rounds if x == 1)} in round 1",
                 f"mean {round(sum(rounds) / max(1, len(rounds)), 2)} rounds, median {rounds[len(rounds) // 2] if rounds else 0}"))
    quota = {}
    for t in sorted(ticks, key=lambda t: t['ts']):
        quota.update(t.get('quota') or {})
    runner_min = collections.Counter()
    for r in ci:
        for j in r['jobs']:
            runner_min[j.get('runner') or '?'] += j['minutes']
    rows.append(('bandwidth',
                 'cux 5h/7d: ' + (', '.join(f"{a} {q['h5']}/{q['d7']} %" for a, q in sorted(quota.items())) or '—'),
                 f"{len(runner_min)} runners seen, {sum(runner_min.values())} runner-minutes"))
    if events:
        rows.extend(intake_latency_rows(events, conv))
    return rows


def fmt_usd(v):
    return '—' if v is None else f"{v:.2f}"


def cost_table(items, ci7, sessions7):
    costs = compute_costs(ci7, sessions7)
    lines = ['| Feature | Title | Sessions | Fix rounds | Runner-min | USD |', '|---|---|--:|--:|--:|--:|']
    rows = []
    for fid in sorted(i for i, it in items.items() if it.get('type') == 'feature'):
        c = subtree_cost(items, costs, fid)
        if c['sessions'] or c['runner_min'] or c['usd'] is not None:
            rows.append((fid, items[fid].get('title', ''), c))
    rows.sort(key=lambda r: (-(r[2]['usd'] or 0), -r[2]['sessions'], r[0]))
    for fid, title, c in rows:
        lines.append(f"| {fid} | {esc(title)} | {c['sessions']} | {c['fix_rounds']} | {round(c['runner_min'])} | {fmt_usd(c['usd'])} |")
    in_feature = {'sessions': 0, 'fix_rounds': 0, 'runner_min': 0.0, 'usd': None}
    other = dict(in_feature)
    feature_ids = {fid for fid, _t, _c in rows}
    for key, c in costs.items():
        owner = feature_of(items, key) if key else None
        target = in_feature if owner in feature_ids else other
        for k in ('sessions', 'fix_rounds', 'runner_min'):
            target[k] += c[k]
        if c['usd'] is not None:
            target['usd'] = (target['usd'] or 0.0) + c['usd']
    if other['sessions'] or other['runner_min'] or other['usd'] is not None:
        lines.append(f"| (no Feature) | unattributed or outside a Feature | {other['sessions']} | {other['fix_rounds']} | {round(other['runner_min'])} | {fmt_usd(other['usd'])} |")
    tot_usd = None if in_feature['usd'] is None and other['usd'] is None else (in_feature['usd'] or 0) + (other['usd'] or 0)
    lines.append(f"| **All** | | {in_feature['sessions'] + other['sessions']} | {in_feature['fix_rounds'] + other['fix_rounds']} "
                 f"| {round(in_feature['runner_min'] + other['runner_min'])} | {fmt_usd(tot_usd)} |")
    return lines


def render_daily(root, day, items, conv=None):
    ci = read_stream(root, 'ci', [day])
    sessions = read_stream(root, 'sessions', [day])
    ticks = read_stream(root, 'ticks', [day])
    week = days_back(day, 7)
    out = [f"# Factory scorecard {day}", '',
           f"generated: {day} — from metrics/ci ({len(ci)}), metrics/sessions ({len(sessions)}), metrics/ticks ({len(ticks)})", '',
           '## Waste', '', '| Metric | Value | Note |', '|---|---|---|']
    for m, v, n in scorecard_rows(ci, sessions, ticks, conv, read_stream(root, 'events', week)):
        out.append(f"| {esc(m)} | {esc(v)} | {esc(n)} |".replace('|  |', '| |'))
    out += ['', f"## Cost per Feature (7 days)", '', f"{week[0]} … {week[-1]}; a Feature's row sums its Tasks, Stories and Bugs; "
            "a CI run's minutes are split over the items it names.", '']
    out += cost_table(items, read_stream(root, 'ci', week), read_stream(root, 'sessions', week))
    return '\n'.join(out) + '\n'


def write_if_changed(path, text):
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return True


def item_path(root, items, iid):
    return os.path.join(root, items[iid]['folder'], f"{iid}.md")


def write_costs(root, items, all_ci, all_sessions, now=None):
    """Write `cost:` (own events, all time) into each touched item's machine block, and `spend_usd` into each
    Epic's. Returns (items rewritten, {epic id: (spend, budget)})."""
    costs = compute_costs(all_ci, all_sessions)
    stamp = iso(now or now_utc())
    changed = []
    spend = {}
    for iid, it in sorted(items.items()):
        c = costs.get(iid)
        new_cost = None
        if c:
            new_cost = {'sessions': c['sessions'], 'fix_rounds': c['fix_rounds'],
                        'runner_min': int(round(c['runner_min'])), 'usd': None if c['usd'] is None else round(c['usd'], 2)}
        sub = subtree_cost(items, costs, iid) if it.get('type') == 'epic' else None
        new_spend = None if not sub or sub['usd'] is None else round(sub['usd'], 2)
        if sub is not None:
            spend[iid] = (new_spend, it.get('budget_usd'))
        want = {}
        if new_cost is not None and it.get('cost') != new_cost:
            want['cost'] = new_cost
        if sub is not None and new_spend != it.get('spend_usd') and not (new_spend is None and 'spend_usd' not in it):
            want['spend_usd'] = new_spend
        if not want:
            continue
        path = item_path(root, items, iid)
        with open(path, encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read(), path=path)
        _typed, machine = frontmatter.split_machine(meta)
        machine.update(want)
        machine['updated'] = stamp
        frontmatter.write_machine(path, machine)
        changed.append(iid)
    return changed, spend


# -------------------------------------------------------------- releases --

def gh(args, timeout=120):
    """`gh …` → stdout text, or None when it failed. The one place the rollup and the backfill call gh."""
    env_vars = {**os.environ, 'PATH': '/opt/homebrew/bin:' + os.environ.get('PATH', '')}
    try:
        p = subprocess.run(['gh'] + list(args), capture_output=True, text=True, timeout=timeout, env=env_vars)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        print(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return p.stdout


def gh_json(args, timeout=120):
    out = gh(args, timeout)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def gh_lines(args, timeout=300):
    """`gh api … --jq '<expr>|@json'` → list of decoded objects, [] on failure."""
    out = gh(args, timeout)
    res = []
    for line in (out or '').splitlines():
        try:
            res.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return res


def _resolve_product(product):
    """A `Product` from an explicit instance, a product name, or None (the operator's configured default)."""
    return product if isinstance(product, env.Product) else env.load_product(product)


def _repo_dir(repo=None, product=None):
    """The product's working checkout, for git ops (`is_ancestor`): an explicit `repo`, else the
    `$BACKLOG_PRODUCT_REPO` env var (kept as an escape hatch), else the resolved Product's `repo_dir`. Lazy: a
    caller that always passes `repo` never needs a Product or `~/.ASF` to exist."""
    return repo or os.environ.get('BACKLOG_PRODUCT_REPO') or _resolve_product(product).repo_dir


def _repo_slug(repo_slug=None, product=None):
    """The product's `owner/repo` slug, for `gh` calls: an explicit `repo_slug`, else the resolved Product's."""
    return repo_slug or _resolve_product(product).repo_slug


TRY_IT = re.compile(r'^\s*[*_>-]*\s*\**Try it:?\**:?\s*(.+?)\s*$', re.I | re.M)
TYPE_TITLES = [('epic', 'Epics'), ('feature', 'Features'), ('story', 'User Stories'), ('task', 'Tasks'), ('bug', 'Bugs')]


def deploy_runs(repo_slug=None, product=None):
    """Recent runs of the product's deploy workflow: [{headSha, conclusion, updatedAt}], newest
    first; `[]` when the product names no `conventions.deploy_workflow`."""
    # Not yet a field of `Conventions` (asf/conventions.py is out of this Task's footprint) —
    # `.get()` reads it from `extra` until it lands there, and will keep reading it once it does.
    workflow = _resolve_product(product).conventions.get('deploy_workflow')
    if not workflow:
        return []
    repo_slug = _repo_slug(repo_slug, product)
    return gh_lines(['api', f'repos/{repo_slug}/actions/workflows/{workflow}/runs?per_page=15', '--jq',
                     '.workflow_runs[]|{headSha:.head_sha,conclusion,updatedAt:.updated_at}|@json']) or []


def pr_info(n, repo_slug=None, product=None):
    """{merge_commit_sha, merged, body, title} of PR n, or None."""
    repo_slug = _repo_slug(repo_slug, product)
    d = gh_json(['api', f'repos/{repo_slug}/pulls/{n}'])
    if not d:
        return None
    return {'merge_commit_sha': d.get('merge_commit_sha'), 'merged': bool(d.get('merged_at')),
            'body': d.get('body') or '', 'title': d.get('title') or ''}


def is_ancestor(repo, commit, of):
    return subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor', commit, of],
                          capture_output=True).returncode == 0


def release_items(items, new_sha, old_sha, repo=None, product=None):
    """{item id: [(pr, try_it or None), …]}: items with a merged PR whose merge commit is in new_sha but not old_sha."""
    repo = _repo_dir(repo, product)
    found = {}
    cache = {}
    for iid, it in sorted(items.items()):
        for n in (it.get('links') or {}).get('prs') or []:
            if n not in cache:
                info = pr_info(n, product=product)
                sha = info and info['merged'] and info['merge_commit_sha']
                inside = bool(sha) and is_ancestor(repo, sha, new_sha) and not (old_sha and is_ancestor(repo, sha, old_sha))
                m = TRY_IT.search(info['body']) if info else None
                cache[n] = (inside, m.group(1) if m else None)
            if cache[n][0]:
                found.setdefault(iid, []).append((n, cache[n][1]))
    return found


def render_release(day, new_sha, old_sha, deployed_at, items, found, tag=None):
    if tag is None:
        out = [f"# Release {day} · {new_sha[:7]}", '',
               f"Deployed sha `{new_sha}` (deploy finished {deployed_at or 'unknown'})."
               + (f" Everything merged since `{old_sha[:7]}`." if old_sha else ''), '']
    else:
        out = [f"# Release {day} · {new_sha[:7]}", '',
               f"Trunk sha `{new_sha}` — the product deploys nothing, so its trunk is production (B-0077)."
               + (f" Everything landed since `{old_sha[:7]}`." if old_sha else ''), '',
               f"Tag `{tag}`.", '']
    if not found:
        out += ['No item reached prod in this release.']
        return '\n'.join(out) + '\n'
    for type_, title in TYPE_TITLES:
        ids = [i for i in sorted(found) if items[i].get('type') == type_]
        if not ids:
            continue
        out += [f"## {title}", '']
        for i in ids:
            prs = ', '.join(f"#{n}" if isinstance(n, int) else f"`{n[:7]}`" for n, _t in found[i])
            out.append(f"- [{i}](../{items[i]['folder']}/{i}.md) {items[i].get('title', '')} — {prs}")
            tries = [t for _n, t in found[i] if t]
            if tries:
                out.append(f"  - Try it: {tries[0]}")
        out.append('')
    return '\n'.join(out).rstrip('\n') + '\n'


#: Any record id a commit message can name (`task(T-0050): …`, `fix(B-0077)`).
COMMIT_ID_RE = re.compile(r'\b[A-Z]-\d{4}\b')
#: The line of a product's `conventions.version_file` that names its version.
VERSION_RE = re.compile(r"""(?im)^\s*[_"']*version[_"']*\s*[=:]\s*["']([^"']+)["']""")
#: The tag a release gets when `v<version>` is unreadable or already names an older release.
RELEASE_TAG = 'release-{day}-{sha7}'


def _git(repo, *args):
    """stdout of a git command in `repo`, or None when it fails."""
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def trunk_product(product):
    """The Product when its trunk IS production — no deploy configured (B-0077): a package, a
    library, a tool. None for a product that deploys (its releases follow the deploy) or when no
    product resolves."""
    try:
        p = _resolve_product(product)
    except env.ConfigError:
        return None
    return None if p.conventions.get('deploy_workflow') or not p.repo_dir else p


def trunk_sha(product):
    """The newest trunk sha that is production: the newest green CI run on the trunk (D-0047), or
    the trunk's head when the product runs no CI. None when neither reads."""
    from asf.evidence import evidence
    if evidence.ci_provider(product):
        green = evidence.ci_green_runs(product)
        return green[0] if green else None
    return _git(product.repo_dir, 'rev-parse', '--verify', '-q', f'origin/{product.main}^{{commit}}')


def commit_items(repo, items, new_sha, old_sha):
    """{item id: [(sha, None), …]}: the items the trunk's commits in old_sha..new_sha name."""
    rng = f'{old_sha}..{new_sha}' if old_sha else new_sha
    log = _git(repo, 'log', '--format=%H%x1f%B%x1e', rng)
    found = {}
    for entry in (log or '').split('\x1e'):
        sha, _sep, msg = entry.strip().partition('\x1f')
        for iid in dict.fromkeys(COMMIT_ID_RE.findall(msg)):
            if iid in items and (sha, None) not in found.get(iid, []):
                found.setdefault(iid, []).append((sha, None))
    return found


def product_version(repo, sha, product):
    """The version `conventions.version_file` declares at `sha`, or None."""
    path = product.conventions.get('version_file')
    text = _git(repo, 'show', f'{sha}:{path}') if path else None
    m = VERSION_RE.search(text or '')
    return m.group(1) if m else None


def cut_tag(repo, day, sha, product):
    """Tag `sha` as a release and push the tag: `v<version>` (D-0045's install pin) when that tag
    is free, else `release-<day>-<sha7>`. A tag already on `sha` is reused. The tag name, or None
    when none could be created and pushed (nothing is left behind locally then)."""
    version = product_version(repo, sha, product)
    names = ([f'v{version}'] if version else []) + [RELEASE_TAG.format(day=day, sha7=sha[:7])]
    for name in names:
        at = _git(repo, 'rev-parse', '--verify', '-q', f'refs/tags/{name}^{{commit}}')
        if at == sha:
            return name
        if at:
            continue
        if _git(repo, 'tag', '-a', name, sha, '-m', f'Release {day} · {sha[:7]}') is None:
            return None
        if _git(repo, 'push', '-q', 'origin', f'refs/tags/{name}') is None:
            _git(repo, 'tag', '-d', name)
            return None
        return name
    return None


def write_trunk_release(root, day, items, product):
    """A release of a product whose trunk is production: at most one a day, none older than the
    newest, and only when a commit naming an item landed since the last one. Tags the sha, then
    writes releases/<day>-<sha7>.md. Returns the path written, or None."""
    repo = product.repo_dir
    rdir = os.path.join(root, 'releases')
    prev = sorted(os.path.basename(p) for p in glob.glob(os.path.join(rdir, '*-*.md')))
    if prev and prev[-1][:10] >= day:
        return None
    new = trunk_sha(product)
    if not new or _git(repo, 'cat-file', '-e', f'{new}^{{commit}}') is None:
        return None
    old = None
    if prev:
        m = re.search(r'-([0-9a-f]{7,40})\.md$', prev[-1])
        old = m and _git(repo, 'rev-parse', '--verify', '-q', f'{m.group(1)}^{{commit}}')
        if old == new or (old and old.startswith(new[:7])):
            return None
    found = commit_items(repo, items, new, old)
    if not found:
        return None     # nothing landed that the record knows: a release of nothing is noise
    tag = cut_tag(repo, day, new, product)
    if not tag:
        print(f"release: could not tag {new[:7]} in {repo}; retrying next rollup", file=sys.stderr)
        return None
    path = os.path.join(rdir, f"{day}-{new[:7]}.md")
    write_if_changed(path, render_release(day, new, old, None, items, found, tag=tag))
    return path


def write_release(root, day, items, repo=None, repo_slug=None, product=None):
    """T14: one releases/<day>-<sha7>.md per new prod deploy. Returns the path written, or None.
    A product that deploys nothing releases its trunk instead (:func:`write_trunk_release`)."""
    trunk = trunk_product(product) if repo is None else None
    if trunk is not None:
        return write_trunk_release(root, day, items, trunk)
    if not any((it.get('links') or {}).get('prs') for it in items.values()):
        return None     # nothing to attribute yet: a "nothing reached prod" file would hide the real one later
    runs = [r for r in deploy_runs(repo_slug, product) if r.get('conclusion') == 'success' and r.get('headSha')]
    if not runs:
        return None
    new = runs[0]
    sha7 = new['headSha'][:7]
    rdir = os.path.join(root, 'releases')
    if glob.glob(os.path.join(rdir, f"*-{sha7}.md")):
        return None
    prev = sorted(os.path.basename(p) for p in glob.glob(os.path.join(rdir, '*-*.md')))
    old = None
    if prev:
        old = re.search(r'-([0-9a-f]{7,40})\.md$', prev[-1])
        old = old.group(1) if old else None
    if old is None:
        old = next((r['headSha'] for r in runs[1:] if r['headSha'][:7] != sha7), None)
    found = release_items(items, new['headSha'], old, repo, product)
    path = os.path.join(rdir, f"{day}-{sha7}.md")
    write_if_changed(path, render_release(day, new['headSha'], old, new.get('updatedAt'), items, found))
    return path


def cmd_rollup(args, root):
    day = args.day or today()
    if not DAY_RE.match(day):
        print(f"error: day must be YYYY-MM-DD, got {day!r}", file=sys.stderr)
        return 2
    items = match.load_index(root)
    try:
        conv = _resolve_product(getattr(args, 'product', None)).conventions
    except env.ConfigError:
        conv = DEFAULTS     # a rollup over a checkout is readable with no product config
    text = render_daily(root, day, items, conv)
    daily = os.path.join(root, 'metrics', 'daily', f"{day}.md")
    print(f"{'wrote' if write_if_changed(daily, text) else 'unchanged'} {os.path.relpath(daily, root)}")
    changed, spend = write_costs(root, items, read_stream(root, 'ci'), read_stream(root, 'sessions'))
    print(f"cost: {len(changed)} item(s) updated")
    for eid, (sp, budget) in sorted(spend.items()):
        if sp is None and budget is None:
            continue
        tail = f" of budget ${budget:g} ({100 * sp / budget:.0f} %)" if budget and sp is not None else \
            (f" (budget ${budget:g})" if budget else '')
        print(f"budget: {eid} {items[eid].get('title', '')}: spend {'—' if sp is None else '$%.2f' % sp}{tail}")
    rel = None if args.no_releases else write_release(root, day, items, product=args.product)
    print(f"release: {os.path.relpath(rel, root)}" if rel else "release: none new")
    if do_index(root) != 0:
        return 1
    return 0


# -------------------------------------------------------------- backfill --

def job_log_path(product_name, job):
    """``~/.ASF/logs/jobs/<product>/<job>.jsonl`` — read-only here; the runtime writes it."""
    return os.path.join(env.log_dir(), 'jobs', str(product_name), f'{job}.jsonl')


def _result_line(path):
    """The job log's last line when it is the session's result record, else None."""
    if not path or not os.path.isfile(path):
        return None
    last = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.strip():
                last = line
    try:
        rec = json.loads(last) if last else None
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) and rec.get('type') == 'result' else None


def session_kind(record):
    """The stream's `kind` for a session record: its row kind, else derived from the job name."""
    kind = RECORD_KINDS.get(str(record.get('kind') or '').lower())
    return kind or derive_kind(record.get('job') or '')


INPUT_USAGE_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def in_tokens(result):
    """The input a session sent: uncached, cache-written and cache-read tokens from the result's
    `usage`, summed over those present as numbers; None when none is. Cached input is re-sent
    input, so the cache warming does not read as a smaller brief."""
    usage = (result or {}).get('usage')
    if not isinstance(usage, dict):
        return None
    got = [usage[k] for k in INPUT_USAGE_KEYS
           if isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool)]
    return int(sum(got)) if got else None


def session_event(record, result, items=None):
    """One `sessions` event from a registry record (+ its log's result line, if any).
    `minutes` prefers the log's own duration, else the registry's started→ended clock; `usd` is
    the log's cost. An event whose item is not in the index is left to the matcher."""
    ended = parse_ts(record.get('ended')) or parse_ts(record.get('started'))
    if ended is None:
        return None
    minutes = None
    if result and isinstance(result.get('duration_ms'), (int, float)):
        minutes = round(result['duration_ms'] / 60000.0, 1)
    else:
        start = parse_ts(record.get('started'))
        if start is not None and start <= ended:
            minutes = round((ended - start).total_seconds() / 60, 1)
    usd = (result or {}).get('total_cost_usd')
    turns = (result or {}).get('num_turns')
    # The record may be public: a session line carries the account's INDEX in the pool, never its
    # name, and only the first line of the session's report, capped (B-0023). The registry under
    # ~/.ASF/state keeps the name and the full report.
    reason = (result or {}).get('result') or None
    if reason:
        reason = str(reason).strip().splitlines()[0][:200]
    ev = {'ts': iso(ended), 'task': record.get('job'), 'account': account_index(record.get('account')),
          'model': record.get('model'), 'kind': session_kind(record),
          'branch': record.get('branch') or None,
          'result': str(record.get('end_reason') or ('running' if not record.get('ended') else 'unknown')),
          'reason': reason,
          'minutes': minutes, 'usd': usd if isinstance(usd, (int, float)) else None,
          'session': record.get('session'), 'in_tokens': in_tokens(result),
          'turns': turns if isinstance(turns, int) and not isinstance(turns, bool) else None}
    item = record.get('item')
    if item and ID_RE.match(str(item)) and (not items or item in items):
        ev['item'] = item
    return ev


def account_index(name):
    """``a<n>``: the account's 1-based position in ``worker_pool.accounts`` — the record never
    carries an account name (B-0023). Unknown or unset → ``a?``."""
    if not name:
        return 'a?'
    try:
        from asf import env
        accounts = (env.load_config().get('worker_pool') or {}).get('accounts') or []
        for i, a in enumerate(accounts, 1):
            if (a.get('name') if isinstance(a, dict) else a) == name:
                return f'a{i}'
    except Exception:
        pass
    return 'a?'


def sessions_from_registry(product, since_day=None, items=None, state_path=None, logs_dir=None):
    """Events for every ended session of a product, from its own bookkeeping.

    The spine is the registry ``state/<p>/sessions.jsonl``; a job log under ``logs/jobs/<p>/``
    with no registry line still counts (a session the registry lost, or one launched before the
    ledger existed) — its file name is the job and its result line the whole record."""
    from asf.workers import pool as pool_mod

    records = {}
    if state_path is None and product is not None:
        records = pool_mod.load_sessions(product)
    elif state_path and os.path.isfile(state_path):
        with open(state_path, encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get('job'):
                    records.setdefault(rec['job'], {}).update(rec)
    name = product.name if isinstance(product, env.Product) else product
    if logs_dir is None and name:
        logs_dir = os.path.dirname(job_log_path(name, 'x'))
    logs = {}
    for path in sorted(glob.glob(os.path.join(logs_dir, '*.jsonl'))) if logs_dir else []:
        logs[os.path.basename(path)[: -len('.jsonl')]] = path
    for job in logs:
        records.setdefault(job, {'job': job})
    out = []
    for job in sorted(records):
        rec = records[job]
        if not rec.get('ended'):
            continue
        ev = session_event(rec, _result_line(logs.get(job) or rec.get('log')), items)
        if ev is None or (since_day and ev['ts'][:10] < since_day):
            continue
        out.append(ev)
    return out


def mins(a, b):
    try:
        return max(0, int((parse_ts(b) - parse_ts(a)).total_seconds() // 60))
    except Exception:
        return 0


def ci_from_api(days, workflow, batch_prs, items, repo_slug=None, product=None, conv=None):
    """Finished CI runs of the last `days` days, with their jobs, as validated events (not yet appended).
    A run on a branch under the product's `batch` prefix (a merge queue's cut) carries that branch as
    its `batch`; a product with no such prefix has no batch runs."""
    repo_slug = _repo_slug(repo_slug, product)
    conv = conv or DEFAULTS
    since = days_back(today(), days)[0]
    runs = [r for r in gh_lines(['api', f'repos/{repo_slug}/actions/runs?created=%3E%3D{since}&status=completed&per_page=100',
                                 '--paginate', '--jq',
                                 '.workflow_runs[]|{id,name,head_branch,head_sha,conclusion,created_at,updated_at,'
                                 'run_attempt,pr:[.pull_requests[].number]}|@json'])
            if r.get('name') == workflow]

    def jobs_of(r):
        return gh_lines(['api', f"repos/{repo_slug}/actions/runs/{r['id']}/jobs?per_page=100", '--paginate',
                         '--jq', '.jobs[]|select(.conclusion!="skipped")|{name,conclusion,runner_name,started_at,'
                                 'completed_at,failed:[.steps[]|select(.conclusion=="failure")|.name]}|@json'])

    with ThreadPoolExecutor(8) as pool:
        all_jobs = list(pool.map(jobs_of, runs))
    by_branch = collections.defaultdict(list)
    for r in runs:
        by_branch[r['head_branch']].append(r['created_at'])
    pr_cache = {}

    def info(n):
        if n not in pr_cache:
            pr_cache[n] = pr_info(n, repo_slug)
        return pr_cache[n]

    events = []
    for r, jobs in zip(runs, all_jobs):
        js = [{'name': j['name'], 'conclusion': j.get('conclusion') or 'unknown', 'runner': j.get('runner_name'),
               'minutes': mins(j.get('started_at'), j.get('completed_at')) if j.get('started_at') and j.get('completed_at') else 0,
               'failed_step': (j.get('failed') or [None])[0]} for j in jobs]
        branch = r['head_branch'] or ''
        prs = r['pr'][0] if r['pr'] else None
        batch = branch if conv.branch_kind(branch) == 'batch' else None
        hints = {}
        if batch and batch_prs.get(batch):
            hints['prs'] = batch_prs[batch]
            hints['pr_info'] = {n: {'title': (info(n) or {}).get('title'), 'body': (info(n) or {}).get('body')}
                                for n in batch_prs[batch]}
        elif prs is not None and info(prs):
            hints['title'], hints['body'] = info(prs)['title'], info(prs)['body']
        events.append({
            'ts': r['updated_at'], 'run': r['id'], 'workflow': r['name'], 'sha': r['head_sha'], 'branch': branch,
            'pr': prs, 'batch': batch, 'conclusion': r.get('conclusion') or 'unknown', 'attempt': r.get('run_attempt') or 1,
            'minutes': sum(j['minutes'] for j in js), 'jobs': js,
            'cancelled_minutes': sum(j['minutes'] for j in js if j['conclusion'] == 'cancelled'),
            'superseded': r.get('conclusion') == 'cancelled' and any(c > r['created_at'] for c in by_branch[branch]),
            HINTS_KEY: hints})
    return events


def cmd_backfill(args, root):
    """The `ci` stream from the CI API, the `sessions` stream from the factory's own registry.
    A previous runner's logs are a separate, one-shot import (`asf import-sessions`)."""
    items = match.load_index(root)
    since = days_back(today(), args.days)[0]
    counts = collections.Counter()
    product = None
    try:
        product = _resolve_product(getattr(args, 'product', None))
    except env.ConfigError as e:
        print(f"no product config: {e}", file=sys.stderr)
    conv = product.conventions if product is not None else DEFAULTS

    def put(stream, ev):
        try:
            status, _p = append_event(root, stream, validate(stream, ev, items))
        except SchemaError as e:
            print(f"skipped a {stream} event: {e}", file=sys.stderr)
            counts[f'{stream} rejected'] += 1
            return
        counts[f'{stream} {status}'] += 1

    if product is not None:
        for ev in ci_from_api(args.days, args.workflow, {}, items, product=product, conv=conv):
            put('ci', ev)
        for ev in sessions_from_registry(product, since_day=since, items=items):
            put('sessions', ev)
    for k in sorted(counts):
        print(f"{k}: {counts[k]}")
    return 0


# ------------------------------------------------------------------- cli --

def build_parser():
    p = argparse.ArgumentParser(prog='metrics.py')
    p.add_argument('--root', default=os.getcwd(), help='the backlog checkout (default: cwd)')
    env.add_product_arg(p)
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('append', help='append one validated event to today\'s stream file')
    a.add_argument('stream', choices=STREAMS)
    a.add_argument('json', nargs='?')
    a.add_argument('--stdin', action='store_true', help='read one JSON event per line from stdin')
    r = sub.add_parser('rollup', help='daily scorecard, item cost:, releases, budget')
    r.add_argument('day', nargs='?')
    r.add_argument('--no-releases', action='store_true')
    b = sub.add_parser('backfill',
                       help="fill the streams from the CI API and the product's session registry")
    b.add_argument('--days', type=int, required=True)
    b.add_argument('--workflow', default='ci')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = os.path.abspath(args.root)
    return {'append': cmd_append, 'rollup': cmd_rollup, 'backfill': cmd_backfill}[args.command](args, root)


if __name__ == '__main__':
    sys.exit(main())
