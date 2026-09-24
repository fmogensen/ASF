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
has a Task child is left alone: the plan was read once, a re-run is a no-op.
"""
import re

from asf.evidence import evidence
from asf.record.core import canonicalize, load_items, today
from asf.record import plan_order
from asf.record.ids import mint_id, write_new_item
from asf.record.ingest import is_retired, match_feature

#: A Feature the ingest already derived Resolved/Closed gets no fresh Task cards from its plan.
DONE_STATES = ('Resolved', 'Closed')

STORIES_LINE_RE = re.compile(r'^\s*stories\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
STORY_ID_RE = re.compile(r'\bS-\d{4}\b')
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


def own_lane_plan(fid, ref, ev):
    """True when the plan at `ref` is the Feature's own lane document: named after its id
    (`f-0047.md`), or landed by a merged spec/plan lane PR whose branch names it."""
    path = ref.split(':', 1)[1] if ':' in ref else ref
    name = path.rsplit('/', 1)[-1]
    if evidence.doc_slug(name).lower() == fid.lower():
        return True
    return path in (((ev or {}).get('lane_docs') or {}).get(fid.upper()) or {}).get('plan', [])


def mint_plan_tasks(root, product, ev, out=print, read_ref=None):
    """Mint the Task cards of every landed plan that has none yet. Returns the new ids. One
    writer through the record stage (R14): a card an invariant refuses is not written, the rest
    are."""
    from asf.record import stage
    made, staged, _findings = stage.guarded(root, 'plan-tasks', _mint, (product, ev, out, read_ref),
                                            product=product, out=out)
    refused = set(staged.refused)
    return [i for i in made if not any(p.endswith(f"/{i}.md") for p in refused)]


def _mint(root, product, ev, out=print, read_ref=None):
    from asf.tick.migrate import plan_task_records, writes_lines
    read_ref = read_ref or (lambda ref: evidence.read_ref(ref, product=product))
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    features = (ev or {}).get('features') or {}
    made = []
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
        # a plan the lane landed for this very id mints at once (B-0059/B-0060); a plan reached
        # through a typed link or a legacy match mints only for a decided card — a migrated
        # record links dozens of old milestone plans, and those are not work the operator ordered
        if rec['meta'].get('decided') is not True and not own_lane_plan(fid, ref, ev):
            continue
        text = read_ref(ref)
        if not text:
            continue
        plan_path = ref.split(':', 1)[1] if ':' in ref else ref
        records = plan_task_records(text)
        if not records:
            out(f'plan-tasks: {fid}: {plan_path} has no `### Task N:` heading — nothing to mint')
            continue
        ids = []
        for t in records:
            typed = {'title': t['title'] or f"{fid} {t['tid']}", 'parent': fid, 'decided': True,
                     'links': {'plan': plan_path}}
            writes = writes_lines(t['body'])
            if writes:
                typed['writes'] = writes
            stories = stories_of(t['body'], canonical)
            if stories:
                typed['stories'] = stories
            new_id = mint_id(root, canonical, 'task')
            body = t['body'].strip()[:DESCRIPTION_CHARS]
            write_new_item(root, canonical, 'task', new_id, typed, body, today(), f'plan {fid}')
            ids.append(new_id)
        made.extend(ids)
        out(f"plan-tasks: {fid}: {len(ids)} Task(s) from {plan_path}: {', '.join(ids)}")
        # the order the plan states, written once every id exists (a Task may name a later one)
        plan_order.backfill(root, lambda _path, _text=text: _text, out=out, only=set(ids))
    return made
