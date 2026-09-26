"""asf.evidence.review — the one review reader (W2 implements the reader; W1's lane calls it).

A review is a file at ``conventions.review_pattern`` (``{reviews_dir}``, ``{n}`` the round,
``{slug}`` substituted) on a branch or on the trunk. The same reader answers for a spec, a plan
and code — spec/plan approval in ``asf.evidence``, T3/T4/T5 in :mod:`asf.harvest.lane` — and
replaces ``harvest.review_verdict``/``review_round``/``review_is_current``,
``evidence.rx_review``/``newest_review``/``verdict_of`` and ``pr_hygiene.parse_verdict``/
``branch_verdict``.

**One contract.** The slug is the item's id, lower-cased (``f-0047``, ``t-0112``) — what the
briefs write (:func:`asf.briefs.preamble.review_path_for`). A record migrated from an older lane
still carries the legacy form ``<prefix>-review-r<n>.md`` in the reviews directory
(``spec-<slug>-review-r1.md``, ``<slug>-plan-review-r2.md``); it is read as a fallback, so an old
review keeps its verdict. When both forms carry a round, the higher round wins, and the pattern
form wins a tie.

**The verdict** is the review's own ``verdict:`` line (:func:`verdict_of`) — the first one wins.
A legacy file written before the line existed is read by its first verdict word
(``APPROVED``/``CHANGES REQUESTED``/``BOUNCE``/``REVISE``), and only a legacy file is.

A review is *current* for a head when the head it names (its ``head:`` line) is that head; a
review of an older head is history, not a verdict.
"""
import re
import subprocess

from asf.conventions import DEFAULT_REVIEW_PATTERN, DEFAULT_REVIEWS_DIR

APPROVED = 'approved'
CHANGES = 'changes'
#: What :func:`verdict_of` may return, besides None (no verdict line).
VERDICTS = (APPROVED, CHANGES)

#: A review's own verdict line: ``verdict: approved``, ``**Verdict:** changes requested``,
#: ``| verdict | approved |`` is not one (a table cell is a check row, not the verdict).
VERDICT_LINE_RE = re.compile(r'^[\s*#>|_-]*verdict[\s*_]*:[\s*_`]*(?P<v>[^\n|]+)', re.I | re.M)
#: The head a review reviewed: ``head: <sha>`` (7–40 hex).
HEAD_LINE_RE = re.compile(r'^[\s*#>|_-]*head[\s*_]*:[\s*_`]*(?P<sha>[0-9a-f]{7,40})\b', re.I | re.M)
#: A legacy review's verdict word, anywhere in the text.
LEGACY_WORD_RE = re.compile(r'APPROVED|CHANGES REQUESTED|BOUNCE|REVISE', re.I)
#: The legacy file name: ``<prefix>-review-r<n>.md`` (an optional letter after the round).
LEGACY_NAME_RE = re.compile(r'(?P<prefix>.+)-review-r(?P<n>\d+)[a-z]?\.md')
#: How much of a review is read for its verdict.
READ_CHARS = 20000


def verdict_of(text):
    """A review's verdict from its text: :data:`APPROVED` for ``verdict: approved``,
    :data:`CHANGES` for ``verdict: changes…`` (``changes requested`` and the like), None when the
    text carries no verdict line. Case-insensitive; the first verdict line wins."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:READ_CHARS]).decode('utf-8', 'replace')
    for m in VERDICT_LINE_RE.finditer((text or '')[:READ_CHARS]):
        word = m.group('v').strip().strip('`*_ ').lower()
        if word.startswith('approved'):
            return APPROVED
        if word.startswith(('changes', 'change requested', 'bounce', 'revise')):
            return CHANGES
    return None


def legacy_verdict_of(text):
    """A legacy review's verdict: its verdict line when it has one, else its first verdict word
    anywhere in the text — the reading the pre-contract lanes used."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:READ_CHARS]).decode('utf-8', 'replace')
    v = verdict_of(text)
    if v:
        return v
    m = LEGACY_WORD_RE.search((text or '')[:READ_CHARS])
    if not m:
        return None
    return APPROVED if m.group(0).upper() == 'APPROVED' else CHANGES


def head_of(text):
    """The sha a review names on its ``head:`` line, or None."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:READ_CHARS]).decode('utf-8', 'replace')
    m = HEAD_LINE_RE.search((text or '')[:READ_CHARS])
    return m.group('sha').lower() if m else None


def pattern_rx(conv, slug):
    """The compiled ``review_pattern`` for ``slug`` (``{n}`` a group), over repo paths."""
    pattern = str(getattr(conv, 'review_pattern', None) or DEFAULT_REVIEW_PATTERN)
    reviews_dir = _dir_of(conv)
    rx = (re.escape(pattern)
          .replace(re.escape('{reviews_dir}'), re.escape(reviews_dir))
          .replace(re.escape('{slug}'), re.escape(str(slug).lower()))
          .replace(re.escape('{n}'), r'(?P<n>\d+)'))
    return re.compile(rx, re.I)


def _dir_of(conv):
    return str(getattr(conv, 'reviews_dir', None) or DEFAULT_REVIEWS_DIR).strip('/')


def rounds(conv, paths, slug, legacy_prefixes=()):
    """Every review of ``slug`` among repo ``paths``: ``[(round, path, legacy)]`` sorted by
    round, the pattern form after a legacy one of the same round (so the last entry is the
    newest, and the pattern form wins a tie). ``legacy_prefixes`` are the ``<prefix>`` values a
    legacy ``<prefix>-review-r<n>.md`` file of this item may carry (``slug`` itself when none)."""
    rx = pattern_rx(conv, slug)
    prefixes = {str(p).lower() for p in (legacy_prefixes or (slug,)) if p}
    rdir = _dir_of(conv) + '/'
    out = []
    for path in paths or ():
        path = str(path)
        m = rx.fullmatch(path)
        if m:
            out.append((int(m.group('n')), path, False))
            continue
        if path.startswith(rdir):
            lm = LEGACY_NAME_RE.fullmatch(path[len(rdir):])
            if lm and lm.group('prefix').lower() in prefixes:
                out.append((int(lm.group('n')), path, True))
    out.sort(key=lambda t: (t[0], not t[2]))
    return out


def pick(conv, paths, slug, legacy_prefixes=()):
    """The newest review of ``slug`` among ``paths``: ``(round, path, legacy)``, or None."""
    found = rounds(conv, paths, slug, legacy_prefixes)
    return found[-1] if found else None


def read(text, legacy=False):
    """``(verdict, head)`` of a review's text, the legacy word fallback for a legacy file."""
    return (legacy_verdict_of(text) if legacy else verdict_of(text)), head_of(text)


def _git(repo, *args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, timeout=120)
    return p.stdout if p.returncode == 0 else None


def newest(product, branch, item):
    """The newest review of ``item`` visible on ``branch`` (the trunk when ``branch`` is the
    trunk), read through ``conventions.review_pattern``: ``(round, verdict, head)`` — ``round``
    the highest ``{n}``, ``verdict`` from :func:`verdict_of`, ``head`` the sha the review names
    (None when it names none) — or None when there is no review file."""
    if not item or not branch or product is None:
        return None
    conv = product.conventions
    repo = getattr(product, 'repo_dir', None)
    if not repo:
        return None
    ref = f'origin/{branch}'
    listed = _git(repo, 'ls-tree', '-r', '--name-only', ref, '--', _dir_of(conv))
    if listed is None:
        return None
    slug = str(item).lower()
    hit = pick(conv, listed.splitlines(), slug, (slug, f'spec-{slug}', f'plan-{slug}',
                                                  f'{slug}-spec', f'{slug}-plan'))
    if hit is None:
        return None
    n, path, legacy = hit
    text = _git(repo, 'show', f'{ref}:{path}') or ''
    verdict, head = read(text, legacy)
    return n, verdict, head


def verdict_text(text):
    """The first verdict line's own words, lower-cased (``changes requested``), or '' — what a
    lane line quotes back to the writer."""
    m = VERDICT_LINE_RE.search((text or '')[:READ_CHARS])
    return m.group('v').strip().strip('`*_ ').lower() if m else ''


def review_at(repo, conv, ref, item):
    """The newest review of ``item`` at ``ref`` (``origin/<branch>``) in ``repo``, for the lane:
    ``{round, verdict, text, head, path, body}`` — or None. The same reading as :func:`newest`."""
    if not item or not repo:
        return None
    listed = _git(repo, 'ls-tree', '-r', '--name-only', ref, '--', _dir_of(conv))
    if listed is None:
        return None
    slug = str(item).lower()
    hit = pick(conv, listed.splitlines(), slug, (slug, f'spec-{slug}', f'plan-{slug}',
                                                  f'{slug}-spec', f'{slug}-plan'))
    if hit is None:
        return None
    n, path, legacy = hit
    body = _git(repo, 'show', f'{ref}:{path}') or ''
    verdict, head = read(body, legacy)
    return {'round': n, 'verdict': verdict, 'text': verdict_text(body) or (verdict or ''),
            'head': head, 'path': path, 'body': body[:READ_CHARS]}


def is_current(repo, conv, ref, review, head):
    """True when ``review`` (from :func:`review_at`) reviewed ``head``, the tip of ``ref``: the
    head its ``head:`` line names, or — a review naming none, as a review session commits it on
    the branch it reviews — nothing outside the reviews directory changed after it."""
    if not review:
        return False
    if review.get('head'):
        return bool(head) and head.lower().startswith(review['head'])
    last = (_git(repo, 'log', '-1', '--format=%H', ref, '--', review['path']) or '').strip()
    if not last:
        return False
    after = (_git(repo, 'diff', '--name-only', last, ref) or '').split()
    reviews = _dir_of(conv) + '/'
    return not [f for f in after if not f.startswith(reviews)]


def only_reviews_since(repo, conv, sha, ref):
    """True when ``sha`` is an ancestor of ``ref`` and nothing outside the reviews directory
    changed between them: ``ref`` is ``sha`` plus review files. A session naming the code commit
    under a review commit (a product's B-1377: ``pushed: yes 99bcb623e``, the head its review
    commit) left the branch where it found it all the same."""
    if not sha or not ref:
        return False
    if subprocess.run(['git', 'merge-base', '--is-ancestor', sha, ref], cwd=repo,
                      capture_output=True).returncode != 0:
        return False
    after = (_git(repo, 'diff', '--name-only', sha, ref) or '').split()
    reviews = _dir_of(conv) + '/'
    return not [f for f in after if not f.startswith(reviews)]
