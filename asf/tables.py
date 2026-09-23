"""asf.tables — markdown pipe tables, drawn as box tables for the console.

Every ``asf`` view writes its tables as markdown: that is the format a record file, a PR body or
an editor holds. The console is only a viewer. :func:`install` wraps ``sys.stdout`` so a run of
``| … |`` lines is redrawn as a box table — cells wrapped to fit the width — and ``**bold**``
markers are dropped; every other line passes through as it comes, so streaming commands still
stream. Files the commands write are untouched: only stdout is redrawn.

``ASF_TABLES`` picks the mode: ``box`` (the plugin's skills set it, their output is captured
through a pipe), ``md`` (never redraw), else ``auto`` — box when stdout is a terminal.
``ASF_TABLE_WIDTH`` caps the width; else the terminal's, else :data:`DEFAULT_WIDTH`.
"""
import os
import re
import shutil
import sys
import unicodedata

DEFAULT_WIDTH = 120   # piped (a skill in Claude Code): its pane is narrower than a terminal
MIN_COL = 6

_ROW = re.compile(r'^\s*\|.*\|\s*$')
_RULE = re.compile(r'^\s*\|(\s*:?-{3,}:?\s*\|)+\s*$')
_BOLD = re.compile(r'\*\*(\S(?:.*?\S)?)\*\*')


def mode(stream=None):
    m = os.environ.get('ASF_TABLES', 'auto').strip().lower()
    if m in ('box', 'md'):
        return m
    stream = stream or sys.stdout
    try:
        return 'box' if stream.isatty() else 'md'
    except (AttributeError, ValueError):
        return 'md'


def width():
    try:
        return max(40, int(os.environ['ASF_TABLE_WIDTH']))
    except (KeyError, ValueError):
        pass
    if not sys.stdout.isatty() and 'COLUMNS' not in os.environ:
        return DEFAULT_WIDTH
    return shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns


def _w(text):
    """Display width: wide East Asian and emoji count two, combining marks none."""
    n = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return n


def _split(line):
    """The cells of one ``| a | b |`` line; ``\\|`` stays a literal bar."""
    body = line.strip()[1:-1]
    cells, cur, i = [], '', 0
    while i < len(body):
        if body[i] == '\\' and i + 1 < len(body) and body[i + 1] == '|':
            cur += '|'
            i += 2
            continue
        if body[i] == '|':
            cells.append(cur)
            cur = ''
        else:
            cur += body[i]
        i += 1
    cells.append(cur)
    return [_plain(c.strip()) for c in cells]


def _plain(text):
    return _BOLD.sub(r'\1', text)


def _wrap(text, limit):
    """Word-wrap ``text`` to display width ``limit``; a word longer than the limit is split."""
    if not text:
        return ['']
    lines, cur = [], ''
    for word in text.split():
        while _w(word) > limit:
            room = limit - (_w(cur) + 1 if cur else 0)
            if room < 1:
                lines.append(cur)
                cur, room = '', limit
            head = ''
            for ch in word:
                if _w(head + ch) > room:
                    break
                head += ch
            cur = f'{cur} {head}' if cur else head
            lines.append(cur)
            cur, word = '', word[len(head):]
        if not cur:
            cur = word
        elif _w(cur) + 1 + _w(word) <= limit:
            cur += ' ' + word
        else:
            lines.append(cur)
            cur = word
    if cur or not lines:
        lines.append(cur)
    return lines


def _widths(rows, total):
    """Column widths that fit ``total``: each column's natural width, the widest shrunk first."""
    n = max(len(r) for r in rows)
    natural = [max((_w(r[i]) if i < len(r) else 0) for r in rows) for i in range(n)]
    longest_word = [max((max((_w(x) for x in r[i].split()), default=0) if i < len(r) else 0)
                        for r in rows) for i in range(n)]
    floor = [max(1, min(natural[i], max(MIN_COL, min(longest_word[i], 12)))) for i in range(n)]
    widths = [max(1, x) for x in natural]
    budget = total - (3 * n + 1)          # "│ " + " │ " between + " │"
    while sum(widths) > budget:
        i = max(range(n), key=lambda k: (widths[k] - floor[k], widths[k]))
        if widths[i] <= floor[i]:
            break
        widths[i] -= 1
    return widths


def box(lines, total=None):
    """Draw the markdown table ``lines`` (header, rule, rows) as a box table."""
    rows = [_split(l) for l in lines if not _RULE.match(l)]
    has_header = len(lines) > 1 and bool(_RULE.match(lines[1]))
    n = max(len(r) for r in rows)
    rows = [r + [''] * (n - len(r)) for r in rows]
    widths = _widths(rows, total or width())

    def rule(left, mid, right):
        return left + mid.join('─' * (w + 2) for w in widths) + right

    def draw(row):
        cols = [_wrap(c, w) for c, w in zip(row, widths)]
        height = max(len(c) for c in cols)
        return ['│ ' + ' │ '.join((c[k] if k < len(c) else '') + ' ' * (w - _w(c[k] if k < len(c) else ''))
                                  for c, w in zip(cols, widths)) + ' │' for k in range(height)]

    drawn = [draw(row) for row in rows]
    # a rule between rows only earns its line when some row wraps; single-line rows read as a list
    between = any(len(d) > 1 for d in drawn[1 if has_header else 0:])
    out = [rule('┌', '┬', '┐')]
    for i, lines in enumerate(drawn):
        out += lines
        if i < len(rows) - 1:
            if i == 0 and has_header:
                out.append(rule('╞', '╪', '╡'))
            elif between:
                out.append(rule('├', '┼', '┤'))
    out.append(rule('└', '┴', '┘'))
    return out


def render(text, total=None):
    """``text`` with every markdown table drawn as a box table and ``**bold**`` dropped."""
    out, block = [], []
    for line in text.split('\n'):
        if _ROW.match(line):
            block.append(line)
            continue
        if block:
            out += box(block, total)
            block = []
        out.append(_plain(line))
    if block:
        out += box(block, total)
    return '\n'.join(out)


class BoxStream:
    """A stdout wrapper: plain lines pass straight through, a table is held until it ends."""

    def __init__(self, stream, total=None):
        self._stream = stream
        self._total = total
        self._partial = ''
        self._block = []

    def write(self, s):
        self._partial += s
        *done, self._partial = self._partial.split('\n')
        for line in done:
            self._line(line)
        return len(s)

    def _line(self, line):
        if _ROW.match(line):
            self._block.append(line)
            return
        self._flush_block()
        self._stream.write(_plain(line) + '\n')

    def _flush_block(self):
        if self._block:
            self._stream.write('\n'.join(box(self._block, self._total)) + '\n')
            self._block = []

    def flush(self):
        # a table is only drawn once it is complete; lines still in flight wait for their end
        self._stream.flush()

    def close(self):
        if self._partial:
            line, self._partial = self._partial, ''
            if _ROW.match(line):
                self._block.append(line)
                self._flush_block()
            else:
                self._flush_block()
                self._stream.write(_plain(line))
        self._flush_block()
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install():
    """Wrap ``sys.stdout`` when the mode is ``box``; returns the finisher to call at exit."""
    if mode() != 'box' or isinstance(sys.stdout, BoxStream):
        return lambda: None
    wrapper = BoxStream(sys.stdout, width())
    sys.stdout = wrapper

    def finish():
        wrapper.close()
        if sys.stdout is wrapper:
            sys.stdout = wrapper._stream
    return finish
