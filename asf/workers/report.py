"""asf.workers.report — the typed REPORT a session ends with, read back.

Every brief ends with the same block (:data:`asf.briefs.build.TAIL`)::

    REPORT
    item: B-0001
    kind: fix-bug
    status: done | partial | blocked
    branch: fix/B-0001
    pushed: yes <sha> | no — <why>
    commits: <sha> <subject> (one per line, or none)
    tests: <what you ran — and its last line>
    left out: <what and why, or none>
    needs writes: <repo paths outside writes: that must change too, or none>
    proves: <the Proves: trailers you wrote, one per line; or none — <why>>

:func:`parse` reads it off a result's text (the last ``REPORT`` block wins). :func:`failure`
names the one failure the report itself declares: ``pushed: no`` — the session says its work
is not on origin. That is a failed session at the source (B-0052: six sessions ended
``success`` with "I'll wait for the background suite and push later"), before health measures
the same thing against git (B-0051). A report that carries no ``pushed:`` line declares
nothing; the evidence rule still applies. :func:`needs_input` names the second thing a report
declares: the question a run raises for a human, either a ``NEEDS OPERATOR:`` line or a
``status: blocked`` report's own words — never a phrase merely mentioned in passing.
"""
import re

FIELDS = ('item', 'kind', 'status', 'branch', 'pushed', 'commits', 'tests', 'left out',
          'needs writes', 'proves', 'ruling', 'blocked_on', 'writes', 'superseded_by',
          'precedent')
HEAD_RE = re.compile(r'^\s*REPORT\s*$', re.M)
FIELD_RE = re.compile(r'^(?P<key>item|kind|status|branch|pushed|commits|tests|left out|needs writes|proves|ruling|blocked_on|writes|superseded_by|precedent)\s*:\s*(?P<value>.*)$', re.I)
NO_RE = re.compile(r'^\s*(no|none|not pushed|unpushed)\b', re.I)
NONE_RE = re.compile(r'^(none|n/a|-|—)$', re.I)
UNPUSHED = 'unpushed work'
#: A line a brief tells every session to print when a human must decide, answer or run something.
OPERATOR_RE = re.compile(r'^\s*NEEDS OPERATOR\s*:\s*(?P<what>.+)$', re.M)
#: A ``NEEDS OPERATOR:`` value that asks nothing: ``none``, ``none — <a note>``, ``omit``, ``n/a``.
NO_QUESTION_RE = re.compile(r'^[\s`*_]*(none|nothing|omit(ted)?|n/a|-|—)(?![\w/])', re.I)
#: A reshape session's answer that its Task stays whole — the reshape brief's own grammar,
#: ``NO SPLIT: <id> does not split along <area> — <why>``, and the older form it replaces,
#: ``NEEDS OPERATOR: <id> does not split along <area> — answer no on the split line``.
NO_SPLIT_RE = re.compile(
    r'^\s*(?:NO SPLIT|NEEDS OPERATOR)\s*:\s*(?P<what>.*\bdoes not split\b.*)$', re.M | re.I)
BLOCKED = 'blocked'
#: The exact command a ``NEEDS OPERATOR:`` line names, backtick-fenced in its own text (B-0042):
#: the one piece of it harvest can act on itself when the command reads only
#: (:func:`asf.harvest.harvest.operator_readonly`), instead of waiting on a person to run it
#: and paste the output back.
OPERATOR_COMMAND_RE = re.compile(r'`([^`\n]+)`')


def operator_command(question):
    """The last backtick-fenced command ``question`` (a declared ``NEEDS OPERATOR:`` question,
    :func:`needs_input`) names, or ``''`` when it names none."""
    found = OPERATOR_COMMAND_RE.findall(str(question or ''))
    return found[-1].strip() if found else ''


def parse(text):
    """``{field: value}`` of the last REPORT block in ``text``, or ``{}``. A field's value runs
    to the next field line; a fenced block's closing ````` ``` ````` ends the report. A line that
    names the field already open — ``proves:``'s own trailers each start ``Proves: …`` — is a
    continuation, not a restart: a real report never states one field twice running."""
    text = str(text or '')
    heads = list(HEAD_RE.finditer(text))
    if not heads:
        return {}
    body = text[heads[-1].end():]
    out, key = {}, None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('```'):
            break
        m = FIELD_RE.match(stripped)
        new_key = ' '.join(m.group('key').lower().split()) if m else None
        if m and new_key != key:
            key = new_key
            out[key] = m.group('value').strip()
        elif key and stripped:
            out[key] = (out[key] + '\n' + stripped).strip()
    return out


def unpushed(report):
    """True when the report says its work is not on origin."""
    value = (report or {}).get('pushed')
    return bool(value) and bool(NO_RE.match(value))


#: A ``pushed:`` value declaring a rebase the factory publishes: ``rebased <sha> — …``.
REBASED_RE = re.compile(r'^\s*rebased\s+`?(?P<sha>[0-9a-f]{7,40})\b', re.I)


def rebased(text):
    """The sha a result's own report declared ``pushed: rebased <sha>`` — the brief's answer to
    a push refused as non-fast-forward, which the factory publishes — or ''."""
    m = REBASED_RE.match((parse(text).get('pushed') or '').split('\n', 1)[0])
    return m.group('sha').lower() if m else ''


def ruling(text):
    """The ``ruling:`` paragraph of an adjudicate session's REPORT, or '' (B-0064): the one
    place a ruling lives — the factory files it on the item's card, the session commits none."""
    return (parse(text).get('ruling') or '').strip()


def precedent(text):
    """The ``precedent:`` line of an adjudicate session's REPORT, or '' — the in-repo precedent
    it established before it parked a question on a person (F-0262)."""
    return (parse(text).get('precedent') or '').strip()


def _claim(value):
    """The value, or None when it is empty or says ``none`` — no claim."""
    value = (value or '').strip()
    return None if not value or NONE_RE.match(value) else value


def ruling_fields(text):
    """``{'blocked_on': id|None, 'writes': [glob, ...]|None, 'superseded_by': id|None}`` off the
    last REPORT block — the ruling's mechanism (F-0090 D10). An absent field and a ``none``
    value are both None: no claim. ``blocked_on`` and ``superseded_by`` name one item, so only
    their first line is the claim: a note run on after the last field (a product's B-1377:
    ``superseded_by: none`` then ``NEEDS OPERATOR: …``) claims nothing."""
    rep = parse(text)
    writes = _claim(rep.get('writes'))
    return {'blocked_on': _claim(_first_line(rep.get('blocked_on'))),
            'writes': writes.split() if writes else None,
            'superseded_by': _claim(_first_line(rep.get('superseded_by')))}


def _first_line(value):
    return (value or '').strip().split('\n', 1)[0]


def failure(text):
    """The failure a result's own report declares, or None: :data:`UNPUSHED` for ``pushed: no``."""
    rep = parse(text)
    if unpushed(rep):
        return UNPUSHED
    return None


def operator_question(text):
    """The first ``NEEDS OPERATOR:`` line in ``text`` that asks something, or ''. A ``none``
    (``none — <a note>``, ``omit``, ``n/a``) asks nothing, nor does a reshape's ``does not split``
    answer (:func:`no_split`)."""
    for m in OPERATOR_RE.finditer(str(text or '')):
        what = m.group('what').strip()
        if NO_QUESTION_RE.match(what) or NO_SPLIT_RE.match(m.group(0)):
            continue  # "none" asks nothing; "does not split" is an answer, not a question
        return what
    return ''


def needs_input(text):
    """The question a result's own text declares, or None: the first ``NEEDS OPERATOR:`` line that
    asks something, else the ``status: blocked`` report's own words (its ``left out:``, else its
    first line).

    Not a question: a ``NEEDS OPERATOR: none`` (``none — <a note>``, ``omit``, ``n/a``) — the
    session said it has none, and its report is judged as what it is (done, nothing to land, a
    named failure); a reshape's ``does not split`` answer (:func:`no_split`); and any report whose
    ``needs writes:`` claims paths — the widening rule answers that (:mod:`asf.feeder.widen`).

    Only these two — both of them things the session *declared*, in the grammar every brief hands
    it (P8). No phrase list: a run that says "waiting for CI" in passing is not waiting for a
    human, and a list of such phrases flags the wrong sessions for ever (D5, D11)."""
    text = str(text or '')
    what = operator_question(text)
    if what:
        if _claims_writes(text):
            return None  # a `needs writes:` claim is answered by the widening rule, not a person
        return what
    rep = parse(text)
    if (rep.get('status') or '').strip().lower() != BLOCKED:
        return None
    if _claims_writes(text):
        return None
    left = _claim(rep.get('left out'))
    if left:
        return left
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else None


def _claims_writes(text):
    """True when the report's own ``needs writes:`` line names paths (never ``left out:`` prose)."""
    source, tokens = footprint_claim(text)
    return source == 'needs writes' and bool(tokens)


def no_split(text):
    """The reshape answer a result's text declares — ``<id> does not split along <area> …`` — or
    ''. Its Task stays whole: the factory takes the answer itself (the item goes back to its own
    row, :mod:`asf.tick.rejudge`), never a question for a person."""
    m = NO_SPLIT_RE.search(str(text or ''))
    return m.group('what').strip() if m else ''


#: The statuses a session reports when its Task is not whole: only these may claim more footprint.
UNFINISHED = ('partial', BLOCKED)
#: A line that opens a section of its own inside a field's run-on value: ``Assumptions:``.
SECTION_RE = re.compile(r'^[A-Z][\w ]{0,40}:\s*$', re.M)


def footprint_claim(text):
    """``(source, tokens)`` — the paths outside ``writes:`` the last REPORT says must change:
    its ``needs writes:`` field when it carries one (``none`` is a claim of none), else — for a
    ``partial`` or ``blocked`` report only — the path-like tokens of ``left out:``. A ``done``
    report whose push went through claims nothing: only a Task not whole, or a refused push,
    widens. ``source`` is
    ``'needs writes'`` | ``'left out'`` | None; the tokens are raw (the caller resolves them
    against the repo, :func:`asf.feeder.widen.resolve`, and drops those already in ``writes:``)."""
    from asf.feeder import widen
    rep = parse(text)
    status = (rep.get('status') or '').strip().lower().split(' ')[0]
    if status == 'done' and rep.get('pushed') and not unpushed(rep):
        # T-0349: a Task done and on origin is whole — a path its report names (an advisory
        # hook row, a note) is never a widening; it goes on to review
        return None, []
    if 'needs writes' in rep:
        value = _claim(rep.get('needs writes'))
        # the field is a path list by contract: every token stands, extension or not (LICENSE)
        tokens = [t.strip('`\'",;') for t in (value or '').split()]
        return 'needs writes', [t for t in dict.fromkeys(tokens) if t and not NONE_RE.match(t)]
    left = _claim(rep.get('left out'))
    if status in UNFINISHED and left:
        left = SECTION_RE.split(left, maxsplit=1)[0]  # a new heading (`Assumptions:`) ends it
        return 'left out', widen.path_tokens(left)
    return None, []
