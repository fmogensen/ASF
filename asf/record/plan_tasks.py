"""asf.record.plan_tasks — Task cards from a plan the lane landed (B-0060).

The plan template promises: "the record turns every ``### Task N:`` heading into a Task card and
the feeder launches coders from them". Only ``asf migrate`` (a one-off, run against a legacy
record) ever did — so a Feature whose plan landed on the trunk sat at ``plan-approved`` with no
Task children, and the feeder, which launches PLAN → CODE rows from Task cards, had nothing to
launch. This pass runs in the tick's record step after the ingest: for every Feature whose plan
is on the trunk and that has no Task child yet, the plan's ``### Task N:`` sections become Task
cards — ``parent`` the Feature, ``writes`` from the Task's ``writes:``/``Files:`` line,
``stories`` from its ``stories:`` line (the ids the record holds), ``links.plan`` the plan's
path, the section's text as the description, ``decided: true`` (the plan is approved: it landed),
and ``after`` from the order the plan states (:mod:`asf.record.plan_order`) — without it every
Task of a plan launched at once and each successor's coder found nothing to build on.
Ids are minted by :func:`asf.record.ids.mint_id` — never by a session. A Feature that already
has a Task child is left alone: the plan was read once, a re-run is a no-op. A plan whose Tasks
cite a decision id (``D-nnnn``, outside a code span) the decision register lacks
(:mod:`asf.record.decisions`) mints nothing: its line names the missing ids. Nor does a plan
citing an S-/T-/B- id no claim covers, or redeclaring one the record holds
(:mod:`asf.record.idcheck`); a Task whose (parent, stories, writes) equals an open Task's — or
an earlier Task of the same plan — is not minted, and its line names the existing id.
"""
import re

from asf.evidence import evidence
from asf.record.core import canonicalize, load_items, today
from asf.record import decisions, idcheck, idclaim, plan_order, trunk_check
from asf.record.ids import mint_id, write_new_item
from asf.record.ingest import is_retired, match_feature
from asf.record.core import ID_DIGITS

#: A Feature the ingest already derived Resolved/Closed gets no fresh Task cards from its plan.
DONE_STATES = ('Resolved', 'Closed')

STORIES_LINE_RE = re.compile(r'^\s*stories\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
STORY_ID_RE = re.compile(rf'\bS-{ID_DIGITS}\b')
DESCRIPTION_CHARS = 4000


def stories_of(body, canonical):
    """The Story ids on the Task's ``stories:`` line that the record holds, in order."""
    m = STORIES_LINE_RE.search(body or '')
    if not m:
        return []
    return [s for s in dict.fromkeys(STORY_ID_RE.findall(m.group(1))) if s in canonical]


def has_task_child(canonical, fid):
    return any(r['meta'].get('type') == 'task' and r['meta'].get('parent') == fid
               for r in canonical.values())


def plan_ref(fev):
    """``rev:path`` of the plan on the trunk, or None."""
    ref = (fev or {}).get('plan')
    return ref if ref and fev.get('plan_on_main') else None


def plan_alias(ev, path):
    """The id the plan at `path` declares in its own H1, as the evidence read it — the `alias`
    of the features entry that carries this plan — or None."""
    from asf.record.ingest import _path_only
    for f in ((ev or {}).get('features') or {}).values():
        if _path_only(f.get('plan')) == path:
            return f.get('alias')
    return None


def own_plan(fid, ref, ev):
    """True when the plan at `ref` declares this Feature, by any of the three routes the matcher
    knows: named after its id (`f-0047.md`); landed by a merged spec/plan lane PR — or a
    fast-forward lane the ledger records — whose branch names it; or headed `# F-0047 — …`,
    whatever the file is called (F-0123).

    These are the same three routes as :func:`asf.record.ingest._own_candidates`, read through
    the same evidence: a plan that brings a Feature to plan-approved is a plan that mints."""
    path = ref.split(':', 1)[1] if ':' in ref else ref
    name = path.rsplit('/', 1)[-1]
    if evidence.doc_slug(name).lower() == fid.lower():
        return True
    if path in (((ev or {}).get('lane_docs') or {}).get(fid.upper()) or {}).get('plan', []):
        return True
    return str(plan_alias(ev, path) or '').upper() == fid.upper()


def _claim_view(root):
    """Origin's id claims (the local mirror when origin cannot be read); [] without one."""
    if not idclaim.has_origin(root):
        return []
    try:
        idclaim.fetch(root)
    except idclaim.ClaimError:
        pass
    return idclaim.claims(root)


def mint_plan_tasks(root, product, ev, out=print, read_ref=None, refusals=None):
    """Mint the Task cards of every landed plan that has none yet. Returns the new ids. One
    writer through the record stage (R14): a card an invariant refuses is not written, the rest
    are. ``refusals`` (a dict, when given) receives each Feature whose landed plan was refused
    whole: ``{fid: {'reason': the line, 'ids': {id: kind}}}`` — ``ids`` the S-/T-/B- ids the
    claim check named (:func:`asf.record.idcheck.findings`), empty for any other refusal."""
    from asf.record import stage
    made, staged, _findings = stage.guarded(root, 'plan-tasks', _mint,
                                            (product, ev, out, read_ref, refusals),
                                            product=product, out=out)
    refused = set(staged.refused)
    return [i for i in made if not any(p.endswith(f"/{i}.md") for p in refused)]


def _mint(root, product, ev, out=print, read_ref=None, refusals=None):
    from asf.tick.migrate import plan_task_records, task_like_headings, writes_lines

    def refuse(fid, line, ids=()):
        out(line)
        if refusals is not None:
            refusals[fid] = {'reason': line.split(': ', 2)[-1], 'ids': dict(ids)}
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    features = (ev or {}).get('features') or {}
    made = []
    known = None   # the decision register, read once and only when a plan is about to mint
    claimed = None  # origin's id claims, read once and only when a plan is about to mint

    # collection pass: every guard above reads metadata only (type, has_task_child, DONE_STATES,
    # is_retired, match_feature/plan_ref, decided/own_plan) — none of it depends on the plan's
    # text, so this reaches the same refs, in the same order, that the minting loop used to
    candidates = []
    specs = {}   # fid -> the landed spec's ref
    for fid in sorted(canonical):
        rec = canonical[fid]
        if rec['meta'].get('type') != 'feature':
            continue
        if (has_task_child(canonical, fid) or rec['meta'].get('state') in DONE_STATES
                or is_retired(rec['meta'])):
            continue
        # the plan is found the way the ingest finds it: links.plan, the lane's slug (B-0059),
        # or the plan the Feature's merged lane PR landed — a date-prefixed file name
        # (`2026-09-20-free-plan.md`) does not carry the id, its lane branch does
        _slug, fev = match_feature(rec['meta'], {**(ev or {}), 'features': features})
        ref = plan_ref(fev)
        if not ref:
            continue
        plan_path = ref.split(':', 1)[1] if ':' in ref else ref
        # a plan the lane landed for this very id mints at once (B-0059/B-0060); a plan reached
        # through a typed link or a legacy match mints only for a decided card — a migrated
        # record links dozens of old milestone plans, and those are not work the operator ordered
        if rec['meta'].get('decided') is not True and not own_plan(fid, ref, ev):
            out(f'plan-tasks: {fid}: {plan_path} is on the trunk but names {fid} nowhere — '
                f'not decided, so nothing minted')
            continue
        candidates.append((fid, ref, plan_path))
        # the Feature's landed spec too: the `### S-…` Stories it declares are minted with
        # the Tasks (F-0285)
        if fev.get('spec') and fev.get('spec_on_main'):
            specs[fid] = fev['spec']

    # one batch for the lot; the injected `read_ref` (the tests' seam) stays per-ref, as before
    refs = [ref for _fid, ref, _plan_path in candidates] + list(specs.values())
    if read_ref is not None:
        texts = {ref: read_ref(ref) for ref in refs}
    else:
        texts = evidence.read_refs(refs, product=product)

    for fid, ref, plan_path in candidates:
        rec = canonical[fid]
        text = texts.get(ref)
        if not text:
            out(f'plan-tasks: {fid}: {plan_path} could not be read — nothing minted')
            continue
        records = plan_task_records(text)
        if not records and task_like_headings(text):
            refuse(fid, f'plan-tasks: {fid}: {plan_path} has {len(task_like_headings(text))} '
                   f'Task-like heading(s) but parses to 0 Tasks — nothing minted (heading format '
                   f'drift: {task_like_headings(text)[0]!r})')
            continue
        if not records:
            refuse(fid, f'plan-tasks: {fid}: {plan_path} has no `### Task N:` heading — '
                   f'nothing to mint')
            continue
        # a Task that cites a decision the register lacks (S6) is a decision that was never made:
        # the plan is refused whole, before any card exists, and the line names each one
        if known is None:
            known = decisions.register(canonical, product)
        missing = list(dict.fromkeys(
            d for t in records for d in decisions.unknown(f"{t['title'] or ''}\n{t['body']}", known)))
        if missing:
            refuse(fid, f"plan-tasks: {fid}: {plan_path} cites decision(s) not in the register: "
                   f"{', '.join(missing)} — nothing minted (record the decision, or drop the id)")
            continue
        # the ids the plan mints must be its session's claimed ones (asf.record.idcheck): an
        # invented id, or one the record holds for another card, refuses the plan whole
        if claimed is None:
            claimed = _claim_view(root)
        bad = idcheck.findings(text, canonical, claimed, fid)
        if bad:
            refuse(fid, f"plan-tasks: {fid}: {plan_path} mints id(s) no claim covers: "
                   f"{'; '.join(line for _i, _k, line in bad)} "
                   f"— nothing minted (take ids from the session's BACKLOG_ID_RANGE)",
                   [(i, k) for i, k, _line in bad])
            continue
        # F-0285: a Story the plan cites must be a card, or a `### S-…: <title>` heading in the
        # plan or its spec — a session that cannot reach the record (a cloud one) declares its
        # Stories that way, from its claimed block, and the record mints them here, before the
        # Tasks that cite them. A Story nobody declared refuses the plan whole.
        declared = {**idcheck.declared_stories(texts.get(specs.get(fid)) or ''),
                    **idcheck.declared_stories(text)}
        phantom = idcheck.phantom_stories(text, canonical, declared)
        if phantom:
            refuse(fid, f"plan-tasks: {fid}: {plan_path} cites Story id(s) never minted: "
                   f"{', '.join(phantom)} — nothing minted (each needs a card, or a "
                   f"`### <id>: <title>` heading with its acceptance lines in the spec or plan)")
            continue
        fresh = {s: d for s, d in declared.items() if s not in canonical}
        bad = idcheck.findings('\n'.join(f'### {s}: {d["title"]}' for s, d in fresh.items()),
                               canonical, claimed, fid)
        if bad:
            refuse(fid, f"plan-tasks: {fid}: {plan_path} declares Story id(s) no claim covers: "
                   f"{'; '.join(line for _i, _k, line in bad)} — nothing minted (take ids from "
                   f"the session's BACKLOG_ID_RANGE)", [(i, k) for i, k, _line in bad])
            continue
        for sid, d in fresh.items():
            write_new_item(root, canonical, 'story', sid,
                           {'title': d['title'] or f'{fid} {sid}', 'parent': fid}, '', today(),
                           f'declared in {plan_path}', acceptance=d['acceptance'] or (),
                           shape=('parent-feature', 'story'))
            made.append(sid)
        if fresh:
            out(f"plan-tasks: {fid}: {len(fresh)} Story(ies) declared: {', '.join(fresh)}")
            by_id, _errors = load_items(root)
            canonical, _dupes = canonicalize(by_id)
        ids = []
        keys = {}
        for t in records:
            # a bare `D7` the register holds is written as the record's `D-0007` on the card
            typed = {'title': decisions.normalise(t['title'], known) or f"{fid} {t['tid']}",
                     'parent': fid, 'decided': True, 'links': {'plan': plan_path}}
            writes = writes_lines(t['body'])
            if writes:
                typed['writes'] = writes
            stories = stories_of(t['body'], canonical)
            if stories:
                typed['stories'] = stories
            dup = idcheck.duplicate_task(canonical, fid, typed.get('stories'), typed.get('writes'),
                                         extra=keys)
            if dup:  # the same (parent, stories, writes) as an open Task: the same work twice
                out(f"plan-tasks: {fid}: {plan_path} {t['tid']} duplicates {dup} "
                    f"(same parent, stories and writes) — not minted")
                continue
            # F-0106: the trunk is asked before the card is born. A Task whose named tests are
            # already on origin/main is minted closed-by-trunk with the sha, not New — a coder
            # launched onto it would find the surface there and end `empty branch` (T-0083/T-0084).
            found = (trunk_check.satisfied_on_trunk(product, t['body'], writes)
                    if product is not None else None)
            state, why = 'New', f'plan {fid}'
            if found:
                sha, subject, reason = found
                typed['landed'] = sha
                state = 'Closed'
                why = f'plan {fid} — already on the trunk at {sha[:12]}: {reason}'
                out(f'plan-tasks: {fid}: {t["tid"]} is already on the trunk at {sha[:12]} '
                    f'"{subject[:60]}" — minted Closed ({reason})')
            new_id = mint_id(root, canonical, 'task')
            keys[new_id] = idcheck.task_key(fid, typed.get('stories'), typed.get('writes'))
            body = decisions.normalise(t['body'].strip(), known)[:DESCRIPTION_CHARS]
            write_new_item(root, canonical, 'task', new_id, typed, body, today(), why,
                           state=state)
            ids.append(new_id)
        made.extend(ids)
        out(f"plan-tasks: {fid}: {len(ids)} Task(s) from {plan_path}: {', '.join(ids)}")
        # the order the plan states, written once every id exists (a Task may name a later one)
        plan_order.backfill(root, lambda _path, _text=text: _text, out=out, only=set(ids))
    return made
