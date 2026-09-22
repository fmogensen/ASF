"""asf.record.check — validate the backlog (``asf check``)."""
import fnmatch
import json
import os
import re

from asf.record import frontmatter
from asf.record.core import (
    BARE_DECISION_RE, FOLDER_TO_TYPE, ID_RE, NO_PARENT_TYPES, PARENT_TYPES, build_index_data,
    canonicalize, compute_derived, expected_body, load_items, parse_sections, today,
)

# A Feature normally requires a parent Epic, but `migrate` may leave one parentless when the
# adopted source names no Epic for it and it carries no Story of its own to infer one from. Set
# per product via a real expiry date once adopted; None means the exception never applies.
FEATURE_NO_PARENT_UNTIL = None


def cmd_check(args, root):
    paths = args.paths or None
    restrict = None
    if paths:
        restrict = {os.path.relpath(os.path.abspath(p), root) for p in paths}

    by_id, parse_errors = load_items(root)
    canonical, dupes = canonicalize(by_id)
    derived = compute_derived(canonical)

    findings = []  # (relpath, line, message)

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
        for i, l in enumerate(body_lines):
            if l.startswith('## '):
                current_heading = l.strip()
            if current_heading in ('## Children', '## Backlinks'):
                continue
            # quoted evidence (commit subjects, log lines) is not prose: skip `code spans` and "quoted text"
            scan_l = re.sub(r'`[^`]*`|"[^"]*"', '', l)
            for m in BARE_DECISION_RE.finditer(scan_l):
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

    # Feature stage vs story coverage
    task_story_ids = set()
    for rec in canonical.values():
        if rec['meta'].get('type') == 'task':
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
                    if g1 == g2 or fnmatch.fnmatch(g1, g2) or fnmatch.fnmatch(g2, g1):
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
        findings = [f for f in findings if f[0] in restrict or f[0] == 'index.json']

    findings.sort(key=lambda f: (f[0], f[1]))
    for path, line, msg in findings:
        print(f"{path}:{line}: {msg}")
    return 1 if findings else 0
