"""asf.groom.delivery — F-0102: the selection rule for a **Delivery** — several small, decided
items that share a footprint or an area, carried by one plan document, one branch, one coder
session, one gate and one landing.

Pure functions over `canonical` (the record's parsed cards) and the conventions. It imports
`asf.feeder.footprint` and `asf.groom.shape` — never the other way round.
"""
import re
from collections import namedtuple

from asf.feeder import footprint
from asf.groom import shape

#: D8: the allowance an estimate makes for one glob of surface, beyond the member's own text.
TOKENS_PER_GLOB = 2000
CHARS_PER_TOKEN = 4

Member = namedtuple('Member', 'id type title writes rank tokens')

_HEADING_RE = re.compile(r'(?m)^## (.+?)\s*$')


def _writes(rec):
    w = rec['meta'].get('writes') or []
    return [w] if isinstance(w, str) else list(w)


def _state(rec):
    return rec['meta'].get('state') or 'New'


def _has_child(canonical, item_id):
    return any(r['meta'].get('parent') == item_id for r in canonical.values())


def _has_open_blocker(rec, canonical):
    bb = rec['meta'].get('blockedBy')
    bb = [bb] if isinstance(bb, str) else list(bb or [])
    for b in bb:
        if not isinstance(b, str) or not b.strip():
            continue
        other = canonical.get(b)
        if other is None or _state(other) != 'Closed':
            return True
    return False


def _past_spec_approved(stage):
    stage = stage or 'card'
    return stage == 'spec-approved' or stage.startswith('plan') or stage.startswith('building')


def is_small(rec, canonical, batch_max_globs):
    """D5: `size: small` on the card wins; else a Bug is small at severity S2/S3 (never S1);
    else a Feature or a Story is small when it has no child in `canonical` and
    `len(writes) <= batch_max_globs`."""
    meta = rec['meta']
    if meta.get('size') == 'small':
        return True
    if meta.get('type') == 'bug':
        return meta.get('severity') in ('S2', 'S3')
    if meta.get('type') in ('feature', 'story'):
        if _has_child(canonical, meta.get('id')):
            return False
        return len(_writes(rec)) <= batch_max_globs
    return False


def deliverable_items(canonical, batch_max_globs):
    """Every decided, open, unblocked small `feature`/`story`/`bug` with a `writes:` footprint
    and no delivery of its own already — `[Member]`, sorted `(rank, id)`."""
    out = []
    for iid, rec in canonical.items():
        meta = rec['meta']
        type_ = meta.get('type')
        if type_ not in ('feature', 'story', 'bug'):
            continue
        if meta.get('removed'):
            continue
        if meta.get('decided') is not True:
            continue
        if _state(rec) != 'New':
            continue
        if meta.get('delivers') or meta.get('delivered_by'):
            continue
        if meta.get('after'):
            continue
        if _has_open_blocker(rec, canonical):
            continue
        writes = _writes(rec)
        if not writes:
            continue
        if not is_small(rec, canonical, batch_max_globs):
            continue
        if type_ == 'feature':
            if (meta.get('stage') or 'card') != 'card':
                continue
        elif type_ == 'story':
            feat = canonical.get(meta.get('parent'))
            if feat is not None and _past_spec_approved(feat['meta'].get('stage')):
                continue
        out.append(Member(iid, type_, meta.get('title', ''), writes, shape._rank(rec),
                           estimate_tokens(rec, writes)))
    return sorted(out, key=lambda m: (m.rank, m.id))


def _section_text(body, name):
    body = body or ''
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() == name.lower():
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return body[start:end]
    return ''


def estimate_tokens(rec, writes):
    """D8: the card's Description, Acceptance and Fix text, divided by `CHARS_PER_TOKEN`. The
    glob allowance is not added here — `budget_cut` adds `TOKENS_PER_GLOB` per union glob once,
    over the whole group."""
    body = rec.get('body', '')
    text = (_section_text(body, 'Description') + _section_text(body, 'Acceptance')
            + _section_text(body, 'Fix'))
    return len(text) // CHARS_PER_TOKEN


def group(members, area_depth):
    """Connected components (size >= 2) of the graph where an edge is an overlapping `writes:`
    or a shared area at `area_depth`; each component in `(rank, id)` order."""
    members = sorted(members, key=lambda m: (m.rank, m.id))
    seen = set()
    out = []
    for start in members:
        if start.id in seen:
            continue
        comp, stack = [], [start]
        seen.add(start.id)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for other in members:
                if other.id in seen:
                    continue
                if (footprint.overlaps(cur.writes, other.writes)
                        or set(shape.areas(cur.writes, area_depth))
                        & set(shape.areas(other.writes, area_depth))):
                    seen.add(other.id)
                    stack.append(other)
        if len(comp) >= 2:
            out.append(sorted(comp, key=lambda m: (m.rank, m.id)))
    return out


def budget_cut(component, max_items, max_globs, max_tokens):
    """Walk the component in `(rank, id)` order, keeping a member while all three ceilings
    hold; a member that would breach one is skipped, not stopped on. Fewer than two members
    fit: `None`. `max_items <= 0`: `None`, always (D9)."""
    if max_items <= 0:
        return None
    ordered = sorted(component, key=lambda m: (m.rank, m.id))
    kept, union, tokens = [], [], 0
    for m in ordered:
        if len(kept) >= max_items:
            continue
        new_union = list(union)
        for g in m.writes:
            if g not in new_union:
                new_union.append(g)
        new_tokens = tokens + m.tokens
        if len(new_union) > max_globs or new_tokens + TOKENS_PER_GLOB * len(new_union) > max_tokens:
            continue
        kept.append(m)
        union, tokens = new_union, new_tokens
    return kept if len(kept) >= 2 else None


def delivery_proposals(canonical, conv, declined=None):
    """`[shape.Proposal('deliver', ids, (), why)]` — one per budget-cut component, dropping any
    whose `shape.proposal_key('deliver', ids)` a named card already declined."""
    members = deliverable_items(canonical, conv.batch_max_globs)
    out = []
    for comp in group(members, conv.area_depth):
        cut = budget_cut(comp, conv.delivery_max_items, conv.delivery_max_globs,
                          conv.delivery_max_tokens)
        if not cut:
            continue
        ids = tuple(m.id for m in cut)
        key = shape.proposal_key('deliver', ids)
        if any(key in (declined or {}).get(i, ()) for i in ids):
            continue
        union = []
        for m in cut:
            for g in m.writes:
                if g not in union:
                    union.append(g)
        pairs = [(cut[i], cut[j]) for i in range(len(cut)) for j in range(i + 1, len(cut))]
        overlap_n = sum(1 for a, b in pairs if footprint.overlaps(a.writes, b.writes))
        area_n = sum(1 for a, b in pairs
                     if not footprint.overlaps(a.writes, b.writes)
                     and set(shape.areas(a.writes, conv.area_depth))
                     & set(shape.areas(b.writes, conv.area_depth)))
        area_names = sorted({a for m in cut for a in shape.areas(m.writes, conv.area_depth)})
        tokens = sum(m.tokens for m in cut) + TOKENS_PER_GLOB * len(union)
        why = ('%d small items in %s (%d overlap, %d share the area); %d globs, ~%dk tokens: '
               'one plan and one coder instead of %d specs, %d plans and %d coders'
               % (len(cut), ', '.join(area_names), overlap_n, area_n, len(union), tokens // 1000,
                  len(cut), len(cut), len(cut)))
        out.append(shape.Proposal('deliver', ids, (), why))
    return out


def deliveries(canonical):
    """`{lead id: [member ids]}` from every card's `delivers:` — the lead is `delivers[0]`."""
    return {iid: list(rec['meta'].get('delivers'))
            for iid, rec in canonical.items() if rec['meta'].get('delivers')}


def releasable(canonical):
    """D11/D12: `[(lead, member)]` where the lead carries `delivers:`, at least one member has
    landed (`Resolved`/`Closed`), and this member is still open with machine `state: New` and no
    commit in its `evidence`. When the *lead* is the one still open and uncommitted, every other
    still-open member comes back with it — the delivery is over."""
    out = []
    for lead, ids in deliveries(canonical).items():
        recs = {i: canonical[i] for i in ids if i in canonical}
        if not any(_state(r) in ('Resolved', 'Closed') for r in recs.values()):
            continue
        lead_rec = recs.get(lead)
        lead_uncommitted = (lead_rec is not None and _state(lead_rec) == 'New'
                             and not lead_rec['meta'].get('evidence'))
        if lead_uncommitted:
            for i in ids:
                if i == lead:
                    continue
                r = recs.get(i)
                if r is not None and _state(r) == 'New' and not r['meta'].get('evidence'):
                    out.append((lead, i))
            continue
        for i in ids:
            if i == lead:
                continue
            r = recs.get(i)
            if r is None:
                continue
            if _state(r) == 'New' and not r['meta'].get('evidence'):
                out.append((lead, i))
    return out
