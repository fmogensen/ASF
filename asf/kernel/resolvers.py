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
- ``symbol``: *whether* a backticked dotted name the question says *does not exist* (a class a
  spec's fence names) was meant to exist. The probe reads the module at origin's trunk (ast);
  absent, the session's own proposal stands; present, the answer names where it is.

:func:`match` returns one :class:`Probe` or None; the facts reader has the host probe it
(``Facts.resolved``: probe key -> result) and ``decide`` turns a result into the answer with
:func:`answer` — None when the probe is missing or unsure (an error, a timeout, a test id that does
not load, a dotted name no module holds): the question then stays an operator Stuck. Pure: nothing
here reads a disk or a network.
"""
import collections
import re

from asf.kernel import idclaims

TRUNK_TESTS, SYMBOL = 'trunk-tests', 'symbol'
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

GREEN = ('green on trunk at %s (%d tests OK): cloud-sandbox only, not a trunk red; proceed — '
         '%s run by the kernel on the factory host.')
RED = ('red on trunk at %s: %s; treat as a trunk red — run by the kernel on the factory host.')
ABSENT = ("`%s` does not exist at trunk %s (%s defines %s) — the question's premise holds; the "
          "plan's own proposal stands, proceed. Checked by the kernel.")
PRESENT = ("`%s` exists at trunk %s (%s) — the question's premise is wrong; name the existing "
           "symbol, do not create it. Checked by the kernel.")

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


def match(text):
    """The :class:`Probe` that answers ``text``, or None: an id-claim question is
    :mod:`asf.kernel.idclaims`'s; a missing-symbol question names exactly one backticked dotted
    name; a trunk-tests question asks whether, names failures, a cloud sandbox and test ids."""
    text = str(text or '')
    if not WHETHER_RE.search(text) or idclaims.is_claim_question(text):
        return None
    if NOT_EXIST_RE.search(text):
        names = list(dict.fromkeys(SYMBOL_RE.findall(text)))
        return Probe(SYMBOL, (names[0],)) if len(names) == 1 else None
    if FAIL_RE.search(text) and WHERE_RE.search(text):
        ids = test_ids(text)
        return Probe(TRUNK_TESTS, tuple(ids)) if ids else None
    return None


def _listed(names):
    names = list(names)
    more = len(names) - MAX_LISTED
    return ', '.join(names[:MAX_LISTED]) + (' and %d more' % more if more > 0 else '')


def answer(probe, result):
    """The kernel's answer to ``probe`` from its ``result`` (the host's probe), or None when the
    result is missing or unsure."""
    if not result or result.get('error') or not result.get('sha'):
        return None
    sha = str(result['sha'])[:7]
    if probe.cls == TRUNK_TESTS:
        ran, failed = int(result.get('ran') or 0), list(result.get('failed') or [])
        if not ran:
            return None
        if result.get('ok') and not failed:
            return GREEN % (sha, ran, _listed(probe.target))
        return RED % (sha, _listed(failed)) if failed else None
    if probe.cls == SYMBOL:
        if result.get('exists'):
            return PRESENT % (probe.target[0], sha, result.get('where') or '?')
        if result.get('exists') is False:
            return ABSENT % (probe.target[0], sha, result.get('path') or '?',
                             _listed(result.get('defined') or ()) or 'nothing')
    return None


def resolve(text, facts, enabled=(TRUNK_TESTS, SYMBOL)):
    """``(answer, class)`` for question ``text`` from ``facts.resolved``, or None (no match, the
    class not in ``enabled``, no or an unsure probe result)."""
    probe = match(text)
    if probe is None or probe.cls not in enabled:
        return None
    text = answer(probe, (getattr(facts, 'resolved', None) or {}).get(probe.key))
    return (text, probe.cls) if text else None
