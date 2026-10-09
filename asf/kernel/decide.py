"""asf.kernel.decide — the one pure decision of the kernel (ASF 0.2).

``decide(facts, config) -> Plan`` reads nothing but its arguments and returns the state of every
item and the actions of one tick. The rules it holds, in the design's words:

- Launch: Ready items in rank order alone, while ``config.max_sessions`` allows; ``facts.paused``
  means no :class:`Launch` at all. An item whose own errors repeat is Stuck; the lane never is.
- Waits: only a declared ``after:`` edge between two visible items, and "two Building items
  with overlapping ``writes``: one at a time".
- Parked: an item whose own ``priority``, or an ancestor's (through ``parent``), is ``later`` is
  invisible everywhere: it is :attr:`State.PARKED` (Done stays Done), gets no launch, no PR upkeep
  (update, rerun, auto-merge), no answer, no mint for its landed spec, is never Stuck, and never
  counts in a ``blocked_count`` or holds anyone through ``after:`` or ``writes``.
- Review: one reviewer per PR head; the verdict is keyed by the head's tree, so a rebase with the
  same tree keeps it. ``changes`` sends the item to Ready with the findings. Every PR without a
  verdict on its head tree gets a review, a document branch (``config.doc_branches``) included.
- Landing: an approved PR gets :class:`EnableAutoMerge`; a behind one :class:`UpdateBranch`.
  Every open PR's item is in Review or Landing (or Ready on a fix round, or Stuck) — never stateless.
- Red: only ``failure``/``timed_out``. A red whose ``failing_files`` meet none of the PR's files
  gets ``config.max_reruns`` reruns, then Stuck(owner=ci). A red meeting the PR's files sends it to
  Ready (a fix round), at most ``config.max_fix_rounds``, then Stuck.
- Stuck: ``config.max_attempts`` failed attempts on one reason; a conflict the update cannot
  resolve; a session question (owner=operator); a session that ended without a push
  (owner=session, its last line as reason). A dead pid ends the session and frees its worktree.
- Record: a landed spec's declared Stories (:func:`asf.kernel.stories.declared_stories`) not on
  the record are minted; pending answers are applied at once.

How the record keeps up (the applier's side of the contract): ``Item.fix_rounds`` counts the fix
rounds already launched; a failed ``UpdateBranch`` on a conflicting PR is recorded as an attempt
whose reason starts with :data:`CONFLICT`. An item whose session ended, or whose answer is applied,
this tick is not relaunched until the next tick has read the record again.
"""
import dataclasses
import fnmatch

from asf.kernel import actions as A
from asf.kernel.model import OWNERS, RED_CONCLUSIONS, State, Stuck
from asf.kernel.stories import declared_stories

#: the item types whose state is derived from their children (when they have any)
CONTAINERS = ('epic', 'feature', 'story')

#: the item types a build session works on
BUILDABLE = ('task', 'bug')

#: the reason prefix of a failed attempt to update a conflicting PR's branch
CONFLICT = 'conflict'

#: a dead pid's attempt reason (the session never ended on its own)
CRASH = 'session died'

#: one line per owner of what happens next, unless the card already says
NEXT_ACTION = {
    'loop': 'the loop retries once its facts change',
    'session': 'a session reads the last line and fixes the cause',
    'ci': 'fix or rerun the red check outside this PR',
    'operator': 'answer on the card',
}

#: the order a plan's actions are applied in: record first, then GitHub, then launches
ORDER = (A.ApplyAnswer, A.EndSession, A.MarkStuck, A.MintStory, A.Rerun, A.UpdateBranch,
         A.EnableAutoMerge, A.Launch)


@dataclasses.dataclass
class _Judged:
    """One item's state after pass one. ``review_branch`` asks for a reviewer on that branch;
    ``branch`` is where a coder would work; ``hold`` keeps it from launching this tick."""
    state: State
    stuck: Stuck = None
    review_branch: str = None
    branch: str = None
    hold: bool = False


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
    _count_blocked(items, states, parked)

    actions += _mint(facts, parked)
    if not facts.paused:
        actions += _launches(facts, config, judged, children, states, parked)
    actions.sort(key=lambda a: ORDER.index(type(a)))
    return A.Plan(states=states, actions=actions)


# ---- pass one: each item on its own facts -------------------------------------------------------

def _judge(it, facts, config, actions):
    """``it``'s :class:`_Judged` from its own card, sessions, PRs, reviews and answers; appends the
    item's own actions (answers, PR upkeep) to ``actions``."""
    stuck = it.stuck if it.state is State.STUCK else None
    question = it.question
    hold = False
    for a in facts.answers:
        if a.item_id == it.id and a.text not in it.answers:
            actions.append(A.ApplyAnswer(it.id, a.text))
            stuck, question, hold = None, None, True

    sessions = [s for s in facts.sessions if s.item_id == it.id]
    live = [s for s in sessions if s.alive]
    attempts = list(it.attempts)
    ended_stuck = None
    for s in sessions:
        if s.alive:
            continue
        hold = True
        if not s.ended:
            attempts.append(CRASH)
        elif s.result == 'question':
            ended_stuck = _stuck(s.question or s.last_line or 'session asked a question',
                                 'operator')
        elif s.result == 'none':
            ended_stuck = _stuck(s.last_line or 'session ended without a push or a report',
                                 'session')

    prs = [p for p in facts.prs if p.item_id == it.id]
    open_pr = max((p for p in prs if not p.merged), key=lambda p: p.number, default=None)
    if open_pr is None and any(p.merged for p in prs) and not it.reopened:
        return _Judged(State.DONE)
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
        return _Judged(State.STUCK, _stuck(repeated, 'loop'))
    if open_pr is not None:
        j = _judge_pr(it, open_pr, attempts, facts, config, actions)
        j.hold = j.hold or hold
        return j
    if it.state is State.DONE and not it.reopened:
        return _Judged(State.DONE)
    if it.priority == 'later' or it.rank is None:
        return _Judged(State.NEW, hold=hold)
    return _Judged(State.READY, hold=hold)


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
        if any(a.startswith(CONFLICT) for a in attempts):
            return _Judged(State.STUCK, _stuck('%s: PR #%d still conflicts after an update'
                                               % (CONFLICT, pr.number), 'loop'))
        actions.append(A.UpdateBranch(pr.number))
        updated = True

    reds = [c for c in pr.checks if c.status == 'completed' and c.conclusion in RED_CONCLUSIONS]
    own = [c for c in reds if set(c.failing_files) & set(pr.files)]
    if own:
        return _fix_round(it, pr, 'red: %s' % ', '.join(c.name for c in own), config)
    for c in reds:
        if c.attempt - 1 < config.max_reruns:
            actions.append(A.Rerun(c.run_id))
        else:
            return _Judged(State.STUCK, _stuck('red off the PR after %d rerun(s): %s'
                                               % (c.attempt - 1, c.name), 'ci'))

    verdict = None
    for r in facts.reviews:
        if r.item_id == it.id and r.tree_sha == pr.tree_sha:
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


def _fix_round(it, pr, reason, config):
    """Ready on ``pr``'s branch for one more fix round, or Stuck(operator) past the cap."""
    if it.fix_rounds + 1 > config.max_fix_rounds:
        return _Judged(State.STUCK, _stuck('%s after %d fix rounds' % (reason, it.fix_rounds),
                                           'operator'))
    return _Judged(State.READY, branch=pr.branch)


def _repeated(attempts, limit):
    """The first reason that ``attempts`` holds at least ``limit`` times, else ``None``."""
    for reason in attempts:
        if attempts.count(reason) >= limit:
            return reason
    return None


def _stuck(reason, owner):
    assert owner in OWNERS, owner
    return Stuck(reason, owner, NEXT_ACTION[owner])


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
    wait on it through declared ``after:`` edges."""
    waiters = {}
    for iid in sorted(items):
        if not _visible(items, iid, parked):
            continue
        for a in items[iid].after:
            if _visible(items, a, parked):
                waiters.setdefault(a, []).append(iid)
    for iid, (state, stuck) in states.items():
        if state is not State.STUCK:
            continue
        seen, todo = set(), list(waiters.get(iid, []))
        while todo:
            w = todo.pop()
            if w in seen or w == iid or states[w][0] is State.DONE:
                continue
            seen.add(w)
            todo += waiters.get(w, [])
        stuck.blocked_count = len(seen)


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


def _launches(facts, config, judged, children, states, parked):
    """Reviews first, then Ready items, each in rank order, while sessions are free; a Ready item
    whose ``writes`` overlap a Building (or just launched) visible item waits its turn."""
    items = facts.items
    free = config.max_sessions - sum(1 for s in facts.sessions if s.alive)
    rank = lambda iid: (items[iid].rank is None, items[iid].rank or 0, iid)  # noqa: E731
    reviews = sorted((i for i, j in judged.items() if j.review_branch and not j.hold), key=rank)
    ready = sorted((i for i, j in judged.items()
                    if states[i][0] is State.READY and not j.hold and _visible(items, i, parked)
                    and not _derived(i, items, children)), key=rank)
    busy = [items[i].writes for i in items
            if states[i][0] is State.BUILDING and _visible(items, i, parked)]
    out = []
    for iid in reviews:
        if free <= 0:
            return out
        out.append(A.Launch('review', iid, judged[iid].review_branch))
        free -= 1
    for iid in ready:
        if free <= 0:
            break
        if any(_overlap(items[iid].writes, w) for w in busy):
            continue
        kind = _kind(items[iid], facts)
        out.append(A.Launch(kind, iid, judged[iid].branch or _branch(kind, iid, config)))
        busy.append(items[iid].writes)
        free -= 1
    return out


def _kind(it, facts):
    if it.type in BUILDABLE:
        return 'build'
    return 'plan' if it.id in facts.specs_landed else 'spec'


def _branch(kind, iid, config):
    for prefix in config.doc_branches:
        if prefix.strip('/') == kind:
            return prefix + iid
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
