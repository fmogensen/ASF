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

:func:`parse` reads it off a result's text (the last ``REPORT`` block wins). :func:`failure`
names the one failure the report itself declares: ``pushed: no`` — the session says its work
is not on origin. That is a failed session at the source (B-0052: six sessions ended
``success`` with "I'll wait for the background suite and push later"), before health measures
the same thing against git (B-0051). A report that carries no ``pushed:`` line declares
nothing; the evidence rule still applies.
"""
import re

FIELDS = ('item', 'kind', 'status', 'branch', 'pushed', 'commits', 'tests', 'left out', 'ruling')
HEAD_RE = re.compile(r'^\s*REPORT\s*$', re.M)
FIELD_RE = re.compile(r'^(?P<key>item|kind|status|branch|pushed|commits|tests|left out|ruling)\s*:\s*(?P<value>.*)$', re.I)
NO_RE = re.compile(r'^\s*(no|none|not pushed|unpushed)\b', re.I)
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
            key = m.group('key').lower()
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


def failure(text):
    """The failure a result's own report declares, or None: :data:`UNPUSHED` for ``pushed: no``."""
    rep = parse(text)
    if unpushed(rep):
        return UNPUSHED
    return None
