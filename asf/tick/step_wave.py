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
   (:func:`demand` — its ready rows, never cut by pressure, room or share;
   :func:`asf.capacity.write_demand`) so an idle partner's share can be lent to it;
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
feeder's ceiling. The lane counts as on only while it is ready (:func:`cloud_readiness`, read
once per tick — :func:`asf.workers.cloud.readiness`): an unready lane adds no seat, lifts no
hold, and the wave prints ``cloud lane unready: <why> — local lane only`` (:func:`split_hold`).

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
import copy
import importlib
import inspect
import os
import re
import subprocess

from asf import approvals, budget, env
from asf import tune as tune_mod
from asf import capacity as capacity_mod
from asf.groom import policy as groom_policy
from asf.record import plan_order
from asf.workers import cloud as cloud_mod
from asf.workers import host as host_mod
from asf.workers import lifecycle
from asf.workers import loops
from asf.workers import pool as pool_mod
from asf.workers import relaunch
from asf.workers import landing
from asf.workers import trunkclose

#: PD9 — for a kind whose job name is not ``<brief kind>-<item id>``, the Row attribute that
#: carries the job's key instead (the groom brief's job is ``groom-<date>``, D7).
KIND_JOB_KEY = {'groom': 'groom_date', 'groom-clerk': 'groom_date'}
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
    runs, work waiting to land, the lane's states (off the run lines), pending corrections — a
    lane record naming a PR the evidence pass saw merged or closed held over by nothing
    (:func:`asf.workers.lifecycle.ended_prs`)."""
    path = pool_mod.sessions_path(product)
    return lifecycle.occupancy(path, ended=lifecycle.ended_prs(os.path.dirname(path)),
                               on_origin=lambda branches: on_origin(product, branches))


def on_origin(product, branches):
    """The ``branches`` origin has a head for — one ``git ls-remote --heads`` for exactly
    those refs — or None when it cannot be asked (no repo, a git error): unknown is never
    "not pushed"."""
    from asf import gitops
    repo = getattr(product, 'repo_dir', None)
    if not branches:
        return set()
    if not repo or not os.path.isdir(repo):
        return None
    p = gitops.git(['ls-remote', '--heads', 'origin', *[gitops.head_ref(b) for b in branches]],
                   repo, timeout=30)
    if not p.ok:
        return None
    return {b for b in branches if gitops.head_sha(p.stdout, b)}


def corrections(product):
    """``{item: {kind, text, at, rounds}}`` — the newest correction still waiting for its session
    (:func:`asf.workers.lifecycle.corrections`; also ``occupancy(product)['corrections']``)."""
    return lifecycle.corrections(pool_mod.sessions_path(product))


def lane_pass(ctx, out=print, defer_pushes=False, dry_run=False):
    """R2: the lane's feeder-visible transitions, in-process, before the wave reads the lane —
    once per tick (the ``prs`` step skips it when the wave ran it). A failure is one line: the
    wave still plans off the lane as it stood. ``defer_pushes`` (the wave's own call): the pass's
    ref pushes wait on ``ctx.lane`` for :func:`push_deferred`, after the launches. ``dry_run``
    (:mod:`asf.tick.dry_run`'s own call): threaded onto the :class:`~asf.harvest.lane.Lane` this
    pass builds — every push, archive, delete, PR open/close/merge and the ``ci_queue`` pass
    :func:`asf.harvest.lane.lane_pass` makes at its end become one ``DRY``/``would …`` line
    instead (2026-09-29: this call hardcoded ``dry_run=False`` regardless of the caller's own —
    ``asf tick --dry-run`` deleted two already-landed branches pending their delete and the
    ci-queue step cancelled four queued runs, both through this one pass)."""
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
        ctx.lane = lane.Lane(product, None, out, dry_run, harvest.record_items(root), root)
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
    job, clerk_job = f'groom-{date}', f'groom-clerk-{date}'
    # sessions, not ledger lines: a run's end and harvest lines are no second attempt
    launches = [rec.get('job') for rec in lifecycle.read_lines(pool_mod.sessions_path(product))
                if rec.get('job') in (job, clerk_job) and lifecycle.is_launch(rec)]
    attempts, clerk_attempts = launches.count(job), launches.count(clerk_job)
    open_ids = [iid for iid, _line in pairs]
    # F-0093 §2.4: the `inbox:` lines are the clerk's half, its own job and its own count (P9)
    clerk_ids = [iid for iid in open_ids if iid.startswith('inbox:')]
    groom_dir = os.path.join(env.state_dir(product), 'groom')
    return {'date': date, 'file': path,
            'answers': os.path.join(groom_dir, f'{date}.answers'),
            'clerk_answers': os.path.join(groom_dir, f'{date}.clerk.answers'),
            'clerk_attempts': clerk_attempts,
            'clerk_new': (_not_yet_put(product, clerk_job, clerk_ids) if clerk_attempts
                          else list(clerk_ids)),
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
    if index is None:
        from asf.views import index_reader
        index = (index_reader.load(root)[0]
                 if root and os.path.isfile(os.path.join(root, 'index.json')) else {})
        if index and product.repo_dir and _triage_wanted():
            index = plan_order.overlay(index, plan_order.trunk_reader(product))
    if _triage_wanted():
        triage = _triage_facts(product, root, index)
    occ, tries = occupancy(product), attempts(product)
    landed_shas, unverified = landings(product, occ, index)
    return {'attempts': tries, 'occupancy': occ,
            'groom_state': groom_state(product, root) if groom_policy.groom_auto(product) else None,
            'held': set(approvals.parked(product)),
            'gate': invariant_gate(product),
            'bandwidth': capacity_mod.bandwidth(product),
            'landed_shas': landed_shas, 'unverified_landed': unverified,
            'unverified_on_trunk': unverified_on_trunk(product, occ, unverified),
            'adjudicated': adjudications(product, index, tries),
            'failing': failing(product),
            **triage}


def failing(product):
    """The jobs failing to spawn tick after tick (:meth:`asf.workers.wave.Failures.read`) —
    ``plan_rows`` marks their rows FAILING TO SPAWN. Never a failure: none read is none."""
    try:
        from asf.workers.wave import Failures
        return Failures.read(product)
    except Exception:  # noqa: BLE001 — a display fact
        return {}


def landings(product, occ, index):
    """``(landed_shas, unverified_landed)`` for ``plan_rows``: the open items the lifecycle
    records as landed, split by whether the landing holds up as theirs
    (:func:`asf.workers.landing.verify_landings`). Never a failure: a product with no repo, or
    a git that cannot answer, verifies nothing."""
    from asf.feeder import rows as feeder_rows
    from asf.workers import landing
    try:
        items = feeder_rows.items_of(index) if index else {}
        return landing.verify_landings(product, occ, items, path=pool_mod.sessions_path(product))
    except Exception:  # noqa: BLE001 — a verification that cannot run claims nothing
        return {}, {}


def unverified_on_trunk(product, occ, unverified):
    """The unverified landings (:func:`landings`) whose recorded sha ``origin/<main>`` carries —
    under ``flags.roots`` an ``after:`` on one is answered
    (:func:`asf.feeder.rows.hold_unlanded`). Flag off, or a git that cannot answer: none."""
    from asf.feeder import rows as feeder_rows
    from asf.workers import landing
    if not unverified or not feeder_rows.roots_on(product):
        return set()
    try:
        landed = (occ or {}).get('landed') or {}
        main = getattr(getattr(product, 'conventions', None), 'main', None) or 'main'
        return {i for i in unverified
                if landing.on_trunk(product.repo_dir, main, landed.get(i) or '')}
    except Exception:  # noqa: BLE001 — a fact that cannot be read answers nothing
        return set()


def adjudications(product, index, tries):
    """``{item: {'runs', 'at', 'same_card'[, 'ruling', 'carried']}}`` for the items past the attempt limit
    (:func:`asf.feeder.rows.attempt_limit`) an adjudicate session has ended on: how many, the
    newest's start, and whether it was handed the card as it stands now (its ``card_digest`` is
    :func:`asf.briefs.build.card_digest` today; a run that recorded none counts as the same
    card). The feeder's over-limit row reads it (:func:`asf.feeder.rows._capped`): adjudicated on
    this card already is a PARKED row, never a silent drop and never the same session again.
    An operator ruling filed since that run (:func:`ruled_since`) is a card change. On the same
    card, the ruling that run filed (:func:`session_ruling`) is ``ruling``, and ``carried`` says
    whether a session has started on the item since it: the feeder hands an uncarried ruling to
    one session instead of parking (F-0109, F-0035, F-0003 on 2026-10-05)."""
    from asf.feeder import rows as feeder_rows
    limit = feeder_rows.attempt_limit(product)
    over = {i for i, n in (tries or {}).items() if n > limit}
    if not over:
        return {}
    digest = importlib.import_module('asf.briefs.build').card_digest
    out, starts = {}, {}
    for rs in lifecycle.runs(pool_mod.sessions_path(product)).values():
        for run in rs:
            item = run.get('item')
            if item in over and run.get('kind') not in ('adjudicate', 'park') \
                    and run.get('started') and not lifecycle.quota_exhausted(run):
                starts.setdefault(item, []).append(run['started'])
            if item not in over or run.get('kind') != 'adjudicate' or not run.get('ended') \
                    or lifecycle.quota_exhausted(run):
                continue
            cur = out.setdefault(item, {'runs': 0, 'at': '', 'digest': ''})
            cur['runs'] += 1
            if (run.get('started') or '') >= cur['at']:
                cur['at'], cur['digest'] = run.get('started') or '', run.get('card_digest') or ''
    for item, cur in out.items():
        try:
            now = digest(product, item, index) if index else ''
        except Exception:  # noqa: BLE001 — a card that cannot be digested is unchanged
            now = ''
        cur['same_card'] = not cur['digest'] or not now or cur.pop('digest') == now
        cur.pop('digest', None)
        if cur['same_card'] and ruled_since(product, item, cur['at']):
            cur['same_card'] = False
        if cur['same_card']:
            ruling = session_ruling(product, item, cur['at'])
            if ruling:
                cur['ruling'] = ruling
                cur['carried'] = carried_after(starts.get(item, ()), ruling['at'])
    return out


def _stamp(at):
    """``2026-10-03T12:30:00Z`` and ``2026-10-03 12:30`` alike, to the minute."""
    return str(at or '')[:16].replace('T', ' ')


def session_ruling(product, item, at):
    """The newest ruling an adjudicate session filed on ``item``'s card (``adjudicate
    (adjudicate-…)``, :func:`asf.tick.step_health.file_rulings`) stamped at or after ``at`` — the
    start of the newest adjudicate run, so the ruling that run produced — as ``{at, job, text}``,
    or None. An operator ruling is :func:`ruled_since`'s: it lifts the park itself."""
    from asf.evidence import rulings
    from asf.workers.correct import OPERATOR
    since = _stamp(at)
    if not since:
        return None
    mine = [r for r in rulings.standing(product, item)
            if r.get('job') != OPERATOR and _stamp(r.get('at')) >= since]
    return max(mine, key=lambda r: _stamp(r.get('at'))) if mine else None


def carried_after(starts, at):
    """True when a session other than an adjudicate one started on the item at or after the
    ruling's stamp ``at`` — the ruling has been handed to a session once already."""
    since = _stamp(at)
    return any(_stamp(s) >= since for s in starts)


def ruled_since(product, item, at):
    """The newest operator ruling (``adjudicate (operator)``, :func:`asf.workers.correct.file_ruling`)
    on ``item``'s card stamped at or after ``at`` (the newest adjudicate run's start), or ''.

    The digest skips ``## History`` (D4), so a person's ruling alone never lifted an
    "adjudicated, card unchanged" park (a product's T-0338, 2026-10-04: parked with two binding
    operator rulings on its History). Only an operator ruling counts: a factory History line
    (a state note, a park) or an adjudicate session's own ruling (filed after the run it parks
    on) is no decision, and counting either would unstick every park."""
    from asf.evidence import rulings
    from asf.workers.correct import OPERATOR
    since = str(at or '')[:16].replace('T', ' ')
    if not since:
        return ''
    stamps = [r['at'].replace('T', ' ') for r in rulings.standing(product, item)
              if r.get('job') == OPERATOR]
    return max((s for s in stamps if s >= since), default='')


def invariant_gate(product):
    """``plan_rows``' ``gate``: the launch-time invariants (I4, I5, I7) judged before the cut
    (:func:`asf.invariants.feeder_waits`) — a violating row waits with the invariant's reason
    and its seat goes to the next valid row. Silent: the wave's :func:`gated_plan` is the net
    that logs."""
    from asf import invariants

    def gate(rows, items):
        return invariants.feeder_waits(product, rows, items, out=lambda _l: None)
    return gate


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


def gated_plan(items, product, running, capacity, inputs, out=print, exclude=None,
               s1_first=True):
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
        kw = dict(inputs, exclude=frozenset(exclude)) if exclude else dict(inputs)
        if not s1_first:
            kw['s1_first'] = False
        planned = feeder_rows.plan_rows(items, product, running, capacity, **kw)
        kept = invariants.feeder_gate(product, planned, items, out=say)
        dropped = ({invariants.row_key(r) for r in planned if r.launches}
                   - {invariants.row_key(r) for r in kept if r.launches})
        if dropped and inputs.get('gate') is not None:
            # the feeder judged these rows before the cut: the net firing means it missed one
            say(f"INVARIANT net: the feeder planned {len(dropped)} row(s) its gate drops "
                f"({', '.join(sorted(dropped))})")
        if not dropped - exclude:
            break
        exclude |= dropped
    return kept, exclude


#: the plan :func:`left_out` compares against: every ready row, whatever the seats
READY_ALL = 10_000


def left_out(items, product, running, inputs, planned, logged, dropped, seats):
    """``[(row, why)]``: each ready launching row the plan cut that no other line names — every
    Ready row a wave does not launch says why (2026-09-26 19:22: a wave launched one S1 row with
    7 seats free and said nothing of 6 ready rows the S1 lane had cut). ``why``: the S1 lane
    (:func:`asf.feeder.tiers.select` — no tier-2 row while an S1 row has no seat), or
    no seat left under ``seats``. ``logged``: rows another line names (the fair share's)."""
    from asf.feeder import tiers
    ready = gated_plan(items, product, running, len(running) + READY_ALL, inputs,
                       out=lambda _l: None, exclude=set(dropped or ()), s1_first=False)[0]
    held = set(inputs.get('held') or ())
    seen = {(x.item_id, x.kind) for x in list(planned) + list(logged)}
    # the S1 lane cuts tier 2 only while an S1 row that needs a session has no seat
    s1 = [x.item_id for x in planned if x.tier == tiers.TIER_S1 and x.item_id not in held
          and x.action == tiers.NO_SLOT]
    out = []
    for row in ready:
        if not row.launches or row.item_id in held or (row.item_id, row.kind) in seen:
            continue
        if s1 and row.tier == tiers.TIER_REST:
            why = (f"S1 first: {', '.join(dict.fromkeys(s1))} needs a seat before any other row "
                   f"(the S1 lane holds tier-2 rows while an S1 row has no seat)")
        else:
            why = f'no seat left: share {seats}, in flight {len(running)}'
        out.append((row, why))
    return out


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


def demand(items, product, running, inputs, extra=0, exclude=None, cfg=None):
    """This product's ``wanted`` for :func:`asf.capacity.write_demand`: its launchable ready rows
    (the gate's drops and a parked item's rows are not), capped only by its own configured
    session ceiling (:func:`asf.capacity.product_sessions`, plus the cloud lane's seats) less
    what it has in flight.

    2026-09-26 08:51 (a product tick): a product with 8 ready rows, held by host pressure,
    recorded ``wanted 2`` — its plan's S1 cut had parked every tier-2 row behind its two S1
    rows — and its partner borrowed the slots its ready work would have filled. So ``wanted``
    ignores host pressure, this tick's room, the S1 lane's cut and the fair share: what the
    product has ready is what it claims."""
    cfg = env.load_config() if cfg is None else cfg
    ceiling = capacity_mod.product_sessions(product, cfg)[0] + extra
    rows = gated_plan(items, product, running, ceiling, inputs, out=lambda _l: None,
                      exclude=set(exclude or ()), s1_first=False)[0]
    return min(wanted(rows, inputs.get('held')), max(0, ceiling - len(running)))


def _repo_facts(*a, **kw):
    from asf.briefs import facts
    return facts.repo_facts(*a, **kw)


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
                        host_load_bypass=host_load_bypass,
                        local_only=cloud_mod.truthy(item.get('local_only')))


def relaunch_assessment(product, row, wrow):
    """``(reason, landed, hit)`` — the relaunch cap's judgement of ``wrow``, read-only
    (:func:`asf.workers.relaunch.assess`, and the trunk evidence a park would close on); sets
    ``wrow.cause``. :func:`relaunch_capped` acts on it; :func:`screen`'s preview only reads it."""
    wrow.cause = relaunch.cause_key(row.kind, getattr(row, 'correction', '') or '')
    path = pool_mod.sessions_path(product)
    head = None
    if product.repo_dir and wrow.branch:
        p = subprocess.run(['git', 'rev-parse', '--verify', '-q',
                            f'refs/remotes/origin/{wrow.branch}'],
                           cwd=product.repo_dir, capture_output=True, text=True)
        head = p.stdout.strip() if p.returncode == 0 else None
    reason, landed = relaunch.assess(path, wrow.job, wrow.item, head=head,
                                     card=wrow.card_digest, cause=wrow.cause,
                                     repo=product.repo_dir, main=product.main,
                                     writes=landing.item_writes(product, wrow.item),
                                     product=product, guard=loops.settings(product))
    hit = trunkclose.evidence(path, wrow.item, product.repo_dir, product.main,
                              landing.item_writes(product, wrow.item), product=product) \
        if reason and landed else None
    return reason, landed, hit


def _stamp_report_key(product, path, item, job):
    """Writes ``report_key`` on ``job``'s newest ended run when the ledger has none yet, so the
    loop guard's same-report rule reads it off the record on every later tick — never off a log
    a later run has since overwritten (:mod:`asf.workers.loops`)."""
    runs = [r for r in lifecycle.item_runs(path, item) if r.get('job') == job and r.get('ended')]
    newest = max(runs, key=lambda r: r.get('started') or '', default=None)
    if newest is not None and not newest.get('report_key'):
        text = str((lifecycle.result_of(newest) or {}).get('result') or '')
        pool_mod.update_session(product, job, report_key=loops.report_key(text))


def relaunch_capped(product, row, wrow, out=print):
    """True when ``wrow`` is not launched: the same job was handed the same state (head, card,
    cause) :data:`asf.workers.relaunch.CAP` times, or once with a terminal report
    (:func:`asf.workers.relaunch.verdict`), or the loop guard's same-report or daily-cap rule
    refused it (:mod:`asf.workers.loops`). The job's newest run is parked instead — the feeder
    shows it ``PARKED`` from the next read on — unless the last report names a commit git
    verifies on the trunk and the branch holds nothing past it: then the run is closed on that
    sha (:func:`asf.workers.trunkclose.close`), never parked. ``wrow.cause`` is set for the
    ledger either way. Before parking, its newest ended run's ``report_key`` is persisted (so a
    later tick's same-report rule reads it off the record) and, unless the same loop already
    raised one (:func:`asf.workers.relaunch.alarmed`), one ``ALARM loop`` line is printed."""
    try:
        reason, landed, hit = relaunch_assessment(product, row, wrow)
    except Exception as e:  # noqa: BLE001 — the cap never blocks a wave by failing
        out(f'relaunch: cap check failed for {wrow.job} — {e}')
        return False
    if not reason:
        return False
    path = pool_mod.sessions_path(product)
    _stamp_report_key(product, path, wrow.item, wrow.job)
    if isinstance(landed, landing.Unknown):
        # the host could not say whether the item's own PR holds its work: neither a park nor a
        # close this tick (a park on an unknown would wait for a person)
        out(f'waits    {wrow.job:<24} {wrow.item:<10} — {trunkclose.WAITS_UNKNOWN}: '
            f'{landed.why}')
        return True
    if hit:
        # the park would only ask a person to close what git already proves landed
        sha, run, _claim = hit
        trunkclose.close(product, run['job'], sha, reason, run.get('trunk_arm', ''))
        out(f'closed   {wrow.job:<24} {wrow.item:<10} — landed: {sha[:9]} (verified on '
            f'origin/{product.main}); not relaunched, not parked')
        return True
    key = relaunch.loop_key(reason)
    if not relaunch.alarmed(path, wrow.job, wrow.item, key):
        out(f'ALARM loop {wrow.job} {wrow.item} — {reason}')
    pool_mod.update_session(product, wrow.job,
                            **relaunch.park_fields(reason, wrow.card_digest, pool_mod.now_iso()))
    out(f'parked   {wrow.job:<24} {wrow.item:<10} — {reason}')
    return True


#: what :func:`screen` says of a row (``Screened.kind``): it starts, or why it does not
STARTS, WAITS, HELD, CLOSED, HOST, NO_SEAT, CAPPED = (
    '', 'waits', 'held', 'closed', 'host', 'no seat', 'relaunch cap')


class Screened:
    """One planned row through :func:`screen`: ``why`` empty when the wave starts it, else the
    wave's own words for why not, ``kind`` one of the names above. ``wrow``/``brief`` are the
    worker row and brief a starting row launches with (the live wave's; a preview's ``wrow`` is
    the bare row the relaunch cap judged)."""

    def __init__(self, row, why='', kind=STARTS, bypass=False, wrow=None, brief=None):
        self.row, self.why, self.kind, self.bypass = row, why, kind, bypass
        self.wrow, self.brief = wrow, brief

    @property
    def starts(self):
        return self.row.launches and not self.why


def _quiet(_line):
    return None


def preview_row(product, row, items):
    """The worker row the relaunch cap judges, without a brief: the job a brief of ``row``'s
    kind launches as and the card digest it would state (:func:`asf.briefs.build.card_digest`)."""
    briefs_build = importlib.import_module('asf.briefs.build')  # the module, not briefs.build()
    kind = briefs_build.normalize_kind(row.brief_kind)
    attr = KIND_JOB_KEY.get(kind)
    return pool_mod.Row(job_name(kind, row.item_id, key=getattr(row, attr, None) if attr else None),
                        row.item_id, kind=kind, branch=row.branch or None,
                        card_digest=briefs_build.card_digest(product, row.item_id, items))


def _preview_capped(product, row, wrow):
    """The relaunch cap's refusal of ``wrow`` in words, '' when it would launch — what
    :func:`relaunch_capped` would print, never parking or closing anything."""
    try:
        reason, landed, hit = relaunch_assessment(product, row, wrow)
    except Exception:  # noqa: BLE001 — as in the wave: a failed cap check never refuses
        return ''
    if not reason:
        return ''
    if isinstance(landed, landing.Unknown):
        return f'{trunkclose.WAITS_UNKNOWN}: {landed.why}'
    if hit:
        return f'landed: {hit[0][:9]} (verified on origin/{product.main}); closed, not relaunched'
    return reason


def screen(product, planned, items, running, held, seats, host=None, bypass_open=False,
           act=False, out=print, build=None, ctx=None):
    """``[Screened]`` — the wave's one per-row filter over ``planned``, in plan order: a row
    that does not launch (``WAITS ON …``); an item an approval hold parks (``held``,
    :func:`asf.approvals.parked`); one whose work is verified on the trunk
    (:func:`asf.workers.trunkclose.closes_before_launch`); the host-pressure hold (``host`` —
    ``(held, why)`` — which an S1 row may pass once while ``bypass_open``); no seat left of
    ``seats`` less ``running``; the relaunch cap (:func:`relaunch_capped`). What passes all of
    them starts.

    The live wave (``act``) prints each ``waits`` line, closes and parks as each check says, and
    builds each starting row's brief and worker row through ``build(row, bypass)``. The status
    cell's preview (``act`` false, :func:`would_start`) runs the same checks in the same order
    and changes nothing — so "Ready to launch N" is the N rows this wave would start."""
    host_held, host_why = tuple(host or (False, ''))[:2]
    say = out if act else _quiet
    room = max(0, seats - len(running))
    started = 0
    result = []
    for row in planned:
        cause = getattr(row, 'cause', '')
        if act and cause:                       # §2.5: a cheap cause is said, held or re-run
            ctx.event('triage', item=row.item_id, cause=cause, row=row.kind, action=row.action)
            out(f'triage   {row.item_id:<10} — {cause}: {row.reason}')
        if not row.launches:
            say(f"waits    {'-':<24} {row.item_id:<10} — {row.action}")
            result.append(Screened(row, row.action, WAITS))
            continue
        job = job_name(row.brief_kind, row.item_id)
        if row.item_id in held:                 # §2.4: a harvest merge hold parks its item
            cls, level = held[row.item_id]
            why = f'held {cls} ({level})'
            say(f'waits    {job:<24} {row.item_id:<10} — {why}')
            result.append(Screened(row, why, HELD))
            continue
        if trunkclose.closes_before_launch(product, row.brief_kind, row.item_id, say,
                                           **({} if act else {'dry_run': True})):
            result.append(Screened(row, 'its work is verified on the trunk — closed, not '
                                        'launched', CLOSED))
            continue
        bypass = bypass_open and (items.get(row.item_id) or {}).get('severity') == 'S1'
        if host_held and not bypass:             # a loaded host takes no new session this tick
            why = f'held: {host_why}'
            say(f'waits    {job:<24} {row.item_id:<10} — {why}')
            result.append(Screened(row, why, HOST))
            continue
        if room <= 0 and not bypass:
            why = (f'no seat left: share {seats}, in flight {len(running)}, '
                   f'this wave {started}')
            say(f'waits    {job:<24} {row.item_id:<10} — {why}')
            result.append(Screened(row, why, NO_SEAT))
            continue
        room -= 1
        if bypass:
            bypass_open = False                 # one bypass at a time, across the whole wave
        if act:
            wrow, brief = build(row, bypass)
            capped = 'parked' if relaunch_capped(product, row, wrow, out) else ''
        else:
            wrow, brief = preview_row(product, row, items), None
            capped = _preview_capped(product, row, wrow)
        if capped:
            room += 1
            if bypass:
                bypass_open = True
            result.append(Screened(row, capped, CAPPED, wrow=wrow))
            continue
        started += 1
        result.append(Screened(row, bypass=bypass, wrow=wrow, brief=brief))
    return result


def would_start(product, root, items=None):
    """``(screened, seats, running)`` — the wave this tick would run, previewed: the plan the
    wave cuts (the same ceiling, cloud seats, gate and holds) through :func:`screen` with
    nothing acted on. What ``asf status`` counts as Ready to launch, and what the dwell
    watchdog (:mod:`asf.dwell`) asks why a free seat stays free."""
    from asf.views import index_reader
    if items is None:
        items, _generated = index_reader.load(root)
        if product.repo_dir:
            items = plan_order.overlay(items, plan_order.trunk_reader(product))
    running = inflight(product)
    r = capacity_mod.resolve(product)
    cloud = cloud_settings(product)
    ready = cloud_readiness(product, cloud)
    _held, _hold, extra = split_hold(cloud, ready, False, '')
    inputs = plan_inputs(product, root, items)
    seats = r.sessions + extra
    planned, _dropped = gated_plan(items, product, running, seats, inputs, out=_quiet)
    host_held, host_why, reading = host_hold(planned)
    host_held, _local, _extra = split_hold(cloud, ready, host_held, host_why)
    bypass_open = bool(host_held
                       and host_mod.load_only_hold(reading,
                                                   host_mod.guards_from_config(env.load_config()))
                       and not s1_bypass_live())
    held = approvals.parked(product)
    return (screen(product, planned, items, running, held, seats, (host_held, host_why),
                   bypass_open, act=False), seats, running)


def trunk_preflight(ctx, planned, items, out=print):
    """The launching code rows whose Task the trunk already carries — refused, and closed by trunk
    (F-0106). Returns the rows that survive.

    The minting check (:mod:`asf.record.trunk_check`) answers a plan at the moment it lands; this
    answers the seat. Three gaps it and it alone covers: a Task minted before that check existed,
    a trunk that moved between the mint and the seat (a sibling landing the surface), and a Task
    whose own id a trunk commit already names — the fact `landed_shas` was meant to carry and no
    production caller ever passes (P6). Nothing is written but the typed sha (C12); the state and
    its History line are the next ingest's, by ``closing``'s ``reconciled`` rule."""
    from asf.harvest import lane
    from asf.record import trunk_check
    product = ctx.product
    if not product.repo_dir:
        return planned
    root = ctx.record_root()
    kept = []
    for row in planned:
        card = items.get(row.item_id) or {}
        if not (row.launches and row.brief_kind == 'task' and card.get('type') == 'task'):
            kept.append(row)
            continue
        sha = lane.item_on_trunk(product.repo_dir, product.main, row.item_id)
        why = f'a commit on origin/{product.main} names {row.item_id}'
        if not sha:
            found = trunk_check.satisfied_on_trunk(product, trunk_check.card_body(root, card),
                                                   card.get('writes') or ())
            sha, _subject, why = found if found else ('', '', '')
        if not sha:
            kept.append(row)
            continue
        trunk_check.close_by_trunk(root, row.item_id, sha, product=product, out=out)
        line = f'on trunk {row.item_id:<10} — {sha[:12]}: {why} — closed by trunk, not launched'
        ctx.event('on_trunk', item=row.item_id, sha=sha, text=line)
        ctx.counts['refusals'] += 1
        out(line)
    return kept


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


def cloud_readiness(product, cloud):
    """``(ready, why)`` of the cloud lane (:func:`asf.workers.cloud.readiness`) — read once per
    tick, and only when the lane is on (it costs a few ``gh`` calls)."""
    if not cloud.on:
        return False, 'cloud lane off'
    try:
        cfg = env.load_config()
    except env.ConfigError as e:
        return False, f'config unreadable ({e})'
    try:
        return cloud_mod.readiness(cfg, product)
    except Exception as e:  # noqa: BLE001 — an unreadable lane is an unready one
        return False, f'readiness unreadable ({type(e).__name__}: {e})'


def local_seats(sessions, running):
    """The local lane's free seats: the share (``sessions``) less the local sessions live —
    ``running`` less its cloud runs, whose seats are the cloud lane's own (:func:`split_hold`)."""
    from asf.workers import cloud as cloud_mod
    return max(0, sessions - sum(1 for r in running if not cloud_mod.is_cloud(r)))


def split_hold(cloud, ready, host_held, host_why):
    """``(host_held, local_hold, extra seats)``: a ready cloud lane takes the host hold off its
    own rows — the hold becomes ``local_hold`` (the local lane only) — and adds its seats beside
    the local ones; an off or unready lane leaves the hold on every row and adds none."""
    lane_open = cloud.on and bool(ready and ready[0])
    extra = cloud.max_inflight if lane_open else 0
    if host_held and lane_open:
        return False, host_why, extra
    return host_held, '', extra


def top_cause(whys):
    """The commonest reason a launchable row waited this wave, digits folded (``''`` with none)."""
    import collections
    c = collections.Counter(re.sub(r'\d+', 'N', str(w)).strip()[:120] for w in whys if w)
    return c.most_common(1)[0][0] if c else ''


def note_seats(ctx, sessions, cloud, running, wanted_n, cause=''):
    """The tick line's seat reading (:func:`asf.metrics.throughput.seats_record`): the local
    share and its busy seats, the cloud lane's maximum (0 with the lane off) and its busy seats,
    the launchable rows. Never a failure of the wave."""
    from asf.metrics import throughput
    try:
        n_cloud = sum(1 for r in running if cloud_mod.is_cloud(r))
        ctx.seats = throughput.seats_record(
            len(running) - n_cloud, sessions or 0, n_cloud,
            cloud.max_inflight if getattr(cloud, 'on', False) else 0, wanted_n or 0, cause)
    except Exception:  # noqa: BLE001 — a reading lost is a gap in the series, never a wave error
        pass


def note_launched(ctx, launched):
    """This wave's launches count as busy on the tick's seat reading."""
    seats = getattr(ctx, 'seats', None)
    if not isinstance(seats, dict) or not launched:
        return
    for _wrow, rec in launched:
        lane = 'cloud_busy' if cloud_mod.is_cloud(rec or {}) else 'local_busy'
        seats[lane] = seats.get(lane, 0) + 1


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


def fresh_index(root):
    """``{id: item}`` of the record's ``index.json`` as origin holds it now — a fetch into the
    record clone and a read of the fetched file, never a reset: the tick's own uncommitted work
    in the clone stays (its push rebases onto what origin gained, :func:`asf.tick.shadow.push`).
    None when it cannot be read."""
    from asf.tick import shadow
    import json
    if subprocess.run(['git', '-C', root, 'fetch', '-q', 'origin'], capture_output=True,
                      text=True).returncode != 0:
        return None
    branch = shadow._default_branch(root)
    p = subprocess.run(['git', '-C', root, 'show', f'origin/{branch}:index.json'],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        raw = json.loads(p.stdout)
        return {k: v for k, v in raw['items'].items() if not v.get('removed')}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def overlay_blockers(items, fresh):
    """``items`` (the record clone's map) with each card's ``blockedBy`` as ``fresh`` (origin's
    map, :func:`fresh_index`) holds it now: the clone is reset at the tick's start and the wave
    plans minutes later, so an ``asf set blockedBy=`` pushed in between reached no plan and the
    wave launched a spec on a blocked Feature (F-1129). A blocker origin dropped is dropped here
    too. The feeder derives ``blocked`` from the field (:func:`asf.feeder.rows.with_blockers`).
    ``fresh`` None (origin unreadable): ``items`` unchanged. The input is not mutated."""
    if not fresh:
        return items
    out = items
    for iid, v in items.items():
        f = fresh.get(iid)
        if not isinstance(f, dict) or f.get('blockedBy') == v.get('blockedBy'):
            continue
        if out is items:
            out = copy.copy(items)  # a copy of the same kind: the retired cards come along
        out[iid] = dict(v, blockedBy=f.get('blockedBy') if 'blockedBy' in f else [])
    return out


def s1_ids(items):
    return {i for i, v in items.items() if (v or {}).get('severity') == 'S1'}


def s1_refresher(ctx, items, held, texts, kinds, capacity, out=print, index_fn=None):
    """The wave's ``refresh`` (:func:`asf.workers.wave.wave`): an S1 item that reached the
    record after the wave's plan was cut (groomed mid-wave) launches in this wave instead of
    waiting a whole wave and the next tick's record and health. Each call re-reads the record
    (:func:`fresh_index`); only an S1 id the wave has not seen yet re-runs the feeder over the
    fresh index, and its launching rows come back as worker rows — each id once."""
    root = ctx.record_root()
    product = ctx.product
    seen = s1_ids(items)
    index_fn = index_fn or fresh_index

    def refresh(known):
        fresh = index_fn(root)
        if not fresh:
            return []
        new = s1_ids(fresh) - seen
        if not new:
            return []
        seen.update(new)
        running = inflight(product)
        planned, _dropped = gated_plan(fresh, product, running, capacity + len(new),
                                       plan_inputs(product, root, fresh), out=lambda _l: None)
        rows = []
        for row in planned:
            if row.item_id not in new or not row.launches or row.item_id in held:
                continue
            brief = _build(product, row, fresh, running,
                           repo_facts=_repo_facts(product, row, fresh, running))
            wrow = worker_row(row, brief, fresh)
            if wrow.job in known:
                continue
            texts[wrow.job] = brief.text
            kinds[wrow.job] = brief.kind
            out(f'wave: {row.item_id} is S1 and new since the wave began — it launches in this wave')
            rows.append(wrow)
        return rows
    return refresh


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
    try:  # a wedged account-manager usage lock, reclaimed before the pool reads quota (opt-in)
        from asf.workers import account_lock
        account_lock.tick_pass(env.load_config(), out, ctx.event)
    except Exception as e:  # noqa: BLE001 — never a blocker for the wave
        out(f'quota    account lock reclaim failed — {e}')
    items, _generated = index_reader.load(ctx.record_root())
    # the clone is as old as the tick's start: a blockedBy pushed since stops this wave (F-1129)
    items = overlay_blockers(items, fresh_index(ctx.record_root()))
    if product.repo_dir:  # defence in depth: a Task whose card lacks `after:` waits on its plan's order
        items = plan_order.overlay(items, plan_order.trunk_reader(product))
    running = inflight(product)
    r = capacity_mod.resolve(product)
    # the cloud lane's seats beside the local ones: the feeder takes every in-flight run (cloud
    # ones too) off the sum, so what is left is the free local seats plus the free cloud ones
    cloud = cloud_settings(product)
    ready = cloud_readiness(product, cloud)     # once per tick, never per row
    _held, _hold, extra = split_hold(cloud, ready, False, '')
    inputs = plan_inputs(product, ctx.record_root(), items)
    # a Task whose writes: reach the amendable set launches nothing: say it to the console once
    approvals.announce_console_amends(
        product, feeder_rows.plan_rows(items, product, running, READY_ALL,
                                       **dict(inputs, s1_first=False)), out)
    # the feeder check point: a violating row is dropped, logged, and its slot goes to the next
    planned, dropped = gated_plan(items, product, running, r.sessions + extra, inputs, out=out)
    # the Epic's line comes from `items`, the tick's own overlaid index, not `planned` — so it
    # cannot go quiet on the busy tick where it matters most (F-0052, D5)
    for s in feeder_rows.epics_over_budget(items):
        ctx.event('budget_hold', item=s.epic_id, spend=s.usd, budget=s.budget)
        out(budget.epic_line(s))
    wider = (gated_plan(items, product, running, r.ceiling + extra, inputs, out=lambda _l: None,
                        exclude=set(dropped))[0]
             if r.ceiling is not None and r.ceiling != r.sessions else planned)
    wanted_n = demand(items, product, running, inputs, extra, exclude=dropped)
    capacity_mod.write_demand(product.name, len(running), wanted_n)
    share_held = held_by_share(items, product, running, r, planned, inputs, wider=wider)
    counted = share_counted(running, planned, inputs.get('held')) if share_held else ''
    waits = []
    for row in share_held:
        job = job_name(row.brief_kind, row.item_id)
        waits.append(r.fair_share_reason)
        out(f'waits    {job:<24} {row.item_id:<10} — {r.fair_share_reason}; {counted}')
    for row, why in left_out(items, product, running, inputs, planned, share_held, dropped,
                             r.sessions + extra):
        waits.append(why)
        out(f'waits    {row_job(row):<24} {row.item_id:<10} — {why}')
    planned = trunk_preflight(ctx, planned, items, out=out)
    host_held, host_why, reading = host_hold(planned)
    note_seats(ctx, r.sessions, cloud, running, wanted_n,
               host_why if host_held else top_cause(waits))
    # a loaded host still starts cloud sessions: nothing of theirs runs here
    host_held, local_hold, _extra = split_hold(cloud, ready, host_held, host_why)
    # an S1 item's row passes the LOAD half of the guard — never memory/swap pressure, and at
    # most one such bypass live at a time, across every product (asf.workers.host.load_only_hold)
    s1_bypass_open = (host_held
                      and host_mod.load_only_hold(reading, host_mod.guards_from_config(env.load_config()))
                      and not s1_bypass_live())
    # the hard cap: whatever the plan holds, this wave starts at most share - live, live being the
    # one count the Capacity row shows (capacity_mod.live_sessions) — the S1 bypass the only row
    # that may pass it, and its session counts toward live on every later wave
    seats = r.sessions + extra

    def build(row, bypass):
        if row.brief_kind == 'adjudicate' and getattr(row, 'between', ()):
            (la, ta), (lb, tb) = row.between
            pair = getattr(row, 'common', '')
            common = f' · both touch {pair}' if pair else ' · no file in common'
            job = job_name(row.brief_kind, row.item_id)
            out(f'adjudicate {job:<24} {row.item_id:<10} — {la}: {ta[:60]} ↔ {lb}: {tb[:60]}{common}')
        brief = _build(product, row, items, running,
                       repo_facts=_repo_facts(product, row, items, running))
        return worker_row(row, brief, items, host_load_bypass=bypass), brief
    screened = screen(product, planned, items, running, held, seats, (host_held, host_why),
                      s1_bypass_open, act=True, out=out, build=build, ctx=ctx)
    starting = [s for s in screened if s.starts]
    bypassed = any(s.bypass for s in starting)
    worker_rows = [s.wrow for s in starting]
    # the self-tuning loop (asf.tune): its pass, then the tuned model and seat share per kind
    worker_rows = tune_mod.wave_hook(product, worker_rows, running, out=out, event=ctx.event)
    texts = {s.wrow.job: s.brief.text for s in starting}
    kinds = {s.wrow.job: s.brief.kind for s in starting}
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
    # an S1 groomed while the wave launches goes in this wave — not under a host hold, whose
    # one S1 bypass (above) is the most a loaded host takes
    refresh = None if host_held else s1_refresher(ctx, items, held, texts, kinds, seats, out=out)
    launched, _waits = _wave(product, worker_rows, len(worker_rows),
                             brief_fn=lambda r: texts[r.job], out=out, refresh=refresh,
                             **({'local_hold': local_hold} if local_hold else {}),
                             **({'cloud_ready': ready} if cloud.on else {}),
                             # the cloud's seats in ``seats`` are the cloud lane's: the local
                             # lane takes the share's free local seats only (+ an S1 bypass)
                             **({'local_seats': local_seats(r.sessions, running) + bypassed}
                                if extra else {}))
    for wrow, rec in launched:
        ctx.event('launch', item=wrow.item, job=wrow.job,
                  model=rec.get('model'), brief_kind=kinds[wrow.job])
    ctx.counts['launches'] += len(launched)
    note_launched(ctx, launched)
    return 0


def launch_now(ctx, out=print):
    """The wave's own clock's call (:mod:`asf.tick.wave_clock`): the launch path alone — read the
    record, plan, screen and spawn — without the lane pass, the hold announcements or the
    record's demand write :func:`launch` also does. Those are the product tick's own job, still
    run there, under its own tick lock; this runs under the wave clock's ``tick-wave.lock``
    instead, so it is never behind whatever else that tick is doing.

    The breaker, quota and seat checks are the exact ones :func:`launch` applies
    (:func:`cloud_settings`, :func:`gated_plan`, :func:`screen`, :func:`_wave`) — none of them
    cache config, so a cool-down or quota edit takes effect on this call, not the next long tick.
    Every spawn goes through :func:`_wave`'s own cross-process seat claim
    (:class:`asf.workers.pool.Pool`, under :mod:`asf.workers.seats`'s lock), and a row already on
    the session ledger plans as ``running`` (:func:`inflight`) — so a row this call claims is one
    the product's own tick, reading the ledger at the same moment, already sees as spoken for, and
    the two never launch it twice."""
    from asf.views import index_reader
    product = ctx.product
    root = ctx.record_root()
    items, _generated = index_reader.load(root)
    if product.repo_dir:
        items = plan_order.overlay(items, plan_order.trunk_reader(product))
    running = inflight(product)
    r = capacity_mod.resolve(product)
    cloud = cloud_settings(product)
    ready = cloud_readiness(product, cloud)
    _held, _hold, extra = split_hold(cloud, ready, False, '')
    inputs = plan_inputs(product, root, items)
    planned, _dropped = gated_plan(items, product, running, r.sessions + extra, inputs, out=out)
    held = approvals.parked(product)
    host_held, host_why, reading = host_hold(planned)
    host_held, local_hold, _extra = split_hold(cloud, ready, host_held, host_why)
    s1_bypass_open = (host_held
                      and host_mod.load_only_hold(reading, host_mod.guards_from_config(env.load_config()))
                      and not s1_bypass_live())
    seats = r.sessions + extra

    def build(row, bypass):
        brief = _build(product, row, items, running,
                       repo_facts=_repo_facts(product, row, items, running))
        return worker_row(row, brief, items, host_load_bypass=bypass), brief

    screened = screen(product, planned, items, running, held, seats, (host_held, host_why),
                      s1_bypass_open, act=True, out=out, build=build, ctx=ctx)
    starting = [s for s in screened if s.starts]
    if host_held and not any(s.bypass for s in starting):
        out(f'wave: held: {host_why} — no new session this run; running sessions go on')
        return 0
    if local_hold:
        out(f'wave: local lane held: {local_hold} — the cloud lane takes what it can')
    worker_rows = [s.wrow for s in starting]
    if not worker_rows:
        out('wave: nothing to launch')
        return 0
    texts = {s.wrow.job: s.brief.text for s in starting}
    launched, _waits = _wave(product, worker_rows, len(worker_rows),
                             brief_fn=lambda rr: texts[rr.job], out=out,
                             **({'local_hold': local_hold} if local_hold else {}),
                             **({'cloud_ready': ready} if cloud.on else {}),
                             **({'local_seats': local_seats(r.sessions, running)}
                                if extra else {}))
    for wrow, rec in launched:
        ctx.event('launch', item=wrow.item, job=wrow.job,
                  model=rec.get('model'), brief_kind=wrow.kind)
    ctx.counts['launches'] = ctx.counts.get('launches', 0) + len(launched)
    return 0
