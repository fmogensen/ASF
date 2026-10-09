"""asf.kernel.apply — a :class:`~asf.kernel.actions.Plan` done through the ports (ASF 0.2).

:func:`apply` walks the plan's actions in order, then writes each item's judged state to its card.
It keeps decide's side of the contract (:mod:`asf.kernel.decide`'s docstring):

- a build launched on an open PR's branch is a fix round: ``kernel_fix_rounds`` + 1, and the
  'changes' findings on that PR's tree go to the card and the brief;
- a failed :class:`~asf.kernel.actions.UpdateBranch` on a conflicting PR is an attempt whose reason
  starts with :data:`asf.kernel.decide.CONFLICT`; a failed launch is an attempt ``launch: …``;
- a session whose pid died without ending is an attempt :data:`asf.kernel.decide.CRASH`.

Idempotent: a state already on the card is not rewritten, a Stuck already recorded keeps its
``since``, a Story already on the record is not minted, and a launch for an item a live session
already holds is skipped. One action's failure is logged in the result and never stops the rest;
one card's failed write is logged the same way.
"""
import dataclasses

from asf.kernel import actions as A
from asf.kernel import ports as P
from asf.kernel.decide import CONFLICT, CONTAINERS, CRASH
from asf.kernel.model import State


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
    return repr(action)


class _Applier:

    def __init__(self, plan, facts, ports, now, log):
        self.plan, self.facts, self.ports, self.now, self.log = plan, facts, ports, now, log
        self.updates = {}   # item id -> {machine key: value}
        self.result = Result()

    def field(self, iid, key, default):
        """The value ``key`` will have on ``iid``'s card: this tick's update, else the card's."""
        if key in self.updates.get(iid, {}):
            return self.updates[iid][key]
        it = self.facts.items.get(iid)
        if it is None:
            return default
        return {P.ATTEMPTS: list(it.attempts), P.FIX_ROUNDS: it.fix_rounds,
                P.ANSWERS: list(it.answers)}.get(key, default)

    def set(self, iid, **fields):
        self.updates.setdefault(iid, {}).update(fields)

    def attempt(self, iid, reason):
        self.set(iid, **{P.ATTEMPTS: self.field(iid, P.ATTEMPTS, []) + [reason]})

    # ---- one method per action type -------------------------------------------------------

    def ApplyAnswer(self, a):
        answers = self.field(a.item_id, P.ANSWERS, [])
        if a.text not in answers:
            self.set(a.item_id, **{P.ANSWERS: answers + [a.text], P.QUESTION: None})

    def EndSession(self, a):
        s = next((s for s in self.facts.sessions if s.job == a.job), None)
        if s is None:
            return 'not in the facts'
        if not s.ended:
            self.attempt(s.item_id, CRASH)
        self.ports.sessions.end(s, a.free_worktree)

    def MarkStuck(self, a):
        pass  # written with the item's state below (one card write per item)

    def MintStory(self, a):
        self.ports.record.mint_story(a.feature_id, a.story_id, a.title, a.acceptance)

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
                        if r.item_id == a.item_id and r.tree_sha == pr.tree_sha
                        and r.verdict != 'approve' for f in r.findings]
        try:
            job = self.ports.sessions.launch(a.kind, a.item_id, a.branch,
                                             brief(it, a, findings, pr))
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
        for iid, (state, stuck) in self.plan.states.items():
            it = self.facts.items.get(iid)
            if it is None or (it.type in CONTAINERS and iid in parents):
                continue
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


def brief(it, launch, findings=(), pr=None):
    """The brief one session gets: the item, the branch, and what this round must answer."""
    lines = ['# %s %s — %s' % (launch.kind, it.id, it.title), '',
             'Item: %s (%s). Branch: `%s`.' % (it.id, it.type, launch.branch)]
    if pr is not None:
        lines.append('Open PR: #%d (head %s).' % (pr.number, pr.head_sha[:12]))
    if launch.kind == 'review':
        lines.append('Review the PR head; record one verdict (approve or changes, with findings) '
                     'keyed by the head tree.')
    elif launch.kind == 'build':
        lines.append('Work only within the declared writes: %s. Push the branch once, at the end.'
                     % (', '.join(it.writes) or '(none declared)'))
    else:
        lines.append('Write the %s document for %s on this branch and push it.' % (launch.kind, it.id))
    if findings:
        lines += ['', '## Findings to answer'] + ['- %s' % f for f in findings]
    if it.answers:
        lines += ['', '## Operator answers'] + ['- %s' % a for a in it.answers]
    return '\n'.join(lines) + '\n'
