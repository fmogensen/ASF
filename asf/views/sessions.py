"""asf.views.sessions — the ``SESSIONS`` table (``asf sessions``).

Only the "Ended" group is ported: it is the one group the operator's original script itself
sourced from the product's own backlog (``metrics/sessions/<day>.jsonl``, written by
``asf metrics backfill``) rather than from operator machinery. "Working"/"Dead" need a live
session registry — heartbeat git refs in the product repo, ``~/.claude/factory.db``, the launch
ledger under ``~/.claude-workers/launch/`` — none of which is a product config field today; that
is a genuine gap, not a bug, and is called out by name in ``asf shadow-diff``.
"""
import datetime
import glob
import json
import os


def _today_and_yesterday():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    yesterday = today - datetime.timedelta(days=1)
    return [d.isoformat() for d in (yesterday, today)]


def _ended_rows(root):
    rows = []
    for day in _today_and_yesterday():
        path = os.path.join(root, 'metrics', 'sessions', f'{day}.jsonl')
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def render(root):
    rows = _ended_rows(root)
    out = [f"**SESSIONS** — {len(rows)} ended (from metrics/sessions/*.jsonl; "
           f"Working/Dead need a live session registry — not yet in product config, see "
           f"`asf shadow-diff`)"]
    out.append("")
    if not rows:
        out.append("Ended: none")
        return "\n".join(out) + "\n"
    out.append("**Ended**")
    out.append("")
    out.append("| Task | Item | Kind | Account | Model | Result |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        out.append("| " + " | ".join(str(r.get(k, '—') or '—') for k in
                    ('id', 'item', 'kind', 'account', 'model', 'result')) + " |")
    return "\n".join(out) + "\n"


def cmd_sessions(args, root):
    print(render(root), end='')
    return 0
