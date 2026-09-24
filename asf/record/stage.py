"""asf.record.stage — the record step, one writer at a time (the contract; W2 and W4 build it).

Today every writer of the record clone (ingest, plan-tasks, plan-order, file-bugs, rollup,
index, the widen pass, groom/set) rewrites cards in place and the tick commits everything at
once (``tick.commit_and_push``), so one bad write refuses every commit after it. The record
step becomes a sequence of *staged* writers:

1. :func:`stage` runs one writer against the clone and captures exactly the paths it changed,
   with their content before it ran (:class:`Staged`).
2. :func:`validate` runs the ``record`` invariants (:mod:`asf.invariants`, I1 per writer, I2
   after ingest's restamp, I3, I10, I11) over that writer's change alone.
3. :func:`refuse` puts back only the offending paths of that writer — to the content they had
   before *it* ran, not to ``HEAD``, so an earlier writer's good output stays.

:func:`run_writers` is the loop. It never raises for a finding: a violation refuses the write,
the rest of the tick commits, and the caller files one Bug per ``(invariant, path)``.

Who builds what: W2 makes each writer a ``fn(root, …)`` that writes only the clone (no commit
of its own) and names itself in :data:`WRITERS`; W4 implements this module and calls
:func:`run_writers` from ``tick.run_step0`` before ``commit_and_push``.
"""
from dataclasses import dataclass, field

#: The record writers in the order the tick runs them. A writer not listed here still runs,
#: staged under its own name.
WRITERS = ('backfill', 'ingest', 'plan-tasks', 'plan-order', 'file-bugs', 'rollup', 'index',
           'widen', 'set', 'groom')


@dataclass
class Staged:
    """One writer's change to the record clone: ``paths`` (record-relative, sorted) it added,
    modified or deleted, and ``before`` — ``{path: text, or None for a path it created}`` — the
    content each had before this writer ran."""
    writer: str
    paths: tuple = field(default_factory=tuple)
    before: dict = field(default_factory=dict)


@dataclass
class RecordContext:
    """What a ``record``-scope invariant's ``check(ctx)`` reads: the clone ``root``, the
    :class:`Staged` change under test, and the product."""
    root: str
    staged: Staged
    product: object = None


def stage(root, writer, fn, *args, **kwargs):
    """Run ``fn(root, *args, **kwargs)`` — one writer — against the record clone at ``root`` and
    return the :class:`Staged` change it made (the working tree compared before and after; the
    index is not touched). A writer that raises leaves its partial change in place and the
    exception propagates, as today."""
    raise NotImplementedError('stage.stage: W4')


def validate(root, staged, product=None):
    """The ``record`` invariants over one writer's change: ``invariants.run(RecordContext(root,
    staged, product), scope='record')``. Each finding's ``paths`` is a subset of
    ``staged.paths``. ``[]`` when the write is sound."""
    raise NotImplementedError('stage.validate: W4')


def refuse(root, staged, findings):
    """Put the paths the ``findings`` name back to ``staged.before`` (delete a path the writer
    created). Returns the sorted tuple of paths restored. Paths of the same writer that no
    finding names are kept."""
    raise NotImplementedError('stage.refuse: W4')


def run_writers(root, writers, product=None):
    """The record step: for each ``(name, fn)`` in ``writers``, :func:`stage` →
    :func:`validate` → :func:`refuse` the offending paths. Returns ``(staged, findings)`` — every
    writer's :class:`Staged` (after refusal) and every finding, in order. Never raises for a
    finding; the caller commits what survived and files the Bugs."""
    raise NotImplementedError('stage.run_writers: W4')
