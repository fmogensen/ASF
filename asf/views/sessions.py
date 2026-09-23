"""asf.views.sessions — the ``SESSIONS`` table (``asf sessions``).

Three groups:

* **Working** — a session in the workers' registry (``~/.ASF/state/<product>/sessions.jsonl``) with
  no ``ended`` whose pid is still alive;
* **Dead** — the same, but its pid is gone (the tick's ``health`` step will reconcile it);
* **Ended** — ``metrics/sessions/<day>.jsonl`` in the record for yesterday and today (written by
  ``asf metrics backfill``).
"""
import datetime
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


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, someone else's
    except OSError:
        return False
    return True


def live_rows(product, alive=None, session_source=None):
    """(working, dead): the registry's sessions with no ``ended``, split on whether the pid lives."""
    if product is None:
        return [], []
    from asf.workers import health as health_mod
    from asf.workers import pool as pool_mod
    live = pool_mod.live_sessions(product)
    if alive is None:
        alive = health_mod.alive_for(product, live, session_source)
    working, dead = [], []
    for s in live:
        (working if alive(s.get('pid')) else dead).append(s)
    return working, dead


LIVE_COLUMNS = ('job', 'item', 'kind', 'account', 'model', 'branch', 'started')


def _table(rows, columns, header):
    out = [f"| {' | '.join(header)} |", '|' + '---|' * len(header)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(k, '—') or '—') for k in columns) + " |")
    return out


def render(root, product=None, alive=None):
    ended = _ended_rows(root)
    working, dead = live_rows(product, alive)
    out = [f"**SESSIONS** — {len(working)} working · {len(dead)} dead · {len(ended)} ended", ""]
    header = ('Job', 'Item', 'Kind', 'Account', 'Model', 'Branch', 'Started')
    for name, rows in (('Working', working), ('Dead', dead)):
        if not rows:
            out.append(f"{name}: none")
            out.append("")
            continue
        out.append(f"**{name}**")
        out.append("")
        out.extend(_table(rows, LIVE_COLUMNS, header))
        out.append("")
    if not ended:
        out.append("Ended: none")
        return "\n".join(out) + "\n"
    out.append("**Ended**")
    out.append("")
    out.extend(_table(ended, ('id', 'item', 'kind', 'account', 'model', 'result'),
                      ('Task', 'Item', 'Kind', 'Account', 'Model', 'Result')))
    return "\n".join(out) + "\n"


def cmd_sessions(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
