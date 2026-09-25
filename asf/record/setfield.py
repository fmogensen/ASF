"""asf.record.setfield — write typed fields through the parser (``asf set``, B-0084).

The typed block is a machine-read format; a hand edit that breaks it costs a tick. ``asf set <id>
field=value…`` renders the change, parses the result back, and writes only when the card
round-trips to exactly what was asked.

A Task's list fields ``writes:`` and ``after:`` take three forms: ``writes=[a, b]`` replaces the
list, ``writes+=a`` (or ``writes+=[a, b]``) adds what is not there yet, ``writes-=a`` removes."""
import os
import sys
import tempfile

from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, record_root
from asf.record.new import _parse_sets

#: The list-valued fields ``asf set`` writes with ``=`` / ``+=`` / ``-=``, per type.
LIST_FIELDS = {'task': ('writes', 'after')}


def _as_list(value):
    if value is None or value == '':
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def parse_assignments(type_, pairs):
    """``[(key, subkey|None, op, value)]`` off ``asf set``'s ``FIELD=VALUE`` pairs: ``op`` is
    ``'='``, or ``'+'`` / ``'-'`` for a list field's add and remove forms. Raise ValueError."""
    lists = LIST_FIELDS.get(type_, ())
    out = []
    for pair in pairs or []:
        key, eq, raw = pair.partition('=')
        op = '='
        if key[-1:] in ('+', '-') and key[:-1] in lists:
            key, op = key[:-1], key[-1]
        if eq and key in lists:
            value = _as_list(frontmatter._parse_value(raw))
            if not value and op != '=':
                raise ValueError(f"{key}{op}= wants a path or a [list], got {raw!r}")
            out.append((key, None, op, value))
            continue
        if key[-1:] in ('+', '-'):
            raise ValueError(f"{key}= — only a list field ({', '.join(lists) or 'none'} on a "
                             f"{type_}) takes the add/remove forms")
        try:
            top, sub, value = _parse_sets(type_, [pair])[0]
        except ValueError as e:
            if not lists:
                raise
            raise ValueError(f"{e}; list fields: {', '.join(lists)} (=, +=, -=)") from None
        out.append((top, sub, op, value))
    return out


def apply_list(current, op, value):
    """``current`` with ``value`` put by ``op``: ``=`` replaces, ``+`` appends what is missing,
    ``-`` drops what is named."""
    have = _as_list(current)
    if op == '+':
        return have + [v for v in dict.fromkeys(value) if v not in have]
    if op == '-':
        return [v for v in have if v not in value]
    return list(dict.fromkeys(value))


def cmd_set(args, root):
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    rec = canonical.get(args.id)
    if rec is None:
        print(f"error: no item {args.id!r}", file=sys.stderr)
        return 2
    try:
        sets = parse_assignments(rec['meta'].get('type'), args.assignments)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    updates = {}
    for top, sub, op, value in sets:
        if top in LIST_FIELDS.get(rec['meta'].get('type'), ()):
            base = updates[top] if top in updates else rec['meta'].get(top)
            updates[top] = apply_list(base, op, value)
            if top == 'writes' and not updates[top]:
                print(f"error: a Task keeps a writes: footprint — {args.id} would have none",
                      file=sys.stderr)
                return 2
        elif sub:
            links = dict(updates.get(top) or rec['meta'].get(top) or {})
            links[sub] = value
            updates[top] = links
        else:
            updates[top] = value

    err = set_typed(rec, updates)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    lists = LIST_FIELDS.get(rec['meta'].get('type'), ())
    print(f"{args.id}: set " + ', '.join(
        f"{k}={' '.join(v)}" if k in lists else k for k, v in updates.items()))
    return 0


def set_typed(rec, updates, writer='set'):
    """Write ``updates`` (typed fields) onto the card ``rec`` (a ``load_items`` record) through the
    parser: rendered on a scratch copy, parsed back, written only when every field round-trips.
    Returns None on success, else the reason the card is unchanged."""
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
            return (f"{', '.join(updates)} cannot be written — {e.file}:{e.line}: {e.why}; "
                    f"{rec['relpath']} is unchanged")
        for key, value in updates.items():
            if meta.get(key) != value:
                return (f"{key}={value!r} does not round-trip through the parser "
                        f"(it reads back as {meta.get(key)!r}); {rec['relpath']} is unchanged")
    finally:
        os.unlink(scratch)
    root = record_root(rec)
    if root is None:  # a card outside any record layout: nothing to validate it against
        _write(None, rec['path'], new_text)
    else:
        # one writer through the stage (R14): an invariant it breaks (I3: an Active Task's
        # writes: now intersecting another's) refuses the write before anyone commits it
        from asf.record import stage
        _r, _staged, findings = stage.guarded(root, writer, _write, (rec['path'], new_text),
                                              only=[rec['relpath']])
        if findings:
            return (f"{', '.join(updates)} refused — "
                    + '; '.join(f'{f.invariant}: {f.message}' for f in findings)
                    + f"; {rec['relpath']} is unchanged")
    rec['text'] = new_text
    return None


def _write(_root, path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
