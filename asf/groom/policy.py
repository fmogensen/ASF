"""asf.groom.policy — rules as code (F-0085/D-0049): the ``approvals.groom`` gate, the open-
question grammar, the four policies, and the approval bound.

Module-level imports stay stdlib and :mod:`asf.env` only, so :mod:`asf.feeder.rows` may import
this module without an import cycle — :mod:`asf.groom.groom` (the applier's side) imports this
module instead. The four policies and :func:`barred` need :mod:`asf.record.core`/
:mod:`asf.record.frontmatter`, so they import those *inside* the function body rather than at
module level, the same trick :mod:`asf.capacity` uses for :mod:`asf.workers.lifecycle`.
"""
import dataclasses
import re

#: D11 — deliberately timid defaults. A policy that answers wrongly is a card closed or decided
#: without anyone asking, so the first version answers only what is not really a question.
DUPLICATE_OVERLAP = 0.95
RECURRING_BUG_COUNT = 2
ADJUDICATE_ATTEMPTS = 2

#: A ``- [ ] <id> <title> — <why> → answer: ____`` line — a question no rule and no session has
#: yet answered (PD4). A barred line's slot reads ``____ (barred: …)`` instead, so it never
#: matches and is never counted open.
OPEN_QUESTION_RE = re.compile(r'^- \[ \]\s+(?P<id>[A-Z]-\d{4})\b.*→\s*answer:\s*____$')

#: An open question's line, split so :func:`suppress` can keep the ``<id> <title> — <why>``
#: text and only replace the answer slot.
_SUPPRESSABLE_RE = re.compile(r'^- \[ \]\s+(?P<id>[A-Z]-\d{4})\s+(?P<body>.*?)\s*→\s*answer:\s*____$')


def groom_auto(product):
    """The single gate (§2.1): ``approvals.groom: auto`` turns on the policy pass, the
    suppression pass, the adjudicate row and the digest. Unset, or anything else (``groom``,
    ``human-now``), and every behaviour this module and its callers add is off — ``asf groom``
    behaves exactly as it does today."""
    approvals = (product.approvals if product is not None else None) or {}
    return str(approvals.get('groom', '')).lower() == 'auto'


def open_questions(text):
    """``[(item_id, line)]`` for every still-open ``- [ ] <id> … → answer: ____`` line in
    ``text``, in file order (PD4) — the questions no rule has answered and no session has
    spoken for."""
    out = []
    for line in text.splitlines():
        m = OPEN_QUESTION_RE.match(line)
        if m:
            out.append((m.group('id'), line))
    return out


def _groom_config(product):
    return (product.groom if product is not None else None) or {}


def duplicate_overlap(product):
    """``groom.duplicate_overlap`` (default 0.95): ``close_exact_duplicate``'s threshold."""
    v = _groom_config(product).get('duplicate_overlap')
    return v if isinstance(v, (int, float)) else DUPLICATE_OVERLAP


def recurring_bug_count(product):
    """``groom.recurring_bug_count`` (default 2): ``decide_recurring_bug``'s threshold."""
    v = _groom_config(product).get('recurring_bug_count')
    return v if isinstance(v, int) else RECURRING_BUG_COUNT


def adjudicate_attempts(product):
    """``groom.adjudicate_attempts`` (default 2, D8): adjudicate sessions per groom date before
    the remaining questions go to the operator instead."""
    v = _groom_config(product).get('adjudicate_attempts')
    return v if isinstance(v, int) and v > 0 else ADJUDICATE_ATTEMPTS


def policy_on(product, name):
    """``groom.policies.<name>: off`` skips that policy; absent, or anything but ``off``, runs
    it."""
    policies = _groom_config(product).get('policies') or {}
    return str(policies.get(name, 'on')).lower() != 'off'


#: D11's default for the fourth policy, kept beside the other three even though it is read
#: through ``stage_limits.undecided_close`` (``asf.tick.stale``), not ``groom:``.
DEFAULT_UNDECIDED_CLOSE = '14d'


@dataclasses.dataclass
class Answer:
    """One policy's ruling on one open question: ``word`` is what gets rendered into the groom
    file (``→ answer: controller: <policy> <word>``); ``field``/``value`` are what
    ``apply_groom_answers`` will end up writing, so a test can check a policy's decision without
    going through the render/parse round trip; ``why`` is the policy's own reason (§2.2's
    "Reason line" column) — not rendered anywhere itself (the card line already carries its
    section's reason, PD5), but what a test asserts on."""
    word: str
    field: str
    value: object
    why: str


@dataclasses.dataclass
class Ctx:
    """What a policy is handed instead of the clock or the config (§2.2): the run's date, its
    ``now``, the three ``groom:`` thresholds already resolved for the product, the
    ``stage_limits.undecided_close`` duration, and the ledger's item set — every id that has
    ever had a session run on it, so ``close_on_starvation`` never answers a card a session is
    (or was) already working."""
    date: str
    now: object
    duplicate_overlap: float = DUPLICATE_OVERLAP
    recurring_bug_count: int = RECURRING_BUG_COUNT
    undecided_close: str = DEFAULT_UNDECIDED_CLOSE
    ledger_items: frozenset = frozenset()


def _named_in_blockedby(item_id, canonical):
    """True when some open item's ``blockedBy`` names ``item_id`` — the "named in no [open
    item's] blockedBy" condition §2.2 gives both ``close_exact_duplicate`` and
    ``close_on_starvation``."""
    from asf.record import frontmatter
    from asf.record.core import is_open
    for rec in canonical.values():
        if not is_open(rec):
            continue
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if item_id in (typed.get('blockedBy') or []):
            return True
    return False


def unblock_on_closed(item_id, rec, canonical, derived, ctx):
    """Always — the blocker's ``state`` is ``Closed``: the card's first ``blockedBy`` entry
    whose own record is ``Closed`` gets unblocked."""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(rec['meta'])
    for blocker in typed.get('blockedBy') or []:
        if not isinstance(blocker, str) or blocker not in canonical:
            continue
        _btyped, bmachine = frontmatter.split_machine(canonical[blocker]['meta'])
        if bmachine.get('state') == 'Closed':
            return Answer(f'unblock {blocker}', 'unblock', blocker, f'blocker {blocker} is Closed')
    return None


def close_exact_duplicate(item_id, rec, canonical, derived, ctx):
    """Overlap ≥ ``ctx.duplicate_overlap``, same ``type``, same ``parent``, and the younger card
    (``item_id`` itself — the id a near-duplicate line always names, §2.9's ``dupes`` section) is
    ``state: New``, ``decided != true``, childless, named in no blockedBy, and has no branch in
    ``links``."""
    from asf.record import frontmatter
    from asf.record.core import is_open, jaccard, tokenize
    typed, machine = frontmatter.split_machine(rec['meta'])
    if machine.get('state', 'New') != 'New':
        return None
    if typed.get('decided') is True:
        return None
    if derived.get(item_id, {}).get('children'):
        return None
    if _named_in_blockedby(item_id, canonical):
        return None
    if (typed.get('links') or {}).get('branches'):
        return None
    my_type = typed.get('type')
    my_parent = typed.get('parent')
    my_tokens = tokenize(typed.get('title', ''))
    best = None
    for oid, orec in sorted(canonical.items()):
        if oid == item_id or not is_open(orec):
            continue
        otyped, _omachine = frontmatter.split_machine(orec['meta'])
        if otyped.get('type') != my_type or otyped.get('parent') != my_parent:
            continue
        score = jaccard(my_tokens, tokenize(otyped.get('title', '')))
        if score >= ctx.duplicate_overlap and (best is None or score > best[1]):
            best = (oid, score)
    if best is None:
        return None
    oid, score = best
    return Answer('no', 'removed', f'groom {ctx.date}', f'duplicate of {oid} (overlap {score:.2f})')


def decide_recurring_bug(item_id, rec, canonical, derived, ctx):
    """The Bug has a ``signature`` and ``count >= ctx.recurring_bug_count``."""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(rec['meta'])
    if typed.get('type') != 'bug' or not typed.get('signature'):
        return None
    count = typed.get('count', 1)
    if not isinstance(count, int) or count < ctx.recurring_bug_count:
        return None
    return Answer('yes', 'decided', True, f'auto-filed, seen {count} times')


def close_on_starvation(item_id, rec, canonical, derived, ctx):
    """Older than ``ctx.undecided_close``, still ``decided != true``, childless, named in no open
    item's ``blockedBy``, and no session in ``ctx.ledger_items`` ever ran on it."""
    from asf.record import frontmatter
    from asf.tick.stale import format_age, limit_seconds, parse_iso
    typed, machine = frontmatter.split_machine(rec['meta'])
    if typed.get('decided') is True:
        return None
    if derived.get(item_id, {}).get('children'):
        return None
    if _named_in_blockedby(item_id, canonical):
        return None
    if item_id in ctx.ledger_items:
        return None
    since = parse_iso(machine.get('stage_since'))
    if since is None:
        return None
    age = (ctx.now - since).total_seconds()
    if age <= limit_seconds(ctx.undecided_close):
        return None
    return Answer('no', 'removed', f'groom {ctx.date}',
                  f'undecided {format_age(age)}, starvation policy')


#: PD3, §2.2's order — ``(name, section key, policy)``. ``name`` must equal
#: ``asf.groom.groom.POLICY_NAMES`` in the same order (a test asserts it); kept as plain strings
#: rather than an import of that module, which would be the cycle this module's docstring rules
#: out.
POLICIES = (
    ('unblock_on_closed', 'blocked_closed', unblock_on_closed),
    ('close_exact_duplicate', 'dupes', close_exact_duplicate),
    ('decide_recurring_bug', 'auto_bugs', decide_recurring_bug),
    ('close_on_starvation', 'undecided14', close_on_starvation),
)


#: §2.8's one-row table, kept as data so F-0031 extends it without touching this module. Each
#: entry: the ``approvals.<key>`` consulted, and a predicate over ``(answer, typed)`` that is
#: true when ``answer`` would cross it.
BARRED = (
    ('new_epic', lambda answer, typed: answer.word == 'yes' and typed.get('type') == 'epic'),
)


def barred(answer, item, product):
    """The ``approvals.<key>`` name when ``answer`` on ``item`` would cross an action class the
    product does not map to ``auto`` (§2.8, D10); ``None`` when nothing bars it. A barred
    question is answered by no policy, and — the same bound — never put to the adjudicate
    session either."""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(item['meta'])
    approvals = (product.approvals if product is not None else None) or {}
    for key, predicate in BARRED:
        if predicate(answer, typed) and str(approvals.get(key, '')).lower() != 'auto':
            return key
    return None


def suppress(sections, index, inflight, product):
    """§2.3: a question the factory is already acting on is asked of nobody. A line is rewritten
    ``- [x] <id> <title> — <why> → answer: (spoken for: <ROW KIND>)`` when its item is either

    - a live session's item (``inflight``), labelled with that session's own ``kind``; or
    - the item of a row :func:`asf.feeder.rows.candidates` would launch right now, labelled with
      that row's ``kind`` (e.g. ``CARD → SPEC``) — this is the more informative label, so it
      wins when both hold.

    Imports :mod:`asf.feeder.rows` inside the function body: that module imports this one at
    module level (for :func:`groom_auto`), so the reverse import must stay lazy (module
    docstring). Returns ``(sections, suppressed_count)``."""
    from asf.feeder import rows as feeder_rows
    spoken = {row.item_id: row.kind for row in feeder_rows.candidates(index, product, inflight)
             if row.launches}
    held = {s.get('item'): s.get('kind') for s in (inflight or ()) if s.get('item')}
    out = {}
    count = 0
    for key, lines in sections.items():
        new_lines = []
        for line in lines:
            m = _SUPPRESSABLE_RE.match(line)
            label = (spoken.get(m.group('id')) or held.get(m.group('id'))) if m else None
            if m and label:
                new_lines.append(f"- [x] {m.group('id')} {m.group('body')} → answer: "
                                 f"(spoken for: {label})")
                count += 1
            else:
                new_lines.append(line)
        out[key] = new_lines
    return out, count
