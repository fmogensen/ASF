"""asf.proves — the claim, its grammar and its idempotent tick (F-0040). A ``Proves:`` git
trailer on a Task's commits names the Story acceptance line the test proves; this module parses
it, counts the bullets ``line <n>`` counts against, validates one Task's claims against the
record, reads git for a branch's claims through an injected callable (never ``subprocess``,
D11), and flips a bullet from ``- [ ]`` to ``- [x]`` — once, never in reverse (D7).

A claim proves a line **whole** (F-0257). One whose prose qualifies it — ``only``, ``partial``,
``half`` and the rest of :data:`PARTIAL_MARKERS` — is refused: :func:`parse` does not return it,
so nothing counts it and nothing ticks, and :func:`parse_all` hands it back with its reason so
the landing and the pull request can say what was refused and why. A line a session knows it has
only part of is said in the other direction, with a ``Not proved:`` trailer
(:func:`parse_not_proved`).

Two callers read :func:`parse`: :mod:`asf.evidence.evidence` (which claims have landed) and
:mod:`asf.harvest.lane` (the pull-request body). Pure functions over text; no product
convention, no branch prefix, no card folder.
"""
import dataclasses
import os
import re

#: A claim, as written on a commit or in a pull-request body. The id shape is the record's own
#: (any prefix, four digits or more) — which prefix means Story is the record's to say, not this
#: module's: `validate` resolves the id and rejects anything that is not a Story.
CLAIM_RE = re.compile(
    r'^[ \t>*-]*Proves:[ \t]*(?P<story>[A-Za-z]+-\d{4,})[ \t]+line[ \t]+(?P<line>\d+)'
    r'[ \t]*[-–—]+[ \t]*(?P<test>\S.*?)[ \t]*$', re.M | re.I)

#: The words that make a ``Proves:`` claim a *qualified* one — a claim whose own prose says it
#: covers part of its line. Read in the claim's prose only (:func:`claim_prose`), never in the
#: commit subject and never in a path or a quoted test name: every separator a real path uses is
#: a word boundary, so a whole-text scan refuses ``tests/only-guard.test.ts`` and
#: ``tests/e2e/half/upload.spec.ts`` (F-0257 P3).
PARTIAL_MARKERS = ('only', 'partial', 'partly', 'half', 'except', 'not yet', 'minus')

#: Why a line with a refused claim against it is unproved — the fourth member of the ``why``
#: vocabulary beside :data:`NO_PROOF`, :data:`PATH_MISSING` and :data:`NAME_MISSING`.
PARTIAL_CLAIM = 'partial claim'
#: Why a claim on an id the record holds no Story card for proves nothing (F-0285): a spec that
#: cited Story ids nobody minted, and a Task that then wrote ``Proves:`` lines against them.
UNKNOWN_STORY = 'unknown story'


def partial_markers():
    """:data:`PARTIAL_MARKERS`, or the operator's ``proves.partial_markers`` where it is set and
    well-formed. Replaces the list wholesale (D6): the reason to set it is a word a product's
    prose uses normally, and appending could not remove one."""
    from asf import config_keys
    markers = config_keys.value('proves.partial_markers', PARTIAL_MARKERS)
    return tuple(str(m) for m in markers if str(m).strip())


#: A quoted span: a test's own name, never prose (D4). Blanked before the prose is read.
_QUOTED_RE = re.compile(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`')
#: What a token must be stripped of before it is judged a citation or read as prose.
_EDGE = '`"\'()[]{},;.:'


def claim_prose(test):
    """The words of a claim's text that are **not** citations: quoted spans blanked, then every
    token holding ``::`` or ``/`` or ending in an extension dropped (:data:`_PATHLIKE_RE`), the
    rest stripped of edge punctuation and joined with single spaces — so a two-word marker
    (``not yet``) matches across tokens. ``''`` for a claim that is nothing but citations, which
    is what a well-written claim is."""
    text = _QUOTED_RE.sub(' ', test or '')
    words = []
    for token in text.split():
        bare = token.strip(_EDGE)
        if not bare or '::' in bare or _PATHLIKE_RE.search(bare):
            continue
        words.append(bare)
    return ' '.join(words)


def partial_marker(test, markers=None):
    """The first marker of ``markers`` (:func:`partial_markers` when None) that appears as a
    whole word in :func:`claim_prose` of ``test``, else ``''``. Case-insensitive."""
    prose = claim_prose(test)
    if not prose:
        return ''
    for marker in (markers if markers is not None else partial_markers()):
        if re.search(r'\b' + re.escape(marker) + r'\b', prose, re.I):
            return marker
    return ''


#: A ``## Acceptance`` checkbox bullet: ``- [ ]``, ``- [x]`` or ``- [X]``, leading whitespace
#: tolerated (P7). The same shape ``asf.record.check.ACCEPTANCE_ITEM_RE`` requires of a Story.
_ACCEPTANCE_BULLET_RE = re.compile(r'^[ \t]*- \[[ xX]\][ \t]+(\S.*)$')

#: The unticked half of `_ACCEPTANCE_BULLET_RE`, for `tick` to flip in place.
_UNTICKED_RE = re.compile(r'^([ \t]*- )\[ \]')


@dataclasses.dataclass(frozen=True)
class Claim:
    story: str      # upper-cased
    line: int       # 1-based, the nth bullet of the Story's ## Acceptance
    test: str       # as written: a path, optionally path::node; '' on a Not proved: trailer
    raw: str        # the line as found, for the refusal text
    refusal: str = ''   # '' when the claim counts; else why this line is not proved


def parse_all(text, markers=None, known=None):
    """``(counted, refused)`` — every claim in ``text``, in first-seen order, duplicates
    collapsed on ``(story, line, test)``. A claim whose prose carries a partial marker
    (:func:`partial_marker`) goes in ``refused`` with ``refusal`` set to
    ``'partial claim: "<marker>"'`` and never in ``counted``: a line is proved whole or it is not
    proved (F-0257). A line that says ``Proves`` but names no id, no line or no test is in
    neither and raises nothing. ``known``: the record's Story ids (or ``{id: meta}``) — given,
    a claim on any other id goes in ``refused`` as :data:`UNKNOWN_STORY` and renders as
    ``Not proved: … — unknown story`` (F-0285)."""
    seen = set()
    counted, refused = [], []
    for m in CLAIM_RE.finditer(text):
        story = m.group('story').upper()
        line = int(m.group('line'))
        test = m.group('test')
        key = (story, line, test)
        if key in seen:
            continue
        seen.add(key)
        marker = partial_marker(test, markers)
        refusal = f'{PARTIAL_CLAIM}: "{marker}"' if marker else ''
        if not refusal and known is not None and not _is_story(story, known):
            refusal = UNKNOWN_STORY
        claim = Claim(story=story, line=line, test=test, raw=m.group(0), refusal=refusal)
        (refused if refusal else counted).append(claim)
    return counted, refused


def _is_story(iid, known):
    """True when ``known`` (a set of ids, or ``{id: meta}``) holds ``iid`` as a Story."""
    if isinstance(known, dict):
        return iid in known and ((known.get(iid) or {}).get('type') or 'story') == 'story'
    return iid in {str(k).upper() for k in known}


def parse(text, markers=None):
    """The claims of ``text`` that count — :func:`parse_all`'s first half. A qualified claim is
    not among them, so no caller of this function can tick a line off one."""
    return parse_all(text, markers)[0]


#: A ``Not proved:`` trailer — the other half of the grammar (F-0257): the line this Task did
#: *not* prove, and what is missing. Same shape as :data:`CLAIM_RE`, no test path.
NOT_PROVED_RE = re.compile(
    r'^[ \t>*-]*Not proved:[ \t]*(?P<story>[A-Za-z]+-\d{4,})[ \t]+line[ \t]+(?P<line>\d+)'
    r'[ \t]*[-–—]+[ \t]*(?P<missing>\S.*?)[ \t]*$', re.M | re.I)


def parse_not_proved(text):
    """Every ``Not proved:`` trailer in ``text`` as a :class:`Claim` with ``test`` empty and
    ``refusal`` the stated gap, first-seen order, duplicates collapsed on ``(story, line,
    missing)``. Refused-by-the-parser and declared-by-the-author are the same fact about the same
    line, so they are the same type and :func:`render_not_proved` renders both (D11)."""
    seen = set()
    claims = []
    for m in NOT_PROVED_RE.finditer(text):
        story = m.group('story').upper()
        line = int(m.group('line'))
        missing = m.group('missing')
        key = (story, line, missing)
        if key in seen:
            continue
        seen.add(key)
        claims.append(Claim(story=story, line=line, test='', raw=m.group(0), refusal=missing))
    return claims


def bullets(body):
    """The ``## Acceptance`` bullets of a card body, in order — what ``line <n>`` counts (D2).
    Checkbox bullets only (P7): ``- [ ]``, ``- [x]``, ``- [X]``, leading whitespace tolerated,
    the checkbox stripped from the returned text. A section between two acceptance sections is
    not counted, and a body with no ``## Acceptance`` heading yields ``[]``."""
    out = []
    inside = False
    for line in body.splitlines():
        if line.startswith('## '):
            inside = line[3:].strip().lower() == 'acceptance'
            continue
        m = _ACCEPTANCE_BULLET_RE.match(line) if inside else None
        if m:
            out.append(m.group(1))
    return out


def card_bullets(root, item):
    """:func:`bullets` of the card ``root/<folder>/<id>.md`` names; ``[]`` when it is unreadable.
    ``item`` is an index entry — the ``folder``/``id`` pair ``step_prs.card_relpath`` uses."""
    folder, iid = (item or {}).get('folder'), (item or {}).get('id')
    if not root or not folder or not iid:
        return []
    try:
        with open(os.path.join(root, folder, f'{iid}.md'), encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return []
    return bullets(text)


def validate(claims, task, items, root, tree=None):
    """``(good, problems)`` for one Task's claims. ``problems`` are one-line strings naming the
    claim and what is wrong with it, in the order found:

    * ``no claim``          — ``claims`` is empty
    * ``unknown story``     — the id is not in ``items``, or is not a Story
    * ``not this Task's``   — the Story is not one of the Task's (:func:`stories_of_task`:
                              its ``stories:``, or its ``parent:`` when that is a Story)
    * ``no such line``      — ``n`` is outside ``1..len(card_bullets(...))``
    * ``no such test``      — ``tree`` is given and the path before ``::`` is not in it

    ``good`` is the claims with no problem; a Task passes the gate when ``problems`` is empty
    and ``good`` is non-empty (D3)."""
    if not claims:
        return [], ['no claim']
    task_stories = stories_of_task(task, items)
    good, problems = [], []
    for claim in claims:
        story_item = (items or {}).get(claim.story)
        if not story_item or story_item.get('type') != 'story':
            problems.append(f'{UNKNOWN_STORY}: {claim.raw}')
            continue
        if claim.story not in task_stories:
            problems.append(f"not this Task's: {claim.raw}")
            continue
        if not (1 <= claim.line <= len(card_bullets(root, story_item))):
            problems.append(f'no such line: {claim.raw}')
            continue
        if tree is not None and claim.test.split('::', 1)[0] not in tree:
            problems.append(f'no such test: {claim.raw}')
            continue
        good.append(claim)
    return good, problems


def stories_of_task(task, items):
    """The Stories a Task covers: its ``stories:`` list, and its ``parent:`` when the record says
    that parent is a Story — a Task filed under a Story is one of its Tasks whether or not its
    ``stories:`` repeats the id. ``items`` maps an id to its meta (``type`` read); upper-cased
    ids, first-seen order."""
    task = task or {}
    out = [str(s).upper() for s in (task.get('stories') or [])]
    parent = str(task.get('parent') or '').upper()
    if parent and ((items or {}).get(parent) or {}).get('type') == 'story':
        out.append(parent)
    return list(dict.fromkeys(out))


def claims_on_branch(git, trunk, branch):
    """The claims on ``origin/<branch>`` above ``origin/<trunk>``, read with the injected ``git``
    callable (D11): one ``git log --format=%B`` over that range, parsed."""
    text = git('log', '--format=%B', f'origin/{trunk}..origin/{branch}')
    return parse(text or '')


def tick(body, line_no, note, suffix=None):
    """``(new_body, changed)`` — flip the ``line_no``-th ``## Acceptance`` bullet from
    ``- [ ]`` to ``- [x]`` and leave every other character of the body alone. ``changed`` is
    False when the bullet does not exist or is already ticked: the tick is idempotent and never
    reverses (D7). ``note`` is the text the caller appends to ``## History``; this function does
    not write it. ``suffix``, given on a bullet that is newly flipped, is appended to the line as
    ``" — {suffix}"`` — the bullet's own text kept byte-identical (D8); a bullet already ticked is
    left exactly as it is, suffix and all, so a re-run is a no-op."""
    lines = body.splitlines(keepends=True)
    idxs = []
    inside = False
    for i, line in enumerate(lines):
        stripped = line.rstrip('\n')
        if stripped.startswith('## '):
            inside = stripped[3:].strip().lower() == 'acceptance'
            continue
        if inside and _ACCEPTANCE_BULLET_RE.match(stripped):
            idxs.append(i)
    if not (1 <= line_no <= len(idxs)):
        return body, False
    i = idxs[line_no - 1]
    new_line, count = _UNTICKED_RE.subn(r'\1[x]', lines[i], count=1)
    if not count:
        return body, False
    if suffix:
        ending = ''
        if new_line.endswith('\r\n'):
            new_line, ending = new_line[:-2], '\r\n'
        elif new_line.endswith('\n'):
            new_line, ending = new_line[:-1], '\n'
        new_line = f'{new_line} — {suffix}{ending}'
    lines[i] = new_line
    return ''.join(lines), True


def render(claims):
    """The ``## Proves`` block a pull-request body carries — one bullet per claim, the same text
    the trailer had."""
    return '\n'.join(f'- {c.story} line {c.line} — {c.test}' for c in claims)


def render_not_proved(claims):
    """The ``## Not proved`` block a pull-request body carries — one bullet per claim that does
    not prove its line: ``- <S-id> line <n> — <refusal>``, and ``(<test>)`` after it where there
    was a claim to refuse."""
    return '\n'.join(f'- {c.story} line {c.line} — {c.refusal}'
                     + (f' ({c.test})' if c.test else '') for c in claims)


# ---- "no test, no done": which acceptance lines a Story has proved --------------------------

#: The ``## History`` entry ingest writes when a landed (or review) claim proves a line — the one
#: record that a line is proved. ``- <stamp> ingest: proved line <n> — <task…> (<test>)``.
PROVED_ENTRY_RE = re.compile(r'^-[^\n]*?\bingest: proved line (\d+)\b')
#: The ``## History`` entry ``asf untick`` writes: the line's earlier proof no longer counts.
UNTICK_ENTRY_RE = re.compile(r'^-[^\n]*?\buntick: line (\d+)\b')
#: A bullet deferred by a decision: ``deferred`` and a ``D-nnnn`` on the bullet's own text.
DEFERRED_RE = re.compile(r'\bdeferred\b.*?\b(D-\d{4,})\b', re.I)


def _history_lines(body):
    inside = False
    for line in (body or '').splitlines():
        if line.startswith('## '):
            inside = line[3:].strip().lower() == 'history'
            continue
        if inside:
            yield line


def proved_lines(body):
    """The acceptance line numbers the card's ``## History`` records as proved — a ``proved
    line <n>`` entry not followed by an ``untick: line <n>`` one. Pure, over the card body."""
    proved = set()
    for line in _history_lines(body):
        m = PROVED_ENTRY_RE.match(line)
        if m:
            proved.add(int(m.group(1)))
            continue
        m = UNTICK_ENTRY_RE.match(line)
        if m:
            proved.discard(int(m.group(1)))
    return proved


def deferred_lines(body):
    """``{line_no: decision_id}`` for the acceptance bullets that say they are deferred by a
    decision (``… — deferred by D-0012``). Whether that decision exists is the caller's
    question (:func:`unproved`)."""
    out = {}
    for n, text in enumerate(bullets(body), start=1):
        m = DEFERRED_RE.search(text)
        if m:
            out[n] = m.group(1).upper()
    return out


def _bullet_indexes(lines):
    idxs = []
    inside = False
    for i, line in enumerate(lines):
        stripped = line.rstrip('\n').rstrip('\r')
        if stripped.startswith('## '):
            inside = stripped[3:].strip().lower() == 'acceptance'
            continue
        if inside and _ACCEPTANCE_BULLET_RE.match(stripped):
            idxs.append(i)
    return idxs


def is_ticked(body, line_no):
    """True when the ``line_no``-th acceptance bullet exists and reads ``- [x]``."""
    lines = body.splitlines(keepends=True)
    idxs = _bullet_indexes(lines)
    if not (1 <= line_no <= len(idxs)):
        return False
    return not _UNTICKED_RE.match(lines[idxs[line_no - 1]])


#: Why an acceptance line is unproved — the first word of each ``why`` :func:`unproved` returns.
NO_PROOF = 'no proof'
PATH_MISSING = 'path missing'
NAME_MISSING = 'test name not found'

#: An inline proof on a ticked bullet: ``… — proven by <path>[:line] ["name"][, <path> …]``.
_PROVEN_BY_RE = re.compile(r'\bproven by\b[ \t]*', re.I)
#: One cited path: a backticked or bare token with no space, quote, comma or semicolon in it.
_PROOF_PATH_RE = re.compile(r'`(?P<tick>[^`\s]+)`|(?P<bare>[^\s,;"“”`]+)')
#: An optional test name after a path: ``"name"``, ``“name”`` or ``("name")``.
_PROOF_NAME_RE = re.compile(r'[ \t]*\(?[ \t]*(?:"(?P<a>[^"\n]+)"|“(?P<b>[^”\n]+)”)[ \t]*\)?')
#: The separator between two cited paths: a comma, a semicolon or ``and``.
_PROOF_SEP_RE = re.compile(r'[ \t]*(?:,|;)?[ \t]*(?:\band\b)?[ \t]*', re.I)
_LINE_SUFFIX_RE = re.compile(r':\d+(?::\d+)?$')
#: A path names a file: a directory part, or a file extension. ``the``, ``absence`` are prose.
_PATHLIKE_RE = re.compile(r'(?:/|\.[A-Za-z0-9]+$)')


def inline_proofs(text):
    """``[(path, name_or_None), …]`` — the files a bullet's ``proven by`` cites, in order. Paths
    are separated by ``,``, ``;`` or ``and``; each may carry a ``:line`` suffix (dropped) and a
    quoted test name. The list stops at the first token that is not a path, so ``proven by the
    absence of …`` cites nothing — an absence is never a proof."""
    m = _PROVEN_BY_RE.search(text or '')
    if not m:
        return []
    rest, pos, out = text, m.end(), []
    while pos < len(rest):
        pm = _PROOF_PATH_RE.match(rest, pos)
        if not pm:
            break
        path = pm.group('tick') or pm.group('bare')
        path = path.rstrip('.')
        while path.endswith(')') and path.count(')') > path.count('('):
            path = path[:-1]
        path = _LINE_SUFFIX_RE.sub('', path)
        if not path or not _PATHLIKE_RE.search(path) or path.startswith(('http:', 'https:')):
            break
        pos = pm.end()
        nm = _PROOF_NAME_RE.match(rest, pos)
        name = None
        if nm:
            name = nm.group('a') or nm.group('b')
            pos = nm.end()
        out.append((path, name))
        sm = _PROOF_SEP_RE.match(rest, pos)
        if not sm or sm.end() == pos or not rest[pos:sm.end()].strip():
            break
        pos = sm.end()
    return out


def _repo_file(repo_dir, path):
    """The absolute file ``path`` names inside ``repo_dir``, or None when it is not a file there
    (or would resolve outside the checkout)."""
    if not repo_dir or os.path.isabs(path):
        return None
    root = os.path.realpath(repo_dir)
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full if os.path.isfile(full) else None


def inline_proof(text, repo_dir, _cache=None):
    """``(proved, why)`` for a bullet's inline ``proven by``: proved when at least one cited path
    is a file in the product checkout ``repo_dir`` and, when that citation quotes a test name,
    the name appears in the file. ``why`` names the failure: :data:`NO_PROOF` (nothing cited),
    :data:`PATH_MISSING` or :data:`NAME_MISSING`."""
    cited = inline_proofs(text)
    if not cited:
        return False, NO_PROOF
    cache = _cache if _cache is not None else {}
    missing_name = None
    for path, name in cited:
        full = _repo_file(repo_dir, path)
        if full is None:
            continue
        if not name:
            return True, ''
        if full not in cache:
            try:
                with open(full, encoding='utf-8', errors='replace') as f:
                    cache[full] = f.read()
            except OSError:
                cache[full] = ''
        if name in cache[full]:
            return True, ''
        missing_name = missing_name or (path, name)
    if missing_name:
        return False, f'{NAME_MISSING}: "{missing_name[1]}" in {missing_name[0]}'
    return False, f'{PATH_MISSING}: {", ".join(p for p, _n in cited)}'


def _bullet_states(body):
    """``[(ticked, text), …]`` — :func:`bullets` with each bullet's tick."""
    out = []
    inside = False
    for line in (body or '').splitlines():
        if line.startswith('## '):
            inside = line[3:].strip().lower() == 'acceptance'
            continue
        m = _ACCEPTANCE_BULLET_RE.match(line) if inside else None
        if m:
            out.append((not _UNTICKED_RE.match(line), m.group(1)))
    return out


def unproved(body, register=(), also_proved=(), repo_dir=None, _cache=None, refused=()):
    """``[(line_no, text, why), …]`` — every acceptance line of a Story that nothing proves. The
    one predicate "no test, no done" reads; a line is proved when any of these holds:

    * the card's History records ``proved line N`` (:func:`proved_lines`), or the line is in
      ``also_proved`` (claims the same pass is about to record);
    * the bullet says ``deferred by D-nnnn`` and ``register`` holds that decision;
    * the bullet is ticked and carries an inline ``proven by <path>`` whose path is a file in
      the product checkout ``repo_dir`` (:func:`inline_proof`).

    ``why`` names the reason: :data:`NO_PROOF`, :data:`PATH_MISSING`, :data:`NAME_MISSING`, or a
    deferral whose decision the register lacks. A Story is done only when this is empty; a body
    with no acceptance bullet has nothing to prove.

    ``refused`` is the line numbers a refused claim named this pass: a line in it that is
    otherwise unproved says :data:`PARTIAL_CLAIM` and its marker rather than :data:`NO_PROOF`,
    because a proof *was* offered and named — reading ``no proof`` there would send the operator
    looking for a missing test instead of at the claim (D9). ``refused`` is a mapping of line
    number to the refusal text, or any iterable of line numbers.
    """
    proved = proved_lines(body) | set(also_proved)
    deferred = deferred_lines(body)
    register = {str(d).upper() for d in register}
    refused_why = refused.get if hasattr(refused, 'get') else (
        lambda n: PARTIAL_CLAIM if n in refused else None)
    out = []
    for n, (ticked, text) in enumerate(_bullet_states(body), start=1):
        if n in proved:
            continue
        did = deferred.get(n)
        if did and did in register:
            continue
        if did:
            out.append((n, text, f'deferred by {did}, not in the decision register'))
            continue
        ok, why = inline_proof(text, repo_dir, _cache) if ticked else (False, NO_PROOF)
        if not ok:
            out.append((n, text, refused_why(n) or why))
    return out


#: The ticked half of `_ACCEPTANCE_BULLET_RE`, for `untick`.
_TICKED_RE = re.compile(r'^([ \t]*- )\[[xX]\]')


def untick(body, line_no):
    """``(new_body, changed)`` — flip the ``line_no``-th acceptance bullet from ``- [x]`` back to
    ``- [ ]``: the operator's ``asf untick``, the one reverse of :func:`tick`. Every other
    character stays; ``changed`` is False when the bullet does not exist or is not ticked."""
    lines = body.splitlines(keepends=True)
    idxs = _bullet_indexes(lines)
    if not (1 <= line_no <= len(idxs)):
        return body, False
    i = idxs[line_no - 1]
    new_line, count = _TICKED_RE.subn(r'\1[ ]', lines[i], count=1)
    if not count:
        return body, False
    lines[i] = new_line
    return ''.join(lines), True
