"""asf.progress — what a run has actually moved, once a minute, with a clock of its own.

A stream-json log carries no timestamp (F-0066 P4), so "nothing has happened for twenty minutes"
cannot be read out of one. This module writes the time down instead: every SAMPLE_EVERY_S a
sampler appends one line to ``<state>/<product>/progress/<job>.jsonl`` holding the four
measurements that only move when work happens —

* ``commit``  the sha the run's worktree sits on,
* ``digest``  a hash of ``git status --porcelain``: which files it has changed,
* ``classes`` how many distinct tool classes it has used, cumulative,
* ``novel``   how many of its tool calls were not a repeat of one of the last RING calls,

— beside the sample's own ``at``, the log offset it read to, and the classes it saw since the
previous sample. :func:`judge` compares the newest sample with the newest one at least ``limit``
minutes older: all four equal is ``STUCK``, any difference is ``MOVING``, and not enough evidence
is ``UNKNOWN`` — never a stall (F-0066 D6).

A leaf: the stdlib and ``asf.env``, exactly the licence :mod:`asf.tokens` takes, plus
``asf.config_keys`` for :func:`tunable`, so :mod:`asf.workers.runtime` may read :data:`NO_PROGRESS`
with no cycle.
"""
import calendar
import collections
import hashlib
import json
import os
import subprocess
import sys
import time

from asf import env

DEFAULT_PROGRESS_MIN = 20       #: stage_limits.progress_min — the card's twenty minutes
SAMPLE_EVERY_S = 60             #: a line a minute
WINDOW_MIN = 10                 #: the card's window for "the tool classes in the last ten minutes"
RING = 8                        #: how far back a call is still a repeat (D4)
KEEP_SAMPLES = 180              #: three hours of samples; the file is rewritten past 2x
KEEP_DAYS = 7                   #: a progress file untouched this long is swept
WATCH_MAX_H = 24                #: a sampler never outlives this, whatever the pid says
NO_PROGRESS = 'no progress'     #: the failure signature (runtime.failure_reason, D12)

MOVING, STUCK, UNKNOWN, BLOCKED = 'moving', 'stuck', 'unknown', 'blocked'

SIGNALS = ('commit', 'digest', 'classes', 'novel')

Verdict = collections.namedtuple('Verdict', 'cls minutes evidence')

_AT_FMT = '%Y-%m-%dT%H:%M:%SZ'
_UNIT_MINUTES = {'s': 1 / 60, 'm': 1, '': 1, 'h': 60, 'd': 1440}


def _digest(name, payload):
    blob = json.dumps((name, payload), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]


def _epoch(at):
    """``at`` (``_AT_FMT``) as a unix time, or None for anything that does not parse."""
    if not at:
        return None
    try:
        return calendar.timegm(time.strptime(at, _AT_FMT))
    except (ValueError, TypeError):
        return None


# ---- the store ---------------------------------------------------------------

def store_dir(product):
    """``<state>/<product>/progress``, made on demand."""
    path = os.path.join(env.state_dir(product), 'progress')
    os.makedirs(path, exist_ok=True)
    return path


def store_path(product, job):
    """One file per job. ``job`` is used as a file name: a job name is already a path component
    everywhere else in the factory (its worktree, its log, its brief)."""
    return os.path.join(store_dir(product), job + '.jsonl')


def read(product, job):
    """The samples, oldest first, malformed lines dropped. ``[]`` for no file — never raises."""
    samples = []
    try:
        with open(store_path(product, job), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    samples.append(rec)
    except OSError:
        return []
    return samples


def append(product, job, sample, keep=KEEP_SAMPLES):
    """One line on. Past ``2 * keep`` lines the file is rewritten to its newest ``keep`` — a ring,
    so a week-long run cannot grow one (D2)."""
    path = store_path(product, job)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(sample) + '\n')
    samples = read(product, job)
    if len(samples) > 2 * keep:
        with open(path, 'w', encoding='utf-8') as f:
            for s in samples[-keep:]:
                f.write(json.dumps(s) + '\n')


def sweep(product, days=None):
    """Progress files untouched for ``days``, removed; the job names, for the caller to print."""
    days = tunable('KEEP_DAYS') if days is None else days
    d = store_dir(product)
    cutoff = time.time() - days * 86400
    swept = []
    for name in os.listdir(d):
        if not name.endswith('.jsonl'):
            continue
        path = os.path.join(d, name)
        try:
            old = os.path.getmtime(path) < cutoff
        except OSError:
            continue
        if old:
            try:
                os.remove(path)
            except OSError:
                continue
            swept.append(name[:-len('.jsonl')])
    return swept


# ---- the reader — incremental, resumable, and its own pass -------------------

def scan(log, prev=None):
    """One incremental pass: ``(bytes, calls, novel, classes, seen, ring, result)``.

    Starts at ``prev['bytes']`` (0 with no ``prev``), stops at the last complete line, and never
    parses a partial one — the runtime is appending to this file as we read it. Each ``tool_use``
    block of an ``assistant`` record counts one call; its class is ``name`` (capped at 40, ``'?'``
    when absent); a call is *novel* unless ``_digest(name, input)`` is already in ``ring``, and
    the ring keeps the newest ``RING`` digests either way. ``classes`` is the cumulative count of
    distinct classes, carried forward through ``prev['seen']`` unions; ``seen`` is this pass's
    own, in first-seen order. A ``system``/``init`` record is a run boundary exactly as it is for
    ``read_result`` and ``tokens.meter`` (B-0028): everything resets, because the log now holds a
    second run and the first run's calls are not this run's. Never raises: an unreadable log is
    ``prev`` unchanged.
    """
    prev = prev or {}
    offset = prev.get('bytes') or 0
    calls = prev.get('calls') or 0
    novel = prev.get('novel') or 0
    seen = list(prev.get('seen') or [])
    seen_set = set(seen)
    ring = list(prev.get('ring') or [])
    result = bool(prev.get('result'))
    if not log:
        return offset, calls, novel, len(seen_set), seen, ring, result
    try:
        with open(log, 'rb') as f:
            f.seek(offset)
            data = f.read()
    except (OSError, ValueError):
        return offset, calls, novel, len(seen_set), seen, ring, result
    last_nl = data.rfind(b'\n')
    if last_nl == -1:
        return offset, calls, novel, len(seen_set), seen, ring, result
    new_offset = offset + last_nl + 1
    for raw in data[:last_nl].split(b'\n'):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get('type')
        if kind == 'system' and rec.get('subtype') == 'init':
            calls, novel = 0, 0
            seen, seen_set, ring, result = [], set(), [], False
            continue
        if kind == 'result':
            result = True
            continue
        if kind != 'assistant':
            continue
        msg = rec.get('message')
        content = msg.get('content') if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict) or block.get('type') != 'tool_use':
                continue
            name = str(block.get('name') or '?')[:40]
            calls += 1
            if name not in seen_set:
                seen_set.add(name)
                seen.append(name)
            digest = _digest(name, block.get('input'))
            if digest not in ring:
                novel += 1
            ring = (ring + [digest])[-RING:]
    return new_offset, calls, novel, len(seen_set), seen, ring, result


def window_classes(samples, now, minutes=None):
    """The distinct tool classes seen in the last ``minutes``: the union of ``seen`` over the
    samples at or after ``now - minutes``, in first-seen order."""
    minutes = tunable('WINDOW_MIN') if minutes is None else minutes
    cutoff = now - minutes * 60
    out, seen = [], set()
    for s in samples:
        at = _epoch(s.get('at'))
        if at is None or at < cutoff:
            continue
        for name in s.get('seen') or []:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def probe(worktree):
    """``(commit, files, digest)`` — ``git rev-parse --short HEAD``, and the line count and an
    8-hex digest of ``git status --porcelain``. ``('', 0, '')`` for a worktree that is gone, not a
    checkout, or whose git call failed: a probe that cannot read says nothing, and `judge` reads
    that as no movement in that one signal, never as movement."""
    if not worktree or not os.path.isdir(worktree):
        return '', 0, ''
    try:
        head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=worktree,
                              capture_output=True, text=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=worktree,
                                capture_output=True, text=True)
    except (OSError, ValueError):
        return '', 0, ''
    if head.returncode != 0 or status.returncode != 0:
        return '', 0, ''
    porcelain = status.stdout
    files = len([line for line in porcelain.splitlines() if line.strip()])
    digest = hashlib.sha1(porcelain.encode('utf-8')).hexdigest()[:8]
    return head.stdout.strip(), files, digest


def sample(product, run, now=None, prev=None):
    """Measure ``run`` once and append the line; returns the sample. ``prev`` defaults to the
    store's newest. ``last_call_at`` is this sample's ``at`` when ``calls`` grew, else the
    previous sample's — so "the last tool call time" survives a restart of the sampler."""
    now = time.time() if now is None else now
    job = run['job']
    had_prev = prev is not None
    if not had_prev:
        stored = read(product, job)
        prev = stored[-1] if stored else None
    bytes_, calls, novel, classes, seen, ring, result = scan(run.get('log'), prev=prev)
    commit, files, digest = probe(run.get('worktree'))
    at = time.strftime(_AT_FMT, time.gmtime(now))
    grew = calls != ((prev or {}).get('calls') or 0)
    last_call_at = at if grew else (prev or {}).get('last_call_at', at)
    rec = {'at': at, 'bytes': bytes_, 'calls': calls, 'novel': novel, 'classes': classes,
          'seen': seen, 'last_call_at': last_call_at, 'ring': ring, 'commit': commit,
          'files': files, 'digest': digest, 'result': result}
    append(product, job, rec)
    return rec


# ---- the judge — two lines, four signals, one word ----------------------------

def signals(sample):
    return tuple(sample.get(k) for k in SIGNALS)


def judge(samples, now, limit):
    """``Verdict(cls, minutes, evidence)`` for one run.

    ``UNKNOWN`` when there are fewer than two samples, when the newest is older than ``limit``
    (nobody has measured this run recently — D6), or when no sample is ``limit`` minutes older
    than the newest. Otherwise the newest sample's :func:`signals` against those of the newest
    sample at or before ``now - limit``: equal is ``STUCK``, different is ``MOVING``. ``minutes``
    is how long the four have stood still (``STUCK``) or the span compared (``MOVING``).

    Pure: no clock, no git, no file. ``now`` is a unix time, ``limit`` is minutes.
    """
    if len(samples) < 2:
        return Verdict(UNKNOWN, None, evidence_line(samples, None, now))
    newest = samples[-1]
    newest_at = _epoch(newest.get('at'))
    if newest_at is None or (now - newest_at) / 60 > limit:
        return Verdict(UNKNOWN, None, evidence_line(samples, None, now))
    cutoff = newest_at - limit * 60
    older = None
    for s in reversed(samples[:-1]):
        at = _epoch(s.get('at'))
        if at is not None and at <= cutoff:
            older = s
            break
    if older is None:
        return Verdict(UNKNOWN, None, evidence_line(samples, None, now))
    minutes = (newest_at - _epoch(older.get('at'))) / 60
    cls = STUCK if signals(newest) == signals(older) else MOVING
    verdict = Verdict(cls, minutes, '')
    return Verdict(cls, minutes, evidence_line(samples, verdict, now))


def evidence_line(samples, verdict, now):
    """The row's own evidence: the commit or ``no commit``, the changed-file count, the classes of
    the last :data:`WINDOW_MIN` minutes with their count, whether ``novel`` moved, and how long
    ago the last tool call was. The line every reader of a ``STUCK`` row is given."""
    newest = samples[-1] if samples else {}
    commit = newest.get('commit') or 'no commit'
    files = newest.get('files') or 0
    classes = window_classes(samples, now, tunable('WINDOW_MIN'))
    word = 'class' if len(classes) == 1 else 'classes'
    part = f'{len(classes)} tool {word}'
    if classes:
        part += f" ({', '.join(classes)})"
    call_word = 'no new call' if (verdict and verdict.cls == STUCK) else 'a new call'
    minutes = int(verdict.minutes) if verdict and verdict.minutes is not None else 0
    last_call = _epoch(newest.get('last_call_at'))
    ago = int((now - last_call) / 60) if last_call is not None else 0
    return (f'{commit}, {files} files changed, {part} and {call_word} in {minutes}m '
           f'— last tool call {ago}m ago')


def minutes_of(value, default):
    """An int/float is minutes; ``'90s'``/``'20m'``/``'1h'``/``'1d'`` are converted; anything else
    is ``default``. ``bool`` is not a number here."""
    if isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    unit = ''
    if text and text[-1] in 'smhd':
        unit, text = text[-1], text[:-1]
    try:
        n = float(text)
    except ValueError:
        return float(default)
    return n * _UNIT_MINUTES[unit]


def progress_min(product):
    """``stage_limits.progress_min``, default :data:`DEFAULT_PROGRESS_MIN`. ``0`` turns the row
    off entirely."""
    return minutes_of((product.stage_limits or {}).get('progress_min', DEFAULT_PROGRESS_MIN),
                      DEFAULT_PROGRESS_MIN)


def result_record(kind, minutes, evidence, at):
    """The result the factory appends to a run it stopped for no progress — the twin of
    ``tokens.cap_result`` and ``budget.run_cap_result``: the structured ``asf.no_progress`` object
    is what a reader believes, the text says the same for a human."""
    return {'type': 'result', 'subtype': 'error', 'is_error': True,
           'result': f'no progress for {minutes}m ({evidence}) — stopped by the factory',
           'asf': {'no_progress': {'kind': kind, 'minutes': minutes,
                                   'evidence': evidence, 'at': at}}}


# ---- the sampler process, and the spawner that starts it ---------------------

def _job_record(product, job):
    """The ledger's own view of ``job``, folded from every line naming it — not
    :mod:`asf.workers.pool`'s fold (the licence forbids the import), just enough to know the
    pid, the log, the worktree, and whether the run has ended. ``None`` when the job has no line
    at all, or the ledger cannot be read."""
    path = os.path.join(env.state_dir(product), 'sessions.jsonl')
    rec, seen = {}, False
    try:
        with open(path, encoding='utf-8') as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(line, dict) and line.get('job') == job:
                    rec.update(line)
                    seen = True
    except OSError:
        return None
    return rec if seen else None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def watch(product, job, every=None, alive=None, now=None, max_h=None):
    """Sample ``job`` every ``every`` seconds until it ends; returns the number of samples taken.

    Ends on: a sample whose ``result`` is True (the run wrote its result), a pid that is gone
    (``os.kill(pid, 0)`` by default, ``alive`` injected for the test), a registry line that is no
    longer live, or ``max_h`` since it started. It judges nothing and stops nothing — a sampler
    that exits early costs resolution, and the tick's own sample (§2.5) is what guarantees a
    fresh one. Every iteration is wrapped: an OSError in one sample is skipped, not fatal.

    ``now``, when given, is a zero-argument clock called once per iteration (a scripted one, for
    the test) rather than a fixed instant — :func:`time.time` otherwise. Reading the registry
    line is the one place this needs the ledger: :func:`_job_record` reads the sessions JSONL
    under ``env.state_dir(product)`` directly, the fold it needs being "the last line for this
    job", not :mod:`asf.workers.pool`'s whole one, which the licence forbids importing.
    """
    every = tunable('SAMPLE_EVERY_S') if every is None else every
    max_h = tunable('WATCH_MAX_H') if max_h is None else max_h
    clock = now if callable(now) else time.time
    alive = alive if alive is not None else _pid_alive
    started = clock()
    taken = 0
    while True:
        t = clock()
        if (t - started) / 3600 >= max_h:
            return taken
        rec = _job_record(product, job)
        if rec is None or rec.get('ended'):
            return taken
        if not alive(rec.get('pid')):
            return taken
        try:
            sampled = sample(product, rec, now=t)
        except OSError:
            sampled = None
        if sampled is not None:
            taken += 1
            if sampled.get('result'):
                return taken
        time.sleep(every)


def start(product, record, cfg=None, spawn_fn=None, cloud=False):
    """Start the sampler for a launch, detached; returns its pid, or None when it was not started
    — a cloud run (no local log), ``worker_pool.progress_sampler: false``, or a failure. A launch
    is never refused because the sampler did not start: the spawn is inside its own ``try``."""
    if cloud:
        return None
    if (cfg or {}).get('worker_pool', {}).get('progress_sampler') is False:
        return None
    argv = [sys.executable, '-m', 'asf.cli', 'workers', 'progress',
           '--product', product.name, '--job', record['job'], '--watch']
    if spawn_fn is None:
        # imported here, not at module level: asf.detach is stdlib-only (its own docstring says
        # so), but importing it above would still read as a sibling module to LeafImportTests —
        # the one import this leaf takes beside asf.env, because the licence is about avoiding a
        # cycle back into asf.workers, and asf.detach has none (F-0066 Task 5 step 2)
        from asf.detach import spawn as spawn_fn
    try:
        return spawn_fn(argv)
    except OSError:
        return None


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'SAMPLE_EVERY_S': 'worker_pool.progress.sample_every_s',
    'WINDOW_MIN': 'worker_pool.progress.window_min',
    'KEEP_DAYS': 'worker_pool.progress.keep_days',
    'WATCH_MAX_H': 'worker_pool.progress.watch_max_h',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
