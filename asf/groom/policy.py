"""asf.groom.policy — rules as code (F-0085/D-0049): the ``approvals.groom`` gate, the open-
question grammar, the policies, and the approval bound.

An undecided card moves by code over facts: every policy reads only the record and the facts
:class:`Ctx` hands it (the date, the thresholds, the trunk's CI runs, the approval levels), so
the same record and the same facts always give the same answer. What no policy answers stays an
open question for the operator or the adjudicate session, exactly as before.

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
ADJUDICATE_PER_DAY = 6
#: ``decide_or_close_ci_red``: a trunk failure older than this is not taken as the job being red
#: now — no answer, rather than an answer off a stale fact.
CI_RED_DAYS = 7

#: A ``- [ ] <id> <title> — <why> → answer: ____`` line — a question no rule and no session has
#: yet answered (PD4). A barred line's slot reads ``____ (barred: …)`` instead, so it never
#: matches and is never counted open.
OPEN_QUESTION_RE = re.compile(
    r'^- \[ \]\s+(?P<id>[A-Z]-\d{4}\b|inbox:\S+).*→\s*answer:\s*____$')

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


def ci_red_days(product):
    """``groom.ci_red_days`` (default :data:`CI_RED_DAYS`): how recent a trunk failure of a
    job must be for ``decide_or_close_ci_red`` to take the job as red."""
    v = _groom_config(product).get('ci_red_days')
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else CI_RED_DAYS


def adjudicate_attempts(product):
    """``groom.adjudicate_attempts`` (default 2, D8): adjudicate sessions per groom date before
    the remaining questions go to the operator instead."""
    v = _groom_config(product).get('adjudicate_attempts')
    return v if isinstance(v, int) and v > 0 else ADJUDICATE_ATTEMPTS


def adjudicate_per_day(product):
    """``groom.adjudicate_per_day`` (default :data:`ADJUDICATE_PER_DAY`): adjudicate sessions a
    groom date may have in all, counting the ones launched for questions asked after the last
    session was briefed — the every-tick intake adds questions all day."""
    v = _groom_config(product).get('adjudicate_per_day')
    return v if isinstance(v, int) and v > 0 else max(ADJUDICATE_PER_DAY,
                                                      adjudicate_attempts(product))


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
    (or was) already working.

    ``ci_runs`` is the trunk's finished CI runs, oldest first, each ``{'ts': datetime, 'sha':
    str, 'jobs': {job name: conclusion}}`` (:func:`asf.groom.groom.trunk_ci_runs` reads them off
    the record's ``metrics/ci`` stream); ``approvals`` is the product's raw ``approvals:`` map,
    which ``decide_by_approval`` reads for its two classes."""
    date: str
    now: object
    duplicate_overlap: float = DUPLICATE_OVERLAP
    recurring_bug_count: int = RECURRING_BUG_COUNT
    undecided_close: str = DEFAULT_UNDECIDED_CLOSE
    ledger_items: frozenset = frozenset()
    ci_red_days: int = CI_RED_DAYS
    ci_runs: tuple = ()
    approvals: dict = dataclasses.field(default_factory=dict)


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


def _undecided(rec):
    """True for a card still waiting on a decision: open, ``decided != true``, not removed and
    not moved to another record."""
    from asf.record import frontmatter
    from asf.record.core import is_open
    if not is_open(rec):
        return False
    typed, _machine = frontmatter.split_machine(rec['meta'])
    return (typed.get('decided') is not True and not typed.get('removed')
            and not typed.get('moved_to'))


def _live_decided(rec):
    """True for a card that stands as decided: ``decided: true``, not removed, not moved."""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(rec['meta'])
    return (typed.get('decided') is True and not typed.get('removed')
            and not typed.get('moved_to'))


def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def superseded_by(item_id, rec, canonical):
    """The id of the decided card that supersedes ``item_id`` — one naming it in its
    ``links.supersedes``, else one of the same type sharing its ``legacy_id`` — or ``None``.
    Lowest id first, so the answer never depends on dict order. (A Story and a Feature carry the
    same legacy id when one was split off the other; that is a lineage, not a duplicate.)"""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(rec['meta'])
    legacy = typed.get('legacy_id')
    named = shared = None
    for oid, orec in sorted(canonical.items()):
        if oid == item_id or not _live_decided(orec):
            continue
        otyped, _omachine = frontmatter.split_machine(orec['meta'])
        if named is None and item_id in _as_list((otyped.get('links') or {}).get('supersedes')):
            named = oid
        if (shared is None and legacy and otyped.get('legacy_id') == legacy
                and otyped.get('type') == typed.get('type')):
            shared = oid
    return named or shared


def close_superseded(item_id, rec, canonical, derived, ctx):
    """An undecided card that a decided card names in ``links.supersedes``, or that shares a
    ``legacy_id`` with a decided card, is closed as superseded by it."""
    if not _undecided(rec):
        return None
    oid = superseded_by(item_id, rec, canonical)
    if oid is None:
        return None
    reason = f'superseded by {oid}'
    return Answer(f'no: {reason}', 'removed', f'{reason} (groom {ctx.date})', reason)


#: The derived stages that say a Feature's spec or plan is approved on the trunk.
_APPROVED_DOC_STAGES = ('spec-approved', 'plan-approved')


def decide_on_approved_doc(item_id, rec, canonical, derived, ctx):
    """An undecided Feature whose derived stage says its spec or plan is approved on the trunk
    (``spec-approved``, ``plan-approved`` or ``building…``) is decided: the approval already
    happened on the trunk, the missing ``decided: true`` is only bookkeeping."""
    from asf.record import frontmatter
    typed, machine = frontmatter.split_machine(rec['meta'])
    if typed.get('type') != 'feature' or not _undecided(rec):
        return None
    stage = str(machine.get('stage') or '')
    if stage not in _APPROVED_DOC_STAGES and not stage.startswith('building'):
        return None
    return Answer('yes', 'decided', True, f'spec or plan approved on trunk ({stage})')


def ci_red_job(typed):
    """The CI job an auto-filed "CI red" Bug names, or ``None``. The Bug filer
    (:func:`asf.tick.file_bugs.ci_signatures`) keys such a Bug on ``<job>: <failed step>`` and
    titles it ``CI red: <signature>``; any other Bug names no job."""
    from asf.tick.file_bugs import CI_RED_TITLE
    sig = typed.get('signature')
    if typed.get('type') != 'bug' or not isinstance(sig, str) or ': ' not in sig:
        return None
    if not str(typed.get('title') or '').startswith(CI_RED_TITLE):
        return None
    return sig.split(': ', 1)[0].strip() or None


def _filed_at(typed, machine):
    """When the Bug was last filed: the later of the start (UTC) of its ``last_filed`` day and
    its ``stage_since`` — a green run earlier on the day it was filed is not "since"."""
    import datetime
    from asf.tick.stale import parse_iso
    lf = typed.get('last_filed')
    if isinstance(lf, datetime.date) and not isinstance(lf, datetime.datetime):
        lf = lf.isoformat()
    day = (parse_iso(f'{lf}T00:00:00Z')
           if isinstance(lf, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', lf) else None)
    since = parse_iso(machine.get('stage_since'))
    known = [t for t in (day, since) if t is not None]
    return max(known) if known else None


def decide_or_close_ci_red(item_id, rec, canonical, derived, ctx):
    """An auto-filed Bug saying a named CI job is red, judged by that job's trunk runs
    (``ctx.ci_runs``): its latest trunk run failed within ``ctx.ci_red_days`` → decided; its
    latest trunk run passed after the Bug was last filed → closed, green since the first run of
    that green streak. No trunk run of the job, or only a stale failure → no answer."""
    import datetime
    from asf.record import frontmatter
    typed, machine = frontmatter.split_machine(rec['meta'])
    job = ci_red_job(typed)
    if job is None or not _undecided(rec):
        return None
    runs = [r for r in ctx.ci_runs
            if r.get('ts') is not None and (r.get('jobs') or {}).get(job) in ('success', 'failure')]
    if not runs:
        return None
    latest = runs[-1]
    if latest['jobs'][job] == 'failure':
        if ctx.now - latest['ts'] > datetime.timedelta(days=ctx.ci_red_days):
            return None
        return Answer('yes', 'decided', True,
                      f"{job} failed on the trunk at {str(latest.get('sha') or '')[:9]}")
    filed = _filed_at(typed, machine)
    if filed is None or latest['ts'] < filed:
        return None
    first = len(runs) - 1
    while first > 0 and runs[first - 1]['jobs'][job] == 'success':
        first -= 1
    reason = f"green since {str(runs[first].get('sha') or '')[:9]}"
    return Answer(f'no: {reason}', 'removed', f'{reason} (groom {ctx.date})', f'{job} {reason}')


#: ``decide_by_approval``'s two approval classes, by the card type each one covers.
DECIDE_CLASSES = {'feature': 'decide_feature', 'bug': 'decide_bug'}


def decide_by_approval(item_id, rec, canonical, derived, ctx):
    """``approvals.decide_feature: auto`` (resp. ``decide_bug``): an undecided, unsuperseded
    Feature (resp. Bug) under a live Epic — open and decided — is decided. The approval bound
    (:data:`BARRED`) holds a card that reads as money, security, production, customer data or
    legal back for the operator before any policy is asked."""
    from asf.record import frontmatter
    from asf.record.core import is_open
    typed, _machine = frontmatter.split_machine(rec['meta'])
    cls = DECIDE_CLASSES.get(typed.get('type'))
    if cls is None or str((ctx.approvals or {}).get(cls, '')).lower() != 'auto':
        return None
    if not _undecided(rec) or superseded_by(item_id, rec, canonical) is not None:
        return None
    epic = canonical.get(typed.get('parent'))
    if epic is None or not is_open(epic) or not _live_decided(epic):
        return None
    if frontmatter.split_machine(epic['meta'])[0].get('type') != 'epic':
        return None
    return Answer('yes', 'decided', True, f"approvals.{cls}: auto, Epic {typed.get('parent')} is live")


#: The groom sections whose lines ask for a card's decision.
DECISION_SECTIONS = ('inbox', 'undecided3', 'no_stories', 'dupes', 'undecided14', 'auto_bugs')

#: PD3 — ``(name, section keys, policy)``. ``name`` must equal ``asf.groom.groom.POLICY_NAMES``
#: in the same order (a test asserts it); kept as plain strings rather than an import of that
#: module, which would be the cycle this module's docstring rules out. The section keys are the
#: groom sections whose lines the policy may answer. A card can sit in several sections:
#: :func:`asf.groom.groom.run_policy_pass` asks the decision policies (all but
#: :data:`PER_LINE_POLICIES`) in this order, and the first answer is the card's one answer on
#: every line of it — a close (duplicate, superseded, CI green) always wins over a decide, and no
#: card is both decided and closed in one pass.
POLICIES = (
    ('unblock_on_closed', ('blocked_closed',), unblock_on_closed),
    ('close_exact_duplicate', ('dupes',), close_exact_duplicate),
    ('close_superseded', DECISION_SECTIONS, close_superseded),
    ('decide_on_approved_doc', DECISION_SECTIONS, decide_on_approved_doc),
    ('decide_or_close_ci_red', DECISION_SECTIONS, decide_or_close_ci_red),
    ('decide_recurring_bug', ('auto_bugs',), decide_recurring_bug),
    ('decide_by_approval', DECISION_SECTIONS, decide_by_approval),
    ('close_on_starvation', ('undecided14',), close_on_starvation),
)

#: Policies answered line by line rather than once per card: an unblock is not a decision.
PER_LINE_POLICIES = ('unblock_on_closed',)


#: The card recognisers of the approval bound: a card whose title reads as one of these classes
#: is the operator's to decide unless the product maps that class to ``auto``. Matched
#: case-insensitively against the title only — a body mentions everything. An auto-filed Bug
#: (one with a ``signature``) is exempt: it is a fault the factory filed under ``file_bug``, and
#: every action its fix takes is still held by the approvals hook when it is taken.
CARD_CLASS_WORDS = {
    'spend_money': re.compile(
        r'\b(stripe|billing|invoices?|payments?|payouts?|pay|paying|paid|pricing|prices?|'
        r'subscriptions?|spend|spending|purchases?|refunds?|unit cost|virtual card|'
        r'credit card|sale|sales)\b', re.IGNORECASE),
    'touch_security': re.compile(
        r'\b(secrets?|credentials?|passwords?|tokens?|api keys?|sso|scim|oauth|'
        r'authenticat\w*|authori[sz]\w*|encrypt\w*|vulnerabilit\w*|security|permissions?|'
        r'2fa|mfa)\b', re.IGNORECASE),
    'touch_production': re.compile(r'\b(prod|production|go[- ]live|dns)\b', re.IGNORECASE),
    'touch_customer_data': re.compile(
        r'\b(customer data|customer records?|personal data|user data|pii|data exports?|'
        r'account deletion|delete (?:my|an?|the) account|lawful basis|cross-tenant)\b',
        re.IGNORECASE),
    'touch_legal': re.compile(
        r'\b(licen[cs]es?|legal|terms|privacy|gdpr|dpa|compliance|cookies?|consent|'
        r'certificat\w*|notices?)\b', re.IGNORECASE),
}


def card_crosses(cls, typed):
    """True when a card (its typed fields) reads as action class ``cls`` —
    :data:`CARD_CLASS_WORDS` over its title."""
    words = CARD_CLASS_WORDS.get(cls)
    if words is None or typed.get('signature'):
        return False
    return bool(words.search(str(typed.get('title') or '')))


#: §2.8's table, kept as data so F-0031 extends it without touching this module. Each entry: the
#: ``approvals.<key>`` consulted, and a predicate over ``(answer, typed)`` that is true when
#: ``answer`` would cross it. A ``yes`` on an Epic opens it (``new_epic``); a ``yes`` on a card
#: that reads as money, security, production, customer data or legal commits the factory to it.
BARRED = (
    ('new_epic', lambda answer, typed: answer.word == 'yes' and typed.get('type') == 'epic'),
) + tuple(
    (cls, lambda answer, typed, _cls=cls: answer.word == 'yes' and card_crosses(_cls, typed))
    for cls in CARD_CLASS_WORDS
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
    # a groom session's item is only the token its job was named by (the oldest question): it
    # acts on no item, so it speaks for none — F-0080 went "(spoken for: groom)" for a day
    held = {s.get('item'): s.get('kind') for s in (inflight or ())
            if s.get('item') and s.get('kind') != 'groom'}
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
