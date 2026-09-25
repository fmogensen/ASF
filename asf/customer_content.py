"""asf.customer_content — text a customer reads never carries an internal note.

A product names the pages a customer reads and what must never reach them::

    conventions:
      customer_content:
        paths: [apps/site/app/legal/**, apps/site/content/**]
        forbidden_markers: ['(?i)\\[\\s*legal\\s*:']   # unset: DEFAULT_FORBIDDEN_MARKERS

Three places read it:

* **the landing gate** (:mod:`asf.harvest.lane`) refuses a branch that *adds* a line matching a
  marker under ``paths`` — before review (PUSHED) and again before the gate — and hands it back
  with every ``file:line`` in the hold (:func:`added_hits`);
* **the deploy pass** (:mod:`asf.harvest.deploy`) refuses to dispatch any environment whose
  candidate sha's *tree* carries a marker under ``paths`` — one loud line (:func:`tree_hits`);
* **the review** of a diff touching ``paths`` carries a required ``read as the customer``
  section in its brief (:func:`review_section`), and a review of such a diff whose check table
  has no ``read as the customer`` row is incomplete: the lane asks for another round
  (:func:`has_customer_row`).

``asf doctor`` names a product that deploys but has no ``customer_content`` (:func:`findings`).
"""
import fnmatch
import re
import subprocess

from asf.conventions import DEFAULT_CUSTOMER_CONTENT_PATHS, DEFAULT_FORBIDDEN_MARKERS

#: The correction kind a branch the marker gate refuses goes back with.
KIND = 'customer-content'
#: The first cell of the review's required check row (matched case-insensitively, as a prefix).
CHECK_ROW = 'read as the customer'
#: The most hits a hold or a refusal line names; the rest are counted.
SHOW_HITS = 10
#: The largest file the tree scan reads (bytes); a bigger one is an asset, not a page.
MAX_FILE_BYTES = 2_000_000

_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
_ROW_RE = re.compile(r'^\s*\|\s*\**\s*read as the customer\b[^|]*\|\s*[*`_]*\s*(pass|fail)\b',
                     re.I | re.M)


def _block(conv):
    v = (conv.get('customer_content') if conv is not None and hasattr(conv, 'get') else None)
    return v if isinstance(v, dict) else {}


def configured(conv):
    """True when the product wrote a ``customer_content`` block at all."""
    return conv is not None and hasattr(conv, 'get') and conv.get('customer_content') is not None


def paths(conv):
    """The ``customer_content.paths`` globs ([] = nothing is customer content)."""
    v = _block(conv).get('paths', DEFAULT_CUSTOMER_CONTENT_PATHS)
    if isinstance(v, str):
        v = [v]
    return [p.strip() for p in v or () if isinstance(p, str) and p.strip()]


def markers(conv):
    """The compiled ``customer_content.forbidden_markers`` (the defaults when unset); a pattern
    that does not compile is left out here — ``asf doctor`` names it."""
    v = _block(conv).get('forbidden_markers')
    raw = DEFAULT_FORBIDDEN_MARKERS if v is None else (v if isinstance(v, list) else [])
    out = []
    for p in raw:
        try:
            out.append(re.compile(str(p)))
        except re.error:
            continue
    return out


def matches(globs, path):
    """True when ``path`` lies under one of ``globs`` (``dir/`` a prefix, else fnmatch, where
    ``*`` crosses ``/``; a bare directory matches everything below it)."""
    for g in globs or ():
        g = str(g).strip()
        if not g:
            continue
        if g.endswith('/') and path.startswith(g):
            return True
        if fnmatch.fnmatch(path, g) or path.startswith(g.rstrip('/') + '/'):
            return True
    return False


def touched(conv, files):
    """The files among ``files`` that are customer content."""
    globs = paths(conv)
    return [f for f in files or () if f and matches(globs, f)] if globs else []


def scan_line(pats, text):
    """The first marker pattern ``text`` matches, or None."""
    for p in pats:
        m = p.search(text)
        if m:
            return m.group(0)
    return None


def _git(repo, args, data=None, text=True):
    try:
        p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=text,
                           input=data, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def added_lines(diff):
    """``[(path, line, text)]`` for every line a ``git diff -U0`` output adds."""
    out, path, n = [], None, 0
    for raw in (diff or '').splitlines():
        if raw.startswith('+++ '):
            target = raw[4:].strip()
            path = target[2:] if target.startswith('b/') else (None if target == '/dev/null'
                                                               else target)
            continue
        m = _HUNK_RE.match(raw)
        if m:
            n = int(m.group(1))
            continue
        if raw.startswith('+') and path is not None:
            out.append((path, n, raw[1:]))
            n += 1
    return out


def added_hits(repo, base, head, conv):
    """``[(path, line, marker)]``: each line ``head`` adds since it left ``base`` under
    ``customer_content.paths`` that matches a forbidden marker. [] when nothing is configured
    or the diff cannot be read."""
    globs = paths(conv)
    if not globs or not repo:
        return []
    diff = _git(repo, ['diff', '-U0', '--no-color', '--no-ext-diff', f'{base}...{head}'])
    pats = markers(conv)
    hits = []
    for path, n, text in added_lines(diff):
        if not matches(globs, path):
            continue
        m = scan_line(pats, text)
        if m:
            hits.append((path, n, m))
    return hits


def tree_hits(repo, sha, conv):
    """``[(path, line, marker)]`` in the tree of ``sha`` under ``customer_content.paths`` — or
    None when that tree cannot be read (the caller refuses: a tree it could not read is not a
    clean one). [] when nothing is configured."""
    globs = paths(conv)
    if not globs:
        return []
    listed = _git(repo, ['ls-tree', '-r', '--name-only', sha]) if repo and sha else None
    if listed is None:
        return None
    files = [f for f in listed.splitlines() if f and matches(globs, f)]
    if not files:
        return []
    data = ''.join(f'{sha}:{f}\n' for f in files).encode()
    blob = _git(repo, ['cat-file', '--batch'], data=data, text=False)
    if blob is None:
        return None
    pats, hits, i = markers(conv), [], 0
    for f in files:
        nl = blob.find(b'\n', i)
        if nl < 0:
            return None
        head = blob[i:nl].split()
        if len(head) < 3 or head[-1] == b'missing':
            i = nl + 1
            continue
        size = int(head[2])
        body = blob[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
        if head[1] != b'blob' or size > MAX_FILE_BYTES or b'\0' in body[:8000]:
            continue
        for n, line in enumerate(body.decode('utf-8', 'replace').splitlines(), 1):
            m = scan_line(pats, line)
            if m:
                hits.append((f, n, m))
    return hits


def describe(hits, limit=SHOW_HITS):
    """``path:line (marker), …`` for a hold or a refusal, the rest counted."""
    shown = [f'{p}:{n} ({m.strip()})' for p, n, m in hits[:limit]]
    more = len(hits) - len(shown)
    return ', '.join(shown) + (f' and {more} more' if more > 0 else '')


def refusal(repo, trunk, branch, conv):
    """``(KIND, text)`` for a lane branch that adds a forbidden marker to customer content,
    else None."""
    hits = added_hits(repo, f'origin/{trunk}', f'origin/{branch}', conv)
    if not hits:
        return None
    return KIND, (f'internal text on a customer page: {describe(hits)} — a customer reads these '
                  f'pages as written; remove every internal note, TODO and placeholder (or '
                  f'resolve it into final copy) under customer_content.paths, then push')


# ---- the review ---------------------------------------------------------------------------

REVIEW_SECTION = """## Required: read as the customer

This diff touches pages a customer reads ({pages}). Before the verdict, read each of them in
full as the customer will see it — render it (run the site, or read the rendered source top to
bottom), never just the diff:

1. every touched page, whole, as published — not only the changed lines;
2. list each contradiction between these pages and the product's other customer pages (a term,
   a price, a period, an entity name, a contact that differs between two pages);
3. flag any internal-looking text — a bracketed note (`[legal: …]`, `[TODO: …]`), TODO/FIXME,
   lorem ipsum, an unresolved placeholder, a note to a colleague or a lawyer.

The check table MUST carry this row — a review of this diff without it is incomplete and goes
back to review, whatever its verdict:

| read as the customer: every touched page read in full, no contradiction, no internal text | pass \\| fail | each page, the contradictions and the internal text found (or "none") |

Any contradiction or internal text found is a C, and the row is `fail`."""


def branch_files(repo, trunk, branch):
    """The files ``origin/<branch>`` changed since it left ``origin/<trunk>`` ([] unreadable)."""
    if not repo or not branch:
        return []
    out = _git(repo, ['diff', '--name-only', f'origin/{trunk}...origin/{branch}'])
    return [l for l in (out or '').splitlines() if l.strip()]


def review_brief_section(product, branch):
    """The ``read as the customer`` section of a review brief for ``branch``: '' when the
    product names no customer content or the branch touches none of it."""
    conv = getattr(product, 'conventions', None)
    if product is None or not paths(conv):
        return ''
    files = branch_files(getattr(product, 'repo_dir', None), getattr(product, 'main', 'main'),
                         branch)
    return review_section(conv, files)


def review_section(conv, files):
    """The review brief's required ``read as the customer`` section for a diff touching
    ``files``, or '' when none of them is customer content."""
    hit = touched(conv, files)
    if not hit:
        return ''
    pages = ', '.join(f'`{p}`' for p in hit[:SHOW_HITS])
    if len(hit) > SHOW_HITS:
        pages += f' and {len(hit) - SHOW_HITS} more'
    return REVIEW_SECTION.format(pages=pages)


def has_customer_row(text):
    """True when a review's check table carries the ``read as the customer`` row with a result
    (``pass`` or ``fail``)."""
    return bool(_ROW_RE.search(text or ''))


# ---- the doctor ---------------------------------------------------------------------------

def findings(product):
    """[(ok, detail)] — the doctor's ``customer content`` row: a product that deploys but names
    no customer content (its pages would ship unchecked), and markers that do not compile."""
    conv = getattr(product, 'conventions', None)
    out = []
    from asf.harvest import deploy
    deploys = bool(deploy._cfg(product)) and bool(deploy.applies(product)
                                                   or deploy.target_names(product))
    if not configured(conv):
        if deploys:
            out.append((False, 'a deploy target is configured but conventions.customer_content'
                               ' is not — customer pages ship with no internal-marker check'
                               ' (set customer_content.paths)'))
        return out
    v = _block(conv).get('forbidden_markers')
    for p in (v if isinstance(v, list) else ()):
        try:
            re.compile(str(p))
        except re.error as e:
            out.append((False, f'customer_content.forbidden_markers: {p!r} is not a regex ({e})'))
    globs = paths(conv)
    if not globs:
        out.append((False, 'conventions.customer_content names no paths — nothing is checked'))
    else:
        n = len(markers(conv))
        out.append((True, f"{', '.join(globs)} · {n} forbidden marker(s)"))
    return out
