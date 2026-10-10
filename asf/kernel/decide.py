"""asf.kernel.decide — the one pure decision of the kernel (ASF 0.2).

``decide(facts, config) -> Plan`` reads nothing but its arguments and returns the state of every
item and the actions of one tick. The rules it holds, in the design's words:

- New and Ready: an item is Ready once nothing else holds it and it is ranked — its own ``rank``,
  else its nearest ancestor's through ``parent`` (Story, Feature, Epic). A Task or Bug whose whole
  lineage is unranked is Ready too, after every ranked item: no rank never means never. Only an
  unranked Feature or Epic (and a declared ``after:`` edge, or ``later``) keeps an item New.
- Launch: finish first (:data:`FINISH_FIRST`) — fix rounds and rebases on open PRs, reviews,
  builds, plans, specs — each class in launch order — (effective rank, own rank, id), unranked
  last — while ``config.max_sessions`` allows; while the items in Review plus Landing exceed
  ``config.max_open_prs`` (the WIP cap) no build, plan or spec launches (``Plan.wip``);
  ``facts.paused`` means no :class:`Launch` at all. An item whose own errors repeat is Stuck; the lane never is.
- Waits: only a declared ``after:`` edge, and "two Building items with overlapping ``writes``:
  one at a time". An edge holds until its dependency is Done (its PR merged) or retired
  (:func:`edge_open`): Building, Review, Landing and Stuck never satisfy it, nor does a card in
  LIMBO (unreadable) or an id not on the record; only a parked dependency holds no one. A New
  item's idle reason names the first one it waits on (``waits on <id>``).
- Blocked on unmerged work (:func:`blocked_on`): a session that stops ``partial``/``blocked`` or
  asks a question naming an item or a ``PR #n`` not yet merged (or whose item's own ``after:``
  edge is still open) is no question to the operator: :class:`WaitOn` adds those ids to
  ``after:`` and clears the Stuck, and the item is New until they are Done.
- Parked: an item whose own ``priority``, or an ancestor's (through ``parent``), is ``later`` is
  invisible everywhere: it is :attr:`State.PARKED` (Done stays Done), gets no launch, no PR upkeep
  (update, rerun, auto-merge), no answer, no mint for its landed spec, is never Stuck, and never
  counts in a ``blocked_count`` or holds anyone through ``after:`` or ``writes``.
- Review: one reviewer per PR head; the verdict is keyed by the head's tree and by the PR's own
  change (``change_id``), and holds when either matches: a rebase with the same tree keeps it, and
  so does an update that merges trunk in (the change is the same). ``changes`` sends the item to
  Ready with the findings. Every PR without a verdict on its head gets a review, a document branch
  (``config.doc_branches``) included.
- Landing: an approved PR gets :class:`EnableAutoMerge`; a behind one :class:`UpdateBranch` —
  a merge train: at most ``config.update_parallel`` Landing PRs are brought up to date at once.
  The behind, non-conflicting ones are taken in :func:`train_key` order: an auto-merge PR
  waiting ``config.landing_max_wait_h`` or more first (longest wait first), then the most items
  it unblocks through ``after:``, then effective rank, then PR number; the free places are
  ``update_parallel`` less the non-conflicting Landing PRs whose CI still runs on their head
  (:func:`in_flight`: a required check pending, or not yet reported while other checks run) —
  behind or not: such a PR is never updated again until its CI ends. The rest wait in Landing with the plan note
  :data:`TRAIN_NOTE` ("queued for update (merge train, k of n)"), never written to the card.
  Behind is GitHub's ``mergeStateStatus == BEHIND`` only: a PR a non-strict base would merge as
  is never needs an update. The train runs only while the trunk's ruleset is strict
  (``Facts.strict``: "require branches to be up to date"); with it off GitHub merges a green PR
  behind its base, so a merely BEHIND PR is never updated (that only burns CI), nothing queues,
  and a conflicting one still goes to its rebase session. The main safety net
  (:mod:`asf.kernel.mainline`) then watches the trunk's own required checks: a red new since the
  last green commit reverts its one candidate PR (:class:`RevertPR`, the item back to Ready) or
  files a Bug for a fix session naming the candidates (:class:`FileBug`); an infra or flaky red
  is rerun once first. ``Plan.main`` says what (``MAIN RED <sha> -> <action>``). A merged PR
  the kernel reverted (``Item.reverted``) never makes its item Done.
  Every open PR's item is in Review or Landing (or Ready on a fix round, or Stuck) — never stateless.
- Red: only ``failure``/``timed_out``, and only on a required check (``config.required_checks``;
  empty: every check counts). A red on any other check holds nothing: it is an item note
  (:class:`NoteItem`, shown by status). A required red whose ``failing_files`` are known and all
  outside the PR's files is off the PR: ``config.max_reruns`` reruns, then Stuck(owner=ci). Any
  other required red — meeting the PR's files, or with no known files (a lint or ratchet step,
  an unread log) — is on the PR: Ready (a fix round carrying the failed step and its log tail,
  :func:`red_finding`), at most ``config.max_fix_rounds``, then Stuck. A recorded Stuck from an
  off-PR red (:data:`RED_OFF`) is judged afresh every tick, so a red that is not required, or
  whose files were unknown, goes back to Landing, Review or a fix round.
- Conflict: a conflicting PR (GitHub's ``mergeable == CONFLICTING`` or ``mergeStateStatus ==
  DIRTY``, or an update of its head that failed on a merge conflict) never gets
  :class:`UpdateBranch` and never takes a merge-train place: it goes straight to one fix round —
  a ``correct`` session whose finding (:data:`REBASE`) is to rebase onto the base and resolve
  the conflicts. A PR that still conflicts after that session ended is Stuck(owner=operator).
- Stuck: ``config.max_attempts`` failed attempts on one reason; a conflict the rebase session
  could not resolve; a session question (owner=operator); a session whose REPORT says
  ``partial`` or ``blocked`` (owner=session, or operator when it asks a ``NEEDS OPERATOR:``
  question), the reason in the report's own words (:func:`asf.kernel.reports.stuck_reason`); a
  ``done`` session with no push and no open PR; a session that ended without a REPORT a second
  time (owner=session, "ended without a REPORT" plus its last line that says something) — the
  first is an attempt :data:`NO_REPORT`: its worktree and branch are kept and it is relaunched,
  before any other Ready item, the ones that hold the most others first. A ``done`` session
  whose worktree holds commits origin lacks (``Session.unpushed``) is pushed by the host with a
  lease and counts as pushed; one whose origin branch holds commits its history never had
  (``Session.push_refused``) is Stuck(owner=operator) on that reason. A session on an open PR's
  branch (a rebase or fix round) is published the same way at any end — ``partial``,
  ``blocked`` or no REPORT too: its sandbox refuses a force-push, so the host's push replaces
  it, the item is not Stuck for that session, and the PR is judged on the next tick's facts. A
  recorded Stuck a refused force-push left (:func:`stranded`) whose rebase worktree still holds
  a safe rebased HEAD (``Facts.stranded``) is re-judged: :class:`PushStranded`, then Review. A ``done`` session
  that pushed a new head moves on (to Review) whatever ``NEEDS OPERATOR:`` line it also carries:
  the question becomes an item note (:class:`NoteItem`, shown by status) and holds nothing. A session whose API
  failed is an attempt :data:`API_FAILED` — relaunched, then Stuck(owner=loop). A dead pid ends
  the session and frees its worktree. No Stuck reason is ever empty, a code fence or noise
  (:func:`asf.kernel.reports.meaningful`); one recorded that way is judged afresh.
- Documents: only a Feature with no Task or Bug under it launches ``spec`` (``plan`` once its
  spec has landed — on trunk, or its spec PR merged): Stories minted from its spec leave its
  document lane its own (:func:`_derived`). A merged spec PR never closes a Feature
  (:func:`_spec_only`). A Story or Epic with no children is New and never launches. A Story's
  children are the Tasks under it through ``parent`` and those naming it on ``stories:``.
- Rank: ``config.rank`` ``inherit`` (the default) walks ``parent`` for a rank; ``own`` reads
  only the item's own.
- Idle: when ``config.idle_alarm`` is on, nothing launches, at least ``config.idle_min_free``
  seats are free and visible Tasks/Bugs wait (New or Ready), the plan carries ``idle``: the free
  seats, how many wait, and the top three reasons with counts (:data:`IDLE_REASONS`).
- Answers: an operator answer the facts let through (newer than the item's Stuck, whatever its
  owner, or to an open question) clears that Stuck once: :class:`ApplyAnswer`, and the item goes
  to whatever its facts imply (Ready, Building under a live session, Review on an open PR). Its
  attempts become one :data:`RELAUNCH` marker carrying the answer (:func:`answer_attempt`): the
  next tick relaunches it first, the one holding the most others first, the answer a finding.
  An answer to a Stuck at the fix-round cap (or a conflict the rebase session could not resolve,
  :func:`capped`) grants exactly one more fix round (``Item.extra_rounds`` + 1; the cap is
  ``max_fix_rounds`` + ``extra_rounds``): the fix round carries the findings and the answer.
- Legacy Stuck: a recorded Stuck(owner=session) the newer rules handle is re-judged once
  (:func:`legacy_relaunch`, :class:`ClearStuck`): "ended without a REPORT" with no
  :data:`NO_REPORT` attempt yet gets its one relaunch; "done without a push: pushed: no — hook
  refused" is relaunched once with :data:`HOOK_FINDING`.
- Stuck never sits (``config.escalate_after_h`` / ``config.rebuild_after_h``, hours; the product
  defaults are 0: on the tick the Stuck appears; None turns a rule off). A Stuck the kernel can
  resolve by its reason class is resolved once it is that old:

  - a required red off the PR (owner ci) gets one more rerun than ``max_reruns``, then a fix round
    carrying the red (:func:`red_finding`);
  - a session that stopped (owner session: ``partial``, ``blocked``, done without a push, no
    REPORT twice) is relaunched once with its report's words as the finding
    (:func:`escalate_session`, a :data:`RELAUNCH` marker);
  - the fix-round cap gets one extra round (:data:`STRONG_ROUND`) on ``config.strong_model``
    carrying every finding the item holds; when that round fails too, the item is rebuilt;
  - a conflict the rebase session could not resolve is rebuilt (``rebuild_after_h``):
    :class:`ArchiveAndReset` archives the branch to ``archive/<branch>``, closes the PR and returns
    the item to Ready on a fresh branch — at most :data:`MAX_REBUILDS` per item (``Item.rebuilds``).

  A recorded Stuck at the cap or on such a conflict is judged afresh every tick, so its age is
  read off ``Item.stuck_since`` against ``Facts.now``. A question to the operator is never
  resolved by the kernel: it waits for an answer — save one that only asks whether an id claim
  covers ids it cites (``config.id_claim_answer``, :mod:`asf.kernel.idclaims`): the kernel
  answers it from ``Facts.id_claims`` (:class:`ApplyAnswer`; "the claim stands, keep" with the
  refs it verified, or "re-mint from your current block") and the item is relaunched carrying the
  answer, on the tick the session ends or on a recorded operator Stuck. A claim it could not read
  leaves the question with the operator. The same holds for the trunk-probe classes of
  :mod:`asf.kernel.resolvers` (``config.resolve_trunk_tests``, ``config.resolve_symbols``):
  whether named tests are red on trunk or only in a cloud sandbox, whether a symbol a question
  says does not exist exists — answered from ``Facts.resolved``; an unprobed or unsure one stays
  with the operator. Every such answer is an :class:`ApplyAnswer` with ``by`` its class. A gate
  script a session could not run (``config.resolve_gates``) is answered from its run on the
  branch head; a Bug card it could not mint (``config.resolve_inbox_bugs``) is filed first
  (:class:`FileInbox`, the item stays on the operator) and answered once the record holds it.
  A REPORT's ``needs writes:`` paths (``config.resolve_needs_writes``) are decided by policy,
  question or not: held behind an unfinished ``after:`` item that writes one, else granted
  (:class:`ApplyAnswer` with ``writes``: the card's ``writes:`` widened first).
- Record: a landed spec's declared Stories (:func:`asf.kernel.stories.declared_stories`) not on
  the record are minted; pending answers are applied at once.

- Nothing waits without an action (the operator's rule: waiting for hours without acting is a
  bug). Every non-terminal item — Ready, Building, Review, Landing, Stuck — and every open kernel
  PR has an action this tick or a session / CI run in flight that will produce one; one with
  neither is in LIMBO (:func:`limbo`, ``Plan.limbo``: the id and why; the target is none). A wait
  over its class's target (``config.wait_targets`` against ``Facts.waits``, the wait ledger's
  current spell) is a breach (:func:`breaches`, ``Plan.breaches``) and takes the breach action
  the kernel has: a Ready item launches first while a seat is free; a Review gets its reviewer
  first, on a local seat first; a behind Landing PR — or a green one GitHub has not merged past the ``merge``
  target (its PR listing can call a behind PR clean) — goes to the front of the merge train (a
  conflicting or red one is already a fix round); a Stuck takes its escalation. A live session
  older than ``config.max_session_age_h`` is ended (:class:`EndSession`, worktree kept, attempt
  :data:`OVER_AGE`) so its item is relaunched. Live processes are bounded by measured p90s
  (``Facts.bounds``, from the wait ledger; the ``config.max_*_age_h`` knobs only when too few
  spells are measured): a session past its class's p90 with no push (twice it with one) is
  ended and relaunched; a PR's required CI past twice the ``ci`` p90 is cancelled
  (:class:`Rerun` ``cancel``) and its cancelled check rerun next tick. A Stuck on the operator
  waits on a console question and is not LIMBO.
- Still needed (:mod:`asf.kernel.needed`, ``config.needed_satisfied``): before every launch of a
  Ready Task or Bug the tests its card names, new since it was planned, are run on the trunk —
  all green, it is Done (:class:`Satisfied`) and nothing launches; a done REPORT that pushed
  nothing and names a landed sha and passing tests is Done the same way; a kernel PR that
  changes nothing is closed (Done, or Ready with a finding). A queued or running CI run whose
  PRs are closed, or whose head is no longer its PR's head, is cancelled
  (``config.cancel_stale_ci``, :class:`CancelRun`).
- A clean floor (``config.close_floor``): an open PR on a kernel branch prefix whose item is not
  on the record (``Facts.orphan_prs``), or is Done or retired (not reopened, not a Feature whose
  spec only landed) and held by no live session, is closed with a comment (:class:`ClosePR`);
  the item stays Done.

How the record keeps up (the applier's side of the contract): ``Item.fix_rounds`` counts the fix
rounds already launched; a failed ``UpdateBranch`` whose error says "merge conflict" (or on a
PR already conflicting) is recorded as an attempt :func:`conflict_attempt` (its reason starts
with :data:`CONFLICT` and names the PR and head); a fix round's findings (a :data:`REBASE` one
included) are written to ``Item.findings`` when it launches. An item whose session ended, or whose answer is applied,
this tick is not relaunched until the next tick has read the record again.
"""
import dataclasses
import datetime
import fnmatch
import re

from asf.kernel import actions as A
from asf.kernel import dor as D
from asf.kernel import idclaims
from asf.kernel import resolvers
from asf.kernel import mainline
from asf.kernel import needed
from asf.kernel import model as M
from asf.kernel import reports as R
from asf.kernel import waits as W
from asf.kernel.model import OWNERS, RED_CONCLUSIONS, State, Stuck, verdict_holds
from asf.kernel.stories import declared_stories

#: the item types whose state is derived from their children (when they have any)
CONTAINERS = ('epic', 'feature', 'story')

#: the item types a build session works on
BUILDABLE = ('task', 'bug')

#: the session kinds that only read and judge: they never push, open a PR or hold a build
READ_ONLY = ('review', D.GROOM_FILL)

#: the one item type a document lane (spec, then plan once its spec landed) launches for: a
#: childless Story or Epic is New, never a spec
DOCUMENTED = 'feature'

#: the reason prefix of a failed attempt to update a conflicting PR's branch
CONFLICT = 'conflict'

#: the prefix of the finding a rebase round carries (on the Launch, then on ``Item.findings``)
REBASE = 'rebase'

#: what a rebase round's session does, after the finding's prefix
REBASE_ASK = ("rebase the branch onto the base, resolve the conflicts, keep the change's intent, "
              "run the touched tests, commit it locally (do not force-push yourself) and report "
              "status: done, pushed: rebased <sha> — the host publishes it")

#: what the operator does with a conflict the rebase session could not resolve
CONFLICT_NEXT = 'rebase PR #%d onto its base by hand and push, or close it and reopen the item'

#: a dead pid's attempt reason (the session never ended on its own)
CRASH = 'session died'

#: the attempt reason of a session whose API failed before it could report (retried, not a
#: verdict on the work: ``config.max_attempts`` of them make the item Stuck(owner=loop))
API_FAILED = 'the session API failed'

#: the attempt reason of a session that ended without a REPORT: its first is relaunched (worktree
#: and branch kept), its :data:`NO_REPORT_LIMIT`-th makes the item Stuck(owner=session)
NO_REPORT = 'ended without a REPORT'
NO_REPORT_LIMIT = 2

#: the attempt a review session that ended without a ``VERDICT:`` line records: an
#: infrastructure failure, not a judgement — at least :data:`NO_VERDICT_LIMIT` of them (else
#: ``config.max_attempts``) before the item is Stuck(owner=loop), so one is always relaunched
NO_VERDICT = 'review: no VERDICT line'
NO_VERDICT_LIMIT = 2

#: the prefix of the Stuck reason of a done session whose unpushed work the host will not push
NOT_PUSHED = 'not pushed: '

#: one line per owner of what happens next, unless the card already says
NEXT_ACTION = {
    'loop': 'the loop retries once its facts change',
    'session': 'a session reads the reason and fixes the cause',
    'ci': 'fix or rerun the red check outside this PR',
    'operator': 'answer on the card',
}

#: why a waiting Task or Bug did not launch (the idle alarm's reasons)
IDLE_REASONS = {
    'after': 'waits on after:',
    'overlap': 'file overlap',
    'held': 'held this tick',
    'paused': 'launches paused',
    'new': 'not ready (new)',
    'seat': 'no free seat',
    'wip': 'WIP cap: finishing first',
    'dor': 'dor: not ready (groom-fill)',
}

#: the order a plan's actions are applied in: record first, then GitHub, then launches
ORDER = (A.FileInbox, A.ApplyAnswer, A.ClearStuck, A.WaitOn, A.EndSession, A.PushStranded,
         A.NoteItem, A.Satisfied, A.MarkStuck, A.MintStory, A.ArchiveAndReset, A.ClosePR,
         A.CancelRun, A.RevertPR, A.FileBug, A.OpenPR, A.Rerun, A.UpdateBranch,
         A.EnableAutoMerge, A.MergePR, A.Launch)

#: the attempt a live session ended past ``Config.max_session_age_h`` leaves on its item
OVER_AGE = 'session ran past its max age'

#: the states an item must have an action or something in flight for (else it is in LIMBO)
ACTIVE = (State.READY, State.BUILDING, State.REVIEW, State.LANDING, State.STUCK)

#: the actions that move an item (a note or a recorded Stuck moves nothing)
PROGRESS = (A.Launch, A.ClearStuck, A.WaitOn, A.ApplyAnswer, A.ArchiveAndReset, A.OpenPR,
            A.PushStranded, A.ClosePR, A.EndSession, A.UpdateBranch, A.EnableAutoMerge, A.Rerun,
            A.RevertPR, A.MergePR)

#: the most characters of a Stuck reason a LIMBO line carries
LIMBO_REASON_MAX = 160

#: the prefix of a Stuck reason a ``done`` REPORT left (its ``NEEDS OPERATOR:`` question, before
#: a done-and-pushed session moved on): with its branch pushed it is re-judged to an OpenPR
DONE_STUCK = R.DONE + ':'

#: the prefix of the Stuck reason a ``done`` REPORT whose push the kernel did not see left: when
#: its ``pushed:`` value claims a push and the branch is on origin it is re-judged to an OpenPR
NO_PUSH_STUCK = 'done without a push: pushed: '

#: the prefix of the Stuck reason of a required red off the PR that used up its reruns: judged
#: afresh every tick (the check may not be required, or its files may not be known)
RED_OFF = 'red off the PR after '

#: the prefix of an attempt that is a relaunch the kernel grants (an operator answer, a legacy
#: Stuck re-judged): the item is relaunched before other Ready items, the rest of the attempt
#: is the finding its brief carries (:func:`relaunch_finding`)
RELAUNCH = 'relaunch: '

#: the finding an operator answer's relaunch carries, before the answer's text
ANSWERED = 'operator answer: '

#: the most characters of an answer a relaunch marker carries (the brief quotes it whole)
ANSWER_MAX = 600

#: a legacy ``done without a push`` Stuck whose push hook refused (the pre-push check timed out
#: under load before it was shortened): relaunched once with :data:`HOOK_FINDING`
HOOK_REFUSED_STUCK = NO_PUSH_STUCK + 'no'
HOOK_REFUSED = 'hook refused'
HOOK_FINDING = ('the pre-push check timed out at load; it is now shortened (no touched-tests '
                'step), push again')

#: the prefix of a red-driven fix round's finding (:func:`red_finding`)
RED = 'red'

#: the most characters of a failed log's tail a finding carries
LOG_TAIL_MAX = 1500

#: the prefix of the finding a session's escalated relaunch carries (after :data:`RELAUNCH` on the
#: attempt that marks it): the Stuck reason in the report's own words follows
ESCALATED = 'escalated: '

#: what an escalated relaunch's finding says, around the Stuck reason
ESCALATED_ASK = ('the previous session stopped (%s) — pick up from its report and branch, and '
                 'finish the item')

#: the attempt that marks the one extra fix round past the cap, on ``Config.strong_model``
STRONG_ROUND = ESCALATED + 'one more fix round on the strong model, with every finding'

#: the times one item is archived and built afresh (:class:`asf.kernel.actions.ArchiveAndReset`)
MAX_REBUILDS = 1

#: the plan note of a behind Landing PR the merge train holds back this tick (its place in the
#: queue, of how many are behind)
TRAIN_NOTE = 'queued for update (merge train, %d of %d)'

#: the card states whose pushed branch with no PR gets one (a build session held it, or its PR
#: was being opened): a stale branch of a New or Ready item is never turned into a PR
PR_STATES = (State.BUILDING, State.REVIEW, State.LANDING)


@dataclasses.dataclass
class _Judged:
    """One item's state after pass one. ``review_branch`` asks for a reviewer on that branch;
    ``branch`` is where a coder would work; ``hold`` keeps it from launching this tick."""
    state: State
    stuck: Stuck = None
    review_branch: str = None
    branch: str = None
    hold: bool = False
    findings: list = dataclasses.field(default_factory=list)
    behind_pr: object = None
    updating: bool = False
    model: str = ''
    start: bool = False
    dor: str = ''


def decide(facts, config):
    """Return the :class:`asf.kernel.actions.Plan` for ``facts`` (:class:`asf.kernel.model.Facts`)
    under ``config`` (:class:`asf.kernel.model.Config`). Pure: same arguments, same plan."""
    items = facts.items
    actions = []
    for s in facts.sessions:
        if not s.alive:
            actions.append(A.EndSession(s.job, free_worktree=True))
    over_age = _over_age(facts, config)
    actions += [A.EndSession(s.job, free_worktree=False) for s, _age in over_age]
    facts, found_done, done = needed.take_done(facts, config)  # the still-needed gate
    actions += found_done
    closes = floor_closes(facts, config)
    closing = {a.item_id for a in closes if a.item_id in items}

    parked_all = _parked(items)
    parked = parked_all - flying(facts, config)
    judged = {iid: (_Judged(State.DONE) if iid in closing
                    else _park(items[iid], facts, config) if iid in parked
                    else _judge(items[iid], facts, config, actions, parked))
              for iid in sorted(items)}
    actions += closes
    gate_actions, gate, gate_notes = needed.judge(facts, config, judged, _Judged, done)
    shut = {a.pr for a in gate_actions if isinstance(a, A.ClosePR)}
    actions = [a for a in actions if not (isinstance(a, (A.EnableAutoMerge, A.UpdateBranch,
                                                         A.MergePR)) and a.pr in shut)]
    actions += gate_actions
    actions += needed.stale_runs(facts, config, {a.pr for a in actions
                                                 if isinstance(a, A.ClosePR)})
    unready = hold_unready(items, judged, facts, config, parked_all)
    risky = hold_risky(items, judged, facts, config, actions)
    for iid, j in judged.items():
        old = items[iid]
        if j.state is State.STUCK and (old.state is not State.STUCK or old.stuck != j.stuck):
            actions.append(A.MarkStuck(iid, j.stuck.reason, j.stuck.owner))

    children = _children(items)
    states = {}
    for iid in sorted(items):
        _state_of(iid, items, judged, children, states, (), parked)
    _apply_waits(items, judged, children, states, parked, facts.unreadable or {})
    blocks = _count_blocked(items, states, parked)
    due = overdue(facts, config, states)
    stalled_ci = ci_stalls(facts, config, states)
    have = {a.run_id for a in actions if isinstance(a, A.Rerun)}
    for iid, (_age, runs) in stalled_ci.items():
        actions += [A.Rerun(r, cancel=True) for r in runs if r not in have]
        have.update(runs)

    for iid, (cls, _age) in due.items():  # green, auto-merge on, yet not merged past its target:
        j = judged.get(iid)                # GitHub's listing may call a PR clean that is behind
        if (cls == 'merge' and facts.strict and j is not None and j.state is State.LANDING
                and iid not in risky
                and j.behind_pr is None and not j.updating):
            j.behind_pr = max((p for p in facts.prs if p.item_id == iid and not p.merged),
                              key=lambda p: p.number, default=None)
    train, notes = _merge_train(items, judged, config, blocks, facts.now,
                                first={i for i, (c, _a) in due.items() if c in ('train', 'merge')})
    for iid, why in list(unready.items()) + list(risky.items()):
        notes.setdefault(iid, []).append(why)
    actions += train
    for iid, texts in gate_notes.items():
        notes.setdefault(iid, []).extend(texts)
    main_actions, main = mainline.judge(facts, config)
    for a in main_actions:
        if isinstance(a, A.RevertPR):  # back to Ready now; relaunched once the revert is on
            judged[a.item_id] = _Judged(State.READY, hold=True)  # the card (next tick)
            states[a.item_id] = (State.READY, None)
        if not (isinstance(a, A.Rerun) and a.run_id in have):
            actions.append(a)
    actions += _mint(facts, parked_all)
    queued, wip = dict.fromkeys(risky, 'risk'), {}
    launches, skipped = (([], {}) if facts.paused
                         else _launches(facts, config, judged, children, states, parked, blocks,
                                        due, queued, wip, unready))
    actions += launches
    actions.sort(key=lambda a: ORDER.index(type(a)))
    idle = _idle(facts, config, judged, states, parked, skipped) if not launches else None
    found = breaches(due, over_age, actions, facts, config, states, judged, queued, stalled_ci)
    stalled = {b['item']: b for b in found if b['action'].startswith(NO_BREACH_ACTION)}
    return A.Plan(states=states, actions=actions, idle=idle, notes=notes,
                  limbo=limbo(facts, config, judged, states, parked, children, actions, queued,
                              stalled),
                  breaches=found, main=main, wip=wip or None, gate=gate,
                  dor=unready if config.dor else None)


def blind_plan(facts):
    """The plan of a tick whose PRs GitHub would not give (``Facts.github_error``): only what no
    PR fact decides — a session that died without ending is ended and its crash counted. Nothing
    launches and no state is judged; an ended session keeps its report for the next readable
    tick, whose facts judge it whole."""
    return A.Plan(actions=[A.EndSession(s.job, free_worktree=True) for s in facts.sessions
                           if not s.alive and not s.ended])


def _merge_train(items, judged, config, blocks=None, now='', first=()):
    """``(UpdateBranch actions, {item: [note]})`` of the merge train: the behind Landing PRs in
    :func:`train_key` order — ``first`` (the items whose train wait is over its target) ahead of
    every other — as many as ``config.update_parallel`` less the updates in flight allows; the
    rest get :data:`TRAIN_NOTE`."""
    blocks = blocks or {}
    behind = sorted((iid for iid, j in judged.items() if j.behind_pr is not None),
                    key=lambda iid: (iid not in first,) + train_key(
                        iid, judged[iid].behind_pr, items, config, blocks, now))
    running = sum(1 for j in judged.values() if j.updating)
    free = max(0, config.update_parallel - running)
    actions = [A.UpdateBranch(judged[iid].behind_pr.number) for iid in behind[:free]]
    notes = {iid: [TRAIN_NOTE % (k, len(behind))]
             for k, iid in enumerate(behind, 1) if k > free}
    return actions, notes


def train_key(iid, pr, items, config, blocks, now):
    """The merge train's order of behind PR ``pr`` (item ``iid``): first every auto-merge PR
    waiting at least ``config.landing_max_wait_h`` hours since auto-merge was enabled
    (``PR.auto_merge_at``), the longest wait first — none starves; then the most items it
    unblocks (``blocks``: transitively through ``after:``), then effective rank, then the oldest
    PR."""
    waited = stuck_age_h(pr.auto_merge_at, now) if pr.auto_merge and pr.auto_merge_at else None
    starved = (config.landing_max_wait_h is not None and waited is not None
               and waited >= config.landing_max_wait_h)
    return (0 if starved else 1, -waited if starved else 0, -blocks.get(iid, 0),
            launch_order(iid, items, config.rank != 'own')[0], pr.number)


def in_flight(pr, config):
    """Whether ``pr``'s head still runs the CI that gates it: a required check not completed, or
    a required check not yet reported while another check on the head still runs (a required
    aggregate job is created only once the jobs it needs finish). An update now would only
    restart that CI, and a peer landing meanwhile would make it stale."""
    running = [c for c in pr.checks if c.status != 'completed']
    if any(required(c.name, config) for c in running):
        return True
    names = {c.name for c in pr.checks}
    return bool(running) and any(n not in names for n in config.required_checks)


# ---- pass one: each item on its own facts -------------------------------------------------------

def _judge(it, facts, config, actions, parked=()):
    """``it``'s :class:`_Judged` from its own card, sessions, PRs, reviews and answers; appends the
    item's own actions (answers, PR upkeep) to ``actions``. ``parked`` are the parked ids (a
    report naming one is no wait)."""
    stuck = (it.stuck if it.state is State.STUCK and not _legacy_conflict(it.stuck)
             and not _red_off(it.stuck) and not _rejudged(it.stuck, config)
             and not str(it.stuck.reason).startswith(D.PREFIX)
             and R.meaningful(it.stuck.reason) else None)
    question = it.question
    hold = False
    attempts = list(it.attempts)
    answered = granted = False
    extra = it.extra_rounds
    answers = list(facts.answers)
    nw = (_needs_writes(it, resolvers.needs_writes_of(it.stuck.reason), facts, config, parked)
          if it.state is State.STUCK and it.stuck is not None and it.stuck.owner == 'operator'
          else None)
    auto, auto_by = (nw[0], resolvers.NEEDS_WRITES) if nw else (
        _auto_answer(it, facts, config) or (None, ''))
    if not auto and it.state is State.STUCK and it.stuck is not None \
            and it.stuck.owner == 'operator':
        _file_inbox(it, facts, config, it.question or it.stuck.reason, actions)
    if auto and not any(a.item_id == it.id for a in answers):
        answers.append(M.Answer(it.id, auto))  # the kernel answers it, as the operator would
    for a in answers:
        if a.item_id == it.id and not answered and (
                a.text not in it.answers or _answers_stuck(a, it)):
            granted = it.state is State.STUCK and capped(it.stuck)
            extra += 1 if granted else 0
            actions.append(A.ApplyAnswer(it.id, a.text, granted,
                                         by=auto_by if a.text == auto else '',
                                         writes=list(nw[1]) if nw and a.text == auto else []))
            stuck, question, hold, answered = None, None, True, True
            if it.state is State.STUCK:  # a fresh start: the applier resets the attempts too
                attempts = [answer_attempt(a.text)]
    if stuck is not None:
        again = legacy_relaunch(it)
        if again:
            actions.append(A.ClearStuck(it.id, again))
            stuck, hold = None, True
    if stuck is not None and stranded(stuck):
        s = next((s for s in facts.stranded if s.item_id == it.id and s.unpushed
                  and not s.push_refused), None)
        if s is not None:  # the host publishes the rebase it left; next tick judges the PR
            actions.append(A.PushStranded(s.job, it.id))
            return _Judged(State.REVIEW, hold=True)

    sessions = [s for s in facts.sessions if s.item_id == it.id]
    live = [s for s in sessions if s.alive]
    prs = [p for p in facts.prs if p.item_id == it.id]
    open_pr = max((p for p in prs if not p.merged), key=lambda p: p.number, default=None)
    ended_stuck, api_detail, pushed, asked = None, '', None, None
    for s in sessions:
        if s.alive:
            continue
        hold = True
        if not s.ended:
            attempts.append(CRASH)
        elif s.kind in READ_ONLY:
            continue  # a reviewer's (a groom-fill's) verdict is read off its report by the applier
        elif host_refuses(s):
            ended_stuck = _stuck(R.cap(NOT_PUSHED + s.push_refused), 'operator')
        elif host_pushes(s) and s.status != R.DONE:
            pushed = s  # the host publishes its rebase; the PR is judged on next tick's facts
            _note(it, pushed_note(s), actions)
        elif s.api_error and not s.fields:
            attempts.append(API_FAILED)
            api_detail = s.api_error
        elif s.kind != 'review' and no_report(s):
            attempts.append(NO_REPORT)
            if attempts.count(NO_REPORT) >= NO_REPORT_LIMIT:
                ended_stuck = _stuck(R.no_report_reason(s.last_line), 'session')
        elif done_and_pushed(s, facts):
            pushed = s
            _note(it, pushed_note(s), actions)
        else:
            ended = _ended_stuck(s, open_pr)
            if ended is not None:
                ended_stuck, asked = ended, s

    spec_only = _spec_only(it, prs, config)
    if open_pr is None and landed(it, prs) and not it.reopened and not spec_only:
        return _Judged(State.DONE)
    if ended_stuck is not None and asked is not None and not answered and not live:
        nw = _needs_writes(it, (asked.fields or {}).get('needs writes'), facts, config, parked)
        if nw:  # a widening the policy decides: held behind its after: owner, or granted
            actions.append(A.ApplyAnswer(it.id, nw[0], by=resolvers.NEEDS_WRITES,
                                         writes=list(nw[1])))
            if it.state is not State.STUCK:  # the applier resets only a Stuck card's attempts
                actions.append(A.ClearStuck(it.id, answer_attempt(nw[0])))
            return _Judged(State.READY, hold=True)
    if open_pr is None and not live and not question:
        branch = _pr_branch(it, sessions, pushed, stuck, ended_stuck, facts, config)
        if branch:
            actions.append(open_pr_action(it, branch, pushed))
            return _Judged(State.REVIEW, hold=True)
    if not answered and not live:
        waits = (blocked_on(it, ended_stuck, _asked_text(asked), facts, parked) if ended_stuck
                 else blocked_on(it, stuck, question or '', facts, parked) if stuck
                 else blocked_on(it, _stuck(question, 'operator'), question, facts, parked)
                 if question else [])
        if waits:  # blocked on unmerged work: it waits on it, no operator involved
            actions.append(A.WaitOn(it.id, waits))
            return _Judged(State.NEW, hold=True)
    if stuck:
        again = (escalate_session(it, stuck) if stuck.owner == 'session' and not live
                 and _due(config.escalate_after_h, it, facts) else None)
        if again:  # a recorded session Stuck old enough: its one relaunch, next tick
            actions.append(A.ClearStuck(it.id, again))
            return _Judged(State.READY, hold=True)
        return _Judged(State.STUCK, _keep(stuck))
    if ended_stuck and ended_stuck.owner == 'operator' and asked is not None and not answered:
        auto, auto_by = _auto_answer(it, facts, config, asked.question) or (None, '')
        if not auto:
            _file_inbox(it, facts, config, asked.question, actions)
        if auto:  # a question the facts answer: relaunched next tick carrying it
            actions.append(A.ApplyAnswer(it.id, auto, by=auto_by))
            if it.state is not State.STUCK:  # the applier resets only a Stuck card's attempts
                actions.append(A.ClearStuck(it.id, answer_attempt(auto)))
            return _Judged(State.READY, hold=True)
    if ended_stuck:
        again = (escalate_session(it, ended_stuck) if ended_stuck.owner == 'session'
                 and _due(config.escalate_after_h, it, facts, fresh=True) else None)
        if again:  # resolved on the tick it appears: relaunched next tick with the report
            actions.append(A.ClearStuck(it.id, again))
            return _Judged(State.READY, hold=True)
        return _Judged(State.STUCK, ended_stuck)
    if question:
        return _Judged(State.STUCK, _stuck(question, 'operator'))
    if live and all(s.kind == D.GROOM_FILL for s in live):
        return _Judged(State.NEW, hold=True, dor=D.PREFIX + 'groom-fill %s in flight' % live[0].job)
    if live:
        builds = [s for s in live if s.kind not in READ_ONLY]
        return _Judged(State.BUILDING if builds else State.REVIEW, hold=True)
    repeated = _repeated(attempts, config.max_attempts)
    if repeated:
        if repeated == API_FAILED and api_detail:
            repeated = '%s: %s' % (API_FAILED, api_detail)
        return _Judged(State.STUCK, _stuck(repeated, 'loop'))
    if open_pr is not None:
        j = _judge_pr(it, open_pr, attempts, facts, config, actions, extra, granted,
                      fresh=pushed is not None)
        j.hold = j.hold or hold
        return j
    if it.state is State.DONE and not it.reopened and not spec_only and not it.reverted:
        return _Judged(State.DONE)
    if it.priority == 'later' or (effective_rank(it.id, facts.items, config.rank != 'own') is None
                                  and it.type not in BUILDABLE):
        return _Judged(State.NEW, hold=hold)
    if it.type not in BUILDABLE and it.type != DOCUMENTED:
        return _Judged(State.NEW, hold=hold)  # only a Feature gets a spec or plan launch
    return _Judged(State.READY, hold=hold, findings=relaunch_findings(it), start=True)


def hold_unready(items, judged, facts, config, parked):
    """``{item: dor reason}``: every Task or Bug about to start (Ready, no open PR) that the
    Definition of Ready (``config.dor``, :func:`asf.kernel.dor.missing`) holds — New with the
    reason ``dor: <missing>``; once ``config.dor_max_fills`` groom-fill sessions could not make it
    ready, Stuck on the operator with that reason (judged afresh every tick: a card mended by hand
    starts). ``judged`` is updated in place."""
    if not config.dor:
        return {}
    out = {}
    for iid in sorted(judged):
        j, it = judged[iid], items[iid]
        if not j.start or it.type not in BUILDABLE or iid in parked:
            continue
        gaps = D.missing(it, items, facts.trunk_files, parked)
        if not gaps:
            continue
        why = D.reason(gaps)
        if it.dor_fills >= config.dor_max_fills:
            judged[iid] = _Judged(State.STUCK, _stuck(
                '%s — %d groom-fill session(s) could not make it ready' % (why, it.dor_fills),
                'operator'))
        else:
            judged[iid] = _Judged(State.NEW, hold=j.hold, dor=why)
        out[iid] = why
    return out


#: a Stuck reason a session's REPORT left (:func:`asf.kernel.reports.stuck_reason`)
REPORT_STUCK_RE = re.compile(r'^(?:partial|blocked|question|done|report): ')

#: an item id a report names (any type letter, four or more digits)
ITEM_ID_RE = re.compile(r'\b[A-Z]-\d{4,}\b')

#: a pull request a report names
PR_REF_RE = re.compile(r'\b(?:PR|pull)\s*(?:#|/)\s*(\d+)\b', re.I)


def _asked_text(s):
    """What an ended session's REPORT says it waits for: its question and its fields."""
    if s is None:
        return ''
    fields = s.fields or {}
    return ' '.join(str(v) for v in [s.question] + [fields.get(k) for k in
                                                    ('blocked_on', 'left out', 'needs writes')]
                    if v)


def _unmerged(x, facts, parked):
    """Whether item ``x``'s change is not on the trunk yet: its card is unreadable, or it is on the
    record, not parked (a retired card is), with no landed PR and not recorded Done."""
    if x in (facts.unreadable or {}):
        return True
    dep = facts.items.get(x)
    if dep is None or x in parked:
        return False
    if landed(dep, [p for p in facts.prs if p.item_id == x]):
        return False
    return not (dep.state is State.DONE and not dep.reopened and not dep.reverted)


def _waits_on(x, iid, items, seen=()):
    """Whether ``x`` already waits on ``iid`` through ``after:`` (an edge back would be a cycle)."""
    if x == iid:
        return True
    if x in seen or x not in items:
        return False
    return any(_waits_on(a, iid, items, seen + (x,)) for a in items[x].after)


def blocked_on(it, stuck, text, facts, parked=()):
    """The ids ``it`` waits on when ``stuck`` is a session's stop (a ``partial``/``blocked``
    REPORT, or a question): every Task or Bug — or unreadable card — the reason or ``text`` names,
    directly or through a ``PR #n`` not merged, whose change is not on the trunk, plus its own
    ``after:`` edges still open; ``[]`` for any other Stuck, or when nothing named is unmerged.
    Never ``it`` itself, its own lineage, a parked item, or one that waits on ``it``."""
    if stuck is None or not (REPORT_STUCK_RE.match(str(stuck.reason or '')) or text):
        return []
    items = facts.items
    words = '%s %s' % (stuck.reason or '', text or '')
    named = ITEM_ID_RE.findall(words)
    by_pr = {p.number: p.item_id for p in facts.prs if not p.merged}
    named += [by_pr[int(n)] for n in PR_REF_RE.findall(words) if int(n) in by_pr]
    named = [x for x in named if x in (facts.unreadable or {})
             or (x in items and items[x].type in BUILDABLE)]
    out = []
    for x in list(it.after) + named:
        if (x in out or x == it.id or _waits_on(x, it.id, items)
                or not (_unmerged(x, facts, parked) or (x in it.after and x not in items))):
            continue
        out.append(x)
    return out


#: the reason prefix of a high-risk Landing PR held back (:func:`hold_risky`)
RISK = 'risk: '


def _open_pr_of(iid, facts):
    return max((p for p in facts.prs if p.item_id == iid and not p.merged),
               key=lambda p: p.number, default=None)


def _hits(paths, globs):
    return bool(globs) and any(fnmatch.fnmatchcase(f, g) for f in paths for g in globs)


def high_risk(it, pr, config):
    """Whether ``it`` (with its open ``pr``, or None) is high-risk under the product's coarse
    flag: its ``writes`` (or the PR's files) hit a ``config.risk_high`` glob, or the PR's diff is
    over ``config.risk_large_lines`` lines."""
    globs = tuple(config.risk_high or ())
    if globs and (_overlap(list(it.writes), globs) or _hits(pr.files if pr else (), globs)):
        return True
    return bool(pr is not None and config.risk_large_lines and pr.lines > config.risk_large_lines)


def _merge_high(c, items, config):
    """Whether trunk commit ``c`` merged a high-risk change: its PR's files or diff, or its
    item's ``writes``."""
    if c.pr is None:
        return False
    it = items.get(c.item_id)
    globs = tuple(config.risk_high or ())
    return bool((it is not None and globs and _overlap(list(it.writes), globs))
                or _hits(c.files, globs)
                or (config.risk_large_lines and c.lines > config.risk_large_lines))


def _main_green(c, config):
    """Whether every required check on trunk commit ``c`` completed green (every check, with
    none named)."""
    req = [k for k in c.checks if required(k.name, config)]
    names = {k.name for k in req}
    return bool(req) and all(n in names for n in config.required_checks) and all(
        k.status == 'completed' and k.conclusion in ('success', 'neutral', 'skipped')
        for k in req)


def hold_risky(items, judged, facts, config, actions):
    """``{item: why}``: the high-risk Landing PRs (:func:`high_risk`) held back this tick — no
    auto-merge, no direct merge, no merge-train update — while (a) the newest high merge on the
    trunk (``Facts.main``) has not gone green on its required checks, or (b) another high PR with
    overlapping ``writes`` (or files) holds the landing lane: the one with auto-merge already on,
    else the lowest PR number. ``judged`` and ``actions`` are updated in place."""
    if not config.risk_high and not config.risk_large_lines:
        return {}
    high = {}
    for iid in sorted(judged):
        if judged[iid].state is State.LANDING:
            pr = _open_pr_of(iid, facts)
            if pr is not None and high_risk(items[iid], pr, config):
                high[iid] = pr
    if not high:
        return {}
    out = {}
    last = next((c for c in facts.main or () if _merge_high(c, items, config)), None)
    if last is not None and not _main_green(last, config):
        why = RISK + 'after high merge %s (#%s) the trunk is not green yet' % (last.sha[:9],
                                                                               last.pr)
        out = {iid: why for iid in high if not high[iid].merged}
    else:
        lane = []
        for iid in sorted(high, key=lambda i: (not high[i].auto_merge, high[i].number)):
            paths = list(items[iid].writes) + list(high[iid].files)
            holder = next((h for h in lane if _overlap(paths, list(items[h].writes)
                                                       + list(high[h].files))), None)
            if holder is None:
                lane.append(iid)
            else:
                out[iid] = RISK + 'high, overlapping writes: waits for #%d (%s) to land' % (
                    high[holder].number, holder)
    held = {high[iid].number for iid in out}
    actions[:] = [a for a in actions if not (isinstance(a, (A.EnableAutoMerge, A.MergePR))
                                             and a.pr in held)]
    for iid in out:
        judged[iid].behind_pr = None
    return out


def landed(it, prs):
    """Whether one of ``it``'s ``prs`` merged and the kernel did not revert it off a red main
    (``Item.reverted``)."""
    return any(p.merged and p.number not in it.reverted for p in prs)


def _auto_answer(it, facts, config, text=None):
    """``(answer, resolver class)`` of a question the facts answer, or None: one that only asks
    whether an id claim covers the ids it cites (:func:`asf.kernel.idclaims.answer`), else one a
    trunk probe answered (:func:`asf.kernel.resolvers.resolve`: named tests red on trunk or only
    in a cloud sandbox, a symbol said not to exist). ``text`` is an ended session's question;
    without it, ``it``'s recorded operator Stuck (its question, else its reason) is read, and an
    answer already on the card is never given again."""
    if text is None:
        if it.state is not State.STUCK or it.stuck is None or it.stuck.owner != 'operator':
            return None
        text = it.question or it.stuck.reason
    if config.id_claim_answer:
        auto = idclaims.answer(it, text, facts, tuple(config.id_claim_prefixes))
        if auto:
            return (auto, resolvers.ID_CLAIM) if auto not in it.answers else None
    branch = resolvers.branch_of(it.id, facts.sessions, facts.prs)
    got = resolvers.resolve(text, facts, _resolving(config), branch)
    return got if got and got[0] not in it.answers else None


def _resolving(config):
    """The :mod:`asf.kernel.resolvers` classes ``config`` turns on."""
    return tuple(c for c, on in ((resolvers.TRUNK_TESTS, config.resolve_trunk_tests),
                                 (resolvers.SYMBOL, config.resolve_symbols),
                                 (resolvers.GATE, config.resolve_gates),
                                 (resolvers.INBOX_BUG, config.resolve_inbox_bugs)) if on)


def _needs_writes(it, value, facts, config, parked=()):
    """``(answer, granted paths)`` for the ``needs writes:`` ``value`` a session's REPORT gave
    (:func:`asf.kernel.resolvers.needs_writes`: held behind an unfinished ``after:`` item that
    writes a path, else granted), or None — the knob off, no path outside ``writes:``, or the
    answer already on the card."""
    if not config.resolve_needs_writes:
        return None
    paths = resolvers.requested_writes(value, it.writes, lambda p, g: _overlap([p], g))
    got = resolvers.needs_writes(it, paths, facts.items,
                                 lambda x: _unmerged(x, facts, parked), _overlap,
                                 tuple(config.risk_high or ()))
    return got if got and got[0] not in it.answers else None


def _file_inbox(it, facts, config, text, actions):
    """Append the :class:`FileInbox` a question asks for when the record does not hold its card
    yet (:func:`asf.kernel.resolvers.to_file`); the next tick answers it with the card's path."""
    card = resolvers.to_file(text, facts, it.id, _resolving(config))
    if card:
        actions.append(A.FileInbox(it.id, *card))


def _answers_stuck(answer, it):
    """Whether ``answer`` (already filtered by the facts) clears ``it``'s Stuck afresh though its
    text is on the card: both times are known, so the facts let only a newer answer through."""
    return it.state is State.STUCK and bool(answer.at) and bool(it.stuck_since)


def _rejudged(stuck, config):
    """Whether a recorded Stuck is judged afresh every tick so the kernel can resolve it: the
    fix-round cap or a conflict the rebase session could not resolve (:func:`capped`), while
    either escalation rule is on."""
    return capped(stuck) and (config.escalate_after_h is not None
                              or config.rebuild_after_h is not None)


def stuck_age_h(since, now):
    """Hours from ``since`` to ``now`` (both ISO-8601 UTC), or None when either is unreadable."""
    a, b = _utc(since), _utc(now)
    return None if a is None or b is None else (b - a).total_seconds() / 3600


def _utc(value):
    try:
        t = datetime.datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def _due(threshold, it, facts, fresh=False):
    """Whether a Stuck of ``it`` is old enough for the kernel to resolve under ``threshold``
    hours: None never; 0 (or less) at once; else a Stuck recorded on the card (not ``fresh``: this
    tick's) whose age (:func:`stuck_age_h`) reaches it — an unknown age counts as old enough."""
    if threshold is None:
        return False
    if threshold <= 0:
        return True
    if fresh or it.state is not State.STUCK:
        return False
    age = stuck_age_h(it.stuck_since, getattr(facts, 'now', ''))
    return age is None or age >= threshold


def escalate_session(it, stuck):
    """The attempt a session-owned Stuck is resolved with — one relaunch marker carrying the
    reason in the report's own words — or None when ``it`` already had its escalated relaunch."""
    if any(str(a).startswith(RELAUNCH + ESCALATED) for a in it.attempts):
        return None
    return RELAUNCH + ESCALATED + ESCALATED_ASK % R.cap(stuck.reason, ANSWER_MAX)


def _rebuild(it, pr, reason, config, facts, actions):
    """Ready (held this tick) with an :class:`ArchiveAndReset` of ``pr`` when ``it`` may still be
    rebuilt (``Item.rebuilds`` under :data:`MAX_REBUILDS`) and its Stuck is old enough
    (``config.rebuild_after_h``; a Stuck recorded on another reason is this tick's), else None."""
    if it.rebuilds >= MAX_REBUILDS or not _due(config.rebuild_after_h, it, facts,
                                               fresh=not capped(it.stuck)):
        return None
    actions.append(A.ArchiveAndReset(it.id, pr.number, pr.branch, pr.head_sha, reason))
    return _Judged(State.READY, hold=True)


def answer_attempt(text):
    """The attempts an operator answer to a Stuck item leaves: one relaunch marker carrying it."""
    return RELAUNCH + ANSWERED + R.cap(text, ANSWER_MAX)


def legacy_relaunch(it):
    """The attempt a legacy Stuck the newer rules handle is re-judged with (its one relaunch), or
    None: a session ``ended without a REPORT`` once with no :data:`NO_REPORT` on the record (a
    relaunch not spent yet), or a ``done without a push`` whose push hook refused, not yet
    relaunched with :data:`HOOK_FINDING`."""
    st = it.stuck
    if it.state is not State.STUCK or st is None or st.owner != 'session':
        return None
    reason = str(st.reason)
    if reason.startswith(NO_REPORT) and NO_REPORT not in it.attempts:
        return NO_REPORT
    marker = RELAUNCH + HOOK_FINDING
    if reason.startswith(HOOK_REFUSED_STUCK) and HOOK_REFUSED in reason \
            and marker not in it.attempts:
        return marker
    return None


def relaunch_findings(it):
    """The finding a granted relaunch carries into its brief (its last attempt is a
    :data:`RELAUNCH` marker), else none."""
    last = it.attempts[-1] if it.attempts else ''
    return [last[len(RELAUNCH):]] if str(last).startswith(RELAUNCH) else []


def effective_rank(iid, items, inherit=True):
    """``iid``'s own ``rank``, else (``inherit``) its nearest ancestor's through ``parent`` (Story,
    Feature, Epic), else None. A ``parent`` cycle ends the walk."""
    if not inherit:
        return items[iid].rank if iid in items else None
    seen, cur = set(), iid
    while cur in items and cur not in seen:
        if items[cur].rank is not None:
            return items[cur].rank
        seen.add(cur)
        cur = items[cur].parent
    return None


def launch_order(iid, items, inherit=True):
    """The key launches sort by: (effective rank, own rank, id), unranked (+inf) last."""
    inf = float('inf')
    eff, own = effective_rank(iid, items, inherit), items[iid].rank
    return (inf if eff is None else eff, inf if own is None else own, iid)


def done_and_pushed(s, facts=None):
    """Whether ended session ``s`` reported ``done`` and pushed a new head: its work moves on
    whatever question it also asked (B-0098). The push is the host's push log
    (``result == 'pushed'``), or the REPORT's ``pushed:`` line claiming one (``yes <sha>``,
    a bare sha, ``rebased <sha>``) with the session's branch on origin (``facts.branches``)."""
    if s.status != R.DONE:
        return False
    if s.result == 'pushed' or host_pushes(s):
        return True
    return facts is not None and _claimed_on_origin((s.fields or {}).get('pushed'), s.item_id,
                                                    s.branch, facts)


def _host_publishable(s):
    """Whether ended session ``s``'s worktree is one the host publishes from: a non-review
    session that pushed nothing itself, holding commits origin lacks (``Session.unpushed``), and
    either reported ``done`` or ran on an open PR's branch (``Session.pr``: a rebase or fix round,
    whatever it reported — ``partial``, ``blocked`` or no REPORT: its sandbox refused the
    force-push it needed)."""
    return (s.ended and s.kind not in READ_ONLY and s.result != 'pushed' and bool(s.unpushed)
            and (s.status == R.DONE or s.pr is not None))


def host_pushes(s):
    """Whether the host pushes ended session ``s``'s worktree HEAD (``Session.unpushed``) with a
    lease before it is ended (:func:`_host_publishable`, and origin's branch holds nothing its
    history never had)."""
    return _host_publishable(s) and not s.push_refused


def host_refuses(s):
    """Whether ended session ``s`` left work the host will not push: origin's branch holds a
    commit its history never had (``Session.push_refused``). Stuck(owner=operator) on that."""
    return _host_publishable(s) and bool(s.push_refused)


def stranded(stuck):
    """Whether a recorded Stuck is one a rebase the host could publish may have left — a conflict
    the rebase session could not resolve, or a session asking the operator for the
    ``--force-with-lease`` push its sandbox refused: re-judged when its worktree still holds a
    safe rebased HEAD (``Facts.stranded``)."""
    reason = str(stuck.reason) if stuck is not None else ''
    return (' the rebase session could not resolve: ' in reason
            or ('NEEDS OPERATOR' in reason and 'force-with-lease' in reason))


def no_report(s):
    """Whether ended session ``s`` stopped without a REPORT (and without a question, an API
    failure or a push of its own)."""
    return (s.ended and not s.fields and not s.status and not s.question and not s.api_error
            and s.result not in ('pushed', 'question'))


def relaunch(it):
    """Whether ``it``'s last attempt is a session that ended without a REPORT, or a relaunch the
    kernel granted (:data:`RELAUNCH`: an operator answer, a legacy Stuck re-judged): relaunched
    first."""
    return bool(it.attempts) and (it.attempts[-1] == NO_REPORT
                                  or str(it.attempts[-1]).startswith(RELAUNCH))


def _claimed_on_origin(value, iid, branch, facts):
    """Whether a ``pushed:`` ``value`` claims a push and ``iid``'s branch (``branch`` when named,
    else any) is on origin: the origin branch is at that sha or has moved past it."""
    claimed, _sha = R.pushed_claim(value)
    if not claimed:
        return False
    names = {b.name for b in facts.branches if b.item_id == iid}
    return branch in names if branch else bool(names)


def pushed_note(s):
    """The note a done-and-pushed session's ``NEEDS OPERATOR:`` line leaves on its item, or ''."""
    q = ' '.join(str(s.question or '').split())
    return '%s: NEEDS OPERATOR: %s' % (s.job, R.cap(q)) if q else ''


def pushed_branch(iid, facts, config, item=None):
    """The branch on origin (``facts.branches``) that holds ``iid``'s work, or None: the one the
    kernel would launch on (:func:`_branch`) first, else the first by name."""
    named = sorted(b.name for b in facts.branches if b.item_id == iid)
    want = _branch('build', iid, config, item)
    return want if want in named else (named[0] if named else None)


def _pr_branch(it, sessions, pushed, stuck, ended_stuck, facts, config):
    """The branch ``it`` (no open PR, no live session) gets a pull request for, or None:

    - a session that ended ``done`` and pushed this tick: its branch;
    - a Stuck recorded from a ``done`` REPORT (:data:`DONE_STUCK`) whose branch is on origin;
    - a ``done without a push`` Stuck (:data:`NO_PUSH_STUCK`) whose ``pushed:`` claims a push and
      whose branch is on origin;
    - a card in :data:`PR_STATES`, no session ended this tick, its branch on origin."""
    if pushed is not None:
        return pushed.branch or pushed_branch(it.id, facts, config, it) or \
            _branch('build', it.id, config, it)
    if ended_stuck is not None or any(not s.alive for s in sessions):
        return None
    branch = pushed_branch(it.id, facts, config, it)
    if not branch:
        return None
    if stuck is not None:
        reason = str(stuck.reason)
        if reason.startswith(DONE_STUCK):
            return branch
        if reason.startswith(NO_PUSH_STUCK) and \
                _claimed_on_origin(reason[len(NO_PUSH_STUCK):], it.id, branch, facts):
            return branch
        return None
    return branch if it.state in PR_STATES else None


def pr_title(it):
    """A kernel PR's title: ``<ITEM-ID> — <card title>`` (the item id first, so a head-branch or
    title check that wants an item id finds it)."""
    title = ' '.join(str(it.title or '').split())
    return '%s — %s' % (it.id, title) if title else it.id


def pr_body(it, session=None):
    """A kernel PR's body: the item it delivers and the REPORT of the session that pushed it."""
    lines = ['Delivers %s%s.' % (it.id, ' — %s' % it.title if it.title else ''), '']
    fields = (session.fields if session is not None else None) or {}
    if fields:
        lines += ['## Session REPORT (%s)' % session.job, '']
        lines += ['- %s: %s' % (k, ' '.join(str(v).split())) for k, v in fields.items() if v]
    else:
        lines.append('Pushed by a kernel build session; its REPORT is on the item\'s card.')
    return '\n'.join(lines) + '\n'


def open_pr_action(it, branch, session=None):
    """The :class:`OpenPR` of ``it``'s pushed ``branch`` against the trunk."""
    return A.OpenPR(it.id, branch, '', pr_title(it), pr_body(it, session))


def _ended_stuck(s, open_pr):
    """The Stuck an ended (not review) session leaves its item in, from what its REPORT says, or
    None when the normal flow goes on (a push, or ``done`` on an open PR)."""
    if s.status in (R.PARTIAL, R.BLOCKED):
        return _stuck(R.stuck_reason(s.status, s.fields, s.question),
                      'operator' if s.question else 'session')
    if s.status == R.DONE and s.result != 'pushed' and s.push_refused:
        return _stuck(R.cap(NOT_PUSHED + s.push_refused), 'operator')
    if s.result == 'question' or s.question:
        return _stuck(R.stuck_reason(s.status or 'question', s.fields, s.question)
                      if s.fields else (s.question or s.last_line or 'session asked a question'),
                      'operator')
    if s.result == 'pushed':
        return None
    if s.status == R.DONE:
        if open_pr is not None:
            return None  # its PR is judged on GitHub's facts
        pushed = ' '.join(str(s.fields.get('pushed') or '').split())
        return _stuck('done without a push%s' % (': pushed: %s' % pushed if pushed else ''),
                      'session')
    if s.fields:
        return _stuck(R.stuck_reason(s.status, s.fields), 'session')
    return _stuck(R.no_report_reason(s.last_line), 'session')


def _park(it, facts, config):
    """A parked item's :class:`_Judged`: Done when it is Done, else Parked; every action its own
    facts would ask for is dropped."""
    j = _judge(it, facts, config, [])
    return j if j.state is State.DONE else _Judged(State.PARKED, hold=True)


def flying(facts, config):
    """The ids of the items with an open kernel PR (:func:`kernel_owned` branch, not merged).
    Parking stops NEW launches only: a parked item in this set carries on through review, its
    fix rounds, the merge train and landing as any other — the finished work lands rather than
    rots — and once its PR merged it is Done (and parked again for anything new)."""
    return {p.item_id for p in facts.prs
            if not p.merged and p.item_id in facts.items
            and kernel_owned(p.branch, config, p.item_id)}


def _parked(items):
    """The ids of every item that is ``priority: later``, or under one through ``parent``."""
    out = set()
    for iid in items:
        seen, cur = set(), iid
        while cur in items and cur not in seen:
            if items[cur].priority == 'later' or cur in out:
                out.add(iid)
                break
            seen.add(cur)
            cur = items[cur].parent
    return out


def _judge_pr(it, pr, attempts, facts, config, actions, extra=0, granted=False, fresh=False):
    """The state of ``it`` holding open PR ``pr``: conflict, red checks, then the review. Its
    fix-round cap is ``config.max_fix_rounds`` + ``extra`` (rounds operator answers granted);
    ``granted``: an answer granted one this tick, so a spent rebase finding no longer counts;
    ``fresh``: a session's head was pushed (or is published by the host) this tick, so the PR's
    conflict facts predate it and it is judged next tick."""
    if conflicting(pr, attempts):
        if fresh:
            return _Judged(State.REVIEW, hold=True)
        if not granted and any(rebase_finding(f, pr.number) for f in it.findings):
            reason = ('%s the rebase session could not resolve: PR #%d still conflicts on head %s'
                      % (CONFLICT, pr.number, pr.head_sha or '?'))
            return (_rebuild(it, pr, reason, config, facts, actions)
                    or _Judged(State.STUCK, Stuck(reason, 'operator', CONFLICT_NEXT % pr.number)))
        return _fix_round(it, pr, '%s: PR #%d' % (CONFLICT, pr.number), config,
                          [rebase_finding_for(pr.number)], extra, facts, actions)

    reds = [c for c in pr.checks if c.status == 'completed' and c.conclusion in RED_CONCLUSIONS]
    gating = [c for c in reds if required(c.name, config)]
    for c in reds:
        if c not in gating:
            _note(it, ignored_red_note(pr, c), actions)
    own = [c for c in gating if on_pr(c, pr)]
    if own:
        return _fix_round(it, pr, '%s: %s' % (RED, ', '.join(c.name for c in own)), config,
                          [red_finding(c) for c in own], extra, facts, actions)
    esc = _due(config.escalate_after_h, it, facts,
               fresh=not _red_off(it.stuck if it.state is State.STUCK else None))
    for c in gating:
        if c.attempt - 1 < config.max_reruns + (1 if esc else 0):
            actions.append(A.Rerun(c.run_id))
        elif esc:  # its one more rerun is spent: the red is the PR's to fix after all
            return _fix_round(it, pr, '%s: %s' % (RED, c.name), config, [red_finding(c)], extra,
                              facts, actions)
        else:
            return _Judged(State.STUCK, _stuck('%s%d rerun(s): %s'
                                               % (RED_OFF, c.attempt - 1, c.name), 'ci'))

    bounded = ci_bound(facts, config) is not None  # stalls handled: a cancel is rerun
    for c in pr.checks:  # a required check cancelled on the head: nothing else will run it
        if (bounded and c.status == 'completed' and c.conclusion == 'cancelled' and c.run_id
                and required(c.name, config) and c.attempt - 1 <= config.max_reruns
                and not any(isinstance(a, A.Rerun) and a.run_id == c.run_id for a in actions)):
            actions.append(A.Rerun(c.run_id))

    verdict = None
    for r in facts.reviews:
        if r.item_id == it.id and verdict_holds(r, pr):
            verdict = r
    if verdict is None:
        if not ci_green(pr, config):  # CI is the test gate: a review is worth running once it is
            return _Judged(State.REVIEW)
        return _Judged(State.REVIEW, review_branch=pr.branch)
    if verdict.verdict != 'approve':
        return _fix_round(it, pr, 'review: changes requested', config, extra=extra, facts=facts,
                          actions=actions)
    if not pr.auto_merge:
        actions.append(A.EnableAutoMerge(pr.number))
    elif auto_merge_idle(pr, config):  # enabled a tick ago, green and CLEAN, not merged
        actions.append(A.MergePR(pr.number, pr.head_sha))
        return _Judged(State.LANDING)
    if in_flight(pr, config):  # its CI runs on: no update until it ends (a red: a fix round)
        return _Judged(State.LANDING, updating=True)
    if pr.behind and facts.strict:  # not strict: GitHub merges it behind, an update burns CI
        return _Judged(State.LANDING, behind_pr=pr)
    return _Judged(State.LANDING)


def ci_green(pr, config):
    """Whether ``pr``'s head has its CI result and it is green: every required check reported and
    completed ``success``/``neutral``/``skipped`` (with none named, every check on the head, and
    a head with no check at all has no CI to wait for). A review is launched, and valued, only
    then: the checks are the test gate, so the reviewer reads and never reruns the suite."""
    req = [c for c in pr.checks if required(c.name, config)]
    names = {c.name for c in req}
    if not all(n in names for n in config.required_checks):
        return False
    return all(c.status == 'completed' and c.conclusion in ('success', 'neutral', 'skipped')
               for c in req)


def auto_merge_idle(pr, config):
    """Whether approved ``pr`` waits on a GitHub auto-merge that has not fired: auto-merge was
    already on when this tick read it (enabled a tick ago or more), GitHub says CLEAN, its head
    is known and every required check completed green on it — the kernel merges it directly
    (:class:`MergePR`, ``--match-head-commit``)."""
    if not (pr.auto_merge and pr.clean and pr.head_sha and not pr.merged):
        return False
    req = [c for c in pr.checks if required(c.name, config)]
    names = {c.name for c in req}
    return bool(req) and all(n in names for n in config.required_checks) and all(
        c.status == 'completed' and c.conclusion in ('success', 'neutral', 'skipped')
        for c in req)


def conflict_attempt(pr_number, head_sha):
    """The reason prefix of the attempt a failed update on a merge conflict leaves for PR
    ``pr_number`` at head ``head_sha``: that head conflicts, whatever GitHub's lazy
    ``mergeable`` says next tick."""
    return '%s: PR #%d at %s: ' % (CONFLICT, pr_number, head_sha or '?')


def conflicting(pr, attempts):
    """Whether ``pr`` conflicts with its base: GitHub says so (``mergeable == CONFLICTING`` or
    ``mergeStateStatus == DIRTY``, read into ``PR.conflicting``), or an update of its current
    head already failed on a merge conflict (an attempt :func:`conflict_attempt`)."""
    if pr.conflicting:
        return True
    return bool(pr.head_sha) and any(
        str(a).startswith(conflict_attempt(pr.number, pr.head_sha)) for a in attempts)


def required(name, config):
    """Whether check ``name`` gates the landing: it is in ``config.required_checks``, or that set
    is empty (nothing named: every check counts)."""
    return not config.required_checks or name in config.required_checks


def on_pr(check, pr):
    """Whether red ``check`` is the PR's own: its failing files meet the PR's, or none are known
    (a lint or ratchet step names no test file; an unread log names nothing). Only a red whose
    files are all known and all outside the PR is off it."""
    return not check.failing_files or bool(set(check.failing_files) & set(pr.files))


def red_finding(check):
    """The finding a fix round on red ``check`` carries: the check, its failed step and the tail
    of its failed log (whatever of them was read)."""
    head = '%s: %s' % (RED, check.name)
    if check.failed_step:
        head += ' — step %r failed' % check.failed_step
    tail = str(check.log_tail or '').strip()
    if len(tail) > LOG_TAIL_MAX:
        tail = '…' + tail[-LOG_TAIL_MAX:]
    return head + (':\n' + tail if tail else '')


def red_findings(findings):
    """The red-driven ones among ``findings``."""
    return [f for f in findings if str(f).startswith(RED + ': ')]


def ignored_red_note(pr, check):
    """The note a red on a check that is not required leaves on its item."""
    return 'PR #%d: %s is red but not a required check — ignored' % (pr.number, check.name)


def _note(it, text, actions):
    """Append a :class:`NoteItem` of ``text`` unless the card or this tick already holds it."""
    if text and text not in it.notes and not any(
            isinstance(a, A.NoteItem) and (a.item_id, a.text) == (it.id, text) for a in actions):
        actions.append(A.NoteItem(it.id, text))


def _red_off(stuck):
    """Whether a recorded Stuck is a required red off the PR (:data:`RED_OFF`): judged afresh."""
    return stuck is not None and stuck.owner == 'ci' and str(stuck.reason).startswith(RED_OFF)


def _fix_round(it, pr, reason, config, findings=(), extra=0, facts=None, actions=None):
    """Ready on ``pr``'s branch for one more fix round (carrying ``findings`` and an operator
    answer's text into the launch), or past the cap — ``config.max_fix_rounds`` plus the
    ``extra`` rounds operator answers granted: one extra round on ``config.strong_model`` with
    every finding the item holds (:data:`STRONG_ROUND`, once ``escalate_after_h`` allows), then
    a rebuild (:func:`_rebuild`), else Stuck(operator)."""
    cap = config.max_fix_rounds + extra
    strong = STRONG_ROUND in it.attempts
    if it.fix_rounds + 1 > cap + (1 if strong else 0):
        stuck = _stuck('%s after %d fix rounds' % (reason, it.fix_rounds), 'operator')
        if actions is None:
            return _Judged(State.STUCK, stuck)
        if not strong and _due(config.escalate_after_h, it, facts,
                               fresh=not capped(it.stuck)):
            actions.append(A.ClearStuck(it.id, STRONG_ROUND))
            strong = True
        else:
            return (_rebuild(it, pr, stuck.reason, config, facts, actions) if strong else None) \
                or _Judged(State.STUCK, stuck)
    answered = [f for f in relaunch_findings(it) if f.startswith((ANSWERED, ESCALATED))]
    if strong and it.fix_rounds + 1 > cap:  # the one extra round: the strong model, every finding
        every = list(findings) + [f for f in it.findings if f not in findings]
        return _Judged(State.READY, branch=pr.branch, findings=every + answered,
                       model=config.strong_model)
    return _Judged(State.READY, branch=pr.branch, findings=list(findings) + answered)


#: the end of a Stuck reason at the fix-round cap (:func:`_fix_round`)
CAPPED_RE = re.compile(r' after \d+ fix rounds$')


def capped(stuck):
    """Whether recorded Stuck ``stuck`` is the fix-round cap's, or a conflict the rebase session
    could not resolve: an operator answer to it grants one more fix round."""
    reason = str(stuck.reason) if stuck is not None else ''
    return bool(CAPPED_RE.search(reason)) or ' the rebase session could not resolve: ' in reason


def rebase_finding_for(pr_number):
    """The finding a rebase round carries for PR ``pr_number``."""
    return '%s: PR #%d conflicts with its base — %s' % (REBASE, pr_number, REBASE_ASK)


def rebase_finding(finding, pr_number=None):
    """Whether ``finding`` is a rebase round's (for PR ``pr_number``, when given)."""
    head = '%s: PR #' % REBASE
    if not str(finding).startswith(head):
        return False
    return pr_number is None or str(finding).startswith('%s%d ' % (head, pr_number))


def _legacy_conflict(stuck):
    """Whether a recorded Stuck is the old 'still conflicts after an update' one (owner=loop,
    before the rebase round existed): it is judged afresh, so it gets its rebase session."""
    return (stuck is not None and stuck.owner == 'loop'
            and str(stuck.reason).startswith('%s: PR #' % CONFLICT)
            and str(stuck.reason).endswith('still conflicts after an update'))


def _repeated(attempts, limit):
    """The first reason that ``attempts`` holds at least ``limit`` times (a :data:`NO_VERDICT` at
    least :data:`NO_VERDICT_LIMIT` times), else ``None``."""
    for reason in attempts:
        if attempts.count(reason) >= (max(limit, NO_VERDICT_LIMIT) if reason == NO_VERDICT
                                      else limit):
            return reason
    return None


def _stuck(reason, owner):
    """A Stuck on ``owner`` whose reason always says something (:func:`asf.kernel.reports.clean`)."""
    assert owner in OWNERS, owner
    return Stuck(R.clean(reason, 'stuck on the %s with no readable reason' % owner), owner,
                 NEXT_ACTION[owner])


def _keep(stuck):
    """A copy of a recorded Stuck (``blocked_count`` is recomputed every tick)."""
    return Stuck(stuck.reason, stuck.owner, stuck.next_action or NEXT_ACTION.get(stuck.owner, ''),
                 stuck.blocked_count)


# ---- pass two: derived states, waits, blocked counts --------------------------------------------

def _children(items):
    """``{parent_id: [child ids]}`` from the ``parent`` link, and for a Story also the Tasks that
    declare it on their ``stories:`` line (:attr:`asf.kernel.model.Item.stories`)."""
    out = {}
    for iid in sorted(items):
        parent = items[iid].parent
        if parent in items:
            out.setdefault(parent, []).append(iid)
        for sid in items[iid].stories:
            if sid != parent and sid in items and items[sid].type == 'story':
                out.setdefault(sid, []).append(iid)
    return out


def _derived(iid, items, children):
    """Whether ``iid``'s state comes from its children: a container that has some — save a Feature
    with no Task or Bug under it (only its Stories, minted from its spec): its document lane is
    still its own, so it launches its plan."""
    if items[iid].type not in CONTAINERS or not children.get(iid):
        return False
    return items[iid].type != DOCUMENTED or _has_work(iid, items, children)


def _has_work(iid, items, children, seen=()):
    """Whether a Task or Bug hangs anywhere under ``iid`` (through :func:`_children`)."""
    for c in children.get(iid, ()):
        if items[c].type in BUILDABLE or (c not in seen
                                          and _has_work(c, items, children, seen + (iid,))):
            return True
    return False


def _state_of(iid, items, judged, children, states, seen, parked):
    """Fill ``states[iid]``: a container with children takes its state from its visible ones
    (Parked when it has none, or is parked itself and not all Done); any other item its
    pass-one state."""
    if iid in states:
        return states[iid]
    if not _derived(iid, items, children) or iid in seen:
        j = judged[iid]
        states[iid] = (j.state, j.stuck)
        return states[iid]
    kids = [(c, _state_of(c, items, judged, children, states, seen + (iid,), parked))
            for c in children[iid]]
    kids = [(c, k) for c, k in kids if k[0] is not State.PARKED]
    if not kids or (iid in parked and len(kids) < len(children[iid])):
        states[iid] = (State.PARKED, None)
        return states[iid]
    kids_ids, kids = [c for c, _ in kids], [k for _, k in kids]
    got = [s for s, _ in kids]
    if all(s is State.DONE for s in got):
        states[iid] = (State.DONE, None)
    elif all(s in (State.DONE, State.STUCK) for s in got):
        stuck = [(c, k[1]) for c, k in zip(kids_ids, kids) if k[0] is State.STUCK]
        reason = 'stuck: ' + ', '.join(c for c, _ in stuck)
        states[iid] = (State.STUCK, Stuck(reason, stuck[0][1].owner, stuck[0][1].next_action))
    else:
        order = (State.BUILDING, State.REVIEW, State.LANDING, State.READY, State.NEW)
        states[iid] = (next(s for s in order if s in got), None)
    return states[iid]


def _visible(items, iid, parked):
    return iid in items and iid not in parked


def edge_open(a, items, states, parked, unreadable=()):
    """Whether an ``after:`` edge to ``a`` still holds: it is satisfied only once ``a`` is Done
    (its PR merged) or retired (a retired card reads as Done) — or parked (nothing waits on a
    parked item). A card in LIMBO (``unreadable``) or an id not on the record holds: whether its
    change landed cannot be told."""
    if a in unreadable or a not in items:
        return True
    return a not in parked and states.get(a, (State.NEW,))[0] is not State.DONE


def open_edges(it, items, states, parked, unreadable=()):
    """``it``'s ``after:`` ids whose edge still holds (:func:`edge_open`), in its order."""
    return [a for a in it.after if a != it.id and edge_open(a, items, states, parked, unreadable)]


def _apply_waits(items, judged, children, states, parked, unreadable=()):
    """A Ready item with a declared ``after:`` edge still open (:func:`edge_open`) waits (New);
    re-derive the containers above it."""
    changed = False
    for iid in sorted(items):
        it = items[iid]
        if states[iid][0] is not State.READY or _derived(iid, items, children):
            continue
        if open_edges(it, items, states, parked, unreadable):
            judged[iid].state = State.NEW
            states[iid] = (State.NEW, None)
            changed = True
    if changed:
        for iid in [i for i in states if _derived(i, items, children)]:
            del states[iid]
        for iid in sorted(items):
            _state_of(iid, items, judged, children, states, (), parked)


def _count_blocked(items, states, parked):
    """Set ``blocked_count`` on every Stuck item: the visible, not-Done items that transitively
    wait on it through declared ``after:`` edges. Returns that count for every item that holds
    any (Stuck or not)."""
    waiters = {}
    for iid in sorted(items):
        if not _visible(items, iid, parked):
            continue
        for a in items[iid].after:
            if _visible(items, a, parked):
                waiters.setdefault(a, []).append(iid)
    counts = {}
    for iid in sorted(waiters):
        seen, todo = set(), list(waiters.get(iid, []))
        while todo:
            w = todo.pop()
            if w in seen or w == iid or w not in states or states[w][0] is State.DONE:
                continue
            seen.add(w)
            todo += waiters.get(w, [])
        if seen:
            counts[iid] = len(seen)
    for iid, (state, stuck) in states.items():
        if state is State.STUCK:
            stuck.blocked_count = counts.get(iid, 0)
    return counts


# ---- the record and the launches ----------------------------------------------------------------

def _mint(facts, parked):
    """A :class:`MintStory` for each Story a landed spec of a visible Feature declares that the
    record lacks."""
    out = []
    for fid in sorted(set(facts.specs_landed) - parked):
        for sid, story in declared_stories(facts.specs_landed[fid]).items():
            if sid not in facts.items:
                out.append(A.MintStory(fid, sid, story['title'], list(story['acceptance'])))
    return out


#: the launch classes, in the order scarce seats fill (finish first): a fix round or rebase on an
#: open PR, a review, a groom-fill of a card the Definition of Ready holds (capped a tick: a
#: build needs a ready card, and a full house of builds would starve it), a build of a Task/Bug,
#: a plan, a spec
FINISH_FIRST = ('fix', 'review', 'build', 'plan', 'spec', 'groom')

#: the launch classes the WIP cap (``Config.max_open_prs``) holds back: new work
NEW_WORK = ('build', 'plan', 'spec')


def open_prs(judged, on_pr=()):
    """The WIP the cap counts: the items in Review or Landing, plus those on a fix round of an
    open PR (Ready or Building with an open PR in ``on_pr``) — finished work not yet landed."""
    return sum(1 for iid, j in judged.items()
               if j.state in (State.REVIEW, State.LANDING)
               or (iid in on_pr and j.state in (State.READY, State.BUILDING)))


def _launches(facts, config, judged, children, states, parked, blocks=None, due=None,
              queued=None, wip=None, unready=None):
    """``(launches, skipped)``: while sessions are free, finish first (:data:`FINISH_FIRST`) — a
    Ready item with an open PR (a fix round, a rebase), then reviews, then builds, plans and specs
    — each class in launch order (:func:`launch_order`): an item whose wait is over its target
    (``due``) before the rest, then (not for a review) a relaunch (:func:`relaunch`), the one
    holding the most others (``blocks``) first. A Ready item whose ``writes`` overlap a Building
    (or just launched) visible item waits its turn (``skipped[iid] = 'overlap'``). A review over
    its target asks for a local seat first (``Launch.local``). While the items in Review plus
    Landing exceed ``config.max_open_prs`` (the WIP cap) no build, plan or spec launches
    (``skipped[iid] = 'wip'``) and ``wip`` (filled when given) records the hold. ``queued``
    (filled when given) maps every item left for want of a seat to ``'seat'``, every overlap to
    ``'overlap'`` and every WIP hold to ``'wip'``. After the reviews come the groom-fill sessions of
    the cards the Definition of Ready holds (``unready``), at most ``config.dor_fill_per_tick`` a
    tick and
    ``config.dor_max_fills`` a card, with live ones at most ``config.dor_max_concurrent`` seats,
    after the fix rounds, reviews, builds, plans and specs, never held by the WIP cap or a file overlap (they only read)."""
    items = facts.items
    free = _free(facts, config)
    inherit = config.rank != 'own'
    blocks = blocks or {}
    due = due or {}
    queued = {} if queued is None else queued
    rank = lambda iid: launch_order(iid, items, inherit)  # noqa: E731
    first = lambda iid: ((iid not in due,),  # noqa: E731
                         (0, -blocks.get(iid, 0)) if relaunch(items[iid]) else (1, 0), rank(iid))
    on_pr = {p.item_id for p in facts.prs if not p.merged}
    cands = [('review', iid, (iid not in due, rank(iid))) for iid, j in judged.items()
             if j.review_branch and not j.hold]
    for iid, j in judged.items():
        if (states[iid][0] is State.READY and not j.hold and _visible(items, iid, parked)
                and not _derived(iid, items, children)):
            kind = _kind(items[iid], facts, config)
            cands.append(('fix' if iid in on_pr else kind, iid, first(iid)))
    for iid in sorted(unready or ()):
        j = judged[iid]
        if (j.state is State.NEW and not j.hold and _visible(items, iid, parked)
                and items[iid].dor_fills < config.dor_max_fills):
            cands.append(('groom', iid, first(iid)))
    cands.sort(key=lambda c: (FINISH_FIRST.index(c[0]), c[2]))
    cap = config.max_open_prs
    n_open = open_prs(judged, on_pr)
    over = cap is not None and n_open > cap
    busy = [items[i].writes for i in items
            if states[i][0] is State.BUILDING and _visible(items, i, parked)]
    out, skipped, held, fills = [], {}, 0, 0
    live_fills = sum(1 for s in facts.sessions if s.alive and s.kind == D.GROOM_FILL)
    for cls, iid, _key in cands:
        if cls == 'groom' and (fills >= config.dor_fill_per_tick or fills + live_fills
                               >= config.dor_max_concurrent):
            continue
        if over and cls in NEW_WORK:
            skipped[iid] = queued[iid] = 'wip'
            held += 1
            continue
        if free <= 0:
            queued[iid] = 'seat'
            continue
        if cls == 'review':
            pr = _open_pr_of(iid, facts)
            out.append(A.Launch('review', iid, judged[iid].review_branch, local=iid in due,
                                model=config.strong_model if high_risk(items[iid], pr, config)
                                else ''))
            free -= 1
            continue
        if cls == 'groom':
            out.append(A.Launch(D.GROOM_FILL, iid, config.groom_branch + iid, [judged[iid].dor]))
            fills += 1
            free -= 1
            continue
        if any(_overlap(items[iid].writes, w) for w in busy):
            skipped[iid] = queued[iid] = 'overlap'
            continue
        kind = _kind(items[iid], facts, config)
        out.append(A.Launch(kind, iid, judged[iid].branch or _branch(kind, iid, config, items[iid]),
                            list(judged[iid].findings), judged[iid].model))
        busy.append(items[iid].writes)
        free -= 1
    if held and wip is not None:
        wip.update({'open': n_open, 'cap': cap, 'held': held})
    return out, skipped


def _free(facts, config):
    """Seats free now: ``config.max_sessions`` — or the host's real capacity
    (``Facts.seats``) when lower — less the live sessions."""
    seats = config.max_sessions if facts.seats is None else min(config.max_sessions, facts.seats)
    return seats - sum(1 for s in facts.sessions if s.alive)


def _idle(facts, config, judged, states, parked, skipped):
    """The idle alarm (see the module doc), or None: why each waiting Task/Bug did not launch."""
    free = _free(facts, config)
    if not config.idle_alarm or free < max(config.idle_min_free, 1):
        return None
    items, reasons = facts.items, {}
    for iid, (state, _st) in states.items():
        it = items.get(iid)
        if (it is None or it.type not in BUILDABLE or not _visible(items, iid, parked)
                or state not in (State.NEW, State.READY)):
            continue
        if state is State.NEW:
            waits = open_edges(it, items, states, parked, facts.unreadable or {})
            why = 'after' if waits else 'dor' if judged[iid].dor else 'new'
        elif facts.paused:
            why = 'paused'
        else:
            why = skipped.get(iid) or ('held' if judged[iid].hold else 'new')
        key = 'waits on %s' % waits[0] if state is State.NEW and waits else IDLE_REASONS[why]
        reasons[key] = reasons.get(key, 0) + 1
    if not reasons:
        return None
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return {'free': free, 'waiting': sum(reasons.values()), 'reasons': top}


def _kind(it, facts, config=None):
    if it.type in BUILDABLE:
        return 'build'
    landed = it.id in facts.specs_landed or (
        config is not None and _spec_only(it, [p for p in facts.prs if p.item_id == it.id], config))
    return 'plan' if landed else 'spec'


def _lane_prefix(config, kind):
    return next((p for p in config.doc_branches if p.strip('/') == kind), None)


def _spec_only(it, prs, config):
    """Whether ``it`` is a Feature whose merged pull requests are all on its spec branch: a landed
    spec is the start of the Feature's work (its plan is next), never its end — and a Done its card
    holds from that merge is not one either."""
    spec = _lane_prefix(config, 'spec')
    merged = [p for p in prs if p.merged]
    return (it.type == DOCUMENTED and bool(spec) and bool(merged)
            and all(p.branch.startswith(spec) for p in merged))


def _branch(kind, iid, config, item=None):
    for prefix in config.doc_branches:
        if prefix.strip('/') == kind:
            return prefix + iid
    if item is not None and item.type == 'bug':
        return config.fix_branch + iid
    return config.work_branch + iid


def _is_glob(path):
    return any(ch in path for ch in '*?[')


def _overlap(writes, others):
    """Whether two lists of path globs can name a common path."""
    for a in writes:
        for b in others:
            if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
                return True
            if _is_glob(a) and _is_glob(b):
                pa, pb = _prefix(a), _prefix(b)
                if pa.startswith(pb) or pb.startswith(pa):
                    return True
    return False


def _prefix(glob):
    """The literal directory part of ``glob`` before its first wildcard (``''`` at the root)."""
    head = glob[:min(glob.find(ch) for ch in '*?[' if ch in glob)]
    return head.rpartition('/')[0] + '/' if '/' in head else ''


# ---- nothing waits without an action: the clean floor, breaches, LIMBO --------------------------

#: how a breach line starts when the kernel has no action for it this tick
NO_BREACH_ACTION = 'none: '


def kernel_owned(branch, config, item_id=None):
    """Whether ``branch`` is one the kernel launches on: one of its prefixes (``work_branch``,
    ``fix_branch``, ``doc_branches``) followed by the item id (``item_id``, else any id) and
    nothing else — a hand-made branch that only names an item (``fix/B-1-kernel-0.2``) is never
    the kernel's to close."""
    prefixes = [p for p in (config.work_branch, config.fix_branch) + tuple(config.doc_branches)
                if p]
    b = str(branch or '')
    for p in prefixes:
        if b.startswith(p):
            rest = b[len(p):]
            if item_id is not None:
                if rest.upper() == str(item_id).upper():
                    return True
            elif re.fullmatch(r'[A-Za-z]+-\d+', rest):
                return True
    return False


def floor_closes(facts, config):
    """The :class:`ClosePR` of every open kernel PR nothing will ever move (``config.close_floor``):
    its item is not on the record (``Facts.orphan_prs``), or its card is Done (a retired card
    reads as Done) — not reopened, not a Feature whose spec only landed, held by no live
    session."""
    if not config.close_floor:
        return []
    out = []
    for p in sorted(facts.orphan_prs, key=lambda p: p.number):
        if (not p.merged and kernel_owned(p.branch, config, p.item_id)
                and p.item_id not in facts.items
                and p.item_id not in facts.unreadable):  # an unreadable card is still there
            out.append(A.ClosePR(p.number, p.branch, p.item_id,
                                 'its item %s is not on the record' % p.item_id))
    for iid in sorted(facts.items):
        it = facts.items[iid]
        prs = [p for p in facts.prs if p.item_id == iid]
        if it.state is not State.DONE or it.reopened or (it.reverted and not landed(it, prs)):
            continue
        if _spec_only(it, prs, config) or any(s.item_id == iid and s.alive
                                              for s in facts.sessions):
            continue
        why = 'its item %s is %s' % (iid, 'Done or retired' if it.priority == 'later' else 'Done')
        out += [A.ClosePR(p.number, p.branch, iid, why)
                for p in sorted(prs, key=lambda p: p.number)
                if not p.merged and kernel_owned(p.branch, config, iid)]
    return out


def session_bound(s, facts, config):
    """Seconds live session ``s`` may run before it is a stall: its class's p90 on the wait ledger
    (``Facts.bounds``: ``review`` for a reviewer, ``building`` for any other) — twice it once the
    item's branch is on origin (it pushed) — else the fallback knob (``config.max_review_age_h``
    / ``config.max_session_age_h``), else None (unbounded)."""
    bounds = facts.bounds or {}
    if s.kind == 'review':
        p = bounds.get('review')
        fallback = config.max_review_age_h if config.max_review_age_h is not None \
            else config.max_session_age_h
    else:
        p = bounds.get('building')
        fallback = config.max_session_age_h
        if p and any(b.item_id == s.item_id for b in facts.branches):
            p *= 2
    if p:
        return p
    return fallback * 3600 if fallback is not None else None


def _over_age(facts, config):
    """``[(session, age in seconds)]``: every live session past its bound
    (:func:`session_bound`; ``Session.started`` against ``Facts.now``)."""
    out = []
    for s in facts.sessions:
        limit = session_bound(s, facts, config)
        age = stuck_age_h(s.started, facts.now) if s.alive and s.started else None
        if limit is not None and age is not None and age * 3600 > limit:
            out.append((s, age * 3600))
    return out


def ci_bound(facts, config):
    """Seconds a PR's required CI may run before it is a stall: twice the ``ci`` class's p90 on
    the wait ledger, else ``config.max_ci_age_h``, else None."""
    p = (facts.bounds or {}).get('ci')
    if p:
        return 2 * p
    return config.max_ci_age_h * 3600 if config.max_ci_age_h is not None else None


def ci_stalls(facts, config, states):
    """``{item: (age in seconds, [run ids])}``: every Landing item whose required CI has run
    (its ``ci`` spell on the wait ledger) past :func:`ci_bound` — its running required checks'
    runs are cancelled and rerun."""
    bound = ci_bound(facts, config)
    out = {}
    if bound is None:
        return out
    for iid, (cls, since) in sorted((facts.waits or {}).items()):
        if cls != 'ci' or iid not in states or states[iid][0] is not State.LANDING:
            continue
        age = stuck_age_h(since, facts.now)
        if age is None or age * 3600 <= bound:
            continue
        pr = max((p for p in facts.prs if p.item_id == iid and not p.merged),
                 key=lambda p: p.number, default=None)
        runs = sorted({c.run_id for c in (pr.checks if pr else [])
                       if c.status != 'completed' and required(c.name, config) and c.run_id})
        if runs:
            out[iid] = (age * 3600, runs)
    return out


def overdue(facts, config, states, same_class=True):
    """``{item: (wait class, age in seconds)}``: every Task or Bug whose ledger spell
    (``Facts.waits``) is older than its class's target (``config.wait_targets``) — and, with
    ``same_class``, whose wait class now (:func:`asf.kernel.waits.classify` of its state this
    tick) is still that one."""
    targets = config.wait_targets or {}
    out = {}
    for iid, (cls_then, since) in sorted((facts.waits or {}).items()):
        it = facts.items.get(iid)
        if it is None or it.type not in W.FOLLOWED or iid not in states:
            continue
        target = targets.get(W.target_key(cls_then))
        if target is None or not W.is_wait(cls_then):
            continue
        age = stuck_age_h(since, facts.now)
        if age is None or age * 3600 <= target:
            continue
        if same_class:
            state, stuck = states[iid]
            if W.classify(iid, state, stuck, facts, config)[0] != cls_then:
                continue
        out[iid] = (cls_then, age * 3600)
    return out


def _item_of(action, facts):
    """The item id ``action`` moves (a PR's or a run's item for a PR action), else None."""
    if isinstance(action, (A.UpdateBranch, A.EnableAutoMerge, A.MergePR)):
        return next((p.item_id for p in facts.prs if p.number == action.pr), None)
    if isinstance(action, A.Rerun):
        return next((p.item_id for p in facts.prs for c in p.checks
                     if c.run_id == action.run_id), None)
    if isinstance(action, A.EndSession):
        return next((s.item_id for s in facts.sessions if s.job == action.job), None)
    return getattr(action, 'item_id', None)


def breaches(due, over_age, actions, facts, config, states, judged, queued, stalled_ci=None):
    """``Plan.breaches``: one record per overdue wait (:func:`overdue`) and per session ended
    past its max age — ``{'item', 'class', 'age_s', 'action'}``, ``action`` the line
    (:func:`asf.kernel.actions.describe`) of what this tick does about it, or
    :data:`NO_BREACH_ACTION` and why the kernel has none."""
    moves = {}
    for a in actions:
        if isinstance(a, PROGRESS) and not (isinstance(a, A.EndSession) and a.free_worktree):
            iid = _item_of(a, facts)
            if iid is not None:
                moves.setdefault(iid, a)
    out = []
    for s, age in over_age:
        out.append({'item': s.item_id, 'class': 'building' if s.kind == 'build'
                    else 'session:%s' % s.kind, 'age_s': age,
                    'action': A.describe(A.EndSession(s.job, free_worktree=False))})
    for iid, (age, runs) in sorted((stalled_ci or {}).items()):
        out.append({'item': iid, 'class': 'ci', 'age_s': age,
                    'action': A.describe(A.Rerun(runs[0], cancel=True))
                    + (' (+%d run(s))' % (len(runs) - 1) if len(runs) > 1 else '')})
    ended = {s.item_id for s, _age in over_age} | set(stalled_ci or {})
    moved_on = {iid: v for iid, v in overdue(facts, config, states, same_class=False).items()
                if iid not in due and iid in moves
                and v[0].startswith('stuck')}  # its escalation this tick ended the Stuck
    for iid, (cls, age) in sorted(dict(due, **moved_on).items()):
        if iid in ended:
            continue
        a = moves.get(iid)
        if a is not None:
            action = A.describe(a)
        else:
            action = NO_BREACH_ACTION + _why_no_breach_action(iid, cls, facts, states, judged,
                                                              queued)
        out.append({'item': iid, 'class': cls, 'age_s': age, 'action': action})
    return out


def _why_no_breach_action(iid, cls, facts, states, judged, queued):
    """Why an overdue wait of class ``cls`` gets no action this tick (one short phrase)."""
    _state, stuck = states[iid]
    if cls.startswith('stuck'):
        return 'waits on the %s' % ((stuck.owner if stuck else '') or 'operator')
    if facts.paused and cls in ('seat', 'review'):
        return 'launches paused'
    live = [s for s in facts.sessions if s.item_id == iid and s.alive]
    if live:
        return 'session %s in flight' % live[0].job
    if queued.get(iid) == 'seat':
        return 'no free seat'
    if queued.get(iid) == 'overlap':
        return 'file overlap with a Building item'
    if queued.get(iid) == 'wip':
        return 'WIP cap: open PRs over kernel.launch.max_open_prs'
    if queued.get(iid) == 'risk':
        return 'high risk: lands one at a time, after a green trunk'
    ended = [s for s in facts.sessions if s.item_id == iid and not s.alive]
    if ended and ended[0].kind == 'review':
        return ('%s %s ended — its verdict is recorded this tick, the PR is judged for landing '
                'next tick' % (ended[0].kind, ended[0].job))
    if ended:
        return ('session %s ended — its report is applied this tick, the item is judged next tick'
                % ended[0].job)
    if judged[iid].hold:
        return 'held this tick'
    return {'ci': 'CI in flight', 'merge': 'waits on GitHub auto-merge',
            'train': 'merge train full of updates in flight'}.get(cls, 'no action exists')


def limbo_reason_stuck(stuck):
    """The LIMBO reason of a Stuck item no action moves this tick."""
    return 'stuck on the %s: %s' % ((stuck.owner if stuck else '') or 'operator',
                                    R.cap(stuck.reason if stuck else '', LIMBO_REASON_MAX))


def limbo(facts, config, judged, states, parked, children, actions, queued=None, stalled=None):
    """``Plan.limbo``: ``{item id or 'PR #n': why}`` for every visible, non-derived item in
    :data:`ACTIVE` that no action this tick moves (:data:`PROGRESS`) and that nothing in flight
    will move — no live session, no CI run, no seat queue, no merge train, no GitHub auto-merge
    still within its target (``stalled``: the overdue waits the kernel had no action for) — and
    for every open kernel PR that is not its live item's PR."""
    items = facts.items
    queued = queued or {}
    stalled = stalled or {}
    moved = {_item_of(a, facts) for a in actions if isinstance(a, PROGRESS)}
    out = {}
    for iid in sorted(judged):
        if iid in parked or iid in moved or _derived(iid, items, children):
            continue
        state, stuck = states[iid]
        if state not in ACTIVE:
            continue
        why = _stalled(iid, state, stuck, facts, config, judged[iid], queued, stalled)
        if why:
            out[iid] = why
    for iid, why in sorted((facts.unreadable or {}).items()):
        out[iid] = 'card unreadable, invisible to the kernel: %s' % why
    out.update(_blocked_for_good(items, states, parked, children, out, facts.unreadable or {}))
    closed = {a.pr for a in actions if isinstance(a, (A.ClosePR, A.ArchiveAndReset))}
    for p in sorted([p for p in facts.prs if not p.merged] + list(facts.orphan_prs),
                    key=lambda p: p.number):
        if p.merged or p.number in closed or not kernel_owned(p.branch, config, p.item_id):
            continue
        it = items.get(p.item_id)
        if it is None and p.item_id in (facts.unreadable or {}):
            continue  # its card's own LIMBO line says why
        if it is None:
            out['PR #%d' % p.number] = 'its item %s is not on the record' % p.item_id
            continue
        mine = max((q.number for q in facts.prs if q.item_id == p.item_id and not q.merged),
                   default=None)
        if mine != p.number:
            out['PR #%d' % p.number] = '%s has a newer open PR #%d' % (p.item_id, mine)
    return out


def _blocked_for_good(items, states, parked, children, stalled, unreadable=()):
    """``{item: why}`` for every visible New item whose ``after:`` chain ends at a blocker that
    nothing will move: the blocker is itself in LIMBO (``stalled``), its card is unreadable or not
    on the record, or the chain is a cycle. A Done blocker (a retired card reads as Done) or a
    parked one never blocks; a blocker with its own next action (a relaunch, a session, CI) or
    waiting on a console question moves."""
    out = {}
    for iid in sorted(items):
        if (iid in parked or states.get(iid, (None,))[0] is not State.NEW
                or _derived(iid, items, children)):
            continue
        roots, cycle, seen, todo = set(), False, {iid}, list(items[iid].after)
        while todo:
            a = todo.pop()
            if not edge_open(a, items, states, parked, unreadable):
                continue
            if a not in items:
                stalled = dict(stalled, **{a: 'card unreadable' if a in unreadable
                                           else 'not on the record'})
                roots.add(a)
                continue
            if a in seen:
                cycle = cycle or a == iid
                continue
            seen.add(a)
            pending = [b for b in items[a].after
                       if edge_open(b, items, states, parked, unreadable)]
            if states[a][0] is State.NEW and pending:
                todo += pending
            else:
                roots.add(a)
        dead = sorted(r for r in roots if r in stalled)
        if dead:
            out[iid] = 'after: %s, which has no action (%s)' % (dead[0], stalled[dead[0]])
        elif cycle:
            out[iid] = 'after: a cycle back to itself'
    return out


def _stalled(iid, state, stuck, facts, config, j, queued, stalled):
    """Why item ``iid`` in ``state`` waits with nothing in flight, or '' when something is."""
    live = [s for s in facts.sessions if s.item_id == iid and s.alive]
    if state is State.STUCK:  # one on the operator waits on a console question: not LIMBO
        return '' if stuck is not None and stuck.owner == 'operator' else \
            limbo_reason_stuck(stuck)
    if state is State.BUILDING:
        return '' if live else 'Building with no live session'
    if live or j.hold or queued.get(iid) in ('seat', 'overlap', 'wip', 'risk'):
        return ''
    if state is State.READY:
        return 'Ready, launches paused' if facts.paused else 'Ready, not launched'
    if state is State.REVIEW:
        if j.review_branch is None:
            return ''  # a fresh head or a PR opened this tick: judged on the next tick's facts
        return 'Review, launches paused' if facts.paused else 'Review, no reviewer launched'
    pr = max((p for p in facts.prs if p.item_id == iid and not p.merged),
             key=lambda p: p.number, default=None)
    if pr is None:
        return 'Landing with no open PR'
    if j.updating or j.behind_pr is not None or any(c.status != 'completed' for c in pr.checks):
        return ''  # CI runs, or the merge train holds its update
    names = {c.name for c in pr.checks}
    missing = [n for n in config.required_checks if n not in names]
    if missing:
        return 'required check(s) never reported on PR #%d: %s' % (pr.number, ', '.join(missing))
    if iid in stalled:
        return 'PR #%d green with auto-merge on, not merged after %s' % (
            pr.number, W.dur(stalled[iid]['age_s']))
    return ''
