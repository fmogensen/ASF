"""asf.kernel.loop — the one loop: lock, read facts, decide, apply (ASF 0.2).

:func:`tick` takes ``state/<product>/kernel.lock`` (a second tick on the same product finds it
held and returns at once), reads :func:`asf.kernel.facts.read_facts`, runs
:func:`asf.kernel.decide.decide` and applies the plan (:func:`asf.kernel.apply.apply`). A dry run
takes no lock, prints the plan instead of applying it, and runs under
:func:`asf.mutation_guard.active`, so even a mutating ``gh`` call that slipped through refuses.

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
from asf.kernel import ports as P
from asf.kernel.apply import apply, describe
from asf.kernel.decide import blind_plan, decide
from asf.kernel.facts import read_facts
from asf.kernel.model import State

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
    }


def idle_line(idle):
    """The idle alarm as one line (``IDLE: 3 seat(s) free, 5 not launched — waits on after: 3,
    file overlap: 2``), or '' when ``idle`` is None."""
    if not idle:
        return ''
    return 'IDLE: %d seat(s) free, %d not launched — %s' % (
        idle['free'], idle['waiting'], ', '.join('%s %d' % (r, n) for r, n in idle['reasons']))


def print_summary(summary, out=print):
    out('kernel tick%s: %s' % (' (dry run)' if summary['dry_run'] else '',
                              ', '.join('%s %d' % kv for kv in summary['states'].items()) or 'no items'))
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


def save_plan(state_dir, plan, facts):
    """Write :data:`PLAN_FILE`: each judged item's state (and Stuck), and the sessions."""
    from asf.kernel.ports import now_iso
    data = {'at': now_iso(), 'states': {
        iid: {'state': s.value, **({'reason': st.reason, 'owner': st.owner,
                                    'blocked': st.blocked_count} if st else {})}
        for iid, (s, st) in sorted(plan.states.items())},
        'sessions': [{'job': x.job, 'item': x.item_id, 'kind': x.kind, 'alive': x.alive,
                      'started': getattr(x, 'started', '')} for x in facts.sessions],
        'idle': plan.idle, 'notes': plan_notes(plan, facts)}
    path = os.path.join(state_dir, PLAN_FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + '.tmp', path)


def sync_cloud(ports, out=print):
    """Before the facts are read: the session port's cloud runs brought up to date (a port with
    ``sync``: :meth:`asf.kernel.ports.RealSessions.sync`), and its launch log pointed at ``out``.
    A failed sync is one line; the tick reads the sessions as the last sync left them."""
    for port in (ports.sessions, ports.brief):
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


def write_plan_out(path, plan, summary):
    """A dry run's plan as JSON at ``path`` (``asf kernel tick --dry-run --plan-out``, read by
    the install's shadow preflight, :func:`asf.kernel.host.preflight`): each judged item's state,
    the state counts and the planned actions by type. ``path`` is the caller's file, never the
    product's state."""
    data = {'states': {iid: s.value for iid, (s, _st) in sorted(plan.states.items())},
            'counts': summary['states'], 'actions': summary['actions']}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)


def blind_tick(facts, ports, out=print):
    """Apply the blind plan of ``facts`` (see the module doc); the tick's summary."""
    plan = blind_plan(facts)
    result = apply(plan, facts, ports, log=out, judged=False)
    publish = getattr(ports.record, 'publish', None)
    if publish and result.written:
        try:
            publish('kernel: blind tick (%d card(s))' % len(result.written))
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
        with mutation_guard.active():
            facts = read_facts(ports)
            if facts.github_error:
                out('kernel tick (dry run): GitHub unreadable — %s' % facts.github_error)
                return {'blind': facts.github_error, 'dry_run': True, 'failed': []}
            plan = decide(facts, config)
        for a in plan.actions:
            out('would %s' % describe(a))
        summary = summarize(plan, facts, dry_run=True)
        summary['waits'] = measure_waits(product, state_dir, plan, facts, config, write=False,
                                         out=out)
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
            facts = read_facts(ports)
            if facts.github_error:
                return blind_tick(facts, ports, out)
            plan = decide(facts, config)
            result = apply(plan, facts, ports, log=out)
            publish = getattr(ports.record, 'publish', None)
            if publish and result.written:
                try:
                    publish('kernel: tick (%d card(s))' % len(result.written))
                except Exception as e:  # a refused or failed record commit never ends the tick
                    out('publish FAILED: %s' % (str(e).splitlines() or [type(e).__name__])[0])
            save_plan(state_dir, plan, facts)
            waits_line = measure_waits(product, state_dir, plan, facts, config, out=out)
    except Locked as e:
        out('kernel tick: another tick holds %s' % e)
        return {'locked': str(e)}
    summary = summarize(plan, facts, result)
    summary['waits'] = waits_line
    print_summary(summary, out)
    return summary
