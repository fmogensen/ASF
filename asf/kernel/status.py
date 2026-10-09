"""asf.kernel.status — the kernel's status table (ASF 0.2).

Three blocks, read-only: the Stuck items first (reason, owner, how many items wait on each, and
how long it has been stuck), then the count of items per state, then the active sessions. The
states are the ones ``decide`` reaches on the facts now (no action is applied); a Stuck item's age
is from its card's ``kernel_stuck_since``, ``-`` until a tick has recorded it.
"""
import collections
import datetime

from asf import mutation_guard
from asf.kernel import ports as P
from asf.kernel.decide import decide
from asf.kernel.facts import read_facts
from asf.kernel.model import State


def _age(since, now):
    try:
        t = datetime.datetime.strptime(str(since), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return '-'
    hours = (now - t).total_seconds() / 3600
    return '%dh' % hours if hours < 48 else '%dd' % (hours / 24)


def _cell(text, width=80):
    text = ' '.join(str(text or '').split()).replace('|', '/')
    return text if len(text) <= width else text[:width - 1] + '…'


def rows(ports, config, now=None):
    """``(stuck rows, state counts, session rows)`` for the status table."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    with mutation_guard.active():
        facts = read_facts(ports)
        plan = decide(facts, config)
    stuck = []
    for iid, (state, st) in plan.states.items():
        if state is not State.STUCK:
            continue
        since = ports.record.card_fields(iid).get(P.STUCK_SINCE)
        stuck.append((iid, st.reason, st.owner, st.blocked_count, _age(since, now) if since else '-'))
    stuck.sort(key=lambda r: (-r[3], r[0]))
    counts = collections.Counter(s.value for s, _ in plan.states.values())
    sessions = [(s.job, s.item_id, s.kind, 'alive' if s.alive else 'dead',
                 _age(getattr(s, 'started', ''), now) if getattr(s, 'started', '') else '-')
                for s in facts.sessions]
    return stuck, counts, sessions


def render(stuck, counts, sessions):
    """The three blocks as markdown tables (the console draws them as boxes)."""
    out = ['## Stuck', '', '| item | owner | blocks | age | reason |', '| --- | --- | --- | --- | --- |']
    out += ['| %s | %s | %d | %s | %s |' % (iid, owner, n, age, _cell(reason))
            for iid, reason, owner, n, age in stuck] or ['| - | - | 0 | - | nothing stuck |']
    out += ['', '## States', '', '| state | items |', '| --- | --- |']
    out += ['| %s | %d |' % (s.value, counts.get(s.value, 0)) for s in State]
    out += ['', '## Sessions', '', '| job | item | kind | pid | age |', '| --- | --- | --- | --- | --- |']
    out += ['| %s | %s | %s | %s | %s |' % r for r in sessions] or ['| - | - | - | - | - |']
    return '\n'.join(out)


def status(product, ports=None, config=None, out=print):
    from asf.kernel.loop import _product
    product = _product(product)
    ports = ports or P.real_ports(product)
    config = config or P.config_for(product)
    text = render(*rows(ports, config))
    out(text)
    return text
