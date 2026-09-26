"""asf.tick.step_wave — the tick's ``wave`` step: what the feeder says to start, launched.

0. ``approvals.raise_holds`` — the open holds said aloud, and the items they park (§2.4): a
   harvest merge hold parks its item, and so does a refused write to the amendable set; any other
   refusal from the hook never does — the item relaunches and its brief names the refusal
   (:func:`asf.approvals.refusal_text`). A hold on an item the record calls Resolved/Closed is
   closed ``done`` first (:func:`asf.approvals.close_landed`);
1. the index from the record clone (the tick's own, made once per tick — :class:`Context`);
2. the lane pass (:func:`asf.harvest.lane.lane_pass`, R2): every lane branch moved as far as its
   facts carry it — a finished branch's PR opened or adopted, its review asked for, a merge seen
   — before the feeder reads the lane. Its ref pushes (a landed branch's delete, a superseded
   one's archive) run the product's pre-push hook, so they wait until after step 5's launches
   (:func:`push_deferred`), each logged ``lane: push <branch> <s>s (<kind>)``; an archive's branch keeps its
   state until its push, as a refused push leaves it; then ``inflight`` (the sessions in
   ``~/.ASF/state/<product>/sessions.jsonl`` with no ``ended``) and the one occupancy answer
   (:func:`asf.workers.lifecycle.occupancy`): what a live run holds, what waits to land, the
   lane's states and the pending corrections;
3. ``feeder.plan_rows(index, product, inflight, capacity)`` — ``capacity`` is the resolver's
   ceiling (``asf.capacity.resolve``, spec §2.2's session law, bounded by the product's fair
   share of the usable pool); the feeder still takes this product's in-flight sessions off it
   itself (P5). A row on an item a hold parks is planned but takes no slot (it waits for a
   person). The feeder's invariant gate runs on the cut (:func:`gated_plan`): a row it drops
   gives its slot to the next candidate. Each launching row the fair share cut prints ``waits …
   — fair share: <n> of <usable> usable slots across <k> products; in flight <i>: <jobs>; this
   wave <w>: <jobs>`` — what the share was spent on; the wave records this product's demand
   (:func:`asf.capacity.write_demand`) so an idle partner's share can be lent to it;
4. per launching row, a brief (``asf.briefs.build``) with the facts of its branch on the product
   repo's origin — whether it is pushed and its last commit, two ``git`` calls at most;
5. one ``workers.wave`` over every briefed row, so the pool's S1 reserve sees them all; it prints
   the ``launched`` / ``waits`` lines (``reserved for S1`` among them). A row the feeder holds
   back (``WAITS ON …``, no slot) prints its own ``waits`` line here, as does one an approval
   class holds.

Before any brief is built, the host-pressure guard (:mod:`asf.workers.host`, ``config.yaml
host_guards``): a host at or over its load or swap guard starts no session this tick — each
launching row prints ``waits … — held: host pressure load <n>/cores <c>, swap <p>%`` and the step
ends on ``wave: held: …``. Sessions already running are never touched. With the cloud lane on
(:mod:`asf.workers.cloud`) the held host only holds the local lane: the step prints ``wave: local
lane held: …`` and the wave sends what the cloud lane can take there; its seats are added to the
feeder's ceiling.

Whatever the plan holds, the wave starts at most ``max(0, share − live)`` rows, ``live`` being
:func:`asf.capacity.live_sessions` — the count the status Capacity row shows — so a stale plan or
a row the feeder mis-cut can never overshoot the share; a row past it prints ``waits … — no seat
left: share <n>, in flight <i>, this wave <w>``. The S1 bypass below is the one row that may pass
it, and its session counts toward ``live`` on every later wave.

An S1 item's row passes the LOAD half of that guard (:func:`asf.workers.host.load_only_hold`):
"S1 first" must hold even under the load other sessions created. It never passes a host over its
memory/swap guard — that holds every row, S1 included — and at most one such bypass may be live
at a time, across every product (:func:`s1_bypass_live`, the session ledger the wave already
reads): a second S1 row waits like any other while one is still running. The launch that takes it
prints ``(S1: passes host load hold)`` on its ``launched`` line.

Each launch appends a ``launch`` event (item, job, model, brief kind) to ``metrics/events``.
"""
import importlib
import inspect
import os
import re
import subprocess

from asf import approvals, env
from asf import capacity as capacity_mod
from asf.groom import policy as groom_policy
from asf.record import plan_order
from asf.workers import cloud as cloud_mod
from asf.workers import host as host_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

#: PD9 — for a kind whose job name is not ``<brief kind>-<item id>``, the Row attribute that
#: carries the job's key instead (the groom brief's job is ``groom-<date>``, D7).
KIND_JOB_KEY = {'groom': 'groom_date'}
_GROOM_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.md$')

#: the session ledger field a launch that passed the S1 load-hold bypass carries, so a later
#: wave (this product's or another's — :func:`s1_bypass_live`) can see one is still live.
HOST_LOAD_BYPASS_FIELD = 'host_load_bypass'


def capacity(product=None):
    return capacity_mod.resolve(product or env.load_product()).sessions


def inflight(product):
    """The feeder's ``inflight`` list off the session ledger — :func:`asf.capacity.live_sessions`,
    the one count the status Capacity row reads too."""
    return capacity_mod.live_sessions(product)


def attempts(product):
    """``{item: runs the ledger holds for it}`` — :func:`asf.workers.lifecycle.attempts`."""
    return lifecycle.attempts(pool_mod.sessions_path(product))


def occupancy(product):
    """The one answer to "is this item busy?" (:func:`asf.workers.lifecycle.occupancy`): live
    runs, work waiting to land, the lane's states (off the run lines), pending corrections."""
    return lifecycle.occupancy(pool_mod.sessions_path(product))


def corrections(product):
    """``{item: {kind, text, at, rounds}}`` — the newest correction still waiting for its session
    (:func:`asf.workers.lifecycle.corrections`; also ``occupancy(product)['corrections']``)."""
    return lifecycle.corrections(pool_mod.sessions_path(product))


def lane_pass(ctx, out=print, defer_pushes=False):
    """R2: the lane's feeder-visible transitions, in-process, before the wave reads the lane —
    once per tick (the ``prs`` step skips it when the wave ran it). A failure is one line: the
    wave still plans off the lane as it stood. ``defer_pushes`` (the wave's own call): the pass's
    ref pushes wait on ``ctx.lane`` for :func:`push_deferred`, after the launches."""
    from asf.harvest import harvest, lane
    from asf.tick import land_spec
    from asf.views import index_reader
    product = ctx.product
    if not product.repo_dir or getattr(ctx, 'lane_passed', False):
        return {}
    ctx.lane_passed = True
    try:
        root = ctx.record_root()
        if os.path.isfile(os.path.join(root, 'index.json')):  # an approved spec off the trunk
            land_spec.adopt(product, index_reader.load(root)[0], out=out)
        ctx.lane = lane.Lane(product, None, out, False, harvest.record_items(root), root)
        results, _found = lane.lane_pass(product, out=out, lane=ctx.lane,
                                          defer_pushes=defer_pushes)
        return results
    except Exception as e:  # noqa: BLE001 — the wave must still run
        out(f'lane: pass failed — {(str(e) or type(e).__name__).splitlines()[0]}')
        return {}


def _newest_groom_file(root):
    d = os.path.join(root, 'groom')
    if not os.path.isdir(d):
        return None, None
    dates = [m.group(1) for name in os.listdir(d)
             for m in [_GROOM_FILE_RE.match(name)] if m]
    if not dates:
        return None, None
    date = sorted(dates)[-1]
    return date, os.path.join(d, f'{date}.md')


def groom_state(product, root):
    """§2.5's fact the feeder cannot derive from ``index.json`` (P5): the newest groom day's
    still-open questions, and how many ``groom-<date>`` sessions the ledger already holds for
    it. ``None`` when no groom file exists yet."""
    date, path = _newest_groom_file(root)
    if not date:
        return None
    with open(path, encoding='utf-8') as f:
        text = f.read()
    # the file may predate the index (answers applied since): what the factory already acts on
    # is nobody's question — the same suppression the groom ran when it wrote the file
    from asf.views import index_reader
    items = index_reader.load(root)[0] if os.path.isfile(os.path.join(root, 'index.json')) else {}
    sections, _n = groom_policy.suppress({'open': text.splitlines()}, items, inflight(product),
                                         product)
    pairs = groom_policy.open_questions('\n'.join(sections['open']))
    # the approval bound, applied again here: a file written without the policy pass (by hand,
    # or before the gate was on) still carries questions the operator owns
    owned = _operator_owned(product, items)
    pairs = [(iid, line) for iid, line in pairs if iid not in owned]
    job = f'groom-{date}'
    # sessions, not ledger lines: a run's end and harvest lines are no second attempt
    attempts = sum(1 for rec in lifecycle.read_lines(pool_mod.sessions_path(product))
                   if rec.get('job') == job and lifecycle.is_launch(rec))
    open_ids = [iid for iid, _line in pairs]
    return {'date': date, 'file': path,
            'answers': os.path.join(env.state_dir(product), 'groom', f'{date}.answers'),
            'open': open_ids, 'lines': [line for _iid, line in pairs],
            'oldest': next((iid for iid, _l in pairs if not iid.startswith('inbox:')),
                           pairs[0][0] if pairs else None),
            'attempts': attempts,
            'new': _not_yet_put(product, job, open_ids) if attempts else list(open_ids)}


def _operator_owned(product, items):
    """The item ids whose question no adjudicate session may rule (§2.8): an answer that would
    cross an action class the product does not map to ``auto`` (:func:`asf.groom.policy.barred`
    — a new Epic), or a card holding an open harvest hold (a merge class) at a level other than
    ``auto``. Those stay with the operator. A refusal from the hook
    (:func:`asf.approvals.session_refusal`) keeps nothing from the adjudicator: it rules on the
    card, and only money, credentials or an irreversible action may go to NEEDS OPERATOR."""
    probe = groom_policy.Answer('yes', 'decided', True, '')
    owned = {iid for iid, item in items.items()
             if groom_policy.barred(probe, {'meta': item}, product)}
    try:
        holds = approvals.open_holds(product)
    except (OSError, ValueError):
        holds = []
    for h in holds:
        cls = h.get('class')
        level = (approvals.level_of(product, cls) if cls in approvals.CLASSES_BY_NAME
                 else h.get('level'))
        if level != 'auto' and not approvals.session_refusal(cls):
            owned.add(h.get('item'))
    return owned


def _not_yet_put(product, job, open_ids):
    """The open questions the day's last adjudicate session was not given — asked since its
    brief was written. Its brief (``briefs/<job>.md``, rewritten per launch) lists the lines it
    was handed; no brief to read, and none counts as new."""
    path = os.path.join(env.state_dir(product), 'briefs', f'{job}.md')
    try:
        with open(path, encoding='utf-8') as f:
            given = {iid for iid, _l in groom_policy.open_questions(f.read())}
    except OSError:
        return []
    return [iid for iid in open_ids if iid not in given]


def stale_briefs(product, root, index):
    """``{item: the brief kind to re-run}`` — the latest *ended* run of each item whose recorded
    ``card_digest`` differs from :func:`asf.briefs.build.card_digest` computed now (F-0090 D5:
    the kind is that run's own). A run with no ``card_digest`` — every run launched before the
    field existed — claims nothing and is never stale (D4)."""
    digest = importlib.import_module('asf.briefs.build').card_digest
    latest = {}
    for rs in lifecycle.runs(pool_mod.sessions_path(product)).values():
        for run in rs:
            if run.get('item') and run.get('ended'):
                held = latest.get(run['item'])
                if held is None or (run.get('started') or '') >= (held.get('started') or ''):
                    latest[run['item']] = run
    return {item: run.get('kind') for item, run in latest.items()
            if run.get('card_digest') and run.get('kind')
            and run['card_digest'] != digest(product, item, index)}


def _triage_facts(product, root, index):
    """``stale_briefs`` and ``rounds`` for ``plan_rows`` — but only for a feeder that takes them
    (F-0090 Task 1) and a ledger that can say them (Task 2): until both have landed the keys are
    left out rather than break every planner."""
    if not _triage_wanted():
        return {}
    return {'stale_briefs': stale_briefs(product, root, index),
            'rounds': lifecycle.round_log(pool_mod.sessions_path(product))}


def _triage_wanted():
    """Both halves of F-0090's triage are in: the feeder takes ``stale_briefs`` and ``rounds``,
    and the ledger can say the rounds."""
    from asf.feeder import rows as feeder_rows
    takes = inspect.signature(feeder_rows.plan_rows).parameters
    return ('stale_briefs' in takes and 'rounds' in takes
            and getattr(lifecycle, 'round_log', None) is not None)


def plan_inputs(product, root, index=None):
    """The ledger's and the record's facts ``plan_rows`` takes beside the index — one place, so
    the tick, ``asf next`` and the status cell plan the same rows. ``index`` is the loaded
    ``index.json``, read from ``root`` when the caller has none — and read through the same
    ``after:`` overlay the wave applies, because ``after`` is a ``DIGEST_FIELDS`` name: a digest
    taken off un-overlaid items differs from the one the tick recorded at launch, and ``asf next``
    and the status cell would call stale every Task whose order the plan derives (D3)."""
    triage = {}
    if _triage_wanted():
        if index is None:
            from asf.views import index_reader
            index = (index_reader.load(root)[0]
                     if os.path.isfile(os.path.join(root, 'index.json')) else {})
            if index and product.repo_dir:
                index = plan_order.overlay(index, plan_order.trunk_reader(product))
        triage = _triage_facts(product, root, index)
    return {'attempts': attempts(product), 'occupancy': occupancy(product),
            'groom_state': groom_state(product, root) if groom_policy.groom_auto(product) else None,
            'held': set(approvals.parked(product)),
            **triage}


def held_by_share(items, product, running, resolved, planned, inputs, wider=None):
    """The launching rows the fair share cut: planned at the ceiling the share lowered, and not
    in ``planned``. Empty when no share bounds this product. ``wider``: that plan, when the
    caller already made it."""
    from asf.feeder import rows as feeder_rows
    if not resolved.fair_share_reason or resolved.ceiling is None:
        return []
    seen = {(r.item_id, r.kind) for r in planned}
    if wider is None:
        wider = feeder_rows.plan_rows(items, product, running, resolved.ceiling, **inputs)
    return [r for r in wider if r.launches and (r.item_id, r.kind) not in seen]


REPLANS = 8   # the gate's findings shrink the candidates each pass; this bounds a pathological loop


def gated_plan(items, product, running, capacity, inputs, out=print, exclude=None):
    """``(rows, exclude)``: the plan at ``capacity`` through the feeder's invariant gate
    (:func:`asf.invariants.feeder_gate`), a dropped row's slot handed to the next candidate.

    2026-09-25 (a product tick): the cut gave 4 of 6 free slots to rows the gate then dropped, and
    those slots went nowhere — 2 launches under a share of 7. So a launching row the gate drops
    is excluded (``plan_rows(exclude=…)``) and the plan made again, until the gate drops
    nothing. ``exclude``: rows already dropped (grown in place, and returned). Each gate line is
    said once however many passes it takes."""
    from asf import invariants
    from asf.feeder import rows as feeder_rows
    exclude = set() if exclude is None else exclude
    said = set()

    def say(line):
        if line not in said:
            said.add(line)
            out(line)
    kept = []
    for _ in range(REPLANS):
        kw = dict(inputs, exclude=frozenset(exclude)) if exclude else inputs
        planned = feeder_rows.plan_rows(items, product, running, capacity, **kw)
        kept = invariants.feeder_gate(product, planned, items, out=say)
        dropped = ({invariants.row_key(r) for r in planned if r.launches}
                   - {invariants.row_key(r) for r in kept if r.launches})
        if not dropped - exclude:
            break
        exclude |= dropped
    return kept, exclude


def row_job(row):
    """The job a feeder row launches as (:func:`job_name`, its PD9 key when its kind has one)."""
    attr = KIND_JOB_KEY.get(row.brief_kind)
    return job_name(row.brief_kind, row.item_id, key=getattr(row, attr, None) if attr else None)


def share_counted(running, planned, held=()):
    """What the fair share counted against this product, for its waits line: ``in flight <n>:
    <jobs>; this wave <m>: <jobs>`` — the live sessions and the rows this wave gives a slot."""
    held = set(held or ())
    live = [s.get('job') or s.get('item') or '?' for s in running or ()]
    wave = [row_job(r) for r in planned if r.launches and r.item_id not in held]
    return (f"in flight {len(live)}: {', '.join(live) or '-'}; "
            f"this wave {len(wave)}: {', '.join(wave) or '-'}")


def wanted(rows, held=()):
    """The launching rows a plan would start given room — a parked item's row is not one."""
    held = set(held or ())
    return sum(1 for r in rows if r.launches and r.item_id not in held)


def _git(repo, args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def repo_facts(product, branch):
    """``{branch, pushed, remote_sha, last_commit}`` for ``branch`` on the product repo's origin."""
    repo = product.repo_dir
    if not repo or not branch:
        return {'branch': branch, 'pushed': False, 'remote_sha': '', 'last_commit': ''}
    heads = _git(repo, ['ls-remote', '--heads', 'origin', branch])
    sha = heads.split()[0] if heads else ''
    last = _git(repo, ['log', '-1', '--format=%H %cI %s', f'origin/{branch}']) if sha else ''
    return {'branch': branch, 'pushed': bool(sha), 'remote_sha': sha, 'last_commit': last}


def _build(*a, **kw):
    from asf import briefs
    return briefs.build(*a, **kw)


def _wave(*a, **kw):
    from asf.workers import wave
    return wave.wave(*a, **kw)


def job_name(brief_kind, item_id, key=None):
    """``<brief kind>-<item id>``, lowercased — unless ``key`` is given (PD9: a kind whose job
    must stay stable while ``item_id`` drifts, e.g. the groom day's oldest question), in which
    case the job carries ``key`` instead."""
    return f'{brief_kind}-{item_id if key is None else key}'.lower()


def worker_row(row, brief, items, host_load_bypass=False):
    """The workers' row for a feeder row and its brief. ``host_load_bypass``: this row's launch
    is the S1 one passing the host guard's LOAD hold (:func:`s1_bypass_live`) — carried onto the
    session ledger (:func:`asf.workers.spawn.spawn`) so a later wave can see it is still live."""
    item = items.get(row.item_id) or {}
    state, _, action = row.kind.partition(' → ')
    attr = KIND_JOB_KEY.get(brief.kind)
    key = getattr(row, attr) if attr else None
    return pool_mod.Row(job_name(brief.kind, row.item_id, key=key), row.item_id, state=state,
                        action=action, title=item.get('title', ''), model=brief.model,
                        kind=brief.kind, severity=item.get('severity'),
                        feature=row.feature_id or None, branch=row.branch or None,
                        add_dirs=getattr(brief, 'add_dirs', None) or (),
                        card_digest=getattr(brief, 'card_digest', '') or '',
                        host_load_bypass=host_load_bypass)


def host_hold(planned):
    """``(held, why, reading)`` of the host-pressure guard (:func:`asf.workers.host.pressure`,
    ``config.yaml host_guards``) — read only when a row would launch; the tick and ``asf tick
    --dry-run`` hold on the same answer."""
    if not any(row.launches for row in planned):
        return False, '', {}
    return host_mod.pressure(env.load_config())


def s1_bypass_live():
    """Whether an S1 session that already passed the load-hold bypass is still live, anywhere
    across every product (:func:`asf.workers.lifecycle.live_all` — the same cross-product ledger
    read the pool sums load over, F-0076): at most one such bypass runs at a time, so a second S1
    row waits behind it like any other row under load pressure."""
    root = os.path.join(env.ASF_HOME, 'state')
    return any(r.get(HOST_LOAD_BYPASS_FIELD) for r in lifecycle.live_all(root))


def cloud_settings(product):
    """The cloud lane's settings (:func:`asf.workers.cloud.settings`); an unreadable config is
    a lane that is off."""
    try:
        cfg = env.load_config()
    except env.ConfigError:
        cfg = {}
    return cloud_mod.settings(cfg, product)


def push_deferred(ctx, out=print):
    """The lane pass's ref pushes (branch deletes, archives), after the wave's launches: each
    push runs the product's pre-push hook, and no launch waits on one. A failure is one line."""
    from asf.harvest import lane
    ln = getattr(ctx, 'lane', None)
    if ln is None or not ln.deferred:
        return
    try:
        lane.push_deferred(ln)
    except Exception as e:  # noqa: BLE001 — the step's own outcome stands
        out(f'lane: deferred pushes failed — {(str(e) or type(e).__name__).splitlines()[0]}')


def run(ctx, out=print):
    """The wave: plan and launch (:func:`launch`), then the lane pass's deferred pushes."""
    try:
        return launch(ctx, out)
    finally:
        push_deferred(ctx, out)


def launch(ctx, out=print):
    from asf.feeder import rows as feeder_rows
    from asf.views import index_reader
    product = ctx.product
    held = approvals.raise_holds(ctx, out)
    lane_pass(ctx, out, defer_pushes=True)
    items, _generated = index_reader.load(ctx.record_root())
    if product.repo_dir:  # defence in depth: a Task whose card lacks `after:` waits on its plan's order
        items = plan_order.overlay(items, plan_order.trunk_reader(product))
    running = inflight(product)
    r = capacity_mod.resolve(product)
    # the cloud lane's seats beside the local ones: the feeder takes every in-flight run (cloud
    # ones too) off the sum, so what is left is the free local seats plus the free cloud ones
    cloud = cloud_settings(product)
    extra = cloud.max_inflight if cloud.on else 0
    inputs = plan_inputs(product, ctx.record_root(), items)
    # the feeder check point: a violating row is dropped, logged, and its slot goes to the next
    planned, dropped = gated_plan(items, product, running, r.sessions + extra, inputs, out=out)
    wider = (gated_plan(items, product, running, r.ceiling + extra, inputs, out=lambda _l: None,
                        exclude=set(dropped))[0]
             if r.ceiling is not None and r.ceiling != r.sessions else planned)
    capacity_mod.write_demand(product.name, len(running), wanted(wider, inputs.get('held')))
    share_held = held_by_share(items, product, running, r, planned, inputs, wider=wider)
    counted = share_counted(running, planned, inputs.get('held')) if share_held else ''
    for row in share_held:
        job = job_name(row.brief_kind, row.item_id)
        out(f'waits    {job:<24} {row.item_id:<10} — {r.fair_share_reason}; {counted}')
    host_held, host_why, reading = host_hold(planned)
    # a loaded host still starts cloud sessions: nothing of theirs runs here
    local_hold = host_why if host_held and cloud.on else ''
    if local_hold:
        host_held = False
    # an S1 item's row passes the LOAD half of the guard — never memory/swap pressure, and at
    # most one such bypass live at a time, across every product (asf.workers.host.load_only_hold)
    s1_bypass_open = (host_held
                      and host_mod.load_only_hold(reading, host_mod.guards_from_config(env.load_config()))
                      and not s1_bypass_live())
    bypassed = False
    # the hard cap: whatever the plan holds, this wave starts at most share - live, live being the
    # one count the Capacity row shows (capacity_mod.live_sessions) — the S1 bypass the only row
    # that may pass it, and its session counts toward live on every later wave
    seats = r.sessions + extra
    room = max(0, seats - len(running))
    worker_rows, texts, kinds = [], {}, {}
    for row in planned:
        cause = getattr(row, 'cause', '')
        if cause:                               # §2.5: a cheap cause is said, held or re-run
            ctx.event('triage', item=row.item_id, cause=cause, row=row.kind, action=row.action)
            out(f'triage   {row.item_id:<10} — {cause}: {row.reason}')
        if not row.launches:
            out(f"waits    {'-':<24} {row.item_id:<10} — {row.action}")
            continue
        if row.item_id in held:                 # §2.4: a harvest merge hold parks its item
            cls, level = held[row.item_id]
            job = job_name(row.brief_kind, row.item_id)
            out(f'waits    {job:<24} {row.item_id:<10} — held {cls} ({level})')
            continue
        bypass = s1_bypass_open and (items.get(row.item_id) or {}).get('severity') == 'S1'
        if host_held and not bypass:             # a loaded host takes no new session this tick
            job = job_name(row.brief_kind, row.item_id)
            out(f'waits    {job:<24} {row.item_id:<10} — held: {host_why}')
            continue
        if room <= 0 and not bypass:
            job = job_name(row.brief_kind, row.item_id)
            out(f'waits    {job:<24} {row.item_id:<10} — no seat left: share {seats}, '
                f'in flight {len(running)}, this wave {len(worker_rows)}')
            continue
        room -= 1
        if bypass:
            s1_bypass_open = False              # one bypass at a time, across the whole wave
            bypassed = True
        if row.brief_kind == 'adjudicate' and getattr(row, 'between', ()):
            (la, ta), (lb, tb) = row.between
            pair = getattr(row, 'common', '')
            common = f' · both touch {pair}' if pair else ' · no file in common'
            job = job_name(row.brief_kind, row.item_id)
            out(f'adjudicate {job:<24} {row.item_id:<10} — {la}: {ta[:60]} ↔ {lb}: {tb[:60]}{common}')
        brief = _build(product, row, items, running,
                       repo_facts=repo_facts(product, row.branch))
        wrow = worker_row(row, brief, items, host_load_bypass=bypass)
        worker_rows.append(wrow)
        texts[wrow.job] = brief.text
        kinds[wrow.job] = brief.kind
    ctx.event('capacity', sessions=r.sessions, sessions_inflight=len(running),
              sessions_bound_by=r.sessions_bound, fair_share=r.fair_share, usable=r.usable,
              borrowed=r.borrowed,
              active_products=r.active, ci=r.ci, ci_inflight=r.ci_inflight,
              ci_bound_by=r.ci_bound)
    if host_held:
        ctx.event('host_pressure', load15=reading.get('load15'), cores=reading.get('cores'),
                  swap_pct=reading.get('swap_pct'))
        if not bypassed:
            out(f'wave: held: {host_why} — no new session this tick; running sessions go on')
            return 0
        out(f'wave: held: {host_why} — an S1 row passes the load hold; the rest wait')
    if local_hold:
        ctx.event('host_pressure', load15=reading.get('load15'), cores=reading.get('cores'),
                  swap_pct=reading.get('swap_pct'))
        out(f'wave: local lane held: {local_hold} — the cloud lane takes what it can')
    if not worker_rows:
        out('wave: nothing to launch')
        return 0
    launched, _waits = _wave(product, worker_rows, len(worker_rows),
                             brief_fn=lambda r: texts[r.job], out=out,
                             **({'local_hold': local_hold} if local_hold else {}))
    for wrow, rec in launched:
        ctx.event('launch', item=wrow.item, job=wrow.job,
                  model=rec.get('model'), brief_kind=kinds[wrow.job])
    ctx.counts['launches'] += len(launched)
    return 0
