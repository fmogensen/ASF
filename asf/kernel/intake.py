"""asf.kernel.intake — intake each tick: inbox notes become cards, undecided cards are decided
(ASF 0.2).

Pure: reads nothing but its arguments. The tick first runs the old floor's minting path
(:func:`asf.groom.inbox.process_inbox`, through the record port), so every inbox note the shape
rules can read becomes a card. Then every undecided work item (:func:`undecided`: ``decided:``
not true, not retired) is decided — by code where the facts already settle it, else by one light
``intake-decide`` session:

- **code** (:func:`shortcut`): a card whose own text names its kind and its parent — a Story or
  Task under a parent of the right type (minted by a landed spec or a plan), a Bug filed by its
  signature, a Feature whose shape was declared rather than defaulted — is decided ``need`` (its
  parent's ``priority`` when the parent carries one); an Epic minted with no parent (it has
  none, and no spec or plan lane) is decided ``need`` (its own ``priority`` when it carries
  one); a card whose work is Done is decided too.
- **session** (:func:`plan`): the rest — a note the shape rules asked a question about, a card
  whose kind was a guess (``shape: default``) — at most ``Config.intake_decide_per_tick`` a tick,
  only on seats every finishing and building launch left free, notes first, then Bugs, oldest
  first. The session judges only and ends with :data:`VERDICT_SCHEMA`; :func:`parse_verdict`
  reads it and :func:`check` validates it against the record — a block that fails either is
  rejected whole and nothing of it is applied. A key that has had ``Config.intake_max_tries``
  sessions without a valid verdict is decided by code: ``later`` (a card), never asked again.

A verdict is applied through the groom's own answer grammar (:func:`answer_words`:
``yes``/``close``/``parent <id>``/``S1|S2|S3`` on a card; :func:`note_clauses`:
``close``/``feature``/``epic``/``story``/``bug <signature>``/``parent <id>``/``S1|S2|S3`` on a note), and
``need``/``nice``/``later`` set the card's ``priority:`` (``later`` parks it in the kernel).
Each decision logs ``INTAKE <id> -> <decision>``.
"""
import dataclasses
import re

from asf.kernel import actions as A

#: the session kind that decides an undecided card or an inbox note with a question
KIND = 'intake-decide'

#: the line an intake-decide session's verdict block opens with
HEAD = 'INTAKE-DECIDE'

DECISIONS = ('need', 'nice', 'later', 'close')
KINDS = ('feature', 'bug', 'epic', 'story')
SEVERITIES = ('S1', 'S2', 'S3')

#: the block an intake-decide session ends with (its brief quotes it)
VERDICT_SCHEMA = """INTAKE-DECIDE
decision: need | nice | later | close
kind: feature | bug | epic | story   # story: an inbox note under a Feature
parent: <id or none>
severity: S1 | S2 | S3   # bugs only
reason: <one line>"""

KEYS = ('decision', 'kind', 'parent', 'severity', 'reason')

#: the key prefix of an inbox note (``inbox.<file name without .md>``): ref- and file-safe
NOTE_PREFIX = 'inbox.'

#: the item type a note is carried as in the facts
NOTE = 'note'

#: the types a card of each type may hang under (``parent:``): Epic > Feature > Story > Task, a
#: Bug under an Epic, a Feature or a Story — an Epic under nothing
PARENT_TYPES = {'feature': ('epic',), 'bug': ('epic', 'feature', 'story'),
                'story': ('feature',), 'task': ('feature', 'story'), 'epic': ()}

#: a History line's inbox shape: ``created (inbox) — shape: <rule> → <kind>``
SHAPE_RE = re.compile(r'created \(([^)]*)\) — shape: (\S+) → (\w+)')

#: the shape rule that means the kind was a guess, not named by the card
GUESSED = 'default'

#: the reason of the code's own decision once a key's sessions are spent
SPENT = 'no valid intake verdict after %d session(s): parked'

_ID_RE = re.compile(r'^[A-Z]-\d+$')


@dataclasses.dataclass
class Verdict:
    """A validated intake decision: ``decision`` (:data:`DECISIONS`), ``kind``
    (:data:`KINDS`, or the card's own type for a code decision), ``parent`` (an id, '' for
    none), ``severity`` ('' unless a Bug's), ``reason`` (one line) and ``by`` (``code`` or the
    session's job). ``set_priority``: the decision is written as the card's ``priority:`` (a
    session's verdict, and the code's ``later`` once a card's sessions are spent)."""
    decision: str
    kind: str = ''
    parent: str = ''
    severity: str = ''
    reason: str = ''
    by: str = 'code'
    set_priority: bool = False


def note_key(name):
    """The key of the inbox note ``name`` (``<slug>.md``)."""
    return NOTE_PREFIX + (name[:-3] if name.endswith('.md') else name)


def note_name(key):
    """The file name of the note ``key`` (:func:`note_key`'s inverse), '' for a card id."""
    return key[len(NOTE_PREFIX):] + '.md' if is_note(key) else ''


def is_note(key):
    return str(key or '').startswith(NOTE_PREFIX)


def shape_rule(body):
    """The inbox shape rule the card's History records (``default``, ``signature``, …), '' when
    the card was not minted from the inbox."""
    m = SHAPE_RE.search(str(body or ''))
    return m.group(2) if m else ''


def undecided(it):
    """Whether ``it`` (an Item, or a note) waits on an intake decision."""
    return it.type == NOTE or not getattr(it, 'decided', True)


def _parent_ok(kind, parent, items):
    p = items.get(parent)
    return (p is not None and p.state.value != 'done'
            and p.type in PARENT_TYPES.get(kind, ()))


def shortcut(it, items):
    """The code's own :class:`Verdict` for the undecided card ``it``, or None when a session
    must judge it (see the module doc)."""
    if it.type == NOTE:
        return None
    if it.state.value == 'done':
        return Verdict('need', it.type, it.parent or '', '', 'its work is done')
    rule = shape_rule(it.body)
    if it.type == 'epic':
        if it.parent:
            return None  # an Epic has no parent: a session judges it, and check() refuses one
        own = str(it.priority or '').lower()
        return Verdict(own if own in ('need', 'nice', 'later') else 'need', 'epic', '', '',
                       'an Epic, with no parent')
    named = (it.type in ('story', 'task') or (it.type == 'bug' and bool(it.signature))
             or (it.type == 'feature' and rule not in ('', GUESSED)))
    if not named or rule == GUESSED or not it.parent or not _parent_ok(it.type, it.parent, items):
        return None
    inherited = str(items[it.parent].priority or '').lower()
    return Verdict(inherited if inherited in ('need', 'nice', 'later') else 'need', it.type,
                   it.parent, '', 'kind %s and parent %s named on the card'
                   % (it.type, it.parent))


def spent(key, tries, config):
    """The code's decision for a card whose sessions are spent, or None (a note, or tries
    left)."""
    if is_note(key) or tries < config.intake_max_tries:
        return None
    return Verdict('later', reason=SPENT % tries, set_priority=True)


# ---- the verdict --------------------------------------------------------------------------------

def _uncomment(value):
    return re.split(r'\s+#\s', ' ' + str(value or ''), maxsplit=1)[0].strip()


def _plain(value):
    text = _uncomment(value).strip('`"\'').strip()
    return '' if text.lower() in ('', 'none', 'n/a', '-', '—') else text


def block(text):
    """``{key: value}`` of the last :data:`HEAD` block in ``text``, or None when it has none."""
    lines = str(text or '').splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().strip('`') == HEAD]
    if not starts:
        return None
    out = {}
    for ln in lines[starts[-1] + 1:]:
        s = ln.strip()
        if s.startswith('```'):
            break
        m = re.match(r'^([a-z_]+)\s*:\s*(.*)$', s)
        if m and m.group(1) in KEYS:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_verdict(text, by=''):
    """``(Verdict, '')`` from a session's report, or ``(None, why)``: no :data:`HEAD` block, or
    a field outside its words. Nothing of a rejected block is applied."""
    got = block(text)
    if got is None:
        return None, 'no %s block' % HEAD
    decision = _plain(got.get('decision')).lower()
    if decision not in DECISIONS:
        return None, 'decision %r is not one of %s' % (decision, ' | '.join(DECISIONS))
    kind = _plain(got.get('kind')).lower()
    if kind not in KINDS:
        return None, 'kind %r is not one of %s' % (kind, ' | '.join(KINDS))
    parent = _plain(got.get('parent')).upper()
    if parent and not _ID_RE.match(parent):
        return None, 'parent %r is not an id or none' % parent
    severity = _plain(got.get('severity')).upper()
    if kind != 'bug':
        severity = ''  # bugs only: a feature's severity is dropped, never an error
    elif severity and severity not in SEVERITIES:
        return None, 'severity %r is not one of %s' % (severity, ' | '.join(SEVERITIES))
    reason = ' '.join(_plain(got.get('reason')).split())
    if not reason:
        return None, 'reason: missing'
    return Verdict(decision, kind, parent, severity, reason, by or 'session', True), ''


def check(v, it, items):
    """Why the verdict ``v`` on ``it`` (a card or a note) cannot be applied, '' when it can: a
    parent must be on the record, not Done, and of a type the kind hangs under; a note going to
    be minted needs a parent unless it is a Bug (the product's default Bug Epic takes it)."""
    if v.decision == 'close':
        return ''
    if v.kind == 'epic' and it.type not in (NOTE, 'epic'):
        return 'a %s is not judged an epic — only an inbox note becomes one' % it.type
    if v.kind == 'story' and it.type not in (NOTE, 'story'):
        return 'a %s is not judged a story — only an inbox note becomes one' % it.type
    kind = v.kind if it.type in (NOTE, 'feature', 'bug') else it.type
    if kind == 'epic' and v.parent:
        return 'an epic has no parent (%s named)' % v.parent
    if v.parent:
        if v.parent not in items:
            return 'parent %s is not on the record' % v.parent
        if items[v.parent].state.value == 'done':
            return 'parent %s is Done' % v.parent
        if items[v.parent].type not in PARENT_TYPES.get(kind, ()):
            return 'parent %s is a %s — a %s hangs under %s' % (
                v.parent, items[v.parent].type, kind,
                ' or '.join(PARENT_TYPES.get(kind, ())) or 'nothing')
    elif it.type == NOTE and kind == 'feature':
        return 'a feature note needs a parent Epic'
    elif it.type == NOTE and kind == 'story':
        return 'a story note needs a parent Feature'
    return ''


def correct(v, it, items):
    """``(Verdict, why)``: the verdict ``v`` on ``it`` corrected by code when :func:`check`
    refused it and the fix is determined, else ``(None, '')``. One case: a note judged a Feature
    under an open Feature is a Story under that Feature (Epic > Feature > Story)."""
    p = items.get(v.parent) if v.parent else None
    if (it is not None and it.type == NOTE and v.decision != 'close' and v.kind == 'feature'
            and p is not None and p.type == 'feature' and p.state.value != 'done'):
        return (dataclasses.replace(v, kind='story'),
                'a note under Feature %s is a Story under it' % v.parent)
    return None, ''


# ---- the groom's grammar ------------------------------------------------------------------------

def answer_words(it, v):
    """The groom answer words (:func:`asf.groom.groom._parse_answer`) that carry ``v`` onto the
    card ``it``: ``close — <reason>``, else ``parent <id>`` when it moves, ``S1|S2|S3`` on a Bug
    whose severity changes, and ``yes``."""
    if v.decision == 'close':
        return ['close — intake: %s' % v.reason]
    words = []
    if v.parent and v.parent != (it.parent or ''):
        words.append('parent %s' % v.parent)
    if it.type == 'bug' and v.severity and v.severity != (getattr(it, 'severity', '') or ''):
        words.append(v.severity)
    return words + ['yes']


def note_clauses(it, v):
    """The inbox answer (:func:`asf.groom.inbox.parse_answer`) that carries ``v`` onto the note
    ``it``: ``close``, else ``feature`` or ``bug <signature>`` (its title), ``parent <id>`` and
    a Bug's ``S1|S2|S3``, ``;``-separated."""
    if v.decision == 'close':
        return 'close'
    clauses = ['bug %s' % (' '.join(str(it.title or '').split()) or 'untitled defect')
               if v.kind == 'bug' else v.kind if v.kind in ('epic', 'story') else 'feature']
    if v.parent:
        clauses.append('parent %s' % v.parent)
    if v.kind == 'bug' and v.severity:
        clauses.append(v.severity)
    return '; '.join(c.replace(';', ',') for c in clauses)


def priority_of(v):
    """The ``priority:`` the verdict sets: '' for ``close`` and for a code decision that leaves
    the priority its parent gives it."""
    return v.decision if v.set_priority and v.decision in ('need', 'nice', 'later') else ''


def line(key, v):
    """``INTAKE <key> -> <decision>`` with who decided and why."""
    return 'INTAKE %s -> %s (%s: %s)' % (key, v.decision, v.by, v.reason)


# ---- the tick's plan ----------------------------------------------------------------------------

def _order(it):
    return (it.type != NOTE, it.type != 'bug', it.created or '~', it.id)


def plan(facts, config, launched=(), free=0, parked=()):
    """``(decisions, launches)`` of one tick: ``decisions`` are ``(key, Verdict)`` the code
    makes now; ``launches`` the keys an intake-decide session starts for, at most
    ``config.intake_decide_per_tick`` and ``free`` (the seats every other launch left).
    ``launched``: the items another launch takes this tick (asked next tick, not now).
    ``parked``: the ids parked (``priority: later`` on it or an ancestor) — such a card is
    decided ``later`` by code, never asked. A key with a live session is left alone."""
    if not config.intake:
        return [], []
    items = facts.items
    notes = getattr(facts, 'notes', None) or {}
    tries = getattr(facts, 'intake_tries', None) or {}
    busy = {s.item_id for s in facts.sessions if s.alive}
    decisions, asks = [], []
    for it in sorted(list(items.values()) + list(notes.values()), key=_order):
        if not undecided(it) or it.id in busy:
            continue
        n = tries.get(it.id, 0)
        v = shortcut(it, items)
        if v is None and it.id in parked:
            v = Verdict('later', reason='parked (priority: later on it or an ancestor)')
        if v is None and n >= config.intake_max_tries:
            v = spent(it.id, n, config)
            if v is None:
                continue  # a note whose sessions are spent stays in the inbox, asked no more
        if v is not None:
            decisions.append((it.id, v))
        elif it.id not in launched:
            asks.append(it.id)
    budget = 0 if facts.paused else max(0, min(config.intake_decide_per_tick, free))
    return decisions, asks[:budget]


def goals(items):
    """The product's goals and rank context for a brief: each open Epic, ranked first (by rank),
    then unranked — ``<id> (rank <n>, <priority>) <title>``."""
    open_epics = [it for it in items.values() if it.type == 'epic' and it.state.value != 'done']
    open_epics.sort(key=lambda e: (e.rank is None, e.rank or 0, e.id))
    out = []
    for e in open_epics:
        tags = ['rank %d' % e.rank if e.rank is not None else 'unranked']
        if e.priority:
            tags.append(str(e.priority))
        out.append('%s (%s) %s' % (e.id, ', '.join(tags), e.title))
    return out


def operator_clauses(text):
    """``(clauses, '')``: the operator's answer to a Stuck note as the inbox's own clauses
    (:func:`note_clauses`' grammar) — ``kind: story; parent: F-0001``, ``story, parent F-0001``,
    ``close`` — or ``('', why)`` when a part is outside it."""
    from asf.groom import inbox
    words = []
    for part in re.split(r'[;,\n]', str(text or '')):
        part = ' '.join(part.split()).strip('`"\'. ')
        if not part:
            continue
        m = re.match(r'^(kind|type|parent|severity)\s*:?\s*(.+)$', part, re.IGNORECASE)
        if m:
            key, value = m.group(1).lower(), m.group(2).strip()
            part = value if key in ('kind', 'type', 'severity') else 'parent %s' % value.upper()
        words.append(part)
    clauses = '; '.join(words)
    if not clauses:
        return '', 'no answer'
    parsed, why = inbox.parse_answer(clauses)
    return (clauses, '') if parsed is not None else ('', why or 'not an inbox answer')


# ---- never silent -------------------------------------------------------------------------------

#: the line a note whose sessions are spent is Stuck with (on the operator)
SPENT_NOTE = ('%d intake-decide session(s) gave no valid verdict — %s; answer it: asf answer '
              '%s --text "kind: feature|bug|epic|story; parent: <id>" (or close)')


def unheard(facts, config, actions):
    """``[(key, owner, reason)]``: each inbox note this tick neither decides, launches, nor finds
    in a live session — never silent. ``owner`` is ``operator`` for a note whose sessions are
    spent (Stuck, the reason its question and the last rejection), '' for one that waits (on a
    seat, a paused factory, or the launch budget)."""
    if not config.intake:
        return []
    notes = getattr(facts, 'notes', None) or {}
    tries = getattr(facts, 'intake_tries', None) or {}
    heard = {a.item_id for a in actions if isinstance(a, A.Decide)
             or (isinstance(a, A.Launch) and a.kind == KIND)}
    busy = {s.item_id for s in facts.sessions if s.alive}
    launched = sum(1 for a in actions if isinstance(a, A.Launch))
    asked = sum(1 for a in actions if isinstance(a, A.Launch) and a.kind == KIND)
    out = []
    for key in sorted(notes):
        if key in heard or key in busy:
            continue
        n = tries.get(key, 0)
        q = ' '.join(str(notes[key].question or 'no question').split())
        if n >= config.intake_max_tries:
            out.append((key, 'operator', SPENT_NOTE % (n, q, key)))
        elif facts.paused:
            out.append((key, '', 'waits: launches are paused'))
        elif asked >= config.intake_decide_per_tick:
            out.append((key, '', 'waits: %d intake-decide launch(es) a tick, all taken'
                        % config.intake_decide_per_tick))
        else:
            out.append((key, '', 'waits for a free seat: %d launch(es) this tick took every '
                        'seat' % launched))
    return out


def unheard_line(key, owner, reason):
    """``INTAKE STUCK <key> [operator] <reason>`` or ``INTAKE WAIT <key> <reason>``."""
    if owner:
        return 'INTAKE STUCK %s [%s] %s' % (key, owner, reason)
    return 'INTAKE WAIT %s %s' % (key, reason)
