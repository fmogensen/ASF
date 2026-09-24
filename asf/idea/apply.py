"""asf.idea.apply — a tree, filed as intake cards.

No model call: a parsed tree in, one intake file per node the record does not already answer
out (F-0023). The groom types and mints them; a parent always sorts before its children, so the
groom's one pass mints the parent first (D3). Every card is rendered in memory before any file is
written, so one failure writes nothing (D11)."""
import datetime
import json
import os
import re
from collections import namedtuple

from asf.conventions import DEFAULT_ANSWER_OVERLAP
from asf.idea.answered import Answered, answer_from_record
from asf.record import core, frontmatter, setfield
from asf.record.core import ID_RE
from asf.record.publish import publish

Applied = namedtuple('Applied', 'filed answered report_path')

STAMP_FORMAT = '%Y%m%dT%H%M%SZ'


def new_stamp(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime(STAMP_FORMAT)


def unused_stamp(intake_dir, stamp):
    """``stamp``, or the next second that has no report in ``<intake_dir>/done`` — a re-applied
    tree in the same second must not overwrite the first run's files."""
    when = datetime.datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=datetime.timezone.utc)
    while os.path.exists(os.path.join(intake_dir, 'done', f'idea-{stamp}.md')):
        when += datetime.timedelta(seconds=1)
        stamp = new_stamp(when)
    return stamp


def _slug(title):
    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:48].strip('-') or 'card'


def _file_name(stamp, index, node):
    return f'idea-{stamp}-{index:02d}-{node.type.lower()}-{_slug(node.title)}.md'


def render_intake(node, parent, children):
    """The intake file's text. ``parent`` is the ``parent:`` value or None; ``children`` the
    titles an Epic lists under ``## Features``."""
    lines = [f'# {node.title}']
    if parent:
        lines.append(f'parent: {parent}')
    lines.append('')
    if node.description:
        lines += [node.description, '']
    if node.type == 'Epic':
        lines += ['## Features'] + [f'- {title}' for title in children] + ['']
    if node.assumptions:
        lines += ['## Assumptions'] + [f'- {item}' for item in node.assumptions] + ['']
    if node.acceptance:
        lines += ['## Acceptance'] + [f'- [ ] {item}' for item in node.acceptance] + ['']
    return '\n'.join(lines).rstrip('\n') + '\n'


def _report(tree, tree_text, rows, stamp):
    lines = [f'# Idea {stamp}', '', '## The ask', tree.ask, '', '## Nodes']
    for node, outcome in rows:
        lines.append(f'- {node.key} {node.type} — {node.title}: {outcome}')
    lines += ['', '## The tree', '', tree_text.rstrip('\n'), '']
    return '\n'.join(lines)


def apply_tree(root, tree, canonical, intake_dir, stamp, overlap=DEFAULT_ANSWER_OVERLAP,
               tree_text=''):
    """File ``tree``'s nodes into ``intake_dir`` (an absolute path) and write the run's report to
    ``<intake_dir>/done/idea-<stamp>.md``. Returns ``Applied(filed, answered, report_path)``:
    ``filed`` the intake file names in tree order, ``answered`` the ``Answered`` of every node the
    record answered — a node under one is recorded as answered by the same item and filed for
    nobody. ``tree_text`` is the tree as it was read, kept whole in the report."""
    file_of, answer_of, plan, rows = {}, {}, [], []
    for index, node in enumerate(tree.nodes, 1):
        inherited = answer_of.get(node.parent_key)
        answer = inherited or answer_from_record(node, canonical, overlap)
        if answer:
            answer_of[node.key] = Answered(node, answer.source, answer.text)
            rows.append((node, f'answered by {answer.source} — {answer.text}'))
            continue
        file_of[node.key] = _file_name(stamp, index, node)
        if node.parent_key in file_of:
            parent = f'inbox:{file_of[node.parent_key]}'
        elif node.parent_key and ID_RE.match(node.parent_key):
            parent = node.parent_key
        else:
            parent = None
        children = [n.title for n in tree.nodes
                    if n.type == 'Feature' and n.parent_key == node.key]
        plan.append((file_of[node.key], render_intake(node, parent, children)))
        rows.append((node, f'filed {file_of[node.key]}'))

    report = _report(tree, tree_text, rows, stamp)

    os.makedirs(os.path.join(intake_dir, 'done'), exist_ok=True)
    written = []
    for name, text in plan:
        path = os.path.join(intake_dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        written.append(path)
    report_path = os.path.join(intake_dir, 'done', f'idea-{stamp}.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    for path in written + [report_path]:
        publish(root, path, f'record: idea {os.path.basename(path)}')
    return Applied([name for name, _ in plan],
                   [answer_of[n.key] for n in tree.nodes if n.key in answer_of], report_path)


def write_applied(tree_path, stamp, applied, report_rel):
    """``<tree>.applied``, last: the guard against a second run and what the front door prints."""
    data = {'stamp': stamp, 'filed': applied.filed,
            'answered': [{'node': a.node.key, 'id': a.source} for a in applied.answered],
            'report': report_rel}
    path = tree_path + '.applied'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    return data


# ---- the other writer --------------------------------------------------------

EMPTY_ITEM_RE = re.compile(r'^\s*-\s*\[ \]\s*$')


def _lines_of(content):
    return content.strip('\n').split('\n') if content.strip('\n') else []


def _section_index(sections, heading):
    return next((i for i, (h, _) in enumerate(sections) if h.strip() == heading), None)


def _enriched_body(body, node, date):
    """``body`` with the node's description, acceptance and assumptions written in and one
    History line; every other section is carried over untouched."""
    preamble, sections = core.parse_sections(body)
    touched = {}

    i = _section_index(sections, '## Description')
    if node.description:
        lines = _lines_of(sections[i][1]) if i is not None else []
        lines += ([''] if lines else []) + node.description.split('\n')
        touched['## Description'] = lines
    i = _section_index(sections, '## Acceptance')
    if node.acceptance:
        lines = [l for l in (_lines_of(sections[i][1]) if i is not None else [])
                 if not EMPTY_ITEM_RE.match(l)]
        touched['## Acceptance'] = lines + [f'- [ ] {item}' for item in node.acceptance]
    i = _section_index(sections, '## Assumptions')
    if node.assumptions:
        lines = _lines_of(sections[i][1]) if i is not None else []
        touched['## Assumptions'] = lines + [f'- {item}' for item in node.assumptions]
    i = _section_index(sections, '## History')
    history = _lines_of(sections[i][1]) if i is not None else []
    counts = f'+{len(node.acceptance)} acceptance, +{len(node.assumptions)} assumption(s)'
    touched['## History'] = history + [f'- {date}: enriched (idea) — {counts}']

    for heading, lines in touched.items():
        i = _section_index(sections, heading)
        if i is not None:
            sections[i][1] = core.section_content(lines, i == len(sections) - 1)
        elif heading == '## Assumptions':   # a new section sits immediately above ## Acceptance
            at = _section_index(sections, '## Acceptance')
            at = len(sections) if at is None else at
            sections.insert(at, [heading, core.section_content(lines, False)])
        else:
            if sections and not sections[-1][1].endswith('\n\n'):
                sections[-1][1] += '\n'
            sections.append([heading, core.section_content(lines, True)])
    return core.render_sections(preamble, sections)


def enrich_card(root, canonical, item_id, node, date):
    """Write one node's worth of substance into the Feature card ``item_id`` (``asf idea
    --enrich``). Returns None on success, else the reason the card is untouched: the write goes
    through the ``asf set`` round trip (render, re-parse, compare) and a card that does not come
    back as what was asked is left exactly as it was."""
    rec = canonical.get(item_id)
    if rec is None:
        return f'no item {item_id!r}'
    if rec['meta'].get('type') != 'feature':
        return f"{item_id} is a {rec['meta'].get('type')}, and only a Feature is enriched"
    body = rec['body']
    if not rec['text'].endswith(body):
        return f"{rec['relpath']} does not split into a header and a body; it is unchanged"
    new_body = _enriched_body(body, node, date)
    original = rec['text']
    staged = dict(rec, text=original[:len(original) - len(body)] + new_body)
    err = setfield.set_typed(staged, {'enriched': date})
    if err:
        return err
    try:
        with open(rec['path'], encoding='utf-8') as f:
            written = f.read()
        _meta, written_body = frontmatter.parse(written, path=rec['relpath'])
    except (OSError, frontmatter.FrontmatterError):
        written_body = None
    if written_body != new_body:
        with open(rec['path'], 'w', encoding='utf-8') as f:
            f.write(original)
        return f"{rec['relpath']} does not round-trip through the parser; it is unchanged"
    rec['text'] = written
    rec['body'] = written_body
    return None
