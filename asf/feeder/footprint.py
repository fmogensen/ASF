"""asf.feeder.footprint — the ``writes:`` gate: two Tasks whose footprints overlap never run together.

A footprint is a list of path globs (``apps/web/**``, ``docs/x.md``). Two footprints overlap when
any glob of one matches any glob of the other, in either direction (the same test ``asf check``
applies to two Active Tasks), or when one names a directory (``dir/``) the other sits under.
Pure functions over plain lists — no filesystem.
"""
from asf.record.core import writes_intersect

WILDCARDS = '*?['


def _literal_head(glob):
    """The part of a glob before its first wildcard: ``apps/web/**`` → ``apps/web/``."""
    for i, ch in enumerate(glob):
        if ch in WILDCARDS:
            return glob[:i]
    return glob


def globs_overlap(a, b):
    if writes_intersect(a, b):  # asf check's own test, then the gate's wider reach
        return True
    ha, hb = _literal_head(a), _literal_head(b)
    # a bare directory (`apps/web/`) covers everything beneath it
    if a.endswith('/') and b.startswith(a) or b.endswith('/') and a.startswith(b):
        return True
    # two globs, each open-ended, whose literal heads nest (`apps/web/**` vs `apps/web/*.ts`):
    # they may name a common file — the gate errs on waiting, a false overlap costs one tick
    return ha != a and hb != b and (ha.startswith(hb) or hb.startswith(ha))


def overlaps(writes_a, writes_b):
    """The first (glob_a, glob_b) pair that overlaps, or None."""
    for a in writes_a or []:
        for b in writes_b or []:
            if globs_overlap(a, b):
                return a, b
    return None


def first_conflict(writes, running):
    """The id of the first running Task whose footprint overlaps ``writes``, or None.

    ``running`` is an ordered list of ``(task_id, writes)``.
    """
    for tid, other in running:
        if overlaps(writes, other):
            return tid
    return None
