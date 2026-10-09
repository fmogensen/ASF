"""asf.kernel.loop — the one loop: lock, read facts, decide, apply (ASF 0.2).

:func:`tick` takes ``state/<product>/kernel.lock`` (a second tick on the same product finds it
held and returns at once), reads :func:`asf.kernel.facts.read_facts`, runs
:func:`asf.kernel.decide.decide` and applies the plan (:func:`asf.kernel.apply.apply`). A dry run
takes no lock, prints the plan instead of applying it, and runs under
:func:`asf.mutation_guard.active`, so even a mutating ``gh`` call that slipped through refuses.
"""
import collections
import contextlib
import fcntl
import json
import os

from asf.kernel import actions as A
from asf.kernel import ports as P
from asf.kernel.apply import apply, describe
from asf.kernel.decide import decide
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
    }


def print_summary(summary, out=print):
    out('kernel tick%s: %s' % (' (dry run)' if summary['dry_run'] else '',
                              ', '.join('%s %d' % kv for kv in summary['states'].items()) or 'no items'))
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


def save_plan(state_dir, plan, facts):
    """Write :data:`PLAN_FILE`: each judged item's state (and Stuck), and the sessions."""
    from asf.kernel.ports import now_iso
    data = {'at': now_iso(), 'states': {
        iid: {'state': s.value, **({'reason': st.reason, 'owner': st.owner,
                                    'blocked': st.blocked_count} if st else {})}
        for iid, (s, st) in sorted(plan.states.items())},
        'sessions': [{'job': x.job, 'item': x.item_id, 'kind': x.kind, 'alive': x.alive,
                      'started': getattr(x, 'started', '')} for x in facts.sessions]}
    path = os.path.join(state_dir, PLAN_FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + '.tmp', path)


def tick(product, dry_run=False, ports=None, config=None, state_dir=None, out=print):
    """One tick of the kernel for ``product`` (a name or an :class:`asf.env.Product`). Returns the
    :func:`summarize` dict; ``{'locked': path}`` when another tick holds the lock."""
    from asf import env, mutation_guard
    product = _product(product)
    ports = ports or P.real_ports(product)
    config = config or P.config_for(product)
    state_dir = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
    if dry_run:
        with mutation_guard.active():
            facts = read_facts(ports)
            plan = decide(facts, config)
        for a in plan.actions:
            out('would %s' % describe(a))
        summary = summarize(plan, facts, dry_run=True)
        print_summary(summary, out)
        return summary
    try:
        with lock(state_dir):
            snapshot = getattr(ports.record, 'snapshot', None)
            if snapshot:
                snapshot()
            facts = read_facts(ports)
            plan = decide(facts, config)
            result = apply(plan, facts, ports, log=out)
            publish = getattr(ports.record, 'publish', None)
            if publish and result.written:
                publish('kernel: tick (%d card(s))' % len(result.written))
            save_plan(state_dir, plan, facts)
    except Locked as e:
        out('kernel tick: another tick holds %s' % e)
        return {'locked': str(e)}
    summary = summarize(plan, facts, result)
    print_summary(summary, out)
    return summary
