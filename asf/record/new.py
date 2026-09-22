"""asf.record.new — mint a new work item (``asf new``)."""
import os
import sys

from asf.record import frontmatter
from asf.record.core import (
    NO_PARENT_TYPES, PARENT_TYPES, TYPES, canonicalize, is_open, jaccard, load_items, now_iso,
    today, tokenize,
)
from asf.record.ids import mint_id
from asf.schema import SCHEMA_VERSION


SEVERITIES = ('S1', 'S2', 'S3')

# Typed fields `--set` may write, beyond the ones that have their own flag.
_COMMON_SET = ('rank', 'decided', 'blockedBy', 'links', 'priority', 'area', 'legacy_id')
SETTABLE = {t: set(_COMMON_SET) for t in TYPES}
SETTABLE['bug'] |= {'source', 'severity', 'found_in', 'signature'}
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
    """The Bug-only flags of ``asf new`` (cli.py calls this on the ``new`` subparser)."""
    p_new.add_argument('--severity', choices=SEVERITIES, help='required for bug')
    p_new.add_argument('--signature', help='bug: the key "same signature = same Bug" files under')
    p_new.add_argument('--set', action='append', metavar='KEY=VALUE',
                       help='any other typed field of the type (repeatable; links.KEY=V for links)')
    p_new.add_argument('--found-in', default='dev', help='bug: where it was found (default dev)')


def cmd_new(args, root):
    type_ = args.type
    severity = getattr(args, 'severity', None)
    signature = getattr(args, 'signature', None)
    found_in = getattr(args, 'found_in', None) or 'dev'
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

    if type_ == 'bug' and severity not in SEVERITIES:
        print("usage: asf new bug --title T --parent E-nnnn --severity {S1,S2,S3} "
              "[--signature S] [--found-in WHERE]", file=sys.stderr)
        print("error: bug requires --severity (S1, S2 or S3)", file=sys.stderr)
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
    if type_ == 'bug':
        meta['severity'] = severity
        meta['found_in'] = found_in
        if signature:
            meta['signature'] = signature
    for key, sub, value in sets:
        if sub:
            meta.setdefault(key, {})[sub] = value
        else:
            meta[key] = value
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
