"""asf.record.plan_order — the order a plan states, as ``after:`` on its Task cards.

A plan states its order in one of a few shapes: an ``after: Task 1, Task 2`` line under a
``### Task N:`` heading, a sentence in that section (``Runs after Task 1``, ``depends on Task 3``,
``Independent of Tasks 1 and 2``), a ``| wave | Task | … | runs after |`` table, or a
``Wave order: (1, 2) → 3`` line. The minter used to drop all of them, so every Task of a plan
launched at once and each successor's coder found its predecessor's surface missing and wrote
nothing. This module reads the order and turns it into card ids:

- a Task's own statement wins (its ``after:`` line, then its prose, then its table row);
- otherwise a Task in wave N runs after every Task of wave N-1;
- a plan that states no order at all leaves its Tasks unordered (parallel), as before.

Three callers: the minter (new cards), :func:`backfill` (open cards minted before this, run by the
tick's record step, idempotent, written through the parser), and :func:`overlay` (the wave's
defence in depth: a Task whose card lacks ``after:`` still waits on its plan's predecessors).
"""
import re

TASK_REF_RE = re.compile(r'\b(?:Tasks?|T)\s*(\d+)((?:\s*(?:,|and|&|or|\+)\s*(?:Task\s*)?\d+)*)',
                         re.IGNORECASE)
AFTER_LINE_RE = re.compile(r'^\s*after\s*:\s*(.*)$', re.IGNORECASE | re.MULTILINE)
DEPENDS_RE = re.compile(r'\b(?:runs\s+after|starts\s+after|comes\s+after|lands\s+after|'
                        r'depends\s+on|builds\s+on|blocked\s+(?:on|by))\s+'
                        r'(Tasks?\s*\d+(?:\s*(?:,|and|&|\+)\s*(?:Task\s*)?\d+)*)', re.IGNORECASE)
INDEPENDENT_RE = re.compile(r'\b(?:independent\s+of|depends\s+on\s+(?:nothing|neither|none)|'
                            r'needs\s+nothing\s+from)\b', re.IGNORECASE)
NONE_RE = re.compile(r'^\s*(?:none|nothing|-+|—|–|\[\s*\])?\s*(?:#.*)?$', re.IGNORECASE)
WAVE_ORDER_RE = re.compile(r'^.*\bwave\s+order\b[^:\n]*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
ARROW_RE = re.compile(r'→|->|⟶|=>')


def _nums(text):
    """Every Task number a ``Task 1, 2 and Task 3`` phrase names, in order."""
    out = []
    for m in TASK_REF_RE.finditer(text or ''):
        out.append(m.group(1))
        out += re.findall(r'\d+', m.group(2) or '')
    return [int(n) for n in dict.fromkeys(out)]


def _explicit(body):
    """The predecessors a Task's own section states: a list (maybe empty), or None if silent."""
    m = AFTER_LINE_RE.search(body or '')
    if m and not re.search(r'\b[A-Z]-\d', m.group(1)):  # card ids are not plan Task numbers
        value = m.group(1)
        nums = _nums(value) or [int(n) for n in re.findall(r'\b\d+\b', value.split('#')[0])]
        if nums:
            return nums
        if NONE_RE.match(value):
            return []
    deps = []
    for dm in DEPENDS_RE.finditer(body or ''):
        deps += _nums(dm.group(1))
    if deps:
        return list(dict.fromkeys(deps))
    return None


def _independent(body):
    return bool(INDEPENDENT_RE.search(body or ''))


def _table(plan_text):
    """({task: wave}, {task: [after]}) from a markdown table whose header has ``wave`` and ``task``."""
    waves, after = {}, {}
    lines = (plan_text or '').splitlines()
    i = 0
    while i < len(lines):
        head = [c.strip().lower() for c in lines[i].strip().strip('|').split('|')]
        if lines[i].lstrip().startswith('|') and 'wave' in head and any(c.startswith('task') for c in head):
            wcol = head.index('wave')
            tcol = next(k for k, c in enumerate(head) if c.startswith('task'))
            acol = next((k for k, c in enumerate(head) if 'after' in c), None)
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                i += 1
                if len(cells) <= max(wcol, tcol) or set(cells[wcol]) <= set('-: '):
                    continue
                wm = re.search(r'\d+', cells[wcol])
                tn = _nums(cells[tcol])
                if not wm or not tn:
                    continue
                waves[tn[0]] = int(wm.group())
                if acol is not None and acol < len(cells):
                    after[tn[0]] = _nums(cells[acol])
            continue
        i += 1
    return waves, after


def _wave_order(plan_text):
    """{task: wave} from a ``Wave order: (1, 2, 3) → (4, 5) → 6`` line."""
    m = WAVE_ORDER_RE.search(plan_text or '')
    if not m:
        return {}
    groups = ARROW_RE.split(m.group(1))
    if len(groups) < 2:
        return {}
    out = {}
    for w, g in enumerate(groups, 1):
        for n in re.findall(r'\d+', g):
            out.setdefault(int(n), w)
    return out


def task_order(plan_text):
    """{task number: [predecessor task numbers]} for every ``### Task N:`` of the plan."""
    from asf.tick.migrate import plan_task_records
    records = plan_task_records(plan_text or '')
    nums = []
    bodies = {}
    for r in records:
        m = re.match(r'T(\d+)', r['tid'])
        if m:
            n = int(m.group(1))
            nums.append(n)
            bodies[n] = r['body']
    known = set(nums)
    waves, table_after = _table(plan_text)
    waves = waves or _wave_order(plan_text)
    out = {}
    for n in nums:
        preds = _explicit(bodies[n])
        if preds is None and n in table_after:
            preds = table_after[n]
        if preds is None and _independent(bodies[n]):
            preds = []
        if preds is None:
            w = waves.get(n)
            preds = sorted(k for k, v in waves.items() if w and v == w - 1)
        # an edge that would close a cycle is dropped: a cycle waits for ever, and says nothing
        out[n] = [p for p in preds if p in known and p != n and not _reaches(out, p, n)]
    return out


def _reaches(graph, start, goal):
    """True if ``goal`` is among ``start``'s predecessors, transitively, in ``graph`` so far."""
    seen, stack = set(), [start]
    while stack:
        k = stack.pop()
        if k == goal:
            return True
        if k not in seen:
            seen.add(k)
            stack.extend(graph.get(k, ()))
    return False


def task_ids(records, cards):
    """{task number: card id} — a card is its plan Task by title, else by position in id order."""
    by_title = {}
    for cid, meta in cards.items():
        by_title.setdefault((meta.get('title') or '').strip().lower(), cid)
    out = {}
    for r in records:
        m = re.match(r'T(\d+)', r['tid'])
        cid = by_title.get((r['title'] or '').strip().lower())
        if m and cid:
            out[int(m.group(1))] = cid
    if len(out) != len(records) and len(cards) == len(records):
        ordered = sorted(cards, key=lambda i: (len(i), i))
        out = {int(re.match(r'T(\d+)', r['tid']).group(1)): cid
               for r, cid in zip(records, ordered) if re.match(r'T(\d+)', r['tid'])}
    return out


def derived_after(plan_text, cards):
    """{card id: [predecessor card ids]} for the cards of one plan (``cards``: {id: meta})."""
    from asf.tick.migrate import plan_task_records
    records = plan_task_records(plan_text or '')
    ids = task_ids(records, cards)
    out = {}
    for n, preds in task_order(plan_text).items():
        cid = ids.get(n)
        if cid:
            out[cid] = [ids[p] for p in preds if p in ids]
    return out


def trunk_reader(product):
    """``read_plan(path)`` → the plan's text on ``origin/<main>`` of the product repo, or None."""
    from asf.evidence import evidence

    def read(path):
        return evidence.read_ref(f'origin/{product.main}:{path}', product=product)
    return read


def _plan_cards(items, plan_path):
    return {i: m for i, m in items.items()
            if m.get('type') == 'task' and ((m.get('links') or {}).get('plan') == plan_path)}


def backfill(root, read_plan, out=print, only=None):
    """Write ``after:`` onto every open Task card of a plan that has none. Idempotent: a card that
    already carries ``after:`` (even ``[]``) is left alone. ``read_plan(path)`` → text or None.
    Writes go through :func:`asf.record.setfield.set_typed` (the parser round-trip).
    Returns {card id: [after ids]} for the cards written."""
    from asf.record.core import canonicalize, load_items
    from asf.record.setfield import set_typed
    from asf.feeder.rows import DONE_STATES
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    metas = {i: r['meta'] for i, r in canonical.items()}
    plans = sorted({(m.get('links') or {}).get('plan') for m in metas.values()
                    if m.get('type') == 'task' and (m.get('links') or {}).get('plan')
                    and (only is None or m.get('id') in only)})
    written = {}
    for path in plans:
        cards = _plan_cards(metas, path)
        todo = [i for i, m in cards.items() if 'after' not in m and m.get('state', 'New') not in DONE_STATES
                and (only is None or i in only)]
        if not todo:
            continue
        text = read_plan(path)
        if not text:
            continue
        order = derived_after(text, cards)
        for cid in sorted(todo):
            preds = order.get(cid)
            if not preds:
                continue
            err = set_typed(canonical[cid], {'after': preds})
            if err:
                out(f'plan-order: {cid}: {err}')
                continue
            written[cid] = preds
            out(f"plan-order: {cid}: after: [{', '.join(preds)}] (from {path})")
    return written


def overlay(items, read_plan):
    """The wave's guard: ``items`` with a derived ``after:`` on every open Task that lacks one.
    Never writes; a plan that cannot be read changes nothing. Returns a new dict."""
    from asf.feeder.rows import DONE_STATES
    plans = {}
    for i, m in items.items():
        path = (m.get('links') or {}).get('plan')
        if (m.get('type') == 'task' and path and 'after' not in m
                and m.get('state', 'New') not in DONE_STATES):
            plans.setdefault(path, []).append(i)
    if not plans:
        return items
    items = dict(items)
    for path, ids in plans.items():
        try:
            text = read_plan(path)
        except Exception:  # the guard never breaks the wave
            text = None
        if not text:
            continue
        order = derived_after(text, _plan_cards(items, path))
        for i in ids:
            if order.get(i):
                items[i] = dict(items[i], after=order[i])
    return items
