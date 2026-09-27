"""asf.groom.conflicts — the supersession graph and the conflict-pairing pass (F-0046).

Pure: no io, no git, no product config. Imports only :mod:`asf.record.core` and
:mod:`asf.record.frontmatter`, the same module-level pair `asf/groom/shape.py` already imports,
which is what lets `asf.record.check` import this module with no cycle. Every function here takes
a record (``{'meta': ..., 'body': ..., 'relpath': ..., 'text': ...}``) or a plain mapping — never
a path, never a subprocess, never a setting a product's yaml would carry.

Four groups: the reader (`statement_of`, `source_of`, `scope_of`, `named_objects`) turns a card's
body into the text a pair is scored on; the graph (`supersession_edges`, `cycles`, `is_superseded`,
`supersession_findings`) reads the two typed fields a card may carry and finds what is wrong with
them; the pass (`live`, `pair_score`, `pairs`, `precedence`, `decline_key`) ranks every live rule
and decision against every other; and the line (`CONFLICT_RE`, `CONFLICT_ANSWER_RE`,
`conflict_lines`) renders a ranked pair into the groom's own line grammar.
"""
import re
from collections import namedtuple

from asf.record import frontmatter
from asf.record.core import as_list, is_open, jaccard, parse_sections, tokenize

#: The body headings a statement may live under, best first — the three shapes this record
#: actually contains: a migrated decision's `## Statement`, a hand-written rule's `## Statement`,
#: and an `asf new` card's `## Description`. `## Decision` is a synonym no writer here uses today
#: but a hand-edited card could carry.
STATEMENT_HEADINGS = ('## Statement', '## Decision', '## Description')

#: "above a half" — strictly above, the way the near-duplicate-titles pass is strictly above its
#: own threshold.
CONFLICT_OVERLAP = 0.5

#: Three literal shapes a card names something by: a backtick code span, a `[[id]]` record link,
#: and a path-shaped token (at least one `/`). Deliberately not `tokenize` — it lowercases and
#: drops punctuation, so a code span and an ordinary sentence would name the same "object".
NAMED_OBJECT_RES = (
    re.compile(r'`([^`\n]+)`'),                          # a code span
    re.compile(r'\[\[([A-Z]-\d+)\]\]'),                  # a record link
    re.compile(r'(?<![\w`./-])((?:[\w-]+/)+[\w.-]+)'),   # a path
)

#: A conflicts question line: `- [ ] <a> conflicts <b> …`. Matched by the groom's applier; the
#: line decides which two cards, never the answer word.
CONFLICT_RE = re.compile(r'^- \[[ xX]\]\s+(?P<a>[A-Z]-\d+)\s+conflicts\s+(?P<b>[A-Z]-\d+)\b')

#: The four words a conflicts line accepts, case-insensitively.
CONFLICT_ANSWER_RE = re.compile(r'^(keep\s+[AB]|both|merge)$', re.IGNORECASE)

#: One ranked candidate: ``a < b`` by id; ``winner``/``why`` are :func:`precedence`'s answer for
#: the pair, computed once and carried so the line and the applier never disagree with each other.
Pair = namedtuple('Pair', 'a b score reason winner why')


def _sections_by_heading(body):
    _preamble, sections = parse_sections(body)
    return {heading.strip(): content for heading, content in sections}


def statement_of(rec):
    """The card's statement as one whitespace-collapsed string: the first of
    :data:`STATEMENT_HEADINGS` whose section has any text, else the card's title. The title is
    never prepended to a section — a shared title is the near-duplicate-titles pass's finding,
    not this one."""
    by_heading = _sections_by_heading(rec['body'])
    for heading in STATEMENT_HEADINGS:
        text = ' '.join(by_heading.get(heading, '').split())
        if text:
            return text
    return ' '.join((rec['meta'].get('title') or '').split())


def source_of(rec):
    """Where the statement came from, for the groom line: the first non-empty line of the card's
    ``## Source`` section, else its ``date:``/``decided_by:``, else its ``relpath``. Never
    empty."""
    by_heading = _sections_by_heading(rec['body'])
    for line in by_heading.get('## Source', '').splitlines():
        line = line.strip()
        if line:
            return line
    meta = rec['meta']
    return meta.get('date') or meta.get('decided_by') or rec['relpath']


def scope_of(rec):
    """``scope:``, else ``area:``, casefolded and stripped; ``None`` when neither is set. Two
    cards with no scope never share one."""
    value = rec['meta'].get('scope') or rec['meta'].get('area')
    if not value:
        return None
    return str(value).strip().casefold()


def named_objects(text):
    """The casefolded set of things ``text`` names: every code span's content, every ``[[id]]``
    link's id, every path-shaped token. Empty matches are dropped."""
    found = set()
    for rx in NAMED_OBJECT_RES:
        for m in rx.finditer(text or ''):
            value = m.group(1)
            if value:
                found.add(value.casefold())
    return found


# ------------------------------------------------------------------------------- the graph --

def supersession_edges(canonical):
    """``{(loser, winner)}`` — one de-duplicated directed edge set off both fields.

    ``superseded_by: W`` on ``L`` and ``supersedes: [L]`` on ``W`` are the same edge; an edge
    written only one way is still an edge here (the missing half is a finding of its own), so the
    cycle search sees the whole graph however it was spelled."""
    edges = set()
    for iid, rec in canonical.items():
        meta = rec['meta']
        successor = meta.get('superseded_by')
        if successor:
            edges.add((iid, successor))
        for loser in as_list(meta.get('supersedes')):
            edges.add((loser, iid))
    return edges


def cycles(edges):
    """Every cycle in ``edges``, each as a list of ids starting at its own lowest id and closing
    on it — ``['D-0001', 'D-0002', 'D-0001']``. A self-edge is a cycle of one. Deterministic:
    nodes are walked in sorted order and each cycle is reported once."""
    graph = {}
    nodes = set()
    for loser, winner in edges:
        graph.setdefault(loser, set()).add(winner)
        nodes.add(loser)
        nodes.add(winner)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    found = {}

    def record(ring):
        lo = min(ring[:-1])
        idx = ring.index(lo)
        rotated = ring[idx:-1] + ring[:idx] + [lo]
        found[tuple(rotated)] = rotated

    for start in sorted(nodes):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        stack = [start]
        iters = [iter(sorted(graph.get(start, ())))]
        while stack:
            advanced = False
            for nxt in iters[-1]:
                c = color.get(nxt, WHITE)
                if c == WHITE:
                    color[nxt] = GRAY
                    stack.append(nxt)
                    iters.append(iter(sorted(graph.get(nxt, ()))))
                    advanced = True
                    break
                if c == GRAY:
                    idx = stack.index(nxt)
                    record(stack[idx:] + [nxt])
            if not advanced:
                color[stack.pop()] = BLACK
                iters.pop()
    return [found[k] for k in sorted(found)]


def is_superseded(entry):
    """True when a card (a ``canonical`` record's ``meta``, or an ``index.json`` entry) carries a
    non-empty ``superseded_by``. A dangling successor still supersedes: the card says it has been
    replaced, ``asf check`` says by what is missing, and the rule run must not enforce it
    meanwhile."""
    return bool(entry.get('superseded_by'))


def supersession_findings(canonical, find_line):
    """``[(relpath, line, message)]`` — the four supersession defects: a dangling ``supersedes``
    or ``superseded_by``, a pair written on only one of the two cards, and a cycle. ``find_line``
    is the caller's own ``(rec, key) -> line`` lookup, so this stays pure over the records it is
    handed."""
    findings = []
    for iid, rec in canonical.items():
        meta = rec['meta']
        for loser in as_list(meta.get('supersedes')):
            if loser not in canonical:
                findings.append((rec['relpath'], find_line(rec, 'supersedes'),
                                  f"supersedes references missing item {loser}"))
                continue
            loser_rec = canonical[loser]
            if loser_rec['meta'].get('superseded_by') != iid:
                findings.append((
                    loser_rec['relpath'], find_line(loser_rec, 'id'),
                    f"{iid} supersedes {loser}, which carries no superseded_by: {iid} "
                    f"(run `asf set {loser} superseded_by={iid}`)"))
        successor = meta.get('superseded_by')
        if successor:
            if successor not in canonical:
                findings.append((rec['relpath'], find_line(rec, 'superseded_by'),
                                  f"superseded_by references missing item {successor}"))
            else:
                winner_rec = canonical[successor]
                if iid not in as_list(winner_rec['meta'].get('supersedes')):
                    findings.append((
                        winner_rec['relpath'], find_line(winner_rec, 'id'),
                        f"{iid} is superseded_by {successor}, which does not list {iid} in "
                        f"supersedes: (run `asf set {successor} supersedes=[{iid}]`)"))
    edges = {(loser, winner) for loser, winner in supersession_edges(canonical)
             if loser in canonical and winner in canonical}
    for ring in cycles(edges):
        rec = canonical[ring[0]]
        findings.append((rec['relpath'], find_line(rec, 'id'),
                          f"supersession cycle {' → '.join(ring)}"))
    return findings


# --------------------------------------------------------------------------------- the pass --

def live(canonical):
    """``{id: rec}`` — every open rule and decision that is not superseded. The candidate set:
    :func:`~asf.record.core.is_open` and not :func:`is_superseded`, so answering a pair `keep A`
    removes it from tomorrow's pass with no other bookkeeping."""
    return {iid: rec for iid, rec in canonical.items()
            if rec['meta'].get('type') in ('rule', 'decision')
            and is_open(rec) and not is_superseded(rec['meta'])}


def _pair_detail(a_rec, b_rec):
    """``(score, reason, overlap, scope, shared_object)`` — the raw numbers behind
    :func:`pair_score`, kept together so :func:`conflict_lines` can render the reason phrase
    without scoring the pair twice."""
    a_stmt, b_stmt = statement_of(a_rec), statement_of(b_rec)
    overlap = jaccard(tokenize(a_stmt), tokenize(b_stmt))
    has_overlap = overlap > CONFLICT_OVERLAP

    a_scope, b_scope = scope_of(a_rec), scope_of(b_rec)
    scope_score = 0.0
    shared_object = None
    has_scope = False
    if a_scope is not None and a_scope == b_scope:
        shared = named_objects(a_stmt) & named_objects(b_stmt)
        if shared:
            has_scope = True
            scope_score = jaccard(named_objects(a_stmt), named_objects(b_stmt))
            shared_object = sorted(shared)[0]

    if has_overlap and has_scope:
        reason = 'overlap+scope'
    elif has_overlap:
        reason = 'overlap'
    elif has_scope:
        reason = 'scope'
    else:
        reason = None
    score = max(overlap if has_overlap else 0.0, scope_score if has_scope else 0.0)
    return score, reason, overlap, a_scope, shared_object


def pair_score(a_rec, b_rec):
    """``(score, reason)`` for one ordered pair, or ``(0.0, None)`` when it is not a candidate.

    - ``overlap``: ``jaccard(tokenize(statement_of(a)), tokenize(statement_of(b))) >
      CONFLICT_OVERLAP`` — score is that Jaccard.
    - ``scope``: the two scopes are equal and non-None **and** they share a named object — score
      is ``jaccard(named_objects(a), named_objects(b))``.
    - both: reason ``overlap+scope``, score the larger of the two.
    """
    score, reason, _overlap, _scope, _obj = _pair_detail(a_rec, b_rec)
    return score, reason


def _id_num(iid):
    return int(iid.split('-', 1)[1])


def precedence(a_rec, b_rec):
    """``(winner id, why)``: the newer of ``date:``, else ``stage_since:``, else ``updated:``,
    compared as the ISO strings they are; on a tie or with nothing to compare, the numerically
    higher id, which is the later mint. ``why`` is the phrase the line prints — ``newer,
    2026-09-20`` or ``same date, minted later``."""
    def dated(rec):
        meta = rec['meta']
        return meta.get('date') or meta.get('stage_since') or meta.get('updated')

    a_id, b_id = a_rec['meta']['id'], b_rec['meta']['id']
    a_date, b_date = dated(a_rec), dated(b_rec)
    if a_date and b_date and a_date != b_date:
        if a_date > b_date:
            return a_id, f"newer, {a_date}"
        return b_id, f"newer, {b_date}"
    winner = a_id if _id_num(a_id) > _id_num(b_id) else b_id
    return winner, "same date, minted later"


def decline_key(a, b):
    """``'<a>+<b>'`` in id order — the `both` answer's suppression key and the line's dedupe
    token, one string derived from the pair alone."""
    lo, hi = sorted((a, b))
    return f"{lo}+{hi}"


def pairs(canonical):
    """Every candidate pair of the live set, ranked. Each is a :data:`Pair` with ``a < b`` by id,
    sorted by ``(-score, a, b)``.

    Excluded: a pair whose key is in either card's ``conflict_declined`` (the groom answered
    `both`), and a pair already joined by a supersession edge in either direction."""
    live_recs = live(canonical)
    joined = {frozenset(edge) for edge in supersession_edges(canonical)}
    ids = sorted(live_recs)
    found = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            if frozenset((a_id, b_id)) in joined:
                continue
            a_rec, b_rec = live_recs[a_id], live_recs[b_id]
            key = decline_key(a_id, b_id)
            a_declined = key in as_list(a_rec['meta'].get('conflict_declined'))
            b_declined = key in as_list(b_rec['meta'].get('conflict_declined'))
            if a_declined or b_declined:
                continue
            score, reason = pair_score(a_rec, b_rec)
            if reason is None:
                continue
            winner, why = precedence(a_rec, b_rec)
            found.append(Pair(a_id, b_id, score, reason, winner, why))
    found.sort(key=lambda p: (-p.score, p.a, p.b))
    return found


# ---------------------------------------------------------------------------------- the line --

def _truncate(text, limit=120):
    if len(text) <= limit:
        return text
    return text[:limit - 1] + '…'


def _reason_phrase(reason, overlap, scope, shared_object):
    if reason == 'overlap':
        return f"overlap {overlap:.2f}"
    if reason == 'scope':
        return f"scope `{scope}`, shares `{shared_object}`"
    return f"overlap {overlap:.2f}; scope `{scope}`, shares `{shared_object}`"


def conflict_lines(canonical):
    """One block per ranked pair — a question line plus three indented lines carrying both
    statements, both sources and the precedence hint. Each block is one list element (the
    question line and its three indented lines joined by newlines), so a section built from this
    list has exactly one entry per pair."""
    lines = []
    for pair in pairs(canonical):
        a_rec, b_rec = canonical[pair.a], canonical[pair.b]
        _score, _reason, overlap, scope, shared_object = _pair_detail(a_rec, b_rec)
        phrase = _reason_phrase(pair.reason, overlap, scope, shared_object)
        head = (f"- [ ] {pair.a} conflicts {pair.b} — {phrase}; "
                f"keep A · keep B · both · merge (merge keeps {pair.winner}) → answer: ____")
        a_line = f"      A {pair.a} \"{_truncate(statement_of(a_rec))}\" — {source_of(a_rec)}"
        b_line = f"      B {pair.b} \"{_truncate(statement_of(b_rec))}\" — {source_of(b_rec)}"
        prec_line = f"      precedence: {pair.winner} ({pair.why})"
        lines.append('\n'.join([head, a_line, b_line, prec_line]))
    return lines
