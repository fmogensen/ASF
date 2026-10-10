"""asf.kernel.model — the facts the kernel decides on, as plain values (ASF 0.2).

One loop: read facts (the record, GitHub, the sessions) -> :func:`asf.kernel.decide.decide` ->
apply the plan's actions. Everything here is what the reader hands ``decide``: no method reaches
a disk, a network or a clock, so a test builds a world by hand and asks what the kernel would do.

Every Task or Bug has exactly one :class:`State`. A Feature's or a Story's state is never stored:
it is derived from the Tasks whose ``parent`` link points at it, and — for a Story — from the Tasks
that declare it on their ``stories:`` line (:attr:`Item.stories`); nothing a body merely mentions
counts.
"""
import dataclasses
import enum


class State(enum.Enum):
    """The one state of a Task or a Bug: ``New -> Ready -> Building -> Review -> Landing -> Done``,
    or ``Stuck`` from any of them (with a :class:`Stuck` saying why, whose move, and what next).

    - ``NEW``: on the record, not yet launchable (a Feature or Epic with no rank in its lineage,
      an open ``after:`` edge, or its spec has not landed). A Task or Bug inherits its nearest
      ancestor's rank, and launches after ranked work when its lineage has none.
    - ``READY``: launchable — its declared ``after:`` edges are Done; may carry review findings.
    - ``BUILDING``: a live session holds it.
    - ``REVIEW``: an open PR whose head tree has no verdict yet.
    - ``LANDING``: an open PR approved on its head tree, waiting on required checks / auto-merge.
    - ``DONE``: its PR merged (a reopen moves a Done item back, and a new PR is accepted).
    - ``STUCK``: nothing the loop does will move it; :class:`Stuck` names the owner.
    - ``PARKED``: the item, or an ancestor (Story, Feature, Epic), is ``priority: later`` — it is
      invisible: no launch, no PR upkeep, no mint, never Stuck, never counted as blocking.
      Derived every tick and never stored on the card, so un-parking finds the card as it was.
    """
    NEW = 'new'
    READY = 'ready'
    BUILDING = 'building'
    REVIEW = 'review'
    LANDING = 'landing'
    DONE = 'done'
    STUCK = 'stuck'
    PARKED = 'parked'


#: who must act on a Stuck item: the loop itself, a session, CI, or the operator
OWNERS = ('loop', 'session', 'ci', 'operator')

#: how a session ended (``Session.result``)
RESULTS = ('pushed', 'report', 'question', 'none')

#: a check conclusion that is a verdict of red; every other conclusion (``skipped``,
#: ``cancelled``, ``neutral``, ...) and every status short of ``completed`` is no verdict
RED_CONCLUSIONS = ('failure', 'timed_out')


@dataclasses.dataclass
class Stuck:
    """Why an item is Stuck. ``owner`` is one of :data:`OWNERS`; ``next_action`` is one line of
    what that owner does next; ``blocked_count`` is how many items transitively wait on this one
    (through declared ``after:`` edges between non-``later`` items)."""
    reason: str
    owner: str
    next_action: str = ''
    blocked_count: int = 0


@dataclasses.dataclass
class Item:
    """One card on the record (any type), as the reader canonicalizes it
    (:func:`asf.record.core.load_items` + :func:`asf.record.core.canonicalize`).

    ``parent`` is the one membership link: a Task belongs to the Story or Feature it names there,
    and to nothing it merely mentions in ``body``. ``rank`` orders launches (lower first; ``None``
    is unranked). ``priority == 'later'`` parks the item and every item under it
    (:attr:`State.PARKED`): nothing waits on it and its ``writes`` hold no one. ``after`` lists
    the declared wait edges.
    ``writes`` are the path globs the item declares it will change. ``stories`` are the Story ids
    a Task declares it covers (its ``stories:`` field, else its body's ``stories:`` line): each
    such Story takes its state from that Task as from a child.

    ``state``/``stuck`` are what the card records now. ``attempts`` is the reason of each failed
    attempt, oldest first (a launch error, a push-less session end); ``fix_rounds`` counts the
    red-driven rounds already spent; ``findings`` are the open review findings carried into the
    next launch; ``answers`` are operator answers already written to the card; ``question`` is
    the open question a session asked. ``notes`` are the questions a session asked while its work
    still moved on (a ``done`` REPORT with a pushed head): kept for status, holding nothing.
    ``reopened`` is set when a Done item was reopened. ``stuck_since`` is when the card's Stuck
    was recorded (ISO-8601 UTC, None when unknown): an operator answer given after it clears it.
    ``extra_rounds`` are the fix rounds operator answers granted beyond ``max_fix_rounds``.
    ``rebuilds`` counts the times the kernel archived the item's branch, closed its PR and built
    it afresh (:class:`asf.kernel.actions.ArchiveAndReset`): at most once per item.
    ``stale_stuck`` is set when the card still stores a Stuck the kernel no longer reads (a
    retired card reads as Done): the next tick clears those fields off the card.
    """
    id: str
    type: str = 'task'
    title: str = ''
    parent: str = None
    rank: int = None
    priority: str = None
    after: list = dataclasses.field(default_factory=list)
    writes: list = dataclasses.field(default_factory=list)
    stories: list = dataclasses.field(default_factory=list)
    body: str = ''
    state: State = State.NEW
    stuck: Stuck = None
    attempts: list = dataclasses.field(default_factory=list)
    fix_rounds: int = 0
    findings: list = dataclasses.field(default_factory=list)
    answers: list = dataclasses.field(default_factory=list)
    question: str = None
    reopened: bool = False
    notes: list = dataclasses.field(default_factory=list)
    stuck_since: str = None
    extra_rounds: int = 0
    rebuilds: int = 0
    stale_stuck: bool = False


@dataclasses.dataclass
class Check:
    """One check run on a PR head. ``status`` is GitHub's (``queued``, ``in_progress``,
    ``completed``); ``conclusion`` is set only when completed. Only a conclusion in
    :data:`RED_CONCLUSIONS` is red. ``failing_files`` are the files the failing tests live in or
    exercise (empty when unknown); ``attempt`` is the run's attempt number (1 = never rerun).
    ``failed_step`` is the name of the step that failed and ``log_tail`` the last lines of its
    failed log ('' when unread): a fix round on a red with no known files carries them."""
    name: str
    status: str = 'completed'
    conclusion: str = None
    run_id: int = None
    failing_files: list = dataclasses.field(default_factory=list)
    attempt: int = 1
    failed_step: str = ''
    log_tail: str = ''


@dataclasses.dataclass
class PR:
    """One pull request against trunk. ``tree_sha`` is the head commit's tree: a rebase that
    changes nothing keeps it, so a verdict keyed by it survives. ``change_id`` is the PR's own
    change: a hash of the diff from the merge base to the head, blind to line numbers, so a
    merge of trunk into the branch (GitHub's "update branch") keeps it while a new commit on the
    PR changes it ('' when unread). ``behind``: the base moved past
    the PR's base; ``conflicting``: GitHub cannot merge it as is (``mergeable`` CONFLICTING or
    ``mergeStateStatus`` DIRTY; an UNKNOWN ``mergeable`` stays unknown). ``files`` are the
    paths the PR changes. ``auto_merge``: auto-merge is already enabled; ``merged``: it landed."""
    number: int
    branch: str
    item_id: str
    head_sha: str = ''
    tree_sha: str = ''
    change_id: str = ''
    behind: bool = False
    conflicting: bool = False
    files: list = dataclasses.field(default_factory=list)
    checks: list = dataclasses.field(default_factory=list)
    auto_merge: bool = False
    merged: bool = False


@dataclasses.dataclass
class Branch:
    """One branch on origin under a kernel work prefix (``work_branch`` / ``fix_branch``):
    ``name``, the ``item_id`` it names, and its ``head_sha``. A pushed branch with no open PR is
    work a session finished and the kernel still has to open a pull request for."""
    name: str
    item_id: str
    head_sha: str = ''


@dataclasses.dataclass
class Session:
    """One worker session the host launched. ``job`` is the host's id for it; ``kind`` is the
    launch kind (``build``, ``review``, ``spec``, ``plan``). ``alive`` is whether its pid answers;
    ``ended`` is whether it exited on its own (a dead pid that never ended is a crash).
    ``result`` is one of :data:`RESULTS`; ``question`` is set when ``result == 'question'``;
    ``last_line`` is the last line it wrote; ``worktree`` is the checkout it holds. ``report`` is
    the whole result text of an ended session (a reviewer's verdict lines are read off it);
    ``pr``/``tree_sha``/``change_id`` are the PR, head tree and PR change a review session was
    launched on; ``branch`` the
    branch it was launched on.

    What an ended session's REPORT declares (:func:`asf.kernel.reports.read`): ``status`` is
    ``done``, ``partial``, ``blocked`` or '' (no REPORT); ``fields`` is the parsed REPORT
    (``pushed``, ``commits``, ``tests``, ``left out``, …); ``question`` a ``NEEDS OPERATOR:`` line
    that asks something; ``api_error`` the API failure that ended it before any REPORT, or ''.

    What its worktree holds that origin lacks (read once it ended, never for a review):
    ``unpushed`` is the worktree's HEAD sha when its branch has commits origin's branch lacks and
    origin's tip may be overwritten (an ancestor of HEAD or of an entry of the branch's reflog —
    its pre-rebase history — or every one of its commits patch-equivalent to one here), so the
    host pushes it with a lease; ``push_refused`` is why such a HEAD is not pushed (origin holds
    a commit the local history never had), or ''.
    ``cloud``: the session runs in the cloud lane — ``alive`` is the remote run's status and
    ``ended`` is set once that status is over (never a dead pid); its REPORT is the report commit
    it pushed (:func:`asf.workers.cloud.sync`)."""
    job: str
    item_id: str
    kind: str = 'build'
    pid: int = None
    alive: bool = True
    ended: bool = False
    result: str = 'none'
    question: str = None
    last_line: str = ''
    worktree: str = ''
    report: str = ''
    pr: int = None
    tree_sha: str = ''
    change_id: str = ''
    status: str = ''
    fields: dict = dataclasses.field(default_factory=dict)
    api_error: str = ''
    branch: str = ''
    unpushed: str = ''
    push_refused: str = ''
    cloud: bool = False


@dataclasses.dataclass
class Review:
    """One reviewer's verdict on an item's PR, keyed by the head's tree and by the PR's own
    change (``change_id``, '' on a verdict recorded before it was kept): it holds for a PR whose
    tree or change equals it (:func:`verdict_holds`). ``verdict`` is ``approve`` or ``changes``;
    ``findings`` (for ``changes``) go back to the item as it returns to Ready."""
    item_id: str
    tree_sha: str
    verdict: str
    findings: list = dataclasses.field(default_factory=list)
    change_id: str = ''


def verdict_holds(review, pr):
    """Whether ``review`` is a verdict on ``pr`` as it stands: the same head tree or the same own
    change (the caller matches the item). A branch update that merges trunk in moves the tree but
    keeps the change, so an approval survives it; a new commit on the PR moves both."""
    if review.tree_sha and review.tree_sha == pr.tree_sha:
        return True
    return bool(review.change_id) and review.change_id == pr.change_id


@dataclasses.dataclass
class Answer:
    """An operator's answer to the question an item is Stuck on, not yet written to its card.
    ``at`` is when it was given (ISO-8601 UTC, '' when the ledger row has none)."""
    item_id: str
    text: str
    at: str = ''


@dataclasses.dataclass
class Facts:
    """Everything one tick knows. ``items`` maps id -> :class:`Item` (every type: Epics, Features,
    Stories, Tasks, Bugs). ``specs_landed`` maps a Feature id to the text of its spec, for every
    spec merged to trunk. ``paused`` holds every launch; nothing else. ``branches`` are the
    :class:`Branch` values on origin under the kernel's work prefixes. ``stranded`` are the ended
    :class:`Session` values (``unpushed``/``push_refused`` read) whose kept worktree holds a rebase
    a Stuck item's refused force-push left (:func:`asf.kernel.decide.stranded`). ``now`` is the
    tick's time (ISO-8601 UTC, '' when unknown): a recorded Stuck's age is ``now`` less its
    ``Item.stuck_since``. ``id_claims`` maps each id a session's id-claim question cites
    (:mod:`asf.kernel.idclaims`) to the ``(ref, sha)`` of the claim on the record's origin that
    covers it, or ``''`` when none does; an id it could not read is absent."""
    items: dict = dataclasses.field(default_factory=dict)
    prs: list = dataclasses.field(default_factory=list)
    sessions: list = dataclasses.field(default_factory=list)
    reviews: list = dataclasses.field(default_factory=list)
    answers: list = dataclasses.field(default_factory=list)
    specs_landed: dict = dataclasses.field(default_factory=dict)
    paused: bool = False
    branches: list = dataclasses.field(default_factory=list)
    stranded: list = dataclasses.field(default_factory=list)
    now: str = ''
    id_claims: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Config:
    """The product's knobs ``decide`` reads; every branch shape or path is a field, never a literal
    in the kernel. ``doc_branches`` are the branch prefixes of the document lanes (spec, plan);
    ``doc_paths`` the path globs a document PR may change without needing a code review.
    ``max_sessions`` caps live sessions; ``max_attempts`` failed attempts on one reason make an
    item Stuck; ``max_fix_rounds`` red-driven rounds before Stuck; ``max_reruns`` reruns of a red
    that touches none of the PR's files before Stuck(owner=ci). ``required_checks`` are the check
    names that gate the landing (``conventions.landing_checks``, else the base branch's rules on
    GitHub): only a red among them is judged; empty means every check counts. ``work_branch`` / ``fix_branch`` are the branch
    prefixes of a build launch for a Task / a Bug with no open PR (a doc lane's prefix is the
    ``doc_branches`` entry named after its kind, e.g. ``spec/``). ``rank`` is ``inherit`` (a Task
    takes its nearest ancestor's rank) or ``own`` (only an item's own rank counts).
    ``idle_alarm``/``idle_min_free``: the plan carries an ``idle`` record when at least that many
    seats are free, work waits and nothing launches. ``update_parallel`` is the merge train's
    length: at most that many Landing PRs are brought up to date (or still run their checks after
    one) at once. ``escalate_after_h`` / ``rebuild_after_h``: a Stuck the kernel can resolve by
    itself is resolved once it is this many hours old (0: on the tick it appears; None: never —
    the bare model's default, so a unit test opts in); ``strong_model`` is the model the one
    extra fix round past the cap runs on. ``id_claim_answer``: a session question that only asks
    whether an id claim covers ids it cites (prefixes ``id_claim_prefixes``) is answered by the
    kernel from ``Facts.id_claims`` (:mod:`asf.kernel.idclaims`). The product file's ``kernel:`` block sets
    them (:mod:`asf.kernel.settings`; its ``stuck`` defaults are 0)."""
    doc_branches: tuple = ()
    doc_paths: tuple = ()
    work_branch: str = ''
    fix_branch: str = ''
    max_sessions: int = 6
    max_attempts: int = 2
    max_fix_rounds: int = 2
    max_reruns: int = 1
    required_checks: tuple = ()
    rank: str = 'inherit'
    idle_alarm: bool = True
    idle_min_free: int = 1
    update_parallel: int = 2
    escalate_after_h: float = None
    rebuild_after_h: float = None
    strong_model: str = ''
    id_claim_answer: bool = True
    id_claim_prefixes: tuple = ('S', 'T')
