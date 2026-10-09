"""asf.kernel.status — the kernel's status table (ASF 0.2).

Three blocks, read-only: the Stuck items first (reason, owner, how many items wait on each, and
how long it has been stuck), then the count of items per state, then the active sessions — and,
when any item has one, a fourth: the notes (a question a session asked while its work moved on). The
states are the last tick's plan (``state/<product>/kernel-plan.json``, written by every applied
tick), so the table costs no network call; ``live=True`` (``--live``), or no plan on disk yet,
decides afresh on the facts now (no action is applied). A Stuck item's age is from its card's
``kernel_stuck_since``, ``-`` until a tick has recorded it.
"""
import collections
import datetime
import json
import os

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


def rows(ports, config, now=None, with_idle=False, with_notes=False):
    """``(stuck rows, state counts, session rows)`` for the status table (plus the plan's idle
    alarm, ``with_idle``, then the ``{item: [note]}`` map, ``with_notes``)."""
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
    from asf.workers import lifecycle  # the state word is lifecycle's (one classifier)
    sessions = [(s.job, s.item_id, s.kind, 'alive' if s.alive else lifecycle.DEAD,
                 _age(getattr(s, 'started', ''), now) if getattr(s, 'started', '') else '-')
                for s in facts.sessions]
    out = (stuck, counts, sessions) + ((plan.idle,) if with_idle else ())
    if with_notes:
        from asf.kernel.loop import plan_notes
        out += (plan_notes(plan, facts),)
    return out


def rows_from_plan(data, record, now=None):
    """The :func:`rows` triple from a saved plan (:func:`asf.kernel.loop.save_plan`)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stuck, counts = [], collections.Counter()
    for iid, row in (data.get('states') or {}).items():
        counts[row.get('state')] += 1
        if row.get('state') == State.STUCK.value:
            since = record.card_fields(iid).get(P.STUCK_SINCE)
            stuck.append((iid, row.get('reason', ''), row.get('owner', ''),
                          int(row.get('blocked') or 0), _age(since, now) if since else '-'))
    stuck.sort(key=lambda r: (-r[3], r[0]))
    from asf.workers import lifecycle
    sessions = [(x.get('job'), x.get('item'), x.get('kind'),
                 'alive' if x.get('alive') else lifecycle.DEAD,
                 _age(x['started'], now) if x.get('started') else '-')
                for x in data.get('sessions') or []]
    return stuck, counts, sessions


def render(stuck, counts, sessions, idle=None, notes=None):
    """The three blocks as markdown tables (the console draws them as boxes), the idle alarm
    (:func:`asf.kernel.loop.idle_line`) first when it is raised, the notes last when any."""
    from asf.kernel.loop import idle_line
    out = [idle_line(idle), ''] if idle else []
    out += ['## Stuck', '', '| item | owner | blocks | age | reason |', '| --- | --- | --- | --- | --- |']
    out += ['| %s | %s | %d | %s | %s |' % (iid, owner, n, age, _cell(reason))
            for iid, reason, owner, n, age in stuck] or ['| - | - | 0 | - | nothing stuck |']
    out += ['', '## States', '', '| state | items |', '| --- | --- |']
    out += ['| %s | %d |' % (s.value, counts.get(s.value, 0)) for s in State]
    out += ['', '## Sessions', '', '| job | item | kind | pid | age |', '| --- | --- | --- | --- | --- |']
    out += ['| %s | %s | %s | %s | %s |' % r for r in sessions] or ['| - | - | - | - | - |']
    noted = [(iid, n) for iid in sorted(notes or {}) for n in notes[iid]]
    if noted:
        out += ['', '## Notes', '', '| item | note |', '| --- | --- |']
        out += ['| %s | %s |' % (iid, _cell(n, 120)) for iid, n in noted]
    return '\n'.join(out)


def status(product, ports=None, config=None, out=print, live=False, state_dir=None):
    from asf import env
    from asf.kernel.loop import PLAN_FILE, _product
    product = _product(product)
    path = os.path.join(state_dir or os.path.join(env.ASF_HOME, 'state', product.name), PLAN_FILE)
    data = None
    if not live:
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
    if data is not None:
        record = ports.record if ports else P.RealRecord(product)
        text = render(*rows_from_plan(data, record), idle=data.get('idle'),
                      notes=data.get('notes')) \
            + '\n\n(the tick of %s; --live for now)' \
            % data.get('at', '?')
        out(text)
        return text
    ports = ports or P.real_ports(product)
    config = config or P.config_for(product)
    stuck, counts, sessions, idle, notes = rows(ports, config, with_idle=True, with_notes=True)
    text = render(stuck, counts, sessions, idle=idle, notes=notes)
    out(text)
    return text
