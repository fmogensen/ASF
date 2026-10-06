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
                           else for that Feature (another review round will not converge); or any
                           launching kind (``CAPPED_KINDS``, and a Bug's fix) with 3 sessions behind
                           it and still open (not a fourth attempt)
    CONFLICT → REBASE      an Active Task/Bug whose PR no longer merges, no session on it
    STALE → CLOSE          an Active Task/Bug whose PR was closed unmerged, branch left behind
    CARD → SPEC            a decided Feature card with no spec
    CARD → SPEC+PLAN       the same for a ``size: s`` Feature: one session writes the spec and the
                           plan as one document (brief ``spec-plan``), no separate review round
    DIRECT → BUILD         a decided ``lane: direct`` Feature with no Tasks: one session builds it
                           end to end on ``conventions.branch_prefixes.direct`` (brief
                           ``direct``) — never a spec or plan row; its branch lands like any code
                           PR (PUSHED → REVIEW / LAND speak for it once pushed)
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
    RESHAPE → REPLAN       a Feature in build whose ``reshape:`` no replan has carried out yet
                           (:mod:`asf.record.replan`): one ``replan`` session rewrites, adds or
                           drops its not-yet-landed Tasks and their ``after:`` in one document the
                           record applies once it lands; until then the Feature's code rows wait
                           (``WAITS ON replan``). A spec/plan session to the caps
                           (:func:`finish_first`, :func:`build_cap`). An open Task whose PR is
                           still in flight (PUSHED, in review, or waiting on a gate/merge) holds
                           the row itself — ``WAITS ON <task> #<pr>`` — until that PR merges or
                           closes (:func:`_tasks_in_flight`): a replan must not rewrite the Task
                           a PR is about to land under
    DELIVERY → PLAN        a delivery lead (``delivers:``) with no plan yet: one session plans
                           every member as one document (brief ``delivery-plan``)
    DELIVERY → CODE        a delivery lead whose plan is approved: one session builds every open
                           member (brief ``delivery-code``) — unless the union of their
                           ``writes:`` overlaps a running Task's, then ``WAITS ON <other>``.
                           Under ``conventions.delivery: feature`` a Feature's Tasks are such a
                           delivery, led by the first Task of each slice
                           (:mod:`asf.record.slice`): the lead's ``after:`` inside the delivery
                           is commit order, not a hold, and a branch the lane held
                           ``incomplete`` (a crash, a run cap, ``status: partial``) comes back
                           as this row with the hold's text — the session continues from the
                           branch's head
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
import copy
import dataclasses
import datetime as dt
import math
import re

from asf import amendable, budget
from asf.feeder import footprint
from asf.groom import policy as groom_policy
from asf.record import replan as replan_mod
from asf.views import index_reader as ix

BUG_FIX = 'BUG → FIX'
FIX_CORRECT = 'FIX → CORRECT'
#: == asf.workers.lifecycle.ROUND_CAP (the feeder imports no git module): holds in a row
#: on the SAME finding before a correction becomes the ADJUDICATE row (operator policy 2026-09-27)
CORRECTION_ROUNDS = 3
#: == asf.workers.lifecycle.NAMING: the lane rewords a naming refusal itself; one it could not
#: goes back to a session as a correction and never to adjudicate, whatever the item's rounds
NAMING = 'naming'
#: == asf.workers.lifecycle.COPIES: the lane's trunk-copies rebuild conflicted — a rebase for a
#: correct session, never adjudicate
COPIES = 'copies'
FOOTPRINT = 'footprint'  # == asf.workers.lifecycle.FOOTPRINT: a correction widen_footprint answers
#: == asf.workers.lifecycle.INCOMPLETE: the lane held a delivery branch a member of which no
#: commit names — the same lead comes back as a DELIVERY → CODE row and continues from the head
INCOMPLETE = 'incomplete'
STALEMATE = 'STALEMATE → ADJUDICATE'
CONFLICT = 'CONFLICT → REBASE'
STALE = 'STALE → CLOSE'
CARD_SPEC = 'CARD → SPEC'
#: a small Feature's one document session (``size: s``, :func:`is_small`)
SPEC_PLAN = 'CARD → SPEC+PLAN'
#: a ``lane: direct`` Feature's one session (:func:`is_direct`)
DIRECT_BUILD = 'DIRECT → BUILD'
#: the brief kinds of those two rows, and the branch kind of the direct lane
SPEC_PLAN_KIND = 'spec-plan'
DIRECT = 'direct'
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
#: a pushed item sent BACK with no correction pending (:func:`pushed_rows`)
WAITS_LANE = 'WAITS ON lane'
#: an Epic past its typed budget (F-0052): this module owns the action word, asf.budget the money
WAITS_BUDGET = 'WAITS ON budget'
#: ``priority: later`` on an item, its Feature or its Epic: the product put the work aside
#: (:func:`hold_shelved`) — its new work waits, and so does every row waiting on it
LATER = 'later'
WAITS_LATER = 'WAITS ON later'
PLAN_CODE = 'PLAN → CODE'
#: A Task (or delivery) whose ``writes:`` reaches the amendable set (:mod:`asf.amendable`): no
#: worker session may edit that set, so none is launched to be refused — the console makes the
#: edit on the item's branch (T-0183, T-0259, T-0288, T-0301, T-0303 on 2026-09-27).
CONSOLE_AMEND = 'CONSOLE → AMEND'
RESHAPE = 'RESHAPE → PLAN'
#: a Feature's pending ``reshape:`` (:mod:`asf.record.replan`): one session re-plans its open Tasks
REPLAN = 'RESHAPE → REPLAN'
REPLAN_KIND = 'replan'
#: the ``waits_on`` of a code row whose Feature waits on its replan
WAITS_REPLAN = 'replan'
#: a delivery lead's document session (:func:`delivery_rows`) — no plan yet
DELIVERY_PLAN = 'DELIVERY → PLAN'
#: a delivery lead's build session — plan approved, every open member built in one branch
DELIVERY_CODE = 'DELIVERY → CODE'
GROOM_ADJUDICATE = 'GROOM → ADJUDICATE'
#: the groom day's clerical half (F-0093 §2.4): the ``inbox:`` lines, a cheap session of their own
GROOM_CLERK = 'GROOM → CLERK'
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
MAX_REPLANS_IN_FLIGHT = 6  #: `conventions.feeder.max_replans_in_flight` (:func:`finish_first`)
#: `conventions.feeder.max_features_in_build` (:func:`build_cap`): a whole number, or ``auto``
AUTO = 'auto'
MAX_FEATURES_IN_BUILD = AUTO
#: `conventions.feeder.features_per_session`: ``auto``'s Features in build per session slot
FEATURES_PER_SESSION = 2
#: ``auto`` never sizes the cap below this: a product with its bandwidth all but gone still
#: finishes two Features side by side, so one stuck Feature never idles the product
MIN_FEATURES_IN_BUILD = 2
#: the stages of a Feature whose plan is approved and whose Tasks are the work
BUILD_STAGES = ('plan-approved', 'building')
#: the rows that start a new document — or a whole direct Feature: what the finish-first cap
#: counts and holds
NEW_DOC_KINDS = (CARD_SPEC, STARVED_SPEC, STARVED_PLAN, SPEC_PLAN, DIRECT_BUILD, REPLAN)
#: the session kinds (a run's brief kind) the finish-first cap counts as in flight
NEW_DOC_SESSIONS = ('spec', 'plan', SPEC_PLAN_KIND, DIRECT, REPLAN_KIND)
FINISH = 'WAITS ON finish'
#: the rows that write a spec or a plan: what ``flags.plan_ahead`` meters (:func:`plan_ahead_cap`)
SPEC_PLAN_KINDS = (CARD_SPEC, STARVED_SPEC, STARVED_PLAN, SPEC_PLAN)
#: the session kinds :func:`plan_ahead_cap` counts as spec/plan work already in flight
SPEC_PLAN_SESSIONS = ('spec', 'plan', SPEC_PLAN_KIND)
#: the action of a spec/plan row :func:`plan_ahead_cap` holds
WAITS_BUILD_SLOT = 'WAITS ON build slot'
#: The kinds ``candidates`` caps at ``attempt_limit`` (F-0080 §2.6, P10) — §1.1's enumeration.
#: Not here: ``BUG → FIX`` (``bug_rows`` caps it itself), ``FIX → CORRECT`` (it carries its own
#: ``CORRECTION_ROUNDS``, and a correction is an answer the harvest asked for, not an attempt the
#: factory chose), ``STALEMATE`` and ``GROOM → ADJUDICATE`` (already the adjudicate row).
CAPPED_KINDS = frozenset({CARD_SPEC, STARVED_SPEC, STARVED_PLAN, PLAN_CODE, CONFLICT, STALE})
REVIEW_RE = re.compile(r'^(spec|plan)-review r(\d+)')
CLOSED_PR_RE = re.compile(r'\bPR #\d+ CLOSED\b')
PR_RE = re.compile(r'\bPR #\d+\b')
#: ingest's line for a PR the host reports open on the item's branch
OPEN_PR_RE = re.compile(r'\bPR #(\d+) OPEN\b')
#: the occupancy keys that say an item's work is pushed: a lane state, a wait on the lane, a
#: correction (:func:`pushed_ids`)
PUSHED_KEYS = ('review', 'landing', 'waiting_landing', 'corrections', 'back')
#: ingest's line for a spec that sits on a branch, not the trunk (``spec on <branch>[ (review …)]``)
SPEC_ON_BRANCH_RE = re.compile(r'^spec on (?!origin/)(\S+)')
#: ingest's line for a plan that sits on a branch, not the trunk (``plan on <branch>[ (review …)]``)
PLAN_ON_BRANCH_RE = re.compile(r'^plan on (?!origin/)(\S+)')
PLAN_ON_TRUNK = 'plan on origin/main'
CONFLICTING = 'CONFLICTING'
#: The RESHAPE → PLAN reason for a Task whose card carries no `writes:` — a Task migrated from a
#: plan written before the machine-read lines existed. The reshape brief keys its second mode on
#: the leading `no writes:` (F-0126 D4).
NO_WRITES_RECUT = ('no writes: declared — this Task came from a plan section with no '
                   'stories:/writes:/after: lines; re-cut that section so it carries them')
#: The ``reshape_declined:`` entry a Task carries once a reshape session answered that it does
#: not split (:mod:`asf.tick.rejudge`): it stays whole. A no-writes re-cut is not launched again —
#: the same session would buy the same answer — and the Task waits on its ``writes:`` in its own
#: row; a footprint the Task outgrows is widened, never reshaped (:func:`asf.feeder.widen.decide`).
WHOLE = 'no split'


def recut_declined(item):
    """True when ``item``'s reshape was answered "does not split" already (:data:`WHOLE`)."""
    return WHOLE in ((item or {}).get('reshape_declined') or ())


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
    #: a CONSOLE → AMEND row only: the ``writes:`` entry that reaches the amendable set
    amend: str = ''
    #: a FIX → CORRECT row an operator ruling raised (``asf correct`` at the cap): one code
    #: session carries it out — the attempt cap never turns it into an adjudication
    ruling: bool = False

    @property
    def launches(self):
        return self.action.startswith(LAUNCH)


# ---- inputs -----------------------------------------------------------------

#: the index reader's live map: removed cards dropped, the done ones kept in ``retired_done``
Items = ix.Items


def items_of(index):
    """The live ``{id: item}`` map (an :class:`Items`) from an ``index.json`` dict or an
    already-loaded item map; a removed card is dropped, and remembered in ``retired_done`` when
    it is done (:func:`asf.views.index_reader.live`)."""
    raw = index.get('items') if isinstance(index.get('items'), dict) else index
    kept = ix.live(raw)
    out = Items(with_blockers(dict(kept), raw))
    out.retired_done = kept.retired_done
    out.retired_open = kept.retired_open
    return out


def with_blockers(items, raw=None):
    """``items`` with ``blocked`` / ``blocked_by_open`` derived from each card's own ``blockedBy``
    against the map's states — the ingest's rule (:func:`asf.evidence.evidence.blocked_of`), read
    here too, because a ``blockedBy`` set after the last ingest (``asf set``) reaches the index
    with no ``blocked`` beside it and would launch a blocked item (F-1129). A card with no
    ``blockedBy`` keeps what it carries. Changed cards are copies; the input is not mutated."""
    from asf.evidence.evidence import blocked_of
    states = None
    out = items
    for iid, v in items.items():
        if 'blockedBy' not in v:
            continue
        if states is None:
            src = raw or items
            # a removed card the index reader set aside (``retired_done``) still answers with
            # its state: a blocker that landed and was then retired does not block
            states = {k: (w or {}).get('state', 'New')
                      for k, w in list(getattr(src, 'retired_done', {}).items()) + list(src.items())
                      if isinstance(w, dict)}
        blocked, open_blockers = blocked_of(v.get('blockedBy'), states)
        if bool(v.get('blocked')) == blocked and list(v.get('blocked_by_open') or ()) == open_blockers:
            continue
        w = dict(v)
        if blocked:
            w.update(blocked=True, blocked_by_open=open_blockers)
        else:
            w.pop('blocked', None)
            w.pop('blocked_by_open', None)
        if out is items:
            out = dict(items)
        out[iid] = w
    return out


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


def trunk_of(product):
    """``conventions.main`` (default ``conventions.DEFAULT_MAIN``): the product's trunk name."""
    return _conventions(product).main


def plan_on_trunk(product):
    """Ingest's line for a plan that landed on the trunk (:data:`PLAN_ON_TRUNK` for a product
    that names none): ``plan on origin/<trunk>``, the product's own trunk name, not always
    ``main`` (§2.5)."""
    return f'plan on origin/{trunk_of(product)}'


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


def max_replans_in_flight(product):
    """``conventions.feeder.max_replans_in_flight`` (default 6): the most replan sessions a
    product runs at once outside the spec/plan cap, while seats are free (:func:`finish_first`)."""
    feeder = _conventions(product).get('feeder')
    v = feeder.get('max_replans_in_flight') if isinstance(feeder, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 \
        else MAX_REPLANS_IN_FLIGHT


def max_features_in_build(product):
    """``conventions.feeder.max_features_in_build`` (default ``auto``): the most Features a
    product builds at once (:func:`build_cap`) — a whole number >= 1, or ``auto``
    (:func:`features_cap`). Anything else reads as the default."""
    feeder = _conventions(product).get('feeder')
    v = feeder.get('max_features_in_build') if isinstance(feeder, dict) else None
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    return MAX_FEATURES_IN_BUILD


def plan_ahead(product):
    """``flags.plan_ahead`` (:meth:`asf.env.Product.flag`): how many Features the product may
    have specified or planned ahead of its build slots (:func:`plan_ahead_cap`) — a whole number
    >= 0. Unset, or anything else: None — no limit, every decided card is specified now."""
    v = _conventions(product).flag('plan_ahead')
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


#: ``flags.roots`` (W8-PR1g): dependency roots decided by code — off unless set ``on``/``true``
ROOTS_UNVERIFIED_HOURS = 24   #: ``flags.roots_unverified_hours``: an unverified landing decides
ROOTS_PARK_STALE_DAYS = 3     #: ``flags.roots_park_stale_days``: a park this old surfaces
ROOTS_MIN_DEPENDANTS = 5      #: ``flags.roots_min_dependants``: rows behind a park that surface it


def roots_on(product):
    """``flags.roots`` is ``on`` (or true): the three dependency-root rules run — an ``after:`` on
    an unverified landing the trunk carries is satisfied (:func:`hold_unlanded`), an unverified
    landing decides itself (:func:`asf.groom.policy.decide_unverified_landing`), a stale park
    with rows behind it surfaces (:func:`surface_stale_parks`). Unset or anything else: off."""
    if product is None:
        return False
    v = _conventions(product).flag('roots')
    return v is True or str(v).strip().lower() in ('on', 'true', 'yes')


#: ``flags.console_wait``: ``hold`` (default) — an item waiting on the console orders its
#: dependants as any unlanded item does; ``aside`` — it orders nothing (:func:`console_only`)
CONSOLE_WAIT_HOLD, CONSOLE_WAIT_ASIDE = 'hold', 'aside'


def console_wait(product):
    """``flags.console_wait`` — :data:`CONSOLE_WAIT_ASIDE` when set so, else
    :data:`CONSOLE_WAIT_HOLD` (unset, ``hold`` or anything else: today's ordering)."""
    v = _conventions(product).flag('console_wait') if product is not None else None
    return CONSOLE_WAIT_ASIDE if str(v or '').strip().lower() == CONSOLE_WAIT_ASIDE \
        else CONSOLE_WAIT_HOLD


def console_only(rows):
    """The item ids whose every row is a CONSOLE → AMEND: work only the console can move, put
    aside (W8-PR1). Under ``console_wait: aside`` such an item no longer orders another row
    (:func:`after_of` drops it, :func:`candidates` sets it on the map as ``console_aside``); an
    item with any other row — a landing, a review — keeps its ``after:`` edges. The groom rows speak for a day's questions, not for the
    item they name, so they never count. I3's overlap check is not touched: a dependant whose
    ``writes:`` overlaps a staged item is still refused at launch."""
    by = {}
    for r in rows:
        if r.kind in (GROOM_ADJUDICATE, GROOM_CLERK):
            continue
        by[r.item_id] = by.get(r.item_id, True) and r.kind == CONSOLE_AMEND
    return {i for i, only in by.items() if only}


def _whole_flag(product, name, default):
    v = _conventions(product).flag(name) if product is not None else None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def roots_settings(product):
    """``(unverified_hours, park_stale_days, min_dependants)`` of ``flags.roots_*``, each a whole
    number >= 0, else its default."""
    return (_whole_flag(product, 'roots_unverified_hours', ROOTS_UNVERIFIED_HOURS),
            _whole_flag(product, 'roots_park_stale_days', ROOTS_PARK_STALE_DAYS),
            _whole_flag(product, 'roots_min_dependants', ROOTS_MIN_DEPENDANTS))


def features_per_session(product):
    """``conventions.feeder.features_per_session`` (default 2): ``auto``'s Features in build per
    session slot — a number > 0."""
    feeder = _conventions(product).get('feeder')
    v = feeder.get('features_per_session') if isinstance(feeder, dict) else None
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    return v if ok else FEATURES_PER_SESSION


def features_cap(product, capacity, bandwidth=None):
    """``(N, why)``: how many Features this product builds at once, and the inputs that set it.

    A fixed ``feeder.max_features_in_build`` is N as it stands. ``auto`` derives N every tick
    from the bandwidth (``bandwidth``: :func:`asf.capacity.bandwidth` — ``sessions``,
    ``accounts``, ``quota_stopped``, ``ci_free``; an absent fact is ``None``): ``sessions ×
    features_per_session``, scaled by the share of accounts not at their quota stop (unless
    ``quota_in_sessions``: the sessions are a fair share of the usable slots already), halved
    while CI has no free slot, rounded up, and never below :data:`MIN_FEATURES_IN_BUILD`. No
    ``sessions`` fact: ``capacity``, the session slots this plan cuts to."""
    fixed = max_features_in_build(product)
    if fixed != AUTO:
        return fixed, 'feeder.max_features_in_build'
    bw = bandwidth or {}
    sessions = bw.get('sessions')
    sessions = sessions if isinstance(sessions, int) and sessions >= 0 else max(int(capacity), 0)
    n = sessions * features_per_session(product)
    accounts, stopped, ci_free = bw.get('accounts'), bw.get('quota_stopped'), bw.get('ci_free')
    # a fair share is cut from the usable slots, which already give a stopped account none:
    # scaling it by the stopped share again counts the stop twice
    if (isinstance(accounts, int) and accounts > 0 and isinstance(stopped, int)
            and not bw.get('quota_in_sessions')):
        n = n * max(accounts - stopped, 0) / accounts
    if isinstance(ci_free, int) and ci_free <= 0:
        n = n / 2
    cap = max(MIN_FEATURES_IN_BUILD, math.ceil(n))

    def said(v):
        return '?' if v is None else str(v)
    return cap, (f"auto: sessions {sessions}, quota-stopped {said(stopped)}"
                 f"{'' if accounts is None else f'/{accounts}'}, CI free {said(ci_free)}")


def decision_rows(product):
    """``conventions.decision_rows`` (default 5): how many undecided cards get a row (D6)."""
    v = _conventions(product).get('decision_rows')
    return v if isinstance(v, int) and v >= 0 else DECISION_ROWS


# ---- per-item predicates ----------------------------------------------------

def landed_ids(items, landed_shas=None, on_trunk=()):
    """Ids that count as landed: the record's Resolved/Closed, plus every id the trunk names.

    The two disagree when a Task's work reached the trunk under a sibling's commit, or when the
    ingest has not caught up with a harvest. `after:` must not hold a successor behind a
    predecessor whose work is *already there* — a coder launched into that gap finds the surface
    present and writes nothing (B-0076, F-0095).

    The fold runs over *all* items, not ``ix.feature_tasks``, so an ``after:`` naming a Task of
    another Feature is answerable at all. No ``landed_shas`` (the fact is absent): the record's
    set alone, exactly as before. A removed card that is done counts too
    (:attr:`Items.retired_done`): groom retires a card that landed, and its successors must not
    wait on it for ever (a product's T-0360).

    ``on_trunk`` (``flags.roots``; :func:`candidates` sets it on the map as ``trunk_unverified``):
    the open items whose recorded landing is on ``origin/<main>`` though not verified as theirs
    (:func:`asf.tick.step_wave.unverified_on_trunk`) — the work is on the trunk by the host's
    word, so an ``after:`` on one is answered; the item's own NEEDS DECISION row stays for the
    person (a product's T-0091 held 19 rows while its landing was disputed).
    """
    done = {i for i, v in items.items() if v.get('state') in DONE_STATES}
    return (done | set(getattr(items, 'retired_done', ())) | set(landed_shas or {})
            | set(on_trunk or ()) | set(getattr(items, 'trunk_unverified', ())))


#: a removal line that names the card carrying the removed card's work on: a groom merge's
#: (:func:`asf.groom.groom.merge_tasks`, ``merged into T-0158 (…)``) or a duplicate's
#: (``duplicate of T-0305 (same parent and footprint) (…)``)
MERGED_INTO_RE = re.compile(r'\b(?:merged into|duplicate of)\s+([A-Za-z]-\d{4})\b')


def _retired(items):
    """Every removed card the index reader set aside: done (``retired_done``) or not
    (``retired_open``)."""
    return {**getattr(items, 'retired_open', {}), **getattr(items, 'retired_done', {})}


def absorbers(items):
    """``{merged id: absorber id}`` off every card's ``merged:`` list — live, and removed
    (:attr:`Items.retired_done`, :attr:`Items.retired_open`), since an absorber may be removed
    in turn, merged into a third card — and off every removed card's own ``removed: merged into
    <id>`` (or ``duplicate of <id>``) line. A groom merge removes the merged card, and the index reader drops a removed
    card, so an ``after:`` naming it would name an id that never lands again (a product's T-0163
    waited on T-0162, merged into T-0159, itself merged into T-0158 — Closed)."""
    out = {}
    retired = _retired(items)
    for v in list(items.values()) + list(retired.values()):
        for m in v.get('merged') or ():
            if isinstance(m, str):
                out.setdefault(m, v.get('id'))
    for iid, v in retired.items():
        m = MERGED_INTO_RE.search(str(v.get('removed') or ''))
        if m and m.group(1).upper() != iid:
            out.setdefault(iid, m.group(1).upper())
    return out


def after_of(items, item, absorbed=None, keep_aside=False):
    """``item``'s ``after:`` with each merged-away id read as the card that absorbed it (a chain
    followed to its end); an absorber the item itself is dropped — it does not wait on its own
    work. A dependency on a card groom removed outright — not done, merged into nothing — is a
    dead edge and dropped (:func:`dead_after`): its work is not coming, so nothing may wait on
    it. A chain that ends on a removed card that never landed is kept: the scope folded into it
    is still the Feature's work, and :func:`orphaned_after` names it for a decision.

    An item the map sets aside as ``console_aside`` (``flags.console_wait: aside``,
    :func:`console_only` — its only row waits on the console) is dropped too: it orders nothing
    (W8-PR1). ``keep_aside``: keep it — the edge as the record has it."""
    absorbed = absorbers(items) if absorbed is None else absorbed
    out = []
    gone = getattr(items, 'retired_open', {})
    aside = () if keep_aside else getattr(items, 'console_aside', ())
    for a in item.get('after') or ():
        seen, hops = {a}, 0
        while a in absorbed and absorbed[a] not in seen:
            a = absorbed[a]
            seen.add(a)
            hops += 1
        if a in gone and not hops:
            continue  # removed outright: a dead edge (dead_after says so)
        if a != item.get('id') and a not in out and a not in aside:
            out.append(a)
    return out


def dead_after(items, item, absorbed=None):
    """``[(id, why)]``: the ``after:`` entries :func:`after_of` drops as dead — a card groom
    removed outright, not done and merged into nothing, so its work is never coming."""
    absorbed = absorbers(items) if absorbed is None else absorbed
    gone = getattr(items, 'retired_open', {})
    return [(a, str(gone[a].get('removed') or 'removed'))
            for a in item.get('after') or () if a in gone and a not in absorbed]


def orphaned_after(items, ids):
    """The first of ``ids`` (an :func:`after_of` list) that is a removed card never landed — a
    merge survivor groom removed in turn — with its removal line, else ``(None, '')``."""
    gone = getattr(items, 'retired_open', {})
    for a in ids:
        if a in gone:
            return a, str(gone[a].get('removed') or 'removed')
    return None, ''


def is_open(item):
    return item.get('state', 'New') not in DONE_STATES


def is_direct(feature):
    """True for a ``lane: direct`` Feature — one session builds it end to end."""
    return str((feature or {}).get('lane') or '').strip().lower() == DIRECT


def is_small(feature):
    """True for a ``size: s`` Feature on the full lane — spec and plan in one session."""
    return (str((feature or {}).get('size') or '').strip().lower() == 's'
            and not is_direct(feature))


def feature_delivery(lead):
    """True for a Feature delivery's slice (:mod:`asf.record.slice`, ``delivery: feature``): a
    Task that leads a ``delivers:`` list. Its plan is its Feature's, already approved, so its
    row is DELIVERY → CODE whatever stage the lead's own card carries."""
    return bool(lead) and lead.get('type') == 'task' and bool(lead.get('delivers'))


def delivery_lead_of(items, item):
    """The lead of the delivery ``item`` is in — itself when it leads, its ``delivered_by``
    otherwise — or None."""
    if item.get('delivers'):
        return item['id']
    lead = item.get('delivered_by')
    return lead if lead in items else None


def delivered(items, item, landed_shas=None):
    """True when a delivery speaks for ``item``: it is in one whose lead is still open and not
    yet on the trunk. A member whose lead is done (the branch landed without it, F-0102 D11)
    is residual: it goes back to its own lane at once, no groom pass needed."""
    lead = delivery_lead_of(items, item)
    if not lead:
        return False
    return is_open(items[lead]) and lead not in (landed_shas or {})


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


def idle_branch(item):
    """True for an Active Task whose branch nobody is moving: no PR names it in the evidence,
    and (the caller's part) no session, no lane state, no correction on it. Nothing pushes
    such a branch forward — the lane prints ``empty branch — waits`` for ever (a product's
    T-0362 and T-0371, 2026-09-26: cloud runs dead on quota with nothing past the trunk) — so
    the coder is launched again on the same branch, its worktree kept as it stands."""
    return (item.get('type') == 'task' and item.get('state') == 'Active'
            and not item.get('blocked')
            and not any(PR_RE.search(str(e)) for e in item.get('evidence') or []))


def _branch_of(item, product, kind):
    branches = (item.get('links') or {}).get('branches') or []
    lane = 'code' if kind == 'task' else kind  # B-0067: a Task's branch is the code lane's
    return branches[0] if branches else branch_for(product, lane, item['id'])


# ---- rows -------------------------------------------------------------------

def _age_key(item):
    """Older card first: an ISO timestamp sorts as text; a card with none goes last."""
    return item.get('created') or item.get('stage_since') or '~'


def bug_rows(items, product, busy, attempts=None, why=None):
    """Within a tier: fewest attempts, then the older card, then id. ``attempts`` is ``{id: sessions
    the registry holds}``, ended or not. A Bug at the limit gets one adjudicate row — its session
    is the next attempt, so the row is gone once it has run.

    A decided, open S1/S2 Bug is never silent (inbox "NEXT drops S1/S2 bugs silently"): each
    branch that gives it no session is a non-launching ``WAITS ON`` row that says why — busy
    (``why``: ``{id: what holds it}``, e.g. a live session or work waiting to land), blocked,
    Active (its fixer branch is the work), or past the attempt limit (a person decides).
    :func:`candidates` drops such a row when another row already speaks for the item."""
    attempts, why, out = attempts or {}, why or {}, []
    limit = attempt_limit(product)
    bugs = sorted(ix.of_type(items, 'bug'), key=lambda v: (attempts.get(v['id'], 0), _age_key(v), v['id']))
    for b in bugs:
        sev = b.get('severity')
        if sev not in ('S1', 'S2') or b.get('decided') is not True or not is_open(b) \
                or b.get('delivers') or b.get('delivered_by'):
            continue
        tier = 0 if sev == 'S1' else 1
        f = feature_of(items, b)
        fid, n = f['id'] if f else '', attempts.get(b['id'], 0)

        def waits(on, reason, bug=b, fid=fid, tier=tier):
            return Row(tier=tier, kind=BUG_FIX, item_id=bug['id'], feature_id=fid,
                       action=f'WAITS ON {on}', brief_kind='fix-bug',
                       branch=branch_for(product, 'fix', bug['id']),
                       reason=f"{bug.get('severity')} open, decided: {reason}", waits_on=on)
        row = foreign_row(product, b, tier, fid, 'fix-bug', BUG_FIX)
        if row is not None:
            out.append(row)
            continue
        if b['id'] in busy:
            held = why.get(b['id']) or 'session running'
            on = 'landing' if 'land' in held else 'session'
            out.append(waits(on, held))
            continue
        if b.get('blocked'):  # B-0058: a blocked Bug waits like a blocked Feature
            blockers = list(b.get('blocked_by_open') or ())
            on = blockers[0] if blockers else 'blocked'
            out.append(waits(on, 'blocked by ' + (', '.join(blockers) or 'an open item')))
            continue
        # an Active Bug has a fixer branch/PR already: CONFLICT/STALE rows speak for it
        if b.get('state') == 'Active':
            out.append(waits('branch', 'Active — its fixer branch/PR is the work, no session '
                                       'or branch row holds it'))
            continue
        if n > limit:
            out.append(waits('operator', f'adjudicated after {n} sessions (limit {limit}): '
                                         'a person decides'))
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


def console_amend_row(product, item_id, feature_id, writes, branch, brief_kind='task',
                      tier=2):
    """The CONSOLE → AMEND row for an item whose ``writes:`` reaches the amendable set
    (:func:`asf.amendable.reaches`, a glob intersection), else None. It launches nothing and
    claims no footprint: a worker would only be refused ``touch_amendable_set`` by the hook, its
    session spent and the item stalled until the console made the edit anyway."""
    hit = amendable.reaches(product, list(writes or ()))
    if not hit:
        return None
    return Row(tier=tier, kind=CONSOLE_AMEND, item_id=item_id, feature_id=feature_id,
               action=f'WAITS ON console: amendable {hit}', brief_kind=brief_kind, branch=branch,
               reason=f'writes: {hit} is in the amendable set — no worker session edits it; the '
                      f'console makes the edit on {branch}',
               waits_on='console', amend=hit)


def foreign_row(product, item, tier, feature_id, brief_kind, kind):
    """The NEEDS DECISION row for an item flagged as another product's work (``belongs_to:``,
    F-0120), else None. It launches nothing and costs no slot (:mod:`asf.feeder.tiers`), so an
    S1 flagged this way no longer holds the tier-2 rows behind it."""
    name = (item or {}).get('belongs_to')
    if not name:
        return None
    return Row(tier=tier, kind=kind, item_id=item['id'], feature_id=feature_id,
               action=NEEDS_DECISION, brief_kind=brief_kind,
               branch=branch_for(product, 'fix' if kind == BUG_FIX else 'spec', item['id']),
               reason=f"looks like {name}'s work, not this product's: "
                      f"asf move {item['id']} --to {name} (or --remove)",
               waits_on='decision')


def footprint_row(item, product, c, tier, fid, branch, items=None):
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
    elif verdict == 'waits' and not _owner_done(items, detail):
        action, waits, why = f'WAITS ON {detail}', detail, f'footprint widening overlaps {detail}'
    else:
        action, waits, why = 'WAITS ON widen_footprint', 'widen', \
            f"footprint needs {' '.join(c.get('needs') or ())}: the rule decides next tick"
    return Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid, action=action,
               brief_kind='correct', branch=branch, reason=why, waits_on=waits)


def _owner_done(items, owner):
    """True when ``owner`` is a card the record holds that is Resolved, Closed or removed: it holds
    no footprint, so a ``waits`` verdict stored on it is stale (the tick re-decides it)."""
    card = (items or {}).get(owner)
    return bool(card) and (bool(card.get('removed')) or not is_open(card))


def landed_doc(item, product, c):
    """True for a :data:`LANDING_GATE` correction on a spec or plan branch whose document the
    ingest already reads on the trunk (``<doc> on origin/<trunk>``): the refusal was answered by
    the landing itself (a product's F-0090: a settled spec hold still spoke for the Feature two
    days after its spec and plan landed, so the Feature got no row)."""
    if (c or {}).get('kind') != LANDING_GATE:
        return False
    doc = _conventions(product).branch_kind(c.get('branch') or '')
    return doc in ('spec', 'plan') and \
        f'{doc} on origin/{trunk_of(product)}' in (item.get('evidence') or ())


def correction_rows(items, product, busy, corrections):
    """``corrections`` is ``{item: {kind, text, rounds, same, at, branch, ruled}}`` — a branch
    the harvest held (the row runs on that branch when it is given). ``same`` is how many holds in
    a row name this one finding (:func:`asf.workers.lifecycle.repeats`; a caller that passes none
    is read by its ``rounds``): fewer than 3 — the first hold, or a correct that failed it once,
    or a new finding after any number of rounds — or a correction ``ruled`` (written after an
    adjudication, not its cap hold): a FIX → CORRECT row in the item's severity tier; otherwise
    3 or more — CORRECT failed twice on the same finding — the ADJUDICATE row.
    Returns ``(rows, ids)``; ``ids`` are the items these rows speak for."""
    out, ids = [], set()
    for iid, c in sorted((corrections or {}).items()):
        item = items.get(iid)
        if not item or not c or not c.get('text') or not is_open(item) or iid in busy:
            continue
        if item.get('blocked'):  # B-0058: a blocked item gets no correction or adjudicate row either
            continue
        if landed_doc(item, product, c):
            # the document the lane refused has since landed: the hold is over, and a Feature it
            # still spoke for would get no row at all — its plan, its replan, its Tasks
            continue
        ids.add(iid)
        f = feature_of(items, item)
        fid, rounds = f['id'] if f else '', c.get('rounds') or 0
        same = c['same'] if c.get('same') is not None else rounds
        tier = {'S1': 0, 'S2': 1}.get(item.get('severity'), 2)
        kind = 'fix' if item['type'] == 'bug' else 'task'
        branch = c.get('branch') or branch_for(product, kind, iid)
        if c.get('parked'):  # a Task that wrote nothing twice waits for a person, not a session
            out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid,
                           action=f'{PARKED} {c.get("reason") or c["kind"]}', brief_kind='correct',
                           branch=branch, reason=c.get('reason') or 'parked', waits_on='operator'))
            continue
        # a correction of an item whose writes: reach the amendable set is the console's too: a
        # session relaunched on it only buys the hook's refusal again (T-0303, 2026-09-27)
        amend = console_amend_row(product, iid, fid, item.get('writes'), branch, 'correct', tier)
        if amend:
            out.append(amend)
            continue
        if c.get('operator_ruling'):
            # ``asf correct`` at the cap: ONE code session on the Task's own branch carries the
            # ruling out — never an adjudication, never a reshape on a spec or plan branch
            if _conventions(product).branch_kind(branch) in ('spec', 'plan'):
                branch = branch_for(product, 'fix' if kind == 'fix' else 'code', iid)
            out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid,
                           action=LAUNCH, brief_kind='correct', branch=branch,
                           correction=c['text'], ruling=True,
                           reason="operator ruling at the round cap: one code session "
                                  "carries it out"))
            continue
        doc = product.conventions.branch_kind(branch) if c.get('kind') == LANDING_GATE else None
        if doc in ('spec', 'plan') and same < CORRECTION_ROUNDS:  # a document the gate refused
            out.append(Row(tier=tier, kind=STARVED_SPEC if doc == 'spec' else STARVED_PLAN,
                           item_id=iid, feature_id=fid or iid, action=LAUNCH, brief_kind=doc,
                           branch=branch, correction=c['text'],
                           reason=f"the lane held it ({c['kind']}), round {rounds}: the {doc} "
                                  f"cannot land as it stands"))
            continue
        if c.get('kind') == FOOTPRINT and c.get('verdict') != 'widen':
            out.append(footprint_row(item, product, c, tier, fid, branch, items=items))
            continue
        if same >= CORRECTION_ROUNDS and c.get('kind') not in (NAMING, COPIES) \
                and not c.get('ruled'):  # an adjudication's instruction: a session carries it out
            if c.get('settled'):  # B-0128: already ruled at this hold — no second adjudicate
                prs = c.get('prs') or ()
                action = f"{WAITS_MERGE}: {', '.join(f'#{n}' for n in prs)}" if prs else WAITS_MERGE
                out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid,
                               action=action, brief_kind='correct', branch=branch,
                               reason=f"adjudicated ({c.get('kind')}): waits on the PR to merge "
                                      f"or close, or a new push", waits_on='merge'))
                continue
            out.append(Row(tier=tier, kind=STALEMATE, item_id=iid, feature_id=fid, action=LAUNCH,
                           brief_kind='adjudicate', branch=branch, correction=c['text'],
                           reason=f"held {same} times on the same finding ({c.get('kind')}): "
                                  f"adjudicate, not another correction"))
        elif c.get('kind') == INCOMPLETE and item.get('delivers'):
            # a delivery branch a member of which no commit names: the same lead, the same
            # brief, the hold's text under it — the session continues from the branch's head
            f = _task_feature(items, item) if item['type'] == 'task' else feature_of(items, item)
            out.append(Row(tier=tier, kind=DELIVERY_CODE, item_id=iid,
                           feature_id=f['id'] if f else fid, action=LAUNCH,
                           brief_kind='delivery-code', branch=branch, correction=c['text'],
                           reason=f"the lane held it ({INCOMPLETE}), round {rounds}: continue "
                                  f"the delivery from the head of {branch}"))
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
    from asf.harvest.lane import docs_only_task, is_pr_item  # local: the lane imports the feeder
    for iid, h, review in sorted(held, key=lambda t: t[0]):
        item = items.get(iid)
        foreign = not item and is_pr_item(iid)  # merge: auto — a PR no factory item made
        if foreign:
            if iid in busy:
                continue
            item = {'id': iid, 'type': 'task'}
        elif not item or not is_open(item) or iid in busy or item.get('blocked'):
            continue
        direct = (item.get('type') == 'feature'
                  and _conventions(product).branch_kind(h.get('branch') or '') == DIRECT)
        if item.get('type') not in ('task', 'bug') and not direct:
            continue
        f = None if foreign else feature_of(items, item)
        fid, branch, number = (f['id'] if f else ''), h.get('branch') or '', h.get('pr')
        what = f'PR #{number}' if number else branch
        if foreign:
            what += ' (opened outside the factory, no card)'
        if review and not foreign and docs_only_task(product, items, iid):
            review = False  # docs-only ``writes:``: no review session (flags.docs_review)
            h = dict(h, state='REVIEW', why='docs-only writes: no review (flags.docs_review)')
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


def branch_rows(items, product, busy, held=(), landed_shas=None):
    """CONFLICT → REBASE and STALE → CLOSE over Active Tasks and Bugs no session holds — and,
    for an Active Task on an idle branch (:func:`idle_branch`: no PR, and nothing in ``busy``
    or ``held`` — no session, no correction, no lane state, no landing the record has yet to
    ingest — nor on the trunk), its PLAN → CODE row again on that same branch: the coder
    continues where the dead run stopped. A Task a delivery speaks for (:func:`delivered`) is
    built there instead."""
    out = []
    landed = landed_ids(items, landed_shas)
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
        elif (idle_branch(v) and v['id'] not in held and v['id'] not in landed
              and not delivered(items, v, landed_shas)):
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=v['id'], feature_id=fid, action=LAUNCH,
                           brief_kind='task', branch=_branch_of(v, product, kind),
                           reason='Active on an idle branch: no session, no PR, nothing past '
                                  'the trunk — the coder continues on its branch, worktree kept'))
    return out


def _delivery_union(items, lead, leave_out=()):
    """The ordered union of ``writes:`` across ``lead``'s open ``delivers:`` members (the lead
    itself included, since ``delivers[0]`` is the lead) — the delivery's whole footprint.
    ``leave_out``: members not built on the delivery's branch (:func:`asf.amendable.console_members`)."""
    out = []
    for m in lead.get('delivers') or ():
        member = items.get(m)
        if member is None or not is_open(member) or m in leave_out:
            continue
        for w in member.get('writes') or ():
            if w not in out:
                out.append(w)
    return out


def deferred_members(items, lead_id, landed_shas=None):
    """The open members of ``lead_id``'s delivery (the lead aside) the delivery cannot build:
    an ``after:`` of theirs outside the delivery has not landed and itself waits — through its
    own ``after:`` chain — on a member of this delivery. Holding the whole delivery on it is a
    cycle no tick breaks (a product's T-0027 delivery: T-0030 after T-0456, T-0456 after
    T-0027). Such a member, and every member after it, is left out: the delivery lands without
    them (F-0102 D11) and they come back on their own rows once their predecessors land."""
    lead = items.get(lead_id) or {}
    members = [m for m in lead.get('delivers') or () if m in items and is_open(items[m])]
    inside = set(members)
    landed = landed_ids(items, landed_shas)
    absorbed = absorbers(items)

    def waits_on_delivery(x):
        stack, seen = [x], set()
        while stack:
            y = stack.pop()
            if y in seen:
                continue
            seen.add(y)
            for a in after_of(items, items.get(y) or {}, absorbed):
                if a in inside:
                    return True
                if a not in landed:
                    stack.append(a)
        return False
    out = []
    for m in members:
        if m == lead_id:
            continue
        deps = after_of(items, items[m], absorbed)
        if any(d in out for d in deps) or any(
                d not in inside and d not in landed and waits_on_delivery(d) for d in deps):
            out.append(m)
    return out


def left_out(product, items, lead_id, landed_shas=None):
    """``(console, deferred)``: the members of ``lead_id``'s delivery not built on its branch —
    the console's (:func:`asf.amendable.console_members`) and the ones a cycle defers
    (:func:`deferred_members`). The feeder's union and holds, the brief's list and the lane's
    completeness check all leave them out alike."""
    console = amendable.console_members(product, items, lead_id) if product is not None else []
    deferred = [m for m in deferred_members(items, lead_id, landed_shas) if m not in console]
    return console, deferred


def running_footprints(items, busy):
    """[(task_id, writes)] of every Task whose files are genuinely in play: a live session, or a
    pushed branch waiting for harvest (both in ``busy``).

    A Task that is merely ``Active`` in the index does NOT hold its footprint (B-0076): a held
    branch — gate red, correction pending, or a card whose only evidence is a branch — is not
    being written by anyone, and treating it as in flight deadlocks every sibling that shares a
    file with it. Two branches that do touch the same file still meet at the rebase, where the
    correction loop resolves it; a wait here must mean "someone is writing this now".

    A delivery lead (``delivers:``) that is open and busy holds its union footprint the same
    way — an unrelated Task sharing a file with any of its open members waits on the lead, not
    on the member (F-0102 D-line)."""
    out = []
    for t in sorted(ix.of_type(items, 'task'), key=lambda v: v['id']):
        if t['id'] in busy and t.get('writes') and is_open(t) and not t.get('removed'):
            out.append((t['id'], list(t['writes'])))
    for v in sorted(items.values(), key=lambda v: v['id']):
        if v.get('delivers') and v['id'] in busy and is_open(v):
            union = _delivery_union(items, v)
            if union:
                out.append((v['id'], union))
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


def plan_carrier(feature):
    """The branch ``feature``'s plan sits on when it is not on the trunk, from ingest's line."""
    for line in feature.get('evidence') or []:
        m = PLAN_ON_BRANCH_RE.match(str(line))
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
    # F-0093 §2.6: a starved spec already exists on its branch — the run amends it in place
    brief = 'spec-amend' if kind == STARVED_SPEC else doc
    return Row(tier=2, kind=kind, item_id=fid, feature_id=fid, action=LAUNCH, brief_kind=brief,
               branch=branch, reason=reason)


def delivery_rows(items, product, busy, running, landed_shas=None):
    """One row per delivery lead (an open card carrying ``delivers:``, in ``(rank, id)`` order),
    plus a non-launching ``WAITS ON delivery <lead>`` row for every other open member — a
    delivery speaks for its members, so they get no row of their own (:func:`feature_rows`,
    :func:`bug_rows`).

    A lead's ``open_members`` are the members of its own ``delivers:`` (the lead included — it is
    ``delivers[0]``) still open: none left, the delivery is delivered, no row at all. A blocked
    open member holds the whole delivery on that blocker. A busy lead gets no launching row of
    its own — the member rows still show ``WAITS ON delivery``. Otherwise the lead's stage picks
    the row: ``card``/``plan-draft``/``plan-review rN`` is :data:`DELIVERY_PLAN`;
    ``plan-approved``/``building …`` is :data:`DELIVERY_CODE`, on the ordered union of the open
    members' ``writes:`` (:func:`_delivery_union`) — ``footprint.first_conflict`` against
    ``running`` holds it on the other item first, else it launches and the union joins
    ``running`` so the Tasks placed after it (:func:`candidates` calls this before
    :func:`feature_rows`) see the delivery's footprint (PD10, PD11)."""
    out = []
    on_trunk = landed_shas or {}
    leads = [v for v in items.values() if v.get('delivers') and is_open(v)
             and v['id'] not in on_trunk]
    for lead in sorted(leads, key=lambda v: (ix.rank(v), v['id'])):
        lid = lead['id']
        slice_ = feature_delivery(lead)
        feature = _task_feature(items, lead) if slice_ else None
        if slice_ and not in_build_stage(feature):
            continue  # the Feature's own rows speak for it (its spec ladder, its stalemate)
        fid = feature['id'] if feature else lid
        open_members = [m for m in lead.get('delivers') or ()
                        if m in items and is_open(items[m]) and m not in on_trunk]
        if not open_members:
            continue
        stage = lead.get('stage') or 'card'
        word = stage.split(' ')[0]
        # a Feature delivery's plan is its Feature's, approved by construction (asf.record.slice)
        code_stage = slice_ or word in BUILD_STAGES
        # a member only the console may edit, or one a cross-delivery cycle defers, is not the
        # delivery's to build: each gets its own row below
        console, deferred = left_out(product, items, lid, landed_shas) if code_stage else ([], [])
        console = [m for m in console if m in open_members]
        deferred = [m for m in deferred if m in open_members]
        skip = set(console) | set(deferred)
        others = [m for m in open_members if m != lid and m not in skip]
        kind = DELIVERY_CODE if code_stage else DELIVERY_PLAN
        brief = 'delivery-code' if code_stage else 'delivery-plan'
        branch = branch_for(product, 'code' if code_stage else 'plan', lid)
        blocked_id = next((m for m in open_members if items[m].get('blocked')), None)
        if blocked_id is not None:
            blockers = list(items[blocked_id].get('blocked_by_open') or ())
            on = blockers[0] if blockers else 'blocked'
            out.append(Row(tier=2, kind=kind, item_id=lid, feature_id=fid,
                           action=f'WAITS ON {on}', brief_kind=brief, branch=branch,
                           reason='blocked by ' + (', '.join(blockers) or 'an open item'),
                           waits_on=on))
        elif lid in busy:
            pass
        elif replan_mod.pending(feature or _task_feature(items, lead)):
            f_ = feature or _task_feature(items, lead)
            out.append(replan_wait(Row(tier=2, kind=kind, item_id=lid, feature_id=fid,
                                       action=LAUNCH, brief_kind=brief, branch=branch,
                                       reason=''), f_['id']))
        elif code_stage and (amend := console_amend_row(
                product, lid, fid, _delivery_union(items, lead, skip), branch, brief)):
            out.append(amend)
        elif code_stage:
            union = _delivery_union(items, lead, skip)
            if not union:       # D1: an empty footprint claims nothing and can build nothing
                out.append(Row(tier=2, kind=kind, item_id=lid, feature_id=fid,
                               action='WAITS ON writes', brief_kind=brief, branch=branch,
                               reason='no writes: declared on any open member: the plan must name '
                                      'the files this delivery writes before a coder can start',
                               waits_on='writes'))
                if lead.get('type') == 'task' and not recut_declined(lead):
                    # the same re-cut a lone Task with no writes gets (task_rows): without it the
                    # delivery waits on a footprint no session is ever launched to declare
                    out.append(Row(tier=2, kind=RESHAPE, item_id=lid, feature_id=fid,
                                   action=LAUNCH, brief_kind='reshape',
                                   branch=branch_for(product, 'plan', lid),
                                   reason=NO_WRITES_RECUT))
                out += [console_member_row(items, product, m, fid, landed_shas) for m in console]
                out += [deferred_member_row(items, product, m, lid, fid, landed_shas)
                        for m in deferred]
                continue
            # an after: outside the delivery holds it here, before it claims a footprint: a
            # delivery that only hold_unlanded turns into a WAITS row would still have taken
            # its union into `running` and held every Task sharing a file with it
            built = [m for m in open_members if m not in skip]
            ahead = [a for m in built for a in after_of(items, items[m])
                     if a not in built and a not in landed_ids(items, landed_shas)]
            other = footprint.first_conflict(union, running,
                                             footprint.shared_globs(product))
            if ahead:
                out.append(Row(tier=2, kind=kind, item_id=lid, feature_id=fid,
                               action=f'WAITS ON {ahead[0]}', brief_kind=brief, branch=branch,
                               reason=f'after: {ahead[0]} has not landed', waits_on=ahead[0]))
            elif other:
                out.append(Row(tier=2, kind=kind, item_id=lid, feature_id=fid,
                               action=f'WAITS ON {other}', brief_kind=brief, branch=branch,
                               reason=f'writes: overlaps {other}', waits_on=other))
            else:
                what = (f'{len(built)} Tasks of {fid}: one branch, one PR, one review'
                        if slice_ else f'{len(built)} items, plan approved')
                out.append(Row(tier=2, kind=kind, item_id=lid, feature_id=fid, action=LAUNCH,
                               brief_kind=brief, branch=branch,
                               reason=f'{what}, footprint free'))
                running.append((lid, union))
        else:
            reason = (f"{len(open_members)} items, no plan yet" if word == 'card'
                      else f"{stage}, no session")
            out.append(Row(tier=2, kind=kind, item_id=lid, feature_id=fid, action=LAUNCH,
                           brief_kind=brief, branch=branch, reason=reason))
        for m in others:
            out.append(Row(tier=2, kind=kind, item_id=m, feature_id=fid,
                           action=f'WAITS ON delivery {lid}', brief_kind='task', branch=branch,
                           reason=f'delivered by {lid}: the delivery speaks for it',
                           waits_on='delivery'))
        out += [console_member_row(items, product, m, fid, landed_shas) for m in console]
        out += [deferred_member_row(items, product, m, lid, fid, landed_shas) for m in deferred]
    return out


def deferred_member_row(items, product, mid, lid, fid, landed_shas=None):
    """The row of a member :func:`deferred_members` left out of ``lid``'s delivery: it waits on
    its first unlanded ``after:``, and builds on its own row once the delivery has landed."""
    landed = landed_ids(items, landed_shas)
    pending = [a for a in after_of(items, items[mid]) if a not in landed] or [lid]
    return Row(tier=2, kind=PLAN_CODE, item_id=mid, feature_id=fid,
               action=f'WAITS ON {pending[0]}', brief_kind='task',
               branch=branch_for(product, 'code', mid),
               reason=f'left out of delivery {lid}: after: {pending[0]} waits on that delivery '
                      f'— it builds on its own row once {pending[0]} lands',
               waits_on=pending[0])


def console_member_row(items, product, mid, fid, landed_shas=None):
    """The CONSOLE → AMEND row of a delivery member only the console may edit
    (:func:`asf.amendable.console_members`): ``WAITS ON <id>`` while an ``after:`` of it has not
    landed (its delivery's code members first), then the console's edit. It never holds the
    delivery's code members, and launches nothing."""
    member = items[mid]
    branch = branch_for(product, 'code', mid)
    landed = landed_ids(items, landed_shas)
    pending = [a for a in after_of(items, member) if a not in landed]
    row = console_amend_row(product, mid, fid, member.get('writes'), branch)
    if pending:
        return dataclasses.replace(row, action=f'WAITS ON {pending[0]}', waits_on=pending[0],
                                   amend='', reason=f'after: {pending[0]} has not landed; then '
                                                    f'{row.reason}')
    return row


def feature_rows(items, product, busy, running, landed_shas=None, occupancy=None):
    """Every Feature's rows, in Feature order (:func:`feature_order`: Epic rank, rank, id).
    ``running`` grows as PLAN → CODE rows are handed out, so two ready Tasks sharing a file never
    both launch. ``occupancy`` (:func:`asf.workers.lifecycle.occupancy`): a spec or plan whose
    work is pushed and waiting to land is not starved (PUSHED → LAND). A blocked Feature
    (``blockedBy`` an open item) launches nothing: each row it would have is a ``WAITS ON
    <blocker>`` row, like a blocked Bug's (B-0058), and it claims no footprint (F-1129)."""
    out = []
    feats = [f for f in ix.of_type(items, 'feature')
             if f.get('decided') is True and is_open(f)
             and not (f.get('delivers') or f.get('delivered_by'))]
    for f in sorted(feats, key=lambda v: feature_order(items, v)):
        if not f.get('blocked'):
            out.extend(_one_feature_rows(items, product, f, busy, running, landed_shas, occupancy))
            continue
        mine = _one_feature_rows(items, product, f, busy, copy.copy(running), landed_shas,
                                 occupancy)
        out.extend(blocked_row(r, f) if r.launches else r for r in mine)
    return out


def blocked_row(row, item):
    """``row`` of a blocked ``item`` as the non-launching row that names what it waits on."""
    blockers = list(item.get('blocked_by_open') or ())
    on = blockers[0] if blockers else 'blocked'
    return dataclasses.replace(row, action=f'WAITS ON {on}', waits_on=on,
                               reason='blocked by ' + (', '.join(blockers) or 'an open item'))


def _one_feature_rows(items, product, f, busy, running, landed_shas, occupancy):
    """One decided, open Feature's rows (:func:`feature_rows`)."""
    out = []
    limit = stalemate_round(product)
    fid = f['id']
    stage = f.get('stage') or 'card'
    word = stage.split(' ')[0]
    if word not in BUILD_STAGES:
        row = foreign_row(product, f, 2, fid, 'spec', UNDECIDED)
        if row is not None:
            out.append(row)
            return out
    doc, rnd = review_round(f)
    if doc and rnd >= limit:
        if fid not in busy:
            out.append(Row(tier=2, kind=STALEMATE, item_id=fid, feature_id=fid, action=LAUNCH,
                           brief_kind='adjudicate', branch=branch_for(product, doc, fid),
                           reason=f"{doc}-review r{rnd} >= r{limit}: adjudicate, no further round"))
        return out
    if fid in busy:
        return out
    waits = (occupancy,)
    if is_direct(f) and word not in BUILD_STAGES:
        row = direct_row(f, product, occupancy)
        if row is not None:
            out.append(row)
        return out
    if word == 'card' and is_small(f):
        out.append(spec_plan_row(fid, product, occupancy))
    elif word == 'card':
        out.append(_doc_row(CARD_SPEC, fid, 'spec', product, 'decided card, no spec', *waits))
    elif word in ('spec-draft', 'spec-review'):
        carrier = spec_carrier(f)
        if carrier and plan_on_trunk(product) in (f.get('evidence') or []):
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
    elif word in ('plan-approved', 'building') and plan_carrier(f):
        out.append(land_plan_row(f, product, *waits))
    elif word in ('plan-approved', 'building') and replan_mod.pending(f):
        # the Feature's plan is being replaced: its Tasks' rows wait on the replan, and claim
        # no footprint (a copy of ``running``) — the replan may move or drop every one of them
        out.append(replan_row(f, product, occupancy, items))
        out.extend(replan_wait(r, fid) if r.launches else r
                   for r in task_rows(items, product, f, busy, copy.copy(running), landed_shas))
    elif word in ('plan-approved', 'building'):
        out.extend(task_rows(items, product, f, busy, running, landed_shas))
    return out


def replan_branch(product, fid):
    """The replan's branch: the plan lane, under a name that is not the Feature's own plan
    branch — the ingest reads ``<plan prefix><id>`` as the Feature's plan, and a replan is not
    that document (:data:`asf.record.replan.SUBDIR`)."""
    return branch_for(product, 'plan', f'{fid}-replan')


def _tasks_in_flight(items, feature, occupancy):
    """``[(task_id, pr)]``, sorted, of ``feature``'s open Tasks whose PR the lane still holds
    open — ``occupancy``'s ``review``/``landing`` (:func:`asf.workers.lifecycle.occupancy`):
    PUSHED, in review, or waiting on a gate/merge; a merged or closed PR has already dropped out
    of both, so it never appears here. A ``reshape:`` must not rewrite the plan under a Task
    whose PR is about to land (live case: F-0094's reshape against T-0048's PUSHED PR #954)."""
    occ = occupancy or {}
    held = dict(occ.get('landing') or {})
    held.update(occ.get('review') or {})
    out = []
    for t in ix.feature_tasks(items, feature):
        if not is_open(t):
            continue
        h = held.get(t['id'])
        if h and h.get('pr'):
            out.append((t['id'], h['pr']))
    return sorted(out)


def replan_row(feature, product, occupancy, items=None):
    """A Feature's pending ``reshape:`` (:func:`asf.record.replan.pending`): RESHAPE → REPLAN,
    one ``replan`` session on :func:`replan_branch` with the reshape text as its binding input —
    or PUSHED → LAND while that branch waits on the lane. Any open Task of the Feature whose PR
    is in flight (:func:`_tasks_in_flight`) holds the row first — ``WAITS ON <task> #<pr>``, one
    per Task named on the same line — since a session rewriting Tasks a PR is about to land under
    would race the merge; the row launches once every such PR has merged or closed. The record
    applies the landed replan (:func:`asf.record.replan.apply_replans`) and records
    ``reshape_applied``, which ends it."""
    fid = feature['id']
    branch = replan_branch(product, fid)
    in_flight = _tasks_in_flight(items or {}, feature, occupancy)
    if in_flight:
        names = ', '.join(f'{tid} #{pr}' for tid, pr in in_flight)
        return Row(tier=2, kind=REPLAN, item_id=fid, feature_id=fid,
                   action=f'WAITS ON {names}', brief_kind=REPLAN_KIND, branch=branch,
                   reason=f"{names} in flight: the replan waits for it to merge or close before "
                          f"it rewrites the plan under it", waits_on=in_flight[0][0])
    waiting = _waiting_doc(fid, 'plan', product, occupancy, branch)
    if waiting:
        return Row(tier=2, kind=PUSHED_LAND, item_id=fid, feature_id=fid,
                   action=f"{WAITS_LANDING}: {waiting}", brief_kind=REPLAN_KIND, branch=branch,
                   reason=waiting)
    return Row(tier=2, kind=REPLAN, item_id=fid, feature_id=fid, action=LAUNCH,
               brief_kind=REPLAN_KIND, branch=branch, reason=f"groom: {feature['reshape']}")


def replan_wait(row, fid):
    """``row`` as the non-launching row of a Feature whose replan is pending."""
    return dataclasses.replace(row, action=f'WAITS ON replan {fid}', waits_on=WAITS_REPLAN,
                               reason=f'{fid} is being re-planned (reshape:): its Tasks wait for '
                                      f'the replan to land')


#: the launching rows a pending replan holds: work cut from the plan the replan replaces
REPLAN_HELD = (PLAN_CODE, RESHAPE, DELIVERY_PLAN, DELIVERY_CODE, CONSOLE_AMEND)


def hold_replanning(rows, items):
    """Every launching :data:`REPLAN_HELD` row of a Feature whose ``reshape:`` is pending
    becomes ``WAITS ON replan <fid>`` — a correction's resume of a delivery included: it would
    build the plan the replan replaces. A review, a landing, a rebase and the replan itself are
    never held: work already pushed finishes."""
    out = []
    for r in rows:
        f = items.get(r.feature_id) or {}
        if r.launches and r.kind in REPLAN_HELD and replan_mod.pending(f):
            r = replan_wait(r, f['id'])
        out.append(r)
    return out


def direct_row(feature, product, occupancy):
    """A ``lane: direct`` Feature's one row: DIRECT → BUILD, a session that builds it end to end
    on ``<branch_prefixes.direct><id>``; once that branch is pushed, a PUSHED → LAND row that
    waits on the lane — or None while the lane's REVIEW/landing state holds it
    (:func:`lane_rows` speaks for it then)."""
    fid, occ = feature['id'], occupancy or {}
    branch = branch_for(product, DIRECT, fid)
    if fid in (occ.get('review') or {}) or fid in (occ.get('landing') or {}):
        return None
    why = (occ.get('branches') or {}).get(branch) or (occ.get('waiting_landing') or {}).get(fid)
    if why:
        return Row(tier=2, kind=PUSHED_LAND, item_id=fid, feature_id=fid,
                   action=f"{WAITS_LANDING}: {why}", brief_kind=DIRECT, branch=branch,
                   reason=f"direct {why}")
    return Row(tier=2, kind=DIRECT_BUILD, item_id=fid, feature_id=fid, action=LAUNCH,
               brief_kind=DIRECT, branch=branch,
               reason='lane: direct — one session builds the Feature end to end, one PR')


def spec_plan_row(fid, product, occupancy):
    """A ``size: s`` card's one document session: CARD → SPEC+PLAN on the plan branch (the
    document it lands is the plan, with the spec beside it), or PUSHED → LAND while that branch
    waits on the lane."""
    row = _doc_row(SPEC_PLAN, fid, 'plan', product,
                   'decided card, size s: spec and plan in one session', occupancy)
    waiting = ((occupancy or {}).get('docs') or {}).get(fid, {}).get(SPEC_PLAN_KIND)
    if row.launches and waiting:
        return dataclasses.replace(row, kind=PUSHED_LAND, action=f"{WAITS_LANDING}: {waiting}",
                                   brief_kind=SPEC_PLAN_KIND, reason=waiting)
    return dataclasses.replace(row, brief_kind=SPEC_PLAN_KIND)


def land_doc_row(feature, product, occupancy, doc):
    """An approved spec or plan on a branch, not the trunk: coders read it from the trunk, so it
    is landed first — as written, never rewritten. Pushed and waiting (a PR open, a run the docs
    lane has not merged yet): PUSHED → LAND; else APPROVED → LAND, which launches nothing — the
    lane adopts the branch (:mod:`asf.tick.land_spec`) and lands it once green. A branch that
    cannot land as it stands comes back as a STARVED → SPEC/PLAN session through its
    :data:`LANDING_GATE` correction (:func:`correction_rows`)."""
    fid = feature['id']
    carrier = spec_carrier(feature) if doc == 'spec' else plan_carrier(feature)
    waiting = _waiting_doc(fid, doc, product, occupancy, carrier)
    if waiting:
        return Row(tier=2, kind=PUSHED_LAND, item_id=fid, feature_id=fid,
                   action=f"{WAITS_LANDING}: {waiting}", brief_kind=doc, branch=carrier,
                   reason=waiting)
    return Row(tier=2, kind=APPROVED_LAND, item_id=fid, feature_id=fid,
               action=f"{WAITS_LANDING}: {doc} approved on {carrier}", brief_kind=doc,
               branch=carrier, waits_on='landing',
               reason=f"{doc} approved on {carrier}, not on the trunk: the lane adopts it and "
                      f"lands it — no coder starts before it is on the trunk")


def land_spec_row(feature, product, occupancy):
    return land_doc_row(feature, product, occupancy, 'spec')


def land_plan_row(feature, product, occupancy):
    return land_doc_row(feature, product, occupancy, 'plan')


def task_rows(items, product, feature, busy, running, landed_shas=None):
    """The Feature's New Tasks, one PLAN → CODE row each — the residual under ``delivery:
    feature``: a Task a delivery speaks for (:func:`delivered`) gets its row from
    :func:`delivery_rows`, not here."""
    out = []
    tasks = [t for t in ix.feature_tasks(items, feature)
             if t.get('state', 'New') == 'New' and t['id'] not in busy and not t.get('blocked')
             and not delivered(items, t, landed_shas)]
    landed = landed_ids(items, landed_shas)
    absorbed = absorbers(items)
    on_trunk = landed_shas or {}
    recut = None     # the one Task of this Feature a reshape session re-cuts (D2)
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
        orphan, removed = orphaned_after(items, pending)
        if orphan:
            # the survivor of a groom merge was removed in turn, unlanded: the scope folded into
            # it is still to do, and nothing will ever land it — a person decides, never a wait
            out.append(Row(tier=2, kind=PLAN_CODE, item_id=t['id'], feature_id=feature['id'],
                           action=NEEDS_DECISION, brief_kind='task',
                           branch=branch_for(product, 'code', t['id']),
                           reason=f"after: {orphan} was removed unlanded ({removed}): re-point "
                                  f"after: or re-cut its scope", waits_on='decision'))
            continue
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
            if recut is None and not recut_declined(t):  # one plan, one session, one branch (D2)
                recut = t['id']
                out.append(Row(tier=2, kind=RESHAPE, item_id=t['id'], feature_id=feature['id'],
                               action=LAUNCH, brief_kind='reshape',
                               branch=branch_for(product, 'plan', t['id']),
                               reason=NO_WRITES_RECUT))
            continue
        amend = console_amend_row(product, t['id'], feature['id'], writes,
                                  branch_for(product, 'code', t['id']))
        if amend:
            out.append(amend)
            continue
        other = footprint.first_conflict(writes, running,
                                         footprint.shared_globs(product))
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


KIND_ORDER = {STALEMATE: 0, CONFLICT: 1, STALE: 2, GROOM_ADJUDICATE: 2, GROOM_CLERK: 2,
              RESHAPE: 3, REPLAN: 3, DELIVERY_PLAN: 4, DELIVERY_CODE: 4}


def _token(line):
    """The item token of an open question line (``F-0001`` or ``inbox:<file>``), else ``''``."""
    m = groom_policy.OPEN_QUESTION_RE.match(line)
    return m.group('id') if m else ''


def _inbox_prefix():
    """``asf.groom.inbox.TOKEN_PREFIX``, imported late: ``asf.groom.inbox`` reaches the record's
    writers, which import this module back — at load it is a cycle."""
    from asf.groom import inbox as inbox_mod
    return inbox_mod.TOKEN_PREFIX


def _split_lines(lines):
    """A groom day's open question lines, partitioned into the clerical half and the judgement
    half. A line whose item token is ``inbox:<file>`` is a card intake could not type — a right
    answer read off the card, not a ruling. Everything else is a policy question."""
    prefix = _inbox_prefix()
    clerk = [l for l in lines if _token(l).startswith(prefix)]
    judge = [l for l in lines if not _token(l).startswith(prefix)]
    return clerk, judge


def _half_due(product, attempts, new):
    """One half's own launch gate: ``attempts`` sessions its job already had, ``new`` the
    questions of its half its last brief did not carry (``None``: the caller does not say)."""
    if new is None:  # a caller that does not say which questions are new: the attempt cap alone
        return attempts < groom_policy.adjudicate_attempts(product)
    # the day already had its session: another only for questions asked since, up to the cap
    return not attempts or bool(new and attempts < groom_policy.adjudicate_per_day(product))


def groom_rows(index, product, busy, groom_state, inflight):
    """At most two rows per groom day (§2.5, F-0093 §2.4): GROOM → ADJUDICATE for the questions
    the policy pass did not answer, and GROOM → CLERK for the ``inbox:`` intakes — each a session
    of its own, on its own job (``groom-<date>``, ``groom-clerk-<date>``), and each minted only
    when its half has a line. Once a half had its session, another only for questions its brief
    did not carry (``groom_state['new']`` / ``['clerk_new']``), up to ``groom.adjudicate_per_day``.
    ``groom_state`` is the one fact this module cannot derive from ``index.json`` (P5) — the
    caller (:mod:`asf.tick.step_wave`) builds it from the record clone's newest
    ``groom/<date>.md`` and the ledger's attempts. No ``groom_state``, the gate off, no open
    question: no row — which keeps every existing feeder test and ``asf next`` unchanged (T8)."""
    if not groom_state or not groom_policy.groom_auto(product):
        return []
    open_ids = list(groom_state.get('open') or ())
    if not open_ids:
        return []
    date = groom_state.get('date')
    live = {s.get('job') for s in inflight or ()}
    lines = list(groom_state.get('lines') or ())
    clerk_lines, judge_lines = _split_lines(lines)
    prefix = _inbox_prefix()
    if not lines:  # a caller that carries ids alone: split the ids on the same token
        judge_ids = [i for i in open_ids if not i.startswith(prefix)]
        clerk_ids = [i for i in open_ids if i.startswith(prefix)]
    else:
        judge_ids = [_token(l) for l in judge_lines]
        clerk_ids = [_token(l) for l in clerk_lines]
    items = items_of(index)
    common = dict(tier=2, action=LAUNCH, branch=branch_for(product, 'groom', date),
                  groom_date=date, groom_file=groom_state.get('file', ''))
    out = []
    new = groom_state.get('new')
    if new is not None:
        new = [i for i in new if not str(i).startswith(prefix)]
    if judge_ids and f'groom-{date}' not in live \
            and _half_due(product, groom_state.get('attempts') or 0, new):
        oldest = groom_state.get('oldest')
        if oldest not in judge_ids:
            oldest = judge_ids[0]
        item = items.get(oldest) or {}
        f = feature_of(items, item) if item else None
        reason = (f"{len(judge_ids)} groom questions no rule answers, oldest {oldest} "
                  f"(undecided {ix.age(item.get('stage_since'))})")
        out.append(Row(kind=GROOM_ADJUDICATE, item_id=oldest, feature_id=f['id'] if f else '',
                       brief_kind='groom', reason=reason,
                       answers_file=groom_state.get('answers', ''),
                       open_questions=tuple(judge_lines), **common))
    clerk_new = groom_state.get('clerk_new')
    if clerk_ids and f'groom-clerk-{date}' not in live \
            and _half_due(product, groom_state.get('clerk_attempts') or 0, clerk_new):
        out.append(Row(kind=GROOM_CLERK, item_id=clerk_ids[0], feature_id='',
                       brief_kind='groom-clerk',
                       reason=f"{len(clerk_ids)} inbox cards intake could not type",
                       answers_file=groom_state.get('clerk_answers', ''),
                       open_questions=tuple(clerk_lines), **common))
    return out


def hold_unlanded(rows, items, landed_shas=None, product=None, on_trunk=None):
    """B-0080: ``after:`` holds every row kind, not only PLAN → CODE. An item whose predecessor
    has not landed is not in dispute, it is waiting: a launching row for it (code, correct,
    adjudicate, rebase, close) becomes ``WAITS ON <id>`` — no session, no round. The groom row
    speaks for a day's questions, not for the item it names, so it is left alone; so is a
    ``PUSHED → REVIEW`` row — a review of work already pushed conflicts with nothing.

    A :data:`DELIVERY_PLAN`/:data:`DELIVERY_CODE` row's scan reads the lead **and** every member
    of its ``delivers:`` — an ``after:`` any of them names holds the whole delivery on the first
    unlanded predecessor found. An ``after:`` naming another member of the same delivery is
    not a hold: it is the order the one session commits in (``delivery: feature``). A member
    the delivery leaves out (:func:`left_out`, given ``product``: the console's, or one a cycle
    defers) is not built on the branch, so its ``after:`` holds its own row, never the
    delivery's.

    ``on_trunk`` (``flags.roots``, :func:`landed_ids`): an ``after:`` on an unverified landing
    the trunk carries no longer holds; a row it released says so in its reason, so a dependant
    built on a landing later reset names the root it trusted.

    ``console_aside`` on the map (``flags.console_wait: aside``, :func:`console_only`): the items
    whose only row waits on the console — :func:`after_of` drops them, so an ``after:`` on one
    no longer holds; a launching row it released says so."""
    on_trunk = set(getattr(items, 'trunk_unverified', ()) if on_trunk is None else on_trunk)
    aside = getattr(items, 'console_aside', ())
    landed = landed_ids(items, landed_shas, on_trunk)
    absorbed = absorbers(items)
    out, said = [], set()
    for r in rows:
        if r.kind in (DELIVERY_PLAN, DELIVERY_CODE):
            # a member's WAITS ON delivery row scans its lead's list too: its own after: on a
            # sibling of the delivery is commit order, not a hold
            lid = delivery_lead_of(items, items.get(r.item_id) or {}) or r.item_id
            lead = items.get(lid) or {}
            skip = set().union(*left_out(product, items, lid, landed_shas)) \
                if product is not None and r.kind == DELIVERY_CODE else set()
            ids = [m for m in lead.get('delivers') or () if m not in skip]
            if r.item_id not in ids:
                ids = [r.item_id] + ids
            pending, seen = [], set(ids)
            for iid in ids:
                for a in after_of(items, items.get(iid) or {}, absorbed):
                    if a not in landed and a not in seen:
                        seen.add(a)
                        pending.append(a)
        else:
            after = after_of(items, items.get(r.item_id) or {}, absorbed)
            pending = [a for a in after if a not in landed]
            trusted = [a for a in after if a in on_trunk]
            if trusted and not pending and r.launches:
                r = dataclasses.replace(r, reason=f"{r.reason} (after: {', '.join(trusted)} on "
                                                  f"the trunk, its landing unverified)")
            put = [a for a in after_of(items, items.get(r.item_id) or {}, absorbed, True)
                   if a in aside] if aside else []
            if put and not pending and r.launches:
                r = dataclasses.replace(r, reason=f"{r.reason} (after: {', '.join(put)} waits on "
                                                  f"the console — put aside, it orders nothing)")
        # ON TRUNK / PARKED / NEEDS DECISION are already non-launching answers with their own
        # waits_on: rewriting them into WAITS ON would hide the row the gate exists to print.
        # A review of a pushed branch reads a diff and changes nothing the predecessor writes:
        # it conflicts with nothing, so after: never holds it (25 ASF reviews held 1,776 item-h)
        keeps = r.kind in (GROOM_ADJUDICATE, GROOM_CLERK, PUSHED_REVIEW) \
            or r.action.startswith((ON_TRUNK, PARKED, NEEDS_DECISION))
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
HELD_KINDS = {'features': (CARD_SPEC, STARVED_SPEC, STARVED_PLAN, PLAN_CODE, SPEC_PLAN,
                          DIRECT_BUILD, REPLAN),
              'bugs': (BUG_FIX,)}
HOLD = 'WAITS ON hold'
#: the ``waits_on`` of a row an Epic's spent budget holds (F-0052)
BUDGET = 'budget'
#: what an Epic's spent budget holds: new work only, HELD_KINDS' own enumeration — one list, so
#: feeder.hold and the budget hold can never disagree on what "new work" means
BUDGET_HELD_KINDS = frozenset(k for ks in HELD_KINDS.values() for k in ks)


def epic_verdicts(items):
    """``{epic id: EpicSpend}`` for every Epic in the map — one subtree sum per Epic per pass."""
    return {e['id']: budget.epic_spend(e['id'], ix.subtree_usd(items, e), e.get('budget_usd'))
            for e in ix.of_type(items, 'epic')}


def epics_over_budget(items):
    """``[EpicSpend]`` in id order: every Epic past its budget that still has open work beneath
    it. Read from the index alone — the wave's line must not depend on how many held rows
    survived the capacity cut (F-0052 §1.1)."""
    verdicts = epic_verdicts(items)
    return [s for eid, s in sorted(verdicts.items())
            if s.over and any(it['id'] != eid and is_open(it)
                              for it in ix.subtree(items, items[eid]))]


def over_budget_epics(rows, items, verdicts=None):
    """F-0052: an Epic whose accumulated spend has passed its typed ``budget_usd`` starts nothing
    new. Each launching row of :data:`BUDGET_HELD_KINDS` under such an Epic becomes
    ``WAITS ON budget`` — no session, no slot, still shown — with the Epic and both figures in
    its ``reason``.

    Never held: every other kind (a review, a landing, a correction, a rebase, a stalemate, the
    groom), because a budget stops starting work, not finishing it; and an S1/S2 Bug's
    ``BUG → FIX``, because an incident is not discretionary spend. An item with no Epic above it
    has no budget to be over."""
    verdicts = epic_verdicts(items) if verdicts is None else verdicts
    out = []
    for r in rows:
        item = items.get(r.item_id) or {}
        epic = ix.epic_of(items, item)
        s = verdicts.get(epic['id']) if epic else None
        exempt = (r.kind == BUG_FIX and item.get('severity') in ('S1', 'S2'))
        if s is None or not s.over or not r.launches or exempt \
                or r.kind not in BUDGET_HELD_KINDS:
            out.append(r)
            continue
        out.append(dataclasses.replace(
            r, action=f'{WAITS_BUDGET}: {budget.epic_over(s)}', waits_on=BUDGET,
            reason=budget.epic_reason(s)))
    return out


def shelved(items, parks=()):
    """``{item id: why}`` for every open item the product has put aside: one with
    ``priority: later`` on itself, its Feature or its Epic (``'F-0112 later'``, the nearest such
    card named), or one a standing item-scope operator park holds (``'operator park'``)."""
    later = {v['id'] for v in items.values()
             if str(v.get('priority') or '').strip().lower() == LATER}
    parked = {p.get('item') for p in parks or () if (p.get('scope') or 'item') == 'item'}
    out = {}
    for v in items.values():
        if not is_open(v):
            continue
        seen, cur = set(), v
        while cur and cur['id'] not in seen:
            seen.add(cur['id'])
            if cur['id'] in later:
                out[v['id']] = f"{cur['id']} {LATER}"
                break
            cur = items.get(cur.get('parent'))
        else:
            if v['id'] in parked:
                out[v['id']] = 'operator park'
    return out


_WAITED = re.compile(r'^WAITS ON (?:delivery )?([A-Z]+-\d+)\b')


def hold_shelved(rows, items, parks=()):
    """Work the product put aside (:func:`shelved`) is neither started nor dressed up as live:

    * a launching row of new work (:data:`BUDGET_HELD_KINDS`) under a ``priority: later``
      Feature or Epic becomes ``WAITS ON later: F-0112 later`` — no session, no slot, still
      shown; a review, a correction or a landing of work already pushed still finishes;
    * a row waiting on a put-aside item says why it will not move:
      ``WAITS ON T-0377 (parked: F-0112 later)``. The ``after:`` / ``delivers:`` edge stays —
      dropping it is the product's call, not the feeder's.

    Returns ``(rows, shelved_rows)``: ``shelved_rows`` holds the ``id()`` of every row of
    put-aside work, which :func:`candidates` orders behind the live parity work."""
    why = shelved(items, parks)
    if not why:
        return rows, set()
    out, aside = [], set()
    for r in rows:
        own = why.get(r.item_id) or why.get(r.feature_id)
        if own and own.endswith(f' {LATER}') and r.launches and r.kind in BUDGET_HELD_KINDS:
            r = dataclasses.replace(r, action=f'{WAITS_LATER}: {own}', waits_on=LATER,
                                    reason=f'priority: {LATER} — {own}: new work waits until '
                                           f'the product raises it')
        elif not r.launches:
            m = _WAITED.match(r.action)
            target = m.group(1) if m else ''
            if target and target in why and '(parked: ' not in r.action:
                r = dataclasses.replace(r, action=f'{r.action} (parked: {why[target]})')
                own = own or why[target]
        out.append(r)
        if own and not r.launches:
            aside.add(id(r))
    return out, aside


def hold_parks(rows, items, parks):
    """A branch or job park (``asf park <branch|job>``, :func:`asf.workers.lifecycle.parks`)
    holds that branch's or that job's rows alone: each row of its item on the branch (or for the
    job, ``<brief kind>-<item>``) gives way to one ``PARKED`` row naming the scope, and the
    item's rows on its other branches go on. An item park is the item's correction instead
    (:func:`correction_rows`)."""
    scoped = [p for p in parks or () if (p.get('scope') or 'item') in ('branch', 'job')
              and is_open(items.get(p.get('item')) or {})]
    if not scoped:
        return rows

    def held(r):
        job = f'{r.brief_kind}-{r.item_id}'.lower()
        return any(p['item'] == r.item_id and (
            (p['scope'] == 'branch' and r.branch and r.branch == p.get('branch'))
            or (p['scope'] == 'job' and job == p.get('on_job'))) for p in scoped)
    out = [r for r in rows if not held(r)]
    for p in scoped:
        item = items.get(p['item']) or {}
        f = feature_of(items, item)
        out.append(Row(tier={'S1': 0, 'S2': 1}.get(item.get('severity'), 2), kind=FIX_CORRECT,
                       item_id=p['item'], feature_id=f['id'] if f else '',
                       action=f'{PARKED} {p.get("reason") or p["scope"]}', brief_kind='correct',
                       branch=p.get('branch') or '', reason=p.get('reason') or 'parked',
                       waits_on='operator'))
    return out


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


def _capped(row, attempts, limit, product, adjudicated=None):
    """The row itself below the limit; the STALEMATE row at it (rows.bug_rows). Above it, never
    nothing: an item silently dropped is parity work no table shows (a product's T-0338, 51
    sessions, and T-0349, 39, vanished from NEXT). So past the limit it is the STALEMATE row
    again — one adjudicate session per card state, its launches bounded by the relaunch cap
    (:mod:`asf.workers.relaunch`) on the head — unless ``adjudicated`` (``{item: {'runs', 'at',
    'same_card'}}``, :func:`asf.tick.step_wave.adjudications`) says an adjudicate session already
    ended on this same card: then a non-launching ``PARKED`` row with the reason."""
    n = attempts.get(row.item_id, 0)
    if n < limit or row.ruling:  # an operator ruling is carried out, not adjudicated again
        return row
    if n == limit:
        return dataclasses.replace(
            row, kind=STALEMATE, brief_kind='adjudicate', action=LAUNCH,
            reason=f"{row.kind} after {n} sessions: adjudicate, not another attempt")
    adj = (adjudicated or {}).get(row.item_id) or {}
    if adj.get('same_card') and adj.get('ruling') and not adj.get('carried'):
        return ruling_row(row, adj['ruling'], n, limit)
    if adj.get('same_card'):
        why = (f"{row.kind} after {n} sessions (limit {limit}); adjudicated "
               f"{adj.get('runs') or 1} time(s), last {str(adj.get('at') or '?')[:16]}, on this "
               f"same card: a card edit or a person's decision moves it")
        return dataclasses.replace(row, kind=STALEMATE, brief_kind='adjudicate',
                                   action=f'{PARKED} adjudicated, card unchanged',
                                   reason=why, waits_on='operator')
    return dataclasses.replace(
        row, kind=STALEMATE, brief_kind='adjudicate', action=LAUNCH,
        reason=f"{row.kind} after {n} sessions (limit {limit}): adjudicate this card state, not "
               f"another attempt")


#: the over-limit kinds whose ruling a document session carries out, not a code correction
DOC_KINDS = frozenset({CARD_SPEC, STARVED_SPEC, STARVED_PLAN})


def ruling_row(row, ruling, n, limit):
    """The over-limit ``row`` an adjudicate session already ruled on, its ruling handed to one
    session (``ruling=True``: the cap never sends it to adjudication again): a Feature's document
    row (:data:`DOC_KINDS`) keeps its own ``spec`` or ``plan`` brief, on its branch, so the stage
    it stands at moves on; any other row becomes a ``correct`` brief on its branch. The ruling's text is the brief's correction. Once a session has started since
    the ruling, :func:`_capped` parks the row again (F-0109, F-0035, F-0003, 2026-10-05: PARKED
    "adjudicated, card unchanged" with the ruling on the card and nothing acting on it)."""
    brief = row.brief_kind if row.kind in DOC_KINDS else 'correct'
    text = (f"Carry out the adjudication ruling {ruling.get('job') or ''} "
            f"({ruling.get('at') or ''}), binding: {ruling.get('text') or ''}")
    return dataclasses.replace(
        row, brief_kind=brief, action=LAUNCH, correction=text, ruling=True,
        reason=f"{row.kind} after {n} sessions (limit {limit}): adjudicated — "
               f"{ruling.get('job') or 'the'} ruling carried out by one {brief} session")


#: the action of a stale park :func:`surface_stale_parks` puts in front of its tier
STALE_PARK_RE = re.compile(r'^NEEDS DECISION: (\d+) rows wait on this park')
#: an item id a row can wait on through ``after:``
AFTER_ID_RE = re.compile(r'^[A-Z]-\d{4,}$')


def park_since(occupancy, adjudicated=None):
    """``{item: iso}``: when each parked item was parked — an operator park's ``at``
    (:func:`asf.workers.lifecycle.parks`), a correction park's ``at``, or the newest adjudicate
    session that left the card unchanged (:func:`_capped`). An item none of them dates is
    absent: a park of unknown age never surfaces."""
    occ = occupancy or {}
    out = {}
    for iid, adj in (adjudicated or {}).items():
        if (adj or {}).get('same_card') and adj.get('at'):
            out[iid] = str(adj['at'])
    for iid, c in (occ.get('corrections') or {}).items():
        if (c or {}).get('parked') and c.get('at'):
            out[iid] = max(out.get(iid, ''), str(c['at']))
    for p in occ.get('parks') or ():
        if p.get('item') and p.get('at'):
            out[p['item']] = max(out.get(p['item'], ''), str(p['at']))
    return out


def waiting_roots(rows):
    """``{root: [dependant item, …]}``: every item a row waits on through ``after:``
    (:func:`hold_unlanded`'s ``WAITS ON <id>``), followed to the item that waits on no other —
    the root — and the distinct items whose rows end there, in row order."""
    on = {}
    for r in rows:
        if (r.waits_on and AFTER_ID_RE.match(str(r.waits_on))
                and r.action == f'WAITS ON {r.waits_on}'):
            on.setdefault(r.item_id, r.waits_on)

    def root(iid):
        seen = {iid}
        while on.get(iid) and on[iid] not in seen:
            iid = on[iid]
            seen.add(iid)
        return iid
    out = {}
    for iid in on:
        out.setdefault(root(on[iid]), []).append(iid)
    return out


def _parse_at(text):
    try:
        t = dt.datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def surface_stale_parks(rows, items, product, since, now=None):
    """Rule 3 of ``flags.roots``: a PARKED root (:func:`waiting_roots`) parked at least
    ``flags.roots_park_stale_days`` (``since``: :func:`park_since`) with at least
    ``flags.roots_min_dependants`` rows behind it becomes ``NEEDS DECISION: <n> rows wait on this
    park (<d> d) — asf unpark / asf close / asf replan``, first in its tier — one line per root,
    never per dependant. Nothing is unparked: the person decides (a product, 2026-10-03: 144 of
    183 rows waited on an item, four parked roots held 55 of them for days, no view said so)."""
    _hours, days, least = roots_settings(product)
    now = now or dt.datetime.now(dt.timezone.utc)
    behind = waiting_roots(rows)
    surfaced = {}
    for r in rows:
        if r.item_id in surfaced or not r.action.startswith(PARKED):
            continue
        n = len(behind.get(r.item_id) or ())
        at = _parse_at(since.get(r.item_id)) if since.get(r.item_id) else None
        if n < max(least, 1) or at is None:
            continue
        age = (now - at).total_seconds() / 86400
        if age < days:
            continue
        iid = r.item_id
        surfaced[iid] = dataclasses.replace(
            r, action=(f'{NEEDS_DECISION}: {n} rows wait on this park ({int(age)} d) — '
                       f'asf unpark {iid} / asf close {iid} / asf replan {iid}'),
            reason=f"{r.reason} — {n} rows wait on it: {', '.join(behind[iid][:5])}"
                   + (f' +{n - 5}' if n > 5 else ''))
    if not surfaced:
        return rows
    out, placed = [], set()
    for r in rows:
        if r.item_id in surfaced and r.action.startswith(PARKED):
            continue
        for iid, s in surfaced.items():
            if iid not in placed and s.tier <= r.tier:
                out.append(s)
                placed.add(iid)
        out.append(r)
    out += [s for iid, s in surfaced.items() if iid not in placed]
    return out


def unverified_rows(items, product, unverified, spoken):
    """A NEEDS DECISION row per open Task/Bug the lifecycle records as landed although the
    landing is not its own (``unverified``: ``{item: why}``) and no other row speaks for: the
    landing holds it from an idle-branch relaunch (:func:`branch_rows`), and the record will not
    close it — without this row it would sit in no table while its successors wait on it (a
    product's T-0091: its reshape's plan merge, recorded at another PR's merge-queue commit)."""
    out = []
    for iid, why in sorted((unverified or {}).items()):
        item = items.get(iid)
        if not item or not is_open(item) or iid in spoken or item['type'] not in ('task', 'bug'):
            continue
        f = _task_feature(items, item) if item['type'] == 'task' else feature_of(items, item)
        kind = 'task' if item['type'] == 'task' else 'fix'
        out.append(Row(tier={'S1': 0, 'S2': 1}.get(item.get('severity'), 2), kind=PLAN_CODE,
                       item_id=iid, feature_id=f['id'] if f else '', action=NEEDS_DECISION,
                       brief_kind='task' if kind == 'task' else 'fix-bug',
                       branch=_branch_of(item, product, kind),
                       reason=f'recorded landed, not verified as its work: {why}',
                       waits_on='decision'))
    return out


def doc_lane_landed(product, occupancy):
    """The items whose recorded landing (``landed_on``) is a spec/plan lane's merge
    (:func:`asf.evidence.evidence.lane_kind`): a document merged, not the item's work (W8-PR3) —
    such a landing holds no idle-branch row, so a Task left Active on its merged plan branch
    still gets its coder. Never a failure: prefixes that cannot be read name none."""
    on = (occupancy or {}).get('landed_on') or {}
    if not on:
        return set()
    try:
        from asf.evidence import evidence as ev
        prefixes = ev.branch_prefixes(product)
        return {i for i, b in on.items() if ev.lane_kind(b, prefixes)}
    except Exception:  # noqa: BLE001 — unreadable conventions: every landing holds as before
        return set()


def candidates(index, product, inflight, attempts=None, occupancy=None, groom_state=None,
               landed_shas=None, decision_limit=None, adjudicated=None, unverified_landed=None,
               unverified_on_trunk=None, now=None):
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
    ``0``: all). ``adjudicated``: :func:`_capped`'s fact — the items past the attempt limit an
    adjudicate session already ended on, per card state. ``unverified_landed``: ``{item: why}``,
    the open items the lifecycle records as landed whose landing does not hold up as theirs
    (:func:`asf.workers.landing.verify_landings`): each gets a NEEDS DECISION row — never a
    silent no-row, never a coder relaunched over a landing the lane recorded.
    ``unverified_on_trunk``: those of them whose landing the trunk carries — under
    ``flags.roots`` an ``after:`` on one is answered (:func:`hold_unlanded`), and a stale park
    with rows behind it surfaces (:func:`surface_stale_parks`, ``now`` its clock).

    ``flags.console_wait: aside`` (W8-PR1): an item whose only row is CONSOLE → AMEND
    (:func:`console_only`) is set on the map as ``console_aside`` and the rows are drawn again —
    it orders nothing (:func:`after_of`), its own row ranks behind the live rows of its tier. A
    Task released that way may itself be console work: the passes run to a fixed point."""
    items = items_of(index)
    roots = roots_on(product)
    items.trunk_unverified = set(unverified_on_trunk or ()) if roots else set()
    items.console_aside = set()
    args = (index, items, product, inflight, attempts, occupancy, groom_state, landed_shas,
            decision_limit, adjudicated, unverified_landed, now, roots)
    ordered = _candidates(*args)
    if console_wait(product) == CONSOLE_WAIT_ASIDE:
        for _ in range(CONSOLE_PASSES):
            more = console_only(ordered) - items.console_aside
            if not more:
                break
            items.console_aside |= more
            ordered = _candidates(*args)
    return ordered


#: the most times :func:`candidates` draws the rows again for ``console_wait: aside`` — each
#: pass can only add console items it released; a chain of them deeper than this stays held
CONSOLE_PASSES = 4


def _candidates(index, items, product, inflight, attempts, occupancy, groom_state, landed_shas,
                decision_limit, adjudicated, unverified_landed, now, roots):
    """:func:`candidates`' one pass over a prepared map."""
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
    held_by = {**{i: 'session running' for i in inflight_ids(inflight)},
               **dict(occ.get('waiting_landing') or {}), **dict(occ.get('busy') or {})}
    bugs = bug_rows(items, product, busy | spoken, attempts, held_by)
    rows += [r for r in bugs if r.launches]
    bug_waits = [r for r in bugs if not r.launches]
    # an idle branch is one nothing holds: a correction, a lane state (a partial occupancy
    # names it under review/landing alone) or a landing the record has not ingested yet
    held = (spoken | set(occ.get('review') or ()) | set(occ.get('landing') or ())
            | {v.get('item') for v in (occ.get('lanes') or {}).values() if v.get('item')}
            | (set(occ.get('landed') or ()) - doc_lane_landed(product, occ)))
    rows += [r for r in branch_rows(items, product, busy, held, landed_shas)
             if r.feature_id not in stalled]
    rows += groom_rows(index, product, busy, groom_state, inflight)
    # a Task a correction row speaks for gets no PLAN → CODE row too: one session per branch
    # (and a direct Feature's correction is its one session: no DIRECT → BUILD beside it)
    tasks_spoken = {i for i in spoken if (items.get(i) or {}).get('type') == 'task'
                    or corrections.get(i, {}).get('kind') == LANDING_GATE
                    or is_direct(items.get(i))}
    # a Feature's spec and plan wait to land one branch at a time: the document rows read that
    # per branch (:func:`_waiting_doc`), so one waiting document never holds the other
    docs_waiting = {i for i in waiting if (items.get(i) or {}).get('type') == 'feature'}
    # a lead a correction row speaks for gets no delivery row too: one session per branch
    rows += delivery_rows(items, product, busy | spoken, running, landed_shas)
    rows += feature_rows(items, product, (busy - docs_waiting) | tasks_spoken, running,
                         landed_shas, occ)
    rows += undecided_rows(items, product, busy, decision_limit)
    # a skipped S1/S2 Bug's WAITS row only where no other row already speaks for it
    spoken_for = {r.item_id for r in rows}
    rows += [r for r in bug_waits if r.item_id not in spoken_for]
    rows += unverified_rows(items, product, unverified_landed, busy | {r.item_id for r in rows})
    pushed = pushed_ids(items, occ)
    rows += pushed_rows(items, product, pushed, occ, live | {r.item_id for r in rows})
    rows = hold_parks(rows, items, occ.get('parks'))
    rows = hold_unlanded(rows, items, landed_shas, product)
    rows = hold_replanning(rows, items)
    rows = hold_classes(rows, product)
    rows = over_budget_epics(rows, items)          # F-0052
    rows, aside = hold_shelved(rows, items, occ.get('parks'))
    # ... and its own row ranks behind the live rows of its tier, as put-aside work does
    aside |= {id(r) for r in rows if r.item_id in items.console_aside and r.kind == CONSOLE_AMEND}
    cap, attempts = attempt_limit(product), attempts or {}
    rows = [_capped(r, attempts, cap, product, adjudicated)
            if r.launches and r.kind in CAPPED_KINDS else r for r in rows]

    landed = landed_ids(items, landed_shas)
    absorbed = absorbers(items)
    nearness = {}

    def near(r, f):
        """Finish the nearest-to-done first: a Task row of a Feature in build sorts by the depth
        of its Feature's ``after:`` chain still to land, shortest first (:func:`chain_left` —
        each link is a trip through the lane), then by the share of its Tasks already landed,
        highest first; the Feature's order breaks a tie."""
        if r.feature_id not in nearness:
            if in_build_stage(f):
                done, total = task_share(items, f, landed)
                nearness[r.feature_id] = (chain_left(items, f, landed, absorbed),
                                          -done / total if total else 0)
            else:
                # a row of a Feature not in build keeps its Feature's order, behind the finishing
                # — but a lane experiment's arm goes first: both arms start in one window
                nearness[r.feature_id] = (0, 0) if f.get('ab_pair') else (ix.BIG, 0)
        return nearness[r.feature_id]

    def key(pair):
        seq, r = pair
        f = items.get(r.feature_id) or {}
        if r.tier < 2:
            return (r.tier, 0, (0, 0), '', 0, seq)
        if r.kind in (GROOM_ADJUDICATE, GROOM_CLERK):
            # one session decides the whole day's questions for every Feature: it goes before
            # the Feature work, not at the rank of whichever card happens to be the oldest (an
            # unranked inbox card put it behind every launch, and the cut never reached it)
            return (r.tier, -1, -1, (-1, -1), -1, '', 0, seq)
        order = feature_order(items, f) if f else (ix.BIG, ix.BIG, r.feature_id or '~')
        phase = finish_phase(items, r)
        # finish before you start, across Features too: a row on pushed work (its review, its
        # landing, its correction) goes before any new coder — 22 PRs waited up to 8 days while
        # the seats went to new Tasks of nearer Features (asf 2026-10-04)
        return (r.tier, phase, 0 if r.item_id in pushed else 1,
                near(r, f) if phase == 0 and f else (0, 0), *order,
                KIND_ORDER.get(r.kind, 5), seq)
    ordered = [r for _seq, r in sorted(enumerate(rows), key=key)]
    # work the product put aside (:func:`hold_shelved`) ranks behind every live row of its tier
    ordered = sorted(ordered, key=lambda r: (r.tier, r.tier >= 2 and id(r) in aside))
    if roots:
        ordered = surface_stale_parks(ordered, items, product, park_since(occ, adjudicated), now)
    return ordered


def open_pr_of(item):
    """The number of the open PR the item's evidence names (``PR #N OPEN``), else ``''``."""
    for e in (item or {}).get('evidence') or ():
        m = OPEN_PR_RE.search(str(e))
        if m:
            return m.group(1)
    return ''


def pushed_ids(items, occupancy):
    """The open Tasks and Bugs whose work is pushed: the lifecycle holds them in a lane state, a
    wait on the lane or a correction (:data:`PUSHED_KEYS`), or the record names an open PR on
    them. Each one is owed a row in every plan (:func:`pushed_rows`, :func:`orphaned_pushed`)."""
    occ = occupancy or {}
    named = set()
    for key in PUSHED_KEYS:
        named |= set(occ.get(key) or ())
    return {iid for iid, v in (items or {}).items()
            if isinstance(v, dict) and v.get('type') in ('task', 'bug') and is_open(v)
            and (iid in named or open_pr_of(v))}


def pushed_rows(items, product, pushed, occupancy, spoken):
    """A non-launching PUSHED → LAND row for each pushed item (:func:`pushed_ids`) no other row
    and no live session speaks for (``spoken``) — never a silent PR: a blocked one waits on its
    blocker, one finished and awaiting harvest says so, and an open PR no run holds waits on the
    lane, which takes run-less branches up (:meth:`asf.harvest.lane.Lane.orphan_claims`)."""
    occ = occupancy or {}
    waiting = occ.get('waiting_landing') or {}
    out = []
    for iid in sorted(set(pushed) - set(spoken)):
        item = items.get(iid) or {}
        kind = 'task' if item.get('type') == 'task' else 'fix'
        f = _task_feature(items, item) if kind == 'task' else feature_of(items, item)
        number = open_pr_of(item)
        what = f'PR #{number}' if number else 'its branch'
        back = (occ.get('back') or {}).get(iid)
        if iid in waiting:
            action = f'{WAITS_LANDING}: {what} {waiting[iid]}'
        elif back:
            # sent back with no correction pending: the lane returns it to PUSHED on its next
            # pass ("correction answered") — no run is missing, and no session is owed
            action = (f"{WAITS_LANE}: {what} sent back ({back.get('reason') or 'BACK'}) on "
                      f"{back.get('branch')}, no correction pending — the lane re-reads it")
        else:
            action = f'{WAITS_LANDING}: {what} open, no run holds it — the lane takes it up'
        row = Row(tier=review_tier(item), kind=PUSHED_LAND, item_id=iid,
                  feature_id=f['id'] if f else '', action=action, brief_kind='review',
                  branch=_branch_of(item, product, kind), reason='pushed work, no other row')
        out.append(blocked_row(row, item) if item.get('blocked') else row)
    return out


def orphaned_pushed(index, rows, occupancy, inflight):
    """The pushed items (:func:`pushed_ids`) with no row in ``rows`` and no live session — the
    invariant ``asf doctor`` counts (``pushed work``); ``[]`` whenever the feeder holds it."""
    items = items_of(index)
    occ = occupancy or {}
    live = inflight_ids(inflight) | set(occ.get('busy') or ())
    rowed = {r.item_id for r in rows or ()}
    return sorted(pushed_ids(items, occ) - rowed - live)


def finish_phase(items, row):
    """Finish before you start (tier 2 only; a Bug's tier is its own): a row on a Task — its
    coder, correction, review, rebase — is 0 and goes before every row on a Feature itself
    (its spec, plan, adjudicate, landing, decision), which is 1. A product, 2026-09-25, over 7
    days: 36 Features plan-approved and idle, 13 in plan-draft, 3 building, 2 landed — the
    Feature order interleaved new documents with the Tasks of Features already planned, and
    the cut spent the slots on the documents.

    A Feature in a lane experiment (``ab_pair``) is 0 too: both arms of a pair must start in
    the same window, and behind every Task row the capacity cut never reached them — exempt
    from the cap (:func:`finish_first`) yet still starved, the pair never started at all."""
    item = items.get(row.item_id) or {}
    if item.get('type') != 'feature':
        return 0
    return 0 if item.get('ab_pair') else 1


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


def in_build_stage(feature):
    """True for an open Feature whose plan is approved: its Tasks are the work (:data:`BUILD_STAGES`)."""
    return (is_open(feature or {}) and feature.get('type') == 'feature'
            and (feature.get('stage') or '').split(' ')[0] in BUILD_STAGES)


def task_share(items, feature, landed=()):
    """``(landed, total)``: the Feature's Tasks, and how many of them are on the trunk."""
    tasks = ix.feature_tasks(items, feature)
    done = sum(1 for t in tasks if t.get('state') in DONE_STATES or t['id'] in landed)
    return done, len(tasks)


def chain_left(items, feature, landed=(), absorbed=None):
    """The depth of the Feature's longest chain of ``after:`` among its Tasks not yet landed —
    each link is a trip through the lane, so a Feature's time to done is its chain, not its
    count: three parallel Tasks left are 1, three chained are 3. None left: 0."""
    landed = set(landed or ())
    absorbed = absorbers(items) if absorbed is None else absorbed
    left = {t['id']: t for t in ix.feature_tasks(items, feature)
            if t.get('state') not in DONE_STATES and t['id'] not in landed}
    depth = {}

    def walk(tid, seen):
        if tid not in depth:
            before = [a for a in after_of(items, left[tid], absorbed) if a in left and a not in seen]
            depth[tid] = 1 + max((walk(a, seen | {a}) for a in before), default=0)
        return depth[tid]
    return max((walk(t, {t}) for t in left), default=0)


def features_in_build(items, busy=(), landed=()):
    """The Features in build: plan approved, still open, and at least one Task started — landed,
    past ``New``, or held by a session or a branch waiting to land (``busy``). A planned
    Feature none of whose Tasks has started is not in build yet: its first Task is what
    :func:`build_cap` holds."""
    busy, landed = set(busy or ()), set(landed or ())
    out = []
    for f in ix.of_type(items, 'feature'):
        if not in_build_stage(f):
            continue
        if any(t.get('state', 'New') != 'New' or t['id'] in busy or t['id'] in landed
               for t in ix.feature_tasks(items, f)):
            out.append(f['id'])
    return sorted(out)


def features_moving(items, rows, busy=(), landed=(), held=(), live=None):
    """The Features in build (:func:`features_in_build`) that take the factory's bandwidth now:
    a Task held by a live session (``live``; absent, ``busy``), or a Task with a launching row
    (:func:`buildable_features`). A Feature in build with neither — its landed Tasks behind it,
    the rest waiting on work that is not moving — is stalled: holding new work does not finish
    it, so it does not count against :func:`build_cap` (37 counted, 7 moving: the factory's own
    record, 2026-09-28). A Task whose branch only waits to land (CI, review, the merge queue) is
    not a session either: 4 of a product's 8 "moving" Features on 2026-10-03 were such Tasks,
    and they held a STARVED → PLAN at cap 4 while 0 sessions ran."""
    busy = set(busy or ())
    live = busy if live is None else set(live)
    ready = set(buildable_features(items, rows, set(held or ())))
    return [f for f in features_in_build(items, busy, landed)
            if f in ready or any(t['id'] in live for t in ix.feature_tasks(items, items[f]))]


def live_ids(inflight, occupancy):
    """The items a session works on now: a running session's, and a live run the occupancy
    names (``busy``) — never one whose work only waits to land (:func:`occupied` has both)."""
    return inflight_ids(inflight) | set((occupancy or {}).get('busy') or ())


def build_state(items, product, capacity, inflight=(), occupancy=None, landed_shas=None,
                bandwidth=None, rows=None, attempts=None, groom_state=None, held=None, gate=None,
                adjudicated=None, unverified_landed=None, unverified_on_trunk=None):
    """``(X, N, why, binds)``: the Features in build that are moving (:func:`features_moving`),
    the cap (:func:`features_cap`), the inputs that set it, and whether the cap holds anything
    now — what ``asf next`` and ``asf status`` show. ``binds``: :func:`build_cap` turns at least
    one launching row into ``WAITS ON finish``. X >= N alone holds nothing when no Feature in
    build has a Task row that launches, so no view may then say the cap stops a start
    (2026-10-03: "7 / 4 — no new Feature starts" while 0 sessions ran and the one launching row,
    a STARVED → PLAN, launched). ``rows``: the uncut candidates; absent, they are drawn here as
    :func:`plan_rows` draws them — the same inputs, so the count is the one the cap reads."""
    index, items = items, items_of(items)
    busy = inflight_ids(inflight) | occupied(occupancy)
    if rows is None:
        rows = candidates(index, product, inflight, attempts, occupancy=occupancy,
                          groom_state=groom_state, landed_shas=landed_shas,
                          adjudicated=adjudicated, unverified_landed=unverified_landed,
                          unverified_on_trunk=unverified_on_trunk)
        if gate is not None:
            rows = gate(rows, items)
    landed = landed_ids(items, landed_shas)
    moving = features_moving(items, rows, busy, landed, held, live_ids(inflight, occupancy))
    cap, why = features_cap(product, capacity, bandwidth)
    capped = build_cap(rows, items, product, inflight, capacity, held, occupancy=occupancy,
                       landed_shas=landed_shas, bandwidth=bandwidth)
    binds = any(a.launches and not b.launches for a, b in zip(rows, capped))
    return len(moving), cap, why, binds


def build_load(items, product, capacity, inflight=(), occupancy=None, landed_shas=None,
               bandwidth=None, rows=None, attempts=None, groom_state=None, held=None, gate=None,
               adjudicated=None, unverified_landed=None, unverified_on_trunk=None):
    """``(X, N, why)`` of :func:`build_state`."""
    return build_state(items, product, capacity, inflight, occupancy, landed_shas, bandwidth,
                       rows, attempts, groom_state, held, gate, adjudicated,
                       unverified_landed, unverified_on_trunk)[:3]


def build_load_line(x, cap, why, binds=None):
    """How every view writes :func:`build_load`: ``Features in build 7 / 6 (auto: …)``; with
    ``binds`` (:func:`build_state`) it says whether the cap holds a start now."""
    return f"Features in build {x} / {cap} ({why})" + build_binds_note(x, cap, binds)


def build_binds_note(x, cap, binds):
    """`` — no new Feature starts`` only while the cap holds a row (``binds``); at or over the cap
    with nothing held, `` — the cap holds no row now``."""
    if binds is None or x < cap:
        return ''
    return ' — no new Feature starts' if binds else ' — the cap holds no row now'


def build_cap(rows, items, product, inflight, capacity, held=(), occupancy=None,
              landed_shas=None, bandwidth=None):
    """``feeder.max_features_in_build`` (default ``auto``, :func:`features_cap`): while the
    product has N or more Features in build (:func:`features_in_build`), no new Feature starts —
    neither a spec or plan (:data:`NEW_DOC_KINDS`) nor the first Task of a planned Feature
    nothing of which has started. Each such launching row becomes ``WAITS ON finish: X in build,
    cap N (…)``. Rows of a Feature already in build (its Tasks, corrections, reviews) are never
    held, nor a correction of a document the lane refused, nor a lane experiment's arm
    (``ab_pair``).

    No Feature in build has a row that launches (:func:`buildable_features`): no cap — the new
    work is the only work there is, and holding it would idle the product."""
    held = set(held or ())
    busy = inflight_ids(inflight) | occupied(occupancy)
    landed = landed_ids(items, landed_shas)
    building = set(features_in_build(items, busy, landed))
    moving = features_moving(items, rows, busy, landed, held, live_ids(inflight, occupancy))
    cap, why = features_cap(product, capacity, bandwidth)
    if len(moving) < cap or not building & set(buildable_features(items, rows, held)):
        return rows
    reason = f"{len(moving)} in build, cap {cap} ({why})"
    out = []
    for r in rows:
        f = items.get(r.feature_id) or items.get(r.item_id) or {}
        first_task = (r.kind == PLAN_CODE
                      or r.kind == DELIVERY_CODE and feature_delivery(items.get(r.item_id)))
        if (r.launches and not r.correction and r.item_id not in held and not f.get('ab_pair')
                and r.feature_id not in building
                and (r.kind in NEW_DOC_KINDS or (first_task and in_build_stage(f)))):
            r = dataclasses.replace(r, action=f'{FINISH}: {reason}', waits_on='finish',
                                    reason=f'finish before you start — {reason}')
        out.append(r)
    return out


def plan_ahead_cap(rows, items, product, inflight, capacity, held=(), occupancy=None,
                   landed_shas=None, bandwidth=None):
    """Specs and plans just in time (``flags.plan_ahead``, :func:`plan_ahead`): a spec or plan
    row (:data:`SPEC_PLAN_KINDS`) launches only while the Features moving
    (:func:`features_moving`) + the spec/plan sessions in flight + the spec/plan rows admitted
    so far stay under the build cap (:func:`features_cap`) + ``plan_ahead``; every launching
    one past that becomes ``WAITS ON build slot (plan_ahead N): …``. A spec written weeks before
    a build slot opens is written twice — the product moved under it (a product, 2026-10: 74
    Features specified and waiting, spec+plan the largest line of its spend). Rows go in Feature
    order, so the nearest card is specified first. Never held: a correction of a document the
    lane refused, a held item, a lane experiment's arm (``ab_pair``). Unset: no limit (today)."""
    ahead = plan_ahead(product)
    if ahead is None:
        return rows
    held = set(held or ())
    busy = inflight_ids(inflight) | occupied(occupancy)
    landed = landed_ids(items, landed_shas)
    moving = len(features_moving(items, rows, busy, landed, held, live_ids(inflight, occupancy)))
    cap, why = features_cap(product, capacity, bandwidth)
    running = sum(1 for s in inflight or () if s.get('kind') in SPEC_PLAN_SESSIONS)
    limit = cap + ahead
    admitted = sum(1 for r in rows if r.kind in SPEC_PLAN_KINDS and r.launches and r.correction
                   and r.item_id not in held)
    out = []
    for r in rows:
        paired = (items.get(r.feature_id) or items.get(r.item_id) or {}).get('ab_pair')
        if (r.kind in SPEC_PLAN_KINDS and r.launches and not r.correction
                and r.item_id not in held and not paired):
            if moving + running + admitted < limit:
                admitted += 1
            else:
                reason = (f"{moving} moving + {running} spec/plan in flight + {admitted} this "
                          f"wave, build cap {cap} ({why}) + plan_ahead {ahead}")
                r = dataclasses.replace(r, action=f'{WAITS_BUILD_SLOT} (plan_ahead {ahead}): '
                                                  f'{reason}',
                                        waits_on='build slot',
                                        reason=f'just in time — {reason}')
        out.append(r)
    return out


def finish_first(rows, items, product, inflight, held=(), free=None):
    """``feeder.max_specs_in_flight`` (default 2): while a planned Feature has Tasks ready to
    build (:func:`buildable_features`), new spec and plan sessions (:data:`NEW_DOC_KINDS`) are
    capped — the ones already running count, and so does a correction of a document the lane
    refused (never held itself) — and each launching row past the cap becomes ``WAITS ON
    finish: …`` with the numbers. No buildable Feature: no cap, the documents are the only work
    there is.

    A RESHAPE → REPLAN row is no new document: it re-cuts a planned Feature so its Tasks build.
    While ``free`` seats remain (None: unknown, read as free) it is outside that cap, bounded by
    its own ceiling ``feeder.max_replans_in_flight`` (default 6; the replans running count) — 18
    pending replans once hid behind a cap of 2 with local seats idle (2026-10-05). With no free
    seat it waits under the spec/plan cap as before."""
    held = set(held or ())
    ready = buildable_features(items, rows, held)
    if not ready:
        return rows
    cap = max_specs_in_flight(product)
    replan_cap = max_replans_in_flight(product)
    replans = sum(1 for s in inflight or () if s.get('kind') == REPLAN_KIND)
    running = sum(1 for s in inflight or () if s.get('kind') in NEW_DOC_SESSIONS) - replans
    free = None if free is None else max(int(free), 0)
    # a correction of a document the lane refused is never held, but it is a document session
    # this wave: it counts against the cap before any new one is admitted
    admitted = sum(1 for r in rows if r.kind in NEW_DOC_KINDS and r.launches and r.correction
                   and r.item_id not in held)
    out = []
    named = ', '.join(ready[:3]) + (f' +{len(ready) - 3}' if len(ready) > 3 else '')
    for r in rows:
        # a Feature in a lane experiment (``ab_pair``) is never held by the cap: both arms of a
        # pair must start in the same window, or the comparison measures the queue, not the lane
        paired = (items.get(r.feature_id) or items.get(r.item_id) or {}).get('ab_pair')
        if (r.kind in NEW_DOC_KINDS and r.launches and not r.correction and r.item_id not in held
                and not paired):
            if r.kind == REPLAN and (free is None or free > 0):
                if replans < replan_cap:
                    replans += 1
                    free = None if free is None else free - 1
                else:
                    why = (f"{replans} replan(s) in flight or admitted, cap {replan_cap} "
                           f"(feeder.max_replans_in_flight)")
                    r = dataclasses.replace(r, action=f'{FINISH}: {why}', waits_on='finish',
                                            reason=f'finish before you start — {why}')
                out.append(r)
                continue
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


#: the mark a launching row carries while its job fails to spawn tick after tick
#: (:meth:`asf.workers.wave.Failures.read`): the row still launches — the retry is what might
#: succeed — but ``asf next`` and ``asf status`` say why nothing has started
FAILING_TO_SPAWN = 'FAILING TO SPAWN'


def failing_to_spawn(rows, failing):
    """``rows`` with each launching row whose job is in ``failing`` (``{job: {'reason',
    'count'}}``) marked ``would launch — FAILING TO SPAWN: <reason> ×<count>``; it keeps its
    tier and still launches."""
    if not failing:
        return rows
    from asf.tick.step_wave import row_job  # local: the wave step imports the feeder
    from asf.workers.wave import short_reason
    out = []
    for r in rows:
        f = failing.get(row_job(r)) if r.launches else None
        if f and FAILING_TO_SPAWN not in r.action:
            r = dataclasses.replace(r, action=f"{LAUNCH} — {FAILING_TO_SPAWN}: "
                                              f"{short_reason(f.get('reason'))} ×{f.get('count')}")
        out.append(r)
    return out


def plan_rows(index, product, inflight, capacity, attempts=None, occupancy=None,
              groom_state=None, landed_shas=None, decision_limit=None, held=None, exclude=None,
              s1_first=True, gate=None, bandwidth=None, adjudicated=None,
              unverified_landed=None, failing=None, unverified_on_trunk=None):
    """The rows the tick emits: tiered, S1 first, cut to ``capacity`` less what is in flight.
    ``s1_first=False``: no S1 cut of the tier-2 rows (:func:`asf.feeder.tiers.select`).
    ``held``: the item ids an approval hold parks — shown, but given no slot. ``exclude``: the
    launching rows (:func:`asf.invariants.row_key`) the feeder's invariant gate dropped — they
    are not candidates, so the cut hands their slots to the next rows. ``gate``: ``gate(rows,
    items) -> rows``, run before the cut — the launch-time invariants
    (:func:`asf.invariants.feeder_waits`) turn a violating row into a WAITS row, so it takes no
    seat and every view shows it waiting, never "would launch". ``bandwidth``: the facts
    ``feeder.max_features_in_build: auto`` sizes its cap from (:func:`build_cap`). ``failing``:
    the jobs failing to spawn (:func:`failing_to_spawn`) — their rows say so."""
    from asf.feeder import tiers
    rows = candidates(index, product, inflight, attempts, occupancy=occupancy,
                      groom_state=groom_state, landed_shas=landed_shas,
                      decision_limit=decision_limit, adjudicated=adjudicated,
                      unverified_landed=unverified_landed,
                      unverified_on_trunk=unverified_on_trunk)
    if exclude:
        from asf.invariants import row_key
        rows = [r for r in rows if not (r.launches and row_key(r) in exclude)]
    if gate is not None:
        rows = gate(rows, items_of(index))
    rows = build_cap(rows, items_of(index), product, inflight, capacity, held,
                     occupancy=occupancy, landed_shas=landed_shas, bandwidth=bandwidth)
    rows = plan_ahead_cap(rows, items_of(index), product, inflight, capacity, held,
                          occupancy=occupancy, landed_shas=landed_shas, bandwidth=bandwidth)
    free = None if capacity is None else capacity - len(inflight or ())
    rows = finish_first(rows, items_of(index), product, inflight, held, free=free)
    keep = pushed_ids(items_of(index), occupancy)
    return failing_to_spawn(tiers.select(rows, inflight, capacity, held=held, s1_first=s1_first,
                                         keep=keep),
                            failing)
