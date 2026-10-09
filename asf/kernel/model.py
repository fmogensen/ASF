"""asf.kernel.model — the facts the kernel decides on, as plain values (ASF 0.2).

One loop: read facts (the record, GitHub, the sessions) -> :func:`asf.kernel.decide.decide` ->
apply the plan's actions. Everything here is what the reader hands ``decide``: no method reaches
a disk, a network or a clock, so a test builds a world by hand and asks what the kernel would do.

Every Task or Bug has exactly one :class:`State`. A Feature's or a Story's state is never stored:
it is derived from the Tasks whose ``parent`` link points at it, and only that link counts.
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
    ``writes`` are the path globs the item declares it will change.

    ``state``/``stuck`` are what the card records now. ``attempts`` is the reason of each failed
    attempt, oldest first (a launch error, a push-less session end); ``fix_rounds`` counts the
    red-driven rounds already spent; ``findings`` are the open review findings carried into the
    next launch; ``answers`` are operator answers already written to the card; ``question`` is
    the open question a session asked. ``reopened`` is set when a Done item was reopened.
    """
    id: str
    type: str = 'task'
    title: str = ''
    parent: str = None
    rank: int = None
    priority: str = None
    after: list = dataclasses.field(default_factory=list)
    writes: list = dataclasses.field(default_factory=list)
    body: str = ''
    state: State = State.NEW
    stuck: Stuck = None
    attempts: list = dataclasses.field(default_factory=list)
    fix_rounds: int = 0
    findings: list = dataclasses.field(default_factory=list)
    answers: list = dataclasses.field(default_factory=list)
    question: str = None
    reopened: bool = False


@dataclasses.dataclass
class Check:
    """One check run on a PR head. ``status`` is GitHub's (``queued``, ``in_progress``,
    ``completed``); ``conclusion`` is set only when completed. Only a conclusion in
    :data:`RED_CONCLUSIONS` is red. ``failing_files`` are the files the failing tests live in or
    exercise (empty when unknown); ``attempt`` is the run's attempt number (1 = never rerun)."""
    name: str
    status: str = 'completed'
    conclusion: str = None
    run_id: int = None
    failing_files: list = dataclasses.field(default_factory=list)
    attempt: int = 1


@dataclasses.dataclass
class PR:
    """One pull request against trunk. ``tree_sha`` is the head commit's tree: a rebase that
    changes nothing keeps it, so a verdict keyed by it survives. ``behind``: the base moved past
    the PR's base; ``conflicting``: GitHub cannot merge it as is. ``files`` are the paths the PR
    changes. ``auto_merge``: auto-merge is already enabled; ``merged``: it landed."""
    number: int
    branch: str
    item_id: str
    head_sha: str = ''
    tree_sha: str = ''
    behind: bool = False
    conflicting: bool = False
    files: list = dataclasses.field(default_factory=list)
    checks: list = dataclasses.field(default_factory=list)
    auto_merge: bool = False
    merged: bool = False


@dataclasses.dataclass
class Session:
    """One worker session the host launched. ``job`` is the host's id for it; ``kind`` is the
    launch kind (``build``, ``review``, ``spec``, ``plan``). ``alive`` is whether its pid answers;
    ``ended`` is whether it exited on its own (a dead pid that never ended is a crash).
    ``result`` is one of :data:`RESULTS`; ``question`` is set when ``result == 'question'``;
    ``last_line`` is the last line it wrote; ``worktree`` is the checkout it holds. ``report`` is
    the whole result text of an ended session (a reviewer's verdict lines are read off it);
    ``pr``/``tree_sha`` are the PR and head tree a review session was launched on; ``branch`` the
    branch it was launched on.

    What an ended session's REPORT declares (:func:`asf.kernel.reports.read`): ``status`` is
    ``done``, ``partial``, ``blocked`` or '' (no REPORT); ``fields`` is the parsed REPORT
    (``pushed``, ``commits``, ``tests``, ``left out``, …); ``question`` a ``NEEDS OPERATOR:`` line
    that asks something; ``api_error`` the API failure that ended it before any REPORT, or ''."""
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
    status: str = ''
    fields: dict = dataclasses.field(default_factory=dict)
    api_error: str = ''
    branch: str = ''


@dataclasses.dataclass
class Review:
    """One reviewer's verdict on an item's PR, keyed by the head's tree. ``verdict`` is
    ``approve`` or ``changes``; ``findings`` (for ``changes``) go back to the item as it returns
    to Ready."""
    item_id: str
    tree_sha: str
    verdict: str
    findings: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Answer:
    """An operator's answer to the question an item is Stuck on, not yet written to its card."""
    item_id: str
    text: str


@dataclasses.dataclass
class Facts:
    """Everything one tick knows. ``items`` maps id -> :class:`Item` (every type: Epics, Features,
    Stories, Tasks, Bugs). ``specs_landed`` maps a Feature id to the text of its spec, for every
    spec merged to trunk. ``paused`` holds every launch; nothing else."""
    items: dict = dataclasses.field(default_factory=dict)
    prs: list = dataclasses.field(default_factory=list)
    sessions: list = dataclasses.field(default_factory=list)
    reviews: list = dataclasses.field(default_factory=list)
    answers: list = dataclasses.field(default_factory=list)
    specs_landed: dict = dataclasses.field(default_factory=dict)
    paused: bool = False


@dataclasses.dataclass
class Config:
    """The product's knobs ``decide`` reads; every branch shape or path is a field, never a literal
    in the kernel. ``doc_branches`` are the branch prefixes of the document lanes (spec, plan);
    ``doc_paths`` the path globs a document PR may change without needing a code review.
    ``max_sessions`` caps live sessions; ``max_attempts`` failed attempts on one reason make an
    item Stuck; ``max_fix_rounds`` red-driven rounds before Stuck; ``max_reruns`` reruns of a red
    that touches none of the PR's files before Stuck(owner=ci). ``work_branch`` / ``fix_branch`` are the branch
    prefixes of a build launch for a Task / a Bug with no open PR (a doc lane's prefix is the
    ``doc_branches`` entry named after its kind, e.g. ``spec/``)."""
    doc_branches: tuple = ()
    doc_paths: tuple = ()
    work_branch: str = ''
    fix_branch: str = ''
    max_sessions: int = 8
    max_attempts: int = 2
    max_fix_rounds: int = 2
    max_reruns: int = 1
