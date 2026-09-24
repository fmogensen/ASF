"""asf.views.sessions — the ``SESSIONS`` table (``asf sessions``).

Four groups:

* **Working** — a session in the workers' registry (``~/.ASF/state/<product>/sessions.jsonl``) with
  no ``ended`` whose pid is still alive;
* **Dead** — the same, but its pid is gone (the tick's ``health`` step will reconcile it);
* **Other** — an observed session (:mod:`asf.workers.observe`) on one of the pool's accounts that
  is not this product's own — another product's, or a foreign one (F-0076 S-8156);
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


def live_groups(product, alive=None, session_source=None, result=None):
    """(working, finished, dead): the registry's sessions with no ``ended``, split on whether the
    pid lives and, when it does not, on whether the run left a success result. A pid that exited
    after a success result is **finished** (awaiting health and harvest), not dead
    (:func:`asf.workers.lifecycle.finished_unrecorded`); only a pid gone without one is dead.
    Neither holds a seat. ``result``: ``callable(run) -> result line`` (a test's)."""
    if product is None:
        return [], [], []
    from asf.workers import health as health_mod
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    live = pool_mod.live_sessions(product)
    if alive is None:
        alive = health_mod.alive_for(product, live, session_source)
    working, finished, dead = [], [], []
    for s in live:
        if alive(s.get('pid')):
            working.append(s)
        elif lifecycle.finished_unrecorded(s, alive, result):
            finished.append(s)
        else:
            dead.append(s)
    return working, finished, dead


def live_rows(product, alive=None, session_source=None, result=None):
    """(working, dead) — :func:`live_groups` without the finished runs, which are neither."""
    working, _finished, dead = live_groups(product, alive, session_source, result)
    return working, dead


LIVE_COLUMNS = ('job', 'item', 'kind', 'account', 'model', 'branch', 'started')


def _table(rows, columns, header):
    out = [f"| {' | '.join(header)} |", '|' + '---|' * len(header)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(k, '—') or '—') for k in columns) + " |")
    return out


OTHER_COLUMNS = ('Session', 'Account', 'Owner', 'Pid')


def _other_rows(observed, product):
    """The observed sessions on a pool account that are not ``product``'s own — another
    product's, or a foreign one (F-0076 D8/S-8156)."""
    name = getattr(product, 'name', None)
    return [o for o in observed if o.account is not None and (o.owner == 'foreign' or o.product != name)]


def _other_table(rows):
    out = [f"| {' | '.join(OTHER_COLUMNS)} |", '|' + '---|' * len(OTHER_COLUMNS)]
    for o in rows:
        cells = (o.session or '—', o.account or '—', o.owner or '—', o.pid)
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    return out


def render(root, product=None, alive=None, cfg=None, session_source=None):
    """``cfg`` (default: :func:`asf.workers.spawn.load_cfg`) drives one observation read, used
    both for the **Other** group and, when ``alive`` is not given, for the working/dead split
    (F-0076 D11: a run is alive only while its own session still holds its pid). An explicit
    ``alive`` (a test's, or the pid rule) is used as given."""
    from asf.workers import spawn as spawn_mod, pool as pool_mod, observe
    if cfg is None:
        cfg = spawn_mod.load_cfg()
    accounts = pool_mod.accounts_from_config(cfg)
    observed, why = observe.read(cfg, accounts, source=session_source)
    if alive is not None:
        effective_alive = alive
    elif why:
        effective_alive = pid_alive
    else:
        runs = list(pool_mod.load_sessions(product).values()) if product is not None else []
        effective_alive = observe.identity_alive(observed, runs)

    ended = _ended_rows(root)
    working, finished, dead = live_groups(product, effective_alive)
    other = [] if why else _other_rows(observed, product)
    other_label = f'unreadable ({why})' if why else f'{len(other)} other'
    out = [f"**SESSIONS** — {len(working)} working · "
           + (f"{len(finished)} finished (awaiting harvest) · " if finished else "")
           + f"{len(dead)} dead · {len(ended)} ended · {other_label}", ""]
    header = ('Job', 'Item', 'Kind', 'Account', 'Model', 'Branch', 'Started')
    groups = (('Working', working),) + ((('Finished', finished),) if finished else ()) \
        + (('Dead', dead),)
    for name, rows in groups:
        if not rows:
            out.append(f"{name}: none")
            out.append("")
            continue
        out.append(f"**{name}**")
        out.append("")
        out.extend(_table(rows, LIVE_COLUMNS, header))
        out.append("")
    if why:
        out.append(f"Other: unreadable ({why})")
        out.append("")
    elif not other:
        out.append("Other: none")
        out.append("")
    else:
        out.append("**Other**")
        out.append("")
        out.extend(_other_table(other))
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
