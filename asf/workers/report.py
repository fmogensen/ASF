"""asf.workers.report — the typed report a session ends with: its schema, its reader, its contract.

A session ends with its prose and, beside it, one fenced block whose info line carries the token
``asf-report``, holding one JSON object. This module owns that object: the common fields, one
field table per job kind, the checker, the reader (:func:`typed`), the renderer (:func:`render`)
and the text a brief prints (:func:`contract`). It imports ``json`` and ``re`` and nothing of
ASF's, so every module that reads a result may import it.

The prose ``REPORT`` block a brief also asks for is for a human reading a log: nothing here
reads it (D11). :func:`failure` names the one failure the report itself declares — ``pushed:
"no"`` — a failed session at the source (B-0052), before health measures the same thing against
git (B-0051). A report that will not parse declares nothing here: :func:`check` is what catches
it, where the kind is known.
"""
import json
import re

REQ = object()                    #: no default: the field must be there
STATUSES = ('done', 'partial', 'blocked')
PUSHED = ('yes', 'rebased', 'no')
FENCE_TOKEN = 'asf-report'
UNPUSHED = 'unpushed work'        #: health matches this end_reason prefix (B-0051)
BAD_REPORT = 'bad report'


class ReportError(ValueError):
    """The one sentence naming what is wrong with a report."""


#: field -> (types, default, the one line the contract prints for it). A tuple of ``_CHECK``
#: names is a type list; a tuple of plain strings is an enumeration of allowed values. An
#: instruction that opens with a JSON literal is the sample the contract prints for the field.
COMMON = {
    'item':    (('str',), REQ, 'the card id this session worked, as the brief gave it'),
    'kind':    (('str',), REQ, 'the job kind, as the brief gave it'),
    'status':  (STATUSES, REQ, 'done | partial | blocked'),
    'branch':  (('str', 'null'), REQ, 'the branch you worked, or null for a job with none'),
    'pushed':  (PUSHED, REQ, 'yes | rebased (the factory publishes) | no'),
    'sha':     (('str', 'null'), None, 'what origin/<branch> now points at; required unless pushed is no'),
    'why':     (('str', 'null'), None, 'why nothing was pushed; required when pushed is no'),
    'commits': (('list',), REQ, '[{"sha": "...", "subject": "..."}] — [] when you committed nothing'),
    'tests':   (('list',), REQ, '[{"command": "...", "last_line": "..."}] — [] when you ran nothing'),
    'left_out': (('list',), REQ, '["<what and why>", ...] — [] when nothing was left out'),
    'needs_operator': (('list',), REQ, '["<what> — <the command or the answer needed>", ...] — [] when nothing needs a human'),
}

KIND_FIELDS = {
    'spec': {
        'spec': (('str',), REQ, 'the spec path you wrote'),
        'sections': (('list',), REQ, '["<the sections you wrote>"]'),
        'stories': (('list',), REQ, '[{"id": "S-0000", "title": "...", "acceptance": "..."}] — the Stories the spec carries'),
    },
    'plan': {
        'plan': (('str',), REQ, 'the plan path you wrote'),
        'tasks': (('list',), REQ, '[{"id": "T-0000", "title": "...", "stories": ["S-0000"], "writes": ["path"], "wave": 1}] — the Tasks, in wave order'),
        'coverage': (('str',), REQ, 'the coverage line: every Story and the Task that covers it'),
    },
    'coder': {
        'files': (('list',), REQ, '["<path>"] — the files you wrote'),
        'assumptions': (('list',), REQ, '["<assumption>"] — the assumptions you recorded'),
    },
    'review': {
        'review': (('str',), REQ, 'the review document path you wrote'),
        'round': (('int',), REQ, '1 — the round of this review'),
        'verdict': (('approved', 'changes requested'), REQ, 'approved | changes requested'),
        'findings': (('list',), REQ, '[{"file": "path", "fix": "..."}] — the failing rows'),
    },
    'fixer': {
        'files': (('list',), REQ, '["<path>"] — the files you changed'),
        'addressed': (('list',), REQ, '["<C-id: what you did>"] — one line per C'),
    },
    'fix-bug': {
        'files': (('list',), REQ, '["<path>"] — the files you changed'),
        'cause': (('str',), REQ, 'what you changed and why it was the cause'),
        'regression_test': (('str',), REQ, "the regression test's name and the run's last line"),
    },
    'rebase': {
        'files': (('list',), REQ, '["<path>"] — the files you resolved'),
    },
    'close': {
        'line': (('str',), REQ, 'the one line that closes the item'),
        'reopen': (('bool',), REQ, 'false — whether the item should be re-opened'),
    },
    'adjudicate': {
        'ruling': (('str',), REQ, 'the ruling: what was disputed, what now holds, what changes (non-empty)'),
        'upheld': (('list',), [], '["<finding>"] — the findings you upheld'),
        'overruled': (('list',), [], '["<finding>"] — the findings you overruled'),
        'blocked_on': (('str', 'null'), None, 'the id this item must wait for, or null'),
        'writes': (('list', 'null'), None, '["<glob>"] — the corrected footprint, or null'),
        'superseded_by': (('str', 'null'), None, 'the id that replaces this item, or null'),
    },
    'correct': {
        'fixed': (('str',), REQ, 'what the failure named and what you did'),
        'files': (('list',), REQ, '["<path>"] — the files you changed'),
    },
    'groom': {
        'answers': (('str',), REQ, 'the answers file you wrote'),
        'answered': (('int',), REQ, '0 — how many questions you answered'),
        'asked': (('int',), REQ, '0 — how many you sent to NEEDS OPERATOR'),
    },
    'reshape': {
        'parts': (('list',), REQ, '[{"id": "T-0000", "title": "...", "writes": ["path"]}] — the parts, with their writes'),
        'coverage': (('str',), REQ, 'the coverage line'),
    },
    'spec-plan': {
        'spec': (('str',), REQ, 'the spec path you wrote'),
        'plan': (('str',), REQ, 'the plan path you wrote — the spec and the plan are one document'),
        'stories': (('list',), REQ, '[{"id": "S-0000", "title": "...", "acceptance": "..."}] — the Stories the document carries'),
        'tasks': (('list',), REQ, '[{"id": "T-0000", "title": "...", "stories": ["S-0000"], "writes": ["path"], "wave": 1}] — the Tasks, in wave order'),
        'coverage': (('str',), REQ, 'the coverage line: every Story and the Task that covers it'),
    },
    'direct': {
        'files': (('list',), REQ, '["<path>"] — the files you wrote'),
    },
}

_CHECK = {
    'str': lambda v: isinstance(v, str),
    'int': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'bool': lambda v: isinstance(v, bool),
    'list': lambda v: isinstance(v, list),
    'dict': lambda v: isinstance(v, dict),
    'null': lambda v: v is None,
}


def _is_types(types):
    return all(t in _CHECK for t in types)


def _check(name, types, value):
    """The sentence refusing ``value`` for field ``name``, or None."""
    if _is_types(types):
        if any(_CHECK[t](value) for t in types):
            return None
        return f"'{name}' must be {' or '.join(types)} — got {type(value).__name__}"
    if isinstance(value, str) and value in types:
        return None
    return f"'{name}' must be one of {', '.join(types)} — got {value!r}"


def schema(kind):
    """``COMMON`` then the kind's own fields. ``ReportError`` on an unknown kind."""
    if kind not in KIND_FIELDS:
        raise ReportError(f'no report schema for kind {kind!r}')
    return {**COMMON, **KIND_FIELDS[kind]}


def fence(text):
    """The body of the last fenced block of ``text`` whose info line carries
    :data:`FENCE_TOKEN`, else None. A fence is three backticks or more and closes on the first
    line of at least that run; a block that never closes (a cut-short result) does not count."""
    found, ticks, info, body = None, 0, '', []
    for line in str(text or '').splitlines():
        if not ticks:
            m = re.match(r'^\s{0,3}(`{3,})([^`]*)$', line)
            if m:
                ticks, info, body = len(m.group(1)), m.group(2), []
            continue
        m = re.match(r'^\s{0,3}(`{3,})\s*$', line)
        if m and len(m.group(1)) >= ticks:
            if FENCE_TOKEN in info.split():
                found = '\n'.join(body)
            ticks = 0
        else:
            body.append(line)
    return found


def _blank(value):
    return value is None or (isinstance(value, (str, list)) and not value)


def typed(text, kind=None):
    """The parsed, checked report object of ``text``. ``ReportError`` names what is wrong. With
    ``kind=None`` only :data:`COMMON` is checked and unknown keys are allowed — the shape a caller
    that does not know the kind needs."""
    body = fence(text)
    if body is None:
        raise ReportError(f'no ```json {FENCE_TOKEN} block in the final message')
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise ReportError(f'the {FENCE_TOKEN} block is not JSON: {e}')
    if not isinstance(obj, dict):
        raise ReportError(f'the {FENCE_TOKEN} block is not an object')
    fields = schema(kind) if kind is not None else COMMON
    if kind is not None:
        unknown = sorted(set(obj) - set(fields))
        if unknown:
            raise ReportError(f"unknown key(s) {', '.join(unknown)}")
    for name, (types, default, _line) in fields.items():
        if name not in obj:
            if default is REQ:
                raise ReportError(f"missing required key '{name}'")
            continue
        bad = _check(name, types, obj[name])
        if bad:
            raise ReportError(bad)
    if obj['pushed'] in ('yes', 'rebased') and _blank(obj.get('sha')):
        raise ReportError(f"'pushed' is {obj['pushed']} but 'sha' is missing")
    if obj['pushed'] == 'no' and _blank(obj.get('why')):
        raise ReportError("'pushed' is no but 'why' is missing")
    if obj['status'] == 'blocked' and _blank(obj['needs_operator']) and _blank(obj['left_out']):
        raise ReportError("'status' is blocked but neither 'needs_operator' nor 'left_out' names why")
    if kind == 'adjudicate' and _blank(obj['ruling'].strip()):
        raise ReportError("'ruling' must not be empty")
    return obj


def check(kind, text):
    """The ``ReportError``'s sentence, or None when the report is good. Never raises."""
    try:
        typed(text, kind)
    except ReportError as e:
        return str(e)
    return None


def _common(text):
    try:
        return typed(text, None)
    except ReportError:
        return {}


def failure(text):
    """:data:`UNPUSHED` when the typed report says ``pushed: no``, else None — an unreadable
    report is not a failure here (D6)."""
    return UNPUSHED if _common(text).get('pushed') == 'no' else None


def ruling(text):
    """The typed report's ``ruling``, stripped, or '' (B-0064): the one place a ruling lives —
    the factory files it on the item's card, the session commits none."""
    value = _common(text).get('ruling')
    return value.strip() if isinstance(value, str) else ''


NONE_RE = re.compile(r'^(none|n/a|-|—)$', re.I)


def _claim(value):
    """The value, or None when it is empty or says ``none`` — no claim."""
    value = value.strip() if isinstance(value, str) else ''
    return None if not value or NONE_RE.match(value) else value


def ruling_fields(text):
    """``{'blocked_on': id|None, 'writes': [glob, ...]|None, 'superseded_by': id|None}`` off the
    typed report — the ruling's mechanism (F-0090 D10). An absent field, an empty one and a
    ``none`` value are all None: no claim."""
    obj = _common(text)
    writes = obj.get('writes')
    if isinstance(writes, str):
        writes = writes.split()
    writes = [w.strip() for w in writes or [] if _claim(w)] if isinstance(writes, list) else []
    return {'blocked_on': _claim(obj.get('blocked_on')),
            'writes': writes or None,
            'superseded_by': _claim(obj.get('superseded_by'))}


def summary(obj):
    """The one line a session's ``reason`` records: ``'<status>'``, ``'<status> — needs
    operator: <first>'`` when blocked, ``'<status> — <why>'`` when nothing was pushed."""
    obj = obj if isinstance(obj, dict) else {}
    status = str(obj.get('status') or '?')
    needs = obj.get('needs_operator')
    if status == 'blocked' and isinstance(needs, list) and needs:
        line = f'{status} — needs operator: {needs[0]}'
    elif obj.get('pushed') == 'no' and obj.get('why'):
        line = f"{status} — {obj['why']}"
    else:
        line = status
    return ' '.join(line.split())


def _placeholder(name, types, kind, fields):
    """What ``render`` writes for a required field the caller left out."""
    if name == 'item':
        return 'X-0000'
    if name == 'kind':
        return kind
    if name == 'status':
        return 'done'
    if name == 'pushed':
        return 'yes'
    if name == 'sha':
        return None if fields.get('pushed') == 'no' else 'abc1234'
    if name == 'why':
        return 'nothing was pushed' if fields.get('pushed') == 'no' else None
    if name == 'needs_operator':
        blocked = fields.get('status') == 'blocked' and _blank(fields.get('left_out'))
        return ['NEEDS OPERATOR: placeholder'] if blocked else []
    if not _is_types(types):
        return types[0]
    if 'null' in types:
        return None
    return {'str': name, 'int': 0, 'bool': False, 'list': [], 'dict': {}}[types[0]]


def render(kind, **fields):
    """A fenced, schema-valid ``json asf-report`` block. Every field the caller did not give takes
    its default or a documented placeholder (``item`` → ``X-0000``, a list → ``[]``, ``pushed`` →
    ``yes`` with a ``sha``), so a fixture names only what its test is about. ``ReportError`` on a
    field the kind's schema does not have."""
    spec = schema(kind)
    unknown = sorted(set(fields) - set(spec))
    if unknown:
        raise ReportError(f"unknown key(s) {', '.join(unknown)} for kind {kind!r}")
    obj = {}
    for name, (types, default, _line) in spec.items():
        if name in fields:
            obj[name] = fields[name]
            continue
        given = {**obj, **fields}
        value = _placeholder(name, types, kind, given) \
            if default is REQ or name in ('sha', 'why') else default
        obj[name] = list(value) if isinstance(value, list) else value
    return f'```json {FENCE_TOKEN}\n{json.dumps(obj, indent=2)}\n```\n'


def _sample(types, default, line):
    """What the contract prints as a field's value: the JSON literal its instruction opens with,
    else the instruction as a ``<placeholder>`` (the words themselves for an enumeration)."""
    if not _is_types(types):
        return line
    try:
        literal, _ = json.JSONDecoder().raw_decode(line)
    except ValueError:
        literal = REQ
    if literal is not REQ and any(_CHECK[t](literal) for t in types):
        return literal
    if default is not REQ and 'required unless' not in line:
        return default
    if 'str' in types:
        return f'<{line}>'
    return {'int': 0, 'bool': False, 'list': [], 'dict': {}}[types[0]]


def contract(kind):
    """The text a brief prints for ``kind``, rendered from its schema — no template prints one.
    :data:`COMMON` comes first, so every kind's contract shares its first eleven fields."""
    spec = schema(kind)
    rows = [f'  {json.dumps(name)}: {json.dumps(_sample(*spec[name]), ensure_ascii=False)}'
            for name in spec]
    legend = '\n'.join(f'- {name}: {spec[name][2]}' for name in spec)
    return ('Then, beside it, the typed report — this is the one the factory reads:\n\n'
            f'```json {FENCE_TOKEN}\n{{\n' + ',\n'.join(rows) + '\n}\n```\n\n'
            f'What goes in each field:\n\n{legend}\n\n'
            'It must parse and it must match this shape exactly — an unreadable report comes '
            'back to you as a correction, and nothing else about your work is re-done.\n')


# ---- the prose fallback: still read off the prose block when there is no fence to read
# instead; it is not the typed report's business. :func:`footprint_claim` stands on it below;
# a report that never demands the typed contract (nothing does yet — D11's own Task 1 note)
# leaves `asf.workers.health.push_retry` and `asf.workers.lifecycle.overruling` standing on it
# too, the same way, for the same reason.

FIELD_RE = re.compile(
    r'^(?P<key>item|kind|status|branch|pushed|commits|tests|left out|needs writes'
    r'|ruling|blocked_on|writes|superseded_by)\s*:\s*(?P<value>.*)$', re.I)
HEAD_RE = re.compile(r'^\s*REPORT\s*$', re.M)
#: The statuses a session reports when its Task is not whole: only these may claim more footprint.
UNFINISHED = ('partial', 'blocked')
#: A line that opens a section of its own inside a field's run-on value: ``Assumptions:``.
SECTION_RE = re.compile(r'^[A-Z][\w ]{0,40}:\s*$', re.M)


def _prose(text):
    """``{field: value}`` of the last prose REPORT block — the fallback :func:`footprint_claim`,
    :func:`asf.workers.health.push_retry` and :func:`asf.workers.lifecycle.overruling` keep for a
    report with no fence to read instead."""
    text = str(text or '')
    heads = list(HEAD_RE.finditer(text))
    if not heads:
        return {}
    out, key = {}, None
    for line in text[heads[-1].end():].splitlines():
        if line.strip().startswith('```'):
            break
        m = FIELD_RE.match(line.strip())
        if m:
            key = ' '.join(m.group('key').lower().split())
            out[key] = m.group('value').strip()
        elif key and line.strip():
            out[key] = (out[key] + '\n' + line.strip()).strip()
    return out


def footprint_claim(text):
    """``(source, tokens)`` — the paths outside ``writes:`` the last REPORT says must change:
    its ``needs writes:`` field when it carries one (``none`` is a claim of none), else — for a
    ``partial`` or ``blocked`` report only — the path-like tokens of ``left out:``. A ``done``
    report whose push went through claims nothing: only a Task not whole, or a refused push,
    widens. ``source`` is
    ``'needs writes'`` | ``'left out'`` | None; the tokens are raw (the caller resolves them
    against the repo, :func:`asf.feeder.widen.resolve`, and drops those already in ``writes:``)."""
    from asf.feeder import widen
    rep = _prose(text)
    status = (rep.get('status') or '').strip().lower().split(' ')[0]
    pushed = (rep.get('pushed') or '').strip().lower().split(' ')[0]
    if status == 'done' and pushed in ('yes', 'rebased'):
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
