"""asf.evidence.precedent — the repo's own answer, read before a question reaches a person.

An adjudicate session on a product (`adjudicate-t-42278`, 2026-10-06) reverted a raw credential
read, then parked the Task on the operator over a question the repo had already settled: a
decision recorded in its own ``docs/decisions/``, and the job-queue pattern the code already
followed once, elsewhere. Every other fact an adjudicator needs is generated into its brief —
the card, the documents, the footprint, its own standing rulings (:mod:`asf.evidence.rulings`).
The product's decision register was not, though the factory already indexes it, for ids alone
(:mod:`asf.record.decisions`). So this module reads it whole — titles, statuses, paths — for a
brief to list (:func:`entries`, :func:`brief_section`), and reads a session's own ``precedent:``
REPORT field back (:func:`claim`, :func:`named`), to put the citation on the park's own sentence
and the ruling's own History line (:func:`park_note`, :func:`history_note`) — the one role that
wrote precedent and read none (F-0262).
"""
import os
import re

from asf.record import decisions
from asf.views import index_reader
from asf.workers import report

#: The head of a decision file read for its title and status — the same budget
#: :func:`asf.record.decisions.docs_ids` already reads for the same file.
HEAD_BYTES = 2048
#: The decisions a brief lists (older ones are named by count). Twenty, not ten: a register is
#: the product's whole settled history, where a ruling list is one item's.
BRIEF_MAX = 20
#: A status that makes a decision *not* precedent — it still reads as settled and is not (C5).
#: Printed, never filtered: nothing here drops a row by this word, or a superseded decision
#: would read as silent instead of settled-and-wrong, which is the more dangerous of the two.
STALE_STATUS = ('superseded', 'rejected', 'withdrawn', 'draft', 'proposed')

TITLE_RE = re.compile(r'^title:\s*"?(?P<t>[^"\n]+?)"?\s*$', re.M)
#: An ADR's own ``ADR nnnn — `` numbering is stripped: the row already carries the id (PD7).
HEADING_RE = re.compile(r'^#\s+(?:ADR\s*\d{3,}\s*[—–-]\s*)?(?P<t>\S.*?)\s*$', re.M)
STATUS_RE = re.compile(r'^(?:-\s*\*\*Status:\*\*|status:)\s*(?P<s>[^\s*]+)', re.M | re.I)
#: A ``precedent:`` value that names something: a repo path, a ``D-nnnn``, or an ADR's number.
CITES_RE = re.compile(r'[\w.@-]*[\w@-]/[\w./@-]*\.[A-Za-z0-9]+|\bD-\d{4,}\b|\bADR\s*\d{3,}\b')
#: A ``precedent:`` value that says the search came back empty: ``none — <where you looked>``.
NO_PRECEDENT_RE = re.compile(r'^[\s`*_]*(none|nothing|no precedent|n/a|-|—)(?![\w/])', re.I)


def _file_id(stem, head):
    """One id for a decision file, in :func:`asf.record.decisions.docs_ids`'s own order of
    tests (PD8): the stem itself, else its leading number, else its front-matter ``id:`` — never
    more than one, so a row's id and its printed path always agree."""
    if decisions.ID_RE.fullmatch(stem.upper()):
        return stem.upper()
    m = decisions._NUMBERED_RE.match(stem)
    if m:
        return f'D-{m.group(1)}'
    m = decisions._FRONT_ID_RE.search(head)
    return m.group(1).upper() if m else None


def _file_entries(product):
    root = decisions.repo_dir(product)
    if not root:
        return []
    folder = os.path.join(root, decisions.DOCS_DIR)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith('.md'):
            continue
        try:
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                head = f.read(HEAD_BYTES)
        except OSError:
            continue  # an unreadable file is skipped, never raised
        iid = _file_id(name[:-3], head)
        if not iid:
            continue
        m = TITLE_RE.search(head) or HEADING_RE.search(head)
        title = m.group('t').strip() if m else ''
        if not title:
            continue
        s = STATUS_RE.search(head)
        out.append({'id': iid, 'title': title, 'status': s.group('s') if s else '',
                    'path': os.path.join(decisions.DOCS_DIR, name)})
    return out


def _card_entries(items):
    out = []
    for entry in (items or {}).values():
        if not isinstance(entry, dict) or entry.get('type') != 'decision':
            continue
        if index_reader.retired(entry):
            continue
        out.append({'id': entry.get('id') or '', 'title': entry.get('title') or '',
                    'status': entry.get('state') or '', 'path': ''})
    return out


def entries(product, items=None):
    """Every in-repo decision, as ``[{'id', 'title', 'status', 'path'}]``, newest id first.

    Two sources, one register (:func:`asf.record.decisions.register`'s own pair): the product's
    ``docs/decisions/`` files, read through :data:`asf.record.decisions.DOCS_DIR` under
    :func:`asf.record.decisions.repo_dir`; and the record's own ``decision`` cards out of
    ``items`` (the index the brief already holds), with one retired
    (:func:`asf.views.index_reader.retired`) dropped before it ever reaches here. ``path`` is
    record-relative for a file and ``''`` for a card.

    Pure and total: no git, no network, no clock. An unreadable directory, an unreadable file or
    a file with no id or no title is skipped, never raised — no register is no precedent, never
    an error (:func:`asf.record.decisions.docs_ids`'s own rule). A file and a card sharing an id
    both survive, keyed ``(id, path)`` — no merge, no disambiguation (Out (f))."""
    rows = {}
    for row in _file_entries(product) + _card_entries(items):
        rows[(row['id'], row['path'])] = row

    def _key(row):
        n = re.search(r'\d+', row['id'])
        return (-int(n.group()) if n else 0, row['id'], row['path'])

    return sorted(rows.values(), key=_key)


def brief_section(entries):
    """The ``IN-REPO PRECEDENT`` block for an adjudicate brief — ``''`` when the register is
    empty (a product with no recorded decision gets no section and no instruction it cannot
    follow)."""
    if not entries:
        return ''
    shown = entries[:BRIEF_MAX]
    older = len(entries) - len(shown)
    lines = [
        'IN-REPO PRECEDENT — READ THIS BEFORE YOU PARK ANYTHING ON A PERSON. This product has '
        f'already settled {len(entries)} question(s) in writing. A dispute an entry below '
        'answers is not a question for a person: it is a ruling you can write in one sentence, '
        'citing the entry. Read the entry itself — the title is an index, not the decision.', '']
    for row in shown:
        where = row['path'] or 'the record'
        status = f" ({row['status']})" if row['status'] else ''
        lines.append(f"- {row['id']} — {row['title']}{status} · {where}")
    if older:
        lines.append(
            f'({older} older decision(s) not listed: the rest of {decisions.DOCS_DIR}/.)')
    lines.append('')
    lines.append(
        'The second precedent is the pattern already in the code: the product usually already '
        'does, once, somewhere, the thing under dispute. Before any `NEEDS OPERATOR`, establish '
        "both — the decisions above and the pattern — and say which in the report's "
        '`precedent:` line. A status that is not `accepted` is not precedent: go read what '
        'replaced it.')
    return '\n'.join(lines)


def claim(text):
    """The ``precedent:`` field of the last REPORT block in ``text``, or ``''``."""
    return (report.parse(text).get('precedent') or '').strip()


def named(text):
    """The citation a ``precedent:`` claim names — the paths, ids and ADR numbers in it, joined —
    or ``''`` when it names none (a ``none — …`` answer, or a sentence with no anchor in it)."""
    value = claim(text)
    if not value or NO_PRECEDENT_RE.match(value):
        return ''
    return ', '.join(dict.fromkeys(CITES_RE.findall(value)))


def park_note(text):
    """The clause :func:`asf.workers.lifecycle.blocked_park_text` appends for an adjudicate run,
    or ``''``. Three shapes, and each is a different fact for the person who opens the park:

    * ``precedent: <citation>`` — it found one and still parked (a licence, money, security or
      customer-visible call the precedent did not settle);
    * ``no in-repo precedent: <what it searched>`` — it looked and the repo is silent;
    * ``its report names no precedent — the repo may already answer this`` — no ``precedent:``
      line at all, so nobody has established that the repo was read."""
    cite = named(text)
    if cite:
        return f'precedent: {cite}'
    value = claim(text)
    if value:
        return f'no in-repo precedent: {value}'
    return 'its report names no precedent — the repo may already answer this'


def history_note(text):
    """``" [precedent: <citation>]"`` for a report that names one, else ``''`` — appended to the
    ruling's own ``## History`` line by :func:`asf.tick.step_health.file_rulings` (C13), where
    :func:`asf.evidence.rulings.standing` reads it back into every later brief."""
    cite = named(text)
    return f' [precedent: {cite}]' if cite else ''
