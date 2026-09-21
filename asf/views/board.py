"""asf.views.board — the ``BOARD`` table (``asf backlog``): one row per Feature, grouped by Epic.

Ported from the operator's Feature-table script's index-backed path.
"""
import collections

from asf.views import index_reader as ix

STAGE_ORDER = ["card", "spec-draft", "spec-review", "spec-approved", "plan-draft", "plan-review",
               "plan-approved", "building", "landed", "on-prod"]
MAX_PRS = 4


def stage_idx(f):
    word = (f.get('stage') or '').split(' ')[0]
    return STAGE_ORDER.index(word) if word in STAGE_ORDER else -1


def doc_cell(f, kind):
    if not (f.get('links') or {}).get(kind):
        return '—'
    i, rnd = stage_idx(f), (f.get('stage') or '').partition(' ')[2]
    first_approved = STAGE_ORDER.index(f"{kind}-approved")
    if i < 0:
        word = 'linked'
    elif i >= first_approved:
        word = 'approved'
    elif i == first_approved - 1:
        word = 'review' + (' ' + rnd if rnd else '')
    else:
        word = 'draft'
    return word + (' · on main' if any(e.startswith(f"{kind} on origin/main") for e in f.get('evidence') or []) else '')


def epic_title(e):
    legacy = e.get('legacy_id') or ''
    return f"{legacy.replace('GOAL', 'Epic')} — {e['title']} ({e['id']})" if legacy.startswith('GOAL') \
        else f"{e['id']} — {e['title']}"


def render(root, product=None):
    import time
    items, generated = ix.load(root)
    feats = ix.of_type(items, 'feature')
    rows = []
    tot = {'spec': collections.Counter(), 'plan': collections.Counter()}
    task_n = task_done = 0
    for f in feats:
        tasks = ix.feature_tasks(items, f)
        done = sum(1 for t in tasks if t['state'] == 'Closed')
        task_n, task_done = task_n + len(tasks), task_done + done
        prs = ix.prs_of(items, f)
        prs_cell = " ".join(f"#{n}" for n in prs[:MAX_PRS]) + (f" +{len(prs) - MAX_PRS}" if len(prs) > MAX_PRS else "")
        spec, plan = doc_cell(f, 'spec'), doc_cell(f, 'plan')
        for kind, val in (('spec', spec), ('plan', plan)):
            if val != '—':
                tot[kind][val.split(' ')[0]] += 1
        name = f['title'] + (f" ({f['legacy_id']})" if f.get('legacy_id') else '')
        cells = [f"{f['id']} {name}", f.get('stage') or f['state'], spec, plan,
                 f"{done} Closed / {len(tasks)}" if tasks else '—', prs_cell or '—',
                 "; ".join(f.get('blocked_by_open') or []) or '—', ix.age(f.get('stage_since')),
                 ix.money(ix.usd(ix.subtree(items, f)))]
        epic = ix.epic_of(items, f)
        rows.append((epic['id'] if epic else None, f, cells))

    epics = sorted(ix.of_type(items, 'epic'), key=lambda e: (ix.rank(e), e['id']))
    n_spec, n_plan = tot['spec'], tot['plan']
    out = [f"**BOARD {time.strftime('%H:%M')}** — {len(feats)} Features · "
           f"specs: {n_spec['approved']} approved / {n_spec['review']} in review / "
           f"{n_spec['draft'] + n_spec['linked']} draft · "
           f"plans: {n_plan['approved']} approved / {n_plan['review']} in review / "
           f"{n_plan['draft'] + n_plan['linked']} draft · "
           f"Tasks: {task_done} Closed / {task_n} · index.json generated {ix.local_stamp(generated)}"]
    groups = [(epic_title(e), e['id']) for e in epics] + [("No Epic (needs a parent Epic)", None)]
    for title, eid in groups:
        group = sorted(((f, cells) for e, f, cells in rows if e == eid), key=lambda r: (ix.rank(r[0]), r[0]['id']))
        if not group:
            continue
        out.append("")
        out.append(f"**{title}**")
        out.append("")
        out.append("| Feature | Stage | Spec | Plan | Tasks | PRs | Blocked on | Age | Cost |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for _f, cells in group:
            out.append("| " + " | ".join(c.replace('|', '/') for c in cells) + " |")
    return "\n".join(out) + "\n"


def cmd_backlog(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
