"""asf.invariants — the executable invariants: the registry, the facts they read, and the
tick's check points.

Each :class:`Invariant` has an ``id`` (``I1``…), a ``scope`` and a ``check(ctx) -> [Finding]``,
pure over the facts ``ctx`` carries. They run at three points of the tick, and every one fails
**soft** (R9) — an invariant never aborts a tick, and a check that raises is one line:

- ``record`` (I1, I2, I3, I10, I11): each staged writer's change before the tick's commit
  (:mod:`asf.record.stage`; I2 after ingest's restamp). A finding puts back only the offending
  paths, the rest of the tick commits, and :func:`asf.tick.tick.file_invariant_bugs` files one
  Bug per ``(invariant, path)``.
- ``feeder`` (I4, I5, I7): over ``plan_rows`` before the wave (:func:`feeder_gate`). A
  violating row is dropped and logged as ``INVARIANT <id>: <row> — <why>``.
- ``lane`` (I8): after harvest (:func:`lane_report`). Reported only. I9 is an event there, not a
  violation: a merge from outside the lane is logged, never refused (R12).

``asf check --invariants`` runs them all read-only against the record and the state directory
(``--deep`` adds I6). I6, I12 and I13 are tests (:data:`TEST_ONLY`), never tick checks (R13).
"""
import os
from dataclasses import dataclass, field
from typing import Callable

#: The points of the tick an invariant runs at.
SCOPES = ('record', 'feeder', 'lane')


@dataclass(frozen=True)
class Finding:
    """One violation: which invariant, where, and what to revert (``record`` scope) or drop
    (``feeder`` scope, ``subject`` the row)."""
    invariant: str
    scope: str
    subject: str
    message: str
    paths: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Invariant:
    """``check(ctx) -> [Finding]``, pure over ``ctx``."""
    id: str
    scope: str
    check: Callable


#: The registry the tick runs: I1–I3, I10, I11 (record), I4, I5, I7 (feeder), I8 (lane).
INVARIANTS = []


def run(ctx, scope=None, out=None):
    """Every registered invariant (only those of ``scope`` when given) over ``ctx``: the
    findings, in registry order; ``[]`` when all hold.

    Soft (R9): a check that raises is one ``INVARIANT <id>: check failed (<error>)`` line and no
    finding — a broken check never refuses a write, drops a row or stops the tick."""
    if scope is not None and scope not in SCOPES:
        raise ValueError(f'not an invariant scope: {scope!r} (one of {", ".join(SCOPES)})')
    findings = []
    for inv in INVARIANTS:
        if scope is None or inv.scope == scope:
            try:
                findings.extend(inv.check(ctx) or ())
            except Exception as e:  # noqa: BLE001 — an invariant fails soft, never the tick
                (out or print)(f'INVARIANT {inv.id}: check failed ({type(e).__name__}: {e})')
    return findings


# ---- the record invariants (I1, I2, I3, I10, I11) ------------------------------------------
#
# Each reads a ``stage.RecordContext``: the clone ``root`` and one writer's ``Staged`` change.
# A finding names only paths that writer changed, and only a violation the writer *introduced*
# (absent before it ran): a card that was already in that shape is not this writer's to answer
# for, and refusing its write would only freeze the card where it stands.

def _card_paths(staged):
    from asf.record.core import ITEM_FOLDERS
    return [p for p in staged.paths
            if p.endswith('.md') and p.split('/', 1)[0] in ITEM_FOLDERS and p.count('/') == 1]


def _parse(text, path):
    from asf.record import frontmatter
    if text is None:
        return None
    try:
        return frontmatter.parse(text, path=path)[0]
    except frontmatter.FrontmatterError:
        return False


def _after(ctx, path):
    import os
    key = ('after', path)
    if key not in ctx.cache:
        full = os.path.join(ctx.root, path)
        text = None
        if os.path.isfile(full):
            with open(full, encoding='utf-8', errors='surrogateescape') as f:
                text = f.read()
        ctx.cache[key] = (text, _parse(text, path))
    return ctx.cache[key]


def _before(ctx, path):
    key = ('before', path)
    if key not in ctx.cache:
        text = ctx.staged.before.get(path)
        ctx.cache[key] = (text, _parse(text, path))
    return ctx.cache[key]


def _machine(meta):
    from asf.record import frontmatter
    return frontmatter.split_machine(meta)[1] if meta else {}


def _finding(inv, subject, message, path):
    return Finding(inv, 'record', subject, message, (path,))


def check_i1(ctx):
    """I1 — never strip machine keys: every machine key a card had before the writer ran is still
    there after it, except the derivable ones (``frontmatter.DERIVABLE_KEYS``); a card the writer
    left unparseable has lost its whole block."""
    from asf.record import frontmatter
    out = []
    for path in _card_paths(ctx.staged):
        _bt, before = _before(ctx, path)
        text, after = _after(ctx, path)
        if not before or text is None:
            continue  # a card this writer created, or removed: nothing of it was stripped
        if after is False:
            out.append(_finding('I1', path, 'the card no longer parses', path))
            continue
        lost = [k for k in _machine(before) if k not in _machine(after)
                and k not in frontmatter.DERIVABLE_KEYS]
        if lost:
            out.append(_finding('I1', after.get('id') or path,
                                f"machine key(s) stripped: {', '.join(lost)}", path))
    return out


def check_i2(ctx):
    """I2 — every card carries the record's ``schema_version`` (``schema.record_version``). It
    runs after ingest's restamp (R11): a card the migration left below the stamp is brought up by
    the restamp in the same writer, so only a writer that writes a card below the stamp — a new
    one, or one it stripped — is refused."""
    from asf import schema
    if 'record_version' not in ctx.cache:
        ctx.cache['record_version'] = schema.record_version(ctx.root)
    version = ctx.cache['record_version']
    if not version:
        return []  # no index.json yet, or an unstamped record: `schema-migrate`'s to stamp
    out = []
    for path in _card_paths(ctx.staged):
        _bt, before = _before(ctx, path)
        _at, after = _after(ctx, path)
        if not after:
            continue
        have = int(_machine(after).get('schema_version') or 0)
        had = int(_machine(before).get('schema_version') or 0) if before else None
        if have < version and (had is None or had >= version or have < had):
            out.append(_finding('I2', after.get('id') or path,
                                f'schema_version {have or "missing"} below the record\'s {version}',
                                path))
    return out


def _record(ctx):
    """``{id: meta}`` of every card after the writer, loaded once per validation."""
    if 'record' not in ctx.cache:
        from asf.record.core import canonicalize, load_items
        by_id, _errors = load_items(ctx.root)
        canonical, _dupes = canonicalize(by_id)
        ctx.cache['record'] = {iid: rec['meta'] for iid, rec in canonical.items()}
        ctx.cache['relpath'] = {iid: rec['relpath'] for iid, rec in canonical.items()}
    return ctx.cache['record']


def _before_record(ctx):
    """The same record as it stood before the writer: the changed cards read from ``before``."""
    if 'record_before' not in ctx.cache:
        metas = dict(_record(ctx))
        for path in _card_paths(ctx.staged):
            _at, after = _after(ctx, path)
            if after and after.get('id') in metas:
                del metas[after['id']]
            _bt, before = _before(ctx, path)
            if before and before.get('id'):
                metas[before['id']] = before
        ctx.cache['record_before'] = metas
    return ctx.cache['record_before']


def _state(meta):
    return _machine(meta).get('state', 'New')


def _active_writes(metas):
    return {iid: [str(w) for w in (m.get('writes') or ())] for iid, m in metas.items()
            if m.get('type') == 'task' and not m.get('removed') and _state(m) == 'Active'
            and m.get('writes')}


def _writes_of(meta):
    return sorted(str(w) for w in (meta.get('writes') or ()))


def _intersecting(metas):
    from asf.record.core import writes_intersect
    tasks = sorted(_active_writes(metas).items())
    pairs = set()
    for i, (a, wa) in enumerate(tasks):
        for b, wb in tasks[i + 1:]:
            if any(writes_intersect(x, y) for x in wa for y in wb):
                pairs.add((a, b))
    return pairs


def check_i3(ctx):
    """I3 — never write intersecting ``writes:``: no two Active Tasks' ``writes:`` intersect by
    ``asf check``'s own test (``core.writes_intersect``). A pair this writer's change created is
    refused at the card(s) of the pair it wrote — the widen pass, ``asf set`` and the plan minter
    are refused before their commit, not after it.

    Only a card whose ``writes:`` this writer wrote (changed, or a new card) is judged: a
    derived state change is a fact, never a footprint write. Ingest moving a Task to Active
    because its branch exists would otherwise be put back on every tick — the record lying
    about the branch, one refusal per tick, forever."""
    changed_ids = {}
    for path in _card_paths(ctx.staged):
        _at, after = _after(ctx, path)
        if not after or not after.get('id'):
            continue
        _bt, before = _before(ctx, path)
        if before and _writes_of(before) == _writes_of(after):
            continue
        changed_ids[after['id']] = path
    if not changed_ids or not any(m.get('type') == 'task' for p in changed_ids.values()
                                  for m in [_after(ctx, p)[1]]):
        return []
    new = _intersecting(_record(ctx)) - _intersecting(_before_record(ctx))
    out = []
    for a, b in sorted(new):
        for iid, other in ((a, b), (b, a)):
            if iid in changed_ids:
                out.append(_finding('I3', iid, f"writes: intersects Active task {other}'s writes:",
                                    changed_ids[iid]))
    return out


def _unclosed_tasks(metas, fid):
    return sorted(iid for iid, m in metas.items() if m.get('type') == 'task'
                  and m.get('parent') == fid and not m.get('removed') and _state(m) != 'Closed')


def _landed(meta):
    m = _machine(meta)
    return m.get('state') in ('Resolved', 'Closed') or m.get('stage') in ('landed', 'on-prod')


def check_i10(ctx):
    """I10 — a Feature is Resolved (or landed) only when every Task under it is Closed; so a
    merged docs PR, which closes no Task, never lands a Feature that has Tasks."""
    out = []
    for path in _card_paths(ctx.staged):
        _at, after = _after(ctx, path)
        if not after or after.get('type') != 'feature' or not _landed(after):
            continue
        fid = after.get('id')
        open_now = _unclosed_tasks(_record(ctx), fid)
        if not open_now:
            continue
        _bt, before = _before(ctx, path)
        if before and _landed(before) and _unclosed_tasks(_before_record(ctx), fid):
            continue  # already so before this writer
        out.append(_finding('I10', fid, f"{_state(after)} with open Task(s) {', '.join(open_now)}",
                            path))
    return out


DERIVED_HEADINGS = ('## Children', '## Backlinks')


def _derived_text(text):
    from asf.record.core import parse_sections
    _pre, sections = parse_sections(text or '')
    return '\n'.join(c for h, c in sections if h.strip() in DERIVED_HEADINGS)


def check_i11(ctx):
    """I11 — derived text (Backlinks, Children) never carries a token the redaction gate refuses
    (``redact.scan_text`` with the operator's patterns): the titles it quotes are scrubbed."""
    from asf import redact
    pats = ctx.cache.get('redact')
    if pats is None:
        pats = ctx.cache['redact'] = redact.default_patterns(ctx.root)
    if not pats:
        return []
    out = []
    for path in _card_paths(ctx.staged):
        text, after = _after(ctx, path)
        if not after:
            continue
        hits = redact.scan_text(path, _derived_text(text), pats)
        if not hits:
            continue
        before_text, _b = _before(ctx, path)
        if before_text is not None and redact.scan_text(path, _derived_text(before_text), pats):
            continue
        out.append(_finding('I11', after.get('id') or path,
                            f'derived Children/Backlinks carry a {hits[0].kind} ({hits[0].source})',
                            path))
    return out


def _staged_only(check):
    """A record check reads a writer's staged change; any other context has none to judge."""
    def run_it(ctx):
        if getattr(ctx, 'staged', None) is None or not getattr(ctx, 'root', None):
            return []
        return check(ctx)
    run_it.__name__ = check.__name__
    run_it.__doc__ = check.__doc__
    return run_it


INVARIANTS.extend([
    Invariant('I1', 'record', _staged_only(check_i1)),
    Invariant('I2', 'record', _staged_only(check_i2)),
    Invariant('I3', 'record', _staged_only(check_i3)),
    Invariant('I10', 'record', _staged_only(check_i10)),
    Invariant('I11', 'record', _staged_only(check_i11)),
])


# ---- the feeder invariants (I4, I5, I7) ----------------------------------------------------
#
# Each reads a :class:`FeederContext`: the rows ``plan_rows`` planned and the facts the tick
# already holds. A finding's ``subject`` is :func:`row_key` of the row to drop (or a branch /
# worktree for two live runs, which no dropped row can mend — reported only).

@dataclass
class FeederContext:
    """What a ``feeder``-scope check reads.

    - ``rows``: the planned feeder rows (:class:`asf.feeder.rows.Row`), in tier order;
    - ``lanes``: ``{branch: lane record}`` (``{state, head, pr, at, reason}``) — the lane state
      read through ``lane.snapshot``/``lane.state``; ``{}`` when the lane has none to give;
    - ``occupancy``: :func:`asf.workers.lifecycle.occupancy`'s answer, or None — the fallback for
      a branch the lane holds no record of;
    - ``runs``: the runs that hold a seat, each ``{job, item, branch, worktree}``;
    - ``docs_on_trunk``: ``{feature id: {'spec': bool|None, 'plan': bool|None}}`` — whether each
      document is on ``origin/<trunk>`` (None: unknown, never judged)."""
    rows: list = field(default_factory=list)
    lanes: dict = field(default_factory=dict)
    occupancy: dict = None
    runs: list = field(default_factory=list)
    docs_on_trunk: dict = field(default_factory=dict)
    product: object = None


def row_key(row):
    """The one-line name of a feeder row a finding carries: ``<kind> <item> @<branch>``."""
    return f"{row.kind} {row.item_id} @{row.branch or '-'}"


def _launching(ctx):
    return [r for r in (getattr(ctx, 'rows', None) or ()) if getattr(r, 'launches', False)]


def _feeder_only(check):
    def run_it(ctx):
        if not isinstance(ctx, FeederContext):
            return []
        return check(ctx)
    run_it.__name__ = check.__name__
    run_it.__doc__ = check.__doc__
    return run_it


#: The launching row kinds each lane state allows (R10). A state not named here — PUSHED,
#: PR_OPEN, GATE, WAITING_CI, WAITING, QUEUED, MERGING — allows none: the lane holds the branch.
#: REVIEW allows its review session; BACK the corrections that answer a send-back. A terminal
#: state (MERGED, STALE, REAPED) or no lane record holds nothing.
LANE_LAUNCH_KINDS = {
    'REVIEW': ('PUSHED → REVIEW',),
    'BACK': ('FIX → CORRECT', 'STARVED → SPEC', 'STARVED → PLAN', 'STALEMATE → ADJUDICATE',
             'RESHAPE → PLAN', 'CONFLICT → REBASE', 'STALE → CLOSE'),
}
#: The occupancy fallback for a branch the lane has no record of: a busy or pushed item takes
#: only its review; an item with a pending correction takes the BACK kinds.
OCCUPANCY_LAUNCH_KINDS = {'busy': LANE_LAUNCH_KINDS['REVIEW'],
                          'waiting_landing': LANE_LAUNCH_KINDS['REVIEW'],
                          'corrections': LANE_LAUNCH_KINDS['BACK']}
_FREE_STATES = ('MERGED', 'STALE', 'REAPED')


def _allowed_kinds(ctx, row):
    """``(the kinds the branch's state allows, the state's name)``, or ``(None, None)`` when
    nothing holds the branch."""
    rec = (ctx.lanes or {}).get(row.branch) if row.branch else None
    state = (rec or {}).get('state')
    if state:
        if state in _FREE_STATES:
            return None, None
        return LANE_LAUNCH_KINDS.get(state, ()), f'lane {state}'
    occ = ctx.occupancy or {}
    for key in ('corrections', 'busy', 'waiting_landing'):
        if row.item_id in (occ.get(key) or {}):
            return OCCUPANCY_LAUNCH_KINDS[key], f'occupancy {key}'
    return None, None


def check_i4(ctx):
    """I4 (R10) — at most one launching row per branch, and its kind matches the branch's lane
    state: REVIEW takes the review session (``PUSHED → REVIEW`` is legal), BACK takes a
    correction, every other open state takes none. The first row of a branch (tier order) is
    kept; a second is dropped."""
    out, seen = [], {}
    for row in _launching(ctx):
        allowed, why = _allowed_kinds(ctx, row)
        if allowed is not None and row.kind not in allowed:
            out.append(Finding('I4', 'feeder', row_key(row),
                               f'{row.kind} launches on {row.branch} in {why} '
                               f"(allows {', '.join(allowed) or 'no launch'})"))
            continue
        if row.branch and row.branch in seen:
            out.append(Finding('I4', 'feeder', row_key(row),
                               f'a second launching row on {row.branch} (first: {seen[row.branch]})'))
            continue
        if row.branch:
            seen[row.branch] = row_key(row)
    return out


#: The rows that start code: a coder on a Feature's Task, or its correction.
CODE_BRIEF_KINDS = ('task', 'coder', 'correct', 'fixer')


def check_i5(ctx):
    """I5 — no code row without the spec (and the plan) on the trunk: a launching code row of a
    Feature's Task needs ``origin/<trunk>:<spec>`` (and ``<plan>``) to exist. A document whose
    place is unknown is not judged."""
    out = []
    for row in _launching(ctx):
        fid = getattr(row, 'feature_id', '') or ''
        if not fid or row.brief_kind not in CODE_BRIEF_KINDS:
            continue
        docs = (ctx.docs_on_trunk or {}).get(fid) or {}
        missing = [d for d in ('spec', 'plan') if docs.get(d) is False]
        if missing:
            out.append(Finding('I5', 'feeder', row_key(row),
                               f"{fid}'s {' and '.join(missing)} not on the trunk"))
    return out


def _worktree_key(run):
    if run.get('worktree_key'):
        return run['worktree_key']
    if not run.get('worktree'):
        return None
    from asf.workers.lifecycle import path_key
    return path_key(run['worktree'])


def check_i7(ctx):
    """I7 — one session per branch and per worktree, worktree paths case-folded
    (:func:`asf.workers.lifecycle.path_key`). Two live runs sharing one is reported; a launching
    row onto a branch a live run holds is dropped."""
    out = []
    by_branch, by_tree = {}, {}
    for run in ctx.runs or ():
        if run.get('branch'):
            by_branch.setdefault(run['branch'], []).append(run.get('job'))
        key = _worktree_key(run)
        if key:
            by_tree.setdefault(key, []).append(run.get('job'))
    for what, groups in (('branch', by_branch), ('worktree', by_tree)):
        for name, jobs in sorted(groups.items()):
            if len(jobs) > 1:
                out.append(Finding('I7', 'feeder', f'{what} {name}',
                                   f"{len(jobs)} live sessions: {', '.join(sorted(map(str, jobs)))}"))
    for row in _launching(ctx):
        if row.branch and row.branch in by_branch:
            out.append(Finding('I7', 'feeder', row_key(row),
                               f'{row.branch} is held by live session {by_branch[row.branch][0]}'))
    return out


# ---- the lane invariant (I8) and event (I9) -------------------------------------------------

@dataclass
class LaneContext:
    """What a ``lane``-scope check reads: ``lanes`` ``{branch: lane record}``; ``branches`` the
    lane branches the facts know (None: not gathered, not judged); ``history`` ``{branch: [lane
    records, oldest first]}``; ``now`` (epoch seconds) and ``stale_after_s``."""
    lanes: dict = field(default_factory=dict)
    branches: object = None
    history: dict = field(default_factory=dict)
    now: float = 0.0
    stale_after_s: float = 0.0
    product: object = None


def _lane_only(check):
    def run_it(ctx):
        if not isinstance(ctx, LaneContext):
            return []
        return check(ctx)
    run_it.__name__ = check.__name__
    run_it.__doc__ = check.__doc__
    return run_it


def _epoch(at):
    import datetime
    if isinstance(at, (int, float)):
        return float(at)
    try:
        return datetime.datetime.fromisoformat(str(at).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def check_i8(ctx):
    """I8 — every lane branch is in exactly one known state, and no open state is older than
    ``lane.stale_after`` without a reason: a silent wait is made visible (reported only)."""
    from asf.harvest import lane
    out = []
    for branch in sorted(set(ctx.branches or ()) - set(ctx.lanes or {})):
        out.append(Finding('I8', 'lane', branch, 'a lane branch with no lane state'))
    for branch, rec in sorted((ctx.lanes or {}).items()):
        state = (rec or {}).get('state')
        if state not in lane.LANE_STATES:
            out.append(Finding('I8', 'lane', branch, f'not a lane state: {state!r}'))
            continue
        if state in lane.TERMINAL_STATES or not ctx.stale_after_s or rec.get('reason'):
            continue
        at = _epoch(rec.get('at'))
        if at is not None and ctx.now - at > ctx.stale_after_s:
            out.append(Finding('I8', 'lane', branch,
                               f'{state} for {int((ctx.now - at) // 3600)}h with no reason '
                               f'(stale after {int(ctx.stale_after_s // 3600)}h)'))
    return out


def i9_events(ctx):
    """I9 (R12) — one controller per product, as an **event**, never a violation: a branch found
    merged outside the lane (``method=external``) with no ``MERGING`` intent of ours before it
    since its last push. A human merging a PR is legitimate; the event only says who merged.
    Returns ``[{'invariant': 'I9', 'branch', 'pr', 'message'}]``."""
    if not isinstance(ctx, LaneContext):
        return []
    out = []
    for branch, history in sorted((ctx.history or {}).items()):
        ours = False
        for rec in history or ():
            state = (rec or {}).get('state')
            if state in ('PUSHED', 'MERGING', 'QUEUED'):
                ours = state != 'PUSHED'
            if state == 'MERGED':
                method = str(rec.get('method') or rec.get('reason') or '')
                if 'external' in method and not ours:
                    out.append({'invariant': 'I9', 'branch': branch, 'pr': rec.get('pr'),
                                'message': f'{branch} merged outside the lane (no MERGING intent)'})
                ours = False
    return out


INVARIANTS.extend([
    Invariant('I4', 'feeder', _feeder_only(check_i4)),
    Invariant('I5', 'feeder', _feeder_only(check_i5)),
    Invariant('I7', 'feeder', _feeder_only(check_i7)),
    Invariant('I8', 'lane', _lane_only(check_i8)),
])


# ---- the facts, gathered read-only (the tick's check points and `asf check --invariants`) ---

def _soft(fn, default):
    """``fn()``, or ``default`` when it raises — a stub not yet built (``NotImplementedError``)
    or a fact that cannot be read is no fact, never a failed tick."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a missing fact is judged as unknown
        return default


def _sessions_path(product):
    from asf.workers import pool
    return pool.sessions_path(product)


def lane_records(product):
    """``{branch: lane record}`` — :func:`asf.harvest.lane.snapshot`, ``{}`` until the lane has
    one to give."""
    from asf.harvest import lane
    return _soft(lambda: dict(lane.snapshot(product) or {}), {})


def lane_history(path):
    """``{branch: [lane records, oldest first]}`` off the raw ``sessions.jsonl`` lines: a line's
    ``lane`` field belongs to the branch its job's launch named."""
    import json
    branch_of, out = {}, {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get('branch') and rec.get('job'):
                branch_of[rec['job']] = rec['branch']
            lane_rec = rec.get('lane')
            branch = (lane_rec or {}).get('branch') if isinstance(lane_rec, dict) else None
            branch = branch or branch_of.get(rec.get('job'))
            if isinstance(lane_rec, dict) and branch:
                out.setdefault(branch, []).append(lane_rec)
    return out


def live_runs(path):
    """The runs that hold a seat (:func:`asf.workers.lifecycle.occupies`), each with its job."""
    from asf.workers import lifecycle
    return [dict(r, job=job) for job, r in lifecycle.latest(path).items()
            if lifecycle.occupies(r)]


def _doc_path(link):
    link = str(link or '').strip()
    return link.split(':', 1)[1] if ':' in link else link


def _on_trunk_by_evidence(item, doc):
    import re
    for line in item.get('evidence') or ():
        m = re.match(rf'^{doc} on (\S+)', str(line))
        if m:
            return m.group(1).startswith('origin/')
    return None


def docs_on_trunk(product, items, feature_ids):
    """``{feature: {'spec': bool|None, 'plan': bool|None}}``: on the trunk when ingest's
    evidence line says so or the document its ``links`` name is on ``origin/<trunk>`` (one
    ``git cat-file --batch-check``); not on it when a source says so and none places it there;
    None when neither knows."""
    import subprocess
    out, asks = {}, []
    repo = getattr(product, 'repo_dir', None)
    trunk = getattr(product, 'main', None) or 'main'
    for fid in sorted(set(feature_ids)):
        item = items.get(fid) or {}
        out[fid] = {}
        for doc in ('spec', 'plan'):
            path = _doc_path((item.get('links') or {}).get(doc))
            if path and repo:
                asks.append((fid, doc, f'origin/{trunk}:{path}'))
            out[fid][doc] = _on_trunk_by_evidence(item, doc)
    if asks:
        try:
            p = subprocess.run(['git', '-C', repo, 'cat-file', '--batch-check'],
                               input=''.join(a[2] + '\n' for a in asks), capture_output=True,
                               text=True, timeout=30)
            answers = p.stdout.splitlines() if p.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired):
            answers = []
        if len(answers) == len(asks):
            for (fid, doc, _ref), ans in zip(asks, answers):
                # either source placing it on the trunk is enough: a false "missing" would
                # drop a real coder row
                out[fid][doc] = out[fid][doc] or not ans.endswith(' missing')
    return out


def feeder_context(product, rows, items):
    """The :class:`FeederContext` for ``rows``, every fact read-only and soft."""
    from asf.workers import lifecycle
    path = _sessions_path(product)
    code = {r.feature_id for r in rows if getattr(r, 'launches', False) and r.feature_id
            and r.brief_kind in CODE_BRIEF_KINDS}
    return FeederContext(
        rows=list(rows), lanes=lane_records(product),
        occupancy=_soft(lambda: lifecycle.occupancy(path, lanes=None), None),
        runs=_soft(lambda: live_runs(path), []),
        docs_on_trunk=_soft(lambda: docs_on_trunk(product, items, code), {}),
        product=product)


def feeder_gate(product, rows, items, out=print):
    """The feeder check point: the ``feeder`` invariants over the planned ``rows``; a violating
    row is dropped and logged ``INVARIANT <id>: <row> — <why>``; a finding about two live
    sessions (no row to drop) is logged too. Returns the rows kept. Never raises: a failure to
    check keeps every row (R9)."""
    try:
        ctx = feeder_context(product, rows, items)
        findings = run(ctx, scope='feeder', out=out)
    except Exception as e:  # noqa: BLE001 — the feeder check never stops the wave
        out(f'INVARIANT feeder: not checked ({type(e).__name__}: {e})')
        return list(rows)
    drop = {}
    for f in findings:
        out(f'INVARIANT {f.invariant}: {f.subject} — {f.message}')
        drop.setdefault(f.subject, f)
    return [r for r in rows if not (getattr(r, 'launches', False) and row_key(r) in drop)]


def lane_context(product, now=None):
    import time
    conv = getattr(product, 'conventions', None)
    stale = _soft(lambda: conv.lane_stale_after_s(), 0) if conv is not None else 0
    return LaneContext(lanes=lane_records(product),
                       history=_soft(lambda: lane_history(_sessions_path(product)), {}),
                       now=time.time() if now is None else now, stale_after_s=stale or 0,
                       product=product)


def lane_report(product, out=print, event=None, now=None):
    """The lane check point, after harvest: ``INVARIANT I8: <branch> — <why>`` per finding and
    ``EVENT I9: <message>`` per merge from outside the lane (and ``event('foreign-merge', …)``
    when given). Report only: returns ``(findings, events)``, never raises."""
    try:
        ctx = lane_context(product, now)
        findings = run(ctx, scope='lane', out=out)
        events = i9_events(ctx)
    except Exception as e:  # noqa: BLE001 — a lane report never stops the tick
        out(f'INVARIANT lane: not checked ({type(e).__name__}: {e})')
        return [], []
    for f in findings:
        out(f'INVARIANT {f.invariant}: {f.subject} — {f.message}')
    for ev in events:
        out(f"EVENT I9: {ev['message']}")
        if event is not None:
            _soft(lambda ev=ev: event('foreign-merge', branch=ev['branch'], pr=ev['pr']), None)
    return findings, events


def record_audit(root, product=None):
    """The ``record`` invariants over the whole record, read-only (``asf check --invariants``):
    every card judged as if one writer had just written it, so a violation already standing is
    listed (I1, which compares a writer's before and after, has nothing to compare)."""
    from asf.record import stage
    from asf.record.core import ITEM_FOLDERS
    paths = []
    for folder in ITEM_FOLDERS:
        d = os.path.join(root, folder)
        if os.path.isdir(d):
            paths += [f'{folder}/{n}' for n in sorted(os.listdir(d)) if n.endswith('.md')]
    staged = stage.Staged('check', tuple(paths), {p: None for p in paths})
    return run(stage.RecordContext(root, staged, product), scope='record')


# ---- the tests-only invariants (I6, I12, I13): never a tick check (R13) --------------------

def _derived_fields(root):
    """``{id: ({state, stage, evidence}, relpath)}`` of every card under ``root``."""
    from asf.record import frontmatter
    from asf.record.core import canonicalize, load_items
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    out = {}
    for iid, rec in canonical.items():
        machine = frontmatter.split_machine(rec['meta'])[1]
        out[iid] = ({k: machine.get(k) for k in ('state', 'stage', 'evidence')}, rec['relpath'])
    return out


def _default_ingest(root):
    import argparse
    from asf.record.ingest import cmd_ingest
    cmd_ingest(argparse.Namespace(fresh=True, product=None), root)


def check_i6(root, ingest=None):
    """I6 — every derived field can be re-derived: ingesting a copy of the record twice is a
    no-op, and stripping every card's machine block and ingesting again gives the same
    ``state``/``stage``/``evidence`` (``updated``/``stage_since`` aside). ``ingest(root)`` is
    the ingest to run (default: ``asf ingest`` for the resolved product). Too slow for a tick:
    ``asf check --invariants --deep`` and the tests only. Never writes ``root``."""
    import shutil
    import tempfile
    from asf.record import frontmatter
    ingest = ingest or _default_ingest
    tmp = tempfile.mkdtemp(prefix='asf-i6-')
    copy = os.path.join(tmp, 'record')
    try:
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns('.git'))
        ingest(copy)
        first = _derived_fields(copy)
        ingest(copy)
        second = _derived_fields(copy)
        found = [Finding('I6', 'record', iid, 'a second ingest changed it', (first[iid][1],))
                 for iid in sorted(first) if first[iid][0] != second.get(iid, (None,))[0]]
        for _iid, (_d, rel) in first.items():
            path = os.path.join(copy, rel)
            with open(path, encoding='utf-8') as f:
                meta, body = frontmatter.parse(f.read(), path=rel)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(frontmatter.render(frontmatter.split_machine(meta)[0], body))
        ingest(copy)
        third = _derived_fields(copy)
        named = {f.subject for f in found}
        found += [Finding('I6', 'record', iid, 'stripped and re-ingested, it derives differently',
                          (first[iid][1],))
                  for iid in sorted(first)
                  if iid not in named and first[iid][0] != third.get(iid, (None,))[0]]
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: A release note's sections that say "landed": the notes' own headings and the note's type
#: headings (:func:`asf.metrics.metrics.render_notes`, :func:`render_release`).
LANDED_HEADINGS = ('### Features landed', '### Bugs fixed', '## Epics', '## Features',
                   '## User Stories', '## Tasks', '## Bugs')
DONE_STATES = ('Resolved', 'Closed')


def check_i12(text, items):
    """I12 — a release note lists under "landed" only items whose derived state is Resolved or
    Closed. ``items`` is the index's ``{id: item}``. A test, never a tick check."""
    import re
    out, current = [], None
    for line in (text or '').splitlines():
        if line.startswith('#'):
            current = line.strip() if line.strip() in LANDED_HEADINGS else None
            continue
        m = re.match(r'^- \[?([A-Z]-\d{4})\b', line) if current else None
        if m and (items.get(m.group(1)) or {}).get('state') not in DONE_STATES:
            state = (items.get(m.group(1)) or {}).get('state', 'unknown')
            out.append(Finding('I12', 'record', m.group(1), f'listed under {current!r} while {state}'))
    return out


def check_i13(declared, minted):
    """I13 — an explicit ``type:`` line in an inbox card decides the minted type. A test."""
    if declared and declared != minted:
        return [Finding('I13', 'record', str(minted), f'declared type {declared}, minted {minted}')]
    return []


#: The invariants the tick never runs (R13): I6 daily/deep only, I12 and I13 in the tests.
TEST_ONLY = {'I6': check_i6, 'I12': check_i12, 'I13': check_i13}
