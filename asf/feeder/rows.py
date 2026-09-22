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

``candidates()`` lists every row; ``plan_rows()`` hands them to :mod:`asf.feeder.tiers` to order
and cut to capacity.
"""
import dataclasses
import re

from asf.feeder import footprint
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

LAUNCH = 'would launch'
DONE_STATES = ('Resolved', 'Closed')
STALEMATE_ROUND = 4
ATTEMPT_LIMIT = 3
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
    return (product.conventions if product is not None else {}) or {}


def branch_for(product, kind, item_id, default=None):
    """``<prefix><id>``; a prefix not already ending in ``/`` or ``-`` gets a ``/``. The
    product's prefix for ``kind``, else the caller's default, else the documented default
    (``code`` → ``worker/``, B-0067), else ``<kind>/``."""
    from asf.conventions import DEFAULT_BRANCH_PREFIXES
    prefixes = _conventions(product).get('branch_prefixes') or {}
    prefix = prefixes.get(kind) or default or DEFAULT_BRANCH_PREFIXES.get(kind) or kind
    if isinstance(prefix, (list, tuple)):
        prefix = kind
    if not prefix.endswith(('/', '-')):
        prefix += '/'
    return f"{prefix}{item_id}"


def stalemate_round(product):
    """``conventions.stalemate_round`` (default 4): the review round that stops the loop."""
    v = _conventions(product).get('stalemate_round')
    return v if isinstance(v, int) and v > 0 else STALEMATE_ROUND


def attempt_limit(product):
    """``conventions.attempt_limit`` (default 3): fix sessions a Bug gets before it is adjudicated."""
    v = _conventions(product).get('attempt_limit')
    return v if isinstance(v, int) and v > 0 else ATTEMPT_LIMIT


# ---- per-item predicates ----------------------------------------------------

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
                           branch=branch_for(product, 'fix', b['id'], default='fix/'),
                           reason=f"{sev} open after {n} sessions: adjudicate, not another fix"))
            continue
        out.append(Row(tier=0 if sev == 'S1' else 1, kind=BUG_FIX, item_id=b['id'],
                       feature_id=fid, action=LAUNCH, brief_kind='fix-bug',
                       branch=branch_for(product, 'fix', b['id'], default='fix/'),
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
        branch = c.get('branch') or branch_for(product, kind, iid, default='fix/' if kind == 'fix' else None)
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
    """[(task_id, writes)] of every Task in flight: held by a session, or Active in the index."""
    out = []
    for t in sorted(ix.of_type(items, 'task'), key=lambda v: v['id']):
        if (t['id'] in busy or t.get('state') == 'Active') and t.get('writes'):
            out.append((t['id'], list(t['writes'])))
    return out


def feature_rows(items, product, busy, running):
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
            out.extend(task_rows(items, product, f, busy, running))
    return out


def task_rows(items, product, feature, busy, running):
    out = []
    tasks = [t for t in ix.feature_tasks(items, feature)
             if t.get('state', 'New') == 'New' and t['id'] not in busy and not t.get('blocked')]
    for t in sorted(tasks, key=lambda v: (ix.rank(v), v['id'])):
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


KIND_ORDER = {STALEMATE: 0, CONFLICT: 1, STALE: 2}


def candidates(index, product, inflight, attempts=None, corrections=None, busy=None):
    """Every row the index supports right now, uncut by capacity, in emit order: tier, then the
    Feature's rank and id, then within a Feature the stalemate, branch housekeeping, new work.
    ``busy``: item ids held by something that is not a session and takes no slot — a pushed
    branch waiting for harvest (:func:`asf.workers.lifecycle.awaiting_harvest`)."""
    items = items_of(index)
    busy = inflight_ids(inflight) | set(busy or ())
    limit = stalemate_round(product)
    stalled = {f['id'] for f in ix.of_type(items, 'feature') if review_round(f)[1] >= limit}
    running = running_footprints(items, busy)
    corrected, spoken = correction_rows(items, product, busy, corrections)
    rows = corrected + bug_rows(items, product, busy | spoken, attempts)
    rows += [r for r in branch_rows(items, product, busy) if r.feature_id not in stalled]
    rows += feature_rows(items, product, busy, running)

    def key(pair):
        seq, r = pair
        f = items.get(r.feature_id) or {}
        if r.tier < 2:
            return (r.tier, 0, '', 0, seq)
        return (r.tier, ix.rank(f), r.feature_id or '~', KIND_ORDER.get(r.kind, 3), seq)
    return [r for _seq, r in sorted(enumerate(rows), key=key)]


def plan_rows(index, product, inflight, capacity, attempts=None, corrections=None, busy=None):
    """The rows the tick emits: tiered, S1 first, cut to ``capacity`` less what is in flight."""
    from asf.feeder import tiers
    return tiers.select(candidates(index, product, inflight, attempts, corrections, busy=busy),
                        inflight, capacity)
