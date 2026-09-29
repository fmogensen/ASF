"""asf.workers.lifecycle — the one model of a lane job's life: its runs, the evidence, the state.

A job (``fix-bug-b-0001``, ``correct-b-0001``, ``adjudicate-b-0001``) runs a session on a branch;
the branch is what lands. The states, and what each one rests on::

    launched   a launch line in the registry, the pid alive, no commit on the branch yet
    running    the pid alive, no result in the log's last run
    ended      a result in the log's last run, or the pid gone (``finished`` | ``failed: …`` |
               ``dead pid`` | ``stopped …``) — recorded once by health as the ``ended`` line
    pushed     ended ``finished``: the result says ok AND ``origin/<branch>`` holds the
               worktree's HEAD with nothing uncommitted (B-0051) — what harvest may gate
    held       harvest held the pushed branch (red gate, conflict): a ``correction`` on the run,
               ``rounds`` counted over every run of the item — the feeder's FIX → CORRECT row
    corrected  a new run on the same branch started after the correction (the answer)
    adjudicate held at the round cap (:data:`ROUND_CAP`): the feeder's ADJUDICATE row, once
    landed     ``harvested: <sha>`` on the run — harvest pushed the rebased tip to the trunk
    reaped     landed (or empty) and the worktree is gone

Only ``reaped`` is terminal: every other state has a successor (:data:`TRANSITIONS`), so no
branch can sit still forever.

Beside this branch lifecycle — what lands — sits the **session state** (:data:`SESSION_STATES`,
F-0098): what a reader prints about the run's own session, five words wide::

    working              the pid answers
    ended-awaiting-tick  the session is over (its result, or its pid gone) but health has not
                         yet written the ``ended`` line — the result is known before the tick is
    finished             health recorded ``ended``, ``end_reason: finished``
    failed               health recorded ``ended``, a named failure
    dead                 the process is gone with no end-of-run record and nothing pushed, or the
                         operator stopped it

One function classifies a run into one of these (:func:`classify`), pure, from the run and its
evidence; :func:`state_of` is the impure entry point that gathers the evidence and calls it. No
other module derives a session state (:func:`holds_seat`, :func:`seats` answer the seat question
the same way) — see ``tests/test_lifecycle.py::OneClassifierTest``.

**The registry** (``sessions.jsonl``) is append-only. A line carrying ``started`` and ``pid`` is
a launch and opens a new run of its job; every other line for that job updates its *latest* run
(:func:`fold`). A run's terminal fields never fold into the next run (B-0041) — the boundary is
the launch line, not a set of nulls the launcher has to remember to write.

**Evidence** is gathered, never remembered: the log's last-run result (:func:`asf.workers.runtime
.read_result`), the pid, and git — ``origin/<branch>``, the worktree's tree and HEAD
(:func:`gather` → :class:`Evidence`). The branch state is derived from the run and the evidence
(:func:`derive`, itself re-expressed over :func:`classify`); nothing stores it a second time. The
one recorded transition is health's ``ended`` line (the timestamp and the reason at the time), so
the feeder, the pool and spawn can ask "is it live" without git (:func:`is_live`).

Every other module asks this one:

* spawn — :func:`may_launch`: refuse only a live run's worktree; an ended (or dead-pid) run's
  worktree and branch are reused (B-0025, B-0046, B-0048, B-0051);
* health — :func:`judge` (what ``ended`` line to write) and :func:`reap_verdict`;
* harvest — :func:`eligible` (pushed, not landed) and :func:`by_branch`;
* the feeder, the wave, the pool and capacity — :func:`occupies` (live on the ledger AND the pid
  answers: a dead run is no load even before health records its end), :func:`inflight`,
  :func:`attempts`, :func:`corrections`;
* the PR step — :func:`finished`;
* every reader of a session's own state — :func:`state_of` and :func:`classify` (F-0098).
"""
import dataclasses
import io
import json
import marshal
import os
import re
import signal
import subprocess
import time

from asf import budget, env, tokens
from asf.workers import cloudpid
from asf.workers import headroom
from asf.workers import runtime as runtime_mod

#: Holds on the SAME finding (:func:`finding_of`) — the first, then two corrections that failed
#: it — before the feeder switches to an ADJUDICATE row. Operator policy 2026-09-27: a different
#: finding resets the count to CORRECT; ``rounds`` still counts every hold, and decides nothing.
ROUND_CAP = 3

LAUNCHED = 'launched'
RUNNING = 'running'
ENDED = 'ended'
PUSHED = 'pushed'
HELD = 'held'
CORRECTED = 'corrected'
ADJUDICATE = 'adjudicate'
LANDED = 'landed'
REAPED = 'reaped'

STATES = (LAUNCHED, RUNNING, ENDED, PUSHED, HELD, CORRECTED, ADJUDICATE, LANDED, REAPED)

#: Every state's successors. Only ``reaped`` has none.
TRANSITIONS = {
    LAUNCHED: (RUNNING, ENDED),
    RUNNING: (ENDED,),
    ENDED: (PUSHED, REAPED, LAUNCHED),      # finished+pushed | empty worktree reaped | relaunched
    PUSHED: (HELD, LANDED),
    HELD: (CORRECTED, ADJUDICATE),
    CORRECTED: (RUNNING,),                  # the correction's run
    ADJUDICATE: (RUNNING,),                 # the adjudicate row's run
    LANDED: (REAPED,),
    REAPED: (),
}

#: A run's own fields: they belong to one launch and never fold into the next (B-0041).
RUN_FIELDS = ('ended', 'end_reason', 'rc', 'corrected', 'operator_flagged', 'harvested',
              'harvest', 'correction', 'rounds', 'stop_tip', 'capped', 'runtime_session',
              'resumed', 'continued', 'publish_refused')

FINISHED = 'finished'
#: read, never written: ledgers on disk carry this on runs health judged before F-0098
DEAD_PID = 'dead pid'
STOPPED = 'stopped'
PUSHED_AFTER_STOP = 'pushed after stop'
EMPTY_BRANCH = 'empty branch: nothing to land'
#: The prefix of a ``push_gap`` line: work done and not on origin (B-0051). One owner for the
#: string the classifier matches on.
NOT_PUSHED = 'not pushed'
#: A failure whose signature this module does not name.
OTHER = 'other'
#: A push the repo's own pre-push hook refused, and one the network dropped (B-0097): each a
#: class of its own, retried with no round spent — the session's work is not at fault.
HOOK_REFUSED = 'hook refused'
NETWORK_ERROR = 'network error'
RETRY_CLASSES = (HOOK_REFUSED, NETWORK_ERROR)
NETWORK_RE = re.compile(r'could not resolve host|connection (?:reset|refused|timed out|closed)|'
                        r'network is unreachable|unable to access|operation timed out|early eof|'
                        r'remote end hung up|ssl_error|gnutls', re.I)
#: The end_reason of a run a spent window cut short (asf.workers.headroom).
QUOTA_EXHAUSTED_REASON = f'failed: {headroom.QUOTA_EXHAUSTED}'
HOOK_RE = re.compile(r'\bhook\b|pre-push|refused|declined', re.I)
#: The factory's own pre-push redaction scan (:func:`_redaction_findings`) refusing a publish: the
#: same refusal the hook would make, so its precise correction reaches the session (F-0003).
REDACT_REFUSAL_RE = re.compile(r'(?:^|; )redact: \S+:\d+ ')
#: How many holds in a row on the SAME hook refusal (:func:`hook_refusal_hold`'s own
#: :func:`next_finding`) run before the second is never tried a third time blind (B-0140): lower
#: than :data:`ROUND_CAP` because a hook refusal spends no round in the first place — a session
#: is not at fault for the hook, so there is no reason to let it try the identical push twice.
HOOK_REFUSAL_CAP = 2
#: A hook's refusal text carries the redaction scanner's own ``redact: <file>:<line>`` line
#: (:mod:`asf.redact`) anywhere in it — the same line a product's own pre-push hook prints when
#: it runs that scan itself.
HOOK_REDACTION_RE = re.compile(r'\bredact:\s*\S+:\d+\b')

#: Every class a session's ``end_reason`` falls into. ``finished`` is the only one that is not a
#: failure; ``other`` is a failure whose signature this module does not name. Composed from the
#: constants that write the strings, so a rename follows.
OUTCOME_CLASSES = (FINISHED, NOT_PUSHED, EMPTY_BRANCH.split(':')[0], DEAD_PID, PUSHED_AFTER_STOP,
                   runtime_mod.report.UNPUSHED, *(name for name, _ in runtime_mod.FAILURE_SIGNATURES),
                   HOOK_REFUSED, NETWORK_ERROR, tokens.TOKEN_CAP, budget.RUN_CAP, OTHER)
FAILING_CLASSES = tuple(c for c in OUTCOME_CLASSES if c != FINISHED)

# ---- the session states (§2.1) ---------------------------------------------------

WORKING = 'working'
ENDED_AWAITING_TICK = 'ended-awaiting-tick'
FAILED = 'failed'
DEAD = 'dead'
#: FINISHED ('finished') is reused: it is already the end_reason health writes.
SESSION_STATES = (WORKING, ENDED_AWAITING_TICK, FINISHED, FAILED, DEAD)

PUSHED_NO_REPORT = 'pushed, no report'
NO_RECORD = 'no end-of-run record, nothing pushed'
STOPPED_BY_OPERATOR = 'stopped by the operator'
DEAD_REASONS = (NO_RECORD, STOPPED_BY_OPERATOR)

#: ``(state, table heading, count word)`` for the three live groups, in the order every view
#: prints them (P11): one tuple, so `views.sessions` and `views.status` cannot word it two ways.
LIVE_GROUPS = ((WORKING, 'Working', 'working'),
              (ENDED_AWAITING_TICK, 'Ended, awaiting tick', 'ended (awaiting tick)'),
              (DEAD, 'Dead', 'dead'))


def is_dead_reason(reason):
    """True for the two spellings a ``dead``-with-no-record ``end_reason`` carries: the retired
    :data:`DEAD_PID` and the honest text this module writes now (P8) — the one place both are
    accepted, so a reader matching on either is honest about ledgers written before and after
    F-0098."""
    return reason in (DEAD_PID, f'{DEAD}: {NO_RECORD}')


# ---- the session id -------------------------------------------------------------

def session_id(product, job, started):
    """``<product>/<job>@<YYYYMMDDTHHMMSSZ>`` — minted once per launch, from the same ``started``
    the registry line carries (F-0076, D1)."""
    return f"{product}/{job}@{started.replace('-', '').replace(':', '')}"


def parse_session(sid):
    """``(product, job, stamp)``, split on the first ``/`` and the last ``@``, or ``None`` for
    anything that does not split into three non-empty parts."""
    if not isinstance(sid, str) or '/' not in sid:
        return None
    product, rest = sid.split('/', 1)
    if '@' not in rest:
        return None
    job, stamp = rest.rsplit('@', 1)
    if not product or not job or not stamp:
        return None
    return product, job, stamp


# ---- the registry -------------------------------------------------------------

def is_launch(rec):
    """A launch line — the one that opens a run — carries ``started`` and ``pid``."""
    return isinstance(rec, dict) and bool(rec.get('job')) and 'started' in rec and 'pid' in rec


def _product_from_registry_dir(path):
    """The product a registry line with no ``product`` of its own takes it from: ``path``'s
    parent directory, but only when that directory sits directly under ``ASF_HOME/state`` (a
    test's hand-built registry elsewhere gets no derived product, and so no derived id) —
    F-0076, D12."""
    state_root = os.path.realpath(os.path.join(env.ASF_HOME, 'state'))
    parent = os.path.realpath(os.path.dirname(os.path.dirname(path)))
    if parent != state_root:
        return None
    return os.path.basename(os.path.dirname(path))


def read_lines(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, 'rb') as f:
        data = f.read()
    return _parse_registry(data, _product_from_registry_dir(path))


def _parse_registry(data, product_from_dir):
    """The registry's lines (``data``, its bytes) as records: undecodable lines and lines with
    no ``job`` skipped, a launch line with no ``session`` given one (F-0076)."""
    out = []
    # read as open(path, encoding='utf-8') would: universal newlines, nothing else a line end
    for line in io.TextIOWrapper(io.BytesIO(data), encoding='utf-8'):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(rec, dict) and rec.get('job')):
            continue
        if is_launch(rec) and not rec.get('session'):
            product = rec.get('product') or product_from_dir
            if product:
                rec = dict(rec, product=product,
                           session=session_id(product, rec['job'], rec.get('started') or ''))
        out.append(rec)
    return out


def _clean(run):
    return {k: v for k, v in run.items() if not (k in RUN_FIELDS and v is None)}


def _times(rec):
    """The times a registry line records: its run's ``started``/``ended`` and its correction's
    and lane record's ``at``."""
    yield rec.get('started') or ''
    yield rec.get('ended') or ''
    for key in ('correction', 'lane'):
        sub = rec.get(key)
        if isinstance(sub, dict):
            yield sub.get('at') or ''


def fold(lines):
    """``{job: [run, …]}`` in launch order. A launch line opens a run; any other line updates
    the latest run of its job (a line before any launch opens a run of its own — a hand-written
    registry). A run field written as null is dropped.

    A correction written with no ``at`` (a hand-set instruction, ``update_session`` before it
    stamped one) is as new as the newest time the registry had recorded when its line was
    written: with an empty time :func:`pending_correction` took every run of the item — even
    the ones before it — as its answer, and the correction was never read (a product's F-0035)."""
    runs, clock = {}, ''
    for rec in lines:
        job = rec['job']
        if is_reset(rec):
            clock = max([clock, rec[RESET].get('at') or ''])
            continue  # a boundary on its item's runs (:func:`resets`), never a run of its own
        corr = rec.get('correction')
        if isinstance(corr, dict) and corr.get('text') and not corr.get('at') and clock:
            rec = dict(rec, correction=dict(corr, at=clock))
        clock = max([clock] + [t for t in _times(rec) if isinstance(t, str)])
        if is_launch(rec) or job not in runs:
            runs.setdefault(job, []).append(dict(rec))
        else:
            runs[job][-1].update(rec)
    return {job: [_clean(r) for r in rs] for job, rs in runs.items()}


#: ``{(path, ASF_HOME): _Folded}`` — the registry folded once per *content*: every question the
#: tick asks of it (``corrections`` asked ``item_runs`` per run per item) used to re-read and
#: re-fold the whole file, thousands of parses a tick. Keyed on the bytes themselves, never on
#: mtime/size (a coarse filesystem clock can leave both unchanged across a rewrite), and on
#: ``ASF_HOME``, which the derived product of a line depends on. A few entries only: one per
#: product's registry.
_REGISTRY_CACHE = {}
_REGISTRY_CACHE_MAX = 8


class _Folded:
    """One registry content, folded: ``view`` is shared and never handed out — callers get
    ``marshal`` copies (fresh objects, nested values included), exactly as a fresh parse."""

    def __init__(self, data, view, resets=None):
        self.data = data
        self.view = view
        self.blob = marshal.dumps(view)
        self.resets = resets or {}
        self._by_item = None

    def by_item(self):
        """``{item: [run, ...]}`` in :func:`runs` order — the view's runs, not copies. A run
        started before its item's newest reset (:func:`resets`) is not the item's any more: it
        spent its rounds, its corrections and its rulings on work that was closed."""
        if self._by_item is None:
            idx = {}
            for rs in self.view.values():
                for r in rs:
                    item = r.get('item')
                    if item and isinstance(item, (str, int, float)):  # a malformed id matches none
                        if not self.before_reset(r):
                            idx.setdefault(item, []).append(r)
            self._by_item = idx
        return self._by_item

    def before_reset(self, run):
        """True when ``run`` started before its item's newest reset."""
        at = self.resets.get((run or {}).get('item'))
        return bool(at) and ((run or {}).get('started') or '') < at['at']


_EMPTY = _Folded(b'', {})


def _folded(path):
    """The registry at ``path`` as a :class:`_Folded` (read-only view; :data:`_EMPTY` when there
    is no file)."""
    if not path or not os.path.isfile(path):
        return _EMPTY
    with open(path, 'rb') as f:
        data = f.read()
    key = (os.path.abspath(path), env.ASF_HOME)
    hit = _REGISTRY_CACHE.get(key)
    if hit is not None and hit.data == data:
        return hit
    lines = _parse_registry(data, _product_from_registry_dir(path))
    hit = _Folded(data, fold(lines), _resets(lines))
    _REGISTRY_CACHE.pop(key, None)
    while len(_REGISTRY_CACHE) >= _REGISTRY_CACHE_MAX:
        _REGISTRY_CACHE.pop(next(iter(_REGISTRY_CACHE)))
    _REGISTRY_CACHE[key] = hit
    return hit


def runs(path):
    """``{job: [run, …]}`` — :func:`fold` of the registry, the caller's own copy."""
    return marshal.loads(_folded(path).blob)


def latest(path):
    """``{job: its latest run}`` — what every reader of the registry means by "the session"."""
    return {job: rs[-1] for job, rs in runs(path).items()}


# ---- resets: an item whose PR was closed unmerged starts over -----------------------------
#: The key of a registry line that resets an item (:func:`note_reset`): ``{at, pr, branch, head,
#: archive}``. Its job is :func:`reset_job` — never a launch, never a run.
RESET = 'reset'


def is_reset(rec):
    return isinstance((rec or {}).get(RESET), dict) and bool((rec or {}).get('item'))


def reset_job(item):
    return f'reset-{str(item).lower()}'


def _resets(lines):
    """``{item: its newest reset}`` from the registry's lines."""
    out = {}
    for rec in lines:
        if is_reset(rec):
            r = dict(rec[RESET], item=rec['item'])
            if (r.get('at') or '') >= ((out.get(rec['item']) or {}).get('at') or ''):
                out[rec['item']] = r
    return out


def resets(path):
    """``{item: {at, pr, branch, head, archive}}`` — each item's newest reset. Every run of the
    item started before ``at`` is history: no round, correction, ruling or attempt of it counts
    (:func:`rounds_of`, :func:`corrections`, :func:`attempts`), and no landing waits on it."""
    return {k: dict(v) for k, v in _folded(path).resets.items()}


def before_reset(path, run):
    """True when ``run`` started before its item's newest reset (:func:`resets`)."""
    return _folded(path).before_reset(run)


def reset_of_branch(path, branch):
    """The newest reset naming ``branch`` (``{at, pr, branch, head, archive, item}``), or None:
    while origin still sits at its ``head`` — the closed PR's — spawn cuts the branch fresh from
    the trunk (:func:`asf.workers.spawn.retire_dead_branch`)."""
    found = [r for r in _folded(path).resets.values() if branch and r.get('branch') == branch]
    return dict(max(found, key=lambda r: r.get('at') or '')) if found else None


def note_reset(path, item, reset, now=None, alive=None):
    """Append one reset line for ``item`` (``reset``: ``{pr, branch, head, archive}``, from
    :func:`asf.evidence.evidence.resets_of`) unless its newest reset already names the same PR
    and head, or a run of the item holds a seat (a live session is left to finish; the next
    pass resets it). True when a line was written."""
    if not path or not item:
        return False
    prev = _folded(path).resets.get(item) or {}
    if prev.get('pr') == reset.get('pr') and prev.get('head') == reset.get('head'):
        return False
    if any(r.get('item') == item and occupies(r, alive)
           for rs in _folded(path).view.values() for r in rs[-1:]):
        return False
    rec = {'job': reset_job(item), 'item': item,
           RESET: {'at': now or now_iso_utc(), 'pr': reset.get('pr'),
                   'branch': reset.get('branch') or '', 'head': reset.get('head') or '',
                   'archive': reset.get('archive') or ''}}
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')
    return True


def now_iso_utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def live_all(state_root, alive=None):
    """Every run that holds a seat (:func:`occupies`) under every product's registry
    (``state_root/*/sessions.jsonl``), each carrying its ``product`` and ``session``
    (:func:`read_lines`) — what the pool sums load over across products (F-0076)."""
    out = []
    if not state_root or not os.path.isdir(state_root):
        return out
    for name in sorted(os.listdir(state_root)):
        path = os.path.join(state_root, name, 'sessions.jsonl')
        if not os.path.isfile(path):
            continue
        out += [r for r in latest(path).values() if occupies(r, alive)]
    return out


def note_spent_windows(state_root=None, alive=None, now=None):
    """Stop, at once, the account of every run under every product's registry that is still
    live on the ledger, whose pid is gone, and whose log's last run closed on a usage-limit
    result (:func:`asf.workers.runtime.failure_reason` → ``quota-exhausted``). Health records the
    end only on its own product's next pass; the wave of another product may place a launch on
    the same account before that, so the pool calls this before it reads the stops
    (:func:`asf.workers.headroom.active_limits`). The lines :func:`headroom.note_exhausted` prints."""
    state_root = state_root or os.path.join(env.ASF_HOME, 'state')
    alive = alive or pid_alive
    out = []
    if not os.path.isdir(state_root):
        return out
    for name in sorted(os.listdir(state_root)):
        path = os.path.join(state_root, name, 'sessions.jsonl')
        if not os.path.isfile(path):
            continue
        for run in latest(path).values():
            if not is_live(run) or not run.get('account') or alive(run.get('pid')):
                continue
            rec = runtime_mod.read_result(run.get('log'))
            if rec is None or runtime_mod.failure_reason(rec) != headroom.QUOTA_EXHAUSTED:
                continue
            out.append(headroom.note_exhausted(run.get('product') or name, run, rec, now=now))
    return out


def by_branch(path):
    """``{branch: the latest run on it}`` across jobs (the last launch naming a branch owns it):
    a branch held and sent back runs under a new job name, and it is that run harvest reads.
    The latest by ``started``, not by job: the fold keys runs by job, so a job name that comes
    back (``correct-t-0360`` after ``adjudicate-t-0360``) would otherwise leave the older job
    the owner (B-0148). A run with no ``started`` falls back to fold order."""
    out = {}
    for rs in runs(path).values():
        for r in rs:
            b = r.get('branch')
            if b and (b not in out or (r.get('started') or '') >= (out[b].get('started') or '')):
                out[b] = r
    return out


def lane_of(run):
    """``run['lane']`` as the lane state machine's record (:mod:`asf.harvest.lane`), ``{}`` when
    the run carries none or it is not a map. A cloud-lane launch's own ``runtime_lane`` marker
    never lands here (F-lane-collision) — but an already-written registry line from before that
    split named the same key with a bare string (``"lane": "cloud"``), and this is every reader's
    one guard against it."""
    rec = (run or {}).get('lane')
    return rec if isinstance(rec, dict) else {}


def _case_insensitive(path):
    """Whether the filesystem holding ``path`` (its nearest existing ancestor with a letter in
    its name) ignores case: the same entry answers under its name with the case swapped."""
    p = path
    while True:
        parent = os.path.dirname(p)
        if os.path.exists(p) and p.swapcase() != p:
            break
        if parent == p:
            return False
        p = parent
    try:
        return os.path.samefile(p, p.swapcase())
    except OSError:
        return False


def path_key(path):
    """The identity of a filesystem path: its realpath, case-folded where the filesystem ignores
    case. ``os.path.realpath`` does not normalise case on macOS, so ``~/.ASF/…`` and ``~/.asf/…``
    — one directory there — were two keys, and a worktree recorded under one spelling was not
    found under the other."""
    rp = os.path.realpath(path)
    return rp.casefold() if _case_insensitive(rp) else rp


def by_worktree(path):
    """``{worktree path key: the latest run in it}`` — a worktree belongs to the run that recorded
    it last, whatever the directory is named (a correction reuses an ended job's worktree). The
    keys are :func:`path_key`: look up with ``path_key(p)``, never a bare realpath."""
    out = {}
    for rs in runs(path).values():
        for r in rs:
            if r.get('worktree'):
                out[path_key(r["worktree"])] = r
    return out


def rounds_of(path, item):
    """The correction rounds an item has had, over every run of every job on it."""
    if not item:
        return 0
    return max([r.get('rounds') or 0 for r in _item_view(path, item)] + [0])


def _item_view(path, item):
    """The runs on ``item`` off the shared view — read only, never handed out."""
    if not item:
        return []
    if not isinstance(item, (str, int, float)):  # no id: matched the slow way, as ever
        return [r for rs in _folded(path).view.values() for r in rs if r.get('item') == item]
    return _folded(path).by_item().get(item, [])


def item_runs(path, item):
    """Every run on ``item``, in :func:`runs` order — the caller's own copies."""
    return marshal.loads(marshal.dumps(_item_view(path, item)))


# ---- the recorded transition, and the questions that need no git ----------------

def is_live(run):
    """No ``ended`` line yet: health has not recorded the end. The feeder's inflight, the pool's
    load and spawn's refusal all read this and nothing else."""
    return bool(run) and not run.get('ended')


def pid_alive(pid):
    """The pid answers a signal 0 (a pid we may not signal is someone's, so alive). A cloud run's
    token (:mod:`asf.workers.cloudpid`) answers from the cloud status file instead."""
    if not pid:
        return False
    if cloudpid.is_token(pid):
        return cloudpid.alive(pid)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def occupies(run, alive=None):
    """The run holds a seat: live on the ledger (:func:`is_live`) AND its pid still answers.

    ``ended`` is written by health, and a tick that does not run health (a product with
    ``steps.health: off``, or a tick between two health passes) never writes it — a session
    that died there stayed "live" on the ledger for ever, counting as its account's load, as
    the product's in-flight session, and as the one job a cooling account may carry, so every
    other row waited on ``quota cooldown`` until an operator ran health by hand. A dead pid is
    no load, whatever the ledger has recorded yet: this is what the pool, the feeder's
    in-flight list and the capacity count all ask. A run with no pid recorded holds nothing."""
    if not is_live(run):
        return False
    return bool((alive or pid_alive)(run.get('pid')))


def result_of(run):
    """The run's log's last-run result line (:func:`asf.workers.runtime.read_result`), or None."""
    return runtime_mod.read_result((run or {}).get('log'))


def finished_unrecorded(run, alive=None, result=None):
    """Live on the ledger, its pid gone, and its log's last run closed on a success result: the
    session finished and exited normally, and health has not written its ``ended`` line yet. It
    holds no seat (:func:`occupies`), but it is not dead either — its work waits for health and
    harvest, not for another session. Only a pid that vanished *without* a result is dead.
    ``result``: ``callable(run) -> result line`` (default :func:`result_of`)."""
    if not is_live(run) or (alive or pid_alive)(run.get('pid')):
        return False
    return runtime_mod.result_ok((result or result_of)(run))


def finished(run):
    """Ended ``finished`` — which health writes only for a result that says ok on a branch that
    is pushed (:func:`judge`), so this alone means "pushed"."""
    return bool(run) and bool(run.get('ended')) and run.get('end_reason') == FINISHED


def landed(run):
    return bool(run) and bool(run.get('harvested'))


def eligible(run):
    """What harvest may gate: finished (hence pushed), not landed, not handed to the PR lane."""
    return finished(run) and not landed(run) and run.get('harvest') != 'pr'


#: ``harvested:`` values that are not a landing: harvest archived the run, nothing reached the trunk
NOT_A_LANDING = ('superseded',)


def landed_earlier(path, run):
    """The sha an earlier run on ``run``'s branch was harvested at — its lane's work is already on
    the trunk (a squash-merged lane PR included: the native landing marks the run at merge) — or
    None. A later session on that branch that writes nothing has nothing to land, and is not sent
    back to push work the trunk already holds."""
    branch, started = (run or {}).get('branch'), (run or {}).get('started') or ''
    if not path or not branch:
        return None
    for rs in _folded(path).view.values():  # read only: the sha is all that leaves
        for r in rs:
            sha = r.get('harvested')
            if (r.get('branch') == branch and sha and sha not in NOT_A_LANDING
                    and (r.get('started') or '') < started):
                return sha
    return None


def empty_on_a_landed_lane(path, run):
    """The harvested sha when ``run`` ended ``failed: empty branch`` on a branch an earlier run
    already landed; else None."""
    if (run or {}).get('end_reason') != f'failed: {EMPTY_BRANCH}' or landed(run):
        return None
    return landed_earlier(path, run)


def pending_correction(run, path=None):
    """The correction on ``run`` still waiting for its session: none of the item's runs started
    at or after it (health and harvest write corrections before the wave launches, so a run of
    the same second is the answer). ``None`` when there is none or it has been answered — or when
    it is an empty-branch correction on a branch whose work an earlier run already landed.

    An ``adjudicate`` run never answers a correction (B-0128): it rules, it does not touch the
    branch, so it must not read as "corrected" and bounce the lane's BACK back to PUSHED —
    restarting review on a head nothing changed, which (the round cap never falling) walks
    straight back to another STALEMATE → ADJUDICATE row. :func:`corrections`' ``settled`` is
    the ruling's own answer: no more adjudicate rows over this same hold."""
    corr = (run or {}).get('correction') or {}
    if not corr.get('text'):
        return None
    if path is not None and (before_reset(path, run) or empty_on_a_landed_lane(path, run)):
        return None  # a reset item's old corrections are for work that was closed
    at = corr.get('at') or ''
    if path is not None:
        me = (run.get('job'), run.get('started'))
        later = [r for r in item_runs(path, run.get('item'))
                 if (r.get('started') or '') >= at and (r.get('job'), r.get('started')) != me
                 and not quota_exhausted(r) and r.get('kind') != 'adjudicate']
        if later:
            return None
    return corr


def _settling_run(path, item, at):
    """The adjudicate run that settled the correction raised ``at`` (B-0128) — the earliest
    finished adjudicate run started at or after it — or None."""
    candidates = sorted((r for r in item_runs(path, item)
                          if r.get('kind') == 'adjudicate' and finished(r)
                          and (r.get('started') or '') >= (at or '')),
                         key=lambda r: r.get('started') or '')
    return candidates[0] if candidates else None


def ruled(path, item, corr):
    """True when ``corr`` is an adjudication's instruction: not health's own hold at the cap
    (``at_cap``), and written after an adjudicate session on ``item`` started. At the round cap
    a hold goes to adjudication; what is written onto the held run after that — the ruling's
    fix, or one made on the operator's behalf — is what the adjudication was for, and a session
    carries it out (a FIX → CORRECT row), not a second adjudicate session over the same hold
    (a product's F-0035 ``redact``). Health's next hold on that session's work is at the cap
    again (``at_cap``), so the loop guard stands: one instruction per adjudication."""
    corr = corr or {}
    at = corr.get('at') or ''
    if not at or corr.get('at_cap'):
        return False
    return any(r.get('kind') == 'adjudicate' and (r.get('started') or '~') <= at
               for r in _item_view(path, item))


def settled(path, item, at):
    """True when an adjudicate session has already *finished* over the correction raised ``at``
    (B-0128): the ruling is in, and the feeder asks for no second one over the same hold. A run
    that crashed, was stopped, or ran out of quota (:func:`finished` is False for any of those)
    delivered no ruling, so it does not settle the hold."""
    return _settling_run(path, item, at) is not None


#: the sha a REPORT's ``pushed:`` line names (``yes <sha>`` / ``rebased <sha> — …``)
PUSHED_SHA_RE = re.compile(r'\b[0-9a-f]{7,40}\b', re.I)


def overruling(path, item, head, unchanged_since=None):
    """The job of the adjudicate run whose ruling stands on ``head``, or None.

    An adjudicate session answers every open finding: *upheld* — it makes the edit and pushes it
    (the head moves, so the review is no longer current and a new round is asked for) — or
    *overruled* — no edit. So the newest *finished* adjudicate run of ``item`` whose REPORT is
    ``status: done``, carries a ``ruling:``, claims no ``blocked_on``/``superseded_by`` and says
    it left the branch at ``head`` (its ``pushed:`` sha) has answered the review of ``head``:
    the lane does not send that review back again (a product's B-1377, 2026-09-26: every ruling
    re-held off the same stale review file, 14 adjudicate sessions).

    The ``pushed:`` sha is the session's own claim, and it names the commit *it* thinks of as
    the tip — B-1377's rulings named the code commit under the review commit that is the head,
    and misspelled it past its ninth digit (``99bcb623ee0a…`` for ``99bcb623e36e…``), four more
    sessions. So the ruling also stands on ``head`` when the run committed nothing
    (``commits: none``) and spawn's ``launch_head`` — the fact — is ``head``, or when
    ``unchanged_since(sha)`` (the lane's :func:`asf.evidence.review.only_reviews_since`) says
    ``head`` is that sha plus review files only."""
    if not head or not path or not item:
        return None
    from asf.workers import report as report_mod
    ruled = sorted((r for r in item_runs(path, item)
                    if r.get('kind') == 'adjudicate' and finished(r)),
                   key=lambda r: r.get('started') or '')
    if not ruled:
        return None
    run = ruled[-1]  # only the newest ruling speaks for the branch as it stands
    rec = result_of(run) or {}
    text = rec.get('result') if isinstance(rec, dict) else ''
    rep = report_mod.parse(text or '')
    status = (rep.get('status') or '').strip().lower().split(' ')[0]
    fields = report_mod.ruling_fields(text or '')
    if status != 'done' or not report_mod.ruling(text or '') \
            or fields['blocked_on'] or fields['superseded_by']:
        return None
    m = PUSHED_SHA_RE.search(rep.get('pushed') or '')
    sha = m.group(0).lower() if m else ''
    if sha and head.lower().startswith(sha):
        return run.get('job')
    if not report_mod._claim(rep.get('commits')) and run.get('launch_head') \
            and run['launch_head'].lower() == head.lower():
        return run.get('job')  # launched on this head, committed nothing: the head it ruled on
    if sha and unchanged_since and unchanged_since(sha):
        return run.get('job')  # the head is that sha plus the review commit on top of it
    return None


#: a PR the ruling paragraph names, e.g. "waits on merge of #773" — B-0128's ``asf next`` line
PR_RE = re.compile(r'#(\d+)')


def settled_prs(path, item, at):
    """The PR numbers (``'773'``, ...) the ruling that settled this hold names, in the order
    they first appear, or ``[]`` when it settled on no ruling text or the ruling names none
    (B-0128: ``asf next`` shows ``WAITS ON merge: #773, #775``)."""
    run = _settling_run(path, item, at)
    if not run:
        return []
    from asf.workers import report as report_mod
    rec = result_of(run) or {}
    text = report_mod.ruling(rec.get('result') if isinstance(rec, dict) else '')
    return list(dict.fromkeys(PR_RE.findall(text)))


def inflight(path, alive=None):
    """The feeder's ``inflight`` list: one dict per run that holds a seat (:func:`occupies`)."""
    return [{'item': r.get('item'), 'kind': r.get('kind'), 'account': r.get('account'),
             'job': job, 'started': r.get('started')}
            for job, r in latest(path).items() if occupies(r, alive)]


def awaiting_harvest(path, alive=None, result=None):
    """The items whose branch is ``pushed`` — its latest run finished, not landed, no correction
    pending — and so waits for harvest, not for another session. The feeder holds them busy
    (B-0025's loop: a finished branch was relaunched every tick until harvest got to it, each
    relaunch a live run that then hid the finished one from harvest). A run that finished and
    exited before health recorded its end (:func:`finished_unrecorded`) waits the same way: its
    pid is gone, but its session is done, not missing."""
    out = set()
    for run in by_branch(path).values():
        if not run.get('item') or before_reset(path, run) or pending_correction(run, path):
            continue
        if eligible(run) or finished_unrecorded(run, alive, result):
            out.add(run['item'])
    return out


PUSHED_WAIT = 'pushed, waiting to land'
PR_WAIT = 'pushed, PR open, waiting to land'
FINISHED_WAIT = 'finished, awaiting harvest'


def unlanded(path, alive=None, result=None):
    """``{item: {kind: why}}`` — every item whose latest run on a branch left work that has not
    landed and is not waiting for a session: finished and pushed (harvest's to gate), handed to
    the PR lane (``harvest: pr`` — a PR open, the merge is the landing), or finished before health
    recorded it. A pending correction is a session's to answer, so it is not listed. The feeder
    reads it per document: a spec or plan pushed and waiting to land is not starved."""
    out = {}
    for run in by_branch(path).values():
        item, kind = run.get('item'), run.get('kind')
        if not item or not kind or landed(run) or before_reset(path, run) \
                or pending_correction(run, path):
            continue
        if run.get('harvest') == 'pr':
            why = PR_WAIT
        elif eligible(run):
            why = PUSHED_WAIT
        elif finished_unrecorded(run, alive, result):
            why = FINISHED_WAIT
        else:
            continue
        out.setdefault(item, {})[kind] = why
    return out


def quota_exhausted(run):
    """The run ended on a spent window (:data:`asf.workers.headroom.QUOTA_EXHAUSTED`): the
    account's fault, not the work's — no attempt, no round, no answer to a correction."""
    return (run or {}).get('end_reason') == QUOTA_EXHAUSTED_REASON


def attempts(path):
    """``{item: runs the registry holds for it}`` — every launch, ended or not, except a run
    a spent window cut short (:func:`quota_exhausted`)."""
    out = {}
    folded = _folded(path)
    for rs in folded.view.values():  # read only: counts leave, never runs
        for r in rs:
            if r.get('item') and not quota_exhausted(r) and not folded.before_reset(r):
                out[r['item']] = out.get(r['item'], 0) + 1
    return out


def corrections(path):
    """``{item: {kind, text, at, rounds, same, branch, ruled, settled, prs}}``: the newest pending correction
    per item, with the branch of the run it was written on (a held spec branch is corrected on
    ``spec/<id>``, not on the item's task prefix). ``settled`` (B-0128): an adjudicate session has
    already ended over this same hold — the feeder shows a WAITS ON row, not another STALEMATE.
    ``prs`` (B-0128): the PR numbers that ruling names, for the same row to print. ``ruled``
    (:func:`ruled`): the correction is an adjudication's instruction — a session answers it
    whatever the round. ``same``: the holds in a row on this finding (:func:`repeats`) — what the
    ADJUDICATE row is decided on."""
    out = {}
    for item in {r.get('item') for rs in _folded(path).view.values() for r in rs if r.get('item')}:
        c = correction_of(path, item)
        if c:
            out[item] = c
    return out


def correction_of(path, item):
    """:func:`corrections`' entry for one ``item``, or None when none is pending."""
    held = [(r, pending_correction(r, path)) for r in item_runs(path, item)]
    held = [(r, c) for r, c in held if c]
    if not held:
        return None
    run, corr = max(held, key=lambda rc: rc[1].get('at') or '')
    return dict(corr, rounds=rounds_of(path, item), same=repeats(corr), branch=run.get('branch'),
                ruled=ruled(path, item, corr),
                settled=settled(path, item, corr.get('at')),
                prs=settled_prs(path, item, corr.get('at')))


def occupancy(path, lanes=None, alive=None, result=None):
    """The one answer to "is this item busy?" (the contract; W3 implements it). The feeder reads
    this and nothing else.

    Folds, from the registry at ``path`` read once: :func:`inflight`, :func:`awaiting_harvest`,
    :func:`unlanded` and :func:`corrections`, plus ``lanes`` — the item ids the lane holds
    (:func:`asf.harvest.lane.busy_items`: every open lane state except BACK). ``alive`` and
    ``result`` are the probes :func:`occupies`/:func:`awaiting_harvest` take (tests pass fakes).

    Returns ``{'busy': {item: why}, 'waiting_landing': {item: why}, 'corrections': {item:
    {kind, text, at, rounds, branch}}}``: ``busy`` — a live run or a lane state holds the item
    (no launch row); ``waiting_landing`` — pushed and waiting on the lane (a PUSHED → REVIEW or
    → LAND row, never a new session); ``corrections`` — as :func:`corrections`, the feeder's
    BACK rows. An item is in at most one of ``busy`` and ``waiting_landing``.

    Beside those, for the feeder's rows: ``lanes`` (``{branch: lane record + item, kind}`` of
    every branch whose lane state holds it), ``review`` (``{item: {branch, round, pr}}`` — lane
    REVIEW: a PUSHED → REVIEW row), ``landing`` (``{item: {branch, state, pr, why}}`` — the other
    lane states: a PUSHED → LAND row), ``branches`` (``{branch: why}`` of every waiting branch)
    and ``docs`` (``{item: {kind: why}}``: a spec or plan pushed and waiting is not starved).

    ``lanes`` (the argument): ``{branch: record}`` to use instead of the run lines' own."""
    from asf.harvest import lane as lane_mod  # the lane's states, no cycle at import
    by = by_branch(path)
    live_items = {s['item']: f"session {s['job']} running" for s in inflight(path, alive)
                  if s.get('item')}
    out = {'busy': dict(live_items), 'waiting_landing': {}, 'corrections': corrections(path),
           'lanes': {}, 'review': {}, 'landing': {}, 'branches': {}, 'docs': {}, 'landed': {}}
    for branch, run in by.items():
        item, kind = run.get('item'), run.get('kind')
        rec = (lanes or {}).get(branch) if lanes is not None else lane_of(run)
        if not item or item in live_items or before_reset(path, run):
            continue  # a reset item's old branch holds nothing: its PR was closed unmerged
        if landed(run) or (rec or {}).get('state') == lane_mod.MERGED:
            # its branch landed and the record may not have caught up yet: not busy, not
            # waiting — and not an idle branch either (the feeder's idle-branch rule reads this)
            out['landed'][item] = run.get('harvested') or (rec or {}).get('sha') or ''
        why = None
        if rec and rec.get('state'):
            state = rec['state']
            if state not in lane_mod.BUSY_STATES or landed(run) and state != lane_mod.MERGING:
                continue
            # a correction written on a landing wait is the lane's BACK already (T9c): the
            # feeder's FIX → CORRECT row speaks for it, not a WAITS ON landing row
            if lane_mod.correction_turns_back(rec, pending_correction(run, path)):
                continue
            out['lanes'][branch] = dict(rec, item=item, kind=kind)
            pr = f" PR #{rec['pr']}" if rec.get('pr') else ''
            why = f"lane {state}{pr}: {rec.get('reason') or ''}".rstrip(': ')
            if state == lane_mod.REVIEW:
                out['review'][item] = {'branch': branch, 'round': int(rec.get('round') or 1),
                                       'pr': rec.get('pr'), 'why': rec.get('reason') or ''}
            else:
                out['landing'][item] = {'branch': branch, 'state': state, 'pr': rec.get('pr'),
                                        'why': why}
        elif landed(run) or pending_correction(run, path):
            continue
        elif run.get('harvest') == 'pr':
            why = PR_WAIT
        elif eligible(run):
            why = PUSHED_WAIT
        elif is_live(run) and finished_unrecorded(run, alive, result):
            why = FINISHED_WAIT
        if not why:
            continue
        out['waiting_landing'].setdefault(item, why)
        out['branches'][branch] = why
        if kind:
            out['docs'].setdefault(item, {})[kind] = why
    return out


#: A card in one of these states has no work left for a session (the feeder's ``DONE_STATES``).
DONE_STATES = ('Resolved', 'Closed')


def closed_state(items, item):
    """Why ``item``'s card wants no more sessions — ``'removed'`` or its done state — or None.
    ``items`` is the record's index (``{id: card}``, removed cards included); None, or an item
    the index does not hold, is never closed (unknown ≠ done). A run of a closed item is ended and
    reaped, never held or sent back to its session: there is nothing for the session to do."""
    card = (items or {}).get(item or '') or {}
    if card.get('removed'):
        return 'removed'
    if card.get('state') in DONE_STATES:
        return card['state']
    return None


# ---- evidence -------------------------------------------------------------------

@dataclasses.dataclass
class Evidence:
    """What the world says about one run. Every field has a null default so a test can state
    exactly the evidence it means and nothing else."""
    result: dict = None          #: the log's last-run result line, None while running
    alive: bool = False          #: the pid answers
    worktree: bool = False       #: the worktree directory exists
    uncommitted: int = 0         #: files in the worktree not committed
    remote_sha: str = ''         #: ``origin/<branch>``'s tip; '' when the branch is not on origin
    head_on_remote: bool = False  #: the worktree's HEAD is contained in ``origin/<branch>``
    unpushed: int = 0            #: commits on HEAD not on ``origin/<branch>`` (``origin/<main>``
    #: when the branch was never pushed)
    has_commits: bool = False    #: the branch was ever committed to
    in_trunk: bool = False       #: HEAD is an ancestor of ``origin/<main>``

    @property
    def pushed(self):
        return bool(self.remote_sha) and self.uncommitted == 0 and self.unpushed == 0


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


def _count(p):
    return int(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip().isdigit() else 0


def unpushed_commits(wt, remote_sha, main='main'):
    """How many of the session's *own* commits are missing from ``origin/<branch>``.

    Counted above ``origin/<main>``, and by patch rather than by sha. Harvest rebases a branch
    onto the trunk in its own throwaway worktree (:func:`asf.harvest.harvest.rebase_and_resolve`)
    *after* the session pushed it, which does two things to a plain ``origin/<branch>..HEAD``
    count: it drags every commit the trunk has gained into the range, and it gives the session's
    own commits new shas. The count then reports the trunk's work as this session's unpushed work
    and an already-pushed commit as missing — and no push a session is allowed to make can bring
    it back to zero, because a plain push of a rebased branch is not a fast-forward and the
    standing rules forbid a force. The branch is then held "unpushed" round after round with no
    move that clears it (B-0053).

    ``git cherry <remote> HEAD <limit>`` answers the question that was meant: of the commits above
    the trunk, which have no equivalent patch on the remote branch (``+``) and which have one
    (``-``).
    """
    if not remote_sha:
        return _count(_git(['rev-list', '--count', f'origin/{main}..HEAD'], wt))
    p = _git(['cherry', remote_sha, 'HEAD', f'origin/{main}'], wt)
    if p.returncode != 0:  # no origin/<main> here, or a remote sha this repo has not fetched
        return _count(_git(['rev-list', '--count', f'{remote_sha}..HEAD'], wt))
    return len([ln for ln in p.stdout.splitlines() if ln.startswith('+')])


#: The line a push refused for a stale head carries: origin holds commits the new head lacks.
STALE_HEAD_RE = re.compile(r'\bwould lose \d+ commit')


def lost_commits(wt, new, remote_sha, branch=''):
    """The short shas of the commits on ``remote_sha`` that pushing ``new`` over it would erase
    (2026-09-25: a stale worktree's head published over a person's newer commit — the lease
    guarded only against origin moving, never against the head lacking what origin held).

    ``[]`` when ``remote_sha`` is an ancestor of ``new``, or when every commit it has that
    ``new`` lacks has a patch-equivalent in ``new`` (a rebase's copies: ``git cherry``'s ``-``).
    None when it cannot be told (the remote commit is not here even after a fetch of
    ``branch``) — a caller refuses then, never pushes blind."""
    if not remote_sha or not new:
        return []
    if _git(['cat-file', '-e', f'{remote_sha}^{{commit}}'], wt).returncode != 0 and branch:
        _git(['fetch', '-q', 'origin', branch], wt)
    if _git(['cat-file', '-e', f'{remote_sha}^{{commit}}'], wt).returncode != 0:
        return None
    if _git(['merge-base', '--is-ancestor', remote_sha, new], wt).returncode == 0:
        return []
    p = _git(['cherry', new, remote_sha], wt)
    if p.returncode != 0:
        return None
    return [ln.split()[1][:9] for ln in p.stdout.splitlines() if ln.startswith('+')]


def loss_refusal(branch, lost, main=None, subjects=None):
    """The one-line reason a push that would erase ``lost`` (None: unknown) is refused, with
    the one move that clears it. A head on the remote's own trunk base is behind
    ``origin/<branch>``: rebase onto it. A head past the remote on the trunk (``main`` given:
    :func:`past_remote_on_trunk`) already did the lane's rebase and dropped ``lost`` on the
    way, and the factory could not carry them onto it (:func:`carry_onto_head`, a conflict):
    rebase onto ``origin/<main>`` again and carry them by hand — never onto ``origin/<branch>``,
    whose stale trunk history is what the rebase was for (a product's T-0338: the two
    instructions alternated for 27 hours)."""
    if lost is None and main:
        return (f'cannot tell what origin/{branch} holds that this rebase onto origin/{main} '
                f'dropped — fetch origin/{branch}, rebase onto origin/{main} again and carry '
                f'what it holds; never onto origin/{branch}')
    if lost is None:
        return (f'cannot tell what origin/{branch} holds that this head lacks — '
                f'fetch and rebase onto origin/{branch}, then push')
    if main:
        named = ', '.join(f'{sha} {subjects[sha]}' if subjects and subjects.get(sha) else sha
                          for sha in lost)
        return (f'would lose {len(lost)} commit(s) on origin/{branch} ({named}) that this '
                f'rebase onto origin/{main} dropped and neither origin/{main} nor the head '
                f'carries — rebase onto origin/{main} again and carry them '
                f'(git cherry-pick {" ".join(lost)}); never onto origin/{branch}')
    return (f'would lose {len(lost)} commit(s) on origin/{branch} ({", ".join(lost)}) — '
            f'rebase onto origin/{branch}, then push')


def past_remote_on_trunk(wt, new, remote_sha, main='main'):
    """True when ``new``'s base on ``origin/<main>`` is strictly past ``remote_sha``'s: the
    head is a rebase onto a newer trunk than ``origin/<branch>`` sits on. Such a head is never
    rebased onto the remote — the trunk commits it carries would replay over their own stale
    copies: a conflict by construction, or a branch with trunk history under it again, which
    the lane holds again (the ping-pong). It is published over the old tip when every commit
    it drops is accounted for (:func:`rebased_off_copies`) or carried onto it
    (:func:`carry_onto_head`), and refused with the trunk named when one does not apply."""
    if not wt or not new or not remote_sha:
        return False
    trunk = f'refs/remotes/origin/{main}'
    old_base = _git(['merge-base', remote_sha, trunk], wt).stdout.strip()
    new_base = _git(['merge-base', new, trunk], wt).stdout.strip()
    return bool(old_base and new_base and old_base != new_base
                and _git(['merge-base', '--is-ancestor', old_base, new_base], wt).returncode == 0)


def carry_onto_head(wt, shas):
    """Cherry-pick ``shas`` (short, in order) onto the worktree's clean HEAD: the commits
    ``origin/<branch>`` holds that a rebase onto the trunk dropped and nothing accounts for,
    carried by the factory the way a session would be told to. One that becomes empty is
    skipped (its change is already here); one that conflicts, or a dirty tree, or None,
    undoes every pick — HEAD is left exactly as it was — and answers False: the caller
    refuses and names them. Never a merge, never a rebase onto the stale remote."""
    if not shas:
        return False
    st = _git(['status', '--porcelain'], wt)
    if st.returncode != 0 or st.stdout.strip():
        return False
    orig = _git(['rev-parse', 'HEAD'], wt).stdout.strip()
    if not orig:
        return False
    for sha in shas:
        r = _git(['cherry-pick', sha], wt)
        if r.returncode == 0:
            continue
        unmerged = _git(['diff', '--name-only', '--diff-filter=U'], wt).stdout.strip()
        if not unmerged and _git(['cherry-pick', '--skip'], wt).returncode == 0:
            continue  # its change is already in the head: nothing to carry
        _git(['cherry-pick', '--abort'], wt)
        _git(['reset', '-q', '--hard', orig], wt)
        return False
    return True


def _subjects(wt, shas):
    """``{short sha: subject}`` for the commits ``shas`` names, each subject cut to 72 chars."""
    out = {}
    for sha in shas or []:
        p = _git(['log', '-1', '--format=%s', sha], wt)
        if p.returncode == 0 and p.stdout.strip():
            out[sha] = p.stdout.strip()[:72]
    return out


def stale_head(text):
    """True for a push refused because the head lacks commits origin holds: the branch is held
    for a rebase onto the remote head, never retried as a push."""
    return bool(STALE_HEAD_RE.search(text or '')) or 'cannot tell what origin/' in (text or '')


def commit_leftovers(wt, branch):
    """Commit, signed off, whatever a finished session left uncommitted in its worktree (B-0094).

    19% of sessions ended ``failed: not pushed: N uncommitted file(s)``: the work was done and the
    session exited before committing it, and each one cost a correction session. The factory
    commits it on the session's behalf, so :func:`publish` can then put it on origin. Only a run
    whose result said ok comes here. The repo's own commit hooks still run (the redaction gate
    among them): a refusal is returned, and the files stay a hold. ``(ok, line)``."""
    if _git(['add', '-A'], wt).returncode != 0:
        return False, f'commit {branch} refused: git add failed'
    p = _git(['commit', '-q', '-s', '-m', f'wip({branch}): the session ended with this uncommitted — '
              'committed by the factory (B-0094)'], wt)
    if p.returncode != 0:
        why = [ln for ln in (p.stderr or p.stdout).splitlines() if ln.strip()]
        return False, f'commit {branch} refused: {why[-1].strip() if why else "commit failed"}'
    return True, f'committed the session\'s leftovers on {branch}'


def rebased_off_copies(wt, new, remote_sha, lost, main='main'):
    """True when every commit in ``lost`` (short shas on ``remote_sha`` that ``new`` lacks by
    patch, :func:`lost_commits`) is accounted for by a rebase onto the trunk
    (:func:`unaccounted_commits` names none). A lane branch holding trunk copies is sent back
    "rebase onto origin/<main>" (asf.harvest.lane.drop_copies); this is that rebase arriving."""
    if not lost or not new or not remote_sha:
        return False
    return unaccounted_commits(wt, new, remote_sha, lost, main) == []


def unaccounted_commits(wt, new, remote_sha, lost, main='main'):
    """The commits in ``lost`` that a rebase of ``remote_sha`` onto the trunk does not account
    for — each a short sha — or None when that cannot be read. A lost commit is accounted for
    when it is the trunk's: a copy of a trunk commit (``git cherry origin/<main>`` ``-``: its
    change is the trunk's), a commit under a trunk subject since the remote's base, reworded
    or not (the trunk's own landing of it, another patch), or one that only added files the
    trunk then added its own copy of (:func:`_resolved_to_trunk`); or the branch's own,
    rewritten: a commit ``new`` carries past the trunk under the same author and subject (a
    conflict resolved by hand keeps its message, not its patch — the patch-equivalent copies
    :func:`lost_commits` already left out), or an empty one; or superseded: the trunk has
    since taken in every line of its files' change, and ``new`` reads the trunk's copy
    (:func:`_superseded_by_trunk`). The whole old tip is accounted
    for when its net change against its trunk base is already in ``new``
    (:func:`_net_change_kept`: a squash, or work the trunk carried in another form). What is
    left is someone's work the head would erase: named to the session, never pushed over."""
    if not lost or not new or not remote_sha:
        return list(lost or [])
    _git(['fetch', '-q', 'origin', f'+refs/heads/{main}:refs/remotes/origin/{main}'], wt)
    trunk = f'refs/remotes/origin/{main}'
    cherry = _git(['cherry', trunk, remote_sha], wt)
    mine = _git(['log', '--no-merges', '--format=%ae%x00%s', f'{trunk}..{new}'], wt)
    theirs = _git(['log', '--no-merges', '--format=%H%x00%ae%x00%s', f'{trunk}..{remote_sha}'],
                  wt)
    if cherry.returncode != 0 or mine.returncode != 0 or theirs.returncode != 0:
        return None
    base = _git(['merge-base', remote_sha, trunk], wt).stdout.strip()
    if _net_change_kept(wt, new, remote_sha, base):
        return []
    copies = {ln.split()[1] for ln in cherry.stdout.splitlines() if ln.startswith('- ')}
    carried = set(mine.stdout.splitlines())
    ident = {}
    for ln in theirs.stdout.splitlines():
        sha, _, rest = ln.partition('\x00')
        ident[sha] = rest
    trunk_subjects = None
    covered = None
    left = []
    for short in lost:
        full = next((s for s in list(copies) + list(ident) if s.startswith(short)), '')
        if not full:
            left.append(short)
            continue
        if full in copies or ident.get(full) in carried or _empty_commit(wt, full) \
                or _resolved_to_trunk(wt, full, new, trunk, base):
            continue
        if covered is None:
            covered = _trunk_covered_tree(wt, remote_sha, trunk, base)
        if _superseded_by_trunk(wt, full, new, trunk, covered, base, remote_sha):
            continue
        if trunk_subjects is None:
            trunk_subjects = _trunk_subjects(wt, remote_sha, trunk)
        subject = (ident.get(full) or '').split('\x00', 1)[-1]
        if subject and (subject in trunk_subjects
                        or REWORD_PREFIX_RE.sub('', subject, count=1) in trunk_subjects):
            continue
        left.append(short)
    return left


#: The prefix the lane's reword puts on a subject that does not name the item
#: (asf.harvest.lane.reword_branch): ``task(T-0338): hotfix(hooks): … (#804)``.
REWORD_PREFIX_RE = re.compile(r'^[a-z][\w-]*\([A-Z][A-Z0-9]*-\d+\): ')


def _trunk_subjects(wt, remote_sha, trunk):
    """The subjects of the trunk's commits since ``remote_sha``'s base on it: a remote commit
    under one of them (reworded or not) is the trunk's own change, landed there under another
    patch (a product's T-0338/T-0349: ``(#804)``, ``(#827)``)."""
    base = _git(['merge-base', remote_sha, trunk], wt).stdout.strip()
    if not base:
        return set()
    p = _git(['log', '--no-merges', '--format=%s', f'{base}..{trunk}'], wt)
    return {ln for ln in p.stdout.splitlines() if ln.strip()} if p.returncode == 0 else set()


def _empty_commit(wt, sha):
    """True for a commit that changes nothing — a cloud session's ``asf: report`` commit made
    ``--allow-empty``: no work of anyone's to lose."""
    tree = _git(['rev-parse', '-q', '--verify', f'{sha}^{{tree}}'], wt).stdout.strip()
    parent = _git(['rev-parse', '-q', '--verify', f'{sha}^1^{{tree}}'], wt).stdout.strip()
    return bool(tree) and tree == parent


def _net_change_kept(wt, new, remote_sha, base):
    """True when the old tip's whole net change against its trunk base, ``base..remote_sha``,
    is already in ``new``: a three-way merge of it onto ``new`` merges cleanly and changes
    nothing. The branch's work reached the trunk in another form (a product's F-0094: a sibling
    lane's "carry the approved spec" commit landed its spec and band rows), so its own commits
    dropped on the rebase lose nothing, whatever their patches. Merge-free: ``git merge-tree``
    writes only objects; an old git without it answers False."""
    if not base:
        return False
    p = _git(['merge-tree', '--write-tree', f'--merge-base={base}', new, remote_sha], wt)
    tree = p.stdout.split('\n', 1)[0].strip()
    if p.returncode != 0 or not tree:
        return False
    return tree == _git(['rev-parse', '-q', '--verify', f'{new}^{{tree}}'], wt).stdout.strip()


def _trunk_covered_tree(wt, remote_sha, trunk, base):
    """The tree of the old tip's net change, ``base..remote_sha``, merged onto the trunk — a
    plain three-way merge, no side winning a conflict — and the paths it left conflicted:
    ``(tree, conflicted)``, or ``('', set())`` when it cannot be read. A path that merges
    cleanly to the trunk's own blob holds no branch change the trunk lacks: every hunk of it is
    already in the trunk. A hunk the trunk left alone survives the merge, and a line both sides
    wrote their own way conflicts, so either keeps the path apart from the trunk (``-X ours``
    let the trunk win such a conflict, and a person's unique line inside a block both sides
    wrote was published away with it). Merge-free: ``git merge-tree`` writes only objects."""
    if not base:
        return '', set()
    p = _git(['merge-tree', '--write-tree', '--name-only', '--no-messages',
              f'--merge-base={base}', trunk, remote_sha], wt)
    lines = p.stdout.splitlines()
    if p.returncode not in (0, 1) or not lines or not lines[0].strip():
        return '', set()
    return lines[0].strip(), {ln for ln in lines[1:] if ln.strip()}


def _superseded_by_trunk(wt, sha, new, trunk, covered, base='', remote_sha=''):
    """True when ``sha``'s change is already the trunk's (a product's #529, F-0097's plan branch:
    spec commits whose content later landed on the trunk through another lane — no patch copy,
    each a conflict when carried). Every path ``sha`` touches must be, at ``new``, the trunk's
    own blob (the head kept nothing of its own there — present or absent alike), and the
    branch's whole net change to that path must merge onto the trunk cleanly and leave the
    trunk's blob (:func:`_trunk_covered_tree`): the trunk holds every line of it. A change the
    trunk wrote its own way — a conflict — is never judged superseded, however much of the
    file agrees: which side's line should survive is a person's call, so its commit stays
    unaccounted, carried by :func:`carry_onto_head` or refused. The old tip is archived before
    the push, as for every accounted rebase."""
    tree, conflicted = covered or ('', set())
    if not tree or not base or not remote_sha:
        return False
    files = _git(['diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '--root',
                  sha], wt)
    paths = [ln for ln in files.stdout.splitlines() if ln.strip()]
    if files.returncode != 0 or not paths:
        return False
    for path in paths:
        if path in conflicted:
            return False
        at_trunk = _blob(wt, trunk, path)
        if _blob(wt, new, path) != at_trunk or _blob(wt, tree, path) != at_trunk:
            return False
    return True


def _blob(wt, rev, path):
    """The blob id of ``path`` at ``rev`` (a commit or a tree), '' when it is absent there."""
    return _git(['rev-parse', '-q', '--verify', f'{rev}:{path}'], wt).stdout.strip()


def _resolved_to_trunk(wt, sha, new, trunk, base=''):
    """True when ``sha`` only touched files that are the branch's own — added by it, or (given
    the old tip's trunk ``base``) absent there — which the trunk has since added its own copy
    of, and ``new`` reads the trunk's: an add/add the rebase resolved in the trunk's favour, the
    commit dropped as empty (a product's F-0037: the trunk landed its own plan at the path the
    branch's plan commit added; F-0014: a later revision of the plan was carried onto the trunk,
    and the branch's follow-up fix to its copy dropped with it). A commit that changed a file
    the trunk already had stays lost work. The old tip is archived before the push, so nothing
    is erased."""
    files = _git(['diff-tree', '--no-commit-id', '--name-status', '-r', '--root', sha], wt)
    rows = [ln.split('\t', 1) for ln in files.stdout.splitlines() if ln.strip()]
    if files.returncode != 0 or not rows:
        return False
    for row in rows:
        if len(row) != 2 or row[0] not in ('A', 'M'):
            return False
        if row[0] == 'M' and (not base or _git(['cat-file', '-e', f'{base}:{row[1]}'],
                                                wt).returncode == 0):
            return False
        a = _git(['rev-parse', '-q', '--verify', f'{new}:{row[1]}'], wt).stdout.strip()
        b = _git(['rev-parse', '-q', '--verify', f'{trunk}:{row[1]}'], wt).stdout.strip()
        if not b or a != b:
            return False
    return True


def rebase_of(wt, new, remote_sha, main='main'):
    """True when ``new`` is a rebase of ``remote_sha`` onto a newer trunk base — progress, though
    no commit of the session's own is new: ``new`` is not ``remote_sha`` nor behind it, its base
    on ``origin/<main>`` is at or past ``remote_sha``'s, and every commit of ``remote_sha`` it
    lacks is accounted for (:func:`lost_commits` ``[]``, or :func:`rebased_off_copies`). A
    product's F-0037, 2026-09-27: a clean rebase with ``commits: none`` was counted an empty run
    and parked by the loop guard, and never published."""
    if not wt or not new or not remote_sha or new == remote_sha:
        return False
    if _git(['merge-base', '--is-ancestor', new, remote_sha], wt).returncode == 0:
        return False  # behind origin, or the same commit
    trunk = f'refs/remotes/origin/{main}'
    if _git(['merge-base', '--is-ancestor', remote_sha, new], wt).returncode == 0:
        return True  # a fast-forward: plainly moved
    old_base = _git(['merge-base', remote_sha, trunk], wt).stdout.strip()
    new_base = _git(['merge-base', new, trunk], wt).stdout.strip()
    if not old_base or not new_base or \
            _git(['merge-base', '--is-ancestor', old_base, new_base], wt).returncode != 0:
        return False
    lost = lost_commits(wt, new, remote_sha)
    if lost == []:
        return True
    return bool(lost) and rebased_off_copies(wt, new, remote_sha, lost, main)


def worktree_head(wt):
    """The worktree's HEAD sha, '' when there is none to read."""
    if not wt or not os.path.isdir(wt):
        return ''
    p = _git(['rev-parse', '-q', '--verify', 'HEAD^{commit}'], wt)
    return p.stdout.strip() if p.returncode == 0 else ''


def copies_archive(branch, remote_sha):
    """The archive ref the old tip of a branch rebuilt off trunk copies is kept under."""
    return f'archive/{branch}-copies-{remote_sha[:9]}'


def _redaction_findings(wt, remote_sha):
    """Every :func:`asf.redact` finding on the commits this push would add (F-0035): the
    factory's own scan of the branch diff, run before it pushes, so a worker-account name or a
    machine path that would trip the product's own redact hook is named precisely up front —
    never spent as a push, a generic refusal, and a round to work out why. No config and no
    forbidden-names list is nothing to look for, not an error; a scanner that could not run costs
    nothing here either (the product's own hook, which does run, is the backstop)."""
    from asf import redact
    try:
        pats = redact.default_patterns(wt)
        published = (remote_sha,) if remote_sha else ()
        return redact.scan_unpublished(wt, 'HEAD', pats, published=published)
    except Exception:
        return []


def publish(wt, branch, remote_sha='', main='main', protected=None, push_timeout_s=None):
    """Push the worktree's HEAD to ``origin/<branch>`` as the factory (B-0056).

    A rebased lane branch — spawn's takeover rebase (B-0046, B-0048) or a conflict the session
    finished resolving — holds commits origin does not, and no push a session may make brings
    them there: a plain push is not a fast-forward and the standing rules and the worker settings
    forbid a force. Told "push the same branch, never a force", a session did the one thing left
    and merged its own stale remote (eight spec branches, 13 to 20 commits of tangle each). So
    the factory publishes, never the session: ``--force-with-lease=<branch>:<remote_sha>`` when
    the branch is on origin (origin moving since the evidence was gathered refuses the push —
    nothing is overwritten unseen), a plain push when it is not. The trunk is never a target.
    A lease guards only against origin moving after ``remote_sha`` was read; a head that lacks
    commits ``remote_sha`` holds is refused before any push (:func:`lost_commits`), with the
    commits it would erase named — by which side of the trunk it sits on:

    * on the remote's own trunk base, or behind it: a stale worktree, and origin moved ahead —
      the factory rebases it onto ``origin/<branch>`` (:func:`rebase_onto_remote`) and pushes
      the fast-forward; a conflict is a hold naming the files and that branch;
    * past the remote's base (:func:`past_remote_on_trunk`): the lane's "rebase onto
      origin/<main>" arriving. Every remote commit it drops must be accounted for
      (:func:`rebased_off_copies`: the trunk's, or the branch's own rewritten): then the old tip
      is archived (:func:`copies_archive`) and the head pushed over it under the lease. One not
      accounted for is carried onto the head by the factory (:func:`carry_onto_head`, a
      cherry-pick) and published the same way; one that does not apply refuses with "rebase
      onto origin/<main> again and carry it" — the head is never rebased onto the stale
      remote, whose trunk history is what the rebase was for.
      Done the other way (a product's T-0338, 2026-09-26/27), the trunk commits replayed over
      their own copies, conflicted, and the hold said "rebase onto origin/<branch>" — the
      opposite of the lane's instruction — for sixty runs.

    The old tip's archive carries no new code and skips the product's pre-push hook; the
    branch's own push runs it. Each push is killed after ``push_timeout_s`` (default
    :func:`asf.gitpush.push_timeout`): a hook or a network hang never holds the tick.
    Before any push, the archive included, :func:`_redaction_findings` runs the same scan the
    hook would; a finding refuses the push right there with a precise ``redact: <file>:<line>
    …`` correction (:func:`asf.redact.correction`, F-0035) rather than the hook's generic
    refusal. A push the hook itself still refuses over a redaction (some other repo, some other
    pattern list) has its captured output parsed the same way
    (:func:`asf.redact.parse_finding_lines`) before falling back to its raw last line.
    ``(ok, line)``."""
    if not branch or branch == main:
        return False, f'publish refused: {branch or "no branch"} is not a lane branch'
    from asf import gitpush, redact, refguard
    limit = push_timeout_s or gitpush.push_timeout()
    guard = refguard.refusal(branch, f'publish {branch}', main, protected)
    if guard:
        return False, guard
    ref = f'refs/heads/{branch}'
    rebased, archive = '', ''
    if remote_sha:
        head = _git(['rev-parse', 'HEAD'], wt).stdout.strip()
        lost = lost_commits(wt, head, remote_sha, branch)
        left = unaccounted_commits(wt, head, remote_sha, lost, main) if lost else lost
        if lost and left == []:
            # the answer to a trunk-copies hold: the old tip is kept, then replaced
            archive = copies_archive(branch, remote_sha)
            if refguard.refusal(archive, f'archive {branch}', main, protected):
                return False, f'publish {branch} refused: {archive} is a protected ref'
            rebased = f'rebased off trunk copies (old tip kept as {archive})'
            lost = []
        elif lost and past_remote_on_trunk(wt, head, remote_sha, main):
            # the lane's rebase, with a commit origin/<branch> holds dropped on the way: the
            # factory carries it onto the head — never the head back onto the stale remote —
            # and one it cannot carry is named, with the trunk as the instruction
            subjects = _subjects(wt, left)
            if not carry_onto_head(wt, left):
                why = loss_refusal(branch, left, main, subjects)
                return False, f'publish {branch} refused: {why}'
            archive = copies_archive(branch, remote_sha)
            if refguard.refusal(archive, f'archive {branch}', main, protected):
                return False, f'publish {branch} refused: {archive} is a protected ref'
            named = ', '.join(f'{s} {subjects[s]}' if subjects.get(s) else s for s in left)
            rebased = (f'carried {len(left)} commit(s) the rebase dropped ({named}) '
                       f'(old tip kept as {archive})')
            lost = []
        if lost is None or lost:
            ok, fetched, rebased = rebase_onto_remote(wt, branch)
            if not ok:
                return False, f'publish {branch} refused: {rebased or loss_refusal(branch, lost)}'
            remote_sha = fetched
    findings = _redaction_findings(wt, remote_sha)
    if findings:
        return False, f'publish {branch} refused: ' + '; '.join(redact.correction(findings))
    if archive:
        a = gitpush.push(['-q', 'origin', f'{remote_sha}:refs/heads/{archive}'], wt,
                         refs_only=True, timeout=limit)
        if a.returncode != 0:
            return False, f'publish {branch} refused: the old tip could not be archived'
    args = ['-q', 'origin', f'HEAD:{ref}']
    if remote_sha:
        args.insert(1, f'--force-with-lease={ref}:{remote_sha}')
    p = gitpush.push(args, wt, timeout=limit)
    if p.returncode != 0:
        hook_findings = redact.parse_finding_lines(f'{p.stderr or ""}\n{p.stdout or ""}')
        if hook_findings:
            return False, f'publish {branch} refused: ' + '; '.join(redact.correction(hook_findings))
        why = [ln for ln in (p.stderr or p.stdout).splitlines() if ln.strip()]
        return False, f'publish {branch} refused: {why[-1].strip() if why else "push failed"}'
    head = _git(['rev-parse', '--short', 'HEAD'], wt).stdout.strip()
    if rebased:
        return True, f'{rebased} and pushed: published {branch} at {head}'
    return True, f'published {branch} at {head}' + (' (rebased; lease held)' if remote_sha else '')


#: The line a publish refused for a rebase onto the remote head that conflicted carries.
REBASE_CONFLICT_RE = re.compile(r'\brebase conflicts in: ')


def rebase_onto_remote(wt, branch):
    """``(ok, remote_sha, line)``: rebase the worktree's clean HEAD onto a freshly fetched
    ``origin/<branch>`` — the factory's own answer to a head that lacks commits origin holds.
    Sessions sent back "rebase onto origin/…, then push" failed at it round after round, each
    round an expensive session; the rebase is deterministic, so the factory does it. Never a
    merge, never a force: the push that follows is a fast-forward of the fetched head. For a
    head on the remote's own trunk base or behind it only: a head past the remote on the trunk
    (:func:`past_remote_on_trunk`) is :func:`publish`'s to archive over or refuse, never to
    bring here — its trunk commits would replay over their own stale copies.

    ``ok`` with the fetched sha and ``rebased onto origin/<branch> (+N remote commits)``; a
    conflict is aborted (the worktree is left as it was) and ``line`` is ``rebase conflicts in:
    a, b``; a dirty tree or a failed fetch gives ``(False, '', '')`` — the caller keeps its own
    refusal."""
    st = _git(['status', '--porcelain'], wt)
    if st.returncode != 0 or st.stdout.strip():
        return False, '', ''
    tracking = f'refs/remotes/origin/{branch}'
    if _git(['fetch', '-q', 'origin', f'+refs/heads/{branch}:{tracking}'], wt).returncode != 0:
        return False, '', ''
    fetched = _git(['rev-parse', tracking], wt).stdout.strip()
    if not fetched:
        return False, '', ''
    n = _count(_git(['rev-list', '--count', f'HEAD..{tracking}'], wt))
    r = _git(['rebase', '-q', tracking], wt)
    if r.returncode != 0:
        files = _git(['diff', '--name-only', '--diff-filter=U'], wt).stdout.split()
        _git(['rebase', '--abort'], wt)
        return False, '', (f'rebase conflicts in: {", ".join(files)}' if files else '')
    return True, fetched, f'rebased onto origin/{branch} (+{n} remote commits)'


def rebase_conflict(text):
    """True for a publish refused because the factory's rebase onto the remote head conflicted."""
    return bool(REBASE_CONFLICT_RE.search(text or ''))


def rebase_conflict_text(branch, line):
    """The correction a run whose factory rebase onto ``origin/<branch>`` conflicted hands its
    next session: the conflicting files, named."""
    files = (line or '').split('rebase conflicts in: ', 1)[-1]
    return (f'origin/{branch} holds commits this worktree lacks and the factory\'s rebase onto it '
            f'conflicted — rebase conflicts in: {files}. Rebase onto origin/{branch}, resolve '
            f'those files, and commit; never a force, never a merge')


def _common_git_dir(wt):
    """The repository ``wt`` is a checkout of — its own ``.git`` directory, or the one its
    ``.git`` file's worktree points into (``commondir``) — or None when that cannot be read off
    the files (the caller then asks git itself)."""
    dot = os.path.join(wt, '.git')
    try:
        if os.path.isdir(dot):
            return os.path.realpath(dot)
        with open(dot, encoding='utf-8') as f:
            first = f.readline().strip()
        if not first.startswith('gitdir:'):
            return None
        gd = first[len('gitdir:'):].strip()
        gd = gd if os.path.isabs(gd) else os.path.join(wt, gd)
        common = os.path.join(gd, 'commondir')
        if os.path.isfile(common):
            with open(common, encoding='utf-8') as f:
                rel = f.read().strip()
            return os.path.realpath(rel if os.path.isabs(rel) else os.path.join(gd, rel))
        return os.path.realpath(gd)
    except OSError:
        return None


class RemoteHeads:
    """``origin``'s heads for one pass over many worktrees: one ``git ls-remote --heads origin``
    per repository, where the pass used to ask once per worktree (a network round trip and a
    git process each — thirty-odd a health pass). Only for a pass that pushes nothing between
    its questions: the answer is origin as it stood at the first one."""

    _GLOB = frozenset('*?[\\')

    def __init__(self):
        self._by_repo = {}

    def sha(self, wt, branch):
        """What ``git ls-remote --heads origin <branch>`` run in ``wt`` would give as its first
        sha — git matches the pattern against the tail of each ref (``refs/heads/x/<branch>``
        too), first in ref order — ``''`` when no head matches; None when the snapshot cannot
        answer (a glob in the name, a checkout it cannot place, ``ls-remote`` failed)."""
        if not branch or self._GLOB & set(branch):
            return None
        repo = _common_git_dir(wt)
        if repo is None:
            return None
        if repo not in self._by_repo:
            p = _git(['ls-remote', '--heads', 'origin'], wt)
            self._by_repo[repo] = ([tuple(ln.split('\t', 1)) for ln in p.stdout.splitlines()
                                    if '\t' in ln] if p.returncode == 0 else None)
        refs = self._by_repo[repo]
        if refs is None:
            return None
        tail = '/' + branch
        return next((sha for sha, ref in refs if ref.endswith(tail)), '')


def gather(product, run, alive=None, worktree=None, heads=None):
    """The :class:`Evidence` for ``run`` — git in its worktree (``run['worktree']`` unless given),
    ``origin/<branch>`` from the product repo's remote, the log through the runtime. ``heads``
    (a :class:`RemoteHeads`) answers ``origin/<branch>`` for a pass over many worktrees."""
    alive = alive or pid_alive
    ev = Evidence(result=runtime_mod.read_result(run.get('log')), alive=alive(run.get('pid')))
    wt = worktree or run.get('worktree')
    branch = run.get('branch')
    main = getattr(product, 'main', 'main') or 'main'
    if not wt or not os.path.isdir(wt):
        return ev
    st = _git(['status', '--porcelain'], wt)
    if st.returncode != 0:
        return ev
    ev.worktree = True
    ev.uncommitted = len([ln for ln in st.stdout.splitlines() if ln.strip()])
    if branch:
        known = heads.sha(wt, branch) if heads is not None else None
        if known is None:
            ls = _git(['ls-remote', '--heads', 'origin', branch], wt)
            known = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''
        ev.remote_sha = known
        log = _git(['reflog', 'show', '--format=%gs', f'refs/heads/{branch}'], wt)
        ev.has_commits = log.returncode == 0 and any(
            not ln.startswith('branch: Created from') for ln in log.stdout.splitlines() if ln.strip())
    if ev.remote_sha:
        ev.head_on_remote = _git(['merge-base', '--is-ancestor', 'HEAD', ev.remote_sha], wt).returncode == 0
        ev.unpushed = unpushed_commits(wt, ev.remote_sha, main)
    else:
        ev.unpushed = _count(_git(['rev-list', '--count', f'origin/{main}..HEAD'], wt))
    ev.in_trunk = _git(['merge-base', '--is-ancestor', 'HEAD', f'origin/{main}'], wt).returncode == 0
    return ev


# ---- the derived state ------------------------------------------------------------

@dataclasses.dataclass
class State:
    name: str
    reason: str = ''
    rounds: int = 0

    def __str__(self):
        return f'{self.name}({self.reason})' if self.reason else self.name


def push_gap(ev):
    """The reason a result that says ok is not ``finished``: what is not on origin (B-0051)."""
    return f'{NOT_PUSHED}: {ev.uncommitted} uncommitted file(s), {ev.unpushed} unpushed commit(s)'


def outcome_class(result):
    """The class of an ``end_reason`` (the ``sessions`` stream's ``result``), or None when the
    line describes no outcome — ``running``, ``unknown``, ``stopped`` (an operator's decision,
    not the factory's outcome), empty. Pure: no clock, no io."""
    text = (result or '').strip().lower()
    if text in ('', 'running', 'unknown', STOPPED):
        return None
    if text == FINISHED:
        return FINISHED
    if text == DEAD_PID:
        return DEAD_PID
    if text == 'failed':
        return OTHER
    if text.startswith('failed: '):
        text = text[len('failed: '):].strip()
    for prefix in (NOT_PUSHED, OUTCOME_CLASSES[2], HOOK_REFUSED, NETWORK_ERROR):
        if text.startswith(prefix):
            return prefix
    return text if text in OUTCOME_CLASSES[3:-1] else OTHER


def push_failure(text):
    """The retry class of a push's failure text — :data:`NETWORK_ERROR` for a transport error,
    :data:`HOOK_REFUSED` for a refusal by the repo's hook — or None for anything else."""
    text = text or ''
    if stale_head(text) or rebase_conflict(text):  # the factory's own refusal: never a retry
        return None
    if NETWORK_RE.search(text):
        return NETWORK_ERROR
    if HOOK_RE.search(text) or REDACT_REFUSAL_RE.search(text):
        return HOOK_REFUSED
    return None


#: Kinds whose work is not a branch: a groom (adjudicate) session, or a groom-clerk half of one,
#: rules into the state dir's answers file and is told the repository is not its work, so it
#: never commits.
NO_LANDING_KINDS = ('groom', 'groom-clerk')


def lands(run, path=None):
    """False for a run whose work is not its branch: a :data:`NO_LANDING_KINDS` run, or any run
    sent back on a branch such a run was launched on (a correction takes the branch, not the
    kind). Such a run is judged on its result alone, never held for an unpushed or empty
    branch."""
    if (run or {}).get('kind') in NO_LANDING_KINDS:
        return False
    branch = (run or {}).get('branch')
    if path and branch:
        for rs in _folded(path).view.values():  # read only
            if any(r.get('branch') == branch and r.get('kind') in NO_LANDING_KINDS for r in rs):
                return False
    return True


def judge(run, ev, landing=None):
    """The ``end_reason`` health records for a run whose session is over, or None while it runs.
    A result that says ok is ``finished`` only when the branch is pushed; a run with no branch
    (nothing to push) is judged on the result alone. A pushed branch never committed to
    (``ev.has_commits``, the reflog beyond its creation — B-0019's distinction from ``in_trunk``,
    which a branch fast-forward-landed onto the trunk is also true of) has nothing for harvest to
    land — read ``finished`` it would sit ``eligible`` forever, blocking every task waiting on the
    item's footprint (B-0076), so it is ``failed: empty branch: nothing to land`` instead, and
    D-0048's loop sends it back to the same session to commit its work or say why there is none."""
    if landing is None:
        landing = lands(run)
    if ev.result is None:
        return None if ev.alive else DEAD_PID
    if not runtime_mod.result_ok(ev.result):
        sig = runtime_mod.failure_reason(ev.result)
        # a run that lands nothing may say `pushed: no` truthfully: that is not unpushed work
        if not (not landing and sig == runtime_mod.report.UNPUSHED
                and not ev.result.get('is_error')
                and ev.result.get('subtype', 'success') == 'success'):
            return f'failed: {sig}' if sig else 'failed'
    if run.get('branch') and landing:
        if not ev.pushed:
            return f'failed: {push_gap(ev)}'
        if not ev.has_commits and not run.get('review_filed'):
            # a review session commits nothing: the review it wrote, filed off the branch
            # (asf.evidence.review_store), is its work
            return f'failed: {EMPTY_BRANCH}'
    return FINISHED


# ---- the session classifier (§2.1-§2.3) ------------------------------------------

@dataclasses.dataclass
class Status:
    """One session's state, as every reader prints it. ``name`` is one of :data:`SESSION_STATES`;
    ``result`` is what the run came to (``finished`` | ``failed: …`` | :data:`PUSHED_NO_REPORT`),
    ``reason`` is why for ``failed`` and ``dead``, ``source`` is what decided it (``'ledger'`` |
    ``'report'`` | ``'branch'`` | ``'pid'``)."""
    name: str
    result: str = ''
    reason: str = ''
    source: str = ''

    @property
    def label(self):
        """What every view prints. One place, so the six readers cannot word it six ways.

        A ``failed`` status' ``reason`` is the ledger's own ``end_reason`` (:func:`judge` already
        spells it ``'failed: …'``), so the label is that text verbatim — prefixing it again would
        print ``failed: failed: …``."""
        if self.name == ENDED_AWAITING_TICK:
            return f'ended, awaiting tick ({self.result})'
        if self.name == FAILED:
            return self.reason
        if self.name == DEAD:
            return f'dead: {self.reason}'
        return self.name


def classify(run, ev, path=None):
    """One run and its evidence to one :class:`Status` (§2.1). Pure: no git, no clock, no file —
    ``judge`` and ``lands`` are the only calls, and ``path`` is passed through to ``lands`` exactly
    as :func:`derive` passes it today."""
    if run.get('ended'):
        reason = run.get('end_reason') or DEAD_PID
        if reason == FINISHED:
            return Status(FINISHED, result=FINISHED, source='ledger')
        if reason == STOPPED:
            return Status(DEAD, result=STOPPED, reason=STOPPED_BY_OPERATOR, source='ledger')
        if is_dead_reason(reason):
            if ev.result is not None:
                return Status(ENDED_AWAITING_TICK, result=judge(run, ev, landing=lands(run, path)),
                              source='report')
            return Status(DEAD, reason=NO_RECORD, source='ledger')
        return Status(FAILED, reason=reason, result=reason, source='ledger')
    if ev.alive:
        return Status(WORKING, source='pid')
    if ev.result is not None:
        return Status(ENDED_AWAITING_TICK, result=judge(run, ev, landing=lands(run, path)),
                      source='report')
    if ev.remote_sha and ev.has_commits:
        return Status(ENDED_AWAITING_TICK, result=PUSHED_NO_REPORT, source='branch')
    return Status(DEAD, reason=NO_RECORD, source='pid')


def state_of(product, run, alive=None, path=None, gather_fn=None):
    """The one impure entry point (§2.2's evidence ladder): gathers no more evidence than the
    answer needs, then :func:`classify`. A recorded run that is not a recorded death classifies
    against ``Evidence()`` and reads nothing; a recorded death reads the log's result line alone;
    a run with no ``ended`` whose pid answers classifies against ``Evidence(alive=True)``; only a
    run with no ``ended`` whose process is gone calls :func:`gather`. ``gather_fn`` exists for a
    test that asserts git is never reached otherwise."""
    run = run or {}
    alive = alive or pid_alive
    if run.get('ended'):
        reason = run.get('end_reason') or DEAD_PID
        ev = Evidence(result=result_of(run)) if is_dead_reason(reason) else Evidence()
        return classify(run, ev, path=path)
    if alive(run.get('pid')):
        return classify(run, Evidence(alive=True), path=path)
    return classify(run, (gather_fn or gather)(product, run, alive=alive), path=path)


def holds_seat(status):
    """The one definition of a taken slot: the classifier's ``working``, and nothing else.
    ``status`` may be a :class:`Status` or its bare ``name`` string."""
    name = status.name if isinstance(status, Status) else status
    return name == WORKING


def seats(rows):
    """The rows of an inflight list that hold a seat (:func:`holds_seat`). A row carrying no
    ``state`` counts as one (§1.4): a hand-written ``--inflight`` row is a live guess, and the
    conservative reading does not over-launch."""
    return sum(1 for r in rows if 'state' not in r or holds_seat(r['state']))


def derive(run, ev, cap=ROUND_CAP, path=None):
    """The state of ``run`` given its evidence. Pure: no git, no clock."""
    if landed(run):
        return State(LANDED if ev.worktree else REAPED)
    corr = pending_correction(run, path)
    rounds = run.get('rounds') or 0
    if corr:
        if corr.get('kind') not in MECHANICAL and (
                corr.get('at_cap') or repeats(corr) >= cap and not ruled(path, run.get('item'), corr)):
            return State(ADJUDICATE, corr.get('text', ''), rounds)
        return State(HELD, corr.get('text', ''), rounds)
    if (run.get('correction') or {}).get('text'):
        return State(CORRECTED, run['correction'].get('text', ''), rounds)
    status = classify(run, ev, path=path)
    if status.name == WORKING:
        return State(RUNNING if ev.has_commits else LAUNCHED)
    if status.result == FINISHED:
        return State(PUSHED, FINISHED, rounds)
    return State(ENDED, status.result or status.reason, rounds)


# ---- stop: the one place that stops a run (B-0069) ------------------------------------

def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(path, run, grace=5.0, poll=0.1, alive=None, tip=None, sleep=time.sleep):
    """``(ok, detail)``: stop ``run`` — signal its whole process group (``pgid``, else the pid,
    which is the group id because spawn uses ``setsid``), TERM then KILL, wait for the group to
    go, then for ``grace`` seconds verify the log stays quiet and ``tip()`` (the branch tip on
    origin, when given) does not move. Only then is ``ended``/``end_reason: stopped`` recorded,
    with ``stop_tip`` so a later push by the stopped run is caught (:func:`pushed_after_stop`)."""
    if cloudpid.is_token(run.get('pid')):  # no process here: the cloud lane stops it
        from asf.workers import cloud  # local: cloud imports the runtime this module imports
        ok, detail = cloud.stop(run)
        line = {'job': run['job'], 'ended': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'end_reason': STOPPED}
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, sort_keys=True) + '\n')
        return ok, detail
    pgid = run.get('pgid') or run.get('pid')
    if not pgid:
        return False, 'no pid or pgid recorded'
    pgid = int(pgid)
    pid_up = alive or (lambda _pid: _group_alive(pgid))

    def gone():
        return not _group_alive(pgid) or (alive is not None and not pid_up(run.get('pid')))

    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except PermissionError as e:
            return False, f'cannot signal group {pgid}: {e}'
        deadline = time.monotonic() + wait
        while not gone() and time.monotonic() < deadline:
            sleep(poll)
        if gone():
            break
    if not gone():
        return False, f'group {pgid} still alive after SIGKILL'

    def mtime():
        try:
            return os.stat(run['log']).st_mtime_ns if run.get('log') else None
        except OSError:
            return None
    before, tip_before = mtime(), (tip() if tip else '')
    sleep(grace)
    if mtime() != before:
        return False, 'log still moving after the group died'
    tip_after = tip() if tip else ''
    if tip_after != tip_before:
        return False, 'branch moved after the group died'
    line = {'job': run['job'], 'ended': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'end_reason': STOPPED, 'pgid': pgid}
    if tip_after:
        line['stop_tip'] = tip_after
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(line, sort_keys=True) + '\n')
    return True, f'stopped group {pgid}'


def pushed_after_stop(run, ev):
    """True when a stopped run's branch tip is not the one recorded at stop."""
    return (run.get('end_reason') == STOPPED and bool(run.get('stop_tip'))
            and bool(ev.remote_sha) and ev.remote_sha != run['stop_tip'])


# ---- the hold: one rule for harvest's gate and health's unpushed verdict --------------

UNPUSHED = 'unpushed'
EMPTY = 'empty'        #: a correction kind of its own: `UNPUSHED` is work not on origin, this is no work
EMPTY_CAP = 2          #: ends that wrote nothing before the item is parked (`conventions.empty_cap`)
#: the lane's refusal of commits that do not name their item: a mechanical defect the lane
#: rewords itself (asf.harvest.lane); a hold of it spends no round and never reaches adjudicate
NAMING = 'naming'
#: the lane's trunk-copies rebuild (asf.harvest.lane.drop_trunk_copies) conflicted: a mechanical
#: rebase its session does and the factory publishes — like NAMING, no round, never adjudicate
COPIES = 'copies'
#: the correction kinds that spend no round and never reach adjudicate
MECHANICAL = (NAMING, COPIES)
#: the lane's hold of a delivery branch (``delivers:``) a member of which no commit names while
#: its report does not say ``done`` — a crash, a run cap, ``status: partial``: no PR opens; the
#: same lead comes back to a session that continues from the branch's head
#: (asf.harvest.lane.incomplete_refusal, the feeder's DELIVERY → CODE row)
INCOMPLETE = 'incomplete'
#: incomplete holds in a row on the same unbuilt Tasks — the first run and one resume that
#: moved no Task forward — before the lead is parked (the operator's "pause after the 2nd
#: failure"); a resume that commits one more Task is a new finding and resumes again
INCOMPLETE_CAP = 2
#: A run that ended with nothing to land while its own report declared a question for a person:
#: relaunching it buys the same report again, so the item is parked until its card changes
#: (F-0126) or `asf unpark` releases it.
BLOCKED = 'blocked'


def incomplete_park_text(n, missing, item):
    return (f'delivery incomplete {n} times in a row with no new Task on the branch '
            f'(still unbuilt: {", ".join(missing or ()) or "?"}): the lead is parked, not '
            f'resumed again. Read the last run\'s report for why it stopped, fix that, then '
            f'`asf unpark {item}`')


def empty_ends(path, item):
    """How many runs of ``item`` ended ``failed: empty branch: nothing to land``."""
    if not item:
        return 0
    return sum(1 for r in item_runs(path, item)
               if r.get('end_reason') == f'failed: {EMPTY_BRANCH}')


def park_text(n):
    return (f'ended empty {n} times: nothing was written on any of them — the Task is parked. '
            f'Check whether its work is already on the trunk, then close it, reshape its plan, '
            f'or `asf unpark <item>` to let the wave try again')


def card_fingerprint(product, item_id, items):
    """A short digest of what ``item_id``'s card states — '' for a falsy ``item_id`` or a
    ``None`` ``items`` (a tick that could not read the record clone parks and releases nothing,
    rather than raising). It is the digest a brief is already cut from
    (:func:`asf.briefs.build.card_digest`): ``## History``, the machine block, ``state``,
    ``evidence``, ``stage_since`` and ``updated`` are outside it by ``DIGEST_FIELDS``' own rule,
    and a park that lifted on those would be no park at all (D9)."""
    if not item_id or items is None or item_id not in items:
        return ''
    import importlib
    digest = importlib.import_module('asf.briefs.build').card_digest
    return digest(product, item_id, items)


def blocked_park_text(question, item):
    return (f'the session ended with nothing to land and declared: {question} — the item is '
            f'parked, not handed to another session. Re-cut its card (a Task with no writes: '
            f'is re-cut by its RESHAPE → PLAN session), or `asf unpark {item}` to release it')


def blocked_park(question, item, card, now):
    """``(fields, line)``: the pending correction that parks a run whose report declared a
    question, and the line to print. A function of its own and not a branch of :func:`hold`,
    because ``hold`` spends a round and counts findings and this spends neither."""
    reason = blocked_park_text(question, item)
    fields = {'correction': {'kind': BLOCKED, 'text': question, 'at': now, 'parked': True,
                             'reason': reason, 'card': card}, 'operator_flagged': 1}
    return fields, f'parked {item}: {reason}'


#: Sessions of one kind handed the same head in a row before the item is parked (the loop guard).
LOOP_CAP = 3


#: The session kind a mechanical hold routes to (the feeder's FIX → CORRECT row): it never
#: reaches adjudicate, so launches of another kind say nothing about whether it loops.
CORRECT = 'correct'


def same_head_loop(path, run, head=None, cap=LOOP_CAP, kind=None, main=None):
    """The sha ``run``'s item is looping on, or None: its last ``cap`` runs (a spent window's
    excepted) are all of ``kind`` — the kind the hold routes to, ``run``'s own when not given —
    and were all launched on one head (spawn's ``launch_head``) — so none of the first
    ``cap - 1`` added a commit — and the branch still sits on it when ``head`` is known (the
    last one added none either). A product, 2026-09-26: ``adjudicate-b-1377`` ×14 and
    ``correct-t-0338`` ×12, every one on a head nothing moved. Only runs started after the
    item's latest ``asf unpark`` (its ``unparked`` stamp) count: an unpark releases the item,
    and recounting the launches it released re-parked T-0338 on the very next tick.

    A worktree whose HEAD is a rebase of that sha onto a newer trunk (:func:`rebase_of`, with
    ``main`` given) moved the branch, though no commit is new: not a loop (a product's F-0037)."""
    item = (run or {}).get('item')
    kind = kind or (run or {}).get('kind')
    if not item or not kind or not path:
        return None
    all_runs = item_runs(path, item)
    since = max((r.get('unparked') or '' for r in all_runs), default='')
    rs = sorted((r for r in all_runs if not quota_exhausted(r)
                 and (r.get('started') or '') > since),
                key=lambda r: r.get('started') or '')[-cap:]
    heads = {r.get('launch_head') or '' for r in rs}
    if len(rs) < cap or len(heads) != 1 or '' in heads \
            or any(r.get('kind') != kind for r in rs):
        return None
    sha = heads.pop()
    if head and head != sha:
        return None
    if main:
        wt = (run or {}).get('worktree')
        if rebase_of(wt, worktree_head(wt), sha, main):
            return None
    return sha


def loop_text(n, kind, sha, item):
    return (f'{kind} launched {n} times on {sha[:9]} and no session added a commit: the item is '
            f'parked, not handed to a {n + 1}th session. Read the last run\'s report for why it '
            f'could not move the branch, fix that, then `asf unpark {item}`')


def hold(path, run, kind, text, now, empty_cap=EMPTY_CAP, head=None, finding=None, main=None):
    """``(fields, line)``: what to append to ``run`` to hold its branch and hand it back, and
    the line to print. The rounds counter runs over every run of the item and stops climbing at
    :data:`ROUND_CAP` (B-0048). The escalation is counted on the hold's finding instead
    (:func:`next_finding`; ``finding``: its keys when the caller knows them — a review's C list):
    at :data:`ROUND_CAP` holds in a row on one finding the correction is marked ``at_cap`` so the
    feeder's ADJUDICATE row takes it, and a second hold at the cap flags the operator instead of
    spawning another. A different finding goes back to a correct session, whatever the rounds.
    An :data:`EMPTY` hold at ``empty_cap`` empty ends parks the item instead and spends no round,
    and so does the loop guard (:func:`same_head_loop`): the same kind handed the same head
    :data:`LOOP_CAP` times — ``head``, when the caller knows it, is where the branch sits now;
    ``main``, when given, lets a worktree rebased onto a newer trunk count as a moved head."""
    item = run.get('item')
    branch = run.get('branch') or run.get('job')
    routes_to = CORRECT if kind in MECHANICAL else run.get('kind')
    loop = same_head_loop(path, run, head, kind=routes_to, main=main)
    head = (text or '').split('\n', 1)[0]  # the line is one line; the correction keeps it all
    if loop:
        reason = loop_text(LOOP_CAP, routes_to, loop, item)
        fields = {'correction': {'kind': kind, 'text': text, 'at': now, 'parked': True,
                                 'reason': reason, 'loop_head': loop},
                  'operator_flagged': 1}
        return fields, f'parked {branch}: {reason}'
    if kind == EMPTY and empty_ends(path, item) >= empty_cap:
        fields = {'correction': {'kind': kind, 'text': text, 'at': now,
                                 'parked': True, 'reason': park_text(empty_ends(path, item))},
                  'operator_flagged': 1}
        return fields, f'parked {branch}: {fields["correction"]["reason"]}'
    if kind in MECHANICAL:  # the lane's reword or rebuild failed: back to its session, no round
        fields = {'correction': {'kind': kind, 'text': text, 'at': now}}
        return fields, f'held {branch}: {head} — back to its session ({kind}, no round)'
    keys, same = next_finding(path, run, kind, text, finding)
    corr = {'kind': kind, 'text': text, 'at': now, 'finding': keys, 'same': same}
    if kind == INCOMPLETE and same >= INCOMPLETE_CAP:
        # a resumed delivery left the very same Tasks unbuilt: pause after the second failure
        reason = incomplete_park_text(same, keys, item)
        fields = {'correction': dict(corr, parked=True, reason=reason), 'operator_flagged': 1}
        return fields, f'parked {branch}: {reason}'
    prev = max([rounds_of(path, item), run.get('rounds') or 0])
    fields = {'correction': corr}
    if prev < ROUND_CAP:  # B-0048: the counter never passes the cap
        fields['rounds'] = prev + 1
    if same >= ROUND_CAP:
        # the first at_cap hold hands the item to adjudicate; the next is that ruling's own
        # attempt failing; only a third — the adjudicate row cannot land either — flags
        at_cap_before = sum(1 for r in item_runs(path, item)
                            if (r.get('correction') or {}).get('at_cap'))
        corr['at_cap'] = True
        if at_cap_before >= 2:
            fields['operator_flagged'] = 1
        return fields, (f'held {branch}: {head} — adjudicate pending ({kind}: the same finding '
                        f'{same} times in a row)')
    return fields, f'held {branch}: {head} — back to its session (round {min(prev + 1, ROUND_CAP)})'


#: The hold kind a review's changes come back as (:mod:`asf.harvest.lane`): its finding is the C
#: list's files, and a C-item carried over from the last round is the same finding.
REVIEW = 'review'
_RED_RE = re.compile(r'checks red:\s*([^\n]+)')
_CONFLICT_RE = re.compile(r'conflicts in:\s*(.+?)(?:\.\s|\.?$)', re.M)
_SHA_RE = re.compile(r'\b[0-9a-f]{7,40}\b')
_NUM_RE = re.compile(r'\d+')


def _names(raw, stop=(' — ', '. ')):
    for s in stop:
        raw = raw.split(s, 1)[0]
    return sorted({n.strip(' .`') for n in raw.split(',') if n.strip(' .`')})


def finding_of(kind, text, keys=None):
    """What a hold of ``kind`` says is wrong, as a sorted list of keys: ``keys`` when the
    caller knows them (a review's C-list files, :func:`asf.evidence.review.c_items`), else the red
    check set (``checks red: gate, gate-tests``), else the conflicting files (``conflicts in:``),
    else the hold's first line with its shas and numbers blanked — so a repeat of one finding
    reads the same while a new one does not."""
    if keys:
        return sorted(set(keys))
    t = text or ''
    m = _RED_RE.search(t)
    if m and _names(m.group(1)):
        return _names(m.group(1))
    m = _CONFLICT_RE.search(t)
    if m and _names(m.group(1), stop=(' — ',)):
        return _names(m.group(1), stop=(' — ',))
    line = _NUM_RE.sub('<n>', _SHA_RE.sub('<sha>', t.split('\n', 1)[0]))
    return [' '.join(line.split())[:200]]


def same_finding(prev, kind, keys):
    """The finding ``prev`` (an earlier correction) and a hold of ``kind`` on ``keys`` share —
    the keys to carry on — or None when this is a new finding. The hold kind must match; a review
    is the same finding while a C-item survives (the files in common), anything else when its
    keys are equal."""
    if not prev or prev.get('kind') != kind:
        return None
    before = prev.get('finding') or finding_of(prev.get('kind'), prev.get('text'))
    if kind == REVIEW:
        return sorted(set(before) & set(keys)) or None
    return keys if sorted(before) == sorted(keys) else None


def repeats(corr):
    """How many holds in a row ``corr`` is on its one finding (1 for a new one). A correction
    written before the count existed reads 1 — unless it was already marked ``at_cap``."""
    corr = corr or {}
    if corr.get('same'):
        return int(corr['same'])
    return ROUND_CAP if corr.get('at_cap') else 1


def review_answered(path, item, review_path, head):
    """The job of a ``correct`` run that answered the review hold on ``review_path`` without a
    commit, or None (B-0149): the item's latest review correction names ``review_path``, and a
    correct run started after it — launched on ``head``, the branch still there — finished.
    The lane's restack can clear a review's finding (a conflict with the trunk) under it, so the
    session has nothing to change; the review then wants a fresh round, not another correction."""
    if not path or not item or not review_path or not head:
        return None
    rs = item_runs(path, item)
    holds = [(r.get('correction') or {}) for r in rs]
    holds = [c for c in holds if c.get('kind') == REVIEW and c.get('at')]
    if not holds:
        return None
    last = max(holds, key=lambda c: c['at'])
    if not str(last.get('text') or '').startswith(f'{review_path} '):
        return None
    for r in sorted(rs, key=lambda r: r.get('started') or '', reverse=True):
        if (r.get('started') or '') <= last['at']:
            break
        if r.get('kind') == CORRECT and r.get('end_reason') == FINISHED \
                and not quota_exhausted(r) \
                and (r.get('launch_head') or '').lower() == head.lower():
            return r.get('job')
    return None


def next_finding(path, run, kind, text, keys=None):
    """``(keys, same)`` for a new hold on ``run``: its finding and how many holds in a row on
    that finding it is. The item's latest counted correction is the one before; when this hold
    names the same finding, the count climbs only when ``run`` is a later session than the one
    that correction was written on — a session that tried and failed it — and not an adjudicate
    one (it rules, it does not correct) nor one a spent window cut short. A re-hold of the same
    run keeps the count; a new finding starts again at 1."""
    keys = finding_of(kind, text, keys)
    prev, prev_run = None, None
    for r in item_runs(path, run.get('item')):
        c = r.get('correction') or {}
        if not c.get('text') or c.get('parked') or c.get('kind') in MECHANICAL + (FOOTPRINT,):
            continue
        if prev is None or (c.get('at') or '') >= (prev.get('at') or ''):
            prev, prev_run = c, (r.get('job'), r.get('started'))
    carried = same_finding(prev, kind, keys)
    if carried is None:
        return keys, 1
    answered = ((run.get('job'), run.get('started')) != prev_run
                and run.get('kind') != 'adjudicate' and not quota_exhausted(run))
    return carried, repeats(prev) + (1 if answered else 0)


#: A correction kind of its own: the branch needs paths outside its Task's ``writes:`` — the
#: ``widen_footprint`` rule (:mod:`asf.feeder.widen`) answers it, never a plain round.
FOOTPRINT = 'footprint'


def footprint_hold(run, paths, fact, text, now, tests=()):
    """``(fields, line)``: hold ``run``'s branch because it needs ``paths`` outside its Task's
    ``writes:`` — ``fact`` names where that came from (the REPORT, the gate). No round is spent:
    the session did its own part; the footprint was the plan's. The rule decides next (its
    ``verdict`` is written on the same correction once it has)."""
    branch = run.get('branch') or run.get('job')
    fields = {'correction': {'kind': FOOTPRINT, 'text': text, 'at': now, 'needs': list(paths),
                             'fact': fact, 'tests': list(tests)}}
    return fields, (f'held {branch}: footprint needs {" ".join(paths)} ({fact}) — '
                    f'widen_footprint decides')


def hook_refusal_hold(path, run, text, now):
    """``(fields, line)``: ``run``'s push the repo's own pre-push hook refused. The first hold on
    a finding spends no round — the hook, not the session, is what failed, so the item is simply
    relaunched to try again (B-0097). A *second* hold naming the same finding in a row
    (:func:`next_finding`, at :data:`HOOK_REFUSAL_CAP`) is never tried a third time blind: the
    hook has now said the identical thing twice, so this reads what it said and routes by it
    (B-0140) — a redaction finding (:data:`HOOK_REDACTION_RE`) is parked as a security hold, since
    only a person decides what a flagged secret needs, never another session; anything else is
    marked ``at_cap`` so the item goes to ADJUDICATE the way any other stuck finding does
    (:func:`hold`). A lint or test naming paths outside the Task's own ``writes:`` is left as a
    plain ``hook refused`` correction either way: :mod:`asf.tick.widen_footprint` turns that into
    a ``footprint`` hold the same tick, before a wave ever reads this one's ``at_cap``."""
    branch = run.get('branch') or run.get('job')
    keys, same = next_finding(path, run, HOOK_REFUSED, text)
    corr = {'kind': HOOK_REFUSED, 'text': text, 'at': now, 'finding': keys, 'same': same}
    if same < HOOK_REFUSAL_CAP:
        return {'correction': corr}, f'held {branch}: {text} (no round spent)'
    if HOOK_REDACTION_RE.search(text):
        reason = (f'a redaction finding refused the push {same} times running — a person '
                  'decides, not another session')
        corr.update(parked=True, reason=reason)
        return ({'correction': corr, 'operator_flagged': 1},
                f'held {branch}: {reason} (security hold)')
    corr['at_cap'] = True
    return ({'correction': corr},
            f'held {branch}: {text} — adjudicate pending (hook refused the same way {same} '
            f'times in a row)')


def widenings(path, item):
    """How many times ``item``'s footprint was already widened: its runs that carry ``widened``
    (the paths the rule added, written beside the correction and never overwritten by one)."""
    if not item:
        return 0
    return sum(1 for r in item_runs(path, item) if r.get('widened'))


def stale_head_text(branch, line):
    """The correction a run whose publish was refused for a stale head hands its next session.
    The refusal (:func:`loss_refusal`) carries the one move that clears it — onto
    ``origin/<branch>`` for a head behind it, onto ``origin/<main>`` again for a head past it
    on the trunk — and this text repeats it, never a second, contradicting one."""
    why = (line or '').split('refused: ', 1)[-1]
    return (f'origin/{branch} holds commits this worktree lacks: {why}; '
            f'never a force, never a merge')


#: A correction kind of its own: the factory's rebase onto the remote head conflicted.
REBASE_CONFLICT = 'rebase conflict'


def rebase_conflict_hold(path, run, text, now, main=None):
    """``(fields, line)``: hold ``run``'s branch after the factory's rebase onto its remote head
    conflicted. The first such hold on the item spends no round — the session did its part; the
    remote moved under it. A repeat is an ordinary round (:func:`hold`)."""
    item = run.get('item')
    earlier = [r for r in item_runs(path, item) if r is not run
               and (r.get('correction') or {}).get('kind') == REBASE_CONFLICT]
    if earlier or not item:
        return hold(path, run, REBASE_CONFLICT, text, now, main=main)
    branch = run.get('branch') or run.get('job')
    fields = {'correction': {'kind': REBASE_CONFLICT, 'text': text, 'at': now}}
    return fields, f'held {branch}: {text} — back to its session (no round spent)'


def unpushed_text(reason):
    """The correction a run judged ``failed: not pushed: …`` hands its next session."""
    gap = reason.split('failed: ', 1)[-1]
    return f'{UNPUSHED} work: {gap} — commit and push what you have, or say why not in the report'


def empty_branch_text():
    """The correction a run judged ``failed: empty branch: …`` hands its next session (B-0076)."""
    return f'{EMPTY_BRANCH} — commit and push what you have, or say why not in the report'


# ---- what spawn and health ask ---------------------------------------------------

ORPHAN = 'orphan'
BUSY = 'busy'


def launch_verdict(path, job, worktree_path, alive=None):
    """``(what, why)`` for launching ``job`` into ``worktree_path``: ``what`` is ``''`` (go: no
    worktree, or an ended / dead-pid run's worktree, which is reused), :data:`BUSY` (a live run
    holds it) or :data:`ORPHAN` (no run recorded it). The owner is found by path identity
    (:func:`path_key`), so a worktree recorded as ``~/.ASF/…`` is the one spawn names
    ``~/.asf/…`` on a case-insensitive filesystem."""
    if not os.path.exists(worktree_path):
        return '', ''
    run = by_worktree(path).get(path_key(worktree_path)) or latest(path).get(job)
    if run is None:
        return ORPHAN, f'worktree already exists: {worktree_path} — no run recorded it'
    if occupies(run, alive):  # a dead pid's worktree is reused like an ended run's
        return BUSY, (f'worktree already exists: {worktree_path} — held by live run '
                      f'{run.get("job")} (pid {run.get("pid")})')
    return '', ''


def may_launch(path, job, worktree_path, alive=None):
    """``(ok, why)``: a worktree at ``worktree_path`` blocks a launch only while the run that
    owns it is live; an ended run's worktree is reused, a worktree no run recorded is refused
    (an orphan is for the operator, not for a session to inherit). See :func:`launch_verdict`."""
    what, why = launch_verdict(path, job, worktree_path, alive)
    return not what, why


def reap_verdict(run, ev, main, alive):
    """``(what, detail)`` for a worktree: ``opening`` | ``keep`` | ``reapable``. ``run`` is None
    for an orphan. Reapable under three rules — landed (the harvested sha is the evidence,
    B-0049); an ended-not-finished run with nothing in the tree to lose (B-0025); a finished run
    whose pushed branch has commits and reached the trunk (B-0019)."""
    if run is not None and is_live(run):
        return ('opening', 'live session, no commits yet') if not ev.has_commits else (None, None)
    what = 'orphan' if run is None else 'ended'
    if run is not None and alive(run.get('pid')):
        return 'keep', f'{what}: pid still alive'
    if landed(run):
        return 'reapable', f'landed {run["harvested"]}'
    if run is not None and run.get('end_reason') != FINISHED:
        if ev.uncommitted == 0 and ev.in_trunk:
            return 'reapable', 'empty'
        return 'keep', f'{what}: session {run.get("end_reason")}, not finished'
    if ev.uncommitted:
        return 'keep', f'{what}: uncommitted changes'
    if not ev.remote_sha:
        return 'keep', f'{what}: branch not pushed'
    if not ev.head_on_remote:
        return 'keep', f'{what}: local commits not pushed'
    if not ev.has_commits:
        return 'keep', f'{what}: no commits yet'
    if not ev.in_trunk:
        return 'keep', f'{what}: not in origin/{main}'
    return 'reapable', what
