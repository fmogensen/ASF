"""asf.evidence.review — the one review reader (W2 implements the reader; W1's lane calls it).

A review is a file at ``conventions.review_pattern`` (``{reviews_dir}``, ``{n}`` the round,
``{slug}`` substituted). A new one lives off the branch it reviews, in the review store
(:mod:`asf.evidence.review_store`), bound to the head it reviewed — a review committed to a PR
branch restarted the PR's CI; the files a branch or the trunk carries (every review before the
store, a cloud session's) are still read, and a stored review wins unless the branch holds a
strictly higher round. The same reader answers for a spec, a plan
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

**The verdict** is derived from the review's own check table first — :mod:`asf.reviews` is the
one owner of that grammar, and its ``BOUNCE`` reads as :data:`CHANGES` here, so no lane branch is
stranded on a third value. The review's own ``verdict:`` line (:func:`verdict_of`) answers only
for a file that carries no table at all — the first one wins. A legacy file written before either
existed is read by its first verdict word (``APPROVED``/``CHANGES REQUESTED``/``BOUNCE``/
``REVISE``), and only a legacy file is.

A review is *current* for a head when the head it names (its ``head:`` line) is that head; a
review of an older head is history, not a verdict.

**The verdict block** (``flags.verdict_block``: ``off`` | ``warn`` | ``on``, default ``off``):
the review brief asks for one fenced block at the end of the review —

    ```verdict
    verdict: approved | changes
    head: <the 40-hex sha the review read>
    asks: [C1, C2] | []
    ```

— and with the flag past ``off`` the reader takes the block first (:func:`verdict_block`),
keeping the table and the ``verdict:`` line as the fallback for a review that carries none (every
review filed before the block). The block's ``head`` is the head the verdict is bound to: on any
other head it is :class:`Stale` unless the two heads carry the same own patch over the trunk
(``git patch-id --stable`` of ``merge-base..old`` and ``merge-base..new`` — a rebase or a
mechanical rebuild), when the verdict carries over (``Verdict.carried_from``, :func:`judge`). At
``on`` the session's Stop gate (:mod:`asf.workers.stopgate`) refuses a review that ends without
a valid block on its worktree's head; at ``warn`` it says so and lets the stop through.
"""
import os
import re
import subprocess
from dataclasses import dataclass, field

from asf import reviews
from asf.evidence import review_store
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
#: How much of a review is read for its verdict. A delivery review holds one check table per
#: member and closes with its ``verdict:`` line and its C list: a product's T-0042 rounds 6–9
#: (2026-09-30, 24–25k characters each) put both past a 20,000-character window, so each
#: "approved, C: none" review read as changes (its table cut mid-row) and sent the branch back
#: to a correction with nothing to answer — 11 sessions in 24h. Read the whole review.
READ_CHARS = 1_000_000


#: ``flags.verdict_block`` values; ``require`` is read as ``on``, anything else as ``off``.
BLOCK_OFF, BLOCK_WARN, BLOCK_ON = 'off', 'warn', 'on'
#: What the review template (``briefs/templates/review.md``) writes to ask for the block — the
#: stop gate checks a review run only when its brief carries it, so a session cut from the older
#: template is never refused for it. Not the fence itself: a brief quoting a filed review carries
#: that.
BLOCK_MARK = 'End the review with THE VERDICT BLOCK'
#: One fenced ``verdict`` block: ```` ```verdict ```` … ```` ``` ````.
BLOCK_RE = re.compile(r'^[ \t>]*```verdict[ \t]*\n(?P<body>.*?)^[ \t>]*```', re.M | re.S)
#: A ``key: value`` line inside the block.
BLOCK_KEY_RE = re.compile(r'^[ \t>]*(?P<k>verdict|head|asks|as_of)[ \t]*:[ \t]*(?P<v>.*?)[ \t]*$',
                          re.I | re.M)
#: A block's ``head``: the sha, 7–40 hex.
BLOCK_HEAD_RE = re.compile(r'^`?(?P<sha>[0-9a-f]{7,40})`?$', re.I)
#: An ``asks`` value that holds nothing.
NO_ASKS = ('', 'none', '-', '[]', 'nothing')


@dataclass(frozen=True)
class Verdict:
    """A review's verdict block: ``kind`` (:data:`APPROVED`/:data:`CHANGES`), the ``head`` it is
    bound to, the C ids it ``asks`` for, its ``as_of`` (optional, as written), and — when the
    verdict was carried to a rebuilt head of the same patch — ``carried_from``, the head the
    review read."""
    kind: str
    head: str
    asks: tuple = field(default_factory=tuple)
    as_of: str = ''
    carried_from: str = ''


@dataclass(frozen=True)
class Stale:
    """A verdict block bound to a head that is not the branch's: ``head`` the block names,
    ``expected`` the branch's head — history, not a verdict."""
    head: str
    expected: str


def block_mode(conv):
    """``flags.verdict_block`` of ``conv`` (a :class:`asf.conventions.Conventions`, a product, or
    None): :data:`BLOCK_OFF` | :data:`BLOCK_WARN` | :data:`BLOCK_ON`; unset or unknown is off."""
    conv = getattr(conv, 'conventions', conv)
    reader = getattr(conv, 'flag', None)
    value = reader('verdict_block', BLOCK_OFF) if callable(reader) else BLOCK_OFF
    value = str(value).strip().lower()
    if value in ('on', 'require', 'true'):
        return BLOCK_ON
    return BLOCK_WARN if value == 'warn' else BLOCK_OFF


def _asks(value):
    value = (value or '').strip()
    if value.lower() in NO_ASKS:
        return ()
    value = value.strip('[]')
    return tuple(a.strip().strip('`\'"') for a in value.split(',')
                 if a.strip().strip('`\'"') and a.strip().lower() not in NO_ASKS)


def _kind(value):
    word = (value or '').strip().strip('`*_ ').lower()
    if word == 'approved':
        return APPROVED
    if word in ('changes', 'changes requested', 'changes-requested'):
        return CHANGES
    return None


def verdict_block(text):
    """The first valid ``verdict`` block of a review: a :class:`Verdict`, or None when the text
    carries none — a block whose ``verdict`` is not exactly ``approved`` or ``changes`` (the
    template's own ``approved | changes`` copied back), or whose ``head`` is not a sha, is not
    valid. An ``approved`` block that still ``asks`` for something reads :data:`CHANGES`: a C
    item is a request, whatever the verdict word says."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:READ_CHARS]).decode('utf-8', 'replace')
    for m in BLOCK_RE.finditer((text or '')[:READ_CHARS]):
        keys = {}
        for k in BLOCK_KEY_RE.finditer(m.group('body')):
            keys.setdefault(k.group('k').lower(), k.group('v'))
        kind = _kind(keys.get('verdict'))
        head = BLOCK_HEAD_RE.match((keys.get('head') or '').strip())
        if not kind or not head:
            continue
        asks = _asks(keys.get('asks'))
        if kind == APPROVED and asks:
            kind = CHANGES
        return Verdict(kind, head.group('sha').lower(), asks, (keys.get('as_of') or '').strip())
    return None


def judge(repo, conv, text, head, trunk=None):
    """The verdict block of ``text`` for the branch head ``head``: the :class:`Verdict` when the
    block names that head; the same verdict with ``carried_from`` set when the block names
    another head whose own patch over ``trunk`` (``origin/<trunk>``) is the same
    (``git patch-id --stable`` of ``merge-base..old`` and ``merge-base..new`` — a rebase or a
    mechanical rebuild, nothing re-reviewed); else :class:`Stale`. None when ``text`` carries
    no valid block."""
    v = verdict_block(text)
    if v is None:
        return None
    head = (head or '').strip().lower()
    if head and head.startswith(v.head):
        return v
    if head and repo and trunk:
        old = (_git(repo, 'rev-parse', '--verify', '--quiet', f'{v.head}^{{commit}}') or '').strip()
        if old:
            mine, theirs = _own_patch(repo, conv, trunk, old), _own_patch(repo, conv, trunk, head)
            if mine and mine == theirs:
                return Verdict(v.kind, head, v.asks, v.as_of, carried_from=v.head)
    return Stale(v.head, head)


def block_problem(text, head):
    """Why a review ``text`` does not hold a valid verdict block bound to ``head`` (the
    worktree's ``git rev-parse HEAD``), or '' when it does — the stop gate's one question."""
    v = verdict_block(text)
    if v is None:
        return 'your review has no ```verdict block'
    if head and not head.lower().startswith(v.head):
        return f'your ```verdict block\'s head {v.head} is not the branch head {head}'
    return ''


def worktree_review(conv, wt, item):
    """``(round, path, text)`` of the newest review of ``item`` in worktree ``wt`` — the file a
    review session writes at ``conventions.review_path`` and leaves uncommitted — or None."""
    if not wt or not item:
        return None
    slug = str(item).lower()
    rdir = _dir_of(conv)
    top = os.path.join(wt, rdir)
    paths = []
    for base, _dirs, files in os.walk(top):
        for name in files:
            paths.append(os.path.relpath(os.path.join(base, name), wt).replace(os.sep, '/'))
    hit = pick(conv, paths, slug)
    if hit is None:
        return None
    n, path, _legacy = hit
    try:
        with open(os.path.join(wt, path), encoding='utf-8', errors='replace') as f:
            return n, path, f.read(READ_CHARS)
    except OSError:
        return None


def verdict_of(text, required=(), block=False):
    """A review's verdict, derived from its check table first (:func:`asf.reviews.verdict`,
    ``BOUNCE`` mapped to :data:`CHANGES` so the lane's two-value vocabulary is never stranded):
    :data:`APPROVED`, :data:`CHANGES`, or — a file with no table at all — its own ``verdict:``
    line (``verdict: approved``, ``verdict: changes…`` and the like), None when neither is
    present. Case-insensitive; the first verdict line wins.

    A table whose only fault is a ``fail`` row reads :data:`APPROVED` when the reviewer approved
    over it and asked for nothing (:func:`asks_nothing`): the review's own ``verdict: approved``
    line and a C list that is empty. Such a review once read as changes and sent the branch to a
    correction whose brief — "answer its C list" — held nothing to answer; the rounds ran out and
    the item went to adjudication (69 of 1,526 reviews over 8 days on two products: a Bug with no
    plan marked "acceptance tests byte-identical" ``fail``, a Gate the reviewer's sandbox could not
    run while CI is green). An unfilled or missing row (``BOUNCE``) still reads :data:`CHANGES`.

    ``block`` (``flags.verdict_block`` past ``off``, :func:`block_mode`): a valid verdict block
    (:func:`verdict_block`) answers first; the table and the line are its fallback."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:READ_CHARS]).decode('utf-8', 'replace')
    text = (text or '')[:READ_CHARS]
    if block:
        v = verdict_block(text)
        if v is not None:
            return v.kind
    v = reviews.verdict(text, required)
    if v == reviews.APPROVED:
        return APPROVED
    if v == reviews.CHANGES and asks_nothing(text):
        # a fail row the reviewer weighed and approved over: nothing for a correction to answer
        return APPROVED
    if v == reviews.BOUNCE and only_malformed(text, required) and asks_nothing(text):
        # every required row there and filled, one result cell worded oddly (``n/a→pass``):
        # the approval stands — a correction would have nothing to answer (T-0652, 2026-10-05)
        return APPROVED
    if v in (reviews.CHANGES, reviews.BOUNCE):
        return CHANGES
    for m in VERDICT_LINE_RE.finditer(text):
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


def head_of(text, block=False):
    """The sha a review names on its ``head:`` line, or None — its verdict block's ``head``
    first when ``block`` (:func:`block_mode` past ``off``)."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text[:READ_CHARS]).decode('utf-8', 'replace')
    if block:
        v = verdict_block(text)
        if v is not None:
            return v.head
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


def read(text, legacy=False, required=(), block=False):
    """``(verdict, head)`` of a review's text, the legacy word fallback for a legacy file; the
    verdict block first when ``block`` (:func:`block_mode` past ``off``)."""
    if legacy and not (block and verdict_block(text)):
        return legacy_verdict_of(text), head_of(text)
    return verdict_of(text, required, block), head_of(text, block)


def _git(repo, *args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, timeout=120)
    return p.stdout if p.returncode == 0 else None


def newest(product, branch, item, required=()):
    """The newest review of ``item`` for ``branch`` (the trunk when ``branch`` is the trunk): the
    review store's (:mod:`asf.evidence.review_store`) first, else the file ``branch`` carries,
    read through ``conventions.review_pattern``: ``(round, verdict, head)`` — ``round`` the
    highest ``{n}``, ``verdict`` from :func:`verdict_of`, ``head`` the sha the review is bound to
    (None when a branch file names none) — or None when there is no review."""
    if not item or not branch or product is None:
        return None
    rv = review_at(getattr(product, 'repo_dir', None), product.conventions, f'origin/{branch}',
                   item, required, store=review_store.root(product))
    return (rv['round'], rv['verdict'], rv['head']) if rv else None


def verdict_text(text):
    """The first verdict line's own words, lower-cased (``changes requested``), or '' — what a
    lane line quotes back to the writer."""
    m = VERDICT_LINE_RE.search((text or '')[:READ_CHARS])
    return m.group('v').strip().strip('`*_ ').lower() if m else ''


#: The heading a review's C list sits under (``## C``, ``### C (blocking)``).
C_HEADING_RE = re.compile(r'^#{2,4}\s*C\b[^\n]*$', re.M)
#: A C item's own line: ``1.``, ``C1.``, ``- **C1**``, ``**C2**``, ``### C3`` — and its first
#: repo path (``path:line`` or a backticked path) is what it is about.
C_ITEM_RE = re.compile(r'^\s*(?:[-*]\s*)?(?:\*\*)?(?:#{3,4}\s*)?C?\d+(?:\*\*)?[.):]?\s+(?P<rest>.*)$')
C_PATH_RE = re.compile(r'`?(?P<p>[\w.@-]*[\w@-]/?[\w./@-]*\.[A-Za-z0-9]+|\.[\w-]+/[\w./@-]+)'
                       r'(?::\d[\d-]*)?`?')


def c_items(body):
    """The files a review's C list opens its items with, sorted and unique — the finding a
    correction of this review answers. A C item carried over from the last round names the same
    file (operator policy 2026-09-27: same C-item, same finding). ``[]`` for a review with no C
    list, or none that names a file."""
    m = C_HEADING_RE.search(body or '')
    if not m:
        return []
    sect = body[m.end():]
    end = re.search(r'^#{1,4}\s', sect, re.M)
    out = set()
    for line in (sect[:end.start()] if end else sect).splitlines():
        item = C_ITEM_RE.match(line)
        path = item and C_PATH_RE.search(item.group('rest'))
        if path:
            out.add(path.group('p'))
    return sorted(out)

#: A C list that says it holds nothing: ``None.``, ``(none)``, ``- none.``, ``**None.**``,
#: ``C: none.``, ``No C list``, ``Nothing blocks``.
NO_C_RE = re.compile(r'^[\s>*_`(\[-]*(?:c(?:\s+list)?\s*[:\u2014-]\s*[*_`(]*)?'
                     r'(?:none|nothing|no\s+c\b)', re.I)
#: A C item labelled as one anywhere in a review: ``C1.``, ``- **C2**``, ``### C3``.
C_LABEL_RE = re.compile(r'^\s*(?:[-*]\s*)?(?:\*\*)?(?:#{3,4}\s*)?C\d+\b', re.M)
#: A C list on one line, with no heading: ``C: none.``, ``**C list:** …``.
C_LINE_RE = re.compile(r'^[\s*_]*C(?:\s+list)?[\s*_]*:.*$', re.M)


def only_malformed(text, required=()):
    """True when a table's bounce is only result cells worded outside the vocabulary (``invalid
    result``): no structural fault, no unfilled row, no required check missing. An unfilled or
    missing row is a check not done; an odd word in a filled cell is not."""
    checks, faults = reviews.parse(text)
    lines = (text or '').splitlines()
    names = [c.name for c in checks]
    for f in faults:
        m = re.match(r'line (\d+): invalid result ', f)
        if not m:
            return False
        row = reviews.cells(lines[int(m.group(1)) - 1])
        names.append(row[0] if row else '')
    need = [n for n in required if not any(reviews.covers(x, n) for x in names)]
    return (bool(faults) and not need
            and not any(c.result == reviews._UNFILLED for c in checks))


def asks_nothing(text):
    """True when a review approved and asked for nothing: its first ``verdict:`` line says
    approved, and its C list is empty — the section under the C heading opens with "none" (what
    follows it is the last round's items, answered), or, with no C heading, no line is labelled
    ``C<n>`` and a one-line ``C:`` says none. A C section that opens with anything else is a request, whatever it says."""
    text = (text or '')[:READ_CHARS]
    m = VERDICT_LINE_RE.search(text)
    if not m or not m.group('v').strip().strip('`*_ ').lower().startswith('approved'):
        return False
    h = C_HEADING_RE.search(text)
    if not h:
        return not C_LABEL_RE.search(text) and not any(
            not NO_C_RE.match(ln) for ln in C_LINE_RE.findall(text))
    sect = text[h.end():]
    end = re.search(r'^#{1,4}\s', sect, re.M)
    lines = [ln for ln in (sect[:end.start()] if end else sect).splitlines() if ln.strip()]
    return not lines or bool(NO_C_RE.match(lines[0]))


def review_at(repo, conv, ref, item, required=(), store=None):
    """The newest review of ``item`` for ``ref`` (``origin/<branch>``) in ``repo``, for the lane:
    ``{round, verdict, text, head, path, body}`` — or None.

    ``store`` (:func:`asf.evidence.review_store.root`) is read first: a review filed there for
    the branch is bound to the head it reviewed (``head``), and its ``path`` is the one the
    convention names (``conventions.review_path``), as if it were on the branch — plus
    ``stored``, the entry's own file. The branch's own review files (every review before the
    store, and a cloud session's) still answer, and win only with a strictly higher round.

    With ``flags.verdict_block`` past ``off`` (:func:`block_mode`) a review's verdict block
    answers first — its verdict, and its ``head`` over the head the store bound it to."""
    if not item:
        return None
    blk = block_mode(conv) != BLOCK_OFF
    slug = str(item).lower()
    branch_rv = None
    listed = _git(repo, 'ls-tree', '-r', '--name-only', ref, '--', _dir_of(conv)) if repo else None
    hit = pick(conv, listed.splitlines(), slug, (slug, f'spec-{slug}', f'plan-{slug}',
                                                 f'{slug}-spec', f'{slug}-plan')) \
        if listed is not None else None
    if hit is not None:
        n, path, legacy = hit
        body = _git(repo, 'show', f'{ref}:{path}') or ''
        verdict, head = read(body, legacy, required, blk)
        branch_rv = {'round': n, 'verdict': verdict, 'text': verdict_text(body) or (verdict or ''),
                     'head': head, 'path': path, 'body': body[:READ_CHARS]}
    stored = review_store.newest(store, slug, ref) if store else None
    if review_store.prefer(stored, branch_rv['round'] if branch_rv else None):
        body = stored['text']
        verdict = verdict_of(body, required, blk)
        return {'round': stored['round'], 'verdict': verdict,
                'text': verdict_text(body) or (verdict or ''),
                'head': (head_of(body, True) if blk and verdict_block(body) else None)
                or stored['head'],
                'path': conv.review_path(slug, stored['round']) if hasattr(conv, 'review_path')
                else stored['file'],
                'body': body[:READ_CHARS], 'stored': stored['file']}
    return branch_rv


def is_current(repo, conv, ref, review, head, trunk=None):
    """True when ``review`` (from :func:`review_at`) reviewed ``head``, the tip of ``ref``: the
    head its ``head:`` line names, or — a review naming none, as a review session commits it on
    the branch it reviews — nothing outside the reviews directory changed after it.

    A named head that is not the tip still stands when the code it names is the code at the tip
    (B-0147): the session's own review commit moves the tip, and the lane rewrites commits
    (reword, sign-off, trunk copies dropped, a restack onto ``trunk``) without changing what was
    reviewed — the same tree outside the reviews directory, or the same own patch over
    ``trunk`` (``origin/<trunk>``)."""
    if not review:
        return False
    if review.get('head'):
        if not head:
            return False
        if head.lower().startswith(review['head']):
            return True
        return same_code(repo, conv, review['head'], ref, trunk)
    last = (_git(repo, 'log', '-1', '--format=%H', ref, '--', review['path']) or '').strip()
    if not last:
        return False
    after = (_git(repo, 'diff', '--name-only', last, ref) or '').split()
    reviews = _dir_of(conv) + '/'
    return not [f for f in after if not f.startswith(reviews)]


def _own_patch(repo, conv, trunk, rev):
    """The ``git patch-id --stable`` of ``rev``'s own diff over ``trunk`` outside the reviews
    directory, or None."""
    base = (_git(repo, 'merge-base', trunk, rev) or '').strip()
    if not base:
        return None
    diff = _git(repo, 'diff', base, rev, '--', '.', f':(exclude){_dir_of(conv)}')
    if not diff:
        return None
    p = subprocess.run(['git', '-C', repo, 'patch-id', '--stable'], input=diff,
                       capture_output=True, text=True, timeout=120)
    return (p.stdout.split() or [None])[0] if p.returncode == 0 else None


def same_code(repo, conv, named, ref, trunk=None):
    """True when the commit ``named`` holds the code ``ref`` holds: no file outside the reviews
    directory differs between them, or — ``ref`` restacked onto a newer ``trunk`` — the two carry
    the same own patch over it. False for a ``named`` sha the repo does not know."""
    sha = (_git(repo, 'rev-parse', '--verify', '--quiet', f'{named}^{{commit}}') or '').strip()
    if not sha:
        return False
    reviews = _dir_of(conv) + '/'
    after = _git(repo, 'diff', '--name-only', sha, ref)
    if after is not None and not [f for f in after.split() if not f.startswith(reviews)]:
        return True
    if not trunk:
        return False
    mine, theirs = _own_patch(repo, conv, trunk, sha), _own_patch(repo, conv, trunk, ref)
    return bool(mine) and mine == theirs


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
