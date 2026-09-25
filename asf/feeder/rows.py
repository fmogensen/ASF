"""asf.feeder.rows — what the tick should start next, as rows.

Every row is read off ``index.json`` (the items' typed fields plus the machine block ingest
derived: ``state``, ``stage``, ``stage_since``, ``evidence``, ``blocked``), the caller's
``inflight`` list — one dict per running session, ``{'item': id, 'kind': brief kind, 'account':
worker, 'age': '12m'}`` — and the one answer to "is this item busy?",
:func:`asf.workers.lifecycle.occupancy` (live runs, work waiting to land, the lane's states, the
pending corrections). No git, no gh, no filesystem: a fact neither carries is not a fact the
feeder can use.

The row kinds::

    BUG → FIX              an open, decided S1/S2 Bug with no session (the S1 lane)
    FIX → CORRECT          an item whose branch the harvest held (red gate, conflict) fewer than
                           3 times: back to a session with the failing output. A ``footprint``
                           hold (paths outside ``writes:``, :mod:`asf.feeder.widen`) waits on
                           the rule until it widened the Task, else is RESHAPE → PLAN
    STALEMATE → ADJUDICATE a Feature at spec-/plan-review round >= 4: adjudicate, and nothing
                           else for that Feature (another review round will not converge); or a
                           Bug with 3 sessions behind it and still open (not a fourth fix)
    CONFLICT → REBASE      an Active Task/Bug whose PR no longer merges, no session on it
    STALE → CLOSE          an Active Task/Bug whose PR was closed unmerged, branch left behind
    CARD → SPEC            a decided Feature card with no spec
    STARVED → SPEC         a spec in draft/review that no session is moving
    STARVED → PLAN         an approved spec with no plan, or a plan in draft/review, unmoved
    PUSHED → REVIEW        a Task/Bug whose lane state is REVIEW: a review session on its branch
                           (a state, not a correction — no round is spent)
    PUSHED → LAND          a Task/Bug in any other open lane state, or what would have been
                           CARD → SPEC / STARVED → SPEC / STARVED → PLAN but that document's
                           branch waits to land: ``WAITS ON landing``, no session
    PLAN → CODE            a New Task of an approved plan — unless its ``writes:`` overlaps a
                           running Task's, then ``WAITS ON <task>`` (the footprint gate)
    RESHAPE → PLAN         a Task the groom's split answer marked: hold it, reshape it
    UNDECIDED → DECIDE     an open Feature, or an open S1/S2 Bug, whose ``decided`` is not true: the
                           card a free slot is waiting for. Launches nothing, costs no slot, and is
                           cut to ``conventions.decision_rows``
    GROOM → ADJUDICATE     an adjudicate session per groom day, for every open question the
                           groom policy pass did not answer (F-0085 §2.5), and another for
                           questions asked since the last was briefed — gated on
                           ``approvals.groom: auto``, given only when the caller passes a
                           ``groom_state``

``candidates()`` lists every row — within tier 2, finish before you start: a Task's rows before
any Feature's own (:func:`finish_phase`); ``plan_rows()`` caps new spec/plan sessions while
planned Tasks wait (:func:`finish_first`) and hands the rows to :mod:`asf.feeder.tiers` to order
and cut to capacity.
"""
import dataclasses
import re

from asf.feeder import footprint
from asf.groom import policy as groom_policy
from asf.views import index_reader as ix

BUG_FIX = 'BUG → FIX'
FIX_CORRECT = 'FIX → CORRECT'
CORRECTION_ROUNDS = 3  # == asf.workers.lifecycle.ROUND_CAP (the feeder imports no git module)
#: == asf.workers.lifecycle.NAMING: the lane rewords a naming refusal itself; one it could not
#: goes back to a session as a correction and never to adjudicate, whatever the item's rounds
NAMING = 'naming'
FOOTPRINT = 'footprint'  # == asf.workers.lifecycle.FOOTPRINT: a correction widen_footprint answers
STALEMATE = 'STALEMATE → ADJUDICATE'
CONFLICT = 'CONFLICT → REBASE'
STALE = 'STALE → CLOSE'
CARD_SPEC = 'CARD → SPEC'
STARVED_SPEC = 'STARVED → SPEC'
STARVED_PLAN = 'STARVED → PLAN'
PUSHED_LAND = 'PUSHED → LAND'
#: an approved spec that sits on a branch, not the trunk: the lane adopts the branch
#: (:mod:`asf.tick.land_spec`) and lands it — a row that launches nothing
APPROVED_LAND = 'APPROVED → LAND'
#: the correction kind the lane writes when a docs branch turns the product's gate red on the
#: trunk, or an approved spec cannot land as it stands (:func:`asf.harvest.lane.send_back`,
#: :mod:`asf.tick.land_spec`): a STARVED → SPEC/PLAN session changes the document on its own
#: branch, with the failing line in its brief (R7) — ``asf.harvest.lane.LANDING_GATE``
LANDING_GATE = 'landing-gate'
PUSHED_REVIEW = 'PUSHED → REVIEW'
WAITS_LANDING = 'WAITS ON landing'
#: a correction already adjudicated at this same hold (B-0128): no session, no round, until the
#: PR merges or closes, or a new push moves the head
WAITS_MERGE = 'WAITS ON merge'
PLAN_CODE = 'PLAN → CODE'
RESHAPE = 'RESHAPE → PLAN'
GROOM_ADJUDICATE = 'GROOM → ADJUDICATE'
UNDECIDED = 'UNDECIDED → DECIDE'
NEEDS_DECISION = 'NEEDS DECISION'
ON_TRUNK = 'ON TRUNK'
PARKED = 'PARKED'

LAUNCH = 'would launch'
DONE_STATES = ('Resolved', 'Closed')
STALEMATE_ROUND = 4
ATTEMPT_LIMIT = 3
DECISION_ROWS = 5  #: `conventions.decision_rows` — rows minted, and ids named in the wave's line
MAX_SPECS_IN_FLIGHT = 2  #: `conventions.feeder.max_specs_in_flight` (:func:`finish_first`)
#: the stages of a Feature whose plan is approved and whose Tasks are the work
BUILD_STAGES = ('plan-approved', 'building')
#: the rows that start a new document: what the finish-first cap counts and holds
NEW_DOC_KINDS = (CARD_SPEC, STARVED_SPEC, STARVED_PLAN)
FINISH = 'WAITS ON finish'
REVIEW_RE = re.compile(r'^(spec|plan)-review r(\d+)')
CLOSED_PR_RE = re.compile(r'\bPR #\d+ CLOSED\b')
#: ingest's line for a spec that sits on a branch, not the trunk (``spec on <branch>[ (review …)]``)
SPEC_ON_BRANCH_RE = re.compile(r'^spec on (?!origin/)(\S+)')
PLAN_ON_TRUNK = 'plan on origin/main'
CONFLICTING = 'CONFLICTING'


@dataclasses.dataclass
class Row:
    tier: int
    kind: str
    item_id: str
    feature_id: str
    action: str
    brief_kind: str
    branch: str
    reason: str
    waits_on: str = ''
    correction: str = ''
    #: a PUSHED → REVIEW row only: the round the reviewer writes
    review_round: int = 0
    #: the GROOM → ADJUDICATE row only (§2.5, PD8): the groom day, the record clone's groom
    #: file and the state dir's answers file, and the open questions' own lines (for the brief).
    groom_date: str = ''
    groom_file: str = ''
    answers_file: str = ''
    open_questions: tuple = ()

    @property
    def launches(self):
        return self.action.startswith(LAUNCH)


# ---- inputs -----------------------------------------------------------------

def items_of(index):
    """The live ``{id: item}`` map from an ``index.json`` dict or an already-loaded item map."""
    raw = index.get('items') if isinstance(index.get('items'), dict) else index
    return {k: v for k, v in raw.items() if isinstance(v, dict) and not v.get('removed')}


def inflight_ids(inflight):
    """Every item id a running session holds."""
    out = set()
    for s in inflight or []:
        iid = s.get('item') or s.get('item_id') or s.get('id')
        if iid:
            out.add(iid)
    return out


def occupied(occupancy):
    """Every item :func:`asf.workers.lifecycle.occupancy` holds from a new session: a live run's,
    and one whose work waits to land."""
    occ = occupancy or {}
    return set(occ.get('busy') or ()) | set(occ.get('waiting_landing') or ())


def session_of(inflight, item_id):
    for s in inflight or []:
        if item_id in (s.get('item'), s.get('item_id'), s.get('id')):
            return s
    return None


# ---- product conventions ----------------------------------------------------

def _conventions(product):
    if product is None:
        from asf.conventions import Conventions
        return Conventions()
    return product.conventions


def branch_for(product, kind, item_id):
    """``<prefix><id>`` — the product's prefix for ``kind`` (:meth:`Conventions.branch`, B-0067)."""
    return _conventions(product).branch(kind, item_id)


def stalemate_round(product):
    """``conventions.stalemate_round`` (default 4): the review round that stops the loop."""
    v = _conventions(product).get('stalemate_round')
    return v if isinstance(v, int) and v > 0 else STALEMATE_ROUND


def attempt_limit(product):
    """``conventions.attempt_limit`` (default 3): fix sessions a Bug gets before it is adjudicated."""
    v = _conventions(product).get('attempt_limit')
    return v if isinstance(v, int) and v > 0 else ATTEMPT_LIMIT


def max_specs_in_flight(product):
    """``conventions.feeder.max_specs_in_flight`` (default 2): the most spec/plan sessions a
    product runs at once while it has planned Features with Tasks ready to build
    (:func:`finish_first`)."""
    feeder = _conventions(product).get('feeder')
    v = feeder.get('max_specs_in_flight') if isinstance(feeder, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else MAX_SPECS_IN_FLIGHT


def decision_rows(product):
    """``conventions.decision_rows`` (default 5): how many undecided cards get a row (D6)."""
    v = _conventions(product).get('decision_rows')
    return v if isinstance(v, int) and v >= 0 else DECISION_ROWS


# ---- per-item predicates ----------------------------------------------------

def landed_ids(items, landed_shas=None):
    """Ids that count as landed: the record's Resolved/Closed, plus every id the trunk names.

    The two disagree when a Task's work reached the trunk under a sibling's commit, or when the
    ingest has not caught up with a harvest. `after:` must not hold a successor behind a
    predecessor whose work is *already there* — a coder launched into that gap finds the surface
    present and writes nothing (B-0076, F-0095).

    The fold runs over *all* items, not ``ix.feature_tasks``, so an ``after:`` naming a Task of
    another Feature is answerable at all. No ``landed_shas`` (the fact is absent): the record's
    set alone, exactly as before.
    """
    done = {i for i, v in items.items() if v.get('state') in DONE_STATES}
    return done | set(landed_shas or {})


def absorbers(items):
    """``{merged id: absorber id}`` off every live card's ``merged:`` list. A groom merge removes
    the merged card, and the index reader drops a removed card, so an ``after:`` naming it would
    name an id that never lands again."""
    out = {}
    for v in items.values():
        for m in v.get('merged') or ():
            if isinstance(m, str):
                out.setdefault(m, v['id'])
    return out


def after_of(items, item, absorbed=None):
    """``item``'s ``after:`` with each merged-away id read as the card that absorbed it (a chain
    followed to its end); an absorber the item itself is dropped — it does not wait on its own
    work."""
    absorbed = absorbers(items) if absorbed is None else absorbed
    out = []
    for a in item.get('after') or ():
        seen = {a}
        while a in absorbed and absorbed[a] not in seen:
            a = absorbed[a]
            seen.add(a)
        if a != item.get('id') and a not in out:
            out.append(a)
    return out


def is_open(item):
    return item.get('state', 'New') not in DONE_STATES


def review_round(item):
    """(doc, round) off a ``spec-review r3`` / ``plan-review r4`` stage, else (None, 0)."""
    m = REVIEW_RE.match(item.get('stage') or '')
    return (m.group(1), int(m.group(2))) if m else (None, 0)


def feature_of(items, item):
    """The Feature above an item (itself for a Feature), or None."""
    seen = set()
    while item and item['id'] not in seen:
        seen.add(item['id'])
        if item['type'] == 'feature':
            return item
        item = items.get(item.get('parent'))
    return None


def epic_of(items, item):
    """The Epic above an item (walking parents), or None."""
    seen = set()
    item = items.get(item.get('parent')) if item else None
    while item and item['id'] not in seen:
        seen.add(item['id'])
        if item['type'] == 'epic':
            return item
        item = items.get(item.get('parent'))
    return None


def feature_order(items, feature):
    """A Feature's place in the feeder: its Epic's rank, then its own rank, then its id — so a
    product puts a whole Epic first by ranking the Epic. Rank orders only within a parent, so a
    Feature's own rank alone would interleave Epics. No Epic, or an unranked one, sorts last."""
    epic = epic_of(items, feature)
    return (ix.rank(epic) if epic else ix.BIG, ix.rank(feature), feature['id'])


def _task_feature(items, task):
    """A Task hangs under a Feature directly or via a Story; fall back to its ``feature:`` field."""
    f = feature_of(items, task)
    if f is None and task.get('feature') in items:
        f = items[task['feature']]
    return f


def _pr_conflicting(item):
    return (item.get('mergeable') == CONFLICTING
            or any(CONFLICTING in e for e in item.get('evidence') or []))


def _pr_closed(item):
    return any(CLOSED_PR_RE.search(e) for e in item.get('evidence') or [])


def _branch_of(item, product, kind):
    branches = (item.get('links') or {}).get('branches') or []
    lane = 'code' if kind == 'task' else kind  # B-0067: a Task's branch is the code lane's
    return branches[0] if branches else branch_for(product, lane, item['id'])


# ---- rows -------------------------------------------------------------------

def _age_key(item):
    """Older card first: an ISO timestamp sorts as text; a card with none goes last."""
    return item.get('created') or item.get('stage_since') or '~'


def bug_rows(items, product, busy, attempts=None):
    """Within a tier: fewest attempts, then the older card, then id. ``attempts`` is ``{id: sessions
    the registry holds}``, ended or not. A Bug at the limit gets one adjudicate row — its session
    is the next attempt, so the row is gone once it has run (attempts above the limit: silent)."""
    attempts, out = attempts or {}, []
    limit = attempt_limit(product)
    bugs = sorted(ix.of_type(items, 'bug'), key=lambda v: (attempts.get(v['id'], 0), _age_key(v), v['id']))
    for b in bugs:
        sev = b.get('severity')
        if sev not in ('S1', 'S2') or b.get('decided') is not True or not is_open(b) or b['id'] in busy:
            continue
        if b.get('blocked'):  # B-0058: a blocked Bug waits like a blocked Feature
            continue
        # an Active Bug has a fixer branch/PR already: CONFLICT/STALE rows speak for it
        if b.get('state') == 'Active':
            continue
        f = feature_of(items, b)
        fid, n = f['id'] if f else '', attempts.get(b['id'], 0)
        if n > limit:
            continue
        if n == limit:
            out.append(Row(tier=0 if sev == 'S1' else 1, kind=STALEMATE, item_id=b['id'],
                           feature_id=fid, action=LAUNCH, brief_kind='adjudicate',
                           branch=branch_for(product, 'fix', b['id']),
                           reason=f"{sev} open after {n} sessions: adjudicate, not another fix"))
            continue
        out.append(Row(tier=0 if sev == 'S1' else 1, kind=BUG_FIX, item_id=b['id'],
                       feature_id=fid, action=LAUNCH, brief_kind='fix-bug',
                       branch=branch_for(product, 'fix', b['id']),
                       reason=f"{sev} open, decided, no session — its ## Fix is the plan"))
    return out


def footprint_row(item, product, c, tier, fid, branch):
    """The row a ``footprint`` correction (:mod:`asf.feeder.widen`) stands for until the rule
    has widened the Task — a widened one is an ordinary FIX → CORRECT row, on the wider
    ``writes:``. A reshape verdict is the RESHAPE row (the card's ``reshape:`` says why); a path
    under an approvals-protected glob waits on its approval; a path a running Task writes waits
    on that Task; an undecided one waits on the rule (the tick's health step decides it)."""
    iid, verdict, detail = item['id'], c.get('verdict'), c.get('detail') or ''
    if verdict == 'reshape':
        return Row(tier=tier, kind=RESHAPE, item_id=iid, feature_id=fid, action=LAUNCH,
                   brief_kind='reshape', branch=branch_for(product, 'plan', iid),
                   reason=item.get('reshape') or detail
                   or f"footprint: needs {' '.join(c.get('needs') or ())}")
    if verdict == 'approval':
        action, waits, why = f'WAITS ON approval {detail}', 'approval', \
            f'footprint needs a path under {detail}: approvals decide'
    elif verdict == 'waits':
        action, waits, why = f'WAITS ON {detail}', detail, f'footprint widening overlaps {detail}'
    else:
        action, waits, why = 'WAITS ON widen_footprint', 'widen', \
            f"footprint needs {' '.join(c.get('needs') or ())}: the rule decides next tick"
    return Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid, action=action,
               brief_kind='correct', branch=branch, reason=why, waits_on=waits)


def correction_rows(items, product, busy, corrections):
    """``corrections`` is ``{item: {kind, text, rounds, at, branch}}`` — a branch the harvest held
    (the row runs on that branch when it is given). Fewer
    than 3 rounds: a FIX → CORRECT row in the item's severity tier; 3 or more: the ADJUDICATE row.
    Returns ``(rows, ids)``; ``ids`` are the items these rows speak for."""
    out, ids = [], set()
    for iid, c in sorted((corrections or {}).items()):
        item = items.get(iid)
        if not item or not c or not c.get('text') or not is_open(item) or iid in busy:
            continue
        if item.get('blocked'):  # B-0058: a blocked item gets no correction or adjudicate row either
            continue
        ids.add(iid)
        f = feature_of(items, item)
        fid, rounds = f['id'] if f else '', c.get('rounds') or 0
        tier = {'S1': 0, 'S2': 1}.get(item.get('severity'), 2)
        kind = 'fix' if item['type'] == 'bug' else 'task'
        branch = c.get('branch') or branch_for(product, kind, iid)
        if c.get('parked'):  # a Task that wrote nothing twice waits for a person, not a session
            out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid,
                           action=f'{PARKED} {c.get("reason") or c["kind"]}', brief_kind='correct',
                           branch=branch, reason=c.get('reason') or 'parked', waits_on='operator'))
            continue
        doc = product.conventions.branch_kind(branch) if c.get('kind') == LANDING_GATE else None
        if doc in ('spec', 'plan') and rounds < CORRECTION_ROUNDS:  # a document the gate refused
            out.append(Row(tier=tier, kind=STARVED_SPEC if doc == 'spec' else STARVED_PLAN,
                           item_id=iid, feature_id=fid or iid, action=LAUNCH, brief_kind=doc,
                           branch=branch, correction=c['text'],
                           reason=f"the lane held it ({c['kind']}), round {rounds}: the {doc} "
                                  f"cannot land as it stands"))
            continue
        if c.get('kind') == FOOTPRINT and c.get('verdict') != 'widen':
            out.append(footprint_row(item, product, c, tier, fid, branch))
            continue
        if rounds >= CORRECTION_ROUNDS:
            if c.get('settled'):  # B-0128: already ruled at this hold — no second adjudicate
                out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid,
                               action=WAITS_MERGE, brief_kind='correct', branch=branch,
                               reason=f"adjudicated ({c.get('kind')}): waits on the PR to merge "
                                      f"or close, or a new push", waits_on='merge'))
                continue
        if rounds >= CORRECTION_ROUNDS and c.get('kind') != NAMING:
            out.append(Row(tier=tier, kind=STALEMATE, item_id=iid, feature_id=fid, action=LAUNCH,
                           brief_kind='adjudicate', branch=branch,
                           reason=f"held {rounds} times ({c.get('kind')}): adjudicate, not another correction"))
        else:
            out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid, action=LAUNCH,
                           brief_kind='correct', branch=branch, correction=c['text'],
                           reason=f"harvest held it ({c.get('kind')}), round {rounds}: back to a session"))
    return out, ids


def review_tier(item):
    """A review row's tier: S1 first, then a Bug's fix (with S2) — a fix waits on its review
    before anything else — then the item's own tier."""
    tier = {'S1': 0, 'S2': 1}.get(item.get('severity'), 2)
    return min(tier, 1) if item.get('type') == 'bug' else tier


def lane_rows(items, product, busy, occupancy):
    """One row per Task/Bug the lane holds (``occupancy['review']`` / ``['landing']``) that is
    open and no session holds and no correction speaks for (``busy``): PUSHED → REVIEW, a launch,
    for the lane's REVIEW state (the round it asks for); a ``WAITS ON landing`` PUSHED → LAND row
    for any other open lane state. BACK is a correction's (FIX → CORRECT)."""
    out = []
    occ = occupancy or {}
    held = [(iid, h, True) for iid, h in (occ.get('review') or {}).items()]
    held += [(iid, h, False) for iid, h in (occ.get('landing') or {}).items()]
    from asf.harvest.lane import is_pr_item  # local: the lane imports the feeder
    for iid, h, review in sorted(held, key=lambda t: t[0]):
        item = items.get(iid)
        foreign = not item and is_pr_item(iid)  # merge: auto — a PR no factory item made
        if foreign:
            if iid in busy:
                continue
            item = {'id': iid, 'type': 'task'}
        elif not item or not is_open(item) or iid in busy or item.get('blocked'):
            continue
        if item.get('type') not in ('task', 'bug'):
            continue
        f = None if foreign else feature_of(items, item)
        fid, branch, number = (f['id'] if f else ''), h.get('branch') or '', h.get('pr')
        what = f'PR #{number}' if number else branch
        if foreign:
            what += ' (opened outside the factory, no card)'
        if review:
            rnd = int(h.get('round') or 1)
            out.append(Row(tier=review_tier(item), kind=PUSHED_REVIEW, item_id=iid,
                           feature_id=fid, action=LAUNCH, brief_kind='review', branch=branch,
                           review_round=rnd,
                           reason=f"{what} has no review of its head: round {rnd} "
                                  f"({h.get('why') or 'no verdict'})"))
        else:
            out.append(Row(tier=review_tier(item), kind=PUSHED_LAND, item_id=iid,
                           feature_id=fid, action=f"{WAITS_LANDING}: {what} {h.get('state')}",
                           brief_kind='review', branch=branch, reason=h.get('why') or ''))
    return out


def branch_rows(items, product, busy):
    """CONFLICT → REBASE and STALE → CLOSE over Active Tasks and Bugs no session holds."""
    out = []
    for v in sorted(items.values(), key=lambda v: v['id']):
        if v['type'] not in ('task', 'bug') or v.get('state') != 'Active' or v['id'] in busy:
            continue
        f = _task_feature(items, v) if v['type'] == 'task' else feature_of(items, v)
        fid = f['id'] if f else ''
        kind = 'task' if v['type'] == 'task' else 'fix'
        if _pr_closed(v):
            out.append(Row(tier=2, kind=STALE, item_id=v['id'], feature_id=fid, action=LAUNCH,
                           brief_kind='close', branch=_branch_of(v, product, kind),
                           reason='PR closed unmerged, branch left behind'))
        elif _pr_conflicting(v):
            out.append(Row(tier=2, kind=CONFLICT, item_id=v['id'], feature_id=fid, action=LAUNCH,
                           brief_kind='rebase', branch=_branch_of(v, product, kind),
                           reason='PR does not merge cleanly, no session on it'))
    return out


def running_footprints(items, busy):
    """[(task_id, writes)] of every Task whose files are genuinely in play: a live session, or a
    pushed branch waiting for harvest (both in ``busy``).

    A Task that is merely ``Active`` in the index does NOT hold its footprint (B-0076): a held
    branch — gate red, correction pending, or a card whose only evidence is a branch — is not
    being written by anyone, and treating it as in flight deadlocks every sibling that shares a
    file with it. Two branches that do touch the same file still meet at the rebase, where the
    correction loop resolves it; a wait here must mean "someone is writing this now"."""
    out = []
    for t in sorted(ix.of_type(items, 'task'), key=lambda v: v['id']):
        if t['id'] in busy and t.get('writes'):
            out.append((t['id'], list(t['writes'])))
    return out


def _waiting_doc(fid, doc, product, occupancy, branch=None):
    """Why ``fid``'s ``doc`` (spec | plan) is not starved though no session holds it — its
    branch waits to land (a finished run, or an open lane state) — else ''."""
    occ = occupancy or {}
    why = (occ.get('branches') or {}).get(branch or branch_for(product, doc, fid)) \
        or ((occ.get('docs') or {}).get(fid) or {}).get(doc)
    return f"{doc} {why}" if why else ''


def spec_carrier(feature):
    """The branch ``feature``'s spec sits on when it is not on the trunk, from ingest's line."""
    for line in feature.get('evidence') or []:
        m = SPEC_ON_BRANCH_RE.match(str(line))
        if m:
            return m.group(1)
    return ''


def _doc_row(kind, fid, doc, product, reason, occupancy, branch=None):
    """The launching row for ``fid``'s ``doc`` — or, when that document's work is pushed and
    waiting to land, a PUSHED → LAND row that launches nothing."""
    branch = branch or branch_for(product, doc, fid)
    waiting = _waiting_doc(fid, doc, product, occupancy, branch)
    if waiting:
        return Row(tier=2, kind=PUSHED_LAND, item_id=fid, feature_id=fid,
                   action=f"{WAITS_LANDING}: {waiting}", brief_kind=doc, branch=branch,
                   reason=waiting)
    return Row(tier=2, kind=kind, item_id=fid, feature_id=fid, action=LAUNCH, brief_kind=doc,
               branch=branch, reason=reason)


def feature_rows(items, product, busy, running, landed_shas=None, occupancy=None):
    """Every Feature's rows, in Feature order (:func:`feature_order`: Epic rank, rank, id).
    ``running`` grows as PLAN → CODE rows are handed out, so two ready Tasks sharing a file never
    both launch. ``occupancy`` (:func:`asf.workers.lifecycle.occupancy`): a spec or plan whose
    work is pushed and waiting to land is not starved (PUSHED → LAND)."""
    out = []
    limit = stalemate_round(product)
    feats = [f for f in ix.of_type(items, 'feature')
             if f.get('decided') is True and is_open(f) and not f.get('blocked')]
    for f in sorted(feats, key=lambda v: feature_order(items, v)):
        fid = f['id']
        stage = f.get('stage') or 'card'
        doc, rnd = review_round(f)
        if doc and rnd >= limit:
            if fid not in busy:
                out.append(Row(tier=2, kind=STALEMATE, item_id=fid, feature_id=fid, action=LAUNCH,
                               brief_kind='adjudicate', branch=branch_for(product, doc, fid),
                               reason=f"{doc}-review r{rnd} >= r{limit}: adjudicate, no further round"))
            continue
        if fid in busy:
            continue
        word = stage.split(' ')[0]
        waits = (occupancy,)
        if word == 'card':
            out.append(_doc_row(CARD_SPEC, fid, 'spec', product, 'decided card, no spec', *waits))
        elif word in ('spec-draft', 'spec-review'):
            carrier = spec_carrier(f)
            if carrier and PLAN_ON_TRUNK in (f.get('evidence') or []):
                # the plan landed on a spec the trunk never got: that spec is landed as it
                # stands, on its own branch — not written again, and no coder starts before it
                out.append(_doc_row(STARVED_SPEC, fid, 'spec', product,
                                    f"{stage}: the plan is on the trunk but the spec is still on "
                                    f"{carrier} — land the existing spec from that branch, "
                                    f"don't rewrite it", *waits, branch=carrier))
            else:
                out.append(_doc_row(STARVED_SPEC, fid, 'spec', product, f"{stage}, no session",
                                    *waits))
        elif word == 'spec-approved' and spec_carrier(f):
            out.append(land_spec_row(f, product, *waits))
        elif word in ('spec-approved', 'plan-draft', 'plan-review'):
            out.append(_doc_row(STARVED_PLAN, fid, 'plan', product,
                                f"{stage}, no session" if word != 'spec-approved'
                                else 'spec approved, no plan', *waits))
        elif word in ('plan-approved', 'building'):
            out.extend(task_rows(items, product, f, busy, running, landed_shas))
    return out


def land_spec_row(feature, product, occupancy):
    """An approved spec on a branch, not the trunk: coders read the spec from the trunk, so it is
    landed first — as written, never rewritten. Pushed and waiting (a PR open, a run the docs
    lane has not merged yet): PUSHED → LAND; else APPROVED → LAND, which launches nothing — the
    lane adopts the branch (:mod:`asf.tick.land_spec`) and lands it once green. A branch that
    cannot land as it stands comes back as a STARVED → SPEC session through its
    :data:`LANDING_GATE` correction (:func:`correction_rows`)."""
    fid, carrier = feature['id'], spec_carrier(feature)
    waiting = _waiting_doc(fid, 'spec', product, occupancy, carrier)
    if waiting:
        return Row(tier=2, kind=PUSHED_LAND, item_id=fid, feature_id=fid,
                   action=f"{WAITS_LANDING}: {waiting}", brief_kind='spec', branch=carrier,
                   reason=waiting)
    return Row(tier=2, kind=APPROVED_LAND, item_id=fid, feature_id=fid,
               action=f"{WAITS_LANDING}: spec approved on {carrier}", brief_kind='spec',
               branch=carrier, waits_on='landing',
               reason=f"spec approved on {carrier}, not on the trunk: the lane adopts it and "
                      f"lands it — no coder starts before it is on the trunk")


def task_rows(items, product, feature, busy, running, landed_shas=None):
    out = []
    tasks = [t for t in ix.feature_tasks(items, feature)
             if t.get('state', 'New') == 'New' and t['id'] not in busy and not t.get('blocked')]
    landed = landed_ids(items, landed_shas)
    absorbed = absorbers(items)
    on_trunk = landed_shas or {}
    for t in sorted(tasks, key=lambda v: (ix.rank(v), v['id'])):
        if t['id'] in on_trunk:  # already on main: a coder would find the surface there and write nothing
            sha, subject = on_trunk[t['id']]
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action=f'{ON_TRUNK} {sha[:12]}', brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason=f'already on main: {sha[:12]} "{subject}" — the card is still '
                                  f'{t.get("state", "New")}, the record has not caught up',
                           waits_on='trunk'))
            continue
        # `after: [T-nnnn]` is a declared dependency: Task N builds on what Task N-1 landed, and
        # a coder started before it finds the surface missing and writes nothing (B-0076).
        # Footprint-disjoint tasks still run in parallel — a plan says so by leaving `after:` off.
        pending = [a for a in after_of(items, t, absorbed) if a not in landed]
        if pending:
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action=f"WAITS ON {pending[0]}", brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason=f"after: {pending[0]} has not landed", waits_on=pending[0]))
            continue
        if t.get('reshape'):
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action='WAITS ON reshape', brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason='groom marked it for reshape: waiting on its reshape session',
                           waits_on='reshape'))
            out.append(Row(tier=2, kind=RESHAPE, item_id=t['id'], feature_id=feature['id'],
                           action=LAUNCH, brief_kind='reshape',
                           branch=branch_for(product, 'plan', t['id']),
                           reason=f"groom: {t['reshape']}"))
            continue
        if t.get('split_from') and t.get('decided') is not True:
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action='WAITS ON confirm', brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason='groom split part, unconfirmed', waits_on='confirm'))
            continue
        writes = t.get('writes') or []
        if not writes:
            # a coder with no declared footprint can neither be checked against the others nor
            # know what it may touch: it reports blocked and the slot is spent (first customer)
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action='WAITS ON writes', brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason='no writes: declared: the plan must name the files this Task '
                                  'writes before a coder can start', waits_on='writes'))
            continue
        other = footprint.first_conflict(writes, running,
                                         _conventions(product).get('shared_paths') or ())
        if other:
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action=f"WAITS ON {other}", brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason=f"writes: overlaps {other}", waits_on=other))
            continue
        # B-0067: a coder works the code lane — `conventions.branch_prefixes.code` (worker/ by
        # default), the one prefix harvest scans for code; `task/` was a lane nobody harvested
        out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                       action=LAUNCH, brief_kind='task', branch=branch_for(product, 'code', t['id']),
                       reason='plan approved, footprint free'))
        running.append((t['id'], list(writes)))
    return out


def undecided_rows(items, product, busy, limit=None):
    """A non-launching row per open card the feeder would start work from if it were decided:
    an open Feature, or an open S1/S2 Bug (D4). Ranked — an undecided S1 first, then the
    roadmap's own order (``ix.rank``, then id) — and cut to ``limit`` (``None``: the product's
    ``decision_rows``; ``0``: every one, ``asf next --all``).

    The row launches nothing and costs no slot (P2); it is the sentence "this card is what a free
    slot is waiting for", in the one table that says what the tick would start. A ``blocked:``
    card gets none — it is the accounting's ``blocked`` bucket, one card, one answer.
    """
    roots = []
    for v in items.values():
        if v['type'] == 'feature':
            tier = 2
        elif v['type'] == 'bug' and v.get('severity') in ('S1', 'S2'):
            tier = 0 if v['severity'] == 'S1' else 1
        else:
            continue
        if v.get('decided') is True or not is_open(v) or v['id'] in busy or v.get('blocked'):
            continue
        roots.append((tier, ix.rank(v), v['id'], v))
    roots.sort(key=lambda t: t[:3])
    cap = decision_rows(product) if limit is None else limit
    if cap:
        roots = roots[:cap]
    out = []
    for tier, _rank, _id, v in roots:
        is_bug = v['type'] == 'bug'
        f = feature_of(items, v)
        out.append(Row(tier=tier, kind=UNDECIDED, item_id=v['id'], feature_id=f['id'] if f else '',
                       action=NEEDS_DECISION, brief_kind='fix-bug' if is_bug else 'spec',
                       branch=branch_for(product, 'fix' if is_bug else 'spec', v['id']),
                       reason=f"undecided {ix.age(v.get('stage_since'))} — "
                              f"{BUG_FIX if is_bug else CARD_SPEC} waits for decided: true",
                       waits_on='decision'))
    return out


KIND_ORDER = {STALEMATE: 0, CONFLICT: 1, STALE: 2, GROOM_ADJUDICATE: 2, RESHAPE: 3}


def groom_row(index, product, busy, groom_state, inflight):
    """At most one GROOM → ADJUDICATE row (§2.5): an adjudicate session per groom day, for
    every question the policy pass did not answer — and, once the day had one, another only for
    questions its brief did not carry (``groom_state['new']``), up to
    ``groom.adjudicate_per_day``. ``groom_state`` is the one fact this module
    cannot derive from ``index.json`` (P5) — the caller (:mod:`asf.tick.step_wave`) builds it
    from the record clone's newest ``groom/<date>.md`` (:func:`asf.groom.policy.open_questions`)
    and the ledger's ``groom-<date>`` attempts. No ``groom_state``, the gate off, no open
    question, a live session already on that day's job, or the attempt cap reached: no row —
    which keeps every existing feeder test and ``asf next`` unchanged (T8)."""
    if not groom_state or not groom_policy.groom_auto(product):
        return None
    open_ids = list(groom_state.get('open') or ())
    if not open_ids:
        return None
    date = groom_state.get('date')
    if any(s.get('job') == f'groom-{date}' for s in inflight or ()):
        return None
    attempts = groom_state.get('attempts') or 0
    new = groom_state.get('new')
    if new is None:  # a caller that does not say which questions are new: the attempt cap alone
        if attempts >= groom_policy.adjudicate_attempts(product):
            return None
    elif attempts and not (new and attempts < groom_policy.adjudicate_per_day(product)):
        # the day already had its session: another only for questions asked since, up to the cap
        return None
    items = items_of(index)
    oldest = groom_state.get('oldest') or open_ids[0]
    item = items.get(oldest) or {}
    f = feature_of(items, item) if item else None
    reason = (f"{len(open_ids)} groom questions no rule answers, oldest {oldest} "
             f"(undecided {ix.age(item.get('stage_since'))})")
    return Row(tier=2, kind=GROOM_ADJUDICATE, item_id=oldest, feature_id=f['id'] if f else '',
              action=LAUNCH, brief_kind='groom',
              branch=branch_for(product, 'groom', date), reason=reason,
              groom_date=date, groom_file=groom_state.get('file', ''),
              answers_file=groom_state.get('answers', ''),
              open_questions=tuple(groom_state.get('lines') or ()))


def hold_unlanded(rows, items, landed_shas=None):
    """B-0080: ``after:`` holds every row kind, not only PLAN → CODE. An item whose predecessor
    has not landed is not in dispute, it is waiting: a launching row for it (code, correct,
    adjudicate, rebase, close) becomes ``WAITS ON <id>`` — no session, no round. The groom row
    speaks for a day's questions, not for the item it names, so it is left alone."""
    landed = landed_ids(items, landed_shas)
    absorbed = absorbers(items)
    out, said = [], set()
    for r in rows:
        pending = [a for a in after_of(items, items.get(r.item_id) or {}, absorbed)
                   if a not in landed]
        # ON TRUNK / PARKED / NEEDS DECISION are already non-launching answers with their own
        # waits_on: rewriting them into WAITS ON would hide the row the gate exists to print
        keeps = r.kind == GROOM_ADJUDICATE or r.action.startswith((ON_TRUNK, PARKED, NEEDS_DECISION))
        if pending and not keeps and (r.launches or r.waits_on):
            if r.item_id in said:  # a Task with a correction also has its PLAN → CODE row: once
                continue
            said.add(r.item_id)
            r = dataclasses.replace(r, action=f"WAITS ON {pending[0]}", waits_on=pending[0],
                                    reason=f"after: {pending[0]} has not landed")
        out.append(r)
    return out


#: the rows ``feeder.hold`` holds, per class it names: new work only — a review, a correction,
#: an adjudicate, the groom and landing are never held
HELD_KINDS = {'features': (CARD_SPEC, STARVED_SPEC, STARVED_PLAN, PLAN_CODE),
              'bugs': (BUG_FIX,)}
HOLD = 'WAITS ON hold'


def hold_classes(rows, product):
    """``feeder.hold`` (:attr:`asf.env.Product.feeder_hold`): each launching row of a held
    class becomes ``WAITS ON hold: <class>`` — no session, no slot, still shown."""
    held = getattr(product, 'feeder_hold', None) or ()
    kinds = {k: cls for cls in sorted(held) for k in HELD_KINDS.get(cls, ())}
    if not kinds:
        return rows
    return [dataclasses.replace(r, action=f'{HOLD}: {kinds[r.kind]}', waits_on='hold',
                                reason=f'feeder.hold: {kinds[r.kind]}')
            if r.kind in kinds and r.launches else r for r in rows]


def candidates(index, product, inflight, attempts=None, occupancy=None, groom_state=None,
               landed_shas=None, decision_limit=None):
    """Every row the index supports right now, uncut by capacity, in emit order: tier, then the
    Feature's order (:func:`feature_order`: Epic rank, Feature rank, id), then within a Feature
    the stalemate, branch housekeeping, new work.

    ``occupancy`` is the one answer to "is this item busy?"
    (:func:`asf.workers.lifecycle.occupancy`): its ``busy`` items (a live run) and its
    ``waiting_landing`` items (work pushed and waiting on the lane) get no new session; its
    ``review``/``landing`` lane states are the PUSHED → REVIEW / PUSHED → LAND rows
    (:func:`lane_rows`), its ``corrections`` the FIX → CORRECT rows. A card already
    Resolved/Closed is never busy: its work is on the trunk whatever the ledger says.
    ``groom_state``: §2.5's fact for the GROOM → ADJUDICATE row; a caller that passes none gets
    none. ``decision_limit``: how many UNDECIDED → DECIDE rows (``None``: ``decision_rows``;
    ``0``: all)."""
    items = items_of(index)
    occ = occupancy or {}
    corrections = occ.get('corrections') or {}
    live = inflight_ids(inflight) | set(occ.get('busy') or ())
    waiting = {i for i in occ.get('waiting_landing') or {} if is_open(items.get(i) or {})}
    busy = live | waiting
    limit = stalemate_round(product)
    stalled = {f['id'] for f in ix.of_type(items, 'feature') if review_round(f)[1] >= limit}
    running = running_footprints(items, busy)
    corrected, spoken = correction_rows(items, product, busy, corrections)
    rows = corrected + lane_rows(items, product, live | spoken, occ)
    rows += bug_rows(items, product, busy | spoken, attempts)
    rows += [r for r in branch_rows(items, product, busy) if r.feature_id not in stalled]
    gr = groom_row(index, product, busy, groom_state, inflight)
    if gr is not None:
        rows.append(gr)
    # a Task a correction row speaks for gets no PLAN → CODE row too: one session per branch
    tasks_spoken = {i for i in spoken if (items.get(i) or {}).get('type') == 'task'
                    or corrections.get(i, {}).get('kind') == LANDING_GATE}
    # a Feature's spec and plan wait to land one branch at a time: the document rows read that
    # per branch (:func:`_waiting_doc`), so one waiting document never holds the other
    docs_waiting = {i for i in waiting if (items.get(i) or {}).get('type') == 'feature'}
    rows += feature_rows(items, product, (busy - docs_waiting) | tasks_spoken, running,
                         landed_shas, occ)
    rows += undecided_rows(items, product, busy, decision_limit)
    rows = hold_unlanded(rows, items, landed_shas)
    rows = hold_classes(rows, product)

    def key(pair):
        seq, r = pair
        f = items.get(r.feature_id) or {}
        if r.tier < 2:
            return (r.tier, 0, '', 0, seq)
        if r.kind == GROOM_ADJUDICATE:
            # one session decides the whole day's questions for every Feature: it goes before
            # the Feature work, not at the rank of whichever card happens to be the oldest (an
            # unranked inbox card put it behind every launch, and the cut never reached it)
            return (r.tier, -1, -1, '', 0, seq)
        order = feature_order(items, f) if f else (ix.BIG, ix.BIG, r.feature_id or '~')
        return (r.tier, finish_phase(items, r), *order, KIND_ORDER.get(r.kind, 4), seq)
    return [r for _seq, r in sorted(enumerate(rows), key=key)]


def finish_phase(items, row):
    """Finish before you start (tier 2 only; a Bug's tier is its own): a row on a Task — its
    coder, correction, review, rebase — is 0 and goes before every row on a Feature itself
    (its spec, plan, adjudicate, landing, decision), which is 1. A product, 2026-09-25, over 7
    days: 36 Features plan-approved and idle, 13 in plan-draft, 3 building, 2 landed — the
    Feature order interleaved new documents with the Tasks of Features already planned, and
    the cut spent the slots on the documents."""
    return 1 if (items.get(row.item_id) or {}).get('type') == 'feature' else 0


def buildable_features(items, rows, held=()):
    """The Features planned but not built whose Tasks have a launching row — a coder,
    correction or review a slot would start now (``held``: items an approval parks)."""
    out = []
    for r in rows:
        item = items.get(r.item_id) or {}
        if not r.launches or r.item_id in held or item.get('type') != 'task':
            continue
        f = items.get(r.feature_id) or {}
        if (f.get('stage') or '').split(' ')[0] in BUILD_STAGES and r.feature_id not in out:
            out.append(r.feature_id)
    return out


def finish_first(rows, items, product, inflight, held=()):
    """``feeder.max_specs_in_flight`` (default 2): while a planned Feature has Tasks ready to
    build (:func:`buildable_features`), new spec and plan sessions (:data:`NEW_DOC_KINDS`) are
    capped — the ones already running count, and so does a correction of a document the lane
    refused (never held itself) — and each launching row past the cap becomes ``WAITS ON
    finish: …`` with the numbers. No buildable Feature: no cap, the documents are the only work
    there is."""
    held = set(held or ())
    ready = buildable_features(items, rows, held)
    if not ready:
        return rows
    cap = max_specs_in_flight(product)
    running = sum(1 for s in inflight or () if s.get('kind') in ('spec', 'plan'))
    # a correction of a document the lane refused is never held, but it is a document session
    # this wave: it counts against the cap before any new one is admitted
    admitted = sum(1 for r in rows if r.kind in NEW_DOC_KINDS and r.launches and r.correction
                   and r.item_id not in held)
    out = []
    named = ', '.join(ready[:3]) + (f' +{len(ready) - 3}' if len(ready) > 3 else '')
    for r in rows:
        if r.kind in NEW_DOC_KINDS and r.launches and not r.correction and r.item_id not in held:
            if running + admitted < cap:
                admitted += 1
            else:
                why = (f"{running} spec/plan in flight + {admitted} this wave, cap {cap} "
                       f"(feeder.max_specs_in_flight) while {len(ready)} planned Feature"
                       f"{' has' if len(ready) == 1 else 's have'} Tasks to build: {named}")
                r = dataclasses.replace(r, action=f'{FINISH}: {why}', waits_on='finish',
                                        reason=f'finish before you start — {why}')
        out.append(r)
    return out


def plan_rows(index, product, inflight, capacity, attempts=None, occupancy=None,
              groom_state=None, landed_shas=None, decision_limit=None, held=None, exclude=None):
    """The rows the tick emits: tiered, S1 first, cut to ``capacity`` less what is in flight.
    ``held``: the item ids an approval hold parks — shown, but given no slot. ``exclude``: the
    launching rows (:func:`asf.invariants.row_key`) the feeder's invariant gate dropped — they
    are not candidates, so the cut hands their slots to the next rows."""
    from asf.feeder import tiers
    rows = candidates(index, product, inflight, attempts, occupancy=occupancy,
                      groom_state=groom_state, landed_shas=landed_shas,
                      decision_limit=decision_limit)
    if exclude:
        from asf.invariants import row_key
        rows = [r for r in rows if not (r.launches and row_key(r) in exclude)]
    rows = finish_first(rows, items_of(index), product, inflight, held)
    return tiers.select(rows, inflight, capacity, held=held)
