"""asf.record.move — ``asf move``: clear an adopted record's foreign cards in bulk (F-0120 §5).

The first customer install's record holds cards that are not that product's work — another
product's, or ASF's own machinery. The groom's ``belongs_to:`` flag (F-0120 T-0445) names a
suspected owner; this command is how an operator clears a whole set of them in one go:
``--remove "<reason>"`` retires every selected card in this record, ``--to <product>`` retires
it here and files a copy in the target product's own inbox, deduped, so running the same command
twice imports nothing twice.

It writes the two typed fields ``asf set`` refuses on purpose (``removed``, ``moved_to``) —
``asf.record.new._COMMON_SET`` holds neither, and that refusal stands: this command is the
closing's one safe door, not a second way through ``asf set``. Safe here because a closing
always carries a reason, is written through the record's own round-trip parser
(:func:`asf.record.setfield.set_typed`), and is all-or-nothing — :func:`check` renders and
round-trips every selected card first, and the command writes nothing at all, in either record,
if any one of them would not come back exactly as asked (D10). The source's write is one
signed-off, pushed commit (``asf.cli._published``); a ``--to`` run's import into the target is a
second commit, in the target's own checkout (D15) — two git repositories, never one commit
across both.
"""
import os
import re
import sys

from asf.record import publish
from asf.record.core import canonicalize, is_retired, load_items, parse_sections, today
from asf.record.setfield import set_typed


def add_arguments(p):
    p.add_argument('ids', nargs='*', help='card ids to select (never together with --query)')
    p.add_argument('--query', action='append', metavar='FIELD=VALUE',
                   help="an exact match on one typed field read off the record, repeatable "
                        "(AND-ed); not together with ids")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--to', metavar='PRODUCT',
                   help="retire the selection here and file it in this product's inbox")
    g.add_argument('--remove', metavar='REASON', help='retire the selection with this reason')
    p.add_argument('--product')
    p.add_argument('--dry-run', action='store_true',
                   help='print the table and write nothing, in either record')


def select(canonical, ids, queries):
    """``([(id, rec)], error)`` off ``ids`` or ``--query FIELD=VALUE`` pairs (D8, D9): never
    both, and at least one. ``--query`` is an exact string match on one typed field, repeatable
    and AND-ed — nothing else, no free-text search. An id naming no card refuses, naming it; a
    card already retired (:func:`asf.record.core.is_retired`, P4) is dropped from the selection
    and counted, so a re-run of the same command is a no-op rather than a second write. An empty
    selection — after that drop — refuses (D8): a silent empty selection exiting 0 is how a bulk
    command would lie about having worked."""
    ids = list(dict.fromkeys(ids or ()))
    queries = list(queries or ())
    if ids and queries:
        return [], "asf move takes ids or --query, never both"
    if not ids and not queries:
        return [], "asf move wants ids, or --query FIELD=VALUE"
    if ids:
        missing = [i for i in ids if i not in canonical]
        if missing:
            return [], f"no item {missing[0]!r}"
        chosen = ids
        label = ' '.join(ids)
    else:
        pairs = []
        for q in queries:
            field, eq, value = q.partition('=')
            if not eq:
                return [], f"--query {q!r} wants FIELD=VALUE"
            pairs.append((field, value))
        chosen = sorted(iid for iid, rec in canonical.items()
                        if all(_field_eq(rec['meta'], field, value) for field, value in pairs))
        label = ' '.join(f'--query {field}={value}' for field, value in pairs)
    selected = [(iid, canonical[iid]) for iid in chosen if not is_retired(canonical[iid]['meta'])]
    if not selected:
        return [], f"{label} selects no card"
    return selected, None


def _field_eq(meta, field, value):
    v = meta.get(field)
    return value == '' if v is None else str(v) == value


def resolve_target(args, root):
    """``(target product, source name, error)`` for ``--to`` (D16, §5 step 2): a ``--to`` that
    does not resolve (``env.ConfigError``) is left to propagate — the console's own
    ``cli.needs_operator_line`` line and exit 2, the way every other command's unresolvable
    product already is. ``--to`` naming the source product itself refuses, and so does a source
    whose own resolved product's ``backlog_dir`` is not ``root`` — compared by directory
    identity, not by spelling, the way :func:`asf.env.product_of_dir` compares. The name is
    written into every ``moved_from:`` ref, so a wrong one would make every future dedupe on the
    target miss."""
    from asf import env
    target = env.load_product(args.to)
    source = env.resolve_product(args.product)
    if args.to == source.name:
        return None, None, f"--to {args.to} is this record's own product"
    source_product = env.load_product(source.name)
    sbd = source_product.backlog_dir
    if not sbd or os.path.realpath(sbd) != os.path.realpath(root):
        return None, None, (f"--to needs the source product's own record — {root} is not "
                            f"{source.name}'s backlog_dir")
    return target, source.name, None


def check(selection, updates):
    """``[(id, reason)]`` for every card in ``selection`` that would not round-trip ``updates``
    (D10): :func:`asf.record.setfield.set_typed` with ``apply=False`` — the writer's own render
    and parse, run twice, never a second parser. Empty when every card would come back exactly as
    asked, which is what lets the caller write the whole selection in one pass with nothing left
    to fail."""
    failures = []
    for iid, rec in selection:
        err = set_typed(rec, updates, writer='move', apply=False)
        if err:
            failures.append((iid, err))
    return failures


def _slug(title):
    return re.sub(r'[^a-z0-9]+', '-', (title or '').lower()).strip('-') or 'card'


def _free_name(taken, slug):
    """``<slug>.md``, or ``-2``/``-3``… for the first name not in ``taken`` — the way
    ``asf.groom.inbox.file_card`` names a slug already taken (P16)."""
    name = f'{slug}.md'
    n = 2
    while name in taken:
        name = f'{slug}-{n}.md'
        n += 1
    return name


def _scrubbed_title(title, root):
    from asf.groom.inbox import scrub_title
    from asf.groom.shape import Card
    return scrub_title(Card(title or '', {}, '', [], []), root).title


def _source_sections(body):
    """``(description, [acceptance item, …])`` off a source card's raw body — its
    ``## Description`` verbatim and its ``## Acceptance`` list stripped of the checkbox markup,
    the shape :func:`asf.groom.inbox.process_inbox` reads back out of an intake file."""
    _preamble, sections = parse_sections(body or '')
    description, acceptance = '', []
    for heading, content in sections:
        h = heading.strip()
        if h == '## Description':
            description = content.strip('\n')
        elif h == '## Acceptance':
            for line in content.split('\n'):
                m = re.match(r'^-\s*\[[ xX]\]\s*(.*)$', line.strip())
                if m and m.group(1):
                    acceptance.append(m.group(1))
    return description, acceptance


def dedupe(target_root, intake_dir, ref):
    """What already carries ``moved_from: <ref>`` on the target, or ``None`` (D14): a minted
    card's id, read from ``index.json`` when there is one, else ``load_items(target_root)``; else
    the intake note's file name, open or already moved to ``done/`` (P16, since the intake file
    is deleted the moment the target's groom mints the card — a filename dedupe alone would
    re-import every card after the first successful groom)."""
    index_path = os.path.join(target_root, 'index.json')
    if os.path.isfile(index_path):
        import json
        with open(index_path, encoding='utf-8') as f:
            data = json.load(f) or {}
        for iid, item in (data.get('items') or {}).items():
            if (item or {}).get('moved_from') == ref:
                return iid
    else:
        by_id, _errors = load_items(target_root)
        for iid, records in by_id.items():
            for rec in records:
                if rec['meta'].get('moved_from') == ref:
                    return iid
    needle = f"moved_from: {ref}"
    d = os.path.join(target_root, intake_dir)
    for sub in ('', 'done'):
        dd = os.path.join(d, sub) if sub else d
        if not os.path.isdir(dd):
            continue
        for name in sorted(os.listdir(dd)):
            path = os.path.join(dd, name)
            if not name.endswith('.md') or not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as f:
                text = f.read()
            if any(line.strip() == needle for line in text.split('\n')):
                return name
    return None


def intake_file(target, rec, source_name):
    """Write one intake note for ``rec`` into ``target``'s inbox — ``<target.backlog_dir>/
    <target.conventions.intake_dir>/<slug>.md``, slug-named and ``-2``/``-3``-suffixed the way
    ``cmd_inbox`` names a taken one (P16, D13). Carries ``moved_from: <source_name>:<id>`` (D14),
    the source card's ``type:``, a Bug's ``signature:``/``severity:`` or a Task's ``writes:`` —
    and never ``parent:``: an id is meaningless in another record, and the target's intake asks
    its own parent question on the target's own groom day. The title goes through
    ``asf.groom.inbox.scrub_title``'s redaction filter first. The path written."""
    meta = rec['meta']
    iid = meta.get('id')
    type_ = meta.get('type')
    root = target.backlog_dir
    intake_dir = target.conventions.intake_dir
    d = os.path.join(root, intake_dir)
    os.makedirs(d, exist_ok=True)
    title = _scrubbed_title(meta.get('title'), root)
    description, acceptance = _source_sections(rec.get('body'))
    headers = [f"moved_from: {source_name}:{iid}", f"type: {type_}"]
    if type_ == 'bug':
        if meta.get('signature'):
            headers.append(f"signature: {meta['signature']}")
        if meta.get('severity'):
            headers.append(f"severity: {meta['severity']}")
    elif type_ == 'task' and meta.get('writes'):
        headers.append(f"writes: [{', '.join(meta['writes'])}]")
    text = f"# {title}\n" + '\n'.join(headers) + "\n\n" + description
    if acceptance:
        text = text.rstrip('\n') + "\n\n## Acceptance\n" + ''.join(f"- [ ] {a}\n" for a in acceptance)
    if not text.endswith('\n'):
        text += '\n'
    taken = set(os.listdir(d))
    name = _free_name(taken, _slug(title))
    path = os.path.join(d, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def table(rows):
    """The dry-run table (§5 step 4): one markdown row per selected card — ``ID``, ``TYPE``,
    ``TITLE``, ``ACTION``, ``TARGET`` — redrawn as a box table on a terminal
    (:mod:`asf.tables`)."""
    head = ['ID', 'TYPE', 'TITLE', 'ACTION', 'TARGET']
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lines.append("| " + " | ".join((c or "—").replace("|", "\\|") for c in r) + " |")
    return "\n".join(lines)


def _rows_and_dedupe(selection, args, target, dups, remove_mode):
    """``(rows, already)`` for the dry-run table — and the preview of each target cell: the
    free intake name the real write would use (never written here), predicted off the intake
    dir's current listing the same way :func:`intake_file` picks one for real, in selection order
    so two cards sharing a title never predict the same name."""
    rows, already, taken = [], 0, set()
    d = os.path.join(target.backlog_dir, target.conventions.intake_dir) if target else None
    if d and os.path.isdir(d):
        taken = set(os.listdir(d))
    for iid, rec in selection:
        meta = rec['meta']
        if remove_mode:
            rows.append([iid, meta.get('type') or '', meta.get('title') or '', 'removed', '—'])
            continue
        dup = dups.get(iid)
        if dup:
            already += 1
            cell = f"already there ({dup})"
        else:
            title = _scrubbed_title(meta.get('title'), target.backlog_dir)
            name = _free_name(taken, _slug(title))
            taken.add(name)
            cell = os.path.join(target.conventions.intake_dir, name)
        rows.append([iid, meta.get('type') or '', meta.get('title') or '',
                    f"moved to {args.to}", cell])
    return rows, already


def cmd_move(args, root):
    """``asf move``: see the module docstring and ``docs/specs/f-0120.md``'s §5. Prints and
    returns 2 on any refusal, with nothing written in either record; on ``--dry-run`` prints the
    table and returns 0, also having written nothing. Dispatched through ``asf.cli._published``
    (not here: a console command never commits its own record — P3)."""
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    selection, err = select(canonical, args.ids, args.query or [])
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    remove_mode = args.to is None
    target = source_name = None
    if not remove_mode:
        target, source_name, err = resolve_target(args, root)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    updates = ({'removed': args.remove, 'belongs_to': None} if remove_mode
               else {'moved_to': args.to, 'belongs_to': None})
    failures = check(selection, updates)
    if failures:
        for iid, reason in failures:
            print(f"error: {iid}: {reason}", file=sys.stderr)
        return 2

    dups = {}
    if not remove_mode:
        for iid, _rec in selection:
            dups[iid] = dedupe(target.backlog_dir, target.conventions.intake_dir,
                               f"{source_name}:{iid}")

    if args.dry_run:
        rows, already = _rows_and_dedupe(selection, args, target, dups, remove_mode)
        print(table(rows))
        footer = f"{len(selection)} card" + ('' if len(selection) == 1 else 's')
        if not remove_mode:
            footer += f", {already} already in the target"
        print(footer)
        return 0

    date = today()
    before = publish.snapshot(target.backlog_dir) if target else None
    for iid, rec in selection:
        if remove_mode:
            history = f"- {date} move: removed — {args.remove}"
        else:
            where = dups[iid] or os.path.basename(intake_file(target, rec, source_name))
            history = f"- {date} move: moved to {args.to} — {args.to}:{where}"
        err = set_typed(rec, updates, writer='move', history=[history])
        if err:  # unreachable after check(): the selection is left unexplained, never half-written
            print(f"error: {iid}: {err}", file=sys.stderr)
            return 2
    if target:
        publish.publish_changes(target.backlog_dir, before,
                                f"move: {len(selection)} cards from {source_name} — inbox")
    print(f"{len(selection)} cards " + ('removed' if remove_mode else f'moved to {args.to}'))
    return 0
