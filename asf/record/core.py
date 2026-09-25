"""asf.record.core — the shared item model: loading, canonicalizing, sections, derived state.

Split out of the original monolithic ``backlog.py`` so ``new``/``check``/``index``/``ingest``
(asf.record), and ``migrate``/``stale``/``file-bugs`` (asf.tick) and ``groom`` (asf.groom) can
share one definition of an item without importing each other's command modules.
"""
import datetime
import fnmatch
import json
import os
import re

from asf.record import frontmatter

TYPES = {
    'epic': ('epics', 'E'),
    'feature': ('features', 'F'),
    'story': ('stories', 'S'),
    'task': ('tasks', 'T'),
    'bug': ('bugs', 'B'),
    'decision': ('decisions', 'D'),
    'rule': ('rules', 'R'),
}
FOLDER_TO_TYPE = {v[0]: k for k, v in TYPES.items()}
ITEM_FOLDERS = [f for f, _ in TYPES.values()]
TYPE_ORDER = list(TYPES.keys())

NO_PARENT_TYPES = {'epic', 'decision', 'rule'}
PARENT_TYPES = {
    'feature': {'epic'},
    'story': {'feature'},
    'task': {'feature', 'story'},
    'bug': {'feature', 'story', 'epic'},
}

STOPWORDS = {
    'a', 'an', 'the', 'of', 'to', 'for', 'and', 'or', 'in', 'on', 'with',
    'is', 'are', 'this', 'that', 'by', 'as', 'at', 'from', 'be', 'it', 'its',
}

ID_RE = re.compile(r'^[A-Z]-\d{4}$')
# not PF-D18 (a letter-hyphen before D is another id's prefix); D607-D616 is still a range of two
BARE_DECISION_RE = re.compile(r'(?<![A-Za-z]-)(?<!\w)D\d{1,3}\b')


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')


def tokenize(title):
    words = re.findall(r'[a-z0-9]+', (title or '').lower())
    return {w for w in words if w not in STOPWORDS}


def jaccard(a, b):
    if not a and not b:
        return 0.0
    u = a | b
    if not u:
        return 0.0
    return len(a & b) / len(u)


def rel_link(folder, iid):
    return f"../{folder}/{iid}.md"


# ---------------------------------------------------------------- loading --

def load_items(root, folders=None):
    """Scan item folders. Returns (by_id: {id: [record,...]}, parse_errors)."""
    folders = folders or ITEM_FOLDERS
    by_id = {}
    errors = []
    for folder in folders:
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith('.md'):
                continue
            path = os.path.join(d, name)
            relpath = os.path.relpath(path, root)
            with open(path, encoding='utf-8') as f:
                text = f.read()
            try:
                meta, body = frontmatter.parse(text, path=relpath)
            except frontmatter.FrontmatterError as e:
                errors.append((e.file, e.line, e.why))
                continue
            rec = {
                'meta': meta, 'body': body, 'path': path, 'relpath': relpath,
                'folder': folder, 'name': name, 'text': text,
            }
            by_id.setdefault(meta.get('id'), []).append(rec)
    return by_id, errors


def canonicalize(by_id):
    """Pick one record per id (first found); return (canonical, dupe_ids)."""
    canonical = {}
    dupes = []
    for iid, records in by_id.items():
        if iid is None:
            continue
        if len(records) > 1:
            dupes.append(iid)
        canonical[iid] = records[0]
    return canonical, dupes


def writes_intersect(a, b):
    """True when two ``writes:`` globs name a common path by ``asf check``'s test: equal, or
    either matching the other. The one definition — ``asf check`` refuses two Active Tasks whose
    ``writes:`` intersect by it, and the feeder's footprint gate and the widening rule build on it."""
    return a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def is_open(rec):
    typed, machine = frontmatter.split_machine(rec['meta'])
    if typed.get('removed'):
        return False
    return machine.get('state', 'New') != 'Closed'


# --------------------------------------------------------------- sections --

def parse_sections(body):
    """Split body into (preamble, [[heading, content], ...]); rejoins exactly."""
    parts = re.split(r'(?m)^(## .+)$', body)
    preamble = parts[0]
    sections = []
    i = 1
    while i < len(parts):
        heading = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ''
        sections.append([heading, content])
        i += 2
    return preamble, sections


def render_sections(preamble, sections):
    out = preamble
    for heading, content in sections:
        out += heading + content
    return out


def section_content(lines, is_last):
    text = '\n' + ''.join(f"{l}\n" for l in lines)
    if not is_last:
        text += '\n'
    return text


# --------------------------------------------------------- derived (index) --

def _flatten_strings(value, out):
    if isinstance(value, dict):
        for v in value.values():
            _flatten_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _flatten_strings(v, out)
    else:
        out.append(str(value))


def scan_text(rec):
    typed, machine = frontmatter.split_machine(rec['meta'])
    parts = []
    _flatten_strings(typed, parts)
    _flatten_strings(machine, parts)
    preamble, sections = parse_sections(rec['body'])
    body_text = preamble
    for heading, content in sections:
        if heading.strip() in ('## Children', '## Backlinks'):
            continue
        body_text += heading + content
    parts.append(body_text)
    return '\n'.join(parts)


MENTION_ID_RE = re.compile(r'[A-Z]-[0-9]{4}')
MENTION_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9])[A-Z]-[0-9]{4}(?![A-Za-z0-9])')


def mention_re(iid):
    return re.compile(r'(?<![A-Za-z0-9])' + re.escape(iid) + r'(?![A-Za-z0-9])')


def sort_key(canonical, iid):
    t = canonical[iid]['meta'].get('type')
    order = TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER)
    return (order, iid)


def compute_derived(canonical):
    """Return {id: {'children': [ids], 'backlinks': [ids]}}."""
    children_map = {iid: [] for iid in canonical}
    for iid, rec in canonical.items():
        parent = rec['meta'].get('parent')
        if parent in canonical:
            children_map[parent].append(iid)
    for iid in children_map:
        children_map[iid].sort(key=lambda cid: sort_key(canonical, cid))

    texts = {iid: scan_text(rec) for iid, rec in canonical.items()}
    # one scan per text, not one regex per (id, text) pair: the pairwise search was quadratic
    # in the item count and was most of the record step's time. For an id of the ``X-0000``
    # shape the token set is exact (such tokens cannot overlap, so findall sees every one);
    # any other id keeps its own regex.
    tokens = {oid: set(MENTION_TOKEN_RE.findall(text)) for oid, text in texts.items()}
    derived = {}
    for iid in canonical:
        children = children_map.get(iid, [])
        excluded = set(children) | {iid}
        if isinstance(iid, str) and MENTION_ID_RE.fullmatch(iid):
            backlinks = [oid for oid in canonical if oid not in excluded and iid in tokens[oid]]
        else:
            regex = mention_re(iid)
            backlinks = [
                oid for oid in canonical
                if oid not in excluded and regex.search(texts[oid])
            ]
        backlinks.sort(key=lambda bid: sort_key(canonical, bid))
        derived[iid] = {'children': children, 'backlinks': backlinks}
    return derived


def _plain(title):
    return title


def is_retired(meta):
    """A card with ``removed:`` or ``moved_to:``: no stage, no Tasks, no session — and no derived
    Children or Backlinks sections."""
    return bool((meta or {}).get('removed') or (meta or {}).get('moved_to'))


def record_root(rec):
    """The record root a loaded card lives under (its path less its record-relative path)."""
    path, rel = rec.get('path') or '', rec.get('relpath') or ''
    return path[:-len(rel)].rstrip(os.sep) if rel and path.endswith(rel) else None


def title_scrub(root=None):
    """``title -> text``: a title as derived text may carry it — every protected name and secret
    the redaction gate refuses replaced by a neutral token (:func:`asf.redact.scrub`)."""
    from asf import redact
    pats = redact.default_patterns(root)
    if not pats:
        return _plain
    return lambda title: redact.scrub(title, pats)


def children_lines(canonical, ids, scrub=_plain):
    lines = []
    for cid in ids:
        crec = canonical[cid]
        title = scrub(crec['meta'].get('title', ''))
        _typed, machine = frontmatter.split_machine(crec['meta'])
        state = machine.get('state', 'New')
        lines.append(f"- [{cid}]({rel_link(crec['folder'], cid)}) {title} — {state}")
    return lines


def backlinks_lines(canonical, ids, scrub=_plain):
    lines = []
    for bid in ids:
        brec = canonical[bid]
        title = scrub(brec['meta'].get('title', ''))
        lines.append(f"- [{bid}]({rel_link(brec['folder'], bid)}) {title}")
    return lines


def expected_body(rec, canonical, derived, scrub=None):
    """The card's body with its derived sections (``## Children``, ``## Backlinks``) as the
    record derives them: every title passed through the redaction scrub (``scrub``, else
    :func:`title_scrub` for the card's record), and both sections empty on a removed or moved
    card."""
    iid = rec['meta'].get('id')
    d = derived.get(iid, {'children': [], 'backlinks': []})
    if is_retired(rec['meta']):
        d = {'children': [], 'backlinks': []}
    if scrub is None:
        scrub = title_scrub(record_root(rec))
    clines = children_lines(canonical, d['children'], scrub)
    blines = backlinks_lines(canonical, d['backlinks'], scrub)
    preamble, sections = parse_sections(rec['body'])
    new_sections = []
    n = len(sections)
    for idx, (heading, content) in enumerate(sections):
        is_last = idx == n - 1
        h = heading.strip()
        if h == '## Children':
            content = section_content(clines, is_last)
        elif h == '## Backlinks':
            content = section_content(blines, is_last)
        new_sections.append([heading, content])
    return render_sections(preamble, new_sections)


def build_index_data(canonical, derived):
    items = {}
    for iid, rec in canonical.items():
        typed, machine = frontmatter.split_machine(rec['meta'])
        entry = dict(typed)
        entry.update(machine)
        entry['folder'] = rec['folder']
        entry['children'] = list(derived[iid]['children'])
        entry['backlinks'] = list(derived[iid]['backlinks'])
        items[iid] = entry
    return {'generated': now_iso(), 'items': items}


def render_index_json(data):
    return json.dumps(data, indent=2, sort_keys=True) + '\n'
