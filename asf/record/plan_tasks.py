"""asf.record.plan_tasks — Task cards from a plan the lane landed (B-0060).

The plan template promises: "the record turns every ``### Task N:`` heading into a Task card and
the feeder launches coders from them". Only ``asf migrate`` (a one-off, run against a legacy
record) ever did — so a Feature whose plan landed on the trunk sat at ``plan-approved`` with no
Task children, and the feeder, which launches PLAN → CODE rows from Task cards, had nothing to
launch. This pass runs in the tick's record step after the ingest: for every Feature whose plan
is on the trunk and that has no Task child yet, the plan's ``### Task N:`` sections become Task
cards — ``parent`` the Feature, ``writes`` from the Task's ``writes:``/``Files:`` line,
``stories`` from its ``stories:`` line (the ids the record holds), ``links.plan`` the plan's
path, the section's text as the description, ``decided: true`` (the plan is approved: it landed).
Ids are minted by :func:`asf.record.ids.mint_id` — never by a session. A Feature that already
has a Task child is left alone: the plan was read once, a re-run is a no-op.
"""
import re

from asf.evidence import evidence
from asf.record.core import canonicalize, load_items, today
from asf.record.ids import mint_id, write_new_item

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


def mint_plan_tasks(root, product, ev, out=print, read_ref=None):
    """Mint the Task cards of every landed plan that has none yet. Returns the new ids."""
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
        ref = plan_ref(features.get(fid.lower()))  # the lane's slug is the id (B-0059)
        if not ref or has_task_child(canonical, fid):
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
    return made
