"""asf.record.replan — a Feature's ``reshape:`` carried out: one replan, applied by the record.

A groom answer ``reshape: <how>`` on a *Task* holds that Task and re-cuts it (the feeder's
RESHAPE → PLAN row). The same answer on a *Feature* is a re-plan: the Feature's not-yet-landed
Tasks are rewritten, added to or dropped, and their ``after:`` moved, while every landed Task and
every commit already on a branch stays as it is.

The decision is pending while the card's ``reshape:`` text is not the one its ``reshape_applied:``
digest names (:func:`pending`) — a newer answer is a newer decision, so a second reshape of the
same Feature is pending again. While it is pending the feeder gives the Feature one
RESHAPE → REPLAN row (a ``replan`` session) and holds its code rows; the session writes one
machine-read document, the **replan**, at :func:`doc_path` on its plan branch, and the docs lane
lands it.

**The replan's format** (the brief states it; this module is its one reader)::

    replan: F-0001 <digest>

    ### Task T-0012: <title>        an open Task of the Feature, rewritten
    stories: S-0003
    writes: lib/a.py, lib/b.py
    after: T-0011                    ids, ``new N`` (the Nth new Task below), or ``none``

    ### Task new: <title>           a Task the record mints
    writes: lib/c.py
    after: none

    ### Task new 2: <title>         a Task the record mints, keyed explicitly as ``new 2`` —
    writes: lib/d.py                 an ``after:`` or another section can name it as ``new 2``
    after: new 1                     whether or not it is the second ``new`` heading on the page

    ### Drop T-0014: <why>          an open Task the replan removes

Once it is on the trunk, the tick's record step applies it (:func:`apply_replans`) — the
record, never the session, writes the cards: a rewritten Task gets the replan's title,
``writes:`` (plus the paths the factory widened it onto that are still on it: its branch already
touches them, :func:`kept_widenings`), ``after:`` (and ``stories:`` when given), and ``links.plan`` the replan's path, so
the plan-order pass reads the new order and not the old plan's; a new Task is minted under the
Feature; a dropped Task is ``removed:``. A section naming a Task that already landed, or one of
another Feature, changes nothing (landed work is kept). An ``after:`` naming a card that can
never land — a removed card that is not done, an id the record does not hold — is dropped: the
dependency on an archived Feature's Tasks is exactly what a replan exists to move. Last, the
Feature gets ``reshape_applied: <digest>`` and ``reshape_applied_at: <time>`` — the record that
the reshape was carried out, which ends the row, and which lifts the parks on the Feature's
Tasks (:func:`replanned_since`, read by :mod:`asf.workers.health`).

A ``### Task new …`` heading that is not ``new`` plus an optional bare number (``new``, ``new 1``,
``new 2``, …), or any other ``###`` line :func:`parse` cannot read as a Task/Drop section, used to
be silently dropped from the document — its body (and any ``after:``/``Drop`` section below it
that happened to read as its own match) still applied, so a Task could vanish into a ``new N``
that was never minted. :func:`parse` now records every such line as a ``bad_sections`` entry, and
every ``after:`` is checked against the Ns that line-up to an actual ``### Task new …`` heading;
when either check fails, :func:`_apply` refuses the **whole** Feature's replan — no mint, no
rewrite, no drop, no ``reshape_applied`` stamp — and prints one ``NEEDS OPERATOR:`` line instead,
so the reshape stays pending for a human rather than a document being half-applied.

Pure helpers first (:func:`digest`, :func:`pending`, :func:`doc_path`, :func:`parse`), then the
record pass. No feeder import: the feeder imports this module.
"""
import hashlib
import re

from asf.record.core import ID_DIGITS, ID_TOKEN_RE

#: the typed fields a replan writes on the Feature
APPLIED = 'reshape_applied'
APPLIED_AT = 'reshape_applied_at'
#: the sub-directory of ``plans_dir`` a replan lives in — outside the plan scan's own directory,
#: so a replan is never read as the Feature's plan (its stage, its review) by the ingest
SUBDIR = 'replans'
DONE_STATES = ('Resolved', 'Closed')      # == asf.feeder.rows.DONE_STATES

HEADER_RE = re.compile(
    rf'^replan:\s*(?P<fid>[A-Z]+-{ID_DIGITS})\s+(?P<digest>[0-9a-f]{{6,64}})\s*$', re.MULTILINE)
SECTION_RE = re.compile(rf'^###\s+(?:Task\s+(?:(?P<tid>[A-Z]+-{ID_DIGITS})|new\s*(?P<newnum>\d*))'
                        rf'|Drop\s+(?P<drop>[A-Z]+-{ID_DIGITS}))\s*:\s*(?P<rest>.*)$',
                        re.MULTILINE | re.IGNORECASE)
#: any ``###`` line at all — used to catch a Task/Drop heading :data:`SECTION_RE` cannot read
#: (``new`` not followed by a bare number, a typo in ``Task``/``Drop``)
HEADING_RE = re.compile(r'^###[ \t]+\S.*$', re.MULTILINE)
#: a heading that means to be a Task or Drop section: one :data:`SECTION_RE` cannot read is a
#: ``bad_sections`` entry; any other ``###`` heading (``### Why``, ``### Order``) is a note
TASKISH_RE = re.compile(r'^###[ \t]+\**[ \t]*(?:task|drop)\b', re.IGNORECASE)
#: where a section's body ends: the next ``##`` or ``###`` heading of any kind
BOUNDARY_RE = re.compile(r'^#{2,3}[ \t]+\S', re.MULTILINE)
#: the second shape: a Feature plan's own ``## … reshaped from <id>`` section (a reshape session
#: that rewrote the plan instead of writing ``replans/``), its Tasks as ``### <id>: <title>``
PLAN_SECTION_RE = re.compile(
    rf'^##[ \t]+[^\n]*?\breshaped from\s+[*`]*(?P<src>[A-Z]-{ID_DIGITS})', re.MULTILINE | re.IGNORECASE)
H2_RE = re.compile(r'^##[ \t]+\S', re.MULTILINE)
DECL_HEADING_RE = re.compile(rf'^###([ \t]+)\**[ \t]*(?P<id>T-{ID_DIGITS})\**[ \t]*:', re.MULTILINE)
#: the field a landed replan the record cannot apply leaves on the Feature: ``<digest>: <why>``
UNREADABLE = 'reshape_unreadable'
FIELD_RE = {k: re.compile(rf'^\s*{k}\s*:\s*(.*)$', re.MULTILINE | re.IGNORECASE)
            for k in ('writes', 'after', 'stories')}
ID_RE = ID_TOKEN_RE
NEW_REF_RE = re.compile(r'\bnew\s+(\d+)\b', re.IGNORECASE)


def digest(text):
    """The first 12 hex of sha256 over ``text`` with its whitespace folded: the name of one
    reshape decision. Re-wrapping the same words is the same decision."""
    return hashlib.sha256(' '.join(str(text or '').split()).encode('utf-8')).hexdigest()[:12]


def pending(feature):
    """The digest of ``feature``'s ``reshape:`` when no replan has carried it out yet, else ''."""
    how = (feature or {}).get('reshape')
    if not how or (feature or {}).get('type') != 'feature':
        return ''
    d = digest(how)
    return '' if str((feature or {}).get(APPLIED) or '') == d else d


def task_pending(task):
    """The digest of an open Task's ``reshape:`` (the groom's split, RESHAPE → PLAN) that no
    reshape section has carried out yet, else ''. The record applies it from the Feature plan's
    ``## … reshaped from <task>`` section (:func:`parse_plan`) when the session that wrote it
    could not reach the record (a cloud session) — never from a ``replans/`` document."""
    t = task or {}
    how = t.get('reshape')
    if not how or t.get('type') != 'task' or t.get('removed') or t.get('state') in DONE_STATES:
        return ''
    d = digest(how)
    return '' if str(t.get(APPLIED) or '') == d else d


def doc_path(plans_dir, fid, d):
    """``<plans_dir>/replans/<fid>-<digest>.md`` — one file per decision, so a second reshape of
    the same Feature never reads the first one's replan."""
    return f"{str(plans_dir).strip('/')}/{SUBDIR}/{fid.lower()}-{d}.md"


def _list(raw):
    raw = (raw or '').strip()
    if raw.startswith('[') and raw.endswith(']'):
        raw = raw[1:-1]
    parts = re.split(r'[,\s]+', raw)
    return [p.strip().strip('`\'"') for p in parts if p.strip().strip('`\'"')]


def _field(body, key):
    m = FIELD_RE[key].search(body or '')
    return m.group(1).strip() if m else None


def parse(text):
    """``{'fid', 'digest', 'tasks': [...], 'drops': [(id, why)], 'bad_sections': [...]}`` off a
    replan, or None when it carries no ``replan:`` header. A task is ``{'id': T-… | None (new),
    'new_num': int (only set for a new task — its explicit ``new N``, else the next N no heading
    claimed, assigned in document order), 'title', 'writes', 'after': [id | ('new', n)] | None,
    'stories': [...] | None, 'body'}``.

    ``bad_sections`` is every ``### Task …``/``### Drop …`` line :data:`SECTION_RE` could not
    read, or whose ``new N`` repeats one already taken — the record never guesses at one of these;
    :func:`_apply` refuses the whole document instead of applying it with a Task silently
    missing. Any other ``###`` heading (``### Why``, ``### Order``) is a ``notes`` entry: prose
    the record reads past, never a refusal. A section's body ends at the next heading."""
    head = HEADER_RE.search(text or '')
    if not head:
        return None
    return _parse_sections(text, head.group('fid'), head.group('digest'))


def _parse_sections(text, fid, d):
    out = {'fid': fid, 'digest': d, 'tasks': [], 'drops': [], 'bad_sections': [], 'notes': []}
    matches = list(SECTION_RE.finditer(text))
    starts = {m.start() for m in matches}
    for hm in HEADING_RE.finditer(text):
        if hm.start() not in starts:
            (out['bad_sections'] if TASKISH_RE.match(hm.group(0)) else out['notes']).append(
                hm.group(0).strip())
    bounds = [b.start() for b in BOUNDARY_RE.finditer(text)]
    seen_nums = set()
    for m in matches:
        end = next((b for b in bounds if b > m.start()), len(text))
        body = text[m.end():end]
        rest = m.group('rest').strip()
        if m.group('drop'):
            out['drops'].append((m.group('drop').upper(), rest))
            continue
        tid = m.group('tid')
        new_num = None
        if tid is None:   # a new-Task heading, bare or with its own N
            raw_num = m.group('newnum')
            if raw_num:
                new_num = int(raw_num)
                if new_num in seen_nums:
                    out['bad_sections'].append(m.group(0).strip())
                    continue
                seen_nums.add(new_num)
        after_raw = _field(body, 'after')
        after = None
        if after_raw is not None:
            after = [] if after_raw.strip().lower() in ('none', '-', '') else \
                ID_RE.findall(after_raw) + [('new', int(n)) for n in NEW_REF_RE.findall(after_raw)]
        stories_raw = _field(body, 'stories')
        writes_raw = _field(body, 'writes')
        out['tasks'].append({
            'id': None if tid is None else tid.upper(),
            'new_num': new_num,
            'title': rest,
            'writes': _list(writes_raw) if writes_raw is not None else None,
            'after': after,
            'stories': re.findall(rf'\bS-{ID_DIGITS}\b', stories_raw) if stories_raw is not None else None,
            'body': body.strip(),
        })
    # a bare new-Task heading with no explicit number claims the next N no heading already took,
    # in document order — the pre-#48 behaviour for a replan that never spells the number out
    next_n = 1
    for t in out['tasks']:
        if t['id'] is None and t['new_num'] is None:
            while next_n in seen_nums:
                next_n += 1
            t['new_num'] = next_n
            seen_nums.add(next_n)
    return out


def parse_plan(text, fid, d):
    """The second shape, off a Feature plan: one doc per ``## … reshaped from <id>`` section, in
    page order, each ``### T-…: <title>`` heading in it read as ``### Task T-…: <title>``
    (``### Task new:`` and ``### Drop <id>:`` read as they do in a replan). ``fid`` and ``d``
    stand in for the header the plan has none of; ``source`` is the id the section was reshaped
    from. ``[]`` when the plan has no such section."""
    text = text or ''
    heads = [h.start() for h in H2_RE.finditer(text)]
    out = []
    for m in PLAN_SECTION_RE.finditer(text):
        nxt = next((h for h in heads if h > m.start()), len(text))
        section = DECL_HEADING_RE.sub(lambda h: f"###{h.group(1)}Task {h.group('id')}:",
                                      text[m.end():nxt])
        doc = _parse_sections(section, fid, d)
        doc['source'] = m.group('src').upper()
        out.append(doc)
    return out


def unreadable(feature):
    """Why the landed replan of ``feature``'s pending ``reshape:`` cannot be applied (its
    :data:`UNREADABLE` field, while it names the pending digest), else ''. The feeder's answer
    to it is a NEEDS DECISION row, never a second replan session."""
    d = pending(feature) or task_pending(feature)
    raw = str((feature or {}).get(UNREADABLE) or '')
    return raw.split(':', 1)[1].strip() if d and raw.startswith(f'{d}:') else ''


def invalid(doc):
    """``None`` when ``doc`` (:func:`parse`'s result) is whole — every heading read as a
    Task/Drop section and every ``after:``'s ``new N`` names a Task the document itself mints —
    else one line naming the first problem. A replan that fails this is refused in full
    (:func:`_apply`): no mint, no rewrite, no drop, no ``reshape_applied`` stamp, rather than
    dropping a Task into a ``new N`` that was never minted (#48)."""
    if doc['bad_sections']:
        return f"unreadable section(s): {'; '.join(doc['bad_sections'])}"
    minted = {t['new_num'] for t in doc['tasks'] if t['id'] is None}
    for t in doc['tasks']:
        for a in t['after'] or ():
            if isinstance(a, tuple) and a[1] not in minted:
                return f"after: new {a[1]} names no Task this replan mints"
    return None


def replanned_since(items, item_id, at):
    """True when the Feature above ``item_id`` (itself for a Feature) was re-planned after
    ``at`` — the park on a Task of it was a park on the plan the replan replaced. ``items`` is
    ``{id: card}``."""
    seen = set()
    item = (items or {}).get(item_id)
    while item and item.get('id') not in seen:
        seen.add(item.get('id'))
        if item.get('type') == 'feature':
            when = str(item.get(APPLIED_AT) or '')
            return bool(when) and when > str(at or '')
        item = items.get(item.get('parent')) or items.get(item.get('feature'))
    return False


def kept_widenings(rec, writes):
    """The paths of ``rec``'s current ``writes:`` that a ``footprint widened: +…`` History line
    added and that the replan's ``writes`` does not list, in the card's order. A replan rewrites
    the plan, never the branch: the Task's commits already touch the paths the factory widened it
    onto, so dropping them sent its next correct session back to ``needs writes`` for the very
    files it had been given — and the Task to a reshape. A widening ``asf`` reverted is no longer
    in ``writes:`` and so is not kept."""
    from asf.feeder.widen import widened_paths    # lazy: the feeder imports this module
    widened = set(widened_paths((rec or {}).get('text')))
    listed = set(writes or ())
    return [w for w in ((rec or {}).get('meta') or {}).get('writes') or ()
            if w in widened and w not in listed]


# ---- the record pass --------------------------------------------------------

def _done(meta):
    return meta.get('state') in DONE_STATES


def apply_replans(root, product, read_plan, out=print):
    """Apply every landed replan of a Feature whose ``reshape:`` is pending. ``read_plan(path)``
    → the file's text on the trunk, or None. Returns ``{fid: [lines]}`` for the Features it
    carried out. One writer through the record stage (R14), as the plan minter is."""
    from asf.record import stage
    made, _staged, _findings = stage.guarded(root, 'replan', _apply,
                                             (product, read_plan, out), product=product, out=out)
    return made or {}


def plan_paths(plans_dir, fid, meta):
    """Where a Feature's own plan may be: its ``links.plan``, then ``<plans_dir>/<fid>.md``."""
    out = []
    link = ((meta or {}).get('links') or {}).get('plan')
    if isinstance(link, str) and link.strip():
        out.append(link.strip())
    out.append(f"{str(plans_dir).strip('/')}/{fid.lower()}.md")
    return list(dict.fromkeys(out))


def find_doc(plans_dir, fid, meta, d, read_plan, canonical=None):
    """``(path, doc, problem)`` for the pending reshape ``d`` of Feature ``fid``: the replan at
    :func:`doc_path` (the first shape), else the ``## … reshaped from`` section of the Feature's
    own plan (:func:`parse_plan`). ``(None, None, None)`` while neither has landed. ``problem``:
    one line when the landed document cannot be applied (:func:`invalid`, another decision's
    replan, a reshape section with no Task in it). A section an earlier reshape already carried
    out (:func:`_settled`) is not this decision's and is passed over."""
    path = doc_path(plans_dir, fid, d)
    text = read_plan(path)
    if text:
        doc = parse(text)
        if not doc or doc['fid'].upper() != fid.upper() or doc['digest'] != d:
            return path, None, f'{path} names another decision'
        return path, doc, invalid(doc)
    return _plan_doc(plan_paths(plans_dir, fid, meta), fid, d, read_plan, canonical)


def _plan_doc(paths, fid, d, read_plan, canonical, source=None):
    for path in paths:
        text = read_plan(path)
        for doc in parse_plan(text, fid, d) if text else ():
            if source is not None and doc['source'] != source.upper():
                continue
            if not doc['tasks'] and not doc['drops']:
                return path, None, (f"{path}: its reshaped-from-{doc['source']} section has no "
                                    f"`### <id>: <title>` Task heading")
            if _settled(doc, canonical or {}):
                continue
            return path, doc, invalid(doc)
    return None, None, None


def find_task_doc(plans_dir, tid, feature, task, d, read_plan, canonical=None):
    """``(path, doc, problem)`` for an open Task's pending ``reshape:`` (:func:`task_pending`):
    the ``## … reshaped from <tid>`` section of its Feature's plan (or of the plan the Task
    links). ``(None, None, None)`` while no plan carries that section."""
    link = ((task or {}).get('links') or {}).get('plan')
    paths = plan_paths(plans_dir, feature.get('id') or '', feature)
    if isinstance(link, str) and link.strip() and link.strip() not in paths:
        paths.append(link.strip())
    return _plan_doc(paths, feature.get('id') or '', d, read_plan, canonical, source=tid)


def _settled(doc, canonical):
    """True when a plan-shape reshape section was carried out already: every Task it names is
    a card and every Drop is gone — an older reshape's section, not this decision's."""
    if doc.get('source') is None:
        return False
    if any(t['id'] is None or t['id'] not in canonical for t in doc['tasks']):
        return False
    return all((canonical.get(i) or {}).get('meta', {}).get('removed') for i, _w in doc['drops'])


def _refuse(rec, d, path, problem, out, set_typed):
    """Leave ``<digest>: <why>`` on the Feature and say NEEDS OPERATOR — once: a Feature that
    already carries this very line is not reported again on the next tick."""
    fid = rec['meta'].get('id') or ''
    line = f'{d}: {path or "the replan"} is not applied — {problem}'
    if str(rec['meta'].get(UNREADABLE) or '') == line:
        return
    out(f'replan: {fid}: NEEDS OPERATOR: {path} is not applied — {problem} — fix the replan and '
        f'land it again, or `asf set {fid} reshape_applied=current --why "<what was done>"`')
    err = set_typed(rec, {UNREADABLE: line}, writer='replan')
    if err:
        out(f'replan: {fid}: {UNREADABLE} not written — {err}')


def _apply(root, product, read_plan, out=print):
    from asf.record import decisions
    from asf.record.core import canonicalize, is_retired, load_items, now_iso, today
    from asf.record.ids import mint_id, write_new_item
    from asf.record.setfield import set_typed
    plans_dir = product.conventions.plans_dir
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    known = None   # the decision register, read once and only when a replan is about to apply
    done = {}
    for iid in sorted(canonical):
        rec = canonical[iid]
        meta = rec['meta']
        if meta.get('type') not in ('feature', 'task') or _done(meta) or is_retired(meta):
            continue
        if meta['type'] == 'feature':
            fid, d = iid, pending(meta)
            if not d:
                continue
            path, doc, problem = find_doc(plans_dir, fid, meta, d, read_plan, canonical)
        else:
            fid, d = str(meta.get('parent') or ''), task_pending(meta)
            owner = canonical.get(fid)
            if (not d or owner is None or owner['meta'].get('type') != 'feature'
                    or _done(owner['meta']) or is_retired(owner['meta'])):
                continue
            path, doc, problem = find_task_doc(plans_dir, iid, owner['meta'], meta, d,
                                               read_plan, canonical)
        if path is None:
            continue
        if problem:
            _refuse(rec, d, path, problem, out, set_typed)
            continue
        if known is None:
            known = decisions.register(canonical, product)
        lines = [f"note: {n}" for n in doc['notes']]

        def live(i):
            r = canonical.get(i)
            return r is not None and (_done(r['meta']) or not is_retired(r['meta']))

        def own_open(i):
            r = canonical.get(i)
            return (r is not None and r['meta'].get('type') == 'task'
                    and r['meta'].get('parent') == fid and not _done(r['meta'])
                    and not is_retired(r['meta']))

        # a plan-shape section's `### T-…:` the record does not hold yet is a Task the reshape
        # session declared from its claimed block: minted under that very id, so the plan's
        # `after:` and a PR's `Proves:` name the card that exists
        declared = {}
        if doc.get('source') is not None:
            for t in doc['tasks']:
                if t['id'] and t['id'] not in canonical:
                    declared[t['id']] = t
                    t['new_num'], t['declared'], t['id'] = -len(declared), t['id'], None

        # new Tasks first, so `new N` resolves before any `after:` is written — keyed by each
        # one's own N (explicit or assigned in parse()), never by its position on the page
        new_by_num = {}
        for t in doc['tasks']:
            if t['id'] is not None:
                continue
            typed = {'title': t['title'] or f'{fid} replan task', 'parent': fid, 'decided': True,
                     'links': {'plan': path}}
            if t['writes']:
                typed['writes'] = t['writes']
            stories = [s for s in t['stories'] or () if s in canonical]
            if stories:
                typed['stories'] = stories
            want = t.get('declared')
            nid = want if want and want.startswith('T-') else mint_id(root, canonical, 'task')
            body = decisions.normalise(t['body'], known)[:4000]
            write_new_item(root, canonical, 'task', nid, typed, body, today(), f'replan {fid}')
            by_id, _e = load_items(root)
            canonical, _d = canonicalize(by_id)
            new_by_num[t['new_num']] = nid
            lines.append(f'{nid} new')
        by_decl = {t['declared']: new_by_num[t['new_num']] for t in declared.values()}

        def resolve(after, me):
            got = []
            for a in after:
                a = new_by_num.get(a[1]) if isinstance(a, tuple) else by_decl.get(a, a)
                if a is None or a == me or a in got:
                    continue
                if live(a):
                    got.append(a)
                else:
                    lines.append(f'{me}: after {a} dropped (no such live card)')
            return got

        named = set()
        for t in doc['tasks']:
            if t['id'] is None:
                me = new_by_num[t['new_num']]
                if t['after']:
                    after = resolve(t['after'], me)
                    if after:
                        set_typed(canonical[me], {'after': after}, writer='replan')
                continue
            tid = t['id']
            named.add(tid)
            if not own_open(tid):
                lines.append(f'{tid}: not an open Task of {fid} — kept as it is')
                continue
            cur = canonical[tid]['meta']
            updates = {'links': dict(cur.get('links') or {}, plan=path)}
            if t['title']:
                updates['title'] = t['title']
            if t['writes'] is not None:
                kept = kept_widenings(canonical[tid], t['writes'])
                updates['writes'] = list(t['writes']) + kept
                if kept:
                    lines.append(f"{tid}: keeps its widened {' '.join(kept)}")
            if t['after'] is not None:
                updates['after'] = resolve(t['after'], tid)
            if t['stories'] is not None:
                updates['stories'] = [s for s in t['stories'] if s in canonical]
            err = set_typed(canonical[tid], updates, writer='replan')
            lines.append(f'{tid}: {err}' if err else f'{tid} rewritten')
        drops = list(doc['drops'])
        src = doc.get('source')
        if src and src not in named and own_open(src) and src not in {i for i, _w in drops}:
            # the Task the section was reshaped from is what its new Tasks replace
            drops.append((src, f"reshaped into {', '.join(new_by_num.values()) or 'the plan'}"))
        for tid, why in drops:
            if not own_open(tid):
                lines.append(f'{tid}: not an open Task of {fid} — not dropped')
                continue
            err = set_typed(canonical[tid], {'removed': f'replan {fid}: {why or "dropped"}'},
                            writer='replan')
            lines.append(f'{tid}: {err}' if err else f'{tid} dropped')
        by_id, _e = load_items(root)
        canonical, _d = canonicalize(by_id)
        err = set_typed(canonical[iid], {APPLIED: d, APPLIED_AT: now_iso()}, writer='replan')
        if err:
            lines.append(f'{iid}: {err}')
        done[iid] = lines
        out(f"replan: {iid}: applied {path} — {'; '.join(lines) or 'nothing to change'}")
    return done
