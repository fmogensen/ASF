"""asf.record.slice — a Feature's Tasks as one delivery (``conventions.delivery: feature``).

The Feature is the unit of value; a Task is a line of its plan. Under the Task lane every Task
pays a branch, a PR, a CI run and a review of its own (a product, 2026-09-27: a landed Task
cost 5.0 sessions and $9.83 at a median diff of 326 lines, and 216 of 316 Tasks named no
Story). Under ``delivery: feature`` the Tasks of a Feature ride F-0102's delivery mechanism
instead: the first Task **leads** (``delivers: [T1, T2, …]``, plan order), the others point back
(``delivered_by: T1``), and the feeder launches one DELIVERY → CODE session that builds them in
that order on one branch — one commit per Task, so the ingest still closes each Task by its
own commit — and lands them as one PR with one review.

**The cut.** A Feature is whole by default. Only mechanically — more than
``conventions.slice_max_tasks`` (6) Tasks, or a union footprint of more than
``size.medium_max_files`` (15) entries — is the plan cut into ordered **slices**
(:func:`cut`): the Tasks are walked in wave order (a Task's wave is one past its deepest
``after:`` predecessor) and packed greedily, and a cut that falls mid-wave backs up to the last
wave boundary inside the slice, so a slice ends where the plan's order does. A ``size: s``
Feature is always whole. A slice of one Task is no delivery: it stays on the Task lane as it is.

**The pass** (:func:`deliveries`) runs in the tick's record step, after the plan's Tasks are
minted and their ``after:`` backfilled. It reads every open Feature whose plan is approved and
turns its **free** Tasks — New, decided, not blocked, no session on them, in no delivery yet —
into deliveries by the cut. That is the mint case (a freshly planned Feature: every Task is
free) and the migration case (a half-built Feature: its unstarted Tasks) in one rule; a slice
lead of a migrated Feature also gains ``after:`` on the Feature's started Tasks, so
:func:`asf.feeder.rows.hold_unlanded` holds the delivery until they land. Mid-lane Tasks land
as they are. Idempotent: a Task in a delivery is never re-cut, and no hand edit is needed.

Pure functions over plain lists first (:func:`levels`, :func:`cut`), then the record pass. The
record does not import the feeder: :data:`DONE_STATES` and :data:`BUILD_STAGES` mirror
:mod:`asf.feeder.rows`'s.
"""
import re

from asf.record.core import canonicalize, load_items
from asf.record import undeliver

DONE_STATES = ('Resolved', 'Closed')       # == asf.feeder.rows.DONE_STATES
BUILD_STAGES = ('plan-approved', 'building')  # == asf.feeder.rows.BUILD_STAGES
BIG = 10 ** 9                              # == asf.views.index_reader.BIG


def levels(order, after):
    """``{tid: wave}`` over ``order`` (the plan's Task ids, in plan order): 0 for a Task with no
    predecessor inside ``order``, else one past its deepest predecessor's. A predecessor outside
    ``order`` (a card of another Feature, a Task already started) does not place the Task; a
    cycle is broken at the Task that closes it."""
    ids = list(order)
    known = set(ids)
    out = {}

    def walk(t, seen):
        if t in out:
            return out[t]
        preds = [p for p in (after.get(t) or ()) if p in known and p != t and p not in seen]
        out[t] = 1 + max((walk(p, seen | {p}) for p in preds), default=-1)
        return out[t]
    for t in ids:
        walk(t, {t})
    return out


def union(ids, writes):
    """The ordered union of ``writes[tid]`` over ``ids``, one entry per glob."""
    out = []
    for t in ids:
        for w in writes.get(t) or ():
            if w not in out:
                out.append(w)
    return out


def fits(ids, writes, max_tasks, max_files):
    """True when ``ids`` is one delivery: at most ``max_tasks`` Tasks and a union footprint of at
    most ``max_files`` entries — an entry is what the plan wrote, a glob counts as one (F-0041
    D1 counts entries the same way)."""
    return len(ids) <= max_tasks and len(union(ids, writes)) <= max_files


def cut(order, after, writes, max_tasks, max_files, whole=False):
    """The slices of a plan: ``[[tid, …], …]``, each in plan order, in wave order. One slice —
    the whole Feature — when ``whole`` (a ``size: s`` Feature) or when the plan :func:`fits`;
    else the Tasks in wave order packed to the limits, each cut backed up to the last wave
    boundary inside the slice when there is one."""
    ids = [t for t in dict.fromkeys(order)]
    if not ids:
        return []
    if whole or fits(ids, writes, max_tasks, max_files):
        return [ids]
    lv = levels(ids, after)
    pos = {t: n for n, t in enumerate(ids)}
    seq = sorted(ids, key=lambda t: (lv[t], pos[t]))
    slices, cur = [], []
    for t in seq:
        while cur and not fits(cur + [t], writes, max_tasks, max_files):
            if lv[t] != lv[cur[-1]]:  # the cut falls on a wave boundary as it is
                slices.append(cur)
                cur = []
                break
            # mid-wave: back up to the last wave boundary inside the slice, when it has one
            j = max((k for k in range(1, len(cur)) if lv[cur[k]] != lv[cur[k - 1]]), default=0)
            if j:
                slices.append(cur[:j])
                cur = cur[j:]
            else:
                slices.append(cur)
                cur = []
        cur.append(t)
    if cur:
        slices.append(cur)
    return [sorted(s, key=lambda t: pos[t]) for s in slices]


# ---- the record pass ------------------------------------------------------------------------

def _rank(meta):
    r = meta.get('rank')
    return r if isinstance(r, int) and not isinstance(r, bool) else BIG


def _feature_of(metas, meta):
    """The Feature above a Task (its parent, or its Story's parent), or None."""
    seen = set()
    while meta and meta.get('id') not in seen:
        seen.add(meta.get('id'))
        if meta.get('type') == 'feature':
            return meta
        meta = metas.get(meta.get('parent'))
    return None


def _in_delivery(meta):
    return bool(meta.get('delivers') or meta.get('delivered_by'))


def undelivered_tasks(canonical):
    """The Task ids an ``asf undeliver`` has taken out of a delivery (D4, D6): every Task whose
    card carries a member-shape ``## History`` line (:func:`asf.record.undeliver.undelivered_from`).
    The frontmatter cannot answer this directly — ``undeliver`` deletes ``delivered_by:``
    (:func:`asf.record.undeliver.cmd_undeliver`), so a Task it ruled out reads exactly like a
    Task that was never bundled. The pass reads the card text itself instead, once per tick, to
    keep that Task out of every delivery it would otherwise rejoin."""
    return {tid for tid, rec in canonical.items() if rec['meta'].get('type') == 'task'
            and undeliver.undelivered_from(rec.get('text'))}


PR_RE = re.compile(r'\bPR #\d+\b')  # == asf.feeder.rows.PR_RE


def idle(meta, busy=()):
    """True for an Active Task on an idle branch (== :func:`asf.feeder.rows.idle_branch`): no PR
    in its evidence and nothing holding it (``busy``). Its branch has nothing past the trunk
    that anyone is moving — the Task is as free as a New one, and folds into the delivery."""
    return (meta.get('state') == 'Active' and meta.get('id') not in busy
            and not any(PR_RE.search(str(e)) for e in meta.get('evidence') or []))


def free_tasks(metas, fid, busy=(), skip=()):
    """The Feature's Tasks the pass may deliver, in plan order (rank, then id — a minted Task's
    id order is its plan order): New (or Active on an idle branch, :func:`idle`), ``decided``,
    not blocked, not removed, no session or pushed branch on them (``busy``), in no delivery
    yet, and not an ``asf undeliver`` has ruled out of this Feature's deliveries (``skip``,
    :func:`undelivered_tasks`)."""
    out = []
    for tid, m in metas.items():
        if m.get('type') != 'task' or m.get('removed') or m.get('moved_to'):
            continue
        f = _feature_of(metas, m)
        if not f or f.get('id') != fid:
            continue
        if (m.get('state', 'New') != 'New' and not idle(m, busy)) or m.get('decided') is not True \
                or m.get('blocked') or tid in busy or _in_delivery(m) or m.get('reshape') \
                or tid in skip:
            continue
        out.append(tid)
    return sorted(out, key=lambda t: (_rank(metas[t]), t))


def started_tasks(metas, fid, busy=()):
    """The Feature's Tasks under way — past ``New`` but not done, or held by a session or a
    pushed branch — that a migrated delivery waits on (``after:``). An idle one is free, not
    started (:func:`idle`)."""
    out = []
    for tid, m in metas.items():
        if m.get('type') != 'task' or m.get('removed'):
            continue
        f = _feature_of(metas, m)
        if not f or f.get('id') != fid:
            continue
        state = m.get('state', 'New')
        if state in DONE_STATES or idle(m, busy):
            continue
        if state != 'New' or tid in busy:
            out.append(tid)
    return sorted(out)


def plan_slices(metas, fid, conv, busy=(), skip=()):
    """The deliveries the pass would form for ``fid`` now: ``[(lead, members)]`` for every slice
    of two or more free Tasks (:func:`cut` over :func:`free_tasks`) — a preview of the pass, with
    no write. ``conv`` is the product's :class:`asf.conventions.Conventions`."""
    feature = metas.get(fid) or {}
    free = free_tasks(metas, fid, busy, skip)
    if len(free) < 2:
        return []
    after = {t: [a for a in (metas[t].get('after') or ()) if isinstance(a, str)] for t in free}
    writes = {t: list(metas[t].get('writes') or ()) for t in free}
    whole = str(feature.get('size') or '').strip().lower() == 's'
    slices = cut(free, after, writes, conv.slice_max_tasks_n(), conv.slice_max_files(), whole)
    return [(s[0], s) for s in slices if len(s) >= 2]


def deliverable_features(metas):
    """The open Features whose plan is approved (a build stage): the ones whose free Tasks the
    pass delivers."""
    out = []
    for fid, m in metas.items():
        if m.get('type') != 'feature' or m.get('removed') or m.get('moved_to'):
            continue
        if m.get('state', 'New') in DONE_STATES or m.get('decided') is not True:
            continue
        if (m.get('stage') or '').split(' ')[0] in BUILD_STAGES:
            out.append(fid)
    return sorted(out)


def _edge_clause(t, member, glob):
    """One ``after:`` edge named by why it is there: the started Task, the member whose
    footprint it meets, and the glob pair :func:`asf.feeder.footprint.first_intersection`
    returned for ``(t's writes, member's writes)`` — the member's glob printed first, joined to
    the started Task's by `` ~ `` when the two strings differ, one glob when they are equal."""
    started_glob, member_glob = glob
    shared = member_glob if member_glob == started_glob else f'{member_glob} ~ {started_glob}'
    return f'{t} ({member} shares {shared})'


def deliveries(root, product, busy=(), out=print):
    """The pass: every deliverable Feature's free Tasks become deliveries. Writes go through
    :func:`asf.record.setfield.set_typed` (the parser round-trip). Returns
    ``{lead: [members]}`` for the deliveries written."""
    from asf.record.setfield import set_typed
    from asf.feeder import footprint  # this module's own idiom, not a layering rule — the
    # record already opens `deliveries` with a function-local `set_typed` and
    # `unordered_overlaps` does the same; `asf/record/check.py` imports the feeder at module
    # scope, so nothing rides on the form either way (F-0315 PD4)
    conv = product.conventions
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    metas = {i: r['meta'] for i, r in canonical.items()}
    busy = set(busy or ())
    shared = footprint.shared_globs(product)
    skip = undelivered_tasks(canonical)
    written = {}
    for fid in deliverable_features(metas):
        started = started_tasks(metas, fid, busy)
        slices = plan_slices(metas, fid, conv, busy, skip)
        for n, (lead, members) in enumerate(slices, 1):
            waits = [a for a in (metas[lead].get('after') or ()) if isinstance(a, str)]
            covered = {a for m in members for a in (metas[m].get('after') or ())}
            binds, loose = [], []
            for t in started:
                if t in covered or t in members:
                    continue
                hit = None
                for m in members:
                    glob = footprint.first_intersection(metas[t].get('writes'),
                                                        metas[m].get('writes'), shared)
                    if glob:
                        hit = (t, m, glob)
                        break
                (binds if hit else loose).append(hit or t)
            extra = [t for t, _m, _g in binds]
            updates = {'delivers': list(members)}
            if extra:
                updates['after'] = waits + [t for t in extra if t not in waits]
            err = set_typed(canonical[lead], updates)
            if err:
                out(f'slice: {lead}: {err}')
                continue
            for m in members[1:]:
                err = set_typed(canonical[m], {'delivered_by': lead})
                if err:
                    out(f'slice: {m}: {err}')
            written[lead] = list(members)
            stories = sorted({s for m in members for s in (metas[m].get('stories') or ())})
            out(f"slice: {fid} {n}/{len(slices)}: {lead} delivers {', '.join(members)}"
                + (f" (proves {', '.join(stories)})" if stories else '')
                + (f" after {', '.join(_edge_clause(*b) for b in binds)}" if binds else '')
                + (f" — {len(loose)} started, no shared path: {', '.join(loose)}"
                   if loose else ''))
    return written


def busy_items(product):
    """The item ids a session or a pushed branch holds — what the pass reads as "started" beside
    the record's own states (:mod:`asf.workers.lifecycle`)."""
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    path = pool_mod.sessions_path(product)
    out = {s['item'] for s in lifecycle.inflight(path) if s.get('item')}
    out |= set(lifecycle.awaiting_harvest(path)) | set(lifecycle.unlanded(path))
    return out
