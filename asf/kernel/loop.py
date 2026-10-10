"""asf.kernel.loop — the one loop: lock, read facts, decide, apply (ASF 0.2).

:func:`tick` takes ``state/<product>/kernel.lock`` (a second tick on the same product finds it
held and returns at once), reads :func:`asf.kernel.facts.read_facts`, runs
:func:`asf.kernel.decide.decide` and applies the plan (:func:`asf.kernel.apply.apply`). A dry run
takes no lock, prints the plan instead of applying it, and runs under
:func:`asf.mutation_guard.active`, so even a mutating ``gh`` call that slipped through refuses.

Before ``decide`` the wait ledger's current spells are read into ``Facts.waits`` (a wait over its
class's target is a breach). Every tick line says ``LIMBO n`` (the items and PRs with no action
and nothing in flight, listed one per line: ``limbo <id>: <why>``) and logs one
``BREACH <item> <class> <age> -> <action>`` line per breach (:func:`asf.kernel.decide.breaches`).
A question the facts answered logs ``RESOLVED <item> <class> -> <answer>``, and the tick counts
them against the questions still on the console (:func:`questions_line`).
Each tick measures what the trunk's head moving cost (:mod:`asf.kernel.mainmoves`) and logs
``MAIN MOVE <sha> cost: ...`` (``MAIN MOVE ALARM`` past ``kernel.main_move.alarm_minutes``).
While the trunk is red it logs ``MAIN RED <sha> -> <action>`` (:mod:`asf.kernel.mainline`).
The still-needed gate (:mod:`asf.kernel.needed`) logs ``SATISFIED <item>: …`` (``EMPTY <item>``
for an empty PR closed and relaunched) and the items whose proving-tests check is pending.

A tick whose GitHub read failed (``Facts.github_error``, after the port's retries) is blind
(:func:`blind_tick`): it applies :func:`asf.kernel.decide.blind_plan` only, writes no state, keeps
the last plan, logs one ``GitHub unreadable`` line and returns ``{'blind': why}`` — exit 0.
"""
import collections
import contextlib
import fcntl
import json
import os

from asf.kernel import actions as A
from asf.kernel import intake
from asf.kernel import ports as P
from asf.kernel.apply import apply, describe
from asf.kernel.decide import blind_plan, decide
from asf.kernel.facts import read_facts
from asf.kernel.model import State
from asf.kernel.needed import PENDING, gate_line

LOCK_FILE = 'kernel.lock'

#: the last tick's plan, for ``asf kernel status`` (``state/<product>/``)
PLAN_FILE = 'kernel-plan.json'

#: the Stuck items a summary names, most-blocking first
TOP_STUCK = 10


class Locked(Exception):
    """Another tick holds the product's kernel lock."""


@contextlib.contextmanager
def lock(state_dir):
    """Hold ``state_dir/kernel.lock`` (non-blocking ``flock``) for the ``with`` block."""
    os.makedirs(state_dir, exist_ok=True)
    fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Locked(os.path.join(state_dir, LOCK_FILE)) from None
        yield
    finally:
        os.close(fd)


def _product(product):
    from asf import env
    return product if isinstance(product, env.Product) else env.load_product(product)


def summarize(plan, facts, result=None, dry_run=False):
    """The tick's summary: state counts, the top Stuck items, the planned (or done) actions."""
    counts = collections.Counter(s.value for s, _ in plan.states.values())
    stuck = sorted(((iid, st) for iid, (s, st) in plan.states.items() if s is State.STUCK),
                   key=lambda p: (-p[1].blocked_count, p[0]))
    return {
        'dry_run': dry_run,
        'paused': facts.paused,
        'states': dict(sorted(counts.items())),
        'stuck': [{'item': iid, 'reason': st.reason, 'owner': st.owner,
                   'blocked': st.blocked_count} for iid, st in stuck[:TOP_STUCK]],
        'stuck_total': len(stuck),
        'actions': dict(collections.Counter(type(a).__name__ for a in plan.actions)),
        'launches': [(a.kind, a.item_id, a.branch) for a in plan.actions
                     if isinstance(a, A.Launch)],
        'failed': [(describe(a) if not isinstance(a, tuple) else 'write %s' % a[1], why)
                   for a, why in (result.failed if result else [])],
        'written': list(result.written) if result else [],
        'idle': plan.idle,
        'limbo': dict(getattr(plan, 'limbo', None) or {}),
        'breaches': list(getattr(plan, 'breaches', None) or []),
        'resolved': sum(1 for a in plan.actions if isinstance(a, A.ApplyAnswer) and a.by),
        'escalated': sum(1 for s, st in plan.states.values()
                         if s is State.STUCK and st is not None and st.owner == 'operator'),
        'main': getattr(plan, 'main', None),
        'wip': getattr(plan, 'wip', None),
        'gate': list(getattr(plan, 'gate', None) or []),
        'pending': sorted(iid for iid, texts in (getattr(plan, 'notes', None) or {}).items()
                          if PENDING in texts),
        'dor': getattr(plan, 'dor', None),
        'intake': {'decided': (len(result.intake) if result is not None
                               else sum(1 for a in plan.actions if isinstance(a, A.Decide))),
                   'launched': sum(1 for a in plan.actions
                                   if isinstance(a, A.Launch) and a.kind == intake.KIND)},
    }


def questions_line(summary):
    """``questions: resolved by code N, to the console M (P% by code)`` — this tick's session
    questions a fact answered (:mod:`asf.kernel.resolvers`) against those still waiting on the
    console (operator Stuck); '' when there are none."""
    done, left = summary.get('resolved') or 0, summary.get('escalated') or 0
    if not done and not left:
        return ''
    return 'questions: resolved by code %d, to the console %d (%d%% by code)' % (
        done, left, round(100 * done / (done + left)))


def idle_line(idle):
    """The idle alarm as one line (``IDLE: 3 seat(s) free, 5 not launched — waits on after: 3,
    file overlap: 2``), or '' when ``idle`` is None."""
    if not idle:
        return ''
    return 'IDLE: %d seat(s) free, %d not launched — %s' % (
        idle['free'], idle['waiting'], ', '.join('%s %d' % (r, n) for r, n in idle['reasons']))


def wip_line(wip):
    """The WIP cap's hold as one line (``WIP CAP: 34 open PRs (Review + Landing) > 30 — 12 new
    build/plan/spec launch(es) wait; seats go to finishing work``), or '' when it holds nothing."""
    if not wip:
        return ''
    return ('WIP CAP: %d open PRs (Review + Landing) > %d — %d new build/plan/spec launch(es) '
            'wait; seats go to finishing work' % (wip['open'], wip['cap'], wip['held']))


def dor_line(held):
    """The Definition of Ready's hold as one line (``DOR: 31 held New — no test named in
    Acceptance or Gate 26, writes: empty 2``: the gaps most cards share first), or '' when it holds
    none."""
    if not held:
        return ''
    gaps = collections.Counter(g.split(':', 1)[0] if g.startswith('writes not on trunk') or
                               g.startswith('after:') else g
                               for why in held.values()
                               for g in why[len('dor: '):].split('; '))
    return 'DOR: %d held New — %s' % (len(held), ', '.join(
        '%s %d' % kv for kv in sorted(gaps.items(), key=lambda kv: (-kv[1], kv[0]))[:4]))


def intake_line(summary):
    """``intake: minted M, decided N, launched K intake-decide session(s)`` — this tick's intake
    (:mod:`asf.kernel.intake`), or '' when it did nothing."""
    got = summary.get('intake') or {}
    minted, decided, launched = (summary.get('minted') or 0, got.get('decided') or 0,
                                 got.get('launched') or 0)
    if not (minted or decided or launched):
        return ''
    return 'intake: minted %d, decided %d, launched %d intake-decide session(s)' % (
        minted, decided, launched)


def mint_inbox(ports, out=print):
    """The groom's minting path through the record port (``mint_inbox``): the new card ids, one
    line when there are any; a failure is one line, never the tick's end."""
    mint = getattr(ports.record, 'mint_inbox', None)
    if mint is None:
        return []
    try:
        created = list(mint() or [])
    except Exception as e:  # noqa: BLE001 — intake never stops the tick
        out('kernel tick: inbox mint failed — %s' % (str(e) or type(e).__name__))
        return []
    if created:
        out('intake: minted %s' % ', '.join(created))
    return created


def mint_plans(ports, out=print, config=None):
    """A landed plan's Tasks, before the tick reads its facts: the trunk is fetched
    (``record.refresh_trunk``) and every plan on it whose Feature has no Task yet becomes Task
    cards (``record.mint_plan_tasks``) — decided on this very tick. With
    ``config.resolve_plan_ids`` a plan refused only for cited ids nothing holds has them claimed
    in code first (``claim_cited``); a plan still refused is read into ``Facts.plan_refusals``
    and re-planned. The new ids, one line when there are any; a failure is one line, never the
    tick's end."""
    import inspect
    refresh = getattr(ports.record, 'refresh_trunk', None)
    mint = getattr(ports.record, 'mint_plan_tasks', None)
    kw = {}
    if mint is not None and getattr(config, 'resolve_plan_ids', False) \
            and 'claim_cited' in inspect.signature(mint).parameters:
        kw['claim_cited'] = True
    try:
        why = refresh() if refresh is not None else None
        if why:
            out('kernel tick: %s' % why)
        created = list(mint(out=out, **kw) or []) if mint is not None else []
    except Exception as e:  # noqa: BLE001 — minting never stops the tick
        out('kernel tick: plan-tasks failed — %s' % (str(e) or type(e).__name__))
        return []
    if created:
        out('plan-tasks: minted %s' % ', '.join(created))
    return created


def breach_line(b):
    """``BREACH <item> <class> <age> -> <action>`` of one :attr:`Plan.breaches` record."""
    from asf.kernel.waits import dur
    return 'BREACH %s %s %s -> %s' % (b['item'], b['class'], dur(b['age_s']), b['action'])


def main_line(main):
    """``MAIN RED <sha> -> <action>`` of :attr:`Plan.main` (:mod:`asf.kernel.mainline`)."""
    return 'MAIN RED %s -> %s' % (main['sha'], main['action'])


def print_summary(summary, out=print):
    limbo = summary.get('limbo') or {}
    out('kernel tick%s: %s, LIMBO %d' % (
        ' (dry run)' if summary['dry_run'] else '',
        ', '.join('%s %d' % kv for kv in summary['states'].items()) or 'no items', len(limbo)))
    for key, why in sorted(limbo.items()):
        out('  limbo %s: %s' % (key, why))
    for b in summary.get('breaches') or []:
        out(breach_line(b))
    if questions_line(summary):
        out(questions_line(summary))
    if summary.get('main'):
        out(main_line(summary['main']))
    for line in summary.get('main_moves') or []:
        out(line)
    if summary.get('wip'):
        out(wip_line(summary['wip']))
    for rec in summary.get('gate') or []:
        out(gate_line(rec))
    if summary.get('pending'):
        out('proving-tests check pending: %s' % ', '.join(summary['pending']))
    if summary.get('dor'):
        out(dor_line(summary['dor']))
    if intake_line(summary):
        out(intake_line(summary))
    for key, owner, why in summary.get('unheard') or []:
        out(intake.unheard_line(key, owner, why))
    if summary.get('idle'):
        out(idle_line(summary['idle']))
    if summary['paused']:
        out('launches paused')
    out('actions: %s' % (', '.join('%s %d' % kv for kv in sorted(summary['actions'].items()))
                         or 'none'))
    if summary['stuck']:
        out('top stuck (%d in all):' % summary['stuck_total'])
        for s in summary['stuck']:
            out('  %s [%s, blocks %d] %s' % (s['item'], s['owner'], s['blocked'], s['reason']))
    for kind, iid, branch in summary['launches']:
        out('  launch %s %s on %s' % (kind, iid, branch))
    for what, why in summary['failed']:
        out('  FAILED %s — %s' % (what, why))
    if summary.get('waits'):
        out(summary['waits'])


def plan_notes(plan, facts):
    """``{item id: [note]}``: each item's notes on its card plus this tick's :class:`NoteItem`s
    and the plan's own notes (``Plan.notes``, never written to a card)."""
    out = {iid: list(it.notes) for iid, it in facts.items.items() if it.notes}
    for iid, texts in sorted((getattr(plan, 'notes', None) or {}).items()):
        out.setdefault(iid, []).extend(t for t in texts if t not in out[iid])
    for a in plan.actions:
        if isinstance(a, A.NoteItem) and a.text not in out.setdefault(a.item_id, []):
            out[a.item_id].append(a.text)
    return out


def save_plan(state_dir, plan, facts, unheard=()):
    """Write :data:`PLAN_FILE`: each judged item's state (and Stuck), and the sessions; an
    inbox note Stuck on its owner (``unheard``: :func:`asf.kernel.intake.unheard`) is a Stuck
    row too."""
    from asf.kernel.ports import now_iso
    states = {iid: {'state': s.value, **({'reason': st.reason, 'owner': st.owner,
                                         'blocked': st.blocked_count} if st else {})}
              for iid, (s, st) in sorted(plan.states.items())}
    for key, owner, why in unheard:
        if owner:
            states[key] = {'state': State.STUCK.value, 'reason': why, 'owner': owner,
                           'blocked': 0}
    data = {'at': now_iso(), 'states': states,
        'sessions': [{'job': x.job, 'item': x.item_id, 'kind': x.kind, 'alive': x.alive,
                      'started': getattr(x, 'started', '')} for x in facts.sessions],
        'idle': plan.idle, 'notes': plan_notes(plan, facts),
        'limbo': dict(getattr(plan, 'limbo', None) or {}),
        'breaches': list(getattr(plan, 'breaches', None) or [])}
    path = os.path.join(state_dir, PLAN_FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + '.tmp', path)


def sync_cloud(ports, out=print):
    """Before the facts are read: the session port's cloud runs brought up to date (a port with
    ``sync``: :meth:`asf.kernel.ports.RealSessions.sync`), and its launch log pointed at ``out``.
    A failed sync is one line; the tick reads the sessions as the last sync left them."""
    for port in (ports.sessions, ports.brief, ports.github):
        if hasattr(port, 'log'):
            port.log = out
    sync = getattr(ports.sessions, 'sync', None)
    if sync is None:
        return
    try:
        sync(out)
    except Exception as e:  # noqa: BLE001 — a cloud read never stops the tick
        out('kernel tick: cloud sync failed — %s' % (str(e) or type(e).__name__))


def measure_waits(product, state_dir, plan, facts, config, write=True, out=print):
    """Append the tick's wait changes to the ledger (:func:`asf.kernel.waits.record`) and return
    the tick line (``over-target waits: N, biggest: <class>``); a failure is one line, never the
    tick's end."""
    from asf.kernel import waits
    try:
        _new, records = waits.record(state_dir, plan, facts, config, write=write)
        return waits.tick_line(waits.report(records, targets=waits._targets(product)))
    except Exception as e:  # noqa: BLE001 — the measure never stops the tick
        out('kernel tick: wait ledger failed — %s' % (str(e) or type(e).__name__))
        return ''


def measure_main_moves(product, state_dir, facts, plan, config, write=True, out=print, now=None):
    """Observe the trunk's head (:func:`asf.kernel.mainmoves.observe`): a move's cost so far and
    each finished move's ledger line; returns the ``MAIN MOVE ...`` lines. A failure is one line,
    never the tick's end."""
    import datetime
    from asf.kernel import mainmoves
    try:
        alarm, window = mainmoves.settings_of(product)
        state, lines, finished = mainmoves.observe(
            mainmoves.read_state(state_dir), facts, plan, config,
            now or datetime.datetime.now(datetime.timezone.utc), alarm, window)
        if write:
            mainmoves.append(state_dir, finished)
            mainmoves.write_state(state_dir, state)
        return lines
    except Exception as e:  # noqa: BLE001 — the measure never stops the tick
        out('kernel tick: main move measure failed — %s' % (str(e) or type(e).__name__))
        return []


def write_plan_out(path, plan, summary):
    """A dry run's plan as JSON at ``path`` (``asf kernel tick --dry-run --plan-out``, read by
    the install's shadow preflight, :func:`asf.kernel.host.preflight`): each judged item's state,
    the state counts and the planned actions by type. ``path`` is the caller's file, never the
    product's state."""
    data = {'states': {iid: s.value for iid, (s, _st) in sorted(plan.states.items())},
            'counts': summary['states'], 'actions': summary['actions'],
            'limbo': summary.get('limbo') or {}, 'breaches': summary.get('breaches') or [],
            'gate': summary.get('gate') or []}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)


def read_waits(facts, state_dir, min_samples=None):
    """Fill ``facts.waits`` with each item's current spell on the wait ledger
    (``{item: (class, since)}``) and ``facts.bounds`` with the measured p90s that bound live
    processes (:func:`asf.kernel.waits.bounds`, ``min_samples`` finished spells a class needs);
    an unreadable ledger leaves both empty (no breach, the fallback bounds)."""
    from asf.kernel import waits
    try:
        records = waits.read_ledger(state_dir)
        facts.waits = {iid: (r.get('reason') or '', r.get('at') or '')
                       for iid, r in waits.fold(records).items()}
        facts.bounds = waits.bounds(records, min_samples=waits.MIN_SAMPLES
                                    if min_samples is None else min_samples)
    except Exception:  # noqa: BLE001 — the measure never stops the tick
        facts.waits, facts.bounds = {}, {}
    return facts


def _min_samples(product):
    try:
        return int(product.kernel['waits']['bound_min_samples'])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def seat_line(facts, config, ports):
    """The dry run's seat computation: configured seats, the host's capacity (``Facts.seats``)
    and how the session port read it, the alive sessions and the free seats ``decide`` used."""
    from asf.kernel.decide import _free
    note = getattr(ports.sessions, 'seat_note', None)
    try:
        note = note() if note else ''
    except Exception as e:  # noqa: BLE001 — a note never stops the dry run
        note = 'unread: %s' % e
    return 'seats: configured %d, capacity %s, alive %d, free %d%s' % (
        config.max_sessions, facts.seats, sum(1 for s in facts.sessions if s.alive),
        _free(facts, config), ' — %s' % note if note else '')


def blind_tick(facts, ports, out=print, minted=()):
    """Apply the blind plan of ``facts`` (see the module doc); the tick's summary."""
    plan = blind_plan(facts)
    result = apply(plan, facts, ports, log=out, judged=False)
    publish = getattr(ports.record, 'publish', None)
    if publish and (result.written or minted):
        try:
            publish('kernel: blind tick (%d card(s))' % (len(result.written) + len(minted)))
        except Exception as e:  # a refused or failed record commit never ends the tick
            out('publish FAILED: %s' % (str(e).splitlines() or [type(e).__name__])[0])
    held = sum(1 for s in facts.sessions if not s.alive and s.ended)
    out('kernel tick: GitHub unreadable — %s; PR actions and launches skipped, %d crashed '
        'session(s) ended, %d ended session(s) held; next tick retries'
        % (facts.github_error, len(plan.actions), held))
    return {'blind': facts.github_error, 'actions': {}, 'launches': [], 'written':
            list(result.written),
            'failed': [(describe(a) if not isinstance(a, tuple) else 'write %s' % a[1], why)
                       for a, why in result.failed]}


def tick(product, dry_run=False, ports=None, config=None, state_dir=None, out=print,
         plan_out=None):
    """One tick of the kernel for ``product`` (a name or an :class:`asf.env.Product`). Returns the
    :func:`summarize` dict; ``{'locked': path}`` when another tick holds the lock. A dry run with
    ``plan_out`` also writes its plan there (:func:`write_plan_out`)."""
    from asf import env, mutation_guard
    product = _product(product)
    ports = ports or P.real_ports(product)
    config = config or P.config_for(product, github=ports.github)
    state_dir = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
    if dry_run:
        if hasattr(ports.github, 'log'):
            ports.github.log = out
        with mutation_guard.active():
            facts = read_facts(ports)
            if facts.github_error:
                out('kernel tick (dry run): GitHub unreadable — %s' % facts.github_error)
                return {'blind': facts.github_error, 'dry_run': True, 'failed': []}
            plan = decide(read_waits(facts, state_dir, _min_samples(product)), config)
        unheard = intake.unheard(facts, config, plan.actions)
        for a in plan.actions:
            out('would %s' % describe(a))
        out(seat_line(facts, config, ports))
        count = getattr(ports.record, 'inbox_count', None)
        if count is not None:
            out('intake (dry run): %d note(s) in the inbox, %d with a question — the rest are '
                'minted on a real tick' % (count(), len(facts.notes)))
        summary = summarize(plan, facts, dry_run=True)
        summary['unheard'] = unheard
        summary['waits'] = measure_waits(product, state_dir, plan, facts, config, write=False,
                                         out=out)
        summary['main_moves'] = measure_main_moves(product, state_dir, facts, plan, config,
                                                   write=False, out=out)
        print_summary(summary, out)
        if plan_out:
            write_plan_out(plan_out, plan, summary)
        return summary
    try:
        with lock(state_dir):
            snapshot = getattr(ports.record, 'snapshot', None)
            if snapshot:
                snapshot()
            sync_cloud(ports, out)
            minted = mint_inbox(ports, out) if config.intake else []
            minted = minted + mint_plans(ports, out, config)
            facts = read_facts(ports)
            if facts.github_error:
                return blind_tick(facts, ports, out, minted)
            plan = decide(read_waits(facts, state_dir, _min_samples(product)), config)
            unheard = intake.unheard(facts, config, plan.actions)
            result = apply(plan, facts, ports, log=out)
            publish = getattr(ports.record, 'publish', None)
            if publish and (result.written or result.intake or minted):
                try:
                    publish('kernel: tick (%d card(s))' % len(
                        set(result.written) | set(result.intake) | set(minted)))
                except Exception as e:  # a refused or failed record commit never ends the tick
                    out('publish FAILED: %s' % (str(e).splitlines() or [type(e).__name__])[0])
            save_plan(state_dir, plan, facts, unheard)
            waits_line = measure_waits(product, state_dir, plan, facts, config, out=out)
            moves_lines = measure_main_moves(product, state_dir, facts, plan, config, out=out)
    except Locked as e:
        out('kernel tick: another tick holds %s' % e)
        return {'locked': str(e)}
    summary = summarize(plan, facts, result)
    summary['minted'] = len(minted)
    summary['unheard'] = unheard
    summary['waits'] = waits_line
    summary['main_moves'] = moves_lines
    print_summary(summary, out)
    return summary
