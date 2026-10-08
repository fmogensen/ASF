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
    reaped     landed (or empty) and the worktree is gone, or the trunk holds what the tree
               holds — whatever origin still has of the branch, or a landing the ledger
               recorded on another run of it

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
* the pool — :func:`spent_sessions` (an ended run's own process, past its grace, is a leftover
  that names no running job; F-0227);
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
import stat
import subprocess
import time

from asf import budget, config_keys, env, gitops, tokens
from asf.workers import account_auth
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
              'resumed', 'continued', 'publish_refused', 'dead_class', 'publish_refused_heads',
              'dead_why', 'heartbeat_refused', 'stopped_by')

FINISHED = 'finished'
#: A session whose deliverable was never a commit, and which delivered it: not a failure, and not
#: a landing. `finished` means "pushed commits"; this means "there was nothing to push, the session
#: said so, and what it did produce is somewhere the branch cannot show" — an adjudicate ruling on
#: the item's card, or a review finding the lane's own restack already cleared (F-0157).
NOTHING_TO_LAND = 'nothing to land'
#: written by `judge` today (`return None if ev.alive else DEAD_PID`) and the key the scorecard
#: counts (`asf/scorecard/score.py:failure_class`); ledgers on disk also carry it on runs health
#: judged before F-0098, which is why `is_dead_reason` reads for it rather than a newer spelling.
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
#: In-tick backoff (seconds) for :func:`publish`'s own retry of a transient network refusal
#: (B-0097): short enough together never to hold a health tick, long enough for a DNS blip —
#: the signature this card was filed on resolved inside a minute — to clear within it. Exhausted,
#: the refusal falls through to the next tick's health pass, which tries again from the
#: session's worktree (``NETWORK_ERROR`` in :func:`asf.workers.health.push_retry`).
PUBLISH_RETRY_BACKOFF_S = (1.0, 2.0)
#: The end_reason of a run a spent window cut short (asf.workers.headroom).
QUOTA_EXHAUSTED_REASON = f'failed: {headroom.QUOTA_EXHAUSTED}'
#: The end_reason of a run an auth error refused on its account (asf.workers.account_auth).
AUTH_REASON = f'failed: {account_auth.AUTH}'
#: What a refusal BY THE REPO'S HOOK says. A bare ``refused`` is not in it: the factory's own
#: publish refusals ("would lose N commit(s)", "rebase conflicts in", "protected ref") say
#: ``refused`` too and are not the hook's.
HOOK_RE = re.compile(r'\bhook\b|pre-push|declined', re.I)
#: The factory's own pre-push redaction scan (:func:`_redaction_findings`) refusing a publish: the
#: same refusal the hook would make, so its precise correction reaches the session (F-0003).
REDACT_REFUSAL_RE = re.compile(r'(?:^|; )redact: \S+:\d+ ')
#: How many holds in a row on the SAME hook refusal (:func:`hook_refusal_hold`'s own
#: :func:`next_finding`) run before the second is never tried a third time blind (B-0140): lower
#: than :data:`ROUND_CAP` because a hook refusal spends no round in the first place — a session
#: is not at fault for the hook, so there is no reason to let it try the identical push twice.
HOOK_REFUSAL_CAP = 2


def round_cap():
    """Config ``harvest.round_cap``, else :data:`ROUND_CAP`."""
    return config_keys.value('harvest.round_cap', ROUND_CAP)


def hook_refusal_cap():
    """Config ``worker_pool.caps.hook_refusal``, else :data:`HOOK_REFUSAL_CAP`."""
    return config_keys.value('worker_pool.caps.hook_refusal', HOOK_REFUSAL_CAP)


def empty_ends_cap():
    """Config ``worker_pool.caps.empty``, else :data:`EMPTY_CAP`."""
    return config_keys.value('worker_pool.caps.empty', EMPTY_CAP)


def incomplete_cap():
    """Config ``worker_pool.caps.incomplete``, else :data:`INCOMPLETE_CAP`."""
    return config_keys.value('worker_pool.caps.incomplete', INCOMPLETE_CAP)


def loop_cap():
    """Config ``worker_pool.caps.same_head_loop``, else :data:`LOOP_CAP`."""
    return config_keys.value('worker_pool.caps.same_head_loop', LOOP_CAP)
#: A hook's refusal text carries the redaction scanner's own ``redact: <file>:<line>`` line
#: (:mod:`asf.redact`) anywhere in it — the same line a product's own pre-push hook prints when
#: it runs that scan itself.
HOOK_REDACTION_RE = re.compile(r'\bredact:\s*\S+:\d+\b|\bredaction gate\b', re.I)

#: Every class a session's ``end_reason`` falls into. ``finished`` and ``nothing to land`` are the
#: only two that are not a failure; ``other`` is a failure whose signature this module does not
#: name. Composed from the constants that write the strings, so a rename follows.
OUTCOME_CLASSES = (FINISHED, NOT_PUSHED, EMPTY_BRANCH.split(':')[0], DEAD_PID, PUSHED_AFTER_STOP,
                   runtime_mod.report.UNPUSHED, *(name for name, _ in runtime_mod.FAILURE_SIGNATURES),
                   HOOK_REFUSED, NETWORK_ERROR, tokens.TOKEN_CAP, budget.RUN_CAP, NOTHING_TO_LAND,
                   OTHER)
FAILING_CLASSES = tuple(c for c in OUTCOME_CLASSES if c not in (FINISHED, NOTHING_TO_LAND))

# ---- the session states (§2.1) ---------------------------------------------------

WORKING = 'working'
ENDED_AWAITING_TICK = 'ended-awaiting-tick'
FAILED = 'failed'
DEAD = 'dead'
#: FINISHED ('finished') is reused: it is already the end_reason health writes.
SESSION_STATES = (WORKING, ENDED_AWAITING_TICK, FINISHED, NOTHING_TO_LAND, FAILED, DEAD)

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
        if is_reset(rec) or is_unreset(rec):
            clock = max([clock, (rec.get(RESET) or rec.get(UNRESET) or {}).get('at') or ''])
            continue  # a boundary on its item's runs (:func:`resets`), never a run of its own
        if is_park_line(rec):
            continue  # an operator park or its release (:func:`parks`), never a run of its own
        corr = rec.get('correction')
        if isinstance(corr, dict) and corr.get('text') and not corr.get('at') and clock:
            rec = dict(rec, correction=dict(corr, at=clock))
        clock = max([clock] + [t for t in _times(rec) if isinstance(t, str)])
        if is_launch(rec) or job not in runs:
            runs.setdefault(job, []).append(dict(rec))
        else:
            _run_of(runs[job], rec.get('branch')).update(rec)
    return {job: [_clean(r) for r in rs] for job, rs in runs.items()}


def _run_of(rs, branch):
    """The run of one job an update line lands on: the latest one, unless the line names a
    branch the latest run is not on and an earlier run of the job is — then that run. One job
    name launched on two branches (a Feature's spec and plan corrected as ``correct-f-…``) left
    the earlier branch's lane record on the later branch's run, and the two swapped every tick
    (a product's F-0011)."""
    latest = rs[-1]
    if not branch or not isinstance(branch, str) or latest.get('branch') == branch:
        return latest
    for r in reversed(rs):
        if r.get('branch') == branch:
            return r
    return latest


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

    def __init__(self, data, view, resets=None, parks=None, voids=None, standing=None):
        self.data = data
        self.view = view
        self.blob = marshal.dumps(view)
        self.resets = resets or {}
        self.parks = parks or {}
        self.voids = voids or {}
        self.standing = standing or {}
        self._by_item = None
        self.sig = None         # the stat signature this content was read under (_folded)
        self.read_ns = 0        # when it was read, wall clock

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


#: A stat signature recorded less than this long after the file's own mtime is not trusted
#: (git's "racy" rule): a rewrite within the same filesystem clock tick can leave every stat
#: field unchanged, so such a hit is checked against the bytes until a later read settles it.
_RACY_NS = 2 * 1_000_000_000


def _signature(st):
    """What identifies one content of the registry without reading it: inode, size, mtime and
    ctime (the last is the kernel's own, never set by ``utime``)."""
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _settled(hit, sig):
    """``hit`` still holds the file whose stat is ``sig``, by the stat alone: the signature it
    was read under is unchanged and that read came well after the file's last change."""
    return (hit.sig is not None and hit.sig == sig
            and hit.read_ns - max(sig[2], sig[3]) >= _RACY_NS)


def _folded(path):
    """The registry at ``path`` as a :class:`_Folded` (read-only view; :data:`_EMPTY` when there
    is no file).

    A hit is O(1): one ``stat``. Every question the tick asks (``rounds_of``, ``item_runs``,
    ``voided_sha``… per row, per item, per run) used to read the whole file and compare its
    bytes with the cached copy — with an 8.9 MB, 21,827-line registry, a wave step spent 23+
    minutes at 100% CPU in that ``read`` and ``memcmp`` and held the tick lock throughout
    (2026-10-04). The bytes are read and compared only when the stat moved or is too fresh to
    trust (:func:`_settled`)."""
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        return _EMPTY
    key = (os.path.abspath(path), env.ASF_HOME)
    hit = _REGISTRY_CACHE.get(key)
    sig = _signature(st)
    if hit is not None and _settled(hit, sig):
        return hit
    read_ns = time.time_ns()
    with open(path, 'rb') as f:
        data = f.read()
    if hit is not None and hit.data == data:
        hit.sig, hit.read_ns = sig, read_ns
        return hit
    lines = _parse_registry(data, _product_from_registry_dir(path))
    hit = _Folded(data, fold(lines), _resets(lines), _parks(lines), _voids(lines),
                  _standing(lines))
    hit.sig, hit.read_ns = sig, read_ns
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


#: The key of a registry line that undoes a reset (``asf reset --undo``): ``{at, pr, head, by,
#: why}`` — the reset naming that ``(pr, head)`` is dropped (:func:`resets`).
UNRESET = 'unreset'


def is_reset(rec):
    return isinstance((rec or {}).get(RESET), dict) and bool((rec or {}).get('item'))


def is_unreset(rec):
    return isinstance((rec or {}).get(UNRESET), dict) and bool((rec or {}).get('item'))


def reset_job(item):
    return f'reset-{str(item).lower()}'


def _claim(r):
    return (r.get('pr'), r.get('head') or '')


def _standing(lines):
    """``{item: [reset, …]}`` — every reset no newer unreset line names (by its ``(pr, head)``),
    in registry order."""
    out = {}
    for rec in lines:
        if is_reset(rec):
            out.setdefault(rec['item'], []).append(dict(rec[RESET], item=rec['item']))
        elif is_unreset(rec):
            u = rec[UNRESET]
            out[rec['item']] = [r for r in out.get(rec['item'], ())
                                if not (_claim(r) == _claim(u)
                                        and (r.get('at') or '') <= (u.get('at') or ''))]
    return out


def _resets(lines):
    """``{item: its newest standing reset}`` from the registry's lines (:func:`_standing`)."""
    out = {}
    for item, rs in _standing(lines).items():
        for r in rs:
            if (r.get('at') or '') >= ((out.get(item) or {}).get('at') or ''):
                out[item] = r
    return out


def _voids(lines):
    """``{item: [void, …]}`` — the standing resets an operator wrote to void a landing
    (:func:`note_reset` with ``why``)."""
    out = {}
    for item, rs in _standing(lines).items():
        vs = [r for r in rs if r.get('void')]
        if vs:
            out[item] = vs
    return out


def voids(path, item):
    """The standing voided landings of ``item``: ``[{at, pr, branch, head, why, by}]``."""
    return [dict(v) for v in _folded(path).voids.get(item, ())]


def _sha_match(a, b):
    a, b = str(a or ''), str(b or '')
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def voided_sha(path, item, sha):
    """The void of ``item`` that names ``sha`` as its landing, or None — a claim the operator
    voided proves nothing, whoever reports it again."""
    return next((v for v in voids(path, item) if _sha_match(v.get('head'), sha)), None)


def voided_run(path, run, sha=None, folded=None):
    """The standing reset that names ``run``'s landing claim, or None: a void of its item whose
    head is ``sha`` (the run's merge sha, else its ``harvested``), or any standing reset of its
    item naming the run's lane PR — with the lane's head when both carry one — on a run that
    started before it. A reset ``(pr, head)`` is never a merge fact, whatever the host says.
    ``folded``: the ledger already folded (:func:`_folded` reads the file each call)."""
    run = run or {}
    item = run.get('item')
    if not item or not isinstance(item, (str, int, float)):
        return None
    folded = folded or _folded(path)
    lane = run.get('lane') if isinstance(run.get('lane'), dict) else {}
    sha = sha or run.get('harvested')
    for v in folded.voids.get(item, ()):
        if sha and _sha_match(v.get('head'), sha):
            return dict(v)
    for v in folded.standing.get(item, ()):
        if not v.get('pr') or lane.get('pr') != v.get('pr'):
            continue
        if (run.get('started') or '') >= (v.get('at') or ''):
            continue
        heads = [h for h in (lane.get('head'), sha, lane.get('sha')) if h]
        if v.get('void') or not v.get('head') or not heads \
                or any(_sha_match(v.get('head'), h) for h in heads):
            return dict(v)
    return None


def resets(path):
    """``{item: {at, pr, branch, head, archive}}`` — each item's newest reset. Every run of the
    item started before ``at`` is history: no round, correction, ruling or attempt of it counts
    (:func:`rounds_of`, :func:`corrections`, :func:`attempts`), and no landing waits on it."""
    return {k: dict(v) for k, v in _folded(path).resets.items()}


# ---- operator parks: a hold at item, branch or job scope until ``asf unpark`` -----------------
#: The key of a registry line that parks by hand (:func:`note_park`): ``{at, scope, branch,
#: on_job, reason, why}``. Its job is :func:`park_job` — never a launch, never a run, so no later
#: run "answers" it, no closed-PR reset (:func:`note_reset`) retires it and no correction written
#: on a run overwrites it: only its release line (:data:`UNPARK`) lifts it.
PARK = 'park'
#: The key of the line that releases a park (``{at, why}``) — the same job as the park.
UNPARK = 'unpark'
#: A park's scopes: the whole item, one branch's rows, or one job's rows.
SCOPE_ITEM, SCOPE_BRANCH, SCOPE_JOB = 'item', 'branch', 'job'
#: The correction kind of a park written by hand (``asf park``).
OPERATOR_PARK = 'operator park'


def is_park_line(rec):
    rec = rec or {}
    return bool(rec.get('item')) and (isinstance(rec.get(PARK), dict)
                                      or isinstance(rec.get(UNPARK), dict))


def park_job(scope, target):
    """``park-<target>``: the one registry job a park at ``scope`` on ``target`` is kept under."""
    slug = re.sub(r'[^a-z0-9]+', '-', str(target).lower()).strip('-')
    return f'park-{slug}' if scope == SCOPE_ITEM else f'park-{scope}-{slug}'


def _parks(lines):
    """``{park job: park}`` of every park no release line has lifted since, each carrying its
    ``job`` and ``item``."""
    out = {}
    for rec in lines:
        if not is_park_line(rec):
            continue
        if isinstance(rec.get(PARK), dict):
            out[rec['job']] = dict(rec[PARK], job=rec['job'], item=rec['item'])
        else:
            out.pop(rec['job'], None)
    return out


def parks(path):
    """Every standing operator park, oldest first: ``[{job, item, scope, branch, on_job,
    reason, why, at}]``."""
    return sorted((dict(p) for p in _folded(path).parks.values()),
                  key=lambda p: (p.get('at') or '', p['job']))


def park_holds(park, item, branch=None, job=None):
    """True when ``park`` holds a row or run of ``item`` on ``branch`` for ``job``: an item park
    holds the item, a branch park that branch alone, a job park that job alone."""
    if (park or {}).get('item') != item:
        return False
    scope = park.get('scope') or SCOPE_ITEM
    if scope == SCOPE_BRANCH:
        return bool(branch) and branch == park.get('branch')
    if scope == SCOPE_JOB:
        return bool(job) and job == park.get('on_job')
    return True


def scoped_park_of(path, run):
    """The standing branch or job park holding ``run`` (its item, branch and job), or None."""
    for p in _folded(path).parks.values():
        if (p.get('scope') or SCOPE_ITEM) != SCOPE_ITEM and \
                park_holds(p, (run or {}).get('item'), run.get('branch'), run.get('job')):
            return p
    return None


def item_park(path, item):
    """The standing item-scope park on ``item``, or None."""
    held = [p for p in _folded(path).parks.values()
            if p.get('item') == item and (p.get('scope') or SCOPE_ITEM) == SCOPE_ITEM]
    return dict(max(held, key=lambda p: p.get('at') or '')) if held else None


def note_park(path, item, scope, reason, why, branch='', on_job='', now=None):
    """Append one park line; returns its job."""
    target = {SCOPE_BRANCH: branch, SCOPE_JOB: on_job}.get(scope) or item
    job = park_job(scope, target)
    rec = {'job': job, 'item': item,
           PARK: {'at': now or now_iso_utc(), 'scope': scope, 'branch': branch or '',
                  'on_job': on_job or '', 'reason': reason, 'why': why}}
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')
    return job


def note_unpark(path, park, why='', now=None):
    """Append the line that releases ``park`` (as :func:`parks` returns it)."""
    rec = {'job': park['job'], 'item': park['item'], UNPARK: {'at': now or now_iso_utc(),
                                                              'why': why or ''}}
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')


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
    pass resets it). True when a line was written.

    ``reset`` may also carry ``why`` and ``by`` (``asf reset``): the line is then a *void* —
    the claim ``(pr, head)`` it names is a landing the operator says did not land the item
    (:func:`voids`)."""
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
    if reset.get('why'):
        rec[RESET].update(void=True, why=reset['why'], by=reset.get('by') or 'operator')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')
    return True


def note_unreset(path, item, reset, why='', by='operator', now=None):
    """Append the line that undoes ``reset`` (one of :func:`voids` or :func:`resets`) of
    ``item``: from now on it is not the item's reset, and its runs are the item's again."""
    rec = {'job': reset_job(item), 'item': item,
           UNRESET: {'at': now or now_iso_utc(), 'pr': reset.get('pr'),
                     'head': reset.get('head') or '', 'why': why, 'by': by}}
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


#: Seconds an ended run's own process gets to exit by itself. Until it is up, the process is
#: still the run's — it may be flushing, committing or pushing, and it still holds the run's
#: worktree open. After it, the run is spent: the factory stops believing the process
#: (:func:`spent_sessions`, read by the pool) and stops the process itself
#: (:func:`asf.workers.health.settle_ended`). One number, so the two can never disagree about
#: when (T-0196, F-0227).
LINGER_GRACE_S = 300


def linger_spent(run, now=None, grace_s=None):
    """True when ``run`` has an ``ended`` line and its grace has run out: the run is finished on
    the ledger (:func:`is_live` is already false) *and* enough time has passed that a process
    still answering for it is a leftover, not the run.

    False for a live run, for one whose ``ended`` stamp cannot be read, and for one still inside
    the grace — all three are "not yet", never "no"."""
    ended = headroom.parse_ts((run or {}).get('ended'))
    if ended is None:
        return False
    grace = LINGER_GRACE_S if grace_s is None else grace_s
    return (time.time() if now is None else now) - ended.timestamp() >= grace


def spent_sessions(state_root, now=None, grace_s=None):
    """``{ASF_SESSION id}`` for every spent run (:func:`linger_spent`) under every product's
    registry — the ids a process may still carry that name no running job.

    The mirror of :func:`live_all`, over the same files and the same fold cache, and matched on
    the session id alone: the id is unique per launch, where a pid is a number the kernel hands
    out again (F-0227 C2). A run that recorded no ``session`` contributes nothing."""
    out = set()
    if not state_root or not os.path.isdir(state_root):
        return out
    for name in sorted(os.listdir(state_root)):
        path = os.path.join(state_root, name, 'sessions.jsonl')
        if not os.path.isfile(path):
            continue
        for r in latest(path).values():
            if r.get('session') and linger_spent(r, now, grace_s):
                out.add(r.get('session'))
    return out


def note_spent_windows(state_root=None, alive=None, now=None):
    """Stop, at once, the account of every run under every product's registry that is still
    live on the ledger, whose pid is gone, and whose log's last run closed on a usage-limit
    result (:func:`asf.workers.runtime.failure_reason` → ``quota-exhausted``), or blocks it on an
    auth error (``auth``, :func:`asf.workers.account_auth.block`). Health records the
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
            sig = runtime_mod.failure_reason(rec) if rec is not None else None
            if sig == account_auth.AUTH:
                # an auth refusal: the account is out from this failure; health's own pass on
                # the run prints the one ALARM (account_auth.note)
                account_auth.block(run['account'], job=run.get('job', ''),
                                   product=run.get('product') or name)
                out.append(f"{run['account']} unusable: auth error")
                continue
            if sig != headroom.QUOTA_EXHAUSTED:
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


def review_attempts(path, lanes):
    """``{branch: n}`` — ended review runs on that branch started at or after its lane record's
    ``head_at``: how many reviewers this head has already had."""
    out = {}
    for rs in runs(path).values():
        for r in rs:
            b = r.get('branch')
            if not b or r.get('kind') != 'review' or not r.get('ended'):
                continue
            head_at = (lanes.get(b) or {}).get('head_at')
            if not head_at or (r.get('started') or '') < head_at:
                continue
            out[b] = out.get(b, 0) + 1
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


#: What a liveness check can conclude about a run's pid. Only :data:`GONE` and :data:`REUSED`
#: end a run; :data:`UNKNOWN` is the honest answer when a process answers at the pid, looks like
#: one of ours, and the observation could not name its session — a worker, not a corpse (F-0234).
ALIVE = 'alive'
GONE = 'gone'
REUSED = 'reused'
UNKNOWN = 'unknown'
LIVENESS = (ALIVE, GONE, REUSED, UNKNOWN)
#: The verdicts under which a run keeps running.
LIVE_VERDICTS = (ALIVE, UNKNOWN)


def liveness(run, observed, exists, is_ours=None, silent_min=None, silent_for=None):
    """Which of :data:`LIVENESS` describes ``run``'s pid. Pure: every fact arrives as an argument.

    ``observed`` is the :class:`asf.workers.observe.Observed` sitting at the pid, or None;
    ``exists(pid)`` is the OS answer (:func:`pid_alive`); ``is_ours`` is True/False/None for
    "the process there is one of the factory's headless workers" (:func:`asf.workers.health.
    is_print_worker` over its command line), None when it was not asked; ``silent_for`` and
    ``silent_min`` are minutes — how long the run's log has been still, and how long it may be.
    """
    pid = run.get('pid')
    if cloudpid.is_token(pid):          # a cloud run is no process here: its status file answers
        return ALIVE if cloudpid.alive(pid) else GONE
    if not exists(pid):
        return GONE                     # the OS has the last word on a pid that answers nothing
    if observed is not None:
        if not run.get('session') or run.get('session') == observed.session:
            return ALIVE
        if observed.session:
            return REUSED               # it names a session, and not ours: the pid was recycled
    if is_ours is False:
        return REUSED                   # something answers there, and it is not a worker of ours
    if silent_min is not None and silent_for is not None and silent_for > silent_min:
        return GONE                     # ours, answering, and it has written nothing since
    return UNKNOWN


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


def delivered(run):
    """Ended with its work delivered: :func:`finished` — pushed commits — or
    :data:`NOTHING_TO_LAND`, a deliverable the branch cannot show (F-0157). What a reader asking
    "did this session do its job" wants; :func:`finished` alone means "and it is on the branch"."""
    return finished(run) or (bool(run) and bool(run.get('ended'))
                             and run.get('end_reason') == NOTHING_TO_LAND)


def landed(run):
    """The run carries a landing mark (``harvested:``). Under ``flags.facts: shadow`` the landing
    fact (:func:`asf.facts.landing.shadow_run`) is compared beside it; the answer is this one."""
    old = bool(run) and bool(run.get('harvested'))
    if not old:
        return old
    from asf.facts import landing as facts_landing
    return facts_landing.shadow_run(run, old)


def lands_nothing(run):
    """True for a run whose kind never lands through the lane (:data:`asf.workers.cloud.
    LOCAL_KINDS`: groom, groom-clerk, close — they write the record, not a branch to merge). Such
    a run may carry an ``item`` (a groom row names the first card it files), and it never puts
    that item in a wait to land: a product's T-0654 read "pushed, waiting to land" off a finished
    groom run, with no code branch, PR or session ever made for it."""
    from asf.workers.cloud import LOCAL_KINDS
    return (run or {}).get('kind') in LOCAL_KINDS


def eligible(run):
    """What harvest may gate: finished (hence pushed), not landed, not handed to the PR lane."""
    return finished(run) and not landed(run) and run.get('harvest') != 'pr'


#: ``harvested:`` values that are not a landing: harvest archived the run, nothing reached the trunk
NOT_A_LANDING = ('superseded',)


def branch_landings(path):
    """``{branch: the sha its latest run landed at}`` — the landings the ledger records, by
    branch. The *latest* run on each branch (:func:`by_branch`) and no earlier one: a branch
    whose last run carries no landing has work on it that has not landed, whoever landed it a
    round ago. An archival (:data:`NOT_A_LANDING`) is no landing.

    A lane branch is landed on the run the lane held for it, which is not always the run that
    owns the worktree sitting on that branch: an approved spec or plan the lane adopted lands
    on a synthetic run with no session and no worktree (:mod:`asf.tick.land_spec`), and the
    session that wrote the document keeps a row with no ``harvested``. This is how that tree's
    reap finds the landing (F-0201)."""
    return {b: r['harvested'] for b, r in by_branch(path).items()
            if r.get('harvested') and r['harvested'] not in NOT_A_LANDING}


def landed_earlier(path, run):
    """The sha an earlier run on ``run``'s branch was harvested at — its lane's work is already on
    the trunk (a squash-merged lane PR included: the native landing marks the run at merge) — or
    None. A later session on that branch that writes nothing has nothing to land, and is not sent
    back to push work the trunk already holds."""
    from asf.facts import landing as facts_landing
    return facts_landing.shadow_earlier(path, run, _landed_earlier(path, run))


def _landed_earlier(path, run):
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


def correction_superseded_by_landing(path, item, branch):
    """The branch that landed in ``branch``'s place, or None: an ``asf correct`` correction or
    ruling written against ``branch`` of ``item``, and a *different* branch of the same item has
    since landed (:func:`landed`) — the work it asked for got done another way (a reshape onto a
    fresh Task, a hand-pushed fix, a second branch the operator landed directly), so carrying it
    out on its own branch now would just redo it (#46). Never called for a park
    (:func:`item_park` is its own correction, with no branch to supersede); when ``branch``
    itself is the one that landed, that is simply done, read as such elsewhere
    (:func:`closed_state`)."""
    for r in item_runs(path, item):
        if r.get('branch') and r.get('branch') != branch and landed(r):
            return r['branch']
    return None


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
                          if r.get('kind') == 'adjudicate' and delivered(r)
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
    if corr.get('operator_ruling'):
        return True  # ``asf correct`` at the cap: the operator's ruling is the instruction itself
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


def overruling(path, item, head, same_code=None):
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
    sessions. So a ruling offers two shas: the one it claims, and — for a run that committed
    nothing — spawn's recorded ``launch_head``, the fact behind the claim. Either stands on
    ``head`` when it *is* ``head``, or when ``same_code(sha)`` (the lane's
    :func:`asf.evidence.review.same_code`) says ``head`` carries the code that sha carried: the
    same tree outside the reviews directory, or the same own patch over the trunk. The lane
    rewrites a branch without changing what was ruled on — a review commit on top, a reword, a
    sign-off, a restack onto a moved trunk — and the ruling is measured with the same ruler
    B-0147 measures its review with, so the two stand or fall together."""
    if not head or not path or not item:
        return None
    from asf.workers import report as report_mod
    ruled = sorted((r for r in item_runs(path, item)
                    if r.get('kind') == 'adjudicate' and delivered(r)),
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
    ruled = [m.group(0).lower()] if m else []
    if not report_mod._claim(rep.get('commits')) and run.get('launch_head'):
        ruled.append(run['launch_head'].lower())  # the fact behind the claim, for a run that
        # committed nothing: one that committed has moved the head past what it ruled on
    for sha in ruled:
        if head.lower().startswith(sha) or (same_code and same_code(sha)):
            return run.get('job')
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
        if not run.get('item') or before_reset(path, run) or pending_correction(run, path) \
                or lands_nothing(run):
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
                or pending_correction(run, path) or lands_nothing(run):
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
    """The run ended on a spent window (:data:`asf.workers.headroom.QUOTA_EXHAUSTED`) or on an
    auth error that took its account out (:data:`AUTH_REASON`, :mod:`asf.workers.account_auth`):
    the account's fault, not the work's — no attempt, no round, no answer to a correction."""
    return (run or {}).get('end_reason') in (QUOTA_EXHAUSTED_REASON, AUTH_REASON)


def auth_failed(run):
    """The run ended on an auth error (:data:`AUTH_REASON`): its account is blocked."""
    return (run or {}).get('end_reason') == AUTH_REASON


def attempts(path):
    """``{item: runs the registry holds for it}`` — every launch, ended or not, except a run
    a spent window cut short (:func:`quota_exhausted`) and the run a hand ``asf park`` writes
    to hold an item no session touched (kind ``park``: nothing was launched)."""
    out = {}
    folded = _folded(path)
    for rs in folded.view.values():  # read only: counts leave, never runs
        for r in rs:
            if r.get('item') and not quota_exhausted(r) and not folded.before_reset(r) \
                    and r.get('kind') != 'park':
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
    for item in {r.get('item') for rs in _folded(path).view.values() for r in rs if r.get('item')} \
            | {p['item'] for p in _folded(path).parks.values()}:
        c = correction_of(path, item)
        if c:
            out[item] = c
    return out


def correction_of(path, item):
    """:func:`corrections`' entry for one ``item``, or None when none is pending. An operator
    park on the item (:func:`item_park`) is its correction whatever else is pending; a correction
    on a run a branch or job park holds (:func:`scoped_park_of`) is not the item's — the park
    speaks for that branch or job alone, and the item's other branches go on."""
    park = item_park(path, item)
    if park:
        return {'kind': OPERATOR_PARK, 'text': park.get('reason') or '', 'at': park.get('at'),
                'parked': True, 'reason': park.get('reason') or '', 'scope': SCOPE_ITEM,
                'park_job': park['job'], 'rounds': rounds_of(path, item), 'same': 0,
                'branch': None, 'ruled': False, 'settled': False, 'prs': ()}
    held = [(r, pending_correction(r, path)) for r in item_runs(path, item)
            if not scoped_park_of(path, r)]
    held = [(r, c) for r, c in held if c]
    if not held:
        return None
    run, corr = max(held, key=lambda rc: rc[1].get('at') or '')
    return dict(corr, rounds=rounds_of(path, item), same=repeats(corr),
                branch=corr.get('branch') or run.get('branch'),
                job=run.get('job'), ruled=ruled(path, item, corr),
                settled=settled(path, item, corr.get('at')),
                prs=settled_prs(path, item, corr.get('at')))


#: the evidence pass's PR list (:class:`asf.evidence.sources.GitHubHost`), under the state dir
PR_CACHE = 'cache-prs.json'


def ended_prs(state_dir, landed=()):
    """``{number: {'state', 'head'}}`` of every PR that is over: the rows the evidence pass last
    saw MERGED or CLOSED on its cache, plus every number in ``landed``
    (:func:`asf.trunk_watch.landed_prs`) as MERGED, whatever its cached row says — the trunk
    carries the merge, and the cache is a copy of an older answer (F-0277). ``{}`` when there is
    no cache and ``landed`` is empty."""
    try:
        with open(os.path.join(state_dir, PR_CACHE)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = []
    out, heads = {}, {}
    for p in data if isinstance(data, list) else ():
        if not isinstance(p, dict):
            continue
        state, number = str(p.get('state') or '').upper(), p.get('number')
        if not isinstance(number, int):
            continue
        heads[number] = p.get('headRefOid') or None
        if state in ('MERGED', 'CLOSED'):
            out[number] = {'state': state, 'head': heads[number]}
    for number in landed or ():
        out[number] = {'state': 'MERGED', 'head': heads.get(number)}
    return out


def pr_ended(rec, ended):
    """The state (``MERGED``/``CLOSED``) of the PR lane record ``rec`` names when ``ended``
    (:func:`ended_prs`) holds it over, else None. A merge is final whatever the branch head did
    after it (a file pushed to the dead branch); a close counts only for the head it closed,
    or when either side names none."""
    try:
        number = int((rec or {}).get('pr') or 0)
    except (TypeError, ValueError):
        return None
    hit = (ended or {}).get(number)
    if not number or not hit:
        return None
    if hit['state'] == 'MERGED':
        return 'MERGED'
    heads = (hit.get('head'), rec.get('head'))
    return 'CLOSED' if None in heads or heads[0] == heads[1] else None


def occupancy(path, lanes=None, alive=None, result=None, ended=None, on_origin=None):
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
    every branch whose lane state holds it), ``review`` (``{item: {branch, round, pr, why,
    attempts}}`` — lane REVIEW: a PUSHED → REVIEW row), ``landing`` (``{item: {branch, state,
    pr, why}}`` — the other lane states: a PUSHED → LAND row), ``branches`` (``{branch: why}``
    of every waiting branch) and ``docs`` (``{item: {kind: why}}``: a spec or plan pushed and
    waiting is not starved);
    ``parks`` (:func:`parks`): the standing operator parks — a branch or job one holds only its
    own rows (its branch is skipped here; the feeder turns its rows into one PARKED row).

    ``lanes`` (the argument): ``{branch: record}`` to use instead of the run lines' own.
    ``ended`` (:func:`ended_prs`): PRs the host says are merged or closed — a lane record still
    naming one holds nothing and asks for nothing, and a correction on its branch is dropped.
    A merged PR's item counts as ``landed`` (``{item: sha}``), and ``landed_on`` names the
    branch that landing was recorded on (:func:`asf.workers.landing.verify_landings`).
    A correction on a dead branch moves to the item's open code branch (:func:`open_code_branch`)
    and carries ``moved_from``. ``back`` (``{item: {branch, pr, reason}}``): a branch the lane
    sent BACK with no correction pending on it — a label, never a launch.

    ``on_origin``: ``callable(branches) -> set`` of those branches origin has a head for, or
    None when it cannot say. A branch that is only "finished, pushed" by its run line (no lane
    record, no PR) waits to land only when origin has it: a run that names an item but never
    pushed its branch (a product's T-0654: a groom run naming the Task it filed) leaves the item
    free for its coder, never a PUSHED → LAND row that launches nothing. None: not checked."""
    from asf.harvest import lane as lane_mod  # the lane's states, no cycle at import
    by = by_branch(path)
    live_items = {s['item']: f"session {s['job']} running" for s in inflight(path, alive)
                  if s.get('item')}
    out = {'busy': dict(live_items), 'waiting_landing': {}, 'corrections': corrections(path),
           'lanes': {}, 'review': {}, 'landing': {}, 'branches': {}, 'docs': {}, 'landed': {},
           'landed_on': {}, 'parks': parks(path), 'back': {}}
    dead_branches, waits = set(), []
    for branch, run in by.items():
        item, kind = run.get('item'), run.get('kind')
        rec = (lanes or {}).get(branch) if lanes is not None else lane_of(run)
        if not item or item in live_items or before_reset(path, run):
            continue  # a reset item's old branch holds nothing: its PR was closed unmerged
        if scoped_park_of(path, run):
            continue  # a branch or job park speaks for it: the feeder's PARKED row (``parks``)
        over = pr_ended(rec, ended) if rec and rec.get('state') != lane_mod.MERGED else None
        if over:  # the PR is over: no review, landing or correction for it, whatever the record
            dead_branches.add(branch)
            if over == 'MERGED':
                out['landed'][item] = (rec or {}).get('sha') or ''
                out['landed_on'][item] = branch
            continue
        if landed(run) or (rec or {}).get('state') == lane_mod.MERGED:
            # its branch landed and the record may not have caught up yet: not busy, not
            # waiting — and not an idle branch either (the feeder's idle-branch rule reads this)
            out['landed'][item] = run.get('harvested') or (rec or {}).get('sha') or ''
            out['landed_on'][item] = branch
        why = None
        if rec and rec.get('state'):
            state = rec['state']
            if state == lane_mod.BACK and not landed(run) and not pending_correction(run, path):
                # sent back, and no correction on it waits for a session: the feeder says so,
                # never "no run holds it" (:func:`asf.feeder.rows.pushed_rows`)
                out['back'][item] = {'branch': branch, 'pr': rec.get('pr'),
                                     'reason': rec.get('reason') or ''}
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
                # B-0043: `heavy`/`head` (the lane's own ci.heavy_after_review bookkeeping,
                # :meth:`asf.harvest.lane.GitHubHost.heavy_gate`) let the feeder's PUSHED → LAND
                # row (:func:`asf.feeder.rows.lane_rows`) tell a MERGING head whose heavy CI was
                # never requested apart from one landing normally — never a silent gap.
                out['landing'][item] = {'branch': branch, 'state': state, 'pr': rec.get('pr'),
                                        'why': why, 'heavy': rec.get('heavy'),
                                        'head': rec.get('head')}
        elif landed(run) or pending_correction(run, path) or lands_nothing(run):
            continue
        elif run.get('harvest') == 'pr':
            why = PR_WAIT
        elif eligible(run):
            why = PUSHED_WAIT
        elif is_live(run) and finished_unrecorded(run, alive, result):
            why = FINISHED_WAIT
        if not why:
            continue
        waits.append((item, branch, kind, why))
    unpushed = set()
    if on_origin is not None:
        claimed = sorted({b for _i, b, _k, why in waits if why == PUSHED_WAIT})
        present = on_origin(claimed) if claimed else set()
        if present is not None:
            unpushed = set(claimed) - set(present)
    for item, branch, kind, why in waits:
        if branch in unpushed:
            continue
        out['waiting_landing'].setdefault(item, why)
        out['branches'][branch] = why
        if kind:
            out['docs'].setdefault(item, {})[kind] = why
    if out['review']:
        tries = review_attempts(path, out['lanes'])
        for entry in out['review'].values():
            entry['attempts'] = tries.get(entry['branch'], 0)
    # a park outlives its PR: a closed PR left the row that looped on it (a product's
    # delivery-code-t-0042, its PR closed, launched 69 times) — only an unpark or a release lifts it.
    # Any other correction on a dead branch moves to the item's open code branch when it has one
    # (an operator ruling written on a plan branch whose PR then merged was dropped, and the
    # Task's own branch, sent back, waited with no row: a product's T-0488, T-0353); with none
    # open, it is dropped — no session is relaunched on a PR that is over
    kept = {}
    for i, c in out['corrections'].items():
        if c.get('branch') not in dead_branches or c.get('parked'):
            kept[i] = c
            continue
        moved = open_code_branch(path, i, by, dead_branches, lanes, lane_mod)
        if moved:
            kept[i] = dict(c, branch=moved, moved_from=c.get('branch'))
    out['corrections'] = kept
    return out


#: run kinds that write a document or the record, never the item's code: a branch any of them
#: ran on is not the item's code branch (:func:`open_code_branch`)
NON_CODE_KINDS = ('spec', 'spec-amend', 'plan', 'replan', 'reshape', 'groom', 'groom-clerk')


def open_code_branch(path, item, by=None, dead=(), lanes=None, lane_mod=None):
    """The branch of ``item``'s code still open — the latest run on it is the item's, it has not
    landed, its lane record is not over (MERGED, STALE, REAPED) and its PR is not one of ``dead``'s
    branches — and no run on it wrote a document or the record (:data:`NON_CODE_KINDS`). The
    newest such branch by its run's start, or None."""
    if lane_mod is None:
        from asf.harvest import lane as lane_mod
    by = by_branch(path) if by is None else by
    kinds = {}
    for rs in runs(path).values():
        for r in rs:
            if r.get('branch'):
                kinds.setdefault(r['branch'], set()).add(r.get('kind'))
    best = None
    for branch, run in by.items():
        if run.get('item') != item or branch in dead or landed(run) or before_reset(path, run):
            continue
        if kinds.get(branch, set()) & set(NON_CODE_KINDS):
            continue
        rec = (lanes or {}).get(branch) if lanes is not None else lane_of(run)
        if (rec or {}).get('state') in lane_mod.TERMINAL_STATES:
            continue
        if best is None or (run.get('started') or '') > (best[1].get('started') or ''):
            best = (branch, run)
    return best[0] if best else None


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
    head: str = ''               #: the worktree's HEAD sha; '' when it could not be read
    remote_sha: str = ''         #: ``origin/<branch>``'s tip; '' when the branch is not on origin
    head_on_remote: bool = False  #: the worktree's HEAD is contained in ``origin/<branch>``
    unpushed: int = 0            #: commits on HEAD not on ``origin/<branch>`` (``origin/<main>``
    #: when the branch was never pushed)
    has_commits: bool = False    #: the branch was ever committed to
    in_trunk: bool = False       #: HEAD is an ancestor of ``origin/<main>``
    liveness: str = ''           #: one of :data:`LIVENESS`, when a caller asked; '' otherwise (F-0234)

    @property
    def pushed(self):
        return bool(self.remote_sha) and self.uncommitted == 0 and self.unpushed == 0


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


def _count(p):
    return int(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip().isdigit() else 0


def remote_head(wt, branch):
    """``origin``'s head of exactly ``branch``, asked of origin now (``ls-remote`` of
    ``refs/heads/<branch>``, :func:`asf.gitops.head_sha`) — ``''`` when origin has no such head
    or could not be asked. Never the first line of a bare ``ls-remote <branch>``: git matches that
    against every ref's tail, and ``archive/<branch>`` sorts before ``<branch>``."""
    if not branch:
        return ''
    ls = _git(['ls-remote', '--heads', 'origin', gitops.head_ref(branch)], wt)
    return gitops.head_sha(ls.stdout, branch) if ls.returncode == 0 else ''


def _with_changes(wt, shas):
    """``shas`` less every commit that changes no file — its tree is its first parent's (an
    ``archive(…): … [skip ci]`` marker, a ``--allow-empty`` note). Such a commit has no patch,
    so ``git cherry`` never finds it on origin and would count it unpushed for good, with no work
    in it to lose. A commit git cannot read is kept (counted), never dropped."""
    shas = [s for s in shas if s]
    if not shas:
        return []
    p = _git(['log', '--no-walk=unsorted', '--format=%H %T %P', *shas], wt)
    if p.returncode != 0:
        return shas
    rows = [ln.split() for ln in p.stdout.splitlines() if ln.strip()]
    parents = [r[2] for r in rows if len(r) > 2]
    trees = {}
    if parents:
        t = _git(['log', '--no-walk=unsorted', '--format=%H %T', *parents], wt)
        if t.returncode == 0:
            trees = dict(ln.split()[:2] for ln in t.stdout.splitlines() if len(ln.split()) >= 2)
    empty = {r[0] for r in rows if len(r) > 2 and trees.get(r[2]) == r[1]}
    return [s for s in shas if s not in empty]


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

    A commit that changes no file (:func:`_with_changes`) is never counted: it has no patch for
    ``git cherry`` to find on origin, so it would read unpushed forever with no work in it.
    """
    if not remote_sha:
        return _count(_git(['rev-list', '--count', f'origin/{main}..HEAD'], wt))
    p = _git(['cherry', remote_sha, 'HEAD', f'origin/{main}'], wt)
    if p.returncode != 0:  # no origin/<main> here, or a remote sha this repo has not fetched
        r = _git(['rev-list', f'{remote_sha}..HEAD'], wt)
        return len(_with_changes(wt, r.stdout.split())) if r.returncode == 0 else 0
    plus = [ln.split()[1] for ln in p.stdout.splitlines()
            if ln.startswith('+') and len(ln.split()) > 1]
    return len(_with_changes(wt, plus))


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


#: git's own error lines in a push's or commit's output. ``hint:`` is not one of them — git prints
#: its fast-forward hint last, so "the last line" is a hint more often than an error.
GIT_ERROR_RE = re.compile(r'^(?:fatal|error|remote)\s*:', re.I)
#: git's summary of a failed push: that there was an error, never which one.
GIT_PUSH_SUMMARY_RE = re.compile(r'^error:\s*failed to push some refs\b', re.I)
#: the most error lines one refusal carries — a refusal is one line on a tick's output
GIT_ERROR_LINES = 3


def git_error(output, limit=GIT_ERROR_LINES):
    """The exact git error in a push's or commit's output (F-0176): its ``fatal:`` / ``error:`` /
    ``remote:`` lines in git's own order, joined with ``; ``. A refusal that named none falls back
    to the last line that is neither a hint nor the ``failed to push some refs`` summary — a local
    pre-push hook's own message has no prefix at all, and it is the one thing worth reading.
    ``'push failed'`` when git printed nothing."""
    lines = [ln.strip() for ln in (output or '').splitlines() if ln.strip()]
    named = [ln for ln in lines if GIT_ERROR_RE.match(ln) and not GIT_PUSH_SUMMARY_RE.match(ln)]
    if named:
        return '; '.join(named[:limit])
    rest = [ln for ln in lines
            if not ln.lower().startswith('hint:') and not GIT_PUSH_SUMMARY_RE.match(ln)]
    return (rest or lines or ['push failed'])[-1]


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
        return False, f'commit {branch} refused: {git_error((p.stderr or "") + chr(10) + (p.stdout or ""))}'
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


def _rewrite_account_names(wt, remote_sha, findings):
    """True when the worktree's unpublished commits were rewritten with every worker-account
    name as ``lane-N`` (:func:`asf.redact.rewrite_unpublished_names`) and the worktree moved to
    the result. Only for a finding that is a worker-account name, and only on a clean worktree
    (nothing the session left uncommitted is ever touched). A session cannot clear such a
    finding itself: its fix is a new commit, and the scan still reads the old ones — the same
    refusal every run, forever."""
    from asf import redact
    if not any(f.kind == 'name' and f.source == redact.NAME_SOURCE_POOL for f in findings):
        return False
    if _git(['status', '--porcelain', '--untracked-files=no'], wt).stdout.strip():
        return False
    try:
        pats = redact.default_patterns(wt)
        new = redact.rewrite_unpublished_names(
            wt, 'HEAD', pats, published=(remote_sha,) if remote_sha else ())
    except Exception:  # noqa: BLE001 — the refusal stands; nothing moved
        return False
    return bool(new) and _git(['reset', '-q', '--hard', new], wt).returncode == 0


def _push_retrying(args, wt, guard, conv=None, timeout=None, sleep=time.sleep,
                    backoff=PUBLISH_RETRY_BACKOFF_S):
    """``gitpush.push``, retried right here with backoff (B-0097) while the only thing refusing
    it is a transient network error — a blip the tick's own retry clears far more often than it
    survives to the next one. A non-network refusal (the hook, auth, a rejected ref) returns on
    its first try, same as before this retry existed. ``conv``/``timeout`` are
    :func:`asf.gitpush.push`'s own budget, carried unchanged into every retry.
    ``sleep`` is overridden by tests."""
    from asf import gitpush
    p = gitpush.push(args, wt, conv=conv, timeout=timeout, guard=guard)
    for delay in backoff:
        if p.returncode == 0:
            break
        text = git_error((p.stderr or '') + chr(10) + (p.stdout or ''))
        if not NETWORK_RE.search(text):
            break
        sleep(delay)
        p = gitpush.push(args, wt, conv=conv, timeout=timeout, guard=guard)
    return p


def publish(wt, branch, remote_sha='', main='main', protected=None, conv=None,
            push_timeout_s=None, droppable=None, transplant=False, sleep=time.sleep):
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

    The old tip's archive carries no new code, skips the product's pre-push hook and keeps the
    short ``git.ref_push_timeout_s``; the branch's own push runs the hook and gets
    ``git.push_timeout_s``, which is that hook's budget and not the network's — one number for
    both killed real publishes at 120 s (F-0165). ``push_timeout_s`` overrides the branch push
    alone. The line says how long the hook took, so the next operator who raises the budget
    raises it from a measurement.
    The branch's own push is retried in place, with backoff, while a transient network error
    (B-0097) is the only thing refusing it (:func:`_push_retrying`,
    :data:`PUBLISH_RETRY_BACKOFF_S`); a refusal that is still network after the backoff falls
    through to the caller, whose own next-tick retry (:func:`asf.workers.health.push_retry`)
    picks it up from there. A non-network refusal is never retried here.
    Before any push, the archive included, :func:`_redaction_findings` runs the same scan the
    hook would; a finding refuses the push right there with a precise ``redact: <file>:<line>
    …`` correction (:func:`asf.redact.correction`, F-0035) rather than the hook's generic
    refusal. A worker-account name is not refused but rewritten: the unpublished commits are
    replayed with it as ``lane-N`` (:func:`_rewrite_account_names`) — a session's own fix is a
    new commit the per-commit scan never credits. A push the hook itself still refuses over a
    redaction (some other repo, some other
    pattern list) has its captured output parsed the same way
    (:func:`asf.redact.parse_finding_lines`) before falling back to its raw last line.

    ``droppable`` (a ``path -> bool``, ``flags.mechanical`` only — :mod:`asf.harvest.mechanical`):
    where either refusal above would stand, origin's commits the head lacks that change ONLY
    files it says yes to — review and notes rounds, filed in the factory's own store — are
    dropped instead: the old tip archived, the head pushed over it under the lease. A commit
    that touches anything else still refuses.

    ``transplant`` (``flags.mechanical`` only — :func:`asf.harvest.mechanical.publish_worktree`
    decides it): the run's own report declared this head a rebase the factory publishes, and
    origin's tip is the one its worktree held — a branch cut fresh and the approved content
    carried over. Every commit origin holds that the head lacks is then dropped the same way:
    the old tip archived, the head pushed over it under the lease. ``(ok, line)``."""
    if transplant:
        droppable = _any_path
    what = TRANSPLANT_DROPS if transplant else 'review/notes round(s)'
    if not branch or branch == main:
        return False, f'publish refused: {branch or "no branch"} is not a lane branch'
    from asf import gitpush, redact, refguard
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
            # a transplant replaced those commits on purpose: never carried back onto it
            carried = False if transplant else carry_onto_head(wt, left)
            if not carried and not only_touching(wt, left, droppable):
                why = loss_refusal(branch, left, main, subjects)
                return False, f'publish {branch} refused: {why}'
            archive = copies_archive(branch, remote_sha)
            if refguard.refusal(archive, f'archive {branch}', main, protected):
                return False, f'publish {branch} refused: {archive} is a protected ref'
            named = ', '.join(f'{s} {subjects[s]}' if subjects.get(s) else s for s in left)
            rebased = (f'carried {len(left)} commit(s) the rebase dropped ({named}) '
                       f'(old tip kept as {archive})' if carried else
                       f'{DROPPED_ROUNDS} {len(left)} {what} origin held ({named}) '
                       f'(old tip kept as {archive})')
            lost = []
        if lost is None or lost:
            # a transplant is never rebased back onto the tip it replaces
            ok, fetched, rebased = (False, '', '') if transplant else \
                rebase_onto_remote(wt, branch)
            if not ok and only_touching(wt, lost, droppable):
                # origin's extra commits are review/notes rounds only: kept on an archive ref,
                # the head published over them under the lease on the tip the evidence read
                archive = copies_archive(branch, remote_sha)
                if refguard.refusal(archive, f'archive {branch}', main, protected):
                    return False, f'publish {branch} refused: {archive} is a protected ref'
                subjects = _subjects(wt, lost)
                named = ', '.join(f'{s} {subjects[s]}' if subjects.get(s) else s for s in lost)
                rebased = (f'{DROPPED_ROUNDS} {len(lost)} {what} origin held '
                           f'({named}) (old tip kept as {archive})')
            elif not ok:
                return False, f'publish {branch} refused: {rebased or loss_refusal(branch, lost)}'
            else:
                remote_sha = fetched
    findings = _redaction_findings(wt, remote_sha)
    before_rewrite = _git(['rev-parse', 'HEAD'], wt).stdout.strip()
    if findings and _rewrite_account_names(wt, remote_sha, findings):
        rebased = ', '.join(x for x in (rebased, 'worker-account names rewritten to lane-N') if x)
        findings = _redaction_findings(wt, remote_sha)
    rewritten = _git(['rev-parse', 'HEAD'], wt).stdout.strip() != before_rewrite

    def refused(why):
        # a refused publish leaves the worktree exactly as it was (#340): the rewrite moved HEAD
        if rewritten:
            _git(['reset', '-q', '--hard', before_rewrite], wt)
        return False, f'publish {branch} refused: {why}'

    if findings:
        return refused('; '.join(redact.correction(findings)))
    if archive:
        a = gitpush.push(['-q', 'origin', f'{remote_sha}:refs/heads/{archive}'], wt,
                         refs_only=True, conv=conv, guard=refguard.Guard(main, protected))
        if a.returncode != 0:
            return refused('the old tip could not be archived')
    args = ['-q', 'origin', f'HEAD:{ref}']
    if remote_sha:
        args.insert(1, f'--force-with-lease={ref}:{remote_sha}')
    p = _push_retrying(args, wt, refguard.Guard(main, protected), conv=conv,
                       timeout=push_timeout_s, sleep=sleep)
    if p.returncode != 0:
        hook_findings = redact.parse_finding_lines(f'{p.stderr or ""}\n{p.stdout or ""}')
        if hook_findings:
            return refused('; '.join(redact.correction(hook_findings)))
        return refused(git_error((p.stderr or "") + chr(10) + (p.stdout or "")))
    head = _git(['rev-parse', '--short', 'HEAD'], wt).stdout.strip()
    if rebased:
        return True, f'{rebased} and pushed: published {branch} at {head} in {p.seconds}s'
    return True, (f'published {branch} at {head} in {p.seconds}s'
                  + (' (rebased; lease held)' if remote_sha else ''))


def published_sha(before, after, line):
    """The sha a publish just put on origin, or ``''`` when nothing moved: ``after`` (the
    branch's ``ls-remote`` sha, read post-publish — never :func:`publish`'s own line, whose
    ``at <head>`` is nine characters and would make every publish look like progress against a
    forty-character ``launch_head``, F-0217) when it differs from ``before`` (the same branch's
    sha read before the publish was attempted) and ``line`` neither is empty nor carries a
    refusal. Pure: no git. What :func:`same_head_loop` folds onto a run's ledger line is this
    value, never ``after`` on its own — a publish that pushed the very head the item's launches
    were read on moved nothing (F-0217)."""
    if not after or after == before or not line or ' refused: ' in line:
        return ''
    return after


#: The words a publish line opens with when it dropped origin's review/notes rounds (``droppable``).
DROPPED_ROUNDS = 'dropped'
#: What a ``transplant`` publish says it dropped: the commits the session's declared rebase left.
TRANSPLANT_DROPS = 'commit(s) the declared transplant replaces,'


def _any_path(_path):
    return True


def only_touching(wt, shas, droppable):
    """True when ``droppable`` is given, ``shas`` is a non-empty list, and every one of those
    commits changes at least one file and only files ``droppable(path)`` says yes to."""
    if droppable is None or not shas:
        return False
    for sha in shas:
        p = _git(['diff-tree', '--no-commit-id', '--name-only', '-r', '--root', sha], wt)
        files = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
        if p.returncode != 0 or not files or not all(droppable(f) for f in files):
            return False
    return True


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
        """``origin``'s sha for exactly ``refs/heads/<branch>`` (:func:`asf.gitops.head_ref` —
        never a ``refs/heads/x/<branch>`` a bare tail pattern would also match) — ``''`` when
        origin has no such head; None when the snapshot cannot answer (a glob in the name, a
        checkout it cannot place, ``ls-remote`` failed)."""
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
        want = gitops.head_ref(branch)
        return next((sha for sha, ref in refs if ref.strip() == want), '')


def _worktree_git_dir(wt):
    """Where ``wt``'s own ``HEAD`` and ``index`` live — its ``.git`` directory when ``wt`` is the
    main worktree, or the linked worktree's own git dir when it is not (``.git/worktrees/<name>``)
    — read off the files exactly as :func:`_common_git_dir` does, but **stopping there rather
    than following ``commondir``**: a linked worktree's index is never in the common dir. None
    when it cannot be read off the files (the caller then asks git itself, uncached)."""
    dot = os.path.join(wt, '.git')
    try:
        if os.path.isdir(dot):
            return os.path.realpath(dot)
        with open(dot, encoding='utf-8') as f:
            first = f.readline().strip()
        if not first.startswith('gitdir:'):
            return None
        gd = first[len('gitdir:'):].strip()
        return os.path.realpath(gd if os.path.isabs(gd) else os.path.join(wt, gd))
    except OSError:
        return None


def _read_ref(gd, ref):
    """The sha a loose or packed ref named ``ref`` resolves to under git dir ``gd`` — the
    subprocess-free half of what ``git rev-parse`` would do for a ref name already in hand.
    None when neither file names it."""
    try:
        with open(os.path.join(gd, ref), encoding='utf-8') as f:
            return f.read().strip() or None
    except OSError:
        pass
    try:
        with open(os.path.join(gd, 'packed-refs'), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and line.endswith(' ' + ref):
                    return line.split(' ', 1)[0]
    except OSError:
        pass
    return None


def _status_lines(wt):
    """One ``git status --porcelain`` of ``wt``, as its non-blank lines — or None when the
    command itself failed (not a git worktree, or worse)."""
    p = _git(['status', '--porcelain'], wt)
    return [ln for ln in p.stdout.splitlines() if ln.strip()] if p.returncode == 0 else None


class WorktreeStatus:
    """``git status --porcelain`` for one pass over many worktrees: one process per worktree per
    *version* of that worktree, where the pass runs one per question — ten a run (P4).

    Keyed on what a change to the worktree moves: its HEAD sha, and the size and mtime of its own
    index (a linked worktree's index lives in its own git dir, not the common one). Every mutation
    the factory makes inside a pass — ``commit_leftovers``, ``publish``, the repair's ``reset``
    and ``cherry-pick`` — moves one of the two, so the re-gather that follows it is a miss by
    construction (P5, D7). A worktree whose git dir cannot be read is never cached: it is asked
    every time, exactly as today. ``git status`` itself opportunistically rewrites a stale index
    stat cache, so the key stored is the one taken *after* the process runs, not before.

    Entered from up to three publish threads at once (``PUBLISH_WORKERS``); ``run_steps``'s
    ``_groups`` union-find never runs two asks that share a worktree at the same time, so a plain
    dict is correct and needs no lock — the worst case of that guarantee ever breaking is two
    threads writing the same key with an equal value, never a wrong one. Do not add a
    ``threading.Lock``: it would serialise the publish phase this factory deliberately
    parallelised (PD4)."""

    def __init__(self):
        self._cache = {}

    def _key(self, wt):
        gd = _worktree_git_dir(wt)
        if gd is None:
            return None
        try:
            with open(os.path.join(gd, 'HEAD'), encoding='utf-8') as f:
                head = f.read().strip()
            idx = os.stat(os.path.join(gd, 'index'))
        except OSError:
            return None
        if head.startswith('ref:'):
            common = _common_git_dir(wt)
            if common is None:
                return None
            sha = _read_ref(common, head[len('ref:'):].strip())
        else:
            sha = head or None
        if not sha:
            return None
        return (os.path.realpath(wt), sha, idx.st_mtime_ns, idx.st_size)

    def lines(self, wt):
        """The porcelain lines, or None when the worktree is not one."""
        key = self._key(wt)
        if key is not None and key in self._cache:
            return self._cache[key]
        result = _status_lines(wt)
        # `git status` itself opportunistically refreshes the index's cached stat info when it
        # finds that stale — rewriting it, and moving the very key taken above. Stored under the
        # key taken again now, after the call, so the *next* lookup's pre-call key (nothing has
        # touched the index since) lands on this entry instead of missing it forever.
        key = self._key(wt)
        if key is not None:
            self._cache[key] = result
        return result


def gather(product, run, alive=None, worktree=None, heads=None, liveness=None, status=None):
    """The :class:`Evidence` for ``run`` — git in its worktree (``run['worktree']`` unless given),
    ``origin/<branch>`` from the product repo's remote, the log through the runtime. ``heads``
    (a :class:`RemoteHeads`) answers ``origin/<branch>`` for a pass over many worktrees.
    ``liveness``, when given, is a verdict callable over a pid, read into ``ev.liveness``.
    ``status`` (a :class:`WorktreeStatus`), when given, answers the ``git status`` below for a
    pass over many worktrees; ``None`` asks git directly, exactly as every caller that passes
    nothing already did."""
    alive = alive or pid_alive
    pid = run.get('pid')
    ev = Evidence(result=runtime_mod.read_result(run.get('log')), alive=alive(pid),
                  liveness=liveness(pid) if liveness else '')
    wt = worktree or run.get('worktree')
    branch = run.get('branch')
    main = getattr(product, 'main', 'main') or 'main'
    if not wt or not os.path.isdir(wt):
        return ev
    lines = status.lines(wt) if status is not None else _status_lines(wt)
    if lines is None:
        return ev
    ev.worktree = True
    ev.uncommitted = len(lines)
    ev.head = _git(['rev-parse', 'HEAD'], wt).stdout.strip()
    if branch:
        known = heads.sha(wt, branch) if heads is not None else None
        if known is None:
            known = remote_head(wt, branch)
        ev.remote_sha = known
        log = _git(['reflog', 'show', '--format=%gs', f'refs/heads/{branch}'], wt)
        ev.has_commits = log.returncode == 0 and any(
            not ln.startswith('branch: Created from') for ln in log.stdout.splitlines() if ln.strip())
        if not ev.has_commits and ev.remote_sha:
            # this worktree's own reflog is blind to work pushed before it ever checked the
            # branch out — a cloud session's direct push, or an earlier worktree already gone
            # (B-0584): read origin's own commits past the trunk instead of calling it empty
            ahead = _git(['rev-list', '--no-merges', f'origin/{main}..{ev.remote_sha}'], wt)
            ev.has_commits = ahead.returncode == 0 and bool(ahead.stdout.split())
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


#: ``judge``'s verdict on a run whose worktree holds nothing origin lacks — no file, no commit —
#: while its branch is not on origin (landed and deleted, or reaped): there is no work to push.
NOTHING_UNPUSHED = f'failed: {NOT_PUSHED}: 0 uncommitted file(s), 0 unpushed commit(s)'
#: The run kinds that work on a branch another run already pushed: a :data:`NOTHING_UNPUSHED`
#: end of one is a no-op, never an ``unpushed`` hold that buys a session to push nothing.
ON_A_PUSHED_BRANCH = ('correct', 'review', 'adjudicate', 'rebase')


def push_gap(ev):
    """The reason a result that says ok is not ``finished``: what is not on origin (B-0051)."""
    return f'{NOT_PUSHED}: {ev.uncommitted} uncommitted file(s), {ev.unpushed} unpushed commit(s)'


def unpublished(wt, branch, main='main', status=None):
    """``(ok, detail)``: whether ``branch``'s work in worktree ``wt`` is on origin, and what is
    missing when it is not — ``not pushed: <n> uncommitted file(s), <m> unpushed commit(s)``,
    the same text :func:`push_gap` formats from gathered evidence (B-0051).

    ``n`` is ``git status --porcelain`` (through ``status``, a :class:`WorktreeStatus`, when
    given — ``None`` asks git directly; a failed status reads as ``n = 0``, exactly as today);
    ``m`` is :func:`unpushed_commits`, by patch and above the trunk, so a branch harvest rebased
    after the session pushed it is not held forever (B-0053). A branch with no remote head at all
    is unpushed even with nothing to push: the session must publish the branch it was given
    (D-0048 then reads ``empty branch``)."""
    if not branch:
        head = _git(['rev-parse', '--abbrev-ref', 'HEAD'], wt)
        branch = head.stdout.strip() if head.returncode == 0 else ''
    if not branch or branch == 'HEAD':
        return False, 'no branch'
    lines = status.lines(wt) if status is not None else _status_lines(wt)
    n = len(lines) if lines is not None else 0
    remote = remote_head(wt, branch)  # asked of origin now, by its full ref — never a namesake
    m = unpushed_commits(wt, remote, main)
    if n == 0 and m == 0 and remote:
        return True, ''
    return False, push_gap(Evidence(uncommitted=n, unpushed=m))


def outcome_class(result):
    """The class of an ``end_reason`` (the ``sessions`` stream's ``result``), or None when the
    line describes no outcome — ``running``, ``unknown``, ``stopped`` (an operator's decision,
    not the factory's outcome), empty. Pure: no clock, no io."""
    text = (result or '').strip().lower()
    if text in ('', 'running', 'unknown', STOPPED):
        return None
    if text == FINISHED:
        return FINISHED
    if text == NOTHING_TO_LAND:
        return NOTHING_TO_LAND
    if text == DEAD_PID:
        return DEAD_PID
    if text == 'failed':
        return OTHER
    if text.startswith('failed: '):
        text = text[len('failed: '):].strip()
    for prefix in (NOT_PUSHED, OUTCOME_CLASSES[2], HOOK_REFUSED, NETWORK_ERROR):
        if text.startswith(prefix):
            return prefix
    # a ``failed: …`` signature is never NOTHING_TO_LAND: that class is not a failure, and
    # `judge` never writes it prefixed — a real failure that happens to share its words is OTHER
    if text == NOTHING_TO_LAND:
        return OTHER
    return text if text in OUTCOME_CLASSES[3:-1] else OTHER


def push_failure(text):
    """The retry class of a push's failure text — :data:`NETWORK_ERROR` for a transport error,
    :data:`HOOK_REFUSED` for a refusal by the repo's hook (or the redaction scan it runs) — or None
    for anything else, the factory's own publish refusals included."""
    text = text or ''
    if stale_head(text) or rebase_conflict(text):  # the factory's own refusal: never a retry
        return None
    if NETWORK_RE.search(text):
        return NETWORK_ERROR
    if HOOK_RE.search(text) or REDACT_REFUSAL_RE.search(text) or HOOK_REDACTION_RE.search(text):
        return HOOK_REFUSED
    return None


#: Kinds whose work is not a branch: a groom (adjudicate) session, or a groom-clerk half of one,
#: rules into the state dir's answers file and is told the repository is not its work, so it
#: never commits. An ``epic-features`` session is the same shape (F-0094): its one write is
#: ``asf inbox`` into the record, never a commit to its branch, so a session that files its
#: cards perfectly must not be judged on a push it was never going to make. A kernel
#: ``groom-fill`` session (:mod:`asf.kernel.dor`) only judges a card and ends with its verdict
#: block, which the kernel applies to the record (2026-10-10: the stop gate refused its stop
#: twice for a branch it was told never to push, and its final message lost the verdict).
NO_LANDING_KINDS = ('groom', 'groom-clerk', 'epic-features', 'groom-fill')

#: Kinds whose work is a file the factory takes off the branch, not a commit on it: a review
#: session writes ``docs/reviews/<n>-<item>.md`` in its worktree and leaves it uncommitted for
#: :mod:`asf.evidence.review_store` to file, because a push to the branch moves its PR head and
#: restarts the PR's whole CI. Only the kind is exempt, never the branch: a review runs on the
#: item's own branch, and the code and correction runs that share it must still be held for
#: their work — so these kinds are read apart from :data:`NO_LANDING_KINDS`, whose branch a
#: run of any kind inherits (B-0276).
OFF_BRANCH_KINDS = ('review',)


def lands(run, path=None):
    """False for a run whose work is not its branch: a :data:`NO_LANDING_KINDS` run, any run
    sent back on a branch such a run was launched on (a correction takes the branch, not the
    kind), or an :data:`OFF_BRANCH_KINDS` run — by kind alone, its branch being the item's own.
    Such a run is judged on its result alone, never held for an unpushed or empty branch."""
    if (run or {}).get('kind') in NO_LANDING_KINDS + OFF_BRANCH_KINDS:
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
        if ev.alive:
            return None
        # the CLI refused the account before its stream began: the refusal is in the raw log
        return AUTH_REASON if account_auth.in_log(run.get('log')) else DEAD_PID
    if not runtime_mod.result_ok(ev.result):
        sig = runtime_mod.failure_reason(ev.result)
        # a run that lands nothing may say `pushed: no` truthfully: that is not unpushed work
        if not (not landing and sig == runtime_mod.report.UNPUSHED
                and not ev.result.get('is_error')
                and ev.result.get('subtype', 'success') == 'success'):
            return f'failed: {sig}' if sig else 'failed'
    if run.get('branch') and landing and not ev.pushed:
        return f'failed: {push_gap(ev)}'
    # an :data:`OFF_BRANCH_KINDS` run is never held for a push (B-0276), but it still owes the
    # work it does not commit: a review session's is the review it wrote, filed off the branch
    # (asf.evidence.review_store), and one that filed none did nothing
    if run.get('branch') and (landing or (run or {}).get('kind') in OFF_BRANCH_KINDS) \
            and not ev.has_commits and not run.get('review_filed'):
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


#: The tick's IN FLIGHT column for an exit health has not judged yet (F-0199): it left, and the
#: pass that comes next says what it left behind.
EXITED_AWAITING = 'exited — awaiting harvest'

#: ``{state: the word the IN FLIGHT column prints}`` — :attr:`Status.label`'s spelling for a
#: padded column. One owner, so the table cannot word a state the views do not (P11).
INFLIGHT_WORDS = {WORKING: WORKING, FINISHED: FINISHED, NOTHING_TO_LAND: FINISHED,
                  ENDED_AWAITING_TICK: EXITED_AWAITING, FAILED: EXITED_AWAITING, DEAD: DEAD}


def inflight_word(status):
    """What the tick's IN FLIGHT table prints for ``status``. ``status`` may be a
    :class:`Status` or its bare ``name`` string, as :func:`holds_seat` takes either."""
    name = status.name if isinstance(status, Status) else status
    return INFLIGHT_WORDS.get(name, name)


def classify(run, ev, path=None):
    """One run and its evidence to one :class:`Status` (§2.1). Pure: no git, no clock, no file —
    ``judge`` and ``lands`` are the only calls, and ``path`` is passed through to ``lands`` exactly
    as :func:`derive` passes it today."""
    if run.get('ended'):
        reason = run.get('end_reason') or DEAD_PID
        if reason == FINISHED:
            return Status(FINISHED, result=FINISHED, source='ledger')
        if reason == NOTHING_TO_LAND:
            return Status(NOTHING_TO_LAND, result=NOTHING_TO_LAND, source='ledger')
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


def live_status(run, alive=None, result=None):
    """The :class:`Status` of a run that is still live on the ledger (no ``ended``), from its
    pid and its log alone — no git, no remote. §2.2's ladder stopped one rung short of the
    tick's own summary, which is best-effort console output and must not ``ls-remote`` once per
    row (F-0199 D3).

    ``working`` while the pid answers; ``finished`` for a pid that exited on a success result —
    the session's own claim, which health confirms or contradicts one pass later
    (:func:`finished_unrecorded`); ``ended-awaiting-tick`` for one that exited on a result that
    is not a success, which health will judge ``failed: …``; ``dead`` — :data:`NO_RECORD` —
    only for one that exited having written no result at all. ``result``: ``callable(run) ->
    result line`` (default :func:`result_of`)."""
    if (alive or pid_alive)(run.get('pid')):
        return Status(WORKING, source='pid')
    rec = (result or result_of)(run)
    if rec is None:
        return Status(DEAD, reason=NO_RECORD, source='pid')
    if runtime_mod.result_ok(rec):
        return Status(FINISHED, result=FINISHED, source='report')
    return Status(ENDED_AWAITING_TICK, result=judge(run, Evidence(result=rec)), source='report')


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


def derive(run, ev, cap=None, path=None):
    """The state of ``run`` given its evidence. Pure: no git, no clock. ``cap``: :func:`round_cap`."""
    cap = round_cap() if cap is None else cap
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
    if status.result == NOTHING_TO_LAND:
        return State(PUSHED, NOTHING_TO_LAND, rounds)
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
EMPTY_CAP = 2          #: ends that wrote nothing before the item is parked (`hold`'s own `empty_cap=`)
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
#: The wave's relaunch cap (:mod:`asf.workers.relaunch`): a job handed the same head, card and
#: cause twice — or once, when its report ended terminal — is parked, not launched again.
RELAUNCH_CAP = 'relaunch cap'


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


def blocked_park_text(question, item, probe=None):
    out = (f'the session ended with nothing to land and declared: {question} — the item is '
          f'parked, not handed to another session. Re-cut its card (a Task with no writes: '
          f'is re-cut by its RESHAPE → PLAN session), or `asf unpark {item}` to release it')
    if probe:
        out += f' — its command ran read-only, output attached ({probe["command"]})'
    return out


def blocked_park(question, item, card, now, probe=None):
    """``(fields, line)``: the pending correction that parks a run whose report declared a
    question, and the line to print. A function of its own and not a branch of :func:`hold`,
    because ``hold`` spends a round and counts findings and this spends neither.

    ``probe``: ``{'command', 'output'}`` when ``question`` named an exact, read-only command
    (:func:`asf.workers.report.operator_command`, :func:`asf.harvest.harvest.operator_readonly`)
    harvest ran itself (B-0042) — attached to the correction so the park carries the answer, not
    just the question, and the line says so."""
    reason = blocked_park_text(question, item, probe)
    fields = {'correction': {'kind': BLOCKED, 'text': question, 'at': now, 'parked': True,
                             'reason': reason, 'card': card}, 'operator_flagged': 1}
    if probe:
        fields['correction']['probe'] = probe
    return fields, f'parked {item}: {reason}'


#: Sessions of one kind handed the same head in a row before the item is parked (the loop guard).
LOOP_CAP = 3


#: The session kind a mechanical hold routes to (the feeder's FIX → CORRECT row): it never
#: reaches adjudicate, so launches of another kind say nothing about whether it loops.
CORRECT = 'correct'


def same_head_loop(path, run, head=None, cap=None, kind=None, main=None):
    """The sha ``run``'s item is looping on, or None: its last ``cap`` runs (a spent window's
    excepted) are all of ``kind`` — the kind the hold routes to, ``run``'s own when not given —
    and were all launched on one head (spawn's ``launch_head``) — so none of the first
    ``cap - 1`` added a commit — and the branch still sits on it when ``head`` is known (the
    last one added none either). A product, 2026-09-26: ``adjudicate-b-1377`` ×14 and
    ``correct-t-0338`` ×12, every one on a head nothing moved. Only runs started after the
    item's latest ``asf unpark`` (its ``unparked`` stamp) count: an unpark releases the item,
    and recounting the launches it released re-parked T-0338 on the very next tick.

    A worktree whose HEAD is a rebase of that sha onto a newer trunk (:func:`rebase_of`, with
    ``main`` given) moved the branch, though no commit is new: not a loop (a product's F-0037).

    A run the factory itself published (:func:`published_sha`, folded onto its ledger line as
    ``published_head``) moved the branch too, even with no worktree left to read: the record is
    the witness that survives a reaped worktree, where the head and trunk escapes above have
    nothing to read (a product's F-0003, 2026-09-27)."""
    cap = loop_cap() if cap is None else cap
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
    # the ledger's own witness: a publish recorded on any of these runs moved the branch, even
    # with no worktree left for the head/trunk escapes below to read (C1, P5)
    if any((r.get('published_head') or '') not in ('', sha) for r in rs):
        return None
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


def hold(path, run, kind, text, now, empty_cap=None, head=None, finding=None, main=None):
    """``(fields, line)``: what to append to ``run`` to hold its branch and hand it back, and
    the line to print. The rounds counter runs over every run of the item and stops climbing at
    :data:`ROUND_CAP` (B-0048). The escalation is counted on the hold's finding instead
    (:func:`next_finding`; ``finding``: its keys when the caller knows them — a review's C list):
    at :data:`ROUND_CAP` holds in a row on one finding the correction is marked ``at_cap`` so the
    feeder's ADJUDICATE row takes it, and a second hold at the cap flags the operator instead of
    spawning another. A different finding goes back to a correct session, whatever the rounds.
    An :data:`EMPTY` hold at ``empty_cap`` empty ends parks the item instead and spends no round,
    and so does the loop guard (:func:`same_head_loop`): the same kind handed the same head
    :func:`loop_cap` times — ``head``, when the caller knows it, is where the branch sits now;
    ``main``, when given, lets a worktree rebased onto a newer trunk count as a moved head."""
    if empty_cap is None:
        empty_cap = empty_ends_cap()
    cap = round_cap()
    item = run.get('item')
    branch = run.get('branch') or run.get('job')
    routes_to = CORRECT if kind in MECHANICAL else run.get('kind')
    loop = same_head_loop(path, run, head, kind=routes_to, main=main)
    # the head this hold judged: a correct round launched after the branch moved past it is
    # briefed with the real head and the commits since (asf.workers.judged)
    judged = head if isinstance(head, str) and re.fullmatch(r'[0-9a-fA-F]{7,40}', head) else None
    head = (text or '').split('\n', 1)[0]  # the line is one line; the correction keeps it all
    if loop:
        reason = loop_text(loop_cap(), routes_to, loop, item)
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
        fields = {'correction': dict({'kind': kind, 'text': text, 'at': now},
                                     **({'judged_head': judged} if judged else {}))}
        return fields, f'held {branch}: {head} — back to its session ({kind}, no round)'
    keys, same = next_finding(path, run, kind, text, finding)
    corr = {'kind': kind, 'text': text, 'at': now, 'finding': keys, 'same': same}
    if judged:
        corr['judged_head'] = judged
    if kind == INCOMPLETE and same >= incomplete_cap():
        # a resumed delivery left the very same Tasks unbuilt: pause after the second failure
        reason = incomplete_park_text(same, keys, item)
        fields = {'correction': dict(corr, parked=True, reason=reason), 'operator_flagged': 1}
        return fields, f'parked {branch}: {reason}'
    prev = max([rounds_of(path, item), run.get('rounds') or 0])
    fields = {'correction': corr}
    if prev < cap:  # B-0048: the counter never passes the cap
        fields['rounds'] = prev + 1
    if same >= cap:
        # the first at_cap hold hands the item to adjudicate; the next is that ruling's own
        # attempt failing; only a third — the adjudicate row cannot land either — flags
        at_cap_before = sum(1 for r in item_runs(path, item)
                            if (r.get('correction') or {}).get('at_cap'))
        corr['at_cap'] = True
        if at_cap_before >= 2:
            fields['operator_flagged'] = 1
        return fields, (f'held {branch}: {head} — adjudicate pending ({kind}: the same finding '
                        f'{same} times in a row)')
    return fields, f'held {branch}: {head} — back to its session (round {min(prev + 1, cap)})'


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
    return round_cap() if corr.get('at_cap') else 1


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
        if r.get('kind') == CORRECT and delivered(r) \
                and not quota_exhausted(r) \
                and (r.get('launch_head') or '').lower() == head.lower():
            return r.get('job')
    return None


def corrected_on(path, item, head):
    """The job of the newest ``correct`` run on ``item`` launched on ``head`` that ended (not on
    an exhausted quota), or None — a correction that has looked at the branch as it stands. The
    lane reads it beside a review that approved and asked for nothing (T5n): that correction
    had nothing to correct, and the branch lands instead of parking."""
    if not path or not item or not head:
        return None
    for r in sorted(item_runs(path, item), key=lambda r: r.get('started') or '', reverse=True):
        if r.get('kind') == CORRECT and r.get('ended') and not quota_exhausted(r) \
                and (r.get('launch_head') or '').lower() == head.lower():
            return r.get('job')
    return None


def delivered_off_branch(path, run, text):
    """Why ``run``'s empty branch is not a failure — the deliverable it produced somewhere its
    branch cannot show — or None. Pure: the ledger and the REPORT text, no git and no clock.

    Two kinds have such a deliverable. An ``adjudicate`` run's is the ``ruling:`` paragraph of its
    REPORT, which :func:`asf.tick.step_health.file_rulings` files on the item's card; its brief
    forbids the commit that would leave a mark on the branch, because an overruled finding is not
    edited. A ``correct`` run answering a **review** hold has none to produce when the lane's own
    restack already cleared the finding under it (B-0149): there is nothing to change, and the
    lane's answer is a fresh review round, not a pass.

    Either way the run must say so itself, in its own REPORT: ``status: done``, ``commits:`` claiming
    nothing (:func:`asf.workers.report._claim`), and no ``NEEDS OPERATOR`` — a question is F-0126's
    blocked park, which keeps winning over this."""
    from asf.workers import report as report_mod
    text = text or ''
    rep = report_mod.parse(text)
    status = (rep.get('status') or '').strip().lower().split(' ')[0]
    if status != 'done' or report_mod._claim(rep.get('commits')) or report_mod.needs_input(text):
        return None
    kind = run.get('kind')
    if kind == 'adjudicate':
        ruling = report_mod.ruling(text)
        return f'ruling filed: {ruling[:60]}…' if ruling else None
    if kind == CORRECT:
        rs = item_runs(path, run.get('item'))
        holds = [(r.get('correction') or {}) for r in rs]
        holds = [c for c in holds if c.get('kind') == REVIEW and c.get('at')]
        if not holds:
            return None
        last = max(holds, key=lambda c: c['at'])
        review_path = str(last.get('text') or '').split(' ', 1)[0]
        return f'{review_path}: the finding is gone' if review_path else None
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


def hook_refusal_hold(path, run, text, now, retried=False):
    """``(fields, line)``: ``run``'s push the repo's own pre-push hook refused. A redaction
    finding (:data:`HOOK_REDACTION_RE`) is parked as a security hold on the FIRST refusal: only a
    person decides what a flagged secret needs, never another session. Otherwise the first hold on
    a finding spends no round — the hook, not the session, is what failed, so the item is simply
    relaunched to try again (B-0097). A *second* hold naming the same finding in a row
    (:func:`next_finding`, at :data:`HOOK_REFUSAL_CAP`) is never tried a third time blind: the
    hook has now said the identical thing twice, so this reads what it said and routes by it
    (B-0140) — anything else is marked ``at_cap`` so the item goes to ADJUDICATE the way any other stuck finding does
    (:func:`hold`). A lint or test naming paths outside the Task's own ``writes:`` is left as a
    plain ``hook refused`` correction either way: :mod:`asf.tick.widen_footprint` turns that into
    a ``footprint`` hold the same tick, before a wave ever reads this one's ``at_cap``.

    ``retried`` (F-0235, :func:`asf.workers.report.hook_retried`) is True when the session's own
    report already said the hook refused the identical push twice in-run: it has then spent, in
    that one run, exactly the retry a second session would otherwise have been launched to make,
    so the FIRST hold is the cap for it — routed by :data:`HOOK_REFUSAL_CAP` the same as an
    unretried second hold. The unretried ladder is unchanged and the cap is still 2.
    ``corr['retried']`` is written only when ``retried`` is true, so a correction written before
    this reads exactly as it did before.

    Every return carries ``corr['refusal']``, the class of the refusal: ``'redaction'`` when
    :data:`HOOK_REDACTION_RE` matches, else ``'gate'`` — on the first hold and the second alike.
    A third class, ``'footprint'``, is never written here: it is
    :func:`asf.tick.widen_footprint.refusal_facts`'s own claim, for a refusal naming paths
    outside the Task's ``writes:``, and it keeps first claim the same tick (P11)."""
    branch = run.get('branch') or run.get('job')
    keys, same = next_finding(path, run, HOOK_REFUSED, text)
    redaction = HOOK_REDACTION_RE.search(text)
    refusal = 'redaction' if redaction else 'gate'
    corr = {'kind': HOOK_REFUSED, 'text': text, 'at': now, 'finding': keys, 'same': same,
            'refusal': refusal}
    if retried:
        corr['retried'] = True
    if redaction:
        # held on the first refusal: a product's correct-f-0086 was relaunched 100 times on one
        # redaction finding, its streak reset each time by a lane hold in between
        reason = (f'a redaction finding refused the push ({same} time(s) running) — a person '
                  'decides, not another session')
        corr.update(parked=True, reason=reason)
        return ({'correction': corr, 'operator_flagged': 1},
                f'held {branch}: {reason} (security hold, {refusal})')
    if same < hook_refusal_cap() and not retried:
        return {'correction': corr}, f'held {branch}: {text} (no round spent, {refusal})'
    corr['at_cap'] = True
    return ({'correction': corr},
            f'held {branch}: {text} — adjudicate pending (hook refused the same way {same} '
            f'times in a row, {refusal})')


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


#: The one way out of the empty-branch loop for an item whose work the trunk already holds under
#: another commit (landed before the card existed, or by another lane): no commit on the trunk
#: names the id, so no evidence rule can close the card, and a report that only says so is judged
#: empty again and relaunched until the loop parks it (five sessions on one Task, each
#: reporting "already on main"). The empty commit naming the id is the evidence the record reads.
ALREADY_LANDED = ('if the work is already on the trunk under another commit, verify it, run the '
                  'test that covers it, then make one empty signed commit per item naming its id '
                  '— `git commit --allow-empty -s -m "<kind>(<id>): already landed in <sha> — '
                  'verified by <test>"` — and push: a report that only says so closes nothing')


def unpushed_text(reason):
    """The correction a run judged ``failed: not pushed: …`` hands its next session."""
    gap = reason.split('failed: ', 1)[-1]
    return (f'{UNPUSHED} work: {gap} — commit and push what you have, or say why not in the '
            f'report; {ALREADY_LANDED}')


def empty_branch_text():
    """The correction a run judged ``failed: empty branch: …`` hands its next session (B-0076)."""
    return (f'{EMPTY_BRANCH} — commit and push what you have, or say why not in the report; '
            f'{ALREADY_LANDED}')


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


def reap_verdict(run, ev, main, alive, branch_landed=''):
    """``(what, detail)`` for a worktree: ``opening`` | ``keep`` | ``reapable``. ``run`` is None
    for an orphan. ``branch_landed`` is the sha the ledger says this worktree's *branch* landed
    at (:func:`branch_landings`), '' when it says none. Reapable under four rules — landed (the
    harvested sha is the evidence, B-0049); an ended-not-finished run with nothing in the tree to
    lose (B-0025); a finished run with commits of its own that the trunk holds, whatever origin
    still has of the branch (B-0019, F-0201); a finished run with commits of its own whose branch
    the ledger says landed under another run (F-0201)."""
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
    if not ev.has_commits:  # B-0019: a fresh branch is an ancestor of the trunk too — opening
        return 'keep', f'{what}: {"no commits yet" if ev.remote_sha else "branch not pushed"}'
    if ev.in_trunk:  # the trunk holds every commit this tree has: nothing here is only here
        return 'reapable', what
    if branch_landed:  # the lane landed this branch on another run — a rebase or a squash
        return 'reapable', f'landed {branch_landed} on its branch'
    if not ev.remote_sha:
        return 'keep', f'{what}: branch not pushed'
    if not ev.head_on_remote:
        return 'keep', f'{what}: local commits not pushed'
    return 'keep', f'{what}: not in origin/{main}'
