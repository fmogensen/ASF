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

**The registry** (``sessions.jsonl``) is append-only. A line carrying ``started`` and ``pid`` is
a launch and opens a new run of its job; every other line for that job updates its *latest* run
(:func:`fold`). A run's terminal fields never fold into the next run (B-0041) — the boundary is
the launch line, not a set of nulls the launcher has to remember to write.

**Evidence** is gathered, never remembered: the log's last-run result (:func:`asf.workers.runtime
.read_result`), the pid, and git — ``origin/<branch>``, the worktree's tree and HEAD
(:func:`gather` → :class:`Evidence`). The state is derived from the run and the evidence
(:func:`derive`); nothing stores it a second time. The one recorded transition is health's
``ended`` line (the timestamp and the reason at the time), so the feeder, the pool and spawn
can ask "is it live" without git (:func:`is_live`).

Every other module asks this one:

* spawn — :func:`may_launch`: refuse only a live run's worktree; an ended (or dead-pid) run's
  worktree and branch are reused (B-0025, B-0046, B-0048, B-0051);
* health — :func:`judge` (what ``ended`` line to write) and :func:`reap_verdict`;
* harvest — :func:`eligible` (pushed, not landed) and :func:`by_branch`;
* the feeder, the wave, the pool and capacity — :func:`occupies` (live on the ledger AND the pid
  answers: a dead run is no load even before health records its end), :func:`inflight`,
  :func:`attempts`, :func:`corrections`;
* the PR step — :func:`finished`.
"""
import dataclasses
import json
import os
import signal
import subprocess
import time

from asf import env
from asf.workers import runtime as runtime_mod

#: Corrections a held branch gets before the feeder switches to an ADJUDICATE row.
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
              'resumed', 'continued')

FINISHED = 'finished'
DEAD_PID = 'dead pid'
STOPPED = 'stopped'
PUSHED_AFTER_STOP = 'pushed after stop'
EMPTY_BRANCH = 'empty branch: nothing to land'
#: The prefix of a ``push_gap`` line: work done and not on origin (B-0051). One owner for the
#: string the classifier matches on.
NOT_PUSHED = 'not pushed'
#: A failure whose signature this module does not name.
OTHER = 'other'

#: Every class a session's ``end_reason`` falls into. ``finished`` is the only one that is not a
#: failure; ``other`` is a failure whose signature this module does not name. Composed from the
#: constants that write the strings, so a rename follows.
OUTCOME_CLASSES = (FINISHED, NOT_PUSHED, EMPTY_BRANCH.split(':')[0], DEAD_PID, PUSHED_AFTER_STOP,
                   runtime_mod.report.UNPUSHED, *(name for name, _ in runtime_mod.FAILURE_SIGNATURES),
                   OTHER)
FAILING_CLASSES = tuple(c for c in OUTCOME_CLASSES if c != FINISHED)


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
    out = []
    if not path or not os.path.isfile(path):
        return out
    product_from_dir = _product_from_registry_dir(path)
    with open(path, encoding='utf-8') as f:
        for line in f:
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


def fold(lines):
    """``{job: [run, …]}`` in launch order. A launch line opens a run; any other line updates
    the latest run of its job (a line before any launch opens a run of its own — a hand-written
    registry). A run field written as null is dropped."""
    runs = {}
    for rec in lines:
        job = rec['job']
        if is_launch(rec) or job not in runs:
            runs.setdefault(job, []).append(dict(rec))
        else:
            runs[job][-1].update(rec)
    return {job: [_clean(r) for r in rs] for job, rs in runs.items()}


def runs(path):
    return fold(read_lines(path))


def latest(path):
    """``{job: its latest run}`` — what every reader of the registry means by "the session"."""
    return {job: rs[-1] for job, rs in runs(path).items()}


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


def by_branch(path):
    """``{branch: the latest run on it}`` across jobs, in file order (the last launch naming a
    branch owns it): a branch held and sent back runs under a new job name, and it is that run
    harvest reads."""
    out = {}
    for rs in runs(path).values():
        for r in rs:
            if r.get('branch'):
                out[r['branch']] = r
    return out


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
    return max([r.get('rounds') or 0 for rs in runs(path).values() for r in rs
                if r.get('item') == item] + [0])


def item_runs(path, item):
    return [r for rs in runs(path).values() for r in rs if item and r.get('item') == item]


# ---- the recorded transition, and the questions that need no git ----------------

def is_live(run):
    """No ``ended`` line yet: health has not recorded the end. The feeder's inflight, the pool's
    load and spawn's refusal all read this and nothing else."""
    return bool(run) and not run.get('ended')


def pid_alive(pid):
    """The pid answers a signal 0 (a pid we may not signal is someone's, so alive)."""
    if not pid:
        return False
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
    for rs in runs(path).values():
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
    it is an empty-branch correction on a branch whose work an earlier run already landed."""
    corr = (run or {}).get('correction') or {}
    if not corr.get('text'):
        return None
    if path is not None and empty_on_a_landed_lane(path, run):
        return None
    at = corr.get('at') or ''
    if path is not None:
        me = (run.get('job'), run.get('started'))
        later = [r for r in item_runs(path, run.get('item'))
                 if (r.get('started') or '') >= at and (r.get('job'), r.get('started')) != me]
        if later:
            return None
    return corr


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
        if not run.get('item') or pending_correction(run, path):
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
        if not item or not kind or landed(run) or pending_correction(run, path):
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


def attempts(path):
    """``{item: runs the registry holds for it}`` — every launch, ended or not."""
    out = {}
    for rs in runs(path).values():
        for r in rs:
            if r.get('item'):
                out[r['item']] = out.get(r['item'], 0) + 1
    return out


def corrections(path):
    """``{item: {kind, text, at, rounds, branch}}``: the newest pending correction per item, with
    the branch of the run it was written on (a held spec branch is corrected on ``spec/<id>``,
    not on the item's task prefix)."""
    out = {}
    for item in {r.get('item') for rs in runs(path).values() for r in rs if r.get('item')}:
        held = [(r, pending_correction(r, path)) for r in item_runs(path, item)]
        held = [(r, c) for r, c in held if c]
        if not held:
            continue
        run, corr = max(held, key=lambda rc: rc[1].get('at') or '')
        out[item] = dict(corr, rounds=rounds_of(path, item), branch=run.get('branch'))
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


def publish(wt, branch, remote_sha='', main='main'):
    """Push the worktree's HEAD to ``origin/<branch>`` as the factory (B-0056).

    A rebased lane branch — spawn's takeover rebase (B-0046, B-0048) or a conflict the session
    finished resolving — holds commits origin does not, and no push a session may make brings
    them there: a plain push is not a fast-forward and the standing rules and the worker settings
    forbid a force. Told "push the same branch, never a force", a session did the one thing left
    and merged its own stale remote (eight spec branches, 13 to 20 commits of tangle each). So
    the factory publishes, never the session: ``--force-with-lease=<branch>:<remote_sha>`` when
    the branch is on origin (origin moving since the evidence was gathered refuses the push —
    nothing is overwritten unseen), a plain push when it is not. The trunk is never a target.
    ``(ok, line)``."""
    if not branch or branch == main:
        return False, f'publish refused: {branch or "no branch"} is not a lane branch'
    ref = f'refs/heads/{branch}'
    args = ['push', '-q', 'origin', f'HEAD:{ref}']
    if remote_sha:
        args.insert(2, f'--force-with-lease={ref}:{remote_sha}')
    p = _git(args, wt)
    if p.returncode != 0:
        why = [ln for ln in (p.stderr or p.stdout).splitlines() if ln.strip()]
        return False, f'publish {branch} refused: {why[-1].strip() if why else "push failed"}'
    head = _git(['rev-parse', '--short', 'HEAD'], wt).stdout.strip()
    return True, f'published {branch} at {head}' + (' (rebased; lease held)' if remote_sha else '')


def gather(product, run, alive=None, worktree=None):
    """The :class:`Evidence` for ``run`` — git in its worktree (``run['worktree']`` unless given),
    ``origin/<branch>`` from the product repo's remote, the log through the runtime."""
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
        ls = _git(['ls-remote', '--heads', 'origin', branch], wt)
        ev.remote_sha = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''
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
    for prefix in (NOT_PUSHED, OUTCOME_CLASSES[2]):
        if text.startswith(prefix):
            return prefix
    return text if text in OUTCOME_CLASSES[3:-1] else OTHER


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
        for rs in runs(path).values():
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
        if not ev.has_commits:
            return f'failed: {EMPTY_BRANCH}'
    return FINISHED


def derive(run, ev, cap=ROUND_CAP, path=None):
    """The state of ``run`` given its evidence. Pure: no git, no clock."""
    if landed(run):
        return State(LANDED if ev.worktree else REAPED)
    corr = pending_correction(run, path)
    rounds = run.get('rounds') or 0
    if corr:
        if corr.get('at_cap') or rounds >= cap:
            return State(ADJUDICATE, corr.get('text', ''), rounds)
        return State(HELD, corr.get('text', ''), rounds)
    if (run.get('correction') or {}).get('text'):
        return State(CORRECTED, run['correction'].get('text', ''), rounds)
    if run.get('ended'):
        reason = run.get('end_reason') or DEAD_PID
        if reason == FINISHED:
            return State(PUSHED, FINISHED, rounds)
        return State(ENDED, reason, rounds)
    reason = judge(run, ev, landing=lands(run, path))
    if reason is None:
        return State(RUNNING if ev.has_commits else LAUNCHED)
    if reason == FINISHED:
        return State(PUSHED, FINISHED, rounds)
    return State(ENDED, reason, rounds)


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


def hold(path, run, kind, text, now, empty_cap=EMPTY_CAP):
    """``(fields, line)``: what to append to ``run`` to hold its branch and hand it back, and
    the line to print. The rounds counter runs over every run of the item; at :data:`ROUND_CAP`
    it stops climbing (B-0048) — the correction is marked ``at_cap`` so the feeder's ADJUDICATE
    row takes it, and a second hold at the cap flags the operator instead of spawning another.
    An :data:`EMPTY` hold at ``empty_cap`` empty ends parks the item instead and spends no round."""
    item = run.get('item')
    branch = run.get('branch') or run.get('job')
    if kind == EMPTY and empty_ends(path, item) >= empty_cap:
        fields = {'correction': {'kind': kind, 'text': text, 'at': now,
                                 'parked': True, 'reason': park_text(empty_ends(path, item))},
                  'operator_flagged': 1}
        return fields, f'parked {branch}: {fields["correction"]["reason"]}'
    prev = max([rounds_of(path, item), run.get('rounds') or 0])
    if prev >= ROUND_CAP:
        at_cap_before = any((r.get('correction') or {}).get('at_cap') for r in item_runs(path, item))
        fields = {'correction': {'kind': kind, 'text': text, 'at': now, 'at_cap': True}}
        if at_cap_before:
            fields['operator_flagged'] = 1
        return fields, f'held {branch}: {text} — adjudicate pending'
    rounds = prev + 1
    fields = {'rounds': rounds, 'correction': {'kind': kind, 'text': text, 'at': now}}
    return fields, f'held {branch}: {text} — back to its session (round {rounds})'


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


def widenings(path, item):
    """How many times ``item``'s footprint was already widened: its runs that carry ``widened``
    (the paths the rule added, written beside the correction and never overwritten by one)."""
    if not item:
        return 0
    return sum(1 for r in item_runs(path, item) if r.get('widened'))


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
