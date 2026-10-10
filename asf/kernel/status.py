"""asf.kernel.status — the kernel's status table (ASF 0.2).

Three blocks, read-only: the Stuck items first (reason, owner, how many items wait on each, and
how long it has been stuck), then the count of items per state, then the active sessions — and,
when any item has one, a fourth: the notes (a question a session asked while its work moved on). The
states are the last tick's plan (``state/<product>/kernel-plan.json``, written by every applied
tick), so the table costs no network call; ``live=True`` (``--live``), or no plan on disk yet,
decides afresh on the facts now (no action is applied) — unless the plan is under one tick
(``kernel.tick.interval_s``) old: then those facts are now's, and no GitHub call is spent. A Stuck item's age is from its card's
``kernel_stuck_since``, ``-`` until a tick has recorded it. The biggest wait
(:func:`asf.kernel.waits.biggest_line`, from the wait ledger) heads the table, after the
"Needs you" block when there is one.

The kernel resolves every Stuck it can by itself (:mod:`asf.kernel.decide`: "Stuck never sits");
what is left on the operator is a question for a person. When any operator-owned Stuck is at least
``kernel.stuck.escalate_after_h`` old (0: from minute zero; an unknown age counts), a "Needs you"
block with each one's age leads the table (:func:`needs_you`).
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


def age_hours(age):
    """The hours an :func:`_age` cell says (``5h``, ``3d``), or None for ``-``."""
    text = str(age or '')
    try:
        return float(text[:-1]) * (24 if text.endswith('d') else 1) if text[-1:] in 'hd' else None
    except ValueError:
        return None


def needs_you(stuck, after_h=0):
    """The operator-owned rows of ``stuck`` (:func:`rows`) at least ``after_h`` hours old (an
    unknown age counts), oldest first: the questions only a person can answer."""
    out = []
    for row in stuck:
        if row[2] != 'operator':
            continue
        h = age_hours(row[4])
        if h is None or h >= (after_h or 0):
            out.append(row)
    return sorted(out, key=lambda r: (-(age_hours(r[4]) or 0), r[0]))


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


def render(stuck, counts, sessions, idle=None, notes=None, needs_after_h=None, head=''):
    """The three blocks as markdown tables (the console draws them as boxes), the idle alarm
    (:func:`asf.kernel.loop.idle_line`) first when it is raised, the notes last when any. With
    ``needs_after_h`` (hours) set, a "Needs you" block (:func:`needs_you`) leads when any
    operator-owned Stuck is that old; ``head`` (the biggest wait) comes right after it."""
    from asf.kernel.loop import idle_line
    out = []
    asks = needs_you(stuck, needs_after_h) if needs_after_h is not None else []
    if asks:
        out += ['## Needs you', '', '| item | age | question |', '| --- | --- | --- |']
        out += ['| %s | %s | %s |' % (iid, age, _cell(reason, 120))
                for iid, reason, _owner, _n, age in asks]
        out.append('')
    out += [head.rstrip('\n'), ''] if head else []
    out += [idle_line(idle), ''] if idle else []
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
    from asf.kernel import waits
    product = _product(product)
    path = os.path.join(state_dir or os.path.join(env.ASF_HOME, 'state', product.name), PLAN_FILE)
    try:
        top = waits.biggest_line(waits.summary(product, state_dir=state_dir)) + '\n\n'
    except Exception as e:  # noqa: BLE001 — a bad ledger never hides the status
        top = 'biggest wait: unreadable — %s\n\n' % (str(e) or type(e).__name__)
    try:
        from asf.kernel import mainmoves
        alarm = mainmoves.alarm_line(os.path.dirname(path))
        top = (alarm + '\n\n' if alarm else '') + top
    except Exception:  # noqa: BLE001 — the alarm never hides the status
        pass
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if live and not (isinstance(data, dict) and _under_one_tick(data.get('at'), product)):
        data = None
    if data is not None:
        record = ports.record if ports else P.RealRecord(product)
        text = render(*rows_from_plan(data, record), idle=data.get('idle'),
                      notes=data.get('notes'), needs_after_h=_needs_after(product),
                      head=top) \
            + ('\n\n(the tick of %s, under one tick old: no GitHub read)' if live
               else '\n\n(the tick of %s; --live for now)') % data.get('at', '?')
        out(text)
        return text
    ports = ports or P.real_ports(product)
    config = config or P.config_for(product, github=ports.github)
    stuck, counts, sessions, idle, notes = rows(ports, config, with_idle=True, with_notes=True)
    text = render(stuck, counts, sessions, idle=idle, notes=notes,
                  needs_after_h=_needs_after(product), head=top)
    out(text)
    return text


def _under_one_tick(at, product, now=None):
    """Whether a plan written at ``at`` is under one tick (``kernel.tick.interval_s``) old:
    ``--live`` then shows it — the facts now are the ones that tick read — and spends no GitHub
    call (the console's every-10-minutes ``--live`` cost a full tick's reads each time)."""
    try:
        then = datetime.datetime.strptime(str(at), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
        interval = float(product.kernel['tick']['interval_s'])
    except Exception:  # noqa: BLE001 — an unknown age is not fresh
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return 0 <= (now - then).total_seconds() <= interval


def _needs_after(product):
    """``kernel.stuck.escalate_after_h`` of ``product`` (0 when its block cannot be read)."""
    try:
        return float(product.kernel['stuck']['escalate_after_h'])
    except Exception:  # noqa: BLE001 — the table never fails on a setting
        return 0.0
