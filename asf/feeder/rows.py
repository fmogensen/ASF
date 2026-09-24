"""asf.feeder.rows — what the tick should start next, as rows.

Every row is read off ``index.json`` (the items' typed fields plus the machine block ingest
derived: ``state``, ``stage``, ``stage_since``, ``evidence``, ``blocked``) and the caller's
``inflight`` list — one dict per running session, ``{'item': id, 'kind': brief kind, 'account':
worker, 'age': '12m'}``. No git, no gh, no filesystem: a fact the index does not carry is not a
fact the feeder can use.

The row kinds::

    BUG → FIX              an open, decided S1/S2 Bug with no session (the S1 lane)
    FIX → CORRECT          an item whose branch the harvest held (red gate, conflict) fewer than
                           3 times: back to a session with the failing output
    STALEMATE → ADJUDICATE a Feature at spec-/plan-review round >= 4: adjudicate, and nothing
                           else for that Feature (another review round will not converge); or a
                           Bug with 3 sessions behind it and still open (not a fourth fix)
    CONFLICT → REBASE      an Active Task/Bug whose PR no longer merges, no session on it
    STALE → CLOSE          an Active Task/Bug whose PR was closed unmerged, branch left behind
    CARD → SPEC            a decided Feature card with no spec
    STARVED → SPEC         a spec in draft/review that no session is moving
    STARVED → PLAN         an approved spec with no plan, or a plan in draft/review, unmoved
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

``candidates()`` lists every row; ``plan_rows()`` hands them to :mod:`asf.feeder.tiers` to order
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
STALEMATE = 'STALEMATE → ADJUDICATE'
CONFLICT = 'CONFLICT → REBASE'
STALE = 'STALE → CLOSE'
CARD_SPEC = 'CARD → SPEC'
STARVED_SPEC = 'STARVED → SPEC'
STARVED_PLAN = 'STARVED → PLAN'
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
REVIEW_RE = re.compile(r'^(spec|plan)-review r(\d+)')
CLOSED_PR_RE = re.compile(r'\bPR #\d+ CLOSED\b')
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
        if rounds >= CORRECTION_ROUNDS:
            out.append(Row(tier=tier, kind=STALEMATE, item_id=iid, feature_id=fid, action=LAUNCH,
                           brief_kind='adjudicate', branch=branch,
                           reason=f"held {rounds} times ({c.get('kind')}): adjudicate, not another correction"))
        else:
            out.append(Row(tier=tier, kind=FIX_CORRECT, item_id=iid, feature_id=fid, action=LAUNCH,
                           brief_kind='correct', branch=branch, correction=c['text'],
                           reason=f"harvest held it ({c.get('kind')}), round {rounds}: back to a session"))
    return out, ids


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


def feature_rows(items, product, busy, running, landed_shas=None):
    """Every Feature's rows, in Feature order (rank, then id). ``running`` grows as PLAN → CODE
    rows are handed out, so two ready Tasks sharing a file never both launch."""
    out = []
    limit = stalemate_round(product)
    feats = [f for f in ix.of_type(items, 'feature')
             if f.get('decided') is True and is_open(f) and not f.get('blocked')]
    for f in sorted(feats, key=lambda v: (ix.rank(v), v['id'])):
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
        if word == 'card':
            out.append(Row(tier=2, kind=CARD_SPEC, item_id=fid, feature_id=fid, action=LAUNCH,
                           brief_kind='spec', branch=branch_for(product, 'spec', fid),
                           reason='decided card, no spec'))
        elif word in ('spec-draft', 'spec-review'):
            out.append(Row(tier=2, kind=STARVED_SPEC, item_id=fid, feature_id=fid, action=LAUNCH,
                           brief_kind='spec', branch=branch_for(product, 'spec', fid),
                           reason=f"{stage}, no session"))
        elif word in ('spec-approved', 'plan-draft', 'plan-review'):
            out.append(Row(tier=2, kind=STARVED_PLAN, item_id=fid, feature_id=fid, action=LAUNCH,
                           brief_kind='plan', branch=branch_for(product, 'plan', fid),
                           reason=f"{stage}, no session" if word != 'spec-approved'
                           else 'spec approved, no plan'))
        elif word in ('plan-approved', 'building'):
            out.extend(task_rows(items, product, f, busy, running, landed_shas))
    return out


def task_rows(items, product, feature, busy, running, landed_shas=None):
    out = []
    tasks = [t for t in ix.feature_tasks(items, feature)
             if t.get('state', 'New') == 'New' and t['id'] not in busy and not t.get('blocked')]
    landed = landed_ids(items, landed_shas)
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
        pending = [a for a in (t.get('after') or []) if a not in landed]
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
        other = footprint.first_conflict(writes, running)
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
                       reason='plan approved, footprint free' if writes else 'plan approved, no writes: declared'))
        if writes:
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
    out, said = [], set()
    for r in rows:
        pending = [a for a in (items.get(r.item_id) or {}).get('after') or [] if a not in landed]
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


def candidates(index, product, inflight, attempts=None, corrections=None, busy=None,
              groom_state=None, landed_shas=None, decision_limit=None):
    """Every row the index supports right now, uncut by capacity, in emit order: tier, then the
    Feature's rank and id, then within a Feature the stalemate, branch housekeeping, new work.
    ``busy``: item ids held by something that is not a session and takes no slot — a pushed
    branch waiting for harvest (:func:`asf.workers.lifecycle.awaiting_harvest`). ``groom_state``:
    §2.5's fact for the GROOM → ADJUDICATE row; a caller that passes none gets none. A card
    already Resolved/Closed is never ``busy``: its work is on the trunk whatever the ledger says
    (an unclosed run held a landed Task's ``writes:`` against its siblings for ever).
    ``decision_limit``: how many UNDECIDED → DECIDE rows (``None``: ``decision_rows``; ``0``: all)."""
    items = items_of(index)
    busy = inflight_ids(inflight) | {i for i in busy or () if is_open(items.get(i) or {})}
    limit = stalemate_round(product)
    stalled = {f['id'] for f in ix.of_type(items, 'feature') if review_round(f)[1] >= limit}
    running = running_footprints(items, busy)
    corrected, spoken = correction_rows(items, product, busy, corrections)
    rows = corrected + bug_rows(items, product, busy | spoken, attempts)
    rows += [r for r in branch_rows(items, product, busy) if r.feature_id not in stalled]
    gr = groom_row(index, product, busy, groom_state, inflight)
    if gr is not None:
        rows.append(gr)
    rows += feature_rows(items, product, busy, running, landed_shas)
    rows += undecided_rows(items, product, busy, decision_limit)
    rows = hold_unlanded(rows, items, landed_shas)

    def key(pair):
        seq, r = pair
        f = items.get(r.feature_id) or {}
        if r.tier < 2:
            return (r.tier, 0, '', 0, seq)
        return (r.tier, ix.rank(f), r.feature_id or '~', KIND_ORDER.get(r.kind, 4), seq)
    return [r for _seq, r in sorted(enumerate(rows), key=key)]


def plan_rows(index, product, inflight, capacity, attempts=None, corrections=None, busy=None,
              groom_state=None, landed_shas=None, decision_limit=None):
    """The rows the tick emits: tiered, S1 first, cut to ``capacity`` less what is in flight."""
    from asf.feeder import tiers
    return tiers.select(candidates(index, product, inflight, attempts, corrections, busy=busy,
                                   groom_state=groom_state, landed_shas=landed_shas,
                                   decision_limit=decision_limit),
                        inflight, capacity)
