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


def feature_status(f):
    """A Feature's bucket for the default collapsing (B-0088): ``'done'`` (``state: Closed`` or
    stage ``on-prod`` — shipped or dead, nothing to act on), ``'undecided'`` (groom has not set
    ``decided: true`` on it yet — :func:`asf.groom.policy._undecided`'s rule, read again here
    since this module stays index-only), else ``'moving'`` (decided and still in the pipeline,
    ``landed`` included — the stage right before ``on-prod``).

    ``'done'`` is tested first, independent of ``decided``: a Feature that shipped or was closed
    before groom ever marked it needs no look either, and reading it as ``'undecided'`` would put
    it in the one summary line an operator does still scan. On the live record three Features
    (``F-0009``, ``F-0011``, ``F-0012``: ``Closed``, ``landed``, ``decided: false``) are exactly
    that shape."""
    word = (f.get('stage') or '').split(' ')[0]
    if f.get('state') == 'Closed' or word == 'on-prod':
        return 'done'
    return 'moving' if f.get('decided') is True else 'undecided'


def render(root, product=None, epic=None, show_all=False):
    """``epic`` (an Epic id, case-insensitive), when given, narrows the output to that one
    epic's group, on top of whatever ``show_all`` already chose — it never widens it.

    By default (``show_all=False``) the board collapses to what still needs a look: full rows,
    grouped per Epic as below, for Features :func:`feature_status` calls ``'moving'`` (decided,
    still in the pipeline — ``landed`` included); the ``'undecided'`` and ``'done'`` (Closed or
    on-prod) Features — most of a mature backlog — fold into one summary line each instead of a
    full row apiece. ``show_all=True`` (``--all``) is the opt-out: every Feature gets a full row,
    the board as it was before this default (339 lines / 67 KB on the real record, B-0088) — a
    console that caps its own output (the operator's terminal, an agent's tool result) cuts it
    off before a Feature anyone is actually looking for ever appears."""
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
        feat_epic = ix.epic_of(items, f)
        rows.append((feat_epic['id'] if feat_epic else None, f, cells))

    epics = sorted(ix.of_type(items, 'epic'), key=lambda e: (ix.rank(e), e['id']))
    n_spec, n_plan = tot['spec'], tot['plan']
    out = [f"**BOARD {time.strftime('%H:%M')}** — {len(feats)} Features · "
           f"specs: {n_spec['approved']} approved / {n_spec['review']} in review / "
           f"{n_spec['draft'] + n_spec['linked']} draft · "
           f"plans: {n_plan['approved']} approved / {n_plan['review']} in review / "
           f"{n_plan['draft'] + n_plan['linked']} draft · "
           f"Tasks: {task_done} Closed / {task_n} · index.json generated {ix.local_stamp(generated)}"]
    groups = [(epic_title(e), e['id']) for e in epics] + [("No Epic (needs a parent Epic)", None)]
    scope = rows
    if epic is not None:
        groups = [(title, eid) for title, eid in groups if eid and eid.lower() == epic.lower()]
        scope = [r for r in rows if r[0] and r[0].lower() == epic.lower()]
    if not show_all:
        for bucket, label in (('undecided', 'Undecided'), ('done', 'Closed / on-prod')):
            n = sum(1 for _e, f, _c in scope if feature_status(f) == bucket)
            if n:
                out.append("")
                out.append(f"**{label}** — {n} Feature{'' if n == 1 else 's'} "
                            f"(`--all` to list)")
    for title, eid in groups:
        group = sorted(((f, cells) for e, f, cells in rows if e == eid
                         and (show_all or feature_status(f) == 'moving')),
                        key=lambda r: (ix.rank(r[0]), r[0]['id']))
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
    print(render(root, product, epic=getattr(args, 'epic', None),
                 show_all=getattr(args, 'all', False)), end='')
    return 0
