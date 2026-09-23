"""asf.evidence.closing — the definition of done, one table, total.

Every item's state comes from exactly one rule of RULES, and RULES is exhaustive: for any
(type, evidence) pair `state_of` returns a Closing naming the rule that decided it, NO_RULE
included. `asf.record.ingest` matches items to evidence; it derives no state of its own.

Pure: no git, no clock, no import from `asf`. Durations arrive as seconds on `Ev`.

The rules, first match per type after the two `any` rows:

  any      reconciled         a typed landed sha and green CI                    -> Closed
  any      parent-closed      the parent is Closed and the item has no evidence  -> Closed
  task     landed-green       a trunk commit or merged PR, and green CI          -> Closed
  task     landed             a trunk commit or merged PR                        -> Resolved
  task     in-flight          a branch or a PR                                   -> Active
  task     planned            otherwise                                          -> New
  story    tasks-closed       its Tasks all Closed, and in prod                  -> Closed
  story    tasks-resolved     its Tasks all Resolved or Closed                   -> Resolved
  story    matrix-done        the plan's matrix says done                        -> Closed
  story    matrix-doing       the plan's matrix says doing                       -> Resolved
  story    task-active        a Task Active, or a child with evidence            -> Active
  story    (else)             no-rule, the state it had is carried
  bug      quiet              fix merged, CI green, signature unseen past the limit
                              (or no signature)                                  -> Closed
  bug      fixed              a fix merged                                       -> Resolved
  bug      fixer              a fixer branch or PR                               -> Active
  bug      filed              otherwise                                          -> New
  feature  children-closed    its children all Closed, and in prod               -> Closed
  feature  children-resolved  its children all Resolved or Closed                -> Resolved
  feature  landed-green       no children, a naming commit, green, in prod       -> Closed
  feature  landed             no children, a naming commit                       -> Resolved
  feature  documented         a spec on main or an approved plan                 -> Active
  feature  card               otherwise                                          -> New
  epic     children-closed    its children all Closed                            -> Closed
  epic     typed-closed       the operator typed `closed: true` (the early close) -> Closed
  epic     children-resolved  its children all Resolved or Closed                -> Resolved
  epic     child-active       a child Active                                     -> Active
  epic     typed              otherwise                                          -> New
"""
import dataclasses

NEW, ACTIVE, RESOLVED, CLOSED = 'New', 'Active', 'Resolved', 'Closed'
NO_RULE = 'no-rule'          #: named residue — reconciliation's input, and `asf status`' count
BUG_QUIET_DEFAULT = '3d'     #: stage_limits.bug_quiet when a product sets none

_DONE = (RESOLVED, CLOSED)


@dataclasses.dataclass(frozen=True)
class Closing:
    state: str                #: New | Active | Resolved | Closed
    rule: str                 #: the clause that decided it — written into evidence: and History
    lines: tuple = ()         #: the evidence lines, as ingest writes them today


@dataclasses.dataclass
class Ev:
    """What a rule is handed. Null per field, so a test states exactly the evidence it means."""
    commit: str = ''          #: a commit on the trunk naming it (evidence.id_evidence)
    green: bool = False       #: CI green at or after that commit; True when the product has no CI
    merged_sha: str = ''      #: a PR of its own, merged
    branch: str = ''
    pr_state: str = ''
    open_prs: tuple = ()
    children: tuple = ()      #: the derived states of its own children
    child_evidence: bool = False   #: any child carries a branch, an open PR or an Active state
    spec_on_main: bool = False
    plan_approved: bool = False
    matrix_status: str = ''   #: a Story's row in the plan's matrix
    in_prod: bool = False     #: merges in the deploy AND the operator's ✓ (or no deploy at all)
    signature: str = ''
    quiet_for: float = 0.0    #: seconds the signature has been unseen
    quiet_limit: float = 0.0  #: stage_limits.bug_quiet, in seconds
    landed: str = ''          #: the typed reconciliation sha (§2.5)
    parent_closed: bool = False
    typed_closed: bool = False     #: an Epic's typed `closed: true`, read only as an early close


def _landed(ev):
    return bool(ev.commit or ev.merged_sha)


def _own_evidence(ev):
    return bool(ev.commit or ev.merged_sha or ev.branch or ev.pr_state or ev.open_prs
                or ev.child_evidence)


def _all(ev, states):
    return bool(ev.children) and all(s in states for s in ev.children)


def _quiet(ev):
    return _landed(ev) and ev.green and (not ev.signature or ev.quiet_for >= ev.quiet_limit)


#: (type, name, predicate, state, prose) — first match per type decides; `any` rows lead.
RULES = (
    ('any', 'reconciled', lambda ev: bool(ev.landed and ev.green), CLOSED,
     'a typed landed sha and green CI'),
    ('any', 'parent-closed', lambda ev: bool(ev.parent_closed and not _own_evidence(ev)), CLOSED,
     'the parent is Closed and the item has no evidence of its own'),
    ('task', 'landed-green', lambda ev: _landed(ev) and ev.green, CLOSED,
     'a trunk commit or merged PR, and green CI'),
    ('task', 'landed', _landed, RESOLVED, 'a trunk commit or merged PR'),
    ('task', 'in-flight', lambda ev: bool(ev.branch or ev.pr_state), ACTIVE, 'a branch or a PR'),
    ('task', 'planned', lambda ev: True, NEW, 'in a plan, nothing yet'),
    ('story', 'tasks-closed', lambda ev: _all(ev, (CLOSED,)) and ev.in_prod, CLOSED,
     'every Task Closed, and in prod'),
    ('story', 'tasks-resolved', lambda ev: _all(ev, _DONE), RESOLVED,
     'every Task Resolved or Closed'),
    ('story', 'matrix-done', lambda ev: ev.matrix_status == 'done', CLOSED,
     "the plan's matrix says done"),
    ('story', 'matrix-doing', lambda ev: ev.matrix_status == 'doing', RESOLVED,
     "the plan's matrix says doing"),
    ('story', 'task-active', lambda ev: ACTIVE in ev.children or ev.child_evidence, ACTIVE,
     'a Task is Active'),
    ('bug', 'quiet', _quiet, CLOSED,
     'fix merged, CI green, signature unseen past the limit (or none)'),
    ('bug', 'fixed', _landed, RESOLVED, 'a fix merged'),
    ('bug', 'fixer', lambda ev: bool(ev.branch or ev.open_prs), ACTIVE, 'a fixer branch or PR'),
    ('bug', 'filed', lambda ev: True, NEW, 'filed, nothing yet'),
    ('feature', 'children-closed', lambda ev: _all(ev, (CLOSED,)) and ev.in_prod, CLOSED,
     'every child Closed, and in prod'),
    ('feature', 'children-resolved', lambda ev: _all(ev, _DONE), RESOLVED,
     'every child Resolved or Closed'),
    ('feature', 'landed-green',
     lambda ev: not ev.children and bool(ev.commit) and ev.green and ev.in_prod, CLOSED,
     'no children, a naming commit, green, in prod'),
    ('feature', 'landed', lambda ev: not ev.children and bool(ev.commit), RESOLVED,
     'no children and a naming commit'),
    ('feature', 'documented', lambda ev: bool(ev.spec_on_main or ev.plan_approved), ACTIVE,
     'a spec on main or an approved plan'),
    ('feature', 'card', lambda ev: True, NEW, 'a card only'),
    ('epic', 'children-closed', lambda ev: _all(ev, (CLOSED,)), CLOSED, 'every child Closed'),
    ('epic', 'typed-closed', lambda ev: ev.typed_closed, CLOSED,
     'the operator typed closed: true — the early close'),
    ('epic', 'children-resolved', lambda ev: _all(ev, _DONE), RESOLVED,
     'every child Resolved or Closed'),
    ('epic', 'child-active', lambda ev: ACTIVE in ev.children, ACTIVE, 'a child is Active'),
    ('epic', 'typed', lambda ev: True, NEW, 'nothing derived'),
)


def state_of(type_, ev, current=NEW):
    """The Closing for one item. Total: the first rule that holds decides, and when none does the
    item keeps `current` under NO_RULE."""
    for t, name, predicate, state, _prose in RULES:
        if t in ('any', type_) and predicate(ev):
            return Closing(state, name)
    return Closing(current, NO_RULE)


def sticky(old, closing):
    """`Closed` never derives backwards (§1.4). Returns the closing that gets written."""
    if old == CLOSED and closing.state != CLOSED:
        return dataclasses.replace(closing, state=CLOSED, rule='closed-terminal',
                                   lines=closing.lines + (f"held Closed; {closing.rule} would say "
                                                          f"{closing.state}",))
    return closing
