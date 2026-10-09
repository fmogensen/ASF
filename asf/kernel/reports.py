"""asf.kernel.reports — what an ended session said, read off its result, and the Stuck reason
built from it (ASF 0.2).

Every brief ends with a REPORT block (:mod:`asf.workers.report` parses it, the same reader the
floor uses). :func:`read` turns an ended session's result into the :class:`Session` fields the
kernel decides on: ``status`` (``done``, ``partial``, ``blocked`` or ''), ``fields`` (the parsed
REPORT), ``question`` (a ``NEEDS OPERATOR:`` line that asks something), ``api_error`` (the
session's API failed before it could report) and ``last_line`` (the last line that says
something). :func:`stuck_reason` builds a Stuck reason from the report's own words, and
:func:`meaningful` is the guard every reason passes: never empty, a code fence or noise such as
``Message ID: msg_…``. Everything but :func:`read` is pure and imports nothing outside the kernel.
"""
import re

#: the statuses a REPORT declares
DONE, PARTIAL, BLOCKED = 'done', 'partial', 'blocked'
STATUSES = (DONE, PARTIAL, BLOCKED)

#: the longest Stuck reason the kernel writes
MAX_REASON = 200

#: a line that says nothing: blank, a code fence, punctuation only, an API id line
NOISE_RE = re.compile(r'^\s*(?:`{3,}[\w-]*|[\W_]*|(?:Message|Request) ID:.*|Details:\s*`?\[\w+\]`?)\s*$',
                      re.I)

#: a line of an API failure the runtime printed in place of a session's work
API_LINE_RE = re.compile(r'^\s*(?:API Error\b|Message ID:\s*msg_|Request ID:\s*req_)', re.I | re.M)
#: an API failure named in a short result: rate limit, overloaded
API_WORD_RE = re.compile(r'\b(?:rate[ _-]?limit(?:ed|_error)?|overloaded(?:_error)?)\b', re.I)
#: a result this short that names an API failure is the failure, not prose about one
API_SHORT = 600

#: a test run's words for a failure
FAILED_RE = re.compile(r'\b(?:FAIL(?:ED|URE|URES)?|ERRORS?|Traceback)\b')


def meaningful(reason):
    """Whether ``reason`` says something: not empty, not a code fence, not punctuation, not an
    API id line (``Message ID: msg_…``)."""
    text = str(reason or '').strip()
    return bool(text) and not NOISE_RE.match(text) and any(ch.isalnum() for ch in text)


def clean(reason, fallback):
    """``reason`` capped at :data:`MAX_REASON`, or ``fallback`` when it says nothing."""
    text = ' '.join(str(reason or '').split())
    return cap(text if meaningful(text) else fallback)


def cap(text, limit=MAX_REASON):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def last_line(text):
    """The last line of ``text`` that says something (:func:`meaningful`), or ''."""
    for line in reversed(str(text or '').splitlines()):
        if meaningful(line):
            return line.strip()
    return ''


def api_error(text, is_error=False):
    """The API failure a result without a REPORT shows, or '': its ``API Error`` line, else the
    first line that says something. ``is_error`` is the runtime's own flag on the result."""
    text = str(text or '')
    hit = is_error or bool(API_LINE_RE.search(text)) or (
        len(text) <= API_SHORT and bool(API_WORD_RE.search(text)))
    if not hit:
        return ''
    lines = [ln.strip() for ln in text.splitlines() if meaningful(ln)]
    named = [ln for ln in lines if re.match(r'API Error\b', ln, re.I)] or \
        [ln for ln in lines if API_WORD_RE.search(ln)] or lines
    return cap(named[0], 160) if named else 'no detail'


def read(result):
    """``{status, fields, question, api_error, last_line}`` of an ended session's ``result``
    record (the runtime's ``result`` line: its ``result`` text and ``is_error`` flag)."""
    from asf.workers import report  # the floor's REPORT reader, shared
    result = result or {}
    text = str(result.get('result') or '')
    fields = report.parse(text)
    status = (fields.get('status') or '').strip().lower().split(' ')[0].strip('`*_,.;:')
    return {
        'status': status if status in STATUSES else '',
        'fields': fields,
        'question': report.operator_question(text),
        'api_error': '' if fields else api_error(text, bool(result.get('is_error'))),
        'last_line': last_line(text),
    }


def _first(value):
    return ' '.join(str(value or '').split())


def _none(value):
    return not value or re.match(r'^(?:none|n/a|-|—)\b', value, re.I)


def _failing(tests):
    """The part of a ``tests:`` value that names a failure, or ''."""
    for part in re.split(r';\s+|\.\s+(?=python|bash|\S+\s*->|\S+\s*→)', tests):
        if FAILED_RE.search(part):
            return part.strip()
    return ''


def stuck_reason(status, fields, question=''):
    """A Stuck reason from a REPORT's own words: ``<status>: <what>``, where ``<what>`` is the
    ``NEEDS OPERATOR:`` question, else the failing part of ``tests:``, else ``left out:``, else
    ``needs writes:``, else ``blocked_on:``, else ``tests:``. Capped at :data:`MAX_REASON`."""
    label = status or 'report'
    fields = fields or {}
    what = ''
    if question:
        what = 'NEEDS OPERATOR: %s' % _first(question)
    tests = _first(fields.get('tests'))
    if not what and tests:
        failing = _failing(tests)
        if failing:
            what = 'tests: %s' % failing
    for key in ('left out', 'needs writes', 'blocked_on'):
        value = _first(fields.get(key))
        if not what and not _none(value):
            what = '%s: %s' % (key, value)
    if not what and tests:
        what = 'tests: %s' % tests
    return clean('%s: %s' % (label, what) if what else '',
                 '%s: the REPORT names no reason' % label)


def no_report_reason(line):
    """The reason of a session that ended without a REPORT, with its last line that says
    something."""
    line = ' '.join(str(line or '').split())
    return cap('ended without a REPORT: %s' % line if meaningful(line)
               else 'ended without a REPORT')
