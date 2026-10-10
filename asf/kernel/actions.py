"""asf.kernel.actions — what one tick does, as plain values (ASF 0.2).

:func:`asf.kernel.decide.decide` returns a :class:`Plan`; the applier walks ``Plan.actions`` in
order and does each one. An action is idempotent on the world it was decided from: applying a plan
twice does what applying it once did. No action merges a PR — landing is GitHub's (required checks
+ up to date when the ruleset is strict + auto-merge); the kernel only enables auto-merge, updates
a branch that is behind (a strict ruleset only), opens a revert PR off a red trunk, and merges
directly (:class:`MergePR`) a green, CLEAN PR whose enabled auto-merge has not fired.
"""
import dataclasses


@dataclasses.dataclass
class Launch:
    """Start one session of ``kind`` (``build``, ``review``, ``spec``, ``plan``) for ``item_id`` on
    ``branch``. The session gets the branch and a brief only; the host mints ids and writes cards.
    ``findings`` are what a fix round answers beyond the review's (a rebase round's ask).
    ``model``: the model this one launch runs on instead of its kind's ('' keeps the kind's) —
    the one extra fix round past the cap runs on ``Config.strong_model``. ``local``: take a
    seat on this host first (a review relaunched past its wait target), the cloud lane only
    when the host has none. ``spec_head``: a plan launched on its Feature's approved, unmerged
    spec PR (``Config.plan_on_approve``) — the spec branch head it reads the spec off."""
    kind: str
    item_id: str
    branch: str
    findings: list = dataclasses.field(default_factory=list)
    model: str = ''
    local: bool = False
    spec_head: str = ''


@dataclasses.dataclass
class EnableAutoMerge:
    """Turn on auto-merge for PR number ``pr`` (approved on its head tree, not yet enabled)."""
    pr: int


@dataclasses.dataclass
class MergePR:
    """Merge PR number ``pr`` now, only while its head is still ``head_sha``
    (``--match-head-commit``): it is CLEAN, approved, green on every required check and had
    auto-merge enabled a tick ago, yet GitHub's auto-merge has not fired (seen 2026-10-10 once
    strict was turned off: 19 green PRs idle 30+ min)."""
    pr: int
    head_sha: str


@dataclasses.dataclass
class UpdateBranch:
    """Bring PR number ``pr``'s branch up to date with trunk (it is behind and not conflicting)."""
    pr: int


@dataclasses.dataclass
class OpenPR:
    """Open the pull request of ``item_id``'s pushed ``branch`` against ``base`` (``''``: the
    product's trunk) with ``title`` (``<ITEM-ID> — <card title>``) and ``body``. Idempotent: a PR
    that already exists for the branch is recorded, not duplicated."""
    item_id: str
    branch: str
    base: str
    title: str
    body: str


@dataclasses.dataclass
class Rerun:
    """Rerun the failed jobs of check run ``run_id`` (a red touching none of the PR's files, not
    yet rerun, or a required check cancelled on the head). ``cancel``: the run is stalled past
    its bound — cancel it now; the next tick reruns the cancelled check."""
    run_id: int
    cancel: bool = False


@dataclasses.dataclass
class MintStory:
    """Write a Story card ``story_id`` under ``feature_id`` with ``title`` and ``acceptance`` (a
    list of lines) — one per Story the landed spec declares that the record does not yet hold."""
    feature_id: str
    story_id: str
    title: str
    acceptance: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Decide:
    """Apply the intake decision the code made on the undecided card (or inbox note)
    ``item_id`` (:mod:`asf.kernel.intake`): ``decision`` (need, nice, later, close) and the
    rest of an :class:`asf.kernel.intake.Verdict`."""
    item_id: str
    decision: str
    kind: str = ''
    parent: str = ''
    severity: str = ''
    reason: str = ''
    by: str = 'code'
    set_priority: bool = False

    def verdict(self):
        from asf.kernel.intake import Verdict
        return Verdict(self.decision, self.kind, self.parent, self.severity, self.reason,
                       self.by, self.set_priority)


@dataclasses.dataclass
class MarkStuck:
    """Record ``item_id`` as Stuck with ``reason`` and ``owner`` (one of
    :data:`asf.kernel.model.OWNERS`)."""
    item_id: str
    reason: str
    owner: str


@dataclasses.dataclass
class EndSession:
    """Close session ``job`` on the host; ``free_worktree`` releases the checkout it held."""
    job: str
    free_worktree: bool = False


@dataclasses.dataclass
class PushStranded:
    """Publish the rebased HEAD that ended session ``job`` left in its kept worktree for
    ``item_id`` (Stuck on a force-push its sandbox refused: ``Facts.stranded``): the host's
    ``--force-with-lease`` push, after the same ancestry check. A refused push leaves the item
    Stuck(owner=operator) on the refusal."""
    job: str
    item_id: str


@dataclasses.dataclass
class NoteItem:
    """Add ``text`` to ``item_id``'s notes: a question a session asked while its work moved on
    anyway (a ``done`` REPORT with a pushed head), or a red on a check that is not required.
    Shown by status; holds nothing."""
    item_id: str
    text: str


@dataclasses.dataclass
class ApplyAnswer:
    """Write the operator's answer ``text`` to ``item_id``'s card and clear its question. On a
    Stuck item the answer is a fresh start too: its attempts become the one relaunch marker
    :func:`asf.kernel.decide.answer_attempt` (the relaunch carries the answer as a finding).
    ``extra_round``: the item was Stuck at its fix-round cap (or on a conflict its rebase session
    could not resolve), so the answer grants one more fix round (``kernel_extra_rounds`` + 1) and
    drops the spent rebase finding from the card. ``by``: the resolver class whose fact
    answered (:mod:`asf.kernel.resolvers`: ``id-claim``, ``trunk-tests``, ``symbol``,
    ``needs-writes``); '' for the operator's own answer. ``writes``: the paths a ``needs-writes``
    grant adds to the card's ``writes:`` — written through the record's writer first; a refused
    widening leaves the answer unwritten (the question stays with the operator)."""
    item_id: str
    text: str
    extra_round: bool = False
    by: str = ''
    writes: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class FileInbox:
    """File one untyped card titled ``title`` with ``body`` into the record's intake dir (the
    groom types it and assigns its id): the Bug a session asked for and could not mint itself
    (:mod:`asf.kernel.resolvers` ``inbox-bug``). A card of that title already in the intake dir
    is not filed again; the next tick answers the question with its path."""
    item_id: str
    title: str
    body: str


@dataclasses.dataclass
class ClearStuck:
    """A Stuck the kernel resolves by itself (a legacy one the newer rules re-judge, or one it
    escalates: :func:`asf.kernel.decide.escalate_session`, the extra fix round past the cap):
    append ``attempt`` to ``item_id``'s attempts (the one relaunch it is given — a
    :data:`asf.kernel.decide.NO_REPORT`, a :data:`asf.kernel.decide.RELAUNCH` marker carrying a
    finding, or the :data:`asf.kernel.decide.STRONG_ROUND` marker); its Stuck is cleared with the
    item's state."""
    item_id: str
    attempt: str


@dataclasses.dataclass
class WaitOn:
    """A session stopped blocked on work not yet merged (:func:`asf.kernel.decide.blocked_on`):
    add ``ids`` to ``item_id``'s ``after:`` line (those not on it yet) and clear its Stuck — the
    item is New and waits on them, and launches again once each is Done or retired."""
    item_id: str
    ids: list


@dataclasses.dataclass
class ArchiveAndReset:
    """Build ``item_id`` afresh: its PR ``pr`` cannot land (a conflict its rebase session could not
    resolve, or the fix-round cap after the strong round). The host pushes ``head_sha`` to
    ``archive/<branch>``, closes the PR with a comment naming ``reason`` and the archive, and
    deletes ``branch``; the record clears ``kernel_fix_rounds``, ``kernel_extra_rounds``,
    ``kernel_attempts`` and ``kernel_findings``, adds one to ``kernel_rebuilds`` and the item is
    Ready on a fresh branch from the trunk. At most once per item."""
    item_id: str
    pr: int
    branch: str
    head_sha: str
    reason: str


@dataclasses.dataclass
class ClosePR:
    """Close the open kernel PR number ``pr`` (on ``branch``, naming ``item_id``) with a comment
    naming ``reason``: its item is not on the record, Done or retired, so nothing would ever move
    it. The branch is kept."""
    pr: int
    branch: str
    item_id: str
    reason: str


@dataclasses.dataclass
class RevertPR:
    """Main is red and ``item_id``'s merged PR ``pr`` (squash commit ``sha``) is the one change
    since the last green commit: the host pushes ``git revert <sha>`` on top of the trunk to
    ``branch`` (``revert/<item>``), opens its PR with ``title`` and ``body`` and enables
    auto-merge on it; the item goes back to Ready with ``note`` (and ``finding`` for its
    relaunch). Idempotent: an open PR on ``branch`` is reused."""
    item_id: str
    pr: int
    sha: str
    branch: str
    title: str
    body: str
    note: str = ''
    finding: str = ''


@dataclasses.dataclass
class FileBug:
    """Main is red with several candidate PRs (or none the kernel can revert): write a Bug card
    titled ``title`` with ``body`` (the candidates, the red checks and the ``key`` marker line
    that keeps it filed once) at ``rank`` — the next tick launches its fix session."""
    key: str
    title: str
    body: str
    rank: int = 0


@dataclasses.dataclass
class Satisfied:
    """The still-needed gate found ``item_id`` already done on the trunk: ``tests`` (new since it
    was planned) pass on a fresh worktree of trunk ``sha``. It is Done with the note
    ``satisfied on main at <sha>: <tests>`` and nothing is launched (:mod:`asf.kernel.needed`)."""
    item_id: str
    sha: str
    tests: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CancelRun:
    """Cancel workflow run ``run_id``: ``why`` it can no longer matter (its PR is closed, or it
    runs on a head that is no longer its PR's head). Never rerun."""
    run_id: int
    why: str = ''


@dataclasses.dataclass
class Plan:
    """The whole decision of one tick. ``states`` maps every item id the kernel judged (Tasks and
    Bugs, plus the derived state of every Feature and Story) to ``(State, Stuck or None)`` — the
    second is set exactly when the first is ``State.STUCK``. ``actions`` is the ordered list of
    the action values above. ``idle`` is the idle alarm (None when not raised): ``{'free': seats
    free, 'waiting': Tasks/Bugs not launched, 'reasons': [(reason, count)], the top three}``.
    ``notes`` maps an item id to this tick's notes that are never written to its card (a behind
    PR the merge train holds back: :data:`asf.kernel.decide.TRAIN_NOTE`). ``limbo`` maps each
    non-terminal item (or ``PR #n``) with no action this tick and nothing in flight to why
    (:func:`asf.kernel.decide.limbo`; the target is none). ``breaches`` are the waits over their
    class's target: ``{'item', 'class', 'age_s', 'action'}`` each, ``action`` the one line of
    what this tick does about it (:func:`asf.kernel.decide.breaches`). ``main`` is the main
    safety net's verdict when the trunk is red (``{'sha', 'action'}``, logged ``MAIN RED <sha>
    -> <action>``; None when green or unread: :mod:`asf.kernel.mainline`). ``wip`` is the WIP
    cap's hold this tick (``{'open', 'cap', 'held'}``: open PRs in Review + Landing, the cap, the
    new builds/plans/specs that wait; None when it holds nothing back). ``gate`` are the
    still-needed gate's lines (:func:`asf.kernel.needed.gate_line`): ``{'item', 'verdict',
    'why'}`` each. ``dor`` maps each Task or Bug the
    Definition of Ready holds to its ``dor: <missing>`` reason (None while it is off)."""
    states: dict = dataclasses.field(default_factory=dict)
    actions: list = dataclasses.field(default_factory=list)
    idle: dict = None
    notes: dict = dataclasses.field(default_factory=dict)
    limbo: dict = dataclasses.field(default_factory=dict)
    breaches: list = dataclasses.field(default_factory=list)
    main: dict = None
    wip: dict = None
    gate: list = dataclasses.field(default_factory=list)
    dor: dict = None


def describe(action):
    """One line naming ``action``."""
    if isinstance(action, Launch):
        return 'launch %s %s on %s%s%s' % (
            action.kind, action.item_id, action.branch,
            ' (local first)' if action.local else '',
            ' (spec approved at %s, not merged)' % action.spec_head[:12]
            if action.spec_head else '')
    if isinstance(action, EnableAutoMerge):
        return 'auto-merge #%d' % action.pr
    if isinstance(action, MergePR):
        return 'MERGE direct #%d (auto-merge idle)' % action.pr
    if isinstance(action, UpdateBranch):
        return 'update-branch #%d' % action.pr
    if isinstance(action, OpenPR):
        return 'open PR %s for %s: %s' % (action.branch, action.item_id, action.title)
    if isinstance(action, Rerun):
        return ('cancel stalled run %s (rerun next tick)' if action.cancel
                else 'rerun run %s') % action.run_id
    if isinstance(action, Decide):
        from asf.kernel.intake import line
        return line(action.item_id, action.verdict())
    if isinstance(action, MintStory):
        return 'mint %s under %s: %s' % (action.story_id, action.feature_id, action.title)
    if isinstance(action, MarkStuck):
        return 'stuck %s (%s): %s' % (action.item_id, action.owner, action.reason)
    if isinstance(action, EndSession):
        return 'end session %s%s' % (action.job, ' + free worktree' if action.free_worktree else '')
    if isinstance(action, ApplyAnswer):
        if action.by:
            return 'RESOLVED %s %s -> %s' % (action.item_id, action.by,
                                             ' '.join(action.text.split())[:200])
        return 'answer %s' % action.item_id
    if isinstance(action, FileInbox):
        return 'file inbox card for %s: %s' % (action.item_id, action.title)
    if isinstance(action, ClearStuck):
        return 're-judge %s: %s' % (action.item_id, action.attempt)
    if isinstance(action, WaitOn):
        return 'wait %s on %s' % (action.item_id, ', '.join(action.ids))
    if isinstance(action, PushStranded):
        return 'publish %s rebase of %s' % (action.item_id, action.job)
    if isinstance(action, NoteItem):
        return 'note %s: %s' % (action.item_id, action.text)
    if isinstance(action, ClosePR):
        return 'close #%d (%s): %s' % (action.pr, action.branch, action.reason)
    if isinstance(action, ArchiveAndReset):
        return 'rebuild %s: archive %s, close #%d (%s)' % (action.item_id, action.branch,
                                                           action.pr, action.reason)
    if isinstance(action, RevertPR):
        return 'revert #%d of %s (%s) on %s' % (action.pr, action.item_id, action.sha[:9],
                                                action.branch)
    if isinstance(action, FileBug):
        return 'file bug: %s' % action.title
    if isinstance(action, Satisfied):
        return 'SATISFIED %s on main at %s: %s' % (action.item_id, action.sha[:9],
                                                   ', '.join(action.tests))
    if isinstance(action, CancelRun):
        return 'cancel run %s: %s' % (action.run_id, action.why)
    return repr(action)
