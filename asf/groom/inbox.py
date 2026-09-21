"""asf.groom.inbox — turn inbox/*.md into cards or one question."""
import os
import re

from asf.record.core import is_open, tokenize
from asf.record.ids import mint_id, write_new_item

INBOX_TYPES = ('bug', 'epic', 'feature')
INBOX_KV_RE = re.compile(r'^(type|parent):\s*(.+?)\s*$', re.IGNORECASE)
BROKEN_WORDS_RE = re.compile(r'\b(broken|red|fails?|failing)\b', re.IGNORECASE)
GOAL_WORD_RE = re.compile(r'\bgoal\b', re.IGNORECASE)


def infer_inbox_type(text):
    if BROKEN_WORDS_RE.search(text):
        return 'bug'
    if GOAL_WORD_RE.search(text):
        return 'epic'
    return 'feature'


def infer_parent_epic(canonical, tokens):
    """The open Epic sharing the most title words with `tokens`, or None."""
    best_id, best_n = None, 0
    for iid, rec in sorted(canonical.items()):
        if rec['meta'].get('type') != 'epic' or not is_open(rec):
            continue
        shared = len(tokens & tokenize(rec['meta'].get('title', '')))
        if shared > best_n:
            best_id, best_n = iid, shared
    return best_id


def parse_inbox_file(text):
    """(title, type_or_None, parent_or_None, body) from one inbox/*.md file's raw text."""
    lines = text.split('\n')
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    title = re.sub(r'^#+\s*', '', lines[idx].strip()) if idx < len(lines) else ''
    rest = lines[idx + 1:] if idx < len(lines) else []

    type_ = None
    parent = None
    body_lines = []
    for l in rest:
        m = INBOX_KV_RE.match(l.strip())
        if m and body_lines == [] and l.strip():
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == 'type':
                type_ = val.lower()
            else:
                parent = val
            continue
        body_lines.append(l)
    body = '\n'.join(body_lines).strip()
    return title, type_, parent, body


def process_inbox(root, canonical, date, default_bug_parent=None):
    """Turn every inbox/*.md into a card (moved to inbox/done/) or leave one `## Question` in
    place. Returns the list of newly minted ids, in filename order.

    `default_bug_parent` is the item a Bug with no explicit `parent:` line is filed under; when
    None (no such convention configured), a Bug always asks for its parent explicitly.
    """
    inbox_dir = os.path.join(root, 'inbox')
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

        title, type_in, parent_in, body = parse_inbox_file(text)
        question = None

        if type_in:
            if type_in not in INBOX_TYPES:
                question = f"Unrecognized `type: {type_in}` — use bug, epic or feature, or remove the line."
            type_ = type_in
        else:
            type_ = infer_inbox_type(title + '\n' + body)

        parent = None
        if question is None and type_ == 'feature':
            if parent_in:
                if parent_in in canonical:
                    parent = parent_in
                else:
                    question = f"`parent: {parent_in}` does not exist — name an existing Epic or remove the line."
            else:
                parent = infer_parent_epic(canonical, tokenize(title))
                if parent is None:
                    question = "Which Epic is this under? No open Epic shares a title word with it."
        elif question is None and type_ == 'bug':
            if parent_in:
                if parent_in not in canonical:
                    question = f"`parent: {parent_in}` does not exist — name an existing item or remove the line."
                else:
                    parent = parent_in
            elif default_bug_parent:
                parent = default_bug_parent
            else:
                question = "Which item is this Bug under? Add a `parent: <id>` line."

        if question:
            new_text = text.rstrip('\n') + f"\n\n## Question\n{question}\n"
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_text)
            continue

        new_id = mint_id(root, canonical, type_)
        typed = {'title': title, 'parent': parent, 'decided': False}
        if type_ == 'bug':
            typed['severity'] = 'S3'
            typed['found_in'] = 'dev'
        write_new_item(root, canonical, type_, new_id, typed, body, date, 'inbox')
        created.append(new_id)

        done_dir = os.path.join(inbox_dir, 'done')
        os.makedirs(done_dir, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or name[:-3]
        with open(os.path.join(done_dir, f"{slug}.md"), 'w', encoding='utf-8') as f:
            f.write(f"→ {new_id}\n\n{text}")
        os.remove(path)
    return created
