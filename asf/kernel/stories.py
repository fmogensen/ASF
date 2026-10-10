"""asf.kernel.stories — the Stories a spec declares, and nothing else (ASF 0.2).

A spec declares a Story in its Stories section (the ``## Stories`` heading, to the next heading of
the same or a higher level) in one of two forms:

- a heading ``### S-<digits>: <title>``, its acceptance lines the bullets beneath it up to the next
  heading;
- a bullet ``- S-<digits>: <title>``, its acceptance lines the bullets nested beneath it.

The id grammar is :data:`asf.record.core.ID_DIGITS`. Only these declarations count: an id named in
prose, anywhere else in the spec or inside a fenced code block (even within the Stories section),
is never declared, and so never minted or checked.
"""
import re

from asf.record.core import ID_DIGITS

_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_BULLET = re.compile(r'^(\s*)[-*+]\s+(.*?)\s*$')
_DECL = re.compile(rf'^(S-{ID_DIGITS})\s*:\s*(.*)$')
_FENCE = re.compile(r'^\s*(```|~~~)')


def _section(lines):
    """The lines of the Stories section, with every fenced block's lines blanked out."""
    out, level, fence = [], None, None
    for line in lines:
        m = _FENCE.match(line)
        if fence or m:
            if fence and m and m.group(1) == fence:
                fence = None
            elif not fence:
                fence = m.group(1)
            if level is not None:
                out.append('')
            continue
        h = _HEADING.match(line)
        if h and level is None:
            if h.group(2).strip().lower() == 'stories':
                level = len(h.group(1))
            continue
        if h and len(h.group(1)) <= level:
            break
        if level is not None:
            out.append(line)
    return out


def declared_stories(spec_text):
    """``{story_id: {'title': str, 'acceptance': [str, ...]}}`` for every Story ``spec_text``
    declares, in declaration order; ``{}`` when it has no Stories section."""
    stories = {}
    current, bullet_indent = None, None  # bullet_indent is None for a heading-form Story
    for line in _section((spec_text or '').splitlines()):
        h = _HEADING.match(line)
        if h:
            d = _DECL.match(h.group(2))
            current, bullet_indent = (d.group(1) if d else None), None
            if d:
                stories.setdefault(current, {'title': d.group(2).strip(), 'acceptance': []})
            continue
        b = _BULLET.match(line)
        if not b:
            continue
        indent, text = len(b.group(1).expandtabs(4)), b.group(2)
        d = _DECL.match(text)
        if d and (bullet_indent is None or indent <= bullet_indent):
            current, bullet_indent = d.group(1), indent
            stories.setdefault(current, {'title': d.group(2).strip(), 'acceptance': []})
        elif current and (bullet_indent is None or indent > bullet_indent):
            stories[current]['acceptance'].append(text)
        elif bullet_indent is not None:
            current = None  # a sibling bullet ends a bullet-form Story
    return stories
