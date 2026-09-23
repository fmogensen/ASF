"""asf.record.setfield — write typed fields through the parser (``asf set``, B-0084).

The typed block is a machine-read format; a hand edit that breaks it costs a tick. ``asf set <id>
field=value…`` renders the change, parses the result back, and writes only when the card
round-trips to exactly what was asked."""
import os
import sys
import tempfile

from asf.record import frontmatter
from asf.record.core import canonicalize, load_items
from asf.record.new import _parse_sets


def cmd_set(args, root):
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    rec = canonical.get(args.id)
    if rec is None:
        print(f"error: no item {args.id!r}", file=sys.stderr)
        return 2
    try:
        sets = _parse_sets(rec['meta'].get('type'), args.assignments)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    updates = {}
    for top, sub, value in sets:
        if sub:
            links = dict(updates.get(top) or rec['meta'].get(top) or {})
            links[sub] = value
            updates[top] = links
        else:
            updates[top] = value

    # write to a scratch copy first: the card is replaced only if it parses back to the ask
    fd, scratch = tempfile.mkstemp(suffix='.md')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(rec['text'])
        frontmatter.write_typed(scratch, updates)
        with open(scratch, encoding='utf-8') as f:
            new_text = f.read()
        try:
            meta, _body = frontmatter.parse(new_text, path=rec['relpath'])
        except frontmatter.FrontmatterError as e:
            print(f"error: {', '.join(updates)} cannot be written — {e.file}:{e.line}: {e.why}; "
                  f"{rec['relpath']} is unchanged", file=sys.stderr)
            return 2
        for key, value in updates.items():
            if meta.get(key) != value:
                print(f"error: {key}={value!r} does not round-trip through the parser "
                      f"(it reads back as {meta.get(key)!r}); {rec['relpath']} is unchanged",
                      file=sys.stderr)
                return 2
    finally:
        os.unlink(scratch)
    with open(rec['path'], 'w', encoding='utf-8') as f:
        f.write(new_text)
    print(f"{args.id}: set {', '.join(updates)}")
    return 0
