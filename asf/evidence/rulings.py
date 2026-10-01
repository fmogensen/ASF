"""asf.evidence.rulings — an adjudicator's ruling binds every later review of the same item.

An adjudicate session ends a dispute: each open finding is *upheld* (and fixed) or *overruled*,
and the tick files its ``ruling:`` paragraph on the item's card as a ``## History`` line
(:func:`asf.tick.step_health.file_rulings`): ``- <stamp> adjudicate (<job>): <ruling>``. Until
now nothing read it back. A later review of the same item re-raised the point the ruling had
settled, the lane sent the branch back on it, the correction had nothing to fix, and the round
cap sent it to adjudication again (a product's T-0042, review rounds 7–9 on 2026-09-30: 11
sessions in 24h over a stray-files point "already adjudicated non-blocking" twice).

So the rulings are binding in two places:

- the review, correct and fixer briefs carry the item's standing rulings verbatim
  (:func:`brief_section`): the reviewer knows what is settled before it writes a C;
- the lane's merge gate asks :func:`reraised_only` of a current changes-requested review: when
  every C item it raises is a point a standing ruling settled — the item cites the ruling
  (names its job, or says the point was already ruled / adjudicated non-blocking / overruled),
  or a ruling's overruling sentence names its C id over the same file, or the ruling quotes its
  text — the review neither sends the branch BACK nor counts as a round. One C item no ruling
  covers is a genuinely new defect, and the review blocks as before.
"""
import os
import re

#: A ruling's ``## History`` line, as :func:`asf.tick.step_health.file_rulings` writes it.
HISTORY_RULING_RE = re.compile(
    r'^\s*-\s*(?P<at>\d{4}-\d\d-\d\d[ T]\d\d:\d\d)\s+adjudicate\s+\((?P<job>[^)\s]+)\):\s*'
    r'(?P<text>\S.*)$', re.M)
#: A C item that says itself it re-raises a settled point.
CITES_RULING_RE = re.compile(
    r'already\s+(?:been\s+)?(?:ruled|adjudicated|overruled)'
    r'|(?:ruled|adjudicated)\s+(?:as\s+)?non[- ]?blocking'
    r'|overruled\s+(?:by|in|at|per)\b'
    r'|per\s+the\s+(?:standing\s+)?(?:adjudicat\w*|ruling)', re.I)
#: A ruling sentence that settles a finding without an edit.
OVERRULES_RE = re.compile(r'overrul|non[- ]?blocking|not\s+(?:a\s+)?(?:defect|blocking|blocker)'
                          r'|not\s+a\s+finding', re.I)
C_ID_RE = re.compile(r'\bC(\d+)\b')
#: A repo path in prose (``path/to/file.ext``, ``path:12``).
PATH_RE = re.compile(r'[\w.@-]*[\w@-]/[\w./@\[\]()-]*\.[A-Za-z0-9]+')
#: The shortest C-item text a ruling's quote of it is matched on.
MIN_QUOTE = 30
#: The newest rulings a brief quotes (older ones are named by count).
BRIEF_MAX = 10


def _card_file(product, item):
    """The card file of ``item`` (an id or an index entry) under ``product.backlog_dir``."""
    from asf.record.core import TYPES
    root = getattr(product, 'backlog_dir', None) if product is not None else None
    entry = item if isinstance(item, dict) else {'id': item}
    iid = str(entry.get('id') or '')
    if not root or not iid:
        return None
    folder = entry.get('folder') or next(
        (f for _t, (f, p) in TYPES.items() if iid.startswith(p + '-')), None)
    return os.path.join(root, folder, f'{iid}.md') if folder else None


def parse(text):
    """The rulings a card's text carries, oldest first: ``[{at, job, text}]``."""
    return [{'at': m.group('at'), 'job': m.group('job'), 'text': m.group('text').strip()}
            for m in HISTORY_RULING_RE.finditer(text or '')]


def standing(product, item):
    """The standing rulings of ``item`` (an id or an index entry), read off its card — ``[]``
    when the card cannot be read: no ruling is no binding, never an error."""
    path = _card_file(product, item)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            return parse(f.read())
    except OSError:
        return []


def _norm(text):
    return ' '.join(re.sub(r'[`*_>#]', ' ', text or '').lower().split())


def _sentences(text):
    return [s for s in re.split(r'(?<=[.;])\s+(?=[(A-Z0-9])', text or '') if s.strip()]


def covers(rulings, label, text):
    """The job of the standing ruling that settled the C item ``label`` (``C2``, or '') with
    ``text``, or None (see the module's rules)."""
    if not rulings:
        return None
    if CITES_RULING_RE.search(text or ''):
        return rulings[-1]['job']
    for r in reversed(rulings):
        if r['job'] and r['job'] in (text or ''):
            return r['job']
    paths = set(PATH_RE.findall(text or ''))
    quote = _norm(re.sub(r'^\s*(?:[-*]\s*)?(?:\*\*)?(?:#{3,4}\s*)?C?\d+(?:\*\*)?[.):]?\s*', '',
                         text or ''))[:120]
    n = (C_ID_RE.match(label or '') or [None, None])[1]
    for r in reversed(rulings):
        if len(quote) >= MIN_QUOTE and quote in _norm(r['text']):
            return r['job']
        if not n:
            continue
        for s in _sentences(r['text']):
            if n in C_ID_RE.findall(s) and OVERRULES_RE.search(s):
                named = set(PATH_RE.findall(s))
                if not named or named & paths:
                    return r['job']
    return None


#: A C item's opening line: ``C1.``, ``- **C2**``, ``### C3``, ``1.``.
C_OPEN_RE = re.compile(r'^\s*(?:[-*]\s*)?(?:\*\*)?(?:#{3,4}\s*)?(?P<label>C?\d+)(?:\*\*)?[.):]?'
                       r'(?:\s+|$)')


#: A C item labelled as one outside a C section: ``C1.``, ``- **C2**:``, ``### C3 —``.
C_LABELLED_RE = re.compile(r'^\s*(?:[-*]\s*)?(?:\*\*)?(?:#{3,4}\s*)?(?P<label>C\d+)(?:\*\*)?'
                           r'\s*[.):\u2014-]')


def c_entries(body):
    """The C items of a review: ``[(label, text)]``, each item's text its opening line and the
    lines under it up to the next item. Read under the C heading
    (:data:`asf.evidence.review.C_HEADING_RE`), else from the lines labelled ``C<n>``."""
    from asf.evidence import review as review_mod
    body = body or ''
    h = review_mod.C_HEADING_RE.search(body)
    lines = body[h.end():].splitlines() if h else body.splitlines()
    opens = C_OPEN_RE if h else C_LABELLED_RE
    out = []
    for ln in lines:
        m = opens.match(ln)
        if m:
            label = m.group('label')
            out.append([label if label.upper().startswith('C') else f'C{label}', ln.strip()])
        elif h and ln.lstrip().startswith('#'):
            break  # the next section: the C list is over
        elif h and out and ln.strip():
            out[-1][1] += ' ' + ln.strip()
    return [(lab, text) for lab, text in out]


def reraised_only(body, rulings):
    """The jobs of the standing ``rulings`` that settled every C item of a changes-requested
    review ``body``, when it only re-raises settled points — else None: no standing ruling, no
    C item, or one C item no ruling covers (a genuinely new defect: the review blocks)."""
    if not rulings:
        return None
    entries = c_entries(body)
    if not entries:
        return None
    jobs = []
    for label, text in entries:
        job = covers(rulings, label, text)
        if not job:
            return None
        jobs.append(job)
    return list(dict.fromkeys(jobs))


def brief_section(rulings):
    """The standing rulings, verbatim, for a review/correct/fixer brief — ``''`` when none."""
    if not rulings:
        return ''
    shown = rulings[-BRIEF_MAX:]
    older = len(rulings) - len(shown)
    lines = ['STANDING RULINGS ON THIS ITEM — BINDING. An adjudicator has ruled on the points '
             'below; each ruling stands for every later round. A point a ruling settled is not '
             'a finding: never raise it as a C again (name the ruling in an I if you must '
             'mention it), and never spend a correction on it. A C list that only re-raises a '
             'ruled point does not hold the branch. A genuinely new defect still is a C.'
             + (f' ({older} older ruling(s) not shown: they are on the card\'s History.)'
                if older else ''), '']
    for r in shown:
        lines.append(f"- {r['at']} {r['job']}: {r['text']}")
    return '\n'.join(lines)
