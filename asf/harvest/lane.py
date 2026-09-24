"""asf.harvest.lane — the PR lane as one state machine (the contract; W1 implements it).

A lane branch's life — pushed, a PR (or, fast-forward, the branch as its own PR), a review, the
gate, merged or back — is one state per branch, moved only here. Harvest shrinks to: fetch,
list the lane branches, :func:`advance` each, then one :func:`gate_set` over every branch in
GATE. Every other module reads the lane through :func:`state`, :func:`busy_items` and
:func:`snapshot`; none writes it.

**Where the state lives.** On the run line in ``state/<product>/sessions.jsonl``, never a ledger
of its own: a transition is ``mark_session(job, lane={state, head, pr, at, reason})`` on the run
that owns the branch, and :func:`asf.workers.lifecycle.by_branch` gives "the last run naming a
branch owns it". Adopting a branch or PR no run made writes a synthetic run (as
``land_spec.adopt`` does today). A v0.1.2 binary reading the same file ignores the extra
``lane`` field, so rollback needs no migration. Transitions that finish a run still write
``harvested:`` / ``correction:`` on it through ``lifecycle.hold`` and ``mark_session``.

**When it runs.** The transitions the feeder reads (PUSHED, PR_OPEN, REVIEW, BACK, STALE, and a
moved head) are computed in-process in the tick, before the wave. Only the GATE outcomes
(MERGED, BACK, WAITING, WAITING_CI, QUEUED) are decided in the detached harvest. Before any
external merge the lane writes the intent ``MERGING pr=<n>``; a later T11 (found merged) with a
prior MERGING is our own merge, not a foreign one.

**Facts.** :func:`facts` gathers everything once per tick — one ``gh pr list --state all``, one
``git ls-remote``, the run per branch, the newest review per head
(:func:`asf.evidence.review.newest`) and the gate cache — and every transition is a pure
function of them (:func:`next_state`), unit-tested with no git and no ``gh``.

Transitions (plan §2 plus the §9 overrides):

- T1  (run) → PUSHED          the run ended ``finished`` and ``origin/<branch>`` is its head
- T2  PUSHED → PR_OPEN         a PR was opened or one already exists (adoption); FF: at once, pr=None
- T3  PR_OPEN → REVIEW         ``conventions.lane.review`` requires a review for the landing
                               class and no review round reviewed this head (a state, no round)
- T4  REVIEW → GATE            the current review reads approved, or the policy is ``none``
- T5  REVIEW → BACK            the current review reads changes (``kind=review``, rounds+1)
- T6  GATE → WAITING_CI        required checks pending/absent under ``landing_checks_missing: wait``
- T7  WAITING_CI → GATE        checks passed (``via=ci``) or the wait expired (``via=local``)
- T8  GATE → WAITING           trunk red alone, no merge budget, deferred, or a shared path
                               already in this tick's set — never a correction, never a round
- T9  GATE → BACK              red alone on a green trunk, conflict, footprint ``reshape``, or a
                               pre-push hook refusal (``kind=gate|conflict|footprint|hook``)
- T10 GATE → MERGING → MERGED  merged by the host (FF ``push_ff``; PR ``gh pr merge``)
- T10q GATE → QUEUED → MERGED  a merge queue took it; a queue rejection → BACK or WAITING;
                               QUEUED counts toward the in-queue budget
- T11 any open → MERGED        found merged outside the lane, or already on the trunk
                               (``method=external|on-trunk``; ours when a MERGING precedes it)
- T12 any open → STALE         PR closed unmerged, branch gone, item closed/superseded, or
                               unchanged and unheld past ``conventions.lane.stale_after``
- T12r STALE → PR_OPEN         the PR was reopened
- T13 BACK → PUSHED            the correcting session finished with a new head
- Th  any open, head moved     → PUSHED(new head)
- T14 MERGED/STALE → REAPED    the worktree was removed (health writes it)
"""

PUSHED = 'PUSHED'
PR_OPEN = 'PR_OPEN'
REVIEW = 'REVIEW'
GATE = 'GATE'
WAITING_CI = 'WAITING_CI'
WAITING = 'WAITING'
QUEUED = 'QUEUED'
MERGING = 'MERGING'
BACK = 'BACK'
MERGED = 'MERGED'
STALE = 'STALE'
REAPED = 'REAPED'

#: Every lane state, in the order a branch normally passes them.
LANE_STATES = (PUSHED, PR_OPEN, REVIEW, GATE, WAITING_CI, WAITING, QUEUED, MERGING, BACK,
               MERGED, STALE, REAPED)
#: The states a branch ends in: no transition out except to REAPED (and STALE → PR_OPEN on a
#: reopened PR).
TERMINAL_STATES = (MERGED, STALE, REAPED)
#: The states that still move — each has an outgoing transition with a time bound.
OPEN_STATES = tuple(s for s in LANE_STATES if s not in TERMINAL_STATES)
#: The states in which the branch's item is busy for the feeder (no launch row): every open
#: state except BACK, which is the feeder's to correct.
BUSY_STATES = tuple(s for s in OPEN_STATES if s != BACK)

#: The two landing classes (:func:`landing_class`).
DOCS = 'docs'
CODE = 'code'


def facts(product):
    """Gather the tick's lane facts once: one ``gh pr list --state all`` (none in FF mode), one
    ``git ls-remote``, :func:`asf.workers.lifecycle.by_branch`, the newest review per branch head
    (:func:`asf.evidence.review.newest`) and the gate cache.

    Returns a mapping ``{branch: BranchFacts}`` over every lane branch (a branch under the
    product's prefixes, or one an open PR names). Each branch's facts carry at least ``head``
    (the remote sha or None), ``item``, ``run`` (the owning run's folded record, or None),
    ``pr`` (the host's view, or None), ``review`` (``(round, verdict, head)`` or None),
    ``landing_class``, ``on_trunk`` and ``now``. Read-only: no git write, no ``gh`` write."""
    raise NotImplementedError('lane.facts: W1')


def next_state(prev, facts):
    """The pure transition: ``prev`` is the branch's last lane record (``{state, head, pr, at,
    reason}``, or None for a branch with none yet) and ``facts`` that branch's facts from
    :func:`facts`. Returns ``(state, reason)`` — ``state`` one of :data:`LANE_STATES`, equal to
    ``prev['state']`` when nothing moves. No I/O, no clock beyond ``facts['now']``."""
    raise NotImplementedError('lane.next_state: W1')


def advance(product, branch, facts):
    """Decide ``branch``'s next state from its facts (:func:`next_state`) and, when it moved,
    write it: ``mark_session(job, lane={state, head, pr, at, reason})`` on the owning run (a
    synthetic run for an adopted branch), plus ``lifecycle.hold`` for BACK and ``harvested=``
    for MERGED. Performs the transition's side effect (open a PR via the host) where the table
    names one. Returns the lane record written, or None when the state did not change. The only
    writer of a lane state."""
    raise NotImplementedError('lane.advance: W1')


def state(product, branch):
    """``branch``'s current lane record (``{state, head, pr, at, reason}``) from the run that
    owns it, or None when no run carries one. Read-only."""
    raise NotImplementedError('lane.state: W1')


def busy_items(product):
    """The item ids whose branch is in one of :data:`BUSY_STATES` — what
    :func:`asf.workers.lifecycle.occupancy` folds in, so the feeder starts no second session
    for an item the lane holds. Read-only."""
    raise NotImplementedError('lane.busy_items: W1')


def snapshot(product):
    """Every lane branch's current record, ``{branch: {state, head, pr, at, reason, item}}``,
    for the views, ``asf tick --dry-run`` and the invariants (I8). Read-only."""
    raise NotImplementedError('lane.snapshot: W1')


def gate_set(product, entries):
    """The one gate, both landing modes. ``entries`` are the branches in GATE this tick (at most
    one touching a ``conventions.shared_paths`` path; the rest WAITING ``shared-path``). Builds a
    combined head, bisects on the red modules, confirms in full, checks the trunk once when
    something is red, and merges the green through the product's :class:`Host`. A trunk that
    moved only on docs roots (:func:`landing_class`) does not re-gate a green branch.

    Returns ``[(branch, state, reason)]``, one per entry, ``state`` one of MERGED, QUEUED, BACK,
    WAITING or WAITING_CI; :func:`advance` records them. ``harvest_gate: per-branch`` is a set of
    size one."""
    raise NotImplementedError('lane.gate_set: W1')


def landing_class(product, files):
    """:data:`DOCS` when every path in ``files`` lies under a docs root — ``specs_dir``,
    ``plans_dir``, ``reviews_dir`` or a ``conventions.doc_paths`` glob — else :data:`CODE`. The
    one rule for docs vs code (replaces ``is_docs_branch``, ``is_inert``, ``docs_only``)."""
    raise NotImplementedError('lane.landing_class: W1')


# ---- the host ---------------------------------------------------------------

class Host:
    """Where a lane branch lands. :func:`host` picks the adapter from the product's ``landing``;
    the state machine is the same for both."""

    def __init__(self, product):
        self.product = product

    def open(self, branch):
        """Open the branch's PR, or adopt the open one already naming it. Returns the PR number,
        or None (fast-forward: the branch is its own PR, T2 passes at once)."""
        raise NotImplementedError

    def status(self, branch):
        """The host's view of the branch: ``{pr, state, head, checks, merged, merge_sha,
        queued}`` — ``checks`` one of ``none|pending|passed|failed``."""
        raise NotImplementedError

    def merge(self, branch, pr):
        """Land it: ``(sha, method)`` with ``method`` one of ``ff|squash|merge|rebase|queue``, or
        a refusal ``(None, reason)``. The caller writes MERGING before calling."""
        raise NotImplementedError


class FastForwardHost(Host):
    """``landing: fast-forward``: no PR host. ``open`` → None; ``status`` → no checks, merged
    when the head is on ``origin/<trunk>``; ``merge`` → ``push_ff``, method ``ff``."""

    def open(self, branch):
        raise NotImplementedError('lane.FastForwardHost.open: W1')

    def status(self, branch):
        raise NotImplementedError('lane.FastForwardHost.status: W1')

    def merge(self, branch, pr):
        raise NotImplementedError('lane.FastForwardHost.merge: W1')


class GitHubHost(Host):
    """``landing: pull-request`` on GitHub: ``gh pr create`` / adopt; ``gh pr view`` and the
    checks; ``gh pr merge`` (squash first) or ``--auto`` into a merge queue (QUEUED)."""

    def open(self, branch):
        raise NotImplementedError('lane.GitHubHost.open: W1')

    def status(self, branch):
        raise NotImplementedError('lane.GitHubHost.status: W1')

    def merge(self, branch, pr):
        raise NotImplementedError('lane.GitHubHost.merge: W1')


def host(product):
    """The :class:`Host` for the product's ``landing``: :class:`GitHubHost` for
    ``pull-request``, else :class:`FastForwardHost`."""
    raise NotImplementedError('lane.host: W1')
