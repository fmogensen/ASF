"""asf.feeder.render — the NEXT rows table, the incident clock, and ``asf next``.

``incidents()`` is data only (the status view prints ``INCIDENTS`` first from it); ``table()``
prints the rows table with the columns tier · row · item · feature · action.
"""
import collections
import json

from asf import env
from asf.feeder import rows as R
from asf.views import index_reader as ix

S1_HOURS = 2

Incident = collections.namedtuple('Incident', 'bug_id severity age session starved')


def _fmt_age(seconds):
    s = max(int(seconds), 0)
    return f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def s1_hours(product):
    """``stage_limits.s1_hours`` (default 2): how long an open S1 may go without a session."""
    v = (product.stage_limits or {}).get('s1_hours') if product is not None else None
    return v if isinstance(v, (int, float)) and v > 0 else S1_HOURS


def incidents(index, inflight, now, product=None):
    """[Incident] — every open S1/S2 Bug, S1 first, oldest first.

    ``starved`` is the S1 clock: open longer than ``s1_hours`` and no session holds it. Past ticks
    are not observable here, so "had a free slot at some tick" is read as "is not in flight now".
    """
    items = R.items_of(index)
    limit = s1_hours(product) * 3600
    out = []
    for b in ix.of_type(items, 'bug'):
        sev = b.get('severity')
        if sev not in ('S1', 'S2') or not R.is_open(b):
            continue
        since = ix.parse_ts(b.get('stage_since') or b.get('created') or '')
        age_s = (now - since).total_seconds() if since else 0
        s = R.session_of(inflight, b['id'])
        session = (s.get('account') or s.get('kind') or 'session') if s else 'no session'
        starved = sev == 'S1' and s is None and age_s > limit
        out.append((sev, -age_s, b['id'],
                    Incident(b['id'], sev, _fmt_age(age_s) if since else '—', session, starved)))
    return [i for *_k, i in sorted(out)]


def _cell(v):
    return (v or '—').replace('|', '/')


def action_cell(row):
    if row.launches:
        return f"{R.LAUNCH} {row.brief_kind} on {row.branch}"
    return row.action


def table(rows, header=None):
    """The rows table — markdown, like every other ``asf`` view."""
    out = [header or f"**NEXT** — {len(rows)} rows · "
                     f"{sum(1 for r in rows if r.launches)} would launch"]
    out.append('')
    out.append('| Tier | Row | Item | Feature | Action |')
    out.append('|---|---|---|---|---|')
    for r in rows:
        out.append('| ' + ' | '.join(_cell(c) for c in
                                     (str(r.tier), r.kind, r.item_id, r.feature_id, action_cell(r))) + ' |')
    if not rows:
        out.append('| — | nothing to start | — | — | — |')
    return '\n'.join(out) + '\n'


def rows_json(rows):
    return json.dumps([dict(r.__dict__) for r in rows], indent=2, ensure_ascii=False) + '\n'


# ---- asf next -----------------------------------------------------------------

def load_inflight(path):
    """The ``--inflight`` file: a JSON list of sessions, or ``{"inflight": [...]}``."""
    if not path:
        return []
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return data.get('inflight', []) if isinstance(data, dict) else data


def cmd_next(args, root=None):
    product = env.load_product(getattr(args, 'product', None))
    root = root or product.backlog_dir
    items, _generated = ix.load(root)
    inflight = load_inflight(getattr(args, 'inflight', None))
    capacity = args.capacity if args.capacity is not None else _default_capacity(product)
    # the tick's own ledger reads (step_wave.run): a branch awaiting harvest is busy in both views
    from asf.tick import step_wave
    rows = R.plan_rows(items, product, inflight, capacity, attempts=step_wave.attempts(product),
                       corrections=step_wave.corrections(product),
                       busy=step_wave.awaiting_harvest(product))
    if getattr(args, 'json', False):
        print(rows_json(rows), end='')
    else:
        print(table(rows), end='')
    return 0


def _default_capacity(product):
    """This product's session ceiling — ``asf.capacity.resolve`` (PD2)."""
    from asf import capacity as capacity_mod
    return capacity_mod.resolve(product).sessions


def register(sub):
    """``asf next --product <p> [--capacity n] [--inflight <json>] [--json]``."""
    p = sub.add_parser('next', help='the NEXT table: what the tick would start, S1 first')
    env.add_product_arg(p)
    p.add_argument('--capacity', type=int, default=None, help='session slots (default: asf.capacity.resolve)')
    p.add_argument('--inflight', default=None, help='JSON file: the running sessions [{item, kind, account, age}]')
    p.add_argument('--json', action='store_true', help='print the rows as JSON')
    p.set_defaults(func=cmd_next)
    return p

