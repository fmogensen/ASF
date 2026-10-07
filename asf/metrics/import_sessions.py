"""asf.metrics.import_sessions — one-shot import of a pre-asf runner's logs (``asf import-sessions``).

The live sources are the factory's own: the session registry
``~/.ASF/state/<product>/sessions.jsonl`` and the job logs ``~/.ASF/logs/jobs/<product>/*.jsonl``
(see :func:`asf.metrics.metrics.sessions_from_registry`). This module is the adapter for what a
*previous* runner left behind, imported once so the history does not start at the cutover::

    asf import-sessions --product p --file <path> --format legacy-results

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

    Start times (and so ``minutes``) come from the mtime of the brief the launch directory kept
    for that task.

The format is read-only and the append is idempotent (the stream's natural key), so a re-import
of the same file is a no-op.
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os

from asf import env
from asf.metrics import metrics
from asf.record import match

FORMATS = ('legacy-results',)


def launch_ledger(launch_dir):
    """{(account, id): model} and {id: [accounts]} from <acct>/dispatched.json. ``launch_dir`` is
    the pre-asf runner's launch directory: ``<dir>/<account>/dispatched.json`` and
    ``<dir>/<account>/brief-<task>.md``. No default — the operator passes ``--launch-dir``."""
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


def sessions_from_results(path, launch_dir, items, since_day):
    """Events for every ended session in a legacy results file (the last line per task wins)."""
    models, accts = launch_ledger(launch_dir)
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
        start = None
        if launch_dir:
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

def import_file(root, path, fmt, launch_dir=None, since_day=None):
    """Import one file into the streams under `root`. Returns a Counter of outcomes."""
    items = match.load_index(root)
    counts = collections.Counter()
    since_day = since_day or '0000-00-00'

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
        for ev in sessions_from_results(path, launch_dir, items, since_day):
            put('sessions', ev)
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
        counts = import_file(root, args.file, args.format,
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
