"""asf.kernel.actions — what one tick does, as plain values (ASF 0.2).

:func:`asf.kernel.decide.decide` returns a :class:`Plan`; the applier walks ``Plan.actions`` in
order and does each one. An action is idempotent on the world it was decided from: applying a plan
twice does what applying it once did. No action merges a PR — landing is GitHub's (required checks
+ up to date + auto-merge); the kernel only enables auto-merge and updates a branch that is behind.
"""
import dataclasses


@dataclasses.dataclass
class Launch:
    """Start one session of ``kind`` (``build``, ``review``, ``spec``, ``plan``) for ``item_id`` on
    ``branch``. The session gets the branch and a brief only; the host mints ids and writes cards.
    ``findings`` are what a fix round answers beyond the review's (a rebase round's ask)."""
    kind: str
    item_id: str
    branch: str
    findings: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class EnableAutoMerge:
    """Turn on auto-merge for PR number ``pr`` (approved on its head tree, not yet enabled)."""
    pr: int


@dataclasses.dataclass
class UpdateBranch:
    """Bring PR number ``pr``'s branch up to date with trunk (it is behind and not conflicting)."""
    pr: int


@dataclasses.dataclass
class Rerun:
    """Rerun the failed jobs of check run ``run_id`` (a red touching none of the PR's files, not
    yet rerun)."""
    run_id: int


@dataclasses.dataclass
class MintStory:
    """Write a Story card ``story_id`` under ``feature_id`` with ``title`` and ``acceptance`` (a
    list of lines) — one per Story the landed spec declares that the record does not yet hold."""
    feature_id: str
    story_id: str
    title: str
    acceptance: list = dataclasses.field(default_factory=list)


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
class NoteItem:
    """Add ``text`` to ``item_id``'s notes: a question a session asked while its work moved on
    anyway (a ``done`` REPORT with a pushed head). Shown by status; holds nothing."""
    item_id: str
    text: str


@dataclasses.dataclass
class ApplyAnswer:
    """Write the operator's answer ``text`` to ``item_id``'s card and clear its question."""
    item_id: str
    text: str


@dataclasses.dataclass
class Plan:
    """The whole decision of one tick. ``states`` maps every item id the kernel judged (Tasks and
    Bugs, plus the derived state of every Feature and Story) to ``(State, Stuck or None)`` — the
    second is set exactly when the first is ``State.STUCK``. ``actions`` is the ordered list of
    the action values above. ``idle`` is the idle alarm (None when not raised): ``{'free': seats
    free, 'waiting': Tasks/Bugs not launched, 'reasons': [(reason, count)], the top three}``."""
    states: dict = dataclasses.field(default_factory=dict)
    actions: list = dataclasses.field(default_factory=list)
    idle: dict = None
