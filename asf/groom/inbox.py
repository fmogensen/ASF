"""asf.groom.inbox — turn <intake_dir>/*.md into cards or one question, by shape."""
import os
import re

from asf.groom.shape import Card, Question, derive, infer_parent_epic
from asf.record.ids import mint_id, write_new_item
from asf.conventions import DEFAULT_INTAKE_DIR

INBOX_KV_RE = re.compile(r'^(type|parent|signature|severity|writes|stories):\s*(.+?)\s*$', re.IGNORECASE)


def _intake_dir(args):
    """The intake directory `asf inbox` files into: the product's convention, else the
    documented default when there is no product config to read (a test, or the command run from
    inside a record checkout on its own).

    `process_inbox` takes its own `intake_dir` from its caller instead — `cmd_groom` already
    holds the Product — so this resolution lives here, where `args` is the only thing on hand.
    """
    from asf import env
    try:
        return env.load_product(getattr(args, 'product', None)).conventions.intake_dir
    except (env.ConfigError, OSError):
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


def cmd_inbox(args, root):
    """``asf inbox --title T [--body-file F] [--parent ID]``: one untyped card into the
    record's intake directory. Mints nothing and runs no groom (D10)."""
    d = os.path.join(root, _intake_dir(args))
    os.makedirs(d, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '-', args.title.lower()).strip('-') or 'card'
    name = f"{slug}.md"
    n = 2
    while os.path.exists(os.path.join(d, name)):
        name = f"{slug}-{n}.md"
        n += 1
    text = f"# {args.title}\n"
    if args.parent:
        text += f"parent: {args.parent}\n"
    text += "\n"
    if args.body_file:
        with open(args.body_file, encoding='utf-8') as f:
            text += f.read()
    path = os.path.join(d, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(os.path.relpath(path, root))
    return 0


def process_inbox(root, canonical, date, default_bug_parent=None, intake_dir=None):
    """Turn every <intake_dir>/*.md into a card (moved to <intake_dir>/done/) or leave one
    `## Question` in place. Returns the list of newly minted ids, in filename order.

    `default_bug_parent` is the item a Bug with no explicit `parent:` line is filed under; when
    None (no such convention configured), a Bug always asks for its parent explicitly.

    `intake_dir` is where a human drops a card for the groom to intake, relative to `root`; None
    falls back to the documented default. The `why` a created card's History records —
    `created (inbox)` — stays the literal word regardless: it names the intake stream, not
    this path.
    """
    inbox_dir = os.path.join(root, intake_dir or DEFAULT_INTAKE_DIR)
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
            continue  # already asked: the groom puts it to the adjudicator (question_lines)

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


# ---- the questions intake leaves, put to the groom (F-0085 under approvals.groom: auto) ------

#: A groom line's item token for an inbox card, which has no id yet: ``inbox:<file name>``.
TOKEN_PREFIX = 'inbox:'
QUESTION_HEADING = '## Question'


def _split_question(text):
    """``(card text without its ## Question block, the question)``."""
    lines = text.split('\n')
    start = next((i for i, l in enumerate(lines) if l.strip() == QUESTION_HEADING), None)
    if start is None:
        return text, ''
    end = start + 1
    while end < len(lines) and not lines[end].startswith('## '):
        end += 1
    question = ' '.join(l.strip() for l in lines[start + 1:end] if l.strip())
    rest = lines[:start] + lines[end:]
    return '\n'.join(rest).rstrip('\n') + '\n', question


def question_lines(root, intake_dir=None):
    """One open groom line per inbox card intake asked a question of, in file-name order:
    ``- [ ] inbox:<name> <title> — <question> → answer: ____``. The groom file carries them so
    the adjudicator answers them (:func:`apply_answer`) — nobody edits a card by hand."""
    d = os.path.join(root, intake_dir or DEFAULT_INTAKE_DIR)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if not name.endswith('.md') or not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            text = f.read()
        body, question = _split_question(text)
        if not question:
            continue
        title = ' '.join(parse_inbox_file(body).title.split())
        out.append(f"- [ ] {TOKEN_PREFIX}{name} {title} — {question} → answer: ____")
    return out


_CLAUSES = (
    (re.compile(r'^feature$', re.IGNORECASE), lambda m: ('type', 'feature')),
    (re.compile(r'^bug\s+(.+)$', re.IGNORECASE), lambda m: ('signature', m.group(1).strip())),
    (re.compile(r'^parent\s+([A-Z]-\d{4})$', re.IGNORECASE), lambda m: ('parent', m.group(1).upper())),
    (re.compile(r'^(S[123])$', re.IGNORECASE), lambda m: ('severity', m.group(1).upper())),
)
_CLOSE_RE = re.compile(r'^(no|close)$', re.IGNORECASE)


def parse_answer(answer):
    """An inbox answer: ``'close'``, or ``{header: value}`` from ``;``-separated clauses —
    ``feature``, ``bug <signature>``, ``parent <id>``, ``S1|S2|S3`` — or None when any clause is
    outside that grammar (the line then changes nothing)."""
    a = answer.strip()
    if _CLOSE_RE.match(a):
        return 'close'
    headers = {}
    for clause in (c.strip() for c in a.split(';')):
        for rx, make in _CLAUSES:
            m = rx.match(clause)
            if m:
                k, v = make(m)
                headers[k] = v
                break
        else:
            return None
    return headers or None


def apply_answer(root, name, answer, date, who, intake_dir=None):
    """Apply one answer to ``<intake_dir>/<name>``: its ``## Question`` block goes, the answer's
    header lines go in under the title (replacing one of the same key), and the next intake
    reads the card again — a card or, still unsettled, a fresh question. ``close`` moves it to
    ``done/`` unminted. Returns True when the card changed."""
    d = os.path.join(root, intake_dir or DEFAULT_INTAKE_DIR)
    path = os.path.join(d, name)
    parsed = parse_answer(answer)
    if parsed is None or os.path.basename(name) != name or not os.path.isfile(path):
        return False
    with open(path, encoding='utf-8') as f:
        text = f.read()
    body, _question = _split_question(text)
    if parsed == 'close':
        os.makedirs(os.path.join(d, 'done'), exist_ok=True)
        with open(os.path.join(d, 'done', name), 'w', encoding='utf-8') as f:
            f.write(f"→ closed (groom {date}, {who})\n\n{text}")
        os.remove(path)
        return True
    lines = body.split('\n')
    idx = next((i for i, l in enumerate(lines) if l.strip()), 0)
    keep = [l for l in lines[idx + 1:]
            if not ((m := INBOX_KV_RE.match(l.strip())) and m.group(1).lower() in parsed)]
    new = lines[:idx + 1] + [f'{k}: {v}' for k, v in parsed.items()] + keep
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(new))
    return True
