"""asf.rehearsal — the release rehearsal: a record-shaped snapshot, built by one command, and
(``asf rehearse``) a runner that drives the CLI and the hooks over it end to end before a
version is offered.

This module's builder half — :func:`filler`, :func:`build`, :func:`manifest_holds` — is what
``asf rehearse --build`` runs. It reads a real record and a real code repo and writes
``tests/rehearsal/``: every id kept verbatim at its real width, every structural frontmatter
field and date kept, every prose run (a title, a body, an intake note, a tag's annotation, a
merge commit's subject) cleared to deterministic ASCII filler of the same length and line
count. A ``manifest.json`` states what it wrote; :func:`manifest_holds` reads a snapshot back
against those claims.

It hardcodes no record path, no branch prefix and no item id: the record's own layout comes
from :mod:`asf.record.core` (the one place ``ID_DIGITS`` and the item folders live) and from the
product's own :class:`asf.conventions.Conventions` (``intake_dir``, ``branch_prefixes``), never
a literal spelled here (P19). The runner (``lay``, ``rehearse``, the acts) is a later Task's.
"""
import hashlib
import json
import os
import random
import re
import shutil

from asf import conventions as conventions_mod
from asf import gitops
from asf.record import core, frontmatter
from asf.record.writer import write_card, write_text

#: a fixed, meaningless ASCII vocabulary — the filler never carries anything of the real record
#: it replaces (C4). Lowercase only, no digits, no punctuation but the spaces between words.
_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt "
    "ut labore et dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco "
    "laboris nisi aliquip ex ea commodo consequat duis aute irure in reprehenderit voluptate "
    "velit esse cillum fugiat nulla pariatur excepteur sint occaecat cupidatat non proident "
    "sunt in culpa qui officia deserunt mollit anim id est laborum"
).split()

_TAG_RE = re.compile(r'^v\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?$')


def _filler_seed(text):
    """A hash of ``text``'s length, not of its content — the filler is reproducible from a
    cleared run's length alone (S-79604's fifth acceptance line)."""
    return int(hashlib.sha256(str(len(text)).encode('ascii')).hexdigest()[:16], 16)


def filler(n, seed):
    """``n`` ASCII lowercase-word characters, deterministic from ``seed``: words drawn from a
    fixed vocabulary, space-separated, cut to exactly ``n`` characters. ``n <= 0`` -> ``''``."""
    if n <= 0:
        return ''
    rng = random.Random(seed)
    parts = []
    length = 0
    while length < n:
        word = rng.choice(_WORDS)
        if parts:
            parts.append(' ')
            length += 1
        parts.append(word)
        length += len(word)
    return ''.join(parts)[:n]


def clear_line(line):
    """``line`` cleared to filler of its own length, seeded by that length alone."""
    return filler(len(line), _filler_seed(line))


def clear_block(text):
    """``text`` cleared line by line: the same line count, each line the same length, none of
    its content."""
    return '\n'.join(clear_line(line) for line in text.split('\n'))


#: A ``## History`` entry's opening: ``- <date>`` or ``- <date> <HH:MM[:SS][Z]>``, the optional
#: colon after it, and the entry's own leading verb. The record writes both shapes — the
#: template's ``- <date>: created`` (``asf/record/new.py:212``, ``asf/record/ids.py:198``) and the
#: timestamped ``- <date> <HH:MM> <verb>: …`` that :func:`asf.record.reopen` (``- 2026-01-01
#: 09:04 reopen: …``), ``asf set`` and groom write — so a line of either shape keeps its date and
#: its verb and loses the prose after them. The ``:?`` is what the review's own pattern left out:
#: without it every ``created`` line in the record takes the fallback below and the date goes
#: with the prose.
_HISTORY_LINE_RE = re.compile(
    r'^(-\s\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?Z?)?:?\s+)(\S+)(.*)$')


def _clear_gap(gap):
    """``gap`` cleared to filler, keeping the run of non-word characters at each edge verbatim
    so an id token kept on either side of it keeps its ``\\b`` word boundary intact."""
    lead = re.match(r'\W*', gap).group(0)
    remainder = gap[len(lead):]
    trail = re.search(r'\W*$', remainder).group(0) if remainder else ''
    mid = remainder[:len(remainder) - len(trail)] if trail else remainder
    return lead + (clear_line(mid) if mid else '') + trail


def clear_ids_kept(line):
    """One line cleared to filler of its own length, every id token inside it
    (:data:`asf.record.core.ID_TOKEN_RE`) kept verbatim, and — via :func:`_clear_gap` — the run
    of non-word characters at each gap's edges kept too, so the ``→`` an intake note's ``done/``
    header opens with survives while the free prose around it does not."""
    out = []
    last = 0
    for idm in core.ID_TOKEN_RE.finditer(line):
        gap = line[last:idm.start()]
        if gap:
            out.append(_clear_gap(gap))
        out.append(idm.group(0))
        last = idm.end()
    tail = line[last:]
    if tail:
        out.append(_clear_gap(tail))
    return ''.join(out)


def clear_block_ids_kept(text):
    """``text`` cleared line by line with :func:`clear_ids_kept`: the same line count, each line
    the same length, every id token kept."""
    return '\n'.join(clear_ids_kept(line) for line in text.split('\n'))


def clear_history_line(line):
    """One ``## History`` line cleared: its date (with the time it may carry) and leading verb
    token kept verbatim, every id token inside it (:data:`asf.record.core.ID_TOKEN_RE`) kept
    verbatim, everything else filler of the same length (the design's "a History section ...
    keeping its real date, its real leading verb ... and its real id references — and filler for
    everything after them"). A line :data:`_HISTORY_LINE_RE` cannot read is cleared
    id-preservingly too, never copied."""
    m = _HISTORY_LINE_RE.match(line)
    if not m:
        # not an entry opening this clearer can read — still a History line, so it is cleared
        # id-preservingly rather than wholly: an `` — see T-00001`` reference inside a shape the
        # regex above does not cover is structure the snapshot owes its readers (C4), and the
        # prose around it goes either way.
        return clear_ids_kept(line)
    prefix, verb, rest = m.groups()
    return prefix + verb + clear_ids_kept(rest)


#: The frontmatter keys whose value is *structure*, not prose: an id, a type, a state, a stage,
#: a date, a rank, a priority, a severity, a lane, a size, a footprint, an edge. These are kept
#: verbatim and **everything else** a card's frontmatter carries is prose until proven otherwise
#: and is cleared (C4) — so a Task's ``reshape:``, a Decision's ``decided_by:`` and a Rule's
#: ``reason:``/``check:`` can never reach the snapshot as written. An allowlist, not a denylist:
#: a typed field this product grows later is cleared by default rather than copied by default.
_STRUCTURAL_KEYS = frozenset((
    'id', 'type', 'state', 'stage', 'stage_since', 'updated', 'parent', 'schema_version',
    'dates', 'rank', 'priority', 'severity', 'lane', 'size', 'writes', 'after', 'delivers',
    'blockedBy', 'supersedes', 'superseded_by', 'legacy_id',
))

#: The keys cleared with :func:`clear_ids_kept` rather than :func:`clear_line`. ``removed``'s
#: value is ``true``/``false`` *or* a hand-written reason (:func:`asf.record.setfield._set_only`),
#: and :mod:`asf.record.ingest` reads a task id back out of that reason, so the prose goes and
#: every id token in it stays. A boolean is not a string and is left alone.
_IDS_KEPT_KEYS = frozenset(('removed',))

#: An ISO date or timestamp, whatever key it sits under (a Decision's ``date:``, a ``decided:``,
#: a timestamp a machine field carries): a value the design keeps — "every structural frontmatter
#: field and value, every date" — and one that carries no prose to clear.
_ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?)?$')


def _clear_meta_value(value, ids_kept=False):
    """``value`` with every string inside it cleared: a scalar, or the values of a list or of
    the one level of nesting the frontmatter subset allows. A bool, an int, a float and ``None``
    are structure and come back as they are, and so does an ISO date (:data:`_ISO_RE`)."""
    if isinstance(value, str):
        if _ISO_RE.match(value):
            return value
        return clear_ids_kept(value) if ids_kept else clear_line(value)
    if isinstance(value, list):
        return [_clear_meta_value(v, ids_kept) for v in value]
    if isinstance(value, dict):
        return {k: _clear_meta_value(v, ids_kept) for k, v in value.items()}
    return value


#: The ``## `` headings the record's own card template writes
#: (:func:`asf.record.new.cmd_new`, ``asf/record/new.py:207-214``) — the only ones kept verbatim.
#: Every other heading *line* is free prose, whoever typed it: a real record carries hand-written
#: section headings that name a date, a pull request number and a product by name, and a heading
#: copied through because it opens with ``## `` is a leak like any other (C4). The heading's own
#: shape survives — ``'## '`` and the line's length — and nothing it said does.
_TEMPLATE_HEADINGS = frozenset((
    '## Description', '## Acceptance', '## Non-goals', '## History', '## Children',
    '## Backlinks',
))


def _clear_heading(heading):
    """``heading`` kept verbatim when it is one of the record template's own
    (:data:`_TEMPLATE_HEADINGS`), else ``'## '`` and filler of the rest's own length, every id
    token in it kept."""
    if heading.strip() in _TEMPLATE_HEADINGS:
        return heading
    return '## ' + clear_ids_kept(heading[len('## '):])


def _clear_card_body(body):
    preamble, sections = core.parse_sections(body)
    if preamble.strip():
        preamble = clear_block(preamble)
    new_sections = []
    for heading, content in sections:
        cleared_heading = _clear_heading(heading)
        if heading.strip() == '## History':
            lines = content.split('\n')
            # a line that does not open an entry is a continuation of the one above it — free
            # prose too, so it is cleared id-preservingly rather than copied. A blank line
            # clears to a blank line, so the section's own shape survives.
            content = '\n'.join(
                clear_history_line(l) if l.lstrip().startswith('- ') else clear_ids_kept(l)
                for l in lines)
        else:
            content = clear_block(content)
        new_sections.append([cleared_heading, content])
    return core.render_sections(preamble, new_sections)


def _clear_card(meta, body):
    """``(meta, body)`` cleared: every frontmatter value but a structural one
    (:data:`_STRUCTURAL_KEYS`) replaced by filler of its own length, and the body cleared
    section by section."""
    cleared = frontmatter.clone(meta)
    for key, value in list(cleared.items()):
        if key in _STRUCTURAL_KEYS:
            continue
        cleared[key] = _clear_meta_value(value, ids_kept=key in _IDS_KEPT_KEYS)
    return cleared, _clear_card_body(body)


_ID_RE = re.compile(r'^([A-Za-z]+)-(\d+)$')


def _digit_run(iid):
    m = _ID_RE.match(iid or '')
    return len(m.group(2)) if m else 0


def _widest_id(ids):
    """``{prefix: widest-id}`` — the id with the longest digit run per prefix; ties keep the
    first one found (``load_items``' own, deterministic sort order)."""
    widest = {}
    for iid in ids:
        m = _ID_RE.match(iid or '')
        if not m:
            continue
        prefix = m.group(1)
        cur = widest.get(prefix)
        if cur is None or _digit_run(iid) > _digit_run(cur):
            widest[prefix] = iid
    return widest


def _conventions_for(product):
    if product is None:
        return conventions_mod.Conventions()
    if isinstance(product, str):
        from asf import env
        return env.load_product(product).conventions
    return product.conventions  # an asf.env.Product


def _asf_version():
    import asf
    return asf.__version__


def _repo_sha(repo_dir):
    r = gitops.git(['rev-parse', 'HEAD'], repo_dir)
    return r.data if r.ok else ''


def _repo_tags(repo_dir):
    """``[(name, annotated, message, date)]`` for every ``v<x.y.z>`` (optionally ``-<suffix>``)
    tag in ``repo_dir``, oldest first; ``()`` when ``repo_dir`` is not a git checkout or carries
    none. ``date`` is the tag's own ``%(creatordate:short)`` — not :func:`asf.record.core.today`
    — so a rebuild of an unchanged source sha writes the same bytes on any later day."""
    names = gitops.git(['tag', '-l', 'v*'], repo_dir)
    if not names.ok:
        return []
    out = []
    for name in sorted(n for n in names.data.split() if _TAG_RE.match(n)):
        kind = gitops.git(['cat-file', '-t', f'refs/tags/{name}'], repo_dir)
        annotated = kind.ok and kind.data == 'tag'
        message = ''
        if annotated:
            msg = gitops.git(['for-each-ref', '--format=%(contents)', f'refs/tags/{name}'],
                              repo_dir)
            message = msg.data.rstrip('\n') if msg.ok else ''
        created = gitops.git(['for-each-ref', '--format=%(creatordate:short)',
                               f'refs/tags/{name}'], repo_dir)
        date = created.data.strip() if created.ok else ''
        out.append((name, annotated, message, date))
    return out


def _build_repo_seed(repo_dir, out_repo, widest):
    """Write ``out_repo``'s seed: a ``CHANGELOG.md`` of filler entries, one per real tag, and
    ``refs.json`` — the same ref plan, kept as an independent copy so :func:`manifest_holds` can
    tell a hand-edited ``manifest.json`` claim from the snapshot's own data (C8). Returns
    ``(refs, branches, merges)`` for the manifest."""
    tags = _repo_tags(repo_dir)
    refs = []
    for name, annotated, message, date in tags:
        cleared = clear_block(message) if message else ''
        refs.append({
            'name': name,
            'annotated': annotated,
            'prerelease': '-' in name[1:],
            'message': cleared,
            'date': date,
        })
    if not any(r['annotated'] for r in refs):
        # no real annotated tag reachable (a shallow or tag-less checkout): one synthetic entry
        # so the snapshot still meets its own In — the manifest records what it actually built.
        # Its date is ``''``, what :func:`_repo_tags` itself returns for an unreadable
        # creatordate, and never :func:`asf.record.core.today`: a tag-less source has to build
        # the same bytes on any later day.
        refs.append({'name': 'v0.0.1', 'annotated': True, 'prerelease': False,
                     'message': clear_line('x' * 24), 'date': ''})

    changelog = []
    for ref in refs:
        changelog.append(f"## {ref['name']} — {ref['date']}\n\n{ref['message'] or clear_line('x' * 16)}\n")
    write_text(os.path.join(out_repo, 'CHANGELOG.md'), '\n'.join(changelog) + '\n')
    with open(os.path.join(out_repo, 'refs.json'), 'w', encoding='utf-8') as f:
        json.dump(refs, f, indent=2, sort_keys=True)
        f.write('\n')

    conv_prefixes = conventions_mod.DEFAULT_BRANCH_PREFIXES
    sample_id = widest.get('T') or next(iter(widest.values()), None)
    branches = []
    if sample_id:
        for kind, prefix in conv_prefixes.items():
            if kind in ('legacy', 'direct'):
                continue
            branches.append(prefix.rstrip('/') + '/' + sample_id)

    merges = []
    for i, iid in enumerate(sorted(widest.values()), start=1):
        merges.append(f"{iid} — {clear_line('x' * 32)} (#{1200 + i})")

    return refs, branches, merges


def _intake_mismatch_text(note_text):
    """The mismatch ``done/`` copy's text: a strict, non-empty prefix of ``note_text`` — a
    *truncation* of the note's own cleared text, so the note can never be a substring of the copy
    it was moved to. :func:`asf.record.staged_guard.check`'s plain containment arm (``text in t``)
    is the one that covers an ordinary move; this shape defeats it on purpose, so the guard's one
    escape, :func:`asf.record.staged_guard._paired_done`, is what has to cover the move — which
    is why the copy is written under the note's own stem and not a name of its own (P5)."""
    body = note_text.strip()
    return body[:max(1, len(body) * 2 // 3)] + '\n'


def _build_intake(record_dir, intake_dir_name, out_record):
    """Carry intake in its three states (S-79606's shape): notes still under ``<intake>/``,
    notes moved to ``<intake>/done/`` with their ``→ <id>`` header cleared id-preservingly and
    their text cleared, and — the third state — one ``done/`` copy of an open note, written under
    *that note's own stem* (``<intake>/done/<note>``) and carrying a truncation of the note's
    cleared text (:func:`_intake_mismatch_text`). The copy is therefore not a byte-for-byte
    superstring of the note it pairs with, which is the shape the staged guard's one escape turns
    on (P5), and the manifest records the pair itself — ``mismatch = {note, done}`` — not a text
    of its own. Returns the manifest's ``intake`` claim.

    Every note is written under a **generated** name — ``open-note-<n>.md`` /
    ``done-note-<n>.md``, numbered in the source's own sort order — never the source's own file
    name: :func:`asf.groom.inbox.file_card` and :func:`asf.groom.inbox.process_inbox` both
    derive that name from the note's own *title* (a slug of it), so the file name is prose like
    any other and may not reach the snapshot. The ``intake`` claim records the generated names, which is what the snapshot
    carries and what :func:`manifest_holds` reads back."""
    src_intake = os.path.join(record_dir, intake_dir_name)
    out_intake = os.path.join(out_record, intake_dir_name)
    out_done = os.path.join(out_intake, 'done')
    os.makedirs(out_done, exist_ok=True)

    open_names = []
    if os.path.isdir(src_intake):
        for name in sorted(os.listdir(src_intake)):
            if name == 'done' or not name.endswith('.md'):
                continue
            with open(os.path.join(src_intake, name), encoding='utf-8') as f:
                text = f.read()
            out_name = f"open-note-{len(open_names) + 1}.md"
            write_text(os.path.join(out_intake, out_name), clear_block(text))
            open_names.append(out_name)

    done_names = []
    src_done = os.path.join(src_intake, 'done')
    if os.path.isdir(src_done):
        for name in sorted(os.listdir(src_done)):
            if not name.endswith('.md'):
                continue
            with open(os.path.join(src_done, name), encoding='utf-8') as f:
                full = f.read()
            header, _, text = full.partition('\n\n')
            # the header is not a bare ``→ <id>``: :func:`asf.groom.inbox.move_to_done` writes
            # free prose into it (``→ closed (groom <date>, <who>)``, ``→ <removal> (<date>)``),
            # so it is cleared id-preservingly too — the ``→`` and every id token survive, the
            # account name, the date's prose and any path in it do not.
            out_name = f"done-note-{len(done_names) + 1}.md"
            write_text(os.path.join(out_done, out_name),
                       f"{clear_block_ids_kept(header)}\n\n{clear_block(text)}")
            done_names.append(out_name)

    if not open_names:
        write_text(os.path.join(out_intake, 'open-note-1.md'),
                   clear_block('a filed note\nwith two lines'))
        open_names.append('open-note-1.md')
    while len(done_names) < 2:
        # the snapshot owes its own shape two done notes whatever the source carried: the
        # mismatch claim below takes the last of them, and one note alone is not a state.
        n = len(done_names) + 1
        out_name = f"done-note-{n}.md"
        write_text(os.path.join(out_done, out_name),
                   f"→ T-00000\n\n{clear_block('a groomed note' * n)}")
        done_names.append(out_name)

    # the third state: a ``done/`` copy of the first open note, under that note's own stem, so
    # :func:`asf.record.staged_guard._paired_done` pairs the two by name — and a truncation of
    # the note's text, so nothing pairs them by containment.
    note_name = open_names[0]
    with open(os.path.join(out_intake, note_name), encoding='utf-8') as f:
        note_text = f.read()
    mismatch_done = note_name
    write_text(os.path.join(out_done, mismatch_done), _intake_mismatch_text(note_text))
    if mismatch_done not in done_names:
        done_names.append(mismatch_done)
    return {'open': open_names, 'done': done_names,
            'mismatch': {'note': note_name, 'done': mismatch_done}}


def build(record_dir, repo_dir, out, product=None):
    """Write a snapshot of ``record_dir``/``repo_dir`` to ``out``: shapes kept, prose cleared.
    Returns the manifest it wrote. Reads a real record; never writes to one."""
    conv = _conventions_for(product)
    by_id, _errors = core.load_items(record_dir)

    out_record = os.path.join(out, 'record')
    out_repo = os.path.join(out, 'repo')
    for d in (out_record, out_repo):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)

    cards = {folder: 0 for folder in core.ITEM_FOLDERS}
    for iid, recs in sorted(by_id.items()):
        rec = recs[0]
        cards[rec['folder']] = cards.get(rec['folder'], 0) + 1
        meta, body = _clear_card(rec['meta'], rec['body'])
        text = frontmatter.render(meta, body)
        write_card(os.path.join(out_record, rec['folder'], rec['name']), text)

    widest = _widest_id(by_id.keys())
    intake = _build_intake(record_dir, conv.intake_dir, out_record)
    refs, branches, merges = _build_repo_seed(repo_dir, out_repo, widest)

    manifest = {
        'cards': cards,
        'widest_id': widest,
        'refs': refs,
        'refs_total': len(refs),
        'refs_annotated': sum(1 for r in refs if r['annotated']),
        'branches': branches,
        'merges': merges,
        'intake': intake,
        'intake_dir': conv.intake_dir,
        'built_at': core.now_iso(),
        'asf_version': _asf_version(),
        'source_sha': _repo_sha(repo_dir),
    }
    with open(os.path.join(out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write('\n')
    _write_readme(out)
    return manifest


_README = """# tests/rehearsal/

What this is: a record-shaped snapshot of a real ASF product, built by `asf rehearse --build`
(`asf/rehearsal.py:build`). Every id is kept verbatim at its real width; every structural
frontmatter field, date and History line's date/verb/id reference is kept; every other
frontmatter value, every title, body, History continuation line, intake note and tag annotation
is cleared to deterministic ASCII filler of the same length and line count. Each intake note is
written under a name this builder generates (`open-note-<n>.md`, `done-note-<n>.md`), because a
note's own file name is a slug of its title. No title, body, History prose, intake note text,
file name or commit message from the real record is in it.

What it must preserve: `manifest.json`'s claims — card count per type, widest id per prefix, the
tag/ref plan, the intake states. `rehearsal.manifest_holds` checks the snapshot against them;
a refresh that changes a claim it does not also meet is a builder defect, not a passing rebuild.

How to refresh it: `asf rehearse --build --from-product <p>`, then re-run this product's own
unit tests and its generic/privacy scans before committing the diff. Until an operator has run
that against a real product, this tree stands on the synthetic stand-in source
`tests/test_rehearsal_snapshot.build_fixture_source` writes, and is refreshed by building from
it: see that function's docstring for the two lines that do it.

What never goes in it: any real title, body, History prose, intake note text, commit message,
account name or machine path. The filler is seeded only by the length of what it replaces.
"""


def _write_readme(out):
    write_text(os.path.join(out, 'README.md'), _README)


def _cards_counts(record_dir):
    counts = {}
    for folder in core.ITEM_FOLDERS:
        d = os.path.join(record_dir, folder)
        counts[folder] = len([n for n in os.listdir(d) if n.endswith('.md')]) \
            if os.path.isdir(d) else 0
    return counts


def manifest_holds(snapshot):
    """``[]`` when ``snapshot`` (a directory: ``manifest.json``, ``record/``, ``repo/``) still
    meets its own manifest's claims — card count per type, widest id per prefix, the ref plan,
    the intake states — else one line per claim it no longer meets (C8)."""
    problems = []
    manifest_path = os.path.join(snapshot, 'manifest.json')
    try:
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [f"manifest.json: {e}"]

    record_dir = os.path.join(snapshot, 'record')
    by_id, _errors = core.load_items(record_dir)

    actual_cards = _cards_counts(record_dir)
    for folder, claimed in (manifest.get('cards') or {}).items():
        actual = actual_cards.get(folder, 0)
        if actual != claimed:
            problems.append(f"cards[{folder}]: manifest claims {claimed}, snapshot has {actual}")

    actual_widest = _widest_id(by_id.keys())
    for prefix, claimed_id in (manifest.get('widest_id') or {}).items():
        actual_id = actual_widest.get(prefix)
        if actual_id != claimed_id:
            problems.append(f"widest_id[{prefix}]: manifest claims {claimed_id!r}, "
                             f"snapshot's widest is {actual_id!r}")

    # PD10: the five-digit claim is proved on the digit run, not len(str(v)) — a four-digit id's
    # string ("T-0001") is already 6 characters long, so that fence alone would never fire.
    runs = [_digit_run(v) for v in (manifest.get('widest_id') or {}).values()]
    if not runs or max(runs) < 5:
        problems.append("widest_id: no prefix claims a five-digit digit run")

    claimed_refs = manifest.get('refs') or []
    refs_path = os.path.join(snapshot, 'repo', 'refs.json')
    try:
        with open(refs_path, encoding='utf-8') as f:
            independent_refs = json.load(f)
    except (OSError, json.JSONDecodeError):
        independent_refs = None
    if independent_refs != claimed_refs:
        problems.append("refs: manifest.json and repo/refs.json disagree")
    elif not any(r.get('annotated') for r in claimed_refs):
        problems.append("refs: no annotated tag claimed")

    # the ref plan's own scalar claims (PD: plan Step 2's "tag count, annotated count") — kept
    # separate from the `refs` list itself so flipping one `annotated` flag in both manifest.json
    # and repo/refs.json (leaving the two lists equal to each other) still shows as a mismatch
    # against the counts recorded at build time.
    actual_refs = independent_refs if independent_refs is not None else claimed_refs
    actual_total = len(actual_refs)
    actual_annotated = sum(1 for r in actual_refs if r.get('annotated'))
    claimed_total = manifest.get('refs_total')
    claimed_annotated = manifest.get('refs_annotated')
    if claimed_total != actual_total:
        problems.append(f"refs_total: manifest claims {claimed_total}, snapshot has {actual_total}")
    if claimed_annotated != actual_annotated:
        problems.append(f"refs_annotated: manifest claims {claimed_annotated}, "
                         f"snapshot has {actual_annotated}")

    claimed_intake = manifest.get('intake') or {}
    intake_dir_name = manifest.get('intake_dir') or conventions_mod.DEFAULT_INTAKE_DIR
    intake_dir = os.path.join(record_dir, intake_dir_name)
    actual_open = sorted(n for n in os.listdir(intake_dir) if n.endswith('.md')) \
        if os.path.isdir(intake_dir) else []
    if actual_open != sorted(claimed_intake.get('open') or []):
        problems.append(f"intake.open: manifest claims {claimed_intake.get('open')}, "
                         f"snapshot has {actual_open}")
    done_dir = os.path.join(intake_dir, 'done')
    actual_done = sorted(n for n in os.listdir(done_dir) if n.endswith('.md')) \
        if os.path.isdir(done_dir) else []
    if actual_done != sorted(claimed_intake.get('done') or []):
        problems.append(f"intake.done: manifest claims {claimed_intake.get('done')}, "
                         f"snapshot has {actual_done}")
    # the third intake state's claim, checked against the note's *real* text rather than a copy
    # of it the manifest carries: the ``done/`` copy pairs with the note by name and must not be
    # a superstring of it, which is what leaves the staged guard's one escape as the only thing
    # that can cover the move (P5).
    mismatch = claimed_intake.get('mismatch')
    if mismatch:
        note_path = os.path.join(intake_dir, mismatch.get('note') or '')
        done_path = os.path.join(done_dir, mismatch.get('done') or '')
        try:
            with open(note_path, encoding='utf-8') as f:
                note_text = f.read()
            with open(done_path, encoding='utf-8') as f:
                done_text = f.read()
        except OSError as e:
            problems.append(f"intake.mismatch: {mismatch.get('note')} / "
                             f"{mismatch.get('done')}: {e.strerror}")
        else:
            if not note_text.strip():
                problems.append(f"intake.mismatch: {mismatch.get('note')} is empty, so its "
                                 "done copy cannot fail to contain it")
            elif note_text.strip() in done_text:
                problems.append(f"intake.mismatch: {mismatch.get('done')}'s done copy now "
                                 f"contains {mismatch.get('note')} whole, which its claim says "
                                 "it does not")

    return problems


def cmd_rehearse(args, root):
    """``asf rehearse``. This Task carries only ``--build``: the runner and its acts land in a
    later Task, so no flag but ``--build``/``--from-product``/``--snapshot`` is read here."""
    snapshot = args.snapshot or os.path.join(root, 'tests', 'rehearsal')
    if not args.build:
        print("asf rehearse: no acts yet (the runner lands in a later Task) — use --build")
        return 2
    if not args.from_product:
        print("asf rehearse --build: --from-product <p> is required")
        return 2
    from asf import env
    product = env.load_product(args.from_product)
    manifest = build(product.backlog_dir, product.repo_dir, snapshot, product=product)
    print(f"rehearse --build: wrote {snapshot} — "
          f"{sum(manifest['cards'].values())} cards, {len(manifest['refs'])} refs")
    return 0


def register(sub):
    """Register ``rehearse`` on the top-level parser (beside ``release-readiness``)."""
    p = sub.add_parser('rehearse', help='run the CLI and the hooks end to end on a '
                                         'record-shaped snapshot before a version is offered')
    p.add_argument('--snapshot', default=None, help='default: tests/rehearsal under the repo root')
    p.add_argument('--build', action='store_true', help="the operator's refresh: read a real "
                    "product's record and repo and (re)write the snapshot and its manifest")
    p.add_argument('--from-product', default=None, metavar='P', help='the product --build reads')
    return p
