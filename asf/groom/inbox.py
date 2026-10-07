"""asf.groom.inbox — turn <intake_dir>/*.md into cards or one question, by shape. Three of
those reads are inferred rather than declared: a title's `S1:`/`S2:`/`S3:` prefix is a severity
(`graded`, `severity_of`), and a body `Error:` line or a named failing spec is the signature a
`type: bug` card was being asked for (`body_signature`, `signed`). A card still stuck on a
question at S1 is said aloud, every tick, naming its file (`stuck_s1_lines`); `question_lines`
carries the same severity as a token."""
import os
import re

from asf.groom.shape import Card, Question, derive, infer_parent_epic
from asf.record.ids import mint_id, write_new_item
from asf.record.publish import publish
from asf.conventions import DEFAULT_INTAKE_DIR

INBOX_KV_RE = re.compile(r'^(type|parent|signature|severity|writes|stories|moved_from):\s*(.+?)\s*$',
                         re.IGNORECASE)

#: A title may open with its severity: `S1: p1-e2e is failing on main` (C4). Case-insensitive,
#: and the match must leave a non-empty title behind — `S3: the third option` is a title, so the
#: `rest` group is what guards it, not a lookahead.
_SEVERITY_PREFIX_RE = re.compile(r'^\s*(S[123])\s*:\s*(?P<rest>\S.*?)\s*$', re.IGNORECASE)

SEVERITIES = ('S1', 'S2', 'S3')

#: A body-inferred signature is capped at the same length a signature is already capped at
#: elsewhere, so it is never longer than one the metrics stream will store or one a flake-filed
#: Bug's title carries: `asf/metrics/metrics.py:1650` (`str(signature)[:120]`) and
#: `asf.tick.flaky.TITLE_MAX`.
SIGNATURE_MAX = 120

#: A `NEEDS OPERATOR` question is capped at the same length the repo's two existing caps for one
#: line of prose quoted into a line a person reads: `asf.credentials.DETAIL_MAX` and
#: `asf.tick.widen_footprint.REASON_MAX`.
QUESTION_MAX = 160

#: Header keys intake does not (yet) read a value for — `after:` on a Task, say. Named here, not
#: matched by shape alone (B-0111 C1): a body line that merely *looks* header-shaped (`Note: …`,
#: a URL) is not one of these, so it is never mistaken for a header, no matter where it sits. A
#: line naming one of these keys always is a header, so it is never mistaken for body either,
#: even as the last line before body confirms nothing further (B-0111 round 2 C1).
_UNREAD_HEADER_KEYS = ('after',)
_UNKNOWN_HEADER_RE = re.compile(r'^(' + '|'.join(_UNREAD_HEADER_KEYS) + r'):\s*.*$', re.IGNORECASE)


#: Is `s` (already stripped) a header line, by either regex intake knows (F-0134): B-0111
#: settled that a header is a header wherever it sits in the *last* position, this settles the
#: *first* — one helper, so `parse_inbox_file` and `apply_answer` cannot drift on the question
#: (PD4).
def _is_header_line(s):
    return bool(s) and bool(INBOX_KV_RE.match(s) or _UNKNOWN_HEADER_RE.match(s))


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
    """A `shape.Card` from one inbox/*.md file's raw text.

    The title is the first non-blank line with its `#` run stripped — unless that line is itself
    a header line, in which case the card has **no** title and the header block starts there
    (C1). A card dropped into the intake dir leading with `type: bug` used to have that line
    eaten as its title, so `headers` never carried the type the operator declared and the card
    was minted by shape alone (F-0134). A header is a header wherever it sits, first or last;
    B-0111 settled the last position, this settles the first. An untitled card is never minted:
    `shape.derive` refuses it (C2, C3).
    """
    lines = text.split('\n')
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    first = lines[idx].strip() if idx < len(lines) else ''
    if first and _is_header_line(first):
        title, rest = '', lines[idx:]
    else:
        title, rest = re.sub(r'^#+\s*', '', first), lines[idx + 1:]

    # A line naming a key intake reads (`INBOX_KV_RE`) or one it merely knows of but does not
    # read (`_UNKNOWN_HEADER_RE`, `after:` and the like) is a header either way, so `body_start`
    # advances past both — that is what keeps a real header from leaking into the body no
    # matter where it sits, first or last (B-0111 round 2 C1). Neither regex matches shape
    # alone (`^word:`), so a body's first line that merely looks header-shaped (`Note: …`, a
    # URL) is never mistaken for one and stops the block instead (B-0111 C1).
    headers = {}
    body_start = 0
    for i, l in enumerate(rest):
        s = l.strip()
        if not s:
            continue
        m = INBOX_KV_RE.match(s)
        if m:
            headers[m.group(1).lower()] = m.group(2).strip()
            body_start = i + 1
            continue
        if _UNKNOWN_HEADER_RE.match(s):
            body_start = i + 1
            continue
        break

    body_lines = rest[body_start:]
    features, body_lines = _lift_section(body_lines, 'Features')
    acceptance, body_lines = _lift_section(body_lines, 'Acceptance')
    description = '\n'.join(body_lines).strip()
    return Card(title, headers, description, features, acceptance)


def header_list(value):
    """A list header's entries: ``a, b``, ``[a, b]``, ``['a', "b"]`` and ``[[S-0001]]`` wiki links
    all read as the bare entries — the brackets and quotes are the list's syntax, never part of an
    entry (an inbox Task's ``writes: [a, b]`` was filed as the one glob ``"[a, b]"``)."""
    text = str(value or '').strip()
    if text.startswith('[') and text.endswith(']') and not (
            text.startswith('[[') and text.endswith(']]') and text.count('[[') == 1):
        text = text[1:-1].strip()
    out = []
    for part in text.split(','):
        part = part.strip().strip('\'"').strip()
        m = re.fullmatch(r'\[\[([^\]]+)\]\]', part)
        if m:
            part = m.group(1).strip()
        if part:
            out.append(part)
    return out


def severity_of(card):
    """The card's severity — its ``severity:`` header, upper-cased, when that names one of
    :data:`SEVERITIES`; ``S3`` otherwise. The one reader of the field: the Bug intake mints and
    the stuck-card line (:func:`stuck_s1_lines`) both come through here, so a card cannot be S1
    in one view and S3 in the other (C5). ``severity: s1`` is S1, which it was not before."""
    severity = (card.headers.get('severity') or '').strip().upper()
    return severity if severity in SEVERITIES else 'S3'


def graded(card):
    """A card whose title opens with ``S1:``/``S2:``/``S3:`` (C4), with that prefix cut from the
    title and — when no ``severity:`` header says otherwise (C3) — recorded as one. The prefix
    is cut either way (C2): a severity is a field, and a title that also carries it is the same
    fact written twice. A title with no prefix comes back unchanged (identity, not a copy)."""
    m = _SEVERITY_PREFIX_RE.match(card.title)
    if not m:
        return card
    headers = card.headers
    if not headers.get('severity'):
        headers = dict(headers, severity=m.group(1).upper())
    return card._replace(title=m.group('rest'), headers=headers)


#: An error line, taken whole: ``Error:``, ``TypeError:``, ``AssertionError:``,
#: ``asf.env.ConfigError:`` — any dotted name ending in ``Error:``, anchored at the line start.
_ERROR_LINE_RE = re.compile(r'^[\w.]*Error:\s*\S')


def body_signature(description):
    """The signature a card's body offers, or ``None``: the first line that is an error line or
    a named failing spec, whitespace-folded.

    Two grammars, in this order:

    * ``<Name>Error: <text>`` — ``Error:``, ``TypeError:``, ``AssertionError:``,
      ``asf.env.ConfigError:`` — taken whole, as the operator wrote it.
    * a failing spec line, parsed by :data:`asf.tick.flaky._TEST_RE` and formatted by
      :func:`asf.tick.flaky.test_key` as ``<file>:<line> › <title>``, so it reads as the
      signature a CI-filed flake carries — without ``flaky.SIG_PREFIX``, which claims a retry
      passed the test and is not this card's to claim (C12). The import is function-local:
      ``asf.groom`` stays out of ``asf.tick`` (P9).

    Capped at :data:`SIGNATURE_MAX` characters with a trailing ``…``."""
    from asf.tick import flaky  # inside: asf.groom stays out of asf.tick (P9)
    for raw in (description or '').split('\n'):
        line = ' '.join(raw.split())
        if not line:
            continue
        if _ERROR_LINE_RE.match(line):
            sig = line
        else:
            m = flaky._TEST_RE.match(line)
            if not m:
                continue
            sig = flaky.test_key({'file': m.group('file').strip(), 'line': int(m.group('line')),
                                  'title': m.group('title')})
        if len(sig) > SIGNATURE_MAX:
            sig = sig[:SIGNATURE_MAX - 1] + '…'
        return sig
    return None


def signed(card):
    """A card with no ``signature:`` whose body names one (:func:`body_signature`) gets it —
    the one header between a card that carries its own evidence and a Bug (C6).

    Filled only when the card carries no ``signature:``, no ``writes:``, no ``## Acceptance``
    list and no ``type:`` other than ``bug``: a card with an acceptance list is work, a card
    with ``writes:`` is a Task, and ``type: feature`` settles the reading as it does today.
    Deliberately *not* gated on ``shape.DEFECT_WORDS_RE`` — ``flaky`` is not one of its words
    (P4), so a defect-word gate would miss the card this rule exists for."""
    headers = card.headers
    type_ = (headers.get('type') or '').strip().lower()
    if (headers.get('signature') or headers.get('writes') or card.acceptance
            or (type_ and type_ != 'bug')):
        return card
    sig = body_signature(card.description)
    if sig is None:
        return card
    return card._replace(headers=dict(headers, signature=sig))


def title_signature(title):
    """The signature an operator's ``type: bug`` card gets when it names none: its title,
    whitespace folded — a Bug is keyed on its signature, and a title is the one line it has."""
    return ' '.join(str(title or '').split()) or 'untitled defect'


def declared(card):
    """An explicit ``type:`` line decides the minted type (I13); the shape rule only fills what is
    missing. A ``type: bug`` card with no ``signature:`` gets one from its title
    (:func:`title_signature`), so it is minted a Bug — never read by its shape as a Feature. Any
    other declared type the shape does not reach stays a question (``shape._typed``), never the
    other type."""
    t = (card.headers.get('type') or '').strip().lower()
    if t == 'bug' and not card.headers.get('signature') and not card.headers.get('writes'):
        return card._replace(headers=dict(card.headers, signature=title_signature(card.title)))
    return card


def scrub_title(card, root=None):
    """The card's title passed through the redaction filter before it is minted: a protected name
    or a secret in it becomes a neutral token, never copied into the record (and from there into
    other cards' derived Backlinks)."""
    from asf import redact
    pats = redact.default_patterns(root)
    title = redact.scrub(card.title, pats) if pats else card.title
    return card if title == card.title else card._replace(title=title)


def cmd_inbox(args, root):
    """``asf inbox --title T [--body-file F] [--parent ID]``: one untyped card into the
    record's intake directory. Mints nothing and runs no groom (D10)."""
    text = ''
    if args.parent:
        text += f"parent: {args.parent}\n"
    text += "\n"
    if args.body_file:
        with open(args.body_file, encoding='utf-8') as f:
            text += f.read()
    path = file_card(root, _intake_dir(args), args.title, text)
    print(os.path.relpath(path, root))
    return 0


def file_card(root, intake_dir, title, rest):
    """Write one card — ``# <title>`` then ``rest`` (its header lines and body) — into
    ``<root>/<intake_dir>/`` under a free name derived from the title, and publish it. The path
    written. The one way a card enters the intake directory: ``asf inbox`` and ASF's own filers
    (:mod:`asf.trunk_red`) alike."""
    d = os.path.join(root, intake_dir)
    os.makedirs(d, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or 'card'
    name = f"{slug}.md"
    n = 2
    while os.path.exists(os.path.join(d, name)):
        name = f"{slug}-{n}.md"
        n += 1
    path = os.path.join(d, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n" + rest)
    publish(root, path, f"record: inbox {os.path.basename(path)}")
    return path


def process_inbox(root, canonical, date, default_bug_parent=None, intake_dir=None, asked=None):
    """Turn every <intake_dir>/*.md into a card (moved to <intake_dir>/done/) or leave one
    `## Question` in place. Returns the list of newly minted ids, in filename order.

    A card that already carries a question is read again, without it, every run: one edited
    since (a `signature:` line, an `## Acceptance` list added) is typed, or asked its new
    question; one that still reads the same is left untouched, so re-running changes nothing.
    `asked`, when given, collects the file names that got a question on this run.

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
    retired = None
    for name in sorted(os.listdir(inbox_dir)):
        path = os.path.join(inbox_dir, name)
        if not name.endswith('.md') or not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            text = f.read()
        body, prior = _split_question(text) if _has_question(text) else (text, '')

        card = declared(signed(graded(scrub_title(parse_inbox_file(body), root))))
        if retired is None:
            retired = retired_keys(canonical, inbox_dir)
        gone = retired_as(card, retired)
        if gone:  # retired once already (`asf retire`): never minted again
            move_to_done(inbox_dir, name, f"→ already retired as {gone} (groom {date})", text)
            continue
        result = derive(card, canonical, default_bug_parent=default_bug_parent)

        if isinstance(result, Question):
            if prior and ' '.join(result.text.split()) == prior:
                continue  # asked, and the card still reads the same: the groom puts it to the adjudicator
            new_text = body.rstrip('\n') + f"\n\n## Question\n{result.text}\n"
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_text)
            if asked is not None:
                asked.append(name)
            continue

        type_, rule, parent = result.type, result.rule, result.parent
        new_id = mint_id(root, canonical, type_)
        typed = {'title': card.title, 'parent': parent, 'decided': False}
        if card.headers.get('moved_from'):
            typed['moved_from'] = card.headers['moved_from']   # F-0120 D14: the dedupe ref survives
        if type_ == 'bug':
            typed['severity'] = severity_of(card)
            typed['found_in'] = 'dev'
            typed['signature'] = card.headers.get('signature')
        elif type_ == 'task':
            typed['writes'] = header_list(card.headers.get('writes'))
            typed['stories'] = header_list(card.headers.get('stories'))
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


# ---- retiring: a note or a card that landed by hand (`asf retire`) -------------------------

#: The first words of what ``asf retire`` writes — a card's ``removed:`` and a retired note's
#: ``done/`` header — so intake knows the item was retired and never mints it again.
RETIRED = 'retired'
_RETIRED_NOTE = '→ ' + RETIRED


def move_to_done(inbox_dir, name, header, text, free=True):
    """Move the note ``<inbox_dir>/<name>`` to ``<inbox_dir>/done/`` as ``header``, a blank line,
    then its ``text`` — the one way a note leaves intake unminted (a ``close`` answer, ``asf
    retire``, a note retired once already). ``free``: a name ``done/`` already holds gets a
    ``-2``, ``-3`` … suffix rather than overwriting what is there. The path written."""
    done_dir = os.path.join(inbox_dir, 'done')
    os.makedirs(done_dir, exist_ok=True)
    stem, out, n = name[:-3] if name.endswith('.md') else name, name, 2
    while free and os.path.exists(os.path.join(done_dir, out)):
        out = f"{stem}-{n}.md"
        n += 1
    target = os.path.join(done_dir, out)
    with open(target, 'w', encoding='utf-8') as f:
        f.write(f"{header}\n\n{text}")
    os.remove(os.path.join(inbox_dir, name))
    return target


def find_note(root, intake_dir, ref):
    """The file name of the inbox note ``ref`` names — its file name, with or without ``.md``,
    or its path from the record root — or ``None``."""
    d = os.path.join(root, intake_dir or DEFAULT_INTAKE_DIR)
    name = os.path.basename(str(ref or '').strip().rstrip('/'))
    if not name:
        return None
    for cand in (name, name + '.md'):
        if cand.endswith('.md') and os.path.isfile(os.path.join(d, cand)):
            return cand
    return None


def retire_note(root, intake_dir, name, removal, date):
    """``asf retire`` on a note nobody groomed: it moves to ``done/`` with ``removal`` (the
    reason and its landing refs) in its header, so the groom neither mints it nor, filed
    again, mints its twin (:func:`retired_keys`). The path written."""
    d = os.path.join(root, intake_dir or DEFAULT_INTAKE_DIR)
    with open(os.path.join(d, name), encoding='utf-8') as f:
        text = f.read()
    return move_to_done(d, name, f"→ {removal} ({date})", text)


def _fold(title):
    return ' '.join(str(title or '').lower().split())


def retired_keys(canonical, inbox_dir):
    """``{('title' | 'signature', folded value): what retired it}`` over every card ``asf retire``
    removed (its ``removed:`` opens with :data:`RETIRED`) and every note it moved to ``done/``."""
    out = {}

    def add(title, signature, ref):
        if _fold(title):
            out.setdefault(('title', _fold(title)), ref)
        if _fold(signature):
            out.setdefault(('signature', _fold(signature)), ref)

    for iid, rec in sorted((canonical or {}).items()):
        meta = rec.get('meta') or {}
        if str(meta.get('removed') or '').startswith(RETIRED):
            add(meta.get('title'), meta.get('signature'), iid)
    done_dir = os.path.join(inbox_dir, 'done')
    if os.path.isdir(done_dir):
        for name in sorted(os.listdir(done_dir)):
            path = os.path.join(done_dir, name)
            if not name.endswith('.md') or not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as f:
                text = f.read()
            head, _sep, rest = text.partition('\n\n')
            if not head.startswith(_RETIRED_NOTE):
                continue
            note = parse_inbox_file(rest)
            add(note.title, note.headers.get('signature'), f"{os.path.basename(inbox_dir)}/done/{name}")
    return out


def retired_as(card, keys):
    """What retired ``card`` already (:func:`retired_keys`): a card or note with its title or its
    signature, else ``None``."""
    return (keys.get(('signature', _fold(card.headers.get('signature'))))
            if _fold(card.headers.get('signature')) else None) \
        or keys.get(('title', _fold(card.title)))


# ---- the questions intake leaves, put to the groom (F-0085 under approvals.groom: auto) ------

#: A groom line's item token for an inbox card, which has no id yet: ``inbox:<file name>``.
TOKEN_PREFIX = 'inbox:'
QUESTION_HEADING = '## Question'


def _has_question(text):
    return any(l.strip() == QUESTION_HEADING for l in text.split('\n'))


def _split_question(text):
    """``(card text without its question, the question)``. The question is the heading and the
    one paragraph under it; whatever the operator wrote below that — an update, an
    ``## Acceptance`` list — stays in the card."""
    lines = text.split('\n')
    start = next((i for i, l in enumerate(lines) if l.strip() == QUESTION_HEADING), None)
    if start is None:
        return text, ''
    end = start + 1
    while end < len(lines) and not lines[end].strip():
        end += 1
    q_start = end
    while end < len(lines) and lines[end].strip() and not lines[end].startswith('## '):
        end += 1
    question = ' '.join(l.strip() for l in lines[q_start:end])
    head = '\n'.join(lines[:start]).rstrip('\n')
    tail = '\n'.join(lines[end:]).strip('\n')
    return (head + ('\n\n' + tail if tail else '')).rstrip('\n') + '\n', question


def question_lines(root, intake_dir=None):
    """One open groom line per inbox card intake asked a question of, in file-name order:
    ``- [ ] inbox:<name> <title> — <question> → answer: ____``. The groom file carries them so
    the adjudicator answers them (:func:`apply_answer`) — nobody edits a card by hand. A severity
    token (C11) sits between the ``inbox:<name>`` token and the title, empty at S3: a card graded
    S1 here carries the same ``S1`` :func:`stuck_s1_lines` says aloud."""
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
        card = graded(scrub_title(parse_inbox_file(body), root))
        title = ' '.join(card.title.split()) or name
        sev = severity_of(card)
        grade = f'{sev} ' if sev != 'S3' else ''
        out.append(f"- [ ] {TOKEN_PREFIX}{name} {grade}{title} — {question} → answer: ____")
    return out


def stuck_s1_lines(root, intake_dir=None, groom_file=None, product=None):
    """One ``NEEDS OPERATOR`` line per intake card sitting on a ``## Question`` whose severity
    reads ``S1`` (C10), in file-name order; ``[]`` when there is none. Pure over the intake
    directory — no ledger, no marker, said again every tick until the card stops being stuck
    (C7)::

        NEEDS OPERATOR: S1 intake card inbox/p1-e2e-on-main.md is stuck on an intake question —
        "This reads as a defect. A Bug carries a signature — …" — answer its
        `inbox:p1-e2e-on-main.md` line in groom/2026-09-29.md (the next tick applies it)

    The file is named record-relative, never by machine path (P13). With no groom file yet the
    remedy is ``asf groom --product <name>``, which writes one. The question is folded to one
    line and capped at :data:`QUESTION_MAX`."""
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
        sev = severity_of(graded(parse_inbox_file(body)))
        if sev != 'S1':
            continue
        q = ' '.join(question.split())
        if len(q) > QUESTION_MAX:
            q = q[:QUESTION_MAX - 1] + '…'
        if groom_file and os.path.isfile(os.path.join(root, groom_file)):
            remedy = (f"answer its `{TOKEN_PREFIX}{name}` line in {groom_file} "
                      "(the next tick applies it)")
        else:
            remedy = f"asf groom --product {getattr(product, 'name', None) or '<name>'}"
        rel = os.path.join(intake_dir or DEFAULT_INTAKE_DIR, name)
        out.append(f'NEEDS OPERATOR: {sev} intake card {rel} is stuck on an intake question — '
                   f'"{q}" — {remedy}')
    return out


_CLAUSES = (
    (re.compile(r'^feature$', re.IGNORECASE), lambda m: ('type', 'feature'), 'feature'),
    (re.compile(r'^bug\s+(.+)$', re.IGNORECASE), lambda m: ('signature', m.group(1).strip()), 'bug <signature>'),
    (re.compile(r'^parent\s+([A-Z]-\d{4})$', re.IGNORECASE), lambda m: ('parent', m.group(1).upper()), 'parent <id>'),
    (re.compile(r'^(S[123])$', re.IGNORECASE), lambda m: ('severity', m.group(1).upper()), 'S1|S2|S3'),
)
_CLOSE_RE = re.compile(r'^(no|close)$', re.IGNORECASE)

#: The whole grammar, in the words the refusal reason quotes back — `close` first (it is
#: `_CLOSE_RE`, matched on the whole answer, before the `;`-split below), then `_CLAUSES`' own
#: forms, read off `_CLAUSES` rather than re-typed so a form added there is a form named here.
_CLAUSE_FORMS = ('close',) + tuple(form for _rx, _make, form in _CLAUSES)


def parse_answer(answer):
    """An inbox answer: ``('close', None)``, ``({header: value}, None)`` from ``;``-separated
    clauses — ``feature``, ``bug <signature>``, ``parent <id>``, ``S1|S2|S3`` — or
    ``(None, reason)`` when a clause is outside that grammar (the line then changes nothing).
    ``reason`` names the first clause that failed and the grammar it did not match; a clause
    carrying an em dash (or its ASCII stand-in, ``' - '``) gets the words that make the mistake
    fixable — a trailing ``— why`` is the one shape this grammar has always dropped."""
    a = answer.strip()
    if _CLOSE_RE.match(a):
        return 'close', None
    headers = {}
    for clause in (c.strip() for c in a.split(';')):
        for rx, make, _form in _CLAUSES:
            m = rx.match(clause)
            if m:
                k, v = make(m)
                headers[k] = v
                break
        else:
            why = ': an answer carries no why' if '—' in clause or ' - ' in clause else ''
            reason = (f'"{clause}" is not a clause{why}. Clauses are '
                      f'{" | ".join(_CLAUSE_FORMS)}, separated by `;`.')
            return None, reason
    return (headers or None), None


def apply_answer(root, name, answer, date, who, intake_dir=None):
    """Apply one answer to ``<intake_dir>/<name>``: its ``## Question`` block goes, the answer's
    header lines go in under the title (replacing one of the same key), and the next intake
    reads the card again — a card or, still unsettled, a fresh question. ``close`` moves it to
    ``done/`` unminted. Returns ``(applied, reason)`` — the pair is the contract, so a caller
    cannot read the tuple's truthiness by accident: ``applied`` is the bool this returned before,
    and ``reason`` is ``None`` when it applied or the message :func:`parse_answer` gave when it
    did not."""
    d = os.path.join(root, intake_dir or DEFAULT_INTAKE_DIR)
    path = os.path.join(d, name)
    parsed, reason = parse_answer(answer)
    if parsed is None or os.path.basename(name) != name or not os.path.isfile(path):
        return False, reason
    with open(path, encoding='utf-8') as f:
        text = f.read()
    body, _question = _split_question(text)
    if parsed == 'close':
        move_to_done(d, name, f"→ closed (groom {date}, {who})", text, free=False)
        return True, None
    lines = body.split('\n')
    idx = next((i for i, l in enumerate(lines) if l.strip()), 0)
    head = idx if _is_header_line(lines[idx].strip() if idx < len(lines) else '') else idx + 1
    keep = [l for l in lines[head:]
            if not ((m := INBOX_KV_RE.match(l.strip())) and m.group(1).lower() in parsed)]
    new = lines[:head] + [f'{k}: {v}' for k, v in parsed.items()] + keep
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(new))
    return True, None
