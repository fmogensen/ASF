"""asf.views.index_reader — the one reader of a backlog's ``index.json`` for every ``asf`` table.

Ported from the operator's ``asf_index.py`` (a script-local helper the pre-``asf`` tools shared),
generalized to take a backlog root instead of a hardcoded path: every ``asf views`` command reads
through here so ``asf tick --shadow`` and a plain ``asf roadmap`` see the same data shape whether
the root is a product's real backlog or its shadow clone.

Read-only, stdlib only. ``load()`` drops typed-``removed`` items: the index carries every card,
the tables only want the live set.
"""
import datetime as dt
import json
import os

BIG = 10 ** 6


def load(root):
    """({id: item}, generated) from ``<root>/index.json``."""
    path = os.path.join(root, 'index.json')
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    items = {k: v for k, v in raw['items'].items() if not v.get('removed')}
    return items, raw.get('generated', '')


def rank(item):
    """The typed order within the parent; an unranked item sorts after every ranked one."""
    r = item.get('rank')
    return r if isinstance(r, int) else BIG


def of_type(items, kind):
    return [v for v in items.values() if v['type'] == kind]


def children(items, item, kind=None):
    out = [items[c] for c in item.get('children', []) if c in items]
    return [c for c in out if kind is None or c['type'] == kind]


def subtree(items, item):
    """The item and everything beneath it, once each."""
    seen, stack, out = set(), [item['id']], []
    while stack:
        i = stack.pop()
        if i in seen or i not in items:
            continue
        seen.add(i)
        out.append(items[i])
        stack.extend(items[i].get('children', []))
    return out


def feature_tasks(items, feature):
    """Tasks of a Feature: its own, and the ones hung on its Stories."""
    return [t for t in subtree(items, feature) if t['type'] == 'task']


def epic_of(items, item):
    """The Epic above an item, or None (a parentless Feature)."""
    seen = set()
    while item and item['id'] not in seen:
        seen.add(item['id'])
        if item['type'] == 'epic':
            return item
        item = items.get(item.get('parent'))
    return None


def parse_ts(ts):
    try:
        return dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None


def local_stamp(ts, fmt='%H:%M %d %b'):
    t = parse_ts(ts)
    return t.astimezone().strftime(fmt) if t else '?'


def span(seconds):
    """A duration in the tables' buckets: 25m, 5h, 3d. Negative is clamped to 0."""
    s = max(seconds, 0)
    return f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 172800 else f"{int(s // 86400)}d"


def age(ts):
    """``stage_since`` as a short age: 25m, 5h, 3d."""
    t = parse_ts(ts)
    return span((dt.datetime.now(dt.timezone.utc) - t).total_seconds()) if t else '—'


def usd(items):
    """Sum of cost.usd over the given items, or None when none of them has a measured figure."""
    vals = [v['cost']['usd'] for v in items
            if isinstance(v.get('cost'), dict) and isinstance(v['cost'].get('usd'), (int, float))]
    return sum(vals) if vals else None


def money(x):
    return '—' if x is None else f"${x:,.2f}"


def prs_of(items, item):
    """PR numbers an item names, its Tasks' included, in first-seen order."""
    out = []
    for v in ([item] + feature_tasks(items, item) if item['type'] == 'feature' else [item]):
        for n in (v.get('links') or {}).get('prs') or []:
            if n not in out:
                out.append(n)
    return out
