"""asf.feeder.footprint — the ``writes:`` gate: two Tasks whose footprints overlap never run together.

A footprint is a list of path globs (``apps/web/**``, ``docs/x.md``). Two footprints overlap when
any glob of one matches any glob of the other, in either direction (the same test ``asf check``
applies to two Active Tasks), or when one names a directory (``dir/``) the other sits under.
Pure functions over plain lists — no filesystem.

``conventions.shared_paths`` (lockfiles) are no one's footprint: a glob under one never makes two
Tasks overlap here; the lane serialises them at merge instead (one per tick).
``conventions.shared_writes`` (append-only registries every Task may add a row to) are no one's
footprint either, and more: they sit inside every Task's ``writes:`` (:func:`shared_writes`), so
touching one is never a widening.
"""
import fnmatch

from asf.record.core import writes_intersect

WILDCARDS = '*?['


def _literal_head(glob):
    """The part of a glob before its first wildcard: ``apps/web/**`` → ``apps/web/``."""
    for i, ch in enumerate(glob):
        if ch in WILDCARDS:
            return glob[:i]
    return glob


def _literal_tail(glob):
    """The part of a glob after its last wildcard: ``migrations/*_a.sql`` → ``_a.sql``."""
    for i in range(len(glob) - 1, -1, -1):
        if glob[i] in WILDCARDS or glob[i] == ']':
            return glob[i + 1:]
    return glob


def _tails_disjoint(a, b):
    """True when no path can match both globs because their literal tails differ: every path a
    glob matches ends with its tail, so one tail must end the other. Undecided (False) for ``**``
    and character classes — those stay a conservative overlap."""
    if '**' in a or '**' in b or '[' in a or '[' in b:
        return False
    ta, tb = _literal_tail(a), _literal_tail(b)
    return not (ta.endswith(tb) or tb.endswith(ta))


def globs_overlap(a, b):
    if writes_intersect(a, b):  # asf check's own test, then the gate's wider reach
        return True
    ha, hb = _literal_head(a), _literal_head(b)
    # a bare directory (`apps/web/`) covers everything beneath it
    if a.endswith('/') and b.startswith(a) or b.endswith('/') and a.startswith(b):
        return True
    # two globs, each open-ended, whose literal heads nest (`apps/web/**` vs `apps/web/*.ts`):
    # they may name a common file — the gate errs on waiting, a false overlap costs one tick
    # — unless their literal tails rule a common file out (`migrations/*_a.sql` vs `*_b.sql`)
    return (ha != a and hb != b and (ha.startswith(hb) or hb.startswith(ha))
            and not _tails_disjoint(a, b))


def shared_globs(product):
    """``conventions.shared_paths`` and ``shared_writes`` for ``product`` — a Product, a
    Conventions, or the plain ``conventions:`` mapping — as a tuple; ``()`` for None and for a product that declares none.
    The one reader of the key: the feeder, the widening rule, ``asf check``, I3, the groomer and
    the lane all ask here, so a product cannot be exempt in one of them and not the others."""
    if product is None:
        return ()
    conv = getattr(product, 'conventions', product)
    return tuple(dict.fromkeys(p for p in [*(conv.get('shared_paths') or ()),
                                           *(conv.get('shared_writes') or ())] if p))


def shared_writes(product):
    """``conventions.shared_writes`` for ``product`` (a Product, a Conventions, or the plain
    ``conventions:`` mapping) as a tuple: the append-only files every Task's ``writes:`` holds
    without declaring them — no widening, no overlap. ``()`` when the product declares none."""
    if product is None:
        return ()
    conv = getattr(product, 'conventions', product)
    return tuple(p for p in (conv.get('shared_writes') or ()) if p)


def is_shared(glob, shared):
    """True when the ``shared_paths`` set covers ``glob`` — ``uv.lock`` against ``uv.lock``, and
    ``apps/web/package-lock.json`` against ``**/package-lock.json``.

    One direction only: a glob *wider* than the shared set is never shared. Matched both ways,
    ``*`` would be covered by any declared lockfile (``fnmatch('uv.lock', '*')``), and a Task
    declaring ``writes: ['*']`` would overlap nothing at all — the footprint gate off for the
    widest footprint there is (D2)."""
    return any(fnmatch.fnmatch(glob, s) for s in shared or ())


def overlaps(writes_a, writes_b, shared=()):
    """The first (glob_a, glob_b) pair that overlaps, or None. A glob naming a ``shared`` path
    (``conventions.shared_paths``) overlaps nothing."""
    for a in writes_a or []:
        if shared and is_shared(a, shared):
            continue
        for b in writes_b or []:
            if shared and is_shared(b, shared):
                continue
            if globs_overlap(a, b):
                return a, b
    return None


def first_conflict(writes, running, shared=()):
    """The id of the first running Task whose footprint overlaps ``writes``, or None.

    ``running`` is an ordered list of ``(task_id, writes)``; ``shared``: the product's
    ``shared_paths``, left out of the overlap.
    """
    for tid, other in running:
        if overlaps(writes, other, shared):
            return tid
    return None
