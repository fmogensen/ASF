"""asf.record.setfield — write typed fields through the parser (``asf set``, B-0084).

The typed block is a machine-read format; a hand edit that breaks it costs a tick. ``asf set <id>
field=value…`` renders the change, parses the result back, and writes only when the card
round-trips to exactly what was asked.

A Task's list fields ``writes:`` and ``after:`` take three forms: ``writes=[a, b]`` replaces the
list, ``writes+=a`` (or ``writes+=[a, b]``) adds what is not there yet, ``writes-=a`` removes.
Both sides are flattened first, so a plan's packed entry (``writes: [a.py b.py]``) is the paths it
names. On ``writes:`` an add the footprint already covers adds nothing, and a remove that would
remove nothing is refused.

A Bug's ``severity`` is settable here (F-0163), and it is the one field this command reasons about
rather than merely writing: it appends ``- <date> set: severity S1 → S2 — <why>`` to ``## History``
in the same guarded write as the field, and refuses a move **off** ``S1`` without ``--why``. S1 is
the severity that holds the whole tier-2 queue (:func:`asf.feeder.rows.bug_rows`), so leaving it
spends something that belongs to other work; the sentence that justifies it is worth more later
than the field."""
import os
import re
import sys
import tempfile

from asf.record import frontmatter
from asf.record import writer as card_writer
from asf.record.core import canonicalize, load_items, parse_sections, record_root, today
from asf.record.new import _parse_sets  # noqa: F401

#: The list-valued fields ``asf set`` writes with ``=`` / ``+=`` / ``-=``, per type.
LIST_FIELDS = {'task': ('writes', 'after')}
#: Fields ``asf set`` writes on an existing card that ``asf new --set`` does not take: a card is
#: re-parented (``parent=S-0001``) or taken off the board (``removed=true`` or a reason).
SET_ONLY = ('parent', 'removed')
#: The History line a severity change files on the Bug — the card's own words, dated like every
#: other line in the record and named for its writer (D3).
SEVERITY_HISTORY = '- {date} set: severity {old} → {new}'
#: …with the reason a move off S1 must carry.
SEVERITY_WHY = ' — {why}'


def severity_change(rec, updates, why):
    """``(history_line, None)`` for a severity assignment in ``updates``, ``(None, None)`` when
    there is none or the value is the one already on the card (D5), or ``(None, reason)`` when the
    change is refused.

    Refused when the card leaves ``S1`` without a ``--why`` (D4), when ``--why`` is given and no
    severity moves (D6), or when the card carries no ``## History`` section to write the line into
    (D9)."""
    new = updates.get('severity')
    old = rec['meta'].get('severity')
    noop = new is None or new == old
    if why and noop:
        return None, "--why records why a severity moved; no severity assignment in this command"
    if why and '\n' in why:
        return None, "--why is one line, because it is written as one line of ## History"
    if noop:
        return None, None
    if old == 'S1' and new != 'S1' and not why:
        return None, (f"severity={new} leaves S1 without --why \"<reason>\" — leaving S1 "
                       "releases the lane's hold on everything behind it")
    if not any(h.strip() == '## History' for h, _ in parse_sections(rec['body'])[1]):
        return None, f"{rec['relpath']} has no ## History section to record the change in"
    line = SEVERITY_HISTORY.format(date=today(), old=old, new=new)
    if why:
        line += SEVERITY_WHY.format(why=why)
    return line, None


def reshape_applied(rec, updates, why):
    """``asf set <fid> reshape_applied=current --why "…"``: the Feature's pending ``reshape:``
    recorded as carried out by hand (a replan the record cannot apply, applied by the operator).
    ``updates`` gets ``reshape_applied: <digest of the reshape: text>`` and ``reshape_applied_at``
    — the very fields the replan applier writes, so the feeder's REPLAN row ends the same way —
    and the History line names the digest and the reason. ``(history_line, None)``, or
    ``(None, reason)`` when refused: not a Feature, no ``reshape:`` on it, or no one-line
    ``--why``."""
    from asf.record import replan
    from asf.record.core import now_iso
    meta = rec['meta']
    if meta.get('type') not in ('feature', 'task'):
        return None, 'reshape_applied= is a Feature or Task field'
    if not meta.get('reshape'):
        return None, f"{meta.get('id')} carries no reshape: — nothing to record as applied"
    if not why or '\n' in why:
        return None, ('reshape_applied=current wants --why "<what was done>" (one line, written '
                      'into ## History)')
    if not any(h.strip() == '## History' for h, _ in parse_sections(rec['body'])[1]):
        return None, f"{rec['relpath']} has no ## History section to record the change in"
    d = replan.digest(meta['reshape'])
    updates[replan.APPLIED] = d
    updates[replan.APPLIED_AT] = now_iso()
    return f'- {today()} set: reshape_applied {d} — {why}', None


def _as_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    # a raw CLI value such as ``a, b, c`` or ``a b c`` is comma- and/or space-separated;
    # split it here so an entry never keeps a trailing comma (B-121288)
    return [v for v in re.split(r'[,\s]+', str(value).strip()) if v]


def _set_only(type_, key, raw, canonical):
    """The value of a :data:`SET_ONLY` field, validated, or raise ValueError. ``parent``: an id
    ``canonical`` holds, of a type :data:`asf.record.core.PARENT_TYPES` lets ``type_`` hang
    under. ``removed``: ``true`` / ``false``, or a reason (any other text)."""
    from asf.record.core import NO_PARENT_TYPES, PARENT_TYPES
    value = frontmatter._parse_value(raw)
    if key == 'removed':
        if isinstance(value, bool) or (isinstance(value, str) and value.strip()):
            return value
        raise ValueError(f"removed={raw!r} — true, false, or the reason it is removed")
    pid = str(value or '').strip()
    if type_ in NO_PARENT_TYPES:
        raise ValueError(f"parent= — a {type_} has no parent")
    prec = (canonical or {}).get(pid) if pid else None
    if prec is None:
        raise ValueError(f"parent={raw!r} — no such item in the record")
    ptype = prec['meta'].get('type')
    allowed = PARENT_TYPES.get(type_, set())
    if ptype not in allowed:
        raise ValueError(f"parent={pid} — a {type_} hangs under a {' or '.join(sorted(allowed))}, "
                         f"not a {ptype}")
    return pid


def parse_assignments(type_, pairs, canonical=None):
    """``[(key, subkey|None, op, value)]`` off ``asf set``'s ``FIELD=VALUE`` pairs: ``op`` is
    ``'='``, or ``'+'`` / ``'-'`` for a list field's add and remove forms. ``canonical``: the
    record, which a ``parent=`` is checked against. Raise ValueError."""
    lists = LIST_FIELDS.get(type_, ())
    out = []
    for pair in pairs or []:
        key, eq, raw = pair.partition('=')
        op = '='
        if eq and key in SET_ONLY:
            out.append((key, None, op, _set_only(type_, key, raw, canonical)))
            continue
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


def split_targets(args):
    """``(ids, assignments)`` off ``asf set ID [ID…] FIELD=VALUE…``: every word with no ``=`` is
    an id, so one call (one record commit) sets a field on many cards."""
    ids, assignments = [args.id], []
    for a in args.assignments:
        (assignments if '=' in a else ids).append(a)
    return list(dict.fromkeys(ids)), assignments


def cmd_set(args, root):
    ids, assignments = split_targets(args)
    if not assignments:
        print("error: asf set wants FIELD=VALUE after the id(s)", file=sys.stderr)
        return 2
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    # every id is resolved and every assignment parsed before any card is written
    for item_id in ids:
        rec = canonical.get(item_id)
        if rec is None:
            print(f"error: no item {item_id!r}", file=sys.stderr)
            return 2
        try:
            parse_assignments(rec['meta'].get('type'), assignments, canonical)
        except ValueError as e:
            print(f"error: {e}" + (f" ({item_id})" if len(ids) > 1 else ''), file=sys.stderr)
            return 2
    for item_id in ids:
        rc = _set_one(args, root, canonical[item_id], item_id, assignments, canonical)
        if rc:
            return rc
    return 0


def _set_one(args, root, rec, item_id, assignments, canonical=None):
    sets = parse_assignments(rec['meta'].get('type'), assignments, canonical)
    updates = {}
    idempotent = []
    all_noop = True  # PD4: the `set` line is suppressed only when every op was a list-field no-op
    # `after+=<other>` naming a Task that already holds `item_id` in its own after: is the
    # operator flipping a standing overlap hold (B-0037) — collected now, flipped once the write
    # below lands (never on a bare after=[...] or an after-=, only a `+`-added id).
    from asf.feeder import widen
    after_adds = [a for top, sub, op, value in sets if top == 'after' and op == '+'
                 for a in widen.norm_writes(value)]
    for top, sub, op, value in sets:
        if top in LIST_FIELDS.get(rec['meta'].get('type'), ()):
            base = updates[top] if top in updates else rec['meta'].get(top)
            updates[top], changed = apply_list(base, op, value, covers=(top == 'writes'))
            if changed:
                all_noop = False
            if op == '-' and not changed:
                print(f"error: {top}-={' '.join(value)} removes nothing — "
                      + _why_not_removed(top, item_id, base, value), file=sys.stderr)
                return 2
            if op == '+' and not changed:
                idempotent.append((top, value))
            if top == 'writes' and not updates[top]:
                print(f"error: a Task keeps a writes: footprint — {item_id} would have none",
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

    if 'reshape_applied' in updates:
        history, err = reshape_applied(rec, updates, getattr(args, 'why', None))
    else:
        history, err = severity_change(rec, updates, getattr(args, 'why', None))
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    from asf.record.check import product_of  # the same soft loader cmd_check uses (D5, PD6)
    err = set_typed(rec, updates, product=product_of(args), history=[history] if history else ())
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if idempotent:
        from asf.feeder import widen  # function-local like the other two (D4)
        for top, value in idempotent:
            print(f"{item_id}: {top}: already covers {' '.join(widen.norm_writes(value))} — "
                  "footprint unchanged")
    if 'writes' in updates:
        _warn_standing_overlaps(root, item_id, product_of(args))
    if after_adds:
        _flip_reverse_holds(root, item_id, after_adds, product_of(args))
    lists = LIST_FIELDS.get(rec['meta'].get('type'), ())
    if not all_noop:
        print(f"{item_id}: set " + ', '.join(
            f"{k}={' '.join(v)}" if k in lists else k for k, v in updates.items()))
    if history:
        print(f"{item_id}: history: " + history.split('set: ', 1)[1])
    return 0


def _warn_standing_overlaps(root, item_id, product):
    """A ``writes:`` set that was accepted still leaves any overlap the card already had with
    another Active Task; say so (a warning, never a refusal: I3 judges what the write added)."""
    try:
        from asf import invariants
        from asf.feeder import footprint
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
        shared = footprint.shared_globs(product)
        pairs = invariants.unordered_overlaps(
            invariants.overlap_tasks({i: r['meta'] for i, r in canonical.items()}), shared)
    except Exception:  # a warning must never fail the write
        return
    for a, b, *_globs in pairs:
        if item_id in (a, b):
            other = b if a == item_id else a
            print(f"warning: {item_id}: writes: still intersects Active task {other}'s writes: "
                  "(standing before this set; not refused)", file=sys.stderr)


def _flip_reverse_holds(root, item_id, added, product):
    """``asf set <item_id> after+=<other>`` for an ``other`` whose own ``after:`` already holds
    ``item_id`` is the operator flipping a standing overlap hold the wrong way round (B-0037):
    the pair's ``writes:`` still intersect, so leaving both edges would close a cycle and leaving
    only the old one would silently keep the hold on ``item_id`` after being told the opposite.
    Clear ``item_id`` out of ``other``'s ``after:`` so the pair ends up ordered once, in the
    direction just asked for. A warning only: this never refuses the ``after+=`` write itself,
    and a flip that fails to write is reported, not retried. Scoped to a standing ``writes:``
    overlap between the two (:func:`asf.feeder.footprint.overlaps`) — an ``after:`` naming
    something else first is a plan's own ordering, never the invariant's to undo."""
    from asf.feeder import footprint
    try:
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
    except Exception:  # the after+= write already landed; a flip that cannot even read is skipped
        return
    mine = canonical.get(item_id) or {}
    my_writes = _as_list(mine.get('meta', {}).get('writes'))
    shared = footprint.shared_globs(product)
    for other in dict.fromkeys(added):
        orec = canonical.get(other)
        if orec is None:
            continue
        after = list(orec['meta'].get('after') or ())
        if item_id not in after:
            continue
        if not footprint.overlaps(my_writes, _as_list(orec['meta'].get('writes')), shared):
            continue
        kept = [a for a in after if a != item_id]
        err = set_typed(orec, {'after': kept}, product=product)
        if err:
            print(f"warning: {other}: after: -{item_id} (flipping the hold {item_id} after+="
                 f"{other} just reversed) not written — {err}", file=sys.stderr)
        else:
            print(f"{other}: set after={' '.join(kept) if kept else '[]'} — flipped: "
                 f"{item_id} now holds the overlap with {other}")


def set_typed(rec, updates, writer='set', product=None, history=()):
    """Write ``updates`` (typed fields) onto the card ``rec`` (a ``load_items`` record) through the
    parser: rendered on a scratch copy, parsed back, written only when every field round-trips.
    ``history``: lines appended to the card's ``## History`` in the same write (``asf retire``,
    and a Bug's severity move, F-0163) — never a second write afterwards, so the guard that can
    refuse the change sees the whole change, and a card never holds a field whose reason failed
    to land.
    ``product``: passed to the record stage's I3 check, so a ``writes:`` update that only adds a
    path ``product``'s ``conventions.shared_paths`` covers is never refused as intersecting
    another Active Task's footprint. Returns None on success, else the reason the card is
    unchanged."""
    # write to a scratch copy first: the card is replaced only if it parses back to the ask
    fd, scratch = tempfile.mkstemp(suffix='.md')
    os.close(fd)
    try:
        card_writer.write_text(scratch, rec['text'])
        frontmatter.write_typed(scratch, updates)
        with open(scratch, encoding='utf-8') as f:
            new_text = f.read()
        if history:
            from asf.record.ingest import append_history_lines
            try:
                meta, body = frontmatter.parse(new_text, path=rec['relpath'])
            except frontmatter.FrontmatterError:
                pass  # the round-trip below names the error
            else:
                new_text = frontmatter.render(meta, append_history_lines(body, list(history)))
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
    card_writer.write_card(path, text)
