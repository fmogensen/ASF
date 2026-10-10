"""asf.kernel.resolvers — a session's fact-checkable question, answered from a trunk probe (ASF 0.2).

A session that ends on ``NEEDS OPERATOR:`` sometimes asks a question the factory host can answer
with a fact, not a judgement. Code, not the console, answers those (the operator's rule: "code
driven, not LLM guesswork"), on the pattern of :mod:`asf.kernel.idclaims`. Two classes, each
matched narrowly by :func:`match`:

- ``trunk-tests``: *whether* named tests that *fail* (are red) in a *cloud* sandbox / container
  are red on the factory host too. The probe runs the named test ids (a class, or the methods it
  lists, :func:`test_ids`) with ``python -m unittest`` on a fresh detached worktree of origin's
  trunk (:class:`asf.kernel.trunk.TrunkProbe`); the answer is "green on trunk at <sha> (N tests
  OK): cloud-sandbox only, not a trunk red; proceed" or "red on trunk at <sha>: <ids>; treat as a
  trunk red".
- ``gate``: a repo gate script the session's runtime refused to run, to *confirm on the host*:
  the probe runs it — only a step of the product's ``pre_push_check`` — on a fresh detached
  worktree of the session's branch head; the answer is its last line and the sha.
- ``inbox-bug``: an order to *mint* a Bug card the session could not (*no record* in its
  sandbox) with the ``asf new bug --title`` it would have run: ``decide`` files it through the
  record's inbox (:class:`asf.kernel.actions.FileInbox`) and, once the record holds it, answers
  "filed as inbox/<file>; do not mint reserved ids".
- ``symbol``: *whether* a backticked dotted name the question says *does not exist* (a class a
  spec's fence names) was meant to exist. The probe reads the module at origin's trunk (ast);
  absent, the session's own proposal stands; present, the answer names where it is.
- ``needs-writes``: a REPORT's ``needs writes: <paths>`` outside the card's ``writes:``
  (:func:`needs_writes`, ``kernel.resolve.needs_writes``): a path an unfinished ``after:`` item
  writes is held ("hold: <path> belongs to <id>, wait for it"); else the paths are granted — the
  card's ``writes:`` widened through the record's writer — and a Building/Review writer of one is
  named (the overlap rule serialises them). Read off the session's fields, not probed.

:func:`match` returns one :class:`Probe` or None; the facts reader has the host probe it
(``Facts.resolved``: probe key -> result) and ``decide`` turns a result into the answer with
:func:`answer` — None when the probe is missing or unsure (an error, a timeout, a test id that does
not load, a dotted name no module holds): the question then stays an operator Stuck. Pure: nothing
here reads a disk or a network.
"""
import collections
import re

from asf.kernel import idclaims
from asf.kernel.model import State

TRUNK_TESTS, SYMBOL, GATE, INBOX_BUG = 'trunk-tests', 'symbol', 'gate', 'inbox-bug'
#: the classes the host's trunk probe answers (:mod:`asf.kernel.trunk`); ``inbox-bug`` is read off
#: the record
PROBED = (TRUNK_TESTS, SYMBOL, GATE)
#: the class an id-claim answer (:mod:`asf.kernel.idclaims`) is logged under
ID_CLAIM = 'id-claim'

#: the most test ids one question may name and still be probed
MAX_IDS = 20


class Probe(collections.namedtuple('Probe', 'cls target')):
    """One probe: its class and the names it checks (test ids, or one dotted symbol)."""
    __slots__ = ()

    @property
    def key(self):
        return '%s:%s' % (self.cls, ','.join(self.target))


WHETHER_RE = re.compile(r'\bwhether\b', re.I)
FAIL_RE = re.compile(r'\b(?:fail(?:s|ed|ing|ures?)?|red)\b', re.I)
WHERE_RE = re.compile(r'\b(?:cloud|containers?|sandbox(?:es)?|factory host)\b', re.I)
NOT_EXIST_RE = re.compile(r"\b(?:does not|doesn't|did not|no longer) exist\b|\bis not defined\b",
                          re.I)

#: a dotted test id with a ``test_*`` module (``tests.test_workers.TestSpawn``)
TEST_ID_RE = re.compile(r'(?<![\w./-])((?:[A-Za-z_]\w*\.)+test_\w+(?:\.[A-Za-z_]\w*)*)')
#: a bare test method (``test_a_review…``), never one cut by a recorded reason's ``…``
METHOD_RE = re.compile(r'(?<![\w./-])(test_\w+)(?![\w.…/-])')
#: a bare test class (``TestSpawn``)
CLASS_RE = re.compile(r'(?<![\w./-])(Test[A-Z]\w*)(?![\w.…/-])')
#: a backticked dotted name, e.g. a class a spec fence names
SYMBOL_RE = re.compile(r'`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)`')

#: the session could not run a repo gate script and asks the host to
NOT_RUN_RE = re.compile(r'\b(?:was not run|not run directly|could not run|cannot run|refused)\b',
                        re.I)
HOST_RE = re.compile(r'\bconfirm\b[^.]*\bon the (?:factory )?host\b', re.I)
#: a backticked command (a word, a space, more)
COMMAND_RE = re.compile(r'`([a-z][\w.-]* [^`\n]+)`')
#: the session asks for a Bug card it could not mint: an order (mint, file, create) …
MINT_RE = re.compile(r'^(?:\w+: )?(?:NEEDS OPERATOR: )?(?:mint|file|create)\b[^.]*\bBug card\b',
                     re.I)
#: … because no record is reachable from its sandbox …
NO_RECORD_RE = re.compile(r'\bno record\b|\bcannot\b[^.]*\brecord\b', re.I)
#: … with the title it would have used
NEW_BUG_RE = re.compile(r'`asf new bug\b[^`]*?--title\s+(?:"([^"`]+)"|\'([^\'`]+)\')')

GREEN = ('green on trunk at %s (%d tests OK): cloud-sandbox only, not a trunk red; proceed — '
         '%s run by the kernel on the factory host.')
RED = ('red on trunk at %s: %s; treat as a trunk red — run by the kernel on the factory host.')
ABSENT = ("`%s` does not exist at trunk %s (%s defines %s) — the question's premise holds; the "
          "plan's own proposal stands, proceed. Checked by the kernel.")
PRESENT = ("`%s` exists at trunk %s (%s) — the question's premise is wrong; name the existing "
           "symbol, do not create it. Checked by the kernel.")

GATE_GREEN = 'Confirmed on the factory host: `%s` on %s at %s -> %s. Proceed.'
GATE_RED = ('Run on the factory host: `%s` on %s at %s -> red (rc %d) -> %s; fix it before the '
            'push.')
FILED = ('Filed by the kernel through the inbox as %s (groom assigns its id); do not mint reserved '
         'ids. Proceed.')
INBOX_BODY = ('Bug filed by the kernel for %s: its session could not mint the card itself (a cloud '
              'session has no record), so it asked the operator.\n\nThe question:\n\n> %s\n')

#: the most names an answer lists
MAX_LISTED = 12


def _is_module(tid):
    return tid.rsplit('.', 1)[-1].startswith('test_') and tid == tid.lower()


def _is_class(tid):
    return tid.rsplit('.', 1)[-1][:1].isupper()


def test_ids(text):
    """The test ids ``text`` names, most specific: a module narrowed by a bare ``Test*`` class it
    names, a single class by the bare ``test_*`` methods it lists (unless the text is cut,
    ``…``); an id another one extends is dropped. None when there is none or too many."""
    ids = list(dict.fromkeys(TEST_ID_RE.findall(text)))
    if not ids:
        return None
    classes = list(dict.fromkeys(CLASS_RE.findall(text)))
    mods = [i for i in ids if _is_module(i)]
    if len(mods) == 1 and classes and not any(i.startswith(mods[0] + '.') for i in ids):
        ids = [i for i in ids if i != mods[0]] + ['%s.%s' % (mods[0], c) for c in classes]
    methods = list(dict.fromkeys(METHOD_RE.findall(text)))
    cls_ids = [i for i in ids if _is_class(i)]
    if methods and len(cls_ids) == 1 and not text.rstrip().endswith('…'):
        ids = [i for i in ids if i != cls_ids[0]] + ['%s.%s' % (cls_ids[0], m) for m in methods]
    ids = [i for i in dict.fromkeys(ids) if not any(o.startswith(i + '.') for o in ids)]
    return ids if 0 < len(ids) <= MAX_IDS else None


def match(text, branch=''):
    """The :class:`Probe` that answers ``text``, or None: an id-claim question is
    :mod:`asf.kernel.idclaims`'s; an order to mint a Bug card the session could not (no record)
    with the ``asf new bug --title`` it would have run is ``inbox-bug``; a gate script the session
    could not run, to confirm on the host, is ``gate`` on ``branch`` (none: no match); then,
    asked "whether", a missing symbol names exactly one backticked dotted name, and a trunk-tests
    question names failures, a cloud sandbox and test ids."""
    text = str(text or '')
    if idclaims.is_claim_question(text):
        return None
    if MINT_RE.search(text.strip()) and NO_RECORD_RE.search(text):
        titles = list(dict.fromkeys(a or b for a, b in NEW_BUG_RE.findall(text)))
        return Probe(INBOX_BUG, (titles[0].strip(),)) if len(titles) == 1 else None
    if NOT_RUN_RE.search(text) and HOST_RE.search(text):
        cmds = list(dict.fromkeys(' '.join(c.split()) for c in COMMAND_RE.findall(text)))
        return Probe(GATE, (cmds[0], branch)) if len(cmds) == 1 and branch else None
    if not WHETHER_RE.search(text):
        return None
    if NOT_EXIST_RE.search(text):
        names = list(dict.fromkeys(SYMBOL_RE.findall(text)))
        return Probe(SYMBOL, (names[0],)) if len(names) == 1 else None
    if FAIL_RE.search(text) and WHERE_RE.search(text):
        ids = test_ids(text)
        return Probe(TRUNK_TESTS, tuple(ids)) if ids else None
    return None


def branch_of(item_id, sessions, prs=()):
    """The branch a ``gate`` question of ``item_id`` is run on: its latest session's (by
    ``started``), else its open PR's; '' when none is known."""
    own = sorted((s for s in sessions if s.item_id == item_id and s.branch),
                 key=lambda s: s.started or '')
    if own:
        return own[-1].branch
    return next((p.branch for p in prs if p.item_id == item_id and not p.merged), '')


def inbox_card(probe, text, item_id):
    """``(title, body)`` of the inbox card an ``inbox-bug`` question asks for: its title, the
    question, and the test ids it names (``Cases:``)."""
    body = INBOX_BODY % (item_id, ' '.join(str(text or '').split()))
    ids = test_ids(str(text or ''))
    if ids:
        body += '\nCases: %s.\n' % ', '.join(ids)
    return probe.target[0], body


def _listed(names):
    names = list(names)
    more = len(names) - MAX_LISTED
    return ', '.join(names[:MAX_LISTED]) + (' and %d more' % more if more > 0 else '')


def answer(probe, result):
    """The kernel's answer to ``probe`` from its ``result`` (the host's probe), or None when the
    result is missing or unsure."""
    if not result or result.get('error'):
        return None
    if probe.cls == INBOX_BUG:
        return FILED % result['filed'] if result.get('filed') else None
    if not result.get('sha'):
        return None
    sha = str(result['sha'])[:7]
    if probe.cls == TRUNK_TESTS:
        ran, failed = int(result.get('ran') or 0), list(result.get('failed') or [])
        if not ran:
            return None
        if result.get('ok') and not failed:
            return GREEN % (sha, ran, _listed(probe.target))
        return RED % (sha, _listed(failed)) if failed else None
    if probe.cls == GATE:
        last = str(result.get('last') or '').strip()
        if 'rc' not in result or not last:
            return None
        cmd, branch = probe.target
        if int(result['rc']) == 0:
            return GATE_GREEN % (cmd, branch, sha, last)
        return GATE_RED % (cmd, branch, sha, int(result['rc']), last)
    if probe.cls == SYMBOL:
        if result.get('exists'):
            return PRESENT % (probe.target[0], sha, result.get('where') or '?')
        if result.get('exists') is False:
            return ABSENT % (probe.target[0], sha, result.get('path') or '?',
                             _listed(result.get('defined') or ()) or 'nothing')
    return None


def resolve(text, facts, enabled=(TRUNK_TESTS, SYMBOL), branch=''):
    """``(answer, class)`` for question ``text`` from ``facts.resolved``, or None (no match, the
    class not in ``enabled``, no or an unsure probe result). ``branch``: a ``gate`` question's."""
    probe = match(text, branch)
    if probe is None or probe.cls not in enabled:
        return None
    text = answer(probe, (getattr(facts, 'resolved', None) or {}).get(probe.key))
    return (text, probe.cls) if text else None


def to_file(text, facts, item_id, enabled=(INBOX_BUG,)):
    """``(title, body)`` of the inbox card question ``text`` asks for when the record does not
    hold it yet (``facts.resolved`` says ``filed: ''``), else None."""
    probe = match(text)
    if probe is None or probe.cls != INBOX_BUG or probe.cls not in enabled:
        return None
    result = (getattr(facts, 'resolved', None) or {}).get(probe.key)
    if result is None or result.get('filed') or result.get('error'):
        return None
    return inbox_card(probe, text, item_id)


# ---- needs-writes: a widening of the card's ``writes:`` a session asked for ---------------------

#: the class a ``needs writes:`` answer is logged under
NEEDS_WRITES = 'needs-writes'

#: where a ``needs writes:`` value's paths end and its prose begins (a spaced dash)
PROSE_RE = re.compile(r'\s(?:—|–|--?)\s')
#: a recorded Stuck reason that carries the REPORT's ``needs writes:`` value
NEEDS_RE = re.compile(r'\bneeds writes:\s*(.+)$', re.I | re.S)
#: a repo path token: a directory part or an extension, no scheme
PATH_TOKEN_RE = re.compile(r'^(?![a-z]+://)[\w.@+*-]+(?:/[\w.@+*-]+)*$')
NONE_TOKEN_RE = re.compile(r'^(?:none|n/a|-|—)$', re.I)

HOLD = ('hold: %s belongs to %s, wait for it — no writes granted; this card waits on it through '
        'after:. Decided by the kernel.')
GRANTED = 'granted %s; nothing else outside writes (card updated by the kernel).'
SERIALISED = ' %s also writes %s (%s): the overlap rule serialises them.'
HIGH_RISK = ' %s matches kernel.risk.high: this item is high-risk now.'


def requested_writes(value, writes, covers=None):
    """The paths a ``needs writes:`` ``value`` names (before any prose after a spaced dash) that
    the card's ``writes`` globs do not cover yet, in order; [] for none."""
    import fnmatch
    covers = covers or (lambda p, globs: any(fnmatch.fnmatchcase(p, g) for g in globs))
    head = PROSE_RE.split(str(value or ''), maxsplit=1)[0]
    out = []
    for tok in re.split(r'[\s,;]+', head):
        tok = tok.strip('`\'"()[]')
        if (not tok or NONE_TOKEN_RE.match(tok) or not PATH_TOKEN_RE.match(tok)
                or ('/' not in tok and '.' not in tok)):
            continue
        if tok not in out and not covers(tok, list(writes)):
            out.append(tok)
    return out


def needs_writes_of(text):
    """The ``needs writes:`` value a recorded Stuck reason (or question) carries, or ''."""
    m = NEEDS_RE.search(str(text or ''))
    return m.group(1).strip() if m else ''


def needs_writes(it, paths, items, unfinished, overlap, risk_high=()):
    """``(answer, granted paths)`` for ``it``'s request to widen its ``writes:`` by ``paths``:

    1. a path in the ``writes`` of an item ``it`` has ``after:`` on and that is ``unfinished``
       (a callable on the id): hold, nothing granted — ``it`` waits through that edge;
    2. else a path in the ``writes`` of a Building or Review item: granted, named in the answer
       (the kernel's overlap rule serialises them);
    3. else: granted.

    ``overlap(a, b)`` says whether two glob lists can name a common path; ``risk_high`` are the
    ``kernel.risk.high`` globs a granted path is checked against. None when ``paths`` is empty."""
    paths = list(paths)
    if not paths:
        return None
    for p in paths:
        for dep in it.after:
            d = items.get(dep)
            if dep != it.id and d is not None and unfinished(dep) and overlap([p], list(d.writes)):
                return HOLD % (p, dep), []
    text = GRANTED % ' '.join(paths)
    for x in sorted(items):
        o = items[x]
        if x == it.id or o.state not in (State.BUILDING, State.REVIEW):
            continue
        hit = [p for p in paths if overlap([p], list(o.writes))]
        if hit:
            text += SERIALISED % (x, ' '.join(hit), o.state.value.title())
    risky = [p for p in paths if risk_high and overlap([p], list(risk_high))]
    if risky:
        text += HIGH_RISK % ' '.join(risky)
    return text, paths
