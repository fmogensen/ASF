"""asf.kernel.apply — a :class:`~asf.kernel.actions.Plan` done through the ports (ASF 0.2).

:func:`apply` walks the plan's actions in order, then writes each item's judged state to its card.
It keeps decide's side of the contract (:mod:`asf.kernel.decide`'s docstring):

- a build launched on an open PR's branch is a fix round: ``kernel_fix_rounds`` + 1, and the
  'changes' findings on that PR's tree, plus the launch's own (a rebase round's), go to the card
  and the brief;
- a failed :class:`~asf.kernel.actions.UpdateBranch` on a conflicting PR is an attempt whose reason
  starts with :data:`asf.kernel.decide.CONFLICT`; a failed launch is an attempt ``launch: …``;
- a session whose pid died without ending is an attempt :data:`asf.kernel.decide.CRASH`; one whose
  API failed before it reported is an attempt :data:`asf.kernel.decide.API_FAILED`;
- an ended session that reported ``pushed: rebased <sha>`` (the floor's wording: "the factory
  publishes") and pushed nothing, or a ``done`` one whose worktree holds commits origin lacks
  (:func:`asf.kernel.decide.host_pushes`: a rebase the session's sandbox would not force-push,
  a push its hook timed out on), is pushed once by the host (``sessions.push_rebase``: the
  worktree's HEAD must be that sha; ``--force-with-lease`` over origin's tip, only when that
  tip is in the branch's own history or patch-equivalent to it; the push recorded on its push
  log) before the session is ended — a kernel session pushes its own branch, and this is the
  safety net; a refused push keeps the worktree, skips the item's OpenPR and leaves it
  Stuck(owner=operator) on the refusal;
- a session that ended without a REPORT is an attempt :data:`asf.kernel.decide.NO_REPORT` and
  its worktree is kept: the relaunch continues on it;
- a review session that ended has its report's verdict lines
  (:func:`asf.kernel.briefs.parse_verdict`) recorded on the review ledger keyed by the tree and
  the PR change it was launched on — or, when it printed none, an attempt :data:`NO_VERDICT`.

Every brief comes from the ports' brief maker (:class:`asf.kernel.briefs.Briefer` on the real
ports): the floor's brief builder, never text of the kernel's own.

Idempotent: a state already on the card is not rewritten, a Stuck already recorded keeps its
``since``, a Story already on the record is not minted, and a launch for an item a live session
already holds is skipped. One action's failure is logged in the result and never stops the rest;
one card's failed write is logged the same way.
"""
import dataclasses
import re

from asf.kernel import actions as A
from asf.kernel import ports as P
from asf.kernel import reports as R
from asf.kernel.briefs import parse_verdict
from asf.kernel.decide import (API_FAILED, CONFLICT, CONTAINERS, CRASH, NEXT_ACTION, NO_REPORT,
                               NOT_PUSHED, host_pushes, no_report)
from asf.kernel.model import State, Stuck, verdict_holds

#: the attempt a review session that ended without a ``VERDICT:`` line records
NO_VERDICT = 'review: no VERDICT line'

#: a REPORT's ``pushed: rebased <sha> …`` — the floor's "the factory publishes" line
REBASED_RE = re.compile(r'^\s*rebased\s+([0-9a-fA-F]{7,40})\b')


def rebased_sha(s):
    """The sha an ended, non-review session reported as ``pushed: rebased <sha>`` and left
    to the factory, else '' (the port skips a sha its push log already holds)."""
    if not s.ended or s.kind == 'review':
        return ''
    m = REBASED_RE.match(str((s.fields or {}).get('pushed') or ''))
    return m.group(1).lower() if m else ''


def host_push_sha(s):
    """The sha the host pushes for ended session ``s`` before ending it, else '': a reported
    ``rebased <sha>``, else the worktree HEAD a ``done`` session left unpushed
    (:func:`asf.kernel.decide.host_pushes`)."""
    return rebased_sha(s) or (s.unpushed.lower() if host_pushes(s) else '')


@dataclasses.dataclass
class Result:
    """What one apply did: ``done`` and ``failed`` hold ``(action, note)`` pairs; ``written`` the
    item ids whose card changed."""
    done: list = dataclasses.field(default_factory=list)
    failed: list = dataclasses.field(default_factory=list)
    written: list = dataclasses.field(default_factory=list)


def describe(action):
    """One line naming ``action``."""
    if isinstance(action, A.Launch):
        return 'launch %s %s on %s' % (action.kind, action.item_id, action.branch)
    if isinstance(action, A.EnableAutoMerge):
        return 'auto-merge #%d' % action.pr
    if isinstance(action, A.UpdateBranch):
        return 'update-branch #%d' % action.pr
    if isinstance(action, A.OpenPR):
        return 'open PR %s for %s: %s' % (action.branch, action.item_id, action.title)
    if isinstance(action, A.Rerun):
        return 'rerun run %s' % action.run_id
    if isinstance(action, A.MintStory):
        return 'mint %s under %s: %s' % (action.story_id, action.feature_id, action.title)
    if isinstance(action, A.MarkStuck):
        return 'stuck %s (%s): %s' % (action.item_id, action.owner, action.reason)
    if isinstance(action, A.EndSession):
        return 'end session %s%s' % (action.job, ' + free worktree' if action.free_worktree else '')
    if isinstance(action, A.ApplyAnswer):
        return 'answer %s' % action.item_id
    if isinstance(action, A.NoteItem):
        return 'note %s: %s' % (action.item_id, action.text)
    return repr(action)


class _Applier:

    def __init__(self, plan, facts, ports, now, log):
        self.plan, self.facts, self.ports, self.now, self.log = plan, facts, ports, now, log
        self.updates = {}   # item id -> {machine key: value}
        self.refused = {}   # item id -> why the host did not push its session's work
        self.result = Result()

    def field(self, iid, key, default):
        """The value ``key`` will have on ``iid``'s card: this tick's update, else the card's."""
        if key in self.updates.get(iid, {}):
            return self.updates[iid][key]
        it = self.facts.items.get(iid)
        if it is None:
            return default
        return {P.ATTEMPTS: list(it.attempts), P.FIX_ROUNDS: it.fix_rounds,
                P.ANSWERS: list(it.answers), P.NOTES: list(it.notes)}.get(key, default)

    def set(self, iid, **fields):
        self.updates.setdefault(iid, {}).update(fields)

    def attempt(self, iid, reason):
        self.set(iid, **{P.ATTEMPTS: self.field(iid, P.ATTEMPTS, []) + [reason]})

    # ---- one method per action type -------------------------------------------------------

    def ApplyAnswer(self, a):
        answers = self.field(a.item_id, P.ANSWERS, [])
        if a.text not in answers:
            self.set(a.item_id, **{P.ANSWERS: answers + [a.text], P.QUESTION: None})

    def NoteItem(self, a):
        notes = self.field(a.item_id, P.NOTES, [])
        if a.text not in notes:
            self.set(a.item_id, **{P.NOTES: notes + [a.text]})

    def EndSession(self, a):
        s = next((s for s in self.facts.sessions if s.job == a.job), None)
        if s is None:
            return 'not in the facts'
        if not s.ended:
            self.attempt(s.item_id, CRASH)
        elif s.kind == 'review':
            self.verdict(s)
        elif s.api_error and not s.fields:
            self.attempt(s.item_id, API_FAILED)
        free, note = a.free_worktree, None
        if s.ended and s.kind != 'review' and no_report(s):
            self.attempt(s.item_id, NO_REPORT)
            free = False  # the relaunch continues on its worktree and branch
        sha = host_push_sha(s)
        if sha:
            try:
                note = self.ports.sessions.push_rebase(s, sha)
            except Exception as e:  # the worktree is kept: its commits are the work
                why = str(e) or type(e).__name__
                free, note = False, 'rebase not pushed: %s' % why
                if host_pushes(s):
                    self.refused[s.item_id] = why
            self.log('%s %s — %s' % (s.job, s.item_id, note))
        self.ports.sessions.end(s, free)
        return note

    def verdict(self, s):
        """Record an ended review session's verdict on the ledger, keyed by the tree and the PR
        change it read."""
        got = parse_verdict(s.report)
        pr = next((p for p in self.facts.prs if p.item_id == s.item_id and not p.merged
                   and (s.pr is None or p.number == s.pr)), None)
        tree = s.tree_sha or (pr.tree_sha if pr else '')
        # the change read with the tree the session was launched on; a session launched before
        # the change was kept takes the PR's only while its tree is still the one reviewed
        same = pr is not None and (not s.tree_sha or s.tree_sha == pr.tree_sha)
        change = s.change_id or (pr.change_id if same else '')
        if got is None or not tree:
            self.attempt(s.item_id, NO_VERDICT)
            return
        self.ports.record.record_review(s.item_id, s.pr or (pr.number if pr else None), tree,
                                        got[0], got[1], change)

    def MarkStuck(self, a):
        pass  # written with the item's state below (one card write per item)

    def MintStory(self, a):
        self.ports.record.mint_story(a.feature_id, a.story_id, a.title, a.acceptance)

    def OpenPR(self, a):
        if a.item_id in self.refused:
            return 'skipped: the host push failed'
        return 'PR #%s' % self.ports.github.open_pr(a.branch, a.base, a.title, a.body)

    def Rerun(self, a):
        self.ports.github.rerun(a.run_id)

    def UpdateBranch(self, a):
        pr = next((p for p in self.facts.prs if p.number == a.pr and not p.merged), None)
        try:
            self.ports.github.update_branch(a.pr)
        except Exception as e:
            if pr is not None and pr.conflicting:
                self.attempt(pr.item_id, '%s: PR #%d: %s' % (CONFLICT, a.pr, e))
            raise

    def EnableAutoMerge(self, a):
        self.ports.github.enable_auto_merge(a.pr)

    def Launch(self, a):
        if any(s.item_id == a.item_id and s.alive and (s.kind == 'review') == (a.kind == 'review')
               for s in self.facts.sessions):
            return 'skipped: a live session holds it'
        it = self.facts.items[a.item_id]
        pr = next((p for p in self.facts.prs
                   if p.item_id == a.item_id and not p.merged and p.branch == a.branch), None)
        fix = a.kind == 'build' and pr is not None
        findings = []
        if fix:
            findings = [f for r in self.facts.reviews
                        if r.item_id == a.item_id and verdict_holds(r, pr)
                        and r.verdict != 'approve' for f in r.findings]
            findings += [f for f in a.findings if f not in findings]
        meta = ({'pr': pr.number, 'tree': pr.tree_sha, 'change': pr.change_id}
                if pr is not None else {})
        try:
            if self.ports.brief is None:
                raise P.PortError('no brief maker on the ports')
            job = self.ports.sessions.launch(a.kind, a.item_id, a.branch,
                                             self.ports.brief(it, a, findings, pr), meta)
        except Exception as e:
            self.attempt(a.item_id, 'launch: %s' % e)
            raise
        if fix:
            self.set(a.item_id, **{P.FIX_ROUNDS: self.field(a.item_id, P.FIX_ROUNDS, 0) + 1,
                                   P.FINDINGS: findings})
        self.set(a.item_id, **{P.STATE: (State.REVIEW if a.kind == 'review'
                                         else State.BUILDING).value})
        return 'job %s' % job

    # ---- the walk -------------------------------------------------------------------------

    def run(self):
        for action in self.plan.actions:
            name = type(action).__name__
            try:
                note = getattr(self, name)(action)
            except Exception as e:  # one action's failure never stops the rest
                self.result.failed.append((action, str(e) or type(e).__name__))
                self.log('FAILED %s — %s' % (describe(action), e))
                continue
            self.result.done.append((action, note or ''))
            self.log('%s%s' % (describe(action), ' — %s' % note if note else ''))
        self.states()
        self.write()
        return self.result

    def states(self):
        """Each judged item's state and Stuck, onto this tick's updates where the card differs.
        A container with children is derived every tick and never stored."""
        parents = {it.parent for it in self.facts.items.values()}
        states = dict(self.plan.states)
        for iid, why in self.refused.items():  # decide counted on a push that did not happen
            if iid in states:
                states[iid] = (State.STUCK, Stuck(reason=R.cap(NOT_PUSHED + why), owner='operator',
                                                  next_action=NEXT_ACTION['operator']))
        for iid, (state, stuck) in states.items():
            it = self.facts.items.get(iid)
            if it is None or state is State.PARKED or (it.type in CONTAINERS and iid in parents):
                continue  # derived every tick, never stored
            upd = self.updates.get(iid, {})
            if P.STATE in upd:   # a launch this tick moved it already
                continue
            fields = {}
            if state is not it.state:
                fields[P.STATE] = state.value
            if state is State.STUCK:
                old = it.stuck if it.state is State.STUCK else None
                if old is None or (old.reason, old.owner) != (stuck.reason, stuck.owner):
                    fields.update({P.STATE: state.value, P.STUCK_REASON: stuck.reason,
                                   P.STUCK_OWNER: stuck.owner, P.STUCK_NEXT: stuck.next_action,
                                   P.STUCK_SINCE: self.now})
            elif it.state is State.STUCK:
                fields.update({P.STUCK_REASON: None, P.STUCK_OWNER: None, P.STUCK_NEXT: None,
                               P.STUCK_SINCE: None})
            if fields:
                self.set(iid, **fields)

    def write(self):
        for iid in sorted(self.updates):
            try:
                self.ports.record.write_fields(iid, self.updates[iid])
            except Exception as e:  # one card's failure never stops the rest
                self.result.failed.append((('write', iid), str(e) or type(e).__name__))
                self.log('FAILED write %s — %s' % (iid, e))
                continue
            self.result.written.append(iid)


def apply(plan, facts, ports, now=None, log=print):
    """Do ``plan`` (decided on ``facts``) through ``ports``; return a :class:`Result`."""
    return _Applier(plan, facts, ports, now or P.now_iso(), log).run()

