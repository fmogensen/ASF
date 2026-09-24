"""asf.idea.tree — the interrogator's one deliverable, parsed.

A tree is a markdown file: ``# The ask`` and its paragraph, then one block per node, blocks
separated by a ``---`` line. A node's header is ``## <Epic|Feature|Story> <key> [under <key>]:
<title>``; ``### Acceptance`` and ``### Assumptions`` are its bullet sections and everything else
its description. ``### Questions`` is refused: an interrogation proposes, it does not ask (F-0023
D2). Every broken rule is a :class:`TreeError` naming the line — a tree that does not parse
files nothing."""
import re
from collections import namedtuple

from asf.groom.shape import SIZE

HEADER_RE = re.compile(r'^##\s+(Epic|Feature|Story)\s+(\w+)(?:\s+under\s+(\w+))?:\s*(.+)$')
ASK_RE = re.compile(r'^#\s+The ask\s*$')
SECTION_RE = re.compile(r'^###\s+(.+?)\s*$')
BULLET_RE = re.compile(r'^\s*-\s+(?:\[[xX ]\]\s*)?(.*?)\s*$')

Node = namedtuple('Node', 'type key parent_key title description acceptance assumptions')
Tree = namedtuple('Tree', 'ask nodes')

#: What each node type hangs under.
PARENT_TYPE = {'Feature': 'Epic', 'Story': 'Feature'}
_ARTICLE = {'Epic': 'an Epic', 'Feature': 'a Feature', 'Story': 'a Story'}


class TreeError(ValueError):
    """The tree breaks a rule of the grammar; the message names the line."""


def _blocks(text):
    """``[(first line number, [lines])]`` split on lines that are exactly ``---``."""
    blocks, current, start = [], [], 1
    for n, line in enumerate(text.split('\n'), 1):
        if line.strip() == '---':
            blocks.append((start, current))
            current, start = [], n + 1
        else:
            current.append(line)
    blocks.append((start, current))
    return blocks


def _first_content(start, lines):
    """``(line number, line)`` of the first non-blank line, or None."""
    for offset, line in enumerate(lines):
        if line.strip():
            return start + offset, line.strip()
    return None


def _parse_node(start, lines, header_no, match):
    kind, key, parent_key, title = match.groups()
    description, acceptance, assumptions = [], [], []
    section = None
    for offset, line in enumerate(lines[header_no - start + 1:], header_no + 1):
        heading = SECTION_RE.match(line)
        if heading:
            name = heading.group(1).lower()
            if name == 'questions':
                raise TreeError(
                    f"line {offset}: an interrogation proposes, it does not ask — write it as an "
                    f"## Assumptions bullet with the answer you propose (D2)")
            section = name if name in ('acceptance', 'assumptions') else None
            if section is None:
                description.append(line)
            continue
        if section is None:
            description.append(line)
            continue
        bullet = BULLET_RE.match(line)
        if bullet and bullet.group(1):
            (acceptance if section == 'acceptance' else assumptions).append(bullet.group(1))
    return Node(kind, key, parent_key, title.strip(), '\n'.join(description).strip(),
                acceptance, assumptions)


def parse_tree(text, enrich=False):
    """The text of a tree file → ``Tree(ask, nodes)``, or a :class:`TreeError`.

    ``enrich`` reads the tree ``asf idea --enrich`` takes: one node rooted on a card the record
    already holds, so nothing above it needs to be in the file — the header grammar, unique
    keys and the ban on questions still hold, the shape rules do not."""
    blocks = _blocks(text)
    first = _first_content(*blocks[0])
    if not first or not ASK_RE.match(first[1]):
        raise TreeError(f"line {first[0] if first else 1}: the tree opens with # The ask")
    start, lines = blocks[0]
    ask = '\n'.join(lines[first[0] - start + 1:]).strip()

    nodes, seen, header_lines = [], {}, {}
    for start, lines in blocks[1:]:
        first = _first_content(start, lines)
        if not first:
            continue
        line_no, header = first
        match = HEADER_RE.match(header)
        if not match:
            raise TreeError(f"line {line_no}: not a node header")
        kind, key, parent_key = match.group(1), match.group(2), match.group(3)
        if key in seen:
            raise TreeError(f"line {line_no}: key {key} is already used at line {header_lines[key]}")
        if enrich and parent_key is None:
            pass
        elif kind == 'Epic':
            if parent_key:
                raise TreeError(f"line {line_no}: an Epic hangs under nothing — a Feature hangs "
                                f"under an Epic")
        else:
            if not parent_key:
                raise TreeError(f"line {line_no}: {_ARTICLE[kind]} hangs under "
                                f"{_ARTICLE[PARENT_TYPE[kind]]}")
            if parent_key not in seen:
                raise TreeError(f"line {line_no}: under {parent_key} — no such key")
            if seen[parent_key].type != PARENT_TYPE[kind]:
                raise TreeError(f"line {line_no}: {_ARTICLE[kind]} hangs under "
                                f"{_ARTICLE[PARENT_TYPE[kind]]}")
        node = _parse_node(start, lines, line_no, match)
        if kind == 'Story' and not node.acceptance and not enrich:
            raise TreeError(f"line {line_no}: a Story is {SIZE['story']} — {key} has no "
                            f"acceptance item")
        seen[key] = node
        header_lines[key] = line_no
        nodes.append(node)

    for node in nodes:
        if node.type != 'Epic' or enrich:
            continue
        spans = sum(1 for n in nodes if n.type == 'Feature' and n.parent_key == node.key)
        if spans < 2:
            raise TreeError(f"line {header_lines[node.key]}: the Epic {node.key} spans {spans} "
                            f"Feature{'' if spans == 1 else 's'} — an Epic is {SIZE['epic']}")
    return Tree(ask, nodes)
