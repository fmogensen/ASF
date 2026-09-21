"""asf.views.roadmap — the ``ROADMAP`` table (``asf roadmap``): one row per Epic.

Ported from the operator's Epic-table script's index-backed path (its pre-index ``goals.txt``
path is not ported: the index has carried every Epic since the record model landed, so there is
no scan fallback to keep).
"""
from asf.views import index_reader as ix
from asf.views.pr_annotate import annotator, pr_states, table

NEXT_FEATURES = 4   # Features named in an Epic's NEXT cell; the rest are counted
PRS_PER_FEATURE = 3


def is_next(f):
    return f['state'] == 'Active' or f.get('stage') == 'plan-approved'


def is_on_prod(f):
    return f['state'] == 'Closed' or f.get('stage') == 'on-prod'


def _cut(text, width=220):
    text = ' '.join(text.split())
    return text if len(text) <= width else text[:width - 1].rstrip() + '…'


def render(root, product=None):
    items, generated = ix.load(root)
    epics = sorted(ix.of_type(items, 'epic'), key=lambda e: (ix.rank(e), e['id']))

    named_prs = set()
    for e in epics:
        for f in [f for f in ix.children(items, e, 'feature') if is_next(f) and not is_on_prod(f)][:NEXT_FEATURES]:
            named_prs.update(ix.prs_of(items, f)[:PRS_PER_FEATURE])
    states = pr_states(product) if (product and named_prs) else {}
    annotate, tally, named = annotator(states)

    rows = []
    for e in epics:
        feats = sorted(ix.children(items, e, 'feature'), key=lambda f: (ix.rank(f), f['id']))
        prod = [f for f in feats if is_on_prod(f)]
        nxt = [f for f in feats if is_next(f) and not is_on_prod(f)]
        parts = []
        for f in nxt[:NEXT_FEATURES]:
            prs = ix.prs_of(items, f)
            cell = f"{f['id']} {f['title']} ({f.get('stage') or f['state']})"
            if prs:
                cell += " " + " ".join(f"#{n}" for n in prs[:PRS_PER_FEATURE])
                if len(prs) > PRS_PER_FEATURE:
                    cell += f" +{len(prs) - PRS_PER_FEATURE}"
            parts.append(annotate(cell))
        if len(nxt) > NEXT_FEATURES:
            parts.append(f"+{len(nxt) - NEXT_FEATURES} more")
        prod_cell = _cut(f"{len(prod)}: " + "; ".join(f"{f['id']} {f['title']}" for f in prod)) if prod else ""
        spend = ix.usd(ix.subtree(items, e))
        budget = e.get('budget_usd')
        spend_cell = '' if spend is None and budget is None else \
            f"{ix.money(spend)} / {ix.money(budget) if isinstance(budget, (int, float)) else '—'}"
        rows.append([
            str(e['rank']) if isinstance(e.get('rank'), int) else '',
            f"{e['id']} {e['title']}" + (f" ({e['legacy_id']})" if e.get('legacy_id') else ''),
            e['state'] + (' · blocked' if e.get('blocked') else ''),
            prod_cell,
            "; ".join(parts),
            _cut("; ".join(annotate(b) for b in e.get('blocked_by_open') or [])),
            spend_cell,
        ])

    out = [f"**ROADMAP** (index.json generated {ix.local_stamp(generated)})", ""]
    out.append(table(rows, ["#", "Epic", "State", "On prod", "Next", "Blocked", "Spend / budget"]))
    out.append("")
    out.append(f"{len(named)} PRs named: {tally['merged']} merged · {tally['in CI']} in CI · {tally['open']} open")
    return "\n".join(out) + "\n"


def cmd_roadmap(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
