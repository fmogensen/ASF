"""asf.views.readme — the README's generated spans (`asf readme`).

A page a human writes, with the numbers written by the build. Two markers, both HTML comments,
so GitHub renders the page as prose and the build reads it as a template:

    <!--asf:n sessions-->1,204<!--/asf:n-->        an inline value
    <!--asf:block scoreboard-->                    a table, markers on their own lines
    | Metric | Value | From |
    <!--/asf:block-->

Read-only over the record: every number comes from `asf.views.index_reader` and
`asf.metrics.metrics`, which the daily scorecard already computes with. Stdlib only.
"""
import collections
import re

#: Digit runs a human may type in the page's prose. Every entry is a name, not a measurement:
#: a measurement belongs in a span.
LITERALS = ('0.1', '2.0')        # the release; Apache-2.0

#: One page: a reader scrolls it twice.
MAX_LINES = 150
MAX_BYTES = 10_000

Span = collections.namedtuple('Span', 'kind key start end body')

_MARKER = re.compile(r'<!--(/?)asf:(n|block)(?:[ \t]+([^\s>]+?))?[ \t]*-->')


def _line_of(text, pos):
    return text.count('\n', 0, pos) + 1


def _alone(text, m):
    """True when the marker is the only thing on its line."""
    a = text.rfind('\n', 0, m.start()) + 1
    b = text.find('\n', m.end())
    b = len(text) if b < 0 else b
    return text[a:m.start()].strip() == '' and text[m.end():b].strip() == ''


def spans(text):
    """Every span in document order. `start`/`end` are the offsets of the body: for an inline
    span the text between the markers, for a block the whole lines between the marker lines."""
    found = []
    seen = set()
    open_ = None                       # (kind, key, marker match, body start)
    for m in _MARKER.finditer(text):
        closing, kind, key = m.group(1) == '/', m.group(2), m.group(3)
        line = _line_of(text, m.start())
        if kind == 'block' and not _alone(text, m):
            raise ValueError('line %d: an asf:block marker must be alone on its line' % line)
        if not closing:
            if not key:
                raise ValueError('line %d: an asf:%s marker without a key' % (line, kind))
            if open_ is not None:
                raise ValueError('line %d: span %r opened inside span %r (line %d)'
                                 % (line, key, open_[1], _line_of(text, open_[2].start())))
            if key in seen:
                raise ValueError('line %d: key %r used twice' % (line, key))
            seen.add(key)
            if kind == 'block':
                nl = text.find('\n', m.end())
                start = len(text) if nl < 0 else nl + 1
            else:
                start = m.end()
            open_ = (kind, key, m, start)
            continue
        if open_ is None:
            raise ValueError('line %d: close of asf:%s with no open' % (line, kind))
        if open_[0] != kind:
            raise ValueError('line %d: close of asf:%s inside span %r, which is an asf:%s'
                             % (line, kind, open_[1], open_[0]))
        end = text.rfind('\n', 0, m.start()) + 1 if kind == 'block' else m.start()
        end = max(end, open_[3])
        found.append(Span(kind, open_[1], open_[3], end, text[open_[3]:end]))
        open_ = None
    if open_ is not None:
        raise ValueError('line %d: span %r is never closed'
                         % (_line_of(text, open_[2].start()), open_[1]))
    return found


def render(text, facts):
    """`text` with each span's body replaced by `facts['numbers'][key]['text']`. Bytes outside
    a span are spliced through untouched."""
    numbers = facts.get('numbers', {})
    found = spans(text)
    keys = {s.key for s in found}
    for s in found:
        if s.key not in numbers:
            raise ValueError('no fact for span %r' % s.key)
    for key in numbers:
        if key not in keys:
            raise ValueError('fact %r has no span in the page' % key)
    out = []
    at = 0
    for s in found:
        new = numbers[s.key]['text']
        if s.kind == 'block' and new and not new.endswith('\n'):
            new += '\n'
        out.append(text[at:s.start])
        out.append(new)
        at = s.end
    out.append(text[at:])
    return ''.join(out)
