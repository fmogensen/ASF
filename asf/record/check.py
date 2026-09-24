"""asf.record.check — validate the backlog (``asf check``)."""
import json
import os
import re

from asf.conventions import DEFAULT_SPECS_DIR
from asf.groom import shape
from asf.init import ITEM_FOLDERS as LAYOUT_FOLDERS, STREAM_FOLDERS
from asf.record import frontmatter
from asf.record.core import (
    BARE_DECISION_RE, FOLDER_TO_TYPE, ID_RE, NO_PARENT_TYPES, PARENT_TYPES, build_index_data,
    canonicalize, compute_derived, expected_body, is_open, load_items, parse_sections, today,
    writes_intersect,
)

ACCEPTANCE_ITEM_RE = re.compile(r'(?m)^- \[[ x]\]\s+\S')
BULLET_RE = re.compile(r'(?m)^- \S')

LANDED_SHA_RE = re.compile(r'[0-9a-fA-F]{7,40}')
RESIDUE_RULE = 'rule: no-rule'
SPEC_CLOSING = DEFAULT_SPECS_DIR + '/f-0080.md'  # the spec this pass points at, by the default layout

# A Feature normally requires a parent Epic, but `migrate` may leave one parentless when the
# adopted source names no Epic for it and it carries no Story of its own to infer one from. Set
# per product via a real expiry date once adopted; None means the exception never applies.
FEATURE_NO_PARENT_UNTIL = None


def check_deliveries(canonical, add, find_line):
    """A delivery is a lead's `delivers:` list; each member points back with `delivered_by:`."""
    claimed = {}  # member id -> the lead that first listed it
    for lid, rec in canonical.items():
        for mid in rec['meta'].get('delivers') or []:
            claimed.setdefault(mid, lid)
    for lid, rec in canonical.items():
        meta = rec['meta']
        delivers = meta.get('delivers')
        if not delivers:
            continue
        line = find_line(rec, 'delivers')
        if meta.get('delivered_by'):
            add(rec, line, f"{lid}: carries both delivers: and delivered_by: — a card leads or is led, not both")
        if delivers[0] != lid:
            add(rec, line, f"{lid}: delivers[0] is {delivers[0]}, not the lead itself")
        for mid in delivers:
            if claimed[mid] != lid:
                add(rec, line, f"{mid} is in two delivers: lists ({claimed[mid]} and {lid})")
                continue
            member = canonical.get(mid)
            if member is None:
                add(rec, line, f"delivers references missing item {mid}")
            elif member['meta'].get('removed'):
                add(rec, line, f"delivers references removed item {mid}")
            elif mid != lid and member['meta'].get('delivered_by') != lid:
                add(rec, line, f"{mid} is in {lid}'s delivers: but does not carry delivered_by: {lid}")


def residue_gaps(meta, evidence, canonical):
    """The parenthetical of a residue finding: the item's type, and what its evidence lines and
    the record do not carry — the reasons no closing rule had anything to read."""
    type_ = meta.get('type')
    iid = meta.get('id')
    gaps = [f"type {type_}"]
    if type_ == 'story':
        listed = any(r['meta'].get('type') == 'task' and not r['meta'].get('removed')
                     and iid in (r['meta'].get('stories') or []) for r in canonical.values())
        if not listed:
            gaps.append('no Task')
        if not any(str(l).startswith('matrix status') for l in evidence):
            gaps.append('no matrix row')
    elif type_ in ('feature', 'epic'):
        if not any(r['meta'].get('parent') == iid for r in canonical.values()):
            gaps.append('no children')
    parent = meta.get('parent')
    if parent in canonical:
        state = frontmatter.split_machine(canonical[parent]['meta'])[1].get('state', 'New')
        gaps.append(f"parent {parent} is {state}")
    return ', '.join(gaps)


def check_residue(canonical, add, find_line):
    """§2.7: an item whose last evidence line is `rule: no-rule` is one no closing rule sees —
    a finding the day it is written; and a typed `landed:` must at least be shaped like a sha
    (whether it is on the trunk is what `ingest` decides, `check` touches no git)."""
    for rec in canonical.values():
        meta = rec['meta']
        typed, machine = frontmatter.split_machine(meta)
        landed = typed.get('landed')
        if landed not in (None, '') and not LANDED_SHA_RE.fullmatch(str(landed)):
            add(rec, find_line(rec, 'landed'),
                f"landed: {str(landed)!r} is not a 7-40 character hex sha; "
                f"whether it is on the trunk is what `ingest` decides; see {SPEC_CLOSING} §2.5")
        evidence = machine.get('evidence')
        if typed.get('removed') or not isinstance(evidence, list) or not evidence \
                or evidence[-1] != RESIDUE_RULE:
            continue
        add(rec, find_line(rec, 'evidence'),
            f"no closing rule sees this item ({residue_gaps(meta, evidence, canonical)}) "
            f"— it can never close; see {SPEC_CLOSING} §2.1")


def cmd_check(args, root):
    paths = args.paths or None
    restrict = None
    if paths:
        restrict = {os.path.relpath(os.path.abspath(p), root) for p in paths}

    by_id, parse_errors = load_items(root)
    canonical, dupes = canonicalize(by_id)
    derived = compute_derived(canonical)

    findings = []  # (relpath, line, message)

    # the layout the README names — item folders plus the stream folders `asf init` lays down;
    # a record missing one of these isn't a bad item, it's a tick waiting to fail on a missing dir
    layout_folders = LAYOUT_FOLDERS + STREAM_FOLDERS
    for folder in layout_folders:
        if not os.path.isdir(os.path.join(root, folder)):
            findings.append((folder, 1, f"{folder}/ is missing (run `mkdir -p {folder}`)"))

    def add(rec_or_path, line, msg):
        relpath = rec_or_path if isinstance(rec_or_path, str) else rec_or_path['relpath']
        findings.append((relpath, line, msg))

    for f, line, why in parse_errors:
        findings.append((f, line, why))

    def find_line(rec, key):
        lines = rec['text'].split('\n')
        for i, l in enumerate(lines):
            if l.startswith(f"{key}:"):
                return i + 1
        return 1

    for iid, rec in canonical.items():
        meta = rec['meta']
        # id == filename
        stem = os.path.splitext(rec['name'])[0]
        if meta.get('id') != stem:
            add(rec, find_line(rec, 'id'), f"id {meta.get('id')!r} does not match filename {rec['name']}")
        # type == folder
        expected_type = FOLDER_TO_TYPE.get(rec['folder'])
        if meta.get('type') != expected_type:
            add(rec, find_line(rec, 'type'), f"type {meta.get('type')!r} does not match folder {rec['folder']}")
        # every card carries the schema it was written under
        if not meta.get('schema_version'):
            add(rec, 1, "missing schema_version (run `asf schema-migrate`)")
        # duplicate id
        if iid in dupes:
            add(rec, find_line(rec, 'id'), f"duplicate id {iid} across folders")
        # parent
        type_ = meta.get('type')
        parent = meta.get('parent')
        if type_ in NO_PARENT_TYPES:
            if parent:
                add(rec, find_line(rec, 'parent'), f"{type_} must not have a parent")
        elif type_ in PARENT_TYPES:
            if not parent:
                exempt = (type_ == 'feature' and FEATURE_NO_PARENT_UNTIL
                          and today() < FEATURE_NO_PARENT_UNTIL)
                if not exempt:
                    add(rec, find_line(rec, 'id'), f"{type_} is missing a parent")
            elif parent not in canonical:
                add(rec, find_line(rec, 'parent'), f"parent {parent} does not exist")
            else:
                ptype = canonical[parent]['meta'].get('type')
                if ptype not in PARENT_TYPES[type_]:
                    add(rec, find_line(rec, 'parent'), f"parent {parent} has wrong type {ptype}")
        # a Bug carries its severity
        if type_ == 'bug' and not meta.get('severity'):
            add(rec, find_line(rec, 'id'), f"{iid}: bug without severity")
        # blockedBy
        for b in meta.get('blockedBy') or []:
            if isinstance(b, str) and ID_RE.match(b) and b not in canonical:
                add(rec, find_line(rec, 'blockedBy'), f"blockedBy references missing item {b}")
        # stories: (Task)
        for s in meta.get('stories') or []:
            if isinstance(s, str) and s not in canonical:
                add(rec, find_line(rec, 'stories'), f"stories references missing item {s}")
        # bare decision refs in body — Children/Backlinks are index-managed mirrors of a
        # child's own title, not new prose, so they're excluded the same way scan_text()
        # excludes them from backlink computation.
        preamble, sections = parse_sections(rec['body'])
        body_lines = rec['body'].split('\n')
        header_offset = len(rec['text'].split('\n')) - len(body_lines)
        current_heading = None
        in_fence = False
        for i, l in enumerate(body_lines):
            if l.lstrip().startswith(('```', '~~~')):
                in_fence = not in_fence  # a fenced block is quoted code (a test, a log), not prose
                continue
            if in_fence:
                continue
            if l.startswith('## '):
                current_heading = l.strip()
            if current_heading in ('## Children', '## Backlinks'):
                continue
            # quoted evidence (commit subjects, log lines) is not prose: skip `code spans` and "quoted text"
            scan_l = re.sub(r'`[^`]*`|"[^"]*"', '', l)
            for m in BARE_DECISION_RE.finditer(scan_l):
                # only a number the record has a D-card for is a link gone bare; any other D<n> is
                # the product's own register (a plan citing its docs), which ASF itself mints
                if f'D-{int(m.group(0)[1:]):04d}' not in canonical:
                    continue
                add(rec, header_offset + i + 1,
                    f"bare decision reference {m.group(0)!r}; write it as [[D-nnnn]]")
        # Children/Backlinks staleness
        exp_body = expected_body(rec, canonical, derived)
        if exp_body != rec['body']:
            _p, exp_sections = parse_sections(exp_body)
            for (heading, content), (_h2, cur_content) in zip(exp_sections, sections):
                h = heading.strip()
                if h in ('## Children', '## Backlinks') and content != cur_content:
                    line = None
                    for i, l in enumerate(rec['text'].split('\n')):
                        if l.strip() == h:
                            line = i + 1
                            break
                    add(rec, line or 1, f"{h} section is stale (run `asf index`)")

    check_deliveries(canonical, add, find_line)
    check_residue(canonical, add, find_line)

    # Size: an item whose History records a shape reading is held to that type's size (D6) —
    # an item never typed by shape (no `— shape:` line) is grandfathered and skipped.
    for iid, rec in canonical.items():
        if not is_open(rec):
            continue
        meta = rec['meta']
        _preamble, sections = parse_sections(rec['body'])
        section_by_heading = {heading.strip(): content for heading, content in sections}
        history = section_by_heading.get('## History', '')
        m = shape.SHAPE_LINE_RE.search(history)
        if not m:
            continue
        shape_type = m.group(3)
        if shape_type == 'bug':
            if not meta.get('signature'):
                add(rec, find_line(rec, 'id'),
                    f"{iid}: bug without a signature (a Bug is {shape.SIZE['bug']})")
        elif shape_type == 'task':
            if not meta.get('writes'):
                add(rec, find_line(rec, 'id'),
                    f"{iid}: task without writes: (a Task is {shape.SIZE['task']})")
        elif shape_type == 'story':
            acceptance = section_by_heading.get('## Acceptance', '')
            if not ACCEPTANCE_ITEM_RE.search(acceptance):
                add(rec, find_line(rec, 'id'),
                    f"{iid}: story without an acceptance list (a Story is {shape.SIZE['story']})")
        elif shape_type == 'epic':
            features = section_by_heading.get('## Features', '')
            feature_bullets = len(BULLET_RE.findall(features))
            feature_children = sum(
                1 for cid in derived[iid]['children']
                if canonical[cid]['meta'].get('type') == 'feature'
            )
            if feature_bullets < 2 and feature_children < 2:
                add(rec, find_line(rec, 'id'),
                    f"{iid}: epic spanning fewer than two Features (an Epic is {shape.SIZE['epic']})")
        elif shape_type == 'feature':
            if meta.get('writes') or meta.get('signature'):
                add(rec, find_line(rec, 'id'),
                    f"{iid}: feature carries writes:/signature: — that is a Task's/Bug's shape "
                    f"(a Feature is {shape.SIZE['feature']})")

    # Feature stage vs story coverage
    task_story_ids = set()
    for rec in canonical.values():
        if rec['meta'].get('type') == 'task' and not rec['meta'].get('removed'):
            for s in rec['meta'].get('stories') or []:
                task_story_ids.add(s)
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'feature':
            continue
        _typed, machine = frontmatter.split_machine(rec['meta'])
        stage = machine.get('stage', '')
        if not stage.startswith('building'):
            continue
        for cid in derived[iid]['children']:
            crec = canonical[cid]
            if crec['meta'].get('type') != 'story':
                continue
            if cid not in task_story_ids:
                add(rec, find_line(rec, 'id'), f"story {cid} has no Task listing it in stories:")

    # Active task writes: overlap
    active_tasks = [
        rec for rec in canonical.values()
        if rec['meta'].get('type') == 'task'
        and frontmatter.split_machine(rec['meta'])[1].get('state') == 'Active'
        and rec['meta'].get('writes')
    ]
    for i in range(len(active_tasks)):
        for j in range(i + 1, len(active_tasks)):
            t1, t2 = active_tasks[i], active_tasks[j]
            for g1 in t1['meta']['writes']:
                for g2 in t2['meta']['writes']:
                    if writes_intersect(g1, g2):
                        add(t1, find_line(t1, 'writes'),
                            f"writes: {g1!r} intersects Active task {t2['meta'].get('id')}'s {g2!r}")

    # index.json staleness
    index_path = os.path.join(root, 'index.json')
    expected_index = build_index_data(canonical, derived)
    if not os.path.isfile(index_path):
        findings.append(('index.json', 1, 'index.json is missing (run `asf index`)'))
    else:
        with open(index_path, encoding='utf-8') as f:
            try:
                on_disk = json.load(f)
            except json.JSONDecodeError:
                on_disk = None
        exp_items = expected_index['items']
        cur_items = (on_disk or {}).get('items', {})
        if on_disk is None or exp_items != cur_items:
            findings.append(('index.json', 1, 'index.json is stale (run `asf index`)'))

    if restrict is not None:
        findings = [f for f in findings
                    if f[0] in restrict or f[0] == 'index.json' or f[0] in layout_folders]

    findings.sort(key=lambda f: (f[0], f[1]))
    for path, line, msg in findings:
        print(f"{path}:{line}: {msg}")
    return 1 if findings else 0
