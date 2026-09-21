"""asf.views.parity — the ``PARITY`` table (``asf parity``): one row per Story.

Ported from the operator's Story-parity script's index-backed path.
"""
import collections
import datetime
import re
import sys

from asf.views import index_reader as ix

STATE_STATUS = {"Closed": "done", "Resolved": "doing", "Active": "doing", "New": "todo"}


def title(row):
    return re.sub(r'\*\*|`', '', row['cap']).split(' — ')[0].split(': ')[0].strip().rstrip('…').strip()


def story_rows(items):
    rows = []
    for s in ix.of_type(items, 'story'):
        feat = items.get(s.get('parent')) or {}
        epic = ix.epic_of(items, feat) if feat else None
        st = STATE_STATUS.get(s['state'])
        ftitle = re.sub(r'\*\*|`', '', feat.get('title', '')).strip()
        ftitle = (ftitle[:57] + '…') if len(ftitle) > 58 else ftitle
        rows.append(dict(
            id=s.get('legacy_id') or s['id'], area=s.get('area') or '(no area)', cap=s['title'],
            status=st or 'todo', broken=st is None, when=f"{ftitle} ({feat['id']})" if feat else '(no Feature)',
            order=(ix.rank(epic) if epic else ix.BIG, ix.rank(feat) if feat else ix.BIG, ftitle, s['id'])))
    return rows


def area_key(a):
    m = re.match(r'(\d+(?:\.\d+)*)', a)
    return (tuple(int(x) for x in m.group(1).split('.')), a) if m else ((10 ** 6,), a)


def render(root, full=False):
    items, generated = ix.load(root)
    rows = story_rows(items)
    broken = [r for r in rows if r['broken']]
    n = collections.Counter(r['status'] for r in rows)
    not_done = [r for r in rows if r['status'] != 'done']
    groups = collections.OrderedDict()
    for r in sorted(not_done, key=lambda r: r['order']):
        groups.setdefault(r['when'], []).append(r)

    now = datetime.datetime.now().strftime('%H:%M')
    out = [f"**PARITY {now}** — {n['done']} of {len(rows)} Stories done; {n['doing']} doing, {n['todo']} todo, "
           f"across {len(groups)} Features still landing. Needs you: 0. Source: `index.json` "
           f"(generated {ix.local_stamp(generated)}); done = the Story is Closed (impl + tests cited on disk), "
           f"not tests green."]
    out.append("")
    out.append("**BY AREA**")
    out.append("")
    out.append("| Area | Done | Doing | Todo | Rows |")
    out.append("|---|---:|---:|---:|---:|")
    by = collections.OrderedDict()
    for r in sorted(rows, key=lambda r: area_key(r['area'])):
        by.setdefault(r['area'], collections.Counter())[r['status']] += 1
    for a, d in by.items():
        out.append(f"| {a} | {d['done']} | {d['doing']} | {d['todo']} | {sum(d.values())} |")
    out.append(f"| **All** | **{n['done']}** | **{n['doing']}** | **{n['todo']}** | **{len(rows)}** |")
    out.append("")
    out.append(f"**NOT DONE — {len(not_done)} rows, by when**" + ("" if full else " (`*` = doing)"))
    out.append("")
    if full:
        out.append("| When | Rows | Id | State | What |")
        out.append("|---|---:|---|---|---|")
        for w, rs in groups.items():
            for i, r in enumerate(rs):
                cap = title(r)
                cap = (cap[:72] + '…') if len(cap) > 73 else cap
                out.append(f"| {w if i == 0 else ''} | {len(rs) if i == 0 else ''} | {r['id']} | {r['status']} | {cap} |")
    else:
        out.append("| When | Rows | Ids |")
        out.append("|---|---:|---|")
        for w, rs in groups.items():
            ids = ", ".join(r['id'] + ('*' if r['status'] == 'doing' else '') for r in rs)
            out.append(f"| {w} | {len(rs)} | {ids} |")
    if broken:
        out.append("")
        out.append(f"**BROKEN ROWS** ({len(broken)}) — the state is not New/Active/Resolved/Closed; "
                    f"counted as todo: " + ", ".join(r['id'] for r in broken))
    return "\n".join(out) + "\n"


def cmd_parity(args, root):
    full = 'full' in sys.argv[1:]
    print(render(root, full=full), end='')
    return 0
