"""asf.record.new — mint a new work item (``asf new``)."""
import os
import sys

from asf.record import frontmatter
from asf.record.core import (
    NO_PARENT_TYPES, PARENT_TYPES, TYPES, canonicalize, is_open, jaccard, load_items, now_iso,
    today, tokenize,
)
from asf.record.ids import mint_id, write_new_item
from asf.schema import SCHEMA_VERSION


SEVERITIES = ('S1', 'S2', 'S3')

# Typed fields `--set` may write, beyond the ones that have their own flag.
_COMMON_SET = ('rank', 'decided', 'blockedBy', 'links', 'priority', 'area', 'legacy_id')
SETTABLE = {t: set(_COMMON_SET) for t in TYPES}
SETTABLE['rule'] |= {'scope', 'enforced', 'reason', 'check'}
SETTABLE['decision'] |= {'decided_by', 'date'}


def _parse_sets(type_, pairs):
    """[(key, subkey|None, value)] from `--set key=value` pairs, or raise ValueError."""
    out = []
    for pair in pairs or []:
        key, eq, raw = pair.partition('=')
        if not eq or not key:
            raise ValueError(f"--set wants key=value, got {pair!r}")
        top, dot, sub = key.partition('.')
        if top not in SETTABLE[type_] or (dot and top != 'links'):
            raise ValueError(f"{type_} has no settable field {key!r} "
                             f"(one of {', '.join(sorted(SETTABLE[type_]))})")
        out.append((top, sub if dot else None, frontmatter._parse_value(raw)))
    return out


def add_arguments(p_new):
    """The shape flags of ``asf new`` (cli.py calls this on the ``new`` subparser)."""
    p_new.add_argument('--set', action='append', metavar='KEY=VALUE',
                       help='any other typed field of the type (repeatable; links.KEY=V for links)')
    p_new.add_argument('--acceptance', action='append', metavar='TEXT',
                       help='story: one acceptance item (repeatable; required)')
    p_new.add_argument('--writes', action='append', metavar='GLOB',
                       help='task: one path of the writes: footprint (repeatable; required)')


def cmd_new(args, root):
    type_ = args.type
    if type_ in ('epic', 'feature', 'bug'):
        print(f"error: {type_} is new work — it enters through the inbox: "
              "asf inbox --title \"…\" [--body-file F]; the groom derives its type "
              "from the card's shape", file=sys.stderr)
        return 2
    if type_ not in TYPES:
        print(f"error: unknown type {type_!r}", file=sys.stderr)
        return 2
    try:
        sets = _parse_sets(type_, getattr(args, 'set', None))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.priority and args.priority not in ('need', 'nice'):
        print("error: --priority must be need or nice", file=sys.stderr)
        return 2

    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)

    folder, prefix = TYPES[type_]

    if type_ in NO_PARENT_TYPES:
        if args.parent:
            print(f"error: {type_} has no parent", file=sys.stderr)
            return 2
    else:
        if not args.parent:
            print(f"error: {type_} requires --parent", file=sys.stderr)
            return 2
        prec = canonical.get(args.parent)
        if prec is None:
            print(f"error: parent {args.parent} not found", file=sys.stderr)
            return 2
        ptype = prec['meta'].get('type')
        if ptype not in PARENT_TYPES[type_]:
            print(f"error: {type_} cannot have parent type {ptype}", file=sys.stderr)
            return 2

    acceptance = getattr(args, 'acceptance', None)
    writes = getattr(args, 'writes', None)
    if type_ == 'story' and not acceptance:
        print("error: a Story is one PR with one acceptance list — give --acceptance",
              file=sys.stderr)
        return 2
    if type_ == 'task' and not writes:
        print("error: a Task is one session with a writes: footprint — give --writes",
              file=sys.stderr)
        return 2

    tokens_new = tokenize(args.title)
    matches = []
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != type_ or not is_open(rec):
            continue
        score = jaccard(tokens_new, tokenize(rec['meta'].get('title', '')))
        if score > 0.6:
            matches.append((iid, rec['meta'].get('title', ''), score))
    if matches and not args.force:
        for iid, title2, score in sorted(matches, key=lambda m: -m[2]):
            print(f"{iid}: {title2} (overlap {score:.2f})", file=sys.stderr)
        return 3

    new_id = mint_id(root, canonical, type_)

    meta = frontmatter.FrontmatterDict()
    meta['id'] = new_id
    meta['type'] = type_
    meta['title'] = args.title
    if args.parent:
        meta['parent'] = args.parent
    if args.priority:
        meta['priority'] = args.priority
    if args.area:
        meta['area'] = args.area
    if args.legacy_id:
        meta['legacy_id'] = args.legacy_id
    if type_ == 'task':
        meta['writes'] = writes
    for key, sub, value in sets:
        if sub:
            meta.setdefault(key, {})[sub] = value
        else:
            meta[key] = value
    if type_ in ('story', 'task'):
        body = ''
        if args.body_file:
            with open(args.body_file, encoding='utf-8') as f:
                body = f.read().rstrip('\n')
        shape = ('parent-feature', 'story') if type_ == 'story' else ('writes', 'task')
        typed = {k: v for k, v in meta.items() if k not in ('id', 'type')}
        write_new_item(root, canonical, type_, new_id, typed, body, today(), 'new',
                       acceptance=acceptance or (), shape=shape)
        print(new_id)
        return 0
    ts = now_iso()
    meta['schema_version'] = SCHEMA_VERSION
    meta['state'] = 'New'
    meta['stage_since'] = ts
    meta['updated'] = ts
    meta.machine_keys = {'schema_version', 'state', 'stage_since', 'updated'}

    if args.body_file:
        with open(args.body_file, encoding='utf-8') as f:
            body = f.read()
    else:
        body = (
            "## Description\n\n"
            "## Acceptance\n"
            "- [ ] \n\n"
            "## Non-goals\n\n"
            "## History\n"
            f"- {today()}: created\n\n"
            "## Children\n\n"
            "## Backlinks\n"
        )

    text = frontmatter.render(meta, body)
    d = os.path.join(root, folder)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{new_id}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(new_id)
    return 0
