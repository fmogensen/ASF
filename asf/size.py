"""asf.size — the footprint decides the size (F-0041 §2.1).

A Task's cost is not its diff; it is a branch, a pull request, a CI run and a review round,
charged the same whether the footprint is two files or fifteen. A **size class** derives that
cost from the Task's own ``writes:`` footprint alone, mechanically: three class names
(:data:`SMALL`, :data:`MEDIUM`, :data:`LARGE`) and five kind names (:data:`KINDS`), in the fixed
four-step order :func:`classify` documents, so a blocker always beats a small count.

Pure functions over plain lists: no filesystem, no ``Product``, no path literal of its own —
this module's neighbour in shape is :mod:`asf.feeder.footprint`, whose one footprint-matching
rule, :func:`~asf.feeder.footprint.globs_overlap`, it borrows rather than reimplements, so an
operator who writes ``writes:`` and an operator who writes ``kind_paths:`` are writing the same
kind of glob.
"""
from dataclasses import dataclass, field

from asf.feeder.footprint import WILDCARDS, globs_overlap

SMALL, MEDIUM, LARGE = 'small', 'medium', 'large'
CLASSES = (SMALL, MEDIUM, LARGE)
#: The kinds a footprint can be, most specific first; ``code`` is the fallback (D8).
KINDS = ('docs', 'test', 'config', 'copy', 'code')


@dataclass(frozen=True)
class SizeConfig:
    """The resolved threshold table :func:`classify` takes. Every field defaulted, so this
    module is usable with no product at all — :meth:`asf.conventions.Conventions.size_config`
    builds the resolved one."""

    small_max_files: int = 3
    small_max_files_by_kind: dict = field(default_factory=lambda: {'docs': 15, 'copy': 15})
    medium_max_files: int = 15
    kind_globs: dict = field(default_factory=dict)
    never_small_globs: tuple = ()


def kind_of(writes, kind_globs):
    """The one kind every entry of ``writes`` matches, else ``'code'`` — the safe class (D8).
    ``kind_globs`` is ``{kind: [glob, …]}``; matching is
    :func:`asf.feeder.footprint.globs_overlap`, the repo's one footprint-matching rule."""
    if not writes:
        return 'code'
    for kind in KINDS:
        if kind == 'code':
            continue
        globs = kind_globs.get(kind)
        if not globs:
            continue
        if all(any(globs_overlap(w, g) for g in globs) for w in writes):
            return kind
    return 'code'


def countable(writes):
    """``(n, blocker)``: how many files the footprint names, or ``(None, why)`` when it cannot
    be counted — a wildcard entry (D1), a cross-repo entry (``record:features/**``), or an
    empty footprint (D6)."""
    if not writes:
        return None, 'no writes: footprint'
    for w in writes:
        if ':' in w:
            return None, f"'{w}' names another repository"
        if any(ch in w for ch in WILDCARDS):
            return None, f"'{w}' is a glob, not a file"
    return len(writes), None


def classify(writes, cfg):
    """``(class, kind, why)``. ``cfg`` is the resolved :class:`SizeConfig`:
    ``small_max_files``, ``small_max_files_by_kind``, ``medium_max_files``, ``kind_globs``,
    ``never_small_globs``. ``why`` is one sentence naming what decided it — the count and the
    threshold, or the blocker and the entry that matched it — and it is what the card, the
    brief and ``asf train status`` all print."""
    floor_why = None
    for w in writes or ():
        if any(globs_overlap(w, g) for g in cfg.never_small_globs):
            floor_why = f"'{w}' matches never_small_paths"
            break

    kind = kind_of(writes, cfg.kind_globs)
    n, blocker = countable(writes)
    if n is None:
        return LARGE, kind, blocker

    threshold = cfg.small_max_files_by_kind.get(kind, cfg.small_max_files)
    if n <= threshold:
        cls, why = SMALL, f'{n} files <= {threshold} ({kind})'
    elif n <= cfg.medium_max_files:
        cls, why = MEDIUM, f'{n} files <= {cfg.medium_max_files}'
    else:
        cls, why = LARGE, f'{n} files > {cfg.medium_max_files}'

    if floor_why is not None and CLASSES.index(cls) < CLASSES.index(MEDIUM):
        return MEDIUM, kind, floor_why
    return cls, kind, why
