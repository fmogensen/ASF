"""asf.scorecard.loop — the standing value loop, run by the tick's daily step and by hand.

Once a day, per product (:func:`daily`):

1. **measure** — :mod:`asf.scorecard.score` over the facts; this ISO week's row is upserted into
   ``state/<product>/scorecard.jsonl`` so the trend survives the streams being trimmed;
2. **diagnose** — :func:`asf.scorecard.diagnose.causes` over the last ``window_days``; a cause
   over its threshold is filed **once**, keyed by its cause key in
   ``state/<product>/scorecard-causes.json``, as an inbox card carrying its numbers and the
   marker line ``scorecard-cause: <key> #<n>``;
3. **verify** — once the card that marker names has landed and ``verify_weeks`` have passed, the
   cause's number over the ``verify_weeks`` before the landing is compared with the same span
   after it. The verdict — ``moved`` or ``didn't move``, with both numbers — goes onto the card's
   History; a card that did not move is reopened as a new inbox card (``#<n+1>``) with the numbers.

Cards and History lines never go straight into a record: they are queued in the *target*
product's ``state/<target>/scorecard-queue.jsonl`` and that product's own daily step writes them
into its own record clone (:func:`drain`), which its tick then commits — so no tick ever writes
another product's record. A ``factory`` cause (the factory's own process) targets the configured
product whose repo is the factory's source; a ``product`` cause (its code, its CI) its own record.
``improve: {scorecard: {file_to: <product>}}`` overrides both.

Settings (``improve: {scorecard: {…}}`` in the product file, every key optional): ``thresholds``
(see :data:`asf.scorecard.diagnose.THRESHOLDS`), ``window_days`` (14), ``verify_weeks`` (2),
``min_move`` (0.2: the number must fall by a fifth to count as moved), ``max_per_run`` (3: a
day files the causes furthest over their threshold, the rest on the days after), ``file_to``,
``epic``.
"""
import datetime
import glob
import json
import os
import re

from asf.scorecard import diagnose, score
from asf.scorecard.facts import iso, load, load_cards, state_file, to_dt

DEFAULTS = {'window_days': 14, 'verify_weeks': 2, 'min_move': 0.2, 'max_per_run': 3}
MARKER = 'scorecard-cause:'
SNAPSHOTS = 'scorecard.jsonl'
CAUSES = 'scorecard-causes.json'
QUEUE = 'scorecard-queue.jsonl'
#: The daily line's lane split looks back this many days (the experiment's week).
LANE_DAYS = 7


# ------------------------------------------------------------ settings --

def settings(product):
    improve = getattr(product, 'improve', None) or {}
    block = improve.get('scorecard') if isinstance(improve, dict) else None
    block = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = block.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    out['thresholds'] = block.get('thresholds') if isinstance(block.get('thresholds'), dict) else {}
    out['file_to'] = block.get('file_to') or None
    out['epic'] = block.get('epic') or (improve.get('epic') if isinstance(improve, dict) else None)
    return out


def configured_products():
    from asf import env
    names = []
    for path in sorted(glob.glob(os.path.join(env.ASF_HOME, 'products', '*.yaml'))):
        names.append(os.path.basename(path)[:-len('.yaml')])
    return names


def factory_product(names=None, load_product=None, is_source=None):
    """The configured product whose repo holds the factory's own source, or ``None``."""
    from asf import env
    from asf.drift import is_factory_source
    load_product = load_product or env.load_product
    is_source = is_source or is_factory_source
    for name in names if names is not None else configured_products():
        try:
            p = load_product(name)
        except Exception:  # noqa: BLE001 — a broken product file is not the factory
            continue
        if p.repo_dir and is_source(p.repo_dir):
            return name
    return None


def target_of(product, scope, cfg=None, factory=None):
    cfg = cfg or settings(product)
    if cfg.get('file_to'):
        return cfg['file_to']
    if scope == diagnose.FACTORY:
        found = factory() if callable(factory) else factory
        if found:
            return found
    return product.name


# ------------------------------------------------------------ JSON files --

def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def _read_lines(path):
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _write_lines(path, rows):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + '\n')
    os.replace(tmp, path)


# ------------------------------------------------------------ snapshots --

def snapshot(product, facts, clutter=None, causes=(), path=None):
    """Upsert this ISO week's row into ``scorecard.jsonl``; returns the row."""
    path = path or state_file(product, SNAPSHOTS)
    row = score.weekly(facts, 1)[0]
    row.update(product=product.name, as_of=facts.as_of, clutter=clutter or facts.clutter,
               causes=sorted(c.key for c in causes))
    rows = [r for r in _read_lines(path) if r.get('week') != row['week']]
    rows.append(row)
    rows.sort(key=lambda r: r.get('week') or '')
    _write_lines(path, rows)
    return row


def snapshots(product, path=None):
    return _read_lines(path or state_file(product, SNAPSHOTS))


# ------------------------------------------------------------ the queue --

def enqueue(target, entry, state_dir=None):
    from asf import env
    d = state_dir or env.state_dir(target)
    with open(os.path.join(d, QUEUE), 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')


def _find_marker(cards, marker):
    for iid, c in sorted(cards.items()):
        if marker in (c.get('text') or ''):
            return iid
    return None


def _slug(title):
    return (re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or 'card')[:90]


def drain(product, root, out=print, state_dir=None):
    """Write every queued card and History line into ``root`` (this product's record clone).
    A History line whose card is not minted yet stays queued. Returns ``(cards, lines)`` written."""
    from asf import env
    from asf.record import frontmatter
    from asf.record.ingest import append_history_lines
    path = os.path.join(state_dir or env.state_dir(product), QUEUE)
    entries = _read_lines(path)
    if not entries:
        return 0, 0
    intake = getattr(getattr(product, 'conventions', None), 'intake_dir', None) or 'inbox'
    by_id = None
    left, n_cards, n_lines = [], 0, 0
    for e in entries:
        if e.get('kind') == 'inbox':
            by_id = by_id if by_id is not None else load_cards(root)
            if _find_marker(by_id, e['marker']) or _inbox_has(root, intake, e['marker']):
                continue
            d = os.path.join(root, intake)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, e['name']), 'w', encoding='utf-8') as f:
                f.write(e['text'])
            n_cards += 1
        elif e.get('kind') == 'history':
            by_id = by_id if by_id is not None else load_cards(root)
            iid = e.get('card') or _find_marker(by_id, e.get('marker') or '\0')
            rec = _card_path(root, iid) if iid else None
            if not rec:
                left.append(e)
                continue
            with open(rec, encoding='utf-8') as f:
                text = f.read()
            meta, body = frontmatter.parse(text, path=rec)
            if e['line'] in body:
                continue
            with open(rec, 'w', encoding='utf-8') as f:
                f.write(frontmatter.render(meta, append_history_lines(body, [e['line']])))
            n_lines += 1
    _write_lines(path, left)
    if n_cards or n_lines:
        out(f"scorecard: wrote {n_cards} card(s) and {n_lines} History line(s) into the record")
    return n_cards, n_lines


def _inbox_has(root, intake, marker):
    for p in glob.glob(os.path.join(root, intake, '**', '*.md'), recursive=True):
        try:
            with open(p, encoding='utf-8') as f:
                if marker in f.read():
                    return True
        except OSError:
            continue
    return False


def _card_path(root, iid):
    from asf.record.core import ITEM_FOLDERS
    for folder in ITEM_FOLDERS:
        p = os.path.join(root, folder, f'{iid}.md')
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------------------ filing --

def _scrub(text, root):
    try:
        from asf import redact
        pats = redact.default_patterns(root)
        return redact.scrub(text, pats) if pats else text
    except Exception:  # noqa: BLE001 — the scrub never blocks a filing
        return text


def epic_of(target, cfg=None, product=None):
    """The Epic a card filed into ``target``'s record hangs under: the filing product's
    ``improve.scorecard.epic`` when it files into its own record, else the target's scorecard or
    ``improve.epic``, else the target's ``conventions.default_bug_epic``; ``None`` when none is set
    (the groom then asks for one)."""
    if product is not None and target == product.name and cfg and cfg.get('epic'):
        return cfg['epic']
    from asf import env
    try:
        p = product if product is not None and target == product.name else env.load_product(target)
    except Exception:  # noqa: BLE001 — no target config: the groom asks for the Epic
        return None
    epic = settings(p).get('epic')
    if epic:
        return epic
    conv = getattr(p, 'conventions', None)
    return getattr(conv, 'default_bug_epic', None) if conv is not None else None


def card_text(cause, n, product_name, cfg, window_days, reopens=None, root=None, epic=None):
    """The inbox card for one cause: its title, its numbers, how it will be verified, its marker."""
    title = cause.title if not reopens else f"Reopen {reopens['card']}: {cause.title}"
    lines = [f'# {title}']
    if epic:
        lines.append(f"parent: {epic}")
    lines += ['', f'Filed by the scorecard loop for product {product_name} '
                  f'({cause.scope} cause, last {window_days} days).', '',
              f'Reading: {cause.value:g} {cause.unit} (threshold {cause.threshold:g}). {cause.detail}', '']
    if reopens:
        lines += [f"{reopens['card']} landed {reopens['landed'][:10]} and did not move this number: "
                  f"{reopens['before']} before, {reopens['after']} after, over "
                  f"{reopens['weeks']} weeks each side. Find the cause it missed.", '']
    lines += [f"Verify: the loop reads this number over the {cfg['verify_weeks']} weeks before this card "
              f"lands and the {cfg['verify_weeks']} weeks after; it must fall by "
              f"{cfg['min_move'] * 100:.0f} %, or the card is reopened with both numbers.", '',
              f'{MARKER} {cause.key} #{n}', '',
              '## Acceptance',
              f"- [ ] {cause.key} reads below {cause.threshold:g} {cause.unit} over "
              f"{cfg['verify_weeks']} weeks after landing", '']
    return _scrub('\n'.join(lines), root), f"scorecard-{_slug(cause.key)}-{n}.md"


def file_causes(product, found, cfg, as_of, state, factory=None, enqueue_fn=enqueue, root=None,
                epic_fn=epic_of):
    """File each cause in ``found`` not already in ``state`` (once per cause key). Returns keys filed."""
    filed = []
    for cause in found:
        if cause.key in state:
            continue
        if len(filed) >= cfg.get('max_per_run', DEFAULTS['max_per_run']):
            break           # the rest are still over tomorrow: a day files the worst few, not a flood
        target = target_of(product, cause.scope, cfg, factory)
        text, name = card_text(cause, 1, product.name, cfg, cfg['window_days'], root=root,
                               epic=epic_fn(target, cfg, product))
        marker = f'{MARKER} {cause.key} #1'
        enqueue_fn(target, {'kind': 'inbox', 'name': name, 'text': text, 'marker': marker})
        state[cause.key] = {'n': 1, 'marker': marker, 'target': target, 'scope': cause.scope,
                            'title': cause.title, 'filed': as_of[:10], 'baseline': cause.value,
                            'unit': cause.unit, 'threshold': cause.threshold,
                            'card': None, 'verdict': None, 'log': []}
        filed.append(cause.key)
    return filed


# ------------------------------------------------------------ verifying --

def _fmt(v):
    return 'no reading' if v is None else f'{v:g}'


def moved(before, after, min_move):
    """True when ``after`` is at least ``min_move`` below ``before`` (a zero stays zero, moved)."""
    if before is None or after is None:
        return None
    if before <= 0:
        return after <= 0
    return after <= before * (1 - min_move)


def verify(product, facts, cfg, state, cards_of, as_of, enqueue_fn=enqueue, root=None, epic_fn=epic_of):
    """For every filed cause whose card has landed ``verify_weeks`` ago: compare, record, reopen.
    ``cards_of(target)`` returns that target's cards (``{id: card}``). Returns ``[(key, verdict)]``."""
    now = to_dt(as_of)
    span = datetime.timedelta(weeks=cfg['verify_weeks'])
    done = []
    for key, entry in sorted(state.items()):
        if entry.get('verdict'):
            continue
        cards = cards_of(entry['target'])
        iid = entry.get('card') or _find_marker(cards, entry['marker'])
        if not iid or iid not in cards:
            continue
        entry['card'] = iid
        c = cards[iid]
        if c.get('removed'):
            entry['verdict'] = 'dropped'
            entry['log'].append(f"{as_of[:10]} {iid} removed — not verified")
            done.append((key, 'dropped'))
            continue
        landed = to_dt(c.get('landed'))
        if landed is None or now < landed + span:
            continue
        try:
            before = diagnose.metric(facts, key, landed - span, landed)
            after = diagnose.metric(facts, key, landed, landed + span)
        except KeyError:
            continue
        if key.startswith('clutter:'):
            before = entry.get('baseline')      # a clutter count has no history: the filing's reading
        verdict = moved(before, after, cfg['min_move'])
        if verdict is None:
            continue
        word = 'moved' if verdict else "didn't move"
        line = (f"- {as_of[:10]} scorecard: {key} {word} — {_fmt(before)} before, {_fmt(after)} after "
                f"({cfg['verify_weeks']} weeks each side of landing {c['landed'][:10]})")
        enqueue_fn(entry['target'], {'kind': 'history', 'card': iid, 'marker': entry['marker'],
                                     'line': line})
        entry['log'].append(line[2:])
        entry.update(verdict=word, verified=as_of[:10], before=before, after=after)
        done.append((key, word))
        if not verdict:
            n = entry['n'] + 1
            cause = diagnose.Cause(key, entry['scope'], after, entry['threshold'], entry['unit'],
                                   entry['title'], f"Still {_fmt(after)} {entry['unit']} after {iid} landed.")
            text, name = card_text(cause, n, product.name, cfg, cfg['verify_weeks'] * 7, root=root,
                                   epic=epic_fn(entry['target'], cfg, product),
                                   reopens={'card': iid, 'landed': c['landed'], 'before': _fmt(before),
                                            'after': _fmt(after), 'weeks': cfg['verify_weeks']})
            marker = f'{MARKER} {key} #{n}'
            enqueue_fn(entry['target'], {'kind': 'inbox', 'name': name, 'text': text, 'marker': marker})
            state[key] = {'n': n, 'marker': marker, 'target': entry['target'], 'scope': entry['scope'],
                          'title': entry['title'], 'filed': as_of[:10], 'baseline': after,
                          'unit': entry['unit'], 'threshold': entry['threshold'], 'card': None,
                          'verdict': None, 'log': entry['log'], 'reopens': iid}
    return done


# ------------------------------------------------------------ the daily part --

def _cards_loader(product, root, facts):
    cache = {product.name: facts.items}

    def cards_of(target):
        if target not in cache:
            from asf import env
            from asf.tick import shadow
            try:
                p = env.load_product(target)
                d = shadow.record_dir(p)
                d = d if os.path.isdir(os.path.join(d, 'features')) else p.backlog_dir
                cache[target] = load_cards(d) if d else {}
            except Exception:  # noqa: BLE001 — an unreadable target is one with no cards yet
                cache[target] = {}
        return cache[target]
    return cards_of


def daily(product, root, out=print, facts=None, factory=None):
    """The loop, once: drain, measure, snapshot, diagnose and file, verify, drain. Returns 0."""
    drain(product, root, out=out)
    cfg = settings(product)
    facts = facts or load(root, product)
    start, end = diagnose.window(facts.as_of, cfg['window_days'])
    found = diagnose.causes(facts, start, end, cfg['thresholds'])
    row = snapshot(product, facts, causes=found)
    state_path = state_file(product, CAUSES)
    state = _read_json(state_path, {})
    fac = factory if factory is not None else (lambda: factory_product())
    filed = file_causes(product, found, cfg, facts.as_of, state, factory=fac, root=root)
    verdicts = verify(product, facts, cfg, state, _cards_loader(product, root, facts), facts.as_of,
                      root=root)
    _write_json(state_path, state)
    drain(product, root, out=out)
    h = score.headline(facts)
    # the lane experiment's one line (``asf scorecard --by-lane`` has the table and the pairs)
    lanes = score.by_lane(facts, *diagnose.window(facts.as_of, LANE_DAYS))
    out(f"scorecard: {score.headline_line(h, facts.clutter)} · week {row['week']}: {row['landed']} landed"
        f" · {len(found)} cause(s) over threshold, {len(filed)} filed, {len(verdicts)} verified"
        f" · {score.lanes_line(lanes, LANE_DAYS)}")
    return 0
