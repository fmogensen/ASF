"""asf.kernel.decide — the one pure decision of the kernel (ASF 0.2).

``decide(facts, config) -> Plan`` reads nothing but its arguments and returns the state of every
item and the actions of one tick. The rules it holds, in the design's words:

- New and Ready: an item is Ready once nothing else holds it and it is ranked — its own ``rank``,
  else its nearest ancestor's through ``parent`` (Story, Feature, Epic). A Task or Bug whose whole
  lineage is unranked is Ready too, after every ranked item: no rank never means never. Only an
  unranked Feature or Epic (and a declared ``after:`` edge, or ``later``) keeps an item New.
- Launch: Ready items in launch order alone — (effective rank, own rank, id), unranked last — while ``config.max_sessions`` allows; ``facts.paused``
  means no :class:`Launch` at all. An item whose own errors repeat is Stuck; the lane never is.
- Waits: only a declared ``after:`` edge between two visible items, and "two Building items
  with overlapping ``writes``: one at a time".
- Parked: an item whose own ``priority``, or an ancestor's (through ``parent``), is ``later`` is
  invisible everywhere: it is :attr:`State.PARKED` (Done stays Done), gets no launch, no PR upkeep
  (update, rerun, auto-merge), no answer, no mint for its landed spec, is never Stuck, and never
  counts in a ``blocked_count`` or holds anyone through ``after:`` or ``writes``.
- Review: one reviewer per PR head; the verdict is keyed by the head's tree and by the PR's own
  change (``change_id``), and holds when either matches: a rebase with the same tree keeps it, and
  so does an update that merges trunk in (the change is the same). ``changes`` sends the item to
  Ready with the findings. Every PR without a verdict on its head gets a review, a document branch
  (``config.doc_branches``) included.
- Landing: an approved PR gets :class:`EnableAutoMerge`; a behind one :class:`UpdateBranch`.
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
- Conflict: a conflicting PR first gets :class:`UpdateBranch`; when that fails, one fix round —
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
  (``Session.push_refused``) is Stuck(owner=operator) on that reason. A ``done`` session
  that pushed a new head moves on (to Review) whatever ``NEEDS OPERATOR:`` line it also carries:
  the question becomes an item note (:class:`NoteItem`, shown by status) and holds nothing. A session whose API
  failed is an attempt :data:`API_FAILED` — relaunched, then Stuck(owner=loop). A dead pid ends
  the session and frees its worktree. No Stuck reason is ever empty, a code fence or noise
  (:func:`asf.kernel.reports.meaningful`); one recorded that way is judged afresh.
- Documents: only a Feature with no children launches ``spec`` (``plan`` once its spec has
  landed); a Story or Epic with no children is New and never launches.
- Rank: ``config.rank`` ``inherit`` (the default) walks ``parent`` for a rank; ``own`` reads
  only the item's own.
- Idle: when ``config.idle_alarm`` is on, nothing launches, at least ``config.idle_min_free``
  seats are free and visible Tasks/Bugs wait (New or Ready), the plan carries ``idle``: the free
  seats, how many wait, and the top three reasons with counts (:data:`IDLE_REASONS`).
- Record: a landed spec's declared Stories (:func:`asf.kernel.stories.declared_stories`) not on
  the record are minted; pending answers are applied at once.

How the record keeps up (the applier's side of the contract): ``Item.fix_rounds`` counts the fix
rounds already launched; a failed ``UpdateBranch`` on a conflicting PR is recorded as an attempt
whose reason starts with :data:`CONFLICT`; a fix round's findings (a :data:`REBASE` one
included) are written to ``Item.findings`` when it launches. An item whose session ended, or whose answer is applied,
this tick is not relaunched until the next tick has read the record again.
"""
import dataclasses
import fnmatch

from asf.kernel import actions as A
from asf.kernel import reports as R
from asf.kernel.model import OWNERS, RED_CONCLUSIONS, State, Stuck, verdict_holds
from asf.kernel.stories import declared_stories

#: the item types whose state is derived from their children (when they have any)
CONTAINERS = ('epic', 'feature', 'story')

#: the item types a build session works on
BUILDABLE = ('task', 'bug')

#: the one item type a document lane (spec, then plan once its spec landed) launches for: a
#: childless Story or Epic is New, never a spec
DOCUMENTED = 'feature'

#: the reason prefix of a failed attempt to update a conflicting PR's branch
CONFLICT = 'conflict'

#: the prefix of the finding a rebase round carries (on the Launch, then on ``Item.findings``)
REBASE = 'rebase'

#: what a rebase round's session does, after the finding's prefix
REBASE_ASK = ("rebase the branch onto the base, resolve the conflicts, keep the change's intent, "
              "run the touched tests, push")

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
}

#: the order a plan's actions are applied in: record first, then GitHub, then launches
ORDER = (A.ApplyAnswer, A.EndSession, A.NoteItem, A.MarkStuck, A.MintStory, A.OpenPR, A.Rerun,
         A.UpdateBranch, A.EnableAutoMerge, A.Launch)

#: the prefix of a Stuck reason a ``done`` REPORT left (its ``NEEDS OPERATOR:`` question, before
#: a done-and-pushed session moved on): with its branch pushed it is re-judged to an OpenPR
DONE_STUCK = R.DONE + ':'

#: the prefix of the Stuck reason a ``done`` REPORT whose push the kernel did not see left: when
#: its ``pushed:`` value claims a push and the branch is on origin it is re-judged to an OpenPR
NO_PUSH_STUCK = 'done without a push: pushed: '

#: the prefix of the Stuck reason of a required red off the PR that used up its reruns: judged
#: afresh every tick (the check may not be required, or its files may not be known)
RED_OFF = 'red off the PR after '

#: the prefix of a red-driven fix round's finding (:func:`red_finding`)
RED = 'red'

#: the most characters of a failed log's tail a finding carries
LOG_TAIL_MAX = 1500

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


def decide(facts, config):
    """Return the :class:`asf.kernel.actions.Plan` for ``facts`` (:class:`asf.kernel.model.Facts`)
    under ``config`` (:class:`asf.kernel.model.Config`). Pure: same arguments, same plan."""
    items = facts.items
    actions = []
    for s in facts.sessions:
        if not s.alive:
            actions.append(A.EndSession(s.job, free_worktree=True))

    parked = _parked(items)
    judged = {iid: (_park(items[iid], facts, config) if iid in parked
                    else _judge(items[iid], facts, config, actions)) for iid in sorted(items)}
    for iid, j in judged.items():
        old = items[iid]
        if j.state is State.STUCK and (old.state is not State.STUCK or old.stuck != j.stuck):
            actions.append(A.MarkStuck(iid, j.stuck.reason, j.stuck.owner))

    children = _children(items)
    states = {}
    for iid in sorted(items):
        _state_of(iid, items, judged, children, states, (), parked)
    _apply_waits(items, judged, children, states, parked)
    blocks = _count_blocked(items, states, parked)

    actions += _mint(facts, parked)
    launches, skipped = (([], {}) if facts.paused
                         else _launches(facts, config, judged, children, states, parked, blocks))
    actions += launches
    actions.sort(key=lambda a: ORDER.index(type(a)))
    idle = _idle(facts, config, judged, states, parked, skipped) if not launches else None
    return A.Plan(states=states, actions=actions, idle=idle)


# ---- pass one: each item on its own facts -------------------------------------------------------

def _judge(it, facts, config, actions):
    """``it``'s :class:`_Judged` from its own card, sessions, PRs, reviews and answers; appends the
    item's own actions (answers, PR upkeep) to ``actions``."""
    stuck = (it.stuck if it.state is State.STUCK and not _legacy_conflict(it.stuck)
             and not _red_off(it.stuck) and R.meaningful(it.stuck.reason) else None)
    question = it.question
    hold = False
    for a in facts.answers:
        if a.item_id == it.id and a.text not in it.answers:
            actions.append(A.ApplyAnswer(it.id, a.text))
            stuck, question, hold = None, None, True

    sessions = [s for s in facts.sessions if s.item_id == it.id]
    live = [s for s in sessions if s.alive]
    attempts = list(it.attempts)
    prs = [p for p in facts.prs if p.item_id == it.id]
    open_pr = max((p for p in prs if not p.merged), key=lambda p: p.number, default=None)
    ended_stuck, api_detail, pushed = None, '', None
    for s in sessions:
        if s.alive:
            continue
        hold = True
        if not s.ended:
            attempts.append(CRASH)
        elif s.kind == 'review':
            continue  # a reviewer's verdict is read off its report by the applier
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
            ended_stuck = _ended_stuck(s, open_pr) or ended_stuck

    if open_pr is None and any(p.merged for p in prs) and not it.reopened:
        return _Judged(State.DONE)
    if open_pr is None and not live and not question:
        branch = _pr_branch(it, sessions, pushed, stuck, ended_stuck, facts, config)
        if branch:
            actions.append(open_pr_action(it, branch, pushed))
            return _Judged(State.REVIEW, hold=True)
    if stuck:
        return _Judged(State.STUCK, _keep(stuck))
    if ended_stuck:
        return _Judged(State.STUCK, ended_stuck)
    if question:
        return _Judged(State.STUCK, _stuck(question, 'operator'))
    if live:
        builds = [s for s in live if s.kind != 'review']
        return _Judged(State.BUILDING if builds else State.REVIEW, hold=True)
    repeated = _repeated(attempts, config.max_attempts)
    if repeated:
        if repeated == API_FAILED and api_detail:
            repeated = '%s: %s' % (API_FAILED, api_detail)
        return _Judged(State.STUCK, _stuck(repeated, 'loop'))
    if open_pr is not None:
        j = _judge_pr(it, open_pr, attempts, facts, config, actions)
        j.hold = j.hold or hold
        return j
    if it.state is State.DONE and not it.reopened:
        return _Judged(State.DONE)
    if it.priority == 'later' or (effective_rank(it.id, facts.items, config.rank != 'own') is None
                                  and it.type not in BUILDABLE):
        return _Judged(State.NEW, hold=hold)
    if it.type not in BUILDABLE and it.type != DOCUMENTED:
        return _Judged(State.NEW, hold=hold)  # only a Feature gets a spec or plan launch
    return _Judged(State.READY, hold=hold)


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


def host_pushes(s):
    """Whether the host pushes ended session ``s``'s worktree HEAD (``Session.unpushed``) with a
    lease before it is ended: a ``done``, non-review session that pushed nothing itself and whose
    origin branch holds nothing its history never had."""
    return (s.ended and s.kind != 'review' and s.status == R.DONE and s.result != 'pushed'
            and bool(s.unpushed) and not s.push_refused)


def no_report(s):
    """Whether ended session ``s`` stopped without a REPORT (and without a question, an API
    failure or a push of its own)."""
    return (s.ended and not s.fields and not s.status and not s.question and not s.api_error
            and s.result not in ('pushed', 'question'))


def relaunch(it):
    """Whether ``it``'s last attempt is a session that ended without a REPORT: relaunched first."""
    return bool(it.attempts) and it.attempts[-1] == NO_REPORT


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


def _judge_pr(it, pr, attempts, facts, config, actions):
    """The state of ``it`` holding open PR ``pr``: conflict, red checks, then the review."""
    updated = False
    if pr.conflicting:
        if any(rebase_finding(f, pr.number) for f in it.findings):
            reason = ('%s the rebase session could not resolve: PR #%d still conflicts on head %s'
                      % (CONFLICT, pr.number, pr.head_sha or '?'))
            return _Judged(State.STUCK, Stuck(reason, 'operator', CONFLICT_NEXT % pr.number))
        if any(a.startswith(CONFLICT) for a in attempts):
            return _fix_round(it, pr, '%s: PR #%d' % (CONFLICT, pr.number), config,
                              [rebase_finding_for(pr.number)])
        actions.append(A.UpdateBranch(pr.number))
        updated = True

    reds = [c for c in pr.checks if c.status == 'completed' and c.conclusion in RED_CONCLUSIONS]
    gating = [c for c in reds if required(c.name, config)]
    for c in reds:
        if c not in gating:
            _note(it, ignored_red_note(pr, c), actions)
    own = [c for c in gating if on_pr(c, pr)]
    if own:
        return _fix_round(it, pr, '%s: %s' % (RED, ', '.join(c.name for c in own)), config,
                          [red_finding(c) for c in own])
    for c in gating:
        if c.attempt - 1 < config.max_reruns:
            actions.append(A.Rerun(c.run_id))
        else:
            return _Judged(State.STUCK, _stuck('%s%d rerun(s): %s'
                                               % (RED_OFF, c.attempt - 1, c.name), 'ci'))

    verdict = None
    for r in facts.reviews:
        if r.item_id == it.id and verdict_holds(r, pr):
            verdict = r
    if verdict is None:
        return _Judged(State.REVIEW, review_branch=pr.branch)
    if verdict.verdict != 'approve':
        return _fix_round(it, pr, 'review: changes requested', config)
    if not pr.auto_merge:
        actions.append(A.EnableAutoMerge(pr.number))
    if pr.behind and not pr.conflicting and not updated:
        actions.append(A.UpdateBranch(pr.number))
    return _Judged(State.LANDING)


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


def _fix_round(it, pr, reason, config, findings=()):
    """Ready on ``pr``'s branch for one more fix round (carrying ``findings`` into the launch),
    or Stuck(operator) past the cap."""
    if it.fix_rounds + 1 > config.max_fix_rounds:
        return _Judged(State.STUCK, _stuck('%s after %d fix rounds' % (reason, it.fix_rounds),
                                           'operator'))
    return _Judged(State.READY, branch=pr.branch, findings=list(findings))


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
    """The first reason that ``attempts`` holds at least ``limit`` times, else ``None``."""
    for reason in attempts:
        if attempts.count(reason) >= limit:
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
    """``{parent_id: [child ids]}`` from the ``parent`` link only."""
    out = {}
    for iid in sorted(items):
        parent = items[iid].parent
        if parent in items:
            out.setdefault(parent, []).append(iid)
    return out


def _derived(iid, items, children):
    return items[iid].type in CONTAINERS and bool(children.get(iid))


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


def _apply_waits(items, judged, children, states, parked):
    """A Ready item with a declared ``after:`` edge to a visible item that is not Done waits (New);
    re-derive the containers above it."""
    changed = False
    for iid in sorted(items):
        it = items[iid]
        if states[iid][0] is not State.READY or _derived(iid, items, children):
            continue
        if any(_visible(items, a, parked) and states[a][0] is not State.DONE for a in it.after):
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


def _launches(facts, config, judged, children, states, parked, blocks=None):
    """``(launches, skipped)``: reviews first, then Ready items, each in launch order
    (:func:`launch_order`) — a relaunch (:func:`relaunch`) before the rest, the one holding the
    most others (``blocks``) first — while sessions are free; a Ready item whose ``writes`` overlap a
    Building (or just launched) visible item waits its turn (``skipped[iid] = 'overlap'``)."""
    items = facts.items
    free = _free(facts, config)
    inherit = config.rank != 'own'
    blocks = blocks or {}
    rank = lambda iid: launch_order(iid, items, inherit)  # noqa: E731
    first = lambda iid: ((0, -blocks.get(iid, 0)) if relaunch(items[iid]) else (1, 0),  # noqa: E731
                         rank(iid))
    reviews = sorted((i for i, j in judged.items() if j.review_branch and not j.hold), key=rank)
    ready = sorted((i for i, j in judged.items()
                    if states[i][0] is State.READY and not j.hold and _visible(items, i, parked)
                    and not _derived(i, items, children)), key=first)
    busy = [items[i].writes for i in items
            if states[i][0] is State.BUILDING and _visible(items, i, parked)]
    out, skipped = [], {}
    for iid in reviews:
        if free <= 0:
            return out, skipped
        out.append(A.Launch('review', iid, judged[iid].review_branch))
        free -= 1
    for iid in ready:
        if free <= 0:
            break
        if any(_overlap(items[iid].writes, w) for w in busy):
            skipped[iid] = 'overlap'
            continue
        kind = _kind(items[iid], facts)
        out.append(A.Launch(kind, iid, judged[iid].branch or _branch(kind, iid, config, items[iid]),
                            list(judged[iid].findings)))
        busy.append(items[iid].writes)
        free -= 1
    return out, skipped


def _free(facts, config):
    return config.max_sessions - sum(1 for s in facts.sessions if s.alive)


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
            why = ('after' if any(_visible(items, a, parked) and states[a][0] is not State.DONE
                                  for a in it.after) else 'new')
        elif facts.paused:
            why = 'paused'
        else:
            why = skipped.get(iid) or ('held' if judged[iid].hold else 'new')
        reasons[IDLE_REASONS[why]] = reasons.get(IDLE_REASONS[why], 0) + 1
    if not reasons:
        return None
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return {'free': free, 'waiting': sum(reasons.values()), 'reasons': top}


def _kind(it, facts):
    if it.type in BUILDABLE:
        return 'build'
    return 'plan' if it.id in facts.specs_landed else 'spec'


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
