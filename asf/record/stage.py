"""asf.record.stage — the record step, one writer at a time.

Every writer of the record clone (ingest, plan-tasks, plan-order, file-bugs, rollup, index, the
widen pass, groom/set) rewrites cards in place, and the tick commits everything at once
(``tick.commit_and_push``), so one bad write used to refuse every commit after it. The record
step is a sequence of *staged* writers:

1. :func:`stage` runs one writer against the clone and captures exactly the paths it changed,
   with their content before it ran (:class:`Staged`).
2. :func:`validate` runs the ``record`` invariants (:mod:`asf.invariants`, I1 per writer, I2
   after ingest's restamp, I3, I10, I11) over that writer's change alone.
3. :func:`refuse` puts back only the offending paths of that writer — to the content they had
   before *it* ran, not to ``HEAD``, so an earlier writer's good output stays.

:func:`run_writers` is the loop, and :func:`guarded` is one turn of it for a writer that guards
itself (``asf ingest``, the plan-task minter, ``asf set`` / the widen pass through
:func:`asf.record.setfield.set_typed`). Neither raises for a finding: a violation refuses the
write, the rest commits, and every finding is kept in :data:`REFUSED` until the tick drains it
(:func:`drain`) to file one Bug per ``(invariant, path)``.
"""
import os
from dataclasses import dataclass, field, replace

#: The record writers in the order the tick runs them. A writer not listed here still runs,
#: staged under its own name.
WRITERS = ('backfill', 'ingest', 'plan-tasks', 'plan-order', 'file-bugs', 'rollup', 'index',
           'widen', 'set', 'groom', 'reopen')

#: Every finding a staged writer was refused on, in order, since the last :func:`drain`.
REFUSED = []

#: Never staged: git's own directory.
SKIP_DIRS = ('.git',)


@dataclass
class Staged:
    """One writer's change to the record clone: ``paths`` (record-relative, sorted) it added,
    modified or deleted, and ``before`` — ``{path: text, or None for a path it created}`` — the
    content each had before this writer ran. After :func:`refuse`, ``refused`` names the paths
    put back and ``paths`` only what was kept."""
    writer: str
    paths: tuple = field(default_factory=tuple)
    before: dict = field(default_factory=dict)
    refused: tuple = field(default_factory=tuple)


@dataclass
class RecordContext:
    """What a ``record``-scope invariant's ``check(ctx) -> [Finding]`` reads: the clone
    ``root``, the :class:`Staged` change under test, and the product. ``cache`` is shared by the
    checks of one :func:`validate` (the record is loaded once)."""
    root: str
    staged: Staged
    product: object = None
    cache: dict = field(default_factory=dict)


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8', 'surrogateescape')
    except (OSError, IsADirectoryError):
        return None


def snapshot(root, only=None):
    """``{relpath: text}`` of every file under ``root`` (``.git`` excluded), or of the paths in
    ``only`` (a missing one is absent)."""
    out = {}
    if only is not None:
        for rel in only:
            text = _read(os.path.join(root, rel))
            if text is not None:
                out[rel] = text
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            text = _read(full)
            if text is not None:
                out[os.path.relpath(full, root).replace(os.sep, '/')] = text
    return out


def _diff(writer, before, after):
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    return Staged(writer, tuple(changed), {p: before.get(p) for p in changed})


def stage(root, writer, fn, *args, **kwargs):
    """Run ``fn(root, *args, **kwargs)`` — one writer — against the record clone at ``root`` and
    return the :class:`Staged` change it made (the working tree compared before and after; the
    index is not touched). A writer that raises leaves its partial change in place and the
    exception propagates, as today."""
    staged, _result = stage_only(root, writer, fn, args, kwargs)
    return staged


def stage_only(root, writer, fn, args=(), kwargs=None, only=None):
    """:func:`stage`, returning ``(staged, fn's result)``; ``only`` limits the comparison to
    those record-relative paths (a writer known to touch one card)."""
    before = snapshot(root, only)
    result = fn(root, *args, **(kwargs or {}))
    return _diff(writer, before, snapshot(root, only)), result


def validate(root, staged, product=None):
    """The ``record`` invariants over one writer's change: ``invariants.run(RecordContext(root,
    staged, product), scope='record')``. Each finding's ``paths`` is a subset of
    ``staged.paths``. ``[]`` when the write is sound."""
    from asf import invariants
    if not staged.paths:
        return []
    ctx = RecordContext(root, staged, product)
    keep = set(staged.paths)
    out = []
    for f in invariants.run(ctx, scope='record'):
        paths = tuple(p for p in f.paths if p in keep)
        if paths:
            out.append(replace(f, paths=paths) if paths != tuple(f.paths) else f)
    return out


def refuse(root, staged, findings):
    """Put the paths the ``findings`` name back to ``staged.before`` (delete a path the writer
    created). Returns the sorted tuple of paths restored. Paths of the same writer that no
    finding names are kept."""
    named = sorted({p for f in findings or () for p in f.paths if p in staged.before})
    for rel in named:
        full = os.path.join(root, rel)
        text = staged.before[rel]
        if text is None:
            if os.path.exists(full):
                os.remove(full)
            continue
        os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
        with open(full, 'wb') as f:
            f.write(text.encode('utf-8', 'surrogateescape'))
    return tuple(named)


def _after_refusal(staged, restored):
    if not restored:
        return staged
    return replace(staged, paths=tuple(p for p in staged.paths if p not in restored),
                   refused=tuple(sorted(set(staged.refused) | set(restored))))


def run_writers(root, writers, product=None):
    """The record step: for each ``(name, fn)`` in ``writers``, :func:`stage` →
    :func:`validate` → :func:`refuse` the offending paths. Returns ``(staged, findings)`` — every
    writer's :class:`Staged` (after refusal) and every finding, in order. Never raises for a
    finding; the caller commits what survived and files the Bugs."""
    all_staged, all_findings = [], []
    for name, fn in writers:
        staged = stage(root, name, fn)
        findings = validate(root, staged, product)
        restored = refuse(root, staged, findings)
        all_staged.append(_after_refusal(staged, restored))
        all_findings.extend(findings)
    return all_staged, all_findings


def guarded(root, writer, fn, args=(), kwargs=None, product=None, only=None, out=None):
    """One writer through the stage: run it, validate its change, put back what an invariant
    refuses. Returns ``(result, staged, findings)``. Each finding is printed as ``INVARIANT <id>:
    <writer> refused <path> — <message>`` and kept in :data:`REFUSED` for the tick's Bug filer."""
    staged, result = stage_only(root, writer, fn, args, kwargs, only)
    findings = validate(root, staged, product)
    restored = refuse(root, staged, findings)
    if findings:
        say = out or print
        for f in findings:
            say(f'INVARIANT {f.invariant}: {writer} refused {", ".join(f.paths)} — {f.message}')
        REFUSED.extend(findings)
    return result, _after_refusal(staged, restored), findings


def drain():
    """Every finding :data:`REFUSED` holds, emptied — what the tick files one Bug per
    ``(invariant, path)`` from."""
    out = list(REFUSED)
    del REFUSED[:]
    return out
