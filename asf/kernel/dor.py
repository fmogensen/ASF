"""asf.kernel.dor — the Definition of Ready, and the groom-fill verdict that answers it (ASF 0.2).

Pure: reads nothing but its arguments. A Task or Bug starts (``decide`` makes it Ready) only when
:func:`missing` finds nothing:

- (i) **a test**: a line of its ``## Acceptance`` names a test (``tests/<file>.py::<Test>``, a
  test file path, or a dotted test module ``tests.test_x[.Class]``), or a line of its
  ``**Gate**`` fenced block names a test module that is on the trunk or in its ``writes:``;
- (ii) **writes**: ``writes:`` is not empty and each path is on the trunk (a glob: some trunk
  file matches it) or declared new (its ``creates:`` names it). With the trunk unread
  (``trunk`` None) only the emptiness is checked;
- (iii) **after**: every ``after:`` id is on the record, and none is parked (``priority:
  later`` on it or an ancestor): a parked blocker never finishes;
- (iv) **parent**: a named ``parent`` is on the record and not Done; a Task names one.

A card that fails stays New with the reason ``dor: <what is missing>`` and the kernel launches a
``groom-fill`` session for it (``kernel.dor.fill_per_tick`` a tick, ``kernel.dor.max_fills`` per
item). The session judges only; it ends with a ``GROOM-FILL`` block (:data:`VERDICT_SCHEMA`) that
:func:`parse_verdict` reads and validates — a block missing a required field is rejected whole,
and code applies the rest (:mod:`asf.kernel.apply`).
"""
import fnmatch
import json
import re

#: the reason prefix of a card the Definition of Ready holds New
PREFIX = 'dor: '

#: the session kind that fills a card the Definition of Ready holds
GROOM_FILL = 'groom-fill'

#: the verdicts a groom-fill session may return
VERDICTS = ('proceed', 'superseded', 'reshape', 'fill')

#: the risk words a groom-fill verdict may raise
RISKS = ('none', 'high')

#: the line a groom-fill session's verdict block opens with
HEAD = 'GROOM-FILL'

#: the block a groom-fill session ends with (its brief quotes it)
VERDICT_SCHEMA = """GROOM-FILL
verdict: proceed | superseded | reshape | fill
superseded_by: <id or sha>   # required for superseded
acceptance: ["<line> — tests/<file>::<Test>"]   # required for fill/reshape
writes: [paths]   # required for fill/reshape; a path that does not exist yet ends " (new)"
risk_raise: none | high
reason: <one line>"""

#: a path a writes entry declares new
NEW_SUFFIX = ' (new)'

_PATH_RE = re.compile(r'(?<![\w./-])([\w./-]+\.py)(?:::([\w.]+))?')
_DOTTED_RE = re.compile(r'(?<![\w./-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)')
_GATE_RE = re.compile(r'^\s*(?:\*\*Gate\*\*|#+\s*Gate)\s*:?\s*$', re.I)
_SECTION_RE = re.compile(r'^##\s+(.+?)\s*$')
_ID_RE = re.compile(r'^[A-Za-z]+-\d+$')
_SHA_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')


def _test_file(path):
    """Whether ``path`` reads as a test file: its name starts ``test`` or a directory is a test
    directory."""
    parts = path.strip('./').split('/')
    return parts[-1].startswith('test') or any(p in ('tests', 'test') for p in parts[:-1])


def test_refs(line):
    """The test modules ``line`` names, as repo paths: ``tests/test_x.py[::Test]`` and test file
    paths as written, a dotted ``tests.test_x[.Class]`` as ``tests/test_x.py``."""
    text = str(line or '')
    out = []
    for m in _PATH_RE.finditer(text):
        if _test_file(m.group(1)):
            out.append(m.group(1).lstrip('./') if m.group(1).startswith('./') else m.group(1))
    for m in _DOTTED_RE.finditer(text):
        comps = m.group(1).split('.')
        if comps[-1] == 'py':
            continue  # a file name, read above
        for i, c in enumerate(comps):
            if c.startswith('test_') or c == 'test':
                out.append('/'.join(comps[:i + 1]) + '.py')
                break
    return list(dict.fromkeys(out))


def acceptance_lines(body):
    """The non-empty lines of the ``## Acceptance`` section of a card's ``body``, checkboxes
    stripped."""
    out, inside = [], False
    for line in str(body or '').splitlines():
        m = _SECTION_RE.match(line)
        if m:
            inside = m.group(1).strip().lower() == 'acceptance'
            continue
        if inside:
            text = re.sub(r'^\s*[-*]\s*(\[[ xX]?\]\s*)?', '', line).strip()
            if text:
                out.append(text)
    return out


def gate_lines(body):
    """The lines of the fenced block that follows a ``**Gate**`` line in a card's ``body``."""
    out, state = [], 0  # 0: looking, 1: after the Gate line, 2: inside its fence
    for line in str(body or '').splitlines():
        if state == 0 and _GATE_RE.match(line):
            state = 1
        elif state == 1 and line.strip().startswith('```'):
            state = 2
        elif state == 1 and _SECTION_RE.match(line):
            state = 0
        elif state == 2:
            if line.strip().startswith('```'):
                state = 0
                continue
            out.append(line.strip())
    return out


def on_trunk(path, trunk):
    """Whether ``path`` (a file, a directory or a glob) names something on the trunk's file list
    ``trunk`` (a set)."""
    p = path.strip()
    if any(ch in p for ch in '*?['):
        return any(fnmatch.fnmatchcase(f, p) for f in trunk)
    if p in trunk:
        return True
    d = p.rstrip('/') + '/'
    return any(f.startswith(d) for f in trunk)


def _named(path, writes):
    return any(path == w or fnmatch.fnmatchcase(path, w) for w in writes)


def missing(it, items, trunk=None, parked=()):
    """What ``it`` (a Task or Bug, :class:`asf.kernel.model.Item`) lacks to be Ready, as short
    phrases ([] when it is ready). ``items``: the record; ``trunk``: the trunk's file paths (None:
    unread, no existence check); ``parked``: the ids parked (``priority: later``)."""
    out = []
    writes = [str(w).strip() for w in it.writes if str(w).strip()]
    creates = [str(c).strip() for c in getattr(it, 'creates', ()) if str(c).strip()]
    acc = [r for line in acceptance_lines(it.body) for r in test_refs(line)]
    gate = [r for line in gate_lines(it.body) for r in test_refs(line)
            if trunk is None or on_trunk(r, trunk) or _named(r, writes)]
    if not acc and not gate:
        out.append('no test named in Acceptance or Gate')
    if not writes:
        out.append('writes: empty')
    elif trunk is not None:
        absent = [w for w in writes if not on_trunk(w, trunk) and not _named(w, creates)]
        if absent:
            out.append('writes not on trunk: %s' % ', '.join(absent[:3])
                       + (' (+%d)' % (len(absent) - 3) if len(absent) > 3 else ''))
    gone = [a for a in it.after if a not in items]
    if gone:
        out.append('after: not on the record: %s' % ', '.join(gone))
    held = [a for a in it.after if a in items and a in parked]
    if held:
        out.append('after: parked: %s' % ', '.join(held))
    if it.parent:
        if it.parent not in items:
            out.append('parent %s not on the record' % it.parent)
        elif items[it.parent].state.value == 'done':
            out.append('parent %s is Done' % it.parent)
    elif it.type == 'task':
        out.append('no parent')
    return out


def reason(gaps):
    """The New reason of a card with ``gaps`` (:func:`missing`)."""
    return PREFIX + '; '.join(gaps)


# ---- the groom-fill verdict ---------------------------------------------------------------------

class Verdict:
    """A validated groom-fill verdict: ``verdict`` (:data:`VERDICTS`), ``superseded_by``,
    ``acceptance`` (lines), ``writes`` (paths, the ``(new)`` mark taken off), ``creates`` (the
    paths it marked new), ``risk_raise`` (:data:`RISKS`) and ``reason``."""

    def __init__(self, verdict, superseded_by='', acceptance=(), writes=(), creates=(),
                 risk_raise='none', reason=''):
        self.verdict, self.superseded_by = verdict, superseded_by
        self.acceptance, self.writes, self.creates = list(acceptance), list(writes), list(creates)
        self.risk_raise, self.reason = risk_raise, reason


def _list(value):
    text = str(value or '').strip()
    if not text:
        return []
    try:
        got = json.loads(text)
    except ValueError:
        if text.startswith('[') and text.endswith(']'):
            got = [p.strip().strip('"\'') for p in text[1:-1].split(',')]
        else:
            return None
    if not isinstance(got, list) or not all(isinstance(g, str) for g in got):
        return None
    return [g.strip() for g in got if g.strip()]


def _plain(value):
    text = str(value or '').split('#', 1)[0].strip().strip('`"\'')
    return '' if text.lower() in ('', 'none', 'n/a', '-', '—') else text


def block(text):
    """``{key: value}`` of the last :data:`HEAD` block in ``text`` (a session's report), or
    None when it has none."""
    lines = str(text or '').splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().strip('`') == HEAD]
    if not starts:
        return None
    out = {}
    for ln in lines[starts[-1] + 1:]:
        s = ln.strip()
        if s.startswith('```'):
            break
        m = re.match(r'^([a-z_]+)\s*:\s*(.*)$', s)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_verdict(text):
    """``(Verdict, '')`` from a groom-fill session's report, or ``(None, why)`` when it has no
    :data:`HEAD` block or the block misses a required field — nothing of a rejected block is
    applied."""
    got = block(text)
    if got is None:
        return None, 'no %s block' % HEAD
    verdict = _plain(got.get('verdict')).lower()
    if verdict not in VERDICTS:
        return None, 'verdict %r is not one of %s' % (verdict, ' | '.join(VERDICTS))
    risk = str(got.get('risk_raise') or '').split('#', 1)[0].strip().strip('`"\'').lower()
    if risk not in RISKS:
        return None, 'risk_raise %r is not one of %s' % (risk, ' | '.join(RISKS))
    why = _plain(got.get('reason'))
    if not why:
        return None, 'reason: missing'
    v = Verdict(verdict, risk_raise=risk, reason=' '.join(why.split()))
    if verdict == 'superseded':
        by = _plain(got.get('superseded_by'))
        if not (_ID_RE.match(by) or _SHA_RE.match(by)):
            return None, 'superseded_by: missing or not an id or sha (%r)' % by
        v.superseded_by = by.upper() if _ID_RE.match(by) else by.lower()
    if verdict in ('fill', 'reshape'):
        acc, writes = _list(got.get('acceptance')), _list(got.get('writes'))
        if not acc:
            return None, 'acceptance: missing or not a list'
        untested = [a for a in acc if not test_refs(a)]
        if untested:
            return None, 'acceptance line names no test: %r' % untested[0][:80]
        if not writes:
            return None, 'writes: missing or not a list'
        v.acceptance = acc
        for w in writes:
            path = w[:-len(NEW_SUFFIX)].strip() if w.endswith(NEW_SUFFIX) else w
            v.writes.append(path)
            if path != w:
                v.creates.append(path)
    return v, ''
