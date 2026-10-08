"""asf.workers.report — the typed REPORT a session ends with, read back.

The module owns the report: the common fields every kind shares, one field table per job kind
(:data:`KIND_FIELDS`, keyed by :data:`asf.briefs.build.KINDS`), the checker, the reader, the
renderer and the contract text a brief prints. It imports ``json`` and ``re`` and nothing of
ASF's, so every module that reads a result may import it.

The typed report is a fenced block in a session's final message, its info line carrying
:data:`FENCE_TOKEN` (``json asf-report``), holding one JSON object — beside the prose ``REPORT``
block a brief still prints (read by nobody: a human reads it in a log). :func:`typed` reads the
object off the **last** such fence, checks it against the kind's schema (required keys, types,
enumerations, and D5's three cross-field rules), and raises :class:`ReportError` with one
sentence naming what is wrong. :func:`check` is the same read, never raising. :func:`render`
is the inverse — a fenced, schema-valid block from keyword fields, used by ``FakeRuntime`` and by
every fixture that scripts a result. :func:`contract` is the text a brief prints for a kind,
generated from its schema.

Below a clear section break, the module keeps a **prose fallback**: ``_prose(text)`` carries the
old, deleted ``parse``'s body, unexported, standing in for the two public names (``parse``,
``unpushed``) this card deletes (F-0025 D11) — not as a second reading path for the typed report
(there is none: :func:`check`, :func:`failure`, :func:`ruling`, :func:`ruling_fields` and
:func:`summary` read the typed object only), but because a handful of callers, inside and outside
this module, still read a ``{field: value}`` off the prose ``REPORT`` block the brief prints and
have no typed-field equivalent to move to (F-0025 replan, PD-READERS): ``footprint_claim``,
``no_split``, ``needs_input`` and ``rebased`` here, and ``health.push_retry``,
``lifecycle.overruling``, ``lifecycle.delivered_off_branch``, ``relaunch.terminal``,
``lane.report_status``, ``lane.run_heads``, ``trunkclose.own_shas``/``trunk_sha`` and
``rejudge``'s status read outside it. :data:`NO_RE` stays public for ``push_retry``'s own match.
"""
import json
import re


class ReportError(Exception):
    """A typed report is unreadable, or the kind's schema refuses it — one sentence naming why."""


REQ = object()                    #: no default: the field must be there
STATUSES = ('done', 'partial', 'blocked')
PUSHED = ('yes', 'rebased', 'no')
FENCE_TOKEN = 'asf-report'
UNPUSHED = 'unpushed work'        #: unchanged — health matches this end_reason prefix
BAD_REPORT = 'bad report'

#: A type name is a key of :data:`_CHECK`; a tuple of plain strings that are not type names is an
#: enumeration of the values the field may hold (how ``status`` and ``pushed`` are declared).
#: field -> (types, default, the one line the contract prints for it)
COMMON = {
    'item':           (('str',), REQ, 'the card id this session worked, as the brief gave it'),
    'kind':           (('str',), REQ, 'the job kind, as the brief gave it'),
    'status':         (STATUSES, REQ, 'done | partial | blocked'),
    'branch':         (('str', 'null'), REQ, 'the branch you worked, or null for a job with none'),
    'pushed':         (PUSHED, REQ, 'yes | rebased (the factory publishes) | no'),
    'sha':            (('str', 'null'), None,
                        'what origin/<branch> now points at; required unless pushed is no'),
    'why':            (('str', 'null'), None, 'why nothing was pushed; required when pushed is no'),
    'commits':        (('list',), REQ,
                        '[{"sha": "...", "subject": "..."}] — [] when you committed nothing'),
    'tests':          (('list',), REQ,
                        '[{"command": "...", "last_line": "..."}] — [] when you ran nothing'),
    'left_out':       (('list',), REQ, '["<what and why>", ...] — [] when nothing was left out'),
    'needs_operator': (('list',), REQ, '["<what> — <the command or the answer needed>", ...]'),
}

#: One entry per job kind (:data:`asf.briefs.build.KINDS` — not imported here, D3/PD-KINDS: the
#: table is the nineteen words written out, and ``tests.test_report`` is what holds the two
#: equal), each field ``(types, default, instruction)`` beyond :data:`COMMON`.
KIND_FIELDS = {
    'spec': {
        'spec':     (('str',), REQ, 'the path you wrote'),
        'sections': (('list',), REQ, 'the sections you wrote'),
        'stories':  (('list',), REQ,
                     '[{"id": "S-0000", "title": "...", "acceptance": "..."}] — the Story count'),
    },
    'plan': {
        'plan':     (('str',), REQ, 'the plan path you wrote'),
        'tasks':    (('list',), REQ,
                     '[{"id": "...", "title": "...", "stories": [...], "writes": [...], '
                     '"wave": n}] — the Task count and the wave order'),
        'coverage': (('str',), REQ, 'the coverage line'),
    },
    'coder': {
        'files':       (('list',), REQ, 'the files written'),
        'assumptions': (('list',), REQ, 'the assumptions recorded'),
    },
    'review': {
        'review':   (('str',), REQ, 'the review path you wrote'),
        'round':    (('int',), REQ, 'the review round'),
        'verdict':  (('approved', 'changes requested'), REQ, 'approved | changes requested'),
        'findings': (('list',), REQ, '[{"file": "...", "fix": "..."}] — the failing rows'),
    },
    'fixer': {
        'files':     (('list',), REQ, 'the files written'),
        'addressed': (('list',), REQ, 'one line per C, the finding it answers'),
    },
    'fix-bug': {
        'files':            (('list',), REQ, 'the files changed'),
        'cause':            (('str',), REQ, 'what caused the bug'),
        'regression_test':  (('str',), REQ, "the regression test's name"),
    },
    'rebase': {
        'files': (('list',), REQ, 'the files resolved'),
    },
    'close': {
        'line':   (('str',), REQ, 'the one line'),
        'reopen': (('bool',), REQ, 'whether the item should be re-opened'),
    },
    'adjudicate': {
        'ruling':        (('str',), REQ, 'the ruling, filled in'),
        'upheld':        (('list',), REQ, 'the findings upheld'),
        'overruled':     (('list',), REQ, 'the findings overruled'),
        'blocked_on':    (('str', 'null'), None, 'the id this item must wait for, or none'),
        'writes':        (('list', 'null'), None,
                           'the corrected footprint, as a list of globs, or none'),
        'superseded_by': (('str', 'null'), None, 'the id that replaces this item, or none'),
    },
    'correct': {
        'fixed': (('str',), REQ, 'what the failure named and what you did'),
        'files': (('list',), REQ, 'the files you touched'),
    },
    'groom': {
        'answers':  (('str',), REQ, 'the answers file you wrote'),
        'answered': (('int',), REQ, 'how many questions you answered'),
        'asked':    (('int',), REQ, 'how many you sent to NEEDS OPERATOR'),
    },
    'reshape': {
        'parts':    (('list',), REQ,
                     '[{"id": "...", "title": "...", "writes": [...]}] — the part ids with '
                     'their writes'),
        'coverage': (('str',), REQ, 'the coverage line'),
    },
    # PD-KINDS: landed since the approved spec's table was written, each derived one for one
    # from its own `Final message:` line.
    'spec-amend': {
        'spec':     (('str',), REQ, 'the path you wrote'),
        'sections': (('list',), REQ, 'the sections you wrote'),
        'stories':  (('list',), REQ,
                     '[{"id": "S-0000", "title": "...", "acceptance": "..."}] — the Story count'),
    },
    'groom-clerk': {
        'answers':  (('str',), REQ, 'the answers file you wrote'),
        'answered': (('int',), REQ, 'how many questions you answered'),
        'asked':    (('int',), REQ, 'how many you sent to NEEDS OPERATOR'),
    },
    'spec-plan': {
        'spec':     (('str',), REQ, 'the spec path you wrote — one of the two paths'),
        'plan':     (('str',), REQ, 'the plan path you wrote — the other of the two paths'),
        'stories':  (('list',), REQ, 'the Stories — the Story count'),
        'tasks':    (('list',), REQ, 'the Tasks — the Task count'),
        'coverage': (('str',), REQ, 'the coverage line'),
    },
    'direct': {
        'files': (('list',), REQ, 'the files written'),
    },
    'delivery-plan': {
        'plan':     (('str',), REQ, 'the plan path you wrote'),
        'delivers': (('list',), REQ,
                     'the ids, in build order — the item count is len(delivers)'),
    },
    'delivery-code': {
        'items': (('list',), REQ,
                  '[{"id": "...", "status": "committed | left_out | already_landed"}] — '
                  'one line per item'),
    },
    'replan': {
        'replan': (('str',), REQ, 'the replan path'),
        'tasks':  (('list',), REQ,
                   '[{"id": "...", "status": "rewritten | new | dropped"}] — per Task'),
    },
}

#: type name -> predicate. ``bool`` is checked ahead of ``int`` nowhere here because no field
#: declares both; a plain ``isinstance(v, bool)`` is enough since no kind needs to tell a bool
#: from an int.
_CHECK = {
    'str': lambda v: isinstance(v, str),
    'int': lambda v: isinstance(v, int) and not isinstance(v, bool),
    'bool': lambda v: isinstance(v, bool),
    'list': lambda v: isinstance(v, list),
    'dict': lambda v: isinstance(v, dict),
    'null': lambda v: v is None,
}


def _is_types(options):
    return all(t in _CHECK for t in options)


def _check(key, options, value):
    """Raises :class:`ReportError` when ``value`` does not satisfy ``options`` — either a tuple
    of :data:`_CHECK` type names, or an enumeration of the literal values allowed."""
    if _is_types(options):
        if not any(_CHECK[t](value) for t in options):
            raise ReportError(f"'{key}' must be a {' or '.join(options)} — got {value!r}")
    elif value not in options:
        raise ReportError(f"'{key}' must be one of {', '.join(options)} — got {value!r}")


def schema(kind):
    """``{field: (types, default, instruction)}`` — :data:`COMMON` merged with ``kind``'s own.
    Raises :class:`ReportError` for a kind with no table."""
    if kind not in KIND_FIELDS:
        raise ReportError(f"no report schema for kind {kind!r}")
    return dict(COMMON, **KIND_FIELDS[kind])


_FENCE_OPEN_RE = re.compile(r'^(`{3,})(.*)$')


def fence(text):
    """The body of the **last** fenced block in ``text`` whose info line carries
    :data:`FENCE_TOKEN`, else ``None``. A fence is three backticks or more; its closing line is
    the first line, after it, of only backticks, at least as many (CommonMark: a fence does not
    nest, so a line that merely starts with enough backticks but carries more after them — a
    nested fence's own opener — is content, not a close)."""
    lines = str(text or '').splitlines()
    found, i, n = None, 0, len(lines)
    while i < n:
        m = _FENCE_OPEN_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        ticks, info = m.groups()
        close_re = re.compile(r'^`{%d,}$' % len(ticks))
        j = i + 1
        while j < n and not close_re.match(lines[j].strip()):
            j += 1
        if FENCE_TOKEN in info:
            found = '\n'.join(lines[i + 1:j])
        i = j + 1
    return found


def typed(text, kind=None):
    """The parsed, checked object off the last ``asf-report`` fence of ``text``. With ``kind``
    given, every field of :func:`schema` is checked (required keys, types/enumerations, no
    unknown key) and D5's three cross-field rules apply; with ``kind=None`` only :data:`COMMON`
    is checked and an unrecognized key is left as-is — the shape a caller who does not know the
    kind (:func:`failure`, :func:`ruling`) needs. Raises :class:`ReportError` naming what is
    wrong; never returns a value that failed a check."""
    body = fence(text)
    if body is None:
        raise ReportError(f'no ```json {FENCE_TOKEN} block in the final message')
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise ReportError(f'the {FENCE_TOKEN} block is not JSON: {e}') from None
    if not isinstance(obj, dict):
        raise ReportError(f'the {FENCE_TOKEN} block is not an object')
    fields = COMMON if kind is None else schema(kind)
    if kind is not None:
        unknown = sorted(set(obj) - set(fields))
        if unknown:
            raise ReportError(f"unknown key(s) {', '.join(unknown)}")
    for key, (types, default, _instr) in fields.items():
        if key not in obj:
            if default is REQ:
                raise ReportError(f"missing required key '{key}'")
            obj[key] = default
        else:
            _check(key, types, obj[key])
    pushed = obj.get('pushed')
    if pushed in ('yes', 'rebased') and not obj.get('sha'):
        raise ReportError(f"'pushed' is {pushed} but 'sha' is missing")
    if pushed == 'no' and not obj.get('why'):
        raise ReportError("'pushed' is no but 'why' is missing")
    if obj.get('status') == 'blocked' and not obj.get('needs_operator') and not obj.get('left_out'):
        raise ReportError("'status' is blocked but 'needs_operator' and 'left_out' are both empty")
    return obj


def check(kind, text):
    """``typed(text, kind)``'s :class:`ReportError` sentence, or ``None`` when the report is
    good. Never raises."""
    try:
        typed(text, kind)
    except ReportError as e:
        return str(e)
    return None


def failure(text):
    """``UNPUSHED`` when the typed report's ``pushed`` is ``no``, else ``None`` — including for a
    report that will not parse (D6: :func:`check` is what catches that, where the kind is
    known)."""
    try:
        obj = typed(text, None)
    except ReportError:
        return None
    return UNPUSHED if obj.get('pushed') == 'no' else None


def ruling(text):
    """The typed report's ``ruling`` field, stripped, or ``''`` — never the prose."""
    try:
        obj = typed(text, None)
    except ReportError:
        return ''
    return str(obj.get('ruling') or '').strip()


def ruling_fields(text):
    """``{'blocked_on': id|None, 'writes': [glob, ...]|None, 'superseded_by': id|None}`` off the
    typed report's own fields (F-0025 replan PD5) — never the prose."""
    try:
        obj = typed(text, None)
    except ReportError:
        obj = {}
    writes = obj.get('writes')
    return {'blocked_on': obj.get('blocked_on') or None,
            'writes': list(writes) if writes else None,
            'superseded_by': obj.get('superseded_by') or None}


def summary(obj):
    """The one line :mod:`asf.metrics.metrics` records as a session's ``reason``: ``'<status>'``,
    ``'<status> — <why>'`` when ``pushed`` is ``no``, ``'<status> — needs operator: <first>'``
    when ``status`` is ``blocked``."""
    obj = obj or {}
    status = str(obj.get('status') or '')
    if obj.get('pushed') == 'no' and obj.get('why'):
        return f'{status} — {obj["why"]}'
    if status == 'blocked':
        ops = obj.get('needs_operator') or []
        left = obj.get('left_out') or []
        first = ops[0] if ops else (left[0] if left else '')
        if first:
            return f'{status} — needs operator: {first}'
    return status


#: Documented placeholders :func:`render` fills for a required field the caller did not give,
#: by field name (checked before type-based defaults).
_PLACEHOLDER_BY_NAME = {
    'item': 'X-0000', 'branch': 'the-branch', 'sha': 'abc1234', 'spec': 'the spec path',
    'plan': 'the plan path', 'review': 'the review path', 'replan': '',
    'ruling': 'the hold was examined and the finding stands', 'round': 1, 'answered': 0,
    'asked': 0,
}


def _placeholder(key, kind, types, default):
    if key == 'kind':
        return kind
    if key in _PLACEHOLDER_BY_NAME:
        return _PLACEHOLDER_BY_NAME[key]
    if key == 'status':
        return 'done'
    if key == 'pushed':
        return 'yes'
    if not _is_types(types):
        return types[0]  # an enumeration: its first allowed value
    for t, value in (('list', []), ('dict', {}), ('bool', False), ('int', 0), ('str', ''),
                     ('null', None)):
        if t in types:
            return value
    return default if default is not REQ else None


def render(kind, **fields):
    """A fenced, schema-valid ``asf-report`` block for ``kind``. Every required field the caller
    did not give takes a documented placeholder, so a fixture names only what its test is about.
    Raises :class:`ReportError` on a field the kind's schema does not have — a fixture cannot
    invent one either."""
    table = schema(kind)
    unknown = sorted(set(fields) - set(table))
    if unknown:
        raise ReportError(f"kind {kind!r} has no field(s) {', '.join(unknown)}")
    obj = {}
    for key, (types, default, _instr) in table.items():
        if key in fields:
            obj[key] = fields[key]
        elif default is REQ:
            obj[key] = _placeholder(key, kind, types, default)
        # else: an optional field with no override is simply left out
    # D5's three cross-field rules, satisfied with a placeholder when the caller's own fields
    # leave them unmet — `sha`/`why`/`needs_operator` have no REQ default (:data:`COMMON`), so
    # the loop above never fills them on its own.
    if obj.get('pushed') in ('yes', 'rebased') and not obj.get('sha'):
        obj['sha'] = _PLACEHOLDER_BY_NAME['sha']
    if obj.get('pushed') == 'no' and not obj.get('why'):
        obj['why'] = 'not yet pushed'
    if obj.get('status') == 'blocked' and not obj.get('needs_operator') and not obj.get('left_out'):
        obj['needs_operator'] = ['see the report']
    return f'```json {FENCE_TOKEN}\n{json.dumps(obj, indent=2)}\n```'


#: How many of :data:`COMMON`'s own fields the contract prints before a kind's own — the twelve
#: nineteen contracts share this prefix verbatim (``test_the_common_fields_come_first_in_every_kind``).
_COMMON_LEN = len(COMMON)


def contract(kind):
    """The text a brief prints for ``kind``, generated from its schema: a field reference naming
    every field and its instruction, then the fenced, placeholder-filled ``asf-report`` shape
    itself. Each JSON value is the token ``"<<field>>"`` — the exact, unique span a caller fills
    in with a real value (:func:`render`'s shape) to get something :func:`typed` accepts; it must
    parse and match this shape exactly, or an unreadable report comes back as a correction."""
    table = schema(kind)
    ref = '\n'.join(f'- {key}: {instr}' for key, (_types, _default, instr) in table.items())
    body_lines = ['{']
    keys = list(table)
    for n, key in enumerate(keys):
        comma = ',' if n < len(keys) - 1 else ''
        body_lines.append(f'  "{key}": "<<{key}>>"{comma}')
    body_lines.append('}')
    body = '\n'.join(body_lines)
    return (
        f'Field reference for a {kind} report:\n{ref}\n\n'
        'Then, beside your prose REPORT, the typed report — this is the one the factory reads:\n\n'
        f'```json {FENCE_TOKEN}\n{body}\n```\n\n'
        'It must parse and it must match this shape exactly — an unreadable report comes back to '
        'you as a correction, and nothing else about your work is re-done.'
    )


# ---------------------------------------------------------------------------------------------
# The prose fallback (F-0025 replan, PD-READERS). None of what follows is a second way to read
# the typed report above — `check`, `failure`, `ruling`, `ruling_fields` and `summary` read the
# typed object only, with no fallback to the prose block below. What follows instead stands for
# the two public names this card deletes (`parse`, `unpushed`), for the callers — inside this
# module (`footprint_claim`, `no_split`, `needs_input`, `rebased`) and outside it
# (`asf.workers.health.push_retry`, `asf.workers.lifecycle.overruling`/`delivered_off_branch`,
# `asf.workers.relaunch.terminal`, `asf.harvest.lane.report_status`/`run_heads`,
# `asf.workers.trunkclose.own_shas`/`trunk_sha`, `asf.tick.rejudge`'s status read) — that predate
# this card and have no typed-field equivalent to move to: they still read a ``{field: value}``
# off the prose ``REPORT`` block a brief prints, unchanged.
# ---------------------------------------------------------------------------------------------

_FIELDS = ('item', 'kind', 'status', 'branch', 'pushed', 'commits', 'tests', 'left out',
           'needs writes', 'proves', 'ruling', 'blocked_on', 'writes', 'superseded_by')
HEAD_RE = re.compile(r'^\s*REPORT\s*$', re.M)
FIELD_RE = re.compile(
    r'^(?P<key>item|kind|status|branch|pushed|commits|tests|left out|needs writes|proves|'
    r'ruling|blocked_on|writes|superseded_by)\s*:\s*(?P<value>.*)$', re.I)
#: A ``pushed:`` value declaring the run's work is not on origin — the prose contract's own
#: vocabulary, read by the handful of external callers above, never by this module's own typed
#: readers.
NO_RE = re.compile(r'^\s*(no|none|not pushed|unpushed)\b', re.I)
NONE_RE = re.compile(r'^(none|n/a|-|—)$', re.I)
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


def _prose(text):
    """``{field: value}`` of the last prose REPORT block in ``text``, or ``{}`` — the deleted
    public ``parse``'s body, renamed and no longer exported (F-0025 D11/PD-READERS). A field's
    value runs to the next field line; a fenced block's closing ````` ``` ````` ends the report. A
    line that names the field already open — ``proves:``'s own trailers each start ``Proves: …``
    — is a continuation, not a restart: a real report never states one field twice running."""
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


#: A ``pushed:`` value declaring a rebase the factory publishes: ``rebased <sha> — …``.
REBASED_RE = re.compile(r'^\s*rebased\s+`?(?P<sha>[0-9a-f]{7,40})\b', re.I)


def rebased(text):
    """The sha a result's own report declared ``pushed: rebased <sha>`` — the brief's answer to
    a push refused as non-fast-forward, which the factory publishes — or ''."""
    m = REBASED_RE.match((_prose(text).get('pushed') or '').split('\n', 1)[0])
    return m.group('sha').lower() if m else ''


def _claim(value):
    """The value, or None when it is empty or says ``none`` — no claim."""
    value = (value or '').strip()
    return None if not value or NONE_RE.match(value) else value


def _first_line(value):
    return (value or '').strip().split('\n', 1)[0]


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
    for m in OPERATOR_RE.finditer(text):
        what = m.group('what').strip()
        if NO_QUESTION_RE.match(what) or NO_SPLIT_RE.match(m.group(0)):
            continue  # "none" asks nothing; "does not split" is an answer, not a question
        if _claims_writes(text):
            return None  # a `needs writes:` claim is answered by the widening rule, not a person
        return what
    rep = _prose(text)
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
    rep = _prose(text)
    status = (rep.get('status') or '').strip().lower().split(' ')[0]
    if status == 'done' and rep.get('pushed') and not NO_RE.match(rep.get('pushed') or ''):
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
