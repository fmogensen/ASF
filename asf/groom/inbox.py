"""asf.groom.inbox — turn <intake_dir>/*.md into cards or one question, by shape."""
import os
import re

from asf import env
from asf.groom.shape import Card, Question, derive, infer_parent_epic
from asf.record.ids import mint_id, write_new_item
from asf.conventions import DEFAULT_INTAKE_DIR

INBOX_KV_RE = re.compile(r'^(type|parent|signature|severity|writes|stories):\s*(.+?)\s*$', re.IGNORECASE)


def intake_dir(args):
    try:
        return env.load_product(getattr(args, 'product', None)).conventions.intake_dir
    except env.ConfigError:
        return DEFAULT_INTAKE_DIR


def _lift_section(lines, heading):
    """Pull a `## <heading>` block out of `lines`, returning (items, remaining_lines)."""
    start = None
    for i, l in enumerate(lines):
        if l.strip() == f"## {heading}":
            start = i
            break
    if start is None:
        return [], lines
    end = start + 1
    while end < len(lines) and not lines[end].startswith('## '):
        end += 1
    items = []
    for l in lines[start + 1:end]:
        s = l.strip()
        if not s.startswith('- '):
            continue
        if heading == 'Acceptance':
            m = re.match(r'^-\s*\[[xX ]\]\s*(.*)$', s)
            if m and m.group(1):
                items.append(m.group(1))
        else:
            items.append(s[2:])
    return items, lines[:start] + lines[end:]


def parse_inbox_file(text):
    """A `shape.Card` from one inbox/*.md file's raw text."""
    lines = text.split('\n')
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    title = re.sub(r'^#+\s*', '', lines[idx].strip()) if idx < len(lines) else ''
    rest = lines[idx + 1:] if idx < len(lines) else []

    headers = {}
    body_start = 0
    for i, l in enumerate(rest):
        s = l.strip()
        if not s:
            continue
        m = INBOX_KV_RE.match(s)
        if not m:
            body_start = i
            break
        headers[m.group(1).lower()] = m.group(2).strip()
        body_start = i + 1
    else:
        body_start = len(rest)

    body_lines = rest[body_start:]
    features, body_lines = _lift_section(body_lines, 'Features')
    acceptance, body_lines = _lift_section(body_lines, 'Acceptance')
    description = '\n'.join(body_lines).strip()
    return Card(title, headers, description, features, acceptance)


def process_inbox(root, canonical, date, default_bug_parent=None, intake_dir='inbox'):
    """Turn every <intake_dir>/*.md into a card (moved to <intake_dir>/done/) or leave one
    `## Question` in place. Returns the list of newly minted ids, in filename order.

    `default_bug_parent` is the item a Bug with no explicit `parent:` line is filed under; when
    None (no such convention configured), a Bug always asks for its parent explicitly.
    """
    inbox_dir = os.path.join(root, intake_dir)
    if not os.path.isdir(inbox_dir):
        return []
    created = []
    for name in sorted(os.listdir(inbox_dir)):
        path = os.path.join(inbox_dir, name)
        if not name.endswith('.md') or not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            text = f.read()
        if '\n## Question' in text or text.startswith('## Question'):
            continue  # already asked; waiting on a human edit

        card = parse_inbox_file(text)
        result = derive(card, canonical, default_bug_parent=default_bug_parent)

        if isinstance(result, Question):
            new_text = text.rstrip('\n') + f"\n\n## Question\n{result.text}\n"
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_text)
            continue

        type_, rule, parent = result.type, result.rule, result.parent
        new_id = mint_id(root, canonical, type_)
        typed = {'title': card.title, 'parent': parent, 'decided': False}
        if type_ == 'bug':
            severity = card.headers.get('severity')
            typed['severity'] = severity if severity in ('S1', 'S2', 'S3') else 'S3'
            typed['found_in'] = 'dev'
            typed['signature'] = card.headers.get('signature')
        elif type_ == 'task':
            typed['writes'] = [w.strip() for w in card.headers.get('writes', '').split(',') if w.strip()]
            typed['stories'] = [s.strip() for s in card.headers.get('stories', '').split(',') if s.strip()]
        write_new_item(root, canonical, type_, new_id, typed, card.description, date, 'inbox',
                        acceptance=card.acceptance, sections={'Features': card.features},
                        shape=(rule, type_))
        created.append(new_id)

        done_dir = os.path.join(inbox_dir, 'done')
        os.makedirs(done_dir, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '-', card.title.lower()).strip('-') or name[:-3]
        with open(os.path.join(done_dir, f"{slug}.md"), 'w', encoding='utf-8') as f:
            f.write(f"→ {new_id}\n\n{text}")
        os.remove(path)
    return created
