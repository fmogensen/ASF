"""asf.kernel.waits — every wait is measured (ASF 0.2).

Each applied tick appends one compact record per Task or Bug whose state or wait class changed
to ``state/<product>/kernel-waits.jsonl`` (:func:`record`)::

    {"item": "T-0012", "from_state": "review", "to_state": "landing", "reason": "ci",
     "at": "2026-10-10T09:12:00Z", "why": "PR #41: build, test"}

An item whose state and class are unchanged writes nothing, so the ledger holds the start of
every spell and nothing else. ``reason`` is the item's wait class (:data:`CLASSES`):

- ``seat``: Ready, no seat has taken it yet
- ``replan``: a Feature whose merged plan the record refused to mint (``Facts.plan_refusals``),
  New or Ready for its re-plan session (the ledger follows such a Feature until its Tasks exist)
- ``ci``: its PR's required checks are still running
- ``review``: its PR awaits a verdict
- ``train``: approved and behind, queued for an update (the merge train; a strict ruleset only)
- ``merge``: approved and green, waiting on GitHub to merge it
- ``conflict``: its PR conflicts with the base
- ``stuck:<owner>``: Stuck, on that owner's move (``loop``, ``session``, ``ci``, ``operator``)
- ``after``: New, waiting on a dependency (an ``after:`` edge, a spec, a rank)
- ``parked``: ``priority: later`` on it or an ancestor

``building`` (a session holds it) and ``done`` are recorded too, so a spell ends, but they are no
wait. An item that leaves the record gets ``gone``.

:func:`report` folds the ledger (and reads the last plan for the current reasons, never GitHub)
into the ``asf kernel waits`` view: per class the count now, p50/p90/max time-in-class over the
window and the item-hours spent; the ten oldest current waits; and the biggest wait — the class
with the most item-hours over the last two hours, with its oldest item. Each class has a target
(``kernel.waits.targets``, :data:`asf.kernel.settings.WAIT_TARGETS`); a current wait older than
its class's target is flagged ⚠ and counted on the tick's summary line.
"""
import collections
import datetime
import json
import os

from asf.kernel.model import State
from asf.kernel.settings import WAIT_CLASSES

LEDGER_FILE = 'kernel-waits.jsonl'

#: the wait classes (``stuck`` is written ``stuck:<owner>``), in display order
CLASSES = WAIT_CLASSES

#: recorded so a spell ends, but no wait
NOT_WAITS = ('building', 'done', 'gone')

#: the item types the ledger follows (a Feature's or a Story's state is derived from these)
FOLLOWED = ('task', 'bug')

#: the window of the "biggest wait" line
BIGGEST_WINDOW = datetime.timedelta(hours=2)

#: the default window of ``asf kernel waits``
DEFAULT_WINDOW = datetime.timedelta(hours=24)

#: the current waits ``asf kernel waits`` lists
TOP = 10

#: the most characters of a record's ``why``
WHY_MAX = 160

FLAG = '⚠'


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(text):
    try:
        return datetime.datetime.strptime(str(text), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _now(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def target_key(cls):
    """The ``kernel.waits.targets`` key of wait class ``cls`` (``stuck:ci`` -> ``stuck``)."""
    return str(cls).split(':', 1)[0]


def is_wait(cls):
    return bool(cls) and cls not in NOT_WAITS


# -- classify -------------------------------------------------------------------------------------

def _open_pr(iid, facts, config=None):
    from asf.kernel.decide import item_pr
    return item_pr(iid, facts, config)


def _required(name, config):
    from asf.kernel.decide import required
    return required(name, config)


def classify(iid, state, stuck, facts, config, notes=None):
    """``(wait class, why)`` of item ``iid`` in ``state`` (``stuck``: its :class:`Stuck`)."""
    if state is State.DONE:
        return 'done', ''
    if state is State.BUILDING:
        return 'building', ''
    if state is State.STUCK:
        return 'stuck:%s' % ((stuck.owner if stuck else '') or 'operator'), \
            (stuck.reason if stuck else '')
    if state is State.PARKED:
        return 'parked', ''
    refused = (getattr(facts, 'plan_refusals', None) or {}).get(iid)
    if refused and state in (State.NEW, State.READY):
        return 'replan', refused
    if state is State.NEW:
        it = facts.items.get(iid)
        return 'after', ', '.join(it.after) if it and it.after else ''
    if state is State.READY:
        return 'seat', ''
    pr = _open_pr(iid, facts, config)
    if pr is None:
        return ('review' if state is State.REVIEW else 'merge'), ''
    if pr.conflicting:
        return 'conflict', 'PR #%d conflicts' % pr.number
    if state is State.REVIEW:
        return 'review', 'PR #%d' % pr.number
    if pr.behind and getattr(facts, 'strict', True):  # not strict: GitHub merges it behind
        note = [n for n in (notes or {}).get(iid, []) if 'merge train' in n]
        return 'train', 'PR #%d %s' % (pr.number, note[-1] if note else 'behind')
    running = [c.name for c in pr.checks if c.status != 'completed' and _required(c.name, config)]
    if running:
        return 'ci', 'PR #%d: %s' % (pr.number, ', '.join(sorted(set(running))))
    return 'merge', 'PR #%d green' % pr.number


def current(plan, facts, config):
    """``{item: (state value, wait class, why)}`` for every followed item the plan judged — a
    Task or Bug, or a Feature whose merged plan the record refused (``Facts.plan_refusals``)."""
    out = {}
    notes = getattr(plan, 'notes', None) or {}
    refused = getattr(facts, 'plan_refusals', None) or {}
    for iid, (state, stuck) in plan.states.items():
        it = facts.items.get(iid)
        if it is None or (it.type not in FOLLOWED and iid not in refused):
            continue
        cls, why = classify(iid, state, stuck, facts, config, notes)
        out[iid] = (state.value, cls, why)
    return out


# -- the ledger -----------------------------------------------------------------------------------

def read_ledger(state_dir):
    """Every ledger record, oldest first (an unreadable line is skipped)."""
    path = os.path.join(state_dir, LEDGER_FILE)
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('item') and _parse(rec.get('at')):
                    out.append(rec)
    except OSError:
        pass
    return out


def fold(records):
    """``{item: last record}`` — each item's current spell (``gone`` items left out)."""
    last = {}
    for rec in records:
        last[rec['item']] = rec
    return {iid: r for iid, r in last.items() if r.get('reason') != 'gone'}


def changes(previous, now_items, at):
    """The records for ``now_items`` (:func:`current`) against ``previous`` (:func:`fold`)."""
    out = []
    for iid in sorted(now_items):
        state, cls, why = now_items[iid]
        old = previous.get(iid)
        if old and old.get('to_state') == state and old.get('reason') == cls:
            continue
        rec = {'item': iid, 'from_state': old.get('to_state') if old else None,
               'to_state': state, 'reason': cls, 'at': at}
        why = ' '.join(str(why or '').split())
        if why:
            rec['why'] = why if len(why) <= WHY_MAX else why[:WHY_MAX - 1] + '…'
        out.append(rec)
    for iid in sorted(set(previous) - set(now_items)):
        out.append({'item': iid, 'from_state': previous[iid].get('to_state'), 'to_state': 'gone',
                    'reason': 'gone', 'at': at})
    return out


def record(state_dir, plan, facts, config, now=None, write=True):
    """Append this tick's changes to the ledger (``write=False``: only compute them). Returns
    ``(records written, every record)``."""
    records = read_ledger(state_dir)
    new = changes(fold(records), current(plan, facts, config), _iso(_now(now)))
    if new and write:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, LEDGER_FILE), 'a', encoding='utf-8') as f:
            for rec in new:
                f.write(json.dumps(rec, sort_keys=True, separators=(',', ':')) + '\n')
    return new, records + new


# -- the measures ---------------------------------------------------------------------------------

def spells(records, now):
    """``[(item, class, start, end or None)]``: every spell the ledger holds, oldest first."""
    by_item = collections.defaultdict(list)
    for rec in records:
        by_item[rec['item']].append(rec)
    out = []
    for iid, recs in by_item.items():
        recs.sort(key=lambda r: r['at'])
        for k, rec in enumerate(recs):
            end = _parse(recs[k + 1]['at']) if k + 1 < len(recs) else None
            out.append((iid, rec.get('reason') or '', _parse(rec['at']), end))
    return sorted(out, key=lambda s: (s[2], s[0]))


#: the wait classes a live process is bounded by (:func:`bounds`)
BOUNDED = ('building', 'review', 'ci')

#: the finished spells a class needs before its p90 bounds a live process (else the fallback knob)
MIN_SAMPLES = 20


def bounds(records, now=None, since=None, min_samples=MIN_SAMPLES):
    """``{class: p90 seconds}`` of the finished spells (:func:`spells`) of each class in
    :data:`BOUNDED` that ended in the window (``since``, default the last 24 h) — only a class
    with at least ``min_samples`` of them: a live process past its bound is a stall
    (``Facts.bounds``)."""
    now = _now(now)
    since = since or now - DEFAULT_WINDOW
    got = collections.defaultdict(list)
    for _iid, cls, start, end in spells(records, now):
        if cls in BOUNDED and end is not None and end >= since:
            got[cls].append((end - start).total_seconds())
    return {cls: percentile(v, 90) for cls, v in got.items() if len(v) >= max(1, min_samples)}


def percentile(values, p):
    """The nearest-rank ``p``-th percentile of ``values`` (None when empty)."""
    if not values:
        return None
    vals = sorted(values)
    k = max(0, min(len(vals) - 1, int(-(-p * len(vals) // 100)) - 1))
    return vals[k]


def _overlap(start, end, lo, hi):
    return max(0.0, (min(end, hi) - max(start, lo)).total_seconds())


def item_hours(all_spells, lo, hi):
    """``{class: item-seconds}`` spent in each wait class between ``lo`` and ``hi``."""
    out = collections.Counter()
    for _iid, cls, start, end in all_spells:
        if is_wait(cls):
            out[cls] += _overlap(start, end or hi, lo, hi)
    return out


def over_target(cls, age_s, targets):
    t = (targets or {}).get(target_key(cls))
    return t is not None and age_s > t


def report(records, now=None, since=None, targets=None, live=None, plan=None):
    """The ``asf kernel waits`` view as a dict: ``classes`` (per class: now, p50, p90, max,
    item_s, target), ``top`` (the oldest current waits), ``over`` (how many are over target),
    ``biggest`` (``{'class', 'item_s', 'item', 'age_s'}`` or None). ``live`` replaces the ledger's
    current spells with ``{item: (state, class, why)}``; ``plan`` (the last saved plan) supplies
    the current reasons."""
    now = _now(now)
    since = since or now - DEFAULT_WINDOW
    targets = targets or {}
    all_spells = spells(records, now)
    cur = {iid: (r.get('reason'), _parse(r['at']), r.get('why', ''))
           for iid, r in fold(records).items()}
    if live is not None:
        cur = {iid: (cls, cur[iid][1] if iid in cur and cur[iid][0] == cls else now, why)
               for iid, (_state, cls, why) in live.items()}
    plan_states = (plan or {}).get('states') or {}
    plan_notes = (plan or {}).get('notes') or {}
    waiting = []
    for iid, (cls, start, why) in cur.items():
        if not is_wait(cls):
            continue
        reason = (plan_states.get(iid) or {}).get('reason') or why
        if cls == 'train' and plan_notes.get(iid):
            reason = plan_notes[iid][-1]
        age = (now - start).total_seconds()
        waiting.append({'item': iid, 'class': cls, 'age_s': age, 'reason': reason,
                        'over': over_target(cls, age, targets)})
    waiting.sort(key=lambda w: (-w['age_s'], w['item']))

    durations = collections.defaultdict(list)
    for _iid, cls, start, end in all_spells:
        if is_wait(cls) and (end is None or end >= since):
            durations[cls].append(((end or now) - start).total_seconds())
    hours = item_hours(all_spells, since, now)
    counts = collections.Counter(w['class'] for w in waiting)
    names = sorted(set(durations) | set(counts),
                   key=lambda c: (CLASSES.index(target_key(c)) if target_key(c) in CLASSES
                                  else len(CLASSES), c))
    classes = [{'class': c, 'now': counts.get(c, 0), 'p50': percentile(durations[c], 50),
                'p90': percentile(durations[c], 90),
                'max': max(durations[c]) if durations[c] else None,
                'item_s': hours.get(c, 0.0), 'target': targets.get(target_key(c)),
                'over': sum(1 for w in waiting if w['class'] == c and w['over'])}
               for c in names]

    recent = item_hours(all_spells, now - BIGGEST_WINDOW, now)
    biggest = None
    if recent and max(recent.values()) > 0:
        cls = max(sorted(recent), key=lambda c: recent[c])
        oldest = next((w for w in waiting if w['class'] == cls), None)
        biggest = {'class': cls, 'item_s': recent[cls],
                   'item': oldest['item'] if oldest else None,
                   'age_s': oldest['age_s'] if oldest else None,
                   'over': bool(oldest and oldest['over'])}
    return {'at': _iso(now), 'since': _iso(since), 'classes': classes, 'top': waiting[:TOP],
            'waiting': len(waiting), 'over': sum(1 for w in waiting if w['over']),
            'biggest': biggest}


# -- rendering ------------------------------------------------------------------------------------

def dur(seconds):
    """``45s``, ``12m``, ``3.2h``, ``2.1d`` ('-' for None)."""
    if seconds is None:
        return '-'
    s = float(seconds)
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%dm' % (s / 60)
    if s < 48 * 3600:
        return '%.1fh' % (s / 3600)
    return '%.1fd' % (s / 86400)


def biggest_line(rep):
    """``biggest wait (2h): ci — 6.3 item-h; oldest T-0012 2.1h ⚠`` (or that nothing waited)."""
    b = rep.get('biggest')
    if not b:
        return 'biggest wait (2h): none — no item waited'
    line = 'biggest wait (2h): %s — %.1f item-h' % (b['class'], b['item_s'] / 3600)
    if b.get('item'):
        line += '; oldest %s %s%s' % (b['item'], dur(b['age_s']), ' ' + FLAG if b['over'] else '')
    return line


def tick_line(rep):
    """The tick summary's line: ``over-target waits: N, biggest: <class>``."""
    b = rep.get('biggest')
    return 'over-target waits: %d, biggest: %s' % (rep.get('over', 0), b['class'] if b else '-')


def _cell(text, width=80):
    text = ' '.join(str(text or '').split()).replace('|', '/')
    return text if len(text) <= width else text[:width - 1] + '…'


def render(rep):
    out = [biggest_line(rep), '',
           '## Waits since %s (%d waiting, %d over target)' % (rep['since'], rep['waiting'],
                                                              rep['over']), '',
           '| class | now | p50 | p90 | max | item-h | target |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    out += ['| %s | %d%s | %s | %s | %s | %.1f | %s |' % (
        c['class'], c['now'], (' ' + FLAG + str(c['over'])) if c['over'] else '', dur(c['p50']),
        dur(c['p90']), dur(c['max']), c['item_s'] / 3600,
        dur(c['target']) if c['target'] is not None else '-')
        for c in rep['classes']] or ['| - | 0 | - | - | - | 0.0 | - |']
    out += ['', '## Oldest waits', '', '| item | class | age | reason |', '| --- | --- | --- | --- |']
    out += ['| %s | %s | %s%s | %s |' % (w['item'], w['class'], dur(w['age_s']),
                                         ' ' + FLAG if w['over'] else '', _cell(w['reason']))
            for w in rep['top']] or ['| - | - | - | nothing waits |']
    return '\n'.join(out)


def _state_dir(product, state_dir):
    from asf import env
    return state_dir or os.path.join(env.ASF_HOME, 'state', product.name)


def _targets(product):
    try:
        return product.kernel['waits']['targets']
    except (AttributeError, KeyError, TypeError):
        from asf.kernel import settings
        return settings.read(None)['waits']['targets']


def summary(product, state_dir=None, now=None):
    """The :func:`report` of ``product``'s ledger (no plan, no network) — the status line's."""
    from asf.kernel.loop import _product
    product = _product(product)
    return report(read_ledger(_state_dir(product, state_dir)), now=now, targets=_targets(product))


def waits(product, since=None, live=False, ports=None, config=None, state_dir=None, out=print,
          now=None):
    """``asf kernel waits``: print :func:`render` of the product's ledger and last plan
    (``live``: the current classes from facts read now, which reads GitHub; nothing is written)."""
    from asf.kernel.loop import PLAN_FILE, _product
    from asf.kernel import settings
    product = _product(product)
    sd = _state_dir(product, state_dir)
    t0 = settings.parse_time(since) if since else None
    if since and t0 is None:
        raise ValueError('--since must be an ISO-8601 time, not %r' % (since,))
    plan = None
    try:
        with open(os.path.join(sd, PLAN_FILE), encoding='utf-8') as f:
            plan = json.load(f)
    except (OSError, ValueError):
        plan = None
    cur = None
    if live:
        from asf import mutation_guard
        from asf.kernel import ports as P
        from asf.kernel.decide import decide
        from asf.kernel.facts import read_facts
        ports = ports or P.real_ports(product)
        config = config or P.config_for(product, github=ports.github)
        with mutation_guard.active():
            facts = read_facts(ports)
            p = decide(facts, config)
        cur = current(p, facts, config)
    rep = report(read_ledger(sd), now=now, since=t0, targets=_targets(product), live=cur,
                 plan=None if live else plan)
    text = render(rep)
    if not live and plan:
        text += '\n\n(the ledger and the tick of %s; --live for now)' % plan.get('at', '?')
    out(text)
    return rep
