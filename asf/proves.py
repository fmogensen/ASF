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
    * ``not this Task's``   — the Story is not in the Task's ``stories:``
    * ``no such line``      — ``n`` is outside ``1..len(card_bullets(...))``
    * ``no such test``      — ``tree`` is given and the path before ``::`` is not in it

    ``good`` is the claims with no problem; a Task passes the gate when ``problems`` is empty
    and ``good`` is non-empty (D3)."""
    if not claims:
        return [], ['no claim']
    task_stories = (task or {}).get('stories') or []
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


def claims_on_branch(git, trunk, branch):
    """The claims on ``origin/<branch>`` above ``origin/<trunk>``, read with the injected ``git``
    callable (D11): one ``git log --format=%B`` over that range, parsed."""
    text = git('log', '--format=%B', f'origin/{trunk}..origin/{branch}')
    return parse(text or '')


def tick(body, line_no, note):
    """``(new_body, changed)`` — flip the ``line_no``-th ``## Acceptance`` bullet from
    ``- [ ]`` to ``- [x]`` and leave every other character of the body alone. ``changed`` is
    False when the bullet does not exist or is already ticked: the tick is idempotent and never
    reverses (D7). ``note`` is the text the caller appends to ``## History``; this function does
    not write it."""
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
    lines[i] = new_line
    return ''.join(lines), True


def render(claims):
    """The ``## Proves`` block a pull-request body carries — one bullet per claim, the same text
    the trailer had."""
    return '\n'.join(f'- {c.story} line {c.line} — {c.test}' for c in claims)
