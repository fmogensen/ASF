"""asf.record.setfield — write typed fields through the parser (``asf set``, B-0084).

The typed block is a machine-read format; a hand edit that breaks it costs a tick. ``asf set <id>
field=value…`` renders the change, parses the result back, and writes only when the card
round-trips to exactly what was asked.

A Task's list fields ``writes:`` and ``after:`` take three forms: ``writes=[a, b]`` replaces the
list, ``writes+=a`` (or ``writes+=[a, b]``) adds what is not there yet, ``writes-=a`` removes.
Both sides are flattened first, so a plan's packed entry (``writes: [a.py b.py]``) is the paths it
names. On ``writes:`` an add the footprint already covers adds nothing, and a remove that would
remove nothing is refused."""
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


def apply_list(current, op, value, covers=False):
    """``(new, changed)``: ``current`` with ``value`` put by ``op``. Both sides are flattened
    first (:func:`asf.feeder.widen.norm_writes`), so an entry a plan packed two paths into
    (``writes: [a.py b.py]``) is the two paths it names — the shape every other reader of
    ``writes:`` already sees (D2).

    ``=`` replaces, ``+`` adds what is not there, ``-`` drops what is named. With ``covers``
    (``writes:``, whose entries are path globs) ``+`` also drops a path the footprint already
    covers by :func:`asf.feeder.widen.covered` — the rule the tick's own widening uses, so the
    hand route and the automatic one add the same paths (D3). ``changed`` is False when the op
    left the footprint as it was."""
    from asf.feeder import widen  # the tree's one splitting and coverage rule (D3, D4)
    have = widen.norm_writes(_as_list(current))
    want = widen.norm_writes(value)
    if op == '+':
        add = [v for v in want if v not in have and not (covers and widen.covered(v, have))]
        return have + add, bool(add)
    if op == '-':
        kept = [v for v in have if v not in want]
        return kept, kept != have
    return want, want != have


def _why_not_removed(field, item_id, base, value):
    """D5's reason: the glob that covers each path, else that the field does not name it."""
    from asf.feeder import widen
    have = widen.norm_writes(_as_list(base))
    out = []
    for v in widen.norm_writes(value):
        glob = next((w for w in have if w != v and widen.covered(v, [w])), None)
        out.append(f"{glob} covers it; replace the glob ({field}=[…])" if glob
                   else f"{item_id}'s {field}: does not name it")
    return '; '.join(dict.fromkeys(out))


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
    idempotent = []
    all_noop = True  # PD4: the `set` line is suppressed only when every op was a list-field no-op
    for top, sub, op, value in sets:
        if top in LIST_FIELDS.get(rec['meta'].get('type'), ()):
            base = updates[top] if top in updates else rec['meta'].get(top)
            updates[top], changed = apply_list(base, op, value, covers=(top == 'writes'))
            if changed:
                all_noop = False
            if op == '-' and not changed:
                print(f"error: {top}-={' '.join(value)} removes nothing — "
                      + _why_not_removed(top, args.id, base, value), file=sys.stderr)
                return 2
            if op == '+' and not changed:
                idempotent.append((top, value))
            if top == 'writes' and not updates[top]:
                print(f"error: a Task keeps a writes: footprint — {args.id} would have none",
                      file=sys.stderr)
                return 2
        elif sub:
            links = dict(updates.get(top) or rec['meta'].get(top) or {})
            links[sub] = value
            updates[top] = links
            all_noop = False
        else:
            updates[top] = value
            all_noop = False

    from asf.record.check import product_of  # the same soft loader cmd_check uses (D5, PD6)
    err = set_typed(rec, updates, product=product_of(args))
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if idempotent:
        from asf.feeder import widen  # function-local like the other two (D4)
        for top, value in idempotent:
            print(f"{args.id}: {top}: already covers {' '.join(widen.norm_writes(value))} — "
                  "footprint unchanged")
    lists = LIST_FIELDS.get(rec['meta'].get('type'), ())
    if not all_noop:
        print(f"{args.id}: set " + ', '.join(
            f"{k}={' '.join(v)}" if k in lists else k for k, v in updates.items()))
    return 0


def set_typed(rec, updates, writer='set', product=None):
    """Write ``updates`` (typed fields) onto the card ``rec`` (a ``load_items`` record) through the
    parser: rendered on a scratch copy, parsed back, written only when every field round-trips.
    ``product``: passed to the record stage's I3 check, so a ``writes:`` update that only adds a
    path ``product``'s ``conventions.shared_paths`` covers is never refused as intersecting
    another Active Task's footprint. Returns None on success, else the reason the card is
    unchanged."""
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
                                              product=product, only=[rec['relpath']])
        if findings:
            return (f"{', '.join(updates)} refused — "
                    + '; '.join(f'{f.invariant}: {f.message}' for f in findings)
                    + f"; {rec['relpath']} is unchanged")
    rec['text'] = new_text
    return None


def _write(_root, path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
