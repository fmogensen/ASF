"""asf.proves — the claim, its grammar and its idempotent tick (F-0040). A ``Proves:`` git
trailer on a Task's commits names the Story acceptance line the test proves; this module parses
it, counts the bullets ``line <n>`` counts against, validates one Task's claims against the
record, reads git for a branch's claims through an injected callable (never ``subprocess``,
D11), and flips a bullet from ``- [ ]`` to ``- [x]`` — once, never in reverse (D7).

Four callers read this module: :mod:`asf.harvest.harvest` (the landing's refusal),
:mod:`asf.tick.step_prs` (the pull-request body), :mod:`asf.evidence.evidence` (which claims
have landed) and ``asf proves`` (the rule's backstop check). Pure functions over text; no
product convention, no branch prefix, no card folder.
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

#: A ``## Acceptance`` checkbox bullet: ``- [ ]``, ``- [x]`` or ``- [X]``, leading whitespace
#: tolerated (P7). The same shape ``asf.record.check.ACCEPTANCE_ITEM_RE`` requires of a Story.
_ACCEPTANCE_BULLET_RE = re.compile(r'^[ \t]*- \[[ xX]\][ \t]+(\S.*)$')

#: The unticked half of `_ACCEPTANCE_BULLET_RE`, for `tick` to flip in place.
_UNTICKED_RE = re.compile(r'^([ \t]*- )\[ \]')


@dataclasses.dataclass(frozen=True)
class Claim:
    story: str      # upper-cased
    line: int       # 1-based, the nth bullet of the Story's ## Acceptance
    test: str       # as written: a path, optionally path::node
    raw: str        # the line as found, for the refusal text


def parse(text):
    """Every claim in ``text``, in first-seen order, duplicates collapsed on
    ``(story, line, test)``. A line that says ``Proves`` but names no id, no line or no test
    yields nothing here and raises nothing."""
    seen = set()
    claims = []
    for m in CLAIM_RE.finditer(text):
        story = m.group('story').upper()
        line = int(m.group('line'))
        test = m.group('test')
        key = (story, line, test)
        if key in seen:
            continue
        seen.add(key)
        claims.append(Claim(story=story, line=line, test=test, raw=m.group(0)))
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
            problems.append(f'unknown story: {claim.raw}')
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


def unproved(body, register=(), also_proved=(), repo_dir=None, _cache=None):
    """``[(line_no, text, why), …]`` — every acceptance line of a Story that nothing proves. The
    one predicate "no test, no done" reads; a line is proved when any of these holds:

    * the card's History records ``proved line N`` (:func:`proved_lines`), or the line is in
      ``also_proved`` (claims the same pass is about to record);
    * the bullet says ``deferred by D-nnnn`` and ``register`` holds that decision;
    * the bullet is ticked and carries an inline ``proven by <path>`` whose path is a file in
      the product checkout ``repo_dir`` (:func:`inline_proof`).

    ``why`` names the reason: :data:`NO_PROOF`, :data:`PATH_MISSING`, :data:`NAME_MISSING`, or a
    deferral whose decision the register lacks. A Story is done only when this is empty; a body
    with no acceptance bullet has nothing to prove."""
    proved = proved_lines(body) | set(also_proved)
    deferred = deferred_lines(body)
    register = {str(d).upper() for d in register}
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
            out.append((n, text, why))
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
