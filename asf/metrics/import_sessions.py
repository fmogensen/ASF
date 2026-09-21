"""asf.metrics.import_sessions — one-shot import of a pre-asf runner's logs (``asf import-sessions``).

The live sources are the factory's own: the session registry
``~/.ASF/state/<product>/sessions.jsonl`` and the job logs ``~/.ASF/logs/jobs/<product>/*.jsonl``
(see :func:`asf.metrics.metrics.sessions_from_registry`). This module is the adapter for what a
*previous* runner left behind, imported once so the history does not start at the cutover::

    asf import-sessions --product p --file <path> --format legacy-results [--log <runner log>]

It is — with ``asf/conventions.py`` — exempt from ``tools/check_conventions.sh``: every literal
below describes the foreign file's shape, not a convention of this factory.

``--format legacy-results``
    A ``session-results.jsonl``: one JSON object per line, appended as a session ends, **the
    last line for a task wins**. Keys read (everything else is ignored)::

        {"task": "spec-free-plan-r2",   # the job name; the event's `task`
         "account": "acct-a",           # which account ran it
         "branch": "spec/free-plan",    # the branch it worked on, may be absent
         "result": "finished",          # finished | failed | … — free text
         "reason": "tests green",       # optional one-line detail
         "ended_at": "2026-09-20T14:05:00Z"}   # required: an event with no end time is skipped

    Start times (and so ``minutes``) come from the runner log's ``LAUNCH <task>`` lines when one
    is given, else from the mtime of the brief the launch directory kept for that task.

``--format legacy-log``
    The runner's own log: ``HH:MM <text>`` lines with no dates. Yields ``ticks`` events — one per
    ``WAVE HHMM:`` wave — and the batch→PR table the ci backfill uses.

Both formats are read-only and the append is idempotent (the stream's natural key), so a
re-import of the same file is a no-op.
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re

from asf import env
from asf.metrics import metrics
from asf.record import match

#: The launch directory of the pre-asf runner: ``<dir>/<account>/dispatched.json`` and
#: ``<dir>/<account>/brief-<task>.md``. No default — the operator passes ``--launch-dir``.
LOG_LINE = re.compile(r'^(\d\d):(\d\d) (.*)$')
CUT_LINE = re.compile(r'LAND: batch (\w+) (worktree-m-batch-[\d-]+) cut[^(]*\(([^)]*)\)')
WAVE_ID = re.compile(r'\bWAVE (\d{4}):')
WAVE_LINE = re.compile(r'WAVE (\d{4}): (\d+) launches')
QUOTA = re.compile(r'(\w+) (\d+)%/(\d+)%')
TICK_WINDOW = dt.timedelta(minutes=9)
STALL_LINE = re.compile(r'\bstall(ed)?\b|\bhung since\b', re.I)

FORMATS = ('legacy-results', 'legacy-log')


def parse_log(lines, now=None):
    """[(aware local datetime, text)] from `HH:MM text` lines. The log has no dates: the last line is today (or
    yesterday when its clock is ahead of `now`), and every step where the clock jumps forward going backwards
    is midnight."""
    now = (now or dt.datetime.now().astimezone()).astimezone()
    recs = [(int(m.group(1)), int(m.group(2)), m.group(3)) for m in map(LOG_LINE.match, lines) if m]
    out = []
    day = now.date()
    prev = None
    if recs and (recs[-1][0], recs[-1][1]) > (now.hour, now.minute):
        day -= dt.timedelta(days=1)
    for h, m, text in reversed(recs):
        if prev is not None and (h, m) > prev:
            day -= dt.timedelta(days=1)
        prev = (h, m)
        out.append((dt.datetime(day.year, day.month, day.day, h, m).astimezone(), text))
    out.reverse()
    return out


def batch_prs_from_log(recs):
    """{batch branch: [PR numbers]} from the LAND … cut lines."""
    out = {}
    for _t, text in recs:
        m = CUT_LINE.search(text)
        if m:
            out[m.group(2)] = [int(n) for n in re.findall(r'(?:^|\s)#?(\d+)(?=[\s,;)]|$)', m.group(3))]
    return out


def wave_centres(recs):
    """[(tick id, centre datetime)] for every `WAVE HHMM:` id in the log, in order of first appearance. The id is the
    tick's clock; its date is the one (yesterday, the line's own day or tomorrow) closest to the line that names it."""
    seen, out = set(), []
    for t, text in recs:
        w = WAVE_ID.search(text)
        if not w:
            continue
        hh, mm = int(w.group(1)[:2]), int(w.group(1)[2:])
        if hh > 23 or mm > 59:
            continue
        cands = [dt.datetime.combine(t.date() + dt.timedelta(days=d), dt.time(hh, mm)).astimezone() for d in (-1, 0, 1)]
        centre = min(cands, key=lambda c: abs(c - t))
        if (int(w.group(1)), centre) not in seen:
            seen.add((int(w.group(1)), centre))
            out.append((int(w.group(1)), centre))
    return out


def ticks_from_log(recs):
    """One tick event per `WAVE HHMM` (the id is the HHMM). Every other log line goes to the wave whose clock is
    nearest, within 9 minutes either side; the lines of that window give its launches (the `N launches` summary),
    merges, relaunches, batch refusals (`REFUSED … on: <file>`; a REFUSED-WRITE is a store write, not a refusal),
    stalls (`stall`, `hung since … killed`) and the quota after it."""
    cuts_by_name = {}
    for _t, text in recs:
        m = CUT_LINE.search(text)
        if m:
            cuts_by_name[m.group(1)] = len(re.findall(r'(?:^|\s)#?(\d+)(?=[\s,;)]|$)', m.group(3))) or 1
    waves = [{'tick': tid, 'centre': c, 'first': None, 'last': None, 'launches': 0, 'merges': 0, 'stalls': 0,
              'refusals': 0, 'relaunches': 0, 'quota': {}, 'refused_files': collections.Counter()}
             for tid, c in wave_centres(recs)]
    for t, text in recs:
        w = WAVE_ID.search(text)
        if w:
            cur = min((x for x in waves if x['tick'] == int(w.group(1))), key=lambda x: abs(x['centre'] - t), default=None)
        else:
            near = [x for x in waves if abs(x['centre'] - t) <= TICK_WINDOW]
            cur = min(near, key=lambda x: (abs(x['centre'] - t), x['centre']), default=None)
        if cur is None:
            continue
        cur['first'] = cur['first'] or t
        cur['last'] = t
        if w:
            lm = WAVE_LINE.search(text)
            if lm:
                cur['launches'] = int(lm.group(2))
                if 'quota after:' in text:
                    cur['quota'] = {a: {'h5': int(x), 'd7': int(y)}
                                    for a, x, y in QUOTA.findall(text.split('quota after:')[1])}
            continue
        if text.startswith('MERGE: '):
            bm = re.match(r'MERGE: batch (\w+)', text)
            cur['merges'] += cuts_by_name.get(bm.group(1), 1) if bm else 1
        if 'RELAUNCH: ' in text and ' → ' in text:
            cur['relaunches'] += 1
        if 'REFUSED' in text and 'REFUSED-WRITE' not in text:
            cur['refusals'] += 1
            fm = re.search(r'on: (\S+)', text)
            if fm:
                cur['refused_files'][fm.group(1)] += 1
        if STALL_LINE.search(text):
            cur['stalls'] += 1
    out = []
    for c in waves:
        secs = int((c['last'] - c['first']).total_seconds()) if c['first'] else 0
        out.append({'ts': metrics.iso(c['last'] or c['centre']), 'tick': c['tick'], 'duration_s': secs or None,
                    'launches': c['launches'], 'merges': c['merges'], 'stalls': c['stalls'],
                    'refusals': c['refusals'], 'relaunches': c['relaunches'], 'quota': c['quota'],
                    'refused_files': dict(c['refused_files'])})
    return out


def launch_ledger(launch_dir):
    """{(account, id): model} and {id: [accounts]} from <acct>/dispatched.json."""
    models, accts = {}, collections.defaultdict(list)
    if not launch_dir:
        return models, accts
    for p in sorted(glob.glob(os.path.join(launch_dir, '*', 'dispatched.json'))):
        acct = os.path.basename(os.path.dirname(p))
        if acct.startswith(('_', '.')):
            continue
        try:
            with open(p, encoding='utf-8') as f:
                rows = json.load(f)
        except (OSError, ValueError):
            continue
        for r in rows if isinstance(rows, list) else []:
            models[(acct, r.get('id'))] = r.get('model')
            accts[r.get('id')].append(acct)
    return models, accts


def sessions_from_results(path, launch_dir, recs, items, since_day):
    """Events for every ended session in a legacy results file (the last line per task wins)."""
    models, accts = launch_ledger(launch_dir)
    launched = {}
    for t, text in recs:
        m = re.match(r'LAUNCH\w*:?\s+(\S+)', text)
        if m:
            launched.setdefault(m.group(1), t)
    last = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                r = json.loads(line)
                last[r['task']] = r
            except (ValueError, KeyError, TypeError):
                pass
    out = []
    for task, r in last.items():
        ended = metrics.parse_ts(r.get('ended_at'))
        if ended is None or metrics.iso(ended)[:10] < since_day:
            continue
        acct = r.get('account') or (accts.get(task) or ['?'])[0]
        start = launched.get(task)
        if start is None and launch_dir:
            for a in [acct] + accts.get(task, []):
                bp = os.path.join(launch_dir, a, f"brief-{task}.md")
                if os.path.exists(bp):
                    start = dt.datetime.fromtimestamp(os.path.getmtime(bp)).astimezone()
                    break
        minutes = None
        if start is not None and start <= ended:
            minutes = round((ended - start).total_seconds() / 60, 1)
        out.append({'ts': metrics.iso(ended), 'task': task, 'account': acct,
                    'model': models.get((acct, task)) or next((models[(a, task)] for a in accts.get(task, [])), None),
                    'branch': r.get('branch') or None, 'result': str(r.get('result') or 'unknown'),
                    'reason': r.get('reason') or None, 'minutes': minutes})
    return out


# ---- the command ------------------------------------------------------------

def import_file(root, path, fmt, log=None, launch_dir=None, since_day=None, now=None):
    """Import one file into the streams under `root`. Returns a Counter of outcomes."""
    items = match.load_index(root)
    counts = collections.Counter()
    since_day = since_day or '0000-00-00'
    recs = []
    if log:
        with open(log, encoding='utf-8', errors='replace') as f:
            recs = parse_log(f.read().splitlines(), now=now)

    def put(stream, ev):
        try:
            status, _p = metrics.append_event(root, stream, metrics.validate(stream, ev, items))
        except metrics.SchemaError as e:
            counts[f'{stream} rejected'] += 1
            counts[f'reason: {e}'] += 0    # keep the key order stable; the reason is printed below
            print(f"skipped a {stream} event: {e}")
            return
        counts[f'{stream} {status}'] += 1

    if fmt == 'legacy-results':
        for ev in sessions_from_results(path, launch_dir, recs, items, since_day):
            put('sessions', ev)
    elif fmt == 'legacy-log':
        if not recs:
            with open(path, encoding='utf-8', errors='replace') as f:
                recs = parse_log(f.read().splitlines(), now=now)
        for ev in ticks_from_log(recs):
            if ev['ts'][:10] >= since_day:
                put('ticks', ev)
    else:
        raise ValueError(f'unknown --format {fmt!r} (want {", ".join(FORMATS)})')
    return counts


def cmd_import_sessions(args):
    root = args.root
    if not root:
        root = env.load_product(getattr(args, 'product', None)).backlog_dir
    root = os.path.abspath(root)
    if not os.path.isfile(args.file):
        print(f'import-sessions: no such file: {args.file}')
        return 2
    try:
        counts = import_file(root, args.file, args.format, log=args.log,
                             launch_dir=args.launch_dir, since_day=args.since)
    except ValueError as e:
        print(f'import-sessions: {e}')
        return 2
    for k in sorted(counts):
        if not k.startswith('reason: '):
            print(f'{k}: {counts[k]}')
    return 0


def register(sub):
    """``asf import-sessions`` — the controller adds one import + one call to cli.py."""
    p = sub.add_parser('import-sessions',
                       help="import a pre-asf runner's session log into the metrics streams")
    env.add_product_arg(p)
    p.add_argument('--file', required=True, help='the file to import')
    p.add_argument('--format', default='legacy-results', choices=FORMATS,
                   help='the line format of --file (see the module docstring)')
    p.add_argument('--log', help='the runner log that accompanies it (start times, batches)')
    p.add_argument('--launch-dir', help="the pre-asf runner's launch directory (models, briefs)")
    p.add_argument('--since', help='ignore events before this day (YYYY-MM-DD)')
    p.add_argument('--root', help="the record checkout (default: the product's backlog_dir)")
    p.set_defaults(func=cmd_import_sessions)
    return p


def main(argv=None):
    p = argparse.ArgumentParser(prog='asf import-sessions')
    p.add_argument('--root')
    register(p.add_subparsers(dest='command', required=True))
    args = p.parse_args(argv)
    return cmd_import_sessions(args)
