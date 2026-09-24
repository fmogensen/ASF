"""The one frontmatter parser/writer.

Implements the controlled YAML-like subset described in README.md's "One item
file" / "The machine block" sections: scalar strings/ints/floats/bools/ISO
dates, `[a, b, "quoted, string"]` inline lists, `- item` block lists, ONE
level of `key:` nesting (`links`, `cost`), comments after `#` on typed lines,
and the `# ---- machine ----` marker line.

Anything outside that subset raises FrontmatterError. Nothing here uses
PyYAML or any third-party module — python3 stdlib only.
"""
import re

MARKER = "# ---- machine ----"

_KEY_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*):(.*)$')
_INT_RE = re.compile(r'^-?\d+$')
_FLOAT_RE = re.compile(r'^-?\d+\.\d+$')
_BARE_DECISION_RE = re.compile(r'\bD\d{1,3}\b')


class FrontmatterError(Exception):
    def __init__(self, file, line, why):
        super().__init__(f"{file}:{line}: {why}")
        self.file = file
        self.line = line
        self.why = why


class _Entry:
    __slots__ = ("kind", "key", "value", "raw")

    def __init__(self, kind, key, value, raw):
        self.kind = kind
        self.key = key
        self.value = value
        self.raw = raw


class FrontmatterDict(dict):
    """A plain dict of frontmatter fields; formatting metadata (comments,
    original line text, which keys are typed vs. machine) lives in instance
    attributes so this still serialises to JSON as a plain key/value map."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.entries = []
        self.machine_keys = set()
        self.comments = {}


_MISSING = object()


def _split_comment(s):
    """Return (content, comment) splitting on the first unquoted '#'."""
    in_quote = False
    for idx, ch in enumerate(s):
        if ch == '"':
            in_quote = not in_quote
        elif ch == '#' and not in_quote:
            return s[:idx], s[idx:]
    return s, None


def _split_top(s, sep):
    """Split s on sep at top level, respecting double-quoted spans."""
    parts = []
    cur = ''
    in_quote = False
    for ch in s:
        if ch == '"':
            in_quote = not in_quote
            cur += ch
        elif ch == sep and not in_quote:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    parts.append(cur)
    return parts


def _coerce_scalar(token):
    token = token.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        inner = token[1:-1]
        return inner.replace('\\"', '"').replace('\\\\', '\\')
    if token == 'true':
        return True
    if token == 'false':
        return False
    if token == 'null':
        return None
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _parse_value(content):
    content = content.strip()
    if content.startswith('[') and content.endswith(']'):
        inner = content[1:-1]
        return [_coerce_scalar(t) for t in _split_top(inner, ',') if t.strip() != '']
    if content.startswith('{') and content.endswith('}'):
        inner = content[1:-1]
        d = {}
        for part in _split_top(inner, ','):
            part = part.strip()
            if not part:
                continue
            k, _, v = part.partition(':')
            d[k.strip()] = _coerce_scalar(v.strip())
        return d
    return _coerce_scalar(content)


def _needs_quote(s, in_list=False):
    if s == '':
        return True
    if s.strip() != s:
        return True
    if ': ' in s or s.endswith(':'):
        return True
    if s[0] in '#[]{}"\'':
        return True
    if in_list and ',' in s:
        return True
    if s.lower() in ('true', 'false', 'null'):
        return True
    if _INT_RE.match(s) or _FLOAT_RE.match(s):
        return True
    if '#' in s:
        return True
    return False


def _quote(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _format_scalar(value, in_list=False):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    return _quote(s) if _needs_quote(s, in_list) else s


def _format_entry(key, value, list_style='inline'):
    if isinstance(value, dict):
        if any(isinstance(v, list) for v in value.values()):
            # a sub-value is a list (links.branches, links.prs, ...): the inline `{a: b}` form
            # has no syntax for that, so fall back to the block form `parse()` already reads
            # (`key:\n  sub: value`), which nests one list deep just fine.
            lines = [f"{key}:"]
            for k, v in value.items():
                if isinstance(v, list):
                    inner = ', '.join(_format_scalar(x, in_list=True) for x in v)
                    lines.append(f"  {k}: [{inner}]")
                else:
                    lines.append(f"  {k}: {_format_scalar(v)}")
            return '\n'.join(lines)
        inner = ', '.join(f"{k}: {_format_scalar(v)}" for k, v in value.items())
        return f"{key}: {{{inner}}}"
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        if list_style == 'block':
            lines = [f"{key}:"]
            for item in value:
                lines.append(f"  - {_format_scalar(item, in_list=True)}")
            return '\n'.join(lines)
        inner = ', '.join(_format_scalar(v, in_list=True) for v in value)
        return f"{key}: [{inner}]"
    return f"{key}: {_format_scalar(value)}"


def parse(text, path=None):
    """Parse a full item file's text into (meta, body)."""
    if not text.startswith('---\n'):
        raise FrontmatterError(path, 1, "file must start with '---'")
    lines = text.split('\n')
    end = None
    for i in range(1, len(lines)):
        if lines[i] == '---':
            end = i
            break
    if end is None:
        raise FrontmatterError(path, len(lines), "missing closing '---'")
    header_lines = lines[1:end]
    body = '\n'.join(lines[end + 1:])

    meta = FrontmatterDict()
    entries = []
    machine_keys = set()
    in_machine = False
    i = 0
    n = len(header_lines)
    while i < n:
        raw_line = header_lines[i]
        line_no = i + 2
        stripped = raw_line.strip()
        if stripped == '':
            entries.append(_Entry('blank', None, None, raw_line))
            i += 1
            continue
        if raw_line.lstrip().startswith(MARKER):
            entries.append(_Entry('marker', None, None, raw_line))
            in_machine = True
            i += 1
            continue
        m = _KEY_RE.match(raw_line)
        if not m:
            raise FrontmatterError(path, line_no, f"cannot parse line: {raw_line!r}")
        key, rest = m.group(1), m.group(2)
        content, _comment = _split_comment(rest)
        content = content.strip()
        if content == '':
            if i + 1 < n and re.match(r'^\s+-\s', header_lines[i + 1]):
                items = []
                item_lines = []
                j = i + 1
                while j < n and re.match(r'^\s+-\s', header_lines[j]):
                    im = re.match(r'^\s+-\s(.*)$', header_lines[j])
                    item_content, _c = _split_comment(im.group(1))
                    items.append(_coerce_scalar(item_content.strip()))
                    item_lines.append(header_lines[j])
                    j += 1
                raw = '\n'.join([raw_line] + item_lines)
                entries.append(_Entry('list', key, items, raw))
                meta[key] = items
                if in_machine:
                    machine_keys.add(key)
                i = j
                continue
            elif i + 1 < n and re.match(r'^\s+[A-Za-z_][A-Za-z0-9_]*:', header_lines[i + 1]):
                subdict = {}
                sub_lines = []
                j = i + 1
                base_indent = len(header_lines[j]) - len(header_lines[j].lstrip())
                while j < n:
                    l = header_lines[j]
                    if l.strip() == '':
                        break
                    indent = len(l) - len(l.lstrip())
                    if indent < base_indent:
                        break
                    mm = re.match(r'^\s+([A-Za-z_][A-Za-z0-9_]*):(.*)$', l)
                    if not mm:
                        raise FrontmatterError(path, j + 2, f"cannot parse nested line: {l!r}")
                    subkey, subrest = mm.group(1), mm.group(2)
                    subcontent, _c = _split_comment(subrest)
                    subdict[subkey] = _parse_value(subcontent.strip())
                    sub_lines.append(l)
                    j += 1
                raw = '\n'.join([raw_line] + sub_lines)
                entries.append(_Entry('dict', key, subdict, raw))
                meta[key] = subdict
                if in_machine:
                    machine_keys.add(key)
                i = j
                continue
            else:
                entries.append(_Entry('scalar', key, None, raw_line))
                meta[key] = None
                if in_machine:
                    machine_keys.add(key)
                i += 1
                continue
        else:
            if len(content) >= 2 and (
                (content.count('[') > content.count(']')) or
                (content.count('{') > content.count('}'))
            ):
                raise FrontmatterError(path, line_no, f"unbalanced inline collection: {raw_line!r}")
            value = _parse_value(content)
            entries.append(_Entry('scalar', key, value, raw_line))
            meta[key] = value
            if in_machine:
                machine_keys.add(key)
            i += 1
            continue

    meta.entries = entries
    meta.machine_keys = machine_keys
    return meta, body


def _entry_style(entry):
    return 'block' if entry.kind == 'list' and '\n' in entry.raw else 'inline'


def render(meta, body, list_style=None):
    """Render (meta, body) back into full item-file text.

    When meta was produced by parse() and is unmodified, this reproduces the
    original text byte-for-byte (including comments) by replaying stored raw
    lines. Modified or fresh keys are rendered canonically.
    """
    list_style = list_style or {}
    entries = getattr(meta, 'entries', None)
    lines = []
    if entries:
        seen = set()
        for e in entries:
            if e.kind == 'blank':
                lines.append('')
                continue
            if e.kind == 'marker':
                lines.append(e.raw)
                continue
            seen.add(e.key)
            current = meta.get(e.key, _MISSING)
            if current is _MISSING:
                continue  # key was deleted
            if current == e.value:
                lines.append(e.raw)
            else:
                lines.append(_format_entry(e.key, current, list_style.get(e.key, _entry_style(e))))
        for k, v in meta.items():
            if k in seen:
                continue
            lines.append(_format_entry(k, v, list_style.get(k, 'inline')))
    else:
        machine_keys = getattr(meta, 'machine_keys', set())
        comments = getattr(meta, 'comments', {})
        typed_lines = []
        machine_lines = []
        for k, v in meta.items():
            line = _format_entry(k, v, list_style.get(k, 'inline'))
            c = comments.get(k)
            if c:
                line += c if c.startswith('#') else f"  # {c}"
            (machine_lines if k in machine_keys else typed_lines).append(line)
        lines = list(typed_lines)
        if machine_keys:
            lines.append(MARKER)
            lines.extend(machine_lines)
    header = '\n'.join(lines)
    return '---\n' + header + '\n---\n' + body


def split_machine(meta):
    """Split a parsed meta into (typed, machine) plain dicts."""
    machine_keys = getattr(meta, 'machine_keys', set())
    typed = {k: v for k, v in meta.items() if k not in machine_keys}
    machine = {k: v for k, v in meta.items() if k in machine_keys}
    return typed, machine


_LIST_BLOCK_KEYS = {'evidence'}


def write_machine(path, machine):
    """Rewrite only the machine block of an item file in place.

    Typed lines (everything up to and including the marker) are copied
    through byte-identical; the machine block is replaced wholesale with
    freshly rendered lines built from `machine`.
    """
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    lines = text.split('\n')
    if not lines or lines[0] != '---':
        raise FrontmatterError(path, 1, "file must start with '---'")
    end = None
    for i in range(1, len(lines)):
        if lines[i] == '---':
            end = i
            break
    if end is None:
        raise FrontmatterError(path, len(lines), "missing closing '---'")
    marker_idx = None
    for i in range(1, end):
        if lines[i].lstrip().startswith(MARKER):
            marker_idx = i
            break
    if marker_idx is None:
        head = lines[1:end] + [MARKER]
    else:
        head = lines[1:marker_idx + 1]
    machine_lines = []
    for k, v in machine.items():
        style = 'block' if k in _LIST_BLOCK_KEYS and isinstance(v, list) else 'inline'
        machine_lines.append(_format_entry(k, v, style))
    body = '\n'.join(lines[end + 1:])
    new_text = '---\n' + '\n'.join(head + machine_lines) + '\n---\n' + body
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text)


#: The machine keys a writer may drop: each is re-derived by every ingest (invariant I1). Any
#: other machine key — ``schema_version``, ``cost``, ``spend_usd``, one a later version adds — is
#: carried through by every writer, byte for byte.
DERIVABLE_KEYS = ('stage', 'evidence', 'blocked', 'blocked_by_open')


def _split_header(path, text):
    lines = text.split('\n')
    if not lines or lines[0] != '---':
        raise FrontmatterError(path, 1, "file must start with '---'")
    end = next((i for i in range(1, len(lines)) if lines[i] == '---'), None)
    if end is None:
        raise FrontmatterError(path, len(lines), "missing closing '---'")
    marker = next((i for i in range(1, end) if lines[i].lstrip().startswith(MARKER)), None)
    return lines, end, marker


def _machine_entries(block):
    """``[(key or None, [raw line, …])]``: one entry per machine key with its continuation lines
    (a ``- item`` list, a nested map); a line that opens no key (a comment) is its own entry."""
    out = []
    for line in block:
        m = _KEY_RE.match(line)
        if m:
            out.append((m.group(1), [line]))
        elif out and line[:1] in (' ', '\t') and out[-1][0] is not None:
            out[-1][1].append(line)
        else:
            out.append((None, [line]))
    return out


def merge_machine(path, updates, drop=(), order=()):
    """Merge ``updates`` into the machine block of an item file in place — never rebuild it.

    Typed lines and the marker are copied through byte-identical. Every machine key already in
    the block keeps its own lines, byte for byte, unless ``updates`` gives it a different value
    (then that key alone is re-rendered where it stands) or ``drop`` names it. A key ``updates``
    adds goes before the first existing key that ``order`` places after it, else at the end.
    ``drop`` may name only :data:`DERIVABLE_KEYS` (I1: a writer never strips another key); any
    other raises ``ValueError``. Returns True when the file changed."""
    stray = [k for k in drop if k not in DERIVABLE_KEYS]
    if stray:
        raise ValueError(f"merge_machine may drop only derivable keys, not {', '.join(stray)}")
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    lines, end, marker = _split_header(path, text)
    meta, _body = parse(text, path=path)
    if marker is None:
        head, block = lines[1:end] + [MARKER], []
    else:
        head, block = lines[1:marker + 1], lines[marker + 1:end]
    rank = {k: i for i, k in enumerate(order)}
    entries = []
    for key, raw in _machine_entries(block):
        if key is not None and key in drop:
            continue
        if key is not None and key in updates:
            value = updates[key]
            if not (key in meta and meta[key] == value and type(meta[key]) is type(value)):
                style = 'block' if key in _LIST_BLOCK_KEYS and isinstance(value, list) else 'inline'
                raw = _format_entry(key, value, style).split('\n')
        entries.append((key, raw))
    present = {k for k, _raw in entries if k is not None}
    for key, value in updates.items():
        if key in present:
            continue
        style = 'block' if key in _LIST_BLOCK_KEYS and isinstance(value, list) else 'inline'
        new = (key, _format_entry(key, value, style).split('\n'))
        at = len(entries)
        if key in rank:
            for i, (k, _raw) in enumerate(entries):
                if k is not None and rank.get(k, len(rank)) > rank[key]:
                    at = i
                    break
        entries.insert(at, new)
        present.add(key)
    machine_lines = [l for _k, raw in entries for l in raw]
    new_text = '\n'.join(['---'] + head + machine_lines + lines[end:])
    if new_text == text:
        return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text)
    return True


def write_typed(path, updates):
    """Rewrite specific TYPED (pre-machine) fields of an item file in place.

    `updates` maps key -> new value; a value of None deletes the key. Existing typed lines not
    named in `updates` are copied through byte-identical, as is the whole machine block (marker
    and everything after it). A new key is appended just before the marker (or at the end of the
    header when there is no machine block). This is T10's one exception to "the machine block is
    the only thing written by a function other than a person" — groom's answers (`decided`,
    `removed`, `rank`, `parent`, `severity`) and the Bug filer's `count`/`links` are typed fields,
    so they land here rather than in write_machine.
    """
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    lines = text.split('\n')
    if not lines or lines[0] != '---':
        raise FrontmatterError(path, 1, "file must start with '---'")
    end = None
    for i in range(1, len(lines)):
        if lines[i] == '---':
            end = i
            break
    if end is None:
        raise FrontmatterError(path, len(lines), "missing closing '---'")
    marker_idx = None
    for i in range(1, end):
        if lines[i].lstrip().startswith(MARKER):
            marker_idx = i
            break
    typed_end = marker_idx if marker_idx is not None else end
    head = lines[1:typed_end]
    tail = lines[typed_end:end]  # the marker line and everything after it, or nothing

    remaining = dict(updates)
    new_head = []
    i = 0
    n = len(head)
    while i < n:
        line = head[i]
        m = _KEY_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            value = remaining.pop(key)
            # skip any continuation lines this key owned (a `- item` or nested `key:` block)
            j = i + 1
            while j < n and re.match(r'^\s+\S', head[j]):
                j += 1
            if value is not None:
                style = 'block' if key in _LIST_BLOCK_KEYS and isinstance(value, list) else 'inline'
                new_head.append(_format_entry(key, value, style))
            i = j
            continue
        new_head.append(line)
        i += 1
    for key, value in remaining.items():
        if value is None:
            continue
        style = 'block' if key in _LIST_BLOCK_KEYS and isinstance(value, list) else 'inline'
        new_head.append(_format_entry(key, value, style))

    body = '\n'.join(lines[end + 1:])
    new_text = '---\n' + '\n'.join(new_head + tail) + '\n---\n' + body
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text)
