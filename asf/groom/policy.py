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

from asf.record.core import ID_DIGITS, ID_TOKEN_RE

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
    rf'^- \[ \]\s+(?P<id>[A-Z]-{ID_DIGITS}\b|inbox:\S+).*→\s*answer:\s*____$')

#: An open question's line, split so :func:`suppress` can keep the ``<id> <title> — <why>``
#: text and only replace the answer slot.
#: The why of a groom line asking the adjudicator about an item the approvals hook keeps
#: refusing (:func:`asf.groom.groom.groom_refused_section`). No relaunch speaks for it: the
#: relaunch is what keeps being refused, so :func:`suppress` leaves the line open.
REFUSED_MARK = 'refused on repeat:'

_SUPPRESSABLE_RE = re.compile(rf'^- \[ \]\s+(?P<id>[A-Z]-{ID_DIGITS})\s+(?P<body>.*?)\s*→\s*answer:\s*____$')


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
    job must be for ``decide_or_close_ci_red`` to take the job as red — and, the same window,
    how recent the job's newest trunk fact must be for the policy to answer off it at all."""
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
    #: ``flags.groom_rules`` resolved (:func:`groom_rules`): the dedupe keys
    #: ``close_exact_duplicate`` may match on besides the title overlap
    groom_rules: frozenset = frozenset()


def _named_in_blockedby(item_id, canonical):
    """True when some open item's ``blockedBy`` names ``item_id`` — the "named in no [open
    item's] blockedBy" condition §2.2 gives both ``close_exact_duplicate`` and
    ``close_on_starvation``."""
    from asf.record import frontmatter
    from asf.record.core import as_list, is_open
    for rec in canonical.values():
        if not is_open(rec):
            continue
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if item_id in as_list(typed.get('blockedBy')):
            return True
    return False


def unblock_on_closed(item_id, rec, canonical, derived, ctx):
    """Always — the blocker's ``state`` is ``Closed``: the card's first ``blockedBy`` entry
    whose own record is ``Closed`` gets unblocked."""
    from asf.record import frontmatter
    from asf.record.core import as_list
    typed, _machine = frontmatter.split_machine(rec['meta'])
    for blocker in as_list(typed.get('blockedBy')):
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
    ``links``. Under ``flags.groom_rules`` an older open card with the same dedupe key
    (:func:`keyed_duplicate_of` — one finding's cause, one scorecard class) is a duplicate
    whatever the titles' overlap."""
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
    keyed = keyed_duplicate_of(item_id, rec, canonical, ctx.groom_rules)
    if keyed is not None:
        oid, label = keyed
        return Answer('no', 'removed', f'groom {ctx.date}', f'duplicate of {oid} ({label})')
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


#: ``groom_near_duplicates``' title-overlap threshold, and the raised one for two titles cut
#: from one template (a migrated plan's ``Parity: <row>`` Tasks share most of their words).
NEAR_DUPLICATE_OVERLAP = 0.6
TEMPLATED_OVERLAP = 0.9
#: The separators a templated title puts after its fixed part (``Parity: <row>``).
TEMPLATE_SEPARATORS = (':', ' — ', ' - ')


def templated(title_a, title_b):
    """True when two titles look cut from one template: the same text before a ``:``, `` — ``
    or `` - `` — the fixed part of the template, the rest its varying slot."""
    a, b = str(title_a or '').strip().lower(), str(title_b or '').strip().lower()
    return any(sep in a and sep in b and a.split(sep, 1)[0].strip() == b.split(sep, 1)[0].strip()
               for sep in TEMPLATE_SEPARATORS)


def near_duplicate_threshold(title_a, title_b):
    """The overlap at which two titles are near-duplicates: raised for templated titles."""
    return tunable('TEMPLATED_OVERLAP') if templated(title_a, title_b) else tunable('NEAR_DUPLICATE_OVERLAP')


def _footprint(typed):
    return tuple(sorted(str(w) for w in (typed.get('writes') or ())))


def task_duplicate_of(item_id, rec, canonical):
    """The older open Task ``item_id`` (a Task) duplicates by rule, or ``None``: same ``parent``
    and the same footprint (``writes:``), a near-duplicate title, and ``item_id`` — the younger,
    higher id — still ``New`` with no branch. Deciding a near-duplicate Task by this rule is
    closing the younger one; any other near-duplicate Task pair is no question at all."""
    from asf.record import frontmatter
    from asf.record.core import is_open, jaccard, tokenize
    typed, machine = frontmatter.split_machine(rec['meta'])
    if typed.get('type') != 'task' or machine.get('state', 'New') != 'New':
        return None
    if (typed.get('links') or {}).get('branches') or not typed.get('parent'):
        return None
    title = typed.get('title', '')
    for oid, orec in sorted(canonical.items()):
        if oid >= item_id or not is_open(orec):
            continue
        otyped, _m = frontmatter.split_machine(orec['meta'])
        if (otyped.get('type') != 'task' or otyped.get('parent') != typed.get('parent')
                or _footprint(otyped) != _footprint(typed)):
            continue
        otitle = otyped.get('title', '')
        if jaccard(tokenize(title), tokenize(otitle)) > near_duplicate_threshold(title, otitle):
            return oid
    return None


def close_duplicate_task(item_id, rec, canonical, derived, ctx):
    """A near-duplicate Task is decided by rule: the younger of two open Tasks with the same
    parent and footprint (:func:`task_duplicate_of`) is closed as a duplicate of the older."""
    oid = task_duplicate_of(item_id, rec, canonical)
    if oid is None or derived.get(item_id, {}).get('children') or \
            _named_in_blockedby(item_id, canonical):
        return None
    reason = f'duplicate of {oid} (same parent and footprint)'
    return Answer(f'no: {reason}', 'removed', f'{reason} (groom {ctx.date})', reason)


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
    that green streak. No trunk run of the job, or a newest run older than ``ctx.ci_red_days`` →
    no answer: a policy never answers off a fact older than its own window (F-0175)."""
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
    if ctx.now - latest['ts'] > datetime.timedelta(days=ctx.ci_red_days):
        return None            # the job's newest fact is older than the window: no answer off it
    if latest['jobs'][job] == 'failure':
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
    Feature (resp. Bug) under a live Epic — open and decided — is decided. Deciding the card
    crosses no approval class (:data:`BARRED`): the approvals hook guards the work it leads to."""
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
DECISION_SECTIONS = ('inbox', 'undecided_new', 'undecided3', 'no_stories', 'dupes', 'undecided14', 'auto_bugs')

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
    ('close_duplicate_task', ('dupes',), close_duplicate_task),
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


#: §2.8's table, kept as data so F-0031 extends it without touching this module. Each entry: the
#: ``approvals.<key>`` consulted, and a predicate over ``(answer, typed)`` that is true when
#: ``answer`` would cross it. A ``yes`` on an Epic opens it (``new_epic``). Deciding any other
#: card crosses no approval class: the class belongs to the actions the item's work will take,
#: and the approvals hook judges each of those when the session takes it — a card's title read
#: as money, security or legal barred a parity Story for nothing (the For-you card, item 2).
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
    # a groom session's item is only the token its job was named by (the oldest question): it
    # acts on no item, so it speaks for none — F-0080 went "(spoken for: groom)" for a day
    held = {s.get('item'): s.get('kind') for s in (inflight or ())
            if s.get('item') and s.get('kind') != 'groom'}
    out = {}
    count = 0
    for key, lines in sections.items():
        new_lines = []
        for line in lines:
            m = None if REFUSED_MARK in line else _SUPPRESSABLE_RE.match(line)
            label = (spoken.get(m.group('id')) or held.get(m.group('id'))) if m else None
            if m and label:
                new_lines.append(f"- [x] {m.group('id')} {m.group('body')} → answer: "
                                 f"(spoken for: {label})")
                count += 1
            else:
                new_lines.append(line)
        out[key] = new_lines
    return out, count


# ---- dependency roots (``flags.roots``, W8-PR1g) --------------------------------------------

#: the ``trunk_closed:`` reason an accepted unverified landing carries — the rule's own name, so
#: the ledger says which rule closed the item
COVERS_ACCEPT = 'groom-covers-accept'
#: the ``by:`` of the ``landing:`` stamp the ingest writes when the card closes on a landing
#: this rule accepted (:func:`landing_by`) — the groom decided it, not a trunk arm
COVERS_ACCEPT_BY = 'groom'


def landing_by(trunk_closed, arm=''):
    """The ``landing: by`` for a card whose landing run was closed on trunk evidence
    (``trunk_closed:`` on the run): ``groom`` when this module's covers rule accepted it
    (:data:`COVERS_ACCEPT`), else ``trunkclose/<arm>`` — the arm that attributed the sha
    (``names``, ``pr`` or ``covers``). A run that names no arm is stamped ``trunkclose/covers``,
    the arm I14 holds to the strictest proof. ``''`` when the run was not closed that way."""
    if not trunk_closed:
        return ''
    if str(trunk_closed).startswith(COVERS_ACCEPT):
        return COVERS_ACCEPT_BY
    return 'trunkclose/' + (arm if arm in ('names', 'pr', 'covers') else 'covers')


def decide_unverified_landing(item_id, sha, age_hours, covers, report, hours):
    """Rule 2 of ``flags.roots``: a landing recorded for ``item_id`` but not verified as its
    work decides itself once it is ``hours`` old — *accept* when the harvested ``sha`` covers
    the item's ``writes:`` (``covers``) and the run's REPORT says ``done`` (``report``), else
    *reset*: the claim is voided and the item starts over. ``covers`` of ``None`` (git could not
    answer) is no answer either way. Younger, or no age known: no answer,
    the NEEDS DECISION row stands. Pure — the caller reads the facts
    (:func:`unverified_landing_facts`) and applies the answer (:func:`apply_unverified_landings`)."""
    if age_hours is None or age_hours < hours:
        return None
    if covers is None:      # git could not answer: an unknown never acts, neither way
        return None
    short = str(sha or '')[:9] or 'no sha'
    if covers and report == 'done':
        why = (f'{COVERS_ACCEPT}: {short} covers its writes: and its report says done '
               f'(unverified for {int(age_hours)} h)')
        return Answer('accept', 'trunk_closed', why, why)
    said = f'report {report}' if report else 'no report'
    why = (f'unverified for {int(age_hours)} h: {short} '
           f"{'covers' if covers else 'does not cover'} its writes:, {said}")
    return Answer('reset', 'reset', why, why)


def unverified_landing_facts(product, occupancy, unverified, items, now, path=None, repo=None):
    """``[(item, run, sha, age_hours, covers, report)]`` for each unverified landing
    (``unverified``: :func:`asf.workers.landing.verify_landings`' second half): its landing run
    (the newest run of the item harvested at the recorded sha), how long since it ended, whether
    the sha covers the item's ``writes:`` (:func:`asf.workers.landing.covers`) and the run's
    REPORT status (:func:`asf.harvest.lane.report_status`). A run already closed by the trunk
    rule, or none found, gives no fact."""
    from asf.harvest.lane import report_status
    from asf.tick.stale import parse_iso
    from asf.workers import landing, lifecycle
    from asf.workers import pool as pool_mod
    path = path or pool_mod.sessions_path(product)
    repo = repo or getattr(product, 'repo_dir', None)
    landed = (occupancy or {}).get('landed') or {}
    out = []
    for iid in sorted(unverified or ()):
        sha = landed.get(iid) or ''
        runs = [r for r in lifecycle.item_runs(path, iid) if sha and r.get('harvested') == sha]
        if not runs or runs[-1].get('trunk_closed'):
            continue
        run = runs[-1]
        ended = parse_iso(run.get('ended'))
        age = (now - ended).total_seconds() / 3600 if ended else None
        writes = [str(w) for w in ((items or {}).get(iid) or {}).get('writes') or ()]
        out.append((iid, run, sha, age, bool(repo) and landing.covers(repo, sha, writes),
                    report_status(run)))
    return out


def _reset(product, item_id, why):
    """``asf reset <item> --why`` when this release has it, else ``None`` (the answer waits)."""
    import argparse
    from asf.workers import correct
    cmd = getattr(correct, 'cmd_reset', None)
    if cmd is None:
        return None
    return cmd(argparse.Namespace(item=item_id, why=why, undo=False, product=product.name))


def apply_unverified_landings(product, root, now=None, out=print, dry_run=False):
    """Rule 2 of ``flags.roots``, once per tick (the groom step): every unverified landing at
    least ``flags.roots_unverified_hours`` old is decided (:func:`decide_unverified_landing`) —
    an accept closes its run on the sha (:func:`asf.workers.trunkclose.close`, reason
    :data:`COVERS_ACCEPT`: the ingest closes the card by its ordinary rule), a reset voids the
    claim (``asf reset``). Flag off: nothing. Returns ``[(item, word)]``."""
    import datetime
    import os
    from asf.feeder import rows as feeder_rows
    if not feeder_rows.roots_on(product):
        return []
    from asf.tick import step_wave
    from asf.views import index_reader
    from asf.workers import trunkclose
    if not root or not os.path.isfile(os.path.join(root, 'index.json')):
        return []
    index, _gen = index_reader.load(root)
    items = feeder_rows.items_of(index)
    occ = step_wave.occupancy(product)
    _verified, unverified = step_wave.landings(product, occ, index)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hours = feeder_rows.roots_settings(product)[0]
    done = []
    for iid, run, sha, age, covers, report in unverified_landing_facts(product, occ, unverified,
                                                                       items, now):
        answer = decide_unverified_landing(iid, sha, age, covers, report, hours)
        if answer is None:
            if covers is None and age is not None and age >= hours:
                out(f"roots: {iid} {'would wait' if dry_run else 'waits'} on git "
                    f'(cannot tell whether {str(sha or "")[:9] or "no sha"} covers its writes:)')
            continue
        if dry_run:
            out(f'roots: would {answer.word} {iid} — {answer.why}')
        elif answer.word == 'accept':
            trunkclose.close(product, run['job'], sha, answer.why)
            out(f'roots: accepted {iid} — {answer.why}')
        else:
            rc = _reset(product, iid, answer.why)
            if rc is None:
                out(f'roots: {iid} needs a reset (asf reset is not in this release) — '
                    f'{answer.why}')
                continue
            if rc:
                out(f'roots: reset {iid} refused (rc {rc}) — {answer.why}')
                continue
            out(f'roots: reset {iid} — {answer.why}')
        done.append((iid, answer.word))
    return done


def stale_park_lines(rows, canonical):
    """Rule 3 of ``flags.roots``: one groom question per stale park the feeder surfaced
    (:func:`asf.feeder.rows.surface_stale_parks`) — never one per dependant::

        - [ ] T-0108 <title> — 31 rows wait on this park (4 d): unpark it, close it or replan it → answer: ____
    """
    from asf.feeder import rows as feeder_rows
    from asf.record import frontmatter
    lines, seen = [], set()
    for r in rows:
        m = feeder_rows.STALE_PARK_RE.match(r.action or '')
        if not m or r.item_id in seen:
            continue
        seen.add(r.item_id)
        rec = (canonical or {}).get(r.item_id)
        title = frontmatter.split_machine(rec['meta'])[0].get('title', '') if rec else ''
        age = re.search(r'\((\d+) d\)', r.action)
        lines.append(f"- [ ] {r.item_id} {title} — {m.group(1)} rows wait on this park"
                     f"{f' ({age.group(1)} d)' if age else ''}: unpark it (`asf unpark`), close "
                     f"it (`close: <why>`) or replan it (`reshape: <how>`) → answer: ____")
    return lines


def stale_park_section(product, root, canonical):
    """:func:`stale_park_lines` over the plan the feeder would make now; ``[]`` with
    ``flags.roots`` off, or no index to plan from."""
    import os
    from asf.feeder import rows as feeder_rows
    if not feeder_rows.roots_on(product) or not root \
            or not os.path.isfile(os.path.join(root, 'index.json')):
        return []
    from asf.tick import step_wave
    from asf.views import index_reader
    index, _gen = index_reader.load(root)
    inputs = step_wave.plan_inputs(product, root, index=index)
    keep = ('attempts', 'occupancy', 'groom_state', 'landed_shas', 'adjudicated',
            'unverified_landed', 'unverified_on_trunk')
    rows = feeder_rows.candidates(index, product, step_wave.inflight(product),
                                  **{k: inputs.get(k) for k in keep})
    return stale_park_lines(rows, canonical)


# ---- groom rules (``flags.groom_rules``, W8-PR4a) -------------------------------------------

#: The rules ``flags.groom_rules`` may name: the younger of two open findings of one cause closes
#: as a duplicate of the oldest; the younger of two open scorecard cards of one class folds into
#: the oldest; a Feature whose open Tasks' ``writes:`` the trunk already covers gets one *verify*
#: Task — a report, never a close (a covering diff is a hint, not the Task's landing).
GROOM_RULES = ('dedupe-findings', 'dedupe-scorecard', 'report-covered')

#: An invariant finding's Bug signature (:data:`asf.tick.file_bugs.INVARIANT_SIG`).
_FINDING_SIG_RE = re.compile(r'^invariant (?P<invariant>\S+): (?P<cause>.*)$')
#: A record card's path (``features/F-0093.md``) — the cause an older filer keyed a finding on.
_CARD_PATH_RE = re.compile(rf'\b[\w.-]+(?:/[\w.-]+)*/[A-Z]-{ID_DIGITS}\.md\b')
#: A finding Bug's first evidence line: ``- [<path> — ]<subject>: <message>``.
_EVIDENCE_RE = re.compile(r'^- (?:\S+ — )?(?P<subject>[^:\n]+): (?P<message>.+)$', re.M)
#: The scorecard loop's marker line on a card it filed (:data:`asf.scorecard.loop.MARKER`).
_SCORECARD_RE = re.compile(r'^scorecard-cause: (?P<key>.+?) #\d+\s*$', re.M)
#: An item id in a commit subject: a commit naming an item is that item's landing, read by the
#: landing rules — never a covers-only hint for another Task.
_ITEM_ID_RE = ID_TOKEN_RE
#: The marker a verify Task carries, so one Feature gets one, ever.
COVERED_MARK = 'groom-covered:'


def groom_rules(product):
    """``flags.groom_rules`` as a frozenset of :data:`GROOM_RULES` names: a list, or one string
    of names split on commas/spaces. Unset (the default), or names this release does not know:
    nothing runs."""
    v = getattr(product, 'flag', None) and product.flag('groom_rules')
    if isinstance(v, str):
        v = re.split(r'[\s,]+', v)
    if not isinstance(v, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(n).strip() for n in v if str(n).strip() in GROOM_RULES)


def _finding_cause(cause, body):
    """The cause half of a finding key. A signature whose cause is only a card path was written
    by the filer before one-Bug-per-cause: its cause is read off the first evidence line's message
    instead, so it keys like the Bug the current filer would write."""
    from asf.tick.file_bugs import collapse_id_lists, error_class
    if _CARD_PATH_RE.fullmatch(cause.strip()):
        m = _EVIDENCE_RE.search(body or '')
        if m is None:
            return '…'
        cause = m.group('message').replace(m.group('subject').strip(), '…')
    # a list of ids is one slot: "T-0183, T-0187" and "T-0547" are the same cause
    return collapse_id_lists(error_class(_CARD_PATH_RE.sub('…', cause)))


def dedupe_key(rec, rules):
    """``(key, label)`` the dedupe rules group ``rec`` by, or ``None``: an invariant finding's Bug
    by ``(invariant, cause)`` under ``dedupe-findings``; a card the scorecard loop filed by its
    cause's class (``failure`` of ``failure:failed: not pushed``) under ``dedupe-scorecard``."""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(rec['meta'])
    if 'dedupe-findings' in rules and typed.get('type') == 'bug':
        m = _FINDING_SIG_RE.match(str(typed.get('signature') or ''))
        if m:
            cause = _finding_cause(m.group('cause'), rec.get('body'))
            return ('finding', m.group('invariant'), cause), f"invariant {m.group('invariant')}: {cause}"
    if 'dedupe-scorecard' in rules:
        m = _SCORECARD_RE.search(rec.get('body') or '')
        if m:
            cls = m.group('key').split(':', 1)[0].strip()
            return ('scorecard', typed.get('type'), cls), f'scorecard class {cls}'
    return None


def keyed_duplicate_of(item_id, rec, canonical, rules):
    """``(oldest id, label)`` when an older open card shares ``item_id``'s :func:`dedupe_key`,
    else ``None``. The oldest is the lowest id: it stays, every younger one is its duplicate."""
    from asf.record.core import is_open
    if not rules:
        return None
    mine = dedupe_key(rec, rules)
    if mine is None:
        return None
    for oid in sorted(canonical):
        if oid >= item_id:
            break
        orec = canonical[oid]
        if is_open(orec) and (dedupe_key(orec, rules) or (None,))[0] == mine[0]:
            return oid, mine[1]
    return None


def _not_started(item_id, rec, canonical, derived):
    """Why a younger duplicate must stay (``''`` when it may close): it has left ``New``, has
    children, is named in an open blockedBy or has a branch — its work, or work on it, began."""
    from asf.record import frontmatter
    typed, machine = frontmatter.split_machine(rec['meta'])
    if machine.get('state', 'New') != 'New':
        return f"started ({machine.get('state')})"
    if derived.get(item_id, {}).get('children'):
        return 'has children'
    if _named_in_blockedby(item_id, canonical):
        return 'named in a blockedBy'
    if (typed.get('links') or {}).get('branches'):
        return 'has a branch'
    return ''


def dedupe_closes(canonical, derived, rules):
    """``([(item, oldest, why)], [(item, oldest, why kept)])`` — the open cards the dedupe rules
    close as duplicates of the oldest card of their key, and the ones they leave because work on
    them began (:func:`_not_started`)."""
    from asf.record.core import is_open
    closes, kept, oldest = [], [], {}
    for iid in sorted(canonical):
        rec = canonical[iid]
        key = dedupe_key(rec, rules) if is_open(rec) else None
        if key is None:
            continue
        if key[0] not in oldest:
            oldest[key[0]] = iid
            continue
        oid, label = oldest[key[0]], key[1]
        stay = _not_started(iid, rec, canonical, derived)
        (kept if stay else closes).append((iid, oid, stay or f'duplicate of {oid} ({label})'))
    return closes, kept


def _created(rec):
    """The day a card was created, off its History ``- <date>: created`` line, else ``None``."""
    m = re.search(r'^- (\d{4}-\d{2}-\d{2}): created\b', rec.get('body') or '', re.M)
    return m.group(1) if m else None


def _covers(paths, writes):
    """True when every ``writes:`` entry (a path or a glob) matches one of ``paths`` — the match
    :func:`asf.workers.landing.covers` makes against one commit's diff."""
    import fnmatch
    return bool(writes) and all(
        any(p == w or fnmatch.fnmatch(p, w) or p.startswith(w.rstrip('/') + '/') for p in paths)
        for w in writes)


def trunk_commits(repo, main, since):
    """``[(sha, day, subject, paths)]`` of ``origin/<main>``'s first-parent commits since ``since``
    (a day), newest first, each with the paths it changed against its first parent; ``None``
    when git could not answer — the report waits, never guesses."""
    from asf import gitops
    if not repo or not since:
        return None
    r = gitops.git(['log', '--first-parent', '-m', '--name-only', f'--since={since}T00:00:00Z',
                    '--format=%x1e%H%x1f%cs%x1f%s', f'origin/{main}'], repo)
    if not r.ok:
        return None
    out = []
    for chunk in (r.data or '').split('\x1e'):
        head, _, rest = chunk.strip('\n').partition('\n')
        parts = head.split('\x1f')
        if len(parts) != 3:
            continue
        out.append((parts[0], parts[1], parts[2], [p for p in rest.splitlines() if p.strip()]))
    return out


#: A Feature's stages the covered report reads: its plan is approved, its Tasks not all landed.
_COVERED_STAGES = ('plan-approved',)


def covered_reports(canonical, commits_for):
    """``[(feature, {task: (sha, subject)}, writes)]`` — each open Feature at ``plan-approved``
    or ``building…`` that has no verify Task yet, whose every open Task has a ``writes:`` that
    one trunk commit since the Task's creation covers, a commit whose subject names no item (one
    that names an item is that item's landing — the landing rules read it, :data:`_ITEM_ID_RE`).
    ``commits_for(day)`` gives the trunk's commits since ``day`` (:func:`trunk_commits`), ``None``
    when unknown — no report then. Never closes anything: a covering diff is a hint the work may
    be on the trunk, not its landing (S-B6)."""
    from asf.record import frontmatter
    from asf.record.core import is_open
    tasks_of, verified = {}, set()
    for tid, trec in canonical.items():
        ttyped, _m = frontmatter.split_machine(trec['meta'])
        if ttyped.get('type') != 'task' or not ttyped.get('parent'):
            continue
        if COVERED_MARK in (trec.get('body') or ''):
            verified.add(ttyped['parent'])
        elif is_open(trec):
            tasks_of.setdefault(ttyped['parent'], []).append((tid, ttyped))
    out = []
    for fid in sorted(canonical):
        rec = canonical[fid]
        typed, machine = frontmatter.split_machine(rec['meta'])
        stage = str(machine.get('stage') or '')
        if typed.get('type') != 'feature' or not is_open(rec) or fid in verified:
            continue
        if stage not in _COVERED_STAGES and not stage.startswith('building'):
            continue
        tasks = sorted(tasks_of.get(fid) or ())
        if not tasks or any(not t.get('writes') for _tid, t in tasks):
            continue
        hits, writes = {}, set()
        for tid, t in tasks:
            w = [str(x) for x in t['writes']]
            writes.update(w)
            since = _created(canonical[tid]) or _created(rec)
            commits = commits_for(since) if since else None
            hit = next(((sha, subj) for sha, day, subj, paths in commits or ()
                        if day >= since and not _ITEM_ID_RE.search(subj) and _covers(paths, w)),
                       None)
            if hit is None:
                break
            hits[tid] = hit
        else:
            out.append((fid, hits, sorted(writes)))
    return out


def _file_verify_task(root, canonical, fid, hits, writes, date):
    """Write the one verify Task under ``fid`` (:func:`covered_reports`); returns its id."""
    from asf.record.ids import mint_id, write_new_item
    lines = [f"{tid}'s writes: are covered by {sha[:9]} ({subj})" for tid, (sha, subj) in
             sorted(hits.items())]
    body = (f"The trunk already carries commits covering the writes: of every open Task of {fid}. "
            f"A covering diff is not the Task's landing: confirm each one and close it with the "
            f"sha that lands it, or name what the covering commit missed.\n\nCovered:\n"
            + '\n'.join(f'- {l}' for l in lines) + f'\n\n{COVERED_MARK} {fid}')
    typed = {'title': f'Verify {fid}: the trunk already covers its open Tasks',
             'parent': fid, 'writes': list(writes)}
    acceptance = [f'each open Task of {fid} is closed on the sha that lands it, or says what the '
                  f'covering commit missed']
    new_id = mint_id(root, canonical, 'task')
    return write_new_item(root, canonical, 'task', new_id, typed, body, date,
                          'groom report-covered', acceptance=acceptance)


def apply_groom_rules(product, root, now=None, out=print, dry_run=False, commits_for=None):
    """``flags.groom_rules``, once per tick (the groom step), over the record at ``root``:
    ``dedupe-findings``/``dedupe-scorecard`` close each younger open card of one key as a
    duplicate of the oldest (``removed:`` + a History line; a started one is left — the dry run
    names it), ``report-covered`` files one verify Task per covered Feature. Flag unset: nothing.
    ``dry_run`` prints ``would …`` lines and writes nothing. Returns ``[(item, word)]``."""
    import os
    rules = groom_rules(product)
    if not rules or not root or not os.path.isdir(root):
        return []
    from asf.groom.groom import _write_card
    from asf.record.core import canonicalize, compute_derived, load_items, today
    canonical, _dupes = canonicalize(load_items(root)[0])
    derived = compute_derived(canonical)
    date = today()
    done = []
    if rules & {'dedupe-findings', 'dedupe-scorecard'}:
        closes, kept = dedupe_closes(canonical, derived, rules)
        for iid, _oid, why in closes:
            if dry_run:
                out(f'groom rules: would close {iid} — {why}')
            else:
                _write_card(canonical[iid], {'removed': f'{why} (groom {date})'},
                            f'- {date} groom: removed → {why} (controller, groom_rules)')
                out(f'groom rules: closed {iid} — {why}')
            done.append((iid, 'close'))
        if dry_run:
            for iid, oid, why in kept:
                out(f'groom rules: keeps {iid} — same key as {oid}, but {why}')
    if 'report-covered' in rules:
        if commits_for is None:
            repo, main, cache = getattr(product, 'repo_dir', None), getattr(product, 'main', 'main'), {}

            def commits_for(day):
                if day not in cache:
                    cache[day] = trunk_commits(repo, main, day)
                return cache[day]
        for fid, hits, writes in covered_reports(canonical, commits_for):
            shas = ', '.join(f'{tid} by {sha[:9]}' for tid, (sha, _s) in sorted(hits.items()))
            if dry_run:
                out(f'groom rules: would file a verify Task under {fid} — {shas}')
            else:
                tid = _file_verify_task(root, canonical, fid, hits, writes, date)
                out(f'groom rules: filed {tid} to verify {fid} — {shas}')
            done.append((fid, 'report'))
    if done and not dry_run:
        from asf.record.index import do_index
        do_index(root)
    return done


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'NEAR_DUPLICATE_OVERLAP': 'groom.near_duplicate_overlap',
    'TEMPLATED_OVERLAP': 'groom.templated_overlap',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
