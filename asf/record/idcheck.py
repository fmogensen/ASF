"""asf.record.idcheck — a landed plan/spec's new ids, and its Tasks' content, checked at land.

**Ids.** A session mints ids only from the block claimed for it (:mod:`asf.record.idclaim`).
A doc that lands citing an S-/T-/B- id the record lacks is citing an id it minted: that id must
sit inside a claimed block — the block of the doc's own lane session when one is known (its
claimant names the item), else any claim. An id no claim covers was invented, and a heading
that declares an id the record already holds for another card (``### T-0123: <other title>``)
reuses a taken one; either refuses the doc before a card exists. When the claim ledger holds no
claim for a prefix (claims off, no origin, a product before claims), its ids are not checked.

**Content.** A new Task whose ``(parent, stories, writes)`` equals an open Task's is the same
work twice: :func:`duplicate_task` names the existing id.
"""
import re

from asf.record import idclaim
from asf.record.core import is_open

PREFIXES = ('S', 'T', 'B')
ID_TOKEN = re.compile(r'\b([STB])-(\d{4,})\b')
DECL = re.compile(r'^[ \t]*#{1,6}[ \t]+\**[ \t]*([STB]-\d{4,})\b[:.\s—-]*\**[ \t]*(?P<title>[^\n]*)$',
                  re.MULTILINE)
FENCE = re.compile(r'```.*?```', re.DOTALL)
CODE_SPAN = re.compile(r'`[^`\n]*`')


def _prose(text):
    return CODE_SPAN.sub(' ', FENCE.sub(' ', text or ''))


def _norm(title):
    return re.sub(r'[^a-z0-9]+', ' ', str(title or '').lower()).strip()


def session_claims(cl, item):
    """The claims whose claimant names ``item`` (a lane job is named after its item)."""
    key = (item or '').lower()
    return [c for c in cl if key and key in (c.claimant or '').lower()]


def check_doc(text, canonical, cl, item=None):
    """Findings (one line each) for the ids ``text`` mints; empty when the doc may land."""
    prose = _prose(text)
    out = []
    for m in DECL.finditer(prose):
        iid, title = m.group(1), m.group('title').strip(' *')
        rec = canonical.get(iid)
        if rec is not None and title and _norm(title) != _norm(rec['meta'].get('title')):
            out.append(f"{iid} already exists in the record ({rec['meta'].get('title')!r})")
    mine = session_claims(cl, item)
    active = {c.prefix for c in cl}
    for iid in dict.fromkeys(m.group(0) for m in ID_TOKEN.finditer(prose)):
        if iid in canonical or iid[0] not in active:
            continue
        own = [c for c in mine if c.prefix == iid[0]]
        pool = own or [c for c in cl if c.prefix == iid[0]]
        if idclaim.covers(pool, iid) is None:
            blocks = ', '.join(f'{c.prefix}:{c.lo:04d}-{c.hi:04d}' for c in own)
            where = f"{item}'s claimed block ({blocks})" if own else 'every claimed block'
            out.append(f"{iid} is outside {where} — an id no claim covers")
    return out


def task_key(parent, stories, writes):
    return (parent or '', tuple(sorted(set(stories or ()))), tuple(sorted(set(writes or ()))))


def duplicate_task(canonical, parent, stories, writes, extra=None):
    """The id of an open Task (or of an ``extra`` ``{id: key}`` minted this pass) with the same
    ``(parent, stories, writes)``, or None. A Task without a writes footprint is never matched."""
    if not writes:
        return None
    key = task_key(parent, stories, writes)
    for iid, k in (extra or {}).items():
        if k == key:
            return iid
    for iid, rec in sorted(canonical.items()):
        meta = rec['meta']
        if meta.get('type') != 'task' or not is_open(rec):
            continue
        if task_key(meta.get('parent'), meta.get('stories'), meta.get('writes')) == key:
            return iid
    return None
