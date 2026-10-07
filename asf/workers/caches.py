"""asf.workers.caches — the build cache the worktrees share, bounded by two numbers.

A product's ``worker_env`` points a build tool's cache at one directory per product under the
state dir, so an unchanged package another session already built is a cache hit
(``TURBO_CACHE_DIR`` by default, ``conventions.cache_prune.env_vars``). Every session writes
into it; nothing read it back out, so it grew without bound (F-0214).

One pass in the tick's health step, in the shape :mod:`asf.workers.retention` and
:mod:`asf.workers.worktrees` already have:

* the directories come from the *resolved* worker environment (:func:`asf.env.worker_env`) — the
  path is the operator's and appears nowhere in this package — and one that is not an absolute
  path, or that resolves outside the product's own state dir, is named and never touched;
* an **entry** is the direct children of the directory that share a stem (an archive and its
  metadata sidecar are one entry, removed together), its size the sum of their sizes and its age
  ``now - max(atime, mtime)`` — a cache *hit* reads the archive without rewriting it, so mtime
  alone would age the hottest entries as if they were never used;
* entries older than ``max_age_days`` go; then, while the directory is over ``max_size_gb``, the
  oldest go until it fits — but never one used inside ``min_age_min``, and never more than
  ``per_pass`` of them;
* the pass prints one line, and records the sizes and counts in :data:`STATE_FILE` for
  ``asf doctor``'s ``caches`` row and ``asf status``'s ``Caches`` row, which measure nothing.

Files only: a child that is a directory is counted and skipped, never recursed into and never
removed as a whole tree — this pass has an unlink in it, and the state dir it runs inside holds
the worktrees.
"""
import dataclasses
import json
import os
import time

from asf import env
from asf.workers import spawn as spawn_mod

STATE_FILE = 'caches.json'
GB = 1024 ** 3
MINUTE_S = 60


def _gb(n_bytes):
    return f'{n_bytes / GB:.1f} GB'


@dataclasses.dataclass
class Entry:
    stem: str
    paths: list                  # the sibling files this entry is made of
    bytes: int
    used: float                  # max(atime, mtime) over them


def stem_of(name):
    """The entry a child belongs to: its name up to the first ``.``, one trailing ``-meta``
    removed — ``h.tar.zst`` and ``h-meta.json`` are one entry (C5)."""
    stem = name.split('.', 1)[0]
    return stem[:-len('-meta')] if stem.endswith('-meta') else stem


def cache_dirs(product, cfg=None, conv=None):
    """``[(var, path, problem)]`` — the cache directories this product's sessions share, from
    ``conventions.cache_prune.env_vars`` against :func:`asf.env.worker_env`. ``problem`` is ''
    when the path may be pruned (or is simply absent — PD3), else why it may not: not an
    absolute path (C3), outside the product's state dir (C2), or not a directory."""
    conv = conv if conv is not None else product.conventions
    resolved = env.worker_env(cfg if cfg is not None else spawn_mod.load_cfg(), product)
    root = os.path.realpath(env.state_dir(product))
    out = []
    for var in conv.pruning('env_vars'):
        value = resolved.get(var)
        if value is None:
            continue
        if not os.path.isabs(value):
            out.append((var, value, 'is not an absolute path'))
            continue
        real = os.path.realpath(value)
        if real == root or not real.startswith(root + os.sep):
            out.append((var, real, "is outside the product's state dir"))
            continue
        if os.path.exists(real) and not os.path.isdir(real):
            out.append((var, real, 'is not a directory'))
            continue
        out.append((var, real, ''))
    return out


def entries(path):
    """``([Entry] oldest first, total bytes, directories skipped)`` from one ``os.scandir`` and
    one ``stat`` per child; a child that vanished between the two is skipped."""
    groups = {}
    skipped_dirs = 0
    with os.scandir(path) as it:
        children = list(it)
    for child in children:
        try:
            if child.is_dir(follow_symlinks=False):
                skipped_dirs += 1
                continue
            st = child.stat(follow_symlinks=False)
        except OSError:
            continue
        stem = stem_of(child.name)
        group = groups.setdefault(stem, {'paths': [], 'bytes': 0, 'used': 0.0})
        group['paths'].append(child.path)
        # the entry's size is governed by its largest member — a metadata sidecar is bytes too,
        # but negligible beside the archive it describes, and this stays extension-agnostic (P16)
        group['bytes'] = max(group['bytes'], st.st_size)
        group['used'] = max(group['used'], st.st_atime, st.st_mtime)
    rows = [Entry(stem=stem, paths=g['paths'], bytes=g['bytes'], used=g['used'])
            for stem, g in groups.items()]
    rows.sort(key=lambda e: e.used)
    total = sum(e.bytes for e in rows)
    return rows, total, skipped_dirs


def plan(rows, total, max_age_s, cap_bytes, min_age_s, per_pass, now):
    """``([Entry] to remove, {'age': n, 'cap': n}, left)``: every entry past ``max_age_s``, then
    the oldest remaining while ``total`` less what is already doomed is over ``cap_bytes`` —
    never an entry used inside ``min_age_s``, at most ``per_pass``, the rest counted in
    ``left``. Pure: nothing is touched."""
    doomed = []
    doomed_bytes = 0
    remaining = []
    for e in rows:          # rows is oldest first; doomed stays oldest first too
        age = now - e.used
        if max_age_s and age > max_age_s and age >= min_age_s:
            doomed.append(('age', e))
            doomed_bytes += e.bytes
        else:
            remaining.append(e)
    if cap_bytes:
        for e in remaining:
            age = now - e.used
            if total - doomed_bytes > cap_bytes and age >= min_age_s:
                doomed.append(('cap', e))
                doomed_bytes += e.bytes
    left = max(0, len(doomed) - per_pass)
    kept = doomed[:per_pass]
    reasons = {'age': 0, 'cap': 0}
    for reason, _e in kept:
        reasons[reason] += 1
    return [e for _reason, e in kept], reasons, left


def prune_dir(path, conv, now=None, fix=True):
    """One directory: :func:`entries`, :func:`plan`, then (``fix``) each doomed entry's files
    unlinked. The dict the state file carries — ``bytes``, ``entries``, ``pruned``,
    ``pruned_bytes``, ``by_reason``, ``left``, ``failed``, ``skipped_dirs`` — plus the two rule
    numbers :func:`line` renders (``max_age_days``, ``cap_bytes``)."""
    now = now if now is not None else time.time()
    rows, total, skipped_dirs = entries(path)
    max_age_days = conv.pruning('max_age_days')
    cap_bytes = int(conv.pruning('max_size_gb') * GB)
    base = {'skipped_dirs': skipped_dirs, 'max_age_days': max_age_days, 'cap_bytes': cap_bytes}
    if not fix:  # a dry pass measures and removes nothing (PD5): the raw totals, untouched
        return {**base, 'bytes': total, 'entries': len(rows), 'pruned': 0, 'pruned_bytes': 0,
                'by_reason': {}, 'left': 0, 'failed': []}
    max_age_s = max_age_days * 86400
    min_age_s = conv.pruning('min_age_min') * MINUTE_S
    per_pass = conv.pruning('per_pass')
    doomed, reasons, left = plan(rows, total, max_age_s, cap_bytes, min_age_s, per_pass, now)
    pruned_bytes = 0
    failed = []
    for e in doomed:
        for p in e.paths:
            try:
                os.unlink(p)
            except OSError:
                failed.append(p)
        pruned_bytes += e.bytes
    return {**base, 'bytes': total - pruned_bytes, 'entries': len(rows) - len(doomed),
            'pruned': len(doomed), 'pruned_bytes': pruned_bytes,
            'by_reason': {k: v for k, v in reasons.items() if v}, 'left': left, 'failed': failed}


def line(var, data, rate=None):
    """The pass's one line for one directory (C10)."""
    if data.get('problem'):
        return f"caches: {var} is not pruned — {data['problem']}"
    if data.get('absent'):
        return f'caches: {var} — nothing there yet'
    head = f"caches: {var} {_gb(data['bytes'])} in {data['entries']} entries"
    by_reason = data.get('by_reason') or {}
    pruned = data.get('pruned') or 0
    if pruned:
        clauses = []
        if by_reason.get('age'):
            clauses.append(f"{by_reason['age']} over {data.get('max_age_days')}d")
        if by_reason.get('cap'):
            clauses.append(f"{by_reason['cap']} over the {_gb(data.get('cap_bytes', 0))} cap")
        verdict = f"pruned {pruned} ({_gb(data.get('pruned_bytes', 0))}): {', '.join(clauses)}"
    else:
        rules = []
        if data.get('max_age_days'):
            rules.append(f"{data['max_age_days']}d")
        if data.get('cap_bytes'):
            rules.append(f"{_gb(data['cap_bytes'])} cap")
        verdict = f"nothing to prune ({', '.join(rules)})" if rules else 'nothing to prune'
    tail = f"; {data['left']} left for the next pass" if data.get('left') else ''
    rate_tail = f'; {rate}' if rate else ''
    return f'{head} — {verdict}{tail}{rate_tail}'


def prune(product, cfg=None, conv=None, fix=True, out=print, now=None):
    """One pass over every directory :func:`cache_dirs` names: prune it, print its line, and
    write the state the two rows read. Returns the state dict; writes nothing and prints nothing
    for a product that configures no cache directory."""
    conv = conv if conv is not None else product.conventions
    now = now if now is not None else time.time()
    dirs = cache_dirs(product, cfg=cfg, conv=conv)
    if not dirs:
        return None
    results = []
    for var, path, problem in dirs:
        if problem:
            data = {'var': var, 'path': path, 'problem': problem, 'pruned': 0}
        elif not os.path.exists(path):  # PD3: configured but not there yet — absent, not red
            data = {'var': var, 'path': path, 'absent': True, 'bytes': 0, 'entries': 0}
        else:
            data = {'var': var, 'path': path, **prune_dir(path, conv, now=now, fix=fix)}
        results.append(data)
        out(line(var, data))
    state = {'at': now, 'dirs': results, 'summaries': None}
    write_state(product, state)
    return state


def write_state(product, data):
    path = os.path.join(env.state_dir(product), STATE_FILE)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_state(product):
    try:
        with open(os.path.join(env.state_dir(product), STATE_FILE), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def doctor_line(product):
    """``(ok, detail)`` off the state file — the doctor's ``caches`` row, once a later card
    fills its body; before that, and before any pass has run, None. Measures nothing itself."""
    read_state(product)
    return None


def status_line(product):
    """The size and the hit rate for the status table, once a later card fills its body; before
    that, None. Measures nothing itself."""
    read_state(product)
    return None
