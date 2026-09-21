"""asf.tick.migrate — create the live item set from the product repo (``asf migrate``).

The one-time-then-idempotent adopter pass that creates the LIVE set of items from the product
repo and its goals file (``conventions.goals_file``; none configured → no Epics are migrated,
said in the report), then runs ingest and index. ``conventions.parity_goal`` names the GOAL
whose Epic parents parity Features (none → they are reported as Features with no Epic).
Re-running matches every candidate on
`legacy_id` (Features also try `links.spec`/`links.plan`) and never overwrites a typed field a
human may have changed — title, priority, rank, decided, blockedBy, body sections — once the
item file exists; only `links` and an empty `parent` are refreshed on a match. All git/gh access
goes through `evidence.migrate_sources()`, never through this module directly.
"""
import argparse
import os
import re
import sys

from asf import env
from asf.evidence import evidence
from asf.record import frontmatter
from asf.record.core import TYPE_ORDER, TYPES, BARE_DECISION_RE, canonicalize, load_items, now_iso, today
from asf.record.ingest import _path_only, cmd_ingest
from asf.schema import SCHEMA_VERSION

# Resolved per call from the product config unless a test overrides it directly
# (`mock.patch.object(migrate, 'GOALS_PATH', ...)`).
GOALS_PATH = None

CARD_PREFIX = 'CARD:'
ACRONYMS = {'EU'}


def _title_word(w):
    core = re.sub(r'[^A-Za-z]', '', w)
    if core and core.upper() in ACRONYMS and w.upper() == w:
        return w.upper()
    chars = list(w)
    seen = False
    for i, c in enumerate(chars):
        if c.isalpha():
            chars[i] = c.upper() if not seen else c.lower()
            seen = True
    return ''.join(chars)


def title_case(s):
    return ' '.join(_title_word(w) for w in s.split(' '))


def truncate(s, n):
    s = (s or '').strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(' ')
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip(',;: ')


def first_sentence(text):
    text = (text or '').strip()
    m = re.search(r'^(.*?[.!?])(\s|$)', text)
    return m.group(1).strip() if m else text


def first_paragraph(text):
    """The spec's first paragraph: skip the H1 heading line and blank lines, join to the next
    blank line."""
    if not text:
        return ''
    lines = text.split('\n')
    i = 1 if lines and lines[0].startswith('#') else 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    para = []
    while i < len(lines) and lines[i].strip():
        para.append(lines[i].strip())
        i += 1
    return ' '.join(para)


H1_TITLE_RE = re.compile(r'^#\s+[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*\s*[—–-]\s*(.+)$')


def doc_title(text, fallback_slug):
    if text:
        m = H1_TITLE_RE.match(text.split('\n', 1)[0].strip())
        if m:
            return m.group(1).strip()
    return title_case(fallback_slug.replace('-', ' '))


def linkify_decisions(text):
    """Every bare `Dnnn` in migrate-authored prose becomes `[[D-nnnn]]`; `check` refuses the bare
    form in a new body."""
    return BARE_DECISION_RE.sub(lambda m: f"[[D-{int(m.group(0)[1:]):04d}]]", text or '')


def _default_body(description=''):
    desc = linkify_decisions(description)
    head = f"## Description\n{desc}\n\n" if desc else "## Description\n\n"
    return (
        head +
        "## Acceptance\n- [ ] \n\n"
        "## Non-goals\n\n"
        "## History\n"
        f"- {today()}: created (migrate)\n\n"
        "## Children\n\n"
        "## Backlinks\n"
    )


def _decision_body(statement, context):
    return (
        f"## Statement\n{linkify_decisions(statement)}\n\n"
        f"## Context\n{linkify_decisions(context)}\n\n"
        "## History\n"
        f"- {today()}: created (migrate)\n\n"
        "## Children\n\n"
        "## Backlinks\n"
    )


# ---- the goals file: one GOAL block per Epic ---------------------------------------------

GOAL_TITLE_RE = re.compile(r'^GOAL\s+\d+\s*[—–-]+\s*([^.]+)\.\s*(.*)$', re.DOTALL)
BLOCKED_RE = re.compile(r'BLOCKED:\s*(.*?)(?:\s+RULES:|$)', re.DOTALL)
NAME_DASH_RE = re.compile(r'^([A-Z][A-Za-z]+)\s*[—-]\s*(.+)$')


def _format_blocker(item):
    item = item.strip().rstrip('.').strip()
    if not item or item in ('—', '-'):
        return None
    m = NAME_DASH_RE.match(item)
    return f"{m.group(1)}: {m.group(2)}" if m else item


def parse_blocked(joined_block):
    m = BLOCKED_RE.search(joined_block)
    if not m:
        return []
    raw = m.group(1).strip().rstrip('.').strip()
    if raw in ('—', '-', ''):
        return []
    return [b for b in (_format_blocker(p) for p in raw.split(';')) if b]


def parse_goal_block(num, joined):
    """One GOAL block's whitespace-joined text -> {num, raw, legacy_id, title, description,
    blockedBy}."""
    m = GOAL_TITLE_RE.match(joined)
    title_raw = m.group(1).strip() if m else ''
    rest = m.group(2).strip() if m else ''
    return {
        'num': num,
        'raw': joined,
        'legacy_id': f"GOAL {num}",
        'title': title_case(title_raw),
        'description': first_sentence(rest),
        'blockedBy': parse_blocked(joined),
    }


def parse_goals(text):
    """[{...}] in file order — the order the goals file writes them, not numeric order."""
    matches = list(evidence.GOAL_HEAD.finditer(text))
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        joined = ' '.join(text[start:end].split())
        out.append(parse_goal_block(m.group(1), joined))
    return out


# ---- plan tasks: title/stories/writes from a plan's own text -------------------------------

def task_title(rest):
    t = rest.strip()
    t = re.sub(r'^[:\-—–]+\s*', '', t)
    t = re.sub(r'\s*\bDONE\b\s*$', '', t, flags=re.IGNORECASE)
    return t.strip()


def plan_task_records(plan_text):
    """[{tid, title, body}] per plan task heading, in document order — the same headings
    `evidence.plan_tasks()` reads for branch dispatch, here read for content instead."""
    matches = list(evidence.TASK_HEAD.finditer(plan_text))
    seen = set()
    out = []
    for i, m in enumerate(matches):
        tid = "T" + m.group(1).lower()
        if tid in seen:
            continue
        seen.add(tid)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plan_text)
        out.append({'tid': tid, 'title': task_title(m.group('rest')), 'body': plan_text[start:end]})
    out.sort(key=lambda r: evidence.natural_key(r['tid']))
    return out


WRITES_RE = re.compile(r'^\s*(?:Files|Writes)\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
STORY_TOKEN_RE = re.compile(r'\bF-[A-Z]+-\d+\b')


def writes_lines(body):
    m = WRITES_RE.search(body)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.startswith('[') and raw.endswith(']'):
        raw = raw[1:-1]
    parts = [p.strip().strip('`\'"') for p in raw.split(',')]
    return [p for p in parts if p]


# ---- decisions: one `| Dnnn | question | verdict |` table row per ruling -------------------

D_ROW_RE = re.compile(r'^\|\s*D(\d+)\s*\|')
VERDICT_MARK_RE = re.compile(r'\*\*(?:Decided|Deferred|Amended)\b')
DATE_IN_VERDICT_RE = re.compile(r'\*\*(?:Decided|Deferred|Amended)[^)]*?(\d{4}-\d{2}-\d{2})')
STATUS_PREFIX_RE = re.compile(r'^(?:Decided|Deferred|Amended)\s*\([^)]*\):\s*')
UNCLOSED_PREFIX_RE = re.compile(r'^(?:Decided|Deferred|Amended)\s*\(')
SUPERSEDE_RE = re.compile(r'(?:supersed\w*|amend\w*|invert\w*|revers\w*)[^.]{0,60}?\bD(\d+)',
                          re.IGNORECASE)


def parse_decision_row(line):
    """One `| Dnnn | question | verdict-cell |` row -> a decision dict, or None."""
    m = D_ROW_RE.match(line.strip())
    if not m:
        return None
    c = evidence.cells(line)
    if len(c) < 3:
        return None
    num = m.group(1)
    verdict_cell = c[2].strip()

    date_m = DATE_IN_VERDICT_RE.search(verdict_cell)
    date = date_m.group(1) if date_m else None

    bold_m = re.search(r'\*\*(.+?)\*\*', verdict_cell, re.DOTALL)
    statement = STATUS_PREFIX_RE.sub('', bold_m.group(1)).strip() if bold_m else ''
    if not statement or UNCLOSED_PREFIX_RE.match(statement):
        after = verdict_cell[bold_m.end():].strip() if bold_m else verdict_cell
        statement = first_sentence(after)

    own = f"D-{int(num):04d}"
    supersedes = sorted({f"D-{int(n):04d}" for n in SUPERSEDE_RE.findall(verdict_cell)} - {own})

    context_parts = []
    if bold_m:
        if bold_m.start() > 0:
            context_parts.append(verdict_cell[:bold_m.start()].strip())
        tail = verdict_cell[bold_m.end():].strip()
        if statement and tail.startswith(statement):
            tail = tail[len(statement):].strip()
        if tail:
            context_parts.append(tail)
    else:
        context_parts.append(verdict_cell)

    return {
        'legacy_id': f"D{num}",
        'id_num': int(num),
        'title': truncate(c[1], 100),
        'date': date,
        'statement': statement,
        'context': ' '.join(p for p in context_parts if p),
        'supersedes': supersedes,
    }


def find_decision_rows(text):
    """Every genuine ruling row in a document's text, filtered by requiring a bold
    Decided/Deferred/Amended marker so a traceability table whose row label merely starts with
    `Dnnn` is never mistaken for a ruling."""
    out = []
    if not text:
        return out
    for line in text.splitlines():
        if not D_ROW_RE.match(line.strip()):
            continue
        c = evidence.cells(line)
        if len(c) < 3 or not VERDICT_MARK_RE.search(c[2]):
            continue
        row = parse_decision_row(line)
        if row:
            out.append(row)
    return out


# ---- hotfix/ci-diag reports -> Bugs ----------------------------------------------------------

def hotfix_bug_fields(name, text):
    """(severity, found_in, signature) from a hotfix/ci-diag report's name and text.

    The filename convention is the more reliable signal — a `ci-diag-*` report can describe a
    prod-facing symptom in its body while still being a CI diagnostic, not a prod incident —
    so it's checked before scanning the body text for "prod ... red/incident/down".
    """
    lower = (text or '').lower()
    if name.startswith('ci-diag'):
        severity, found_in = 'S2', 'ci'
    elif 'prod' in lower and re.search(r'\bred\b|\bincident\b|\bdown\b|\boutage\b', lower):
        severity, found_in = 'S1', 'prod'
    elif re.search(r'\bci\.yml\b|\bworkflow\b', lower):
        severity, found_in = 'S2', 'ci'
    else:
        severity, found_in = 'S3', 'dev'
    signature = None
    for line in (text or '').splitlines():
        if line.strip() and re.search(r'\b(error|fail(?:ed|ing)?)\b', line, re.IGNORECASE):
            signature = line.strip()[:200]
            break
    return severity, found_in, signature


def find_feature_by_links(canonical, spec_path, plan_path):
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'feature':
            continue
        links = rec['meta'].get('links') or {}
        if spec_path and links.get('spec') == spec_path:
            return iid, rec
        if plan_path and links.get('plan') == plan_path:
            return iid, rec
    return None, None


def cmd_migrate(args, root):
    dry_run = getattr(args, 'dry_run', False)
    ev = evidence.load(fresh=getattr(args, 'fresh', False))
    src = evidence.migrate_sources()

    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)

    legacy_lookup = {}
    for iid, rec in canonical.items():
        lg = rec['meta'].get('legacy_id')
        if lg:
            legacy_lookup[(rec['meta'].get('type'), lg)] = iid

    next_num = {}
    for folder, prefix in TYPES.values():
        n = 0
        d = os.path.join(root, folder)
        if os.path.isdir(d):
            for name in os.listdir(d):
                m = re.match(rf'^{prefix}-(\d+)\.md$', name)
                if m:
                    n = max(n, int(m.group(1)))
        next_num[prefix] = n

    report = {t: {'created': 0, 'matched': 0, 'skipped': 0} for t in TYPES}
    unmatched = []
    no_epic = []
    plan_not_approved = []

    def write_record(type_, new_id, meta, body, folder):
        text = frontmatter.render(meta, body)
        path = os.path.join(root, folder, f"{new_id}.md")
        if not dry_run:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        canonical[new_id] = {'meta': meta, 'body': body, 'path': path,
                             'relpath': os.path.relpath(path, root), 'folder': folder,
                             'name': f"{new_id}.md", 'text': text}

    def upsert(type_, legacy_id, typed_fields, body, extra_match=None):
        iid = legacy_lookup.get((type_, legacy_id))
        rec = canonical.get(iid) if iid else None
        if rec is None and extra_match:
            iid, rec = extra_match()

        if rec is None:
            folder, prefix = TYPES[type_]
            next_num[prefix] += 1
            new_id = f"{prefix}-{next_num[prefix]:04d}"
            meta = frontmatter.FrontmatterDict()
            meta['id'] = new_id
            meta['type'] = type_
            for k, v in typed_fields.items():
                if k == 'links':
                    meta[k] = v or {}
                elif v not in (None, [], {}):
                    meta[k] = v
            # `legacy_id` is the match key every call passes in separately from typed_fields;
            # guarantee it lands in the file regardless of whether a call site also duplicated
            # it into typed_fields, so a second `migrate` run can always find this record again.
            if 'legacy_id' not in meta:
                meta['legacy_id'] = legacy_id
            ts = now_iso()
            meta['schema_version'] = SCHEMA_VERSION
            meta['state'] = 'New'
            meta['stage_since'] = ts
            meta['updated'] = ts
            meta.machine_keys = {'schema_version', 'state', 'stage_since', 'updated'}
            write_record(type_, new_id, meta, body, folder)
            legacy_lookup[(type_, legacy_id)] = new_id
            report[type_]['created'] += 1
            return new_id

        changed = False
        new_links = typed_fields.get('links')
        if new_links is not None:
            # Merge rather than overwrite: two evidence slugs can share one legacy_id, and
            # blindly replacing `links` on every match would make whichever slug sorts last the
            # sole survivor, churning on every run and losing the other slugs' branches/PRs.
            merged_links = dict(rec['meta'].get('links') or {})
            for k, v in new_links.items():
                if k in ('branches', 'prs'):
                    combined = sorted(set(merged_links.get(k) or []) | set(v or []))
                    if combined != merged_links.get(k):
                        merged_links[k] = combined
                elif k not in merged_links and v:
                    merged_links[k] = v
            if merged_links != (rec['meta'].get('links') or {}):
                rec['meta']['links'] = merged_links
                changed = True
        new_parent = typed_fields.get('parent')
        if new_parent and not rec['meta'].get('parent'):
            rec['meta']['parent'] = new_parent
            changed = True
        if changed:
            new_text = frontmatter.render(rec['meta'], rec['body'])
            if not dry_run:
                with open(rec['path'], 'w', encoding='utf-8') as f:
                    f.write(new_text)
            rec['text'] = new_text
            report[type_]['matched'] += 1
        else:
            report[type_]['skipped'] += 1
        return iid

    def upsert_decision(row):
        new_id = f"D-{row['id_num']:04d}"
        if new_id in canonical:
            report['decision']['skipped'] += 1
            return new_id
        meta = frontmatter.FrontmatterDict()
        meta['id'] = new_id
        meta['type'] = 'decision'
        meta['title'] = row['title']
        if row['date']:
            meta['date'] = row['date']
        meta['legacy_id'] = row['legacy_id']
        if row['supersedes']:
            meta['supersedes'] = row['supersedes']
        ts = now_iso()
        meta['schema_version'] = SCHEMA_VERSION
        meta['state'] = 'New'
        meta['stage_since'] = ts
        meta['updated'] = ts
        meta.machine_keys = {'schema_version', 'state', 'stage_since', 'updated'}
        body = _decision_body(row['statement'], row['context'])
        write_record('decision', new_id, meta, body, TYPES['decision'][0])
        report['decision']['created'] += 1
        return new_id

    # ---- 1. Epics from the goals file (conventions.goals_file) ----------------------------
    conventions = _conventions(args)
    goals_path = GOALS_PATH or goals_file_path(args, conventions)
    goals_text = ''
    if goals_path and os.path.isfile(goals_path):
        with open(goals_path, encoding='utf-8') as f:
            goals_text = f.read()
    else:
        unmatched.append(('epic', goals_path or '(conventions.goals_file unset)',
                          'no goals file — no Epics migrated'))
    goals = parse_goals(goals_text)
    epic_id_by_num = {}
    goal_raw = {g['num']: g['raw'] for g in goals}
    for rank, g in enumerate(goals, start=1):
        typed = {'title': g['title'], 'priority': 'need', 'decided': True, 'rank': rank}
        if g['blockedBy']:
            typed['blockedBy'] = g['blockedBy']
        epic_id_by_num[g['num']] = upsert('epic', g['legacy_id'], typed,
                                          _default_body(g['description']))

    def epic_for_goal(num):
        # the goals file can be live-edited outside this run; an Epic already on disk from an
        # earlier GOAL block stays reachable by its `legacy_id` even on a run where the file no
        # longer mentions that GOAL number.
        return epic_id_by_num.get(num) or legacy_lookup.get(('epic', f'GOAL {num}'))

    # the Epic that parents parity Features and Features cited but placed under no GOAL
    parity_goal = conventions.get('parity_goal')
    parity_epic = epic_for_goal(str(parity_goal)) if parity_goal is not None else None

    def epic_mentioning(token):
        if not token:
            return None
        pat = re.compile(r'(?<![A-Za-z0-9])' + re.escape(token) + r'(?![A-Za-z0-9])')
        for num, raw in goal_raw.items():
            m = pat.search(raw)
            if m:
                return epic_id_by_num[num], m.start()
        return None

    # ---- 2. Features ← discover()["features"] --------------------------------------------
    doc_refs = [r for fev in ev['features'].values() for r in (fev.get('spec'), fev.get('plan'))
               if r]
    doc_texts = evidence.read_refs(doc_refs) if doc_refs else {}

    hits = {}
    for slug, fev in ev['features'].items():
        hit = epic_mentioning(fev.get('alias')) or epic_mentioning(slug)
        if hit:
            hits[slug] = hit
    rank_of = {}
    by_parent = {}
    for slug, (parent, offset) in hits.items():
        by_parent.setdefault(parent, []).append((offset, slug))
    for parent, lst in by_parent.items():
        for i, (_off, slug) in enumerate(sorted(lst), start=1):
            rank_of[slug] = i

    def features_citing(story_id):
        pat = re.compile(r'\b' + re.escape(story_id) + r'\b')
        for slug in sorted(ev['features']):
            fev = ev['features'][slug]
            text = doc_texts.get(fev.get('spec'))
            if text and pat.search(text):
                return slug
        return None

    story_feature_slug = {sid: features_citing(sid) for sid in ev['stories']}
    cited_slugs = {slug for slug in story_feature_slug.values() if slug}

    feature_id = {}
    feature_legacy_id = {}
    feature_plan_text = {}
    for slug in sorted(ev['features']):
        fev = ev['features'][slug]
        alias = fev.get('alias')
        legacy_id = alias or slug
        spec_ref, plan_ref = fev.get('spec'), fev.get('plan')
        spec_text, plan_text = doc_texts.get(spec_ref), doc_texts.get(plan_ref)
        feature_plan_text[slug] = plan_text

        parent, _off = hits.get(slug, (None, None))
        rank = rank_of.get(slug)
        if parent is None and slug in cited_slugs:
            parent = parity_epic

        spec_path, plan_path = _path_only(spec_ref), _path_only(plan_ref)
        links = {}
        if spec_path:
            links['spec'] = spec_path
        if plan_path:
            links['plan'] = plan_path
        branches = sorted({b for b in (fev.get('spec_branch'), fev.get('plan_branch')) if b})
        if branches:
            links['branches'] = branches
        if fev.get('prs'):
            links['prs'] = sorted(fev['prs'])

        typed = {'title': doc_title(spec_text or plan_text, slug), 'legacy_id': legacy_id,
                 'decided': bool(spec_path), 'links': links}
        if parent:
            typed['parent'] = parent
        if rank is not None:
            typed['rank'] = rank
        fid = upsert('feature', legacy_id, typed, _default_body(first_paragraph(spec_text)),
                    extra_match=lambda sp=spec_path, pp=plan_path: find_feature_by_links(canonical, sp, pp))
        feature_id[slug] = fid
        feature_legacy_id[slug] = legacy_id
        if not parent:
            no_epic.append(slug)

    # ---- 3. Stories ← discover()["stories"] -----------------------------------------------
    parity_area_feature = {}

    def parity_feature_for(area):
        area = area or 'Unspecified'
        if area not in parity_area_feature:
            legacy_id = CARD_PREFIX + 'parity-' + re.sub(r'[^a-z0-9]+', '-', area.lower()).strip('-')
            typed = {'title': f"Parity — {area}", 'priority': 'need', 'decided': True,
                     'parent': parity_epic, 'legacy_id': legacy_id, 'links': {}}
            body = _default_body(f"Stories in {area} with no Feature of their own.")
            parity_area_feature[area] = upsert('feature', legacy_id, typed, body)
        return parity_area_feature[area]

    story_id = {}
    for sid in sorted(ev['stories']):
        sev = ev['stories'][sid]
        milestone = sev.get('milestone') or ''
        ms_token = milestone.split(' ')[0] if milestone else ''
        ms_pat = re.compile(r'(?<![A-Za-z0-9])' + re.escape(ms_token) + r'(?![A-Za-z0-9])') \
            if ms_token else None
        decided = sev.get('status') in ('done', 'doing') or bool(
            ms_pat and any(ms_pat.search(raw) for raw in goal_raw.values()))

        parent_slug = story_feature_slug.get(sid)
        parent = feature_id.get(parent_slug) if parent_slug else None
        if parent is None:
            parent = parity_feature_for(sev.get('area'))

        links = {}
        if sev.get('impl'):
            links['impl'] = sev['impl']
        if sev.get('test'):
            links['test'] = sev['test']
        typed = {'title': truncate(first_sentence(sev.get('cap')), 120), 'area': sev.get('area'),
                 'priority': 'need', 'decided': decided, 'parent': parent, 'links': links}
        story_id[sid] = upsert('story', sid, typed, _default_body(''))

    # ---- 4. Tasks ← every approved plan ----------------------------------------------------
    for slug in sorted(ev['features']):
        fev = ev['features'][slug]
        plan_text = feature_plan_text.get(slug)
        if not plan_text:
            continue
        plan_review = fev.get('plan_review')
        if not (plan_review and plan_review[1] == 'APPROVED'):
            plan_not_approved.append(slug)
            continue
        legacy_id = feature_legacy_id[slug]
        fid = feature_id[slug]
        plan_path = _path_only(fev.get('plan'))
        for t in plan_task_records(plan_text):
            tid, title, tbody = t['tid'], t['title'], t['body']
            task_legacy = f"{legacy_id}/{tid}"
            mentioned = sorted({m for m in STORY_TOKEN_RE.findall(tbody) if m in ev['stories']})
            stories_field = [story_id[m] for m in mentioned if m in story_id]
            parent = story_id[mentioned[0]] if len(mentioned) == 1 and mentioned[0] in story_id \
                else fid
            tev = fev['tasks'].get(tid, {})
            links = {}
            if tev.get('branch'):
                links['branches'] = [tev['branch']]
            if tev.get('pr'):
                links['prs'] = [tev['pr']]
            if plan_path:
                links['plan'] = f"{plan_path}#task-{tid[1:].lower()}"
            typed = {'title': title, 'parent': parent, 'decided': True, 'links': links}
            if stories_field:
                typed['stories'] = stories_field
            writes = writes_lines(tbody)
            if writes:
                typed['writes'] = writes
            upsert('task', task_legacy, typed, _default_body(''))

    # ---- 5. Bugs ← hotfix/ci-diag reports on open work branches -----------------------------
    default_bug_epic = conventions.get('default_bug_epic')

    def bug_parent(text):
        for slug, legacy in feature_legacy_id.items():
            if re.search(r'(?<![A-Za-z0-9])' + re.escape(legacy) + r'(?![A-Za-z0-9])', text or ''):
                return feature_id[slug]
        return default_bug_epic

    for ref in sorted(src['hotfix_texts']):
        name = os.path.basename(ref)
        stem = name[:-3]
        if ref.startswith('origin/main:'):
            unmatched.append(('bug', stem, 'already on origin/main — merged'))
            continue
        text = src['hotfix_texts'][ref] or ''
        severity, found_in, signature = hotfix_bug_fields(name, text)
        typed = {'title': truncate(first_sentence(first_paragraph(text)) or stem, 120),
                 'severity': severity, 'found_in': found_in, 'decided': True,
                 'parent': bug_parent(text)}
        if signature:
            typed['signature'] = signature
        upsert('bug', stem, typed, _default_body(first_paragraph(text)))

    # ---- 6. Decisions -----------------------------------------------------------------------
    design_spec_name = conventions.get('design_spec_name')
    seen_nums = set()
    sources = [src.get('design_spec_text')]
    sources += [t for n, t in sorted(src.get('main_spec_texts', {}).items())
               if n != design_spec_name]
    sources.append(src.get('sdd_decisions_text'))
    for text in sources:
        for row in find_decision_rows(text):
            if row['legacy_id'] in seen_nums:
                continue
            seen_nums.add(row['legacy_id'])
            upsert_decision(row)

    # ---- report --------------------------------------------------------------------------
    lines = [f"{'type':<10} {'created':>7} {'matched':>7} {'skipped':>7}"]
    for t in TYPE_ORDER:
        r = report[t]
        if r['created'] or r['matched'] or r['skipped']:
            lines.append(f"{t:<10} {r['created']:>7} {r['matched']:>7} {r['skipped']:>7}")
    if unmatched:
        lines.append("")
        lines.append(f"unmatched ({len(unmatched)}):")
        lines.extend(f"  {t} {ref}: {reason}" for t, ref, reason in unmatched)
    if no_epic:
        lines.append("")
        lines.append(f"Features with no Epic ({len(no_epic)}):")
        lines.extend(f"  {slug}" for slug in no_epic)
    if plan_not_approved:
        lines.append("")
        lines.append(f"Tasks skipped — plan not approved ({len(plan_not_approved)} features):")
        lines.extend(f"  {slug}" for slug in plan_not_approved)
    table = '\n'.join(lines)
    print(table)

    if dry_run:
        return 0
    return cmd_ingest(argparse.Namespace(fresh=False), root)


def _product(args):
    try:
        return env.load_product(getattr(args, 'product', None))
    except env.ConfigError:
        return None


def _conventions(args):
    """The product's ``conventions`` (``{}`` when there is no product config)."""
    product = _product(args)
    return (product.conventions if product is not None else None) or {}


def goals_file_path(args, conventions):
    """``conventions.goals_file`` under the product repo (an absolute path is kept); None when
    the product keeps no goals file — the goals are the adopter's own, never a name this module
    knows."""
    name = conventions.get('goals_file')
    product = _product(args)
    if not name:
        return None
    name = os.path.expanduser(str(name))
    if os.path.isabs(name) or product is None or not product.repo_dir:
        return name
    return os.path.join(product.repo_dir, name)
