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

:func:`parse` reads it off a result's text (the last ``REPORT`` block wins). :func:`failure`
names the one failure the report itself declares: ``pushed: no`` — the session says its work
is not on origin. That is a failed session at the source (B-0052: six sessions ended
``success`` with "I'll wait for the background suite and push later"), before health measures
the same thing against git (B-0051). A report that carries no ``pushed:`` line declares
nothing; the evidence rule still applies.
"""
import re

FIELDS = ('item', 'kind', 'status', 'branch', 'pushed', 'commits', 'tests', 'left out',
          'needs writes', 'ruling', 'blocked_on', 'writes', 'superseded_by')
HEAD_RE = re.compile(r'^\s*REPORT\s*$', re.M)
FIELD_RE = re.compile(r'^(?P<key>item|kind|status|branch|pushed|commits|tests|left out|needs writes|ruling|blocked_on|writes|superseded_by)\s*:\s*(?P<value>.*)$', re.I)
NO_RE = re.compile(r'^\s*(no|none|not pushed|unpushed)\b', re.I)
NONE_RE = re.compile(r'^(none|n/a|-|—)$', re.I)
UNPUSHED = 'unpushed work'


def parse(text):
    """``{field: value}`` of the last REPORT block in ``text``, or ``{}``. A field's value runs
    to the next field line; a fenced block's closing ````` ``` ````` ends the report."""
    text = str(text or '')
    heads = list(HEAD_RE.finditer(text))
    if not heads:
        return {}
    body = text[heads[-1].end():]
    out, key = {}, None
    for line in body.splitlines():
        if line.strip().startswith('```'):
            break
        m = FIELD_RE.match(line.strip())
        if m:
            key = ' '.join(m.group('key').lower().split())
            out[key] = m.group('value').strip()
        elif key and line.strip():
            out[key] = (out[key] + '\n' + line.strip()).strip()
    return out


def unpushed(report):
    """True when the report says its work is not on origin."""
    value = (report or {}).get('pushed')
    return bool(value) and bool(NO_RE.match(value))


def ruling(text):
    """The ``ruling:`` paragraph of an adjudicate session's REPORT, or '' (B-0064): the one
    place a ruling lives — the factory files it on the item's card, the session commits none."""
    return (parse(text).get('ruling') or '').strip()


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


#: The statuses a session reports when its Task is not whole: only these may claim more footprint.
UNFINISHED = ('partial', 'blocked')
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
