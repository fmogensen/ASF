"""asf.groom.shape — the shape rules: derive(card, canonical, default_bug_parent) decides an
inbox card's type, or asks the one Question that would settle it. Pure over the parsed card and
the record; the only place a type is chosen.

Also the delivery side (F-0086): ready_tasks reads which Tasks could launch now, and
merge_proposals / batch_proposals / split_proposals say how to cut them so the lane runs faster.
The overlap test is asf.feeder.footprint's own, never a second one."""
import re
from collections import namedtuple

from asf.feeder import footprint
from asf.record import frontmatter
from asf.record.core import PARENT_TYPES, is_open, tokenize

Shape = namedtuple('Shape', 'type rule parent')
Question = namedtuple('Question', 'text')
Card = namedtuple('Card', 'title headers description features acceptance assumptions')

SIZE = {  # the card's definitions, in one place; the History line and asf check quote them
    'epic': 'a business outcome spanning several Features',
    'feature': 'one spec and one plan, landing as one deployable thing',
    'story': 'one PR with one acceptance list',
    'task': 'one session in one worktree with a writes: footprint',
    'bug': 'a defect with a signature',
}

RULES = ('signature', 'writes', 'features-list', 'parent-feature', 'default')

SHAPE_LINE_RE = re.compile(r'created \(([^)]*)\) — shape: (\S+) → (\w+)')

DEFECT_WORDS_RE = re.compile(r'\b(broken|red|fails?|failing)\b', re.IGNORECASE)


def infer_parent_epic(canonical, tokens):
    """The open Epic sharing the most title words with `tokens`, or None."""
    best_id, best_n = None, 0
    for iid, rec in sorted(canonical.items()):
        if rec['meta'].get('type') != 'epic' or not is_open(rec):
            continue
        shared = len(tokens & tokenize(rec['meta'].get('title', '')))
        if shared > best_n:
            best_id, best_n = iid, shared
    return best_id


def _typed(card, shape):
    """D4: a `type:` line that disagrees with the reading turns a Shape into a Question."""
    t = card.headers.get('type')
    if t and t.lower() != shape.type:
        return Question(
            f"`type: {t}` is not read — the card's shape reads as {shape.type} "
            f"({shape.rule}: {SIZE[shape.type]}). Remove the line, or change the shape."
        )
    return shape


def derive(card, canonical, default_bug_parent=None):
    headers = card.headers
    signature = headers.get('signature')
    writes = headers.get('writes')
    parent = headers.get('parent')

    if signature and writes:
        return Question(
            'A card is one thing: signature: makes it a Bug, writes: makes it a Task — drop one.'
        )

    if parent and parent not in canonical:
        return Question(f"`parent: {parent}` does not exist — name an existing item or remove the line.")

    if signature:
        if parent:
            ptype = canonical[parent]['meta'].get('type')
            if ptype not in PARENT_TYPES['bug']:
                return Question(
                    f"`parent: {parent}` is a {ptype} — a Bug hangs under an Epic, a Feature or a Story."
                )
            bug_parent = parent
        elif default_bug_parent:
            bug_parent = default_bug_parent
        else:
            return Question('Which item is this Bug under? Add a `parent: <id>` line.')
        return _typed(card, Shape('bug', 'signature', bug_parent))

    if writes:
        if not parent or canonical[parent]['meta'].get('type') not in PARENT_TYPES['task']:
            return Question('A Task hangs under a Feature or a Story — add parent: <id>.')
        return _typed(card, Shape('task', 'writes', parent))

    if len(card.features) >= 2:
        if parent:
            return Question(
                'An Epic has no parent — drop the parent: line, or it is a Feature under that Epic.'
            )
        return _typed(card, Shape('epic', 'features-list', None))

    if parent:
        ptype = canonical[parent]['meta'].get('type')
        if ptype == 'feature':
            if not card.acceptance:
                return Question(
                    'A Story is one PR with one acceptance list — add an ## Acceptance checklist.'
                )
            return _typed(card, Shape('story', 'parent-feature', parent))
        if ptype == 'story':
            return Question(
                'Under a Story only a Task hangs — add a writes: line, or parent the card to the Feature.'
            )

    explicit_feature = (headers.get('type') or '').lower() == 'feature'  # the answer to it
    if (not card.acceptance and not explicit_feature
            and DEFECT_WORDS_RE.search(f"{card.title} {card.description}")):
        return Question(
            'This reads as a defect. A Bug carries a signature — add signature: <the failing '
            'test or error line>; or an ## Acceptance list if it is new work.'
        )

    if parent:
        ptype = canonical[parent]['meta'].get('type')
        if ptype != 'epic':
            return Question(f"`parent: {parent}` is a {ptype} — a Feature hangs under an Epic.")
        default_parent = parent
    else:
        default_parent = infer_parent_epic(canonical, tokenize(card.title))
        if default_parent is None:
            return Question('Which Epic is this under? No open Epic shares a title word with it.')
    return _typed(card, Shape('feature', 'default', default_parent))


# ---- delivery shape (F-0086) -------------------------------------------------------------------

Proposal = namedtuple('Proposal', 'verb ids areas why')
#: a ready Task: its Feature, its footprint, and the two ranks the feeder orders by
Ready = namedtuple('Ready', 'id feature writes rank feat_rank')

_BIG = 10 ** 9


def area_of(glob, depth):
    """D5: the glob's literal head, minus its last path component unless the glob ends in `/`,
    cut to the first `depth` segments; a root file is `.`."""
    head = footprint._literal_head(glob)
    if not head.endswith('/'):
        head = head.rpartition('/')[0]
    segments = [seg for seg in head.split('/') if seg]
    return '/'.join(segments[:depth]) or '.'


def areas(writes, depth):
    """`{area: [globs]}` in first-seen order."""
    out = {}
    for glob in writes or []:
        out.setdefault(area_of(glob, depth), []).append(glob)
    return out


def _rank(rec):
    r = rec['meta'].get('rank')
    return r if isinstance(r, int) else _BIG


def _feature_of(canonical, rec):
    """The Feature above a Task — the parent chain, else its `feature:` field."""
    seen = set()
    cur = rec
    while cur is not None and cur['meta'].get('id') not in seen:
        seen.add(cur['meta'].get('id'))
        if cur['meta'].get('type') == 'feature':
            return cur
        cur = canonical.get(cur['meta'].get('parent'))
    f = canonical.get(rec['meta'].get('feature'))
    return f if f is not None and f['meta'].get('type') == 'feature' else None


def _machine(rec):
    return frontmatter.split_machine(rec['meta'])[1]


def _writes(rec):
    w = rec['meta'].get('writes') or []
    return [w] if isinstance(w, str) else list(w)


def ready_tasks(canonical, derived=None):
    """Every Task that could launch now, sorted (Feature rank, Task rank, id): an open, unblocked
    `New` Task of a decided, open Feature at `plan-approved` or `building …`, with no `reshape:`,
    and — when it is a split part — `decided: true`."""
    out = []
    for tid, rec in canonical.items():
        meta = rec['meta']
        if meta.get('type') != 'task' or not is_open(rec):
            continue
        if _machine(rec).get('state', 'New') != 'New' or meta.get('blocked') or meta.get('reshape'):
            continue
        if meta.get('split_from') and meta.get('decided') is not True:
            continue
        feat = _feature_of(canonical, rec)
        if feat is None or not is_open(feat) or feat['meta'].get('decided') is not True:
            continue
        stage = _machine(feat).get('stage') or ''
        if stage != 'plan-approved' and not stage.startswith('building'):
            continue
        out.append(Ready(tid, feat['meta']['id'], _writes(rec), _rank(rec), _rank(feat)))
    return sorted(out, key=lambda r: (r.feat_rank, r.rank, r.id))


def active_tasks(canonical):
    """`[(id, writes)]` of every open Task whose machine state is `Active` — the holders."""
    return [(tid, _writes(rec)) for tid, rec in sorted(canonical.items())
            if rec['meta'].get('type') == 'task' and is_open(rec)
            and _machine(rec).get('state') == 'Active']


def declined_keys(canonical):
    """`{task id: [reshape_declined keys]}` for every Task that carries any."""
    return {tid: list(rec['meta'].get('reshape_declined') or [])
            for tid, rec in canonical.items() if rec['meta'].get('reshape_declined')}


def proposal_key(verb, ids, areas=()):
    """D11: `<verb> <sorted ids joined by +>`, or `split <id> <sorted areas joined by ' | '>`."""
    if verb == 'split':
        return 'split %s %s' % (sorted(ids)[0], ' | '.join(sorted(areas)))
    return '%s %s' % (verb, '+'.join(sorted(ids)))


def _is_declined(key, ids, declined):
    return any(key in (declined or {}).get(i, ()) for i in ids)


def _by_feature(ready):
    out = {}
    for r in ready:
        out.setdefault(r.feature, []).append(r)
    return out


def merge_proposals(ready, declined=None):
    """D2: one Proposal per connected component (size >= 2) of the overlap graph over one
    Feature's ready Tasks. `ids` is ordered (rank, id), so `ids[0]` survives (D7)."""
    out = []
    for fid, group in sorted(_by_feature(ready).items()):
        group = sorted(group, key=lambda r: (r.rank, r.id))
        seen = set()
        for start in group:
            if start.id in seen:
                continue
            comp, stack = [], [start]
            seen.add(start.id)
            while stack:
                cur = stack.pop()
                comp.append(cur)
                for other in group:
                    if other.id not in seen and footprint.overlaps(cur.writes, other.writes):
                        seen.add(other.id)
                        stack.append(other)
            if len(comp) < 2:
                continue
            comp = sorted(comp, key=lambda r: (r.rank, r.id))
            ids = tuple(r.id for r in comp)
            hit = next(h for i, a in enumerate(comp) for b in comp[i + 1:]
                       for h in [footprint.overlaps(a.writes, b.writes)] if h)
            why = ('%s: writes overlap (%s ↔ %s); they run one after the other anyway, '
                   'one session and one gate saved' % (fid, hit[0], hit[1]))
            if not _is_declined(proposal_key('merge', ids), ids, declined):
                out.append(Proposal('merge', ids, (), why))
    return sorted(out, key=lambda p: p.ids)


def batch_proposals(ready, merges, capacity, max_globs, declined=None):
    """D3: per Feature, when the ready units (a pending merge counts as one) exceed `capacity`
    and at least two small Tasks (`len(writes) <= max_globs`) exist, batch the
    `max(2, units - capacity + 1)` smallest, ordered (len(writes), rank, id)."""
    merged_away = {i for m in merges for i in m.ids}
    out = []
    for fid, group in sorted(_by_feature(ready).items()):
        gids = {r.id for r in group}
        units = len(group) - sum(len(m.ids) - 1 for m in merges if m.ids[0] in gids)
        if units <= capacity:
            continue
        small = sorted((r for r in group if len(r.writes) <= max_globs and r.id not in merged_away),
                       key=lambda r: (len(r.writes), r.rank, r.id))
        if len(small) < 2:
            continue
        ids = tuple(r.id for r in small[:max(2, units - capacity + 1)])
        why = ('%s has %d ready Tasks for %d slots; these %d write ≤%d globs each: '
               'one session and one gate instead of %d'
               % (fid, units, capacity, len(ids), max_globs, len(ids)))
        if not _is_declined(proposal_key('batch', ids), ids, declined):
            out.append(Proposal('batch', ids, (), why))
    return out


def split_proposals(ready, active, depth, declined=None):
    """D4: a ready Task with globs in two or more areas, where an area overlaps a holder (an
    `Active` Task or a ready Task of another Feature) and another overlaps none. `active` is
    `[(id, writes)]`."""
    out = []
    for r in ready:
        by_area = areas(r.writes, depth)
        if len(by_area) < 2:
            continue
        holders = [(tid, w, 'Active') for tid, w in active if tid != r.id]
        holders += [(o.id, o.writes, 'ready, ' + o.feature) for o in ready if o.feature != r.feature]
        held, free = [], []
        for area, globs in by_area.items():
            hit = next(((tid, kind) for tid, w, kind in holders if footprint.overlaps(globs, w)), None)
            (held if hit else free).append((area, hit))
        if not held or not free:
            continue
        held_txt = '; '.join('%s is held by %s (%s)' % (a, h[0], h[1]) for a, h in held)
        free_txt = ', '.join(a for a, _ in free)
        why = '%s; %s %s free: the free part can start now' % (
            held_txt, free_txt, 'is' if len(free) == 1 else 'are')
        names = tuple(by_area)
        if not _is_declined(proposal_key('split', (r.id,), names), (r.id,), declined):
            out.append(Proposal('split', (r.id,), names, why))
    return out
