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


_HISTORY_LINE_RE = re.compile(r'^(-\s\d{4}-\d{2}-\d{2}:\s*)(\S+)(.*)$')


def clear_history_line(line):
    """One ``## History`` line cleared: its date and leading verb token kept verbatim, every id
    token inside it (:data:`asf.record.core.ID_TOKEN_RE`) kept verbatim, everything else filler
    of the same length (the design's "a History section ... keeping its real date, its real
    leading verb ... and its real id references — and filler for everything after them")."""
    m = _HISTORY_LINE_RE.match(line)
    if not m:
        return clear_line(line)
    prefix, verb, rest = m.groups()
    out = []
    last = 0
    for idm in core.ID_TOKEN_RE.finditer(rest):
        gap = rest[last:idm.start()]
        if gap:
            out.append(clear_line(gap))
        out.append(idm.group(0))
        last = idm.end()
    tail = rest[last:]
    if tail:
        out.append(clear_line(tail))
    return prefix + verb + ''.join(out)


#: frontmatter scalar keys cleared to filler — the only prose a card's frontmatter ever carries.
#: Everything else (id, type, state, stage, parent, dates, ``writes``, ``delivers`` ...) is
#: structural and kept verbatim.
_PROSE_KEYS = ('title',)


def _clear_card_body(body):
    preamble, sections = core.parse_sections(body)
    if preamble.strip():
        preamble = clear_block(preamble)
    new_sections = []
    for heading, content in sections:
        if heading.strip() == '## History':
            lines = content.split('\n')
            content = '\n'.join(
                clear_history_line(l) if l.lstrip().startswith('- ') else l for l in lines)
        else:
            content = clear_block(content)
        new_sections.append([heading, content])
    return core.render_sections(preamble, new_sections)


def _clear_card(meta, body):
    cleared = frontmatter.clone(meta)
    for key in _PROSE_KEYS:
        if isinstance(cleared.get(key), str):
            cleared[key] = clear_line(cleared[key])
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
    """``[(name, annotated, message)]`` for every ``v<x.y.z>`` (optionally ``-<suffix>``) tag in
    ``repo_dir``, oldest first; ``()`` when ``repo_dir`` is not a git checkout or carries none."""
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
        out.append((name, annotated, message))
    return out


def _build_repo_seed(repo_dir, out_repo, widest):
    """Write ``out_repo``'s seed: a ``CHANGELOG.md`` of filler entries, one per real tag, and
    ``refs.json`` — the same ref plan, kept as an independent copy so :func:`manifest_holds` can
    tell a hand-edited ``manifest.json`` claim from the snapshot's own data (C8). Returns
    ``(refs, branches, merges)`` for the manifest."""
    tags = _repo_tags(repo_dir)
    refs = []
    for name, annotated, message in tags:
        cleared = clear_block(message) if message else ''
        refs.append({
            'name': name,
            'annotated': annotated,
            'prerelease': '-' in name[1:],
            'message': cleared,
        })
    if not any(r['annotated'] for r in refs):
        # no real annotated tag reachable (a shallow or tag-less checkout): one synthetic entry
        # so the snapshot still meets its own In — the manifest records what it actually built
        refs.append({'name': 'v0.0.1', 'annotated': True,
                     'prerelease': False, 'message': clear_line('x' * 24)})

    changelog = []
    for ref in refs:
        changelog.append(f"## {ref['name']} — {core.today()}\n\n{ref['message'] or clear_line('x' * 16)}\n")
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


def _build_intake(record_dir, intake_dir_name, out_record):
    """Carry intake in its three states (S-79606's shape): notes still under ``<intake>/``,
    notes moved to ``<intake>/done/`` with their ``→ <id>`` header kept and their text
    cleared, and one done note the manifest names whose cleared text is a byte or two shorter
    than the ``original`` it also records — so that recorded ``original`` is never a substring
    of what the snapshot actually carries, the shape the staged guard's one escape turns on
    (P5). Returns the manifest's ``intake`` claim."""
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
            write_text(os.path.join(out_intake, name), clear_block(text))
            open_names.append(name)

    done_names = []
    src_done = os.path.join(src_intake, 'done')
    if os.path.isdir(src_done):
        for name in sorted(os.listdir(src_done)):
            if not name.endswith('.md'):
                continue
            with open(os.path.join(src_done, name), encoding='utf-8') as f:
                full = f.read()
            header, _, text = full.partition('\n\n')
            write_text(os.path.join(out_done, name), f"{header}\n\n{clear_block(text)}")
            done_names.append(name)

    if not open_names:
        write_text(os.path.join(out_intake, 'note-1.md'), clear_block('a filed note\nwith two lines'))
        open_names.append('note-1.md')
    n = 1
    while len(done_names) < 2:
        name = f"done-pad-{n}.md"
        n += 1
        if name in done_names:
            continue
        write_text(os.path.join(out_done, name),
                   f"→ T-00000\n\n{clear_block('a groomed note' * n)}")
        done_names.append(name)
    done_names.sort()

    mismatch_name = done_names[-1]
    path = os.path.join(out_done, mismatch_name)
    with open(path, encoding='utf-8') as f:
        current = f.read()
    _header, _, text = current.partition('\n\n')
    original = text + '\n' + clear_line('x' * 40)   # longer than what the snapshot carries —
    #                                                  never a substring of it, by construction
    return {'open': open_names, 'done': done_names,
            'mismatch': {'name': mismatch_name, 'original': original}}


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
        'branches': branches,
        'merges': merges,
        'intake': intake,
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
frontmatter field, date and History line's date/verb/id reference is kept; every title, body,
intake note and tag annotation is cleared to deterministic ASCII filler of the same length and
line count. No title, body, History prose, intake note text or commit message from the real
record is in it.

What it must preserve: `manifest.json`'s claims — card count per type, widest id per prefix, the
tag/ref plan, the intake states. `rehearsal.manifest_holds` checks the snapshot against them;
a refresh that changes a claim it does not also meet is a builder defect, not a passing rebuild.

How to refresh it: `asf rehearse --build --from-product <p>`, then re-run this product's own
unit tests and its generic/privacy scans before committing the diff.

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

    claimed_intake = manifest.get('intake') or {}
    intake_dir = os.path.join(record_dir, 'inbox')
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
    mismatch = claimed_intake.get('mismatch')
    if mismatch:
        path = os.path.join(done_dir, mismatch.get('name', ''))
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            problems.append(f"intake.mismatch: {mismatch.get('name')} missing")
        else:
            if mismatch.get('original') in text:
                problems.append(f"intake.mismatch: {mismatch.get('name')}'s done copy now "
                                 "contains the text its claim says it lacks")

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
